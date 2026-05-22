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
        self._setup(tmp_path, monkeypatch,
                    entry_spent_usd=0.70,
                    exit_spent_usd=0.30,
                    entry_ceiling_usd=round(config.API_BUDGET_NORMAL_CEILING * config.ENTRY_BUDGET_RATIO, 4),
                    exit_ceiling_usd=round(config.API_BUDGET_NORMAL_CEILING * config.EXIT_BUDGET_RATIO, 4))
        from trading.risk_manager import check_entry_budget, check_exit_budget, load_budget
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
def test_entry_exhausted_flag_rehydrated_from_disk(tmp_path, monkeypatch):
    """After restart, is_entry_budget_exhausted() must read disk if in-memory flag is False."""
    import trading.risk_manager as rm

    from datetime import datetime
    from utils.constants import ET
    budget = {
        "date": datetime.now(ET).strftime("%Y-%m-%d"), "entry_exhausted": True,
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
    save_json(str(budget_file), {"date": datetime.now(ET).strftime("%Y-%m-%d"), "entry_exhausted": False})
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


def _make_iv_budget(**kwargs):
    from indicators.values import IndicatorValues
    defaults = dict(
        symbol="AAPL", timestamp="2026-01-01T10:00:00",
        current_price=100.0, vwap=99.0,
        vwap_std1_upper=100.5, vwap_std1_lower=97.5,
        vwap_std2_upper=101.5, vwap_std2_lower=96.5,
        rsi=60.0, macd=0.1, macd_signal=0.05, macd_hist=0.05,
        bb_upper=102.0, bb_mid=100.0, bb_lower=98.0,
        ema9=100.2, ema21=99.8, atr=1.5, volume_ratio=1.0,
        stoch_k=60.0, stoch_d=60.0, obv=1000.0,
        vwap_slope=0.1, rsi_slope=0.0,
        daily_volume_ratio=1.0, daily_volume_trend=0.0, daily_volume_mean=1_000_000.0,
        composite_score=25.0, signal="BUY", confidence=0.7,
    )
    defaults.update(kwargs)
    return IndicatorValues(**defaults)


@pytest.mark.unit
class TestAgentBudgetRecording:
    """Verify that watch_list_agent and research/analyzer call risk_manager.record_usage()."""

    def _fake_response(self, input_tok=100, output_tok=50, cache_write=200, cache_read=0):
        """Build a minimal mock of the anthropic Messages response."""
        from unittest.mock import MagicMock
        usage = MagicMock()
        usage.input_tokens = input_tok
        usage.output_tokens = output_tok
        usage.cache_creation_input_tokens = cache_write
        usage.cache_read_input_tokens = cache_read
        resp = MagicMock()
        resp.usage = usage
        resp.content = [MagicMock(text='["AAPL", "NVDA"]')]
        return resp

    def test_watch_list_agent_records_budget(self, monkeypatch, tmp_path):
        """run_watch_list_agent records token usage after its Claude call."""
        from agents import watch_list as watch_list_agent

        fake_resp = self._fake_response(input_tok=300, output_tok=80, cache_write=500)
        fake_ticker = tmp_path / "tickers.txt"
        fake_ticker.write_text("AAPL\n")
        monkeypatch.setattr("config.DAILY_TRADEABLE_FILE", tmp_path / "nonexistent.txt")
        monkeypatch.setattr("config.TICKERS_FILE", fake_ticker)
        monkeypatch.setattr("config.WATCH_LIST_FILE", tmp_path / "watch_list.txt")
        monkeypatch.setattr("config.STRATEGY_MEMORY_FILE", tmp_path / "strategy_memory.md")
        monkeypatch.setattr("config.WATCH_LIST_MAX", 15)

        from unittest.mock import MagicMock, patch
        mock_alpaca = MagicMock()
        mock_alpaca.get_daily_bars.return_value = None  # _score_stock returns None → scored=[]

        recorded = {}
        def fake_record(input_tokens, output_tokens, cache_write_tokens=0, cache_read_tokens=0, **kwargs):
            recorded["input"] = input_tokens
            recorded["output"] = output_tokens
            recorded["cache_write"] = cache_write_tokens

        with patch("agents.watch_list.AlpacaClient", return_value=mock_alpaca), \
             patch("anthropic.Anthropic") as MockAI, \
             patch("trading.risk_manager.record_usage", side_effect=fake_record), \
             patch("agents.watch_list.telegram_notify.notify_watch_list"):
            MockAI.return_value.messages.create.return_value = fake_resp
            monkeypatch.setattr(watch_list_agent, "_load_universe", lambda: ["AAPL"])
            mock_score = {"symbol": "AAPL", "price": 100.0, "vol_spike": 2.0, "atr_pct": 2.0, "gap_pct": 0.5}
            monkeypatch.setattr(watch_list_agent, "_score_stock", lambda sym, client: mock_score)
            watch_list_agent.run_watch_list_agent()

        assert recorded.get("input") == 300
        assert recorded.get("output") == 80
        assert recorded.get("cache_write") == 500

    def test_research_analyzer_records_budget(self, monkeypatch):
        """_analyze_stock records token usage after its Claude call."""
        from unittest.mock import MagicMock, patch
        from research.analyzer import _analyze_stock

        usage = MagicMock()
        usage.input_tokens = 400
        usage.output_tokens = 60
        usage.cache_creation_input_tokens = 800
        usage.cache_read_input_tokens = 0
        resp = MagicMock()
        resp.usage = usage
        resp.content = [MagicMock(text='{"news_sentiment_score": 10, "news_sentiment_label": "BULLISH", "social_sentiment_score": 5, "social_sentiment_label": "BULLISH", "combined_sentiment_score": 8, "combined_sentiment_label": "BULLISH", "key_catalyst": null, "risk_flag": null, "divergence_alert": null, "summary": "ok", "strategy": "hold"}')]
        ai = MagicMock()
        ai.messages.create.return_value = resp
        recorded = {}
        def fake_record(input_tokens, output_tokens, cache_write_tokens=0, cache_read_tokens=0, **kwargs):
            recorded["input"] = input_tokens
            recorded["output"] = output_tokens
            recorded["cache_write"] = cache_write_tokens

        with patch("trading.risk_manager.record_usage", side_effect=fake_record):
            _analyze_stock("AAPL", [], "ctx", {}, {}, {}, ai, [])

        assert recorded.get("input") == 400
        assert recorded.get("output") == 60
        assert recorded.get("cache_write") == 800


@pytest.mark.unit
class TestBudgetEscalationNotify:
    def _patch_fake_token(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "fake_token")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "12345")

    def test_notify_budget_escalation_calls_send(self, monkeypatch):
        self._patch_fake_token(monkeypatch)
        from unittest.mock import patch
        import notifications.telegram as telegram_notify
        with patch("requests.post") as mock_post:
            telegram_notify.notify_budget_escalation(0.25, 1.50)
            mock_post.assert_called_once()
            text = mock_post.call_args[1]["json"]["text"]
            assert "escalated" in text.lower()

    def test_notify_budget_escalation_swallows_exceptions(self, monkeypatch):
        self._patch_fake_token(monkeypatch)
        from unittest.mock import patch
        import notifications.telegram as telegram_notify
        with patch("requests.post", side_effect=Exception("network error")):
            telegram_notify.notify_budget_escalation(0.25, 1.50)
            # Must not raise — function should return normally


