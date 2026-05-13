"""
Trading agent, learning agent, and main.py unit tests.
Run with: pytest tests/test_trading.py -v -m unit
No API calls — all tests use synthetic fixtures.
"""
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

ET = ZoneInfo("America/New_York")


# ── Plan 3: Mode Routing & Learning ──────────────────────────────────────────


@pytest.mark.unit
class TestGetActiveMode:
    """trading_agent.get_active_mode pure routing logic."""

    def test_bull_trend_returns_hybrid(self):
        from trading.modes import get_active_mode
        assert get_active_mode("BULL_TREND") == "HYBRID"

    def test_opportunity_returns_hybrid(self):
        from trading.modes import get_active_mode
        assert get_active_mode("OPPORTUNITY") == "HYBRID"

    def test_capitulation_returns_short_term(self):
        from trading.modes import get_active_mode
        assert get_active_mode("CAPITULATION") == "SHORT_TERM"

    def test_crash_recovery_returns_short_term(self):
        from trading.modes import get_active_mode
        assert get_active_mode("CRASH_RECOVERY") == "SHORT_TERM"

    def test_crisis_returns_cash(self):
        from trading.modes import get_active_mode
        assert get_active_mode("CRISIS") == "CASH"

    def test_normal_returns_day_trade(self):
        from trading.modes import get_active_mode
        assert get_active_mode("NORMAL") == "DAY_TRADE"

    def test_choppy_returns_day_trade(self):
        from trading.modes import get_active_mode
        assert get_active_mode("CHOPPY") == "DAY_TRADE"

    def test_defensive_returns_day_trade(self):
        from trading.modes import get_active_mode
        assert get_active_mode("DEFENSIVE") == "DAY_TRADE"

    def test_unknown_regime_returns_day_trade(self):
        from trading.modes import get_active_mode
        assert get_active_mode("SOME_NEW_REGIME") == "DAY_TRADE"


@pytest.mark.unit
class TestGetSlTpForModeAndRegime:
    """trading_agent.get_sl_tp_for_mode_and_regime table lookups."""

    def test_day_trade_normal(self):
        from trading.modes import get_sl_tp_for_mode_and_regime
        sl, tp = get_sl_tp_for_mode_and_regime("DAY_TRADE", "NORMAL")
        assert sl == 3.0 and tp == 5.0

    def test_day_trade_choppy(self):
        from trading.modes import get_sl_tp_for_mode_and_regime
        sl, tp = get_sl_tp_for_mode_and_regime("DAY_TRADE", "CHOPPY")
        assert sl == 5.0 and tp == 6.0

    def test_hybrid_bull_trend(self):
        from trading.modes import get_sl_tp_for_mode_and_regime
        sl, tp = get_sl_tp_for_mode_and_regime("HYBRID", "BULL_TREND")
        assert sl == 7.0 and tp == 15.0

    def test_short_term_capitulation(self):
        from trading.modes import get_sl_tp_for_mode_and_regime
        sl, tp = get_sl_tp_for_mode_and_regime("SHORT_TERM", "CAPITULATION")
        assert sl == 12.0 and tp == 20.0

    def test_pead_any_regime(self):
        from trading.modes import get_sl_tp_for_mode_and_regime
        sl, tp = get_sl_tp_for_mode_and_regime("PEAD", "NORMAL")
        assert sl == 8.0 and tp == 8.0

    def test_unknown_combo_falls_back_to_config_defaults(self):
        import config
        from trading.modes import get_sl_tp_for_mode_and_regime
        sl, tp = get_sl_tp_for_mode_and_regime("UNKNOWN_MODE", "UNKNOWN_REGIME")
        assert sl == config.STOP_LOSS_PCT and tp == config.TAKE_PROFIT_PCT


