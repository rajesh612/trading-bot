import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import feedparser
import requests

import config

logger = logging.getLogger(__name__)

REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TradingBot/1.0; +research)"}

MARKET_RSS_FEEDS = [
    "https://feeds.marketwatch.com/marketwatch/topstories/",
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "https://feeds.benzinga.com/benzinga",
    "https://finance.yahoo.com/news/rssindex",
    "https://feeds.bloomberg.com/markets/news.rss",
]

REDDIT_SUBREDDITS = [
    "wallstreetbets",
    "stocks",
    "investing",
    "options",
    "StockMarket",
    "SecurityAnalysis",
]


def _fetch_with_retry(fn, *args, attempts: int = 3, base_delay: float = 1.0, **kwargs):
    """Call fn(*args, **kwargs) up to `attempts` times with exponential backoff."""
    if attempts < 1:
        raise ValueError(f"attempts must be >= 1, got {attempts}")
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception:
            if i == attempts - 1:
                raise
            time.sleep(base_delay * (2 ** i))


def _fetch_rss(url: str, max_items: int = 6) -> list[dict]:
    try:
        resp = _fetch_with_retry(requests.get, url, headers=REQUEST_HEADERS, timeout=8)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        return [
            {
                "title": e.get("title", ""),
                "summary": e.get("summary", "")[:400],
                "published": e.get("published", ""),
            }
            for e in feed.entries[:max_items]
        ]
    except Exception as exc:
        logger.debug(f"RSS fetch failed {url}: {exc}")
        return []


def _fetch_stock_news(symbol: str) -> list[dict]:
    items: list[dict] = []
    items.extend(_fetch_rss(f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US"))
    items.extend(_fetch_rss(f"https://www.benzinga.com/stock/{symbol.lower()}/feed"))
    return items[:10]


def _fetch_market_news() -> list[dict]:
    all_items: list[dict] = []
    for url in MARKET_RSS_FEEDS:
        all_items.extend(_fetch_rss(url))
    return all_items[:25]


def _fetch_stocktwits(symbol: str) -> dict:
    """
    Returns raw messages plus a pre-counted bullish/bearish split.
    StockTwits users optionally tag posts as Bullish or Bearish — this is
    the cleanest free retail-sentiment signal available.
    """
    try:
        url = f"https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"
        resp = _fetch_with_retry(requests.get, url, headers=REQUEST_HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        messages = data.get("messages", [])
        bullish = sum(
            1 for m in messages
            if m.get("entities", {}).get("sentiment", {}).get("basic") == "Bullish"
        )
        bearish = sum(
            1 for m in messages
            if m.get("entities", {}).get("sentiment", {}).get("basic") == "Bearish"
        )
        total_tagged = bullish + bearish
        bullish_pct = round(bullish / total_tagged * 100) if total_tagged > 0 else None

        snippets = [
            {
                "text": m.get("body", "")[:200],
                "sentiment": m.get("entities", {}).get("sentiment", {}).get("basic"),
                "likes": m.get("likes", {}).get("total", 0),
            }
            for m in messages[:5]
        ]

        return {
            "available": True,
            "message_count": len(messages),
            "bullish_count": bullish,
            "bearish_count": bearish,
            "bullish_pct": bullish_pct,
            "snippets": snippets,
        }
    except Exception as exc:
        logger.debug(f"StockTwits fetch failed for {symbol}: {exc}")
        return {"available": False}


def _fetch_reddit(symbol: str) -> dict:
    """
    Fetches Reddit posts using the public JSON API.
    Six subreddit searches run in parallel via ThreadPoolExecutor.
    """
    headers = {"User-Agent": "TradingBot/1.0 (sentiment research; read-only)"}

    def _fetch_sub(sub: str) -> list[dict]:
        url = (
            f"https://www.reddit.com/r/{sub}/search.json"
            f"?q={symbol}+OR+%24{symbol}"
            f"&sort=new&t=day&restrict_sr=true&limit=5"
        )
        resp = _fetch_with_retry(requests.get, url, headers=headers, timeout=10)
        resp.raise_for_status()
        children = resp.json().get("data", {}).get("children", [])
        return [
            {
                "subreddit": sub,
                "title": p.get("title", ""),
                "text": (p.get("selftext", "") or "")[:300],
                "score": p.get("score", 0),
                "upvote_ratio": p.get("upvote_ratio", 0),
                "num_comments": p.get("num_comments", 0),
                "url": p.get("url", ""),
            }
            for child in children
            for p in [child.get("data", {})]
        ]

    posts: list[dict] = []
    with ThreadPoolExecutor(max_workers=len(REDDIT_SUBREDDITS)) as executor:
        futures = {executor.submit(_fetch_sub, sub): sub for sub in REDDIT_SUBREDDITS}
        for future in as_completed(futures):
            sub = futures[future]
            try:
                posts.extend(future.result())
            except Exception as exc:
                logger.debug(f"Reddit r/{sub} fetch failed for {symbol}: {exc}")

    posts.sort(key=lambda p: p["score"], reverse=True)
    return {"available": True, "post_count": len(posts), "posts": posts[:5]}


def _fetch_x_posts(symbol: str) -> dict:
    """
    Fetches recent tweets mentioning $SYMBOL.
    Requires X Basic API ($100/mo). Skips gracefully if token not set.
    Filters out retweets and focuses on original posts with engagement.
    """
    if not config.X_BEARER_TOKEN:
        return {"available": False, "reason": "X_BEARER_TOKEN not configured"}

    try:
        import tweepy
        client = tweepy.Client(bearer_token=config.X_BEARER_TOKEN, wait_on_rate_limit=False)

        query = f"${symbol} lang:en -is:retweet -is:reply"
        response = _fetch_with_retry(
            client.search_recent_tweets,
            query=query,
            max_results=20,
            tweet_fields=["created_at", "public_metrics", "text", "author_id"],
            attempts=2,
            base_delay=0.5,
        )

        if not response.data:
            return {"available": True, "tweet_count": 0, "tweets": []}

        tweets = [
            {
                "text": t.text[:280],
                "likes": t.public_metrics.get("like_count", 0),
                "retweets": t.public_metrics.get("retweet_count", 0),
                "replies": t.public_metrics.get("reply_count", 0),
                "created_at": str(t.created_at),
            }
            for t in response.data
        ]
        # Sort by engagement (likes + retweets) so top posts come first
        tweets.sort(key=lambda t: t["likes"] + t["retweets"], reverse=True)

        return {
            "available": True,
            "tweet_count": len(tweets),
            "tweets": tweets[:5],
        }
    except Exception as exc:
        logger.warning(f"X fetch failed for {symbol}: {exc}")
        return {"available": False, "reason": str(exc)}
