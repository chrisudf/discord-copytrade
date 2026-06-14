"""
风险熔断管理器
4 道防线：
  1. 单张合约价格上限
  2. 单笔订单总成本上限
  3. 当日累计成本上限（触发后当日全停）
  4. 当日下单次数上限（触发后当日全停）

时区：美东时间 00:00 切换交易日
持久化：SQLite，重启不丢失
"""
import os
import sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from typing import Optional
from dotenv import load_dotenv
from loguru import logger

# 加载 .env
ENV_PATH = Path(__file__).resolve().parent.parent.parent / "config" / ".env"
load_dotenv(ENV_PATH, override=True)

# 风控参数
MAX_PRICE_PER_CONTRACT = float(os.getenv("MAX_PRICE_PER_CONTRACT", "5.0"))
MAX_COST_PER_ORDER = float(os.getenv("MAX_COST_PER_ORDER", "500"))
MAX_DAILY_COST = float(os.getenv("MAX_DAILY_COST", "2000"))
MAX_DAILY_ORDERS = int(os.getenv("MAX_DAILY_ORDERS", "10"))

# 数据库路径
DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "risk.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# 美东时区（不考虑夏令时简化版，US Eastern = UTC-5/UTC-4）
# Python 内置时区处理：用 ZoneInfo 更准
try:
    from zoneinfo import ZoneInfo
    US_EASTERN = ZoneInfo("America/New_York")
except ImportError:
    # Python < 3.9 兜底
    US_EASTERN = timezone(timedelta(hours=-5))
    logger.warning("[Risk] zoneinfo 不可用，用固定 UTC-5（无夏令时）")


# ============ 数据结构 ============

@dataclass
class RiskCheckResult:
    """风控检查结果"""
    passed: bool
    reason: str = ""           # 拦截原因（passed=False 时）
    detail: str = ""           # 详细信息
    block_rest_of_day: bool = False  # 是否触发当日全停


# ============ 时间工具 ============

def get_trading_date() -> str:
    """
    返回当前交易日字符串 YYYY-MM-DD（美东时间）
    切换时点：美东 00:00
    """
    now_et = datetime.now(US_EASTERN)
    return now_et.strftime("%Y-%m-%d")


# ============ 数据库 ============

def _init_db():
    """初始化数据库表"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trading_date TEXT NOT NULL,
                ts TEXT NOT NULL,
                symbol TEXT,
                strike REAL,
                side TEXT,
                expiry TEXT,
                price REAL NOT NULL,
                qty INTEGER NOT NULL,
                cost REAL NOT NULL,
                channel_id TEXT,
                channel_name TEXT
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_trading_date 
            ON daily_orders(trading_date)
        """)
        # 熔断锁表：哪天被全停了
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_circuit_breaker (
                trading_date TEXT PRIMARY KEY,
                reason TEXT NOT NULL,
                triggered_at TEXT NOT NULL
            )
        """)
        conn.commit()


_init_db()


# ============ 查询 ============

def get_daily_stats(trading_date: Optional[str] = None) -> dict:
    """返回当日统计"""
    if trading_date is None:
        trading_date = get_trading_date()
    
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("""
            SELECT COUNT(*), COALESCE(SUM(cost), 0)
            FROM daily_orders WHERE trading_date = ?
        """, (trading_date,))
        count, total_cost = cur.fetchone()
        
        cur = conn.execute("""
            SELECT reason, triggered_at FROM daily_circuit_breaker
            WHERE trading_date = ?
        """, (trading_date,))
        cb = cur.fetchone()
    
    return {
        "trading_date": trading_date,
        "order_count": count,
        "total_cost": total_cost,
        "circuit_broken": cb is not None,
        "circuit_reason": cb[0] if cb else None,
        "circuit_triggered_at": cb[1] if cb else None,
        "max_orders": MAX_DAILY_ORDERS,
        "max_cost": MAX_DAILY_COST,
        "remaining_orders": MAX_DAILY_ORDERS - count,
        "remaining_cost": MAX_DAILY_COST - total_cost,
    }


def is_circuit_broken(trading_date: Optional[str] = None) -> bool:
    """当日是否已熔断"""
    if trading_date is None:
        trading_date = get_trading_date()
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("""
            SELECT 1 FROM daily_circuit_breaker WHERE trading_date = ?
        """, (trading_date,))
        return cur.fetchone() is not None


def _trigger_circuit_breaker(reason: str):
    """触发当日熔断"""
    trading_date = get_trading_date()
    now = datetime.now(US_EASTERN).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT OR IGNORE INTO daily_circuit_breaker 
            (trading_date, reason, triggered_at)
            VALUES (?, ?, ?)
        """, (trading_date, reason, now))
        conn.commit()
    logger.warning(f"[Risk] 🚨 当日熔断触发: {reason} (date={trading_date})")


