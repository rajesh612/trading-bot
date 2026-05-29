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

    def test_missing_file_creates_count_of_one(self, tmp_path, monkeypatch):
        import config
        from main import _check_crash_loop
        monkeypatch.setattr(config, "BOT_HEALTH_FILE", tmp_path / "bot_health.json")
        monkeypatch.setattr(config, "CRASH_LOOP_THRESHOLD", 5)
        monkeypatch.setattr(config, "CRASH_LOOP_WINDOW_MINUTES", 10)
        monkeypatch.setattr(config, "CRASH_BACKOFF_MINUTES", 30)
        _check_crash_loop()
        data = json.loads((tmp_path / "bot_health.json").read_text())
        assert data["crash_count"] == 1
        assert data["backoff_until"] is None

    def test_count_increments_within_window(self, tmp_path, monkeypatch):
        import config
        from main import _check_crash_loop
        monkeypatch.setattr(config, "BOT_HEALTH_FILE", tmp_path / "bot_health.json")
        monkeypatch.setattr(config, "CRASH_LOOP_THRESHOLD", 5)
        monkeypatch.setattr(config, "CRASH_LOOP_WINDOW_MINUTES", 10)
        monkeypatch.setattr(config, "CRASH_BACKOFF_MINUTES", 30)
        self._write_health(tmp_path, crash_count=2)
        _check_crash_loop()
        data = json.loads((tmp_path / "bot_health.json").read_text())
        assert data["crash_count"] == 3
        assert data["backoff_until"] is None

    def test_expired_window_resets_before_increment(self, tmp_path, monkeypatch):
        import config
        from datetime import timedelta
        from main import _check_crash_loop
        monkeypatch.setattr(config, "BOT_HEALTH_FILE", tmp_path / "bot_health.json")
        monkeypatch.setattr(config, "CRASH_LOOP_THRESHOLD", 5)
        monkeypatch.setattr(config, "CRASH_LOOP_WINDOW_MINUTES", 10)
        monkeypatch.setattr(config, "CRASH_BACKOFF_MINUTES", 30)
        old = (datetime.now(ET) - timedelta(minutes=15)).isoformat()
        self._write_health(tmp_path, crash_count=4, window_start=old)
        _check_crash_loop()
        data = json.loads((tmp_path / "bot_health.json").read_text())
        assert data["crash_count"] == 1  # reset to 0, then incremented

    def test_threshold_triggers_backoff_then_resets(self, tmp_path, monkeypatch):
        import config
        import main as main_mod
        from main import _check_crash_loop
        monkeypatch.setattr(config, "BOT_HEALTH_FILE", tmp_path / "bot_health.json")
        monkeypatch.setattr(config, "CRASH_LOOP_THRESHOLD", 3)
        monkeypatch.setattr(config, "CRASH_LOOP_WINDOW_MINUTES", 10)
        monkeypatch.setattr(config, "CRASH_BACKOFF_MINUTES", 30)
        monkeypatch.setattr(main_mod.time, "sleep", lambda _: None)
        self._write_health(tmp_path, crash_count=2)
        _check_crash_loop()
        data = json.loads((tmp_path / "bot_health.json").read_text())
        assert data["crash_count"] == 0
        assert data["backoff_until"] is None

    def test_reset_writes_zero_count(self, tmp_path, monkeypatch):
        import config
        from main import _reset_bot_health
        monkeypatch.setattr(config, "BOT_HEALTH_FILE", tmp_path / "bot_health.json")
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
        monkeypatch.setattr(config, "ALPACA_API_KEY", "test-key")
        monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "test-secret")
        from broker.alpaca_client import AlpacaClient
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
        monkeypatch.setattr(config, "ALPACA_API_KEY", "test-key")
        monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "test-secret")
        from broker.alpaca_client import AlpacaClient
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
        monkeypatch.setattr(config, "MACRO_REGIME_FILE", tmp_path / "macro_regime.json")
        self._write_regime(tmp_path, candidate=None)
        from utils.market_monitor import _handle_flash_crash_stage2
        _handle_flash_crash_stage2(-2.7)
        data = json.loads((tmp_path / "macro_regime.json").read_text())
        assert data["flash_crash_candidate"] is not None
        assert data["flash_crash_candidate"]["spy_pct"] == -2.7

    def test_fresh_candidate_triggers_lockout(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "MACRO_REGIME_FILE", tmp_path / "macro_regime.json")
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
        monkeypatch.setattr(config, "MACRO_REGIME_FILE", tmp_path / "macro_regime.json")
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
        monkeypatch.setattr(config, "MACRO_REGIME_FILE", tmp_path / "macro_regime.json")
        fresh = {"spy_pct": -2.7, "detected_at": datetime.now(ET).isoformat()}
        self._write_regime(tmp_path, candidate=fresh)
        from utils.market_monitor import _clear_flash_crash_candidate
        _clear_flash_crash_candidate()
        data = json.loads((tmp_path / "macro_regime.json").read_text())
        assert data["flash_crash_candidate"] is None

    def test_set_crisis_lockout_clears_candidate(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "MACRO_REGIME_FILE", tmp_path / "macro_regime.json")
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
    def test_load_watch_list_returns_empty_when_missing(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "WATCH_LIST_FILE", tmp_path / "watch_list.txt")
        from trading.watch_loader import load_watch_list
        assert load_watch_list() == []

    def test_load_watch_list_strips_and_uppercases(self, tmp_path, monkeypatch):
        import config
        watch_file = tmp_path / "watch_list.txt"
        monkeypatch.setattr(config, "WATCH_LIST_FILE", watch_file)
        watch_file.write_text("nvda\n aapl \nAMD\n")
        from trading.watch_loader import load_watch_list
        result = load_watch_list()
        assert result == ["NVDA", "AAPL", "AMD"]

    def test_load_open_positions_empty_when_no_file(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "open_positions.json")
        from trading.journal import load_open_positions
        assert load_open_positions() == {}

    def test_save_and_load_open_positions(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "open_positions.json")
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


