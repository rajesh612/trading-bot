"""Concurrency, position sizing, and mode-dispatch tests."""
import pytest
import types


# ── ConcurrencyParams / get_concurrency_params ────────────────────────────────

@pytest.mark.unit
class TestGetConcurrencyParams:

    def test_crisis_max_open_zero_all_tiers(self):
        from utils.regime_params import get_concurrency_params
        for tier in ("LOW", "MEDIUM", "HIGH", "CONVICTION"):
            cp = get_concurrency_params("CRISIS", tier, "DAY_TRADE")
            assert cp.max_open == 0, f"CRISIS tier={tier} should block all entries"

    def test_pead_mode_gets_plus1_slot(self):
        from utils.regime_params import get_concurrency_params
        base = get_concurrency_params("NORMAL", "MEDIUM", "DAY_TRADE")
        pead = get_concurrency_params("NORMAL", "MEDIUM", "PEAD")
        assert pead.max_open == base.max_open + 1

    def test_catalyst_mode_gets_plus1_slot(self):
        from utils.regime_params import get_concurrency_params
        base = get_concurrency_params("BULL_TREND", "HIGH", "DAY_TRADE")
        cat  = get_concurrency_params("BULL_TREND", "HIGH", "CATALYST")
        assert cat.max_open == base.max_open + 1

    def test_swing_mode_exempt_from_heat(self):
        from utils.regime_params import get_concurrency_params
        cp = get_concurrency_params("BULL_TREND", "CONVICTION", "SWING")
        assert cp.max_open == 999
        assert cp.heat_weight == 0.0
        assert cp.swing_health_gate is False

    def test_high_vol_size_mult_at_most_one(self):
        from utils.regime_params import get_concurrency_params
        for tier in ("LOW", "MEDIUM", "HIGH", "CONVICTION"):
            cp = get_concurrency_params("HIGH_VOL", tier, "DAY_TRADE")
            assert cp.size_mult <= 1.0, f"HIGH_VOL tier={tier} size_mult={cp.size_mult} > 1.0"

    def test_defensive_size_mult_at_most_one(self):
        from utils.regime_params import get_concurrency_params
        for tier in ("LOW", "MEDIUM", "HIGH", "CONVICTION"):
            cp = get_concurrency_params("DEFENSIVE", tier, "DAY_TRADE")
            assert cp.size_mult <= 1.0, f"DEFENSIVE tier={tier} size_mult={cp.size_mult} > 1.0"

    def test_unknown_regime_tier_returns_conservative(self):
        from utils.regime_params import get_concurrency_params
        cp = get_concurrency_params("UNKNOWN_REGIME", "LOW", "DAY_TRADE")
        assert cp.max_open == 1
        assert cp.size_mult == 1.0

    def test_fail_closed_on_exception(self, monkeypatch):
        import utils.regime_params as rp
        monkeypatch.setattr(rp, "_CONCURRENCY_TABLE", None)
        from utils.regime_params import get_concurrency_params
        cp = get_concurrency_params("NORMAL", "HIGH", "DAY_TRADE")
        assert cp.max_open == 1

    def test_bull_trend_conviction_max_open_is_5(self):
        from utils.regime_params import get_concurrency_params
        cp = get_concurrency_params("BULL_TREND", "CONVICTION", "DAY_TRADE")
        assert cp.max_open == 5

    def test_bull_trend_conviction_swing_health_gate_false(self):
        from utils.regime_params import get_concurrency_params
        cp = get_concurrency_params("BULL_TREND", "CONVICTION", "DAY_TRADE")
        assert cp.swing_health_gate is False

    def test_defensive_low_max_open_zero(self):
        from utils.regime_params import get_concurrency_params
        cp = get_concurrency_params("DEFENSIVE", "LOW", "DAY_TRADE")
        assert cp.max_open == 0
