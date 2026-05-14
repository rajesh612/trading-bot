# Trading Agent Modularization + Gate Unification Design
**Date:** 2026-05-14  
**Status:** Approved  

---

## Problem

Two compounding issues prevent reliable automated trading:

1. **`trading_agent.py` violates CLAUDE.md Rules 6, 7, and 17.** It sits at root level (Rule 6), re-exports symbols from `trading.journal`, `trading.closer`, and `utils.market_monitor` instead of callers importing directly (Rule 7), and is 303 lines (Rule 17 cap: 300). All three violations in one file.

2. **Gate thresholds diverge across three layers** (pre_claude_filter → check_all_gates → executor), creating silent dead zones: stocks that will never trade because they're blocked before Claude but would be allowed past execution gates, or blocked after Claude on a check that could have been done cheaply before the API call.

---

## Design

### Part 1 — Module Split

`trading_agent.py` is deleted. Its three concerns move to focused modules under `trading/`:

| New file | Lines (est.) | Responsibility |
|---|---|---|
| `trading/cycle.py` | ~130 | `run_trading_cycle()` + `_zero_pass` dead-day tracker |
| `trading/monitor.py` | ~90 | `run_position_monitor()` + bracket reconciliation + swing monitor call |
| `trading/watch_loader.py` | ~10 | `load_watch_list()` — pure file I/O, no business logic |

No shim left at root level. All callers updated to import directly from the canonical source:

| Symbol | Old: `trading_agent` | New: canonical source |
|---|---|---|
| `run_trading_cycle` | — | `trading.cycle` |
| `run_position_monitor` | — | `trading.monitor` |
| `load_watch_list` | — | `trading.watch_loader` |
| `force_close_all` | re-export | `trading.closer` |
| `load_open_positions` | re-export | `trading.journal` |
| `save_open_positions` | re-export | `trading.journal` |
| `append_trade` | re-export | `trading.journal` |
| `reset_daily_closed` | re-export | `trading.journal` |
| `archive_old_journal_entries` | re-export | `trading.journal` |
| `run_market_monitor` + internals | re-export | `utils.market_monitor` |

**Files with callers to update:** `scheduler_jobs.py`, `main.py`, `utils/startup.py`, `utils/market_monitor.py`, `utils/bot_health.py`, `trading/eod_holds.py`.

---

### Part 2 — Gate Unification

#### 2.1 The Problem: Three-layer divergence

```
pre_claude_filter  →  check_all_gates  →  executor.py
     layer 1–5           (hard gates)     (pre-flight)
```

Currently these three layers use **different constants for the same concept**, creating unreachable composite ranges and wasted Claude calls:

| Concept | pre_claude_filter | executor | Gap |
|---|---|---|---|
| Composite min (NORMAL) | `PRE_CLAUDE_COMPOSITE_MIN=20` | `COMPOSITE_SCORE_THRESHOLD=15` | Composite 15–19 can never trade |
| Composite min (HIGH_VOL) | `HIGH_VOL_COMPOSITE_MIN=35` | not checked | Stock passes executor unchecked |
| Composite min (CHOPPY) | not checked (uses 20) | `CHOPPY_COMPOSITE_GATE=30` | Composite 20–29 in CHOPPY pays Claude then fails |
| RSI overbought | `RSI_HARD_SKIP_THRESHOLD=72` | `RSI_BUY_MAX=75` | RSI 73–74 pre-blocked, executor would allow |
| Sentiment hard block | `SENTIMENT_HARD_BLOCK=−15` | duplicate check | Post-Claude redundancy |
| VWAP slope downtrend | not checked | `params[vwap_slope_min]` | Pays Claude, then silently fails |
| VWAP overextension | not checked | `passes_vwap_guard()` in check_all_gates | Same waste |

#### 2.2 Config changes

**Remove dead/conflicting constants:**

