# Bot Resilience: Crash Recovery, Duplicate Trade Prevention & False-Positive Lockout

**Date:** 2026-05-03  
**Status:** Approved — ready for implementation

---

## Problem Statement

Three gaps in the current crash-recovery system caused real incidents on live/paper trading:

1. **Duplicate BUY orders after restart** — `_reconcile_positions()` checks Alpaca filled positions only, not pending orders. A market order submitted just before a crash leaves an orphan pending order in Alpaca. The next startup doesn't see it; the next trading cycle places a second bracket order for the same symbol.

2. **48-hour flash crash lockout from a single bad SPY tick** — `run_market_monitor()` triggers a 48h CRISIS lockout on one reading ≤ −2.5%. No confirmation, no auto-recovery. Manual intervention (Python one-liner) is the only escape today.

3. **Crash loops go undetected** — the watchdog only catches scheduler freeze (>35 min no heartbeat). A rapid launchd restart loop (crash → restart in seconds) is invisible and can repeatedly submit orphan orders.

---

## Design: Four Layers

All layers are subscription-free. Polygon/Massive is an optional enhancement in Layer 2.

```
Startup
  Layer 1: Crash loop gate          bot_health.json — backoff if ≥3 crashes/10 min
  Layer 2: Orphan-order drain       cancel open BUY orders not in positions (free Trading API)
  Layer 3: Position reconcile       existing — no change

Every 5 min (market hours)
  Layer 2 (flash crash):            two-reading confirmation + optional Massive/Polygon cross-check

8:55 AM daily
  Layer 2 (auto-clear):             clear lockout if VIX/SPY conditions recovered

Clean shutdown
  Layer 1:                          reset crash counter
```

---

## Layer 1 — Crash Loop Detection

### State file: `bot_health.json`

Auto-created on first startup. Never committed to git.

```json
{
  "crash_count": 0,
  "window_start": "2026-05-03T10:00:00-04:00",
  "backoff_until": null
}
```

### Startup logic (runs before `_reconcile_positions`)

```
1. Read bot_health.json (default: crash_count=0, window_start=now if missing)
2. If window_start older than CRASH_LOOP_WINDOW_MINUTES → reset crash_count=0, window_start=now
3. Increment crash_count; set window_start=now if crash_count was 0 before increment
4. Write updated bot_health.json
5. If crash_count >= CRASH_LOOP_THRESHOLD:
     - Compute backoff_until = now + CRASH_BACKOFF_MINUTES
     - Write backoff_until to bot_health.json
     - Log CRITICAL: "Crash loop detected (N crashes in M min) — pausing P min"
     - Sleep until backoff_until  ← scheduler not started; no orders can fire during sleep
     - Reset crash_count=0, backoff_until=null, write bot_health.json
6. Continue normal startup
```

### Clean shutdown reset

`_handle_shutdown()` catches SIGTERM/SIGINT. The reset must be written **between** `scheduler.shutdown(wait=False)` and `sys.exit(0)` — not as a separate file-write inside the signal handler body, which is unsafe for arbitrary I/O. Sequence:

```python
def _handle_shutdown(signum, frame):
    logger.info("Shutdown signal received — stopping.")
    _clear_pid()
    scheduler.shutdown(wait=False)
    _reset_bot_health()   # ← write crash_count=0 here, after scheduler is stopped
    sys.exit(0)
```

A clean stop via `stop.sh` or SIGTERM resets the counter so the next morning restart is not penalised.

### Config additions (`config.py` under `# ── Crash Recovery ──`)

```python
BOT_HEALTH_FILE           = BASE_DIR / "bot_health.json"
CRASH_LOOP_THRESHOLD      = 3    # crashes within window → trigger backoff
CRASH_LOOP_WINDOW_MINUTES = 10   # rolling window in minutes
CRASH_BACKOFF_MINUTES     = 30   # pause duration before resuming
```

---

