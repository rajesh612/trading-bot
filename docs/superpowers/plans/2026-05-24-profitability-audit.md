# Profitability Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix six structural P&L gaps (fixed TP/SL, static swing days, blind learning agent, empty backtest sentiment, incomplete FRED signals, missing Claude context) via a 7-track unified refactor.

**Architecture:** `TPSLParams` (144-entry frozen-dataclass table) replaces all inline TP/SL/swing constants; `derive_mode()` is extended to a 5-mode taxonomy; `trading/modes.py` is retired; FRED expands from 3 → 7 series with Rule 4 compliance.

**Tech Stack:** Python 3.11, frozen dataclasses, pytest, `requests.Session`, FRED API

---

## File Map

| File | Action |
|------|--------|
| `config.py` | Add CATALYST_*, INTRADAY_NEWS_STALE_MINUTES, FRED_* constants; **DELETE** retired TP/SL/swing constants |
| `utils/regime_params.py` | Add `TPSLParams` + 144-entry table + `get_tp_sl_params()`; add `max_position_pct` to `EntryParams`; strip `SwingParams` to `eligible: bool` only |
| `utils/ticker_quality.py` | Extend `derive_mode()` to 5-mode; restore `mode` in `_compute_unlocks`; add `_is_fresh()` + staleness guard; add `fred_cache` param + `_score_sector_fred()` |
| `signals/entry_gates.py` | Add `is_catalyst_active(symbol, snap)` pure gate |
| `signals/tp_scaler.py` | Add `mode` param to `compute_tp_sl()`; replace global floor/ceiling with `TPSLParams.clamp_tp/clamp_sl` |
| `trading/cycle_context.py` | Add `post_event_window`, `catalyst_surprise_score`, `fred_cache` already exists |
| `trading/cycle.py` | Populate `snap.post_event_window` + `snap.catalyst_surprise_score` |
| `trading/pre_claude_filter.py` | Add `catalyst_active` param; call `derive_mode()` instead of hardcoded `"DAY_TRADE"` |
| `trading/pipeline.py` | Compute `catalyst_active` per symbol; pass to filter |
| `trading/executor.py` | Replace `get_active_mode()` with `derive_mode()`; add CATALYST position limit; write `mode`+`atr_pct` on open |
| `trading/position_sizer.py` | Replace `_MODE_MAX_PCT.get(mode)` with `get_entry_params(regime).max_position_pct`; remove `mode` param |
| `trading/claude_caller.py` | Replace `get_active_mode()` with `derive_mode()` |
| `trading/swing_manager.py` | Replace `get_swing_params()+get_effective_profit_lock()` with `get_tp_sl_params()`; use `p.max_days` |
| `trading/journal.py` | Propagate `mode`+`atr_pct` to `daily_closed.json` on close |
| `trading/context_builder.py` | Add concentration, entry countdown, flash-crash lines |
| `trading/modes.py` | **DELETE** |
| `macro/fred_cache.py` | Expand to 7 series; fix CPI 12-month YoY; Rule 4 Session; expand modifier ±15; add `load_fred_cache()` |
| `macro/regime.py` | Add `_apply_fred_overrides()` yield-curve hard DEFENSIVE override |
| `macro/economic_calendar.py` | Add `get_post_event_window(now)`; add `get_catalyst_surprise_score(event_type)` |
| `learning/trade_analysis.py` | Add `_aggregate_by_tier()`; replace `weekday%3` with `_most_constrained_param()` |
| `learning/agent.py` | Pass `tier_agg` + `force_close_rate` to Step 1 prompt |
| `research/agent.py` | Add `_fetch_daily_bars_for_cap_check()` + `_score_capitulation_candidate()` |
| `backtest/simulator.py` | `_BacktestSnap` sentiment priors; delete `_build_quality_map()`; per-bar quality; T2 CHOPPY unlock |
| `tests/test_tpsl_params.py` | CREATE — 144-entry parametrised tests |
| `tests/test_catalyst.py` | CREATE — is_catalyst_active, derive_mode 5-mode, get_post_event_window |
| `tests/test_fred.py` | CREATE — CPI YoY, yield curve override, modifier range, surprise score |
| `tests/test_backtest_simulator.py` | Extend — sentiment priors, per-bar quality, T2 unlock |
| `tests/test_swing.py` | Extend — profit_lock/min_pnl from TPSLParams |
| `tests/test_learning.py` | Extend — tier aggregation, constrained param |
| `tests/test_pipeline.py` | Extend — compute_tp_sl mode param, context builder, CATALYST filter path |

---

## Task 1: Config Additions

**Files:**
- Modify: `config.py`

- [ ] **Step 1: Add all new constants to config.py under a new header**

Find the last `# ──` section header in `config.py` and append:

```python
# ── Plan: Profitability Audit ──────────────────────────────────────────────────

# Track 0 — CATALYST mode
CATALYST_SETTLE_MINUTES     = 30     # min after event before CATALYST entries open
CATALYST_WINDOW_MINUTES     = 120    # post_event_window lookback; expires after this
CATALYST_ETF_MIN_MOVE_PCT   = 0.5   # sub-sector ETF must move >= this % to confirm
CATALYST_MAX_POSITIONS      = 2     # max simultaneous CATALYST-mode open positions
CATALYST_MIN_SURPRISE_SCORE = -0.1  # FRED surprise gate; negative still passes

# Track 4 — intraday staleness
INTRADAY_NEWS_STALE_MINUTES = 45

# Track 6 — FRED signal enrichment
FRED_CPI_TARGET_PCT              = 2.0    # Fed inflation target
FRED_CPI_OBS_LOOKBACK            = 13     # CPIAUCSL observations for 12-month YoY
FRED_YIELD_CURVE_SERIES          = "T10Y2Y"
FRED_CREDIT_SPREAD_SERIES        = "BAMLH0A0HYM2"
FRED_BREAKEVEN_SERIES            = "T10YIE"
FRED_CLAIMS_SERIES               = "ICSA"
FRED_YIELD_CURVE_INVERSION_THRESHOLD = 0.0   # T10Y2Y < 0 → inverted → DEFENSIVE
FRED_CREDIT_SPREAD_RISK_OFF      = 500    # bps >= this → risk-off modifier -5
FRED_BREAKEVEN_HIGH_THRESHOLD    = 3.0   # % breakeven > this → modifier -2
FRED_CLAIMS_SPIKE_THRESHOLD      = 30000 # WoW initial claims increase >= this
FRED_MODIFIER_MIN                = -15   # expanded clamp (was effectively ±8)
FRED_MODIFIER_MAX                = 15

```

- [ ] **Step 1b: DELETE retired config constants (hard delete — not comment-out)**

Grep first to confirm no remaining live callers outside `config.py`:
```bash
grep -rn "ATR_TP_FLOOR_PCT\|ATR_TP_CEILING_PCT\|SWING_TP1_PCT\|SWING_TP2_PCT\|SWING_MIN_PNL\|SWING_PROFIT_LOCK\|BACKTEST_SWING_MAX_DAYS" --include="*.py" . | grep -v "^config\.py"
```
If any callers remain, update them to use `get_tp_sl_params()` before deleting. Then delete these lines from `config.py`:
- `ATR_TP_FLOOR_PCT` and `ATR_TP_CEILING_PCT`
- `SWING_TP1_PCT`, `SWING_TP2_PCT`, `SWING_TP1_PCT_CAPITULATION`, `SWING_TP2_PCT_CAPITULATION`
- `SWING_MIN_PNL_PCT_CAPITULATION`, `SWING_MIN_PNL_NORMAL`, `SWING_MIN_PNL_BULL`, `SWING_MIN_PNL_CRASH`
- `SWING_PROFIT_LOCK_NORMAL`, `SWING_PROFIT_LOCK_BULL`, `SWING_PROFIT_LOCK_DEFENSIVE`
- `BACKTEST_SWING_MAX_DAYS`

- [ ] **Step 2: Verify config loads cleanly**

```bash
.venv/bin/python3 -c "import config; print('ok')"
```
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "config: add CATALYST, INTRADAY_NEWS_STALE, FRED enrichment constants"
```

---

## Task 2: TPSLParams Table + EntryParams.max_position_pct + SwingParams Strip

**Files:**
- Modify: `utils/regime_params.py`
- Create: `tests/test_tpsl_params.py`

- [ ] **Step 1: Write failing tests for TPSLParams**

Create `tests/test_tpsl_params.py`:

```python
import pytest
from utils.regime_params import (
    get_tp_sl_params, TPSLParams, REGIMES, TIERS, MODES, get_entry_params,
)

ALL_COMBOS     = [(r, t, m) for r in REGIMES for t in TIERS for m in MODES]
ENABLED_COMBOS = [
    (r, t, m) for r, t, m in ALL_COMBOS
    if not get_tp_sl_params(r, t, m).is_disabled
]


@pytest.mark.unit
@pytest.mark.parametrize("regime,tier,mode", ALL_COMBOS)
def test_no_missing_entries(regime, tier, mode):
    p = get_tp_sl_params(regime, tier, mode)
    assert p is not None


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
def test_swing_max_days_monotone():
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
    assert not p.is_disabled


@pytest.mark.unit
@pytest.mark.parametrize("regime", REGIMES)
def test_entry_params_max_position_pct(regime):
    p = get_entry_params(regime)
    assert 0.0 <= p.max_position_pct <= 0.05


