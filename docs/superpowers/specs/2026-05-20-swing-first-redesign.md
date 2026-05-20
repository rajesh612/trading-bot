# Swing-First Redesign + Parameter Hardening

**Date:** 2026-05-20  
**Status:** Approved for implementation

---

## Background & Motivation

30-day backtest (174 trades, 2026-04-09 – 2026-05-19) and live-journal analysis expose three compounding problems:

| Engine | Trades | Net $ | Avg $/trade | Win rate |
|---|---|---|---|---|
| Day trades | 157 | -$2,224 | -$14 | 41% |
| Swing trades | 17 | +$1,574 | +$93 | 47% |

- **55% of all trades are force-closed at 3:55pm** at avg -0.28% — positions never reach SL or TP.
- SL fires more often than TP (19% vs 17%) because intraday noise on 5-min bars tags the closer level first.
- The composite floor (30) is too low: live trades with composite < 50 lose at 0–22% win rate; composite ≥ 50 → 56% win rate.
- Stocks above $500 lose -$55/trade avg at 28% win rate; one ticker (LITE) alone cost -$709 over 10 trades.

The swing engine is already built (`trading/swing_manager.py`, `trading/eod_holds.py`, `trading/closer.py`). It is under-used because the 7-gate AND qualification (`qualifies_for_eod_hold`) almost always rejects candidates, and `SWING_MAX_POSITIONS = 2` means it fills immediately and blocks everything else.

---

## Scope

Four independent changes, all Phase 1:

1. **Swing-first hold logic** — relax EOD qualification to 3 hard-veto gates (fail-fast) instead of 7 pass-all gates
2. **Composite floor raise** — `COMPOSITE_MIN_NORMAL` 30 → 50; `COMPOSITE_MIN_HIGH_VOL` 35 → 55
3. **Entry price cap** — new `MAX_ENTRY_PRICE = 400`; enforced in `check_all_gates()`
4. **Ticker prune** — remove persistent losers from `tickers.json`

Phase 2 (scaled entry / buy-the-dip) is out of scope.

---

## Design

### 1. Swing-first hold logic

**File:** `trading/eod_holds.py` → `qualifies_for_eod_hold()`

Current: 7 gates, all must pass (AND logic). Any failure → force-close.  
New: 3 hard-veto gates. Any veto → force-close. Everything else → swing.

**Hard vetoes (keep):**
| Gate | Rationale |
|---|---|
| `regime == CRISIS` | No overnight exposure during crisis — capital preservation |
| `earnings TOMORROW` (1 day, not 2) | Earnings binary risk is unhedgeable overnight |
| `current_pnl_pct < SWING_MIN_PNL_PCT` (-2.0%) | PTJ rule: don't hold losers overnight |

