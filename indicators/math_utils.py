# indicators/math_utils.py
import numpy as np
import pandas as pd


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
