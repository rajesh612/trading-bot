# Intraday News Monitoring + Auto-Execute Pipeline

**Date:** 2026-05-13  
**Status:** Approved — extended with root-cause bug fixes (May 8 + May 13 zero-trade post-mortem)

---

## Goal

1. Fix 7 confirmed root-cause bugs that caused zero trades on May 8, May 12, and May 13 despite the bot running all day.
2. Add a lightweight intraday news refresh (every 30 min) so the bot reacts to breaking news during the trading day — both to block bad trades and to enable opportunistic entries.
3. Wire in a three-tier decision pipeline: hard-block → auto-execute → Claude. Eliminates 60–80% of wasteful Claude calls while preserving Claude's judgment for genuinely ambiguous setups.

---

## Root Cause Post-Mortem — May 8 and May 13 (bot not run May 12)

### What Happened

- **9:30–11:30 AM ET**: 12 cycles ran. Each cycle sent 10–15 stocks to Claude in 3 batch calls. All 36+ calls returned HOLD. $0.687 of $0.70 entry budget consumed.
- **11:30 AM ET**: Entry budget ceiling hit. Five hours of trading day remained. Zero trades placed.
- **11:30 AM–3:50 PM ET**: 22 more cycles ran. The pipeline STILL built `needs_claude` lists and fired 3 blocked calls per cycle (66 blocked call attempts doing wasted work — no trades possible but CPU and log spinning all afternoon).

### Confirmed Bugs

| # | Bug | Location | Impact |
|---|-----|----------|--------|
| **B1** | RSI/Stochastic hard vetos defined in `entry_gates.py` never prevent Claude calls — they run POST-Claude in `executor.py`, not PRE-Claude in `pipeline.py` Phase 2 | `trading/pipeline.py` Phase 2, `trading/executor.py` | ~50% wasted Claude tokens |
| **B2** | `COMPOSITE_SCORE_THRESHOLD=15` sends stocks to Claude when composite is 15–40, but Claude applies a 40-point minimum gate — guaranteed HOLD for the entire 15–40 range | `config.py`, `trading/pipeline.py` | ~40% wasted Claude tokens |
| **B3** | No time-of-day budget reservation — first 2 hours consume 100% of entry budget, leaving 0 for the prime 1–3 PM ET window | `trading/risk_manager.py` | Afternoon always dead |
| **B4** | After budget exhaustion, the pipeline still builds `needs_claude` batches and fires blocked Claude calls every cycle for the rest of the day | `trading/pipeline.py` Phase 2 | Wasted CPU + log spam post-exhaustion |
| **B5** | Watch list agent selects stocks with no technical pre-filter — today's full list had Stochastic >88 or RSI >70 at market open; all entered Claude and returned HOLD | `watch_list_agent.py` | 100% HOLD day when market opens overbought |
| **B6** | Exit budget ($0.30) fully unused when no positions held; no rollover to entry pool | `trading/risk_manager.py` | 30% of daily budget stranded |
| **B7** | Code comment in `entry_gates.py` explicitly states RSI/Stoch checks "save Claude tokens on conditions that will HOLD anyway" — design intent was pre-Claude; implementation placed them post-Claude | `signals/entry_gates.py`, `trading/executor.py` | Documented design intent never fulfilled |

---

## Background / Current State

- Research runs **once at 9:15 AM**. Any breaking news during the day (FDA ruling, earnings shock, macro event) is invisible until the next morning.
- `AUTO_BUY_COMPOSITE_THRESHOLD = 35` and `AUTO_BUY_MIN_SENTIMENT = -10` exist in `config.py` but are **not wired** — every stock above `composite_score > 15` still calls Claude unconditionally.
- Claude is called every 10 min for all watch-list stocks above threshold (2–3 batch calls per cycle, ~18–27 entry calls/day plus separate exit evaluations).

---

## Bug Fixes

### Fix B1 + B7: Pre-Claude Fast Gate (pipeline Phase 2)

**Problem:** `check_all_gates()` is called only in `executor.py` after Claude says BUY. Deterministic hard-veto checks (RSI, Stochastic) never run before the Claude call.

**Fix:** Add a `fast_gate(iv)` function in `signals/entry_gates.py` that runs only indicator-based hard-veto checks available from `IndicatorValues` (no disk I/O, no extra API calls). Pipeline Phase 2 calls `fast_gate()` on every candidate before adding to `needs_claude`. If it fails, the symbol is logged and skipped — no Claude call.

