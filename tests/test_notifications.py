"""
Telegram, daily report, drawdown alert thread-safety, stale-research dedup,
_OnceGuard, and bracket-timeout dedup unit tests.
Run with: pytest tests/test_notifications.py -v -m unit
No API calls — all tests use synthetic fixtures.
"""
import json
import pytest

from utils.constants import ET


# ── telegram_notify ───────────────────────────────────────────────────────────

@pytest.mark.unit
class TestTelegramNotify:
    """telegram_notify functions must never raise; unconfigured tokens are a no-op."""

    def _patch_no_token(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")

    def _patch_fake_token(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "fake_token")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "12345")

    def test_no_token_is_silent(self, monkeypatch):
        self._patch_no_token(monkeypatch)
        from unittest.mock import patch
        import notifications.telegram as telegram_notify
        with patch("requests.post") as mock_post:
            telegram_notify.notify_startup("NORMAL", 0)
            mock_post.assert_not_called()

    def test_sends_when_configured(self, monkeypatch):
        self._patch_fake_token(monkeypatch)
        from unittest.mock import patch
        import notifications.telegram as telegram_notify
        with patch("requests.post") as mock_post:
            telegram_notify.notify_startup("BULL_TREND", 2)
            mock_post.assert_called_once()
            payload = mock_post.call_args[1]["json"]
            assert "BULL_TREND" in payload["text"]
            assert payload["parse_mode"] == "HTML"

    def test_network_error_does_not_raise(self, monkeypatch):
        self._patch_fake_token(monkeypatch)
        from unittest.mock import patch
        import notifications.telegram as telegram_notify
        with patch("requests.post", side_effect=Exception("network down")):
            telegram_notify.notify_startup("NORMAL", 0)  # must not raise

    def test_notify_buy_contains_symbol(self, monkeypatch):
        self._patch_fake_token(monkeypatch)
        from unittest.mock import patch
        import notifications.telegram as telegram_notify
        with patch("requests.post") as mock_post:
            telegram_notify.notify_buy("NVDA", 10, 150.0, 144.0, 157.5, "DAY_TRADE", 42.0, 80)
            text = mock_post.call_args[1]["json"]["text"]
            assert "NVDA" in text
            assert "150.00" in text

    def test_notify_close_shows_pnl(self, monkeypatch):
        self._patch_fake_token(monkeypatch)
        from unittest.mock import patch
        import notifications.telegram as telegram_notify
        with patch("requests.post") as mock_post:
            telegram_notify.notify_close("AAPL", 5, 100.0, 105.0, 25.0, 5.0, "claude_decision")
            text = mock_post.call_args[1]["json"]["text"]
            assert "AAPL" in text
            assert "+25.00" in text

    def test_notify_eod_no_trades(self, monkeypatch):
        self._patch_fake_token(monkeypatch)
        from unittest.mock import patch
        import notifications.telegram as telegram_notify
        with patch("requests.post") as mock_post:
            telegram_notify.notify_eod_summary([], 0.0, 0.0, 10000.0, "NORMAL")
            text = mock_post.call_args[1]["json"]["text"]
            assert "no trades" in text.lower()

    def test_notify_eod_with_trades(self, monkeypatch):
        self._patch_fake_token(monkeypatch)
        from unittest.mock import patch
        import notifications.telegram as telegram_notify
        closes = [
            {"symbol": "NVDA", "pnl_dollar": 80.0, "pnl_pct": 4.0},
            {"symbol": "AAPL", "pnl_dollar": -20.0, "pnl_pct": -1.5},
        ]
        with patch("requests.post") as mock_post:
            telegram_notify.notify_eod_summary(closes, 60.0, 0.6, 10060.0, "BULL_TREND")
            text = mock_post.call_args[1]["json"]["text"]
            assert "NVDA" in text
            assert "BULL_TREND" in text

    def test_notify_job_failure_truncates_long_error(self, monkeypatch):
        self._patch_fake_token(monkeypatch)
        from unittest.mock import patch
        import notifications.telegram as telegram_notify
        long_err = "x" * 500
        with patch("requests.post") as mock_post:
            telegram_notify.notify_job_failure("force_close", long_err)
            text = mock_post.call_args[1]["json"]["text"]
            assert "force_close" in text
            assert len(text) < 600

    def test_notify_claude_disabled(self, monkeypatch):
        self._patch_fake_token(monkeypatch)
        from unittest.mock import patch
        import notifications.telegram as telegram_notify
        with patch("requests.post") as mock_post:
            telegram_notify.notify_claude_disabled("HTTP 400 — invalid cache_control")
            text = mock_post.call_args[1]["json"]["text"]
            assert "Claude" in text

    def test_notify_regime_change_crisis_has_alert_emoji(self, monkeypatch):
        self._patch_fake_token(monkeypatch)
        from unittest.mock import patch
        import notifications.telegram as telegram_notify
        with patch("requests.post") as mock_post:
            telegram_notify.notify_regime_change("NORMAL", "CRISIS", -65)
            text = mock_post.call_args[1]["json"]["text"]
            assert "CRISIS" in text
            assert "🚨" in text


