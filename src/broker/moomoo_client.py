"""
moomoo OpenAPI 下单封装

职责：
- 构造 option_code、计算 limit_price（分档 slippage）、调用 SDK
- 不做风控（已迁移到 risk_manager）
- 同步函数，调用方需 asyncio.to_thread 包装

设计：
- ctx 单例懒加载（首次 place_order 才连 OpenD）
- account_id 从 .env 读，写死 MOOMOO_ACC_ID
- unlock_trade 只解一次（模拟盘其实不需要，留着兼容真实盘）

Slippage 分档（基于 6/17 HOOD 4-bagger 实战教训）：
- price <  $1.5  → 12%   # lotto / 低价单 spread 宽
- price <  $3.0  →  8%
- price >= $3.0  →  5%
TODO: 用 Polygon 回测后用真实 fill 数据校准这三档

返回格式约定（成功 / 失败统一字段）：
{
    "success": bool,
    "message": str,
    "order_id": str | None,
    "code": str,
    "qty": int,
    "price": float,
}
"""
import os
import threading
import time
from datetime import date
from zoneinfo import ZoneInfo
from src.utils.logger import logger

# ---- 配置（从 .env 读取） ----
DEFAULT_QTY = int(os.getenv("DEFAULT_QTY", 1))
# .strip().upper() 防止 .env 写成 "simulate" / " REAL " 之类导致下面所有 == 判断失效
TRD_ENV_STR = os.getenv("MOOMOO_TRD_ENV", "SIMULATE").strip().upper()
OPEND_HOST = os.getenv("MOOMOO_HOST", "127.0.0.1")
OPEND_PORT = int(os.getenv("MOOMOO_PORT", 11111))
# .env 里统一 MOOMOO_TRD_* 前缀（TRD_ENV / TRD_PWD），跟原 MOOMOO_TRADE_PWD 对齐
# 模拟盘 SIMULATE 不需要密码所以历史没发现这个 typo，上真盘前必修
# 兼容老 .env 里的 MOOMOO_TRADE_PWD：旧名字命中时打个 warn 提示迁移
_legacy_pwd = os.getenv("MOOMOO_TRADE_PWD")
TRADE_PWD = os.getenv("MOOMOO_TRD_PWD") or _legacy_pwd or ""
if _legacy_pwd and not os.getenv("MOOMOO_TRD_PWD"):
    logger.warning(
        "[broker] 检测到旧 env 名 MOOMOO_TRADE_PWD，建议改为 MOOMOO_TRD_PWD（见 .env.example）"
    )
ACC_ID = int(os.getenv("MOOMOO_ACC_ID", 0))

# ---- SDK 导入 ----
try:
    from moomoo import (
        OpenSecTradeContext, OpenQuoteContext,
        TrdMarket, SecurityFirm,
        TrdSide, OrderType, TrdEnv, RET_OK,
    )
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False
    logger.warning("[broker] moomoo SDK 未安装，仅 DRY_RUN 可用")

# ---- 模块级单例 ----
_ctx = None
_unlocked = False

# Quote context（行情）独立单例：跟 trd_ctx 解耦避免互相干扰
# 必须用 threading.Lock 而非 asyncio.Lock：SDK 同步调用通过 to_thread 跑，
# 多个 watcher 并发调时是真线程并发，asyncio lock 不管用
_quote_ctx = None
_quote_lock = threading.Lock()

# 真盘行情限频 backoff（snapshot 60 次/30s 命中后整段冷却）
_quote_backoff_until: float = 0.0

# get_last_price 真盘路径日志去重（早期未实现时用，保留作为 fallback warning）
_real_quote_warned_once: bool = False
_real_quote_warned_codes: set[str] = set()

# 行情新鲜度阈值（秒）：snapshot.update_time 比 now 旧超过这个值视为 stale
# 延迟数据账户拿到的报价 ~15min 旧，会全部被这个阈值过滤掉 → 启动 probe 时会暴露
QUOTE_FRESHNESS_SEC = 60.0

# moomoo snapshot 的 update_time 是**无时区的美东时间**字符串。
# 之前 pd.to_datetime(...).timestamp() 把它当 UTC：ET 落后 UTC 4-5 小时，
# 解析出来的 epoch 比真实值早 4-5h → 每条实时报价都被 60s 新鲜度检查
# 判为 stale 丢弃 → 真盘 SL/TP/EOD 永远拿不到价、全部 no-op。
# 若实测发现 OpenD 返回的是其它时区，用 MOOMOO_QUOTE_TZ 覆盖。
QUOTE_TZ = ZoneInfo(os.getenv("MOOMOO_QUOTE_TZ", "America/New_York"))


def _quote_epoch(update_time) -> float:
    """update_time（无 tz 字符串/时间戳）→ POSIX epoch 秒，按 QUOTE_TZ 本地化。"""
    import pandas as pd
    ts = pd.to_datetime(update_time)
    if ts.tzinfo is None:
        ts = ts.tz_localize(QUOTE_TZ)
    return ts.timestamp()


def _reset_ctx():
    """连接异常后重置单例，下次调用会重连。
    OpenD 重启或网络抖动会让旧 ctx 永久变坏，必须显式丢弃。"""
    global _ctx, _unlocked
    if _ctx is not None:
        try:
            _ctx.close()
        except Exception:
            pass
    _ctx = None
    _unlocked = False
    logger.warning("[broker] ctx reset, will reconnect on next call")


