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
        from tests.test_signals import make_full_iv
        return make_full_iv(composite_score=composite, rsi=rsi, stoch_d=stoch_d, **kw)

    @pytest.mark.unit
    def test_skip_composite_below_minimum(self):
        from trading.pre_claude_filter import apply_pre_claude_filter
        result = apply_pre_claude_filter("TEST", self._iv(composite=15.0), self._snap())
        assert result.decision == "SKIP"
        assert "composite" in result.reason

    @pytest.mark.unit
    def test_skip_rsi_hard_veto(self):
        from trading.pre_claude_filter import apply_pre_claude_filter
        # composite=65 → MEDIUM tier → passes composite gate; rsi=75 > RSI_BUY_MAX=72 → SKIP
        result = apply_pre_claude_filter("TEST", self._iv(composite=65.0, rsi=75.0), self._snap())
        assert result.decision == "SKIP"
        assert "RSI" in result.reason

    @pytest.mark.unit
    def test_skip_macro_shock(self):
        from trading.pre_claude_filter import apply_pre_claude_filter
        snap = self._snap(research_meta={"macro_shock_score": 60})
        result = apply_pre_claude_filter("TEST", self._iv(composite=65.0), snap)
        assert result.decision == "SKIP"
        assert "shock" in result.reason

    @pytest.mark.unit
    def test_skip_macro_health_too_low(self):
        from trading.pre_claude_filter import apply_pre_claude_filter
        snap = self._snap(regime_info={"regime": "NORMAL", "macro_health_score": -30})
        result = apply_pre_claude_filter("TEST", self._iv(composite=65.0), snap)
        assert result.decision == "SKIP"
        assert "macro health" in result.reason

    @pytest.mark.unit
    def test_skip_bearish_sentiment_hard_block(self):
        from trading.pre_claude_filter import apply_pre_claude_filter
        snap = self._snap(research={"TEST": {"sentiment_score": -20}})
        # composite=65 reaches NORMAL+LOW tier (negative sentiment), composite_min=55, 65>55 passes gate
        result = apply_pre_claude_filter("TEST", self._iv(composite=65.0), snap)
        assert result.decision == "SKIP"
        assert "sentiment" in result.reason

    @pytest.mark.unit
    def test_skip_negative_sentiment_weak_composite(self):
        from trading.pre_claude_filter import apply_pre_claude_filter
        snap = self._snap(research={"TEST": {"sentiment_score": -5}})
        # composite=25 < SENTIMENT_WEAK_COMPOSITE_MIN=30, negative sentiment → SKIP
        result = apply_pre_claude_filter("TEST", self._iv(composite=25.0), snap)
        assert result.decision == "SKIP"

    @pytest.mark.unit
    def test_skip_intraday_shock(self):
        from trading.pre_claude_filter import apply_pre_claude_filter
        snap = self._snap(intraday_news={"TEST": {"shock_score": 65, "sentiment_score": 10}})
        result = apply_pre_claude_filter("TEST", self._iv(composite=65.0), snap)
        assert result.decision == "SKIP"
        assert "shock" in result.reason

    @pytest.mark.unit
    def test_auto_execute_strong_signal(self):
        from trading.pre_claude_filter import apply_pre_claude_filter
        # composite=76 > NORMAL+MEDIUM AE threshold (ep.min=55 + offset=20 = 75), intra_sent=25 > 20
        snap = self._snap(intraday_news={"TEST": {"shock_score": 0, "sentiment_score": 25}})
        result = apply_pre_claude_filter("TEST", self._iv(composite=76.0), snap)
        assert result.decision == "AUTO_EXECUTE"

    @pytest.mark.unit
    def test_auto_execute_not_triggered_below_new_composite_threshold(self):
        """composite=70 (= AE threshold, not strictly >) routes to CLAUDE."""
        from trading.pre_claude_filter import apply_pre_claude_filter
        snap = self._snap(intraday_news={"TEST": {"shock_score": 0, "sentiment_score": 25}})
        result = apply_pre_claude_filter("TEST", self._iv(composite=70.0), snap)
        assert result.decision == "CLAUDE"

    @pytest.mark.unit
    def test_auto_execute_not_triggered_below_new_sentiment_threshold(self):
        """composite=76 > AE threshold but intra_sent=19 < sentiment_min=20 → CLAUDE."""
        from trading.pre_claude_filter import apply_pre_claude_filter
        snap = self._snap(intraday_news={"TEST": {"shock_score": 0, "sentiment_score": 19}})
        result = apply_pre_claude_filter("TEST", self._iv(composite=76.0), snap)
        assert result.decision == "CLAUDE"

    @pytest.mark.unit
    def test_claude_path_moderate_signal(self):
        from trading.pre_claude_filter import apply_pre_claude_filter
        # composite=65 → MEDIUM tier, passes composite gate (55), below AE threshold (75) → CLAUDE
        snap = self._snap(intraday_news={"TEST": {"shock_score": 0, "sentiment_score": 5}})
        result = apply_pre_claude_filter("TEST", self._iv(composite=65.0), snap)
        assert result.decision == "CLAUDE"

    @pytest.mark.unit
    def test_claude_path_no_intraday_cache(self):
        """Falls back to research sentiment when intraday cache is empty."""
        from trading.pre_claude_filter import apply_pre_claude_filter
        snap = self._snap(
            research={"TEST": {"sentiment_score": 25}},
            intraday_news={},  # stale/missing
        )
        result = apply_pre_claude_filter("TEST", self._iv(composite=65.0), snap)
        assert result.decision == "CLAUDE"

    @pytest.mark.unit
    def test_high_vol_regime_raises_composite_bar(self):
        from trading.pre_claude_filter import apply_pre_claude_filter
        snap = self._snap(
            regime_info={"regime": "HIGH_VOL", "macro_health_score": 0},
            regime="HIGH_VOL",
        )
        # composite=30 passes NORMAL (>=30) but fails HIGH_VOL (>=35)
        result = apply_pre_claude_filter("TEST", self._iv(composite=30.0), snap)
        assert result.decision == "SKIP"

    @pytest.mark.unit
    def test_choppy_composite_gate_blocks_below_gate(self):
        """In CHOPPY regime, composite well below CHOPPY_COMPOSITE_GATE should be SKIP."""
        from trading.pre_claude_filter import apply_pre_claude_filter
        snap = self._snap(
            regime_info={"regime": "CHOPPY", "macro_health_score": 0},
            regime="CHOPPY",
        )
        result = apply_pre_claude_filter("TEST", self._iv(composite=25.0), snap)
        assert result.decision == "SKIP"
        assert "composite" in result.reason

    @pytest.mark.unit
    def test_choppy_composite_gate_passes_above_gate(self):
        """In CHOPPY regime, composite well above gate reaches MEDIUM tier and passes Layer 1."""
        from trading.pre_claude_filter import apply_pre_claude_filter
        snap = self._snap(
            regime_info={"regime": "CHOPPY", "macro_health_score": 0},
            regime="CHOPPY",
        )
        # composite=70 → MEDIUM tier in CHOPPY (ep.min=55, score earns enough pts), composite_min=55
        result = apply_pre_claude_filter("TEST", self._iv(composite=70.0), snap)
        assert result.decision != "SKIP" or "composite" not in result.reason

    @pytest.mark.unit
    def test_vwap_slope_not_blocked_in_pre_claude_filter(self):
        """Rule 23: VWAP slope is a safety gate — must NOT be enforced by pre_claude_filter.

        VWAP slope enforcement lives exclusively in check_all_gates() via DayTradeParams.vwap_slope_min.
        pre_claude_filter is a quality/budget router only.
        """
        from trading.pre_claude_filter import apply_pre_claude_filter
        from tests.test_signals import make_full_iv
        snap = self._snap()
        # composite=65 → MEDIUM tier, rsi=55 passes, vwap_slope=-0.5 (downtrend)
        # pre_claude_filter must NOT block on VWAP slope — that is check_all_gates() territory
        iv = make_full_iv(composite_score=65.0, rsi=55.0, stoch_d=50.0, vwap_slope=-0.5)
        result = apply_pre_claude_filter("TEST", iv, snap)
        assert "VWAP slope" not in result.reason, (
            f"Rule 23 violated: VWAP slope blocked in pre_claude_filter: {result.reason}"
        )

    @pytest.mark.unit
    def test_layer_2_5_vwap_overextension_not_blocked_in_filter(self):
        """Option A: price > VWAP+2σ is a safety gate, not a quality signal — must pass filter."""
        from trading.pre_claude_filter import apply_pre_claude_filter
        snap = self._snap()
        iv = self._iv(composite=65.0)
        result = apply_pre_claude_filter("TEST", iv, snap)
        assert result.decision != "SKIP", (
            f"Option A violated: VWAP overextension blocked in pre_claude_filter: {result.reason}"
        )


