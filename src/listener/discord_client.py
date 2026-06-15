"""
Discord self-bot 监听器（多频道版）

主链路：
  Discord message
    ↓ channel 过滤（registry.is_monitored）
    ↓ user 过滤（cfg.is_trigger_user）
    ↓ dedup（防止 edit/重连重复处理）
    ↓ parse_signal
    ↓ risk_manager.check_order  ← 全局熔断 + 单笔/日累计/日次数
    ↓ broker.place_order        ← 实际下单（DRY_RUN 时只 mock）
    ↓ risk_manager.record_order ← 写库累计统计
    ↓ telegram 通知

注意：
- on_message_edit 只记录日志，不重新触发下单（避免重复下单风险）
- 一切异常都吞掉只 log，不让 Discord 链路崩
- broker.place_order 是同步函数，必须用 asyncio.to_thread 包
"""
import os
import sys
import asyncio
from collections import deque
from datetime import datetime
from pathlib import Path

import discord
from dotenv import load_dotenv

# 让 src 可以从任何启动路径 import
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

load_dotenv(Path(__file__).resolve().parents[2] / "config" / ".env", override=True)

from src.parser.signal_parser import parse_signal, detect_action
from src.broker.moomoo_client import place_order
from src.config.channel_loader import registry
from src.risk.risk_manager import check_order, record_order
from src.notifier.telegram_client import (
    send_telegram_sync,
    format_signal_alert,
    format_order_filled,
    format_risk_blocked,
    format_error,
)
from src.storage.logger_db import log_raw_signal, log_order
from src.utils.logger import logger

TOKEN = os.getenv("DISCORD_USER_TOKEN")

client = discord.Client()

# ============================================================
# Dedup: bounded deque + set，O(1) 查重，避免无限增长
# ============================================================
# 场景：同一条 message 被 on_message 和 on_message_edit 都触发；
#       或断线重连时 ready 阶段重放历史 message
_DEDUP_MAX = 2000
_processed_msg_ids: deque = deque(maxlen=_DEDUP_MAX)
_processed_set: set = set()


def _seen(msg_id: int) -> bool:
    """O(1) 检查 + 记录。返回 True 表示之前见过。"""
    if msg_id in _processed_set:
        return True
    if len(_processed_msg_ids) >= _DEDUP_MAX:
        # 即将顶出最老的 ID，同步从 set 删掉
        old = _processed_msg_ids[0]
        _processed_set.discard(old)
    _processed_msg_ids.append(msg_id)
    _processed_set.add(msg_id)
    return False


# ============================================================
# Discord events
# ============================================================
@client.event
async def on_ready():
    """连接成功时打印监听的频道清单 + 检查可见性。"""
    logger.info(f"Discord logged in as: {client.user} (id={client.user.id})")

    enabled = registry.enabled_channel_ids()
    logger.info(f"Monitoring {len(enabled)} channel(s):")
    for cid in enabled:
        cfg = registry.get(cid)
        ch = client.get_channel(cid)
        if ch is None:
            # 看不到说明：token 没那个频道权限 / 不在那个服务器 / 频道 ID 错
            logger.error(f"  ❌ {cfg.name} ({cid}) NOT visible!")
        else:
            guild = ch.guild.name if ch.guild else "DM"
            logger.info(
                f"  ✅ {cfg.name} ({cid}) → #{ch.name} @ {guild} "
                f"qty={cfg.default_qty} max_price={cfg.max_price}"
            )


@client.event
async def on_message(message):
    """信号处理主入口。"""
    # 推到 handle_message 是为了让 test_full_flow 可以直接调，绕过真 Discord
    await handle_message(message)


@client.event
async def on_message_edit(before, after):
    """
    只记录编辑历史，不重新触发下单。

    原因：作者经常发完信号后微调价格/strike，如果重新触发会重复下单。
    TODO P2: 保存编辑历史到 DB 用于复盘分析
    """
    cid = after.channel.id
    if not registry.is_monitored(cid):
        return
    cfg = registry.get(cid)
    if not cfg.is_trigger_user(after.author.id):
        return
    logger.info(
        f"✏️  Edit in {cfg.name} from {after.author.name}:\n"
        f"  BEFORE: {before.content}\n"
        f"  AFTER:  {after.content}"
    )


