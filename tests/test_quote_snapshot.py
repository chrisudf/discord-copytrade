"""quote_ctx / get_last_prices / validate_option_codes 行为锁定。

不依赖真实 moomoo 连接：mock OpenQuoteContext.get_market_snapshot。
"""
import os
import time
from unittest.mock import MagicMock, patch
import pytest

import src.broker.moomoo_client as bc


@pytest.fixture(autouse=True)
def _reset_quote_state(monkeypatch):
    """每个测试独立 quote_ctx / backoff，避免前一个测试污染状态。"""
    # 清掉 quote 单例和 backoff
    monkeypatch.setattr(bc, "_quote_ctx", None)
    monkeypatch.setattr(bc, "_quote_backoff_until", 0.0)
    # 强制走真盘路径
    monkeypatch.setattr(bc, "_is_dry_run", lambda: False)
    # SDK_AVAILABLE 假设为 True
    monkeypatch.setattr(bc, "SDK_AVAILABLE", True)
    yield


def _make_snapshot_row(code, last_price, update_offset_s=0):
    """构造一行 mock snapshot 数据。update_offset_s 负数代表多久之前更新过。

    用 UTC 时间因为 broker 里 `pd.to_datetime(s).timestamp()` 把 naive
    字符串当 UTC（pandas 行为，跟 Python datetime 不同）。生产数据如果
    moomoo 返回的是本地/ET 时间，需要 broker 端 reconvert——见同名 TODO。
    """
    import pandas as pd
    ts = pd.Timestamp.utcnow().tz_localize(None) + pd.Timedelta(seconds=update_offset_s)
    return {
        "code": code,
        "last_price": last_price,
        "update_time": ts.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _mock_ctx_returning(snapshot_rows, ret=None):
    """构造一个 mock OpenQuoteContext，其 get_market_snapshot 返回指定 rows。"""
    import pandas as pd
    df = pd.DataFrame(snapshot_rows) if snapshot_rows else pd.DataFrame()
    ctx = MagicMock()
    ctx.get_market_snapshot.return_value = (ret if ret is not None else bc.RET_OK, df)
    return ctx


# ===== get_last_prices =====

def test_get_last_prices_normal(monkeypatch):
    rows = [
        _make_snapshot_row("US.AAPL260117C200000", 5.20),
        _make_snapshot_row("US.NVDA260117P800000", 12.30),
    ]
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: _mock_ctx_returning(rows))
    out = bc.get_last_prices(["US.AAPL260117C200000", "US.NVDA260117P800000"])
    assert out["US.AAPL260117C200000"] == 5.20
    assert out["US.NVDA260117P800000"] == 12.30


def test_get_last_prices_stale_filtered(monkeypatch):
    """update_time > QUOTE_FRESHNESS_SEC 旧的应返回 None"""
    rows = [_make_snapshot_row("US.AAPL", 5.20, update_offset_s=-300)]  # 5 分钟前
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: _mock_ctx_returning(rows))
    out = bc.get_last_prices(["US.AAPL"])
    assert out["US.AAPL"] is None  # 太旧


def test_get_last_prices_zero_price_filtered(monkeypatch):
    """last_price=0 或 NaN 视为没成交，返回 None"""
    import pandas as pd
    rows = [
        _make_snapshot_row("US.A", 0),
        _make_snapshot_row("US.B", float("nan")),
        _make_snapshot_row("US.C", 1.50),
    ]
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: _mock_ctx_returning(rows))
    out = bc.get_last_prices(["US.A", "US.B", "US.C"])
    assert out["US.A"] is None
    assert out["US.B"] is None
    assert out["US.C"] == 1.50


def test_get_last_prices_empty_input():
    out = bc.get_last_prices([])
    assert out == {}


def test_get_last_prices_snapshot_failure_returns_all_none(monkeypatch):
    """ret != RET_OK 时全部 None"""
    monkeypatch.setattr(
        bc, "_get_quote_ctx",
        lambda: _mock_ctx_returning([], ret=-1),
    )
    out = bc.get_last_prices(["US.X", "US.Y"])
    assert out == {"US.X": None, "US.Y": None}


