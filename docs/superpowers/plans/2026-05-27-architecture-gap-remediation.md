# Architecture Gap Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close three confirmed architecture gaps — backtest CRASH_RECOVERY parity, regime×tier swing promotion gate, and FORCE_CLOSE entry-hour instrumentation.

**Architecture:** `SwingGateParams` frozen dataclass in `utils/regime_params.py` is the single source of truth for swing promotion composite+RSI thresholds; both live (`trading/eod_holds.py`) and backtest (`backtest/simulator.py`) call `get_swing_gate_params(regime, tier)`. The CRASH_RECOVERY fix is self-contained in `backtest/data.py` `_classify_vix_day()`. Entry-hour tagging writes one field at BUY time and copies it into every CLOSE record.

**Tech Stack:** Python 3.11, pytest, `dataclasses.dataclass(frozen=True)`, `utils.regime_params` pattern

**Spec:** `docs/superpowers/specs/2026-05-27-architecture-gap-remediation-design.md`

---

## File Map

| File | What changes |
|---|---|
| `config.py` | Add `CRASH_RECOVERY_MIN_GREEN_DAYS=2`; update NORMAL MEDIUM `SWING_HOLD_PARAMS` to `(7, 1.5, 1.2)`; delete `BACKTEST_SWING_COMPOSITE_MIN` |
| `utils/regime_params.py` | Add `SwingGateParams` dataclass + `_SWING_GATE_TABLE` (20 entries) + `get_swing_gate_params()` |
| `backtest/data.py` | Add `green_streak` counter inside `_classify_vix_day()`; add green_streak gate to `is_crash_recovery` |
| `trading/eod_holds.py` | `qualifies_for_eod_hold()`: add `entry_rsi` + `entry_composite` params + quality gate block; `evaluate_eod_holds()`: pass values from position record |
| `backtest/simulator.py` | `_evaluate_swing_promotion()`: add `precomp` param, use `get_swing_gate_params()`; `simulate_day()`: pass `precomp`; `Trade`: add `entry_hour`; `_close_trade()`: populate `entry_hour` |
| `trading/executor.py` | Add `"entry_hour": now.hour` to `position_data` dict |
| `trading/closer.py` | Add `"entry_hour": pos.get("entry_hour")` to both `force_close_all()` and `close_position()` journal records |
| `tests/test_config_coherence.py` | 3 new assertions: constant retired, table coverage, fallback value |
| `tests/test_foundation.py` | 4 new tests for `qualifies_for_eod_hold()` RSI+composite gate |
| `tests/test_pipeline.py` | 2 new tests for `_classify_vix_day()` consecutive green days gate |
| `tests/test_trading_lifecycle.py` | 3 new tests: entry_hour in force-close record, SL-close record, backtest Trade |

---

## Task 1: Config changes (foundation for all other tasks)

**Files:**
- Modify: `config.py`
- Modify: `tests/test_config_coherence.py`

- [ ] **Step 1: Write three failing tests in `tests/test_config_coherence.py`**

Add at the end of the file:

```python
@pytest.mark.unit
def test_backtest_swing_composite_min_retired():
    """BACKTEST_SWING_COMPOSITE_MIN must be removed — callers use get_swing_gate_params()."""
    assert not hasattr(config, "BACKTEST_SWING_COMPOSITE_MIN"), (
        "BACKTEST_SWING_COMPOSITE_MIN still exists — delete it from config.py"
    )


@pytest.mark.unit
def test_swing_hold_params_normal_medium_min_pnl():
    """NORMAL MEDIUM min_pnl_pct raised to 1.2% to filter marginal overnight positions."""
    _, _, min_pnl = config.SWING_HOLD_PARAMS[("NORMAL", "MEDIUM")]
    assert min_pnl == 1.2, f"Expected 1.2, got {min_pnl}"


@pytest.mark.unit
def test_crash_recovery_min_green_days_exists():
    """CRASH_RECOVERY_MIN_GREEN_DAYS must exist and be set to 2."""
    assert hasattr(config, "CRASH_RECOVERY_MIN_GREEN_DAYS")
    assert config.CRASH_RECOVERY_MIN_GREEN_DAYS == 2
```

- [ ] **Step 2: Run tests to confirm they all fail**

```bash
cd ~/Documents/projects/trading-bot
.venv/bin/python -m pytest tests/test_config_coherence.py::test_backtest_swing_composite_min_retired tests/test_config_coherence.py::test_swing_hold_params_normal_medium_min_pnl tests/test_config_coherence.py::test_crash_recovery_min_green_days_exists -v
```

Expected: all 3 FAIL.

- [ ] **Step 3: Apply config changes**

