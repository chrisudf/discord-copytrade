"""Phase 1: verify Discord connection + multi-channel config loading."""
import os
import sys
import asyncio
from pathlib import Path
from dotenv import load_dotenv

# 让 src 可 import
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

load_dotenv(Path(__file__).resolve().parents[1] / "config" / ".env", override=True)

import discord
from src.config.channel_loader import registry

TOKEN = os.getenv("DISCORD_USER_TOKEN")

client = discord.Client()


@client.event
async def on_ready():
    print(f"\n✅ Logged in as: {client.user} (id={client.user.id})")
    print(f"📋 Monitored channels ({len(registry.enabled_channel_ids())}):")

    for cid in registry.enabled_channel_ids():
        cfg = registry.get(cid)
        ch = client.get_channel(cid)
        if ch is None:
            print(f"   ❌ {cfg.name} ({cid}) NOT visible — check membership/token")
        else:
            guild_name = ch.guild.name if ch.guild else "DM"
            print(f"   ✅ {cfg.name} ({cid}) → #{ch.name} @ {guild_name}")
            print(f"      trigger_users={cfg.trigger_user_ids}, qty={cfg.default_qty}, max_price={cfg.max_price}")

    print("\n👂 Listening... (Ctrl+C to stop)\n")


@client.event
async def on_message(message):
    cid = message.channel.id
    if not registry.is_monitored(cid):
        return  # 完全忽略非监控频道

    cfg = registry.get(cid)
    is_trigger = cfg.is_trigger_user(message.author.id)
    marker = "🎯 TRIGGER" if is_trigger else "📍 channel match, user mismatch"

    print(f"\n[{marker}] {message.author.name} (id={message.author.id}) in #{message.channel.name} ({cfg.name})")
    print(f"  Content: {message.content!r}")
    if message.embeds:
        print(f"  Embeds: {len(message.embeds)}")
    if message.attachments:
        print(f"  Attachments: {len(message.attachments)}")


async def main():
    if not TOKEN:
        raise RuntimeError("DISCORD_USER_TOKEN not set in config/.env")
    await client.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