def _get_quote_ctx():
    """懒加载 quote_ctx 单例。失败抛异常由调用方 catch。"""
    global _quote_ctx
    if _quote_ctx is None:
        if not SDK_AVAILABLE:
            raise RuntimeError("moomoo SDK not installed")
        logger.info(f"[broker] 连接 quote OpenD {OPEND_HOST}:{OPEND_PORT}")
        _quote_ctx = OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT)
    return _quote_ctx


def _reset_quote_ctx():
    """quote 链路异常时重置；下次调用会重连。
    跟 _reset_ctx 解耦——交易和行情走两条独立 socket，互不影响。
    持 _quote_lock 执行，避免把并发 snapshot 正在用的 ctx 从脚下关掉。"""
    global _quote_ctx
    with _quote_lock:
        if _quote_ctx is not None:
            try:
                _quote_ctx.close()
            except Exception:
                pass
        _quote_ctx = None
    logger.warning("[broker] quote_ctx reset, will reconnect on next call")


def _is_dry_run() -> bool:
    """实时读取 DRY_RUN，避免 import 时锁定导致测试无法覆盖真实分支"""
    return os.getenv("DRY_RUN", "true").lower() == "true"


def _get_trd_env():
    """字符串配置 → SDK 枚举"""
    return TrdEnv.REAL if TRD_ENV_STR == "REAL" else TrdEnv.SIMULATE


def _get_slippage_pct(price: float) -> float:
    """
    分档 slippage：低价合约 spread 更宽，需要更大偏移才追得到 fill。

    分档（v1，待回测校准）：
    - < $1.5  → 12%
    - < $3.0  →  8%
    - >= $3.0 →  5%

    例子：
    - HOOD 1.10 → 1.10 * 1.12 = 1.23   (旧版 5% 只挂 1.16，6/17 漏吃 4-bagger)
    - NOW  3.90 → 3.90 * 1.05 = 4.09   (与旧版一致)
    - IREN 0.68 → 0.68 * 1.12 = 0.76
    """
    if price < 1.5:
        return 0.12
    elif price < 3.0:
        return 0.08
    else:
        return 0.05


def calc_limit_price(price: float) -> float:
    """
    计算挂单限价 = entry_price * (1 + slippage_pct)，2 位小数。

    公开导出：listener 风控前也要调它——Layer 2/3/4 的成本必须按
    实际挂单价算，否则 REAL $1000 硬顶会被 slippage 突破最多 12%。

    TODO: 加入 Penny Pilot tick 档位对齐
    - Penny Pilot (IREN/SPY/QQQ/HOOD 等)：tick = $0.01，当前 round(2) 已对齐
    - 非 Penny：< $3 → $0.05 / >= $3 → $0.10，当前会挂出非法价位
    暂不实现，等收集到非 Penny 拒单数据再做。
    """
    pct = _get_slippage_pct(price)
    return round(price * (1 + pct), 2)


def _get_ctx():
    """懒加载 ctx 单例。失败抛异常由调用方 catch。"""
    global _ctx
    if _ctx is None:
        if not SDK_AVAILABLE:
            raise RuntimeError("moomoo SDK not installed")
        logger.info(f"[broker] 连接 OpenD {OPEND_HOST}:{OPEND_PORT}")
        _ctx = OpenSecTradeContext(
            filter_trdmarket=TrdMarket.US,
            host=OPEND_HOST,
            port=OPEND_PORT,
            security_firm=SecurityFirm.FUTUINC,
        )
    return _ctx


def _ensure_account():
    """直接返回 .env 配置的账号 ID。"""
    if ACC_ID == 0:
        raise RuntimeError("MOOMOO_ACC_ID 未配置")
    return ACC_ID


def _ensure_unlocked():
    """首次调用解锁交易。

    moomoo SIMULATE 模式 **不支持** unlock_trade —— 调它必返回
    "ERROR. No one available account!"（即使 acc_status=ACTIVE）。
    因此 SIMULATE 永远跳过，无论 TRADE_PWD 是否配置。
    """
    global _unlocked
    if _unlocked:
        return
    if TRD_ENV_STR == "SIMULATE":
        logger.info("[broker] SIMULATE 模式，跳过 unlock_trade（moomoo 不支持）")
        _unlocked = True
        return
    ctx = _get_ctx()
    if not TRADE_PWD:
        raise RuntimeError("REAL 模式但 MOOMOO_TRD_PWD 未配置，无法 unlock_trade")
    ret, data = ctx.unlock_trade(password=TRADE_PWD, is_unlock=True)
    if ret != RET_OK:
        raise RuntimeError(f"unlock_trade failed: {data}")
    _unlocked = True
    logger.info("[broker] unlock_trade OK (REAL)")


# moomoo 会话/账户失效返回的关键字（小写匹配）。命中时 reset ctx + 重试一次。
# 之所以放白名单：避免把"价格无效""超额"等业务拒单也当成 stale 反复重试。
_STALE_SESSION_HINTS = (
    "no one available account",   # SIMULATE 会话过期 / 账户挂起
    "account is not unlock",      # 真盘 unlock 状态丢失
    "session",                    # 通用 session 失效
    "not login",                  # OpenD 失连
)


def _is_stale_session(msg: str) -> bool:
    s = (msg or "").lower()
    return any(h in s for h in _STALE_SESSION_HINTS)


