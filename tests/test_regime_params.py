"""
Regime params unit tests: get_entry_params, get_swing_params, get_t2_params,
qualifies_for_eod_hold (regime matrix and gate enforcement).
Run with: pytest tests/test_regime_params.py -v -m unit
No API calls — all tests use synthetic fixtures.
"""
import pytest

from utils.constants import ET


# ── utils/regime_params ───────────────────────────────────────────────────────

@pytest.mark.unit
class TestGetEntryParams:
    def test_crisis_has_zero_hard_close_hour(self):
        from utils.regime_params import get_entry_params
        p = get_entry_params("CRISIS")
        assert p.hard_close_hour == 0

    def test_choppy_raises_composite_min(self):
        from utils.regime_params import get_entry_params
        normal = get_entry_params("NORMAL")
        choppy = get_entry_params("CHOPPY")
        assert choppy.composite_min > normal.composite_min

    def test_bull_trend_returns_entry_params(self):
        from utils.regime_params import get_entry_params
        p = get_entry_params("BULL_TREND")
        assert isinstance(p.composite_min, int)
        assert p.hard_close_hour == 14

    def test_unknown_regime_falls_back_to_normal(self):
        from utils.regime_params import get_entry_params
        p = get_entry_params("UNKNOWN_FUTURE_REGIME")
        normal = get_entry_params("NORMAL")
        assert p.composite_min == normal.composite_min


@pytest.mark.unit
class TestGetSwingParams:
    def test_crisis_not_eligible(self):
        from utils.regime_params import get_swing_params
        p = get_swing_params("CRISIS")
        assert p.eligible is False

    def test_choppy_not_eligible(self):
        from utils.regime_params import get_swing_params
        p = get_swing_params("CHOPPY")
        assert p.eligible is False

    def test_defensive_not_eligible(self):
        from utils.regime_params import get_swing_params
        p = get_swing_params("DEFENSIVE")
        assert p.eligible is False

    def test_normal_eligible_with_positive_min_pnl(self):
        from utils.regime_params import get_swing_params, get_tp_sl_params
        sp = get_swing_params("NORMAL")
        assert sp.eligible is True
        p = get_tp_sl_params("NORMAL", "MEDIUM", "SWING")
        assert p.min_pnl_pct > 0

    def test_profit_lock_bypass_above_min_pnl(self):
        from utils.regime_params import get_tp_sl_params
        p = get_tp_sl_params("CHOPPY", "CONVICTION", "SWING")
        assert p.profit_lock_pct > p.min_pnl_pct

    def test_high_vol_swing_ineligible(self):
        """HIGH_VOL must not be eligible for swing promotion (same as CHOPPY/DEFENSIVE)."""
        from utils.regime_params import get_swing_params
        sp = get_swing_params("HIGH_VOL")
        assert sp.eligible is False


@pytest.mark.unit
class TestGetT2Params:
    def test_crisis_has_no_t2(self):
        from utils.regime_params import get_t2_params
        p = get_t2_params("CRISIS")
        assert p.cutoff_hour == 0

    def test_normal_t2_params_well_formed(self):
        from utils.regime_params import get_t2_params
        p = get_t2_params("NORMAL")
        assert p.cutoff_hour > 0

    def test_choppy_t2_more_restrictive_than_normal(self):
        from utils.regime_params import get_t2_params
        choppy = get_t2_params("CHOPPY")
        assert choppy.cutoff_hour == 0  # CHOPPY fully disables T2 — sentinel value


