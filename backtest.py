"""
Backtest: last 7 trading days (Apr 14–22, 2026), starting with $100,000.
Uses 5-min bars from Alpaca (IEX feed). No Claude API calls — pure indicator logic.

Signal at bar T  →  entry at bar T+1 open price  (no look-ahead bias)
SL = -2%, TP = +5%, max 5 concurrent positions, max 5% of portfolio per position
0.05% slippage on entry, 0.05% slippage on exit. One entry per stock per day.
Force-close at first bar whose timestamp >= 15:55 ET.
"""

import logging
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

import config
from indicators.engine import compute_indicators
from utils.constants import ET
from utils.io_utils import save_json

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ── Parameters ──────────────────────────────────────────────────────────────
TRADING_DAYS = [
    datetime(2026, 4, 14, tzinfo=ET),
    datetime(2026, 4, 15, tzinfo=ET),
    datetime(2026, 4, 16, tzinfo=ET),
    datetime(2026, 4, 17, tzinfo=ET),
    datetime(2026, 4, 18, tzinfo=ET),
    datetime(2026, 4, 21, tzinfo=ET),
    datetime(2026, 4, 22, tzinfo=ET),
]
STARTING_CAPITAL = 100_000.0
SLIPPAGE_PCT     = 0.0005      # 0.05% each way
STOP_LOSS_PCT    = 4.0
TAKE_PROFIT_PCT  = 5.0
MAX_POSITIONS    = 5
MAX_POSITION_PCT = 0.05
RISK_PER_TRADE   = 0.005       # 0.5% of portfolio ATR-sized (matches live)
MIN_COMPOSITE    = 15.0        # same as live BUY threshold
MAX_DAILY_LOSS   = 0.03        # halt new entries if down >3% on the day
RSI_BUY_MAX      = 45.0        # only buy when RSI is not overbought (normal regime)
RSI_BUY_MAX_HV   = 35.0        # tighter RSI when VIX elevated
VWAP_SLOPE_MIN   = -0.3        # skip BUY if VWAP trending down by this % over 10 bars
ENTRY_START_H    = 10          # no new entries before 10:00 AM ET
ENTRY_END_H      = 15          # no new entries at/after 3:00 PM ET
HIGH_VOL_MULT    = 0.5         # halve position size in HIGH_VOL/CRISIS
MARKET_OPEN_H    = 9
MARKET_OPEN_M    = 30
FORCE_CLOSE_H    = 15
FORCE_CLOSE_M    = 55
LOOKBACK_BARS    = 100         # bars fed to indicator engine
WATCH_LIST_SIZE  = 25          # top N volatile tickers each day


# ── Data structures ──────────────────────────────────────────────────────────

@dataclass
class Trade:
    symbol: str
    date: str
    entry_ts: str
    exit_ts: str
    entry_price: float
    exit_price: float
    qty: float
    exit_reason: str           # "TP", "SL", "FORCE_CLOSE", "EOD"
    pnl: float
    pnl_pct: float


@dataclass
class SimState:
    cash: float = STARTING_CAPITAL
    positions: dict = field(default_factory=dict)   # symbol → {qty, entry, sl, tp, entry_ts}
    daily_closed: set = field(default_factory=set)
    equity_curve: list = field(default_factory=list)
    trades: list = field(default_factory=list)


# ── Alpaca data fetch ────────────────────────────────────────────────────────