@pytest.mark.unit
def test_swing_params_stripped():
    from utils.regime_params import get_swing_params
    sp = get_swing_params("NORMAL")
    assert hasattr(sp, "eligible")
    # must NOT have old fields that moved to TPSLParams
    assert not hasattr(sp, "tp1_pct")
    assert not hasattr(sp, "profit_lock_pct")
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
.venv/bin/python -m pytest tests/test_tpsl_params.py -q 2>&1 | head -20
```
Expected: ImportError on `TPSLParams, REGIMES, TIERS, MODES`

- [ ] **Step 3: Add TPSLParams dataclass + factory helpers + constants to regime_params.py**

At the top of `utils/regime_params.py`, after the imports, add:

```python
from typing import Literal

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
```

- [ ] **Step 4: Add `_build_tpsl_table()` + singleton + `get_tp_sl_params()` to regime_params.py**

After the disabled combos definition, add:

```python
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

    t[("HIGH_VOL",       "LOW",        "DAY_TRADE")] = _dt(1.5, 3.0, 2.0, 3.0, 1.5)
    t[("HIGH_VOL",       "MEDIUM",     "DAY_TRADE")] = _dt(1.5, 3.5, 2.0, 2.5, 1.8)
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
    t[("NORMAL",         "LOW",        "SWING")] = _sw(4.0,  8.0, 2.5, 4.0, 2.5, 3, 3.5, 1.0)
    t[("NORMAL",         "MEDIUM",     "SWING")] = _sw(4.5,  8.5, 2.5, 3.5, 3.0, 4, 2.5, 0.5)
    t[("NORMAL",         "HIGH",       "SWING")] = _sw(5.0,  9.0, 2.5, 3.5, 3.2, 5, 2.0, 0.3)
    t[("NORMAL",         "CONVICTION", "SWING")] = _sw(5.5, 10.0, 2.0, 3.0, 3.5, 6, 1.5, 0.2)

    t[("BULL_TREND",     "LOW",        "SWING")] = _sw(4.5,  8.5, 2.5, 4.0, 2.8, 4, 3.0, 0.5)
    t[("BULL_TREND",     "MEDIUM",     "SWING")] = _sw(5.0,  9.0, 2.5, 3.5, 3.2, 5, 2.0, 0.3)
    t[("BULL_TREND",     "HIGH",       "SWING")] = _sw(6.0, 10.5, 2.0, 3.0, 3.5, 7, 1.5, 0.2)
    t[("BULL_TREND",     "CONVICTION", "SWING")] = _sw(7.0, 12.0, 2.0, 3.0, 4.0, 8, 1.0, 0.1)

    t[("OPPORTUNITY",    "LOW",        "SWING")] = _sw(4.0,  8.0, 2.5, 4.0, 2.5, 3, 3.5, 0.8)
    t[("OPPORTUNITY",    "MEDIUM",     "SWING")] = _sw(5.0,  9.0, 2.5, 3.5, 3.0, 5, 2.5, 0.5)
    t[("OPPORTUNITY",    "HIGH",       "SWING")] = _sw(5.5, 10.0, 2.0, 3.5, 3.2, 6, 2.0, 0.3)
    t[("OPPORTUNITY",    "CONVICTION", "SWING")] = _sw(6.0, 11.0, 2.0, 3.0, 3.5, 7, 1.5, 0.2)

    t[("CRASH_RECOVERY", "LOW",        "SWING")] = _sw(3.5,  7.0, 3.0, 5.0, 2.5, 3, 4.0, 1.5)
    t[("CRASH_RECOVERY", "MEDIUM",     "SWING")] = _sw(4.0,  8.0, 2.5, 4.5, 3.0, 4, 3.0, 1.0)
    t[("CRASH_RECOVERY", "HIGH",       "SWING")] = _sw(5.0,  9.0, 2.5, 4.0, 3.2, 5, 2.5, 0.5)
    t[("CRASH_RECOVERY", "CONVICTION", "SWING")] = _sw(5.5, 10.0, 2.5, 3.5, 3.5, 6, 2.0, 0.3)

    t[("DEFENSIVE",      "LOW",        "SWING")] = _sw(3.0,  6.0, 2.5, 4.0, 2.0, 2, 4.0, 1.5)
    t[("DEFENSIVE",      "MEDIUM",     "SWING")] = _sw(3.5,  7.0, 2.5, 4.0, 2.5, 3, 3.5, 1.0)
    t[("DEFENSIVE",      "HIGH",       "SWING")] = _sw(4.0,  8.0, 2.5, 3.5, 2.8, 4, 3.0, 0.5)
    t[("DEFENSIVE",      "CONVICTION", "SWING")] = _sw(5.0,  9.0, 2.0, 3.5, 3.0, 5, 2.5, 0.3)

    t[("CHOPPY",         "HIGH",       "SWING")] = _sw(3.5,  7.0, 2.5, 4.0, 2.0, 3, 2.5, 1.0)
    t[("CHOPPY",         "CONVICTION", "SWING")] = _sw(4.0,  8.0, 2.5, 3.5, 2.5, 4, 1.0, 0.5)

    t[("HIGH_VOL",       "MEDIUM",     "SWING")] = _sw(3.5,  7.0, 3.0, 5.0, 2.5, 2, 3.5, 1.0)
    t[("HIGH_VOL",       "HIGH",       "SWING")] = _sw(4.0,  8.0, 3.0, 4.5, 3.0, 3, 3.0, 0.5)
    t[("HIGH_VOL",       "CONVICTION", "SWING")] = _sw(5.0,  9.5, 2.5, 4.0, 3.2, 4, 2.5, 0.3)

    t[("CAPITULATION",   "HIGH",       "SWING")] = _sw(6.0, 12.0, 4.0, 6.0, 3.5, 5, 3.0, -5.0)
    t[("CAPITULATION",   "CONVICTION", "SWING")] = _sw(8.0, 15.0, 4.0, 5.5, 4.0, 7, 2.0, -5.0)

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
```

- [ ] **Step 5: Add `max_position_pct` to `EntryParams` and update `_entry_table()`**

In `utils/regime_params.py`, update the `EntryParams` dataclass:

```python
@dataclass(frozen=True)
class EntryParams:
    composite_min: int
    rsi_max: int
    hard_close_hour: int = 13
    max_position_pct: float = 0.05
```

In `_entry_table()`, update each regime entry to include the fourth positional value (add after `hard_close_hour`). The existing table uses 3-arg `EntryParams`; update each line to include `max_position_pct`:

```python
# Example — find each line in _entry_table() and add the 4th field:
"NORMAL":          EntryParams(composite_min, rsi_max, 13,   0.05),
"BULL_TREND":      EntryParams(composite_min, rsi_max, 14,   0.03),
"OPPORTUNITY":     EntryParams(composite_min, rsi_max, 14,   0.03),
"CRASH_RECOVERY":  EntryParams(composite_min, rsi_max, 13,   0.025),
"DEFENSIVE":       EntryParams(composite_min, rsi_max, 12,   0.04),
"CHOPPY":          EntryParams(composite_min, rsi_max, 12,   0.04),
"HIGH_VOL":        EntryParams(composite_min, rsi_max, 13,   0.03),
"CAPITULATION":    EntryParams(composite_min, rsi_max, 11,   0.025),
"CRISIS":          EntryParams(composite_min, rsi_max, 0,    0.0),
```

(Keep the existing `composite_min` and `rsi_max` values — only the 3rd and 4th args change.)

- [ ] **Step 6: Strip `SwingParams` to `eligible: bool` only**

Replace the current `SwingParams` dataclass definition:

Old:
```python
@dataclass(frozen=True)
class SwingParams:
    eligible: bool
    min_pnl_pct: float
    profit_lock_pct: float
    tp1_pct: float
    tp2_pct: float
    sl_pct: float
```

New:
```python
@dataclass(frozen=True)
class SwingParams:
    eligible: bool
```

Update `_swing_table()` so each entry only passes `eligible=True/False`:
- All currently eligible regimes: `SwingParams(True)`
- CRISIS: `SwingParams(False)`

**Important:** At this step, `swing_manager.py` and `backtest/simulator.py` will break because they read `sp.tp1_pct`, `sp.profit_lock_pct`, etc. That is expected — those callers are migrated in Task 7 (swing wiring). Do not run the full test suite until Task 7 is complete.

- [ ] **Step 7: Run TPSLParams tests**

```bash
.venv/bin/python -m pytest tests/test_tpsl_params.py -q
```
Expected: All pass

- [ ] **Step 8: Commit**

```bash
git add utils/regime_params.py tests/test_tpsl_params.py
git commit -m "feat(regime_params): TPSLParams 144-entry table, EntryParams.max_position_pct, SwingParams strip"
```

---

## Task 3: derive_mode 5-Mode + _compute_unlocks + _is_fresh

**Files:**
- Modify: `utils/ticker_quality.py`

- [ ] **Step 1: Write failing tests in test_pipeline.py**

Add to `tests/test_pipeline.py`:

```python
# ── derive_mode 5-mode tests ──────────────────────────────────────────────────
class TestDeriveMode:
    @pytest.mark.unit
    def test_swing_takes_priority_over_pead(self):
        from utils.ticker_quality import derive_mode
        assert derive_mode("AAPL", pead_ok=True, existing_swing_pos=True) == "SWING"

    @pytest.mark.unit
    def test_catalyst_before_day_trade(self):
        from utils.ticker_quality import derive_mode
        assert derive_mode("AAPL", pead_ok=False, catalyst_active=True) == "CATALYST"

    @pytest.mark.unit
    def test_default_is_day_trade(self):
        from utils.ticker_quality import derive_mode
        assert derive_mode("AAPL", pead_ok=False) == "DAY_TRADE"

    @pytest.mark.unit
    def test_pead_before_catalyst(self):
        from utils.ticker_quality import derive_mode
        assert derive_mode("AAPL", pead_ok=True, catalyst_active=True) == "PEAD"
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/python -m pytest tests/test_pipeline.py::TestDeriveMode -q
```
Expected: FAIL — `derive_mode` doesn't accept `catalyst_active`

- [ ] **Step 3: Update `derive_mode()` in `utils/ticker_quality.py`**

Replace the current `derive_mode` function (currently 3-mode, line 33):

```python
def derive_mode(
    symbol: str,
    pead_ok: bool,
    existing_swing_pos: bool = False,
    catalyst_active: bool = False,
) -> str:
    """Derive trading mode — single SoT (Rule 32). Priority: SWING > PEAD > CATALYST > DAY_TRADE."""
    if existing_swing_pos:
        return "SWING"
    if pead_ok:
        return "PEAD"
    if catalyst_active:
        return "CATALYST"
    return "DAY_TRADE"
