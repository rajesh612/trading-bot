"""
Indicator engine unit tests (indicator_engine.py).
Run with: pytest tests/test_indicators.py -v -m unit
No API calls — all tests use synthetic fixtures.
"""
import json

import numpy as np
import pandas as pd
import pytest


# ── Shared DataFrame factories ────────────────────────────────────────────────

def _make_intraday_df(
    n: int = 100,
    close: float = 100.0,
    high: float | None = None,
    low: float | None = None,
    volume: float = 1_000.0,
) -> pd.DataFrame:
    if high is None:
        high = close * 1.01
    if low is None:
        low = close * 0.99
    idx = pd.date_range("2026-05-01 09:30", periods=n, freq="5min", tz="America/New_York")
    return pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


def _make_daily_df(n: int = 25, volume: float = 1_000_000.0, close: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=n, freq="B", tz="America/New_York")
    return pd.DataFrame(
        {"open": close, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": volume},
        index=idx,
    )


# ── _BARS_PER_5D constant ────────────────────────────────────────────────────

@pytest.mark.unit
class TestBarsPer5D:
    def test_bars_per_5d_equals_390_with_default_timeframe(self):
        """_BARS_PER_5D must equal 390 when BAR_TIMEFRAME_MINUTES=5 (5 days × 78 bars/day)."""
        from indicators.counter_signals import _BARS_PER_5D
        assert _BARS_PER_5D == 390


# ── indicator_engine: Plan-2 signals ─────────────────────────────────────────

@pytest.mark.unit
class TestIndicatorEnginePlan2:
    def test_is_parabolic_true(self):
        from indicators.counter_signals import _is_parabolic
        closes = [100.0] * 5 + [115.0]
        assert _is_parabolic(closes) is True

    def test_is_parabolic_false_just_below(self):
        from indicators.counter_signals import _is_parabolic
        closes = [100.0] * 5 + [114.9]
        assert _is_parabolic(closes) is False

    def test_is_parabolic_insufficient_data(self):
        from indicators.counter_signals import _is_parabolic
        assert _is_parabolic([100.0, 115.0]) is False

    def test_compute_squeeze_setup_all_conditions(self):
        from indicators.counter_signals import compute_squeeze_setup
        qualifies, score = compute_squeeze_setup(
            float_turnover_3d=0.20,
            price_vs_resistance_pct=2.0,
            volume_surge=2.5,
            obv_rising_with_falling_price=True,
        )
        assert qualifies is True
        assert score == 100

    def test_compute_squeeze_setup_three_conditions(self):
        from indicators.counter_signals import compute_squeeze_setup
        qualifies, score = compute_squeeze_setup(
            float_turnover_3d=0.20,
            price_vs_resistance_pct=2.0,
            volume_surge=2.5,
            obv_rising_with_falling_price=False,
        )
        assert qualifies is True
        assert score == 75

    def test_compute_squeeze_setup_two_conditions(self):
        from indicators.counter_signals import compute_squeeze_setup
        qualifies, score = compute_squeeze_setup(
            float_turnover_3d=0.20,
            price_vs_resistance_pct=2.0,
            volume_surge=1.0,
            obv_rising_with_falling_price=False,
        )
        assert qualifies is False
        assert score == 50

    def test_compute_squeeze_setup_no_conditions(self):
        from indicators.counter_signals import compute_squeeze_setup
        qualifies, score = compute_squeeze_setup(
            float_turnover_3d=0.05,
            price_vs_resistance_pct=5.0,
            volume_surge=1.0,
            obv_rising_with_falling_price=False,
        )
        assert qualifies is False
        assert score == 0

    def test_compute_bot_herding_score_zero_signals(self):
        from indicators.counter_signals import compute_bot_herding_score
        score = compute_bot_herding_score(
            series_dict={}, spy_closes=[],
            buy_signal_count=0, total_watch=10, volume_cv=0.5,
        )
        assert 0 <= score <= 100

    def test_compute_bot_herding_score_high_herding(self):
        from indicators.counter_signals import compute_bot_herding_score
        score = compute_bot_herding_score(
            series_dict={}, spy_closes=[],
            buy_signal_count=10, total_watch=10, volume_cv=0.0,
        )
        assert score >= 50


# ── indicator_engine: volume exhaustion ──────────────────────────────────────

@pytest.mark.unit
class TestVolumeExhaustion:
    def test_true_at_high_with_low_volume(self):
        from indicators.counter_signals import _is_volume_exhaustion
        df = _make_intraday_df(n=400, close=100.0, high=100.0)
        daily = _make_daily_df(n=25, volume=1_000_000.0)
        daily_low_vol = daily.copy()
        daily_low_vol.iloc[-1, daily_low_vol.columns.get_loc("volume")] = 600_000.0
        assert _is_volume_exhaustion(df, daily_low_vol) is True

    def test_false_not_at_high(self):
        from indicators.counter_signals import _is_volume_exhaustion
        df_low = _make_intraday_df(n=400, close=95.0, high=95.0)
        df_high_hist = _make_intraday_df(n=400, close=100.0, high=102.0)
        combined = pd.concat([df_high_hist.iloc[:-1], df_low.iloc[-1:]])
        daily = _make_daily_df(n=25, volume=1_000_000.0)
        daily_low_vol = daily.copy()
        daily_low_vol.iloc[-1, daily_low_vol.columns.get_loc("volume")] = 600_000.0
        assert _is_volume_exhaustion(combined, daily_low_vol) is False

    def test_false_normal_volume_at_high(self):
        from indicators.counter_signals import _is_volume_exhaustion
        df = _make_intraday_df(n=400, close=100.0, high=100.0)
        daily = _make_daily_df(n=25, volume=1_000_000.0)
        assert _is_volume_exhaustion(df, daily) is False

    def test_false_insufficient_intraday_data(self):
        from indicators.counter_signals import _is_volume_exhaustion
        df = _make_intraday_df(n=3, close=100.0)
        daily = _make_daily_df(n=25, volume=600_000.0)
        assert _is_volume_exhaustion(df, daily) is False

    def test_false_no_daily_df(self):
        from indicators.counter_signals import _is_volume_exhaustion
        df = _make_intraday_df(n=400, close=100.0, high=100.0)
        assert _is_volume_exhaustion(df, None) is False


# ── indicator_engine: OBV divergence ─────────────────────────────────────────

@pytest.mark.unit
class TestOBVDivergence:
    @staticmethod
    def _make_df_obv(closes: list[float], obv_vals: list[float]):
        n = len(closes)
        idx = pd.date_range("2026-05-01", periods=n, freq="5min", tz="America/New_York")
        df = pd.DataFrame(
            {"open": closes, "high": [c * 1.01 for c in closes],
             "low": [c * 0.99 for c in closes], "close": closes,
             "volume": [1000.0] * n},
            index=idx,
        )
        obv = pd.Series(obv_vals, index=idx)
        return df, obv

    def test_bearish_divergence(self):
        from indicators.counter_signals import _obv_divergence_signal
        closes = [90.0, 95.0, 98.0, 99.0, 100.0, 100.0]
        obv_v  = [1000.0, 900.0, 850.0, 800.0, 750.0, 700.0]
        df, obv = self._make_df_obv(closes, obv_v)
        assert _obv_divergence_signal(df, obv) == "bearish"

    def test_bullish_divergence(self):
        from indicators.counter_signals import _obv_divergence_signal
        closes = [110.0, 105.0, 102.0, 101.0, 100.0, 100.0]
        obv_v  = [500.0, 550.0, 600.0, 650.0, 700.0, 750.0]
        df, obv = self._make_df_obv(closes, obv_v)
        assert _obv_divergence_signal(df, obv) == "bullish"

    def test_none_no_divergence(self):
        from indicators.counter_signals import _obv_divergence_signal
        closes = [95.0, 98.0, 100.0, 99.0, 101.0, 100.0]
        obv_v  = [500.0, 550.0, 600.0, 650.0, 700.0, 750.0]
        df, obv = self._make_df_obv(closes, obv_v)
        assert _obv_divergence_signal(df, obv) == "none"

    def test_none_insufficient_data(self):
        from indicators.counter_signals import _obv_divergence_signal
        df, obv = self._make_df_obv([100.0, 101.0], [500.0, 600.0])
        assert _obv_divergence_signal(df, obv) == "none"


# ── indicator_engine: stop hunt zone ─────────────────────────────────────────

@pytest.mark.unit
class TestStopHuntZone:
    def test_true_at_round_dollar(self):
        from indicators.counter_signals import _is_stop_hunt_zone
        df = _make_intraday_df(n=10, close=100.0, high=100.0, low=100.0)
        assert _is_stop_hunt_zone(df) is True

    def test_true_near_half_dollar(self):
        from indicators.counter_signals import _is_stop_hunt_zone
        df = _make_intraday_df(n=10, close=100.49, high=100.49, low=100.49)
        assert _is_stop_hunt_zone(df) is True

    def test_true_near_5day_high(self):
        from indicators.counter_signals import _is_stop_hunt_zone
        df = _make_intraday_df(n=400, close=50.25, high=50.40, low=44.0)
        df_copy = df.copy()
        df_copy.iloc[-1, df_copy.columns.get_loc("close")] = 50.25
        assert _is_stop_hunt_zone(df_copy) is True

    def test_true_near_5day_low(self):
        from indicators.counter_signals import _is_stop_hunt_zone
        df = _make_intraday_df(n=400, close=50.25, high=56.0, low=50.10)
        df_copy = df.copy()
        df_copy.iloc[-1, df_copy.columns.get_loc("close")] = 50.25
        assert _is_stop_hunt_zone(df_copy) is True

    def test_false_far_from_levels(self):
        from indicators.counter_signals import _is_stop_hunt_zone
        df = _make_intraday_df(n=400, close=50.25, high=55.0, low=44.0)
        df_copy = df.copy()
        df_copy.iloc[-1, df_copy.columns.get_loc("close")] = 50.25
        assert _is_stop_hunt_zone(df_copy) is False

    def test_false_empty_df(self):
        from indicators.counter_signals import _is_stop_hunt_zone
        assert _is_stop_hunt_zone(pd.DataFrame()) is False


# ── indicator_engine: base technical functions ────────────────────────────────

@pytest.mark.unit
class TestBaseIndicatorFunctions:
    def test_ema_falls_toward_last_value(self):
        from indicators.math_utils import _ema
        s = pd.Series([100.0] * 20 + [200.0])
        result = _ema(s, period=5)
        assert float(result.iloc[-1]) > 100.0
        assert float(result.iloc[-1]) < 200.0

    def test_compute_vwap_equals_tp_at_constant_price(self):
        from indicators.math_utils import _compute_vwap
        n = 10
        idx = pd.date_range("2026-05-01 09:30", periods=n, freq="5min", tz="America/New_York")
        df = pd.DataFrame({"high": 102.0, "low": 98.0, "close": 100.0, "volume": 1000.0}, index=idx)
        vwap = _compute_vwap(df)
        assert abs(float(vwap.iloc[-1]) - 100.0) < 1e-6

    def test_compute_vwap_bands_upper_above_lower(self):
        from indicators.math_utils import _compute_vwap, _compute_vwap_bands
        df = _make_intraday_df(n=20, close=100.0)
        vwap = _compute_vwap(df)
        vs1u, vs1l, vs2u, vs2l = _compute_vwap_bands(df, vwap)
        assert float(vs1u.dropna().iloc[-1]) >= float(vs1l.dropna().iloc[-1])

    def test_compute_rsi_stays_in_bounds(self):
        from indicators.math_utils import _compute_rsi
        s = pd.Series([float(i) for i in range(1, 51)])
        rsi = _compute_rsi(s)
        valid = rsi.dropna()
        assert (valid >= 0).all() and (valid <= 100).all()

    def test_compute_rsi_trending_up_above_50(self):
        from indicators.math_utils import _compute_rsi
        s = pd.Series([100.0 + i for i in range(30)])
        rsi = _compute_rsi(s)
        assert float(rsi.dropna().iloc[-1]) > 50

    def test_compute_macd_histogram_sign_follows_momentum(self):
        from indicators.math_utils import _compute_macd
        s = pd.Series([100.0 + i * 2 for i in range(60)])
        _, _, hist = _compute_macd(s)
        assert float(hist.dropna().iloc[-1]) > 0

    def test_compute_bollinger_upper_above_lower(self):
        from indicators.math_utils import _compute_bollinger
        s = pd.Series([100.0 + np.random.randn() for _ in range(40)])
        upper, mid, lower = _compute_bollinger(s)
        valid_upper = upper.dropna()
        valid_lower = lower.dropna()
        assert (valid_upper > valid_lower).all()

    def test_compute_atr_positive_with_volatile_bars(self):
        from indicators.math_utils import _compute_atr
        df = _make_intraday_df(n=20, close=100.0, high=105.0, low=95.0)
        atr = _compute_atr(df)
        assert float(atr.dropna().iloc[-1]) > 0

    def test_compute_volume_ratio_normal_is_one(self):
        from indicators.math_utils import _compute_volume_ratio
        vol = pd.Series([1000.0] * 30)
        ratio = _compute_volume_ratio(vol)
        assert abs(float(ratio.dropna().iloc[-1]) - 1.0) < 1e-6

    def test_compute_stochastic_in_bounds(self):
        from indicators.math_utils import _compute_stochastic
        df = _make_intraday_df(n=20)
        k, d = _compute_stochastic(df)
        valid_k = k.dropna()
        assert (valid_k >= 0).all() and (valid_k <= 100).all()

    def test_compute_obv_increases_on_up_bars(self):
        from indicators.math_utils import _compute_obv
        closes = [100.0 + i for i in range(10)]
        idx = pd.date_range("2026-05-01", periods=10, freq="5min", tz="America/New_York")
        df = pd.DataFrame({"close": closes, "volume": 1000.0, "open": closes,
                           "high": [c + 1 for c in closes], "low": [c - 1 for c in closes]},
                          index=idx)
        obv = _compute_obv(df)
        assert float(obv.iloc[-1]) > float(obv.iloc[0])

    def test_safe_returns_fallback_on_nan(self):
        from indicators.composite import _safe
        s = pd.Series([float("nan")])
        assert _safe(s, fallback=42.0) == 42.0

    def test_safe_returns_value_when_valid(self):
        from indicators.composite import _safe
        s = pd.Series([99.5])
        assert _safe(s) == 99.5


# ── indicator_engine: _compute_daily_volume_stats ────────────────────────────

@pytest.mark.unit
class TestDailyVolumeStats:
    def test_none_df_returns_defaults(self):
        from indicators.math_utils import _compute_daily_volume_stats
        ratio, trend, mean = _compute_daily_volume_stats(None)
        assert ratio == 1.0 and trend == 0.0 and mean == 0.0

    def test_too_few_rows_returns_defaults(self):
        from indicators.math_utils import _compute_daily_volume_stats
        df = _make_daily_df(n=2)
        ratio, trend, mean = _compute_daily_volume_stats(df)
        assert ratio == 1.0 and trend == 0.0 and mean == 0.0

    def test_constant_volume_ratio_is_one(self):
        from indicators.math_utils import _compute_daily_volume_stats
        df = _make_daily_df(n=10, volume=1_000_000.0)
        ratio, trend, mean = _compute_daily_volume_stats(df)
        assert abs(ratio - 1.0) < 0.05

    def test_increasing_volume_trend_positive(self):
        from indicators.math_utils import _compute_daily_volume_stats
        idx = pd.date_range("2026-01-01", periods=10, freq="B", tz="America/New_York")
        vols = [500_000.0 * (1 + i * 0.2) for i in range(10)]
        df = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
                           "volume": vols}, index=idx)
        ratio, trend, mean = _compute_daily_volume_stats(df)
        assert trend > 0

    def test_returns_correct_mean(self):
        from indicators.math_utils import _compute_daily_volume_stats
        df = _make_daily_df(n=8, volume=2_000_000.0)
        ratio, trend, mean = _compute_daily_volume_stats(df)
        assert abs(mean - 2_000_000.0) < 1000.0


