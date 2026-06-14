"""SQLite persistence: raw signals / orders"""
import sqlite3
from pathlib import Path
from datetime import datetime

DB_PATH = Path("data/trades.db")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


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
        (str(msg_id), author, content, received_at.isoformat()),
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
            datetime.now().isoformat(),
        ),
    )
    conn.commit()
    conn.close()
