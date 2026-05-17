# CLAUDE.md

## Quick Start

```bash
./start.sh && tail -f logs/trading_bot.log   # start + monitor
./stop.sh                               # graceful stop
.venv/bin/python3 main.py              # foreground (dev)
.venv/bin/python -m pytest tests/ -m unit -q  # run tests
.venv/bin/python3 backtest.py          # last 30 trading days (default)
.venv/bin/python3 backtest.py --days 60
.venv/bin/python3 backtest.py --start 2026-04-01 --end 2026-04-30
```

Bot runs weekdays 9:30 AM – 3:55 PM ET. Auto-start: `./scripts/setup_launchagent.sh` (macOS) or `./scripts/setup_cron.sh` (Linux).

---

## Architecture

Multi-agent intraday trader: Alpaca paper trading + Claude decisions. APScheduler drives all jobs (ET timezone, weekdays only).

### Scheduler

| Time | Job |
|---|---|
| 8:00 AM | `earnings_calendar.refresh_earnings_calendar()` |
| 8:30 AM | `economic_calendar.refresh_economic_calendar()` |
| 8:45 AM | `agents/volatility_screener.run_volatility_screener()` — top 50 tickers → `daily_tradeable.txt` |
| 8:55 AM | `macro.regime.fetch_macro_regime()` — 9-state regime |
| 9:00 AM | `agents/watch_list.run_watch_list_agent()` — Claude picks 10–15 stocks |
| 9:15 AM | `research/agent.run_research_agent()` — sentiment + `__meta__` |
| Every 10 min (9:30–3:50) | `trading.cycle.run_trading_cycle()` |
| Every 60 sec (9:30–3:50) | `trading.monitor.run_position_monitor()` |
| 3:50 PM | `trading.swing_manager.evaluate_eod_holds()` |
| 3:55 PM | `trading.cycle.force_close_all()` |
| 4:00 PM | `macro.regime.refresh_fred_cache()` |
| 4:15 PM | `learning/agent.run_learning_agent()` |
| 4:20 PM | `notifications/daily_report.send_daily_report()` |
| Every 30 min | Heartbeat |
| 9:30 AM next day | `trading.swing_manager.run_gap_check()` |

### Key Modules

| Module | Role |
|---|---|
| `indicators/engine.py` | 10+ technicals → `composite_score` [-100,+100]. Score >15 → BUY, <-15 → CLOSE |
| `indicators/values.py` | `IndicatorValues` dataclass: all computed signals incl. `relative_volume`, `or_position` |
| `trading/pre_claude_filter.py` | **Quality router only** (5 layers): routes each symbol to SKIP / AUTO_EXECUTE / CLAUDE. No safety gates here — see Rule 23 |
| `signals/entry_gates.py` | `check_all_gates()` — **sole enforcer of all safety gates** (time window, earnings, event risk, Stoch, parabolic, herding, gap-skip, soft flags) |
| `signals/volume_guard.py` | `is_volume_weak()` — relative-volume soft gate (< `REL_VOLUME_MIN`) |
| `signals/opening_range_gate.py` | `get_or_position()` — opening range position (inside/outside/unknown) |
| `trading/pipeline.py` | Per-cycle orchestrator: fetch bars → indicators → pre-filter → Claude → execute |
| `trading/cycle.py` | `run_trading_cycle()` — cycle driver; dead-day alerting; telemetry recording |
| `trading/cycle_context.py` | `CycleSnapshot` dataclass: regime, params, research, intraday news, breadth ratio |
| `trading/executor.py` | `execute_buy()` — calls `check_all_gates()`, places orders, records fills |
| `trading/risk_manager.py` | Budget TOCTOU-safe R/W under `BUDGET_LOCK`; 75% intraday warning; gap guard |
| `trading/monitor.py` | Position monitor: SL/TP enforcement, drawdown alerts at `-DRAWDOWN_WARN_PCT` |
| `trading/swing_manager.py` | Overnight swing: 7-gate EOD qualification, tiered TP 40/30/30, trailing stop |
| `macro/regime.py` | 9-state regime (CRISIS/BULL_TREND/CHOPPY/…) from VIX/SPY/SOXX/QQQ/WTI/TNX/Gold/DXY |
| `macro/breadth_signals.py` | `get_advance_decline_ratio()` — IWM/SPY session return ratio; < 0.85 → soft flag |
| `data/market_data.py` | Provider-agnostic OHLCV (Yahoo/Alpaca/hybrid via `MARKET_DATA_PROVIDER`) |
| `research/agent.py` | RSS + Reddit + StockTwits sentiment; `__meta__` macro shock score |
| `calendars/earnings_calendar.py` | Next-earnings dates + PEAD detection (5-condition score ≥ 80) |
| `trading/journal.py` | JSON state R/W with locks: `_POSITIONS_LOCK`, `_DAILY_CLOSED_LOCK`, `_JOURNAL_LOCK` |
| `learning/agent.py` | Nightly: rolling 5-day trade analysis, weight clamping, strategy_memory versioning |
| `utils/metrics.py` | `record_cycle_metric()` + `get_daily_summary()` — JSONL cycle telemetry |
| `utils/http.py` | Shared `yahoo_get()` — Session + 429 backoff |
| `utils/io_utils.py` | `load_json` / `save_json` — atomic writes, corrupt-file recovery |
| `utils/constants.py` | `ET = ZoneInfo("America/New_York")` — single source |