```python
def fast_gate(iv: IndicatorValues) -> GateResult:
    """Deterministic pre-Claude check. No I/O. Called in pipeline Phase 2."""
    if iv.rsi > config.RSI_HARD_SKIP_THRESHOLD:
        return GateResult(False, f"RSI hard skip (RSI={iv.rsi:.1f})")
    if iv.stoch_d > config.STOCH_HARD_VETO_THRESHOLD:
        return GateResult(False, f"Stochastic hard veto (%D={iv.stoch_d:.1f})")
    if iv.parabolic and config.PARABOLIC_ENTRY_BLOCK:
        return GateResult(False, "parabolic move")
    if iv.stop_hunt_zone:
        return GateResult(False, "stop-hunt zone")
    return GateResult(True)
```

The full `check_all_gates()` in `executor.py` remains unchanged (it catches additional conditions after a BUY decision). This is additive — it does not replace the executor check.

**Expected impact:** Eliminates ~50% of Claude calls on overbought-market days.

---

### Fix B2: Raise Pre-Claude Composite Threshold

**Problem:** `COMPOSITE_SCORE_THRESHOLD=15` sends composite 15–40 stocks to Claude; Claude applies a 40-point minimum and always returns HOLD.

**Fix:** Add a new config constant `PRE_CLAUDE_COMPOSITE_MIN = 40`. Pipeline Phase 2 uses this as the threshold for adding to `needs_claude`. The existing `COMPOSITE_SCORE_THRESHOLD = 15` stays — it controls whether a symbol is even worth computing indicators for (future use). Rationale: 40 matches Claude's observed effective gate; the 15–40 zone is pure token waste.

```python
PRE_CLAUDE_COMPOSITE_MIN = 40  # pipeline Phase 2 gate — below this never reaches Claude
COMPOSITE_SCORE_THRESHOLD = 15  # existing: indicator-compute skip threshold (kept)
```

**Expected impact:** Eliminates ~40% of Claude calls when market opens flat/moderate.

---

### Fix B3: Time-of-Day Budget Reservation

**Problem:** Entry budget is consumed uniformly; first 2 hours exhaust $0.70, afternoon window gets nothing.

**Fix:** Add time-based budget caps in `risk_manager.check_entry_budget()`. The entry pool is split into three windows using ET time:

| Window | Hours ET | Max cumulative spend |
|--------|----------|----------------------|
| Morning open | 9:30–11:30 AM | 35% of entry ceiling |
| Midday | 11:30 AM–1:00 PM | 65% of entry ceiling |
| Prime afternoon | 1:00–3:55 PM | 100% of entry ceiling |

If `entry_spent_usd` already exceeds the window cap, `check_entry_budget()` returns False. The cap is only enforced during market hours. New config constants:

```python
BUDGET_MORNING_CAP_PCT  = 0.35   # max 35% of entry_ceiling by 11:30 AM ET
BUDGET_MIDDAY_CAP_PCT   = 0.65   # max 65% of entry_ceiling by 1:00 PM ET
```

**Expected impact:** Preserves 35% of entry budget for the prime 1–3 PM ET window regardless of morning activity.

---

### Fix B4: Budget Pre-Check Before Building `needs_claude`

**Problem:** Pipeline builds `needs_claude` batches and fires Claude calls even after budget is exhausted — wasting effort for 5+ hours post-exhaustion.

**Fix:** Add a single budget pre-check at the start of Phase 2. If `risk_manager.is_entry_budget_exhausted()` returns True, skip Phase 2 and Phase 3 entirely and log once per cycle ("entry budget exhausted — skipping Claude phase"). Expose `is_entry_budget_exhausted()` as a cheap read-only check (no locking needed — reads `entry_exhausted` flag).

**Expected impact:** Eliminates all wasted batch-building and blocked-call logs from 11:30 AM to market close on exhausted-budget days.

---

### Fix B5: Watch List Technical Pre-Filter

**Problem:** Watch list agent at 9:00 AM selects stocks with no check on whether they're already technically overbought. On strong gap-up days the entire list is in hard-veto territory before the first trade cycle.

**Fix:** In `watch_list_agent.py`, after building the candidate list and before writing `watch_list.txt`, fetch a quick snapshot of RSI and Stochastic for each candidate using the last available 5-min bar. Filter out any symbol where RSI > 70 OR Stochastic %D > 80 at 9:00 AM. If fewer than 6 symbols remain after filtering, relax to RSI > 74 / Stochastic > 85 to ensure a minimum viable list.

New config constants:
```python
WATCH_LIST_RSI_FILTER      = 70   # exclude if RSI > this at 9:00 AM snapshot
WATCH_LIST_STOCH_FILTER    = 80   # exclude if Stochastic %D > this at 9:00 AM snapshot
WATCH_LIST_MIN_AFTER_FILTER = 6   # relax filter if fewer symbols would remain
```