In `config.py`, find the `# ── Backtest Regime ──` section and add:

```python
CRASH_RECOVERY_MIN_GREEN_DAYS: int = 2
```

Find `BACKTEST_SWING_COMPOSITE_MIN` and **delete the entire line** (currently: `BACKTEST_SWING_COMPOSITE_MIN = 50.0`).

Find `SWING_HOLD_PARAMS` and update NORMAL MEDIUM from `(7, 1.5, 0.8)` to:

```python
    ("NORMAL",         "MEDIUM"):     (7,  1.5, 1.2),   # max_days=7, profit_lock=1.5%, min_pnl=1.2%
```

Also update the comment block above `SWING_HOLD_PARAMS` (around lines 287–289) that references the old value:

```python
    # min_pnl_pct raised for NORMAL MEDIUM (0.8→1.2) to avoid overnight risk on marginal positions.
```

- [ ] **Step 4: Run tests — expect all 3 to pass**

```bash
.venv/bin/python -m pytest tests/test_config_coherence.py::test_backtest_swing_composite_min_retired tests/test_config_coherence.py::test_swing_hold_params_normal_medium_min_pnl tests/test_config_coherence.py::test_crash_recovery_min_green_days_exists -v
```

Expected: 3 PASSED.

- [ ] **Step 5: Run full suite — confirm no regressions**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

If any test fails with `AttributeError: module 'config' has no attribute 'BACKTEST_SWING_COMPOSITE_MIN'` — that's the `simulator.py` local alias on line 36 (`SWING_COMPOSITE_MIN = config.BACKTEST_SWING_COMPOSITE_MIN`). Fix it now by deleting that line from `simulator.py`. The alias will be replaced by `get_swing_gate_params()` in Task 5.

- [ ] **Step 6: Commit**

```bash
git add config.py backtest/simulator.py tests/test_config_coherence.py
git commit -m "feat: retire BACKTEST_SWING_COMPOSITE_MIN, raise NORMAL MEDIUM min_pnl to 1.2%, add CRASH_RECOVERY_MIN_GREEN_DAYS"
```

---

## Task 2: `SwingGateParams` in `utils/regime_params.py`

**Files:**
- Modify: `utils/regime_params.py`
- Modify: `tests/test_config_coherence.py`

- [ ] **Step 1: Write three failing tests in `tests/test_config_coherence.py`**

```python
@pytest.mark.unit
def test_swing_gate_params_fallback_is_normal_medium():
    """Unknown regime/tier combo falls back to NORMAL MEDIUM values."""
    from utils.regime_params import get_swing_gate_params
    sgp = get_swing_gate_params("UNKNOWN_REGIME", "UNKNOWN_TIER")
    assert sgp.composite_min == 58
    assert sgp.rsi_ceiling == 70


@pytest.mark.unit
def test_swing_gate_table_covers_all_swing_eligible_regimes():
    """All swing-eligible regimes have entries for all 4 tiers."""
    from utils.regime_params import get_swing_gate_params
    swing_eligible = ["BULL_TREND", "NORMAL", "OPPORTUNITY", "CRASH_RECOVERY", "CAPITULATION"]
    tiers = ["CONVICTION", "HIGH", "MEDIUM", "LOW"]
    for regime in swing_eligible:
        for tier in tiers:
            sgp = get_swing_gate_params(regime, tier)
            assert sgp.composite_min > 0, f"{regime}/{tier} composite_min is 0"
            assert sgp.rsi_ceiling > 0, f"{regime}/{tier} rsi_ceiling is 0"


@pytest.mark.unit
def test_swing_gate_params_bull_trend_more_permissive_than_normal():
    """BULL_TREND thresholds are lower (more permissive) than NORMAL for the same tier."""
    from utils.regime_params import get_swing_gate_params
    for tier in ["CONVICTION", "HIGH", "MEDIUM", "LOW"]:
        bull = get_swing_gate_params("BULL_TREND", tier)
        normal = get_swing_gate_params("NORMAL", tier)
        assert bull.composite_min <= normal.composite_min, f"{tier}: BULL_TREND composite not <= NORMAL"
        assert bull.rsi_ceiling >= normal.rsi_ceiling, f"{tier}: BULL_TREND rsi_ceiling not >= NORMAL"
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
.venv/bin/python -m pytest tests/test_config_coherence.py::test_swing_gate_params_fallback_is_normal_medium tests/test_config_coherence.py::test_swing_gate_table_covers_all_swing_eligible_regimes tests/test_config_coherence.py::test_swing_gate_params_bull_trend_more_permissive_than_normal -v
```

Expected: 3 FAIL with `ImportError: cannot import name 'get_swing_gate_params'`.

