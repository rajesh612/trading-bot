# Backtest Speedup & Package Refactor — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor `backtest.py` into a `backtest/` package that eliminates per-day API calls, adds Parquet caching, and parallelises fetches — cutting 30d from ~15 min to under 2 min with zero changes to live-bot modules.

**Architecture:** Extract `backtest.py` into four focused modules (`data`, `simulator`, `report`, `runner`) plus a thin CLI wrapper. The data layer fixes the two root-cause bottlenecks (per-day daily-bar fetch loop, per-day VIX loop) and adds Parquet cache + `ThreadPoolExecutor` for 5-min bar fetch. Simulation logic is moved unchanged — no behavioral differences.

**Tech Stack:** Python 3.11+, pandas, pyarrow (Parquet), `concurrent.futures.ThreadPoolExecutor`, Alpaca SDK, `utils.http.yahoo_get`

---

## Pre-flight: Look-ahead & live-code safety notes

Before touching any code, confirm these invariants are preserved throughout:

- **Look-ahead bias (5-min bars):** `simulate_day` feeds `df_sym[df_sym.index <= ts].tail(LOOKBACK_BARS)` at each bar. The Parquet cache stores raw bars — slicing logic is unchanged.
- **Look-ahead bias (daily bars):** `select_watch_list_for_day` slices to `prior_day` (one day before `day`). Identical to current `_select_watch_list` which uses `prior_day` as the upper bound.
- **Look-ahead bias (VIX):** `_classify_vix_day` uses strict `t < day_ts` — data seen only before market open on `day`. Unchanged.
- **Live-bot isolation:** `backtest.py` is never imported by any live-bot module. Only `tests/test_foundation.py:572` imports it, and that test is updated in Task 8.
- **Config SoT:** All live-bot params continue to read from `config.py`. No constants are redefined outside `backtest/simulator.py` (sim-only constants) and `config.py`.

---

## File Map

```
Create: backtest/__init__.py
Create: backtest/simulator.py        # SimState, Trade, simulate_day + helpers (pure, no I/O)
Create: backtest/report.py           # print_report, CSV/equity JSON output
Create: backtest/data.py             # fetch + Parquet cache + VIX (all network I/O)
Create: backtest/runner.py           # run_backtest, _load_backtest_params, _get_trading_days
Create: tests/test_backtest_data.py  # unit tests for backtest/data.py
Modify: backtest.py                  # shrink to ~40-line CLI wrapper
Modify: tests/test_foundation.py     # update _load_backtest_params import path (line 572)
```

---

## Task 1: Create `backtest/` package + extract `backtest/simulator.py`

**Files:**
- Create: `backtest/__init__.py`
- Create: `backtest/simulator.py`

This is a pure extraction — no logic changes. All sim-only constants, `SimState`, `Trade`, and all helper functions move here. Live-bot params still read from `config.py`.

- [ ] **Step 1: Create `backtest/__init__.py`**

Keep it empty — avoids a circular import where `runner.py` imports from `backtest.data` etc. while `__init__.py` is still loading.

```python
# backtest package — import from submodules directly
```

- [ ] **Step 2: Create `backtest/simulator.py`**

