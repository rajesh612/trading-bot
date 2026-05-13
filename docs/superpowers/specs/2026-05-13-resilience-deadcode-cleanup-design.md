# Resilience Fixes + Dead Code Cleanup Design

**Date:** 2026-05-13  
**Goal:** Fix six verified bugs that cause zero-trade days, clean up all dead code found in the audit, and eliminate redundant disk reads — no new modules, only targeted changes to existing files.

---

## Root Cause Context

May 8 and May 13 produced zero-trade days through two independent failure paths:
1. A single HTTP 429 tripped `_CLAUDE_DISABLED` permanently — no auto-recovery.
2. The in-memory `_entry_exhausted_flag` stayed `True` from a prior day on continuous (Linux cloud) deployment — `is_entry_budget_exhausted()` short-circuited all entries before the disk budget could show today was fresh.

Both failures silently killed the entire entry pipeline. The fixes below close all six gaps identified in the audit.

---

## Fix 1 — `_entry_exhausted_flag` Day Reset

**File:** `trading/risk_manager.py`

`is_entry_budget_exhausted()` returns the in-memory `_entry_exhausted_flag` directly, bypassing `load_budget()`. If the bot runs continuously overnight and yesterday's entry pool was exhausted, the flag stays `True` all of today.

**Fix:** Inside `check_entry_budget()`, after `load_budget()` returns, sync the in-memory flag when the disk budget is fresh for today:

```python
b = load_budget()
if not b.get("entry_exhausted"):          # fresh-day budget → clear stale flag
    global _entry_exhausted_flag
    _entry_exhausted_flag = False
```

This ensures the flag is authoritative-from-disk at the first budget call of each new day, with zero extra I/O.

---

## Fix 2 — Circuit Breaker TTL + Exponential Backoff

**File:** `trading/claude_caller.py`

`_CLAUDE_DISABLED` is a `threading.Event` that is set on HTTP 402/429 and **never cleared**. Any rate-limit hit at 10 AM kills all Claude calls for the rest of the day.

**Fix:** Add a `_CLAUDE_DISABLED_UNTIL: float = 0.0` module-level monotonic timestamp. On exception:
- `429` (rate limit) → 5-min TTL (transient, clears fast)
- `402` (billing) → 30-min TTL + Telegram alert

At each call site where `_CLAUDE_DISABLED.is_set()` is checked, also check if the TTL has expired — if so, clear the event and allow the call to proceed as a probe. If the probe fails again, double the remaining TTL (exponential backoff, capped at 30 min). If it succeeds, TTL resets to zero.

Backoff state is carried entirely in `_CLAUDE_DISABLED_UNTIL` — each new trip doubles the remaining TTL (capped at 30 min). No separate counter needed.

---

## Fix 3 — `daily_closed` as Layer 0 in Pre-Claude Filter

**Files:** `trading/pipeline.py`, `trading/pre_claude_filter.py`

`pipeline.py:113` calls `load_daily_closed()` but discards the return value. Symbols already closed today pass through all 5 filter layers and reach Claude before being blocked in `executor.py` — wasting API budget.

**Fix:**
- `pipeline.py`: `daily_closed = load_daily_closed()` — capture the set.
- Pass `daily_closed` to `pre_claude_filter()` as a new parameter.
- Add **Layer 0** at the top of `pre_claude_filter()`:
  ```python
  if symbol in daily_closed:
      return FilterResult("SKIP", "already closed today")
  ```
- Update the `FilterResult` dataclass if needed — no change required, Layer 0 reuses the same SKIP path.

---

## Fix 4 — Eliminate Redundant Disk Reads in `trading_agent.py`

**Files:** `trading_agent.py`, `trading/cycle_context.py`

`run_trading_cycle()` makes three reads before calling `build_cycle_snapshot()`, which then makes them all again:
- `load_macro_regime()` at line 213 → re-read inside `build_cycle_snapshot()` at `cycle_context.py:71`
- `load_trading_params()` at line 215 → re-read inside `build_cycle_snapshot()`
- `load_json(config.FRED_CACHE_FILE)` at lines 217-220 → re-read via `_get_fred_cache()` inside snapshot **and the local variable is never used afterward** (dead code)

**Fix:**
- Extend `build_cycle_snapshot(portfolio_gain_pct, *, regime_info=None, params=None)` — if callers pass these in, skip the inner loads.
- `trading_agent.py`: pass `regime_info=regime_info, params=params` to avoid re-reads.
- Remove lines 217-220 (dead `fred_cache` load).

