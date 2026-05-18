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
    """Return total % return of equal-weight buy-at-open/sell-at-close
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