```python
"""
Pure simulation engine: SimState, Trade, and simulate_day.
No network I/O, no file I/O — safe to call from tests without side effects.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

import config
from indicators.engine import compute_indicators
from utils.constants import ET

logger = logging.getLogger(__name__)

# ── Backtest-only simulation constants (no live-bot equivalent) ──────────────
STARTING_CAPITAL = 100_000.0
SLIPPAGE_PCT     = 0.0005
MAX_POSITIONS    = 5
WATCH_LIST_SIZE  = 25
MARKET_OPEN_H    = 9
MARKET_OPEN_M    = 30
FORCE_CLOSE_H    = 15
FORCE_CLOSE_M    = 55
SWING_EVAL_H     = 15
SWING_EVAL_M     = 50

SWING_ENABLED       = True
SWING_COMPOSITE_MIN = 50.0
SWING_SL_PCT        = 7.0
SWING_TP_PCT        = 10.0

# ── Live-bot params — single source of truth is config.py ────────────────────
STOP_LOSS_PCT    = config.STOP_LOSS_PCT
TAKE_PROFIT_PCT  = config.TAKE_PROFIT_PCT
MIN_COMPOSITE    = config.COMPOSITE_MIN_NORMAL
MAX_DAILY_LOSS   = config.MAX_DAILY_LOSS_PCT
RSI_BUY_MAX      = config.RSI_BUY_MAX
RSI_BUY_MAX_HV   = config.RSI_BUY_MAX_HIGH_VOL
VWAP_SLOPE_MIN   = config.VWAP_SLOPE_MIN
ENTRY_START_H    = config.ENTRY_START_HOUR
ENTRY_END_H      = config.ENTRY_END_HOUR
HIGH_VOL_MULT    = config.HIGH_VOL_POSITION_MULT
MAX_POSITION_PCT = config.MAX_POSITION_PCT
RISK_PER_TRADE   = config.RISK_PER_TRADE_PCT
LOOKBACK_BARS    = config.BARS_LOOKBACK
SWING_MAX_POSITIONS = config.SWING_MAX_POSITIONS
SWING_MAX_DAYS   = config.SWING_MAX_DAYS


@dataclass
class Trade:
    symbol: str
    date: str
    entry_ts: str
    exit_ts: str
    entry_price: float
    exit_price: float
    qty: float
    exit_reason: str
    pnl: float
    pnl_pct: float


@dataclass
class SimState:
    cash: float = STARTING_CAPITAL
    positions: dict = field(default_factory=dict)
    daily_closed: set = field(default_factory=set)
    equity_curve: list = field(default_factory=list)
    trades: list = field(default_factory=list)


def _calc_qty(portfolio_value: float, price: float, atr: float) -> float:
    if price <= 0:
        return 0.0
    risk_dollars = portfolio_value * RISK_PER_TRADE
    sl_dollars = price * STOP_LOSS_PCT / 100
    if sl_dollars <= 0:
        sl_dollars = atr if atr > 0 else price * 0.02
    qty_risk = risk_dollars / sl_dollars
    qty_cap = (portfolio_value * MAX_POSITION_PCT) / price
    return max(1.0, round(min(qty_risk, qty_cap)))


def _entry_price(bar_open: float) -> float:
    return round(bar_open * (1 + SLIPPAGE_PCT), 4)


def _exit_price(price: float) -> float:
    return round(price * (1 - SLIPPAGE_PCT), 4)


def _pos_value_at(state: SimState, day_bars: dict, ts) -> float:
    total = 0.0
    for sym, p in state.positions.items():
        df = day_bars.get(sym)
        if df is None or df.empty:
            total += p["qty"] * p["entry"]
            continue
        available = df[df.index <= ts]
        price = float(available["close"].iloc[-1]) if not available.empty else p["entry"]
        total += p["qty"] * price
    return total


def _open_trade(
    state: SimState, symbol: str, ts: str, bar_open: float,
    atr: float, pos_mult: float = 1.0, composite: float = 0.0,
) -> None:
    portfolio_value = state.cash + sum(
        pos["qty"] * pos["entry"] for pos in state.positions.values()
    )
    entry = _entry_price(bar_open)
    qty = max(1.0, round(_calc_qty(portfolio_value, entry, atr) * pos_mult))
    cost = entry * qty
    if cost > state.cash:
        qty = max(1.0, round(state.cash / entry))
        cost = entry * qty
    if qty < 1 or cost > state.cash:
        return
    sl = round(entry * (1 - STOP_LOSS_PCT / 100), 4)
    tp = round(entry * (1 + TAKE_PROFIT_PCT / 100), 4)
    state.cash -= cost
    state.positions[symbol] = {
        "qty": qty, "entry": entry, "sl": sl, "tp": tp,
        "entry_ts": ts, "composite": composite,
    }
    logger.debug(f"    OPEN  {symbol:5s} x{qty:.0f} @ ${entry:.2f}  SL=${sl:.2f} TP=${tp:.2f}")


def _close_trade(
    state: SimState, symbol: str, exit_ts: str,
    exit_raw: float, reason: str, date_str: str,
) -> None:
    pos = state.positions.pop(symbol)
    ep = _exit_price(exit_raw)
    proceeds = ep * pos["qty"]
    state.cash += proceeds
    pnl = proceeds - pos["entry"] * pos["qty"]
    pnl_pct = (ep / pos["entry"] - 1) * 100
    state.trades.append(Trade(
        symbol=symbol, date=date_str, entry_ts=pos["entry_ts"], exit_ts=exit_ts,
        entry_price=pos["entry"], exit_price=ep, qty=pos["qty"],
        exit_reason=reason, pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 2),
    ))
    state.daily_closed.add(symbol)
    logger.debug(f"    CLOSE {symbol:5s} @ ${ep:.2f}  PnL=${pnl:+.2f} [{reason}]")


def simulate_day(
    day: datetime,
    watch_list: list[str],
    all_bars: dict[str, pd.DataFrame],
    state: SimState,
    regime: str = "NORMAL",
    params: dict | None = None,
) -> None:
    p = params or {}
    date_str = day.strftime("%Y-%m-%d")
    rsi_max  = p.get("rsi_buy_max_high_vol", RSI_BUY_MAX_HV) if regime in ("HIGH_VOL", "CRISIS") else p.get("rsi_buy_max", RSI_BUY_MAX)
    pos_mult = p.get("high_vol_position_mult", HIGH_VOL_MULT) if regime in ("HIGH_VOL", "CRISIS") else 1.0
    logger.info(f"\n=== {date_str} === regime={regime} rsi_max={rsi_max} pos_mult={pos_mult}")
    state.daily_closed.clear()

    mkt_open      = day.replace(hour=MARKET_OPEN_H, minute=MARKET_OPEN_M, second=0, microsecond=0)
    force_ts      = day.replace(hour=FORCE_CLOSE_H, minute=FORCE_CLOSE_M, second=0, microsecond=0)
    swing_eval_ts = day.replace(hour=SWING_EVAL_H,  minute=SWING_EVAL_M,  second=0, microsecond=0)
    entry_start   = day.replace(hour=ENTRY_START_H, minute=0, second=0, microsecond=0)
    entry_end     = day.replace(hour=ENTRY_END_H,   minute=0, second=0, microsecond=0)
    day_start_equity = state.cash
    halted = False
    swing_evaluated = False

    if regime == "CRISIS":
        logger.info("  CRISIS regime — skipping all new entries today.")
        halted = True

    all_ts: set[datetime] = set()
    day_bars: dict[str, pd.DataFrame] = {}
    for sym in watch_list:
        df = all_bars.get(sym)
        if df is None or df.empty:
            continue
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        df_day = df[
            (df.index >= mkt_open.astimezone(ZoneInfo("UTC"))) &
            (df.index <  (day + timedelta(days=1)).astimezone(ZoneInfo("UTC")))
        ]
        if df_day.empty:
            continue
        day_bars[sym] = df_day
        all_ts.update(df_day.index.tolist())

    sorted_ts = sorted(all_ts)
    if not sorted_ts:
        logger.info("  No bars for this day — skipping.")
        return

    pending_signals: dict[str, dict] = {}

    for i, ts in enumerate(sorted_ts):
        ts_et = ts.astimezone(ET)
        is_force = ts_et >= force_ts

        if SWING_ENABLED and not swing_evaluated and ts_et >= swing_eval_ts:
            swing_evaluated = True
            n_swings = sum(1 for p in state.positions.values() if p.get("is_swing"))
            for sym2 in list(state.positions.keys()):
                pos2 = state.positions[sym2]
                if pos2.get("is_swing"):
                    continue
                df2 = day_bars.get(sym2)
                if df2 is None or df2.empty:
                    continue
                avail2 = df2[df2.index <= ts]
                eod_price = float(avail2["close"].iloc[-1]) if not avail2.empty else pos2["entry"]
                pnl_pct = (eod_price - pos2["entry"]) / pos2["entry"] * 100
                if (
                    regime == "NORMAL"
                    and pnl_pct > 0
                    and pos2.get("composite", 0) >= SWING_COMPOSITE_MIN
                    and n_swings < SWING_MAX_POSITIONS
                ):
                    pos2["is_swing"] = True
                    pos2["swing_days_remaining"] = SWING_MAX_DAYS
                    pos2["sl"] = round(pos2["entry"] * (1 - SWING_SL_PCT / 100), 4)
                    pos2["tp"] = round(pos2["entry"] * (1 + SWING_TP_PCT / 100), 4)
                    n_swings += 1
                    logger.info(f"    SWING PROMOTE {sym2}: P&L={pnl_pct:+.2f}%")

        for sym in list(state.positions.keys()):
            df_sym = day_bars.get(sym)
            if df_sym is None or ts not in df_sym.index:
                continue
            bar = df_sym.loc[ts]
            pos = state.positions[sym]
            reason = None
            exit_price = None
            is_sw = pos.get("is_swing", False)

            if bar["high"] >= pos["tp"]:
                reason = "SWING_TP" if is_sw else "TP"
                exit_price = pos["tp"]
            elif bar["low"] <= pos["sl"]:
                reason = "SWING_SL" if is_sw else "SL"
                exit_price = pos["sl"]
            elif is_force and not is_sw:
                reason, exit_price = "FORCE_CLOSE", bar["close"]

            if reason:
                _close_trade(state, sym, str(ts), exit_price, reason, date_str)

        if is_force:
            continue

        if not halted:
            current_equity = state.cash + _pos_value_at(state, day_bars, ts)
            daily_loss_pct = (current_equity - day_start_equity) / day_start_equity
            if daily_loss_pct <= -MAX_DAILY_LOSS:
                logger.info(f"    [CIRCUIT BREAKER] daily loss {daily_loss_pct*100:.2f}% — halting {date_str}")
                halted = True
                pending_signals.clear()

        in_entry_window = entry_start <= ts_et < entry_end
        if pending_signals and i > 0 and not halted and in_entry_window:
            n_open = len(state.positions)
            for sym, sig in list(pending_signals.items()):
                if n_open >= MAX_POSITIONS:
                    break
                if sym in state.positions or sym in state.daily_closed:
                    continue
                df_sym = day_bars.get(sym)
                if df_sym is None or ts not in df_sym.index:
                    continue
                bar_open = float(df_sym.loc[ts, "open"])
                _open_trade(state, sym, str(ts), bar_open, sig["atr"], pos_mult, sig["score"])
                n_open += 1
            pending_signals.clear()

        for sym in watch_list:
            if sym in state.positions or sym in state.daily_closed:
                continue
            df_sym = all_bars.get(sym)
            if df_sym is None or df_sym.empty:
                continue
            if df_sym.index.tz is None:
                df_sym.index = df_sym.index.tz_localize("UTC")
            df_window = df_sym[df_sym.index <= ts].tail(LOOKBACK_BARS)
            if len(df_window) < 30:
                continue

            iv = compute_indicators(sym, df_window, as_of=ts_et)
            if iv is None:
                continue

            vwap_slope_min = p.get("vwap_slope_min", VWAP_SLOPE_MIN)
            if (
                iv.composite_score > MIN_COMPOSITE
                and iv.signal == "BUY"
                and iv.rsi <= rsi_max
                and iv.vwap_slope >= vwap_slope_min
            ):
                pending_signals[sym] = {"score": iv.composite_score, "atr": iv.atr}

        equity = state.cash + _pos_value_at(state, day_bars, ts)
        state.equity_curve.append({"ts": str(ts), "equity": round(equity, 2)})

    for sym in list(state.positions.keys()):
        pos = state.positions[sym]
        df_sym = day_bars.get(sym)
        last_price = (
            float(df_sym["close"].iloc[-1]) if df_sym is not None and not df_sym.empty
            else pos["entry"]
        )
        if pos.get("is_swing"):
            pos["swing_days_remaining"] -= 1
            pnl_pct = (last_price / pos["entry"] - 1) * 100
            if pos["swing_days_remaining"] <= 0:
                _close_trade(state, sym, str(sorted_ts[-1]) + "_EOD", last_price, "SWING_MAXDAYS", date_str)
            else:
                logger.info(f"  SWING CARRY {sym}: {pos['swing_days_remaining']}d left | P&L={pnl_pct:+.2f}%")
        else:
            _close_trade(state, sym, str(sorted_ts[-1]) + "_EOD", last_price, "EOD", date_str)

    portfolio_value = state.cash + sum(
        pos["qty"] * pos["entry"] for pos in state.positions.values()
    )
    logger.info(f"  End of day equity ≈ ${state.cash:,.2f} cash  (portfolio ~${portfolio_value:,.2f})")
```