```

- [ ] **Step 4: Restore `mode` param in `_compute_unlocks()` and pass it through**

Find `_compute_unlocks` in `utils/ticker_quality.py`. Update its signature and add CATALYST unlock logic:

```python
def _compute_unlocks(
    tier: str,
    mode: str,
    pead_ok: bool,
    intra_sent: int,
    sentiment: int,
    earnings_clear_days: int,
    regime: str,
) -> frozenset:
    unlocks: set[str] = set()
    if tier == "CONVICTION":
        if pead_ok or intra_sent >= 60 or mode == "CATALYST":
            unlocks.add("T2_CHOPPY")
        if intra_sent >= 50 and earnings_clear_days >= 14:
            unlocks.add("SWING_CHOPPY")
        if pead_ok and regime in ("CAPITULATION", "CHOPPY"):
            unlocks.add("PEAD_EXTENDED_WINDOW")
    if tier in ("HIGH", "CONVICTION") and sentiment >= 40:
        unlocks.add("SWING_SENTIMENT")
    return frozenset(unlocks)
```

Find the call to `_compute_unlocks` inside `compute_ticker_quality()` and pass `mode`:

```python
unlocks = _compute_unlocks(tier, mode, pead_ok, intra_sent, sentiment, earnings_clear_days, regime)
```

- [ ] **Step 5: Add `_is_fresh()` helper and intraday staleness guard**

Add after `_compute_unlocks`:

```python
def _is_fresh(ts_str: str | None, max_minutes: int) -> bool:
    if not ts_str:
        return False
    from datetime import datetime
    from utils.constants import ET
    try:
        ts = datetime.fromisoformat(ts_str).astimezone(ET)
        delta = (datetime.now(ET) - ts).total_seconds() / 60
        return delta <= max_minutes
    except (ValueError, TypeError):
        return False
```

Inside `compute_ticker_quality()`, before `intra_sent` is used in scoring, add:

```python
intra_ts    = intra_data.get("timestamp")
intra_fresh = _is_fresh(intra_ts, config.INTRADAY_NEWS_STALE_MINUTES)
intra_sent  = intra_data.get("sentiment_score", sentiment) if intra_fresh else 0
if not intra_fresh and intra_ts:
    gaps = (*gaps, "intraday_stale")
```

- [ ] **Step 6: Run failing tests**

```bash
.venv/bin/python -m pytest tests/test_pipeline.py::TestDeriveMode -q
```
Expected: All pass

- [ ] **Step 7: Run full unit test suite to check for regressions**

```bash
.venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -5
```
Expected: same or fewer failures than before Task 3 (only expected breakage from SwingParams strip in Task 2)

- [ ] **Step 8: Commit**

```bash
git add utils/ticker_quality.py
git commit -m "feat(ticker_quality): derive_mode 5-mode taxonomy, restore _compute_unlocks mode param, intraday staleness guard"
```

---

## Task 4: CATALYST Gate + CycleSnapshot Fields + post_event_window

**Files:**
- Modify: `trading/cycle_context.py`
- Modify: `trading/cycle.py`
- Modify: `macro/economic_calendar.py`
- Modify: `signals/entry_gates.py`
- Create: `tests/test_catalyst.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_catalyst.py`:

```python
import pytest
from unittest.mock import patch
from datetime import datetime, timedelta
from utils.constants import ET


def _make_snap(post_event_window=None, sector_etf_cache=None, catalyst_surprise_score=0.0):
    from trading.cycle_context import CycleSnapshot
    return CycleSnapshot(
        regime_info={"regime": "NORMAL"},
        regime="NORMAL",
        fred_cache={},
        research={},
        research_meta={},
        strategy_memory="",
        weights={},
        params={},
        portfolio_gain_pct=0.0,
        post_event_window=post_event_window,
        sector_etf_cache=sector_etf_cache or {},
        catalyst_surprise_score=catalyst_surprise_score,
    )


class TestIsCatalystActive:
    @pytest.mark.unit
    def test_none_window_returns_false(self):
        from signals.entry_gates import is_catalyst_active
        snap = _make_snap(post_event_window=None)
        assert not is_catalyst_active("AAPL", snap)

    @pytest.mark.unit
    def test_too_soon_after_event_returns_false(self):
        from signals.entry_gates import is_catalyst_active
        import config
        event = {"type": "CPI", "impact": "HIGH"}
        snap = _make_snap(
            post_event_window=(event, config.CATALYST_SETTLE_MINUTES - 1),
            sector_etf_cache={"XLK": 1.0},
        )
        assert not is_catalyst_active("AAPL", snap)

    @pytest.mark.unit
    def test_etf_move_too_small_returns_false(self):
        from signals.entry_gates import is_catalyst_active
        import config
        event = {"type": "CPI", "impact": "HIGH"}
        snap = _make_snap(
            post_event_window=(event, config.CATALYST_SETTLE_MINUTES + 10),
            sector_etf_cache={"XLK": 0.1},  # below CATALYST_ETF_MIN_MOVE_PCT
        )
        with patch("signals.entry_gates.get_sub_sector_etf", return_value="XLK"):
            assert not is_catalyst_active("AAPL", snap)

    @pytest.mark.unit
    def test_valid_catalyst_returns_true(self):
        from signals.entry_gates import is_catalyst_active
        import config
        event = {"type": "CPI", "impact": "HIGH"}
        snap = _make_snap(
            post_event_window=(event, config.CATALYST_SETTLE_MINUTES + 30),
            sector_etf_cache={"XLK": 0.8},
            catalyst_surprise_score=0.3,
        )
        with patch("signals.entry_gates.get_sub_sector_etf", return_value="XLK"):
            assert is_catalyst_active("AAPL", snap)

    @pytest.mark.unit
    def test_low_surprise_score_blocks(self):
        from signals.entry_gates import is_catalyst_active
        import config
        event = {"type": "CPI", "impact": "HIGH"}
        snap = _make_snap(
            post_event_window=(event, config.CATALYST_SETTLE_MINUTES + 30),
            sector_etf_cache={"XLK": 0.8},
            catalyst_surprise_score=-0.5,  # below CATALYST_MIN_SURPRISE_SCORE = -0.1
        )
        with patch("signals.entry_gates.get_sub_sector_etf", return_value="XLK"):
            assert not is_catalyst_active("AAPL", snap)


class TestGetPostEventWindow:
    @pytest.mark.unit
    def test_returns_none_when_no_events(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "ECONOMIC_CALENDAR_FILE", str(tmp_path / "econ.json"))
        from utils.io_utils import save_json
        save_json(str(tmp_path / "econ.json"), {"events": []})
        from macro.economic_calendar import get_post_event_window
        result = get_post_event_window(datetime.now(ET))
        assert result is None

    @pytest.mark.unit
    def test_returns_event_within_window(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "ECONOMIC_CALENDAR_FILE", str(tmp_path / "econ.json"))
        from utils.io_utils import save_json
        now = datetime.now(ET)
        event_time = (now - timedelta(minutes=45)).isoformat()
        save_json(str(tmp_path / "econ.json"), {
            "events": [{"type": "CPI", "impact": "HIGH", "time": event_time}]
        })
        from macro.economic_calendar import get_post_event_window
        result = get_post_event_window(now)
        assert result is not None
        event_dict, elapsed = result
        assert 40 <= elapsed <= 50
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/python -m pytest tests/test_catalyst.py -q 2>&1 | head -10
```
Expected: ImportError on `post_event_window` field / `is_catalyst_active`

- [ ] **Step 3: Add new fields to `CycleSnapshot` in `trading/cycle_context.py`**

After the `sector_etf_cache` field, add:

```python
post_event_window: tuple | None = None
# (event_dict, elapsed_minutes) for most recent HIGH-impact event within
# CATALYST_WINDOW_MINUTES, or None. Populated once per cycle by cycle.py.
catalyst_surprise_score: float = 0.0
# Pre-fetched from get_catalyst_surprise_score(); 0.0 = no data or neutral.
```

- [ ] **Step 4: Add `get_post_event_window()` to `macro/economic_calendar.py`**

Add this function (find the existing `is_event_risk_window` for placement context):

```python
def get_post_event_window(
    now: datetime,
    lookback_minutes: int = 120,
) -> tuple[dict, int] | None:
    """Return (event, minutes_elapsed) for the most recent HIGH-impact event
    that fired within the last lookback_minutes minutes, or None."""
    from utils.io_utils import load_json
    from utils.constants import ET
    events = load_json(config.ECONOMIC_CALENDAR_FILE, {}).get("events", [])
    now_et = now.astimezone(ET)
    for event in sorted(events, key=lambda e: e.get("time", ""), reverse=True):
        if event.get("impact") != "HIGH":
            continue
        try:
            event_dt = datetime.fromisoformat(event["time"]).astimezone(ET)
        except (KeyError, ValueError):
            continue
        elapsed = int((now_et - event_dt).total_seconds() / 60)
        if 0 <= elapsed <= lookback_minutes:
            return event, elapsed
    return None
