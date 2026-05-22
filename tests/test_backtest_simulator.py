"""
Tests for backtest/simulator.py — quality tier integration.
"""
import pytest
from unittest.mock import MagicMock
from datetime import datetime
from utils.constants import ET


def _make_iv(composite=55.0, rsi=50.0, vwap_slope=0.1, stoch_d=40.0,
             parabolic=False, relative_volume=1.0, prev_close=0.0,
             current_price=100.0, signal="BUY", atr=2.0):
    iv = MagicMock()
    iv.composite_score = composite
    iv.rsi = rsi
    iv.vwap_slope = vwap_slope
    iv.stoch_d = stoch_d
    iv.parabolic = parabolic
    iv.relative_volume = relative_volume
    iv.prev_close = prev_close
    iv.current_price = current_price
    iv.signal = signal
    iv.atr = atr
    iv.squeeze_setup = False
    iv.bot_herding_score = 0
    iv.stop_hunt_zone = False
    iv.or_position = "outside"
    iv.momentum_exhaustion = False
    return iv


@pytest.mark.unit
class TestBacktestQuality:
    def test_quality_map_built_for_watch_list(self):
        """_build_quality_map returns a TickerQuality for every symbol that has a precomp iv."""
        from backtest.simulator import _build_quality_map, _BacktestSnap
        iv = _make_iv(composite=60.0)
        precomp = {"AAPL": {datetime(2026, 1, 2, 10, 0, tzinfo=ET): iv}}
        snap = _BacktestSnap(regime="NORMAL")
        qmap = _build_quality_map(["AAPL", "MISSING"], precomp, snap)
        assert "AAPL" in qmap
        assert qmap["AAPL"].tier in ("LOW", "MEDIUM", "HIGH", "CONVICTION")

    def test_quality_map_skips_symbol_with_no_precomp(self):
        from backtest.simulator import _build_quality_map, _BacktestSnap
        snap = _BacktestSnap(regime="NORMAL")
        qmap = _build_quality_map(["MISSING"], {}, snap)
        assert "MISSING" not in qmap

    def test_open_trade_stores_quality_tier(self, monkeypatch, tmp_path):
        """Position dict must contain 'quality_tier' after _open_trade."""
        import config
        monkeypatch.setattr(config, "STOP_LOSS_PCT", 4.0)
        from backtest.simulator import SimState, _open_trade
        state = SimState(cash=100_000.0)
        ts_et = datetime(2026, 1, 2, 10, 30, tzinfo=ET)
        _open_trade(state, "AAPL", "2026-01-02 10:30:00", 100.0, 2.0,
                    pos_mult=1.0, composite=55.0, tier="HIGH",
                    regime="NORMAL", entry_time_et=ts_et)
        assert "AAPL" in state.positions
        assert state.positions["AAPL"]["quality_tier"] == "HIGH"
