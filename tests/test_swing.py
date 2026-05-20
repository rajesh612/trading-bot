"""
Swing manager unit tests.
Run with: pytest tests/test_swing.py -v -m unit
No API calls — all tests use synthetic fixtures.
"""
import json
import os
from datetime import datetime

import pytest

from utils.constants import ET


# ── swing_manager: _execute_partial_close ────────────────────────────────────

@pytest.mark.unit
class TestExecutePartialClose:
    class _MockAlpaca:
        def __init__(self):
            self.orders: list[tuple] = []

        def place_market_order(self, symbol: str, qty: int, side: str) -> None:
            self.orders.append((symbol, qty, side))

    def _write_swing_file(self, tmp_path, monkeypatch, symbol: str = "NVDA", **overrides) -> None:
        import config
        swing_file = tmp_path / "swing_positions.json"
        monkeypatch.setattr(config, "SWING_POSITIONS_FILE", swing_file)
        pos = {
            "symbol": symbol, "entry_price": 100.0, "qty": 10,
            "tp1_qty": 4, "tp2_qty": 3, "trailing_qty": 3,
            "tp1_hit": False, "tp2_hit": False,
            "trailing_stop_active": False, "trailing_high_water": 100.0,
            "stop_loss_pct": 7.0,
        }
        pos.update(overrides)
        with open(swing_file, "w") as f:
            json.dump({symbol: pos}, f)

    def test_close_tp1_marks_hit_keeps_position(self, tmp_path, monkeypatch):
        from trading.swing_manager import _execute_partial_close, load_swing_positions
        self._write_swing_file(tmp_path, monkeypatch)
        mock = self._MockAlpaca()
        _execute_partial_close("NVDA", "CLOSE_TP1", 4, mock, 105.0)
        swings = load_swing_positions()
        assert "NVDA" in swings
        assert swings["NVDA"]["tp1_hit"] is True
        assert ("NVDA", 4, "SELL") in mock.orders

    def test_close_tp2_activates_trailing_keeps_position(self, tmp_path, monkeypatch):
        from trading.swing_manager import _execute_partial_close, load_swing_positions
        self._write_swing_file(tmp_path, monkeypatch, tp1_hit=True)
        mock = self._MockAlpaca()
        _execute_partial_close("NVDA", "CLOSE_TP2", 3, mock, 112.0)
        swings = load_swing_positions()
        assert "NVDA" in swings
        assert swings["NVDA"]["tp2_hit"] is True
        assert swings["NVDA"]["trailing_stop_active"] is True
        assert swings["NVDA"]["trailing_high_water"] == 112.0

    def test_close_trailing_removes_position(self, tmp_path, monkeypatch):
        from trading.swing_manager import _execute_partial_close, load_swing_positions
        self._write_swing_file(tmp_path, monkeypatch, tp1_hit=True, tp2_hit=True)
        mock = self._MockAlpaca()
        _execute_partial_close("NVDA", "CLOSE_TRAILING", 3, mock, 108.0)
        swings = load_swing_positions()
        assert "NVDA" not in swings
        assert ("NVDA", 3, "SELL") in mock.orders

    def test_close_tp1_removes_fully_closed_one_share_position(self, tmp_path, monkeypatch):
        from trading.swing_manager import _execute_partial_close, load_swing_positions
        # 1-share position: after TP1, nothing remains → position removed
        self._write_swing_file(tmp_path, monkeypatch, qty=1, tp1_qty=1, tp2_qty=0, trailing_qty=0)
        mock = self._MockAlpaca()
        _execute_partial_close("NVDA", "CLOSE_TP1", 1, mock, 105.0)
        swings = load_swing_positions()
        assert "NVDA" not in swings


# ── swing_manager: tier quantities + tiered TP ───────────────────────────────

