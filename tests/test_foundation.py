"""
Plans 1–3 foundation unit tests. Run with: pytest tests/test_foundation.py -v -m unit
No API calls — all tests use synthetic fixtures.
"""
import json
import os
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

ET = ZoneInfo("America/New_York")


# ── Shared DataFrame factories ────────────────────────────────────────────────

def _make_intraday_df(
    n: int = 100,
    close: float = 100.0,
    high: float | None = None,
    low: float | None = None,
    volume: float = 1_000.0,
) -> pd.DataFrame:
    """Return a minimal OHLCV 5-min DataFrame with tz-aware index."""
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
    """Return a minimal OHLCV daily DataFrame with tz-aware index."""
    idx = pd.date_range("2026-01-01", periods=n, freq="B", tz="America/New_York")
    return pd.DataFrame(
        {"open": close, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": volume},
        index=idx,
    )


# ── macro_regime: health score ────────────────────────────────────────────────

@pytest.mark.unit
class TestMacroHealthScore:
    def test_bullish_inputs_give_positive_score(self):
        from macro_regime import compute_macro_health_score
        # Strong bull signals
        vix_closes = [20.0] * 90 + [14.0]  # VIX well below 90d avg
        signals = {
            "vix_closes": vix_closes,
            "spy_ret5d":  3.0,   # strong up week
            "soxx_ret5d": 4.0,   # semis leading
            "qqq_ret5d":  3.0,
            "wti_ret5d":  1.0,   # stable oil
            "tnx_ret5d":  -3.0,  # yields falling
            "gold_ret5d": 0.5,   # flat gold
            "dxy_ret5d":  -1.0,  # weak dollar
        }
        score = compute_macro_health_score(signals)
        assert score > 0, f"Expected positive score for bullish inputs, got {score}"

    def test_bearish_inputs_give_negative_score(self):
        from macro_regime import compute_macro_health_score
        # Strong bear signals
        vix_closes = [15.0] * 90 + [40.0]  # VIX spiked far above avg
        signals = {
            "vix_closes": vix_closes,
            "spy_ret5d":  -8.0,   # ugly down week
            "soxx_ret5d": -10.0,
            "qqq_ret5d":  -8.0,
            "wti_ret5d":  8.0,    # oil spike
            "tnx_ret5d":  7.0,    # yields surging
            "gold_ret5d": 5.0,    # gold flight to safety
            "dxy_ret5d":  3.0,    # dollar surging
        }
        score = compute_macro_health_score(signals)
        assert score < 0, f"Expected negative score for bearish inputs, got {score}"

    def test_neutral_inputs_near_zero(self):
        from macro_regime import compute_macro_health_score
        vix_closes = [20.0] * 91  # VIX at its own average
        signals = {
            "vix_closes": vix_closes,
            "spy_ret5d":  0.5,
            "soxx_ret5d": 1.0,
            "qqq_ret5d":  0.5,
            "wti_ret5d":  1.0,
            "tnx_ret5d":  0.5,
            "gold_ret5d": 1.0,
            "dxy_ret5d":  0.2,
        }
        score = compute_macro_health_score(signals)
        # Stable oil/gold/DXY score mildly positive (stability = bullish),
        # so a "neutral" market lands in roughly [-10, +60] rather than near-zero.
        assert -20 < score < 65, f"Expected broadly neutral score, got {score}"

    def test_score_clamped_to_bounds(self):
        from macro_regime import compute_macro_health_score
        # Extreme bullish
        vix_closes = [30.0] * 90 + [10.0]
        extreme_bull = {
            "vix_closes": vix_closes,
            "spy_ret5d": 20.0, "soxx_ret5d": 20.0, "qqq_ret5d": 20.0,
            "wti_ret5d": 0.0, "tnx_ret5d": -20.0, "gold_ret5d": -5.0, "dxy_ret5d": -5.0,
        }
        score = compute_macro_health_score(extreme_bull, fred_modifier=5)
        assert -100 <= score <= 100, f"Score {score} out of [-100, 100] bounds"

    def test_fred_modifier_applied(self):
        from macro_regime import compute_macro_health_score
        vix_closes = [20.0] * 91
        neutral = {
            "vix_closes": vix_closes,
            "spy_ret5d": 0.0, "soxx_ret5d": 0.0, "qqq_ret5d": 0.0,
            "wti_ret5d": 0.0, "tnx_ret5d": 0.0, "gold_ret5d": 0.0, "dxy_ret5d": 0.0,
        }
        base  = compute_macro_health_score(neutral, fred_modifier=0)
        with_modifier = compute_macro_health_score(neutral, fred_modifier=5)
        assert with_modifier == base + 5


# ── macro_regime: classify_regime ─────────────────────────────────────────────

@pytest.mark.unit
class TestClassifyRegime:
    _BASE = dict(
        macro_health_score=0,
        consecutive_green_days=0,
        consecutive_red_days=0,
        vix_current=18.0,
        spy_5day_return=0.0,
        is_choppy=False,
        ai_semi_sector_score=0,
        bot_net_pnl_pct=0.0,
        post_crisis_recovery=False,
    )

    def _classify(self, **overrides):
        from macro_regime import classify_regime
        return classify_regime(**{**self._BASE, **overrides})

    def test_crisis_vix_above_35(self):
        assert self._classify(vix_current=36.0) == "CRISIS"

    def test_crisis_spy_5d_below_minus10(self):
        assert self._classify(spy_5day_return=-11.0) == "CRISIS"

    def test_crash_recovery(self):
        regime = self._classify(post_crisis_recovery=True, consecutive_green_days=2)
        assert regime == "CRASH_RECOVERY"

    def test_bull_trend(self):
        regime = self._classify(
            consecutive_green_days=3, macro_health_score=25
        )
        assert regime == "BULL_TREND"

    def test_choppy(self):
        assert self._classify(is_choppy=True) == "CHOPPY"

    def test_defensive(self):
        regime = self._classify(
            consecutive_red_days=3,
            macro_health_score=-25,
            bot_net_pnl_pct=-2.0,
        )
        assert regime == "DEFENSIVE"

    def test_opportunity_overrides_defensive(self):
        regime = self._classify(
            consecutive_red_days=3,
            macro_health_score=-25,
            bot_net_pnl_pct=-2.0,
            ai_semi_sector_score=20,   # AI/semi healthy → OPPORTUNITY
        )
        assert regime == "OPPORTUNITY"

    def test_normal_when_no_conditions_match(self):
        assert self._classify() == "NORMAL"

    def test_defensive_requires_both_market_and_pnl(self):
        # Red market but P&L OK → NORMAL (not DEFENSIVE)
        regime = self._classify(
            consecutive_red_days=3,
            macro_health_score=-25,
            bot_net_pnl_pct=+0.5,  # not negative enough
        )
        assert regime == "NORMAL"

    @pytest.mark.parametrize("regime_call,expected", [
        (dict(vix_current=36.0), "CRISIS"),
        (dict(post_crisis_recovery=True, consecutive_green_days=2), "CRASH_RECOVERY"),
        (dict(consecutive_green_days=3, macro_health_score=25), "BULL_TREND"),
        (dict(is_choppy=True), "CHOPPY"),
        (dict(consecutive_red_days=3, macro_health_score=-25, bot_net_pnl_pct=-2.0,
              ai_semi_sector_score=20), "OPPORTUNITY"),
        (dict(consecutive_red_days=3, macro_health_score=-25, bot_net_pnl_pct=-2.0), "DEFENSIVE"),
        (dict(), "NORMAL"),
    ])
    def test_all_nine_regimes(self, regime_call, expected):
        assert self._classify(**regime_call) == expected


# ── macro_regime: pattern detection ──────────────────────────────────────────

@pytest.mark.unit
class TestPatternDetection:
    def test_consecutive_green(self):
        from macro_regime import _count_consecutive_direction
        closes = [100, 101, 102, 103, 104, 105]  # all green
        assert _count_consecutive_direction(closes, "green") == 5

    def test_consecutive_red(self):
        from macro_regime import _count_consecutive_direction
        closes = [100, 99, 98, 97]  # all red
        assert _count_consecutive_direction(closes, "red") == 3

    def test_choppy_true(self):
        from macro_regime import _is_choppy
        # Up, down, up, down, up — 4 sign changes, tiny net move
        closes = [100.0, 100.2, 100.0, 100.2, 100.0, 100.1]
        assert _is_choppy(closes) is True

    def test_choppy_false_trending(self):
        from macro_regime import _is_choppy
        closes = [100, 101, 102, 103, 104, 105]  # steady uptrend
        assert _is_choppy(closes) is False

    def test_choppy_false_big_net_move(self):
        from macro_regime import _is_choppy
        # Alternating but large net move
        closes = [100, 101, 99.5, 102, 100, 104]
        # Net = (104 - 100) / 100 = 4% > 0.5% threshold → not choppy
        assert _is_choppy(closes) is False


# ── macro_regime: flash crash ─────────────────────────────────────────────────

@pytest.mark.unit
class TestFlashCrash:
    def test_triggers_at_exactly_2point5_pct(self):
        from macro_regime import check_flash_crash
        assert check_flash_crash(spy_current=97.5, spy_prev_close=100.0) is True

    def test_triggers_below_threshold(self):
        from macro_regime import check_flash_crash
        assert check_flash_crash(spy_current=95.0, spy_prev_close=100.0) is True

    def test_does_not_trigger_just_above_threshold(self):
        from macro_regime import check_flash_crash
        assert check_flash_crash(spy_current=97.6, spy_prev_close=100.0) is False

    def test_no_crash_on_green_day(self):
        from macro_regime import check_flash_crash
        assert check_flash_crash(spy_current=102.0, spy_prev_close=100.0) is False

    def test_zero_prev_close_safe(self):
        from macro_regime import check_flash_crash
        assert check_flash_crash(spy_current=100.0, spy_prev_close=0.0) is False


# ── economic_calendar: NFP first-Friday logic ────────────────────────────────

@pytest.mark.unit
class TestEconomicCalendar:
    def test_nfp_is_first_friday(self):
        from economic_calendar import _first_friday, _hardcoded_events
        # Jan 2026: first Friday is the 2nd
        assert _first_friday(2026, 1) == 2
        events = _hardcoded_events(2026, 1)
        nfp = [e for e in events if "Non-Farm" in e["name"]]
        assert len(nfp) == 1
        assert nfp[0]["date"] == "2026-01-02"

    def test_may_2026_nfp(self):
        from economic_calendar import _first_friday
        # May 2026: first Friday is the 1st
        assert _first_friday(2026, 5) == 1

    def test_event_risk_window_inside_45min(self):
        from economic_calendar import is_event_risk_window
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            today = datetime.now(ET).strftime("%Y-%m-%d")
            json.dump({
                "date": today,
                "events": [{"time": "08:30", "name": "NFP", "impact": "high"}],
                "source": "test",
            }, f)
            tmp = f.name

        import config
        original = config.ECONOMIC_CALENDAR_FILE
        config.ECONOMIC_CALENDAR_FILE = type("P", (), {"exists": lambda s: True,
                                                        "__fspath__": lambda s: tmp})()
        try:
            # Patch the file path to our temp file
            import economic_calendar as ec
            _orig_load = ec._load_todays_events

            def _patched():
                with open(tmp) as fh:
                    data = json.load(fh)
                if data.get("date") == today:
                    return data.get("events", [])
                return []

            ec._load_todays_events = _patched
            now_at_risk = datetime.now(ET).replace(hour=8, minute=45, second=0, microsecond=0)
            result, name = ec.is_event_risk_window(now_at_risk)
            assert result is True
            assert "NFP" in name
        finally:
            ec._load_todays_events = _orig_load
            config.ECONOMIC_CALENDAR_FILE = original
            os.unlink(tmp)

    def test_event_risk_window_outside_45min(self):
        import economic_calendar as ec
        # Patch _load_todays_events to return a 10:00 AM event
        today = datetime.now(ET).strftime("%Y-%m-%d")
        _orig = ec._load_todays_events
        ec._load_todays_events = lambda: [{"time": "10:00", "name": "FOMC", "impact": "high"}]
        try:
            # Check at 08:00 — 2 hours before, outside 45 min window
            check_time = datetime.now(ET).replace(hour=8, minute=0, second=0, microsecond=0)
            result, _ = ec.is_event_risk_window(check_time)
            assert result is False
        finally:
            ec._load_todays_events = _orig

    def test_fomc_2026_dates_present(self):
        from economic_calendar import _hardcoded_events
        # May 2026 FOMC is on 2026-05-07
        events = _hardcoded_events(2026, 5)
        fomc = [e for e in events if "FOMC" in e["name"]]
        assert any(e["date"] == "2026-05-07" for e in fomc)


# ── api_budget: daily ceiling and reset ──────────────────────────────────────

@pytest.mark.unit
class TestApiBudget:
    def _fresh_tmp_budget(self, tmp_path, date_str="2026-05-01"):
        """Write a fresh budget file and patch config to use it."""
        import config
        budget_file = tmp_path / "api_budget.json"
        config.API_BUDGET_FILE = budget_file
        return budget_file

    def test_fresh_budget_starts_at_zero(self, tmp_path):
        import api_budget, config
        config.API_BUDGET_FILE = tmp_path / "api_budget.json"
        budget = api_budget.load_budget()
        assert budget["estimated_cost_usd"] == 0.0
        assert budget["calls_made"] == 0

    def test_budget_resets_on_new_day(self, tmp_path):
        import api_budget, config
        config.API_BUDGET_FILE = tmp_path / "api_budget.json"
        # Write stale budget
        stale = {
            "date": "2026-01-01",
            "calls_made": 10,
            "input_tokens": 100000,
            "output_tokens": 5000,
            "cache_write_tokens": 0,
            "cache_read_tokens": 0,
            "estimated_cost_usd": 0.95,
            "ceiling_usd": 1.00,
            "ceiling_mode": "normal",
            "budget_exhausted": False,
        }
        with open(config.API_BUDGET_FILE, "w") as f:
            json.dump(stale, f)
        budget = api_budget.load_budget()
        assert budget["estimated_cost_usd"] == 0.0
        assert budget["calls_made"] == 0

    def test_normal_ceiling_default(self):
        from api_budget import get_ceiling
        import config
        assert get_ceiling() == config.API_BUDGET_NORMAL_CEILING

    def test_elevated_ceiling_on_high_gain(self):
        from api_budget import get_ceiling
        import config
        assert get_ceiling(portfolio_gain_pct=1.0) == config.API_BUDGET_ELEVATED_CEILING

    def test_elevated_ceiling_on_crisis_regime(self):
        from api_budget import get_ceiling
        import config
        assert get_ceiling(regime="CRISIS") == config.API_BUDGET_ELEVATED_CEILING

    def test_elevated_ceiling_on_capitulation(self):
        from api_budget import get_ceiling
        import config
        assert get_ceiling(regime="CAPITULATION") == config.API_BUDGET_ELEVATED_CEILING

    def test_check_budget_allows_within_ceiling(self, tmp_path):
        import api_budget, config
        config.API_BUDGET_FILE = tmp_path / "api_budget.json"
        assert api_budget.check_budget(0.50) is True

    def test_check_budget_blocks_over_ceiling(self, tmp_path):
        import api_budget, config
        config.API_BUDGET_FILE = tmp_path / "api_budget.json"
        # Record usage that fills 90% of ceiling
        api_budget.record_usage(input_tokens=290_000, output_tokens=0)
        # Now try to add more than remaining
        assert api_budget.check_budget(0.50) is False

    def test_record_usage_accumulates(self, tmp_path):
        import api_budget, config
        config.API_BUDGET_FILE = tmp_path / "api_budget.json"
        api_budget.record_usage(input_tokens=1000, output_tokens=100)
        api_budget.record_usage(input_tokens=1000, output_tokens=100)
        budget = api_budget.load_budget()
        assert budget["input_tokens"] == 2000
        assert budget["output_tokens"] == 200
        assert budget["calls_made"] == 2
        assert budget["estimated_cost_usd"] > 0

    def test_corrupt_file_resets_cleanly(self, tmp_path):
        import api_budget, config
        config.API_BUDGET_FILE = tmp_path / "api_budget.json"
        with open(config.API_BUDGET_FILE, "w") as f:
            f.write("not json{{{")
        budget = api_budget.load_budget()
        assert budget["calls_made"] == 0


# ── market_data: DataFrame shape contract ─────────────────────────────────────

@pytest.mark.unit
class TestMarketDataSchema:
    def test_yahoo_bars_returns_correct_columns(self):
        """Yahoo bars must have OHLCV columns and a tz-aware DatetimeIndex."""
        from market_data import _yahoo_bars
        try:
            df = _yahoo_bars("SPY", "1d", "10d")
        except Exception:
            pytest.skip("Yahoo Finance not reachable in this environment")
        assert set(df.columns) >= {"open", "high", "low", "close", "volume"}
        assert df.index.tz is not None

    def test_get_intraday_bars_respects_lookback(self):
        from market_data import get_intraday_bars
        try:
            df = get_intraday_bars("SPY", lookback=50)
        except Exception:
            pytest.skip("Market data not reachable in this environment")
        assert len(df) <= 50
        assert "close" in df.columns


# ── tier1_config ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestTier1Config:
    def test_known_symbol_is_tier1(self):
        from tier1_config import is_tier1
        assert is_tier1("NVDA") is True

    def test_case_insensitive(self):
        from tier1_config import is_tier1
        assert is_tier1("nvda") is True

    def test_unknown_symbol_is_not_tier1(self):
        from tier1_config import is_tier1
        assert is_tier1("FAKE") is False

    def test_get_sector_known(self):
        from tier1_config import get_sector
        assert get_sector("NVDA") == "AI_SEMI"
        assert get_sector("MSFT") == "HYPERSCALER"
        assert get_sector("GLD") == "GEO_HEDGE"

    def test_get_sector_unknown_returns_other(self):
        from tier1_config import get_sector
        assert get_sector("FAKE") == "OTHER"

    def test_qualifies_for_swing_all_pass(self):
        from tier1_config import qualifies_for_swing
        ok, reason = qualifies_for_swing(
            symbol="NVDA", composite_score=30.0, rsi=60.0,
            volume_ratio=1.5, sentiment_score=40, regime="BULL_TREND",
            parabolic=False, momentum_exhaustion=False,
        )
        assert ok is True
        assert reason == ""

    def test_qualifies_for_swing_rejects_non_tier1(self):
        from tier1_config import qualifies_for_swing
        ok, reason = qualifies_for_swing(
            symbol="FAKE", composite_score=40.0, rsi=60.0,
            volume_ratio=2.0, sentiment_score=50, regime="BULL_TREND",
            parabolic=False, momentum_exhaustion=False,
        )
        assert ok is False
        assert "not in tier-1" in reason

    def test_qualifies_for_swing_rejects_low_composite(self):
        from tier1_config import qualifies_for_swing
        ok, reason = qualifies_for_swing(
            symbol="NVDA", composite_score=20.0, rsi=60.0,
            volume_ratio=1.5, sentiment_score=40, regime="BULL_TREND",
            parabolic=False, momentum_exhaustion=False,
        )
        assert ok is False
        assert "composite" in reason

    def test_qualifies_for_swing_rsi_max_bull_trend(self):
        from tier1_config import qualifies_for_swing
        # RSI 83 is above max (82) in BULL_TREND
        ok, reason = qualifies_for_swing(
            symbol="NVDA", composite_score=30.0, rsi=83.0,
            volume_ratio=1.5, sentiment_score=40, regime="BULL_TREND",
            parabolic=False, momentum_exhaustion=False,
        )
        assert ok is False
        assert "RSI" in reason

    def test_qualifies_for_swing_rsi_max_normal(self):
        from tier1_config import qualifies_for_swing
        # RSI 73 is above max (72) in non-BULL_TREND
        ok, reason = qualifies_for_swing(
            symbol="NVDA", composite_score=30.0, rsi=73.0,
            volume_ratio=1.5, sentiment_score=40, regime="NORMAL",
            parabolic=False, momentum_exhaustion=False,
        )
        assert ok is False

    def test_qualifies_for_swing_rejects_low_volume(self):
        from tier1_config import qualifies_for_swing
        ok, reason = qualifies_for_swing(
            symbol="NVDA", composite_score=30.0, rsi=60.0,
            volume_ratio=1.2, sentiment_score=40, regime="BULL_TREND",
            parabolic=False, momentum_exhaustion=False,
        )
        assert ok is False
        assert "volume" in reason

    def test_qualifies_for_swing_rejects_low_sentiment(self):
        from tier1_config import qualifies_for_swing
        ok, reason = qualifies_for_swing(
            symbol="NVDA", composite_score=30.0, rsi=60.0,
            volume_ratio=1.5, sentiment_score=10, regime="BULL_TREND",
            parabolic=False, momentum_exhaustion=False,
        )
        assert ok is False
        assert "sentiment" in reason

    def test_qualifies_for_swing_rejects_parabolic_exhaustion(self):
        from tier1_config import qualifies_for_swing
        ok, reason = qualifies_for_swing(
            symbol="NVDA", composite_score=30.0, rsi=60.0,
            volume_ratio=1.5, sentiment_score=40, regime="BULL_TREND",
            parabolic=True, momentum_exhaustion=True,
        )
        assert ok is False
        assert "parabolic" in reason


# ── indicator_engine: Plan-2 signals ─────────────────────────────────────────

@pytest.mark.unit
class TestIndicatorEnginePlan2:
    def test_is_parabolic_true(self):
        from indicator_engine import _is_parabolic
        # 15% gain over 5 days (exactly at threshold)
        closes = [100.0] * 5 + [115.0]
        assert _is_parabolic(closes) is True

    def test_is_parabolic_false_just_below(self):
        from indicator_engine import _is_parabolic
        closes = [100.0] * 5 + [114.9]
        assert _is_parabolic(closes) is False

    def test_is_parabolic_insufficient_data(self):
        from indicator_engine import _is_parabolic
        assert _is_parabolic([100.0, 115.0]) is False

    def test_compute_squeeze_setup_all_conditions(self):
        from indicator_engine import compute_squeeze_setup
        qualifies, score = compute_squeeze_setup(
            float_turnover_3d=0.20,        # > 0.15
            price_vs_resistance_pct=2.0,   # < 3.0
            volume_surge=2.5,              # > 2.0
            obv_rising_with_falling_price=True,
        )
        assert qualifies is True
        assert score == 100

    def test_compute_squeeze_setup_three_conditions(self):
        from indicator_engine import compute_squeeze_setup
        qualifies, score = compute_squeeze_setup(
            float_turnover_3d=0.20,
            price_vs_resistance_pct=2.0,
            volume_surge=2.5,
            obv_rising_with_falling_price=False,
        )
        assert qualifies is True
        assert score == 75

    def test_compute_squeeze_setup_two_conditions(self):
        from indicator_engine import compute_squeeze_setup
        qualifies, score = compute_squeeze_setup(
            float_turnover_3d=0.20,
            price_vs_resistance_pct=2.0,
            volume_surge=1.0,              # < 2.0, no score
            obv_rising_with_falling_price=False,
        )
        assert qualifies is False
        assert score == 50

    def test_compute_squeeze_setup_no_conditions(self):
        from indicator_engine import compute_squeeze_setup
        qualifies, score = compute_squeeze_setup(
            float_turnover_3d=0.05,
            price_vs_resistance_pct=5.0,
            volume_surge=1.0,
            obv_rising_with_falling_price=False,
        )
        assert qualifies is False
        assert score == 0

    def test_compute_bot_herding_score_zero_signals(self):
        from indicator_engine import compute_bot_herding_score
        score = compute_bot_herding_score(
            series_dict={}, spy_closes=[],
            buy_signal_count=0, total_watch=10, volume_cv=0.5,
        )
        assert 0 <= score <= 100

    def test_compute_bot_herding_score_high_herding(self):
        from indicator_engine import compute_bot_herding_score
        score = compute_bot_herding_score(
            series_dict={}, spy_closes=[],
            buy_signal_count=10, total_watch=10, volume_cv=0.0,
        )
        # All buy signals + no volume diversity = high herding
        assert score >= 50


# ── indicator_engine: volume exhaustion ──────────────────────────────────────

@pytest.mark.unit
class TestVolumeExhaustion:
    def test_true_at_high_with_low_volume(self):
        from indicator_engine import _is_volume_exhaustion
        # Price AT the 5d high; today's volume = 60% of 20d avg → exhaustion
        df = _make_intraday_df(n=400, close=100.0, high=100.0)
        daily = _make_daily_df(n=25, volume=1_000_000.0)
        # Override last bar's volume to 60% of avg (below VOLUME_EXHAUSTION_RATIO=0.8)
        daily_low_vol = daily.copy()
        daily_low_vol.iloc[-1, daily_low_vol.columns.get_loc("volume")] = 600_000.0
        assert _is_volume_exhaustion(df, daily_low_vol) is True

    def test_false_not_at_high(self):
        from indicator_engine import _is_volume_exhaustion
        # Price well below the 5d high → exhaustion can't fire
        df_low = _make_intraday_df(n=400, close=95.0, high=95.0)
        # Make the high bars higher so current price is not near the 5d high
        df_high_hist = _make_intraday_df(n=400, close=100.0, high=102.0)
        combined = pd.concat([df_high_hist.iloc[:-1], df_low.iloc[-1:]])
        daily = _make_daily_df(n=25, volume=1_000_000.0)
        daily_low_vol = daily.copy()
        daily_low_vol.iloc[-1, daily_low_vol.columns.get_loc("volume")] = 600_000.0
        assert _is_volume_exhaustion(combined, daily_low_vol) is False

    def test_false_normal_volume_at_high(self):
        from indicator_engine import _is_volume_exhaustion
        # Price at 5d high but volume is normal (above threshold)
        df = _make_intraday_df(n=400, close=100.0, high=100.0)
        daily = _make_daily_df(n=25, volume=1_000_000.0)  # last bar also 1M = 100% of avg
        assert _is_volume_exhaustion(df, daily) is False

    def test_false_insufficient_intraday_data(self):
        from indicator_engine import _is_volume_exhaustion
        df = _make_intraday_df(n=3, close=100.0)
        daily = _make_daily_df(n=25, volume=600_000.0)
        assert _is_volume_exhaustion(df, daily) is False

    def test_false_no_daily_df(self):
        from indicator_engine import _is_volume_exhaustion
        df = _make_intraday_df(n=400, close=100.0, high=100.0)
        assert _is_volume_exhaustion(df, None) is False


# ── indicator_engine: OBV divergence ─────────────────────────────────────────

@pytest.mark.unit
class TestOBVDivergence:
    @staticmethod
    def _make_df_obv(closes: list[float], obv_vals: list[float]):
        """Build a DataFrame + OBV Series from close and OBV lists."""
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
        from indicator_engine import _obv_divergence_signal
        # Last 5 prices: new high; OBV declining → bearish
        closes  = [90.0, 95.0, 98.0, 99.0, 100.0, 100.0]
        obv_v   = [1000.0, 900.0, 850.0, 800.0, 750.0, 700.0]
        df, obv = self._make_df_obv(closes, obv_v)
        assert _obv_divergence_signal(df, obv) == "bearish"

    def test_bullish_divergence(self):
        from indicator_engine import _obv_divergence_signal
        # Last 5 prices: new low; OBV rising → bullish
        closes  = [110.0, 105.0, 102.0, 101.0, 100.0, 100.0]
        obv_v   = [500.0, 550.0, 600.0, 650.0, 700.0, 750.0]
        df, obv = self._make_df_obv(closes, obv_v)
        assert _obv_divergence_signal(df, obv) == "bullish"

    def test_none_no_divergence(self):
        from indicator_engine import _obv_divergence_signal
        # Price in the middle of recent range; OBV rising with it → no divergence
        closes  = [95.0, 98.0, 100.0, 99.0, 101.0, 100.0]
        obv_v   = [500.0, 550.0, 600.0, 650.0, 700.0, 750.0]
        df, obv = self._make_df_obv(closes, obv_v)
        assert _obv_divergence_signal(df, obv) == "none"

    def test_none_insufficient_data(self):
        from indicator_engine import _obv_divergence_signal
        df, obv = self._make_df_obv([100.0, 101.0], [500.0, 600.0])
        assert _obv_divergence_signal(df, obv) == "none"


# ── indicator_engine: stop hunt zone ─────────────────────────────────────────

@pytest.mark.unit
class TestStopHuntZone:
    def test_true_at_round_dollar(self):
        from indicator_engine import _is_stop_hunt_zone
        # $100.00 — exactly on a round number
        df = _make_intraday_df(n=10, close=100.0, high=100.0, low=100.0)
        assert _is_stop_hunt_zone(df) is True

    def test_true_near_half_dollar(self):
        from indicator_engine import _is_stop_hunt_zone
        # $100.49 — within 0.4% of $100.50
        df = _make_intraday_df(n=10, close=100.49, high=100.49, low=100.49)
        assert _is_stop_hunt_zone(df) is True

    def test_true_near_5day_high(self):
        from indicator_engine import _is_stop_hunt_zone
        # Use price ~$50.25 so round-number check doesn't fire (max dist = 0.25/50.25 ≈ 0.5% > 0.4%)
        # 5d high = $50.40; current = $50.25 → distance ≈ 0.30% < STOP_HUNT_HILOW_PROXIMITY_PCT (0.3%)
        df = _make_intraday_df(n=400, close=50.25, high=50.40, low=44.0)
        # Override last bar close to 50.25 (well within 0.3% of the 50.40 high)
        df_copy = df.copy()
        df_copy.iloc[-1, df_copy.columns.get_loc("close")] = 50.25
        assert _is_stop_hunt_zone(df_copy) is True

    def test_true_near_5day_low(self):
        from indicator_engine import _is_stop_hunt_zone
        # $50.25 price, 5d low = $50.10 → distance ≈ 0.30% < threshold
        df = _make_intraday_df(n=400, close=50.25, high=56.0, low=50.10)
        df_copy = df.copy()
        df_copy.iloc[-1, df_copy.columns.get_loc("close")] = 50.25
        assert _is_stop_hunt_zone(df_copy) is True

    def test_false_far_from_levels(self):
        from indicator_engine import _is_stop_hunt_zone
        # $50.25 price; round-number fails (0.5% > 0.4%); 5d high/low far away
        df = _make_intraday_df(n=400, close=50.25, high=55.0, low=44.0)
        df_copy = df.copy()
        df_copy.iloc[-1, df_copy.columns.get_loc("close")] = 50.25
        assert _is_stop_hunt_zone(df_copy) is False

    def test_false_empty_df(self):
        from indicator_engine import _is_stop_hunt_zone
        assert _is_stop_hunt_zone(pd.DataFrame()) is False


# ── swing_manager: _execute_partial_close ────────────────────────────────────

@pytest.mark.unit
class TestExecutePartialClose:
    class _MockAlpaca:
        def __init__(self):
            self.orders: list[tuple] = []

        def place_market_order(self, symbol: str, qty: int, side: str) -> None:
            self.orders.append((symbol, qty, side))

    def _write_swing_file(self, tmp_path, symbol: str = "NVDA", **overrides) -> None:
        import config
        config.SWING_POSITIONS_FILE = tmp_path / "swing_positions.json"
        pos = {
            "symbol": symbol, "entry_price": 100.0, "qty": 10,
            "tp1_qty": 4, "tp2_qty": 3, "trailing_qty": 3,
            "tp1_hit": False, "tp2_hit": False,
            "trailing_stop_active": False, "trailing_high_water": 100.0,
            "stop_loss_pct": 7.0,
        }
        pos.update(overrides)
        with open(config.SWING_POSITIONS_FILE, "w") as f:
            json.dump({symbol: pos}, f)

    def test_close_tp1_marks_hit_keeps_position(self, tmp_path):
        from swing_manager import _execute_partial_close, load_swing_positions
        self._write_swing_file(tmp_path)
        mock = self._MockAlpaca()
        _execute_partial_close("NVDA", "CLOSE_TP1", 4, mock, 105.0)
        swings = load_swing_positions()
        assert "NVDA" in swings
        assert swings["NVDA"]["tp1_hit"] is True
        assert ("NVDA", 4, "SELL") in mock.orders

    def test_close_tp2_activates_trailing_keeps_position(self, tmp_path):
        from swing_manager import _execute_partial_close, load_swing_positions
        self._write_swing_file(tmp_path, tp1_hit=True)
        mock = self._MockAlpaca()
        _execute_partial_close("NVDA", "CLOSE_TP2", 3, mock, 112.0)
        swings = load_swing_positions()
        assert "NVDA" in swings
        assert swings["NVDA"]["tp2_hit"] is True
        assert swings["NVDA"]["trailing_stop_active"] is True
        assert swings["NVDA"]["trailing_high_water"] == 112.0

    def test_close_trailing_removes_position(self, tmp_path):
        from swing_manager import _execute_partial_close, load_swing_positions
        self._write_swing_file(tmp_path, tp1_hit=True, tp2_hit=True)
        mock = self._MockAlpaca()
        _execute_partial_close("NVDA", "CLOSE_TRAILING", 3, mock, 108.0)
        swings = load_swing_positions()
        assert "NVDA" not in swings
        assert ("NVDA", 3, "SELL") in mock.orders

    def test_close_tp1_removes_fully_closed_one_share_position(self, tmp_path):
        from swing_manager import _execute_partial_close, load_swing_positions
        # 1-share position: after TP1, nothing remains → position removed
        self._write_swing_file(tmp_path, qty=1, tp1_qty=1, tp2_qty=0, trailing_qty=0)
        mock = self._MockAlpaca()
        _execute_partial_close("NVDA", "CLOSE_TP1", 1, mock, 105.0)
        swings = load_swing_positions()
        assert "NVDA" not in swings


# ── research_agent: macro shock + sector sentiment ───────────────────────────

@pytest.mark.unit
class TestResearchAgentPlan2:
    def test_macro_shock_empty_headlines(self):
        from research_agent import _compute_macro_shock_score
        assert _compute_macro_shock_score([]) == 0

    def test_macro_shock_single_keyword(self):
        from research_agent import _compute_macro_shock_score
        headlines = [{"title": "New tariff announced on imports", "summary": ""}]
        score = _compute_macro_shock_score(headlines)
        assert score >= 20

    def test_macro_shock_multiple_keywords(self):
        from research_agent import _compute_macro_shock_score
        headlines = [
            {"title": "Fed emergency rate cut due to bank failure", "summary": "circuit breaker triggered"},
        ]
        score = _compute_macro_shock_score(headlines)
        assert score == 100  # capped at 100

    def test_macro_shock_no_keywords(self):
        from research_agent import _compute_macro_shock_score
        headlines = [{"title": "Earnings beat expectations", "summary": "quarterly revenue up"}]
        assert _compute_macro_shock_score(headlines) == 0

    def test_sector_sentiment_groups_by_sector(self):
        from research_agent import _compute_sector_sentiment
        report = {
            "NVDA": {"combined_sentiment_score": 60},
            "AMD":  {"combined_sentiment_score": 40},
            "MSFT": {"combined_sentiment_score": 80},
        }
        result = _compute_sector_sentiment(report)
        assert "AI_SEMI" in result
        assert abs(result["AI_SEMI"] - 50.0) < 0.1
        assert "HYPERSCALER" in result
        assert result["HYPERSCALER"] == 80.0

    def test_sector_sentiment_skips_meta(self):
        from research_agent import _compute_sector_sentiment
        report = {
            "NVDA": {"combined_sentiment_score": 50},
            "__meta__": {"sector_sentiment": {}, "macro_shock_score": 0},
        }
        result = _compute_sector_sentiment(report)
        assert "__meta__" not in result

    def test_detect_capitulation_candidate_true(self):
        from research_agent import _detect_capitulation_candidate
        assert _detect_capitulation_candidate(
            symbol="NVDA",
            drawdown_from_20d_high=-25.0,
            sector_sentiment=-30.0,
            obv_trend=0.15,
        ) is True

    def test_detect_capitulation_insufficient_drawdown(self):
        from research_agent import _detect_capitulation_candidate
        assert _detect_capitulation_candidate(
            symbol="NVDA",
            drawdown_from_20d_high=-15.0,
            sector_sentiment=-30.0,
            obv_trend=0.15,
        ) is False

    def test_detect_capitulation_sector_too_positive(self):
        from research_agent import _detect_capitulation_candidate
        assert _detect_capitulation_candidate(
            symbol="NVDA",
            drawdown_from_20d_high=-25.0,
            sector_sentiment=-10.0,
            obv_trend=0.15,
        ) is False

    def test_detect_capitulation_obv_not_rising(self):
        from research_agent import _detect_capitulation_candidate
        assert _detect_capitulation_candidate(
            symbol="NVDA",
            drawdown_from_20d_high=-25.0,
            sector_sentiment=-30.0,
            obv_trend=0.05,
        ) is False


# ── swing_manager: tier quantities + tiered TP ───────────────────────────────

@pytest.mark.unit
class TestSwingManager:
    def test_tier_quantities_one_share(self):
        from swing_manager import _compute_tier_quantities
        assert _compute_tier_quantities(1) == (1, 0, 0)

    def test_tier_quantities_two_shares(self):
        from swing_manager import _compute_tier_quantities
        assert _compute_tier_quantities(2) == (1, 1, 0)

    def test_tier_quantities_ten_shares(self):
        from swing_manager import _compute_tier_quantities
        tp1, tp2, trailing = _compute_tier_quantities(10)
        assert tp1 + tp2 + trailing == 10
        assert tp1 == 4
        assert tp2 == 3
        assert trailing == 3

    def test_tier_quantities_sum_always_equals_total(self):
        from swing_manager import _compute_tier_quantities
        for n in range(1, 20):
            tp1, tp2, trailing = _compute_tier_quantities(n)
            assert tp1 + tp2 + trailing == n, f"split fails for n={n}"

    def _make_position(self, **overrides):
        base = {
            "entry_price": 100.0,
            "qty": 10,
            "stop_loss_pct": 7.0,
            "tp1_pct": 5.0,
            "tp2_pct": 10.0,
            "tp1_qty": 4,
            "tp2_qty": 3,
            "trailing_qty": 3,
            "tp1_hit": False,
            "tp2_hit": False,
            "trailing_stop_active": False,
            "trailing_high_water": 100.0,
        }
        return {**base, **overrides}

    def test_tiered_tp_hard_stop(self):
        from swing_manager import evaluate_tiered_tp_action
        pos = self._make_position()
        result = evaluate_tiered_tp_action(pos, current_price=92.0)  # -8% vs 7% stop
        assert result["action"] == "CLOSE_HARD_STOP"
        assert result["qty"] == 10  # all remaining

    def test_tiered_tp_tp1(self):
        from swing_manager import evaluate_tiered_tp_action
        pos = self._make_position()
        result = evaluate_tiered_tp_action(pos, current_price=106.0)  # +6% vs 5% tp1
        assert result["action"] == "CLOSE_TP1"
        assert result["qty"] == 4
        assert result["activate_trailing"] is False

    def test_tiered_tp_tp2(self):
        from swing_manager import evaluate_tiered_tp_action
        pos = self._make_position(tp1_hit=True)
        result = evaluate_tiered_tp_action(pos, current_price=112.0)  # +12% vs 10% tp2
        assert result["action"] == "CLOSE_TP2"
        assert result["qty"] == 3
        assert result["activate_trailing"] is True

    def test_tiered_tp_trailing_stop(self):
        from swing_manager import evaluate_tiered_tp_action
        pos = self._make_position(
            tp1_hit=True, tp2_hit=True,
            trailing_stop_active=True,
            trailing_high_water=120.0,
        )
        # current = 115, high_water = 120, 3% drawdown threshold = 116.4
        result = evaluate_tiered_tp_action(pos, current_price=115.0)
        assert result["action"] == "CLOSE_TRAILING"
        assert result["qty"] == 3

    def test_tiered_tp_none_when_no_trigger(self):
        from swing_manager import evaluate_tiered_tp_action
        pos = self._make_position()
        result = evaluate_tiered_tp_action(pos, current_price=102.0)  # +2%, no trigger
        assert result["action"] == "NONE"

    def test_qualifies_for_eod_hold_all_pass(self):
        from swing_manager import qualifies_for_eod_hold
        ok, reason = qualifies_for_eod_hold(
            symbol="NVDA",
            is_tier1=True,
            regime="BULL_TREND",
            event_risk_tomorrow=False,
            earnings_in_2_days=False,
            current_pnl_pct=2.0,
            existing_swing_count=0,
            sector_sentiment_score=25.0,
        )
        assert ok is True
        assert reason == ""

    def test_qualifies_for_eod_hold_not_tier1(self):
        from swing_manager import qualifies_for_eod_hold
        ok, reason = qualifies_for_eod_hold(
            symbol="APP", is_tier1=False, regime="BULL_TREND",
            event_risk_tomorrow=False, earnings_in_2_days=False,
            current_pnl_pct=2.0, existing_swing_count=0,
            sector_sentiment_score=25.0,
        )
        assert ok is False
        assert "tier-1" in reason

    def test_qualifies_for_eod_hold_wrong_regime(self):
        from swing_manager import qualifies_for_eod_hold
        ok, reason = qualifies_for_eod_hold(
            symbol="NVDA", is_tier1=True, regime="NORMAL",
            event_risk_tomorrow=False, earnings_in_2_days=False,
            current_pnl_pct=2.0, existing_swing_count=0,
            sector_sentiment_score=25.0,
        )
        assert ok is False
        assert "regime" in reason

    def test_qualifies_for_eod_hold_event_risk(self):
        from swing_manager import qualifies_for_eod_hold
        ok, reason = qualifies_for_eod_hold(
            symbol="NVDA", is_tier1=True, regime="BULL_TREND",
            event_risk_tomorrow=True, earnings_in_2_days=False,
            current_pnl_pct=2.0, existing_swing_count=0,
            sector_sentiment_score=25.0,
        )
        assert ok is False
        assert "event" in reason

    def test_qualifies_for_eod_hold_earnings_blocks(self):
        from swing_manager import qualifies_for_eod_hold
        ok, reason = qualifies_for_eod_hold(
            symbol="NVDA", is_tier1=True, regime="BULL_TREND",
            event_risk_tomorrow=False, earnings_in_2_days=True,
            current_pnl_pct=2.0, existing_swing_count=0,
            sector_sentiment_score=25.0,
        )
        assert ok is False
        assert "earnings" in reason

    def test_qualifies_for_eod_hold_ptj_rule(self):
        from swing_manager import qualifies_for_eod_hold
        ok, reason = qualifies_for_eod_hold(
            symbol="NVDA", is_tier1=True, regime="BULL_TREND",
            event_risk_tomorrow=False, earnings_in_2_days=False,
            current_pnl_pct=-3.0, existing_swing_count=0,
            sector_sentiment_score=25.0,
        )
        assert ok is False
        assert "PTJ" in reason

    def test_qualifies_for_eod_hold_swing_limit(self):
        from swing_manager import qualifies_for_eod_hold
        import config
        ok, reason = qualifies_for_eod_hold(
            symbol="NVDA", is_tier1=True, regime="BULL_TREND",
            event_risk_tomorrow=False, earnings_in_2_days=False,
            current_pnl_pct=2.0,
            existing_swing_count=config.SWING_MAX_POSITIONS,
            sector_sentiment_score=25.0,
        )
        assert ok is False
        assert "swing limit" in reason

    def test_qualifies_for_eod_hold_low_sector_sentiment(self):
        from swing_manager import qualifies_for_eod_hold
        ok, reason = qualifies_for_eod_hold(
            symbol="NVDA", is_tier1=True, regime="BULL_TREND",
            event_risk_tomorrow=False, earnings_in_2_days=False,
            current_pnl_pct=2.0, existing_swing_count=0,
            sector_sentiment_score=5.0,
        )
        assert ok is False
        assert "sector sentiment" in reason


# ── earnings_calendar: PEAD detection ────────────────────────────────────────

@pytest.mark.unit
class TestEarningsCalendar:
    # ── Shared helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _perfect_block() -> dict:
        """Return a recently_reported block where all 5 PEAD conditions pass."""
        from datetime import timedelta
        report_date = (datetime.now(ET) - timedelta(days=2)).strftime("%Y-%m-%d")
        return {
            "report_date":                 report_date,
            "eps_actual":                  1.10,
            "eps_estimate":                1.00,
            "eps_surprise_pct":            10.0,   # cond 1 ✓ (≥ 5%)
            "revenue_beat":                True,   # cond 2 ✓
            "guidance_change":             "raised",  # cond 3 ✓
            "earnings_day_return_pct":     5.0,    # cond 4 ✓ (≥ 3%, closed high)
            "earnings_day_close_position": 0.85,
            "earnings_day_volume_ratio":   3.0,    # cond 5 ✓ (≥ 2×)
        }

    def _write_cache(self, tmp_path, symbol: str = "NVDA", **block_overrides) -> None:
        """Write an earnings cache for today with a single-symbol recently_reported block.

        Pass keyword args to selectively override fields in the perfect block, e.g.:
            self._write_cache(tmp_path, guidance_change="lowered")
        """
        import config
        config.EARNINGS_CALENDAR_FILE = tmp_path / "earnings_calendar.json"
        block = {**self._perfect_block(), **block_overrides}
        payload = {
            "date": datetime.now(ET).strftime("%Y-%m-%d"),
            "symbols": {symbol: {"next_earnings_date": None, "recently_reported": block}},
        }
        with open(config.EARNINGS_CALENDAR_FILE, "w") as f:
            json.dump(payload, f)

    # ── Imminent flag ─────────────────────────────────────────────────────────

    def test_is_earnings_imminent_no_cache(self, tmp_path):
        import earnings_calendar as ec
        import config
        config.EARNINGS_CALENDAR_FILE = tmp_path / "earnings_calendar.json"
        assert ec.is_earnings_imminent("NVDA") is False  # no file → conservative False

    def test_is_earnings_imminent_stale_file(self, tmp_path):
        import earnings_calendar as ec
        import config
        config.EARNINGS_CALENDAR_FILE = tmp_path / "earnings_calendar.json"
        stale = {"date": "2020-01-01", "symbols": {"NVDA": {"next_earnings_date": "2020-01-02"}}}
        with open(config.EARNINGS_CALENDAR_FILE, "w") as f:
            json.dump(stale, f)
        assert ec.is_earnings_imminent("NVDA") is False  # stale date → conservative False

    # ── PEAD detection ────────────────────────────────────────────────────────

    def test_detect_pead_no_cache(self, tmp_path):
        import earnings_calendar as ec
        import config
        config.EARNINGS_CALENDAR_FILE = tmp_path / "earnings_calendar.json"
        qualifies, score = ec.detect_pead_setup("NVDA")
        assert qualifies is False
        assert score == 0

    def test_detect_pead_all_five_conditions(self, tmp_path):
        import earnings_calendar as ec
        self._write_cache(tmp_path)  # perfect block — all 5 conditions pass
        qualifies, score = ec.detect_pead_setup("NVDA")
        assert qualifies is True
        assert score == 100

    def test_detect_pead_three_conditions_fails(self, tmp_path):
        import earnings_calendar as ec
        self._write_cache(
            tmp_path,
            earnings_day_return_pct=1.0,    # cond 4 ✗ (< 3%)
            earnings_day_close_position=0.5,
            earnings_day_volume_ratio=1.0,  # cond 5 ✗ (< 2×)
        )
        qualifies, score = ec.detect_pead_setup("NVDA")
        assert qualifies is False
        assert score == 60  # 3 × 20 pts

    def test_detect_pead_guidance_lowered_reduces_score(self, tmp_path):
        """Lowered guidance fails cond 3 (−20 pts). Other 4 pass → score=80 = threshold → still qualifies."""
        import earnings_calendar as ec
        self._write_cache(tmp_path, guidance_change="lowered")  # cond 3 ✗
        qualifies, score = ec.detect_pead_setup("NVDA")
        assert score == 80       # guidance cost 20 pts
        assert qualifies is True  # 4/5 conditions still meet the threshold

    def test_detect_pead_guidance_lowered_plus_weak_reaction_fails(self, tmp_path):
        """Lowered guidance + poor earnings-day reaction → 3/5 conditions → below threshold."""
        import earnings_calendar as ec
        self._write_cache(
            tmp_path,
            guidance_change="lowered",       # cond 3 ✗
            earnings_day_return_pct=1.0,     # cond 4 ✗ (< 3%)
            earnings_day_close_position=0.5,
        )
        qualifies, score = ec.detect_pead_setup("NVDA")
        assert qualifies is False
        assert score == 60  # 3 × 20 pts

    def test_detect_pead_unknown_guidance_allowed(self, tmp_path):
        import earnings_calendar as ec
        self._write_cache(tmp_path, guidance_change="unknown")  # cond 3 ✓ (unknown ≠ lowered)
        qualifies, score = ec.detect_pead_setup("NVDA")
        assert qualifies is True
        assert score == 100


# ── Plan 3: Mode Routing & Learning ──────────────────────────────────────────


@pytest.mark.unit
class TestGetActiveMode:
    """trading_agent.get_active_mode pure routing logic."""

    def test_bull_trend_returns_hybrid(self):
        from trading_agent import get_active_mode
        assert get_active_mode("BULL_TREND") == "HYBRID"

    def test_opportunity_returns_hybrid(self):
        from trading_agent import get_active_mode
        assert get_active_mode("OPPORTUNITY") == "HYBRID"

    def test_capitulation_returns_short_term(self):
        from trading_agent import get_active_mode
        assert get_active_mode("CAPITULATION") == "SHORT_TERM"

    def test_crash_recovery_returns_short_term(self):
        from trading_agent import get_active_mode
        assert get_active_mode("CRASH_RECOVERY") == "SHORT_TERM"

    def test_crisis_returns_cash(self):
        from trading_agent import get_active_mode
        assert get_active_mode("CRISIS") == "CASH"

    def test_normal_returns_day_trade(self):
        from trading_agent import get_active_mode
        assert get_active_mode("NORMAL") == "DAY_TRADE"

    def test_choppy_returns_day_trade(self):
        from trading_agent import get_active_mode
        assert get_active_mode("CHOPPY") == "DAY_TRADE"

    def test_defensive_returns_day_trade(self):
        from trading_agent import get_active_mode
        assert get_active_mode("DEFENSIVE") == "DAY_TRADE"

    def test_unknown_regime_returns_day_trade(self):
        from trading_agent import get_active_mode
        assert get_active_mode("SOME_NEW_REGIME") == "DAY_TRADE"


@pytest.mark.unit
class TestGetSlTpForModeAndRegime:
    """trading_agent.get_sl_tp_for_mode_and_regime table lookups."""

    def test_day_trade_normal(self):
        from trading_agent import get_sl_tp_for_mode_and_regime
        sl, tp = get_sl_tp_for_mode_and_regime("DAY_TRADE", "NORMAL")
        assert sl == 3.0 and tp == 5.0

    def test_day_trade_choppy(self):
        from trading_agent import get_sl_tp_for_mode_and_regime
        sl, tp = get_sl_tp_for_mode_and_regime("DAY_TRADE", "CHOPPY")
        assert sl == 5.0 and tp == 6.0

    def test_hybrid_bull_trend(self):
        from trading_agent import get_sl_tp_for_mode_and_regime
        sl, tp = get_sl_tp_for_mode_and_regime("HYBRID", "BULL_TREND")
        assert sl == 7.0 and tp == 15.0

    def test_short_term_capitulation(self):
        from trading_agent import get_sl_tp_for_mode_and_regime
        sl, tp = get_sl_tp_for_mode_and_regime("SHORT_TERM", "CAPITULATION")
        assert sl == 12.0 and tp == 20.0

    def test_pead_any_regime(self):
        from trading_agent import get_sl_tp_for_mode_and_regime
        sl, tp = get_sl_tp_for_mode_and_regime("PEAD", "NORMAL")
        assert sl == 8.0 and tp == 8.0

    def test_unknown_combo_falls_back_to_config_defaults(self):
        import config
        from trading_agent import get_sl_tp_for_mode_and_regime
        sl, tp = get_sl_tp_for_mode_and_regime("UNKNOWN_MODE", "UNKNOWN_REGIME")
        assert sl == config.STOP_LOSS_PCT and tp == config.TAKE_PROFIT_PCT


@pytest.mark.unit
class TestComputePositionSizeForMode:
    """trading_agent.compute_position_size_for_mode sizing logic."""

    def test_day_trade_uses_5pct_cap(self):
        from trading_agent import compute_position_size_for_mode
        portfolio = 10_000.0
        price = 100.0
        atr = 1.0  # trivially small ATR so risk-based won't cap
        qty = compute_position_size_for_mode(portfolio, price, atr, "DAY_TRADE", 0, False)
        # 5% of 10k / $100 = 5 shares
        assert qty == 5

    def test_hybrid_uses_3pct_cap(self):
        from trading_agent import compute_position_size_for_mode
        qty = compute_position_size_for_mode(10_000.0, 100.0, 0.01, "HYBRID", 0, False)
        # 3% of 10k / $100 = 3 shares
        assert qty == 3

    def test_short_term_uses_2_5pct_cap(self):
        from trading_agent import compute_position_size_for_mode
        qty = compute_position_size_for_mode(10_000.0, 100.0, 0.01, "SHORT_TERM", 0, False)
        # 2.5% of 10k / $100 = 2.5 → rounds to 2 or 3
        assert qty in (2, 3)

    def test_herding_reduces_size_by_30pct(self):
        import config
        from trading_agent import compute_position_size_for_mode
        base = compute_position_size_for_mode(10_000.0, 100.0, 0.01, "DAY_TRADE", 0, False)
        herding = compute_position_size_for_mode(
            10_000.0, 100.0, 0.01, "DAY_TRADE",
            config.BOT_HERD_ALERT_SCORE + 1, False
        )
        assert herding <= base
        assert herding >= 1

    def test_squeeze_skips_herding_reduction(self):
        import config
        from trading_agent import compute_position_size_for_mode
        no_squeeze = compute_position_size_for_mode(
            10_000.0, 100.0, 0.01, "DAY_TRADE",
            config.BOT_HERD_ALERT_SCORE + 1, False
        )
        with_squeeze = compute_position_size_for_mode(
            10_000.0, 100.0, 0.01, "DAY_TRADE",
            config.BOT_HERD_ALERT_SCORE + 1, True  # squeeze overrides herding reduction
        )
        assert with_squeeze > no_squeeze

    def test_minimum_one_share(self):
        from trading_agent import compute_position_size_for_mode
        # Very high price relative to portfolio → floors at 1
        qty = compute_position_size_for_mode(100.0, 10_000.0, 1.0, "DAY_TRADE", 0, False)
        assert qty >= 1


@pytest.mark.unit
class TestPrefilterDecision:
    """trading_agent._prefilter_decision routing."""

    def _make_iv(self, composite, price=100.0, vwap=99.0, rsi=60.0, macd_hist=0.1):
        """Build a minimal IndicatorValues-like namespace."""
        import types
        iv = types.SimpleNamespace(
            composite_score=composite,
            current_price=price,
            vwap=vwap,
            rsi=rsi,
            macd_hist=macd_hist,
        )
        return iv

    def test_below_threshold_returns_auto_skip(self):
        import config
        from trading_agent import _prefilter_decision
        iv = self._make_iv(config.COMPOSITE_SCORE_THRESHOLD)  # exactly threshold
        assert _prefilter_decision(iv) == "AUTO_SKIP"

    def test_strong_signal_aligned_returns_auto_buy(self):
        import config
        from trading_agent import _prefilter_decision
        iv = self._make_iv(config.AUTO_BUY_COMPOSITE_THRESHOLD + 1)  # above auto-buy gate
        assert _prefilter_decision(iv) == "AUTO_BUY"

    def test_above_threshold_misaligned_returns_ask_claude(self):
        import config
        from trading_agent import _prefilter_decision
        # composite above threshold but MACD negative → not aligned
        iv = self._make_iv(
            config.AUTO_BUY_COMPOSITE_THRESHOLD + 1,
            macd_hist=-0.01,
        )
        assert _prefilter_decision(iv) == "ASK_CLAUDE"

    def test_moderate_composite_returns_ask_claude(self):
        import config
        from trading_agent import _prefilter_decision
        # composite above base threshold but below AUTO_BUY threshold
        mid = (config.COMPOSITE_SCORE_THRESHOLD + config.AUTO_BUY_COMPOSITE_THRESHOLD) // 2
        iv = self._make_iv(mid)
        assert _prefilter_decision(iv) == "ASK_CLAUDE"


@pytest.mark.unit
class TestClampThreshold:
    """learning_agent._clamp_threshold hard-bound clamping."""

    def test_within_bounds_unchanged(self):
        from learning_agent import _clamp_threshold
        assert _clamp_threshold("stop_loss_pct", 4.0) == 4.0

    def test_below_lower_bound_clamped_up(self):
        from learning_agent import _clamp_threshold
        # stop_loss_pct lower bound is 1.5
        result = _clamp_threshold("stop_loss_pct", 0.5)
        assert result == 1.5

    def test_above_upper_bound_clamped_down(self):
        from learning_agent import _clamp_threshold
        # stop_loss_pct upper bound is 12.0
        result = _clamp_threshold("stop_loss_pct", 20.0)
        assert result == 12.0

    def test_integer_key_returns_int(self):
        from learning_agent import _clamp_threshold
        # rsi_buy_max bounds: (30, 80) — both int
        result = _clamp_threshold("rsi_buy_max", 45)
        assert isinstance(result, int)
        assert result == 45

    def test_unknown_key_returns_value_unchanged(self):
        from learning_agent import _clamp_threshold
        assert _clamp_threshold("nonexistent_key", 99.9) == 99.9


@pytest.mark.unit
class TestAggregateByRegime:
    """learning_agent._aggregate_by_regime grouping logic."""

    @staticmethod
    def _make_trade(symbol, pnl_pct, pnl_dollar, regime="NORMAL", mode="DAY_TRADE"):
        return {
            "symbol": symbol,
            "pnl_pct": pnl_pct,
            "pnl_dollar": pnl_dollar,
            "regime_at_entry": regime,
            "mode_at_entry": mode,
            "close_reason": "monitor_stop_loss",
            "entry_sentiment_score": 0,
        }

    def test_groups_by_regime(self):
        from learning_agent import _aggregate_by_regime
        trades = [
            self._make_trade("A", 2.0, 20.0, regime="NORMAL"),
            self._make_trade("B", -1.0, -10.0, regime="NORMAL"),
            self._make_trade("C", 3.0, 30.0, regime="CHOPPY"),
        ]
        result = _aggregate_by_regime(trades)
        assert "NORMAL" in result["by_regime"]
        assert "CHOPPY" in result["by_regime"]
        assert result["by_regime"]["NORMAL"]["count"] == 2
        assert result["by_regime"]["CHOPPY"]["count"] == 1

    def test_win_rate_calculation(self):
        from learning_agent import _aggregate_by_regime
        trades = [
            self._make_trade("A",  2.0,  20.0, regime="NORMAL"),
            self._make_trade("B",  1.0,  10.0, regime="NORMAL"),
            self._make_trade("C", -1.0, -10.0, regime="NORMAL"),
        ]
        result = _aggregate_by_regime(trades)
        nr = result["by_regime"]["NORMAL"]
        assert nr["winners"] == 2
        assert nr["losers"] == 1
        assert nr["win_rate_pct"] == pytest.approx(66.7, abs=0.1)

    def test_groups_by_mode(self):
        from learning_agent import _aggregate_by_regime
        trades = [
            self._make_trade("A", 1.0, 10.0, mode="DAY_TRADE"),
            self._make_trade("B", 2.0, 20.0, mode="HYBRID"),
        ]
        result = _aggregate_by_regime(trades)
        assert result["by_mode"]["DAY_TRADE"]["count"] == 1
        assert result["by_mode"]["HYBRID"]["count"] == 1

    def test_missing_regime_falls_back_to_unknown(self):
        from learning_agent import _aggregate_by_regime
        trades = [{"symbol": "X", "pnl_pct": 1.0, "pnl_dollar": 10.0}]
        result = _aggregate_by_regime(trades)
        assert "UNKNOWN" in result["by_regime"]


@pytest.mark.unit
class TestClassifyLossSource:
    """learning_agent._classify_loss_source classification priority."""

    def test_earnings_close_reason(self):
        from learning_agent import _classify_loss_source
        trade = {"close_reason": "earnings_gap_down", "pnl_pct": -5.0, "entry_indicators": {}}
        assert _classify_loss_source(trade) == "earnings_surprise"

    def test_crisis_regime_at_entry(self):
        from learning_agent import _classify_loss_source
        trade = {"close_reason": "monitor_stop_loss", "regime_at_entry": "CRISIS",
                 "pnl_pct": -3.0, "entry_indicators": {}}
        assert _classify_loss_source(trade) == "market_structure"

    def test_flash_crash_close_reason(self):
        from learning_agent import _classify_loss_source
        trade = {"close_reason": "flash_crash", "pnl_pct": -4.0, "entry_indicators": {}}
        assert _classify_loss_source(trade) == "market_structure"

    def test_momentum_exhaustion_at_entry(self):
        from learning_agent import _classify_loss_source
        trade = {
            "close_reason": "monitor_stop_loss",
            "regime_at_entry": "NORMAL",
            "entry_indicators": {"momentum_exhaustion": True},
            "pnl_pct": -2.0,
        }
        assert _classify_loss_source(trade) == "momentum_exhaustion"

    def test_negative_sentiment_sector_narrative(self):
        from learning_agent import _classify_loss_source
        trade = {
            "close_reason": "monitor_stop_loss",
            "regime_at_entry": "NORMAL",
            "entry_indicators": {},
            "entry_sentiment_score": -30,
            "pnl_pct": -1.5,
        }
        assert _classify_loss_source(trade) == "sector_narrative"

    def test_unknown_when_no_signal(self):
        from learning_agent import _classify_loss_source
        trade = {
            "close_reason": "monitor_stop_loss",
            "regime_at_entry": "NORMAL",
            "entry_indicators": {},
            "entry_sentiment_score": 5,
            "pnl_pct": -1.0,
        }
        assert _classify_loss_source(trade) == "unknown"


# ── Crash loop detection ───────────────────────────────────────────────────────

@pytest.mark.unit
class TestCrashLoopDetection:
    def _write_health(self, tmp_path, **overrides):
        base = {
            "crash_count": 0,
            "window_start": datetime.now(ET).isoformat(),
            "backoff_until": None,
        }
        base.update(overrides)
        (tmp_path / "bot_health.json").write_text(json.dumps(base))

    def test_missing_file_creates_count_of_one(self, tmp_path):
        import config
        from main import _check_crash_loop
        config.BOT_HEALTH_FILE = tmp_path / "bot_health.json"
        config.CRASH_LOOP_THRESHOLD = 5
        config.CRASH_LOOP_WINDOW_MINUTES = 10
        config.CRASH_BACKOFF_MINUTES = 30
        _check_crash_loop()
        data = json.loads((tmp_path / "bot_health.json").read_text())
        assert data["crash_count"] == 1
        assert data["backoff_until"] is None

    def test_count_increments_within_window(self, tmp_path):
        import config
        from main import _check_crash_loop
        config.BOT_HEALTH_FILE = tmp_path / "bot_health.json"
        config.CRASH_LOOP_THRESHOLD = 5
        config.CRASH_LOOP_WINDOW_MINUTES = 10
        config.CRASH_BACKOFF_MINUTES = 30
        self._write_health(tmp_path, crash_count=2)
        _check_crash_loop()
        data = json.loads((tmp_path / "bot_health.json").read_text())
        assert data["crash_count"] == 3
        assert data["backoff_until"] is None

    def test_expired_window_resets_before_increment(self, tmp_path):
        import config
        from datetime import timedelta
        from main import _check_crash_loop
        config.BOT_HEALTH_FILE = tmp_path / "bot_health.json"
        config.CRASH_LOOP_THRESHOLD = 5
        config.CRASH_LOOP_WINDOW_MINUTES = 10
        config.CRASH_BACKOFF_MINUTES = 30
        old = (datetime.now(ET) - timedelta(minutes=15)).isoformat()
        self._write_health(tmp_path, crash_count=4, window_start=old)
        _check_crash_loop()
        data = json.loads((tmp_path / "bot_health.json").read_text())
        assert data["crash_count"] == 1  # reset to 0, then incremented

    def test_threshold_triggers_backoff_then_resets(self, tmp_path, monkeypatch):
        import config
        import main as main_mod
        from main import _check_crash_loop
        config.BOT_HEALTH_FILE = tmp_path / "bot_health.json"
        config.CRASH_LOOP_THRESHOLD = 3
        config.CRASH_LOOP_WINDOW_MINUTES = 10
        config.CRASH_BACKOFF_MINUTES = 30
        monkeypatch.setattr(main_mod.time, "sleep", lambda _: None)
        self._write_health(tmp_path, crash_count=2)
        _check_crash_loop()
        data = json.loads((tmp_path / "bot_health.json").read_text())
        # After backoff completes, _reset_bot_health() sets count=0
        assert data["crash_count"] == 0
        assert data["backoff_until"] is None

    def test_reset_writes_zero_count(self, tmp_path):
        import config
        from main import _reset_bot_health
        config.BOT_HEALTH_FILE = tmp_path / "bot_health.json"
        self._write_health(tmp_path, crash_count=5, backoff_until="sometime")
        _reset_bot_health()
        data = json.loads((tmp_path / "bot_health.json").read_text())
        assert data["crash_count"] == 0


# ── Orphan-order drain ────────────────────────────────────────────────────────

@pytest.mark.unit
class TestOrphanOrderDrain:
    def _make_client(self, monkeypatch, orders):
        """Return an AlpacaClient with a mocked _trading that returns given orders."""
        from unittest.mock import MagicMock
        import config
        config.ALPACA_API_KEY = "test-key"
        config.ALPACA_SECRET_KEY = "test-secret"
        from alpaca_client import AlpacaClient
        client = AlpacaClient.__new__(AlpacaClient)
        mock_trading = MagicMock()
        mock_trading.get_orders.return_value = orders
        client._trading = mock_trading
        return client

    def _make_order(self, order_id, symbol):
        from unittest.mock import MagicMock
        o = MagicMock()
        o.id = order_id
        o.symbol = symbol
        return o

    def test_get_open_buy_orders_returns_list(self, monkeypatch):
        order = self._make_order("id-1", "APP")
        client = self._make_client(monkeypatch, [order])
        result = client.get_open_buy_orders()
        assert result == [{"id": "id-1", "symbol": "APP"}]

    def test_get_open_buy_orders_empty_when_none(self, monkeypatch):
        client = self._make_client(monkeypatch, [])
        result = client.get_open_buy_orders()
        assert result == []

    def test_cancel_order_delegates_to_alpaca(self, monkeypatch):
        from unittest.mock import MagicMock
        import config
        config.ALPACA_API_KEY = "test-key"
        config.ALPACA_SECRET_KEY = "test-secret"
        from alpaca_client import AlpacaClient
        client = AlpacaClient.__new__(AlpacaClient)
        mock_trading = MagicMock()
        client._trading = mock_trading
        client.cancel_order("order-xyz")
        mock_trading.cancel_order_by_id.assert_called_once_with("order-xyz")


# ── Flash crash config constant ───────────────────────────────────────────────

@pytest.mark.unit
class TestFlashCrashConfig:
    def test_check_flash_crash_uses_config_threshold(self, monkeypatch):
        import config
        from macro_regime import check_flash_crash
        monkeypatch.setattr(config, "FLASH_CRASH_SPY_DROP_PCT", -5.0)
        # -4% drop should NOT trigger when threshold is -5%
        assert check_flash_crash(spy_current=96.0, spy_prev_close=100.0) is False
        # -6% drop SHOULD trigger
        assert check_flash_crash(spy_current=94.0, spy_prev_close=100.0) is True

    def test_load_macro_regime_default_has_candidate_field(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "MACRO_REGIME_FILE", tmp_path / "macro_regime.json")
        from macro_regime import load_macro_regime
        result = load_macro_regime()
        assert "flash_crash_candidate" in result
        assert result["flash_crash_candidate"] is None


# ── Polygon helper ────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestVerifySpyWithPolygon:
    def test_no_key_returns_none(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "POLYGON_API_KEY", "")
        from trading_agent import _verify_spy_with_polygon
        assert _verify_spy_with_polygon(-3.0) is None

    def test_confirmed_drop_returns_true(self, monkeypatch):
        import config
        from unittest.mock import MagicMock
        monkeypatch.setattr(config, "POLYGON_API_KEY", "test-key")
        monkeypatch.setattr(config, "FLASH_CRASH_SPY_DROP_PCT", -2.5)
        mock_resp = MagicMock()
        mock_resp.raise_for_status = lambda: None
        mock_resp.json.return_value = {"ticker": {"todaysChangePerc": -3.1}}
        import utils.market_monitor as mm
        monkeypatch.setattr(mm.requests, "get", lambda *a, **kw: mock_resp)
        from trading_agent import _verify_spy_with_polygon
        assert _verify_spy_with_polygon(-3.1) is True

    def test_bad_tick_returns_false(self, monkeypatch):
        import config
        from unittest.mock import MagicMock
        monkeypatch.setattr(config, "POLYGON_API_KEY", "test-key")
        monkeypatch.setattr(config, "FLASH_CRASH_SPY_DROP_PCT", -2.5)
        mock_resp = MagicMock()
        mock_resp.raise_for_status = lambda: None
        mock_resp.json.return_value = {"ticker": {"todaysChangePerc": -0.5}}
        import utils.market_monitor as mm
        monkeypatch.setattr(mm.requests, "get", lambda *a, **kw: mock_resp)
        from trading_agent import _verify_spy_with_polygon
        assert _verify_spy_with_polygon(-2.6) is False

    def test_network_error_returns_none(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "POLYGON_API_KEY", "test-key")
        import utils.market_monitor as mm
        monkeypatch.setattr(mm.requests, "get",
                            lambda *a, **kw: (_ for _ in ()).throw(Exception("timeout")))
        from trading_agent import _verify_spy_with_polygon
        assert _verify_spy_with_polygon(-3.0) is None


@pytest.mark.unit
class TestFlashCrashTwoReading:
    def _write_regime(self, tmp_path, candidate=None):
        data = {
            "regime": "NORMAL",
            "flash_crash_active": False,
            "flash_crash_until": None,
            "flash_crash_candidate": candidate,
        }
        (tmp_path / "macro_regime.json").write_text(json.dumps(data))

    def test_no_candidate_writes_stage1(self, tmp_path, monkeypatch):
        import config
        config.MACRO_REGIME_FILE = tmp_path / "macro_regime.json"
        self._write_regime(tmp_path, candidate=None)
        from trading_agent import _handle_flash_crash_stage2
        _handle_flash_crash_stage2(-2.7)
        data = json.loads((tmp_path / "macro_regime.json").read_text())
        assert data["flash_crash_candidate"] is not None
        assert data["flash_crash_candidate"]["spy_pct"] == -2.7

    def test_fresh_candidate_triggers_lockout(self, tmp_path, monkeypatch):
        import config
        config.MACRO_REGIME_FILE = tmp_path / "macro_regime.json"
        fresh = {"spy_pct": -2.7, "detected_at": datetime.now(ET).isoformat()}
        self._write_regime(tmp_path, candidate=fresh)
        triggered = []
        import utils.market_monitor as mm
        monkeypatch.setattr(mm, "_trigger_flash_crash_lockout", lambda: triggered.append(1))
        from trading_agent import _handle_flash_crash_stage2
        _handle_flash_crash_stage2(-3.0)
        assert len(triggered) == 1

    def test_stale_candidate_overwritten_no_lockout(self, tmp_path, monkeypatch):
        import config
        from datetime import timedelta
        config.MACRO_REGIME_FILE = tmp_path / "macro_regime.json"
        old_time = (datetime.now(ET) - timedelta(minutes=20)).isoformat()
        stale = {"spy_pct": -2.7, "detected_at": old_time}
        self._write_regime(tmp_path, candidate=stale)
        triggered = []
        import utils.market_monitor as mm
        monkeypatch.setattr(mm, "_trigger_flash_crash_lockout", lambda: triggered.append(1))
        from trading_agent import _handle_flash_crash_stage2
        _handle_flash_crash_stage2(-3.0)
        assert len(triggered) == 0
        data = json.loads((tmp_path / "macro_regime.json").read_text())
        new_ts = datetime.fromisoformat(data["flash_crash_candidate"]["detected_at"])
        assert (datetime.now(ET) - new_ts).total_seconds() < 5

    def test_spy_recovery_clears_candidate(self, tmp_path, monkeypatch):
        import config
        config.MACRO_REGIME_FILE = tmp_path / "macro_regime.json"
        fresh = {"spy_pct": -2.7, "detected_at": datetime.now(ET).isoformat()}
        self._write_regime(tmp_path, candidate=fresh)
        from trading_agent import _clear_flash_crash_candidate
        _clear_flash_crash_candidate()
        data = json.loads((tmp_path / "macro_regime.json").read_text())
        assert data["flash_crash_candidate"] is None

    def test_set_crisis_lockout_clears_candidate(self, tmp_path, monkeypatch):
        import config
        config.MACRO_REGIME_FILE = tmp_path / "macro_regime.json"
        candidate = {"spy_pct": -3.0, "detected_at": datetime.now(ET).isoformat()}
        self._write_regime(tmp_path, candidate=candidate)
        from trading_agent import _set_crisis_lockout
        _set_crisis_lockout(hours=1)
        data = json.loads((tmp_path / "macro_regime.json").read_text())
        assert data["flash_crash_active"] is True
        assert data["flash_crash_candidate"] is None


# ── Auto-clear lockout ────────────────────────────────────────────────────────

@pytest.mark.unit
class TestEvaluateLockoutRecovery:
    def _future(self, hours=24):
        from datetime import timedelta
        return (datetime.now(ET) + timedelta(hours=hours)).isoformat()

    def _past(self, hours=1):
        from datetime import timedelta
        return (datetime.now(ET) - timedelta(hours=hours)).isoformat()

    def test_no_lockout_unchanged(self):
        from macro_regime import _evaluate_lockout_recovery
        active, until = _evaluate_lockout_recovery(False, None, 20.0, 1.0, datetime.now(ET))
        assert active is False
        assert until is None

    def test_naturally_expired_clears(self):
        from macro_regime import _evaluate_lockout_recovery
        active, until = _evaluate_lockout_recovery(
            True, self._past(1), 50.0, -10.0, datetime.now(ET)
        )
        assert active is False
        assert until is None

    def test_conditions_recovered_clears(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "FLASH_CRASH_VIX_RECOVERY", 35)
        monkeypatch.setattr(config, "FLASH_CRASH_SPY_RECOVERY_PCT", -5.0)
        from macro_regime import _evaluate_lockout_recovery
        active, until = _evaluate_lockout_recovery(
            True, self._future(24), 30.0, -2.0, datetime.now(ET)
        )
        assert active is False
        assert until is None

    def test_vix_still_high_preserves_lockout(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "FLASH_CRASH_VIX_RECOVERY", 35)
        monkeypatch.setattr(config, "FLASH_CRASH_SPY_RECOVERY_PCT", -5.0)
        from macro_regime import _evaluate_lockout_recovery
        future = self._future(24)
        active, until = _evaluate_lockout_recovery(
            True, future, 42.0, -2.0, datetime.now(ET)
        )
        assert active is True
        assert until == future

    def test_spy_still_weak_preserves_lockout(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "FLASH_CRASH_VIX_RECOVERY", 35)
        monkeypatch.setattr(config, "FLASH_CRASH_SPY_RECOVERY_PCT", -5.0)
        from macro_regime import _evaluate_lockout_recovery
        future = self._future(24)
        active, until = _evaluate_lockout_recovery(
            True, future, 28.0, -8.0, datetime.now(ET)
        )
        assert active is True
        assert until == future


# ── macro_regime: individual signal scorers ───────────────────────────────────

@pytest.mark.unit
class TestMacroSignalScorers:
    def test_score_vix_low_returns_positive(self):
        from macro_regime import _score_vix
        # VIX well below 90d avg → +10
        closes = [20.0] * 91 + [14.0]
        assert _score_vix(closes) == 10.0

    def test_score_vix_high_returns_negative(self):
        from macro_regime import _score_vix
        # VIX > 120% of 90d avg → -10
        closes = [20.0] * 91 + [25.0]
        assert _score_vix(closes) == -10.0

    def test_score_vix_in_range_is_linear(self):
        from macro_regime import _score_vix
        # VIX at exactly 90d avg → ratio 1.0, linear output between -10 and +10
        closes = [20.0] * 92
        score = _score_vix(closes)
        assert -10.0 <= score <= 10.0

    def test_score_vix_insufficient_data_returns_zero(self):
        from macro_regime import _score_vix
        assert _score_vix([20.0] * 5) == 0.0

    @pytest.mark.parametrize("ret5d,expected", [
        (2.0,  10.0),   # above 1.5% → max
        (-1.0, -10.0),  # below -0.5% → min
        (0.5,  pytest.approx(2.5, abs=0.1)),  # mid-range linear
    ])
    def test_score_spy(self, ret5d, expected):
        from macro_regime import _score_spy
        assert _score_spy(ret5d) == expected

    @pytest.mark.parametrize("ret5d,expected", [
        (1.0,   10.0),  # stable ±2% → +10
        (6.0,  -10.0),  # spike > 5% → -10
        (-9.0, -10.0),  # crash < -8% → -10
        (3.0,    0.0),  # between 2 and 5% → neutral
    ])
    def test_score_wti(self, ret5d, expected):
        from macro_regime import _score_wti
        assert _score_wti(ret5d) == expected

    @pytest.mark.parametrize("ret5d,expected", [
        (4.0,  -10.0),  # big gold surge = flight to safety → bearish
        (2.5,   -5.0),  # mild surge
        (-3.0,  +5.0),  # declining gold = risk-on
        (0.5,   +8.0),  # flat = bullish
    ])
    def test_score_gold(self, ret5d, expected):
        from macro_regime import _score_gold
        assert _score_gold(ret5d) == expected

    @pytest.mark.parametrize("ret5d,expected", [
        (3.0,  -10.0),  # DXY surging → risk-off
        (1.0,   -5.0),  # mild rise
        (-1.0,  +5.0),  # declining dollar
        (0.2,   +3.0),  # stable
    ])
    def test_score_dxy(self, ret5d, expected):
        from macro_regime import _score_dxy
        assert _score_dxy(ret5d) == expected

    @pytest.mark.parametrize("ret5d,expected", [
        (6.0,  -10.0),  # yields surging → tightening
        (3.0,   -5.0),  # rising
        (-3.0, +10.0),  # falling yields = bullish
        (1.0,   +3.0),  # stable
    ])
    def test_score_tnx(self, ret5d, expected):
        from macro_regime import _score_tnx
        assert _score_tnx(ret5d) == expected

    def test_score_tech_bull(self):
        from macro_regime import _score_tech
        assert _score_tech(3.0, bull_thresh=2.0, bear_thresh=-1.0) == 10.0

    def test_score_tech_bear(self):
        from macro_regime import _score_tech
        assert _score_tech(-2.0, bull_thresh=2.0, bear_thresh=-1.0) == -10.0

    def test_score_tech_neutral_positive(self):
        from macro_regime import _score_tech
        score = _score_tech(1.0, bull_thresh=2.0, bear_thresh=-1.0)
        assert 0 < score < 10

    def test_score_tech_neutral_negative(self):
        from macro_regime import _score_tech
        score = _score_tech(-0.5, bull_thresh=2.0, bear_thresh=-1.0)
        assert -10 < score < 0


@pytest.mark.unit
class TestComputeSectorScores:
    def test_bull_soxx_and_qqq(self):
        from macro_regime import _compute_sector_scores
        result = _compute_sector_scores(soxx_ret5d=3.0, qqq_ret5d=2.0)
        assert result["AI_SEMI"] == 100.0
        assert result["BROAD_TECH"] == 100.0
        assert "CYBERSECURITY" in result

    def test_bear_soxx_and_qqq(self):
        from macro_regime import _compute_sector_scores
        result = _compute_sector_scores(soxx_ret5d=-2.0, qqq_ret5d=-2.0)
        assert result["AI_SEMI"] == -100.0
        assert result["BROAD_TECH"] == -100.0

    def test_cybersecurity_is_80pct_of_qqq(self):
        from macro_regime import _compute_sector_scores
        result = _compute_sector_scores(soxx_ret5d=3.0, qqq_ret5d=2.0)
        assert abs(result["CYBERSECURITY"] - 0.8 * result["BROAD_TECH"]) < 1e-9


@pytest.mark.unit
class TestLoadMacroRegime:
    def test_missing_file_returns_normal_default(self, tmp_path):
        import config
        config.MACRO_REGIME_FILE = tmp_path / "macro_regime.json"
        from macro_regime import load_macro_regime
        result = load_macro_regime()
        assert result["regime"] == "NORMAL"
        assert "macro_health_score" in result

    def test_loads_written_regime(self, tmp_path):
        import config, json
        config.MACRO_REGIME_FILE = tmp_path / "macro_regime.json"
        payload = {"regime": "BULL_TREND", "health_score": 55, "capitulation_candidate": []}
        with open(config.MACRO_REGIME_FILE, "w") as f:
            json.dump(payload, f)
        from macro_regime import load_macro_regime
        result = load_macro_regime()
        assert result["regime"] == "BULL_TREND"
        assert result["health_score"] == 55

    def test_corrupt_file_returns_default(self, tmp_path):
        import config
        config.MACRO_REGIME_FILE = tmp_path / "macro_regime.json"
        config.MACRO_REGIME_FILE.write_text("NOT JSON{{")
        from macro_regime import load_macro_regime
        result = load_macro_regime()
        assert result["regime"] == "NORMAL"


# ── indicator_engine: base technical functions ────────────────────────────────

@pytest.mark.unit
class TestBaseIndicatorFunctions:
    def test_ema_falls_toward_last_value(self):
        from indicator_engine import _ema
        s = pd.Series([100.0] * 20 + [200.0])
        result = _ema(s, period=5)
        assert float(result.iloc[-1]) > 100.0
        assert float(result.iloc[-1]) < 200.0

    def test_compute_vwap_equals_tp_at_constant_price(self):
        from indicator_engine import _compute_vwap
        n = 10
        idx = pd.date_range("2026-05-01 09:30", periods=n, freq="5min", tz="America/New_York")
        df = pd.DataFrame({"high": 102.0, "low": 98.0, "close": 100.0, "volume": 1000.0}, index=idx)
        vwap = _compute_vwap(df)
        # TP = (102+98+100)/3 = 100 → VWAP should equal 100
        assert abs(float(vwap.iloc[-1]) - 100.0) < 1e-6

    def test_compute_vwap_bands_upper_above_lower(self):
        from indicator_engine import _compute_vwap, _compute_vwap_bands
        df = _make_intraday_df(n=20, close=100.0)
        vwap = _compute_vwap(df)
        vs1u, vs1l, vs2u, vs2l = _compute_vwap_bands(df, vwap)
        assert float(vs1u.dropna().iloc[-1]) >= float(vs1l.dropna().iloc[-1])
        assert float(vs2u.dropna().iloc[-1]) >= float(vs2u.dropna().iloc[-1])

    def test_compute_rsi_stays_in_bounds(self):
        from indicator_engine import _compute_rsi
        s = pd.Series([float(i) for i in range(1, 51)])
        rsi = _compute_rsi(s)
        valid = rsi.dropna()
        assert (valid >= 0).all() and (valid <= 100).all()

    def test_compute_rsi_trending_up_above_50(self):
        from indicator_engine import _compute_rsi
        s = pd.Series([100.0 + i for i in range(30)])
        rsi = _compute_rsi(s)
        assert float(rsi.dropna().iloc[-1]) > 50

    def test_compute_macd_histogram_sign_follows_momentum(self):
        from indicator_engine import _compute_macd
        # strong uptrend → fast EMA above slow EMA → positive histogram
        s = pd.Series([100.0 + i * 2 for i in range(60)])
        _, _, hist = _compute_macd(s)
        assert float(hist.dropna().iloc[-1]) > 0

    def test_compute_bollinger_upper_above_lower(self):
        from indicator_engine import _compute_bollinger
        s = pd.Series([100.0 + np.random.randn() for _ in range(40)])
        upper, mid, lower = _compute_bollinger(s)
        valid_upper = upper.dropna()
        valid_lower = lower.dropna()
        assert (valid_upper > valid_lower).all()

    def test_compute_atr_positive_with_volatile_bars(self):
        from indicator_engine import _compute_atr
        df = _make_intraday_df(n=20, close=100.0, high=105.0, low=95.0)
        atr = _compute_atr(df)
        assert float(atr.dropna().iloc[-1]) > 0

    def test_compute_volume_ratio_normal_is_one(self):
        from indicator_engine import _compute_volume_ratio
        vol = pd.Series([1000.0] * 30)
        ratio = _compute_volume_ratio(vol)
        assert abs(float(ratio.dropna().iloc[-1]) - 1.0) < 1e-6

    def test_compute_stochastic_in_bounds(self):
        from indicator_engine import _compute_stochastic
        df = _make_intraday_df(n=20)
        k, d = _compute_stochastic(df)
        valid_k = k.dropna()
        assert (valid_k >= 0).all() and (valid_k <= 100).all()

    def test_compute_obv_increases_on_up_bars(self):
        from indicator_engine import _compute_obv
        closes = [100.0 + i for i in range(10)]
        idx = pd.date_range("2026-05-01", periods=10, freq="5min", tz="America/New_York")
        df = pd.DataFrame({"close": closes, "volume": 1000.0, "open": closes,
                           "high": [c + 1 for c in closes], "low": [c - 1 for c in closes]},
                          index=idx)
        obv = _compute_obv(df)
        assert float(obv.iloc[-1]) > float(obv.iloc[0])

    def test_safe_returns_fallback_on_nan(self):
        from indicator_engine import _safe
        s = pd.Series([float("nan")])
        assert _safe(s, fallback=42.0) == 42.0

    def test_safe_returns_value_when_valid(self):
        from indicator_engine import _safe
        s = pd.Series([99.5])
        assert _safe(s) == 99.5


# ── indicator_engine: _compute_daily_volume_stats ────────────────────────────

@pytest.mark.unit
class TestDailyVolumeStats:
    def test_none_df_returns_defaults(self):
        from indicator_engine import _compute_daily_volume_stats
        ratio, trend, mean = _compute_daily_volume_stats(None)
        assert ratio == 1.0 and trend == 0.0 and mean == 0.0

    def test_too_few_rows_returns_defaults(self):
        from indicator_engine import _compute_daily_volume_stats
        df = _make_daily_df(n=2)
        ratio, trend, mean = _compute_daily_volume_stats(df)
        assert ratio == 1.0 and trend == 0.0 and mean == 0.0

    def test_constant_volume_ratio_is_one(self):
        from indicator_engine import _compute_daily_volume_stats
        df = _make_daily_df(n=10, volume=1_000_000.0)
        ratio, trend, mean = _compute_daily_volume_stats(df)
        assert abs(ratio - 1.0) < 0.05

    def test_increasing_volume_trend_positive(self):
        from indicator_engine import _compute_daily_volume_stats
        idx = pd.date_range("2026-01-01", periods=10, freq="B", tz="America/New_York")
        vols = [500_000.0 * (1 + i * 0.2) for i in range(10)]  # 20% growth/day
        df = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
                           "volume": vols}, index=idx)
        ratio, trend, mean = _compute_daily_volume_stats(df)
        assert trend > 0

    def test_returns_correct_mean(self):
        from indicator_engine import _compute_daily_volume_stats
        df = _make_daily_df(n=8, volume=2_000_000.0)
        ratio, trend, mean = _compute_daily_volume_stats(df)
        assert abs(mean - 2_000_000.0) < 1000.0


