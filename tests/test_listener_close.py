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
