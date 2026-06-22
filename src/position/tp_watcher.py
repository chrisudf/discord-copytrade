"""分批止盈守护：后台 asyncio task

设计基于用户策略（对话定稿）：
- weekly: +50% trim 50% → +100% trim 剩余 50% → 剩 ~25% 跑（靠 EOD/expiry/手动）
- swing:  +100% trim 50% → +200% trim 剩余 50% → 剩 ~25% 跑
- 0dte / lotto: 不挂 TP（前者靠 EOD，后者放飞）

档位 = 阈值是 avg_entry_price * (1 + threshold_pct)，
按 ladder 顺序触发，每档卖固定比例。

防重复触发：positions.tp_hits 位掩码持久化，T1=1 / T2=2 / T3=4。
- T1 触发后置位 → 下次扫描不再考虑 T1
- bot 重启不丢，跟 _triggered 内存 set 比更稳

环境变量：
- TP_POLL_INTERVAL   : 默认 5 秒（同 SL）
- TP_SELL_SLIP       : 默认 0.05（卖出限价 = last * 0.95，TP 不需要 SL 那么激进）

TODO（实测调整）：
- 阈值经验值，跑实盘后 PnL 复盘哪档过早 / 过晚
- T1/T2 卖出比例（现 50%/50%）也是经验值
- swing 的 +100/+200 太宽？小波动 swing 可能整周到不了
- 加 trailing TP：T1 触发后启动 trailing，从高点回撤 N% 卖剩余
- 共用 quote tick 而非独立 polling（SL + TP + future 健康检查合并一个 loop）
"""
import asyncio
import os
from typing import Optional

from src.broker.moomoo_client import place_sell_order, get_last_price
from src.position import manager as position_mgr
from src.storage import positions_db
from src.notifier.telegram_client import (
    send_telegram_sync, format_close_filled, format_error,
)
from src.utils.logger import logger


# Ladder 定义：(threshold_pct, trim_pct_of_remaining, tier_bit)
# 注意 trim_pct 是相对"当时剩余"，所以 T1 卖 50% → 剩 50%；T2 再卖 50% → 剩 25%
LADDER = {
    "weekly": [
        (0.50, 50, 1),    # T1: +50% → 卖剩余的 50%
        (1.00, 50, 2),    # T2: +100% → 卖剩余的 50%
    ],
    "swing": [
        (1.00, 50, 1),    # T1: +100% → 卖剩余的 50%
        (2.00, 50, 2),    # T2: +200% → 卖剩余的 50%
    ],
    # 0dte / lotto / 0dte_lotto 不在表里 = 不挂 TP
}


def _cfg() -> dict:
    return {
        "interval": int(os.getenv("TP_POLL_INTERVAL", "5")),
        "sell_slip": float(os.getenv("TP_SELL_SLIP", "0.05")),
    }


# 单 tick 内已触发的 (code, tier_bit) 避免在 broker 返回前重复触发
_triggered_this_tick: set[tuple[str, int]] = set()


async def _trigger_tp(pos: dict, last_price: float, threshold_pct: float,
                      trim_pct: int, tier_bit: int, sell_slip: float):
    """对单个仓位触发 TP 单档。"""
    code = pos["option_code"]
    key = (code, tier_bit)
    if key in _triggered_this_tick:
        return
    _triggered_this_tick.add(key)

    qty_to_sell = max(1, round(pos["qty_remaining"] * trim_pct / 100))
    qty_to_sell = min(qty_to_sell, pos["qty_remaining"])
    limit = round(last_price * (1 - sell_slip), 2)
    if limit <= 0:
        limit = 0.01

    logger.info(
        f"[tp] 🎯 T{tier_bit} HIT {code}: last={last_price:.2f} "
        f">= entry*({1+threshold_pct:.2f})={pos['avg_entry_price']*(1+threshold_pct):.2f}, "
        f"selling {qty_to_sell}/{pos['qty_remaining']} @ {limit}"
    )

    try:
        result = await asyncio.to_thread(
            place_sell_order,
            option_code=code, qty=qty_to_sell,
            limit_price=limit, remark=f"tp_t{tier_bit}",
        )
    except Exception as e:
        logger.exception("[tp] place_sell_order failed")
        _triggered_this_tick.discard(key)
        await asyncio.to_thread(send_telegram_sync,
            format_error("TP sell error", f"{code}\n{e}"))
        return

    if not result.get("success"):
        err = result.get("message", "unknown")
        logger.error(f"[tp] sell rejected: {err}")
        _triggered_this_tick.discard(key)
        await asyncio.to_thread(send_telegram_sync,
            format_error("TP sell rejected", f"{code} qty={qty_to_sell}\n{err}"))
        return

    # 先持久化档位（即使下面 on_close_filled 出错也不会重复触发同档）
    try:
        positions_db.mark_tp_hit(code, tier_bit)
    except Exception as e:
        logger.error(f"[tp] mark_tp_hit failed: {e}")

    try:
        position_mgr.on_close_filled(
            option_code=code,
            qty_sold=result.get("qty", qty_to_sell),
            fill_price=result.get("price", limit),
            trigger_source="tp_polling",
            order_id=result.get("order_id"),
            note=f"TP T{tier_bit} +{int(threshold_pct*100)}%: last={last_price:.2f}",
        )
    except Exception as e:
        logger.error(f"[tp] on_close_filled failed: {e}")

    await asyncio.to_thread(send_telegram_sync, format_close_filled(
        pos["symbol"], pos["strike"], pos["side"], pos["expiry"],
        result.get("qty", qty_to_sell), result.get("price", limit),
        trim_pct, f"tp_t{tier_bit}", result.get("order_id", "N/A"),
    ))


async def _tp_tick():
    """单轮检查。仅扫 category 在 LADDER 里的活跃仓位。"""
    cfg = _cfg()
    global _triggered_this_tick
    _triggered_this_tick = set()  # tick 边界重置（每轮独立判断）

    positions = [
        p for p in position_mgr.get_open_positions()
        if p["category"] in LADDER and p["qty_remaining"] > 0
    ]
    if not positions:
        return

    for pos in positions:
        cat = pos["category"]
        last = await asyncio.to_thread(get_last_price, pos["option_code"])
        if last is None:
            continue

        # 检查每档：未触发过 + 价格达标 → 触发
        for threshold_pct, trim_pct, tier_bit in LADDER[cat]:
            if pos["tp_hits"] & tier_bit:
                continue  # 此档已触发过
            threshold_price = pos["avg_entry_price"] * (1 + threshold_pct)
            if last >= threshold_price:
                await _trigger_tp(pos, last, threshold_pct, trim_pct, tier_bit, cfg["sell_slip"])
                # 触发一档后 pos 数据已过时（qty_remaining 变了），中断本仓位本轮
                # 下一轮 tick 重新读取最新状态，自然处理下一档
                break


async def run_tp_watcher():
    cfg = _cfg()
    logger.info(
        f"[tp] watcher started: interval={cfg['interval']}s "
        f"sell_slip={cfg['sell_slip']*100:.0f}% "
        f"ladders={ {k: [(int(t*100), p) for t,p,_ in v] for k,v in LADDER.items()} }"
    )
    while True:
        try:
            await _tp_tick()
        except Exception:
            logger.exception("[tp] tick error (continuing)")
        await asyncio.sleep(_cfg()["interval"])
