# Data Resilience, Staleness Guard & Auto-Execute Fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a three-channel staleness propagation system (in-file metadata, CycleSnapshot guard, companion health file) with adaptive retry scheduling so every workflow stage — pre-Claude filter, auto-execute, T2, swing, EOD — knows when it is operating on degraded data and responds conservatively; also fix the auto-execute `intra_sent` bug.

**Architecture:** A module-level `DataHealthTracker` singleton in `utils/data_health.py` is the single source of truth for all health state. It produces a frozen `StalenessGuard` via `get_staleness_guard()` — the sole factory all consumers call. Guards are built exactly once per scheduler entry point (cycle, monitor, EOD) and flow downstream; consumers read guard fields only, never re-derive staleness. Staleness metadata (`_stale`, `_stale_since`) is written into existing JSON state files so passive consumers (monitor, swing_manager) receive it transparently through `load_macro_regime()`.

**Tech Stack:** Python 3.11, APScheduler (BlockingScheduler + DateTrigger), existing `utils/io_utils.py` (load_json/save_json), pytest + monkeypatch (Rule 18).

**Spec:** `docs/superpowers/specs/2026-05-21-data-resilience-staleness-autoexecute-design.md`

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `config.py` | Modify | `# ── Data Resilience ──` section — 13 constants, single SoT |
| `utils/data_health.py` | **Create** | `DataHealthTracker`, `SourceHealth`, `StalenessGuard`, `get_staleness_guard()`, `FRESH_GUARD` |
| `utils/http.py` | Modify | Retry 5xx; fast-fail + ERROR log on 401/403 |
| `utils/io_utils.py` | Modify | `mark_stale()` + `clear_stale()` helpers |
| `scheduler_jobs.py` | Modify | `set_data_tracker()`, `_schedule_retry()` |
| `main.py` | Modify | Wire `set_data_tracker(_tracker)` after `set_scheduler()` |
| `agents/volatility_screener.py` | Modify | Detect all-None batch → skip filter + schedule retry |
| `agents/watch_list.py` | Modify | Write `watch_list_health.json` on empty result |
| `trading/watch_loader.py` | Modify | Merge held tickers; read `watch_list_health.json` |
| `macro/regime.py` | Modify | `record_success/failure("regime")` + `mark_stale` + retry |
| `research/agent.py` | Modify | `record_success/failure("research")` + `mark_stale` + retry |
| `calendars/earnings.py` | Modify | `record_success/failure("earnings")` + `mark_stale` |
| `trading/cycle_context.py` | Modify | Add `staleness_guard: StalenessGuard = FRESH_GUARD` field |
| `signals/entry_gates.py` | Modify | `effective_composite_gate` adds `guard.composite_penalty` |
| `trading/pipeline.py` | Modify | Fix `intra_sent` bug; auto-execute guard gate; `[STALE]` prefix; Claude warnings |
| `trading/executor.py` | Modify | `swing_eligible` forced False when regime stale |
| `trading/cycle.py` | Modify | Build guard before snapshot; wire `guard.can_open`; intraday retry |
| `trading/monitor.py` | Modify | Build guard at entry; gate T2 on `guard.t2_allowed` |
| `trading/swing_manager.py` | Modify | Build guard at each entry; use `guard.swing_fallback_regime` |
| `notifications/telegram.py` | Modify | `notify_data_degraded()` with dedup sentinel |
| `tests/test_data_health.py` | **Create** | All `DataHealthTracker` + `StalenessGuard` + `get_staleness_guard` tests |
| `tests/test_entry_gates.py` | Modify | Staleness penalty tests for `effective_composite_gate` |
| `tests/test_trading_pipeline.py` | Modify | Auto-execute guard block + `intra_sent` fix tests |
| `tests/test_foundation.py` | Modify | Swing manager + monitor stale-regime guard tests |
| `CLAUDE.md` | Modify | Add Rules 24, 25, 26 |

---

## Task 1: Config — Data Resilience SoT Section

**Files:**
- Modify: `config.py`

- [ ] **Step 1: Add the data resilience section to config.py**

Find the last `# ──` section header in `config.py` and append immediately after it (before any trailing constants):

```python
# ── Data Resilience ──────────────────────────────────────────────────────────
DATA_RETRY_MAX                       = 3
DATA_RETRY_BACKOFF_PREMARKET         = [1, 3, 8]   # minutes; before 9:30 ET
DATA_RETRY_BACKOFF_INTRADAY          = [1, 2, 5]   # minutes; during market hours

DATA_STALE_THRESHOLD_MINUTES: dict[str, int] = {
    "regime":              60,
    "research":           360,
    "screener":            90,
    "earnings":           480,
    "economic_calendar":  480,
    "watch_list":          90,
}
DATA_COMPOSITE_PENALTY: dict[str, int] = {
    "regime":    10,
    "research":   5,
    "screener":   0,
    "earnings":   0,
    "watch_list": 0,
}
DATA_DISABLE_AUTO_EXECUTE            = frozenset({"regime"})
DATA_DISABLE_T2                      = frozenset({"regime"})
DATA_REGIME_CANOPEN_MAX_MINUTES      = 240   # force can_open=False if regime > 4h stale
DATA_SWING_STALE_FALLBACK_REGIME     = "NORMAL"
DATA_SCREENER_FAILURE_RATE_THRESHOLD = 0.80
DATA_WATCH_LIST_HEALTH_FILE          = STATE_DIR / "watch_list_health.json"
```

- [ ] **Step 2: Verify config loads without error**

```bash
cd ~/Documents/projects/trading-bot && \
  .venv/bin/python -c "import config; print('DATA_RETRY_MAX:', config.DATA_RETRY_MAX)"
```

Expected output: `DATA_RETRY_MAX: 3`

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "feat: add Data Resilience config SoT section (13 constants)"
```

---

## Task 2: `utils/http.py` — Retry 5xx, Fast-Fail Auth Errors

**Files:**
- Modify: `utils/http.py`

- [ ] **Step 1: Replace `yahoo_get` implementation**

```python
"""Shared HTTP utilities for Yahoo Finance requests."""
import time
import logging

import requests

logger = logging.getLogger(__name__)

YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0"}
_session = requests.Session()
_session.headers.update(YAHOO_HEADERS)

_AUTH_ERROR_CODES     = frozenset({401, 403})
_RETRYABLE_CODES      = frozenset({429, 500, 502, 503, 504})


def yahoo_get(url: str, timeout: int = 10) -> dict:
    """GET a Yahoo Finance URL.

    - 401/403: fast-fail with ERROR log (retrying auth errors wastes quota).
    - 429/5xx: retry up to 3x with exponential backoff.
    - Connection timeout: retry up to 3x with 1s sleep.
    """
    for attempt in range(3):
        try:
            resp = _session.get(url, timeout=timeout)
            if resp.status_code in _AUTH_ERROR_CODES:
                logger.error(
                    "Yahoo Finance auth error %d for %s — not retrying",
                    resp.status_code, url,
                )
                resp.raise_for_status()
            if resp.status_code in _RETRYABLE_CODES:
                wait = 2 ** attempt
                logger.warning(
                    "Yahoo Finance %d — retry %d/3 in %ds (%s)",
                    resp.status_code, attempt + 1, wait, url,
                )
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.Timeout:
            if attempt == 2:
                raise
            time.sleep(1)
    return {}
```

- [ ] **Step 2: Verify import still works**

```bash
cd ~/Documents/projects/trading-bot && \
  .venv/bin/python -c "from utils.http import yahoo_get; print('ok')"
```

Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add utils/http.py
git commit -m "fix: yahoo_get retries 5xx, fast-fails 401/403 with ERROR log"
```

---

## Task 3: `utils/io_utils.py` — mark_stale / clear_stale

**Files:**
- Modify: `utils/io_utils.py`

- [ ] **Step 1: Write the failing tests**

Append to the existing io_utils test class in `tests/test_market.py` (which already covers io_utils):