**Expected impact:** On gap-up overbought days, eliminates an entire category of guaranteed-HOLD Claude calls. On normal days, has no effect.

---

### Fix B6: Exit Budget Rollover to Entry

**Problem:** Exit budget ($0.30) is never used on days with no open positions. Entry budget exhausts at $0.70; the $0.30 is stranded.

**Fix:** In `risk_manager`, add `try_rollover_exit_to_entry()`. At 12:30 PM ET, if `exit_spent_usd = 0` and no open positions exist, move the unused exit ceiling into the entry pool: `entry_ceiling_usd += exit_ceiling_usd * BUDGET_ROLLOVER_FRACTION`. Cap the rolled-over amount at `EXIT_ROLLOVER_MAX_USD`.

```python
BUDGET_ROLLOVER_FRACTION = 0.5       # roll over 50% of unused exit budget
EXIT_ROLLOVER_MAX_USD    = 0.15      # cap rollover at $0.15
BUDGET_ROLLOVER_HOUR     = 12        # ET hour at which rollover is evaluated
BUDGET_ROLLOVER_MINUTE   = 30
```

This adds at most $0.15 to the entry pool on no-position midday days, extending the afternoon window.

Scheduler addition: one-shot job at 12:30 PM ET, `max_instances=1, misfire_grace_time=300`.

---

## Architecture

### New Module: `intraday_news.py`

Pure data-fetch module. No Claude calls.

**Responsibilities:**
- Fetch symbol-specific RSS headlines using the existing `_fetch_stock_news(symbol)` helper in `research/fetchers.py` (per-symbol search, same fetch used by the full research agent).
- Fetch StockTwits bullish/bearish tag counts per symbol using the existing `_fetch_stocktwits(symbol)` helper in `research/fetchers.py`.
- Compute per-symbol `shock_score` by applying the existing `_compute_macro_shock_score(headlines)` from `research/analyzer.py` to the symbol's headlines (same keyword logic, but scoped to per-symbol news rather than market-wide feeds).
- Compute per-symbol `sentiment_score` via a new `_compute_symbol_sentiment(headlines, stocktwits)` function defined in `intraday_news.py`: adds +5 per positive keyword hit ("beat", "upgrade", "approval", "surge", "record"), subtracts -5 per negative keyword hit ("miss", "downgrade", "recall", "warning", "cut"), then adds the StockTwits bull ratio (0–100 scaled to -15 to +15). Result is clamped to [-100, +100]. No Claude calls.
- Write `intraday_news_cache.json` via `utils.io_utils.save_json`.

**Output schema (`intraday_news_cache.json`):**
```json
{
  "__updated_at": "2026-05-13T11:00:00-04:00",
  "NVDA": { "sentiment_score": 22, "shock_score": 0, "headlines": ["..."] },
  "TSLA": { "sentiment_score": -5, "shock_score": 72, "headlines": ["Tesla recall announced..."] }
}
```

**Public API:**
```python
def refresh_intraday_news(watch_list: list[str]) -> None: ...
def load_intraday_news() -> dict: ...  # returns cache or {} on missing/stale
```

`load_intraday_news()` returns `{}` (not an error) if the cache file is missing or older than `INTRADAY_NEWS_STALE_MIN` minutes — callers fall back to the 9:15 AM research sentiment gracefully.

---

### Scheduler Addition (`main.py`)

```
Every 30 min (9:30 AM – 3:30 PM ET): intraday_news.refresh_intraday_news(watch_list)
  max_instances=1, misfire_grace_time=30
```

Runs independently of the trading cycle. The trading cycle reads the cache; it does not call `refresh_intraday_news()` directly.

---

### Pipeline Changes (`trading/pipeline.py`)

Phase 2 (gate checks) is replaced by the three-tier decision:

#### Tier 1 — Hard Block (no Claude, no entry)

Condition: `composite_score < config.COMPOSITE_SCORE_THRESHOLD` **OR** `shock_score >= config.INTRADAY_NEWS_SHOCK_BLOCK`

- Symbol is skipped for new entries.
- **If an open position exists** and `shock_score >= INTRADAY_NEWS_SHOCK_BLOCK`: symbol is added to `exit_candidates` immediately (Phase 2.5), regardless of composite drop threshold.
- No Claude call.

#### Tier 2 — Auto-Execute (no Claude, immediate BUY)

