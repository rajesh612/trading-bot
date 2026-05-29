from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import config

_SENTINEL = 999  # sentinel value for disabled/CRISIS rows — callers must check eligible/cutoff_hour first

Mode = Literal["DAY_TRADE", "PEAD", "SWING", "CATALYST"]
Tier = Literal["LOW", "MEDIUM", "HIGH", "CONVICTION"]

REGIMES: tuple[str, ...] = (
    "NORMAL", "BULL_TREND", "OPPORTUNITY", "CRASH_RECOVERY",
    "DEFENSIVE", "CHOPPY", "HIGH_VOL", "CAPITULATION", "CRISIS",
)
TIERS:  tuple[str, ...] = ("LOW", "MEDIUM", "HIGH", "CONVICTION")
MODES:  tuple[str, ...] = ("DAY_TRADE", "PEAD", "SWING", "CATALYST")


@dataclass(frozen=True)
class TPSLParams:
    tp_floor_pct:    float
    tp_ceiling_pct:  float
    sl_floor_pct:    float
    sl_ceiling_pct:  float
    atr_mult:        float
    max_days:        int   = 0
    profit_lock_pct: float = 0.0
    min_pnl_pct:     float = 0.0

    @property
    def is_disabled(self) -> bool:
        return self.atr_mult == 0.0

    def clamp_tp(self, raw_tp: float) -> float:
        return max(self.tp_floor_pct, min(self.tp_ceiling_pct, raw_tp))

    def clamp_sl(self, raw_sl: float) -> float:
        return max(self.sl_floor_pct, min(self.sl_ceiling_pct, raw_sl))


def _dt(tp_f, tp_c, sl_f, sl_c, atr) -> TPSLParams:
    return TPSLParams(tp_f, tp_c, sl_f, sl_c, atr)

def _pd(tp_f, tp_c, sl_f, sl_c, atr) -> TPSLParams:
    return TPSLParams(tp_f, tp_c, sl_f, sl_c, atr)

def _sw(tp1, tp2, sl_f, sl_c, atr, days, plock, minpnl) -> TPSLParams:
    return TPSLParams(tp1, tp2, sl_f, sl_c, atr, days, plock, minpnl)

def _cl(tp_f, tp_c, sl_f, sl_c, atr) -> TPSLParams:
    return TPSLParams(tp_f, tp_c, sl_f, sl_c, atr)

_TPSL_DISABLED = TPSLParams(0.0, 0.0, 0.0, 0.0, 0.0)

_DISABLED_COMBOS: frozenset[tuple[str, str, str]] = frozenset({
    *(("CRISIS", t, m) for t in TIERS for m in MODES),
    *(("CAPITULATION", t, m) for t in ("LOW", "MEDIUM") for m in MODES),
    ("CHOPPY", "LOW",    "SWING"),
    ("CHOPPY", "MEDIUM", "SWING"),
    ("CHOPPY", "LOW",    "CATALYST"),
    ("CHOPPY", "MEDIUM", "CATALYST"),
    ("CHOPPY", "HIGH",   "CATALYST"),
    ("HIGH_VOL", "LOW", "SWING"),
    ("HIGH_VOL", "LOW", "CATALYST"),
})