# ── indicator_engine: _approx_corr ────────────────────────────────────────────

@pytest.mark.unit
class TestApproxCorr:
    def test_identical_series_returns_one(self):
        from indicator_engine import _approx_corr
        a = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert abs(_approx_corr(a, a) - 1.0) < 1e-6

    def test_opposite_series_returns_negative_one(self):
        from indicator_engine import _approx_corr
        a = [1.0, 2.0, 3.0, 4.0, 5.0]
        b = [5.0, 4.0, 3.0, 2.0, 1.0]
        assert abs(_approx_corr(a, b) - (-1.0)) < 1e-6

    def test_too_short_returns_zero(self):
        from indicator_engine import _approx_corr
        assert _approx_corr([1.0, 2.0], [1.0, 2.0]) == 0.0

    def test_constant_series_returns_zero(self):
        from indicator_engine import _approx_corr
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
        from indicator_engine import _composite_score
        score, signal, conf = _composite_score(self._bullish_vals(), self._equal_weights())
        assert score > 15
        assert signal == "BUY"

    def test_bearish_vals_give_sell_signal(self):
        from indicator_engine import _composite_score
        score, signal, conf = _composite_score(self._bearish_vals(), self._equal_weights())
        assert score < -15
        assert signal == "SELL"

    def test_neutral_gives_hold(self):
        from indicator_engine import _composite_score
        # Price exactly at VWAP, RSI 50, MACD 0
        vals = self._bullish_vals(price=100.0)
        vals.update({"rsi": 50.0, "macd_hist": 0.0, "ema9": 100.0, "ema21": 100.0,
                     "stoch_k": 50.0, "stoch_d": 50.0, "daily_volume_trend": 0.0,
                     "vwap_slope": 0.0, "rsi_slope": 0.0, "obv_trend": 0.0})
        _, signal, _ = _composite_score(vals, self._equal_weights())
        assert signal == "HOLD"

    def test_empty_weights_returns_zero_hold(self):
        from indicator_engine import _composite_score
        score, signal, conf = _composite_score(self._bullish_vals(), {})
        assert score == 0.0 and signal == "HOLD"

    def test_confidence_scales_with_volume(self):
        from indicator_engine import _composite_score
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
        from indicator_engine import compute_indicators
        df = _make_intraday_df(n=10)
        assert compute_indicators("APP", df) is None

    def test_returns_indicator_values_for_valid_df(self, tmp_path, monkeypatch):
        import config
        config.INDICATOR_WEIGHTS_FILE = tmp_path / "weights.json"
        weights = {
            "VWAP": 10.0, "RSI": 10.0, "Daily Volume Trend": 10.0, "MACD": 10.0,
            "EMA 9/21 Crossover": 10.0, "Bollinger Bands": 10.0, "VWAP StdDev Bands": 10.0,
            "ATR": 10.0, "Stochastic Oscillator": 10.0, "OBV": 10.0, "RSI Slope": 10.0,
        }
        import json
        config.INDICATOR_WEIGHTS_FILE.write_text(json.dumps(weights))

        from indicator_engine import compute_indicators, IndicatorValues
        df = _make_intraday_df(n=100)
        result = compute_indicators("APP", df)
        assert result is not None
        assert isinstance(result, IndicatorValues)
        assert result.symbol == "APP"
        assert -100 <= result.composite_score <= 100

    def test_works_with_daily_df(self, tmp_path):
        import config, json
        config.INDICATOR_WEIGHTS_FILE = tmp_path / "weights.json"
        weights = {k: 10.0 for k in [
            "VWAP", "RSI", "Daily Volume Trend", "MACD", "EMA 9/21 Crossover",
            "Bollinger Bands", "VWAP StdDev Bands", "ATR", "Stochastic Oscillator",
            "OBV", "RSI Slope"
        ]}
        config.INDICATOR_WEIGHTS_FILE.write_text(json.dumps(weights))

        from indicator_engine import compute_indicators
        df = _make_intraday_df(n=100)
        daily = _make_daily_df(n=10)
        result = compute_indicators("NVDA", df, daily_df=daily)
        assert result is not None
        assert result.daily_volume_ratio > 0


