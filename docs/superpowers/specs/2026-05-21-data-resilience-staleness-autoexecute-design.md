# Data Resilience, Staleness Guard & Auto-Execute Fix

**Date:** 2026-05-21  
**Status:** Approved for implementation  
**Trigger:** Yahoo Finance v10/quoteSummary returned 401 → 103 tickers → 0 → empty watch list → no Telegram notification today; auto-execute `intra_sent` reads wrong field.

---

## 1. Problem Statement

Three independent failures observed today:

| # | Failure | Root Cause | Impact |
|---|---|---|---|
| 1 | Watch list Telegram notification not sent | Yahoo Finance market cap API 401 → all tickers filtered out → empty watch list → agent returns early before `notify_watch_list()` | Silent — no visibility |
| 2 | `_fetch_market_cap` catches 401 as `Exception` → returns `None` silently | No WARNING log, no alert, pipeline degrades invisibly | All 103 tickers excluded |
| 3 | Auto-execute `intra_sent` reads `__meta__.shock_score` (inverted macro metric) instead of symbol intraday sentiment | Wrong confidence score computed for every auto-execute trade | Confidence systematically miscalculated |

Secondary gaps found during audit:
- No staleness flag propagated to `monitor.py`, `swing_manager.py`, T2 gates — all consume stale data silently
- Held position tickers not pinned into symbol set when watch list is empty → Claude cannot CLOSE existing positions
- `yahoo_get` retries only on 429; 5xx server errors cause immediate raise with no retry

---

## 2. Design Principles

1. **Single SoT config** — all retry schedules, staleness thresholds, penalties, disable sets in `config.py` under `# ── Data Resilience ──`. Adding or tuning any value = one-line config change, zero code changes.
2. **Single SoT code** — `utils/data_health.py` is the sole authority for health tracking and staleness decisions. No consumer reimplements staleness logic.
3. **Single SoT data** — staleness metadata (`_stale`, `_stale_since`, `_fallback_from`) is written into existing JSON state files by `DataHealthTracker`. All module loaders get the flag transparently.
4. **Workflow-ordered guard injection** — `StalenessGuard` is built exactly once per workflow entry point (cycle, monitor tick, EOD), after all upstream data is resolved, and flows downstream through the entire stage. Never built mid-stage.
5. **No consumer logic** — consumers read `guard.field`; they never evaluate staleness themselves.

---

## 3. Trading Workflow & Guard Injection Points

The guard is built at the START of each independent scheduler entry point, after all data for that stage is loaded. It is never shared across scheduler boundaries.

