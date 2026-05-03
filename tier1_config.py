"""
Tier-1 names: high-quality liquid stocks eligible for swing / PEAD positions.
These names get wider stops, larger TP targets, and can be held overnight.
"""

TIER1_NAMES: dict[str, dict] = {
    # AI & Semiconductors
    "NVDA": {"sector": "AI_SEMI",      "theme": "AI_GPU"},
    "AVGO": {"sector": "AI_SEMI",      "theme": "AI_NETWORKING"},
    "AMD":  {"sector": "AI_SEMI",      "theme": "AI_GPU"},
    "MRVL": {"sector": "AI_SEMI",      "theme": "AI_NETWORKING"},
    "ASML": {"sector": "AI_SEMI",      "theme": "SEMICONDUCTOR_EQUIPMENT"},
    "AMAT": {"sector": "AI_SEMI",      "theme": "SEMICONDUCTOR_EQUIPMENT"},
    "LRCX": {"sector": "AI_SEMI",      "theme": "SEMICONDUCTOR_EQUIPMENT"},
    "MU":   {"sector": "AI_SEMI",      "theme": "MEMORY"},
    # Hyperscalers
    "MSFT": {"sector": "HYPERSCALER",  "theme": "CLOUD_AI"},
    "GOOGL":{"sector": "HYPERSCALER",  "theme": "CLOUD_AI"},
    "AMZN": {"sector": "HYPERSCALER",  "theme": "CLOUD_ECOMMERCE"},
    "META": {"sector": "HYPERSCALER",  "theme": "SOCIAL_AI"},
    # AI Optics / Photonics
    "LITE": {"sector": "AI_OPTICS",    "theme": "OPTICAL_INTERCONNECT"},
    "COHR": {"sector": "AI_OPTICS",    "theme": "OPTICAL_INTERCONNECT"},
    # Data Center Infrastructure
    "VST":  {"sector": "DATA_CENTER",  "theme": "POWER_GENERATION"},
    "CEG":  {"sector": "DATA_CENTER",  "theme": "NUCLEAR_POWER"},
    "EQIX": {"sector": "DATA_CENTER",  "theme": "COLOCATION"},
    # Geopolitical / Macro Hedges
    "GLD":  {"sector": "GEO_HEDGE",    "theme": "GOLD_ETF"},
    "NEM":  {"sector": "GEO_HEDGE",    "theme": "GOLD_MINER"},
    "LMT":  {"sector": "GEO_HEDGE",    "theme": "DEFENSE"},
    "RTX":  {"sector": "GEO_HEDGE",    "theme": "DEFENSE"},
}


def is_tier1(symbol: str) -> bool:
    return symbol.upper() in TIER1_NAMES


def get_sector(symbol: str) -> str:
    """Return the sector for a tier-1 symbol, 'OTHER' if not in tier-1 list."""
    entry = TIER1_NAMES.get(symbol.upper())
    return entry["sector"] if entry else "OTHER"


def qualifies_for_swing(
    symbol: str,
    composite_score: float,
    rsi: float,
    volume_ratio: float,
    sentiment_score: float,
    regime: str,
    parabolic: bool,
    momentum_exhaustion: bool,
) -> tuple[bool, str]:
    """
    Returns (True, '') if the position qualifies for an overnight swing hold,
    or (False, reason) with a rejection reason.
    Gates from spec §7: tier-1, composite ≥ 25, RSI 50–82/72, volume 1.3×, sentiment ≥ 20.
    """
    if not is_tier1(symbol):
        return False, f"{symbol} not in tier-1 list"

    if composite_score < 25:
        return False, f"composite {composite_score:.1f} < 25"

    rsi_max = 82 if regime == "BULL_TREND" else 72
    if not (50 <= rsi <= rsi_max):
        return False, f"RSI {rsi:.1f} outside 50–{rsi_max} for regime {regime}"

    if volume_ratio < 1.3:
        return False, f"volume_ratio {volume_ratio:.2f} < 1.3"

    if sentiment_score < 20:
        return False, f"sentiment {sentiment_score} < 20"

    if parabolic and momentum_exhaustion:
        return False, "parabolic + momentum exhaustion — too late to enter"

    return True, ""
