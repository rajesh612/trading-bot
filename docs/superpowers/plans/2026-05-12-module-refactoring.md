# Module Refactoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split five oversized source files into focused sub-modules so every file stays under 300 lines, without changing any public API.

**Architecture:** Each original module becomes a thin re-export shim (`from new.module import fn`) so existing callers — including 10+ production modules and 5 test files — need zero changes. Real code moves to new per-responsibility files inside `indicators/`, `research/`, `learning/`, and `trading/` packages, and two new `utils/` helpers for `main.py`.

**Tech Stack:** Python 3.11, existing stdlib + third-party deps (numpy, pandas, anthropic, feedparser, requests, apscheduler)

---

## File Map

| Original | Lines | Extracts to | Lines each |
|---|---|---|---|
| `indicator_engine.py` | 555 | `indicators/values.py`, `indicators/math_utils.py`, `indicators/counter_signals.py`, `indicators/composite.py`, `indicators/engine.py`, `indicators/__init__.py` | 70 / 120 / 160 / 100 / 130 / 10 |
| `research_agent.py` | 589 | `research/__init__.py`, `research/fetchers.py`, `research/analyzer.py`, `research/agent.py` | 5 / 230 / 170 / 100 |
| `learning_agent.py` | 422 | `learning/__init__.py`, `learning/trade_analysis.py`, `learning/data.py`, `learning/agent.py` | 5 / 120 / 50 / 210 |
| `swing_manager.py` | 442 | `trading/tiered_tp.py`, `trading/eod_holds.py`, `swing_manager.py` (trimmed) | 65 / 130 / 250 |
| `main.py` | 838 | `utils/instance_lock.py`, `utils/startup.py`, `utils/bot_health.py`, `utils/catchup.py`, `scheduler_jobs.py`, `main.py` (trimmed) | 55 / 130 / 110 / 110 / 280 / 200 |

**Backward-compat rule:** every original module (`indicator_engine.py`, `research_agent.py`, `learning_agent.py`) ends up as a re-export shim. No test imports or `main.py` job calls need updating.

---

## Task 1: indicators/ package from indicator_engine.py

**Files:**
- Create: `indicators/__init__.py`
- Create: `indicators/values.py`
- Create: `indicators/math_utils.py`
- Create: `indicators/counter_signals.py`
- Create: `indicators/composite.py`
- Create: `indicators/engine.py`
- Modify: `indicator_engine.py` (replace body with re-exports)
- Test: `tests/test_indicators.py` (run existing tests — no changes needed)

- [ ] **Step 1: Create indicators/__init__.py**

```python
# indicators/__init__.py
from indicators.values import IndicatorValues
from indicators.engine import compute_indicators
from indicators.composite import load_weights
from indicators.counter_signals import compute_squeeze_setup, compute_bot_herding_score

__all__ = [
    "IndicatorValues",
    "compute_indicators",
    "load_weights",
    "compute_squeeze_setup",
    "compute_bot_herding_score",
]
```

- [ ] **Step 2: Create indicators/values.py**

Move only the `IndicatorValues` dataclass (lines 14–68 of `indicator_engine.py`):

```python
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
    daily_volume_ratio: float
    daily_volume_trend: float
    daily_volume_mean: float
    # Composite
    composite_score: float
    signal: str
    confidence: float
    # Counter-bot / regime signals (Plan 2)
    parabolic: bool = False
    volume_exhaustion: bool = False
    obv_divergence: str = "none"
    momentum_exhaustion: bool = False
    bot_herding_score: int = 0
    squeeze_setup: bool = False
    squeeze_confidence: int = 0
    stop_hunt_zone: bool = False
```

- [ ] **Step 3: Create indicators/math_utils.py**

Move all `_compute_*` and `_ema` functions (lines 70–179 of `indicator_engine.py`):

```python
# indicators/math_utils.py
import numpy as np
import pandas as pd
import config


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
    rsi = pd.Series(
        np.where(avg_loss == 0, 100.0, 100 - (100 / (1 + avg_gain / avg_loss))),
        index=close.index,
    )
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
    """Returns (daily_volume_ratio, daily_volume_trend, daily_volume_mean) from last 5 complete days."""
    if daily_df is None or len(daily_df) < 3:
        return 1.0, 0.0, 0.0

    complete = daily_df["volume"].iloc[:-1].dropna()
    vols = complete.tail(5).values
    if len(vols) < 2:
        return 1.0, 0.0, 0.0

    mean_vol = float(vols.mean())
    if mean_vol <= 0:
        return 1.0, 0.0, 0.0

    daily_volume_ratio = float(vols[-1] / mean_vol)
    x = np.arange(len(vols), dtype=float)
    slope = float(np.polyfit(x, vols / mean_vol, 1)[0])
    daily_volume_trend = float(np.clip(slope / 0.10, -1.0, 1.0))

    return round(daily_volume_ratio, 3), round(daily_volume_trend, 3), round(mean_vol, 0)
```

- [ ] **Step 4: Create indicators/counter_signals.py**

Move Plan-2 counter-signal functions (lines 182–299 of `indicator_engine.py`):

```python
# indicators/counter_signals.py
import config


def _approx_corr(a: list[float], b: list[float]) -> float:
    """Fast Pearson correlation for two short equal-length lists."""
    n = min(len(a), len(b))
    if n < 3:
        return 0.0
    a, b = a[-n:], b[-n:]
    ma = sum(a) / n
    mb = sum(b) / n
    num = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    da = sum((x - ma) ** 2 for x in a) ** 0.5
    db = sum((x - mb) ** 2 for x in b) ** 0.5
    if da == 0 or db == 0:
        return 0.0
    return num / (da * db)


def _is_parabolic(daily_closes: list[float]) -> bool:
    """True if 5-day daily return >= config.PARABOLIC_5DAY_RETURN_PCT."""
    if len(daily_closes) < 6:
        return False
    ret5d = (daily_closes[-1] - daily_closes[-6]) / daily_closes[-6] * 100
    return ret5d >= config.PARABOLIC_5DAY_RETURN_PCT


def _is_volume_exhaustion(df, daily_df) -> bool:
    """True if price is at/near 5-day high AND volume < VOLUME_EXHAUSTION_RATIO × 20-day avg."""
    if df is None or len(df) < 5:
        return False
    high_5d = df["high"].tail(5 * 78).max()
    current_price = float(df["close"].iloc[-1])
    if current_price < high_5d * 0.995:
        return False
    if daily_df is None or len(daily_df) < 20:
        return False
    avg_vol = daily_df["volume"].tail(20).mean()
    today_vol = float(daily_df["volume"].iloc[-1]) if len(daily_df) else 0.0
    return bool(today_vol < avg_vol * config.VOLUME_EXHAUSTION_RATIO)


def _obv_divergence_signal(df, obv) -> str:
    """Bearish divergence: new price high + OBV declining. Bullish: price low + OBV rising."""
    if len(df) < 6 or len(obv) < 6:
        return "none"
    recent_prices = df["close"].tail(5)
    recent_obv = obv.tail(5)
    if recent_prices.iloc[-1] >= recent_prices.max() * 0.999 and recent_obv.iloc[-1] < recent_obv.iloc[0]:
        return "bearish"
    if recent_prices.iloc[-1] <= recent_prices.min() * 1.001 and recent_obv.iloc[-1] > recent_obv.iloc[0]:
        return "bullish"
    return "none"


def _is_stop_hunt_zone(df) -> bool:
    """True if within proximity of a round number or 5-day hi/lo."""
    if df is None or df.empty:
        return False
    price = float(df["close"].iloc[-1])
    round_pct = config.STOP_HUNT_ROUND_PROXIMITY_PCT / 100
    hilow_pct = config.STOP_HUNT_HILOW_PROXIMITY_PCT / 100
    nearest_round = round(price * 2) / 2
    if abs(price - nearest_round) / price < round_pct:
        return True
    hi5 = df["high"].tail(5 * 78).max()
    lo5 = df["low"].tail(5 * 78).min()
    return abs(price - hi5) / price < hilow_pct or abs(price - lo5) / price < hilow_pct


def compute_squeeze_setup(
    float_turnover_3d: float,
    price_vs_resistance_pct: float,
    volume_surge: float,
    obv_rising_with_falling_price: bool,
) -> tuple[bool, int]:
    """4 weighted conditions; threshold = SQUEEZE_CONFIDENCE_GATE (70)."""
    score = 0
    if float_turnover_3d > 0.15:
        score += 25
    if price_vs_resistance_pct < 3.0:
        score += 25
    if volume_surge > 2.0:
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
    """Combine avg correlation vs SPY, fraction buying, and volume CV into 0-100 herding score."""
    frac_buying = buy_signal_count / max(total_watch, 1)
    frac_score = min(40, int(frac_buying * 100))
    vol_score = max(0, min(30, int((1.0 - min(volume_cv, 1.0)) * 30)))
    corr_score = 0
    if spy_closes and series_dict:
        n_correlated = sum(
            1 for closes in series_dict.values()
            if len(closes) >= 5 and len(spy_closes) >= 5
            and _approx_corr(closes[-5:], spy_closes[-5:]) > 0.85
        )
        corr_score = min(30, int(n_correlated / max(len(series_dict), 1) * 60))
    return min(100, frac_score + vol_score + corr_score)
```

- [ ] **Step 5: Create indicators/composite.py**

Move score helpers and `_composite_score` (lines 302–419 of `indicator_engine.py`):

```python
# indicators/composite.py
import json
import numpy as np
import pandas as pd
import config


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

    s_vwap     = clamp((price - vwap) / (atr * 2 + 1e-9))
    s_rsi      = clamp((vals["rsi"] - 50) / 25)
    s_macd     = clamp(vals["macd_hist"] / (atr * 0.1 + 1e-9))
    bb_range   = vals["bb_upper"] - vals["bb_lower"]
    s_bb       = clamp(2 * (price - (vals["bb_upper"] + vals["bb_lower"]) / 2) / (bb_range + 1e-9)) if bb_range > 0 else 0.0
    s_ema      = clamp((vals["ema9"] - vals["ema21"]) / (price * 0.005 + 1e-9))
    s_atr      = 0.0
    s_daily_vol = clamp(vals.get("daily_volume_trend", 0.0))
    s_vwap_std = clamp(vals.get("vwap_slope", 0.0) / 0.5)
    s_stoch    = clamp((vals["stoch_k"] - vals["stoch_d"]) / 30)
    s_rsi_slope = clamp(vals.get("rsi_slope", 0.0) / 15)
    s_obv      = clamp(vals.get("obv_trend", 0.0))

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
```

