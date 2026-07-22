"""listener 报警启发式测试（7/13 复盘新增的两个）

1. _looks_like_sized_entry：enrich 风格"带仓位比例、无 C/P 方向"的入场，
   parser 不能下单（不猜方向）但必须 TG 提醒。
2. _twin_of_recent_exec：EN 信号执行成功后 ~1s 的 ZH 翻译孪生 parse-fail
   不该触发 "Parse failed (looks like signal)" 报警。
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.listener import discord_client as dc


# ============ sized entry（enrich 无方向入场） ============

def test_sized_entry_hits_enrich_ibm_en_and_zh():
    # 7/13 00:22-00:24 实际漏掉的 4 条（中英×2）
    en = "enrich:\n$IBM weekly $310 $1.33\n\n2% position \n\n@everyone $alert"
    zh = "enrich:\n$IBM 每周 $310 $1.33\n\n2% 头寸 \n\n@everyone $alert"
    assert dc._looks_like_sized_entry(en) == "IBM"
    assert dc._looks_like_sized_entry(zh) == "IBM"


def test_sized_entry_ignores_levels_message():
    # enrich 每日 levels：一个 ticker + 一堆 $数字，但没有 "N% position"
    text = (
        "enrich:\n$SPY levels for the day 7/13/2026:\n\n"
        "Blue zone= $751.53, $753.85\n"
        "Green targets = $754.88, $756.24, $757.51\n"
        "Red targets = $750.37, $749.13, $746.99\n\n@everyone $alert"
    )
    assert dc._looks_like_sized_entry(text) is None


def test_sized_entry_ignores_holding_update():
    # 有 "2% position" 但没有 strike/price（$数字 < 2）——纯持仓状态
    text = "$IBM - Holding my 2% position - rest of the portfolio is cash today."
    assert dc._looks_like_sized_entry(text) is None


def test_sized_entry_ignores_multi_ticker_watchlist():
    text = "holding into tomorrow: $CIFR 20% position $1.50, $ASTS 20% position $2.50"
    assert dc._looks_like_sized_entry(text) is None


def test_sized_entry_ignores_plain_commentary():
    text = "$IBM - Hourly wedge still looks A+ and intact. I'm in for now."
    assert dc._looks_like_sized_entry(text) is None


# ============ ZH/EN 孪生 parse-fail 报警抑制 ============

@pytest.fixture(autouse=True)
def _clean_recent_exec():
    dc._recent_exec.clear()
    yield
    dc._recent_exec.clear()


def _mu_signal():
    """_record_recent_exec 需要的最小开仓信号快照。"""
    return {"symbol": "MU", "strike": 1050.0, "side": "CALL", "price": 2.60}


def test_twin_suppressed_same_channel_same_symbol():
    dc._record_recent_exec(123, _mu_signal())
    zh = "@everyone\nKC Trades Bot：MU 1050c 7月15日 @ 2.60 日内交易彩票"
    assert dc._twin_of_recent_exec(zh, 123) == "MU"


def test_twin_not_suppressed_other_channel():
    dc._record_recent_exec(123, _mu_signal())
    zh = "KC Trades Bot：MU 1050c 7月15日 @ 2.60"
    assert dc._twin_of_recent_exec(zh, 456) is None


def test_twin_not_suppressed_different_symbol():
    # 60s 内的**另一个**标的 parse-fail 是真漏检，必须照常报警
    dc._record_recent_exec(123, _mu_signal())
    assert dc._twin_of_recent_exec("NFLX 80c 7/17 @ 1.38 day trade", 123) is None


def test_twin_suppression_expires_after_window():
    dc._recent_exec[(123, "MU")] = {
        "ts": datetime.now(timezone.utc) - dc._TWIN_SUPPRESS_WINDOW - timedelta(seconds=1),
        "strike": 1050.0, "side": "CALL", "price": 2.60,
    }
    assert dc._twin_of_recent_exec("MU 1050c @ 2.60", 123) is None


def test_twin_matches_ticker_attached_to_hanzi():
    # ZH 文本汉字紧贴 ticker（"减仓MU"），\b 不触发，lookaround 要能命中
    dc._record_recent_exec(123, _mu_signal())
    assert dc._twin_of_recent_exec("KC交易机器人：减仓MU 1050c @ 2.60", 123) == "MU"


def test_record_recent_exec_prunes_stale_entries():
    dc._recent_exec[(123, "OLD")] = {
        "ts": datetime.now(timezone.utc) - dc._TWIN_SUPPRESS_WINDOW - timedelta(seconds=5),
        "strike": 1.0, "side": "CALL", "price": 1.0,
    }
    dc._record_recent_exec(123, _mu_signal())
    assert (123, "OLD") not in dc._recent_exec
    assert (123, "MU") in dc._recent_exec


# ============ 原文级消息去重（7/14 源频道每条消息双发） ============

@pytest.fixture(autouse=True)
def _clean_recent_raw():
    dc._recent_raw.clear()
    yield
    dc._recent_raw.clear()


def test_raw_dedup_same_channel_same_text():
    raw = "@everyone\nKC Trades Bot:PLTR 160c 8/21 starter swing @ 2.50"
    assert dc._is_duplicate_raw(123, raw) is False  # 首条放行
    assert dc._is_duplicate_raw(123, raw) is True   # 双发第二条挡住


def test_raw_dedup_different_channel_not_blocked():
    raw = "same text"
    assert dc._is_duplicate_raw(123, raw) is False
    assert dc._is_duplicate_raw(456, raw) is False


def test_raw_dedup_different_text_not_blocked():
    assert dc._is_duplicate_raw(123, "trimmed PLTR @ 3.40") is False
    assert dc._is_duplicate_raw(123, "trimmed PLTR @ 3.65") is False


def test_raw_dedup_expires_after_window():
    raw = "repeat me"
    dc._recent_raw[(123, raw)] = (
        datetime.now(timezone.utc) - dc._RAW_DEDUP_WINDOW - timedelta(seconds=1)
    )
    # 窗口外的旧记录不算重复（KC 隔几分钟重发同文本是真实场景）
    assert dc._is_duplicate_raw(123, raw) is False


# ============ ZH 方向词进 looks-like-signal 启发式 ============

def test_open_attempt_recognizes_zh_side_word():
    """parser 因其他原因失败的 ZH 方向信号应报 "looks like signal"，
    而不是掉进 sized-entry 的"无 C/P 方向"（7/14 HOOD 误报）。"""
    assert dc._looks_like_open_attempt("$HOOD - 7/24 $125 看涨期权 $1.50") is True
    # 且不再被 sized-entry 分支捕获的前提成立：有方向词的文本 side RE 必命中
    assert dc._OPEN_SIDE_RE.search("买入看跌期权对冲") is not None


# ============ scalp/NDTE 无方向入场提醒（7/15 MSFT 0DTE 漏检） ============

def test_sized_entry_hits_scalp_dte_format():
    # 7/15 实测原文（中英双发全静默漏掉，后续 +200%）
    en = "enrich:\nScalp - $MSFT 0DTE $397.50 $.90\n\n@everyone $alert"
    zh = "enrich:\n头皮 - $MSFT 0DTE $397.50 $.90\n\n@everyone $alert"
    assert dc._looks_like_sized_entry(en) == "MSFT"
    assert dc._looks_like_sized_entry(zh) == "MSFT"


def test_sized_entry_dte_needs_two_dollar_numbers():
    # "$META scalp $685s" —— 无 DTE 无价格，不值得提醒
    assert dc._looks_like_sized_entry("enrich:\n$META scalp $685s") is None


# ============ 翻译孪生 close 防护（7/15 SPY "smaller size"→"小规模减仓"） ============

def _spy_open():
    return {"symbol": "SPY", "strike": 760.0, "side": "CALL", "price": 3.0}


def test_close_twin_blocked_by_same_price():
    dc._record_recent_exec(123, _spy_open())
    parsed = {"signal_price": 3.0, "hint_strike": None, "hint_side": None}
    assert dc._close_is_open_twin(123, "SPY", parsed) is not None


def test_close_twin_blocked_by_same_contract_hint():
    dc._record_recent_exec(123, _spy_open())
    # 7/15 实测 parse 结果：strike=760.0 side=CALL price=3.0
    parsed = {"signal_price": None, "hint_strike": 760.0, "hint_side": "CALL"}
    assert dc._close_is_open_twin(123, "SPY", parsed) is not None


def test_close_twin_not_blocked_different_price():
    # 真砍仓：价格已变（-11% 后 KC 喊 2.65 之类），必须放行
    dc._record_recent_exec(123, _spy_open())
    parsed = {"signal_price": 2.65, "hint_strike": None, "hint_side": None}
    assert dc._close_is_open_twin(123, "SPY", parsed) is None


def test_close_twin_not_blocked_no_price_no_hint():
    # "全部平仓 SPY -11%" 无价无 strike —— 不能仅凭时间窗判定孪生
    dc._record_recent_exec(123, _spy_open())
    parsed = {"signal_price": None, "hint_strike": None, "hint_side": None}
    assert dc._close_is_open_twin(123, "SPY", parsed) is None


def test_close_twin_not_blocked_outside_window():
    dc._recent_exec[(123, "SPY")] = {
        "ts": datetime.now(timezone.utc) - dc._TWIN_SUPPRESS_WINDOW - timedelta(seconds=1),
        "strike": 760.0, "side": "CALL", "price": 3.0,
    }
    parsed = {"signal_price": 3.0, "hint_strike": None, "hint_side": None}
    assert dc._close_is_open_twin(123, "SPY", parsed) is None


def test_close_twin_not_blocked_other_channel_or_none_cid():
    dc._record_recent_exec(123, _spy_open())
    parsed = {"signal_price": 3.0, "hint_strike": None, "hint_side": None}
    assert dc._close_is_open_twin(456, "SPY", parsed) is None
    assert dc._close_is_open_twin(None, "SPY", parsed) is None  # 旧调用方/测试


# ============ 短线标签 × 长 DTE 防护（7/16 June-20 笔误 → 2027 合约） ============

from datetime import date


def test_suspicious_long_dte_blocks_typo():
    sig = {"symbol": "SPY", "strike": 755.0, "side": "CALL",
           "expiry_date": date(2027, 6, 17), "tags": ["day_trade"]}
    assert dc._suspicious_long_dte(sig, date(2026, 7, 16)) is not None


def test_suspicious_long_dte_allows_leap_swing():
    # 真 LEAPS："SOFI 20c Jan 15 2027 starter leap swing" tags=['swing']
    sig = {"symbol": "SOFI", "strike": 20.0, "side": "CALL",
           "expiry_date": date(2027, 1, 15), "tags": ["swing"]}
    assert dc._suspicious_long_dte(sig, date(2026, 7, 17)) is None


def test_suspicious_long_dte_allows_short_dte_lotto():
    sig = {"symbol": "AMZN", "strike": 250.0, "side": "CALL",
           "expiry_date": date(2026, 7, 17), "tags": ["lotto", "day_trade"]}
    assert dc._suspicious_long_dte(sig, date(2026, 7, 14)) is None


def test_suspicious_long_dte_no_tags_not_blocked():
    sig = {"symbol": "UPS", "strike": 130.0, "side": "CALL",
           "expiry_date": date(2027, 1, 15), "tags": []}
    assert dc._suspicious_long_dte(sig, date(2026, 7, 7)) is None


# ============ "calls up @ price" 行情评论不再误报 looks-like-signal（7/21） ============

def test_open_attempt_ignores_bare_calls_commentary():
    # 7/21 实测:三件套(SPY + calls + @3.80)全中但其实是行情评论,不该报
    assert dc._looks_like_open_attempt(
        "@everyone\nKC Trades Bot:PDH here on SPY, calls up @ 3.80 🚀💰"
    ) is False
    assert dc._looks_like_open_attempt("SPY calls paying nicely @ 2.50 ✅") is False


def test_open_attempt_still_catches_strike_bearing_signals():
    # strike 紧贴方向词的真开仓仍要报(parser 万一没接住)
    assert dc._looks_like_open_attempt("$NVDA $210 calls $.58 7/22") is True   # $210 calls
    assert dc._looks_like_open_attempt("TSLA 250c 7/11 @ 1.20 没接住") is True  # 750c 紧凑形
    assert dc._looks_like_open_attempt("$AAOI weekly $220 calls for $2.05") is True  # strike 前置
    assert dc._looks_like_open_attempt("$HOOD - 7/24 $125 看涨期权 $1.50") is True   # ZH
