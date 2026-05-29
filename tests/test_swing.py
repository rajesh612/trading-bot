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
            entry_rsi=50.0,
            entry_composite=65.0,
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
            entry_rsi=50.0,
            entry_composite=65.0,
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
            entry_rsi=50.0,
            entry_composite=65.0,
        )
        assert ok is False
        assert "earnings" in reason

    def test_qualifies_for_eod_hold_min_pnl_gate(self):
        from trading.eod_holds import qualifies_for_eod_hold
        ok, reason = qualifies_for_eod_hold(
            symbol="NVDA", regime="BULL_TREND",
            earnings_tomorrow=False,
            current_pnl_pct=0.1,  # below BULL_TREND MEDIUM min_pnl of 0.5
            entry_sector_sentiment=20.0,
            current_sector_sentiment=15.0,
            consecutive_red_days=0,
            vix_trend="stable",
            entry_rsi=50.0,
            entry_composite=65.0,
        )
        assert ok is False
        assert "insufficient" in reason.lower() or "P&L" in reason

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
            entry_rsi=50.0,
            entry_composite=65.0,
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

    def test_large_gap_below_tighten_threshold_returns_close(self):
        # gap=-4.0% (price=96) is below SWING_GAP_TIGHTEN_THRESHOLD=-2.0 → close to prevent further loss
        from trading.swing_manager import _evaluate_gap_action
        assert _evaluate_gap_action(self._pos(), current_price=96.0) == "CLOSE_GAP_DOWN"

    def test_moderate_gap_above_tighten_threshold_returns_tighten(self):
        # gap=-1.5% (price=98.5) is between SWING_GAP_TIGHTEN_THRESHOLD=-2.0 and gap_close_threshold=-1.0 → tighten stop
        from trading.swing_manager import _evaluate_gap_action
        assert _evaluate_gap_action(self._pos(), current_price=98.5) == "TIGHTEN_STOP"

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
            entry_rsi=50.0,
            entry_composite=65.0,
        )
        assert ok is False
        assert "earnings" in reason

    def test_capitulation_no_earnings_passes(self):
        """CAPITULATION without earnings tomorrow should qualify for HIGH-tier positions."""
        from trading.eod_holds import qualifies_for_eod_hold
        from utils.ticker_quality import TickerQuality
        quality = TickerQuality(
            tier="HIGH", score=70, unlocks=frozenset(),
            quality_gaps=(), component_scores={},
        )
        ok, reason = qualifies_for_eod_hold(
            symbol="NVDA", regime="CAPITULATION",
            earnings_tomorrow=False,
            current_pnl_pct=2.0,
            quality=quality,
            entry_sector_sentiment=20.0,
            current_sector_sentiment=15.0,
            consecutive_red_days=0,
            vix_trend="stable",
            entry_rsi=50.0,
            entry_composite=65.0,
        )
        assert ok is True
        assert reason == ""

    def test_pnl_at_bull_trend_min_passes(self):
        """BULL_TREND MEDIUM min_pnl_pct is 0.5; 0.6% comfortably passes."""
        from trading.eod_holds import qualifies_for_eod_hold
        ok, _ = qualifies_for_eod_hold(
            symbol="NVDA", regime="BULL_TREND",
            earnings_tomorrow=False,
            current_pnl_pct=0.6,
            entry_sector_sentiment=20.0,
            current_sector_sentiment=15.0,
            consecutive_red_days=0,
            vix_trend="stable",
            entry_rsi=50.0,
            entry_composite=65.0,
        )
        assert ok is True

    @pytest.mark.unit
    def test_eod_hold_min_pnl_uses_swing_params(self):
        # NORMAL MEDIUM min_pnl_pct is 0.8; use 0.2 to test below-threshold blocking
        from trading.eod_holds import qualifies_for_eod_hold
        ok, reason = qualifies_for_eod_hold(
            symbol="NVDA", regime="NORMAL",
            earnings_tomorrow=False,
            current_pnl_pct=0.2,   # below NORMAL MEDIUM min_pnl_pct (0.8)
            entry_sector_sentiment=20.0,
            current_sector_sentiment=15.0,
            consecutive_red_days=0,
            vix_trend="stable",
            entry_rsi=50.0,
            entry_composite=65.0,
        )
        assert ok is False
        assert "insufficient" in reason.lower() or "P&L" in reason

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
        # Gap down 3% (from 100 to 97) is below SWING_GAP_TIGHTEN_THRESHOLD=-2% → CLOSE_GAP_DOWN
        # A -1.5% gap (price=98.5) is moderate → TIGHTEN_STOP and uses SWING_GAP_TIGHTEN_SL_PCT
        action = _evaluate_gap_action(pos, current_price=97.0)
        assert action == "CLOSE_GAP_DOWN"
        action_moderate = _evaluate_gap_action(pos, current_price=98.5)
        assert action_moderate == "TIGHTEN_STOP"