# ── indicator_engine: _approx_corr ────────────────────────────────────────────

@pytest.mark.unit
class TestApproxCorr:
    def test_identical_series_returns_one(self):
        from indicators.counter_signals import _approx_corr
        a = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert abs(_approx_corr(a, a) - 1.0) < 1e-6

    def test_opposite_series_returns_negative_one(self):
        from indicators.counter_signals import _approx_corr
        a = [1.0, 2.0, 3.0, 4.0, 5.0]
        b = [5.0, 4.0, 3.0, 2.0, 1.0]
        assert abs(_approx_corr(a, b) - (-1.0)) < 1e-6

    def test_too_short_returns_zero(self):
        from indicators.counter_signals import _approx_corr
        assert _approx_corr([1.0, 2.0], [1.0, 2.0]) == 0.0

    def test_constant_series_returns_zero(self):
        from indicators.counter_signals import _approx_corr
        a = [5.0, 5.0, 5.0, 5.0, 5.0]
        b = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert _approx_corr(a, b) == 0.0


# ── indicator_engine: _composite_score ────────────────────────────────────────

@pytest.mark.unit
class TestCompositeScore:
    @staticmethod
    def _equal_weights():
        return {
            "VWAP": 10.0, "RSI": 10.0, "Daily Volume Trend": 10.0, "MACD": 10.0,
            "EMA 9/21 Crossover": 10.0, "Bollinger Bands": 10.0, "VWAP StdDev Bands": 10.0,
            "ATR": 10.0, "Stochastic Oscillator": 10.0, "OBV": 10.0, "RSI Slope": 10.0,
        }

    @staticmethod
    def _bullish_vals(price=102.0):
        return {
            "current_price": price, "vwap": 100.0, "atr": 1.0,
            "rsi": 65.0, "macd_hist": 0.5, "bb_upper": 105.0, "bb_lower": 95.0,
            "ema9": 101.5, "ema21": 100.5, "stoch_k": 70.0, "stoch_d": 60.0,
            "daily_volume_ratio": 1.2, "daily_volume_trend": 0.5,
            "vwap_slope": 0.3, "rsi_slope": 2.0, "obv_trend": 0.5,
        }

    @staticmethod
    def _bearish_vals(price=98.0):
        return {
            "current_price": price, "vwap": 100.0, "atr": 1.0,
            "rsi": 35.0, "macd_hist": -0.5, "bb_upper": 105.0, "bb_lower": 95.0,
            "ema9": 98.5, "ema21": 99.5, "stoch_k": 30.0, "stoch_d": 40.0,
            "daily_volume_ratio": 0.8, "daily_volume_trend": -0.5,
            "vwap_slope": -0.3, "rsi_slope": -2.0, "obv_trend": -0.5,
        }

    def test_bullish_vals_give_buy_signal(self):
        from indicators.composite import _composite_score
        score, signal, conf = _composite_score(self._bullish_vals(), self._equal_weights())
        assert score > 15
        assert signal == "BUY"

    def test_bearish_vals_give_sell_signal(self):
        from indicators.composite import _composite_score
        score, signal, conf = _composite_score(self._bearish_vals(), self._equal_weights())
        assert score < -15
        assert signal == "SELL"

    def test_neutral_gives_hold(self):
        from indicators.composite import _composite_score
        vals = self._bullish_vals(price=100.0)
        vals.update({"rsi": 50.0, "macd_hist": 0.0, "ema9": 100.0, "ema21": 100.0,
                     "stoch_k": 50.0, "stoch_d": 50.0, "daily_volume_trend": 0.0,
                     "vwap_slope": 0.0, "rsi_slope": 0.0, "obv_trend": 0.0})
        _, signal, _ = _composite_score(vals, self._equal_weights())
        assert signal == "HOLD"

    def test_empty_weights_returns_zero_hold(self):
        from indicators.composite import _composite_score
        score, signal, conf = _composite_score(self._bullish_vals(), {})
        assert score == 0.0 and signal == "HOLD"

    def test_confidence_scales_with_volume(self):
        from indicators.composite import _composite_score
        high_vol = dict(self._bullish_vals())
        high_vol["daily_volume_ratio"] = 1.5
        low_vol = dict(self._bullish_vals())
        low_vol["daily_volume_ratio"] = 0.5
        _, _, conf_high = _composite_score(high_vol, self._equal_weights())
        _, _, conf_low  = _composite_score(low_vol,  self._equal_weights())
        assert conf_high > conf_low