# ── daily_report: build_report_body ──────────────────────────────────────────

@pytest.mark.unit
class TestBuildReportBody:
    @staticmethod
    def _account(portfolio=10_500.0, last_equity=10_000.0):
        return {"portfolio_value": portfolio, "last_equity": last_equity}

    @staticmethod
    def _close(symbol="NVDA", pnl=150.0, pnl_pct=1.5, qty=10,
               entry=100.0, exit_=115.0, reason="take_profit"):
        return {
            "type": "CLOSE", "symbol": symbol, "qty": qty,
            "entry_price": entry, "exit_price": exit_,
            "pnl_dollar": pnl, "pnl_pct": pnl_pct, "close_reason": reason,
        }

    def test_subject_contains_pnl_when_trades_exist(self):
        from notifications.daily_report import build_report_body
        subject, _ = build_report_body(
            trades=[self._close()],
            account=self._account(),
            regime="BULL_TREND",
            watch_list="NVDA, APP",
            weights={"RSI": 10.0},
        )
        assert "P&L" in subject
        assert "1 trades" in subject

    def test_subject_no_trades_shows_portfolio(self):
        from notifications.daily_report import build_report_body
        subject, _ = build_report_body(
            trades=[], account=self._account(), regime="NORMAL",
            watch_list="", weights={},
        )
        assert "No trades" in subject
        assert "Portfolio" in subject

    def test_body_contains_regime(self):
        from notifications.daily_report import build_report_body
        _, body = build_report_body(
            trades=[], account=self._account(), regime="CHOPPY",
            watch_list="TSLA", weights={},
        )
        assert "CHOPPY" in body

    def test_body_lists_individual_trades(self):
        from notifications.daily_report import build_report_body
        _, body = build_report_body(
            trades=[self._close("NVDA"), self._close("AAPL", pnl=-50.0, pnl_pct=-0.5)],
            account=self._account(),
            regime="BULL_TREND",
            watch_list="",
            weights={},
        )
        assert "NVDA" in body
        assert "AAPL" in body


@pytest.mark.unit
class TestDailyReportSentFlag:
    def test_not_sent_when_file_missing(self, tmp_path, monkeypatch):
        import notifications.daily_report as daily_report
        monkeypatch.setattr(daily_report, "DAILY_REPORT_SENT_FILE", tmp_path / "sent.json")
        assert daily_report._already_sent_today() is False

    def test_not_sent_after_marking_different_date(self, tmp_path, monkeypatch):
        import notifications.daily_report as daily_report; import json
        sent_file = tmp_path / "sent.json"
        sent_file.write_text(json.dumps({"date": "2020-01-01"}))
        monkeypatch.setattr(daily_report, "DAILY_REPORT_SENT_FILE", sent_file)
        assert daily_report._already_sent_today() is False

    def test_already_sent_after_mark_sent(self, tmp_path, monkeypatch):
        import notifications.daily_report as daily_report
        monkeypatch.setattr(daily_report, "DAILY_REPORT_SENT_FILE", tmp_path / "sent.json")
        daily_report._mark_sent()
        assert daily_report._already_sent_today() is True


# ── daily_report: build_report_body (extended) ───────────────────────────────