| Constant | Action | Reason |
|---|---|---|
| `COMPOSITE_SCORE_THRESHOLD = 15` | **Delete** | Unreachable — pre_claude_filter already requires ≥20 for NORMAL |
| `RSI_HARD_SKIP_THRESHOLD = 72` | **Delete** | Merged into `RSI_BUY_MAX` |
| `PRE_CLAUDE_COMPOSITE_MIN = 20` | **Rename** → `COMPOSITE_MIN_NORMAL = 20` | Name implied it was only for pre-Claude; it is now the system-wide NORMAL gate |
| `HIGH_VOL_COMPOSITE_MIN = 35` | **Rename** → `COMPOSITE_MIN_HIGH_VOL = 35` | Consistent naming |
| `RSI_BUY_MAX = 75` | **Change to 72** | Unify with old `RSI_HARD_SKIP_THRESHOLD`; 72 is the correct conservative cap |

**Constants that stay (no rename):**

| Constant | Value | Used by |
|---|---|---|
| `CHOPPY_COMPOSITE_GATE = 30` | 30 | pre_claude_filter + executor (now shared) |
| `HERD_ENTRY_GATE = 25` | 25 | pre_claude_filter + executor (now shared) |
| `RSI_BUY_MAX_HIGH_VOL = 65` | 65 | pre_claude_filter + executor (now shared) |
| `VWAP_SLOPE_MIN = -0.3` | −0.3 | pre_claude_filter + executor fallback |
| `STOCH_HARD_VETO_THRESHOLD = 88` | 88 | check_all_gates only |
| `PARABOLIC_ENTRY_BLOCK = True` | True | check_all_gates only |
| `GAP_SKIP_PCT = 3.0` | 3.0 | check_all_gates only |

#### 2.3 `effective_composite_gate()` — single source of truth

Move from `executor.py` → `signals/entry_gates.py`. Both pre_claude_filter and executor import it. Updated to cover ALL regimes:

```python
def effective_composite_gate(iv: IndicatorValues, regime: str) -> int:
    """Single source of truth for composite entry threshold by regime and signal context."""
    if regime in ("HIGH_VOL", "DEFENSIVE"):
        return config.COMPOSITE_MIN_HIGH_VOL   # 35
    if iv.squeeze_setup:
        return 12                              # squeeze override (strong signal)
    if iv.bot_herding_score >= config.BOT_HERD_ALERT_SCORE:
        return config.HERD_ENTRY_GATE          # 25
    if regime == "CHOPPY":
        return config.CHOPPY_COMPOSITE_GATE   # 30
    return config.COMPOSITE_MIN_NORMAL        # 20
```

Priority order: HIGH_VOL (safety first) → squeeze_setup → herding → CHOPPY → NORMAL.

#### 2.4 `pre_claude_filter.py` revised layers

```
Layer 0:  Already closed today (unchanged)
Layer 1:  composite < effective_composite_gate(iv, snap.regime)
          → unified thresholds, same function as executor
Layer 2:  RSI > regime RSI max
          → snap.regime in ("HIGH_VOL", "DEFENSIVE"): RSI_BUY_MAX_HIGH_VOL (65)
          → all other regimes: RSI_BUY_MAX (72)
          (same constants executor already passes as rsi_max)
Layer 2.5 VWAP:
          → slope: iv.vwap_slope < snap.params.get("vwap_slope_min", VWAP_SLOPE_MIN)
          → overextension: not passes_vwap_guard(iv)
Layer 3:  Macro health + shock (unchanged)
Layer 4:  Research sentiment (unchanged)
Layer 5:  Intraday news (unchanged)
```

#### 2.5 `signals/entry_gates.py` cleanup

- **Delete** `fast_gate()` — never called after 2026-05-14 fix; its checks are covered by check_all_gates and pre_claude_filter.
- **Remove** duplicate RSI lines 119–120 and Stochastic lines 123–124 from `check_all_gates` — fast_gate no longer exists to be the "first copy."
- **Add** import of `effective_composite_gate` (moved from executor).

`check_all_gates` hard gates that stay (these belong here, not in pre_claude_filter, because they require live external data or state):

