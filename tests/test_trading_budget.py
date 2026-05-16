"""
Risk manager budget, gap guard, thesis-break, fill correction, and auto-execute unit tests.
Run with: pytest tests/test_trading_budget.py -v -m unit
"""
import json
import os
from datetime import datetime

import pytest

from utils.constants import ET


# ── trading/risk_manager: budget pools ───────────────────────────────────────

@pytest.mark.unit
class TestRiskManagerBudget:
    """Tests for trading.risk_manager budget pool functions."""

    def _setup(self, tmp_path, monkeypatch, **overrides):
        """Write a pre-populated budget to tmp_path and patch config path."""
        import config
        budget_file = tmp_path / "api_budget.json"
        monkeypatch.setattr(config, "API_BUDGET_FILE", budget_file)

        # Build a fresh budget from the module, then apply overrides
        from trading.risk_manager import _fresh_budget
        budget = _fresh_budget()
        budget.update(overrides)
        import json
        budget_file.write_text(json.dumps(budget))
        return budget_file

    # 1. Entry pool blocks when spent
    def test_check_entry_blocks_when_entry_pool_spent(self, tmp_path, monkeypatch):
        """check_entry_budget returns False when entry pool is exhausted."""
        import config
        self._setup(tmp_path, monkeypatch, entry_spent_usd=0.70,
                    entry_ceiling_usd=round(config.API_BUDGET_NORMAL_CEILING * config.ENTRY_BUDGET_RATIO, 4))
        from trading.risk_manager import check_entry_budget
        result = check_entry_budget(0.01, "NORMAL")
        assert result is False

    # 2. Entry pool allows when under limit
    def test_check_entry_allows_when_under_limit(self, tmp_path, monkeypatch):
        """check_entry_budget returns True when entry pool has room."""
        self._setup(tmp_path, monkeypatch, entry_spent_usd=0.0)
        from trading.risk_manager import check_entry_budget
        result = check_entry_budget(0.01, "NORMAL")
        assert result is True

    # 3. Exit pool escalates when exhausted with open positions
    def test_check_exit_escalates_when_pool_exhausted_with_open_positions(self, tmp_path, monkeypatch):
        """check_exit_budget escalates ceiling and calls notify_budget_escalation once."""
        import config
        self._setup(tmp_path, monkeypatch, exit_spent_usd=0.30,
                    exit_ceiling_usd=round(config.API_BUDGET_NORMAL_CEILING * config.EXIT_BUDGET_RATIO, 4),
                    escalated=False)
        from unittest.mock import patch
        with patch("trading.risk_manager.telegram_notify.notify_budget_escalation") as mock_notify:
            from trading.risk_manager import check_exit_budget
            result = check_exit_budget(0.01, open_position_count=1, regime="NORMAL")
        mock_notify.assert_called_once()
        assert result is True

    # 4. Escalation is idempotent
    def test_check_exit_escalation_idempotent(self, tmp_path, monkeypatch):
        """Second call when already escalated does not trigger a second notification."""
        import config
        self._setup(tmp_path, monkeypatch, exit_spent_usd=0.30,
                    exit_ceiling_usd=round(config.API_BUDGET_NORMAL_CEILING * config.EXIT_BUDGET_RATIO, 4),
                    escalated=True,
                    ceiling_mode="elevated",
                    ceiling_usd=config.API_BUDGET_ELEVATED_CEILING)
        from unittest.mock import patch
        with patch("trading.risk_manager.telegram_notify.notify_budget_escalation") as mock_notify:
            from trading.risk_manager import check_exit_budget
            check_exit_budget(0.01, open_position_count=1, regime="NORMAL")
        mock_notify.assert_not_called()

    # 5. record_usage routes entry to entry pool
    def test_record_usage_entry_routes_to_entry_pool(self, tmp_path, monkeypatch):
        """record_usage with kind='entry' increments entry_spent_usd, not exit_spent_usd."""
        self._setup(tmp_path, monkeypatch)
        from trading.risk_manager import record_usage, load_budget
        record_usage(100, 50, 0, 0, kind="entry")
        b = load_budget()
        assert b["entry_spent_usd"] > 0
        assert b["exit_spent_usd"] == 0.0

    # 6. record_usage routes exit to exit pool
    def test_record_usage_exit_routes_to_exit_pool(self, tmp_path, monkeypatch):
        """record_usage with kind='exit' increments exit_spent_usd, not entry_spent_usd."""
        self._setup(tmp_path, monkeypatch)
        from trading.risk_manager import record_usage, load_budget
        record_usage(100, 50, 0, 0, kind="exit")
        b = load_budget()
        assert b["exit_spent_usd"] > 0
        assert b["entry_spent_usd"] == 0.0

    # 7. budget_exhausted only when both pools spent
    def test_budget_exhausted_only_when_both_pools_spent(self, tmp_path, monkeypatch):
        """budget_exhausted is True only after both entry and exit are exhausted."""
        import config
        from unittest.mock import patch
        self._setup(tmp_path, monkeypatch,
                    entry_spent_usd=0.70,
                    exit_spent_usd=0.30,
                    entry_ceiling_usd=round(config.API_BUDGET_NORMAL_CEILING * config.ENTRY_BUDGET_RATIO, 4),
                    exit_ceiling_usd=round(config.API_BUDGET_NORMAL_CEILING * config.EXIT_BUDGET_RATIO, 4))
        from trading.risk_manager import check_entry_budget, check_exit_budget, load_budget
        # Bypass time-of-day caps so the ceiling check runs and sets entry_exhausted
        with patch("trading.risk_manager.morning_cap_exceeded", return_value=False), \
             patch("trading.risk_manager.midday_cap_exceeded", return_value=False):
            check_entry_budget(0.01, "NORMAL")   # marks entry_exhausted
        check_exit_budget(0.01, open_position_count=0, regime="NORMAL")  # marks exit_exhausted, no escalation
        b = load_budget()
        assert b["budget_exhausted"] is True

    # 8. Exit allows when already escalated with room
    def test_check_exit_allows_when_already_escalated_with_room(self, tmp_path, monkeypatch):
        """After escalation, exit_ceiling_usd is elevated. A call with room should return True."""
        import config
        from trading.risk_manager import _fresh_budget, check_exit_budget

        budget = {
            **_fresh_budget(),
            "escalated": True,
            "ceiling_mode": "elevated",
            "ceiling_usd": config.API_BUDGET_ELEVATED_CEILING,
            "exit_ceiling_usd": round(config.API_BUDGET_ELEVATED_CEILING * config.EXIT_BUDGET_RATIO, 4),
            "exit_spent_usd": 0.0,
        }
        budget_file = tmp_path / "api_budget.json"
        monkeypatch.setattr(config, "API_BUDGET_FILE", budget_file)
        import json
        budget_file.write_text(json.dumps(budget))

        result = check_exit_budget(0.01, open_position_count=1, regime="NORMAL")
        assert result is True