@pytest.mark.unit
class TestSwingManager:
    def test_tier_quantities_one_share(self):
        from trading.swing_manager import _compute_tier_quantities
        assert _compute_tier_quantities(1) == (1, 0, 0)

    def test_tier_quantities_two_shares(self):
        from trading.swing_manager import _compute_tier_quantities
        assert _compute_tier_quantities(2) == (1, 1, 0)

    def test_tier_quantities_ten_shares(self):
        from trading.swing_manager import _compute_tier_quantities
        tp1, tp2, trailing = _compute_tier_quantities(10)
        assert tp1 + tp2 + trailing == 10
        assert tp1 == 4
        assert tp2 == 3
        assert trailing == 3

    def test_tier_quantities_sum_always_equals_total(self):
        from trading.swing_manager import _compute_tier_quantities
        for n in range(1, 20):
            tp1, tp2, trailing = _compute_tier_quantities(n)
            assert tp1 + tp2 + trailing == n, f"split fails for n={n}"

    def _make_position(self, **overrides):
        base = {
            "entry_price": 100.0,
            "qty": 10,
            "stop_loss_pct": 7.0,
            "tp1_pct": 5.0,
            "tp2_pct": 10.0,
            "tp1_qty": 4,
            "tp2_qty": 3,
            "trailing_qty": 3,
            "tp1_hit": False,
            "tp2_hit": False,
            "trailing_stop_active": False,
            "trailing_high_water": 100.0,
        }
        return {**base, **overrides}

    def test_tiered_tp_hard_stop(self):
        from trading.swing_manager import evaluate_tiered_tp_action
        pos = self._make_position()
        result = evaluate_tiered_tp_action(pos, current_price=92.0)  # -8% vs 7% stop
        assert result["action"] == "CLOSE_HARD_STOP"
        assert result["qty"] == 10  # all remaining

    def test_tiered_tp_tp1(self):
        from trading.swing_manager import evaluate_tiered_tp_action
        pos = self._make_position()
        result = evaluate_tiered_tp_action(pos, current_price=106.0)  # +6% vs 5% tp1
        assert result["action"] == "CLOSE_TP1"
        assert result["qty"] == 4
        assert result["activate_trailing"] is False

    def test_tiered_tp_tp2(self):
        from trading.swing_manager import evaluate_tiered_tp_action
        pos = self._make_position(tp1_hit=True)
        result = evaluate_tiered_tp_action(pos, current_price=112.0)  # +12% vs 10% tp2
        assert result["action"] == "CLOSE_TP2"
        assert result["qty"] == 3
        assert result["activate_trailing"] is True

    def test_tiered_tp_trailing_stop(self):
        from trading.swing_manager import evaluate_tiered_tp_action
        pos = self._make_position(
            tp1_hit=True, tp2_hit=True,
            trailing_stop_active=True,
            trailing_high_water=120.0,
        )
        # current = 115, high_water = 120, 3% drawdown threshold = 116.4
        result = evaluate_tiered_tp_action(pos, current_price=115.0)
        assert result["action"] == "CLOSE_TRAILING"
        assert result["qty"] == 3

    def test_tiered_tp_none_when_no_trigger(self):
        from trading.swing_manager import evaluate_tiered_tp_action
        pos = self._make_position()
        result = evaluate_tiered_tp_action(pos, current_price=102.0)  # +2%, no trigger
        assert result["action"] == "NONE"

    def test_qualifies_for_eod_hold_all_pass(self):
        from trading.eod_holds import qualifies_for_eod_hold
        ok, reason = qualifies_for_eod_hold(
            symbol="NVDA",
            regime="BULL_TREND",
            earnings_tomorrow=False,
            current_pnl_pct=2.0,
            entry_sector_sentiment=20.0,
            current_sector_sentiment=15.0,
            consecutive_red_days=0,
            vix_trend="stable",
        )
        assert ok is True
        assert reason == ""

    def test_qualifies_for_eod_hold_wrong_regime(self):
        from trading.eod_holds import qualifies_for_eod_hold
        ok, reason = qualifies_for_eod_hold(
            symbol="NVDA", regime="CHOPPY",
            earnings_tomorrow=False,
            current_pnl_pct=2.0,
            entry_sector_sentiment=20.0,
            current_sector_sentiment=15.0,
            consecutive_red_days=0,
            vix_trend="stable",
        )
        assert ok is False
        assert "regime" in reason

    def test_qualifies_for_eod_hold_earnings_blocks(self):
        from trading.eod_holds import qualifies_for_eod_hold
        ok, reason = qualifies_for_eod_hold(
            symbol="NVDA", regime="BULL_TREND",
            earnings_tomorrow=True,
            current_pnl_pct=2.0,
            entry_sector_sentiment=20.0,
            current_sector_sentiment=15.0,
            consecutive_red_days=0,
            vix_trend="stable",
        )
        assert ok is False
        assert "earnings" in reason

    def test_qualifies_for_eod_hold_ptj_rule(self):
        from trading.eod_holds import qualifies_for_eod_hold
        ok, reason = qualifies_for_eod_hold(
            symbol="NVDA", regime="BULL_TREND",
            earnings_tomorrow=False,
            current_pnl_pct=-3.1,  # below BULL_TREND threshold of -3.0
            entry_sector_sentiment=20.0,
            current_sector_sentiment=15.0,
            consecutive_red_days=0,
            vix_trend="stable",
        )
        assert ok is False
        assert "PTJ" in reason

    @pytest.mark.unit
    def test_qualifies_for_eod_hold_normal_regime_allowed(self):
        from trading.eod_holds import qualifies_for_eod_hold
        ok, reason = qualifies_for_eod_hold(
            symbol="NVDA", regime="NORMAL",
            earnings_tomorrow=False,
            current_pnl_pct=1.5,
            entry_sector_sentiment=20.0,
            current_sector_sentiment=15.0,
            consecutive_red_days=0,
            vix_trend="stable",
        )
        assert ok is True, f"NORMAL regime should be swing-eligible, got: {reason}"


