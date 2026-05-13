# Intraday News Monitoring + Auto-Execute Pipeline

**Date:** 2026-05-13  
**Status:** Approved

---

## Goal

1. Add a lightweight intraday news refresh (every 30 min) so the bot reacts to breaking news during the trading day — both to block bad trades and to enable opportunistic entries.
2. Wire in a three-tier decision pipeline that replaces the current single-gate approach: hard-block → auto-execute → Claude. This saves 10–20% of Claude tokens while preserving Claude's judgment for every ambiguous setup.

---

## Background / Current State

- Research runs **once at 9:15 AM**. Any breaking news during the day (FDA ruling, earnings shock, macro event) is invisible until the next morning.
- `AUTO_BUY_COMPOSITE_THRESHOLD = 35` and `AUTO_BUY_MIN_SENTIMENT = -10` exist in `config.py` but are **not wired** — every stock above `composite_score > 15` still calls Claude unconditionally.
- Claude is called every 10 min for all watch-list stocks above threshold (2–3 batch calls per cycle, ~18–27 entry calls/day plus separate exit evaluations).

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

Added under a new `# ── Intraday News + Auto-Execute ──` section:

```python
INTRADAY_NEWS_REFRESH_MIN     = 30   # scheduler cadence (minutes)
INTRADAY_NEWS_SHOCK_BLOCK     = 60   # shock_score >= this → hard-block new entries & add held positions to exit eval
INTRADAY_NEWS_STALE_MIN       = 45   # cache older than this → fall back to research sentiment; disable auto-execute
AUTO_EXECUTE_COMPOSITE_MIN    = 45   # composite must exceed this for auto-execute (replaces AUTO_BUY_COMPOSITE_THRESHOLD)
AUTO_EXECUTE_SENTIMENT_MIN    = 15   # intraday sentiment_score must exceed this for auto-execute
```

`AUTO_BUY_COMPOSITE_THRESHOLD` and `AUTO_BUY_MIN_SENTIMENT` are removed (were unwired; superseded by above).

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
| Clean textbook setup (composite > 45, good news) | 1 Claude call | 0 Claude calls |
| News-hostile symbol (shock ≥ 60) | 1 Claude call (HOLD returned) | 0 Claude calls |
| Ambiguous setup | 1 Claude call | 1 Claude call (unchanged) |
| Exit on held position with news shock | Only if composite dropped | Also triggered by shock score |

Estimated token reduction: **15–25%** on active days (depends on how often composite > 45 setups occur).

---

## Files Touched

| File | Change |
|---|---|
| `intraday_news.py` | New module |
| `config.py` | 5 new constants; remove 2 stale ones |
| `trading/pipeline.py` | Three-tier gate in Phase 2; shock-exit trigger in Phase 2.5 |
| `trading/context_builder.py` | `build_stock_block()` gains optional intraday headline param |
| `main.py` | Add 30-min scheduler job for `refresh_intraday_news` |
| `tests/test_trading.py` | 8 new unit tests |
