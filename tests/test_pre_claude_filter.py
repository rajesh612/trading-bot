"""Tests for pre_claude_filter, daily_closed layer, sub-ETF threading, and dynamic regime params."""
import pytest
from unittest.mock import patch, MagicMock

from utils.constants import ET
from utils.regime_params import (
    get_filter_params, get_gate_params, get_auto_execute_params,
    FilterParams, GateParams, AutoExecuteParams,
)


# ─── pre_claude_filter ───────────────────────────────────────────────────────

class TestPreClaudeFilter:

    def _snap(self, **overrides) -> "CycleSnapshot":
        from trading.cycle_context import CycleSnapshot
        defaults = dict(
            regime_info={"regime": "NORMAL", "macro_health_score": 0},
            regime="NORMAL",
            fred_cache={},
            research={"TEST": {"sentiment_score": 20}},
            research_meta={"macro_shock_score": 0},
            strategy_memory="",
            weights={},
            params={},
            portfolio_gain_pct=0.0,
            intraday_news={},
        )
        defaults.update(overrides)
        return CycleSnapshot(**defaults)

    def _iv(self, composite=50.0, rsi=55.0, stoch_d=50.0, **kw):
        from tests.test_signals import make_full_iv
        return make_full_iv(composite_score=composite, rsi=rsi, stoch_d=stoch_d, **kw)

    @pytest.mark.unit
    def test_skip_composite_below_minimum(self):
        from trading.pre_claude_filter import apply_pre_claude_filter
        result = apply_pre_claude_filter("TEST", self._iv(composite=15.0), self._snap())
        assert result.decision == "SKIP"
        assert "composite" in result.reason

    @pytest.mark.unit
    def test_skip_rsi_hard_veto(self):
        from trading.pre_claude_filter import apply_pre_claude_filter
        # composite=65 → MEDIUM tier → passes composite gate; rsi=75 > RSI_BUY_MAX=72 → SKIP
        result = apply_pre_claude_filter("TEST", self._iv(composite=65.0, rsi=75.0), self._snap())
        assert result.decision == "SKIP"
        assert "RSI" in result.reason

    @pytest.mark.unit
    def test_skip_macro_shock(self):
        from trading.pre_claude_filter import apply_pre_claude_filter
        snap = self._snap(research_meta={"macro_shock_score": 60})
        result = apply_pre_claude_filter("TEST", self._iv(composite=65.0), snap)
        assert result.decision == "SKIP"
        assert "shock" in result.reason

    @pytest.mark.unit
    def test_skip_macro_health_too_low(self):
        from trading.pre_claude_filter import apply_pre_claude_filter
        snap = self._snap(regime_info={"regime": "NORMAL", "macro_health_score": -30})
        result = apply_pre_claude_filter("TEST", self._iv(composite=65.0), snap)
        assert result.decision == "SKIP"
        assert "macro health" in result.reason

    @pytest.mark.unit
    def test_skip_bearish_sentiment_hard_block(self):
        from trading.pre_claude_filter import apply_pre_claude_filter
        snap = self._snap(research={"TEST": {"sentiment_score": -20}})
        # composite=65 reaches NORMAL+LOW tier (negative sentiment), composite_min=55, 65>55 passes gate
        result = apply_pre_claude_filter("TEST", self._iv(composite=65.0), snap)
        assert result.decision == "SKIP"
        assert "sentiment" in result.reason

    @pytest.mark.unit
    def test_skip_negative_sentiment_weak_composite(self):
        from trading.pre_claude_filter import apply_pre_claude_filter
        snap = self._snap(research={"TEST": {"sentiment_score": -5}})
        # composite=25 < SENTIMENT_WEAK_COMPOSITE_MIN=30, negative sentiment → SKIP
        result = apply_pre_claude_filter("TEST", self._iv(composite=25.0), snap)
        assert result.decision == "SKIP"

    @pytest.mark.unit
    def test_skip_intraday_shock(self):
        from trading.pre_claude_filter import apply_pre_claude_filter
        snap = self._snap(intraday_news={"TEST": {"shock_score": 65, "sentiment_score": 10}})
        result = apply_pre_claude_filter("TEST", self._iv(composite=65.0), snap)
        assert result.decision == "SKIP"
        assert "shock" in result.reason

    @pytest.mark.unit
    def test_auto_execute_strong_signal(self):
        from trading.pre_claude_filter import apply_pre_claude_filter
        # composite=76 > NORMAL+MEDIUM AE threshold (ep.min=55 + offset=20 = 75), intra_sent=25 > 20
        snap = self._snap(intraday_news={"TEST": {"shock_score": 0, "sentiment_score": 25}})
        result = apply_pre_claude_filter("TEST", self._iv(composite=76.0), snap)
        assert result.decision == "AUTO_EXECUTE"

    @pytest.mark.unit
    def test_auto_execute_not_triggered_below_new_composite_threshold(self):
        """composite=70 (= AE threshold, not strictly >) routes to CLAUDE."""
        from trading.pre_claude_filter import apply_pre_claude_filter
        snap = self._snap(intraday_news={"TEST": {"shock_score": 0, "sentiment_score": 25}})
        result = apply_pre_claude_filter("TEST", self._iv(composite=70.0), snap)
        assert result.decision == "CLAUDE"

    @pytest.mark.unit
    def test_opportunity_high_auto_execute_fires_when_sub_etf_meets_threshold(self):
        """OPPORTUNITY+HIGH must AUTO_EXECUTE when sub_etf_chg >= 0.3 and composite > threshold.

        Regression for dead-code bug: pipeline was passing sub_etf_chg=0.0 (default),
        so 0.0 >= 0.3 always failed and OPPORTUNITY could never auto-execute.

        Tier math (need score >= 60 for HIGH):
          composite=95 → 25pts, sentiment=35 → 14pts, intraday=35 → 10pts,
          sub_etf=0.5 → 5pts, earnings=30 → 10pts  →  total=64 → HIGH tier
        AE threshold for OPPORTUNITY+HIGH = max(45, 40) + 15 = 60; composite=95 > 60 ✓
        """
        from trading.pre_claude_filter import apply_pre_claude_filter
        snap = self._snap(
            regime_info={"regime": "OPPORTUNITY", "macro_health_score": 10},
            regime="OPPORTUNITY",
            research={"TEST": {"sentiment_score": 35}},
            intraday_news={"TEST": {"shock_score": 0, "sentiment_score": 35}},
        )
        result = apply_pre_claude_filter(
            "TEST", self._iv(composite=95.0), snap, sub_etf_chg=0.5
        )
        assert result.decision == "AUTO_EXECUTE", (
            f"OPPORTUNITY HIGH should AUTO_EXECUTE with sub_etf_chg=0.5; got {result.decision}: {result.reason}"
        )

    @pytest.mark.unit
    def test_opportunity_high_stays_claude_when_sub_etf_below_threshold(self):
        """OPPORTUNITY+HIGH must NOT auto-execute when sub_etf_chg < 0.3 (old broken default)."""
        from trading.pre_claude_filter import apply_pre_claude_filter
        snap = self._snap(
            regime_info={"regime": "OPPORTUNITY", "macro_health_score": 10},
            regime="OPPORTUNITY",
            research={"TEST": {"sentiment_score": 35}},
            intraday_news={"TEST": {"shock_score": 0, "sentiment_score": 35}},
        )
        result = apply_pre_claude_filter(
            "TEST", self._iv(composite=95.0), snap, sub_etf_chg=0.0
        )
        assert result.decision == "CLAUDE", (
            f"sub_etf_chg=0.0 < 0.3 must block OPPORTUNITY AE; got {result.decision}"
        )

    @pytest.mark.unit
    def test_pipeline_populates_sub_etf_chg_for_opportunity_filter(self):
        """Pipeline Phase 2 must pass sub_etf_chg from sector_etf_cache to the filter.

        Previously sub_etf_chg always defaulted to 0.0, making OPPORTUNITY AE dead code.
        NVDA has sub_etf=SOXX; sector_etf_cache[SOXX]=0.5 → sub_etf_chg=0.5 >= 0.3 → AE fires.
        """
        from trading.pre_claude_filter import apply_pre_claude_filter
        snap = self._snap(
            regime_info={"regime": "OPPORTUNITY", "macro_health_score": 10},
            regime="OPPORTUNITY",
            research={"NVDA": {"sentiment_score": 35}},
            intraday_news={"NVDA": {"shock_score": 0, "sentiment_score": 35}},
            sector_etf_cache={"SOXX": 0.5},
        )
        result = apply_pre_claude_filter(
            "NVDA", self._iv(composite=95.0), snap, sub_etf_chg=0.5
        )
        assert result.decision == "AUTO_EXECUTE", (
            f"NVDA (SOXX) with etf_chg=0.5 >= 0.3 must AUTO_EXECUTE in OPPORTUNITY+HIGH; got {result.decision}"
        )

    @pytest.mark.unit
    def test_auto_execute_not_triggered_below_new_sentiment_threshold(self):
        """composite=76 > AE threshold but intra_sent=19 < sentiment_min=20 → CLAUDE."""
        from trading.pre_claude_filter import apply_pre_claude_filter
        snap = self._snap(intraday_news={"TEST": {"shock_score": 0, "sentiment_score": 19}})
        result = apply_pre_claude_filter("TEST", self._iv(composite=76.0), snap)
        assert result.decision == "CLAUDE"

    @pytest.mark.unit
    def test_claude_path_moderate_signal(self):
        from trading.pre_claude_filter import apply_pre_claude_filter
        # composite=65 → MEDIUM tier, passes composite gate (55), below AE threshold (75) → CLAUDE
        snap = self._snap(intraday_news={"TEST": {"shock_score": 0, "sentiment_score": 5}})
        result = apply_pre_claude_filter("TEST", self._iv(composite=65.0), snap)
        assert result.decision == "CLAUDE"

    @pytest.mark.unit
    def test_claude_path_no_intraday_cache(self):
        """Falls back to research sentiment when intraday cache is empty."""
        from trading.pre_claude_filter import apply_pre_claude_filter
        snap = self._snap(
            research={"TEST": {"sentiment_score": 25}},
            intraday_news={},  # stale/missing
        )
        result = apply_pre_claude_filter("TEST", self._iv(composite=65.0), snap)
        assert result.decision == "CLAUDE"

    @pytest.mark.unit
    def test_high_vol_regime_raises_composite_bar(self):
        from trading.pre_claude_filter import apply_pre_claude_filter
        snap = self._snap(
            regime_info={"regime": "HIGH_VOL", "macro_health_score": 0},
            regime="HIGH_VOL",
        )
        # composite=30 passes NORMAL (>=30) but fails HIGH_VOL (>=35)
        result = apply_pre_claude_filter("TEST", self._iv(composite=30.0), snap)
        assert result.decision == "SKIP"

    @pytest.mark.unit
    def test_choppy_composite_gate_blocks_below_gate(self):
        """In CHOPPY regime, composite well below CHOPPY_COMPOSITE_GATE should be SKIP."""
        from trading.pre_claude_filter import apply_pre_claude_filter
        snap = self._snap(
            regime_info={"regime": "CHOPPY", "macro_health_score": 0},
            regime="CHOPPY",
        )
        result = apply_pre_claude_filter("TEST", self._iv(composite=25.0), snap)
        assert result.decision == "SKIP"
        assert "composite" in result.reason

    @pytest.mark.unit
    def test_choppy_composite_gate_passes_above_gate(self):
        """In CHOPPY regime, composite well above gate reaches MEDIUM tier and passes Layer 1."""
        from trading.pre_claude_filter import apply_pre_claude_filter
        snap = self._snap(
            regime_info={"regime": "CHOPPY", "macro_health_score": 0},
            regime="CHOPPY",
        )
        # composite=70 → MEDIUM tier in CHOPPY (ep.min=55, score earns enough pts), composite_min=55
        result = apply_pre_claude_filter("TEST", self._iv(composite=70.0), snap)
        assert result.decision != "SKIP" or "composite" not in result.reason

    @pytest.mark.unit
    def test_vwap_slope_not_blocked_in_pre_claude_filter(self):
        """Rule 23: VWAP slope is a safety gate — must NOT be enforced by pre_claude_filter.

        VWAP slope enforcement lives exclusively in check_all_gates() via DayTradeParams.vwap_slope_min.
        pre_claude_filter is a quality/budget router only.
        """
        from trading.pre_claude_filter import apply_pre_claude_filter
        from tests.test_signals import make_full_iv
        snap = self._snap()
        # composite=65 → MEDIUM tier, rsi=55 passes, vwap_slope=-0.5 (downtrend)
        # pre_claude_filter must NOT block on VWAP slope — that is check_all_gates() territory
        iv = make_full_iv(composite_score=65.0, rsi=55.0, stoch_d=50.0, vwap_slope=-0.5)
        result = apply_pre_claude_filter("TEST", iv, snap)
        assert "VWAP slope" not in result.reason, (
            f"Rule 23 violated: VWAP slope blocked in pre_claude_filter: {result.reason}"
        )

    @pytest.mark.unit
    def test_layer_2_5_vwap_overextension_not_blocked_in_filter(self):
        """Option A: price > VWAP+2σ is a safety gate, not a quality signal — must pass filter."""
        from trading.pre_claude_filter import apply_pre_claude_filter
        snap = self._snap()
        iv = self._iv(composite=65.0)
        result = apply_pre_claude_filter("TEST", iv, snap)
        assert result.decision != "SKIP", (
            f"Option A violated: VWAP overextension blocked in pre_claude_filter: {result.reason}"
        )