def _fetch_vix_regime_for_day(day: datetime) -> str:
    """
    Classify the VIX regime for a given backtest day.
    Returns "NORMAL", "HIGH_VOL", or "CRISIS".
    """
    try:
        def _yahoo_pairs(symbol: str, range_str: str) -> list[tuple[int, float]]:
            """Return [(unix_ts, close), ...] with Nones dropped, timestamps aligned."""
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range={range_str}"
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            result = r.json()["chart"]["result"][0]
            ts_raw = result["timestamp"]
            cl_raw = result["indicators"]["quote"][0]["close"]
            return [(t, c) for t, c in zip(ts_raw, cl_raw) if c is not None]

        vix_pairs = _yahoo_pairs("%5EVIX", "120d")
        spy_pairs = _yahoo_pairs("SPY",    "10d")

        # Use the last VIX close whose date < market open of `day`
        day_ts = day.timestamp()
        past_vix = [(t, c) for t, c in vix_pairs if t < day_ts]
        if not past_vix:
            return "NORMAL"

        _, vix_current = past_vix[-1]
        _, vix_prev    = past_vix[-2] if len(past_vix) >= 2 else past_vix[-1], vix_current
        vix_rising     = vix_current > vix_prev

        lookback    = min(90, len(past_vix) - 1)
        vix_avg_90d = sum(c for _, c in past_vix[-lookback - 1 : -1]) / lookback if lookback > 0 else vix_current

        past_spy = [(t, c) for t, c in spy_pairs if t < day_ts]
        spy_change = 0.0
        if len(past_spy) >= 2:
            spy_change = (past_spy[-1][1] - past_spy[-2][1]) / past_spy[-2][1] * 100

        if vix_current > vix_avg_90d and vix_rising and spy_change < -1.0:
            return "CRISIS"
        elif vix_current > vix_avg_90d:
            return "HIGH_VOL"
        return "NORMAL"
    except Exception as exc:
        logger.warning(f"VIX regime fetch failed for {day.date()}: {exc} — using NORMAL")
        return "NORMAL"


def _fetch_5min_bars(symbols: list[str], start: datetime, end: datetime) -> dict[str, pd.DataFrame]:
    """Fetch 5-min bars for all symbols in one batch request, return {symbol: df}."""
    client = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)
    req = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame(5, TimeFrameUnit.Minute),
        start=start,
        end=end,
        feed="iex",
    )
    bars = client.get_stock_bars(req)
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
    else:
        # single symbol returned
        if symbols:
            result[symbols[0]] = df_all
    return result


def _fetch_daily_bars(symbols: list[str], start: datetime, end: datetime) -> dict[str, pd.DataFrame]:
    """Fetch daily bars for volatility ranking."""
    client = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)
    req = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        feed="iex",
    )
    bars = client.get_stock_bars(req)
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
    else:
        if symbols:
            result[symbols[0]] = df_all
    return result


# ── Watch-list selection (volatility) ───────────────────────────────────────

def _select_watch_list(all_tickers: list[str], prior_day: datetime, n: int = WATCH_LIST_SIZE) -> list[str]:
    """
    Rank tickers by prior-day volatility (ATR as % of close).
    Returns top N. Falls back to full list if data sparse.
    """
    logger.info(f"  Fetching daily bars for volatility ranking ({len(all_tickers)} tickers)…")
    start = prior_day - timedelta(days=20)
    daily = _fetch_daily_bars(all_tickers, start, prior_day + timedelta(days=1))

    scores = {}
    for sym, df in daily.items():
        if len(df) < 5:
            continue
        prev_close = df["close"].shift(1)
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = tr.ewm(com=13, min_periods=5).mean().iloc[-1]
        price = df["close"].iloc[-1]
        scores[sym] = atr / price if price > 0 else 0.0

    ranked = sorted(scores, key=lambda s: scores[s], reverse=True)
    watch = ranked[:n] if len(ranked) >= n else list(scores.keys())
    logger.info(f"  Watch list ({len(watch)}): {', '.join(watch[:10])}{'…' if len(watch) > 10 else ''}")
    return watch


# ── Position sizing ──────────────────────────────────────────────────────────

def _calc_qty(portfolio_value: float, price: float, atr: float) -> float:
    if price <= 0:
        return 0.0
    risk_dollars = portfolio_value * RISK_PER_TRADE
    sl_dollars = price * STOP_LOSS_PCT / 100
    if sl_dollars <= 0:
        sl_dollars = atr if atr > 0 else price * 0.02
    qty_risk = risk_dollars / sl_dollars
    qty_cap = (portfolio_value * MAX_POSITION_PCT) / price
    qty = min(qty_risk, qty_cap)
    return max(1.0, round(qty))


# ── Trade execution helpers ──────────────────────────────────────────────────

def _entry_price(bar_open: float) -> float:
    return round(bar_open * (1 + SLIPPAGE_PCT), 4)