# ── swing_manager: _evaluate_gap_action ──────────────────────────────────────

@pytest.mark.unit
class TestEvaluateGapAction:
    @staticmethod
    def _pos(entry=100.0, sl_pct=7.0, tp1_hit=False):
        return {"entry_price": entry, "stop_loss_pct": sl_pct, "tp1_hit": tp1_hit}

    def test_gap_down_past_stop_returns_close(self):
        from trading.swing_manager import _evaluate_gap_action
        assert _evaluate_gap_action(self._pos(), current_price=92.0) == "CLOSE_GAP_DOWN"

    def test_gap_down_2_to_5pct_returns_tighten(self):
        from trading.swing_manager import _evaluate_gap_action
        assert _evaluate_gap_action(self._pos(), current_price=96.0) == "TIGHTEN_STOP"

    def test_gap_up_3pct_returns_take_tp1(self):
        from trading.swing_manager import _evaluate_gap_action
        assert _evaluate_gap_action(self._pos(), current_price=103.5) == "TAKE_TP1_GAP_UP"

    def test_gap_up_when_tp1_already_hit_returns_none(self):
        from trading.swing_manager import _evaluate_gap_action
        assert _evaluate_gap_action(self._pos(tp1_hit=True), current_price=103.5) == "NONE"

    def test_no_gap_returns_none(self):
        from trading.swing_manager import _evaluate_gap_action
        assert _evaluate_gap_action(self._pos(), current_price=100.5) == "NONE"


# ── swing_manager: load/save ──────────────────────────────────────────────────

@pytest.mark.unit
class TestLoadSwingPositions:
    def test_missing_file_returns_empty_dict(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "SWING_POSITIONS_FILE", tmp_path / "swing_positions.json")
        from trading.swing_manager import load_swing_positions
        assert load_swing_positions() == {}

    def test_loads_written_positions(self, tmp_path, monkeypatch):
        import config, json
        swing_file = tmp_path / "swing_positions.json"
        monkeypatch.setattr(config, "SWING_POSITIONS_FILE", swing_file)
        payload = {"NVDA": {"entry_price": 100.0, "qty": 10}}
        swing_file.write_text(json.dumps(payload))
        from trading.swing_manager import load_swing_positions
        result = load_swing_positions()
        assert "NVDA" in result
        assert result["NVDA"]["qty"] == 10