```

- [ ] **Step 5: Add `is_catalyst_active()` to `signals/entry_gates.py`**

Add after existing gate functions:

```python
def is_catalyst_active(symbol: str, snap) -> bool:
    """Pure CATALYST gate (Rule 33). Returns True when event settled, sector confirms, surprise passes."""
    window = snap.post_event_window
    if window is None:
        return False
    event_dict, elapsed_min = window
    if elapsed_min < config.CATALYST_SETTLE_MINUTES:
        return False
    from sectors import get_sub_sector_etf
    sub_etf = get_sub_sector_etf(symbol)
    etf_chg = snap.sector_etf_cache.get(sub_etf, 0.0)
    if abs(etf_chg) < config.CATALYST_ETF_MIN_MOVE_PCT:
        return False
    surprise = getattr(snap, "catalyst_surprise_score", 0.0)
    if surprise < config.CATALYST_MIN_SURPRISE_SCORE:
        return False
    return True
```

- [ ] **Step 6: Populate `post_event_window` and `catalyst_surprise_score` in `trading/cycle.py`**

In `run_trading_cycle()` (or `build_cycle_snapshot()`), after `major_event_today` is set, add:

```python
from macro.economic_calendar import get_post_event_window
from macro.economic_calendar import get_catalyst_surprise_score
_post_event_window = get_post_event_window(datetime.now(ET))
_catalyst_surprise = 0.0
if _post_event_window:
    _event_dict, _ = _post_event_window
    _catalyst_surprise = get_catalyst_surprise_score(_event_dict.get("type", ""))
```

Then pass to `CycleSnapshot` constructor:
```python
post_event_window=_post_event_window,
catalyst_surprise_score=_catalyst_surprise,
```

Note: `get_catalyst_surprise_score` is added to `macro/economic_calendar.py` in Task 11 (Track 6). For now, add a stub:

```python
def get_catalyst_surprise_score(event_type: str) -> float:
    return 0.0
```

- [ ] **Step 7: Run tests**

```bash
.venv/bin/python -m pytest tests/test_catalyst.py -q
```
Expected: All pass

- [ ] **Step 8: Commit**

```bash
git add trading/cycle_context.py trading/cycle.py macro/economic_calendar.py signals/entry_gates.py tests/test_catalyst.py
git commit -m "feat(catalyst): is_catalyst_active gate, CycleSnapshot.post_event_window, get_post_event_window"
```

---

## Task 5: modes.py Retirement + Caller Migration + pre_claude_filter Fix

**Files:**
- Modify: `trading/executor.py`
- Modify: `trading/position_sizer.py`
- Modify: `trading/claude_caller.py`
- Modify: `trading/pre_claude_filter.py`
- Modify: `trading/pipeline.py`
- Delete: `trading/modes.py`

- [ ] **Step 1: Check all callers of modes.py**

```bash
grep -rn "from trading.modes\|import modes\|get_active_mode\|get_sl_tp_for_mode" --include="*.py" .
```

Note all files returned. These all need migration.

- [ ] **Step 2: Update `trading/position_sizer.py`**

Find `compute_position_size_for_mode()`. Replace `_MODE_MAX_PCT.get(mode)` (or equivalent) with:

```python
from utils.regime_params import get_entry_params
max_pct = get_entry_params(regime).max_position_pct
```

Remove the `mode` parameter from the function signature (regime now drives sizing). Update all call sites of `compute_position_size_for_mode()` to drop `mode=` argument.

- [ ] **Step 3: Update `trading/executor.py`**

Replace `get_active_mode()` call with:
```python
from utils.ticker_quality import derive_mode
mode = derive_mode(symbol, pead_ok=pead_ok, existing_swing_pos=False,
                   catalyst_active=is_catalyst_active(symbol, snap))
```

Replace `get_sl_tp_for_mode_and_regime()` call with `compute_tp_sl()` (already exists).

Add CATALYST position limit check (after opening `open_positions` load):
```python
if mode == "CATALYST":
    cat_count = sum(
        1 for p in open_positions.values()
        if p.get("mode_at_entry") == "CATALYST"
    )
    if cat_count >= config.CATALYST_MAX_POSITIONS:
        return

```

Write `mode` and `atr_pct` at fill time:
```python
position["mode_at_entry"] = mode
position["atr_pct"]       = base_tp_pct   # raw ATR% before mult/clamp
```

- [ ] **Step 4: Update `trading/claude_caller.py`**

Replace `get_active_mode()` with `derive_mode()`. The mode string is used only for prompt-building context — so the call just needs `derive_mode(symbol, pead_ok, catalyst_active=False)` since the cycle snapshot isn't readily available in claude_caller; use `False` as a safe default (CATALYST prompt context isn't critical).

- [ ] **Step 5: Fix `trading/pre_claude_filter.py`**

Add `catalyst_active: bool = False` parameter to `apply_pre_claude_filter()`. Replace hardcoded `"DAY_TRADE"` mode argument in `compute_ticker_quality()` calls:

```python
def apply_pre_claude_filter(
    symbol: str,
    iv,
    snap,
    daily_closed: set[str] | None = None,
    sub_etf_chg: float = 0.0,
    earnings_clear_days: int = 30,
    pead_ok: bool = False,
    pead_score: int = 0,
    event_active: bool = False,
    catalyst_active: bool = False,
) -> FilterResult:
    mode = derive_mode(symbol, pead_ok, existing_swing_pos=False,
                       catalyst_active=catalyst_active)
    # rest of function: replace "DAY_TRADE" hardcodes with mode
```

- [ ] **Step 6: Update `trading/pipeline.py`**

Add per-symbol CATALYST check before calling `apply_pre_claude_filter`:

```python
from signals.entry_gates import is_catalyst_active
...
catalyst_active = is_catalyst_active(symbol, snap)
filt = apply_pre_claude_filter(
    symbol, iv, snap, ..., pead_ok=pead_ok, catalyst_active=catalyst_active,
)
```

- [ ] **Step 7: Delete `trading/modes.py` and its test file**

```bash
git rm trading/modes.py
git rm tests/test_trading_modes.py
```

`tests/test_trading_modes.py` tests only `get_active_mode` and `get_sl_tp_for_mode_and_regime` — both retired. Deleting the whole file is correct; the new equivalents (`derive_mode` + `get_tp_sl_params`) are covered by `tests/test_catalyst.py` and `tests/test_tpsl_params.py`.

- [ ] **Step 7b: Fix dead patches in existing test files**

`tests/test_trading_budget.py` and `tests/test_trading_pipeline.py` patch the retired functions. Update them:

In `tests/test_trading_budget.py` (lines ~364 and ~418), replace:
```python
patch("trading.executor.get_active_mode", return_value="DAY_TRADE"),
patch("trading.executor.get_sl_tp_for_mode_and_regime", return_value=(4.0, 5.0)),
```
with:
```python
patch("trading.executor.derive_mode", return_value="DAY_TRADE"),
patch("signals.tp_scaler.compute_tp_sl", return_value=(5.0, 2.0)),
```

In `tests/test_trading_pipeline.py` (lines ~392, ~982-983), apply the same replacement pattern.

Run to confirm the patched tests still pass:
```bash
.venv/bin/python -m pytest tests/test_trading_budget.py tests/test_trading_pipeline.py -m unit -q 2>&1 | tail -5
```

- [ ] **Step 8: Propagate `mode` + `atr_pct` in `trading/journal.py`**

In the `close_position()` function (or wherever positions move from `open_positions` to `daily_closed`), ensure these fields are forwarded:
```python
closed["mode_at_entry"] = pos.get("mode_at_entry", "DAY_TRADE")
closed["atr_pct"]       = pos.get("atr_pct", 0.0)
```

- [ ] **Step 9: Run unit tests**

```bash
.venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -10
```
Expected: Tests that previously relied on `modes.py` now pass using new callers. Fix any remaining import errors.

- [ ] **Step 10: Commit**

```bash
git add trading/executor.py trading/position_sizer.py trading/claude_caller.py trading/pre_claude_filter.py trading/pipeline.py trading/journal.py tests/test_trading_budget.py tests/test_trading_pipeline.py
git commit -m "feat(modes): retire modes.py + test file, migrate callers to derive_mode + EntryParams.max_position_pct, fix dead patches"
```

---

## Task 6: compute_tp_sl Upgrade (TPSLParams-aware)

**Files:**
- Modify: `signals/tp_scaler.py`

- [ ] **Step 1: Write failing test in test_pipeline.py**

Add to `tests/test_pipeline.py`:

```python
class TestComputeTpSlWithMode:
    @pytest.mark.unit
    def test_mode_param_accepted(self):
        from signals.tp_scaler import compute_tp_sl
        from datetime import datetime
        from utils.constants import ET
        tp, sl = compute_tp_sl(
            base_tp=2.0, base_sl=1.5,
            entry_time=datetime(2026, 5, 24, 10, 30, tzinfo=ET),
            tier="MEDIUM", regime="NORMAL", mode="DAY_TRADE",
        )
        assert 1.5 <= tp <= 4.0   # within NORMAL/MEDIUM/DAY_TRADE TPSLParams bounds
        assert 1.5 <= sl <= 2.0

    @pytest.mark.unit
    def test_swing_mode_uses_swing_table(self):
        from signals.tp_scaler import compute_tp_sl
        from datetime import datetime
        from utils.constants import ET
        tp, sl = compute_tp_sl(
            base_tp=5.0, base_sl=2.0,
            entry_time=datetime(2026, 5, 24, 10, 0, tzinfo=ET),
            tier="CONVICTION", regime="BULL_TREND", mode="SWING",
        )
        # BULL_TREND/CONVICTION/SWING: tp_floor=7.0, tp_ceiling=12.0
        assert 7.0 <= tp <= 12.0

    @pytest.mark.unit
    def test_disabled_combo_raises(self):
        from signals.tp_scaler import compute_tp_sl
        from datetime import datetime
        from utils.constants import ET
        with pytest.raises(ValueError):
            compute_tp_sl(
                base_tp=2.0, base_sl=1.5,
                entry_time=datetime(2026, 5, 24, 10, 0, tzinfo=ET),
                tier="LOW", regime="CRISIS", mode="DAY_TRADE",
            )
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/python -m pytest tests/test_pipeline.py::TestComputeTpSlWithMode -q
```
Expected: FAIL — `compute_tp_sl` doesn't accept `mode`

- [ ] **Step 3: Rewrite `signals/tp_scaler.py`**

Replace the entire file content:

```python
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
```

- [ ] **Step 4: Update all callers of `compute_tp_sl` to pass `mode`**

```bash
grep -rn "compute_tp_sl(" --include="*.py" .
```

For each call site, add `mode=mode` (where `mode` is the derived mode for that context). If the caller doesn't have mode available yet, use `mode="DAY_TRADE"` as a safe default until the full migration is wired.

- [ ] **Step 5: Run tests**

```bash
.venv/bin/python -m pytest tests/test_pipeline.py::TestComputeTpSlWithMode -q
```
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add signals/tp_scaler.py
git commit -m "feat(tp_scaler): compute_tp_sl uses TPSLParams table; removes global ATR floor/ceiling"
```

