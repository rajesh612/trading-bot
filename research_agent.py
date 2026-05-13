# research_agent.py — backward-compatible re-exports; real code in research/
from research.agent import run_research_agent
from research.fetchers import (
    _fetch_with_retry,
    _fetch_rss,
    _fetch_stock_news,
    _fetch_market_news,
    _fetch_stocktwits,
    _fetch_reddit,
    _fetch_x_posts,
    MARKET_RSS_FEEDS,
    REDDIT_SUBREDDITS,
    REQUEST_HEADERS,
)
from research.analyzer import (
    _compute_macro_shock_score,
    _compute_sector_sentiment,
    _detect_capitulation_candidate,
    _quick_sentiment_result,
    _analyze_stock,
    _MACRO_SHOCK_KEYWORDS,
)

__all__ = ["run_research_agent"]
