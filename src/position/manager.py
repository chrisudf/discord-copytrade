"""持仓管理（高层接口）

把 positions_db 的低层操作和业务语义粘起来：
- 买单成交 → on_order_filled：算 category、入库、决定是否挂 SL
- 卖单成交 → on_close_filled：扣减、推 PnL 估算到 TG
- 暴露 get_open_symbols 给 CLOSE parser

不直接调 broker —— broker.place_*_order 由 listener / polling 触发，
manager 只负责"事后"持久化。这样 DRY_RUN 路径也能完整跑流程。

TODO（测试调整）：
- on_order_filled 在 DRY_RUN 下也写 positions，方便联调；上真盘后要不要加个开关
  避免 mock 仓位污染状态
- broker 返回的 price 是 limit_price 不是真实 fill_price——上真盘后改用
  query_order_status 拿 dealt_avg_price，回填 avg_entry_price
- on_close_filled 没算实际 PnL，等真实 fill 数据接入后补
"""
import math
from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from src.storage import positions_db
from src.utils.logger import logger

ET_TZ = ZoneInfo("America/New_York")


def _today_et() -> date:
    return datetime.now(timezone.utc).astimezone(ET_TZ).date()


def on_order_filled(
    signal: dict,
    order_result: dict,
    channel_name: str,
    msg_id: str,
) -> Optional[dict]:
    """买单确认 success=True 之后调用。

    Args:
        signal: parser 输出的 dict（含 symbol/strike/side/expiry_date/tags/price）
        order_result: broker.place_order 返回（含 code/qty/price=limit_price/order_id）
        channel_name: 来源 channel
        msg_id: 触发消息 ID

    Returns:
        新建/更新后的 position dict；缺失关键字段返回 None。
    """
    option_code = order_result.get("code")
    qty = order_result.get("qty")
    fill_price = order_result.get("price")  # = limit_price，TODO 真盘换 dealt_avg
    expiry_d = signal.get("expiry_date")

    if not (option_code and qty and fill_price and expiry_d):
        logger.error(
            f"[position_mgr] missing fields: code={option_code} qty={qty} "
            f"price={fill_price} exp={expiry_d}"
        )
        return None

    tags = signal.get("tags") or []
    category, apply_sl, eod_force = positions_db.categorize(
        expiry_d, _today_et(), tags
    )

    pos = positions_db.open_or_add(
        option_code=option_code,
        symbol=signal["symbol"],
        strike=signal["strike"],
        side=signal["side"],
        expiry=expiry_d,
        qty=qty,
        fill_price=fill_price,
        category=category,
        apply_sl=apply_sl,
        eod_force_close=eod_force,
        tags=tags,
        channel_name=channel_name,
        msg_id=str(msg_id),
    )

    logger.info(
        f"[position_mgr] OPEN {option_code} qty={qty} avg={fill_price:.2f} "
        f"category={category} apply_sl={apply_sl} eod={eod_force} tags={tags}"
    )
    return pos


def on_close_filled(
    option_code: str,
    qty_sold: int,
    fill_price: float,
    trigger_source: str,
    ref_msg_id: Optional[str] = None,
    order_id: Optional[str] = None,
    note: str = "",
) -> Optional[dict]:
    """卖单确认 success=True 之后调用，扣减 qty_remaining。

    trigger_source 枚举：kc_signal / sl_polling / tp_polling / eod / manual
    """
    return positions_db.record_close(
        option_code=option_code,
        qty_sold=qty_sold,
        fill_price=fill_price,
        trigger_source=trigger_source,
        ref_msg_id=ref_msg_id,
        order_id=order_id,
        note=note,
    )


# ============ 给 CLOSE parser / polling 用的查询接口 ============

def get_open_symbols() -> set[str]:
    """活跃仓位 symbol 集合（CLOSE parser 白名单）。"""
    return positions_db.get_open_symbols()


def get_open_positions() -> list[dict]:
    return positions_db.get_open_positions()


def find_by_symbol(symbol: str) -> list[dict]:
    return positions_db.find_by_symbol(symbol)


def calc_qty_to_sell(position: dict, pct: int) -> int:
    """根据 close 信号给的 % 算实际卖出张数。

    规则（v1）：
    - pct=100 → 全平剩余
    - 否则向上取整，至少卖 1 张
      （信号说"trim 33%"但只剩 1 张 → 卖掉 1 张比留着合理，反正是 trim 意图）

    用 math.ceil 而不是 round()：round() 是 banker's rounding，
    remaining=5/pct=50 会算成 2（应为 3），与"向上取整"语义不符。

    TODO: 实测后看 33% 是否应该向下取整保留 runner
    """
    remaining = position["qty_remaining"]
    if pct >= 100:
        return remaining
    qty = max(1, math.ceil(remaining * pct / 100))
    return min(qty, remaining)
