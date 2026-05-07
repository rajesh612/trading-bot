"""
Multi-timeframe confirmation filter using 15-min EMA(9) > EMA(21).

Used in the BUY path to confirm longer-term trend alignment before entry.
"""
from __future__ import annotations
import pandas as pd


def passes_mtf_filter(bars_15min: pd.DataFrame | None) -> bool:
    """
    True if 15-min EMA(9) > EMA(21) for long entry confirmation.
    Returns True (pass) when insufficient data — fail-safe allows entries
    when we cannot fetch multi-timeframe data.
    """
    if bars_15min is None or len(bars_15min) < 21:
        return True  # insufficient data → don't block

    close = bars_15min["close"] if "close" in bars_15min.columns else bars_15min.iloc[:, 3]
    ema9 = close.ewm(span=9, adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()

    return float(ema9.iloc[-1]) > float(ema21.iloc[-1])
