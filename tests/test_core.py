"""
Core infrastructure unit tests: main.py, alpaca_client, utils/market_monitor.
Run with: pytest tests/test_core.py -v -m unit
No API calls — all tests use synthetic fixtures.
"""
import json
from datetime import datetime

import pytest

from utils.constants import ET


# ── main.py: crash-loop detection ────────────────────────────────────────────

@pytest.mark.unit
class TestCrashLoopDetection:
    def _write_health(self, tmp_path, **overrides):
        base = {
            "crash_count": 0,
            "window_start": datetime.now(ET).isoformat(),
            "backoff_until": None,
        }
        base.update(overrides)
        (tmp_path / "bot_health.json").write_text(json.dumps(base))

    def test_missing_file_creates_count_of_one(self, tmp_path):
        import config
        from main import _check_crash_loop
        config.BOT_HEALTH_FILE = tmp_path / "bot_health.json"
        config.CRASH_LOOP_THRESHOLD = 5
        config.CRASH_LOOP_WINDOW_MINUTES = 10
        config.CRASH_BACKOFF_MINUTES = 30
        _check_crash_loop()
        data = json.loads((tmp_path / "bot_health.json").read_text())
        assert data["crash_count"] == 1
        assert data["backoff_until"] is None

    def test_count_increments_within_window(self, tmp_path):
        import config
        from main import _check_crash_loop
        config.BOT_HEALTH_FILE = tmp_path / "bot_health.json"
        config.CRASH_LOOP_THRESHOLD = 5
        config.CRASH_LOOP_WINDOW_MINUTES = 10
        config.CRASH_BACKOFF_MINUTES = 30
        self._write_health(tmp_path, crash_count=2)
        _check_crash_loop()
        data = json.loads((tmp_path / "bot_health.json").read_text())
        assert data["crash_count"] == 3
        assert data["backoff_until"] is None

    def test_expired_window_resets_before_increment(self, tmp_path):
        import config
        from datetime import timedelta
        from main import _check_crash_loop
        config.BOT_HEALTH_FILE = tmp_path / "bot_health.json"
        config.CRASH_LOOP_THRESHOLD = 5
        config.CRASH_LOOP_WINDOW_MINUTES = 10
        config.CRASH_BACKOFF_MINUTES = 30
        old = (datetime.now(ET) - timedelta(minutes=15)).isoformat()
        self._write_health(tmp_path, crash_count=4, window_start=old)
        _check_crash_loop()
        data = json.loads((tmp_path / "bot_health.json").read_text())
        assert data["crash_count"] == 1  # reset to 0, then incremented

    def test_threshold_triggers_backoff_then_resets(self, tmp_path, monkeypatch):
        import config
        import main as main_mod
        from main import _check_crash_loop
        config.BOT_HEALTH_FILE = tmp_path / "bot_health.json"
        config.CRASH_LOOP_THRESHOLD = 3
        config.CRASH_LOOP_WINDOW_MINUTES = 10
        config.CRASH_BACKOFF_MINUTES = 30
        monkeypatch.setattr(main_mod.time, "sleep", lambda _: None)
        self._write_health(tmp_path, crash_count=2)
        _check_crash_loop()
        data = json.loads((tmp_path / "bot_health.json").read_text())
        assert data["crash_count"] == 0
        assert data["backoff_until"] is None

    def test_reset_writes_zero_count(self, tmp_path):
        import config
        from main import _reset_bot_health
        config.BOT_HEALTH_FILE = tmp_path / "bot_health.json"
        self._write_health(tmp_path, crash_count=5, backoff_until="sometime")
        _reset_bot_health()
        data = json.loads((tmp_path / "bot_health.json").read_text())
        assert data["crash_count"] == 0


# ── alpaca_client: orphan-order drain ────────────────────────────────────────

@pytest.mark.unit
class TestOrphanOrderDrain:
    def _make_client(self, monkeypatch, orders):
        from unittest.mock import MagicMock
        import config
        config.ALPACA_API_KEY = "test-key"
        config.ALPACA_SECRET_KEY = "test-secret"
        from alpaca_client import AlpacaClient
        client = AlpacaClient.__new__(AlpacaClient)
        mock_trading = MagicMock()
        mock_trading.get_orders.return_value = orders
        client._trading = mock_trading
        return client

    def _make_order(self, order_id, symbol):
        from unittest.mock import MagicMock
        o = MagicMock()
        o.id = order_id
        o.symbol = symbol
        return o

    def test_get_open_buy_orders_returns_list(self, monkeypatch):
        order = self._make_order("id-1", "APP")
        client = self._make_client(monkeypatch, [order])
        result = client.get_open_buy_orders()
        assert result == [{"id": "id-1", "symbol": "APP"}]

    def test_get_open_buy_orders_empty_when_none(self, monkeypatch):
        client = self._make_client(monkeypatch, [])
        result = client.get_open_buy_orders()
        assert result == []

    def test_cancel_order_delegates_to_alpaca(self, monkeypatch):
        from unittest.mock import MagicMock
        import config
        config.ALPACA_API_KEY = "test-key"
        config.ALPACA_SECRET_KEY = "test-secret"
        from alpaca_client import AlpacaClient
        client = AlpacaClient.__new__(AlpacaClient)
        mock_trading = MagicMock()
        client._trading = mock_trading
        client.cancel_order("order-xyz")
        mock_trading.cancel_order_by_id.assert_called_once_with("order-xyz")


