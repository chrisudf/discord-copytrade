"""止损守护：后台 asyncio task

职责：
- 每 POLL_INTERVAL 秒一轮
- 扫所有 apply_sl=True 的活跃仓位
- 拿 broker.get_last_price，比对 avg_entry * (1 - STOP_LOSS_PCT)
- 触发即全平（激进限价确保成交），调 on_close_filled + TG

设计权衡：
- 用 in-memory `_triggered` 防止单 tick 内重复触发同 code；
  on_close_filled 后 position 转 CLOSED，下轮自然不会再被选上
- 不做"先 UPDATE 占位再卖"——broker fail 后还想重试时反而麻烦
- Bot 崩溃 → in-memory set 丢失。但若 SL 已实际成交，position 也已 CLOSED；
  若 SL 卖单还在挂着没成交，重启会再发一次 sell——可接受（broker 会拒重复 or 多卖一份）
  TODO（实测调整）：要更稳就用 query_order_status 确认 fill 状态再标记

环境变量：
- STOP_LOSS_PCT       : 默认 0.50（亏 50% 触发）
- SL_POLL_INTERVAL    : 默认 5 秒
- SL_SELL_SLIP        : 默认 0.08（卖出限价相对当前价的下偏移，确保成交）

TODO（实测调整）：
- 50% 阈值经验值，看真实 fill 数据后可能要按 category 分档（weekly 紧一点）
- 8% 卖出 slip 在低流动性合约会被吃穿，要不要分档
- 行情 API 限流 / 失败时 backoff 而非死循环
- watcher 启动时要不要先打一次完整状态到 TG（"开始监控 N 个仓位"）
"""
import asyncio
import os
from typing import Optional

from src.broker.moomoo_client import place_sell_order, get_last_price
from src.position import manager as position_mgr
from src.notifier.telegram_client import (
    send_telegram_sync, format_close_filled, format_error,
)
from src.utils.logger import logger


def _cfg() -> dict:
    """实时读取配置，便于 .env 改动不重启即生效（与 DRY_RUN 同策略）。"""
    return {
        "sl_pct": float(os.getenv("STOP_LOSS_PCT", "0.50")),
        "interval": int(os.getenv("SL_POLL_INTERVAL", "5")),
        "sell_slip": float(os.getenv("SL_SELL_SLIP", "0.08")),
    }


# 单 tick 内已触发的 option_code，避免在等待 broker 返回期间被下一轮重复触发
# 同进程生命周期，bot 重启清空（接受这个 trade-off）
_triggered: set[str] = set()


async def _trigger_sl(pos: dict, last_price: float, threshold: float, sell_slip: float):
    """对单个仓位触发止损全平。"""
    code = pos["option_code"]
    if code in _triggered:
        logger.debug(f"[sl] already triggered this run: {code}")
        return
    _triggered.add(code)

    qty = pos["qty_remaining"]
    limit = round(last_price * (1 - sell_slip), 2)
    if limit <= 0:
        # 极低价兜底——0.01 起挂
        limit = 0.01

    logger.warning(
        f"[sl] 🛑 TRIGGER {code}: last={last_price:.2f} <= threshold={threshold:.2f} "
        f"(entry={pos['avg_entry_price']:.2f}), selling {qty} @ {limit}"
    )

    try:
        result = await asyncio.to_thread(
            place_sell_order,
            option_code=code, qty=qty,
            limit_price=limit, remark="sl_polling",
        )
    except Exception as e:
        logger.exception("[sl] place_sell_order failed")
        await asyncio.to_thread(send_telegram_sync,
            format_error("SL sell error", f"{code}\n{e}"))
        _triggered.discard(code)  # 让下一轮重试
        return

    if not result.get("success"):
        err = result.get("message", "unknown")
        logger.error(f"[sl] sell rejected: {err}")
        await asyncio.to_thread(send_telegram_sync,
            format_error("SL sell rejected", f"{code} qty={qty}\n{err}"))
        _triggered.discard(code)
        return

    try:
        position_mgr.on_close_filled(
            option_code=code,
            qty_sold=result.get("qty", qty),
            fill_price=result.get("price", limit),
            trigger_source="sl_polling",
            order_id=result.get("order_id"),
            note=f"SL: last={last_price:.2f} threshold={threshold:.2f} entry={pos['avg_entry_price']:.2f}",
        )
    except Exception as e:
        logger.error(f"[sl] on_close_filled failed: {e}")

    await asyncio.to_thread(send_telegram_sync, format_close_filled(
        pos["symbol"], pos["strike"], pos["side"], pos["expiry"],
        result.get("qty", qty), result.get("price", limit),
        100, "sl_polling", result.get("order_id", "N/A"),
    ))


async def _sl_tick():
    """单轮检查。可独立测试。"""
    cfg = _cfg()
    positions = [
        p for p in position_mgr.get_open_positions()
        if p.get("apply_sl") and p["qty_remaining"] > 0
    ]
    if not positions:
        return

    for pos in positions:
        code = pos["option_code"]
        last = await asyncio.to_thread(get_last_price, code)
        if last is None:
            continue
        threshold = pos["avg_entry_price"] * (1 - cfg["sl_pct"])
        if last <= threshold:
            await _trigger_sl(pos, last, threshold, cfg["sell_slip"])


async def run_sl_watcher():
    """后台主循环。在 start_listener 里 asyncio.create_task 启动。"""
    cfg = _cfg()
    logger.info(
        f"[sl] watcher started: pct={cfg['sl_pct']*100:.0f}% "
        f"interval={cfg['interval']}s sell_slip={cfg['sell_slip']*100:.0f}%"
    )
    while True:
        try:
            await _sl_tick()
        except Exception:
            logger.exception("[sl] tick error (continuing)")
        # 实时读 interval 让运行时调参生效
        await asyncio.sleep(_cfg()["interval"])
