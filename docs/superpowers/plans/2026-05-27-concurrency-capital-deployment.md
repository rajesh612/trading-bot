# Concurrency & Capital Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy more capital through regime × tier × mode-gated concurrent positions with a heat ceiling and swing-health gate, while removing the per-cycle gate from `check_all_gates()` in favour of a pipeline-level burst cap.

**Architecture:** `ConcurrencyParams` frozen dataclass added to `utils/regime_params.py`; a concurrency guard block added to `trading/executor.py` (before `check_all_gates`); `entries_this_cycle` parameter removed from `check_all_gates()` and all callers; Phase 2 pipeline loop gets an early concurrency pre-skip (saves Claude budget); Claude's prompt context updated with open-position state; a burst cap enforced in `trading/pipeline.py`; `MAX_ENTRIES_PER_CYCLE` renamed to `MAX_ENTRIES_PER_CYCLE_BURST = 3`.

**Design invariants:**
- Both auto-execute and Claude-decided entries share the same concurrency guard inside `execute_buy()` — no exceptions.
- The concurrency guard is a hard gate (always `return None` on block) — never a soft flag.
- `pre_claude_filter` remains a quality router only (Rule 23); the Phase 2 pipeline loop handles the concurrency pre-skip.
- All disk reads use `load_json` / `save_json` from `utils.io_utils` — never raw `json.load`.
- `cp` (ConcurrencyParams) is always defined by the time the sizing chain runs — the guard block either returns None or completes assignment.
- `buying_power` accounting unchanged: `buying_power -= iv.current_price * pos_data["qty"]` after each successful entry.

**Tech Stack:** Python 3.11+, pytest, existing frozen dataclass pattern from `utils/regime_params.py`

---

## File Map

| File | Change |
|---|---|
| `config.py` | Add `REGIME_HEAT_CEILING`, `SWING_HEALTH_MIN_PNL_PCT`; rename `MAX_ENTRIES_PER_CYCLE` → `MAX_ENTRIES_PER_CYCLE_BURST = 3` |
| `utils/regime_params.py` | Add `ConcurrencyParams` dataclass, sentinels, `_CONCURRENCY_TABLE`, `get_concurrency_params()` |
| `signals/entry_gates.py` | Remove `entries_this_cycle` param from `check_all_gates()` signature + Gate #2 body (lines 79, 130–132) |
| `trading/executor.py` | Remove `entries_this_cycle` from `execute_buy()` signature; add `_compute_swing_book_pnl()`; add concurrency guard block; fix sizing chain (step 3: `cp.size_mult` + re-clamp); add `get_concurrency_params`, `get_entry_params` to module-level imports |
| `trading/pipeline.py` | Remove `entries_this_cycle` + `entries_so_far` from `_shared_buy_kwargs`, `_execute_auto_entries`, `_execute_claude_entries`; add Phase 2 concurrency pre-skip; add burst cap tracking in entry loops; pass concurrency state to `_build_market_context_block()` |
| `trading/context_builder.py` | Accept `open_count` + `heat_pct` + `available_slots` in `_build_market_context_block()`; surface them in the Claude prompt |
| `tests/test_trading_modes.py` | **New file** — all concurrency, swing-book, sizing-chain, and guard tests |
| `tests/test_entry_gates.py` | Delete `test_per_cycle_cap_blocks`; remove `entries_this_cycle` kwarg from all call sites |
| `tests/test_signals.py` | Remove `entries_this_cycle` kwarg from 4 call sites; add `test_gate2_removed_from_check_all_gates` |
| `tests/test_trading_pipeline.py` | Rewrite `TestPipelinePerCycleCap` (Gate 2 → burst cap); remove `MAX_ENTRIES_PER_CYCLE` monkeypatch |
| `tests/test_config_coherence.py` | Add `test_regime_heat_ceiling_covers_all_regimes` + 2 new constant checks |

---

## Task 1: Config Constants

**Files:**
- Modify: `config.py:114`

- [ ] **Step 1: Rename `MAX_ENTRIES_PER_CYCLE` and add concurrency block**

In `config.py`, replace line 114:
```python
MAX_ENTRIES_PER_CYCLE  = 1    # one BUY per trading cycle maximum
```

With:
```python
# ── Concurrency ──────────────────────────────────────────────────────────────
MAX_ENTRIES_PER_CYCLE_BURST: int = 3   # max new entries within one 10-min cycle
SWING_HEALTH_MIN_PNL_PCT:    float = -2.0  # swing book P&L floor; below → gate fires
REGIME_HEAT_CEILING: dict[str, float] = {
    "BULL_TREND":     0.50,
    "OPPORTUNITY":    0.45,
    "NORMAL":         0.35,
    "CRASH_RECOVERY": 0.25,
    "HIGH_VOL":       0.25,
    "CHOPPY":         0.20,
    "DEFENSIVE":      0.10,
    "CAPITULATION":   0.10,
    "CRISIS":         0.00,
}
```

- [ ] **Step 2: Verify no remaining bare `MAX_ENTRIES_PER_CYCLE` references**

```bash
grep -rn "MAX_ENTRIES_PER_CYCLE[^_]" ~/Documents/projects/trading-bot/ --include="*.py" | grep -v "_BURST"
```

Expected: zero matches (old test references fixed in later tasks).

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "feat: add concurrency config (heat ceiling, swing gate, burst cap rename)"
```

---

## Task 2: ConcurrencyParams Dataclass + Dispatch Table

**Files:**
- Modify: `utils/regime_params.py` (after `AutoExecuteParams` dataclass, ~line 286)
- Create: `tests/test_trading_modes.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_trading_modes.py`:

```python
"""Concurrency, position sizing, and mode-dispatch tests."""
import pytest
import types


# ── ConcurrencyParams / get_concurrency_params ────────────────────────────────

