# indicators/counter_signals.py
import pandas as pd

import config

# Number of intraday bars in 5 trading days; recalculates if BAR_TIMEFRAME_MINUTES changes
_BARS_PER_5D = 5 * (config.TRADING_MINUTES_PER_DAY // config.BAR_TIMEFRAME_MINUTES)


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
    high_5d = df["high"].tail(_BARS_PER_5D).max()  # bars_per_day = TRADING_MINUTES_PER_DAY // BAR_TIMEFRAME_MINUTES
    current_price = float(df["close"].iloc[-1])
    at_high = current_price >= high_5d * config.VOLUME_EXHAUSTION_PRICE_PROXIMITY
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
    price_high = recent_prices.iloc[-1] >= recent_prices.max() * config.OBV_PRICE_HIGH_PROXIMITY
    obv_declining = recent_obv.iloc[-1] < recent_obv.iloc[0]
    if price_high and obv_declining:
        return "bearish"
    price_low = recent_prices.iloc[-1] <= recent_prices.min() * config.OBV_PRICE_LOW_PROXIMITY
    obv_rising = recent_obv.iloc[-1] > recent_obv.iloc[0]
    if price_low and obv_rising:
        return "bullish"
    return "none"


def _is_stop_hunt_zone(df: pd.DataFrame) -> bool:
    """
    True if within STOP_HUNT_ROUND_PROXIMITY_PCT of a whole-dollar round number ($X.00)
    OR within STOP_HUNT_HILOW_PROXIMITY_PCT of the 5-day hi/lo.

    Note: uses $1.00 boundaries (not $0.50) because $0.50 spacing + 0.4% threshold was
    mathematically guaranteed to fire for every stock above $62.50.
    """
    if df is None or df.empty:
        return False
    price = float(df["close"].iloc[-1])
    round_pct = config.STOP_HUNT_ROUND_PROXIMITY_PCT / 100
    hilow_pct = config.STOP_HUNT_HILOW_PROXIMITY_PCT / 100
    # Round number proximity — nearest whole dollar ($1.00 marks)
    nearest_round = round(price)
    if abs(price - nearest_round) / price < round_pct:
        return True
    # 5-day high/low proximity
    hi5 = df["high"].tail(_BARS_PER_5D).max()
    lo5 = df["low"].tail(_BARS_PER_5D).min()
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
    return score >= config.SQUEEZE_CONFIDENCE_GATE, score


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
