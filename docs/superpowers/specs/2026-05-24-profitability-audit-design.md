# Profitability Audit — Design Spec

**Date:** 2026-05-24
**Status:** Approved for implementation

---

## Problem Statement

Post-implementation audit of the regime×tier system (2026-05-21) against 30d backtest data
reveals five structural gaps that suppress P&L regardless of signal quality:

| # | Gap | Measured cost |
|---|-----|--------------|
| P0 | TP/SL fixed globally — time-decay TP neutralised by `ATR_TP_FLOOR_PCT=3.0%` | −$3,860 (25 SL hits at exactly −3.05%; 26 FC exits at +0–1% wasted) |
| P1 | Swing max-days fixed at 3 (backtest) / 5 (live) — cuts winners before TP2 | 4 positions at +6.7–14% force-exited; SWING_TP2 fires only 2/42 |
| P2 | `build_dynamic_user_context` missing spec-2.7 items | Claude decides TP/SL without knowing entry countdown, concentration, breadth |
| P3 | Learning agent blind to tier/mode; ATR-TP tuning never activates | `atr_pct` field never written to journal; tier_agg never sent to Claude |
| P4 | `_BacktestSnap` empty sentiment → all tiers biased LOW/MEDIUM | Quality scores miss 35/100 pts every bar; tier distribution unrepresentative |

**Core finding:** Day trades are net −$1,240 across 95 trades; swing trades produce +$2,361
across 42 trades. The P0 SL over-width (3% fixed vs practical 1.5–2%) alone explains most
of the day-trade drag.

---

## Architecture

Five independent tracks with a single new data structure (`TPSLParams`) as the foundation.
All tracks extend the existing frozen-dataclass + table-function pattern in `regime_params.py`.

```
Track 1 ── TPSLParams (108 entries: 9 regimes × 4 tiers × 3 modes)
           └─ absorbs SwingParams fields (tp1/tp2/sl/max_days/profit_lock/min_pnl)
           └─ replaces ATR_TP_FLOOR/CEILING global constants
           └─ used by tp_scaler, executor, swing_manager, backtest — single SoT

Track 2 ── Swing promotion wiring (swing_manager + backtest use TPSLParams)
           └─ SwingParams stripped to eligible: bool only

Track 3 ── Learning agent tier/mode feedback
           └─ trade journal gains atr_pct + mode fields
           └─ tier_agg + force_close_rate added to Claude prompt

Track 4 ── Context builder + research completeness
           └─ entry countdown, concentration, flash-crash wired
           └─ capitulation detection gets real drawdown/OBV scoring

Track 5 ── Backtest fidelity
           └─ regime sentiment priors in _BacktestSnap
           └─ per-bar quality (not per-day)
           └─ T2 CHOPPY unlock check
```

---

## Rule Additions

- **Rule 31:** `TPSLParams` in `utils/regime_params.py` is the sole source of truth for
  all TP/SL widths, ATR multipliers, swing max-days, profit-lock thresholds, and swing
  min-pnl. No caller holds inline TP/SL constants. `config.ATR_TP_FLOOR_PCT`,
  `ATR_TP_CEILING_PCT`, `SWING_TP1_PCT`, `SWING_TP2_PCT`, `SWING_PROFIT_LOCK_*`,
  and `SWING_MIN_PNL_*` become documentation-only comments.

---

## Track 1 — `TPSLParams`: 108-entry Regime × Tier × Mode Table

### 1.1 Dataclass

Add to `utils/regime_params.py`, following the existing frozen-dataclass pattern:

```python
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal

Mode   = Literal["DAY_TRADE", "PEAD", "SWING"]
Tier   = Literal["LOW", "MEDIUM", "HIGH", "CONVICTION"]

# Re-exported so tests can parametrize without hardcoding string lists
REGIMES: tuple[str, ...] = (
    "NORMAL", "BULL_TREND", "OPPORTUNITY", "CRASH_RECOVERY",
    "DEFENSIVE", "CHOPPY", "HIGH_VOL", "CAPITULATION", "CRISIS",
)
TIERS: tuple[str, ...] = ("LOW", "MEDIUM", "HIGH", "CONVICTION")
MODES: tuple[str, ...] = ("DAY_TRADE", "PEAD", "SWING")


@dataclass(frozen=True)
class TPSLParams:
    """
    All TP/SL/duration parameters for a (regime, tier, mode) triple.

    For DAY_TRADE and PEAD:
        tp_floor_pct   — minimum TP% (ATR-TP clamped to this floor)
        tp_ceiling_pct — maximum TP% (ATR-TP clamped to this ceiling)
        max_days       — always 0 (intraday; not used)
        profit_lock_pct — always 0.0 (not used)
        min_pnl_pct    — always 0.0 (not used)

    For SWING:
        tp_floor_pct   — TP1 trigger (40% of position closes here)
        tp_ceiling_pct — TP2 trigger (30% closes here; trailing activates on remainder)
        max_days       — trading days to hold before forced EOD exit
        profit_lock_pct — profit-lock bypass threshold for EOD promotion
        min_pnl_pct    — standard EOD promotion minimum P&L
    """
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
        """True for CRISIS / CAPITULATION LOW-MEDIUM / CHOPPY SWING LOW-MEDIUM."""
        return self.atr_mult == 0.0

    def clamp_tp(self, raw_tp: float) -> float:
        return max(self.tp_floor_pct, min(self.tp_ceiling_pct, raw_tp))

    def clamp_sl(self, raw_sl: float) -> float:
        return max(self.sl_floor_pct, min(self.sl_ceiling_pct, raw_sl))
```