Condition (all must be true):
- `composite_score > config.AUTO_EXECUTE_COMPOSITE_MIN` (45)
- `intraday_sentiment_score > config.AUTO_EXECUTE_SENTIMENT_MIN` (15)
- `shock_score < config.INTRADAY_NEWS_SHOCK_BLOCK` (60)
- All existing `check_all_gates()` pass
- No open position for this symbol
- `can_open` is True and buying power sufficient

Before calling `execute_buy()`, the pipeline calls `check_all_gates()` explicitly. If gates fail, the symbol falls through to Tier 3 (Claude) rather than being silently skipped — so Claude still sees it and can decide. If gates pass, `execute_buy()` is called with `source="auto_execute"` written to the trade journal entry. Claude is not called.

**Fallback:** If `intraday_news_cache.json` is stale or missing, auto-execute is disabled for that cycle — the symbol falls through to Tier 3 (Claude).

#### Tier 3 — Claude (existing batch path)

Condition: `composite_score >= COMPOSITE_SCORE_THRESHOLD` AND not qualifying for Tier 1 block or Tier 2 auto-execute.

Identical to today's Claude batch call, with one addition: `build_stock_block()` receives the intraday headline summary (top 3 headlines, sentiment score, shock score) so Claude has current news context in its prompt.

---

### Exit Evaluation Enhancement (Phase 2.5)

Existing thesis-break logic is unchanged. One new trigger is added:

- At the start of Phase 2.5, load `intraday_news_cache.json`.
- Any open position whose symbol has `shock_score >= INTRADAY_NEWS_SHOCK_BLOCK` is added to `exit_candidates`, even if the composite drop threshold (`EXIT_EVAL_COMPOSITE_DROP`) has not been reached.
- This ensures a mid-day news shock on a held position triggers an exit evaluation in the current cycle rather than waiting.

---

## Config Changes (`config.py`)

Added under `# ── Intraday News + Auto-Execute ──`:

```python
INTRADAY_NEWS_REFRESH_MIN     = 30   # scheduler cadence (minutes)
INTRADAY_NEWS_SHOCK_BLOCK     = 60   # shock_score >= this → hard-block new entries & add held positions to exit eval
INTRADAY_NEWS_STALE_MIN       = 45   # cache older than this → fall back to research sentiment; disable auto-execute
AUTO_EXECUTE_COMPOSITE_MIN    = 45   # composite must exceed this for auto-execute
AUTO_EXECUTE_SENTIMENT_MIN    = 15   # intraday sentiment_score must exceed this for auto-execute
```

Added under `# ── Bug Fixes: Token Efficiency ──`:

```python
PRE_CLAUDE_COMPOSITE_MIN      = 40   # pipeline Phase 2 gate — below this, symbol never reaches Claude (Fix B2)
BUDGET_MORNING_CAP_PCT        = 0.35  # max 35% of entry_ceiling by 11:30 AM ET (Fix B3)
BUDGET_MIDDAY_CAP_PCT         = 0.65  # max 65% of entry_ceiling by 1:00 PM ET (Fix B3)
BUDGET_ROLLOVER_FRACTION      = 0.5   # fraction of unused exit budget to roll into entry at 12:30 PM (Fix B6)
EXIT_ROLLOVER_MAX_USD         = 0.15  # cap on rollover (Fix B6)
BUDGET_ROLLOVER_HOUR          = 12    # ET hour for rollover evaluation (Fix B6)
BUDGET_ROLLOVER_MINUTE        = 30
WATCH_LIST_RSI_FILTER         = 70    # exclude from watch list if RSI > this at 9 AM snapshot (Fix B5)
WATCH_LIST_STOCH_FILTER       = 80    # exclude from watch list if Stochastic %D > this at 9 AM snapshot (Fix B5)
WATCH_LIST_MIN_AFTER_FILTER   = 6     # relax filter if fewer symbols would survive (Fix B5)
```

Removed (unwired, superseded):
- `AUTO_BUY_COMPOSITE_THRESHOLD` → replaced by `PRE_CLAUDE_COMPOSITE_MIN` and `AUTO_EXECUTE_COMPOSITE_MIN`
- `AUTO_BUY_MIN_SENTIMENT` → replaced by `AUTO_EXECUTE_SENTIMENT_MIN`

---

## Data Flow

```
9:15 AM  research_agent  →  research_report.json  (full sentiment, Reddit, StockTwits, Claude scoring)
9:30 AM+ intraday_news   →  intraday_news_cache.json  (RSS + StockTwits only, no Claude, every 30 min)

Every 10 min trading cycle:
  load intraday_news_cache  (stale? → fallback to research_report sentiment)
  for each symbol in watch_list:
    Tier 1: shock >= 60 OR composite < 15  → skip (or exit eval if held)
    Tier 2: composite > 45 AND sentiment > 15 AND gates pass  → auto-execute BUY
    Tier 3: composite >= 15 (rest)  → Claude batch (with fresh headlines in prompt)
```

