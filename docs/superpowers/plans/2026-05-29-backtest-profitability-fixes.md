# Backtest Profitability Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 6 identified gaps between backtest analysis and live trading that suppress profitability: HIGH_VOL entry gate too loose, swing gate composite floors too high, NORMAL/MEDIUM swing promotion P&L threshold too high, backtest position sizing mismatch vs live, ineffective learned `vwap_slope_min`, and narrow param_sweep search space.

**Architecture:** Changes are spread across three layers — (1) regime parameter tables (`utils/regime_params.py`, `config.py`) for gate/swing tuning, (2) backtest simulator (`backtest/simulator.py`) for sizing alignment, (3) learning agent (`learning/trade_analysis.py`, `learning/agent.py`) to remove a dead parameter, and (4) param_sweep script expansion. Each task is independently testable.

**Tech Stack:** Python, pytest (`@pytest.mark.unit`), `unittest.mock.patch` (sweep overrides), `monkeypatch` (tests), `utils/regime_params.py` frozen-dataclass table pattern.

---

## File Map

| File | What Changes |
|------|-------------|
| `utils/regime_params.py` | `_SWING_GATE_TABLE` composite floors (Task 1); `_day_trade_table()` HIGH_VOL rows (Task 2) |
| `config.py` | `SWING_HOLD_PARAMS` NORMAL/MEDIUM min_pnl_pct (Task 3) |
| `backtest/simulator.py` | `_calc_qty` — use ATR not STOP_LOSS_PCT (Task 4) |
| `learning/trade_analysis.py` | Remove `vwap_slope_min` from `_ZERO_TRADE_BOUNDS` (Task 5) |
| `learning/agent.py` | Remove `vwap_slope_min` from `param_keys` (Task 5) |
| `scripts/param_sweep.py` | Expand `SWEEP_AXES` + two new override factories (Task 6) |
| `tests/test_config_coherence.py` | Tests for Tasks 1, 2, 3 |
| `tests/test_backtest_simulator.py` | Test for Task 4 |
| `tests/test_learning.py` | Test for Task 5 |

---

## Task 1: Expand Swing Gate Composite Floors

**Why:** Swing trades are 82–95% WR across all windows vs ~50% for day trades. The NORMAL/MEDIUM composite floor of 65 and BULL_TREND/MEDIUM floor of 55 block positions that could be profitable overnight. Lowering by 5 points opens the gate to more swing candidates without touching the RSI ceiling (unchanged).

**Files:**
- Modify: `utils/regime_params.py` — `_SWING_GATE_TABLE`
- Test: `tests/test_config_coherence.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config_coherence.py` (after existing swing gate tests, ~line 268):

```python
@pytest.mark.unit
def test_swing_gate_normal_medium_composite_floor_at_most_60():
    """NORMAL/MEDIUM swing gate composite_min must be ≤ 60 (expanded to capture more swing candidates)."""
    from utils.regime_params import get_swing_gate_params
    sgp = get_swing_gate_params("NORMAL", "MEDIUM")
    assert sgp.composite_min <= 60, (
        f"NORMAL/MEDIUM swing gate composite_min={sgp.composite_min} > 60 — "
        "swing gate is too restrictive; lower it to allow more overnight holds"
    )


@pytest.mark.unit
def test_swing_gate_bull_trend_medium_composite_floor_at_most_50():
    """BULL_TREND/MEDIUM swing gate composite_min must be ≤ 50."""
    from utils.regime_params import get_swing_gate_params
    sgp = get_swing_gate_params("BULL_TREND", "MEDIUM")
    assert sgp.composite_min <= 50, (
        f"BULL_TREND/MEDIUM swing gate composite_min={sgp.composite_min} > 50"
    )
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/rajeshkumar/Documents/projects/trading-bot
.venv/bin/python -m pytest tests/test_config_coherence.py::test_swing_gate_normal_medium_composite_floor_at_most_60 tests/test_config_coherence.py::test_swing_gate_bull_trend_medium_composite_floor_at_most_50 -v
```

Expected: FAIL (current NORMAL/MEDIUM=65, BULL_TREND/MEDIUM=55).

- [ ] **Step 3: Update the swing gate table**

In `utils/regime_params.py`, find `_SWING_GATE_TABLE` (around line 305) and change:

```python
# OLD:
("BULL_TREND",     "MEDIUM"):     SwingGateParams(55, 72),
# ...
("NORMAL",         "MEDIUM"):     SwingGateParams(65, 70),

# NEW:
("BULL_TREND",     "MEDIUM"):     SwingGateParams(50, 72),
# ...
("NORMAL",         "MEDIUM"):     SwingGateParams(60, 70),
```

- [ ] **Step 4: Run all swing gate tests**

```bash
.venv/bin/python -m pytest tests/test_config_coherence.py -k "swing_gate" -v
```

Expected: all PASS. The existing `test_swing_gate_params_bull_trend_more_permissive_than_normal` must still pass (50 ≤ 60 ✓) and `test_entry_late_composite_min_at_least_swing_gate_medium` must still pass (ENTRY_LATE_COMPOSITE_MIN=68 ≥ 60 ✓).

- [ ] **Step 5: Run full unit suite to check for regressions**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

Expected: all pass, no regressions.

- [ ] **Step 6: Commit**

```bash
git add utils/regime_params.py tests/test_config_coherence.py
git commit -m "perf: lower swing gate composite floors (NORMAL/MEDIUM 65→60, BULL_TREND/MEDIUM 55→50)

Swing trades are 82–95% WR across all backtest windows. Lowering composite
floors by 5 points expands overnight hold candidates without touching RSI ceilings.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 2: Tighten HIGH_VOL Entry Gates

**Why:** The 100d backtest shows Jan–Feb 2026 (HIGH_VOL/CRISIS period) generated ≈-$2,886 in losses across 16 trading days. HIGH_VOL/MEDIUM currently allows entries until noon (hard_close_hour=12) with late_composite=72. Tightening to hard_close_hour=11 and late_composite=78 reduces afternoon exposure and raises the quality bar.

**Files:**
- Modify: `utils/regime_params.py` — `_day_trade_table()` HIGH_VOL rows
- Test: `tests/test_config_coherence.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config_coherence.py`:

```python
@pytest.mark.unit
def test_high_vol_medium_entry_gates_stricter_than_normal():
    """HIGH_VOL/MEDIUM must have a stricter late_composite and earlier hard_close than NORMAL/MEDIUM.
    HIGH_VOL Jan–Feb 2026 generated ≈-$2.9k in losses — tighter gates reduce this drag."""
    from utils.regime_params import get_day_trade_params
    hv = get_day_trade_params("HIGH_VOL", "MEDIUM")
    nm = get_day_trade_params("NORMAL", "MEDIUM")
    assert hv.late_composite > nm.late_composite, (
        f"HIGH_VOL/MEDIUM late_composite={hv.late_composite} must be > NORMAL/MEDIUM={nm.late_composite}"
    )
    assert hv.hard_close_hour <= nm.hard_close_hour, (
        f"HIGH_VOL/MEDIUM hard_close_hour={hv.hard_close_hour} must be <= NORMAL/MEDIUM={nm.hard_close_hour}"
    )
    assert hv.late_composite >= 78, (
        f"HIGH_VOL/MEDIUM late_composite={hv.late_composite} < 78"
    )
    assert hv.hard_close_hour <= 11, (
        f"HIGH_VOL/MEDIUM hard_close_hour={hv.hard_close_hour} > 11"
    )
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python -m pytest tests/test_config_coherence.py::test_high_vol_medium_entry_gates_stricter_than_normal -v
```

Expected: FAIL (current HIGH_VOL/MEDIUM: hard_close=12, late_composite=72; NORMAL/MEDIUM: hard_close=12, late_composite=68 — same hard_close, and 72 is not ≥ 78).

- [ ] **Step 3: Update HIGH_VOL DayTradeParams rows**

In `utils/regime_params.py`, find `_day_trade_table()` HIGH_VOL section and change:

```python
# OLD:
("HIGH_VOL", "CONVICTION"): dt(hard_close_hour=12, late_entry_hour=11, late_composite=65, vwap_slope_min=0.05),
("HIGH_VOL", "HIGH"):       dt(hard_close_hour=12, late_entry_hour=11, late_composite=68, vwap_slope_min=0.06),
("HIGH_VOL", "MEDIUM"):     dt(hard_close_hour=12, late_entry_hour=11, late_composite=72, vwap_slope_min=0.08),
("HIGH_VOL", "LOW"):        dt(hard_close_hour=11, late_entry_hour=10, late_composite=80, vwap_slope_min=0.15),