# ─── daily_closed as Layer 0 (Task 4: Fix 3) ────────────────────────────────────

class TestPreClaudeFilterDailyClosed:

    def _snap(self):
        from trading.cycle_context import CycleSnapshot
        return CycleSnapshot(
            regime_info={"regime": "NORMAL", "macro_health_score": 0},
            regime="NORMAL",
            fred_cache={},
            research={"TEST": {"sentiment_score": 20}},
            research_meta={"macro_shock_score": 0},
            strategy_memory="",
            weights={},
            params={},
            portfolio_gain_pct=0.0,
            intraday_news={},
        )

    def _iv(self, composite=50.0):
        from tests.test_signals import make_full_iv
        return make_full_iv(composite_score=composite, rsi=55.0, stoch_d=50.0)

    @pytest.mark.unit
    def test_layer0_skips_already_closed_symbol(self):
        """Symbol in daily_closed returns SKIP before any other layer."""
        from trading.pre_claude_filter import apply_pre_claude_filter
        result = apply_pre_claude_filter("TEST", self._iv(composite=80.0), self._snap(),
                                         daily_closed={"TEST"})
        assert result.decision == "SKIP"
        assert "already closed" in result.reason

    @pytest.mark.unit
    def test_layer0_passes_symbol_not_in_closed(self):
        """Symbol absent from daily_closed is not blocked by Layer 0."""
        from trading.pre_claude_filter import apply_pre_claude_filter
        result = apply_pre_claude_filter("TEST", self._iv(composite=50.0), self._snap(),
                                         daily_closed={"OTHER"})
        # Must not be skipped due to "already closed"
        assert not (result.decision == "SKIP" and "already closed" in result.reason)

    @pytest.mark.unit
    def test_layer0_not_triggered_with_empty_set(self):
        """Passing an empty set never triggers Layer 0 SKIP."""
        from trading.pre_claude_filter import apply_pre_claude_filter
        result = apply_pre_claude_filter("TEST", self._iv(composite=50.0), self._snap(),
                                         daily_closed=set())
        if result.decision == "SKIP":
            assert "already closed" not in result.reason

    @pytest.mark.unit
    def test_layer0_not_triggered_when_none(self):
        """daily_closed=None (default) skips Layer 0 entirely."""
        from trading.pre_claude_filter import apply_pre_claude_filter
        result = apply_pre_claude_filter("TEST", self._iv(composite=50.0), self._snap())
        if result.decision == "SKIP":
            assert "already closed" not in result.reason


