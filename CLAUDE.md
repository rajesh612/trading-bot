# CLAUDE.md

## Quick Start

```bash
./start.sh && tail -f logs/trading_bot.log   # start + monitor
./stop.sh                               # graceful stop
.venv/bin/python3 main.py              # foreground (dev)
.venv/bin/python -m pytest tests/ -m unit -q  # run tests
# Backtesting: verify with current config
.venv/bin/python3 backtest.py 2>&1 | tee /tmp/bt_30d.txt          # last 30 trading days
.venv/bin/python3 backtest.py --days 60 2>&1 | tee /tmp/bt_60d.txt
.venv/bin/python3 backtest.py --start 2026-04-01 --end 2026-04-30 2>&1 | tee /tmp/bt_custom.txt
# Tuning: sweep params → apply winner → re-run backtest.py to confirm
.venv/bin/python scripts/param_sweep.py                            # 30d sweep (28 combos, ~7 min)
.venv/bin/python scripts/param_sweep.py --days 60 --top 15        # 60d sweep, show top 15
```

Bot runs weekdays 9:30 AM – 3:55 PM ET. Auto-start: `./scripts/setup_launchagent.sh` (macOS) or `./scripts/setup_cron.sh` (Linux).

---

## Architecture

Multi-agent intraday trader: Alpaca paper trading + Claude decisions. APScheduler drives all jobs (ET timezone, weekdays only).

### Scheduler

| Time | Job |
|---|---|
| 8:00 AM | `calendars.earnings.refresh_earnings_calendar()` |
| 8:20 AM | `macro.economic_calendar.refresh_economic_calendar()` |
| 8:45 AM | `agents.volatility_screener.run_volatility_screener()` — top 50 tickers → `daily_tradeable.txt` |
| 8:55 AM | `macro.regime.fetch_macro_regime()` — 9-state regime |
| 9:00 AM | `agents.watch_list.run_watch_list_agent()` — Claude picks 10–15 stocks |
| 9:15 AM | `research.agent.run_research_agent()` — sentiment + `__meta__` |
| 9:30 AM | `trading.swing_manager.run_gap_check()` — pre-market gap evaluation on swing positions |
| Every 10 min (9:30–3:50) | `trading.cycle.run_trading_cycle()` |
| Every 60 sec (9:30–3:50) | `trading.monitor.run_position_monitor()` |
| Every 5 min (9:30–3:50) | `utils.market_monitor.run_market_monitor()` — flash-crash detection via SPY |
| Every 30 min (9:05–2:35) | `research.intraday.refresh_intraday_news()` — intraday news refresh |
| 12:00 PM | `macro.regime.fetch_macro_regime()` — midday regime refresh (keeps regime fresh through 3:55 PM) |
| 12:00 PM | Midday check — Telegram alert if zero BUY trades executed today |
| 3:50 PM | `trading.swing_manager.evaluate_eod_holds()` |
| 3:55 PM | `trading.closer.force_close_all()` |
| 4:00 PM | `macro.regime.refresh_fred_cache()` |
| 4:15 PM | `learning.agent.run_learning_agent()` |
| 4:20 PM | `notifications.daily_report.send_daily_report()` |
| Every 30 min | Heartbeat |

### Key Modules

