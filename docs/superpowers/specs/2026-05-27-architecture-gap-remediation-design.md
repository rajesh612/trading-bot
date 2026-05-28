# Architecture Gap Remediation Design
**Date:** 2026-05-27
**Status:** Approved

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

During Apr–May 2026 tariff recovery, VIX was declining from ~45 but consecutive green SPY days were only achieved after day 3–4. Backtest triggered CRASH_RECOVERY 1–2 days early (VIX was still elevated); live bot waited for `consecutive_green_days >= 2`, entering at better confirmation. This explains the -37% alpha gap in the 75d backtest.

### Fix

**`config.py`** — add under `# ── Backtest Regime ──`:
```python
CRASH_RECOVERY_MIN_GREEN_DAYS: int = 2
```

**`backtest/data.py`** — in `_build_regime_series()` (or equivalent daily-data assembly step):
1. Compute `consecutive_green_days` as a rolling column: reset to 0 on any day where `spy_close <= spy_close_prev`; increment by 1 on green days
2. Pass `consecutive_green_days` into `_classify_vix_day()` as a parameter
3. Gate CRASH_RECOVERY on `consecutive_green_days >= config.CRASH_RECOVERY_MIN_GREEN_DAYS` alongside existing VIX criteria

No changes to live bot path — the live `classify_regime()` already enforces this.

---

## Gap 2: Swing Promotion Quality Gate

### Root Cause

`BACKTEST_SWING_COMPOSITE_MIN = 50` is flat — identical for all regimes and tiers. No RSI cap exists at promotion time.

The NORMAL MEDIUM floor sits at -4.55% (SL floor + slippage). All losing swings cluster at exactly this level — late-momentum longs (RSI 68–74, composite 50–55) promoted at 3:50 PM, fading overnight.

`SWING_HOLD_PARAMS` already has regime×tier granularity for hold-duration criteria but composite and RSI are not in that table.

### Fix

**New frozen dataclass in `utils/regime_params.py`**:
```python
@dataclass(frozen=True)
class SwingGateParams:
    composite_min: int
    rsi_ceiling: int
```

**`_SWING_GATE_TABLE` keyed on `(regime, tier)`**:

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

CHOPPY / DEFENSIVE / HIGH_VOL / CRISIS: swing is ineligible (`SwingParams.eligible=False`) — `SwingGateParams` does not apply.

**`get_swing_gate_params(regime: str, tier: str) -> SwingGateParams`** — sole lookup function. Falls back to NORMAL MEDIUM (`composite_min=58, rsi_ceiling=70`) for unknown combos.

**Callsites** — both use `get_swing_gate_params(regime, tier)`:
- `backtest/simulator.py` `_evaluate_swing_promotion()`: replace `composite >= BACKTEST_SWING_COMPOSITE_MIN` with `composite >= sgp.composite_min` and add `iv.rsi < sgp.rsi_ceiling`
- `trading/swing_manager.py` `evaluate_eod_holds()`: same gate using the position's quality tier

**`config.SWING_HOLD_PARAMS`** — update NORMAL MEDIUM `min_pnl_pct` from 0.8 → 1.2%:
```python
("NORMAL",  "MEDIUM"): (7, 1.5, 1.2),   # was (7, 1.5, 0.8)
```

**Retirement** — `config.BACKTEST_SWING_COMPOSITE_MIN` is deleted. Any reference must use `get_swing_gate_params()` instead.

---

## Gap 3: Force-Close Entry-Hour Tag

### Instrumentation Only (analyse first)

At BUY time in `trading/executor.py`, write `"entry_hour": datetime.now(ET).hour` into the position dict saved to `open_positions.json`.

At `force_close_all()` in `trading/closer.py`, read `entry_hour` from the open position record and include it in the closed-trade record written to `trade_journal.json`.

No analysis logic in this pass. Enables future correlation: which entry hours produce the most FORCE_CLOSE exits.

---

## Files Changed

| File | Change |
|---|---|
| `config.py` | Add `CRASH_RECOVERY_MIN_GREEN_DAYS`; update `SWING_HOLD_PARAMS` NORMAL MEDIUM; delete `BACKTEST_SWING_COMPOSITE_MIN` |
| `utils/regime_params.py` | Add `SwingGateParams` dataclass, `_SWING_GATE_TABLE`, `get_swing_gate_params()` |
| `backtest/data.py` | Compute `consecutive_green_days`; pass to `_classify_vix_day()`; gate CRASH_RECOVERY |
| `backtest/simulator.py` | Replace flat composite gate + add RSI cap using `get_swing_gate_params()` |
| `trading/swing_manager.py` | Add RSI cap + regime×tier composite gate using `get_swing_gate_params()` |
| `trading/executor.py` | Write `entry_hour` into open position record |
| `trading/closer.py` | Copy `entry_hour` from open position into closed-trade record |

## Tests

- `test_config_coherence.py` — assert `BACKTEST_SWING_COMPOSITE_MIN` removed; assert `SwingGateParams` fallback == NORMAL MEDIUM; assert all swing-eligible regimes have full tier coverage in `_SWING_GATE_TABLE`
- `test_pipeline.py` — assert CRASH_RECOVERY fires on day 2+ of green SPY, not day 0
- `test_signals.py` or `test_foundation.py` — assert swing promotion blocked when `rsi >= sgp.rsi_ceiling`; assert swing promotion blocked when `composite < sgp.composite_min`; assert NORMAL MEDIUM `min_pnl_pct == 1.2`
- `test_trading_lifecycle.py` — assert `entry_hour` present in FORCE_CLOSE trade record
