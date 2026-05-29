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


# ---------------------------------------------------------------------------
# watch_loader — held-ticker merge tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_watch_loader_merges_open_position_tickers(monkeypatch, tmp_path):
    """load_watch_list always includes tickers from open_positions.json."""
    import config
    from utils.io_utils import save_json

    watch_file = tmp_path / "watch_list.txt"
    watch_file.write_text("AAPL\nMSFT\n")
    open_pos_file = tmp_path / "open_positions.json"
    save_json(open_pos_file, {"NVDA": {"qty": 10}})
    swing_pos_file = tmp_path / "swing_positions.json"
    save_json(swing_pos_file, {})

    monkeypatch.setattr(config, "WATCH_LIST_FILE", watch_file)
    monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", open_pos_file)
    monkeypatch.setattr(config, "SWING_POSITIONS_FILE", swing_pos_file)
    monkeypatch.setattr(config, "DATA_WATCH_LIST_HEALTH_FILE", tmp_path / "wl_health.json")

    import importlib
    import trading.watch_loader
    importlib.reload(trading.watch_loader)
    from trading.watch_loader import load_watch_list
    result = load_watch_list()

    assert "NVDA" in result
    assert "AAPL" in result
    assert "MSFT" in result


@pytest.mark.unit
def test_watch_loader_merges_swing_position_tickers(monkeypatch, tmp_path):
    """load_watch_list includes tickers from swing_positions.json."""
    import config
    from utils.io_utils import save_json

    watch_file = tmp_path / "watch_list.txt"
    watch_file.write_text("AAPL\n")
    open_pos_file = tmp_path / "open_positions.json"
    save_json(open_pos_file, {})
    swing_pos_file = tmp_path / "swing_positions.json"
    save_json(swing_pos_file, {"PANW": {"qty": 5}})

    monkeypatch.setattr(config, "WATCH_LIST_FILE", watch_file)
    monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", open_pos_file)
    monkeypatch.setattr(config, "SWING_POSITIONS_FILE", swing_pos_file)
    monkeypatch.setattr(config, "DATA_WATCH_LIST_HEALTH_FILE", tmp_path / "wl_health.json")

    import importlib
    import trading.watch_loader
    importlib.reload(trading.watch_loader)
    from trading.watch_loader import load_watch_list
    result = load_watch_list()

    assert "PANW" in result
    assert "AAPL" in result


@pytest.mark.unit
def test_watch_loader_no_duplicates(monkeypatch, tmp_path):
    """If a held ticker is already in watch_list.txt, it appears only once."""
    import config
    from utils.io_utils import save_json

    watch_file = tmp_path / "watch_list.txt"
    watch_file.write_text("AAPL\nNVDA\n")
    open_pos_file = tmp_path / "open_positions.json"
    save_json(open_pos_file, {"NVDA": {"qty": 10}})
    swing_pos_file = tmp_path / "swing_positions.json"
    save_json(swing_pos_file, {})

    monkeypatch.setattr(config, "WATCH_LIST_FILE", watch_file)
    monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", open_pos_file)
    monkeypatch.setattr(config, "SWING_POSITIONS_FILE", swing_pos_file)
    monkeypatch.setattr(config, "DATA_WATCH_LIST_HEALTH_FILE", tmp_path / "wl_health.json")

    import importlib
    import trading.watch_loader
    importlib.reload(trading.watch_loader)
    from trading.watch_loader import load_watch_list
    result = load_watch_list()

    assert result.count("NVDA") == 1
