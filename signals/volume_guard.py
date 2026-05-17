"""
Relative-volume soft gate. Flags low-volume breakouts that have a high
false-positive rate (dead-cat bounces, stop hunts on thin tape).

Pure function — no I/O, no Alpaca calls.
"""
from __future__ import annotations

import config


def is_volume_weak(relative_volume: float) -> bool:
    """Return True if current volume is below REL_VOLUME_MIN of 20-day average.

    relative_volume = today's estimated full-day vol / 20d avg vol.
    """
    return relative_volume < config.REL_VOLUME_MIN