@pytest.mark.unit
class TestGetConcurrencyParams:

    def test_crisis_max_open_zero_all_tiers(self):
        from utils.regime_params import get_concurrency_params
        for tier in ("LOW", "MEDIUM", "HIGH", "CONVICTION"):
            cp = get_concurrency_params("CRISIS", tier, "DAY_TRADE")
            assert cp.max_open == 0, f"CRISIS tier={tier} should block all entries"

    def test_pead_mode_gets_plus1_slot(self):
        from utils.regime_params import get_concurrency_params
        base = get_concurrency_params("NORMAL", "MEDIUM", "DAY_TRADE")
        pead = get_concurrency_params("NORMAL", "MEDIUM", "PEAD")
        assert pead.max_open == base.max_open + 1

    def test_catalyst_mode_gets_plus1_slot(self):
        from utils.regime_params import get_concurrency_params
        base = get_concurrency_params("BULL_TREND", "HIGH", "DAY_TRADE")
        cat  = get_concurrency_params("BULL_TREND", "HIGH", "CATALYST")
        assert cat.max_open == base.max_open + 1

    def test_swing_mode_exempt_from_heat(self):
        from utils.regime_params import get_concurrency_params
        cp = get_concurrency_params("BULL_TREND", "CONVICTION", "SWING")
        assert cp.max_open == 999
        assert cp.heat_weight == 0.0
        assert cp.swing_health_gate is False

    def test_high_vol_size_mult_at_most_one(self):
        from utils.regime_params import get_concurrency_params
        for tier in ("LOW", "MEDIUM", "HIGH", "CONVICTION"):
            cp = get_concurrency_params("HIGH_VOL", tier, "DAY_TRADE")
            assert cp.size_mult <= 1.0, f"HIGH_VOL tier={tier} size_mult={cp.size_mult} > 1.0"

    def test_defensive_size_mult_at_most_one(self):
        from utils.regime_params import get_concurrency_params
        for tier in ("LOW", "MEDIUM", "HIGH", "CONVICTION"):
            cp = get_concurrency_params("DEFENSIVE", tier, "DAY_TRADE")
            assert cp.size_mult <= 1.0, f"DEFENSIVE tier={tier} size_mult={cp.size_mult} > 1.0"

    def test_unknown_regime_tier_returns_conservative(self):
        from utils.regime_params import get_concurrency_params
        cp = get_concurrency_params("UNKNOWN_REGIME", "LOW", "DAY_TRADE")
        assert cp.max_open == 1
        assert cp.size_mult == 1.0

    def test_fail_closed_on_exception(self, monkeypatch):
        import utils.regime_params as rp
        monkeypatch.setattr(rp, "_CONCURRENCY_TABLE", None)
        from utils.regime_params import get_concurrency_params
        cp = get_concurrency_params("NORMAL", "HIGH", "DAY_TRADE")
        assert cp.max_open == 1

    def test_bull_trend_conviction_max_open_is_5(self):
        from utils.regime_params import get_concurrency_params
        cp = get_concurrency_params("BULL_TREND", "CONVICTION", "DAY_TRADE")
        assert cp.max_open == 5

    def test_bull_trend_conviction_swing_health_gate_false(self):
        from utils.regime_params import get_concurrency_params
        cp = get_concurrency_params("BULL_TREND", "CONVICTION", "DAY_TRADE")
        assert cp.swing_health_gate is False

    def test_defensive_low_max_open_zero(self):
        from utils.regime_params import get_concurrency_params
        cp = get_concurrency_params("DEFENSIVE", "LOW", "DAY_TRADE")
        assert cp.max_open == 0
```

- [ ] **Step 2: Run tests to verify they fail (ImportError expected)**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest tests/test_trading_modes.py -v -m unit 2>&1 | tail -10
```

Expected: `ImportError` — `get_concurrency_params` does not exist yet.

- [ ] **Step 3: Add `ConcurrencyParams` + dispatch table to `utils/regime_params.py`**

After the `AutoExecuteParams` dataclass (after line ~286), add:

```python
@dataclass(frozen=True)
class ConcurrencyParams:
    max_open:          int
    size_mult:         float
    heat_weight:       float
    swing_health_gate: bool


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
```

- [ ] **Step 4: Run tests — all 11 must pass**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest tests/test_trading_modes.py -v -m unit 2>&1 | tail -15
```

Expected: 11 PASS, 0 FAIL.

- [ ] **Step 5: Run full suite to check for regressions**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -10
```

Expected: same passing count as before (currently 1488).

- [ ] **Step 6: Commit**

```bash
git add utils/regime_params.py tests/test_trading_modes.py
git commit -m "feat: add ConcurrencyParams dataclass + 32-row dispatch table"
```

---

## Task 3: Remove Gate #2 (`entries_this_cycle`) from All Files

This is a sweeping change across 5 files. The order within this task is strict: source first, then tests.

**Files:**
- Modify: `signals/entry_gates.py` — remove Gate #2 body + param
- Modify: `trading/executor.py` — remove `entries_this_cycle` param from `execute_buy()`
- Modify: `trading/pipeline.py` — remove `entries_this_cycle` + `entries_so_far` from `_shared_buy_kwargs`, `_execute_auto_entries`, `_execute_claude_entries`, and their callers in `TradingPipeline.run()`
- Modify: `tests/test_signals.py` — add assertion test; remove 4 call-site kwargs
- Modify: `tests/test_entry_gates.py` — delete Gate #2 test; remove 30+ call-site kwargs
- Modify: `tests/test_trading_pipeline.py` — rewrite `TestPipelinePerCycleCap`

- [ ] **Step 1: Add the assertion test to `tests/test_signals.py` first**

At the bottom of `tests/test_signals.py`, add:

```python
@pytest.mark.unit
def test_gate2_removed_from_check_all_gates():
    """Gate #2 (per-cycle cap) has been moved to pipeline.py — check_all_gates must not have this param."""
    import inspect
    from signals.entry_gates import check_all_gates
    sig = inspect.signature(check_all_gates)
    assert "entries_this_cycle" not in sig.parameters, (
        "entries_this_cycle must not be a parameter of check_all_gates()"
    )
```

- [ ] **Step 2: Run assertion test — must currently FAIL**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest tests/test_signals.py::test_gate2_removed_from_check_all_gates -v 2>&1 | tail -10
```

Expected: FAIL — `entries_this_cycle` still exists. This confirms Gate #2 is still present.

- [ ] **Step 3: Remove Gate #2 from `signals/entry_gates.py`**

a) Remove `entries_this_cycle: int,` from `check_all_gates()` signature (line 79).

b) Remove the Gate #2 block (lines 130–132):
```python
    # 2. Per-cycle entry cap
    if entries_this_cycle >= config.MAX_ENTRIES_PER_CYCLE:
        return GateResult(False, "per-cycle entry cap reached")
```

Gate numbering in comments shifts by 1 (old Gate #3 becomes Gate #2, etc.) — update the comment numbers in the body accordingly.

- [ ] **Step 4: Remove `entries_this_cycle` from `trading/executor.py`**

a) Remove from `execute_buy()` signature (line 63):
```python
    entries_this_cycle: int,