- [ ] **Step 3: Add `SwingGateParams` to `utils/regime_params.py`**

Add the dataclass after the existing `ConcurrencyParams` dataclass (around line 294). Find `_CONCURRENCY_DISABLED` and insert before it:

```python
@dataclass(frozen=True)
class SwingGateParams:
    composite_min: int
    rsi_ceiling: int


_SWING_GATE_FALLBACK = SwingGateParams(composite_min=58, rsi_ceiling=70)

_SWING_GATE_TABLE: dict[tuple[str, str], SwingGateParams] = {
    ("BULL_TREND",     "CONVICTION"): SwingGateParams(50, 75),
    ("BULL_TREND",     "HIGH"):       SwingGateParams(53, 73),
    ("BULL_TREND",     "MEDIUM"):     SwingGateParams(55, 72),
    ("BULL_TREND",     "LOW"):        SwingGateParams(58, 69),
    ("NORMAL",         "CONVICTION"): SwingGateParams(52, 73),
    ("NORMAL",         "HIGH"):       SwingGateParams(55, 71),
    ("NORMAL",         "MEDIUM"):     SwingGateParams(58, 70),
    ("NORMAL",         "LOW"):        SwingGateParams(62, 67),
    ("OPPORTUNITY",    "CONVICTION"): SwingGateParams(50, 75),
    ("OPPORTUNITY",    "HIGH"):       SwingGateParams(53, 73),
    ("OPPORTUNITY",    "MEDIUM"):     SwingGateParams(56, 71),
    ("OPPORTUNITY",    "LOW"):        SwingGateParams(60, 68),
    ("CRASH_RECOVERY", "CONVICTION"): SwingGateParams(52, 70),
    ("CRASH_RECOVERY", "HIGH"):       SwingGateParams(55, 68),
    ("CRASH_RECOVERY", "MEDIUM"):     SwingGateParams(58, 66),
    ("CRASH_RECOVERY", "LOW"):        SwingGateParams(62, 63),
    ("CAPITULATION",   "CONVICTION"): SwingGateParams(54, 68),
    ("CAPITULATION",   "HIGH"):       SwingGateParams(57, 65),
    ("CAPITULATION",   "MEDIUM"):     SwingGateParams(60, 63),
    ("CAPITULATION",   "LOW"):        SwingGateParams(63, 60),
}


def get_swing_gate_params(regime: str, tier: str) -> SwingGateParams:
    return _SWING_GATE_TABLE.get((regime, tier), _SWING_GATE_FALLBACK)
```

- [ ] **Step 4: Run tests — expect all 3 to pass**

```bash
.venv/bin/python -m pytest tests/test_config_coherence.py::test_swing_gate_params_fallback_is_normal_medium tests/test_config_coherence.py::test_swing_gate_table_covers_all_swing_eligible_regimes tests/test_config_coherence.py::test_swing_gate_params_bull_trend_more_permissive_than_normal -v
```

Expected: 3 PASSED.

- [ ] **Step 5: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

Expected: no regressions.

- [ ] **Step 6: Commit**

```bash
git add utils/regime_params.py tests/test_config_coherence.py
git commit -m "feat: add SwingGateParams regime×tier table and get_swing_gate_params() lookup"
```

---

## Task 3: Backtest CRASH_RECOVERY regime parity

**Files:**
- Modify: `backtest/data.py`
- Modify: `tests/test_foundation.py`

- [ ] **Step 1: Write two failing tests in `tests/test_foundation.py`**

Add a new class at the end of the file:

```python
@pytest.mark.unit
class TestBacktestCrashRecoveryGate:
    """_classify_vix_day must require >= 2 consecutive green SPY days for CRASH_RECOVERY."""

    def _vix_crash_recovery(self):
        """VIX series that satisfies the VIX-only CRASH_RECOVERY criteria.
        vix_current=38, vix_prev=40 (not rising), peak_5d=44 → drop=13.6% >= 10%.
        vix_avg_90d ≈ 43 (all 90 historical bars at 44), so vix_current < avg → no HIGH_VOL fallback.
        """
        historical = [(i, 44.0) for i in range(1, 92)]   # 91 bars of VIX=44
        return historical + [(92, 40.0), (93, 38.0)]      # last two: prev=40, current=38

    def _spy_green_streak(self, n: int):
        """SPY series with exactly n consecutive green days ending at the last bar."""
        # Build a flat base then add n rising days
        base = [(i, 400.0) for i in range(1, 10)]
        red_break = [(10, 398.0)]                          # one red day to reset streak
        green_days = [(10 + j + 1, 399.0 + j * 0.5) for j in range(n)]
        return base + red_break + green_days

    def test_two_green_days_triggers_crash_recovery(self):
        from backtest.data import _classify_vix_day
        result = _classify_vix_day(self._vix_crash_recovery(), self._spy_green_streak(2))
        assert result == "CRASH_RECOVERY", f"Expected CRASH_RECOVERY with 2 green days, got {result}"

    def test_one_green_day_does_not_trigger_crash_recovery(self):
        from backtest.data import _classify_vix_day
        result = _classify_vix_day(self._vix_crash_recovery(), self._spy_green_streak(1))
        assert result != "CRASH_RECOVERY", f"CRASH_RECOVERY must not fire with only 1 green day, got {result}"
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
.venv/bin/python -m pytest tests/test_foundation.py::TestBacktestCrashRecoveryGate -v
```

