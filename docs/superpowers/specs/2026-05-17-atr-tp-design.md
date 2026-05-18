# ATR-Based Dynamic Take-Profit Design

**Date:** 2026-05-17  
**Status:** Approved for Phase 1 implementation

---

## Problem

30-day backtest (2026-04-07 → 2026-05-15) shows 60% of trades force-close at 3:55 PM with average P&L of +0.5% instead of hitting the 5% TP target. Root cause: `TAKE_PROFIT_PCT = 5.0` is unreachable for most intraday moves. 29/168 trades (17%) hit TP; 101/168 are force-closed after holding all day.

TP trades average 2.3 hours hold time; force-closed trades sit 5 hours and plateau at ~0.5–1.5%. Capital is locked in stalled positions instead of being recycled into better setups.

The fix: replace the flat 5% TP with a per-stock ATR-based target that reflects the stock's actual intraday range.

---

## Benchmark Issue

SPY is the wrong benchmark for this bot. The watch list is dominated by speculative growth (IONQ, LUNR, CRWV, QBTS, ASTS) — an ARKK-class universe, not S&P 500 components. SPY gained ~14% during the April tariff recovery while the bot returned +4.95%, but the comparison is invalid because:
- Different stock universe (small/mid-cap growth vs large-cap blend)
- Cash drag: bot runs 10–25% gross exposure, not 100% invested
- Bot is intraday-only — can't capture overnight gap recoveries that drove SPY's +14%
- Max drawdown constrained to 3% vs SPY's 15%+ drawdown in the same period

**Better benchmark:** equal-weight daily buy-at-open / sell-at-close of the same watch-list symbols (added to `backtest/report.py` in Phase 1). This measures true alpha: did the bot's entries, exits, and timing add value over just holding the basket?

---

## Solution: ATR-Based TP

`TP% = max(ATR_TP_FLOOR_PCT, min(ATR_TP_CEILING_PCT, atr_pct × multiplier))`

Where:
- `atr_pct = (ATR / price) × 100` — stock's daily volatility as a percent
- `multiplier` = 2.0 default (tunable per regime, learned nightly)
- Floor = 1.5% (prevents trivially small TP on slow stocks)
- Ceiling = 12.0% (prevents runaway TP on explosive movers)

Examples with multiplier = 2.0:
- IONQ (ATR 3.5%) → TP 7%
- LUNR (ATR 4.2%) → TP 8.4%
- NET  (ATR 1.8%) → TP 3.6%
- CRWV (ATR 5.1%) → TP 10.2%

---

## Gate and Workflow Preservation

This change affects only the **exit price target** on an already-approved trade. The full pre-entry pipeline is unchanged:

```
Regime (VIX/SPY) → Volatility screener → Watch-list agent (Claude, 9:00 AM)
         ↓
Research agent (sentiment, PEAD, earnings, __meta__ — 9:15 AM)
         ↓
Every 10-min cycle:
  pre_claude_filter (quality router: SKIP / AUTO_EXECUTE / CLAUDE)  ← unchanged
         ↓
  check_all_gates() (time, earnings, event-risk, Stoch, parabolic,  ← unchanged
                     gap-skip, breadth, volume)
         ↓
  CLAUDE path: compute_atr_tp_pct() → inject into Claude prompt context
               Claude returns take_profit_pct (may accept or adjust)
               execute_buy() uses Claude's value
         ↓
  AUTO_EXECUTE path: compute_atr_tp_pct() → directly into execute_buy()
```

Claude retains full authority to override the ATR-based suggestion — if Claude sees a strong catalyst it can widen TP; if momentum is weak it can tighten it. The ATR suggestion is context, not a constraint.

Note on watch-list timing: the watch-list agent intentionally runs at 9:00 AM before the 9:15 AM research agent. The watch-list is a technical candidate universe (ATR/momentum ranking). Research, PEAD, and event-risk enforcement happen inside each cycle via `pre_claude_filter` + `check_all_gates()`. This separation of concerns is correct and unchanged.

---

## Implementation Phases

### Phase 1 — Backtest Validation (implement now)