## Layer 2 — Startup Orphan-Order Drain

### New methods: `alpaca_client.py`

`GetOrdersRequest` accepts `side` as a direct field — filter server-side to avoid fetching SELL-side bracket legs:

```python
def get_open_buy_orders(self) -> list[dict]:
    """Return all pending BUY orders (free Trading API, all Alpaca tiers)."""
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus, OrderSide
    orders = self._trading.get_orders(
        filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, side=OrderSide.BUY)
    )
    return [{"id": str(o.id), "symbol": o.symbol} for o in orders]

def cancel_order(self, order_id: str) -> None:
    """Cancel a single order by ID."""
    self._trading.cancel_order_by_id(order_id)
```

### New step 4 in `_reconcile_positions()` (`main.py`)

Appended after the existing three steps, using the reconciled `local_positions` dict:

```
4. Orphan BUY order drain
   a. Call alpaca.get_open_buy_orders()  ← server-side BUY filter; SELL legs not returned
   b. For each pending BUY order:
      - Cancel unconditionally (symbol in or out of local_positions)
      - Rationale: a filled bracket BUY leg has status=filled, not open; any open BUY is
        either an unfilled orphan or a ghost — safe to cancel in both cases
      - Log each cancellation at WARNING with symbol and order_id
   c. Log total count at INFO level
```

---

## Layer 2 — Flash Crash Two-Reading Confirmation

### State: `macro_regime.json` gains one new field

```json
"flash_crash_candidate": {
  "spy_pct": -2.7,
  "detected_at": "2026-05-03T11:05:00-04:00"
}
```

Field is `null` when no candidate is pending. Must be included in:
- `fetch_macro_regime()` result dict (written as `null` on every 8:55 AM run — intentionally drops stale candidates overnight)
- `load_macro_regime()` default return dict (so `.get("flash_crash_candidate")` is always defined)

### `check_flash_crash()` uses config constant, not literal

Current hardcoded `-2.5` in `macro_regime.py:355` must be replaced:

```python
# Before
return (spy_current - spy_prev_close) / spy_prev_close * 100 <= -2.5

# After
return (spy_current - spy_prev_close) / spy_prev_close * 100 <= config.FLASH_CRASH_SPY_DROP_PCT
```

### Updated `run_market_monitor()` logic (`trading_agent.py`)

Three-way Polygon result (`True` / `False` / `None`) must be handled explicitly:

```
spy_pct = (spy_current - spy_prev) / spy_prev * 100

If spy_pct <= FLASH_CRASH_SPY_DROP_PCT:
  If POLYGON_API_KEY set:
    result = _verify_spy_with_polygon(spy_pct)
    if result is True  → confirmed; force-close + set lockout; clear candidate; return
    if result is False → bad tick; clear candidate; return (no lockout, no Stage 2)
    if result is None  → Polygon unavailable; fall through to Stage 2 path below

  # Stage 2 path (no key, or Polygon returned None)
  candidate = load existing flash_crash_candidate from macro_regime.json
  if candidate is None:
    write flash_crash_candidate = {spy_pct, detected_at=now}; log WARNING; return
  elif now - detected_at <= 15 min:
    confirmed; force-close + set lockout; clear candidate; return
  else:
    stale candidate; overwrite with fresh {spy_pct, detected_at=now}; log WARNING; return

If spy_pct > FLASH_CRASH_SPY_DROP_PCT:
  if candidate exists → clear it (SPY recovered between readings)
```

The 15-minute expiry prevents a candidate written near market close from triggering a lockout the next morning.

### `_set_crisis_lockout()` clears candidate

`_set_crisis_lockout()` in `trading_agent.py` patches `macro_regime.json` directly. It must also clear the candidate field when setting the lockout:

```python
data["flash_crash_active"]    = True
data["flash_crash_until"]     = until
data["regime"]                = "CRISIS"
data["flash_crash_candidate"] = None   # ← add this line
```