- [ ] **Step 3: Verify `backtest/simulator.py` imports cleanly**

```bash
cd /path/to/trading-bot
.venv/bin/python -c "from backtest.simulator import SimState, Trade, simulate_day; print('OK')"
```

Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add backtest/__init__.py backtest/simulator.py
git commit -m "refactor: extract backtest/simulator.py — pure sim engine, no I/O"
```

---

## Task 2: Create `backtest/report.py`

**Files:**
- Create: `backtest/report.py`

Pure extraction from `backtest.py`. No logic changes.

- [ ] **Step 1: Create `backtest/report.py`**

```python
"""Backtest reporting: console summary, CSV trade log, equity JSON."""
from dataclasses import asdict

import pandas as pd

import config
from backtest.simulator import SimState, STARTING_CAPITAL
from utils.io_utils import save_json


def print_report(state: SimState) -> None:
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

    print("\n" + "=" * 70)
    print(f"  BACKTEST RESULTS  —  {start_lbl} → {end_lbl}  —  $100,000 starting capital")
    print("=" * 70)
    print(f"  Final equity        : ${final_equity:>12,.2f}")
    print(f"  Total P&L           : ${total_pnl:>+12,.2f}   ({total_return_pct:+.2f}%)")
    print(f"  Max drawdown        : {max_dd:.2f}%")
    print(f"  Total trades        : {n}")
    print(f"  Win / Loss          : {wins} / {losses}  (win rate {win_rate:.1f}%)")
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

- [ ] **Step 2: Verify import**

```bash
.venv/bin/python -c "from backtest.report import print_report; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add backtest/report.py
git commit -m "refactor: extract backtest/report.py — reporting only"
```

---

## Task 3: Write failing tests for `backtest/data.py`

**Files:**
- Create: `tests/test_backtest_data.py`

TDD: write all tests before implementing `data.py`. These tests cover the pure functions and the VIX classification helper. Cache and fetch tests are in Task 5.

- [ ] **Step 1: Create `tests/test_backtest_data.py`**

