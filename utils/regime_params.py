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


@dataclass(frozen=True)
class AutoExecuteParams:
    composite_offset: int       # 999 = disabled
    sentiment_min: int
    extra_unlock: str | None    # named unlock required, or None


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
        "NORMAL":         SwingParams(True,  0.5,  2.5, 5.0,  10.0, config.SWING_OVERNIGHT_SL_PCT),
        "BULL_TREND":     SwingParams(True,  0.3,  2.0, 5.0,  10.0, config.SWING_OVERNIGHT_SL_PCT),
        "OPPORTUNITY":    SwingParams(True,  0.5,  2.5, 5.0,  10.0, config.SWING_OVERNIGHT_SL_PCT),
        "CRASH_RECOVERY": SwingParams(True,  1.0,  2.5, 5.0,  10.0, config.SWING_OVERNIGHT_SL_PCT),
        "CAPITULATION":   SwingParams(True,  -5.0, _SENTINEL, 10.0, 20.0, config.SWING_SL_PCT_CAPITULATION),
        "CHOPPY":         SwingParams(False, 0.5,  2.5, 5.0,  10.0, config.SWING_OVERNIGHT_SL_PCT),
        "DEFENSIVE":      SwingParams(False, 0.5,  3.0, 5.0,  10.0, config.SWING_OVERNIGHT_SL_PCT),
        "HIGH_VOL":       SwingParams(False, 0.5,  3.0, 5.0,  10.0, config.SWING_OVERNIGHT_SL_PCT),
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
    offsets = {"LOW": +10, "MEDIUM": 0, "HIGH": -5, "CONVICTION": -10}
    table = {}
    for regime, (base_comp, sent_block, intra_block) in base.items():
        for tier, offset in offsets.items():
            table[(regime, tier)] = FilterParams(
                composite_min=max(0, base_comp + offset),
                sentiment_hard_block=sent_block,
                intraday_block=intra_block,
            )
    return table


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
