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
def test_compute_atr_tp_pct_higher_multiplier_ordering():
    """Higher multiplier produces strictly wider TP than lower multiplier."""
    from signals.atr_tp import compute_atr_tp_pct
    result_2x = compute_atr_tp_pct(atr=1.0, price=100.0, multiplier=2.0)
    result_3x = compute_atr_tp_pct(atr=1.0, price=100.0, multiplier=3.0)
    assert result_3x > result_2x


@pytest.mark.unit
def test_compute_atr_tp_pct_multiplier_scaling():
    """TP% scales linearly with multiplier (atr=1%, price=100 → tp=mult%)."""
    from signals.atr_tp import compute_atr_tp_pct
    assert abs(compute_atr_tp_pct(atr=1.0, price=100.0, multiplier=2.0) - 2.0) < 0.001
    assert abs(compute_atr_tp_pct(atr=1.0, price=100.0, multiplier=3.0) - 3.0) < 0.001


@pytest.mark.unit
def test_simulator_open_trade_uses_atr_tp():
    """_open_trade uses ATR-based TP, not flat TAKE_PROFIT_PCT."""
    from backtest.simulator import _open_trade, SimState
    from datetime import datetime
    from zoneinfo import ZoneInfo

    state = SimState(atr_tp_multiplier=2.0)
    entry_bar_open = 50.0
    atr = 1.75
    ts = datetime(2026, 5, 17, 10, 0, 0, tzinfo=ZoneInfo("America/New_York")).isoformat()

    _open_trade(state, "TEST", ts, entry_bar_open, atr, pos_mult=1.0, composite=25.0)

    assert "TEST" in state.positions
    pos = state.positions["TEST"]
    expected_tp_pct = 7.0
    expected_entry = 50.025
    expected_tp = round(expected_entry * (1 + expected_tp_pct / 100), 4)
    assert abs(pos["tp"] - expected_tp) < 0.01


@pytest.mark.unit
def test_simulator_open_trade_flat_tp_not_used():
    """_open_trade TP differs from flat 5% baseline."""
    from backtest.simulator import _open_trade, SimState
    from datetime import datetime
    from zoneinfo import ZoneInfo

    state = SimState(atr_tp_multiplier=2.0)
    entry_bar_open = 50.0
    atr = 1.75
    ts = datetime(2026, 5, 17, 10, 0, 0, tzinfo=ZoneInfo("America/New_York")).isoformat()

    _open_trade(state, "TEST", ts, entry_bar_open, atr, pos_mult=1.0, composite=25.0)

    pos = state.positions["TEST"]
    expected_entry = 50.0005
    flat_tp = round(expected_entry * (1 + 5.0 / 100), 4)
    assert pos["tp"] != flat_tp
