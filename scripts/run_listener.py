"""
主入口：连接 Discord，监听所有 enabled 频道，触发 handle_message
用法：
  python scripts/run_listener.py

环境变量（config/.env）:
  DISCORD_USER_TOKEN  必填
  DRY_RUN        true/false（默认 true，安全起见）
  MOOMOO_TRD_ENV SIMULATE/REAL
"""
import sys
import os
import asyncio
import signal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
ENV_PATH = Path(__file__).resolve().parent.parent / "config" / ".env"
load_dotenv(ENV_PATH, override=True)

import discord
from loguru import logger

from src.config.channel_loader import registry, validate_channels
from src.listener.discord_client import handle_message
from src.notifier.telegram_client import send_telegram, format_error
from src.risk.risk_manager import get_daily_stats
from src.broker.moomoo_client import (
    probe_broker, probe_quote_access,
    QUOTE_OK, QUOTE_DELAYED, QUOTE_NO_PERMISSION, QUOTE_ERROR,
)
from src.position.sl_watcher import run_sl_watcher
from src.position.eod_watcher import run_eod_watcher
from src.position.tp_watcher import run_tp_watcher


# ============ 启动检查 ============

def preflight() -> str:
    """启动前检查，返回 token"""
    token = os.getenv("DISCORD_USER_TOKEN", "").strip()
    if not token:
        logger.error("❌ DISCORD_USER_TOKEN 未配置")
        sys.exit(1)

    enabled_ids = registry.enabled_channel_ids()
    if not enabled_ids:
        logger.error("❌ config/channels.json 没有任何 enabled 频道")
        sys.exit(1)

    dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
    trd_env = os.getenv("MOOMOO_TRD_ENV", "SIMULATE")

    # 实盘/模拟盘真单模式都需要 ACC_ID。空着启动 → 接到信号才报错的悲剧
    # （6/18 IWM 卖单失败就是这个原因）。
    if not dry_run:
        try:
            acc_id = int(os.getenv("MOOMOO_ACC_ID", 0))
        except ValueError:
            acc_id = 0
        if acc_id == 0:
            logger.error(
                "❌ DRY_RUN=False 但 MOOMOO_ACC_ID 未配置或为 0。\n"
                "   检查 config/.env 里 MOOMOO_ACC_ID=<数字> 是否存在且非零。\n"
                "   不强制 exit 反而下单时才暴露，会丢真实信号。"
            )
            sys.exit(1)
    
    logger.info("=" * 60)
    logger.info("🚀 Discord Copytrade Listener 启动")
    logger.info("=" * 60)
    logger.info(f"DRY_RUN       = {dry_run}  {'(不会真下单)' if dry_run else '⚠️  真实下单!'}")
    logger.info(f"MOOMOO_TRD_ENV = {trd_env}")
    logger.info(f"监听频道数    = {len(enabled_ids)}")
    for cid in enabled_ids:
        cfg = registry.get(cid)
        logger.info(f"  • {cfg.name} (id={cid}, qty={cfg.default_qty}, "
                    f"max_price=${cfg.max_price}, triggers={cfg.trigger_user_ids})")

    # max_price 高额提示。不再 sys.exit——真盘单笔成本由 risk_manager Layer 2 硬卡 $1000，
    # 单张价 × 100 × qty 任何超过 $1000 的订单都会被 check_order 拒绝。
    # channels.json 的 max_price 现在主要服务 SIMULATE 测试灵活性。
    HIGH_MAX_PRICE_THRESHOLD = 50.0  # >$50/张就当成"明显放宽了 Layer 1"
    high_price_channels = [
        (cid, registry.get(cid))
        for cid in enabled_ids
        if registry.get(cid).max_price > HIGH_MAX_PRICE_THRESHOLD
    ]
    if high_price_channels:
        is_simulate = trd_env.strip().upper() == "SIMULATE"
        for cid, cfg in high_price_channels:
            if is_simulate or dry_run:
                tag = "OK · SIMULATE"
            else:
                tag = "REAL · Layer 2 $1000 兜底"
            logger.warning(
                f"  ⚠️  {cfg.name} max_price=${cfg.max_price} > ${HIGH_MAX_PRICE_THRESHOLD} [{tag}]"
            )
        if not is_simulate and not dry_run:
            logger.warning(
                "   注意：REAL 单笔成本被 risk_manager 硬卡 $1000，超额订单会在 check_order 被拒绝"
            )

    # Broker 启动探测：避免昨晚那种"运行一夜才发现 broker 链路是死的"
    logger.info("─" * 60)
    logger.info("🔍 Broker 健康探测...")
    ok, msg = probe_broker()
    if ok:
        logger.info(f"  ✅ {msg}")
    else:
        logger.error(f"  ❌ {msg}")
        if not dry_run:
            logger.error("DRY_RUN=False 但 broker 不可用，拒绝启动。修复后重试。")
            sys.exit(1)
        else:
            logger.warning("DRY_RUN=True，broker 探测失败但允许继续（不会真下单）")

    # 期权行情订阅探测：决定 SL/TP/EOD watcher 真盘是否真能工作
    # 不阻塞启动，只 log；运营自己决定是否升级订阅
    logger.info("🔍 OPRA 行情订阅探测...")
    quote_status, quote_msg = probe_quote_access()
    if quote_status == QUOTE_OK:
        logger.info(f"  ✅ {quote_msg}")
    elif quote_status == QUOTE_DELAYED:
        logger.warning(f"  ⚠️  delayed-data tier:\n{quote_msg}")
    elif quote_status == QUOTE_NO_PERMISSION:
        logger.warning(f"  ⚠️  no permission:\n{quote_msg}")
    else:  # QUOTE_ERROR
        logger.error(f"  ❌ {quote_msg}")
    
    stats = get_daily_stats()
    from src.risk.risk_manager import _effective_max_cost_per_order
    per_order_cap = _effective_max_cost_per_order()
    logger.info(f"今日风控      : {stats['order_count']}/{stats['max_orders']} 单, "
                f"${stats['total_cost']}/${stats['max_cost']}")
    logger.info(f"单笔成本上限  : ${per_order_cap:,.0f} (REAL 硬卡 $1000，SIMULATE 不限)")
    if stats["circuit_broken"]:
        logger.warning(f"🚨 当日已熔断: {stats['circuit_reason']}")
    logger.info("=" * 60)
    
    return token