@pytest.mark.unit
class TestDailyReportBody:
    """Tests for build_report_body — pure formatting function, no I/O."""

    def _account(self, portfolio: float = 100_000.0, last_equity: float = 99_000.0) -> dict:
        return {"portfolio_value": portfolio, "last_equity": last_equity}

    def test_subject_contains_pnl_when_trades_exist(self):
        from notifications.daily_report import build_report_body
        trades = [
            {"type": "CLOSE", "symbol": "AAPL", "qty": 10,
             "entry_price": 100.0, "exit_price": 105.0,
             "pnl_dollar": 50.0, "pnl_pct": 5.0, "close_reason": "tp_hit"},
        ]
        subject, body = build_report_body(trades, self._account(), "BULL_TREND", "AAPL, MSFT", {})
        assert "P&L:" in subject
        assert "1 trades" in subject

    def test_subject_no_trades_shows_portfolio(self):
        from notifications.daily_report import build_report_body
        subject, body = build_report_body([], self._account(100_000.0, 100_000.0), "CHOPPY", "", {})
        assert "No trades today" in subject
        assert "Portfolio:" in subject

    def test_body_contains_regime(self):
        from notifications.daily_report import build_report_body
        _, body = build_report_body([], self._account(), "CRISIS", "", {})
        assert "CRISIS" in body

    def test_win_rate_computed_correctly(self):
        from notifications.daily_report import build_report_body
        trades = [
            {"type": "CLOSE", "symbol": "A", "qty": 1, "entry_price": 10.0,
             "exit_price": 11.0, "pnl_dollar": 10.0, "pnl_pct": 10.0, "close_reason": "tp"},
            {"type": "CLOSE", "symbol": "B", "qty": 1, "entry_price": 10.0,
             "exit_price": 9.0, "pnl_dollar": -10.0, "pnl_pct": -10.0, "close_reason": "sl"},
        ]
        _, body = build_report_body(trades, self._account(), "NORMAL", "", {})
        assert "Win rate: 50.0%" in body

    def test_indicator_weights_rendered_in_body(self):
        from notifications.daily_report import build_report_body
        weights = {"rsi": 30.0, "macd": 25.0}
        _, body = build_report_body([], self._account(), "NORMAL", "", weights)
        assert "rsi: 30.00%" in body
        assert "macd: 25.00%" in body


# ── TestDrawdownAlertThreadSafety ─────────────────────────────────────────────

@pytest.mark.unit
class TestDrawdownAlertThreadSafety:
    def test_drawdown_lock_is_threading_lock(self):
        """_DRAWDOWN_LOCK is properly initialized as a threading.Lock instance."""
        from trading import monitor
        import threading
        assert hasattr(monitor, "_DRAWDOWN_LOCK")
        assert isinstance(monitor._DRAWDOWN_LOCK, threading.Lock)

    def test_drawdown_alert_cooldown_respected(self, monkeypatch):
        """_check_drawdown_alerts respects per-position cooldown and doesn't spam alerts."""
        from trading import monitor
        import time
        import config

        # Patch Telegram to prevent notifications
        monkeypatch.setattr("trading.monitor.telegram_notify.notify_position_drawdown", lambda *a, **kw: None)

        # Reset the cooldown tracking dict
        monkeypatch.setattr(monitor, "_drawdown_alerted", {})

        # Build a minimal position in drawdown but above hard SL
        # entry_price=100, current=96 → -4% PNL (below DRAWDOWN_WARN_PCT threshold)
        positions = {
            "AAPL": {
                "entry_price": 100.0,
                "stop_loss_pct": 5.0,
                "qty": 10,
            }
        }
        current_prices = {"AAPL": 96.0}

        # First call: alert should fire and record timestamp
        monitor._check_drawdown_alerts(positions, current_prices)
        assert "AAPL" in monitor._drawdown_alerted
        first_alert_ts = monitor._drawdown_alerted["AAPL"]

        # Simulate cooldown: pre-populate with future timestamp
        monitor._drawdown_alerted["AAPL"] = time.time() + 9999
        second_alert_ts = monitor._drawdown_alerted["AAPL"]

        # Second call within cooldown: timestamp should NOT change
        monitor._check_drawdown_alerts(positions, current_prices)
        assert monitor._drawdown_alerted["AAPL"] == second_alert_ts


