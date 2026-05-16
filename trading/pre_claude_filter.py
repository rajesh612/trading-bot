"""
5-layer pre-Claude filter: routes each candidate to SKIP, AUTO_EXECUTE, or CLAUDE.
All data comes from CycleSnapshot — zero I/O per symbol.
"""
from __future__ import annotations

from dataclasses import dataclass

import config
from indicators.values import IndicatorValues
from trading.cycle_context import CycleSnapshot
from signals.entry_gates import effective_composite_gate
from signals.vwap_guard import passes_vwap_guard


@dataclass(frozen=True)
class FilterResult:
    decision: str   # "SKIP" | "AUTO_EXECUTE" | "CLAUDE"
    reason: str


def pre_claude_filter(
    symbol: str,
    iv: IndicatorValues,
    snap: CycleSnapshot,
    daily_closed: set[str] | None = None,
) -> FilterResult:
    """
    Layer 0 — Already closed today.
    Layer 1 — Composite minimum (regime-adjusted via effective_composite_gate).
    Layer 2 — RSI maximum (regime-adjusted, same constant as executor).
    Layer 2.5 — VWAP: slope downtrend + price overextension.
    Layer 3 — Macro health score + morning shock score.
    Layer 4 — Research sentiment (9:15 AM report).
    Layer 5 — Intraday news (30-min cache; graceful fallback if empty).

    If all layers pass and signal is very strong → AUTO_EXECUTE (skip Claude).
    Otherwise → CLAUDE.
    """
    # Layer 0 — Already closed today
    if daily_closed and symbol in daily_closed:
        return FilterResult("SKIP", "already closed today")

    # Layer 1 — Composite minimum (unified with executor via effective_composite_gate)
    composite_min = effective_composite_gate(iv, snap.regime)
    if iv.composite_score < composite_min:
        return FilterResult("SKIP",
            f"composite {iv.composite_score:+.1f} < {composite_min} ({snap.regime})")

    # Layer 2 — RSI maximum (must match executor rsi_max exactly — uses learned threshold)
    rsi_max = (
        snap.params.get("rsi_buy_max_high_vol", config.RSI_BUY_MAX_HIGH_VOL)
        if snap.regime in ("HIGH_VOL", "CRISIS", "DEFENSIVE")
        else snap.params.get("rsi_buy_max", config.RSI_BUY_MAX)
    )
    if iv.rsi > rsi_max:
        return FilterResult("SKIP",
            f"RSI {iv.rsi:.1f} > {rsi_max} ({snap.regime})")

    # Layer 2.1 — Stochastic overbought (mirrors executor gate; avoids burning budget on guaranteed HOLDs)
    if iv.stoch_d > config.STOCH_HARD_VETO_THRESHOLD:
        return FilterResult("SKIP",
            f"Stochastic {iv.stoch_d:.1f} > {config.STOCH_HARD_VETO_THRESHOLD}")

    # Layer 2.5 — VWAP: slope downtrend + price overextension (same criteria as executor)
    vwap_slope_min = snap.params.get("vwap_slope_min", config.VWAP_SLOPE_MIN)
    if iv.vwap_slope < vwap_slope_min:
        return FilterResult("SKIP",
            f"VWAP slope {iv.vwap_slope:.3f} < {vwap_slope_min:.3f} (downtrend)")
    if not passes_vwap_guard(iv):
        return FilterResult("SKIP", "VWAP overextension (price > VWAP+2σ)")

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
