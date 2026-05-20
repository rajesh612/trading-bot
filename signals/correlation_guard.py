"""
Correlation guard: prevents opening multiple positions in the same sector ETF group.
Sector assignment is O(1) via sectors.py pre-built dict.
"""
from __future__ import annotations
import config
import sectors


def sector_of(symbol: str) -> str:
    """Returns the sector ETF key for a symbol, or 'OTHER' if unknown."""
    return sectors.get_sector_etf(symbol)


def passes_correlation_guard(symbol: str, open_positions: dict) -> bool:
    """Returns False if MAX_SAME_SECTOR_POSITIONS or more open positions share this symbol's sector."""
    my_sector = sector_of(symbol)
    if my_sector == "OTHER":
        return True  # never block unknown sectors

    same_sector_count = sum(
        1 for sym in open_positions
        if sector_of(sym) == my_sector
    )
    return same_sector_count < config.MAX_SAME_SECTOR_POSITIONS