- [ ] **Step 6: Create indicators/engine.py**

Move `compute_indicators` (lines 422–555 of `indicator_engine.py`):

```python
# indicators/engine.py
import logging
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

import config
from indicators.values import IndicatorValues
from indicators.math_utils import (
    _ema, _compute_vwap, _compute_vwap_bands, _compute_rsi, _compute_macd,
    _compute_bollinger, _compute_atr, _compute_volume_ratio, _compute_stochastic,
    _compute_obv, _compute_daily_volume_stats,
)
from indicators.counter_signals import (
    _is_parabolic, _is_volume_exhaustion, _obv_divergence_signal,
    _is_stop_hunt_zone, compute_squeeze_setup,
)
from indicators.composite import _safe, _safe_penultimate, load_weights, _composite_score

logger = logging.getLogger(__name__)


def compute_indicators(
    symbol: str,
    df: pd.DataFrame,
    daily_df: "pd.DataFrame | None" = None,
    as_of: "datetime | None" = None,
) -> Optional[IndicatorValues]:
    if df is None or len(df) < 30:
        logger.warning(f"Insufficient bars for {symbol}: {len(df) if df is not None else 0}")
        return None

    df = df.copy()

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

    vwap = vwap.reindex(df.index)
    vs1u = vs1u.reindex(df.index)
    vs1l = vs1l.reindex(df.index)
    vs2u = vs2u.reindex(df.index)
    vs2l = vs2l.reindex(df.index)

    rsi = _compute_rsi(df["close"])
    macd_line, macd_sig, macd_hist = _compute_macd(df["close"])
    bb_upper, bb_mid, bb_lower = _compute_bollinger(df["close"])
    ema9  = _ema(df["close"], 9)
    ema21 = _ema(df["close"], 21)
    atr   = _compute_atr(df)
    vol_ratio = _compute_volume_ratio(df["volume"])
    stoch_k, stoch_d = _compute_stochastic(df)
    obv = _compute_obv(df)

    obv_trend = 0.0
    if len(obv) >= 6:
        obv_change = obv.iloc[-1] - obv.iloc[-6]
        vol_scale = df["volume"].iloc[-5:].mean() * 5 + 1e-9
        obv_trend = float(np.clip(obv_change / vol_scale, -1.0, 1.0))

    vwap_clean = vwap.dropna()
    if len(vwap_clean) >= 11:
        v0, v1 = float(vwap_clean.iloc[-11]), float(vwap_clean.iloc[-1])
        vwap_slope = (v1 - v0) / v0 * 100 if v0 > 0 else 0.0
    else:
        vwap_slope = 0.0

    rsi_clean = rsi.dropna()
    rsi_slope = float(rsi_clean.iloc[-1] - rsi_clean.iloc[-6]) if len(rsi_clean) >= 6 else 0.0

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

    daily_closes: list[float] = []
    if daily_df is not None and not daily_df.empty:
        daily_closes = daily_df["close"].dropna().tolist()

    is_par      = _is_parabolic(daily_closes)
    vol_exhaust = _is_volume_exhaustion(df, daily_df)
    obv_div     = _obv_divergence_signal(df, obv)
    stop_hunt   = _is_stop_hunt_zone(df)
    momentum_exhaust = sum([is_par, vol_exhaust, obv_div == "bearish"]) >= 2

    vol_surge = float(df["volume"].tail(5).mean() / (df["volume"].tail(20).mean() + 1e-9))
    _squeeze, _squeeze_conf = compute_squeeze_setup(
        float_turnover_3d=0.0,
        price_vs_resistance_pct=abs(price - float(df["high"].tail(5 * 78).max())) / price * 100,
        volume_surge=vol_surge,
        obv_rising_with_falling_price=(obv_div == "bullish"),
    )

    return IndicatorValues(
        symbol=symbol, timestamp=ts, current_price=price,
        vwap=vals["vwap"], vwap_std1_upper=vals["vwap_std1_upper"],
        vwap_std1_lower=vals["vwap_std1_lower"], vwap_std2_upper=vals["vwap_std2_upper"],
        vwap_std2_lower=vals["vwap_std2_lower"], rsi=vals["rsi"],
        macd=vals["macd"], macd_signal=vals["macd_signal"], macd_hist=vals["macd_hist"],
        bb_upper=vals["bb_upper"], bb_mid=vals["bb_mid"], bb_lower=vals["bb_lower"],
        ema9=vals["ema9"], ema21=vals["ema21"], atr=vals["atr"],
        volume_ratio=vals["volume_ratio"], stoch_k=vals["stoch_k"], stoch_d=vals["stoch_d"],
        obv=vals["obv"], vwap_slope=vwap_slope, rsi_slope=rsi_slope,
        daily_volume_ratio=daily_volume_ratio, daily_volume_trend=daily_volume_trend,
        daily_volume_mean=daily_volume_mean, composite_score=composite_score,
        signal=signal, confidence=confidence,
        parabolic=is_par, volume_exhaustion=vol_exhaust, obv_divergence=obv_div,
        momentum_exhaustion=momentum_exhaust, squeeze_setup=_squeeze,
        squeeze_confidence=_squeeze_conf, stop_hunt_zone=stop_hunt,
    )
```

- [ ] **Step 7: Replace indicator_engine.py with re-export shim**

Read the current file first, then replace the entire body:

```python
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
```

- [ ] **Step 8: Verify tests still pass**

Run: `pytest tests/test_indicators.py tests/test_agents.py tests/test_foundation.py -v -m unit`

Expected: all previously-passing tests continue to pass; no import errors.

- [ ] **Step 9: Commit**

```bash
git add indicators/ indicator_engine.py
git commit -m "refactor: split indicator_engine into indicators/ package"
```

---

## Task 2: research/ package from research_agent.py

**Files:**
- Create: `research/__init__.py`
- Create: `research/fetchers.py`
- Create: `research/analyzer.py`
- Create: `research/agent.py`
- Modify: `research_agent.py` (replace body with re-exports)
- Test: `tests/test_research.py` (run — no changes needed)

- [ ] **Step 1: Create research/__init__.py**

```python
# research/__init__.py
from research.agent import run_research_agent

__all__ = ["run_research_agent"]
```

- [ ] **Step 2: Create research/fetchers.py**

Move all fetch functions and constants (lines 1–229 of `research_agent.py`):

```python
# research/fetchers.py
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import feedparser
import requests

import config

logger = logging.getLogger(__name__)

REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TradingBot/1.0; +research)"}

MARKET_RSS_FEEDS = [
    "https://feeds.marketwatch.com/marketwatch/topstories/",
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "https://feeds.benzinga.com/benzinga",
    "https://finance.yahoo.com/news/rssindex",
    "https://feeds.bloomberg.com/markets/news.rss",
]

REDDIT_SUBREDDITS = [
    "wallstreetbets", "stocks", "investing",
    "options", "StockMarket", "SecurityAnalysis",
]


def _fetch_with_retry(fn, *args, attempts: int = 3, base_delay: float = 1.0, **kwargs):
    """Call fn(*args, **kwargs) up to `attempts` times with exponential backoff."""
    if attempts < 1:
        raise ValueError(f"attempts must be >= 1, got {attempts}")
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception:
            if i == attempts - 1:
                raise
            time.sleep(base_delay * (2 ** i))


def _fetch_rss(url: str, max_items: int = 6) -> list[dict]:
    try:
        resp = _fetch_with_retry(requests.get, url, headers=REQUEST_HEADERS, timeout=8)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        return [
            {"title": e.get("title", ""), "summary": e.get("summary", "")[:400], "published": e.get("published", "")}
            for e in feed.entries[:max_items]
        ]
    except Exception as exc:
        logger.debug(f"RSS fetch failed {url}: {exc}")
        return []


def _fetch_stock_news(symbol: str) -> list[dict]:
    items: list[dict] = []
    items.extend(_fetch_rss(f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US"))
    items.extend(_fetch_rss(f"https://www.benzinga.com/stock/{symbol.lower()}/feed"))
    return items[:10]


def _fetch_market_news() -> list[dict]:
    all_items: list[dict] = []
    for url in MARKET_RSS_FEEDS:
        all_items.extend(_fetch_rss(url))
    return all_items[:25]


def _fetch_stocktwits(symbol: str) -> dict:
    """Returns raw messages plus pre-counted bullish/bearish split."""
    try:
        url = f"https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"
        resp = _fetch_with_retry(requests.get, url, headers=REQUEST_HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        messages = data.get("messages", [])
        bullish = sum(1 for m in messages if m.get("entities", {}).get("sentiment", {}).get("basic") == "Bullish")
        bearish = sum(1 for m in messages if m.get("entities", {}).get("sentiment", {}).get("basic") == "Bearish")
        total_tagged = bullish + bearish
        bullish_pct = round(bullish / total_tagged * 100) if total_tagged > 0 else None
        snippets = [
            {"text": m.get("body", "")[:200], "sentiment": m.get("entities", {}).get("sentiment", {}).get("basic"), "likes": m.get("likes", {}).get("total", 0)}
            for m in messages[:5]
        ]
        return {"available": True, "message_count": len(messages), "bullish_count": bullish,
                "bearish_count": bearish, "bullish_pct": bullish_pct, "snippets": snippets}
    except Exception as exc:
        logger.debug(f"StockTwits fetch failed for {symbol}: {exc}")
        return {"available": False}


def _fetch_reddit(symbol: str) -> dict:
    """Fetches Reddit posts using the public JSON API. Six subreddit searches run in parallel."""
    headers = {"User-Agent": "TradingBot/1.0 (sentiment research; read-only)"}

    def _fetch_sub(sub: str) -> list[dict]:
        url = (f"https://www.reddit.com/r/{sub}/search.json"
               f"?q={symbol}+OR+%24{symbol}&sort=new&t=day&restrict_sr=true&limit=5")
        resp = _fetch_with_retry(requests.get, url, headers=headers, timeout=10)
        resp.raise_for_status()
        children = resp.json().get("data", {}).get("children", [])
        return [
            {"subreddit": sub, "title": p.get("title", ""), "text": (p.get("selftext", "") or "")[:300],
             "score": p.get("score", 0), "upvote_ratio": p.get("upvote_ratio", 0),
             "num_comments": p.get("num_comments", 0), "url": p.get("url", "")}
            for child in children for p in [child.get("data", {})]
        ]

    posts: list[dict] = []
    with ThreadPoolExecutor(max_workers=len(REDDIT_SUBREDDITS)) as executor:
        futures = {executor.submit(_fetch_sub, sub): sub for sub in REDDIT_SUBREDDITS}
        for future in as_completed(futures):
            sub = futures[future]
            try:
                posts.extend(future.result())
            except Exception as exc:
                logger.debug(f"Reddit r/{sub} fetch failed for {symbol}: {exc}")
    posts.sort(key=lambda p: p["score"], reverse=True)
    return {"available": True, "post_count": len(posts), "posts": posts[:5]}


def _fetch_x_posts(symbol: str) -> dict:
    """Fetches recent tweets mentioning $SYMBOL. Skips gracefully if token not set."""
    if not config.X_BEARER_TOKEN:
        return {"available": False, "reason": "X_BEARER_TOKEN not configured"}
    try:
        import tweepy
        client = tweepy.Client(bearer_token=config.X_BEARER_TOKEN, wait_on_rate_limit=False)
        response = _fetch_with_retry(
            client.search_recent_tweets,
            query=f"${symbol} lang:en -is:retweet -is:reply",
            max_results=20,
            tweet_fields=["created_at", "public_metrics", "text", "author_id"],
            attempts=2, base_delay=0.5,
        )
        if not response.data:
            return {"available": True, "tweet_count": 0, "tweets": []}
        tweets = [
            {"text": t.text[:280], "likes": t.public_metrics.get("like_count", 0),
             "retweets": t.public_metrics.get("retweet_count", 0),
             "replies": t.public_metrics.get("reply_count", 0), "created_at": str(t.created_at)}
            for t in response.data
        ]
        tweets.sort(key=lambda t: t["likes"] + t["retweets"], reverse=True)
        return {"available": True, "tweet_count": len(tweets), "tweets": tweets[:5]}
    except Exception as exc:
        logger.warning(f"X fetch failed for {symbol}: {exc}")
        return {"available": False, "reason": str(exc)}
```