class TestPipelineSubEtfThreading:
    """Pipeline Phase 2 must read sub_etf_chg from sector_etf_cache and pass it to the filter.

    Previously pipeline called apply_pre_claude_filter without sub_etf_chg (defaulted to 0.0),
    making OPPORTUNITY HIGH/CONVICTION auto-execute permanently dead.
    """

    def _make_pipeline_snap(self, regime="OPPORTUNITY", etf_cache=None):
        from trading.cycle_context import CycleSnapshot
        from utils.data_health import FRESH_GUARD
        return CycleSnapshot(
            regime_info={"regime": regime, "macro_health_score": 20,
                         "vix_current": 15.0, "spy_5day_return": 1.0},
            regime=regime, fred_cache={},
            research={"NVDA": {"sentiment_score": 35}},
            research_meta={"macro_shock_score": 0},
            strategy_memory="",
            weights={}, params={}, portfolio_gain_pct=0.0,
            intraday_news={"NVDA": {"shock_score": 0, "sentiment_score": 35}},
            staleness_guard=FRESH_GUARD,
            sector_etf_cache=etf_cache or {},
        )

    @pytest.mark.unit
    def test_pipeline_passes_sub_etf_chg_from_cache_to_filter(self, monkeypatch):
        """Phase 2 must call apply_pre_claude_filter with sub_etf_chg=0.5 for NVDA (sub_etf=SOXX).

        Regression: pipeline previously omitted sub_etf_chg (defaulted 0.0 in filter),
        permanently blocking OPPORTUNITY AE.
        """
        import trading.pipeline as pipeline_mod
        captured_calls: list[dict] = []

        orig_filter = pipeline_mod.apply_pre_claude_filter
        def spy_filter(symbol, iv, snap, *args, sub_etf_chg=0.0, **kwargs):
            captured_calls.append({"symbol": symbol, "sub_etf_chg": sub_etf_chg})
            return orig_filter(symbol, iv, snap, *args, sub_etf_chg=sub_etf_chg, **kwargs)

        monkeypatch.setattr(pipeline_mod, "apply_pre_claude_filter", spy_filter)

        snap = self._make_pipeline_snap(
            regime="OPPORTUNITY",
            etf_cache={"SOXX": 0.5},
        )
        from tests.test_signals import make_full_iv
        iv = make_full_iv(composite_score=95.0, rsi=55.0, stoch_d=50.0)

        pipeline_mod._run_pre_filter_for_symbol("NVDA", iv, snap, set(), None)

        nvda_call = next((c for c in captured_calls if c["symbol"] == "NVDA"), None)
        assert nvda_call is not None, "apply_pre_claude_filter was not called for NVDA"
        assert nvda_call["sub_etf_chg"] == pytest.approx(0.5), (
            f"Pipeline must pass sub_etf_chg=0.5 (from cache[SOXX]); got {nvda_call['sub_etf_chg']}"
        )