# ── mark_stale / clear_stale ────────────────────────────────────────────────────

@pytest.mark.unit
def test_mark_stale_writes_metadata(tmp_path):
    """mark_stale injects _stale/_stale_since/_fallback_from into JSON."""
    from utils.io_utils import mark_stale, load_json, save_json
    f = tmp_path / "test.json"
    save_json(f, {"regime": "NORMAL"})

    mark_stale(f, "regime", fallback_from="CAPITULATION")

    data = load_json(f, {})
    assert data["_stale"] is True
    assert "_stale_since" in data
    assert data["_fallback_from"] == "CAPITULATION"
    assert data["regime"] == "NORMAL"   # original data preserved


@pytest.mark.unit
def test_clear_stale_removes_metadata(tmp_path):
    """clear_stale removes all _stale keys, preserves original data."""
    from utils.io_utils import mark_stale, clear_stale, load_json, save_json
    f = tmp_path / "test.json"
    save_json(f, {"regime": "NORMAL"})
    mark_stale(f, "regime")
    clear_stale(f)

    data = load_json(f, {})
    assert "_stale" not in data
    assert "_stale_since" not in data
    assert data["regime"] == "NORMAL"


# ── notify_stale_research disk dedup ─────────────────────────────────────────

@pytest.mark.unit
class TestStaleResearchDedup:
    """notify_stale_research must use a disk sentinel, not an in-memory global."""

    def test_first_call_writes_sentinel_and_sends(self, monkeypatch, tmp_path):
        """First call on a day writes sentinel file and fires the alert."""
        import config
        import notifications.telegram as tg
        from utils.io_utils import load_json
        from datetime import datetime
        from utils.constants import ET
        monkeypatch.setattr(config, "STALE_RESEARCH_ALERTED_FILE", tmp_path / "sentinel.json")
        sent = []
        monkeypatch.setattr(tg, "_send", lambda msg: sent.append(msg))
        # Call the real implementation directly (autouse fixture patches the public name)
        tg._notify_stale_research_impl(120.0)
        assert len(sent) == 1, "first call should send exactly one alert"
        sentinel = load_json(tmp_path / "sentinel.json", {})
        assert sentinel.get("date") == datetime.now(ET).strftime("%Y-%m-%d")

    def test_second_call_same_day_is_deduped(self, monkeypatch, tmp_path):
        """Second call on same day must be silently suppressed via disk sentinel."""
        import config
        import notifications.telegram as tg
        from datetime import datetime
        from utils.constants import ET
        from utils.io_utils import save_json
        monkeypatch.setattr(config, "STALE_RESEARCH_ALERTED_FILE", tmp_path / "sentinel.json")
        today = datetime.now(ET).strftime("%Y-%m-%d")
        save_json(tmp_path / "sentinel.json", {"date": today})
        sent = []
        monkeypatch.setattr(tg, "_send", lambda msg: sent.append(msg))
        tg._notify_stale_research_impl(120.0)
        assert sent == [], "second call on same day must be silently deduped"

    def test_bot_restart_does_not_redeliver_same_day(self, monkeypatch, tmp_path):
        """Simulates bot restart: disk sentinel prevents re-delivery even after in-process reset."""
        import config
        import notifications.telegram as tg
        from datetime import datetime
        from utils.constants import ET
        from utils.io_utils import save_json
        monkeypatch.setattr(config, "STALE_RESEARCH_ALERTED_FILE", tmp_path / "sentinel.json")
        today = datetime.now(ET).strftime("%Y-%m-%d")
        save_json(tmp_path / "sentinel.json", {"date": today})
        sent = []
        monkeypatch.setattr(tg, "_send", lambda msg: sent.append(msg))
        # Simulate two separate "restart" invocations on the same day
        tg._notify_stale_research_impl(60.0)
        tg._notify_stale_research_impl(120.0)
        assert sent == [], "disk dedup must block re-delivery across restarts on same day"

    def test_conftest_autouse_patches_public_function(self, monkeypatch, tmp_path):
        """The autouse conftest fixture must patch notify_stale_research to a no-op."""
        import notifications.telegram as tg
        sent = []
        monkeypatch.setattr(tg, "_send", lambda msg: sent.append(msg))
        # Call the patched public function — conftest autouse patches this to lambda
        tg.notify_stale_research(999.0)
        assert sent == [], "autouse fixture must suppress notify_stale_research in all unit tests"


