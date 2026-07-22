# Lessons learned

Hard-to-find gotchas discovered the painful way during this refactor.
Filed here so future-me (or anyone else) doesn't re-spend hours on the
same surprise. Each entry: **what was wrong**, **how it looked**, **why
the lesson is non-obvious**, **how we now defend against it**.

---

## 1. moomoo SIMULATE does **not** accept `unlock_trade`

**Symptom**: All overnight signals fail at order submission with
`unlock_trade failed: ERROR. No one available account!` even though
`get_acc_list` shows the account as ACTIVE.

**Trigger condition**: `MOOMOO_TRD_ENV=SIMULATE` AND `MOOMOO_TRD_PWD`
is non-empty. Was latent for months because `MOOMOO_TRD_PWD` was unset
in .env, so the old `if not TRADE_PWD: skip unlock` branch saved us.
The bug appeared the moment a real-trade password got configured.

**Why non-obvious**:
- The error message says "No one available account" — pointing to
  account state, not to "you shouldn't be calling unlock_trade".
- get_acc_list shows the account fine. The accounts are there;
  unlock_trade just doesn't apply to them in SIMULATE.
- moomoo docs do not warn about this.

**Defense**: `_ensure_unlocked` now always skips unlock in SIMULATE,
regardless of password. REAL mode additionally fails fast if
TRADE_PWD is missing rather than calling unlock_trade with `""`.
See [src/broker/moomoo_client.py](../src/broker/moomoo_client.py)
`_ensure_unlocked()` + `probe_broker()` startup check.

---

## 2. moomoo SDK `get_market_snapshot` poisons whole batch on one bad ticker

**Symptom**: Calling `get_market_snapshot(["US.HOOD...", "US.TEM..."])`
returns `(-1, "Unknown stock. TEM260717C065000")` — the whole call
fails, and we never see HOOD's data even though HOOD is valid.

**Why non-obvious**:
- SDK docs imply batch is just "snapshot for multiple codes".
- The natural mental model is "valid codes get data, invalid ones
  return None or empty rows" (like most REST batch APIs).
- Reality: the SDK treats the input as a single conceptual query;
  any unknown code is fatal for the whole call.

**Defense**: `validate_option_codes` first tries batch; on failure it
falls back to per-code snapshot, isolating bad codes so good ones
still get validated. Adds N RTTs only when batch failed — fast path
stays cheap. See `validate_option_codes` in
[src/broker/moomoo_client.py](../src/broker/moomoo_client.py).

---

## 3. moomoo OPRA permission gates **more** APIs than just snapshot

**Symptom**: `probe_quote_access` step 4 (option snapshot) was
expected to fail without OPRA subscription, but **step 3
(get_option_chain) already fails** with the same "No permission"
error. The probe then misclassifies the failure as `QUOTE_ERROR`
instead of `QUOTE_NO_PERMISSION`.

**Why non-obvious**:
- `get_option_chain` reads listing metadata (which contracts exist),
  not real-time quotes. We assumed it would work for free.
- moomoo's permission system bundles "anything that touches options
  data" together, even discovery queries.

**Defense**: `probe_quote_access` now matches "no permission" on
**every** OPRA-adjacent API call (chain, snapshot, etc.) and routes
them all to `QUOTE_NO_PERMISSION` instead of `QUOTE_ERROR`. So the
operator gets the right "subscribe Lv1" message instead of a confusing
"chain fetch failed" error.

---

## 4. pandas `pd.to_datetime(naive_str).timestamp()` assumes UTC; Python `datetime` assumes local

**Symptom**: Stale-quote filter test failed by ~9h. Mock fixture
constructed `update_time` from `pd.Timestamp.now()` (local), broker
code parsed it back with `pd.to_datetime(...).timestamp()` and got a
value 9h off, so a "5min old" timestamp looked like "9h5min old".

**Why non-obvious**:
- Python's stdlib: `datetime.fromisoformat(...).timestamp()` on a
  naive datetime assumes local-zone.
- pandas: `pd.to_datetime(naive_str).timestamp()` assumes **UTC**.
- They look identical at the call site, behave 5-10 hours different.

