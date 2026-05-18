# ATR-Based Dynamic TP — Phase 1 (Backtest) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the flat 5% take-profit in the backtest simulator with a per-stock ATR-based TP (`TP% = ATR% × multiplier`), add a universe benchmark to the report, validate that force-close rate drops below 40% on a 60-day backtest, and confirm the optimal multiplier via a sweep.

**Architecture:** A new pure function `compute_atr_tp_pct` in `signals/atr_tp.py` does the math. `SimState` gains an `atr_tp_multiplier` field; `_open_trade` calls the function instead of using the flat `TAKE_PROFIT_PCT` constant. The runner loads the multiplier from `trading_params.json` (falling back to `ATR_TP_MULT_DEFAULT`). The report gains a universe benchmark: equal-weight buy-at-open/sell-at-close return over the same watch lists, so you can see whether the bot adds alpha over just holding the basket.

**Tech Stack:** Python 3.11+, pandas, pytest (`@pytest.mark.unit`), existing `backtest/` and `signals/` packages.

---

## File Map

| Action | File | Change |
|---|---|---|
| Modify | `config.py` | Add 5 ATR TP constants under `# ── ATR-based TP ──` |
| Create | `signals/atr_tp.py` | Pure function `compute_atr_tp_pct` |
| Modify | `backtest/simulator.py` | `SimState` + `_open_trade` |
| Modify | `backtest/runner.py` | Load multiplier, track `day_watch_map`, pass to report |
| Modify | `backtest/report.py` | `print_report` signature + `_compute_universe_benchmark` |
| Create | `tests/test_atr_tp.py` | 9 unit tests |
| Create | `backtest_sweep.py` | CLI sweep script (validates multipliers 1.0–3.0) |

---

## Task 1: Add Config Constants

**Files:**
- Modify: `config.py` (end of file, after `TRAILING_STOP_PCT_FALLBACK`)

- [ ] **Step 1: Add the 5 constants**

Open `config.py` and append at the end (after line `TRAILING_STOP_PCT_FALLBACK = 0.03`):

```python
# ── ATR-based TP (Phase 1: backtest; Phase 2: live) ──────────────────────────
ATR_TP_MULT_DEFAULT = 2.0   # TP = ATR% × this multiplier
ATR_TP_MULT_MIN     = 1.0   # learning agent lower bound
ATR_TP_MULT_MAX     = 4.0   # learning agent upper bound
ATR_TP_FLOOR_PCT    = 1.5   # TP never below 1.5% (slow/thinly-traded stocks)
ATR_TP_CEILING_PCT  = 12.0  # TP never above 12%
```

- [ ] **Step 2: Verify import works**

```bash
.venv/bin/python3 -c "import config; print(config.ATR_TP_MULT_DEFAULT)"
```
Expected output: `2.0`

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "feat: add ATR_TP_MULT_DEFAULT and bounds to config"
```

---

## Task 2: Pure Function `compute_atr_tp_pct` + Tests

**Files:**
- Create: `signals/atr_tp.py`
- Create: `tests/test_atr_tp.py`

`atr` is a dollar ATR value (e.g., `iv.atr = 1.75`). `price` is the entry price. `multiplier` is the tunable factor. The function converts dollar ATR to ATR% then multiplies.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_atr_tp.py`:

```python
"""Unit tests for signals/atr_tp.py — compute_atr_tp_pct pure function."""
import pytest
import config


@pytest.mark.unit
def test_compute_atr_tp_pct_normal():
    """Standard case: atr=1.75, price=50 → atr_pct=3.5%, mult=2.0 → tp=7.0%."""
    from signals.atr_tp import compute_atr_tp_pct
    result = compute_atr_tp_pct(atr=1.75, price=50.0, multiplier=2.0)
    assert abs(result - 7.0) < 0.001


@pytest.mark.unit
def test_compute_atr_tp_pct_floor(monkeypatch):
    """Very low ATR → clamped to ATR_TP_FLOOR_PCT."""
    monkeypatch.setattr(config, "ATR_TP_FLOOR_PCT", 1.5)
    from signals.atr_tp import compute_atr_tp_pct
    result = compute_atr_tp_pct(atr=0.05, price=100.0, multiplier=2.0)
    assert result == 1.5


@pytest.mark.unit
def test_compute_atr_tp_pct_ceiling(monkeypatch):
    """Very high ATR → clamped to ATR_TP_CEILING_PCT."""
    monkeypatch.setattr(config, "ATR_TP_CEILING_PCT", 12.0)
    from signals.atr_tp import compute_atr_tp_pct
    result = compute_atr_tp_pct(atr=10.0, price=50.0, multiplier=2.0)
    assert result == 12.0


@pytest.mark.unit
def test_compute_atr_tp_pct_zero_price():
    """Zero price must not raise ZeroDivisionError — returns floor."""
    from signals.atr_tp import compute_atr_tp_pct
    result = compute_atr_tp_pct(atr=1.0, price=0.0, multiplier=2.0)
    assert result == config.ATR_TP_FLOOR_PCT


@pytest.mark.unit
def test_compute_atr_tp_pct_higher_multiplier():
    """Multiplier 3.0 produces proportionally wider TP."""
    from signals.atr_tp import compute_atr_tp_pct
    result_2x = compute_atr_tp_pct(atr=1.0, price=100.0, multiplier=2.0)
    result_3x = compute_atr_tp_pct(atr=1.0, price=100.0, multiplier=3.0)
    assert result_3x > result_2x
    assert abs(result_3x - 3.0) < 0.001
    assert abs(result_2x - 2.0) < 0.001
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
.venv/bin/python -m pytest tests/test_atr_tp.py -v --tb=short
```
Expected: `ModuleNotFoundError: No module named 'signals.atr_tp'` (5 failures)

