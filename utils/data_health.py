"""Single source of truth for data-source health tracking and staleness decisions.

DataHealthTracker (module-level singleton _tracker) records success/failure per
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

_MARKET_OPEN_HOUR = 9
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
            entry["next_retry_at"] = None

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
    regime_age = t.age_minutes("regime", now=now)
    open_ok  = regime_age == float("inf") or regime_age < config.DATA_REGIME_CANOPEN_MAX_MINUTES
    swing_fb = config.DATA_SWING_STALE_FALLBACK_REGIME if "regime" in stale else None
    warnings = tuple(
        f"WARNING STALE DATA — {s}: signals have lower conviction"
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