| Gate | Reason it stays in check_all_gates |
|---|---|
| Time gate (open 30 min / close 35 min) | Checked at execution time, not pre-filter time |
| Per-cycle entry cap | Runtime counter, not derivable from IV |
| Already open position | Runtime state |
| Earnings imminent | Requires calendar lookup |
| Economic event window | Requires calendar lookup |
| Stop-hunt zone | Only here — fast_gate was deleted; pre_claude_filter no longer checks this |
| VWAP overextension | Now also in pre_claude_filter |
| Parabolic + bot herding | Fast IV checks — final safety before order |
| Gap-up skip | Cheap IV check — final safety before order |
| Stochastic >88 | Final overbought guard at execution |

#### 2.6 `executor.py` cleanup

- **Remove** sentiment hard block (line 85) — pre_claude_filter already handles it; no longer needed post-Claude.
- **RSI check** (line 66): use `RSI_BUY_MAX` (72) in the `rsi_max` parameter path for normal regime. `RSI_BUY_MAX_HIGH_VOL` (65) for HIGH_VOL.
- **Composite gate** (line 112): call `effective_composite_gate(iv, regime)` from `signals.entry_gates` — same function as pre_claude_filter uses.
- **VWAP slope check** (line 69): keep as belt-and-suspenders; uses same constant `VWAP_SLOPE_MIN` / `params["vwap_slope_min"]`.

---

### Part 3 — Gate Responsibility Summary (post-refactor)

```
pre_claude_filter       check_all_gates         executor
─────────────────       ───────────────         ────────
composite (unified) ──► (not checked)       ──► composite (same fn)
RSI max (unified)   ──► (removed dupes)     ──► RSI max (same const)
VWAP slope (new)    ──► (unchanged)         ──► VWAP slope (same)
VWAP overext (new)  ──► VWAP overext        ──► (via check_all_gates)
macro health        ──► (not here)          ──► (not here)
sentiment           ──► (not here)          ──► (removed dupe)
intraday news       ──► (not here)          ──► (not here)
                        time gate (only here)
                        per-cycle cap
                        earnings/events
                        stochastic >88
                        gap-up skip
                        parabolic
```

---

### Part 4 — Tests

Tests that need updating due to constant renames or threshold changes:

| Test | Change needed |
|---|---|
| Any test asserting `PRE_CLAUDE_COMPOSITE_MIN` | Update constant name to `COMPOSITE_MIN_NORMAL` |
| Any test asserting `HIGH_VOL_COMPOSITE_MIN` | Update constant name to `COMPOSITE_MIN_HIGH_VOL` |
| Any test asserting `RSI_BUY_MAX = 75` | Update to 72 |
| Any test asserting `RSI_HARD_SKIP_THRESHOLD` | Remove (deleted constant) |
| `test_fast_gate_*` tests | Delete (fast_gate is deleted) |
| Tests importing from `trading_agent` | Update to new canonical source |
| New tests needed | `effective_composite_gate()` with all 5 regime/signal combos |
| New tests needed | `pre_claude_filter` Layer 2.5 (VWAP slope + overextension blocks) |
| New tests needed | HIGH_VOL composite gate in executor via unified function |

---

### Part 5 — File Change Summary

| File | Change |
|---|---|
| `trading_agent.py` | **Delete** |
| `trading/cycle.py` | **Create** |
| `trading/monitor.py` | **Create** |
| `trading/watch_loader.py` | **Create** |
| `config.py` | Remove 2 constants, rename 2, change RSI_BUY_MAX value |
| `signals/entry_gates.py` | Delete fast_gate(), remove duplicate lines, add effective_composite_gate() |
| `trading/pre_claude_filter.py` | Update Layer 1+2+add Layer 2.5, import effective_composite_gate |
| `trading/executor.py` | Remove sentiment dupe, align RSI/composite constants, import effective_composite_gate |
| `scheduler_jobs.py` | Update 5 import paths |
| `main.py` | Update 2 import paths |
| `utils/startup.py` | Update 1 import path |
| `utils/market_monitor.py` | Update 1 import path |
| `utils/bot_health.py` | Update 1 import path |
| `trading/eod_holds.py` | Update 1 import path |
| `tests/*` | Update constant names, remove fast_gate tests, add 3 new test groups |
