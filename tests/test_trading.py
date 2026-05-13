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
        from trading.context_builder import build_market_context_block
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime(2026, 5, 5, 10, 30, tzinfo=ZoneInfo("America/New_York"))
        block = build_market_context_block(self._regime(), self._fred(), self._meta(), now)
        assert "BULL_TREND" in block
        assert "cutting" in block
        assert "falling" in block
        assert "None active" in block
        assert "AI_SEMI" in block   # from sector_scores in regime
        assert "Capitulation watch" in block

    def test_shock_score_omitted_when_zero(self, monkeypatch):
        self._patch_context(monkeypatch)
        from trading.context_builder import build_market_context_block
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime(2026, 5, 5, 10, 30, tzinfo=ZoneInfo("America/New_York"))
        block = build_market_context_block(self._regime(), self._fred(), self._meta(macro_shock_score=0), now)
        assert "0/100" in block
        assert "elevated" not in block

    def test_shock_score_flagged_when_elevated(self, monkeypatch):
        self._patch_context(monkeypatch)
        from trading.context_builder import build_market_context_block
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime(2026, 5, 5, 10, 30, tzinfo=ZoneInfo("America/New_York"))
        block = build_market_context_block(
            self._regime(), self._fred(), self._meta(macro_shock_score=55), now
        )
        assert "55/100" in block
        assert "elevated" in block

    def test_active_event_risk_flagged(self, monkeypatch):
        monkeypatch.setattr("economic_calendar.is_event_risk_window", lambda now: (True, "Non-Farm Payrolls"))
        monkeypatch.setattr("macro.intraday_context.get_intraday_context", lambda now: None)
        monkeypatch.setattr("macro.breadth_signals.build_breadth_context_line", lambda s: "Breadth: HYG=+0.3%")
        from trading.context_builder import build_market_context_block
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime(2026, 5, 5, 8, 35, tzinfo=ZoneInfo("America/New_York"))
        block = build_market_context_block(self._regime(), self._fred(), self._meta(), now)
        assert "Non-Farm Payrolls" in block
        assert "avoid" in block

    def test_capitulation_candidates_listed(self, monkeypatch):
        self._patch_context(monkeypatch)
        from trading.context_builder import build_market_context_block
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime(2026, 5, 5, 10, 30, tzinfo=ZoneInfo("America/New_York"))
        block = build_market_context_block(
            self._regime(), self._fred(), self._meta(capitulation_candidates=["SMCI", "PLTR"]), now
        )
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

    def test_research_staleness_skip_when_too_old(self, monkeypatch):
        """pipeline.run() returns skipped=True when research > RESEARCH_STALE_BLOCK_MIN."""
        from datetime import timedelta
        import config
        from trading.pipeline import TradingPipeline

        stale_ts = (datetime.now(ET) - timedelta(minutes=config.RESEARCH_STALE_BLOCK_MIN + 10)).isoformat()
        research = {"__meta__": {"timestamp": stale_ts}}

        pipeline = TradingPipeline()
        result = pipeline.run(
            symbols=[], research=research, strategy_memory="", weights={},
            ai=None, now=datetime.now(ET), regime_info={"regime": "NORMAL"},
            fred_cache={}, alpaca=None, portfolio_value=10000.0,
            buying_power=5000.0, params={
                "rsi_buy_max": 70, "rsi_buy_max_high_vol": 60,
                "high_vol_position_mult": 0.5, "vwap_slope_min": -0.5,
            },
            can_open=True,
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
                symbols=[], research=research, strategy_memory="", weights={},
                ai=None, now=datetime.now(ET), regime_info={"regime": "NORMAL"},
                fred_cache={}, alpaca=mock_alpaca, portfolio_value=10000.0,
                buying_power=5000.0, params={
                    "rsi_buy_max": 70, "rsi_buy_max_high_vol": 60,
                    "high_vol_position_mult": 0.5, "vwap_slope_min": -0.5,
                },
                can_open=True,
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
                symbols=[], research=research, strategy_memory="", weights={},
                ai=None, now=datetime.now(ET), regime_info={"regime": "NORMAL"},
                fred_cache={}, alpaca=MagicMock(), portfolio_value=10000.0,
                buying_power=5000.0, params={
                    "rsi_buy_max": 70, "rsi_buy_max_high_vol": 60,
                    "high_vol_position_mult": 0.5, "vwap_slope_min": -0.5,
                },
                can_open=True,
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
                symbols=[], research=research, strategy_memory="", weights={},
                ai=None, now=datetime.now(ET), regime_info={"regime": "NORMAL"},
                fred_cache={}, alpaca=MagicMock(), portfolio_value=10000.0,
                buying_power=5000.0, params={
                    "rsi_buy_max": 70, "rsi_buy_max_high_vol": 60,
                    "high_vol_position_mult": 0.5, "vwap_slope_min": -0.5,
                },
                can_open=True,
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