---

## Task 7: Swing Manager + Backtest Swing Wiring (Track 2)

**Files:**
- Modify: `trading/swing_manager.py`
- Modify: `backtest/simulator.py`
- Modify: `tests/test_swing.py`
- Modify: `tests/test_backtest_simulator.py`

- [ ] **Step 1: Add failing tests for swing manager**

Add to `tests/test_swing.py`:

```python
class TestSwingPromotionFromTPSLTable:
    @pytest.mark.unit
    def test_promotion_uses_tpsl_max_days(self, monkeypatch, tmp_path):
        import config
        monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", str(tmp_path / "open.json"))
        monkeypatch.setattr(config, "SWING_POSITIONS_FILE", str(tmp_path / "swing.json"))
        monkeypatch.setattr(config, "BACKTEST_SWING_COMPOSITE_MIN", 40)
        # NORMAL/HIGH/SWING: max_days=5
        from utils.regime_params import get_tp_sl_params
        p = get_tp_sl_params("NORMAL", "HIGH", "SWING")
        assert p.max_days == 5

    @pytest.mark.unit
    def test_profit_lock_from_table(self):
        from utils.regime_params import get_tp_sl_params
        p = get_tp_sl_params("BULL_TREND", "CONVICTION", "SWING")
        assert p.profit_lock_pct == 1.0
        assert p.min_pnl_pct == 0.1

    @pytest.mark.unit
    def test_get_effective_profit_lock_removed(self):
        import utils.regime_params as rp
        assert not hasattr(rp, "get_effective_profit_lock"), (
            "get_effective_profit_lock must be deleted — use get_tp_sl_params directly"
        )
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/python -m pytest tests/test_swing.py::TestSwingPromotionFromTPSLTable -q
```

- [ ] **Step 3: Update `trading/swing_manager.py` — `evaluate_eod_holds()`**

Find the `evaluate_eod_holds()` function. Replace the block that reads `get_swing_params(regime)` and `get_effective_profit_lock()`:

```python
from utils.regime_params import get_swing_params, get_tp_sl_params

for sym, pos in positions.items():
    tier   = pos.get("quality_tier", "MEDIUM")
    p      = get_tp_sl_params(regime, tier, "SWING")
    sw     = get_swing_params(regime)

    if p.is_disabled or not sw.eligible:
        continue

    pnl_pct = pos.get("unrealised_pnl_pct", 0.0)
    profit_lock_bypass = (pnl_pct >= p.profit_lock_pct and regime != "CRISIS")
    standard_qualify   = (
        sw.eligible
        and pnl_pct >= p.min_pnl_pct
        and pos.get("composite", 0) >= config.BACKTEST_SWING_COMPOSITE_MIN
    )

    if profit_lock_bypass or standard_qualify:
        pos["is_swing"]             = True
        pos["swing_days_remaining"] = p.max_days
        pos["tp1_pct"]              = p.tp_floor_pct
        pos["tp2_pct"]              = p.tp_ceiling_pct
        pos["stop_loss_pct"]        = p.sl_floor_pct
```

- [ ] **Step 4: Delete `get_effective_profit_lock()` from `utils/regime_params.py`**

Search for `get_effective_profit_lock` in `utils/regime_params.py` and remove the function entirely.

- [ ] **Step 5: Update `backtest/simulator.py` — `_evaluate_swing_promotion()`**

Apply the same replacement as Step 3 — replace `get_swing_params()+get_effective_profit_lock()` with `get_tp_sl_params(regime, tier, "SWING")`. Replace `BACKTEST_SWING_MAX_DAYS` references with `p.max_days`.

- [ ] **Step 6: Update `_init_tiered_swing_fields()` in backtest/simulator.py**

Find where swing fields are initialized at promotion and replace SwingParams field references with TPSLParams:

```python
p = get_tp_sl_params(regime, tier, "SWING")
pos["tp1_pct"]  = p.tp_floor_pct
pos["tp2_pct"]  = p.tp_ceiling_pct
pos["sl_pct"]   = p.sl_floor_pct
pos["max_days"] = p.max_days
```

- [ ] **Step 7: Run tests**

```bash
.venv/bin/python -m pytest tests/test_swing.py tests/test_backtest_simulator.py -q 2>&1 | tail -10
```
Expected: Swing and backtest tests pass

- [ ] **Step 8: Run full unit suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -5
```
Expected: ≥ 1040 passing

- [ ] **Step 9: Commit**

```bash
git add trading/swing_manager.py backtest/simulator.py utils/regime_params.py tests/test_swing.py
git commit -m "feat(swing): swing_manager + backtest use TPSLParams for max_days/profit_lock; remove get_effective_profit_lock"
```

---

## Task 8: Learning Agent Tier Feedback (Track 3)

**Files:**
- Modify: `learning/trade_analysis.py`
- Modify: `learning/agent.py`
- Modify: `tests/test_learning.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_learning.py`:

```python
class TestAggregateTier:
    @pytest.mark.unit
    def test_tier_aggregation_win_rate(self):
        from learning.trade_analysis import _aggregate_by_tier
        trades = [
            {"quality_tier": "HIGH", "pnl_pct": 2.5, "atr_pct": 1.2, "exit_reason": "TP"},
            {"quality_tier": "HIGH", "pnl_pct": -1.0, "atr_pct": 1.1, "exit_reason": "SL"},
            {"quality_tier": "MEDIUM", "pnl_pct": 0.8, "atr_pct": 0.9, "exit_reason": "TP"},
        ]
        result = _aggregate_by_tier(trades)
        assert result["HIGH"]["win_rate"] == pytest.approx(0.5)
        assert result["HIGH"]["count"] == 2
        assert result["MEDIUM"]["count"] == 1

    @pytest.mark.unit
    def test_force_close_rate_computed(self):
        from learning.trade_analysis import _aggregate_by_tier
        trades = [
            {"quality_tier": "LOW", "pnl_pct": 0.3, "atr_pct": 0.8, "exit_reason": "FORCE_CLOSE"},
            {"quality_tier": "LOW", "pnl_pct": 1.0, "atr_pct": 0.9, "exit_reason": "TP"},
        ]
        result = _aggregate_by_tier(trades)
        assert result["LOW"]["force_close_rate"] == pytest.approx(0.5)

    @pytest.mark.unit
    def test_most_constrained_param(self):
        from learning.trade_analysis import _most_constrained_param, _ZERO_TRADE_BOUNDS
        # Build a params dict where RSI_MAX is at its upper bound (most restricted)
        params = {}
        for key, (lo, hi) in _ZERO_TRADE_BOUNDS.items():
            params[key] = hi  # all at max restriction
        result = _most_constrained_param(params)
        assert result in _ZERO_TRADE_BOUNDS
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/python -m pytest tests/test_learning.py::TestAggregateTier -q
```
Expected: ImportError on `_aggregate_by_tier`, `_most_constrained_param`

- [ ] **Step 3: Add `_aggregate_by_tier()` to `learning/trade_analysis.py`**

```python
def _aggregate_by_tier(trades: list[dict]) -> dict[str, dict]:
    """Win rate, avg P&L, count, avg atr_pct, force_close_rate per quality tier."""
    result: dict[str, dict] = {}
    for tier in ("LOW", "MEDIUM", "HIGH", "CONVICTION"):
        bucket = [t for t in trades if t.get("quality_tier") == tier]
        if not bucket:
            continue
        wins = [t for t in bucket if (t.get("pnl_pct") or 0) > 0]
        result[tier] = {
            "count":           len(bucket),
            "win_rate":        len(wins) / len(bucket),
            "avg_pnl_pct":     sum(t.get("pnl_pct", 0) for t in bucket) / len(bucket),
            "avg_atr_pct":     sum(t.get("atr_pct", 0) for t in bucket) / len(bucket),
            "force_close_rate": sum(
                1 for t in bucket if t.get("exit_reason") == "FORCE_CLOSE"
            ) / len(bucket),
        }
    return result
```

- [ ] **Step 4: Add `_most_constrained_param()` and replace weekday rotation**

Add to `learning/trade_analysis.py`:

```python
def _most_constrained_param(params: dict) -> str:
    """Return the param name closest to its upper (most restrictive) bound."""
    scores: dict[str, float] = {}
    for key, (lo, hi) in _ZERO_TRADE_BOUNDS.items():
        val = float(params.get(key, hi))
        scores[key] = (val - lo) / (hi - lo) if hi != lo else 0.0
    return max(scores, key=lambda k: scores[k])
