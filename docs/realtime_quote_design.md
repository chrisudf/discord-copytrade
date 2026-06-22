# Design: Real-env `get_last_price` implementation

Status: **draft, pre-implementation** — captures everything we know before opening the PR series. Move to "implemented" sections (or delete) as items land.

## Why

`broker.get_last_price()` currently returns `None` in real env (`DRY_RUN=false`). All three watchers (`sl_watcher`, `tp_watcher`, `eod_watcher`) treat `None` as "skip this position this tick". Production impact:

| Watcher | Real-env behavior | Risk |
|---|---|---|
| SL | Never fires | Max loss = full premium of every contract |
| TP T1/T2 | Never fires | +50%/+100% gains round-trip back to 0 |
| EOD force-close | Never fires | **0DTE positions held to expiry → 100% loss** |

The 0DTE case is the headline risk: any signal tagged `lotto` / `0dte` / `0dte_lotto` defaults `eod_force_close=True`. The whole protection is currently a no-op in prod.

## moomoo snapshot architecture (background)

Pipeline:
```
Python SDK
  ↓ TCP socket 127.0.0.1:11111
OpenD daemon (local)
  ↓ persistent TCP/TLS
Futu HK/US gateway
  ↓ subscribes to OPRA / NASDAQ / NYSE feeds
Exchange
```

A `get_market_snapshot([code])` call:
1. SDK serializes codes to protobuf, sends to local OpenD
2. OpenD checks its in-memory cache:
   - Hit → returns immediately (μs scale)
   - Miss → pulls one frame from Futu gateway (~200ms-2s)
3. Returns a DataFrame, one row per code, dozens of fields (`last_price`, `bid`, `ask`, `volume`, `update_time`, …)

### Snapshot vs subscribe

| | snapshot | subscribe + handler |
|---|---|---|
| Trigger | Client pull | Exchange push |
| Latency | Hundreds of ms - seconds | Millisecond |
| Data | One frame at call time | Every tick |
| State | Stateless | Manage subscribe/unsubscribe + quota |
| OPRA billing | Each call counts | Single charge per subscription |
| Implementation complexity | Low | High (callback threads, lifecycle, reconnect) |

Snapshot's `last_price` is the **last trade price**, not necessarily current — it can be seconds or even minutes old for illiquid OTM options.

### Rate limits (Futu public docs + observed)

- Option snapshot: ~**60 calls / 30s** (each code counts)
- Repeat snapshot for same code within window: hits local OpenD cache, doesn't count
- Over limit: `ret != RET_OK`, message contains `request quota exceeded`
- OPRA real-time data: **separate paid subscription**; default account = **15-min delayed**

Last point is critical: a default-tier account returns 15-min-old prices. Useless for 0DTE EOD decisions. **Must verify quote tier at startup.**

## Plan: three independent PRs

### PR 1: `feat(broker): real-env get_last_prices via snapshot`

Add `OpenQuoteContext` singleton + batch snapshot. Keep the single-code `get_last_price(code)` API for compatibility, route it through the batch path internally. Watchers unchanged in this PR.

```python
_quote_ctx_singleton: Optional[OpenQuoteContext] = None
_quote_lock = threading.Lock()
_quota_backoff_until: float = 0.0

def _quote_ctx() -> OpenQuoteContext:
    global _quote_ctx_singleton
    if _quote_ctx_singleton is None:
        _quote_ctx_singleton = OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT)
    return _quote_ctx_singleton

def _reset_quote_ctx() -> None:
    global _quote_ctx_singleton
    if _quote_ctx_singleton is not None:
        try: _quote_ctx_singleton.close()
        except Exception: pass
    _quote_ctx_singleton = None

def get_last_prices(codes: list[str]) -> dict[str, Optional[float]]:
    out: dict[str, Optional[float]] = {c: None for c in codes}
    if _is_dry_run():
        return {c: _mock_price(c) for c in codes}
    if not codes:
        return out
    if time.time() < _quota_backoff_until:
        return out  # silent skip during quota backoff

    try:
        with _quote_lock:
            ret, df = _quote_ctx().get_market_snapshot(codes)
    except Exception:
        logger.exception("[broker] snapshot exception")
        _reset_quote_ctx()
        return out

    if ret != RET_OK:
        msg = str(df)
        if "quota" in msg.lower():
            global _quota_backoff_until
            _quota_backoff_until = time.time() + 60
            logger.warning("[broker] snapshot quota exceeded, backoff 60s")
        else:
            logger.warning(f"[broker] snapshot ret={ret}: {msg[:200]}")
        return out

    if df.empty:
        return out

    now = time.time()
    for _, row in df.iterrows():
        code = row.get("code")
        last = row.get("last_price")
        if pd.isna(last) or last is None or last <= 0:
            continue
        # Freshness check — protects against stale OpenD cache after reconnect
        try:
            ts = pd.to_datetime(row["update_time"]).timestamp()
            if now - ts > 60:
                continue
        except Exception:
            pass  # if update_time missing, trust the snapshot
        out[code] = float(last)
    return out

def get_last_price(option_code: str) -> Optional[float]:
    return get_last_prices([option_code]).get(option_code)
```

