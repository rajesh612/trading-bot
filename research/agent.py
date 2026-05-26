import logging
import threading
import time
from datetime import datetime
import anthropic

import config
import sectors
from utils.io_utils import save_json
from research.fetchers import (
    _fetch_market_news,
    _fetch_stock_news,
    _fetch_stocktwits,
    _fetch_reddit,
    _fetch_x_posts,
)
from research.analyzer import (
    _analyze_stock,
    _compute_macro_shock_score,
    _compute_sector_sentiment,
    _quick_sentiment_result,
)

from utils.constants import ET

logger = logging.getLogger(__name__)


def _score_capitulation_candidate(symbol: str, bars) -> bool:
    """True if symbol shows real capitulation: drawdown >= 15% from 20d high + OBV declining."""
    if bars is None or len(bars) < 5:
        return False
    high_20d = bars["high"].rolling(20).max().iloc[-1]
    current  = float(bars["close"].iloc[-1])
    drawdown = (high_20d - current) / high_20d if high_20d > 0 else 0.0
    obv      = (bars["close"].diff().apply(lambda x: 1 if x > 0 else -1) * bars["volume"]).cumsum()
    obv_trend_down = float(obv.iloc[-1]) < float(obv.iloc[-5])
    return drawdown >= 0.15 and obv_trend_down


def _fetch_daily_bars_for_cap_check(symbols: list[str]) -> dict:
    from data.market_data import get_daily_bars
    result = {}
    for sym in symbols:
        try:
            result[sym] = get_daily_bars(sym, days=20)
        except Exception:
            result[sym] = None
    return result

_RESEARCH_LOCK = threading.Lock()


def run_research_agent(watch_list: list[str]) -> dict:
    if not _RESEARCH_LOCK.acquire(blocking=False):
        logger.info("Research agent already running — skipping duplicate invocation.")
        return {}
    try:
        return _run_research_agent_impl(watch_list)
    finally:
        _RESEARCH_LOCK.release()


def _run_research_agent_impl(watch_list: list[str]) -> dict:
    from utils.data_health import _tracker
    from utils.io_utils import mark_stale, clear_stale
    try:
        if not watch_list:
            logger.warning("Watch list empty — skipping research.")
            return {}

        x_enabled = bool(config.X_BEARER_TOKEN)
        logger.info(
            f"Research agent: {len(watch_list)} stocks | "
            f"Reddit=yes (public JSON) | "
            f"X={'yes' if x_enabled else 'no (no token)'} | "
            f"StockTwits=yes"
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
                + (f" | {analysis.get('divergence_alert')}" if analysis.get("divergence_alert") else "")
            )

            # Flush partial results after each stock so we never lose all work on a crash
            save_json(config.RESEARCH_REPORT_FILE, report)

            time.sleep(0.3)

        # ── Write __meta__ block ────────────────────────────────────────────────
        macro_shock_score  = _compute_macro_shock_score(market_news)
        sector_sentiment   = _compute_sector_sentiment(report)

        # _detect_capitulation_candidate requires drawdown_from_20d_high and obv_trend,
        # which need intraday bars — data not fetched at research time. Use sentiment as
        # a conservative proxy: trading_agent refines this with live OBV when it calls
        # _detect_capitulation_candidate with full indicator data during the trading cycle.
        capitulation_candidates = []
        for symbol, data in report.items():
            if sectors.is_tier1(symbol) and data.get("sentiment_score", 0) < -30:
                capitulation_candidates.append(symbol)

        report["__meta__"] = {
            "sector_sentiment":      sector_sentiment,
            "macro_shock_score":     macro_shock_score,
            "unscheduled_event_risk": macro_shock_score >= 60,
            "capitulation_candidates": capitulation_candidates,
            "timestamp":             datetime.now(ET).isoformat(),
        }

        save_json(config.RESEARCH_REPORT_FILE, report)

        logger.info(
            f"Research report saved. macro_shock={macro_shock_score} | "
            f"sectors={list(sector_sentiment.keys())} | "
            f"capitulation_candidates={capitulation_candidates}"
        )
        clear_stale(config.RESEARCH_REPORT_FILE)
        _tracker.record_success("research")
        return report
    except Exception as exc:
        logger.error("run_research_agent failed: %s", exc)
        from utils.io_utils import load_json as _load_json
        prev = _load_json(config.RESEARCH_REPORT_FILE, {})
        mark_stale(config.RESEARCH_REPORT_FILE, "research")
        from scheduler_jobs import _schedule_retry
        _schedule_retry("research", lambda: run_research_agent(watch_list))
        _tracker.record_failure("research")
        return prev
