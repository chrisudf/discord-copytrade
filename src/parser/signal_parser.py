"""Signal parser - supports KC Trades / generic Discord format

Rules (2026-06):
1. English-only
2. No multi-leg
3. No price = no trade
4. No price range
5. expiry 相对消息时间戳计算（回测正确性）
"""
import re
from datetime import date, timedelta
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
    """
    if not text or len(text.strip()) < 5:
        return None

    today = msg_ts or date.today()

    text = _strip_chinese(text)
    if not text or len(text.strip()) < 5:
        return None

    if _has_skip_keyword(text):
        logger.info(f"[parser] skip (holding/remaining): {text[:60]}")
        return None

    if _has_price_range(text):
        logger.info(f"[parser] skip (price range): {text[:60]}")
        return None

    sig = _try_pattern_a(text, today) or _try_pattern_b(text, today)

    if sig is None:
        logger.warning(f"[parser] no signal: {text[:80]}")
        return None

    if sig.get("price") is None:
        logger.warning(f"[parser] skip (no price): {sig['matched']}")
        return None

    return sig


def _try_pattern_a(text: str, today: date):
    """Pattern A: SYMBOL STRIKEc/p MM/DD @ PRICE"""
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
            "expiry_date": smart_expiry(int(mm), int(dd), today=today),
            "price": float(price),
            "tags": _extract_tags(text),
        }

    return None


def _try_pattern_b(text: str, today: date):
    """Pattern B: 多种 $SYMBOL 形态。

    优先级：
    B0: 含明确 MM/DD（最准确）
    B1: 含 NDTE
    B2: weekly 无日期 → 默认本周五
    B3: $STRIKE 在 calls 前的倒序写法
    """

    # ----- B0: 含 MM/DD（包括 "weekly 5/15" / "4/29" 等） -----
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
            "expiry_date": smart_expiry(int(mm), int(dd), today=today),
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
            "expiry_date": today + timedelta(days=int(dte)),
            "price": float(price),
            "tags": _extract_tags(text),
        }

    # ----- B1b: $SYMBOL $STRIKE calls NDTE $PRICE （DTE 在中间） -----
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
            "expiry_date": today + timedelta(days=int(dte)),
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
            "expiry_date": _next_friday(today),
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
            "expiry_date": smart_expiry(int(mm), int(dd), today=today),
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