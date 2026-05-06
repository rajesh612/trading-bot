import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo

import feedparser
import requests
import anthropic

import config
from tier1_config import get_sector

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TradingBot/1.0; +research)"}


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


# ── News sources ─────────────────────────────────────────────────────────────

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


# ── StockTwits (free, no auth) ────────────────────────────────────────────────

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


# ── Reddit (PRAW — free with credentials) ────────────────────────────────────

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


# ── X / Twitter (tweepy — requires Basic API $100/mo) ────────────────────────

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


# ── Macro shock + sector analysis ─────────────────────────────────────────────

_MACRO_SHOCK_KEYWORDS = {
    "tariff": 20, "trade war": 25, "sanction": 20, "war escalation": 30,
    "fed emergency": 35, "bank failure": 35, "cpi shock": 25, "inflation surge": 20,
    "recession": 20, "default": 25, "nuclear": 30, "pandemic": 30,
    "market crash": 30, "circuit breaker": 35,
}


def _compute_macro_shock_score(headlines: list[dict]) -> int:
    """Keyword-weighted scan of market headlines; capped at 100."""
    if not headlines:
        return 0
    text = " ".join(
        (h.get("title", "") + " " + h.get("summary", "")).lower()
        for h in headlines
    )
    score = 0
    for keyword, weight in _MACRO_SHOCK_KEYWORDS.items():
        if keyword in text:
            score += weight
    return min(100, score)


def _compute_sector_sentiment(per_stock_report: dict) -> dict:
    """Group combined_sentiment_score by tier-1 sector; return mean per bucket."""
    buckets: dict[str, list[float]] = {}
    for symbol, data in per_stock_report.items():
        if symbol == "__meta__":
            continue
        sector = get_sector(symbol)
        score = data.get("combined_sentiment_score", data.get("sentiment_score", 0))
        buckets.setdefault(sector, []).append(float(score))
    return {s: round(sum(v) / len(v), 1) for s, v in buckets.items() if v}


def _detect_capitulation_candidate(
    symbol: str,
    drawdown_from_20d_high: float,
    sector_sentiment: float,
    obv_trend: float,
) -> bool:
    """True if drawdown ≤ -20%, sector sentiment < -20, and OBV rising (> 0.10)."""
    return drawdown_from_20d_high <= -20.0 and sector_sentiment < -20 and obv_trend > 0.10


# ── Rule-based pre-filter (saves Claude tokens for ambiguous cases) ──────────

def _quick_sentiment_result(stocktwits: dict, stock_news: list[dict]) -> dict | None:
    """
    Returns a filled analysis dict when StockTwits signal is unambiguous AND there
    is no stock-specific news that could override social sentiment.  Returns None
    when Claude is still needed (news present, weak signal, or insufficient data).

    Fires only when:
      - StockTwits is available with ≥ STOCKTWITS_MIN_TAGGED tagged messages
      - bullish_pct ≥ QUICK_SENTIMENT_BULL_PCT  →  rule-based BULLISH
      - bullish_pct ≤ QUICK_SENTIMENT_BEAR_PCT  →  rule-based BEARISH
      - zero stock-specific news items (news could carry a catalyst that overrides social)
    """
    if stock_news:
        return None  # always ask Claude when news exists — catalyst could flip the signal

    if not stocktwits.get("available"):
        return None

    tagged = stocktwits.get("bullish_count", 0) + stocktwits.get("bearish_count", 0)
    bullish_pct = stocktwits.get("bullish_pct")

    if tagged < config.STOCKTWITS_MIN_TAGGED or bullish_pct is None:
        return None

    if bullish_pct >= config.QUICK_SENTIMENT_BULL_PCT:
        label, score = "BULLISH", config.QUICK_SENTIMENT_SCORE_BULL
    elif bullish_pct <= config.QUICK_SENTIMENT_BEAR_PCT:
        label, score = "BEARISH", config.QUICK_SENTIMENT_SCORE_BEAR
    else:
        return None  # ambiguous — ask Claude

    return {
        "news_sentiment_score":    0,
        "news_sentiment_label":    "NEUTRAL",
        "social_sentiment_score":  score,
        "social_sentiment_label":  label,
        "combined_sentiment_score": score,
        "combined_sentiment_label": label,
        "key_catalyst":   None,
        "risk_flag":      None,
        "divergence_alert": None,
        "summary":   f"Rule-based: StockTwits {bullish_pct}% bullish ({tagged} tagged msgs, no news).",
        "strategy":  f"Social sentiment strongly {label.lower()} — trade with trend.",
    }


# ── Claude unified analysis ───────────────────────────────────────────────────