- [ ] **Step 3: Create research/analyzer.py**

Move analysis helpers (lines 232–434 of `research_agent.py`):

```python
# research/analyzer.py
import json
import logging
import re

import anthropic

import config
from tier1_config import get_sector

logger = logging.getLogger(__name__)

_MACRO_SHOCK_KEYWORDS = {
    "tariff": 20, "trade war": 25, "sanction": 20, "war escalation": 30,
    "fed emergency": 35, "bank failure": 35, "cpi shock": 25, "inflation surge": 20,
    "recession": 20, "default": 25, "nuclear": 30, "pandemic": 30,
    "market crash": 30, "circuit breaker": 35,
}


def _compute_macro_shock_score(headlines: list[dict]) -> int:
    """Keyword-weighted scan of market headlines; capped at 100."""
    if not headlines:
        return 0
    text = " ".join((h.get("title", "") + " " + h.get("summary", "")).lower() for h in headlines)
    score = sum(weight for keyword, weight in _MACRO_SHOCK_KEYWORDS.items() if keyword in text)
    return min(100, score)


def _compute_sector_sentiment(per_stock_report: dict) -> dict:
    """Group combined_sentiment_score by tier-1 sector; return mean per bucket."""
    buckets: dict[str, list[float]] = {}
    for symbol, data in per_stock_report.items():
        if symbol == "__meta__":
            continue
        sector = get_sector(symbol)
        score = data.get("combined_sentiment_score", data.get("sentiment_score", 0))
        buckets.setdefault(sector, []).append(float(score))
    return {s: round(sum(v) / len(v), 1) for s, v in buckets.items() if v}


def _detect_capitulation_candidate(
    symbol: str,
    drawdown_from_20d_high: float,
    sector_sentiment: float,
    obv_trend: float,
) -> bool:
    """True if drawdown <= -20%, sector sentiment < -20, and OBV rising (> 0.10)."""
    return drawdown_from_20d_high <= -20.0 and sector_sentiment < -20 and obv_trend > 0.10


def _quick_sentiment_result(stocktwits: dict, stock_news: list[dict]) -> dict | None:
    """
    Returns a filled analysis dict when StockTwits signal is unambiguous AND there
    is no stock-specific news that could override social sentiment.  Returns None
    when Claude is still needed.
    """
    if stock_news:
        return None
    if not stocktwits.get("available"):
        return None
    tagged = stocktwits.get("bullish_count", 0) + stocktwits.get("bearish_count", 0)
    bullish_pct = stocktwits.get("bullish_pct")
    if tagged < config.STOCKTWITS_MIN_TAGGED or bullish_pct is None:
        return None
    if bullish_pct >= config.QUICK_SENTIMENT_BULL_PCT:
        label, score = "BULLISH", config.QUICK_SENTIMENT_SCORE_BULL
    elif bullish_pct <= config.QUICK_SENTIMENT_BEAR_PCT:
        label, score = "BEARISH", config.QUICK_SENTIMENT_SCORE_BEAR
    else:
        return None
    return {
        "news_sentiment_score": 0, "news_sentiment_label": "NEUTRAL",
        "social_sentiment_score": score, "social_sentiment_label": label,
        "combined_sentiment_score": score, "combined_sentiment_label": label,
        "key_catalyst": None, "risk_flag": None, "divergence_alert": None,
        "summary": f"Rule-based: StockTwits {bullish_pct}% bullish ({tagged} tagged msgs, no news).",
        "strategy": f"Social sentiment strongly {label.lower()} — trade with trend.",
    }


def _analyze_stock(
    symbol: str,
    stock_news: list[dict],
    market_context_block: str,
    stocktwits: dict,
    reddit: dict,
    x_posts: dict,
    ai: anthropic.Anthropic,
    cached_system: list[dict],
) -> dict:
    news_block = "\n".join(f"- {n['title']}: {n['summary'][:200]}" for n in stock_news[:3]) or "No specific news."

    if stocktwits.get("available"):
        st_snippets = "\n".join(f"  [{s.get('sentiment','?')}] {s['text']}" for s in stocktwits["snippets"][:3])
        st_block = (
            f"StockTwits: {stocktwits['message_count']} messages | "
            f"Bull: {stocktwits['bullish_count']} Bear: {stocktwits['bearish_count']}"
            + (f" ({stocktwits['bullish_pct']}% bullish)" if stocktwits["bullish_pct"] else "")
            + f"\n{st_snippets}"
        )
    else:
        st_block = "StockTwits: not available."

    if reddit.get("available") and reddit.get("post_count", 0) > 0:
        reddit_block = "Reddit: {post_count} posts\n".format(**reddit) + "\n".join(
            f"  [r/{p['subreddit']} score={p['score']}] {p['title']}" for p in reddit["posts"][:5]
        )
    elif reddit.get("available"):
        reddit_block = "Reddit: no posts today."
    else:
        reddit_block = f"Reddit: {reddit.get('reason', 'not available')}."

    if x_posts.get("available") and x_posts.get("tweet_count", 0) > 0:
        x_block = "X: {tweet_count} posts\n".format(**x_posts) + "\n".join(
            f"  [❤{t['likes']} RT{t['retweets']}] {t['text'][:120]}" for t in x_posts["tweets"][:5]
        )
    elif x_posts.get("available"):
        x_block = "X: no posts found."
    else:
        x_block = f"X: {x_posts.get('reason', 'not available')}."

    response = ai.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=500,
        system=cached_system,
        messages=[{"role": "user", "content": (
            f"Symbol: {symbol}\n\n=== STOCK NEWS ===\n{news_block}\n\n"
            f"=== STOCKTWITS ===\n{st_block}\n\n=== REDDIT ===\n{reddit_block}\n\n"
            f"=== X ===\n{x_block}\n\n"
            'Respond with:\n{\n  "news_sentiment_score": <-100 to 100>,\n'
            '  "news_sentiment_label": "<BULLISH|BEARISH|NEUTRAL>",\n'
            '  "social_sentiment_score": <-100 to 100>,\n'
            '  "social_sentiment_label": "<BULLISH|BEARISH|NEUTRAL>",\n'
            '  "combined_sentiment_score": <-100 to 100, weighted blend>,\n'
            '  "combined_sentiment_label": "<BULLISH|BEARISH|NEUTRAL>",\n'
            '  "key_catalyst": "<main driver or null>",\n'
            '  "risk_flag": "<major risk or null>",\n'
            '  "divergence_alert": "<short squeeze or divergence if present, else null>",\n'
            '  "summary": "<2-sentence combined summary>",\n'
            '  "strategy": "<2-3 bullet points on intraday strategy>"\n}'
        )}],
    )

    text = response.content[0].text.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    return {
        "news_sentiment_score": 0, "news_sentiment_label": "NEUTRAL",
        "social_sentiment_score": 0, "social_sentiment_label": "NEUTRAL",
        "combined_sentiment_score": 0, "combined_sentiment_label": "NEUTRAL",
        "key_catalyst": None, "risk_flag": None, "divergence_alert": None,
        "summary": "Parse error.", "strategy": "No strategy data.",
    }
```

- [ ] **Step 4: Create research/agent.py**

Move `run_research_agent` (lines 439–578 of `research_agent.py`):