| Module | Role |
|---|---|
| `indicators/engine.py` | 10+ technicals → `composite_score` [-100,+100]. Score >15 → BUY, <-15 → CLOSE |
| `indicators/values.py` | `IndicatorValues` dataclass: all computed signals incl. `relative_volume`, `or_position` |
| `indicators/precompute.py` | `precompute_indicators_for_day()` — vectorized EWM/RSI/MACD precompute; eliminates per-bar recomputation (15× speedup for backtests) |
| `backtest/simulator.py` | `simulate_day()` — pure simulation engine; accepts optional `precomp_cache` to skip recomputation across param-sweep iterations (8× sweep speedup) |
| `scripts/param_sweep.py` | Parameter sweep harness — fetches data once, iterates combos in-process via `unittest.mock.patch`, outputs ranked `sweep_results.csv`. Edit `SWEEP_AXES` to define the search space; `MAX_COMBOS` caps total runs. |
| `signals/atr_tp.py` | `compute_atr_tp_pct()` — ATR-based take-profit: `ATR% × atr_tp_multiplier`, clamped to floor/ceiling |
| `trading/pre_claude_filter.py` | **Quality router + correlation pre-check** (5 layers + Layer 5.5): routes each symbol to SKIP / AUTO_EXECUTE / CLAUDE. Runs correlation hard-gate before Claude call to save API budget. No other safety gates here — see Rule 23 |
| `signals/entry_gates.py` | `check_all_gates()` — **sole enforcer of all safety gates** (time window, earnings, event risk, Stoch, parabolic, herding, gap-skip, stop-hunt zone [exempt in BULL_TREND/OPPORTUNITY], soft flags, soft-flag overload) |
| `signals/volume_guard.py` | `is_volume_weak()` — relative-volume soft gate (< `REL_VOLUME_MIN`) |
| `signals/opening_range_gate.py` | `get_or_position()` — opening range position (inside/outside/unknown) |
| `trading/pipeline.py` | Per-cycle orchestrator: fetch bars → indicators → pre-filter → Claude → execute |
| `trading/cycle.py` | `run_trading_cycle()` — cycle driver; dead-day alerting; telemetry recording |
| `trading/cycle_context.py` | `CycleSnapshot` dataclass: regime, params, research, intraday news, breadth ratio |
| `trading/executor.py` | `execute_buy()` — calls `check_all_gates()`, places orders, records fills |
| `trading/risk_manager.py` | Budget TOCTOU-safe R/W under `BUDGET_LOCK`; 75% intraday warning; gap guard |
| `trading/monitor.py` | Position monitor: SL/TP enforcement, drawdown alerts at `-DRAWDOWN_WARN_PCT`; batch-fetches real sector ETF intraday % change for T2 Gate 11; midday profit-floor exit (NORMAL, ≥1% P&L after 2 PM held 2h+) |
| `trading/swing_manager.py` | Overnight swing: EOD qualification via `get_swing_params(regime)` (profit-lock bypass + min P&L gate), tiered TP 40/30/30, trailing stop |
| `macro/regime.py` | 9-state regime (CRISIS/BULL_TREND/CHOPPY/…) from VIX/SPY/SOXX/QQQ/WTI/TNX/Gold/DXY |
| `macro/breadth_signals.py` | `get_advance_decline_ratio()` — IWM/SPY session return ratio; < 0.85 → soft flag |
| `data/market_data.py` | Provider-agnostic OHLCV (Yahoo/Alpaca/hybrid via `MARKET_DATA_PROVIDER`) |
| `research/agent.py` | RSS + Reddit + StockTwits sentiment; `__meta__` macro shock score |
| `calendars/earnings.py` | Next-earnings dates + PEAD detection (5-condition score ≥ 80) |
| `macro/economic_calendar.py` | `refresh_economic_calendar()` — loads FOMC/NFP/CPI event-risk windows |
| `trading/closer.py` | `force_close_all()` — cancels orders and closes all day-trade positions at 3:55 PM |
| `research/intraday.py` | `refresh_intraday_news()` — intraday headline refresh for active watch list |
| `trading/journal.py` | JSON state R/W with locks: `_POSITIONS_LOCK`, `_DAILY_CLOSED_LOCK`, `_JOURNAL_LOCK` |
| `learning/agent.py` | Nightly: rolling 5-day trade analysis, weight clamping + re-normalisation, ATR-TP multiplier tuning, strategy_memory versioning; caps trade payload to most-recent `LEARNING_MAX_TRADES_FOR_CLAUDE` trades |
| `utils/metrics.py` | `record_cycle_metric()` + `get_daily_summary()` — JSONL cycle telemetry |
| `utils/regime_params.py` | **SoT for all regime × mode dispatch** — `EntryParams`, `SwingParams`, `T2Params`, `TPSLParams`, `DayTradeParams`, `FilterParams`, `GateParams`, `AutoExecuteParams` frozen dataclasses; lookup functions for each. All per-regime × tier thresholds live here. No caller should hold inline `if regime ==` or `if tier ==` dispatch chains. |
| `utils/http.py` | Shared `yahoo_get()` — Session + 429 backoff (3 retries) |
| `utils/io_utils.py` | `load_json` / `save_json` — atomic writes, corrupt-file recovery |
| `utils/catchup.py` | `catchup_missed_agents()` — runs stale pre-market agents on startup if bot missed their cron window (threshold: 8:00 AM ET) |
| `utils/constants.py` | `ET = ZoneInfo("America/New_York")` — single source |
| `utils/ticker_quality.py` | `compute_ticker_quality()` — 4-tier quality scorer (LOW/MEDIUM/HIGH/CONVICTION); `derive_mode()` — trading mode derivation |
| `utils/data_health.py` | `DataHealthTracker` singleton — per-source retry/staleness state; `StalenessGuard` frozen dataclass — composite penalty, `can_open`, T2/auto-execute flags; `get_staleness_guard()` — sole factory; `should_retry_proactive()` — age-based retry without prior failure |
| `macro/fred_cache.py` | `refresh_fred_cache()` — fetches 13-series FRED data; `FRED_SCHEMA_KEYS` — frozenset of all keys this module writes (schema contract) |
| `trading/context_builder.py` | `_build_market_context_block()` — assembles Claude prompt context; `FRED_KEYS_CONSUMED` — frozenset of 9 FRED keys read here (fed_direction, cpi, unemployment, yield_curve_inverted, yield_curve_spread, credit_spread_bps, credit_spread_risk_off, claims_spike; tested against `FRED_SCHEMA_KEYS`) |