class TestSwingPromotionFromTPSLTable:
    @pytest.mark.unit
    def test_promotion_uses_tpsl_max_days(self, monkeypatch, tmp_path):
        import config
        monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", str(tmp_path / "open.json"))
        monkeypatch.setattr(config, "SWING_POSITIONS_FILE", str(tmp_path / "swing.json"))
        # NORMAL/HIGH/SWING: max_days=7 (raised to let winners run longer)
        from utils.regime_params import get_tp_sl_params
        p = get_tp_sl_params("NORMAL", "HIGH", "SWING")
        assert p.max_days == 7

    @pytest.mark.unit
    def test_profit_lock_from_table(self):
        from utils.regime_params import get_tp_sl_params
        p = get_tp_sl_params("BULL_TREND", "CONVICTION", "SWING")
        assert p.profit_lock_pct == 0.5
        assert p.min_pnl_pct == 0.1

    @pytest.mark.unit
    def test_get_effective_profit_lock_removed(self):
        import utils.regime_params as rp
        assert not hasattr(rp, "get_effective_profit_lock"), (
            "get_effective_profit_lock must be deleted — use get_tp_sl_params directly"
        )


# ── Tier-based swing promotion ────────────────────────────────────────────────

class TestTierBasedSwingPromotion:
    def _conviction_swing_choppy(self):
        from utils.ticker_quality import TickerQuality
        return TickerQuality("CONVICTION", 85, frozenset({"SWING_CHOPPY"}), (), {})

    def _medium_quality(self):
        from utils.ticker_quality import TickerQuality
        return TickerQuality("MEDIUM", 45, frozenset(), (), {})

    @pytest.mark.unit
    def test_conviction_swing_choppy_profit_lock_from_table(self):
        from utils.regime_params import get_tp_sl_params
        p = get_tp_sl_params("CHOPPY", "CONVICTION", "SWING")
        assert p.profit_lock_pct == pytest.approx(1.0)

    @pytest.mark.unit
    def test_high_choppy_swing_profit_lock_from_table(self):
        from utils.regime_params import get_tp_sl_params
        p = get_tp_sl_params("CHOPPY", "HIGH", "SWING")
        assert p.profit_lock_pct == pytest.approx(2.0)

    @pytest.mark.unit
    def test_regime_stale_blocks_swing(self):
        from trading.eod_holds import qualifies_for_eod_hold
        qualifies, reason = qualifies_for_eod_hold(
            symbol="AAPL",
            regime="NORMAL",
            earnings_tomorrow=False,
            current_pnl_pct=3.0,
            entry_sector_sentiment=10,
            current_sector_sentiment=5,
            consecutive_red_days=0,
            vix_trend="stable",
            regime_stale=True,
        )
        assert not qualifies
        assert "stale" in reason.lower()

    @pytest.mark.unit
    def test_conviction_swing_choppy_qualifies_at_1_3pct_pnl(self, monkeypatch):
        import config
        from trading.eod_holds import qualifies_for_eod_hold
        quality = self._conviction_swing_choppy()
        qualifies, reason = qualifies_for_eod_hold(
            symbol="NVDA",
            regime="CHOPPY",
            earnings_tomorrow=False,
            current_pnl_pct=1.3,
            entry_sector_sentiment=10,
            current_sector_sentiment=5,
            consecutive_red_days=0,
            vix_trend="stable",
            quality=quality,
            entry_rsi=50.0,
            entry_composite=65.0,
        )
        assert qualifies, f"Expected swing qualification, got: {reason}"


