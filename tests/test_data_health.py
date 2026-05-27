"""Tests for DataHealthTracker, StalenessGuard, and get_staleness_guard."""
import pytest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def _now_et(**replace):
    base = datetime.now(ET)
    return base.replace(**replace) if replace else base


@pytest.mark.unit
class TestSourceHealth:
    def test_fresh_guard_is_all_good(self):
        from utils.data_health import FRESH_GUARD
        assert FRESH_GUARD.composite_penalty == 0
        assert FRESH_GUARD.auto_execute_allowed is True
        assert FRESH_GUARD.t2_allowed is True
        assert FRESH_GUARD.swing_fallback_regime is None
        assert FRESH_GUARD.can_open is True
        assert FRESH_GUARD.claude_warnings == ()
        assert FRESH_GUARD.stale_sources == frozenset()


@pytest.mark.unit
class TestDataHealthTracker:
    def _tracker(self):
        from utils.data_health import DataHealthTracker
        return DataHealthTracker()

    def test_record_success_marks_fresh(self):
        t = self._tracker()
        now = _now_et()
        t.record_success("regime", now=now)
        health = t.get_status("regime")
        assert health.status == "fresh"
        assert health.attempts_today == 0
        assert health.next_retry_at is None

    def test_age_minutes_zero_immediately_after_success(self):
        t = self._tracker()
        now = _now_et()
        t.record_success("regime", now=now)
        assert t.age_minutes("regime", now=now) == pytest.approx(0.0, abs=0.1)

    def test_age_minutes_infinity_before_any_success(self):
        t = self._tracker()
        import math
        assert math.isinf(t.age_minutes("regime"))

    def test_record_failure_increments_attempts(self):
        t = self._tracker()
        now = _now_et()
        t.record_failure("regime", now=now)
        assert t.get_status("regime").attempts_today == 1
        t.record_failure("regime", now=now)
        assert t.get_status("regime").attempts_today == 2

    def test_record_failure_sets_next_retry_at(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "DATA_RETRY_BACKOFF_INTRADAY", [1, 2, 5])
        monkeypatch.setattr(config, "DATA_RETRY_MAX", 3)
        t = self._tracker()
        now = _now_et(hour=10, minute=0)   # intraday
        t.record_failure("regime", now=now)
        health = t.get_status("regime")
        expected = now + timedelta(minutes=1)
        assert abs((health.next_retry_at - expected).total_seconds()) < 2

    def test_record_failure_premarket_uses_premarket_schedule(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "DATA_RETRY_BACKOFF_PREMARKET", [1, 3, 8])
        monkeypatch.setattr(config, "DATA_RETRY_MAX", 3)
        t = self._tracker()
        now = _now_et(hour=8, minute=50)   # pre-market
        t.record_failure("regime", now=now)
        health = t.get_status("regime")
        expected = now + timedelta(minutes=1)
        assert abs((health.next_retry_at - expected).total_seconds()) < 2

    def test_can_retry_false_when_attempts_exhausted(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "DATA_RETRY_MAX", 2)
        monkeypatch.setattr(config, "DATA_RETRY_BACKOFF_INTRADAY", [1, 2, 5])
        t = self._tracker()
        now = _now_et(hour=10)
        t.record_failure("regime", now=now)
        t.record_failure("regime", now=now)
        assert t.can_retry("regime", now=now + timedelta(minutes=10)) is False

    def test_can_retry_false_before_next_retry_at(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "DATA_RETRY_MAX", 3)
        monkeypatch.setattr(config, "DATA_RETRY_BACKOFF_INTRADAY", [5, 10, 20])
        t = self._tracker()
        now = _now_et(hour=10)
        t.record_failure("regime", now=now)
        assert t.can_retry("regime", now=now + timedelta(minutes=1)) is False

    def test_can_retry_true_after_backoff_elapsed(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "DATA_RETRY_MAX", 3)
        monkeypatch.setattr(config, "DATA_RETRY_BACKOFF_INTRADAY", [1, 2, 5])
        t = self._tracker()
        now = _now_et(hour=10)
        t.record_failure("regime", now=now)
        assert t.can_retry("regime", now=now + timedelta(minutes=2)) is True

    def test_record_success_resets_attempts(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "DATA_RETRY_MAX", 3)
        monkeypatch.setattr(config, "DATA_RETRY_BACKOFF_INTRADAY", [1, 2, 5])
        t = self._tracker()
        now = _now_et(hour=10)
        t.record_failure("regime", now=now)
        t.record_failure("regime", now=now)
        t.record_success("regime", now=now + timedelta(minutes=5))
        assert t.get_status("regime").attempts_today == 0
        assert t.can_retry("regime", now=now + timedelta(minutes=6)) is False   # fresh now

    def test_stale_sources_includes_aged_source(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "DATA_STALE_THRESHOLD_MINUTES", {"regime": 60})
        t = self._tracker()
        old = _now_et() - timedelta(minutes=90)
        t.record_success("regime", now=old)
        stale = t.stale_sources()
        assert "regime" in stale

    def test_stale_sources_excludes_fresh_source(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "DATA_STALE_THRESHOLD_MINUTES", {"regime": 60})
        t = self._tracker()
        t.record_success("regime", now=_now_et())
        assert "regime" not in t.stale_sources()

    def test_stale_sources_includes_failed_source(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "DATA_RETRY_MAX", 1)
        monkeypatch.setattr(config, "DATA_RETRY_BACKOFF_INTRADAY", [1, 2, 5])
        monkeypatch.setattr(config, "DATA_STALE_THRESHOLD_MINUTES", {"regime": 60})
        t = self._tracker()
        t.record_failure("regime", now=_now_et(hour=10))
        assert "regime" in t.stale_sources()


