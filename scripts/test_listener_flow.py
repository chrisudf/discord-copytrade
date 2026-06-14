"""Simulate end-to-end flow without connecting to Discord"""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.parser.signal_parser import parse_signal, detect_action
from src.broker.moomoo_client import place_order
from src.storage.logger_db import log_raw_signal, log_order
from datetime import datetime


async def simulate(raw_msg: str, msg_id: str = "TEST_001"):
    print(f"\n{'='*70}\nSimulating: {raw_msg}")
    t0 = datetime.now()
    log_raw_signal(msg_id, "test_user", raw_msg, t0)

    action = detect_action(raw_msg)
    print(f"Action: {action}")
    if action == "CLOSE":
        print("-> Skip (CLOSE TODO)")
        return

    signal = parse_signal(raw_msg)
    if not signal:
        print("-> Parse failed")
        return

    if isinstance(signal, list):
        print(f"-> {len(signal)} signals, taking first")
        signal = signal[0]

    result = await place_order(signal)
    log_order(msg_id, signal, result)
    print(f"-> Order result: {result}")


async def main():
    samples = [
        ("AMZN 260c 7/17 @ 2.64 swing", "T001"),
        ("MSFT 440c 7/17 @ 3.05 swing, I like the 420c 7/17 @ 6.05", "T002"),
        ("Lotto $IREN 0DTE $60 calls $.68", "T003"),
        ("closed AMZN 260c for +50%", "T004"),
        ("garbage message", "T005"),
    ]
    for raw, mid in samples:
        await simulate(raw, mid)


if __name__ == "__main__":
    asyncio.run(main())