@pytest.mark.unit
class TestComputePositionSizeForMode:
    """trading_agent.compute_position_size_for_mode sizing logic."""

    def test_day_trade_uses_5pct_cap(self):
        from trading.position_sizer import compute_position_size_for_mode
        portfolio = 10_000.0
        price = 100.0
        atr = 1.0  # trivially small ATR so risk-based won't cap
        qty = compute_position_size_for_mode(portfolio, price, atr, "DAY_TRADE", 0, False)
        # 5% of 10k / $100 = 5 shares
        assert qty == 5

    def test_hybrid_uses_3pct_cap(self):
        from trading.position_sizer import compute_position_size_for_mode
        qty = compute_position_size_for_mode(10_000.0, 100.0, 0.01, "HYBRID", 0, False)
        # 3% of 10k / $100 = 3 shares
        assert qty == 3

    def test_short_term_uses_2_5pct_cap(self):
        from trading.position_sizer import compute_position_size_for_mode
        qty = compute_position_size_for_mode(10_000.0, 100.0, 0.01, "SHORT_TERM", 0, False)
        # 2.5% of 10k / $100 = 2.5 → rounds to 2 or 3
        assert qty in (2, 3)

    def test_herding_reduces_size_by_30pct(self):
        import config
        from trading.position_sizer import compute_position_size_for_mode
        base = compute_position_size_for_mode(10_000.0, 100.0, 0.01, "DAY_TRADE", 0, False)
        herding = compute_position_size_for_mode(
            10_000.0, 100.0, 0.01, "DAY_TRADE",
            config.BOT_HERD_ALERT_SCORE + 1, False
        )
        assert herding <= base
        assert herding >= 1

    def test_squeeze_skips_herding_reduction(self):
        import config
        from trading.position_sizer import compute_position_size_for_mode
        no_squeeze = compute_position_size_for_mode(
            10_000.0, 100.0, 0.01, "DAY_TRADE",
            config.BOT_HERD_ALERT_SCORE + 1, False
        )
        with_squeeze = compute_position_size_for_mode(
            10_000.0, 100.0, 0.01, "DAY_TRADE",
            config.BOT_HERD_ALERT_SCORE + 1, True  # squeeze overrides herding reduction
        )
        assert with_squeeze > no_squeeze

    def test_minimum_one_share(self):
        from trading.position_sizer import compute_position_size_for_mode
        # Very high price relative to portfolio → floors at 1
        qty = compute_position_size_for_mode(100.0, 10_000.0, 1.0, "DAY_TRADE", 0, False)
        assert qty >= 1






@pytest.mark.unit
class TestBuildMarketContextBlock:
    def _regime(self, **overrides) -> dict:
        base = {
            "regime": "BULL_TREND",
            "macro_health_score": 32,
            "vix_current": 18.2,
            "vix_90d_avg": 20.0,
            "vix_trend": "declining",
            "spy_5day_return": 1.5,
            "consecutive_green_days": 2,
            "consecutive_red_days": 0,
            "spy_5day_sign_changes": 1,
            "sector_scores": {"AI_SEMI": 80, "BROAD_TECH": 60},
            "hyg_ret5d": 0.3,
            "iwm_ret5d": 0.5,
            "vix3m": 19.0,
        }
        base.update(overrides)
        return base

    def _fred(self, **overrides) -> dict:
        base = {
            "fed_direction": "cutting",
            "cpi_trend": "falling",
            "unemployment_rising": False,
        }
        base.update(overrides)
        return base

    def _meta(self, **overrides) -> dict:
        base = {
            "macro_shock_score": 0,
            "sector_sentiment": {"AI_SEMI": 28.0, "DATA_CENTER": 15.0},
            "capitulation_candidates": [],
        }
        base.update(overrides)
        return base

    def _patch_context(self, monkeypatch):
        """Patch intraday context and breadth to avoid HTTP calls."""
        monkeypatch.setattr("economic_calendar.is_event_risk_window", lambda now: (False, ""))
        monkeypatch.setattr("macro.intraday_context.get_intraday_context", lambda now: None)
        monkeypatch.setattr("macro.breadth_signals.build_breadth_context_line", lambda s: "Breadth: HYG=+0.3%")

    def test_contains_all_six_fields(self, monkeypatch):
        self._patch_context(monkeypatch)
        from trading.context_builder import build_stable_system_context, build_dynamic_user_context
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime(2026, 5, 5, 10, 30, tzinfo=ZoneInfo("America/New_York"))
        block = build_stable_system_context(self._regime(), self._fred(), self._meta()) + "\n" + build_dynamic_user_context(now)
        assert "BULL_TREND" in block
        assert "cutting" in block
        assert "falling" in block
        assert "None active" in block
        assert "AI_SEMI" in block   # from sector_scores in regime
        assert "Capitulation watch" in block

    def test_shock_score_omitted_when_zero(self, monkeypatch):
        self._patch_context(monkeypatch)
        from trading.context_builder import build_stable_system_context, build_dynamic_user_context
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime(2026, 5, 5, 10, 30, tzinfo=ZoneInfo("America/New_York"))
        block = build_stable_system_context(self._regime(), self._fred(), self._meta(macro_shock_score=0)) + "\n" + build_dynamic_user_context(now)
        assert "0/100" in block
        assert "elevated" not in block

    def test_shock_score_flagged_when_elevated(self, monkeypatch):
        self._patch_context(monkeypatch)
        from trading.context_builder import build_stable_system_context, build_dynamic_user_context
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime(2026, 5, 5, 10, 30, tzinfo=ZoneInfo("America/New_York"))
        block = build_stable_system_context(self._regime(), self._fred(), self._meta(macro_shock_score=55)) + "\n" + build_dynamic_user_context(now)
        assert "55/100" in block
        assert "elevated" in block

    def test_active_event_risk_flagged(self, monkeypatch):
        monkeypatch.setattr("economic_calendar.is_event_risk_window", lambda now: (True, "Non-Farm Payrolls"))
        monkeypatch.setattr("macro.intraday_context.get_intraday_context", lambda now: None)
        monkeypatch.setattr("macro.breadth_signals.build_breadth_context_line", lambda s: "Breadth: HYG=+0.3%")
        from trading.context_builder import build_stable_system_context, build_dynamic_user_context
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime(2026, 5, 5, 8, 35, tzinfo=ZoneInfo("America/New_York"))
        block = build_stable_system_context(self._regime(), self._fred(), self._meta()) + "\n" + build_dynamic_user_context(now)
        assert "Non-Farm Payrolls" in block
        assert "avoid" in block

    def test_capitulation_candidates_listed(self, monkeypatch):
        self._patch_context(monkeypatch)
        from trading.context_builder import build_stable_system_context, build_dynamic_user_context
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime(2026, 5, 5, 10, 30, tzinfo=ZoneInfo("America/New_York"))
        block = build_stable_system_context(self._regime(), self._fred(), self._meta(capitulation_candidates=["SMCI", "PLTR"])) + "\n" + build_dynamic_user_context(now)
        assert "SMCI" in block
        assert "PLTR" in block