class TestDynamicRegimeParams:
    @pytest.mark.unit
    def test_filter_params_all_regimes_covered(self):
        from utils.regimes import VALID
        for regime in VALID:
            for tier in ("LOW", "MEDIUM", "HIGH", "CONVICTION"):
                p = get_filter_params(regime, tier)
                assert isinstance(p, FilterParams)

    @pytest.mark.unit
    def test_gate_params_all_regimes_covered(self):
        from utils.regimes import VALID
        for regime in VALID:
            for tier in ("LOW", "MEDIUM", "HIGH", "CONVICTION"):
                p = get_gate_params(regime, tier)
                assert isinstance(p, GateParams)

    @pytest.mark.unit
    def test_auto_execute_disabled_for_crisis(self):
        for tier in ("LOW", "MEDIUM", "HIGH", "CONVICTION"):
            p = get_auto_execute_params("CRISIS", tier)
            assert p.composite_offset == 999  # disabled sentinel

    @pytest.mark.unit
    def test_auto_execute_conviction_normal(self):
        p = get_auto_execute_params("NORMAL", "CONVICTION")
        assert p.composite_offset == 10
        assert p.sentiment_min == 20
        assert p.extra_unlock is None

    @pytest.mark.unit
    def test_auto_execute_conviction_capitulation_requires_pead(self):
        p = get_auto_execute_params("CAPITULATION", "CONVICTION")
        assert p.composite_offset == 20
        assert p.sentiment_min == 30
        assert p.extra_unlock == "PEAD_EXTENDED_WINDOW"

    @pytest.mark.unit
    def test_auto_execute_choppy_low_disabled(self):
        p = get_auto_execute_params("CHOPPY", "LOW")
        assert p.composite_offset == 999

    @pytest.mark.unit
    def test_auto_execute_opportunity_high_requires_sub_etf(self):
        p = get_auto_execute_params("OPPORTUNITY", "HIGH")
        assert p.composite_offset != 999
        assert p.extra_unlock == "SUB_ETF_CONFIRM_03"

    @pytest.mark.unit
    def test_unknown_regime_falls_back_to_normal(self):
        p = get_filter_params("UNKNOWN_REGIME", "MEDIUM")
        normal_p = get_filter_params("NORMAL", "MEDIUM")
        assert p.composite_min == normal_p.composite_min


