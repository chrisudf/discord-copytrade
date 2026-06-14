"""Real-world signal parser test"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.parser.signal_parser import parse_signal, detect_action

samples = [
    # KC Trades real samples
    "AMZN 260c 7/17 @ 2.64 swing",
    "MSFT 440c 7/17 @ 3.05 swing, I like the 420c 7/17 @ 6.05 if you want a higher delta",
    "SMH 530p 6/18 lotto swing @ 8.20",
    "CCL 28c 6/18, safer swing, filled @ 1.10 on 28c and 1.40 on 30c",
    "SPIR 20c 6/18 @ 1.90",
    # Old format
    "Lotto $IREN 0DTE $60 calls $.68",
    "$alert $TSLA 1DTE $250 puts $1.20",
    # Year rollover test
    "AAPL 200c 1/15 @ 3.00",
    # Should fail
    "garbage message no signal",
    "PPI data tomorrow",
]

for s in samples:
    print("\n" + "=" * 70)
    print(f"INPUT:  {s}")
    print(f"ACTION: {detect_action(s)}")
    result = parse_signal(s)
    if result is None:
        print("OUTPUT: None")
    elif isinstance(result, list):
        print(f"OUTPUT: {len(result)} signals")
        for i, sig in enumerate(result, 1):
            print(f"  [{i}] {sig['symbol']} {sig['strike']}{sig['side'][0]} "
                  f"{sig['expiry']}({sig['expiry_date']}) @ ${sig['price']} "
                  f"tags={sig['tags']}")
    else:
        print(f"OUTPUT: {result['symbol']} {result['strike']}{result['side'][0]} "
              f"{result['expiry']}({result['expiry_date']}) @ ${result['price']} "
              f"tags={result['tags']}")