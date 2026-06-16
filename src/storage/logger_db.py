"""SQLite persistence: raw signals / orders

时区策略：所有时间统一存 UTC ISO with 'Z' 后缀。
复盘时用 scripts/show_today.py 自动转 ET 显示。
"""
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

DB_PATH = Path("data/trades.db")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def _utc_iso(dt: datetime = None) -> str:
    """统一 UTC ISO 格式：2026-06-16T00:16:42.258Z"""
    if dt is None:
        dt = datetime.now(timezone.utc)
    elif dt.tzinfo is None:
        # naive datetime 一律视作 UTC（防御性，正常不应进入此分支）
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS raw_signals (
            msg_id TEXT PRIMARY KEY,
            author TEXT,
            content TEXT,
            received_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            msg_id TEXT,
            symbol TEXT,
            side TEXT,
            strike REAL,
            expiry TEXT,
            entry_price REAL,
            qty INTEGER,
            option_code TEXT,
            success INTEGER,
            message TEXT,
            order_id TEXT,
            placed_at TEXT
        )
    """)
    return conn


def log_raw_signal(msg_id, author, content, received_at):
    conn = _conn()
    conn.execute(
        "INSERT OR IGNORE INTO raw_signals VALUES (?, ?, ?, ?)",
        (str(msg_id), author, content, _utc_iso(received_at)),
    )
    conn.commit()
    conn.close()


def log_order(msg_id, signal, result):
    conn = _conn()
    conn.execute(
        """INSERT INTO orders
           (msg_id, symbol, side, strike, expiry, entry_price, qty,
            option_code, success, message, order_id, placed_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            str(msg_id),
            signal.get("symbol"),
            signal.get("side"),
            signal.get("strike"),
            str(signal.get("expiry")),
            signal.get("price"),
            result.get("qty"),
            result.get("code"),
            int(result.get("success", False)),
            result.get("message"),
            result.get("order_id"),
            _utc_iso(),  # 用当前 UTC 时间
        ),
    )
    conn.commit()
    conn.close()