Expected: `test_two_green_days_triggers_crash_recovery` PASS (VIX-only check already fires), `test_one_green_day_does_not_trigger_crash_recovery` FAIL — because the current code doesn't check green days yet.

- [ ] **Step 3: Add `green_streak` counter to `_classify_vix_day()` in `backtest/data.py`**

Find the `_classify_vix_day()` function (around line 104). After the `vix_drop_pct` calculation and before `is_crash_recovery`, insert:

```python
    # Count consecutive green SPY days (each close > prior close) ending at today
    green_streak = 0
    for i in range(len(past_spy) - 1, 0, -1):
        if past_spy[i][1] > past_spy[i - 1][1]:
            green_streak += 1
        else:
            break
```

Then update the `is_crash_recovery` assignment to add the green-streak gate:

```python
    is_crash_recovery = (
        vix_current >= config.BACKTEST_CRISIS_VIX_THRESHOLD
        and not vix_rising
        and vix_drop_pct >= config.BACKTEST_CRISIS_VIX_RECOVERY_DROP_PCT
        and green_streak >= config.CRASH_RECOVERY_MIN_GREEN_DAYS
    )
```

- [ ] **Step 4: Run tests — expect both to pass**

```bash
.venv/bin/python -m pytest tests/test_foundation.py::TestBacktestCrashRecoveryGate -v
```

Expected: 2 PASSED.

- [ ] **Step 5: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

Expected: no regressions.

- [ ] **Step 6: Commit**

```bash
git add backtest/data.py tests/test_foundation.py
git commit -m "feat: gate backtest CRASH_RECOVERY on consecutive_green_days >= 2 (live-bot parity)"
```

---

## Task 4: Live bot swing promotion gate (`trading/eod_holds.py`)

**Files:**
- Modify: `trading/eod_holds.py`
- Modify: `tests/test_foundation.py`

- [ ] **Step 1: Write four failing tests in `tests/test_foundation.py`**

Add a new class after `TestBacktestCrashRecoveryGate`:

```python
@pytest.mark.unit
class TestQualifiesForEodHoldGate:
    """qualifies_for_eod_hold must enforce regime×tier composite_min and rsi_ceiling."""

    def _call(self, **overrides):
        from types import SimpleNamespace
        from trading.eod_holds import qualifies_for_eod_hold
        defaults = dict(
            symbol="AAPL",
            regime="NORMAL",
            earnings_tomorrow=False,
            current_pnl_pct=2.0,        # above min_pnl (1.2%), below profit_lock
            entry_sector_sentiment=10.0,
            current_sector_sentiment=5.0,
            consecutive_red_days=0,
            vix_trend="stable",
            quality=SimpleNamespace(tier="MEDIUM"),
            entry_rsi=65.0,             # default: within NORMAL MEDIUM ceiling (70)
            entry_composite=60.0,       # default: above NORMAL MEDIUM min (58)
        )
        defaults.update(overrides)
        return qualifies_for_eod_hold(**defaults)

    def test_rsi_at_ceiling_blocks_promotion(self):
        ok, reason = self._call(entry_rsi=70.0)   # == ceiling, not <
        assert not ok
        assert "RSI" in reason

    def test_rsi_below_ceiling_allows_promotion(self):
        ok, _ = self._call(entry_rsi=69.0)
        assert ok

    def test_composite_below_min_blocks_promotion(self):
        ok, reason = self._call(entry_composite=57.0)   # < 58
        assert not ok
        assert "composite" in reason

    def test_profit_lock_bypass_ignores_rsi_and_composite(self):
        """Strongly profitable positions skip quality gate entirely."""
        ok, _ = self._call(
            current_pnl_pct=10.0,   # well above any profit_lock threshold
            entry_rsi=85.0,          # would fail RSI gate
            entry_composite=40.0,    # would fail composite gate
        )
        assert ok
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
.venv/bin/python -m pytest tests/test_foundation.py::TestQualifiesForEodHoldGate -v
```

