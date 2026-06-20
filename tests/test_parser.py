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


def test_scalp_puts_with_modifier():
    """`$SPY $746 scalp puts $.40` —— strike 和 puts 之间有 'scalp' 修饰词

    旧 pattern `\\s*(calls?|puts?)` 太严，scalp 卡住返回 None。
    现在允许中间最多 3 个修饰词。
    """
    r = parse_signal("$SPY $746 scalp puts can work here $.40 - in and out",
                     msg_ts=FIXED_TODAY)
    assert r is not None
    assert r["symbol"] == "SPY"
    assert r["side"] == "PUT"
    assert r["strike"] == 746.0
    assert r["price"] == 0.40


def test_weekly_modifier():
    """`$AAOI weekly $220 calls for $2.05`"""
    r = parse_signal("$AAOI weekly $220 calls for $2.05", msg_ts=FIXED_TODAY)
    assert r is not None
    assert r["symbol"] == "AAOI"
    assert r["strike"] == 220.0
    assert r["price"] == 2.05


def test_detect_action_zh_close():
    """中文 close 关键字应被识别为 CLOSE，不能漏到 OPEN parser"""
    from src.parser.signal_parser import detect_action
    assert detect_action("KC交易机器人：减仓特斯拉 3.00") == "CLOSE"
    assert detect_action("$AAOI 全部卖出") == "CLOSE"
    assert detect_action("平仓微软420看涨 @ 7.00") == "CLOSE"
    # 纯 OPEN 信号不能误判
    assert detect_action("TSLA 415c June 26 @ 2.70") == "OPEN"
    assert detect_action("$AAOI weekly $220 calls $2.05") == "OPEN"