def breakeven_exit_price(entry_price: float, sell_slip: float = 0.05) -> tuple[float, float]:
    """计算"我们跟单不亏"所需的最低 KC 卖出价（毛 PnL %）。

    我们买入 ≈ entry × (1 + buy_slip)
    我们卖出 ≈ KC_exit × (1 - sell_slip)
    净 PnL = 0 → KC_exit = entry × (1 + buy_slip) / (1 - sell_slip)

    返回 (breakeven_price, breakeven_gross_pct)
    例: entry=2.70（$1.5-$3 档，buy_slip=8%）→ (3.07, +13.7%)

    实战价值：开单时 TG 提示 KC 至少要 +N% 退出我们才不亏，
    用户对照 KC 历史 trim 阈值（通常 +50%/+100%）能直观判断信号好坏。
    """
    if entry_price is None or entry_price <= 0:
        return 0.0, 0.0
    buy = _get_slippage_pct(entry_price)
    be_price = entry_price * (1 + buy) / (1 - sell_slip)
    be_pct = (be_price / entry_price - 1) * 100
    return round(be_price, 2), round(be_pct, 1)


def build_option_code(symbol: str, exp_date: date, strike: float, side: str) -> str:
    """
    构造 moomoo 期权代码

    格式：US.{SYMBOL}{YYMMDD}{C/P}{STRIKE*1000}
    例子：IREN 60C exp 2026-06-15 → US.IREN260615C60000

    注意：
    - strike * 1000 是因为 moomoo 用千分之一美元为单位
    - strike 段是**裸整数，不做前导零填充**：
      strike=2.5  → 2500
      strike=60   → 60000
      strike=400  → 400000
      7/13 复盘实锤：带前导零的 code 被 moomoo 100% 拒（"Cannot find ... in
      US Stocks"）——OSCR 30c(6/23)、TEM 65c(6/29)、RGTI 15p、NFLX 80c(7/13)
      四笔全灭；strike ≥ $100（天然 ≥6 位）的全部成功。此前 :06d 填充只是
      恰好没被大票踩到。
    - round 而非 int 截断：浮点误差下 int() 可能把 x999.9999 截成 x999
    """
    date_str = exp_date.strftime("%y%m%d")
    cp = "C" if side == "CALL" else "P"
    strike_str = str(round(strike * 1000))
    return f"US.{symbol}{date_str}{cp}{strike_str}"


def place_order(signal: dict, qty: int = None) -> dict:
    """
    下单（同步函数，调用方需用 asyncio.to_thread 包装）

    参数:
        signal: dict, 必含 symbol, expiry_date, strike, side, price
        qty: 不传则用 DEFAULT_QTY

    返回: 见模块顶部约定
    """
    qty = qty if qty is not None else DEFAULT_QTY

    option_code = build_option_code(
        signal["symbol"], signal["expiry_date"],
        signal["strike"], signal["side"],
    )
    entry_price = signal["price"]
    limit_price = calc_limit_price(entry_price)
    slip_pct = _get_slippage_pct(entry_price)

    dry_run = _is_dry_run()
    logger.info(
        f"[broker] Order: {option_code} x {qty} @ {limit_price:.2f} "
        f"(entry={entry_price:.2f}, slip={slip_pct*100:.0f}%) "
        f"[env={TRD_ENV_STR}, dry_run={dry_run}] tags={signal.get('tags')}"
    )

    # ---- DRY_RUN 路径 ----
    if dry_run:
        return {
            "success": True, "message": "DRY_RUN",
            "order_id": "MOCK_001", "code": option_code,
            "qty": qty, "price": limit_price,
        }

    # ---- contract 预校验：避免 broker "Cannot find" 拒单（6/22 OSCR / 6/29 TEM/DRAM）
    # 用 quote snapshot 试取一次，找不到直接返回 rejected 不走 broker。
    # 节省一次 broker RTT，且错误消息更明确（运营能知道是 strike/expiry 不存在）。
    valid = validate_option_codes([option_code])
    if not valid.get(option_code):
        msg = f"option contract not found on OPRA: {option_code} (check strike/expiry exists)"
        logger.error(f"[broker] pre-validate rejected: {msg}")
        return {
            "success": False, "message": msg,
            "order_id": None, "code": option_code,
            "qty": qty, "price": limit_price,
        }

    # ---- 真实下单 ----
    try:
        ctx = _get_ctx()
        acc_id = _ensure_account()
        _ensure_unlocked()

        order_args = dict(
            price=limit_price,
            qty=qty,
            code=option_code,
            trd_side=TrdSide.BUY,
            order_type=OrderType.NORMAL,  # 限价单
            trd_env=_get_trd_env(),
            acc_id=acc_id,
            remark="discord_auto",
        )
        ret, data = ctx.place_order(**order_args)
        # 中途 session 失效 → 重连 + 重试一次。避免昨晚 SNOW 那种"运行半夜忽然账户挂"丢信号
        if ret != RET_OK and _is_stale_session(str(data)):
            logger.warning(f"[broker] stale session ({data}), reset 后重试一次")
            _reset_ctx()
            ctx = _get_ctx()
            _ensure_unlocked()
            ret, data = ctx.place_order(**order_args)
        if ret == RET_OK:
            order_id = str(data["order_id"].iloc[0])
            logger.info(f"[broker] 下单成功 order_id={order_id}")
            return {
                "success": True, "message": "submitted",
                "order_id": order_id, "code": option_code,
                "qty": qty, "price": limit_price,
            }
        else:
            logger.error(f"[broker] 下单失败: {data}")
            return {
                "success": False, "message": str(data),
                "order_id": None, "code": option_code,
                "qty": qty, "price": limit_price,
            }
    except Exception as e:
        logger.exception("[broker] place_order 异常")
        _reset_ctx()
        return {
            "success": False, "message": str(e),
            "order_id": None, "code": option_code,
            "qty": qty, "price": limit_price,
        }