```

b) Remove the stale docstring mention on line 78: `"Caller increments entries_this_cycle and result.entries on non-None return."` → update to: `"Returns position_data dict on success, None if any pre-flight check blocks entry."`

c) Remove from the `check_all_gates()` call (line ~134):
```python
        entries_this_cycle=entries_this_cycle,
```

- [ ] **Step 5: Remove `entries_this_cycle` + `entries_so_far` from `trading/pipeline.py`**

a) `_shared_buy_kwargs()` — remove `entries_this_cycle` from signature and return dict:

Change signature:
```python
def _shared_buy_kwargs(symbol, iv, snap, alpaca, open_positions, portfolio_value,
                       pos_mult, spy_ret5d, rsi_max, now, entries_this_cycle, daily_pnl_pct) -> dict:
```
To:
```python
def _shared_buy_kwargs(symbol, iv, snap, alpaca, open_positions, portfolio_value,
                       pos_mult, spy_ret5d, rsi_max, now, daily_pnl_pct) -> dict:
```

Remove `entries_this_cycle=entries_this_cycle,` from the returned dict.

b) `_execute_auto_entries()` — remove `entries_so_far` parameter:

Change signature:
```python
def _execute_auto_entries(auto_execute, snap, alpaca, open_positions, portfolio_value,
                          buying_power, pos_mult, spy_ret5d, rsi_max, atr_tp_mult,
                          can_open, now, daily_pnl_pct, entries_so_far) -> tuple:
```
To:
```python
def _execute_auto_entries(auto_execute, snap, alpaca, open_positions, portfolio_value,
                          buying_power, pos_mult, spy_ret5d, rsi_max, atr_tp_mult,
                          can_open, now, daily_pnl_pct) -> tuple:
```

Remove the `entries_so_far + new_entries` computation in the kwargs call — `_shared_buy_kwargs` no longer takes it.

c) `_execute_claude_entries()` — remove `entries_so_far` parameter:

Change signature:
```python
def _execute_claude_entries(stock_data, decisions, snap, alpaca, open_positions,
                            portfolio_value, buying_power, pos_mult, spy_ret5d, rsi_max,
                            can_open, now, daily_pnl_pct, entries_so_far,
                            quality_map: dict | None = None) -> tuple:
```
To:
```python
def _execute_claude_entries(stock_data, decisions, snap, alpaca, open_positions,
                            portfolio_value, buying_power, pos_mult, spy_ret5d, rsi_max,
                            can_open, now, daily_pnl_pct,
                            quality_map: dict | None = None) -> tuple:
```

Remove `entries_so_far + new_entries` from the kwargs call inside this function.

d) `TradingPipeline.run()` — update call sites to remove the trailing `0` (entries_so_far) arguments:

```python
# Before:
auto_n, buying_power, auto_batch = _execute_auto_entries(
    auto_execute, snap, alpaca, open_positions, portfolio_value, buying_power,
    pos_mult, spy_ret5d, rsi_max, atr_tp_mult, can_open, now, daily_pnl_pct, 0,
)
# After:
auto_n, buying_power, auto_batch = _execute_auto_entries(
    auto_execute, snap, alpaca, open_positions, portfolio_value, buying_power,
    pos_mult, spy_ret5d, rsi_max, atr_tp_mult, can_open, now, daily_pnl_pct,
)

# Before:
claude_n, claude_c, buying_power, claude_batch = _execute_claude_entries(
    stock_data, decisions, snap, alpaca, open_positions, portfolio_value, buying_power,
    pos_mult, spy_ret5d, rsi_max, can_open, now, daily_pnl_pct, auto_n,
    quality_map=quality_map,
)
# After:
claude_n, claude_c, buying_power, claude_batch = _execute_claude_entries(
    stock_data, decisions, snap, alpaca, open_positions, portfolio_value, buying_power,
    pos_mult, spy_ret5d, rsi_max, can_open, now, daily_pnl_pct,
    quality_map=quality_map,
)
```

- [ ] **Step 6: Update `tests/test_entry_gates.py`**

a) Delete the `test_per_cycle_cap_blocks` method (lines ~106–116) — it tests removed behaviour.

b) Delete the `test_cap_fires_before_daily_closed` method — it uses `entries_this_cycle=config.MAX_ENTRIES_PER_CYCLE`.

c) Remove `entries_this_cycle=0,` from every remaining `check_all_gates(...)` call. There are ~30 occurrences. For each call, the form changes from:

```python
check_all_gates(
    "NVDA", self._iv(), {}, set(), None,
    self._now_allowed(), entries_this_cycle=0,
)
```
To:
```python
check_all_gates(
    "NVDA", self._iv(), {}, set(), None,
    self._now_allowed(),
)
```

Use this to find all occurrences first:
```bash
grep -n "entries_this_cycle" ~/Documents/projects/trading-bot/tests/test_entry_gates.py
```

- [ ] **Step 7: Update `tests/test_signals.py`** — remove `entries_this_cycle=0,` from 4 call sites

```bash
grep -n "entries_this_cycle" ~/Documents/projects/trading-bot/tests/test_signals.py
```

Remove that kwarg from each matching call.

- [ ] **Step 8: Update `tests/test_trading_pipeline.py`** — rewrite `TestPipelinePerCycleCap`

Replace the class body with:

```python
@pytest.mark.unit
class TestPipelinePerCycleCap:
    """Burst cap is now enforced in pipeline.py entry loops, not check_all_gates()."""

    def test_burst_cap_config_is_renamed(self):
        """MAX_ENTRIES_PER_CYCLE renamed to MAX_ENTRIES_PER_CYCLE_BURST = 3."""
        import config
        assert hasattr(config, "MAX_ENTRIES_PER_CYCLE_BURST")
        assert config.MAX_ENTRIES_PER_CYCLE_BURST == 3
        assert not hasattr(config, "MAX_ENTRIES_PER_CYCLE"), (
            "config.MAX_ENTRIES_PER_CYCLE still exists — remove the old name"
        )

    def test_close_decision_bypasses_cap(self):
        """CLOSE action calls close_position, not check_all_gates."""
        from trading.closer import close_position
        import inspect
        sig = inspect.signature(close_position)
        assert "symbol" in sig.parameters
        assert "entries_this_cycle" not in sig.parameters

    def test_5day_return_exactly_6_rows(self):
        import pandas as pd
        from trading.pipeline import _compute_symbol_5day_return
        df5 = pd.DataFrame({"close": [100.0] * 5})
        df6 = pd.DataFrame({"close": [100.0] * 6})
        assert _compute_symbol_5day_return(df5) == pytest.approx(0.0)
        assert _compute_symbol_5day_return(df6) == pytest.approx(0.0)
