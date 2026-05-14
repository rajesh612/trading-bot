"""
FRED API cache — fetches Fed funds, CPI, unemployment and writes fred_cache.json.
Boundary module: file I/O is intentional here.
"""
import logging
from datetime import datetime
import requests

import config
from utils.io_utils import load_json, save_json
from utils.constants import ET

logger = logging.getLogger(__name__)


def refresh_fred_cache() -> None:
    """Fetch Fed funds, CPI, unemployment from FRED API. Skip silently if key missing."""
    if not config.FRED_API_KEY:
        logger.info("FRED_API_KEY not set — skipping FRED cache refresh.")
        return

    base = "https://api.stlouisfed.org/fred/series/observations"
    params_common = {
        "api_key": config.FRED_API_KEY,
        "file_type": "json",
        "sort_order": "desc",
        "limit": "3",
    }

    def _fetch(series_id: str) -> list[float]:
        try:
            resp = requests.get(
                base, params={"series_id": series_id, **params_common}, timeout=10
            )
            resp.raise_for_status()
            obs = resp.json().get("observations", [])
            return [float(o["value"]) for o in obs if o.get("value") not in (".", "")]
        except Exception as exc:
            logger.warning(f"FRED fetch {series_id} failed: {exc}")
            return []

    dff   = _fetch("DFF")       # Fed funds rate
    cpi   = _fetch("CPIAUCSL")  # CPI
    urate = _fetch("UNRATE")    # Unemployment rate

    # Fed direction
    if len(dff) >= 2:
        if dff[0] < dff[1]:
            fed_direction = "cutting"
        elif dff[0] > dff[1]:
            fed_direction = "hiking"
        else:
            fed_direction = "paused"
    else:
        fed_direction = "unknown"

    # CPI trend (YoY, 3-month direction)
    cpi_trend = "stable"
    if len(cpi) >= 3:
        if cpi[0] > cpi[2] * 1.01:
            cpi_trend = "rising"
        elif cpi[0] < cpi[2] * 0.99:
            cpi_trend = "falling"

    # Unemployment trend
    unemployment_rising = len(urate) >= 2 and (urate[0] - urate[1]) >= 0.3

    # Compute modifier: cutting=+5, hiking=-5, rising unemployment=-3
    fred_modifier = 0
    if fed_direction == "cutting":
        fred_modifier += 5
    elif fed_direction == "hiking":
        fred_modifier -= 5
    if unemployment_rising:
        fred_modifier -= 3

    cache = {
        "fed_funds_rate":      dff[0] if dff else None,
        "fed_direction":       fed_direction,
        "cpi_trend":           cpi_trend,
        "unemployment_rising": unemployment_rising,
        "fred_modifier":       fred_modifier,
        "updated_at":          datetime.now(ET).isoformat(),
    }
    save_json(config.FRED_CACHE_FILE, cache)
    logger.info(
        f"FRED cache updated: fed={fed_direction}, cpi={cpi_trend}, "
        f"unemp_rising={unemployment_rising}, modifier={fred_modifier:+d}"
    )


def load_fred_modifier() -> int:
    """Return the FRED modifier from cache, or 0 if unavailable."""
    try:
        return int(load_json(config.FRED_CACHE_FILE, {}).get("fred_modifier", 0))
    except Exception:
        return 0
