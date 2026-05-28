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
        """11:59 ET in NORMAL (default) regime should be allowed (hard cutoff at 12:00)."""
        assert is_entry_allowed(self._et(11, 59)) is True

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
        """NVDA (tier1, XLK) should return correct sector ETF key."""
        result = sector_of("NVDA")
        assert result == "XLK"

    def test_sector_of_another_tier1_symbol(self):
        """MSFT (tier1, XLK) should return correct sector ETF key."""
        result = sector_of("MSFT")
        assert result == "XLK"

    def test_sector_of_extended_symbol(self):
        """SNOW (XLK) should return correct sector ETF key."""
        result = sector_of("SNOW")
        assert result == "XLK"

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


# ── Plan 5: Volume guard ──────────────────────────────────────────────────────

class TestVolumeGuard:
    @pytest.mark.unit
    def test_volume_below_min_flags_as_weak(self, monkeypatch):
        monkeypatch.setattr(config, "REL_VOLUME_MIN", 0.60)
        from signals.volume_guard import is_volume_weak
        assert is_volume_weak(relative_volume=0.45) is True

    @pytest.mark.unit
    def test_volume_above_min_does_not_flag(self, monkeypatch):
        monkeypatch.setattr(config, "REL_VOLUME_MIN", 0.60)
        from signals.volume_guard import is_volume_weak
        assert is_volume_weak(relative_volume=0.80) is False

    @pytest.mark.unit
    def test_volume_guard_neutral_passes(self):
        from signals.volume_guard import is_volume_weak
        assert is_volume_weak(relative_volume=1.0) is False


# ── Plan 5: Opening range gate ────────────────────────────────────────────────

class TestOpeningRangeGate:
    @pytest.mark.unit
    def test_price_inside_or_flagged(self):
        from signals.opening_range_gate import get_or_position
        assert get_or_position(current_price=102.0, or_high=105.0, or_low=100.0) == "inside"

    @pytest.mark.unit
    def test_price_above_or_is_outside(self):
        from signals.opening_range_gate import get_or_position
        assert get_or_position(current_price=106.0, or_high=105.0, or_low=100.0) == "outside"

    @pytest.mark.unit
    def test_price_below_or_is_outside(self):
        from signals.opening_range_gate import get_or_position
        assert get_or_position(current_price=98.0, or_high=105.0, or_low=100.0) == "outside"

    @pytest.mark.unit
    def test_unknown_when_or_not_formed(self):
        from signals.opening_range_gate import get_or_position
        assert get_or_position(current_price=100.0, or_high=0.0, or_low=0.0) == "unknown"


# ── Task 1: VIX regime classifier ─────────────────────────────────────────────

class TestVixClassifier:
    """Tests for backtest/data.py _classify_vix_day"""

    def _make_vix(self, values: list[float]) -> list[tuple[int, float]]:
        """Build (timestamp, close) pairs from a list of VIX values."""
        return [(i * 86400, v) for i, v in enumerate(values)]

    def _make_spy(self, values: list[float]) -> list[tuple[int, float]]:
        return [(i * 86400, v) for i, v in enumerate(values)]

    @pytest.mark.unit
    def test_crisis_when_vix_rising_and_spy_falling(self):
        """VIX above 90d avg, rising, SPY falling → CRISIS."""
        from backtest.data import _classify_vix_day
        vix = self._make_vix([20] * 90 + [38, 40])
        spy = self._make_spy([500, 490])
        assert _classify_vix_day(vix, spy) == "CRISIS"

    @pytest.mark.unit
    def test_crash_recovery_when_vix_elevated_but_falling_from_peak(self):
        """VIX above CRISIS threshold but dropping >=10% from 5d peak → CRASH_RECOVERY.
        Requires >= 2 consecutive green SPY days for live-bot parity."""
        from backtest.data import _classify_vix_day
        # VIX was 50 (peak), now 43 — that's a 14% drop from peak; still above avg 20
        vix = self._make_vix([20] * 90 + [50, 50, 50, 50, 50, 43])
        spy = self._make_spy([500, 505, 510])  # 2 consecutive green days
        result = _classify_vix_day(vix, spy)
        assert result == "CRASH_RECOVERY"

    @pytest.mark.unit
    def test_high_vol_when_vix_elevated_small_drop(self):
        """VIX above avg, not rising, but drop from peak < threshold → HIGH_VOL."""
        from backtest.data import _classify_vix_day
        # VIX was 30, now 29 — only 3.3% drop from peak (below 10% threshold)
        vix = self._make_vix([20] * 90 + [30, 30, 30, 30, 30, 29])
        spy = self._make_spy([500, 501])
        result = _classify_vix_day(vix, spy)
        assert result == "HIGH_VOL"

    @pytest.mark.unit
    def test_normal_when_vix_below_avg(self):
        """VIX below 90d avg → NORMAL."""
        from backtest.data import _classify_vix_day
        vix = self._make_vix([30] * 90 + [18, 17])
        spy = self._make_spy([500, 501])
        assert _classify_vix_day(vix, spy) == "NORMAL"