**Tests** (DRY_RUN path can be unit-tested; real path needs integration gate):
- `INTEGRATION_TESTS=1`: snapshot a known-liquid option, assert `last_price > 0`
- Mock `get_market_snapshot` to return empty df → expect `None`
- Mock to return `last_price = NaN` → expect `None`
- Mock to raise → expect `_reset_quote_ctx` called, return `None`
- Mock quota error → expect 60s backoff state set

### PR 2: `refactor(watcher): batch prefetch prices per tick`

Switch SL/TP/EOD `_tick` from N×`get_last_price(code)` to one `get_last_prices(codes)`. Each tick: 1 RTT instead of N. Avoids quota burn when multiple positions open.

Before:
```python
for pos in positions:
    last = await asyncio.to_thread(get_last_price, pos["option_code"])
    if last is None: continue
    ...
```

After:
```python
codes = [p["option_code"] for p in positions]
prices = await asyncio.to_thread(get_last_prices, codes)
for pos in positions:
    last = prices.get(pos["option_code"])
    if last is None: continue
    ...
```

Watchers each do their own prefetch (SL/TP/EOD have different position filters), but within a tick batch is single RTT.

Also: wrap the `to_thread` call in `asyncio.wait_for(timeout=2.0)`. Snapshot occasionally hangs (the 6/18 Discord-reconnect TODO). Timeout → `_reset_quote_ctx` and skip this tick.

### PR 3: `feat(broker): OPRA permission check on startup`

Before any watcher starts, probe whether quote tier supports real-time options:

```python
def assert_realtime_options_available() -> bool:
    if _is_dry_run():
        return True  # DRY_RUN doesn't need real quotes
    try:
        ret, df = _quote_ctx().get_market_snapshot(["US.AAPL"])
        if ret != RET_OK:
            logger.error(f"[broker] startup quote probe failed: {df}")
            return False
        if df.empty:
            return False
        # Delayed data has update_time ~15min behind now
        ts = pd.to_datetime(df.iloc[0]["update_time"]).timestamp()
        lag = time.time() - ts
        if lag > 300:
            logger.error(
                f"[broker] ⚠️ quote lag {lag:.0f}s — likely delayed-data tier."
                " Real-time OPRA subscription required for watchers to work."
            )
            return False
        return True
    except Exception:
        logger.exception("[broker] startup quote probe exception")
        return False
```

