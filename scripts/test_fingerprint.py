"""验证 fingerprint 去重逻辑"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.listener.discord_client import _is_duplicate_signal, _signal_fps
from src.listener import discord_client as dc
from datetime import datetime, timedelta


def reset():
    _signal_fps.clear()


# Case 1: NNE 双发（间隔 8s 实测场景）
print("=== Case 1: NNE 双发 ===")
reset()
sig = {"symbol": "NNE", "side": "CALL", "strike": 31.0, "expiry_date": "2026-05-15"}
r1 = _is_duplicate_signal(sig)
r2 = _is_duplicate_signal(sig)
print(f"1st: {r1}")
print(f"2nd: {r2}")
assert r1[0] is False, f"1st should be False, got {r1}"
assert r2[0] is True,  f"2nd should be True, got {r2}"

# Case 2: MRVL 价格修正 $1.10 → $.95（同指纹）
print("\n=== Case 2: MRVL 价格修正 ===")
reset()
sig1 = {"symbol": "MRVL", "side": "CALL", "strike": 185.0, "expiry_date": "2026-05-15", "price": 1.10}
sig2 = {"symbol": "MRVL", "side": "CALL", "strike": 185.0, "expiry_date": "2026-05-15", "price": 0.95}
r1 = _is_duplicate_signal(sig1)
r2 = _is_duplicate_signal(sig2)
print(f"$1.10: {r1}")
print(f"$.95:  {r2}")
assert r1[0] is False
assert r2[0] is True, "价格不同但 fingerprint 同，应被拦"

# Case 3: 不同 strike 不拦
print("\n=== Case 3: 不同 strike ===")
reset()
sig1 = {"symbol": "TSLA", "side": "CALL", "strike": 460.0, "expiry_date": "2026-05-15"}
sig2 = {"symbol": "TSLA", "side": "CALL", "strike": 465.0, "expiry_date": "2026-05-15"}
r1 = _is_duplicate_signal(sig1)
r2 = _is_duplicate_signal(sig2)
print(f"460: {r1}")
print(f"465: {r2}")
assert r1[0] is False
assert r2[0] is False, "不同 strike 不应拦"

# Case 4: CALL vs PUT 不拦
print("\n=== Case 4: CALL vs PUT ===")
reset()
sig1 = {"symbol": "SPY", "side": "CALL", "strike": 700.0, "expiry_date": "2026-06-15"}
sig2 = {"symbol": "SPY", "side": "PUT",  "strike": 700.0, "expiry_date": "2026-06-15"}
r1 = _is_duplicate_signal(sig1)
r2 = _is_duplicate_signal(sig2)
print(f"CALL: {r1}")
print(f"PUT:  {r2}")
assert r1[0] is False
assert r2[0] is False, "CALL 和 PUT 不应互相拦"

# Case 5: 不同 expiry 不拦（同 strike 不同周）
print("\n=== Case 5: 不同 expiry ===")
reset()
sig1 = {"symbol": "AAPL", "side": "CALL", "strike": 200.0, "expiry_date": "2026-06-19"}
sig2 = {"symbol": "AAPL", "side": "CALL", "strike": 200.0, "expiry_date": "2026-06-26"}
r1 = _is_duplicate_signal(sig1)
r2 = _is_duplicate_signal(sig2)
print(f"6/19: {r1}")
print(f"6/26: {r2}")
assert r1[0] is False
assert r2[0] is False

# Case 6: 窗口过期后不拦
print("\n=== Case 6: 窗口过期（手动改时间戳）===")
reset()
sig = {"symbol": "QQQ", "side": "PUT", "strike": 500.0, "expiry_date": "2026-06-19"}
r1 = _is_duplicate_signal(sig)
print(f"1st: {r1}")
assert r1[0] is False

# 手动把刚记录的时间戳改成 6 分钟前
fp_key = list(dc._signal_fps.keys())[0]
dc._signal_fps[fp_key] = datetime.now() - timedelta(minutes=6)

r2 = _is_duplicate_signal(sig)
print(f"6min later: {r2}")
assert r2[0] is False, "过 5min 窗口后应放行"

# Case 7: 跨频道同信号（验证 fingerprint 不含 channel）
print("\n=== Case 7: 跨频道同信号 ===")
reset()
# 模拟 KC 主频道 + enrich 翻译版同时发 MRVL
sig_kc      = {"symbol": "MRVL", "side": "CALL", "strike": 190.0, "expiry_date": "2026-05-15"}
sig_enrich  = {"symbol": "MRVL", "side": "CALL", "strike": 190.0, "expiry_date": "2026-05-15"}
r1 = _is_duplicate_signal(sig_kc)
r2 = _is_duplicate_signal(sig_enrich)
print(f"KC:     {r1}")
print(f"enrich: {r2}")
assert r1[0] is False
assert r2[0] is True, "跨频道转发也应拦"

print("\n✅ All 7 fingerprint cases passed")