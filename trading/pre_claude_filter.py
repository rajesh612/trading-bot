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
from utils.ticker_quality import TickerQuality, compute_ticker_quality, derive_mode
from utils.regime_params import get_filter_params, get_auto_execute_params, get_entry_params


@dataclass(frozen=True)
class FilterResult:
    decision: str   # "SKIP" | "AUTO_EXECUTE" | "CLAUDE"
    reason: str
    quality: TickerQuality | None = field(default=None)
    gate_result: GateResult | None = field(default=None)


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
    catalyst_active: bool = False,
) -> FilterResult:
    """Pre-Claude filter with dynamic FilterParams per regime×tier and quality scoring.

    Computes TickerQuality first, then uses tier-adjusted thresholds via get_filter_params().
    """
    mode = derive_mode(symbol, pead_ok, existing_swing_pos=False, catalyst_active=catalyst_active)

    # Layer 0 — Already closed today
    if daily_closed and symbol in daily_closed:
        quality = compute_ticker_quality(symbol, iv, snap, mode,
                                         sub_etf_chg, earnings_clear_days,
                                         pead_ok, pead_score, event_active)
        return FilterResult("SKIP", "already closed today", quality)

    # Compute quality first — tier drives all subsequent thresholds
    quality = compute_ticker_quality(symbol, iv, snap, mode,
                                     sub_etf_chg, earnings_clear_days,
                                     pead_ok, pead_score, event_active)
    fp = get_filter_params(snap.regime, quality.tier)

    # Layer 1 — Composite minimum (higher of regime gate and tier-adjusted floor)
    eff_gate = effective_composite_gate(
        iv, snap.regime, params=snap.params,
        staleness_guard=snap.staleness_guard,
    )
    composite_min = max(eff_gate, fp.composite_min)
    if iv.composite_score < composite_min:
        return FilterResult("SKIP",
            f"composite {iv.composite_score:+.1f} < {composite_min}"
            f" ({snap.regime} tier={quality.tier})", quality)

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

    # Auto-execute — dynamic 9×4 table (regime × tier); disabled entries have composite_offset=999
    ae = get_auto_execute_params(snap.regime, quality.tier)
    if ae.composite_offset != 999:
        ep = get_entry_params(snap.regime)
        ae_threshold = ep.composite_min + ae.composite_offset
        ae_sentiment_ok = intra_sent > ae.sentiment_min
        if ae.extra_unlock == "SUB_ETF_CONFIRM_03":
            ae_unlock_ok = sub_etf_chg >= config.SUB_ETF_CONFIRM_MIN_03
        elif ae.extra_unlock == "SUB_ETF_CONFIRM_05":
            ae_unlock_ok = sub_etf_chg >= config.SUB_ETF_CONFIRM_MIN_05
        elif ae.extra_unlock is not None:
            ae_unlock_ok = ae.extra_unlock in quality.unlocks
        else:
            ae_unlock_ok = True
        if iv.composite_score > ae_threshold and ae_sentiment_ok and ae_unlock_ok:
            return FilterResult("AUTO_EXECUTE",
                f"composite={iv.composite_score:+.1f} intraday_sentiment={intra_sent:+d}"
                f" tier={quality.tier}", quality)

    return FilterResult("CLAUDE", "", quality)
