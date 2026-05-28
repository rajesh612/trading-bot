"""
Pure simulation engine: SimState, Trade, and simulate_day.
No network I/O, no file I/O — safe to call from tests without side effects.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import cached_property
from zoneinfo import ZoneInfo

import pandas as pd

import config
from indicators.precompute import precompute_indicators_for_day
from signals.atr_tp import compute_atr_tp_pct
from signals.entry_gates import effective_composite_gate
from trading.tiered_tp import evaluate_tiered_tp_action
from utils.constants import ET
from utils.regime_params import get_swing_params, get_tp_sl_params, get_entry_params, get_t2_params, get_filter_params, get_gate_params, get_swing_gate_params
from signals.tp_scaler import compute_tp_sl as _compute_tp_sl
from utils.ticker_quality import TickerQuality, compute_ticker_quality

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
# ── Live-bot params — single source of truth is config.py ────────────────────
STOP_LOSS_PCT = config.STOP_LOSS_PCT
MAX_DAILY_LOSS = config.MAX_DAILY_LOSS_PCT
RSI_BUY_MAX   = config.RSI_BUY_MAX;    RSI_BUY_MAX_HV   = config.RSI_BUY_MAX_HIGH_VOL
VWAP_SLOPE_MIN = config.VWAP_SLOPE_MIN; ENTRY_START_H   = config.ENTRY_START_HOUR
HIGH_VOL_MULT    = config.HIGH_VOL_POSITION_MULT
MAX_POSITION_PCT = config.MAX_POSITION_PCT; RISK_PER_TRADE = config.RISK_PER_TRADE_PCT
LOOKBACK_BARS = config.BARS_LOOKBACK;   SWING_MAX_POSITIONS = config.SWING_MAX_POSITIONS


_REGIME_SENTIMENT_PRIOR: dict[str, int] = {
    "BULL_TREND":     40,
    "OPPORTUNITY":    35,
    "NORMAL":         15,
    "CRASH_RECOVERY": 10,
    "DEFENSIVE":       0,
    "CHOPPY":         -5,
    "HIGH_VOL":      -10,
    "CAPITULATION":  -20,
    "CRISIS":        -40,
}


@dataclass
class _BacktestSnap:
    """Minimal snap stub for compute_ticker_quality — regime-derived sentiment priors."""
    regime: str
    _watch_list: list = field(default_factory=list)

    @cached_property
    def research(self) -> dict:
        prior = _REGIME_SENTIMENT_PRIOR.get(self.regime, 0)
        return {sym: {"sentiment_score": prior} for sym in self._watch_list}

    @cached_property
    def intraday_news(self) -> dict:
        prior = _REGIME_SENTIMENT_PRIOR.get(self.regime, 0)
        return {sym: {"sentiment_score": prior} for sym in self._watch_list}


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
    t2_fire_count: int = 0


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
    tier: str = "MEDIUM", regime: str = "NORMAL",
    entry_time_et=None,
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
    base_tp_pct = compute_atr_tp_pct(atr, entry, state.atr_tp_multiplier)
    tp_pct, sl_pct = _compute_tp_sl(
        base_tp_pct, STOP_LOSS_PCT, entry_time_et or datetime.now(ET), tier, regime
    )
    sl = round(entry * (1 - sl_pct / 100), 4)
    tp = round(entry * (1 + tp_pct / 100), 4)
    state.cash -= cost
    state.positions[symbol] = {
        "qty": qty, "entry": entry, "sl": sl, "tp": tp,
        "entry_ts": ts, "entry_time": ts, "composite": composite,
        "quality_tier": tier,
    }
    logger.debug(f"    OPEN  {symbol:5s} x{qty:.0f} @ ${entry:.2f}  SL=${sl:.2f} TP=${tp:.2f}  [{tier}]")


def _close_trade(
    state: SimState, symbol: str, exit_ts: str,
    exit_raw: float, reason: str, date_str: str,
    qty: int | None = None,
) -> None:
    pos = state.positions[symbol]
    close_qty = qty if qty is not None else int(pos["qty"])
    ep = _exit_price(exit_raw)
    proceeds = ep * close_qty
    state.cash += proceeds
    pnl = proceeds - pos["entry"] * close_qty
    pnl_pct = (ep / pos["entry"] - 1) * 100
    state.trades.append(Trade(
        symbol=symbol, date=date_str, entry_ts=pos["entry_ts"], exit_ts=exit_ts,
        entry_price=pos["entry"], exit_price=ep, qty=close_qty,
        exit_reason=reason, pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 2),
    ))
    remaining = pos["qty"] - close_qty
    if remaining > 0:
        pos["qty"] = remaining
    else:
        del state.positions[symbol]
        state.daily_closed.add(symbol)
    logger.debug(
        f"    CLOSE {symbol:5s} x{close_qty:.0f} @ ${ep:.2f}  "
        f"PnL=${pnl:+.2f} ({pnl_pct:+.2f}%)  [{reason}]"
    )


def _init_tiered_swing_fields(pos: dict, regime: str) -> None:
    """Initialise 40/30/30 tiered TP fields on a position being promoted to swing."""
    from trading.tiered_tp import _compute_tier_quantities
    tier = pos.get("quality_tier", "MEDIUM")
    p = get_tp_sl_params(regime, tier, "SWING")
    if p.is_disabled:
        p = get_tp_sl_params("NORMAL", tier, "SWING")
    total_qty = int(pos["qty"])
    tp1_qty, tp2_qty, trailing_qty = _compute_tier_quantities(total_qty)
    pos.update({
        "tp1_qty":              tp1_qty,
        "tp2_qty":              tp2_qty,
        "trailing_qty":         trailing_qty,
        "tp1_hit":              False,
        "tp2_hit":              False,
        "trailing_stop_active": False,
        "trailing_high_water":  pos["entry"],
        "entry_price":          pos["entry"],
        "stop_loss_pct":        p.sl_floor_pct,
        "tp1_pct":              p.tp_floor_pct,
        "tp2_pct":              p.tp_ceiling_pct,
    })


def _evaluate_swing_promotion(
    state: SimState, day_bars: dict, ts, regime: str, n_swings: int,
    quality_map: "dict[str, TickerQuality] | None" = None,
    precomp: "dict | None" = None,
) -> int:
    qmap = quality_map or {}
    for sym2 in list(state.positions.keys()):
        pos2 = state.positions[sym2]
        if pos2.get("is_swing"):
            continue
        df2 = day_bars.get(sym2)
        if df2 is None or df2.empty: continue
        avail2 = df2[df2.index <= ts]
        eod_price = float(avail2["close"].iloc[-1]) if not avail2.empty else pos2["entry"]
        pnl_pct = (eod_price - pos2["entry"]) / pos2["entry"] * 100
        tier = pos2.get("quality_tier", "MEDIUM")
        sp = get_swing_params(regime)
        p  = get_tp_sl_params(regime, tier, "SWING")

        if p.is_disabled:
            continue

        sgp = get_swing_gate_params(regime, tier)
        iv_eod = (precomp or {}).get(sym2, {}).get(ts)
        eod_rsi = getattr(iv_eod, "rsi", 100.0) if iv_eod else 100.0

        # Mirror live-bot quality gate: block late-momentum promotions for moderate wins.
        # Strongly profitable positions (>= PROFIT_LOCK_SWING_PCT) bypass the RSI/composite gate.
        if pnl_pct < config.PROFIT_LOCK_SWING_PCT:
            if eod_rsi >= sgp.rsi_ceiling:
                continue
            if pos2.get("composite", 0) < sgp.composite_min:
                continue

        quality = qmap.get(sym2)
        choppy_conviction_ok = (
            regime == "CHOPPY"
            and quality is not None
            and "SWING_CHOPPY" in quality.unlocks
        )
        if not sp.eligible and not choppy_conviction_ok:
            continue

        profit_lock_bypass = (
            pnl_pct >= p.profit_lock_pct
            and regime != "CRISIS"
            and n_swings < SWING_MAX_POSITIONS
        )
        standard_qualify = (
            (sp.eligible or choppy_conviction_ok)
            and pnl_pct >= p.min_pnl_pct
            and n_swings < SWING_MAX_POSITIONS
        )
        if profit_lock_bypass or standard_qualify:
            pos2["is_swing"] = True
            pos2["swing_days_remaining"] = p.max_days
            _init_tiered_swing_fields(pos2, regime)
            pos2["sl"] = round(pos2["entry"] * (1 - pos2["stop_loss_pct"] / 100), 4)
            n_swings += 1
            tag = "PROFIT-LOCK" if profit_lock_bypass else "STANDARD"
            logger.info(
                f"    SWING PROMOTE {sym2} [{tag}]: P&L={pnl_pct:+.2f}% composite={pos2['composite']:.0f}"
                f" | SL={pos2['sl']:.2f} TP1={pos2.get('tp1_pct', 0):.1f}% TP2={pos2.get('tp2_pct', 0):.1f}%"
            )
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
    state: SimState, pending: dict, day_bars: dict, ts, pos_mult: float,
    regime: str = "NORMAL", ts_et=None,
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
        _open_trade(state, sym, str(ts), float(df_sym.loc[ts, "open"]),
                    sig["atr"], pos_mult, sig["score"],
                    tier=sig.get("tier", "MEDIUM"), regime=regime,
                    entry_time_et=ts_et)
        n_open += 1
    pending.clear()


def _sim_tranche2_gates_pass(
    pos: dict,
    iv,
    drop_pct: float,
    regime: str,
    now,
    quality=None,
) -> bool:
    """Simplified T2 gate check for per-bar simulation — no earnings/sentiment data available."""
    if pos.get("tranche_2_triggered"):
        return False
    if now.hour >= config.TRANCHE2_CUTOFF_HOUR:
        return False
    if get_t2_params(regime).cutoff_hour == 0:
        return False
    if regime == "CHOPPY" and (quality is None or "T2_CHOPPY" not in quality.unlocks):
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


def _sim_entry_signal_passes(
    iv,
    regime: str,
    quality: "TickerQuality | None",
    params: dict,
    rsi_max: float,
) -> bool:
    """Return True iff the bar's signals pass all backtest entry gates.

    Mirrors the hard-gate checks in check_all_gates() that are computable
    from precomputed IndicatorValues — no calendar/journal data required.
    """
    tier = quality.tier if quality else "MEDIUM"
    fp = get_filter_params(regime, tier)
    gp = get_gate_params(regime, tier)

    composite_floor = max(fp.composite_min, effective_composite_gate(iv, regime, params=params))
    if iv.composite_score <= composite_floor:
        return False
    if iv.signal != "BUY":
        return False
    if iv.rsi > rsi_max:
        return False
    if iv.vwap_slope < VWAP_SLOPE_MIN:
        return False
    # Stochastic hard veto (Gate 11 in check_all_gates)
    if iv.stoch_d > gp.stoch_hard_veto:
        return False
    # Parabolic hard block (Gate 8)
    if config.PARABOLIC_ENTRY_BLOCK and iv.parabolic:
        return False
    # Gap-up skip (Gate 12)
    if iv.prev_close > 0 and iv.current_price > iv.prev_close * (1 + config.GAP_SKIP_PCT / 100):
        return False
    return True


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
    entry_start = _ts(ENTRY_START_H)
    entry_end   = _ts(get_entry_params(regime).hard_close_hour)
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

    _snap = _BacktestSnap(regime=regime, _watch_list=watch_list)

    pending_signals: dict[str, dict] = {}

    for i, ts in enumerate(sorted_ts):
        ts_et = ts.astimezone(ET)
        is_force = ts_et >= force_ts

        if SWING_ENABLED and not swing_evaluated and ts_et >= swing_eval_ts:
            swing_evaluated = True
            n_swings = sum(1 for p in state.positions.values() if p.get("is_swing"))
            n_swings = _evaluate_swing_promotion(state, day_bars, ts, regime, n_swings,
                                                  precomp=precomp)

        for sym in list(state.positions.keys()):
            df_sym = day_bars.get(sym)
            if df_sym is None or ts not in df_sym.index:
                continue
            bar = df_sym.loc[ts]
            pos = state.positions[sym]
            is_sw = pos.get("is_swing", False)

            if is_sw:
                if pos.get("trailing_stop_active") and bar["high"] > pos.get("trailing_high_water", 0):
                    pos["trailing_high_water"] = float(bar["high"])
                result = evaluate_tiered_tp_action(pos, float(bar["close"]))
                action = result["action"]
                if action == "CLOSE_TP1":
                    _close_trade(state, sym, str(ts), float(bar["close"]), "SWING_TP1", date_str,
                                 qty=result["qty"])
                    if sym in state.positions:
                        pos["tp1_hit"] = True
                elif action == "CLOSE_TP2":
                    _close_trade(state, sym, str(ts), float(bar["close"]), "SWING_TP2", date_str,
                                 qty=result["qty"])
                    if sym in state.positions:
                        pos["tp2_hit"] = True
                        pos["trailing_stop_active"] = True
                        pos["trailing_high_water"] = float(bar["close"])
                elif action in ("CLOSE_TRAILING", "CLOSE_HARD_STOP"):
                    exit_price_sw = pos["sl"] if action == "CLOSE_HARD_STOP" else float(bar["close"])
                    reason_sw = "SWING_SL" if action == "CLOSE_HARD_STOP" else "SWING_TRAILING"
                    _close_trade(state, sym, str(ts), exit_price_sw, reason_sw, date_str)
                    if reason_sw == "SWING_SL":
                        state.sl_blacklist_pending.add(sym)
            else:
                reason = None
                exit_price = None
                if bar["high"] >= pos["tp"]:
                    reason, exit_price = "TP", pos["tp"]
                elif bar["low"] <= pos["sl"]:
                    reason, exit_price = "SL", pos["sl"]
                elif is_force:
                    reason, exit_price = "FORCE_CLOSE", bar["close"]
                if reason:
                    _close_trade(state, sym, str(ts), exit_price, reason, date_str)
                    if reason == "SL":
                        state.sl_blacklist_pending.add(sym)
                # T2 add-on check — use bar["low"] as worst intrabar price proxy
                elif not pos.get("tranche_2_triggered"):
                    t1_entry = pos.get("tranche_1_entry", pos.get("entry", 0))
                    if t1_entry > 0:
                        drop_pct = (t1_entry - float(bar["low"])) / t1_entry * 100
                        iv_at_bar = precomp.get(sym, {}).get(ts)
                        if iv_at_bar is None:
                            iv_at_bar = type("_IV", (), {"rsi": 100, "composite_score": 0})()
                        stored_tier = pos.get("quality_tier", "MEDIUM")
                        t2_quality = TickerQuality(
                            tier=stored_tier, score=50, unlocks=frozenset(),
                            quality_gaps=(), component_scores={},
                        )
                        if _sim_tranche2_gates_pass(pos, iv_at_bar, drop_pct, regime, ts_et, quality=t2_quality):
                            current_portfolio = state.cash + _pos_value_at(state, day_bars, ts)
                            _sim_apply_tranche2(pos, float(bar["low"]), int(pos.get("qty", 1)), current_portfolio)
                            state.t2_fire_count += 1

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
            _execute_pending_signals(state, pending_signals, day_bars, ts, pos_mult,
                                     regime=regime, ts_et=ts_et)

        for sym in watch_list:
            if sym in state.positions or sym in state.daily_closed: continue
            if sym in state.sl_blacklist: continue
            iv = precomp.get(sym, {}).get(ts)
            if iv is None: continue
            from utils.ticker_quality import derive_mode
            quality = compute_ticker_quality(
                sym, iv, _snap, derive_mode(sym, pead_ok=False, catalyst_active=False),
                fred_cache={},
            )
            tier = quality.tier
            if not _sim_entry_signal_passes(iv, regime, quality, p, rsi_max):
                continue
            if ts_et.hour >= config.ENTRY_LATE_HOUR and iv.composite_score < config.ENTRY_LATE_COMPOSITE_MIN:
                continue
            pending_signals[sym] = {"score": iv.composite_score, "atr": iv.atr, "tier": tier}

        equity = state.cash + _pos_value_at(state, day_bars, ts)
        state.equity_curve.append({"ts": str(ts), "equity": round(equity, 2)})

    _eod_manage_positions(state, day_bars, sorted_ts, date_str)

    portfolio_value = state.cash + sum(p["qty"] * p["entry"] for p in state.positions.values())
    logger.info(f"  End of day equity ≈ ${state.cash:,.2f} cash  (portfolio ~${portfolio_value:,.2f})")