**Defense**: All test fixtures build `update_time` from
`pd.Timestamp.utcnow().tz_localize(None)`. Production code has a TODO
to verify whether moomoo `update_time` is UTC or local ET — needs
real OPRA data to confirm; for now we trust pandas' UTC assumption.
If moomoo turns out to send ET timestamps, that TODO becomes a real
bug.

---

## 5. `MOOMOO_TRD_ENV` env-var matching is case-sensitive in Python

**Symptom**: User wrote `MOOMOO_TRD_ENV=simulate` (lowercase) in .env.
Code compared `TRD_ENV_STR == "SIMULATE"` (uppercase) → fell into the
REAL branch → tried `unlock_trade` → see lesson #1.

**Why non-obvious**: most config files / env vars in the wild are
case-insensitive by convention (e.g., shell flags). Python `==` isn't.

**Defense**: At module load, normalize: `TRD_ENV_STR =
os.getenv("MOOMOO_TRD_ENV", "SIMULATE").strip().upper()`. Same trick
applied wherever the value is read at runtime
(`_effective_max_cost_per_order` etc.).

---

## 6. `discord.py-self` pairs `on_disconnect` calls (`WS + HTTP`)

**Symptom**: Overnight logs showed 14-17 `Discord on_disconnect fired`
warnings per night — much higher than expected for a stable network.
Closer look: they always came in pairs <1s apart, then one reconnect.

**Why non-obvious**: The pairs are not 14 *disconnect events* — they're
7 *underlying drops* each surfacing twice in the callback (once for
WebSocket close, once for HTTP session close). The pair structure isn't
mentioned in discord.py-self docs and looks alarming.

**Defense**: `on_disconnect` in [scripts/run_listener.py](../scripts/run_listener.py)
now debounces with a 3s window — pair becomes one log line. Plus we
log `reconnect after Xms` from `on_resumed` / `on_ready` so the actual
recovery time is visible.

---

## 7. Mac WiFi power-saving silently drops idle TCP, breaks Discord gateway

**Symptom**: Even after fixing the pair-debounce, base rate was still
~5-17 disconnects per night. Discord library logged close reason as
`None` with "Connection closed unexpectedly by server (EOF)" — i.e.
no close frame, just TCP EOF. Pattern was strikingly regular
(~15-20 min intervals).

**Why non-obvious**:
- The disconnect *looks* like a Discord-side problem (the library
  receives EOF on the socket).
- Actually it's macOS putting the WiFi NIC to sleep when "idle",
  which kills the heartbeat keepalive Discord expects.
- No code-level fix on either Discord's or our side resolves this;
  it's an OS power-management decision.

**Defense**: Run the bot under `caffeinate -i python ...`. This single
shell wrapper reduced overnight reconnects from 17 → 4 (with the
remaining 4 being clean session-resume in <1s). Documented in
[docs/realtime_quote_design.md](realtime_quote_design.md) under
"runtime ops".

---

## 8. `asyncio.run(coro)` cannot be called twice in one process if `coro` uses module-level async state

**Symptom**: `send_telegram_sync` was implemented as
`asyncio.run(send_telegram(...))`. First call worked; second call
raised `RuntimeError: ... bound to a different event loop` referring
to the module-level `asyncio.Lock` and `httpx.AsyncClient` inside
`send_telegram`.

**Why non-obvious**:
- `asyncio.run` creates a fresh event loop each call and closes it.
- Module-level async primitives are bound to the first loop they
  touch. Subsequent loops can't reuse them.
- The error message points at "different event loop" which sounds
  like a misuse of asyncio, not a "your sync wrapper is wrong" hint.

**Defense**: `send_telegram_sync` now uses synchronous `httpx.Client`
directly, no async primitives at all. Sync and async paths are fully
isolated — no cross-loop state sharing possible.

---

## 9. `sqlite3.Row` does not implement `.get()`

**Symptom**: After refactoring `positions_db.open_or_add` to detect
reopen vs add-on, code that read `existing.get("status", "")` blew up
with `AttributeError: 'sqlite3.Row' object has no attribute 'get'`.

**Why non-obvious**: `sqlite3.Row` exposes `__getitem__` (so
`row["col"]` works) and behaves like a dict in many places, but it's
not actually a dict and lacks `.get()`. Static type hints often say
`Mapping` and IDEs autocomplete `.get`.