def _build_tpsl_table() -> dict[tuple[str, str, str], TPSLParams]:
    t: dict[tuple[str, str, str], TPSLParams] = {}

    # ── DAY_TRADE ──────────────────────────────────────────────────────────────
    t[("NORMAL",         "LOW",        "DAY_TRADE")] = _dt(1.5, 3.0, 1.5, 2.0, 1.5)
    t[("NORMAL",         "MEDIUM",     "DAY_TRADE")] = _dt(2.0, 3.5, 1.5, 2.0, 1.8)
    t[("NORMAL",         "HIGH",       "DAY_TRADE")] = _dt(2.0, 4.0, 1.5, 1.8, 2.0)
    t[("NORMAL",         "CONVICTION", "DAY_TRADE")] = _dt(2.5, 4.5, 1.2, 1.8, 2.0)

    t[("BULL_TREND",     "LOW",        "DAY_TRADE")] = _dt(1.5, 3.5, 1.5, 2.0, 1.8)
    t[("BULL_TREND",     "MEDIUM",     "DAY_TRADE")] = _dt(2.0, 4.0, 1.5, 2.0, 2.0)
    t[("BULL_TREND",     "HIGH",       "DAY_TRADE")] = _dt(2.5, 4.5, 1.5, 1.8, 2.0)
    t[("BULL_TREND",     "CONVICTION", "DAY_TRADE")] = _dt(3.0, 5.0, 1.2, 1.8, 2.2)

    t[("OPPORTUNITY",    "LOW",        "DAY_TRADE")] = _dt(1.5, 3.5, 1.5, 2.0, 1.8)
    t[("OPPORTUNITY",    "MEDIUM",     "DAY_TRADE")] = _dt(2.0, 4.0, 1.5, 2.0, 2.0)
    t[("OPPORTUNITY",    "HIGH",       "DAY_TRADE")] = _dt(2.0, 4.5, 1.5, 1.8, 2.0)
    t[("OPPORTUNITY",    "CONVICTION", "DAY_TRADE")] = _dt(2.5, 5.0, 1.2, 1.8, 2.2)

    t[("CRASH_RECOVERY", "LOW",        "DAY_TRADE")] = _dt(1.5, 3.0, 1.5, 2.0, 1.5)
    t[("CRASH_RECOVERY", "MEDIUM",     "DAY_TRADE")] = _dt(1.5, 3.5, 1.5, 2.0, 1.8)
    t[("CRASH_RECOVERY", "HIGH",       "DAY_TRADE")] = _dt(2.0, 4.0, 1.5, 2.0, 2.0)
    t[("CRASH_RECOVERY", "CONVICTION", "DAY_TRADE")] = _dt(2.5, 4.5, 1.5, 1.8, 2.0)

    t[("DEFENSIVE",      "LOW",        "DAY_TRADE")] = _dt(1.5, 2.5, 1.2, 2.0, 1.3)
    t[("DEFENSIVE",      "MEDIUM",     "DAY_TRADE")] = _dt(1.5, 3.0, 1.2, 1.8, 1.5)
    t[("DEFENSIVE",      "HIGH",       "DAY_TRADE")] = _dt(2.0, 3.5, 1.2, 1.8, 1.8)
    t[("DEFENSIVE",      "CONVICTION", "DAY_TRADE")] = _dt(2.0, 4.0, 1.2, 1.5, 2.0)

    t[("CHOPPY",         "LOW",        "DAY_TRADE")] = _dt(1.5, 2.5, 1.2, 2.0, 1.2)
    t[("CHOPPY",         "MEDIUM",     "DAY_TRADE")] = _dt(1.5, 3.0, 1.2, 1.8, 1.4)
    t[("CHOPPY",         "HIGH",       "DAY_TRADE")] = _dt(2.0, 3.5, 1.2, 1.8, 1.6)
    t[("CHOPPY",         "CONVICTION", "DAY_TRADE")] = _dt(2.0, 4.0, 1.2, 1.5, 1.8)

    t[("HIGH_VOL",       "LOW",        "DAY_TRADE")] = _dt(2.0, 3.5, 2.0, 3.0, 1.5)
    t[("HIGH_VOL",       "MEDIUM",     "DAY_TRADE")] = _dt(2.0, 4.0, 2.0, 2.5, 1.8)
    t[("HIGH_VOL",       "HIGH",       "DAY_TRADE")] = _dt(2.0, 4.0, 2.0, 2.5, 2.0)
    t[("HIGH_VOL",       "CONVICTION", "DAY_TRADE")] = _dt(2.5, 4.5, 1.8, 2.5, 2.0)

    t[("CAPITULATION",   "HIGH",       "DAY_TRADE")] = _dt(2.0, 5.0, 2.0, 3.5, 2.0)
    t[("CAPITULATION",   "CONVICTION", "DAY_TRADE")] = _dt(2.5, 6.0, 2.0, 3.0, 2.5)

    # ── PEAD ───────────────────────────────────────────────────────────────────
    t[("NORMAL",         "LOW",        "PEAD")] = _pd(2.0, 5.0, 1.5, 2.5, 2.0)
    t[("NORMAL",         "MEDIUM",     "PEAD")] = _pd(2.5, 6.0, 1.5, 2.5, 2.5)
    t[("NORMAL",         "HIGH",       "PEAD")] = _pd(3.0, 7.0, 1.5, 2.0, 2.8)
    t[("NORMAL",         "CONVICTION", "PEAD")] = _pd(3.5, 8.0, 1.5, 2.0, 3.0)

    t[("BULL_TREND",     "LOW",        "PEAD")] = _pd(2.5, 6.0, 1.5, 2.5, 2.2)
    t[("BULL_TREND",     "MEDIUM",     "PEAD")] = _pd(3.0, 7.0, 1.5, 2.5, 2.8)
    t[("BULL_TREND",     "HIGH",       "PEAD")] = _pd(3.5, 8.0, 1.5, 2.0, 3.0)
    t[("BULL_TREND",     "CONVICTION", "PEAD")] = _pd(4.0, 10.0, 1.5, 2.0, 3.5)

    t[("OPPORTUNITY",    "LOW",        "PEAD")] = _pd(2.5, 6.0, 1.5, 2.5, 2.2)
    t[("OPPORTUNITY",    "MEDIUM",     "PEAD")] = _pd(3.0, 7.0, 1.5, 2.0, 2.5)
    t[("OPPORTUNITY",    "HIGH",       "PEAD")] = _pd(3.5, 8.0, 1.5, 2.0, 3.0)
    t[("OPPORTUNITY",    "CONVICTION", "PEAD")] = _pd(4.0, 10.0, 1.5, 2.0, 3.5)

    t[("CRASH_RECOVERY", "LOW",        "PEAD")] = _pd(2.0, 5.0, 1.5, 2.5, 2.0)
    t[("CRASH_RECOVERY", "MEDIUM",     "PEAD")] = _pd(2.5, 6.0, 1.5, 2.5, 2.5)
    t[("CRASH_RECOVERY", "HIGH",       "PEAD")] = _pd(3.0, 7.0, 1.5, 2.0, 2.8)
    t[("CRASH_RECOVERY", "CONVICTION", "PEAD")] = _pd(3.5, 8.0, 1.5, 2.0, 3.0)

    t[("DEFENSIVE",      "LOW",        "PEAD")] = _pd(2.0, 4.0, 1.5, 2.5, 1.8)
    t[("DEFENSIVE",      "MEDIUM",     "PEAD")] = _pd(2.0, 5.0, 1.5, 2.5, 2.0)
    t[("DEFENSIVE",      "HIGH",       "PEAD")] = _pd(2.5, 6.0, 1.5, 2.0, 2.5)
    t[("DEFENSIVE",      "CONVICTION", "PEAD")] = _pd(3.0, 7.0, 1.5, 2.0, 2.8)

    t[("CHOPPY",         "LOW",        "PEAD")] = _pd(1.5, 3.5, 1.5, 2.5, 1.5)
    t[("CHOPPY",         "MEDIUM",     "PEAD")] = _pd(2.0, 4.5, 1.5, 2.5, 1.8)
    t[("CHOPPY",         "HIGH",       "PEAD")] = _pd(2.5, 5.5, 1.5, 2.0, 2.2)
    t[("CHOPPY",         "CONVICTION", "PEAD")] = _pd(3.0, 6.5, 1.5, 2.0, 2.5)

    t[("HIGH_VOL",       "LOW",        "PEAD")] = _pd(2.0, 5.0, 2.0, 3.5, 1.8)
    t[("HIGH_VOL",       "MEDIUM",     "PEAD")] = _pd(2.5, 6.0, 2.0, 3.0, 2.2)
    t[("HIGH_VOL",       "HIGH",       "PEAD")] = _pd(3.0, 7.0, 2.0, 3.0, 2.5)
    t[("HIGH_VOL",       "CONVICTION", "PEAD")] = _pd(3.5, 8.0, 2.0, 2.5, 2.8)

    t[("CAPITULATION",   "HIGH",       "PEAD")] = _pd(4.0, 10.0, 2.5, 5.0, 3.0)
    t[("CAPITULATION",   "CONVICTION", "PEAD")] = _pd(5.0, 12.0, 2.5, 4.5, 3.5)

    # ── SWING ──────────────────────────────────────────────────────────────────
    # SL floors come from config.SWING_SL_FLOOR_PARAMS (per regime×tier — full granularity).
    # Hold params (max_days, profit_lock, min_pnl) come from config.SWING_HOLD_PARAMS.
    # TP floors and ceilings are unchanged from the original table design.
    def _sw_cfg(tp1, tp2, regime, tier, atr):
        sl_f = config.SWING_SL_FLOOR_PARAMS.get((regime, tier), 3.5)
        sl_c = round(sl_f + 1.5, 1)
        days, plock, minpnl = config.SWING_HOLD_PARAMS[(regime, tier)]
        tp1 = max(tp1, sl_f)   # first TP must be >= SL floor for valid risk/reward
        return TPSLParams(tp1, tp2, sl_f, sl_c, atr, days, plock, minpnl)

    t[("NORMAL",         "LOW",        "SWING")] = _sw_cfg(4.0,  8.0, "NORMAL",  "LOW",        2.5)
    t[("NORMAL",         "MEDIUM",     "SWING")] = _sw_cfg(4.5,  8.5, "NORMAL",  "MEDIUM",     3.0)
    t[("NORMAL",         "HIGH",       "SWING")] = _sw_cfg(5.0,  9.0, "NORMAL",  "HIGH",       3.2)
    t[("NORMAL",         "CONVICTION", "SWING")] = _sw_cfg(5.5, 10.0, "NORMAL",  "CONVICTION", 3.5)

    t[("BULL_TREND",     "LOW",        "SWING")] = _sw_cfg(4.5,  8.5, "BULL_TREND", "LOW",        2.8)
    t[("BULL_TREND",     "MEDIUM",     "SWING")] = _sw_cfg(5.0,  9.0, "BULL_TREND", "MEDIUM",     3.2)
    t[("BULL_TREND",     "HIGH",       "SWING")] = _sw_cfg(6.0, 10.5, "BULL_TREND", "HIGH",       3.5)
    t[("BULL_TREND",     "CONVICTION", "SWING")] = _sw_cfg(7.0, 12.0, "BULL_TREND", "CONVICTION", 4.0)

    t[("OPPORTUNITY",    "LOW",        "SWING")] = _sw_cfg(4.0,  8.0, "OPPORTUNITY", "LOW",        2.5)
    t[("OPPORTUNITY",    "MEDIUM",     "SWING")] = _sw_cfg(5.0,  9.0, "OPPORTUNITY", "MEDIUM",     3.0)
    t[("OPPORTUNITY",    "HIGH",       "SWING")] = _sw_cfg(5.5, 10.0, "OPPORTUNITY", "HIGH",       3.2)
    t[("OPPORTUNITY",    "CONVICTION", "SWING")] = _sw_cfg(6.0, 11.0, "OPPORTUNITY", "CONVICTION", 3.5)

    t[("CRASH_RECOVERY", "LOW",        "SWING")] = _sw_cfg(3.5,  7.0, "CRASH_RECOVERY", "LOW",        2.5)
    t[("CRASH_RECOVERY", "MEDIUM",     "SWING")] = _sw_cfg(4.0,  8.0, "CRASH_RECOVERY", "MEDIUM",     3.0)
    t[("CRASH_RECOVERY", "HIGH",       "SWING")] = _sw_cfg(5.0,  9.0, "CRASH_RECOVERY", "HIGH",       3.2)
    t[("CRASH_RECOVERY", "CONVICTION", "SWING")] = _sw_cfg(5.5, 10.0, "CRASH_RECOVERY", "CONVICTION", 3.5)

    t[("DEFENSIVE",      "LOW",        "SWING")] = _sw_cfg(3.0,  6.0, "DEFENSIVE", "LOW",        2.0)
    t[("DEFENSIVE",      "MEDIUM",     "SWING")] = _sw_cfg(3.5,  7.0, "DEFENSIVE", "MEDIUM",     2.5)
    t[("DEFENSIVE",      "HIGH",       "SWING")] = _sw_cfg(4.0,  8.0, "DEFENSIVE", "HIGH",       2.8)
    t[("DEFENSIVE",      "CONVICTION", "SWING")] = _sw_cfg(5.0,  9.0, "DEFENSIVE", "CONVICTION", 3.0)

    t[("CHOPPY",         "HIGH",       "SWING")] = _sw_cfg(3.5,  7.0, "CHOPPY", "HIGH",       2.0)
    t[("CHOPPY",         "CONVICTION", "SWING")] = _sw_cfg(4.0,  8.0, "CHOPPY", "CONVICTION", 2.5)

    t[("HIGH_VOL",       "MEDIUM",     "SWING")] = _sw_cfg(3.5,  7.0, "HIGH_VOL", "MEDIUM",     2.5)
    t[("HIGH_VOL",       "HIGH",       "SWING")] = _sw_cfg(4.0,  8.0, "HIGH_VOL", "HIGH",       3.0)
    t[("HIGH_VOL",       "CONVICTION", "SWING")] = _sw_cfg(5.0,  9.5, "HIGH_VOL", "CONVICTION", 3.2)

    t[("CAPITULATION",   "HIGH",       "SWING")] = _sw_cfg(6.0, 12.0, "CAPITULATION", "HIGH",       3.5)
    t[("CAPITULATION",   "CONVICTION", "SWING")] = _sw_cfg(8.0, 15.0, "CAPITULATION", "CONVICTION", 4.0)

    # ── CATALYST ────────────────────────────────────────────────────────────────
    t[("NORMAL",         "LOW",        "CATALYST")] = _cl(2.0, 3.5, 1.2, 1.8, 1.8)
    t[("NORMAL",         "MEDIUM",     "CATALYST")] = _cl(2.5, 4.0, 1.2, 1.8, 2.0)
    t[("NORMAL",         "HIGH",       "CATALYST")] = _cl(3.0, 4.5, 1.2, 1.8, 2.2)
    t[("NORMAL",         "CONVICTION", "CATALYST")] = _cl(3.5, 5.0, 1.0, 1.5, 2.5)

    t[("BULL_TREND",     "LOW",        "CATALYST")] = _cl(2.5, 4.0, 1.2, 1.8, 2.0)
    t[("BULL_TREND",     "MEDIUM",     "CATALYST")] = _cl(3.0, 4.5, 1.2, 1.8, 2.2)
    t[("BULL_TREND",     "HIGH",       "CATALYST")] = _cl(3.5, 5.0, 1.2, 1.8, 2.5)
    t[("BULL_TREND",     "CONVICTION", "CATALYST")] = _cl(4.0, 6.0, 1.0, 1.5, 3.0)

    t[("OPPORTUNITY",    "LOW",        "CATALYST")] = _cl(2.5, 4.0, 1.2, 1.8, 2.0)
    t[("OPPORTUNITY",    "MEDIUM",     "CATALYST")] = _cl(3.0, 4.5, 1.2, 1.8, 2.2)
    t[("OPPORTUNITY",    "HIGH",       "CATALYST")] = _cl(3.5, 5.0, 1.2, 1.8, 2.5)
    t[("OPPORTUNITY",    "CONVICTION", "CATALYST")] = _cl(4.0, 6.0, 1.0, 1.5, 3.0)

    t[("CRASH_RECOVERY", "LOW",        "CATALYST")] = _cl(2.0, 4.0, 1.5, 2.5, 1.8)
    t[("CRASH_RECOVERY", "MEDIUM",     "CATALYST")] = _cl(2.5, 4.5, 1.5, 2.0, 2.0)
    t[("CRASH_RECOVERY", "HIGH",       "CATALYST")] = _cl(3.0, 5.0, 1.5, 2.0, 2.5)
    t[("CRASH_RECOVERY", "CONVICTION", "CATALYST")] = _cl(3.5, 6.0, 1.5, 2.0, 2.8)

    t[("DEFENSIVE",      "LOW",        "CATALYST")] = _cl(1.5, 3.0, 1.2, 2.0, 1.5)
    t[("DEFENSIVE",      "MEDIUM",     "CATALYST")] = _cl(2.0, 3.5, 1.2, 1.8, 1.8)
    t[("DEFENSIVE",      "HIGH",       "CATALYST")] = _cl(2.5, 4.0, 1.2, 1.8, 2.0)
    t[("DEFENSIVE",      "CONVICTION", "CATALYST")] = _cl(3.0, 4.5, 1.2, 1.5, 2.2)

    t[("CHOPPY",         "CONVICTION", "CATALYST")] = _cl(2.5, 4.0, 1.5, 2.0, 2.0)

    t[("HIGH_VOL",       "MEDIUM",     "CATALYST")] = _cl(2.0, 4.0, 2.0, 3.0, 1.8)
    t[("HIGH_VOL",       "HIGH",       "CATALYST")] = _cl(2.5, 4.5, 2.0, 2.5, 2.0)
    t[("HIGH_VOL",       "CONVICTION", "CATALYST")] = _cl(3.0, 5.0, 2.0, 2.5, 2.5)

    t[("CAPITULATION",   "HIGH",       "CATALYST")] = _cl(3.0, 6.0, 2.5, 4.0, 2.5)
    t[("CAPITULATION",   "CONVICTION", "CATALYST")] = _cl(4.0, 8.0, 2.5, 3.5, 3.0)

    # Stamp disabled entries
    for key in _DISABLED_COMBOS:
        t[key] = _TPSL_DISABLED

    return t