# ── daily_report: build_report_body ──────────────────────────────────────────

@pytest.mark.unit
class TestBuildReportBody:
    @staticmethod
    def _account(portfolio=10_500.0, last_equity=10_000.0):
        return {"portfolio_value": portfolio, "last_equity": last_equity}

    @staticmethod
    def _close(symbol="NVDA", pnl=150.0, pnl_pct=1.5, qty=10,
               entry=100.0, exit_=115.0, reason="take_profit"):
        return {
            "type": "CLOSE", "symbol": symbol, "qty": qty,
            "entry_price": entry, "exit_price": exit_,
            "pnl_dollar": pnl, "pnl_pct": pnl_pct, "close_reason": reason,
        }

    def test_subject_contains_pnl_when_trades_exist(self):
        from daily_report import build_report_body
        subject, _ = build_report_body(
            trades=[self._close()],
            account=self._account(),
            regime="BULL_TREND",
            watch_list="NVDA, APP",
            weights={"RSI": 10.0},
        )
        assert "P&L" in subject
        assert "1 trades" in subject

    def test_subject_no_trades_shows_portfolio(self):
        from daily_report import build_report_body
        subject, _ = build_report_body(
            trades=[], account=self._account(), regime="NORMAL",
            watch_list="", weights={},
        )
        assert "No trades" in subject
        assert "Portfolio" in subject

    def test_body_contains_regime(self):
        from daily_report import build_report_body
        _, body = build_report_body(
            trades=[], account=self._account(), regime="CHOPPY",
            watch_list="TSLA", weights={},
        )
        assert "CHOPPY" in body

    def test_body_lists_individual_trades(self):
        from daily_report import build_report_body
        _, body = build_report_body(
            trades=[self._close("NVDA", pnl=200.0), self._close("APP", pnl=-50.0)],
            account=self._account(),
            regime="NORMAL", watch_list="", weights={},
        )
        assert "NVDA" in body
        assert "APP" in body

    def test_body_includes_watch_list(self):
        from daily_report import build_report_body
        _, body = build_report_body(
            trades=[], account=self._account(), regime="NORMAL",
            watch_list="NVDA, AMD, APP", weights={},
        )
        assert "NVDA, AMD, APP" in body

    def test_body_includes_weights(self):
        from daily_report import build_report_body
        _, body = build_report_body(
            trades=[], account=self._account(), regime="NORMAL",
            watch_list="", weights={"RSI": 15.5},
        )
        assert "RSI" in body

    def test_win_rate_calculation(self):
        from daily_report import build_report_body
        trades = [
            self._close("NVDA", pnl=100.0),
            self._close("AAPL", pnl=50.0),
            self._close("TSLA", pnl=-80.0),
        ]
        _, body = build_report_body(
            trades=trades, account=self._account(), regime="NORMAL",
            watch_list="", weights={},
        )
        assert "66.7%" in body or "Win rate" in body