### New helper: `_verify_spy_with_polygon(spy_pct: float) -> bool | None`

Location: `trading_agent.py` (private helper).

```
GET https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers/SPY
Header: Authorization: Bearer {config.POLYGON_API_KEY}
Timeout: 5 seconds

On success:
  polygon_pct = response["ticker"]["todaysChangePerc"]
  return polygon_pct <= config.FLASH_CRASH_SPY_DROP_PCT   # True = confirmed, False = bad tick

On any exception (network, auth, parse, timeout):
  log WARNING "Polygon/Massive check failed — falling back to two-reading confirmation"
  return None   # caller uses Stage 2 path
```

### Auto-clear in `fetch_macro_regime()` (`macro_regime.py`)

Inside the existing lockout carry-forward block (where `flash_crash_active` is read from `prev`), add the recovery check:

```
If flash_crash_active = True AND flash_crash_until is set:
  If now >= until_dt:
    flash_crash_active = False; flash_crash_until = None  ← existing expiry logic
  Elif vix_current < FLASH_CRASH_VIX_RECOVERY
       AND spy_5day_return > FLASH_CRASH_SPY_RECOVERY_PCT:
    flash_crash_active = False; flash_crash_until = None  ← new auto-clear
    log INFO "Flash crash lockout auto-cleared — VIX={vix_current:.1f}, SPY5d={spy_5day_return:+.2f}%"
  Else:
    regime = "CRISIS"  ← carry forward (lockout still valid)
```

### Config additions

```python
FLASH_CRASH_SPY_DROP_PCT     = -2.5   # replaces hardcoded -2.5 in check_flash_crash()
FLASH_CRASH_VIX_RECOVERY     = 35     # auto-clear if VIX below this
FLASH_CRASH_SPY_RECOVERY_PCT = -5.0   # auto-clear if 5d return above this
```

---

## Polygon / Massive API Integration

