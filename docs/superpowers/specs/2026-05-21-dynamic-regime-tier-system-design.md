# Dynamic Regime × Tier Trading System — Design Spec

**Date:** 2026-05-21
**Status:** Approved for implementation

---

## Problem Statement

The bot applies the same static config thresholds (composite floors, sentiment gates,
auto-execute bars, TP/SL widths, swing eligibility) regardless of regime or individual
ticker quality. This produces three measurable problems:

1. **54% force-close rate (100d backtest)** — positions entered too late without
   time-adjusted TP, dragging -$5,133 from force-closes vs +$13,325 from TP hits.
2. **T2 fires only 3.6% of positions** — gates too rigid; high-conviction tickers in
   CHOPPY cannot unlock T2 even with PEAD=True + sentiment=+80.
3. **Claude receives incomplete context** — soft gate flags, quality tier, sector
   sub-ETF performance, and regime parameters never reach Claude; blind spots in
   TP/SL calibration and concentration risk.

---

## Architecture

Two phases with a clean interface boundary.

**Phase 1 — Foundation:** New infrastructure modules with no behavioral change.
All new dataclasses, scoring logic, and the tp_scaler are pure functions (Rule 15).
No wiring into live trading paths yet.

**Phase 2 — Wire-in:** All behavioral changes. Every decision point (pre_claude_filter,
entry_gates, executor, eod_holds, context_builder) switches from static config constants
to dynamic lookups through the Phase 1 interface.

---

## Rule Additions

- **Rule 28:** `compute_ticker_quality()` in `utils/ticker_quality.py` is the sole
  factory for `TickerQuality`. No inline tier logic or quality scoring outside this module.
- **Rule 29:** All regime × tier threshold dispatch lives in `utils/regime_params.py`.
  `FilterParams`, `GateParams`, `AutoExecuteParams` follow the frozen-dataclass +
  table-function pattern of `EntryParams/SwingParams/T2Params`.
- **Rule 30:** TP/SL computation always goes through `signals/tp_scaler.compute_tp_sl()`.
  No direct `config.TAKE_PROFIT_PCT` / `config.STOP_LOSS_PCT` assignments in executor
  or gate functions.

---

## Phase 1 — Foundation

### 1.1 TickerQuality (`utils/ticker_quality.py`)

**Dataclass:**
```python
@dataclass(frozen=True)
class TickerQuality:
    tier: str           # "LOW" | "MEDIUM" | "HIGH" | "CONVICTION"
    score: int          # 0–100
    unlocks: frozenset  # named privilege set
    quality_gaps: list  # signals dragging score (for LOW/MEDIUM context block)
    component_scores: dict  # {"composite": 25, "sentiment": 18, ...} (for logging)
```

**Scoring — 7 components, total 100 points:**

| Component | Max pts | Signal source |
|-----------|---------|---------------|
| Composite (normalised) | 25 | `(iv.composite_score - floor) / 50 * 25`, clamped 0–25 |
| Research sentiment | 20 | `sentiment_score` mapped: ≥+60→20, ≥+30→14, ≥0→8, <0→0 |
| Intraday news sentiment | 15 | `intra_sent` same scale, slightly steeper curve |
| Sub-sector ETF intraday % | 15 | `sub_etf_chg`: ≥+2%→15, ≥+1%→10, ≥0%→5, <0%→0 |
| PEAD score | 10 | `pead_score/100 * 10` if PEAD-eligible, else 0 |
| Earnings-clear days | 10 | 0d→0, 7d→3, 14d→6, 21d→8, ≥30d→10 |
| Economic calendar | 5 | High-impact event active→0, medium→2, clear→5 |

**Tier assignment:**
- `< 35` → LOW
- `35–59` → MEDIUM
- `60–79` → HIGH
- `≥ 80` → CONVICTION

**Named unlocks (evaluated inside `compute_ticker_quality`):**
- `T2_CHOPPY` — CONVICTION + (PEAD=True OR intra_sent ≥ +60)
- `SWING_CHOPPY` — CONVICTION + intra_sent ≥ +50 + earnings_clear ≥ 14d
- `PEAD_EXTENDED_WINDOW` — PEAD=True + regime in (CAPITULATION, CHOPPY)
- `SWING_SENTIMENT` — HIGH or CONVICTION + sentiment ≥ +40 (lowers profit_lock to 1.5%)

**`quality_gaps`** — list of strings identifying the weakest components, used in LOW/MEDIUM
context blocks. E.g. `["vol_weak(-12)", "sector_flat(-8)", "earnings_near(-7)"]`.

