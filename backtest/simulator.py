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
from indicators.precompute import precompute_indicators_for_day
from signals.atr_tp import compute_atr_tp_pct
from utils.constants import ET

logger = logging.getLogger(__name__)

# ── Backtest-only simulation constants ───────────────────────────────────────
STARTING_CAPITAL    = config.BACKTEST_STARTING_CAPITAL
SLIPPAGE_PCT        = config.BACKTEST_SLIPPAGE_PCT
MAX_POSITIONS       = config.BACKTEST_MAX_POSITIONS
MARKET_OPEN_H       = config.MARKET_OPEN_HOUR
MARKET_OPEN_M       = config.MARKET_OPEN_MINUTE
FORCE_CLOSE_H       = config.EOD_FORCE_CLOSE_HOUR
FORCE_CLOSE_M       = config.EOD_FORCE_CLOSE_MINUTE
SWING_EVAL_H        = config.EOD_HOLDS_EVAL_HOUR
SWING_EVAL_M        = config.EOD_HOLDS_EVAL_MINUTE
SWING_ENABLED       = True
SWING_COMPOSITE_MIN = config.BACKTEST_SWING_COMPOSITE_MIN
SWING_SL_PCT        = config.SWING_OVERNIGHT_SL_PCT
SWING_TP_PCT        = config.SWING_TP2_PCT
# ── Live-bot params — single source of truth is config.py ────────────────────
STOP_LOSS_PCT = config.STOP_LOSS_PCT
MIN_COMPOSITE = config.COMPOSITE_MIN_NORMAL; MAX_DAILY_LOSS = config.MAX_DAILY_LOSS_PCT
RSI_BUY_MAX   = config.RSI_BUY_MAX;    RSI_BUY_MAX_HV   = config.RSI_BUY_MAX_HIGH_VOL
VWAP_SLOPE_MIN = config.VWAP_SLOPE_MIN; ENTRY_START_H   = config.ENTRY_START_HOUR
ENTRY_END_H   = config.ENTRY_END_HOUR;  HIGH_VOL_MULT    = config.HIGH_VOL_POSITION_MULT
MAX_POSITION_PCT = config.MAX_POSITION_PCT; RISK_PER_TRADE = config.RISK_PER_TRADE_PCT
LOOKBACK_BARS = config.BARS_LOOKBACK;   SWING_MAX_POSITIONS = config.SWING_MAX_POSITIONS
SWING_MAX_DAYS = config.BACKTEST_SWING_MAX_DAYS


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
    atr_tp_multiplier: float = config.ATR_TP_MULT_DEFAULT
    sl_blacklist: set = field(default_factory=set)
    sl_blacklist_pending: set = field(default_factory=set)


def _calc_qty(portfolio_value: float, price: float, atr: float) -> float:
    if price <= 0:
        return 0.0
    sl_dollars = price * STOP_LOSS_PCT / 100 or (atr if atr > 0 else price * config.ATR_FALLBACK_PROXY_PCT)
    qty_risk = portfolio_value * RISK_PER_TRADE / sl_dollars
    return max(1.0, round(min(qty_risk, portfolio_value * MAX_POSITION_PCT / price)))


def _entry_price(bar_open: float) -> float:
    return round(bar_open * (1 + SLIPPAGE_PCT), 4)


def _exit_price(price: float, side: str = "sell") -> float:
    mult = (1 - SLIPPAGE_PCT) if side == "sell" else (1 + SLIPPAGE_PCT)
    return round(price * mult, 4)


def _pos_value_at(state: SimState, day_bars: dict, ts) -> float:
    def _px(sym, p):
        df = day_bars.get(sym)
        avail = df[df.index <= ts] if df is not None and not df.empty else None
        return float(avail["close"].iloc[-1]) if avail is not None and not avail.empty else p["entry"]
    return sum(p["qty"] * _px(sym, p) for sym, p in state.positions.items())


