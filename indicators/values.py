# indicators/values.py
from dataclasses import dataclass


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
