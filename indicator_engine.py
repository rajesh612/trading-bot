# indicator_engine.py — backward-compatible re-exports; real code in indicators/
from indicators.values import IndicatorValues
from indicators.engine import compute_indicators
from indicators.composite import load_weights
from indicators.counter_signals import (
    compute_squeeze_setup,
    compute_bot_herding_score,
    _is_parabolic,
    _is_volume_exhaustion,
    _obv_divergence_signal,
    _is_stop_hunt_zone,
    _approx_corr,
)
from indicators.math_utils import (
    _ema, _compute_vwap, _compute_vwap_bands, _compute_rsi, _compute_macd,
    _compute_bollinger, _compute_atr, _compute_volume_ratio, _compute_stochastic,
    _compute_obv, _compute_daily_volume_stats,
)
from indicators.composite import _safe, _safe_penultimate, _composite_score

__all__ = [
    "IndicatorValues", "compute_indicators", "load_weights",
    "compute_squeeze_setup", "compute_bot_herding_score",
]