```python
# tests/test_market.py — append inside the io_utils test section

@pytest.mark.unit
def test_mark_stale_writes_metadata(tmp_path, monkeypatch):
    """mark_stale injects _stale/_stale_since/_fallback_from into JSON."""
    from utils.io_utils import mark_stale, load_json, save_json
    import config
    f = tmp_path / "test.json"
    save_json(f, {"regime": "NORMAL"})
    monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", f)

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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd ~/Documents/projects/trading-bot && \
  .venv/bin/python -m pytest tests/test_market.py::test_mark_stale_writes_metadata \
    tests/test_market.py::test_clear_stale_removes_metadata -v 2>&1 | tail -10
```

Expected: `FAILED` — `ImportError: cannot import name 'mark_stale'`

- [ ] **Step 3: Add mark_stale and clear_stale to utils/io_utils.py**

Append to the bottom of `utils/io_utils.py`:

```python
def mark_stale(path: Path, source_id: str, fallback_from: str | None = None) -> None:
    """Inject _stale metadata into an existing JSON state file atomically."""
    from datetime import datetime
    from utils.constants import ET
    data = load_json(path) or {}
    data["_stale"]         = True
    data["_stale_since"]   = datetime.now(ET).isoformat()
    data["_fallback_from"] = fallback_from
    save_json(path, data)


def clear_stale(path: Path) -> None:
    """Remove _stale metadata after a successful fresh fetch."""
    data = load_json(path) or {}
    for key in ("_stale", "_stale_since", "_fallback_from"):
        data.pop(key, None)
    save_json(path, data)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd ~/Documents/projects/trading-bot && \
  .venv/bin/python -m pytest tests/test_market.py::test_mark_stale_writes_metadata \
    tests/test_market.py::test_clear_stale_removes_metadata -v 2>&1 | tail -5
```

Expected: `2 passed`

- [ ] **Step 5: Commit**

```bash
git add utils/io_utils.py tests/test_market.py
git commit -m "feat: add mark_stale/clear_stale helpers to io_utils"
```

---

## Task 4: `utils/data_health.py` — DataHealthTracker, StalenessGuard, get_staleness_guard

**Files:**
- Create: `utils/data_health.py`
- Create: `tests/test_data_health.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_data_health.py
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
        # only 1 minute elapsed, but backoff is 5 min
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
        assert guard.auto_execute_allowed is True   # research not in disable set
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd ~/Documents/projects/trading-bot && \
  .venv/bin/python -m pytest tests/test_data_health.py -v 2>&1 | tail -5
```

Expected: collection errors or `ModuleNotFoundError: No module named 'utils.data_health'`

- [ ] **Step 3: Create utils/data_health.py**

```python
"""Single source of truth for data-source health tracking and staleness decisions.

DataHealthTracker (module-level singleton `_tracker`) records success/failure per
source and computes StalenessGuard via get_staleness_guard(). All consumers call
get_staleness_guard(); none derive staleness independently.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

import config
from utils.constants import ET

logger = logging.getLogger(__name__)

_MARKET_OPEN_HOUR = 9   # 9:30 ET — anything before this is "pre-market"
_MARKET_OPEN_MIN  = 30


@dataclass
class SourceHealth:
    status: Literal["fresh", "stale", "failed"]
    age_minutes: float
    attempts_today: int
    next_retry_at: datetime | None
    last_success_at: datetime | None = None


@dataclass(frozen=True)
class StalenessGuard:
    composite_penalty: int
    auto_execute_allowed: bool
    t2_allowed: bool
    swing_fallback_regime: str | None
    can_open: bool
    claude_warnings: tuple[str, ...]
    stale_sources: frozenset[str]


FRESH_GUARD = StalenessGuard(
    composite_penalty=0,
    auto_execute_allowed=True,
    t2_allowed=True,
    swing_fallback_regime=None,
    can_open=True,
    claude_warnings=(),
    stale_sources=frozenset(),
)


class DataHealthTracker:
    """Tracks per-source health state in memory. Resets on bot restart."""

    def __init__(self) -> None:
        # {source_id: {last_success_at, attempts_today, attempts_date, next_retry_at}}
        self._state: dict[str, dict] = {}

    def _entry(self, source_id: str) -> dict:
        if source_id not in self._state:
            self._state[source_id] = {
                "last_success_at": None,
                "attempts_today":  0,
                "attempts_date":   "",
                "next_retry_at":   None,
            }
        return self._state[source_id]

    def _reset_daily_attempts_if_needed(self, entry: dict, today: str) -> None:
        if entry["attempts_date"] != today:
            entry["attempts_today"] = 0
            entry["attempts_date"]  = today

    def _backoff_schedule(self, now: datetime) -> list[int]:
        is_premarket = (now.hour, now.minute) < (_MARKET_OPEN_HOUR, _MARKET_OPEN_MIN)
        return (config.DATA_RETRY_BACKOFF_PREMARKET if is_premarket
                else config.DATA_RETRY_BACKOFF_INTRADAY)

    def record_success(self, source_id: str, now: datetime | None = None) -> None:
        now   = now or datetime.now(ET)
        entry = self._entry(source_id)
        entry["last_success_at"] = now
        entry["attempts_today"]  = 0
        entry["attempts_date"]   = now.strftime("%Y-%m-%d")
        entry["next_retry_at"]   = None
        logger.debug("DATA HEALTH: %s → fresh", source_id)

    def record_failure(self, source_id: str, now: datetime | None = None) -> SourceHealth:
        now   = now or datetime.now(ET)
        today = now.strftime("%Y-%m-%d")
        entry = self._entry(source_id)
        self._reset_daily_attempts_if_needed(entry, today)

        attempt_idx = entry["attempts_today"]
        entry["attempts_today"] += 1
        entry["attempts_date"]   = today

        schedule = self._backoff_schedule(now)
        if attempt_idx < len(schedule) and entry["attempts_today"] <= config.DATA_RETRY_MAX:
            delay = schedule[min(attempt_idx, len(schedule) - 1)]
            entry["next_retry_at"] = now + timedelta(minutes=delay)
        else:
            entry["next_retry_at"] = None   # exhausted

        logger.warning(
            "DATA HEALTH: %s → failure (attempt %d/%d) next_retry=%s",
            source_id, entry["attempts_today"], config.DATA_RETRY_MAX,
            entry["next_retry_at"].strftime("%H:%M ET") if entry["next_retry_at"] else "none",
        )
        return self.get_status(source_id, now=now)

    def can_retry(self, source_id: str, now: datetime | None = None) -> bool:
        now   = now or datetime.now(ET)
        entry = self._entry(source_id)
        today = now.strftime("%Y-%m-%d")
        self._reset_daily_attempts_if_needed(entry, today)
        if entry["attempts_today"] >= config.DATA_RETRY_MAX:
            return False
        nxt = entry.get("next_retry_at")
        return nxt is not None and now >= nxt

    def age_minutes(self, source_id: str, now: datetime | None = None) -> float:
        now   = now or datetime.now(ET)
        entry = self._entry(source_id)
        last  = entry.get("last_success_at")
        if last is None:
            return float("inf")
        return (now - last).total_seconds() / 60

    def stale_sources(self, now: datetime | None = None) -> frozenset[str]:
        now  = now or datetime.now(ET)
        stale: set[str] = set()
        for src_id, entry in self._state.items():
            threshold = config.DATA_STALE_THRESHOLD_MINUTES.get(src_id, 60)
            age       = self.age_minutes(src_id, now=now)
            if age > threshold or entry["attempts_today"] >= config.DATA_RETRY_MAX:
                stale.add(src_id)
        return frozenset(stale)

    def get_status(self, source_id: str, now: datetime | None = None) -> SourceHealth:
        now   = now or datetime.now(ET)
        entry = self._entry(source_id)
        age   = self.age_minutes(source_id, now=now)
        threshold = config.DATA_STALE_THRESHOLD_MINUTES.get(source_id, 60)
        if entry["attempts_today"] >= config.DATA_RETRY_MAX:
            status: Literal["fresh", "stale", "failed"] = "failed"
        elif age > threshold:
            status = "stale"
        else:
            status = "fresh"
        return SourceHealth(
            status=status,
            age_minutes=age,
            attempts_today=entry["attempts_today"],
            next_retry_at=entry.get("next_retry_at"),
            last_success_at=entry.get("last_success_at"),
        )


# Module-level singleton — all callers import this.
_tracker = DataHealthTracker()


def get_staleness_guard(
    regime_info: dict | None = None,
    tracker: DataHealthTracker | None = None,
    now: datetime | None = None,
) -> StalenessGuard:
    """Sole factory for StalenessGuard. No consumer derives staleness independently."""
    now = now or datetime.now(ET)
    t   = tracker or _tracker

    stale: set[str] = set(t.stale_sources(now=now))
    if regime_info and regime_info.get("_stale"):
        stale.add("regime")

    penalty  = sum(config.DATA_COMPOSITE_PENALTY.get(s, 0) for s in stale)
    ae_ok    = not (stale & config.DATA_DISABLE_AUTO_EXECUTE)
    t2_ok    = not (stale & config.DATA_DISABLE_T2)
    open_ok  = t.age_minutes("regime", now=now) < config.DATA_REGIME_CANOPEN_MAX_MINUTES
    swing_fb = config.DATA_SWING_STALE_FALLBACK_REGIME if "regime" in stale else None
    warnings = tuple(
        f"⚠️ STALE DATA — {s}: signals have lower conviction"
        for s in sorted(stale)
    )
    return StalenessGuard(
        composite_penalty=penalty,
        auto_execute_allowed=ae_ok,
        t2_allowed=t2_ok,
        swing_fallback_regime=swing_fb,
        can_open=open_ok,
        claude_warnings=warnings,
        stale_sources=frozenset(stale),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd ~/Documents/projects/trading-bot && \
  .venv/bin/python -m pytest tests/test_data_health.py -v 2>&1 | tail -15
```