@pytest.mark.unit
def test_execute_buy_blocks_at_max_daily_loss(monkeypatch, tmp_path):
    """execute_buy returns None when daily_pnl_pct hits MAX_DAILY_LOSS_PCT."""
    from unittest.mock import MagicMock
    from trading.executor import execute_buy
    import config

    monkeypatch.setattr(config, "MAX_DAILY_LOSS_PCT", 0.03)
    monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "positions.json")
    monkeypatch.setattr(config, "DAILY_CLOSED_FILE", tmp_path / "closed.json")
    monkeypatch.setattr("notifications.telegram.notify_buy", lambda **kw: None)
    monkeypatch.setattr("notifications.telegram.notify_daily_loss_warning", lambda *a: None)

    iv = _make_iv_budget(composite_score=30.0)
    alpaca_mock = MagicMock()

    result = execute_buy(
        symbol="AAPL", iv=iv,
        decision={"action": "BUY", "confidence": 80, "stop_loss_pct": 4.0, "take_profit_pct": 5.0},
        regime="NORMAL", regime_info={}, research={},
        open_positions={}, alpaca=alpaca_mock,
        portfolio_value=100_000, pos_mult=1.0,
        spy_ret5d=0.0, symbol_ret5d=0.0, rsi_max=72.0,
        params={}, now=datetime.now(ET).replace(hour=11),
        entries_this_cycle=0,
        daily_pnl_pct=-3.5,   # exceeds MAX_DAILY_LOSS_PCT = 0.03 → -3%
        sector_today={},
        symbol_sector_etf=None,
    )
    assert result is None
    alpaca_mock.place_bracket_order.assert_not_called()