| Property | Value |
|---|---|
| Provider | Massive.com (ex-Polygon.io, API-compatible) |
| Base URL | `https://api.massive.com` |
| Endpoint | `GET /v2/snapshot/locale/us/markets/stocks/tickers/SPY` |
| Auth | `Authorization: Bearer {POLYGON_API_KEY}` |
| Response field | `ticker.todaysChangePerc` (today's % change) |
| Fallback | Key absent → skip Polygon entirely; call fails → `None` → Stage 2 path |
| Tier required | Free (called only on flash-crash candidate events, not every 5 min) |

---

## Auto-Launcher Changes

### macOS — `trading_bot_start.plist`

Add `ThrottleInterval` and change `KeepAlive` to restart only on crash:

```xml
<!-- Before -->
<key>KeepAlive</key>
<false/>

<!-- After -->
<key>KeepAlive</key>
<dict>
    <key>SuccessfulExit</key>
    <false/>
</dict>
<key>ThrottleInterval</key>
<integer>15</integer>
```

`ThrottleInterval=15` ensures a minimum 15-second gap between launchd-driven restarts. Without it, a bot that crashes before writing `bot_health.json` can spin at full speed before the crash loop gate gets a chance to operate.

`SuccessfulExit=false` means:
- Crash (non-zero exit) → launchd restarts ✅
- `stop.sh` SIGTERM → `_handle_shutdown()` exits 0 → launchd does NOT restart ✅
- Watchdog `os._exit(1)` → non-zero → launchd restarts ✅

### Linux — no cron change

Cron fires on a schedule only and cannot respond to mid-day crashes. For Linux cloud deployment, use a systemd unit with `Restart=on-failure` and `RestartSec=15`. Document this in `README.md` as an alternative to the cron setup. `setup_cron.sh` itself is unchanged.

---

## Files Changed

| File | Change |
|---|---|
| `config.py` | 7 new constants under `# ── Crash Recovery ──` |
| `alpaca_client.py` | Add `get_open_buy_orders()`, `cancel_order()` |
| `main.py` | Crash loop gate + `_reset_bot_health()` in `main()` and `_handle_shutdown()`; orphan drain in `_reconcile_positions()` |
| `trading_agent.py` | Two-reading confirmation + explicit 3-way Polygon branch + `_verify_spy_with_polygon()` in `run_market_monitor()`; clear candidate in `_set_crisis_lockout()` |
| `macro_regime.py` | Replace `-2.5` literal with `config.FLASH_CRASH_SPY_DROP_PCT` in `check_flash_crash()`; auto-clear lockout in `fetch_macro_regime()`; add `flash_crash_candidate: null` to result dict and `load_macro_regime()` defaults |
| `trading_bot_start.plist` | `KeepAlive={SuccessfulExit: false}` + `ThrottleInterval=15` |
| `README.md` | Update Crash Recovery section; remove manual Python one-liner; add `bot_health.json` to state table; add systemd note for Linux |
| `.env.example` | Add `POLYGON_API_KEY=` with massive.com comment |
| `tests/test_foundation.py` | Unit tests for all new logic (no network calls) |
| `bot_health.json` | New state file — auto-created, added to `.gitignore` |

No changes to `vix_regime.py`, `swing_manager.py`, `learning_agent.py`, `watch_list_agent.py`, or any scheduler job timing.

---

## What Is Not Changed

- `vix_regime.py` remains as-is (still imported by `learning_agent.py` and `trading_agent.py` for `load_trading_params`)
- Ticker universe (102 tickers → volatility screener caps at top 50 → no trading performance impact)
- Swing position handling — reconcile already skips swing symbols
- All scheduler job timings unchanged
- `setup_launchagent.sh` and `setup_cron.sh` scripts unchanged (plist files are what change)

---

## Testing Requirements

All tests in `tests/test_foundation.py`, decorated `@pytest.mark.unit`, no network calls:

1. **Crash loop gate** — missing file → count=1, no backoff; count hits threshold → backoff_until set; window expired → counter resets to 1 (not 0) after increment
2. **Clean shutdown reset** — `_reset_bot_health()` writes crash_count=0, backoff_until=null
3. **Orphan-order drain** — open BUY order for any symbol → cancelled; confirms SELL orders never appear (server-side filter)
4. **Two-reading flash crash, no Polygon** — single reading → candidate written, no lockout; second reading within 15 min → lockout set, candidate cleared; SPY recovers → candidate cleared; stale candidate (>15 min) → overwritten, no lockout
5. **Polygon helper** — pct ≤ −2.5 → True; pct > −2.5 → False; exception → None
6. **Caller three-way branch** — True → lockout set immediately; False → candidate cleared, no lockout; None → Stage 2 path used
7. **`_set_crisis_lockout()` clears candidate** — after call, `flash_crash_candidate` is null in written JSON
8. **Auto-clear lockout** — VIX < 35 and spy_5d > −5 → cleared; VIX ≥ 35 → preserved; spy_5d ≤ −5 → preserved
9. **`check_flash_crash()` uses config** — patching `config.FLASH_CRASH_SPY_DROP_PCT` changes the threshold

---

## Post-Implementation Documentation

After all code is implemented and tests pass:

1. Update `README.md`:
   - Crash Recovery section: reflect all protections including crash loop backoff and auto-clear lockout
   - Remove the manual Python one-liner for lockout clearing
   - Add `bot_health.json` row to the State / Persistence table
   - Add systemd note for Linux cloud deployment
   - Update `POLYGON_API_KEY` reference to point to massive.com

2. Update `.env.example`:
   ```bash
   # Optional: cross-checks SPY flash-crash readings via Massive API (ex-Polygon.io).
   # Get free key at https://massive.com/dashboard/keys
   POLYGON_API_KEY=
   ```

3. Add `bot_health.json` to `.gitignore`.
