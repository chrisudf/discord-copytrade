"""SL / EOD watcher 单元测试

只测核心 tick 逻辑，不跑 while True 主循环。
"""
import asyncio
import os
from datetime import date, datetime, timezone
from unittest.mock import patch, AsyncMock
from zoneinfo import ZoneInfo

import pytest

from src.storage import positions_db
from src.position import manager, sl_watcher, eod_watcher


ET_TZ = ZoneInfo("America/New_York")


def _uniq_code(prefix: str) -> str:
    return f"US.{prefix}{datetime.now().strftime('%H%M%S%f')}C001000"


def _open_weekly(symbol: str, code: str, qty: int = 2, entry: float = 1.00):
    """开一个 weekly 仓位（apply_sl=True）。"""
    return positions_db.open_or_add(
        option_code=code, symbol=symbol, strike=10.0, side="CALL",
        expiry=date(2026, 6, 25), qty=qty, fill_price=entry,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ut", msg_id="m1",
    )


def _open_0dte(symbol: str, code: str, expiry: date, qty: int = 2, entry: float = 1.00):
    """开一个 0DTE 仓位（apply_sl=False, eod_force_close=True）。"""
    return positions_db.open_or_add(
        option_code=code, symbol=symbol, strike=10.0, side="CALL",
        expiry=expiry, qty=qty, fill_price=entry,
        category="0dte", apply_sl=False, eod_force_close=True, tags=[],
        channel_name="ut", msg_id="m1",
    )


# ============ SL watcher ============

def _quote_for(code: str, price):
    """生成一个只对指定 code 返回价格、其他返回 None 的 get_last_price mock。

    避免前面 test 留下的 OPEN 仓位被本 test 的 mock 一起命中。
    """
    def _f(c):
        return price if c == code else None
    return _f


@pytest.mark.asyncio
async def test_sl_triggers_on_threshold():
    """entry 1.0, last 0.4 → 跌 60% > 50% → 触发"""
    code = _uniq_code("SL1")
    _open_weekly("SLT1", code, qty=3, entry=1.00)
    sl_watcher._triggered.discard(code)

    os.environ["STOP_LOSS_PCT"] = "0.50"
    with patch("src.position.sl_watcher.get_last_price", side_effect=_quote_for(code, 0.40)), \
         patch("src.position.sl_watcher.place_sell_order",
               return_value={"success": True, "qty": 3, "price": 0.37,
                             "order_id": "SL_ORD_1", "code": code}), \
         patch("src.position.sl_watcher.send_telegram_sync"):
        await sl_watcher._sl_tick()

    pos = positions_db.get(code)
    assert pos["status"] == "CLOSED"
    assert pos["qty_remaining"] == 0

    events = positions_db.get_events(code)
    sl_evt = [e for e in events if e["trigger_source"] == "sl_polling"]
    assert len(sl_evt) == 1
    assert sl_evt[0]["qty_delta"] == -3


@pytest.mark.asyncio
async def test_sl_skips_above_threshold():
    """entry 1.0, last 0.6 → 跌 40% < 50% → 不触发"""
    code = _uniq_code("SL2")
    _open_weekly("SLT2", code, qty=2, entry=1.00)
    sl_watcher._triggered.discard(code)

    os.environ["STOP_LOSS_PCT"] = "0.50"
    sell_mock = AsyncMock()
    with patch("src.position.sl_watcher.get_last_price", side_effect=_quote_for(code, 0.60)), \
         patch("src.position.sl_watcher.place_sell_order", side_effect=sell_mock), \
         patch("src.position.sl_watcher.send_telegram_sync"):
        await sl_watcher._sl_tick()

    sell_mock.assert_not_called()
    pos = positions_db.get(code)
    assert pos["status"] == "OPEN"
    # 收尾：避免污染下一个 test
    positions_db.record_close(code, qty_sold=2, fill_price=0.60,
                              trigger_source="manual", note="ut cleanup")


@pytest.mark.asyncio
async def test_sl_skips_apply_sl_false():
    """apply_sl=False 的 lotto/0dte 仓位即使深亏也不触发"""
    code = _uniq_code("SL3")
    positions_db.open_or_add(
        option_code=code, symbol="SLT3", strike=10.0, side="CALL",
        expiry=date(2026, 6, 25), qty=2, fill_price=1.00,
        category="lotto", apply_sl=False, eod_force_close=False, tags=["lotto"],
        channel_name="ut", msg_id="m1",
    )
    sl_watcher._triggered.discard(code)

    sell_mock = AsyncMock()
    with patch("src.position.sl_watcher.get_last_price", side_effect=_quote_for(code, 0.05)), \
         patch("src.position.sl_watcher.place_sell_order", side_effect=sell_mock), \
         patch("src.position.sl_watcher.send_telegram_sync"):
        await sl_watcher._sl_tick()
    sell_mock.assert_not_called()
    positions_db.record_close(code, qty_sold=2, fill_price=0.05,
                              trigger_source="manual", note="ut cleanup")


