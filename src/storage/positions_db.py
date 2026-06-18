"""持仓追踪 + 流水

设计：
- positions: 当前仓位状态，以 option_code 为 PK
  - 同 symbol+strike+side+expiry 累计入一行（加仓 → qty_total+, qty_remaining+）
  - 分批卖出 → qty_remaining-，status 在 OPEN/PARTIAL/CLOSED 之间转移
- position_events: 不可变流水，所有变更都追加一行
  - 用于复盘、对账、PnL 估算
  - 含触发源（kc_signal/sl_polling/tp_polling/eod/manual）

时区策略：同 logger_db / risk_manager，UTC ISO 带 Z 后缀。
expiry 列存 ISO date 字符串（YYYY-MM-DD），category 决定 SL 策略。

category 是纯展示标签（用于日志/TG/复盘），行为靠两个独立 flag：

- apply_sl         = 是否挂止损（DTE 1-7 且非 lotto 才挂）
- eod_force_close  = 是否当日 EOD 强平（只看 DTE==0，不管 lotto 标签——
                     0DTE 当天必过期，必须平；周内 lotto 要放飞到 expiry）

category 命名（信息性）：
- 0dte          DTE == 0 且非 lotto
- 0dte_lotto    DTE == 0 且 lotto 标签
- lotto         DTE >= 1 且 lotto 标签
- weekly        DTE 1-7 且非 lotto（唯一挂 SL 的类目）
- swing         DTE >= 8 且非 lotto

apply_sl / eod_force_close 都冗余存到 positions 表，
避免后续规则调整污染历史仓位。

TODO（测试调整）：
- swing 8-20 DTE 区间是否要细分加一个 'mid' category 套宽松 SL
- 加仓场景（同 option_code 第二次 OPEN）的 avg_entry_price 加权平均逻辑测试
- 同 symbol 多 strike 的查询接口（find_by_symbol）排序规则（按 opened_at? expiry?）
"""
import json
import sqlite3
from pathlib import Path
from datetime import datetime, date, timezone
from typing import Optional

from src.utils.logger import logger

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "trades.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


# ============ 时间工具 ============

def _utc_iso(dt: datetime = None) -> str:
    """统一 UTC ISO 带 Z 后缀。naive datetime 抛错（同 logger_db 策略）。"""
    if dt is None:
        dt = datetime.now(timezone.utc)
    elif dt.tzinfo is None:
        raise ValueError(f"_utc_iso() received naive datetime: {dt!r}")
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


# ============ 类目判定 ============

def categorize(
    expiry_d: date, today_et: date, tags: list[str]
) -> tuple[str, bool, bool]:
    """根据 DTE + tags 决定 category / apply_sl / eod_force_close。

    Args:
        expiry_d: 实际下单的到期日（已过假日调整）
        today_et: 当前 ET 日期
        tags: 信号 tags（lotto / swing / scalp 等）

    Returns:
        (category, apply_sl, eod_force_close)

    决策矩阵：
        DTE  | lotto | category    | apply_sl | eod_force
        -----+-------+-------------+----------+----------
        0    | no    | 0dte        | False    | True
        0    | yes   | 0dte_lotto  | False    | True   ← 必过期，强平
        1-7  | no    | weekly      | True     | False
        1-7  | yes   | lotto       | False    | False  ← 彩票放飞
        8+   | any   | swing       | False    | False

    TODO: lotto 实测后看要不要加 max_loss_pct（比如 -80% 硬底）
    TODO: swing 8-20 是否细分，独立测一段时间数据
    """
    dte = (expiry_d - today_et).days
    if dte < 0:
        logger.warning(f"[positions] negative DTE {dte}, treat as 0dte")
        dte = 0

    is_lotto = "lotto" in tags
    eod_force = (dte == 0)

    if dte == 0:
        return ("0dte_lotto" if is_lotto else "0dte"), False, eod_force
    if is_lotto:
        return "lotto", False, False
    if dte <= 7:
        return "weekly", True, False
    return "swing", False, False


# ============ DB 初始化 ============

