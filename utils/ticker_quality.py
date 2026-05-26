"""
TickerQuality scorer — Rule 28 SoT for tier assignment and unlock evaluation.
Pure — no I/O. All inputs supplied by callers.
"""
from __future__ import annotations

from dataclasses import dataclass

from utils.regime_params import get_entry_params

TIERS = ("LOW", "MEDIUM", "HIGH", "CONVICTION")


@dataclass(frozen=True)
class TickerQuality:
    tier: str
    score: int
    unlocks: frozenset
    quality_gaps: tuple
    component_scores: dict


def _score_to_tier(score: int) -> str:
    if score >= 80:
        return "CONVICTION"
    if score >= 60:
        return "HIGH"
    if score >= 35:
        return "MEDIUM"
    return "LOW"


def derive_mode(
    symbol: str,
    pead_ok: bool,
    existing_swing_pos: bool = False,
    catalyst_active: bool = False,
) -> str:
    """Derive trading mode — single SoT (Rule 32). Priority: SWING > PEAD > CATALYST > DAY_TRADE."""
    if existing_swing_pos:
        return "SWING"
    if pead_ok:
        return "PEAD"
    if catalyst_active:
        return "CATALYST"
    return "DAY_TRADE"


# ---------------------------------------------------------------------------
# Component scorers (max points noted next to each)
# ---------------------------------------------------------------------------

def _score_composite(composite: float, floor: int) -> tuple[int, str | None]:
    """Max 25 pts — composite signal strength above the regime floor."""
    pts = int(min(25, max(0, (composite - floor) / 50 * 25)))
    gap = f"composite_weak(-{25 - pts})" if pts < 10 else None
    return pts, gap


def _score_sentiment(sentiment: int) -> tuple[int, str | None]:
    """Max 20 pts — research/pre-market sentiment score."""
    if sentiment >= 60:
        pts = 20
    elif sentiment >= 30:
        pts = 14
    elif sentiment >= 0:
        pts = 8
    else:
        pts = 0
    gap = f"sentiment_weak(-{20 - pts})" if pts < 8 else None
    return pts, gap


def _score_intraday(intra_sent: int) -> tuple[int, str | None]:
    """Max 15 pts — intraday headline sentiment score."""
    if intra_sent >= 55:
        pts = 15
    elif intra_sent >= 25:
        pts = 10
    elif intra_sent >= 0:
        pts = 5
    else:
        pts = 0
    gap = f"intraday_weak(-{15 - pts})" if pts < 5 else None
    return pts, gap


def _score_sub_etf(sub_etf_chg: float) -> tuple[int, str | None]:
    """Max 15 pts — sub-sector ETF intraday % change (tailwind)."""
    if sub_etf_chg >= 2.0:
        pts = 15
    elif sub_etf_chg >= 1.0:
        pts = 10
    elif sub_etf_chg >= 0.0:
        pts = 5
    else:
        pts = 0
    gap = f"sector_weak(-{15 - pts})" if pts < 5 else None
    return pts, gap


def _score_pead(pead_ok: bool, pead_score: int) -> tuple[int, str | None]:
    """Max 10 pts — PEAD qualification bonus."""
    if not pead_ok:
        return 0, None
    pts = int(pead_score / 100 * 10)
    return pts, None


def _score_earnings(earnings_clear_days: int) -> tuple[int, str | None]:
    """Max 10 pts — how far away the next earnings event is."""
    if earnings_clear_days >= 30:
        pts = 10
    elif earnings_clear_days >= 21:
        pts = 8
    elif earnings_clear_days >= 14:
        pts = 6
    elif earnings_clear_days >= 7:
        pts = 3
    else:
        pts = 0
    gap = f"earnings_near(-{10 - pts})" if pts < 6 else None
    return pts, gap


def _score_sector_fred(sub_etf: str, fred_cache: dict) -> tuple[int, str | None]:
    """Max 5 pts — FRED macro headwind/tailwind for sub-sector ETF. Negatives clamped to 0."""
    if not sub_etf or not fred_cache:
        return 0, None
    direction = fred_cache.get("fed_direction", "unknown")
    inverted  = fred_cache.get("yield_curve_inverted", False)
    cpi_above = fred_cache.get("cpi_vs_target") == "above"

    TAILWINDS: dict[str, int] = {
        "XLF":  3 if direction == "hiking"  else 0,
        "XLU": -4 if direction == "hiking"  else 0,
        "XLRE":-4 if direction == "hiking"  else 0,
        "XLP":  2 if direction == "cutting" else 0,
        "XLK":  3 if direction == "cutting" else 0,
        "XLY":  2 if direction == "cutting" else 0,
        "XLE":  3 if cpi_above             else 0,
    }
    pts_raw = TAILWINDS.get(sub_etf, 2 if inverted and sub_etf == "GLD" else 0)
    pts     = max(0, min(5, pts_raw))
    gap     = f"sector_fred_headwind({pts_raw})" if pts_raw < -1 else None
    return pts, gap


