# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the Bot

```bash
./start.sh        # Start bot in background (creates .venv if missing, logs to trading_bot.log)
./stop.sh         # Stop bot gracefully (SIGTERM → SIGKILL after 2s)
tail -f trading_bot.log   # Monitor live output
```

The bot runs weekdays only, 9:30 AM – 3:55 PM ET. Use the launchers below for auto-start/stop, or run manually.

**Auto-start/stop — macOS:**
```bash
./setup_launchagent.sh            # Install (starts 8:30 AM ET, stops 4:30 PM ET, Mon–Fri)
./setup_launchagent.sh uninstall  # Remove
```

**Auto-start/stop — Linux cloud:**
```bash
./setup_cron.sh            # Install (CRON_TZ=America/New_York, DST-aware)
./setup_cron.sh uninstall  # Remove
```

## Development Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python3 main.py   # Run directly (foreground)
```

## Testing & Backtesting

```bash
pytest tests/test_foundation.py -v -m unit   # Plan 1 + 2 unit tests (no API calls)
pytest test_agents.py -v -m unit             # Legacy unit tests
pytest test_agents.py -v -m live             # Tests requiring live Alpaca credentials

.venv/bin/python3 backtest.py          # 7-day backtest; outputs backtest_trades.csv + backtest_equity.json
```

No linter or formatter is configured.

## Architecture Overview

The bot is a multi-agent, adaptive intraday day trader using Alpaca paper trading and Claude for decisions.

### Scheduler Flow (`main.py`)

APScheduler drives all jobs on ET timezone, weekdays only:

| Time | Job |
|---|---|
| 8:00 AM | `earnings_calendar.refresh_earnings_calendar()` — fetch next earnings dates + PEAD data for watch list + tier-1 |
| 8:30 AM | `economic_calendar.refresh_economic_calendar()` — load NFP/FOMC/CPI event-risk windows |
| 8:45 AM | `volatility_screener.py` — filter top 50 most volatile tickers into `daily_tradeable.txt` |
| 8:55 AM | `macro_regime.fetch_macro_regime()` — 9-state regime classification (replaces vix_regime) |
| 9:00 AM | `watch_list_agent.py` — Claude selects 10–15 stocks |
| 9:15 AM | `research_agent.run_research_agent()` — sentiment from RSS, Reddit, StockTwits; writes `__meta__` (macro shock, sector sentiment) |
| Every 10 min (9:30–3:50 PM) | `trading_agent.run_trading_cycle()` — compute technicals + Claude decision for new entry candidates only |
| Every 60 sec (9:30–3:50 PM) | `trading_agent.run_position_monitor()` — enforce SL/TP via two-layer protection |
| 3:50 PM | `swing_manager.evaluate_eod_holds()` — promote qualifying day-trade positions to overnight swing |
| 3:55 PM | `trading_agent.force_close_all()` — cancel orders, close all non-swing open positions |
| 4:00 PM | `macro_regime.refresh_fred_cache()` — update Fed/CPI/unemployment modifiers |
| 4:15 PM | `learning_agent.py` — Claude reviews closed trades, updates `indicator_weights.json` and `trading_params.json` |
| 4:20 PM | `daily_report.send_daily_report()` — email + Telegram P&L summary |
| Every 30 min | Heartbeat — logs open position count |
| 9:30 AM (next day) | `swing_manager.run_gap_check()` — evaluate pre-market gaps on held swing positions |

### Core Components

**`indicator_engine.py`** — Computes 10+ technical indicators on 5-min bars (VWAP + bands, RSI, MACD, Bollinger Bands, EMA 9/21, ATR, Volume Ratio, Stochastic, OBV) plus 6 Plan-2 counter-bot signals (parabolic, volume_exhaustion, OBV divergence, momentum_exhaustion, squeeze_setup, stop_hunt_zone). Produces a `composite_score` in [-100, +100] weighted by `indicator_weights.json`. Score > 15 → BUY; < -15 → SELL/CLOSE.

**`trading_agent.py`** — Core per-cycle logic:
1. Fetch 100 bars of 5-min data via `market_data`
2. Compute indicators
3. Ask Claude for BUY / HOLD / CLOSE decision, confidence, and custom SL/TP levels
4. BUY → place bracket order, write to `open_positions.json`
5. CLOSE → close manually, write to `trade_journal.json`
6. Position monitor layer 1: detect Alpaca bracket order fills; layer 2: manual close if price breaches SL/TP

**`macro_regime.py`** — 9-state regime classifier (CRISIS, CRASH_RECOVERY, BULL_TREND, CHOPPY, OPPORTUNITY, DEFENSIVE, CAPITULATION, NORMAL, HIGH_VOL). Fetches 8 Yahoo signals (VIX, SPY, SOXX, QQQ, WTI, TNX, Gold, DXY), computes a health score, adds FRED modifier. Writes `macro_regime.json`.

**`market_data.py`** — Provider-agnostic data layer. Selects Yahoo / Alpaca / hybrid via `MARKET_DATA_PROVIDER` env var. Returns a tz-aware OHLCV DataFrame with a consistent schema regardless of provider.

**`research_agent.py`** — Sentiment from 5 RSS feeds (MarketWatch, CNBC, Benzinga, Yahoo, Bloomberg), 6 Reddit subreddits, and StockTwits. Claude scores each ticker. Appends `__meta__` block to `research_report.json` (macro shock score, sector sentiment, capitulation candidates).

**`swing_manager.py`** — Overnight swing lifecycle: EOD hold qualification (7 gates), morning gap check, tiered TP (40/30/30 split, trailing stop after TP2), max-days enforcement. Thread-safe via `_SWING_LOCK`.

**`earnings_calendar.py`** — Daily 8:00 AM refresh of next-earnings dates and PEAD setup detection (4-of-5 conditions: EPS beat, revenue beat, guidance not lowered, strong earnings-day reaction, 2× volume).

**`tier1_config.py`** — Static registry of 32 tier-1 symbols (AI_SEMI, HYPERSCALER, AI_OPTICS, DATA_CENTER, GEO_HEDGE) and swing qualification gates. Tier-1 status enables PEAD detection, wider SL/TP, and overnight swing holds.

**`learning_agent.py`** — Post-market: Claude analyzes `trade_journal.json`, adjusts indicator weights (max ±5/indicator/day, sum = 100, min = 0) and trading params. Persisted to JSON files.

**`alpaca_client.py`** — Thin execution wrapper around `alpaca-py`. Data methods delegate to `market_data`. All trading uses paper trading endpoint only.

### Position Sizing

Risk-based: `shares = portfolio × 0.5% / ATR_per_share`, capped at `portfolio × 5% / price`. Scaled down ×0.5 in HIGH_VOL regime; no new entries in CRISIS.

### State / Persistence (all JSON)

| File | Purpose |
|---|---|
| `open_positions.json` | Active day-trade positions with entry price, SL, TP |
| `swing_positions.json` | Active overnight swing positions with tiered TP state |
| `trade_journal.json` | Recent trade history (BUY + CLOSE) — entries > 90 days archived at startup |
| `trade_journal_archive.json` | Long-term archive auto-populated from `trade_journal.json` |
| `indicator_weights.json` | Learned per-indicator weights (updated daily by learning agent) |
| `trading_params.json` | Learned entry hours, RSI/VWAP thresholds |
| `macro_regime.json` | Today's 9-state regime + health score (replaces vix_regime.json) |
| `vix_regime.json` | Legacy — kept on disk; `learning_agent.py` reads it |
| `economic_calendar.json` | Today's high-impact events + 45-min risk windows |
| `earnings_calendar.json` | Next earnings dates + PEAD data per symbol (refreshed 8:00 AM) |
| `api_budget.json` | Daily Claude token spend tracker |
| `macro_regime.json` / `fred_cache.json` | FRED modifier (Fed rate direction, CPI trend, unemployment) |
| `research_report.json` | Sentiment scores, catalysts, `__meta__` (macro shock, sector sentiment) |
| `daily_closed.json` | Symbols closed today (prevents same-day re-entry) |
| `watch_list.txt` | Today's 10–15 selected tickers |
| `strategy_memory.md` | Freeform notes fed to Claude as context |

### Claude Integration

Model: `claude-sonnet-4-6` (set in `config.py`). All four agents call Claude; the trading agent applies `cache_control: ephemeral` to the strategy memory block only when it exceeds 3000 chars (≈ 1024 tokens minimum for caching). HTTP 400/429 errors are handled gracefully — 400 (format errors) fall back to HOLD without disabling Claude; only 402/429 trip the session circuit breaker. Trading agent expects structured JSON output: `{ action, confidence, stop_loss_pct, take_profit_pct, reasoning }`.

### Key Config (`config.py`)

- `MAX_POSITION_PCT = 0.05` (5% per position)
- `RISK_PER_TRADE_PCT = 0.005` (0.5% ATR-based sizing)
- `STOP_LOSS_PCT = 4.0`, `TAKE_PROFIT_PCT = 5.0` (defaults, overridable by Claude)
- `MAX_DAILY_LOSS_PCT = 0.03` (3% circuit breaker)
- `AUTO_BUY_COMPOSITE_THRESHOLD = 35` (auto-BUY without Claude when composite > this + indicators aligned)
- `AUTO_BUY_MIN_SENTIMENT = -10` (sentiment floor — any BUY blocked if score below this)
- `TRADE_JOURNAL_MAX_DAYS = 90` (entries older than this archived at startup)
- `CORRUPT_BACKUP_MAX_DAYS = 7` (`.corrupt.json` backups deleted at startup after this many days)
- `ENTRY_START_HOUR = 10`, `ENTRY_END_HOUR = 15` (ET)
- `BAR_TIMEFRAME_MINUTES = 5`, `BARS_LOOKBACK = 100`

All API keys loaded from `.env` (Alpaca, Anthropic, Reddit, X/Twitter).

---

## Coding Practices

These rules apply to all new code and tests in this repo.

### Configuration over hardcoding

Every numeric threshold, file path, and flag must live in `config.py`. Source modules read from `config`; they never define their own magic numbers. This makes tuning a one-file change and keeps the learning agent's parameter updates consistent.

```python
# ✓ correct
return ret5d >= config.PARABOLIC_5DAY_RETURN_PCT

