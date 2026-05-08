import json
import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

import config

logger = logging.getLogger(__name__)


@dataclass
class IndicatorValues:
    symbol: str
    timestamp: str
    current_price: float
    # VWAP
    vwap: float
    vwap_std1_upper: float
    vwap_std1_lower: float
    vwap_std2_upper: float
    vwap_std2_lower: float
    # RSI
    rsi: float
    # MACD
    macd: float
    macd_signal: float
    macd_hist: float
    # Bollinger Bands
    bb_upper: float
    bb_mid: float
    bb_lower: float
    # EMA
    ema9: float
    ema21: float
    # ATR
    atr: float
    # Intraday volume ratio (kept for OBV confidence scaling; not used in composite)
    volume_ratio: float
    # Stochastic
    stoch_k: float
    stoch_d: float
    # OBV
    obv: float
    # VWAP slope (% change over last 10 bars — negative = downtrend day)
    vwap_slope: float
    # RSI slope (RSI change over last 5 bars — positive = momentum building)
    rsi_slope: float
    # 5-day daily volume stats
    daily_volume_ratio: float    # latest complete day volume / 5-day mean (1.0 = normal)
    daily_volume_trend: float    # normalised slope of 5-day volumes (-1 to +1)
    daily_volume_mean: float     # 5-day mean daily volume (absolute shares)
    # Composite
    composite_score: float
    signal: str
    confidence: float
    # Counter-bot / regime signals (Plan 2)
    parabolic: bool = False              # 5-day return ≥ 15% on daily bars
    volume_exhaustion: bool = False      # at 5d high + volume < 0.8× 20d avg
    obv_divergence: str = "none"         # "bearish" | "bullish" | "none"
    momentum_exhaustion: bool = False    # 2+ of (parabolic, vol_exhaustion, obv=="bearish")
    bot_herding_score: int = 0           # 0-100; populated at cycle level by trading_agent
    squeeze_setup: bool = False
    squeeze_confidence: int = 0          # 0-100
    stop_hunt_zone: bool = False


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _compute_vwap(df: pd.DataFrame) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3
    cum_vol = df["volume"].cumsum()
    cum_tpvol = (tp * df["volume"]).cumsum()
    return cum_tpvol / cum_vol.replace(0, np.nan)


def _compute_vwap_bands(df: pd.DataFrame, vwap: pd.Series):
    tp = (df["high"] + df["low"] + df["close"]) / 3
    cum_vol = df["volume"].cumsum()
    variance = ((tp - vwap) ** 2 * df["volume"]).cumsum() / cum_vol.replace(0, np.nan)
    std = np.sqrt(variance)
    return vwap + std, vwap - std, vwap + 2 * std, vwap - 2 * std


def _compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rsi = pd.Series(np.where(avg_loss == 0, 100.0, 100 - (100 / (1 + avg_gain / avg_loss))),
                    index=close.index)
    rsi[avg_gain.isna()] = np.nan
    return rsi


def _compute_macd(close: pd.Series, fast=12, slow=26, signal=9):
    ema_fast = _ema(close, fast)
    ema_slow = _ema(close, slow)
    macd_line = ema_fast - ema_slow
    signal_line = _ema(macd_line, signal)
    return macd_line, signal_line, macd_line - signal_line


def _compute_bollinger(close: pd.Series, period=20, std_mult=2):
    mid = close.rolling(period).mean()
    std = close.rolling(period).std()
    return mid + std_mult * std, mid, mid - std_mult * std