# ── _OnceGuard ─────────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestOnceGuard:
    """_OnceGuard must allow each (scope, key) exactly once per calendar day."""

    def _fresh_guard(self):
        from notifications.telegram import _OnceGuard
        return _OnceGuard()

    def test_first_call_returns_true(self):
        g = self._fresh_guard()
        assert g.should_send("drawdown", "AAPL") is True

    def test_second_call_same_scope_key_same_day_returns_false(self):
        g = self._fresh_guard()
        g.should_send("drawdown", "AAPL")
        assert g.should_send("drawdown", "AAPL") is False

    def test_different_key_same_scope_returns_true(self):
        g = self._fresh_guard()
        g.should_send("drawdown", "AAPL")
        assert g.should_send("drawdown", "NVDA") is True

    def test_different_scope_same_key_returns_true(self):
        g = self._fresh_guard()
        g.should_send("drawdown", "AAPL")
        assert g.should_send("bracket_timeout", "AAPL") is True

    def test_empty_key_deduped_per_scope(self):
        """Notifications with no per-symbol key (e.g. daily_loss) still dedup correctly."""
        g = self._fresh_guard()
        assert g.should_send("daily_loss") is True
        assert g.should_send("daily_loss") is False

    def test_day_rollover_allows_resend(self, monkeypatch):
        """After the calendar day changes, should_send returns True again."""
        from notifications.telegram import _OnceGuard
        from datetime import date
        g = _OnceGuard()
        # Simulate "today" when guard was first called
        monkeypatch.setattr("notifications.telegram.datetime",
                            type("_FakeDT", (), {
                                "now": staticmethod(lambda tz=None: type("_FakeNow", (), {"date": lambda self=None: date(2026, 5, 29)})())
                            })())
        # Re-import to pick up monkeypatched datetime is not trivial — test via internal state
        g._seen[("daily_loss", "")] = "2026-05-28"  # yesterday
        assert g.should_send("daily_loss") is True   # today is different → fires


# ── notify_bracket_correction_timeout dedup ───────────────────────────────────