```

In `_handle_zero_trade_day()`, replace:
```python
# OLD
param = param_order[weekday % len(param_order)]
```
with:
```python
# NEW
param = _most_constrained_param(params)
```

- [ ] **Step 5: Wire tier_agg + force_close_rate into learning agent Step 1 prompt**

In `learning/agent.py`, in the function that builds the Step 1 user message, add:

```python
from learning.trade_analysis import _aggregate_by_tier
tier_agg = _aggregate_by_tier(closed_trades)
force_close_rt = (
    sum(1 for t in closed_trades if t.get("exit_reason") == "FORCE_CLOSE")
    / len(closed_trades)
) if closed_trades else 0.0

# Append to existing Step 1 user message content:
f"<performance_by_tier>{json.dumps(tier_agg)}</performance_by_tier>\n\n"
f"<force_close_rate>{force_close_rt:.1%}</force_close_rate>\n\n"
```

- [ ] **Step 6: Run tests**

```bash
.venv/bin/python -m pytest tests/test_learning.py -q 2>&1 | tail -5
```
Expected: All pass

- [ ] **Step 7: Commit**

```bash
git add learning/trade_analysis.py learning/agent.py tests/test_learning.py
git commit -m "feat(learning): tier aggregation feedback, replace weekday rotation with constrained param selection"
```

---

## Task 9: Context Builder Additions + Capitulation Detection (Track 4)

**Files:**
- Modify: `trading/context_builder.py`
- Modify: `research/agent.py`

- [ ] **Step 1: Write failing test for context builder**

Add to `tests/test_pipeline.py`:

```python
class TestContextBuilderAdditions:
    @pytest.mark.unit
    def test_context_includes_entry_countdown(self, monkeypatch):
        from trading.context_builder import build_dynamic_user_context
        from datetime import datetime
        from utils.constants import ET
        with patch("trading.context_builder.get_cached_regime", return_value="NORMAL"), \
             patch("trading.context_builder.load_open_positions", return_value={}), \
             patch("trading.context_builder.is_flash_crash_active", return_value=False):
            ctx = build_dynamic_user_context(
                datetime(2026, 5, 24, 10, 30, tzinfo=ET), 0.0
            )
        assert "Entry window:" in ctx

    @pytest.mark.unit
    def test_context_includes_flash_crash_status(self, monkeypatch):
        from trading.context_builder import build_dynamic_user_context
        from datetime import datetime
        from utils.constants import ET
        with patch("trading.context_builder.get_cached_regime", return_value="NORMAL"), \
             patch("trading.context_builder.load_open_positions", return_value={}), \
             patch("trading.context_builder.is_flash_crash_active", return_value=True):
            ctx = build_dynamic_user_context(
                datetime(2026, 5, 24, 10, 30, tzinfo=ET), 0.0
            )
        assert "Flash crash: ACTIVE" in ctx
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/python -m pytest tests/test_pipeline.py::TestContextBuilderAdditions -q
```

- [ ] **Step 3: Add three lines to `build_dynamic_user_context()` in `trading/context_builder.py`**

Inside the function body, before the return statement:

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

# Entry window countdown
from macro.regime import get_cached_regime
from utils.regime_params import get_entry_params
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

Append all three to the return string.

- [ ] **Step 4: Add capitulation helpers to `research/agent.py`**

```python
def _score_capitulation_candidate(symbol: str, bars) -> bool:
    """True if symbol shows real capitulation: drawdown >= 15% from 20d high + OBV declining."""
    if bars is None or len(bars) < 5:
        return False
    high_20d = bars["high"].rolling(20).max().iloc[-1]
    current  = float(bars["close"].iloc[-1])
    drawdown = (high_20d - current) / high_20d if high_20d > 0 else 0.0
    obv      = (bars["close"].diff().apply(lambda x: 1 if x > 0 else -1) * bars["volume"]).cumsum()
    obv_trend_down = float(obv.iloc[-1]) < float(obv.iloc[-5])
    return drawdown >= 0.15 and obv_trend_down


def _fetch_daily_bars_for_cap_check(symbols: list[str]) -> dict:
    from data.market_data import get_daily_bars
    result = {}
    for sym in symbols:
        try:
            result[sym] = get_daily_bars(sym, days=20)
        except Exception:
            result[sym] = None
    return result
```

Replace the existing sentiment < −30 capitulation proxy with calls to `_fetch_daily_bars_for_cap_check` + `_score_capitulation_candidate` for flagged symbols.

- [ ] **Step 5: Run tests**

```bash
.venv/bin/python -m pytest tests/test_pipeline.py::TestContextBuilderAdditions -q
```
Expected: Pass

- [ ] **Step 6: Commit**

```bash
git add trading/context_builder.py research/agent.py
git commit -m "feat(context): entry countdown, concentration, flash-crash in Claude context; real capitulation detection"
```

---

## Task 10: Backtest Fidelity (Track 5)

**Files:**
- Modify: `backtest/simulator.py`
- Modify: `tests/test_backtest_simulator.py`

- [ ] **Step 1: Add failing tests**

Add to `tests/test_backtest_simulator.py`:

```python
class TestBacktestFidelity:
    @pytest.mark.unit
    def test_bull_trend_snap_has_positive_sentiment(self):
        from backtest.simulator import _BacktestSnap
        snap = _BacktestSnap(regime="BULL_TREND", _watch_list=["AAPL"])
        assert snap.research["AAPL"]["sentiment_score"] > 0

    @pytest.mark.unit
    def test_crisis_snap_has_negative_sentiment(self):
        from backtest.simulator import _BacktestSnap
        snap = _BacktestSnap(regime="CRISIS", _watch_list=["AAPL"])
        assert snap.research["AAPL"]["sentiment_score"] < 0

    @pytest.mark.unit
    def test_snap_research_is_cached(self):
        from backtest.simulator import _BacktestSnap
        snap = _BacktestSnap(regime="NORMAL", _watch_list=["AAPL", "MSFT"])
        r1 = snap.research
        r2 = snap.research
        assert r1 is r2  # cached_property
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/python -m pytest tests/test_backtest_simulator.py::TestBacktestFidelity -q
```

- [ ] **Step 3: Update `_BacktestSnap` with regime sentiment priors**

In `backtest/simulator.py`, replace the existing `_BacktestSnap` class:

```python
from functools import cached_property

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

- [ ] **Step 4: Delete `_build_quality_map()` and switch to per-bar quality**

In the entry-signal loop in `backtest/simulator.py`, find where `quality_map.get(sym)` is called and replace with inline quality computation:

```python
from utils.ticker_quality import compute_ticker_quality, derive_mode
# Inside the per-bar loop:
quality = compute_ticker_quality(
    sym, iv, _snap, derive_mode(sym, pead_ok=False, catalyst_active=False),
)
tier = quality.tier
```

Delete the `_build_quality_map()` function and all calls to it.

- [ ] **Step 5: Add T2 CHOPPY unlock check**

In `_sim_tranche2_gates_pass()`:

```python
if regime == "CHOPPY":
    if quality is None or "T2_CHOPPY" not in quality.unlocks:
        return False
```

Ensure `quality` is passed through as a parameter to this function from the entry loop.

- [ ] **Step 6: Run tests**

```bash
.venv/bin/python -m pytest tests/test_backtest_simulator.py -q 2>&1 | tail -5
```
Expected: All pass

- [ ] **Step 7: Commit**

```bash
git add backtest/simulator.py tests/test_backtest_simulator.py
git commit -m "feat(backtest): regime sentiment priors, per-bar quality computation, T2 CHOPPY unlock"
```

---

## Task 11: FRED Signal Enrichment — 7 Series + Rule 4 + CPI Fix (Track 6A)

**Files:**
- Modify: `macro/fred_cache.py`
- Create: `tests/test_fred.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_fred.py`:

```python
import pytest
from unittest.mock import patch, MagicMock


class TestFredCacheExpansion:
    @pytest.mark.unit
    def test_load_fred_cache_returns_dict(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "FRED_CACHE_FILE", str(tmp_path / "fred.json"))
        from utils.io_utils import save_json
        save_json(str(tmp_path / "fred.json"), {"fred_modifier": 3})
        from macro.fred_cache import load_fred_cache
        result = load_fred_cache()
        assert isinstance(result, dict)
        assert result["fred_modifier"] == 3

    @pytest.mark.unit
    def test_cpi_yoy_computed_correctly(self):
        from macro.fred_cache import _compute_cpi_yoy
        # 13 obs: index 0 = most recent (280), index 12 = 12 months ago (260)
        cpi = [280.0] + [270.0] * 11 + [260.0]
        yoy = _compute_cpi_yoy(cpi)
        expected = (280.0 / 260.0 - 1) * 100
        assert abs(yoy - expected) < 0.01

    @pytest.mark.unit
    def test_modifier_clamped_to_range(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "FRED_MODIFIER_MIN", -15)
        monkeypatch.setattr(config, "FRED_MODIFIER_MAX", 15)
        from macro.fred_cache import _compute_modifier
        # All negative signals: hiking + above CPI + inverted curve + credit spread + claims
        result = _compute_modifier(
            fed_direction="hiking",
            cpi_vs_target="above",
            yield_curve_inverted=True,
            credit_spread_risk_off=True,
            breakeven_high=True,
            unemployment_rising=True,
            claims_spike=True,
        )
        assert result == -15   # clamped to min

    @pytest.mark.unit
    def test_modifier_all_positive(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "FRED_MODIFIER_MIN", -15)
        monkeypatch.setattr(config, "FRED_MODIFIER_MAX", 15)
        from macro.fred_cache import _compute_modifier
        result = _compute_modifier(
            fed_direction="cutting",
            cpi_vs_target="below",
            yield_curve_inverted=False,
            credit_spread_risk_off=False,
            breakeven_high=False,
            unemployment_rising=False,
            claims_spike=False,
        )
        assert result == 7   # cutting(+5) + below_cpi(+2)

    @pytest.mark.unit
    def test_uses_session_not_bare_get(self):
        import macro.fred_cache as fc
        import inspect
        src = inspect.getsource(fc)
        assert "requests.get(" not in src, "Must use Session, not bare requests.get()"
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/python -m pytest tests/test_fred.py -q 2>&1 | head -15
```

