"""Unit tests for signals/atr_tp.py — compute_atr_tp_pct pure function."""
import pytest
import config


@pytest.mark.unit
def test_compute_atr_tp_pct_normal():
    """Standard case: atr=1.75, price=50 → atr_pct=3.5%, mult=2.0 → tp=7.0%."""
    from signals.atr_tp import compute_atr_tp_pct
    result = compute_atr_tp_pct(atr=1.75, price=50.0, multiplier=2.0)
    assert abs(result - 7.0) < 0.001


@pytest.mark.unit
def test_compute_atr_tp_pct_floor(monkeypatch):
    """Very low ATR → clamped to ATR_TP_FLOOR_PCT."""
    monkeypatch.setattr(config, "ATR_TP_FLOOR_PCT", 1.5)
    from signals.atr_tp import compute_atr_tp_pct
    result = compute_atr_tp_pct(atr=0.05, price=100.0, multiplier=2.0)
    assert result == 1.5


@pytest.mark.unit
def test_compute_atr_tp_pct_ceiling(monkeypatch):
    """Very high ATR → clamped to ATR_TP_CEILING_PCT."""
    monkeypatch.setattr(config, "ATR_TP_CEILING_PCT", 12.0)
    from signals.atr_tp import compute_atr_tp_pct
    result = compute_atr_tp_pct(atr=10.0, price=50.0, multiplier=2.0)
    assert result == 12.0


@pytest.mark.unit
def test_compute_atr_tp_pct_zero_price():
    """Zero price must not raise ZeroDivisionError — returns floor."""
    from signals.atr_tp import compute_atr_tp_pct
    result = compute_atr_tp_pct(atr=1.0, price=0.0, multiplier=2.0)
    assert result == config.ATR_TP_FLOOR_PCT


@pytest.mark.unit
def test_compute_atr_tp_pct_higher_multiplier():
    """Multiplier 3.0 produces proportionally wider TP."""
    from signals.atr_tp import compute_atr_tp_pct
    result_2x = compute_atr_tp_pct(atr=1.0, price=100.0, multiplier=2.0)
    result_3x = compute_atr_tp_pct(atr=1.0, price=100.0, multiplier=3.0)
    assert result_3x > result_2x
    assert abs(result_3x - 3.0) < 0.001
    assert abs(result_2x - 2.0) < 0.001
