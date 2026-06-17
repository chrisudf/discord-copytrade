"""验证 parser intentional skip 返回 dict 而不是 None。"""
from datetime import date
from src.parser.signal_parser import parse_signal

# 1. holding skip
sig = parse_signal("$QCOM holding for now - LOD $223 is a good risk stop",
                   msg_ts=date(2026, 6, 15))
print(f"holding: {sig}")
assert isinstance(sig, dict) and sig.get("skip") == "holding_or_remaining"
print("✅ holding → {'skip': 'holding_or_remaining'}")

# 2. price range skip
sig = parse_signal("$SPY $500 calls $1.00-$1.50", msg_ts=date(2026, 6, 15))
print(f"price_range: {sig}")
assert isinstance(sig, dict) and sig.get("skip") == "price_range"
print("✅ price range → {'skip': 'price_range'}")

# 3. 真·解析失败 = None
sig = parse_signal("just some random gibberish nothing useful here at all",
                   msg_ts=date(2026, 6, 15))
print(f"garbage: {sig}")
assert sig is None
print("✅ 无法解析的内容 → None（保留警告路径）")

# 4. 正常成功 = dict 无 skip key
sig = parse_signal("$QCOM weekly $245 calls $2.25", msg_ts=date(2026, 6, 15))
print(f"normal: {sig['symbol']} {sig['strike']}")
assert sig.get("skip") is None
assert sig["symbol"] == "QCOM"
print("✅ 正常信号 → dict 无 skip key")

print("\n🎉 skip 路径全部通过")