def _exit_price(price: float, side: str = "sell") -> float:
    mult = (1 - SLIPPAGE_PCT) if side == "sell" else (1 + SLIPPAGE_PCT)
    return round(price * mult, 4)


def _pos_value_at(state: SimState, day_bars: dict, ts) -> float:
    """MTM value of all open positions, using last known close if no bar at ts."""
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


def _open_trade(state: SimState, symbol: str, ts: str, bar_open: float, atr: float, pos_mult: float = 1.0) -> None:
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
    state.positions[symbol] = {"qty": qty, "entry": entry, "sl": sl, "tp": tp, "entry_ts": ts}
    logger.debug(f"    OPEN  {symbol:5s} x{qty:.0f} @ ${entry:.2f}  SL=${sl:.2f} TP=${tp:.2f}")


def _close_trade(state: SimState, symbol: str, exit_ts: str, exit_raw: float, reason: str, date_str: str) -> None:
    pos = state.positions.pop(symbol)
    ep = _exit_price(exit_raw)
    proceeds = ep * pos["qty"]
    state.cash += proceeds
    pnl = proceeds - pos["entry"] * pos["qty"]
    pnl_pct = (ep / pos["entry"] - 1) * 100
    state.trades.append(Trade(
        symbol=symbol,
        date=date_str,
        entry_ts=pos["entry_ts"],
        exit_ts=exit_ts,
        entry_price=pos["entry"],
        exit_price=ep,
        qty=pos["qty"],
        exit_reason=reason,
        pnl=round(pnl, 2),
        pnl_pct=round(pnl_pct, 2),
    ))
    state.daily_closed.add(symbol)
    logger.debug(f"    CLOSE {symbol:5s} x{pos['qty']:.0f} @ ${ep:.2f}  PnL=${pnl:+.2f} ({pnl_pct:+.2f}%)  [{reason}]")


# ── Single-day simulation ────────────────────────────────────────────────────

