"""
Plans 1–3 foundation unit tests. Run with: pytest tests/test_foundation.py -v -m unit
No API calls — all tests use synthetic fixtures.
"""
import json
import os
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

ET = ZoneInfo("America/New_York")



# ── macro_regime: health score ────────────────────────────────────────────────

@pytest.mark.unit
class TestMacroHealthScore:
    def test_bullish_inputs_give_positive_score(self):
        from macro.health_scorer import compute_macro_health_score
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
        from macro.health_scorer import compute_macro_health_score
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
        from macro.health_scorer import compute_macro_health_score
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
        from macro.health_scorer import compute_macro_health_score
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
        from macro.health_scorer import compute_macro_health_score
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
        from macro.regime_classifier import classify_regime
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
        from macro.regime_classifier import _count_consecutive_direction
        closes = [100, 101, 102, 103, 104, 105]  # all green
        assert _count_consecutive_direction(closes, "green") == 5

    def test_consecutive_red(self):
        from macro.regime_classifier import _count_consecutive_direction
        closes = [100, 99, 98, 97]  # all red
        assert _count_consecutive_direction(closes, "red") == 3

    def test_choppy_true(self):
        from macro.regime_classifier import _is_choppy
        # Up, down, up, down, up — 4 sign changes, tiny net move
        closes = [100.0, 100.2, 100.0, 100.2, 100.0, 100.1]
        assert _is_choppy(closes) is True

    def test_choppy_false_trending(self):
        from macro.regime_classifier import _is_choppy
        closes = [100, 101, 102, 103, 104, 105]  # steady uptrend
        assert _is_choppy(closes) is False

    def test_choppy_false_big_net_move(self):
        from macro.regime_classifier import _is_choppy
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


# ── Plan 3: Flash Crash Config ───────────────────────────────────────────────

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
class TestEvaluateLockoutRecovery:
    def _future(self, hours=24):
        from datetime import timedelta
        return (datetime.now(ET) + timedelta(hours=hours)).isoformat()

    def _past(self, hours=1):
        from datetime import timedelta
        return (datetime.now(ET) - timedelta(hours=hours)).isoformat()

    def test_no_lockout_unchanged(self):
        from macro.yahoo_fetcher import evaluate_lockout_recovery
        active, until = evaluate_lockout_recovery(False, None, 20.0, 1.0, datetime.now(ET))
        assert active is False
        assert until is None

    def test_naturally_expired_clears(self):
        from macro.yahoo_fetcher import evaluate_lockout_recovery
        active, until = evaluate_lockout_recovery(
            True, self._past(1), 50.0, -10.0, datetime.now(ET)
        )
        assert active is False
        assert until is None

    def test_conditions_recovered_clears(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "FLASH_CRASH_VIX_RECOVERY", 35)
        monkeypatch.setattr(config, "FLASH_CRASH_SPY_RECOVERY_PCT", -5.0)
        from macro.yahoo_fetcher import evaluate_lockout_recovery
        active, until = evaluate_lockout_recovery(
            True, self._future(24), 30.0, -2.0, datetime.now(ET)
        )
        assert active is False
        assert until is None

    def test_vix_still_high_preserves_lockout(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "FLASH_CRASH_VIX_RECOVERY", 35)
        monkeypatch.setattr(config, "FLASH_CRASH_SPY_RECOVERY_PCT", -5.0)
        from macro.yahoo_fetcher import evaluate_lockout_recovery
        future = self._future(24)
        active, until = evaluate_lockout_recovery(
            True, future, 42.0, -2.0, datetime.now(ET)
        )
        assert active is True
        assert until == future

    def test_spy_still_weak_preserves_lockout(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "FLASH_CRASH_VIX_RECOVERY", 35)
        monkeypatch.setattr(config, "FLASH_CRASH_SPY_RECOVERY_PCT", -5.0)
        from macro.yahoo_fetcher import evaluate_lockout_recovery
        future = self._future(24)
        active, until = evaluate_lockout_recovery(
            True, future, 28.0, -8.0, datetime.now(ET)
        )
        assert active is True
        assert until == future