def _init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                option_code      TEXT PRIMARY KEY,
                symbol           TEXT NOT NULL,
                strike           REAL NOT NULL,
                side             TEXT NOT NULL,
                expiry           TEXT NOT NULL,
                qty_total        INTEGER NOT NULL,
                qty_remaining    INTEGER NOT NULL,
                avg_entry_price  REAL NOT NULL,
                category         TEXT NOT NULL,
                apply_sl         INTEGER NOT NULL,
                eod_force_close  INTEGER NOT NULL DEFAULT 0,
                tags             TEXT,
                channel_name     TEXT,
                open_msg_id      TEXT,
                opened_at        TEXT NOT NULL,
                last_action_at   TEXT NOT NULL,
                closed_at        TEXT,
                status           TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_pos_symbol ON positions(symbol)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_pos_status ON positions(status)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS position_events (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                option_code      TEXT NOT NULL,
                event_type       TEXT NOT NULL,
                qty_delta        INTEGER NOT NULL,
                price            REAL,
                pct              REAL,
                trigger_source   TEXT NOT NULL,
                ref_msg_id       TEXT,
                order_id         TEXT,
                ts               TEXT NOT NULL,
                note             TEXT
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_evt_code ON position_events(option_code)
        """)
        # 迁移：早期版本没有 eod_force_close 列
        cols = {r[1] for r in conn.execute("PRAGMA table_info(positions)").fetchall()}
        if "eod_force_close" not in cols:
            conn.execute(
                "ALTER TABLE positions ADD COLUMN eod_force_close INTEGER NOT NULL DEFAULT 0"
            )
            logger.info("[positions] migrated: added eod_force_close column")


_init_db()


# ============ 写入 ============

def open_or_add(
    option_code: str, symbol: str, strike: float, side: str,
    expiry: date, qty: int, fill_price: float,
    category: str, apply_sl: bool, eod_force_close: bool,
    tags: list[str], channel_name: str, msg_id: str,
) -> dict:
    """开仓 / 加仓。

    - 不存在 → INSERT 新仓位 + OPEN 事件
    - 已存在（同 option_code）→ UPDATE qty + 加权平均价 + ADD_ON 事件

    返回当前持仓 dict（含更新后的 qty_remaining）。
    """
    now = _utc_iso()
    expiry_str = expiry.isoformat()

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        existing = conn.execute(
            "SELECT * FROM positions WHERE option_code = ?",
            (option_code,),
        ).fetchone()

        if existing is None:
            conn.execute("""
                INSERT INTO positions (
                    option_code, symbol, strike, side, expiry,
                    qty_total, qty_remaining, avg_entry_price,
                    category, apply_sl, eod_force_close, tags, channel_name,
                    open_msg_id, opened_at, last_action_at, status
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                option_code, symbol, strike, side, expiry_str,
                qty, qty, fill_price,
                category, int(apply_sl), int(eod_force_close),
                json.dumps(tags), channel_name,
                str(msg_id), now, now, "OPEN",
            ))
            event_type = "OPEN"
        else:
            # 加权平均：(old_qty*old_avg + new_qty*new_price) / total
            old_total = existing["qty_total"]
            old_avg = existing["avg_entry_price"]
            new_total = old_total + qty
            new_avg = (old_total * old_avg + qty * fill_price) / new_total
            new_remaining = existing["qty_remaining"] + qty
            conn.execute("""
                UPDATE positions
                SET qty_total = ?, qty_remaining = ?, avg_entry_price = ?,
                    last_action_at = ?, status = ?
                WHERE option_code = ?
            """, (
                new_total, new_remaining, new_avg,
                now, "OPEN", option_code,
            ))
            event_type = "ADD_ON"
            logger.info(
                f"[positions] add-on {option_code}: +{qty} @ {fill_price:.2f} "
                f"new_avg={new_avg:.2f} qty_rem={new_remaining}"
            )

        conn.execute("""
            INSERT INTO position_events (
                option_code, event_type, qty_delta, price, pct,
                trigger_source, ref_msg_id, ts, note
            ) VALUES (?,?,?,?,?,?,?,?,?)
        """, (
            option_code, event_type, qty, fill_price, None,
            "kc_signal", str(msg_id), now,
            f"category={category} apply_sl={apply_sl} eod_force={eod_force_close}",
        ))

    return get(option_code)