`start_listener` checks this. If false: `_safe_notify` a loud warning to Telegram, leave watchers running (they'll just no-op the prices are stale) **or** refuse to start them. Open question: should the bot refuse to start orders too? Probably not — manual trading from KC signals is still useful even without auto-exits. Just **make the degraded mode loud and explicit**.

## Bugs / pitfalls (read before writing code)

### 1. `last_price = 0` or `NaN` on illiquid OTM
Cold OTM contract with no trades today returns `0.0` or `NaN`. Naively casting to float and feeding to SL = instant "price dropped to zero" trigger. Always check `pd.isna(last) or last <= 0`.

### 2. `code` column format drift
Subscribe path returns `US.AAPL250117C200000`; some SDK versions strip the `US.` prefix in snapshot results. Reverse-lookup into your prefetch dict fails silently. Normalize on read.

### 3. OPRA vs Futu code format roundtrip
OPRA: `AAPL  250117C00200000` (space-padded, fixed width). Futu: `US.AAPL250117C200000` (no spaces, variable). SDK conversion occasionally fails on fractional strikes (`1.5` → `001500` vs `00001500`) — `ret=RET_OK` but `df` is empty. Empty df is not an error condition, treat as "no data".

### 4. `update_time` freshness
Snapshot doesn't flag staleness. Always compute lag from `update_time`; > 60s = treat as `None`. Without this check, delayed-data tier accounts will use 15-min-old prices for EOD decisions on 0DTE.

### 5. OpenD reconnect serves stale cache
OpenD ↔ Futu gateway dropping isn't visible from Python. Snapshot still returns — the last cached frame from before the drop. `get_global_state()` exposes `local_to_server_time`; if skew > 30s, OpenD is likely disconnected and cache is unreliable.

### 6. Pre-market returns previous-day close
9:00-9:30 ET: snapshot returns yesterday's close, `update_time` is yesterday. Watcher started in pre-market would tick on stale prices. Gate watcher logic on ET market hours (09:30-16:00).

### 7. Quota error retry cascade
Hit quota → `ret=Quota_Exceeded`. If watcher just retries on next 5s tick, you're still over the rolling window. Three watchers retrying simultaneously = 3x amplification. Implement shared backoff (≥ 60s).

### 8. Per-contract vs per-share pricing
`last_price` is **per share**. Contract notional = `last_price * 100 * qty`. DRY_RUN mock already handles this; easy to forget when reading real values.

### 9. SDK thread safety
`OpenQuoteContext` internals use background threads for callbacks. **Concurrent snapshot calls from multiple watchers can return interleaved DataFrames.** Wrap calls in `threading.Lock`. (Selection of design C — single batch per tick — naturally serializes; the lock is defense-in-depth.)

### 10. `close()` makes instance unusable
Same trap as `trd_ctx`: calling `quote_ctx.close()` doesn't null the singleton. Next call uses dead instance. Mirror the existing `_ctx` / `_reset_ctx` pattern.

### 11. SDK blocking calls starve thread pool
This is the 6/18 reconnect TODO noted in [moomoo_client.py:425](../src/broker/moomoo_client.py). `asyncio.to_thread` defers to the default ThreadPoolExecutor (size `min(32, cpu_count+4)`). One hung SDK call = one occupied thread. Enough hangs = pool exhausted = Discord heartbeat ticks queue behind broker calls = reconnect storm.

**Mitigation**: `asyncio.wait_for(asyncio.to_thread(...), timeout=2.0)`. Timeout cancels the wait, but the thread itself keeps running until SDK returns (Python threads can't be killed). If SDK truly hangs forever, threads accumulate — but at < 1/hour you have headroom for days, and the loud restart-on-reset gives you signal to act.

## Open questions for review

1. **Quote tier degraded mode**: refuse to start watchers, or run them no-op with loud warning? Lean: no-op + loud warning. Manual trade still useful.
2. **Subscribe path later?** If snapshot quota or latency proves insufficient with N > 10 positions, upgrade to subscribe. Punt until measured.
3. **`get_last_price` (single-code) caller compatibility**: keep wrapping `get_last_prices`, or deprecate? PR 2 will mostly route through batch; the single-code API only lingers for ad-hoc callers (none currently — verified via grep).
4. **Backoff state shared across watchers**: `_quota_backoff_until` is module-level. Probably right — SL/TP/EOD all hit the same quota bucket. Worth documenting.

## Acceptance criteria (for PR series merge)

- [ ] PR 1: unit tests cover all 7 documented edge cases (empty, NaN, stale, quota, exception, mock prices, normal). Real-env path gated behind `INTEGRATION_TESTS=1`.
- [ ] PR 2: each watcher tick does ≤ 1 snapshot call regardless of N positions. Confirmed via mock call count.
- [ ] PR 3: startup probe + Telegram warning. Verified by setting `MOOMOO_TRD_ENV=REAL` against a delayed-data account.
- [ ] Manual smoke: 1 paper-trade 0DTE, watch SL/TP fire (not no-op), verify EOD strike at 15:50 ET.