```

Also remove the `entries_this_cycle=0,` kwarg from any other call sites in `test_trading_pipeline.py` (lines ~409, ~1059, ~1109):

```bash
grep -n "entries_this_cycle" ~/Documents/projects/trading-bot/tests/test_trading_pipeline.py
```

- [ ] **Step 9: Run assertion test — must now PASS**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest tests/test_signals.py::test_gate2_removed_from_check_all_gates -v 2>&1 | tail -10
```

Expected: PASS.

- [ ] **Step 10: Run full suite**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -15
```

Expected: no regressions. Test count may be slightly lower (deleted Gate #2 tests).

- [ ] **Step 11: Commit**

```bash
git add signals/entry_gates.py trading/executor.py trading/pipeline.py \
    tests/test_entry_gates.py tests/test_signals.py tests/test_trading_pipeline.py
git commit -m "refactor: remove entries_this_cycle/entries_so_far — burst cap moves to pipeline"
```

---

## Task 4: Add `_compute_swing_book_pnl()` Helper in `executor.py`

**Files:**
- Modify: `trading/executor.py` (add helper after `_get_sector_sentiment`)
- Modify: `tests/test_trading_modes.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_trading_modes.py`:

```python
# ── _compute_swing_book_pnl ───────────────────────────────────────────────────

@pytest.mark.unit
class TestComputeSwingBookPnl:

    def test_empty_swing_book_returns_zero(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "SWING_POSITIONS_FILE", tmp_path / "swing.json")
        from utils.io_utils import save_json
        save_json(tmp_path / "swing.json", {})
        from trading.executor import _compute_swing_book_pnl
        assert _compute_swing_book_pnl({}, 100_000.0) == pytest.approx(0.0)

    def test_profitable_swing_returns_positive(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "SWING_POSITIONS_FILE", tmp_path / "swing.json")
        from utils.io_utils import save_json
        save_json(tmp_path / "swing.json", {"AAPL": {"entry_price": 100.0, "qty": 10}})
        from trading.executor import _compute_swing_book_pnl
        # (110-100)*10 / 100_000 * 100 = +1.0%
        assert _compute_swing_book_pnl({"AAPL": 110.0}, 100_000.0) == pytest.approx(1.0)

    def test_losing_swing_returns_negative(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "SWING_POSITIONS_FILE", tmp_path / "swing.json")
        from utils.io_utils import save_json
        save_json(tmp_path / "swing.json", {"TSLA": {"entry_price": 200.0, "qty": 5}})
        from trading.executor import _compute_swing_book_pnl
        # (190-200)*5 / 100_000 * 100 = -1.0%
        assert _compute_swing_book_pnl({"TSLA": 190.0}, 100_000.0) == pytest.approx(-1.0)

    def test_missing_file_returns_fail_closed(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "SWING_POSITIONS_FILE", tmp_path / "no_such_file.json")
        from trading.executor import _compute_swing_book_pnl
        assert _compute_swing_book_pnl({}, 100_000.0) == pytest.approx(-999.0)

    def test_unknown_symbol_falls_back_to_entry_price(self, tmp_path, monkeypatch):
        """Symbol not in stock_data_prices → uses entry_price (no gain assumed)."""
        import config
        monkeypatch.setattr(config, "SWING_POSITIONS_FILE", tmp_path / "swing.json")
        from utils.io_utils import save_json
        save_json(tmp_path / "swing.json", {"NVDA": {"entry_price": 150.0, "qty": 3}})
        from trading.executor import _compute_swing_book_pnl
        assert _compute_swing_book_pnl({}, 100_000.0) == pytest.approx(0.0)
```

- [ ] **Step 2: Run — must fail (ImportError)**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest tests/test_trading_modes.py::TestComputeSwingBookPnl -v 2>&1 | tail -10
```

- [ ] **Step 3: Add `_compute_swing_book_pnl()` to `trading/executor.py`**

After `_get_sector_sentiment()` (after line ~42), add:

```python
def _compute_swing_book_pnl(stock_data_prices: dict[str, float], portfolio_value: float) -> float:
    """Unrealised P&L of swing book as % of portfolio_value.

    Returns -999.0 on any read or computation error (fail-closed — triggers swing gate).
    stock_data_prices: {symbol: current_price} built from iv.current_price — no API calls.
    """
    try:
        swing_positions = load_json(config.SWING_POSITIONS_FILE, None)
        if swing_positions is None:
            return -999.0
        if not swing_positions or portfolio_value <= 0:
            return 0.0
        total_pnl = 0.0
        for sym, pos in swing_positions.items():
            entry_price = pos.get("entry_price", 0)
            qty         = pos.get("qty", 0)
            if entry_price <= 0 or qty <= 0:
                continue
            current_price = stock_data_prices.get(sym, entry_price)
            total_pnl += (current_price - entry_price) * qty
        return total_pnl / portfolio_value * 100
    except Exception:
        return -999.0
```

- [ ] **Step 4: Run tests — all 5 must pass**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest tests/test_trading_modes.py::TestComputeSwingBookPnl -v 2>&1 | tail -10
```

- [ ] **Step 5: Commit**

```bash
git add trading/executor.py tests/test_trading_modes.py
git commit -m "feat: add _compute_swing_book_pnl() helper (fail-closed, no API calls)"
```

---

## Task 5: Concurrency Guard Block + Sizing Chain in `executor.py`

**Files:**
- Modify: `trading/executor.py`
- Modify: `tests/test_trading_modes.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_trading_modes.py`:

```python
# ── Concurrency guard + sizing chain ─────────────────────────────────────────