**Function signature:**
```python
def compute_ticker_quality(
    symbol: str,
    iv: IndicatorValues,
    snap: CycleSnapshot,
    gate_flags: list[str],
    mode: str,
    sub_etf_chg: float = 0.0,   # sub-sector ETF intraday % — fetched upstream by pipeline
) -> TickerQuality
```

Pure — no I/O. `sub_etf_chg` is fetched once per cycle by the pipeline (batch fetch all
unique sub-sector ETFs in the watchlist via market_data) and passed per-symbol. Callers
supply all inputs.

---

### 1.2 TP/SL Scaler (`signals/tp_scaler.py`)

**Time multipliers** (applied universally):

| TimeSlot | ET entry time | TP multiplier |
|----------|--------------|---------------|
| FULL | ≤ 12:00 | 1.00 |
| LATE | 12:00–13:30 | 0.80 |
| TIGHT | 13:30–14:30 | 0.65 |
| VERY_TIGHT | > 14:30 | 0.50 |

**Tier multipliers** (applied only in safe regimes: NORMAL, BULL_TREND, OPPORTUNITY):

| Tier | TP multiplier | SL multiplier |
|------|--------------|---------------|
| LOW | 1.00 | 1.00 |
| MEDIUM | 1.00 | 1.00 |
| HIGH | 1.10 | 0.95 |
| CONVICTION | 1.20 | 0.90 |

**Final computation:**
```
tp_final = clamp(base_tp * time_mult * tier_mult, ATR_TP_FLOOR_PCT, ATR_TP_CEILING_PCT)
sl_final = clamp(base_sl * sl_tier_mult, 2.0, 5.0)   # floor 2%, ceiling 5%
```

Pure function: `compute_tp_sl(base_tp, base_sl, entry_time, tier, regime) → (float, float)`

---

### 1.3 Extended Regime Params (`utils/regime_params.py`)

Three new frozen dataclasses following the existing pattern:

**`FilterParams`** — pre_claude_filter thresholds per regime × tier:
```python
@dataclass(frozen=True)
class FilterParams:
    composite_min: int        # minimum composite to pass filter
    sentiment_hard_block: int # sentiment below this → SKIP
    intraday_block: int       # intraday sentiment below this → SKIP
```

**`GateParams`** — entry gate soft/hard thresholds per regime × tier:
```python
@dataclass(frozen=True)
class GateParams:
    stoch_soft_flag: int      # stoch_d ≥ this → soft flag (default 85)
    stoch_hard_veto: int      # stoch_d ≥ this → hard veto (default 97)
    rel_volume_min: float     # relative_volume < this → soft flag
    breadth_weak_threshold: float
```

**`AutoExecuteParams`** — auto-execute routing per regime × tier:
```python
@dataclass(frozen=True)
class AutoExecuteParams:
    composite_offset: int     # offset above regime composite_min; 999 = disabled
    sentiment_min: int        # intraday_sentiment must exceed this
    extra_unlock: str | None  # named unlock required (e.g. "PEAD_EXTENDED_WINDOW") or None
```

**Auto-execute table (all 9 regimes × 4 tiers):**

| Regime | LOW | MEDIUM | HIGH | CONVICTION |
|--------|-----|--------|------|------------|
| NORMAL | disabled | offset=+20, sent>20 | offset=+15, sent>20 | offset=+10, sent>20 |
| BULL_TREND | disabled | offset=+20, sent>20 | offset=+15, sent>20 | offset=+10, sent>20 |
| OPPORTUNITY | disabled | disabled | offset=+15, sent>20, sub_etf>+0.3% | offset=+10, sent>20, sub_etf>+0.5% |
| CRASH_RECOVERY | disabled | offset=+20, sent>20 | offset=+15, sent>20 | offset=+10, sent>20 |
| DEFENSIVE | disabled | disabled | offset=+20, sent>25 | offset=+15, sent>25 |
| CHOPPY | disabled | disabled | disabled | offset=+15, sent>25 |
| HIGH_VOL | disabled | offset=+20, sent>20 | offset=+15, sent>20 | offset=+10, sent>20 |
| CAPITULATION | disabled | disabled | disabled | offset=+20, sent>30, unlock=PEAD_EXTENDED_WINDOW |
| CRISIS | **all disabled — hard entry block** | | | |

New lookup functions:
- `get_filter_params(regime: str, tier: str) -> FilterParams`
- `get_gate_params(regime: str, tier: str) -> GateParams`
- `get_auto_execute_params(regime: str, tier: str) -> AutoExecuteParams`

All fallback to NORMAL/MEDIUM params on unknown regime/tier.

---

### 1.4 Sector Sub-Grouping (`tickers.json` + `sectors.py`)

**tickers.json** (single SoT): Each ticker gains a `"sub_etf"` field.