# ── Task 2: SL/TP ratio ───────────────────────────────────────────────────────

class TestSLTPRatio:
    @pytest.mark.unit
    def test_stop_loss_pct_is_3(self):
        """STOP_LOSS_PCT must be 3.0 to match ATR_TP_FLOOR."""
        assert config.STOP_LOSS_PCT == 3.0

    @pytest.mark.unit
    def test_atr_tp_floor_is_3(self):
        """ATR TP floor is hardcoded to 3.0 to ensure positive R:R vs STOP_LOSS_PCT."""
        assert 3.0 >= config.STOP_LOSS_PCT

    @pytest.mark.unit
    def test_compute_atr_tp_pct_respects_new_floor(self):
        """compute_atr_tp_pct never returns less than 3.0% (hardcoded floor)."""
        from signals.atr_tp import compute_atr_tp_pct
        result = compute_atr_tp_pct(atr=0.01, price=100.0, multiplier=2.5)
        assert result == 3.0


class TestDayTradeGates:
    """Day-trade time/momentum gates driven by DayTradeParams regime × tier table."""

    def _iv(self, composite: float = 80.0, vwap_slope: float = 0.1):
        return types.SimpleNamespace(
            current_price=100.0, vwap=99.0, vwap_std2_upper=110.0,
            stop_hunt_zone=False, parabolic=False, momentum_exhaustion=False,
            bot_herding_score=0, squeeze_setup=False, rsi=50.0, stoch_d=50.0,
            prev_close=100.0, relative_volume=1.0, or_position="outside",
            composite_score=composite, vwap_slope=vwap_slope,
        )

    def _patch(self, monkeypatch):
        import calendars.earnings as ec
        import macro.economic_calendar as ecc
        monkeypatch.setattr(ec, "is_earnings_imminent", lambda *a, **kw: False)
        monkeypatch.setattr(ecc, "is_event_risk_window", lambda *a, **kw: (False, ""))

    def _now(self, hour, minute=0):
        from zoneinfo import ZoneInfo
        return datetime(2024, 1, 15, hour, minute, tzinfo=ZoneInfo("America/New_York"))

    @pytest.mark.unit
    def test_per_tier_hard_cutoff_blocks_medium_at_noon(self, monkeypatch):
        """NORMAL/MEDIUM hard_close_hour=12: entry at 12:00 ET is blocked."""
        from signals.entry_gates import check_all_gates
        self._patch(monkeypatch)
        result = check_all_gates(
            symbol="AAPL", iv=self._iv(composite=80.0),
            open_positions={}, daily_closed=set(), bars_15min=None,
            now=self._now(12, 0), regime="NORMAL", mode="DAY_TRADE",
        )
        assert result.passed is False
        assert "cutoff" in result.reason.lower() or "hard" in result.reason.lower()

    @pytest.mark.unit
    def test_per_tier_hard_cutoff_allows_conviction_at_noon(self, monkeypatch):
        """NORMAL/CONVICTION hard_close_hour=13: entry at 12:30 ET is NOT blocked by time gate."""
        from signals.entry_gates import check_all_gates
        from utils.ticker_quality import TickerQuality
        self._patch(monkeypatch)
        conviction_quality = TickerQuality(tier="CONVICTION", score=90, unlocks=frozenset(), quality_gaps=(), component_scores={})
        result = check_all_gates(
            symbol="AAPL", iv=self._iv(composite=80.0),
            open_positions={}, daily_closed=set(), bars_15min=None,
            now=self._now(12, 30), regime="NORMAL", mode="DAY_TRADE",
            quality=conviction_quality,
        )
        # Should not fail due to day-trade hard cutoff (CONVICTION=13)
        assert "cutoff" not in result.reason.lower()

    @pytest.mark.unit
    def test_late_entry_low_composite_blocked(self, monkeypatch):
        """BULL_TREND/MEDIUM at 12:30 with composite 35 < late_composite 68 → blocked."""
        from signals.entry_gates import check_all_gates
        self._patch(monkeypatch)
        result = check_all_gates(
            symbol="AAPL", iv=self._iv(composite=35.0),
            open_positions={}, daily_closed=set(), bars_15min=None,
            now=self._now(12, 30), regime="BULL_TREND", mode="DAY_TRADE",
        )
        assert result.passed is False
        assert "late" in result.reason.lower() or "composite" in result.reason.lower()

    @pytest.mark.unit
    def test_late_entry_high_composite_passes_time_gate(self, monkeypatch):
        """BULL_TREND/MEDIUM at 13:30 with composite 80 >= late_composite 68 → time gate passes."""
        from signals.entry_gates import check_all_gates
        self._patch(monkeypatch)
        result = check_all_gates(
            symbol="AAPL", iv=self._iv(composite=80.0),
            open_positions={}, daily_closed=set(), bars_15min=None,
            now=self._now(13, 30), regime="BULL_TREND", mode="DAY_TRADE",
        )
        assert "late" not in result.reason.lower()

    @pytest.mark.unit
    def test_vwap_slope_too_flat_blocks_day_trade(self, monkeypatch):
        """NORMAL/MEDIUM vwap_slope_min=0.05: slope 0.02 < 0.05 → blocked."""
        from signals.entry_gates import check_all_gates
        self._patch(monkeypatch)
        result = check_all_gates(
            symbol="AAPL", iv=self._iv(composite=80.0, vwap_slope=0.02),
            open_positions={}, daily_closed=set(), bars_15min=None,
            now=self._now(10, 30), regime="NORMAL", mode="DAY_TRADE",
        )
        assert result.passed is False
        assert "vwap slope" in result.reason.lower()

    @pytest.mark.unit
    def test_vwap_slope_above_min_passes(self, monkeypatch):
        """NORMAL/MEDIUM: slope 0.10 >= 0.05 → VWAP gate passes."""
        from signals.entry_gates import check_all_gates
        self._patch(monkeypatch)
        result = check_all_gates(
            symbol="AAPL", iv=self._iv(composite=80.0, vwap_slope=0.10),
            open_positions={}, daily_closed=set(), bars_15min=None,
            now=self._now(10, 30), regime="NORMAL", mode="DAY_TRADE",
        )
        assert "vwap slope" not in result.reason.lower()

    @pytest.mark.unit
    def test_swing_mode_bypasses_day_trade_gates(self, monkeypatch):
        """SWING mode is not subject to day-trade hard cutoff or VWAP slope gate."""
        from signals.entry_gates import check_all_gates
        self._patch(monkeypatch)
        result = check_all_gates(
            symbol="AAPL", iv=self._iv(composite=80.0, vwap_slope=0.01),
            open_positions={}, daily_closed=set(), bars_15min=None,
            now=self._now(12, 30), regime="NORMAL", mode="SWING",
        )
        assert "cutoff" not in result.reason.lower()
        assert "vwap slope" not in result.reason.lower()

    @pytest.mark.unit
    def test_early_entry_not_blocked_by_late_gate(self, monkeypatch):
        """At 10:30 ET in NORMAL/MEDIUM (late_entry_hour=11), late-gate does not apply."""
        from signals.entry_gates import check_all_gates
        self._patch(monkeypatch)
        result = check_all_gates(
            symbol="AAPL", iv=self._iv(composite=55.0, vwap_slope=0.10),
            open_positions={}, daily_closed=set(), bars_15min=None,
            now=self._now(10, 30), regime="NORMAL", mode="DAY_TRADE",
        )
        assert "late" not in result.reason.lower()


