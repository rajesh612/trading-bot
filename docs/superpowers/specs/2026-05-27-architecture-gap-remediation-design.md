# Architecture Gap Remediation Design
**Date:** 2026-05-27
**Status:** Approved (patched after code cross-reference)

## Overview

Three architecture gaps identified via 45d/75d backtest analysis and pipeline audit:

1. **Gap 1** — Backtest CRASH_RECOVERY classifier diverges from live bot: misses recovery rallies
2. **Gap 2** — Swing promotion uses flat composite threshold + no RSI cap: promotes late-momentum longs
3. **Gap 3** — FORCE_CLOSE trades lack `entry_hour` tag: no data to correlate intraday entry time with EOD exits

---

## Gap 1: Backtest Regime Parity

### Root Cause

`backtest/data.py` `_classify_vix_day()` requires:
- `vix_current >= 35.0` AND not rising AND `vix_drop_pct >= 10%` → CRASH_RECOVERY

Live `macro/regime_classifier.py` `classify_regime()` additionally requires:
- `post_crisis_recovery == True` AND `consecutive_green_days >= 2`

During Apr–May 2026 tariff recovery, VIX was declining from ~45 but consecutive green SPY days were only achieved after day 3–4. Backtest triggered CRASH_RECOVERY 1–2 days early (VIX criteria met before market confirmed recovery); live bot waited for `consecutive_green_days >= 2`. This explains the -37% alpha gap in the 75d backtest.

**Out-of-scope note:** The backtest classifier only ever returns 4 regimes (CRISIS / CRASH_RECOVERY / HIGH_VOL / NORMAL). The live bot returns 9. BULL_TREND is entirely absent from backtest — the bot ran the Apr 2026 rally under NORMAL/HIGH_VOL. This is the next gap after this spec.

### Fix

**`config.py`** — add under `# ── Backtest Regime ──`:
```python
CRASH_RECOVERY_MIN_GREEN_DAYS: int = 2
```

**`backtest/data.py`** — inside `_classify_vix_day()` (which already receives `past_spy`):

Compute consecutive green days directly from the existing `past_spy` list — no signature change required:
```python
# Count trailing consecutive green SPY days (each close > prior close)
green_streak = 0
for i in range(len(past_spy) - 1, 0, -1):
    if past_spy[i][1] > past_spy[i - 1][1]:
        green_streak += 1
    else:
        break
```

Gate CRASH_RECOVERY on `green_streak >= config.CRASH_RECOVERY_MIN_GREEN_DAYS` alongside the existing VIX criteria:
```python
is_crash_recovery = (
    vix_current >= config.BACKTEST_CRISIS_VIX_THRESHOLD
    and not vix_rising
    and vix_drop_pct >= config.BACKTEST_CRISIS_VIX_RECOVERY_DROP_PCT
    and green_streak >= config.CRASH_RECOVERY_MIN_GREEN_DAYS   # ← new
)
```

No changes to live bot path — `classify_regime()` already enforces this.

---

## Gap 2: Swing Promotion Quality Gate

### Root Cause

`BACKTEST_SWING_COMPOSITE_MIN = 50` is flat — identical for all regimes and tiers. No RSI cap exists at promotion time. The live bot's `qualifies_for_eod_hold()` in `trading/eod_holds.py` has **no composite check at all** — only profit_lock bypass, eligible flag, min_pnl_pct, and sector-reversal vetos.

The NORMAL MEDIUM SL floor sits at -4.55% (floor + slippage). Losing swings cluster exactly there — late-momentum longs (RSI 68–74, composite 50–55) promoted at 3:50 PM, fading overnight.

### Fix

#### New frozen dataclass — `utils/regime_params.py`

```python
@dataclass(frozen=True)
class SwingGateParams:
    composite_min: int
    rsi_ceiling: int
```

#### `_SWING_GATE_TABLE` keyed on `(regime, tier)`