Net: 3 disk reads eliminated per cycle = 108 fewer reads/day (36 cycles × 3).

---

## Fix 5 — Auto-Execute Runs Regardless of Entry Budget Exhaustion

**File:** `trading/pipeline.py`

`pipeline.py:143-158` wraps the **entire** pre-Claude filter loop in `if not is_entry_budget_exhausted()`. When budget is exhausted, auto-execute candidates are never collected — the one entry path that costs $0 in API budget is silently killed.

**Fix:** Restructure Phase 2 so auto-execute is always collected; only the Claude batch is budget-gated:

```python
# Always run pre-filter loop
for symbol, iv, existing_pos, _ret5d in stock_data:
    if existing_pos:
        ...log bracket-managed...
        continue
    filt = pre_claude_filter(symbol, iv, snap, daily_closed)
    if filt.decision == "SKIP":
        logger.info("SKIP-PRECLAUDE ...")
    elif filt.decision == "AUTO_EXECUTE":
        auto_execute.append((symbol, iv))      # ← always, budget-exempt
        logger.info("AUTO-EXECUTE ...")
    elif not risk_manager.is_entry_budget_exhausted():
        needs_claude.append((symbol, iv, None))
    else:
        logger.info("BUDGET-EXHAUSTED HOLD %s: would need Claude", symbol)
```

No extra network calls: Phase 1 already fetched all data. `pre_claude_filter()` is pure in-memory. Auto-execute's `execute_buy()` (1 disk read + 1 bar fetch) is unavoidable for any trade.

---

## Fix 6 — Remove `gain_pct` Dead Parameter

**Files:** `trading/risk_manager.py`, `trading/claude_caller.py`

`check_entry_budget(estimated_cost, gain_pct, regime)` — `gain_pct` is accepted but never referenced in the function body. Remove from signature and all callers.

---

## Dead Code Removal

| Location | Dead code | Reason |
|----------|-----------|--------|
| `trading_agent.py:34` | `from signals.entry_gates import check_all_gates` | Never called in this file; called via `executor.py` |
| `trading_agent.py:61–69` | `load_research()`, `load_weights()` | Superseded by `build_cycle_snapshot()` in `cycle_context.py` |
| `trading_agent.py:217–220` | `fred_cache = load_json(...)` try/except block | Variable assigned but never read (Fix 4 above) |
| `trading/claude_caller.py:49–51` | `is_claude_disabled()` public function | Exported but never called anywhere outside tests |

---

## Test Coverage

All new tests go in existing files per the file-naming convention:

| Test class | File | What it covers |
|-----------|------|----------------|
| `TestEntryExhaustedFlagReset` | `tests/test_pipeline.py` | Flag syncs to False when `load_budget()` returns fresh-day dict |
| `TestCircuitBreakerTTL` | `tests/test_pipeline.py` | 429 → 5 min TTL; 402 → 30 min; auto-clear after TTL; backoff doubling |
| `TestAutoExecuteWhenBudgetExhausted` | `tests/test_pipeline.py` | Auto-execute candidates collected and executed even when `_entry_exhausted_flag=True` |
| `TestPreClaudeFilterDailyClosed` | `tests/test_pipeline.py` | Symbol in daily_closed returns SKIP (Layer 0); symbol not in daily_closed passes through |
| Update `TestPreClaudeFilter` | `tests/test_pipeline.py` | Add `daily_closed=set()` param to all existing test calls |
| Update `TestRiskManagerAdditions` | `tests/test_pipeline.py` | Remove `gain_pct` from `check_entry_budget` call |

---

## Files Changed

| File | Change type |
|------|-------------|
| `trading/risk_manager.py` | Fix 1 + Fix 6 (flag sync, remove gain_pct param) |
| `trading/claude_caller.py` | Fix 2 + dead code (TTL, remove is_claude_disabled) |
| `trading/pipeline.py` | Fix 3 + Fix 5 (daily_closed, auto-execute guard) |
| `trading/pre_claude_filter.py` | Fix 3 (daily_closed Layer 0) |
| `trading/cycle_context.py` | Fix 4 (optional regime_info/params params) |
| `trading_agent.py` | Fix 4 + dead code (pass regime_info/params, remove 3 dead items) |
| `tests/test_pipeline.py` | New + updated tests |

No new modules. No interface changes visible to callers outside the `trading/` package.