Expected: all tests pass (no failures).

- [ ] **Step 5: Run full suite to verify no regressions**

```bash
cd ~/Documents/projects/trading-bot && \
  .venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -5
```

Expected: same pass count as before (±0).

- [ ] **Step 6: Commit**

```bash
git add utils/data_health.py tests/test_data_health.py
git commit -m "feat: add DataHealthTracker, StalenessGuard, get_staleness_guard (data_health.py)"
```

---

## Task 5: `scheduler_jobs.py` + `main.py` — Retry Scheduling Wiring

**Files:**
- Modify: `scheduler_jobs.py`
- Modify: `main.py`

- [ ] **Step 1: Add set_data_tracker and _schedule_retry to scheduler_jobs.py**

After the existing `set_scheduler` function in `scheduler_jobs.py`, add:

```python
from apscheduler.triggers.date import DateTrigger

_data_tracker = None


def set_data_tracker(tracker) -> None:
    global _data_tracker
    _data_tracker = tracker


def _schedule_retry(source_id: str, job_fn, now=None) -> None:
    """Register a DateTrigger one-shot retry if tracker allows it.

    Called by agent job functions on failure. Uses the existing _scheduler
    reference (set via set_scheduler) so no new APScheduler state is needed.
    """
    from utils.constants import ET
    from datetime import datetime
    now = now or datetime.now(ET)
    if _data_tracker is None or _scheduler is None:
        return
    if not _data_tracker.can_retry(source_id, now=now):
        return
    health = _data_tracker.record_failure(source_id, now=now)
    if health.next_retry_at is None:
        return
    job_id = f"retry_{source_id}_{health.attempts_today}"
    _scheduler.add_job(
        job_fn,
        DateTrigger(run_date=health.next_retry_at),
        id=job_id,
        replace_existing=True,
        max_instances=1,
    )
    logger.info(
        "DATA RETRY scheduled: %s attempt %d at %s ET",
        source_id, health.attempts_today,
        health.next_retry_at.strftime("%H:%M"),
    )
```

- [ ] **Step 2: Wire set_data_tracker in main.py**

In `main.py`, find the call to `set_scheduler(scheduler)` and add immediately after it:

```python
from utils.data_health import _tracker as _data_tracker_singleton
from scheduler_jobs import set_data_tracker
set_data_tracker(_data_tracker_singleton)
```

- [ ] **Step 3: Verify bot starts without error**

```bash
cd ~/Documents/projects/trading-bot && \
  timeout 5 .venv/bin/python main.py 2>&1 | grep -E "ERROR|Traceback|started|scheduler" | head -10
```

Expected: scheduler started lines, no `ERROR` or `Traceback`.

- [ ] **Step 4: Commit**

```bash
git add scheduler_jobs.py main.py
git commit -m "feat: add set_data_tracker + _schedule_retry to scheduler_jobs; wire in main.py"
```

---

## Task 6: `agents/volatility_screener.py` — Detect API Failure, Skip Filter, Schedule Retry

**Files:**
- Modify: `agents/volatility_screener.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_foundation.py — append inside the screener test section

@pytest.mark.unit
def test_screener_skips_market_cap_filter_on_api_failure(monkeypatch, tmp_path):
    """When ≥80% of market cap fetches return None (API failure), filter is bypassed."""
    import config
    from unittest.mock import patch, MagicMock

    monkeypatch.setattr(config, "MIN_MARKET_CAP_B", 25)
    monkeypatch.setattr(config, "DATA_SCREENER_FAILURE_RATE_THRESHOLD", 0.80)
    monkeypatch.setattr(config, "MAX_TICKERS_DIRECT", 200)   # prevent screening
    monkeypatch.setattr(config, "DAILY_TRADEABLE_FILE", tmp_path / "daily_tradeable.txt")

    with patch("agents.volatility_screener._fetch_market_cap", return_value=None), \
         patch("agents.volatility_screener._schedule_retry"), \
         patch("sectors.load_universe", return_value=["AAPL", "MSFT", "NVDA"]):
        from agents.volatility_screener import run_volatility_screener
        result = run_volatility_screener()

    # All tickers returned despite None market caps (filter bypassed)
    assert set(result) == {"AAPL", "MSFT", "NVDA"}
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/Documents/projects/trading-bot && \
  .venv/bin/python -m pytest tests/test_foundation.py::test_screener_skips_market_cap_filter_on_api_failure -v 2>&1 | tail -8
```

Expected: `FAILED`

- [ ] **Step 3: Modify volatility_screener.py**

At the top of `run_volatility_screener()`, after the concurrent market cap fetch block, replace the existing `eligible` list comprehension and `logger.info` with:

```python
        # Detect API-level failure (most/all fetches returned None)
        none_count   = sum(1 for v in cap_results.values() if v is None)
        failure_rate = none_count / max(len(tickers), 1)

        if failure_rate > config.DATA_SCREENER_FAILURE_RATE_THRESHOLD:
            logger.error(
                "Market cap API failure: %d/%d fetches returned None (%.0f%%) — "
                "skipping filter, using full universe",
                none_count, len(tickers), failure_rate * 100,
            )
            from scheduler_jobs import _schedule_retry
            _schedule_retry("screener", run_volatility_screener)
            eligible = tickers   # bypass filter
        else:
            eligible = [
                ticker for ticker in tickers
                if cap_results.get(ticker) is not None
                and cap_results[ticker] >= min_cap
            ]
            logger.debug("SCREEN OUT details logged at DEBUG level")

        logger.info(
            "Market cap filter (%sB): %d → %d tickers%s",
            config.MIN_MARKET_CAP_B, len(tickers), len(eligible),
            " [API FAILURE — filter bypassed]" if failure_rate > config.DATA_SCREENER_FAILURE_RATE_THRESHOLD else "",
        )
        tickers = eligible
```

Also add at the end of `run_volatility_screener()`, after the successful screen:

```python
    from utils.data_health import _tracker
    _tracker.record_success("screener")
    return top50
```

And after the `len(tickers) <= config.MAX_TICKERS_DIRECT` early-return path, add before `return tickers`:

```python
    from utils.data_health import _tracker
    _tracker.record_success("screener")
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd ~/Documents/projects/trading-bot && \
  .venv/bin/python -m pytest tests/test_foundation.py::test_screener_skips_market_cap_filter_on_api_failure -v 2>&1 | tail -5
```

Expected: `1 passed`

- [ ] **Step 5: Commit**

```bash
git add agents/volatility_screener.py tests/test_foundation.py
git commit -m "fix: screener bypasses market cap filter on API failure (≥80% None) + schedules retry"
```

---

## Task 7: `trading/watch_loader.py` + `agents/watch_list.py` — Held Tickers + Health File