### 1.2 DRY Factory Helpers (module-private)

Three lightweight constructors eliminate repetition across 108 entries. The zero-default
fields (`max_days`, `profit_lock_pct`, `min_pnl_pct`) are irrelevant for DAY_TRADE/PEAD,
so the `_dt` and `_pd` helpers omit them entirely:

```python
def _dt(tp_f, tp_c, sl_f, sl_c, atr) -> TPSLParams:
    """DAY_TRADE entry — max_days/profit_lock/min_pnl default to 0."""
    return TPSLParams(tp_f, tp_c, sl_f, sl_c, atr)

def _pd(tp_f, tp_c, sl_f, sl_c, atr) -> TPSLParams:
    """PEAD entry — same shape as DAY_TRADE factory."""
    return TPSLParams(tp_f, tp_c, sl_f, sl_c, atr)

def _sw(tp1, tp2, sl_f, sl_c, atr, days, plock, minpnl) -> TPSLParams:
    """SWING entry — all fields required."""
    return TPSLParams(tp1, tp2, sl_f, sl_c, atr, days, plock, minpnl)

_TPSL_DISABLED = TPSLParams(0.0, 0.0, 0.0, 0.0, 0.0)  # is_disabled → True
```

### 1.3 Disabled-Keys Set (DRY)

Instead of repeating `_TPSL_DISABLED` across 20+ rows, declare the disabled space once:

```python
_DISABLED_COMBOS: frozenset[tuple[str, str, str]] = frozenset({
    # CRISIS — all tiers, all modes
    *(("CRISIS", t, m) for t in TIERS for m in MODES),
    # CAPITULATION — LOW/MEDIUM, all modes
    *(("CAPITULATION", t, m) for t in ("LOW", "MEDIUM") for m in MODES),
    # CHOPPY SWING — LOW/MEDIUM only (HIGH/CONVICTION have unlock)
    ("CHOPPY", "LOW",    "SWING"),
    ("CHOPPY", "MEDIUM", "SWING"),
    # HIGH_VOL SWING — LOW only
    ("HIGH_VOL", "LOW", "SWING"),
})
```

### 1.4 Full 108-Entry Table

Built as a single module-level dict (singleton — created once at import time):

