from __future__ import annotations

from dataclasses import dataclass

import config

_SENTINEL = 999  # sentinel value for disabled/CRISIS rows — callers must check eligible/cutoff_hour first


@dataclass(frozen=True)
class EntryParams:
    composite_min: int
    rsi_max: int
    hard_close_hour: int = 13


@dataclass(frozen=True)
class SwingParams:
    eligible: bool
    min_pnl_pct: float
    profit_lock_pct: float
    tp1_pct: float
    tp2_pct: float
    sl_pct: float


@dataclass(frozen=True)
class T2Params:
    cutoff_hour: int


def _entry_table() -> dict[str, EntryParams]:
    defensive_composite = round(config.COMPOSITE_MIN_HIGH_VOL * config.DEFENSIVE_COMPOSITE_MULT)
    return {
        "CRISIS":         EntryParams(config.COMPOSITE_MIN_NORMAL,     config.RSI_BUY_MAX,              0),
        "NORMAL":         EntryParams(config.COMPOSITE_MIN_NORMAL,     config.RSI_BUY_MAX,             13),
        "BULL_TREND":     EntryParams(config.COMPOSITE_MIN_BULL_TREND, config.RSI_BUY_MAX,             14),
        "CHOPPY":         EntryParams(config.CHOPPY_COMPOSITE_GATE,    config.CHOPPY_RSI_PULLBACK_MAX, 12),
        "DEFENSIVE":      EntryParams(defensive_composite,             config.RSI_BUY_MAX_HIGH_VOL,    12),
        "HIGH_VOL":       EntryParams(config.COMPOSITE_MIN_HIGH_VOL,   config.RSI_BUY_MAX_HIGH_VOL,   13),
        "OPPORTUNITY":    EntryParams(config.COMPOSITE_MIN_NORMAL,     config.RSI_BUY_MAX,             14),
        "CRASH_RECOVERY": EntryParams(config.COMPOSITE_MIN_NORMAL,     config.RSI_BUY_MAX,             13),
        "CAPITULATION":   EntryParams(config.COMPOSITE_MIN_NORMAL,     config.RSI_BUY_MAX,             13),
    }


_CRISIS_SWING = SwingParams(False, _SENTINEL, _SENTINEL, 0.0, 0.0, 0.0)


def _swing_table() -> dict[str, SwingParams]:
    return {
        "NORMAL":         SwingParams(True,  config.SWING_MIN_PNL_NORMAL,           config.SWING_PROFIT_LOCK_NORMAL,    config.SWING_TP1_PCT,             config.SWING_TP2_PCT,             config.SWING_OVERNIGHT_SL_PCT),
        "BULL_TREND":     SwingParams(True,  config.SWING_MIN_PNL_BULL,             config.SWING_PROFIT_LOCK_BULL,      config.SWING_TP1_PCT,             config.SWING_TP2_PCT,             config.SWING_OVERNIGHT_SL_PCT),
        "OPPORTUNITY":    SwingParams(True,  config.SWING_MIN_PNL_NORMAL,           config.SWING_PROFIT_LOCK_NORMAL,    config.SWING_TP1_PCT,             config.SWING_TP2_PCT,             config.SWING_OVERNIGHT_SL_PCT),
        "CRASH_RECOVERY": SwingParams(True,  config.SWING_MIN_PNL_CRASH,            config.SWING_PROFIT_LOCK_NORMAL,    config.SWING_TP1_PCT,             config.SWING_TP2_PCT,             config.SWING_OVERNIGHT_SL_PCT),
        "CAPITULATION":   SwingParams(True,  config.SWING_MIN_PNL_PCT_CAPITULATION, _SENTINEL,                          config.SWING_TP1_PCT_CAPITULATION, config.SWING_TP2_PCT_CAPITULATION, config.SWING_SL_PCT_CAPITULATION),
        "CHOPPY":         SwingParams(False, config.SWING_MIN_PNL_NORMAL,           config.SWING_PROFIT_LOCK_NORMAL,    config.SWING_TP1_PCT,             config.SWING_TP2_PCT,             config.SWING_OVERNIGHT_SL_PCT),
        "DEFENSIVE":      SwingParams(False, config.SWING_MIN_PNL_NORMAL,           config.SWING_PROFIT_LOCK_DEFENSIVE, config.SWING_TP1_PCT,             config.SWING_TP2_PCT,             config.SWING_OVERNIGHT_SL_PCT),
        "HIGH_VOL":       SwingParams(False, config.SWING_MIN_PNL_NORMAL,           config.SWING_PROFIT_LOCK_DEFENSIVE, config.SWING_TP1_PCT,             config.SWING_TP2_PCT,             config.SWING_OVERNIGHT_SL_PCT),
        "CRISIS":         _CRISIS_SWING,
    }


_DISABLED_T2 = T2Params(0)


def _t2_table() -> dict[str, T2Params]:
    _normal = T2Params(config.TRANCHE2_CUTOFF_HOUR)
    return {
        "CRISIS":         _DISABLED_T2,
        "CHOPPY":         _DISABLED_T2,
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