**Defense**: Inside DB access functions, use `row["col"]` (raises
KeyError if column missing — that's actually what you want for a
schema-managed table). Or convert with `dict(row)` if you really want
`.get()` semantics. Lesson: don't trust autocomplete on `sqlite3.Row`.

---

## 10. `discord.py-self` channel-state cache is empty at `on_ready`

**Symptom**: Channel-ID validation in `on_ready` used
`client.get_channel(cid)` and logged "NOT visible" for valid IDs at
startup. Same IDs worked fine 30s later when messages arrived.

**Why non-obvious**: `get_channel` is a **cache lookup**, not a fetch.
On bot startup with discord.py-self, the cache is populated lazily as
guild events arrive — not at the moment `on_ready` fires. Returns
`None` for both "invalid ID" and "not yet cached", which the caller
cannot distinguish.

**Defense**: `validate_channels()` now uses `await
client.fetch_channel(cid)` (REST call) which returns the channel
object or raises `discord.NotFound` / `discord.Forbidden` —
unambiguous result, properly distinguishes the two failure modes.

---

## 11. CLOSE matching on symbol alone can wrong-close a multi-strike position

**Symptom**: 6/30 overnight, enrich posted `$TSLA 7/1 $425 calls for $1.55` —
we opened TSLA 425c. KC (independently, on his own pre-existing position)
posted `all out TSLA 420c runner @ 15.35` ~3 hours later. Our close parser
extracted `symbols=['TSLA']`, the listener applied that to all open TSLA
positions, and we closed our 425c at $14.58. The trade was lucky (420c and
425c had near-identical ITM intrinsic on expiry day) — we netted ~+770%.

**Why non-obvious**:
- The close parser was designed with the explicit comment "almost all
  close signals don't carry strike → symbol-level match". For most KC
  trims that's still true.
- The danger only surfaces when two unrelated signals on the same symbol
  (different strike or even side) exist simultaneously. Easy to miss in
  unit tests because each test sets up a single position.
- The reward (we made money) hides the wrongness of the mechanism.
- Next time the same pattern could close a winning position when KC
  was closing a losing one, or close our call when KC closes a put.

**Defense**: `_extract_strike_hint` in close_parser now extracts an
optional `(strike, side)` when the close text contains an explicit
strike like `TSLA 420c` or `AMZN 255 calls`. The listener filters
positions by `(strike, side)` whenever the hint is set; when no hint is
present (most casual trims), behavior is unchanged. If hint is set and
no position matches, the close is **skipped** and a TG warning fires.
See `_handle_close_signal` in [src/listener/discord_client.py](../src/listener/discord_client.py).

Tests in [tests/test_listener_close.py](../tests/test_listener_close.py):
strike mismatch → skip, strike match → execute, no hint → legacy
behavior.

---

## 12. `discord.py-self` IDENTIFY rate-limit produces ~7-minute outages with exponential backoff

**Symptom**: 6/30 04:36 — single `on_disconnect`, followed by
`Attempting reconnect in 1.90s`, then 0.29s, 6.23s, 14.72s, 3.92s,
32.68s, 82.51s, 117.86s, 138.85s. Each retry triggered another
`on_disconnect` callback. After ~7 minutes total, full
`Discord logged in as ...` (fresh login, not session resume). During
the 7-min window the bot was completely offline — any KC signal arriving
then would be missed.

**Why non-obvious**:
- Looks like our bot is broken (10+ rapid disconnects in 7 minutes).
- Actually it's Discord's IDENTIFY rate-limit: too many connect
  attempts in a short window trigger increasing backoff (`Retry-After`
  on the IDENTIFY response, library obeys it).
- Underlying cause is usually a single brief network blip or Mac
  partial wake, but the visible symptom looks catastrophic.
- `caffeinate -i` reduced normal-state reconnects from 17/night to 4-5,
  but it doesn't prevent the occasional storm — Discord's rate-limit
  kicks in regardless.

**Defense**: Storm detector in [scripts/run_listener.py](../scripts/run_listener.py)
`on_disconnect`: 60s sliding window, threshold 3 disconnects → loud
`logger.error` + Telegram alert (with 5-min cooldown to prevent
spam during the storm itself). The bot stays running; the operator
gets a heads-up that we're in a degraded window and may need to
manually restart. Documented `1006 / EOF` close codes already get
captured by `_DiscordGatewayLogCapture` so the storm log line carries
context.