# ─── budget_guard ────────────────────────────────────────────────────────────

class TestBudgetGuard:

    def _now_et(self, hour, minute=0):
        return datetime(2026, 5, 13, hour, minute, tzinfo=ET)

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

        with patch("trading.risk_manager.config") as mc:
            mc.API_BUDGET_FILE = budget_file
            mc.ENTRY_BUDGET_RATIO = 0.7
            mc.EXIT_BUDGET_RATIO  = 0.3
            mc.API_BUDGET_WARNING_PCT = 0.8
            mc.ENTRY_BUDGET_WARNING_PCT = 0.75
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
        snap.params = {}
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
        from trading.pre_claude_filter import FilterResult
        snap = self._make_snap()

        t_before = time.monotonic()
        with patch("trading.claude_caller.risk_manager.check_entry_budget", return_value=True), \
             patch("trading.claude_caller.build_stock_block", return_value="block"), \
             patch("trading.claude_caller._get_cached_stable_context", return_value="ctx"), \
             patch("trading.claude_caller.build_dynamic_user_context", return_value="dyn"), \
             patch("trading.claude_caller.compute_atr_tp_pct", return_value=5.0), \
             patch("trading.claude_caller.telegram_notify"):
            mock_ai = MagicMock()
            mock_ai.messages.create.side_effect = self._make_exc(429, "rate limit exceeded")
            claude_caller._ask_claude_batch([("TEST", MagicMock(), None, FilterResult("CLAUDE", ""))], snap, mock_ai)

        assert claude_caller._CLAUDE_DISABLED.is_set()
        ttl = claude_caller._CLAUDE_DISABLED_UNTIL - t_before
        assert 290 <= ttl <= 310, f"Expected ~300s TTL, got {ttl:.1f}s"

    @pytest.mark.unit
    def test_402_sets_30min_ttl_and_telegrams(self):
        """HTTP 402 trips circuit breaker with ~1800s TTL and sends Telegram."""
        from trading import claude_caller
        from trading.pre_claude_filter import FilterResult
        snap = self._make_snap()

        t_before = time.monotonic()
        with patch("trading.claude_caller.risk_manager.check_entry_budget", return_value=True), \
             patch("trading.claude_caller.build_stock_block", return_value="block"), \
             patch("trading.claude_caller._get_cached_stable_context", return_value="ctx"), \
             patch("trading.claude_caller.build_dynamic_user_context", return_value="dyn"), \
             patch("trading.claude_caller.compute_atr_tp_pct", return_value=5.0), \
             patch("trading.claude_caller.telegram_notify") as mock_tg:
            mock_ai = MagicMock()
            mock_ai.messages.create.side_effect = self._make_exc(402, "billing error")
            claude_caller._ask_claude_batch([("TEST", MagicMock(), None, FilterResult("CLAUDE", ""))], snap, mock_ai)

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
        claude_caller._CLAUDE_DISABLED_REASON = "stale reason"

        mock_ai = MagicMock()
        mock_ai.messages.create.return_value = MagicMock(
            content=[MagicMock(text='{"TEST": {"action": "HOLD", "confidence": 0, '
                                    '"reasoning": "ok", "stop_loss_pct": 4.0, '
                                    '"take_profit_pct": 5.0, "indicator_signals": {}}}')],
            usage=MagicMock(input_tokens=100, output_tokens=50,
                            cache_creation_input_tokens=0, cache_read_input_tokens=0),
        )

        snap = self._make_snap()
        from trading.pre_claude_filter import FilterResult
        with patch("trading.claude_caller.risk_manager.check_entry_budget", return_value=True), \
             patch("trading.claude_caller.risk_manager.record_usage"), \
             patch("trading.claude_caller.build_stock_block", return_value="block"), \
             patch("trading.claude_caller._get_cached_stable_context", return_value="ctx"), \
             patch("trading.claude_caller.build_dynamic_user_context", return_value="dyn"), \
             patch("trading.claude_caller.compute_atr_tp_pct", return_value=5.0):
            claude_caller._ask_claude_batch([("TEST", MagicMock(), None, FilterResult("CLAUDE", ""))], snap, mock_ai)

        assert not claude_caller._CLAUDE_DISABLED.is_set()
        assert claude_caller._CLAUDE_DISABLED_REASON == ""  # reason cleared on TTL expiry
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
        from trading.pre_claude_filter import FilterResult
        with patch("trading.claude_caller.risk_manager.check_entry_budget", return_value=True), \
             patch("trading.claude_caller.build_stock_block", return_value="block"), \
             patch("trading.claude_caller._get_cached_stable_context", return_value="ctx"), \
             patch("trading.claude_caller.build_dynamic_user_context", return_value="dyn"), \
             patch("trading.claude_caller.compute_atr_tp_pct", return_value=5.0), \
             patch("trading.claude_caller.telegram_notify"):
            mock_ai = MagicMock()
            mock_ai.messages.create.side_effect = self._make_exc(429, "rate limit")
            claude_caller._ask_claude_batch([("TEST", MagicMock(), None, FilterResult("CLAUDE", ""))], snap, mock_ai)

        new_ttl = claude_caller._CLAUDE_DISABLED_UNTIL - t_before
        # remaining was ~200s; doubled = 400s > base 300s → new_ttl should be ~400s
        assert new_ttl >= 390, f"Expected TTL >= 390s (double of ~200s remaining), got {new_ttl:.1f}s"
        assert new_ttl <= 1800, "TTL should not exceed 30 min cap"