---

## Error Handling

| Failure | Behaviour |
|---|---|
| `intraday_news_cache.json` missing | `load_intraday_news()` returns `{}`. Pipeline reads `sentiment_score` per symbol from `research_report.json` (the 9:15 AM report) and treats `shock_score` as 0 for all symbols. Auto-execute disabled. |
| Cache stale (> 45 min) | Same as missing. `research_report.json["<SYMBOL>"]["sentiment_score"]` used; `__meta__.macro_shock_score` is NOT used (it is a market-wide score, not per-symbol). |
| RSS fetch error in `refresh_intraday_news` | Logged as warning. Previous cache retained. No crash. |
| StockTwits fetch error | Sentinel `sentiment_score = 0` used for that symbol. |
| `shock_score` key absent for symbol | Treated as `shock_score = 0` (no block). |

---

## Testing (`tests/test_trading.py`)

All new tests are `@pytest.mark.unit`. No network calls.

| Test | What it verifies |
|---|---|
| `test_hard_block_on_shock` | composite > 15 but shock ≥ 60 → symbol not in `needs_claude` or auto-execute list |
| `test_hard_block_on_low_composite` | composite < 15, shock = 0 → same outcome (no entry path) |
| `test_auto_execute_threshold` | composite > 45 + sentiment > 15, all gates pass → BUY queued, Claude not called |
| `test_auto_execute_falls_through_to_claude` | composite > 45 but sentiment = 10 → symbol in `needs_claude` |
| `test_auto_execute_disabled_when_stale` | missing/stale cache → symbol in `needs_claude`, auto-execute not triggered |
| `test_news_shock_on_open_position` | shock ≥ 60 on held symbol → added to exit candidates regardless of composite drop |
| `test_intraday_news_fallback_to_research` | stale cache → pipeline reads `research_report.json` sentiment instead |
| `test_intraday_headlines_in_claude_prompt` | Tier 3 stock block includes top-3 headlines and sentiment score |

---

## Token Impact

| Path | Before | After |
|---|---|---|
| RSI > 72 or Stochastic > 88 (Fix B1) | 1 Claude call (HOLD) | 0 Claude calls |
| Composite 15–40 (Fix B2) | 1 Claude call (HOLD) | 0 Claude calls |
| Clean textbook setup (composite > 45, good news — auto-execute) | 1 Claude call | 0 Claude calls |
| News-hostile symbol (shock ≥ 60) | 1 Claude call (HOLD) | 0 Claude calls |
| Post-budget-exhaustion cycles (Fix B4) | 3 blocked batch attempts/cycle × 22 cycles | 0 (phase skipped) |
| Ambiguous setup (composite 40–45, neutral news) | 1 Claude call | 1 Claude call (unchanged) |
| Exit on held position with news shock | Only if composite dropped | Also triggered by shock score |

**Combined estimated reduction:**
- May 13 scenario (overbought open, 42 calls, 0 trades): from 42 calls → estimated **6–10 calls** (only the genuinely ambiguous mid-composite setups reach Claude)
- Budget remaining at 1 PM ET: from **$0** → estimated **$0.40+** (time-cap + rollover preserving afternoon window)
- Probability of ≥1 trade on a day like today: from **0%** → depends on whether any setup clears all gates, but the afternoon window is no longer dead

---

## Files Touched

| File | Change | Bug fixed |
|---|---|---|
| `intraday_news.py` | New module — 30-min RSS+StockTwits refresh | new feature |
| `config.py` | 11 new constants; remove 2 stale ones | B2, B3, B5, B6 |
| `signals/entry_gates.py` | Add `fast_gate(iv)` function | B1, B7 |
| `trading/pipeline.py` | Pre-Claude fast gate + budget pre-check + three-tier decision + shock-exit trigger | B1, B2, B4 |
| `trading/risk_manager.py` | Time-of-day budget caps + `is_entry_budget_exhausted()` + rollover logic | B3, B4, B6 |
| `trading/context_builder.py` | `build_stock_block()` gains optional intraday headline param | new feature |
| `watch_list_agent.py` | Technical pre-filter (RSI/Stoch) after candidate selection | B5 |
| `main.py` | 30-min intraday news job + 12:30 PM rollover job | new feature, B6 |
| `tests/test_trading.py` | 8 intraday news + auto-execute tests | new feature |
| `tests/test_foundation.py` | 7 bug-fix regression tests (fast gate, budget caps, rollover, watch list filter) | B1–B6 |