def _get_long_qty(option_code: str) -> int:
    """查 broker 里持有的 long qty。用于 naked-short 防护。

    Returns:
        long qty（>= 0）。broker 确认无此 code 或方向为 SHORT → 返 0。

    Raises:
        RuntimeError: position_list_query 失败（含 stale-session 重试一次后仍失败）。
        之前把查询失败静默当 qty=0 处理，会用误导性的 naked-short 拒单
        挡掉所有卖出（含 SL 止损单），且绕过了 stale-session 恢复逻辑。
    """
    ctx = _get_ctx()
    ret, df = ctx.position_list_query(
        code=option_code,
        trd_env=_get_trd_env(),
        acc_id=_ensure_account(),
    )
    if ret != RET_OK and _is_stale_session(str(df)):
        logger.warning(f"[broker] naked-check stale session ({df}), reset 后重试一次")
        _reset_ctx()
        ctx = _get_ctx()
        _ensure_unlocked()
        ret, df = ctx.position_list_query(
            code=option_code,
            trd_env=_get_trd_env(),
            acc_id=_ensure_account(),
        )
    if ret != RET_OK:
        raise RuntimeError(f"position_list_query failed: {df}")
    if df is None or len(df) == 0:
        return 0
    # position_side: LONG / SHORT
    row = df.iloc[0]
    side = str(row.get("position_side", "")).upper()
    qty = int(row.get("qty", 0))
    if side != "LONG" or qty <= 0:
        return 0
    return qty


def place_sell_order(
    option_code: str,
    qty: int,
    limit_price: float,
    remark: str = "auto_close",
) -> dict:
    """卖单（限价，同步，调用方需 to_thread 包装）

    Args:
        option_code: 标的代码（同 build_option_code 输出）
        qty: 卖出张数
        limit_price: 限价。SL/EOD 场景建议传 bid * 0.95 偏激进确保成交；
                     CLOSE 信号正常 trim 可传 bid 附近。
        remark: 标记触发源（kc_close / sl_polling / tp_polling / eod_force）

    返回:
        {success, message, order_id, code, qty, price}

    TODO（测试调整）：
    - 实测后看是否要支持 OrderType.MARKET（SL 紧急情况）
    - 模拟盘 SIMULATE 卖单是否需要先有真实持仓，没有的话 SDK 会拒
    - 部分成交（dealt_qty < qty）的处理 —— 当前只看 RET_OK，不轮询 fill
    """
    if qty <= 0:
        return {
            "success": False, "message": f"invalid qty={qty}",
            "order_id": None, "code": option_code,
            "qty": qty, "price": limit_price,
        }

    dry_run = _is_dry_run()
    logger.info(
        f"[broker] SELL: {option_code} x {qty} @ {limit_price:.2f} "
        f"[env={TRD_ENV_STR}, dry_run={dry_run}, remark={remark}]"
    )

    if dry_run:
        return {
            "success": True, "message": "DRY_RUN sell",
            "order_id": f"MOCK_SELL_{option_code[-6:]}",
            "code": option_code, "qty": qty, "price": limit_price,
        }

    # ---- Naked-short 防护 ----
    # 系统只允许卖出已持有的 long option。不能挂 SELL 让 broker 视作开裸空仓。
    # 背景：7/2 发现本地 DB 与 broker 严重脱钩（自动 exercise 后本地仍 OPEN），
    # 如果后续 close 信号误触发 SELL，broker 会当"开裸空 call/put"处理 —— 无限风险。
    # 见 docs/lessons.md #15。
    try:
        available = _get_long_qty(option_code)
    except Exception as e:
        logger.exception(f"[broker] naked-check position_list_query failed for {option_code}")
        return {
            "success": False,
            "message": f"naked-short check failed (position_list_query exception): {e}",
            "order_id": None, "code": option_code, "qty": qty, "price": limit_price,
        }
    if available < qty:
        msg = (
            f"naked-short refused: broker has only {available} long of {option_code}, "
            f"asked to sell {qty}. This would open a naked short — refusing."
        )
        logger.error(f"[broker] {msg}")
        # naked_short=True 是给守护用的"脱钩"信号（区别于上面 position_list_query
        # 异常那种瞬时失败）：查询成功、broker 权威地说"没这么多 long"，说明本地 DB
        # 高估了持仓。守护据此把本地核销到 broker 实数并停止重试，而不是每 tick 重挂。
        # broker_qty 带回 broker 实际持有的 long 张数（0 = 完全不持有）。
        return {
            "success": False, "message": msg,
            "order_id": None, "code": option_code, "qty": qty, "price": limit_price,
            "naked_short": True, "broker_qty": available,
        }

    try:
        ctx = _get_ctx()
        acc_id = _ensure_account()
        _ensure_unlocked()

        order_args = dict(
            price=limit_price,
            qty=qty,
            code=option_code,
            trd_side=TrdSide.SELL,
            order_type=OrderType.NORMAL,  # 限价
            trd_env=_get_trd_env(),
            acc_id=acc_id,
            remark=remark,
        )
        ret, data = ctx.place_order(**order_args)
        if ret != RET_OK and _is_stale_session(str(data)):
            logger.warning(f"[broker] sell stale session ({data}), reset 后重试一次")
            _reset_ctx()
            ctx = _get_ctx()
            _ensure_unlocked()
            ret, data = ctx.place_order(**order_args)
        if ret == RET_OK:
            order_id = str(data["order_id"].iloc[0])
            logger.info(f"[broker] 卖单成功 order_id={order_id}")
            return {
                "success": True, "message": "submitted",
                "order_id": order_id, "code": option_code,
                "qty": qty, "price": limit_price,
            }
        logger.error(f"[broker] 卖单失败: {data}")
        return {
            "success": False, "message": str(data),
            "order_id": None, "code": option_code,
            "qty": qty, "price": limit_price,
        }
    except Exception as e:
        logger.exception("[broker] place_sell_order 异常")
        _reset_ctx()
        return {
            "success": False, "message": str(e),
            "order_id": None, "code": option_code,
            "qty": qty, "price": limit_price,
        }