def test_get_last_prices_exception_resets_ctx(monkeypatch):
    """SDK 抛异常 → reset ctx → 返回全 None"""
    ctx = MagicMock()
    ctx.get_market_snapshot.side_effect = RuntimeError("disconnected")
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: ctx)
    reset_called = []
    monkeypatch.setattr(bc, "_reset_quote_ctx", lambda: reset_called.append(True))

    out = bc.get_last_prices(["US.X"])
    assert out == {"US.X": None}
    assert reset_called == [True]


def test_get_last_prices_quota_triggers_backoff(monkeypatch):
    """ret != RET_OK 含 'quota' 触发 60s backoff"""
    monkeypatch.setattr(
        bc, "_get_quote_ctx",
        lambda: _mock_ctx_returning([], ret=-1),
    )
    # 让 get_market_snapshot 返回 quota 错误
    ctx = MagicMock()
    ctx.get_market_snapshot.return_value = (-1, "request quota exceeded")
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: ctx)

    out = bc.get_last_prices(["US.X"])
    assert out == {"US.X": None}
    assert bc._quote_backoff_until > time.monotonic() + 30  # 至少 30s 后


def test_get_last_prices_dry_run_uses_mock_env(monkeypatch):
    monkeypatch.setattr(bc, "_is_dry_run", lambda: True)
    monkeypatch.setenv("MOCK_LAST_PRICE_US.A", "9.99")
    monkeypatch.setenv("MOCK_LAST_PRICE", "1.23")
    out = bc.get_last_prices(["US.A", "US.B"])
    assert out["US.A"] == 9.99
    assert out["US.B"] == 1.23


# ===== get_last_price (single code wrapper) =====

def test_get_last_price_single(monkeypatch):
    rows = [_make_snapshot_row("US.X", 7.50)]
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: _mock_ctx_returning(rows))
    assert bc.get_last_price("US.X") == 7.50


# ===== validate_option_codes =====

def test_validate_option_codes_found(monkeypatch):
    """snapshot 里出现的 code 视为 valid"""
    rows = [_make_snapshot_row("US.AAPL", 5.20)]
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: _mock_ctx_returning(rows))
    out = bc.validate_option_codes(["US.AAPL", "US.MISSING"])
    assert out["US.AAPL"] is True
    assert out["US.MISSING"] is False


def test_validate_option_codes_zero_price_still_valid(monkeypatch):
    """contract 存在但 last_price=0（今日无成交）仍然 valid，能下单"""
    rows = [_make_snapshot_row("US.NEWLISTED", 0)]
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: _mock_ctx_returning(rows))
    out = bc.validate_option_codes(["US.NEWLISTED"])
    assert out["US.NEWLISTED"] is True


def test_validate_option_codes_dry_run_always_true(monkeypatch):
    monkeypatch.setattr(bc, "_is_dry_run", lambda: True)
    out = bc.validate_option_codes(["US.ANYTHING"])
    assert out == {"US.ANYTHING": True}


def test_validate_option_codes_snapshot_fails_all_false(monkeypatch):
    """ret != RET_OK 全部 False（保守 reject，宁可不下也不错下）"""
    ctx = MagicMock()
    ctx.get_market_snapshot.return_value = (-1, "some error")
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: ctx)
    out = bc.validate_option_codes(["US.X"])
    assert out == {"US.X": False}


def test_validate_option_codes_no_permission_falls_back_to_true(monkeypatch):
    """账户没期权行情权限时降级：返全 True 让 broker 自己判，否则会把所有信号 reject"""
    ctx = MagicMock()
    ctx.get_market_snapshot.return_value = (
        -1,
        "No permission to get quotes for US.HOOD260702C108000. "
        "Please check US MarketOptions quote permissions.",
    )
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: ctx)
    out = bc.validate_option_codes(["US.A", "US.B"])
    assert out == {"US.A": True, "US.B": True}


def test_validate_option_codes_empty():
    assert bc.validate_option_codes([]) == {}


# ===== backoff =====

def test_snapshot_respects_backoff(monkeypatch):
    """_quote_backoff_until 大于 now 时不调 SDK，直接返回 backoff 错误"""
    monkeypatch.setattr(bc, "_quote_backoff_until", time.monotonic() + 60)
    ctx_called = []
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: ctx_called.append(True) or _mock_ctx_returning([]))
    ret, msg = bc._snapshot(["US.X"])
    assert ret != bc.RET_OK
    assert "backoff" in str(msg).lower()
    assert ctx_called == []  # 完全没调 SDK
