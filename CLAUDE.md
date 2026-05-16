# CLAUDE.md

## Quick Start

```bash
./start.sh && tail -f logs/trading_bot.log   # start + monitor
./stop.sh                               # graceful stop
.venv/bin/python3 main.py              # foreground (dev)
.venv/bin/python -m pytest tests/ -m unit -q  # run tests
.venv/bin/python3 backtest.py          # 7-day backtest
```

Bot runs weekdays 9:30 AM – 3:55 PM ET. Auto-start: `./setup_launchagent.sh` (macOS) or `./setup_cron.sh` (Linux).

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
| Every 10 min (9:30–3:50) | `trading_agent.run_trading_cycle()` |
| Every 60 sec (9:30–3:50) | `trading_agent.run_position_monitor()` |
| 3:50 PM | `trading.swing_manager.evaluate_eod_holds()` |
| 3:55 PM | `trading_agent.force_close_all()` |
| 4:00 PM | `macro.regime.refresh_fred_cache()` |
| 4:15 PM | `learning/agent.run_learning_agent()` |
| 4:20 PM | `daily_report.send_daily_report()` |
| Every 30 min | Heartbeat |
| 9:30 AM next day | `trading.swing_manager.run_gap_check()` |

### Key Modules

| Module | Role |
|---|---|
| `indicators/engine.py` | 10+ technicals → `composite_score` [-100,+100]. Score >15 → BUY, <-15 → CLOSE |
| `trading/pipeline.py` | Per-cycle orchestrator: fetch bars → indicators → Claude → execute |
| `macro/regime.py` | 9-state regime (CRISIS/BULL_TREND/CHOPPY/…) from VIX/SPY/SOXX/QQQ/WTI/TNX/Gold/DXY |
| `data/market_data.py` | Provider-agnostic OHLCV (Yahoo/Alpaca/hybrid via `MARKET_DATA_PROVIDER`) |
| `research/agent.py` | RSS + Reddit + StockTwits sentiment; `__meta__` macro shock score |
| `trading/swing_manager.py` | Overnight swing: 7-gate EOD qualification, tiered TP 40/30/30, trailing stop |
| `calendars/earnings_calendar.py` | Next-earnings dates + PEAD detection (5-condition score ≥ 80) |
| `trading/journal.py` | JSON state R/W with locks: `_POSITIONS_LOCK`, `_DAILY_CLOSED_LOCK`, `_JOURNAL_LOCK` |
| `utils/http.py` | Shared `yahoo_get()` — Session + 429 backoff |
| `utils/io_utils.py` | `load_json` / `save_json` — atomic writes, corrupt-file recovery |
| `utils/constants.py` | `ET = ZoneInfo("America/New_York")` — single source |

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
| `data/state/trading_params.json` | Learned entry hours, RSI/VWAP thresholds |
| `data/state/macro_regime.json` | Today's regime + health score |
| `data/state/economic_calendar.json` | High-impact events + 45-min risk windows |
| `data/state/earnings_calendar.json` | Next earnings dates + PEAD data |
| `data/state/api_budget.json` | Daily Claude token spend |
| `data/state/research_report.json` | Sentiment scores + `__meta__` |
| `data/state/daily_closed.json` | Symbols closed today (blocks re-entry) |

### Claude Integration

Model: `claude-sonnet-4-6` (`config.py`). Structured output: `{ action, confidence, stop_loss_pct, take_profit_pct, reasoning }`. HTTP 400 → HOLD (no disable); 402/429 → circuit breaker. Cache `strategy_memory` block when > 3000 chars.

### Key Config

| Constant | Value | Meaning |
|---|---|---|
| `MAX_POSITION_PCT` | 0.05 | 5% per position |
| `RISK_PER_TRADE_PCT` | 0.005 | 0.5% ATR-based sizing |
| `STOP_LOSS_PCT` / `TAKE_PROFIT_PCT` | 4.0 / 5.0 | Defaults (Claude can override) |
| `MAX_DAILY_LOSS_PCT` | 0.03 | 3% daily loss circuit breaker |
| `AUTO_BUY_COMPOSITE_THRESHOLD` | 35 | Auto-BUY threshold (composite score) |
| `ENTRY_START_HOUR` / `ENTRY_END_HOUR` | 10 / 15 | ET entry window |
| `BAR_TIMEFRAME_MINUTES` / `BARS_LOOKBACK` | 5 / 100 | Bar config |

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
Every read-modify-write on a shared JSON state file must hold the corresponding lock from `trading/journal.py`.

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

### 19. Test file naming
Split by domain, sub-domain when > 700 lines:

| File | Domain |
|---|---|
| `tests/test_trading_modes.py` | Mode routing, SL/TP, position sizing |
| `tests/test_trading_lifecycle.py` | Startup, force-close, profit-lock, journal |
| `tests/test_trading_pipeline.py` | Pipeline, cycle context, budget escalation |
| `tests/test_trading_budget.py` | Risk manager, gap guard, fill correction, auto-execute |
| `tests/test_signals.py` | signals/ package |
| `tests/test_indicators.py` | indicator_engine |
| `tests/test_foundation.py` | macro, regime, swing, research, earnings |

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
