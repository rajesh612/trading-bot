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
