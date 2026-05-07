"""
Relative Strength (RS) filter: compare symbol 5-day return against SPY.
RS ratio = symbol_ret5d / spy_ret5d. Blocks underperformers in uptrends.
"""
from __future__ import annotations
import config


def rs_rank(symbol: str, spy_ret5d: float, symbol_ret5d: float) -> float:
    """
    Returns RS ratio: symbol_ret5d / spy_ret5d.
    > 1.0 means symbol outperforming SPY.
    When SPY is flat (spy_ret5d == 0.0), returns neutral 1.0.
    """
    if spy_ret5d == 0.0:
        return 1.0  # neutral when SPY is flat
    return symbol_ret5d / spy_ret5d


def passes_rs_filter(rs_ratio: float, threshold: float = None) -> bool:
    """
    True if symbol RS ratio >= RS_MIN_RATIO threshold.

    Args:
        rs_ratio: computed RS rank (symbol_ret5d / spy_ret5d)
        threshold: RS ratio floor; defaults to config.RS_MIN_RATIO

    Returns:
        True if rs_ratio passes filter (>= threshold), False otherwise.
    """
    if threshold is None:
        threshold = config.RS_MIN_RATIO
    return rs_ratio >= threshold