@pytest.mark.unit
class TestGetStalenessGuard:
    def _tracker_with_stale(self, source_id, monkeypatch):
        import config
        monkeypatch.setattr(config, "DATA_STALE_THRESHOLD_MINUTES", {source_id: 60})
        from utils.data_health import DataHealthTracker
        t = DataHealthTracker()
        t.record_success(source_id, now=_now_et() - timedelta(minutes=90))
        return t

    def test_fresh_tracker_returns_fresh_guard(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "DATA_STALE_THRESHOLD_MINUTES", {})
        monkeypatch.setattr(config, "DATA_COMPOSITE_PENALTY", {})
        monkeypatch.setattr(config, "DATA_DISABLE_AUTO_EXECUTE", frozenset())
        monkeypatch.setattr(config, "DATA_DISABLE_T2", frozenset())
        monkeypatch.setattr(config, "DATA_REGIME_CANOPEN_MAX_MINUTES", 240)
        from utils.data_health import DataHealthTracker, get_staleness_guard
        t = DataHealthTracker()
        guard = get_staleness_guard(tracker=t)
        assert guard.composite_penalty == 0
        assert guard.auto_execute_allowed is True
        assert guard.t2_allowed is True
        assert guard.can_open is True

    def test_stale_regime_disables_auto_execute(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "DATA_STALE_THRESHOLD_MINUTES", {"regime": 60})
        monkeypatch.setattr(config, "DATA_COMPOSITE_PENALTY", {"regime": 10})
        monkeypatch.setattr(config, "DATA_DISABLE_AUTO_EXECUTE", frozenset({"regime"}))
        monkeypatch.setattr(config, "DATA_DISABLE_T2", frozenset({"regime"}))
        monkeypatch.setattr(config, "DATA_REGIME_CANOPEN_MAX_MINUTES", 240)
        monkeypatch.setattr(config, "DATA_SWING_STALE_FALLBACK_REGIME", "NORMAL")
        t = self._tracker_with_stale("regime", monkeypatch)
        from utils.data_health import get_staleness_guard
        guard = get_staleness_guard(tracker=t)
        assert guard.auto_execute_allowed is False
        assert guard.t2_allowed is False
        assert guard.composite_penalty == 10
        assert guard.swing_fallback_regime == "NORMAL"

    def test_stale_research_adds_penalty_only(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "DATA_STALE_THRESHOLD_MINUTES", {"research": 360})
        monkeypatch.setattr(config, "DATA_COMPOSITE_PENALTY", {"research": 5})
        monkeypatch.setattr(config, "DATA_DISABLE_AUTO_EXECUTE", frozenset({"regime"}))
        monkeypatch.setattr(config, "DATA_DISABLE_T2", frozenset({"regime"}))
        monkeypatch.setattr(config, "DATA_REGIME_CANOPEN_MAX_MINUTES", 240)
        monkeypatch.setattr(config, "DATA_SWING_STALE_FALLBACK_REGIME", "NORMAL")
        from utils.data_health import DataHealthTracker, get_staleness_guard
        t = DataHealthTracker()
        t.record_success("research", now=_now_et() - timedelta(minutes=400))
        guard = get_staleness_guard(tracker=t)
        assert guard.composite_penalty == 5
        assert guard.auto_execute_allowed is True
        assert guard.t2_allowed is True

    def test_regime_info_stale_flag_adds_regime_to_stale_sources(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "DATA_STALE_THRESHOLD_MINUTES", {})
        monkeypatch.setattr(config, "DATA_COMPOSITE_PENALTY", {"regime": 10})
        monkeypatch.setattr(config, "DATA_DISABLE_AUTO_EXECUTE", frozenset({"regime"}))
        monkeypatch.setattr(config, "DATA_DISABLE_T2", frozenset({"regime"}))
        monkeypatch.setattr(config, "DATA_REGIME_CANOPEN_MAX_MINUTES", 240)
        monkeypatch.setattr(config, "DATA_SWING_STALE_FALLBACK_REGIME", "NORMAL")
        from utils.data_health import DataHealthTracker, get_staleness_guard
        t = DataHealthTracker()
        regime_info = {"regime": "NORMAL", "_stale": True}
        guard = get_staleness_guard(regime_info=regime_info, tracker=t)
        assert "regime" in guard.stale_sources
        assert guard.auto_execute_allowed is False

    def test_stale_regime_forces_can_open_false_beyond_max(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "DATA_STALE_THRESHOLD_MINUTES", {"regime": 60})
        monkeypatch.setattr(config, "DATA_COMPOSITE_PENALTY", {})
        monkeypatch.setattr(config, "DATA_DISABLE_AUTO_EXECUTE", frozenset())
        monkeypatch.setattr(config, "DATA_DISABLE_T2", frozenset())
        monkeypatch.setattr(config, "DATA_REGIME_CANOPEN_MAX_MINUTES", 240)
        monkeypatch.setattr(config, "DATA_SWING_STALE_FALLBACK_REGIME", "NORMAL")
        from utils.data_health import DataHealthTracker, get_staleness_guard
        t = DataHealthTracker()
        t.record_success("regime", now=_now_et() - timedelta(minutes=250))
        guard = get_staleness_guard(tracker=t)
        assert guard.can_open is False

    def test_claude_warnings_populated_for_stale_sources(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "DATA_STALE_THRESHOLD_MINUTES", {"regime": 60})
        monkeypatch.setattr(config, "DATA_COMPOSITE_PENALTY", {"regime": 10})
        monkeypatch.setattr(config, "DATA_DISABLE_AUTO_EXECUTE", frozenset({"regime"}))
        monkeypatch.setattr(config, "DATA_DISABLE_T2", frozenset({"regime"}))
        monkeypatch.setattr(config, "DATA_REGIME_CANOPEN_MAX_MINUTES", 240)
        monkeypatch.setattr(config, "DATA_SWING_STALE_FALLBACK_REGIME", "NORMAL")
        t = self._tracker_with_stale("regime", monkeypatch)
        from utils.data_health import get_staleness_guard
        guard = get_staleness_guard(tracker=t)
        assert len(guard.claude_warnings) >= 1
        assert any("regime" in w for w in guard.claude_warnings)


