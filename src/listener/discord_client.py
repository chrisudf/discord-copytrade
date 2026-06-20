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
from src.broker.moomoo_client import place_order, place_sell_order, breakeven_exit_price
from src.config.channel_loader import registry
from src.risk.risk_manager import check_order, record_order
from src.notifier.telegram_client import (
    send_telegram_sync,
    format_signal_alert,
    format_order_filled,
    format_risk_blocked,
    format_error,
    format_close_filled,
    format_close_skipped,
)
from src.storage.logger_db import log_raw_signal, log_order
from src.position import manager as position_mgr
from src.position.sl_watcher import run_sl_watcher
from src.position.eod_watcher import run_eod_watcher
from src.position.tp_watcher import run_tp_watcher
from src.utils.logger import logger

TOKEN = os.getenv("DISCORD_USER_TOKEN")
ET_TZ = ZoneInfo("America/New_York")

client = discord.Client()

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


def _is_duplicate_close(parsed: dict) -> tuple[bool, float]:
    """返回 (是否重复, 距上次秒数)。结构同 _is_duplicate_signal。"""
    fp = _close_fingerprint(parsed)
    now = datetime.now(timezone.utc)

    expired = [k for k, ts in _close_fps.items() if now - ts > CLOSE_FP_WINDOW]
    for k in expired:
        _close_fps.pop(k, None)

    if len(_close_fps) >= _CLOSE_FP_MAX:
        oldest = min(_close_fps, key=_close_fps.get)
        _close_fps.pop(oldest, None)

    prev_ts = _close_fps.get(fp)
    if prev_ts is not None:
        return True, (now - prev_ts).total_seconds()

    _close_fps[fp] = now
    return False, 0.0


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
@client.event
async def on_ready():
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
        # 真·没匹配任何模式（rare），值得报警关注
        logger.warning("Parse failed")
        await _safe_notify(format_error("Parse failed", raw))
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

    # 解析成功立即预警，带 breakeven 提示
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
    ))

    # ---- 风控 ----
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
        await _safe_notify(format_close_skipped("parser skipped (recap/no-symbol)", raw))
        return

    # CLOSE dedup —— 双语双发 / 同信号重发拦截
    is_dup, ago = _is_duplicate_close(parsed)
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

    for symbol in targets:
        # 同 symbol 可能多 strike，全部按 pct 卖
        # TODO：v1 全部对待；后续可能要按 strike/expiry 匹配 close 信号里的细节
        positions = position_mgr.find_by_symbol(symbol)
        if not positions:
            logger.warning(f"[CLOSE] {symbol} not found in open positions")
            continue

        for pos in positions:
            qty_to_sell = position_mgr.calc_qty_to_sell(pos, pct)
            if qty_to_sell <= 0:
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
                continue

            if not result.get("success"):
                err = result.get("message", "unknown")
                logger.error(f"[CLOSE] sell rejected: {err}")
                await _safe_notify(format_error(
                    "Sell rejected by broker",
                    f"{pos['option_code']} qty={qty_to_sell}\n{err}",
                ))
                any_executed = True  # 持仓找到了只是 broker 拒单，不再报 "no matching"
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

            await _safe_notify(format_close_filled(
                pos["symbol"], pos["strike"], pos["side"], pos["expiry"],
                result.get("qty", qty_to_sell), result.get("price", limit),
                pct, "kc_signal", result.get("order_id", "N/A"),
            ))
            any_executed = True

    if not any_executed:
        await _safe_notify(format_close_skipped(
            "no matching open positions",
            f"parsed: kind={parsed['kind']} symbols={targets} pct={pct}\n\n{raw}",
        ))


# ============================================================
# 工具：Telegram 通知
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