### Gate Architecture (Option A)

**`pre_claude_filter`** is a quality/budget router only — it decides whether a symbol is worth calling Claude for. It enforces composite minimums, RSI ceiling, VWAP slope, macro health, and sentiment, but **no safety rules**.

**`check_all_gates()`** in `signals/entry_gates.py` is the sole enforcer of all safety gates:
- Time window, daily P&L loss limit
- Earnings imminent, event-risk window
- Stochastic hard veto (≥97) and soft flag (85–97)
- Parabolic (5-day return > threshold)
- Bot herding score
- Gap-skip
- Soft flags: relative volume weak, inside opening range, breadth weak

**Never add safety rules to `pre_claude_filter`** — they would duplicate `check_all_gates` and go stale independently.

### Position Sizing

`shares = portfolio × 0.5% / ATR_per_share`, capped at `portfolio × 5% / price`. ×0.5 in HIGH_VOL; no entries in CRISIS.

### State Files (JSON)

All JSON state files live under `data/state/`. Log files live under `logs/`.

| File | Purpose |
|---|---|
| `data/state/open_positions.json` | Active day-trade positions |
| `data/state/swing_positions.json` | Active overnight swing positions |
| `data/state/trade_journal.json` | Trade history (BUY + CLOSE); entries > 90 days archived at startup |
| `data/state/indicator_weights.json` | Per-indicator weights updated nightly |
| `data/state/trading_params.json` | Learned entry hours, RSI/VWAP thresholds (bounded by min/max multipliers) |
| `data/state/macro_regime.json` | Today's regime + health score |
| `data/state/economic_calendar.json` | High-impact events + 45-min risk windows |
| `data/state/earnings_calendar.json` | Next earnings dates + PEAD data |
| `data/state/api_budget.json` | Daily Claude token spend (TOCTOU-safe under `BUDGET_LOCK`) |
| `data/state/research_report.json` | Sentiment scores + `__meta__` |
| `data/state/daily_closed.json` | Symbols closed today (blocks re-entry) |
| `data/state/metrics.jsonl` | Per-cycle telemetry: regime, candidates, entries, closes, budget%, latency |

### Claude Integration

Model: `claude-sonnet-4-6` (`config.py`). Structured output: `{ action, confidence, stop_loss_pct, take_profit_pct, reasoning }`. Decision bounds validated: confidence 0–100, SL 0.5–10%, TP 1–15%. HTTP 400 → HOLD (no disable); 402/429 → circuit breaker. Cache `strategy_memory` block when > 3000 chars. Strategy memory gets a dated `.bak` on every write (pruned to `STRATEGY_MEMORY_BACKUP_MAX=7`).

### Key Config