# ✗ wrong — new threshold, hidden from config
return ret5d >= 15.0
```

### DRY — Don't Repeat Yourself

If the same structure appears in two places, extract a helper. Common patterns:

- **Shared test data** → static factory method (e.g. `_perfect_block()`) or pytest fixture; tests override only the fields under test.
- **Repeated file-write boilerplate** → helper method (e.g. `_write_cache(tmp_path, **overrides)`).
- **Repeated guard clauses** → extract a predicate function and import it.

### Single responsibility

Each function does one thing. Orchestrators call helpers; helpers do not call back up to orchestrators. Examples:

- `evaluate_tiered_tp_action` — pure function, no Alpaca calls.
- `monitor_swing_positions` — orchestrator only; dispatches to `evaluate_tiered_tp_action` and `_execute_full_close`.
- `_compute_macro_shock_score` — pure keyword scan; `run_research_agent` calls it.

### Error handling at boundaries only

Validate and catch exceptions where data enters the system (Yahoo/Alpaca HTTP calls, JSON file reads, user-supplied config). Internal functions trust their callers and let exceptions propagate naturally. Never catch `Exception` just to silence it inside a pure computation.

### Tests

- All new tests go in `tests/test_foundation.py`, decorated `@pytest.mark.unit`.
- Tests must not make network calls or require a running Alpaca account. Patch file paths via `tmp_path`; skip with `pytest.skip` if a network call is unavoidable.
- Use class-level helpers (`_perfect_block`, `_write_cache`) to avoid repeating payload construction across related tests.
- Assert on both the return value **and** its content (not just `assert result is not None`).
- One logical assertion per test where feasible — split into separate test methods rather than combining unrelated checks.

### Naming

| Kind | Convention | Example |
|---|---|---|
| Constants | `UPPER_SNAKE_CASE` in `config.py` | `PARABOLIC_5DAY_RETURN_PCT` |
| Module-private helpers | leading `_` | `_is_parabolic`, `_write_cache` |
| Public API functions | `snake_case`, verb-first | `compute_indicators`, `refresh_earnings_calendar` |
| Dataclass fields | `snake_case` | `composite_score`, `obv_divergence` |

### Comments

Write a comment only when the **why** is non-obvious — a hidden constraint, a spec citation, a workaround for a data quirk. Never describe what the code does (well-named identifiers already do that). Never reference the task, PR, or plan number inside source files.

```python
# ✓ explains a non-obvious invariant
trailing = total_qty - tp1 - tp2  # guaranteed > 0 by the guard above