def _compute_atr(df: pd.DataFrame, period=14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(com=period - 1, min_periods=period).mean()


def _compute_volume_ratio(volume: pd.Series, period=20) -> pd.Series:
    avg = volume.rolling(period).mean()
    return volume / avg.replace(0, np.nan)


def _compute_stochastic(df: pd.DataFrame, k_period=14, d_period=3):
    low_min = df["low"].rolling(k_period).min()
    high_max = df["high"].rolling(k_period).max()
    denom = (high_max - low_min).replace(0, np.nan)
    k = 100 * (df["close"] - low_min) / denom
    d = k.rolling(d_period).mean()
    return k, d


def _compute_obv(df: pd.DataFrame) -> pd.Series:
    direction = np.sign(df["close"].diff()).fillna(0)
    return (direction * df["volume"]).cumsum()


def _compute_daily_volume_stats(daily_df: pd.DataFrame) -> tuple[float, float, float]:
    """
    Returns (daily_volume_ratio, daily_volume_trend, daily_volume_mean) from
    the last 5 *complete* trading days (today's partial session is excluded).

    daily_volume_ratio: last complete day volume / 5-day mean
                        1.0 = normal, 0.5 = half, 2.0 = double
    daily_volume_trend: normalised linear slope of the 5 daily volumes (-1 to +1)
                        +1 = volume growing ~10%/day over the window
                        -1 = volume shrinking ~10%/day over the window
    daily_volume_mean:  raw 5-day mean volume (absolute shares, IEX feed)
    """
    if daily_df is None or len(daily_df) < 3:
        return 1.0, 0.0, 0.0

    # Drop the most-recent bar — it may be today's incomplete session.
    # We want only fully-settled trading days for meaningful volume comparison.
    complete = daily_df["volume"].iloc[:-1].dropna()

    vols = complete.tail(5).values
    if len(vols) < 2:
        return 1.0, 0.0, 0.0

    mean_vol = float(vols.mean())
    if mean_vol <= 0:
        return 1.0, 0.0, 0.0

    # Ratio: most-recent complete day vs 5-day mean
    daily_volume_ratio = float(vols[-1] / mean_vol)

    # Trend: normalised linear regression slope (fraction per day, ±1 at ±10%/day)
    x = np.arange(len(vols), dtype=float)
    slope = float(np.polyfit(x, vols / mean_vol, 1)[0])
    daily_volume_trend = float(np.clip(slope / 0.10, -1.0, 1.0))

    return round(daily_volume_ratio, 3), round(daily_volume_trend, 3), round(mean_vol, 0)


def _is_parabolic(daily_closes: list[float]) -> bool:
    """True if 5-day daily return ≥ config.PARABOLIC_5DAY_RETURN_PCT."""
    if len(daily_closes) < 6:
        return False
    ret5d = (daily_closes[-1] - daily_closes[-6]) / daily_closes[-6] * 100
    return ret5d >= config.PARABOLIC_5DAY_RETURN_PCT


def _is_volume_exhaustion(df: pd.DataFrame, daily_df: "pd.DataFrame | None") -> bool:
    """True if price is at/near 5-day high AND volume < VOLUME_EXHAUSTION_RATIO × 20-day average."""
    if df is None or len(df) < 5:
        return False
    high_5d = df["high"].tail(5 * 78).max()  # ~78 5-min bars per session
    current_price = float(df["close"].iloc[-1])
    at_high = current_price >= high_5d * 0.995  # within 0.5%
    if not at_high:
        return False
    if daily_df is None or len(daily_df) < 20:
        return False
    avg_vol = daily_df["volume"].tail(20).mean()
    today_vol = float(daily_df["volume"].iloc[-1]) if len(daily_df) else 0.0
    return bool(today_vol < avg_vol * config.VOLUME_EXHAUSTION_RATIO)


def _obv_divergence_signal(df: pd.DataFrame, obv: pd.Series) -> str:
    """
    Bearish divergence: new intraday price high + OBV declining.
    Bullish divergence: new intraday price low + OBV rising.
    """
    if len(df) < 6 or len(obv) < 6:
        return "none"
    recent_prices = df["close"].tail(5)
    recent_obv    = obv.tail(5)
    price_high = recent_prices.iloc[-1] >= recent_prices.max() * 0.999
    obv_declining = recent_obv.iloc[-1] < recent_obv.iloc[0]
    if price_high and obv_declining:
        return "bearish"
    price_low = recent_prices.iloc[-1] <= recent_prices.min() * 1.001
    obv_rising = recent_obv.iloc[-1] > recent_obv.iloc[0]
    if price_low and obv_rising:
        return "bullish"
    return "none"


def _is_stop_hunt_zone(df: pd.DataFrame) -> bool:
    """
    True if within STOP_HUNT_ROUND_PROXIMITY_PCT of a round number ($X.00 / $X.50)
    OR within STOP_HUNT_HILOW_PROXIMITY_PCT of the 5-day hi/lo.
    """
    if df is None or df.empty:
        return False
    price = float(df["close"].iloc[-1])
    round_pct = config.STOP_HUNT_ROUND_PROXIMITY_PCT / 100
    hilow_pct = config.STOP_HUNT_HILOW_PROXIMITY_PCT / 100
    # Round number proximity
    nearest_round = round(price * 2) / 2  # nearest $0.50
    if abs(price - nearest_round) / price < round_pct:
        return True
    # 5-day high/low proximity
    hi5 = df["high"].tail(5 * 78).max()
    lo5 = df["low"].tail(5 * 78).min()
    if abs(price - hi5) / price < hilow_pct or abs(price - lo5) / price < hilow_pct:
        return True
    return False


def compute_squeeze_setup(
    float_turnover_3d: float,
    price_vs_resistance_pct: float,
    volume_surge: float,
    obv_rising_with_falling_price: bool,
) -> tuple[bool, int]:
    """
    4 weighted conditions; threshold = SQUEEZE_CONFIDENCE_GATE (70).
    Max score per condition caps at 25 (need ≥ 3 signals to reach 70).
    """
    score = 0
    if float_turnover_3d > 0.15:       # 15%+ of float traded in 3 days
        score += 25
    if price_vs_resistance_pct < 3.0:  # within 3% of resistance
        score += 25
    if volume_surge > 2.0:             # volume > 2× average
        score += 25
    if obv_rising_with_falling_price:
        score += 25
    from config import SQUEEZE_CONFIDENCE_GATE
    return score >= SQUEEZE_CONFIDENCE_GATE, score


def compute_bot_herding_score(
    series_dict: dict,
    spy_closes: list[float],
    buy_signal_count: int,
    total_watch: int,
    volume_cv: float,
) -> int:
    """
    Combine avg correlation vs SPY, fraction of watch list above BUY threshold,
    and volume coefficient of variation into a 0-100 herding score.
    """
    # Fraction of watch list generating BUY signals this bar
    frac_buying = buy_signal_count / max(total_watch, 1)
    frac_score = min(40, int(frac_buying * 100))  # 0-40 points

    # High volume CV = diverse individual volume → low herding; low CV = herding
    vol_score = max(0, min(30, int((1.0 - min(volume_cv, 1.0)) * 30)))

    # Correlation vs SPY among buy candidates (simple proxy)
    corr_score = 0
    if spy_closes and series_dict:
        n_correlated = sum(
            1 for closes in series_dict.values()
            if len(closes) >= 5 and len(spy_closes) >= 5
            and _approx_corr(closes[-5:], spy_closes[-5:]) > 0.85
        )
        corr_score = min(30, int(n_correlated / max(len(series_dict), 1) * 60))

    return min(100, frac_score + vol_score + corr_score)


def _approx_corr(a: list[float], b: list[float]) -> float:
    """Fast Pearson correlation for two short equal-length lists."""
    n = min(len(a), len(b))
    if n < 3:
        return 0.0
    a, b = a[-n:], b[-n:]
    ma = sum(a) / n
    mb = sum(b) / n
    num = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    da  = sum((x - ma) ** 2 for x in a) ** 0.5
    db  = sum((x - mb) ** 2 for x in b) ** 0.5
    if da == 0 or db == 0:
        return 0.0
    return num / (da * db)


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
    ET = __import__("zoneinfo").ZoneInfo("America/New_York")
    _dt = __import__("datetime")
    ref_et = (as_of.astimezone(ET) if as_of is not None else _dt.datetime.now(ET))
    market_open_today = ref_et.replace(hour=9, minute=30, second=0, microsecond=0)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df_today = df[df.index >= market_open_today.astimezone(__import__("zoneinfo").ZoneInfo("UTC"))]
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
        **{k: v for k, v in vals.items() if k not in ("obv_trend",)},
    )