# ── should_retry_proactive ────────────────────────────────────────────────────

@pytest.mark.unit
class TestShouldRetryProactive:
    """Proactive age-based retry: fires when source aged out but never failed."""

    def _t(self):
        from utils.data_health import DataHealthTracker
        return DataHealthTracker()

    def _now(self):
        return datetime(2026, 5, 27, 11, 0, 0, tzinfo=ET)

    def test_returns_true_when_stale_by_age(self, monkeypatch):
        """Fires when source aged past threshold with no failure recorded."""
        import config
        monkeypatch.setattr(config, "DATA_STALE_THRESHOLD_MINUTES", {"regime": 60})
        monkeypatch.setattr(config, "DATA_RETRY_MAX", 3)
        t = self._t()
        t.record_success("regime", now=self._now() - timedelta(minutes=90))
        assert t.should_retry_proactive("regime", now=self._now()) is True

    def test_returns_false_when_source_fresh(self, monkeypatch):
        """Does not fire when source is within stale threshold."""
        import config
        monkeypatch.setattr(config, "DATA_STALE_THRESHOLD_MINUTES", {"regime": 60})
        monkeypatch.setattr(config, "DATA_RETRY_MAX", 3)
        t = self._t()
        t.record_success("regime", now=self._now() - timedelta(minutes=30))
        assert t.should_retry_proactive("regime", now=self._now()) is False

    def test_returns_false_when_in_failure_backoff(self, monkeypatch):
        """Does not double-trigger when failure-backoff next_retry_at is set."""
        import config
        monkeypatch.setattr(config, "DATA_STALE_THRESHOLD_MINUTES", {"regime": 60})
        monkeypatch.setattr(config, "DATA_RETRY_MAX", 3)
        monkeypatch.setattr(config, "DATA_RETRY_BACKOFF_INTRADAY", [1, 2, 5])
        t = self._t()
        t.record_success("regime", now=self._now() - timedelta(minutes=90))
        t.record_failure("regime", now=self._now() - timedelta(minutes=2))
        assert t.should_retry_proactive("regime", now=self._now()) is False

    def test_returns_false_when_attempts_exhausted(self, monkeypatch):
        """Stops after DATA_RETRY_MAX attempts today."""
        import config
        monkeypatch.setattr(config, "DATA_STALE_THRESHOLD_MINUTES", {"regime": 60})
        monkeypatch.setattr(config, "DATA_RETRY_MAX", 1)
        monkeypatch.setattr(config, "DATA_RETRY_BACKOFF_INTRADAY", [1])
        t = self._t()
        t.record_success("regime", now=self._now() - timedelta(minutes=90))
        t.record_failure("regime", now=self._now() - timedelta(minutes=5))
        t._state["regime"]["next_retry_at"] = None  # isolate attempts check
        assert t.should_retry_proactive("regime", now=self._now()) is False

    def test_returns_false_when_never_succeeded(self, monkeypatch):
        """Does not fire if source was never fetched successfully."""
        import config
        monkeypatch.setattr(config, "DATA_STALE_THRESHOLD_MINUTES", {"regime": 60})
        monkeypatch.setattr(config, "DATA_RETRY_MAX", 3)
        t = self._t()
        assert t.should_retry_proactive("regime", now=self._now()) is False