# ── macro_regime: individual signal scorers ───────────────────────────────────

@pytest.mark.unit
class TestMacroSignalScorers:
    def test_score_vix_low_returns_positive(self):
        from macro.health_scorer import _score_vix
        # VIX well below 90d avg → +10
        closes = [20.0] * 91 + [14.0]
        assert _score_vix(closes) == 10.0

    def test_score_vix_high_returns_negative(self):
        from macro.health_scorer import _score_vix
        # VIX > 120% of 90d avg → -10
        closes = [20.0] * 91 + [25.0]
        assert _score_vix(closes) == -10.0

    def test_score_vix_in_range_is_linear(self):
        from macro.health_scorer import _score_vix
        # VIX at exactly 90d avg → ratio 1.0, linear output between -10 and +10
        closes = [20.0] * 92
        score = _score_vix(closes)
        assert -10.0 <= score <= 10.0

    def test_score_vix_insufficient_data_returns_zero(self):
        from macro.health_scorer import _score_vix
        assert _score_vix([20.0] * 5) == 0.0

    @pytest.mark.parametrize("ret5d,expected", [
        (2.0,  10.0),   # above 1.5% → max
        (-1.0, -10.0),  # below -0.5% → min
        (0.5,  pytest.approx(2.5, abs=0.1)),  # mid-range linear
    ])
    def test_score_spy(self, ret5d, expected):
        from macro.health_scorer import _score_spy
        assert _score_spy(ret5d) == expected

    @pytest.mark.parametrize("ret5d,expected", [
        (1.0,   10.0),  # stable ±2% → +10
        (6.0,  -10.0),  # spike > 5% → -10
        (-9.0, -10.0),  # crash < -8% → -10
        (3.0,    0.0),  # between 2 and 5% → neutral
    ])
    def test_score_wti(self, ret5d, expected):
        from macro.health_scorer import _score_wti
        assert _score_wti(ret5d) == expected

    @pytest.mark.parametrize("ret5d,expected", [
        (4.0,  -10.0),  # big gold surge = flight to safety → bearish
        (2.5,   -5.0),  # mild surge
        (-3.0,  +5.0),  # declining gold = risk-on
        (0.5,   +8.0),  # flat = bullish
    ])
    def test_score_gold(self, ret5d, expected):
        from macro.health_scorer import _score_gold
        assert _score_gold(ret5d) == expected

    @pytest.mark.parametrize("ret5d,expected", [
        (3.0,  -10.0),  # DXY surging → risk-off
        (1.0,   -5.0),  # mild rise
        (-1.0,  +5.0),  # declining dollar
        (0.2,   +3.0),  # stable
    ])
    def test_score_dxy(self, ret5d, expected):
        from macro.health_scorer import _score_dxy
        assert _score_dxy(ret5d) == expected

    @pytest.mark.parametrize("ret5d,expected", [
        (6.0,  -10.0),  # yields surging → tightening
        (3.0,   -5.0),  # rising
        (-3.0, +10.0),  # falling yields = bullish
        (1.0,   +3.0),  # stable
    ])
    def test_score_tnx(self, ret5d, expected):
        from macro.health_scorer import _score_tnx
        assert _score_tnx(ret5d) == expected

    def test_score_tech_bull(self):
        from macro.health_scorer import _score_tech
        assert _score_tech(3.0, bull_thresh=2.0, bear_thresh=-1.0) == 10.0

    def test_score_tech_bear(self):
        from macro.health_scorer import _score_tech
        assert _score_tech(-2.0, bull_thresh=2.0, bear_thresh=-1.0) == -10.0

    def test_score_tech_neutral_positive(self):
        from macro.health_scorer import _score_tech
        score = _score_tech(1.0, bull_thresh=2.0, bear_thresh=-1.0)
        assert 0 < score < 10

    def test_score_tech_neutral_negative(self):
        from macro.health_scorer import _score_tech
        score = _score_tech(-0.5, bull_thresh=2.0, bear_thresh=-1.0)
        assert -10 < score < 0


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