```python
"""Unit tests for backtest/data.py — pure functions and VIX classification."""
import pytest
import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


@pytest.mark.unit
def test_select_watch_list_for_day_ranks_by_atr():
    """HIGHVOL (10% ATR) should rank above LOWVOL (1% ATR)."""
    from backtest.data import select_watch_list_for_day

    day = datetime(2026, 5, 4, tzinfo=ET)  # Monday
    dates = pd.date_range("2026-04-06", periods=20, freq="B", tz="UTC")

    highvol = pd.DataFrame(
        {"open": [10.0]*20, "high": [11.0]*20, "low": [9.0]*20,
         "close": [10.0]*20, "volume": [5000]*20},
        index=dates,
    )
    lowvol = pd.DataFrame(
        {"open": [100.0]*20, "high": [100.5]*20, "low": [99.5]*20,
         "close": [100.0]*20, "volume": [5000]*20},
        index=dates,
    )

    result = select_watch_list_for_day({"HIGHVOL": highvol, "LOWVOL": lowvol}, day, n=10)

    assert result[0] == "HIGHVOL"
    assert "LOWVOL" in result


@pytest.mark.unit
def test_select_watch_list_for_day_excludes_future_bars():
    """Bars on `day` itself must not be used — only prior_day and earlier."""
    from backtest.data import select_watch_list_for_day

    day = datetime(2026, 5, 5, tzinfo=ET)
    dates_past = pd.date_range("2026-04-07", periods=20, freq="B", tz="UTC")
    dates_future = pd.date_range("2026-05-05", periods=5, freq="B", tz="UTC")

    # SYM_A: low ATR in past bars, extreme ATR in future bars
    past_df = pd.DataFrame(
        {"open": [10.0]*20, "high": [10.5]*20, "low": [9.5]*20,
         "close": [10.0]*20, "volume": [1000]*20},
        index=dates_past,
    )
    future_df = pd.DataFrame(
        {"open": [10.0]*5, "high": [20.0]*5, "low": [1.0]*5,
         "close": [10.0]*5, "volume": [1000]*5},
        index=dates_future,
    )
    sym_a = pd.concat([past_df, future_df])

    # SYM_B: consistent moderate ATR
    sym_b = pd.DataFrame(
        {"open": [100.0]*20, "high": [103.0]*20, "low": [97.0]*20,
         "close": [100.0]*20, "volume": [5000]*20},
        index=dates_past,
    )

    result = select_watch_list_for_day({"SYM_A": sym_a, "SYM_B": sym_b}, day, n=5)

    # SYM_B should rank first; future bars for SYM_A must not inflate its score
    assert result[0] == "SYM_B"


@pytest.mark.unit
def test_select_watch_list_for_day_respects_n():
    """Returns at most n symbols."""
    from backtest.data import select_watch_list_for_day

    day = datetime(2026, 5, 4, tzinfo=ET)
    dates = pd.date_range("2026-04-06", periods=20, freq="B", tz="UTC")

    daily_bars = {}
    for i in range(10):
        sym = f"SYM{i}"
        daily_bars[sym] = pd.DataFrame(
            {"open": [10.0]*20, "high": [11.0]*20, "low": [9.0]*20,
             "close": [10.0]*20, "volume": [1000]*20},
            index=dates,
        )

    result = select_watch_list_for_day(daily_bars, day, n=3)

    assert len(result) == 3


@pytest.mark.unit
def test_classify_vix_day_normal():
    """VIX below 90d average → NORMAL."""
    from backtest.data import _classify_vix_day

    # 91 bars at 20.0 (avg=20), then current at 15.0 (below avg)
    past_vix = [(i, 20.0) for i in range(91)] + [(91, 15.0)]
    past_spy = [(0, 400.0), (1, 400.0)]

    assert _classify_vix_day(past_vix, past_spy) == "NORMAL"


@pytest.mark.unit
def test_classify_vix_day_high_vol():
    """VIX above 90d avg but SPY not down > 1% → HIGH_VOL."""
    from backtest.data import _classify_vix_day

    # avg of past 90 = 15, current = 25 (above), prev = 20 (rising), SPY flat
    past_vix = [(i, 15.0) for i in range(90)] + [(90, 20.0), (91, 25.0)]
    past_spy = [(0, 400.0), (1, 400.0)]

    assert _classify_vix_day(past_vix, past_spy) == "HIGH_VOL"


@pytest.mark.unit
def test_classify_vix_day_crisis():
    """VIX above avg, rising, and SPY down > 1% → CRISIS."""
    from backtest.data import _classify_vix_day

    past_vix = [(i, 15.0) for i in range(90)] + [(90, 20.0), (91, 25.0)]
    past_spy = [(0, 400.0), (1, 392.0)]  # -2% drop

    assert _classify_vix_day(past_vix, past_spy) == "CRISIS"


@pytest.mark.unit
def test_classify_vix_day_empty_returns_normal():
    """Empty VIX data → falls back to NORMAL."""
    from backtest.data import _classify_vix_day

    assert _classify_vix_day([], []) == "NORMAL"


@pytest.mark.unit
def test_fetch_vix_regimes_uses_yahoo_get_once(monkeypatch):
    """fetch_vix_regimes must call yahoo_get exactly twice (VIX + SPY), not once per day."""
    call_urls: list[str] = []

    def mock_yahoo_get(url: str) -> dict:
        call_urls.append(url)
        ts = list(range(1_700_000_000, 1_700_000_200))
        prices = [15.0] * 200
        return {"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [{"close": prices}]}}]}}

    monkeypatch.setattr("backtest.data.yahoo_get", mock_yahoo_get)

    from backtest.data import fetch_vix_regimes
    days = [
        datetime(2026, 5, 1, tzinfo=ET),
        datetime(2026, 5, 2, tzinfo=ET),
        datetime(2026, 5, 5, tzinfo=ET),
    ]
    fetch_vix_regimes(days)

    assert len(call_urls) == 2  # one VIX, one SPY — NOT 6 (2 × 3 days)


@pytest.mark.unit
def test_fetch_vix_regimes_fallback_on_error(monkeypatch):
    """If Yahoo fetch raises, all days fall back to NORMAL."""
    def mock_yahoo_get(url: str) -> dict:
        raise ConnectionError("network down")

    monkeypatch.setattr("backtest.data.yahoo_get", mock_yahoo_get)

    from backtest.data import fetch_vix_regimes
    days = [datetime(2026, 5, 1, tzinfo=ET), datetime(2026, 5, 2, tzinfo=ET)]
    result = fetch_vix_regimes(days)

    assert result == {"2026-05-01": "NORMAL", "2026-05-02": "NORMAL"}
```

