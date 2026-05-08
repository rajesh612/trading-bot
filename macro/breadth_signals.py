"""
Market breadth scorer functions. All pure — no I/O, no API calls.
Called during the 8:55 AM macro regime fetch.
"""


def score_hyg(ret5d: float) -> float:
    """High-yield bond 5-day return → credit stress signal."""
    if ret5d < -1.5:
        return -10.0
    if ret5d < -0.5:
        return -5.0
    if ret5d > 0.5:
        return 8.0
    return 3.0


def score_iwm_vs_spy(iwm_ret: float, spy_ret: float) -> float:
    """Small-cap vs large-cap spread → risk appetite breadth."""
    spread = iwm_ret - spy_ret
    if spread < -1.5:
        return -10.0
    if spread < -0.5:
        return -5.0
    if spread > 1.0:
        return 8.0
    return 0.0


def score_vix_term_structure(vix_current: float, vix3m: float) -> float:
    """VIX / VIX3M ratio → backwardation (fear) vs contango (calm)."""
    if vix3m <= 0:
        return 0.0
    ratio = vix_current / vix3m
    if ratio > 1.1:
        return -10.0   # backwardation — immediate fear elevated
    if ratio > 1.02:
        return -5.0
    if ratio < 0.92:
        return 8.0    # contango — calm
    return 0.0


def build_breadth_context_line(signals: dict) -> str:
    """
    Returns a single-line breadth summary for the Claude context block.
    signals: {"hyg_ret5d": float, "iwm_ret5d": float, "spy_ret5d": float,
              "vix_current": float, "vix3m": float}
    """
    hyg  = signals.get("hyg_ret5d", 0.0)
    iwm  = signals.get("iwm_ret5d", 0.0)
    spy  = signals.get("spy_ret5d", 0.0)
    vix  = signals.get("vix_current", 0.0)
    vix3m = signals.get("vix3m", 0.0)

    spread = iwm - spy
    term_label = ""
    if vix3m > 0:
        ratio = vix / vix3m
        if ratio > 1.02:
            term_label = " VIX-term=backwardation"
        elif ratio < 0.92:
            term_label = " VIX-term=contango"

    return (
        f"Breadth: HYG={hyg:+.1f}% IWM-vs-SPY={spread:+.1f}%{term_label}"
    )