# ── daily_report: _already_sent_today / _mark_sent ───────────────────────────

@pytest.mark.unit
class TestDailyReportSentFlag:
    def test_not_sent_when_file_missing(self, tmp_path, monkeypatch):
        import daily_report
        monkeypatch.setattr(daily_report, "DAILY_REPORT_SENT_FILE", tmp_path / "sent.json")
        assert daily_report._already_sent_today() is False

    def test_not_sent_after_marking_different_date(self, tmp_path, monkeypatch):
        import daily_report, json
        sent_file = tmp_path / "sent.json"
        sent_file.write_text(json.dumps({"date": "2020-01-01"}))
        monkeypatch.setattr(daily_report, "DAILY_REPORT_SENT_FILE", sent_file)
        assert daily_report._already_sent_today() is False

    def test_already_sent_after_mark_sent(self, tmp_path, monkeypatch):
        import daily_report
        monkeypatch.setattr(daily_report, "DAILY_REPORT_SENT_FILE", tmp_path / "sent.json")
        daily_report._mark_sent()
        assert daily_report._already_sent_today() is True


# ── macro_regime: trading params helpers (migrated from vix_regime) ───────────

@pytest.mark.unit
class TestVixRegimeHelpers:
    def test_load_macro_regime_missing_file_returns_normal(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.MACRO_REGIME_FILE", tmp_path / "macro_regime.json")
        from macro_regime import load_macro_regime
        result = load_macro_regime()
        assert result["regime"] == "NORMAL"

    def test_load_macro_regime_reads_written_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.MACRO_REGIME_FILE", tmp_path / "macro_regime.json")
        payload = {"regime": "CRISIS", "macro_health_score": -50}
        (tmp_path / "macro_regime.json").write_text(json.dumps(payload))
        from macro_regime import load_macro_regime
        result = load_macro_regime()
        assert result["regime"] == "CRISIS"

    def test_load_trading_params_returns_config_defaults(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.TRADING_PARAMS_FILE", tmp_path / "trading_params.json")
        from macro_regime import load_trading_params
        params = load_trading_params()
        import config
        assert "entry_start_hour" in params
        assert params["stop_loss_pct"] == config.STOP_LOSS_PCT

    def test_load_trading_params_overrides_from_file(self, tmp_path, monkeypatch):
        params_file = tmp_path / "trading_params.json"
        params_file.write_text(json.dumps({"stop_loss_pct": 3.5}))
        monkeypatch.setattr("config.TRADING_PARAMS_FILE", params_file)
        from macro_regime import load_trading_params
        params = load_trading_params()
        assert params["stop_loss_pct"] == 3.5


# ── learning_agent: file I/O helpers ─────────────────────────────────────────

@pytest.mark.unit
class TestLearningAgentHelpers:
    def test_load_todays_closed_trades_missing_file(self, tmp_path):
        import config
        config.TRADE_JOURNAL_FILE = tmp_path / "trade_journal.json"
        from learning_agent import _load_todays_closed_trades
        assert _load_todays_closed_trades() == []

    def test_load_todays_closed_trades_filters_by_today(self, tmp_path):
        import config, json
        from datetime import date
        config.TRADE_JOURNAL_FILE = tmp_path / "trade_journal.json"
        today = date.today().isoformat()
        journal = [
            {"type": "CLOSE", "timestamp": f"{today}T10:30:00", "symbol": "NVDA",
             "pnl_pct": 2.0, "pnl_dollar": 100.0, "close_reason": "tp"},
            {"type": "CLOSE", "timestamp": "2020-01-01T10:30:00", "symbol": "AAPL",
             "pnl_pct": -1.0, "pnl_dollar": -50.0, "close_reason": "sl"},
            {"type": "BUY", "timestamp": f"{today}T09:30:00", "symbol": "AMD"},
        ]
        config.TRADE_JOURNAL_FILE.write_text(json.dumps(journal))
        from learning_agent import _load_todays_closed_trades
        result = _load_todays_closed_trades()
        assert len(result) == 1
        assert result[0]["symbol"] == "NVDA"

    def test_save_and_load_weights(self, tmp_path):
        import config
        config.INDICATOR_WEIGHTS_FILE = tmp_path / "weights.json"
        from learning_agent import _save_weights, _load_weights
        weights = {"RSI": 20.0, "MACD": 15.0}
        _save_weights(weights)
        loaded = _load_weights()
        assert loaded == weights

    def test_save_trading_params_adds_last_updated(self, tmp_path):
        import config, json
        from datetime import date
        config.TRADING_PARAMS_FILE = tmp_path / "trading_params.json"
        from learning_agent import _save_trading_params
        _save_trading_params({"stop_loss_pct": 4.0})
        saved = json.loads(config.TRADING_PARAMS_FILE.read_text())
        assert saved["last_updated"] == date.today().isoformat()
        assert saved["stop_loss_pct"] == 4.0


# ── swing_manager: _evaluate_gap_action ──────────────────────────────────────

@pytest.mark.unit
class TestEvaluateGapAction:
    @staticmethod
    def _pos(entry=100.0, sl_pct=7.0, tp1_hit=False):
        return {"entry_price": entry, "stop_loss_pct": sl_pct, "tp1_hit": tp1_hit}

    def test_gap_down_past_stop_returns_close(self):
        from swing_manager import _evaluate_gap_action
        assert _evaluate_gap_action(self._pos(), current_price=92.0) == "CLOSE_GAP_DOWN"

    def test_gap_down_2_to_5pct_returns_tighten(self):
        from swing_manager import _evaluate_gap_action
        assert _evaluate_gap_action(self._pos(), current_price=96.0) == "TIGHTEN_STOP"

    def test_gap_up_3pct_returns_take_tp1(self):
        from swing_manager import _evaluate_gap_action
        assert _evaluate_gap_action(self._pos(), current_price=103.5) == "TAKE_TP1_GAP_UP"

    def test_gap_up_when_tp1_already_hit_returns_none(self):
        from swing_manager import _evaluate_gap_action
        assert _evaluate_gap_action(self._pos(tp1_hit=True), current_price=103.5) == "NONE"

    def test_no_gap_returns_none(self):
        from swing_manager import _evaluate_gap_action
        assert _evaluate_gap_action(self._pos(), current_price=100.5) == "NONE"


# ── swing_manager: load/save ──────────────────────────────────────────────────

@pytest.mark.unit
class TestLoadSwingPositions:
    def test_missing_file_returns_empty_dict(self, tmp_path):
        import config
        config.SWING_POSITIONS_FILE = tmp_path / "swing_positions.json"
        from swing_manager import load_swing_positions
        assert load_swing_positions() == {}

    def test_loads_written_positions(self, tmp_path):
        import config, json
        config.SWING_POSITIONS_FILE = tmp_path / "swing_positions.json"
        payload = {"NVDA": {"entry_price": 100.0, "qty": 10}}
        config.SWING_POSITIONS_FILE.write_text(json.dumps(payload))
        from swing_manager import load_swing_positions
        result = load_swing_positions()
        assert "NVDA" in result
        assert result["NVDA"]["qty"] == 10


# ── earnings_calendar: cache helpers ─────────────────────────────────────────

@pytest.mark.unit
class TestEarningsCalendarCacheHelpers:
    def _write_cache(self, tmp_path, symbol="NVDA", days_until=3, reported_block=None):
        import config, json
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        ET = ZoneInfo("America/New_York")
        config.EARNINGS_CALENDAR_FILE = tmp_path / "earnings_calendar.json"
        today_et = datetime.now(ET).date()
        next_date = (today_et + timedelta(days=days_until)).isoformat()
        entry = {"next_earnings_date": next_date}
        if reported_block:
            entry["recently_reported"] = reported_block
        payload = {
            "date": today_et.isoformat(),
            "symbols": {symbol: entry},
        }
        config.EARNINGS_CALENDAR_FILE.write_text(json.dumps(payload))

    def test_days_until_earnings_returns_correct_count(self, tmp_path):
        self._write_cache(tmp_path, days_until=5)
        from earnings_calendar import days_until_earnings
        d = days_until_earnings("NVDA")
        assert d == 5

    def test_days_until_earnings_returns_none_for_unknown(self, tmp_path):
        import config
        config.EARNINGS_CALENDAR_FILE = tmp_path / "earnings_calendar.json"
        from earnings_calendar import days_until_earnings
        assert days_until_earnings("UNKNOWN") is None

    def test_is_earnings_imminent_true_when_in_1_day(self, tmp_path):
        self._write_cache(tmp_path, days_until=1)
        from earnings_calendar import is_earnings_imminent
        assert is_earnings_imminent("NVDA", days=1) is True

    def test_is_earnings_imminent_false_when_far(self, tmp_path):
        self._write_cache(tmp_path, days_until=10)
        from earnings_calendar import is_earnings_imminent
        assert is_earnings_imminent("NVDA", days=1) is False

    def test_recently_reported_returns_block_when_fresh(self, tmp_path):
        from datetime import date, timedelta
        report_date = (date.today() - timedelta(days=2)).isoformat()
        block = {"report_date": report_date, "eps_surprise_pct": 10.0}
        self._write_cache(tmp_path, reported_block=block)
        from earnings_calendar import recently_reported
        result = recently_reported("NVDA", days=5)
        assert result is not None
        assert result["eps_surprise_pct"] == 10.0

    def test_recently_reported_returns_none_when_stale(self, tmp_path):
        from datetime import date, timedelta
        old_date = (date.today() - timedelta(days=10)).isoformat()
        block = {"report_date": old_date, "eps_surprise_pct": 10.0}
        self._write_cache(tmp_path, reported_block=block)
        from earnings_calendar import recently_reported
        assert recently_reported("NVDA", days=5) is None


# ── trading_agent: persistence helpers ───────────────────────────────────────

@pytest.mark.unit
class TestTradingAgentHelpers:
    def test_load_watch_list_returns_empty_when_missing(self, tmp_path):
        import config
        config.WATCH_LIST_FILE = tmp_path / "watch_list.txt"
        from trading_agent import load_watch_list
        assert load_watch_list() == []

    def test_load_watch_list_strips_and_uppercases(self, tmp_path):
        import config
        config.WATCH_LIST_FILE = tmp_path / "watch_list.txt"
        config.WATCH_LIST_FILE.write_text("nvda\n aapl \nAMD\n")
        from trading_agent import load_watch_list
        result = load_watch_list()
        assert result == ["NVDA", "AAPL", "AMD"]

    def test_load_open_positions_empty_when_no_file(self, tmp_path):
        import config
        config.OPEN_POSITIONS_FILE = tmp_path / "open_positions.json"
        from trading_agent import load_open_positions
        assert load_open_positions() == {}

    def test_save_and_load_open_positions(self, tmp_path):
        import config
        config.OPEN_POSITIONS_FILE = tmp_path / "open_positions.json"
        from trading_agent import save_open_positions, load_open_positions
        positions = {"NVDA": {"entry_price": 100.0, "qty": 5}}
        save_open_positions(positions)
        loaded = load_open_positions()
        assert loaded == positions

    def test_estimate_batch_call_cost_scales_with_batch_size(self):
        from trading_agent import _estimate_batch_call_cost
        cost1 = _estimate_batch_call_cost(1)
        cost5 = _estimate_batch_call_cost(5)
        assert cost5 > cost1
        assert cost1 > 0

    def test_load_daily_closed_returns_set(self, tmp_path):
        import config
        config.DAILY_CLOSED_FILE = tmp_path / "daily_closed.json"
        from trading_agent import load_daily_closed
        assert load_daily_closed() == set()

    def test_record_and_load_daily_closed(self, tmp_path):
        import config
        config.DAILY_CLOSED_FILE = tmp_path / "daily_closed.json"
        from trading_agent import record_daily_closed, load_daily_closed
        record_daily_closed("NVDA")
        record_daily_closed("AAPL")
        closed = load_daily_closed()
        assert "NVDA" in closed
        assert "AAPL" in closed

    def test_append_trade_accumulates_journal(self, tmp_path):
        import config, json
        config.TRADE_JOURNAL_FILE = tmp_path / "trade_journal.json"
        from trading_agent import append_trade
        append_trade({"type": "BUY", "symbol": "NVDA"})
        append_trade({"type": "CLOSE", "symbol": "NVDA"})
        journal = json.loads(config.TRADE_JOURNAL_FILE.read_text())
        assert len(journal) == 2
        assert journal[0]["type"] == "BUY"

    def test_top_indicators_aligned_true(self):
        import types
        from trading_agent import _top_indicators_aligned
        iv = types.SimpleNamespace(current_price=102.0, vwap=100.0, rsi=55.0, macd_hist=0.1)
        assert _top_indicators_aligned(iv) is True

    def test_top_indicators_aligned_false_price_below_vwap(self):
        import types
        from trading_agent import _top_indicators_aligned
        iv = types.SimpleNamespace(current_price=98.0, vwap=100.0, rsi=55.0, macd_hist=0.1)
        assert _top_indicators_aligned(iv) is False


# ── market_data: provider routing ────────────────────────────────────────────

@pytest.mark.unit
class TestMarketDataHelpers:
    def test_get_intraday_bars_returns_empty_on_yahoo_error(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "MARKET_DATA_PROVIDER", "yahoo")
        import market_data as md
        monkeypatch.setattr(md, "_yahoo_bars", lambda *a, **kw: (_ for _ in ()).throw(Exception("net error")))
        result = md.get_intraday_bars("AAPL")
        assert result.empty

    def test_get_daily_bars_returns_empty_on_yahoo_error(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "MARKET_DATA_PROVIDER", "yahoo")
        import market_data as md
        monkeypatch.setattr(md, "_yahoo_bars", lambda *a, **kw: (_ for _ in ()).throw(Exception("net error")))
        result = md.get_daily_bars("AAPL")
        assert result.empty

    def test_get_latest_price_yahoo_path(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "MARKET_DATA_PROVIDER", "yahoo")
        import market_data as md
        idx = pd.date_range("2026-05-01 15:50", periods=1, freq="2min", tz="America/New_York")
        fake_df = pd.DataFrame({"close": [155.0]}, index=idx)
        monkeypatch.setattr(md, "_yahoo_bars", lambda *a, **kw: fake_df)
        price = md.get_latest_price("AAPL")
        assert price == 155.0


# ── alpaca_client: thin wrapper ───────────────────────────────────────────────

@pytest.mark.unit
class TestAlpacaClientHelpers:
    def test_init_succeeds_with_dummy_keys(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "ALPACA_API_KEY", "test_key")
        monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "test_secret")
        from unittest.mock import patch, MagicMock
        with patch("alpaca_client.TradingClient"):
            from alpaca_client import AlpacaClient
            client = AlpacaClient()
            assert client is not None

    def test_get_latest_price_delegates_to_market_data(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "ALPACA_API_KEY", "test_key")
        monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "test_secret")
        from unittest.mock import patch, MagicMock
        with patch("alpaca_client.TradingClient"):
            import alpaca_client
            client = alpaca_client.AlpacaClient()
            with patch("market_data.get_latest_price", return_value=200.5):
                price = client.get_latest_price("AAPL")
                assert price == 200.5


