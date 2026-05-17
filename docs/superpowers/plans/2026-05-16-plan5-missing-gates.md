# Plan 5: Missing Entry Gates

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add three missing soft gates that any retail trading playbook demands: relative volume (avoid low-volume breakouts), opening-range context (flag entries inside first-hour range), and market-breadth confirmation (flag divergence when NYSE breadth is negative). All are soft flags — they append to `gate.flags` for Claude to evaluate, never hard-block.

**Architecture:** Each gate is a pure function in its own `signals/` module (Rule 15 — pure, no I/O). They are wired into `check_all_gates()` as soft gates 16-18. The opening-range and breadth data is fetched by `build_cycle_snapshot()` and added to `CycleSnapshot` — zero I/O inside gates. Relative volume uses the existing daily bars already fetched in Phase 1.

**Tech Stack:** Python 3.11, pandas, pytest

---

## File Map

| File | Change |
|---|---|
| `signals/volume_guard.py` | NEW — `passes_relative_volume(iv) -> (bool, float)` |
| `signals/opening_range_gate.py` | NEW — `opening_range_flag(iv, now, first_bar_open, first_bar_high, first_bar_low)` |
| `macro/breadth_signals.py` | Add `get_advance_decline_ratio()` — uses existing MACRO_BREADTH_TICKERS fetch |
| `trading/cycle_context.py` | Add `advance_decline_ratio: float`, `opening_range: dict` to CycleSnapshot |
| `indicators/values.py` | Add `relative_volume: float` (ratio vs 20d avg), `or_position: str` fields |
| `indicators/engine.py` | Compute `relative_volume` from daily bars in `compute_indicators()` |
| `signals/entry_gates.py` | Wire gates 17-19 (rel-vol, opening-range, breadth) as soft gates |
| `config.py` | Add `REL_VOLUME_MIN = 0.60`, `BREADTH_WEAK_THRESHOLD = 0.85`, `OPENING_RANGE_MINUTES = 60` |
| `tests/test_signals.py` | Tests for all 3 new gates |

---

### Task 1: Config + IndicatorValues — relative_volume field

**Files:**
- Modify: `config.py`
- Modify: `indicators/values.py`
- Modify: `indicators/engine.py`

- [ ] **Step 1: Add config constants**

```python
# config.py — add near Signal Integrity section
REL_VOLUME_MIN          = 0.60   # current volume < 60% of 20d avg → soft flag (weak tape)
BREADTH_WEAK_THRESHOLD  = 0.85   # advancing/declining ratio below this → soft flag
OPENING_RANGE_MINUTES   = 60     # first N minutes of session form the opening range
```

- [ ] **Step 2: Write failing test for relative_volume in IndicatorValues**

```python
# tests/test_indicators.py — add
import pytest
from indicators.values import IndicatorValues

@pytest.mark.unit
def test_indicator_values_has_relative_volume_field():
    from dataclasses import fields
    field_names = [f.name for f in fields(IndicatorValues)]
    assert "relative_volume" in field_names, "IndicatorValues missing relative_volume field"

@pytest.mark.unit
def test_indicator_values_has_or_position_field():
    field_names = [f.name for f in __import__("dataclasses").fields(IndicatorValues)]
    assert "or_position" in field_names, "IndicatorValues missing or_position field"
```

- [ ] **Step 3: Add fields to indicators/values.py**

In the `IndicatorValues` dataclass, add:

```python
relative_volume: float = 1.0      # current volume / 20d avg volume; 1.0 = neutral
or_position: str = "unknown"      # "inside" | "outside" | "unknown" (opening range)
```

- [ ] **Step 4: Compute relative_volume in engine.py**

In `compute_indicators()`, after the daily bars are available:

```python
    # Relative volume: today's total bar volume vs 20-day average daily volume
    relative_volume = 1.0
    try:
        if daily_df is not None and len(daily_df) >= 20 and bars is not None:
            avg_daily_vol = float(daily_df["volume"].iloc[-21:-1].mean())
            todays_vol = float(bars["volume"].sum()) if "volume" in bars.columns else 0.0
            if avg_daily_vol > 0:
                # Scale by session fraction elapsed (bars × 5min / 390min)
                session_fraction = min(1.0, len(bars) * 5 / 390)
                estimated_full_day_vol = todays_vol / session_fraction if session_fraction > 0 else todays_vol
                relative_volume = round(estimated_full_day_vol / avg_daily_vol, 2)
    except Exception:
        pass
```