_TPSL_TABLE: dict[tuple[str, str, str], TPSLParams] = _build_tpsl_table()
_TPSL_FALLBACK = _TPSL_TABLE[("NORMAL", "MEDIUM", "DAY_TRADE")]


def get_tp_sl_params(regime: str, tier: str, mode: str) -> TPSLParams:
    return _TPSL_TABLE.get((regime, tier, mode), _TPSL_FALLBACK)


@dataclass(frozen=True)
class EntryParams:
    composite_min: int
    rsi_max: int
    hard_close_hour: int = 13
    max_position_pct: float = 0.05


@dataclass(frozen=True)
class SwingParams:
    eligible: bool


@dataclass(frozen=True)
class T2Params:
    cutoff_hour: int


@dataclass(frozen=True)
class FilterParams:
    composite_min: int
    sentiment_hard_block: int
    intraday_block: int


@dataclass(frozen=True)
class GateParams:
    stoch_soft_flag: int
    stoch_hard_veto: int
    rel_volume_min: float
    breadth_weak_threshold: float
    stop_hunt_veto: bool = True   # False for momentum regimes where near-high is a buy signal


@dataclass(frozen=True)
class AutoExecuteParams:
    composite_offset: int       # 999 = disabled
    sentiment_min: int
    extra_unlock: str | None    # named unlock required, or None