**Removed gates:**
| Gate | Why removed |
|---|---|
| `is_tier1` | Tier1 was a legacy quality proxy; composite ≥ 50 now does that job |
| `event_risk_tomorrow` (all events) | Too broad; major events (FOMC) already covered by regime shift to DEFENSIVE/CRISIS |
| `sector_sentiment_score < 10` | Weak signal; removes too many valid candidates |
| `existing_swing_count >= SWING_MAX_POSITIONS` | Becomes a soft cap (raise limit; don't use as hard gate in pure function) |

**Signature change:** Remove `is_tier1`, `event_risk_tomorrow`, `sector_sentiment_score` parameters from `qualifies_for_eod_hold()`. The function becomes pure with 4 inputs: `symbol`, `regime`, `earnings_tomorrow`, `current_pnl_pct`.

The `existing_swing_count` / `SWING_MAX_POSITIONS` check moves to `_promote_to_swing()` where it already runs under lock — no logic change there, just raise the cap.

**Backtest parity:** `BACKTEST_SWING_COMPOSITE_MIN` currently 40 → raise to 50 to match new live floor.

### 2. Composite floor

**File:** `config.py`

| Constant | Old | New | Rationale |
|---|---|---|---|
| `COMPOSITE_MIN_NORMAL` | 30 | 50 | Live data: 56% win rate at ≥50 vs 0–22% below |
| `COMPOSITE_MIN_HIGH_VOL` | 35 | 55 | Maintain 5-point spread above normal |
| `AUTO_EXECUTE_COMPOSITE_MIN` | 65 | 70 | Keep auto-execute well above the new floor |
| `ENTRY_LATE_COMPOSITE_MIN` | 50 | 55 | Late-hour entries warrant tighter filter |
| `BACKTEST_SWING_COMPOSITE_MIN` | 40 | 50 | Match live floor for backtest accuracy |
| `SWING_MAX_POSITIONS` | 2 | 6 | Allow more concurrent swings |

`SQUEEZE_COMPOSITE_MIN` (12) and `SENTIMENT_WEAK_COMPOSITE_MIN` (30) stay unchanged — these are secondary thresholds that don't gate entries directly.

### 3. Entry price cap

**File:** `config.py` — add `MAX_ENTRY_PRICE = 400`  
**File:** `signals/entry_gates.py` — add one gate in `check_all_gates()`

Gate logic:
```python
if config.MAX_ENTRY_PRICE and current_price > config.MAX_ENTRY_PRICE:
    return GateResult(passed=False, reason=f"price ${current_price:.0f} > MAX_ENTRY_PRICE ${config.MAX_ENTRY_PRICE}")
```

Placed after the existing soft-flag checks, before returning. Pure — no I/O.  
Backtest: add same check in simulator's entry filter.

**Why $400:** Stocks >$500 lose -$55/trade avg at 28% win rate (30d backtest). $400 cap excludes the worst offenders (LITE $800–1000, COHR $400+) while keeping ORCL ($184), MRVL ($155), PLTR ($140) etc.

### 4. Ticker prune

**File:** `data/tickers.json`

Remove these 8 persistent losers (all had negative net PnL over 30d with low win rates):

| Symbol | 30d Net | Win rate | Reason |
|---|---|---|---|
| LITE | -$709 | 30% | High-price, repeated SL hits |
| MU | -$336 | 0% | Volatile, not in bot's signal window |
| COHR | -$317 | 27% | High-price ($400+), consistently loses |
| QCOM | -$311 | 0% | SL hits every trade in window |
| SHOP | -$269 | 0% | 3/3 losses |
| TTD | -$217 | 33% | Low win rate vs peers |
| INTC | -$210 | 30% | Secular downtrend |
| ZS | -$209 | 33% | 3/3 losses |

Note: BKNG (-$221) and NOW (-$185) are borderline; leave in for now (small sample sizes).

---

## File Change Summary

| File | Change |
|---|---|
| `config.py` | 6 constant changes + 1 new (`MAX_ENTRY_PRICE`) |
| `trading/eod_holds.py` | `qualifies_for_eod_hold()`: remove 4 params, remove 4 gates |
| `trading/eod_holds.py` | `evaluate_eod_holds()`: remove 3 call-site args |
| `signals/entry_gates.py` | Add price-cap gate in `check_all_gates()` |
| `backtest/simulator.py` | Add price-cap filter at entry |
| `data/tickers.json` | Remove 8 tickers |
| `tests/test_entry_gates.py` | Tests for price-cap gate |
| `tests/test_signals.py` | Update any composite-threshold tests |
| `tests/test_foundation.py` | Update `qualifies_for_eod_hold` tests (fewer params) |

---

## Testing Requirements

- `test_entry_gates.py`: price above cap → gate blocks; price at cap → passes; `MAX_ENTRY_PRICE = 0` disables check
- `test_foundation.py`: swing qualify with 3-veto logic; all 3 vetoes individually block; non-tier1 still qualifies; low sector-sentiment still qualifies
- Backtest re-run 30d after implementation; swing % should rise from 10% → 25%+ of trades

---

## Risk & Rollback

- Raising composite floor reduces trade count — expected. Days with 0 trades are possible.
- Swing-first increases overnight exposure — mitigated by: CRISIS hard-veto, PTJ rule (no losers), SWING_OVERNIGHT_SL_PCT = 3.0% already in place.
- If overnight exposure causes drawdown: set `SWING_MAX_POSITIONS = 2` in config to revert immediately without code change.
- Ticker prune is reversible: add back to `tickers.json`.