```python
# research/agent.py
import json
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic

import api_budget
import config
from research.fetchers import (
    _fetch_market_news, _fetch_stock_news, _fetch_stocktwits, _fetch_reddit, _fetch_x_posts,
)
from research.analyzer import (
    _quick_sentiment_result, _analyze_stock,
    _compute_macro_shock_score, _compute_sector_sentiment,
)
from tier1_config import is_tier1

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


def run_research_agent(watch_list: list[str]) -> dict:
    if not watch_list:
        logger.warning("Watch list empty — skipping research.")
        return {}

    x_enabled = bool(config.X_BEARER_TOKEN)
    logger.info(
        f"Research agent: {len(watch_list)} stocks | "
        f"Reddit=✓ (public JSON) | X={'✓' if x_enabled else '✗ (no token)'} | StockTwits=✓"
    )

    ai = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    market_news = _fetch_market_news()
    market_context_block = "\n".join(f"- {n['title']}: {n['summary']}" for n in market_news[:6])

    cached_system = [{
        "type": "text",
        "text": (
            "You are a financial analyst combining institutional news with retail social media "
            "sentiment to generate intraday trading intelligence.\n\n"
            "Source weighting:\n"
            "- News: highest weight, institutional perspective\n"
            "- StockTwits: bullish/bearish tags from active traders — strong intraday signal\n"
            "- Reddit WSB/stocks: noisy — weight by post score\n"
            "- X: real-time but mixed quality — weight by engagement\n\n"
            "Flag divergence (news bearish + social very bullish) as a potential squeeze setup.\n"
            "Be concise and data-driven. Respond in strict JSON.\n\n"
            f"=== TODAY'S MARKET CONTEXT ===\n{market_context_block}"
        ),
        "cache_control": {"type": "ephemeral"},
    }]

    report: dict = {}
    for symbol in watch_list:
        try:
            stock_news  = _fetch_stock_news(symbol)
            stocktwits  = _fetch_stocktwits(symbol)
            reddit_data = _fetch_reddit(symbol)
            x_data      = _fetch_x_posts(symbol)

            quick = _quick_sentiment_result(stocktwits, stock_news)
            if quick is not None:
                logger.info(
                    f"    {symbol}: rule-based sentiment ({quick['combined_sentiment_label']}, "
                    f"pct={stocktwits.get('bullish_pct')}%) — Claude skipped"
                )
                analysis = quick
            else:
                analysis = _analyze_stock(
                    symbol, stock_news, market_context_block,
                    stocktwits, reddit_data, x_data, ai, cached_system,
                )
                # Record usage for budget tracking
                # Note: _analyze_stock returns analysis dict; usage recorded via api_budget
        except Exception as exc:
            logger.warning(f"  {symbol}: research failed, using neutral defaults — {exc}")
            analysis = {
                "news_sentiment_score": 0, "news_sentiment_label": "NEUTRAL",
                "social_sentiment_score": 0, "social_sentiment_label": "NEUTRAL",
                "combined_sentiment_score": 0, "combined_sentiment_label": "NEUTRAL",
                "key_catalyst": None, "risk_flag": "Research error",
                "divergence_alert": None, "summary": "Research unavailable.",
                "strategy": "No data — trade cautiously.",
            }
            stocktwits = {}
            reddit_data = {}
            x_data = {}

        report[symbol] = {
            "timestamp":               datetime.now(ET).isoformat(),
            "sentiment_score":         analysis.get("combined_sentiment_score", 0),
            "sentiment_label":         analysis.get("combined_sentiment_label", "NEUTRAL"),
            "news_sentiment_score":    analysis.get("news_sentiment_score", 0),
            "news_sentiment_label":    analysis.get("news_sentiment_label", "NEUTRAL"),
            "social_sentiment_score":  analysis.get("social_sentiment_score", 0),
            "social_sentiment_label":  analysis.get("social_sentiment_label", "NEUTRAL"),
            "key_catalyst":            analysis.get("key_catalyst"),
            "risk_flag":               analysis.get("risk_flag"),
            "divergence_alert":        analysis.get("divergence_alert"),
            "summary":                 analysis.get("summary", ""),
            "strategy":                analysis.get("strategy", ""),
            "stocktwits_message_count": stocktwits.get("message_count", 0),
            "stocktwits_bullish_pct":   stocktwits.get("bullish_pct"),
            "reddit_post_count":        reddit_data.get("post_count", 0),
            "x_tweet_count":            x_data.get("tweet_count", 0),
        }

        logger.info(
            f"    {symbol}: news={analysis.get('news_sentiment_label')}({analysis.get('news_sentiment_score'):+d}) | "
            f"social={analysis.get('social_sentiment_label')}({analysis.get('social_sentiment_score'):+d}) | "
            f"combined={analysis.get('combined_sentiment_label')}({analysis.get('combined_sentiment_score'):+d})"
            + (f" | ⚠️  {analysis.get('divergence_alert')}" if analysis.get("divergence_alert") else "")
        )

        with open(config.RESEARCH_REPORT_FILE, "w") as f:
            json.dump(report, f, indent=2)
        time.sleep(0.3)

    macro_shock_score = _compute_macro_shock_score(market_news)
    sector_sentiment  = _compute_sector_sentiment(report)

    capitulation_candidates = []
    for symbol, data in report.items():
        if is_tier1(symbol) and data.get("sentiment_score", 0) < -30:
            capitulation_candidates.append(symbol)

    report["__meta__"] = {
        "sector_sentiment":       sector_sentiment,
        "macro_shock_score":      macro_shock_score,
        "unscheduled_event_risk": macro_shock_score >= 60,
        "capitulation_candidates": capitulation_candidates,
        "timestamp":              datetime.now(ET).isoformat(),
    }

    with open(config.RESEARCH_REPORT_FILE, "w") as f:
        json.dump(report, f, indent=2)

    logger.info(
        f"Research report saved. macro_shock={macro_shock_score} | "
        f"sectors={list(sector_sentiment.keys())} | capitulation_candidates={capitulation_candidates}"
    )
    return report
```

- [ ] **Step 5: Replace research_agent.py with re-export shim**

```python
# research_agent.py — backward-compatible re-exports; real code in research/
from research.agent import run_research_agent
from research.fetchers import (
    _fetch_with_retry, _fetch_rss, _fetch_stock_news, _fetch_market_news,
    _fetch_stocktwits, _fetch_reddit, _fetch_x_posts,
    MARKET_RSS_FEEDS, REDDIT_SUBREDDITS, REQUEST_HEADERS,
)
from research.analyzer import (
    _compute_macro_shock_score, _compute_sector_sentiment,
    _detect_capitulation_candidate, _quick_sentiment_result, _analyze_stock,
    _MACRO_SHOCK_KEYWORDS,
)

__all__ = ["run_research_agent"]
```

- [ ] **Step 6: Verify tests still pass**

Run: `pytest tests/test_research.py tests/test_foundation.py -v -m unit`

Expected: all tests pass; no import errors.

- [ ] **Step 7: Commit**

```bash
git add research/ research_agent.py
git commit -m "refactor: split research_agent into research/ package"
```

---

## Task 3: learning/ package from learning_agent.py

**Files:**
- Create: `learning/__init__.py`
- Create: `learning/trade_analysis.py`
- Create: `learning/data.py`
- Create: `learning/agent.py`
- Modify: `learning_agent.py` (replace body with re-exports)
- Test: `tests/test_learning.py` (run — no changes needed)

- [ ] **Step 1: Create learning/__init__.py**

```python
# learning/__init__.py
from learning.agent import run_learning_agent

__all__ = ["run_learning_agent"]
```

- [ ] **Step 2: Create learning/trade_analysis.py**

Move threshold bounds, classify functions (lines 17–148 of `learning_agent.py`):

```python
# learning/trade_analysis.py

_TRADE_SUMMARY_KEYS = {
    "symbol", "timestamp", "pnl_pct", "pnl_dollar", "close_reason",
    "entry_composite_score", "exit_composite_score",
    "indicator_signals_at_entry", "claude_reasoning", "entry_sentiment_score",
    "mode_at_entry", "regime_at_entry",
}

# Hard bounds on learnable thresholds — prevents Claude from disabling stops or
# removing entire regimes.
_THRESHOLD_BOUNDS: dict[str, tuple] = {
    "choppy_composite_gate":          (25,   40),
    "bull_trend_rsi_max_tier1":       (76,   88),
    "swing_max_days":                 (3,    7),
    "capitulation_drawdown_threshold":(15,   30),
    "squeeze_confidence_gate":        (60,   80),
    "herd_size_reduction":            (0.15, 0.50),
    "stop_loss_pct":                  (1.5,  12.0),
    "take_profit_pct":                (2.0,  25.0),
    "rsi_buy_max":                    (30,   80),
    "rsi_buy_max_high_vol":           (25,   70),
    "vwap_slope_min":                 (-2.0, 0.0),
    "high_vol_position_mult":         (0.20, 0.75),
}


def _clamp_threshold(key: str, value) -> float | int:
    """Clamp `value` to the hard bounds for `key`. Returns value unchanged if key unknown."""
    if key not in _THRESHOLD_BOUNDS:
        return value
    lo, hi = _THRESHOLD_BOUNDS[key]
    try:
        v = float(value)
        clamped = max(lo, min(hi, v))
        return int(clamped) if isinstance(lo, int) else clamped
    except (TypeError, ValueError):
        return value


def _compute_regime_winrate(closed_trades: list) -> dict[str, float]:
    """Returns win-rate (0.0–1.0) per regime for closed trades."""
    regime_totals: dict[str, int] = {}
    regime_wins: dict[str, int] = {}
    for trade in closed_trades:
        if trade.get("action") != "CLOSE":
            continue
        regime = trade.get("regime_at_entry")
        if not regime:
            continue
        regime_totals[regime] = regime_totals.get(regime, 0) + 1
        if float(trade.get("pnl_pct", 0)) > 0:
            regime_wins[regime] = regime_wins.get(regime, 0) + 1
    return {r: regime_wins.get(r, 0) / total for r, total in regime_totals.items()}


def _aggregate_by_regime(closed_trades: list[dict]) -> dict:
    """Group closed trades by regime_at_entry and mode_at_entry."""
    by_regime: dict[str, list[dict]] = {}
    by_mode:   dict[str, list[dict]] = {}
    for t in closed_trades:
        by_regime.setdefault(t.get("regime_at_entry") or "UNKNOWN", []).append(t)
        by_mode.setdefault(t.get("mode_at_entry") or "UNKNOWN", []).append(t)

    def _summarise(trades: list[dict]) -> dict:
        winners = [t for t in trades if (t.get("pnl_pct") or 0) > 0]
        losers  = [t for t in trades if (t.get("pnl_pct") or 0) <= 0]
        total_pnl = sum(t.get("pnl_dollar") or 0 for t in trades)
        avg_pnl   = sum(t.get("pnl_pct") or 0 for t in trades) / len(trades) if trades else 0
        return {
            "count": len(trades), "winners": len(winners), "losers": len(losers),
            "win_rate_pct": round(len(winners) / len(trades) * 100, 1) if trades else 0,
            "avg_pnl_pct": round(avg_pnl, 2), "total_pnl_dollar": round(total_pnl, 2),
        }

    return {
        "by_regime": {k: _summarise(v) for k, v in by_regime.items()},
        "by_mode":   {k: _summarise(v) for k, v in by_mode.items()},
    }


def _classify_loss_source(trade: dict) -> str:
    """
    Classify the root cause of a losing trade.
    Returns: earnings_surprise | market_structure | momentum_exhaustion |
             fundamental | macro_pressure | sector_narrative | unknown
    """
    close_reason = (trade.get("close_reason") or "").lower()
    entry_ind    = trade.get("entry_indicators") or {}

    if any(kw in close_reason for kw in ("earnings", "pead")):
        return "earnings_surprise"
    regime = (trade.get("regime_at_entry") or "").upper()
    if regime in ("CRISIS", "CRASH_RECOVERY") or "flash_crash" in close_reason:
        return "market_structure"
    if entry_ind.get("momentum_exhaustion"):
        return "momentum_exhaustion"
    if any(kw in close_reason for kw in ("fundamental", "guidance", "downgrade")):
        return "fundamental"
    if (entry_ind.get("entry_macro_shock_score") or 0) >= 60:
        return "macro_pressure"
    if (trade.get("entry_sentiment_score") or 0) <= -25:
        return "sector_narrative"
    return "unknown"
```