@dataclass(frozen=True)
class ConcurrencyParams:
    max_open:          int
    size_mult:         float
    heat_weight:       float
    swing_health_gate: bool


@dataclass(frozen=True)
class SwingGateParams:
    composite_min: int
    rsi_ceiling: int


_SWING_GATE_FALLBACK = SwingGateParams(composite_min=58, rsi_ceiling=70)

_SWING_GATE_TABLE: dict[tuple[str, str], SwingGateParams] = {
    ("BULL_TREND",     "CONVICTION"): SwingGateParams(50, 75),
    ("BULL_TREND",     "HIGH"):       SwingGateParams(53, 73),
    ("BULL_TREND",     "MEDIUM"):     SwingGateParams(55, 72),
    ("BULL_TREND",     "LOW"):        SwingGateParams(58, 69),
    ("NORMAL",         "CONVICTION"): SwingGateParams(55, 73),
    ("NORMAL",         "HIGH"):       SwingGateParams(62, 71),
    ("NORMAL",         "MEDIUM"):     SwingGateParams(65, 70),
    ("NORMAL",         "LOW"):        SwingGateParams(66, 67),
    ("OPPORTUNITY",    "CONVICTION"): SwingGateParams(52, 75),
    ("OPPORTUNITY",    "HIGH"):       SwingGateParams(57, 73),
    ("OPPORTUNITY",    "MEDIUM"):     SwingGateParams(63, 71),
    ("OPPORTUNITY",    "LOW"):        SwingGateParams(64, 68),
    ("CRASH_RECOVERY", "CONVICTION"): SwingGateParams(52, 70),
    ("CRASH_RECOVERY", "HIGH"):       SwingGateParams(55, 68),
    ("CRASH_RECOVERY", "MEDIUM"):     SwingGateParams(58, 66),
    ("CRASH_RECOVERY", "LOW"):        SwingGateParams(62, 63),
    ("CAPITULATION",   "CONVICTION"): SwingGateParams(54, 68),
    ("CAPITULATION",   "HIGH"):       SwingGateParams(57, 65),
    ("CAPITULATION",   "MEDIUM"):     SwingGateParams(60, 63),
    ("CAPITULATION",   "LOW"):        SwingGateParams(63, 60),
}


