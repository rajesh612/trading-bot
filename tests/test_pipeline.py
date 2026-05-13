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