# ── trading/risk_manager: gap guard ──────────────────────────────────────────

@pytest.mark.unit
class TestRiskManagerGapGuard:
    def test_is_gap_skip_true_when_gap_exceeds_threshold(self):
        # prev_close=100.0, current_price=104.0 (4% gap > GAP_SKIP_PCT=3.0%)
        from trading import risk_manager
        assert risk_manager.is_gap_skip(104.0, 100.0) is True

    def test_is_gap_skip_false_when_gap_within_threshold(self):
        # prev_close=100.0, current_price=102.0 (2% gap < GAP_SKIP_PCT=3.0%)
        from trading import risk_manager
        assert risk_manager.is_gap_skip(102.0, 100.0) is False

    def test_is_gap_skip_false_when_prev_close_zero(self):
        # prev_close=0.0 means missing data — should not block
        from trading import risk_manager
        assert risk_manager.is_gap_skip(150.0, 0.0) is False


# ── trading/risk_manager: thesis-break heuristic ─────────────────────────────

@pytest.mark.unit
class TestThesisBreakHeuristic:
    def _make_position(self, entry_composite=50, entry_price=100.0, stop_loss_pct=4.0):
        return {
            "entry_composite_score": entry_composite,
            "entry_price": entry_price,
            "stop_loss_pct": stop_loss_pct,
        }

    def _make_iv(self, composite_score, current_price=100.0, symbol="AAPL"):
        from indicators.values import IndicatorValues
        return IndicatorValues(
            symbol=symbol,
            timestamp="2026-05-12T10:00:00",
            current_price=current_price,
            vwap=current_price,
            vwap_std1_upper=current_price + 1,
            vwap_std1_lower=current_price - 1,
            vwap_std2_upper=current_price + 2,
            vwap_std2_lower=current_price - 2,
            rsi=50.0,
            macd=0.0,
            macd_signal=0.0,
            macd_hist=0.0,
            bb_upper=current_price + 3,
            bb_mid=current_price,
            bb_lower=current_price - 3,
            ema9=current_price,
            ema21=current_price,
            atr=1.0,
            volume_ratio=1.0,
            stoch_k=50.0,
            stoch_d=50.0,
            obv=1000.0,
            vwap_slope=0.0,
            rsi_slope=0.0,
            daily_volume_ratio=1.0,
            daily_volume_trend=0.0,
            daily_volume_mean=1_000_000.0,
            composite_score=composite_score,
            signal="HOLD",
            confidence=0.5,
        )

    def test_flags_on_composite_drop(self):
        import config
        from trading import risk_manager
        # composite well below entry threshold
        entry_composite = 50
        low_composite = entry_composite - config.EXIT_EVAL_COMPOSITE_DROP - 1
        iv = self._make_iv(composite_score=low_composite)
        pos = self._make_position(entry_composite=entry_composite)
        assert risk_manager.needs_exit_evaluation(iv, pos, {}) is True

    def test_flags_on_low_sentiment(self):
        import config
        from trading import risk_manager
        # composite score healthy, but sentiment below floor
        iv = self._make_iv(composite_score=50)
        pos = self._make_position(entry_composite=50)
        research = {"AAPL": {"sentiment_score": config.EXIT_EVAL_SENTIMENT_FLOOR - 10}}
        assert risk_manager.needs_exit_evaluation(iv, pos, research) is True

    def test_flags_when_pnl_near_stop_loss(self):
        from trading import risk_manager
        # entry_price=100, SL=4%, PnL=-3.5% which is < -(SL-1.0)=-3.0 → triggers
        # current_price = 100 * (1 - 0.035) = 96.5
        iv = self._make_iv(composite_score=50, current_price=96.5)
        pos = self._make_position(entry_composite=50, entry_price=100.0, stop_loss_pct=4.0)
        assert risk_manager.needs_exit_evaluation(iv, pos, {}) is True

    def test_returns_false_for_healthy_position(self):
        import config
        from trading import risk_manager
        # composite well above entry, good sentiment, price well above SL
        iv = self._make_iv(composite_score=60, current_price=103.0)
        pos = self._make_position(entry_composite=50, entry_price=100.0, stop_loss_pct=4.0)
        # pnl_pct = +3%, -(sl-1) = -3.0 → 3.0 > -3.0 so no PnL trigger
        # composite 60 > 50-10=40 so no drop trigger
        research = {"AAPL": {"sentiment_score": config.EXIT_EVAL_SENTIMENT_FLOOR + 20}}
        assert risk_manager.needs_exit_evaluation(iv, pos, research) is False