GICS-aligned sub-ETFs added:
- `XLC` — Communication Services (META, GOOGL, NFLX, SNAP, PINS, TTD)
- `SOXX` — Semiconductors (NVDA, AMD, AVGO, QCOM, MU, AMAT, LRCX, KLAC, MRVL, ARM, SMCI, ON, TXN, ADI)
- `IGV` — Software (MSFT, CRM, NOW, ORCL, ADBE, SNOW, MDB, DDOG, HUBS, ZS, CRWD, PANW, S, OKTA, VEEV)
- `CIBR` — Cybersecurity (CRWD, PANW, ZS, OKTA, S, FTNT — already in IGV overlap; sub_etf wins)
- `XBI` — Biotech (sub-group of XLV: MRNA, BNTX, CRSP, EDIT, NTLA, BEAM)
- `COHR/LITE/IIVI` — Photonics stays XLK but sub_etf=`IGV` or new `FIVG` as appropriate

`sectors.py` additions:
```python
def get_sub_sector_etf(symbol: str) -> str:
    """Returns sub_etf if set in tickers.json, else falls back to get_sector_etf()."""
```

All existing callers of `get_sector_etf()` are unchanged.

---

## Phase 2 — Wire-in

### 2.1 `data/market_data.py` — Yahoo parse guard (P1-8)

```python
# BEFORE (crashes on empty quote list)
q = result["indicators"]["quote"][0]

# AFTER
quote_list = result.get("indicators", {}).get("quote", [])
if not quote_list:
    return pd.DataFrame()
q = quote_list[0]
```

### 2.2 `signals/tranche2_gates.py` — T2 bug fixes

- **Gate 5.5 (P0-1):** Before price-drop window: `if t1_pnl_pct < 0 → return T2Result(False, "T1 underwater")`
- **T1 SL check (P1-6):** If T2 drop% ≥ T1 SL% → skip T2 (no value added)
- **entry_time guard (P0-2):** `except Exception → return T2Result(False, "entry_time invalid")`
- **Sector ETF fetch failure (P1-1):** Default to −2.0, not 0.0

### 2.3 `trading/pre_claude_filter.py` — dynamic FilterParams

- Call `compute_ticker_quality()` at filter entry; attach to `FilterResult`
- Replace all inline `config.*` threshold references with `get_filter_params(regime, tier)`
- Remove DEFENSIVE duplicate composite gate (Rule 23 fix)
- `FilterResult` gains `quality: TickerQuality` field

### 2.4 `signals/entry_gates.py` — dynamic GateParams + CHOPPY unlocks

- Accept `quality: TickerQuality` param in `check_all_gates()`
- Replace stoch/volume/breadth thresholds with `get_gate_params(regime, tier)`
- T2 in CHOPPY: check `T2_CHOPPY in quality.unlocks` before hard block
- Swing profit_lock: 1.0% if `SWING_CHOPPY in quality.unlocks`, else regime default
- Fix: pass `staleness_guard` to `effective_composite_gate()` (P0-4)

### 2.5 `trading/executor.py` — auto-execute 9×4 + tp_scaler

- Auto-execute routing reads `get_auto_execute_params(regime, tier)`:
  - `composite_offset`: if 999 → disabled; else check `composite ≥ floor + offset`
  - `sentiment_min`: intraday_sentiment must exceed
  - `extra_unlock`: if set, check in `quality.unlocks`
- TP/SL set via `compute_tp_sl(base_tp, base_sl, entry_time, tier, regime)`
- Fix: pass `staleness_guard` to `effective_composite_gate()` (P0-4)
- Fix: T2 profitability gate before T2 evaluation (P0-1)

### 2.6 `trading/eod_holds.py` — tier-based swing + sector ETF gate

- CONVICTION + `SWING_CHOPPY` in unlocks → swing eligible in CHOPPY at profit_lock=1.0%
- HIGH/CONVICTION + `SWING_SENTIMENT` → profit_lock=1.5% in NORMAL/BULL_TREND
- Add sector ETF direction gate for all regimes (fetch sub_etf intraday %)
- Block swing promotion if regime is stale (not fallback to NORMAL)
- CAPITULATION earnings-close exemption: check `regime == "CAPITULATION"` in addition to `capitulation_hold` flag

### 2.7 `trading/context_builder.py` — tier × regime dispatching

`build_stock_block` new signature:
```python
def build_stock_block(
    symbol: str,
    iv: IndicatorValues,
    snap: CycleSnapshot,
    gate_result: GateResult,
    quality: TickerQuality,
    mode: str,
    entry_time: datetime,
    existing_pos: dict | None = None,
    atr_suggested_tp_pct: float | None = None,
    intraday_news: dict | None = None,
) -> str
```

