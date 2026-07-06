"""
Discord self-bot 监听器（多频道版）

主链路：
  Discord message
    ↓ channel 过滤（registry.is_monitored）
    ↓ user 过滤（cfg.is_trigger_user）
    ↓ dedup（防止 edit/重连重复处理）
    ↓ parse_signal（传 msg_ts 用 ET 日期）
    ↓ risk_manager.check_order
    ↓ broker.place_order
    ↓ record_order ← 仅 success=True 才记（避免污染配额）
    ↓ telegram 通知

注意：
- on_message_edit 只记录日志，不重新触发下单
- 一切异常都吞掉只 log，不让 Discord 链路崩
- broker.place_order 是同步函数，必须用 asyncio.to_thread 包
"""
import os
import sys
import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from dotenv import load_dotenv

# 让 src 可以从任何启动路径 import
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

load_dotenv(Path(__file__).resolve().parents[2] / "config" / ".env", override=True)

from src.parser.signal_parser import parse_signal, detect_action
from src.parser.close_parser import parse_close
from src.broker.moomoo_client import (
    place_order,
    place_sell_order,
    breakeven_exit_price,
    calc_limit_price,
)
from src.config.channel_loader import registry, validate_channels
from src.risk.risk_manager import check_order, record_order
from src.notifier.telegram_client import (
    send_telegram,
    format_signal_alert,
    format_order_filled,
    format_risk_blocked,
    format_error,
    format_close_filled,
    format_close_skipped,
    format_addon_alert,
)
from src.storage.logger_db import log_raw_signal, log_order
from src.position import manager as position_mgr
from src.position import fill_checker
from src.position.sl_watcher import run_sl_watcher
from src.position.eod_watcher import run_eod_watcher
from src.position.tp_watcher import run_tp_watcher
from src.utils.logger import logger

TOKEN = os.getenv("DISCORD_USER_TOKEN")
ET_TZ = ZoneInfo("America/New_York")

client = discord.Client()

# OPEN 链路串行锁：check_order → place_order → record_order 必须原子，
# 否则两条几乎同时到达的信号都会用"旧配额"通过风控（见 handle_message 内注释）
_order_flow_lock = asyncio.Lock()

# ============================================================
# Dedup: bounded deque + set 查重，避免无限增长
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
# Dedup 层 2: signal fingerprint 去重 ===
# ============================================================
# 上面的 _seen() 只能拦 msg_id 重复（edit / 重连重放）。
# 但有一类场景 msg_id 不同、信号语义却相同，需要单独拦：
#
# 实测场景：
#   1. 翻译机器人独立中英文双发
#      MRVL 5/11 15:23:51（中文）+ 15:23:53（英文）间隔 2s，两条独立 msg
#   2. KC 同信号重发
#      NNE 5/11 17:57:55 + 17:58:03 间隔 8s
#   3. KC 价格修正（fill 通知）
#      MRVL 5/15 $1.10 → $.95，间隔 68s，按"订阅第一信号"原则只跟首条
#   4. 跨频道转发（KC 主频道 + enrich 翻译版同一信号）
#
# 设计要点：
#   - key = symbol|side|strike|expiry_date（不含 price/channel/tags）
#     → 价格修正 / 跨频道都能拦
#   - 5 分钟窗口（实测最长间隔 ~68s 价格修正场景，留 4x 余量）
#   - dict 存 timestamp：惰性 GC + 硬上限保护，避免内存无限增长
#
# 拦截位置：parse 成功后、风控前。拦得越早越好（省 risk_check / TG / broker）。
FINGERPRINT_WINDOW = timedelta(minutes=5)
_FP_MAX = 200
_signal_fps: dict[str, datetime] = {}


def _signal_fingerprint(sig: dict) -> str:
    """同 symbol + side + strike + expiry_date → 视为同信号"""
    return (
        f"{sig['symbol']}|{sig['side']}|"
        f"{sig['strike']}|{sig.get('expiry_date', '')}"
    )


# ============================================================
# CLOSE dedup —— 中英文双发 / 同信号重发 拦截
# ============================================================
# 场景：KC 机器人翻译流程会发英文 + 中文两条；
#       现在 ZH parser 也能解析了，两条都会触发，需要二次拦截。
# key = (kind, sorted symbols, pct)  —— 不带 lang，跨语言去重
# 窗口 5min（同信号语义重发的时间尺度，参考 _is_duplicate_signal 设计）
_close_fps: dict[tuple, "datetime"] = {}
_CLOSE_FP_MAX = 100
CLOSE_FP_WINDOW = timedelta(minutes=5)