# NEW:
("HIGH_VOL", "CONVICTION"): dt(hard_close_hour=12, late_entry_hour=11, late_composite=70, vwap_slope_min=0.05),
("HIGH_VOL", "HIGH"):       dt(hard_close_hour=11, late_entry_hour=11, late_composite=75, vwap_slope_min=0.06),
("HIGH_VOL", "MEDIUM"):     dt(hard_close_hour=11, late_entry_hour=11, late_composite=78, vwap_slope_min=0.08),
("HIGH_VOL", "LOW"):        dt(hard_close_hour=11, late_entry_hour=10, late_composite=85, vwap_slope_min=0.15),
```

- [ ] **Step 4: Run the new test and full suite**

```bash
.venv/bin/python -m pytest tests/test_config_coherence.py::test_high_vol_medium_entry_gates_stricter_than_normal -v
.venv/bin/python -m pytest tests/ -m unit -q
```

Expected: new test PASS, no regressions.

- [ ] **Step 5: Commit**

```bash
git add utils/regime_params.py tests/test_config_coherence.py
git commit -m "perf: tighten HIGH_VOL entry gates to reduce Jan–Feb style losses

HIGH_VOL/MEDIUM: hard_close 12→11, late_composite 72→78.
HIGH_VOL/HIGH: hard_close 12→11, late_composite 68→75.
HIGH_VOL/CONVICTION: late_composite 65→70.
HIGH_VOL/LOW: late_composite 80→85.

Jan–Feb 2026 HIGH_VOL period generated ≈-\$2.9k across 16 trading days.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 3: Lower NORMAL/MEDIUM Swing Promotion Min P&L

**Why:** `SWING_HOLD_PARAMS[("NORMAL", "MEDIUM")]` has `min_pnl_pct=1.2` — a position needs +1.2% gain to qualify for standard overnight promotion. This blocks positions with 0.5–1.1% gains that have 88%+ WR as swings (per 30d data: DDOG +0.60%, MDB +0.89%, SNOW +0.96% all promoted successfully as BULL_TREND swings where min_pnl=0.5%). Lowering NORMAL/MEDIUM to 0.5% aligns with BULL_TREND/MEDIUM.

**Files:**
- Modify: `config.py` — `SWING_HOLD_PARAMS`
- Test: `tests/test_config_coherence.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config_coherence.py`:

```python
@pytest.mark.unit
def test_normal_medium_swing_promote_min_pnl_at_most_0_5():
    """NORMAL/MEDIUM swing promotion min_pnl_pct must be ≤ 0.5%.
    BULL_TREND/MEDIUM is 0.5%; NORMAL shouldn't be more than 2× stricter given 88%+ swing WR."""
    assert ("NORMAL", "MEDIUM") in config.SWING_HOLD_PARAMS, \
        "SWING_HOLD_PARAMS missing ('NORMAL', 'MEDIUM') key"
    _max_days, _profit_lock, min_pnl = config.SWING_HOLD_PARAMS[("NORMAL", "MEDIUM")]
    assert min_pnl <= 0.5, (
        f"NORMAL/MEDIUM swing min_pnl_pct={min_pnl} > 0.5 — "
        "too restrictive; 88%+ swing WR justifies promoting lower-gain positions"
    )
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python -m pytest tests/test_config_coherence.py::test_normal_medium_swing_promote_min_pnl_at_most_0_5 -v
```

Expected: FAIL (current value is 1.2).

- [ ] **Step 3: Update SWING_HOLD_PARAMS**

In `config.py`, find `SWING_HOLD_PARAMS` (around line 326) and change the NORMAL/MEDIUM row:

```python
# OLD:
("NORMAL",         "MEDIUM"):     (7,  1.5, 1.2),

# NEW:
("NORMAL",         "MEDIUM"):     (7,  1.5, 0.5),
```

- [ ] **Step 4: Run the test and full suite**

```bash
.venv/bin/python -m pytest tests/test_config_coherence.py::test_normal_medium_swing_promote_min_pnl_at_most_0_5 -v
.venv/bin/python -m pytest tests/ -m unit -q
```

Expected: new test PASS, no regressions.

- [ ] **Step 5: Commit**