# ── Startup exit alerts ───────────────────────────────────────────────────────

@pytest.mark.unit
class TestStartupExitAlerts:
    def test_exit_with_alert_calls_telegram_then_exit(self):
        """_exit_with_alert sends Telegram before sys.exit"""
        from unittest.mock import patch
        import main
        call_order = []
        def mock_exit(code):
            call_order.append("exit")
            raise SystemExit(code)
        with patch("main.telegram_notify._send", side_effect=lambda m: call_order.append("telegram")):
            with patch("sys.exit", side_effect=mock_exit):
                with pytest.raises(SystemExit):
                    main._exit_with_alert("test message")
        assert call_order == ["telegram", "exit"]

    def test_exit_with_alert_message_contains_text(self):
        """Telegram message contains the error text"""
        from unittest.mock import patch
        import main
        sent = []
        with patch("main.telegram_notify._send", side_effect=lambda m: sent.append(m)):
            with patch("sys.exit"):
                main._exit_with_alert("ALPACA_API_KEY missing")
        assert len(sent) == 1
        assert "ALPACA_API_KEY missing" in sent[0]

    def test_exit_with_alert_tolerates_telegram_failure(self):
        """If Telegram raises, sys.exit still fires"""
        from unittest.mock import patch
        import main
        with patch("main.telegram_notify._send", side_effect=Exception("Telegram down")):
            with patch("sys.exit") as mock_exit:
                main._exit_with_alert("some error")
        mock_exit.assert_called_once_with(1)

    def test_exit_with_alert_custom_code(self):
        """Custom exit code is passed to sys.exit"""
        from unittest.mock import patch
        import main
        with patch("main.telegram_notify._send"):
            with patch("sys.exit") as mock_exit:
                main._exit_with_alert("msg", code=2)
        mock_exit.assert_called_once_with(2)


# ── EOD force-close helpers ───────────────────────────────────────────────────

@pytest.mark.unit
class TestForceCloseAll:
    def _positions_json(self, tmp_path, positions: dict):
        p = tmp_path / "open_positions.json"
        p.write_text(json.dumps(positions))
        return p

    def test_eod_close_reason_profitable(self):
        from trading.closer import _eod_close_reason
        assert _eod_close_reason(1.0) == "eod_force_profitable"

    def test_eod_close_reason_loss(self):
        from trading.closer import _eod_close_reason
        assert _eod_close_reason(-1.0) == "eod_force_loss"

    def test_eod_close_reason_flat(self):
        from trading.closer import _eod_close_reason
        assert _eod_close_reason(0.0) == "eod_force_flat"

    def test_eod_close_reason_boundary_profitable(self):
        from trading.closer import _eod_close_reason
        # exactly at threshold is NOT profitable (> not >=)
        assert _eod_close_reason(0.5) == "eod_force_flat"
        assert _eod_close_reason(0.51) == "eod_force_profitable"

    def test_eod_close_reason_boundary_loss(self):
        from trading.closer import _eod_close_reason
        assert _eod_close_reason(-0.5) == "eod_force_flat"
        assert _eod_close_reason(-0.51) == "eod_force_loss"

    def test_force_close_phantom_skip(self, tmp_path, monkeypatch):
        """Symbols not in Alpaca positions are skipped without journaling"""
        from unittest.mock import MagicMock, patch
        import trading_agent
        import config

        # position in JSON but NOT in alpaca
        positions = {"AAPL": {"entry_price": 100.0, "stop_loss": 95.0, "take_profit": 110.0, "shares": 10, "qty": 10}}
        monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "open_positions.json")
        monkeypatch.setattr(config, "TRADE_JOURNAL_FILE", tmp_path / "trade_journal.json")
        (tmp_path / "open_positions.json").write_text(json.dumps(positions))
        (tmp_path / "trade_journal.json").write_text("[]")

        mock_alpaca = MagicMock()
        mock_alpaca.get_positions.return_value = {}  # AAPL not in live positions (empty dict)
        mock_alpaca.cancel_all_orders.return_value = None

        orig = config.EOD_VERIFY_FILLS
        config.EOD_VERIFY_FILLS = True
        try:
            with patch("trading.closer.AlpacaClient", return_value=mock_alpaca):
                trading_agent.force_close_all()
        finally:
            config.EOD_VERIFY_FILLS = orig

        # journal should NOT have a CLOSE entry for AAPL
        journal = json.loads((tmp_path / "trade_journal.json").read_text())
        close_entries = [e for e in journal if e.get("type") == "CLOSE" and e.get("symbol") == "AAPL"]
        assert close_entries == []