| Constant | Value | Meaning |
|---|---|---|
| `MAX_POSITION_PCT` | 0.05 | 5% per position |
| `RISK_PER_TRADE_PCT` | 0.005 | 0.5% ATR-based sizing |
| `STOP_LOSS_PCT` / `TAKE_PROFIT_PCT` | 4.0 / 5.0 | Defaults (Claude can override within bounds) |
| `MAX_DAILY_LOSS_PCT` | 0.03 | 3% daily loss circuit breaker |
| `DAILY_LOSS_WARN_PCT` | 0.015 | Warn Telegram at 1.5% intraday loss |
| `COMPOSITE_MIN_NORMAL` / `COMPOSITE_MIN_HIGH_VOL` | 20 / 35 | Pre-Claude composite threshold (regime-adjusted) |
| `AUTO_EXECUTE_COMPOSITE_MIN` | 55 | Auto-execute (skip Claude) composite threshold |
| `ENTRY_START_HOUR` / `ENTRY_END_HOUR` | 10 / 15 | ET entry window |
| `BAR_TIMEFRAME_MINUTES` / `BARS_LOOKBACK` | 5 / 100 | Bar config |
| `STOCH_HARD_VETO_THRESHOLD` | 97 | Stochastic ≥ this → hard block |
| `STOCH_SOFT_FLAG_THRESHOLD` | 85 | Stochastic ≥ this → soft flag for Claude |
| `REL_VOLUME_MIN` | 0.60 | Current volume < 60% of 20d avg → soft flag |
| `BREADTH_WEAK_THRESHOLD` | 0.85 | IWM/SPY ratio < this → breadth soft flag |
| `OPENING_RANGE_MINUTES` | 60 | First N minutes define the opening range |
| `LEARNING_ROLLING_DAYS` | 5 | Days of trade history for learning agent (prevents single-day overfit) |
| `INDICATOR_WEIGHT_MAX_MULT` | 2.0 | Max multiplier vs default weight |
| `INDICATOR_WEIGHT_MIN_MULT` | 0.3 | Min multiplier vs default weight |
| `TRAILING_STOP_PCT_FALLBACK` | 0.03 | 3% trailing stop when ATR is unavailable |
| `DRAWDOWN_WARN_PCT` | 2.0 | Alert when position unrealised loss ≥ this % |
| `REGIME_ADAPTIVE_LOGIC` | True | CHOPPY blocks high-RSI entries; DEFENSIVE raises composite bar |

---

## Coding Rules

### 1. Config over hardcoding
Every threshold, file path, and flag lives in `config.py`. Never define magic numbers in source modules.

### 2. JSON file I/O — always use io_utils
All JSON reads/writes go through `utils.io_utils.load_json` / `save_json`. Never use `json.load` / `json.dump` directly on files — breaks atomic writes and corrupt-file recovery.

### 3. Timezone constant
Never define `ET = ZoneInfo("America/New_York")` locally. Always: `from utils.constants import ET`

### 4. Yahoo Finance HTTP
Never call `requests.get(yahoo_url)` directly. Always: `from utils.http import yahoo_get` — owns User-Agent header, 429 backoff (3 retries), TCP Session reuse.

### 5. External HTTP sessions
New modules making repeated HTTP calls to the same host must use a module-level `requests.Session`. For Yahoo, use `yahoo_get` instead.

### 6. Package placement — no root-level agent files
| Domain | Package |
|---|---|
| Watch-list, volatility screening | `agents/` |
| Macro data, regime | `macro/` |
| Research, sentiment | `research/` |
| Trade execution, lifecycle, journaling | `trading/` |
| Indicator computation | `indicators/` |
| Pure signal functions (gates, guards) | `signals/` |
| Utility helpers | `utils/` |
| Broker I/O, order routing | `broker/` |
| Earnings dates, PEAD detection | `calendars/` |
| Market data, OHLCV providers | `data/` |
| Telegram + email notifications | `notifications/` |

If no package fits, create one with `__init__.py` before adding the module.

### 7. No re-export shims
Never create a root-level `.py` that only re-exports from a package. Update callers and delete the shim.

