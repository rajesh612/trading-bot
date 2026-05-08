"""
Macro health score computation. All functions are pure — no file I/O.
Weights updated 2026-05-07: HYG=0.12, IWM=0.08, VIX_term=0.06 (sum=1.0).
"""
from macro.breadth_signals import score_hyg, score_iwm_vs_spy, score_vix_term_structure


def _score_vix(vix_closes: list[float]) -> float:
    """VIX vs 90d avg: < 90% of avg → +10, 90–120% → linear, > 120% → -10."""
    if len(vix_closes) < 10:
        return 0.0
    lookback = min(90, len(vix_closes) - 1)
    avg = sum(vix_closes[-lookback - 1:-1]) / lookback
    if avg <= 0:
        return 0.0
    ratio = vix_closes[-1] / avg
    if ratio < 0.9:
        return 10.0
    elif ratio > 1.2:
        return -10.0
    else:
        return 10.0 - (ratio - 0.9) / 0.3 * 20.0


def _score_spy(ret5d: float) -> float:
    """SPY 5-day return: > +1.5% → +10, -0.5% to +1.5% → linear, < -0.5% → -10."""
    if ret5d > 1.5:
        return 10.0
    elif ret5d >= -0.5:
        return (ret5d + 0.5) / 2.0 * 10.0 - 2.5
    else:
        return -10.0


def _score_wti(ret5d: float) -> float:
    """WTI Oil: stable ±2% → +10, ±2–5% → 0, spike > +5% or crash < -8% → -10."""
    if abs(ret5d) <= 2.0:
        return 10.0
    elif ret5d > 5.0 or ret5d < -8.0:
        return -10.0
    else:
        return 0.0


def _score_gold(ret5d: float) -> float:
    """Gold: flat/mild → +10, surge > +2% (flight to safety) → -10."""
    if ret5d > 3.0:
        return -10.0
    elif ret5d > 2.0:
        return -5.0
    elif ret5d < -2.0:
        return +5.0  # declining gold = risk-on
    else:
        return +8.0  # flat/mild = bullish signal


def _score_dxy(ret5d: float) -> float:
    """DXY: mild/stable → +5, sharp rise → -10 (risk-off)."""
    if ret5d > 2.0:
        return -10.0
    elif ret5d > 0.5:
        return -5.0
    elif ret5d < -0.5:
        return +5.0
    else:
        return +3.0


def _score_tnx(ret5d: float) -> float:
    """10Y yield: declining/stable → +10, rapid rise → -10."""
    if ret5d > 5.0:
        return -10.0
    elif ret5d > 2.0:
        return -5.0
    elif ret5d < -2.0:
        return +10.0
    else:
        return +3.0


def _score_tech(ret5d: float, bull_thresh: float, bear_thresh: float) -> float:
    """Generic tech/sector ETF scorer."""
    if ret5d > bull_thresh:
        return +10.0
    elif ret5d < bear_thresh:
        return -10.0
    elif ret5d >= 0:
        return ret5d / bull_thresh * 10.0
    else:
        return ret5d / abs(bear_thresh) * 10.0


_WEIGHTS = {
    "vix":      0.15,
    "spy":      0.17,
    "hyg":      0.12,
    "soxx":     0.12,
    "iwm_spy":  0.08,
    "qqq":      0.08,
    "vix_term": 0.06,
    "wti":      0.07,
    "tnx":      0.07,
    "gold":     0.05,
    "dxy":      0.03,
}


def compute_macro_health_score(signals: dict, fred_modifier: int = 0) -> int:
    """
    Compute macro_health_score in [-100, 100] from Yahoo signals + FRED modifier.
    signals: {vix_closes, spy_ret5d, wti_ret5d, gold_ret5d, dxy_ret5d,
              tnx_ret5d, soxx_ret5d, qqq_ret5d,
              hyg_ret5d, iwm_ret5d, vix_current, vix3m}
    Weights sum to 1.0; breadth signals added 2026-05-07.
    """
    raw_scores = {
        "vix":      _score_vix(signals.get("vix_closes", [])),
        "spy":      _score_spy(signals.get("spy_ret5d", 0.0)),
        "hyg":      score_hyg(signals.get("hyg_ret5d", 0.0)),
        "soxx":     _score_tech(signals.get("soxx_ret5d", 0.0), 2.0, -1.0),
        "iwm_spy":  score_iwm_vs_spy(signals.get("iwm_ret5d", 0.0), signals.get("spy_ret5d", 0.0)),
        "qqq":      _score_tech(signals.get("qqq_ret5d", 0.0), 1.5, -1.0),
        "vix_term": score_vix_term_structure(signals.get("vix_current", 0.0), signals.get("vix3m", 0.0)),
        "wti":      _score_wti(signals.get("wti_ret5d", 0.0)),
        "tnx":      _score_tnx(signals.get("tnx_ret5d", 0.0)),
        "gold":     _score_gold(signals.get("gold_ret5d", 0.0)),
        "dxy":      _score_dxy(signals.get("dxy_ret5d", 0.0)),
    }

    weighted_sum = sum(_WEIGHTS[k] * raw_scores[k] for k in _WEIGHTS)
    # weighted_sum is in [-10, +10]; scale to [-100, +100]
    score = weighted_sum * 10.0 + fred_modifier
    return int(max(-100, min(100, round(score))))