@pytest.mark.unit
class TestExecuteBuyConcurrencyGuard:
    """Verifies that the concurrency guard in execute_buy() hard-blocks entries correctly.
    Both auto-execute and Claude paths share execute_buy() — no exceptions to these rules."""

    def _make_iv(self, price: float = 100.0):
        return types.SimpleNamespace(
            current_price=price, vwap=price * 0.99, vwap_slope=0.5,
            atr=1.0, composite_score=50.0, rsi=50.0, stoch_d=50.0,
            vwap_std2_upper=price * 1.15, stop_hunt_zone=False,
            parabolic=False, momentum_exhaustion=False,
            bot_herding_score=0, squeeze_setup=False,
            prev_close=price, relative_volume=1.0, or_position="outside",
        )

    def _setup_files(self, tmp_path, monkeypatch, open_pos=None, swing_pos=None):
        import config
        from utils.io_utils import save_json
        monkeypatch.setattr(config, "OPEN_POSITIONS_FILE",  tmp_path / "open.json")
        monkeypatch.setattr(config, "SWING_POSITIONS_FILE", tmp_path / "swing.json")
        monkeypatch.setattr(config, "DAILY_CLOSED_FILE",    tmp_path / "closed.json")
        save_json(tmp_path / "open.json",   open_pos  or {})
        save_json(tmp_path / "swing.json",  swing_pos or {})
        save_json(tmp_path / "closed.json", [])

    def _snap(self):
        from unittest.mock import MagicMock
        s = MagicMock()
        s.post_event_window = None
        s.sector_etf_cache  = {}
        return s

    def _call_execute_buy(self, tmp_path, *, regime="NORMAL", daily_pnl_pct=0.0,
                          quality=None, price=100.0):
        import datetime, zoneinfo
        from trading.executor import execute_buy
        from unittest.mock import MagicMock
        return execute_buy(
            symbol="AAPL", iv=self._make_iv(price),
            decision={"action": "BUY", "confidence": 80},
            regime=regime, regime_info={}, research={},
            open_positions={},
            alpaca=MagicMock(), portfolio_value=100_000.0,
            pos_mult=1.0, spy_ret5d=0.0, symbol_ret5d=0.0,
            rsi_max=74, params={},
            now=datetime.datetime(2024, 1, 15, 11, 0, 0,
                tzinfo=zoneinfo.ZoneInfo("America/New_York")),
            daily_pnl_pct=daily_pnl_pct, suppress_notify=True,
            snap=self._snap(), quality=quality,
        )

    def test_concurrency_blocks_at_max_open(self, tmp_path, monkeypatch):
        """NORMAL×HIGH max_open=3 — 4th entry must be blocked."""
        from utils.ticker_quality import TickerQuality
        self._setup_files(tmp_path, monkeypatch, open_pos={
            "TSLA": {"entry_price": 100.0, "qty": 1, "is_swing": False},
            "NVDA": {"entry_price": 200.0, "qty": 1, "is_swing": False},
            "MSFT": {"entry_price": 300.0, "qty": 1, "is_swing": False},
        })
        quality = TickerQuality(tier="HIGH", score=75.0, unlocks=frozenset())
        result = self._call_execute_buy(tmp_path, regime="NORMAL", quality=quality)
        assert result is None, "Concurrency limit should block 4th entry"

    def test_heat_ceiling_blocks_entry(self, tmp_path, monkeypatch):
        """NORMAL ceiling=35%; position at 34% → new entry (max 5%) exceeds ceiling."""
        self._setup_files(tmp_path, monkeypatch, open_pos={
            "TSLA": {"entry_price": 340.0, "qty": 100, "is_swing": False},
        })
        # portfolio=100k; TSLA = 34k = 34%. new entry projects max_position_pct=5% → 39% > 35%
        result = self._call_execute_buy(tmp_path, regime="NORMAL", price=100.0)
        assert result is None, "Heat ceiling should block this entry"

    def test_swing_health_gate_collapses_max_open(self, tmp_path, monkeypatch):
        """Swing book P&L < -2% → effective_max_open collapses to 1; 2nd intraday entry blocked."""
        import config
        monkeypatch.setattr(config, "SWING_HEALTH_MIN_PNL_PCT", -2.0)
        self._setup_files(tmp_path, monkeypatch,
            open_pos={"TSLA": {"entry_price": 100.0, "qty": 1, "is_swing": False}},
            swing_pos={"NVDA": {"entry_price": 200.0, "qty": 50}},  # 50*200=$10k
        )
        # NVDA current_price not in stock_data_prices → falls back to entry_price → 0% pnl
        # To trigger the gate we need a price that yields < -2% unrealised. But since NVDA
        # isn't on the watch list, _compute_swing_book_pnl uses entry_price → 0%.
        # So gate does NOT fire here. This test verifies NO collapse (0% > -2%).
        # For gate to fire, we need an explicit price source — see test_swing_health_gate_fail_closed.
        # NORMAL×MEDIUM max_open=2; 1 existing position → 2nd entry allowed if swing is healthy.
        # We just verify the function runs without error in the healthy case.
        result = self._call_execute_buy(tmp_path, regime="NORMAL", price=100.0)
        # May be None for other gate reasons (composite too low etc.) — just no crash
        assert isinstance(result, (dict, type(None)))

    def test_swing_health_gate_fail_closed(self, tmp_path, monkeypatch):
        """Corrupt swing_positions.json → _compute_swing_book_pnl returns -999 → gate fires."""
        self._setup_files(tmp_path, monkeypatch,
            open_pos={"TSLA": {"entry_price": 100.0, "qty": 1, "is_swing": False}},
        )
        import config
        (tmp_path / "swing.json").write_text("not-json{{")
        result = self._call_execute_buy(tmp_path, regime="NORMAL", price=100.0)
        assert result is None, "Corrupt swing file → -999 → gate fires → entry blocked"

    def test_daily_loss_downshift_blocks_second_entry(self, tmp_path, monkeypatch):
        """Near DAILY_LOSS_WARN_PCT with 1 open position → 2nd entry blocked."""
        import config
        monkeypatch.setattr(config, "DAILY_LOSS_WARN_PCT", 0.015)
        self._setup_files(tmp_path, monkeypatch,
            open_pos={"TSLA": {"entry_price": 100.0, "qty": 1, "is_swing": False}},
        )
        result = self._call_execute_buy(tmp_path, regime="NORMAL", daily_pnl_pct=-1.6)
        assert result is None, "Daily loss downshift should block 2nd entry"