Validate that ATR-based TP improves backtest performance before touching live code.

**Files changed:**
1. `config.py` — 5 new constants under `# ── ATR-based TP ──`
2. `signals/atr_tp.py` — new pure function `compute_atr_tp_pct(atr, price, multiplier)`
3. `backtest/simulator.py` — `_open_trade()` uses ATR-based TP; `SimState` gets `atr_tp_multiplier` field
4. `backtest/runner.py` — loads `atr_tp_multiplier` from trading_params (default `ATR_TP_MULT_DEFAULT`)
5. `backtest/report.py` — add universe benchmark (equal-weight daily hold of watch list)

**Validation window: 60 trading days minimum.** 30 days is insufficient — it starts at the April tariff recovery (a specific market condition) with only 6 HIGH_VOL days and produces only ~168 trades, too few to distinguish signal from noise across multipliers. 60d covers multiple regime transitions (HIGH_VOL → NORMAL → NORMAL) and ~300+ trades for meaningful Sharpe comparison.

Run backtest sweep at multipliers 1.0, 1.5, 2.0, 2.5, 3.0. Compare total P&L, win rate, % trades force-closed, and Sharpe. If 2.0× improves on current results, proceed to Phase 2.

**New constants in `config.py`:**
```python
ATR_TP_MULT_DEFAULT = 2.0
ATR_TP_MULT_MIN     = 1.0
ATR_TP_MULT_MAX     = 4.0
ATR_TP_FLOOR_PCT    = 1.5
ATR_TP_CEILING_PCT  = 12.0
```

**`signals/atr_tp.py`** (pure, no I/O):
```python
from config import ATR_TP_FLOOR_PCT, ATR_TP_CEILING_PCT

def compute_atr_tp_pct(atr: float, price: float, multiplier: float) -> float:
    """TP% = ATR% × multiplier, clamped to [floor, ceiling]."""
    atr_pct = (atr / price) * 100 if price > 0 else 0
    return max(ATR_TP_FLOOR_PCT, min(ATR_TP_CEILING_PCT, atr_pct * multiplier))
```

**`backtest/simulator.py` change:**
- `SimState` dataclass gains `atr_tp_multiplier: float = 2.0`
- `_open_trade()`: replace `tp = round(entry * (1 + TAKE_PROFIT_PCT / 100), 4)` with
  ```python
  tp_pct = compute_atr_tp_pct(atr, entry, state.atr_tp_multiplier)
  tp = round(entry * (1 + tp_pct / 100), 4)
  ```

**Universe benchmark in `backtest/report.py`:**
- For each day: equal-weight buy-at-open / sell-at-close of all watch-list symbols
- Report: bot total return vs universe return = alpha
- Add to summary table alongside current metrics

### Phase 2 — Live Trading (after Phase 1 validated)

**Files changed:**
1. `trading/pipeline.py` — compute `atr_suggested_tp_pct` and inject into Claude context block
2. `trading/executor.py` — use `compute_atr_tp_pct()` for AUTO_EXECUTE path
3. `data/state/trading_params.json` — add `atr_tp_multiplier: 2.0`

**Claude context injection (pipeline.py):**
```python
atr_suggested_tp_pct = compute_atr_tp_pct(iv.atr, iv.current_price, atr_tp_multiplier)
# Add to Claude prompt: "ATR for {symbol} = {iv.atr_pct:.1f}% daily range.
#  Calibrated TP = {atr_suggested_tp_pct:.1f}% (2× ATR). Adjust if catalyst justifies."
```

**Executor AUTO_EXECUTE path:**
```python
tp_pct = compute_atr_tp_pct(iv.atr, iv.current_price, atr_tp_multiplier)
```

Also: store `atr_pct` in the BUY journal record to enable Phase 3.

### Phase 3 — Learning Agent Auto-Tuning (after Phase 2 validated)

**Files changed:**
1. `learning/agent.py` — add `atr_tp_multiplier` grid search using recent trade ATRs
2. `trading/journal.py` — confirm `atr_pct` is stored per BUY trade

**Setup requirements for Phase 3:**