def record_close(
    option_code: str, qty_sold: int, fill_price: float,
    trigger_source: str, ref_msg_id: Optional[str] = None,
    order_id: Optional[str] = None, note: str = "",
) -> Optional[dict]:
    """记录卖出（部分或全部）。

    Args:
        qty_sold: 本次卖出张数（正数）
        trigger_source: kc_signal / sl_polling / tp_polling / eod / manual
        ref_msg_id: 触发源是 discord 时填 msg_id
        order_id: broker 返回的卖单 ID

    Returns:
        更新后的 position dict，仓位不存在返回 None
    """
    if qty_sold <= 0:
        raise ValueError(f"qty_sold must be positive, got {qty_sold}")

    now = _utc_iso()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        pos = conn.execute(
            "SELECT * FROM positions WHERE option_code = ?",
            (option_code,),
        ).fetchone()
        if pos is None:
            logger.warning(f"[positions] close ignored, no position: {option_code}")
            return None

        remaining = pos["qty_remaining"] - qty_sold
        if remaining < 0:
            # 卖多了，clamp 到 0 并 log（可能 close 信号要求卖 50% 但只剩 1 张）
            logger.warning(
                f"[positions] over-close {option_code}: had {pos['qty_remaining']}, "
                f"asked {qty_sold}, clamping"
            )
            qty_sold = pos["qty_remaining"]
            remaining = 0

        if remaining == 0:
            new_status = "CLOSED"
            closed_at = now
        else:
            new_status = "PARTIAL"
            closed_at = None

        conn.execute("""
            UPDATE positions
            SET qty_remaining = ?, status = ?, last_action_at = ?, closed_at = ?
            WHERE option_code = ?
        """, (remaining, new_status, now, closed_at, option_code))

        # event_type 区分用途：CLOSE = 全平，TRIM = 部分
        event_type = "CLOSE" if remaining == 0 else "TRIM"
        pct = round(qty_sold / pos["qty_remaining"] * 100, 2) if pos["qty_remaining"] else 0
        conn.execute("""
            INSERT INTO position_events (
                option_code, event_type, qty_delta, price, pct,
                trigger_source, ref_msg_id, order_id, ts, note
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            option_code, event_type, -qty_sold, fill_price, pct,
            trigger_source, ref_msg_id, order_id, now, note,
        ))

    logger.info(
        f"[positions] {event_type} {option_code}: -{qty_sold} @ {fill_price:.2f} "
        f"({trigger_source}) remaining={remaining}"
    )
    return get(option_code)


# ============ 查询 ============

def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["apply_sl"] = bool(d["apply_sl"])
    d["eod_force_close"] = bool(d.get("eod_force_close", 0))
    try:
        d["tags"] = json.loads(d["tags"]) if d["tags"] else []
    except json.JSONDecodeError:
        d["tags"] = []
    return d


def get(option_code: str) -> Optional[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM positions WHERE option_code = ?",
            (option_code,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def get_open_positions() -> list[dict]:
    """所有未完全平掉的仓位（OPEN + PARTIAL）。"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT * FROM positions
            WHERE status IN ('OPEN', 'PARTIAL')
            ORDER BY opened_at DESC
        """).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_open_symbols() -> set[str]:
    """活跃仓位的 symbol 集合 —— 供 CLOSE parser 做白名单消歧用。"""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT DISTINCT symbol FROM positions
            WHERE status IN ('OPEN', 'PARTIAL')
        """).fetchall()
    return {r[0] for r in rows}


def find_by_symbol(symbol: str) -> list[dict]:
    """查同 symbol 所有活跃仓位（可能多 strike）。

    排序：opened_at DESC（最近的在前）。
    TODO: close 信号匹配多 strike 时的策略——v1 暂时全平所有同 symbol 仓位，
          后续可能要按"最近开仓"或"strike 最接近 close 信号中提到的价"匹配。
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT * FROM positions
            WHERE symbol = ? AND status IN ('OPEN', 'PARTIAL')
            ORDER BY opened_at DESC
        """, (symbol,)).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_events(option_code: str) -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT * FROM position_events
            WHERE option_code = ? ORDER BY id
        """, (option_code,)).fetchall()
    return [dict(r) for r in rows]
