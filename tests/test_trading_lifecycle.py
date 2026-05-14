"""
Startup alerts, force-close, profit-lock, and journal lock unit tests.
Run with: pytest tests/test_trading_lifecycle.py -v -m unit
"""
import json
import os
from datetime import datetime

import pytest

from utils.constants import ET


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
        from trading import closer as trading_agent
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


# ── trading/watch_loader: load_watch_list ────────────────────────────────────

@pytest.mark.unit
def test_load_watch_list_returns_symbols(tmp_path, monkeypatch):
    """load_watch_list strips whitespace and uppercases symbols."""
    from trading.watch_loader import load_watch_list
    import config
    monkeypatch.setattr(config, "WATCH_LIST_FILE", tmp_path / "watch_list.txt")
    (tmp_path / "watch_list.txt").write_text("AAPL\nnvda\n  MSFT  \n")
    result = load_watch_list()
    assert result == ["AAPL", "NVDA", "MSFT"]


@pytest.mark.unit
def test_load_watch_list_missing_file_returns_empty(tmp_path, monkeypatch):
    """load_watch_list returns empty list when file does not exist."""
    from trading.watch_loader import load_watch_list
    import config
    monkeypatch.setattr(config, "WATCH_LIST_FILE", tmp_path / "nonexistent.txt")
    result = load_watch_list()
    assert result == []


# ── trading/monitor: run_position_monitor ───────────────────────────────────

@pytest.mark.unit
def test_run_position_monitor_noop_when_no_positions(tmp_path):
    """Monitor returns immediately when there are no open positions."""
    from unittest.mock import patch
    with patch("trading.monitor.load_open_positions", return_value={}):
        from trading.monitor import run_position_monitor
        run_position_monitor()  # should not raise


# ── trading/cycle: run_trading_cycle ─────────────────────────────────────────

@pytest.mark.unit
def test_run_trading_cycle_skips_empty_watch_list(tmp_path):
    """Cycle returns immediately when watch list is empty."""
    from unittest.mock import patch, MagicMock
    mock_account = MagicMock()
    mock_account.portfolio_value = "10000"
    mock_account.buying_power = "5000"
    mock_account.last_equity = "10000"
    with patch("trading.cycle.AlpacaClient") as mock_alpaca_cls, \
         patch("trading.cycle.load_watch_list", return_value=[]), \
         patch("trading.cycle.load_macro_regime", return_value={"regime": "NORMAL"}), \
         patch("trading.cycle.load_trading_params", return_value={
             "entry_start_hour": 10, "entry_end_hour": 15,
             "rsi_buy_max": 72, "rsi_buy_max_high_vol": 65,
             "high_vol_position_mult": 0.5,
         }):
        mock_alpaca_cls.return_value.get_account.return_value = mock_account
        from trading.cycle import run_trading_cycle
        run_trading_cycle()  # should not raise
