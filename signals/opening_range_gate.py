"""
Opening-range context gate.
The opening range is defined as the high/low of the first OPENING_RANGE_MINUTES
of the trading session (9:30-10:30 ET by default).

Entries inside the OR on the first attempt carry high reversal risk — flagged for Claude.
Entries above OR = breakout (preferred). Below OR = potential breakdown.

Pure function — no I/O.
"""
from __future__ import annotations


def get_or_position(current_price: float, or_high: float, or_low: float) -> str:
    """Return 'inside', 'outside', or 'unknown'."""
    if or_high <= 0 or or_low <= 0:
        return "unknown"
    if current_price > or_high or current_price < or_low:
        return "outside"
    return "inside"


def is_inside_opening_range(current_price: float, or_high: float, or_low: float) -> bool:
    return get_or_position(current_price, or_high, or_low) == "inside"