- [ ] **Step 3: Create `signals/atr_tp.py`**

```python
"""ATR-based take-profit calculation — pure, no I/O."""
import config


def compute_atr_tp_pct(atr: float, price: float, multiplier: float) -> float:
    """Return TP% = (ATR / price × 100) × multiplier, clamped to [floor, ceiling].

    atr      — dollar ATR (from IndicatorValues.atr)
    price    — entry price in dollars
    multiplier — tunable factor (stored in trading_params as atr_tp_multiplier)
    """
    atr_pct = (atr / price) * 100 if price > 0 else 0.0
    raw = atr_pct * multiplier
    return max(config.ATR_TP_FLOOR_PCT, min(config.ATR_TP_CEILING_PCT, raw))
```

- [ ] **Step 4: Run tests — all 5 should pass**

```bash
.venv/bin/python -m pytest tests/test_atr_tp.py -v --tb=short
```
Expected: `5 passed`

- [ ] **Step 5: Commit**

```bash
git add signals/atr_tp.py tests/test_atr_tp.py
git commit -m "feat: add compute_atr_tp_pct pure function + 5 tests"
```

---

## Task 3: SimState + `_open_trade` Changes + Tests

**Files:**
- Modify: `backtest/simulator.py` (lines 50–57 for `SimState`, lines 84–104 for `_open_trade`)
- Modify: `tests/test_atr_tp.py` (add 2 more tests)

`_open_trade` already receives `atr` as a parameter (line 86). ATR is a dollar value (same unit as `iv.atr`). We just need to call `compute_atr_tp_pct` instead of using `TAKE_PROFIT_PCT`.

- [ ] **Step 1: Write 2 failing tests — add to `tests/test_atr_tp.py`**

Append to `tests/test_atr_tp.py`:

```python
@pytest.mark.unit
def test_simulator_open_trade_uses_atr_tp():
    """Position TP reflects ATR-based calculation, not flat TAKE_PROFIT_PCT."""
    from backtest.simulator import SimState, _open_trade
    from signals.atr_tp import compute_atr_tp_pct

    state = SimState(cash=10_000.0, atr_tp_multiplier=2.0)
    # Simulate opening a trade: atr=$2.00, entry bar_open=$100.00
    # atr_pct = (2.0/100)*100 = 2.0%, tp_pct = 2.0*2 = 4.0%
    # Expected TP price = 100 * (1 + slippage) * (1 + 0.04)
    _open_trade(state, "TEST", "2026-05-01 10:00:00+00:00", 100.0, atr=2.0)

    assert "TEST" in state.positions
    pos = state.positions["TEST"]
    expected_tp_pct = compute_atr_tp_pct(2.0, pos["entry"], 2.0)
    expected_tp = round(pos["entry"] * (1 + expected_tp_pct / 100), 4)
    assert pos["tp"] == expected_tp


@pytest.mark.unit
def test_simulator_open_trade_flat_tp_not_used():
    """Position TP must NOT equal the flat TAKE_PROFIT_PCT=5% target."""
    from backtest.simulator import SimState, _open_trade

    state = SimState(cash=10_000.0, atr_tp_multiplier=2.0)
    # atr=$1.00, price≈$100 → atr_pct=1%, tp_pct=2% → TP ≠ 5%
    _open_trade(state, "TEST2", "2026-05-01 10:00:00+00:00", 100.0, atr=1.0)

    pos = state.positions["TEST2"]
    flat_tp = round(pos["entry"] * (1 + config.TAKE_PROFIT_PCT / 100), 4)
    assert pos["tp"] != flat_tp
```

- [ ] **Step 2: Run new tests to confirm they fail**