# ✗ describes what the code does — remove it
# compute the 5-day return
ret5d = (daily_closes[-1] - daily_closes[-6]) / daily_closes[-6] * 100
```

### Adding new Plan features

Before touching source files:
1. Add all thresholds and file paths to `config.py` under a `# ── Plan N ──` section header.
2. Write the unit tests first (or alongside) — the acceptance criteria in the plan are the test cases.
3. Keep new functions pure where possible (no side effects, no Alpaca calls) so they are trivially testable.
4. Wire into `main.py` scheduler and `_catchup_missed_agents` last, after the module is verified.

### Signal purity

All signal functions in `signals/` must be pure (no Alpaca calls, no file I/O). This enables Approach C backtest tuning — the backtest runner can sweep thresholds without any side effects.

### JSON file I/O

All JSON file reads and writes must go through `utils.io_utils.load_json` / `save_json`. Never use ad-hoc `json.load` / `json.dump` directly in module bodies — this ensures atomic writes and corrupt-file recovery everywhere.

### Startup exits

Never call `sys.exit()` directly in `main.py`. Always use `_exit_with_alert()` so a Telegram notification fires before the process exits. This prevents silent startup failures when the bot auto-starts overnight.

### Module size limits

No source file may exceed 300 lines. No test file may exceed 1,000 lines. If a file is approaching the limit, extract a focused submodule before adding more code.

