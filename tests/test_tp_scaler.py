# tests/test_tp_scaler.py
import pytest
from datetime import datetime
from zoneinfo import ZoneInfo
from signals.tp_scaler import compute_tp_sl, compute_time_multiplier

ET = ZoneInfo("America/New_York")

def _et(hour, minute=0):
    return datetime(2026, 5, 21, hour, minute, tzinfo=ET)

@pytest.mark.unit
class TestTimeMultiplier:
    def test_full_slot_before_1100(self):
        assert compute_time_multiplier(_et(10, 30)) == 0.90

    def test_mid_slot_1100_to_1230(self):
        assert compute_time_multiplier(_et(11, 30)) == 0.75

    def test_late_slot_1230_to_1400(self):
        assert compute_time_multiplier(_et(13, 0)) == 0.60

    def test_tight_slot_after_1400(self):
        assert compute_time_multiplier(_et(14, 31)) == 0.50

@pytest.mark.unit
class TestComputeTpSl:
    def test_normal_medium_returns_clamped_tp(self):
        tp, sl = compute_tp_sl(2.0, 1.5, _et(10, 30), "MEDIUM", "NORMAL")
        # NORMAL/MEDIUM/DAY_TRADE: tp_floor=2.0, tp_ceiling=3.5, sl_floor=1.5, sl_ceiling=2.0, atr_mult=1.8
        assert 2.0 <= tp <= 3.5
        assert 1.5 <= sl <= 2.0

    def test_late_entry_tightens_tp(self):
        tp_early, _ = compute_tp_sl(2.5, 1.5, _et(10, 30), "MEDIUM", "NORMAL")
        tp_late, _  = compute_tp_sl(2.5, 1.5, _et(14, 31), "MEDIUM", "NORMAL")
        assert tp_late <= tp_early

    def test_conviction_widens_tp_ceiling(self):
        p_med = compute_tp_sl(3.0, 1.5, _et(10, 30), "MEDIUM", "NORMAL")
        p_con = compute_tp_sl(3.0, 1.5, _et(10, 30), "CONVICTION", "NORMAL")
        # CONVICTION allows higher ceiling (4.5) vs MEDIUM (3.5)
        assert p_con[0] >= p_med[0]

    def test_crisis_raises(self):
        with pytest.raises(ValueError):
            compute_tp_sl(2.0, 1.5, _et(10, 30), "LOW", "CRISIS")

    def test_sl_clamped_to_floor(self):
        _, sl = compute_tp_sl(2.0, 0.1, _et(10, 30), "MEDIUM", "NORMAL")
        # sl_floor for NORMAL/MEDIUM is 1.5
        assert sl >= 1.5