def _simulate_day(
    day: datetime,
    watch_list: list[str],
    all_bars: dict[str, pd.DataFrame],
    state: SimState,
    regime: str = "NORMAL",
) -> None:
    date_str = day.strftime("%Y-%m-%d")
    rsi_max  = RSI_BUY_MAX_HV if regime in ("HIGH_VOL", "CRISIS") else RSI_BUY_MAX
    pos_mult = HIGH_VOL_MULT  if regime in ("HIGH_VOL", "CRISIS") else 1.0
    logger.info(f"\n=== {date_str} === regime={regime} rsi_max={rsi_max} pos_mult={pos_mult}")
    state.daily_closed.clear()

    mkt_open      = day.replace(hour=MARKET_OPEN_H, minute=MARKET_OPEN_M, second=0, microsecond=0)
    force_ts      = day.replace(hour=FORCE_CLOSE_H, minute=FORCE_CLOSE_M, second=0, microsecond=0)
    entry_start   = day.replace(hour=ENTRY_START_H, minute=0, second=0, microsecond=0)
    entry_end     = day.replace(hour=ENTRY_END_H,   minute=0, second=0, microsecond=0)
    day_start_equity = state.cash
    halted = False

    # CRISIS: no new entries at all
    if regime == "CRISIS":
        logger.info("  CRISIS regime — skipping all new entries today.")
        halted = True

    # Collect all bar timestamps for today across watch-list symbols
    all_ts: set[datetime] = set()
    day_bars: dict[str, pd.DataFrame] = {}
    for sym in watch_list:
        df = all_bars.get(sym)
        if df is None or df.empty:
            continue
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        df_day = df[(df.index >= mkt_open.astimezone(ZoneInfo("UTC"))) &
                    (df.index <  (day + timedelta(days=1)).astimezone(ZoneInfo("UTC")))]
        if df_day.empty:
            continue
        day_bars[sym] = df_day
        all_ts.update(df_day.index.tolist())

    sorted_ts = sorted(all_ts)
    if not sorted_ts:
        logger.info("  No bars for this day — skipping.")
        return

    # Signals computed at bar T are cached here; acted on at bar T+1
    pending_signals: dict[str, dict] = {}   # symbol → {score, atr}

    for i, ts in enumerate(sorted_ts):
        ts_et = ts.astimezone(ET)
        is_force = ts_et >= force_ts

        # ── Check SL/TP and force-close for open positions ──────────────────
        for sym in list(state.positions.keys()):
            df_sym = day_bars.get(sym)
            if df_sym is None or ts not in df_sym.index:
                continue
            bar = df_sym.loc[ts]
            pos = state.positions[sym]
            reason = None
            exit_price = None

            # TP hit (check high)
            if bar["high"] >= pos["tp"]:
                reason, exit_price = "TP", pos["tp"]
            # SL hit (check low)
            elif bar["low"] <= pos["sl"]:
                reason, exit_price = "SL", pos["sl"]
            # Force close
            elif is_force:
                reason, exit_price = "FORCE_CLOSE", bar["close"]

            if reason:
                _close_trade(state, sym, str(ts), exit_price, reason, date_str)

        # Skip opening new positions at or after force-close time
        if is_force:
            continue

        # ── Daily loss circuit-breaker ───────────────────────────────────────
        if not halted:
            current_equity = state.cash + _pos_value_at(state, day_bars, ts)
            daily_loss_pct = (current_equity - day_start_equity) / day_start_equity
            if daily_loss_pct <= -MAX_DAILY_LOSS:
                logger.info(f"    [CIRCUIT BREAKER] Daily loss {daily_loss_pct*100:.2f}% — halting new entries for {date_str}")
                halted = True
                pending_signals.clear()

        # ── Act on signals from the PREVIOUS bar (no look-ahead) ────────────
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
                _open_trade(state, sym, str(ts), bar_open, sig["atr"], pos_mult)
                n_open += 1
            pending_signals.clear()

        # ── Compute indicators at this bar ───────────────────────────────────
        for sym in watch_list:
            if sym in state.positions or sym in state.daily_closed:
                continue
            df_sym = all_bars.get(sym)
            if df_sym is None or df_sym.empty:
                continue
            if df_sym.index.tz is None:
                df_sym.index = df_sym.index.tz_localize("UTC")
            # Feed only bars up to and including this timestamp (no look-ahead)
            df_window = df_sym[df_sym.index <= ts].tail(LOOKBACK_BARS)
            if len(df_window) < 30:
                continue

            iv = compute_indicators(sym, df_window, as_of=ts_et)
            if iv is None:
                continue

            if (
                iv.composite_score > MIN_COMPOSITE
                and iv.signal == "BUY"
                and iv.rsi <= rsi_max
                and iv.vwap_slope >= VWAP_SLOPE_MIN
            ):
                pending_signals[sym] = {"score": iv.composite_score, "atr": iv.atr}

        # ── Snapshot equity at end of each bar ──────────────────────────────
        equity = state.cash + _pos_value_at(state, day_bars, ts)
        state.equity_curve.append({"ts": str(ts), "equity": round(equity, 2)})

    # EOD: close any leftover positions at last bar's close
    for sym in list(state.positions.keys()):
        df_sym = day_bars.get(sym)
        last_price = (
            float(df_sym["close"].iloc[-1]) if df_sym is not None and not df_sym.empty
            else state.positions[sym]["entry"]
        )
        _close_trade(state, sym, str(sorted_ts[-1]) + "_EOD", last_price, "EOD", date_str)

    portfolio_value = state.cash + sum(
        pos["qty"] * pos["entry"] for pos in state.positions.values()
    )
    logger.info(f"  End of day equity ≈ ${state.cash:,.2f} cash  (portfolio ~${portfolio_value:,.2f})")


# ── Reporting ────────────────────────────────────────────────────────────────

