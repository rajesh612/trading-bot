# Spec: Refactor, 80% Coverage & Signal Quality Improvements

**Date:** 2026-05-06
**Goal:** Reorganize into `utils/` and `signals/` packages, reach 80% test coverage via orchestration-path mocking, implement 8 signal improvements to push profitable-day rate from ~33% toward 55–65%, and fix 5 production bugs discovered during analysis.

**Approach C compatibility:** All new code lives in `utils/` and `signals/` packages with clean interfaces. Approach C's backtest-driven threshold tuner will extend `signals/` without touching the files created here.

---

## 1. Folder Structure

```
trading-bot/
├── utils/
│   ├── __init__.py
│   ├── io_utils.py            # shared _load_json / _save_json — single implementation
│   └── market_monitor.py      # flash-crash, SPY verify, lockout — extracted from trading_agent.py
├── signals/
│   ├── __init__.py
│   ├── time_gate.py           # block entries 9:30–10:00 ET and 15:20–15:55 ET
│   ├── relative_strength.py   # rank watchlist vs SPY; only enter top-60%
│   ├── correlation_guard.py   # block if 2+ open positions in same GICS sector
│   ├── sentiment_sizer.py     # sentiment score → position size multiplier (0.5–1.25×)
│   └── mtf_confirm.py         # 15-min EMA(9) > EMA(21) required before 5-min entry
├── trading_agent.py           # slimmed: imports utils/ and signals/; owns trading cycle
├── utils/ (existing modules unchanged interface)
├── tests/
│   └── test_foundation.py     # all @pytest.mark.unit tests (existing + ~120 new)
└── docs/superpowers/specs/    # this file
```

---

## 2. Refactoring

### 2a. `utils/io_utils.py`

