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


def test_tag_day_trade_variants():
    """'small day trade' / 'daytrade' / 'day-trade' 都应该 tag day_trade"""
    r = parse_signal("TSLA 415c June 26 @ 2.70 small day trade",
                     msg_ts=FIXED_TODAY)
    assert "day_trade" in r["tags"]

    r = parse_signal("NOW 115c June 26 small fun day trade here @ 2.00",
                     msg_ts=FIXED_TODAY)
    assert "day_trade" in r["tags"]

    # daytrade（无空格）
    r = parse_signal("$SPY $746 daytrade puts $.40", msg_ts=FIXED_TODAY)
    assert "day_trade" in r["tags"]


def test_tag_no_day_trade_when_swing():
    """普通 swing 信号不该被打 day_trade tag"""
    r = parse_signal("MRVL 400c 7/2 @ 6.95 smaller size swing for now",
                     msg_ts=FIXED_TODAY)
    assert "day_trade" not in r["tags"]
    assert "swing" in r["tags"]


# === Pattern C: 简写格式（6/22 APLD 漏接修复）===

def test_apld_shorthand_weeklies():
    """6/22 凌晨 enrich 频道 APLD 漏接，原始文本：
    'Adding $APLD 50c weeklies here @role_1362783378704699603 +alert .98 fill'
    """
    r = parse_signal(
        "Adding $APLD 50c weeklies here @role_1362783378704699603 +alert .98 fill",
        msg_ts=FIXED_TODAY,
    )
    assert r is not None, "APLD 简写信号应该被识别"
    assert r["symbol"] == "APLD"
    assert r["side"] == "CALL"
    assert r["strike"] == 50.0
    assert r["price"] == 0.98


def test_shorthand_put_mmdd():
    """简写 + 显式日期：$SYMBOL Nc/p MM/DD ... .XX fill"""
    r = parse_signal("$SNOW 215p 7/2 .85 fill", msg_ts=FIXED_TODAY)
    assert r is not None
    assert r["symbol"] == "SNOW"
    assert r["side"] == "PUT"
    assert r["strike"] == 215.0
    assert r["price"] == 0.85
    assert r["expiry"] == "7/2"


def test_shorthand_at_price():
    """@$X.XX 价格写法"""
    r = parse_signal("Adding $NVDA 800c weeklies @ $1.20", msg_ts=FIXED_TODAY)
    assert r is not None
    assert r["symbol"] == "NVDA"
    assert r["strike"] == 800.0
    assert r["price"] == 1.20


def test_shorthand_at_price_no_dollar():
    """@.98 不带 $"""
    r = parse_signal("$IREN 60c weeklies @ .68", msg_ts=FIXED_TODAY)
    assert r is not None
    assert r["symbol"] == "IREN"
    assert r["price"] == 0.68


def test_shorthand_does_not_match_role_mention():
    """@role_数字 不应该被当成价格"""
    # 仅有 @role 没有真价格 → 不匹配
    r = parse_signal("Adding $APLD 50c weeklies @role_1362783378704699603 alert",
                     msg_ts=FIXED_TODAY)
    assert r is None, "没有合法价格写法应该返回 None"


def test_shorthand_requires_dollar_prefix():
    """裸 SYMBOL Nc/p 没有 $ 前缀不应该被 Pattern C 抓（避免假阳）。

    `APLD 50c` 在 A 路径需要 MM/DD，C 路径要求 $ 前缀，两个都不命中 → None
    """
    r = parse_signal("APLD 50c weeklies .98 fill", msg_ts=FIXED_TODAY)
    assert r is None
