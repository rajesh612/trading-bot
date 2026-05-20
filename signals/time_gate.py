from __future__ import annotations
import config
from datetime import datetime

from utils.constants import ET


def is_entry_allowed(now: datetime) -> bool:
    """Returns False during first ENTRY_GATE_OPEN_MINUTES after open and last ENTRY_GATE_CLOSE_MINUTES before close."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    else:
        now = now.astimezone(ET)

    minutes = now.hour * 60 + now.minute  # minutes since midnight ET
    gate_open_end = 9 * 60 + 30 + config.ENTRY_GATE_OPEN_MINUTES    # default: 10:00
    gate_close_start = 16 * 60 - config.ENTRY_GATE_CLOSE_MINUTES     # default: 15:25

    return gate_open_end <= minutes < gate_close_start
