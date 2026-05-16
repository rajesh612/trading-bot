"""
Signal package unit tests (signals/).
Run with: pytest tests/test_signals.py -v -m unit
No API calls — all tests use synthetic fixtures.
"""
import types
from datetime import datetime

import pandas as pd
import pytest
import pytz

import config

_ET_ZONE = pytz.timezone("America/New_York")


def make_full_iv(**overrides):
    """Module-level factory: builds a fully-populated IndicatorValues for tests.

    All required fields are given safe defaults that pass every hard gate.
    Pass keyword overrides to exercise specific conditions.
    """
    from indicators.values import IndicatorValues
    defaults = dict(
        symbol="TEST", timestamp="2024-01-15T11:00:00",
        current_price=100.0, vwap=100.0,
        vwap_std1_upper=102.5, vwap_std1_lower=97.5,
        vwap_std2_upper=105.0, vwap_std2_lower=95.0,
        rsi=55.0, rsi_delta=1.0, stoch_k=50.0, stoch_d=50.0,
        composite_score=50.0, atr=1.0, prev_close=99.0,
        parabolic=False, stop_hunt_zone=False,
        momentum_exhaustion=False, bot_herding_score=0,
        squeeze_setup=False, obv_divergence="none",
        volume_exhaustion=False, volume_ratio=1.0,
        macd=0.1, macd_signal=0.05, macd_hist=0.05,
        bb_upper=105.0, bb_mid=100.0, bb_lower=95.0,
        ema9=100.0, ema21=99.0,
        obv=1000.0, vwap_slope=0.1, rsi_slope=0.1,
        daily_volume_ratio=1.0, daily_volume_trend=0.0, daily_volume_mean=1000000.0,
        signal="BUY", confidence=0.7,
    )
    defaults.update(overrides)
    return IndicatorValues(**defaults)


# ── signals.time_gate ─────────────────────────────────────────────────────────

from signals.time_gate import is_entry_allowed


@pytest.mark.unit
class TestTimeGate:
    """Tests for time-based entry gates."""

    def _et(self, h, m) -> datetime:
        """Create a tz-aware ET datetime on a weekday."""
        return _ET_ZONE.localize(datetime(2024, 1, 15, h, m, 0))  # 2024-01-15 is a Monday

    def test_allowed_during_trading_window(self):
        """Middle of trading day should allow entry."""
        assert is_entry_allowed(self._et(11, 0)) is True

    def test_blocked_at_market_open(self):
        """9:30 AM ET (market open) should be blocked."""
        assert is_entry_allowed(self._et(9, 30)) is False

    def test_blocked_during_first_gate(self):
        """9:59 AM ET should be blocked (within first 30 minutes)."""
        assert is_entry_allowed(self._et(9, 59)) is False

    def test_allowed_exactly_at_gate_open_end(self):
        """10:00 AM ET should be allowed (ENTRY_GATE_OPEN_MINUTES = 30)."""
        assert is_entry_allowed(self._et(10, 0)) is True

    def test_blocked_at_close_window_start(self):
        """15:25 ET should be blocked (16:00 - 35 minutes, with ENTRY_GATE_CLOSE_MINUTES = 35)."""
        assert is_entry_allowed(self._et(15, 25)) is False

    def test_blocked_near_close(self):
        """15:50 ET should be blocked (close at 16:00)."""
        assert is_entry_allowed(self._et(15, 50)) is False

    def test_allowed_before_close_window(self):
        """15:24 ET should be allowed (just before close gate)."""
        assert is_entry_allowed(self._et(15, 24)) is True

    def test_naive_datetime_handled(self):
        """Naive datetime (no timezone) should be localized to ET and not raise."""
        naive = datetime(2024, 1, 15, 11, 0, 0)
        result = is_entry_allowed(naive)
        assert isinstance(result, bool)
        assert result is True


# ── signals.relative_strength ─────────────────────────────────────────────────

from signals.relative_strength import rs_rank, passes_rs_filter