def query_order_status(order_id: str) -> dict:
    """
    查单状态（同步）

    返回:
        {success, status, filled_qty, filled_avg_price, message}
    """
    if _is_dry_run():
        return {
            "success": True, "status": "FILLED_ALL",
            "filled_qty": 1, "filled_avg_price": 0.0,
            "message": "DRY_RUN",
        }
    try:
        ctx = _get_ctx()
        acc_id = _ensure_account()
        ret, data = ctx.order_list_query(
            order_id=order_id,
            trd_env=_get_trd_env(),
            acc_id=acc_id,
        )
        if ret != RET_OK:
            return {"success": False, "message": str(data),
                    "status": None, "filled_qty": 0, "filled_avg_price": 0.0}
        if len(data) == 0:
            return {"success": False, "message": "order not found",
                    "status": None, "filled_qty": 0, "filled_avg_price": 0.0}
        row = data.iloc[0]
        dealt_price = row.get("dealt_avg_price", 0)
        return {
            "success": True,
            "status": str(row["order_status"]),
            "filled_qty": int(row["dealt_qty"]),
            "filled_avg_price": float(dealt_price) if dealt_price else 0.0,
            "message": "ok",
        }
    except Exception as e:
        logger.exception("[broker] query_order_status 异常")
        _reset_ctx()
        return {"success": False, "message": str(e),
                "status": None, "filled_qty": 0, "filled_avg_price": 0.0}


# no-permission 退避日志节流：每个 300s 退避周期到期后 SL/TP 各重探一次，
# 每次都打 WARNING 一夜能刷 ~200 行（7/9 实测）。状态没变化时只在
# 首次 + 每小时提醒一次，其余降为 DEBUG。
# 哨兵必须是 None 而非 0.0：monotonic 是**开机以来**的秒数，uptime < 1h 的
# 机器（重启后的生产机、CI runner）上 `now - 0.0 < 3600` 会把首条 WARNING
# 吞掉——7/14 CI 实锤（本地 uptime 数天所以测不出来）。
_no_perm_last_warn: "float | None" = None
_NO_PERM_WARN_INTERVAL = 3600.0


def _snapshot(codes: list) -> "tuple[int, object]":
    """单层 wrapper：处理 lock、限频 backoff、异常 reset。返回 (ret, df_or_msg)。

    设计见 docs/realtime_quote_design.md (PR 1)。
    """
    global _quote_backoff_until, _no_perm_last_warn
    now = time.monotonic()
    if now < _quote_backoff_until:
        return -1, f"backoff (limit/quota) for another {_quote_backoff_until - now:.0f}s"

    try:
        with _quote_lock:
            ctx = _get_quote_ctx()
            ret, df = ctx.get_market_snapshot(codes)
    except Exception as e:
        logger.exception("[broker] snapshot exception")
        _reset_quote_ctx()
        return -1, f"exception: {type(e).__name__}"

    if ret != RET_OK:
        msg = str(df)
        msg_lower = msg.lower()
        # 限频/配额 → 短退避。7/8 实测 moomoo 的限频报错原文是
        # "request failed due to high frequency. Maximum 60 times per 30 seconds."
        # ——不含 quota/limit 字样，旧关键词接不住 → watcher 不退避持续硬打。
        if any(k in msg_lower for k in ("quota", "limit", "high frequency", "frequent")):
            _quote_backoff_until = time.monotonic() + 60.0
            logger.warning(f"[broker] snapshot quota/limit exceeded, backoff 60s: {msg[:120]}")
        # 无期权行情权限 → 长退避。权限不会在一次 tick 之间凭空出现，
        # 但每次失败的调用**照样消耗 60/30s 频率配额**（7/8 整夜被 watcher
        # 打满，validate_option_codes 全靠 fail-open 才没误拒买单）。
        # 5 分钟重试一次：中途开通订阅也能在几分钟内自动恢复。
        elif "no permission" in msg_lower or "quote permission" in msg_lower:
            _quote_backoff_until = time.monotonic() + 300.0
            note = (
                f"[broker] snapshot no-permission, backoff 300s "
                f"(watcher 轮询暂停，避免打满频率配额): {msg[:120]}"
            )
            if (_no_perm_last_warn is None
                    or time.monotonic() - _no_perm_last_warn >= _NO_PERM_WARN_INTERVAL):
                _no_perm_last_warn = time.monotonic()
                logger.warning(note)
            else:
                logger.debug(note)
    return ret, df


