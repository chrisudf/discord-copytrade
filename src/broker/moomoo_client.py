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
from datetime import date
from src.utils.logger import logger

# ---- 配置（从 .env 读取） ----
DEFAULT_QTY = int(os.getenv("DEFAULT_QTY", 1))
TRD_ENV_STR = os.getenv("MOOMOO_TRD_ENV", "SIMULATE")
OPEND_HOST = os.getenv("MOOMOO_HOST", "127.0.0.1")
OPEND_PORT = int(os.getenv("MOOMOO_PORT", 11111))
TRADE_PWD = os.getenv("MOOMOO_TRADE_PWD", "")
ACC_ID = int(os.getenv("MOOMOO_ACC_ID", 0))

# ---- SDK 导入 ----
try:
    from moomoo import (
        OpenSecTradeContext, TrdMarket, SecurityFirm,
        TrdSide, OrderType, TrdEnv, RET_OK,
    )
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False
    logger.warning("[broker] moomoo SDK 未安装，仅 DRY_RUN 可用")

# ---- 模块级单例 ----
_ctx = None
_unlocked = False


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


def _calc_limit_price(price: float) -> float:
    """
    计算挂单限价 = entry_price * (1 + slippage_pct)，2 位小数。

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
    """首次调用解锁交易。模拟盘可能不需要密码。"""
    global _unlocked
    if _unlocked:
        return
    ctx = _get_ctx()
    if TRD_ENV_STR == "SIMULATE" and not TRADE_PWD:
        logger.info("[broker] 模拟盘且无密码，跳过 unlock_trade")
        _unlocked = True
        return
    ret, data = ctx.unlock_trade(password=TRADE_PWD, is_unlock=True)
    if ret != RET_OK:
        raise RuntimeError(f"unlock_trade failed: {data}")
    _unlocked = True
    logger.info("[broker] unlock_trade OK")


def build_option_code(symbol: str, exp_date: date, strike: float, side: str) -> str:
    """
    构造 moomoo 期权代码

    格式：US.{SYMBOL}{YYMMDD}{C/P}{STRIKE*1000:06d}
    例子：IREN 60C exp 2026-06-15 → US.IREN260615C060000

    注意：
    - strike * 1000 是因为 moomoo 用千分之一美元为单位
    - 6 位前导零填充（不是 8 位！）
      strike=2.5  → 002500
      strike=60   → 060000
      strike=400  → 400000
    """
    date_str = exp_date.strftime("%y%m%d")
    cp = "C" if side == "CALL" else "P"
    strike_str = f"{int(strike * 1000):06d}"
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
    limit_price = _calc_limit_price(entry_price)
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

    # ---- 真实下单 ----
    try:
        ctx = _get_ctx()
        acc_id = _ensure_account()
        _ensure_unlocked()

        ret, data = ctx.place_order(
            price=limit_price,
            qty=qty,
            code=option_code,
            trd_side=TrdSide.BUY,
            order_type=OrderType.NORMAL,  # 限价单
            trd_env=_get_trd_env(),
            acc_id=acc_id,
            remark="discord_auto",
        )
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

    try:
        ctx = _get_ctx()
        acc_id = _ensure_account()
        _ensure_unlocked()

        ret, data = ctx.place_order(
            price=limit_price,
            qty=qty,
            code=option_code,
            trd_side=TrdSide.SELL,
            order_type=OrderType.NORMAL,  # 限价
            trd_env=_get_trd_env(),
            acc_id=acc_id,
            remark=remark,
        )
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


def get_last_price(option_code: str):
    """查期权最新成交价。SL / EOD watcher 用。

    返回:
        float 最新价；None 表示拿不到（watcher 应跳过该仓位）

    DRY_RUN 路径：
        - 默认返回 None（不误触发 SL）
        - 设 MOCK_LAST_PRICE=0.5 强制固定价，方便联调 SL 阈值
        - 设 MOCK_LAST_PRICE_<CODE>=0.5 针对单 option_code 设价

    TODO（实测调整）：
    - 上真盘换 OpenQuoteContext.get_market_snapshot([code])，取 last_price 字段
    - quote_ctx 单独维护（和 trade_ctx 分开），同样懒加载 + reset 策略
    - 批量查询：watcher 一次拿一批 code 比 N 次单查省 N 倍 RTT
    - 行情订阅 vs 快照：snapshot 简单，但延迟高；订阅推送实时但要状态管理
    """
    if _is_dry_run():
        per_code = os.getenv(f"MOCK_LAST_PRICE_{option_code}")
        if per_code:
            return float(per_code)
        fixed = os.getenv("MOCK_LAST_PRICE")
        if fixed:
            return float(fixed)
        return None

    # TODO: 真盘走 quote_ctx.get_market_snapshot
    logger.warning(f"[broker] get_last_price not implemented for real env: {option_code}")
    return None


def close_ctx():
    """优雅退出时调用。"""
    global _ctx, _unlocked
    if _ctx is not None:
        try:
            _ctx.close()
        except Exception:
            pass
        _ctx = None
        _unlocked = False
        logger.info("[broker] ctx closed")