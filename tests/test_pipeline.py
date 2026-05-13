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