### Gate Architecture (Option A)

**`pre_claude_filter`** is a quality/budget router — it decides whether a symbol is worth calling Claude for. It enforces composite minimums, RSI ceiling, VWAP slope, macro health, and sentiment. It also runs one pre-check (Layer 5.5): **correlation hard-gate** — if `GateParams.correlation_hard_gate=True` for the regime/tier, it checks sector concentration before calling Claude. No other safety rules here — see Rule 23.

**`check_all_gates()`** in `signals/entry_gates.py` is the sole enforcer of all safety gates:
- Time window, daily P&L loss limit
- Earnings imminent, event-risk window
- Stochastic hard veto (≥97) and soft flag (85–97)
- Parabolic (5-day return > threshold)
- Bot herding score
- Gap-skip
- Stop-hunt zone (round number / 5d hi/lo proximity) — **exempt in BULL_TREND/OPPORTUNITY** where near-high is a buy signal, not a trap (via `GateParams.stop_hunt_veto`)
- Soft flags: relative volume weak, inside opening range, breadth weak
- **Soft-flag overload gate**: ≥ `SOFT_FLAG_OVERLOAD_THRESHOLD` (4) simultaneous soft flags → hard block

**Never add safety rules to `pre_claude_filter`** beyond the correlation pre-check — they would duplicate `check_all_gates` and go stale independently.

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
| `data/state/trading_params.json` | Learned entry hours, RSI/VWAP thresholds, `atr_tp_multiplier` (bounded by min/max multipliers) |
| `data/state/macro_regime.json` | Today's regime + health score |
| `data/state/economic_calendar.json` | High-impact events + 45-min risk windows |
| `data/state/earnings_calendar.json` | Next earnings dates + PEAD data |
| `data/state/api_budget.json` | Daily Claude token spend (TOCTOU-safe under `BUDGET_LOCK`) |
| `data/state/research_report.json` | Sentiment scores + `__meta__` |
| `data/state/daily_closed.json` | Symbols closed today (blocks re-entry) |
| `data/state/metrics.jsonl` | Per-cycle telemetry: regime, candidates, entries, closes, budget%, latency |

### Claude Integration

Model: `claude-sonnet-4-6` (`config.py`). Structured output: `{ action, confidence, stop_loss_pct, take_profit_pct, reasoning }`. Decision bounds validated: confidence 0–100, SL 0.5–10%, TP 1–15%. HTTP 400 → HOLD (no disable); 402/429 → circuit breaker. Both system blocks (`stable_system_text` and `volatile_text`) carry `cache_control: ephemeral` for Anthropic prompt caching. Cache `strategy_memory` block when > 3000 chars. Strategy memory gets a dated `.bak` on every write (pruned to `STRATEGY_MEMORY_BACKUP_MAX=7`). Stable context cache is invalidated on regime change (noon refresh) in addition to TTL expiry.

### Key Config