```
PRE-MARKET PIPELINE (dependency-ordered, 8:00–9:15 AM ET)
────────────────────────────────────────────────────────────
8:00 AM  earnings_calendar.refresh()
           → tracker.record_success/failure("earnings")
           → on failure: mark_stale(earnings_calendar.json)
                         schedule_retry("earnings", backoff[0])

8:20 AM  economic_calendar.refresh()
           → tracker.record_success/failure("economic_calendar")
           → on failure: schedule_retry (low-risk, no penalty)

8:45 AM  volatility_screener.run()
           → fetch market caps concurrently
           → if ≥80% None: tracker.record_failure("screener")
                            write empty universe fallback
                            schedule_retry("screener", backoff[0])
           → on success: tracker.record_success("screener")
                         clear_stale(daily_tradeable.txt companion)

8:55 AM  macro_regime.fetch()
           → tracker.record_success/failure("regime")
           → on failure: mark_stale(macro_regime.json, fallback_from=prev_regime)
                         schedule_retry("regime", backoff[0])

9:00 AM  watch_list_agent.run()
           → reads daily_tradeable.txt (may be stale/empty)
           → if empty: load previous watch_list.txt as fallback
                       write watch_list_health.json {stale: true, date: prev_date}
                       tracker.record_failure("watch_list")
                       send notify_data_degraded("watch_list", guard)
           → always: merge open_positions + swing_positions tickers
           → on success: tracker.record_success("watch_list")
                         clear watch_list_health.json stale flag

9:15 AM  research_agent.run()
           → tracker.record_success/failure("research")
           → on failure: mark_stale(research_report.json)
                         schedule_retry("research", backoff[0])


PER-CYCLE PIPELINE (every 10 min, 9:30–3:50 ET)
────────────────────────────────────────────────
Step 0   regime_info = load_macro_regime()        ← reads _stale flag
         guard = get_staleness_guard(regime_info) ← GUARD BUILT HERE (SoT)
         snap  = build_cycle_snapshot(..., staleness_guard=guard)
           includes: regime, research, intraday_news, params, breadth_ratio

Step 1   symbol_set = load_watch_list()           ← always includes held tickers
         [parallel bar fetch + indicator compute per symbol]

Step 2   PRE-CLAUDE FILTER (ordered gates, all use snap/guard)
           Layer 0: already_closed_today gate
           Layer 1: composite_min gate
                    → effective_composite_gate adds guard.composite_penalty
           Layer 2: RSI max gate (regime-adjusted via snap)
           Layer 2.5: VWAP slope gate
           Layer 3: macro health + shock gate (reads snap.regime_info)
           Layer 4: research sentiment gate     (reads snap.research)
           Layer 5: intraday news + sentiment   (reads snap.intraday_news)

Step 3   AUTO-EXECUTE (guard gate first)
           → if not guard.auto_execute_allowed: skip entire auto-execute
           → else: compute confidence with CORRECT intra_sent (fix)
                   call execute_buy → check_all_gates → place order → notify_buy

Step 4   CLAUDE EVALUATION
           → guard.claude_warnings injected into _build_market_context_block()
           → Claude sees: "⚠️ STALE: regime", "⚠️ STALE: research" etc.
           → decisions: BUY / HOLD / CLOSE

Step 5   SWING MODE DETERMINATION (inside execute_buy)
           → swing_eligible = (regime in SWING_ELIGIBLE)
           → if "regime" in guard.stale_sources: swing_eligible = False
                 (wrong regime → wrong swing params → safe to block)

Step 6   EXECUTE TRADE
           → execute_buy → check_all_gates (receives guard) → place order

Step 7   NOTIFY
           → notify_buy / notify_close as today


POSITION MONITOR (every 60 sec, 9:30–3:55 ET)
────────────────────────────────────────────────
Step 0   regime_info = load_macro_regime()        ← reads _stale flag
         guard = get_staleness_guard(regime_info) ← GUARD BUILT HERE (independent)

Step 1   SL/TP enforcement (no guard dependency)

Step 2   T2 eligibility check
           → if not guard.t2_allowed: skip _check_tranche2_triggers entirely
           → else: check_tranche2_gates() → execute_tranche2_addon() → notify_tranche2_add


EOD PIPELINE (3:50 PM, 3:55 PM)
────────────────────────────────────────────────
Step 0   regime_info = load_macro_regime()         ← reads _stale flag
         guard = get_staleness_guard(regime_info)  ← GUARD BUILT HERE (independent)

Step 1   swing_manager.evaluate_eod_holds()
           → effective_regime = guard.swing_fallback_regime or regime
           → get_swing_params(effective_regime)
           → promote/reject each position under correct params

Step 2   force_close_all() at 3:55 PM
           → no guard dependency — always closes all day-trade positions
```

---

## 4. Config SoT (`config.py`)

All data-resilience values under a single section header. **Zero magic numbers in any other file.**

```python
# ── Data Resilience ──────────────────────────────────────────────────────────
DATA_RETRY_MAX                  = 3
DATA_RETRY_BACKOFF_PREMARKET    = [1, 3, 8]      # minutes; before 9:30 ET
DATA_RETRY_BACKOFF_INTRADAY     = [1, 2, 5]      # minutes; during market hours

DATA_STALE_THRESHOLD_MINUTES: dict[str, int] = {
    "regime":            60,
    "research":         360,
    "screener":          90,
    "earnings":         480,
    "economic_calendar": 480,
    "watch_list":        90,
}
DATA_COMPOSITE_PENALTY: dict[str, int] = {
    "regime":    10,
    "research":   5,
    "screener":   0,
    "earnings":   0,
    "watch_list": 0,
}
DATA_DISABLE_AUTO_EXECUTE       = frozenset({"regime"})
DATA_DISABLE_T2                 = frozenset({"regime"})
DATA_REGIME_CANOPEN_MAX_MINUTES = 240    # force can_open=False if regime age > 4h
DATA_SWING_STALE_FALLBACK_REGIME = "NORMAL"   # safest fallback when regime unknown
DATA_SCREENER_FAILURE_RATE_THRESHOLD = 0.80   # fraction of None caps → treat as API failure
DATA_WATCH_LIST_HEALTH_FILE     = STATE_DIR / "watch_list_health.json"
```