def _score_econ_and_fred(
    event_active: bool, sub_etf: str, fred_cache: dict
) -> tuple[int, list[str]]:
    """Max 5 pts — economic calendar risk window + FRED sector tailwind."""
    if event_active:
        return 0, []
    pts, gap = _score_sector_fred(sub_etf, fred_cache)
    gaps = [gap] if gap else []
    return pts, gaps


def _is_fresh(ts_str: str | None, max_minutes: int) -> bool:
    if not ts_str:
        return False
    from datetime import datetime
    from utils.constants import ET
    try:
        ts = datetime.fromisoformat(ts_str).astimezone(ET)
        delta = (datetime.now(ET) - ts).total_seconds() / 60
        return delta <= max_minutes
    except (ValueError, TypeError):
        return False


def _compute_unlocks(
    tier: str,
    mode: str,
    pead_ok: bool,
    intra_sent: int,
    sentiment: int,
    earnings_clear_days: int,
    regime: str,
) -> frozenset:
    unlocks: set[str] = set()
    if tier == "CONVICTION":
        if pead_ok or intra_sent >= 60 or mode == "CATALYST":
            unlocks.add("T2_CHOPPY")
        if intra_sent >= 50 and earnings_clear_days >= 14:
            unlocks.add("SWING_CHOPPY")
        if pead_ok and regime in ("CAPITULATION", "CHOPPY"):
            unlocks.add("PEAD_EXTENDED_WINDOW")
    if tier in ("HIGH", "CONVICTION") and sentiment >= 40:
        unlocks.add("SWING_SENTIMENT")
    return frozenset(unlocks)


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------

def compute_ticker_quality(
    symbol: str,
    iv,
    snap,
    mode: str = "DAY_TRADE",
    sub_etf_chg: float = 0.0,
    earnings_clear_days: int = 30,
    pead_ok: bool = False,
    pead_score: int = 0,
    event_active: bool = False,
    fred_cache: dict | None = None,
) -> TickerQuality:
    """Compute a TickerQuality for *symbol* at the current evaluation instant.

    Parameters
    ----------
    symbol:              Ticker being evaluated.
    iv:                  IndicatorValues (or MagicMock in tests) for the symbol.
    snap:                CycleSnapshot (regime, research, intraday_news).
    mode:                "DAY_TRADE" | "PEAD" | "SWING" — use derive_mode() to obtain.
    sub_etf_chg:         Sub-sector ETF intraday % change (from monitor batch fetch).
    earnings_clear_days: Days until the next earnings event (≥0).
    pead_ok:             Whether the symbol qualifies as a PEAD setup (pre-computed by caller).
    pead_score:          PEAD score 0–100 (pre-computed by caller).
    event_active:        Whether an economic risk window is currently active (pre-computed by caller).
    fred_cache:          FRED macro state dict (fed_direction, yield_curve_inverted, cpi_vs_target).
    """
    import config as _config
    regime = snap.regime
    ep = get_entry_params(regime)

    research_sym = snap.research.get(symbol, {})
    sentiment: int = research_sym.get("sentiment_score", 0)
    intra_data = snap.intraday_news.get(symbol, {})
    intra_ts    = intra_data.get("timestamp")
    # treat missing timestamp as fresh (no staleness data = pass-through)
    intra_fresh = (intra_ts is None) or _is_fresh(intra_ts, _config.INTRADAY_NEWS_STALE_MINUTES)
    intra_sent: int = intra_data.get("sentiment_score", sentiment) if intra_fresh else 0

    from sectors import get_sub_sector_etf
    sub_etf = get_sub_sector_etf(symbol)

    c_pts, c_gap = _score_composite(iv.composite_score, ep.composite_min)
    s_pts, s_gap = _score_sentiment(sentiment)
    i_pts, i_gap = _score_intraday(intra_sent)
    e_pts, e_gap = _score_sub_etf(sub_etf_chg)
    p_pts, _     = _score_pead(pead_ok, pead_score)
    ec_pts, ec_gap = _score_earnings(earnings_clear_days)
    ev_pts, ev_gaps = _score_econ_and_fred(event_active, sub_etf, fred_cache or {})

    score = max(0, min(100, c_pts + s_pts + i_pts + e_pts + p_pts + ec_pts + ev_pts))
    tier = _score_to_tier(score)

    extra_gaps: list[str] = []
    if not intra_fresh and intra_ts:
        extra_gaps.append("intraday_stale")
    gaps = tuple(g for g in [c_gap, s_gap, i_gap, e_gap, ec_gap, *ev_gaps, *extra_gaps] if g)
    unlocks = _compute_unlocks(
        tier, mode, pead_ok, intra_sent, sentiment, earnings_clear_days, regime
    )

    return TickerQuality(
        tier=tier,
        score=score,
        unlocks=unlocks,
        quality_gaps=gaps,
        component_scores={
            "composite": c_pts,
            "sentiment": s_pts,
            "intraday": i_pts,
            "sub_etf": e_pts,
            "pead": p_pts,
            "earnings": ec_pts,
            "econ_fred": ev_pts,
        },
    )