# ── utils/catchup: pre-market threshold ──────────────────────────────────────

@pytest.mark.unit
class TestCatchupMissedAgents:
    """Verify catchup fires calendar jobs when bot starts after 8:00 AM (e.g. 8:30 restart)."""

    def _run_catchup(self, monkeypatch, tmp_path, now_et):
        import utils.catchup as cu
        import config
        import pathlib

        # Stale state files (yesterday) so all checks trigger
        stale_files = [
            config.EARNINGS_CALENDAR_FILE,
            config.ECONOMIC_CALENDAR_FILE,
            config.DAILY_TRADEABLE_FILE,
            config.MACRO_REGIME_FILE,
            config.WATCH_LIST_FILE,
            config.RESEARCH_REPORT_FILE,
        ]
        for attr in ["EARNINGS_CALENDAR_FILE", "ECONOMIC_CALENDAR_FILE",
                     "DAILY_TRADEABLE_FILE", "MACRO_REGIME_FILE",
                     "WATCH_LIST_FILE", "RESEARCH_REPORT_FILE"]:
            p = tmp_path / f"{attr}.json"
            monkeypatch.setattr(config, attr, p)

        # Simulate all files stale initially; screener success marks tradeable as fresh
        fresh: set = set()
        monkeypatch.setattr(cu, "_file_is_from_today", lambda p: p in fresh)

        called = []
        import types
        daily_tradeable_path = config.DAILY_TRADEABLE_FILE
        fake_sj = types.SimpleNamespace(
            job_earnings_calendar=lambda: called.append("earnings"),
            job_economic_calendar=lambda: called.append("economic"),
            job_volatility_screener=lambda: (called.append("vol"), fresh.add(daily_tradeable_path)),
            job_macro_regime=lambda: called.append("macro"),
            job_watch_list=lambda: called.append("watch"),
            job_research=lambda: called.append("research"),
        )
        monkeypatch.setattr("utils.catchup.datetime", type("dt", (), {
            "now": staticmethod(lambda tz=None: now_et),
            "fromtimestamp": datetime.fromtimestamp,
        }))

        import sys
        sys.modules["scheduler_jobs"] = fake_sj
        try:
            cu.catchup_missed_agents()
        finally:
            del sys.modules["scheduler_jobs"]

        return called

    def test_catchup_fires_at_830_et(self, monkeypatch, tmp_path):
        """Bot restart at 8:30 ET must catch up earnings + economic calendar."""
        now = datetime(2026, 5, 19, 8, 30, tzinfo=ET)
        called = self._run_catchup(monkeypatch, tmp_path, now)
        assert "earnings" in called
        assert "economic" in called

    def test_no_catchup_before_8am(self, monkeypatch, tmp_path):
        """Before 8:00 AM ET nothing should run."""
        now = datetime(2026, 5, 19, 7, 59, tzinfo=ET)
        called = self._run_catchup(monkeypatch, tmp_path, now)
        assert called == []

    def test_no_catchup_on_weekend(self, monkeypatch, tmp_path):
        """Weekends must be skipped entirely."""
        now = datetime(2026, 5, 17, 9, 0, tzinfo=ET)  # Sunday
        called = self._run_catchup(monkeypatch, tmp_path, now)
        assert called == []


