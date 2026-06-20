"""CLOSE 信号解析

设计基于 6/14-6/18 真实样本（详见对话 review）：
- 20 条 close-ish 消息中 40% 是复盘/计划噪音，必须先过滤
- 70% 的可执行信号是 "trimmed/Selling X%"（部分平），不是全平
- 几乎所有 close 信号都不带 strike → symbol 级匹配
- "NOW" 在 close 信号里既可能是 ServiceNow 也可能是副词
  → 必须用 open_symbols 白名单消歧

返回结构：
    {
        "kind": "CLOSE" | "BULK_TRIM",
        "symbols": [...],   # BULK_TRIM 时空
        "pct": int,         # 卖出百分比 1-100
        "matched": str,     # debug 用
    }
    或 None（噪音/无法定位 symbol）

中文 fallback：
- 历史数据：63% 双语对子中文先到，但 100% 配对率（gap<60s 内英文必到）
- 实际链路：parse_close → 先 EN 路径 → 失败 fallback 到 ZH 路径
- 双发场景靠 listener 的 CLOSE fingerprint dedup（symbols+pct+kind）兜底
- ZH 路径只用 $SYMBOL + 白名单裸 ticker 两档
  → KC 翻译中文公司名（亚马逊/微软等）抓不到 → 静默 None → 1-3s 后 EN 版本兜底
  → 兜底失败时 log [zh_unrecognized] warning 便于事后排查

TODO（实测调整）：
- enrich 长段多 symbol 列表（"Trimmed the following:\n$APLD ...\n$CRWV ..."）
  当前 parser 会把所有 $XXX 都返回，需要进一步按行拆分各自的 pct
- "stop moved to entry" / "stops to BE" 是元信号，不是 close（应跳过，已在 RECAP）
- ACTION_DONE 里的 "took" 单独看可能误触（"took the trade"），暂依赖 RECAP_MARKERS 兜底
- 若 [zh_unrecognized] warning 频繁，再考虑数据驱动的中文名→ticker 自动学习
  （EN 版本成功时关联同时段 ZH 文本里的未知中文名）
"""
import re
from typing import Optional

from src.utils.logger import logger


# 复盘/计划标记 —— 出现任一即跳过
# 注意：必须先于 ACTION 检测，因为 "scaled some yesterday" 含 "scaled" 但不动手
RECAP_MARKERS = [
    "yesterday", "this morning", "earlier today",
    "(so far)", "so far,",  # PnL 复盘 "(so far) the account"，不拦 "so far before FOMC"
    "the plan", "my plan", "here's my plan",
    "have an order", "will be ", "going to ",
    "out of 4", "out of 5",  # PnL 复盘 "2 losing out of 4"
]

# bulk action —— 不指定 symbol，对所有持仓批量 trim
BULK_MARKERS = [
    "all positions", "every position", "everything",
]

# 当前动作（gerund / 完成时）—— 真要动手的信号
ACTION_VERBS = [
    "trimming", "trimmed",
    "cutting", "cut ",        # "cut " 加空格避免匹配 "scout/circuit"
    "selling", "sold here",
    "closing", "closed",
    "dumping", "dumped",
    "scaling out",
    "bang!", "bang -",        # KC 的情绪触发词，通常配 trim
]

# 全平动词（pct 缺省 → 100）
FULL_CLOSE_VERBS = ["closed", "cutting", "cut ", "dumped", "dumping"]

# 提取百分比："25%" / "20 %"
# 排除 `-15%` `+30%` 这类 PnL 标注（前面有符号/数字 → 不是 trim 比例）
PCT_PATTERN = re.compile(r"(?<![-+\d.])(\d{1,3})\s*%")

# 提取 $SYMBOL（强信号）
DOLLAR_SYM_PATTERN = re.compile(r"\$([A-Z]{1,5})\b")

# 提取裸 SYMBOL（弱信号，需 open_symbols 白名单验证）
BARE_SYM_PATTERN = re.compile(r"\b([A-Z]{2,5})\b")

