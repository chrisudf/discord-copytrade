"""Daily report: read SQLite -> CSV + stats"""
import sqlite3
import pandas as pd
from datetime import date
from pathlib import Path

DB = "data/trades.db"
OUT_DIR = Path("data/reports")
OUT_DIR.mkdir(parents=True, exist_ok=True)


def generate_daily_report(target=None):
    target = target or date.today()
    conn = sqlite3.connect(DB)
    df = pd.read_sql(
        "SELECT * FROM orders WHERE date(placed_at) = ?",
        conn,
        params=[target.isoformat()],
    )
    conn.close()

    if df.empty:
        print(f"No orders for {target}")
        return

    csv = OUT_DIR / f"report_{target}.csv"
    df.to_csv(csv, index=False)

    print(f"=== Report {target} ===")
    print(f"Total:   {len(df)}")
    print(f"Success: {df['success'].sum()}")
    print(f"Failed:  {(df['success'] == 0).sum()}")
    print(f"Symbols: {df['symbol'].value_counts().to_dict()}")
    print(f"Saved -> {csv}")


if __name__ == "__main__":
    generate_daily_report()