Pass `relative_volume=relative_volume` to the `IndicatorValues(...)` constructor.

- [ ] **Step 5: Run tests**

```
.venv/bin/python -m pytest tests/test_indicators.py -k "relative_volume or or_position" -v
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 6: Commit**

```bash
git add config.py indicators/values.py indicators/engine.py tests/test_indicators.py
git commit -m "feat: add relative_volume and or_position fields to IndicatorValues; compute rel-vol from daily bars"
```

---

### Task 2: signals/volume_guard.py — relative volume soft gate

**Files:**
- Create: `signals/volume_guard.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_signals.py — add
from signals.volume_guard import is_volume_weak
import config

@pytest.mark.unit
def test_volume_below_min_flags_as_weak(monkeypatch):
    monkeypatch.setattr(config, "REL_VOLUME_MIN", 0.60)
    assert is_volume_weak(relative_volume=0.45) is True

@pytest.mark.unit
def test_volume_above_min_does_not_flag(monkeypatch):
    monkeypatch.setattr(config, "REL_VOLUME_MIN", 0.60)
    assert is_volume_weak(relative_volume=0.80) is False

@pytest.mark.unit
def test_volume_guard_unknown_passes(monkeypatch):
    """relative_volume=1.0 (default/unknown) should not flag."""
    assert is_volume_weak(relative_volume=1.0) is False
```

- [ ] **Step 2: Run to confirm failure**

```
.venv/bin/python -m pytest tests/test_signals.py -k "volume_below_min or volume_above_min or volume_guard_unknown" -v
```

- [ ] **Step 3: Create signals/volume_guard.py**

```python
"""
Relative-volume soft gate. Flags low-volume breakouts that have a high
false-positive rate (dead-cat bounces, stop hunts on thin tape).

Pure function — no I/O, no Alpaca calls.
"""
from __future__ import annotations
import config


def is_volume_weak(relative_volume: float) -> bool:
    """Return True if current volume is below REL_VOLUME_MIN of 20-day average.
    relative_volume = today's estimated full-day vol / 20d avg vol.
    """
    return relative_volume < config.REL_VOLUME_MIN
```

- [ ] **Step 4: Run tests**

```
.venv/bin/python -m pytest tests/test_signals.py -k "volume" -v
```

- [ ] **Step 5: Commit**

```bash
git add signals/volume_guard.py tests/test_signals.py
git commit -m "feat: signals/volume_guard.py — relative-volume soft gate (flags thin-tape breakouts)"
```

---

### Task 3: signals/opening_range_gate.py — first-hour range flag

**Files:**
- Create: `signals/opening_range_gate.py`
- Modify: `trading/cycle_context.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_signals.py — add
from signals.opening_range_gate import get_or_position

@pytest.mark.unit
def test_price_inside_or_flagged(monkeypatch):
    monkeypatch.setattr(config, "OPENING_RANGE_MINUTES", 60)
    # OR: 100-105; price at 102 → inside
    result = get_or_position(current_price=102.0, or_high=105.0, or_low=100.0)
    assert result == "inside"

@pytest.mark.unit
def test_price_above_or_is_outside(monkeypatch):
    monkeypatch.setattr(config, "OPENING_RANGE_MINUTES", 60)
    result = get_or_position(current_price=106.0, or_high=105.0, or_low=100.0)
    assert result == "outside"

@pytest.mark.unit
def test_price_below_or_is_outside(monkeypatch):
    result = get_or_position(current_price=98.0, or_high=105.0, or_low=100.0)
    assert result == "outside"
```

- [ ] **Step 2: Create signals/opening_range_gate.py**

```python
"""
Opening-range context gate.
The opening range is defined as the high/low of the first OPENING_RANGE_MINUTES
of the trading session (9:30-10:30 ET by default).

Entries inside the OR on the first attempt are high-reversal-risk — flag for Claude.
Entries above OR = breakout (preferred). Below OR = avoid.

Pure function — no I/O.
"""
from __future__ import annotations