# ─── TestDynamicPreClaudeFilter ──────────────────────────────────────────────

class TestDynamicPreClaudeFilter:

    def _make_snap(self, regime="NORMAL"):
        from unittest.mock import MagicMock
        snap = MagicMock()
        snap.regime = regime
        snap.research = {}
        snap.intraday_news = {}
        snap.params = {}
        snap.staleness_guard = MagicMock(auto_execute_allowed=True, composite_penalty=0)
        snap.regime_info = {"macro_health_score": 80}
        snap.research_meta = {"macro_shock_score": 0}
        snap.circuit_breaker_composite_boost = 0
        return snap

    @pytest.mark.unit
    def test_filter_result_carries_quality(self):
        from unittest.mock import MagicMock
        from trading.pre_claude_filter import apply_pre_claude_filter
        from utils.ticker_quality import TickerQuality
        snap = self._make_snap("NORMAL")
        iv = MagicMock()
        iv.composite_score = 55.0
        iv.rsi = 60.0
        iv.relative_volume = 1.2
        iv.vwap_slope = 0.01
        result = apply_pre_claude_filter("AAPL", iv, snap)
        assert hasattr(result, "quality")
        assert isinstance(result.quality, TickerQuality)

    @pytest.mark.unit
    def test_defensive_regime_no_duplicate_gate(self):
        import inspect
        import trading.pre_claude_filter as pcf
        src = inspect.getsource(pcf)
        assert 'regime == "DEFENSIVE"' not in src or "Rule 23" in src

    @pytest.mark.unit
    def test_layer0_skip_does_not_compute_quality(self, monkeypatch):
        """SKIP for already_closed_today must not invoke compute_ticker_quality."""
        from unittest.mock import MagicMock
        import trading.pre_claude_filter as pcf

        quality_calls = []

        def fake_quality(*args, **kwargs):
            quality_calls.append(True)
            from utils.ticker_quality import TickerQuality
            return TickerQuality("MEDIUM", 50, frozenset(), (), {})

        monkeypatch.setattr(pcf, "compute_ticker_quality", fake_quality)

        snap = self._make_snap("NORMAL")
        iv = MagicMock()
        iv.composite_score = 50.0

        result = pcf.apply_pre_claude_filter("AAPL", iv, snap, daily_closed={"AAPL"})
        assert result.decision == "SKIP"
        assert len(quality_calls) == 0, (
            f"compute_ticker_quality was called {len(quality_calls)} times — should be 0 for closed-today SKIP"
        )

    @pytest.mark.unit
    def test_correlation_hard_gate_skips_before_claude(self, monkeypatch):
        """When correlation_hard_gate=True and sector is concentrated, pre-filter returns SKIP."""
        from unittest.mock import MagicMock
        import trading.pre_claude_filter as pcf
        from utils.regime_params import GateParams

        monkeypatch.setattr(pcf, "get_gate_params",
            lambda regime, tier: GateParams(
                stoch_soft_flag=85, stoch_hard_veto=97,
                rel_volume_min=0.6, breadth_weak_threshold=0.85,
                stop_hunt_veto=True, correlation_hard_gate=True,
            ))
        monkeypatch.setattr(pcf, "passes_correlation_guard", lambda symbol, open_pos: False)

        snap = self._make_snap("NORMAL")
        snap.fred_cache = {}
        iv = MagicMock()
        iv.composite_score = 65.0
        iv.rsi = 55.0
        iv.relative_volume = 1.2
        iv.vwap_slope = 0.01
        iv.squeeze_setup = False
        iv.bot_herding_score = 0

        result = pcf.apply_pre_claude_filter(
            "AAPL", iv, snap,
            open_positions={"MSFT": {"sector": "XLK"}},
        )
        assert result.decision == "SKIP"
        assert "sector concentration" in result.reason

    @pytest.mark.unit
    def test_ae_threshold_respects_tier_composite_floor(self, monkeypatch):
        """AE threshold uses max(regime_floor, tier_floor) to prevent premature auto-execute.

        Scenario: fp.composite_min=50, ep.composite_min=30, offset=15, composite=60.
        Old logic: ae_threshold = 30+15 = 45 → 60 > 45 → AUTO_EXECUTE (wrong).
        New logic: ae_threshold = max(30,50)+15 = 65 → 60 < 65 → CLAUDE (correct).
        Layer 1 allows composite=60 because 60 >= fp.composite_min=50.
        """
        from unittest.mock import MagicMock
        import trading.pre_claude_filter as pcf
        from utils.regime_params import FilterParams, AutoExecuteParams, EntryParams

        monkeypatch.setattr(pcf, "get_filter_params",
            lambda regime, tier: FilterParams(composite_min=50, sentiment_hard_block=-50, intraday_block=-50))
        monkeypatch.setattr(pcf, "get_auto_execute_params",
            lambda regime, tier: AutoExecuteParams(composite_offset=15, sentiment_min=-100, extra_unlock=None))
        monkeypatch.setattr(pcf, "get_entry_params",
            lambda regime: EntryParams(composite_min=30, rsi_max=65))

        snap = self._make_snap("NORMAL")
        snap.circuit_breaker_composite_boost = 0
        snap.fred_cache = {}
        # macro health and shock pass layers 3 (health=80 already set in snap)

        iv = MagicMock()
        iv.composite_score = 60.0  # passes Layer1 (≥50); old AE: 30+15=45 → AE; new: max(30,50)+15=65 → CLAUDE
        iv.rsi = 50.0
        iv.relative_volume = 1.2
        iv.vwap_slope = 0.01
        iv.squeeze_setup = False
        iv.bot_herding_score = 0

        result = pcf.apply_pre_claude_filter("AAPL", iv, snap)
        assert result.decision == "CLAUDE", (
            f"Expected CLAUDE (tier floor raises AE bar), got {result.decision}: {result.reason}"
        )


