"""
Time-aware + tier-adjusted TP/SL scaler — Rule 30 SoT for all TP/SL computation.
Pure — no I/O.
"""
from __future__ import annotations
from datetime import datetime
from enum import Enum

import config

_SAFE_REGIMES = frozenset({"NORMAL", "BULL_TREND", "OPPORTUNITY", "CRASH_RECOVERY"})

_TIER_TP_MULT = {"LOW": 1.00, "MEDIUM": 1.00, "HIGH": 1.10, "CONVICTION": 1.20}
_TIER_SL_MULT = {"LOW": 1.00, "MEDIUM": 1.00, "HIGH": 0.95, "CONVICTION": 0.90}


class TimeSlot(Enum):
    FULL       = "FULL"
    LATE       = "LATE"
    TIGHT      = "TIGHT"
    VERY_TIGHT = "VERY_TIGHT"


def compute_time_multiplier(entry_time: datetime) -> float:
    """Return a TP multiplier based on time of day (ET). Later entries get tighter targets."""
    hour = entry_time.hour
    minute = entry_time.minute
    total_min = hour * 60 + minute
    if total_min < 12 * 60:
        return 1.00
    if total_min < 13 * 60 + 30:
        return 0.80
    if total_min < 14 * 60 + 30:
        return 0.65
    return 0.50


def compute_tp_sl(
    base_tp: float,
    base_sl: float,
    entry_time: datetime,
    tier: str,
    regime: str,
) -> tuple[float, float]:
    """
    Compute final TP and SL percentages adjusted for time of day and ticker tier.

    Tier multipliers are only applied in safe (non-defensive) regimes.
    TP is clamped to [3.0, 12.0].
    SL is clamped to [2.0, 5.0].
    """
    time_mult = compute_time_multiplier(entry_time)
    tier_tp = _TIER_TP_MULT.get(tier, 1.0) if regime in _SAFE_REGIMES else 1.0
    tier_sl = _TIER_SL_MULT.get(tier, 1.0) if regime in _SAFE_REGIMES else 1.0

    tp = base_tp * time_mult * tier_tp
    sl = base_sl * tier_sl

    tp = max(3.0, min(12.0, tp))
    sl = max(2.0, min(5.0, sl))
    return round(tp, 2), round(sl, 2)
