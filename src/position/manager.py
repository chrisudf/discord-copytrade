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
import asyncio
import math
from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from src.storage import positions_db
from src.utils.logger import logger

ET_TZ = ZoneInfo("America/New_York")


def _today_et() -> date:
    return datetime.now(timezone.utc).astimezone(ET_TZ).date()


# ============ 卖出串行化 ============
# 每个 option_code 一把 asyncio.Lock，序列化四条卖出路径
# （kc_close / sl_polling / tp_polling / eod_force）。
#
# 背景：每条路径都是"读仓位 → await broker(to_thread) → 写 DB"，
# 读与写之间有秒级窗口，两条路径可能同时读到 qty_remaining=N 并各卖 N 张。
# DB 端 record_close 的 clamp 会把超卖藏起来，broker 端则可能变成重复卖单
# （naked-short check 只在真盘生效，且自身是 check-then-act，拦不住并发）。
#
# 用法约定：拿到锁后必须用 manager.get(option_code) 重读仓位再决定卖多少，
# 不能用锁外读到的旧 dict。
#
# dict 无界但 key 数 = 历史 option_code 数（个人跟单场景一天个位数），不做 GC。
_sell_locks: dict[str, asyncio.Lock] = {}


def sell_lock(option_code: str) -> asyncio.Lock:
    """取（或创建）该 option_code 的卖出锁。单 event loop 下无竞态。"""
    lock = _sell_locks.get(option_code)
    if lock is None:
        lock = _sell_locks.setdefault(option_code, asyncio.Lock())
    return lock


def get(option_code: str) -> Optional[dict]:
    """按 option_code 取当前仓位（含 CLOSED）。卖出路径锁内重读用。"""
    return positions_db.get(option_code)


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

def reconcile_to_broker(option_code: str, broker_qty: int) -> bool:
    """naked-short 脱钩时把本地持仓核销到 broker 实数。见 positions_db.reconcile_to_broker。

    Returns True 仅当确实改动了一行活跃仓位（调用方据此只告警一次）。
    """
    return positions_db.reconcile_to_broker(option_code, broker_qty)


def get_open_symbols() -> set[str]:
    """活跃仓位 symbol 集合（CLOSE parser 白名单）。"""
    return positions_db.get_open_symbols()


def get_open_positions() -> list[dict]:
    return positions_db.get_open_positions()


def find_by_symbol(symbol: str) -> list[dict]:
    return positions_db.find_by_symbol(symbol)


def sweep_expired() -> list[dict]:
    """清扫 expiry < 今天（ET）的残留仓位 → EXPIRED。返回被清的仓位。

    调用点：listener 启动时（watchers 起来之前，避免第一轮 tick 就对
    过期 code 取快照进 backoff）+ eod_watcher 每轮 tick（跨日兜底）。
    """
    return positions_db.sweep_expired(_today_et())


def calc_qty_to_sell(position: dict, pct: int) -> int:
    """根据 close 信号给的 % 算实际卖出张数。

    规则（v2, 2026-07-02 开始）：
    - pct >= 100 → 全平剩余（"closed all" / "out full" / KC 明确清仓）
    - remaining == 1 且 pct < 100 → **不卖，保留 runner**
      理由：跟单单张持仓时，任何 <100% 的 trim 数学上都会被 math.ceil 拉到 1，
      即等于全平。历史损失：
        - 6/30 SPY 748c：$2.59 → 我们 33% 平在 $2.71，KC 后续 4.00 (+40%)
        - 7/1 MSFT 390c：$2.48 → 我们 33% 平在 $2.56，KC 后续 4.60 (+100%)
      合计放弃 ~$330+/合约。选项 A（保留 runner）优于全平退出。
      100% 明确清仓仍然会正常执行——不影响真反转信号。
    - remaining > 1 → 向上取整（math.ceil 而非 round，round 是 banker's rounding，
      remaining=5/pct=50 会误算 2 而非 3）

    TODO（等 OPRA 权限）：升级到策略 B —— 用报价判断"我们已经到 +X%"再选择性
    响应 trim 信号（早期跟单，中后期变 runner）。见 docs/TODO.md 中 P0。
    """
    remaining = position["qty_remaining"]
    if pct >= 100:
        return remaining
    if remaining == 1:
        # 策略 A：保留 runner，等真正的 100% close 信号
        logger.info(
            f"[calc_qty_to_sell] runner-preserve: remaining=1 pct={pct}, "
            f"skipping trim (option={position.get('option_code', '?')})"
        )
        return 0
    qty = max(1, math.ceil(remaining * pct / 100))
    return min(qty, remaining)