class TestSLBlacklist:
    """Tests for the persistent SL-hit blacklist."""

    @pytest.mark.unit
    def test_record_sl_hit_writes_today(self, monkeypatch, tmp_path):
        """record_sl_hit writes today's date for the symbol."""
        monkeypatch.setattr(config, "SL_BLACKLIST_FILE", str(tmp_path / "sl_blacklist.json"))
        monkeypatch.setattr(config, "SL_BLACKLIST_ENABLED", True)
        from utils.sl_blacklist import record_sl_hit
        from utils.io_utils import load_json
        record_sl_hit("AAPL")
        data = load_json(str(tmp_path / "sl_blacklist.json"), {})
        assert "AAPL" in data

    @pytest.mark.unit
    def test_is_sl_blacklisted_true_same_day(self, monkeypatch, tmp_path):
        """Symbol is blacklisted when hit today (days < SL_BLACKLIST_DAYS=1)."""
        monkeypatch.setattr(config, "SL_BLACKLIST_FILE", str(tmp_path / "sl_blacklist.json"))
        monkeypatch.setattr(config, "SL_BLACKLIST_ENABLED", True)
        monkeypatch.setattr(config, "SL_BLACKLIST_DAYS", 1)
        from utils.sl_blacklist import record_sl_hit, is_sl_blacklisted
        record_sl_hit("AAPL")
        assert is_sl_blacklisted("AAPL") is True

    @pytest.mark.unit
    def test_is_sl_blacklisted_false_when_disabled(self, monkeypatch, tmp_path):
        """When SL_BLACKLIST_ENABLED=False, is_sl_blacklisted always returns False."""
        monkeypatch.setattr(config, "SL_BLACKLIST_FILE", str(tmp_path / "sl_blacklist.json"))
        monkeypatch.setattr(config, "SL_BLACKLIST_ENABLED", False)
        from utils.sl_blacklist import record_sl_hit, is_sl_blacklisted
        record_sl_hit("AAPL")
        assert is_sl_blacklisted("AAPL") is False

    @pytest.mark.unit
    def test_check_all_gates_blocks_sl_blacklisted(self, monkeypatch):
        """check_all_gates returns False when sl_blacklisted=True."""
        import types
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from signals.entry_gates import check_all_gates
        import calendars.earnings as ec
        import macro.economic_calendar as ecc
        monkeypatch.setattr(ec, "is_earnings_imminent", lambda *a, **kw: False)
        monkeypatch.setattr(ecc, "is_event_risk_window", lambda *a, **kw: (False, ""))
        iv = types.SimpleNamespace(
            current_price=100.0, vwap=99.0, vwap_std2_upper=110.0,
            stop_hunt_zone=False, parabolic=False, momentum_exhaustion=False,
            bot_herding_score=0, squeeze_setup=False, rsi=50.0, stoch_d=50.0,
            prev_close=100.0, relative_volume=1.0, or_position="outside",
            composite_score=80.0,  # above ENTRY_LATE_COMPOSITE_MIN so SL gate fires
        )
        now = datetime(2024, 1, 15, 11, 0, 0, tzinfo=ZoneInfo("America/New_York"))
        result = check_all_gates(
            symbol="AAPL", iv=iv, open_positions={}, daily_closed=set(),
            bars_15min=None, now=now, sl_blacklisted=True,
        )
        assert result.passed is False
        assert "blacklist" in result.reason.lower() or "sl" in result.reason.lower()