---

## 5. `utils/data_health.py` (new file)

Single module. Three exports: `DataHealthTracker` (module-level singleton), `StalenessGuard` (frozen dataclass), `get_staleness_guard()` (factory, the SoT for all staleness decisions).

### 5.1 `SourceHealth` and `StalenessGuard`

```python
@dataclass(frozen=True)
class SourceHealth:
    status: Literal["fresh", "stale", "failed"]
    age_minutes: float
    attempts_today: int
    next_retry_at: datetime | None

@dataclass(frozen=True)
class StalenessGuard:
    composite_penalty: int           # sum of applicable penalties from config
    auto_execute_allowed: bool       # False if any DATA_DISABLE_AUTO_EXECUTE source stale
    t2_allowed: bool                 # False if any DATA_DISABLE_T2 source stale
    swing_fallback_regime: str|None  # None = use real regime; str = regime override
    can_open: bool                   # False if regime age > DATA_REGIME_CANOPEN_MAX_MINUTES
    claude_warnings: tuple[str,...]  # injected into Claude context block verbatim
    stale_sources: frozenset[str]    # which source IDs are stale

FRESH_GUARD = StalenessGuard(0, True, True, None, True, (), frozenset())
```

### 5.2 `DataHealthTracker`

Module-level singleton. State is in-memory; resets on bot restart (safe — bot re-fetches all data at startup via catchup).

```python
class DataHealthTracker:
    def record_success(source_id: str) -> None
        # mark fresh, reset attempt counter, clear next_retry_at

    def record_failure(source_id: str) -> SourceHealth
        # increment attempts_today (capped at DATA_RETRY_MAX)
        # compute next_retry_at using backoff schedule (pre-market vs intraday)
        # return updated SourceHealth

    def can_retry(source_id: str, now: datetime) -> bool
        # True if: attempts_today < DATA_RETRY_MAX and now >= next_retry_at

    def age_minutes(source_id: str) -> float
        # minutes since last successful record_success(); ∞ if never succeeded

    def stale_sources(self) -> frozenset[str]
        # source IDs where age_minutes > DATA_STALE_THRESHOLD_MINUTES[id]
        # OR status == "failed"

    def to_snapshot(self) -> dict[str, SourceHealth]
        # all tracked sources

_tracker = DataHealthTracker()   # module-level singleton
```

### 5.3 `get_staleness_guard()` — the single SoT factory

```python
def get_staleness_guard(regime_info: dict | None = None) -> StalenessGuard:
    """
    Sole factory for StalenessGuard. All consumers call this; none derive staleness themselves.
    Reads: in-memory tracker state + regime_info._stale flag (JSON-embedded).
    """
    stale = set(_tracker.stale_sources())
    if regime_info and regime_info.get("_stale"):
        stale.add("regime")

    penalty   = sum(config.DATA_COMPOSITE_PENALTY.get(s, 0) for s in stale)
    ae_ok     = not (stale & config.DATA_DISABLE_AUTO_EXECUTE)
    t2_ok     = not (stale & config.DATA_DISABLE_T2)
    open_ok   = _tracker.age_minutes("regime") < config.DATA_REGIME_CANOPEN_MAX_MINUTES
    swing_fb  = config.DATA_SWING_STALE_FALLBACK_REGIME if "regime" in stale else None
    warnings  = tuple(f"⚠️ STALE DATA — {s}: treat signals with lower conviction"
                      for s in sorted(stale))

    return StalenessGuard(
        composite_penalty=penalty, auto_execute_allowed=ae_ok,
        t2_allowed=t2_ok, swing_fallback_regime=swing_fb,
        can_open=open_ok, claude_warnings=warnings,
        stale_sources=frozenset(stale),
    )
```

### 5.4 Retry scheduling

`DataHealthTracker` does not schedule jobs directly — it stays pure of APScheduler. Instead, `scheduler_jobs.py` exposes a `set_data_tracker(tracker)` alongside the existing `set_scheduler(sched)`. Job functions call `tracker.record_failure()` and then register their own retry using the existing `_scheduler` reference:

