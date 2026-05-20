"""
Trading-mode routing and SL/TP table lookup.
Pure — no I/O, no Alpaca calls.
"""
from __future__ import annotations

import config
from utils import regimes as _r

_HYBRID_REGIMES     = frozenset({_r.BULL_TREND, _r.OPPORTUNITY})
_SHORT_TERM_REGIMES = frozenset({_r.CAPITULATION, _r.CRASH_RECOVERY})

_SL_TP_TABLE: dict[tuple[str, str], tuple[float, float]] = {
    ("DAY_TRADE",  "NORMAL"):               (3.0,  5.0),
    ("DAY_TRADE",  "CHOPPY"):               (5.0,  6.0),
    ("DAY_TRADE",  "DEFENSIVE"):            (4.0,  5.0),
    ("DAY_TRADE",  "BULL_TREND_PARABOLIC"): (3.0,  5.0),
    ("HYBRID",     "BULL_TREND"):           (7.0, 15.0),
    ("HYBRID",     "OPPORTUNITY"):          (7.0, 15.0),
    ("SHORT_TERM", "CAPITULATION"):         (12.0, 20.0),
    ("SHORT_TERM", "CRASH_RECOVERY"):       (10.0, 18.0),
    ("PEAD",       "any"):                  (8.0,  8.0),
}

_MODE_POSITION_LIMITS: dict[str, int] = {
    "SHORT_TERM": config.SHORT_TERM_MAX_POSITIONS,
    "PEAD":       config.SHORT_TERM_MAX_POSITIONS,
}


def get_active_mode(regime: str, symbol: str | None = None) -> str:
    """
    Returns trading mode for this regime/symbol combination.
    PEAD overrides if symbol qualifies and regime is not CRISIS.
    """
    if symbol and regime != "CRISIS":
        from calendars.earnings import detect_pead_setup
        import sectors
        pead_ok, _ = detect_pead_setup(symbol)
        if pead_ok and sectors.is_tier1(symbol):
            return "PEAD"
    if regime in _HYBRID_REGIMES:
        return "HYBRID"
    if regime in _SHORT_TERM_REGIMES:
        return "SHORT_TERM"
    if regime == "CRISIS":
        return "CASH"
    return "DAY_TRADE"


def get_sl_tp_for_mode_and_regime(mode: str, regime: str) -> tuple[float, float]:
    """Return (stop_loss_pct, take_profit_pct) for the given mode/regime pair."""
    if (mode, regime) in _SL_TP_TABLE:
        return _SL_TP_TABLE[(mode, regime)]
    if (mode, "any") in _SL_TP_TABLE:
        return _SL_TP_TABLE[(mode, "any")]
    return config.STOP_LOSS_PCT, config.TAKE_PROFIT_PCT