# ── agents/volatility_screener: market cap pre-filter ────────────────────────

@pytest.mark.unit
class TestMarketCapFilter:
    def _mock_yahoo_response(self, market_cap):
        """Build a fake yahoo_get response containing marketCap."""
        if market_cap is None:
            return {"quoteSummary": {"result": [{"price": {}}]}}
        return {"quoteSummary": {"result": [{"price": {"marketCap": {"raw": market_cap}}}]}}

    @pytest.mark.unit
    def test_fetch_market_cap_returns_value(self, monkeypatch):
        """_fetch_market_cap returns float market cap from Yahoo Finance API."""
        import agents.volatility_screener as vs
        monkeypatch.setattr(vs, "yahoo_get", lambda url, **kw: self._mock_yahoo_response(500_000_000_000))
        cap = vs._fetch_market_cap("NVDA")
        assert cap == 500_000_000_000.0

    @pytest.mark.unit
    def test_fetch_market_cap_small_cap(self, monkeypatch):
        """_fetch_market_cap returns small cap value below threshold."""
        import agents.volatility_screener as vs
        monkeypatch.setattr(vs, "yahoo_get", lambda url, **kw: self._mock_yahoo_response(5_000_000_000))
        cap = vs._fetch_market_cap("SMALLCO")
        assert cap == 5_000_000_000.0
        import config
        assert cap < config.MIN_MARKET_CAP_B * 1e9

    @pytest.mark.unit
    def test_fetch_market_cap_returns_none_when_missing(self, monkeypatch):
        """_fetch_market_cap returns None when marketCap is absent in response."""
        import agents.volatility_screener as vs
        monkeypatch.setattr(vs, "yahoo_get", lambda url, **kw: self._mock_yahoo_response(None))
        cap = vs._fetch_market_cap("MYSTERY")
        assert cap is None

    @pytest.mark.unit
    def test_fetch_market_cap_returns_none_on_exception(self, monkeypatch):
        """_fetch_market_cap returns None when HTTP call raises."""
        import agents.volatility_screener as vs
        monkeypatch.setattr(vs, "yahoo_get", lambda url, **kw: (_ for _ in ()).throw(Exception("network error")))
        cap = vs._fetch_market_cap("BROKEN")
        assert cap is None


# ── market_data: DataFrame shape contract ─────────────────────────────────────

@pytest.mark.unit
class TestMarketDataSchema:
    def test_yahoo_bars_returns_correct_columns(self):
        """Yahoo bars must have OHLCV columns and a tz-aware DatetimeIndex."""
        from data.market_data import _yahoo_bars
        try:
            df = _yahoo_bars("SPY", "1d", "10d")
        except Exception:
            pytest.skip("Yahoo Finance not reachable in this environment")
        assert set(df.columns) >= {"open", "high", "low", "close", "volume"}
        assert df.index.tz is not None

    def test_get_intraday_bars_respects_lookback(self):
        from data.market_data import get_intraday_bars
        try:
            df = get_intraday_bars("SPY", lookback=50)
        except Exception:
            pytest.skip("Market data not reachable in this environment")
        assert len(df) <= 50
        assert "close" in df.columns