# ── Option A pre_claude_filter quality-only tests ────────────────────────────

def _make_iv_pipeline(**kwargs):
    from indicators.values import IndicatorValues
    defaults = dict(
        symbol="AAPL", timestamp="2026-01-01T10:00:00",
        current_price=100.0, vwap=99.0,
        vwap_std1_upper=100.5, vwap_std1_lower=97.5,
        vwap_std2_upper=101.5, vwap_std2_lower=96.5,
        rsi=60.0, macd=0.1, macd_signal=0.05, macd_hist=0.05,
        bb_upper=102.0, bb_mid=100.0, bb_lower=98.0,
        ema9=100.2, ema21=99.8, atr=1.5, volume_ratio=1.0,
        stoch_k=60.0, stoch_d=60.0, obv=1000.0,
        vwap_slope=0.1, rsi_slope=0.0,
        daily_volume_ratio=1.0, daily_volume_trend=0.0, daily_volume_mean=1_000_000.0,
        composite_score=25.0, signal="BUY", confidence=0.7,
    )
    defaults.update(kwargs)
    return IndicatorValues(**defaults)


def _make_snap_pipeline(regime="NORMAL"):
    from unittest.mock import MagicMock
    from utils.data_health import FRESH_GUARD
    snap = MagicMock()
    snap.regime = regime
    snap.params = {}
    snap.regime_info = {"macro_health_score": 0}
    snap.research_meta = {"macro_shock_score": 0}
    snap.research = {}
    snap.intraday_news = {}
    snap.sector_today = {}
    snap.staleness_guard = FRESH_GUARD
    snap.circuit_breaker_composite_boost = 0
    return snap