- [ ] **Step 3: Create learning/data.py**

Move persistence helpers (lines 150–174 of `learning_agent.py`):

```python
# learning/data.py
from datetime import date

import config
from learning.trade_analysis import _TRADE_SUMMARY_KEYS
from utils.io_utils import load_json, save_json


def _load_todays_closed_trades() -> list[dict]:
    if not config.TRADE_JOURNAL_FILE.exists():
        return []
    journal = load_json(config.TRADE_JOURNAL_FILE, [])
    today = date.today().isoformat()
    return [
        {k: v for k, v in t.items() if k in _TRADE_SUMMARY_KEYS}
        for t in journal
        if t.get("type") == "CLOSE" and t.get("timestamp", "").startswith(today)
    ]


def _load_weights() -> dict:
    return load_json(config.INDICATOR_WEIGHTS_FILE, {})


def _save_weights(weights: dict) -> None:
    save_json(config.INDICATOR_WEIGHTS_FILE, weights)


def _save_trading_params(params: dict) -> None:
    params["last_updated"] = date.today().isoformat()
    save_json(config.TRADING_PARAMS_FILE, params)
```

- [ ] **Step 4: Create learning/agent.py**

Move `run_learning_agent` (lines 176–416 of `learning_agent.py`). This is large but contains a single orchestrating function — keep it whole:

```python
# learning/agent.py
import json
import logging
import re
from datetime import date

import anthropic

import api_budget
import config
from macro_regime import load_macro_regime, load_trading_params
from learning.trade_analysis import (
    _clamp_threshold, _compute_regime_winrate, _aggregate_by_regime, _classify_loss_source,
)
from learning.data import _load_todays_closed_trades, _load_weights, _save_weights, _save_trading_params

logger = logging.getLogger(__name__)


def run_learning_agent():
    today = date.today().isoformat()
    closed_trades = _load_todays_closed_trades()

    if not closed_trades:
        logger.info("No closed trades today — nothing to learn from.")
        return

    current_weights = _load_weights()
    current_params  = load_trading_params()
    regime_info     = load_macro_regime()
    ai = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    regime_agg    = _aggregate_by_regime(closed_trades)
    regime_winrate = _compute_regime_winrate(closed_trades)
    loss_sources  = {
        t["symbol"]: _classify_loss_source(t)
        for t in closed_trades if (t.get("pnl_pct") or 0) < 0
    }

    # ── Step 1: Performance + regime analysis ──────────────────────────────────
    analysis_response = ai.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=2000,
        system=[{
            "type": "text",
            "text": (
                "You are a quantitative trading analyst reviewing today's closed trades.\n"
                "Your job is two-fold:\n"
                "1. Analyse which indicators were reliable and suggest weight adjustments.\n"
                "2. Evaluate whether the trading parameters should be tuned based on today's regime.\n\n"
                "Weight adjustment rules:\n"
                "- Max ±5 per indicator per day\n"
                "- Adjustments must keep total weights summing to 100\n"
                "- Only adjust based on evidence from today's trades\n\n"
                "Trading params adjustment rules:\n"
                "- entry_start_hour: 9–11 (integer)\n"
                "- entry_end_hour: 13–15 (integer)\n"
                "- rsi_buy_max: 30–50 (normal regime)\n"
                "- rsi_buy_max_high_vol: 25–40 (high vol regime)\n"
                "- vwap_slope_min: -1.0 to 0.0\n"
                "- high_vol_position_mult: 0.25–0.75\n"
                "- Only suggest param changes if you have strong evidence from today.\n\n"
                "Respond in strict JSON."
            ),
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": (
            f"Date: {today}\nVIX regime today: {json.dumps(regime_info, indent=2)}\n\n"
            f"Performance by regime/mode:\n{json.dumps(regime_agg, indent=2)}\n\n"
            f"Regime win rates:\n{json.dumps(regime_winrate, indent=2)}\n\n"
            f"Loss source classification:\n{json.dumps(loss_sources, indent=2)}\n\n"
            f"Closed trades:\n{json.dumps(closed_trades, indent=2)}\n\n"
            f"Current indicator weights:\n{json.dumps(current_weights, indent=2)}\n\n"
            f"Current trading params:\n{json.dumps(current_params, indent=2)}\n\n"
            'Return JSON:\n{\n  "total_trades": <int>,\n  "winning_trades": <int>,\n'
            '  "losing_trades": <int>,\n  "total_pnl_dollar": <float>,\n'
            '  "win_rate_pct": <float>,\n  "market_conditions": "<trending|ranging|high_vol|low_vol|mixed>",\n'
            '  "regime_impact": "<how did the VIX regime affect performance today>",\n'
            '  "indicator_performance": {\n    "<indicator_name>": {\n'
            '      "accuracy_pct": <0-100>,\n      "weight_adjustment": <-5 to 5>,\n'
            '      "notes": "<one sentence>"\n    }\n  },\n'
            '  "suggested_params": {\n    "entry_start_hour": <int>,\n    "entry_end_hour": <int>,\n'
            '    "rsi_buy_max": <int>,\n    "rsi_buy_max_high_vol": <int>,\n'
            '    "vwap_slope_min": <float>,\n    "high_vol_position_mult": <float>,\n'
            '    "stop_loss_pct": <float>,\n    "take_profit_pct": <float>\n  },\n'
            '  "strategy_learnings": ["<lesson1>", "..."],\n'
            '  "per_stock_notes": {"<SYMBOL>": "<observation>"}\n}'
        )}],
    )

    # Record API usage
    try:
        usage = analysis_response.usage
        api_budget.record_usage(
            input_tokens=getattr(usage, "input_tokens", 0),
            output_tokens=getattr(usage, "output_tokens", 0),
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0),
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0),
        )
    except Exception:
        pass

    text = analysis_response.content[0].text.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        logger.error("Could not parse learning agent analysis.")
        return
    analysis = json.loads(match.group())

    # ── Step 2: Update indicator weights ───────────────────────────────────────
    new_weights = dict(current_weights)
    for indicator, data in analysis.get("indicator_performance", {}).items():
        if indicator in new_weights:
            adj = float(data.get("weight_adjustment", 0))
            new_weights[indicator] = max(1.0, new_weights[indicator] + adj)
    total = sum(new_weights.values())
    if total > 0:
        new_weights = {k: round(v / total * 100, 2) for k, v in new_weights.items()}
    _save_weights(new_weights)
    logger.info(f"Weights updated: {new_weights}")

    # ── Step 3: Update trading params with hard-bound clamping ─────────────────
    suggested = analysis.get("suggested_params", {})
    new_params = dict(current_params)
    param_keys = [
        "entry_start_hour", "entry_end_hour", "rsi_buy_max", "rsi_buy_max_high_vol",
        "vwap_slope_min", "high_vol_position_mult", "stop_loss_pct", "take_profit_pct",
    ]
    changed_params = []
    for key in param_keys:
        if key in suggested and suggested[key] != current_params.get(key):
            new_val = _clamp_threshold(key, suggested[key])
            if new_val != current_params.get(key):
                changed_params.append(f"{key}: {current_params.get(key)} → {new_val}")
                new_params[key] = new_val
    _save_trading_params(new_params)
    if changed_params:
        logger.info(f"Trading params updated: {', '.join(changed_params)}")
    else:
        logger.info("Trading params unchanged today.")

    # ── Step 4: Append to performance log ──────────────────────────────────────
    log_lines = [
        f"\n## {today}\n",
        f"**Regime:** {regime_info.get('regime', 'NORMAL')} | "
        f"VIX={regime_info.get('vix_current', 0):.1f} (90d avg={regime_info.get('vix_avg_90d', 0):.1f}) | "
        f"SPY={regime_info.get('spy_change_pct', 0):+.2f}%",
        f"**Trades:** {analysis.get('total_trades', 0)} total | "
        f"W:{analysis.get('winning_trades', 0)} L:{analysis.get('losing_trades', 0)} | "
        f"Win rate: {analysis.get('win_rate_pct', 0):.1f}% | P&L: ${analysis.get('total_pnl_dollar', 0):.2f}",
        f"**Market conditions:** {analysis.get('market_conditions', 'unknown')} | "
        f"Regime impact: {analysis.get('regime_impact', 'n/a')}\n",
        "### Indicator Performance\n",
    ]
    for indicator, data in analysis.get("indicator_performance", {}).items():
        old_w = current_weights.get(indicator, 0)
        new_w = new_weights.get(indicator, 0)
        adj   = data.get("weight_adjustment", 0)
        adj_str = f"+{adj}" if adj > 0 else str(adj)
        log_lines.append(
            f"- **{indicator}**: accuracy={data.get('accuracy_pct', 0):.0f}% | "
            f"weight {old_w}% → {new_w}% ({adj_str}%) | {data.get('notes', '')}"
        )
    if changed_params:
        log_lines += ["\n### Param Changes\n"] + [f"- {c}" for c in changed_params]
    log_lines += ["\n### Strategy Learnings\n"] + [f"- {l}" for l in analysis.get("strategy_learnings", [])]
    log_lines += ["\n### Per-Stock Notes\n"] + [f"- **{s}**: {n}" for s, n in analysis.get("per_stock_notes", {}).items()]
    log_lines.append("\n---")
    existing = (
        config.INDICATOR_PERFORMANCE_LOG.read_text()
        if config.INDICATOR_PERFORMANCE_LOG.exists()
        else "# Indicator Performance Log\n"
    )
    config.INDICATOR_PERFORMANCE_LOG.write_text(existing + "\n".join(log_lines) + "\n")

    # ── Step 5: Update strategy memory ─────────────────────────────────────────
    current_memory = config.STRATEGY_MEMORY_FILE.read_text() if config.STRATEGY_MEMORY_FILE.exists() else ""
    if len(current_memory) > config.STRATEGY_MEMORY_MAX_CHARS:
        current_memory = current_memory[-config.STRATEGY_MEMORY_MAX_CHARS:]

    memory_response = ai.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=1500,
        system=[{
            "type": "text",
            "text": (
                "You maintain a living trading strategy memory document.\n"
                "Update it with today's learnings. Keep it concise, data-backed, and actionable.\n"
                "Target length: under 1000 words. Summarise older entries if needed.\n\n"
                "Required sections:\n"
                "## General Rules\n## Market Regime Playbook (NORMAL / HIGH_VOL / CRISIS)\n"
                "## Entry Filter Rules (time-of-day, RSI, VWAP slope)\n"
                "## Per-Stock Notes\n## Weight Rationale\n## Recent Lessons (last 5 trading days)\n\n"
                "Preserve valuable prior learnings. Remove stale or contradicted entries. "
                "Use numbers and conditions, not vague prose."
            ),
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": (
            f"Today's analysis ({today}):\n{json.dumps(analysis, indent=2)}\n\n"
            f"Updated weights:\n{json.dumps(new_weights, indent=2)}\n\n"
            f"Updated params:\n{json.dumps(new_params, indent=2)}\n\n"
            f"VIX regime: {json.dumps(regime_info)}\n\n"
            f"Current memory:\n{current_memory or 'No prior memory.'}\n\n"
            "Write the complete updated strategy_memory.md. Keep it under 1000 words."
        )}],
    )

    # Record API usage for second call
    try:
        usage = memory_response.usage
        api_budget.record_usage(
            input_tokens=getattr(usage, "input_tokens", 0),
            output_tokens=getattr(usage, "output_tokens", 0),
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0),
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0),
        )
    except Exception:
        pass

    config.STRATEGY_MEMORY_FILE.write_text(memory_response.content[0].text.strip())
    logger.info(
        f"Learning agent complete. "
        f"W:{analysis.get('winning_trades')}/L:{analysis.get('losing_trades')}, "
        f"P&L=${analysis.get('total_pnl_dollar', 0):.2f} | "
        f"Regime={regime_info.get('regime')} | Params changed: {len(changed_params)}"
    )
```

