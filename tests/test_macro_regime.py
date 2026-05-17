"""
Macro regime unit tests: health scorer, classify_regime, pattern detection,
flash crash, signal scorers, lockout recovery.
Run with: pytest tests/test_macro_regime.py -v -m unit
"""
import json
from datetime import datetime

import pytest

from utils.constants import ET


# ── macro_regime: health score ────────────────────────────────────────────────

@pytest.mark.unit
class TestMacroHealthScore:
    def test_bullish_inputs_give_positive_score(self):
        from macro.health_scorer import compute_macro_health_score
        vix_closes = [20.0] * 90 + [14.0]
        signals = {
            "vix_closes": vix_closes,
            "spy_ret5d":  3.0,
            "soxx_ret5d": 4.0,
            "qqq_ret5d":  3.0,
            "wti_ret5d":  1.0,
            "tnx_ret5d":  -3.0,
            "gold_ret5d": 0.5,
            "dxy_ret5d":  -1.0,
        }
        score = compute_macro_health_score(signals)
        assert score > 0, f"Expected positive score for bullish inputs, got {score}"

    def test_bearish_inputs_give_negative_score(self):
        from macro.health_scorer import compute_macro_health_score
        vix_closes = [15.0] * 90 + [40.0]
        signals = {
            "vix_closes": vix_closes,
            "spy_ret5d":  -8.0,
            "soxx_ret5d": -10.0,
            "qqq_ret5d":  -8.0,
            "wti_ret5d":  8.0,
            "tnx_ret5d":  7.0,
            "gold_ret5d": 5.0,
            "dxy_ret5d":  3.0,
        }
        score = compute_macro_health_score(signals)
        assert score < 0, f"Expected negative score for bearish inputs, got {score}"

    def test_neutral_inputs_near_zero(self):
        from macro.health_scorer import compute_macro_health_score
        vix_closes = [20.0] * 91
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
        assert -20 < score < 65, f"Expected broadly neutral score, got {score}"

    def test_score_clamped_to_bounds(self):
        from macro.health_scorer import compute_macro_health_score
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
        regime = self._classify(consecutive_green_days=3, macro_health_score=25)
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
            ai_semi_sector_score=20,
        )
        assert regime == "OPPORTUNITY"

    def test_normal_when_no_conditions_match(self):
        assert self._classify() == "NORMAL"

    def test_defensive_requires_both_market_and_pnl(self):
        regime = self._classify(
            consecutive_red_days=3,
            macro_health_score=-25,
            bot_net_pnl_pct=+0.5,
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
        closes = [100, 101, 102, 103, 104, 105]
        assert _count_consecutive_direction(closes, "green") == 5

    def test_consecutive_red(self):
        from macro.regime_classifier import _count_consecutive_direction
        closes = [100, 99, 98, 97]
        assert _count_consecutive_direction(closes, "red") == 3

    def test_choppy_true(self):
        from macro.regime_classifier import _is_choppy
        closes = [100.0, 100.2, 100.0, 100.2, 100.0, 100.1]
        assert _is_choppy(closes) is True

    def test_choppy_false_trending(self):
        from macro.regime_classifier import _is_choppy
        closes = [100, 101, 102, 103, 104, 105]
        assert _is_choppy(closes) is False

    def test_choppy_false_big_net_move(self):
        from macro.regime_classifier import _is_choppy
        closes = [100, 101, 99.5, 102, 100, 104]
        assert _is_choppy(closes) is False


# ── macro_regime: flash crash ─────────────────────────────────────────────────

@pytest.mark.unit
class TestFlashCrash:
    def test_triggers_at_exactly_2point5_pct(self):
        from macro.regime import check_flash_crash
        assert check_flash_crash(spy_current=97.5, spy_prev_close=100.0) is True

    def test_triggers_below_threshold(self):
        from macro.regime import check_flash_crash
        assert check_flash_crash(spy_current=95.0, spy_prev_close=100.0) is True

    def test_does_not_trigger_just_above_threshold(self):
        from macro.regime import check_flash_crash
        assert check_flash_crash(spy_current=97.6, spy_prev_close=100.0) is False

    def test_no_crash_on_green_day(self):
        from macro.regime import check_flash_crash
        assert check_flash_crash(spy_current=102.0, spy_prev_close=100.0) is False

    def test_zero_prev_close_safe(self):
        from macro.regime import check_flash_crash
        assert check_flash_crash(spy_current=100.0, spy_prev_close=0.0) is False


# ── Plan 3: Flash Crash Config ───────────────────────────────────────────────

@pytest.mark.unit
class TestFlashCrashConfig:
    def test_check_flash_crash_uses_config_threshold(self, monkeypatch):
        import config
        from macro.regime import check_flash_crash
        monkeypatch.setattr(config, "FLASH_CRASH_SPY_DROP_PCT", -5.0)
        assert check_flash_crash(spy_current=96.0, spy_prev_close=100.0) is False
        assert check_flash_crash(spy_current=94.0, spy_prev_close=100.0) is True

    def test_load_macro_regime_default_has_candidate_field(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "MACRO_REGIME_FILE", tmp_path / "macro_regime.json")
        from macro.regime import load_macro_regime
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
        closes = [20.0] * 91 + [14.0]
        assert _score_vix(closes) == 10.0

    def test_score_vix_high_returns_negative(self):
        from macro.health_scorer import _score_vix
        closes = [20.0] * 91 + [25.0]
        assert _score_vix(closes) == -10.0

    def test_score_vix_in_range_is_linear(self):
        from macro.health_scorer import _score_vix
        closes = [20.0] * 92
        score = _score_vix(closes)
        assert -10.0 <= score <= 10.0

    def test_score_vix_insufficient_data_returns_zero(self):
        from macro.health_scorer import _score_vix
        assert _score_vix([20.0] * 5) == 0.0

    @pytest.mark.parametrize("ret5d,expected", [
        (2.0,  10.0),
        (-1.0, -10.0),
        (0.5,  pytest.approx(2.5, abs=0.1)),
    ])
    def test_score_spy(self, ret5d, expected):
        from macro.health_scorer import _score_spy
        assert _score_spy(ret5d) == expected

    @pytest.mark.parametrize("ret5d,expected", [
        (1.0,   10.0),
        (6.0,  -10.0),
        (-9.0, -10.0),
        (3.0,    0.0),
    ])
    def test_score_wti(self, ret5d, expected):
        from macro.health_scorer import _score_wti
        assert _score_wti(ret5d) == expected

    @pytest.mark.parametrize("ret5d,expected", [
        (4.0,  -10.0),
        (2.5,   -5.0),
        (-3.0,  +5.0),
        (0.5,   +8.0),
    ])
    def test_score_gold(self, ret5d, expected):
        from macro.health_scorer import _score_gold
        assert _score_gold(ret5d) == expected

    @pytest.mark.parametrize("ret5d,expected", [
        (3.0,  -10.0),
        (1.0,   -5.0),
        (-1.0,  +5.0),
        (0.2,   +3.0),
    ])
    def test_score_dxy(self, ret5d, expected):
        from macro.health_scorer import _score_dxy
        assert _score_dxy(ret5d) == expected

    @pytest.mark.parametrize("ret5d,expected", [
        (6.0,  -10.0),
        (3.0,   -5.0),
        (-3.0, +10.0),
        (1.0,   +3.0),
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
    def test_missing_file_returns_normal_default(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "MACRO_REGIME_FILE", tmp_path / "macro_regime.json")
        from macro.regime import load_macro_regime
        result = load_macro_regime()
        assert result["regime"] == "NORMAL"
        assert "macro_health_score" in result

    def test_loads_written_regime(self, tmp_path, monkeypatch):
        import config
        regime_file = tmp_path / "macro_regime.json"
        monkeypatch.setattr(config, "MACRO_REGIME_FILE", regime_file)
        payload = {"regime": "BULL_TREND", "health_score": 55, "capitulation_candidate": []}
        with open(regime_file, "w") as f:
            json.dump(payload, f)
        from macro.regime import load_macro_regime
        result = load_macro_regime()
        assert result["regime"] == "BULL_TREND"
        assert result["health_score"] == 55

    def test_corrupt_file_returns_default(self, tmp_path, monkeypatch):
        import config
        regime_file = tmp_path / "macro_regime.json"
        monkeypatch.setattr(config, "MACRO_REGIME_FILE", regime_file)
        regime_file.write_text("NOT JSON{{")
        from macro.regime import load_macro_regime
        result = load_macro_regime()
        assert result["regime"] == "NORMAL"


# ── macro_regime: trading params helpers ─────────────────────────────────────

@pytest.mark.unit
class TestVixRegimeHelpers:
    def test_load_macro_regime_missing_file_returns_normal(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.MACRO_REGIME_FILE", tmp_path / "macro_regime.json")
        from macro.regime import load_macro_regime
        result = load_macro_regime()
        assert result["regime"] == "NORMAL"

    def test_load_macro_regime_reads_written_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.MACRO_REGIME_FILE", tmp_path / "macro_regime.json")
        payload = {"regime": "CRISIS", "macro_health_score": -50}
        (tmp_path / "macro_regime.json").write_text(json.dumps(payload))
        from macro.regime import load_macro_regime
        result = load_macro_regime()
        assert result["regime"] == "CRISIS"

    def test_load_trading_params_returns_config_defaults(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.TRADING_PARAMS_FILE", tmp_path / "trading_params.json")
        from macro.regime import load_trading_params
        params = load_trading_params()
        import config
        assert "entry_start_hour" in params
        assert params["stop_loss_pct"] == config.STOP_LOSS_PCT

    def test_load_trading_params_overrides_from_file(self, tmp_path, monkeypatch):
        params_file = tmp_path / "trading_params.json"
        params_file.write_text(json.dumps({"stop_loss_pct": 3.5}))
        monkeypatch.setattr("config.TRADING_PARAMS_FILE", params_file)
        from macro.regime import load_trading_params
        params = load_trading_params()
        assert params["stop_loss_pct"] == 3.5


# ── macro_regime: scoring coverage ───────────────────────────────────────────

@pytest.mark.unit
class TestMacroRegimeCoverage:
    """Tests for sector scores and ret5d in macro_regime.py."""

    def test_sector_scores_structure(self):
        from macro.regime import _sector_scores
        result = _sector_scores(soxx_ret5d=3.0, qqq_ret5d=2.0)
        assert set(result.keys()) == {"AI_SEMI", "BROAD_TECH", "CYBERSECURITY"}

    def test_sector_scores_cybersecurity_derived(self):
        from macro.regime import _sector_scores
        result = _sector_scores(soxx_ret5d=0.0, qqq_ret5d=2.0)
        assert result["CYBERSECURITY"] == pytest.approx(result["BROAD_TECH"] * 0.8)

    def test_ret5d_correct_calculation(self):
        from macro.yahoo_fetcher import ret5d
        closes = [100.0, 102.0, 104.0, 106.0, 108.0, 110.0]
        result = ret5d(closes)
        assert result == pytest.approx(10.0)

    def test_ret5d_too_few_closes_returns_zero(self):
        from macro.yahoo_fetcher import ret5d
        assert ret5d([100.0, 101.0]) == 0.0