@pytest.mark.unit
class TestRelativeStrength:
    """Tests for relative strength (RS) filter."""

    def test_rs_rank_outperforming(self):
        """Symbol up 4% while SPY up 2% → RS ratio = 2.0 (outperforming)."""
        ratio = rs_rank("NVDA", spy_ret5d=2.0, symbol_ret5d=4.0)
        assert ratio == pytest.approx(2.0)

    def test_rs_rank_underperforming(self):
        """Symbol up 1% while SPY up 2% → RS ratio = 0.5 (underperforming)."""
        ratio = rs_rank("XYZ", spy_ret5d=2.0, symbol_ret5d=1.0)
        assert ratio == pytest.approx(0.5)

    def test_rs_rank_equal_performance(self):
        """Symbol and SPY both up 2% → RS ratio = 1.0 (neutral)."""
        ratio = rs_rank("AAPL", spy_ret5d=2.0, symbol_ret5d=2.0)
        assert ratio == pytest.approx(1.0)

    def test_rs_rank_zero_spy_return(self):
        """SPY flat (0% return) → return 1.0 (neutral) regardless of symbol return."""
        ratio = rs_rank("AAPL", spy_ret5d=0.0, symbol_ret5d=3.0)
        assert ratio == pytest.approx(1.0)

    def test_passes_rs_filter_default_threshold(self):
        """RS ratio of 0.5 should pass with config.RS_MIN_RATIO default (0.0)."""
        assert passes_rs_filter(0.5) is True

    def test_passes_rs_filter_fails_below_threshold(self):
        """RS ratio of -0.5 with threshold 0.0 → fails filter."""
        assert passes_rs_filter(-0.5, threshold=0.0) is False


# ── signals.correlation_guard ─────────────────────────────────────────────────

from signals.correlation_guard import sector_of, passes_correlation_guard


@pytest.mark.unit
class TestCorrelationGuard:
    """Tests for correlation guard (sector-based position limit)."""

    def test_sector_of_tier1_symbol(self):
        """NVDA (tier1, AI_SEMI) should return correct sector string."""
        result = sector_of("NVDA")
        assert result == "AI_SEMI"

    def test_sector_of_another_tier1_symbol(self):
        """MSFT (tier1, HYPERSCALER) should return correct sector string."""
        result = sector_of("MSFT")
        assert result == "HYPERSCALER"

    def test_sector_of_extended_symbol(self):
        """SNOW (extended, CLOUD) should return correct sector string."""
        result = sector_of("SNOW")
        assert result == "CLOUD"

    def test_sector_of_unknown_returns_other(self):
        """Unknown symbol ZZZ_UNKNOWN should return 'OTHER'."""
        assert sector_of("ZZZ_UNKNOWN") == "OTHER"

    def test_passes_with_zero_positions(self):
        """Empty open_positions dict should always pass."""
        assert passes_correlation_guard("NVDA", {}) is True

    def test_passes_with_one_same_sector(self):
        """With MAX_SAME_SECTOR_POSITIONS=2, one existing same-sector position should pass."""
        assert passes_correlation_guard("AMD", {"NVDA": {}}) is True

    def test_blocked_at_max_same_sector(self):
        """With MAX_SAME_SECTOR_POSITIONS=2, two existing same-sector positions should block."""
        assert passes_correlation_guard("INTC", {"NVDA": {}, "AMD": {}}) is False

    def test_passes_different_sectors(self):
        """Positions in different sectors should not block entry."""
        assert passes_correlation_guard("NVDA", {"PYPL": {}, "AFRM": {}}) is True

    def test_unknown_sector_never_blocked(self):
        """Unknown sector ('OTHER') should always pass regardless of open positions."""
        assert passes_correlation_guard("ZZZ_UNKNOWN", {"NVDA": {}, "AMD": {}, "INTC": {}}) is True


# ── signals.sentiment_sizer ───────────────────────────────────────────────────

from signals.sentiment_sizer import sentiment_size_multiplier


@pytest.mark.unit
class TestSentimentSizer:
    """Tests for sentiment-based position sizing multiplier."""

    def test_score_above_20_returns_1_25(self):
        assert sentiment_size_multiplier(25) == pytest.approx(1.25)

    def test_score_exactly_20_returns_1_25(self):
        assert sentiment_size_multiplier(20) == pytest.approx(1.25)

    def test_score_between_10_and_19_returns_1_10(self):
        assert sentiment_size_multiplier(15) == pytest.approx(1.10)

    def test_score_exactly_10_returns_1_10(self):
        assert sentiment_size_multiplier(10) == pytest.approx(1.10)

    def test_score_between_neg10_and_9_returns_1_00(self):
        assert sentiment_size_multiplier(0) == pytest.approx(1.00)

    def test_score_exactly_neg10_returns_1_00(self):
        assert sentiment_size_multiplier(-10) == pytest.approx(1.00)

    def test_score_between_neg20_and_neg11_returns_0_75(self):
        assert sentiment_size_multiplier(-15) == pytest.approx(0.75)

    def test_score_exactly_neg20_returns_0_75(self):
        assert sentiment_size_multiplier(-20) == pytest.approx(0.75)

    def test_score_below_neg20_returns_floor(self):
        assert sentiment_size_multiplier(-25) == pytest.approx(0.50)

    def test_score_extreme_negative_returns_floor(self):
        assert sentiment_size_multiplier(-100) == pytest.approx(0.50)