def get_last_prices(codes: list) -> dict:
    """批量取期权最新价。SL/TP/EOD watcher 应每 tick 调用一次（而非 N×get_last_price）。

    Returns:
        {code: float | None}，找不到/stale/无成交 一律 None
    """
    out = {c: None for c in codes}
    if not codes:
        return out

    if _is_dry_run():
        for c in codes:
            per = os.getenv(f"MOCK_LAST_PRICE_{c}")
            if per:
                out[c] = float(per)
                continue
            fixed = os.getenv("MOCK_LAST_PRICE")
            if fixed:
                out[c] = float(fixed)
        return out

    ret, df = _snapshot(codes)
    if ret != RET_OK or df is None:
        return out
    if not hasattr(df, "iterrows") or len(df) == 0:
        return out

    import pandas as pd  # 仅 SDK 路径需要，pandas 是 moomoo 必装依赖
    now_ts = time.time()
    for _, row in df.iterrows():
        code = row.get("code")
        if code not in out:
            continue
        last = row.get("last_price")
        if pd.isna(last) or last is None or last <= 0:
            continue
        # freshness check：避免 OpenD 持旧 cache 或延迟数据账户。
        # update_time 是无 tz 的美东时间，必须按 QUOTE_TZ 本地化再转 epoch
        # （当 UTC 解析会整体偏早 4-5h，所有实时报价都被误判 stale）。
        try:
            ts = _quote_epoch(row["update_time"])
            if now_ts - ts > QUOTE_FRESHNESS_SEC:
                continue
        except Exception:
            pass  # update_time 缺失时仍信任 snapshot（少见）
        out[code] = float(last)
    return out


def get_last_price(option_code: str):
    """查单个期权最新成交价。SL / EOD watcher 用。

    内部走 get_last_prices([code])，统一一条码路径。

    Returns:
        float 最新价；None 表示拿不到（watcher 应跳过该仓位）

    DRY_RUN 路径：
        - 默认返回 None（不误触发 SL）
        - 设 MOCK_LAST_PRICE=0.5 强制固定价
        - 设 MOCK_LAST_PRICE_<CODE>=0.5 针对单 option_code 设价
    """
    return get_last_prices([option_code]).get(option_code)


# 明确表示"合约不存在"的错误关键字。只有命中这些才 fail-closed 拒单；
# 其余失败（quota backoff / 网络抖动 / OpenD 重启中 / 未知错误）一律 fail-open
# 放行给 broker 自己判 —— 预校验是优化不是闸门，它挂了不能把合法信号拒掉。
_DEFINITELY_MISSING_HINTS = (
    "unknown stock", "cannot find", "no such", "invalid stock", "stock not exist",
)


def _is_definitely_missing(msg: str) -> bool:
    s = (msg or "").lower()
    return any(h in s for h in _DEFINITELY_MISSING_HINTS)


def _validate_one(code: str) -> bool:
    """单 code 校验。返回 True=可下单（含权限不足/瞬时失败时的"未知放行"），
    False=**确认**不存在。"""
    ret, df = _snapshot([code])
    if ret != RET_OK:
        # 只有明确 "Unknown stock" 类才判不存在；quota backoff、超时等
        # 瞬时失败一律放行，让 broker 做最终裁决
        return not _is_definitely_missing(str(df))
    if df is None or not hasattr(df, "iterrows") or len(df) == 0:
        return False
    return any(row.get("code") == code for _, row in df.iterrows())


def validate_option_codes(codes: list) -> dict:
    """下单前预校验 option_code 是否存在于 OPRA 链上。

    策略：
    1. 先一次性 batch snapshot（最省 RTT）
    2. batch 失败时若是"no permission"，全部降级返 True（让 broker 自己判）
    3. batch 失败时若是"unknown stock"类（某个 code 让整 batch 挂掉），
       拆成 per-code 重试 —— 隔离坏 code，让好的能正常 validate
    4. batch 成功，按 snapshot 出现与否标 True/False

    Returns:
        {code: True/False}。True = 存在/未知（可让 broker 试）；False = 确认不存在
    """
    out = {c: False for c in codes}
    if not codes:
        return out
    if _is_dry_run():
        return {c: True for c in codes}

    ret, df = _snapshot(codes)

    if ret == RET_OK and df is not None and hasattr(df, "iterrows"):
        # 正常：只把出现在 snapshot 的标 True
        for _, row in df.iterrows():
            code = row.get("code")
            if code in out:
                out[code] = True
        return out

    # batch 失败处理
    msg_lower = str(df).lower()
    if "no permission" in msg_lower or "quote permission" in msg_lower:
        logger.warning(
            "[broker] validate skipped: no US options quote permission "
            f"({str(df)[:120]}). 让 broker 自行判断 contract 存在性。"
            " 要启用预校验，请在 moomoo app 订阅 US MarketOptions Lv1+。"
        )
        return {c: True for c in codes}

    # 瞬时失败（quota backoff / 超时 / OpenD 抖动）→ fail-open 全部放行。
    # 之前 fail-closed：backoff 期间 60s 内所有合法买单都被
    # "contract not found" 误拒 —— 预校验不能变成闸门。
    if not _is_definitely_missing(str(df)):
        logger.warning(
            f"[broker] validate transient failure ({str(df)[:100]}), "
            f"fail-open: 放行 {len(codes)} 个 code 让 broker 判定"
        )
        return {c: True for c in codes}

    # 明确 "Unknown stock" 类（一个坏 code 拖累整 batch）
    # 拆成 per-code 重查，隔离坏 code。代价 = N 次 RTT，但只在 batch 失败时才走
    if len(codes) > 1:
        logger.info(
            f"[broker] batch validate failed ({str(df)[:80]}), "
            f"falling back to per-code check for {len(codes)} codes"
        )
        for c in codes:
            out[c] = _validate_one(c)
        return out

    # 单 code 且明确不存在 → 拒
    logger.warning(f"[broker] validate rejected {codes[0]}: {str(df)[:120]}")
    return out