# ============ 核心检查 ============

def check_order(price: float, qty: int, 
                symbol: str = "", strike: float = 0,
                side: str = "", expiry: str = "",
                channel_name: str = "",
                max_price_override: float = None) -> RiskCheckResult:
    """
    下单前风控检查（不记录订单，只检查）
    
    Args:
        price: 单张合约价格（每股美元）
        qty: 下单张数
    
    Returns:
        RiskCheckResult
    """
    trading_date = get_trading_date()
    cost = price * 100 * qty
    
    # ---------- Layer 0: 当日已熔断 ----------
    if is_circuit_broken(trading_date):
        stats = get_daily_stats(trading_date)
        return RiskCheckResult(
            passed=False,
            reason="当日已熔断",
            detail=f"原因: {stats['circuit_reason']} | 触发时间: {stats['circuit_triggered_at']}"
        )
    
    # ---------- Layer 1: 单张价格 ----------
    # channel 配置优先；没传则用全局 MAX_PRICE_PER_CONTRACT
    effective_max_price = max_price_override if max_price_override is not None else MAX_PRICE_PER_CONTRACT
    if price > effective_max_price:
        return RiskCheckResult(
            passed=False,
            reason="单张合约价格超限",
            detail=f"信号价 ${price} > 上限 ${effective_max_price} (channel override={max_price_override is not None})"
        )
    
    # ---------- Layer 2: 单笔成本 ----------
    if cost > MAX_COST_PER_ORDER:
        return RiskCheckResult(
            passed=False,
            reason="单笔订单成本超限",
            detail=f"本笔成本 ${cost:.0f} > 上限 ${MAX_COST_PER_ORDER:.0f}"
        )
    
    # ---------- Layer 3 & 4: 当日累计 ----------
    stats = get_daily_stats(trading_date)
    
    # 次数检查
    if stats["order_count"] >= MAX_DAILY_ORDERS:
        _trigger_circuit_breaker(f"当日下单次数达上限 ({MAX_DAILY_ORDERS})")
        return RiskCheckResult(
            passed=False,
            reason="当日下单次数达上限",
            detail=f"已下 {stats['order_count']}/{MAX_DAILY_ORDERS} 单，当日剩余全停",
            block_rest_of_day=True
        )
    
    # 累计成本检查（本笔会导致超限）
    if stats["total_cost"] + cost > MAX_DAILY_COST:
        _trigger_circuit_breaker(
            f"当日累计成本将达上限 (${stats['total_cost']:.0f} + ${cost:.0f} > ${MAX_DAILY_COST:.0f})"
        )
        return RiskCheckResult(
            passed=False,
            reason="当日累计成本达上限",
            detail=f"已花 ${stats['total_cost']:.0f}，本笔 ${cost:.0f}，上限 ${MAX_DAILY_COST:.0f}，当日剩余全停",
            block_rest_of_day=True
        )
    
    # ---------- 全部通过 ----------
    return RiskCheckResult(passed=True)


def record_order(price: float, qty: int,
                 symbol: str = "", strike: float = 0,
                 side: str = "", expiry: str = "",
                 channel_id: str = "", channel_name: str = ""):
    """
    记录已下单（必须在下单成功后调用）
    """
    trading_date = get_trading_date()
    now = datetime.now(US_EASTERN).isoformat()
    cost = price * 100 * qty
    
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO daily_orders 
            (trading_date, ts, symbol, strike, side, expiry, price, qty, cost, 
             channel_id, channel_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (trading_date, now, symbol, strike, side, expiry, 
              price, qty, cost, channel_id, channel_name))
        conn.commit()
    
    logger.info(
        f"[Risk] 订单已记录: {symbol} {strike}{side} {expiry} "
        f"x{qty} @ ${price} (cost=${cost:.0f}) date={trading_date}"
    )


# ============ 手动管理工具 ============

def manual_reset_today() -> dict:
    """手动重置当日（清除熔断 + 清除今日订单记录）"""
    trading_date = get_trading_date()
    with sqlite3.connect(DB_PATH) as conn:
        cur1 = conn.execute("DELETE FROM daily_circuit_breaker WHERE trading_date = ?", 
                            (trading_date,))
        cur2 = conn.execute("DELETE FROM daily_orders WHERE trading_date = ?", 
                            (trading_date,))
        conn.commit()
        return {
            "trading_date": trading_date,
            "circuit_breaker_cleared": cur1.rowcount,
            "orders_cleared": cur2.rowcount,
        }


def clear_circuit_breaker_only(trading_date: Optional[str] = None) -> bool:
    """只清除熔断标记，保留订单记录（更安全）"""
    if trading_date is None:
        trading_date = get_trading_date()
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("DELETE FROM daily_circuit_breaker WHERE trading_date = ?", 
                          (trading_date,))
        conn.commit()
        return cur.rowcount > 0