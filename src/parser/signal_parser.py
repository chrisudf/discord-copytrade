"""Signal parser - supports KC Trades / generic Discord format

Rules (2026-06):
1. English-only
2. No multi-leg
3. No price = no trade
4. No price range
5. expiry 相对消息时间戳计算（回测正确性）
6. expiry 落假日/周末 → 自动前移到最近交易日（如 6/19 Juneteenth → 6/18）
7. expiry 显示字符串与 expiry_date 保持一致（避免 TG/DB 显示 6/19 但实际下 6/18）
"""
import re
from datetime import date, timedelta
from src.parser.holidays import adjust_to_trading_day, is_trading_day
from src.utils.logger import logger


# ===== 跨年容错 =====
def smart_expiry(mm: int, dd: int, today: date = None) -> date:
    today = today or date.today()
    candidates = [
        date(today.year, mm, dd),
        date(today.year + 1, mm, dd),
        date(today.year - 1, mm, dd),
    ]
    future = [d for d in candidates if d >= today - timedelta(days=2)]
    if not future:
        return candidates[0]
    return min(future, key=lambda d: abs((d - today).days))


def _next_friday(today: date) -> date:
    """本周或下周五（如果今天就是周五，返回今天）。"""
    days_to_friday = (4 - today.weekday()) % 7
    return today + timedelta(days=days_to_friday)


# === 假日调整封装 ===
def _adjust_expiry(d: date, context: str = "") -> date:
    """把 expiry 调整到最近的交易日（向前回退）。

    适用场景：weekly 算到 6/19 Juneteenth → 实际为 6/18 周四。

    Args:
        d: 候选 expiry
        context: 日志上下文（如 "weekly" / "MM/DD"），仅 log 用
    """
    if is_trading_day(d):
        return d
    adjusted = adjust_to_trading_day(d, direction="backward")
    logger.info(
        f"[parser] expiry {d} → {adjusted} (holiday/weekend adjusted, ctx={context})"
    )
    return adjusted


# === 同步 expiry 显示字符串（避免 holiday adjust 后显示与实际不符）===
def _finalize_signal(sig: dict) -> dict:
    """在 return 之前调用：把 expiry 字符串统一改成 M/D，
    与 expiry_date 实际日期对齐。
    NDTE / weekly 原始字符串会被覆盖——实际下单日比相对表达更有用，
    TG/DB 也不会出现"显示 6/19 实际下 6/18"的错位。
    """
    if sig.get("expiry_date"):
        d = sig["expiry_date"]
        sig["expiry"] = f"{d.month}/{d.day}"
    return sig


# === 英文月份映射 ===
MONTH_NAME_TO_NUM = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

MONTH_NAMES_RE = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)


# ===== Pre-filter =====
SKIP_KEYWORDS = [
    "holding", "remaining", "into tomorrow",
    "持仓", "i'm holding", "im holding",
]

PRICE_RANGE_PATTERN = re.compile(
    r"\$\.?\d+(?:\.\d+)?\s*(?:-|to|~)\s*\$?\.?\d+(?:\.\d+)?",
    re.IGNORECASE,
)

CHINESE_MARKERS = ["美股会员网rich", "美股会员网机器人"]


def _strip_chinese(text: str) -> str:
    for marker in CHINESE_MARKERS:
        if marker not in text:
            continue

        idx = text.find(f"\n\n{marker}")
        if idx > 0:
            return text[:idx].strip()

        idx = text.find(f"\n{marker}")
        if idx > 0:
            return text[:idx].strip()

        first = text.find(marker)
        second = text.find(marker, first + len(marker))
        if second > 0:
            segment = text[first:second]
            segment = re.sub(rf"^{re.escape(marker)}\s*:?\s*", "", segment)
            return segment.strip()

        if text.lstrip().startswith(marker):
            cleaned = re.sub(rf"^\s*{re.escape(marker)}\s*:?\s*", "", text)
            return cleaned.strip()

        return text[:first].strip()

    return text


