"""
Tier-1 names: high-quality liquid stocks eligible for swing / PEAD positions.
These names get wider stops, larger TP targets, and can be held overnight.
"""

TIER1_NAMES: dict[str, dict] = {
    # AI & Semiconductors
    "NVDA": {"sector": "AI_SEMI",      "theme": "AI_GPU"},
    "AVGO": {"sector": "AI_SEMI",      "theme": "AI_NETWORKING"},
    "AMD":  {"sector": "AI_SEMI",      "theme": "AI_GPU"},
    "ARM":  {"sector": "AI_SEMI",      "theme": "AI_CHIP_ARCHITECTURE"},
    "MRVL": {"sector": "AI_SEMI",      "theme": "AI_NETWORKING"},
    "QCOM": {"sector": "AI_SEMI",      "theme": "AI_EDGE"},
    "ASML": {"sector": "AI_SEMI",      "theme": "SEMICONDUCTOR_EQUIPMENT"},
    "AMAT": {"sector": "AI_SEMI",      "theme": "SEMICONDUCTOR_EQUIPMENT"},
    "LRCX": {"sector": "AI_SEMI",      "theme": "SEMICONDUCTOR_EQUIPMENT"},
    "KLAC": {"sector": "AI_SEMI",      "theme": "SEMICONDUCTOR_EQUIPMENT"},
    "MU":   {"sector": "AI_SEMI",      "theme": "MEMORY"},
    "STX":  {"sector": "AI_SEMI",      "theme": "MEMORY"},
    "TSM":  {"sector": "AI_SEMI",      "theme": "SEMICONDUCTOR_FOUNDRY"},
    "INTC": {"sector": "AI_SEMI",      "theme": "SEMICONDUCTOR_FOUNDRY"},
    # Hyperscalers
    "MSFT": {"sector": "HYPERSCALER",  "theme": "CLOUD_AI"},
    "GOOGL":{"sector": "HYPERSCALER",  "theme": "CLOUD_AI"},
    "AMZN": {"sector": "HYPERSCALER",  "theme": "CLOUD_ECOMMERCE"},
    "META": {"sector": "HYPERSCALER",  "theme": "SOCIAL_AI"},
    "ORCL": {"sector": "HYPERSCALER",  "theme": "CLOUD_DATABASE"},
    "PLTR": {"sector": "HYPERSCALER",  "theme": "AI_SOFTWARE"},
    # AI Software & SaaS
    "NOW":  {"sector": "AI_SAAS",      "theme": "ENTERPRISE_AI_WORKFLOW"},
    "ADBE": {"sector": "AI_SAAS",      "theme": "AI_CREATIVE_TOOLS"},
    "INTU": {"sector": "AI_SAAS",      "theme": "AI_FINTECH_SAAS"},
    "SNPS": {"sector": "AI_SAAS",      "theme": "EDA_TOOLS"},
    "CRWD": {"sector": "AI_SAAS",      "theme": "CYBERSECURITY"},
    "PANW": {"sector": "AI_SAAS",      "theme": "CYBERSECURITY"},
    "APP":  {"sector": "AI_SAAS",      "theme": "AI_ADTECH"},
    "SHOP": {"sector": "AI_SAAS",      "theme": "AI_ECOMMERCE"},
    "DDOG": {"sector": "AI_SAAS",      "theme": "AI_OBSERVABILITY"},
    # High-Growth Tech & Consumer
    "DIS":  {"sector": "TECHNOLOGY",   "theme": "AI_CONTENT_STREAMING"},
    "UBER": {"sector": "TECHNOLOGY",   "theme": "AI_MOBILITY"},
    "SPOT": {"sector": "TECHNOLOGY",   "theme": "AI_AUDIO_STREAMING"},
    "TXN":  {"sector": "AI_SEMI",      "theme": "ANALOG_SEMI"},
    "ADI":  {"sector": "AI_SEMI",      "theme": "ANALOG_SEMI"},
    # AI Optics / Photonics
    "LITE": {"sector": "AI_OPTICS",    "theme": "OPTICAL_INTERCONNECT"},
    "COHR": {"sector": "AI_OPTICS",    "theme": "OPTICAL_INTERCONNECT"},
    # Data Center Infrastructure
    "EQIX": {"sector": "DATA_CENTER",  "theme": "COLOCATION"},
    "VRT":  {"sector": "DATA_CENTER",  "theme": "COOLING_POWER"},
    "ANET": {"sector": "DATA_CENTER",  "theme": "NETWORKING"},
    # Software & Technology (large cap ≥ ~$100B or strategic quality)
    "AAPL": {"sector": "TECHNOLOGY",   "theme": "CONSUMER_ELECTRONICS"},
    "TSLA": {"sector": "TECHNOLOGY",   "theme": "EV_ROBOTICS_AI"},
    "IBM":  {"sector": "TECHNOLOGY",   "theme": "ENTERPRISE_AI"},
    "NFLX": {"sector": "TECHNOLOGY",   "theme": "AI_CONTENT_STREAMING"},
    "CRM":  {"sector": "TECHNOLOGY",   "theme": "AI_AGENTS_CRM"},
    # Financials
    "JPM":  {"sector": "FINANCIALS",   "theme": "BANKING"},
    "GS":   {"sector": "FINANCIALS",   "theme": "INVESTMENT_BANKING"},
    "MS":   {"sector": "FINANCIALS",   "theme": "WEALTH_MANAGEMENT"},
    "BRK.B":{"sector": "FINANCIALS",   "theme": "INVESTMENT_HOLDING"},
    "V":    {"sector": "FINANCIALS",   "theme": "DIGITAL_PAYMENTS"},
    "MA":   {"sector": "FINANCIALS",   "theme": "DIGITAL_PAYMENTS"},
    "AXP":  {"sector": "FINANCIALS",   "theme": "PREMIUM_PAYMENTS"},
    "COIN": {"sector": "FINANCIALS",   "theme": "CRYPTO_EXCHANGE"},
    "SPGI": {"sector": "FINANCIALS",   "theme": "FINANCIAL_DATA"},
    "BLK":  {"sector": "FINANCIALS",   "theme": "ASSET_MANAGEMENT"},
    "BKNG": {"sector": "TRAVEL_TECH",  "theme": "ONLINE_TRAVEL"},
    "ABNB": {"sector": "TRAVEL_TECH",  "theme": "HOME_SHARING"},
    "MAR":  {"sector": "TRAVEL_TECH",  "theme": "HOSPITALITY"},
    "HLT":  {"sector": "TRAVEL_TECH",  "theme": "HOSPITALITY"},
    # Industrial & Infrastructure AI
    "CAT":  {"sector": "INDUSTRIAL",   "theme": "AUTONOMOUS_EQUIPMENT"},
    "HON":  {"sector": "INDUSTRIAL",   "theme": "INDUSTRIAL_AI"},
    "GE":   {"sector": "INDUSTRIAL",   "theme": "AEROSPACE_POWER"},
    "GEV":  {"sector": "INDUSTRIAL",   "theme": "GRID_ELECTRIFICATION"},
    "DELL": {"sector": "INDUSTRIAL",   "theme": "AI_SERVER_INFRASTRUCTURE"},
    # Pharmaceuticals & Healthcare
    "LLY":  {"sector": "PHARMA_HEALTH", "theme": "PHARMACEUTICALS"},
    "UNH":  {"sector": "PHARMA_HEALTH", "theme": "HEALTH_INSURANCE"},
    "JNJ":  {"sector": "PHARMA_HEALTH", "theme": "DIVERSIFIED_HEALTHCARE"},
    "ABBV": {"sector": "PHARMA_HEALTH", "theme": "PHARMACEUTICALS"},
    "MRK":  {"sector": "PHARMA_HEALTH", "theme": "PHARMACEUTICALS"},
    "AMGN": {"sector": "PHARMA_HEALTH", "theme": "BIOTECHNOLOGY"},
    "ISRG": {"sector": "PHARMA_HEALTH", "theme": "SURGICAL_ROBOTICS"},
    "REGN": {"sector": "PHARMA_HEALTH", "theme": "BIOTECHNOLOGY"},
    # Consumer Staples
    "WMT":  {"sector": "CONSUMER_STAPLES", "theme": "RETAIL"},
    "PG":   {"sector": "CONSUMER_STAPLES", "theme": "CONSUMER_GOODS"},
    "COST": {"sector": "CONSUMER_STAPLES", "theme": "RETAIL"},
    "KO":   {"sector": "CONSUMER_STAPLES", "theme": "BEVERAGES"},
    # Energy
    "XOM":  {"sector": "ENERGY",       "theme": "INTEGRATED_OIL"},
    "CVX":  {"sector": "ENERGY",       "theme": "INTEGRATED_OIL"},
    "NEE":  {"sector": "ENERGY",       "theme": "RENEWABLE_ENERGY"},
    "VST":  {"sector": "ENERGY",       "theme": "POWER_GENERATION"},
    "CEG":  {"sector": "ENERGY",       "theme": "NUCLEAR_POWER"},
    # Aerospace & Space
    "BA":   {"sector": "AEROSPACE_DEFENSE", "theme": "COMMERCIAL_AEROSPACE"},
    "GD":   {"sector": "AEROSPACE_DEFENSE", "theme": "DEFENSE_SYSTEMS"},
    "LHX":  {"sector": "AEROSPACE_DEFENSE", "theme": "DEFENSE_ELECTRONICS"},
    "TDG":  {"sector": "AEROSPACE_DEFENSE", "theme": "AEROSPACE_COMPONENTS"},
    "HWM":  {"sector": "AEROSPACE_DEFENSE", "theme": "AEROSPACE_COMPONENTS"},
    "HEI":  {"sector": "AEROSPACE_DEFENSE", "theme": "AEROSPACE_PARTS"},
    # Geopolitical / Macro Hedges
    "GLD":  {"sector": "GEO_HEDGE",    "theme": "GOLD_ETF"},
    "NEM":  {"sector": "GEO_HEDGE",    "theme": "GOLD_MINER"},
    "LMT":  {"sector": "GEO_HEDGE",    "theme": "DEFENSE"},
    "RTX":  {"sector": "GEO_HEDGE",    "theme": "DEFENSE"},
    "NOC":  {"sector": "GEO_HEDGE",    "theme": "DEFENSE"},
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