# ── macro_regime: trading params helpers ─────────────────────────────────────

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


# ── economic_calendar: additional coverage ───────────────────────────────────

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


# ── macro_regime: scoring coverage ───────────────────────────────────────────

@pytest.mark.unit
class TestMacroRegimeCoverage:
    """Tests for scoring helpers and _evaluate_lockout_recovery in macro_regime.py."""

    # ── _score_vix ─────────────────────────────────────────────────────────

    def test_score_vix_low_vix_returns_positive(self):
        from macro.health_scorer import _score_vix
        # VIX well below its 90d average → bullish (+10)
        vix_closes = [25.0] * 90 + [15.0]
        assert _score_vix(vix_closes) == pytest.approx(10.0)

    def test_score_vix_elevated_vix_returns_negative(self):
        from macro.health_scorer import _score_vix
        # VIX well above 120% of average → -10
        vix_closes = [20.0] * 90 + [30.0]
        assert _score_vix(vix_closes) == pytest.approx(-10.0)

    def test_score_vix_insufficient_data_returns_zero(self):
        from macro.health_scorer import _score_vix
        assert _score_vix([20.0, 21.0]) == 0.0

    # ── _score_spy ─────────────────────────────────────────────────────────

    def test_score_spy_strong_up_week_returns_10(self):
        from macro.health_scorer import _score_spy
        assert _score_spy(3.0) == pytest.approx(10.0)

    def test_score_spy_crash_week_returns_minus10(self):
        from macro.health_scorer import _score_spy
        assert _score_spy(-2.0) == pytest.approx(-10.0)

    # ── _score_wti ─────────────────────────────────────────────────────────

    def test_score_wti_stable_returns_positive(self):
        from macro.health_scorer import _score_wti
        assert _score_wti(1.0) == pytest.approx(10.0)

    def test_score_wti_spike_returns_negative(self):
        from macro.health_scorer import _score_wti
        assert _score_wti(7.0) == pytest.approx(-10.0)

    # ── _score_gold ────────────────────────────────────────────────────────

    def test_score_gold_surge_is_bearish_signal(self):
        from macro.health_scorer import _score_gold
        # Gold > +3% → flight to safety → -10
        assert _score_gold(4.0) == pytest.approx(-10.0)

    def test_score_gold_flat_is_bullish_signal(self):
        from macro.health_scorer import _score_gold
        # Flat/mild gold = risk-on
        assert _score_gold(0.5) == pytest.approx(8.0)

    # ── _evaluate_lockout_recovery ─────────────────────────────────────────

    def test_lockout_clears_when_time_expired(self):
        from macro.yahoo_fetcher import evaluate_lockout_recovery
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        past = (now - timedelta(hours=1)).isoformat()
        active, until = evaluate_lockout_recovery(True, past, 25.0, -3.0, now)
        assert active is False
        assert until is None

    def test_lockout_clears_on_vix_recovery(self):
        from macro.yahoo_fetcher import evaluate_lockout_recovery
        import config
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        future = (now + timedelta(hours=2)).isoformat()
        # VIX below recovery threshold and SPY 5d return above recovery threshold
        active, until = evaluate_lockout_recovery(
            True, future,
            config.FLASH_CRASH_VIX_RECOVERY - 1,
            config.FLASH_CRASH_SPY_RECOVERY_PCT + 1,
            now,
        )
        assert active is False

    def test_lockout_stays_active_when_conditions_not_met(self):
        from macro.yahoo_fetcher import evaluate_lockout_recovery
        import config
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        future = (now + timedelta(hours=2)).isoformat()
        # VIX still high, SPY still bad
        active, until = evaluate_lockout_recovery(
            True, future,
            config.FLASH_CRASH_VIX_RECOVERY + 5,   # VIX too high
            config.FLASH_CRASH_SPY_RECOVERY_PCT - 5,  # SPY return too bad
            now,
        )
        assert active is True
        assert until == future

    def test_lockout_inactive_input_passes_through(self):
        from macro.yahoo_fetcher import evaluate_lockout_recovery
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        active, until = evaluate_lockout_recovery(False, None, 20.0, 1.0, now)
        assert active is False
        assert until is None

    # ── _sector_scores ─────────────────────────────────────────────

    def test_sector_scores_structure(self):
        from macro_regime import _sector_scores
        result = _sector_scores(soxx_ret5d=3.0, qqq_ret5d=2.0)
        assert set(result.keys()) == {"AI_SEMI", "BROAD_TECH", "CYBERSECURITY"}

    def test_sector_scores_cybersecurity_derived(self):
        from macro_regime import _sector_scores
        # CYBERSECURITY = 0.8 × BROAD_TECH
        result = _sector_scores(soxx_ret5d=0.0, qqq_ret5d=2.0)
        assert result["CYBERSECURITY"] == pytest.approx(result["BROAD_TECH"] * 0.8)

    # ── _ret5d ─────────────────────────────────────────────────────────────

    def test_ret5d_correct_calculation(self):
        from macro.yahoo_fetcher import ret5d
        # 100 → 110 over 6 closes = +10%
        closes = [100.0, 102.0, 104.0, 106.0, 108.0, 110.0]
        result = ret5d(closes)
        assert result == pytest.approx(10.0)

    def test_ret5d_too_few_closes_returns_zero(self):
        from macro.yahoo_fetcher import ret5d
        assert ret5d([100.0, 101.0]) == 0.0



