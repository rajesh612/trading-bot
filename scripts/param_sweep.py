#!/usr/bin/env python3
"""
Backtest parameter sweep — data fetched ONCE, all combos run in-process.

No production code is modified. Overrides are applied via unittest.mock.patch
scoped to each iteration; originals are restored automatically.

Usage:
    .venv/bin/python scripts/param_sweep.py               # 30 trading days
    .venv/bin/python scripts/param_sweep.py --days 60
    .venv/bin/python scripts/param_sweep.py --start 2026-04-01 --end 2026-05-28
    .venv/bin/python scripts/param_sweep.py --top 15      # show top N in terminal

Output:
    sweep_results.csv  — all combos ranked by total_pnl
"""
from __future__ import annotations

import argparse
import csv
import itertools
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config
from backtest import data as _data
from backtest import simulator as _sim
from backtest.simulator import SimState
from utils.constants import ET
from utils.io_utils import load_json
from utils.regime_params import (
    DayTradeParams, TPSLParams,
    get_day_trade_params, get_tp_sl_params,
)

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")


# ─────────────────────────────────────────────────────────────────────────────
# Sweep axes — edit to define what to search
# Keys map to override builder args; each list is one axis.
# Total combos = product of axis lengths (+ 1 for baseline).
# ─────────────────────────────────────────────────────────────────────────────

SWEEP_AXES: dict[str, list[Any]] = {
    # SL ceiling for NORMAL/MEDIUM/DAY_TRADE  (was 2.0; all SL exits at -2.05%)
    "normal_med_sl_ceiling": [1.5, 1.8, 2.0],

    # NORMAL/MEDIUM DayTradeParams: hour after which composite floor activates
    "normal_med_late_hour": [10, 11, 12],

    # NORMAL/MEDIUM DayTradeParams: composite floor required after late_hour
    "normal_med_late_composite": [68, 72, 75],
}

MAX_COMBOS = 150  # safety cap — set lower if sweeps take too long


# ─────────────────────────────────────────────────────────────────────────────
# NYSE holiday calendar — mirrors runner.py
# ─────────────────────────────────────────────────────────────────────────────

from datetime import date as _date

_NYSE_HOLIDAYS = {
    _date(2026, 1, 1), _date(2026, 1, 19), _date(2026, 2, 16), _date(2026, 4, 3),
    _date(2026, 5, 25), _date(2026, 7, 3), _date(2026, 9, 7), _date(2026, 11, 26),
    _date(2026, 12, 25),
    _date(2025, 1, 1), _date(2025, 1, 20), _date(2025, 2, 17), _date(2025, 4, 18),
    _date(2025, 5, 26), _date(2025, 6, 19), _date(2025, 7, 4), _date(2025, 9, 1),
    _date(2025, 11, 27), _date(2025, 12, 25),
}


def _get_trading_days(start: datetime, end: datetime) -> list[datetime]:
    days: list[datetime] = []
    d = start.date()
    end_d = end.date()
    while d <= end_d:
        if d.weekday() < 5 and d not in _NYSE_HOLIDAYS:
            days.append(datetime(d.year, d.month, d.day, tzinfo=ET))
        d += timedelta(days=1)
    return days


def _load_base_params() -> dict:
    base: dict = {
        "rsi_buy_max":              config.RSI_BUY_MAX,
        "rsi_buy_max_high_vol":     config.RSI_BUY_MAX_HIGH_VOL,
        "vwap_slope_min":           config.VWAP_SLOPE_MIN,
        "composite_min_normal":     config.COMPOSITE_MIN_NORMAL,
        "composite_min_high_vol":   config.COMPOSITE_MIN_HIGH_VOL,
        "stop_loss_pct":            config.STOP_LOSS_PCT,
        "take_profit_pct":          config.TAKE_PROFIT_PCT,
        "max_position_pct":         config.MAX_POSITION_PCT,
        "risk_per_trade_pct":       config.RISK_PER_TRADE_PCT,
        "high_vol_position_mult":   config.HIGH_VOL_POSITION_MULT,
        "atr_tp_multiplier":        config.ATR_TP_MULT_DEFAULT,
    }
    learned = load_json(config.TRADING_PARAMS_FILE, {})
    if learned:
        for key in list(base.keys()):
            if learned.get(key) is not None:
                base[key] = learned[key]
    return base