@pytest.mark.unit
class TestQualifiesForEodHold:
    def _call(self, monkeypatch, **kwargs):
        import config
        monkeypatch.setattr(config, "PROFIT_LOCK_SWING_PCT", 2.5)
        monkeypatch.setattr(config, "SWING_PROMOTE_MIN_PNL_PCT", 0.5)
        from trading.eod_holds import qualifies_for_eod_hold
        defaults = dict(
            symbol="AAPL", regime="NORMAL", earnings_tomorrow=False,
            current_pnl_pct=1.0, entry_sector_sentiment=10.0,
            current_sector_sentiment=5.0, consecutive_red_days=0, vix_trend="stable",
            entry_rsi=50.0, entry_composite=65.0,
        )
        defaults.update(kwargs)
        return qualifies_for_eod_hold(**defaults)

    def test_profit_lock_bypass_choppy(self, monkeypatch):
        result, reason = self._call(monkeypatch, regime="CHOPPY", current_pnl_pct=2.5)
        assert result is True
        assert reason == ""

    def test_profit_lock_requires_enough_pnl(self, monkeypatch):
        result, reason = self._call(monkeypatch, regime="CHOPPY", current_pnl_pct=1.0)
        assert result is False
        assert "CHOPPY" in reason or "swing" in reason.lower()

    def test_profit_lock_blocked_in_crisis(self, monkeypatch):
        result, reason = self._call(monkeypatch, regime="CRISIS", current_pnl_pct=5.0)
        assert result is False
        assert "CRISIS" in reason

    def test_min_pnl_gate_blocks_flat_position(self, monkeypatch):
        # NORMAL MEDIUM min_pnl_pct is 0.5; use 0.3 to test below-threshold blocking
        result, reason = self._call(monkeypatch, regime="NORMAL", current_pnl_pct=0.3)
        assert result is False
        assert "insufficient" in reason.lower() or "P&L" in reason

    def test_min_pnl_gate_passes_at_threshold(self, monkeypatch):
        # NORMAL MEDIUM min_pnl_pct is 1.2; 1.5 comfortably passes
        result, reason = self._call(monkeypatch, regime="NORMAL", current_pnl_pct=1.5)
        assert result is True
        assert reason == ""

    @pytest.mark.unit
    def test_earnings_tomorrow_overrides_profit_lock_bypass(self, monkeypatch):
        """earnings_tomorrow=True must veto even a highly profitable position (profit-lock bypass should not fire)."""
        ok, reason = self._call(monkeypatch, regime="CHOPPY", current_pnl_pct=4.0, earnings_tomorrow=True)
        assert not ok
        assert "earnings" in reason.lower()