def _close_fingerprint(parsed: dict) -> tuple:
    """生成 CLOSE 信号 fingerprint。"""
    syms = tuple(sorted(parsed.get("symbols") or []))
    return (parsed["kind"], syms, parsed["pct"])


def _check_duplicate_close(parsed: dict) -> tuple[bool, float]:
    """只查重，不登记。返回 (是否重复, 距上次秒数)。

    登记动作拆到 _record_close_fp，由调用方在**至少一笔卖单成功提交后**调用。
    之前是查即登记：ZH 版先到但卖单失败（broker 异常/拒单）时，1-3s 后到的
    EN 版会被当 dup 拦掉——天然的重试机会没了。现在失败不登记，EN 版正常重试。
    代价：完全没匹配到持仓时两个语言版本各发一条 skip 通知（可接受）。
    """
    fp = _close_fingerprint(parsed)
    now = datetime.now(timezone.utc)

    expired = [k for k, ts in _close_fps.items() if now - ts > CLOSE_FP_WINDOW]
    for k in expired:
        _close_fps.pop(k, None)

    prev_ts = _close_fps.get(fp)
    if prev_ts is not None:
        return True, (now - prev_ts).total_seconds()
    return False, 0.0


def _record_close_fp(parsed: dict):
    """登记 CLOSE 指纹（含硬上限保护）。仅在实际执行成功后调用。"""
    if len(_close_fps) >= _CLOSE_FP_MAX:
        oldest = min(_close_fps, key=_close_fps.get)
        _close_fps.pop(oldest, None)
    _close_fps[_close_fingerprint(parsed)] = datetime.now(timezone.utc)


def _is_duplicate_signal(sig: dict) -> tuple[bool, float]:
    """
    返回 (是否重复, 距上次秒数)。

    每次调用都先惰性清理过期项 + 硬上限保护，
    极端情况下（窗口内来 200+ 不同信号）丢最老的。
    """
    fp = _signal_fingerprint(sig)
    now = datetime.now(timezone.utc)

    # 1. 惰性清理过期项（最多 200 个，O(n) 可接受）
    expired = [k for k, ts in _signal_fps.items() if now - ts > FINGERPRINT_WINDOW]
    for k in expired:
        _signal_fps.pop(k, None)

    # 2. 硬上限保护
    if len(_signal_fps) >= _FP_MAX:
        oldest = min(_signal_fps, key=_signal_fps.get)
        _signal_fps.pop(oldest, None)

    # 3. 查重
    prev_ts = _signal_fps.get(fp)
    if prev_ts is not None:
        ago = (now - prev_ts).total_seconds()
        return True, ago

    # 4. 记录
    _signal_fps[fp] = now
    return False, 0.0


# ============================================================
# 工具：从 message 提取 ET 日期（给 parser 用）
# ============================================================
def _extract_et_date(message) -> "date":
    """
    Discord message.created_at 是 UTC aware datetime（discord.py 保证）。
    转 ET 后取 date，用于 parser 计算 expiry（如 weekly → _next_friday）。

    FakeMessage（test_full_flow）可能没 created_at，fallback 到 utcnow。
    """
    created = getattr(message, "created_at", None)
    if created is None:
        created = datetime.now(timezone.utc)
    elif created.tzinfo is None:
        # 极端兜底（不应发生）：discord.py 始终返回 aware
        created = created.replace(tzinfo=timezone.utc)
    return created.astimezone(ET_TZ).date()


# ============================================================
# Discord events
# ============================================================

# on_ready 每次 gateway 重连都会触发（约 20-30 min 一次），
# 频道校验 + TG 报警只做一次，避免重连刷屏 + REST 限流
_channels_validated = False