```python
def _build_tpsl_table() -> dict[tuple[str, str, str], TPSLParams]:
    D = _TPSL_DISABLED
    t = {}   # (regime, tier, mode) → TPSLParams

    # ── DAY_TRADE ──────────────────────────────────────────────────────────────
    # Format: _dt(tp_floor, tp_ceiling, sl_floor, sl_ceiling, atr_mult)
    # Rationale: typical intraday ATR ~1.2% → range ~4–5%.
    #            SL tightened from fixed 3.0% to 1.5–2.0% (no overnight gap risk).
    #            TP ceiling regime-aware; floor 1.5% prevents spread/slippage eating gain.

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

    # CHOPPY: quick reversals — tight ceiling, fast exit
    t[("CHOPPY",         "LOW",        "DAY_TRADE")] = _dt(1.5, 2.5, 1.2, 2.0, 1.2)
    t[("CHOPPY",         "MEDIUM",     "DAY_TRADE")] = _dt(1.5, 3.0, 1.2, 1.8, 1.4)
    t[("CHOPPY",         "HIGH",       "DAY_TRADE")] = _dt(2.0, 3.5, 1.2, 1.8, 1.6)
    t[("CHOPPY",         "CONVICTION", "DAY_TRADE")] = _dt(2.0, 4.0, 1.2, 1.5, 1.8)

    # HIGH_VOL: wider SL to survive intraday noise (~2× normal)
    t[("HIGH_VOL",       "LOW",        "DAY_TRADE")] = _dt(1.5, 3.0, 2.0, 3.0, 1.5)
    t[("HIGH_VOL",       "MEDIUM",     "DAY_TRADE")] = _dt(1.5, 3.5, 2.0, 2.5, 1.8)
    t[("HIGH_VOL",       "HIGH",       "DAY_TRADE")] = _dt(2.0, 4.0, 2.0, 2.5, 2.0)
    t[("HIGH_VOL",       "CONVICTION", "DAY_TRADE")] = _dt(2.5, 4.5, 1.8, 2.5, 2.0)

    # CAPITULATION LOW/MEDIUM — disabled; HIGH/CONVICTION bounce plays only
    t[("CAPITULATION",   "HIGH",       "DAY_TRADE")] = _dt(2.0, 5.0, 2.0, 3.5, 2.0)
    t[("CAPITULATION",   "CONVICTION", "DAY_TRADE")] = _dt(2.5, 6.0, 2.0, 3.0, 2.5)

    # ── PEAD ───────────────────────────────────────────────────────────────────
    # Format: _pd(tp_floor, tp_ceiling, sl_floor, sl_ceiling, atr_mult)
    # Rationale: catalyst-driven drift; 3–12% moves in 1–3 days.
    #            SL slightly wider than DAY_TRADE — volatile first bar after earnings.

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
    # Format: _sw(tp1, tp2, sl_floor, sl_ceiling, atr_mult, max_days, profit_lock, min_pnl)
    # Rationale: TP1 = 40% close trigger; TP2 = 30% close + trailing activates.
    #            Data: TP1 cluster at 5.02–6.45% — calibrated. TP2 at 10% barely fires
    #            (2/42 hits) — reduced to 7–12% by regime/tier.
    #            SL wider for overnight gap tolerance (typical gap: 1–3%).
    #            max_days: BULL_TREND CONVICTION 8d vs CHOPPY HIGH 3d.
    #            profit_lock/min_pnl replace SwingParams + unlock system entirely.

    t[("NORMAL",         "LOW",        "SWING")] = _sw(4.0,  8.0,  2.5, 4.0, 2.5, 3, 3.5, 1.0)
    t[("NORMAL",         "MEDIUM",     "SWING")] = _sw(4.5,  8.5,  2.5, 3.5, 3.0, 4, 2.5, 0.5)
    t[("NORMAL",         "HIGH",       "SWING")] = _sw(5.0,  9.0,  2.5, 3.5, 3.2, 5, 2.0, 0.3)
    t[("NORMAL",         "CONVICTION", "SWING")] = _sw(5.5, 10.0,  2.0, 3.0, 3.5, 6, 1.5, 0.2)

    t[("BULL_TREND",     "LOW",        "SWING")] = _sw(4.5,  8.5,  2.5, 4.0, 2.8, 4, 3.0, 0.5)
    t[("BULL_TREND",     "MEDIUM",     "SWING")] = _sw(5.0,  9.0,  2.5, 3.5, 3.2, 5, 2.0, 0.3)
    t[("BULL_TREND",     "HIGH",       "SWING")] = _sw(6.0, 10.5,  2.0, 3.0, 3.5, 7, 1.5, 0.2)
    t[("BULL_TREND",     "CONVICTION", "SWING")] = _sw(7.0, 12.0,  2.0, 3.0, 4.0, 8, 1.0, 0.1)

    t[("OPPORTUNITY",    "LOW",        "SWING")] = _sw(4.0,  8.0,  2.5, 4.0, 2.5, 3, 3.5, 0.8)
    t[("OPPORTUNITY",    "MEDIUM",     "SWING")] = _sw(5.0,  9.0,  2.5, 3.5, 3.0, 5, 2.5, 0.5)
    t[("OPPORTUNITY",    "HIGH",       "SWING")] = _sw(5.5, 10.0,  2.0, 3.5, 3.2, 6, 2.0, 0.3)
    t[("OPPORTUNITY",    "CONVICTION", "SWING")] = _sw(6.0, 11.0,  2.0, 3.0, 3.5, 7, 1.5, 0.2)

    t[("CRASH_RECOVERY", "LOW",        "SWING")] = _sw(3.5,  7.0,  3.0, 5.0, 2.5, 3, 4.0, 1.5)
    t[("CRASH_RECOVERY", "MEDIUM",     "SWING")] = _sw(4.0,  8.0,  2.5, 4.5, 3.0, 4, 3.0, 1.0)
    t[("CRASH_RECOVERY", "HIGH",       "SWING")] = _sw(5.0,  9.0,  2.5, 4.0, 3.2, 5, 2.5, 0.5)
    t[("CRASH_RECOVERY", "CONVICTION", "SWING")] = _sw(5.5, 10.0,  2.5, 3.5, 3.5, 6, 2.0, 0.3)

    t[("DEFENSIVE",      "LOW",        "SWING")] = _sw(3.0,  6.0,  2.5, 4.0, 2.0, 2, 4.0, 1.5)
    t[("DEFENSIVE",      "MEDIUM",     "SWING")] = _sw(3.5,  7.0,  2.5, 4.0, 2.5, 3, 3.5, 1.0)
    t[("DEFENSIVE",      "HIGH",       "SWING")] = _sw(4.0,  8.0,  2.5, 3.5, 2.8, 4, 3.0, 0.5)
    t[("DEFENSIVE",      "CONVICTION", "SWING")] = _sw(5.0,  9.0,  2.0, 3.5, 3.0, 5, 2.5, 0.3)

    # CHOPPY SWING — LOW/MEDIUM disabled; HIGH/CONVICTION via SWING_CHOPPY unlock only
    t[("CHOPPY",         "HIGH",       "SWING")] = _sw(3.5,  7.0,  2.5, 4.0, 2.0, 3, 2.5, 1.0)
    t[("CHOPPY",         "CONVICTION", "SWING")] = _sw(4.0,  8.0,  2.5, 3.5, 2.5, 4, 1.0, 0.5)

    # HIGH_VOL SWING — LOW disabled; wider SL for gap risk
    t[("HIGH_VOL",       "MEDIUM",     "SWING")] = _sw(3.5,  7.0,  3.0, 5.0, 2.5, 2, 3.5, 1.0)
    t[("HIGH_VOL",       "HIGH",       "SWING")] = _sw(4.0,  8.0,  3.0, 4.5, 3.0, 3, 3.0, 0.5)
    t[("HIGH_VOL",       "CONVICTION", "SWING")] = _sw(5.0,  9.5,  2.5, 4.0, 3.2, 4, 2.5, 0.3)

    # CAPITULATION SWING — LOW/MEDIUM disabled; V-bounce recovery needs long window
    t[("CAPITULATION",   "HIGH",       "SWING")] = _sw(6.0, 12.0,  4.0, 6.0, 3.5, 5, 3.0, -5.0)
    t[("CAPITULATION",   "CONVICTION", "SWING")] = _sw(8.0, 15.0,  4.0, 5.5, 4.0, 7, 2.0, -5.0)

    # ── Stamp disabled entries from the declared set ───────────────────────────
    for key in _DISABLED_COMBOS:
        t[key] = _TPSL_DISABLED

    return t


# Singleton — built once at import, never mutated
_TPSL_TABLE: dict[tuple[str, str, str], TPSLParams] = _build_tpsl_table()

_TPSL_FALLBACK = _TPSL_TABLE[("NORMAL", "MEDIUM", "DAY_TRADE")]


def get_tp_sl_params(regime: str, tier: str, mode: str) -> TPSLParams:
    """
    Single SoT for TP/SL/duration parameters — used by live trading AND backtest.

    Falls back to NORMAL/MEDIUM/DAY_TRADE on unknown keys so a bad regime string
    never raises at trade-time. Callers must check `.is_disabled` before placing orders.
    """
    return _TPSL_TABLE.get((regime, tier, mode), _TPSL_FALLBACK)
```