# ── utils/market_monitor: polygon SPY verification ───────────────────────────

@pytest.mark.unit
class TestVerifySpyWithPolygon:
    def test_no_key_returns_none(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "POLYGON_API_KEY", "")
        from utils.market_monitor import _verify_spy_with_polygon
        assert _verify_spy_with_polygon(-3.0) is None

    def test_confirmed_drop_returns_true(self, monkeypatch):
        import config
        from unittest.mock import MagicMock
        monkeypatch.setattr(config, "POLYGON_API_KEY", "test-key")
        monkeypatch.setattr(config, "FLASH_CRASH_SPY_DROP_PCT", -2.5)
        mock_resp = MagicMock()
        mock_resp.raise_for_status = lambda: None
        mock_resp.json.return_value = {"ticker": {"todaysChangePerc": -3.1}}
        import utils.market_monitor as mm
        monkeypatch.setattr(mm.requests, "get", lambda *a, **kw: mock_resp)
        from utils.market_monitor import _verify_spy_with_polygon
        assert _verify_spy_with_polygon(-3.1) is True

    def test_bad_tick_returns_false(self, monkeypatch):
        import config
        from unittest.mock import MagicMock
        monkeypatch.setattr(config, "POLYGON_API_KEY", "test-key")
        monkeypatch.setattr(config, "FLASH_CRASH_SPY_DROP_PCT", -2.5)
        mock_resp = MagicMock()
        mock_resp.raise_for_status = lambda: None
        mock_resp.json.return_value = {"ticker": {"todaysChangePerc": -0.5}}
        import utils.market_monitor as mm
        monkeypatch.setattr(mm.requests, "get", lambda *a, **kw: mock_resp)
        from utils.market_monitor import _verify_spy_with_polygon
        assert _verify_spy_with_polygon(-2.6) is False

    def test_network_error_returns_none(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "POLYGON_API_KEY", "test-key")
        import utils.market_monitor as mm
        monkeypatch.setattr(mm.requests, "get",
                            lambda *a, **kw: (_ for _ in ()).throw(Exception("timeout")))
        from utils.market_monitor import _verify_spy_with_polygon
        assert _verify_spy_with_polygon(-3.0) is None


# ── utils/market_monitor: flash-crash two-reading protocol ───────────────────