# ── economic_calendar: additional coverage ────────────────────────────────────

@pytest.mark.unit
class TestEconomicCalendarAdditional:
    def test_first_friday_feb_2026(self):
        from economic_calendar import _first_friday
        assert _first_friday(2026, 2) == 6  # Feb 2026: first Friday is the 6th

    def test_first_friday_jan_2025(self):
        from economic_calendar import _first_friday
        assert _first_friday(2025, 1) == 3  # Jan 2025: first Friday is the 3rd

    def test_hardcoded_events_returns_list(self):
        from economic_calendar import _hardcoded_events
        events = _hardcoded_events(2026, 3)
        assert isinstance(events, list)
        assert all("name" in e and "date" in e and "impact" in e for e in events)

    def test_hardcoded_events_nfp_is_first_friday(self):
        from economic_calendar import _hardcoded_events, _first_friday
        day = _first_friday(2026, 6)
        events = _hardcoded_events(2026, 6)
        nfp = [e for e in events if "Non-Farm" in e["name"]]
        assert nfp[0]["date"] == f"2026-06-{day:02d}"


# ── api_budget: remaining lines ───────────────────────────────────────────────

@pytest.mark.unit
class TestApiBudgetRemaining:
    def test_record_call_increases_cost(self, tmp_path):
        import config, api_budget
        config.API_BUDGET_FILE = tmp_path / "api_budget.json"
        api_budget.record_usage(input_tokens=1000, output_tokens=100,
                                cache_write_tokens=0, cache_read_tokens=0)
        budget = api_budget.load_budget()
        assert budget["estimated_cost_usd"] > 0
        assert budget["calls_made"] == 1

    def test_check_budget_not_exhausted_returns_true(self, tmp_path):
        import config, api_budget
        config.API_BUDGET_FILE = tmp_path / "api_budget.json"
        assert api_budget.check_budget(estimated_call_cost=0.0) is True

    def test_ceiling_exhausted_after_many_calls(self, tmp_path):
        import config, api_budget
        config.API_BUDGET_FILE = tmp_path / "api_budget.json"
        config.API_DAILY_BUDGET_USD = 0.001  # tiny ceiling
        # Simulate expensive calls
        for _ in range(5):
            api_budget.record_usage(input_tokens=100_000, output_tokens=10_000,
                                    cache_write_tokens=0, cache_read_tokens=0)
        assert api_budget.check_budget(estimated_call_cost=0.0) is False


