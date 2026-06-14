"""Phase 1: just verify we can connect and see the channel."""
import os
import asyncio
import discord
from dotenv import load_dotenv

load_dotenv("config/.env")

TOKEN = os.getenv("DISCORD_USER_TOKEN")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", 0))
TRIGGER_USERS = [int(x) for x in os.getenv("DISCORD_TRIGGER_USER_IDS", "").split(",") if x]

client = discord.Client()


@client.event
async def on_ready():
    print(f"✅ Logged in as: {client.user} (id={client.user.id})")
    print(f"📺 Target channel: {CHANNEL_ID}")
    print(f"👤 Trigger users: {TRIGGER_USERS}")

    ch = client.get_channel(CHANNEL_ID)
    if ch is None:
        print(f"❌ Channel {CHANNEL_ID} NOT visible. Check token / membership.")
        await client.close()
        return
    print(f"✅ Channel visible: #{ch.name}")
    if ch.guild:
        print(f"   Guild: {ch.guild.name}")
    print("\n👂 Listening for messages... (Ctrl+C to stop)\n")


@client.event
async def on_message(message):
    is_target_channel = message.channel.id == CHANNEL_ID
    is_target_user = (not TRIGGER_USERS) or message.author.id in TRIGGER_USERS

    marker = ""
    if is_target_channel and is_target_user:
        marker = "🎯 MATCH"
    elif is_target_channel:
        marker = "📍 channel match, user mismatch"
    else:
        return  # ignore other channels entirely

    print(f"\n[{marker}] {message.author.name} (id={message.author.id}) in #{message.channel.name}")
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