Open question for future: when the storm hits, should we auto-restart
the process (forfeit any in-flight state but reset the rate-limit
clock)? Currently no — restart loses the SQLite dedup state for a few
seconds and could double-execute a close signal that arrived mid-restart.
Leaving as manual decision until we see this fail in a way the alert
doesn't catch.

---

## 13. `math.ceil` on 1-contract positions turns every trim into a full close

**Symptom**: When we hold exactly 1 contract and KC posts a `trimmed X%`
signal, `calc_qty_to_sell` computes `max(1, math.ceil(1 * pct / 100))` =
1 for any pct in `[1, 100]`. So a 33% trim intended to preserve most of
the position closes the whole thing. Then KC's actual runner (the
remaining part *KC* is holding) keeps climbing and we miss it.

Concrete misses on record:
- 6/30 SPY 748c: opened @ $2.59, 33% trim signal → we sold @ $2.71.
  KC's runner went to $4.00 (+40%). Missed ~$130/contract.
- 7/1 MSFT 390c: opened @ $2.48, 33% trim signal → we sold @ $2.56.
  KC's runner went to $4.60 (+85%). Missed ~$200/contract.

**Why non-obvious**:
- The old comment on `calc_qty_to_sell` said "trim 33% but only 1 left
  → sell 1 is more reasonable than keeping". That intuition breaks
  down when the signal source (KC) is also holding partial runners:
  the "trim" *means* "sell some, keep the rest for higher"—not "close
  because it might drop".
- Numerically the calculation is correct (`ceil(0.33) = 1`), so no
  test failure signals the problem.
- Small P&L (+$8, +$3) makes it look like the system worked, hiding
  the massive opportunity cost.
- Two overnights before the pattern became obvious enough to name.

**Defense (strategy A, 2026-07-02)**: `calc_qty_to_sell` now returns
`0` when `remaining == 1 and pct < 100`, so trim signals on
single-contract positions are ignored while the position rides. The
100% path is untouched — explicit `closed all` / `out full` / KC
clearly finishing still gets executed. See
[src/position/manager.py](../src/position/manager.py) and tests in
[tests/test_positions.py](../tests/test_positions.py)
(`test_calc_qty_to_sell_single_contract_runner_preserve`).

**Strategy B, blocked on OPRA subscription**: The right answer is
quote-aware — early in a trade (say < +50% PnL) match KC's trims for
risk management, then transition to runner-hold after we've de-risked.
Requires `get_last_prices` to actually return prices, which requires
US MarketOptions Lv1 subscription. Filed as P0 in
[docs/TODO.md](TODO.md) "Runner mode B". Until then, strategy A holds.

**Trade-off strategy A carries**: If KC's early trim signal (e.g., at
+9%) is a genuine reversal warning, we now hold instead of exiting.
Accepting this asymmetry deliberately: past data shows KC's early
trims are usually profit-taking, not reversal calls; the reversal
calls are worded as `closed`, `out full`, `stop hit` (which parse as
100%). If evidence changes, drop strategy A back to the old ceil
behavior.

---

## 14. Long ITM options auto-exercise into shares at expiry (OCC/moomoo standard behavior)

**Symptom**: 7/2 → 7/3 morning, moomoo SIMULATE 持仓 showed HOOD 100 shares
($10,800 cost), IBM 200 shares ($57,000), TSLA 100 shares ($42,500), plus
some option positions we didn't recognize. Local DB still marked HOOD 108c,
IBM 285c ×2, TSLA 425c, RKLB 108c etc as `status='OPEN'`. RKLB 108c that had
just been bought a few hours earlier was gone from broker but present in local
DB.

`history_order_list_query` showed a burst of auto-generated orders at 20:40
UTC (post-market close ET) with pattern:
```
20:40:04 US.HOOD BUY 100 @ 108     dealt=0 status=N/A
20:40:03 US.HOOD260702C108000 SELL qty=1 dealt=0 status=N/A
20:40:02 US.IBM  BUY 200 @ 285     dealt=0 status=N/A
20:40:01 US.IBM260702C285000 SELL qty=2 dealt=0 status=N/A
20:40:00 US.TSLA BUY 100 @ 425     dealt=0 status=N/A
20:40:00 US.TSLA260701C425000 SELL qty=1 dealt=0 status=N/A
```

