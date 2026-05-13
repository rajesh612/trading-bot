"""
Time-of-day budget caps and exit-to-entry rollover.
Pure functions — all state is passed in; no disk I/O.
Imported by trading.risk_manager.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import config

ET = ZoneInfo("America/New_York")


def morning_cap_exceeded(entry_spent: float, entry_ceiling: float, now: datetime) -> bool:
    """
    True if morning spend cap is reached.
    Cap applies 9:30–11:30 AM ET only.
    """
    et = now.astimezone(ET)
    h, m = et.hour, et.minute
    in_window = (h == 9 and m >= 30) or h == 10 or (h == 11 and m < 30)
    if not in_window:
        return False
    return entry_spent >= entry_ceiling * config.BUDGET_MORNING_CAP_PCT


def midday_cap_exceeded(entry_spent: float, entry_ceiling: float, now: datetime) -> bool:
    """
    True if midday spend cap is reached.
    Cap applies 11:30 AM–1:00 PM ET only.
    """
    et = now.astimezone(ET)
    h, m = et.hour, et.minute
    in_window = (h == 11 and m >= 30) or h == 12 or (h == 13 and m == 0)
    if not in_window:
        return False
    return entry_spent >= entry_ceiling * config.BUDGET_MIDDAY_CAP_PCT


def apply_exit_rollover(b: dict, now: datetime) -> bool:
    """
    Roll a fraction of unused exit budget into entry pool at 12:30 PM ET.
    Modifies b in-place. Returns True if rollover was applied.
    Idempotent: second call on same budget dict is a no-op.
    """
    et = now.astimezone(ET)
    if et.hour != config.BUDGET_ROLLOVER_HOUR or et.minute != config.BUDGET_ROLLOVER_MINUTE:
        return False
    if b.get("_rollover_applied"):
        return False
    exit_ceil  = b.get("exit_ceiling_usd", 0.0)
    exit_spent = b.get("exit_spent_usd",   0.0)
    unused = exit_ceil - exit_spent
    if unused <= 0:
        return False
    rollover = min(unused * config.BUDGET_ROLLOVER_FRACTION, config.EXIT_ROLLOVER_MAX_USD)
    b["entry_ceiling_usd"] = round(b.get("entry_ceiling_usd", 0.0) + rollover, 4)
    b["_rollover_applied"] = True
    return True
