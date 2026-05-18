"""Backtest-only: vectorized per-symbol per-day indicator precomputation."""
import datetime as _dt
import logging
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import config
from indicators.composite import _composite_score, load_weights
from indicators.counter_signals import (
    _is_parabolic, _is_volume_exhaustion, _is_stop_hunt_zone, compute_squeeze_setup,
)
from indicators.math_utils import (
    _ema, _compute_vwap, _compute_vwap_bands, _compute_rsi, _compute_macd,
    _compute_bollinger, _compute_atr, _compute_volume_ratio, _compute_stochastic,
    _compute_obv, _compute_daily_volume_stats,
)
from indicators.values import IndicatorValues

logger = logging.getLogger(__name__)


def precompute_indicators_for_day(
    symbol: str,
    all_bars_sym: pd.DataFrame,
    day: _dt.datetime,
    daily_df: "pd.DataFrame | None" = None,
) -> "dict[pd.Timestamp, IndicatorValues]":
    """Vectorized precomputation: compute all indicators once per symbol per day.

    Returns {Timestamp → IndicatorValues} for all intraday bars on `day`.
    Computes RSI/ATR/MACD/etc. as full Series, then reads values via numpy indexing
    (O(1) per bar vs O(100) EWM recomputation in the original per-bar approach).
    """
    from signals.opening_range_gate import get_or_position as _or_pos

    if all_bars_sym is None or len(all_bars_sym) < 30:
        return {}

    df_hist = all_bars_sym.copy()
    if df_hist.index.tz is None:
        df_hist.index = df_hist.index.tz_localize("UTC")

    market_open_et  = day.replace(hour=9, minute=30, second=0, microsecond=0)
    market_open_utc = market_open_et.astimezone(ZoneInfo("UTC"))
    df_today = df_hist[df_hist.index >= market_open_utc]
    if df_today.empty:
        return {}

    # ── Rolling/EWM indicator series — one pass each ─────────────────────────
    rsi_s            = _compute_rsi(df_hist["close"])
    macd_l, macd_sig, macd_h = _compute_macd(df_hist["close"])
    bb_u, bb_m, bb_l = _compute_bollinger(df_hist["close"])
    ema9_s           = _ema(df_hist["close"], 9)
    ema21_s          = _ema(df_hist["close"], 21)
    atr_s            = _compute_atr(df_hist)
    vol_ratio_s      = _compute_volume_ratio(df_hist["volume"])
    stoch_k_s, stoch_d_s = _compute_stochastic(df_hist)
    obv_s            = _compute_obv(df_hist)

    vwap_today = _compute_vwap(df_today)
    vwap_s     = vwap_today.reindex(df_hist.index)
    vs1u, vs1l, vs2u, vs2l = _compute_vwap_bands(df_today, vwap_today)
    vs1u = vs1u.reindex(df_hist.index)
    vs1l = vs1l.reindex(df_hist.index)
    vs2u = vs2u.reindex(df_hist.index)
    vs2l = vs2l.reindex(df_hist.index)

    vwap_slope_s    = ((vwap_s - vwap_s.shift(10)) / vwap_s.shift(10) * 100).fillna(0.0)
    rsi_slope_s     = rsi_s.diff(5).fillna(0.0)
    vol_ratio_prev_s = vol_ratio_s.shift(1).fillna(1.0)
    obv_trend_s     = (
        (obv_s - obv_s.shift(5)) / (df_hist["volume"].rolling(5).mean() * 5 + 1e-9)
    ).clip(-1, 1).fillna(0.0)
    vol_surge_s     = vol_ratio_s   # rolling(5)/rolling(20) — already computed
    resistance_s    = df_hist["high"].rolling(390).max()

    # ── Per-day constants ─────────────────────────────────────────────────────
    or_bars = round(config.OPENING_RANGE_MINUTES / config.BAR_TIMEFRAME_MINUTES)
    or_high = float(df_today.iloc[:or_bars]["high"].max()) if len(df_today) >= or_bars else float("nan")
    or_low  = float(df_today.iloc[:or_bars]["low"].min())  if len(df_today) >= or_bars else float("nan")

    daily_closes = daily_df["close"].dropna().tolist() if daily_df is not None and not daily_df.empty else []
    is_parabolic   = _is_parabolic(daily_closes)
    vol_exhaustion = _is_volume_exhaustion(df_today, daily_df)
    stop_hunt      = _is_stop_hunt_zone(df_today)
    prev_close     = float(daily_df["close"].iloc[-2]) if daily_df is not None and len(daily_df) >= 2 else 0.0
    daily_volume_ratio, daily_volume_trend, daily_volume_mean = _compute_daily_volume_stats(daily_df)
    avg_daily_vol  = (
        float(daily_df["volume"].iloc[-21:-1].mean()) if daily_df is not None and len(daily_df) >= 20 else 0.0
    )
    weights = load_weights()

    # ── Extract numpy arrays indexed to today's bars (O(1) per-bar access) ───
    today_pos = df_hist.index.get_indexer(df_today.index)

    def _arr(s: pd.Series, fallback: float) -> np.ndarray:
        a = s.to_numpy(dtype=float)
        out = a[today_pos]
        return np.where(np.isnan(out), fallback, out)

    close_a     = _arr(df_hist["close"], 100.0)
    vwap_a      = _arr(vwap_s,   np.nan); vs1u_a = _arr(vs1u, np.nan)
    vs1l_a      = _arr(vs1l, np.nan);     vs2u_a = _arr(vs2u, np.nan)
    vs2l_a      = _arr(vs2l, np.nan)
    rsi_a       = _arr(rsi_s,    50.0);   macd_l_a  = _arr(macd_l,   0.0)
    macd_sig_a  = _arr(macd_sig, 0.0);    macd_h_a  = _arr(macd_h,   0.0)
    bb_u_a      = _arr(bb_u, np.nan);     bb_m_a    = _arr(bb_m, np.nan)
    bb_l_a      = _arr(bb_l, np.nan)
    ema9_a      = _arr(ema9_s,  np.nan);  ema21_a   = _arr(ema21_s, np.nan)
    atr_a       = _arr(atr_s,    0.0);    volr_a    = _arr(vol_ratio_prev_s, 1.0)
    stk_a       = _arr(stoch_k_s, 50.0);  std_a     = _arr(stoch_d_s, 50.0)
    obv_a       = _arr(obv_s,    0.0);    obv_tr_a  = _arr(obv_trend_s,  0.0)
    vwap_sl_a   = _arr(vwap_slope_s, 0.0); rsi_sl_a = _arr(rsi_slope_s,  0.0)
    vol_surge_a = _arr(vol_surge_s,  1.0); resist_a  = _arr(resistance_s, np.nan)
    vol_cum_a   = df_today["volume"].cumsum().to_numpy(dtype=float)
    today_close_a = df_today["close"].to_numpy(dtype=float)
    today_obv_a   = obv_s.reindex(df_today.index).to_numpy(dtype=float)

    def _px(a_val, price):
        return price if np.isnan(a_val) else float(a_val)

    result: dict = {}
    for i, ts in enumerate(df_today.index):
        price = float(close_a[i])
        vals = {
            "current_price":      price,
            "vwap":               _px(vwap_a[i], price),
            "vwap_std1_upper":    _px(vs1u_a[i], price),
            "vwap_std1_lower":    _px(vs1l_a[i], price),
            "vwap_std2_upper":    _px(vs2u_a[i], price),
            "vwap_std2_lower":    _px(vs2l_a[i], price),
            "rsi":                float(rsi_a[i]),
            "macd":               float(macd_l_a[i]),
            "macd_signal":        float(macd_sig_a[i]),
            "macd_hist":          float(macd_h_a[i]),
            "bb_upper":           _px(bb_u_a[i], price),
            "bb_mid":             _px(bb_m_a[i], price),
            "bb_lower":           _px(bb_l_a[i], price),
            "ema9":               _px(ema9_a[i], price),
            "ema21":              _px(ema21_a[i], price),
            "atr":                float(atr_a[i]),
            "volume_ratio":       float(volr_a[i]),
            "stoch_k":            float(stk_a[i]),
            "stoch_d":            float(std_a[i]),
            "obv":                float(obv_a[i]),
            "obv_trend":          float(obv_tr_a[i]),
            "vwap_slope":         round(float(vwap_sl_a[i]), 4),
            "rsi_slope":          round(float(rsi_sl_a[i]), 2),
            "daily_volume_ratio": daily_volume_ratio,
            "daily_volume_trend": daily_volume_trend,
            "daily_volume_mean":  daily_volume_mean,
        }
        composite_score, signal, confidence = _composite_score(vals, weights)

        relative_volume = 1.0
        if avg_daily_vol > 0:
            sf = min(1.0, (i + 1) * config.BAR_TIMEFRAME_MINUTES / 390)
            if sf > 0:
                relative_volume = round((float(vol_cum_a[i]) / sf) / avg_daily_vol, 2)

        or_position = "unknown"
        try:
            if not (pd.isna(or_high) or pd.isna(or_low)):
                or_position = _or_pos(price, or_high, or_low)
        except Exception:
            pass

        s0 = max(0, i - 4)
        rp, ro = today_close_a[s0:i + 1], today_obv_a[s0:i + 1]
        if len(rp) >= 2:
            if   rp[-1] >= rp.max() * 0.999 and ro[-1] < ro[0]: obv_div = "bearish"
            elif rp[-1] <= rp.min() * 1.001 and ro[-1] > ro[0]: obv_div = "bullish"
            else:                                                  obv_div = "none"
        else:
            obv_div = "none"

        res_v = price if np.isnan(resist_a[i]) else float(resist_a[i])
        _squeeze, _sq_conf = compute_squeeze_setup(
            float_turnover_3d=0.0,
            price_vs_resistance_pct=abs(price - res_v) / price * 100 if price > 0 else 0.0,
            volume_surge=float(vol_surge_a[i]),
            obv_rising_with_falling_price=(obv_div == "bullish"),
        )
        momentum_exhaust = sum([is_parabolic, vol_exhaustion, obv_div == "bearish"]) >= 2

        result[ts] = IndicatorValues(
            symbol=symbol, timestamp=str(ts),
            composite_score=composite_score, signal=signal, confidence=confidence,
            parabolic=is_parabolic, volume_exhaustion=vol_exhaustion,
            obv_divergence=obv_div, momentum_exhaustion=momentum_exhaust,
            bot_herding_score=0, squeeze_setup=_squeeze, squeeze_confidence=_sq_conf,
            stop_hunt_zone=stop_hunt, prev_close=prev_close,
            relative_volume=relative_volume, or_position=or_position,
            **{k: v for k, v in vals.items() if k != "obv_trend"},
        )
    return result