def _has_price_range(text: str) -> bool:
    return bool(PRICE_RANGE_PATTERN.search(text))


def _has_skip_keyword(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in SKIP_KEYWORDS)


# ===== 主解析 =====
def parse_signal(text: str, msg_ts: date = None):
    """Parse single signal.

    Args:
        text: 消息内容
        msg_ts: 消息发送日期（默认 today，回测时传消息时间戳）

    Returns:
        - dict: 成功解析的信号
        - dict {"skip": "..."}: 主动 skip（非错误，listener 不应警告）
        - None: 真·解析失败（应警告 + TG）
    """
    if not text or len(text.strip()) < 5:
        return None

    today = msg_ts or date.today()

    text = _strip_chinese(text)
    if not text or len(text.strip()) < 5:
        return None

    if _has_skip_keyword(text):
        logger.info(f"[parser] skip (holding/remaining): {text[:60]}")
        return {"skip": "holding_or_remaining"}

    if _has_price_range(text):
        logger.info(f"[parser] skip (price range): {text[:60]}")
        return {"skip": "price_range"}

    sig = _try_pattern_a(text, today) or _try_pattern_b(text, today)

    if sig is None:
        logger.warning(f"[parser] no signal: {text[:80]}")
        return None

    if sig.get("price") is None:
        logger.warning(f"[parser] skip (no price): {sig['matched']}")
        return {"skip": "no_price"}

    return _finalize_signal(sig)


def _try_pattern_a(text: str, today: date):
    """Pattern A: SYMBOL STRIKEc/p {MM/DD | Month DD} @ PRICE"""

    # === A2: 英文月份在先（优先级更高，避免 A1 误吃）===
    pattern_a2 = re.compile(
        rf"\b([A-Z]{{1,5}})\s+"
        rf"(\d+(?:\.\d+)?)([cp])\s+"
        rf"({MONTH_NAMES_RE})\s+(\d{{1,2}})(?:st|nd|rd|th)?"
        rf"(?:[^@\n]*?@\s*\$?(\d+(?:\.\d+)?))?",
        re.IGNORECASE,
    )
    for m in pattern_a2.finditer(text):
        symbol, strike, cp, month_name, dd, price = m.groups()
        if symbol.upper() in {"I", "A", "THE", "AT", "ON", "IS", "DTE", "IPO"}:
            continue
        if price is None:
            filled = re.search(r"filled?\s*@\s*\$?(\d+(?:\.\d+)?)", text, re.I)
            if filled:
                price = filled.group(1)
        if price is None:
            continue
        mm = MONTH_NAME_TO_NUM[month_name.lower()]
        dd_int = int(dd)
        return {
            "raw": text,
            "matched": m.group(0).strip(),
            "symbol": symbol.upper(),
            "side": "CALL" if cp.lower() == "c" else "PUT",
            "strike": float(strike),
            "expiry": f"{mm}/{dd_int}",
            "expiry_date": _adjust_expiry(
                smart_expiry(mm, dd_int, today=today), context="A2 Month DD"
            ),
            "price": float(price),
            "tags": _extract_tags(text),
        }

    # A1: 数字日期 MM/DD
    pattern = re.compile(
        r"\b([A-Z]{1,5})\s+"
        r"(\d+(?:\.\d+)?)([cp])\s+"
        r"(\d{1,2})/(\d{1,2})"
        r"(?:[^@\n]*?@\s*\$?(\d+(?:\.\d+)?))?",
        re.IGNORECASE,
    )

    for m in pattern.finditer(text):
        symbol, strike, cp, mm, dd, price = m.groups()

        if symbol.upper() in {"I", "A", "THE", "AT", "ON", "IS", "DTE", "IPO"}:
            continue

        if price is None:
            filled = re.search(r"filled?\s*@\s*\$?(\d+(?:\.\d+)?)", text, re.I)
            if filled:
                price = filled.group(1)

        if price is None:
            continue

        return {
            "raw": text,
            "matched": m.group(0).strip(),
            "symbol": symbol.upper(),
            "side": "CALL" if cp.lower() == "c" else "PUT",
            "strike": float(strike),
            "expiry": f"{int(mm)}/{int(dd)}",
            "expiry_date": _adjust_expiry(
                smart_expiry(int(mm), int(dd), today=today), context="A1 MM/DD"
            ),
            "price": float(price),
            "tags": _extract_tags(text),
        }

    return None