```bash
git add config.py tests/test_config_coherence.py
git commit -m "perf: lower NORMAL/MEDIUM swing promotion min P&L from 1.2% to 0.5%

Aligns NORMAL with BULL_TREND/MEDIUM threshold. Backtest shows 88%+ swing WR
even at sub-1% entry P&L. Prior 1.2% was overly conservative given the data.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 4: Fix Backtest Position Sizing to Match Live (Use ATR Not STOP_LOSS_PCT)

**Why:** `backtest/simulator.py:_calc_qty` sizes positions using `sl_dollars = price * STOP_LOSS_PCT / 100` (3.0% fixed). The live `trading/position_sizer.py:compute_position_size_for_mode` uses `risk / ATR` directly. These diverge whenever ATR differs from `price * 3%`. For NORMAL/MEDIUM the actual applied SL is clamped to 2.0% (regime_params sl_ceiling), so backtest positions are also 33% undersized relative to the actual risk taken. Fix: use ATR directly, matching live logic.

**Files:**
- Modify: `backtest/simulator.py` — `_calc_qty`
- Test: `tests/test_backtest_simulator.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_backtest_simulator.py` (in the existing class or as a standalone test):

```python
@pytest.mark.unit
def test_calc_qty_uses_atr_not_stop_loss_pct():
    """_calc_qty must size on ATR (matching live position_sizer), not STOP_LOSS_PCT.
    
    With price=$100, ATR=$2, portfolio=$100_000, RISK_PER_TRADE=0.5%:
      - ATR-based: risk=$500, shares=$500/$2=250 → capped at 5% = 50 shares
    With STOP_LOSS_PCT=3.0%:
      - sl_dollars=$3.00, shares=$500/$3=167 → capped at 50 shares
    
    We test that the result is identical whether we provide a 'typical' ATR or
    force it to match STOP_LOSS_PCT, since the cap is usually binding. More
    importantly, when ATR << price*3%, the ATR path gives MORE shares (uncapped path).
    """
    import config
    from backtest.simulator import _calc_qty

    # Case: ATR is small (e.g. $0.50 on $20 stock) — ATR path gives far more shares before cap
    # risk=$500, ATR-based=$500/$0.50=1000 shares, STOP_LOSS_PCT-based=$500/$0.60=833 shares
    # Both get capped by 5% of $100k / $20 = 250 shares
    # So cap is binding for cheap stocks too — verify they agree at the cap
    portfolio = 100_000.0
    price = 20.0
    atr = 0.50
    qty = _calc_qty(portfolio, price, atr)
    cap = round(portfolio * config.MAX_POSITION_PCT / price)
    assert qty == cap, f"Expected qty={cap} (cap-bound), got {qty}"

    # Case: high-priced stock where ATR matters for uncapped path
    # price=$500, ATR=$5 (1%), risk=$500, ATR-based=$500/$5=100, cap=5%*100k/$500=10
    # STOP_LOSS_PCT-based: $500/$15=33 → cap 10.  Both cap at 10 — verify.
    qty2 = _calc_qty(portfolio, 500.0, 5.0)
    cap2 = round(portfolio * config.MAX_POSITION_PCT / 500.0)
    assert qty2 == cap2, f"Expected qty={cap2} (cap-bound at $500), got {qty2}"

    # Case: ATR=0 → falls back to ATR_FALLBACK_PROXY_PCT (not STOP_LOSS_PCT)
    qty3 = _calc_qty(portfolio, price, 0.0)
    assert qty3 >= 1, "qty must be at least 1 when ATR=0"
```

- [ ] **Step 2: Run test to verify it passes already or understand current behavior**

```bash
.venv/bin/python -m pytest tests/test_backtest_simulator.py::test_calc_qty_uses_atr_not_stop_loss_pct -v
```

Note: this test validates cap-bound behavior which both old and new code satisfy. Its value is as a regression guard after the change. It will likely PASS already. That's expected — the real behavioral change is on the uncapped path.

- [ ] **Step 3: Write a test that specifically catches the old vs new behavior**

Add another test to `tests/test_backtest_simulator.py`:

```python
@pytest.mark.unit
def test_calc_qty_uncapped_uses_atr_not_stop_loss_pct():
    """When the position cap is not binding, _calc_qty must use ATR for sizing.

    If ATR=$1 and STOP_LOSS_PCT=3.0% on a $10 stock:
      - ATR-based: risk=$50 / $1 = 50 shares
      - STOP_LOSS_PCT-based: risk=$50 / ($10*0.03) = $50/$0.30 = 167 shares
    
    Cap = 5% * $10k / $10 = 50 shares.  Both paths are capped here.
    Use a tiny portfolio so cap is non-binding:
      portfolio=$1000, price=$10, ATR=$1, RISK=0.5%=$5
      ATR-based: $5/$1 = 5 shares, cap = 5%*$1000/$10 = 5 shares → both agree at 5
      But with STOP_LOSS_PCT=3%: $5/$0.30 = 17 shares, then capped at 5
    
    To isolate: use a large cap (big portfolio, cheap stock, large max_pct) so cap never binds.
    portfolio=$1000, price=$1, ATR=$0.10, RISK=0.5%=$5, cap=5%*$1000/$1=50 shares
      ATR: $5/$0.10 = 50 shares  (coincidentally equals cap)
    Use ATR=$0.20: $5/$0.20 = 25 shares < cap(50) → uncapped
      STOP_LOSS_PCT: $5/($1*0.03) = $5/$0.03 = 167 shares → capped at 50
    """
    import config
    from unittest.mock import patch
    from backtest.simulator import _calc_qty

    portfolio = 1_000.0
    price = 1.0
    atr = 0.20   # ATR-based: 1000*0.005/0.20 = 25 shares; SL-based: 1000*0.005/(1*0.03) = 167 → cap 50

    with patch.object(config, "MAX_POSITION_PCT", 0.05):
        qty = _calc_qty(portfolio, price, atr)

    # After the fix, result should be ATR-based: 25 shares (uncapped; cap=50)
    # Before the fix: capped at 50 shares (SL-based 167 → capped)
    assert qty == 25, (
        f"Expected 25 (ATR-based uncapped), got {qty}. "
        "If this fails, _calc_qty still uses STOP_LOSS_PCT instead of ATR."
    )
