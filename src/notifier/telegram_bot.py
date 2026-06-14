"""Telegram notifier"""
import os
import aiohttp
from src.utils.logger import logger

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


async def send_notification(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        logger.debug("Telegram not configured, skip.")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.post(url, json=payload, timeout=5) as r:
                if r.status != 200:
                    logger.error(f"TG send failed: {await r.text()}")
    except Exception as e:
        logger.error(f"TG exception: {e}")
