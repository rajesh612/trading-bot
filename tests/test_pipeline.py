"""Tests for cycle_context, pre_claude_filter, budget_guard, and pipeline tiers."""
import json
import threading
import time
from datetime import datetime
from unittest.mock import patch, MagicMock
import pytest

from utils.constants import ET


# ─── CycleSnapshot / FRED TTL cache ──────────────────────────────────────────

class TestFredTTLCache:

    @pytest.mark.unit
    def test_fred_cache_not_reloaded_within_ttl(self, monkeypatch, tmp_path):
        """Second call within TTL returns cached dict without re-reading disk."""
        import config
        from trading import cycle_context

        fred_file = tmp_path / "fred_cache.json"
        fred_file.write_text('{"cpi_trend": "stable"}')
        monkeypatch.setattr(config, "FRED_CACHE_FILE", fred_file)
        # Reset module cache so first call loads from disk
        monkeypatch.setattr(cycle_context, "_fred_cache", {})
        monkeypatch.setattr(cycle_context, "_fred_loaded_at", 0.0)

        first = cycle_context._get_fred_cache()
        # Modify file on disk — TTL should prevent re-read
        fred_file.write_text('{"cpi_trend": "rising"}')
        second = cycle_context._get_fred_cache()

        assert first == {"cpi_trend": "stable"}
        assert second == {"cpi_trend": "stable"}  # still cached

    @pytest.mark.unit
    def test_fred_cache_reloaded_after_invalidate(self, monkeypatch, tmp_path):
        """invalidate_fred_cache() forces reload on next access."""
        import config
        from trading import cycle_context

        fred_file = tmp_path / "fred_cache.json"
        fred_file.write_text('{"cpi_trend": "rising"}')
        monkeypatch.setattr(config, "FRED_CACHE_FILE", fred_file)
        monkeypatch.setattr(cycle_context, "_fred_cache", {"cpi_trend": "stable"})
        monkeypatch.setattr(cycle_context, "_fred_loaded_at", time.monotonic())

        cycle_context.invalidate_fred_cache()
        result = cycle_context._get_fred_cache()

        assert result == {"cpi_trend": "rising"}

    @pytest.mark.unit
    def test_fred_cache_thread_safe(self, monkeypatch, tmp_path):
        """Concurrent reads don't corrupt the cache (thread-safe via _FRED_LOCK)."""
        import config
        from trading import cycle_context

        fred_file = tmp_path / "fred_cache.json"
        fred_file.write_text('{"cpi_trend": "stable"}')
        # Use monkeypatch so config is patched for all threads without race conditions
        monkeypatch.setattr(config, "FRED_CACHE_FILE", fred_file)
        monkeypatch.setattr(cycle_context, "_fred_cache", {})
        monkeypatch.setattr(cycle_context, "_fred_loaded_at", 0.0)

        results = []
        lock = threading.Lock()

        def reader():
            val = cycle_context._get_fred_cache()
            with lock:
                results.append(val)

        threads = [threading.Thread(target=reader) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(r == {"cpi_trend": "stable"} for r in results)


# ─── risk_manager additions ───────────────────────────────────────────────────

class TestRiskManagerAdditions:

    @pytest.mark.unit
    def test_is_entry_budget_exhausted_false_by_default(self):
        from trading import risk_manager
        risk_manager._entry_exhausted_flag = False
        assert risk_manager.is_entry_budget_exhausted() is False

    @pytest.mark.unit
    def test_is_entry_budget_exhausted_true_after_set(self):
        from trading import risk_manager
        risk_manager._entry_exhausted_flag = True
        assert risk_manager.is_entry_budget_exhausted() is True
        risk_manager._entry_exhausted_flag = False  # cleanup

    @pytest.mark.unit
    def test_try_rollover_writes_to_json(self, tmp_path):
        from trading import risk_manager
        from unittest.mock import patch
        budget = {
            "date": "2026-05-13",
            "entry_ceiling_usd": 0.70,
            "exit_ceiling_usd": 0.30,
            "exit_spent_usd": 0.0,
            "entry_spent_usd": 0.60,
        }
        budget_file = tmp_path / "api_budget.json"
        budget_file.write_text(json.dumps(budget))

        now = datetime(2026, 5, 13, 12, 30, tzinfo=ET)
        with patch("trading.risk_manager.config") as mc:
            mc.API_BUDGET_FILE = budget_file
            mc.BUDGET_ROLLOVER_HOUR = 12
            mc.BUDGET_ROLLOVER_MINUTE = 30
            mc.BUDGET_ROLLOVER_FRACTION = 0.5
            mc.EXIT_ROLLOVER_MAX_USD = 0.15
            mc.API_BUDGET_NORMAL_CEILING = 1.0
            mc.ENTRY_BUDGET_RATIO = 0.70
            mc.EXIT_BUDGET_RATIO = 0.30
            from trading.risk_manager import try_rollover_exit_to_entry
            try_rollover_exit_to_entry(now)

        updated = json.loads(budget_file.read_text())
        assert updated["entry_ceiling_usd"] == pytest.approx(0.85, abs=0.01)

    @pytest.mark.unit
    def test_check_entry_budget_no_gain_pct(self, tmp_path):
        """check_entry_budget accepts (cost, regime) — no gain_pct argument."""
        from unittest.mock import patch
        from trading import risk_manager
        today = datetime.now(ET).strftime("%Y-%m-%d")
        budget = {
            "date": today, "calls_made": 0, "entry_calls": 0, "exit_calls": 0,
            "input_tokens": 0, "output_tokens": 0, "cache_write_tokens": 0,
            "cache_read_tokens": 0, "estimated_cost_usd": 0.0,
            "entry_spent_usd": 0.0, "exit_spent_usd": 0.0,
            "ceiling_usd": 1.0,
            "entry_ceiling_usd": 0.70, "exit_ceiling_usd": 0.30,
            "ceiling_mode": "normal", "entry_exhausted": False,
            "exit_exhausted": False, "budget_exhausted": False, "escalated": False,
        }
        budget_file = tmp_path / "api_budget.json"
        budget_file.write_text(json.dumps(budget))
        with patch("trading.risk_manager.config") as mc:
            mc.API_BUDGET_FILE = budget_file
            mc.ENTRY_BUDGET_RATIO = 0.7
            mc.EXIT_BUDGET_RATIO  = 0.3
            mc.API_BUDGET_WARNING_PCT = 0.8
            mc.ENTRY_BUDGET_WARNING_PCT = 0.75
            result = risk_manager.check_entry_budget(0.001, "NORMAL")
        assert result is True


# ─── watch list filter ────────────────────────────────────────────────────────

class TestWatchListFilter:

    @pytest.mark.unit
    def test_filters_overbought_symbols(self):
        from agents.watch_list import _apply_technical_filter
        snapshots = {
            "NVDA": {"rsi": 75.0, "stoch_d": 60.0},   # RSI too high → out
            "AAPL": {"rsi": 55.0, "stoch_d": 85.0},   # Stoch too high → out
            "MSFT": {"rsi": 55.0, "stoch_d": 55.0},   # clean → in
            "AMZN": {"rsi": 60.0, "stoch_d": 65.0},   # clean → in
            "GOOG": {"rsi": 50.0, "stoch_d": 50.0},   # clean → in
            "META": {"rsi": 45.0, "stoch_d": 40.0},   # clean → in
            "TSLA": {"rsi": 55.0, "stoch_d": 60.0},   # clean → in
            "AMD":  {"rsi": 60.0, "stoch_d": 70.0},   # clean → in
        }
        result = _apply_technical_filter(
            ["NVDA", "AAPL", "MSFT", "AMZN", "GOOG", "META", "TSLA", "AMD"], snapshots,
            rsi_limit=70, stoch_limit=80,
        )
        assert "MSFT" in result
        assert "AMZN" in result
        assert "NVDA" not in result
        assert "AAPL" not in result

    @pytest.mark.unit
    def test_relaxes_thresholds_if_too_few_pass(self):
        """If < WATCH_LIST_MIN_AFTER_FILTER pass, retry with relaxed limits."""
        from agents.watch_list import _apply_technical_filter
        # All overbought at 70/80 but clean at 76/87
        snapshots = {s: {"rsi": 72.0, "stoch_d": 83.0} for s in ["A", "B", "C", "D", "E", "F"]}
        result = _apply_technical_filter(
            list(snapshots.keys()), snapshots,
            rsi_limit=70, stoch_limit=80,
        )
        # relaxed: rsi_limit=76, stoch_limit=87 → all pass
        assert len(result) >= 6

    @pytest.mark.unit
    def test_missing_snapshot_treated_as_clean(self):
        """Symbol with no snapshot data is not filtered out."""
        from agents.watch_list import _apply_technical_filter
        snapshots = {}  # no data
        result = _apply_technical_filter(["NVDA"], snapshots, rsi_limit=70, stoch_limit=80)
        assert "NVDA" in result


# ─── CycleSnapshot intraday_news wiring (Task 9) ─────────────────────────────

class TestCycleSnapshotIntradayNews:

    @pytest.mark.unit
    def test_build_snapshot_includes_intraday_news(self, tmp_path):
        """build_cycle_snapshot() populates intraday_news from cache."""
        from datetime import timezone
        from trading.cycle_context import build_cycle_snapshot, invalidate_fred_cache

        fresh_ts = datetime.now(timezone.utc).isoformat()
        news_cache = {"__updated_at": fresh_ts, "NVDA": {"sentiment_score": 15, "shock_score": 0, "headlines": []}}
        news_file = tmp_path / "intraday_news_cache.json"
        news_file.write_text(json.dumps(news_cache))

        with patch("trading.cycle_context.load_macro_regime", return_value={"regime": "NORMAL", "macro_health_score": 0}), \
             patch("trading.cycle_context.load_trading_params", return_value={}), \
             patch("trading.cycle_context.load_json", return_value={}), \
             patch("trading.cycle_context.load_intraday_news", return_value={"NVDA": {"sentiment_score": 15, "shock_score": 0}}):
            snap = build_cycle_snapshot(portfolio_gain_pct=0.0)

        assert snap.intraday_news.get("NVDA", {}).get("sentiment_score") == 15


# ─── CycleSnapshot major_event_today (Task 6) ────────────────────────────────

class TestCycleSnapshotMajorEvent:

    @pytest.mark.unit
    def test_cycle_snapshot_has_major_event_today_true(self, monkeypatch, tmp_path):
        """CycleSnapshot.major_event_today is True when a high-impact event is scheduled today."""
        import config
        monkeypatch.setattr(config, "RESEARCH_REPORT_FILE", tmp_path / "rr.json")
        monkeypatch.setattr(config, "INDICATOR_WEIGHTS_FILE", tmp_path / "iw.json")
        monkeypatch.setattr(config, "STRATEGY_MEMORY_FILE", tmp_path / "sm.md")
        monkeypatch.setattr(config, "FRED_CACHE_FILE", tmp_path / "fred.json")
        monkeypatch.setattr(config, "ECONOMIC_CALENDAR_FILE", tmp_path / "ec.json")

        from utils.io_utils import save_json
        today = datetime.now(ET).strftime("%Y-%m-%d")
        save_json(tmp_path / "ec.json", {
            "date": today,
            "events": [{"name": "FOMC Rate Decision", "time": "14:00"}],
        })

        monkeypatch.setattr("macro.regime.load_macro_regime", lambda: {"regime": "NORMAL", "macro_health_score": 0})
        monkeypatch.setattr("macro.regime.load_trading_params", lambda: {})
        monkeypatch.setattr("macro.intraday_context.get_intraday_context", lambda dt: None)
        monkeypatch.setattr("macro.breadth_signals.get_advance_decline_ratio", lambda: 1.0)
        monkeypatch.setattr("research.intraday.load_intraday_news", lambda: {})

        from trading.cycle_context import build_cycle_snapshot
        snap = build_cycle_snapshot(portfolio_gain_pct=0.0)
        assert snap.major_event_today is True

    @pytest.mark.unit
    def test_cycle_snapshot_major_event_today_false_when_no_event(self, monkeypatch, tmp_path):
        """CycleSnapshot.major_event_today is False when no high-impact events scheduled."""
        import config
        monkeypatch.setattr(config, "RESEARCH_REPORT_FILE", tmp_path / "rr.json")
        monkeypatch.setattr(config, "INDICATOR_WEIGHTS_FILE", tmp_path / "iw.json")
        monkeypatch.setattr(config, "STRATEGY_MEMORY_FILE", tmp_path / "sm.md")
        monkeypatch.setattr(config, "FRED_CACHE_FILE", tmp_path / "fred.json")
        monkeypatch.setattr(config, "ECONOMIC_CALENDAR_FILE", tmp_path / "ec.json")

        today = datetime.now(ET).strftime("%Y-%m-%d")
        from utils.io_utils import save_json
        save_json(tmp_path / "ec.json", {"date": today, "events": []})

        monkeypatch.setattr("macro.regime.load_macro_regime", lambda: {"regime": "NORMAL", "macro_health_score": 0})
        monkeypatch.setattr("macro.regime.load_trading_params", lambda: {})
        monkeypatch.setattr("macro.intraday_context.get_intraday_context", lambda dt: None)
        monkeypatch.setattr("macro.breadth_signals.get_advance_decline_ratio", lambda: 1.0)
        monkeypatch.setattr("research.intraday.load_intraday_news", lambda: {})

        from trading.cycle_context import build_cycle_snapshot
        snap = build_cycle_snapshot(portfolio_gain_pct=0.0)
        assert snap.major_event_today is False


# ─── Pipeline phase-2 tiers (Task 10) ────────────────────────────────────────

class TestPipelinePhase2Tiers:

    def _make_iv(self, symbol, composite, rsi=55.0, stoch_d=50.0, current_price=100.0):
        from tests.test_signals import make_full_iv
        return make_full_iv(
            symbol=symbol, composite_score=composite, rsi=rsi,
            stoch_d=stoch_d, current_price=current_price,
        )

    def _snap(self, research=None, intraday_news=None):
        from trading.cycle_context import CycleSnapshot
        return CycleSnapshot(
            regime_info={"regime": "NORMAL", "macro_health_score": 0},
            regime="NORMAL",
            fred_cache={},
            research=research or {"TEST": {"sentiment_score": 25}},
            research_meta={"macro_shock_score": 0},
            strategy_memory="",
            weights={},
            params={
                "rsi_buy_max": 72, "rsi_buy_max_high_vol": 65,
                "high_vol_position_mult": 0.5,
                "entry_start_hour": 10, "entry_end_hour": 15,
            },
            portfolio_gain_pct=0.0,
            intraday_news=intraday_news or {},
        )

    @pytest.mark.unit
    def test_budget_exhausted_skips_claude_phase(self):
        from trading.pipeline import TradingPipeline
        from trading import risk_manager
        risk_manager._entry_exhausted_flag = True
        pipeline = TradingPipeline()
        with patch("trading.pipeline._ask_claude_batch") as mock_claude, \
             patch("trading.pipeline._fetch_symbol_data", return_value=None):
            result = pipeline.run(
                symbols=["TEST"], snap=self._snap(),
                ai=MagicMock(), now=datetime(2026, 5, 13, 10, 0, tzinfo=ET),
                alpaca=MagicMock(), portfolio_value=10000, buying_power=5000,
                can_open=True,
            )
        mock_claude.assert_not_called()
        risk_manager._entry_exhausted_flag = False

    @pytest.mark.unit
    def test_low_composite_skipped_no_claude(self):
        from trading.pipeline import TradingPipeline
        iv = self._make_iv("TEST", composite=15.0)
        with patch("trading.pipeline._fetch_symbol_data", return_value=("TEST", iv, None, 0.0)), \
             patch("trading.pipeline._ask_claude_batch") as mock_claude:
            TradingPipeline().run(
                symbols=["TEST"], snap=self._snap(),
                ai=MagicMock(), now=datetime(2026, 5, 13, 10, 0, tzinfo=ET),
                alpaca=MagicMock(), portfolio_value=10000, buying_power=5000,
                can_open=True,
            )
        mock_claude.assert_not_called()

    @pytest.mark.unit
    def test_conviction_not_blocked_by_medium_concurrency_limit(self, monkeypatch, tmp_path):
        """CONVICTION tier (max_open=4) must reach Claude even when MEDIUM slots (max_open=2) are full.

        The old pre-skip used MEDIUM tier for ALL symbols, wrongly blocking CONVICTION entries
        when 2 positions were open. The fix: use the actual tier from apply_pre_claude_filter.
        """
        from trading.pipeline import TradingPipeline
        from trading.pre_claude_filter import FilterResult
        from utils.ticker_quality import TickerQuality
        import config

        monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "open_positions.json")
        monkeypatch.setattr(config, "API_BUDGET_FILE", tmp_path / "api_budget.json")
        monkeypatch.setattr(config, "DAILY_CLOSED_FILE", tmp_path / "daily_closed.json")

        iv = self._make_iv("AAPL", composite=80.0, rsi=60.0)

        # Simulate 2 intraday positions already open — MEDIUM max_open=2 is reached
        two_positions = {
            "A": {"entry_price": 100, "qty": 10, "is_swing": False},
            "B": {"entry_price": 200, "qty": 5,  "is_swing": False},
        }

        conviction_quality = TickerQuality(
            tier="CONVICTION", score=90, unlocks=frozenset(),
            quality_gaps=(), component_scores={},
        )
        conviction_filter = FilterResult(
            decision="CLAUDE", reason="high conviction", quality=conviction_quality
        )

        claude_called_symbols: list[str] = []

        def fake_ask_claude(batch, snap, ai):
            claude_called_symbols.extend(sym for sym, *_ in batch)
            return {}

        with patch("trading.pipeline._fetch_symbol_data", return_value=("AAPL", iv, None, 0.0)), \
             patch("trading.pipeline._ask_claude_batch", side_effect=fake_ask_claude), \
             patch("trading.pipeline.load_open_positions", return_value=two_positions), \
             patch("trading.pipeline.apply_pre_claude_filter", return_value=conviction_filter), \
             patch("trading.executor.execute_buy", return_value=None):
            TradingPipeline().run(
                symbols=["AAPL"], snap=self._snap(),
                ai=MagicMock(), now=datetime(2026, 5, 13, 10, 30, tzinfo=ET),
                alpaca=MagicMock(), portfolio_value=100_000, buying_power=50_000,
                can_open=True,
            )

        assert "AAPL" in claude_called_symbols, (
            "CONVICTION-tier AAPL should reach Claude even when 2 MEDIUM positions are open"
        )

    @pytest.mark.unit
    def test_medium_skipped_when_medium_concurrency_limit_reached(self, monkeypatch, tmp_path):
        """MEDIUM tier (max_open=2) is correctly blocked when 2 positions are already open."""
        from trading.pipeline import TradingPipeline
        from trading.pre_claude_filter import FilterResult
        from utils.ticker_quality import TickerQuality
        import config

        monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "open_positions.json")
        monkeypatch.setattr(config, "API_BUDGET_FILE", tmp_path / "api_budget.json")
        monkeypatch.setattr(config, "DAILY_CLOSED_FILE", tmp_path / "daily_closed.json")

        iv = self._make_iv("AAPL", composite=60.0, rsi=60.0)

        two_positions = {
            "A": {"entry_price": 100, "qty": 10, "is_swing": False},
            "B": {"entry_price": 200, "qty": 5,  "is_swing": False},
        }

        medium_quality = TickerQuality(
            tier="MEDIUM", score=60, unlocks=frozenset(),
            quality_gaps=(), component_scores={},
        )
        medium_filter = FilterResult(
            decision="CLAUDE", reason="medium quality", quality=medium_quality
        )

        claude_called_symbols: list[str] = []

        def fake_ask_claude(batch, snap, ai):
            claude_called_symbols.extend(sym for sym, *_ in batch)
            return {}

        with patch("trading.pipeline._fetch_symbol_data", return_value=("AAPL", iv, None, 0.0)), \
             patch("trading.pipeline._ask_claude_batch", side_effect=fake_ask_claude), \
             patch("trading.pipeline.load_open_positions", return_value=two_positions), \
             patch("trading.pipeline.apply_pre_claude_filter", return_value=medium_filter), \
             patch("trading.executor.execute_buy", return_value=None):
            TradingPipeline().run(
                symbols=["AAPL"], snap=self._snap(),
                ai=MagicMock(), now=datetime(2026, 5, 13, 10, 30, tzinfo=ET),
                alpaca=MagicMock(), portfolio_value=100_000, buying_power=50_000,
                can_open=True,
            )

        assert "AAPL" not in claude_called_symbols, (
            "MEDIUM-tier AAPL must be blocked when 2 positions (MEDIUM max_open=2) are already open"
        )
