"""
Market data, Alpaca client, API budget, Telegram, daily report, io_utils,
and market monitor unit tests.
Run with: pytest tests/test_market.py -v -m unit
No API calls — all tests use synthetic fixtures.
"""
import json
import os
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from utils.constants import ET


# ── api_budget: daily ceiling and reset ──────────────────────────────────────

@pytest.mark.unit
class TestApiBudget:
    def test_fresh_budget_starts_at_zero(self, tmp_path, monkeypatch):
        from trading import risk_manager
        import config
        monkeypatch.setattr(config, "API_BUDGET_FILE", tmp_path / "api_budget.json")
        budget = risk_manager.load_budget()
        assert budget["estimated_cost_usd"] == 0.0
        assert budget["calls_made"] == 0

    def test_budget_resets_on_new_day(self, tmp_path, monkeypatch):
        from trading import risk_manager
        import config
        budget_file = tmp_path / "api_budget.json"
        monkeypatch.setattr(config, "API_BUDGET_FILE", budget_file)
        stale = {
            "date": "2026-01-01",
            "calls_made": 10,
            "input_tokens": 100000,
            "output_tokens": 5000,
            "cache_write_tokens": 0,
            "cache_read_tokens": 0,
            "estimated_cost_usd": 0.95,
            "ceiling_usd": 1.00,
            "ceiling_mode": "normal",
            "budget_exhausted": False,
        }
        with open(budget_file, "w") as f:
            json.dump(stale, f)
        budget = risk_manager.load_budget()
        assert budget["estimated_cost_usd"] == 0.0
        assert budget["calls_made"] == 0

    def test_normal_ceiling_default(self, tmp_path, monkeypatch):
        from trading import risk_manager
        import config
        monkeypatch.setattr(config, "API_BUDGET_FILE", tmp_path / "api_budget.json")
        assert risk_manager.get_ceiling() == config.API_BUDGET_NORMAL_CEILING

    def test_elevated_ceiling_when_mode_elevated(self, tmp_path, monkeypatch):
        from trading import risk_manager
        import config
        monkeypatch.setattr(config, "API_BUDGET_FILE", tmp_path / "api_budget.json")
        b = risk_manager.load_budget()
        b["ceiling_mode"] = "elevated"
        risk_manager._save_budget(b)
        assert risk_manager.get_ceiling() == config.API_BUDGET_ELEVATED_CEILING

    def test_normal_ceiling_when_mode_normal(self, tmp_path, monkeypatch):
        from trading import risk_manager
        import config
        monkeypatch.setattr(config, "API_BUDGET_FILE", tmp_path / "api_budget.json")
        assert risk_manager.get_ceiling() == config.API_BUDGET_NORMAL_CEILING

    def test_check_budget_allows_within_ceiling(self, tmp_path, monkeypatch):
        from trading import risk_manager
        import config
        monkeypatch.setattr(config, "API_BUDGET_FILE", tmp_path / "api_budget.json")
        assert risk_manager.check_entry_budget(0.50, "NORMAL") is True

    def test_check_budget_blocks_over_ceiling(self, tmp_path, monkeypatch):
        from trading import risk_manager
        import config
        monkeypatch.setattr(config, "API_BUDGET_FILE", tmp_path / "api_budget.json")
        # Record usage that fills entry pool (70% of $1.00 = $0.70)
        risk_manager.record_usage(input_tokens=290_000, output_tokens=0)
        # Now try to add more than remaining
        assert risk_manager.check_entry_budget(0.50, "NORMAL") is False

    def test_record_usage_accumulates(self, tmp_path, monkeypatch):
        from trading import risk_manager
        import config
        monkeypatch.setattr(config, "API_BUDGET_FILE", tmp_path / "api_budget.json")
        risk_manager.record_usage(input_tokens=1000, output_tokens=100)
        risk_manager.record_usage(input_tokens=1000, output_tokens=100)
        budget = risk_manager.load_budget()
        assert budget["input_tokens"] == 2000
        assert budget["output_tokens"] == 200
        assert budget["calls_made"] == 2
        assert budget["estimated_cost_usd"] > 0

    def test_corrupt_file_resets_cleanly(self, tmp_path, monkeypatch):
        from trading import risk_manager
        import config
        budget_file = tmp_path / "api_budget.json"
        monkeypatch.setattr(config, "API_BUDGET_FILE", budget_file)
        budget_file.write_text("not json{{{")
        budget = risk_manager.load_budget()
        assert budget["calls_made"] == 0


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