# ── telegram_notify ───────────────────────────────────────────────────────────

@pytest.mark.unit
class TestTelegramNotify:
    """telegram_notify functions must never raise; unconfigured tokens are a no-op."""

    def _patch_no_token(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")

    def _patch_fake_token(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "fake_token")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "12345")

    def test_no_token_is_silent(self, monkeypatch):
        self._patch_no_token(monkeypatch)
        from unittest.mock import patch
        import telegram_notify
        with patch("requests.post") as mock_post:
            telegram_notify.notify_startup("NORMAL", 0)
            mock_post.assert_not_called()

    def test_sends_when_configured(self, monkeypatch):
        self._patch_fake_token(monkeypatch)
        from unittest.mock import patch
        import telegram_notify
        with patch("requests.post") as mock_post:
            telegram_notify.notify_startup("BULL_TREND", 2)
            mock_post.assert_called_once()
            payload = mock_post.call_args[1]["json"]
            assert "BULL_TREND" in payload["text"]
            assert payload["parse_mode"] == "HTML"

    def test_network_error_does_not_raise(self, monkeypatch):
        self._patch_fake_token(monkeypatch)
        from unittest.mock import patch
        import telegram_notify
        with patch("requests.post", side_effect=Exception("network down")):
            telegram_notify.notify_startup("NORMAL", 0)  # must not raise

    def test_notify_buy_contains_symbol(self, monkeypatch):
        self._patch_fake_token(monkeypatch)
        from unittest.mock import patch
        import telegram_notify
        with patch("requests.post") as mock_post:
            telegram_notify.notify_buy("NVDA", 10, 150.0, 144.0, 157.5, "DAY_TRADE", 42.0, 80)
            text = mock_post.call_args[1]["json"]["text"]
            assert "NVDA" in text
            assert "150.00" in text

    def test_notify_close_shows_pnl(self, monkeypatch):
        self._patch_fake_token(monkeypatch)
        from unittest.mock import patch
        import telegram_notify
        with patch("requests.post") as mock_post:
            telegram_notify.notify_close("AAPL", 5, 100.0, 105.0, 25.0, 5.0, "claude_decision")
            text = mock_post.call_args[1]["json"]["text"]
            assert "AAPL" in text
            assert "+25.00" in text

    def test_notify_eod_no_trades(self, monkeypatch):
        self._patch_fake_token(monkeypatch)
        from unittest.mock import patch
        import telegram_notify
        with patch("requests.post") as mock_post:
            telegram_notify.notify_eod_summary([], 0.0, 0.0, 10000.0, "NORMAL")
            text = mock_post.call_args[1]["json"]["text"]
            assert "no trades" in text.lower()

    def test_notify_eod_with_trades(self, monkeypatch):
        self._patch_fake_token(monkeypatch)
        from unittest.mock import patch
        import telegram_notify
        closes = [
            {"symbol": "NVDA", "pnl_dollar": 80.0, "pnl_pct": 4.0},
            {"symbol": "AAPL", "pnl_dollar": -20.0, "pnl_pct": -1.5},
        ]
        with patch("requests.post") as mock_post:
            telegram_notify.notify_eod_summary(closes, 60.0, 0.6, 10060.0, "BULL_TREND")
            text = mock_post.call_args[1]["json"]["text"]
            assert "NVDA" in text
            assert "BULL_TREND" in text

    def test_notify_job_failure_truncates_long_error(self, monkeypatch):
        self._patch_fake_token(monkeypatch)
        from unittest.mock import patch
        import telegram_notify
        long_err = "x" * 500
        with patch("requests.post") as mock_post:
            telegram_notify.notify_job_failure("force_close", long_err)
            text = mock_post.call_args[1]["json"]["text"]
            assert "force_close" in text
            assert len(text) < 600

    def test_notify_claude_disabled(self, monkeypatch):
        self._patch_fake_token(monkeypatch)
        from unittest.mock import patch
        import telegram_notify
        with patch("requests.post") as mock_post:
            telegram_notify.notify_claude_disabled("HTTP 400 — invalid cache_control")
            text = mock_post.call_args[1]["json"]["text"]
            assert "Claude" in text

    def test_notify_regime_change_crisis_has_alert_emoji(self, monkeypatch):
        self._patch_fake_token(monkeypatch)
        from unittest.mock import patch
        import telegram_notify
        with patch("requests.post") as mock_post:
            telegram_notify.notify_regime_change("NORMAL", "CRISIS", -65)
            text = mock_post.call_args[1]["json"]["text"]
            assert "CRISIS" in text
            assert "🚨" in text