def probe_broker() -> tuple[bool, str]:
    """启动时探测 broker 健康度。供 run_listener 在 client.start 前调用。

    检查项：
      1. SDK 装好了吗
      2. OpenD 连得上吗
      3. get_acc_list 返回里有没有 MOOMOO_ACC_ID + MOOMOO_TRD_ENV 匹配的活跃账户
      4. REAL 模式：unlock_trade 测一下密码

    DRY_RUN=true 时跳过整套探测，直接返回 OK。

    Returns:
        (ok, message)：ok=False 时 message 是用户可读的诊断原因
    """
    # 启动时把生效配置打到日志，方便人眼对比 .env，避免"以为改了实际没生效"
    logger.info(
        f"[broker] config snapshot: TRD_ENV={TRD_ENV_STR} ACC_ID={ACC_ID} "
        f"TRADE_PWD={'set' if TRADE_PWD else 'empty'} "
        f"OpenD={OPEND_HOST}:{OPEND_PORT} DRY_RUN={_is_dry_run()}"
    )
    if _is_dry_run():
        return True, "DRY_RUN: 跳过 broker 探测"
    if not SDK_AVAILABLE:
        return False, "moomoo SDK 未安装（pip install moomoo-api）"
    if ACC_ID == 0:
        return False, "MOOMOO_ACC_ID 未配置或为 0"

    try:
        ctx = _get_ctx()
    except Exception as e:
        return False, f"连接 OpenD {OPEND_HOST}:{OPEND_PORT} 失败: {e}"

    try:
        ret, df = ctx.get_acc_list()
    except Exception as e:
        _reset_ctx()
        return False, f"get_acc_list 抛异常: {type(e).__name__}: {e}"

    if ret != RET_OK:
        return False, f"get_acc_list 失败: {df}"
    if df is None or len(df) == 0:
        return False, (
            "OpenD 没返回任何账户。检查：\n"
            "  1) moomoo 桌面端是否已登录\n"
            "  2) SIMULATE: 需要在桌面端启用模拟交易并选过一个账户\n"
            "  3) 试着重启 OpenD 或重登桌面端"
        )

    # 匹配账户。
    # moomoo SDK 不同版本对 trd_env 字段返回值不一致：可能是字符串 "SIMULATE"/"REAL"，
    # 也可能是 enum TrdEnv.SIMULATE / 整型。直接 == TRD_ENV_STR 比较会在 enum/int 形态下
    # 误判为不匹配，明明账户可用却阻止启动。这里把候选值都转字符串再比，覆盖所有形态。
    try:
        target = TRD_ENV_STR  # 已经 .upper() 过
        env_str = df["trd_env"].astype(str).str.upper().str.replace("TRDENV.", "", regex=False)
        matched = df[(df["acc_id"] == ACC_ID) & (env_str == target)]
    except Exception as e:
        return False, f"过滤账户列异常: {e}（df.columns={getattr(df, 'columns', '?').tolist() if hasattr(df, 'columns') else '?'}）"

    if len(matched) == 0:
        ids = df["acc_id"].tolist() if "acc_id" in df.columns else []
        envs = df["trd_env"].tolist() if "trd_env" in df.columns else []
        return False, (
            f"MOOMOO_ACC_ID={ACC_ID} env={TRD_ENV_STR} 不在可用账户列表里。\n"
            f"  OpenD 返回的 acc_id: {ids}\n"
            f"  OpenD 返回的 trd_env: {envs}\n"
            f"  → 把 .env 里 MOOMOO_ACC_ID 改成上面其中之一"
        )

    status = matched.iloc[0].get("acc_status", "UNKNOWN")
    if status != "ACTIVE":
        return False, f"账户 {ACC_ID} 状态为 {status}（非 ACTIVE），无法下单"

    # REAL 模式：试 unlock。SIMULATE 由 _ensure_unlocked 直接 skip，这里不碰
    if TRD_ENV_STR == "REAL":
        if not TRADE_PWD:
            return False, "REAL 模式但 MOOMOO_TRD_PWD 未配置"
        try:
            ret, data = ctx.unlock_trade(password=TRADE_PWD, is_unlock=True)
            if ret != RET_OK:
                return False, f"REAL unlock_trade 失败: {data}（密码错？）"
            global _unlocked
            _unlocked = True
        except Exception as e:
            return False, f"unlock_trade 抛异常: {type(e).__name__}: {e}"

    return True, (
        f"OK · env={TRD_ENV_STR} acc_id={ACC_ID} status={status} "
        f"({len(matched)} matching / {len(df)} total)"
    )


# Probe quote access tier，状态码用于 banner 着色和后续判断
QUOTE_OK = "ok"                # 期权 snapshot 真返价 + 新鲜
QUOTE_DELAYED = "delayed"      # snapshot 通了但行情滞后（疑似 delayed-data tier）
QUOTE_NO_PERMISSION = "no_perm"  # 账户没 US MarketOptions 订阅
QUOTE_ERROR = "error"          # 其它失败（OpenD 连不上 / chain 取不到 / etc.）