# ─────────────────────────────────────────────────────────────────────────────
# Override factories — return drop-in replacements for regime_params functions
# ─────────────────────────────────────────────────────────────────────────────

def _tp_sl_override(normal_med_sl_ceiling: float):
    """Wraps get_tp_sl_params; replaces NORMAL/MEDIUM/DAY_TRADE sl_ceiling only."""
    _orig = get_tp_sl_params
    def _fn(regime: str, tier: str, mode: str) -> TPSLParams:
        p = _orig(regime, tier, mode)
        if regime == "NORMAL" and tier == "MEDIUM" and mode == "DAY_TRADE":
            return TPSLParams(
                p.tp_floor_pct, p.tp_ceiling_pct,
                p.sl_floor_pct, normal_med_sl_ceiling,
                p.atr_mult,
            )
        return p
    return _fn


def _day_trade_override(late_hour: int, late_composite: int):
    """Wraps get_day_trade_params; replaces NORMAL/MEDIUM late_hour and late_composite."""
    _orig = get_day_trade_params
    def _fn(regime: str, tier: str) -> DayTradeParams:
        p = _orig(regime, tier)
        if regime == "NORMAL" and tier == "MEDIUM":
            return DayTradeParams(
                hard_close_hour=p.hard_close_hour,
                late_entry_hour=late_hour,
                late_composite=late_composite,
                vwap_slope_min=p.vwap_slope_min,
            )
        return p
    return _fn


# ─────────────────────────────────────────────────────────────────────────────
# Metrics extraction
# ─────────────────────────────────────────────────────────────────────────────

def _metrics(state: SimState) -> dict:
    trades = state.trades
    if not trades:
        return {
            "trades": 0, "total_pnl": 0.0, "win_rate": 0.0,
            "fc_rate": 0.0, "fc_pnl": 0.0,
            "sl_count": 0, "sl_pnl": 0.0, "sl_avg": 0.0,
            "tp_count": 0, "tp_pnl": 0.0,
            "swing_pnl": 0.0, "swing_wr": 0.0,
        }

    n = len(trades)
    wins = sum(1 for t in trades if t.pnl > 0)
    fc   = [t for t in trades if t.exit_reason == "FORCE_CLOSE"]
    sl   = [t for t in trades if t.exit_reason == "SL"]
    tp   = [t for t in trades if t.exit_reason == "TP"]
    sw   = [t for t in trades if t.exit_reason.startswith("SWING")]
    sw_wins = sum(1 for t in sw if t.pnl > 0)

    total_pnl = sum(t.pnl for t in trades)
    sl_pnl    = sum(t.pnl for t in sl)

    return {
        "trades":    n,
        "total_pnl": round(total_pnl, 2),
        "win_rate":  round(wins / n * 100, 1),
        "fc_rate":   round(len(fc) / n * 100, 1),
        "fc_pnl":    round(sum(t.pnl for t in fc), 2),
        "sl_count":  len(sl),
        "sl_pnl":    round(sl_pnl, 2),
        "sl_avg":    round(sl_pnl / len(sl), 2) if sl else 0.0,
        "tp_count":  len(tp),
        "tp_pnl":    round(sum(t.pnl for t in tp), 2),
        "swing_pnl": round(sum(t.pnl for t in sw), 2),
        "swing_wr":  round(sw_wins / len(sw) * 100, 1) if sw else 0.0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Single run
# ─────────────────────────────────────────────────────────────────────────────

def _run_combo(
    combo: dict[str, Any],
    trading_days: list[datetime],
    day_watch_map: dict[str, list[str]],
    all_bars: dict,
    regimes: dict[str, str],
    base_params: dict,
    day_precomp: dict[str, dict[str, dict]],   # pre-built per-day indicator cache
) -> dict:
    sl_ceiling   = combo["normal_med_sl_ceiling"]
    late_hour    = combo["normal_med_late_hour"]
    late_comp    = combo["normal_med_late_composite"]

    tp_sl_fn = _tp_sl_override(sl_ceiling)
    dt_fn    = _day_trade_override(late_hour, late_comp)

    is_baseline = combo.get("_baseline_", False)

    state = SimState(atr_tp_multiplier=base_params.get("atr_tp_multiplier", config.ATR_TP_MULT_DEFAULT))

    patches = [] if is_baseline else [
        patch("backtest.simulator.get_tp_sl_params", tp_sl_fn),
        patch("signals.tp_scaler.get_tp_sl_params",  tp_sl_fn),
        patch("utils.regime_params.get_day_trade_params", dt_fn),
    ]

    def _run():
        for day in trading_days:
            date_str = day.strftime("%Y-%m-%d")
            watch = day_watch_map.get(date_str)
            if not watch:
                continue
            regime = regimes.get(date_str, "NORMAL")
            _sim.simulate_day(
                day, watch, all_bars, state,
                regime=regime, params=base_params,
                precomp_cache=day_precomp.get(date_str),
            )

    if patches:
        # Stack context managers manually for Python 3.9 compatibility
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            _run()
    else:
        _run()

    return _metrics(state)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Backtest parameter sweep.")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--days",  type=int, default=30, help="trailing N trading days (default 30)")
    grp.add_argument("--start", type=str, help="start date YYYY-MM-DD")
    ap.add_argument("--end",    type=str, help="end date YYYY-MM-DD (default today)")
    ap.add_argument("--top",    type=int, default=10, help="rows to print in terminal summary")
    ap.add_argument("--out",    type=str, default="sweep_results.csv", help="output CSV path")
    return ap.parse_args()