```python
# scheduler_jobs.py  (pattern already used for position_monitor pause/resume)
_data_tracker = None

def set_data_tracker(tracker) -> None:
    global _data_tracker
    _data_tracker = tracker

def _schedule_retry(job_fn, source_id: str, now: datetime) -> None:
    """Register a one-shot DateTrigger retry if tracker says retry is allowed."""
    if _data_tracker is None or _scheduler is None:
        return
    if not _data_tracker.can_retry(source_id, now):
        return
    health = _data_tracker.record_failure(source_id)  # advances next_retry_at
    if health.next_retry_at:
        _scheduler.add_job(job_fn, DateTrigger(run_date=health.next_retry_at),
                           id=f"retry_{source_id}_{health.attempts_today}",
                           replace_existing=True, max_instances=1)
        logger.info("DATA RETRY scheduled: %s attempt %d at %s",
                    source_id, health.attempts_today, health.next_retry_at.strftime("%H:%M ET"))
```

Called from each job function on failure before returning.

---

## 6. `utils/io_utils.py` additions

Two pure helpers. No change to existing `load_json` / `save_json` signatures.

```python
def mark_stale(path: Path, source_id: str, fallback_from: str | None = None) -> None:
    """Inject _stale metadata into an existing JSON state file atomically."""
    data = load_json(path) or {}
    data["_stale"]        = True
    data["_stale_since"]  = datetime.now(ET).isoformat()
    data["_fallback_from"] = fallback_from
    save_json(path, data)

def clear_stale(path: Path) -> None:
    """Remove _stale metadata after a successful fresh fetch."""
    data = load_json(path) or {}
    for key in ("_stale", "_stale_since", "_fallback_from"):
        data.pop(key, None)
    save_json(path, data)
```

---

## 7. `utils/http.py` changes