def get_swing_gate_params(regime: str, tier: str) -> SwingGateParams:
    return _SWING_GATE_TABLE.get((regime, tier), _SWING_GATE_FALLBACK)


@dataclass(frozen=True)
class DayTradeParams:
    """Per-regime × tier controls for day-trade entry timing and momentum.
    hard_close_hour: no new entries at/after this hour ET (per tier, finer than EntryParams which is CONVICTION-level).
    late_entry_hour: composite floor kicks in after this hour.
    late_composite:  minimum composite required if hour >= late_entry_hour.
    vwap_slope_min:  VWAP slope floor — flat/declining stocks are excluded.
    """
    hard_close_hour: int
    late_entry_hour: int
    late_composite: int
    vwap_slope_min: float


def _day_trade_table() -> dict[tuple[str, str], DayTradeParams]:
    dt = DayTradeParams
    return {
        # ── NORMAL ───────────────────────────────────────────────────────────
        ("NORMAL", "CONVICTION"):  dt(hard_close_hour=13, late_entry_hour=12, late_composite=65, vwap_slope_min= 0.00),
        ("NORMAL", "HIGH"):        dt(hard_close_hour=12, late_entry_hour=11, late_composite=68, vwap_slope_min= 0.03),
        ("NORMAL", "MEDIUM"):      dt(hard_close_hour=12, late_entry_hour=10, late_composite=68, vwap_slope_min= 0.05),
        ("NORMAL", "LOW"):         dt(hard_close_hour=11, late_entry_hour=10, late_composite=75, vwap_slope_min= 0.10),
        # ── BULL_TREND ───────────────────────────────────────────────────────
        ("BULL_TREND", "CONVICTION"):  dt(hard_close_hour=14, late_entry_hour=13, late_composite=60, vwap_slope_min=-0.05),
        ("BULL_TREND", "HIGH"):        dt(hard_close_hour=14, late_entry_hour=12, late_composite=65, vwap_slope_min= 0.00),
        ("BULL_TREND", "MEDIUM"):      dt(hard_close_hour=13, late_entry_hour=12, late_composite=68, vwap_slope_min= 0.02),
        ("BULL_TREND", "LOW"):         dt(hard_close_hour=12, late_entry_hour=11, late_composite=72, vwap_slope_min= 0.05),
        # ── OPPORTUNITY ──────────────────────────────────────────────────────
        ("OPPORTUNITY", "CONVICTION"): dt(hard_close_hour=14, late_entry_hour=13, late_composite=60, vwap_slope_min=-0.05),
        ("OPPORTUNITY", "HIGH"):       dt(hard_close_hour=14, late_entry_hour=12, late_composite=65, vwap_slope_min= 0.00),
        ("OPPORTUNITY", "MEDIUM"):     dt(hard_close_hour=13, late_entry_hour=12, late_composite=68, vwap_slope_min= 0.02),
        ("OPPORTUNITY", "LOW"):        dt(hard_close_hour=12, late_entry_hour=11, late_composite=72, vwap_slope_min= 0.05),
        # ── HIGH_VOL ─────────────────────────────────────────────────────────
        ("HIGH_VOL", "CONVICTION"): dt(hard_close_hour=12, late_entry_hour=11, late_composite=65, vwap_slope_min=0.05),
        ("HIGH_VOL", "HIGH"):       dt(hard_close_hour=12, late_entry_hour=11, late_composite=68, vwap_slope_min=0.06),
        ("HIGH_VOL", "MEDIUM"):     dt(hard_close_hour=12, late_entry_hour=11, late_composite=72, vwap_slope_min=0.08),
        ("HIGH_VOL", "LOW"):        dt(hard_close_hour=11, late_entry_hour=10, late_composite=80, vwap_slope_min=0.15),
        # ── CHOPPY ───────────────────────────────────────────────────────────
        ("CHOPPY", "CONVICTION"): dt(hard_close_hour=12, late_entry_hour=10, late_composite=70, vwap_slope_min=0.05),
        ("CHOPPY", "HIGH"):       dt(hard_close_hour=11, late_entry_hour=10, late_composite=75, vwap_slope_min=0.08),
        ("CHOPPY", "MEDIUM"):     dt(hard_close_hour=11, late_entry_hour=10, late_composite=80, vwap_slope_min=0.10),
        ("CHOPPY", "LOW"):        dt(hard_close_hour=11, late_entry_hour=10, late_composite=85, vwap_slope_min=0.15),
        # ── DEFENSIVE ────────────────────────────────────────────────────────
        ("DEFENSIVE", "CONVICTION"): dt(hard_close_hour=12, late_entry_hour=10, late_composite=70, vwap_slope_min=0.08),
        ("DEFENSIVE", "HIGH"):       dt(hard_close_hour=11, late_entry_hour=10, late_composite=75, vwap_slope_min=0.12),
        ("DEFENSIVE", "MEDIUM"):     dt(hard_close_hour=11, late_entry_hour=10, late_composite=80, vwap_slope_min=0.15),
        ("DEFENSIVE", "LOW"):        dt(hard_close_hour=11, late_entry_hour=10, late_composite=85, vwap_slope_min=0.20),
        # ── CRASH_RECOVERY ───────────────────────────────────────────────────
        ("CRASH_RECOVERY", "CONVICTION"): dt(hard_close_hour=12, late_entry_hour=11, late_composite=65, vwap_slope_min=0.05),
        ("CRASH_RECOVERY", "HIGH"):       dt(hard_close_hour=12, late_entry_hour=10, late_composite=70, vwap_slope_min=0.08),
        ("CRASH_RECOVERY", "MEDIUM"):     dt(hard_close_hour=11, late_entry_hour=10, late_composite=75, vwap_slope_min=0.10),
        ("CRASH_RECOVERY", "LOW"):        dt(hard_close_hour=11, late_entry_hour=10, late_composite=80, vwap_slope_min=0.15),
        # ── CAPITULATION ─────────────────────────────────────────────────────
        ("CAPITULATION", "CONVICTION"): dt(hard_close_hour=12, late_entry_hour=11, late_composite=65, vwap_slope_min=0.05),
        ("CAPITULATION", "HIGH"):       dt(hard_close_hour=12, late_entry_hour=10, late_composite=70, vwap_slope_min=0.08),
        ("CAPITULATION", "MEDIUM"):     dt(hard_close_hour=11, late_entry_hour=10, late_composite=75, vwap_slope_min=0.10),
        ("CAPITULATION", "LOW"):        dt(hard_close_hour=11, late_entry_hour=10, late_composite=80, vwap_slope_min=0.15),
        # ── CRISIS (no entries; table present for fallback safety) ────────────
        ("CRISIS", "CONVICTION"): dt(hard_close_hour=0, late_entry_hour=0, late_composite=999, vwap_slope_min=0.0),
        ("CRISIS", "HIGH"):       dt(hard_close_hour=0, late_entry_hour=0, late_composite=999, vwap_slope_min=0.0),
        ("CRISIS", "MEDIUM"):     dt(hard_close_hour=0, late_entry_hour=0, late_composite=999, vwap_slope_min=0.0),
        ("CRISIS", "LOW"):        dt(hard_close_hour=0, late_entry_hour=0, late_composite=999, vwap_slope_min=0.0),
    }