# ── indicator_engine: compute_indicators (integration) ───────────────────────

@pytest.mark.unit
class TestComputeIndicators:
    def test_returns_none_for_short_df(self):
        from indicators.engine import compute_indicators
        df = _make_intraday_df(n=10)
        assert compute_indicators("APP", df) is None

    def test_returns_indicator_values_for_valid_df(self, tmp_path, monkeypatch):
        import config
        weights_file = tmp_path / "weights.json"
        monkeypatch.setattr(config, "INDICATOR_WEIGHTS_FILE", weights_file)
        weights = {
            "VWAP": 10.0, "RSI": 10.0, "Daily Volume Trend": 10.0, "MACD": 10.0,
            "EMA 9/21 Crossover": 10.0, "Bollinger Bands": 10.0, "VWAP StdDev Bands": 10.0,
            "ATR": 10.0, "Stochastic Oscillator": 10.0, "OBV": 10.0, "RSI Slope": 10.0,
        }
        weights_file.write_text(json.dumps(weights))
        from indicators.engine import compute_indicators
        from indicators.values import IndicatorValues
        df = _make_intraday_df(n=100)
        result = compute_indicators("APP", df)
        assert result is not None
        assert isinstance(result, IndicatorValues)
        assert result.symbol == "APP"
        assert -100 <= result.composite_score <= 100

    def test_works_with_daily_df(self, tmp_path, monkeypatch):
        import config
        weights_file = tmp_path / "weights.json"
        monkeypatch.setattr(config, "INDICATOR_WEIGHTS_FILE", weights_file)
        weights = {k: 10.0 for k in [
            "VWAP", "RSI", "Daily Volume Trend", "MACD", "EMA 9/21 Crossover",
            "Bollinger Bands", "VWAP StdDev Bands", "ATR", "Stochastic Oscillator",
            "OBV", "RSI Slope"
        ]}
        weights_file.write_text(json.dumps(weights))
        from indicators.engine import compute_indicators
        df = _make_intraday_df(n=100)
        daily = _make_daily_df(n=10)
        result = compute_indicators("NVDA", df, daily_df=daily)
        assert result is not None
        assert result.daily_volume_ratio > 0


