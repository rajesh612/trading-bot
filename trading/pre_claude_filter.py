"""
5-layer pre-Claude filter: routes each candidate to SKIP, AUTO_EXECUTE, or CLAUDE.
All data comes from CycleSnapshot — zero I/O per symbol.
"""
from __future__ import annotations

from dataclasses import dataclass

import config
from indicators.values import IndicatorValues
from signals.entry_gates import fast_gate
from trading.cycle_context import CycleSnapshot


@dataclass(frozen=True)
class FilterResult:
    decision: str   # "SKIP" | "AUTO_EXECUTE" | "CLAUDE"
    reason: str


def pre_claude_filter(symbol: str, iv: IndicatorValues, snap: CycleSnapshot) -> FilterResult:
    """
    Layer 1 — Composite minimum (regime-adjusted).
    Layer 2 — Technical fast gate (RSI, Stochastic, parabolic, stop-hunt).
    Layer 3 — Macro health score + morning shock score.
    Layer 4 — Research sentiment (9:15 AM report).
    Layer 5 — Intraday news (30-min cache; graceful fallback if empty).

    If all layers pass and signal is very strong → AUTO_EXECUTE (skip Claude).
    Otherwise → CLAUDE.
    """
    # Layer 1 — Composite minimum
    composite_min = (
        config.HIGH_VOL_COMPOSITE_MIN
        if snap.regime in ("HIGH_VOL", "DEFENSIVE")
        else config.PRE_CLAUDE_COMPOSITE_MIN
    )
    if iv.composite_score < composite_min:
        return FilterResult("SKIP",
            f"composite {iv.composite_score:+.1f} < {composite_min} ({snap.regime})")

    # Layer 2 — Technical fast gate
    fg = fast_gate(iv)
    if not fg.passed:
        return FilterResult("SKIP", fg.reason)

    # Layer 3 — Macro health + shock
    health = snap.regime_info.get("macro_health_score", 0)
    shock  = snap.research_meta.get("macro_shock_score", 0)
    if health < config.MACRO_HEALTH_SKIP_THRESHOLD:
        return FilterResult("SKIP",
            f"macro health {health} < {config.MACRO_HEALTH_SKIP_THRESHOLD}")
    if shock > config.MACRO_SHOCK_SKIP_THRESHOLD:
        return FilterResult("SKIP",
            f"macro shock {shock} > {config.MACRO_SHOCK_SKIP_THRESHOLD}")

    # Layer 4 — Research sentiment
    sentiment = snap.research.get(symbol, {}).get("sentiment_score", 0)
    if sentiment < config.SENTIMENT_HARD_BLOCK:
        return FilterResult("SKIP",
            f"sentiment {sentiment:+d} < hard block {config.SENTIMENT_HARD_BLOCK}")
    if sentiment < 0 and iv.composite_score < config.SENTIMENT_WEAK_COMPOSITE_MIN:
        return FilterResult("SKIP",
            f"negative sentiment ({sentiment:+d}) + composite {iv.composite_score:+.1f}"
            f" < {config.SENTIMENT_WEAK_COMPOSITE_MIN}")

    # Layer 5 — Intraday news (falls back to research sentiment if cache empty)
    intraday     = snap.intraday_news.get(symbol, {})
    intra_shock  = intraday.get("shock_score", 0)
    intra_sent   = intraday.get("sentiment_score", sentiment)  # fallback to research
    if intra_shock >= config.INTRADAY_NEWS_SHOCK_BLOCK:
        return FilterResult("SKIP",
            f"intraday shock {intra_shock} >= {config.INTRADAY_NEWS_SHOCK_BLOCK}")
    if intra_sent < config.INTRADAY_SENTIMENT_BLOCK:
        return FilterResult("SKIP",
            f"intraday sentiment {intra_sent:+d} < {config.INTRADAY_SENTIMENT_BLOCK}")

    # Auto-execute — all gates cleared, signal convincingly strong
    if (iv.composite_score > config.AUTO_EXECUTE_COMPOSITE_MIN
            and intra_sent > config.AUTO_EXECUTE_SENTIMENT_MIN):
        return FilterResult("AUTO_EXECUTE",
            f"composite={iv.composite_score:+.1f} intraday_sentiment={intra_sent:+d}")

    return FilterResult("CLAUDE", "")
