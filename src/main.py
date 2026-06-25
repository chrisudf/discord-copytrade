"""主入口: 启动 Discord 监听 -> 解析 -> 下单 -> 通知

⚠️  推荐入口是 `scripts/run_listener.py`，它做了完整的 preflight：
    - Broker 健康探测（OpenD 可达 + 账户 ACTIVE + unlock 验证）
    - 频道 ID REST 校验
    - max_price 高额阈值 gate（>$50 在 REAL+!DRY_RUN 直接 sys.exit）
    - 启动 Telegram 通知

此文件保留只为兼容老脚本；新部署一律走 run_listener.py。
"""
import asyncio
import os
from src.listener.discord_client import start_listener
from src.utils.logger import logger

if __name__ == "__main__":
    logger.warning(
        "⚠️  src.main 入口已弱化（不跑 preflight）。生产请用 "
        "`python scripts/run_listener.py`。"
    )
    # 兜底硬卡：真盘模式下绝不允许这条路径起，避免绕过 max_price gate
    dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
    trd_env = os.getenv("MOOMOO_TRD_ENV", "SIMULATE").strip().upper()
    if not dry_run and trd_env != "SIMULATE":
        logger.error(
            "❌ src.main 入口禁止在 REAL + DRY_RUN=false 下启动（绕过 preflight）。"
            " 请改用 scripts/run_listener.py。"
        )
        raise SystemExit(1)

    logger.info("Copy Trade Bot starting...")
    try:
        asyncio.run(start_listener())
    except KeyboardInterrupt:
        logger.info("Manual stop.")