```bash
.venv/bin/python -m pytest tests/test_atr_tp.py::test_simulator_open_trade_uses_atr_tp tests/test_atr_tp.py::test_simulator_open_trade_flat_tp_not_used -v --tb=short
```
Expected: `TypeError` or `AttributeError` (SimState has no `atr_tp_multiplier`)

- [ ] **Step 3: Update `SimState` dataclass in `backtest/simulator.py`**

Locate the `SimState` dataclass (lines 50–57). Add the new field:

```python
@dataclass
class SimState:
    cash: float = STARTING_CAPITAL
    positions: dict = field(default_factory=dict)
    daily_closed: set = field(default_factory=set)
    equity_curve: list = field(default_factory=list)
    trades: list = field(default_factory=list)
    atr_tp_multiplier: float = field(default_factory=lambda: config.ATR_TP_MULT_DEFAULT)
```

- [ ] **Step 4: Add the import at top of `backtest/simulator.py`**

After the existing `import config` line, add:

```python
from signals.atr_tp import compute_atr_tp_pct
```

- [ ] **Step 5: Update `_open_trade` to use ATR-based TP**

Locate `_open_trade` (around line 84). Replace the two lines that set `sl` and `tp`:

**Before:**
```python
    sl = round(entry * (1 - STOP_LOSS_PCT / 100), 4)
    tp = round(entry * (1 + TAKE_PROFIT_PCT / 100), 4)
```

**After:**
```python
    sl = round(entry * (1 - STOP_LOSS_PCT / 100), 4)
    tp_pct = compute_atr_tp_pct(atr, entry, state.atr_tp_multiplier)
    tp = round(entry * (1 + tp_pct / 100), 4)
```

- [ ] **Step 6: Run all atr_tp tests — all 7 should pass**

```bash
.venv/bin/python -m pytest tests/test_atr_tp.py -v --tb=short
```
Expected: `7 passed`

- [ ] **Step 7: Run full test suite to check for regressions**

```bash
.venv/bin/python -m pytest tests/ -m unit -q --tb=short
```
Expected: `743 passed` (same count as before)

- [ ] **Step 8: Commit**

```bash
git add backtest/simulator.py tests/test_atr_tp.py
git commit -m "feat: SimState.atr_tp_multiplier; _open_trade uses ATR-based TP"
```

---

## Task 4: Runner Loads Multiplier + Tracks Watch Map

**Files:**
- Modify: `backtest/runner.py`

`_load_backtest_params` already loads from `trading_params.json`. We add `atr_tp_multiplier` to the base dict. We also collect the daily watch list in a `day_watch_map` dict so the report can compute the universe benchmark.

- [ ] **Step 1: Update `_load_backtest_params` in `backtest/runner.py`**

In `_load_backtest_params`, add `atr_tp_multiplier` to the `base` dict (after `"max_positions"`):

```python
        "atr_tp_multiplier":        config.ATR_TP_MULT_DEFAULT,
```

The full `base` dict after the change:
```python
    base: dict = {
        "rsi_buy_max":              config.RSI_BUY_MAX,
        "rsi_buy_max_high_vol":     config.RSI_BUY_MAX_HIGH_VOL,
        "vwap_slope_min":           config.VWAP_SLOPE_MIN,
        "stoch_soft_threshold":     config.STOCH_SOFT_FLAG_THRESHOLD,
        "composite_min_normal":     config.COMPOSITE_MIN_NORMAL,
        "composite_min_high_vol":   config.COMPOSITE_MIN_HIGH_VOL,
        "auto_execute_composite_min": config.AUTO_EXECUTE_COMPOSITE_MIN,
        "stop_loss_pct":            config.STOP_LOSS_PCT,
        "take_profit_pct":          config.TAKE_PROFIT_PCT,
        "max_position_pct":         config.MAX_POSITION_PCT,
        "risk_per_trade_pct":       config.RISK_PER_TRADE_PCT,
        "high_vol_position_mult":   config.HIGH_VOL_POSITION_MULT,
        "max_positions":            config.SWING_MAX_POSITIONS,
        "atr_tp_multiplier":        config.ATR_TP_MULT_DEFAULT,
    }
```

- [ ] **Step 2: Update `run_backtest` to pass multiplier to `SimState` and collect watch map**

Replace the `run_backtest` function body. The key changes are: (a) pass `atr_tp_multiplier` when constructing `SimState`, (b) collect `day_watch_map`, (c) pass both to `print_report`.