Extend `yahoo_get` to retry on 5xx (server-side transient) and fast-fail on 401/403 (auth — retrying won't help, fail immediately with ERROR log so `DataHealthTracker` can record the failure accurately).

```python
AUTH_ERROR_CODES    = {401, 403}
RETRYABLE_CODES     = {429, 500, 502, 503, 504}

def yahoo_get(url: str, timeout: int = 10) -> dict:
    for attempt in range(3):
        try:
            resp = _session.get(url, timeout=timeout)
            if resp.status_code in AUTH_ERROR_CODES:
                logger.error("Yahoo Finance auth error %d — %s (will not retry)",
                             resp.status_code, url)
                resp.raise_for_status()          # raises immediately
            if resp.status_code in RETRYABLE_CODES:
                wait = 2 ** attempt
                logger.warning("Yahoo Finance %d — retry %d/%d in %ds",
                               resp.status_code, attempt + 1, 3, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.Timeout:
            if attempt == 2:
                raise
            time.sleep(1)
    return {}
```

---

## 8. Pre-market agent changes

### 8.1 `agents/volatility_screener.py`

Detect all-None batch (API failure) vs genuine small-cap exclusion (valid). When failure rate exceeds `DATA_SCREENER_FAILURE_RATE_THRESHOLD`:
- Log `ERROR` with failure count and rate
- Skip market cap filter entirely for this run (use full universe)
- Call `_tracker.record_failure("screener")` → triggers retry scheduling
- Still write `daily_tradeable.txt` (with full universe or previous fallback)

On any successful screen: `_tracker.record_success("screener")`.

```python
none_count   = sum(1 for v in cap_results.values() if v is None)
failure_rate = none_count / max(len(tickers), 1)
if failure_rate > config.DATA_SCREENER_FAILURE_RATE_THRESHOLD:
    logger.error(
        "Market cap API failure: %d/%d fetches returned None (%.0f%%) — "
        "skipping filter, using full universe",
        none_count, len(tickers), failure_rate * 100
    )
    _schedule_retry(job_volatility_screener, "screener", datetime.now(ET))
    eligible = tickers              # bypass filter, use all
else:
    _tracker.record_success("screener")
    eligible = [t for t in tickers
                if cap_results.get(t) is not None
                and cap_results[t] >= min_cap]
```

### 8.2 `trading/watch_loader.py`

Three responsibilities, strictly ordered:

1. **Load primary watch list** from `watch_list.txt` (today's or stale fallback)
2. **Merge held tickers** from `open_positions.json` + `swing_positions.json` (always)
3. **Read staleness flag** from `watch_list_health.json` and expose to cycle

```python
def load_watch_list() -> list[str]:
    """
    Returns symbol set for the trading cycle.
    Always includes held position tickers regardless of watch list state.
    Emits staleness flag via watch_list_health.json when using fallback.
    """
    # Step 1: primary or fallback watch list
    if config.WATCH_LIST_FILE.exists():
        symbols = [t.strip().upper()
                   for t in config.WATCH_LIST_FILE.read_text().splitlines()
                   if t.strip()]
    else:
        symbols = []

    # Step 2: always pin held tickers
    held = _load_held_tickers()           # reads open + swing positions
    merged = list(dict.fromkeys(symbols + [t for t in held if t not in symbols]))

    return merged


def _load_held_tickers() -> list[str]:
    open_pos  = load_json(config.OPEN_POSITIONS_FILE,  {})
    swing_pos = load_json(config.SWING_POSITIONS_FILE, {})
    return list({*open_pos.keys(), *swing_pos.keys()})
```

`watch_list_health.json` is written by the watch list agent (not `watch_loader`), so the loader only reads it. The cycle reads it to inject into `CycleSnapshot.staleness_guard`.

### 8.3 Regime, research, earnings agents

Each wraps its fetch in `record_success` / `record_failure` + `mark_stale` / `clear_stale` + `_schedule_retry`. Pattern is identical across all three — applied at the top-level try/except in each agent's entry function. No internal logic changes.

---

## 9. `trading/cycle_context.py` change

Add `staleness_guard` to `CycleSnapshot`. Default is `FRESH_GUARD` so all existing tests pass without change.

```python
from utils.data_health import StalenessGuard, FRESH_GUARD

@dataclass
class CycleSnapshot:
    ...
    staleness_guard: StalenessGuard = field(default_factory=lambda: FRESH_GUARD)
```

`build_cycle_snapshot()` accepts an optional `staleness_guard` kwarg (built by cycle before calling the function):

```python
def build_cycle_snapshot(..., staleness_guard: StalenessGuard = FRESH_GUARD) -> CycleSnapshot:
    ...
    return CycleSnapshot(..., staleness_guard=staleness_guard)
```

---

## 10. Per-cycle consumer changes

### 10.1 `trading/cycle.py`

Guard is built **before** `build_cycle_snapshot` — not inside it — so `can_open` can be derived from the guard before the snapshot is constructed:

```python
regime_info = load_macro_regime()
guard       = get_staleness_guard(regime_info)        # SoT: build once

# Existing can_open logic (CRISIS, flash crash, time window) is unchanged.
# Append guard's can_open after existing checks:
if not guard.can_open:
    logger.warning("Regime data stale > %dmin — blocking new entries.",
                   config.DATA_REGIME_CANOPEN_MAX_MINUTES)
    can_open = False

snap = build_cycle_snapshot(
    portfolio_gain_pct=portfolio_gain_pct,
    regime_info=regime_info,
    params=params,
    staleness_guard=guard,
)
```

Intraday retry check at cycle start (before watch list load):

```python
for source_id, job_fn in _INTRADAY_RETRY_MAP.items():   # dict in scheduler_jobs.py
    if _data_tracker and _data_tracker.can_retry(source_id, now):
        logger.info("DATA RETRY (intraday): re-running %s", source_id)
        job_fn()
```

`_INTRADAY_RETRY_MAP = {"research": job_research, "regime": job_macro_regime}` — only sources that can meaningfully be refreshed mid-day.

### 10.2 `signals/entry_gates.py`

`effective_composite_gate` accepts optional `staleness_guard`:

```python
def effective_composite_gate(
    iv: IndicatorValues,
    regime: str,
    params: dict | None = None,
    staleness_guard: StalenessGuard = FRESH_GUARD,
) -> int:
    # ... existing priority chain unchanged ...
    base = ep.composite_min   # from get_entry_params(regime)
    if regime == "NORMAL":
        base = (params or {}).get("composite_min_normal", base)
    return base + staleness_guard.composite_penalty   # additive, single line
```

Callers pass `snap.staleness_guard` — no other changes needed.

### 10.3 `trading/pipeline.py`

**Bug fix — `intra_sent` in `_execute_auto_entries`:**

```python
# Before (wrong): reads macro shock score — inverted metric, wrong field
intra_sent = snap.research.get("__meta__", {}).get("shock_score", 0)

# After (correct): matches pre_claude_filter Layer 5 exactly
intra_sent = snap.intraday_news.get(symbol, {}).get(
    "sentiment_score",
    snap.research.get(symbol, {}).get("sentiment_score", 0),
)
```

**Auto-execute guard gate** (before symbol loop):

```python
def _execute_auto_entries(auto_execute, snap, ...):
    guard = snap.staleness_guard
    if not guard.auto_execute_allowed:
        logger.info("[AUTO-EXEC] blocked — stale sources: %s",
                    ", ".join(sorted(guard.stale_sources & config.DATA_DISABLE_AUTO_EXECUTE)))
        return 0, buying_power, []
    ...
```

**`[STALE]` prefix on cycle log line** when any source is stale:

```python
stale_tag = f"[STALE: {','.join(sorted(snap.staleness_guard.stale_sources))}] " \
            if snap.staleness_guard.stale_sources else ""
logger.info("%s[CYCLE] %d symbols | ...", stale_tag, len(symbols))
```

**Claude context injection** — append `guard.claude_warnings` to `_build_market_context_block()`:

```python
if snap.staleness_guard.claude_warnings:
    warning_block = "\n".join(snap.staleness_guard.claude_warnings)
    context += f"\n\nDATA QUALITY NOTICE:\n{warning_block}\n"
```

### 10.4 `trading/executor.py`

`execute_buy` passes `staleness_guard` to `check_all_gates` for `swing_eligible` override:

```python
swing_eligible_raw = regime in regimes.SWING_ELIGIBLE
swing_eligible = swing_eligible_raw and "regime" not in staleness_guard.stale_sources
```

---

## 11. Position monitor changes (`trading/monitor.py`)

Guard built once at the top of `run_position_monitor()`, before T2 path:

```python
def run_position_monitor() -> None:
    regime_info = load_macro_regime()
    guard       = get_staleness_guard(regime_info)    # SoT: build once
    ...
    # existing SL/TP logic — no guard dependency
    ...
    if guard.t2_allowed:
        _check_tranche2_triggers(open_positions, alpaca, now)
    else:
        logger.debug("T2 blocked — stale sources: %s",
                     ", ".join(sorted(guard.stale_sources & config.DATA_DISABLE_T2)))
```

---

## 12. Swing manager changes (`trading/swing_manager.py`)

Guard built once at the top of each entry function:

```python
def evaluate_eod_holds() -> list[str]:
    regime_info      = load_macro_regime()
    guard            = get_staleness_guard(regime_info)    # SoT
    effective_regime = guard.swing_fallback_regime or regime_info.get("regime", "NORMAL")
    swing_p          = get_swing_params(effective_regime)
    ...

def run_gap_check(alpaca_client) -> None:
    regime_info      = load_macro_regime()
    guard            = get_staleness_guard(regime_info)    # SoT
    effective_regime = guard.swing_fallback_regime or regime_info.get("regime", "NORMAL")
    ...
```

---

## 13. Notification (`notifications/telegram.py`)

One new function. Single call site per source per failure event — no spam.

```python
def notify_data_degraded(source_id: str, guard: StalenessGuard) -> None:
    """One-shot alert when a data source activates stale fallback. Deduped by source+date."""
    # sentinel pattern — same as notify_watch_list's already-sent guard
    today = datetime.now(ET).strftime("%Y-%m-%d")
    sentinel_key = f"{source_id}_{today}"
    if _degraded_alerted.get(sentinel_key):
        return
    _degraded_alerted[sentinel_key] = True

    penalties = [f"+{v} composite" for k, v in config.DATA_COMPOSITE_PENALTY.items()
                 if k in guard.stale_sources and v > 0]
    blocks    = []
    if not guard.auto_execute_allowed: blocks.append("auto-execute")
    if not guard.t2_allowed:           blocks.append("T2")

    msg = (
        f"⚠️ <b>DATA DEGRADED — {source_id.upper()}</b>\n"
        f"Fallback data active. Penalties: {', '.join(penalties) or 'none'}. "
        f"Blocked: {', '.join(blocks) or 'none'}."
    )
    _send(msg)
```

---

## 14. Auto-Execute Bug — Root Cause & Fix Summary

**Root cause:** `_execute_auto_entries` (pipeline.py) pulled `intra_sent` from `snap.research["__meta__"]["shock_score"]`. This is the macro shock score (0–100, higher = worse macro conditions), not the symbol's intraday sentiment (-100 to +100, higher = more bullish). The two scales are inverted and semantically unrelated.

**Effect:** Every auto-execute trade since the feature was built has computed confidence using the wrong input. Specifically, `_compute_auto_execute_confidence` adds a bonus proportional to `intra_sent` — with macro shock (always ≥ 0), this added a spurious confidence bonus on bad macro days.

**Fix:** Use the same `intra_sent` computation as `pre_claude_filter` Layer 5 — the value that actually qualified this symbol for auto-execute. One-line change, zero behaviour change on good-data days (they match).

---

## 15. File Change Map

| File | Type | Key change |
|---|---|---|
| `config.py` | Edit | New `# ── Data Resilience ──` section — 13 constants |
| `utils/data_health.py` | **New** | `DataHealthTracker` singleton, `StalenessGuard`, `get_staleness_guard()`, `FRESH_GUARD` |
| `utils/http.py` | Edit | Retry on 5xx; fast-fail + ERROR log on 401/403 |
| `utils/io_utils.py` | Edit | `mark_stale()` + `clear_stale()` helpers |
| `scheduler_jobs.py` | Edit | `set_data_tracker()`, `_schedule_retry()`, `_INTRADAY_RETRY_MAP` |
| `main.py` | Edit | Call `set_data_tracker(_tracker)` after `set_scheduler()` |
| `trading/cycle_context.py` | Edit | Add `staleness_guard: StalenessGuard = FRESH_GUARD` to `CycleSnapshot` |
| `trading/cycle.py` | Edit | Build guard before snapshot; wire `guard.can_open`; intraday retry check |
| `signals/entry_gates.py` | Edit | `effective_composite_gate` adds `staleness_guard.composite_penalty` |
| `trading/pipeline.py` | Edit | Fix `intra_sent` bug; `auto_execute_allowed` gate; `[STALE]` log prefix; Claude warning injection |
| `trading/executor.py` | Edit | Pass guard to `check_all_gates`; `swing_eligible` override when regime stale |
| `trading/monitor.py` | Edit | Build guard at entry; gate T2 on `guard.t2_allowed` |
| `trading/swing_manager.py` | Edit | Build guard at each entry; use `guard.swing_fallback_regime` |
| `agents/volatility_screener.py` | Edit | Detect all-None batch; `record_failure` + retry; bypass filter on API failure |
| `trading/watch_loader.py` | Edit | Merge held tickers; read `watch_list_health.json` |
| `notifications/telegram.py` | Edit | `notify_data_degraded()` with dedup sentinel |
| `tests/test_data_health.py` | **New** | `DataHealthTracker`, `StalenessGuard`, guard factory, backoff schedule, stale_sources |
| `tests/test_entry_gates.py` | Edit | Staleness penalty applied via guard; FRESH_GUARD baseline |
| `tests/test_trading_pipeline.py` | Edit | Auto-execute guard block; `intra_sent` fix; `[STALE]` log assertion |
| `tests/test_foundation.py` | Edit | `swing_manager` + `monitor` stale-regime guard tests |

**2 new files. 18 edits. 0 new external dependencies.**

---

## 16. Invariants & Rules

- **Rule 24 (new):** `get_staleness_guard()` is the single factory for `StalenessGuard`. No module evaluates staleness thresholds, computes composite penalties, or decides T2/auto-execute eligibility outside `utils/data_health.py`.
- **Rule 25 (new):** Guard is built exactly once per scheduler entry point (cycle, monitor, EOD) before any downstream stage that uses it. Never built mid-stage or shared across scheduler boundaries.
- **Rule 26 (new):** All data resilience constants live in `config.py` under `# ── Data Resilience ──`. No magic numbers in `data_health.py`, agent files, or job functions.
- **Existing Rule 12 holds:** Auto-execute is blocked by `guard.auto_execute_allowed` before `execute_buy` is called. `check_all_gates` is still called inside `execute_buy` regardless — the guard is an additional outer gate, not a replacement.
