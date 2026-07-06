"""listener _handle_close_signal 端到端测试

补 6/18 IWM 事件暴露的两个 bug：
- broker 拒单后误报 "no matching open positions"（原 any_executed 没 set）
"""
import os
from datetime import date, datetime
from unittest.mock import patch

import pytest

from src.storage import positions_db
from src.listener import discord_client


def _uniq_code(prefix: str) -> str:
    return f"US.{prefix}{datetime.now().strftime('%H%M%S%f')}C001000"


@pytest.mark.asyncio
async def test_broker_reject_does_not_report_no_matching():
    """broker 拒单时 TG 应该收到 'Sell rejected'，不该再收到 'no matching open positions'。"""
    os.environ["DRY_RUN"] = "true"
    code = _uniq_code("BRJ")
    positions_db.open_or_add(
        option_code=code, symbol="REJX", strike=220.0, side="CALL",
        expiry=date(2026, 6, 30), qty=3, fill_price=2.0,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ut", msg_id="m1",
    )
    discord_client._close_fps.clear()

    notifications = []

    async def capture(msg):
        notifications.append(msg)

    with patch.object(discord_client, "_safe_notify", side_effect=capture), \
         patch.object(discord_client, "place_sell_order",
                      return_value={"success": False,
                                    "message": "MOOMOO_ACC_ID 未配置",
                                    "order_id": None, "code": code,
                                    "qty": 1, "price": 1.9}):
        await discord_client._handle_close_signal(
            "KC Trades Bot:trimmed REJX @ 2.45", msg_id=9999,
        )

    text = "\n".join(notifications)
    assert "Sell rejected" in text or "rejected" in text.lower(), (
        "应有 broker 拒单告警", text,
    )
    assert "no matching" not in text.lower(), (
        "broker 拒单后不应再报 'no matching open positions'", text,
    )

    # 仓位仍然是 OPEN（broker 没成交）
    pos = positions_db.get(code)
    assert pos["status"] == "OPEN"
    assert pos["qty_remaining"] == 3

    positions_db.record_close(code, 3, 2.0, "manual", note="ut cleanup")


# === 6/30 strike-aware close hint regression ===

@pytest.mark.asyncio
async def test_strike_hint_mismatch_skips_close():
    """KC 喊关 TSLA 420c，但我们持仓是 TSLA 425c → 跳过卖单，发 TG '不匹配' 提示"""
    os.environ["DRY_RUN"] = "true"
    code = _uniq_code("STK")
    # 持仓：TSLA 425c
    positions_db.open_or_add(
        option_code=code, symbol="TSLAA", strike=425.0, side="CALL",
        expiry=date(2026, 7, 1), qty=1, fill_price=1.67,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ut", msg_id="m_strike_1",
    )
    discord_client._close_fps.clear()

    notifications = []

    async def capture(msg):
        notifications.append(msg)

    sell_called = []

    async def fake_sell(*args, **kwargs):
        sell_called.append((args, kwargs))
        return {"success": True}

    with patch.object(discord_client, "_safe_notify", side_effect=capture), \
         patch.object(discord_client, "place_sell_order", side_effect=fake_sell):
        # KC 平 420c，不是 425c
        await discord_client._handle_close_signal(
            "trimmed TSLAA 420c @ 15.35", msg_id=88888,
        )

    text = "\n".join(notifications)
    # 应有"不匹配"告警，不应该卖
    assert "strike" in text.lower() and "不匹配" in text, (
        "应有 strike 不匹配的 TG 告警", text,
    )
    assert sell_called == [], "strike 不匹配时不应触发卖单"

    # 仓位仍 OPEN
    pos = positions_db.get(code)
    assert pos["status"] == "OPEN"
    assert pos["qty_remaining"] == 1

    positions_db.record_close(code, 1, 1.67, "manual", note="ut cleanup")


@pytest.mark.asyncio
async def test_strike_hint_match_executes_close():
    """KC 喊关 TSLA 425c，持仓恰好 TSLA 425c → 正常卖单"""
    os.environ["DRY_RUN"] = "true"
    code = _uniq_code("STM")
    positions_db.open_or_add(
        option_code=code, symbol="TSLAB", strike=425.0, side="CALL",
        expiry=date(2026, 7, 1), qty=1, fill_price=1.67,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ut", msg_id="m_strike_2",
    )
    discord_client._close_fps.clear()

    sell_called = []

    def fake_sell(*args, **kwargs):
        sell_called.append((args, kwargs))
        return {"success": True, "order_id": "X", "code": code, "qty": 1, "price": 14.58}

    async def noop_notify(msg):
        pass

    with patch.object(discord_client, "_safe_notify", side_effect=noop_notify), \
         patch.object(discord_client, "place_sell_order", side_effect=fake_sell):
        await discord_client._handle_close_signal(
            "全部平仓 TSLAB 425c 持仓 @ 15.35", msg_id=77777,
        )

    # strike 匹配应该触发卖单
    assert len(sell_called) == 1, f"strike 匹配应触发卖单，实际: {sell_called}"

    # 收尾
    positions_db.record_close(code, 1, 14.58, "manual", note="ut cleanup")