# ── trading/risk_manager: fill correction ────────────────────────────────────

@pytest.mark.unit
class TestFillCorrection:
    def _make_trading_client(self, fill_price, status="filled"):
        import types
        from unittest.mock import MagicMock
        stop_leg = types.SimpleNamespace(type="stop", id="leg-stop-1")
        limit_leg = types.SimpleNamespace(type="limit", id="leg-limit-1")
        order = types.SimpleNamespace(
            status=status,
            filled_avg_price=str(fill_price) if status == "filled" else None,
            legs=[stop_leg, limit_leg],
        )
        client = MagicMock()
        client.get_order_by_id.return_value = order
        return client

    def test_corrects_sl_tp_when_slippage_exceeds_threshold(self):
        import config
        from trading import risk_manager
        # estimated_price=100.0, fill_price=100.6 → slippage=0.6% > FILL_SLIPPAGE_THRESHOLD=0.5%
        estimated_price = 100.0
        fill_price = 100.6
        sl_pct = 4.0
        tp_pct = 5.0
        client = self._make_trading_client(fill_price)

        result = risk_manager.correct_bracket_after_fill(
            client, "order-123", estimated_price, sl_pct, tp_pct
        )

        expected_sl = round(fill_price * (1 - sl_pct / 100), 2)
        expected_tp = round(fill_price * (1 + tp_pct / 100), 2)
        assert result is not None
        assert result["fill_price"] == pytest.approx(fill_price)
        assert result["stop_loss_price"] == pytest.approx(expected_sl)
        assert result["take_profit_price"] == pytest.approx(expected_tp)
        assert client.replace_order_by_id.call_count == 2

    def test_skips_correction_when_fill_within_threshold(self):
        from trading import risk_manager
        # estimated_price=100.0, fill_price=100.3 → slippage=0.3% < 0.5% threshold
        client = self._make_trading_client(fill_price=100.3)
        result = risk_manager.correct_bracket_after_fill(
            client, "order-456", 100.0, 4.0, 5.0
        )
        assert result is None
        client.replace_order_by_id.assert_not_called()

    def test_returns_none_on_poll_timeout(self):
        import types
        from unittest.mock import MagicMock
        from trading import risk_manager
        # Order never fills — always returns status="pending"
        pending_order = types.SimpleNamespace(
            status="pending",
            filled_avg_price=None,
            legs=[],
        )
        client = MagicMock()
        client.get_order_by_id.return_value = pending_order
        result = risk_manager.correct_bracket_after_fill(
            client, "order-789", 100.0, 4.0, 5.0
        )
        assert result is None


