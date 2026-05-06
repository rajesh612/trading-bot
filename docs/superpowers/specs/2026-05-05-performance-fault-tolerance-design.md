# Trading Bot — Performance, Fault Tolerance & Claude Context Design
**Date:** 2026-05-05  
**Status:** Approved

---

## Design Principles
All new functions are **atomic** (single responsibility, no side effects), **pure where possible** (inputs → output, no file I/O inside), and **reusable** across modules. Shared utilities (`_fetch_with_retry`, `_build_market_context_block`) live in the module that owns the concern, imported by callers — no circular dependencies.

---

## Goals

1. Fix critical bugs (timedelta import, silent API degradation)
2. Send all relevant market context to Claude without token waste
3. Parallelize network I/O in research agent and trading cycle
4. Add retry logic to all external fetches
5. Clean stale data aggressively to keep file reads fast
6. Reduce logging noise while keeping signal

---

## Changes by Module

### A. `trading_agent.py`

#### A1. Fix timedelta import (line 6) — Critical bug
`from datetime import datetime` → `from datetime import datetime, timedelta`  
Without this, `archive_old_journal_entries()` crashes when journal has entries older than 90 days.

#### A2. Enrich Claude system prompt — `_build_market_context_block()`
New pure function assembled once per trading cycle, passed as a **cached system block** (qualifies for prompt caching since total system prompt exceeds 1024 tokens with strategy_memory).

Contents (~150 tokens):
```
MARKET CONTEXT (HH:MM ET):
Regime: <REGIME> | health=<±N> | VIX=<N.N>
FRED: Fed <cutting|hiking|paused> | CPI <rising|falling|stable> | Unemployment <rising|stable>
Event risk: <None active | EVENT_NAME in N min>
Macro shock: <N>/100 [keyword summary if >0]
Sector sentiment: <SECTOR>=<±N> …
Capitulation watch: <SYM1, SYM2 | None>
```

Sources:
- `macro_regime.json` → regime, health score, VIX
- `fred_cache.json` → fed_direction, cpi_trend, unemployment_rising
- `economic_calendar.json` → next event name + minutes until window opens
- `research_report.json.__meta__` → macro_shock_score, sector_sentiment, capitulation_candidates

**Token optimization:** Block is identical for all stocks in the batch → single cache write on first call, cache reads on subsequent batches within the same cycle. Saves ~150 tokens × (batch_size − 1) per cycle after first.

#### A3. Per-stock PEAD line in `_build_stock_block()`
Add one line to each stock block (user message, not cached):
```
PEAD: 4/5 conditions (score=80) — expect multi-day drift   [only if pead_ok]
```
Pulls from `detect_pead_setup(symbol)` already called by `get_active_mode()`. No additional API calls.

#### A4. Parallel Phase 1 bar fetching
Replace the sequential `for symbol in watch_list` bar-fetch loop with `ThreadPoolExecutor(max_workers=8)`. Each task: `(get_intraday_bars, get_daily_bars, compute_indicators)` for one symbol. Results collected with `futures.as_completed()`. Thread-safe because each symbol writes to its own local result. Estimated speedup: ~4× for 12-symbol list (from ~4s → ~1s).

#### A5. Research fallback warning
In `load_research()`, after loading the file, check:
- If report has no symbols (excluding `__meta__`) → log WARNING "Research report empty — Claude will have no sentiment context"
- If report timestamp is >6 hours old → log WARNING "Research report stale (Xh old) — sentiment may not reflect current conditions"
No behavior change; just explicit signal so you know when degraded.

---

### B. `research_agent.py`

#### B1. `_fetch_with_retry()` helper
```python
def _fetch_with_retry(fn, *args, attempts=3, base_delay=1.0, **kwargs):
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if i == attempts - 1:
                raise
            time.sleep(base_delay * (2 ** i))
```
All four fetch functions wrap their HTTP calls through this. No duplicate retry code.

#### B2. Parallel Reddit subreddit fetches
The inner loop over `REDDIT_SUBREDDITS` (6 subreddits) for each symbol switches to `ThreadPoolExecutor(max_workers=6)`. Per-symbol sequential order is preserved (Reddit rate-limit is per-IP, not per-subreddit). Estimated speedup: ~5× for the Reddit leg per symbol.