| Constant | Value | Meaning |
|---|---|---|
| `MAX_POSITION_PCT` | 0.05 | 5% per position |
| `MAX_POSITION_USD` | 10_000 | Hard dollar cap per position regardless of portfolio size (0 = disabled) |
| `RISK_PER_TRADE_PCT` | 0.005 | 0.5% ATR-based sizing |
| `STOP_LOSS_PCT` / `TAKE_PROFIT_PCT` | 3.0 / 5.0 | Defaults (Claude can override within bounds) |
| `MAX_DAILY_LOSS_PCT` | 0.03 | 3% daily loss circuit breaker |
| `DAILY_LOSS_WARN_PCT` | 0.02 | Warn Telegram at 2% intraday loss |
| `COMPOSITE_MIN_NORMAL` / `COMPOSITE_MIN_HIGH_VOL` | 50 / 55 | Pre-Claude composite threshold (regime-adjusted) |
| `AUTO_EXECUTE_COMPOSITE_MIN` | 70 | Reference for AE confidence delta computation; actual AE threshold = `max(regime_floor, tier_floor) + ae.composite_offset` |
| `ENTRY_START_HOUR` / `ENTRY_END_HOUR` | 10 / 15 | ET entry window (ENTRY_END_HOUR is the absolute market close; per-regime hard cutoff is `EntryParams.hard_close_hour` via `get_entry_params()`) |
| `ENTRY_HARD_CLOSE_HOUR` | 13 | Default hard cutoff hour (NORMAL regime); CHOPPY/DEFENSIVE=12, BULL_TREND/OPPORTUNITY=14. Overrides `ENTRY_END_HOUR` earlier in the day. Per-tier overrides live in `DayTradeParams` table in `utils/regime_params.py`. |
| `ENTRY_LATE_COMPOSITE_MIN` | 68 | Reference value only — live gate reads `DayTradeParams.late_composite` per regime × tier. NORMAL/MEDIUM: composite ≥ 68 required from 10 AM onward. |
| `BAR_TIMEFRAME_MINUTES` / `BARS_LOOKBACK` | 5 / 100 | Bar config |
| `STOCH_HARD_VETO_THRESHOLD` | 97 | Stochastic ≥ this → hard block |
| `STOCH_SOFT_FLAG_THRESHOLD` | 85 | Stochastic ≥ this → soft flag for Claude |
| `REL_VOLUME_MIN` | 0.60 | Current volume < 60% of 20d avg → soft flag |
| `BREADTH_WEAK_THRESHOLD` | 0.85 | IWM/SPY ratio < this → breadth soft flag |
| `SOFT_FLAG_OVERLOAD_THRESHOLD` | 4 | ≥ this many simultaneous soft flags → hard block (holistically poor setup) |
| `OPENING_RANGE_MINUTES` | 60 | First N minutes define the opening range |
| `LEARNING_ROLLING_DAYS` | 5 | Days of trade history for learning agent (prevents single-day overfit) |
| `LEARNING_MAX_TRADES_FOR_CLAUDE` | 25 | Max trades sent to learning agent Claude call (most-recent tail; rest used for free deterministic pre-compute) |
| `INDICATOR_WEIGHT_MAX_MULT` | 2.0 | Max multiplier vs default weight |
| `INDICATOR_WEIGHT_MIN_MULT` | 0.3 | Min multiplier vs default weight |
| `TRAILING_STOP_PCT_FALLBACK` | 0.03 | 3% trailing stop when ATR is unavailable |
| `DRAWDOWN_WARN_PCT` | 2.0 | Alert when position unrealised loss ≥ this % |
| `REGIME_ADAPTIVE_LOGIC` | True | CHOPPY blocks high-RSI entries; DEFENSIVE raises composite bar |
| `ATR_TP_MULT_DEFAULT` | 2.0 | Default ATR-TP multiplier (TP = ATR% × multiplier) |
| `ATR_TP_MULT_MIN` / `ATR_TP_MULT_MAX` | 1.0 / 4.0 | Learning agent bounds for ATR-TP multiplier |
| `ATR_TP_FLOOR_PCT` / `ATR_TP_CEILING_PCT` | 1.5 / 12.0 | Hard floor/ceiling for computed ATR-TP % |
| `PROFIT_LOCK_SWING_PCT` | 2.5 | Positions with unrealised P&L ≥ this % qualify for swing in any non-CRISIS regime (per-regime override in `SwingParams.profit_lock_pct`) |
| `SWING_PROMOTE_MIN_PNL_PCT` | 0.5 | Standard EOD swing promotion requires at least this % gain (per-regime override in `SwingParams.min_pnl_pct`; 0.3 BULL_TREND, 1.0 CRASH_RECOVERY) |
| `MIDDAY_PROFIT_FLOOR_PCT` | 1.0 | NORMAL regime: lock profits ≥ this % after `MIDDAY_EXIT_HOUR` if held ≥ `MIDDAY_EXIT_MIN_HOLD_H` hours |
| `MIDDAY_EXIT_HOUR` / `MIDDAY_EXIT_MAX_HOUR` | 14 / 15 | 2–3 PM ET window for midday profit-floor exit (force-close handles positions after 3 PM) |
| `CIRCUIT_BREAKER_FC_LOOKBACK` | 5 | Look at last N force-closes; if all are losses → circuit fires |
| `CIRCUIT_BREAKER_COMPOSITE_BOOST` | 10 | Raise composite floor by this when force-close losing streak detected |
| `AUTO_EXECUTE_CONFIDENCE_TIER_ADJ` | `{CONVICTION:+5, LOW:-10, …}` | Per-tier delta added to AE confidence after base computation |
| `AUTO_EXECUTE_CONFIDENCE_LOW_CAP` | 75 | LOW tier AE confidence never exceeds this |
| `AUTO_EXECUTE_CONFIDENCE_MODE_ADJ` | `{PEAD:+5, CATALYST:+3, …}` | Per-mode delta added to AE confidence (PEAD drift and CATALYST event edges) |
| `DATA_STALE_THRESHOLD_MINUTES` | `{"regime": 420, "research": 360, …}` | Per-source staleness threshold; exceeded → adds `DATA_COMPOSITE_PENALTY` to composite floor |
| `DATA_REGIME_CANOPEN_MAX_MINUTES` | 480 | If regime data age exceeds this, `guard.can_open=False` — blocks ALL new entries until refresh |

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
- **Always patch Telegram when calling production entry points.** Functions like `run_watch_list_agent()`, `run_trading_cycle()`, `run_learning_agent()` fire `telegram_notify.*` which writes sentinel files to disk. Patch the relevant `telegram_notify` symbol before calling any such function in tests.