@pytest.mark.unit
def test_execute_buy_warns_at_daily_loss_warn_threshold(monkeypatch, tmp_path):
    """execute_buy fires loss warning notification when daily_pnl_pct between -2% and -3%."""
    from unittest.mock import MagicMock
    from trading.executor import execute_buy
    import config

    monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "positions.json")
    monkeypatch.setattr(config, "DAILY_CLOSED_FILE", tmp_path / "closed.json")
    monkeypatch.setattr(config, "MAX_DAILY_LOSS_PCT", 0.03)
    monkeypatch.setattr(config, "DAILY_LOSS_WARN_PCT", 0.02)
    monkeypatch.setattr("notifications.telegram.notify_buy", lambda **kw: None)

    warned = []
    monkeypatch.setattr(
        "notifications.telegram.notify_daily_loss_warning",
        lambda pct, ceil: warned.append(pct)
    )

    iv = _make_iv_budget(composite_score=30.0)
    alpaca_mock = MagicMock()
    alpaca_mock.place_bracket_order.return_value = {
        "id": "ord1", "stop_loss_price": 96.0, "take_profit_price": 105.0
    }
    monkeypatch.setattr("trading.risk_manager.correct_bracket_after_fill", lambda *a, **kw: None)
    monkeypatch.setattr("trading.executor.append_trade", lambda d: None)

    execute_buy(
        symbol="AAPL", iv=iv,
        decision={"action": "BUY", "confidence": 80, "stop_loss_pct": 4.0, "take_profit_pct": 5.0},
        regime="NORMAL", regime_info={}, research={},
        open_positions={}, alpaca=alpaca_mock,
        portfolio_value=100_000, pos_mult=1.0,
        spy_ret5d=0.0, symbol_ret5d=0.0, rsi_max=72.0,
        params={}, now=datetime.now(ET).replace(hour=11),
        entries_this_cycle=0,
        daily_pnl_pct=-2.5,   # between warn (-2%) and halt (-3%)
        sector_today={},
        symbol_sector_etf=None,
    )
    assert warned, "Expected daily_loss_warning notification to fire"


@pytest.mark.unit
def test_executor_logs_warning_on_slippage_timeout(monkeypatch, tmp_path, caplog):
    import logging
    import config
    monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "positions.json")
    monkeypatch.setattr(config, "DAILY_CLOSED_FILE", tmp_path / "closed.json")
    monkeypatch.setattr(config, "COMPOSITE_MIN_NORMAL", 20)
    monkeypatch.setattr("notifications.telegram.notify_buy", lambda **kw: None)
    monkeypatch.setattr("notifications.telegram.notify_daily_loss_warning", lambda *a: None)
    monkeypatch.setattr("trading.risk_manager.correct_bracket_after_fill", lambda *a, **kw: None)
    monkeypatch.setattr("trading.executor.append_trade", lambda d: None)

    from unittest.mock import MagicMock
    alpaca_mock = MagicMock()
    alpaca_mock.place_bracket_order.return_value = {
        "id": "ord1", "stop_loss_price": 96.0, "take_profit_price": 105.0
    }

    from trading.executor import execute_buy

    iv = _make_iv_budget(composite_score=30.0)

    with caplog.at_level(logging.WARNING, logger="trading.executor"):
        execute_buy(
            symbol="AAPL", iv=iv,
            decision={"action": "BUY", "confidence": 80, "stop_loss_pct": 4.0, "take_profit_pct": 5.0},
            regime="NORMAL", regime_info={}, research={},
            open_positions={}, alpaca=alpaca_mock,
            portfolio_value=100_000, pos_mult=1.0,
            spy_ret5d=0.0, symbol_ret5d=0.0, rsi_max=72.0,
            params={}, now=datetime.now(ET).replace(hour=11),
            entries_this_cycle=0, daily_pnl_pct=0.0,
            sector_today={}, symbol_sector_etf=None,
        )

    assert any(
        "timed out" in r.message.lower() or "slippage" in r.message.lower() or "fill" in r.message.lower()
        for r in caplog.records
    )


