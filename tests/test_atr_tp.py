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
def test_compute_atr_tp_pct_floor():
    """Very low ATR → clamped to hardcoded floor of 3.0."""
    from signals.atr_tp import compute_atr_tp_pct
    result = compute_atr_tp_pct(atr=0.05, price=100.0, multiplier=2.0)
    assert result == 3.0


@pytest.mark.unit
def test_compute_atr_tp_pct_ceiling():
    """Very high ATR → clamped to hardcoded ceiling of 12.0."""
    from signals.atr_tp import compute_atr_tp_pct
    result = compute_atr_tp_pct(atr=10.0, price=50.0, multiplier=2.0)
    assert result == 12.0


@pytest.mark.unit
def test_compute_atr_tp_pct_zero_price():
    """Zero price must not raise ZeroDivisionError — returns floor (3.0)."""
    from signals.atr_tp import compute_atr_tp_pct
    result = compute_atr_tp_pct(atr=1.0, price=0.0, multiplier=2.0)
    assert result == 3.0


@pytest.mark.unit
def test_compute_atr_tp_pct_higher_multiplier_ordering():
    """Higher multiplier produces strictly wider TP than lower multiplier."""
    from signals.atr_tp import compute_atr_tp_pct
    # Use ATR=2.0 (atr_pct=2%) so 2x=4% and 3x=6% are both above floor=3%
    result_2x = compute_atr_tp_pct(atr=2.0, price=100.0, multiplier=2.0)
    result_3x = compute_atr_tp_pct(atr=2.0, price=100.0, multiplier=3.0)
    assert result_3x > result_2x


@pytest.mark.unit
def test_compute_atr_tp_pct_multiplier_scaling_2x():
    """TP% scales linearly with multiplier: 2x multiplier (atr=2%, price=100 → tp=4%)."""
    from signals.atr_tp import compute_atr_tp_pct
    # Use atr=2.0 so atr_pct=2%, then 2.0 × 2.0 = 4.0% (above floor)
    assert abs(compute_atr_tp_pct(atr=2.0, price=100.0, multiplier=2.0) - 4.0) < 0.001


@pytest.mark.unit
def test_compute_atr_tp_pct_multiplier_scaling_3x():
    """TP% scales linearly with multiplier: 3x multiplier (atr=1%, price=100 → tp=3%)."""
    from signals.atr_tp import compute_atr_tp_pct
    assert abs(compute_atr_tp_pct(atr=1.0, price=100.0, multiplier=3.0) - 3.0) < 0.001


@pytest.mark.unit
def test_simulator_open_trade_uses_atr_tp():
    """_open_trade uses ATR-based TP routed through compute_tp_sl (not flat TAKE_PROFIT_PCT)."""
    from backtest.simulator import _open_trade, SimState, SLIPPAGE_PCT
    from signals.atr_tp import compute_atr_tp_pct
    from signals.tp_scaler import compute_tp_sl
    from datetime import datetime
    from zoneinfo import ZoneInfo

    state = SimState(atr_tp_multiplier=2.0)
    entry_bar_open = 50.0
    atr = 1.75
    ts_et = datetime(2026, 5, 17, 10, 0, 0, tzinfo=ZoneInfo("America/New_York"))

    _open_trade(state, "TEST", ts_et.isoformat(), entry_bar_open, atr, pos_mult=1.0,
                composite=25.0, tier="MEDIUM", regime="NORMAL", entry_time_et=ts_et)

    pos = state.positions["TEST"]
    entry = entry_bar_open * (1 + SLIPPAGE_PCT)
    base_tp_pct = compute_atr_tp_pct(atr=atr, price=entry, multiplier=2.0)
    import config as _cfg
    expected_tp_pct, _ = compute_tp_sl(base_tp_pct, _cfg.STOP_LOSS_PCT, ts_et, "MEDIUM", "NORMAL")
    expected_tp = round(entry * (1 + expected_tp_pct / 100), 4)
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
    expected_entry = 50.025
    flat_tp = round(expected_entry * (1 + 5.0 / 100), 4)
    assert pos["tp"] != flat_tp


@pytest.mark.unit
def test_compute_universe_benchmark_basic():
    """Equal-weight buy-open/sell-close over two days compounds correctly."""
    import pandas as pd
    from backtest.report import _compute_universe_benchmark

    daily_bars = {
        "AAA": pd.DataFrame(
            {"open": [100.0, 103.0], "close": [102.0, 106.09]},
            index=pd.DatetimeIndex([
                pd.Timestamp("2026-05-01", tz="UTC"),
                pd.Timestamp("2026-05-02", tz="UTC"),
            ]),
        ),
        "BBB": pd.DataFrame(
            {"open": [50.0, 50.0], "close": [51.0, 51.5]},
            index=pd.DatetimeIndex([
                pd.Timestamp("2026-05-01", tz="UTC"),
                pd.Timestamp("2026-05-02", tz="UTC"),
            ]),
        ),
    }
    day_watch_map = {
        "2026-05-01": ["AAA", "BBB"],
        "2026-05-02": ["AAA", "BBB"],
    }
    result = _compute_universe_benchmark(day_watch_map, daily_bars)
    assert abs(result - 5.06) < 0.05


@pytest.mark.unit
def test_compute_universe_benchmark_missing_symbol():
    """Symbol in watch map but absent from daily_bars is silently skipped."""
    import pandas as pd
    from backtest.report import _compute_universe_benchmark

    daily_bars = {
        "AAA": pd.DataFrame(
            {"open": [100.0], "close": [102.0]},
            index=pd.DatetimeIndex([pd.Timestamp("2026-05-01", tz="UTC")]),
        )
    }
    day_watch_map = {"2026-05-01": ["AAA", "MISSING"]}
    result = _compute_universe_benchmark(day_watch_map, daily_bars)
    assert abs(result - 2.0) < 0.1