Every expired ITM long call generated a synthetic SELL (of the option) +
BUY (of underlying × strike × 100). Some assignments succeeded earlier
(HOOD/IBM/TSLA shares are actually held), others left the account with
just cash movement.

**Why non-obvious**:
- This is standard OCC/exchange behavior, not moomoo-specific. Any long
  option ITM by at least $0.01 at expiration auto-exercises.
- No documentation flag on the position that says "will auto-exercise
  Friday if ITM." moomoo just silently processes it.
- We had zero exercise/assignment code in the repo — expiry handling
  happened server-side without notifying us.
- Result was silent — the sold option position vanished from
  `position_list_query`, our local DB was never touched by anything.

**Defense**:
- `scripts/sync_positions.py`: queries broker, compares to local DB
  OPEN, marks stale entries as CLOSED with `trigger_source=broker_sync`.
  Recommend running before each `run_listener.py` session (potentially
  as a preflight step in a future PR).
- Ongoing: EOD watcher should force-close ITM positions before market
  close on expiry day. Currently no-op because OPRA subscription is
  missing. Filed under `docs/TODO.md` P0.
- The stray stock positions (HOOD/IBM/TSLA shares) still sit in the
  account — they're outside our system's execution scope (we only place
  option orders). Need to close them manually in moomoo.

---

## 15. `SELL` on an option we don't hold opens a naked short (broker accepts, we didn't intend)

**Symptom**: Direct consequence of #14. Once the local DB was out of sync
with broker, our close pipeline could easily fire `place_sell_order` for
an option the account no longer holds. moomoo would happily accept that
as **opening a new naked short position**, not "closing existing long"
— because we ran out of long inventory. Naked short calls have unlimited
loss; naked short puts are limited but still large.

Nothing in the pipeline would have caught this before 7/3:
- `place_sell_order` submitted straight to `ctx.place_order` with
  `TrdSide.SELL`. If broker accepted, we logged "success" and moved on.
- No pre-check that we actually owned the option we were selling.

**Why non-obvious**:
- On the surface, a SELL order looks like "close position." The broker
  semantics are actually "sell N contracts, however you want to source
  them" — long-close and open-short use the same trade side.