### 1.5 `SwingParams` Simplified

`SwingParams` retains only `eligible: bool`. All other fields move to `TPSLParams`:

```python
@dataclass(frozen=True)
class SwingParams:
    eligible: bool   # regime allows swing promotion at all
```

The `_SWING_TABLE` dict and `get_swing_params()` shrink accordingly. Callers that read
`sp.tp1_pct`, `sp.tp2_pct`, `sp.sl_pct`, `sp.profit_lock_pct`, `sp.min_pnl_pct` are
migrated to `get_tp_sl_params(regime, tier, "SWING")`.

### 1.6 Updated `compute_tp_sl()` in `signals/tp_scaler.py`

```python
from utils.regime_params import get_tp_sl_params, TPSLParams

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

    base_tp is the ATR-derived suggestion (from compute_atr_tp_pct).
    The table's atr_mult replaces the global ATR_TP_MULT_DEFAULT.
    Time-decay is applied after the ATR scaling; the result is clamped
    to the regime×tier×mode floor/ceiling, not global constants.
    """
    p: TPSLParams = get_tp_sl_params(regime, tier, mode)
    if p.is_disabled:
        raise ValueError(
            f"compute_tp_sl called for disabled combo ({regime}, {tier}, {mode})"
        )
    time_mult  = _time_multiplier(entry_time)
    raw_tp     = base_tp * p.atr_mult * time_mult
    raw_sl     = base_sl
    return p.clamp_tp(raw_tp), p.clamp_sl(raw_sl)
```

`ATR_TP_FLOOR_PCT` and `ATR_TP_CEILING_PCT` are no longer read inside this function.
The time-decay table (`_time_multiplier`) is unchanged.

---

## Track 2 — Swing Promotion Wiring

### 2.1 `trading/swing_manager.py` — `evaluate_eod_holds()`

Replace `get_swing_params(regime)` + `get_effective_profit_lock(regime, quality)` with:

```python
tier = pos.get("quality_tier", "MEDIUM")
p    = get_tp_sl_params(regime, tier, "SWING")

# Disabled combo check — defense in depth
if p.is_disabled:
    continue

profit_lock_bypass = (pnl_pct >= p.profit_lock_pct and regime != "CRISIS")
standard_qualify   = (
    get_swing_params(regime).eligible
    and pnl_pct >= p.min_pnl_pct
    and pos.get("composite", 0) >= config.BACKTEST_SWING_COMPOSITE_MIN
)

if profit_lock_bypass or standard_qualify:
    pos["is_swing"]              = True
    pos["swing_days_remaining"]  = p.max_days   # dynamic, not SWING_MAX_DAYS
    pos["tp1_pct"]               = p.tp_floor_pct
    pos["tp2_pct"]               = p.tp_ceiling_pct
    pos["stop_loss_pct"]         = p.sl_floor_pct
```

`get_effective_profit_lock()` is deleted. Its two call sites (here and backtest) use the
table directly.

### 2.2 `backtest/simulator.py` — `_evaluate_swing_promotion()`

Identical migration. `BACKTEST_SWING_MAX_DAYS` constant is retired. `_init_tiered_swing_fields()`
reads `tp_floor_pct` / `tp_ceiling_pct` / `sl_floor_pct` from `TPSLParams` instead of
`SwingParams`.

---

## Track 3 — Learning Agent Tier Feedback

### 3.1 Trade Journal — New Fields

`trading/executor.py` writes two new fields to `open_positions.json` at order fill:

```python
position["mode"]    = mode          # "DAY_TRADE" | "PEAD" | "SWING"
position["atr_pct"] = base_tp_pct  # ATR% at entry — raw, before mult/clamp
```

`trading/journal.py` propagates both fields to `daily_closed.json` on close.
No schema change to existing fields — additions only.

### 3.2 `learning/trade_analysis.py` — New Helper (Single SoT)

```python
def _aggregate_by_tier(trades: list[dict]) -> dict[str, dict]:
    """
    Win rate, avg P&L, count, avg atr_pct per quality tier.
    Mirrors _aggregate_by_regime — same shape, same JSON structure.
    """
    result: dict[str, dict] = {}
    for tier in ("LOW", "MEDIUM", "HIGH", "CONVICTION"):
        bucket = [t for t in trades if t.get("quality_tier") == tier]
        if not bucket:
            continue
        wins   = [t for t in bucket if (t.get("pnl_pct") or 0) > 0]
        result[tier] = {
            "count":       len(bucket),
            "win_rate":    len(wins) / len(bucket),
            "avg_pnl_pct": sum(t.get("pnl_pct", 0) for t in bucket) / len(bucket),
            "avg_atr_pct": sum(t.get("atr_pct", 0) for t in bucket) / len(bucket),
            "force_close_rate": sum(
                1 for t in bucket if t.get("exit_reason") == "FORCE_CLOSE"
            ) / len(bucket),
        }
    return result
```

### 3.3 Learning Agent — Step 1 Prompt Addition

```python
tier_agg       = _aggregate_by_tier(closed_trades)
force_close_rt = sum(
    1 for t in closed_trades if t.get("exit_reason") == "FORCE_CLOSE"
) / len(closed_trades) if closed_trades else 0.0

# Added to existing Step 1 user message:
f"<performance_by_tier>{json.dumps(tier_agg)}</performance_by_tier>\n\n"
f"<force_close_rate>{force_close_rt:.1%}</force_close_rate>\n\n"
```

Claude can now distinguish "MEDIUM tier is 38% win rate vs HIGH tier 61% win rate" and
tighten composite thresholds for MEDIUM specifically, rather than blunt regime-level moves.

### 3.4 Zero-Trade Param Relaxation — History-Driven Selection

Replace the `weekday % 3` rotation with constraint-score selection:

```python
def _most_constrained_param(params: dict) -> str:
    """
    Return the param name whose current value is closest to its upper bound
    (i.e., most restrictive). This targets the actual bottleneck, not an
    arbitrary rotation.
    """
    scores: dict[str, float] = {}
    for key, (lo, hi) in _ZERO_TRADE_BOUNDS.items():
        val = float(params.get(key, hi))
        # Normalised distance to upper bound: 1.0 = at maximum restriction
        scores[key] = (val - lo) / (hi - lo) if hi != lo else 0.0
    return max(scores, key=lambda k: scores[k])
```

`_handle_zero_trade_day()` calls `_most_constrained_param()` instead of
`param_order[weekday % len(param_order)]`.

---

## Track 4 — Context Builder + Research Agent

### 4.1 `trading/context_builder.py` — Three Missing Items

`build_dynamic_user_context(now, portfolio_gain_pct)` extended with three lines.
Signature unchanged — no caller updates needed:

```python
# Portfolio sector concentration
from trading.journal import load_open_positions
from sectors import get_sector_etf
open_pos  = load_open_positions()
held      = [get_sector_etf(s) for s in open_pos]
conc_line = (
    "Positions: " + ", ".join(
        f"{s}×{held.count(s)}" for s in sorted(set(held))
    ) if held else "Positions: none"
)

# Entry window countdown (uses regime from CycleSnapshot via module-level cache)
from macro.regime import get_cached_regime
regime       = get_cached_regime()
hard_close_h = get_entry_params(regime).hard_close_hour
remaining_m  = max(0, hard_close_h * 60 - now.hour * 60 - now.minute)
window_line  = f"Entry window: {remaining_m // 60}h{remaining_m % 60:02d}m remaining"

# Flash-crash status
from utils.market_monitor import is_flash_crash_active
flash_line = (
    "Flash crash: ACTIVE — entries blocked"
    if is_flash_crash_active()
    else "Flash crash: clear"
)
```

All three appended to the return string before the closing newline.

### 4.2 `research/agent.py` — Real Capitulation Detection

The current proxy (sentiment < −30) is replaced with a proper scoring pass. For any
symbol already flagged by the sentiment check, fetch 20 daily bars (reuses the existing
`yahoo_get` session — no new dependencies):

```python
def _score_capitulation_candidate(symbol: str, bars: pd.DataFrame) -> bool:
    """
    True if symbol shows real capitulation signals beyond negative sentiment.
    Requires: drawdown from 20d high >= 15%, OBV trend declining.
    Pure function — caller supplies bars; no I/O inside.
    """
    if bars.empty or len(bars) < 5:
        return False
    high_20d = bars["high"].rolling(20).max().iloc[-1]
    current  = float(bars["close"].iloc[-1])
    drawdown = (high_20d - current) / high_20d if high_20d > 0 else 0.0
    obv      = (bars["close"].diff().apply(lambda x: 1 if x > 0 else -1) * bars["volume"]).cumsum()
    obv_trend_down = float(obv.iloc[-1]) < float(obv.iloc[-5])
    return drawdown >= 0.15 and obv_trend_down

def _fetch_daily_bars_for_cap_check(symbols: list[str]) -> dict[str, pd.DataFrame]:
    """
    Batch-fetch 20d daily OHLCV for capitulation candidate symbols.
    Uses yahoo_get session — no new HTTP dependency.
    Returns empty DataFrame for any symbol that fails to fetch.
    """
    from data.market_data import get_daily_bars
    result: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        try:
            result[sym] = get_daily_bars(sym, days=20)
        except Exception:
            result[sym] = pd.DataFrame()
    return result
```

`_score_capitulation_candidate` is pure (Rule 15) — all I/O happens in the fetcher,
keeping the scorer testable in isolation.

### 4.3 `utils/ticker_quality.py` — Intraday Staleness Guard

Inside `compute_ticker_quality()`, before `intra_sent` is used:

```python
intra_ts    = intra_data.get("timestamp")
intra_fresh = _is_fresh(intra_ts, config.INTRADAY_NEWS_STALE_MINUTES)
intra_sent  = intra_data.get("sentiment_score", sentiment) if intra_fresh else 0

if not intra_fresh:
    gaps = (*gaps, "intraday_stale")
```

`_is_fresh(ts_str, max_minutes)` is a private pure helper — parses ISO timestamp,
computes delta vs `datetime.now(ET)`, returns bool. No I/O.

New config constant (under `# ── Data Resilience ──`):
```python
INTRADAY_NEWS_STALE_MINUTES = 45
```

---

## Track 5 — Backtest Fidelity

### 5.1 `_BacktestSnap` — Regime Sentiment Priors

Replace empty dicts with regime-calibrated priors so tier distribution matches live:

```python
# Module-level constant — single SoT shared with any future backtest variants
_REGIME_SENTIMENT_PRIOR: dict[str, int] = {
    "BULL_TREND":     40,
    "OPPORTUNITY":    35,
    "NORMAL":         15,
    "CRASH_RECOVERY": 10,
    "DEFENSIVE":       0,
    "CHOPPY":         -5,
    "HIGH_VOL":      -10,
    "CAPITULATION":  -20,
    "CRISIS":        -40,
}


@dataclass
class _BacktestSnap:
    regime: str
    _watch_list: list[str] = field(default_factory=list)

    @cached_property
    def research(self) -> dict:
        prior = _REGIME_SENTIMENT_PRIOR.get(self.regime, 0)
        return {sym: {"sentiment_score": prior} for sym in self._watch_list}

    @cached_property
    def intraday_news(self) -> dict:
        prior = _REGIME_SENTIMENT_PRIOR.get(self.regime, 0)
        return {sym: {"sentiment_score": prior} for sym in self._watch_list}
```

`cached_property` ensures the dicts are built once per snap instance, not on every
`compute_ticker_quality` call.

### 5.2 Per-Bar Quality Computation

`_build_quality_map()` is deleted. Quality is computed inline in the entry-signal loop:

```python
# Inside per-bar entry scan (replaces quality_map.get(sym)):
iv = precomp.get(sym, {}).get(ts)
if iv is None:
    continue
quality = compute_ticker_quality(
    sym, iv, _snap, derive_mode(sym, pead_ok=False),
)
tier = quality.tier
if not _sim_entry_signal_passes(iv, regime, quality, p, rsi_max):
    continue
```