| regime | tier | composite_min | rsi_ceiling |
|---|---|---|---|
| BULL_TREND | CONVICTION | 50 | 75 |
| BULL_TREND | HIGH | 53 | 73 |
| BULL_TREND | MEDIUM | 55 | 72 |
| BULL_TREND | LOW | 58 | 69 |
| NORMAL | CONVICTION | 52 | 73 |
| NORMAL | HIGH | 55 | 71 |
| NORMAL | MEDIUM | 58 | 70 |
| NORMAL | LOW | 62 | 67 |
| OPPORTUNITY | CONVICTION | 50 | 75 |
| OPPORTUNITY | HIGH | 53 | 73 |
| OPPORTUNITY | MEDIUM | 56 | 71 |
| OPPORTUNITY | LOW | 60 | 68 |
| CRASH_RECOVERY | CONVICTION | 52 | 70 |
| CRASH_RECOVERY | HIGH | 55 | 68 |
| CRASH_RECOVERY | MEDIUM | 58 | 66 |
| CRASH_RECOVERY | LOW | 62 | 63 |
| CAPITULATION | CONVICTION | 54 | 68 |
| CAPITULATION | HIGH | 57 | 65 |
| CAPITULATION | MEDIUM | 60 | 63 |
| CAPITULATION | LOW | 63 | 60 |

CHOPPY / DEFENSIVE / HIGH_VOL / CRISIS: swing ineligible (`SwingParams.eligible=False`) — `SwingGateParams` does not apply. Fallback for unknown combos: NORMAL MEDIUM (`composite_min=58, rsi_ceiling=70`).

**`get_swing_gate_params(regime: str, tier: str) -> SwingGateParams`** — sole lookup, sole authority (Rule 24/29).

#### RSI source — live vs backtest

**Live bot** (`trading/eod_holds.py`): `executor.py` already persists `"entry_indicators": asdict(iv)` in the position record, which contains `rsi`. At promotion time, read `entry_rsi = pos.get("entry_indicators", {}).get("rsi", 100.0)`. No new BUY-time field required.

**Backtest** (`backtest/simulator.py`): `simulate_day()` builds `precomp[sym][ts]` — the bar's full `IndicatorValues` at the swing eval timestamp (~3:50 PM). Pass `precomp` into `_evaluate_swing_promotion()` as a new parameter; use `precomp.get(sym, {}).get(ts)` for EOD RSI. This gives a more accurate gate (current RSI, not entry RSI) and matches how the backtest already uses precomp for T2 gates.

Note: live uses entry RSI; backtest uses EOD RSI. Acceptable asymmetry — both prevent late-momentum promotions; EOD RSI in backtest is stricter, which is conservative.

#### Callsite changes

**`trading/eod_holds.py` `qualifies_for_eod_hold()`** — add two params and one gate block:
```python
def qualifies_for_eod_hold(
    ...,
    entry_rsi: float = 100.0,
    entry_composite: float = 0.0,
) -> tuple[bool, str]:
```
Gate inserted **after** profit_lock bypass, **before** min_pnl_pct (fail-fast on quality first):
```python
from utils.regime_params import get_swing_gate_params
sgp = get_swing_gate_params(regime, tier)
if entry_rsi >= sgp.rsi_ceiling:
    return False, f"RSI {entry_rsi:.0f} >= ceiling {sgp.rsi_ceiling} for {regime}/{tier}"
if entry_composite < sgp.composite_min:
    return False, f"composite {entry_composite:.0f} < min {sgp.composite_min} for {regime}/{tier}"
```

**`trading/eod_holds.py` `evaluate_eod_holds()`** — pass values from position record:
```python
entry_indicators = pos.get("entry_indicators", {})
qualifies, reason = qualifies_for_eod_hold(
    ...,
    entry_rsi=float(entry_indicators.get("rsi", 100.0)),
    entry_composite=float(pos.get("entry_composite_score", 0.0)),
)
```

**`backtest/simulator.py` `_evaluate_swing_promotion()`** — add `precomp` param, use EOD RSI:
```python
def _evaluate_swing_promotion(
    state, day_bars, ts, regime, n_swings,
    quality_map=None, precomp=None,   # ← precomp added
) -> int:
```
Replace flat composite gate and add RSI gate:
```python
sgp = get_swing_gate_params(regime, tier)
iv_eod = (precomp or {}).get(sym2, {}).get(ts)
eod_rsi = getattr(iv_eod, "rsi", 100.0) if iv_eod else 100.0
standard_qualify = (
    (sp.eligible or choppy_conviction_ok)
    and pnl_pct >= p.min_pnl_pct
    and pos2.get("composite", 0) >= sgp.composite_min    # regime×tier (was flat SWING_COMPOSITE_MIN)
    and eod_rsi < sgp.rsi_ceiling                        # new RSI cap
    and n_swings < SWING_MAX_POSITIONS
)
```
Update `simulate_day()` call at line ~473:
```python
n_swings = _evaluate_swing_promotion(state, day_bars, ts, regime, n_swings,
                                      precomp=precomp)
```