# ── executor: auto-execute soft-flag guard ────────────────────────────────────

@pytest.mark.unit
class TestAutoExecuteSoftFlagGuard:
    """execute_buy must block auto-execute trades when check_all_gates returns soft flags."""

    def _iv(self, composite=60.0, current_price=100.0):
        from tests.test_signals import make_full_iv
        return make_full_iv(
            symbol="TEST", composite_score=composite, rsi=55.0,
            stoch_d=50.0, current_price=current_price,
        )

    def _base_kwargs(self, iv, decision):
        from unittest.mock import MagicMock
        return dict(
            symbol="TEST", iv=iv, decision=decision,
            regime="NORMAL",
            regime_info={"regime": "NORMAL", "spy_5day_return": 0.0},
            research={"TEST": {"sentiment_score": 25}},
            open_positions={},
            alpaca=MagicMock(),
            portfolio_value=10_000.0,
            pos_mult=1.0,
            spy_ret5d=0.0,
            symbol_ret5d=0.0,
            rsi_max=72.0,
            params={"vwap_slope_min": -0.5},
            now=datetime(2026, 5, 13, 10, 30, tzinfo=ET),
            entries_this_cycle=0,
        )

    @pytest.mark.unit
    def test_auto_execute_blocked_when_gate_returns_soft_flags(self):
        """execute_buy returns None for auto-execute trades when gate has soft flags (e.g. mtf_not_confirmed)."""
        from unittest.mock import patch
        from signals.entry_gates import GateResult
        from trading.executor import execute_buy

        iv = self._iv()
        decision = {
            "action": "BUY", "confidence": 90,
            "reasoning": "auto-execute: composite+sentiment threshold met",
            "stop_loss_pct": 4.0, "take_profit_pct": 5.0,
            "source": "auto_execute",
        }

        soft_flag_gate = GateResult(passed=True, reason="", flags=("mtf_not_confirmed",))

        with patch("trading.executor.check_all_gates", return_value=soft_flag_gate), \
             patch("trading.executor.get_intraday_bars", return_value=None), \
             patch("trading.executor.load_json", return_value={}), \
             patch("trading.executor.get_active_mode", return_value="DAY_TRADE"), \
             patch("trading.executor._MODE_POSITION_LIMITS", {}):
            result = execute_buy(**self._base_kwargs(iv, decision))

        assert result is None

    @pytest.mark.unit
    def test_claude_buy_proceeds_with_soft_flags(self):
        """execute_buy does NOT block Claude-reviewed BUY when gate has soft flags — only auto-execute is strict."""
        from unittest.mock import patch, MagicMock
        from signals.entry_gates import GateResult
        from trading.executor import execute_buy

        iv = self._iv()
        decision = {
            "action": "BUY", "confidence": 75,
            "reasoning": "claude decision",
            "stop_loss_pct": 4.0, "take_profit_pct": 5.0,
            # no "source" key → not auto_execute
        }

        soft_flag_gate = GateResult(passed=True, reason="", flags=("mtf_not_confirmed",))

        with patch("trading.executor.check_all_gates", return_value=soft_flag_gate), \
             patch("trading.executor.get_intraday_bars", return_value=None), \
             patch("trading.executor.load_json", return_value={}), \
             patch("trading.executor.get_active_mode", return_value="DAY_TRADE"), \
             patch("trading.executor._MODE_POSITION_LIMITS", {}), \
             patch("trading.executor.compute_position_size_for_mode", return_value=10), \
             patch("trading.executor.apply_regime_multiplier", return_value=10), \
             patch("trading.executor.apply_sentiment_multiplier", return_value=10), \
             patch("trading.executor.get_sl_tp_for_mode_and_regime", return_value=(4.0, 5.0)), \
             patch("trading.executor.append_trade"), \
             patch("trading.executor.save_json"), \
             patch("trading.executor.risk_manager.correct_bracket_after_fill", return_value=None), \
             patch("trading.executor.telegram_notify"), \
             patch("trading.executor._POSITIONS_LOCK"):
            mock_alpaca = MagicMock()
            mock_alpaca.place_bracket_order.return_value = MagicMock(id="order-123")
            mock_alpaca.get_latest_price.return_value = 100.0
            kwargs = self._base_kwargs(iv, decision)
            kwargs["alpaca"] = mock_alpaca
            result = execute_buy(**kwargs)

        # Result is not None — the trade proceeded (soft flags don't block Claude-reviewed trades)
        assert result is not None