#### Patching config — always use monkeypatch

**Never mutate `config.*` directly.** Direct assignment (`config.X = value`) persists for the entire test session — it is not rolled back between tests, which causes ordering-dependent failures and may write to real `data/state/` files.

Use `monkeypatch.setattr` for every config override, including file paths, API keys, thresholds, and boolean flags:

```python
# ✅ Correct — rolled back automatically after the test
def test_something(self, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "open_positions.json")
    monkeypatch.setattr(config, "ALPACA_API_KEY", "test-key")
    monkeypatch.setattr(config, "MAX_DAILY_LOSS_PCT", 0.01)

# ❌ Wrong — persists across the entire test session
def test_something(self, tmp_path):
    config.OPEN_POSITIONS_FILE = tmp_path / "open_positions.json"
    config.ALPACA_API_KEY = "test-key"
```

If a helper method (`_make_client`, `_setup`, etc.) sets config values, pass `monkeypatch` into it so it can use `setattr` — don't use a manual `try/finally` restore, which is fragile and misses exceptions before the restore runs.

The only exception: reading config constants as inputs to derive test fixture values (e.g. `threshold = config.STOCH_HARD_VETO_THRESHOLD + 1`) is fine — that is not a mutation.

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
| `tests/test_pipeline.py` | `cycle_context`, `pre_claude_filter`, `budget_guard`, pipeline tiers |
| `tests/test_market.py` | `api_budget`, Telegram, daily report, `io_utils`, market monitor |
| `tests/test_core.py` | `main.py` crash-loop, `alpaca_client`, `market_monitor` |
| `tests/test_data_health.py` | `DataHealthTracker`, `StalenessGuard`, `get_staleness_guard()`, `should_retry_proactive()` |
| `tests/test_config_coherence.py` | Config threshold coherence, Rule 23 gate-architecture lint, guard-ordering guard, FRED schema contract |
| `tests/test_trading_cycle.py` | `run_trading_cycle()` dead-day alert content and cycle-level behaviour |

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