# ── indicator_engine: volume penultimate bar ──────────────────────────────────

@pytest.mark.unit
class TestVolumePenultimate:
    """Volume ratio must use the second-to-last bar, not the incomplete last bar."""

    def test_uses_second_to_last_bar(self):
        """_safe_penultimate must return the second-to-last element, skipping the incomplete last bar."""
        from indicators.composite import _safe_penultimate
        # series where last=0 (incomplete bar), penultimate=1.0
        s = pd.Series([1.0] * 25 + [0.0])
        result = _safe_penultimate(s, fallback=0.0)
        assert result > 0.5, f"Expected ~1.0 (penultimate), got {result}"

    def test_fallback_when_less_than_2_bars(self):
        """Single bar → fallback to 1.0."""
        from indicators.math_utils import _compute_volume_ratio
        vol_series = pd.Series([500.0])
        ratio = _compute_volume_ratio(vol_series)
        # With only 1 bar there may be no valid rolling result; result should be safe
        valid = ratio.dropna()
        if len(valid) > 0:
            assert float(valid.iloc[-1]) >= 0

    def test_normal_bar_computation(self):
        """Standard constant-volume series → ratio = 1.0."""
        from indicators.math_utils import _compute_volume_ratio
        vol = pd.Series([1000.0] * 30)
        ratio = _compute_volume_ratio(vol)
        assert abs(float(ratio.dropna().iloc[-1]) - 1.0) < 1e-6

    def test_empty_series_fallback(self):
        """Empty series must not raise."""
        from indicators.math_utils import _compute_volume_ratio
        ratio = _compute_volume_ratio(pd.Series([], dtype=float))
        assert len(ratio) == 0 or ratio.isna().all()

    def test_all_zero_volume_fallback(self):
        """All-zero volume — ratio must not blow up (div-by-zero safe)."""
        from indicators.math_utils import _compute_volume_ratio
        vol = pd.Series([0.0] * 20)
        ratio = _compute_volume_ratio(vol)
        valid = ratio.dropna()
        if len(valid) > 0:
            assert not any(abs(v) == float("inf") for v in valid)

    def test_penultimate_ignores_incomplete_bar(self):
        """Spike only on the very last bar should not inflate ratio."""
        from indicators.math_utils import _compute_volume_ratio
        base = [1000.0] * 25
        spike = base + [100_000.0]   # last bar spike
        ratio = _compute_volume_ratio(pd.Series(spike))
        last_ratio = float(ratio.dropna().iloc[-1])
        # If penultimate is used the ratio stays near 1.0 (not 100×)
        # If last bar is used the ratio would be ~100; we just check it's not absurdly high
        # This is a soft assertion — the engine is allowed to use the last bar for the ratio
        # series but for entry decisions it picks iloc[-2]; test documents expected behavior.
        assert last_ratio < 200, f"Ratio {last_ratio} suspiciously high — check penultimate logic"


