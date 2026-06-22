"""
主入口：连接 Discord，监听所有 enabled 频道，触发 handle_message
用法：
  python scripts/run_listener.py

环境变量（config/.env）:
  DISCORD_USER_TOKEN  必填
  DRY_RUN        true/false（默认 true，安全起见）
  MOOMOO_TRD_ENV SIMULATE/REAL
"""
import sys
import os
import asyncio
import signal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
ENV_PATH = Path(__file__).resolve().parent.parent / "config" / ".env"
load_dotenv(ENV_PATH, override=True)

import discord
from loguru import logger

from src.config.channel_loader import registry, validate_channels
from src.listener.discord_client import handle_message
from src.notifier.telegram_client import send_telegram, format_error
from src.risk.risk_manager import get_daily_stats


# ============ 启动检查 ============

def preflight() -> str:
    """启动前检查，返回 token"""
    token = os.getenv("DISCORD_USER_TOKEN", "").strip()
    if not token:
        logger.error("❌ DISCORD_USER_TOKEN 未配置")
        sys.exit(1)

    enabled_ids = registry.enabled_channel_ids()
    if not enabled_ids:
        logger.error("❌ config/channels.json 没有任何 enabled 频道")
        sys.exit(1)

    dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
    trd_env = os.getenv("MOOMOO_TRD_ENV", "SIMULATE")

    # 实盘/模拟盘真单模式都需要 ACC_ID。空着启动 → 接到信号才报错的悲剧
    # （6/18 IWM 卖单失败就是这个原因）。
    if not dry_run:
        try:
            acc_id = int(os.getenv("MOOMOO_ACC_ID", 0))
        except ValueError:
            acc_id = 0
        if acc_id == 0:
            logger.error(
                "❌ DRY_RUN=False 但 MOOMOO_ACC_ID 未配置或为 0。\n"
                "   检查 config/.env 里 MOOMOO_ACC_ID=<数字> 是否存在且非零。\n"
                "   不强制 exit 反而下单时才暴露，会丢真实信号。"
            )
            sys.exit(1)
    
    logger.info("=" * 60)
    logger.info("🚀 Discord Copytrade Listener 启动")
    logger.info("=" * 60)
    logger.info(f"DRY_RUN       = {dry_run}  {'(不会真下单)' if dry_run else '⚠️  真实下单!'}")
    logger.info(f"MOOMOO_TRD_ENV = {trd_env}")
    logger.info(f"监听频道数    = {len(enabled_ids)}")
    for cid in enabled_ids:
        cfg = registry.get(cid)
        logger.info(f"  • {cfg.name} (id={cid}, qty={cfg.default_qty}, "
                    f"max_price=${cfg.max_price}, triggers={cfg.trigger_user_ids})")
    
    stats = get_daily_stats()
    logger.info(f"今日风控      : {stats['order_count']}/{stats['max_orders']} 单, "
                f"${stats['total_cost']}/${stats['max_cost']}")
    if stats["circuit_broken"]:
        logger.warning(f"🚨 当日已熔断: {stats['circuit_reason']}")
    logger.info("=" * 60)
    
    return token


# ============ Discord Client ============

client = discord.Client()


# on_ready 每次重连都会触发（discord.py-self 约 20-30 min 一次），
# 只在首次连接时发 TG 启动通知，避免刷屏。
_startup_notified = False


@client.event
async def on_ready():
    global _startup_notified
    logger.info(f"✅ Discord logged in as: {client.user} (id={client.user.id})")

    # 仅首次 on_ready 做完整频道校验（REST fetch），重连只 log 不重新探测
    if _startup_notified:
        return
    _startup_notified = True

    failures = await validate_channels(client)
    enabled_count = len(registry.enabled_channel_ids())

    if failures:
        lines = "\n".join(f"• {name} (id={cid}): {reason}" for cid, name, reason in failures)
        try:
            await send_telegram(
                f"⚠️ 频道配置校验失败\n"
                f"{len(failures)}/{enabled_count} 个频道无法解析:\n{lines}\n"
                f"请检查 config/channels.json",
                parse_mode=None,
            )
        except Exception as e:
            logger.warning(f"Telegram channel-failure notify failed: {e}")
        if len(failures) == enabled_count:
            logger.error(
                "❌ 所有 enabled 频道都校验失败，listener 没有消息源 — 退出。"
                " 修复 config/channels.json 后重启。"
            )
            await client.close()
            return

    try:
        await send_telegram(
            f"🟢 Listener 启动\n"
            f"账号: {client.user}\n"
            f"监听: {enabled_count - len(failures)}/{enabled_count} 频道有效\n"
            f"DRY_RUN: {os.getenv('DRY_RUN', 'true')}",
            parse_mode=None,
        )
    except Exception as e:
        logger.warning(f"Telegram startup notify failed: {e}")


@client.event
async def on_message(message):
    try:
        await handle_message(message)
    except Exception as e:
        logger.exception(f"on_message crashed: {e}")
        try:
            await send_telegram(format_error("on_message", str(e)))
        except Exception:
            pass


@client.event
async def on_message_edit(before, after):
    # 暂不触发下单，只记录（防止 KC 改单价导致重复触发）
    if registry.is_monitored(after.channel.id):
        logger.info(f"✏️  [edit] {after.channel.name}: {after.content[:80]}")


# 诊断断线原因：6/18 出现 29 次重连（vs 之前 2-4 次/天）
# discord.py-self 不会自动 log reason，得自己挂 event handler。
@client.event
async def on_disconnect():
    logger.warning("⚠️  Discord on_disconnect fired (websocket dropped)")


@client.event
async def on_resumed():
    logger.info("🔄 Discord session resumed")


@client.event
async def on_error(event_name, *args, **kwargs):
    logger.exception(f"❌ Discord on_error in '{event_name}'")


# ============ 优雅退出 ============

async def shutdown():
    logger.info("收到退出信号，关闭 Discord client...")
    try:
        await send_telegram("🔴 *Listener 退出*")
    except Exception:
        pass
    await client.close()


def setup_signal_handlers(loop):
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown()))


# ============ 主入口 ============

async def main():
    token = preflight()
    loop = asyncio.get_running_loop()
    setup_signal_handlers(loop)
    
    try:
        await client.start(token)
    except discord.LoginFailure:
        logger.error("❌ Discord 登录失败：token 失效或被风控")
        try:
            await send_telegram("❌ *Listener 启动失败*\nDiscord token 失效")
        except Exception:
            pass
        sys.exit(1)
    except Exception as e:
        logger.exception(f"Discord client crashed: {e}")
        try:
            await send_telegram(format_error("Discord client", str(e)))
        except Exception:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("用户 Ctrl+C 退出")