# ============ Discord Client ============

client = discord.Client()


# on_ready 每次重连都会触发（discord.py-self 约 20-30 min 一次），
# 只在首次连接时发 TG 启动通知，避免刷屏。
_startup_notified = False


@client.event
async def on_ready():
    global _startup_notified
    logger.info(f"✅ Discord logged in as: {client.user} (id={client.user.id})")

    # 仅首次 on_ready 做完整频道校验（REST fetch），重连只 log 不重新探测
    if _startup_notified:
        _log_reconnect_time("logged back in")
        return
    _startup_notified = True

    failures = await validate_channels(client)
    enabled_count = len(registry.enabled_channel_ids())

    if failures:
        lines = "\n".join(f"• {name} (id={cid}): {reason}" for cid, name, reason in failures)
        try:
            await send_telegram(
                f"⚠️ 频道配置校验失败\n"
                f"{len(failures)}/{enabled_count} 个频道无法解析:\n{lines}\n"
                f"请检查 config/channels.json",
                parse_mode=None,
            )
        except Exception as e:
            logger.warning(f"Telegram channel-failure notify failed: {e}")
        if len(failures) == enabled_count:
            logger.error(
                "❌ 所有 enabled 频道都校验失败，listener 没有消息源 — 退出。"
                " 修复 config/channels.json 后重启。"
            )
            await client.close()
            return

    try:
        await send_telegram(
            f"🟢 Listener 启动\n"
            f"账号: {client.user}\n"
            f"监听: {enabled_count - len(failures)}/{enabled_count} 频道有效\n"
            f"DRY_RUN: {os.getenv('DRY_RUN', 'true')}",
            parse_mode=None,
        )
    except Exception as e:
        logger.warning(f"Telegram startup notify failed: {e}")


