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


@pytest.mark.unit
class TestBacktestSafetyGates:
    def test_stoch_hard_veto_blocks_entry(self):
        """iv.stoch_d above stoch_hard_veto must not produce a pending signal."""
        from utils.regime_params import get_gate_params
        gp = get_gate_params("NORMAL", "MEDIUM")
        iv = _make_iv(composite=60.0, stoch_d=float(gp.stoch_hard_veto) + 1)
        from backtest.simulator import _sim_entry_signal_passes
        from utils.ticker_quality import TickerQuality
        quality = TickerQuality(tier="MEDIUM", score=50, unlocks=frozenset(),
                                quality_gaps=(), component_scores={})
        result = _sim_entry_signal_passes(iv, "NORMAL", quality, {}, rsi_max=74.0)
        assert result is False

    def test_parabolic_blocks_entry(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "PARABOLIC_ENTRY_BLOCK", True)
        iv = _make_iv(composite=60.0, parabolic=True)
        from backtest.simulator import _sim_entry_signal_passes
        from utils.ticker_quality import TickerQuality
        quality = TickerQuality(tier="MEDIUM", score=50, unlocks=frozenset(),
                                quality_gaps=(), component_scores={})
        result = _sim_entry_signal_passes(iv, "NORMAL", quality, {}, rsi_max=74.0)
        assert result is False

    def test_gap_up_skip_blocks_entry(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "GAP_SKIP_PCT", 3.0)
        iv = _make_iv(composite=60.0, prev_close=100.0, current_price=104.0)
        from backtest.simulator import _sim_entry_signal_passes
        from utils.ticker_quality import TickerQuality
        quality = TickerQuality(tier="MEDIUM", score=50, unlocks=frozenset(),
                                quality_gaps=(), component_scores={})
        result = _sim_entry_signal_passes(iv, "NORMAL", quality, {}, rsi_max=74.0)
        assert result is False

    def test_clean_signal_passes_all_gates(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "PARABOLIC_ENTRY_BLOCK", True)
        monkeypatch.setattr(config, "GAP_SKIP_PCT", 3.0)
        iv = _make_iv(composite=60.0, stoch_d=40.0, parabolic=False,
                      prev_close=100.0, current_price=100.5)
        from backtest.simulator import _sim_entry_signal_passes
        from utils.ticker_quality import TickerQuality
        quality = TickerQuality(tier="MEDIUM", score=50, unlocks=frozenset(),
                                quality_gaps=(), component_scores={})
        result = _sim_entry_signal_passes(iv, "NORMAL", quality, {}, rsi_max=74.0)
        assert result is True


@pytest.mark.unit
class TestBacktestTPDecay:
    def _open(self, hour: int, minute: int = 0, tier="MEDIUM", regime="NORMAL"):
        from backtest.simulator import SimState, _open_trade
        state = SimState(cash=100_000.0)
        ts_et = datetime(2026, 1, 2, hour, minute, tzinfo=ET)
        _open_trade(state, "AAPL", str(ts_et), 100.0, 2.0,
                    pos_mult=1.0, composite=55.0, tier=tier,
                    regime=regime, entry_time_et=ts_et)
        return state.positions.get("AAPL")

    def test_morning_entry_full_tp(self):
        """Entry before noon has wider TP than afternoon entry."""
        pos_morning = self._open(10, 30)
        pos_afternoon = self._open(14, 0)
        assert pos_morning is not None and pos_afternoon is not None
        assert pos_morning["tp"] > pos_afternoon["tp"]

    def test_afternoon_entry_tighter_tp(self):
        """Entry at 14:00 ET uses reduced multiplier — TP must be < morning entry."""
        pos_early = self._open(10, 0)
        pos_late = self._open(14, 0)
        assert pos_late["tp"] < pos_early["tp"]

    def test_conviction_tier_widens_tp(self):
        """CONVICTION tier applies wider TP multiplier in NORMAL regime."""
        pos_med = self._open(10, 30, tier="MEDIUM", regime="NORMAL")
        pos_conv = self._open(10, 30, tier="CONVICTION", regime="NORMAL")
        assert pos_conv["tp"] > pos_med["tp"]

    def test_conviction_tightens_sl(self):
        """CONVICTION tier applies tighter SL (higher absolute price = closer to entry)."""
        pos_med = self._open(10, 30, tier="MEDIUM", regime="NORMAL")
        pos_conv = self._open(10, 30, tier="CONVICTION", regime="NORMAL")
        assert pos_conv["sl"] > pos_med["sl"]


@pytest.mark.unit
class TestBacktestTieredSwingTP:
    def _make_swing_pos(self, qty=10, entry=100.0, sl=97.0):
        from trading.tiered_tp import _compute_tier_quantities
        tp1_qty, tp2_qty, trailing_qty = _compute_tier_quantities(qty)
        return {
            "qty": qty, "entry": entry, "entry_price": entry,
            "sl": sl, "tp": round(entry * 1.10, 4),
            "entry_ts": "2026-01-02 10:30:00", "entry_time": "2026-01-02 10:30:00",
            "composite": 60.0, "quality_tier": "MEDIUM", "is_swing": True,
            "swing_days_remaining": 3,
            "tp1_qty": tp1_qty, "tp2_qty": tp2_qty, "trailing_qty": trailing_qty,
            "tp1_hit": False, "tp2_hit": False,
            "trailing_stop_active": False, "trailing_high_water": entry,
            "stop_loss_pct": 3.0, "tp1_pct": 5.0, "tp2_pct": 10.0,
        }

    def test_swing_promotion_initializes_tiered_fields(self):
        """After _init_tiered_swing_fields, position has tp1/tp2/trailing_qty summing to total."""
        from backtest.simulator import SimState, _init_tiered_swing_fields
        state = SimState(cash=100_000.0)
        state.positions["AAPL"] = {
            "qty": 10, "entry": 100.0, "sl": 96.0, "tp": 105.0,
            "entry_ts": "2026-01-02 10:30:00", "entry_time": "2026-01-02 10:30:00",
            "composite": 55.0, "quality_tier": "MEDIUM",
        }
        _init_tiered_swing_fields(state.positions["AAPL"], "NORMAL")
        pos = state.positions["AAPL"]
        assert "tp1_qty" in pos
        assert "tp2_qty" in pos
        assert "trailing_qty" in pos
        assert pos["tp1_qty"] + pos["tp2_qty"] + pos["trailing_qty"] == 10
        assert pos["tp1_hit"] is False

    def test_partial_close_reduces_qty(self):
        """_close_trade with qty < pos['qty'] reduces position size without removing it."""
        from backtest.simulator import SimState, _close_trade
        state = SimState(cash=100_000.0)
        state.positions["AAPL"] = self._make_swing_pos(qty=10, entry=100.0)
        _close_trade(state, "AAPL", "ts", 105.0, "SWING_TP1", "2026-01-02", qty=4)
        assert "AAPL" in state.positions
        assert state.positions["AAPL"]["qty"] == 6
        assert len(state.trades) == 1
        assert state.trades[0].qty == 4
        assert "AAPL" not in state.daily_closed

    def test_full_close_removes_position(self):
        """_close_trade without qty closes entire position."""
        from backtest.simulator import SimState, _close_trade
        state = SimState(cash=100_000.0)
        state.positions["AAPL"] = self._make_swing_pos(qty=10, entry=100.0)
        _close_trade(state, "AAPL", "ts", 105.0, "SWING_TP2", "2026-01-02")
        assert "AAPL" not in state.positions
        assert "AAPL" in state.daily_closed