@pytest.mark.unit
def test_rollover_not_applied_twice(monkeypatch, tmp_path):
    """Rollover must only apply once even under concurrent calls."""
    import threading
    import config
    monkeypatch.setattr(config, "API_BUDGET_FILE", tmp_path / "budget.json")
    from utils.io_utils import save_json
    from trading import risk_manager

    budget = {
        "date": datetime.now(ET).strftime("%Y-%m-%d"), "calls_made": 2, "entry_calls": 1, "exit_calls": 1,
        "input_tokens": 100, "output_tokens": 50,
        "cache_write_tokens": 0, "cache_read_tokens": 0,
        "estimated_cost_usd": 0.15, "entry_spent_usd": 0.10, "exit_spent_usd": 0.05,
        "ceiling_usd": 1.00,
        "entry_ceiling_usd": 0.70, "exit_ceiling_usd": 0.30,
        "ceiling_mode": "normal",
        "entry_exhausted": False, "exit_exhausted": False,
        "budget_exhausted": False, "escalated": False,
    }
    save_json(config.API_BUDGET_FILE, budget)

    results = []
    def do_rollover():
        risk_manager.try_rollover_exit_to_entry(datetime.now(ET))
        results.append(1)

    t1 = threading.Thread(target=do_rollover)
    t2 = threading.Thread(target=do_rollover)
    t1.start(); t2.start()
    t1.join(); t2.join()

    # Both threads complete without exception — idempotency guaranteed
    assert len(results) == 2


@pytest.mark.unit
def test_auto_execute_uses_atr_tp(monkeypatch, tmp_path):
    """Phase 4a AUTO_EXECUTE decision uses ATR-based TP, not flat TAKE_PROFIT_PCT."""
    import config
    from trading.pipeline import TradingPipeline
    from trading.cycle_context import CycleSnapshot
    from indicators.values import IndicatorValues
    from datetime import datetime
    from zoneinfo import ZoneInfo

    ET = ZoneInfo("America/New_York")
    captured_decision = {}

    def mock_execute_buy(symbol, iv, decision, **kwargs):
        captured_decision.update(decision)
        return None  # no position opened

    monkeypatch.setattr("trading.pipeline.execute_buy", mock_execute_buy)
    monkeypatch.setattr("trading.pipeline.load_open_positions", lambda: {})
    monkeypatch.setattr("trading.pipeline.load_daily_closed", lambda: set())
    monkeypatch.setattr("trading.pipeline.pre_claude_filter", lambda *a, **kw: type("F", (), {"decision": "AUTO_EXECUTE", "reason": "test"})())
    monkeypatch.setattr("trading.pipeline.risk_manager.is_entry_budget_exhausted", lambda: False)
    monkeypatch.setattr("trading.pipeline.risk_manager.needs_exit_evaluation", lambda *a, **kw: False)
    monkeypatch.setattr("trading.pipeline.telegram_notify.notify_buy_batch", lambda *a, **kw: None)
    monkeypatch.setattr("trading.pipeline._ask_claude_batch", lambda *a, **kw: {})
    monkeypatch.setattr("trading.pipeline._ask_claude_exit_batch", lambda *a, **kw: {})

    # Patch data fetch to return one symbol with atr=3.0, price=100.0
    iv = IndicatorValues.__new__(IndicatorValues)
    for field in ["composite_score", "rsi", "rsi_slope", "macd_hist", "bb_lower", "bb_upper",
                  "ema9", "ema21", "stoch_k", "stoch_d", "vwap", "daily_volume_ratio",
                  "vwap_slope", "parabolic", "stop_hunt_zone"]:
        setattr(iv, field, 0.0)
    iv.composite_score = 60.0
    iv.signal = "BUY"
    iv.current_price = 100.0
    iv.atr = 3.0
    iv.parabolic = False
    iv.stop_hunt_zone = False

    monkeypatch.setattr("trading.pipeline._fetch_symbol_data", lambda sym, alpaca, open_pos: (sym, iv, None, 0.0))

    snap = CycleSnapshot.__new__(CycleSnapshot)
    snap.params = {"atr_tp_multiplier": 2.0}
    snap.research = {"__meta__": {"shock_score": 0}}
    snap.research_meta = {"timestamp": datetime.now(ET).isoformat()}
    snap.fred_cache = {}
    snap.regime_info = {"spy_5day_return": 0.0, "vix_current": 15.0}
    snap.regime = "NORMAL"
    snap.weights = {}
    snap.strategy_memory = ""
    snap.intraday_news = {}
    snap.sector_today = {}
    snap.advance_decline_ratio = 1.0
    from utils.data_health import FRESH_GUARD
    snap.staleness_guard = FRESH_GUARD
    snap.major_event_today = False

    pipeline = TradingPipeline()
    pipeline.run(
        symbols=["TEST"],
        snap=snap,
        ai=None,
        now=datetime(2026, 5, 18, 10, 30, tzinfo=ET),
        alpaca=None,
        portfolio_value=100_000.0,
        buying_power=50_000.0,
        can_open=True,
        daily_pnl_pct=0.0,
    )

    # atr=3.0, price=100.0, mult=2.0 → atr_pct=3.0% → raw=6.0% → clamped to 6.0
    assert abs(captured_decision.get("take_profit_pct", -1) - 6.0) < 0.01