_DAY_TRADE_TABLE = _day_trade_table()
_DAY_TRADE_FALLBACK = _DAY_TRADE_TABLE[("NORMAL", "MEDIUM")]


def get_day_trade_params(regime: str, tier: str) -> DayTradeParams:
    return _DAY_TRADE_TABLE.get((regime, tier), _DAY_TRADE_FALLBACK)


_CONCURRENCY_DISABLED     = ConcurrencyParams(max_open=0,   size_mult=1.0, heat_weight=1.0, swing_health_gate=True)
_CONCURRENCY_CONSERVATIVE = ConcurrencyParams(max_open=1,   size_mult=1.0, heat_weight=1.0, swing_health_gate=True)


_CONCURRENCY_TABLE: dict[tuple[str, str], ConcurrencyParams] = {
    ("BULL_TREND",     "CONVICTION"): ConcurrencyParams(5,   1.35, 0.90, False),
    ("BULL_TREND",     "HIGH"):       ConcurrencyParams(4,   1.15, 1.00, False),
    ("BULL_TREND",     "MEDIUM"):     ConcurrencyParams(3,   1.00, 1.00, True),
    ("BULL_TREND",     "LOW"):        ConcurrencyParams(2,   0.80, 1.20, True),
    ("NORMAL",         "CONVICTION"): ConcurrencyParams(4,   1.25, 0.90, False),
    ("NORMAL",         "HIGH"):       ConcurrencyParams(3,   1.10, 1.00, True),
    ("NORMAL",         "MEDIUM"):     ConcurrencyParams(2,   1.00, 1.00, True),
    ("NORMAL",         "LOW"):        ConcurrencyParams(1,   0.80, 1.20, True),
    ("OPPORTUNITY",    "CONVICTION"): ConcurrencyParams(4,   1.25, 0.90, False),
    ("OPPORTUNITY",    "HIGH"):       ConcurrencyParams(3,   1.10, 1.00, True),
    ("OPPORTUNITY",    "MEDIUM"):     ConcurrencyParams(2,   1.00, 1.00, True),
    ("OPPORTUNITY",    "LOW"):        ConcurrencyParams(1,   0.80, 1.20, True),
    ("CHOPPY",         "CONVICTION"): ConcurrencyParams(3,   1.00, 0.90, True),
    ("CHOPPY",         "HIGH"):       ConcurrencyParams(2,   0.90, 1.00, True),
    ("CHOPPY",         "MEDIUM"):     ConcurrencyParams(1,   0.80, 1.10, True),
    ("CHOPPY",         "LOW"):        ConcurrencyParams(1,   0.70, 1.30, True),
    ("HIGH_VOL",       "CONVICTION"): ConcurrencyParams(3,   1.00, 0.90, True),
    ("HIGH_VOL",       "HIGH"):       ConcurrencyParams(2,   0.90, 1.00, True),
    ("HIGH_VOL",       "MEDIUM"):     ConcurrencyParams(1,   0.80, 1.10, True),
    ("HIGH_VOL",       "LOW"):        ConcurrencyParams(1,   0.70, 1.30, True),
    ("CRASH_RECOVERY", "CONVICTION"): ConcurrencyParams(3,   1.00, 0.90, True),
    ("CRASH_RECOVERY", "HIGH"):       ConcurrencyParams(2,   0.90, 1.00, True),
    ("CRASH_RECOVERY", "MEDIUM"):     ConcurrencyParams(1,   0.90, 1.00, True),
    ("CRASH_RECOVERY", "LOW"):        ConcurrencyParams(1,   0.80, 1.20, True),
    ("DEFENSIVE",      "CONVICTION"): ConcurrencyParams(2,   1.00, 1.00, True),
    ("DEFENSIVE",      "HIGH"):       ConcurrencyParams(1,   0.90, 1.00, True),
    ("DEFENSIVE",      "MEDIUM"):     ConcurrencyParams(1,   0.80, 1.10, True),
    ("DEFENSIVE",      "LOW"):        ConcurrencyParams(0,   1.00, 1.00, True),
    ("CAPITULATION",   "CONVICTION"): ConcurrencyParams(2,   1.00, 1.00, True),
    ("CAPITULATION",   "HIGH"):       ConcurrencyParams(1,   0.90, 1.00, True),
    ("CAPITULATION",   "MEDIUM"):     ConcurrencyParams(0,   1.00, 1.00, True),
    ("CAPITULATION",   "LOW"):        ConcurrencyParams(0,   1.00, 1.00, True),
}