@pytest.mark.asyncio
async def test_sl_skips_when_quote_unavailable():
    """get_last_price 返回 None → 跳过该仓位（不触发）"""
    code = _uniq_code("SL4")
    _open_weekly("SLT4", code, qty=1, entry=1.00)
    sl_watcher._triggered.discard(code)

    sell_mock = AsyncMock()
    with patch("src.position.sl_watcher.get_last_price", return_value=None), \
         patch("src.position.sl_watcher.place_sell_order", side_effect=sell_mock), \
         patch("src.position.sl_watcher.send_telegram_sync"):
        await sl_watcher._sl_tick()
    sell_mock.assert_not_called()
    positions_db.record_close(code, qty_sold=1, fill_price=1.0,
                              trigger_source="manual", note="ut cleanup")


# ============ EOD watcher ============

def test_eod_window_weekday_after_cutoff():
    et = datetime(2026, 6, 17, 15, 51, tzinfo=ET_TZ)  # 周三 15:51
    assert eod_watcher._is_eod_window(et, 15, 50) is True


def test_eod_window_weekday_before_cutoff():
    et = datetime(2026, 6, 17, 15, 49, tzinfo=ET_TZ)
    assert eod_watcher._is_eod_window(et, 15, 50) is False


def test_eod_window_weekend():
    """周六不触发"""
    et = datetime(2026, 6, 20, 15, 51, tzinfo=ET_TZ)  # 周六
    assert eod_watcher._is_eod_window(et, 15, 50) is False


@pytest.mark.asyncio
async def test_eod_force_closes_matching_expiry():
    """expiry==today 且 eod_force_close=True → 强平"""
    today_et = datetime.now(timezone.utc).astimezone(ET_TZ).date()
    code = _uniq_code("EOD1")
    _open_0dte("EODT1", code, expiry=today_et, qty=2, entry=1.00)

    # 模拟 15:51 ET
    now_et = datetime.now(ET_TZ).replace(hour=15, minute=51, second=0, microsecond=0)
    # 强行让 weekday 是工作日
    while now_et.weekday() >= 5:
        now_et = now_et.replace(day=now_et.day - 1)

    with patch("src.position.eod_watcher.get_last_price", return_value=0.30), \
         patch("src.position.eod_watcher.place_sell_order",
               return_value={"success": True, "qty": 2, "price": 0.27,
                             "order_id": "EOD_ORD", "code": code}), \
         patch("src.position.eod_watcher.send_telegram_sync"), \
         patch("src.position.eod_watcher._is_eod_window", return_value=True):
        await eod_watcher._eod_tick(now_et)

    pos = positions_db.get(code)
    assert pos["status"] == "CLOSED"


@pytest.mark.asyncio
async def test_eod_skips_future_expiry():
    """eod_force_close=True 但 expiry 不是今天 → 不动"""
    future = date(2026, 12, 19)
    code = _uniq_code("EOD2")
    # 强行造一个"过去开的 0DTE 但 expiry 是未来"的怪状态
    positions_db.open_or_add(
        option_code=code, symbol="EODT2", strike=10.0, side="CALL",
        expiry=future, qty=1, fill_price=1.00,
        category="0dte", apply_sl=False, eod_force_close=True, tags=[],
        channel_name="ut", msg_id="m1",
    )

    now_et = datetime(2026, 6, 17, 15, 51, tzinfo=ET_TZ)
    sell_mock = AsyncMock()
    with patch("src.position.eod_watcher.place_sell_order", side_effect=sell_mock), \
         patch("src.position.eod_watcher._is_eod_window", return_value=True):
        await eod_watcher._eod_tick(now_et)
    sell_mock.assert_not_called()


@pytest.mark.asyncio
async def test_eod_skips_before_window():
    """未到 EOD 窗口 → 不动"""
    today_et = datetime.now(timezone.utc).astimezone(ET_TZ).date()
    code = _uniq_code("EOD3")
    _open_0dte("EODT3", code, expiry=today_et, qty=1, entry=1.00)

    now_et = datetime.now(ET_TZ).replace(hour=10, minute=0)
    sell_mock = AsyncMock()
    with patch("src.position.eod_watcher.place_sell_order", side_effect=sell_mock):
        await eod_watcher._eod_tick(now_et)
    sell_mock.assert_not_called()