Extract `_load_json` and `_save_json` from `trading_agent.py:35–59`. All callers updated to import from `utils.io_utils`. Modules that currently roll their own `json.load/dump` patterns (`swing_manager.py`, `macro_regime.py`, `learning_agent.py`) are migrated to use `load_json` / `save_json` (public names, no leading underscore since it's a shared utility).

```python
def load_json(path: Path, default):
    """Atomic read with corrupt-file recovery. Returns default if file missing."""

def save_json(path: Path, data):
    """Validate JSON serialization before touching disk — raises on bad data."""
```

### 2b. `utils/market_monitor.py`

Extract ~150 lines from `trading_agent.py`:
- `_get_spy_prev_close()`
- `_verify_spy_with_polygon()`
- `_write_flash_crash_candidate()` / `_clear_flash_crash_candidate()`
- `_trigger_flash_crash_lockout()` / `_handle_flash_crash_stage2()`
- `_set_crisis_lockout()`
- `run_market_monitor()`

`trading_agent.py` imports these from `utils.market_monitor`. Net result: `trading_agent.py` drops from 1,370 → ~1,080 lines. `market_monitor.py` is independently testable.

### 2c. Slim `trading_agent.py`

After extraction, `trading_agent.py` owns exactly:
- State I/O helpers (now delegating to `utils.io_utils`)
- Entry gate logic
- Claude batch call
- `_close_position()`
- `run_position_monitor()`
- `run_trading_cycle()`
- `force_close_all()` (with bug fixes — see Section 4)

---

## 3. Signal Improvements

All signal functions are **pure** (no Alpaca calls, no file I/O) so they are trivially testable and Approach C can tune their thresholds via backtest without touching orchestration code.

### 3a. Time-of-day gate — `signals/time_gate.py`

```python
def is_entry_allowed(now: datetime) -> bool:
    """Returns False during first 30 min (9:30–10:00 ET) and last 35 min (15:20–15:55 ET)."""
```

Wired into `_check_entry_gates()` in `trading_agent.py`. Thresholds stored in `config.py`:
- `ENTRY_GATE_OPEN_MINUTES = 30` (skip first N minutes after open)
- `ENTRY_GATE_CLOSE_MINUTES = 35` (skip last N minutes before close)

Rationale: first 30 min has highest spread + gap fill noise; last 35 min has forced EOD behavior and thin liquidity.

### 3b. Relative strength filter — `signals/relative_strength.py`

```python
def rs_rank(symbol: str, spy_ret5d: float, symbol_ret5d: float) -> float:
    """Returns RS ratio: symbol_ret5d / spy_ret5d. > 1.0 means outperforming SPY."""

def passes_rs_filter(rs_ratio: float, threshold: float = config.RS_MIN_RATIO) -> bool:
    """True if symbol is outperforming SPY by at least the threshold."""
```

Config: `RS_MIN_RATIO = 0.0` (symbol must not underperform SPY by more than this). Wired into `_check_entry_gates()`. Uses the 5-day return already fetched by `macro_regime.py` — no extra API call.

### 3c. Correlation guard — `signals/correlation_guard.py`

```python
def sector_of(symbol: str) -> str | None:
    """Returns GICS sector string from tier1_config or a static fallback map."""

def passes_correlation_guard(symbol: str, open_positions: dict) -> bool:
    """Returns False if 2+ open positions share this symbol's sector."""
```

Config: `MAX_SAME_SECTOR_POSITIONS = 2`. Sector map is a static dict in `tier1_config.py` (already has category groupings: AI_SEMI, HYPERSCALER, etc.) extended to cover all 126 tickers.

### 3d. Sentiment-weighted position sizer — `signals/sentiment_sizer.py`

```python
def sentiment_size_multiplier(sentiment_score: int) -> float:
    """
    Maps research sentiment score to a position size multiplier:
      score >= +20  → 1.25×
      score >= +10  → 1.10×
      score >= -10  → 1.00×
      score >= -20  → 0.75×
      score <  -20  → 0.50×
    """
```

Wired into `compute_position_size_for_mode()` — multiply final shares by this value before capping. Thresholds in `config.py` as `SENTIMENT_SIZE_TIERS: list[tuple[int, float]]`.

### 3e. Multi-timeframe confirmation — `signals/mtf_confirm.py`

```python
def passes_mtf_filter(bars_15min: pd.DataFrame) -> bool:
    """True if 15-min EMA(9) > EMA(21) for long entries (trend confirmation)."""
```

`trading_agent.py` fetches 15-min bars (40 bars lookback) only when a BUY signal is pending — lazy fetch, not on every cycle. Config: `MTF_BARS_LOOKBACK = 40`, `MTF_TIMEFRAME_MINUTES = 15`.

### 3f. Profit-lock trailing stop

Not a separate module — added directly to `run_position_monitor()` in `trading_agent.py`:

```
If unrealised PnL >= PROFIT_LOCK_TRIGGER_PCT (2.0%):
    new_stop = max(current_stop, entry_price)  # move stop to at least breakeven
    update open_positions.json with new_stop
```

Config: `PROFIT_LOCK_TRIGGER_PCT = 2.0`. The position monitor already runs every 60s — this adds one comparison per position per cycle.

### 3g. Progressive loss reduction

Added to `compute_position_size_for_mode()`:

```
loss_count = count of CLOSE entries in today's journal with pnl_pct < 0
size_multiplier = max(0.4, 1.0 - 0.2 * loss_count)
```

Config: `PROGRESSIVE_LOSS_STEP = 0.2`, `PROGRESSIVE_LOSS_FLOOR = 0.4`. Resets next trading day automatically (journal is day-scoped).

### 3h. Per-regime win-rate context for learning agent

`learning_agent.py` computes and passes to Claude:

```python
regime_winrate = {regime: wins/total for regime in closed_trades grouped by regime_at_entry}
```

Added to the Claude prompt in `run_learning_agent()`. The learning agent already groups by regime (`_aggregate_by_regime`) but doesn't compute a win rate — just win count vs. loss count. This adds the ratio and passes it back to Claude for weight adjustment context.

---

## 4. Bug Fixes

### Bug 1 — No Telegram alert before `sys.exit(1)` for missing critical keys

**File:** `main.py`

Add `_exit_with_alert(msg: str, code: int = 1)` helper:
```python
def _exit_with_alert(msg: str, code: int = 1) -> None:
    logger.error(msg)
    telegram_notify._send(f"🚨 Bot failed to start: {msg}")
    sys.exit(code)
```

Replace bare `sys.exit(1)` calls at lines 764–768 with `_exit_with_alert(...)`.

**CLAUDE.md rule added:** "Never call `sys.exit()` directly in `main.py` — always use `_exit_with_alert()` so Telegram fires first."

**Test added:** `TestStartupExitAlerts` — mock `telegram_notify._send` and `sys.exit`, verify Telegram is called before exit for each missing-key scenario.

### Bug 2 — Silent position close failure at EOD (no retry, no alert)

**File:** `trading_agent.py` → `force_close_all()`

Current `close_position()` swallows exceptions. Fix:
1. After `cancel_all_orders()`, wait 1s for Alpaca to process cancellations.
2. `close_position()` failure → retry once after 2s.
3. If retry fails → fall back to `close_all_positions()` (nuclear).
4. After sweep, re-fetch `get_positions()` and Telegram-alert any symbol still open.

### Bug 3 — Phantom journal entries for unfilled entry orders

**File:** `trading_agent.py` → `force_close_all()`

Before journaling a CLOSE, verify the symbol exists in `alpaca.get_positions()`. If not present (entry never filled), skip journal, log a warning, remove from `open_positions.json`. Config flag: `EOD_VERIFY_FILLS = True`.

### Bug 4 — EOD close reason has no trade classification

**File:** `trading_agent.py` → `force_close_all()`

Replace hardcoded `"end_of_day_force_close"` with:
```python
def _eod_close_reason(pnl_pct: float) -> str:
    if pnl_pct > 0.5:  return "eod_force_profitable"
    if pnl_pct < -0.5: return "eod_force_loss"
    return "eod_force_flat"
```

Config: `EOD_PROFITABLE_THRESHOLD = 0.5`, `EOD_LOSS_THRESHOLD = -0.5`.

### Bug 5 — `_log_capability_summary` not tested for startup exit ordering

**File:** `tests/test_foundation.py`

New `TestStartupExitAlerts` class verifies that for every hard-exit path in `main()`, Telegram is called with a message containing the key name before `sys.exit` fires.

---

## 5. Test Coverage Plan

**Target:** 80% overall (from current 50%)

**Coverage breakdown by file:**

| File | Current | Target | Strategy |
|---|---|---|---|
| `trading_agent.py` | 28% | 75% | Mock AlpacaClient + file I/O; test entry gates, close logic, position monitor, force_close |
| `research_agent.py` | 27% | 75% | Mock HTTP fetches; test sentiment computation, dedup, Claude response parsing |
| `macro_regime.py` | 45% | 80% | Mock Yahoo fetch; test all scorer functions + classify_regime paths |
| `swing_manager.py` | 43% | 80% | Mock AlpacaClient; test evaluate_eod_holds, gap_action, tiered TP paths |
| `learning_agent.py` | 48% | 80% | Mock Claude response; test weight clamping, regime aggregation, param updates |
| `earnings_calendar.py` | 45% | 80% | Mock HTTP; test PEAD detection, refresh, cache helpers |
| `economic_calendar.py` | 46% | 80% | Mock requests; test event parsing, risk window computation |
| `daily_report.py` | 44% | 75% | Mock email/Telegram; test report body construction |
| `utils/io_utils.py` | — | 95% | Pure I/O — test corrupt file recovery, validation, atomic write |
| `utils/market_monitor.py` | — | 90% | Mock requests + file I/O; test all flash-crash state transitions |
| `signals/*.py` | — | 95% | Pure functions — all branches covered with parametrize |

**New test classes (~120 tests):**
- `TestTimeGate` — 8 tests (market open, first 30min, last 35min, exact boundaries, weekend, holiday)
- `TestRelativeStrength` — 6 tests (outperform, underperform, equal, zero SPY return edge case)
- `TestCorrelationGuard` — 8 tests (0/1/2/3 same-sector positions, unknown sector, empty positions)
- `TestSentimentSizer` — 10 tests (boundary values for all 5 tiers, extreme values)
- `TestMtfConfirm` — 6 tests (EMA9>EMA21, EMA9<EMA21, equal, insufficient data)
- `TestProfitLockTrailingStop` — 6 tests (below trigger, at trigger, above trigger, stop update)
- `TestProgressiveLossReduction` — 8 tests (0/1/2/3 losses, floor enforcement)
- `TestRegimeWinRate` — 5 tests (pure function, empty trades, single regime, multi-regime)
- `TestForceCloseAll` — 12 tests (happy path, retry on failure, phantom entry skip, close_reason classification)
- `TestStartupExitAlerts` — 4 tests (missing Alpaca key, missing Anthropic key, ordering verified)
- `TestIoUtils` — 10 tests (corrupt recovery, missing file default, validation error, atomic write)
- `TestMarketMonitor` — 15 tests (flash crash stages, SPY verification, lockout set/clear, recovery)
- `TestResearchAgentOrchestration` — 10 tests (mock HTTP, sentiment parsing, dedup)
- `TestSwingManagerOrchestration` — 8 tests (eod_holds, gap_check, tiered TP execution)
- `TestLearningAgentOrchestration` — 8 tests (Claude response parsing, weight update, param update)

---

## 6. CLAUDE.md Additions

Three new coding rules:
1. `"Never call sys.exit() directly in main.py — always use _exit_with_alert() so Telegram fires first."`
2. `"All signal functions in signals/ must be pure (no Alpaca calls, no file I/O) — this enables Approach C backtest tuning."`
3. `"JSON file I/O must go through utils.io_utils.load_json / save_json — never roll ad-hoc json.load/dump in module bodies."`

---

## 7. Approach C Compatibility

Every design decision here prepares for Approach C (backtest-driven threshold tuning):

| Element | How C extends it |
|---|---|
| `signals/*.py` — pure functions with config thresholds | C adds `signals/backtest_tuner.py` that sweeps thresholds against historical data |
| `utils/io_utils.py` | C's backtest runner reads/writes result files through the same interface |
| `close_reason` classification | C uses `eod_force_profitable/loss/flat` to measure signal quality per trade |
| `per-regime win-rate` in learning agent | C uses this to validate threshold changes before going live |
| No hardcoded values in `signals/` | All thresholds in `config.py` — C can override them per-run without code changes |