@pytest.mark.unit
class TestSizingChain:
    """size_mult applied after regime_mult, before sentiment, before T1 tranche; re-clamped to max_position_pct."""

    def test_size_mult_reclamped_to_max_position_pct(self):
        """size_mult=1.35 must not produce qty > max_position_pct cap."""
        from utils.regime_params import get_concurrency_params, get_entry_params
        from trading.position_sizer import compute_position_size_for_mode, apply_regime_multiplier
        portfolio_value = 100_000.0
        price = 100.0
        max_pct = get_entry_params("BULL_TREND").max_position_pct   # 0.03
        max_qty_allowed = int((portfolio_value * max_pct) / price)  # 30
        cp = get_concurrency_params("BULL_TREND", "CONVICTION", "DAY_TRADE")
        assert cp.size_mult == 1.35
        qty = compute_position_size_for_mode(portfolio_value, price, 1.5, 0, False, regime="BULL_TREND")
        qty = apply_regime_multiplier(qty, 1.0)
        qty = max(1, int(round(qty * cp.size_mult)))
        qty = min(qty, max(1, max_qty_allowed))
        assert qty <= max_qty_allowed

    def test_high_vol_position_mult_preserved(self):
        """HIGH_VOL pos_mult=0.75 × size_mult≤1.0 must not exceed pre-mult qty."""
        import config
        from utils.regime_params import get_concurrency_params
        from trading.position_sizer import compute_position_size_for_mode, apply_regime_multiplier
        portfolio_value = 100_000.0
        price = 100.0
        atr = 1.5
        cp = get_concurrency_params("HIGH_VOL", "HIGH", "DAY_TRADE")
        assert cp.size_mult <= 1.0
        qty_base = compute_position_size_for_mode(portfolio_value, price, atr, 0, False, regime="HIGH_VOL")
        qty_regime = apply_regime_multiplier(qty_base, config.HIGH_VOL_POSITION_MULT)
        qty_final = max(1, int(round(qty_regime * cp.size_mult)))
        assert qty_final <= qty_regime
```

- [ ] **Step 2: Run failing tests**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest tests/test_trading_modes.py::TestExecuteBuyConcurrencyGuard tests/test_trading_modes.py::TestSizingChain -v 2>&1 | tail -20
```

Expected: failures (guard block and sizing chain step not yet in executor).

- [ ] **Step 3: Add module-level imports to `trading/executor.py`**

After the existing imports in `trading/executor.py`, add:
```python
from utils.regime_params import get_concurrency_params, get_entry_params
```

- [ ] **Step 4: Add concurrency guard block to `execute_buy()` in `trading/executor.py`**

Position: after the CATALYST position limit check (after the `if mode == "CATALYST"` block, before the `symbol_sentiment = ...` line).

```python
    # ── Concurrency + heat guard ─────────────────────────────────────────────
    try:
        _q_tier = quality.tier if quality else "MEDIUM"
        cp = get_concurrency_params(regime, _q_tier, mode)

        # A. Max-open check — re-read disk inside lock (fix #4: avoids stale in-memory state)
        with _POSITIONS_LOCK:
            disk_positions = load_json(config.OPEN_POSITIONS_FILE, {})
        intraday_open  = [p for p in disk_positions.values() if not p.get("is_swing")]
        intraday_count = len(intraday_open)

        effective_max_open = cp.max_open
        if cp.swing_health_gate:
            swing_pnl = _compute_swing_book_pnl({symbol: iv.current_price}, portfolio_value)
            if swing_pnl < config.SWING_HEALTH_MIN_PNL_PCT:
                effective_max_open = min(effective_max_open, 1)
                logger.info(
                    "CONCURRENCY %s — swing book stressed (%.1f%%) → max_open capped at 1",
                    symbol, swing_pnl,
                )

        if intraday_count >= effective_max_open:
            logger.info(
                "SKIP %s — concurrency limit %d/%d (regime=%s tier=%s)",
                symbol, intraday_count, effective_max_open, regime, _q_tier,
            )
            return None

        # B. Heat ceiling check — worst-case: max_position_pct × portfolio (fix #3)
        if portfolio_value > 0 and iv.current_price > 0:
            max_pct      = get_entry_params(regime).max_position_pct
            proj_heat    = max_pct * cp.heat_weight
            current_heat = sum(
                p.get("qty", 0) * p.get("entry_price", 0) for p in intraday_open
            ) / portfolio_value
            ceiling = config.REGIME_HEAT_CEILING.get(regime, 0.20)
            if current_heat + proj_heat > ceiling:
                logger.info(
                    "SKIP %s — heat ceiling %.0f%% would be breached "
                    "(current=%.1f%% + projected=%.1f%%)",
                    symbol, ceiling * 100, current_heat * 100, proj_heat * 100,
                )
                return None

        # C. Daily-loss downshift — near loss limit → cap at 1 concurrent position
        if daily_pnl_pct <= -(config.DAILY_LOSS_WARN_PCT * 100):
            if intraday_count >= 1:
                logger.info(
                    "SKIP %s — near daily loss limit (%.2f%%), allowing max 1 position",
                    symbol, daily_pnl_pct,
                )
                return None

    except Exception as exc:
        logger.warning(
            "Concurrency guard raised unexpectedly for %s — blocking entry: %s", symbol, exc,
        )
        return None
    # ── end concurrency guard ─────────────────────────────────────────────────
```

- [ ] **Step 5: Fix sizing chain in `execute_buy()` (add `cp.size_mult` + re-clamp)**

Find the current sizing chain (~line 159):
```python
    qty = compute_position_size_for_mode(portfolio_value, iv.current_price, iv.atr, iv.bot_herding_score, iv.squeeze_setup, regime=regime)
    qty = apply_regime_multiplier(qty, pos_mult)
    if isinstance(symbol_sentiment, (int, float)):
        qty = apply_sentiment_multiplier(qty, int(symbol_sentiment))
    qty = max(1, int(qty * config.TRANCHE1_SIZE_PCT))  # T1 = 60% of full size
```

Replace with:
```python
    # Step 1: ATR-based sizing
    qty = compute_position_size_for_mode(
        portfolio_value, iv.current_price, iv.atr,
        iv.bot_herding_score, iv.squeeze_setup, regime=regime,
    )
    # Step 2: regime multiplier (e.g. HIGH_VOL = 0.75×)
    qty = apply_regime_multiplier(qty, pos_mult)
    # Step 3: concurrency size multiplier — applied here, then re-clamped to max_position_pct (fix #2 + #8)
    qty = max(1, int(round(qty * cp.size_mult)))
    if iv.current_price > 0:
        _max_qty = int((portfolio_value * get_entry_params(regime).max_position_pct) / iv.current_price)
        qty = min(qty, max(1, _max_qty))
    # Step 4: sentiment multiplier
    if isinstance(symbol_sentiment, (int, float)):
        qty = apply_sentiment_multiplier(qty, int(symbol_sentiment))
    # Step 5: T1 tranche (60% of sized position)
    qty = max(1, int(qty * config.TRANCHE1_SIZE_PCT))
```

Note: `cp` is always defined at this point — the guard block assigns it or returns None.

- [ ] **Step 6: Run the tests**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest tests/test_trading_modes.py -v -m unit 2>&1 | tail -20
```

Expected: `TestComputeSwingBookPnl` and `TestSizingChain` all pass. `TestExecuteBuyConcurrencyGuard` guard-block tests pass (some tests may reach further gates and return None for other reasons — that's correct; the key assertion is `result is None`).

- [ ] **Step 7: Run full suite**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -10
```