def get_or_position(current_price: float, or_high: float, or_low: float) -> str:
    """Return 'inside', 'outside', or 'unknown'."""
    if or_high <= 0 or or_low <= 0:
        return "unknown"
    if current_price > or_high or current_price < or_low:
        return "outside"
    return "inside"


def is_inside_opening_range(current_price: float, or_high: float, or_low: float) -> bool:
    return get_or_position(current_price, or_high, or_low) == "inside"
```

- [ ] **Step 3: Compute opening range in indicators/engine.py**

Add OR computation when intraday bars are available:

```python
    # Opening range: high/low of first OPENING_RANGE_MINUTES bars
    or_high, or_low = 0.0, 0.0
    or_position = "unknown"
    try:
        import config as _config
        from signals.opening_range_gate import get_or_position as _or_pos
        or_bars = round(_config.OPENING_RANGE_MINUTES / _config.BAR_TIMEFRAME_MINUTES)
        if bars is not None and len(bars) >= or_bars:
            or_window = bars.iloc[:or_bars]
            or_high = float(or_window["high"].max())
            or_low  = float(or_window["low"].min())
            or_position = _or_pos(current_price, or_high, or_low)
    except Exception:
        pass
```

Pass `or_position=or_position` to `IndicatorValues(...)`.

- [ ] **Step 4: Run tests**

```
.venv/bin/python -m pytest tests/test_signals.py -k "or_flagged or above_or or below_or" -v
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 5: Commit**

```bash
git add signals/opening_range_gate.py indicators/engine.py tests/test_signals.py
git commit -m "feat: opening-range gate — flags entries inside first 60-min range for Claude review"
```

---

### Task 4: Breadth soft gate — A/D ratio

**Files:**
- Modify: `macro/breadth_signals.py`
- Modify: `trading/cycle_context.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_foundation.py — add
from macro.breadth_signals import get_advance_decline_ratio
import pytest

@pytest.mark.unit
def test_advance_decline_ratio_returns_float_or_none():
    """get_advance_decline_ratio must return float or None (never raises)."""
    from unittest.mock import patch
    with patch("macro.breadth_signals._fetch_ad_ratio", return_value=1.3):
        result = get_advance_decline_ratio()
    assert result is None or isinstance(result, float)
```

- [ ] **Step 2: Add get_advance_decline_ratio() to macro/breadth_signals.py**

```python
def get_advance_decline_ratio() -> float | None:
    """Fetch NYSE A/D ratio from breadth data. Returns None on failure.
    Uses IWM (small-cap proxy) advance/decline as a breadth indicator:
    - IWM up → broad participation
    - IWM down while SPY up → narrowing breadth (bull trap signal)
    """
    try:
        from data.market_data import get_intraday_bars
        from utils.constants import ET
        from datetime import datetime

        iwm = get_intraday_bars("IWM", lookback=20)
        spy = get_intraday_bars("SPY", lookback=20)
        if iwm is None or spy is None or len(iwm) < 5 or len(spy) < 5:
            return None

        iwm_chg = (float(iwm["close"].iloc[-1]) - float(iwm["open"].iloc[0])) / float(iwm["open"].iloc[0])
        spy_chg = (float(spy["close"].iloc[-1]) - float(spy["open"].iloc[0])) / float(spy["open"].iloc[0])

        # Breadth ratio: IWM / SPY session return (>1.0 = broad, <0.85 = narrow/diverging)
        if abs(spy_chg) < 0.0001:
            return 1.0  # flat day
        return round(iwm_chg / spy_chg, 2) if spy_chg != 0 else None
    except Exception:
        return None
```

- [ ] **Step 3: Add advance_decline_ratio to CycleSnapshot**

```python
# trading/cycle_context.py
@dataclass
class CycleSnapshot:
    ...
    advance_decline_ratio: float = 1.0   # IWM/SPY breadth ratio; <0.85 → breadth weak
```

In `build_cycle_snapshot()`:

```python
    try:
        from macro.breadth_signals import get_advance_decline_ratio
        ad = get_advance_decline_ratio()
        advance_decline_ratio = ad if ad is not None else 1.0
    except Exception:
        advance_decline_ratio = 1.0

    return CycleSnapshot(
        ...,
        advance_decline_ratio=advance_decline_ratio,
    )
```