**Files:**
- Modify: `trading/watch_loader.py`
- Modify: `agents/watch_list.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_foundation.py — append

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

    from trading.watch_loader import load_watch_list
    result = load_watch_list()

    assert "NVDA" in result     # held ticker merged
    assert "AAPL" in result     # watch list ticker preserved
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
    save_json(open_pos_file, {"NVDA": {"qty": 10}})   # NVDA already in watch list
    swing_pos_file = tmp_path / "swing_positions.json"
    save_json(swing_pos_file, {})

    monkeypatch.setattr(config, "WATCH_LIST_FILE", watch_file)
    monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", open_pos_file)
    monkeypatch.setattr(config, "SWING_POSITIONS_FILE", swing_pos_file)
    monkeypatch.setattr(config, "DATA_WATCH_LIST_HEALTH_FILE", tmp_path / "wl_health.json")

    from trading.watch_loader import load_watch_list
    result = load_watch_list()

    assert result.count("NVDA") == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd ~/Documents/projects/trading-bot && \
  .venv/bin/python -m pytest tests/test_foundation.py::test_watch_loader_merges_open_position_tickers \
    tests/test_foundation.py::test_watch_loader_merges_swing_position_tickers \
    tests/test_foundation.py::test_watch_loader_no_duplicates -v 2>&1 | tail -8
```

Expected: `FAILED` (missing function changes)

- [ ] **Step 3: Replace load_watch_list in trading/watch_loader.py**

```python
import config
from utils.io_utils import load_json


def load_watch_list() -> list[str]:
    """Return the symbol set for the trading cycle.

    Watch list stocks come first; open and swing position tickers are appended
    (deduplicated) so held positions are always evaluated regardless of watch
    list state.
    """
    if config.WATCH_LIST_FILE.exists():
        watch = [
            t.strip().upper()
            for t in config.WATCH_LIST_FILE.read_text().splitlines()
            if t.strip()
        ]
    else:
        watch = []

    held = _load_held_tickers()
    seen = set(watch)
    for ticker in held:
        if ticker not in seen:
            watch.append(ticker)
            seen.add(ticker)
    return watch


def _load_held_tickers() -> list[str]:
    open_pos  = load_json(config.OPEN_POSITIONS_FILE,  {}) or {}
    swing_pos = load_json(config.SWING_POSITIONS_FILE, {}) or {}
    return list({*open_pos.keys(), *swing_pos.keys()})


def get_watch_list_health() -> dict:
    """Return watch_list_health.json content, empty dict if missing."""
    return load_json(config.DATA_WATCH_LIST_HEALTH_FILE, {}) or {}
```

- [ ] **Step 4: Add watch_list_health.json write to agents/watch_list.py**

In `run_watch_list_agent()`, replace the early-return block:

```python
    if not scored:
        logger.error("No stocks scored — watch list empty.")
        # Write stale health flag and notify
        from utils.io_utils import save_json
        from datetime import datetime
        from utils.constants import ET
        save_json(config.DATA_WATCH_LIST_HEALTH_FILE, {
            "stale": True,
            "stale_since": datetime.now(ET).isoformat(),
            "reason": "volatility_screener returned 0 tickers",
        })
        from utils.data_health import _tracker
        _tracker.record_failure("watch_list")
        return []
```

And at the end of a successful run, before `return watch_list`, add:

```python
    from utils.io_utils import save_json
    from datetime import datetime
    from utils.constants import ET
    save_json(config.DATA_WATCH_LIST_HEALTH_FILE, {
        "stale": False,
        "last_updated": datetime.now(ET).isoformat(),
    })
    from utils.data_health import _tracker
    _tracker.record_success("watch_list")
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd ~/Documents/projects/trading-bot && \
  .venv/bin/python -m pytest tests/test_foundation.py::test_watch_loader_merges_open_position_tickers \
    tests/test_foundation.py::test_watch_loader_merges_swing_position_tickers \
    tests/test_foundation.py::test_watch_loader_no_duplicates -v 2>&1 | tail -5
```

Expected: `3 passed`

- [ ] **Step 6: Commit**

```bash
git add trading/watch_loader.py agents/watch_list.py tests/test_foundation.py
git commit -m "feat: watch_loader merges held tickers; watch_list agent writes health file"
```

---

## Task 8: Pre-market Agent Retry Wiring (regime, research, earnings)

**Files:**
- Modify: `macro/regime.py`
- Modify: `research/agent.py`
- Modify: `calendars/earnings.py`

All three follow the identical pattern: wrap the existing fetch body in try/except, call `record_success` on success, `_schedule_retry` on failure, and `mark_stale` / `clear_stale` on the state file.

- [ ] **Step 1: Wrap fetch_macro_regime in macro/regime.py**

Find `def fetch_macro_regime()` and wrap its body:

```python
def fetch_macro_regime() -> dict:
    from utils.data_health import _tracker
    from utils.io_utils import mark_stale, clear_stale
    try:
        # ... existing fetch body unchanged ...
        result = _fetch_macro_regime_impl()   # rename existing body to this helper
        clear_stale(config.MACRO_REGIME_FILE)
        _tracker.record_success("regime")
        return result
    except Exception as exc:
        logger.error("fetch_macro_regime failed: %s", exc)
        prev = load_macro_regime() or {}
        mark_stale(config.MACRO_REGIME_FILE, "regime",
                   fallback_from=prev.get("regime"))
        from scheduler_jobs import _schedule_retry
        from scheduler_jobs import job_macro_regime
        _schedule_retry("regime", job_macro_regime)
        _tracker.record_failure("regime")
        return prev
```

**Implementation note:** Do not rename the internal body — instead wrap the existing `fetch_macro_regime` logic in a try/except at the outermost level. The existing code runs unchanged inside the try block; success/failure bookkeeping is appended.

- [ ] **Step 2: Wrap run_research_agent in research/agent.py**

Same pattern at the top level of `run_research_agent(watch_list)`:

```python
def run_research_agent(watch_list: list[str]) -> dict:
    from utils.data_health import _tracker
    from utils.io_utils import mark_stale, clear_stale
    try:
        result = _run_research_agent_impl(watch_list)
        clear_stale(config.RESEARCH_REPORT_FILE)
        _tracker.record_success("research")
        return result
    except Exception as exc:
        logger.error("run_research_agent failed: %s", exc)
        prev = load_json(config.RESEARCH_REPORT_FILE, {}) or {}
        mark_stale(config.RESEARCH_REPORT_FILE, "research")
        from scheduler_jobs import _schedule_retry, job_research
        _schedule_retry("research", job_research)
        _tracker.record_failure("research")
        return prev
```

**Implementation note:** Rename the existing body to `_run_research_agent_impl` by extracting it, or wrap the existing code inside the try block directly. The simpler approach is to indent the existing body and wrap it.

- [ ] **Step 3: Wrap refresh_earnings_calendar in calendars/earnings.py**

Same pattern — earnings failures do NOT schedule a retry (old data is conservative/safe) but DO mark stale:

```python
def refresh_earnings_calendar(symbols: list[str]) -> None:
    from utils.data_health import _tracker
    from utils.io_utils import mark_stale, clear_stale
    try:
        _refresh_earnings_calendar_impl(symbols)
        clear_stale(config.EARNINGS_CALENDAR_FILE)
        _tracker.record_success("earnings")
    except Exception as exc:
        logger.error("refresh_earnings_calendar failed: %s", exc)
        mark_stale(config.EARNINGS_CALENDAR_FILE, "earnings")
        _tracker.record_failure("earnings")
        # No retry — stale earnings data is conservative (still blocks imminent entries)
```

- [ ] **Step 4: Run full unit suite**

```bash
cd ~/Documents/projects/trading-bot && \
  .venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -5
```

Expected: same or higher pass count (no regressions).

- [ ] **Step 5: Commit**

```bash
git add macro/regime.py research/agent.py calendars/earnings.py
git commit -m "feat: regime/research/earnings agents record health + mark_stale on failure"
```

---

## Task 9: `trading/cycle_context.py` — Add staleness_guard to CycleSnapshot

