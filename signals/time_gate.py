from __future__ import annotations
import config
from datetime import datetime

from utils.constants import ET
from utils.regime_params import get_entry_params


def is_entry_allowed(now: datetime, regime: str = "NORMAL") -> bool:
    """Returns False during first ENTRY_GATE_OPEN_MINUTES after open, last ENTRY_GATE_CLOSE_MINUTES before close,
    or at/after the per-regime hard cutoff hour."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    else:
        now = now.astimezone(ET)

    minutes = now.hour * 60 + now.minute  # minutes since midnight ET
    gate_open_end = 9 * 60 + 30 + config.ENTRY_GATE_OPEN_MINUTES    # default: 10:00
    gate_close_start = 16 * 60 - config.ENTRY_GATE_CLOSE_MINUTES     # default: 15:25
    hard_close_mins = get_entry_params(regime).hard_close_hour * 60

    return gate_open_end <= minutes < min(gate_close_start, hard_close_mins)
