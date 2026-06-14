"""
紧急重置当日风控
用法：
  python scripts/reset_daily_limit.py          # 只清熔断标记，保留订单记录（推荐）
  python scripts/reset_daily_limit.py --hard   # 清熔断 + 清订单记录（慎用）
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
from src.risk.risk_manager import (
    clear_circuit_breaker_only,
    manual_reset_today,
    get_daily_stats,
    is_circuit_broken,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hard", action="store_true",
                        help="清除熔断+订单记录（默认只清熔断）")
    args = parser.parse_args()
    
    stats_before = get_daily_stats()
    print(f"\n当前状态:")
    print(f"  交易日: {stats_before['trading_date']}")
    print(f"  已下单: {stats_before['order_count']} 笔")
    print(f"  累计成本: ${stats_before['total_cost']}")
    print(f"  熔断状态: {'🚨 已熔断' if stats_before['circuit_broken'] else '✅ 正常'}")
    
    confirm = input("\n确认重置? (yes/no): ").strip().lower()
    if confirm != "yes":
        print("取消")
        return
    
    if args.hard:
        result = manual_reset_today()
        print(f"\n🔴 硬重置完成:")
        print(f"  清除熔断: {result['circuit_breaker_cleared']} 条")
        print(f"  清除订单: {result['orders_cleared']} 条")
    else:
        cleared = clear_circuit_breaker_only()
        print(f"\n🟢 软重置完成: 熔断标记 {'已清除' if cleared else '本来就没有'}")
        print(f"  订单记录保留（继续累计当日成本和次数）")
    
    stats_after = get_daily_stats()
    print(f"\n重置后:")
    print(f"  熔断状态: {'🚨 已熔断' if stats_after['circuit_broken'] else '✅ 正常'}")
    print(f"  剩余次数: {stats_after['remaining_orders']}")
    print(f"  剩余成本: ${stats_after['remaining_cost']}")


if __name__ == "__main__":
    main()