#### B3. Deduplicate `_fetch_market_news()` call
Called twice: once at line 437 (for system prompt) and again at line 530 (for macro shock). Cache in local variable `market_news` and pass to `_compute_macro_shock_score(market_news)`. Saves one full RSS round-trip (~500ms, 5 feeds).

#### B4. Reduce per-symbol log noise
Current: logs 3 lines per symbol (`Researching…`, then the full scores line, then any alert). Keep the scores line; remove the `Researching {symbol}…` preamble. Already logged via `logger.info` inside `_analyze_stock`. Reduces log volume by ~40% for research runs.

---

### C. `main.py`

#### C1. `_log_capability_summary()` — startup integration status
Called once in `main()` after the hard key checks. Logs one INFO line per integration:

```
[✓] FRED         — macro modifier active (Fed cutting, CPI falling)
[✗] POLYGON      — flash-crash single-source only
[✓] REDDIT       — public JSON, no credentials needed
[✗] X/Twitter    — disabled (no X_BEARER_TOKEN)
[✓] EMAIL        — daily report → rajesh6121991@gmail.com
[✓] TELEGRAM     — notifications active
[~] MARKET_DATA  — provider=yahoo (paper trading)
```

Reads current FRED cache to show current fed_direction in the FRED line. No new API calls.

---

### D. `config.py` — Data hygiene constants

| Constant | Old | New | Reason |
|---|---|---|---|
| `TRADE_JOURNAL_MAX_DAYS` | 90 | 30 | Faster journal reads; archive keeps full history |
| `CORRUPT_BACKUP_MAX_DAYS` | 7 | 3 | `.corrupt.json` files serve no purpose after 3 days |
| `STRATEGY_MEMORY_MAX_CHARS` | 4000 | 3500 | Slightly tighter — still above 1024-token cache threshold |
| `LOG_ROTATION_MAX_BYTES` | 10 MB | 5 MB | Faster log handler flushes; 5 backups still kept |

#### D1. Stale-file cleanup at startup (`main.py:_cleanup_stale_files()`)
Extend existing function to also prune `research_report.json` at startup if its timestamp is from a prior trading day — yesterday's symbol scores for today's watch list are misleading. The research run already rewrites the full file, so this is just a safety net for missed-run scenarios.

`daily_closed.json` and `api_budget.json` already reset by date in existing code — verified, no change needed.

---

### E. Unchanged (intentionally out of scope)

- `swing_manager.py`, `learning_agent.py`, `earnings_calendar.py` — no changes; correct behavior, not on the critical path for this refactor
- `alpaca_client.py` — paper trading only; no parallelization needed (Alpaca rate limits apply)
- `vix_regime.py` — legacy; kept for `learning_agent` compatibility; not touched

---

## Token Budget Impact

| Change | Tokens added | Tokens saved | Net |
|---|---|---|---|
| Market context block | +150/cycle | — | +150 first batch |
| Context block cached | — | −150 × (batches−1) | −150 after cache hit |
| PEAD line per stock | +10/stock | — | +10/ASK_CLAUDE stock |
| Remove `Researching…` logs | — | — | 0 (log not sent to Claude) |
| Dedup market_news | — | −5 RSS calls | Network savings only |
| Shorter journal | — | Faster I/O | Disk savings only |
| Strategy memory 4000→3500 | −500 | — | −500 tokens/call |

**Net Claude token change per cycle:** +100–200 tokens (context block) − 500 (tighter strategy_memory) = **−300 to −400 tokens net** while adding significantly more decision-relevant context.

---

## Testing

All new pure functions (`_build_market_context_block`, `_fetch_with_retry`, `_log_capability_summary`) will have `@pytest.mark.unit` tests in `tests/test_foundation.py`:
- `_build_market_context_block` with mock regime/FRED/economic/research data — assert all 6 fields present
- `_fetch_with_retry` — mock a function that fails twice then succeeds; assert result returned on 3rd attempt
- `_log_capability_summary` — mock config vars; assert log output contains expected `[✓]`/`[✗]` markers

Parallel fetch changes are integration-path only (network calls); no new unit tests for those, but existing tests must still pass.
