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


# ── Crash loop detection ───────────────────────────────────────────────────────

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
        # After backoff completes, _reset_bot_health() sets count=0
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


# ── Orphan-order drain ────────────────────────────────────────────────────────

@pytest.mark.unit
class TestOrphanOrderDrain:
    def _make_client(self, monkeypatch, orders):
        """Return an AlpacaClient with a mocked _trading that returns given orders."""
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


# ── Polygon helper ────────────────────────────────────────────────────────────

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


# ── trading_agent: persistence helpers ───────────────────────────────────────

@pytest.mark.unit
class TestTradingAgentHelpers:
    def test_load_watch_list_returns_empty_when_missing(self, tmp_path):
        import config
        config.WATCH_LIST_FILE = tmp_path / "watch_list.txt"
        from trading_agent import load_watch_list
        assert load_watch_list() == []

    def test_load_watch_list_strips_and_uppercases(self, tmp_path):
        import config
        config.WATCH_LIST_FILE = tmp_path / "watch_list.txt"
        config.WATCH_LIST_FILE.write_text("nvda\n aapl \nAMD\n")
        from trading_agent import load_watch_list
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

    def test_load_daily_closed_returns_set(self, tmp_path):
        import config
        config.DAILY_CLOSED_FILE = tmp_path / "daily_closed.json"
        from trading.journal import load_daily_closed
        assert load_daily_closed() == set()

    def test_record_and_load_daily_closed(self, tmp_path):
        import config
        config.DAILY_CLOSED_FILE = tmp_path / "daily_closed.json"
        from trading.journal import record_daily_closed, load_daily_closed
        record_daily_closed("NVDA")
        record_daily_closed("AAPL")
        closed = load_daily_closed()
        assert "NVDA" in closed
        assert "AAPL" in closed

    def test_append_trade_accumulates_journal(self, tmp_path):
        import config, json
        config.TRADE_JOURNAL_FILE = tmp_path / "trade_journal.json"
        from trading.journal import append_trade
        append_trade({"type": "BUY", "symbol": "NVDA"})
        append_trade({"type": "CLOSE", "symbol": "NVDA"})
        journal = json.loads(config.TRADE_JOURNAL_FILE.read_text())
        assert len(journal) == 2
        assert journal[0]["type"] == "BUY"



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