- [ ] **Step 3: Rewrite `macro/fred_cache.py`**

Replace the entire file:

```python
"""
FRED macro signal fetcher. Expands from 3 → 7 series.
Uses module-level Session (Rule 4/5). Cached in fred_cache.json at 4 PM.
"""
from __future__ import annotations
import logging
import time
from datetime import datetime

import requests

import config
from utils.constants import ET
from utils.io_utils import load_json, save_json

_logger = logging.getLogger(__name__)

_FRED_BASE   = "https://api.stlouisfed.org/fred/series/observations"
_FRED_SESSION: requests.Session | None = None


def _get_session() -> requests.Session:
    global _FRED_SESSION
    if _FRED_SESSION is None:
        _FRED_SESSION = requests.Session()
        _FRED_SESSION.headers["Accept"] = "application/json"
    return _FRED_SESSION


def _fetch_n(series_id: str, n: int) -> list[float]:
    """Fetch the n most-recent observations for a FRED series. Returns floats descending (newest first)."""
    api_key = getattr(config, "FRED_API_KEY", "")
    if not api_key:
        return []
    sess = _get_session()
    params = {
        "series_id":            series_id,
        "api_key":              api_key,
        "file_type":            "json",
        "sort_order":           "desc",
        "observation_start":    "2000-01-01",
        "limit":                str(n),
    }
    for attempt in range(3):
        try:
            resp = sess.get(_FRED_BASE, params=params, timeout=10)
            resp.raise_for_status()
            obs = resp.json().get("observations", [])
            values = []
            for o in obs:
                try:
                    v = float(o["value"])
                    values.append(v)
                except (KeyError, ValueError):
                    pass
            return values
        except requests.HTTPError as exc:
            if hasattr(exc, "response") and exc.response.status_code == 429 and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            _logger.warning("FRED fetch %s attempt %d: %s", series_id, attempt, exc)
            return []
        except Exception as exc:
            _logger.warning("FRED fetch %s: %s", series_id, exc)
            return []
    return []


def _compute_cpi_yoy(cpi: list[float]) -> float | None:
    """12-month YoY from 13 monthly observations (index 0 = most recent)."""
    if len(cpi) < 13:
        return None
    return (cpi[0] / cpi[12] - 1) * 100


def _compute_modifier(
    fed_direction: str,
    cpi_vs_target: str,
    yield_curve_inverted: bool,
    credit_spread_risk_off: bool,
    breakeven_high: bool,
    unemployment_rising: bool,
    claims_spike: bool,
) -> int:
    score = 0
    if fed_direction == "cutting":
        score += 5
    elif fed_direction == "hiking":
        score -= 5
    if cpi_vs_target == "above":
        score -= 3
    elif cpi_vs_target == "below":
        score += 2
    if yield_curve_inverted:
        score -= 4
    if credit_spread_risk_off:
        score -= 5
    if breakeven_high:
        score -= 2
    if unemployment_rising:
        score -= 3
    if claims_spike:
        score -= 2
    return max(config.FRED_MODIFIER_MIN, min(config.FRED_MODIFIER_MAX, score))


def refresh_fred_cache() -> None:
    """Fetch 7 FRED series and write fred_cache.json. Called at 4 PM by scheduler."""
    # Fed funds rate
    dff   = _fetch_n("DFF", 3)
    fed_direction = "unknown"
    if len(dff) >= 3:
        if dff[0] < dff[2]:
            fed_direction = "cutting"
        elif dff[0] > dff[2]:
            fed_direction = "hiking"
        else:
            fed_direction = "paused"

    # CPI — 12-month YoY
    cpi     = _fetch_n("CPIAUCSL", config.FRED_CPI_OBS_LOOKBACK)
    cpi_yoy = _compute_cpi_yoy(cpi)
    if cpi_yoy is None:
        cpi_vs_target = "unknown"
    elif cpi_yoy > config.FRED_CPI_TARGET_PCT + 0.5:
        cpi_vs_target = "above"
    elif cpi_yoy < config.FRED_CPI_TARGET_PCT - 0.5:
        cpi_vs_target = "below"
    else:
        cpi_vs_target = "at"

    # Unemployment
    unrate = _fetch_n("UNRATE", 3)
    unemployment_rising = len(unrate) >= 2 and (unrate[0] - unrate[1]) >= 0.3

    # Yield curve (T10Y2Y)
    yc     = _fetch_n(config.FRED_YIELD_CURVE_SERIES, 2)
    yc_val = yc[0] if yc else None
    yield_curve_inverted = yc_val is not None and yc_val < config.FRED_YIELD_CURVE_INVERSION_THRESHOLD

    # HY credit spread
    cs     = _fetch_n(config.FRED_CREDIT_SPREAD_SERIES, 2)
    cs_val = cs[0] if cs else None
    credit_spread_risk_off = cs_val is not None and cs_val >= config.FRED_CREDIT_SPREAD_RISK_OFF

    # Breakeven inflation
    bei    = _fetch_n(config.FRED_BREAKEVEN_SERIES, 2)
    bei_val = bei[0] if bei else None
    breakeven_high = bei_val is not None and bei_val > config.FRED_BREAKEVEN_HIGH_THRESHOLD

    # Weekly jobless claims
    icsa   = _fetch_n(config.FRED_CLAIMS_SERIES, 3)
    claims_spike = len(icsa) >= 2 and (icsa[0] - icsa[1]) >= config.FRED_CLAIMS_SPIKE_THRESHOLD

    modifier = _compute_modifier(
        fed_direction, cpi_vs_target, yield_curve_inverted,
        credit_spread_risk_off, breakeven_high, unemployment_rising, claims_spike,
    )

    cache = {
        "fed_funds_rate":         dff[0] if dff else None,
        "fed_direction":          fed_direction,
        "cpi_yoy_pct":            cpi_yoy,
        "cpi_vs_target":          cpi_vs_target,
        "unemployment_rising":    unemployment_rising,
        "yield_curve_inverted":   yield_curve_inverted,
        "yield_curve_spread":     yc_val,
        "credit_spread_bps":      cs_val,
        "credit_spread_risk_off": credit_spread_risk_off,
        "breakeven_inflation":    bei_val,
        "claims_spike":           claims_spike,
        "fred_modifier":          modifier,
        "updated_at":             datetime.now(ET).isoformat(),
    }
    save_json(config.FRED_CACHE_FILE, cache)
    _logger.info("FRED cache refreshed: modifier=%d", modifier)


def load_fred_modifier() -> int:
    return load_json(config.FRED_CACHE_FILE, {}).get("fred_modifier", 0)


def load_fred_cache() -> dict:
    return load_json(config.FRED_CACHE_FILE, {})
```

- [ ] **Step 4: Run FRED tests**

```bash
.venv/bin/python -m pytest tests/test_fred.py -q
```
Expected: All pass

- [ ] **Step 5: Run full suite regression check**

```bash
.venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -5
```

- [ ] **Step 6: Commit**

```bash
git add macro/fred_cache.py tests/test_fred.py
git commit -m "feat(fred): expand to 7 series, fix CPI 12-month YoY, Rule 4 Session, modifier ±15"
```

---

## Task 12: Yield Curve Override + CATALYST Surprise Detection (Track 6B)

**Files:**
- Modify: `macro/regime.py`
- Modify: `macro/economic_calendar.py`
- Modify: `trading/cycle_context.py` (already updated in Task 4)
- Modify: `trading/cycle.py` (stub already added in Task 4)

- [ ] **Step 1: Write failing tests**

Add to `tests/test_fred.py`:

```python
class TestYieldCurveOverride:
    @pytest.mark.unit
    def test_bull_trend_overridden_to_defensive_when_inverted(self):
        from macro.regime import _apply_fred_overrides
        result = _apply_fred_overrides("BULL_TREND", {"yield_curve_inverted": True})
        assert result == "DEFENSIVE"

    @pytest.mark.unit
    def test_normal_not_overridden_without_inversion(self):
        from macro.regime import _apply_fred_overrides
        result = _apply_fred_overrides("NORMAL", {"yield_curve_inverted": False})
        assert result == "NORMAL"

    @pytest.mark.unit
    def test_crisis_not_overridden(self):
        from macro.regime import _apply_fred_overrides
        result = _apply_fred_overrides("CRISIS", {"yield_curve_inverted": True})
        assert result == "CRISIS"

    @pytest.mark.unit
    def test_opportunity_overridden_when_inverted(self):
        from macro.regime import _apply_fred_overrides
        result = _apply_fred_overrides("OPPORTUNITY", {"yield_curve_inverted": True})
        assert result == "DEFENSIVE"


class TestCatalystSurpriseScore:
    @pytest.mark.unit
    def test_hot_cpi_is_bearish(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "FRED_CACHE_FILE", str(tmp_path / "fred.json"))
        monkeypatch.setattr(config, "FRED_CPI_TARGET_PCT", 2.0)
        from utils.io_utils import save_json
        save_json(str(tmp_path / "fred.json"), {"cpi_yoy_pct": 5.5, "cpi_vs_target": "above"})
        from macro.economic_calendar import get_catalyst_surprise_score
        score = get_catalyst_surprise_score("CPI")
        assert score < 0

    @pytest.mark.unit
    def test_low_cpi_is_bullish(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "FRED_CACHE_FILE", str(tmp_path / "fred.json"))
        monkeypatch.setattr(config, "FRED_CPI_TARGET_PCT", 2.0)
        from utils.io_utils import save_json
        save_json(str(tmp_path / "fred.json"), {"cpi_yoy_pct": 1.0, "cpi_vs_target": "below"})
        from macro.economic_calendar import get_catalyst_surprise_score
        score = get_catalyst_surprise_score("CPI")
        assert score > 0

    @pytest.mark.unit
    def test_unknown_event_returns_zero(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "FRED_CACHE_FILE", str(tmp_path / "fred.json"))
        from utils.io_utils import save_json
        save_json(str(tmp_path / "fred.json"), {})
        from macro.economic_calendar import get_catalyst_surprise_score
        assert get_catalyst_surprise_score("EARNINGS") == 0.0
```

