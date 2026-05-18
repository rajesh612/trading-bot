#!/usr/bin/env python3
"""ATR TP multiplier sweep: compare bot metrics across multiplier values.

Usage:
    .venv/bin/python3 backtest_sweep.py              # last 60 trading days
    .venv/bin/python3 backtest_sweep.py --days 90
    .venv/bin/python3 backtest_sweep.py --start 2026-03-01 --end 2026-05-16
"""
import argparse
import logging
from dataclasses import asdict
from datetime import datetime, timedelta

import pandas as pd

import config
from backtest.data import fetch_5min_bars, fetch_daily_bars_range, fetch_vix_regimes, select_watch_list_for_day
from backtest.report import _compute_universe_benchmark
from backtest.runner import _get_trading_days, _load_tickers, _load_backtest_params
from backtest.simulator import SimState, simulate_day
from utils.constants import ET

logging.basicConfig(level=logging.WARNING, format="%(message)s")


def _run_one(trading_days, all_bars, daily_bars, regimes, multiplier) -> dict:
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
        return {
            "multiplier": multiplier, "trades": 0, "pnl_pct": 0.0,
            "win_rate": 0.0, "force_close_pct": 0.0, "max_dd": 0.0,
            "bench_pct": 0.0, "alpha": 0.0,
        }

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
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--start")
    parser.add_argument("--end")
    args = parser.parse_args()

    end_dt = datetime.now(tz=ET).replace(hour=0, minute=0, second=0, microsecond=0)
    if args.end:
        end_dt = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=ET)
    start_dt = end_dt - timedelta(days=int(args.days * 1.5))
    if args.start:
        start_dt = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=ET)

    trading_days = _get_trading_days(start_dt, end_dt)
    if not trading_days:
        print("No trading days in range.")
        return

    tickers = _load_tickers()
    if not tickers:
        print("No tickers loaded.")
        return

    fetch_start = trading_days[0] - timedelta(days=5)
    fetch_end = trading_days[-1] + timedelta(days=1)

    print(f"Fetching data for {len(tickers)} tickers ({len(trading_days)} days)…")
    all_bars = fetch_5min_bars(tickers, fetch_start, fetch_end)
    daily_bars = fetch_daily_bars_range(tickers, fetch_start, fetch_end)
    regimes = fetch_vix_regimes(trading_days)

    multipliers = [1.0, 1.5, 2.0, 2.5, 3.0]
    results = []
    for m in multipliers:
        print(f"  Running multiplier {m:.1f}×…", end=" ", flush=True)
        r = _run_one(trading_days, all_bars, daily_bars, regimes, m)
        results.append(r)
        print(f"P&L={r['pnl_pct']:+.2f}%  FC={r['force_close_pct']:.0f}%  α={r['alpha']:+.2f}%")

    print("\n" + "=" * 75)
    print(f"  {'Mult':>4}  {'Trades':>6}  {'P&L%':>7}  {'WinRate':>7}  {'FC%':>5}  {'MaxDD':>6}  {'Bench%':>7}  {'Alpha':>6}")
    print("-" * 75)
    for r in results:
        marker = " ◄ default" if r["multiplier"] == config.ATR_TP_MULT_DEFAULT else ""
        print(
            f"  {r['multiplier']:>4.1f}×  {r['trades']:>6}  {r['pnl_pct']:>+7.2f}%"
            f"  {r['win_rate']:>6.1f}%  {r['force_close_pct']:>4.0f}%"
            f"  {r['max_dd']:>5.2f}%  {r['bench_pct']:>+7.2f}%  {r['alpha']:>+6.2f}%{marker}"
        )
    print("=" * 75)

    best = max(results, key=lambda x: x["alpha"])
    print(f"\n  Best alpha: {best['multiplier']:.1f}× → α={best['alpha']:+.2f}%  (P&L={best['pnl_pct']:+.2f}%  FC={best['force_close_pct']:.0f}%)")
    if best["multiplier"] != config.ATR_TP_MULT_DEFAULT:
        print(f"\n  Tip: update ATR_TP_MULT_DEFAULT in config.py to {best['multiplier']}")
        print(f"       or add \"atr_tp_multiplier\": {best['multiplier']} to data/state/trading_params.json")


if __name__ == "__main__":
    main()