@pytest.mark.unit
class TestEntryHardCutoff:
    def _now(self, hour, minute=0):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime(2026, 5, 21, hour, minute, tzinfo=ZoneInfo("America/New_York"))

    def test_entry_allowed_before_hard_cutoff_normal(self):
        from signals.time_gate import is_entry_allowed
        # NORMAL EntryParams.hard_close_hour=13 (CONVICTION-tier cycle gate); 12:59 allowed
        assert is_entry_allowed(self._now(12, 59), regime="NORMAL") is True

    def test_entry_blocked_at_hard_cutoff_normal(self):
        from signals.time_gate import is_entry_allowed
        # Cycle-level gate blocks at 13:00 for NORMAL
        assert is_entry_allowed(self._now(13, 0), regime="NORMAL") is False

    def test_entry_blocked_earlier_in_choppy(self, monkeypatch):
        from signals.time_gate import is_entry_allowed
        # 12:00 PM in CHOPPY (cutoff=12) should be blocked
        assert is_entry_allowed(self._now(12, 0), regime="CHOPPY") is False

    def test_entry_allowed_later_in_bull_trend(self, monkeypatch):
        from signals.time_gate import is_entry_allowed
        # 1:30 PM in BULL_TREND (cutoff=14) should be allowed
        assert is_entry_allowed(self._now(13, 30), regime="BULL_TREND") is True

    def test_entry_blocked_after_bull_trend_cutoff(self, monkeypatch):
        from signals.time_gate import is_entry_allowed
        # 2:00 PM in BULL_TREND (cutoff=14) should be blocked
        assert is_entry_allowed(self._now(14, 0), regime="BULL_TREND") is False

    def test_backward_compat_no_regime_arg(self):
        from signals.time_gate import is_entry_allowed
        # No regime arg → defaults to NORMAL (CONVICTION-level cycle cutoff=13)
        # 12:59 should be allowed, 13:00 blocked
        assert is_entry_allowed(self._now(12, 59)) is True
        assert is_entry_allowed(self._now(13, 0)) is False



@pytest.mark.unit
def test_gate2_removed_from_check_all_gates():
    """Gate #2 (per-cycle cap) has been moved to pipeline.py — check_all_gates must not have this param."""
    import inspect
    from signals.entry_gates import check_all_gates
    sig = inspect.signature(check_all_gates)
    assert "entries_this_cycle" not in sig.parameters, (
        "entries_this_cycle must not be a parameter of check_all_gates()"
    )
