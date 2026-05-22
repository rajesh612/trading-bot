"""
Trading universe — single source of truth.
Loads tickers.json once at import via load_json and builds O(1) lookup dicts.
All callers import from here; tier1_config.py and tickers.txt are deleted once
all callers are migrated.
"""
from __future__ import annotations
import logging
import config
from utils.io_utils import load_json

logger = logging.getLogger(__name__)


def _build() -> tuple:
    data = load_json(config.TICKERS_FILE, {})
    if not data:
        logger.critical(
            "sectors: %s missing or empty — trading universe is empty. "
            "Screener will run with 0 tickers and send a Telegram alert.",
            config.TICKERS_FILE,
        )
        return {}, frozenset(), {}, {}, [], [], {}

    symbol_to_etf: dict[str, str] = {}
    tier1_set: set[str] = set()
    symbol_to_theme: dict[str, str] = {}
    etf_to_symbols: dict[str, list[str]] = {}
    screener_symbols: list[str] = []
    symbol_to_sub_etf: dict[str, str] = {}

    for etf_key, sector_data in data.items():
        if etf_key.startswith("_"):
            continue
        symbols = sector_data.get("symbols", [])
        etf_to_symbols[etf_key] = []
        for entry in symbols:
            sym = entry["symbol"]
            symbol_to_etf[sym] = etf_key
            if entry.get("tier1", False):
                tier1_set.add(sym)
            symbol_to_theme[sym] = entry.get("theme", "")
            if "sub_etf" in entry:
                symbol_to_sub_etf[sym] = entry["sub_etf"]
            if entry.get("screener_eligible", True):
                screener_symbols.append(sym)
                etf_to_symbols[etf_key].append(sym)

    tier1_symbols = [s for s in screener_symbols if s in tier1_set]
    return symbol_to_etf, frozenset(tier1_set), symbol_to_theme, etf_to_symbols, screener_symbols, tier1_symbols, symbol_to_sub_etf


_sym_to_etf, TIER1_SET, _sym_to_theme, ETF_TO_SYMBOLS, SCREENER_SYMBOLS, TIER1_SYMBOLS, _sym_to_sub_etf = _build()

SYMBOL_TO_ETF    = _sym_to_etf
SYMBOL_TO_SECTOR = _sym_to_etf   # alias for clarity in sector-aware callers
SYMBOL_TO_THEME  = _sym_to_theme
SYMBOL_TO_SUB_ETF = _sym_to_sub_etf


def is_tier1(symbol: str) -> bool:
    return symbol.upper() in TIER1_SET


def get_sector_etf(symbol: str) -> str:
    """Returns the sector ETF key ('XLK', 'XLF', …) or 'OTHER' for unknown symbols."""
    return SYMBOL_TO_ETF.get(symbol.upper(), "OTHER")


def get_sub_sector_etf(symbol: str) -> str:
    """Returns sub_etf if defined in tickers.json, else falls back to get_sector_etf()."""
    return SYMBOL_TO_SUB_ETF.get(symbol.upper(), get_sector_etf(symbol))


def get_sector(symbol: str) -> str:
    """Alias of get_sector_etf for backward compatibility with tier1_config.get_sector()."""
    return get_sector_etf(symbol)


def get_theme(symbol: str) -> str:
    return SYMBOL_TO_THEME.get(symbol.upper(), "")


def sector_of(symbol: str) -> str:
    """Used by correlation_guard. Same as get_sector_etf."""
    return get_sector_etf(symbol)


def load_universe() -> list[str]:
    """Return all screener-eligible symbols in tickers.json order."""
    return list(SCREENER_SYMBOLS)
