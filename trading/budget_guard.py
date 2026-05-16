"""
Exit-to-entry budget rollover at 12:30 PM ET.
Pure functions — all state is passed in; no disk I/O.
Imported by trading.risk_manager.
"""
from __future__ import annotations

from datetime import datetime

import config
from utils.constants import ET


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