def get_concurrency_params(regime: str, tier: str, mode: str) -> ConcurrencyParams:
    try:
        if regime == "CRISIS":
            return _CONCURRENCY_DISABLED
        cp = _CONCURRENCY_TABLE.get((regime, tier), _CONCURRENCY_CONSERVATIVE)
        if mode in ("PEAD", "CATALYST"):
            cp = ConcurrencyParams(
                max_open=cp.max_open + 1,
                size_mult=cp.size_mult,
                heat_weight=cp.heat_weight,
                swing_health_gate=cp.swing_health_gate,
            )
        if mode == "SWING":
            return ConcurrencyParams(max_open=999, size_mult=1.0, heat_weight=0.0, swing_health_gate=False)
        return cp
    except Exception:
        return _CONCURRENCY_CONSERVATIVE


def _entry_table() -> dict[str, EntryParams]:
    defensive_composite = round(config.COMPOSITE_MIN_HIGH_VOL * config.DEFENSIVE_COMPOSITE_MULT)
    return {
        "CRISIS":         EntryParams(config.COMPOSITE_MIN_NORMAL,     config.RSI_BUY_MAX,              0,  0.0),
        "NORMAL":         EntryParams(config.COMPOSITE_MIN_NORMAL,     config.RSI_BUY_MAX,             13,  0.05),  # CONVICTION-tier cutoff; per-tier via DayTradeParams
        "BULL_TREND":     EntryParams(config.COMPOSITE_MIN_BULL_TREND, config.RSI_BUY_MAX,             14,  0.03),
        "CHOPPY":         EntryParams(config.CHOPPY_COMPOSITE_GATE,    config.CHOPPY_RSI_PULLBACK_MAX, 12,  0.04),
        "DEFENSIVE":      EntryParams(defensive_composite,             config.RSI_BUY_MAX_HIGH_VOL,    12,  0.04),
        "HIGH_VOL":       EntryParams(config.COMPOSITE_MIN_HIGH_VOL,   config.RSI_BUY_MAX_HIGH_VOL,   12,  0.03),
        "OPPORTUNITY":    EntryParams(config.COMPOSITE_MIN_NORMAL,     config.RSI_BUY_MAX,             14,  0.03),
        "CRASH_RECOVERY": EntryParams(config.COMPOSITE_MIN_NORMAL,     config.RSI_BUY_MAX,             12,  0.025),
        "CAPITULATION":   EntryParams(config.COMPOSITE_MIN_NORMAL,     config.RSI_BUY_MAX,             12,  0.025),
    }


_CRISIS_SWING = SwingParams(False)