# ── signals.mtf_confirm ───────────────────────────────────────────────────────

@pytest.mark.unit
class TestMtfConfirm:
    def _make_bars(self, closes: list[float]) -> pd.DataFrame:
        """Return a minimal OHLCV DataFrame with close series."""
        return pd.DataFrame({"close": closes})

    def test_ema9_above_ema21_passes(self):
        """Uptrend: rising prices → EMA9 > EMA21 → pass."""
        from signals.mtf_confirm import passes_mtf_filter
        closes = [100.0 + i * 0.5 for i in range(40)]
        assert passes_mtf_filter(self._make_bars(closes)) is True

    def test_ema9_below_ema21_blocked(self):
        """Downtrend: falling prices → EMA9 < EMA21 → blocked."""
        from signals.mtf_confirm import passes_mtf_filter
        closes = [140.0 - i * 0.5 for i in range(40)]
        assert passes_mtf_filter(self._make_bars(closes)) is False

    def test_insufficient_data_passes(self):
        """Less than 21 bars → fail-safe pass."""
        from signals.mtf_confirm import passes_mtf_filter
        closes = [100.0] * 10
        assert passes_mtf_filter(self._make_bars(closes)) is True

    def test_none_input_passes(self):
        """None DataFrame → fail-safe pass."""
        from signals.mtf_confirm import passes_mtf_filter
        assert passes_mtf_filter(None) is True

    def test_empty_dataframe_passes(self):
        """Empty DataFrame → fail-safe pass."""
        from signals.mtf_confirm import passes_mtf_filter
        assert passes_mtf_filter(pd.DataFrame()) is True

    def test_flat_prices_ema_equal_blocked(self):
        """Flat prices → EMA9 ≈ EMA21 → not strictly > → blocked."""
        from signals.mtf_confirm import passes_mtf_filter
        closes = [100.0] * 40
        assert passes_mtf_filter(self._make_bars(closes)) is False


# ── signals.vwap_guard ────────────────────────────────────────────────────────

@pytest.mark.unit
class TestVwapGuard:
    """Tests for VWAP overextension gate."""

    @staticmethod
    def _iv(price: float, vwap_std2_upper: float | None):
        """Build a minimal IndicatorValues-like namespace."""
        return types.SimpleNamespace(
            current_price=price,
            vwap_std2_upper=vwap_std2_upper if vwap_std2_upper is not None else float("inf"),
        )

    def test_price_below_vwap_upper_passes(self):
        """Price well below upper band → no overextension."""
        from signals.vwap_guard import passes_vwap_guard
        iv = self._iv(58.0, 60.0)
        assert passes_vwap_guard(iv) is True

    def test_price_at_vwap_upper_passes(self):
        """Price exactly at upper band → boundary is inclusive."""
        from signals.vwap_guard import passes_vwap_guard
        iv = self._iv(60.0, 60.0)
        assert passes_vwap_guard(iv) is True

    def test_price_above_vwap_upper_blocks(self):
        """Price above upper band → overextension block."""
        from signals.vwap_guard import passes_vwap_guard
        iv = self._iv(61.0, 60.0)
        assert passes_vwap_guard(iv) is False

    def test_gate_disabled_always_passes(self, monkeypatch):
        """VWAP_OVEREXTENSION_GATE=False → always pass even above upper band."""
        import config
        monkeypatch.setattr(config, "VWAP_OVEREXTENSION_GATE", False)
        from signals.vwap_guard import passes_vwap_guard
        iv = self._iv(100.0, 50.0)  # price >> upper band
        assert passes_vwap_guard(iv) is True

    def test_vwap_guard_with_inf_upper(self):
        """None upper mapped to inf → always passes (safe default)."""
        from signals.vwap_guard import passes_vwap_guard
        iv = self._iv(999.0, None)  # _iv maps None → inf
        assert passes_vwap_guard(iv) is True

    def test_vwap_guard_exact_boundary_enabled(self, monkeypatch):
        """Gate enabled; price exactly at upper band → still passes."""
        import config
        monkeypatch.setattr(config, "VWAP_OVEREXTENSION_GATE", True)
        from signals.vwap_guard import passes_vwap_guard
        iv = self._iv(100.0, 100.0)
        assert passes_vwap_guard(iv) is True