class TestQualifiesForEodHoldRegimeMatrix:
    """Tests for the regime-aware swing-hold veto matrix."""

    def _call(self, regime="NORMAL", earnings_tomorrow=False, current_pnl_pct=1.0,
              entry_sector_sentiment=20.0, current_sector_sentiment=15.0,
              consecutive_red_days=0, vix_trend="stable", entry_rsi=50.0, entry_composite=65.0):
        from trading.eod_holds import qualifies_for_eod_hold
        return qualifies_for_eod_hold(
            symbol="TEST", regime=regime, earnings_tomorrow=earnings_tomorrow,
            current_pnl_pct=current_pnl_pct,
            entry_sector_sentiment=entry_sector_sentiment,
            current_sector_sentiment=current_sector_sentiment,
            consecutive_red_days=consecutive_red_days,
            vix_trend=vix_trend,
            entry_rsi=entry_rsi,
            entry_composite=entry_composite,
        )

    @pytest.mark.unit
    def test_crisis_always_vetoes(self):
        ok, reason = self._call(regime="CRISIS", current_pnl_pct=5.0)
        assert not ok
        assert "CRISIS" in reason

    @pytest.mark.unit
    def test_earnings_tomorrow_vetoes_all_regimes(self):
        for regime in ("NORMAL", "BULL_TREND", "OPPORTUNITY", "CRASH_RECOVERY"):
            ok, reason = self._call(regime=regime, earnings_tomorrow=True)
            assert not ok, f"{regime} should veto on earnings tomorrow"
            assert "earnings" in reason.lower()

    @pytest.mark.unit
    def test_choppy_blocks_new_promotions(self):
        ok, reason = self._call(regime="CHOPPY", current_pnl_pct=2.0)
        assert not ok
        assert "CHOPPY" in reason

    @pytest.mark.unit
    def test_defensive_blocks_new_promotions(self):
        ok, reason = self._call(regime="DEFENSIVE", current_pnl_pct=2.0)
        assert not ok
        assert "DEFENSIVE" in reason

    @pytest.mark.unit
    def test_normal_min_pnl_gate_blocks_below_threshold(self):
        # NORMAL MEDIUM min_pnl_pct is 0.5; use 0.3 to test below-threshold blocking
        ok, reason = self._call(regime="NORMAL", current_pnl_pct=0.3)
        assert not ok
        assert "insufficient" in reason.lower() or "P&L" in reason

    @pytest.mark.unit
    def test_normal_passes_at_min_pnl_threshold(self):
        # NORMAL MEDIUM min_pnl_pct is 1.2; 1.5% comfortably passes
        ok, _ = self._call(regime="NORMAL", current_pnl_pct=1.5)
        assert ok

    @pytest.mark.unit
    def test_normal_sector_reversal_vetoes(self):
        """Sector reversed: current < -20 AND entry was > +10."""
        # Use 1.3% (between min_pnl=1.2 and profit_lock=1.5) to test sector veto before profit-lock bypass
        ok, reason = self._call(
            regime="NORMAL", entry_sector_sentiment=25.0, current_sector_sentiment=-25.0, current_pnl_pct=1.3
        )
        assert not ok
        assert "sector" in reason.lower()

    @pytest.mark.unit
    def test_normal_sector_soft_drop_does_not_veto(self):
        """Sector dropped but not reversed — no veto."""
        # Use 1.3% (between min_pnl=1.2 and profit_lock=1.5) to test sector gates
        ok, _ = self._call(
            regime="NORMAL", entry_sector_sentiment=15.0, current_sector_sentiment=5.0, current_pnl_pct=1.3
        )
        assert ok

    @pytest.mark.unit
    def test_bull_trend_relaxed_min_pnl_threshold(self):
        # BULL_TREND MEDIUM min_pnl_pct is 0.5; 0.6 passes, 0.4 blocks
        ok_at_0_6, _ = self._call(regime="BULL_TREND", current_pnl_pct=0.6)
        ok_at_0_4, _ = self._call(regime="BULL_TREND", current_pnl_pct=0.4)
        assert ok_at_0_6
        assert not ok_at_0_4

    @pytest.mark.unit
    def test_opportunity_vetoes_when_sector_goes_negative(self):
        ok, reason = self._call(
            regime="OPPORTUNITY", entry_sector_sentiment=15.0, current_sector_sentiment=-5.0
        )
        assert not ok
        assert "sector" in reason.lower()

    @pytest.mark.unit
    def test_opportunity_passes_when_sector_still_positive(self):
        ok, _ = self._call(
            regime="OPPORTUNITY", entry_sector_sentiment=15.0, current_sector_sentiment=5.0
        )
        assert ok

    @pytest.mark.unit
    def test_capitulation_wider_ptj_threshold(self):
        """CAPITULATION/HIGH swing allows down to -5% P&L (min_pnl_pct=-5.0 in table)."""
        from utils.ticker_quality import TickerQuality
        from trading.eod_holds import qualifies_for_eod_hold
        high_q = TickerQuality(tier="HIGH", score=70, unlocks=frozenset(),
                               quality_gaps=(), component_scores={})
        ok_at_minus_4_9, _ = qualifies_for_eod_hold(
            symbol="TEST", regime="CAPITULATION", earnings_tomorrow=False,
            current_pnl_pct=-4.9, quality=high_q,
            entry_sector_sentiment=20.0, current_sector_sentiment=15.0,
            consecutive_red_days=0, vix_trend="stable",
            entry_rsi=50.0, entry_composite=65.0,
        )
        ok_at_minus_5_1, _ = qualifies_for_eod_hold(
            symbol="TEST", regime="CAPITULATION", earnings_tomorrow=False,
            current_pnl_pct=-5.1, quality=high_q,
            entry_sector_sentiment=20.0, current_sector_sentiment=15.0,
            consecutive_red_days=0, vix_trend="stable",
            entry_rsi=50.0, entry_composite=65.0,
        )
        assert ok_at_minus_4_9
        assert not ok_at_minus_5_1

    @pytest.mark.unit
    def test_crash_recovery_vetoes_on_3_red_days(self):
        """3 consecutive red days blocks CRASH_RECOVERY swing even with sufficient P&L."""
        ok_2_red, _ = self._call(regime="CRASH_RECOVERY", consecutive_red_days=2, current_pnl_pct=1.1)
        ok_3_red, reason = self._call(regime="CRASH_RECOVERY", consecutive_red_days=3, current_pnl_pct=1.1)
        assert ok_2_red
        assert not ok_3_red
        assert "red" in reason.lower()

    @pytest.mark.unit
    def test_non_tier1_no_longer_vetoes(self):
        """is_tier1 gate removed — non-tier1 symbols now qualify in NORMAL."""
        ok, _ = self._call(regime="NORMAL", current_pnl_pct=1.5)
        assert ok

    @pytest.mark.unit
    def test_event_risk_tomorrow_no_longer_vetoes(self):
        """event_risk_tomorrow gate removed — only earnings_tomorrow matters."""
        ok, _ = self._call(regime="NORMAL", earnings_tomorrow=False, current_pnl_pct=1.5)
        assert ok

    @pytest.mark.unit
    def test_profit_lock_bypass_blocked_below_composite_floor(self):
        """Even with profit-lock P&L, entry composite below SWING_PROFIT_LOCK_MIN_COMPOSITE blocks swing."""
        import config
        from trading.eod_holds import qualifies_for_eod_hold
        ok, reason = qualifies_for_eod_hold(
            symbol="TEST", regime="NORMAL", earnings_tomorrow=False,
            current_pnl_pct=3.0,
            entry_sector_sentiment=20.0, current_sector_sentiment=15.0,
            consecutive_red_days=0, vix_trend="stable",
            entry_rsi=50.0, entry_composite=55.0,
        )
        assert not ok
        assert "composite" in reason.lower()

    @pytest.mark.unit
    def test_profit_lock_bypass_passes_above_composite_floor(self):
        """Profit-lock P&L with composite >= SWING_PROFIT_LOCK_MIN_COMPOSITE passes."""
        from trading.eod_holds import qualifies_for_eod_hold
        ok, _ = qualifies_for_eod_hold(
            symbol="TEST", regime="NORMAL", earnings_tomorrow=False,
            current_pnl_pct=3.0,
            entry_sector_sentiment=20.0, current_sector_sentiment=15.0,
            consecutive_red_days=0, vix_trend="stable",
            entry_rsi=50.0, entry_composite=65.0,
        )
        assert ok

    @pytest.mark.unit
    def test_normal_medium_composite_gate_raised_to_60(self):
        """NORMAL/MEDIUM requires composite >= 60 for standard swing promotion."""
        from trading.eod_holds import qualifies_for_eod_hold
        from utils.ticker_quality import TickerQuality
        med_q = TickerQuality(tier="MEDIUM", score=50, unlocks=frozenset(),
                              quality_gaps=(), component_scores={})
        # Use pnl below profit_lock (1.5) so only the composite gate fires
        ok_59, reason = qualifies_for_eod_hold(
            symbol="TEST", regime="NORMAL", earnings_tomorrow=False,
            current_pnl_pct=1.4, quality=med_q,
            entry_sector_sentiment=20.0, current_sector_sentiment=15.0,
            consecutive_red_days=0, vix_trend="stable",
            entry_rsi=50.0, entry_composite=59.0,
        )
        assert not ok_59, f"composite=59 should be blocked for NORMAL/MEDIUM, got: {reason}"
        ok_60, _ = qualifies_for_eod_hold(
            symbol="TEST", regime="NORMAL", earnings_tomorrow=False,
            current_pnl_pct=1.4, quality=med_q,
            entry_sector_sentiment=20.0, current_sector_sentiment=15.0,
            consecutive_red_days=0, vix_trend="stable",
            entry_rsi=50.0, entry_composite=60.0,
        )
        assert ok_60


