"""
Pure analysis helpers for the learning agent — no I/O, no Alpaca calls.
"""
import logging

import config
from utils.io_utils import load_json, save_json

logger = logging.getLogger(__name__)


_TRADE_SUMMARY_KEYS = {
    "symbol", "timestamp", "pnl_pct", "pnl_dollar", "close_reason",
    "entry_composite_score", "exit_composite_score",
    "indicator_signals_at_entry", "claude_reasoning", "entry_sentiment_score",
    "mode_at_entry", "regime_at_entry", "atr_pct",
}

# Hard bounds on learnable thresholds — prevents Claude from disabling stops or
# removing entire regimes.  Values come from the Plan 3 spec §18.
_THRESHOLD_BOUNDS: dict[str, tuple] = {
    "choppy_composite_gate":          (25,   40),
    "bull_trend_rsi_max_tier1":       (76,   88),
    "swing_max_days":                 (3,    7),
    "capitulation_drawdown_threshold":(15,   30),
    "squeeze_confidence_gate":        (60,   80),
    "herd_size_reduction":            (0.15, 0.50),
    "stop_loss_pct":                  (1.5,  12.0),
    "take_profit_pct":                (2.0,  25.0),
    "rsi_buy_max":                    (30,   80),
    "rsi_buy_max_high_vol":           (25,   70),
    "vwap_slope_min":                 (-2.0, 0.0),
    "high_vol_position_mult":         (0.20, 0.75),
}


def _clamp_threshold(key: str, value) -> float | int:
    """Clamp `value` to the hard bounds for `key`.  Returns value unchanged if key unknown."""
    if key not in _THRESHOLD_BOUNDS:
        return value
    lo, hi = _THRESHOLD_BOUNDS[key]
    try:
        v = float(value)
        clamped = max(lo, min(hi, v))
        return int(clamped) if isinstance(lo, int) else clamped
    except (TypeError, ValueError):
        return value


def _compute_regime_winrate(closed_trades: list) -> dict[str, float]:
    """
    Returns win-rate (0.0–1.0) per regime for closed trades.
    Only CLOSE entries with a 'regime_at_entry' field are counted.
    """
    regime_totals: dict[str, int] = {}
    regime_wins: dict[str, int] = {}

    for trade in closed_trades:
        if trade.get("action") != "CLOSE":
            continue
        regime = trade.get("regime_at_entry")
        if not regime:
            continue
        regime_totals[regime] = regime_totals.get(regime, 0) + 1
        if float(trade.get("pnl_pct", 0)) > 0:
            regime_wins[regime] = regime_wins.get(regime, 0) + 1

    return {
        regime: regime_wins.get(regime, 0) / total
        for regime, total in regime_totals.items()
    }


def _aggregate_by_regime(closed_trades: list[dict]) -> dict:
    """
    Group today's closed trades by regime_at_entry and mode_at_entry.
    Returns a dict with per-regime and per-mode performance summaries.
    """
    by_regime: dict[str, list[dict]] = {}
    by_mode:   dict[str, list[dict]] = {}
    for t in closed_trades:
        regime = t.get("regime_at_entry") or "UNKNOWN"
        mode   = t.get("mode_at_entry")   or "UNKNOWN"
        by_regime.setdefault(regime, []).append(t)
        by_mode.setdefault(mode,   []).append(t)

    def _summarise(trades: list[dict]) -> dict:
        winners = [t for t in trades if (t.get("pnl_pct") or 0) > 0]
        losers  = [t for t in trades if (t.get("pnl_pct") or 0) <= 0]
        total_pnl = sum(t.get("pnl_dollar") or 0 for t in trades)
        avg_pnl   = sum(t.get("pnl_pct") or 0 for t in trades) / len(trades) if trades else 0
        return {
            "count":           len(trades),
            "winners":         len(winners),
            "losers":          len(losers),
            "win_rate_pct":    round(len(winners) / len(trades) * 100, 1) if trades else 0,
            "avg_pnl_pct":     round(avg_pnl, 2),
            "total_pnl_dollar":round(total_pnl, 2),
        }

    return {
        "by_regime": {k: _summarise(v) for k, v in by_regime.items()},
        "by_mode":   {k: _summarise(v) for k, v in by_mode.items()},
    }


