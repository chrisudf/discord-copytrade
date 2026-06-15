"""
干跑测试：不连真 Discord，喂 FakeMessage 走全链路

覆盖场景：
  1. 正常信号（监听频道 + 触发用户）→ 应下单
  2. 非监听频道 → 应忽略
  3. 监听频道但非触发用户 → 应忽略
  4. 解析失败的垃圾内容 → 应发 Telegram 报错但不下单
  5. 价格超 channel max_price → 风控拦截
  6. 重复 message id → dedup 跳过
  7. CLOSE 信号 → 跳过（不下单）
  8. 多信号 → 取第一个

运行：
  python scripts/test_full_flow.py
"""
import sys
import asyncio
from pathlib import Path
from dataclasses import dataclass, field

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.listener.discord_client import handle_message, client as discord_client
from src.listener import discord_client as dc_module  # === [新增] 用于 reset state ===
from src.utils.logger import logger


# ============================================================
# FakeMessage：模拟 discord.Message，只暴露 handle_message 用到的字段
# ============================================================
@dataclass
class FakeAuthor:
    id: int
    name: str


@dataclass
class FakeChannel:
    id: int
    name: str


@dataclass
class FakeMessage:
    id: int
    channel: FakeChannel
    author: FakeAuthor
    content: str
    embeds: list = field(default_factory=list)
    attachments: list = field(default_factory=list)


# ============================================================
# 测试数据
# ============================================================
KC_CHANNEL = FakeChannel(id=1443396572581859405, name="kc-期权-波段-s0")
ENRICH_CHANNEL = FakeChannel(id=1426514716943187979, name="enrich-期权-波段-s0")
RANDOM_CHANNEL = FakeChannel(id=9999999999, name="random")

KC_USER = FakeAuthor(id=1426229538722938880, name="KC")
RANDOM_USER = FakeAuthor(id=8888888888, name="random_user")


# msg_id 必须每次不同，否则 dedup 会跳过
_MSG_ID_COUNTER = [1_000_000]


def next_msg_id() -> int:
    _MSG_ID_COUNTER[0] += 1
    return _MSG_ID_COUNTER[0]


# === [新增] 清空 listener 内部状态，保证 case 之间相互独立 ===
def reset_state():
    """清空 fingerprint 指纹 + msg_id dedup + processed set，避免 case 间互相干扰"""
    dc_module._signal_fps.clear()
    dc_module._processed_msg_ids.clear()
    dc_module._processed_set.clear()


# ============================================================
# Test cases
# ============================================================
async def test_1_normal_signal():
    reset_state()  # === [新增] ===
    print("\n" + "=" * 60)
    print("[1] 正常信号：KC 在 kc 频道发 IREN 60C")
    print("=" * 60)
    msg = FakeMessage(
        id=next_msg_id(),
        channel=KC_CHANNEL,
        author=KC_USER,
        content="IREN 60c 6/14 @ 2.50",
    )
    await handle_message(msg)


async def test_2_unmonitored_channel():
    reset_state()  # === [新增] ===
    print("\n" + "=" * 60)
    print("[2] 非监听频道：应该完全忽略")
    print("=" * 60)
    msg = FakeMessage(
        id=next_msg_id(),
        channel=RANDOM_CHANNEL,
        author=KC_USER,
        content="IREN 60c 6/14 @ 2.50",
    )
    await handle_message(msg)
    print("✅ 没有任何输出 = 正确忽略")


async def test_3_wrong_user():
    reset_state()  # === [新增] ===
    print("\n" + "=" * 60)
    print("[3] 监听频道但非触发用户：应该忽略")
    print("=" * 60)
    msg = FakeMessage(
        id=next_msg_id(),
        channel=KC_CHANNEL,
        author=RANDOM_USER,
        content="IREN 60c 6/14 @ 2.50",
    )
    await handle_message(msg)
    print("✅ 没有下单 = 正确忽略")


async def test_4_parse_fail():
    reset_state()  # === [新增] ===
    print("\n" + "=" * 60)
    print("[4] 垃圾内容：parse 失败应发 Telegram 报错")
    print("=" * 60)
    msg = FakeMessage(
        id=next_msg_id(),
        channel=KC_CHANNEL,
        author=KC_USER,
        content="今天行情真烂，等等再说",
    )
    await handle_message(msg)


async def test_5_price_too_high():
    reset_state()  # === [新增] ===
    print("\n" + "=" * 60)
    print("[5] 价格 $8 > channel max_price $5：风控应拦截")
    print("=" * 60)
    msg = FakeMessage(
        id=next_msg_id(),
        channel=KC_CHANNEL,
        author=KC_USER,
        content="AMZN 200c 6/20 @ 8.00",
    )
    await handle_message(msg)


async def test_6_dedup():
    reset_state()  # === [新增] case 内部需要保留状态，所以只在开头 reset ===
    print("\n" + "=" * 60)
    print("[6] 重复 message_id：dedup 应跳过第二次")
    print("=" * 60)
    fixed_id = next_msg_id()
    msg = FakeMessage(
        id=fixed_id,
        channel=KC_CHANNEL,
        author=KC_USER,
        content="MSFT 500c 6/20 @ 3.00",
    )
    print("第一次：")
    await handle_message(msg)
    print("\n第二次（同 id）：")
    await handle_message(msg)
    print("✅ 第二次应该静默跳过")


async def test_7_close_signal():
    reset_state()  # === [新增] ===
    print("\n" + "=" * 60)
    print("[7] CLOSE 信号：应跳过不下单")
    print("=" * 60)
    msg = FakeMessage(
        id=next_msg_id(),
        channel=KC_CHANNEL,
        author=KC_USER,
        content="Closed IREN 60c for +30%",
    )
    await handle_message(msg)


async def test_8_multi_signal():
    reset_state()  # === [新增] ===
    print("\n" + "=" * 60)
    print("[8] 多信号：应取第一个")
    print("=" * 60)
    msg = FakeMessage(
        id=next_msg_id(),
        channel=KC_CHANNEL,
        author=KC_USER,
        content="IREN 60c 6/14 @ 2.50, AMZN 200c 6/20 @ 3.50",
    )
    await handle_message(msg)


# ============================================================
# Runner
# ============================================================
async def main():
    # 给 discord_client.client.user 塞一个假对象，避免 on_message 里 self check 报错
    class FakeSelf:
        id = 710013635438575637  # 你的小号 id，避免被当成"自己"
    # discord_client.user = FakeSelf()

    tests = [
        test_1_normal_signal,
        test_2_unmonitored_channel,
        test_3_wrong_user,
        test_4_parse_fail,
        test_5_price_too_high,
        test_6_dedup,
        test_7_close_signal,
        test_8_multi_signal,
    ]
    for t in tests:
        try:
            await t()
        except Exception as e:
            logger.exception(f"Test {t.__name__} crashed: {e}")

    print("\n" + "=" * 60)
    print("✅ All tests completed (check output above + Telegram + data/risk.db)")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())