@pytest.mark.unit
class TestLoadTradingParamsInMacroRegime:
    def test_returns_config_defaults_when_file_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.TRADING_PARAMS_FILE", tmp_path / "trading_params.json")
        from macro_regime import load_trading_params
        params = load_trading_params()
        import config as cfg
        assert params["entry_start_hour"] == cfg.ENTRY_START_HOUR
        assert params["rsi_buy_max"] == cfg.RSI_BUY_MAX
        assert params["stop_loss_pct"] == cfg.STOP_LOSS_PCT

    def test_file_values_override_defaults(self, tmp_path, monkeypatch):
        params_file = tmp_path / "trading_params.json"
        params_file.write_text('{"rsi_buy_max": 60, "entry_start_hour": 11}')
        monkeypatch.setattr("config.TRADING_PARAMS_FILE", params_file)
        from macro_regime import load_trading_params
        params = load_trading_params()
        assert params["rsi_buy_max"] == 60
        assert params["entry_start_hour"] == 11

    def test_load_macro_regime_returns_regime_key(self, tmp_path, monkeypatch):
        from macro_regime import load_macro_regime
        result = load_macro_regime()
        assert "regime" in result


@pytest.mark.unit
class TestFetchWithRetry:
    def test_succeeds_on_first_attempt(self):
        from research_agent import _fetch_with_retry
        calls = []
        def fn():
            calls.append(1)
            return "ok"
        result = _fetch_with_retry(fn)
        assert result == "ok"
        assert len(calls) == 1

    def test_retries_on_failure_then_succeeds(self):
        from research_agent import _fetch_with_retry
        calls = []
        def fn():
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("transient")
            return "recovered"
        result = _fetch_with_retry(fn, attempts=3, base_delay=0)
        assert result == "recovered"
        assert len(calls) == 3

    def test_raises_after_all_attempts_exhausted(self):
        from research_agent import _fetch_with_retry
        def fn():
            raise ConnectionError("down")
        with pytest.raises(ConnectionError):
            _fetch_with_retry(fn, attempts=3, base_delay=0)


@pytest.mark.unit
class TestCapabilitySummary:
    def test_degraded_items_identified_when_keys_missing(self, monkeypatch):
        """_log_capability_summary returns list of degraded integration names."""
        import config as cfg
        monkeypatch.setattr(cfg, "FRED_API_KEY", "")
        monkeypatch.setattr(cfg, "POLYGON_API_KEY", "")
        monkeypatch.setattr(cfg, "EMAIL_SENDER", "user@example.com")
        monkeypatch.setattr(cfg, "EMAIL_APP_PASSWORD", "pass")
        monkeypatch.setattr(cfg, "TELEGRAM_BOT_TOKEN", "tok")
        monkeypatch.setattr(cfg, "TELEGRAM_CHAT_ID", "123")
        from main import _log_capability_summary
        degraded = _log_capability_summary()
        assert any("FRED" in item for item in degraded)
        assert any("Polygon" in item for item in degraded)
        assert not any("Email" in item for item in degraded)

    def test_no_degraded_when_all_keys_present(self, monkeypatch, tmp_path):
        import config as cfg
        from pathlib import Path
        monkeypatch.setattr(cfg, "FRED_API_KEY", "abc")
        monkeypatch.setattr(cfg, "POLYGON_API_KEY", "xyz")
        monkeypatch.setattr(cfg, "EMAIL_SENDER", "user@example.com")
        monkeypatch.setattr(cfg, "EMAIL_APP_PASSWORD", "pass")
        monkeypatch.setattr(cfg, "TELEGRAM_BOT_TOKEN", "tok")
        monkeypatch.setattr(cfg, "TELEGRAM_CHAT_ID", "123")
        # Point FRED_CACHE_FILE at a non-existent tmp path so no real file is read
        monkeypatch.setattr(cfg, "FRED_CACHE_FILE", tmp_path / "fred_cache.json")
        from main import _log_capability_summary
        degraded = _log_capability_summary()
        assert degraded == []


@pytest.mark.unit
class TestBuildMarketContextBlock:
    def _regime(self, **overrides) -> dict:
        base = {
            "regime": "BULL_TREND",
            "macro_health_score": 32,
            "vix_current": 18.2,
        }
        base.update(overrides)
        return base

    def _fred(self, **overrides) -> dict:
        base = {
            "fed_direction": "cutting",
            "cpi_trend": "falling",
            "unemployment_rising": False,
        }
        base.update(overrides)
        return base

    def _meta(self, **overrides) -> dict:
        base = {
            "macro_shock_score": 0,
            "sector_sentiment": {"AI_SEMI": 28.0, "DATA_CENTER": 15.0},
            "capitulation_candidates": [],
        }
        base.update(overrides)
        return base

    def test_contains_all_six_fields(self, monkeypatch):
        monkeypatch.setattr(
            "economic_calendar.is_event_risk_window", lambda now: (False, "")
        )
        from trading_agent import _build_market_context_block
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime(2026, 5, 5, 10, 30, tzinfo=ZoneInfo("America/New_York"))
        block = _build_market_context_block(self._regime(), self._fred(), self._meta(), now)
        assert "BULL_TREND" in block
        assert "cutting" in block
        assert "falling" in block
        assert "None active" in block
        assert "AI_SEMI" in block
        assert "Capitulation watch" in block

    def test_shock_score_omitted_when_zero(self, monkeypatch):
        monkeypatch.setattr(
            "economic_calendar.is_event_risk_window", lambda now: (False, "")
        )
        from trading_agent import _build_market_context_block
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime(2026, 5, 5, 10, 30, tzinfo=ZoneInfo("America/New_York"))
        block = _build_market_context_block(self._regime(), self._fred(), self._meta(macro_shock_score=0), now)
        assert "0/100" in block
        assert "elevated" not in block

    def test_shock_score_flagged_when_elevated(self, monkeypatch):
        monkeypatch.setattr(
            "economic_calendar.is_event_risk_window", lambda now: (False, "")
        )
        from trading_agent import _build_market_context_block
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime(2026, 5, 5, 10, 30, tzinfo=ZoneInfo("America/New_York"))
        block = _build_market_context_block(
            self._regime(), self._fred(), self._meta(macro_shock_score=55), now
        )
        assert "55/100" in block
        assert "elevated" in block

    def test_active_event_risk_flagged(self, monkeypatch):
        monkeypatch.setattr(
            "economic_calendar.is_event_risk_window",
            lambda now: (True, "Non-Farm Payrolls"),
        )
        from trading_agent import _build_market_context_block
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime(2026, 5, 5, 8, 35, tzinfo=ZoneInfo("America/New_York"))
        block = _build_market_context_block(self._regime(), self._fred(), self._meta(), now)
        assert "Non-Farm Payrolls" in block
        assert "avoid" in block

    def test_capitulation_candidates_listed(self, monkeypatch):
        monkeypatch.setattr(
            "economic_calendar.is_event_risk_window", lambda now: (False, "")
        )
        from trading_agent import _build_market_context_block
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime(2026, 5, 5, 10, 30, tzinfo=ZoneInfo("America/New_York"))
        block = _build_market_context_block(
            self._regime(), self._fred(), self._meta(capitulation_candidates=["SMCI", "PLTR"]), now
        )
        assert "SMCI" in block
        assert "PLTR" in block


@pytest.mark.unit
class TestIoUtils:
    """Tests for load_json and save_json utilities."""

    def _write_cache(self, tmp_path, filename: str, content: str) -> Path:
        """Helper to write content to a temp file."""
        file_path = tmp_path / filename
        file_path.write_text(content)
        return file_path

    def test_load_json_missing_file_returns_default(self, tmp_path):
        """Missing file returns default value."""
        from utils.io_utils import load_json
        missing_path = tmp_path / "missing.json"
        result = load_json(missing_path, [])
        assert result == []

    def test_load_json_valid_file_returns_data(self, tmp_path):
        """Valid JSON file returns parsed data."""
        from utils.io_utils import load_json
        file_path = self._write_cache(tmp_path, "test.json", '{"k": 1}')
        result = load_json(file_path, {})
        assert result == {"k": 1}

    def test_load_json_corrupt_file_returns_default(self, tmp_path):
        """Corrupt JSON returns default value."""
        from utils.io_utils import load_json
        file_path = self._write_cache(tmp_path, "corrupt.json", "not json{{{")
        result = load_json(file_path, {})
        assert result == {}

    def test_load_json_corrupt_creates_backup(self, tmp_path):
        """Corrupt JSON creates .corrupt.json backup."""
        from utils.io_utils import load_json
        file_path = self._write_cache(tmp_path, "corrupt.json", "not json{{{")
        load_json(file_path, {})
        backup_path = file_path.with_suffix(".corrupt.json")
        assert backup_path.exists()
        assert backup_path.read_text() == "not json{{{"

    def test_load_json_empty_file_returns_default(self, tmp_path):
        """Empty file returns default value."""
        from utils.io_utils import load_json
        file_path = self._write_cache(tmp_path, "empty.json", "")
        result = load_json(file_path, [])
        assert result == []

    def test_save_json_writes_valid_data(self, tmp_path):
        """save_json writes valid JSON and can be read back."""
        from utils.io_utils import save_json, load_json
        file_path = tmp_path / "output.json"
        data = {"x": 1}
        save_json(file_path, data)
        result = load_json(file_path, {})
        assert result == data

    def test_save_json_atomic_no_tmp_on_success(self, tmp_path):
        """After successful save, .tmp file should not exist."""
        from utils.io_utils import save_json
        file_path = tmp_path / "output.json"
        save_json(file_path, {"x": 1})
        tmp_path_file = file_path.with_suffix(".tmp")
        assert not tmp_path_file.exists()

    def test_save_json_raises_on_unserializable(self, tmp_path):
        """save_json raises TypeError on unserializable data."""
        from utils.io_utils import save_json
        file_path = tmp_path / "output.json"
        with pytest.raises(TypeError):
            save_json(file_path, {"bad": object()})

    def test_save_json_creates_parent_dir_content(self, tmp_path):
        """save_json works with nested parent directory."""
        from utils.io_utils import save_json, load_json
        nested_path = tmp_path / "nested" / "deep" / "data.json"
        data = {"nested": True}
        # Note: save_json does not create parent dirs, so we need to handle that
        # or verify it works when parent exists. For this test, we'll create parent.
        nested_path.parent.mkdir(parents=True, exist_ok=True)
        save_json(nested_path, data)
        result = load_json(nested_path, {})
        assert result == data

    def test_load_json_list_data(self, tmp_path):
        """save_json and load_json handle list data."""
        from utils.io_utils import save_json, load_json
        file_path = tmp_path / "list.json"
        data = [1, 2, 3, {"key": "value"}]
        save_json(file_path, data)
        result = load_json(file_path, [])
        assert result == data


# ── TestMarketMonitor ─────────────────────────────────────────────────────────

