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
    assert p.max_days == 10   # raised from 8: longer hold in bull trend captures more alpha
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
