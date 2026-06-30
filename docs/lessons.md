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

## Format guidelines for adding new lessons

Keep entries focused on **gotchas that weren't documented or
discoverable from the API surface alone**. Things like "we made a
typo" or "we forgot to handle X" don't belong here — those are just
bugs. The bar is: would a competent developer reading the official
docs have known to defend against this? If yes, skip the entry.