# ─── daily_closed as Layer 0 (Task 4: Fix 3) ────────────────────────────────────

class TestPreClaudeFilterDailyClosed:

    def _snap(self):
        from trading.cycle_context import CycleSnapshot
        return CycleSnapshot(
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

    def _iv(self, composite=50.0):
        from tests.test_signals import make_full_iv
        return make_full_iv(composite_score=composite, rsi=55.0, stoch_d=50.0)

    @pytest.mark.unit
    def test_layer0_skips_already_closed_symbol(self):
        """Symbol in daily_closed returns SKIP before any other layer."""
        from trading.pre_claude_filter import apply_pre_claude_filter
        result = apply_pre_claude_filter("TEST", self._iv(composite=80.0), self._snap(),
                                         daily_closed={"TEST"})
        assert result.decision == "SKIP"
        assert "already closed" in result.reason

    @pytest.mark.unit
    def test_layer0_passes_symbol_not_in_closed(self):
        """Symbol absent from daily_closed is not blocked by Layer 0."""
        from trading.pre_claude_filter import apply_pre_claude_filter
        result = apply_pre_claude_filter("TEST", self._iv(composite=50.0), self._snap(),
                                         daily_closed={"OTHER"})
        # Must not be skipped due to "already closed"
        assert not (result.decision == "SKIP" and "already closed" in result.reason)

    @pytest.mark.unit
    def test_layer0_not_triggered_with_empty_set(self):
        """Passing an empty set never triggers Layer 0 SKIP."""
        from trading.pre_claude_filter import apply_pre_claude_filter
        result = apply_pre_claude_filter("TEST", self._iv(composite=50.0), self._snap(),
                                         daily_closed=set())
        if result.decision == "SKIP":
            assert "already closed" not in result.reason

    @pytest.mark.unit
    def test_layer0_not_triggered_when_none(self):
        """daily_closed=None (default) skips Layer 0 entirely."""
        from trading.pre_claude_filter import apply_pre_claude_filter
        result = apply_pre_claude_filter("TEST", self._iv(composite=50.0), self._snap())
        if result.decision == "SKIP":
            assert "already closed" not in result.reason


# ─── auto-execute outside budget gate (Task 5: Fix 5) ───────────────────────

class TestAutoExecuteWhenBudgetExhausted:

    def _make_iv(self, symbol, composite, rsi=55.0, stoch_d=50.0, current_price=100.0):
        from tests.test_signals import make_full_iv
        return make_full_iv(
            symbol=symbol, composite_score=composite, rsi=rsi,
            stoch_d=stoch_d, current_price=current_price,
        )

    def _snap(self, research=None, intraday_news=None):
        from trading.cycle_context import CycleSnapshot
        return CycleSnapshot(
            regime_info={"regime": "NORMAL", "macro_health_score": 0, "spy_5day_return": 0.0},
            regime="NORMAL",
            fred_cache={},
            research=research or {"AUTO": {"sentiment_score": 25}},
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
    def test_auto_execute_runs_when_budget_exhausted(self):
        """AUTO_EXECUTE candidates are collected and execute_buy is called even when entry budget exhausted."""
        from trading.pipeline import TradingPipeline
        from trading import risk_manager

        # composite=76, sentiment=25 → NORMAL+MEDIUM tier, AE threshold=75 (55+20), sentiment_min=20 → AUTO_EXECUTE
        composite = 76.0
        sentiment = 25
        iv = self._make_iv("AUTO", composite=composite)
        snap = self._snap(
            research={"AUTO": {"sentiment_score": sentiment}},
            intraday_news={"AUTO": {"shock_score": 0, "sentiment_score": sentiment}},
        )

        risk_manager._entry_exhausted_flag = True
        try:
            with patch("trading.pipeline._fetch_symbol_data", return_value=("AUTO", iv, None, 0.0)), \
                 patch("trading.pipeline._ask_claude_batch") as mock_claude, \
                 patch("trading.pipeline.execute_buy", return_value=None) as mock_buy, \
                 patch("trading.pipeline.load_open_positions", return_value={}), \
                 patch("trading.pipeline.load_daily_closed", return_value=set()), \
                 patch("trading.pipeline._ask_claude_exit_batch", return_value={}):
                TradingPipeline().run(
                    symbols=["AUTO"], snap=snap,
                    ai=MagicMock(), now=datetime(2026, 5, 13, 10, 30, tzinfo=ET),
                    alpaca=MagicMock(), portfolio_value=10000, buying_power=5000,
                    can_open=True,
                )
            mock_claude.assert_not_called()
            mock_buy.assert_called_once()
        finally:
            risk_manager._entry_exhausted_flag = False

    @pytest.mark.unit
    def test_claude_candidates_blocked_when_budget_exhausted(self):
        """Symbols that route to Claude are held (not collected) when entry budget exhausted."""
        from trading.pipeline import TradingPipeline
        from trading import risk_manager

        # composite=42, sentiment=5 → passes all gates but not AUTO_EXECUTE threshold
        iv = self._make_iv("HOLD_ME", composite=42.0)
        snap = self._snap(
            research={"HOLD_ME": {"sentiment_score": 5}},
            intraday_news={"HOLD_ME": {"shock_score": 0, "sentiment_score": 5}},
        )

        risk_manager._entry_exhausted_flag = True
        try:
            with patch("trading.pipeline._fetch_symbol_data", return_value=("HOLD_ME", iv, None, 0.0)), \
                 patch("trading.pipeline._ask_claude_batch") as mock_claude, \
                 patch("trading.pipeline.load_open_positions", return_value={}), \
                 patch("trading.pipeline.load_daily_closed", return_value=set()), \
                 patch("trading.pipeline._ask_claude_exit_batch", return_value={}):
                TradingPipeline().run(
                    symbols=["HOLD_ME"], snap=snap,
                    ai=MagicMock(), now=datetime(2026, 5, 13, 10, 30, tzinfo=ET),
                    alpaca=MagicMock(), portfolio_value=10000, buying_power=5000,
                    can_open=True,
                )
            mock_claude.assert_not_called()
        finally:
            risk_manager._entry_exhausted_flag = False


from utils.regime_params import (
    get_filter_params, get_gate_params, get_auto_execute_params,
    FilterParams, GateParams, AutoExecuteParams,
)


class TestDynamicRegimeParams:
    @pytest.mark.unit
    def test_filter_params_all_regimes_covered(self):
        from utils.regimes import VALID
        for regime in VALID:
            for tier in ("LOW", "MEDIUM", "HIGH", "CONVICTION"):
                p = get_filter_params(regime, tier)
                assert isinstance(p, FilterParams)

    @pytest.mark.unit
    def test_gate_params_all_regimes_covered(self):
        from utils.regimes import VALID
        for regime in VALID:
            for tier in ("LOW", "MEDIUM", "HIGH", "CONVICTION"):
                p = get_gate_params(regime, tier)
                assert isinstance(p, GateParams)

    @pytest.mark.unit
    def test_auto_execute_disabled_for_crisis(self):
        for tier in ("LOW", "MEDIUM", "HIGH", "CONVICTION"):
            p = get_auto_execute_params("CRISIS", tier)
            assert p.composite_offset == 999  # disabled sentinel

    @pytest.mark.unit
    def test_auto_execute_conviction_normal(self):
        p = get_auto_execute_params("NORMAL", "CONVICTION")
        assert p.composite_offset == 10
        assert p.sentiment_min == 20
        assert p.extra_unlock is None

    @pytest.mark.unit
    def test_auto_execute_conviction_capitulation_requires_pead(self):
        p = get_auto_execute_params("CAPITULATION", "CONVICTION")
        assert p.composite_offset == 20
        assert p.sentiment_min == 30
        assert p.extra_unlock == "PEAD_EXTENDED_WINDOW"

    @pytest.mark.unit
    def test_auto_execute_choppy_low_disabled(self):
        p = get_auto_execute_params("CHOPPY", "LOW")
        assert p.composite_offset == 999

    @pytest.mark.unit
    def test_auto_execute_opportunity_high_requires_sub_etf(self):
        p = get_auto_execute_params("OPPORTUNITY", "HIGH")
        assert p.composite_offset != 999
        assert p.extra_unlock == "SUB_ETF_CONFIRM_03"

    @pytest.mark.unit
    def test_unknown_regime_falls_back_to_normal(self):
        p = get_filter_params("UNKNOWN_REGIME", "MEDIUM")
        normal_p = get_filter_params("NORMAL", "MEDIUM")
        assert p.composite_min == normal_p.composite_min


# ─── TestDynamicPreClaudeFilter ──────────────────────────────────────────────

class TestDynamicPreClaudeFilter:

    def _make_snap(self, regime="NORMAL"):
        from unittest.mock import MagicMock
        snap = MagicMock()
        snap.regime = regime
        snap.research = {}
        snap.intraday_news = {}
        snap.params = {}
        snap.staleness_guard = MagicMock(auto_execute_allowed=True, composite_penalty=0)
        snap.regime_info = {"macro_health_score": 80}
        snap.research_meta = {"macro_shock_score": 0}
        snap.circuit_breaker_composite_boost = 0
        return snap

    @pytest.mark.unit
    def test_filter_result_carries_quality(self):
        from unittest.mock import MagicMock
        from trading.pre_claude_filter import apply_pre_claude_filter
        from utils.ticker_quality import TickerQuality
        snap = self._make_snap("NORMAL")
        iv = MagicMock()
        iv.composite_score = 55.0
        iv.rsi = 60.0
        iv.relative_volume = 1.2
        iv.vwap_slope = 0.01
        result = apply_pre_claude_filter("AAPL", iv, snap)
        assert hasattr(result, "quality")
        assert isinstance(result.quality, TickerQuality)

    @pytest.mark.unit
    def test_defensive_regime_no_duplicate_gate(self):
        import inspect
        import trading.pre_claude_filter as pcf
        src = inspect.getsource(pcf)
        assert 'regime == "DEFENSIVE"' not in src or "Rule 23" in src

    @pytest.mark.unit
    def test_ae_threshold_respects_tier_composite_floor(self, monkeypatch):
        """AE threshold uses max(regime_floor, tier_floor) to prevent premature auto-execute.

        Scenario: fp.composite_min=50, ep.composite_min=30, offset=15, composite=60.
        Old logic: ae_threshold = 30+15 = 45 → 60 > 45 → AUTO_EXECUTE (wrong).
        New logic: ae_threshold = max(30,50)+15 = 65 → 60 < 65 → CLAUDE (correct).
        Layer 1 allows composite=60 because 60 >= fp.composite_min=50.
        """
        from unittest.mock import MagicMock
        import trading.pre_claude_filter as pcf
        from utils.regime_params import FilterParams, AutoExecuteParams, EntryParams

        monkeypatch.setattr(pcf, "get_filter_params",
            lambda regime, tier: FilterParams(composite_min=50, sentiment_hard_block=-50, intraday_block=-50))
        monkeypatch.setattr(pcf, "get_auto_execute_params",
            lambda regime, tier: AutoExecuteParams(composite_offset=15, sentiment_min=-100, extra_unlock=None))
        monkeypatch.setattr(pcf, "get_entry_params",
            lambda regime: EntryParams(composite_min=30, rsi_max=65))

        snap = self._make_snap("NORMAL")
        snap.circuit_breaker_composite_boost = 0
        snap.fred_cache = {}
        # macro health and shock pass layers 3 (health=80 already set in snap)

        iv = MagicMock()
        iv.composite_score = 60.0  # passes Layer1 (≥50); old AE: 30+15=45 → AE; new: max(30,50)+15=65 → CLAUDE
        iv.rsi = 50.0
        iv.relative_volume = 1.2
        iv.vwap_slope = 0.01
        iv.squeeze_setup = False
        iv.bot_herding_score = 0

        result = pcf.apply_pre_claude_filter("AAPL", iv, snap)
        assert result.decision == "CLAUDE", (
            f"Expected CLAUDE (tier floor raises AE bar), got {result.decision}: {result.reason}"
        )


# ── derive_mode 5-mode tests ──────────────────────────────────────────────────
class TestDeriveMode:
    @pytest.mark.unit
    def test_swing_takes_priority_over_pead(self):
        from utils.ticker_quality import derive_mode
        assert derive_mode("AAPL", pead_ok=True, existing_swing_pos=True) == "SWING"

    @pytest.mark.unit
    def test_catalyst_before_day_trade(self):
        from utils.ticker_quality import derive_mode
        assert derive_mode("AAPL", pead_ok=False, catalyst_active=True) == "CATALYST"

    @pytest.mark.unit
    def test_default_is_day_trade(self):
        from utils.ticker_quality import derive_mode
        assert derive_mode("AAPL", pead_ok=False) == "DAY_TRADE"

    @pytest.mark.unit
    def test_pead_before_catalyst(self):
        from utils.ticker_quality import derive_mode
        assert derive_mode("AAPL", pead_ok=True, catalyst_active=True) == "PEAD"


class TestComputeTpSlWithMode:
    @pytest.mark.unit
    def test_mode_param_accepted(self):
        from signals.tp_scaler import compute_tp_sl
        from datetime import datetime
        from utils.constants import ET
        tp, sl = compute_tp_sl(
            base_tp=2.0, base_sl=1.5,
            entry_time=datetime(2026, 5, 24, 10, 30, tzinfo=ET),
            tier="MEDIUM", regime="NORMAL", mode="DAY_TRADE",
        )
        assert 1.5 <= tp <= 4.0   # within NORMAL/MEDIUM/DAY_TRADE TPSLParams bounds
        assert 1.5 <= sl <= 2.0

    @pytest.mark.unit
    def test_swing_mode_uses_swing_table(self):
        from signals.tp_scaler import compute_tp_sl
        from datetime import datetime
        from utils.constants import ET
        tp, sl = compute_tp_sl(
            base_tp=5.0, base_sl=2.0,
            entry_time=datetime(2026, 5, 24, 10, 0, tzinfo=ET),
            tier="CONVICTION", regime="BULL_TREND", mode="SWING",
        )
        # BULL_TREND/CONVICTION/SWING: tp_floor=7.0, tp_ceiling=12.0
        assert 7.0 <= tp <= 12.0

    @pytest.mark.unit
    def test_disabled_combo_returns_fallback(self):
        """Disabled combos return (base_tp, base_sl) instead of raising."""
        from signals.tp_scaler import compute_tp_sl
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime(2024, 1, 15, 11, 0, tzinfo=ZoneInfo("America/New_York"))
        tp, sl = compute_tp_sl(5.0, 2.0, now, tier="MEDIUM", regime="CRISIS")
        assert isinstance(tp, float) and tp > 0
        assert isinstance(sl, float) and sl > 0


class TestContextBuilderAdditions:
    @pytest.mark.unit
    def test_context_includes_entry_countdown(self, monkeypatch):
        from trading.context_builder import build_dynamic_user_context
        from datetime import datetime
        from utils.constants import ET
        from unittest.mock import patch
        with patch("trading.context_builder.get_cached_regime", return_value="NORMAL"), \
             patch("trading.context_builder.load_open_positions", return_value={}), \
             patch("trading.context_builder.is_flash_crash_active", return_value=False):
            ctx = build_dynamic_user_context(
                datetime(2026, 5, 24, 10, 30, tzinfo=ET), 0.0
            )
        assert "Entry window:" in ctx

    @pytest.mark.unit
    def test_context_includes_flash_crash_status(self, monkeypatch):
        from trading.context_builder import build_dynamic_user_context
        from datetime import datetime
        from utils.constants import ET
        from unittest.mock import patch
        with patch("trading.context_builder.get_cached_regime", return_value="NORMAL"), \
             patch("trading.context_builder.load_open_positions", return_value={}), \
             patch("trading.context_builder.is_flash_crash_active", return_value=True):
            ctx = build_dynamic_user_context(
                datetime(2026, 5, 24, 10, 30, tzinfo=ET), 0.0
            )
        assert "Flash crash: ACTIVE" in ctx


@pytest.mark.unit
class TestClaudeCallerCacheControl:
    """Entry and exit batch callers must NOT include cache_control on system blocks.

    10-min trading cycles always exceed Anthropic's 5-min cache TTL, so cache_control
    causes write surcharges on every call with zero cache reads — net cost increase.
    cache_control should remain in research/agent.py and learning/agent.py where
    multiple API calls happen within the 5-min window.
    """

    def _make_snap(self):
        from trading.cycle_context import CycleSnapshot
        from utils.data_health import FRESH_GUARD
        return CycleSnapshot(
            regime_info={"regime": "NORMAL", "macro_health_score": 0,
                         "vix_current": 18.0, "spy_5day_return": 0.5},
            regime="NORMAL", fred_cache={}, research={},
            research_meta={"macro_shock_score": 0},
            strategy_memory="test memory",
            weights={"rsi": 1.0},
            params={
                "rsi_buy_max": 72, "rsi_buy_max_high_vol": 65,
                "high_vol_position_mult": 0.5,
                "entry_start_hour": 10, "entry_end_hour": 15,
                "atr_tp_multiplier": 2.0,
            },
            portfolio_gain_pct=0.0, intraday_news={},
            staleness_guard=FRESH_GUARD,
        )

    def test_entry_batch_system_blocks_have_no_cache_control(self):
        """_ask_claude_batch must not set cache_control on any system block."""
        from unittest.mock import MagicMock, patch
        from tests.test_signals import make_full_iv
        from trading.pre_claude_filter import FilterResult

        captured_kwargs = {}

        def mock_create(**kwargs):
            captured_kwargs.update(kwargs)
            resp = MagicMock()
            resp.usage.input_tokens = 100
            resp.usage.output_tokens = 50
            resp.usage.cache_creation_input_tokens = 0
            resp.usage.cache_read_input_tokens = 0
            resp.content = [MagicMock(type="text", text='{"TEST": {"action": "HOLD", "confidence": 0, "reasoning": "test", "stop_loss_pct": 4.0, "take_profit_pct": 5.0, "indicator_signals": {}}}')]
            return resp

        iv = make_full_iv(symbol="TEST", composite_score=70.0)
        fr = FilterResult(decision="CLAUDE", reason="test", quality=None, gate_result=None)
        snap = self._make_snap()

        with patch("trading.claude_caller.risk_manager.check_entry_budget", return_value=True), \
             patch("trading.claude_caller.risk_manager.record_usage"), \
             patch("trading.claude_caller.build_stable_system_context", return_value="stable ctx"), \
             patch("trading.claude_caller.build_dynamic_user_context", return_value="dyn ctx"), \
             patch("trading.claude_caller.build_stock_block", return_value="stock block"):
            ai = MagicMock()
            ai.messages.create.side_effect = mock_create
            from trading.claude_caller import _ask_claude_batch
            _ask_claude_batch([("TEST", iv, None, fr)], snap, ai)

        system_blocks = captured_kwargs.get("system", [])
        assert system_blocks, "No system blocks captured"
        for block in system_blocks:
            assert "cache_control" not in block, (
                f"cache_control found in entry system block: {block}"
            )
