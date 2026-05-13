import json
import logging
import re

import anthropic

import config
from trading import risk_manager
from tier1_config import get_sector

logger = logging.getLogger(__name__)

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
    """True if drawdown <= -20%, sector sentiment < -20, and OBV rising (> 0.10)."""
    return drawdown_from_20d_high <= -20.0 and sector_sentiment < -20 and obv_trend > 0.10


def _quick_sentiment_result(stocktwits: dict, stock_news: list[dict]) -> dict | None:
    """
    Returns a filled analysis dict when StockTwits signal is unambiguous AND there
    is no stock-specific news that could override social sentiment.  Returns None
    when Claude is still needed (news present, weak signal, or insufficient data).

    Fires only when:
      - StockTwits is available with >= STOCKTWITS_MIN_TAGGED tagged messages
      - bullish_pct >= QUICK_SENTIMENT_BULL_PCT  ->  rule-based BULLISH
      - bullish_pct <= QUICK_SENTIMENT_BEAR_PCT  ->  rule-based BEARISH
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
            f"  [likes={t['likes']} RT={t['retweets']}] {t['text'][:120]}"
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

    try:
        usage = response.usage
        risk_manager.record_usage(
            input_tokens=getattr(usage, "input_tokens", 0),
            output_tokens=getattr(usage, "output_tokens", 0),
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0),
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0),
        )
    except Exception:
        pass

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