### 8. DRY
Same structure in two places → extract a helper. Shared test data → `_perfect_block()` factory or fixture.

### 9. Single responsibility
Each function does one thing. Orchestrators call helpers; helpers never call back up.

### 10. Error handling at boundaries only
Catch exceptions where external data enters (HTTP, JSON reads, config). Internal functions let exceptions propagate. Never `except Exception: pass` inside pure computation.

### 11. Locks for shared state
Every read-modify-write on a shared JSON state file must hold the corresponding lock from `trading/journal.py`. Budget R/W uses `BUDGET_LOCK` in `trading/risk_manager.py`.

### 12. No AUTO-BUY shortcuts
All BUY orders must pass `check_all_gates()`. No bypass for any reason — gate-bypass caused multiple production losses.

### 13. Scheduler job defaults
Every `scheduler.add_job()` must set `max_instances=1` and `misfire_grace_time` (300s pre-market, 30s intraday).

### 14. Startup exits
Never call `sys.exit()` in `main.py`. Use `_exit_with_alert()` to fire Telegram before exit.

### 15. Signal purity
All functions in `signals/` must be pure (no Alpaca calls, no file I/O). Required for backtest sweep.

### 16. Intraday context required for Claude
`_build_market_context_block()` must include `IntradayContext` (spy_today_pct, bias, sector perf) and post-event-day flag.

### 17. Module size limits
Source files: 300 lines max. Test files: 700 lines max (split by sub-domain before adding more).

### 18. Tests
- New tests go in the domain-scoped file (see naming below), marked `@pytest.mark.unit`
- No network calls. Patch paths via `tmp_path`
- Assert on return value **and** its content — not just `assert result is not None`
- One logical assertion per test method
- **Never mutate `config.*` file paths directly** (`config.SOME_FILE = ...`). Always use `monkeypatch.setattr(config, "SOME_FILE", tmp_path / "file.json")`. Direct mutations persist across tests, write to real `data/state/` files, and produce unstaged git changes on every test run.
- **Always patch Telegram when calling production entry points.** Functions like `run_watch_list_agent()`, `run_trading_cycle()`, `run_learning_agent()` fire `telegram_notify.*` which writes sentinel files to disk. Patch the relevant `telegram_notify` symbol before calling any such function in tests.

### 19. Test file naming
Split by domain, sub-domain when > 700 lines:

| File | Domain |
|---|---|
| `tests/test_trading_modes.py` | Mode routing, SL/TP, position sizing |
| `tests/test_trading_lifecycle.py` | Startup, force-close, profit-lock, journal |
| `tests/test_trading_pipeline.py` | Pipeline, cycle context, budget escalation |
| `tests/test_trading_budget.py` | Risk manager, gap guard, fill correction, auto-execute |
| `tests/test_signals.py` | signals/ package (entry gates, volume guard, opening range) |
| `tests/test_indicators.py` | indicator_engine, IndicatorValues |
| `tests/test_foundation.py` | macro, regime, swing, research, earnings, metrics, stale-research |
| `tests/test_learning.py` | learning agent, rolling window, weight clamping, memory versioning |
| `tests/test_entry_gates.py` | check_all_gates() — all gate paths |

### 20. Naming conventions
| Kind | Style | Example |
|---|---|---|
| Constants | `UPPER_SNAKE_CASE` in config.py | `PARABOLIC_5DAY_RETURN_PCT` |
| Private helpers | leading `_` | `_is_parabolic` |
| Public functions | `snake_case`, verb-first | `compute_indicators` |

### 21. Comments
Only when the **why** is non-obvious. Never describe what the code does. Never reference task/PR numbers.

### 22. New plan features
1. Add thresholds + paths to `config.py` under `# ── Plan N ──` header
2. Write tests first
3. Keep new functions pure
4. Wire into `main.py` scheduler last

### 23. Gate architecture — never duplicate safety rules
`pre_claude_filter` is a quality/budget router only. `check_all_gates()` is the single source of truth for all safety enforcement. Adding a safety rule to `pre_claude_filter` creates a duplicate that drifts independently — add it to `check_all_gates()` instead.