def _open_trade(
    state: SimState, symbol: str, ts: str, bar_open: float,
    atr: float, pos_mult: float = 1.0, composite: float = 0.0,
) -> None:
    portfolio_value = state.cash + sum(p["qty"] * p["entry"] for p in state.positions.values())
    entry = _entry_price(bar_open)
    qty = max(1.0, round(_calc_qty(portfolio_value, entry, atr) * pos_mult))
    cost = entry * qty
    if cost > state.cash:
        qty = max(1.0, round(state.cash / entry))
        cost = entry * qty
    if qty < 1 or cost > state.cash:
        return
    sl = round(entry * (1 - STOP_LOSS_PCT / 100), 4)
    tp_pct = compute_atr_tp_pct(atr, entry, state.atr_tp_multiplier)
    tp = round(entry * (1 + tp_pct / 100), 4)
    state.cash -= cost
    state.positions[symbol] = {
        "qty": qty, "entry": entry, "sl": sl, "tp": tp,
        "entry_ts": ts, "entry_time": ts, "composite": composite,
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
    logger.debug(f"    CLOSE {symbol:5s} x{pos['qty']:.0f} @ ${ep:.2f}  PnL=${pnl:+.2f} ({pnl_pct:+.2f}%)  [{reason}]")


def _evaluate_swing_promotion(
    state: SimState, day_bars: dict, ts, regime: str, n_swings: int
) -> int:
    for sym2 in list(state.positions.keys()):
        pos2 = state.positions[sym2]
        if pos2.get("is_swing"):
            continue
        df2 = day_bars.get(sym2)
        if df2 is None or df2.empty: continue
        avail2 = df2[df2.index <= ts]
        eod_price = float(avail2["close"].iloc[-1]) if not avail2.empty else pos2["entry"]
        pnl_pct = (eod_price - pos2["entry"]) / pos2["entry"] * 100
        if regime in ("NORMAL", "CRASH_RECOVERY") and pnl_pct > 0 and pos2.get("composite", 0) >= SWING_COMPOSITE_MIN and n_swings < SWING_MAX_POSITIONS:
            pos2["is_swing"] = True
            pos2["swing_days_remaining"] = SWING_MAX_DAYS
            pos2["sl"] = round(pos2["entry"] * (1 - SWING_SL_PCT / 100), 4)
            pos2["tp"] = round(pos2["entry"] * (1 + SWING_TP_PCT / 100), 4)
            n_swings += 1
            logger.info(f"    SWING PROMOTE {sym2}: P&L={pnl_pct:+.2f}% composite={pos2['composite']:.0f} | SL={pos2['sl']:.2f} TP={pos2['tp']:.2f}")
    return n_swings


def _eod_manage_positions(
    state: SimState, day_bars: dict, sorted_ts: list, date_str: str
) -> None:
    for sym in list(state.positions.keys()):
        pos = state.positions[sym]
        df_sym = day_bars.get(sym)
        last_price = float(df_sym["close"].iloc[-1]) if df_sym is not None and not df_sym.empty else pos["entry"]
        if pos.get("is_swing"):
            pos["swing_days_remaining"] -= 1
            pnl_pct = (last_price / pos["entry"] - 1) * 100
            if pos["swing_days_remaining"] <= 0:
                _close_trade(state, sym, str(sorted_ts[-1]) + "_EOD", last_price, "SWING_MAXDAYS", date_str)
            else:
                logger.info(f"  SWING CARRY {sym}: {pos['swing_days_remaining']}d left | P&L={pnl_pct:+.2f}%")
        else:
            _close_trade(state, sym, str(sorted_ts[-1]) + "_EOD", last_price, "EOD", date_str)


def _build_day_bars(
    watch_list: list[str], all_bars: dict, mkt_open: datetime, day: datetime
) -> tuple[dict, list]:
    all_ts: set[datetime] = set()
    day_bars: dict[str, pd.DataFrame] = {}
    utc_open = mkt_open.astimezone(ZoneInfo("UTC"))
    utc_end  = (day + timedelta(days=1)).astimezone(ZoneInfo("UTC"))
    for sym in watch_list:
        df = all_bars.get(sym)
        if df is None or df.empty: continue
        if df.index.tz is None: df.index = df.index.tz_localize("UTC")
        df_day = df[(df.index >= utc_open) & (df.index < utc_end)]
        if df_day.empty: continue
        day_bars[sym] = df_day
        all_ts.update(df_day.index.tolist())
    return day_bars, sorted(all_ts)


def _execute_pending_signals(
    state: SimState, pending: dict, day_bars: dict, ts, pos_mult: float
) -> None:
    n_open = len(state.positions)
    for sym, sig in list(pending.items()):
        if n_open >= MAX_POSITIONS:
            break
        if sym in state.positions or sym in state.daily_closed:
            continue
        df_sym = day_bars.get(sym)
        if df_sym is None or ts not in df_sym.index:
            continue
        _open_trade(state, sym, str(ts), float(df_sym.loc[ts, "open"]), sig["atr"], pos_mult, sig["score"])
        n_open += 1
    pending.clear()


def _sim_tranche2_gates_pass(
    pos: dict,
    iv,
    drop_pct: float,
    regime: str,
    now,
) -> bool:
    """Simplified T2 gate check for per-bar simulation — no earnings/sentiment data available."""
    if pos.get("tranche_2_triggered"):
        return False
    if now.hour >= config.TRANCHE2_CUTOFF_HOUR:
        return False
    if regime in ("CRISIS", "CHOPPY"):
        return False
    if drop_pct < config.TRANCHE2_DROP_MIN_PCT or drop_pct > config.TRANCHE2_DROP_MAX_PCT:
        return False
    if getattr(iv, "rsi", 100) > config.TRANCHE2_RSI_MAX:
        return False
    if getattr(iv, "composite_score", 0) < config.TRANCHE2_COMPOSITE_MIN:
        return False
    try:
        from datetime import datetime as _dt
        entry_dt = _dt.fromisoformat(pos["entry_time"])
        elapsed_min = (now - entry_dt).total_seconds() / 60
        if elapsed_min < config.TRANCHE2_MIN_HOLD_MIN:
            return False
    except Exception:
        pass
    return True


def _sim_apply_tranche2(pos: dict, bar_low: float, t1_qty: int, portfolio_value: float) -> None:
    """Update position dict in-place with blended T2 values. Mutates pos."""
    t1_entry = pos.get("tranche_1_entry", pos.get("entry", 0))
    price    = bar_low

    risk_amount    = portfolio_value * config.RISK_PER_TRADE_PCT
    atr_proxy      = price * config.ATR_FALLBACK_PROXY_PCT
    shares_by_risk = risk_amount / atr_proxy if atr_proxy > 0 else 1
    shares_by_pct  = (portfolio_value * config.MAX_POSITION_PCT) / price if price > 0 else 1
    full_qty       = max(1, int(min(shares_by_risk, shares_by_pct)))
    t2_qty         = max(1, int(full_qty * config.TRANCHE2_SIZE_PCT))

    combined_qty  = t1_qty + t2_qty
    blended_entry = (t1_qty * t1_entry + t2_qty * price) / combined_qty
    blended_sl    = price * (1 - config.TRANCHE2_SL_PCT / 100)

    pos["tranche_2_triggered"] = True
    pos["tranche_2_qty"]       = t2_qty
    pos["qty"]                 = combined_qty
    pos["entry"]               = round(blended_entry, 4)
    pos["sl"]                  = round(blended_sl, 4)


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
    # Promote yesterday's SL hits to active blacklist for today
    state.sl_blacklist = set(state.sl_blacklist_pending)
    state.sl_blacklist_pending.clear()

    def _ts(h, m=0): return day.replace(hour=h, minute=m, second=0, microsecond=0)
    mkt_open      = _ts(MARKET_OPEN_H, MARKET_OPEN_M)
    force_ts      = _ts(FORCE_CLOSE_H, FORCE_CLOSE_M)
    swing_eval_ts = _ts(SWING_EVAL_H,  SWING_EVAL_M)
    entry_start   = _ts(ENTRY_START_H); entry_end = _ts(ENTRY_END_H)
    day_start_equity = state.cash
    halted = False
    swing_evaluated = False

    if regime == "CRISIS":
        logger.info("  CRISIS regime — skipping all new entries today.")
        halted = True

    day_bars, sorted_ts = _build_day_bars(watch_list, all_bars, mkt_open, day)
    if not sorted_ts:
        logger.info("  No bars for this day — skipping.")
        return

    # Precompute indicators for all watch-list symbols for this day (vectorized, ~60× faster)
    precomp: dict[str, dict] = {}
    for sym in watch_list:
        df_sym = all_bars.get(sym)
        if df_sym is not None and not df_sym.empty:
            precomp[sym] = precompute_indicators_for_day(sym, df_sym, day)

    pending_signals: dict[str, dict] = {}

    for i, ts in enumerate(sorted_ts):
        ts_et = ts.astimezone(ET)
        is_force = ts_et >= force_ts

        if SWING_ENABLED and not swing_evaluated and ts_et >= swing_eval_ts:
            swing_evaluated = True
            n_swings = sum(1 for p in state.positions.values() if p.get("is_swing"))
            _evaluate_swing_promotion(state, day_bars, ts, regime, n_swings)

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
                reason, exit_price = ("SWING_TP" if is_sw else "TP"), pos["tp"]
            elif bar["low"] <= pos["sl"]:
                reason, exit_price = ("SWING_SL" if is_sw else "SL"), pos["sl"]
            elif is_force and not is_sw:
                reason, exit_price = "FORCE_CLOSE", bar["close"]

            if reason:
                _close_trade(state, sym, str(ts), exit_price, reason, date_str)
                if reason in ("SL", "SWING_SL"):
                    state.sl_blacklist_pending.add(sym)
            # T2 add-on check — use bar["low"] as worst intrabar price proxy
            elif not pos.get("tranche_2_triggered") and not is_sw:
                t1_entry = pos.get("tranche_1_entry", pos.get("entry", 0))
                if t1_entry > 0:
                    drop_pct = (t1_entry - float(bar["low"])) / t1_entry * 100
                    iv_at_bar = precomp.get(sym, {}).get(ts)
                    if iv_at_bar is None:
                        iv_at_bar = type("_IV", (), {"rsi": 100, "composite_score": 0})()
                    if _sim_tranche2_gates_pass(pos, iv_at_bar, drop_pct, regime, ts_et):
                        current_portfolio = state.cash + _pos_value_at(state, day_bars, ts)
                        _sim_apply_tranche2(pos, float(bar["low"]), int(pos.get("qty", 1)), current_portfolio)

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
            _execute_pending_signals(state, pending_signals, day_bars, ts, pos_mult)

        for sym in watch_list:
            if sym in state.positions or sym in state.daily_closed: continue
            if sym in state.sl_blacklist: continue
            iv = precomp.get(sym, {}).get(ts)
            if iv is None: continue
            if iv.composite_score > MIN_COMPOSITE and iv.signal == "BUY" and iv.rsi <= rsi_max and iv.vwap_slope >= VWAP_SLOPE_MIN:
                if ts_et.hour >= config.ENTRY_LATE_HOUR and iv.composite_score < config.ENTRY_LATE_COMPOSITE_MIN:
                    continue
                pending_signals[sym] = {"score": iv.composite_score, "atr": iv.atr}

        equity = state.cash + _pos_value_at(state, day_bars, ts)
        state.equity_curve.append({"ts": str(ts), "equity": round(equity, 2)})

    _eod_manage_positions(state, day_bars, sorted_ts, date_str)

    portfolio_value = state.cash + sum(p["qty"] * p["entry"] for p in state.positions.values())
    logger.info(f"  End of day equity ≈ ${state.cash:,.2f} cash  (portfolio ~${portfolio_value:,.2f})")