@pytest.mark.asyncio
async def test_no_strike_hint_keeps_legacy_symbol_only_behavior():
    """普通 trim 信号（无 strike）→ 按 symbol 关全部，保留旧行为。

    注：qty=2 而非 1，因为 v2 起 1 张 + trim<100% 会触发 runner-preserve 跳过
    （见 test_calc_qty_to_sell_single_contract_runner_preserve）。这里测的是
    strike-filter 的旧回落行为，跟 runner 规则解耦。
    """
    os.environ["DRY_RUN"] = "true"
    code = _uniq_code("LEG")
    positions_db.open_or_add(
        option_code=code, symbol="SPYZ", strike=748.0, side="CALL",
        expiry=date(2026, 7, 6), qty=2, fill_price=2.59,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ut", msg_id="m_strike_3",
    )
    discord_client._close_fps.clear()

    sell_called = []

    def fake_sell(*args, **kwargs):
        sell_called.append((args, kwargs))
        return {"success": True, "order_id": "Y", "code": code, "qty": 1, "price": 2.71}

    async def noop_notify(msg):
        pass

    with patch.object(discord_client, "_safe_notify", side_effect=noop_notify), \
         patch.object(discord_client, "place_sell_order", side_effect=fake_sell):
        # 无 strike 的 trim
        await discord_client._handle_close_signal(
            "trimmed SPYZ @ 2.85", msg_id=66666,
        )

    # 旧行为：symbol 匹配即关（qty=2 时 33% ceil = 1 张，卖单会挂）
    assert len(sell_called) == 1, "无 strike hint 应保持旧 symbol-only 行为"

    positions_db.record_close(code, 2, 2.71, "manual", note="ut cleanup")


# === 7/6 runner-preserve TG 文案回归 ===

@pytest.mark.asyncio
async def test_runner_preserve_reports_truthfully():
    """1 张持仓 + trim 信号 → 跳过卖单，TG 必须说 runner-preserve，
    不能落到 'no matching open positions' 兜底（7/6 IBM 两次实锤误导）。"""
    os.environ["DRY_RUN"] = "true"
    code = _uniq_code("RUN")
    positions_db.open_or_add(
        option_code=code, symbol="RUNX", strike=305.0, side="CALL",
        expiry=date(2026, 7, 10), qty=1, fill_price=2.11,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ut", msg_id="m_run_1",
    )
    discord_client._close_fps.clear()

    notifications = []

    async def capture(msg):
        notifications.append(msg)

    sell_called = []

    def fake_sell(*args, **kwargs):
        sell_called.append((args, kwargs))
        return {"success": True}

    with patch.object(discord_client, "_safe_notify", side_effect=capture), \
         patch.object(discord_client, "place_sell_order", side_effect=fake_sell):
        await discord_client._handle_close_signal(
            "trimmed RUNX @ 2.85", msg_id=55555,
        )

    assert sell_called == [], "runner-preserve 不应触发卖单"
    text = "\n".join(notifications)
    assert "runner" in text.lower(), ("TG 应说明 runner-preserve", text)
    assert "no matching" not in text.lower(), (
        "不应再报误导性的 'no matching open positions'", text,
    )
    # 仓位原样保留
    pos = positions_db.get(code)
    assert pos["status"] == "OPEN"
    assert pos["qty_remaining"] == 1


# === 7/6 add-on 检测 ===

def _open_addon_pos(symbol: str) -> str:
    code = _uniq_code("ADO")
    positions_db.open_or_add(
        option_code=code, symbol=symbol, strike=742.0, side="PUT",
        expiry=date(2026, 7, 13), qty=1, fill_price=2.57,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ut", msg_id="m_addon",
    )
    return code


def test_addon_detected_en_bare_ticker():
    """7/6 00:31 'small add SPY @ 1.86' —— 裸 ticker + 持仓白名单 → 命中"""
    _open_addon_pos("ADX1")
    got = discord_client._looks_like_addon_attempt(
        "KC Trades Bot:small add ADX1 @ 1.86, my stop is right above this zone near 750"
    )
    assert got == "ADX1"


def test_addon_detected_zh():
    """ZH 版 '小加仓SPY @ 1.86'（汉字-字母边界无 \\b，用 lookaround）"""
    _open_addon_pos("ADX2")
    got = discord_client._looks_like_addon_attempt(
        "KC Trades Bot: 小加仓ADX2 @ 1.86, 止损位于750附近区域上方。"
    )
    assert got == "ADX2"