def _apply_weight_update(updates: dict, defaults: dict) -> None:
    """Apply indicator weight updates with [MIN_MULT×default, MAX_MULT×default] clamping.

    Logs any clamped values so the operator can inspect learning quality.
    """
    existing = load_json(config.INDICATOR_WEIGHTS_FILE, dict(defaults))
    clamped_any = False
    for key, raw_val in updates.items():
        default = defaults.get(key, 1.0)
        lo = default * config.INDICATOR_WEIGHT_MIN_MULT
        hi = default * config.INDICATOR_WEIGHT_MAX_MULT
        clamped = max(lo, min(hi, float(raw_val)))
        if abs(clamped - float(raw_val)) > 0.01:
            logger.warning(
                "Weight %s clamped: %.3f → %.3f (bounds: %.3f–%.3f)",
                key, raw_val, clamped, lo, hi,
            )
            clamped_any = True
        existing[key] = round(clamped, 4)
    save_json(config.INDICATOR_WEIGHTS_FILE, existing)
    if clamped_any:
        logger.info("Weight update applied with clamping — check learning quality.")


def _classify_loss_source(trade: dict) -> str:
    """
    Classify the root cause of a losing trade.  Priority order matches spec §19.
    Returns one of: earnings_surprise | market_structure | momentum_exhaustion |
                    fundamental | macro_pressure | sector_narrative | unknown
    """
    close_reason  = (trade.get("close_reason") or "").lower()
    entry_ind     = trade.get("entry_indicators") or {}
    entry_score   = trade.get("entry_composite_score") or 0

    # Earnings surprise: earnings-driven close reason
    if any(kw in close_reason for kw in ("earnings", "pead")):
        return "earnings_surprise"

    # Market structure: broker forced close or extreme regime
    regime = (trade.get("regime_at_entry") or "").upper()
    if regime in ("CRISIS", "CRASH_RECOVERY") or "flash_crash" in close_reason:
        return "market_structure"

    # Momentum exhaustion flagged at entry
    if entry_ind.get("momentum_exhaustion"):
        return "momentum_exhaustion"

    # Fundamental news driver
    if any(kw in close_reason for kw in ("fundamental", "guidance", "downgrade")):
        return "fundamental"

    # Macro pressure: high shock score at entry
    if (entry_ind.get("entry_macro_shock_score") or 0) >= 60:
        return "macro_pressure"

    # Sector narrative: negative sentiment at entry
    if (trade.get("entry_sentiment_score") or 0) <= -25:
        return "sector_narrative"

    return "unknown"


_ATR_TP_GRID = [1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0]
_ATR_TP_MIN_TRADES = 15


def _tune_atr_tp_multiplier(trades: list[dict]) -> float | None:
    """Grid-search for the ATR-TP multiplier that maximises Sharpe over recent trades.

    For each candidate multiplier M, simulates what P&L each trade would have produced:
    - sim_tp = clamp(atr_pct × M, FLOOR, CEILING)
    - If actual pnl_pct >= sim_tp: trade hit the target → credit sim_tp (locked in)
    - Otherwise: trade missed the target → use actual pnl_pct

    Ranks candidates by Sharpe (mean/std); falls back to mean when std == 0.
    Returns None if fewer than _ATR_TP_MIN_TRADES trades have an `atr_pct` field.
    """
    annotated = [
        t for t in trades
        if t.get("atr_pct") and t.get("pnl_pct") is not None
    ]
    if len(annotated) < _ATR_TP_MIN_TRADES:
        return None

    best_mult = _ATR_TP_GRID[0]
    best_score: float = float("-inf")

    for mult in _ATR_TP_GRID:
        pnl_series = []
        for t in annotated:
            sim_tp = max(config.ATR_TP_FLOOR_PCT,
                         min(config.ATR_TP_CEILING_PCT, t["atr_pct"] * mult))
            actual = t["pnl_pct"]
            pnl_series.append(sim_tp if actual >= sim_tp else actual)

        n = len(pnl_series)
        mean = sum(pnl_series) / n
        variance = sum((x - mean) ** 2 for x in pnl_series) / n
        std = variance ** 0.5
        score = mean / std if std > 0 else mean

        if score > best_score:
            best_score = score
            best_mult = mult

    return float(max(config.ATR_TP_MULT_MIN, min(config.ATR_TP_MULT_MAX, best_mult)))