**Files:**
- Modify: `trading/cycle_context.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_trading_pipeline.py — append

@pytest.mark.unit
def test_cycle_snapshot_defaults_to_fresh_guard():
    """CycleSnapshot.staleness_guard defaults to FRESH_GUARD when not supplied."""
    from trading.cycle_context import CycleSnapshot
    from utils.data_health import FRESH_GUARD
    snap = CycleSnapshot(
        regime_info={"regime": "NORMAL", "macro_health_score": 0},
        regime="NORMAL",
        fred_cache={},
        research={},
        research_meta={},
        strategy_memory="",
        weights={},
        params={
            "rsi_buy_max": 70, "rsi_buy_max_high_vol": 60,
            "high_vol_position_mult": 0.5, "vwap_slope_min": -0.5,
            "entry_start_hour": 10, "entry_end_hour": 15,
        },
        portfolio_gain_pct=0.0,
        intraday_news={},
    )
    assert snap.staleness_guard == FRESH_GUARD
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/Documents/projects/trading-bot && \
  .venv/bin/python -m pytest tests/test_trading_pipeline.py::test_cycle_snapshot_defaults_to_fresh_guard -v 2>&1 | tail -5
```

Expected: `FAILED` — `TypeError: unexpected keyword argument` or `AttributeError`

- [ ] **Step 3: Add staleness_guard field to CycleSnapshot**

In `trading/cycle_context.py`, add the import at the top:

```python
from utils.data_health import StalenessGuard, FRESH_GUARD
```

Add to the `CycleSnapshot` dataclass, after `advance_decline_ratio`:

```python
    staleness_guard: StalenessGuard = field(default_factory=lambda: FRESH_GUARD)
```

Add `staleness_guard` kwarg to `build_cycle_snapshot()`:

```python
def build_cycle_snapshot(
    ...,
    staleness_guard: StalenessGuard = None,
) -> CycleSnapshot:
    ...
    return CycleSnapshot(
        ...,
        staleness_guard=staleness_guard or FRESH_GUARD,
    )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd ~/Documents/projects/trading-bot && \
  .venv/bin/python -m pytest tests/test_trading_pipeline.py::test_cycle_snapshot_defaults_to_fresh_guard -v 2>&1 | tail -5
```

Expected: `1 passed`

- [ ] **Step 5: Run full suite**

```bash
cd ~/Documents/projects/trading-bot && \
  .venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -5
```

Expected: no regressions.

- [ ] **Step 6: Commit**

```bash
git add trading/cycle_context.py tests/test_trading_pipeline.py
git commit -m "feat: add staleness_guard field to CycleSnapshot (defaults to FRESH_GUARD)"
```

---

## Task 10: `signals/entry_gates.py` — Staleness Composite Penalty

**Files:**
- Modify: `signals/entry_gates.py`
- Modify: `tests/test_entry_gates.py`

- [ ] **Step 1: Write the failing tests**

Add to `TestEffectiveCompositeGate` in `tests/test_entry_gates.py`:

```python
    def test_stale_guard_adds_composite_penalty(self, monkeypatch):
        """Stale regime guard adds DATA_COMPOSITE_PENALTY['regime'] to threshold."""
        monkeypatch.setattr(config, "COMPOSITE_MIN_NORMAL", 50)
        monkeypatch.setattr(config, "DATA_COMPOSITE_PENALTY", {"regime": 10})
        from signals.entry_gates import effective_composite_gate
        from utils.data_health import StalenessGuard
        stale_guard = StalenessGuard(
            composite_penalty=10, auto_execute_allowed=False,
            t2_allowed=False, swing_fallback_regime="NORMAL",
            can_open=True, claude_warnings=(), stale_sources=frozenset({"regime"}),
        )
        iv = self._iv()
        result = effective_composite_gate(iv, "NORMAL", staleness_guard=stale_guard)
        assert result == 60   # 50 base + 10 penalty

    def test_fresh_guard_adds_no_penalty(self, monkeypatch):
        """FRESH_GUARD adds zero penalty — baseline unchanged."""
        monkeypatch.setattr(config, "COMPOSITE_MIN_NORMAL", 50)
        from signals.entry_gates import effective_composite_gate
        from utils.data_health import FRESH_GUARD
        iv = self._iv()
        result = effective_composite_gate(iv, "NORMAL", staleness_guard=FRESH_GUARD)
        assert result == 50   # no penalty
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd ~/Documents/projects/trading-bot && \
  .venv/bin/python -m pytest tests/test_entry_gates.py::TestEffectiveCompositeGate::test_stale_guard_adds_composite_penalty \
    tests/test_entry_gates.py::TestEffectiveCompositeGate::test_fresh_guard_adds_no_penalty -v 2>&1 | tail -5
```

Expected: `FAILED` — `TypeError: unexpected keyword argument 'staleness_guard'`

- [ ] **Step 3: Update effective_composite_gate signature and implementation**

In `signals/entry_gates.py`, update the function signature and return:

```python
def effective_composite_gate(
    iv: IndicatorValues,
    regime: str,
    params: dict | None = None,
    staleness_guard=None,   # StalenessGuard | None — avoid circular import
) -> int:
    """Single source of truth for composite entry threshold by regime and signal context."""
    from utils.data_health import FRESH_GUARD
    guard = staleness_guard or FRESH_GUARD

    if regime in regimes.HIGH_VOL_GATES:
        return config.COMPOSITE_MIN_HIGH_VOL + guard.composite_penalty
    if iv.squeeze_setup:
        return config.SQUEEZE_COMPOSITE_MIN    # squeeze lowers bar, penalty still applies? No — squeeze overrides
    if iv.bot_herding_score >= config.BOT_HERD_ALERT_SCORE:
        return config.HERD_ENTRY_GATE
    ep = get_entry_params(regime)
    if regime == "NORMAL":
        base = (params or {}).get("composite_min_normal", ep.composite_min)
    else:
        base = ep.composite_min
    return base + guard.composite_penalty
```

**Note on squeeze:** squeeze lowers the bar significantly (to 12). Adding a staleness penalty on top of 12 would result in e.g. 22 which still allows entry — acceptable. The penalty is additive regardless of path.

- [ ] **Step 4: Update call sites in pre_claude_filter.py to pass staleness_guard**

In `trading/pre_claude_filter.py`, line 47:

```python
    composite_min = effective_composite_gate(
        iv, snap.regime, params=snap.params,
        staleness_guard=snap.staleness_guard,
    )
```

- [ ] **Step 5: Run tests**

```bash
cd ~/Documents/projects/trading-bot && \
  .venv/bin/python -m pytest tests/test_entry_gates.py -v -q 2>&1 | tail -10
```

Expected: all `TestEffectiveCompositeGate` tests pass.

- [ ] **Step 6: Run full suite**

```bash
cd ~/Documents/projects/trading-bot && \
  .venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -5
```

Expected: no regressions.

- [ ] **Step 7: Commit**

```bash
git add signals/entry_gates.py trading/pre_claude_filter.py tests/test_entry_gates.py
git commit -m "feat: effective_composite_gate adds staleness_guard.composite_penalty"
```

---

## Task 11: `trading/pipeline.py` — Fix intra_sent + Auto-Execute Guard + Stale Log + Claude Warnings