@client.event
async def on_ready():
    global _channels_validated
    logger.info(f"Discord logged in as: {client.user} (id={client.user.id})")
    if _channels_validated:
        return
    _channels_validated = True

    failures = await validate_channels(client)
    if failures:
        lines = "\n".join(
            f"• {name} (id={cid}): {reason}" for cid, name, reason, _ in failures
        )
        await _safe_notify(format_error(
            "频道配置校验失败",
            f"{len(failures)}/{len(registry.enabled_channel_ids())} 个频道无法解析:\n{lines}\n\n"
            f"请检查 config/channels.json 的 channel_id"
        ))
        # 只有全部失败且**全部是确定性失败**（404/403 = 配置真的错了）才退出；
        # 网络抖动/限流这类瞬时失败重连后会自愈，退出反而把 bot 干死在夜里
        all_definitive = all(definitive for _, _, _, definitive in failures)
        if len(failures) == len(registry.enabled_channel_ids()) and all_definitive:
            logger.error(
                "❌ 所有 enabled 频道都确定性校验失败，listener 没有任何消息源 — 退出。"
                " 修复 config/channels.json 后重启。"
            )
            await client.close()
            return


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
# 核心处理函数
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
    try:
        await _handle_message_inner(message)
    except Exception as e:
        # 最后防线：任何未预料的异常都不能让信号静默消失。
        # discord.py 会把 event handler 的异常吞进默认 on_error（只写日志），
        # TG 侧完全看不到 —— 模块 docstring 承诺的"一切异常都吞掉只 log"
        # 在这里兑现，并显式报警让人工接管。
        logger.exception("handle_message crashed")
        raw = getattr(message, "content", "") or ""
        await _safe_notify(format_error(
            "handle_message crashed",
            f"{type(e).__name__}: {e}\n\nraw: {raw[:200]}",
        ))