def _analyze_stock(
    symbol: str,
    stock_news: list[dict],
    market_context_block: str,
    stocktwits: dict,
    reddit: dict,
    x_posts: dict,
    ai: anthropic.Anthropic,
    cached_system: list[dict],
) -> dict:
    # Cap input size: 3 headlines, 200-char summaries to reduce input tokens
    news_block = "\n".join(
        f"- {n['title']}: {n['summary'][:200]}" for n in stock_news[:3]
    ) or "No specific news."

    # StockTwits block
    if stocktwits.get("available"):
        st_snippets = "\n".join(
            f"  [{s.get('sentiment','?')}] {s['text']}" for s in stocktwits["snippets"][:3]
        )
        st_block = (
            f"StockTwits: {stocktwits['message_count']} messages | "
            f"Bull: {stocktwits['bullish_count']} Bear: {stocktwits['bearish_count']}"
            + (f" ({stocktwits['bullish_pct']}% bullish)" if stocktwits["bullish_pct"] else "")
            + f"\n{st_snippets}"
        )
    else:
        st_block = "StockTwits: not available."

    # Reddit block
    if reddit.get("available") and reddit.get("post_count", 0) > 0:
        reddit_snippets = "\n".join(
            f"  [r/{p['subreddit']} score={p['score']}] {p['title']}"
            for p in reddit["posts"][:5]
        )
        reddit_block = f"Reddit: {reddit['post_count']} posts\n{reddit_snippets}"
    elif reddit.get("available"):
        reddit_block = "Reddit: no posts today."
    else:
        reddit_block = f"Reddit: {reddit.get('reason', 'not available')}."

    # X block
    if x_posts.get("available") and x_posts.get("tweet_count", 0) > 0:
        x_snippets = "\n".join(
            f"  [❤{t['likes']} RT{t['retweets']}] {t['text'][:120]}"
            for t in x_posts["tweets"][:5]
        )
        x_block = f"X: {x_posts['tweet_count']} posts\n{x_snippets}"
    elif x_posts.get("available"):
        x_block = "X: no posts found."
    else:
        x_block = f"X: {x_posts.get('reason', 'not available')}."

    response = ai.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=500,
        system=cached_system,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Symbol: {symbol}\n\n"
                    f"=== STOCK NEWS ===\n{news_block}\n\n"
                    f"=== STOCKTWITS ===\n{st_block}\n\n"
                    f"=== REDDIT ===\n{reddit_block}\n\n"
                    f"=== X ===\n{x_block}\n\n"
                    "Respond with:\n"
                    "{\n"
                    '  "news_sentiment_score": <-100 to 100>,\n'
                    '  "news_sentiment_label": "<BULLISH|BEARISH|NEUTRAL>",\n'
                    '  "social_sentiment_score": <-100 to 100>,\n'
                    '  "social_sentiment_label": "<BULLISH|BEARISH|NEUTRAL>",\n'
                    '  "combined_sentiment_score": <-100 to 100, weighted blend>,\n'
                    '  "combined_sentiment_label": "<BULLISH|BEARISH|NEUTRAL>",\n'
                    '  "key_catalyst": "<main driver or null>",\n'
                    '  "risk_flag": "<major risk or null>",\n'
                    '  "divergence_alert": "<short squeeze or divergence if present, else null>",\n'
                    '  "summary": "<2-sentence combined summary>",\n'
                    '  "strategy": "<2-3 bullet points on intraday strategy>"\n'
                    "}"
                ),
            }
        ],
    )

    text = response.content[0].text.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    return {
        "news_sentiment_score": 0,
        "news_sentiment_label": "NEUTRAL",
        "social_sentiment_score": 0,
        "social_sentiment_label": "NEUTRAL",
        "combined_sentiment_score": 0,
        "combined_sentiment_label": "NEUTRAL",
        "key_catalyst": None,
        "risk_flag": None,
        "divergence_alert": None,
        "summary": "Parse error.",
        "strategy": "No strategy data.",
    }


# ── Main entry point ──────────────────────────────────────────────────────────