@pytest.mark.unit
def test_execute_buy_stores_entry_sector_sentiment(monkeypatch, tmp_path):
    """execute_buy persists entry_sector_sentiment and entry_news_sentiment in position record."""
    import config
    from unittest.mock import MagicMock, patch
    from signals.entry_gates import GateResult
    from trading.executor import execute_buy
    from utils.io_utils import load_json

    monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "positions.json")
    monkeypatch.setattr(config, "DAILY_CLOSED_FILE", tmp_path / "closed.json")
    monkeypatch.setattr(config, "COMPOSITE_MIN_NORMAL", 20)
    monkeypatch.setattr("notifications.telegram.notify_buy", lambda **kw: None)
    monkeypatch.setattr("notifications.telegram.notify_daily_loss_warning", lambda *a: None)
    monkeypatch.setattr("trading.risk_manager.correct_bracket_after_fill", lambda *a, **kw: None)
    monkeypatch.setattr("trading.executor.append_trade", lambda d: None)

    alpaca_mock = MagicMock()
    alpaca_mock.place_bracket_order.return_value = {
        "id": "ord-nvda-1", "stop_loss_price": 450.0, "take_profit_price": 500.0,
    }

    iv = _make_iv_budget(symbol="NVDA", composite_score=30.0, current_price=470.0)
    research = {
        "NVDA": {"sentiment_score": 42},
        "__meta__": {"sector_sentiment": {"XLK": 15.0}},
    }

    passing_gate = GateResult(passed=True, reason="", flags=())

    with patch("trading.executor.check_all_gates", return_value=passing_gate), \
         patch("trading.executor.get_intraday_bars", return_value=None), \
         patch("trading.executor.load_json", return_value={}), \
         patch("trading.executor._POSITIONS_LOCK"), \
         patch("trading.executor.save_json") as mock_save:

        result = execute_buy(
            symbol="NVDA", iv=iv,
            decision={"action": "BUY", "confidence": 80, "stop_loss_pct": 4.0, "take_profit_pct": 5.0},
            regime="NORMAL", regime_info={}, research=research,
            open_positions={}, alpaca=alpaca_mock,
            portfolio_value=100_000, pos_mult=1.0,
            spy_ret5d=0.0, symbol_ret5d=0.0, rsi_max=72.0,
            params={}, now=datetime.now(ET).replace(hour=11),
            entries_this_cycle=0, daily_pnl_pct=0.0,
            sector_today={}, symbol_sector_etf=None,
        )

    assert result is not None, "execute_buy should succeed"
    assert "entry_sector_sentiment" in result
    assert "entry_news_sentiment" in result
    assert result["entry_news_sentiment"] == pytest.approx(42.0)

    # Verify save_json was called with a position containing the new fields
    assert mock_save.called
    saved_positions = mock_save.call_args[0][1]
    assert "entry_sector_sentiment" in saved_positions.get("NVDA", {})
    assert "entry_news_sentiment" in saved_positions.get("NVDA", {})