@pytest.mark.unit
def test_auto_execute_not_blocked_by_mtf_unavailable():
    """mtf_unavailable is a data-fetch failure; must not block auto-execute."""
    from signals.entry_gates import GateResult
    from unittest.mock import patch, MagicMock
    from trading.executor import execute_buy
    from tests.test_signals import make_full_iv

    iv = make_full_iv(symbol="AAPL", composite_score=60.0, rsi=55.0, stoch_d=50.0, current_price=100.0)
    decision = {
        "action": "BUY", "confidence": 90,
        "reasoning": "auto-execute",
        "stop_loss_pct": 4.0, "take_profit_pct": 5.0,
        "source": "auto_execute",
    }
    base_kwargs = dict(
        symbol="AAPL", iv=iv, decision=decision,
        regime="NORMAL", regime_info={"regime": "NORMAL", "spy_5day_return": 0.0},
        research={"AAPL": {"sentiment_score": 25}},
        open_positions={}, portfolio_value=10_000.0, pos_mult=1.0,
        spy_ret5d=0.5, symbol_ret5d=1.0, rsi_max=72.0,
        params={"vwap_slope_min": -0.5},
        now=datetime(2026, 5, 13, 10, 30, tzinfo=ET),
        entries_this_cycle=0,
    )

    # Only mtf_unavailable (data-fetch failure) — should NOT block auto-execute
    gate = GateResult(passed=True, reason="", flags=("mtf_unavailable",))

    with patch("trading.executor.check_all_gates", return_value=gate), \
         patch("trading.executor.get_intraday_bars", return_value=None), \
         patch("trading.executor.load_json", return_value={}), \
         patch("trading.executor.get_active_mode", return_value="DAY_TRADE"), \
         patch("trading.executor._MODE_POSITION_LIMITS", {}), \
         patch("trading.executor.compute_position_size_for_mode", return_value=10), \
         patch("trading.executor.apply_regime_multiplier", return_value=10), \
         patch("trading.executor.apply_sentiment_multiplier", return_value=10), \
         patch("trading.executor.get_sl_tp_for_mode_and_regime", return_value=(4.0, 5.0)), \
         patch("trading.executor.append_trade"), \
         patch("trading.executor.save_json"), \
         patch("trading.executor.risk_manager.correct_bracket_after_fill", return_value=None), \
         patch("trading.executor.telegram_notify"), \
         patch("trading.executor._POSITIONS_LOCK"):
        mock_alpaca = MagicMock()
        mock_alpaca.place_bracket_order.return_value = MagicMock(id="order-123")
        mock_alpaca.get_latest_price.return_value = 100.0
        base_kwargs["alpaca"] = mock_alpaca
        result = execute_buy(**base_kwargs)

    assert result is not None, "mtf_unavailable must not block auto-execute"