- [ ] **Step 5: Replace learning_agent.py with re-export shim**

```python
# learning_agent.py — backward-compatible re-exports; real code in learning/
from learning.agent import run_learning_agent
from learning.trade_analysis import (
    _clamp_threshold, _compute_regime_winrate, _aggregate_by_regime,
    _classify_loss_source, _THRESHOLD_BOUNDS, _TRADE_SUMMARY_KEYS,
)
from learning.data import (
    _load_todays_closed_trades, _load_weights, _save_weights, _save_trading_params,
)

__all__ = ["run_learning_agent"]
```

- [ ] **Step 6: Verify tests still pass**

Run: `pytest tests/test_learning.py tests/test_foundation.py -v -m unit`

Expected: all tests pass; `from learning_agent import _clamp_threshold` still works via shim.

- [ ] **Step 7: Commit**

```bash
git add learning/ learning_agent.py
git commit -m "refactor: split learning_agent into learning/ package"
```

---

## Task 4: Extract trading/tiered_tp.py and trading/eod_holds.py from swing_manager.py

**Files:**
- Create: `trading/tiered_tp.py`
- Create: `trading/eod_holds.py`
- Modify: `swing_manager.py` (import from new files, remove moved code)
- Test: `tests/test_swing.py` (run — no changes needed)

- [ ] **Step 1: Create trading/tiered_tp.py**

Move lines 36–104 of `swing_manager.py`:

```python
# trading/tiered_tp.py
"""Pure tiered take-profit logic — no Alpaca calls, no file I/O."""


def _compute_tier_quantities(total_qty: int) -> tuple[int, int, int]:
    """Split total_qty into (tp1_qty, tp2_qty, trailing_qty) at 40/30/30."""
    if total_qty <= 1:
        return total_qty, 0, 0
    if total_qty == 2:
        return 1, 1, 0
    tp1 = max(1, round(total_qty * 0.4))
    tp2 = max(1, round(total_qty * 0.3))
    trailing = total_qty - tp1 - tp2
    if trailing <= 0:
        tp2 = max(1, total_qty - tp1 - 1)
        trailing = total_qty - tp1 - tp2
    if trailing <= 0:
        trailing = 1
        tp2 = total_qty - tp1 - trailing
    return tp1, tp2, trailing


def evaluate_tiered_tp_action(position: dict, current_price: float) -> dict:
    """
    Priority: hard stop > TP1 > TP2 (activates trailing) > trailing drawdown.
    Returns {action, qty, activate_trailing, reason}.
    action in: NONE | CLOSE_TP1 | CLOSE_TP2 | CLOSE_TRAILING | CLOSE_HARD_STOP
    """
    entry = position["entry_price"]
    pnl_pct = (current_price - entry) / entry * 100
    sl_pct  = position.get("stop_loss_pct", 7.0)
    tp1_pct = position.get("tp1_pct", 5.0)
    tp2_pct = position.get("tp2_pct", 10.0)

    remaining_qty = (
        position.get("trailing_qty", 0)
        + (0 if position.get("tp1_hit") else position.get("tp1_qty", 0))
        + (0 if position.get("tp2_hit") else position.get("tp2_qty", 0))
    )
    if pnl_pct <= -sl_pct:
        return {"action": "CLOSE_HARD_STOP", "qty": remaining_qty, "activate_trailing": False,
                "reason": f"hard stop at {pnl_pct:.2f}%"}

    if not position.get("tp1_hit") and pnl_pct >= tp1_pct:
        tp1_qty = position.get("tp1_qty", 0)
        if tp1_qty > 0:
            return {"action": "CLOSE_TP1", "qty": tp1_qty, "activate_trailing": False,
                    "reason": f"TP1 at {pnl_pct:.2f}%"}

    if position.get("tp1_hit") and not position.get("tp2_hit") and pnl_pct >= tp2_pct:
        tp2_qty = position.get("tp2_qty", 0)
        if tp2_qty > 0:
            return {"action": "CLOSE_TP2", "qty": tp2_qty, "activate_trailing": True,
                    "reason": f"TP2 at {pnl_pct:.2f}%, activating trailing stop"}

    if position.get("tp2_hit") and position.get("trailing_stop_active"):
        high_water = position.get("trailing_high_water", entry)
        trailing_qty = position.get("trailing_qty", 0)
        if trailing_qty > 0 and current_price <= high_water * 0.97:
            return {"action": "CLOSE_TRAILING", "qty": trailing_qty, "activate_trailing": False,
                    "reason": f"trailing stop at {pnl_pct:.2f}% ({current_price:.2f} vs HW {high_water:.2f})"}

    return {"action": "NONE", "qty": 0, "activate_trailing": False, "reason": ""}
```

- [ ] **Step 2: Create trading/eod_holds.py**

Move lines 107–243 of `swing_manager.py`:

```python
# trading/eod_holds.py
"""EOD swing qualification logic — pure gate check + orchestrator."""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import config
from earnings_calendar import is_earnings_imminent
from economic_calendar import is_event_risk_window
from trading.tiered_tp import _compute_tier_quantities
from utils.io_utils import load_json

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

_SWING_ELIGIBLE_REGIMES = {"BULL_TREND", "OPPORTUNITY", "CAPITULATION", "CRASH_RECOVERY"}


def qualifies_for_eod_hold(
    symbol: str,
    is_tier1: bool,
    regime: str,
    event_risk_tomorrow: bool,
    earnings_in_2_days: bool,
    current_pnl_pct: float,
    existing_swing_count: int,
    sector_sentiment_score: float,
) -> tuple[bool, str]:
    """Return (True, '') if position qualifies for overnight hold, (False, reason) otherwise."""
    if not is_tier1:
        return False, f"{symbol} not in tier-1 list"
    if regime not in _SWING_ELIGIBLE_REGIMES:
        return False, f"regime {regime} not eligible for swing"
    if event_risk_tomorrow:
        return False, "high-impact event tomorrow morning"
    if earnings_in_2_days and regime != "CAPITULATION":
        return False, "earnings within 2 sessions (not CAPITULATION mode)"
    if current_pnl_pct <= -2.0:
        return False, f"PTJ rule: P&L {current_pnl_pct:.1f}% ≤ -2% (don't hold losers overnight)"
    if existing_swing_count >= config.SWING_MAX_POSITIONS:
        return False, f"swing limit reached ({existing_swing_count}/{config.SWING_MAX_POSITIONS})"
    if sector_sentiment_score < 10:
        return False, f"sector sentiment {sector_sentiment_score:.0f} < +10"
    return True, ""


def evaluate_eod_holds(load_swing_positions, _save_swing_positions, _SWING_LOCK) -> list[str]:
    """
    Called at 3:50 PM ET. Evaluate open day-trade positions for overnight swing hold.
    Returns list of promoted symbols.

    Accepts persistence callables from swing_manager to avoid circular imports.
    """
    from trading_agent import load_open_positions
    from macro_regime import load_macro_regime
    from tier1_config import is_tier1 as _is_tier1, get_sector

    regime_info = load_macro_regime()
    regime = regime_info.get("regime", "NORMAL")
    now = datetime.now(ET)

    open_positions = load_open_positions()
    existing_swings = load_swing_positions()
    existing_count = len(existing_swings)
    promoted = []

    sector_sentiment: dict = {}
    try:
        rr = load_json(config.RESEARCH_REPORT_FILE, {})
        sector_sentiment = rr.get("__meta__", {}).get("sector_sentiment", {})
    except Exception:
        pass

    for symbol, pos in open_positions.items():
        try:
            entry = pos["entry_price"]
            from alpaca_client import AlpacaClient
            alpaca = AlpacaClient()
            current_price = alpaca.get_latest_price(symbol)
            pnl_pct = (current_price - entry) / entry * 100

            event_tomorrow, _ = is_event_risk_window(
                now.replace(hour=9, minute=30, second=0) + __import__("datetime").timedelta(days=1)
            )
            earnings_soon = is_earnings_imminent(symbol, days=2)
            sector = get_sector(symbol)
            sect_score = sector_sentiment.get(sector, 0)

            qualifies, reason = qualifies_for_eod_hold(
                symbol=symbol, is_tier1=_is_tier1(symbol), regime=regime,
                event_risk_tomorrow=event_tomorrow, earnings_in_2_days=earnings_soon,
                current_pnl_pct=pnl_pct,
                existing_swing_count=existing_count + len(promoted),
                sector_sentiment_score=sect_score,
            )
            if not qualifies:
                logger.info(f"SWING SKIP {symbol}: {reason}")
                continue

            capitulation = regime == "CAPITULATION"
            tp1_pct = 10.0 if capitulation else 5.0
            tp2_pct = 20.0 if capitulation else 10.0
            sl_pct  = 12.0 if capitulation else pos.get("stop_loss_pct", 7.0)

            total_qty = int(pos["qty"])
            tp1_qty, tp2_qty, trailing_qty = _compute_tier_quantities(total_qty)

            swing_pos = {
                "symbol": symbol, "entry_price": entry, "qty": total_qty,
                "entry_time": pos.get("entry_time", now.isoformat()),
                "stop_loss_pct": sl_pct, "take_profit_pct": tp2_pct,
                "stop_loss_price": round(entry * (1 - sl_pct / 100), 2),
                "take_profit_price": round(entry * (1 + tp2_pct / 100), 2),
                "tp1_pct": tp1_pct, "tp1_qty": tp1_qty, "tp1_hit": False,
                "tp2_pct": tp2_pct, "tp2_qty": tp2_qty, "tp2_hit": False,
                "trailing_qty": trailing_qty, "trailing_stop_active": False,
                "trailing_high_water": current_price,
                "swing_days_held": 0, "max_swing_days": config.SWING_MAX_DAYS,
                "regime_at_entry": regime, "capitulation_hold": capitulation,
                "hard_deadline": None,
                "claude_reasoning": pos.get("claude_reasoning", ""),
                "order_id": pos.get("order_id"),
                "mode_at_entry": "HYBRID" if not capitulation else "SHORT_TERM",
            }

            with _SWING_LOCK:
                swings = load_swing_positions()
                swings[symbol] = swing_pos
                _save_swing_positions(swings)

            promoted.append(symbol)
            logger.info(
                f"SWING PROMOTE {symbol}: P&L={pnl_pct:+.2f}% | "
                f"TP1={tp1_pct}% TP2={tp2_pct}% SL={sl_pct}% | {regime} regime"
            )
        except Exception as exc:
            logger.warning(f"evaluate_eod_holds: error for {symbol}: {exc}")

    return promoted
```

- [ ] **Step 3: Update swing_manager.py to import from new files**

At the top of `swing_manager.py`, replace the tiered-TP and EOD-holds function bodies with imports:

```python
# swing_manager.py (top section — add these imports after existing imports)
from trading.tiered_tp import _compute_tier_quantities, evaluate_tiered_tp_action
from trading.eod_holds import qualifies_for_eod_hold, _SWING_ELIGIBLE_REGIMES
```

Then delete the function bodies for `_compute_tier_quantities`, `evaluate_tiered_tp_action`, `qualifies_for_eod_hold`, and the body of `evaluate_eod_holds` (replace with):

```python
def evaluate_eod_holds() -> list[str]:
    from trading.eod_holds import evaluate_eod_holds as _eval
    return _eval(load_swing_positions, _save_swing_positions, _SWING_LOCK)
```

- [ ] **Step 4: Verify tests still pass**

Run: `pytest tests/test_swing.py tests/test_foundation.py -v -m unit`

Expected: all tests pass; `evaluate_tiered_tp_action` and `qualifies_for_eod_hold` still importable from `swing_manager`.

- [ ] **Step 5: Commit**

```bash
git add trading/tiered_tp.py trading/eod_holds.py swing_manager.py
git commit -m "refactor: extract tiered_tp and eod_holds from swing_manager"
```

---

## Task 5: Extract utils/ helpers from main.py

**Files:**
- Create: `utils/instance_lock.py`
- Create: `utils/startup.py`
- Create: `utils/bot_health.py`
- Create: `utils/catchup.py`
- Create: `scheduler_jobs.py`
- Modify: `main.py` (import from new files, remove moved code)

- [ ] **Step 1: Create utils/instance_lock.py**

Move lines 67–121 of `main.py` (instance lock + PID management):

```python
# utils/instance_lock.py
import fcntl
import logging
import os
import signal
import time
from pathlib import Path

import config

logger = logging.getLogger(__name__)

PID_FILE  = config.BASE_DIR / "trading_bot.pid"
LOCK_FILE = config.BASE_DIR / "trading_bot.lock"
_lock_fh  = None


def acquire_instance_lock() -> None:
    """Exit immediately if another bot process is already running."""
    global _lock_fh
    _lock_fh = open(LOCK_FILE, "w")
    try:
        fcntl.flock(_lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fh.write(str(os.getpid()))
        _lock_fh.flush()
    except IOError:
        print("[trading_bot] Another instance is already running — exiting.", flush=True)
        import sys
        sys.exit(0)


def kill_existing_instance() -> None:
    if not PID_FILE.exists():
        return
    try:
        old_pid = int(PID_FILE.read_text().strip())
        if old_pid == os.getpid():
            return
        os.kill(old_pid, 0)
        logger.info(f"Killing existing instance PID {old_pid}…")
        os.kill(old_pid, signal.SIGTERM)
        time.sleep(3)
        try:
            os.kill(old_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        logger.info(f"Previous instance PID {old_pid} terminated.")
    except (ValueError, ProcessLookupError):
        pass


def write_pid() -> None:
    PID_FILE.write_text(str(os.getpid()))


def clear_pid() -> None:
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass
```

- [ ] **Step 2: Create utils/startup.py**

Move `_validate_state_files`, `_cleanup_stale_files`, `_log_capability_summary`, `_exit_with_alert`:

```python
# utils/startup.py
import json
import logging
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config
import telegram_notify

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


def validate_state_files() -> None:
    """Check JSON state files on startup; reset corrupt ones and alert via Telegram."""
    candidates = [
        (config.TRADE_JOURNAL_FILE,     []),
        (config.OPEN_POSITIONS_FILE,    {}),
        (config.SWING_POSITIONS_FILE,   {}),
        (config.INDICATOR_WEIGHTS_FILE, {}),
    ]
    for path, default in candidates:
        if not path.exists():
            continue
        try:
            json.loads(path.read_text())
        except (json.JSONDecodeError, ValueError):
            backup = path.with_suffix(".corrupt.json")
            path.rename(backup)
            path.write_text(json.dumps(default))
            logger.error(f"CORRUPT state file reset: {path.name} → backup {backup.name}")
            telegram_notify.notify_file_corruption(path.name)


def cleanup_stale_files() -> None:
    """Delete stale .corrupt.json backups, leftover .tmp files, and prior-day research report."""
    cutoff = datetime.now(ET) - timedelta(days=config.CORRUPT_BACKUP_MAX_DAYS)
    for f in config.BASE_DIR.glob("*.corrupt.json"):
        try:
            if datetime.fromtimestamp(f.stat().st_mtime, tz=ET) < cutoff:
                f.unlink()
                logger.info(f"Deleted stale backup: {f.name}")
        except Exception as exc:
            logger.warning(f"Could not delete {f.name}: {exc}")

    for f in config.BASE_DIR.glob("*.tmp"):
        try:
            f.unlink()
            logger.info(f"Deleted leftover tmp file: {f.name}")
        except Exception as exc:
            logger.warning(f"Could not delete {f.name}: {exc}")

    try:
        if config.RESEARCH_REPORT_FILE.exists():
            rr = json.loads(config.RESEARCH_REPORT_FILE.read_text())
            meta_ts = rr.get("__meta__", {}).get("timestamp", "")
            if meta_ts:
                report_dt = datetime.fromisoformat(meta_ts)
                if report_dt.tzinfo is None:
                    report_dt = report_dt.replace(tzinfo=ET)
                if report_dt < datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0):
                    config.RESEARCH_REPORT_FILE.unlink()
                    logger.info("Deleted stale research_report.json (from prior trading day)")
    except Exception as exc:
        logger.debug(f"research_report.json prune skipped: {exc}")

    try:
        from trading_agent import archive_old_journal_entries
        archive_old_journal_entries()
    except Exception as exc:
        logger.warning(f"Journal archival failed: {exc}")


def exit_with_alert(message: str) -> None:
    """Send a Telegram alert then exit. Use instead of sys.exit() in main.py."""
    logger.critical(f"STARTUP EXIT: {message}")
    try:
        telegram_notify.notify_startup_error(message)
    except Exception:
        pass
    sys.exit(1)
```

- [ ] **Step 3: Create utils/bot_health.py**

Move `_reset_bot_health`, `_check_crash_loop`, `_reconcile_positions`:

```python
# utils/bot_health.py
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import config
import telegram_notify
from utils.io_utils import load_json, save_json

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

_BOT_HEALTH_FILE = config.BASE_DIR / "bot_health.json"


def reset_bot_health() -> None:
    save_json(_BOT_HEALTH_FILE, {
        "start_time": datetime.now(ET).isoformat(),
        "crash_count": 0,
        "last_crash": None,
    })


def check_crash_loop() -> None:
    """Alert via Telegram if the bot has crashed ≥ 3 times today."""
    health = load_json(_BOT_HEALTH_FILE, {})
    crash_count = health.get("crash_count", 0)
    if crash_count >= 3:
        telegram_notify.notify_crash_loop(crash_count)
        logger.warning(f"CRASH LOOP DETECTED: {crash_count} restarts today")


def reconcile_positions(alpaca) -> None:
    """Best-effort sync of open_positions.json with live Alpaca positions on startup."""
    try:
        live = {p.symbol: p for p in alpaca.list_positions()}
        saved = load_json(config.OPEN_POSITIONS_FILE, {})
        orphaned = [sym for sym in saved if sym not in live]
        if orphaned:
            logger.warning(f"RECONCILE: orphaned positions in JSON not in Alpaca: {orphaned}")
            for sym in orphaned:
                del saved[sym]
            save_json(config.OPEN_POSITIONS_FILE, saved)
        missing = [sym for sym in live if sym not in saved]
        if missing:
            logger.warning(f"RECONCILE: positions in Alpaca not in JSON: {missing}")
    except Exception as exc:
        logger.warning(f"Position reconcile failed: {exc}")
```

- [ ] **Step 4: Create utils/catchup.py**

Move `_catchup_missed_agents`, `_catchup_daily_report`:

```python
# utils/catchup.py
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


def catchup_missed_agents() -> None:
    """
    On startup between 9:15 AM and market close, run any pre-market agents
    that may have been missed (e.g., bot restarted at 9:45 AM).
    """
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return
    hour = now.hour

    if hour >= 8:
        try:
            from earnings_calendar import refresh_earnings_calendar
            from tier1_config import TIER1_NAMES
            import config
            watch_list = []
            if config.WATCH_LIST_FILE.exists():
                watch_list = [t.strip() for t in config.WATCH_LIST_FILE.read_text().splitlines() if t.strip()]
            refresh_earnings_calendar(list(dict.fromkeys(watch_list + list(TIER1_NAMES.keys()))))
            logger.info("Catchup: earnings_calendar done")
        except Exception as exc:
            logger.warning(f"Catchup earnings_calendar failed: {exc}")

    if hour >= 9:
        try:
            from macro_regime import fetch_macro_regime
            fetch_macro_regime()
            logger.info("Catchup: macro_regime done")
        except Exception as exc:
            logger.warning(f"Catchup macro_regime failed: {exc}")

    if hour >= 9 and (now.minute >= 15 or hour > 9):
        try:
            from research_agent import run_research_agent
            import config
            if config.WATCH_LIST_FILE.exists():
                watch_list = [t.strip() for t in config.WATCH_LIST_FILE.read_text().splitlines() if t.strip()]
                if watch_list:
                    run_research_agent(watch_list)
                    logger.info("Catchup: research_agent done")
        except Exception as exc:
            logger.warning(f"Catchup research_agent failed: {exc}")


def catchup_daily_report() -> None:
    """Send the daily report if bot restarted after 4:00 PM and report was not sent."""
    now = datetime.now(ET)
    if now.weekday() >= 5 or now.hour < 16:
        return
    try:
        from daily_report import send_daily_report
        send_daily_report()
        logger.info("Catchup: daily_report sent")
    except Exception as exc:
        logger.warning(f"Catchup daily_report failed: {exc}")
```

- [ ] **Step 5: Create scheduler_jobs.py**

Move all `job_*` functions from `main.py`:

```python
# scheduler_jobs.py
"""APScheduler job wrappers. Each job_* function is called by main.py's setup_scheduler."""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import config
import telegram_notify

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


def _job_guard(name: str, fn) -> None:
    """Run fn(); send a Telegram alert on exception, then re-raise."""
    try:
        fn()
    except Exception as exc:
        telegram_notify.notify_job_failure(name, f"{type(exc).__name__}: {exc}")
        raise


def job_earnings_calendar():
    logger.info("━━━ EARNINGS CALENDAR ━━━")
    def _run():
        from earnings_calendar import refresh_earnings_calendar
        from tier1_config import TIER1_NAMES
        watch_list = []
        if config.WATCH_LIST_FILE.exists():
            watch_list = [t.strip() for t in config.WATCH_LIST_FILE.read_text().splitlines() if t.strip()]
        refresh_earnings_calendar(list(dict.fromkeys(watch_list + list(TIER1_NAMES.keys()))))
    _job_guard("earnings_calendar", _run)


def job_economic_calendar():
    logger.info("━━━ ECONOMIC CALENDAR ━━━")
    from economic_calendar import refresh_economic_calendar
    _job_guard("economic_calendar", refresh_economic_calendar)


def job_macro_regime():
    logger.info("━━━ MACRO REGIME ━━━")
    from macro_regime import fetch_macro_regime
    _job_guard("macro_regime", fetch_macro_regime)


def job_fred_cache():
    logger.info("━━━ FRED CACHE REFRESH ━━━")
    from macro_regime import refresh_fred_cache
    _job_guard("fred_cache", refresh_fred_cache)


def job_volatility_screener():
    logger.info("━━━ VOLATILITY SCREENER ━━━")
    from volatility_screener import run_volatility_screener
    _job_guard("volatility_screener", run_volatility_screener)


def job_watch_list():
    logger.info("━━━ WATCH LIST AGENT ━━━")
    def _run():
        from trading_agent import reset_daily_closed
        reset_daily_closed()
        from watch_list_agent import run_watch_list_agent
        run_watch_list_agent()
    _job_guard("watch_list", _run)


def job_research():
    logger.info("━━━ RESEARCH AGENT ━━━")
    def _run():
        from research_agent import run_research_agent
        if config.WATCH_LIST_FILE.exists():
            watch_list = [t.strip() for t in config.WATCH_LIST_FILE.read_text().splitlines() if t.strip()]
        else:
            from watch_list_agent import _load_universe
            watch_list = _load_universe()[:config.WATCH_LIST_MAX]
        run_research_agent(watch_list)
    _job_guard("research", _run)


def job_trading():
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return
    if now.hour < 9 or (now.hour == 9 and now.minute < 30):
        return
    if now.hour > 15 or (now.hour == 15 and now.minute >= 55):
        return
    logger.info("━━━ TRADING CYCLE ━━━")
    from trading_agent import run_trading_cycle
    _job_guard("trading_cycle", run_trading_cycle)


def job_position_monitor():
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return
    if now.hour < 9 or (now.hour == 9 and now.minute < 30):
        return
    from trading_agent import run_position_monitor
    _job_guard("position_monitor", run_position_monitor)


def job_swing_manager():
    from swing_manager import monitor_swing_positions
    _job_guard("swing_manager", monitor_swing_positions)


def job_eod_holds():
    logger.info("━━━ EOD HOLDS ━━━")
    from swing_manager import evaluate_eod_holds
    _job_guard("eod_holds", evaluate_eod_holds)


def job_force_close():
    logger.info("━━━ FORCE CLOSE ━━━")
    from trading_agent import force_close_all
    _job_guard("force_close", force_close_all)


def job_learning_agent():
    logger.info("━━━ LEARNING AGENT ━━━")
    from learning_agent import run_learning_agent
    _job_guard("learning_agent", run_learning_agent)


def job_daily_report():
    logger.info("━━━ DAILY REPORT ━━━")
    from daily_report import send_daily_report
    _job_guard("daily_report", send_daily_report)


def job_gap_check(alpaca_client):
    logger.info("━━━ GAP CHECK ━━━")
    from swing_manager import run_gap_check
    _job_guard("gap_check", lambda: run_gap_check(alpaca_client))


def job_heartbeat(last_heartbeat_ts: list) -> None:
    last_heartbeat_ts[0] = __import__("time").time()
    from trading_agent import load_open_positions
    count = len(load_open_positions())
    logger.info(f"♥ heartbeat — open positions: {count}")
```

- [ ] **Step 6: Update main.py to import from utils/ and scheduler_jobs.py**

In `main.py`, replace the bodies of all moved functions with imports at the top:

```python
# add to main.py imports section
from utils.instance_lock import acquire_instance_lock, kill_existing_instance, write_pid, clear_pid
from utils.startup import validate_state_files, cleanup_stale_files, exit_with_alert
from utils.bot_health import reset_bot_health, check_crash_loop, reconcile_positions
from utils.catchup import catchup_missed_agents, catchup_daily_report
from scheduler_jobs import (
    _job_guard, job_earnings_calendar, job_economic_calendar, job_macro_regime,
    job_fred_cache, job_volatility_screener, job_watch_list, job_research,
    job_trading, job_position_monitor, job_swing_manager, job_eod_holds,
    job_force_close, job_learning_agent, job_daily_report, job_gap_check, job_heartbeat,
)
```

Then remove the duplicate function bodies from `main.py` for all moved functions, keeping only:
- Logging setup (lines 1–42)
- `_watchdog` function
- `setup_scheduler` 
- `_handle_shutdown`
- `main`

- [ ] **Step 7: Verify full test suite still passes**

Run: `pytest tests/ -v -m unit`

Expected: all tests pass; `main.py` now under 300 lines.

- [ ] **Step 8: Verify line counts**

```bash
wc -l main.py indicators/*.py research/*.py learning/*.py trading/tiered_tp.py trading/eod_holds.py scheduler_jobs.py utils/instance_lock.py utils/startup.py utils/bot_health.py utils/catchup.py
```

Expected: every file ≤ 300 lines.

- [ ] **Step 9: Commit**

```bash
git add utils/ scheduler_jobs.py main.py
git commit -m "refactor: extract job functions and utilities from main.py"
```

---

## Self-Review

**Spec coverage:**
- ✅ All five oversized files split (indicator_engine 555→≤160/file, research_agent 589→≤230/file, learning_agent 422→≤210/file, swing_manager 442→≤260/file after extract, main.py 838→≤300/file)
- ✅ Backward-compatible re-exports keep all existing callers working with zero import changes
- ✅ Test files importing private functions (`_clamp_threshold`, `_is_parabolic`, etc.) continue to work via shims
- ✅ Every new file has a single stated responsibility
- ✅ Signal purity maintained: `trading/tiered_tp.py` and `indicators/*` are pure (no Alpaca, no I/O)
- ✅ `api_budget.record_usage()` added to `learning/agent.py` for both Claude calls (addresses token-efficiency Task 4)

**Placeholder scan:** None found — all steps contain complete code.

**Type consistency:** `evaluate_eod_holds` in `trading/eod_holds.py` accepts the persistence callables as parameters to avoid circular import with `swing_manager.py`; the wrapper in `swing_manager.py` passes them correctly.