- [ ] **Step 4: Wire into check_all_gates() as soft gate**

Pass `advance_decline_ratio: float = 1.0` to `check_all_gates()`.

```python
    # 18. Market breadth (soft gate — warn when IWM/SPY breadth diverging)
    if advance_decline_ratio < config.BREADTH_WEAK_THRESHOLD:
        flags.append(f"breadth_weak(ad={advance_decline_ratio:.2f})")
```

Update the `check_all_gates()` call in `executor.py`:

```python
    gate = check_all_gates(
        ...,
        advance_decline_ratio=snap.advance_decline_ratio if hasattr(snap, "advance_decline_ratio") else 1.0,
    )
```

Wait — `executor.py` doesn't have access to `snap`. The breadth ratio needs to flow through: `cycle.py → pipeline.run() → execute_buy()` with a new `advance_decline_ratio` param, OR read it from `CycleSnapshot` passed through pipeline.

Simpler: add `advance_decline_ratio: float = 1.0` to `execute_buy()` and pass it from `pipeline.py` via `snap.advance_decline_ratio`.

- [ ] **Step 5: Run tests**

```
.venv/bin/python -m pytest tests/test_foundation.py -k "advance_decline" -v
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 6: Commit**

```bash
git add macro/breadth_signals.py trading/cycle_context.py trading/executor.py signals/entry_gates.py tests/test_foundation.py
git commit -m "feat: IWM/SPY breadth soft gate in check_all_gates; flags bull traps on narrow breadth"
```

---

### Task 5: Wire all three soft gates into check_all_gates + executor

- [ ] **Step 1: Update check_all_gates() signature**

Final signature with all new params:

```python
def check_all_gates(
    symbol: str,
    iv: IndicatorValues,
    open_positions: dict,
    daily_closed: set[str],
    bars_15min: pd.DataFrame | None,
    now: datetime,
    entries_this_cycle: int,
    spy_ret5d: float = 0.0,
    symbol_ret5d: float = 0.0,
    mode: str = "DAY_TRADE",
    rsi_max: float = config.RSI_BUY_MAX,
    sector_today: dict | None = None,
    symbol_sector_etf: str | None = None,
    advance_decline_ratio: float = 1.0,
) -> GateResult:
```

Add new soft gates (after existing gate 15 correlation guard):

```python
    # 16. Sector ETF health (from Plan 1)
    if sector_today and symbol_sector_etf:
        etf_chg = sector_today.get(symbol_sector_etf)
        if etf_chg is not None and etf_chg < config.SECTOR_ETF_SOFT_DROP_PCT:
            flags.append(f"sector_weak({symbol_sector_etf}={etf_chg:+.1f}%)")

    # 17. Relative volume
    from signals.volume_guard import is_volume_weak
    if is_volume_weak(iv.relative_volume):
        flags.append(f"rel_vol_low({iv.relative_volume:.2f}x)")

    # 18. Opening range position
    if iv.or_position == "inside":
        flags.append("inside_opening_range")

    # 19. Market breadth
    if advance_decline_ratio < config.BREADTH_WEAK_THRESHOLD:
        flags.append(f"breadth_weak(ad={advance_decline_ratio:.2f})")

    return GateResult(True, "", tuple(flags))
```

- [ ] **Step 2: Update execute_buy() + pipeline.py to thread advance_decline_ratio**

```python
# trading/executor.py — add to execute_buy() signature:
advance_decline_ratio: float = 1.0,

# Pass to check_all_gates():
gate = check_all_gates(
    ...,
    advance_decline_ratio=advance_decline_ratio,
)

# trading/pipeline.py — in execute_buy() calls:
execute_buy(
    ...,
    advance_decline_ratio=snap.advance_decline_ratio,
)
```

- [ ] **Step 3: Run full test suite**

```
.venv/bin/python -m pytest tests/ -m unit -q --tb=short
```
Expected: all green.

- [ ] **Step 4: Commit + tag**

```bash
git add signals/entry_gates.py trading/executor.py trading/pipeline.py tests/test_signals.py
git commit -m "feat: wire rel-vol, opening-range, breadth soft gates into check_all_gates"
git tag plan5-missing-gates-complete
```