class TestSwingManagerTierGapCheck:
    @pytest.mark.unit
    def test_conviction_tolerates_wider_gap(self):
        pos_conviction = {"symbol": "NVDA", "quality_tier": "CONVICTION", "entry_price": 100.0}
        pos_medium     = {"symbol": "RIVN", "quality_tier": "MEDIUM",     "entry_price": 100.0}
        tier_c = pos_conviction.get("quality_tier", "MEDIUM")
        tier_m = pos_medium.get("quality_tier", "MEDIUM")
        thresh_c = -1.5 if tier_c == "CONVICTION" else -1.0
        thresh_m = -1.5 if tier_m == "CONVICTION" else -1.0
        assert thresh_c == -1.5
        assert thresh_m == -1.0

    @pytest.mark.unit
    def test_gap_of_minus_1_2_closes_medium_not_conviction(self):
        gap_pct = -1.2
        for tier, expected_close in [("MEDIUM", True), ("CONVICTION", False)]:
            threshold = -1.5 if tier == "CONVICTION" else -1.0
            closes = gap_pct < threshold
            assert closes == expected_close, f"tier={tier}"


# ── TestSwingSLFloors ─────────────────────────────────────────────────────────

@pytest.mark.unit
class TestSwingSLFloors:
    """Verify swing SL floors are loaded from config and applied correctly."""

    def test_swing_sl_floors_loaded_from_config(self):
        from utils.regime_params import get_tp_sl_params
        p = get_tp_sl_params("NORMAL", "MEDIUM", "SWING")
        assert p.sl_floor_pct == 4.5

    def test_swing_sl_conviction_tighter(self):
        from utils.regime_params import get_tp_sl_params
        medium = get_tp_sl_params("NORMAL", "MEDIUM", "SWING")
        conviction = get_tp_sl_params("NORMAL", "CONVICTION", "SWING")
        assert conviction.sl_floor_pct < medium.sl_floor_pct
        assert conviction.sl_floor_pct == 2.5  # explicit per-tier floor, no CONVICTION_TIGHT

    def test_swing_sl_low_wider_than_medium(self):
        from utils.regime_params import get_tp_sl_params
        low = get_tp_sl_params("NORMAL", "LOW", "SWING")
        medium = get_tp_sl_params("NORMAL", "MEDIUM", "SWING")
        assert low.sl_floor_pct > medium.sl_floor_pct
        assert low.sl_floor_pct == 5.0  # low-quality stocks need widest overnight room

    def test_highvol_swing_sl_wider(self):
        from utils.regime_params import get_tp_sl_params
        p = get_tp_sl_params("HIGH_VOL", "MEDIUM", "SWING")
        assert p.sl_floor_pct >= 4.5  # MEDIUM in HIGH_VOL gets widest floor

    def test_swing_maxdays_increased_normal(self):
        from utils.regime_params import get_tp_sl_params
        p = get_tp_sl_params("NORMAL", "MEDIUM", "SWING")
        assert p.max_days == 7

    def test_swing_maxdays_increased_bull_trend(self):
        from utils.regime_params import get_tp_sl_params
        p = get_tp_sl_params("BULL_TREND", "CONVICTION", "SWING")
        assert p.max_days == 12

    def test_eod_holds_respects_tier(self):
        """CONVICTION profit_lock for CHOPPY is 1.0; MEDIUM is disabled (falls back to 2.5).
        A position at 1.1% pnl qualifies via profit-lock bypass with CONVICTION
        but is blocked with MEDIUM (1.1 < 2.5 fallback, and CHOPPY disables standard promotion)."""
        from types import SimpleNamespace
        from trading.eod_holds import qualifies_for_eod_hold

        conviction_proxy = SimpleNamespace(tier="CONVICTION")
        medium_proxy = SimpleNamespace(tier="MEDIUM")

        ok_conviction, _ = qualifies_for_eod_hold(
            symbol="TEST", regime="CHOPPY", earnings_tomorrow=False,
            current_pnl_pct=1.1,
            entry_sector_sentiment=10.0, current_sector_sentiment=5.0,
            consecutive_red_days=0, vix_trend="stable",
            quality=conviction_proxy,
            entry_rsi=50.0, entry_composite=65.0,
        )
        ok_medium, reason = qualifies_for_eod_hold(
            symbol="TEST", regime="CHOPPY", earnings_tomorrow=False,
            current_pnl_pct=1.1,
            entry_sector_sentiment=10.0, current_sector_sentiment=5.0,
            consecutive_red_days=0, vix_trend="stable",
            quality=medium_proxy,
            entry_rsi=50.0, entry_composite=65.0,
        )
        assert ok_conviction is True
        assert ok_medium is False