@pytest.mark.unit
def test_pre_claude_filter_passes_stoch_95():
    """Option A: Stoch 95 must NOT cause SKIP in apply_pre_claude_filter (only in check_all_gates)."""
    from trading.pre_claude_filter import apply_pre_claude_filter
    # composite=65 → MEDIUM tier → passes composite gate; stoch should not block here
    iv = _make_iv_pipeline(composite_score=65.0, stoch_d=95.0)
    snap = _make_snap_pipeline()
    result = apply_pre_claude_filter("AAPL", iv, snap, set())
    assert result.decision != "SKIP", f"Option A violated: Stoch 95 caused SKIP: {result.reason}"


@pytest.mark.unit
def test_pre_claude_filter_passes_vwap_overextended():
    """Option A: price > VWAP+2σ must NOT cause SKIP in apply_pre_claude_filter."""
    from trading.pre_claude_filter import apply_pre_claude_filter
    # composite=65 → MEDIUM tier → passes composite gate; VWAP overextension should not block here
    iv = _make_iv_pipeline(composite_score=65.0, current_price=104.0, vwap_std2_upper=101.5)
    snap = _make_snap_pipeline()
    result = apply_pre_claude_filter("AAPL", iv, snap, set())
    assert result.decision != "SKIP", f"Option A violated: VWAP overextension caused SKIP: {result.reason}"