# Chinese 友好版：用 lookahead/lookbehind 在字母/数字边界判定，
# 这样 "减仓IWM" 也能正确抓 IWM（Python re 把中文当 \w，\b 在汉字-字母处不触发）
BARE_SYM_PATTERN_ZH = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2,5})(?![A-Za-z0-9])")

# "N% LEFT" / "down to N% runners" / "runners only" → 卖 (100-N)%
LEFT_PATTERN = re.compile(r"(\d{1,3})\s*%\s*(?:left|remaining)", re.I)

# KC 喊出的卖出价 —— 用来挂卖单限价，避免按 entry × 0.95 倒挂
# 场景：
#   `trimmed MSFT 420c @ 7.00`     ← @ + 价
#   `trimmed IWM @ 2.45`           ← @ + 价
#   `BANG! Trimmed another IWM here @ 2.80` ← @ + 价
#   `trimmed TSLA 3.00`            ← 无 @，裸 X.XX
# 排除：strike ("$420")、pct ("25%")、PnL ("-15%")、合约后缀 (420c)
PRICE_AT_PATTERN = re.compile(r"@\s*\$?(\.?\d+(?:\.\d+)?)")
# 裸 X.XX：必须带小数点，且前面不是 $ 或数字，后面不是 c/p/%/ 数字
PRICE_BARE_PATTERN = re.compile(
    r"(?<![\$\d.])(\d+\.\d{1,2})(?![cp%\d])",
    re.IGNORECASE,
)


def _extract_signal_price(scope: str) -> "float | None":
    """从 action 句 scope 抽取 KC 喊的卖出价。

    返回 None → caller fallback 到 entry-based 算法
    """
    m = PRICE_AT_PATTERN.search(scope)
    if m:
        try:
            v = float(m.group(1))
            if 0.01 <= v <= 100:  # 期权合理价区间
                return v
        except ValueError:
            pass
    m = PRICE_BARE_PATTERN.search(scope)
    if m:
        try:
            v = float(m.group(1))
            if 0.01 <= v <= 100:
                return v
        except ValueError:
            pass
    return None


# === KC 喊的 PnL (% 标注) ===
# 场景：
#   "closed NOW small day trade -15%"      → -15
#   "Closed IREN 60c for +30%"             → +30
#   "closed NOW at entry"                  → 0   （持平退出）
#   "trimmed @ break even"                 → 0
# 不同于 trim 比例 ("Selling 25%")：PnL 必须带正负号 / 或 "at entry" 之类显式标记。
PNL_SIGNED_PATTERN = re.compile(r"([+\-])\s*(\d{1,3}(?:\.\d+)?)\s*%")
AT_ENTRY_PATTERN = re.compile(
    r"\bat\s+(?:entry|breakeven|break\s*even|be)\b", re.I
)
# 中文版："在进场位 / 在入场位 / 保本 / 平本"
ZH_AT_ENTRY_PATTERN = re.compile(r"在\s*(?:进场|入场)位|保本|平本")


def _extract_signal_pnl(scope: str, is_zh: bool = False) -> "float | None":
    """从 action 句 scope 抽 KC 报告的 PnL（%）。

    返回 None → 没找到（不代表 PnL 为零，而是不知道）
    返回 0    → 显式 "at entry" / "保本"
    返回 -15  → "-15%"
    """
    # 显式持平
    if AT_ENTRY_PATTERN.search(scope):
        return 0.0
    if is_zh and ZH_AT_ENTRY_PATTERN.search(scope):
        return 0.0

    # 带符号 N%
    m = PNL_SIGNED_PATTERN.search(scope)
    if m:
        sign = -1.0 if m.group(1) == "-" else 1.0
        try:
            v = float(m.group(2))
            if 0 <= v <= 1000:  # 合理 PnL 范围（lotto +500% 也可能）
                return sign * v
        except ValueError:
            pass
    return None


