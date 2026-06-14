"""loguru logger: split files + daily rotation"""
import sys
from pathlib import Path
from loguru import logger

Path("logs").mkdir(exist_ok=True)
Path("logs/errors").mkdir(exist_ok=True)

logger.remove()
logger.add(
    sys.stdout,
    level="INFO",
    format="<green>{time:HH:mm:ss.SSS}</green> | <level>{level}</level> | {message}",
)
logger.add(
    "logs/app_{time:YYYY-MM-DD}.log",
    rotation="1 day",
    retention="30 days",
    level="DEBUG",
)
logger.add(
    "logs/errors/error_{time:YYYY-MM-DD}.log",
    rotation="1 day",
    retention="60 days",
    level="ERROR",
)
