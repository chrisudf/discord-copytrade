"""
moomoo OpenAPI 下单封装

职责（精简后）：
- 只负责构造 option_code、计算 limit_price、调用 SDK
- 不做任何价格/数量风控（已全部迁移到 src/risk/risk_manager.py）
- 同步函数：被调用方需用 asyncio.to_thread() 包装到事件循环线程外执行
            （SDK 是同步阻塞的，不能直接在 async 上下文里跑）

返回格式约定（成功 / 失败统一字段）：
{
    "success": bool,
    "message": str,          # 简短描述（成功 = "submitted" / 失败 = 错误原因）
    "order_id": str,         # 成功才有；失败为 None
    "code": str,             # option_code（构造出来的合约代码）
    "qty": int,              # 实际下单张数
    "price": float,          # 实际限价（含滑点）
}
"""
import os
from datetime import date
from src.utils.logger import logger

# TODO P0: 部署到模拟盘时取消下面这块的注释
# from moomoo import (
#     OpenSecTradeContext, TrdMarket, SecurityFirm,
#     TrdSide, OrderType, TrdEnv
# )


# ---- 配置（从 .env 读取） ----
# DEFAULT_QTY: 兜底数量，仅当 signal 没显式给 qty 时用
DEFAULT_QTY = int(os.getenv("DEFAULT_QTY", 1))

# 滑点：限价单实际下单价 = signal_price * (1 + MAX_SLIPPAGE_PCT)
# 例如 signal $2.50 + 5% 滑点 → 限价 $2.625
MAX_SLIPPAGE_PCT = float(os.getenv("MAX_SLIPPAGE_PCT", 5)) / 100

# 模拟盘 / 真实盘切换
TRD_ENV = os.getenv("MOOMOO_TRD_ENV", "SIMULATE")

# DRY_RUN: 不走 SDK，只返回 Mock 结果。用于本地测试 / 风控验证
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"


def build_option_code(symbol: str, exp_date: date, strike: float, side: str) -> str:
    """
    构造 moomoo 期权代码

    格式：US.{SYMBOL}{YYMMDD}{C/P}{STRIKE*1000:08d}
    例子：IREN 60C exp 2026-06-14 → US.IREN260614C00060000

    注意：
    - strike * 1000 是因为 moomoo 用千分之一美元为单位
    - 8 位前导零填充，例如 strike=2.5 → 00002500
    """
    date_str = exp_date.strftime("%y%m%d")
    cp = "C" if side == "CALL" else "P"
    strike_str = f"{int(strike * 1000):08d}"
    return f"US.{symbol}{date_str}{cp}{strike_str}"


def place_order(signal: dict, qty: int = None) -> dict:
    """
    下单（同步函数，调用方需用 asyncio.to_thread 包装）

    参数：
        signal: 解析后的信号 dict，必须包含：
            - symbol (str)
            - expiry_date (date)
            - strike (float)
            - side ("CALL" / "PUT")
            - price (float)            # 信号原始价，用于算限价
            - tags (list, optional)    # 暂未使用，未来差异化策略用
        qty: 下单张数。None 则用 DEFAULT_QTY
             （现在由 discord_client 从 channel_config 拿，所以一般都会传）

    返回：见模块顶部约定的统一格式

    重要：本函数不再做任何风控判断（价格/成本/数量）。
          所有风控在 risk_manager.check_order() 完成。
          走到这里说明已经通过风控，可以无脑下单。
    """
    qty = qty if qty is not None else DEFAULT_QTY

    # 构造合约代码
    option_code = build_option_code(
        signal["symbol"],
        signal["expiry_date"],
        signal["strike"],
        signal["side"],
    )

    # 限价 = 信号价 + 滑点缓冲（向上取 2 位小数）
    limit_price = round(signal["price"] * (1 + MAX_SLIPPAGE_PCT), 2)

    logger.info(
        f"[broker] Order: {option_code} x {qty} @ {limit_price:.2f} "
        f"[env={TRD_ENV}, dry_run={DRY_RUN}] tags={signal.get('tags')}"
    )

    # ========== DRY_RUN / Mock 路径 ==========
    if DRY_RUN:
        return {
            "success": True,
            "message": "DRY_RUN - not actually placed",
            "order_id": "MOCK_001",
            "code": option_code,
            "qty": qty,
            "price": limit_price,
        }

    # ========== 真实下单路径（暂时屏蔽） ==========
    # TODO P0: 部署模拟盘时打开这段
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
    #     return {
    #         "success": False, "message": str(data),
    #         "order_id": None, "code": option_code, "qty": qty, "price": limit_price,
    #     }
    # except Exception as e:
    #     logger.exception("[broker] place_order exception")
    #     return {
    #         "success": False, "message": str(e),
    #         "order_id": None, "code": option_code, "qty": qty, "price": limit_price,
    #     }

    # SDK 未启用时的兜底返回（理论上 DRY_RUN=true 不会走到这里）
    return {
        "success": False,
        "message": "moomoo SDK not enabled (uncomment in moomoo_client.py)",
        "order_id": None,
        "code": option_code,
        "qty": qty,
        "price": limit_price,
    }