def test_addon_not_held_symbol_returns_none():
    """add + @price 但 symbol 不在持仓白名单 → None（新开仓评论不提醒）"""
    got = discord_client._looks_like_addon_attempt(
        "small add ZZQQ @ 2.10 looks good here"
    )
    assert got is None


def test_addon_requires_price():
    """持仓 symbol + add 但没喊价 → None（'can add more later' 类评论）"""
    _open_addon_pos("ADX3")
    got = discord_client._looks_like_addon_attempt(
        "might add more ADX3 if we reclaim the level"
    )
    assert got is None


def test_addon_requires_add_keyword():
    """持仓 symbol + @price 但无 add 词 → None（普通评论）"""
    _open_addon_pos("ADX4")
    got = discord_client._looks_like_addon_attempt(
        "ADX4 holding the line @ 1.86 nicely"
    )
    assert got is None


# === CLOSE 指纹登记时机回归（review 0005）===

@pytest.mark.asyncio
async def test_failed_close_allows_bilingual_retry():
    """卖单失败时不登记指纹 → 1-3s 后到达的另一语言版本可以正常重试。

    回归：之前查即登记，ZH 版先到但 broker 拒单后，EN 版被当 dup 拦掉，
    天然的重试机会丢失。
    """
    os.environ["DRY_RUN"] = "true"
    code = _uniq_code("RTY")
    positions_db.open_or_add(
        option_code=code, symbol="RTYX", strike=100.0, side="CALL",
        expiry=date(2026, 7, 10), qty=2, fill_price=2.0,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ut", msg_id="m_rty_1",
    )
    discord_client._close_fps.clear()

    async def noop_notify(msg):
        pass

    calls = []

    def failing_then_ok_sell(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return {"success": False, "message": "transient broker error",
                    "order_id": None, "code": code, "qty": 1, "price": 1.9}
        return {"success": True, "order_id": "RTY_OK", "code": code,
                "qty": 1, "price": 1.9}

    with patch.object(discord_client, "_safe_notify", side_effect=noop_notify), \
         patch.object(discord_client, "place_sell_order", side_effect=failing_then_ok_sell):
        # 第一发（模拟 ZH 先到）：broker 拒单 → 不应登记指纹
        await discord_client._handle_close_signal("减仓 RTYX @ 2.45", msg_id=1111)
        # 第二发（模拟 EN 双发）：应被当作重试执行，而不是 dup 拦掉
        await discord_client._handle_close_signal("trimmed RTYX @ 2.45", msg_id=1112)

    assert len(calls) == 2, f"第二发应重试卖出，实际 broker 调用: {len(calls)}"

    # 第二发成功后指纹已登记 → 第三发同信号应被 dup 拦掉
    with patch.object(discord_client, "_safe_notify", side_effect=noop_notify), \
         patch.object(discord_client, "place_sell_order", side_effect=failing_then_ok_sell):
        await discord_client._handle_close_signal("trimmed RTYX @ 2.45", msg_id=1113)
    assert len(calls) == 2, "成功后同指纹信号应被 dup 拦截"

    positions_db.record_close(code, 2, 2.0, "manual", note="ut cleanup")


@pytest.mark.asyncio
async def test_deterministic_skip_registers_fp_no_twin_spam():
    """确定性跳过（runner-preserve）也登记指纹 → 双语孪生不会重复刷 TG。

    这是对 0005 "只在成功后登记" 的本地精修：runner-preserve / 无价格参照
    这类结果是确定性的，孪生重试只会重复告警，应照常去重。
    """
    os.environ["DRY_RUN"] = "true"
    code = _uniq_code("DSK")
    positions_db.open_or_add(
        option_code=code, symbol="DSKX", strike=100.0, side="CALL",
        expiry=date(2026, 7, 10), qty=1, fill_price=2.0,  # qty=1 → runner-preserve
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ut", msg_id="m_dsk_1",
    )
    discord_client._close_fps.clear()

    notifications = []

    async def capture(msg):
        notifications.append(msg)

    with patch.object(discord_client, "_safe_notify", side_effect=capture), \
         patch.object(discord_client, "place_sell_order") as sell_mock:
        await discord_client._handle_close_signal("trimmed DSKX @ 2.45", msg_id=2221)
        await discord_client._handle_close_signal("减仓 DSKX @ 2.45", msg_id=2222)

    sell_mock.assert_not_called()
    runner_alerts = [n for n in notifications if "runner" in n.lower()]
    assert len(runner_alerts) == 1, (
        f"孪生版本应被指纹去重，只发一条 runner-preserve TG，实际 {len(runner_alerts)}",
        notifications,
    )

    positions_db.record_close(code, 1, 2.0, "manual", note="ut cleanup")