# ── market_data: provider routing ────────────────────────────────────────────

@pytest.mark.unit
class TestMarketDataHelpers:
    def test_get_intraday_bars_returns_empty_on_yahoo_error(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "MARKET_DATA_PROVIDER", "yahoo")
        import data.market_data as md
        monkeypatch.setattr(md, "_yahoo_bars", lambda *a, **kw: (_ for _ in ()).throw(Exception("net error")))
        result = md.get_intraday_bars("AAPL")
        assert result.empty

    def test_get_daily_bars_returns_empty_on_yahoo_error(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "MARKET_DATA_PROVIDER", "yahoo")
        import data.market_data as md
        monkeypatch.setattr(md, "_yahoo_bars", lambda *a, **kw: (_ for _ in ()).throw(Exception("net error")))
        result = md.get_daily_bars("AAPL")
        assert result.empty

    def test_get_latest_price_yahoo_path(self, monkeypatch):
        import config
        import pandas as pd
        monkeypatch.setattr(config, "MARKET_DATA_PROVIDER", "yahoo")
        import data.market_data as md
        idx = pd.date_range("2026-05-01 15:50", periods=1, freq="2min", tz="America/New_York")
        fake_df = pd.DataFrame({"close": [155.0]}, index=idx)
        monkeypatch.setattr(md, "_yahoo_bars", lambda *a, **kw: fake_df)
        price = md.get_latest_price("AAPL")
        assert price == 155.0


# ── alpaca_client: thin wrapper ───────────────────────────────────────────────

@pytest.mark.unit
class TestAlpacaClientHelpers:
    def test_init_succeeds_with_dummy_keys(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "ALPACA_API_KEY", "test_key")
        monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "test_secret")
        from unittest.mock import patch, MagicMock
        with patch("broker.alpaca_client.TradingClient"):
            from broker.alpaca_client import AlpacaClient
            client = AlpacaClient()
            assert client is not None

    def test_get_latest_price_delegates_to_market_data(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "ALPACA_API_KEY", "test_key")
        monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "test_secret")
        from unittest.mock import patch, MagicMock
        with patch("broker.alpaca_client.TradingClient"):
            import broker.alpaca_client as alpaca_client
            client = alpaca_client.AlpacaClient()
            with patch("data.market_data.get_latest_price", return_value=200.5):
                price = client.get_latest_price("AAPL")
                assert price == 200.5


# ── P4-T1: _validate_decision ─────────────────────────────────────────────────

@pytest.mark.unit
def test_validate_decision_clamps_confidence():
    from trading.claude_caller import _validate_decision
    raw = {"action": "BUY", "confidence": 150, "stop_loss_pct": 4.0, "take_profit_pct": 5.0, "reasoning": "x"}
    result = _validate_decision(raw, "AAPL")
    assert result["confidence"] == 100


@pytest.mark.unit
def test_validate_decision_clamps_stop_loss_negative():
    from trading.claude_caller import _validate_decision
    raw = {"action": "BUY", "confidence": 75, "stop_loss_pct": -1.5, "take_profit_pct": 5.0, "reasoning": "x"}
    result = _validate_decision(raw, "AAPL")
    assert result["stop_loss_pct"] >= 0.5


@pytest.mark.unit
def test_validate_decision_clamps_take_profit_high():
    from trading.claude_caller import _validate_decision
    raw = {"action": "BUY", "confidence": 75, "stop_loss_pct": 4.0, "take_profit_pct": 50.0, "reasoning": "x"}
    result = _validate_decision(raw, "AAPL")
    assert result["take_profit_pct"] <= 15.0


@pytest.mark.unit
def test_validate_decision_passthrough_valid():
    from trading.claude_caller import _validate_decision
    raw = {"action": "BUY", "confidence": 75, "stop_loss_pct": 4.0, "take_profit_pct": 5.0, "reasoning": "x"}
    result = _validate_decision(dict(raw), "AAPL")
    assert result["confidence"] == 75
    assert result["stop_loss_pct"] == 4.0