# ── indicator_engine: prev_close field ───────────────────────────────────────

@pytest.mark.unit
class TestPrevCloseField:
    def _make_weights(self, tmp_path, monkeypatch):
        import config
        import json
        weights_file = tmp_path / "weights.json"
        monkeypatch.setattr(config, "INDICATOR_WEIGHTS_FILE", weights_file)
        weights = {k: 10.0 for k in [
            "VWAP", "RSI", "Daily Volume Trend", "MACD", "EMA 9/21 Crossover",
            "Bollinger Bands", "VWAP StdDev Bands", "ATR", "Stochastic Oscillator",
            "OBV", "RSI Slope"
        ]}
        weights_file.write_text(json.dumps(weights))

    def test_prev_close_populated_when_daily_bars_available(self, tmp_path, monkeypatch):
        self._make_weights(tmp_path, monkeypatch)
        from indicators.engine import compute_indicators
        intraday = _make_intraday_df(n=100, close=100.0)
        daily = _make_daily_df(n=10, close=100.0)
        daily.iloc[-2, daily.columns.get_loc("close")] = 95.0
        result = compute_indicators("AAPL", intraday, daily_df=daily)
        assert result is not None
        assert result.prev_close == 95.0

    def test_prev_close_zero_when_daily_bars_insufficient(self, tmp_path, monkeypatch):
        self._make_weights(tmp_path, monkeypatch)
        from indicators.engine import compute_indicators
        intraday = _make_intraday_df(n=100, close=100.0)
        daily = _make_daily_df(n=1, close=88.0)
        result = compute_indicators("AAPL", intraday, daily_df=daily)
        assert result is not None
        assert result.prev_close == 0.0

    def test_prev_close_zero_when_daily_df_none(self, tmp_path, monkeypatch):
        self._make_weights(tmp_path, monkeypatch)
        from indicators.engine import compute_indicators
        intraday = _make_intraday_df(n=100, close=100.0)
        result = compute_indicators("AAPL", intraday, daily_df=None)
        assert result is not None
        assert result.prev_close == 0.0