@pytest.mark.unit
def test_budget_intraday_warning_fired_at_75pct(monkeypatch, tmp_path):
    import config
    monkeypatch.setattr(config, "API_BUDGET_FILE", tmp_path / "budget.json")
    from utils.io_utils import save_json
    from trading import risk_manager

    from datetime import datetime
    from utils.constants import ET
    entry_ceil = 0.70
    budget = {
        "date": datetime.now(ET).strftime("%Y-%m-%d"), "calls_made": 5, "entry_calls": 5, "exit_calls": 0,
        "input_tokens": 0, "output_tokens": 0,
        "cache_write_tokens": 0, "cache_read_tokens": 0,
        "estimated_cost_usd": 0.536, "entry_spent_usd": 0.536, "exit_spent_usd": 0.0,
        "ceiling_usd": 1.00, "entry_ceiling_usd": entry_ceil, "exit_ceiling_usd": 0.30,
        "ceiling_mode": "normal",
        "entry_exhausted": False, "exit_exhausted": False,
        "budget_exhausted": False, "escalated": False,
    }
    save_json(config.API_BUDGET_FILE, budget)

    warned = []
    monkeypatch.setattr("notifications.telegram.notify_budget_intraday_warning",
                        lambda s, c: warned.append((s, c)))

    risk_manager.check_entry_budget(estimated_cost=0.01, regime="NORMAL")
    assert warned, "Expected budget intraday warning at 76% of entry ceiling"