# ── swing_manager: extended coverage ─────────────────────────────────────────

@pytest.mark.unit
class TestSwingManagerOrchestration:
    """Extended coverage for swing_manager pure helpers."""

    @staticmethod
    def _pos(**overrides):
        base = {
            "entry_price": 100.0,
            "qty": 10,
            "stop_loss_pct": 7.0,
            "tp1_pct": 5.0,
            "tp2_pct": 10.0,
            "tp1_qty": 4,
            "tp2_qty": 3,
            "trailing_qty": 3,
            "tp1_hit": False,
            "tp2_hit": False,
            "trailing_stop_active": False,
            "trailing_high_water": 100.0,
        }
        base.update(overrides)
        return base

    # ── _remaining_qty ─────────────────────────────────────────────────────────

    def test_remaining_qty_no_hits(self):
        from trading.swing_manager import _remaining_qty
        pos = self._pos(qty=10, tp1_hit=False, tp2_hit=False, tp1_qty=4, tp2_qty=3)
        assert _remaining_qty(pos) == 10

    def test_remaining_qty_tp1_hit(self):
        from trading.swing_manager import _remaining_qty
        pos = self._pos(qty=10, tp1_hit=True, tp2_hit=False, tp1_qty=4, tp2_qty=3)
        assert _remaining_qty(pos) == 6

    def test_remaining_qty_both_hits(self):
        from trading.swing_manager import _remaining_qty
        pos = self._pos(qty=10, tp1_hit=True, tp2_hit=True, tp1_qty=4, tp2_qty=3)
        assert _remaining_qty(pos) == 3

    def test_remaining_qty_never_negative(self):
        """Oversold scenario — remaining_qty must floor at 0."""
        from trading.swing_manager import _remaining_qty
        pos = self._pos(qty=1, tp1_hit=True, tp2_hit=True, tp1_qty=4, tp2_qty=3)
        assert _remaining_qty(pos) == 0

    # ── evaluate_tiered_tp_action edge cases ──────────────────────────────

    def test_hard_stop_after_tp1_hit_uses_remaining_qty(self):
        """Hard stop after TP1 fill: only tp2_qty + trailing_qty remain."""
        from trading.swing_manager import evaluate_tiered_tp_action
        pos = self._pos(tp1_hit=True)
        result = evaluate_tiered_tp_action(pos, current_price=90.0)  # -10% > -7% stop
        assert result["action"] == "CLOSE_HARD_STOP"
        # remaining = trailing(3) + tp2_qty(3) = 6
        assert result["qty"] == 6

    def test_tp2_requires_tp1_hit(self):
        """TP2 must NOT trigger if tp1 has not been hit yet, even if price exceeds tp2."""
        from trading.swing_manager import evaluate_tiered_tp_action
        pos = self._pos(tp1_hit=False)
        # Price at +12%, above tp2=10%, but tp1 not hit yet
        result = evaluate_tiered_tp_action(pos, current_price=112.0)
        # Should trigger TP1, not TP2
        assert result["action"] == "CLOSE_TP1"

    def test_trailing_does_not_fire_without_trailing_stop_active(self):
        """Trailing stop must not trigger if trailing_stop_active is False."""
        from trading.swing_manager import evaluate_tiered_tp_action
        pos = self._pos(tp1_hit=True, tp2_hit=True, trailing_stop_active=False,
                        trailing_high_water=120.0)
        result = evaluate_tiered_tp_action(pos, current_price=115.0)
        assert result["action"] == "NONE"

    def test_trailing_does_not_fire_when_price_above_drawdown_threshold(self):
        """Trailing stop must not fire when price is within 3% of high water."""
        from trading.swing_manager import evaluate_tiered_tp_action
        pos = self._pos(tp1_hit=True, tp2_hit=True, trailing_stop_active=True,
                        trailing_high_water=120.0)
        # 120 * 0.97 = 116.4 — price at 117 is above threshold, no close
        result = evaluate_tiered_tp_action(pos, current_price=117.0)
        assert result["action"] == "NONE"

    # ── _compute_tier_quantities edge cases ───────────────────────────────

    def test_tier_quantities_three_shares_all_positive(self):
        from trading.swing_manager import _compute_tier_quantities
        tp1, tp2, trailing = _compute_tier_quantities(3)
        assert tp1 + tp2 + trailing == 3
        assert trailing >= 1

    def test_tier_quantities_five_shares_sum_correct(self):
        from trading.swing_manager import _compute_tier_quantities
        tp1, tp2, trailing = _compute_tier_quantities(5)
        assert tp1 + tp2 + trailing == 5
        assert tp1 >= 1 and tp2 >= 1 and trailing >= 1

    # ── qualifies_for_eod_hold ────────────────────────────────────────────

    def test_earnings_tomorrow_blocks_capitulation_regime(self):
        """earnings_tomorrow is a binary veto for all regimes including CAPITULATION."""
        from trading.eod_holds import qualifies_for_eod_hold
        ok, reason = qualifies_for_eod_hold(
            symbol="NVDA", regime="CAPITULATION",
            earnings_tomorrow=True,
            current_pnl_pct=2.0,
            entry_sector_sentiment=20.0,
            current_sector_sentiment=15.0,
            consecutive_red_days=0,
            vix_trend="stable",
        )
        assert ok is False
        assert "earnings" in reason

    def test_capitulation_no_earnings_passes(self):
        """CAPITULATION without earnings tomorrow should qualify."""
        from trading.eod_holds import qualifies_for_eod_hold
        ok, reason = qualifies_for_eod_hold(
            symbol="NVDA", regime="CAPITULATION",
            earnings_tomorrow=False,
            current_pnl_pct=2.0,
            entry_sector_sentiment=20.0,
            current_sector_sentiment=15.0,
            consecutive_red_days=0,
            vix_trend="stable",
        )
        assert ok is True
        assert reason == ""

    def test_pnl_exactly_minus_two_passes_in_bull_trend(self):
        """BULL_TREND PTJ threshold is -3.0 so -2.0 P&L should pass."""
        from trading.eod_holds import qualifies_for_eod_hold
        ok, _ = qualifies_for_eod_hold(
            symbol="NVDA", regime="BULL_TREND",
            earnings_tomorrow=False,
            current_pnl_pct=-2.0,
            entry_sector_sentiment=20.0,
            current_sector_sentiment=15.0,
            consecutive_red_days=0,
            vix_trend="stable",
        )
        assert ok is True

    @pytest.mark.unit
    def test_eod_hold_ptj_rule_uses_config_threshold(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "SWING_MIN_PNL_PCT", -1.0)  # stricter than default -2.0
        from trading.eod_holds import qualifies_for_eod_hold
        ok, reason = qualifies_for_eod_hold(
            symbol="NVDA", regime="NORMAL",
            earnings_tomorrow=False,
            current_pnl_pct=-1.5,   # below -1.0 threshold
            entry_sector_sentiment=20.0,
            current_sector_sentiment=15.0,
            consecutive_red_days=0,
            vix_trend="stable",
        )
        assert ok is False
        assert "PTJ" in reason

    @pytest.mark.unit
    def test_gap_tighten_uses_config_sl_pct(self, monkeypatch, tmp_path):
        import config
        monkeypatch.setattr(config, "SWING_GAP_TIGHTEN_SL_PCT", 3.0)
        monkeypatch.setattr(config, "SWING_POSITIONS_FILE", tmp_path / "swing.json")
        from utils.io_utils import save_json
        save_json(tmp_path / "swing.json", {
            "NVDA": {"entry_price": 100.0, "stop_loss_pct": 7.0, "tp1_hit": False}
        })
        from trading.swing_manager import _evaluate_gap_action
        pos = {"entry_price": 100.0, "stop_loss_pct": 7.0, "tp1_hit": False}
        # Gap down 3% (from 100 to 97) — between -7% stop and -2% threshold → TIGHTEN_STOP
        action = _evaluate_gap_action(pos, current_price=97.0)
        assert action == "TIGHTEN_STOP"
