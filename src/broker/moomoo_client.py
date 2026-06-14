"""moomoo OpenAPI order wrapper"""
import os
from datetime import date
from src.utils.logger import logger

# TODO: uncomment when deploying
# from moomoo import (
#     OpenSecTradeContext, TrdMarket, SecurityFirm,
#     TrdSide, OrderType, TrdEnv
# )

DEFAULT_QTY = int(os.getenv("DEFAULT_QTY", 1))
MAX_SLIPPAGE_PCT = float(os.getenv("MAX_SLIPPAGE_PCT", 5)) / 100
TRD_ENV = os.getenv("MOOMOO_TRD_ENV", "SIMULATE")

# Price guardrails by trade style
MAX_PRICE_LOTTO = float(os.getenv("MAX_PRICE_LOTTO", 1.5))      # 0DTE / lotto
MAX_PRICE_SWING = float(os.getenv("MAX_PRICE_SWING", 10.0))     # swing
MAX_PRICE_DEFAULT = float(os.getenv("MAX_PRICE_DEFAULT", 5.0))  # no tag


def build_option_code(symbol: str, exp_date: date, strike: float, side: str) -> str:
    """moomoo option code: US.IREN240119C00060000"""
    date_str = exp_date.strftime("%y%m%d")
    cp = "C" if side == "CALL" else "P"
    strike_str = f"{int(strike * 1000):08d}"
    return f"US.{symbol}{date_str}{cp}{strike_str}"


def get_price_limit(tags: list) -> float:
    """Decide max price based on tags."""
    tags = tags or []
    if "lotto" in tags:
        return MAX_PRICE_LOTTO
    if "swing" in tags:
        return MAX_PRICE_SWING
    return MAX_PRICE_DEFAULT


async def place_order(signal: dict) -> dict:
    """Place order. Returns {success, message, order_id, ...}"""

    # Differentiated price guardrail
    price_limit = get_price_limit(signal.get("tags", []))
    if signal["price"] > price_limit:
        msg = (
            f"Price {signal['price']:.2f} exceeds limit {price_limit:.2f} "
            f"(tags={signal.get('tags')})"
        )
        logger.warning(msg)
        return {"success": False, "message": msg}

    # TODO P1: position sizing based on account balance + tag
    qty = DEFAULT_QTY

    option_code = build_option_code(
        signal["symbol"], signal["expiry_date"],
        signal["strike"], signal["side"]
    )
    limit_price = round(signal["price"] * (1 + MAX_SLIPPAGE_PCT), 2)

    logger.info(
        f"Order: {option_code} x {qty} @ {limit_price:.2f} [{TRD_ENV}] "
        f"tags={signal.get('tags')}"
    )

    # === Real order placement (uncomment to enable) ===
    # try:
    #     ctx = OpenSecTradeContext(
    #         filter_trdmarket=TrdMarket.US,
    #         host=os.getenv("MOOMOO_HOST", "127.0.0.1"),
    #         port=int(os.getenv("MOOMOO_PORT", 11111)),
    #         security_firm=SecurityFirm.FUTUINC,
    #     )
    #     env = TrdEnv.REAL if TRD_ENV == "REAL" else TrdEnv.SIMULATE
    #     ret, data = ctx.place_order(
    #         price=limit_price, qty=qty, code=option_code,
    #         trd_side=TrdSide.BUY, order_type=OrderType.NORMAL,
    #         trd_env=env, remark="discord_auto",
    #     )
    #     ctx.close()
    #     if ret == 0:
    #         return {
    #             "success": True, "message": "submitted",
    #             "order_id": str(data["order_id"][0]),
    #             "code": option_code, "qty": qty, "price": limit_price,
    #         }
    #     return {"success": False, "message": str(data)}
    # except Exception as e:
    #     logger.exception("Order exception")
    #     return {"success": False, "message": str(e)}

    # Mock response
    return {
        "success": True,
        "message": "MOCK mode - not actually placed",
        "order_id": "MOCK_001",
        "code": option_code,
        "qty": qty,
        "price": limit_price,
    }