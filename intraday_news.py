"""
Intraday news cache — refreshed every 30 min by scheduler.
No Claude calls. Uses existing fetchers from research/fetchers.py.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

import config
from research.fetchers import _fetch_stock_news, _fetch_stocktwits
from research.analyzer import _compute_macro_shock_score
from utils.io_utils import load_json, save_json

logger = logging.getLogger(__name__)

_POSITIVE_KEYWORDS = {"beat", "upgrade", "approval", "surge", "record", "strong", "breakout", "raised"}
_NEGATIVE_KEYWORDS = {"miss", "downgrade", "recall", "warning", "cut", "weak", "fail", "suspend", "fraud"}


def _compute_symbol_sentiment(headlines: list[dict], stocktwits: dict) -> int:
    """
    Keyword-weighted score: +5 per positive hit, -5 per negative hit,
    plus StockTwits bull ratio scaled to [-15, +15].
    Clamped to [-100, +100].
    """
    if not headlines and not stocktwits:
        return 0

    text = " ".join(
        (h.get("title", "") + " " + h.get("summary", "")).lower()
        for h in headlines
    )
    score = 0
    for kw in _POSITIVE_KEYWORDS:
        if kw in text:
            score += 5
    for kw in _NEGATIVE_KEYWORDS:
        if kw in text:
            score -= 5

    bull_pct = stocktwits.get("bullish_percent", 50)
    score += round((bull_pct - 50) * 0.3)   # 50% neutral = 0, 100% bullish = +15

    return max(-100, min(100, score))


def refresh_intraday_news(watch_list: list[str]) -> None:
    """
    Fetch fresh headlines + StockTwits for each symbol.
    Writes intraday_news_cache.json. No Claude calls.
    """
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
    Load cache. Returns {} (not an error) if file missing or older than
    INTRADAY_NEWS_STALE_MIN minutes — callers fall back to research sentiment.
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
