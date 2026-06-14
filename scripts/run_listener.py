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

from src.config.channel_loader import registry
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


@client.event
async def on_ready():
    logger.info(f"✅ Discord logged in as: {client.user} (id={client.user.id})")
    try:
        await send_telegram(
            f"🟢 *Listener 启动*\n"
            f"账号: `{client.user}`\n"
            f"监听: {len(registry.enabled_channel_ids())} 频道\n"
            f"DRY_RUN: `{os.getenv('DRY_RUN', 'true')}`"
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