# ============================================================
# 中文 fallback
# ============================================================
#
# 不维护中文公司名→ticker 映射（亚马逊/微软等）。
# 这类信号靠 1-3s 后到达的 EN 版本兜底；本路径主要处理：
#   - $SYMBOL（enrich 中文版常保留 $）
#   - 裸 ticker（IWM/SPY/QQQ 等不翻译的）+ 白名单消歧
# ZH 抓到 action 动词但没 symbol 时 → log [zh_unrecognized] 便于事后追踪。

# 复盘/计划 —— 出现即跳
# 注意区分 "卖出了一些" (recap) vs "又减仓了一笔" (announcement)：
# 前者必带"时"/"在 N% 时"/"昨天"等过去时间锚；后者是当下宣告
ZH_RECAP_MARKERS = [
    "昨天", "昨日", "今早", "今天早些", "早些时候",
    "我的计划", "的计划是", "计划：",
    "打算", "准备", "即将", "将要", "将把",  # 未来意图
    "时卖出", "时减仓", "时清", "时砍", "时抛",  # "在 40% 时卖出"
    "了一些",  # "卖出了一些" 多为复盘；与 "了一笔" / "了一份" 区分
]

# bulk action
ZH_BULK_MARKERS = ["所有持仓", "全部持仓", "全部仓位"]

# 当前动作动词
ZH_ACTION_VERBS = [
    "减仓", "平仓", "清仓", "全平", "清空",
    "卖出", "卖了",
    "砍掉", "砍仓",
    "抛出", "抛了",
    "止盈",
]

# 全平动词 / 短语（pct 缺省 → 100）
ZH_FULL_CLOSE_VERBS = ["平仓", "清仓", "全平", "清空", "全部卖出", "全部抛"]

# "剩下 30%" / "剩 N%" → 卖 (100-N)%
ZH_LEFT_PATTERN = re.compile(r"剩\s*下?\s*(\d{1,3})\s*%")


def _has_recap_marker(text_lower: str) -> bool:
    return any(m in text_lower for m in RECAP_MARKERS)


def _has_bulk_marker(text_lower: str) -> bool:
    return any(m in text_lower for m in BULK_MARKERS)


def _has_action_verb(text_lower: str) -> bool:
    return any(v in text_lower for v in ACTION_VERBS)


def _has_full_close_verb(text_lower: str) -> bool:
    return any(v in text_lower for v in FULL_CLOSE_VERBS)


_ACTION_RE = re.compile("|".join(re.escape(v) for v in [
    "trimming", "trimmed", "cutting", "cut ", "selling", "sold here",
    "closing", "closed", "dumping", "dumped", "scaling out", "bang!", "bang -",
]), re.IGNORECASE)


def _action_sentences(text: str) -> str:
    """返回包含动作动词的句子拼接。

    用 . ! ? 分句，但避免在小数点处切（`@ 2.45` 必须保持完整）。
    场景：'closed the NOW small day trade -15%. Just hanging out now after 3/3 on AMZN MSFT swings.'
    第一句有 closed 抓 NOW；第二句是复盘，不该抓 AMZN/MSFT。
    若没有任何句子命中（整段一句话），fallback 用整段 —— 不漏召回。
    """
    # 分隔符：句末 .!? 后跟空白（且 .!? 前后不是数字 → 排除小数点）
    sentences = re.split(r"(?<!\d)[.!?](?!\d)\s+|(?<=[.!?])(?=\s)", text)
    hit = [s for s in sentences if _ACTION_RE.search(s)]
    return " ".join(hit) if hit else text