@pytest.mark.unit
class TestQualifiesForEodHoldGate:
    """qualifies_for_eod_hold must enforce regime×tier composite_min and rsi_ceiling."""

    def _call(self, **overrides):
        from types import SimpleNamespace
        from trading.eod_holds import qualifies_for_eod_hold
        defaults = dict(
            symbol="AAPL",
            regime="NORMAL",
            earnings_tomorrow=False,
            current_pnl_pct=2.0,        # above min_pnl (1.2%), below profit_lock
            entry_sector_sentiment=10.0,
            current_sector_sentiment=5.0,
            consecutive_red_days=0,
            vix_trend="stable",
            quality=SimpleNamespace(tier="MEDIUM"),
            entry_rsi=65.0,             # default: within NORMAL MEDIUM ceiling (70)
            entry_composite=62.0,       # default: above NORMAL MEDIUM min (60)
        )
        defaults.update(overrides)
        return qualifies_for_eod_hold(**defaults)

    def test_rsi_at_ceiling_blocks_promotion(self):
        ok, reason = self._call(entry_rsi=70.0)   # == ceiling, not <
        assert not ok
        assert "RSI" in reason

    def test_rsi_below_ceiling_allows_promotion(self):
        ok, _ = self._call(entry_rsi=69.0)
        assert ok

    def test_composite_below_min_blocks_promotion(self):
        ok, reason = self._call(entry_composite=59.0)   # < 60 (NORMAL MEDIUM min)
        assert not ok
        assert "composite" in reason

    def test_profit_lock_bypass_blocked_if_composite_below_floor(self):
        """Strongly profitable positions are still blocked if composite is below the profit-lock floor."""
        ok, reason = self._call(
            current_pnl_pct=10.0,   # well above any profit_lock threshold
            entry_rsi=85.0,          # would fail RSI gate
            entry_composite=40.0,    # below SWING_PROFIT_LOCK_MIN_COMPOSITE (62)
        )
        assert not ok
        assert "composite" in reason.lower()
