"""
Single source of truth for regime × mode dispatch.

All per-regime entry, swing, and T2 parameters are defined here.
Callers use get_entry_params(), get_swing_params(), get_t2_params()
instead of inline if/elif chains.
"""
from __future__ import annotations

from dataclasses import dataclass

import config


@dataclass(frozen=True)
class EntryParams:
    composite_min: int
    rsi_max: int
    entry_allowed: bool
    position_mult: float


@dataclass(frozen=True)
class SwingParams:
    eligible: bool
    min_pnl_pct: float
    profit_lock_pct: float
    sentiment_min: float
    sector_floor_pct: float
    tp1_pct: float
    tp2_pct: float
    sl_pct: float


@dataclass(frozen=True)
class T2Params:
    cutoff_hour: int
    drop_min_pct: float
    drop_max_pct: float
    rsi_max: int
    composite_min: int
    sentiment_min: float
    sector_floor_pct: float
    min_hold_min: int


def _entry_table() -> dict[str, EntryParams]:
    defensive_composite = int(config.COMPOSITE_MIN_HIGH_VOL * config.DEFENSIVE_COMPOSITE_MULT)
    return {
        "CRISIS":         EntryParams(config.COMPOSITE_MIN_NORMAL,    config.RSI_BUY_MAX,         False, 0.0),
        "NORMAL":         EntryParams(config.COMPOSITE_MIN_NORMAL,    config.RSI_BUY_MAX,         True,  1.0),
        "BULL_TREND":     EntryParams(config.COMPOSITE_MIN_BULL_TREND, config.RSI_BUY_MAX,        True,  1.0),
        "CHOPPY":         EntryParams(config.CHOPPY_COMPOSITE_GATE,   config.CHOPPY_RSI_PULLBACK_MAX, True, 0.5),
        "DEFENSIVE":      EntryParams(defensive_composite,            config.RSI_BUY_MAX_HIGH_VOL, True, 0.5),
        "HIGH_VOL":       EntryParams(config.COMPOSITE_MIN_HIGH_VOL,  config.RSI_BUY_MAX_HIGH_VOL, True, 0.5),
        "OPPORTUNITY":    EntryParams(config.COMPOSITE_MIN_NORMAL,    config.RSI_BUY_MAX,         True,  1.0),
        "CRASH_RECOVERY": EntryParams(config.COMPOSITE_MIN_NORMAL,    config.RSI_BUY_MAX,         True,  1.0),
        "CAPITULATION":   EntryParams(config.COMPOSITE_MIN_NORMAL,    config.RSI_BUY_MAX,         True,  1.0),
    }


def _swing_table() -> dict[str, SwingParams]:
    return {
        "NORMAL":         SwingParams(True,  0.5,  2.5,  -20, -1.0, config.SWING_TP1_PCT, config.SWING_TP2_PCT, config.SWING_OVERNIGHT_SL_PCT),
        "BULL_TREND":     SwingParams(True,  0.3,  2.0,  -30, -2.0, config.SWING_TP1_PCT, config.SWING_TP2_PCT, config.SWING_OVERNIGHT_SL_PCT),
        "OPPORTUNITY":    SwingParams(True,  0.5,  2.5,  -10,  0.0, config.SWING_TP1_PCT, config.SWING_TP2_PCT, config.SWING_OVERNIGHT_SL_PCT),
        "CRASH_RECOVERY": SwingParams(True,  1.0,  2.5,    0,  0.0, config.SWING_TP1_PCT, config.SWING_TP2_PCT, config.SWING_OVERNIGHT_SL_PCT),
        "CAPITULATION":   SwingParams(True,  config.SWING_MIN_PNL_PCT_CAPITULATION, 50.0, -50, -5.0, config.SWING_TP1_PCT_CAPITULATION, config.SWING_TP2_PCT_CAPITULATION, config.SWING_SL_PCT_CAPITULATION),
        "CHOPPY":         SwingParams(False, 0.5,  2.5,   10,  0.5, config.SWING_TP1_PCT, config.SWING_TP2_PCT, config.SWING_OVERNIGHT_SL_PCT),
        "DEFENSIVE":      SwingParams(False, 0.5,  3.0,    0,  0.0, config.SWING_TP1_PCT, config.SWING_TP2_PCT, config.SWING_OVERNIGHT_SL_PCT),
        "HIGH_VOL":       SwingParams(True,  0.5,  3.0,  -10, -1.0, config.SWING_TP1_PCT, config.SWING_TP2_PCT, config.SWING_OVERNIGHT_SL_PCT),
        "CRISIS":         SwingParams(False, 999,  999,  999, 999,   0,                    0,                    0),
    }


def _t2_table() -> dict[str, T2Params]:
    _disabled = T2Params(0, 0, 0, 0, 0, 999, 999, 999)
    _normal = T2Params(
        config.TRANCHE2_CUTOFF_HOUR,
        config.TRANCHE2_DROP_MIN_PCT,
        config.TRANCHE2_DROP_MAX_PCT,
        config.TRANCHE2_RSI_MAX,
        config.TRANCHE2_COMPOSITE_MIN,
        config.TRANCHE2_MIN_SENTIMENT,
        config.TRANCHE2_SECTOR_WEAK_PCT,
        config.TRANCHE2_MIN_HOLD_MIN,
    )
    return {
        "CRISIS":         _disabled,
        "CHOPPY":         _disabled,
        "NORMAL":         _normal,
        "BULL_TREND":     _normal,
        "OPPORTUNITY":    _normal,
        "CRASH_RECOVERY": _normal,
        "CAPITULATION":   _normal,
        "DEFENSIVE":      _normal,
        "HIGH_VOL":       _normal,
    }


def get_entry_params(regime: str) -> EntryParams:
    return _entry_table().get(regime, _entry_table()["NORMAL"])


def get_swing_params(regime: str) -> SwingParams:
    return _swing_table().get(regime, _swing_table()["NORMAL"])


def get_t2_params(regime: str) -> T2Params:
    return _t2_table().get(regime, _t2_table()["NORMAL"])