```python
def run_backtest(start: datetime, end: datetime) -> None:
    """Orchestrate a full backtest from start to end (inclusive)."""
    trading_days = _get_trading_days(start, end)
    if not trading_days:
        logger.warning("No trading days in range %s – %s — nothing to backtest.", start.date(), end.date())
        return

    tickers = _load_tickers()
    if not tickers:
        logger.warning("No tickers loaded — aborting backtest.")
        return

    fetch_start = trading_days[0] - timedelta(days=5)
    fetch_end = trading_days[-1] + timedelta(days=1)

    logger.info(
        "Fetching 5-min bars %s → %s for %d tickers…",
        fetch_start.date(), fetch_end.date(), len(tickers),
    )
    all_bars = _data.fetch_5min_bars(tickers, fetch_start, fetch_end)
    logger.info("  Bars received for %d symbols.", len(all_bars))

    logger.info("Fetching daily bars for watch-list selection…")
    daily_bars = _data.fetch_daily_bars_range(tickers, fetch_start, fetch_end)

    logger.info("Fetching VIX regime for each trading day…")
    regimes = _data.fetch_vix_regimes(trading_days)

    params = _load_backtest_params()
    state = _sim.SimState(atr_tp_multiplier=params.get("atr_tp_multiplier", config.ATR_TP_MULT_DEFAULT))

    day_watch_map: dict[str, list[str]] = {}

    for day in trading_days:
        date_str = day.strftime("%Y-%m-%d")
        watch = _data.select_watch_list_for_day(daily_bars, day)
        watch = [s for s in watch if s in all_bars]
        if not watch:
            logger.warning("  %s: no watch-list symbols with bars — skipping.", date_str)
            continue
        day_watch_map[date_str] = watch
        regime = regimes.get(date_str, "NORMAL")
        _sim.simulate_day(day, watch, all_bars, state, regime=regime, params=params)

    _report.print_report(state, day_watch_map=day_watch_map, daily_bars=daily_bars)
```

- [ ] **Step 3: Verify the backtest runs without error (quick smoke test)**

```bash
.venv/bin/python3 backtest.py --days 5 2>&1 | tail -15
```
Expected: shows results summary ending with `Trade log saved to:` (no exceptions)

- [ ] **Step 4: Commit**

```bash
git add backtest/runner.py
git commit -m "feat: runner passes atr_tp_multiplier to SimState, collects day_watch_map"
```

---

## Task 5: Universe Benchmark in Report + Tests

**Files:**
- Modify: `backtest/report.py`
- Modify: `tests/test_atr_tp.py` (add 2 more tests)

The universe benchmark answers: "if you had just bought equal shares of every watch-list stock at the open and sold at close each day, what would your return be?" This is the true baseline for alpha measurement.

`daily_bars` is a `dict[str, pd.DataFrame]` with daily OHLCV. Each DataFrame's index is UTC-timestamped dates. We find the bar whose date matches the trading day, then compute `(close - open) / open`.

- [ ] **Step 1: Write 2 failing tests — append to `tests/test_atr_tp.py`**

```python
@pytest.mark.unit
def test_compute_universe_benchmark_basic():
    """Two symbols, two days: benchmark equals average open→close return compounded."""
    import pandas as pd
    from zoneinfo import ZoneInfo
    from backtest.report import _compute_universe_benchmark

    UTC = ZoneInfo("UTC")
    # Day 1: both symbols up 2%
    # Day 2: both symbols up 3%
    idx1 = [pd.Timestamp("2026-05-01", tz="UTC")]
    idx2 = [pd.Timestamp("2026-05-02", tz="UTC")]

    daily_bars = {
        "AAA": pd.DataFrame({"open": [100.0], "close": [102.0]}, index=idx1 + idx2,
                            columns=["open", "close"]).reindex(
                                pd.DatetimeIndex([pd.Timestamp("2026-05-01", tz="UTC"),
                                                  pd.Timestamp("2026-05-02", tz="UTC")])),
        "BBB": pd.DataFrame({"open": [50.0, 50.0], "close": [51.0, 51.5]},
                            index=pd.DatetimeIndex([
                                pd.Timestamp("2026-05-01", tz="UTC"),
                                pd.Timestamp("2026-05-02", tz="UTC"),
                            ])),
    }
    # Rebuild AAA properly
    daily_bars["AAA"] = pd.DataFrame(
        {"open": [100.0, 103.0], "close": [102.0, 106.09]},
        index=pd.DatetimeIndex([
            pd.Timestamp("2026-05-01", tz="UTC"),
            pd.Timestamp("2026-05-02", tz="UTC"),
        ]),
    )

    day_watch_map = {
        "2026-05-01": ["AAA", "BBB"],
        "2026-05-02": ["AAA", "BBB"],
    }

    result = _compute_universe_benchmark(day_watch_map, daily_bars)
    # Day1: AAA=(102-100)/100=2%, BBB=(51-50)/50=2% → avg=2%
    # Day2: AAA=(106.09-103)/103≈3%, BBB=(51.5-50)/50=3% → avg=3%
    # Compounded: (1.02 * 1.03 - 1) * 100 ≈ 5.06%
    assert result > 4.5
    assert result < 6.0


@pytest.mark.unit
def test_compute_universe_benchmark_missing_symbol():
    """Symbol in watch map but not in daily_bars is silently skipped."""
    import pandas as pd
    from backtest.report import _compute_universe_benchmark

    daily_bars = {
        "AAA": pd.DataFrame(
            {"open": [100.0], "close": [102.0]},
            index=pd.DatetimeIndex([pd.Timestamp("2026-05-01", tz="UTC")]),
        )
    }
    day_watch_map = {"2026-05-01": ["AAA", "MISSING"]}

    result = _compute_universe_benchmark(day_watch_map, daily_bars)
    # Only AAA: (102-100)/100 = 2%
    assert abs(result - 2.0) < 0.1
```

