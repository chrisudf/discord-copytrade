"""Discord self-bot listener"""
import os
import asyncio
from collections import deque
from datetime import datetime

import discord
from dotenv import load_dotenv

from src.parser.signal_parser import parse_signal, detect_action
from src.broker.moomoo_client import place_order
from src.notifier.telegram_bot import send_notification
from src.storage.logger_db import log_raw_signal, log_order
from src.utils.logger import logger

load_dotenv("config/.env")

CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", 0))
TRIGGER_USERS = [int(x) for x in os.getenv("DISCORD_TRIGGER_USER_IDS", "").split(",") if x]
TOKEN = os.getenv("DISCORD_USER_TOKEN")

client = discord.Client()

# Bounded dedup cache (avoid memory leak)
_DEDUP_MAX = 2000
_processed_msg_ids: deque = deque(maxlen=_DEDUP_MAX)
_processed_set: set = set()


def _seen(msg_id: int) -> bool:
    if msg_id in _processed_set:
        return True
    if len(_processed_msg_ids) >= _DEDUP_MAX:
        old = _processed_msg_ids[0]
        _processed_set.discard(old)
    _processed_msg_ids.append(msg_id)
    _processed_set.add(msg_id)
    return False


@client.event
async def on_ready():
    logger.info(f"Discord logged in as: {client.user} (id={client.user.id})")
    logger.info(f"Watching channel: {CHANNEL_ID}")
    logger.info(f"Trigger users: {TRIGGER_USERS or 'ALL'}")

    # Sanity check: confirm we can actually see the channel
    ch = client.get_channel(CHANNEL_ID)
    if ch is None:
        logger.error(f"⚠️  Channel {CHANNEL_ID} not visible! Check token / membership.")
    else:
        logger.info(f"Channel OK: #{ch.name} in {ch.guild.name if ch.guild else 'DM'}")


@client.event
async def on_message(message):
    t0 = datetime.now()

    # Ignore self
    if client.user and message.author.id == client.user.id:
        return

    # Channel filter
    if message.channel.id != CHANNEL_ID:
        return

    # User filter
    if TRIGGER_USERS and message.author.id not in TRIGGER_USERS:
        return

    # Dedup
    if _seen(message.id):
        return

    raw = message.content
    if not raw.strip():
        # Empty content (could be embed-only / attachment-only)
        logger.warning(f"Empty content from {message.author.name}, "
                       f"embeds={len(message.embeds)}, attachments={len(message.attachments)}")
        return

    logger.info(f"📩 Signal from {message.author.name}: {raw}")
    try:
        log_raw_signal(message.id, message.author.name, raw, t0)
    except Exception as e:
        logger.error(f"log_raw_signal failed: {e}")

    # Detect OPEN / CLOSE
    action = detect_action(raw)
    if action == "CLOSE":
        # TODO P2: auto-close logic
        logger.info("Close signal detected, skip (TODO P2)")
        await _safe_notify(f"[CLOSE - skipped]\n{raw}")
        return

    # Parse
    signal = parse_signal(raw)
    if not signal:
        logger.warning("Parse failed")
        await _safe_notify(f"⚠️ Parse failed:\n{raw}")
        return

    # Multi-signal: take first
    if isinstance(signal, list):
        logger.info(f"Multi-signal ({len(signal)}), taking first: "
                    f"{signal[0]['symbol']} {signal[0]['strike']}{signal[0]['side'][0]}")
        await _safe_notify(
            f"[Multi-signal] {len(signal)} contracts, taking first only.\n"
            + "\n".join(
                f"  {i+1}. {s['symbol']} {s['strike']}{s['side'][0]} "
                f"{s['expiry']} @ ${s['price']}"
                for i, s in enumerate(signal)
            )
        )
        signal = signal[0]

    # TODO P3: symbol blacklist

    # Place order (wrap sync moomoo call in thread)
    try:
        order_result = await asyncio.to_thread(place_order, signal)
    except Exception as e:
        logger.exception("place_order failed")
        await _safe_notify(f"❌ Order error: {e}\nSignal: {raw}")
        return

    try:
        log_order(message.id, signal, order_result)
    except Exception as e:
        logger.error(f"log_order failed: {e}")

    elapsed = (datetime.now() - t0).total_seconds() * 1000
    await _safe_notify(format_msg(signal, order_result, elapsed))
    logger.info(f"⏱️  End-to-end latency: {elapsed:.0f}ms")


@client.event
async def on_message_edit(before, after):
    """Log edits for post-mortem, do NOT re-trigger orders."""
    if after.channel.id != CHANNEL_ID:
        return
    if TRIGGER_USERS and after.author.id not in TRIGGER_USERS:
        return
    logger.info(f"✏️  Edit from {after.author.name}:\n  BEFORE: {before.content}\n  AFTER:  {after.content}")
    # TODO: store edit history for post-mortem analysis


async def _safe_notify(msg: str):
    try:
        result = send_notification(msg)
        if asyncio.iscoroutine(result):
            await result
    except Exception as e:
        logger.error(f"notify failed: {e}")


def format_msg(signal, result, elapsed_ms):
    status = "✅ OK" if result.get("success") else "❌ FAIL"
    tags = f" [{','.join(signal.get('tags', []))}]" if signal.get("tags") else ""
    return (
        f"{status}{tags} {signal['symbol']} {signal['side']} "
        f"${signal['strike']} {signal['expiry']}\n"
        f"Entry: ${signal['price']}\n"
        f"Latency: {elapsed_ms:.0f}ms\n"
        f"Result: {result.get('message', 'N/A')}"
    )


async def start_listener():
    if not TOKEN:
        raise RuntimeError("DISCORD_USER_TOKEN not configured")
    await client.start(TOKEN)