- The failure mode requires the local DB to be wrong first (#14). If
  the local DB were always correct, we'd never call sell on something
  we don't hold. But #14 shows the DB *does* go stale.

**Defense**: `_get_long_qty(option_code)` in
[src/broker/moomoo_client.py](../src/broker/moomoo_client.py) queries
`position_list_query(code=...)` and returns 0 if we don't hold `LONG`
inventory. `place_sell_order` calls it before submitting. If broker
`long qty < requested sell qty`, we refuse and return an explicit
"naked-short refused" message — no order goes out. Costs one extra RTT
per sell (probably ~50ms) but prevents unbounded downside from a state
mismatch. Applies to real-env only (`DRY_RUN` short-circuits earlier).

---

## 16. Full re-login (`on_ready`) does **not** replay missed messages; only RESUME does

**Symptom**: 7/20 overnight the local network flapped ~every 15-20 min for
10 hours. Discord dropped ~28 times, of which ~25 were **full re-logins**
(`logged back in after ~3000ms`) and only 3 were fast `RESUMED`. moomoo's
trade+quote contexts dropped in the same seconds (9 reconnects), confirming
it was the shared network layer flapping, not Discord blocking the token.

Two hidden failures fell out of this:
- **~75s of silent blindness.** A gateway RESUME replays events buffered
  during the gap; a full re-IDENTIFY does **not** — the session is gone and
  any message KC sent during those ~3s windows is never delivered to
  `on_message`. 25 × 3s ≈ 75s where a signal would vanish with zero trace.
  That night nothing fired in a gap, but it's a real loss window.
- **Zero alerting.** The existing storm detector only fires on "3
  disconnects in 60s" (the 6/30 identify-rate-limit burst pattern). Slow
  chronic churn every 15-20 min never trips a 60s window, so the bot
  limped all night with no TG warning.

**Why non-obvious**:
- `on_ready` and `on_resumed` both look like "we're back online." The
  critical difference — resume replays the gap, re-identify silently drops
  it — is a gateway-protocol detail, not visible in the callback names.
- The storm detector *existed* and looked like "reconnect churn is
  covered." It only covered the *fast* burst shape; the *slow* churn shape
  is a different failure the same code doesn't catch.
- Simultaneous Discord+moomoo drops are the tell for "network, not
  service" — easy to misread as a Discord/token problem and go chasing the
  wrong layer.

**Defense** (all in [scripts/run_listener.py](../scripts/run_listener.py)):
- `_backfill_missed()` runs in the `on_ready` **re-login** branch (not
  `on_resumed`): fetches each monitored channel's `history(after=...)`
  since the disconnect wall-clock time and re-feeds through
  `handle_message`. Idempotent because `handle_message` already dedups on
  `_seen(message.id)` — replayed messages that were processed live are
  dropped, so no double orders. `_last_disconnect_wall` is set on the first
  disconnect of a gap and cleared on resume/backfill so we cover the whole
  gap exactly once.
- Chronic-churn detector: 30-min rolling window, ≥5 disconnects → one TG
  alert per 30 min, alongside the existing 60s storm detector.
- `shutdown()` is now idempotent (a second ^C is ignored) so two concurrent
  shutdown coroutines don't race on `close_ctx`/`client.close`.
- Root cause (WiFi power-save / router dropping idle connections) is
  environmental — code only *mitigates* (backfill + alert), it cannot stop
  the drops. Wired/ethernet or disabling WiFi power management is the real
  fix.

---

## 17. A rejected auto-sell must reconcile local state, not retry forever — local DB and the (SIMULATE) broker silently desync

**What happened (7/21):** the TP watcher fired `T1 HIT` on positions the
local `positions_db` recorded as OPEN (some with a `FILL_ADJUST` event
proving the buy filled), but `place_sell_order`'s naked-short guard
(lesson #15) rejected every sell with *"broker has only 0 long"*. The
watcher logged the rejection, discarded its per-tick trigger, and **tried
again the next tick** — every ~6 s, for one contract from 01:15 to 06:00
(~2,700 attempts), plus SPY 750c/755c/760c. The 8-hour log is ~99% this
one storm, and each rejection also fired an un-deduped Telegram error →
TG flood / 429s.

**Two non-obvious things:**

1. **moomoo SIMULATE `position_list_query` does not reliably reflect
   filled option positions.** The buy order returns an `order_id` and a
   fill (dealt_avg), our DB records OPEN, yet querying the paper account's
   position list for that option returns empty (0 long). So the local DB
   and the broker desync with *no error anywhere* — the only symptom is
   the sell guard refusing. This isn't in any moomoo doc; don't assume
   "buy filled" ⟹ "position query shows it" in SIMULATE.
2. **A correct guard + a naïve retry loop = a self-inflicted DoS.** The
   naked-short refusal is right (never open a naked short), and retrying
   is the right default for *transient* failures — but a naked-short
   rejection is a **desync signal, not a transient error**. No amount of
   retrying fixes it; it just spams the broker and TG all night.

**Defenses added:**
- `place_sell_order` tags the naked-short branch distinctly
  (`naked_short=True, broker_qty=N`) — separate from the
  `position_list_query` *exception* path (that one stays transient and
  retryable; we must never reconcile-to-CLOSED on a query failure, only on
  an authoritative "you hold N").
- New `positions_db.reconcile_to_broker(code, broker_qty)`: shrinks local
  `qty_remaining` to the broker's actual (0 → CLOSED). Once CLOSED the
  position drops out of `get_open_positions()`, so **all three** watchers
  (TP/SL/EOD) stop scanning it — the storm ends structurally, not via a
  per-watcher flag.
- It returns `True` only on the first state change, which the watchers use
  to alert **exactly once** (a CLOSED position is never re-selected; a
  partial shrink succeeds on the next tick). The `sell_lock` serializes
  the three watchers so only one reconciles + alerts.
- Root cause is the DB↔broker desync itself (environmental to SIMULATE);
  the code now *contains* it (reconcile + one alert) instead of storming.
  The local DB still needs a manual reconcile / reset against the paper
  account when it drifts.

---

## Format guidelines for adding new lessons

Keep entries focused on **gotchas that weren't documented or
discoverable from the API surface alone**. Things like "we made a
typo" or "we forgot to handle X" don't belong here — those are just
bugs. The bar is: would a competent developer reading the official
docs have known to defend against this? If yes, skip the entry.
