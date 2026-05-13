# indicators/composite.py
import json
import logging

import numpy as np
import pandas as pd

import config

logger = logging.getLogger(__name__)


def _safe(series: pd.Series, fallback: float = 0.0) -> float:
    val = series.iloc[-1]
    return fallback if pd.isna(val) else float(val)


def _safe_penultimate(series: pd.Series, fallback: float = 0.0) -> float:
    """Use second-to-last bar — the last bar is always the incomplete current bar."""
    if len(series) < 2:
        return fallback
    val = series.iloc[-2]
    return fallback if pd.isna(val) else float(val)


def load_weights() -> dict:
    with open(config.INDICATOR_WEIGHTS_FILE) as f:
        return json.load(f)


def _composite_score(vals: dict, weights: dict) -> tuple[float, str, float]:
    price = vals["current_price"]
    vwap = vals["vwap"]
    atr = vals["atr"] or 1.0

    def clamp(v: float) -> float:
        return max(-1.0, min(1.0, v))

    # Each indicator produces a [-1, 1] momentum-directional score.

    # VWAP: price above VWAP = in the trend
    vwap_dev = (price - vwap) / (atr * 2 + 1e-9)
    s_vwap = clamp(vwap_dev)

    # RSI as momentum direction: >50 = uptrend, <50 = downtrend
    rsi = vals["rsi"]
    s_rsi = clamp((rsi - 50) / 25)

    # MACD histogram: positive = upward momentum
    macd_hist = vals["macd_hist"]
    s_macd = clamp(macd_hist / (atr * 0.1 + 1e-9))

    # Bollinger Bands: price above midband = uptrend continuation
    bb_upper, bb_lower = vals["bb_upper"], vals["bb_lower"]
    bb_range = bb_upper - bb_lower
    s_bb = clamp(2 * (price - (bb_upper + bb_lower) / 2) / (bb_range + 1e-9)) if bb_range > 0 else 0.0

    # EMA 9/21: crossover direction and magnitude
    ema_diff = (vals["ema9"] - vals["ema21"]) / (price * 0.005 + 1e-9)
    s_ema = clamp(ema_diff)

    # ATR: not directional
    s_atr = 0.0

    # Daily Volume Trend: 5-day volume slope — increasing volume = institutional accumulation
    daily_vol_trend = vals.get("daily_volume_trend", 0.0)
    s_daily_vol = clamp(daily_vol_trend)

    # VWAP StdDev Bands: VWAP slope direction (rising VWAP = bullish day)
    vwap_slope = vals.get("vwap_slope", 0.0)
    s_vwap_std = clamp(vwap_slope / 0.5)

    # Stochastic: %K > %D = momentum bullish
    stoch_k = vals["stoch_k"]
    stoch_d = vals["stoch_d"]
    s_stoch = clamp((stoch_k - stoch_d) / 30)

    # RSI slope: rising RSI confirms momentum is building
    rsi_slope = vals.get("rsi_slope", 0.0)
    s_rsi_slope = clamp(rsi_slope / 15)

    s_obv = clamp(vals.get("obv_trend", 0.0))

    signal_map = {
        "VWAP":                  s_vwap,
        "RSI":                   s_rsi,
        "Daily Volume Trend":    s_daily_vol,
        "MACD":                  s_macd,
        "EMA 9/21 Crossover":    s_ema,
        "Bollinger Bands":       s_bb,
        "VWAP StdDev Bands":     s_vwap_std,
        "ATR":                   s_atr,
        "Stochastic Oscillator": s_stoch,
        "OBV":                   s_obv,
        "RSI Slope":             s_rsi_slope,
    }

    total_w = sum(weights.values())
    weighted_sum = sum(weights.get(k, 0) * v for k, v in signal_map.items())
    raw_score = (weighted_sum / total_w) * 100 if total_w > 0 else 0.0

    # Confidence: scale by daily volume ratio (0.8–1.2 = normal, <0.5 = anomalous)
    daily_vol_ratio = vals.get("daily_volume_ratio", 1.0)
    vol_conf_mult = float(np.clip(daily_vol_ratio, 0.5, 1.5))
    confidence = min(100.0, abs(raw_score) * vol_conf_mult)

    if raw_score > 15:
        signal = "BUY"
    elif raw_score < -15:
        signal = "SELL"
    else:
        signal = "HOLD"

    return round(raw_score, 2), signal, round(confidence, 1)