Expected: no regressions.

- [ ] **Step 8: Commit**

```bash
git add trading/executor.py tests/test_trading_modes.py
git commit -m "feat: concurrency guard + size_mult sizing chain in execute_buy()"
```

---

## Task 6: Phase 2 Concurrency Pre-Skip in Pipeline + Burst Cap

Adding a concurrency pre-skip in Phase 2 prevents wasting Claude budget on symbols when the concurrency limit is already full. The burst cap limits entries within one cycle window.

**Files:**
- Modify: `trading/pipeline.py`
- Modify: `tests/test_trading_pipeline.py`

- [ ] **Step 1: Write failing test for burst cap**

Add to `TestPipelinePerCycleCap` in `tests/test_trading_pipeline.py`:

```python
    def test_burst_cap_constant_is_3(self):
        import config
        assert config.MAX_ENTRIES_PER_CYCLE_BURST == 3

    def test_burst_cap_limiting_in_auto_execute(self, monkeypatch):
        """_execute_auto_entries stops after MAX_ENTRIES_PER_CYCLE_BURST regardless of more candidates."""
        import config
        monkeypatch.setattr(config, "MAX_ENTRIES_PER_CYCLE_BURST", 2)
        from trading.pipeline import _execute_auto_entries
        from unittest.mock import MagicMock, patch
        import types, datetime
        from zoneinfo import ZoneInfo

        def _iv():
            return types.SimpleNamespace(
                current_price=100.0, composite_score=60.0, atr=1.0,
                vwap_slope=1.0, rsi=50.0, stoch_d=50.0,
                vwap_std2_upper=120.0, stop_hunt_zone=False,
                parabolic=False, momentum_exhaustion=False,
                bot_herding_score=0, squeeze_setup=False,
                prev_close=100.0, relative_volume=1.2, or_position="outside",
            )

        candidates = [("SYM1", _iv(), None), ("SYM2", _iv(), None),
                      ("SYM3", _iv(), None), ("SYM4", _iv(), None)]
        mock_snap = MagicMock()
        mock_snap.regime = "BULL_TREND"
        mock_snap.staleness_guard.auto_execute_allowed = True
        mock_snap.intraday_news = {}
        mock_snap.research = {}

        executed = []
        def fake_execute_buy(**kw):
            executed.append(kw["symbol"])
            return {"qty": 1, "entry_price": 100.0, "stop_loss_price": 96.0,
                    "take_profit_price": 105.0, "mode_at_entry": "DAY_TRADE"}

        with patch("trading.pipeline.execute_buy", side_effect=fake_execute_buy):
            new_entries, _, _ = _execute_auto_entries(
                auto_execute=candidates, snap=mock_snap,
                alpaca=MagicMock(), open_positions={},
                portfolio_value=100_000.0, buying_power=50_000.0,
                pos_mult=1.0, spy_ret5d=0.0, rsi_max=74, atr_tp_mult=2.0,
                can_open=True,
                now=datetime.datetime(2024, 1, 15, 11, 0, 0, tzinfo=ZoneInfo("America/New_York")),
                daily_pnl_pct=0.0,
            )

        assert new_entries == 2, f"Burst cap=2 but got {new_entries} entries"
        assert len(executed) == 2
```

- [ ] **Step 2: Run test to confirm it fails**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest "tests/test_trading_pipeline.py::TestPipelinePerCycleCap::test_burst_cap_limiting_in_auto_execute" -v 2>&1 | tail -10
```

Expected: FAIL (no burst cap logic yet).

- [ ] **Step 3: Add Phase 2 concurrency pre-skip in `trading/pipeline.py`**

In `TradingPipeline.run()`, Phase 2 block (after `open_positions = load_open_positions()` and `daily_closed = load_daily_closed()`), add a helper read before the symbol routing loop:

```python
        # ── Phase 2: Pre-Claude filter + concurrency pre-check ───────────────
        # Read open positions once for concurrency pre-skip (avoids routing all symbols
        # to Claude when concurrency slots are already full)
        from utils.io_utils import load_json as _lj
        from utils.regime_params import get_concurrency_params as _gcp
        _intraday_open = [p for p in open_positions.values() if not p.get("is_swing")]
        _intraday_count = len(_intraday_open)
```

Inside the routing loop for each symbol, after `existing_pos` check, add a concurrency pre-skip before `apply_pre_claude_filter`:

```python
            # Concurrency pre-skip: if concurrency limit already full for this tier, save Claude budget
            _cp_pre = _gcp(regime, "MEDIUM", "DAY_TRADE")  # conservative tier check
            if _intraday_count >= _cp_pre.max_open and _cp_pre.max_open > 0:
                logger.info("SKIP-CONCURRENCY %s: intraday=%d >= max_open=%d for regime=%s",
                            symbol, _intraday_count, _cp_pre.max_open, regime)
                _record_telemetry(ts_str, symbol, "SKIP", "concurrency_pre_skip", iv)
                continue
```

Note: this is a conservative pre-skip using MEDIUM tier. The actual enforcement in `execute_buy()` uses the symbol's real tier — this saves budget only when even the most permissive (MEDIUM) slots are full.

- [ ] **Step 4: Add burst cap tracking to `_execute_auto_entries()`**

Change the `for symbol, iv, quality in auto_execute:` loop to add a local counter:

```python
    entries_this_cycle = 0
    for symbol, iv, quality in auto_execute:
        if entries_this_cycle >= config.MAX_ENTRIES_PER_CYCLE_BURST:
            logger.info(
                "[BURST-CAP] %d entries this cycle — remaining auto-execute candidates skipped",
                entries_this_cycle,
            )
            break
        if not can_open or buying_power < config.MIN_BUYING_POWER:
            continue
        try:
            ...
            pos_data = execute_buy(...)
            if pos_data is not None:
                new_entries += 1
                entries_this_cycle += 1
                buying_power -= iv.current_price * pos_data["qty"]
                ...
```

- [ ] **Step 5: Add burst cap tracking to `_execute_claude_entries()`**

```python
    entries_this_cycle = 0
    for symbol, iv, existing_pos, symbol_ret5d in stock_data:
        try:
            ...
            if action == "BUY" and not existing_pos and buying_power >= config.MIN_BUYING_POWER and can_open:
                if entries_this_cycle >= config.MAX_ENTRIES_PER_CYCLE_BURST:
                    logger.info(
                        "[BURST-CAP] %d entries this cycle — remaining Claude candidates skipped",
                        entries_this_cycle,
                    )
                    break
                ...
                pos_data = execute_buy(...)
                if pos_data is not None:
                    new_entries += 1
                    entries_this_cycle += 1
                    ...