@client.event
async def on_message(message):
    try:
        await handle_message(message)
    except Exception as e:
        logger.exception(f"on_message crashed: {e}")
        try:
            await send_telegram(format_error("on_message", str(e)))
        except Exception:
            pass


@client.event
async def on_message_edit(before, after):
    # 暂不触发下单，只记录（防止 KC 改单价导致重复触发）
    if registry.is_monitored(after.channel.id):
        logger.info(f"✏️  [edit] {after.channel.name}: {after.content[:80]}")


# 诊断断线原因：6/18 出现 29 次重连（vs 之前 2-4 次/天）
# discord.py-self 不会自动 log reason，得自己挂 event handler。
#
# 6/24 观察：on_disconnect 总是成对触发（< 1s 内两次），疑似 discord.py-self
# 内部 WS + HTTP 两路 close 各发一次 event。加防抖：< 3s 内的重复只算一次。
# 同时记录 disconnect → reconnect 用时，方便后续判断网络/库/系统层问题。
import time as _time
import re as _re
import logging as _stdlib_logging

_DISCONNECT_DEBOUNCE_SEC = 3.0
_last_disconnect_ts: float = 0.0  # monotonic 秒
_disconnect_in_progress: bool = False
_last_close_code: str = ""  # 最近一次 WS 关闭码（由下面的 logging 捕获填充）

# Storm 检测：60s 窗口内出现 >= 3 次真断线 → TG 告警一次
# 背景：6/30 04:36-04:43 出现 7min identify-rate-limit storm，bot 实际离线 7min。
# 这个窗口里若 KC 发信号会丢。debounce 只过滤成对 callback，storm 是更深层故障。
_STORM_WINDOW_SEC = 60.0
_STORM_THRESHOLD = 3
_recent_disconnects: list = []   # monotonic 时间戳 list
_storm_notified_at: float = 0.0  # 防止 storm 期间 TG 重复轰炸
_STORM_NOTIFY_COOLDOWN_SEC = 300.0  # 5 分钟内只告警一次


# 关闭码速查（来自 RFC 6455 + Discord）：
#   1000 normal closure（干净，库主动 reconnect）
#   1001 going away（peer 主动关）
#   1006 abnormal closure（没收到 close frame）—— Mac WiFi 睡 / NAT 超时 / 网络丢包典型
#   4000 unknown error / 4001 unknown opcode / 4002 decode error
#   4003 not authenticated / 4004 authentication failed
#   4007 invalid seq / 4008 rate limited / 4009 session timeout
#   4010-4014 invalid params（shard / version / 等）
# 1006 集中 → 网络/系统层；4xxx 集中 → Discord 侧 / 自身账号问题
_CLOSE_CODE_RE = _re.compile(r"\b(\d{4})\b")


class _DiscordGatewayLogCapture(_stdlib_logging.Handler):
    """抓 discord.py-self 内部 gateway log 里的 close code。

    discord.py-self 自己用 stdlib logging 打 'Webocket Closed with 1006' 这类，
    不走我们的 loguru。这里附一个 handler 转发关键消息过来，并提取关闭码
    放到 _last_close_code，供下次 on_disconnect 打到 log 里关联。
    """
    def emit(self, record):
        global _last_close_code
        try:
            msg = record.getMessage()
            lower = msg.lower()
            if not any(k in lower for k in ("close", "disconnect", "reconnect", "resumed")):
                return
            m = _CLOSE_CODE_RE.search(msg)
            if m:
                _last_close_code = m.group(1)
                logger.warning(f"[gateway code={_last_close_code}] {msg[:200]}")
            else:
                # 不带 code 但是 close/resume 类，info 级
                logger.info(f"[gateway] {msg[:200]}")
        except Exception:
            pass


def _install_gateway_log_capture():
    """启动时调一次。不要重复挂否则会重复输出。"""
    h = _DiscordGatewayLogCapture()
    h.setLevel(_stdlib_logging.INFO)
    # 挂在 discord 根 logger，覆盖 discord.client / discord.gateway / discord.http
    _stdlib_logging.getLogger("discord").addHandler(h)
    # 同时确保它的 level 够低能看到 INFO+
    _stdlib_logging.getLogger("discord").setLevel(_stdlib_logging.INFO)