def _extract_symbols(text: str, open_symbols: set[str]) -> list[str]:
    """抽 symbol。优先 $SYMBOL；只有完全没有 $ 标记时才 fallback 到裸 SYMBOL。

    设计原因：enrich 长消息里"$HOOD - NOW ITM"，NOW 在持仓白名单也会被误匹配。
    既然作者用了 $ 标注，就只信 $ —— 这是 KC/enrich 的明确约定。
    KC Bot 风格（"trimmed AMZN"）不带 $，才需要白名单兜底。

    扫描范围限制在含 action 动词的句子内，避免抓到下一句 commentary 里的 ticker。
    """
    # 两遍扫描：先 action 句 scope，没抓到再 fallback 全文
    # 场景：'$HOOD - Nobody let these go red. Selling 25% here.' —— $HOOD 在第一句，
    # 动作在第二句，分句后 action scope 没 $HOOD，需要 fallback。
    for scope in (_action_sentences(text), text):
        found = []
        seen = set()
        for m in DOLLAR_SYM_PATTERN.finditer(scope):
            s = m.group(1)
            if s not in seen:
                found.append(s)
                seen.add(s)
        if found:
            return found
        for m in BARE_SYM_PATTERN.finditer(scope):
            s = m.group(1)
            if s in seen:
                continue
            if s in open_symbols:
                found.append(s)
                seen.add(s)
        if found:
            return found
    return []


def _extract_pct(text: str, text_lower: str) -> int:
    """提取卖出百分比。

    规则：
    - "30% LEFT" / "30% remaining" → 卖 70%
    - "Selling 25%" → 卖 25%
    - 无 % 数字：FULL_CLOSE_VERBS → 100%，否则 33%（trim 默认）

    扫描范围限制在 action 句内，避免抓到 commentary 里的 PnL %。
    PCT_PATTERN 已经排除了 -N% / +N%（PnL 标注）。

    TODO: KC "trimmed @ price" 没有 %，默认 33% 是经验值，实测后调
    """
    scope = _action_sentences(text)

    left_m = LEFT_PATTERN.search(scope)
    if left_m:
        n = int(left_m.group(1))
        return max(1, min(100, 100 - n))

    pct_m = PCT_PATTERN.search(scope)
    if pct_m:
        n = int(pct_m.group(1))
        return max(1, min(100, n))

    return 100 if _has_full_close_verb(text_lower) else 33


def _parse_close_en(text: str, open_symbols: set[str]) -> Optional[dict]:
    """英文路径（原 parse_close 逻辑）。"""
    text_lower = text.lower()
    if _has_recap_marker(text_lower):
        logger.info(f"[close_parser] EN skip (recap): {text[:80]}")
        return None
    if not _has_action_verb(text_lower):
        return None

    scope_en = _action_sentences(text)
    signal_price = _extract_signal_price(scope_en)
    signal_pnl_pct = _extract_signal_pnl(scope_en, is_zh=False)

    if _has_bulk_marker(text_lower):
        pct = _extract_pct(text, text_lower)
        if pct == 33:
            pct = 50
        logger.info(f"[close_parser] EN BULK_TRIM pct={pct}")
        return {"kind": "BULK_TRIM", "symbols": [], "pct": pct,
                "signal_price": signal_price, "signal_pnl_pct": signal_pnl_pct,
                "matched": text[:120], "lang": "en"}

    symbols = _extract_symbols(text, open_symbols)
    if not symbols:
        return None
    pct = _extract_pct(text, text_lower)
    logger.info(
        f"[close_parser] EN CLOSE symbols={symbols} pct={pct} "
        f"price={signal_price} pnl={signal_pnl_pct} text={text[:80]}"
    )
    return {"kind": "CLOSE", "symbols": symbols, "pct": pct,
            "signal_price": signal_price, "signal_pnl_pct": signal_pnl_pct,
            "matched": text[:120], "lang": "en"}


# ---- 中文 helpers ----

def _has_zh_recap(text: str) -> bool:
    return any(m in text for m in ZH_RECAP_MARKERS)


def _has_zh_action(text: str) -> bool:
    return any(v in text for v in ZH_ACTION_VERBS)


def _has_zh_bulk(text: str) -> bool:
    return any(m in text for m in ZH_BULK_MARKERS)


def _has_zh_full_close(text: str) -> bool:
    return any(v in text for v in ZH_FULL_CLOSE_VERBS)


