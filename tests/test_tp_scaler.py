# tests/test_tp_scaler.py
import pytest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import config
from signals.tp_scaler import compute_tp_sl, compute_time_multiplier, TimeSlot

ET = ZoneInfo("America/New_York")

def _et(hour, minute=0):
    return datetime(2026, 5, 21, hour, minute, tzinfo=ET)

@pytest.mark.unit
class TestTimeMultiplier:
    def test_full_slot_before_noon(self):
        assert compute_time_multiplier(_et(10, 30)) == 1.00

    def test_late_slot_noon_to_1330(self):
        assert compute_time_multiplier(_et(12, 30)) == 0.80

    def test_tight_slot_1330_to_1430(self):
        assert compute_time_multiplier(_et(14, 0)) == 0.65

    def test_very_tight_after_1430(self):
        assert compute_time_multiplier(_et(14, 31)) == 0.50

@pytest.mark.unit
class TestComputeTpSl:
    def test_normal_medium_full_slot(self, monkeypatch):
        monkeypatch.setattr(config, "ATR_TP_FLOOR_PCT", 3.0)
        monkeypatch.setattr(config, "ATR_TP_CEILING_PCT", 12.0)
        tp, sl = compute_tp_sl(5.0, 3.0, _et(10, 30), "MEDIUM", "NORMAL")
        assert tp == pytest.approx(5.0)   # no tier mult for MEDIUM
        assert sl == pytest.approx(3.0)

    def test_conviction_normal_full_slot_widens_tp(self, monkeypatch):
        monkeypatch.setattr(config, "ATR_TP_FLOOR_PCT", 3.0)
        monkeypatch.setattr(config, "ATR_TP_CEILING_PCT", 12.0)
        tp, sl = compute_tp_sl(5.0, 3.0, _et(10, 30), "CONVICTION", "NORMAL")
        assert tp == pytest.approx(5.0 * 1.20)
        assert sl == pytest.approx(3.0 * 0.90)

    def test_late_entry_tightens_tp(self, monkeypatch):
        monkeypatch.setattr(config, "ATR_TP_FLOOR_PCT", 3.0)
        monkeypatch.setattr(config, "ATR_TP_CEILING_PCT", 12.0)
        tp, sl = compute_tp_sl(5.0, 3.0, _et(13, 0), "MEDIUM", "NORMAL")
        assert tp == pytest.approx(5.0 * 0.80)

    def test_tp_clamped_to_floor(self, monkeypatch):
        monkeypatch.setattr(config, "ATR_TP_FLOOR_PCT", 3.0)
        monkeypatch.setattr(config, "ATR_TP_CEILING_PCT", 12.0)
        tp, sl = compute_tp_sl(3.5, 3.0, _et(14, 31), "LOW", "CHOPPY")
        assert tp == pytest.approx(3.0)   # 3.5 * 0.50 = 1.75 -> clamped to floor

    def test_tier_mult_not_applied_in_choppy(self, monkeypatch):
        monkeypatch.setattr(config, "ATR_TP_FLOOR_PCT", 3.0)
        monkeypatch.setattr(config, "ATR_TP_CEILING_PCT", 12.0)
        tp, sl = compute_tp_sl(5.0, 3.0, _et(10, 30), "CONVICTION", "CHOPPY")
        assert tp == pytest.approx(5.0)   # no tier mult in CHOPPY