### 24. Regime dispatch — always use utils/regime_params.py
Never write inline `if regime == "CHOPPY": ...` dispatch chains in source modules. All regime × mode threshold dispatch belongs in `utils/regime_params.py`. Callers use `get_entry_params(regime)`, `get_swing_params(regime)`, or `get_t2_params(regime)`. Adding a new regime variant = update the table in `regime_params.py` only.

### 25. Data staleness — always use get_staleness_guard()
`get_staleness_guard()` in `utils/data_health.py` is the sole factory for `StalenessGuard`.
No module evaluates staleness thresholds, computes composite penalties, or decides T2/auto-execute
eligibility outside `utils/data_health.py`. Adding a new staleness consequence = update
`config.DATA_*` sets and `get_staleness_guard()` only.

### 26. Guard injection — once per scheduler entry point
`StalenessGuard` is built exactly once per scheduler entry point (cycle, monitor tick, EOD)
before any downstream stage. Never build mid-stage or share guards across scheduler boundaries.
`CycleSnapshot.staleness_guard` carries it through the cycle; monitor and swing_manager build
their own independently.

### 27. Data resilience constants — all in config.py
All retry schedules, staleness thresholds, composite penalties, and disable sets live under
`# ── Data Resilience ──` in `config.py`. No magic numbers in `data_health.py`, agent files,
or job functions.

### 28. Ticker quality — always use compute_ticker_quality()
`compute_ticker_quality()` in `utils/ticker_quality.py` is the sole factory for `TickerQuality`.
No inline tier logic, quality scoring, or unlock evaluation outside this module.
Adding a new signal component = update the scoring table in `utils/ticker_quality.py` only.

### 29. Regime × tier dispatch — always use utils/regime_params.py
`FilterParams`, `GateParams`, `AutoExecuteParams` follow the frozen-dataclass + table-function
pattern of `EntryParams/SwingParams/T2Params`. No inline `if tier ==` or `if regime ==` dispatch
chains outside `utils/regime_params.py`. Adding a new tier variant = update the table only.

### 30. TP/SL computation — always use signals/tp_scaler.compute_tp_sl()
All TP/SL assignments in executor, monitor, and swing_manager go through `compute_tp_sl()`.
No direct `config.TAKE_PROFIT_PCT` / `config.STOP_LOSS_PCT` assignments at order-placement time.

### 31. DayTradeParams — always use get_day_trade_params()
`DayTradeParams` (hard_close_hour, late_entry_hour, late_composite, vwap_slope_min) per regime × tier
live in `utils/regime_params.py`. No inline hard_close or late-entry thresholds in entry_gates.py or
backtest/simulator.py. Adding a new timing variant = update the `_day_trade_table()` only.

### 32. Parameter optimisation — use scripts/param_sweep.py
When tuning entry thresholds (composite floors, timing gates, SL/TP ceilings), run
`scripts/param_sweep.py` rather than editing config and re-running `backtest.py` manually.
Edit `SWEEP_AXES` in the script, run once — results rank all combos by total P&L in a CSV.
Overrides are applied via `unittest.mock.patch`; no production code is modified during the sweep.

### 33. Telegram notifications — always use `_once.should_send()` for recurring alerts
Every `notify_*` function that can fire from a loop or monitor (position monitor, trading cycle,
data health) **must** call `_once.should_send(scope, key)` before `_send()`:

```python
def notify_my_alert(symbol: str, ...) -> None:
    if not _once.should_send("my_alert", symbol):
        return
    _send(...)
```

`scope` = short slug for the alert type; `key` = discriminator (`symbol`, `sector_etf`, or `""` for global).
Functions that fire exactly once per event (buy, close, EOD summary, startup) are exempt — add their
name to `_NOTIFY_DEDUP_EXEMPT` in `tests/test_config_coherence.py` with a justification comment.
`test_recurring_notify_functions_use_once_guard` enforces this structurally via AST inspection.

**Return-value contract for `correct_bracket_after_fill`:**
- `None` → poll timed out (fire timeout notification)
- `{}` → fill confirmed, slippage within threshold (no notification — normal case)
- `{stop_loss_price, take_profit_price, fill_price}` → correction applied
