# indicators/engine.py
import datetime as _dt
import logging
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from indicators.values import IndicatorValues
from indicators.math_utils import (
    _ema,
    _compute_vwap,
    _compute_vwap_bands,
    _compute_rsi,
    _compute_macd,
    _compute_bollinger,
    _compute_atr,
    _compute_volume_ratio,
    _compute_stochastic,
    _compute_obv,
    _compute_daily_volume_stats,
)
from indicators.composite import _safe, _safe_penultimate, load_weights, _composite_score
from indicators.counter_signals import (
    _is_parabolic,
    _is_volume_exhaustion,
    _obv_divergence_signal,
    _is_stop_hunt_zone,
    compute_squeeze_setup,
)

logger = logging.getLogger(__name__)


def compute_indicators(
    symbol: str,
    df: pd.DataFrame,
    daily_df: "pd.DataFrame | None" = None,   # 5-day daily bars for volume context
    as_of: "datetime | None" = None,           # pass historical datetime for backtesting
) -> Optional[IndicatorValues]:
    if df is None or len(df) < 30:
        logger.warning(f"Insufficient bars for {symbol}: {len(df) if df is not None else 0}")
        return None

    df = df.copy()

    # VWAP resets at market open each day.
    ET = ZoneInfo("America/New_York")
    ref_et = (as_of.astimezone(ET) if as_of is not None else _dt.datetime.now(ET))
    market_open_today = ref_et.replace(hour=9, minute=30, second=0, microsecond=0)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df_today = df[df.index >= market_open_today.astimezone(ZoneInfo("UTC"))]
    df_vwap = df_today if len(df_today) >= 2 else df

    vwap = _compute_vwap(df_vwap)
    vs1u, vs1l, vs2u, vs2l = _compute_vwap_bands(df_vwap, vwap)

    vwap  = vwap.reindex(df.index)
    vs1u  = vs1u.reindex(df.index)
    vs1l  = vs1l.reindex(df.index)
    vs2u  = vs2u.reindex(df.index)
    vs2l  = vs2l.reindex(df.index)

    rsi = _compute_rsi(df["close"])
    macd_line, macd_sig, macd_hist = _compute_macd(df["close"])
    bb_upper, bb_mid, bb_lower = _compute_bollinger(df["close"])
    ema9  = _ema(df["close"], 9)
    ema21 = _ema(df["close"], 21)
    atr   = _compute_atr(df)
    vol_ratio = _compute_volume_ratio(df["volume"])
    stoch_k, stoch_d = _compute_stochastic(df)
    obv = _compute_obv(df)

    # OBV trend: compare last bar to 5 bars ago
    obv_trend = 0.0
    if len(obv) >= 6:
        obv_change = obv.iloc[-1] - obv.iloc[-6]
        vol_scale = df["volume"].iloc[-5:].mean() * 5 + 1e-9
        obv_trend = float(np.clip(obv_change / vol_scale, -1.0, 1.0))

    # VWAP slope: % change over last 10 intraday bars
    vwap_clean = vwap.dropna()
    if len(vwap_clean) >= 11:
        v0, v1 = float(vwap_clean.iloc[-11]), float(vwap_clean.iloc[-1])
        vwap_slope = (v1 - v0) / v0 * 100 if v0 > 0 else 0.0
    else:
        vwap_slope = 0.0

    # RSI slope: point change over last 5 bars
    rsi_clean = rsi.dropna()
    rsi_slope = float(rsi_clean.iloc[-1] - rsi_clean.iloc[-6]) if len(rsi_clean) >= 6 else 0.0

    # 5-day daily volume stats
    daily_volume_ratio, daily_volume_trend, daily_volume_mean = _compute_daily_volume_stats(daily_df)

    price = float(df["close"].iloc[-1])
    ts    = str(df.index[-1])

    vals = {
        "current_price":      price,
        "vwap":               _safe(vwap, price),
        "vwap_std1_upper":    _safe(vs1u, price),
        "vwap_std1_lower":    _safe(vs1l, price),
        "vwap_std2_upper":    _safe(vs2u, price),
        "vwap_std2_lower":    _safe(vs2l, price),
        "rsi":                _safe(rsi, 50.0),
        "macd":               _safe(macd_line),
        "macd_signal":        _safe(macd_sig),
        "macd_hist":          _safe(macd_hist),
        "bb_upper":           _safe(bb_upper, price),
        "bb_mid":             _safe(bb_mid, price),
        "bb_lower":           _safe(bb_lower, price),
        "ema9":               _safe(ema9, price),
        "ema21":              _safe(ema21, price),
        "atr":                _safe(atr, 0.0),
        "volume_ratio":       _safe_penultimate(vol_ratio, 1.0),
        "stoch_k":            _safe(stoch_k, 50.0),
        "stoch_d":            _safe(stoch_d, 50.0),
        "obv":                float(obv.iloc[-1]),
        "obv_trend":          obv_trend,
        "vwap_slope":         round(vwap_slope, 4),
        "rsi_slope":          round(rsi_slope, 2),
        "daily_volume_ratio": daily_volume_ratio,
        "daily_volume_trend": daily_volume_trend,
        "daily_volume_mean":  daily_volume_mean,
    }

    weights = load_weights()
    composite_score, signal, confidence = _composite_score(vals, weights)

    prev_close = float(daily_df["close"].iloc[-2]) if daily_df is not None and len(daily_df) >= 2 else 0.0

    # ── New Plan-2 signals ────────────────────────────────────────────────────
    daily_closes: list[float] = []
    if daily_df is not None and not daily_df.empty:
        daily_closes = daily_df["close"].dropna().tolist()

    is_parabolic      = _is_parabolic(daily_closes)
    vol_exhaustion    = _is_volume_exhaustion(df, daily_df)
    obv_div           = _obv_divergence_signal(df, obv)
    stop_hunt         = _is_stop_hunt_zone(df)
    momentum_exhaust  = sum([is_parabolic, vol_exhaustion, obv_div == "bearish"]) >= 2

    # Squeeze setup using best available proxies from intraday data
    vol_surge = float(df["volume"].tail(5).mean() / (df["volume"].tail(20).mean() + 1e-9))
    _squeeze, _squeeze_conf = compute_squeeze_setup(
        float_turnover_3d=0.0,          # populated by trading_agent with float data
        price_vs_resistance_pct=abs(price - float(df["high"].tail(5 * 78).max())) / price * 100,
        volume_surge=vol_surge,
        obv_rising_with_falling_price=(obv_div == "bullish"),
    )

    return IndicatorValues(
        symbol=symbol,
        timestamp=ts,
        composite_score=composite_score,
        signal=signal,
        confidence=confidence,
        parabolic=is_parabolic,
        volume_exhaustion=vol_exhaustion,
        obv_divergence=obv_div,
        momentum_exhaustion=momentum_exhaust,
        bot_herding_score=0,   # set at cycle level by trading_agent
        squeeze_setup=_squeeze,
        squeeze_confidence=_squeeze_conf,
        stop_hunt_zone=stop_hunt,
        prev_close=prev_close,
        **{k: v for k, v in vals.items() if k not in ("obv_trend",)},
    )