`_snap` is the day's `_BacktestSnap` — shared across all symbols for that bar.
Per-bar computation matches live behaviour where `compute_ticker_quality` is called
once per trading cycle per symbol.

### 5.3 T2 CHOPPY Unlock Check

```python
def _sim_tranche2_gates_pass(pos, iv, drop_pct, regime, now, quality=None) -> bool:
    ...
    if regime == "CHOPPY":
        if quality is None or "T2_CHOPPY" not in quality.unlocks:
            return False
    ...
```

`quality` is now available per-bar (Track 5.2), so this is a one-line guard on an
existing function.

---

## Python Design Principles Applied

| Principle | Implementation |
|-----------|---------------|
| **Single SoT** | `_TPSL_TABLE` in `regime_params.py` — one dict, one lookup function, consumed identically by live code and backtest |
| **Singleton** | `_TPSL_TABLE` is a module-level constant built once at import; `get_tp_sl_params()` is a pure accessor — no mutable state |
| **DRY** | `_dt()` / `_pd()` / `_sw()` factory helpers remove 108-row repetition; `_DISABLED_COMBOS` frozenset generates disabled sentinels without per-row duplication |
| **OOP** | `TPSLParams.is_disabled` property encapsulates sentinel check; `clamp_tp()` / `clamp_sl()` methods keep clamping co-located with the data |
| **Frozen dataclasses** | `TPSLParams` is immutable — safe to share across threads, safe to use as dict values, no accidental mutation |
| **Type literals** | `REGIMES`, `TIERS`, `MODES` tuples exported for caller type hints and test parametrize — single declaration drives both |
| **Fallback safety** | `get_tp_sl_params()` never raises on unknown keys — falls back to NORMAL/MEDIUM/DAY_TRADE, logs warning |
| **Separation of concerns** | `TPSLParams` holds data; `compute_tp_sl()` holds computation; `get_tp_sl_params()` holds lookup — three distinct responsibilities |

---

## Testing Strategy

### Single SoT for Tests

`REGIMES`, `TIERS`, `MODES` tuples exported from `utils/regime_params.py` are the
canonical iteration targets for both parametrised tests and production code:

```python
# tests/test_tpsl_params.py
import pytest
from utils.regime_params import get_tp_sl_params, _TPSL_DISABLED, REGIMES, TIERS, MODES

ALL_COMBOS = [(r, t, m) for r in REGIMES for t in TIERS for m in MODES]
ENABLED_COMBOS = [
    (r, t, m) for r, t, m in ALL_COMBOS
    if not get_tp_sl_params(r, t, m).is_disabled
]

@pytest.mark.unit
@pytest.mark.parametrize("regime,tier,mode", ALL_COMBOS)
def test_no_missing_entries(regime, tier, mode):
    assert get_tp_sl_params(regime, tier, mode) is not None

@pytest.mark.unit
@pytest.mark.parametrize("regime,tier,mode", ENABLED_COMBOS)
def test_rr_positive(regime, tier, mode):
    p = get_tp_sl_params(regime, tier, mode)
    assert p.tp_floor_pct >= p.sl_floor_pct, (
        f"{regime}/{tier}/{mode}: tp_floor={p.tp_floor_pct} < sl_floor={p.sl_floor_pct}"
    )

@pytest.mark.unit
@pytest.mark.parametrize("regime,tier,mode", ENABLED_COMBOS)
def test_ceiling_above_floor(regime, tier, mode):
    p = get_tp_sl_params(regime, tier, mode)
    assert p.tp_ceiling_pct > p.tp_floor_pct
    assert p.sl_ceiling_pct >= p.sl_floor_pct

@pytest.mark.unit
@pytest.mark.parametrize("tier,mode", [(t, m) for t in TIERS for m in MODES])
def test_crisis_all_disabled(tier, mode):
    assert get_tp_sl_params("CRISIS", tier, mode).is_disabled

@pytest.mark.unit
@pytest.mark.parametrize("mode", MODES)
def test_capitulation_low_medium_disabled(mode):
    assert get_tp_sl_params("CAPITULATION", "LOW",    mode).is_disabled
    assert get_tp_sl_params("CAPITULATION", "MEDIUM", mode).is_disabled

@pytest.mark.unit
def test_swing_max_days_increases_with_conviction():
    for regime in ("NORMAL", "BULL_TREND", "OPPORTUNITY"):
        days = [
            get_tp_sl_params(regime, t, "SWING").max_days
            for t in TIERS
            if not get_tp_sl_params(regime, t, "SWING").is_disabled
        ]
        assert days == sorted(days), f"{regime}: max_days not monotone with tier"

@pytest.mark.unit
def test_bull_trend_conviction_swing_widest():
    p = get_tp_sl_params("BULL_TREND", "CONVICTION", "SWING")
    assert p.max_days == 8
    assert p.tp_ceiling_pct == 12.0

@pytest.mark.unit
def test_fallback_on_unknown_regime():
    p = get_tp_sl_params("UNKNOWN_REGIME", "MEDIUM", "DAY_TRADE")
    assert not p.is_disabled   # fallback, not disabled
```