```

- [ ] **Step 4: Run to verify it FAILS with current code**

```bash
.venv/bin/python -m pytest tests/test_backtest_simulator.py::test_calc_qty_uncapped_uses_atr_not_stop_loss_pct -v
```

Expected: FAIL (current code uses STOP_LOSS_PCT → result is 50, not 25).

- [ ] **Step 5: Fix _calc_qty in simulator.py**

In `backtest/simulator.py`, find `_calc_qty` (around line 108) and change:

```python
# OLD:
def _calc_qty(portfolio_value: float, price: float, atr: float) -> float:
    if price <= 0:
        return 0.0
    sl_dollars = price * STOP_LOSS_PCT / 100 or (atr if atr > 0 else price * config.ATR_FALLBACK_PROXY_PCT)
    qty_risk = portfolio_value * RISK_PER_TRADE / sl_dollars
    return max(1.0, round(min(qty_risk, portfolio_value * MAX_POSITION_PCT / price)))

# NEW:
def _calc_qty(portfolio_value: float, price: float, atr: float) -> float:
    if price <= 0:
        return 0.0
    # Match live position_sizer.py: size on ATR (actual intraday risk), not a fixed SL%.
    risk_unit = atr if atr > 0 else price * config.ATR_FALLBACK_PROXY_PCT
    qty_risk = portfolio_value * RISK_PER_TRADE / risk_unit
    return max(1.0, round(min(qty_risk, portfolio_value * MAX_POSITION_PCT / price)))
```

- [ ] **Step 6: Run both tests**

```bash
.venv/bin/python -m pytest tests/test_backtest_simulator.py::test_calc_qty_uses_atr_not_stop_loss_pct tests/test_backtest_simulator.py::test_calc_qty_uncapped_uses_atr_not_stop_loss_pct -v
```

Expected: both PASS.

- [ ] **Step 7: Run full unit suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add backtest/simulator.py tests/test_backtest_simulator.py
git commit -m "fix: backtest _calc_qty uses ATR (matches live position_sizer)

Was using price*STOP_LOSS_PCT/100 (3%) as the risk unit, while live code
uses ATR directly. This caused backtest positions to be undersized by ~33%
for NORMAL/MEDIUM where actual SL is 2% not 3%, and diverged from live
behavior whenever ATR != price*3%.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 5: Remove Ineffective vwap_slope_min from Learning Agent

**Why:** The learned `vwap_slope_min` (currently -0.2 in trading_params.json) has no effect in NORMAL/MEDIUM or any regime where `DayTradeParams.vwap_slope_min` is stricter (0.05 for NORMAL/MEDIUM, 0.08 for HIGH_VOL). The `DayTradeParams` gate always fires before the global config gate. In the backtest, the module-level binding `VWAP_SLOPE_MIN = config.VWAP_SLOPE_MIN` is set at import time and ignores post-import overrides entirely. The learning agent expends Claude tokens updating a parameter that has no behavioral effect.

**Files:**
- Modify: `learning/trade_analysis.py` — remove from `_ZERO_TRADE_BOUNDS`
- Modify: `learning/agent.py` — remove from `param_keys`
- Test: `tests/test_learning.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_learning.py`:

```python
@pytest.mark.unit
def test_learning_agent_does_not_update_vwap_slope_min():
    """vwap_slope_min must not appear in the learning agent param_keys list.
    
    DayTradeParams.vwap_slope_min (per regime×tier, e.g. 0.05 for NORMAL/MEDIUM)
    is always stricter than config.VWAP_SLOPE_MIN, so the global config value is
    never the binding constraint. Learning agent updating it wastes Claude tokens.
    """
    import learning.agent as agent_mod
    import inspect, ast

    src = inspect.getsource(agent_mod.run_learning_agent)
    tree = ast.parse(src)
    # Find assignment `param_keys = [...]`
    param_keys_values = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "param_keys":
                    if isinstance(node.value, ast.List):
                        param_keys_values = [
                            elt.s for elt in node.value.elts
                            if isinstance(elt, ast.Constant) and isinstance(elt.s, str)
                        ]
    assert "vwap_slope_min" not in param_keys_values, (
        "vwap_slope_min is in param_keys — remove it; "
        "DayTradeParams table is the authoritative vwap gate and can't be overridden via learned params"
    )