- [ ] **Step 2: Add `_apply_fred_overrides()` to `macro/regime.py`**

```python
def _apply_fred_overrides(regime: str, fred_cache: dict) -> str:
    """Hard regime downgrade when FRED signals structural contradiction."""
    if fred_cache.get("yield_curve_inverted") and regime not in ("CRISIS", "CAPITULATION"):
        if regime in ("BULL_TREND", "OPPORTUNITY"):
            return "DEFENSIVE"
    return regime
```

In `fetch_macro_regime()`, after the primary regime is computed and before writing to disk:

```python
from macro.fred_cache import load_fred_cache
raw_regime = _compute_primary_regime(...)   # whatever the existing computation is called
fred_cache = load_fred_cache()
regime = _apply_fred_overrides(raw_regime, fred_cache)
```

- [ ] **Step 3: Replace the `get_catalyst_surprise_score` stub with the real implementation in `macro/economic_calendar.py`**

Replace the stub added in Task 4 Step 6:

```python
def get_catalyst_surprise_score(event_type: str) -> float:
    """Return -1.0 to +1.0 surprise magnitude. >0 = bullish, <0 = bearish, 0 = neutral/no data."""
    from macro.fred_cache import load_fred_cache
    cache = load_fred_cache()
    if event_type == "CPI":
        yoy = cache.get("cpi_yoy_pct")
        if yoy is None:
            return 0.0
        if yoy > config.FRED_CPI_TARGET_PCT + 0.5:
            return -min(1.0, (yoy - 2.5) / 2.0)
        if yoy < config.FRED_CPI_TARGET_PCT - 0.5:
            return min(1.0, (1.5 - yoy) / 1.5)
        return 0.0
    if event_type in ("NFP", "JOBS"):
        return -0.5 if cache.get("unemployment_rising") else 0.3
    if event_type == "JOBLESS_CLAIMS":
        return -0.5 if cache.get("claims_spike") else 0.2
    return 0.0
```

- [ ] **Step 4: Update `trading/cycle.py` to use real `get_catalyst_surprise_score`** (replace stub call — already added in Task 4, just ensure the stub is removed and real version is called)

- [ ] **Step 5: Run tests**

```bash
.venv/bin/python -m pytest tests/test_fred.py -q
```
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add macro/regime.py macro/economic_calendar.py trading/cycle.py
git commit -m "feat(fred): yield curve hard DEFENSIVE override, CATALYST surprise score from FRED actuals"
```

---

## Task 13: Sector FRED Quality Scoring (Track 6C)

**Files:**
- Modify: `utils/ticker_quality.py`
- Modify: `trading/cycle_context.py`
- Modify: `trading/cycle.py`
- Modify: `trading/pre_claude_filter.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_fred.py`:

```python
class TestSectorFredScoring:
    @pytest.mark.unit
    def test_xlf_hiking_tailwind(self):
        from utils.ticker_quality import _score_sector_fred
        pts, gap = _score_sector_fred("XLF", {"fed_direction": "hiking"})
        assert pts == 3

    @pytest.mark.unit
    def test_xlu_hiking_headwind_clamped_to_zero(self):
        from utils.ticker_quality import _score_sector_fred
        pts, gap = _score_sector_fred("XLU", {"fed_direction": "hiking"})
        assert pts == 0   # negative clamped to 0
        assert gap is not None  # gap reported

    @pytest.mark.unit
    def test_xlk_cutting_tailwind(self):
        from utils.ticker_quality import _score_sector_fred
        pts, gap = _score_sector_fred("XLK", {"fed_direction": "cutting"})
        assert pts == 3

    @pytest.mark.unit
    def test_unknown_etf_returns_zero(self):
        from utils.ticker_quality import _score_sector_fred
        pts, gap = _score_sector_fred("XYZ", {"fed_direction": "hiking"})
        assert pts == 0
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/python -m pytest tests/test_fred.py::TestSectorFredScoring -q
```

- [ ] **Step 3: Add `_score_sector_fred()` to `utils/ticker_quality.py`**

```python
def _score_sector_fred(sub_etf: str, fred_cache: dict) -> tuple[int, str | None]:
    """Max 5 pts — FRED macro headwind/tailwind for sub-sector ETF. Negatives clamped to 0."""
    if not sub_etf or not fred_cache:
        return 0, None
    direction  = fred_cache.get("fed_direction", "unknown")
    inverted   = fred_cache.get("yield_curve_inverted", False)
    cpi_above  = fred_cache.get("cpi_vs_target") == "above"

    TAILWINDS: dict[str, int] = {
        "XLF":  3 if direction == "hiking"  else 0,
        "XLU": -4 if direction == "hiking"  else 0,
        "XLRE":-4 if direction == "hiking"  else 0,
        "XLP":  2 if direction == "cutting" else 0,
        "XLK":  3 if direction == "cutting" else 0,
        "XLY":  2 if direction == "cutting" else 0,
        "XLE":  3 if cpi_above             else 0,
    }
    pts_raw = TAILWINDS.get(sub_etf, 2 if inverted and sub_etf == "GLD" else 0)
    pts     = max(0, min(5, pts_raw))
    gap     = f"sector_fred_headwind({pts_raw})" if pts_raw < -1 else None
    return pts, gap
```

- [ ] **Step 4: Rename `_score_econ()` → `_score_econ_and_fred()` and add FRED component**

Find `_score_econ` in `utils/ticker_quality.py`. Update to accept `fred_cache` and `sub_etf`:

```python
def _score_econ_and_fred(
    event_active: bool, sub_etf: str, fred_cache: dict
) -> tuple[int, list[str]]:
    if event_active:
        return 0, []
    pts, gap = _score_sector_fred(sub_etf, fred_cache)
    gaps = [gap] if gap else []
    return pts, gaps
```

Update the call site in `compute_ticker_quality()` accordingly.

- [ ] **Step 5: Add `fred_cache` parameter to `compute_ticker_quality()`**

Update the function signature:
```python
def compute_ticker_quality(
    symbol: str,
    iv,
    snap,
    mode: str = "DAY_TRADE",
    sub_etf_chg: float = 0.0,
    earnings_clear_days: int = 30,
    pead_ok: bool = False,
    pead_score: int = 0,
    event_active: bool = False,
    fred_cache: dict | None = None,
) -> TickerQuality:
```

Pass `fred_cache or {}` to `_score_econ_and_fred`.

- [ ] **Step 6: Wire `fred_cache` through call chain**

`apply_pre_claude_filter()` already receives `snap`; pass `snap.fred_cache` to `compute_ticker_quality()`:
```python
quality = compute_ticker_quality(
    symbol, iv, snap, mode, ..., fred_cache=snap.fred_cache
)
```

`backtest/simulator.py` passes `fred_cache={}` (no FRED in historical simulation).

- [ ] **Step 7: Run tests**

```bash
.venv/bin/python -m pytest tests/test_fred.py -q && .venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -5
```
Expected: All fred tests pass; overall suite ≥ baseline

- [ ] **Step 8: Commit**

```bash
git add utils/ticker_quality.py trading/pre_claude_filter.py
git commit -m "feat(fred): sector FRED headwind/tailwind in quality scoring via _score_sector_fred"
```

---

## Task 14: Final Validation

- [ ] **Step 1: Run full unit test suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```
Expected: ≥ 1043 passing (current baseline), 0 unexpected failures

- [ ] **Step 2: Run backtest smoke test**

```bash
.venv/bin/python3 backtest.py --days 30 2>&1 | tail -20
```
Expected output includes validation targets from the spec:
- Force-close rate ≤ 25%
- Total P&L ≥ +$2,500
- SL avg loss ≥ −$90 (avg)

- [ ] **Step 3: Verify no bare `requests.get` in fred_cache.py**

```bash
grep "requests.get(" macro/fred_cache.py && echo "VIOLATION" || echo "ok"
```
Expected: `ok`

- [ ] **Step 4: Verify modes.py is deleted**

```bash
[ -f trading/modes.py ] && echo "STILL EXISTS" || echo "deleted ok"
```
Expected: `deleted ok`

- [ ] **Step 5: Verify REGIMES/TIERS/MODES exported from regime_params**

```bash
.venv/bin/python3 -c "from utils.regime_params import REGIMES, TIERS, MODES; print(len(REGIMES), len(TIERS), len(MODES))"
```
Expected: `9 4 4`

- [ ] **Step 6: Verify 144-entry table is complete**

```bash
.venv/bin/python3 -c "
from utils.regime_params import _TPSL_TABLE, REGIMES, TIERS, MODES
total = len(REGIMES) * len(TIERS) * len(MODES)
found = len(_TPSL_TABLE)
print(f'{found}/{total} entries')
"
```
Expected: `144/144 entries`

- [ ] **Step 7: Final commit**

```bash
git add -A
git commit -m "chore: profitability audit complete — 7 tracks, TPSLParams, CATALYST mode, FRED enrichment"
```