- [ ] **Step 2: Run new tests to confirm they fail**

```bash
.venv/bin/python -m pytest tests/test_atr_tp.py::test_compute_universe_benchmark_basic tests/test_atr_tp.py::test_compute_universe_benchmark_missing_symbol -v --tb=short
```
Expected: `ImportError: cannot import name '_compute_universe_benchmark'`

- [ ] **Step 3: Add `_compute_universe_benchmark` and update `print_report` signature in `backtest/report.py`**

Add the new function and update `print_report`. Full updated `backtest/report.py`:

```python
"""Backtest reporting: console summary, CSV trade log, equity JSON."""
from dataclasses import asdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

import config
from backtest.simulator import SimState, STARTING_CAPITAL
from utils.io_utils import save_json


def _compute_universe_benchmark(
    day_watch_map: dict[str, list[str]],
    daily_bars: dict[str, pd.DataFrame],
) -> float:
    """Return total % return of equal-weight buy-at-open / sell-at-close
    over the watch list for each day. Missing symbols are skipped."""
    UTC = ZoneInfo("UTC")
    daily_returns: list[float] = []

    for date_str in sorted(day_watch_map):
        watch = day_watch_map[date_str]
        day_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC)
        next_dt = day_dt + timedelta(days=1)

        day_rets: list[float] = []
        for sym in watch:
            df = daily_bars.get(sym)
            if df is None or df.empty:
                continue
            if df.index.tz is None:
                df = df.copy()
                df.index = df.index.tz_localize("UTC")
            day_df = df[(df.index >= day_dt) & (df.index < next_dt)]
            if day_df.empty:
                continue
            open_px = float(day_df.iloc[0]["open"])
            close_px = float(day_df.iloc[-1]["close"])
            if open_px > 0:
                day_rets.append((close_px - open_px) / open_px)

        if day_rets:
            daily_returns.append(sum(day_rets) / len(day_rets))

    total = 1.0
    for r in daily_returns:
        total *= (1 + r)
    return (total - 1) * 100


def print_report(
    state: SimState,
    day_watch_map: dict | None = None,
    daily_bars: dict | None = None,
) -> None:
    trades = state.trades
    if not trades:
        print("\nNo trades executed.")
        return

    df = pd.DataFrame([asdict(t) for t in trades])
    total_pnl = df["pnl"].sum()
    n = len(df)
    wins   = (df["pnl"] > 0).sum()
    losses = (df["pnl"] < 0).sum()
    win_rate = wins / n * 100 if n else 0.0
    avg_win  = df.loc[df["pnl"] > 0, "pnl"].mean() if wins else 0.0
    avg_loss = df.loc[df["pnl"] < 0, "pnl"].mean() if losses else 0.0

    equity_vals = [e["equity"] for e in state.equity_curve]
    peak   = STARTING_CAPITAL
    max_dd = 0.0
    for eq in equity_vals:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100
        if dd > max_dd:
            max_dd = dd

    final_equity     = equity_vals[-1] if equity_vals else STARTING_CAPITAL + total_pnl
    total_return_pct = (final_equity / STARTING_CAPITAL - 1) * 100

    start_lbl = state.equity_curve[0]["ts"][:10]  if state.equity_curve else "?"
    end_lbl   = state.equity_curve[-1]["ts"][:10] if state.equity_curve else "?"

    # Universe benchmark
    bench_pct: float | None = None
    if day_watch_map and daily_bars:
        bench_pct = _compute_universe_benchmark(day_watch_map, daily_bars)

    force_close_count = (df["exit_reason"] == "FORCE_CLOSE").sum()
    force_close_pct   = force_close_count / n * 100 if n else 0.0

    print("\n" + "=" * 70)
    print(f"  BACKTEST RESULTS  —  {start_lbl} → {end_lbl}  —  $100,000 starting capital")
    print("=" * 70)
    print(f"  ATR TP multiplier   : {state.atr_tp_multiplier:.2f}×")
    print(f"  Final equity        : ${final_equity:>12,.2f}")
    print(f"  Total P&L           : ${total_pnl:>+12,.2f}   ({total_return_pct:+.2f}%)")
    if bench_pct is not None:
        alpha = total_return_pct - bench_pct
        print(f"  Universe benchmark  : {bench_pct:+.2f}%  (equal-weight watch-list)")
        print(f"  Alpha vs benchmark  : {alpha:+.2f}%")
    print(f"  Max drawdown        : {max_dd:.2f}%")
    print(f"  Total trades        : {n}")
    print(f"  Win / Loss          : {wins} / {losses}  (win rate {win_rate:.1f}%)")
    print(f"  Force-close rate    : {force_close_pct:.1f}%  ({force_close_count}/{n} trades)")
    print(f"  Avg winning trade   : ${avg_win:+.2f}")
    print(f"  Avg losing trade    : ${avg_loss:+.2f}")

    print("\n── Exit-reason breakdown ──────────────────────────────────────────")
    for reason, grp in df.groupby("exit_reason"):
        print(f"  {reason:<15} {len(grp):>3} trades   P&L ${grp['pnl'].sum():>+10,.2f}")

    swing_mask = df["exit_reason"].str.startswith("SWING")
    n_swing = swing_mask.sum()
    if n_swing > 0:
        sw = df[swing_mask]
        sw_wins = (sw["pnl"] > 0).sum()
        print(f"\n── Swing summary ({n_swing} trades, win rate {sw_wins/n_swing*100:.1f}%) ─────")
        print(f"  Total swing P&L : ${sw['pnl'].sum():>+10,.2f}")
        print(f"  Avg swing trade : ${sw['pnl'].mean():>+.2f}  |  avg hold = {(sw['pnl_pct'].abs() > 0).sum()} trades")
        for sym, grp in sw.groupby("symbol"):
            print(f"    {sym:<6} {len(grp):>2} trades  P&L ${grp['pnl'].sum():>+8,.2f}  win {(grp['pnl']>0).sum()}/{len(grp)}")

    print("\n── Daily P&L ──────────────────────────────────────────────────────")
    for date_val, grp in df.groupby("date"):
        print(f"  {date_val}   {len(grp):>2} trades   ${grp['pnl'].sum():>+9,.2f}")

    print("\n── Top 10 trades by P&L ───────────────────────────────────────────")
    cols = ["date", "symbol", "qty", "entry_price", "exit_price", "pnl", "pnl_pct", "exit_reason"]
    print(df.nlargest(10, "pnl")[cols].to_string(index=False))

    print("\n── Bottom 10 trades by P&L ────────────────────────────────────────")
    print(df.nsmallest(10, "pnl")[cols].to_string(index=False))

    print("\n── Per-symbol summary ─────────────────────────────────────────────")
    sym_summary = (
        df.groupby("symbol")
        .agg(trades=("pnl", "count"), total_pnl=("pnl", "sum"),
             win_rate=("pnl", lambda x: (x > 0).mean() * 100))
        .sort_values("total_pnl", ascending=False)
    )
    print(sym_summary.head(20).to_string())
    print("=" * 70)

    out_path = config.BASE_DIR / "backtest_trades.csv"
    df.to_csv(out_path, index=False)
    print(f"\n  Trade log saved to: {out_path}")

    equity_path = config.BASE_DIR / "backtest_equity.json"
    save_json(equity_path, state.equity_curve)
    print(f"  Equity curve saved to: {equity_path}")
```