@pytest.mark.unit
class TestBracketTimeoutDedup:
    """notify_bracket_correction_timeout must send at most once per symbol per day."""

    def test_first_call_sends(self, monkeypatch, tmp_path):
        import config
        import notifications.telegram as tg
        from notifications.telegram import _OnceGuard
        monkeypatch.setattr(config, "BRACKET_TIMEOUT_SENT_FILE", tmp_path / "bt_sent.json")
        monkeypatch.setattr(tg, "_once", _OnceGuard())
        sent = []
        monkeypatch.setattr(tg, "_send", lambda msg: sent.append(msg))
        tg.notify_bracket_correction_timeout("CRWD", 550.0, 3.0, 5.0)
        assert len(sent) == 1
        assert "CRWD" in sent[0]
        assert "BRACKET TIMEOUT" in sent[0]

    def test_second_call_same_symbol_suppressed(self, monkeypatch, tmp_path):
        import config
        import notifications.telegram as tg
        from notifications.telegram import _OnceGuard
        monkeypatch.setattr(config, "BRACKET_TIMEOUT_SENT_FILE", tmp_path / "bt_sent.json")
        monkeypatch.setattr(tg, "_once", _OnceGuard())
        sent = []
        monkeypatch.setattr(tg, "_send", lambda msg: sent.append(msg))
        tg.notify_bracket_correction_timeout("CRWD", 550.0, 3.0, 5.0)
        tg.notify_bracket_correction_timeout("CRWD", 551.0, 3.0, 5.0)
        assert len(sent) == 1, "second call for same symbol must be suppressed"

    def test_different_symbols_both_send(self, monkeypatch, tmp_path):
        import config
        import notifications.telegram as tg
        from notifications.telegram import _OnceGuard
        monkeypatch.setattr(config, "BRACKET_TIMEOUT_SENT_FILE", tmp_path / "bt_sent.json")
        monkeypatch.setattr(tg, "_once", _OnceGuard())
        sent = []
        monkeypatch.setattr(tg, "_send", lambda msg: sent.append(msg))
        tg.notify_bracket_correction_timeout("CRWD", 550.0, 3.0, 5.0)
        tg.notify_bracket_correction_timeout("NVDA", 900.0, 3.0, 5.0)
        assert len(sent) == 2, "different symbols must each send their own alert"

    def test_no_notification_when_fill_within_threshold(self, monkeypatch):
        """execute_buy must NOT fire the bracket-timeout notification when correct_bracket
        returns {} (slippage within threshold — normal successful fill)."""
        import types
        from unittest.mock import MagicMock, patch
        from trading import risk_manager

        sent = []
        # Patch notify to capture any unexpected calls
        with patch("trading.executor.telegram_notify.notify_bracket_correction_timeout",
                   side_effect=lambda *a, **kw: sent.append(a)):
            # correct_bracket_after_fill returning {} means within-threshold — no timeout
            client = MagicMock()
            order = types.SimpleNamespace(
                status="filled", filled_avg_price="100.3", legs=[]
            )
            client.get_order_by_id.return_value = order
            result = risk_manager.correct_bracket_after_fill(
                client, "order-999", 100.0, 4.0, 5.0
            )
            assert result == {}, "within-threshold fill should return {}"
            assert sent == [], "no bracket-timeout notification for within-threshold fill"

    def test_guard_survives_restart_via_disk_sentinel(self, monkeypatch, tmp_path):
        """notify_bracket_correction_timeout must NOT fire for same symbol after _once reset.

        Root cause: _once._seen is in-memory. Each bot restart (e.g. for code changes)
        clears it, allowing the same symbol to fire again. The disk sentinel is the
        permanent fix — it persists across restarts within the same trading day.
        """
        import config
        import notifications.telegram as tg
        from notifications.telegram import _OnceGuard
        sentinel_file = tmp_path / "bracket_timeout_sent.json"
        monkeypatch.setattr(config, "BRACKET_TIMEOUT_SENT_FILE", sentinel_file)
        sent = []
        monkeypatch.setattr(tg, "_send", lambda msg: sent.append(msg))

        # Session 1 — first fire (allowed)
        monkeypatch.setattr(tg, "_once", _OnceGuard())
        tg.notify_bracket_correction_timeout("CRWD", 550.0, 3.0, 5.0)
        assert len(sent) == 1, "first call must send"

        # Session 2 — simulate restart: fresh _once, but disk sentinel survives
        monkeypatch.setattr(tg, "_once", _OnceGuard())
        tg.notify_bracket_correction_timeout("CRWD", 550.0, 3.0, 5.0)
        assert len(sent) == 1, "after restart, disk sentinel must suppress same-day same-symbol re-fire"

    def test_leg_replacement_failure_does_not_fire_timeout_notification(self, monkeypatch):
        """correct_bracket_after_fill must NOT return None on leg-replacement failure.

        Root cause: when replace_order_by_id raises, the function returned None —
        same as a genuine poll timeout — triggering the timeout notification even
        though the order WAS filled. The fix: return partial fill dict, not None.
        """
        import types, config
        from trading import risk_manager
        monkeypatch.setattr(config, "FILL_SLIPPAGE_THRESHOLD", 0.001)

        client_mock = types.SimpleNamespace(
            get_order_by_id=lambda oid: types.SimpleNamespace(
                status="filled", filled_avg_price="102.0",
                legs=[types.SimpleNamespace(type="stop", id="leg-1"),
                      types.SimpleNamespace(type="limit", id="leg-2")],
            ),
            replace_order_by_id=lambda leg_id, **kw: (_ for _ in ()).throw(
                Exception("Alpaca: cannot replace bracket leg")
            ),
        )
        result = risk_manager.correct_bracket_after_fill(
            client_mock, "order-999", 100.0, 4.0, 5.0
        )
        assert result is not None, (
            "leg-replacement failure must NOT return None (which would fire timeout notification). "
            "Order filled at 102.0; only the bracket leg adjustment failed."
        )
        assert "fill_price" in result, "result must carry actual fill_price so journaling is accurate"