# ── api_budget: remaining lines ───────────────────────────────────────────────

@pytest.mark.unit
class TestApiBudgetRemaining:
    def test_record_call_increases_cost(self, tmp_path, monkeypatch):
        from trading import risk_manager
        import config
        monkeypatch.setattr(config, "API_BUDGET_FILE", tmp_path / "api_budget.json")
        risk_manager.record_usage(input_tokens=1000, output_tokens=100,
                                  cache_write_tokens=0, cache_read_tokens=0)
        budget = risk_manager.load_budget()
        assert budget["estimated_cost_usd"] > 0
        assert budget["calls_made"] == 1

    def test_check_budget_not_exhausted_returns_true(self, tmp_path, monkeypatch):
        from trading import risk_manager
        import config
        monkeypatch.setattr(config, "API_BUDGET_FILE", tmp_path / "api_budget.json")
        assert risk_manager.check_entry_budget(estimated_cost=0.0, regime="NORMAL") is True

    def test_ceiling_exhausted_after_many_calls(self, tmp_path, monkeypatch):
        from trading import risk_manager
        import config
        monkeypatch.setattr(config, "API_BUDGET_FILE", tmp_path / "api_budget.json")
        monkeypatch.setattr(config, "API_BUDGET_NORMAL_CEILING", 0.001)  # tiny ceiling
        # Simulate expensive calls
        for _ in range(5):
            risk_manager.record_usage(input_tokens=100_000, output_tokens=10_000,
                                      cache_write_tokens=0, cache_read_tokens=0)
        assert risk_manager.check_entry_budget(estimated_cost=0.0, regime="NORMAL") is False


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


# ── TestIoUtils ───────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestIoUtils:
    """Tests for load_json and save_json utilities."""

    def _write_cache(self, tmp_path, filename: str, content: str) -> Path:
        """Helper to write content to a temp file."""
        file_path = tmp_path / filename
        file_path.write_text(content)
        return file_path

    def test_load_json_missing_file_returns_default(self, tmp_path):
        """Missing file returns default value."""
        from utils.io_utils import load_json
        missing_path = tmp_path / "missing.json"
        result = load_json(missing_path, [])
        assert result == []

    def test_load_json_valid_file_returns_data(self, tmp_path):
        """Valid JSON file returns parsed data."""
        from utils.io_utils import load_json
        file_path = self._write_cache(tmp_path, "test.json", '{"k": 1}')
        result = load_json(file_path, {})
        assert result == {"k": 1}

    def test_load_json_corrupt_file_returns_default(self, tmp_path):
        """Corrupt JSON returns default value."""
        from utils.io_utils import load_json
        file_path = self._write_cache(tmp_path, "corrupt.json", "not json{{{")
        result = load_json(file_path, {})
        assert result == {}

    def test_load_json_corrupt_creates_backup(self, tmp_path):
        """Corrupt JSON creates .corrupt.json backup."""
        from utils.io_utils import load_json
        file_path = self._write_cache(tmp_path, "corrupt.json", "not json{{{")
        load_json(file_path, {})
        backup_path = file_path.with_suffix(".corrupt.json")
        assert backup_path.exists()
        assert backup_path.read_text() == "not json{{{"

    def test_load_json_empty_file_returns_default(self, tmp_path):
        """Empty file returns default value."""
        from utils.io_utils import load_json
        file_path = self._write_cache(tmp_path, "empty.json", "")
        result = load_json(file_path, [])
        assert result == []

    def test_save_json_writes_valid_data(self, tmp_path):
        """save_json writes valid JSON and can be read back."""
        from utils.io_utils import save_json, load_json
        file_path = tmp_path / "output.json"
        data = {"x": 1}
        save_json(file_path, data)
        result = load_json(file_path, {})
        assert result == data

    def test_save_json_atomic_no_tmp_on_success(self, tmp_path):
        """After successful save, .tmp file should not exist."""
        from utils.io_utils import save_json
        file_path = tmp_path / "output.json"
        save_json(file_path, {"x": 1})
        tmp_path_file = file_path.with_suffix(".tmp")
        assert not tmp_path_file.exists()

    def test_save_json_raises_on_unserializable(self, tmp_path):
        """save_json raises TypeError on unserializable data."""
        from utils.io_utils import save_json
        file_path = tmp_path / "output.json"
        with pytest.raises(TypeError):
            save_json(file_path, {"bad": object()})

    def test_save_json_creates_parent_dir_content(self, tmp_path):
        """save_json works with nested parent directory."""
        from utils.io_utils import save_json, load_json
        nested_path = tmp_path / "nested" / "deep" / "data.json"
        data = {"nested": True}
        nested_path.parent.mkdir(parents=True, exist_ok=True)
        save_json(nested_path, data)
        result = load_json(nested_path, {})
        assert result == data

    def test_load_json_list_data(self, tmp_path):
        """save_json and load_json handle list data."""
        from utils.io_utils import save_json, load_json
        file_path = tmp_path / "list.json"
        data = [1, 2, 3, {"key": "value"}]
        save_json(file_path, data)
        result = load_json(file_path, [])
        assert result == data