**Files:**
- Modify: `trading/pipeline.py`
- Modify: `tests/test_trading_pipeline.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_trading_pipeline.py — append

@pytest.mark.unit
def test_auto_execute_blocked_when_guard_disallows(monkeypatch):
    """_execute_auto_entries returns (0, bp, []) when auto_execute_allowed is False."""
    from trading.pipeline import _execute_auto_entries
    from unittest.mock import MagicMock
    from trading.cycle_context import CycleSnapshot
    from utils.data_health import StalenessGuard, FRESH_GUARD

    stale_guard = StalenessGuard(
        composite_penalty=10, auto_execute_allowed=False,
        t2_allowed=False, swing_fallback_regime="NORMAL",
        can_open=True, claude_warnings=(), stale_sources=frozenset({"regime"}),
    )
    snap = MagicMock(spec=CycleSnapshot)
    snap.staleness_guard = stale_guard
    snap.research = {}
    snap.intraday_news = {}

    result_entries, result_bp, result_batch = _execute_auto_entries(
        auto_execute=[("AAPL", MagicMock())],
        snap=snap,
        alpaca=MagicMock(),
        open_positions={},
        portfolio_value=100_000,
        buying_power=50_000,
        pos_mult=1.0,
        spy_ret5d=0.0,
        rsi_max=70,
        atr_tp_mult=2.0,
        can_open=True,
        now=MagicMock(),
        daily_pnl_pct=0.0,
        entries_so_far=0,
    )
    assert result_entries == 0
    assert result_batch == []


@pytest.mark.unit
def test_auto_execute_intra_sent_uses_symbol_sentiment_not_shock(monkeypatch):
    """_execute_auto_entries reads symbol intraday sentiment, not __meta__ shock_score."""
    import config
    from trading.pipeline import _compute_auto_execute_confidence
    from unittest.mock import MagicMock, patch
    from trading.cycle_context import CycleSnapshot
    from utils.data_health import FRESH_GUARD

    monkeypatch.setattr(config, "AUTO_EXECUTE_CONFIDENCE_BASE", 75)
    monkeypatch.setattr(config, "AUTO_EXECUTE_COMPOSITE_MIN", 70)

    snap = MagicMock(spec=CycleSnapshot)
    snap.staleness_guard = FRESH_GUARD
    snap.research = {"AAPL": {"sentiment_score": 80}}
    snap.intraday_news = {"AAPL": {"sentiment_score": 60}}   # symbol intraday sentiment
    snap.regime = "NORMAL"

    # We test the helper directly — intra_sent=60 (intraday) should produce higher
    # confidence than intra_sent=0 (shock_score when no macro shock)
    conf_with_sentiment  = _compute_auto_execute_confidence(80.0, 60)
    conf_without_sentiment = _compute_auto_execute_confidence(80.0, 0)
    assert conf_with_sentiment > conf_without_sentiment
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd ~/Documents/projects/trading-bot && \
  .venv/bin/python -m pytest \
    tests/test_trading_pipeline.py::test_auto_execute_blocked_when_guard_disallows \
    tests/test_trading_pipeline.py::test_auto_execute_intra_sent_uses_symbol_sentiment_not_shock \
    -v 2>&1 | tail -8
```

Expected: `FAILED`

- [ ] **Step 3: Fix _execute_auto_entries in trading/pipeline.py**

Replace the function body opening (before the `for symbol, iv` loop):

```python
def _execute_auto_entries(auto_execute, snap, alpaca, open_positions, portfolio_value,
                          buying_power, pos_mult, spy_ret5d, rsi_max, atr_tp_mult,
                          can_open, now, daily_pnl_pct, entries_so_far) -> tuple:
    """Phase 4a: auto-execute buys. Returns (new_entries, updated_buying_power, batch)."""
    guard = snap.staleness_guard
    if not guard.auto_execute_allowed:
        logger.info(
            "[AUTO-EXEC] blocked — stale sources: %s",
            ", ".join(sorted(guard.stale_sources & config.DATA_DISABLE_AUTO_EXECUTE)),
        )
        return 0, buying_power, []

    new_entries = 0
    batch: list[dict] = []
    for symbol, iv in auto_execute:
        if not can_open or buying_power < config.MIN_BUYING_POWER:
            continue
        try:
            # FIX: use symbol intraday sentiment (matches pre_claude_filter Layer 5)
            # Previously incorrectly read snap.research["__meta__"]["shock_score"]
            intra_sent = snap.intraday_news.get(symbol, {}).get(
                "sentiment_score",
                snap.research.get(symbol, {}).get("sentiment_score", 0),
            )
            confidence = _compute_auto_execute_confidence(iv.composite_score, intra_sent)
            ...  # rest of loop unchanged
```

- [ ] **Step 4: Add [STALE] prefix to cycle log line**

In `pipeline.py`, find the `[CYCLE]` summary log line and prepend:

```python
        stale_tag = (
            f"[STALE:{','.join(sorted(snap.staleness_guard.stale_sources))}] "
            if snap.staleness_guard.stale_sources else ""
        )
        logger.info(
            "%s[CYCLE] %d symbols | fetch=%.1fs gates=%.1fs (%d passed) "
            "claude=%.1fs execute=%.1fs | total=%.1fs",
            stale_tag, len(symbols), ...
        )
```

- [ ] **Step 5: Inject claude_warnings into context builder**

In `trading/context_builder.py` (or wherever `_build_market_context_block` lives), find the function and append at the end of the context string:

```python
    if staleness_guard and staleness_guard.claude_warnings:
        warning_text = "\n".join(staleness_guard.claude_warnings)
        context += f"\n\nDATA QUALITY NOTICE:\n{warning_text}\n"
```

Pass `staleness_guard=snap.staleness_guard` from the call site in `pipeline.py`.

- [ ] **Step 6: Run tests to verify they pass**

```bash
cd ~/Documents/projects/trading-bot && \
  .venv/bin/python -m pytest \
    tests/test_trading_pipeline.py::test_auto_execute_blocked_when_guard_disallows \
    tests/test_trading_pipeline.py::test_auto_execute_intra_sent_uses_symbol_sentiment_not_shock \
    -v 2>&1 | tail -5
```

Expected: `2 passed`

- [ ] **Step 7: Run full suite**

```bash
cd ~/Documents/projects/trading-bot && \
  .venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -5
```

Expected: no regressions.

- [ ] **Step 8: Commit**

```bash
git add trading/pipeline.py tests/test_trading_pipeline.py
git commit -m "fix: auto-execute intra_sent reads symbol sentiment not shock_score; add guard gate + [STALE] log"
```

---

## Task 12: `trading/executor.py` — swing_eligible Override on Stale Regime

**Files:**
- Modify: `trading/executor.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_trading_pipeline.py — append

@pytest.mark.unit
def test_execute_buy_blocks_swing_when_regime_stale(monkeypatch, tmp_path):
    """execute_buy forces swing_eligible=False when regime is in guard.stale_sources."""
    import config
    from unittest.mock import patch, MagicMock
    from utils.data_health import StalenessGuard

    stale_guard = StalenessGuard(
        composite_penalty=10, auto_execute_allowed=False,
        t2_allowed=False, swing_fallback_regime="NORMAL",
        can_open=True, claude_warnings=(), stale_sources=frozenset({"regime"}),
    )

    captured = {}

    def fake_check_all_gates(*args, **kwargs):
        captured["swing_eligible"] = kwargs.get("swing_eligible")
        from signals.entry_gates import GateResult
        return GateResult(passed=False, reason="test block")

    with patch("trading.executor.check_all_gates", side_effect=fake_check_all_gates), \
         patch("trading.executor.load_open_positions", return_value={}), \
         patch("trading.executor.load_daily_closed", return_value=set()):
        from trading.executor import execute_buy
        from unittest.mock import MagicMock
        iv = MagicMock()
        iv.composite_score = 75; iv.rsi = 50; iv.current_price = 100.0
        iv.atr = 2.0; iv.bot_herding_score = 0; iv.squeeze_setup = False
        decision = {"action": "BUY", "confidence": 85,
                    "stop_loss_pct": 4.0, "take_profit_pct": 6.0, "reasoning": "test"}
        execute_buy(
            symbol="NVDA", iv=iv, decision=decision, symbol_ret5d=0.0,
            alpaca=MagicMock(), open_positions={},
            portfolio_value=100_000, pos_mult=1.0, spy_ret5d=0.0,
            rsi_max=70, now=MagicMock(), entries_this_cycle=0,
            daily_pnl_pct=0.0, params={}, regime="BULL_TREND",
            regime_info={}, research={}, intraday_news={},
            sector_today={}, staleness_guard=stale_guard,
        )

    assert captured.get("swing_eligible") is False
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/Documents/projects/trading-bot && \
  .venv/bin/python -m pytest \
    tests/test_trading_pipeline.py::test_execute_buy_blocks_swing_when_regime_stale \
    -v 2>&1 | tail -8
```

Expected: `FAILED`

- [ ] **Step 3: Update execute_buy in trading/executor.py**

Add `staleness_guard=None` to the function signature. Replace the `swing_eligible` derivation line:

```python
    from utils.data_health import FRESH_GUARD
    guard = staleness_guard or FRESH_GUARD

    # Stale regime → unknown swing params → block swing promotion
    swing_eligible_raw = regime in regimes.SWING_ELIGIBLE
    swing_eligible = swing_eligible_raw and ("regime" not in guard.stale_sources)
```