@pytest.mark.unit
class TestFlashCrashTwoReading:
    def _write_regime(self, tmp_path, candidate=None):
        data = {
            "regime": "NORMAL",
            "flash_crash_active": False,
            "flash_crash_until": None,
            "flash_crash_candidate": candidate,
        }
        (tmp_path / "macro_regime.json").write_text(json.dumps(data))

    def test_no_candidate_writes_stage1(self, tmp_path, monkeypatch):
        import config
        config.MACRO_REGIME_FILE = tmp_path / "macro_regime.json"
        self._write_regime(tmp_path, candidate=None)
        from utils.market_monitor import _handle_flash_crash_stage2
        _handle_flash_crash_stage2(-2.7)
        data = json.loads((tmp_path / "macro_regime.json").read_text())
        assert data["flash_crash_candidate"] is not None
        assert data["flash_crash_candidate"]["spy_pct"] == -2.7

    def test_fresh_candidate_triggers_lockout(self, tmp_path, monkeypatch):
        import config
        config.MACRO_REGIME_FILE = tmp_path / "macro_regime.json"
        fresh = {"spy_pct": -2.7, "detected_at": datetime.now(ET).isoformat()}
        self._write_regime(tmp_path, candidate=fresh)
        triggered = []
        import utils.market_monitor as mm
        monkeypatch.setattr(mm, "_trigger_flash_crash_lockout", lambda: triggered.append(1))
        from utils.market_monitor import _handle_flash_crash_stage2
        _handle_flash_crash_stage2(-3.0)
        assert len(triggered) == 1

    def test_stale_candidate_overwritten_no_lockout(self, tmp_path, monkeypatch):
        import config
        from datetime import timedelta
        config.MACRO_REGIME_FILE = tmp_path / "macro_regime.json"
        old_time = (datetime.now(ET) - timedelta(minutes=20)).isoformat()
        stale = {"spy_pct": -2.7, "detected_at": old_time}
        self._write_regime(tmp_path, candidate=stale)
        triggered = []
        import utils.market_monitor as mm
        monkeypatch.setattr(mm, "_trigger_flash_crash_lockout", lambda: triggered.append(1))
        from utils.market_monitor import _handle_flash_crash_stage2
        _handle_flash_crash_stage2(-3.0)
        assert len(triggered) == 0
        data = json.loads((tmp_path / "macro_regime.json").read_text())
        new_ts = datetime.fromisoformat(data["flash_crash_candidate"]["detected_at"])
        assert (datetime.now(ET) - new_ts).total_seconds() < 5

    def test_spy_recovery_clears_candidate(self, tmp_path, monkeypatch):
        import config
        config.MACRO_REGIME_FILE = tmp_path / "macro_regime.json"
        fresh = {"spy_pct": -2.7, "detected_at": datetime.now(ET).isoformat()}
        self._write_regime(tmp_path, candidate=fresh)
        from utils.market_monitor import _clear_flash_crash_candidate
        _clear_flash_crash_candidate()
        data = json.loads((tmp_path / "macro_regime.json").read_text())
        assert data["flash_crash_candidate"] is None

    def test_set_crisis_lockout_clears_candidate(self, tmp_path, monkeypatch):
        import config
        config.MACRO_REGIME_FILE = tmp_path / "macro_regime.json"
        candidate = {"spy_pct": -3.0, "detected_at": datetime.now(ET).isoformat()}
        self._write_regime(tmp_path, candidate=candidate)
        from utils.market_monitor import _set_crisis_lockout
        _set_crisis_lockout(hours=1)
        data = json.loads((tmp_path / "macro_regime.json").read_text())
        assert data["flash_crash_active"] is True
        assert data["flash_crash_candidate"] is None


# ── trading_agent.py + trading/: persistence helpers ─────────────────────────

@pytest.mark.unit
class TestTradingAgentHelpers:
    def test_load_watch_list_returns_empty_when_missing(self, tmp_path):
        import config
        config.WATCH_LIST_FILE = tmp_path / "watch_list.txt"
        from trading.watch_loader import load_watch_list
        assert load_watch_list() == []

    def test_load_watch_list_strips_and_uppercases(self, tmp_path):
        import config
        config.WATCH_LIST_FILE = tmp_path / "watch_list.txt"
        config.WATCH_LIST_FILE.write_text("nvda\n aapl \nAMD\n")
        from trading.watch_loader import load_watch_list
        result = load_watch_list()
        assert result == ["NVDA", "AAPL", "AMD"]

    def test_load_open_positions_empty_when_no_file(self, tmp_path):
        import config
        config.OPEN_POSITIONS_FILE = tmp_path / "open_positions.json"
        from trading.journal import load_open_positions
        assert load_open_positions() == {}

    def test_save_and_load_open_positions(self, tmp_path):
        import config
        config.OPEN_POSITIONS_FILE = tmp_path / "open_positions.json"
        from trading.journal import save_open_positions, load_open_positions
        positions = {"NVDA": {"entry_price": 100.0, "qty": 5}}
        save_open_positions(positions)
        loaded = load_open_positions()
        assert loaded == positions

    def test_estimate_batch_call_cost_scales_with_batch_size(self):
        from trading.claude_caller import _estimate_batch_call_cost
        cost1 = _estimate_batch_call_cost(1)
        cost5 = _estimate_batch_call_cost(5)
        assert cost5 > cost1
        assert cost1 > 0


# ── main.py: startup exit alerts ─────────────────────────────────────────────

@pytest.mark.unit
class TestStartupExitAlerts:
    def test_exit_with_alert_calls_telegram_then_exit(self):
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
        from unittest.mock import patch
        import main
        sent = []
        with patch("main.telegram_notify._send", side_effect=lambda m: sent.append(m)):
            with patch("sys.exit"):
                main._exit_with_alert("ALPACA_API_KEY missing")
        assert len(sent) == 1
        assert "ALPACA_API_KEY missing" in sent[0]

    def test_exit_with_alert_tolerates_telegram_failure(self):
        from unittest.mock import patch
        import main
        with patch("main.telegram_notify._send", side_effect=Exception("Telegram down")):
            with patch("sys.exit") as mock_exit:
                main._exit_with_alert("some error")
        mock_exit.assert_called_once_with(1)

    def test_exit_with_alert_custom_code(self):
        from unittest.mock import patch
        import main
        with patch("main.telegram_notify._send"):
            with patch("sys.exit") as mock_exit:
                main._exit_with_alert("msg", code=2)
        mock_exit.assert_called_once_with(2)