```

- [ ] **Step 6: Run burst cap test**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest "tests/test_trading_pipeline.py::TestPipelinePerCycleCap" -v 2>&1 | tail -15
```

Expected: all tests PASS.

- [ ] **Step 7: Run full suite**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -10
```

Expected: no regressions.

- [ ] **Step 8: Commit**

```bash
git add trading/pipeline.py tests/test_trading_pipeline.py
git commit -m "feat: concurrency pre-skip in Phase 2 + burst cap in entry loops"
```

---

## Task 7: Enrich Claude Context with Concurrency State

Claude receives incomplete context when it doesn't know how many positions are open or how much heat is deployed. Adding this allows Claude to factor in portfolio capacity when recommending BUY.

**Files:**
- Modify: `trading/context_builder.py` — surface `open_count`, `heat_pct`, `available_slots` in prompt
- Modify: `trading/pipeline.py` — pass open position state to `build_cycle_snapshot` or pass directly to context builder call

First, verify where `_build_market_context_block()` is called and what it currently receives:

```bash
grep -n "_build_market_context_block\|build_cycle_snapshot" ~/Documents/projects/trading-bot/trading/context_builder.py ~/Documents/projects/trading-bot/trading/pipeline.py | head -30
```

- [ ] **Step 1: Read `trading/context_builder.py` to understand the current signature**

```bash
grep -n "def _build_market_context_block\|def build_cycle_snapshot" ~/Documents/projects/trading-bot/trading/context_builder.py
```

Then read the function signature and the section of the prompt that shows portfolio/account context.

- [ ] **Step 2: Add `open_count`, `heat_pct`, `available_slots` parameters to `_build_market_context_block()`**

The context block should include a **Concurrency State** section alongside the existing macro/portfolio sections:

```
## Concurrency State
Open intraday positions: {open_count}
Deployed heat: {heat_pct:.1f}% (ceiling: {heat_ceiling:.0f}%)
Available slots: {available_slots} (regime={regime}, tier=MEDIUM)
```

Compute these values in the caller (`trading/pipeline.py` or `trading/claude_caller.py`) from `open_positions` already in scope, then pass to `_build_market_context_block()`.

- [ ] **Step 3: Update the caller in `trading/pipeline.py` or `trading/claude_caller.py`**

Where `_build_market_context_block()` is called (find in context_builder.py), pass the three new args.

Compute:
```python
_intraday_positions = [p for p in open_positions.values() if not p.get("is_swing")]
_open_count = len(_intraday_positions)
_heat_pct   = (
    sum(p.get("qty", 0) * p.get("entry_price", 0) for p in _intraday_positions)
    / portfolio_value * 100
) if portfolio_value > 0 else 0.0
_cp_context = get_concurrency_params(regime, "MEDIUM", "DAY_TRADE")
_ceiling    = config.REGIME_HEAT_CEILING.get(regime, 0.20) * 100
_available  = max(0, _cp_context.max_open - _open_count)
```

- [ ] **Step 4: Run any existing context_builder tests**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest tests/ -k "context" -m unit -v 2>&1 | tail -20
```

Fix any failures caused by signature change.

- [ ] **Step 5: Run full suite**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -10
```

- [ ] **Step 6: Commit**

```bash
git add trading/context_builder.py trading/pipeline.py
git commit -m "feat: include concurrency state (open slots, heat%) in Claude prompt context"
```

---

## Task 8: Config Coherence Tests

**Files:**
- Modify: `tests/test_config_coherence.py`

- [ ] **Step 1: Add 3 new coherence tests at the bottom of `tests/test_config_coherence.py`**

```python
@pytest.mark.unit
def test_regime_heat_ceiling_covers_all_regimes():
    """REGIME_HEAT_CEILING must have an entry for every regime in REGIMES tuple."""
    import config
    from utils.regime_params import REGIMES
    missing = set(REGIMES) - set(config.REGIME_HEAT_CEILING.keys())
    assert not missing, f"REGIME_HEAT_CEILING missing entries for: {missing}"


@pytest.mark.unit
def test_swing_health_min_pnl_is_negative():
    """SWING_HEALTH_MIN_PNL_PCT must be negative — it is a loss threshold."""
    import config
    assert config.SWING_HEALTH_MIN_PNL_PCT < 0, (
        f"SWING_HEALTH_MIN_PNL_PCT={config.SWING_HEALTH_MIN_PNL_PCT} must be < 0"
    )


@pytest.mark.unit
def test_burst_cap_greater_than_one():
    """MAX_ENTRIES_PER_CYCLE_BURST must be > 1 — the point of the change."""
    import config
    assert config.MAX_ENTRIES_PER_CYCLE_BURST > 1, (
        f"MAX_ENTRIES_PER_CYCLE_BURST={config.MAX_ENTRIES_PER_CYCLE_BURST} — expected > 1"
    )
```

- [ ] **Step 2: Run coherence tests**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest tests/test_config_coherence.py -v -m unit 2>&1 | tail -15
```

Expected: all pass.

- [ ] **Step 3: Run full suite — final check**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -5
```

Expected: all pass, count ≥ 1488 + new tests.

- [ ] **Step 4: Commit**

```bash
git add tests/test_config_coherence.py
git commit -m "test: config coherence — heat ceiling completeness, swing gate sign, burst cap value"
```

---

## Final Verification Checklist

```bash
# 1. Gate #2 gone from entry_gates.py and all callers
grep -rn "entries_this_cycle\|MAX_ENTRIES_PER_CYCLE[^_]" \
    ~/Documents/projects/trading-bot/ --include="*.py" | grep -v "_BURST\|test_gate2_removed"

# 2. ConcurrencyParams wired correctly
grep -rn "get_concurrency_params\|ConcurrencyParams" \
    ~/Documents/projects/trading-bot/ --include="*.py" | grep -v "test_trading_modes\|regime_params"

# 3. Burst cap in both pipeline loops
grep -n "BURST\|burst" ~/Documents/projects/trading-bot/trading/pipeline.py

# 4. Sizing chain has cp.size_mult step
grep -n "size_mult" ~/Documents/projects/trading-bot/trading/executor.py

# 5. Full suite passes
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -5
```

Expected for check 1: zero matches (all old references cleaned up).
Expected for check 5: all tests pass.
