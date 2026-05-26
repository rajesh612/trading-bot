"""
FRED macro signal fetcher. Expands from 3 → 7 series.
Uses module-level Session (Rule 4/5). Cached in fred_cache.json at 4 PM.
"""
from __future__ import annotations
import logging
import time
from datetime import datetime

import requests

import config
from utils.constants import ET
from utils.io_utils import load_json, save_json

_logger = logging.getLogger(__name__)

_FRED_BASE   = "https://api.stlouisfed.org/fred/series/observations"
_FRED_SESSION: requests.Session | None = None


def _get_session() -> requests.Session:
    global _FRED_SESSION
    if _FRED_SESSION is None:
        _FRED_SESSION = requests.Session()
        _FRED_SESSION.headers["Accept"] = "application/json"
    return _FRED_SESSION


def _fetch_n(series_id: str, n: int) -> list[float]:
    """Fetch the n most-recent observations for a FRED series. Returns floats descending (newest first)."""
    api_key = getattr(config, "FRED_API_KEY", "")
    if not api_key:
        return []
    sess = _get_session()
    params = {
        "series_id":         series_id,
        "api_key":           api_key,
        "file_type":         "json",
        "sort_order":        "desc",
        "observation_start": "2000-01-01",
        "limit":             str(n),
    }
    for attempt in range(3):
        try:
            resp = sess.get(_FRED_BASE, params=params, timeout=10)
            resp.raise_for_status()
            obs = resp.json().get("observations", [])
            values = []
            for o in obs:
                try:
                    v = float(o["value"])
                    values.append(v)
                except (KeyError, ValueError):
                    pass
            return values
        except requests.HTTPError as exc:
            if hasattr(exc, "response") and exc.response.status_code == 429 and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            _logger.warning("FRED fetch %s attempt %d: %s", series_id, attempt, exc)
            return []
        except Exception as exc:
            _logger.warning("FRED fetch %s: %s", series_id, exc)
            return []
    return []


def _compute_cpi_yoy(cpi: list[float]) -> float | None:
    """12-month YoY from 13 monthly observations (index 0 = most recent)."""
    if len(cpi) < 13:
        return None
    return (cpi[0] / cpi[12] - 1) * 100


def _compute_modifier(
    fed_direction: str,
    cpi_vs_target: str,
    yield_curve_inverted: bool,
    credit_spread_risk_off: bool,
    breakeven_high: bool,
    unemployment_rising: bool,
    claims_spike: bool,
) -> int:
    score = 0
    if fed_direction == "cutting":
        score += 5
    elif fed_direction == "hiking":
        score -= 5
    if cpi_vs_target == "above":
        score -= 3
    elif cpi_vs_target == "below":
        score += 2
    if yield_curve_inverted:
        score -= 4
    if credit_spread_risk_off:
        score -= 5
    if breakeven_high:
        score -= 2
    if unemployment_rising:
        score -= 3
    if claims_spike:
        score -= 2
    return max(config.FRED_MODIFIER_MIN, min(config.FRED_MODIFIER_MAX, score))


def refresh_fred_cache() -> None:
    """Fetch 7 FRED series and write fred_cache.json. Called at 4 PM by scheduler."""
    # Fed funds rate
    dff   = _fetch_n("DFF", 3)
    fed_direction = "unknown"
    if len(dff) >= 3:
        if dff[0] < dff[2]:
            fed_direction = "cutting"
        elif dff[0] > dff[2]:
            fed_direction = "hiking"
        else:
            fed_direction = "paused"

    # CPI — 12-month YoY
    cpi     = _fetch_n("CPIAUCSL", config.FRED_CPI_OBS_LOOKBACK)
    cpi_yoy = _compute_cpi_yoy(cpi)
    if cpi_yoy is None:
        cpi_vs_target = "unknown"
    elif cpi_yoy > config.FRED_CPI_TARGET_PCT + 0.5:
        cpi_vs_target = "above"
    elif cpi_yoy < config.FRED_CPI_TARGET_PCT - 0.5:
        cpi_vs_target = "below"
    else:
        cpi_vs_target = "at"

    # Unemployment
    unrate = _fetch_n("UNRATE", 3)
    unemployment_rising = len(unrate) >= 2 and (unrate[0] - unrate[1]) >= 0.3

    # Yield curve (T10Y2Y)
    yc     = _fetch_n(config.FRED_YIELD_CURVE_SERIES, 2)
    yc_val = yc[0] if yc else None
    yield_curve_inverted = yc_val is not None and yc_val < config.FRED_YIELD_CURVE_INVERSION_THRESHOLD

    # HY credit spread
    cs     = _fetch_n(config.FRED_CREDIT_SPREAD_SERIES, 2)
    cs_val = cs[0] if cs else None
    credit_spread_risk_off = cs_val is not None and cs_val >= config.FRED_CREDIT_SPREAD_RISK_OFF

    # Breakeven inflation
    bei     = _fetch_n(config.FRED_BREAKEVEN_SERIES, 2)
    bei_val = bei[0] if bei else None
    breakeven_high = bei_val is not None and bei_val > config.FRED_BREAKEVEN_HIGH_THRESHOLD

    # Weekly jobless claims
    icsa        = _fetch_n(config.FRED_CLAIMS_SERIES, 3)
    claims_spike = len(icsa) >= 2 and (icsa[0] - icsa[1]) >= config.FRED_CLAIMS_SPIKE_THRESHOLD

    modifier = _compute_modifier(
        fed_direction, cpi_vs_target, yield_curve_inverted,
        credit_spread_risk_off, breakeven_high, unemployment_rising, claims_spike,
    )

    cache = {
        "fed_funds_rate":         dff[0] if dff else None,
        "fed_direction":          fed_direction,
        "cpi_yoy_pct":            cpi_yoy,
        "cpi_vs_target":          cpi_vs_target,
        "unemployment_rising":    unemployment_rising,
        "yield_curve_inverted":   yield_curve_inverted,
        "yield_curve_spread":     yc_val,
        "credit_spread_bps":      cs_val,
        "credit_spread_risk_off": credit_spread_risk_off,
        "breakeven_inflation":    bei_val,
        "claims_spike":           claims_spike,
        "fred_modifier":          modifier,
        "updated_at":             datetime.now(ET).isoformat(),
    }
    save_json(config.FRED_CACHE_FILE, cache)
    _logger.info("FRED cache refreshed: modifier=%d", modifier)


def load_fred_modifier() -> int:
    return load_json(config.FRED_CACHE_FILE, {}).get("fred_modifier", 0)


def load_fred_cache() -> dict:
    return load_json(config.FRED_CACHE_FILE, {})