@pytest.mark.unit
def test_zero_trade_day_does_not_relax_vwap_slope_min():
    """_ZERO_TRADE_BOUNDS must not include vwap_slope_min.
    
    Zero-trade-day relaxation of vwap_slope_min has no effect because
    DayTradeParams.vwap_slope_min (0.05 for NORMAL/MEDIUM) is always stricter.
    """
    from learning.trade_analysis import _ZERO_TRADE_BOUNDS
    assert "vwap_slope_min" not in _ZERO_TRADE_BOUNDS, (
        "vwap_slope_min is in _ZERO_TRADE_BOUNDS — remove it; "
        "relaxing the global config value has no effect when DayTradeParams gate is stricter"
    )
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_learning.py::test_learning_agent_does_not_update_vwap_slope_min tests/test_learning.py::test_zero_trade_day_does_not_relax_vwap_slope_min -v
```

Expected: FAIL (both currently include vwap_slope_min).

- [ ] **Step 3: Remove vwap_slope_min from _ZERO_TRADE_BOUNDS**

In `learning/trade_analysis.py`, find `_ZERO_TRADE_BOUNDS` (around line 170) and remove the entry:

```python
# OLD:
_ZERO_TRADE_BOUNDS: dict[str, tuple] = {
    "rsi_buy_max":          (65, 80),
    "composite_min_normal": (15, 30),
    "vwap_slope_min":       (-0.6, -0.1),
}

# NEW:
_ZERO_TRADE_BOUNDS: dict[str, tuple] = {
    "rsi_buy_max":          (65, 80),
    "composite_min_normal": (15, 30),
}
```

- [ ] **Step 4: Remove vwap_slope_min from param_keys in learning/agent.py**

In `learning/agent.py`, find `param_keys` (around line 247) and remove `"vwap_slope_min"`:

```python
# OLD:
param_keys = [
    "entry_start_hour", "entry_end_hour", "rsi_buy_max", "rsi_buy_max_high_vol",
    "vwap_slope_min", "high_vol_position_mult", "stop_loss_pct", "take_profit_pct",
    "choppy_rsi_max",
]

# NEW:
param_keys = [
    "entry_start_hour", "entry_end_hour", "rsi_buy_max", "rsi_buy_max_high_vol",
    "high_vol_position_mult", "stop_loss_pct", "take_profit_pct",
    "choppy_rsi_max",
]
```

- [ ] **Step 5: Also remove vwap_slope_min from the zero-trade-day handler in agent.py**

In `learning/agent.py`, the `_handle_zero_trade_day` function has a special case for `vwap_slope_min`:

```python
# OLD (around line 65–67):
    if target == "vwap_slope_min":
        new_val = max(low, current - 0.05)
    else:
        new_val = min(high, current + config.LEARNING_ZERO_TRADE_RSI_STEP)

# NEW (remove the special case entirely — vwap_slope_min is no longer in _ZERO_TRADE_BOUNDS):
    new_val = min(high, current + config.LEARNING_ZERO_TRADE_RSI_STEP)
