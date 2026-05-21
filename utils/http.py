"""Shared HTTP utilities for Yahoo Finance requests."""
import time
import logging

import requests

logger = logging.getLogger(__name__)

YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0"}
_session = requests.Session()
_session.headers.update(YAHOO_HEADERS)

_AUTH_ERROR_CODES     = frozenset({401, 403})
_RETRYABLE_CODES      = frozenset({429, 500, 502, 503, 504})


def yahoo_get(url: str, timeout: int = 10) -> dict:
    """GET a Yahoo Finance URL.

    - 401/403: fast-fail with ERROR log (retrying auth errors wastes quota).
    - 429/5xx: retry up to 3x with exponential backoff.
    - Connection timeout: retry up to 3x with 1s sleep.
    """
    for attempt in range(3):
        try:
            resp = _session.get(url, timeout=timeout)
            if resp.status_code in _AUTH_ERROR_CODES:
                logger.error(
                    "Yahoo Finance auth error %d for %s — not retrying",
                    resp.status_code, url,
                )
                resp.raise_for_status()
            if resp.status_code in _RETRYABLE_CODES:
                wait = 2 ** attempt
                logger.warning(
                    "Yahoo Finance %d — retry %d/3 in %ds (%s)",
                    resp.status_code, attempt + 1, wait, url,
                )
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.Timeout:
            if attempt == 2:
                raise
            time.sleep(1)
    return {}
