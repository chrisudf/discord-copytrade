"""
风控模块单元测试
覆盖 4 道防线 + 熔断 + 重置
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.risk.risk_manager import (
    check_order, record_order, get_daily_stats,
    is_circuit_broken, manual_reset_today,
    MAX_PRICE_PER_CONTRACT, MAX_COST_PER_ORDER,
    MAX_DAILY_COST, MAX_DAILY_ORDERS,
)


def hr(t=""):
    print("\n" + "=" * 60)
    if t:
        print(t)
        print("=" * 60)


def test_pass():
    hr("Test 1: 正常订单应该通过")
    r = check_order(price=1.5, qty=1, symbol="AMZN")
    assert r.passed, f"应该通过但被拦截: {r.reason}"
    print(f"  ✅ PASS  (price=1.5, qty=1, cost=$150)")


def test_price_limit():
    hr("Test 2: 单张价格超限")
    r = check_order(price=6.0, qty=1, symbol="AMZN")
    assert not r.passed, "应该拦截但通过了"
    assert r.reason == "单张合约价格超限"
    print(f"  ✅ PASS  reason={r.reason} | {r.detail}")


def test_cost_per_order_limit():
    hr("Test 3: 单笔成本超限")
    # price=3, qty=2, cost=600 > 500
    r = check_order(price=3.0, qty=2, symbol="AMZN")
    assert not r.passed, "应该拦截但通过了"
    assert r.reason == "单笔订单成本超限"
    print(f"  ✅ PASS  reason={r.reason} | {r.detail}")


def test_daily_order_count():
    hr("Test 4: 当日下单次数达上限")
    manual_reset_today()  # 先清空
    
    # 连下 MAX_DAILY_ORDERS 次小单
    for i in range(MAX_DAILY_ORDERS):
        r = check_order(price=0.5, qty=1, symbol=f"TEST{i}")
        assert r.passed, f"第 {i+1} 次应该通过"
        record_order(price=0.5, qty=1, symbol=f"TEST{i}")
    
    print(f"  已下 {MAX_DAILY_ORDERS} 单，下一单应该触发熔断")
    
    # 第 N+1 次应该被拦截
    r = check_order(price=0.5, qty=1, symbol="OVERFLOW")
    assert not r.passed
    assert r.block_rest_of_day
    print(f"  ✅ PASS  reason={r.reason}")
    print(f"        熔断状态: {is_circuit_broken()}")
    
    # 后续任何订单都该被拦截
    r2 = check_order(price=0.1, qty=1, symbol="BLOCKED")
    assert not r2.passed
    assert r2.reason == "当日已熔断"
    print(f"  ✅ 熔断后所有订单全部拦截: {r2.reason}")


def test_daily_cost_limit():
    hr("Test 5: 当日累计成本达上限")
    manual_reset_today()
    
    # 下 4 笔 $500，累计 $2000
    for i in range(4):
        r = check_order(price=5.0, qty=1, symbol=f"BIG{i}")
        assert r.passed, f"第 {i+1} 笔 ($500) 应该通过"
        record_order(price=5.0, qty=1, symbol=f"BIG{i}")
    
    stats = get_daily_stats()
    print(f"  已下 4 笔 $500，累计 ${stats['total_cost']}")
    
    # 第 5 笔会让累计超过 $2000
    r = check_order(price=5.0, qty=1, symbol="OVERFLOW")
    assert not r.passed
    assert r.block_rest_of_day
    print(f"  ✅ PASS  reason={r.reason}")


def test_reset():
    hr("Test 6: 手动重置")
    assert is_circuit_broken(), "应该处于熔断状态（继承上一个测试）"
    
    result = manual_reset_today()
    print(f"  重置结果: {result}")
    
    assert not is_circuit_broken(), "重置后不应该熔断"
    
    r = check_order(price=1.0, qty=1, symbol="AFTER_RESET")
    assert r.passed
    print(f"  ✅ 重置后正常通过")


def test_stats():
    hr("Test 7: 统计查询")
    manual_reset_today()
    record_order(price=1.5, qty=1, symbol="A")
    record_order(price=2.0, qty=1, symbol="B")
    
    stats = get_daily_stats()
    print(f"  统计: {stats}")
    assert stats["order_count"] == 2
    assert stats["total_cost"] == 350  # 150 + 200
    assert stats["remaining_orders"] == MAX_DAILY_ORDERS - 2
    assert stats["remaining_cost"] == MAX_DAILY_COST - 350
    print(f"  ✅ 统计正确")


def cleanup():
    hr("清理测试数据")
    result = manual_reset_today()
    print(f"  清理结果: {result}")


if __name__ == "__main__":
    print(f"\n风控配置:")
    print(f"  MAX_PRICE_PER_CONTRACT = ${MAX_PRICE_PER_CONTRACT}")
    print(f"  MAX_COST_PER_ORDER     = ${MAX_COST_PER_ORDER}")
    print(f"  MAX_DAILY_COST         = ${MAX_DAILY_COST}")
    print(f"  MAX_DAILY_ORDERS       = {MAX_DAILY_ORDERS}")
    
    test_pass()
    test_price_limit()
    test_cost_per_order_limit()
    test_daily_order_count()
    test_daily_cost_limit()
    test_reset()
    test_stats()
    cleanup()
    
    hr("✅ 所有测试通过")