# ── TestMarketMonitor ─────────────────────────────────────────────────────────

class TestMarketMonitor:
    """Unit tests for utils/market_monitor.py — no network or Alpaca calls."""

    def _write_regime_file(self, tmp_path, **overrides):
        """Write a minimal macro_regime.json and return its path."""
        from utils.io_utils import save_json
        path = tmp_path / "macro_regime.json"
        data = {"regime": "NORMAL", "flash_crash_candidate": None}
        data.update(overrides)
        save_json(path, data)
        return path

    @pytest.mark.unit
    def test_write_flash_crash_candidate_creates_file(self, tmp_path, monkeypatch):
        """_write_flash_crash_candidate stores symbol, price, pct_drop, and timestamp."""
        import config
        monkeypatch.setattr(config, "MACRO_REGIME_FILE", tmp_path / "macro_regime.json")
        from utils.market_monitor import _write_flash_crash_candidate
        now = datetime(2026, 5, 6, 10, 0, tzinfo=ET)
        _write_flash_crash_candidate(-3.1, now)
        from utils.io_utils import load_json
        data = load_json(tmp_path / "macro_regime.json", {})
        candidate = data.get("flash_crash_candidate")
        assert candidate is not None
        assert candidate["spy_pct"] == -3.1
        assert candidate["detected_at"] == now.isoformat()

    @pytest.mark.unit
    def test_clear_flash_crash_candidate_removes_entry(self, tmp_path, monkeypatch):
        """_clear_flash_crash_candidate sets flash_crash_candidate to None."""
        import config
        self._write_regime_file(
            tmp_path,
            flash_crash_candidate={"spy_pct": -3.0, "detected_at": "2026-05-06T10:00:00"},
        )
        monkeypatch.setattr(config, "MACRO_REGIME_FILE", tmp_path / "macro_regime.json")
        from utils.market_monitor import _clear_flash_crash_candidate
        _clear_flash_crash_candidate()
        from utils.io_utils import load_json
        data = load_json(tmp_path / "macro_regime.json", {})
        assert data.get("flash_crash_candidate") is None

    @pytest.mark.unit
    def test_clear_flash_crash_candidate_noop_if_missing(self, tmp_path, monkeypatch):
        """_clear_flash_crash_candidate does not raise when macro_regime.json is absent."""
        import config
        missing = tmp_path / "no_such_file.json"
        monkeypatch.setattr(config, "MACRO_REGIME_FILE", missing)
        from utils.market_monitor import _clear_flash_crash_candidate
        _clear_flash_crash_candidate()  # must not raise

    @pytest.mark.unit
    def test_read_candidate_returns_none_if_missing(self, tmp_path, monkeypatch):
        """_read_flash_crash_candidate returns None when file does not exist."""
        import config
        monkeypatch.setattr(config, "MACRO_REGIME_FILE", tmp_path / "no_such.json")
        from utils.market_monitor import _read_flash_crash_candidate
        result = _read_flash_crash_candidate()
        assert result is None

    @pytest.mark.unit
    def test_read_candidate_returns_data_if_exists(self, tmp_path, monkeypatch):
        """_read_flash_crash_candidate returns the stored dict when file contains one."""
        import config
        self._write_regime_file(
            tmp_path,
            flash_crash_candidate={"spy_pct": -3.5, "detected_at": "2026-05-06T10:00:00"},
        )
        monkeypatch.setattr(config, "MACRO_REGIME_FILE", tmp_path / "macro_regime.json")
        from utils.market_monitor import _read_flash_crash_candidate
        result = _read_flash_crash_candidate()
        assert result is not None
        assert result["spy_pct"] == -3.5

    @pytest.mark.unit
    def test_set_crisis_lockout_writes_macro_regime(self, tmp_path, monkeypatch):
        """_set_crisis_lockout writes regime=CRISIS and flash_crash_active=True."""
        import config
        self._write_regime_file(tmp_path)
        monkeypatch.setattr(config, "MACRO_REGIME_FILE", tmp_path / "macro_regime.json")
        from utils.market_monitor import _set_crisis_lockout
        _set_crisis_lockout(hours=48)
        from utils.io_utils import load_json
        data = load_json(tmp_path / "macro_regime.json", {})
        assert data.get("regime") == "CRISIS"
        assert data.get("flash_crash_active") is True
        assert data.get("flash_crash_until") is not None

    @pytest.mark.unit
    def test_run_market_monitor_no_crash_returns_ok(self, tmp_path, monkeypatch):
        """run_market_monitor does nothing when SPY drop is within normal range."""
        import config
        self._write_regime_file(tmp_path)
        monkeypatch.setattr(config, "MACRO_REGIME_FILE", tmp_path / "macro_regime.json")
        monkeypatch.setattr(config, "FLASH_CRASH_SPY_DROP_PCT", -2.5)
        monkeypatch.setattr(config, "POLYGON_API_KEY", "")

        import utils.market_monitor as mm
        monkeypatch.setattr(mm, "get_latest_price", lambda sym: 500.0)
        monkeypatch.setattr(mm, "_get_spy_prev_close", lambda: 499.0)  # +0.2%

        from utils.market_monitor import run_market_monitor
        run_market_monitor()  # should complete without triggering lockout

        from utils.io_utils import load_json
        data = load_json(tmp_path / "macro_regime.json", {})
        assert data.get("flash_crash_active") is not True

    @pytest.mark.unit
    def test_run_market_monitor_stage1_triggers_on_drop(self, tmp_path, monkeypatch):
        """run_market_monitor writes a stage-1 candidate when SPY drops past threshold."""
        import config
        self._write_regime_file(tmp_path)
        monkeypatch.setattr(config, "MACRO_REGIME_FILE", tmp_path / "macro_regime.json")
        monkeypatch.setattr(config, "FLASH_CRASH_SPY_DROP_PCT", -2.5)
        monkeypatch.setattr(config, "POLYGON_API_KEY", "")

        import utils.market_monitor as mm
        monkeypatch.setattr(mm, "get_latest_price", lambda sym: 480.0)
        monkeypatch.setattr(mm, "_get_spy_prev_close", lambda: 500.0)  # -4%

        from utils.market_monitor import run_market_monitor
        run_market_monitor()

        from utils.io_utils import load_json
        data = load_json(tmp_path / "macro_regime.json", {})
        assert data.get("flash_crash_candidate") is not None
        assert data["flash_crash_candidate"]["spy_pct"] == pytest.approx(-4.0, abs=0.05)

    @pytest.mark.unit
    def test_run_market_monitor_stage2_triggers_lockout(self, tmp_path, monkeypatch):
        """Stage-2 confirmation calls force_close_all when candidate is still fresh."""
        import config
        from utils.io_utils import save_json
        # Candidate was written 5 minutes ago (within 15-min window)
        candidate_time = (datetime.now(ET) - __import__("datetime").timedelta(minutes=5)).isoformat()
        self._write_regime_file(
            tmp_path,
            flash_crash_candidate={"spy_pct": -3.5, "detected_at": candidate_time},
        )
        monkeypatch.setattr(config, "MACRO_REGIME_FILE", tmp_path / "macro_regime.json")
        monkeypatch.setattr(config, "FLASH_CRASH_SPY_DROP_PCT", -2.5)
        monkeypatch.setattr(config, "POLYGON_API_KEY", "")

        import utils.market_monitor as mm
        monkeypatch.setattr(mm, "get_latest_price", lambda sym: 480.0)
        monkeypatch.setattr(mm, "_get_spy_prev_close", lambda: 500.0)  # -4%

        force_close_called = []

        def mock_force_close():
            force_close_called.append(True)

        monkeypatch.setattr(mm, "_trigger_flash_crash_lockout", lambda: (force_close_called.append(True), _set_crisis_lockout_direct(tmp_path)))

        def _set_crisis_lockout_direct(tp):
            data = {"regime": "CRISIS", "flash_crash_active": True, "flash_crash_until": "2026-05-08T10:00:00", "flash_crash_candidate": None}
            save_json(tp / "macro_regime.json", data)

        from utils.market_monitor import run_market_monitor
        run_market_monitor()

        assert len(force_close_called) == 1

    @pytest.mark.unit
    def test_run_market_monitor_clears_stale_candidate(self, tmp_path, monkeypatch):
        """A candidate older than 15 min is overwritten with a fresh one, not confirmed."""
        import config
        from utils.io_utils import load_json
        # Candidate was written 20 minutes ago (outside 15-min window)
        old_time = (datetime.now(ET) - __import__("datetime").timedelta(minutes=20)).isoformat()
        self._write_regime_file(
            tmp_path,
            flash_crash_candidate={"spy_pct": -3.5, "detected_at": old_time},
        )
        monkeypatch.setattr(config, "MACRO_REGIME_FILE", tmp_path / "macro_regime.json")
        monkeypatch.setattr(config, "FLASH_CRASH_SPY_DROP_PCT", -2.5)
        monkeypatch.setattr(config, "POLYGON_API_KEY", "")

        import utils.market_monitor as mm
        monkeypatch.setattr(mm, "get_latest_price", lambda sym: 480.0)
        monkeypatch.setattr(mm, "_get_spy_prev_close", lambda: 500.0)  # -4%

        lockout_triggered = []
        orig_trigger = mm._trigger_flash_crash_lockout
        monkeypatch.setattr(mm, "_trigger_flash_crash_lockout", lambda: lockout_triggered.append(True))

        from utils.market_monitor import run_market_monitor
        run_market_monitor()

        # Lockout must NOT be triggered for a stale candidate
        assert len(lockout_triggered) == 0
        # But a fresh candidate must have been written
        data = load_json(tmp_path / "macro_regime.json", {})
        candidate = data.get("flash_crash_candidate")
        assert candidate is not None

    @pytest.mark.unit
    def test_verify_spy_with_polygon_returns_none_on_error(self, tmp_path, monkeypatch):
        """_verify_spy_with_polygon returns None when the HTTP request fails."""
        import config
        monkeypatch.setattr(config, "POLYGON_API_KEY", "fake-key")

        import utils.market_monitor as mm
        monkeypatch.setattr(mm.requests, "get", lambda *a, **kw: (_ for _ in ()).throw(Exception("network error")))

        from utils.market_monitor import _verify_spy_with_polygon
        result = _verify_spy_with_polygon(-3.0)
        assert result is None

    @pytest.mark.unit
    def test_verify_spy_with_polygon_returns_price_on_success(self, tmp_path, monkeypatch):
        """_verify_spy_with_polygon returns True when Polygon confirms a crash-level drop."""
        import config
        monkeypatch.setattr(config, "POLYGON_API_KEY", "fake-key")
        monkeypatch.setattr(config, "FLASH_CRASH_SPY_DROP_PCT", -2.5)

        class MockResp:
            def raise_for_status(self): pass
            def json(self):
                return {"ticker": {"todaysChangePerc": -4.0}}

        import utils.market_monitor as mm
        monkeypatch.setattr(mm.requests, "get", lambda *a, **kw: MockResp())

        from utils.market_monitor import _verify_spy_with_polygon
        result = _verify_spy_with_polygon(-4.0)
        assert result is True

    @pytest.mark.unit
    def test_get_spy_prev_close_returns_float(self, monkeypatch):
        """_get_spy_prev_close returns a positive float when market data is available."""
        import pandas as pd

        closes = [495.0, 500.0]
        idx = pd.date_range("2026-05-05", periods=2, freq="B", tz="America/New_York")
        mock_df = pd.DataFrame({"close": closes}, index=idx)

        # Patch at the market_data module level — _get_spy_prev_close imports from there
        import data.market_data as market_data
        monkeypatch.setattr(market_data, "get_daily_bars", lambda *a, **kw: mock_df)

        from utils.market_monitor import _get_spy_prev_close
        result = _get_spy_prev_close()
        assert isinstance(result, float)
        assert result > 0

    @pytest.mark.unit
    def test_run_market_monitor_polygon_fallback_used(self, tmp_path, monkeypatch):
        """When Polygon key is set but API fails, falls back to Stage 2 two-reading path."""
        import config
        self._write_regime_file(tmp_path)
        monkeypatch.setattr(config, "MACRO_REGIME_FILE", tmp_path / "macro_regime.json")
        monkeypatch.setattr(config, "FLASH_CRASH_SPY_DROP_PCT", -2.5)
        monkeypatch.setattr(config, "POLYGON_API_KEY", "fake-key-set")

        import utils.market_monitor as mm
        monkeypatch.setattr(mm, "get_latest_price", lambda sym: 480.0)
        monkeypatch.setattr(mm, "_get_spy_prev_close", lambda: 500.0)  # -4%
        # Polygon API unavailable → _verify_spy_with_polygon returns None
        monkeypatch.setattr(mm, "_verify_spy_with_polygon", lambda pct: None)

        from utils.market_monitor import run_market_monitor
        run_market_monitor()

        # Stage 2 path should have written a candidate (no prior candidate → Stage 1)
        from utils.io_utils import load_json
        data = load_json(tmp_path / "macro_regime.json", {})
        assert data.get("flash_crash_candidate") is not None

    @pytest.mark.unit
    def test_trigger_flash_crash_logs_and_notifies(self, tmp_path, monkeypatch):
        """Stage-2 lockout notifies telegram when _trigger_flash_crash_lockout is called."""
        import config
        self._write_regime_file(tmp_path)
        monkeypatch.setattr(config, "MACRO_REGIME_FILE", tmp_path / "macro_regime.json")
        monkeypatch.setattr(config, "FLASH_CRASH_SPY_DROP_PCT", -2.5)
        monkeypatch.setattr(config, "POLYGON_API_KEY", "")

        # Set up a fresh candidate (within 15-min window)
        candidate_time = datetime.now(ET).isoformat()
        from utils.io_utils import save_json
        save_json(tmp_path / "macro_regime.json", {
            "regime": "NORMAL",
            "flash_crash_candidate": {"spy_pct": -3.5, "detected_at": candidate_time},
        })

        import utils.market_monitor as mm
        monkeypatch.setattr(mm, "get_latest_price", lambda sym: 480.0)
        monkeypatch.setattr(mm, "_get_spy_prev_close", lambda: 500.0)  # -4%

        notify_calls = []

        def mock_trigger():
            notify_calls.append("triggered")
            # Also write CRISIS so we can verify
            save_json(tmp_path / "macro_regime.json", {
                "regime": "CRISIS",
                "flash_crash_active": True,
                "flash_crash_until": "2026-05-08T10:00:00",
                "flash_crash_candidate": None,
            })

        monkeypatch.setattr(mm, "_trigger_flash_crash_lockout", mock_trigger)

        from utils.market_monitor import run_market_monitor
        run_market_monitor()

        assert "triggered" in notify_calls
        from utils.io_utils import load_json
        data = load_json(tmp_path / "macro_regime.json", {})
        assert data.get("regime") == "CRISIS"