def _print_report(state: SimState) -> None:
    trades = state.trades
    if not trades:
        print("\nNo trades executed.")
        return

    df = pd.DataFrame([asdict(t) for t in trades])
    total_pnl = df["pnl"].sum()
    n = len(df)
    wins = (df["pnl"] > 0).sum()
    losses = (df["pnl"] < 0).sum()
    win_rate = wins / n * 100 if n else 0.0
    avg_win  = df.loc[df["pnl"] > 0, "pnl"].mean() if wins else 0.0
    avg_loss = df.loc[df["pnl"] < 0, "pnl"].mean() if losses else 0.0

    equity_vals = [e["equity"] for e in state.equity_curve]
    peak = STARTING_CAPITAL
    max_dd = 0.0
    for eq in equity_vals:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100
        if dd > max_dd:
            max_dd = dd

    final_equity = equity_vals[-1] if equity_vals else STARTING_CAPITAL + total_pnl
    total_return_pct = (final_equity / STARTING_CAPITAL - 1) * 100

    print("\n" + "=" * 70)
    print("  BACKTEST RESULTS  —  Apr 14-22, 2026  —  $100,000 starting capital")
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

    print("\n── Daily P&L ──────────────────────────────────────────────────────")
    for date, grp in df.groupby("date"):
        daily_pnl = grp["pnl"].sum()
        print(f"  {date}   {len(grp):>2} trades   ${daily_pnl:>+9,.2f}")

    print("\n── Top 10 trades by P&L ───────────────────────────────────────────")
    top10 = df.nlargest(10, "pnl")[["date", "symbol", "qty", "entry_price", "exit_price", "pnl", "pnl_pct", "exit_reason"]]
    print(top10.to_string(index=False))

    print("\n── Bottom 10 trades by P&L ────────────────────────────────────────")
    bot10 = df.nsmallest(10, "pnl")[["date", "symbol", "qty", "entry_price", "exit_price", "pnl", "pnl_pct", "exit_reason"]]
    print(bot10.to_string(index=False))

    print("\n── Per-symbol summary ─────────────────────────────────────────────")
    sym_summary = (
        df.groupby("symbol")
        .agg(trades=("pnl", "count"), total_pnl=("pnl", "sum"), win_rate=("pnl", lambda x: (x > 0).mean() * 100))
        .sort_values("total_pnl", ascending=False)
    )
    print(sym_summary.head(20).to_string())
    print("=" * 70)

    # Save detailed trade log
    out_path = config.BASE_DIR / "backtest_trades.csv"
    df.to_csv(out_path, index=False)
    print(f"\n  Trade log saved to: {out_path}")

    equity_path = config.BASE_DIR / "backtest_equity.json"
    save_json(equity_path, state.equity_curve)
    print(f"  Equity curve saved to: {equity_path}")


# ── Main ─────────────────────────────────────────────────────────────────────

def run_backtest() -> None:
    logger.info("Loading tickers…")
    with open(config.TICKERS_FILE) as f:
        all_tickers = [line.strip().upper() for line in f if line.strip()]
    logger.info(f"  {len(all_tickers)} tickers loaded.")

    # Fetch all 5-min bars for the entire backtest window in one batch
    fetch_start = TRADING_DAYS[0] - timedelta(days=5)   # extra buffer for lookback
    fetch_end   = TRADING_DAYS[-1] + timedelta(days=1)
    logger.info(f"Fetching 5-min bars {fetch_start.date()} → {fetch_end.date()} for all {len(all_tickers)} tickers…")
    logger.info("  (This may take 30–90 seconds — Alpaca batch request)")
    all_bars = _fetch_5min_bars(all_tickers, fetch_start, fetch_end)
    logger.info(f"  Bars received for {len(all_bars)} symbols.")

    # Fetch VIX regime once per day (one HTTP call, cached in dict)
    logger.info("Fetching VIX regime classification for each trading day…")
    vix_regimes: dict[str, str] = {}
    for day in TRADING_DAYS:
        vix_regimes[day.strftime("%Y-%m-%d")] = _fetch_vix_regime_for_day(day)
        logger.info(f"  {day.date()}: regime={vix_regimes[day.strftime('%Y-%m-%d')]}")

    state = SimState()

    for day in TRADING_DAYS:
        prior_day = day - timedelta(days=1)
        regime = vix_regimes[day.strftime("%Y-%m-%d")]
        watch_list = _select_watch_list(all_tickers, prior_day)
        watch_list = [s for s in watch_list if s in all_bars]
        if not watch_list:
            logger.warning(f"  {day.date()}: no watch-list symbols with data — skipping day.")
            continue
        _simulate_day(day, watch_list, all_bars, state, regime=regime)

    _print_report(state)


if __name__ == "__main__":
    run_backtest()