Expected: all 4 FAIL (`TypeError: qualifies_for_eod_hold() got unexpected keyword argument 'entry_rsi'`).

- [ ] **Step 3: Update `qualifies_for_eod_hold()` signature in `trading/eod_holds.py`**

Add `entry_rsi` and `entry_composite` as keyword-only params with safe defaults (at the end so existing callers without them still work):

```python
def qualifies_for_eod_hold(
    symbol: str,
    regime: str,
    earnings_tomorrow: bool,
    current_pnl_pct: float,
    entry_sector_sentiment: float,
    current_sector_sentiment: float,
    consecutive_red_days: int,
    vix_trend: str,
    quality=None,
    regime_stale: bool = False,
    entry_rsi: float = 100.0,
    entry_composite: float = 0.0,
) -> tuple[bool, str]:
```

Add the import at the top of the function (or at module level):

```python
from utils.regime_params import get_swing_params, get_tp_sl_params, get_swing_gate_params
```

Insert the new gate block **after** the profit-lock bypass and **after** the `is_disabled / not eligible` block, **before** the `min_pnl_pct` check. Find the comment `# Min P&L gate:` and insert directly above it:

```python
    # Regime×tier quality gate: block late-momentum promotions
    sgp = get_swing_gate_params(regime, tier)
    if entry_rsi >= sgp.rsi_ceiling:
        return False, (
            f"RSI {entry_rsi:.0f} >= ceiling {sgp.rsi_ceiling} for {regime}/{tier} swing"
        )
    if entry_composite < sgp.composite_min:
        return False, (
            f"composite {entry_composite:.0f} < min {sgp.composite_min} for {regime}/{tier} swing"
        )
```

- [ ] **Step 4: Update `evaluate_eod_holds()` in `trading/eod_holds.py` to pass values**

Inside the `for symbol, pos in open_positions.items():` loop, find the `qualifies_for_eod_hold(...)` call and add the two new kwargs:

```python
            entry_indicators = pos.get("entry_indicators", {})
            qualifies, reason = qualifies_for_eod_hold(
                symbol=symbol,
                regime=regime,
                earnings_tomorrow=earnings_tomorrow,
                current_pnl_pct=pnl_pct,
                entry_sector_sentiment=entry_sector_score,
                current_sector_sentiment=current_sector_score,
                consecutive_red_days=regime_info.get("consecutive_red_days", 0),
                vix_trend=regime_info.get("vix_trend", "stable"),
                quality=quality_proxy,
                entry_rsi=float(entry_indicators.get("rsi", 100.0)),
                entry_composite=float(pos.get("entry_composite_score", 0.0)),
            )
```

- [ ] **Step 5: Run tests — expect all 4 to pass**

```bash
.venv/bin/python -m pytest tests/test_foundation.py::TestQualifiesForEodHoldGate -v
```

Expected: 4 PASSED.

- [ ] **Step 6: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

Expected: no regressions.

- [ ] **Step 7: Commit**

```bash
git add trading/eod_holds.py tests/test_foundation.py
git commit -m "feat: add regime×tier composite+RSI gate to qualifies_for_eod_hold() (live swing promotion)"
```

---

## Task 5: Backtest swing promotion gate (`backtest/simulator.py`)

**Files:**
- Modify: `backtest/simulator.py`
- Modify: `tests/test_foundation.py`

- [ ] **Step 1: Write two failing tests in `tests/test_foundation.py`**

Add a new class after `TestQualifiesForEodHoldGate`:

```python
@pytest.mark.unit
class TestBacktestSwingPromotion:
    """_evaluate_swing_promotion must enforce regime×tier gate via get_swing_gate_params()."""

    def _make_state(self, composite: float, rsi: float, tier: str = "MEDIUM") -> "SimState":
        from backtest.simulator import SimState
        state = SimState(cash=100_000.0)
        state.positions["AAPL"] = {
            "qty": 10,
            "entry": 100.0,
            "entry_ts": "2026-05-27T10:30:00+00:00",
            "composite": composite,
            "quality_tier": tier,
            "is_swing": False,
        }
        return state

    def _make_precomp(self, rsi: float, ts):
        """Minimal precomp dict with an IV-like object at the given timestamp."""
        from types import SimpleNamespace
        return {"AAPL": {ts: SimpleNamespace(rsi=rsi)}}

    def test_high_rsi_blocks_swing_promotion(self):
        """RSI at ceiling (70 for NORMAL MEDIUM) must block standard promotion."""
        import pandas as pd
        from zoneinfo import ZoneInfo
        from backtest.simulator import _evaluate_swing_promotion, SimState

        ts = pd.Timestamp("2026-05-27 19:50:00", tz="UTC")
        state = self._make_state(composite=62.0, rsi=70.0, tier="MEDIUM")
        # Provide a bar so eod_price can be computed
        df = pd.DataFrame({"close": [102.0]}, index=[ts])
        day_bars = {"AAPL": df}
        precomp = self._make_precomp(rsi=70.0, ts=ts)

        n = _evaluate_swing_promotion(state, day_bars, ts, "NORMAL", 0, precomp=precomp)
        assert n == 0, "AAPL should NOT be promoted: RSI at ceiling"
        assert not state.positions["AAPL"].get("is_swing")

    def test_low_rsi_high_composite_allows_swing_promotion(self):
        """RSI below ceiling and composite above min allows standard promotion."""
        import pandas as pd
        from backtest.simulator import _evaluate_swing_promotion

        ts = pd.Timestamp("2026-05-27 19:50:00", tz="UTC")
        # NORMAL MEDIUM: composite_min=58, rsi_ceiling=70; min_pnl=1.2%
        state = self._make_state(composite=62.0, rsi=65.0, tier="MEDIUM")
        state.positions["AAPL"]["entry"] = 100.0
        df = pd.DataFrame({"close": [102.0]}, index=[ts])   # pnl_pct=2.0% >= 1.2%
        day_bars = {"AAPL": df}
        precomp = self._make_precomp(rsi=65.0, ts=ts)

        n = _evaluate_swing_promotion(state, day_bars, ts, "NORMAL", 0, precomp=precomp)
        assert n == 1, "AAPL should be promoted: RSI and composite within bounds"
        assert state.positions["AAPL"].get("is_swing")
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
.venv/bin/python -m pytest tests/test_foundation.py::TestBacktestSwingPromotion -v
```

Expected: both FAIL (`TypeError: _evaluate_swing_promotion() got an unexpected keyword argument 'precomp'`).

- [ ] **Step 3: Update `_evaluate_swing_promotion()` in `backtest/simulator.py`**

First, add `get_swing_gate_params` to the import line at the top of simulator.py:

```python
from utils.regime_params import get_swing_params, get_tp_sl_params, get_entry_params, get_t2_params, get_filter_params, get_gate_params, get_swing_gate_params
```

Update the function signature to accept `precomp`:

```python
def _evaluate_swing_promotion(
    state: SimState, day_bars: dict, ts, regime: str, n_swings: int,
    quality_map: "dict[str, TickerQuality] | None" = None,
    precomp: "dict | None" = None,
) -> int:
```

Inside the loop, after `tier = pos2.get("quality_tier", "MEDIUM")`, add the gate lookup and replace `standard_qualify`:

```python
        sgp = get_swing_gate_params(regime, tier)
        iv_eod = (precomp or {}).get(sym2, {}).get(ts)
        eod_rsi = getattr(iv_eod, "rsi", 100.0) if iv_eod else 100.0

        profit_lock_bypass = (
            pnl_pct >= p.profit_lock_pct
            and regime != "CRISIS"
            and n_swings < SWING_MAX_POSITIONS
        )
        standard_qualify = (
            (sp.eligible or choppy_conviction_ok)
            and pnl_pct >= p.min_pnl_pct
            and pos2.get("composite", 0) >= sgp.composite_min
            and eod_rsi < sgp.rsi_ceiling
            and n_swings < SWING_MAX_POSITIONS
        )
```

- [ ] **Step 4: Update the call site in `simulate_day()` to pass `precomp`**

Find the line in `simulate_day()` (around line 473):

```python
            n_swings = _evaluate_swing_promotion(state, day_bars, ts, regime, n_swings)
```

Replace with:

```python
            n_swings = _evaluate_swing_promotion(state, day_bars, ts, regime, n_swings,
                                                  precomp=precomp)
```

- [ ] **Step 5: Run tests — expect both to pass**

```bash
.venv/bin/python -m pytest tests/test_foundation.py::TestBacktestSwingPromotion -v
```

Expected: 2 PASSED.

- [ ] **Step 6: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

Expected: no regressions.

- [ ] **Step 7: Commit**

```bash
git add backtest/simulator.py tests/test_foundation.py
git commit -m "feat: enforce regime×tier SwingGateParams in backtest swing promotion (composite+RSI gate)"
```

---

## Task 6: Entry-hour instrumentation

**Files:**
- Modify: `trading/executor.py`
- Modify: `trading/closer.py`
- Modify: `backtest/simulator.py`
- Modify: `tests/test_trading_lifecycle.py`

- [ ] **Step 1: Write three failing tests in `tests/test_trading_lifecycle.py`**

