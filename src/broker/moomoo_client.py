"""
moomoo OpenAPI 下单封装

职责：
- 构造 option_code、计算 limit_price、调用 SDK
- 不做风控（已迁移到 risk_manager）
- 同步函数，调用方需 asyncio.to_thread 包装

设计：
- ctx 单例懒加载（首次 place_order 才连 OpenD）
- account_id 从 .env 读，写死 MOOMOO_ACC_ID
- unlock_trade 只解一次（模拟盘其实不需要，留着兼容真实盘）

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
MAX_SLIPPAGE_PCT = float(os.getenv("MAX_SLIPPAGE_PCT", 5)) / 100
TRD_ENV_STR = os.getenv("MOOMOO_TRD_ENV", "SIMULATE")
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
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


def _get_trd_env():
    """字符串配置 → SDK 枚举"""
    return TrdEnv.REAL if TRD_ENV_STR == "REAL" else TrdEnv.SIMULATE


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
    limit_price = round(signal["price"] * (1 + MAX_SLIPPAGE_PCT), 2)

    logger.info(
        f"[broker] Order: {option_code} x {qty} @ {limit_price:.2f} "
        f"[env={TRD_ENV_STR}, dry_run={DRY_RUN}] tags={signal.get('tags')}"
    )

    # ---- DRY_RUN 路径 ----
    if DRY_RUN:
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
    if DRY_RUN:
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
        return {"success": False, "message": str(e),
                "status": None, "filled_qty": 0, "filled_avg_price": 0.0}


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