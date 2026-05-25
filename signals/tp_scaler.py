"""
Time-aware TP/SL scaler — Rule 30 SoT for all TP/SL computation.
Uses TPSLParams from regime_params. Pure — no I/O.
"""
from __future__ import annotations
from datetime import datetime

import config
from utils.regime_params import get_tp_sl_params


def compute_time_multiplier(entry_time: datetime) -> float:
    """Return a TP multiplier based on time of day (ET). Later = tighter."""
    total_min = entry_time.hour * 60 + entry_time.minute
    if total_min < 12 * 60:
        return 1.00
    if total_min < 13 * 60 + 30:
        return 0.80
    if total_min < 14 * 60 + 30:
        return 0.65
    return 0.50


def compute_tp_sl(
    base_tp: float,
    base_sl: float,
    entry_time: datetime,
    tier: str,
    regime: str,
    mode: str = "DAY_TRADE",
) -> tuple[float, float]:
    """
    Compute final TP% and SL% for an entry.

    base_tp is the ATR-derived suggestion. The table's atr_mult replaces
    the global ATR_TP_MULT_DEFAULT. Time-decay is applied after ATR scaling;
    result is clamped to regime×tier×mode floor/ceiling, not global constants.
    """
    p = get_tp_sl_params(regime, tier, mode)
    if p.is_disabled:
        raise ValueError(
            f"compute_tp_sl called for disabled combo ({regime}, {tier}, {mode})"
        )
    time_mult = compute_time_multiplier(entry_time)
    raw_tp    = base_tp * p.atr_mult * time_mult
    return round(p.clamp_tp(raw_tp), 2), round(p.clamp_sl(base_sl), 2)