def run_research_agent(watch_list: list[str]) -> dict:
    if not watch_list:
        logger.warning("Watch list empty — skipping research.")
        return {}

    x_enabled = bool(config.X_BEARER_TOKEN)
    logger.info(
        f"Research agent: {len(watch_list)} stocks | "
        f"Reddit=✓ (public JSON) | "
        f"X={'✓' if x_enabled else '✗ (no token)'} | "
        f"StockTwits=✓"
    )

    ai = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    # Build cached system prompt once — includes market context so it is not
    # re-sent in every per-stock user message. The ephemeral cache stays warm
    # across the sequential per-stock calls (each takes only a few seconds).
    market_news = _fetch_market_news()
    market_context_block = "\n".join(f"- {n['title']}: {n['summary']}" for n in market_news[:6])
    cached_system = [
        {
            "type": "text",
            "text": (
                "You are a financial analyst combining institutional news with retail social media "
                "sentiment to generate intraday trading intelligence.\n\n"
                "Source weighting:\n"
                "- News: highest weight, institutional perspective\n"
                "- StockTwits: bullish/bearish tags from active traders — strong intraday signal\n"
                "- Reddit WSB/stocks: noisy — weight by post score\n"
                "- X: real-time but mixed quality — weight by engagement\n\n"
                "Flag divergence (news bearish + social very bullish) as a potential squeeze setup.\n"
                "Be concise and data-driven. Respond in strict JSON.\n\n"
                f"=== TODAY'S MARKET CONTEXT ===\n{market_context_block}"
            ),
            "cache_control": {"type": "ephemeral"},
        }
    ]

    report: dict = {}

    for symbol in watch_list:
        try:
            stock_news  = _fetch_stock_news(symbol)
            stocktwits  = _fetch_stocktwits(symbol)
            reddit_data = _fetch_reddit(symbol)
            x_data      = _fetch_x_posts(symbol)

            quick = _quick_sentiment_result(stocktwits, stock_news)
            if quick is not None:
                logger.info(
                    f"    {symbol}: rule-based sentiment ({quick['combined_sentiment_label']}, "
                    f"pct={stocktwits.get('bullish_pct')}%) — Claude skipped"
                )
                analysis = quick
            else:
                analysis = _analyze_stock(
                    symbol, stock_news, market_context_block, stocktwits, reddit_data, x_data, ai, cached_system
                )
        except Exception as exc:
            logger.warning(f"  {symbol}: research failed, using neutral defaults — {exc}")
            analysis = {
                "news_sentiment_score": 0, "news_sentiment_label": "NEUTRAL",
                "social_sentiment_score": 0, "social_sentiment_label": "NEUTRAL",
                "combined_sentiment_score": 0, "combined_sentiment_label": "NEUTRAL",
                "key_catalyst": None, "risk_flag": "Research error",
                "divergence_alert": None, "summary": "Research unavailable.",
                "strategy": "No data — trade cautiously.",
            }
            stocktwits  = {}
            reddit_data = {}
            x_data      = {}

        report[symbol] = {
            "timestamp":               datetime.now(ET).isoformat(),
            # Combined (used by trading agent)
            "sentiment_score":         analysis.get("combined_sentiment_score", 0),
            "sentiment_label":         analysis.get("combined_sentiment_label", "NEUTRAL"),
            # Broken out for learning agent / journal
            "news_sentiment_score":    analysis.get("news_sentiment_score", 0),
            "news_sentiment_label":    analysis.get("news_sentiment_label", "NEUTRAL"),
            "social_sentiment_score":  analysis.get("social_sentiment_score", 0),
            "social_sentiment_label":  analysis.get("social_sentiment_label", "NEUTRAL"),
            # Signals
            "key_catalyst":            analysis.get("key_catalyst"),
            "risk_flag":               analysis.get("risk_flag"),
            "divergence_alert":        analysis.get("divergence_alert"),
            "summary":                 analysis.get("summary", ""),
            "strategy":                analysis.get("strategy", ""),
            # Raw social stats
            "stocktwits_message_count": stocktwits.get("message_count", 0),
            "stocktwits_bullish_pct":   stocktwits.get("bullish_pct"),
            "reddit_post_count":        reddit_data.get("post_count", 0),
            "x_tweet_count":            x_data.get("tweet_count", 0),
        }

        logger.info(
            f"    {symbol}: news={analysis.get('news_sentiment_label')}({analysis.get('news_sentiment_score'):+d}) | "
            f"social={analysis.get('social_sentiment_label')}({analysis.get('social_sentiment_score'):+d}) | "
            f"combined={analysis.get('combined_sentiment_label')}({analysis.get('combined_sentiment_score'):+d})"
            + (f" | ⚠️  {analysis.get('divergence_alert')}" if analysis.get("divergence_alert") else "")
        )

        # Flush partial results after each stock so we never lose all work on a crash
        with open(config.RESEARCH_REPORT_FILE, "w") as f:
            json.dump(report, f, indent=2)

        time.sleep(0.3)

    # ── Write __meta__ block ────────────────────────────────────────────────
    macro_shock_score  = _compute_macro_shock_score(market_news)
    sector_sentiment   = _compute_sector_sentiment(report)

    # _detect_capitulation_candidate requires drawdown_from_20d_high and obv_trend,
    # which need intraday bars — data not fetched at research time. Use sentiment as
    # a conservative proxy: trading_agent refines this with live OBV when it calls
    # _detect_capitulation_candidate with full indicator data during the trading cycle.
    capitulation_candidates = []
    from tier1_config import is_tier1
    for symbol, data in report.items():
        if is_tier1(symbol) and data.get("sentiment_score", 0) < -30:
            capitulation_candidates.append(symbol)

    report["__meta__"] = {
        "sector_sentiment":      sector_sentiment,
        "macro_shock_score":     macro_shock_score,
        "unscheduled_event_risk": macro_shock_score >= 60,
        "capitulation_candidates": capitulation_candidates,
        "timestamp":             datetime.now(ET).isoformat(),
    }

    with open(config.RESEARCH_REPORT_FILE, "w") as f:
        json.dump(report, f, indent=2)

    logger.info(
        f"Research report saved. macro_shock={macro_shock_score} | "
        f"sectors={list(sector_sentiment.keys())} | "
        f"capitulation_candidates={capitulation_candidates}"
    )
    return report


if __name__ == "__main__":
    import logging as _l
    _l.basicConfig(level=_l.INFO)
    wl = (
        [t.strip() for t in config.WATCH_LIST_FILE.read_text().splitlines() if t.strip()]
        if config.WATCH_LIST_FILE.exists()
        else ["CRDO", "TSLA", "AMD"]
    )
    run_research_agent(wl)