Dispatches internally to `_block_low / _block_medium / _block_high / _block_conviction`.

**What each tier includes:**

| Element | LOW | MEDIUM | HIGH | CONVICTION |
|---------|-----|--------|------|------------|
| composite, mode, vol, OR | ✓ | ✓ | ✓ | ✓ |
| RSI, VWAP direction | ✓ | ✓ | ✓ | ✓ |
| Stochastic raw k/d | ✗ | ✓ | ✓ | ✓ |
| VWAP slope number | ✗ | ✗ | ✓ | ✓ |
| BB / EMA9/21 / MACD | ✗ | ✗ | ✗ | ✗ |
| gate_flags | ✓ (prominent) | ✓ | ✓ | ✓ |
| quality_gaps | ✓ (top of block) | ✓ | ✗ | ✗ |
| caution line | ✓ | ✗ | ✗ | ✗ |
| sector ETF | ✗ | sector only | sector + sub_etf | sub_etf only |
| earnings_clear | ✗ | ✓ | ✓ | ✓ |
| econ_clear | ✗ | ✗ | ✓ | ✓ |
| PEAD score | ✗ | if PEAD | ✓ | ✓ |
| swing/T2 eligibility | ✗ | swing only | ✓ | ✓ |
| time_adj ATR-TP | ✗ | raw only | time_adj + raw | time_adj only |
| unlocks | ✗ | ✗ | ✗ | ✓ |
| quality component breakdown | ✗ | ✗ | ✗ | ✗ |
| quality_gaps breakdown | ✓ | ✓ | ✗ | ✗ |
| news summary | ✗ | ✓ (truncated) | ✓ | ✓ (catalyst) |
| sentiment score | ✓ | ✓ | ✓ | ✓ |

`build_stable_system_context` additions:
- Sub-sector scores line: `Sub-sector: SOXX=+78 IGV=+45 CIBR=+52 XBI=+12`
- Regime parameters block: `entry_window=10:00–14:00 composite_floor=50 swing=eligible T2=enabled stale_sources=None`

`build_dynamic_user_context` additions:
- Portfolio concentration: `open=3(XLK×2, XLV×1)`
- Entry window countdown: `entry_window: 2h22m remaining`
- Breadth A/D ratio: `Breadth: A/D=1.12(healthy)`
- Flash crash status: `flash_crash=None`

### 2.8 `trading/claude_caller.py` — pass new params

- Compute `entry_time = datetime.now(ET)` before building stock blocks
- Pass `gate_result, quality, mode, entry_time` to `build_stock_block`
- Pass `open_positions, breadth_ratio, flash_crash_active` to `build_dynamic_user_context`

---

## Testing Strategy

- **Phase 1 modules** (`ticker_quality`, `tp_scaler`, new regime_params tables): Unit-tested
  in isolation. All pure functions — no mocks needed for core logic. Patch config via monkeypatch.
- **Phase 2 wire-in**: Existing test files extended. No new test files unless a domain is
  uncovered. All Telegram calls patched per Rule 18.
- **Backtest regression**: After Phase 2 complete, run 30d backtest. Force-close rate target: ≤40% (down from 54%).

---

## Files Created / Modified

| File | Change |
|------|--------|
| `utils/ticker_quality.py` | CREATE |
| `signals/tp_scaler.py` | CREATE |
| `utils/regime_params.py` | MODIFY — add 3 dataclasses + 3 tables + 3 lookup functions |
| `tickers.json` | MODIFY — add sub_etf to all tickers |
| `sectors.py` | MODIFY — add get_sub_sector_etf() |
| `data/market_data.py` | MODIFY — Yahoo parse guard |
| `signals/tranche2_gates.py` | MODIFY — 4 bug fixes |
| `trading/pre_claude_filter.py` | MODIFY — FilterParams + quality |
| `signals/entry_gates.py` | MODIFY — GateParams + unlocks |
| `trading/executor.py` | MODIFY — AutoExecuteParams + tp_scaler + 2 fixes |
| `trading/eod_holds.py` | MODIFY — tier-based swing + sector ETF |
| `trading/context_builder.py` | MODIFY — tier×regime dispatch |
| `trading/claude_caller.py` | MODIFY — pass new params |
| `tests/test_ticker_quality.py` | CREATE |
| `tests/test_tp_scaler.py` | CREATE |
| `tests/test_pipeline.py` | MODIFY |
| `tests/test_signals.py` | MODIFY |
| `tests/test_entry_gates.py` | MODIFY |
| `tests/test_swing.py` | MODIFY |
| `tests/test_trading_budget.py` | MODIFY |