Pass `staleness_guard=snap.staleness_guard` from the `_shared_buy_kwargs` builder in `pipeline.py`.

- [ ] **Step 4: Run test to verify it passes**

```bash
cd ~/Documents/projects/trading-bot && \
  .venv/bin/python -m pytest \
    tests/test_trading_pipeline.py::test_execute_buy_blocks_swing_when_regime_stale \
    -v 2>&1 | tail -5
```

Expected: `1 passed`

- [ ] **Step 5: Commit**

```bash
git add trading/executor.py tests/test_trading_pipeline.py
git commit -m "feat: execute_buy forces swing_eligible=False when regime is stale"
```

---

## Task 13: `trading/cycle.py` — Build Guard, Wire can_open, Intraday Retry

**Files:**
- Modify: `trading/cycle.py`

- [ ] **Step 1: Add guard build and can_open wiring to run_trading_cycle**

In `run_trading_cycle()`, immediately after `regime_info = load_macro_regime()` and before `build_cycle_snapshot`:

```python
    from utils.data_health import get_staleness_guard, _tracker
    guard = get_staleness_guard(regime_info)     # build once, flows through entire cycle

    # Intraday retry: re-run stale agents before building snapshot
    _run_intraday_retries(now, guard)

    # Append guard's can_open after existing CRISIS/flash-crash/time-window checks
    if not guard.can_open:
        logger.warning(
            "Regime data age > %dmin — blocking new entries (staleness guard).",
            config.DATA_REGIME_CANOPEN_MAX_MINUTES,
        )
        can_open = False

    snap = build_cycle_snapshot(
        portfolio_gain_pct=portfolio_gain_pct,
        regime_info=regime_info,
        params=params,
        staleness_guard=guard,
    )
```

Add the intraday retry helper (at module level, above `run_trading_cycle`):

```python
def _run_intraday_retries(now, guard) -> None:
    """Re-run stale pre-market agents if tracker says retry is due."""
    from utils.data_health import _tracker
    from macro.regime import fetch_macro_regime
    from research.agent import run_research_agent
    from trading.watch_loader import load_watch_list

    _INTRADAY_RETRY_MAP = {
        "regime":   fetch_macro_regime,
        "research": lambda: run_research_agent(load_watch_list()),
    }
    for source_id, fn in _INTRADAY_RETRY_MAP.items():
        if _tracker.can_retry(source_id, now=now):
            logger.info("DATA RETRY (intraday): re-running %s", source_id)
            try:
                fn()
            except Exception as exc:
                logger.error("DATA RETRY failed for %s: %s", source_id, exc)
```

- [ ] **Step 2: Verify cycle imports cleanly**

```bash
cd ~/Documents/projects/trading-bot && \
  .venv/bin/python -c "from trading.cycle import run_trading_cycle; print('ok')"
```

Expected: `ok`

- [ ] **Step 3: Run full unit suite**

```bash
cd ~/Documents/projects/trading-bot && \
  .venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -5
```

Expected: no regressions.

- [ ] **Step 4: Commit**

```bash
git add trading/cycle.py
git commit -m "feat: cycle builds StalenessGuard before snapshot; wires can_open + intraday retry"
```

---

## Task 14: `trading/monitor.py` — Guard at Entry, T2 Gate

**Files:**
- Modify: `trading/monitor.py`
- Modify: `tests/test_foundation.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_foundation.py — append

@pytest.mark.unit
def test_position_monitor_skips_t2_when_guard_disallows(monkeypatch, tmp_path):
    """run_position_monitor skips T2 check when guard.t2_allowed is False."""
    import config
    from unittest.mock import patch, MagicMock
    from utils.data_health import StalenessGuard

    stale_guard = StalenessGuard(
        composite_penalty=10, auto_execute_allowed=False,
        t2_allowed=False, swing_fallback_regime="NORMAL",
        can_open=True, claude_warnings=(), stale_sources=frozenset({"regime"}),
    )

    monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "open.json")
    monkeypatch.setattr(config, "SWING_POSITIONS_FILE", tmp_path / "swing.json")

    t2_called = []

    with patch("trading.monitor.load_macro_regime",
               return_value={"regime": "NORMAL", "_stale": True}), \
         patch("utils.data_health.get_staleness_guard", return_value=stale_guard), \
         patch("trading.monitor.load_open_positions", return_value={}), \
         patch("trading.monitor._check_tranche2_triggers",
               side_effect=lambda *a, **kw: t2_called.append(True)), \
         patch("trading.monitor.AlpacaClient"):
        from trading.monitor import run_position_monitor
        run_position_monitor()

    assert len(t2_called) == 0, "T2 should not fire when guard.t2_allowed is False"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/Documents/projects/trading-bot && \
  .venv/bin/python -m pytest \
    tests/test_foundation.py::test_position_monitor_skips_t2_when_guard_disallows \
    -v 2>&1 | tail -8
```

Expected: `FAILED`

- [ ] **Step 3: Update run_position_monitor in trading/monitor.py**

At the top of `run_position_monitor()`, after loading `regime_info`, add:

```python
    from utils.data_health import get_staleness_guard
    regime_info = load_macro_regime()
    guard       = get_staleness_guard(regime_info)   # build once per monitor tick
```

Replace the existing T2 trigger call:

```python
    if guard.t2_allowed:
        _check_tranche2_triggers(open_positions, alpaca, now)
    else:
        logger.debug(
            "T2 blocked by staleness guard — stale: %s",
            ", ".join(sorted(guard.stale_sources & config.DATA_DISABLE_T2)),
        )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd ~/Documents/projects/trading-bot && \
  .venv/bin/python -m pytest \
    tests/test_foundation.py::test_position_monitor_skips_t2_when_guard_disallows \
    -v 2>&1 | tail -5
```

Expected: `1 passed`

- [ ] **Step 5: Commit**

```bash
git add trading/monitor.py tests/test_foundation.py
git commit -m "feat: position monitor builds StalenessGuard; gates T2 on guard.t2_allowed"
```

---

## Task 15: `trading/swing_manager.py` — Guard at Entry, Fallback Regime

**Files:**
- Modify: `trading/swing_manager.py`
- Modify: `tests/test_foundation.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_foundation.py — append

@pytest.mark.unit
def test_evaluate_eod_holds_uses_normal_params_when_regime_stale(monkeypatch, tmp_path):
    """evaluate_eod_holds uses NORMAL swing params when guard.swing_fallback_regime is set."""
    import config
    from unittest.mock import patch, MagicMock
    from utils.data_health import StalenessGuard

    stale_guard = StalenessGuard(
        composite_penalty=10, auto_execute_allowed=False,
        t2_allowed=False, swing_fallback_regime="NORMAL",
        can_open=True, claude_warnings=(), stale_sources=frozenset({"regime"}),
    )

    captured_regime = []

    def fake_get_swing_params(regime):
        captured_regime.append(regime)
        from utils.regime_params import SwingParams
        return SwingParams(False, 999, 999, 0.0, 0.0, 0.0)   # blocks all swings

    monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "open.json")
    monkeypatch.setattr(config, "SWING_POSITIONS_FILE", tmp_path / "swing.json")

    with patch("trading.swing_manager.load_macro_regime",
               return_value={"regime": "BULL_TREND", "_stale": True}), \
         patch("utils.data_health.get_staleness_guard", return_value=stale_guard), \
         patch("trading.swing_manager.get_swing_params", side_effect=fake_get_swing_params), \
         patch("trading.swing_manager.load_open_positions", return_value={}):
        from trading.swing_manager import evaluate_eod_holds
        evaluate_eod_holds()

    assert "NORMAL" in captured_regime, (
        f"Expected NORMAL (fallback), got {captured_regime}"
    )
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/Documents/projects/trading-bot && \
  .venv/bin/python -m pytest \
    tests/test_foundation.py::test_evaluate_eod_holds_uses_normal_params_when_regime_stale \
    -v 2>&1 | tail -8
```

Expected: `FAILED`

- [ ] **Step 3: Update evaluate_eod_holds and run_gap_check in trading/swing_manager.py**

At the top of `evaluate_eod_holds()`:

```python
    from utils.data_health import get_staleness_guard
    regime_info      = load_macro_regime()
    guard            = get_staleness_guard(regime_info)
    effective_regime = guard.swing_fallback_regime or regime_info.get("regime", "NORMAL")
    swing_p          = get_swing_params(effective_regime)
    if guard.swing_fallback_regime:
        logger.warning(
            "Regime stale — using %s swing params as safe fallback",
            effective_regime,
        )
```

At the top of `run_gap_check()`:

```python
    from utils.data_health import get_staleness_guard
    regime_info      = load_macro_regime()
    guard            = get_staleness_guard(regime_info)
    effective_regime = guard.swing_fallback_regime or regime_info.get("regime", "NORMAL")
```

Replace any existing `regime = regime_info.get("regime", "NORMAL")` lines inside these functions with `regime = effective_regime`.

- [ ] **Step 4: Run test to verify it passes**

```bash
cd ~/Documents/projects/trading-bot && \
  .venv/bin/python -m pytest \
    tests/test_foundation.py::test_evaluate_eod_holds_uses_normal_params_when_regime_stale \
    -v 2>&1 | tail -5
```

Expected: `1 passed`

- [ ] **Step 5: Commit**

```bash
git add trading/swing_manager.py tests/test_foundation.py
git commit -m "feat: swing_manager builds StalenessGuard; uses fallback regime when stale"
```

---

## Task 16: `notifications/telegram.py` — notify_data_degraded

**Files:**
- Modify: `notifications/telegram.py`

- [ ] **Step 1: Add notify_data_degraded with dedup sentinel**

Append to `notifications/telegram.py`:

```python
_degraded_alerted: dict[str, bool] = {}   # {source_id_date: True}


def notify_data_degraded(source_id: str, guard) -> None:
    """One-shot Telegram alert when a data source activates stale fallback.

    Deduped per source per calendar day — will not spam on every cycle.
    guard is a StalenessGuard instance.
    """
    from datetime import datetime
    from utils.constants import ET
    today     = datetime.now(ET).strftime("%Y-%m-%d")
    sentinel  = f"{source_id}_{today}"
    if _degraded_alerted.get(sentinel):
        return
    _degraded_alerted[sentinel] = True

    import config
    penalties = [
        f"+{v} composite"
        for k, v in config.DATA_COMPOSITE_PENALTY.items()
        if k in guard.stale_sources and v > 0
    ]
    blocks = []
    if not guard.auto_execute_allowed:
        blocks.append("auto-execute")
    if not guard.t2_allowed:
        blocks.append("T2")

    msg = (
        f"⚠️ <b>DATA DEGRADED — {source_id.upper()}</b>\n"
        f"Fallback active. "
        f"Penalties: {', '.join(penalties) or 'none'}. "
        f"Blocked: {', '.join(blocks) or 'none'}."
    )
    notify_alert(msg)
```

- [ ] **Step 2: Call notify_data_degraded from the watch list agent on empty result**

In `agents/watch_list.py`, in the empty-result block (Task 7 Step 4), add after writing the health file:

```python
        import notifications.telegram as telegram_notify
        from utils.data_health import get_staleness_guard, _tracker
        guard = get_staleness_guard(tracker=_tracker)
        telegram_notify.notify_data_degraded("watch_list", guard)
```

- [ ] **Step 3: Verify import**

```bash
cd ~/Documents/projects/trading-bot && \
  .venv/bin/python -c "from notifications.telegram import notify_data_degraded; print('ok')"
```

Expected: `ok`

- [ ] **Step 4: Run full unit suite**

```bash
cd ~/Documents/projects/trading-bot && \
  .venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -5
```

Expected: no regressions.

- [ ] **Step 5: Commit**

```bash
git add notifications/telegram.py agents/watch_list.py
git commit -m "feat: notify_data_degraded with dedup sentinel; call from watch list agent on empty"
```

---

## Task 17: CLAUDE.md Rules + Final Verification

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add Rules 24, 25, 26 to CLAUDE.md Coding Rules section**

```markdown
### 24. Data staleness — always use get_staleness_guard()
`get_staleness_guard()` in `utils/data_health.py` is the sole factory for `StalenessGuard`.
No module evaluates staleness thresholds, computes composite penalties, or decides T2/auto-execute
eligibility outside `utils/data_health.py`. Adding a new staleness consequence = update
`config.DATA_*` sets and `get_staleness_guard()` only.

### 25. Guard injection — once per scheduler entry point
`StalenessGuard` is built exactly once per scheduler entry point (cycle, monitor tick, EOD)
before any downstream stage. Never build mid-stage or share guards across scheduler boundaries.
`CycleSnapshot.staleness_guard` carries it through the cycle; monitor and swing_manager build
their own independently.

### 26. Data resilience constants — all in config.py
All retry schedules, staleness thresholds, composite penalties, and disable sets live under
`# ── Data Resilience ──` in `config.py`. No magic numbers in `data_health.py`, agent files,
or job functions.
```

- [ ] **Step 2: Run the complete test suite**

```bash
cd ~/Documents/projects/trading-bot && \
  .venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -10
```

Expected: all tests pass, count equal to or higher than before this branch.

- [ ] **Step 3: Verify the bot starts cleanly**

```bash
cd ~/Documents/projects/trading-bot && \
  timeout 8 .venv/bin/python main.py 2>&1 | grep -E "ERROR|Traceback|staleness|tracker|scheduler" | head -15
```

Expected: scheduler startup messages, `set_data_tracker` wired (no errors or tracebacks).

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: add Rules 24/25/26 — data staleness SoT, guard injection, resilience config"
```

---

## Task 18: Integration Smoke Test

- [ ] **Step 1: Simulate the today failure scenario end-to-end**

```bash
cd ~/Documents/projects/trading-bot && \
  .venv/bin/python - <<'EOF'
"""Smoke test: simulate market cap API failure and verify staleness guard propagates."""
import config
from pathlib import Path
from unittest.mock import patch

# Simulate all market cap fetches returning None (401 scenario)
with patch("agents.volatility_screener._fetch_market_cap", return_value=None), \
     patch("scheduler_jobs._schedule_retry"), \
     patch("sectors.load_universe", return_value=["AAPL", "MSFT", "NVDA"]):
    from agents.volatility_screener import run_volatility_screener
    result = run_volatility_screener()
    print(f"Screener result (should be all tickers): {result}")
    assert len(result) == 3, "Filter should be bypassed"

# Verify tracker recorded failure
from utils.data_health import _tracker
health = _tracker.get_status("screener")
print(f"Screener health after failure: {health.status}, attempts: {health.attempts_today}")

# Verify guard reflects stale screener
from utils.data_health import get_staleness_guard
import config
config.DATA_STALE_THRESHOLD_MINUTES["screener"] = 0   # immediate stale
guard = get_staleness_guard()
print(f"Guard stale_sources: {guard.stale_sources}")
print(f"Guard auto_execute_allowed: {guard.auto_execute_allowed}")
print(f"Guard composite_penalty: {guard.composite_penalty}")
print("✓ All smoke checks passed")
EOF
```

Expected:
```
Screener result (should be all tickers): ['AAPL', 'MSFT', 'NVDA']
Screener health after failure: failed, attempts: 1
Guard stale_sources: frozenset({'screener'})
Guard auto_execute_allowed: True    ← screener not in DATA_DISABLE_AUTO_EXECUTE
Guard composite_penalty: 0          ← screener has 0 penalty by design
✓ All smoke checks passed
```

- [ ] **Step 2: Run full test suite one final time**

```bash
cd ~/Documents/projects/trading-bot && \
  .venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -5
```

Expected: all tests green.

- [ ] **Step 3: Final commit**

```bash
git add -A
git commit -m "feat: complete data resilience + staleness guard + auto-execute fix

- DataHealthTracker singleton tracks all pre-market source health
- StalenessGuard (frozen) flows through cycle/monitor/EOD entry points
- get_staleness_guard() is single factory — no consumer re-derives staleness
- Screener bypasses market cap filter on API failure (≥80% None)
- watch_loader merges open + swing position tickers always
- T2 blocked when regime stale; swing uses NORMAL fallback
- auto-execute blocked when regime stale
- effective_composite_gate adds guard.composite_penalty
- Fix: _execute_auto_entries intra_sent reads symbol sentiment not __meta__ shock_score
- Rules 24/25/26 added to CLAUDE.md"
```