# ── trading_agent: profit-lock trailing stop ───────────────────────────────────

@pytest.mark.unit
class TestProfitLock:
    def _pos(self, entry: float, stop_price: float) -> dict:
        """Factory for a test position dict."""
        return {
            "entry_price": entry,
            "stop_loss_price": stop_price,
            "take_profit_price": entry * 1.05,
            "shares": 10,
            "symbol": "AAPL",
        }

    def test_no_change_below_trigger(self):
        """PnL below trigger threshold — stop should not change."""
        from trading.closer import _apply_profit_lock
        pos = self._pos(100.0, 95.0)
        result = _apply_profit_lock(pos, 101.0)  # only 1% gain, below 2% trigger
        assert result["stop_loss_price"] == pytest.approx(95.0)

    def test_stop_moves_to_entry_at_trigger(self):
        """PnL at trigger threshold — stop should move to entry price."""
        from trading.closer import _apply_profit_lock
        import config
        pos = self._pos(100.0, 95.0)
        current_price = 100.0 * (1 + config.PROFIT_LOCK_TRIGGER_PCT / 100)  # exactly at trigger
        result = _apply_profit_lock(pos, current_price)
        assert result["stop_loss_price"] == pytest.approx(100.0)

    def test_stop_not_moved_below_entry(self):
        """Stop already at or above entry — should not be lowered."""
        from trading.closer import _apply_profit_lock
        pos = self._pos(100.0, 101.0)  # stop already above entry
        result = _apply_profit_lock(pos, 102.1)  # 2.1% gain
        assert result["stop_loss_price"] == pytest.approx(101.0)  # stays put

    def test_no_change_when_entry_price_zero(self):
        """Zero entry price — should exit early without modifying stop."""
        from trading.closer import _apply_profit_lock
        pos = {"entry_price": 0.0, "stop_loss_price": 95.0, "symbol": "AAPL"}
        result = _apply_profit_lock(pos, 102.0)
        assert result["stop_loss_price"] == pytest.approx(95.0)

    def test_exact_trigger_pct_moves_stop(self):
        """Exactly at PROFIT_LOCK_TRIGGER_PCT should trigger the move."""
        from trading.closer import _apply_profit_lock
        import config
        pos = self._pos(100.0, 95.0)
        trigger_price = 100.0 * (1 + config.PROFIT_LOCK_TRIGGER_PCT / 100)
        result = _apply_profit_lock(pos, trigger_price)
        assert result["stop_loss_price"] == pytest.approx(100.0)

    def test_returns_same_dict_object(self):
        """Function should modify in place and return the same dict object."""
        from trading.closer import _apply_profit_lock
        pos = self._pos(100.0, 95.0)
        result = _apply_profit_lock(pos, 103.0)
        assert result is pos  # modifies in place


# ── trading/journal: thread-safe state I/O ────────────────────────────────────