_install_gateway_log_capture()


@client.event
async def on_disconnect():
    global _last_disconnect_ts, _disconnect_in_progress, _last_close_code
    global _storm_notified_at
    now = _time.monotonic()
    if _disconnect_in_progress and (now - _last_disconnect_ts) < _DISCONNECT_DEBOUNCE_SEC:
        # 同一次断线的成对回调，抑制重复日志
        return
    _last_disconnect_ts = now
    _disconnect_in_progress = True
    code_suffix = f" code={_last_close_code}" if _last_close_code else ""
    logger.warning(f"⚠️  Discord on_disconnect fired (websocket dropped){code_suffix}")
    _last_close_code = ""

    # storm 检测：60s 窗口里累计 >= 3 次 → TG 告警一次
    _recent_disconnects.append(now)
    cutoff = now - _STORM_WINDOW_SEC
    while _recent_disconnects and _recent_disconnects[0] < cutoff:
        _recent_disconnects.pop(0)
    if len(_recent_disconnects) >= _STORM_THRESHOLD:
        if now - _storm_notified_at >= _STORM_NOTIFY_COOLDOWN_SEC:
            _storm_notified_at = now
            n = len(_recent_disconnects)
            logger.error(
                f"🌀 Discord storm: {n} disconnects in last "
                f"{_STORM_WINDOW_SEC:.0f}s — bot may be offline soon"
            )
            try:
                # 用 plain text 避免 markdown 转义出意外
                await send_telegram(
                    f"🌀 Discord 重连风暴：{_STORM_WINDOW_SEC:.0f}s 内 {n} 次断线。\n"
                    f"可能进入 identify-rate-limit 退避（最长 ~3min）。"
                    f"建议盯一下盘，必要时手动重启 listener。",
                    parse_mode=None,
                )
            except Exception as e:
                logger.warning(f"storm TG notify failed: {e}")


def _log_reconnect_time(label: str):
    """on_ready / on_resumed 复用：算 disconnect→reconnect 用时"""
    global _last_disconnect_ts, _disconnect_in_progress
    if not _disconnect_in_progress:
        return
    elapsed_ms = (_time.monotonic() - _last_disconnect_ts) * 1000
    logger.info(f"🔄 Discord {label} after {elapsed_ms:.0f}ms")
    _disconnect_in_progress = False


@client.event
async def on_resumed():
    _log_reconnect_time("session resumed")


@client.event
async def on_error(event_name, *args, **kwargs):
    logger.exception(f"❌ Discord on_error in '{event_name}'")


# ============ 优雅退出 ============

async def shutdown():
    logger.info("收到退出信号，关闭 Discord client...")
    try:
        await send_telegram("🔴 *Listener 退出*")
    except Exception:
        pass
    await client.close()


def setup_signal_handlers(loop):
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown()))


# ============ 主入口 ============

async def main():
    token = preflight()
    loop = asyncio.get_running_loop()
    setup_signal_handlers(loop)

    # 保护性 watcher：SL 止损 / EOD 到期强平 / TP 分批止盈。
    # 之前只有 src.main（start_listener）启动它们，而这个生产入口一直没起——
    # src.main 又被硬卡禁止在 REAL 运行，等于真盘持仓完全没有自动保护。
    # watcher 内部自带 try/except + DRY_RUN 无报价时 no-op，起在这里是安全的。
    asyncio.create_task(run_sl_watcher(), name="sl_watcher")
    asyncio.create_task(run_eod_watcher(), name="eod_watcher")
    asyncio.create_task(run_tp_watcher(), name="tp_watcher")
    logger.info("🛡️  watchers started: sl / eod / tp")

    try:
        await client.start(token)
    except discord.LoginFailure:
        logger.error("❌ Discord 登录失败：token 失效或被风控")
        try:
            await send_telegram("❌ *Listener 启动失败*\nDiscord token 失效")
        except Exception:
            pass
        sys.exit(1)
    except Exception as e:
        logger.exception(f"Discord client crashed: {e}")
        try:
            await send_telegram(format_error("Discord client", str(e)))
        except Exception:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("用户 Ctrl+C 退出")
