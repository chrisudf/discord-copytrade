from datetime import date
import pytest
from src.parser.signal_parser import parse_signal

# 固定 msg_ts，避免测试随今天日期飘
FIXED_TODAY = date(2026, 6, 17)  # 周三，非假日


def test_basic_call():
    r = parse_signal("Lotto $IREN 0DTE $60 calls $.68", msg_ts=FIXED_TODAY)
    assert r["symbol"] == "IREN"
    assert r["side"] == "CALL"
    assert r["strike"] == 60.0
    assert r["price"] == 0.68
    # NDTE 被 _finalize_signal 覆盖成 M/D（与 expiry_date 对齐）
    assert r["expiry"] == "6/17"
    assert r["expiry_date"] == date(2026, 6, 17)


def test_put():
    r = parse_signal("$TSLA 1DTE $250 puts $1.20", msg_ts=FIXED_TODAY)
    assert r["symbol"] == "TSLA"
    assert r["side"] == "PUT"
    assert r["strike"] == 250.0
    assert r["price"] == 1.20
    # 6/18 周四，是交易日，不被假日调整
    assert r["expiry"] == "6/18"


def test_invalid():
    assert parse_signal("hello world") is None
