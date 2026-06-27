"""positions_db + position manager 冒烟测试

注意：用真实 DB 文件（data/trades.db）—— 测试前后用唯一 option_code 隔离，
不清表避免污染线上数据。TODO: 后续切到 tmpdir + monkeypatch DB_PATH 干净隔离。
"""
from datetime import date, datetime, timezone
import pytest

from src.storage import positions_db
from src.position import manager


def _uniq(prefix: str = "TEST") -> str:
    """唯一 option_code，避免测试间污染。"""
    return f"US.{prefix}{datetime.now().strftime('%H%M%S%f')}C001000"


def test_categorize_weekly_has_sl():
    today = date(2026, 6, 17)
    cat, sl, eod = positions_db.categorize(date(2026, 6, 20), today, [])
    assert (cat, sl, eod) == ("weekly", True, False)


def test_categorize_0dte_no_sl_but_eod_force():
    today = date(2026, 6, 17)
    cat, sl, eod = positions_db.categorize(today, today, [])
    assert (cat, sl, eod) == ("0dte", False, True)


def test_categorize_0dte_lotto_still_eod_force():
    """0DTE + lotto 标签：仍然必须 EOD 强平（合约要过期）。"""
    today = date(2026, 6, 17)
    cat, sl, eod = positions_db.categorize(today, today, ["lotto"])
    assert (cat, sl, eod) == ("0dte_lotto", False, True)


def test_categorize_weekly_lotto_no_sl_no_eod():
    """周内 lotto：放飞到 expiry，不挂 SL、不 EOD 强平。"""
    today = date(2026, 6, 17)
    cat, sl, eod = positions_db.categorize(date(2026, 6, 20), today, ["lotto"])
    assert (cat, sl, eod) == ("lotto", False, False)


def test_categorize_swing_no_sl():
    today = date(2026, 6, 17)
    cat, sl, eod = positions_db.categorize(date(2026, 7, 17), today, [])
    assert (cat, sl, eod) == ("swing", False, False)


def test_open_and_partial_close():
    code = _uniq("OPN")
    pos = positions_db.open_or_add(
        option_code=code, symbol="TEST", strike=10.0, side="CALL",
        expiry=date(2026, 6, 25), qty=4, fill_price=1.50,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ut", msg_id="m1",
    )
    assert pos["qty_remaining"] == 4
    assert pos["status"] == "OPEN"
    assert pos["apply_sl"] is True

    # 部分卖 2 张
    pos = positions_db.record_close(
        option_code=code, qty_sold=2, fill_price=2.50,
        trigger_source="kc_signal", ref_msg_id="m2",
    )
    assert pos["qty_remaining"] == 2
    assert pos["status"] == "PARTIAL"

    # 全平剩下
    pos = positions_db.record_close(
        option_code=code, qty_sold=2, fill_price=3.00,
        trigger_source="tp_polling",
    )
    assert pos["qty_remaining"] == 0
    assert pos["status"] == "CLOSED"
    assert pos["closed_at"] is not None


def test_add_on_weighted_average():
    code = _uniq("ADD")
    positions_db.open_or_add(
        option_code=code, symbol="TEST", strike=10.0, side="CALL",
        expiry=date(2026, 6, 25), qty=2, fill_price=1.00,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ut", msg_id="m1",
    )
    pos = positions_db.open_or_add(
        option_code=code, symbol="TEST", strike=10.0, side="CALL",
        expiry=date(2026, 6, 25), qty=2, fill_price=2.00,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ut", msg_id="m2",
    )
    assert pos["qty_total"] == 4
    assert pos["qty_remaining"] == 4
    assert pos["avg_entry_price"] == pytest.approx(1.50)


def test_over_close_clamps():
    code = _uniq("OVR")
    positions_db.open_or_add(
        option_code=code, symbol="TEST", strike=10.0, side="CALL",
        expiry=date(2026, 6, 25), qty=1, fill_price=1.00,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ut", msg_id="m1",
    )
    pos = positions_db.record_close(
        option_code=code, qty_sold=5, fill_price=2.00,  # 卖多了
        trigger_source="manual",
    )
    assert pos["qty_remaining"] == 0
    assert pos["status"] == "CLOSED"


def test_get_open_symbols_excludes_closed():
    code = _uniq("SYM")
    positions_db.open_or_add(
        option_code=code, symbol="ZZTEST", strike=10.0, side="CALL",
        expiry=date(2026, 6, 25), qty=1, fill_price=1.00,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ut", msg_id="m1",
    )
    assert "ZZTEST" in positions_db.get_open_symbols()
    positions_db.record_close(code, 1, 2.0, trigger_source="manual")
    assert "ZZTEST" not in positions_db.get_open_symbols()


def test_manager_on_order_filled_routes_correctly():
    code = _uniq("MGR")
    # 用相对日期 today+30 而非硬编码：避免日历走过 2026-06-25 后
    # categorize() 把信号判成 0DTE → category 变 "0dte_lotto"
    from datetime import timedelta
    signal = {
        "symbol": "MGRTEST", "strike": 10.0, "side": "CALL",
        "expiry_date": date.today() + timedelta(days=30), "price": 1.0,
        "tags": ["lotto"],
    }
    order_result = {
        "success": True, "code": code, "qty": 2, "price": 1.10,
        "order_id": "ORD1",
    }
    pos = manager.on_order_filled(
        signal=signal, order_result=order_result,
        channel_name="ut", msg_id="m1",
    )
    assert pos is not None
    assert pos["category"] == "lotto"
    assert pos["apply_sl"] is False
    assert pos["avg_entry_price"] == pytest.approx(1.10)


def test_calc_qty_to_sell():
    pos = {"qty_remaining": 4}
    assert manager.calc_qty_to_sell(pos, 100) == 4
    assert manager.calc_qty_to_sell(pos, 50) == 2
    assert manager.calc_qty_to_sell(pos, 25) == 1
    # 向上取整保证至少 1 张
    pos = {"qty_remaining": 1}
    assert manager.calc_qty_to_sell(pos, 33) == 1
