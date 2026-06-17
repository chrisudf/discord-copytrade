"""验证假日表 + adjust_to_trading_day 行为。"""
from datetime import date
from src.parser.holidays import is_trading_day, adjust_to_trading_day

print("=" * 60)
print("假日表测试")
print("=" * 60)

# Juneteenth 2026
assert not is_trading_day(date(2026, 6, 19)), "6/19/26 Juneteenth 应非交易日"
assert adjust_to_trading_day(date(2026, 6, 19)) == date(2026, 6, 18), \
    "6/19 → 应前移到 6/18 周四"
print("✅ 2026-06-19 Juneteenth → 6/18 周四")

# 周末
assert not is_trading_day(date(2026, 6, 20)), "周六应非交易日"
assert adjust_to_trading_day(date(2026, 6, 20)) == date(2026, 6, 19) or \
       adjust_to_trading_day(date(2026, 6, 20)) == date(2026, 6, 18), \
    "周六 6/20 → 应找前一交易日"
# 注意 6/20 周六 backward → 6/19 (Juneteenth) → 再 backward → 6/18
assert adjust_to_trading_day(date(2026, 6, 20)) == date(2026, 6, 18), \
    "6/20 周六 backward 应连跳到 6/18（6/19 是假日）"
print("✅ 2026-06-20 Sat → 6/18（连跳 Juneteenth）")

# Good Friday 2026
assert not is_trading_day(date(2026, 4, 3))
assert adjust_to_trading_day(date(2026, 4, 3)) == date(2026, 4, 2)
print("✅ 2026-04-03 Good Friday → 4/2 周四")

# 普通交易日
assert is_trading_day(date(2026, 6, 18))
assert is_trading_day(date(2026, 6, 22))  # Monday
assert adjust_to_trading_day(date(2026, 6, 18)) == date(2026, 6, 18)
print("✅ 普通交易日不变")

# 2027 Juneteenth observed
assert not is_trading_day(date(2027, 6, 18))
assert adjust_to_trading_day(date(2027, 6, 18)) == date(2027, 6, 17)
print("✅ 2027-06-18 Juneteenth observed → 6/17")

print("\n🎉 假日表全部通过")