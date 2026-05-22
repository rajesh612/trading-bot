"""
5-layer pre-Claude quality filter: routes each candidate to SKIP, AUTO_EXECUTE, or CLAUDE.
All data comes from CycleSnapshot — zero I/O per symbol.

OPTION A: This filter is a quality/budget router ONLY.
Safety gates (time window, earnings, event risk, Stoch veto, parabolic, herding,
gap-skip) are enforced exclusively by check_all_gates() inside execute_buy().
Do NOT add safety rules here — they duplicate check_all_gates and go stale independently.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import config
from indicators.values import IndicatorValues
from trading.cycle_context import CycleSnapshot
from signals.entry_gates import effective_composite_gate, GateResult
from utils import regimes
from utils.ticker_quality import TickerQuality, compute_ticker_quality
from utils.regime_params import get_filter_params


@dataclass(frozen=True)
class FilterResult:
    decision: str   # "SKIP" | "AUTO_EXECUTE" | "CLAUDE"
    reason: str
    quality: TickerQuality | None = field(default=None)
    gate_result: GateResult | None = field(default=None)


def pre_claude_filter(
    symbol: str,
    iv: IndicatorValues,
    snap: CycleSnapshot,
    daily_closed: set[str] | None = None,
) -> FilterResult:
    """
    Layer 0 — Already closed today.
    Layer 1 — Composite minimum (regime-adjusted).
    Layer 2 — RSI maximum (regime-adjusted; saves tokens on guaranteed HOLDs).
    Layer 2.5 — VWAP slope downtrend (trend quality signal, not safety gate).
    Layer 3 — Macro health score + morning shock score.
    Layer 4 — Research sentiment (9:15 AM report).
    Layer 5 — Intraday news shock + sentiment (30-min cache).
    """
    # Layer 0 — Already closed today
    if daily_closed and symbol in daily_closed:
        return FilterResult("SKIP", "already closed today")

    # Layer 1 — Composite minimum (regime-adjusted)
    composite_min = effective_composite_gate(
        iv, snap.regime, params=snap.params,
        staleness_guard=snap.staleness_guard,
    )
    if iv.composite_score < composite_min:
        return FilterResult("SKIP",
            f"composite {iv.composite_score:+.1f} < {composite_min} ({snap.regime})")

    # Layer 2 — RSI maximum (quality filter: don't call Claude if RSI is clearly too high)
    rsi_max = (
        snap.params.get("rsi_buy_max_high_vol", config.RSI_BUY_MAX_HIGH_VOL)
        if snap.regime in regimes.HIGH_VOL_GATES | {regimes.CRISIS}
        else snap.params.get("rsi_buy_max", config.RSI_BUY_MAX)
    )
    if iv.rsi > rsi_max:
        return FilterResult("SKIP",
            f"RSI {iv.rsi:.1f} > {rsi_max} ({snap.regime})")

    # Layer 1.5 — Regime-adaptive directional bias (CHOPPY RSI pullback only)
    if config.REGIME_ADAPTIVE_LOGIC:
        from utils.regime_params import get_entry_params
        ep = get_entry_params(snap.regime)
        if snap.regime == "CHOPPY":
            choppy_rsi_max = snap.params.get("choppy_rsi_max", ep.rsi_max)
            if iv.rsi > choppy_rsi_max:
                return FilterResult("SKIP",
                    f"choppy regime: RSI {iv.rsi:.1f} > pullback max {choppy_rsi_max}")

    # Layer 2.5 — VWAP slope downtrend (trend direction quality signal)
    vwap_slope_min = snap.params.get("vwap_slope_min", config.VWAP_SLOPE_MIN)
    if iv.vwap_slope < vwap_slope_min:
        return FilterResult("SKIP",
            f"VWAP slope {iv.vwap_slope:.3f} < {vwap_slope_min:.3f} (downtrend)")

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
    intraday    = snap.intraday_news.get(symbol, {})
    intra_shock = intraday.get("shock_score", 0)
    intra_sent  = intraday.get("sentiment_score", sentiment)  # fallback to research
    if intra_shock >= config.INTRADAY_NEWS_SHOCK_BLOCK:
        return FilterResult("SKIP",
            f"intraday shock {intra_shock} >= {config.INTRADAY_NEWS_SHOCK_BLOCK}")
    if intra_sent < config.INTRADAY_SENTIMENT_BLOCK:
        return FilterResult("SKIP",
            f"intraday sentiment {intra_sent:+d} < {config.INTRADAY_SENTIMENT_BLOCK}")

    # Auto-execute — all quality layers cleared, signal convincingly strong
    if (iv.composite_score > config.AUTO_EXECUTE_COMPOSITE_MIN
            and intra_sent > config.AUTO_EXECUTE_SENTIMENT_MIN):
        return FilterResult("AUTO_EXECUTE",
            f"composite={iv.composite_score:+.1f} intraday_sentiment={intra_sent:+d}")

    return FilterResult("CLAUDE", "")


def apply_pre_claude_filter(
    symbol: str,
    iv,
    snap,
    daily_closed: set[str] | None = None,
    sub_etf_chg: float = 0.0,
    earnings_clear_days: int = 30,
    pead_ok: bool = False,
    pead_score: int = 0,
    event_active: bool = False,
) -> FilterResult:
    """Pre-Claude filter with dynamic FilterParams per regime×tier and quality scoring.

    Computes TickerQuality first, then uses tier-adjusted thresholds via get_filter_params().
    """
    # Layer 0 — Already closed today
    if daily_closed and symbol in daily_closed:
        quality = compute_ticker_quality(symbol, iv, snap, "DAY_TRADE",
                                         sub_etf_chg, earnings_clear_days,
                                         pead_ok, pead_score, event_active)
        return FilterResult("SKIP", "already closed today", quality)

    # Compute quality first — tier drives all subsequent thresholds
    quality = compute_ticker_quality(symbol, iv, snap, "DAY_TRADE",
                                     sub_etf_chg, earnings_clear_days,
                                     pead_ok, pead_score, event_active)
    fp = get_filter_params(snap.regime, quality.tier)

    # Layer 1 — Composite minimum (tier-adjusted, regime-specific)
    composite_min = effective_composite_gate(
        iv, snap.regime, params=snap.params,
        staleness_guard=snap.staleness_guard,
    )
    if iv.composite_score < composite_min:
        return FilterResult("SKIP",
            f"composite {iv.composite_score:+.1f} < {composite_min} ({snap.regime})", quality)

    # Layer 2 — RSI maximum (regime-adjusted)
    rsi_max = (
        snap.params.get("rsi_buy_max_high_vol", config.RSI_BUY_MAX_HIGH_VOL)
        if snap.regime in regimes.HIGH_VOL_GATES | {regimes.CRISIS}
        else snap.params.get("rsi_buy_max", config.RSI_BUY_MAX)
    )
    if iv.rsi > rsi_max:
        return FilterResult("SKIP",
            f"RSI {iv.rsi:.1f} > {rsi_max} ({snap.regime})", quality)

    # Layer 1.5 — Regime-adaptive directional bias (CHOPPY RSI pullback only)
    if config.REGIME_ADAPTIVE_LOGIC and snap.regime == "CHOPPY":
        from utils.regime_params import get_entry_params
        ep = get_entry_params(snap.regime)
        choppy_rsi_max = snap.params.get("choppy_rsi_max", ep.rsi_max)
        if iv.rsi > choppy_rsi_max:
            return FilterResult("SKIP",
                f"choppy regime: RSI {iv.rsi:.1f} > pullback max {choppy_rsi_max}", quality)

    # Layer 2.5 — VWAP slope downtrend
    vwap_slope_min = snap.params.get("vwap_slope_min", config.VWAP_SLOPE_MIN)
    if iv.vwap_slope < vwap_slope_min:
        return FilterResult("SKIP",
            f"VWAP slope {iv.vwap_slope:.3f} < {vwap_slope_min:.3f} (downtrend)", quality)

    # Layer 3 — Macro health + shock
    health = snap.regime_info.get("macro_health_score", 0)
    shock  = snap.research_meta.get("macro_shock_score", 0)
    if health < config.MACRO_HEALTH_SKIP_THRESHOLD:
        return FilterResult("SKIP",
            f"macro health {health} < {config.MACRO_HEALTH_SKIP_THRESHOLD}", quality)
    if shock > config.MACRO_SHOCK_SKIP_THRESHOLD:
        return FilterResult("SKIP",
            f"macro shock {shock} > {config.MACRO_SHOCK_SKIP_THRESHOLD}", quality)

    # Layer 4 — Research sentiment (tier-adjusted via FilterParams)
    sentiment = snap.research.get(symbol, {}).get("sentiment_score", 0)
    if sentiment < fp.sentiment_hard_block:
        return FilterResult("SKIP",
            f"sentiment {sentiment:+d} < hard block {fp.sentiment_hard_block}"
            f" (tier={quality.tier})", quality)
    if sentiment < 0 and iv.composite_score < config.SENTIMENT_WEAK_COMPOSITE_MIN:
        return FilterResult("SKIP",
            f"negative sentiment ({sentiment:+d}) + composite {iv.composite_score:+.1f}"
            f" < {config.SENTIMENT_WEAK_COMPOSITE_MIN}", quality)

    # Layer 5 — Intraday news (tier-adjusted via FilterParams)
    intraday    = snap.intraday_news.get(symbol, {})
    intra_shock = intraday.get("shock_score", 0)
    intra_sent  = intraday.get("sentiment_score", sentiment)
    if intra_shock >= config.INTRADAY_NEWS_SHOCK_BLOCK:
        return FilterResult("SKIP",
            f"intraday shock {intra_shock} >= {config.INTRADAY_NEWS_SHOCK_BLOCK}", quality)
    if intra_sent < fp.intraday_block:
        return FilterResult("SKIP",
            f"intraday sentiment {intra_sent:+d} < {fp.intraday_block}"
            f" (tier={quality.tier})", quality)

    # Auto-execute — all quality layers cleared, signal convincingly strong
    if (iv.composite_score > config.AUTO_EXECUTE_COMPOSITE_MIN
            and intra_sent > config.AUTO_EXECUTE_SENTIMENT_MIN):
        return FilterResult("AUTO_EXECUTE",
            f"composite={iv.composite_score:+.1f} intraday_sentiment={intra_sent:+d}", quality)

    return FilterResult("CLAUDE", "", quality)