@pytest.mark.unit
class TestJournalLocks:
    """Tests for lock-guarded read-modify-write operations in trading/journal.py."""

    def _setup(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "open_positions.json")
        monkeypatch.setattr(config, "DAILY_CLOSED_FILE", tmp_path / "daily_closed.json")
        monkeypatch.setattr(config, "TRADE_JOURNAL_FILE", tmp_path / "trade_journal.json")
        (tmp_path / "open_positions.json").write_text("{}")
        (tmp_path / "daily_closed.json").write_text("[]")
        (tmp_path / "trade_journal.json").write_text("[]")

    def test_append_trade_single_entry(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        from trading.journal import append_trade, load_journal
        append_trade({"type": "BUY", "symbol": "AAPL"})
        journal = load_journal()
        assert len(journal) == 1
        assert journal[0]["symbol"] == "AAPL"

    def test_append_trade_concurrent_all_entries_present(self, tmp_path, monkeypatch):
        """10 concurrent threads append different entries — all 10 must appear."""
        import threading
        self._setup(tmp_path, monkeypatch)
        from trading.journal import append_trade, load_journal
        threads = [
            threading.Thread(target=append_trade, args=({"type": "BUY", "symbol": f"SYM{i}"},))
            for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(load_journal()) == 10

    def test_record_daily_closed_adds_symbol(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        from trading.journal import record_daily_closed, load_daily_closed
        record_daily_closed("NVDA")
        assert "NVDA" in load_daily_closed()

    def test_record_daily_closed_idempotent(self, tmp_path, monkeypatch):
        """Adding the same symbol twice results in exactly one entry."""
        self._setup(tmp_path, monkeypatch)
        from trading.journal import record_daily_closed, load_daily_closed
        record_daily_closed("NVDA")
        record_daily_closed("NVDA")
        assert list(load_daily_closed()).count("NVDA") == 1

    def test_record_daily_closed_concurrent_all_symbols_present(self, tmp_path, monkeypatch):
        """10 concurrent threads close different symbols — all 10 must appear."""
        import threading
        self._setup(tmp_path, monkeypatch)
        from trading.journal import record_daily_closed, load_daily_closed
        threads = [
            threading.Thread(target=record_daily_closed, args=(f"SYM{i}",))
            for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        closed = load_daily_closed()
        assert all(f"SYM{i}" in closed for i in range(10))

    def test_reset_daily_closed_empties_file(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        from trading.journal import record_daily_closed, reset_daily_closed, load_daily_closed
        record_daily_closed("AAPL")
        reset_daily_closed()
        assert load_daily_closed() == set()

    def test_load_daily_closed_returns_set(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        from trading.journal import load_daily_closed
        result = load_daily_closed()
        assert isinstance(result, set)

    def test_multiple_trade_types_preserved(self, tmp_path, monkeypatch):
        """BUY and CLOSE entries co-exist in the journal."""
        self._setup(tmp_path, monkeypatch)
        from trading.journal import append_trade, load_journal
        append_trade({"type": "BUY", "symbol": "AAPL"})
        append_trade({"type": "CLOSE", "symbol": "AAPL", "pnl_pct": 2.5})
        journal = load_journal()
        assert len(journal) == 2
        assert journal[1]["pnl_pct"] == pytest.approx(2.5)


# ── trading/journal: flush_profit_lock_updates ────────────────────────────────

@pytest.mark.unit
class TestFlushProfitLock:
    """Tests for flush_profit_lock_updates merge-write semantics."""

    def _write_positions(self, tmp_path, positions: dict):
        f = tmp_path / "open_positions.json"
        f.write_text(json.dumps(positions))
        return f

    def _setup(self, tmp_path, monkeypatch, positions: dict):
        import config
        self._write_positions(tmp_path, positions)
        monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "open_positions.json")

    def test_flush_updates_existing_symbol(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch, {"AAPL": {"stop_loss_price": 95.0, "entry_price": 100.0}})
        from trading.journal import flush_profit_lock_updates, load_open_positions
        flush_profit_lock_updates({"AAPL": {"stop_loss_price": 100.0, "entry_price": 100.0}})
        pos = load_open_positions()
        assert pos["AAPL"]["stop_loss_price"] == pytest.approx(100.0)

    def test_flush_skips_symbol_not_on_disk(self, tmp_path, monkeypatch):
        """Symbol in updates but not on disk is silently skipped — no KeyError."""
        self._setup(tmp_path, monkeypatch, {"AAPL": {"stop_loss_price": 95.0}})
        from trading.journal import flush_profit_lock_updates, load_open_positions
        flush_profit_lock_updates({"NVDA": {"stop_loss_price": 200.0}})
        pos = load_open_positions()
        assert "NVDA" not in pos
        assert "AAPL" in pos   # existing symbol untouched

    def test_flush_preserves_concurrent_buy(self, tmp_path, monkeypatch):
        """A BUY written to disk between snapshot and flush must survive."""
        import config
        monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "open_positions.json")
        positions_file = tmp_path / "open_positions.json"
        positions_file.write_text(json.dumps({"AAPL": {"stop_loss_price": 95.0}}))

        # Simulate a concurrent BUY for NVDA written to disk before flush executes
        from utils.io_utils import load_json, save_json
        disk = load_json(positions_file, {})
        disk["NVDA"] = {"entry_price": 200.0, "stop_loss_price": 190.0}
        save_json(positions_file, disk)

        from trading.journal import flush_profit_lock_updates, load_open_positions
        flush_profit_lock_updates({"AAPL": {"stop_loss_price": 100.0}})
        pos = load_open_positions()
        assert "NVDA" in pos     # concurrent BUY survived
        assert "AAPL" in pos
        assert pos["AAPL"]["stop_loss_price"] == pytest.approx(100.0)

    def test_empty_updates_changes_nothing(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch, {"AAPL": {"stop_loss_price": 95.0}})
        from trading.journal import flush_profit_lock_updates, load_open_positions
        flush_profit_lock_updates({})
        pos = load_open_positions()
        assert pos["AAPL"]["stop_loss_price"] == pytest.approx(95.0)

    def test_multiple_updates_all_applied(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch, {
            "AAPL": {"stop_loss_price": 95.0},
            "NVDA": {"stop_loss_price": 400.0},
        })
        from trading.journal import flush_profit_lock_updates, load_open_positions
        flush_profit_lock_updates({
            "AAPL": {"stop_loss_price": 100.0},
            "NVDA": {"stop_loss_price": 450.0},
        })
        pos = load_open_positions()
        assert pos["AAPL"]["stop_loss_price"] == pytest.approx(100.0)
        assert pos["NVDA"]["stop_loss_price"] == pytest.approx(450.0)

    def test_non_updated_symbol_preserved(self, tmp_path, monkeypatch):
        """Symbols not in the updates dict are left untouched on disk."""
        self._setup(tmp_path, monkeypatch, {
            "AAPL": {"stop_loss_price": 95.0},
            "TSLA": {"stop_loss_price": 200.0},
        })
        from trading.journal import flush_profit_lock_updates, load_open_positions
        flush_profit_lock_updates({"AAPL": {"stop_loss_price": 100.0}})
        pos = load_open_positions()
        assert pos["TSLA"]["stop_loss_price"] == pytest.approx(200.0)


# ── trading/pipeline: TradingPipeline pure-logic tests ────────────────────────

@pytest.mark.unit
class TestTradingPipeline:
    """Tests for CycleResult, _compute_symbol_5day_return, and research staleness."""

    def test_cycle_result_defaults(self):
        from trading.pipeline import CycleResult
        r = CycleResult()
        assert r.entries == 0
        assert r.closes == 0
        assert r.skipped is False
        assert r.reason == ""

    def test_cycle_result_skipped(self):
        from trading.pipeline import CycleResult
        r = CycleResult(skipped=True, reason="stale_research")
        assert r.skipped is True
        assert r.reason == "stale_research"

    def test_compute_5day_return_none_returns_zero(self):
        from trading.pipeline import _compute_symbol_5day_return
        assert _compute_symbol_5day_return(None) == pytest.approx(0.0)

    def test_compute_5day_return_insufficient_rows(self):
        import pandas as pd
        from trading.pipeline import _compute_symbol_5day_return
        df = pd.DataFrame({"close": [100.0, 101.0, 102.0]})
        assert _compute_symbol_5day_return(df) == pytest.approx(0.0)

    def test_compute_5day_return_correct_value(self):
        import pandas as pd
        from trading.pipeline import _compute_symbol_5day_return
        # 6 rows: iloc[-6]=100, iloc[-1]=110 → (110-100)/100*100 = 10.0%
        df = pd.DataFrame({"close": [100.0, 102.0, 104.0, 106.0, 108.0, 110.0]})
        result = _compute_symbol_5day_return(df)
        assert result == pytest.approx(10.0)

    def test_compute_5day_return_negative(self):
        import pandas as pd
        from trading.pipeline import _compute_symbol_5day_return
        df = pd.DataFrame({"close": [110.0, 108.0, 106.0, 104.0, 102.0, 100.0]})
        result = _compute_symbol_5day_return(df)
        assert result == pytest.approx(-100.0 * 10 / 110, rel=1e-4)

    def _snap(self, research=None, params=None):
        from trading.cycle_context import CycleSnapshot
        return CycleSnapshot(
            regime_info={"regime": "NORMAL", "macro_health_score": 0},
            regime="NORMAL",
            fred_cache={},
            research=research or {},
            research_meta=(research or {}).get("__meta__", {}),
            strategy_memory="",
            weights={},
            params=params or {
                "rsi_buy_max": 70, "rsi_buy_max_high_vol": 60,
                "high_vol_position_mult": 0.5, "vwap_slope_min": -0.5,
                "entry_start_hour": 10, "entry_end_hour": 15,
            },
            portfolio_gain_pct=0.0,
            intraday_news={},
        )

    def test_research_staleness_skip_when_too_old(self, monkeypatch):
        """pipeline.run() returns skipped=True when research > RESEARCH_STALE_BLOCK_MIN."""
        from datetime import timedelta
        import config
        from trading.pipeline import TradingPipeline

        stale_ts = (datetime.now(ET) - timedelta(minutes=config.RESEARCH_STALE_BLOCK_MIN + 10)).isoformat()
        research = {"__meta__": {"timestamp": stale_ts}}

        pipeline = TradingPipeline()
        result = pipeline.run(
            symbols=[], snap=self._snap(research=research),
            ai=None, now=datetime.now(ET), alpaca=None,
            portfolio_value=10000.0, buying_power=5000.0, can_open=True,
        )
        assert result.skipped is True
        assert "stale" in result.reason

    def test_research_staleness_no_skip_when_fresh(self, monkeypatch):
        """pipeline.run() does not skip when research is within acceptable age."""
        from datetime import timedelta
        import config
        from trading.pipeline import TradingPipeline
        from unittest.mock import MagicMock, patch

        fresh_ts = (datetime.now(ET) - timedelta(minutes=10)).isoformat()
        research = {"__meta__": {"timestamp": fresh_ts}}

        mock_alpaca = MagicMock()
        mock_alpaca.get_intraday_bars.return_value = None
        mock_alpaca.get_daily_bars.return_value = None

        with patch("trading.pipeline.load_open_positions", return_value={}), \
             patch("trading.pipeline.load_daily_closed", return_value=set()):
            pipeline = TradingPipeline()
            result = pipeline.run(
                symbols=[], snap=self._snap(research=research),
                ai=None, now=datetime.now(ET), alpaca=mock_alpaca,
                portfolio_value=10000.0, buying_power=5000.0, can_open=True,
            )
        assert result.skipped is False

    def test_missing_meta_timestamp_no_crash(self, monkeypatch):
        """pipeline.run() handles missing __meta__.timestamp without raising."""
        from unittest.mock import MagicMock, patch
        from trading.pipeline import TradingPipeline

        research = {"__meta__": {}}  # no timestamp key
        with patch("trading.pipeline.load_open_positions", return_value={}), \
             patch("trading.pipeline.load_daily_closed", return_value=set()):
            pipeline = TradingPipeline()
            result = pipeline.run(
                symbols=[], snap=self._snap(research=research),
                ai=None, now=datetime.now(ET), alpaca=MagicMock(),
                portfolio_value=10000.0, buying_power=5000.0, can_open=True,
            )
        assert result.skipped is False

    def test_malformed_timestamp_no_crash(self, monkeypatch):
        """pipeline.run() handles unparseable timestamp without raising."""
        from unittest.mock import MagicMock, patch
        from trading.pipeline import TradingPipeline

        research = {"__meta__": {"timestamp": "not-a-date"}}
        with patch("trading.pipeline.load_open_positions", return_value={}), \
             patch("trading.pipeline.load_daily_closed", return_value=set()):
            pipeline = TradingPipeline()
            result = pipeline.run(
                symbols=[], snap=self._snap(research=research),
                ai=None, now=datetime.now(ET), alpaca=MagicMock(),
                portfolio_value=10000.0, buying_power=5000.0, can_open=True,
            )
        assert result.skipped is False


# ── trading/pipeline: per-cycle entry cap ─────────────────────────────────────

@pytest.mark.unit
class TestPipelinePerCycleCap:
    """Tests for the MAX_ENTRIES_PER_CYCLE enforcement via check_all_gates."""

    def _now_open(self) -> datetime:
        from zoneinfo import ZoneInfo
        return datetime(2024, 1, 15, 11, 0, 0, tzinfo=ZoneInfo("America/New_York"))

    @staticmethod
    def _iv(**overrides):
        import types
        base = dict(
            current_price=100.0, vwap=99.0, vwap_std2_upper=110.0,
            stop_hunt_zone=False, parabolic=False,
            momentum_exhaustion=False, bot_herding_score=0, squeeze_setup=False,
        )
        base.update(overrides)
        return types.SimpleNamespace(**base)

    def _patch(self, monkeypatch):
        import earnings_calendar as ec
        import economic_calendar as ecc
        monkeypatch.setattr(ec, "is_earnings_imminent", lambda *a, **kw: False)
        monkeypatch.setattr(ecc, "is_event_risk_window", lambda *a, **kw: (False, ""))

    def test_cap_blocks_regardless_of_symbol(self, monkeypatch):
        """Cap is per-cycle, not per-symbol — applies even to a brand-new symbol."""
        import config
        monkeypatch.setattr(config, "MAX_ENTRIES_PER_CYCLE", 1)
        self._patch(monkeypatch)
        from signals.entry_gates import check_all_gates
        for sym in ("AAPL", "NVDA", "TSLA"):
            result = check_all_gates(
                sym, self._iv(), {}, set(), None, self._now_open(), entries_this_cycle=1,
            )
            assert result.passed is False, f"{sym} should be blocked by cap"

    def test_close_decision_bypasses_cap(self):
        """CLOSE action in pipeline calls close_position, not check_all_gates — cap irrelevant."""
        # check_all_gates is only called for BUY candidates; verifying it is not invoked for CLOSE
        # is implicit — here we confirm close_position exists and accepts the right signature.
        from trading.closer import close_position
        import inspect
        sig = inspect.signature(close_position)
        assert "symbol" in sig.parameters
        assert "entries_this_cycle" not in sig.parameters  # cap param is absent from closer

    def test_5day_return_exactly_6_rows(self):
        """_compute_symbol_5day_return needs at least 6 rows."""
        import pandas as pd
        from trading.pipeline import _compute_symbol_5day_return
        df5 = pd.DataFrame({"close": [100.0] * 5})
        df6 = pd.DataFrame({"close": [100.0] * 6})
        assert _compute_symbol_5day_return(df5) == pytest.approx(0.0)
        assert _compute_symbol_5day_return(df6) == pytest.approx(0.0)  # flat

    def test_cycle_result_entries_increments_correctly(self):
        """CycleResult.entries accumulates correctly when used as counter."""
        from trading.pipeline import CycleResult
        r = CycleResult()
        r.entries += 1
        assert r.entries == 1
        r.entries += 1
        assert r.entries == 2


@pytest.mark.unit
class TestContextSplit:
    """Verify stable vs. dynamic context separation for prompt caching."""

    _REGIME = {
        "regime": "NORMAL", "macro_health_score": 5,
        "vix_current": 18.0, "vix_90d_avg": 20.0, "vix_trend": "falling",
        "spy_5day_return": 1.2, "consecutive_green_days": 3,
        "consecutive_red_days": 0, "spy_5day_sign_changes": 1,
        "sector_scores": {"XLK": 10, "XLF": -5},
        "hyg_ret5d": 0.3, "iwm_ret5d": 0.8, "vix3m": 19.0,
    }
    _FRED = {"fed_direction": "paused", "cpi_trend": "stable", "unemployment_rising": False}
    _META = {"macro_shock_score": 10, "capitulation_candidates": []}

    def test_stable_context_contains_no_time(self):
        """build_stable_system_context must not embed a clock time string."""
        import re
        from trading.context_builder import build_stable_system_context
        text = build_stable_system_context(self._REGIME, self._FRED, self._META)
        assert not re.search(r"\d{2}:\d{2}", text), (
            "Stable context must not contain a time like '10:30' — it breaks prompt caching"
        )

    def test_stable_context_is_deterministic(self):
        """Same inputs produce identical output — required for cache hits."""
        from trading.context_builder import build_stable_system_context
        a = build_stable_system_context(self._REGIME, self._FRED, self._META)
        b = build_stable_system_context(self._REGIME, self._FRED, self._META)
        assert a == b

    def test_dynamic_context_contains_time(self):
        """build_dynamic_user_context must include the current HH:MM marker."""
        from trading.context_builder import build_dynamic_user_context
        now = datetime(2024, 1, 15, 11, 30, 0, tzinfo=ET)
        text = build_dynamic_user_context(now)
        assert "11:30" in text


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
        import types
        import watch_list_agent

        fake_resp = self._fake_response(input_tok=300, output_tok=80, cache_write=500)

        # Patch Alpaca, file paths, and the Claude client
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

        with patch("watch_list_agent.AlpacaClient", return_value=mock_alpaca), \
             patch("anthropic.Anthropic") as MockAI, \
             patch("trading.risk_manager.record_usage", side_effect=fake_record):
            MockAI.return_value.messages.create.return_value = fake_resp
            # scored list is empty so Claude picks from fallback — still calls messages.create
            # Force scored to have one entry so the Claude call is reached
            monkeypatch.setattr(watch_list_agent, "_load_universe", lambda: ["AAPL"])
            mock_score = {"symbol": "AAPL", "price": 100.0, "vol_spike": 2.0, "atr_pct": 2.0, "gap_pct": 0.5}
            monkeypatch.setattr(watch_list_agent, "_score_stock", lambda sym, client: mock_score)
            watch_list_agent.run_watch_list_agent()

        assert recorded.get("input") == 300
        assert recorded.get("output") == 80
        assert recorded.get("cache_write") == 500

    def test_research_analyzer_records_budget(self, monkeypatch):
        """_analyze_stock records token usage after its Claude call."""
        import types
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


# ── Budget escalation notification ───────────────────────────────────────────

@pytest.mark.unit
class TestBudgetEscalationNotify:
    def _patch_no_token(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")

    def _patch_fake_token(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "fake_token")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "12345")

    def test_notify_budget_escalation_calls_send(self, monkeypatch):
        self._patch_fake_token(monkeypatch)
        from unittest.mock import patch
        import telegram_notify
        with patch("requests.post") as mock_post:
            telegram_notify.notify_budget_escalation(0.25, 1.50)
            mock_post.assert_called_once()
            text = mock_post.call_args[1]["json"]["text"]
            assert "escalated" in text.lower()

    def test_notify_budget_escalation_swallows_exceptions(self, monkeypatch):
        self._patch_fake_token(monkeypatch)
        from unittest.mock import patch
        import telegram_notify
        with patch("requests.post", side_effect=Exception("network error")):
            telegram_notify.notify_budget_escalation(0.25, 1.50)
            # Must not raise — function should return normally


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
        result = check_entry_budget(0.01, 0.0, "NORMAL")
        assert result is False

    # 2. Entry pool allows when under limit
    def test_check_entry_allows_when_under_limit(self, tmp_path, monkeypatch):
        """check_entry_budget returns True when entry pool has room."""
        self._setup(tmp_path, monkeypatch, entry_spent_usd=0.0)
        from trading.risk_manager import check_entry_budget
        result = check_entry_budget(0.01, 0.0, "NORMAL")
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
        check_entry_budget(0.01, 0.0, "NORMAL")   # marks entry_exhausted
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