def probe_quote_access() -> tuple[str, str]:
    """探测期权行情订阅状态，决定 SL/TP/EOD watcher 真盘是否能工作。

    步骤：
      1. quote_ctx 连得上吗
      2. 个股 snapshot（US.SPY）能拿到吗 —— 这个不要 OPRA 权限
      3. 拿一个真实存在的 SPY 期权 code（via get_option_chain）
      4. 对该 code 调 snapshot —— "No permission" 返 NO_PERMISSION
      5. 检查报价新鲜度 —— age > 15min 视为 DELAYED

    不阻塞启动，调用方根据返回决定 banner / warn / 是否启动 watcher。

    Returns:
        (status, message) where status ∈ {OK, DELAYED, NO_PERMISSION, ERROR}
    """
    if _is_dry_run():
        return QUOTE_OK, "DRY_RUN: 跳过期权行情探测"
    if not SDK_AVAILABLE:
        return QUOTE_ERROR, "moomoo SDK 未安装"

    # 1. quote ctx 连得通
    try:
        ctx = _get_quote_ctx()
    except Exception as e:
        return QUOTE_ERROR, f"quote_ctx 连接失败: {type(e).__name__}: {e}"

    # 2. 个股 snapshot —— 基本 quote 连通性 + tier 探测
    try:
        ret, df = ctx.get_market_snapshot(["US.SPY"])
    except Exception as e:
        _reset_quote_ctx()
        return QUOTE_ERROR, f"个股 snapshot 抛异常: {type(e).__name__}: {e}"
    if ret != RET_OK:
        return QUOTE_ERROR, f"个股 snapshot 失败（基本 quote 都不通）: {str(df)[:120]}"

    # 3. 取一个真实期权 code（任何 SPY 近月 ATM 附近都行，列表第一个）
    from datetime import date as _date, timedelta as _timedelta
    today = _date.today()
    # 下周三 → 让 weekly/monthly 大概率都覆盖；不论今天是周几
    target = today + _timedelta(days=(2 - today.weekday()) % 7 + 7)
    try:
        ret, chain = ctx.get_option_chain(
            code="US.SPY", start=target.isoformat(), end=target.isoformat(),
        )
    except Exception as e:
        return QUOTE_ERROR, f"get_option_chain 抛异常: {type(e).__name__}: {e}"
    if ret != RET_OK:
        # get_option_chain 本身也吃 OPRA 权限（实测 6/30）
        msg_lower = str(chain).lower()
        if "no permission" in msg_lower or "quote permission" in msg_lower:
            return QUOTE_NO_PERMISSION, (
                "账户缺 US MarketOptions Lv1+ 行情订阅（get_option_chain 失败）。\n"
                "  影响：SL / TP / EOD watcher 真盘下 no-op；validate_option_codes "
                "降级到不预校验（让 broker 拒）。\n"
                "  开通方法：moomoo app → 我的 → 行情订阅 → US MarketOptions Lv1。"
            )
        return QUOTE_ERROR, f"无法获取 SPY {target} 期权链: {str(chain)[:120]}"
    if chain is None or len(chain) == 0:
        return QUOTE_ERROR, f"SPY {target} 期权链为空（可能无该到期日，换一天试）"
    sample_code = chain.iloc[0]["code"]

    # 4. 对真实期权 code 试 snapshot —— 触发 OPRA 权限检查
    try:
        ret, opt_df = ctx.get_market_snapshot([sample_code])
    except Exception as e:
        return QUOTE_ERROR, f"OPRA snapshot 抛异常: {type(e).__name__}: {e}"
    if ret != RET_OK:
        msg_lower = str(opt_df).lower()
        if "no permission" in msg_lower or "quote permission" in msg_lower:
            return QUOTE_NO_PERMISSION, (
                "账户缺 US MarketOptions Lv1+ 行情订阅。\n"
                "  影响：SL / TP / EOD watcher 真盘下 no-op；validate_option_codes "
                "降级到不预校验（让 broker 拒）。\n"
                "  开通方法：moomoo app → 我的 → 行情订阅 → US MarketOptions Lv1。"
            )
        return QUOTE_ERROR, f"OPRA snapshot 异常: {str(opt_df)[:200]}"

    # 5. 新鲜度（delayed-data tier 通常滞后 15 分钟）
    try:
        update_ts = _quote_epoch(opt_df.iloc[0]["update_time"])
        age = time.time() - update_ts
        if age > 900:  # 15 分钟
            return QUOTE_DELAYED, (
                f"OPRA 行情可拿但滞后 {age:.0f}s（疑似 delayed-data tier）。\n"
                f"  影响：watcher 的 60s 新鲜度过滤会把所有报价当 stale 丢弃 → "
                f"实际 SL/TP/EOD 仍然 no-op。\n"
                f"  开通方法：升级到 US MarketOptions Lv1 实时行情。"
            )
    except Exception:
        pass  # 拿不到 update_time 时不阻断

    return QUOTE_OK, f"OPRA 行情可用 + 实时（sample {sample_code}）"


def close_ctx():
    """优雅退出时调用。同时关交易和行情两路。"""
    global _ctx, _unlocked, _quote_ctx
    if _ctx is not None:
        try:
            _ctx.close()
        except Exception:
            pass
        _ctx = None
        _unlocked = False
        logger.info("[broker] trade ctx closed")
    if _quote_ctx is not None:
        try:
            _quote_ctx.close()
        except Exception:
            pass
        _quote_ctx = None
        logger.info("[broker] quote ctx closed")