"""Signal parser - supports KC Trades / generic Discord format"""
import re
from datetime import date, timedelta
from src.utils.logger import logger


# ===== 跨年容错 =====
def smart_expiry(mm: int, dd: int, today: date = None) -> date:
    """
    Resolve MM/DD to a date with year auto-correction.
    Rule: pick the nearest future date within +/- 6 months.
    """
    today = today or date.today()
    candidates = [
        date(today.year, mm, dd),
        date(today.year + 1, mm, dd),
        date(today.year - 1, mm, dd),
    ]
    # Filter: must be within next ~10 months, not too far in past
    future = [d for d in candidates if d >= today - timedelta(days=2)]
    if not future:
        return candidates[0]
    # Pick the closest one in the future
    return min(future, key=lambda d: abs((d - today).days))


def parse_signal(text: str):
    """
    Parse signals like:
      "AMZN 260c 7/17 @ 2.64 swing"
      "MSFT 440c 7/17 @ 3.05"
      "Lotto $IREN 0DTE $60 calls $.68"
      "SMH 530p 6/18 lotto swing @ 8.20"

    Returns list of signals (one msg may contain multiple), or None.
    """
    if not text or len(text.strip()) < 5:
        return None

    signals = []

    # ===== Pattern A: SYMBOL STRIKE+c/p MM/DD ... @ PRICE =====
    # e.g. "AMZN 260c 7/17 @ 2.64", "MSFT 440c 7/17 @ 3.05"
    pattern_a = re.compile(
        r"\b([A-Z]{1,5})\s+"          # symbol
        r"(\d+(?:\.\d+)?)([cp])\s+"   # strike + c/p
        r"(\d{1,2})/(\d{1,2})"        # MM/DD
        r"(?:[^@\n]*?@\s*\$?(\d+(?:\.\d+)?))?",  # optional @ price
        re.IGNORECASE,
    )

    for m in pattern_a.finditer(text):
        symbol, strike, cp, mm, dd, price = m.groups()

        # Filter false positives (common English words)
        if symbol.upper() in {"I", "A", "THE", "AT", "ON", "IS", "DTE", "IPO"}:
            continue

        sig = {
            "raw": text,
            "matched": m.group(0).strip(),
            "symbol": symbol.upper(),
            "side": "CALL" if cp.lower() == "c" else "PUT",
            "strike": float(strike),
            "expiry": f"{int(mm)}/{int(dd)}",
            "expiry_date": smart_expiry(int(mm), int(dd)),
            "price": float(price) if price else None,
            "tags": _extract_tags(text),
        }

        if sig["price"] is None:
            # Try to find "filled @ X" anywhere in text
            filled = re.search(r"filled\s*@\s*\$?(\d+(?:\.\d+)?)", text, re.I)
            if filled:
                sig["price"] = float(filled.group(1))

        if sig["price"] is None:
            logger.warning(f"No price found for {sig['matched']}")
            continue

        signals.append(sig)

    # ===== Pattern B: $SYMBOL 0DTE/1DTE $STRIKE calls/puts $PRICE =====
    # e.g. "Lotto $IREN 0DTE $60 calls $.68"
    pattern_b = re.compile(
        r"\$([A-Z]{1,5})\b"                            # $SYMBOL
        r".*?(\d+)DTE"                                 # NDTE
        r".*?\$(\d+(?:\.\d+)?)\s*(calls?|puts?)"       # $strike calls/puts
        r".*?\$(\.?\d+(?:\.\d+)?)",                    # $price
        re.IGNORECASE | re.DOTALL,
    )

    for m in pattern_b.finditer(text):
        symbol, dte, strike, side, price = m.groups()
        sig = {
            "raw": text,
            "matched": m.group(0).strip(),
            "symbol": symbol.upper(),
            "side": "CALL" if side.lower().startswith("call") else "PUT",
            "strike": float(strike),
            "expiry": f"{dte}DTE",
            "expiry_date": date.today() + timedelta(days=int(dte)),
            "price": float(price),
            "tags": _extract_tags(text),
        }
        # Dedupe
        if not any(s["matched"] == sig["matched"] for s in signals):
            signals.append(sig)

    if not signals:
        logger.warning(f"No signal parsed from: {text[:80]}")
        return None

    # Return single dict for backward compat, list if multiple
    return signals[0] if len(signals) == 1 else signals


def _extract_tags(text: str) -> list:
    """Extract trade-style tags."""
    tags = []
    lower = text.lower()
    for kw in ["lotto", "swing", "scalp", "daytrade", "fafo"]:
        if kw in lower:
            tags.append(kw)
    return tags


# ===== Action detection (open / close / trim) =====
CLOSE_KEYWORDS = re.compile(
    r"\b(closed?|sold|exit|stopped|trim|trimmed|out of)\b", re.I
)

def detect_action(text: str) -> str:
    """Return 'CLOSE' if it's an exit signal, else 'OPEN'."""
    if CLOSE_KEYWORDS.search(text):
        return "CLOSE"
    return "OPEN"