"""
Intraday news cache — refreshed every 30 min by scheduler.
No Claude calls. Uses existing fetchers from research/fetchers.py.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import config
from research.fetchers import _fetch_stock_news, _fetch_stocktwits
from research.analyzer import _compute_macro_shock_score, _compute_symbol_sentiment
from utils.io_utils import load_json, save_json

logger = logging.getLogger(__name__)


def refresh_intraday_news(watch_list: list[str]) -> None:
    """Fetch fresh headlines + StockTwits for each symbol and write cache."""
    cache: dict = {"__updated_at": datetime.now(timezone.utc).isoformat()}
    for symbol in watch_list:
        try:
            headlines  = _fetch_stock_news(symbol)
            stocktwits = _fetch_stocktwits(symbol)
            shock      = _compute_macro_shock_score(headlines)
            sentiment  = _compute_symbol_sentiment(headlines, stocktwits)
            top3       = [h.get("title", "") for h in headlines[:3]]
            cache[symbol] = {
                "sentiment_score": sentiment,
                "shock_score":     shock,
                "headlines":       top3,
            }
        except Exception as exc:
            logger.warning("intraday_news: failed to fetch %s: %s", symbol, exc)
            cache[symbol] = {"sentiment_score": 0, "shock_score": 0, "headlines": []}

    save_json(config.INTRADAY_NEWS_CACHE_FILE, cache)
    logger.info("Intraday news cache updated for %d symbols.", len(watch_list))


def load_intraday_news() -> dict:
    """
    Load cache. Returns {} if file missing or older than INTRADAY_NEWS_STALE_MIN
    minutes — callers fall back to research sentiment.
    Strips __updated_at meta key from returned dict.
    """
    raw = load_json(config.INTRADAY_NEWS_CACHE_FILE, {})
    if not raw:
        return {}

    ts_str = raw.get("__updated_at")
    if ts_str:
        try:
            updated = datetime.fromisoformat(ts_str)
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            age_min = (datetime.now(timezone.utc) - updated).total_seconds() / 60
            if age_min > config.INTRADAY_NEWS_STALE_MIN:
                logger.debug("Intraday news cache stale (%.0f min) — treating as missing.", age_min)
                return {}
        except Exception:
            return {}

    return {k: v for k, v in raw.items() if k != "__updated_at"}
