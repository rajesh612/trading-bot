import pytest
from utils.regime_params import (
    get_tp_sl_params, TPSLParams, REGIMES, TIERS, MODES, get_entry_params,
)

ALL_COMBOS     = [(r, t, m) for r in REGIMES for t in TIERS for m in MODES]
ENABLED_COMBOS = [
    (r, t, m) for r, t, m in ALL_COMBOS
    if not get_tp_sl_params(r, t, m).is_disabled
]


@pytest.mark.unit
@pytest.mark.parametrize("regime,tier,mode", ALL_COMBOS)
def test_no_missing_entries(regime, tier, mode):
    p = get_tp_sl_params(regime, tier, mode)
    assert p is not None


@pytest.mark.unit
@pytest.mark.parametrize("regime,tier,mode", ENABLED_COMBOS)
def test_rr_positive(regime, tier, mode):
    p = get_tp_sl_params(regime, tier, mode)
    assert p.tp_floor_pct >= p.sl_floor_pct, (
        f"{regime}/{tier}/{mode}: tp_floor={p.tp_floor_pct} < sl_floor={p.sl_floor_pct}"
    )


@pytest.mark.unit
@pytest.mark.parametrize("regime,tier,mode", ENABLED_COMBOS)
def test_ceiling_above_floor(regime, tier, mode):
    p = get_tp_sl_params(regime, tier, mode)
    assert p.tp_ceiling_pct > p.tp_floor_pct
    assert p.sl_ceiling_pct >= p.sl_floor_pct


@pytest.mark.unit
@pytest.mark.parametrize("tier,mode", [(t, m) for t in TIERS for m in MODES])
def test_crisis_all_disabled(tier, mode):
    assert get_tp_sl_params("CRISIS", tier, mode).is_disabled


@pytest.mark.unit
@pytest.mark.parametrize("mode", MODES)
def test_capitulation_low_medium_disabled(mode):
    assert get_tp_sl_params("CAPITULATION", "LOW",    mode).is_disabled
    assert get_tp_sl_params("CAPITULATION", "MEDIUM", mode).is_disabled


@pytest.mark.unit
def test_swing_max_days_monotone():
    for regime in ("NORMAL", "BULL_TREND", "OPPORTUNITY"):
        days = [
            get_tp_sl_params(regime, t, "SWING").max_days
            for t in TIERS
            if not get_tp_sl_params(regime, t, "SWING").is_disabled
        ]
        assert days == sorted(days), f"{regime}: max_days not monotone with tier"


@pytest.mark.unit
def test_bull_trend_conviction_swing_widest():
    p = get_tp_sl_params("BULL_TREND", "CONVICTION", "SWING")
    assert p.max_days == 12   # raised to 12: longer hold in bull trend captures more alpha
    assert p.tp_ceiling_pct == 12.0


@pytest.mark.unit
def test_fallback_on_unknown_regime():
    p = get_tp_sl_params("UNKNOWN_REGIME", "MEDIUM", "DAY_TRADE")
    assert not p.is_disabled


@pytest.mark.unit
@pytest.mark.parametrize("regime", REGIMES)
def test_entry_params_max_position_pct(regime):
    p = get_entry_params(regime)
    assert 0.0 <= p.max_position_pct <= 0.05


@pytest.mark.unit
def test_swing_params_stripped():
    from utils.regime_params import get_swing_params
    sp = get_swing_params("NORMAL")
    assert hasattr(sp, "eligible")
    assert not hasattr(sp, "tp1_pct")
    assert not hasattr(sp, "profit_lock_pct")


class TestComputeTpSlWithMode:
    @pytest.mark.unit
    def test_mode_param_accepted(self):
        from signals.tp_scaler import compute_tp_sl
        from datetime import datetime
        from utils.constants import ET
        tp, sl = compute_tp_sl(
            base_tp=2.0, base_sl=1.5,
            entry_time=datetime(2026, 5, 24, 10, 30, tzinfo=ET),
            tier="MEDIUM", regime="NORMAL", mode="DAY_TRADE",
        )
        assert 1.5 <= tp <= 4.0   # within NORMAL/MEDIUM/DAY_TRADE TPSLParams bounds
        assert 1.5 <= sl <= 2.0

    @pytest.mark.unit
    def test_swing_mode_uses_swing_table(self):
        from signals.tp_scaler import compute_tp_sl
        from datetime import datetime
        from utils.constants import ET
        tp, sl = compute_tp_sl(
            base_tp=5.0, base_sl=2.0,
            entry_time=datetime(2026, 5, 24, 10, 0, tzinfo=ET),
            tier="CONVICTION", regime="BULL_TREND", mode="SWING",
        )
        # BULL_TREND/CONVICTION/SWING: tp_floor=7.0, tp_ceiling=12.0
        assert 7.0 <= tp <= 12.0

    @pytest.mark.unit
    def test_disabled_combo_returns_fallback(self):
        """Disabled combos return (base_tp, base_sl) instead of raising."""
        from signals.tp_scaler import compute_tp_sl
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime(2024, 1, 15, 11, 0, tzinfo=ZoneInfo("America/New_York"))
        tp, sl = compute_tp_sl(5.0, 2.0, now, tier="MEDIUM", regime="CRISIS")
        assert isinstance(tp, float) and tp > 0
        assert isinstance(sl, float) and sl > 0