### Test file naming convention

Test files are domain-scoped, not monolithic:
- `tests/test_signals.py` — signals/ package unit tests
- `tests/test_trading.py` — trading/ package unit tests
- `tests/test_indicators.py` — indicator_engine.py unit tests
- `tests/test_foundation.py` — macro, regime, swing, research, earnings

Never add a new test class to `test_foundation.py` if it belongs in a more specific file.

### No AUTO-BUY shortcuts

All BUY orders must pass `check_all_gates()` before execution. No order bypasses gate checks for any reason — not for high composite scores, not for high sentiment. The gate-bypass pattern is the root cause of multiple production losses.

### Locks for all shared state

Any read-modify-write on a JSON state file shared between scheduler jobs must hold the corresponding lock from `trading/journal.py` (`_POSITIONS_LOCK`, `_DAILY_CLOSED_LOCK`, `_JOURNAL_LOCK`). Never read-modify-write shared state without a lock.

### Intraday context is required for Claude

`_build_market_context_block()` must include `IntradayContext` (spy_today_pct, bias, sector performance) and post-event-day flag. Claude cannot make informed decisions with only the daily regime snapshot.

### Scheduler job defaults

Every `scheduler.add_job()` call must specify `max_instances=1` and `misfire_grace_time`. Use `misfire_grace_time=300` for pre-market jobs (before 9:15 ET) and `misfire_grace_time=30` for intraday jobs.