# ============================================================
# 核心处理函数（抽出来方便测试，FakeMessage 也能调）
# ============================================================
async def handle_message(message):
    """
    处理一条消息。message 可以是真 discord.Message，也可以是 FakeMessage。
    需要的字段：
        message.id (int)
        message.channel.id (int)
        message.channel.name (str)
        message.author.id (int)
        message.author.name (str)
        message.content (str)
        message.embeds (list, optional)
        message.attachments (list, optional)
    """
    t0 = datetime.now()

    # ---- 过滤 1：忽略自己发的消息 ----
    if client.user and message.author.id == client.user.id:
        return

    # ---- 过滤 2：channel 必须在监听列表 ----
    cid = message.channel.id
    if not registry.is_monitored(cid):
        return

    cfg = registry.get(cid)

    # ---- 过滤 3：作者必须是触发用户 ----
    if not cfg.is_trigger_user(message.author.id):
        return

    # ---- 过滤 4：dedup ----
    if _seen(message.id):
        logger.debug(f"Skip duplicate msg {message.id}")
        return

    raw = message.content or ""
    if not raw.strip():
        # 空内容可能是纯 embed / 纯附件（图片信号）—— 现在 parser 不支持，先 skip
        logger.warning(
            f"Empty content from {message.author.name}, "
            f"embeds={len(getattr(message, 'embeds', []))}, "
            f"attachments={len(getattr(message, 'attachments', []))}"
        )
        return

    logger.info(f"📩 [{cfg.name}] {message.author.name}: {raw}")

    # 落库原始信号（即使后面失败也留底）
    try:
        log_raw_signal(message.id, message.author.name, raw, t0)
    except Exception as e:
        logger.error(f"log_raw_signal failed: {e}")

    # ---- 检测 OPEN / CLOSE ----
    action = detect_action(raw)
    if action == "CLOSE":
        # TODO P2: 自动平仓需要先实现持仓追踪 + moomoo 卖单
        logger.info("Close signal detected, skip (TODO P2)")
        await _safe_notify(f"[CLOSE - skipped]\n{raw}")
        return

    # ---- 解析信号 ----
    signal = parse_signal(raw)
    if not signal:
        logger.warning("Parse failed")
        await _safe_notify(format_error("Parse failed", raw))
        return

    # ---- 多信号：取第一个 ----
    # TODO: parser 已重写为只返回 dict|None，此分支当前不可达。
    # 保留作为防御层；若未来 parser 改回支持多信号 list，此处自动生效。
    # 触达后请确认是否还要 Telegram 告警（用户目前规则：不做多腿）。
    if isinstance(signal, list):
        all_signals = signal
        signal = all_signals[0]
        logger.info(
            f"Multi-signal ({len(all_signals)}), taking first: "
            f"{signal['symbol']} {signal['strike']}{signal['side'][0]}"
        )
        await _safe_notify(
            f"⚠️ Multi-signal ({len(all_signals)} contracts), taking first only:\n"
            + "\n".join(
                f"  {i+1}. {s['symbol']} {s['strike']}{s['side'][0]} "
                f"{s['expiry']} @ ${s['price']}"
                for i, s in enumerate(all_signals)
            )
        )

    # TODO P3: symbol blacklist 检查

    # 解析成功后立刻发预警（让用户知道收到了，正在处理）
    await _safe_notify(format_signal_alert(
        cfg.name,
        signal["symbol"],
        signal["strike"],
        signal["expiry"],
        signal["side"][0],
        signal.get("price", 0) or 0,
        cfg.default_qty,
        signal.get("action", "OPEN"),
    ))

    # ============================================================
    # 风控检查
    # ============================================================
    # 关键参数说明：
    # - max_price_override: channel 的 max_price 覆盖全局 MAX_PRICE_PER_CONTRACT
    # - qty 来自 channel 配置，不同 channel 可以设不同张数
    qty = cfg.default_qty

    risk_result = check_order(
        price=signal["price"],
        qty=qty,
        symbol=signal["symbol"],
        strike=signal["strike"],
        side=signal["side"],
        expiry=signal.get("expiry", ""),
        channel_name=cfg.name,
        max_price_override=cfg.max_price,
    )

    if not risk_result.passed:
        logger.warning(
            f"🛡️  Risk blocked: {risk_result.reason} - {risk_result.detail}"
        )
        await _safe_notify(format_risk_blocked(risk_result.reason, risk_result.detail))
        return

    # ============================================================
    # 下单（broker.place_order 是同步函数，必须 to_thread 包装）
    # ============================================================
    try:
        order_result = await asyncio.to_thread(place_order, signal, qty)
    except Exception as e:
        logger.exception("place_order failed")
        await _safe_notify(format_error("Order error", str(e)))
        return

    # 落库订单（业务日志，跟风控的 record_order 是两件事）
    try:
        log_order(message.id, signal, order_result)
    except Exception as e:
        logger.error(f"log_order failed: {e}")

    # 记录风控累计（DRY_RUN 也算 —— 因为我们要测试累计统计的准确性）
    # 真实场景下，如果下单失败可能不该计数，但保守起见还是计：
    # 宁可让今日额度变紧，也不要因为 broker 误报"失败"而漏算实际成交
    try:
        record_order(
            price=signal["price"],
            qty=qty,
            symbol=signal["symbol"],
            strike=signal["strike"],
            side=signal["side"],
            expiry=signal.get("expiry", ""),
            channel_id=str(cid),
            channel_name=cfg.name,
        )
    except Exception as e:
        logger.error(f"record_order failed: {e}")

    # ---- 通知 + 延迟统计 ----
    elapsed = (datetime.now() - t0).total_seconds() * 1000
    await _safe_notify(format_order_filled(
        signal["symbol"],
        signal["strike"],
        signal["side"][0],
        signal["expiry"],
        order_result.get("price", signal.get("price", 0) or 0),
        order_result.get("qty", cfg.default_qty),
        order_result.get("order_id", "N/A"),
    ))
    logger.info(f"⏱️  End-to-end latency: {elapsed:.0f}ms")


# ============================================================
# 工具：安全的 Telegram 通知（吞异常）
# ============================================================
async def _safe_notify(msg: str):
    """
    发 Telegram，失败只 log 不抛。
    用 to_thread 是因为 send_telegram_sync 内部用 httpx 同步调用，
    不能在事件循环里阻塞。
    """
    try:
        await asyncio.to_thread(send_telegram_sync, msg)
    except Exception as e:
        logger.error(f"telegram notify failed: {e}")


# ============================================================
# 启动入口
# ============================================================
async def start_listener():
    """供 main.py 调用的启动函数。"""
    if not TOKEN:
        raise RuntimeError("DISCORD_USER_TOKEN not configured in config/.env")
    await client.start(TOKEN)