Existing test files extended (no new files except `test_tpsl_params.py`):
- `test_backtest_simulator.py` — per-bar quality, max_days from table, T2 CHOPPY unlock
- `test_swing.py` — swing promotion reads profit_lock/min_pnl from table
- `test_learning.py` — `_aggregate_by_tier()`, `_most_constrained_param()`
- `test_pipeline.py` — `compute_tp_sl()` mode param; `build_dynamic_user_context` additions

---

## Config Changes

Under `# ── Plan: Profitability Audit ──` in `config.py`:

```python
# New constant
INTRADAY_NEWS_STALE_MINUTES = 45

# Retired — documentation only; replaced by TPSLParams table in regime_params.py
# ATR_TP_FLOOR_PCT    = 3.0   → per (regime, tier, mode) tp_floor_pct
# ATR_TP_CEILING_PCT  = 12.0  → per (regime, tier, mode) tp_ceiling_pct
# SWING_TP1_PCT       = 5.0   → per (regime, tier) tp_floor_pct in SWING rows
# SWING_TP2_PCT       = 10.0  → per (regime, tier) tp_ceiling_pct in SWING rows
# SWING_PROFIT_LOCK_* → per (regime, tier) profit_lock_pct in SWING rows
# SWING_MIN_PNL_*     → per (regime, tier) min_pnl_pct in SWING rows
# BACKTEST_SWING_MAX_DAYS → per (regime, tier) max_days in SWING rows
```

---

## Files Created / Modified

| File | Change |
|------|--------|
| `utils/regime_params.py` | Add `TPSLParams`, `_dt/_pd/_sw`, `_DISABLED_COMBOS`, `_TPSL_TABLE`, `get_tp_sl_params()`. Export `REGIMES/TIERS/MODES`. Strip `SwingParams` to `eligible` only. Delete `get_effective_profit_lock()`. |
| `signals/tp_scaler.py` | Add `mode` param to `compute_tp_sl()`. Remove global floor/ceiling clamp. Use `TPSLParams.clamp_tp/clamp_sl`. |
| `trading/swing_manager.py` | Replace `get_swing_params()` + unlock logic with `get_tp_sl_params()`. Use `p.max_days` at promotion. |
| `trading/executor.py` | Write `mode` + `atr_pct` to position on open. |
| `trading/journal.py` | Propagate `mode` + `atr_pct` to `daily_closed.json` on close. |
| `trading/context_builder.py` | Add concentration, entry countdown, flash-crash to `build_dynamic_user_context`. |
| `learning/trade_analysis.py` | Add `_aggregate_by_tier()`. Replace `weekday % 3` with `_most_constrained_param()`. |
| `learning/agent.py` | Pass `tier_agg` + `force_close_rate` to Step 1 prompt. |
| `research/agent.py` | Add `_fetch_daily_bars_for_cap_check()` + `_score_capitulation_candidate()`. |
| `utils/ticker_quality.py` | Add `_is_fresh()` helper + intraday staleness guard in `compute_ticker_quality()`. |
| `backtest/simulator.py` | `_BacktestSnap` with `cached_property` sentiment priors. Delete `_build_quality_map()`. Per-bar quality in entry loop. T2 CHOPPY unlock check. |
| `config.py` | Add `INTRADAY_NEWS_STALE_MINUTES`. Retire global TP/SL/swing constants to doc comments. |
| `tests/test_tpsl_params.py` | CREATE — 108-entry parametrised smoke tests using `REGIMES/TIERS/MODES` as single SoT. |
| `tests/test_backtest_simulator.py` | Extend — per-bar quality, dynamic max_days, T2 unlock. |
| `tests/test_swing.py` | Extend — profit_lock/min_pnl from table. |
| `tests/test_learning.py` | Extend — tier aggregation, constrained param selection. |
| `tests/test_pipeline.py` | Extend — `compute_tp_sl` mode param, context builder additions. |

---

## Backtest Validation Target

After all 5 tracks implemented, run `backtest.py --days 30`:

| Metric | Baseline | Target |
|--------|----------|--------|
| Force-close rate | 31.4% | ≤ 25% |
| Day trade P&L | −$1,240 | ≥ −$400 |
| Swing SWING_TP2 hits | 2 / 42 (4.8%) | ≥ 8 / estimated-42 (19%+) |
| Total P&L | +$1,120 | ≥ +$2,500 |
| SL avg loss | −$154 (−3.05%) | ≥ −$90 (−1.8%) |