- [ ] **Step 2: Run tests — confirm they all fail (module doesn't exist yet)**

```bash
.venv/bin/python -m pytest tests/test_backtest_data.py -q
```

Expected: `ERROR` — `ModuleNotFoundError: No module named 'backtest.data'`

- [ ] **Step 3: Commit failing tests**

```bash
git add tests/test_backtest_data.py
git commit -m "test: add failing tests for backtest/data.py pure functions + VIX"
```

---

## Task 4: Implement `backtest/data.py` — pure functions + VIX + daily-bar fetch

**Files:**
- Create: `backtest/data.py`

Implements the functions tested in Task 3. Also includes `fetch_daily_bars_range` (single Alpaca call replacing the per-day loop).

**Note on VIX bug fix:** The original `_fetch_vix_regime_for_day` contains a Python tuple-unpacking bug:
```python
# BUG in backtest.py line 176:
_, vix_prev = past_vix[-2] if len(past_vix) >= 2 else past_vix[-1], vix_current
# Right side is a 2-tuple: (past_vix[-2], vix_current)
# So: _ = past_vix[-2] tuple, vix_prev = vix_current → vix_rising is always False → CRISIS never fires
```
The new `_classify_vix_day` fixes this: `vix_prev = past_vix[-2][1] if len(past_vix) >= 2 else vix_current`.

- [ ] **Step 1: Create `backtest/data.py`**

```python
"""
Backtest data layer: fetch 5-min bars (cached), daily bars, VIX regimes.
All network I/O lives here. No simulation logic.
"""
import hashlib
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

import config
from utils.constants import ET
from utils.http import yahoo_get

logger = logging.getLogger(__name__)

_FETCH_SYMBOL_BATCH = 25
_FETCH_DAY_CHUNK    = 30   # trading days per time-window chunk
_CACHE_DIR          = config.BASE_DIR / "data" / "cache"


# ── Watch-list selection ─────────────────────────────────────────────────────

def select_watch_list_for_day(
    daily_bars: dict[str, pd.DataFrame],
    day: datetime,
    n: int = 25,
) -> list[str]:
    """
    Rank tickers by ATR% using daily bars up to (but not including) `day`.
    No network calls — slices the pre-fetched daily_bars dict in memory.
    """
    prior_day_cutoff = day.replace(hour=0, minute=0, second=0, microsecond=0)
    lookback_cutoff  = prior_day_cutoff - timedelta(days=22)  # ~20 trading-day buffer

    scores: dict[str, float] = {}
    for sym, df in daily_bars.items():
        if df.empty:
            continue
        idx = df.index
        if idx.tz is None:
            idx = idx.tz_localize("UTC")
            df = df.copy()
            df.index = idx

        window = df[
            (df.index >= lookback_cutoff.astimezone(ZoneInfo("UTC"))) &
            (df.index <  prior_day_cutoff.astimezone(ZoneInfo("UTC")))
        ]
        if len(window) < 5:
            continue

        prev_close = window["close"].shift(1)
        tr = pd.concat([
            window["high"] - window["low"],
            (window["high"] - prev_close).abs(),
            (window["low"]  - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr   = tr.ewm(com=13, min_periods=5).mean().iloc[-1]
        price = window["close"].iloc[-1]
        scores[sym] = atr / price if price > 0 else 0.0

    ranked = sorted(scores, key=lambda s: scores[s], reverse=True)
    watch  = ranked[:n] if len(ranked) >= n else list(ranked)
    logger.info(
        "  Watch list for %s (%d): %s%s",
        day.date(), len(watch),
        ", ".join(watch[:10]), "…" if len(watch) > 10 else "",
    )
    return watch


# ── Daily bars — single fetch for entire range ───────────────────────────────

def fetch_daily_bars_range(
    symbols: list[str], start: datetime, end: datetime
) -> dict[str, pd.DataFrame]:
    """
    Fetch daily OHLCV bars for all symbols in one Alpaca request.
    Call once per backtest run; slice per day with select_watch_list_for_day().
    """
    client = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)
    logger.info("Fetching daily bars %s → %s for %d tickers…", start.date(), end.date(), len(symbols))

    sym_batches = [symbols[i:i + _FETCH_SYMBOL_BATCH] for i in range(0, len(symbols), _FETCH_SYMBOL_BATCH)]
    result: dict[str, pd.DataFrame] = {}

    for batch in sym_batches:
        try:
            req = StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
                feed="iex",
            )
            bars  = client.get_stock_bars(req)
            df_all = bars.df
            if df_all is None or df_all.empty:
                continue
            if isinstance(df_all.index, pd.MultiIndex):
                for sym in batch:
                    try:
                        df = df_all.xs(sym, level="symbol")
                        if not df.empty:
                            result[sym] = df
                    except KeyError:
                        pass
            elif batch:
                result[batch[0]] = df_all
        except Exception as exc:
            logger.warning("Daily bar fetch failed for batch %s…: %s", batch[:3], exc)

    logger.info("  Daily bars received for %d symbols.", len(result))
    return result


# ── VIX regime — single fetch for entire range ───────────────────────────────

def _classify_vix_day(
    past_vix: list[tuple[int, float]],
    past_spy: list[tuple[int, float]],
) -> str:
    """Classify one day's regime from pre-fetched VIX + SPY pair lists."""
    if not past_vix:
        return "NORMAL"

    _, vix_current = past_vix[-1]
    vix_prev  = past_vix[-2][1] if len(past_vix) >= 2 else vix_current
    vix_rising = vix_current > vix_prev

    lookback    = min(90, len(past_vix) - 1)
    vix_avg_90d = (
        sum(c for _, c in past_vix[-lookback - 1:-1]) / lookback
        if lookback > 0 else vix_current
    )

    spy_change = 0.0
    if len(past_spy) >= 2:
        spy_change = (past_spy[-1][1] - past_spy[-2][1]) / past_spy[-2][1] * 100

    if vix_current > vix_avg_90d and vix_rising and spy_change < -1.0:
        return "CRISIS"
    if vix_current > vix_avg_90d:
        return "HIGH_VOL"
    return "NORMAL"


def fetch_vix_regimes(trading_days: list[datetime]) -> dict[str, str]:
    """
    Fetch VIX + SPY once, then classify every day in trading_days.
    Returns {date_str → "NORMAL" | "HIGH_VOL" | "CRISIS"}.
    """
    def _yahoo_pairs(symbol: str, range_str: str) -> list[tuple[int, float]]:
        data = yahoo_get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            f"?interval=1d&range={range_str}"
        )
        result = data["chart"]["result"][0]
        ts_raw = result["timestamp"]
        cl_raw = result["indicators"]["quote"][0]["close"]
        return [(t, c) for t, c in zip(ts_raw, cl_raw) if c is not None]

    try:
        vix_pairs = _yahoo_pairs("%5EVIX", "200d")
        spy_pairs = _yahoo_pairs("SPY",    "200d")
        logger.info("  VIX/SPY fetched: %d VIX bars, %d SPY bars", len(vix_pairs), len(spy_pairs))
    except Exception as exc:
        logger.warning("VIX fetch failed (%s) — defaulting all days to NORMAL", exc)
        return {d.strftime("%Y-%m-%d"): "NORMAL" for d in trading_days}

    regimes: dict[str, str] = {}
    for day in trading_days:
        date_str = day.strftime("%Y-%m-%d")
        day_ts   = day.timestamp()
        past_vix = [(t, c) for t, c in vix_pairs if t < day_ts]
        past_spy = [(t, c) for t, c in spy_pairs if t < day_ts]
        try:
            regimes[date_str] = _classify_vix_day(past_vix, past_spy)
        except Exception as exc:
            logger.warning("VIX classify failed for %s: %s — NORMAL", date_str, exc)
            regimes[date_str] = "NORMAL"
        logger.info("  %s: regime=%s", date_str, regimes[date_str])

    return regimes


# ── 5-min bars with Parquet cache + parallel fetch ───────────────────────────

def _cache_path(symbols: list[str], start: datetime, end: datetime, cache_dir: Path) -> Path:
    sym_hash = hashlib.sha256(",".join(sorted(symbols)).encode()).hexdigest()[:8]
    return cache_dir / f"bars_5min_{start.date()}_{end.date()}_{sym_hash}.parquet"


def _fetch_one_batch(
    client: StockHistoricalDataClient,
    symbols: list[str],
    start: datetime,
    end: datetime,
) -> dict[str, pd.DataFrame]:
    req = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame(5, TimeFrameUnit.Minute),
        start=start,
        end=end,
        feed="iex",
    )
    bars   = client.get_stock_bars(req)
    df_all = bars.df
    result: dict[str, pd.DataFrame] = {}
    if df_all is None or df_all.empty:
        return result
    if isinstance(df_all.index, pd.MultiIndex):
        for sym in symbols:
            try:
                df = df_all.xs(sym, level="symbol")
                if not df.empty:
                    result[sym] = df
            except KeyError:
                pass
    elif symbols:
        result[symbols[0]] = df_all
    return result


def _fetch_5min_cold(
    client: StockHistoricalDataClient,
    symbols: list[str],
    start: datetime,
    end: datetime,
) -> dict[str, pd.DataFrame]:
    """Fetch all symbol batches × time chunks in parallel via ThreadPoolExecutor."""
    chunk_size_days = _FETCH_DAY_CHUNK * 7 // 5
    time_chunks: list[tuple[datetime, datetime]] = []
    cs = start
    while cs < end:
        ce = min(cs + timedelta(days=chunk_size_days), end)
        time_chunks.append((cs, ce))
        cs = ce

    sym_batches = [symbols[i:i + _FETCH_SYMBOL_BATCH] for i in range(0, len(symbols), _FETCH_SYMBOL_BATCH)]
    tasks = [(b, ts, te) for ts, te in time_chunks for b in sym_batches]

    logger.info(
        "  Fetching %d batches × %d time chunks = %d parallel tasks…",
        len(sym_batches), len(time_chunks), len(tasks),
    )

    result: dict[str, pd.DataFrame] = {}
    lock = threading.Lock()

    def _run(batch: list[str], t_start: datetime, t_end: datetime) -> dict[str, pd.DataFrame]:
        return _fetch_one_batch(client, batch, t_start, t_end)

    n_workers = min(8, len(tasks))
    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        futures = {executor.submit(_run, *task): task for task in tasks}
        for future in as_completed(futures):
            batch, t_start, t_end = futures[future]
            try:
                chunk = future.result()
                with lock:
                    for sym, df in chunk.items():
                        if sym in result:
                            result[sym] = pd.concat([result[sym], df]).sort_index()
                            result[sym] = result[sym][~result[sym].index.duplicated(keep="last")]
                        else:
                            result[sym] = df
            except Exception as exc:
                logger.warning(
                    "  Fetch failed batch %s… %s→%s: %s — skipping",
                    batch[:3], t_start.date(), t_end.date(), exc,
                )

    return result


def fetch_5min_bars(
    symbols: list[str],
    start: datetime,
    end: datetime,
    cache_dir: Path | None = None,
) -> dict[str, pd.DataFrame]:
    """
    Fetch 5-min bars with Parquet disk cache.

    Cache key encodes start, end, and a hash of sorted symbols — stale data
    is impossible because the date range is embedded in the filename.
    Writes atomically via a .tmp file then rename.
    """
    cache_root = cache_dir or _CACHE_DIR
    cache_root.mkdir(parents=True, exist_ok=True)
    cache_file = _cache_path(symbols, start, end, cache_root)

    if cache_file.exists():
        logger.info("  Cache hit: %s — loading from disk…", cache_file.name)
        df_all = pd.read_parquet(cache_file)
        result: dict[str, pd.DataFrame] = {}
        for sym in df_all.index.get_level_values("symbol").unique():
            result[sym] = df_all.xs(sym, level="symbol")
        logger.info("  Loaded %d symbols from cache.", len(result))
        return result

    logger.info(
        "  Cache miss — fetching from Alpaca: %d symbols, %s → %s",
        len(symbols), start.date(), end.date(),
    )
    client = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)
    result = _fetch_5min_cold(client, symbols, start, end)
    logger.info("  Bars received for %d symbols.", len(result))

    if result:
        combined = pd.concat(
            {sym: df for sym, df in result.items()},
            names=["symbol"],
        )
        tmp = cache_file.with_suffix(".tmp")
        combined.to_parquet(tmp)
        tmp.rename(cache_file)
        logger.info("  Cached to %s", cache_file.name)

    return result
```

- [ ] **Step 2: Run the Task 3 tests — they should all pass now**

```bash
.venv/bin/python -m pytest tests/test_backtest_data.py -q
```

Expected: all 9 tests pass, 0 failures.

- [ ] **Step 3: Commit**

```bash
git add backtest/data.py
git commit -m "feat: backtest/data.py — Parquet cache, parallel fetch, single VIX+daily-bar fetch"
```

---

## Task 5: Write failing tests for Parquet cache behaviour

**Files:**
- Modify: `tests/test_backtest_data.py`

Add cache hit/miss tests to the existing file.

- [ ] **Step 1: Append the following tests to `tests/test_backtest_data.py`**

```python
@pytest.mark.unit
def test_cache_miss_fetches_and_saves_parquet(tmp_path, monkeypatch):
    """Cold fetch: _fetch_one_batch is called and a .parquet file is written."""
    import pandas as pd
    from datetime import datetime, timezone

    mock_df = pd.DataFrame(
        {"open": [100.0], "high": [101.0], "low": [99.0],
         "close": [100.5], "volume": [1000]},
        index=pd.DatetimeIndex(
            [pd.Timestamp("2026-05-01 14:30:00", tz="UTC")], name="timestamp"
        ),
    )
    call_count = {"n": 0}

    def mock_fetch_one_batch(client, symbols, start, end):
        call_count["n"] += 1
        return {"AAPL": mock_df}

    monkeypatch.setattr("backtest.data._fetch_one_batch", mock_fetch_one_batch)
    monkeypatch.setattr(
        "backtest.data.StockHistoricalDataClient",
        lambda *a, **kw: None,
    )

    from backtest.data import fetch_5min_bars
    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    end   = datetime(2026, 5, 2, tzinfo=timezone.utc)

    result = fetch_5min_bars(["AAPL"], start, end, cache_dir=tmp_path)

    assert "AAPL" in result
    parquet_files = list(tmp_path.glob("*.parquet"))
    assert len(parquet_files) == 1
    assert call_count["n"] >= 1  # network was called


@pytest.mark.unit
def test_cache_hit_skips_network(tmp_path, monkeypatch):
    """Warm fetch: _fetch_one_batch is NOT called on second invocation."""
    import pandas as pd
    from datetime import datetime, timezone

    mock_df = pd.DataFrame(
        {"open": [100.0], "high": [101.0], "low": [99.0],
         "close": [100.5], "volume": [1000]},
        index=pd.DatetimeIndex(
            [pd.Timestamp("2026-05-01 14:30:00", tz="UTC")], name="timestamp"
        ),
    )
    call_count = {"n": 0}

    def mock_fetch_one_batch(client, symbols, start, end):
        call_count["n"] += 1
        return {"AAPL": mock_df}

    monkeypatch.setattr("backtest.data._fetch_one_batch", mock_fetch_one_batch)
    monkeypatch.setattr(
        "backtest.data.StockHistoricalDataClient",
        lambda *a, **kw: None,
    )

    from backtest.data import fetch_5min_bars
    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    end   = datetime(2026, 5, 2, tzinfo=timezone.utc)

    fetch_5min_bars(["AAPL"], start, end, cache_dir=tmp_path)  # cold
    calls_after_cold = call_count["n"]
    fetch_5min_bars(["AAPL"], start, end, cache_dir=tmp_path)  # warm

    assert call_count["n"] == calls_after_cold  # no additional network calls on warm hit


@pytest.mark.unit
def test_cache_key_differs_for_different_date_ranges(tmp_path, monkeypatch):
    """Different date ranges must produce different cache files — no cross-contamination."""
    import pandas as pd
    from datetime import datetime, timezone
    from backtest.data import _cache_path

    symbols = ["AAPL", "TSLA"]
    start_a = datetime(2026, 4, 1, tzinfo=timezone.utc)
    end_a   = datetime(2026, 4, 30, tzinfo=timezone.utc)
    start_b = datetime(2026, 5, 1, tzinfo=timezone.utc)
    end_b   = datetime(2026, 5, 31, tzinfo=timezone.utc)

    path_a = _cache_path(symbols, start_a, end_a, tmp_path)
    path_b = _cache_path(symbols, start_b, end_b, tmp_path)

    assert path_a != path_b


@pytest.mark.unit
def test_cache_key_differs_for_different_symbols(tmp_path):
    """Different symbol sets must produce different cache files."""
    from datetime import datetime, timezone
    from backtest.data import _cache_path

    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    end   = datetime(2026, 5, 31, tzinfo=timezone.utc)

    path_a = _cache_path(["AAPL", "TSLA"], start, end, tmp_path)
    path_b = _cache_path(["AAPL", "NVDA"], start, end, tmp_path)

    assert path_a != path_b
```

- [ ] **Step 2: Run the new cache tests to confirm they pass**

```bash
.venv/bin/python -m pytest tests/test_backtest_data.py -q
```

Expected: all 13 tests pass.

- [ ] **Step 3: Commit**

```bash
git add tests/test_backtest_data.py
git commit -m "test: add Parquet cache hit/miss and key-uniqueness tests"
```

---

## Task 6: Create `backtest/runner.py`

**Files:**
- Create: `backtest/runner.py`

Orchestration only. Replaces the `run_backtest()` function and associated helpers from `backtest.py`.

- [ ] **Step 1: Create `backtest/runner.py`**

```python
"""
Backtest orchestration: load tickers, fetch data, loop days, produce report.
No simulation logic. No network I/O (delegates to backtest.data).
"""
import logging
from datetime import date, datetime, timedelta

import config
from backtest.data import (
    fetch_5min_bars, fetch_daily_bars_range,
    fetch_vix_regimes, select_watch_list_for_day,
)
from backtest.report import print_report
from backtest.simulator import (
    SimState, simulate_day,
    RSI_BUY_MAX, RSI_BUY_MAX_HV, MIN_COMPOSITE,
    ENTRY_START_H, ENTRY_END_H, HIGH_VOL_MULT, VWAP_SLOPE_MIN,
    WATCH_LIST_SIZE,
)
from utils.constants import ET
from utils.io_utils import load_json

logger = logging.getLogger(__name__)

# US market holidays (NYSE) — extend annually
_NYSE_HOLIDAYS: set[date] = {
    date(2026, 1, 1),  date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 7, 3),  date(2026, 9, 7),  date(2026, 11, 26),
    date(2026, 12, 25),
    date(2025, 1, 1),  date(2025, 1, 20), date(2025, 2, 17), date(2025, 4, 18),
    date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4),  date(2025, 9, 1),
    date(2025, 11, 27), date(2025, 12, 25),
}


def get_trading_days(start: date, end: date) -> list[datetime]:
    """Return market-open datetimes for weekdays excluding NYSE holidays in [start, end]."""
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5 and d not in _NYSE_HOLIDAYS:
            days.append(datetime(d.year, d.month, d.day, tzinfo=ET))
        d += timedelta(days=1)
    return days


def _load_backtest_params() -> dict:
    """Load learned trading params; fall back to config defaults if file missing."""
    try:
        params = load_json(config.TRADING_PARAMS_FILE, {})
        if params:
            logger.info("Backtest: using learned params: %s", params)
            return params
    except Exception as exc:
        logger.warning("Could not load trading_params.json (%s) — using defaults", exc)
    return {
        "rsi_buy_max":            RSI_BUY_MAX,
        "rsi_buy_max_high_vol":   RSI_BUY_MAX_HV,
        "composite_min_normal":   MIN_COMPOSITE,
        "entry_start_hour":       ENTRY_START_H,
        "entry_end_hour":         ENTRY_END_H,
        "high_vol_position_mult": HIGH_VOL_MULT,
        "vwap_slope_min":         VWAP_SLOPE_MIN,
    }


def run_backtest(trading_days: list[datetime]) -> None:
    if not trading_days:
        logger.error("No trading days in range — nothing to backtest.")
        return

    logger.info("Loading tickers…")
    with open(config.TICKERS_FILE) as f:
        all_tickers = [
            line.strip().upper() for line in f
            if line.strip() and not line.strip().startswith("#")
        ]
    logger.info("  %d tickers loaded.", len(all_tickers))

    fetch_start  = trading_days[0]  - timedelta(days=5)
    fetch_end    = trading_days[-1] + timedelta(days=1)
    daily_start  = trading_days[0]  - timedelta(days=30)  # ATR lookback buffer

    # Single fetch for 5-min bars (cached) — replaces per-batch loops
    logger.info("Fetching 5-min bars %s → %s…", fetch_start.date(), fetch_end.date())
    all_bars = data.fetch_5min_bars(all_tickers, fetch_start, fetch_end)
    logger.info("  5-min bars for %d symbols.", len(all_bars))

    # Single fetch for daily bars — replaces per-day _select_watch_list calls
    logger.info("Fetching daily bars %s → %s…", daily_start.date(), fetch_end.date())
    daily_bars = data.fetch_daily_bars_range(all_tickers, daily_start, fetch_end)

    # Single VIX+SPY fetch — replaces per-day Yahoo calls
    logger.info("Fetching VIX regimes for %d trading days…", len(trading_days))
    regimes = data.fetch_vix_regimes(trading_days)

    params = _load_backtest_params()
    state  = SimState()

    for day in trading_days:
        date_str = day.strftime("%Y-%m-%d")
        regime   = regimes.get(date_str, "NORMAL")
        watch = select_watch_list_for_day(daily_bars, day, n=WATCH_LIST_SIZE)
        watch = [s for s in watch if s in all_bars]
        if not watch:
            logger.warning("  %s: no watch-list symbols with 5-min data — skipping.", day.date())
            continue
        simulate_day(day, watch, all_bars, state, regime=regime, params=params)

    report.print_report(state)
```

- [ ] **Step 2: Verify import**

```bash
.venv/bin/python -c "from backtest.runner import run_backtest, get_trading_days; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add backtest/runner.py
git commit -m "feat: backtest/runner.py — orchestration, single-fetch data pipeline"
```

---

## Task 7: Slim `backtest.py` to CLI wrapper + fix broken test

**Files:**
- Modify: `backtest.py`
- Modify: `tests/test_foundation.py` (line 572)

`backtest.py` becomes a ~40-line entry point. The `test_foundation.py` test that imports `backtest._load_backtest_params` is updated to import from `backtest.runner`.

- [ ] **Step 1: Replace `backtest.py` with CLI-only content**

```python
"""
Backtest CLI entry point.
Run:  python backtest.py                  # last 30 trading days
      python backtest.py --days 60
      python backtest.py --start 2026-04-01
      python backtest.py --start 2026-04-01 --end 2026-04-30
"""
import argparse
import logging
import sys
from datetime import date, datetime, timedelta

from backtest.runner import _NYSE_HOLIDAYS, get_trading_days, run_backtest
from utils.constants import ET

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Trading bot backtest")
    group  = parser.add_mutually_exclusive_group()
    group.add_argument("--days",  type=int, default=30,           help="Past N trading days (default: 30)")
    group.add_argument("--start", type=str, metavar="YYYY-MM-DD", help="Start date")
    parser.add_argument("--end",  type=str, metavar="YYYY-MM-DD", help="End date (used with --start)")
    args = parser.parse_args()

    today = datetime.now(ET).date()

    if args.start:
        start_date   = date.fromisoformat(args.start)
        end_date     = date.fromisoformat(args.end) if args.end else today
        trading_days = get_trading_days(start_date, end_date)
    else:
        days_needed  = args.days
        candidate    = today - timedelta(days=1)
        trading_days: list[datetime] = []
        for _ in range(days_needed * 3):
            if candidate.weekday() < 5 and candidate not in _NYSE_HOLIDAYS:
                trading_days.insert(0, datetime(candidate.year, candidate.month, candidate.day, tzinfo=ET))
                if len(trading_days) == days_needed:
                    break
            candidate -= timedelta(days=1)

    if not trading_days:
        print("No trading days found in requested range.")
        sys.exit(1)

    run_backtest(trading_days)
```

- [ ] **Step 2: Update the broken test in `tests/test_foundation.py`**

Find line ~572:
```python
    import backtest
    params = backtest._load_backtest_params()
```

Replace with:
```python
    from backtest.runner import _load_backtest_params
    params = _load_backtest_params()
```

- [ ] **Step 3: Run the updated test to confirm it passes**

```bash
.venv/bin/python -m pytest tests/test_foundation.py::test_backtest_load_params_returns_dict -v
```

Expected: `PASSED`

- [ ] **Step 4: Commit**

```bash
git add backtest.py tests/test_foundation.py
git commit -m "refactor: slim backtest.py to CLI wrapper; update test import to backtest.runner"
```

---

## Task 8: Full test suite verification

**Files:** (none modified)

Confirm zero regressions across all 728+ existing tests plus the 13 new ones.

- [ ] **Step 1: Run the full test suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q --tb=short 2>&1 | tail -20
```

Expected: all tests pass, 0 failures. Count should be ≥ 741 (728 existing + 13 new).

- [ ] **Step 2: Confirm live-bot modules are untouched**

```bash
git diff HEAD~6 --name-only | grep -v "^backtest\|^tests/test_backtest\|^docs/"
```

Expected: only `backtest.py` and `tests/test_foundation.py` appear outside the `backtest/` package — both are confirmed non-live-bot files.

- [ ] **Step 3: Smoke-test CLI still works**

```bash
.venv/bin/python -c "
from backtest.runner import get_trading_days, _load_backtest_params
from datetime import date
days = get_trading_days(date(2026, 5, 1), date(2026, 5, 5))
print('trading days:', [d.date() for d in days])
params = _load_backtest_params()
print('params keys:', list(params.keys()))
"
```

Expected output (example):
```
trading days: [datetime.date(2026, 5, 1), datetime.date(2026, 5, 2), datetime.date(2026, 5, 5)]
params keys: ['rsi_buy_max', 'rsi_buy_max_high_vol', 'composite_min_normal', ...]
```

- [ ] **Step 4: Final commit tag**

```bash
git tag backtest-speedup-v1
```

---

## Safety Checklist (verify before closing)

| Check | Where to verify |
|---|---|
| No look-ahead bias in 5-min bars | `simulator.py`: `df_sym[df_sym.index <= ts].tail(LOOKBACK_BARS)` — unchanged |
| No look-ahead bias in watch list | `data.py select_watch_list_for_day`: cutoff = `day.replace(hour=0)` strict less-than |
| No look-ahead bias in VIX | `data.py fetch_vix_regimes`: `t < day_ts` — strict less-than preserved |
| No live-bot module touched | `git diff HEAD~6 --name-only` — no files under `trading/`, `signals/`, `indicators/`, `config.py` |
| Config SoT intact | `simulator.py` reads all live params from `config.*` — no re-definitions |
| VIX bug fixed | `_classify_vix_day`: `vix_prev = past_vix[-2][1]` — not the broken tuple-unpack |
| Test isolation | All new tests use `monkeypatch` + `tmp_path`, no real network calls |
| Rule 17 (300-line limit) | `backtest.py` ~40 lines, each module under 300 lines |