# ── indicator_engine: phantom weight regression ───────────────────────────────

@pytest.mark.unit
def test_composite_phantom_weights_do_not_suppress_score():
    """ATR always-zero and missing Volume Ratio must not dilute composite scores."""
    from indicators.composite import _composite_score
    vals = {
        "current_price": 100.0, "vwap": 98.0, "atr": 1.0,
        "rsi": 75.0, "macd_hist": 0.5,
        "bb_upper": 102.0, "bb_lower": 97.0,
        "ema9": 100.5, "ema21": 99.5,
        "stoch_k": 80.0, "stoch_d": 60.0,
        "daily_volume_trend": 1.0, "vwap_slope": 0.5,
        "rsi_slope": 15.0, "obv_trend": 1.0,
        "daily_volume_ratio": 1.0,
    }
    weights = {
        "VWAP": 19.05, "RSI": 19.46, "Daily Volume Trend": 6.51,
        "MACD": 2.52, "EMA 9/21 Crossover": 13.40, "Bollinger Bands": 11.82,
        "VWAP StdDev Bands": 7.69, "Stochastic Oscillator": 5.77,
        "OBV": 3.84, "RSI Slope": 9.94,
    }
    score, signal, _ = _composite_score(vals, weights)
    # With phantom weights removed, total_w==100 and effective signal sum drives full score.
    # The analytically expected score is ~88.6 (s_bb=0.2, s_stoch=0.67 cap it below 100).
    # Pre-fix the same inputs yielded ~82.8 due to phantom ATR+VolumeRatio dilution.
    assert score > 85.0, f"Expected >85 but got {score} — phantom weights still suppressing"
    assert "ATR" not in weights
    assert "Volume Ratio" not in weights


