# Backtest Speedup & Package Refactor — Design Spec

**Date:** 2026-05-17  
**Status:** Approved  
**Scope:** `backtest.py` and new `backtest/` package only. Zero changes to live-bot modules.

---

## Problem

30d backtest takes ~15 minutes; 60d takes similar or longer. Root causes identified:

| Bottleneck | Impact |
|---|---|
| `_select_watch_list()` called once per trading day | 60 separate Alpaca daily-bar API calls for a 60d run |
| `_fetch_vix_regime_for_day()` called once per trading day | 120 Yahoo Finance HTTP calls for a 60d run |
| No disk caching | Every re-run re-fetches all bars even for same date range |
| Sequential symbol batch requests | 4–8 serial HTTP requests where parallel would do |
| `backtest.py` is 785 lines | Violates Rule 17 (300-line limit); hard to navigate |

---

## Goals

1. **Cold run:** 60d backtest under 3 minutes (down from ~15 min)
2. **Warm run:** 60d re-run under 10 seconds via Parquet cache
3. **Correctness:** Identical simulation output — no behavioral changes to sim logic
4. **Config SoT:** All live-bot params still read from `config.py` only
5. **Rule compliance:** All files under 300 lines (Rule 17); no safety rules in wrong layer (Rule 23)

---

## Package Layout

```
backtest/
  __init__.py      # exports run_backtest for external callers
  data.py          # all network I/O + Parquet cache (no sim logic)
  simulator.py     # pure simulation: SimState, Trade, simulate_day()
  report.py        # print_report(), CSV + equity JSON output
  runner.py        # run_backtest() orchestration
backtest.py        # CLI only: argparse → runner.run_backtest() (~40 lines)
```

---

## Module Responsibilities

### `backtest/data.py`

All network I/O. No simulation logic. Three public functions:

**`fetch_5min_bars(symbols, start, end, cache_dir) → dict[str, DataFrame]`**
- Cache key: `{start}_{end}_{sha256(sorted_symbols)[:8]}.parquet` under `data/cache/`
- Cache hit: load from Parquet (~1s)
- Cache miss: `ThreadPoolExecutor(max_workers=8)` across all `(symbol_batch × time_chunk)` pairs concurrently → save to Parquet on completion
- Batch size and chunk size remain `_FETCH_SYMBOL_BATCH=25`, `_FETCH_DAY_CHUNK=30` (unchanged)

**`fetch_daily_bars_range(symbols, start, end) → dict[str, DataFrame]`**
- Single Alpaca request for the full period (replaces per-day loop)
- Returns full DataFrames; slicing per day is done in-memory by `select_watch_list_for_day()`

**`fetch_vix_regimes(trading_days) → dict[str, str]`**
- Fetches VIX + SPY once for the whole range (2 Yahoo calls total)
- Classifies all days in one pass
- Returns `{date_str → "NORMAL" | "HIGH_VOL" | "CRISIS"}`

**`select_watch_list_for_day(daily_bars, day, n) → list[str]`**
- Pure in-memory function: slices `daily_bars` for `prior_day`, ranks by ATR%
- No network calls

### `backtest/simulator.py`

Pure simulation — no HTTP, no file I/O (consistent with Rule 15 spirit).

- `Trade` dataclass (unchanged)
- `SimState` dataclass (unchanged)
- `simulate_day(day, watch_list, all_bars, state, regime, params)` (unchanged logic)
- All helpers: `_open_trade`, `_close_trade`, `_calc_qty`, `_entry_price`, `_exit_price`, `_pos_value_at`
- Backtest-only simulation constants: `STARTING_CAPITAL`, `SLIPPAGE_PCT`, `MAX_POSITIONS`, etc.
- Live-bot params read from `config.py` (unchanged SoT)

### `backtest/report.py`

- `print_report(state)` (unchanged logic)
- Saves `backtest_trades.csv` and `backtest_equity.json`

### `backtest/runner.py`

Orchestration — no simulation logic, no formatting:

```python
def run_backtest(trading_days: list[datetime]) -> None:
    tickers    = _load_tickers()
    fetch_start, fetch_end = trading_days[0] - timedelta(days=5), trading_days[-1] + timedelta(days=1)
    all_bars   = data.fetch_5min_bars(tickers, fetch_start, fetch_end)
    daily_bars = data.fetch_daily_bars_range(tickers, fetch_start, fetch_end)
    regimes    = data.fetch_vix_regimes(trading_days)
    params     = _load_backtest_params()
    state      = SimState()
    for day in trading_days:
        watch = data.select_watch_list_for_day(daily_bars, day)
        watch = [s for s in watch if s in all_bars]
        simulator.simulate_day(day, watch, all_bars, state, regimes[day.strftime("%Y-%m-%d")], params)
    report.print_report(state)
```

### `backtest.py` (CLI entry point)

~40 lines. Argparse only. Calls `from backtest.runner import run_backtest`.
`_get_trading_days()` and `_NYSE_HOLIDAYS` stay here or move to `backtest/runner.py` — runner.py is the better home.

---

## Parquet Cache

- Location: `data/cache/` (auto-created, gitignored)
- File name: `bars_5min_{start}_{end}_{sym_hash}.parquet`
- Written atomically: write to `.tmp` then rename
- Invalidation: date range is in the key — no stale-data risk
- Format: multi-symbol Parquet with `symbol` + `timestamp` as index levels (matches Alpaca SDK output)
- `--no-cache` flag: not in this iteration — can be added as `data.fetch_5min_bars(..., use_cache=False)` later without API changes

---

## Expected Performance

| Scenario | Before | After |
|---|---|---|
| 30d cold run | ~8–15 min | ~60–90s |
| 60d cold run | ~15 min | ~2–3 min |
| 30d warm re-run | ~8–15 min | ~5–10s |
| 60d warm re-run | ~15 min | ~10–15s |

Primary gains:
- Daily-bar fix: eliminates ~59 serial API calls (60d)
- VIX fix: eliminates ~118 serial HTTP calls (60d)
- Parallel fetch: 3–4× speedup on 5-min bar fetch
- Parquet cache: makes all re-runs near-instant

---

## Testing

- No new unit tests for the pure simulation logic (it's unchanged code, existing tests cover `indicators/engine.py`)
- New unit tests in `tests/test_backtest_data.py`:
  - `test_select_watch_list_for_day_ranks_by_atr` — in-memory, no network
  - `test_fetch_vix_regimes_classifies_correctly` — mock Yahoo response, all three regime outcomes
  - `test_parquet_cache_hit_skips_fetch` — mock Alpaca client, verify it is not called on second invocation
  - `test_parquet_cache_miss_saves_file` — verify Parquet file written after cold fetch
- All marked `@pytest.mark.unit`; no network calls; use `monkeypatch` + `tmp_path`

---

## Constraints & Non-Goals

- **No live-bot changes.** `trading/`, `signals/`, `indicators/`, `config.py` untouched.
- **No behavioral changes to simulation.** Identical trade output for same inputs.
- **No new config.py constants** — all backtest-only values stay in `backtest/simulator.py`.
- **No `--no-cache` flag** in this iteration (design accommodates it cleanly later).
- **No streaming/progress bar** in this iteration (can add `tqdm` later without design changes).