#### Config update

**`config.SWING_HOLD_PARAMS`** — update NORMAL MEDIUM `min_pnl_pct` 0.8 → 1.2% and fix inline comment:
```python
("NORMAL", "MEDIUM"): (7, 1.5, 1.2),   # max_days=7, profit_lock=1.5%, min_pnl=1.2%
```

**Retire** `config.BACKTEST_SWING_COMPOSITE_MIN` — delete the constant; all callers use `get_swing_gate_params()`.

---

## Gap 3: Force-Close Entry-Hour Tag

### Instrumentation Only (analyse first)

**`trading/executor.py`** — add to `position_data` dict:
```python
"entry_hour": now.hour,
```

**`trading/closer.py` `force_close_all()`** — add to `append_trade()` call:
```python
"entry_hour": pos.get("entry_hour"),
```

**`trading/closer.py` `close_position()`** — add to `append_trade()` call for SL/TP closes too:
```python
"entry_hour": pos.get("entry_hour"),
```

**`backtest/simulator.py` `Trade` dataclass** — add field:
```python
entry_hour: int = 0
```
Populate in `_close_trade()` from `pos.get("entry_ts")` → `datetime.fromisoformat(...).astimezone(ET).hour`.

No analysis logic in this pass. Enables future correlation: which entry hours produce the most FORCE_CLOSE exits in both live and backtest.

---

## Files Changed

| File | Change |
|---|---|
| `config.py` | Add `CRASH_RECOVERY_MIN_GREEN_DAYS`; update `SWING_HOLD_PARAMS` NORMAL MEDIUM to `(7, 1.5, 1.2)`; delete `BACKTEST_SWING_COMPOSITE_MIN` |
| `utils/regime_params.py` | Add `SwingGateParams` dataclass, `_SWING_GATE_TABLE` (20 entries), `get_swing_gate_params()` with NORMAL MEDIUM fallback |
| `backtest/data.py` | Add `green_streak` counter inside `_classify_vix_day()`; gate CRASH_RECOVERY on `green_streak >= CRASH_RECOVERY_MIN_GREEN_DAYS` |
| `backtest/simulator.py` | `_evaluate_swing_promotion()`: add `precomp` param, replace flat composite gate + add RSI cap via `get_swing_gate_params()`; update call site in `simulate_day()`; add `entry_hour` to `Trade` dataclass; populate in `_close_trade()` |
| `trading/eod_holds.py` | `qualifies_for_eod_hold()`: add `entry_rsi` + `entry_composite` params, insert composite + RSI gate after profit_lock bypass; `evaluate_eod_holds()`: pass values from position record |
| `trading/executor.py` | Add `"entry_hour": now.hour` to position_data dict |
| `trading/closer.py` | Add `"entry_hour": pos.get("entry_hour")` to both `force_close_all()` and `close_position()` journal records |

## Tests

| File | What to assert |
|---|---|
| `test_config_coherence.py` | `BACKTEST_SWING_COMPOSITE_MIN` removed; `get_swing_gate_params` fallback == NORMAL MEDIUM (58, 70); all swing-eligible regimes × 4 tiers present in table |
| `test_pipeline.py` | CRASH_RECOVERY fires on day 2+ consecutive green SPY, not day 0; NORMAL returned on day 1 |
| `test_foundation.py` | `qualifies_for_eod_hold()` blocked when `entry_rsi >= sgp.rsi_ceiling`; blocked when `entry_composite < sgp.composite_min`; passes when both within bounds; NORMAL MEDIUM `min_pnl_pct == 1.2` from `SWING_HOLD_PARAMS` |
| `test_trading_lifecycle.py` | `entry_hour` present in FORCE_CLOSE trade record; `entry_hour` present in SL-close trade record; backtest `Trade.entry_hour` populated correctly |
