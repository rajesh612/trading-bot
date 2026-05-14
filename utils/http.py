"""Shared HTTP utilities for Yahoo Finance requests."""
import time

import requests

YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0"}
_session = requests.Session()
_session.headers.update(YAHOO_HEADERS)


def yahoo_get(url: str, timeout: int = 10) -> dict:
    """GET a Yahoo Finance URL; retries up to 3x with exponential backoff on 429."""
    for attempt in range(3):
        try:
            resp = _session.get(url, timeout=timeout)
            if resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1)
    return {}