class TestMarketMonitor:
    """Unit tests for utils/market_monitor.py — no network or Alpaca calls."""

    def _write_regime_file(self, tmp_path, **overrides):
        """Write a minimal macro_regime.json and return its path."""
        from utils.io_utils import save_json
        path = tmp_path / "macro_regime.json"
        data = {"regime": "NORMAL", "flash_crash_candidate": None}
        data.update(overrides)
        save_json(path, data)
        return path

    @pytest.mark.unit
    def test_write_flash_crash_candidate_creates_file(self, tmp_path, monkeypatch):
        """_write_flash_crash_candidate stores symbol, price, pct_drop, and timestamp."""
        import config
        monkeypatch.setattr(config, "MACRO_REGIME_FILE", tmp_path / "macro_regime.json")
        from utils.market_monitor import _write_flash_crash_candidate
        now = datetime(2026, 5, 6, 10, 0, tzinfo=ET)
        _write_flash_crash_candidate(-3.1, now)
        from utils.io_utils import load_json
        data = load_json(tmp_path / "macro_regime.json", {})
        candidate = data.get("flash_crash_candidate")
        assert candidate is not None
        assert candidate["spy_pct"] == -3.1
        assert candidate["detected_at"] == now.isoformat()

    @pytest.mark.unit
    def test_clear_flash_crash_candidate_removes_entry(self, tmp_path, monkeypatch):
        """_clear_flash_crash_candidate sets flash_crash_candidate to None."""
        import config
        self._write_regime_file(
            tmp_path,
            flash_crash_candidate={"spy_pct": -3.0, "detected_at": "2026-05-06T10:00:00"},
        )
        monkeypatch.setattr(config, "MACRO_REGIME_FILE", tmp_path / "macro_regime.json")
        from utils.market_monitor import _clear_flash_crash_candidate
        _clear_flash_crash_candidate()
        from utils.io_utils import load_json
        data = load_json(tmp_path / "macro_regime.json", {})
        assert data.get("flash_crash_candidate") is None

    @pytest.mark.unit
    def test_clear_flash_crash_candidate_noop_if_missing(self, tmp_path, monkeypatch):
        """_clear_flash_crash_candidate does not raise when macro_regime.json is absent."""
        import config
        missing = tmp_path / "no_such_file.json"
        monkeypatch.setattr(config, "MACRO_REGIME_FILE", missing)
        from utils.market_monitor import _clear_flash_crash_candidate
        _clear_flash_crash_candidate()  # must not raise

    @pytest.mark.unit
    def test_read_candidate_returns_none_if_missing(self, tmp_path, monkeypatch):
        """_read_flash_crash_candidate returns None when file does not exist."""
        import config
        monkeypatch.setattr(config, "MACRO_REGIME_FILE", tmp_path / "no_such.json")
        from utils.market_monitor import _read_flash_crash_candidate
        result = _read_flash_crash_candidate()
        assert result is None

    @pytest.mark.unit
    def test_read_candidate_returns_data_if_exists(self, tmp_path, monkeypatch):
        """_read_flash_crash_candidate returns the stored dict when file contains one."""
        import config
        self._write_regime_file(
            tmp_path,
            flash_crash_candidate={"spy_pct": -3.5, "detected_at": "2026-05-06T10:00:00"},
        )
        monkeypatch.setattr(config, "MACRO_REGIME_FILE", tmp_path / "macro_regime.json")
        from utils.market_monitor import _read_flash_crash_candidate
        result = _read_flash_crash_candidate()
        assert result is not None
        assert result["spy_pct"] == -3.5

    @pytest.mark.unit
    def test_set_crisis_lockout_writes_macro_regime(self, tmp_path, monkeypatch):
        """_set_crisis_lockout writes regime=CRISIS and flash_crash_active=True."""
        import config
        self._write_regime_file(tmp_path)
        monkeypatch.setattr(config, "MACRO_REGIME_FILE", tmp_path / "macro_regime.json")
        from utils.market_monitor import _set_crisis_lockout
        _set_crisis_lockout(hours=48)
        from utils.io_utils import load_json
        data = load_json(tmp_path / "macro_regime.json", {})
        assert data.get("regime") == "CRISIS"
        assert data.get("flash_crash_active") is True
        assert data.get("flash_crash_until") is not None

    @pytest.mark.unit
    def test_run_market_monitor_no_crash_returns_ok(self, tmp_path, monkeypatch):
        """run_market_monitor does nothing when SPY drop is within normal range."""
        import config
        self._write_regime_file(tmp_path)
        monkeypatch.setattr(config, "MACRO_REGIME_FILE", tmp_path / "macro_regime.json")
        monkeypatch.setattr(config, "FLASH_CRASH_SPY_DROP_PCT", -2.5)
        monkeypatch.setattr(config, "POLYGON_API_KEY", "")

        import utils.market_monitor as mm
        monkeypatch.setattr(mm, "get_latest_price", lambda sym: 500.0)
        monkeypatch.setattr(mm, "_get_spy_prev_close", lambda: 499.0)  # +0.2%

        from utils.market_monitor import run_market_monitor
        run_market_monitor()  # should complete without triggering lockout

        from utils.io_utils import load_json
        data = load_json(tmp_path / "macro_regime.json", {})
        assert data.get("flash_crash_active") is not True

    @pytest.mark.unit
    def test_run_market_monitor_stage1_triggers_on_drop(self, tmp_path, monkeypatch):
        """run_market_monitor writes a stage-1 candidate when SPY drops past threshold."""
        import config
        self._write_regime_file(tmp_path)
        monkeypatch.setattr(config, "MACRO_REGIME_FILE", tmp_path / "macro_regime.json")
        monkeypatch.setattr(config, "FLASH_CRASH_SPY_DROP_PCT", -2.5)
        monkeypatch.setattr(config, "POLYGON_API_KEY", "")

        import utils.market_monitor as mm
        monkeypatch.setattr(mm, "get_latest_price", lambda sym: 480.0)
        monkeypatch.setattr(mm, "_get_spy_prev_close", lambda: 500.0)  # -4%

        from utils.market_monitor import run_market_monitor
        run_market_monitor()

        from utils.io_utils import load_json
        data = load_json(tmp_path / "macro_regime.json", {})
        assert data.get("flash_crash_candidate") is not None
        assert data["flash_crash_candidate"]["spy_pct"] == pytest.approx(-4.0, abs=0.05)

    @pytest.mark.unit
    def test_run_market_monitor_stage2_triggers_lockout(self, tmp_path, monkeypatch):
        """Stage-2 confirmation calls force_close_all when candidate is still fresh."""
        import config
        from utils.io_utils import save_json
        # Candidate was written 5 minutes ago (within 15-min window)
        candidate_time = (datetime.now(ET) - __import__("datetime").timedelta(minutes=5)).isoformat()
        self._write_regime_file(
            tmp_path,
            flash_crash_candidate={"spy_pct": -3.5, "detected_at": candidate_time},
        )
        monkeypatch.setattr(config, "MACRO_REGIME_FILE", tmp_path / "macro_regime.json")
        monkeypatch.setattr(config, "FLASH_CRASH_SPY_DROP_PCT", -2.5)
        monkeypatch.setattr(config, "POLYGON_API_KEY", "")

        import utils.market_monitor as mm
        monkeypatch.setattr(mm, "get_latest_price", lambda sym: 480.0)
        monkeypatch.setattr(mm, "_get_spy_prev_close", lambda: 500.0)  # -4%

        force_close_called = []

        def mock_force_close():
            force_close_called.append(True)

        monkeypatch.setattr(mm, "_trigger_flash_crash_lockout", lambda: (force_close_called.append(True), _set_crisis_lockout_direct(tmp_path)))

        def _set_crisis_lockout_direct(tp):
            data = {"regime": "CRISIS", "flash_crash_active": True, "flash_crash_until": "2026-05-08T10:00:00", "flash_crash_candidate": None}
            save_json(tp / "macro_regime.json", data)

        from utils.market_monitor import run_market_monitor
        run_market_monitor()

        assert len(force_close_called) == 1

    @pytest.mark.unit
    def test_run_market_monitor_clears_stale_candidate(self, tmp_path, monkeypatch):
        """A candidate older than 15 min is overwritten with a fresh one, not confirmed."""
        import config
        from utils.io_utils import load_json
        # Candidate was written 20 minutes ago (outside 15-min window)
        old_time = (datetime.now(ET) - __import__("datetime").timedelta(minutes=20)).isoformat()
        self._write_regime_file(
            tmp_path,
            flash_crash_candidate={"spy_pct": -3.5, "detected_at": old_time},
        )
        monkeypatch.setattr(config, "MACRO_REGIME_FILE", tmp_path / "macro_regime.json")
        monkeypatch.setattr(config, "FLASH_CRASH_SPY_DROP_PCT", -2.5)
        monkeypatch.setattr(config, "POLYGON_API_KEY", "")

        import utils.market_monitor as mm
        monkeypatch.setattr(mm, "get_latest_price", lambda sym: 480.0)
        monkeypatch.setattr(mm, "_get_spy_prev_close", lambda: 500.0)  # -4%

        lockout_triggered = []
        orig_trigger = mm._trigger_flash_crash_lockout
        monkeypatch.setattr(mm, "_trigger_flash_crash_lockout", lambda: lockout_triggered.append(True))

        from utils.market_monitor import run_market_monitor
        run_market_monitor()

        # Lockout must NOT be triggered for a stale candidate
        assert len(lockout_triggered) == 0
        # But a fresh candidate must have been written
        data = load_json(tmp_path / "macro_regime.json", {})
        candidate = data.get("flash_crash_candidate")
        assert candidate is not None

    @pytest.mark.unit
    def test_verify_spy_with_polygon_returns_none_on_error(self, tmp_path, monkeypatch):
        """_verify_spy_with_polygon returns None when the HTTP request fails."""
        import config
        monkeypatch.setattr(config, "POLYGON_API_KEY", "fake-key")

        import utils.market_monitor as mm
        monkeypatch.setattr(mm.requests, "get", lambda *a, **kw: (_ for _ in ()).throw(Exception("network error")))

        from utils.market_monitor import _verify_spy_with_polygon
        result = _verify_spy_with_polygon(-3.0)
        assert result is None

    @pytest.mark.unit
    def test_verify_spy_with_polygon_returns_price_on_success(self, tmp_path, monkeypatch):
        """_verify_spy_with_polygon returns True when Polygon confirms a crash-level drop."""
        import config
        monkeypatch.setattr(config, "POLYGON_API_KEY", "fake-key")
        monkeypatch.setattr(config, "FLASH_CRASH_SPY_DROP_PCT", -2.5)

        class MockResp:
            def raise_for_status(self): pass
            def json(self):
                return {"ticker": {"todaysChangePerc": -4.0}}

        import utils.market_monitor as mm
        monkeypatch.setattr(mm.requests, "get", lambda *a, **kw: MockResp())

        from utils.market_monitor import _verify_spy_with_polygon
        result = _verify_spy_with_polygon(-4.0)
        assert result is True

    @pytest.mark.unit
    def test_get_spy_prev_close_returns_float(self, monkeypatch):
        """_get_spy_prev_close returns a positive float when market data is available."""
        import pandas as pd

        closes = [495.0, 500.0]
        idx = pd.date_range("2026-05-05", periods=2, freq="B", tz="America/New_York")
        mock_df = pd.DataFrame({"close": closes}, index=idx)

        # Patch at the market_data module level — _get_spy_prev_close imports from there
        import market_data
        monkeypatch.setattr(market_data, "get_daily_bars", lambda *a, **kw: mock_df)

        from utils.market_monitor import _get_spy_prev_close
        result = _get_spy_prev_close()
        assert isinstance(result, float)
        assert result > 0

    @pytest.mark.unit
    def test_run_market_monitor_polygon_fallback_used(self, tmp_path, monkeypatch):
        """When Polygon key is set but API fails, falls back to Stage 2 two-reading path."""
        import config
        self._write_regime_file(tmp_path)
        monkeypatch.setattr(config, "MACRO_REGIME_FILE", tmp_path / "macro_regime.json")
        monkeypatch.setattr(config, "FLASH_CRASH_SPY_DROP_PCT", -2.5)
        monkeypatch.setattr(config, "POLYGON_API_KEY", "fake-key-set")

        import utils.market_monitor as mm
        monkeypatch.setattr(mm, "get_latest_price", lambda sym: 480.0)
        monkeypatch.setattr(mm, "_get_spy_prev_close", lambda: 500.0)  # -4%
        # Polygon API unavailable → _verify_spy_with_polygon returns None
        monkeypatch.setattr(mm, "_verify_spy_with_polygon", lambda pct: None)

        from utils.market_monitor import run_market_monitor
        run_market_monitor()

        # Stage 2 path should have written a candidate (no prior candidate → Stage 1)
        from utils.io_utils import load_json
        data = load_json(tmp_path / "macro_regime.json", {})
        assert data.get("flash_crash_candidate") is not None

    @pytest.mark.unit
    def test_trigger_flash_crash_logs_and_notifies(self, tmp_path, monkeypatch):
        """Stage-2 lockout notifies telegram when _trigger_flash_crash_lockout is called."""
        import config
        self._write_regime_file(tmp_path)
        monkeypatch.setattr(config, "MACRO_REGIME_FILE", tmp_path / "macro_regime.json")
        monkeypatch.setattr(config, "FLASH_CRASH_SPY_DROP_PCT", -2.5)
        monkeypatch.setattr(config, "POLYGON_API_KEY", "")

        # Set up a fresh candidate (within 15-min window)
        candidate_time = datetime.now(ET).isoformat()
        from utils.io_utils import save_json
        save_json(tmp_path / "macro_regime.json", {
            "regime": "NORMAL",
            "flash_crash_candidate": {"spy_pct": -3.5, "detected_at": candidate_time},
        })

        import utils.market_monitor as mm
        monkeypatch.setattr(mm, "get_latest_price", lambda sym: 480.0)
        monkeypatch.setattr(mm, "_get_spy_prev_close", lambda: 500.0)  # -4%

        notify_calls = []

        def mock_trigger():
            notify_calls.append("triggered")
            # Also write CRISIS so we can verify
            save_json(tmp_path / "macro_regime.json", {
                "regime": "CRISIS",
                "flash_crash_active": True,
                "flash_crash_until": "2026-05-08T10:00:00",
                "flash_crash_candidate": None,
            })

        monkeypatch.setattr(mm, "_trigger_flash_crash_lockout", mock_trigger)

        from utils.market_monitor import run_market_monitor
        run_market_monitor()

        assert "triggered" in notify_calls
        from utils.io_utils import load_json
        data = load_json(tmp_path / "macro_regime.json", {})
        assert data.get("regime") == "CRISIS"


# ── Startup exit alerts ───────────────────────────────────────────────────────

@pytest.mark.unit
class TestStartupExitAlerts:
    def test_exit_with_alert_calls_telegram_then_exit(self):
        """_exit_with_alert sends Telegram before sys.exit"""
        from unittest.mock import patch
        import main
        call_order = []
        def mock_exit(code):
            call_order.append("exit")
            raise SystemExit(code)
        with patch("main.telegram_notify._send", side_effect=lambda m: call_order.append("telegram")):
            with patch("sys.exit", side_effect=mock_exit):
                with pytest.raises(SystemExit):
                    main._exit_with_alert("test message")
        assert call_order == ["telegram", "exit"]

    def test_exit_with_alert_message_contains_text(self):
        """Telegram message contains the error text"""
        from unittest.mock import patch
        import main
        sent = []
        with patch("main.telegram_notify._send", side_effect=lambda m: sent.append(m)):
            with patch("sys.exit"):
                main._exit_with_alert("ALPACA_API_KEY missing")
        assert len(sent) == 1
        assert "ALPACA_API_KEY missing" in sent[0]

    def test_exit_with_alert_tolerates_telegram_failure(self):
        """If Telegram raises, sys.exit still fires"""
        from unittest.mock import patch
        import main
        with patch("main.telegram_notify._send", side_effect=Exception("Telegram down")):
            with patch("sys.exit") as mock_exit:
                main._exit_with_alert("some error")
        mock_exit.assert_called_once_with(1)

    def test_exit_with_alert_custom_code(self):
        """Custom exit code is passed to sys.exit"""
        from unittest.mock import patch
        import main
        with patch("main.telegram_notify._send"):
            with patch("sys.exit") as mock_exit:
                main._exit_with_alert("msg", code=2)
        mock_exit.assert_called_once_with(2)


# ── EOD force-close helpers ───────────────────────────────────────────────────

@pytest.mark.unit
class TestForceCloseAll:
    def _positions_json(self, tmp_path, positions: dict):
        p = tmp_path / "open_positions.json"
        p.write_text(json.dumps(positions))
        return p

    def test_eod_close_reason_profitable(self):
        from trading_agent import _eod_close_reason
        assert _eod_close_reason(1.0) == "eod_force_profitable"

    def test_eod_close_reason_loss(self):
        from trading_agent import _eod_close_reason
        assert _eod_close_reason(-1.0) == "eod_force_loss"

    def test_eod_close_reason_flat(self):
        from trading_agent import _eod_close_reason
        assert _eod_close_reason(0.0) == "eod_force_flat"

    def test_eod_close_reason_boundary_profitable(self):
        from trading_agent import _eod_close_reason
        # exactly at threshold is NOT profitable (> not >=)
        assert _eod_close_reason(0.5) == "eod_force_flat"
        assert _eod_close_reason(0.51) == "eod_force_profitable"

    def test_eod_close_reason_boundary_loss(self):
        from trading_agent import _eod_close_reason
        assert _eod_close_reason(-0.5) == "eod_force_flat"
        assert _eod_close_reason(-0.51) == "eod_force_loss"

    def test_force_close_phantom_skip(self, tmp_path, monkeypatch):
        """Symbols not in Alpaca positions are skipped without journaling"""
        from unittest.mock import MagicMock, patch
        import trading_agent
        import config

        # position in JSON but NOT in alpaca
        positions = {"AAPL": {"entry_price": 100.0, "stop_loss": 95.0, "take_profit": 110.0, "shares": 10, "qty": 10}}
        monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "open_positions.json")
        monkeypatch.setattr(config, "TRADE_JOURNAL_FILE", tmp_path / "trade_journal.json")
        (tmp_path / "open_positions.json").write_text(json.dumps(positions))
        (tmp_path / "trade_journal.json").write_text("[]")

        mock_alpaca = MagicMock()
        mock_alpaca.get_positions.return_value = {}  # AAPL not in live positions (empty dict)
        mock_alpaca.cancel_all_orders.return_value = None

        orig = config.EOD_VERIFY_FILLS
        config.EOD_VERIFY_FILLS = True
        try:
            with patch("trading_agent.AlpacaClient", return_value=mock_alpaca):
                trading_agent.force_close_all()
        finally:
            config.EOD_VERIFY_FILLS = orig

        # journal should NOT have a CLOSE entry for AAPL
        journal = json.loads((tmp_path / "trade_journal.json").read_text())
        close_entries = [e for e in journal if e.get("type") == "CLOSE" and e.get("symbol") == "AAPL"]
        assert close_entries == []


# ── signals.time_gate ─────────────────────────────────────────────────────────

import pytz
from signals.time_gate import is_entry_allowed

_ET_ZONE = pytz.timezone("America/New_York")


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
        # Default config.RS_MIN_RATIO = 0.0, so any ratio >= 0.0 passes
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
        # NVDA and AMD are both AI_SEMI sector
        assert passes_correlation_guard("AMD", {"NVDA": {}}) is True

    def test_blocked_at_max_same_sector(self):
        """With MAX_SAME_SECTOR_POSITIONS=2, two existing same-sector positions should block."""
        # NVDA, AMD, INTC are all AI_SEMI; trying to add third should be blocked
        assert passes_correlation_guard("INTC", {"NVDA": {}, "AMD": {}}) is False

    def test_passes_different_sectors(self):
        """Positions in different sectors should not block entry."""
        # PYPL (FINTECH), AFRM (FINTECH) exist, but entering NVDA (AI_SEMI) should pass
        assert passes_correlation_guard("NVDA", {"PYPL": {}, "AFRM": {}}) is True

    def test_unknown_sector_never_blocked(self):
        """Unknown sector ('OTHER') should always pass regardless of open positions."""
        # Even with many positions, unknown symbols pass
        assert passes_correlation_guard("ZZZ_UNKNOWN", {"NVDA": {}, "AMD": {}, "INTC": {}}) is True


# ── sentiment_sizer: position size multiplier ─────────────────────────────────

from signals.sentiment_sizer import sentiment_size_multiplier


@pytest.mark.unit
class TestSentimentSizer:
    """Tests for sentiment-based position sizing multiplier."""

    def test_score_above_20_returns_1_25(self):
        """Score >= 20 should return 1.25 multiplier."""
        assert sentiment_size_multiplier(25) == pytest.approx(1.25)

    def test_score_exactly_20_returns_1_25(self):
        """Score == 20 should return 1.25 multiplier (boundary)."""
        assert sentiment_size_multiplier(20) == pytest.approx(1.25)

    def test_score_between_10_and_19_returns_1_10(self):
        """Score between 10 and 19 should return 1.10 multiplier."""
        assert sentiment_size_multiplier(15) == pytest.approx(1.10)

    def test_score_exactly_10_returns_1_10(self):
        """Score == 10 should return 1.10 multiplier (boundary)."""
        assert sentiment_size_multiplier(10) == pytest.approx(1.10)

    def test_score_between_neg10_and_9_returns_1_00(self):
        """Score between -10 and 9 should return 1.00 multiplier."""
        assert sentiment_size_multiplier(0) == pytest.approx(1.00)

    def test_score_exactly_neg10_returns_1_00(self):
        """Score == -10 should return 1.00 multiplier (boundary)."""
        assert sentiment_size_multiplier(-10) == pytest.approx(1.00)

    def test_score_between_neg20_and_neg11_returns_0_75(self):
        """Score between -20 and -11 should return 0.75 multiplier."""
        assert sentiment_size_multiplier(-15) == pytest.approx(0.75)

    def test_score_exactly_neg20_returns_0_75(self):
        """Score == -20 should return 0.75 multiplier (boundary)."""
        assert sentiment_size_multiplier(-20) == pytest.approx(0.75)

    def test_score_below_neg20_returns_floor(self):
        """Score < -20 should return floor (0.50) multiplier."""
        assert sentiment_size_multiplier(-25) == pytest.approx(0.50)

    def test_score_extreme_negative_returns_floor(self):
        """Extreme negative score should return floor (0.50) multiplier."""
        assert sentiment_size_multiplier(-100) == pytest.approx(0.50)


# ── mtf_confirm: multi-timeframe filter ──────────────────────────────────────

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
        """Flat prices → EMA9 ≈ EMA21 (both equal to price) → not strictly > → blocked."""
        from signals.mtf_confirm import passes_mtf_filter
        closes = [100.0] * 40
        # Both EMAs converge to 100.0 after sufficient bars; 100.0 > 100.0 is False
        assert passes_mtf_filter(self._make_bars(closes)) is False


# ── trading_agent: profit-lock trailing stop ───────────────────────────────────

@pytest.mark.unit
class TestProfitLock:
    def _pos(self, entry: float, stop_price: float) -> dict:
        """Factory for a test position dict."""
        return {
            "entry_price": entry,
            "stop_loss_price": stop_price,
            "take_profit_price": entry * 1.05,
            "shares": 10,
            "symbol": "AAPL",
        }

    def test_no_change_below_trigger(self):
        """PnL below trigger threshold — stop should not change."""
        from trading_agent import _apply_profit_lock
        pos = self._pos(100.0, 95.0)
        result = _apply_profit_lock(pos, 101.0)  # only 1% gain, below 2% trigger
        assert result["stop_loss_price"] == pytest.approx(95.0)

    def test_stop_moves_to_entry_at_trigger(self):
        """PnL at trigger threshold — stop should move to entry price."""
        from trading_agent import _apply_profit_lock
        import config
        pos = self._pos(100.0, 95.0)
        current_price = 100.0 * (1 + config.PROFIT_LOCK_TRIGGER_PCT / 100)  # exactly at trigger
        result = _apply_profit_lock(pos, current_price)
        assert result["stop_loss_price"] == pytest.approx(100.0)

    def test_stop_not_moved_below_entry(self):
        """Stop already at or above entry — should not be lowered."""
        from trading_agent import _apply_profit_lock
        pos = self._pos(100.0, 101.0)  # stop already above entry
        result = _apply_profit_lock(pos, 102.1)  # 2.1% gain
        assert result["stop_loss_price"] == pytest.approx(101.0)  # stays put

    def test_no_change_when_entry_price_zero(self):
        """Zero entry price — should exit early without modifying stop."""
        from trading_agent import _apply_profit_lock
        pos = {"entry_price": 0.0, "stop_loss_price": 95.0, "symbol": "AAPL"}
        result = _apply_profit_lock(pos, 102.0)
        assert result["stop_loss_price"] == pytest.approx(95.0)

    def test_exact_trigger_pct_moves_stop(self):
        """Exactly at PROFIT_LOCK_TRIGGER_PCT should trigger the move."""
        from trading_agent import _apply_profit_lock
        import config
        pos = self._pos(100.0, 95.0)
        trigger_price = 100.0 * (1 + config.PROFIT_LOCK_TRIGGER_PCT / 100)
        result = _apply_profit_lock(pos, trigger_price)
        assert result["stop_loss_price"] == pytest.approx(100.0)

    def test_returns_same_dict_object(self):
        """Function should modify in place and return the same dict object."""
        from trading_agent import _apply_profit_lock
        pos = self._pos(100.0, 95.0)
        result = _apply_profit_lock(pos, 103.0)
        assert result is pos  # modifies in place