Add a new class after the existing `TestForceCloseAll` class:

```python
@pytest.mark.unit
class TestEntryHourTag:
    """entry_hour must be written at BUY time and propagated to every CLOSE record."""

    def test_force_close_record_contains_entry_hour(self, tmp_path, monkeypatch):
        """force_close_all() copies entry_hour from position into the CLOSE journal record."""
        import json
        from unittest.mock import MagicMock, patch
        from trading import closer
        import config

        positions = {
            "AAPL": {
                "entry_price": 100.0, "qty": 10, "entry_hour": 10,
                "entry_composite_score": 65.0, "entry_indicators": {},
                "entry_sentiment_score": 5, "claude_reasoning": "",
                "indicator_signals": {}, "mode_at_entry": "DAY_TRADE",
                "regime_at_entry": "NORMAL", "atr_pct": 1.5,
            }
        }
        monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "open_positions.json")
        monkeypatch.setattr(config, "TRADE_JOURNAL_FILE", tmp_path / "trade_journal.json")
        monkeypatch.setattr(config, "EOD_VERIFY_FILLS", False)
        monkeypatch.setattr(config, "SWING_POSITIONS_FILE", tmp_path / "swing_positions.json")
        (tmp_path / "open_positions.json").write_text(json.dumps(positions))
        (tmp_path / "trade_journal.json").write_text("[]")
        (tmp_path / "swing_positions.json").write_text("{}")

        mock_alpaca = MagicMock()
        mock_alpaca.get_latest_price.return_value = 102.0
        mock_alpaca.cancel_all_orders.return_value = None
        mock_alpaca.close_position.return_value = None
        mock_alpaca.get_positions.return_value = {}

        with patch("trading.closer.AlpacaClient", return_value=mock_alpaca):
            with patch("trading.closer.telegram_notify"):
                closer.force_close_all()

        journal = json.loads((tmp_path / "trade_journal.json").read_text())
        close_entries = [e for e in journal if e.get("type") == "CLOSE"]
        assert len(close_entries) == 1
        assert close_entries[0].get("entry_hour") == 10

    def test_sl_close_record_contains_entry_hour(self, tmp_path, monkeypatch):
        """close_position() copies entry_hour from position into the CLOSE journal record."""
        import json
        from unittest.mock import MagicMock, patch
        from trading import closer
        import config

        pos = {
            "entry_price": 100.0, "qty": 10, "entry_hour": 11,
            "entry_composite_score": 65.0, "entry_indicators": {},
            "entry_sentiment_score": 5, "claude_reasoning": "",
            "indicator_signals": {}, "mode_at_entry": "DAY_TRADE",
            "regime_at_entry": "NORMAL", "atr_pct": 1.5,
            "stop_order_id": None,
        }
        open_positions = {"AAPL": pos}
        monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "open_positions.json")
        monkeypatch.setattr(config, "TRADE_JOURNAL_FILE", tmp_path / "trade_journal.json")
        monkeypatch.setattr(config, "DAILY_CLOSED_FILE", tmp_path / "daily_closed.json")
        (tmp_path / "open_positions.json").write_text(json.dumps(open_positions))
        (tmp_path / "trade_journal.json").write_text("[]")
        (tmp_path / "daily_closed.json").write_text("[]")

        mock_alpaca = MagicMock()
        mock_alpaca.close_position.return_value = None

        with patch("trading.closer.telegram_notify"):
            closer.close_position(
                symbol="AAPL",
                alpaca=mock_alpaca,
                open_positions=open_positions,
                current_price=96.0,
                iv=None,
                reason="SL",
            )

        journal = json.loads((tmp_path / "trade_journal.json").read_text())
        close_entries = [e for e in journal if e.get("type") == "CLOSE"]
        assert len(close_entries) == 1
        assert close_entries[0].get("entry_hour") == 11

    def test_backtest_trade_entry_hour_populated(self):
        """Backtest Trade.entry_hour is derived from entry_ts and set correctly."""
        import pandas as pd
        from backtest.simulator import SimState, _open_trade, _close_trade
        from zoneinfo import ZoneInfo

        state = SimState(cash=50_000.0)
        # Simulate a position opened at 10:30 AM ET (UTC = 14:30)
        ts_open = "2026-05-27T14:30:00+00:00"
        state.positions["AAPL"] = {
            "qty": 10, "entry": 100.0, "sl": 96.0, "tp": 106.0,
            "entry_ts": ts_open, "entry_time": ts_open, "composite": 65.0,
            "quality_tier": "MEDIUM",
        }
        state.cash -= 100.0 * 10

        ts_close = pd.Timestamp("2026-05-27 19:55:00", tz="UTC")
        _close_trade(state, "AAPL", str(ts_close), 101.0, "FORCE_CLOSE", "2026-05-27")

        assert len(state.trades) == 1
        assert state.trades[0].entry_hour == 10   # 14:30 UTC = 10:30 ET
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
.venv/bin/python -m pytest tests/test_trading_lifecycle.py::TestEntryHourTag -v
```