def _try_pattern_b(text: str, today: date):
    """Pattern B: 多种 $SYMBOL 形态。

    优先级：
    B0:    含明确 MM/DD（最准确）
    B0.5:  含英文月份 (June 26 / Jan 15)
    B1:    含 NDTE
    B2:    weekly 无日期 → 默认本周五
    B3:    $STRIKE 在 calls 前的倒序写法
    """

    # ----- B0: 含 MM/DD -----
    p_mmdd = re.compile(
        r"\$([A-Z]{1,5})\b"
        r"[^\$\n]*?(\d{1,2})/(\d{1,2})"
        r"[^\$\n]*?\$(\d+(?:\.\d+)?)\s*(calls?|puts?)"
        r"[^\$\n]*?\$(\.?\d+(?:\.\d+)?)",
        re.IGNORECASE,
    )
    m = p_mmdd.search(text)
    if m:
        symbol, mm, dd, strike, side, price = m.groups()
        return {
            "raw": text,
            "matched": m.group(0).strip(),
            "symbol": symbol.upper(),
            "side": "CALL" if side.lower().startswith("call") else "PUT",
            "strike": float(strike),
            "expiry": f"{int(mm)}/{int(dd)}",
            "expiry_date": _adjust_expiry(
                smart_expiry(int(mm), int(dd), today=today), context="B0 MM/DD"
            ),
            "price": float(price),
            "tags": _extract_tags(text),
        }

    # ----- B0.5: $SYMBOL ... Month DD ... $STRIKE calls $PRICE -----
    p_month_name = re.compile(
        rf"\$([A-Z]{{1,5}})\b"
        rf"[^\$\n]*?({MONTH_NAMES_RE})\s+(\d{{1,2}})(?:st|nd|rd|th)?"
        rf"[^\$\n]*?\$(\d+(?:\.\d+)?)\s*(calls?|puts?)"
        rf"[^\$\n]*?\$(\.?\d+(?:\.\d+)?)",
        re.IGNORECASE,
    )
    m = p_month_name.search(text)
    if m:
        symbol, month_name, dd, strike, side, price = m.groups()
        mm = MONTH_NAME_TO_NUM[month_name.lower()]
        dd_int = int(dd)
        return {
            "raw": text,
            "matched": m.group(0).strip(),
            "symbol": symbol.upper(),
            "side": "CALL" if side.lower().startswith("call") else "PUT",
            "strike": float(strike),
            "expiry": f"{mm}/{dd_int}",
            "expiry_date": _adjust_expiry(
                smart_expiry(mm, dd_int, today=today), context="B0.5 Month DD"
            ),
            "price": float(price),
            "tags": _extract_tags(text),
        }

    # ----- B1: $SYMBOL ... NDTE ... $STRIKE calls/puts ... $PRICE -----
    p_dte_first = re.compile(
        r"\$([A-Z]{1,5})\b"
        r".*?(\d+)DTE"
        r".*?\$(\d+(?:\.\d+)?)\s*(calls?|puts?)"
        r".*?\$(\.?\d+(?:\.\d+)?)",
        re.IGNORECASE | re.DOTALL,
    )
    m = p_dte_first.search(text)
    if m:
        symbol, dte, strike, side, price = m.groups()
        return {
            "raw": text,
            "matched": m.group(0).strip(),
            "symbol": symbol.upper(),
            "side": "CALL" if side.lower().startswith("call") else "PUT",
            "strike": float(strike),
            "expiry": f"{dte}DTE",
            "expiry_date": _adjust_expiry(
                today + timedelta(days=int(dte)), context="B1 NDTE"
            ),
            "price": float(price),
            "tags": _extract_tags(text),
        }

    # ----- B1b: $SYMBOL $STRIKE calls NDTE $PRICE -----
    p_dte_mid = re.compile(
        r"\$([A-Z]{1,5})\b"
        r"[^\$\n]*?\$(\d+(?:\.\d+)?)\s*(calls?|puts?)"
        r"[^\$\n]*?(\d+)DTE"
        r"[^\$\n]*?\$(\.?\d+(?:\.\d+)?)",
        re.IGNORECASE,
    )
    m = p_dte_mid.search(text)
    if m:
        symbol, strike, side, dte, price = m.groups()
        return {
            "raw": text,
            "matched": m.group(0).strip(),
            "symbol": symbol.upper(),
            "side": "CALL" if side.lower().startswith("call") else "PUT",
            "strike": float(strike),
            "expiry": f"{dte}DTE",
            "expiry_date": _adjust_expiry(
                today + timedelta(days=int(dte)), context="B1b NDTE"
            ),
            "price": float(price),
            "tags": _extract_tags(text),
        }

    # ----- B2: $SYMBOL [weekly] $STRIKE calls/puts $PRICE （无日期） -----
    p_weekly = re.compile(
        r"\$([A-Z]{1,5})\b"
        r"[^\$\n]*?\$(\d+(?:\.\d+)?)\s*(calls?|puts?)"
        r"[^\$\n]*?\$(\.?\d+(?:\.\d+)?)",
        re.IGNORECASE,
    )
    m = p_weekly.search(text)
    if m:
        symbol, strike, side, price = m.groups()
        return {
            "raw": text,
            "matched": m.group(0).strip(),
            "symbol": symbol.upper(),
            "side": "CALL" if side.lower().startswith("call") else "PUT",
            "strike": float(strike),
            "expiry": "weekly",
            "expiry_date": _adjust_expiry(
                _next_friday(today), context="B2 weekly"
            ),
            "price": float(price),
            "tags": _extract_tags(text),
        }

    # ----- B3: $SYMBOL $STRIKE weekly calls MM/DD $PRICE -----
    p_alt = re.compile(
        r"\$([A-Z]{1,5})\b"
        r"[^\$\n]*?\$(\d+(?:\.\d+)?)\s*"
        r"(?:weekly\s+)?(calls?|puts?)"
        r"[^\$\n]*?(\d{1,2})/(\d{1,2})"
        r"[^\$\n]*?\$(\.?\d+(?:\.\d+)?)",
        re.IGNORECASE,
    )
    m = p_alt.search(text)
    if m:
        symbol, strike, side, mm, dd, price = m.groups()
        return {
            "raw": text,
            "matched": m.group(0).strip(),
            "symbol": symbol.upper(),
            "side": "CALL" if side.lower().startswith("call") else "PUT",
            "strike": float(strike),
            "expiry": f"{int(mm)}/{int(dd)}",
            "expiry_date": _adjust_expiry(
                smart_expiry(int(mm), int(dd), today=today), context="B3 MM/DD"
            ),
            "price": float(price),
            "tags": _extract_tags(text),
        }

    return None


def _extract_tags(text: str) -> list:
    tags = []
    lower = text.lower()
    for kw in ["lotto", "swing", "scalp", "daytrade", "fafo"]:
        if kw in lower:
            tags.append(kw)
    return tags


# ===== Action detection =====
CLOSE_KEYWORDS = re.compile(
    r"\b(closed?|sold|exit|stopped|trim|trimmed|out of)\b", re.I
)


def detect_action(text: str) -> str:
    if CLOSE_KEYWORDS.search(text):
        return "CLOSE"
    return "OPEN"