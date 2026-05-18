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