# ── auto-execute 9×4 routing ──────────────────────────────────────────────────

class TestAutoExecuteRouting:
    def _quality(self, tier, unlocks=frozenset()):
        from utils.ticker_quality import TickerQuality
        return TickerQuality(tier=tier, score=80, unlocks=unlocks,
                             quality_gaps=(), component_scores={})

    @pytest.mark.unit
    def test_low_tier_never_auto_executes(self):
        from utils.regime_params import get_auto_execute_params
        for regime in ("NORMAL", "CHOPPY", "BULL_TREND", "CAPITULATION"):
            p = get_auto_execute_params(regime, "LOW")
            assert p.composite_offset == 999

    @pytest.mark.unit
    def test_conviction_normal_auto_executes_at_offset_10(self, monkeypatch):
        import config
        from utils.regime_params import get_auto_execute_params
        monkeypatch.setattr(config, "COMPOSITE_MIN_NORMAL", 50)
        p = get_auto_execute_params("NORMAL", "CONVICTION")
        floor = config.COMPOSITE_MIN_NORMAL
        threshold = floor + p.composite_offset
        assert threshold == 60

    @pytest.mark.unit
    def test_capitulation_conviction_requires_pead_unlock(self):
        from utils.ticker_quality import TickerQuality
        from utils.regime_params import get_auto_execute_params
        p = get_auto_execute_params("CAPITULATION", "CONVICTION")
        assert p.extra_unlock == "PEAD_EXTENDED_WINDOW"
        quality_no_pead = TickerQuality("CONVICTION", 85, frozenset(), (), {})
        quality_with_pead = TickerQuality("CONVICTION", 85,
                                          frozenset({"PEAD_EXTENDED_WINDOW"}), (), {})
        assert p.extra_unlock not in quality_no_pead.unlocks
        assert p.extra_unlock in quality_with_pead.unlocks


# ── position quality persistence ─────────────────────────────────────────────

class TestPositionQualityPersistence:
    @pytest.mark.unit
    def test_quality_fields_in_position_record(self):
        from utils.ticker_quality import TickerQuality
        quality = TickerQuality("CONVICTION", 85, frozenset({"T2_CHOPPY"}), (), {})
        pos = {
            "symbol": "NVDA",
            "quality_tier": quality.tier,
            "quality_unlocks": list(quality.unlocks),
        }
        assert pos["quality_tier"] == "CONVICTION"
        assert "T2_CHOPPY" in pos["quality_unlocks"]


# ── monitor T2_CHOPPY unlock check ───────────────────────────────────────────

class TestMonitorT2UnlockCheck:
    @pytest.mark.unit
    def test_t2_choppy_unlock_allows_t2_in_choppy(self):
        pos = {
            "symbol": "NVDA", "entry_price": 100.0, "qty": 10,
            "quality_tier": "CONVICTION", "quality_unlocks": ["T2_CHOPPY"],
            "tranche_2_triggered": False,
        }
        pos_unlocks = frozenset(pos.get("quality_unlocks", []))
        regime = "CHOPPY"
        should_skip = regime == "CHOPPY" and "T2_CHOPPY" not in pos_unlocks
        assert not should_skip

    @pytest.mark.unit
    def test_no_unlock_blocks_t2_in_choppy(self):
        pos = {
            "symbol": "RIVN", "quality_tier": "MEDIUM", "quality_unlocks": [],
            "tranche_2_triggered": False,
        }
        pos_unlocks = frozenset(pos.get("quality_unlocks", []))
        regime = "CHOPPY"
        should_skip = regime == "CHOPPY" and "T2_CHOPPY" not in pos_unlocks
        assert should_skip
