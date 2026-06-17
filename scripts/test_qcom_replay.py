"""回放 6/16 QCOM 信号，验证 expiry 修复到 6/18。"""
from datetime import date
from src.parser.signal_parser import parse_signal

# 真实信号文本（enrich 6/16 ET 09:59）
raw = "enrich:\n$QCOM - weekly $245 calls $2.25\n\n@everyone $alert"

# ET 周一（msg_ts 模拟 listener 传入的值）
sig = parse_signal(raw, msg_ts=date(2026, 6, 15))

print(f"raw: {raw[:60]}")
print(f"parsed: {sig}")

assert sig is not None, "应解析成功"
assert sig.get("skip") is None, "不应是 skip"
assert sig["symbol"] == "QCOM"
assert sig["strike"] == 245.0
assert sig["side"] == "CALL"
assert sig["price"] == 2.25
assert sig["expiry_date"] == date(2026, 6, 18), \
    f"expected 6/18 (Juneteenth 前移), got {sig['expiry_date']}"

print(f"\n✅ QCOM expiry = {sig['expiry_date']} (期望 2026-06-18 Thu)")

# 再测 IREN（msg_ts 也是 6/15 周一 ET）
raw2 = "enrich:\nPotential run into EOD - $IREN weekly $65 calls for $.93\n\n@everyone"
sig2 = parse_signal(raw2, msg_ts=date(2026, 6, 15))
print(f"\nIREN parsed: {sig2}")
assert sig2["symbol"] == "IREN"
assert sig2["strike"] == 65.0
assert sig2["expiry_date"] == date(2026, 6, 18)
print(f"✅ IREN expiry = {sig2['expiry_date']}")

# 测一个非假日的周五，确保不被错误前移
raw3 = "$SPY weekly $500 calls $1.50"
sig3 = parse_signal(raw3, msg_ts=date(2026, 6, 22))  # 周一
print(f"\nSPY parsed expiry = {sig3['expiry_date']}")
assert sig3["expiry_date"] == date(2026, 6, 26), "6/26 周五不是假日，应保留"
print(f"✅ SPY weekly 6/22 周一 → expiry 6/26 周五（无前移）")

print("\n🎉 信号回放全部通过")