def _zh_action_sentences(text: str) -> str:
    """按中文句号 / 英文句号切，留含动作动词的句子。

    `.` 在数字之间不切（如 '@ 2.45'）。中文 `。！？` 总是切。
    """
    sents = re.split(r"[。！？]|(?<!\d)[.!?](?!\d)", text)
    hit = [s for s in sents if any(v in s for v in ZH_ACTION_VERBS)]
    return " ".join(hit) if hit else text


def _extract_zh_symbols(text: str, open_symbols: set[str]) -> list[str]:
    """中文版 symbol 抽取：$SYMBOL → 裸 ticker 白名单。

    两遍扫描：先在 action 句 scope 找，没找到再 fallback 全文（同 EN 路径）。
    用 BARE_SYM_PATTERN_ZH 避免汉字-字母边界 \b 失效。

    不处理中文公司名（亚马逊→AMZN 之类）—— 这类信号靠 EN 版本兜底。
    """
    for scope in (_zh_action_sentences(text), text):
        found = []
        seen = set()
        # 1. $SYMBOL（enrich 中文版常保留）
        for m in DOLLAR_SYM_PATTERN.finditer(scope):
            s = m.group(1)
            if s not in seen:
                found.append(s); seen.add(s)
        if found:
            return found
        # 2. 裸 ticker + 白名单消歧（IWM/SPY/QQQ 等不被翻译的）
        for m in BARE_SYM_PATTERN_ZH.finditer(scope):
            s = m.group(1)
            if s in seen:
                continue
            if s in open_symbols:
                found.append(s); seen.add(s)
        if found:
            return found
    return []


def _extract_zh_pct(text: str) -> int:
    """中文版百分比抽取。"""
    scope = _zh_action_sentences(text)

    left_m = ZH_LEFT_PATTERN.search(scope)
    if left_m:
        n = int(left_m.group(1))
        return max(1, min(100, 100 - n))

    pct_m = PCT_PATTERN.search(scope)
    if pct_m:
        return max(1, min(100, int(pct_m.group(1))))

    return 100 if _has_zh_full_close(text) else 33


def _parse_close_zh(text: str, open_symbols: set[str]) -> Optional[dict]:
    """中文 fallback 路径。"""
    if _has_zh_recap(text):
        logger.info(f"[close_parser] ZH skip (recap): {text[:80]}")
        return None
    if not _has_zh_action(text):
        return None

    scope_zh = _zh_action_sentences(text)
    signal_price = _extract_signal_price(scope_zh)
    signal_pnl_pct = _extract_signal_pnl(scope_zh, is_zh=True)

    if _has_zh_bulk(text):
        pct = _extract_zh_pct(text)
        if pct == 33:
            pct = 50
        logger.info(f"[close_parser] ZH BULK_TRIM pct={pct}")
        return {"kind": "BULK_TRIM", "symbols": [], "pct": pct,
                "signal_price": signal_price, "signal_pnl_pct": signal_pnl_pct,
                "matched": text[:120], "lang": "zh"}

    symbols = _extract_zh_symbols(text, open_symbols)
    if not symbols:
        logger.warning(
            f"[close_parser] [zh_unrecognized] ZH close intent but symbol "
            f"not extractable (likely Chinese company name): {text[:120]}"
        )
        return None
    pct = _extract_zh_pct(text)
    logger.info(
        f"[close_parser] ZH CLOSE symbols={symbols} pct={pct} "
        f"price={signal_price} pnl={signal_pnl_pct} text={text[:80]}"
    )
    return {"kind": "CLOSE", "symbols": symbols, "pct": pct,
            "signal_price": signal_price, "signal_pnl_pct": signal_pnl_pct,
            "matched": text[:120], "lang": "zh"}


def parse_close(text: str, open_symbols: set[str]) -> Optional[dict]:
    """CLOSE 信号解析（顶层 dispatcher）。

    流程：
      EN 路径 → 命中返回
      ZH 路径 → 命中返回
      都没命中 → None

    返回 dict 多了 'lang' 字段标识哪个路径命中，方便日志和回归。
    """
    if not text or len(text.strip()) < 3:
        return None

    return _parse_close_en(text, open_symbols) or _parse_close_zh(text, open_symbols)