def main() -> None:
    args = _parse_args()
    today = datetime.now(tz=ET).replace(hour=0, minute=0, second=0, microsecond=0)

    if args.start:
        start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=ET)
        end   = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=ET) if args.end else today
    else:
        end   = today
        start = today - timedelta(days=int(args.days * 1.5))

    trading_days = _get_trading_days(start, end)
    if not trading_days:
        print(f"No trading days in range {start.date()} – {end.date()}")
        sys.exit(1)
    print(f"Trading window: {trading_days[0].date()} → {trading_days[-1].date()} ({len(trading_days)} days)")

    # ── Build cartesian product ──────────────────────────────────────────────
    axis_names  = list(SWEEP_AXES.keys())
    axis_values = [SWEEP_AXES[k] for k in axis_names]
    combos: list[dict[str, Any]] = []
    for vals in itertools.product(*axis_values):
        combos.append(dict(zip(axis_names, vals)))

    if len(combos) > MAX_COMBOS:
        print(f"ERROR: {len(combos)} combos exceeds MAX_COMBOS={MAX_COMBOS}. Reduce SWEEP_AXES.")
        sys.exit(1)

    # Prepend baseline (current config, no patches) — skip if already in the sweep grid
    current_sl  = get_tp_sl_params("NORMAL", "MEDIUM", "DAY_TRADE").sl_ceiling_pct
    current_dtp = get_day_trade_params("NORMAL", "MEDIUM")
    baseline = {
        "normal_med_sl_ceiling":     current_sl,
        "normal_med_late_hour":      current_dtp.late_entry_hour,
        "normal_med_late_composite": current_dtp.late_composite,
        "_baseline_": True,
    }
    baseline_key = (current_sl, current_dtp.late_entry_hour, current_dtp.late_composite)
    already_in_grid = any(
        (c["normal_med_sl_ceiling"], c["normal_med_late_hour"], c["normal_med_late_composite"]) == baseline_key
        for c in combos
    )
    if already_in_grid:
        # Mark the matching combo as baseline instead of adding a duplicate run
        for c in combos:
            if (c["normal_med_sl_ceiling"], c["normal_med_late_hour"], c["normal_med_late_composite"]) == baseline_key:
                c["_baseline_"] = True
                break
    else:
        combos.insert(0, baseline)

    print(f"Sweep: {len(combos)} combos (incl. baseline)  ·  axes: {axis_names}")

    # ── Fetch data ONCE ──────────────────────────────────────────────────────
    import sectors
    tickers = sectors.load_universe()
    if not tickers:
        print("ERROR: No tickers loaded — aborting.")
        sys.exit(1)

    fetch_start = trading_days[0] - timedelta(days=5)
    fetch_end   = trading_days[-1] + timedelta(days=1)

    print(f"Fetching bars for {len(tickers)} tickers ({fetch_start.date()} → {fetch_end.date()})…")
    t0 = time.time()
    all_bars    = _data.fetch_5min_bars(tickers, fetch_start, fetch_end)
    daily_bars  = _data.fetch_daily_bars_range(tickers, fetch_start, fetch_end)
    regimes     = _data.fetch_vix_regimes(trading_days)
    print(f"  Bars ready in {time.time()-t0:.1f}s  ({len(all_bars)} symbols with 5-min data)")

    # Build watch list per day (same logic as runner)
    day_watch_map: dict[str, list[str]] = {}
    for day in trading_days:
        watch = _data.select_watch_list_for_day(daily_bars, day)
        watch = [s for s in watch if s in all_bars]
        if watch:
            day_watch_map[day.strftime("%Y-%m-%d")] = watch

    base_params = _load_base_params()

    # ── Pre-build indicator cache ONCE (eliminates ~90% of per-combo compute time) ──
    print("Pre-computing indicators for all days/symbols…", end="", flush=True)
    t_pre = time.time()
    from indicators.precompute import precompute_indicators_for_day
    day_precomp: dict[str, dict[str, dict]] = {}
    for day in trading_days:
        date_str = day.strftime("%Y-%m-%d")
        watch = day_watch_map.get(date_str, [])
        cache: dict[str, dict] = {}
        for sym in watch:
            df_sym = all_bars.get(sym)
            if df_sym is not None and not df_sym.empty:
                cache[sym] = precompute_indicators_for_day(sym, df_sym, day)
        day_precomp[date_str] = cache
    print(f" done in {time.time()-t_pre:.1f}s")

    # ── Run sweep ────────────────────────────────────────────────────────────
    results: list[dict] = []
    n_total = len(combos)
    for i, combo in enumerate(combos, 1):
        label = "BASELINE" if combo.get("_baseline_") else (
            f"sl={combo['normal_med_sl_ceiling']}  "
            f"late@{combo['normal_med_late_hour']}h≥{combo['normal_med_late_composite']}"
        )
        print(f"  [{i:>3}/{n_total}] {label}", end="", flush=True)
        t1 = time.time()
        m = _run_combo(combo, trading_days, day_watch_map, all_bars, regimes, base_params, day_precomp)
        elapsed = time.time() - t1
        print(f"  → P&L ${m['total_pnl']:>+9,.2f}  trades={m['trades']:>3}  "
              f"WR={m['win_rate']:>5.1f}%  SL={m['sl_count']}@${m['sl_avg']:>+.0f}  "
              f"({elapsed:.1f}s)")
        row = {**{k: v for k, v in combo.items() if not k.startswith("_")}, **m}
        results.append(row)

    # ── Write CSV ─────────────────────────────────────────────────────────────
    results.sort(key=lambda r: r["total_pnl"], reverse=True)
    out_path = ROOT / args.out
    fieldnames = list(results[0].keys()) if results else []
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)
    print(f"\nResults saved → {out_path}")

    # ── Terminal summary ──────────────────────────────────────────────────────
    top_n = min(args.top, len(results))
    col_w = 8
    header = (
        f"{'rank':>4}  {'sl_ceil':>7}  {'late_h':>6}  {'late_c':>6}  "
        f"{'total_pnl':>10}  {'trades':>6}  {'WR%':>5}  "
        f"{'fc%':>5}  {'sl_n':>4}  {'sl_avg':>7}  {'tp_n':>4}  {'swing_pnl':>9}"
    )
    print(f"\n{'─'*len(header)}")
    print(f"Top {top_n} combos by total P&L")
    print(f"{'─'*len(header)}")
    print(header)
    print(f"{'─'*len(header)}")
    for rank, r in enumerate(results[:top_n], 1):
        baseline_marker = " *" if (
            r["normal_med_sl_ceiling"] == baseline["normal_med_sl_ceiling"]
            and r["normal_med_late_hour"] == baseline["normal_med_late_hour"]
            and r["normal_med_late_composite"] == baseline["normal_med_late_composite"]
        ) else "  "
        print(
            f"{rank:>4}{baseline_marker}"
            f"  {r['normal_med_sl_ceiling']:>7.1f}"
            f"  {r['normal_med_late_hour']:>6}"
            f"  {r['normal_med_late_composite']:>6}"
            f"  ${r['total_pnl']:>+9,.2f}"
            f"  {r['trades']:>6}"
            f"  {r['win_rate']:>5.1f}"
            f"  {r['fc_rate']:>5.1f}"
            f"  {r['sl_count']:>4}"
            f"  ${r['sl_avg']:>+6.0f}"
            f"  {r['tp_count']:>4}"
            f"  ${r['swing_pnl']:>+8,.2f}"
        )
    print(f"{'─'*len(header)}")
    print("  * = current config (baseline)")


if __name__ == "__main__":
    main()
