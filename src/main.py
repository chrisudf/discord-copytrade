"""主入口: 启动 Discord 监听 -> 解析 -> 下单 -> 通知"""
import asyncio
from src.listener.discord_client import start_listener
from src.utils.logger import logger

if __name__ == "__main__":
    logger.info("Copy Trade Bot starting...")
    try:
        asyncio.run(start_listener())
    except KeyboardInterrupt:
        logger.info("Manual stop.")