- [ ] **Step 4: Run all 9 tests — all should pass**

```bash
.venv/bin/python -m pytest tests/test_atr_tp.py -v --tb=short
```
Expected: `9 passed`

- [ ] **Step 5: Run full test suite — no regressions**

```bash
.venv/bin/python -m pytest tests/ -m unit -q --tb=short
```
Expected: `743 passed`

- [ ] **Step 6: Commit**

```bash
git add backtest/report.py tests/test_atr_tp.py
git commit -m "feat: universe benchmark in backtest report; force-close % in summary"
```

---

## Task 6: Sweep Script

**Files:**
- Create: `backtest_sweep.py` (top-level, alongside `backtest.py`)

The sweep runs the full backtest at each of five multipliers and prints a comparison table. This validates the 2.0× default before going live. It reads the date range from CLI args (same interface as `backtest.py`).

- [ ] **Step 1: Create `backtest_sweep.py`**

```python
#!/usr/bin/env python3
"""ATR TP multiplier sweep: compare bot metrics across multiplier values.

Usage:
    .venv/bin/python3 backtest_sweep.py              # last 60 trading days
    .venv/bin/python3 backtest_sweep.py --days 90
    .venv/bin/python3 backtest_sweep.py --start 2026-03-01 --end 2026-05-16
"""
import argparse
import logging
from datetime import datetime, timedelta

import config
from backtest.data import fetch_5min_bars, fetch_daily_bars_range, fetch_vix_regimes, select_watch_list_for_day
from backtest.runner import _get_trading_days, _load_tickers, _load_backtest_params
from backtest.simulator import SimState, simulate_day
from backtest.report import _compute_universe_benchmark
from dataclasses import asdict
import pandas as pd
from utils.constants import ET

logging.basicConfig(level=logging.WARNING, format="%(message)s")


def _run_one(trading_days, tickers, all_bars, daily_bars, regimes, multiplier) -> dict:
    params = _load_backtest_params()
    params["atr_tp_multiplier"] = multiplier
    state = SimState(atr_tp_multiplier=multiplier)
    day_watch_map: dict[str, list[str]] = {}

    for day in trading_days:
        date_str = day.strftime("%Y-%m-%d")
        watch = select_watch_list_for_day(daily_bars, day)
        watch = [s for s in watch if s in all_bars]
        if not watch:
            continue
        day_watch_map[date_str] = watch
        regime = regimes.get(date_str, "NORMAL")
        simulate_day(day, watch, all_bars, state, regime=regime, params=params)

    trades = state.trades
    if not trades:
        return {"multiplier": multiplier, "trades": 0, "pnl_pct": 0.0,
                "win_rate": 0.0, "force_close_pct": 0.0, "max_dd": 0.0, "alpha": 0.0}

    df = pd.DataFrame([asdict(t) for t in trades])
    n = len(df)
    equity_vals = [e["equity"] for e in state.equity_curve]
    final_equity = equity_vals[-1] if equity_vals else 100_000.0
    pnl_pct = (final_equity / 100_000.0 - 1) * 100

    peak, max_dd = 100_000.0, 0.0
    for eq in equity_vals:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100
        if dd > max_dd:
            max_dd = dd

    fc_pct = (df["exit_reason"] == "FORCE_CLOSE").sum() / n * 100
    win_rate = (df["pnl"] > 0).sum() / n * 100

    bench = _compute_universe_benchmark(day_watch_map, daily_bars) if day_watch_map else 0.0
    alpha = pnl_pct - bench

    return {
        "multiplier": multiplier,
        "trades": n,
        "pnl_pct": round(pnl_pct, 2),
        "win_rate": round(win_rate, 1),
        "force_close_pct": round(fc_pct, 1),
        "max_dd": round(max_dd, 2),
        "bench_pct": round(bench, 2),
        "alpha": round(alpha, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--start")
    parser.add_argument("--end")
    args = parser.parse_args()

    end_dt = datetime.now(tz=ET).replace(hour=0, minute=0, second=0, microsecond=0)
    if args.end:
        end_dt = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=ET)
    start_dt = end_dt - timedelta(days=args.days * 1.5)
    if args.start:
        start_dt = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=ET)

    trading_days = _get_trading_days(start_dt, end_dt)
    if not trading_days:
        print("No trading days in range.")
        return

    tickers = _load_tickers()
    fetch_start = trading_days[0] - timedelta(days=5)
    fetch_end = trading_days[-1] + timedelta(days=1)

    print(f"Fetching data for {len(tickers)} tickers ({len(trading_days)} days)…")
    all_bars = fetch_5min_bars(tickers, fetch_start, fetch_end)
    daily_bars = fetch_daily_bars_range(tickers, fetch_start, fetch_end)
    regimes = fetch_vix_regimes(trading_days)

    multipliers = [1.0, 1.5, 2.0, 2.5, 3.0]
    results = []
    for m in multipliers:
        print(f"  Running multiplier {m}×…", end=" ", flush=True)
        r = _run_one(trading_days, tickers, all_bars, daily_bars, regimes, m)
        results.append(r)
        print(f"P&L={r['pnl_pct']:+.2f}%  FC={r['force_close_pct']:.0f}%  α={r['alpha']:+.2f}%")

    print("\n" + "=" * 75)
    print(f"  {'Mult':>4}  {'Trades':>6}  {'P&L%':>7}  {'WinRate':>7}  {'FC%':>5}  {'MaxDD':>6}  {'Bench%':>7}  {'Alpha':>6}")
    print("-" * 75)
    for r in results:
        marker = " ◄ current default" if r["multiplier"] == config.ATR_TP_MULT_DEFAULT else ""
        print(f"  {r['multiplier']:>4.1f}×  {r['trades']:>6}  {r['pnl_pct']:>+7.2f}%  {r['win_rate']:>6.1f}%  {r['force_close_pct']:>4.0f}%  {r['max_dd']:>5.2f}%  {r['bench_pct']:>+7.2f}%  {r['alpha']:>+6.2f}%{marker}")
    print("=" * 75)

    best = max(results, key=lambda x: x["alpha"])
    print(f"\n  Best alpha: {best['multiplier']}× → α={best['alpha']:+.2f}%  (P&L={best['pnl_pct']:+.2f}%  FC={best['force_close_pct']:.0f}%)")
    if best["multiplier"] != config.ATR_TP_MULT_DEFAULT:
        print(f"\n  Tip: update ATR_TP_MULT_DEFAULT in config.py to {best['multiplier']} or")
        print(f"       add \"atr_tp_multiplier\": {best['multiplier']} to data/state/trading_params.json")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify the sweep script runs (smoke test — just 5 days to keep it fast)**

```bash
.venv/bin/python3 backtest_sweep.py --days 5 2>&1 | tail -20
```
Expected: prints the comparison table ending with `Best alpha:` line (no exceptions)

- [ ] **Step 3: Commit**

```bash
git add backtest_sweep.py
git commit -m "feat: backtest_sweep.py — ATR TP multiplier comparison table"
```

---

## Task 7: Full 60-Day Validation

This task validates Phase 1 against the success criteria in the spec. No code changes — run and record results.

**Success criteria (all must pass):**
- Force-close rate < 40%
- Total P&L ≥ previous +4.95%
- Max drawdown ≤ 2%
- Universe alpha ≥ 0%

- [ ] **Step 1: Run the sweep on 60 trading days**

```bash
time .venv/bin/python3 backtest_sweep.py --days 60 2>&1 | tee sweep_results.txt
```
Expected runtime: ~10–15 minutes (5× the normal backtest)

- [ ] **Step 2: Run the standard backtest with default multiplier (2.0×) to see full report**

```bash
time .venv/bin/python3 backtest.py --days 60 2>&1 | tee backtest_60d.txt
```

- [ ] **Step 3: Check success criteria from the output**

Look for these lines in `backtest_60d.txt`:
- `Force-close rate    :` — must be `< 40%`
- `Total P&L           :` — must be `≥ +4.95%` (or higher than 30d baseline of +4.95%)
- `Max drawdown        :` — must be `≤ 2.00%`
- `Alpha vs benchmark  :` — must be `≥ 0%`

- [ ] **Step 4: If sweep shows a better multiplier, update `trading_params.json`**

If the sweep table shows (e.g.) 1.5× gives higher alpha:
```bash
# Edit data/state/trading_params.json — add the atr_tp_multiplier key:
# { ..., "atr_tp_multiplier": 1.5 }
```
Then re-run `backtest.py --days 60` to confirm.

- [ ] **Step 5: Commit results and any param update**

```bash
git add sweep_results.txt backtest_60d.txt
# If trading_params.json was updated:
git add data/state/trading_params.json
git commit -m "test: 60d backtest validation — Phase 1 ATR TP results"
```

- [ ] **Step 6: If all criteria pass, update the spec status**

Edit `docs/superpowers/specs/2026-05-17-atr-tp-design.md` first line:
```
**Status:** Phase 1 validated ✓ — proceed to Phase 2
```

```bash
git add docs/superpowers/specs/2026-05-17-atr-tp-design.md
git commit -m "docs: mark Phase 1 validated in spec"
```

---

## Self-Review Checklist

**Spec coverage:**
- ✓ `config.py` 5 new constants — Task 1
- ✓ `signals/atr_tp.py` pure function — Task 2
- ✓ `SimState.atr_tp_multiplier` — Task 3
- ✓ `_open_trade` uses ATR TP — Task 3
- ✓ Runner loads `atr_tp_multiplier` from trading_params — Task 4
- ✓ Universe benchmark in report — Task 5
- ✓ Force-close % displayed in report — Task 5
- ✓ 60d validation requirement — Task 7
- ✓ Sweep to find optimal multiplier — Task 6
- ✓ 9 unit tests — Tasks 2, 3, 5

**Test count by task:**
- Task 2: 5 tests (`test_compute_atr_tp_pct_*`)
- Task 3: 2 tests (`test_simulator_open_trade_*`)
- Task 5: 2 tests (`test_compute_universe_benchmark_*`)
- Total: 9 tests in `tests/test_atr_tp.py`

**Type consistency:**
- `compute_atr_tp_pct(atr: float, price: float, multiplier: float) -> float` — consistent across Tasks 2, 3, 4, 5, 6
- `SimState.atr_tp_multiplier: float` — consistent across Tasks 3, 4, 6
- `_compute_universe_benchmark(day_watch_map: dict, daily_bars: dict) -> float` — consistent across Tasks 5, 6
- `print_report(state, day_watch_map=None, daily_bars=None)` — backward-compatible; Tasks 4, 5

**Gate preservation:** No entry gate, pre-claude filter, or safety logic is modified in Phase 1 — only `_open_trade` TP calculation and report/runner scaffolding.