Expected: all 3 FAIL.

- [ ] **Step 3: Add `entry_hour` to `position_data` in `trading/executor.py`**

Find the `position_data = {` dict (around line 302). After the `"entry_time": now.isoformat(),` line, add:

```python
        "entry_hour": now.hour,
```

- [ ] **Step 4: Add `entry_hour` to both CLOSE records in `trading/closer.py`**

In `force_close_all()`, find the `append_trade({` call (around line 209). After `"atr_pct": pos.get("atr_pct", 0.0),` add:

```python
            "entry_hour": pos.get("entry_hour"),
```

In `close_position()`, find the `append_trade({` call (around line 99). After `"atr_pct": pos.get("atr_pct", 0.0),` add:

```python
        "entry_hour": pos.get("entry_hour"),
```

- [ ] **Step 5: Add `entry_hour` to `Trade` dataclass and populate in `_close_trade()` in `backtest/simulator.py`**

Find the `Trade` dataclass (around line 78). Add a field with a default (keeps old callers working):

```python
@dataclass
class Trade:
    symbol: str
    date: str
    entry_ts: str
    exit_ts: str
    entry_price: float
    exit_price: float
    qty: float
    exit_reason: str
    pnl: float
    pnl_pct: float
    entry_hour: int = 0
```

In `_close_trade()`, find the `state.trades.append(Trade(...))` call (around line 171). Add `entry_hour` computation before the append and pass it in:

```python
    try:
        _entry_dt = datetime.fromisoformat(pos["entry_ts"]).astimezone(ET)
        _entry_hour = _entry_dt.hour
    except Exception:
        _entry_hour = 0
    state.trades.append(Trade(
        symbol=symbol, date=date_str, entry_ts=pos["entry_ts"], exit_ts=exit_ts,
        entry_price=pos["entry"], exit_price=ep, qty=close_qty,
        exit_reason=reason, pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 2),
        entry_hour=_entry_hour,
    ))
```

- [ ] **Step 6: Run tests — expect all 3 to pass**

```bash
.venv/bin/python -m pytest tests/test_trading_lifecycle.py::TestEntryHourTag -v
```

Expected: 3 PASSED.

- [ ] **Step 7: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

Expected: no regressions. Check test count is higher than before (≥ 1474 + 12 new tests).

- [ ] **Step 8: Commit**

```bash
git add trading/executor.py trading/closer.py backtest/simulator.py tests/test_trading_lifecycle.py
git commit -m "feat: add entry_hour tag to BUY position record and all CLOSE journal entries"
```

---

## Self-Review Checklist

| Spec requirement | Task |
|---|---|
| `CRASH_RECOVERY_MIN_GREEN_DAYS = 2` in config | Task 1 |
| `SWING_HOLD_PARAMS` NORMAL MEDIUM min_pnl 0.8→1.2% | Task 1 |
| `BACKTEST_SWING_COMPOSITE_MIN` deleted | Task 1 |
| `SwingGateParams` dataclass + 20-entry table | Task 2 |
| `get_swing_gate_params(regime, tier)` with NORMAL MEDIUM fallback | Task 2 |
| BULL_TREND more permissive than NORMAL | Task 2 (test asserts this) |
| `green_streak` counter inside `_classify_vix_day()` | Task 3 |
| CRASH_RECOVERY gated on `green_streak >= 2` | Task 3 |
| `qualifies_for_eod_hold()` gains `entry_rsi` + `entry_composite` params | Task 4 |
| Gate inserted after profit-lock bypass, before min_pnl | Task 4 |
| `evaluate_eod_holds()` passes `entry_indicators.rsi` + `entry_composite_score` | Task 4 |
| `_evaluate_swing_promotion()` gains `precomp` param, uses EOD RSI | Task 5 |
| `simulate_day()` passes `precomp` to `_evaluate_swing_promotion()` | Task 5 |
| `entry_hour` in `position_data` in executor | Task 6 |
| `entry_hour` in `force_close_all()` CLOSE record | Task 6 |
| `entry_hour` in `close_position()` CLOSE record | Task 6 |
| `Trade.entry_hour` in backtest | Task 6 |
| `trading/eod_holds.py` in files changed (not swing_manager.py) | Tasks 4 ✓ |

All spec requirements covered. No placeholders. Types and method names consistent across tasks.
