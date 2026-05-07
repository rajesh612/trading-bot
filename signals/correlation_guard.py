"""
Correlation guard: prevents opening multiple positions in the same sector.
Sectors are mapped from tier1_config or an extended fallback dictionary.
"""
from __future__ import annotations
import config
from tier1_config import TIER1_NAMES

# Extended sector map for non-tier-1 tickers
_EXTENDED_SECTOR: dict[str, str] = {
    # Semis
    "AMD": "SEMICONDUCTOR", "INTC": "SEMICONDUCTOR", "QCOM": "SEMICONDUCTOR",
    "MRVL": "SEMICONDUCTOR", "ON": "SEMICONDUCTOR", "SWKS": "SEMICONDUCTOR",
    # Cloud/Hyperscaler
    "SNOW": "CLOUD", "DDOG": "CLOUD", "HUBS": "CLOUD", "MDB": "CLOUD",
    "CFLT": "CLOUD", "NET": "CLOUD", "ZS": "CLOUD",
    # Consumer Tech
    "AAPL": "CONSUMER_TECH", "SPOT": "CONSUMER_TECH", "PINS": "CONSUMER_TECH",
    "RDDT": "CONSUMER_TECH", "TTWO": "CONSUMER_TECH",
    # Fintech
    "PYPL": "FINTECH", "SQ": "FINTECH", "AFRM": "FINTECH", "SOFI": "FINTECH",
    # EV
    "RIVN": "EV", "LCID": "EV", "NIO": "EV",
    # Biotech
    "ISRG": "BIOTECH", "DXCM": "BIOTECH", "IDXX": "BIOTECH",
    # Industrial/Other
    "URI": "INDUSTRIAL", "DE": "INDUSTRIAL",
    "TTD": "ADTECH",
    "RBLX": "GAMING",
}


def sector_of(symbol: str) -> str:
    """Returns GICS sector string from tier1_config or extended fallback. Returns 'OTHER' if unknown."""
    # Check tier1 config first
    entry = TIER1_NAMES.get(symbol.upper())
    if entry:
        sector = entry.get("sector", "")
        return sector if sector else "OTHER"
    return _EXTENDED_SECTOR.get(symbol.upper(), "OTHER")


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