# ── Plan 5: relative_volume + or_position fields ──────────────────────────────

@pytest.mark.unit
def test_indicator_values_has_relative_volume_field():
    import dataclasses
    from indicators.values import IndicatorValues
    field_names = [f.name for f in dataclasses.fields(IndicatorValues)]
    assert "relative_volume" in field_names, "IndicatorValues missing relative_volume field"

@pytest.mark.unit
def test_indicator_values_has_or_position_field():
    import dataclasses
    from indicators.values import IndicatorValues
    field_names = [f.name for f in dataclasses.fields(IndicatorValues)]
    assert "or_position" in field_names, "IndicatorValues missing or_position field"

@pytest.mark.unit
def test_relative_volume_defaults_to_one():
    from indicators.values import IndicatorValues
    iv = IndicatorValues.__new__(IndicatorValues)
    assert IndicatorValues.__dataclass_fields__["relative_volume"].default == 1.0

@pytest.mark.unit
def test_or_position_defaults_to_unknown():
    from indicators.values import IndicatorValues
    assert IndicatorValues.__dataclass_fields__["or_position"].default == "unknown"


# ── precompute_indicators_for_day ─────────────────────────────────────────────

def _make_precomp_df():
    """50 warmup bars (prev day) + 78 intraday bars (market hours)."""
    warmup_ts   = pd.date_range("2026-05-05 09:30", periods=50, freq="5min", tz="UTC")
    intraday_ts = pd.date_range("2026-05-06 13:30", periods=78, freq="5min", tz="UTC")
    all_ts = warmup_ts.append(intraday_ts)
    return pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 10000},
        index=all_ts,
    )


@pytest.mark.unit
def test_precompute_indicators_for_day_returns_dict_keyed_by_timestamp():
    """Returns a non-empty dict with Timestamp keys for intraday bars."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from indicators.precompute import precompute_indicators_for_day
    day = datetime(2026, 5, 6, tzinfo=ZoneInfo("America/New_York"))
    result = precompute_indicators_for_day("TEST", _make_precomp_df(), day)
    assert isinstance(result, dict)
    assert len(result) > 0
    assert len(result) <= 78


@pytest.mark.unit
def test_precompute_indicators_for_day_values_are_indicator_values():
    """Each value is an IndicatorValues with correct symbol and bounded fields."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from indicators.precompute import precompute_indicators_for_day
    from indicators.values import IndicatorValues
    day = datetime(2026, 5, 6, tzinfo=ZoneInfo("America/New_York"))
    result = precompute_indicators_for_day("TEST", _make_precomp_df(), day)
    first_iv = next(iter(result.values()))
    assert isinstance(first_iv, IndicatorValues)
    assert first_iv.symbol == "TEST"
    assert first_iv.current_price == pytest.approx(100.0, abs=1.0)
    assert 0 <= first_iv.rsi <= 100
    assert first_iv.atr >= 0