```

- [ ] **Step 6: Run the new tests and full suite**

```bash
.venv/bin/python -m pytest tests/test_learning.py::test_learning_agent_does_not_update_vwap_slope_min tests/test_learning.py::test_zero_trade_day_does_not_relax_vwap_slope_min -v
.venv/bin/python -m pytest tests/ -m unit -q
```

Expected: both new tests PASS, no regressions. The existing test at test_learning.py:556 (`"vwap_slope_min": -0.3`) is a fixture that only sets up params — it doesn't assert the learning agent must update it, so no change needed there.

- [ ] **Step 7: Commit**

```bash
git add learning/trade_analysis.py learning/agent.py tests/test_learning.py
git commit -m "fix: remove vwap_slope_min from learning agent outputs

DayTradeParams.vwap_slope_min (0.05 NORMAL/MEDIUM, 0.08 HIGH_VOL) is always
the binding VWAP gate. config.VWAP_SLOPE_MIN is never checked first in any
live regime. In backtests the module-level binding means learned values never
take effect at all. Removing from param_keys and _ZERO_TRADE_BOUNDS stops
wasting Claude tokens on a no-op parameter.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 6: Expand param_sweep SWEEP_AXES

**Why:** The current sweep covers only 3 axes × 3 values = 27 combos. Missing: ATR-TP multiplier (currently 2.5×, untested across range), midday profit floor PCT (1.0%, the #2 profit driver, never swept), and BULL_TREND/MEDIUM late composite (68, untested looser at 60/65). The `atr_tp_multiplier` axis injects through the existing `SimState` path. The midday floor and BULL_TREND late composite need new patches in `_run_combo`. The `_day_trade_override` factory is also replaced by a merged wrapper that handles both NORMAL/MEDIUM and BULL_TREND/MEDIUM in one patch.

**Files:**
- Modify: `scripts/param_sweep.py`

- [ ] **Step 1: Expand SWEEP_AXES**

In `scripts/param_sweep.py`, replace the `SWEEP_AXES` dict (around line 52) and raise `MAX_COMBOS`:

```python
SWEEP_AXES: dict[str, list[Any]] = {
    # SL ceiling for NORMAL/MEDIUM/DAY_TRADE
    "normal_med_sl_ceiling": [1.5, 1.8, 2.0],

    # NORMAL/MEDIUM DayTradeParams: hour after which composite floor activates
    "normal_med_late_hour": [10, 11, 12],

    # NORMAL/MEDIUM DayTradeParams: composite floor required after late_hour
    "normal_med_late_composite": [65, 68, 72],

    # ATR-TP multiplier — injected via SimState (no patch needed)
    "atr_tp_multiplier": [2.0, 2.5, 3.0],

    # Midday profit floor: minimum unrealised % to trigger early exit 2–3 PM
    "midday_profit_floor_pct": [0.75, 1.0, 1.25],

    # BULL_TREND/MEDIUM late composite bar
    "bull_trend_med_late_composite": [60, 65, 68],
}

MAX_COMBOS = 200  # raised from 150; 6-axis 3^6=729 grid needs more coverage
```

- [ ] **Step 2: Replace _day_trade_override with a merged factory**

In `scripts/param_sweep.py`, replace the existing `_day_trade_override` function with a merged version that handles both NORMAL/MEDIUM and BULL_TREND/MEDIUM in a single patch:

```python
# DELETE _day_trade_override and replace with:

def _day_trade_override(late_hour: int, late_comp: int, bull_late: int | None):
    """Wraps get_day_trade_params for NORMAL/MEDIUM and optionally BULL_TREND/MEDIUM."""
    _orig = get_day_trade_params
    def _fn(regime: str, tier: str) -> DayTradeParams:
        p = _orig(regime, tier)
        if regime == "NORMAL" and tier == "MEDIUM":
            return DayTradeParams(
                hard_close_hour=p.hard_close_hour,
                late_entry_hour=late_hour,
                late_composite=late_comp,
                vwap_slope_min=p.vwap_slope_min,
            )
        if bull_late is not None and regime == "BULL_TREND" and tier == "MEDIUM":
            return DayTradeParams(
                hard_close_hour=p.hard_close_hour,
                late_entry_hour=p.late_entry_hour,
                late_composite=bull_late,
                vwap_slope_min=p.vwap_slope_min,
            )
        return p
    return _fn
```

- [ ] **Step 3: Update _run_combo to extract new axes and update patches**

In `scripts/param_sweep.py`, inside `_run_combo`, find the block that builds `tp_sl_fn`, `dt_fn`, `state`, and `patches`, and replace it entirely:

```python
    sl_ceiling = combo.get("normal_med_sl_ceiling",      config.STOP_LOSS_PCT)
    late_hour  = combo.get("normal_med_late_hour",       _DAY_TRADE_FALLBACK.late_entry_hour)
    late_comp  = combo.get("normal_med_late_composite",  _DAY_TRADE_FALLBACK.late_composite)
    atr_tp_mult = combo.get("atr_tp_multiplier",         base_params.get("atr_tp_multiplier", config.ATR_TP_MULT_DEFAULT))
    midday_pct  = combo.get("midday_profit_floor_pct",   config.MIDDAY_PROFIT_FLOOR_PCT)
    bull_late   = combo.get("bull_trend_med_late_composite", None)

    is_baseline = combo.get("_baseline_", False)

    state = SimState(atr_tp_multiplier=atr_tp_mult)

    tp_sl_fn = _tp_sl_override(sl_ceiling)
    dt_fn    = _day_trade_override(late_hour, late_comp, bull_late)

    patches = [] if is_baseline else [
        patch("backtest.simulator.get_tp_sl_params",     tp_sl_fn),
        patch("signals.tp_scaler.get_tp_sl_params",      tp_sl_fn),
        patch("utils.regime_params.get_day_trade_params", dt_fn),
        patch("backtest.simulator.config.MIDDAY_PROFIT_FLOOR_PCT", midday_pct),
    ]
```

Note: `_DAY_TRADE_FALLBACK` is already imported from `utils.regime_params` (check the import line at the top of the script — add it if missing: `from utils.regime_params import DayTradeParams, TPSLParams, get_day_trade_params, get_tp_sl_params, _DAY_TRADE_FALLBACK`).

If `_DAY_TRADE_FALLBACK` is not exported, use hard-coded fallbacks:
```python
    late_hour  = combo.get("normal_med_late_hour",      10)
    late_comp  = combo.get("normal_med_late_composite", 68)
```

- [ ] **Step 4: Add _DAY_TRADE_FALLBACK to the regime_params import in param_sweep.py**

In `scripts/param_sweep.py`, find the regime_params import line (around line 32):

```python
# OLD:
from utils.regime_params import (
    DayTradeParams, TPSLParams,
    get_day_trade_params, get_tp_sl_params,
)

# NEW:
from utils.regime_params import (
    DayTradeParams, TPSLParams,
    get_day_trade_params, get_tp_sl_params,
    _DAY_TRADE_FALLBACK,
)
```

- [ ] **Step 5: Verify the sweep runs without error**

```bash
cd /Users/rajeshkumar/Documents/projects/trading-bot
.venv/bin/python scripts/param_sweep.py --days 5 --top 5 --out /tmp/sweep_test.csv
```

Expected: runs 5 trading days, prints top-5 combos to terminal, writes `/tmp/sweep_test.csv`. No errors or tracebacks. The first line of output should say "Running N combos" (capped at 200).

- [ ] **Step 6: Commit**

```bash
git add scripts/param_sweep.py
git commit -m "feat: expand param_sweep to 6 axes (atr_tp_mult, midday_floor, bull_late_composite)

Adds ATR-TP multiplier [2.0, 2.5, 3.0], midday profit floor [0.75, 1.0, 1.25]%,
and BULL_TREND/MEDIUM late composite [60, 65, 68] sweep axes.
Replaces _day_trade_override with merged factory covering both NORMAL/MEDIUM and
BULL_TREND/MEDIUM. Raises MAX_COMBOS 150→200.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Final Verification

- [ ] **Run the full test suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

Expected: all 1646+ tests pass.

- [ ] **Run a 30d backtest to validate directional improvement**

```bash
.venv/bin/python backtest.py 2>&1 | tee /tmp/bt_30d_post.txt
grep -E "Final equity|Total P&L|Win / Loss|Force-close|Swing summary" /tmp/bt_30d_post.txt
```

Compare vs pre-fix: `Total P&L: +$4,166 (+4.16%)`, `Force-close rate: 31.7%`, `Swing WR: 88.9%`.
Expected directional changes: swing count ↑, force-close rate ↓ (via tighter HIGH_VOL gate), swing WR maintained or improved.

- [ ] **Run param sweep to find new optimal config**

```bash
.venv/bin/python scripts/param_sweep.py --days 30 --top 15
```

Apply the winner config to `config.py` / `utils/regime_params.py` per the sweep's top result.

- [ ] **Commit final state**

```bash
git add -p  # review any config changes from sweep winner
git commit -m "perf: apply param_sweep winner config post-profitability fixes"
```