def _swing_table() -> dict[str, SwingParams]:
    return {
        "NORMAL":         SwingParams(True),
        "BULL_TREND":     SwingParams(True),
        "OPPORTUNITY":    SwingParams(True),
        "CRASH_RECOVERY": SwingParams(True),
        "CAPITULATION":   SwingParams(True),
        "CHOPPY":         SwingParams(False),
        "DEFENSIVE":      SwingParams(False),
        "HIGH_VOL":       SwingParams(False),
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


_AE_DISABLED = AutoExecuteParams(999, 0, None)


def _filter_table() -> dict[tuple[str, str], FilterParams]:
    base = {
        "NORMAL":         (config.COMPOSITE_MIN_NORMAL, -15, -10),
        "BULL_TREND":     (config.COMPOSITE_MIN_BULL_TREND, -15, -10),
        "OPPORTUNITY":    (config.COMPOSITE_MIN_NORMAL, -15, -10),
        "CRASH_RECOVERY": (config.COMPOSITE_MIN_NORMAL, -15, -10),
        "DEFENSIVE":      (config.COMPOSITE_MIN_HIGH_VOL, -10, -5),
        "CHOPPY":         (config.CHOPPY_COMPOSITE_GATE, -10, -5),
        "HIGH_VOL":       (config.COMPOSITE_MIN_HIGH_VOL, -10, -5),
        "CAPITULATION":   (config.COMPOSITE_MIN_NORMAL, -15, -10),
        "CRISIS":         (999, 0, 0),
    }
    offsets = {"LOW": +5, "MEDIUM": 0, "HIGH": -5, "CONVICTION": -10}
    table = {}
    for regime, (base_comp, sent_block, intra_block) in base.items():
        for tier, offset in offsets.items():
            table[(regime, tier)] = FilterParams(
                composite_min=max(0, base_comp + offset),
                sentiment_hard_block=sent_block,
                intraday_block=intra_block,
            )
    return table


_STOP_HUNT_VETO_DISABLED = frozenset({"BULL_TREND", "OPPORTUNITY"})


def _gate_table() -> dict[tuple[str, str], GateParams]:
    base = {
        "NORMAL":         (85, 97, 0.60, 0.85),
        "BULL_TREND":     (88, 97, 0.55, 0.80),
        "OPPORTUNITY":    (85, 97, 0.60, 0.85),
        "CRASH_RECOVERY": (83, 95, 0.60, 0.85),
        "DEFENSIVE":      (80, 95, 0.65, 0.90),
        "CHOPPY":         (80, 95, 0.65, 0.90),
        "HIGH_VOL":       (82, 95, 0.65, 0.88),
        "CAPITULATION":   (78, 93, 0.55, 0.85),
        "CRISIS":         (70, 90, 0.70, 0.90),
    }
    table = {}
    for regime, (ss, sv, rv, bw) in base.items():
        for tier in ("LOW", "MEDIUM", "HIGH", "CONVICTION"):
            adj = -3 if tier in ("LOW", "MEDIUM") else (3 if tier == "CONVICTION" else 0)
            table[(regime, tier)] = GateParams(
                stoch_soft_flag=min(sv - 2, ss + adj),
                stoch_hard_veto=sv,
                rel_volume_min=rv,
                breadth_weak_threshold=bw,
                stop_hunt_veto=regime not in _STOP_HUNT_VETO_DISABLED,
            )
    return table


def _ae_table() -> dict[tuple[str, str], AutoExecuteParams]:
    AE = AutoExecuteParams
    D  = _AE_DISABLED
    return {
        ("NORMAL", "LOW"):        D,
        ("NORMAL", "MEDIUM"):     AE(20, 20, None),
        ("NORMAL", "HIGH"):       AE(15, 20, None),
        ("NORMAL", "CONVICTION"): AE(10, 20, None),
        ("BULL_TREND", "LOW"):        D,
        ("BULL_TREND", "MEDIUM"):     AE(20, 20, None),
        ("BULL_TREND", "HIGH"):       AE(15, 20, None),
        ("BULL_TREND", "CONVICTION"): AE(10, 20, None),
        ("OPPORTUNITY", "LOW"):        D,
        ("OPPORTUNITY", "MEDIUM"):     D,
        ("OPPORTUNITY", "HIGH"):       AE(15, 20, "SUB_ETF_CONFIRM_03"),
        ("OPPORTUNITY", "CONVICTION"): AE(10, 20, "SUB_ETF_CONFIRM_05"),
        ("CRASH_RECOVERY", "LOW"):        D,
        ("CRASH_RECOVERY", "MEDIUM"):     AE(20, 20, None),
        ("CRASH_RECOVERY", "HIGH"):       AE(15, 20, None),
        ("CRASH_RECOVERY", "CONVICTION"): AE(10, 20, None),
        ("DEFENSIVE", "LOW"):        D,
        ("DEFENSIVE", "MEDIUM"):     D,
        ("DEFENSIVE", "HIGH"):       AE(20, 25, None),
        ("DEFENSIVE", "CONVICTION"): AE(15, 25, None),
        ("CHOPPY", "LOW"):        D,
        ("CHOPPY", "MEDIUM"):     D,
        ("CHOPPY", "HIGH"):       D,
        ("CHOPPY", "CONVICTION"): AE(15, 25, None),
        ("HIGH_VOL", "LOW"):        D,
        ("HIGH_VOL", "MEDIUM"):     AE(20, 20, None),
        ("HIGH_VOL", "HIGH"):       AE(15, 20, None),
        ("HIGH_VOL", "CONVICTION"): AE(10, 20, None),
        ("CAPITULATION", "LOW"):        D,
        ("CAPITULATION", "MEDIUM"):     D,
        ("CAPITULATION", "HIGH"):       D,
        ("CAPITULATION", "CONVICTION"): AE(20, 30, "PEAD_EXTENDED_WINDOW"),
        ("CRISIS", "LOW"):        D,
        ("CRISIS", "MEDIUM"):     D,
        ("CRISIS", "HIGH"):       D,
        ("CRISIS", "CONVICTION"): D,
    }


_FILTER_TABLE = _filter_table()
_GATE_TABLE   = _gate_table()
_AE_TABLE     = _ae_table()
_FALLBACK_FILTER = _FILTER_TABLE[("NORMAL", "MEDIUM")]
_FALLBACK_GATE   = _GATE_TABLE[("NORMAL", "MEDIUM")]


def get_filter_params(regime: str, tier: str) -> FilterParams:
    return _FILTER_TABLE.get((regime, tier), _FALLBACK_FILTER)


def get_gate_params(regime: str, tier: str) -> GateParams:
    return _GATE_TABLE.get((regime, tier), _FALLBACK_GATE)


def get_auto_execute_params(regime: str, tier: str) -> AutoExecuteParams:
    return _AE_TABLE.get((regime, tier), _AE_DISABLED)
