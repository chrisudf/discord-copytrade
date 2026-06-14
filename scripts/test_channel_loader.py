"""
频道配置加载测试
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config.channel_loader import registry


def main():
    print("=" * 60)
    print("频道配置测试")
    print("=" * 60)
    
    # Test 1: 列出所有启用频道
    enabled = registry.enabled_channel_ids()
    print(f"\n[1] 启用频道数: {len(enabled)}")
    for ch_id in enabled:
        cfg = registry.get(ch_id)
        print(f"    - {cfg.name} ({ch_id})")
    
    # Test 2: 查询 KC 频道
    print(f"\n[2] 查询 KC 频道:")
    kc = registry.get(1443396572581859405)
    assert kc is not None, "KC 频道应该存在"
    print(f"    name={kc.name}")
    print(f"    qty={kc.default_qty}")
    print(f"    max_price={kc.max_price}")
    print(f"    triggers={kc.trigger_user_ids}")
    print(f"    是触发人 1426229538722938880? {kc.is_trigger_user(1426229538722938880)}")
    print(f"    是触发人 9999999999? {kc.is_trigger_user(9999999999)}")
    
    # Test 3: 查询不存在的频道
    print(f"\n[3] 查询不存在频道:")
    fake = registry.get(9999999999)
    print(f"    结果: {fake}")
    assert fake is None
    
    # Test 4: is_monitored
    print(f"\n[4] is_monitored 快速判断:")
    print(f"    1443396572581859405: {registry.is_monitored(1443396572581859405)}")
    print(f"    9999999999: {registry.is_monitored(9999999999)}")
    
    print("\n" + "=" * 60)
    print("✅ 频道配置测试通过")
    print("=" * 60)


if __name__ == "__main__":
    main()