Phase 3 cannot produce useful results until Phase 2 has been live for at least `LEARNING_ROLLING_DAYS` (5 trading days = 1 week). The learning agent must have a minimum number of ATR-annotated trades before the grid search runs.

1. **Phase 2 live prerequisite:** `atr_pct` must be stored in every BUY journal record (Phase 2 delivers this). Phase 3 will silently skip the multiplier update if fewer than 15 trades in the rolling window have `atr_pct`.
2. **Minimum trade guard:** `if len([t for t in trades if t.get("atr_pct")]) < 15: skip update`. Keeps current multiplier rather than producing a noisy result from too-few trades.
3. **Sharpe edge case:** if simulated P&L values have std = 0 (e.g., all trades hit TP at the same multiplier), fall back to average P&L as the ranking metric.
4. **No scheduler change needed:** Phase 3 logic runs inside the existing 4:15 PM `run_learning_agent()` job. No new cron jobs required.
5. **Bootstrap period:** the multiplier stays at `ATR_TP_MULT_DEFAULT = 2.0` until Phase 2 accumulates 15+ annotated trades. This takes 2–3 live trading days at current trade frequency (~6 trades/day).

**Optimization logic (learning agent, nightly):**
- Load last `LEARNING_ROLLING_DAYS` closed trades from journal
- Filter to trades with `atr_pct` field set (Phase 2 annotates these)
- If fewer than 15 annotated trades → skip multiplier update, log reason
- For each trade: simulate TP hit at multipliers [1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0]
- Rank by Sharpe (`mean / std`); fall back to mean if std = 0
- Pick best multiplier; clamp to `[ATR_TP_MULT_MIN, ATR_TP_MULT_MAX]`
- Write to `trading_params.json` as `atr_tp_multiplier`
- Claude's existing param update also allowed to tune `atr_tp_multiplier`

---

## Testing Requirements

All tests marked `@pytest.mark.unit`. No network calls. Patch file paths via `tmp_path`.

**Phase 1 tests (new file: `tests/test_atr_tp.py`):**
- `test_compute_atr_tp_pct_normal`: ATR=3.5%, price=$50, mult=2.0 → TP=7%
- `test_compute_atr_tp_pct_floor`: ATR=0.3%, price=$100, mult=2.0 → TP=1.5% (floor)
- `test_compute_atr_tp_pct_ceiling`: ATR=10%, price=$50, mult=2.0 → TP=12% (ceiling)
- `test_compute_atr_tp_pct_zero_price`: returns floor without division error
- `test_simulator_uses_atr_tp`: SimState with multiplier=2.0 sets per-stock TP correctly
- `test_runner_loads_atr_tp_multiplier`: runner passes multiplier to simulator
- `test_universe_benchmark_computed`: report includes equal-weight benchmark return

**Phase 2 tests (add to `tests/test_trading_pipeline.py` and `tests/test_trading_budget.py`):**
- `test_atr_tp_injected_into_claude_context`: Claude prompt contains `atr_suggested_tp_pct`
- `test_auto_execute_uses_atr_tp`: auto-execute path uses ATR-based TP, not TAKE_PROFIT_PCT
- `test_journal_stores_atr_pct`: BUY record in journal includes `atr_pct` field

**Phase 3 tests (add to `tests/test_learning.py`):**
- `test_learning_agent_tunes_atr_multiplier`: agent saves `atr_tp_multiplier` to trading_params
- `test_multiplier_clamped_to_bounds`: result in [ATR_TP_MULT_MIN, ATR_TP_MULT_MAX]
- `test_grid_search_picks_best_sharpe`: verifies selection logic on mocked trade data

---

## Success Criteria

Phase 1 passes if 30-day backtest with ATR multiplier=2.0 shows:
- % force-closed trades drops from 60% to < 40%
- Total P&L ≥ current +4.95%
- Max drawdown stays ≤ 2%
- Universe alpha ≥ 0 (bot beats equal-weight hold of its own watch list)

Phase 2 passes if first live week shows no regressions in gate hit rates or error logs.

Phase 3 passes if nightly learning agent successfully writes `atr_tp_multiplier` to trading_params and value stays within [1.0, 4.0].