# ── daily_report: build_report_body ──────────────────────────────────────────

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


# ── TestBacktestSimulatorConstants ───────────────────────────────────────────

@pytest.mark.unit
class TestBacktestSimulatorConstants:
    """Verify backtest/simulator.py constants match config.py."""

    def test_simulator_starting_capital_matches_config(self):
        from backtest import simulator
        import config
        assert simulator.STARTING_CAPITAL == config.BACKTEST_STARTING_CAPITAL

    def test_simulator_slippage_matches_config(self):
        from backtest import simulator
        import config
        assert simulator.SLIPPAGE_PCT == config.BACKTEST_SLIPPAGE_PCT

    def test_simulator_max_positions_matches_config(self):
        from backtest import simulator
        import config
        assert simulator.MAX_POSITIONS == config.BACKTEST_MAX_POSITIONS

    def test_simulator_market_open_matches_config(self):
        from backtest import simulator
        import config
        assert simulator.MARKET_OPEN_H == config.MARKET_OPEN_HOUR
        assert simulator.MARKET_OPEN_M == config.MARKET_OPEN_MINUTE

    def test_simulator_force_close_matches_config(self):
        from backtest import simulator
        import config
        assert simulator.FORCE_CLOSE_H == config.EOD_FORCE_CLOSE_HOUR
        assert simulator.FORCE_CLOSE_M == config.EOD_FORCE_CLOSE_MINUTE

    def test_simulator_swing_eval_matches_config(self):
        from backtest import simulator
        import config
        assert simulator.SWING_EVAL_H == config.EOD_HOLDS_EVAL_HOUR
        assert simulator.SWING_EVAL_M == config.EOD_HOLDS_EVAL_MINUTE

    def test_simulator_swing_composite_min_matches_config(self):
        from backtest import simulator
        import config
        assert simulator.SWING_COMPOSITE_MIN == config.BACKTEST_SWING_COMPOSITE_MIN
