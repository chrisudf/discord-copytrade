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