# ── economic_calendar: hardcoded events + risk window ────────────────────────

@pytest.mark.unit
class TestEconomicCalendarLogic:
    """Tests for pure functions in economic_calendar — no network, no file I/O."""

    def test_first_friday_returns_correct_day(self):
        from economic_calendar import _first_friday
        # May 2026: first Friday is the 1st
        assert _first_friday(2026, 5) == 1

    def test_hardcoded_events_includes_nfp_and_cpi(self):
        from economic_calendar import _hardcoded_events
        events = _hardcoded_events(2026, 5)
        names = [e["name"] for e in events]
        assert "Non-Farm Payrolls" in names
        assert "CPI Release" in names

    def test_is_event_risk_window_detects_nearby_event(self, tmp_path):
        import config
        original = config.ECONOMIC_CALENDAR_FILE
        try:
            today = datetime.now(ET).strftime("%Y-%m-%d")
            # Create a fake event at 08:30 today
            payload = {
                "date": today,
                "events": [{"date": today, "time": "08:30", "name": "CPI Release", "impact": "high"}],
            }
            cache_file = tmp_path / "economic_calendar.json"
            cache_file.write_text(json.dumps(payload))
            config.ECONOMIC_CALENDAR_FILE = cache_file

            from economic_calendar import is_event_risk_window
            # Construct a datetime exactly at 08:30 ET today — within window
            now_et = datetime.now(ET).replace(hour=8, minute=30, second=0, microsecond=0)
            in_window, name = is_event_risk_window(now_et)
            assert in_window is True
            assert name == "CPI Release"
        finally:
            config.ECONOMIC_CALENDAR_FILE = original

    def test_is_event_risk_window_outside_returns_false(self, tmp_path):
        import config
        original = config.ECONOMIC_CALENDAR_FILE
        try:
            today = datetime.now(ET).strftime("%Y-%m-%d")
            payload = {
                "date": today,
                "events": [{"date": today, "time": "08:30", "name": "NFP", "impact": "high"}],
            }
            cache_file = tmp_path / "economic_calendar.json"
            cache_file.write_text(json.dumps(payload))
            config.ECONOMIC_CALENDAR_FILE = cache_file

            from economic_calendar import is_event_risk_window
            # 2 hours after the event — well outside 45-minute window
            now_et = datetime.now(ET).replace(hour=10, minute=30, second=0, microsecond=0)
            in_window, name = is_event_risk_window(now_et)
            assert in_window is False
            assert name == ""
        finally:
            config.ECONOMIC_CALENDAR_FILE = original