async def _handle_message_inner(message):
    t0 = datetime.now(timezone.utc)

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
        await _handle_close_signal(raw, message.id)
        return

    # ---- 解析信号 ----
    # === [改动 Bug C] 传 msg_ts=ET 日期，避免 fallback 到 AEST 本地 ===
    msg_date_et = _extract_et_date(message)
    signal = parse_signal(raw, msg_ts=msg_date_et)

    # === [改动 Bug B] 区分 intentional skip vs 真·解析失败 ===
    if signal is None:
        # 真·没匹配任何模式。只对"含 $TICKER + 侧别 + 价格"三件套的发 TG，
        # 否则视为 KC 状态评论/行情解说，仅 log（避免每晚 10+ 条 TG 噪音，见 6/24 review）
        logger.warning("Parse failed")
        # 先查 add-on（更具体）：加已持仓标的 + @price → 提醒人工跟加（7/6 SPY 漏加实锤）
        addon_sym = _looks_like_addon_attempt(raw)
        if addon_sym:
            now = datetime.now(timezone.utc)
            prev = _addon_alerted.get(addon_sym)
            if prev is None or now - prev > _ADDON_ALERT_WINDOW:
                _addon_alerted[addon_sym] = now
                await _safe_notify(format_addon_alert(addon_sym, raw))
            else:
                logger.info(
                    f"🔁 addon alert dedup: {addon_sym} "
                    f"(prev {(now - prev).total_seconds():.0f}s ago)"
                )
        elif _looks_like_open_attempt(raw):
            await _safe_notify(format_error("Parse failed (looks like signal)", raw))
        return

    if signal.get("skip"):
        # parser 主动 skip（holding / price_range / no_price），属于正常过滤，不发 TG
        logger.debug(f"Parser intentional skip: {signal['skip']}")
        return

    # ---- 多信号（防御层，parser 当前不返 list） ----
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

    # ---- Fingerprint 去重 ----
    # 必须放在 parse 成功之后（要拿 symbol/strike/side/expiry_date 作 key）
    # 必须放在风控/TG/下单之前（拦得越早越省）
    # 不发 TG：双发场景下 TG 跟着双响会刷屏；只 log 留痕用于复盘
    is_dup, ago_sec = _is_duplicate_signal(signal)
    if is_dup:
        logger.info(
            f"🔁 Duplicate signal skipped: "
            f"{signal['symbol']} {signal['strike']}{signal['side'][0]} "
            f"{signal.get('expiry_date', '')} "
            f"(prev {ago_sec:.0f}s ago)"
        )
        return

    # TODO P3: symbol blacklist

    # 解析成功立即预警，带 breakeven 提示 + KC tags
    entry_p = signal.get("price", 0) or 0
    be_info = breakeven_exit_price(entry_p) if entry_p > 0 else None
    await _safe_notify(format_signal_alert(
        cfg.name,
        signal["symbol"],
        signal["strike"],
        signal["expiry"],
        signal["side"][0],
        entry_p,
        cfg.default_qty,
        signal.get("action", "OPEN"),
        breakeven=be_info,
        tags=signal.get("tags") or None,
    ))

    # ---- 风控 + 下单 + 配额记录：整段串行 ----
    # check_order 与 record_order 之间隔着 broker RTT（await），没有锁的话
    # 两条几乎同时到达的信号会都用"旧配额"通过 Layer 3/4 检查，
    # MAX_DAILY_COST / MAX_DAILY_ORDERS 可以被双双突破。
    # 信号频率是每天个位数，串行化整个下单段的延迟代价可以忽略。
    async with _order_flow_lock:
        # ---- 风控 ----
        # 关键参数说明：
        # - max_price_override: channel 的 max_price 覆盖全局 MAX_PRICE_PER_CONTRACT
        # - qty 来自 channel 配置，不同 channel 可以设不同张数
        # - effective_price: broker 实际会挂 signal_price × (1+5~12% slippage)，
        #   成本类风控（单笔/当日累计）必须按挂单价算，否则 REAL $1000 硬顶被滑点穿透
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
            effective_price=calc_limit_price(signal["price"]),
        )

        if not risk_result.passed:
            logger.warning(
                f"🛡️  Risk blocked: {risk_result.reason} - {risk_result.detail}"
            )
            await _safe_notify(format_risk_blocked(risk_result.reason, risk_result.detail))
            return

        # ---- 下单 ----
        # 下单（broker.place_order 是同步函数，必须 to_thread 包装）
        try:
            order_result = await asyncio.to_thread(place_order, signal, qty)
        except Exception as e:
            logger.exception("place_order failed")
            await _safe_notify(format_error("Order error", str(e)))
            return

        # 落库订单（成功失败都记，作为业务日志）
        try:
            log_order(message.id, signal, order_result)
        except Exception as e:
            logger.error(f"log_order failed: {e}")

        # === [改动 Bug A] 只有 success=True 才 record_order ===
        # 实测背景：6/16 QCOM/IREN 期权代码错误（Juneteenth 未处理），
        # broker 返回 success=False，但旧逻辑仍 record_order 污染配额，
        # 导致 daily_orders 表出现"成功记录"但订单实际未成交。
        # 现在失败 → 发 TG 提示用户 + 不污染配额。
        if not order_result.get("success"):
            err_msg = order_result.get("message", "unknown error")
            logger.error(f"Order rejected by broker: {err_msg}")
            await _safe_notify(format_error(
                "Order rejected by broker",
                f"{signal['symbol']} {signal['strike']}{signal['side'][0]} "
                f"{signal.get('expiry', '')}\n{err_msg}"
            ))
            return

        try:
            # 用 broker 实际挂单价计成本（含 slippage），否则 MAX_DAILY_COST 会被低估
            effective_price = order_result.get("price", signal["price"])
            record_order(
                price=effective_price,
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

    # ---- 持仓追踪（用于后续 close 信号匹配 / SL polling / EOD 强平） ----
    try:
        position_mgr.on_order_filled(
            signal=signal,
            order_result=order_result,
            channel_name=cfg.name,
            msg_id=str(message.id),
        )
    except Exception as e:
        logger.error(f"position_mgr.on_order_filled failed: {e}")

    # ---- 成交确认（fire-and-forget）----
    # broker success 只是"已提交"；确认成交后回填真实 avg_entry，
    # 超时未成交则 TG 告警提示对账。见 fill_checker 模块 docstring。
    fill_checker.spawn(fill_checker.confirm_buy_fill(
        order_result.get("order_id") or "",
        order_result.get("code") or "",
        order_result.get("qty", qty),
        order_result.get("price", 0.0) or 0.0,
    ))

    # ---- 通知 + 延迟统计 ----
    elapsed = (datetime.now(timezone.utc) - t0).total_seconds() * 1000
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
# CLOSE 信号处理
# ============================================================

# 卖单挂价相对当前 avg_entry 的负偏移 —— v1 直接用 avg_entry * (1 - SELL_SLIP)
# 简化策略：模拟盘没真实 bid，先用 entry 价做参照位；上真盘后改用 query 报价
# 偏移要够"吃"穿 bid，避免挂在 ask 上没人接
# TODO（实测调整）：
#  - 接入 moomoo 实时 quote 后，用 bid * (1 - SELL_SLIP)
#  - 分档：trim (pct<100) 用浅偏移、close (pct=100) 用深偏移
#  - SL/EOD 触发时用更激进偏移
SELL_SLIP = 0.05


def _calc_sell_limit(avg_entry: float, signal_price: float = None) -> "float | None":
    """卖出限价。

    优先级：
      1. signal_price 存在 → 用 KC 喊的价 × (1 - SELL_SLIP)
      2. signal_price 缺失 → 返回 None，**拒绝执行**

    设计原则（"宁错过不错杀"）：
    没有可靠价参照（信号没喊价 + OPRA 报价不可用）就不卖。
    用 avg_entry 当兜底参照会**锁定 -5% 亏损**——曾在 TSLA 案例上踩坑。

    TODO（OPRA 到位后）: signal_price 缺失时 fallback 到 last_price，
    都没有才返回 None。
    """
    if signal_price is None:
        return None
    return round(signal_price * (1 - SELL_SLIP), 2)


async def _handle_close_signal(raw: str, msg_id: int):
    """detect_action==CLOSE 时调用。

    流程：
      1. 查当前活跃持仓 symbols → 给 parser 做白名单消歧
      2. parse_close → dict | None
      3. 多 symbol 循环：每个 symbol 可能对应多 strike，全部按 pct 卖
      4. broker.place_sell_order（to_thread 包同步调用）
      5. position_mgr.on_close_filled 扣减
      6. Telegram 通知
    """
    open_symbols = position_mgr.get_open_symbols()
    if not open_symbols:
        logger.info("[CLOSE] no open positions, ignoring close signal")
        return

    parsed = parse_close(raw, open_symbols)
    if parsed is None:
        logger.info(f"[CLOSE] parser skipped: {raw[:80]}")
        # 只对"含 ticker + 价格 hint"的发 TG：捕获真漏检（如 ZH 公司名映射失败）
        # 过滤无 ticker 的 follow-up close（如 "trim runners here at 3.45"）
        if _looks_like_close_attempt(raw):
            await _safe_notify(format_close_skipped("parser skipped (recap/no-symbol)", raw))
        return

    # CLOSE dedup —— 双语双发 / 同信号重发拦截
    # 注意：这里只查重，指纹在下面"至少一笔卖单成功"后才登记（_record_close_fp），
    # 让执行失败时 1-3s 后到达的另一语言版本天然充当重试。
    is_dup, ago = _check_duplicate_close(parsed)
    if is_dup:
        logger.info(
            f"🔁 [CLOSE] dup skipped (lang={parsed.get('lang')}): "
            f"{parsed['kind']} symbols={parsed.get('symbols')} pct={parsed['pct']} "
            f"prev {ago:.1f}s ago"
        )
        return

    if parsed["kind"] == "BULK_TRIM":
        # 全仓 trim，遍历所有 open symbols
        targets = list(open_symbols)
    else:
        targets = parsed["symbols"]

    pct = parsed["pct"]
    any_executed = False
    any_success = False  # 至少一笔卖单成功提交 → 登记 CLOSE 指纹
    # broker 侧失败（异常/拒单）——这类失败是瞬时的，值得让 1-3s 后的
    # 双语孪生版本重试；确定性跳过（runner-preserve / 无价格参照 /
    # strike 不匹配）重试也是同样结果，不算在内
    any_broker_failure = False

    hint_strike = parsed.get("hint_strike")
    hint_side = parsed.get("hint_side")
    # hint 是从 symbols[0] 附近抽的（见 close_parser._extract_strike_hint），
    # 只能约束**那一个** symbol。用 TSLA 的 420c 去过滤 MSFT 的持仓，
    # 会把 MSFT 的平仓静默跳过（"Trimmed TSLA 420c and MSFT here" 案例）。
    hint_source_symbol = (parsed.get("symbols") or [None])[0]

    for symbol in targets:
        positions = position_mgr.find_by_symbol(symbol)
        if not positions:
            logger.warning(f"[CLOSE] {symbol} not found in open positions")
            continue

        # strike-aware filter：close 文本里显式给了 strike+side 时只关匹配的仓位。
        # 背景见 [docs/lessons.md](docs/lessons.md) #11：6/30 KC 平 TSLA 420c
        # 触发我们平 TSLA 425c，这次运气好两个 strike 价差小，下次未必。
        # 只对 hint 所属的 symbol 生效（多 symbol close 的其余 symbol 不受约束）。
        if hint_strike is not None and hint_side is not None and symbol == hint_source_symbol:
            matched = [
                p for p in positions
                if p["strike"] == hint_strike and p["side"] == hint_side
            ]
            if not matched:
                logger.warning(
                    f"[CLOSE] {symbol} {hint_strike}{hint_side[0]} hinted but "
                    f"no matching position (have: "
                    f"{[(p['strike'], p['side'][0]) for p in positions]}). Skipping."
                )
                await _safe_notify(format_close_skipped(
                    f"strike 不匹配（KC 平 {symbol} {hint_strike}{hint_side[0]} 但我们持仓不同 strike）",
                    raw,
                ))
                any_executed = True  # 已发专属 TG，不再让外层报 "no matching"
                continue
            logger.info(
                f"[CLOSE] strike-filter: {symbol} {hint_strike}{hint_side[0]} → "
                f"{len(matched)}/{len(positions)} positions selected"
            )
            positions = matched

        for pos in positions:
            # 卖出串行化：同一 option_code 上 SL/TP/EOD/CLOSE 四条路径互斥。
            # 锁内重读仓位——等锁期间 watcher 可能已经卖过了。
            async with position_mgr.sell_lock(pos["option_code"]):
                fresh = position_mgr.get(pos["option_code"])
                if fresh is not None:
                    pos = fresh
                if pos["status"] not in ("OPEN", "PARTIAL") or pos["qty_remaining"] <= 0:
                    logger.info(
                        f"[CLOSE] {pos['option_code']} already closed "
                        f"while waiting for lock, skip"
                    )
                    any_executed = True  # 有人处理过了，不报 "no matching"
                    continue
                qty_to_sell = position_mgr.calc_qty_to_sell(pos, pct)
                if qty_to_sell <= 0:
                    # runner-preserve（策略 A）：remaining=1 且 pct<100 故意跳过 trim。
                    # 必须发专属 TG 并标记"已处理"——否则落到外层
                    # "no matching open positions" 兜底文案（7/6 IBM 两次实锤，
                    # 半夜看到会以为仓位状态错乱）
                    await _safe_notify(format_close_skipped(
                        f"runner-preserve：{pos['symbol']} "
                        f"{pos['strike']}{pos['side'][0]} 剩 1 张，"
                        f"跳过 {pct}% trim（策略 A，等 100% 全平信号）",
                        raw,
                    ))
                    any_executed = True
                    continue
                limit = _calc_sell_limit(
                    pos["avg_entry_price"], parsed.get("signal_price"),
                )
                if limit is None:
                    # 信号没喊价 + OPRA 不可用 → 拒绝执行，TG 警报让人工接管
                    logger.warning(
                        f"[CLOSE] no price ref for {pos['option_code']}, "
                        f"skipping sell ({pct}%)"
                    )
                    await _safe_notify(format_error(
                        "CLOSE 跳过：无价格参照",
                        f"{pos['option_code']} qty={qty_to_sell} ({pct}%)\n"
                        f"原因：信号无价 + OPRA 报价不可用\n"
                        f"请在 moomoo 手动平仓\n\n"
                        f"原文: {raw[:200]}"
                    ))
                    any_executed = True  # 算"处理过"，不让外层再发 "no matching" 提示
                    continue
                logger.info(
                    f"[CLOSE] sell {pos['option_code']} qty={qty_to_sell} "
                    f"limit={limit} ({pct}%, ref=signal)"
                )
                try:
                    result = await asyncio.to_thread(
                        place_sell_order,
                        option_code=pos["option_code"],
                        qty=qty_to_sell,
                        limit_price=limit,
                        remark=f"kc_close_{pct}pct",
                    )
                except Exception as e:
                    logger.exception("place_sell_order failed")
                    await _safe_notify(format_error("Sell order error", str(e)))
                    any_executed = True  # 持仓找到了只是 broker 异常，不再报 "no matching"
                    any_broker_failure = True
                    continue

                if not result.get("success"):
                    err = result.get("message", "unknown")
                    logger.error(f"[CLOSE] sell rejected: {err}")
                    await _safe_notify(format_error(
                        "Sell rejected by broker",
                        f"{pos['option_code']} qty={qty_to_sell}\n{err}",
                    ))
                    any_executed = True  # 持仓找到了只是 broker 拒单，不再报 "no matching"
                    any_broker_failure = True
                    continue

                try:
                    position_mgr.on_close_filled(
                        option_code=pos["option_code"],
                        qty_sold=result.get("qty", qty_to_sell),
                        fill_price=result.get("price", limit),
                        trigger_source="kc_signal",
                        ref_msg_id=str(msg_id),
                        order_id=result.get("order_id"),
                        note=f"pct={pct} matched={parsed['matched'][:60]}",
                    )
                except Exception as e:
                    logger.error(f"on_close_filled failed: {e}")

                # 卖单成交确认：DB 已按已平处理，若限价单实际没成交必须告警
                fill_checker.spawn(fill_checker.confirm_sell_fill(
                    result.get("order_id") or "", pos["option_code"],
                    result.get("qty", qty_to_sell), "kc_close",
                ))

            await _safe_notify(format_close_filled(
                pos["symbol"], pos["strike"], pos["side"], pos["expiry"],
                result.get("qty", qty_to_sell), result.get("price", limit),
                pct, "kc_signal", result.get("order_id", "N/A"),
            ))
            any_executed = True
            any_success = True

    if any_success or not any_broker_failure:
        # 登记指纹拦掉 1-3s 后的双语孪生版本，两种情况：
        #   1. 至少一笔卖单成功——正常路径
        #   2. 全部是确定性结果（runner-preserve / 无价格参照 / strike 不匹配 /
        #      no matching）——孪生版本重试也是同样结果，只会重复刷 TG
        #      （7/6 实测：runner-preserve 和"无价格参照"各触发一次时，
        #      ZH/EN 双发若不去重会连发 2-4 条相同告警）
        # 唯一不登记的情况：零成交且出现过 broker 失败（异常/拒单）——
        # 让另一语言版本充当天然重试（0005 补丁的核心目的）。
        _record_close_fp(parsed)

    if not any_executed:
        await _safe_notify(format_close_skipped(
            "no matching open positions",
            f"parsed: kind={parsed['kind']} symbols={targets} pct={pct}\n\n{raw}",
        ))


# ============================================================
# 启发：判断"看起来像信号"，控制 TG 噪音
# ============================================================
# 背景：6/24 夜里 11 条 "Parse failed" TG 全是 KC 闲聊（"$RKLB - Boom."、
# "AMZN +50% who got paid?!" 之类），实际不是漏检信号，但每条都炸 TG。
# 改为：parse 失败时 → 只在文本"看起来真的想发信号"才 TG，否则 log.warn 收尾。

import re as _re

# 现成的"开仓"句法特征：含 TICKER + (Nc/p|calls/puts) + 价格-like 数字
# ticker 同时接受 $ 前缀和裸大写（parser 的 Pattern A 本身就是裸 ticker 语法，
# 只认 $ 会把 "TSLA 250c 7/11 @ 1.20 好像没接住" 这类真漏检静默掉）。
# 裸大写词（BANG/OK 等）会带来一点过报，但这只是 TG 告警闸门，宁多勿漏。
_OPEN_TICKER_RE = _re.compile(r"\$[A-Z]{1,5}\b|\b[A-Z]{2,5}\b")
_OPEN_SIDE_RE = _re.compile(r"\b\d+(?:\.\d+)?[cp]\b|\bcalls?\b|\bputs?\b", _re.I)
# 价格写法：$X.XX / @X.XX / .98 fill / .98 filled
_OPEN_PRICE_RE = _re.compile(
    r"\$\.?\d+(?:\.\d+)?"
    r"|@\s*\$?\.?\d+(?:\.\d+)?"
    r"|\.?\d+(?:\.\d+)?\s*fill(?:ed)?",
    _re.I,
)


def _looks_like_open_attempt(text: str) -> bool:
    """三件套都有 → 大概率是想发开仓信号但 parser 没接住。值得 TG。

    否则一律视为 KC 状态评论 / recap / 行情解说，silence 即可。
    """
    if not text:
        return False
    return bool(
        _OPEN_TICKER_RE.search(text)
        and _OPEN_SIDE_RE.search(text)
        and _OPEN_PRICE_RE.search(text)
    )


# ============================================================
# 启发：疑似加仓（add-on）信号检测
# ============================================================
# 背景 7/6：KC "small add SPY @ 1.86" ×4（EN×2 + ZH×2）全部 parse-fail 静默丢弃。
# 无 strike/side 的 add-on 没法自动执行（需要"关联已有仓位"上下文，错配风险
# 同 follow-up close，见 close_parser 顶部注释），但至少要提醒人工——
# 裸 ticker 不满足 _looks_like_open_attempt 的 $TICKER 要求，之前连 TG 都没有。
_ADDON_KEYWORD_RE = _re.compile(r"\badd(?:ing|ed)?\b", _re.I)
_ZH_ADDON_KEYWORDS = ("加仓", "补仓")
# KC 的 add-on 惯例带 @price；没喊价的 add 评论不值得吵醒人
_ADDON_PRICE_RE = _re.compile(r"@\s*\$?\s*\.?\d+(?:\.\d+)?")

# 双语双发 dedup：同 symbol 5 分钟内只提醒一次（7/6 场景是 4 连发）
_ADDON_ALERT_WINDOW = timedelta(minutes=5)
_addon_alerted: dict[str, datetime] = {}


def _looks_like_addon_attempt(text: str) -> "str | None":
    """检测"疑似加仓已持仓标的"的简写信号。返回命中的 symbol，未命中返回 None。

    三件套（缺一不可，控制误报）：
      1. add 关键词（EN add/adding/added；ZH 加仓/补仓）
      2. @price 写法
      3. 文本中出现**我们已持仓**的 symbol（裸写或 $ 前缀都认）——
         白名单消歧是关键：add-on 的语义就是加已有仓位，
         白名单外的 ticker + add 多半是新开仓评论/闲聊
    """
    if not text:
        return None
    has_kw = bool(_ADDON_KEYWORD_RE.search(text)) or any(
        k in text for k in _ZH_ADDON_KEYWORDS
    )
    if not has_kw or not _ADDON_PRICE_RE.search(text):
        return None
    try:
        open_symbols = position_mgr.get_open_symbols()
    except Exception as e:
        logger.error(f"addon-check get_open_symbols failed: {e}")
        return None
    for sym in open_symbols:
        # 汉字-字母边界 \b 不触发（"小加仓SPY"），用显式 alnum lookaround
        # （同 close_parser.BARE_SYM_PATTERN_ZH 的做法）
        if _re.search(rf"(?<![A-Za-z0-9]){_re.escape(sym)}(?![A-Za-z0-9])", text):
            return sym
    return None


# 常见的中文公司名 → 大致映射到 ticker 的兜底（仅用作"这段文本里含 ticker 提及"判断，
# 不参与下单）。命中即认为 close skipped TG 有价值。
_ZH_TICKER_HINTS = ("亚马逊", "微软", "特斯拉", "苹果", "英伟达", "谷歌", "脸书", "网飞")


def _looks_like_close_attempt(text: str) -> bool:
    """close_parser 返回 None 但文本里有 ticker + 价格-like → 值得 TG（可能漏接）

    "can trim some runners here at 3.45" 这种没 ticker 的 follow-up → silence
    """
    if not text:
        return False
    has_ticker = bool(_OPEN_TICKER_RE.search(text)) or any(t in text for t in _ZH_TICKER_HINTS)
    # 价格-like：$X / @X / 任何 d.dd（不用 \b 边界，因为中文+数字无 word boundary）
    has_price_hint = bool(_re.search(r"\$\.?\d|@\s*\.?\d|\d+\.\d{1,2}", text))
    return has_ticker and has_price_hint


# ============================================================
# 工具：Telegram 通知
# ============================================================
async def _safe_notify(msg: str):
    """发 Telegram，失败只 log 不抛。

    return 值打 log 是为了让运营在 log 里能确认 TG 链路是否工作
    （send_telegram 成功只在 debug 级；6/23 OSCR 拒单 TG 是否发出去看不出）。
    """
    head = msg.replace("\n", " ")[:60]
    try:
        ok = await send_telegram(msg)
        if ok:
            logger.info(f"[notify] TG sent: {head}")
        else:
            logger.warning(f"[notify] TG send returned False: {head}")
    except Exception as e:
        logger.error(f"[notify] TG raised: {type(e).__name__}: {e} (msg head: {head})")


# ============================================================
# 启动入口
# ============================================================
async def start_listener():
    """供 main.py 调用的启动函数。

    并行启动：
      - Discord client（主链路）
      - SL watcher（止损轮询）
      - EOD watcher（收盘强平）
    三个 task 共享一个 event loop，watcher 用 to_thread 包同步 SDK 调用。
    """
    if not TOKEN:
        raise RuntimeError("DISCORD_USER_TOKEN not configured in config/.env")

    # fire-and-forget 后台 task；它们自己用 try/except 兜底，不会让主循环退出
    asyncio.create_task(run_sl_watcher(), name="sl_watcher")
    asyncio.create_task(run_eod_watcher(), name="eod_watcher")
    asyncio.create_task(run_tp_watcher(), name="tp_watcher")

    await client.start(TOKEN)