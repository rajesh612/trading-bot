"""Tests for cycle_context, pre_claude_filter, budget_guard, and pipeline tiers."""
import json
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch, MagicMock
import pytest

ET = ZoneInfo("America/New_York")


# ─── CycleSnapshot / FRED TTL cache ──────────────────────────────────────────

class TestFredTTLCache:

    @pytest.mark.unit
    def test_fred_cache_not_reloaded_within_ttl(self, tmp_path):
        """Second call within TTL returns cached dict without re-reading disk."""
        from trading import cycle_context
        cycle_context._fred_cache = {}
        cycle_context._fred_loaded_at = 0.0

        fred_file = tmp_path / "fred_cache.json"
        fred_file.write_text('{"cpi_trend": "stable"}')

        with patch("trading.cycle_context.config") as mock_cfg:
            mock_cfg.FRED_CACHE_FILE = fred_file
            first = cycle_context._get_fred_cache()
            # Modify file on disk — TTL should prevent re-read
            fred_file.write_text('{"cpi_trend": "rising"}')
            second = cycle_context._get_fred_cache()

        assert first == {"cpi_trend": "stable"}
        assert second == {"cpi_trend": "stable"}  # still cached

    @pytest.mark.unit
    def test_fred_cache_reloaded_after_invalidate(self, tmp_path):
        """invalidate_fred_cache() forces reload on next access."""
        from trading import cycle_context
        cycle_context._fred_cache = {"cpi_trend": "stable"}
        cycle_context._fred_loaded_at = time.monotonic()

        fred_file = tmp_path / "fred_cache.json"
        fred_file.write_text('{"cpi_trend": "rising"}')

        cycle_context.invalidate_fred_cache()

        with patch("trading.cycle_context.config") as mock_cfg:
            mock_cfg.FRED_CACHE_FILE = fred_file
            result = cycle_context._get_fred_cache()

        assert result == {"cpi_trend": "rising"}

    @pytest.mark.unit
    def test_fred_cache_thread_safe(self, tmp_path):
        """Concurrent reads don't corrupt the cache."""
        from trading import cycle_context
        cycle_context._fred_cache = {}
        cycle_context._fred_loaded_at = 0.0

        fred_file = tmp_path / "fred_cache.json"
        fred_file.write_text('{"cpi_trend": "stable"}')
        results = []

        def reader():
            with patch("trading.cycle_context.config") as mock_cfg:
                mock_cfg.FRED_CACHE_FILE = fred_file
                results.append(cycle_context._get_fred_cache())

        threads = [threading.Thread(target=reader) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(r == {"cpi_trend": "stable"} for r in results)


# ─── pre_claude_filter ───────────────────────────────────────────────────────

class TestPreClaudeFilter:

    def _snap(self, **overrides) -> "CycleSnapshot":
        from trading.cycle_context import CycleSnapshot
        defaults = dict(
            regime_info={"regime": "NORMAL", "macro_health_score": 0},
            regime="NORMAL",
            fred_cache={},
            research={"TEST": {"sentiment_score": 20}},
            research_meta={"macro_shock_score": 0},
            strategy_memory="",
            weights={},
            params={},
            portfolio_gain_pct=0.0,
            intraday_news={},
        )
        defaults.update(overrides)
        return CycleSnapshot(**defaults)

    def _iv(self, composite=50.0, rsi=55.0, stoch_d=50.0, **kw):
        from indicators.values import IndicatorValues
        # Read indicators/values.py to see all required fields; fill them all
        # Use same pattern as TestFastGate._iv() in tests/test_signals.py
        from tests.test_signals import TestFastGate
        base = TestFastGate()._iv(composite_score=composite, rsi=rsi, stoch_d=stoch_d, **kw)
        return base

    @pytest.mark.unit
    def test_skip_composite_below_minimum(self):
        from trading.pre_claude_filter import pre_claude_filter
        result = pre_claude_filter("TEST", self._iv(composite=30.0), self._snap())
        assert result.decision == "SKIP"
        assert "composite" in result.reason

    @pytest.mark.unit
    def test_skip_rsi_hard_veto(self):
        from trading.pre_claude_filter import pre_claude_filter
        result = pre_claude_filter("TEST", self._iv(composite=50.0, rsi=75.0), self._snap())
        assert result.decision == "SKIP"
        assert "RSI" in result.reason

    @pytest.mark.unit
    def test_skip_macro_shock(self):
        from trading.pre_claude_filter import pre_claude_filter
        snap = self._snap(research_meta={"macro_shock_score": 60})
        result = pre_claude_filter("TEST", self._iv(), snap)
        assert result.decision == "SKIP"
        assert "shock" in result.reason

    @pytest.mark.unit
    def test_skip_macro_health_too_low(self):
        from trading.pre_claude_filter import pre_claude_filter
        snap = self._snap(regime_info={"regime": "NORMAL", "macro_health_score": -30})
        result = pre_claude_filter("TEST", self._iv(), snap)
        assert result.decision == "SKIP"
        assert "macro health" in result.reason

    @pytest.mark.unit
    def test_skip_bearish_sentiment_hard_block(self):
        from trading.pre_claude_filter import pre_claude_filter
        snap = self._snap(research={"TEST": {"sentiment_score": -20}})
        result = pre_claude_filter("TEST", self._iv(), snap)
        assert result.decision == "SKIP"
        assert "sentiment" in result.reason

    @pytest.mark.unit
    def test_skip_negative_sentiment_weak_composite(self):
        from trading.pre_claude_filter import pre_claude_filter
        snap = self._snap(research={"TEST": {"sentiment_score": -5}})
        result = pre_claude_filter("TEST", self._iv(composite=42.0), snap)
        assert result.decision == "SKIP"

    @pytest.mark.unit
    def test_skip_intraday_shock(self):
        from trading.pre_claude_filter import pre_claude_filter
        snap = self._snap(intraday_news={"TEST": {"shock_score": 65, "sentiment_score": 10}})
        result = pre_claude_filter("TEST", self._iv(), snap)
        assert result.decision == "SKIP"
        assert "shock" in result.reason

    @pytest.mark.unit
    def test_auto_execute_strong_signal(self):
        from trading.pre_claude_filter import pre_claude_filter
        snap = self._snap(intraday_news={"TEST": {"shock_score": 0, "sentiment_score": 20}})
        result = pre_claude_filter("TEST", self._iv(composite=50.0), snap)
        assert result.decision == "AUTO_EXECUTE"

    @pytest.mark.unit
    def test_claude_path_moderate_signal(self):
        from trading.pre_claude_filter import pre_claude_filter
        # composite 42, sentiment 5 — passes all gates but not high enough for auto-execute
        snap = self._snap(intraday_news={"TEST": {"shock_score": 0, "sentiment_score": 5}})
        result = pre_claude_filter("TEST", self._iv(composite=42.0), snap)
        assert result.decision == "CLAUDE"

    @pytest.mark.unit
    def test_claude_path_no_intraday_cache(self):
        """Falls back to research sentiment when intraday cache is empty."""
        from trading.pre_claude_filter import pre_claude_filter
        snap = self._snap(
            research={"TEST": {"sentiment_score": 25}},
            intraday_news={},  # stale/missing
        )
        result = pre_claude_filter("TEST", self._iv(composite=42.0), snap)
        assert result.decision == "CLAUDE"

    @pytest.mark.unit
    def test_high_vol_regime_raises_composite_bar(self):
        from trading.pre_claude_filter import pre_claude_filter
        snap = self._snap(
            regime_info={"regime": "HIGH_VOL", "macro_health_score": 0},
            regime="HIGH_VOL",
        )
        # composite=45 passes NORMAL (>=40) but fails HIGH_VOL (>=50)
        result = pre_claude_filter("TEST", self._iv(composite=45.0), snap)
        assert result.decision == "SKIP"


# ─── budget_guard ────────────────────────────────────────────────────────────

class TestBudgetGuard:

    def _now_et(self, hour, minute=0):
        return datetime(2026, 5, 13, hour, minute, tzinfo=ET)

    @pytest.mark.unit
    def test_morning_cap_blocks_at_35pct_during_morning(self):
        from trading.budget_guard import morning_cap_exceeded
        result = morning_cap_exceeded(
            entry_spent=0.36, entry_ceiling=1.0, now=self._now_et(10, 0)
        )
        assert result is True

    @pytest.mark.unit
    def test_morning_cap_allows_under_35pct(self):
        from trading.budget_guard import morning_cap_exceeded
        result = morning_cap_exceeded(
            entry_spent=0.34, entry_ceiling=1.0, now=self._now_et(10, 0)
        )
        assert result is False

    @pytest.mark.unit
    def test_morning_cap_inactive_after_1130(self):
        from trading.budget_guard import morning_cap_exceeded
        result = morning_cap_exceeded(
            entry_spent=0.99, entry_ceiling=1.0, now=self._now_et(11, 45)
        )
        assert result is False

    @pytest.mark.unit
    def test_midday_cap_blocks_at_65pct(self):
        from trading.budget_guard import midday_cap_exceeded
        result = midday_cap_exceeded(
            entry_spent=0.66, entry_ceiling=1.0, now=self._now_et(12, 0)
        )
        assert result is True

    @pytest.mark.unit
    def test_midday_cap_inactive_after_1pm(self):
        from trading.budget_guard import midday_cap_exceeded
        result = midday_cap_exceeded(
            entry_spent=0.99, entry_ceiling=1.0, now=self._now_et(13, 30)
        )
        assert result is False

    @pytest.mark.unit
    def test_rollover_applied_at_1230(self):
        from trading.budget_guard import apply_exit_rollover
        b = {
            "entry_ceiling_usd": 0.70,
            "exit_ceiling_usd": 0.30,
            "exit_spent_usd": 0.0,
        }
        result = apply_exit_rollover(b, self._now_et(12, 30))
        assert result is True
        assert b["entry_ceiling_usd"] == pytest.approx(0.85, abs=0.01)
        assert b["_rollover_applied"] is True

    @pytest.mark.unit
    def test_rollover_not_applied_twice(self):
        from trading.budget_guard import apply_exit_rollover
        b = {
            "entry_ceiling_usd": 0.70,
            "exit_ceiling_usd": 0.30,
            "exit_spent_usd": 0.0,
            "_rollover_applied": True,
        }
        result = apply_exit_rollover(b, self._now_et(12, 30))
        assert result is False
        assert b["entry_ceiling_usd"] == 0.70

    @pytest.mark.unit
    def test_rollover_not_applied_wrong_time(self):
        from trading.budget_guard import apply_exit_rollover
        b = {"entry_ceiling_usd": 0.70, "exit_ceiling_usd": 0.30, "exit_spent_usd": 0.0}
        result = apply_exit_rollover(b, self._now_et(11, 0))
        assert result is False

    @pytest.mark.unit
    def test_rollover_skipped_if_exit_already_spent(self):
        from trading.budget_guard import apply_exit_rollover
        b = {
            "entry_ceiling_usd": 0.70,
            "exit_ceiling_usd": 0.30,
            "exit_spent_usd": 0.30,
        }
        result = apply_exit_rollover(b, self._now_et(12, 30))
        assert result is False


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
        import json
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
            from trading.risk_manager import try_rollover_exit_to_entry
            try_rollover_exit_to_entry(now)

        updated = json.loads(budget_file.read_text())
        assert updated["entry_ceiling_usd"] == pytest.approx(0.85, abs=0.01)

    @pytest.mark.unit
    def test_check_entry_budget_no_gain_pct(self, tmp_path):
        """check_entry_budget accepts (cost, regime) — no gain_pct argument."""
        import json
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
        with patch("trading.risk_manager.config") as mc, \
             patch("trading.risk_manager.morning_cap_exceeded", return_value=False), \
             patch("trading.risk_manager.midday_cap_exceeded", return_value=False):
            mc.API_BUDGET_FILE = budget_file
            mc.ENTRY_BUDGET_RATIO = 0.7
            mc.EXIT_BUDGET_RATIO  = 0.3
            mc.API_BUDGET_WARNING_PCT = 0.8
            result = risk_manager.check_entry_budget(0.001, "NORMAL")
        assert result is True


# ─── watch list filter ────────────────────────────────────────────────────────

class TestWatchListFilter:

    @pytest.mark.unit
    def test_filters_overbought_symbols(self):
        from watch_list_agent import _apply_technical_filter
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
        from watch_list_agent import _apply_technical_filter
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
        from watch_list_agent import _apply_technical_filter
        snapshots = {}  # no data
        result = _apply_technical_filter(["NVDA"], snapshots, rsi_limit=70, stoch_limit=80)
        assert "NVDA" in result


# ─── CycleSnapshot intraday_news wiring (Task 9) ─────────────────────────────

class TestCycleSnapshotIntradayNews:

    @pytest.mark.unit
    def test_build_snapshot_includes_intraday_news(self, tmp_path):
        """build_cycle_snapshot() populates intraday_news from cache."""
        import json
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


# ─── Pipeline phase-2 tiers (Task 10) ────────────────────────────────────────

class TestPipelinePhase2Tiers:

    def _make_iv(self, symbol, composite, rsi=55.0, stoch_d=50.0, current_price=100.0):
        from tests.test_signals import TestFastGate
        return TestFastGate()._iv(
            symbol=symbol, composite_score=composite, rsi=rsi,
            stoch_d=stoch_d, current_price=current_price
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
        iv = self._make_iv("TEST", composite=25.0)
        with patch("trading.pipeline._fetch_symbol_data", return_value=("TEST", iv, None, 0.0)), \
             patch("trading.pipeline._ask_claude_batch") as mock_claude:
            TradingPipeline().run(
                symbols=["TEST"], snap=self._snap(),
                ai=MagicMock(), now=datetime(2026, 5, 13, 10, 0, tzinfo=ET),
                alpaca=MagicMock(), portfolio_value=10000, buying_power=5000,
                can_open=True,
            )
        mock_claude.assert_not_called()


# ─── entry_exhausted_flag day-reset (Task 2: Fix 1) ──────────────────────────

class TestEntryExhaustedFlagReset:

    def setup_method(self):
        from trading import risk_manager
        risk_manager._entry_exhausted_flag = False

    def teardown_method(self):
        from trading import risk_manager
        risk_manager._entry_exhausted_flag = False

    @pytest.mark.unit
    def test_flag_syncs_to_false_on_fresh_day_budget(self, tmp_path):
        """Stale True flag is cleared when disk budget shows entry_exhausted=False."""
        import json
        from unittest.mock import patch
        from trading import risk_manager
        risk_manager._entry_exhausted_flag = True  # stale from yesterday

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

        with patch("trading.risk_manager.config") as mc, \
             patch("trading.risk_manager.morning_cap_exceeded", return_value=False), \
             patch("trading.risk_manager.midday_cap_exceeded", return_value=False):
            mc.API_BUDGET_FILE = budget_file
            mc.ENTRY_BUDGET_RATIO = 0.7
            mc.EXIT_BUDGET_RATIO  = 0.3
            mc.API_BUDGET_WARNING_PCT = 0.8
            result = risk_manager.check_entry_budget(0.001, "NORMAL")

        assert result is True
        assert risk_manager._entry_exhausted_flag is False

    @pytest.mark.unit
    def test_flag_stays_true_when_disk_entry_exhausted(self, tmp_path):
        """Flag is not cleared when disk budget confirms entry_exhausted=True."""
        import json
        from unittest.mock import patch
        from trading import risk_manager
        risk_manager._entry_exhausted_flag = True

        today = datetime.now(ET).strftime("%Y-%m-%d")
        budget = {
            "date": today, "calls_made": 5, "entry_calls": 5, "exit_calls": 0,
            "input_tokens": 0, "output_tokens": 0, "cache_write_tokens": 0,
            "cache_read_tokens": 0, "estimated_cost_usd": 0.70,
            "entry_spent_usd": 0.70, "exit_spent_usd": 0.0,
            "ceiling_usd": 1.0,
            "entry_ceiling_usd": 0.70, "exit_ceiling_usd": 0.30,
            "ceiling_mode": "normal", "entry_exhausted": True,
            "exit_exhausted": False, "budget_exhausted": False, "escalated": False,
        }
        budget_file = tmp_path / "api_budget.json"
        budget_file.write_text(json.dumps(budget))

        with patch("trading.risk_manager.config") as mc:
            mc.API_BUDGET_FILE = budget_file
            mc.ENTRY_BUDGET_RATIO = 0.7
            mc.EXIT_BUDGET_RATIO  = 0.3
            result = risk_manager.check_entry_budget(0.001, "NORMAL")

        assert result is False
        assert risk_manager._entry_exhausted_flag is True


# ─── Circuit breaker TTL + exponential backoff (Task 3: Fix 2) ───────────────

class TestCircuitBreakerTTL:

    def setup_method(self):
        from trading import claude_caller
        claude_caller._CLAUDE_DISABLED.clear()
        claude_caller._CLAUDE_DISABLED_UNTIL = 0.0
        claude_caller._CLAUDE_DISABLED_REASON = ""

    def teardown_method(self):
        from trading import claude_caller
        claude_caller._CLAUDE_DISABLED.clear()
        claude_caller._CLAUDE_DISABLED_UNTIL = 0.0
        claude_caller._CLAUDE_DISABLED_REASON = ""

    def _make_snap(self):
        """Minimal CycleSnapshot-like mock."""
        snap = MagicMock()
        snap.regime = "NORMAL"
        snap.portfolio_gain_pct = 0.0
        snap.research = {}
        snap.research_meta = {}
        snap.regime_info = {}
        snap.fred_cache = {}
        snap.weights = {}
        snap.strategy_memory = ""
        return snap

    def _make_exc(self, status_code, message="error"):
        """Build a minimal APIStatusError without calling its real __init__."""
        import anthropic
        exc = anthropic.APIStatusError.__new__(anthropic.APIStatusError)
        exc.status_code = status_code
        exc.message = message
        return exc

    @pytest.mark.unit
    def test_429_sets_5min_ttl(self):
        """HTTP 429 trips circuit breaker with ~300s TTL."""
        from trading import claude_caller
        snap = self._make_snap()

        t_before = time.monotonic()
        with patch("trading.claude_caller.risk_manager.check_entry_budget", return_value=True), \
             patch("trading.claude_caller.build_stock_block", return_value="block"), \
             patch("trading.claude_caller._get_cached_stable_context", return_value="ctx"), \
             patch("trading.claude_caller.build_dynamic_user_context", return_value="dyn"), \
             patch("trading.claude_caller.telegram_notify"):
            mock_ai = MagicMock()
            mock_ai.messages.create.side_effect = self._make_exc(429, "rate limit exceeded")
            claude_caller._ask_claude_batch([("TEST", MagicMock(), None)], snap, mock_ai)

        assert claude_caller._CLAUDE_DISABLED.is_set()
        ttl = claude_caller._CLAUDE_DISABLED_UNTIL - t_before
        assert 290 <= ttl <= 310, f"Expected ~300s TTL, got {ttl:.1f}s"

    @pytest.mark.unit
    def test_402_sets_30min_ttl_and_telegrams(self):
        """HTTP 402 trips circuit breaker with ~1800s TTL and sends Telegram."""
        from trading import claude_caller
        snap = self._make_snap()

        t_before = time.monotonic()
        with patch("trading.claude_caller.risk_manager.check_entry_budget", return_value=True), \
             patch("trading.claude_caller.build_stock_block", return_value="block"), \
             patch("trading.claude_caller._get_cached_stable_context", return_value="ctx"), \
             patch("trading.claude_caller.build_dynamic_user_context", return_value="dyn"), \
             patch("trading.claude_caller.telegram_notify") as mock_tg:
            mock_ai = MagicMock()
            mock_ai.messages.create.side_effect = self._make_exc(402, "billing error")
            claude_caller._ask_claude_batch([("TEST", MagicMock(), None)], snap, mock_ai)

        assert claude_caller._CLAUDE_DISABLED.is_set()
        ttl = claude_caller._CLAUDE_DISABLED_UNTIL - t_before
        assert 1790 <= ttl <= 1810, f"Expected ~1800s TTL, got {ttl:.1f}s"
        mock_tg.notify_claude_disabled.assert_called_once()

    @pytest.mark.unit
    def test_circuit_breaker_clears_after_ttl_expires(self):
        """When TTL is expired, _ask_claude_batch clears the event and allows the call."""
        from trading import claude_caller

        # Set circuit breaker with already-expired TTL
        claude_caller._CLAUDE_DISABLED.set()
        claude_caller._CLAUDE_DISABLED_UNTIL = time.monotonic() - 1.0

        mock_ai = MagicMock()
        mock_ai.messages.create.return_value = MagicMock(
            content=[MagicMock(text='{"TEST": {"action": "HOLD", "confidence": 0, '
                                    '"reasoning": "ok", "stop_loss_pct": 4.0, '
                                    '"take_profit_pct": 5.0, "indicator_signals": {}}}')],
            usage=MagicMock(input_tokens=100, output_tokens=50,
                            cache_creation_input_tokens=0, cache_read_input_tokens=0),
        )

        snap = self._make_snap()
        with patch("trading.claude_caller.risk_manager.check_entry_budget", return_value=True), \
             patch("trading.claude_caller.risk_manager.record_usage"), \
             patch("trading.claude_caller.build_stock_block", return_value="block"), \
             patch("trading.claude_caller._get_cached_stable_context", return_value="ctx"), \
             patch("trading.claude_caller.build_dynamic_user_context", return_value="dyn"):
            claude_caller._ask_claude_batch([("TEST", MagicMock(), None)], snap, mock_ai)

        assert not claude_caller._CLAUDE_DISABLED.is_set()
        mock_ai.messages.create.assert_called_once()

    @pytest.mark.unit
    def test_backoff_doubles_remaining_on_probe_failure(self):
        """Re-trip while prior TTL has remaining time doubles the effective TTL."""
        from trading import claude_caller

        # Simulate a situation where the circuit was cleared for a probe but
        # 200s of TTL still remain in _CLAUDE_DISABLED_UNTIL from the previous trip.
        claude_caller._CLAUDE_DISABLED_UNTIL = time.monotonic() + 200.0
        # Event is NOT set (probe in progress)

        snap = self._make_snap()
        t_before = time.monotonic()
        with patch("trading.claude_caller.risk_manager.check_entry_budget", return_value=True), \
             patch("trading.claude_caller.build_stock_block", return_value="block"), \
             patch("trading.claude_caller._get_cached_stable_context", return_value="ctx"), \
             patch("trading.claude_caller.build_dynamic_user_context", return_value="dyn"), \
             patch("trading.claude_caller.telegram_notify"):
            mock_ai = MagicMock()
            mock_ai.messages.create.side_effect = self._make_exc(429, "rate limit")
            claude_caller._ask_claude_batch([("TEST", MagicMock(), None)], snap, mock_ai)

        new_ttl = claude_caller._CLAUDE_DISABLED_UNTIL - t_before
        # remaining was ~200s; doubled = 400s > base 300s → new_ttl should be ~400s
        assert new_ttl >= 390, f"Expected TTL >= 390s (double of ~200s remaining), got {new_ttl:.1f}s"
        assert new_ttl <= 1800, "TTL should not exceed 30 min cap"