@pytest.mark.unit
def test_auto_execute_still_blocked_by_mtf_not_confirmed():
    """mtf_not_confirmed is a real bearish signal and must still block auto-execute."""
    from signals.entry_gates import GateResult
    from unittest.mock import patch, MagicMock
    from trading.executor import execute_buy
    from tests.test_signals import make_full_iv

    iv = make_full_iv(symbol="AAPL", composite_score=60.0, rsi=55.0, stoch_d=50.0, current_price=100.0)
    decision = {
        "action": "BUY", "confidence": 90,
        "reasoning": "auto-execute",
        "stop_loss_pct": 4.0, "take_profit_pct": 5.0,
        "source": "auto_execute",
    }
    base_kwargs = dict(
        symbol="AAPL", iv=iv, decision=decision,
        regime="NORMAL", regime_info={"regime": "NORMAL", "spy_5day_return": 0.0},
        research={"AAPL": {"sentiment_score": 25}},
        open_positions={}, alpaca=MagicMock(), portfolio_value=10_000.0, pos_mult=1.0,
        spy_ret5d=0.5, symbol_ret5d=1.0, rsi_max=72.0,
        params={"vwap_slope_min": -0.5},
        now=datetime(2026, 5, 13, 10, 30, tzinfo=ET),
        entries_this_cycle=0,
    )

    # mtf_not_confirmed is a real signal flag — must block auto-execute
    gate = GateResult(passed=True, reason="", flags=("mtf_not_confirmed",))

    with patch("trading.executor.check_all_gates", return_value=gate), \
         patch("trading.executor.get_intraday_bars", return_value=None), \
         patch("trading.executor.load_json", return_value={}), \
         patch("trading.executor.get_active_mode", return_value="DAY_TRADE"), \
         patch("trading.executor._MODE_POSITION_LIMITS", {}):
        result = execute_buy(**base_kwargs)

    assert result is None, "mtf_not_confirmed must block auto-execute"


@pytest.mark.unit
def test_entry_exhausted_flag_rehydrated_from_disk(tmp_path, monkeypatch):
    """After restart, is_entry_budget_exhausted() must read disk if in-memory flag is False."""
    import trading.risk_manager as rm

    from datetime import date as _date
    budget = {
        "date": _date.today().isoformat(), "entry_exhausted": True,
        "entry_spent_usd": 0.87, "entry_ceiling_usd": 0.85,
        "exit_exhausted": False, "budget_exhausted": False, "escalated": False,
        "calls_made": 49, "entry_calls": 49, "exit_calls": 0,
        "input_tokens": 0, "output_tokens": 0,
        "cache_write_tokens": 0, "cache_read_tokens": 0,
        "estimated_cost_usd": 0.87, "exit_spent_usd": 0.0,
        "ceiling_usd": 0.70, "entry_ceiling_usd": 0.85, "exit_ceiling_usd": 0.35,
        "ceiling_mode": "normal",
    }
    budget_file = tmp_path / "api_budget.json"
    from utils.io_utils import save_json
    save_json(str(budget_file), budget)

    monkeypatch.setattr("config.API_BUDGET_FILE", str(budget_file))

    # Simulate fresh module state (flag = False, as after restart)
    rm._entry_exhausted_flag = False
    rm._disk_rehydrated = False

    result = rm.is_entry_budget_exhausted()
    assert result is True, "Must read disk when in-memory flag is False and disk shows exhausted"


@pytest.mark.unit
def test_is_entry_budget_exhausted_does_not_re_read_disk_on_subsequent_calls(tmp_path, monkeypatch):
    """After the first disk check, subsequent calls must not read disk again (O(1))."""
    import trading.risk_manager as rm
    from utils.io_utils import save_json

    budget_file = tmp_path / "api_budget.json"
    save_json(str(budget_file), {"date": "2026-05-16", "entry_exhausted": False})
    monkeypatch.setattr("config.API_BUDGET_FILE", str(budget_file))

    rm._entry_exhausted_flag = False
    rm._disk_rehydrated = False

    disk_reads = []
    original_load = rm.load_budget
    def counting_load():
        disk_reads.append(1)
        return original_load()
    monkeypatch.setattr(rm, "load_budget", counting_load)

    # First call: rehydrates from disk (1 read expected)
    rm.is_entry_budget_exhausted()
    assert len(disk_reads) == 1, "First call must read disk once"

    # Second and third call: flag still False, but disk must NOT be re-read
    rm.is_entry_budget_exhausted()
    rm.is_entry_budget_exhausted()
    assert len(disk_reads) == 1, "Subsequent calls must not re-read disk"
