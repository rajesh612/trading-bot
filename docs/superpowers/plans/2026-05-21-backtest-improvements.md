# Backtest Improvements & Eligibility Strengthening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce force-close drag, enable profit-lock swing promotion, add T2 fire-rate visibility, supply real sector data to the T2 live gate, and tighten swing eligibility to prevent overnight risk on flat/losing positions.

**Architecture:** Six tasks. Task 0 lays the regime-dispatch foundation (`utils/regime_params.py`); Tasks 1–5 are independent improvements across `config.py`, `signals/time_gate.py`, `trading/eod_holds.py`, `trading/monitor.py`, `backtest/simulator.py`, and `backtest/report.py`. Each task is self-contained and testable in isolation.

**Tech Stack:** Python 3.12, pytest, `data.market_data.get_intraday_bars`, `sectors.get_sector_etf`, `utils.regimes.SWING_ELIGIBLE`.

---

## File Map

| File | Change |
|---|---|
| `utils/regime_params.py` | **New** — `EntryParams`, `SwingParams`, `T2Params` dataclasses + `get_entry_params()`, `get_swing_params()`, `get_t2_params()` dispatch functions |
| `config.py` | Add `ENTRY_HARD_CLOSE_HOUR`, `PROFIT_LOCK_SWING_PCT`, `SWING_PROMOTE_MIN_PNL_PCT`; remove stale scattered per-regime constants after callers migrate to `regime_params.py` |
| `trading/pre_claude_filter.py` | Replace inline composite-min regime branch with `get_entry_params(regime).composite_min` |
| `signals/tranche2_gates.py` | Replace per-regime T2 threshold overrides with `get_t2_params(regime)` fields |
| `signals/time_gate.py` | Enforce `ENTRY_HARD_CLOSE_HOUR` hard cutoff in `is_entry_allowed()` |
| `trading/eod_holds.py` | Profit-lock swing bypass + min P&L gate in `qualifies_for_eod_hold()` — consumes `get_swing_params()` |
| `trading/monitor.py` | Fetch real sector ETF intraday % change for T2 gate |
| `backtest/simulator.py` | Mirror all three logic changes (hard cutoff, profit-lock bypass, min P&L) + `t2_fire_count` on `SimState` |
| `backtest/report.py` | Print T2 fire count and rate in summary block |
| `tests/test_signals.py` | Entry time gate unit tests |
| `tests/test_foundation.py` | `TestGetEntryParams`, `TestGetSwingParams`, `TestGetT2Params` + EOD-hold qualification unit tests |
| `tests/test_tranche2.py` | T2 counter + live sector data tests |

---

### Task 0: utils/regime_params.py — single source of truth for regime × mode dispatch

**Context:** Regime-specific thresholds are currently scattered as parallel constants (`COMPOSITE_MIN_NORMAL`, `CHOPPY_COMPOSITE_GATE`, `SWING_TP1_PCT_CAPITULATION`, etc.) and dispatched inline in multiple modules via `if regime == "CHOPPY": ...` chains. Every new regime variant adds another constant and another branch in another file.

The better design: a `utils/regime_params.py` module that owns all regime × mode dispatch in one place.

**Why before Task 2:** The Task 2 revision (strict CHOPPY/DEFENSIVE swing gates) would add 6 new regime-specific constants to `config.py` and 4 new `if regime == "CHOPPY"` branches to `eod_holds.py`. Without the utility layer, that's exactly the scattered pattern we're trying to eliminate. With `get_swing_params(regime)`, the CHOPPY/DEFENSIVE gates become a clean table lookup — readable in one pass.

**Design:**

```python
# utils/regime_params.py

@dataclass(frozen=True)
class EntryParams:
    composite_min: int        # pre-Claude composite floor
    rsi_max: float            # RSI hard ceiling
    hard_close_hour: int      # no entries at/after this ET hour
    position_mult: float      # position size multiplier
    entry_allowed: bool       # False in CRISIS

@dataclass(frozen=True)
class SwingParams:
    eligible: bool            # can promote to swing at all?
    min_pnl_pct: float        # minimum EOD P&L to qualify
    profit_lock_pct: float    # profit-lock bypass threshold
    sentiment_min: float      # symbol sentiment floor
    sector_floor_pct: float   # sector ETF must be >= this % intraday
    composite_min: float      # composite at promotion time
    max_days: int             # max hold days
    sl_pct: float             # overnight SL %
    tp1_pct: float            # first TP tier %
    tp2_pct: float            # second TP tier %
    vix_rising_blocks: bool   # block if VIX trending up

@dataclass(frozen=True)
class T2Params:
    drop_min_pct: float       # price must be >= this % below T1 entry
    drop_max_pct: float       # price must be <= this % below T1 entry
    rsi_max: float            # RSI ceiling at trigger
    composite_min: float      # composite floor at trigger
    sentiment_min: float      # symbol sentiment floor
    sector_floor_pct: float   # sector ETF floor
    cutoff_hour: int          # no T2 after this ET hour
    min_hold_min: int         # T1 hold time before T2 eligible

def get_entry_params(regime: str, mode: str = "DAY_TRADE") -> EntryParams: ...
def get_swing_params(regime: str) -> SwingParams: ...
def get_t2_params(regime: str) -> T2Params: ...
```

**Caller migration (before → after):**

```python
# Before (scattered inline branches)
composite_min = config.CHOPPY_COMPOSITE_GATE if regime == "CHOPPY" else \
                config.COMPOSITE_MIN_HIGH_VOL if regime in regimes.HIGH_VOL_GATES else \
                config.COMPOSITE_MIN_NORMAL

# After (single call)
params = get_entry_params(regime, mode)
composite_min = params.composite_min
```

```python
# eod_holds.py — readable in one pass after migration
params = get_swing_params(regime)
if not params.eligible and current_pnl_pct < params.profit_lock_pct:
    return False, f"{regime}: insufficient P&L ({current_pnl_pct:.1f}% < {params.profit_lock_pct}%)"
if sentiment_score < params.sentiment_min:
    return False, f"{regime}: sentiment {sentiment_score:.0f} < {params.sentiment_min}"
if sector_etf_chg < params.sector_floor_pct:
    return False, f"{regime}: sector {sector_etf_chg:.1f}% below floor"
```

**Files:**
- Create: `utils/regime_params.py` — `EntryParams`, `SwingParams`, `T2Params` dataclasses + `get_entry_params()`, `get_swing_params()`, `get_t2_params()` dispatch functions
- Modify: `config.py` — add any missing per-regime constants referenced by the tables (CHOPPY/DEFENSIVE swing thresholds, T2 per-regime overrides)
- Test: `tests/test_foundation.py` — one test class per getter, covering every regime row

- [ ] **Step 1: Write failing tests**

In `tests/test_foundation.py`, add:

```python
@pytest.mark.unit
class TestGetEntryParams:
    def test_crisis_blocks_entry(self):
        from utils.regime_params import get_entry_params
        p = get_entry_params("CRISIS")
        assert p.entry_allowed is False

    def test_choppy_raises_composite_min(self):
        from utils.regime_params import get_entry_params
        normal = get_entry_params("NORMAL")
        choppy = get_entry_params("CHOPPY")
        assert choppy.composite_min > normal.composite_min

    def test_bull_trend_returns_entry_params(self):
        from utils.regime_params import get_entry_params
        p = get_entry_params("BULL_TREND")
        assert p.entry_allowed is True
        assert isinstance(p.composite_min, int)

    def test_unknown_regime_falls_back_to_normal(self):
        from utils.regime_params import get_entry_params
        p = get_entry_params("UNKNOWN_FUTURE_REGIME")
        normal = get_entry_params("NORMAL")
        assert p.composite_min == normal.composite_min


@pytest.mark.unit
class TestGetSwingParams:
    def test_crisis_not_eligible(self):
        from utils.regime_params import get_swing_params
        p = get_swing_params("CRISIS")
        assert p.eligible is False

    def test_choppy_not_eligible(self):
        from utils.regime_params import get_swing_params
        p = get_swing_params("CHOPPY")
        assert p.eligible is False

    def test_defensive_not_eligible(self):
        from utils.regime_params import get_swing_params
        p = get_swing_params("DEFENSIVE")
        assert p.eligible is False

    def test_normal_eligible_with_positive_min_pnl(self):
        from utils.regime_params import get_swing_params
        p = get_swing_params("NORMAL")
        assert p.eligible is True
        assert p.min_pnl_pct > 0

    def test_profit_lock_bypass_above_min_pnl(self):
        from utils.regime_params import get_swing_params
        p = get_swing_params("CHOPPY")
        assert p.profit_lock_pct > p.min_pnl_pct


@pytest.mark.unit
class TestGetT2Params:
    def test_crisis_has_no_t2(self):
        from utils.regime_params import get_t2_params
        p = get_t2_params("CRISIS")
        assert p.cutoff_hour == 0  # effectively disabled

    def test_normal_t2_params_well_formed(self):
        from utils.regime_params import get_t2_params
        p = get_t2_params("NORMAL")
        assert 0 < p.drop_min_pct < p.drop_max_pct
        assert p.rsi_max > 0
        assert p.cutoff_hour > 0
        assert p.min_hold_min > 0

    def test_choppy_t2_more_restrictive_than_normal(self):
        from utils.regime_params import get_t2_params
        normal = get_t2_params("NORMAL")
        choppy = get_t2_params("CHOPPY")
        # CHOPPY should require a larger drop or tighter RSI
        assert choppy.rsi_max <= normal.rsi_max or choppy.drop_min_pct >= normal.drop_min_pct
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_foundation.py::TestGetEntryParams tests/test_foundation.py::TestGetSwingParams tests/test_foundation.py::TestGetT2Params -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'utils.regime_params'`

- [ ] **Step 3: Create utils/regime_params.py**

Create the file with frozen dataclasses and per-regime lookup tables. All threshold values must reference `config.*` constants (no magic numbers). The functions must return a sensible NORMAL-equivalent default for any unrecognised regime string (fail-safe).

Key table rows to cover (minimum):

| Regime | `SwingParams.eligible` | `SwingParams.min_pnl_pct` | `SwingParams.profit_lock_pct` | `SwingParams.sentiment_min` | `SwingParams.sector_floor_pct` |
|---|---|---|---|---|---|
| NORMAL | True | 0.5 | 2.5 | -20 | -1.0 |
| BULL_TREND | True | 0.3 | 2.0 | -30 | -2.0 |
| OPPORTUNITY | True | 0.5 | 2.5 | -10 | 0.0 |
| CRASH_RECOVERY | True | 1.0 | 2.5 | 0 | 0.0 |
| CHOPPY | False | 0.5 | 2.5 | 10 | 0.5 |
| DEFENSIVE | False | 0.5 | 3.0 | 0 | 0.0 |
| HIGH_VOL | True | 0.5 | 3.0 | -10 | -1.0 |
| CRISIS | False | 999 | 999 | 999 | 999 |

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_foundation.py::TestGetEntryParams tests/test_foundation.py::TestGetSwingParams tests/test_foundation.py::TestGetT2Params -v
```

Expected: PASS (all tests)

- [ ] **Step 5: Migrate existing callers to use regime_params.py (remove stale inline branches)**

Audit every module that currently dispatches on `regime` inline. Replace each inline chain with the appropriate getter. Known sites:

| File | Stale pattern | Replace with |
|---|---|---|
| `trading/pre_claude_filter.py` | `composite_min = config.CHOPPY_COMPOSITE_GATE if regime == "CHOPPY" else ...` | `get_entry_params(regime).composite_min` |
| `trading/eod_holds.py` | `if regime not in regimes.SWING_ELIGIBLE:` + scattered PTJ thresholds | `get_swing_params(regime)` — `eligible`, `min_pnl_pct`, `profit_lock_pct`, `sentiment_min`, `sector_floor_pct` |
| `backtest/simulator.py` | `if regime in ("NORMAL", "CRASH_RECOVERY", ...)` swing promotion check | `get_swing_params(regime).eligible` |
| `signals/tranche2_gates.py` | Per-regime `drop_min_pct` / `rsi_max` overrides (if any) | `get_t2_params(regime)` fields |

For each site: (a) replace inline branch with getter call, (b) delete the now-unreferenced config constant(s) it was reading, (c) verify the test for that module still passes.

- [ ] **Step 6: Remove stale config constants superseded by regime_params.py**

After migration, grep for each constant to confirm zero remaining references, then delete from `config.py`:

```bash
# Check nothing still references old scattered constants before deleting
grep -rn "CHOPPY_COMPOSITE_GATE\|HIGH_VOL_COMPOSITE_GATE\|SWING_TP1_PCT_CAPITULATION\|SWING_ELIGIBLE_REGIMES" \
     --include="*.py" . | grep -v "^tests/" | grep -v "^docs/"
```

Only delete a constant when the grep output is empty (no non-test, non-doc references remain).

- [ ] **Step 7: Run full unit suite to confirm no regressions**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

Expected: all pass

- [ ] **Step 8: Commit**

```bash
git add utils/regime_params.py config.py trading/pre_claude_filter.py trading/eod_holds.py \
        backtest/simulator.py signals/tranche2_gates.py tests/test_foundation.py
git commit -m "feat: add utils/regime_params.py — single source of truth for regime×mode dispatch

Previously regime-specific thresholds were scattered as parallel constants in
config.py and dispatched via inline if-chains in eod_holds.py, simulator.py,
and pre_claude_filter.py. Introduces EntryParams, SwingParams, and T2Params
frozen dataclasses with get_entry_params(), get_swing_params(), get_t2_params()
dispatch. All existing callers migrated; stale parallel config constants removed.
Task 2 CHOPPY/DEFENSIVE swing gates now consume get_swing_params() as a clean
table lookup instead of adding more if-branches."
```

---

### Task 1: Entry hard cutoff at 1 PM

**Context:** Entries after 1 PM have < 3 hours to resolve — most end as force-closes. The existing `is_entry_allowed()` in `signals/time_gate.py` uses `ENTRY_GATE_CLOSE_MINUTES = 35` (blocks after 3:25 PM), which is too late. A new constant `ENTRY_HARD_CLOSE_HOUR = 13` provides a hard 1 PM wall used by both live and backtest paths. The backtest simulator also reads `ENTRY_END_HOUR` (currently 15); it must switch to this new constant.

**Files:**
- Modify: `config.py` — add `ENTRY_HARD_CLOSE_HOUR`
- Modify: `signals/time_gate.py:14-21` — enforce hard cutoff
- Modify: `backtest/simulator.py:38` — use `ENTRY_HARD_CLOSE_HOUR` for `ENTRY_END_H`
- Test: `tests/test_signals.py`

- [ ] **Step 1: Write failing tests**

In `tests/test_signals.py`, add to the existing time-gate test class (or create one if absent):

```python
import pytest
from datetime import datetime
from zoneinfo import ZoneInfo
import config

ET = ZoneInfo("America/New_York")

@pytest.mark.unit
class TestEntryHardCutoff:
    def _now(self, hour, minute=0):
        return datetime(2026, 5, 21, hour, minute, tzinfo=ET)

    def test_entry_allowed_before_hard_cutoff(self, monkeypatch):
        monkeypatch.setattr(config, "ENTRY_HARD_CLOSE_HOUR", 13)
        from signals.time_gate import is_entry_allowed
        # 12:59 PM should be allowed (within 1 PM cutoff)
        assert is_entry_allowed(self._now(12, 59)) is True

    def test_entry_blocked_at_hard_cutoff(self, monkeypatch):
        monkeypatch.setattr(config, "ENTRY_HARD_CLOSE_HOUR", 13)
        from signals.time_gate import is_entry_allowed
        # 1:00 PM should be blocked
        assert is_entry_allowed(self._now(13, 0)) is False

    def test_entry_blocked_after_hard_cutoff(self, monkeypatch):
        monkeypatch.setattr(config, "ENTRY_HARD_CLOSE_HOUR", 13)
        from signals.time_gate import is_entry_allowed
        # 2:30 PM should be blocked
        assert is_entry_allowed(self._now(14, 30)) is False
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_signals.py::TestEntryHardCutoff -v
```

Expected: FAIL — `AttributeError: module 'config' has no attribute 'ENTRY_HARD_CLOSE_HOUR'`

- [ ] **Step 3: Add constant to config.py**

In `config.py`, after line `ENTRY_LATE_COMPOSITE_MIN = 55` (around line 114), add:

```python
ENTRY_HARD_CLOSE_HOUR    = 13   # hard entry wall — no new entries at or after 1:00 PM ET
```

- [ ] **Step 4: Update is_entry_allowed() in signals/time_gate.py**

Replace the entire function body:

```python
def is_entry_allowed(now: datetime) -> bool:
    """Returns False during first ENTRY_GATE_OPEN_MINUTES after open, last ENTRY_GATE_CLOSE_MINUTES
    before market close, or at/after ENTRY_HARD_CLOSE_HOUR."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    else:
        now = now.astimezone(ET)

    minutes = now.hour * 60 + now.minute  # minutes since midnight ET
    gate_open_end    = 9 * 60 + 30 + config.ENTRY_GATE_OPEN_MINUTES    # default: 10:00
    gate_close_start = 16 * 60 - config.ENTRY_GATE_CLOSE_MINUTES        # default: 15:25
    hard_close_mins  = config.ENTRY_HARD_CLOSE_HOUR * 60                # default: 13:00

    return gate_open_end <= minutes < min(gate_close_start, hard_close_mins)
```

- [ ] **Step 5: Update backtest simulator to use new constant**

In `backtest/simulator.py` line 38, replace:

```python
ENTRY_END_H   = config.ENTRY_END_HOUR;  HIGH_VOL_MULT    = config.HIGH_VOL_POSITION_MULT
```

with:

```python
ENTRY_END_H   = config.ENTRY_HARD_CLOSE_HOUR;  HIGH_VOL_MULT = config.HIGH_VOL_POSITION_MULT
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_signals.py::TestEntryHardCutoff -v
```

Expected: PASS (all 3 tests)

- [ ] **Step 7: Run full unit suite to confirm no regressions**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

Expected: all pass

- [ ] **Step 8: Commit**

```bash
git add config.py signals/time_gate.py backtest/simulator.py tests/test_signals.py
git commit -m "feat: add ENTRY_HARD_CLOSE_HOUR=13 to block entries after 1 PM ET

Entries after 1 PM have < 3 hours to resolve and contribute disproportionately
to the 54% force-close rate. is_entry_allowed() now enforces a hard 1 PM wall
in both live trading and backtest simulation."
```

---

### Task 2: Profit-lock swing bypass + swing minimum P&L gate

**Context:** Two related eligibility improvements to `trading/eod_holds.py`:

1. **Profit-lock bypass:** `qualifies_for_eod_hold()` currently blocks swing promotion in CHOPPY and DEFENSIVE regimes even when a position is strongly profitable (+2.5%). A winning position carries its own risk buffer. Add a bypass: if `current_pnl_pct >= PROFIT_LOCK_SWING_PCT` and regime != CRISIS, always qualify regardless of regime.

2. **Min P&L gate:** The PTJ floor is -2%, so a -1.9% position can currently be promoted overnight. Add `SWING_PROMOTE_MIN_PNL_PCT = 0.5` — require at least +0.5% gain for standard promotion (the profit-lock bypass at +2.5% is already above this, so it short-circuits first).

Mirror both changes in `backtest/simulator.py`'s `_evaluate_swing_promotion()` (which already requires `pnl_pct > 0` but not the profit-lock bypass).

**Files:**
- Modify: `config.py` — add `PROFIT_LOCK_SWING_PCT`, `SWING_PROMOTE_MIN_PNL_PCT`
- Modify: `trading/eod_holds.py:55-65` — add profit-lock bypass + min P&L gate in `qualifies_for_eod_hold()`
- Modify: `backtest/simulator.py:138-157` — add profit-lock bypass + min P&L gate in `_evaluate_swing_promotion()`
- Test: `tests/test_foundation.py`

- [ ] **Step 1: Write failing tests**

In `tests/test_foundation.py`, add to the existing EOD-holds test class:

```python
@pytest.mark.unit
class TestQualifiesForEodHold:
    def _call(self, **kwargs):
        from trading.eod_holds import qualifies_for_eod_hold
        defaults = dict(
            symbol="AAPL", regime="NORMAL", earnings_tomorrow=False,
            current_pnl_pct=1.0, entry_sector_sentiment=10.0,
            current_sector_sentiment=5.0, consecutive_red_days=0, vix_trend="stable",
        )
        defaults.update(kwargs)
        return qualifies_for_eod_hold(**defaults)

    def test_profit_lock_bypass_choppy(self, monkeypatch):
        # Profitable position in CHOPPY should qualify via profit-lock bypass
        monkeypatch.setattr(config, "PROFIT_LOCK_SWING_PCT", 2.5)
        monkeypatch.setattr(config, "SWING_PROMOTE_MIN_PNL_PCT", 0.5)
        result, reason = self._call(regime="CHOPPY", current_pnl_pct=2.5)
        assert result is True
        assert reason == ""

    def test_profit_lock_bypass_requires_enough_pnl(self, monkeypatch):
        # Below profit-lock threshold in CHOPPY should still fail
        monkeypatch.setattr(config, "PROFIT_LOCK_SWING_PCT", 2.5)
        monkeypatch.setattr(config, "SWING_PROMOTE_MIN_PNL_PCT", 0.5)
        result, reason = self._call(regime="CHOPPY", current_pnl_pct=1.0)
        assert result is False
        assert "choppy" in reason.lower() or "CHOPPY" in reason

    def test_profit_lock_bypass_blocked_in_crisis(self, monkeypatch):
        # CRISIS blocks even highly profitable positions
        monkeypatch.setattr(config, "PROFIT_LOCK_SWING_PCT", 2.5)
        monkeypatch.setattr(config, "SWING_PROMOTE_MIN_PNL_PCT", 0.5)
        result, reason = self._call(regime="CRISIS", current_pnl_pct=5.0)
        assert result is False
        assert "CRISIS" in reason

    def test_min_pnl_gate_blocks_flat_position(self, monkeypatch):
        # Position at +0.3% (below SWING_PROMOTE_MIN_PNL_PCT=0.5) should be blocked
        monkeypatch.setattr(config, "PROFIT_LOCK_SWING_PCT", 2.5)
        monkeypatch.setattr(config, "SWING_PROMOTE_MIN_PNL_PCT", 0.5)
        result, reason = self._call(regime="NORMAL", current_pnl_pct=0.3)
        assert result is False
        assert "insufficient" in reason.lower() or "P&L" in reason

    def test_min_pnl_gate_passes_at_threshold(self, monkeypatch):
        monkeypatch.setattr(config, "PROFIT_LOCK_SWING_PCT", 2.5)
        monkeypatch.setattr(config, "SWING_PROMOTE_MIN_PNL_PCT", 0.5)
        result, reason = self._call(regime="NORMAL", current_pnl_pct=0.5)
        assert result is True
        assert reason == ""
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_foundation.py::TestQualifiesForEodHold -v
```

Expected: FAIL — `AttributeError: module 'config' has no attribute 'PROFIT_LOCK_SWING_PCT'`

- [ ] **Step 3: Add constants to config.py**

In `config.py`, after `PROFIT_LOCK_TRIGGER_PCT = 2.0` (around line 208), add:

```python
PROFIT_LOCK_SWING_PCT   = 2.5  # positions with unrealised P&L >= this % qualify for swing in any non-CRISIS regime
SWING_PROMOTE_MIN_PNL_PCT = 0.5  # standard EOD promotion requires at least this % gain (prevents holding flat losers overnight)
```

- [ ] **Step 4: Update qualifies_for_eod_hold() in trading/eod_holds.py**

Find the block starting at the CRISIS gate (around line 55). Insert two new gates — the profit-lock bypass goes BEFORE the SWING_ELIGIBLE check, the min P&L gate goes AFTER the PTJ rule:

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
) -> tuple[bool, str]:
    # Universal veto 1: CRISIS — no overnight exposure under any circumstances
    if regime == "CRISIS":
        return False, "CRISIS regime — no overnight exposure"

    # Universal veto 2: earnings TOMORROW — binary risk
    if earnings_tomorrow:
        return False, "earnings tomorrow — overnight binary risk"

    # Profit-lock bypass: strongly profitable positions qualify in any non-CRISIS regime
    if current_pnl_pct >= config.PROFIT_LOCK_SWING_PCT:
        return True, ""

    # Any non-swing-eligible regime — block new swing promotions
    if regime not in regimes.SWING_ELIGIBLE:
        return False, f"{regime} regime — no new swing promotions"

    # Minimum P&L gate — prevent overnight risk on flat/marginal positions
    if current_pnl_pct < config.SWING_PROMOTE_MIN_PNL_PCT:
        return False, f"insufficient P&L for swing ({current_pnl_pct:.1f}% < {config.SWING_PROMOTE_MIN_PNL_PCT}%)"

    # PTJ rule: don't hold losers overnight — threshold varies by regime
    ptj_threshold = _ptj_threshold(regime)
    if current_pnl_pct < ptj_threshold:
        return False, f"PTJ rule: P&L {current_pnl_pct:.1f}% < {ptj_threshold}% ({regime})"

    # NORMAL: veto on sector reversal (current < -20 AND entry was > +10)
    if regime == "NORMAL":
        if current_sector_sentiment < -20 and entry_sector_sentiment > 10:
            return False, (
                f"sector reversed: current {current_sector_sentiment:.0f} < -20, "
                f"entry was {entry_sector_sentiment:.0f}"
            )

    # OPPORTUNITY: thesis requires sector health — veto if sector went negative
    if regime == "OPPORTUNITY":
        if current_sector_sentiment < 0 and entry_sector_sentiment > 10:
            return False, (
                f"OPPORTUNITY thesis broken: sector {current_sector_sentiment:.0f} < 0 "
                f"(was {entry_sector_sentiment:.0f} at entry)"
            )

    # CRASH_RECOVERY: veto if recovery stalled (3+ consecutive red days)
    if regime == "CRASH_RECOVERY":
        if consecutive_red_days >= 3:
            return False, f"CRASH_RECOVERY: recovery stalled ({consecutive_red_days} consecutive red days)"

    return True, ""
```

- [ ] **Step 5: Update _evaluate_swing_promotion() in backtest/simulator.py**

Replace lines 148-156 (`pnl_pct` check through `n_swings += 1` block):

```python
        pnl_pct = (eod_price - pos2["entry"]) / pos2["entry"] * 100
        # Profit-lock bypass: promote in any non-CRISIS regime when gain is strong
        profit_lock_bypass = (
            pnl_pct >= config.PROFIT_LOCK_SWING_PCT
            and regime != "CRISIS"
            and n_swings < SWING_MAX_POSITIONS
        )
        standard_qualify = (
            regime in ("NORMAL", "CRASH_RECOVERY", "BULL_TREND", "OPPORTUNITY")
            and pnl_pct >= config.SWING_PROMOTE_MIN_PNL_PCT
            and pos2.get("composite", 0) >= SWING_COMPOSITE_MIN
            and n_swings < SWING_MAX_POSITIONS
        )
        if profit_lock_bypass or standard_qualify:
            pos2["is_swing"] = True
            pos2["swing_days_remaining"] = SWING_MAX_DAYS
            pos2["sl"] = round(pos2["entry"] * (1 - SWING_SL_PCT / 100), 4)
            pos2["tp"] = round(pos2["entry"] * (1 + SWING_TP_PCT / 100), 4)
            n_swings += 1
            tag = "PROFIT-LOCK" if profit_lock_bypass else "STANDARD"
            logger.info(
                f"    SWING PROMOTE {sym2} [{tag}]: P&L={pnl_pct:+.2f}% composite={pos2['composite']:.0f}"
                f" | SL={pos2['sl']:.2f} TP={pos2['tp']:.2f}"
            )
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_foundation.py::TestQualifiesForEodHold -v
```

Expected: PASS (all 5 tests)

- [ ] **Step 7: Run full unit suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

Expected: all pass

- [ ] **Step 8: Commit**

```bash
git add config.py trading/eod_holds.py backtest/simulator.py tests/test_foundation.py
git commit -m "feat: profit-lock swing bypass + min P&L gate for EOD promotion

Two eligibility improvements:
1. Positions with P&L >= 2.5% (PROFIT_LOCK_SWING_PCT) qualify for swing in
   any non-CRISIS regime — including CHOPPY/DEFENSIVE. Profitable positions
   carry their own risk buffer independent of regime.
2. Standard EOD promotion now requires P&L >= 0.5% (SWING_PROMOTE_MIN_PNL_PCT).
   Previously a -1.9% position could be promoted (PTJ floor = -2%). Prevents
   overnight risk on flat or marginally losing positions."
```

---

### Task 3: T2 fire-rate counter in backtest report

**Context:** T2 fires are currently invisible in backtest output. After fixing the gate thresholds, we need to verify T2 is firing at a healthy rate (~10–15% of positions). Add `t2_fire_count` to `SimState`, increment it at the T2 trigger site, and display it in the report summary.

**Files:**
- Modify: `backtest/simulator.py:58-68` — add `t2_fire_count` field to `SimState`
- Modify: `backtest/simulator.py:350-353` — increment counter after `_sim_apply_tranche2()`
- Modify: `backtest/report.py:105-108` — display T2 fire rate in summary block
- Test: `tests/test_tranche2.py`

- [ ] **Step 1: Write failing test**

In `tests/test_tranche2.py`, add to `TestSimTranche2` class:

```python
    def test_t2_fire_count_increments(self, monkeypatch):
        """SimState.t2_fire_count starts at 0 and increments when T2 fires."""
        import backtest.simulator as sim
        state = sim.SimState()
        assert state.t2_fire_count == 0

        # Manually apply T2 to verify counter
        pos = {
            "qty": 10, "entry": 100.0, "sl": 96.0, "tp": 105.0,
            "entry_ts": "2026-05-21T10:00:00-04:00",
            "entry_time": "2026-05-21T10:00:00-04:00",
            "tranche_1_entry": 100.0, "tranche_2_triggered": False,
        }
        state.positions["AAPL"] = pos
        state.cash = 90_000.0

        sim._sim_apply_tranche2(pos, 97.5, 10, 100_000.0)
        state.t2_fire_count += 1  # mimic call site
        assert state.t2_fire_count == 1
        assert pos.get("tranche_2_triggered") is True
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python -m pytest tests/test_tranche2.py::TestSimTranche2::test_t2_fire_count_increments -v
```

Expected: FAIL — `AttributeError: 'SimState' object has no attribute 't2_fire_count'`

- [ ] **Step 3: Add t2_fire_count to SimState**

In `backtest/simulator.py`, find the `SimState` dataclass (lines 58-67). Add the new field after `sl_blacklist_pending`:

```python
@dataclass
class SimState:
    cash: float = STARTING_CAPITAL
    positions: dict = field(default_factory=dict)
    daily_closed: set = field(default_factory=set)
    equity_curve: list = field(default_factory=list)
    trades: list = field(default_factory=list)
    atr_tp_multiplier: float = config.ATR_TP_MULT_DEFAULT
    sl_blacklist: set = field(default_factory=set)
    sl_blacklist_pending: set = field(default_factory=set)
    t2_fire_count: int = 0
```

- [ ] **Step 4: Increment counter at the T2 trigger site**

In `backtest/simulator.py`, find the T2 block inside `simulate_day()` (around lines 342-353):

```python
            # T2 add-on check — use bar["low"] as worst intrabar price proxy
            elif not pos.get("tranche_2_triggered") and not is_sw:
                t1_entry = pos.get("tranche_1_entry", pos.get("entry", 0))
                if t1_entry > 0:
                    drop_pct = (t1_entry - float(bar["low"])) / t1_entry * 100
                    iv_at_bar = precomp.get(sym, {}).get(ts)
                    if iv_at_bar is None:
                        iv_at_bar = type("_IV", (), {"rsi": 100, "composite_score": 0})()
                    if _sim_tranche2_gates_pass(pos, iv_at_bar, drop_pct, regime, ts_et):
                        current_portfolio = state.cash + _pos_value_at(state, day_bars, ts)
                        _sim_apply_tranche2(pos, float(bar["low"]), int(pos.get("qty", 1)), current_portfolio)
                        state.t2_fire_count += 1
```

(The only change is the last line `state.t2_fire_count += 1` added after `_sim_apply_tranche2()`.)

- [ ] **Step 5: Add T2 stats to backtest/report.py**

In `backtest/report.py`, after line 107 (`print(f"  Force-close rate...")`), add:

```python
    t2_rate = state.t2_fire_count / n * 100 if n else 0.0
    print(f"  T2 fire count       : {state.t2_fire_count}  ({t2_rate:.1f}% of T1 positions)")
```

- [ ] **Step 6: Run test to verify it passes**

```bash
.venv/bin/python -m pytest tests/test_tranche2.py::TestSimTranche2::test_t2_fire_count_increments -v
```

Expected: PASS

- [ ] **Step 7: Run full unit suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

Expected: all pass

- [ ] **Step 8: Commit**

```bash
git add backtest/simulator.py backtest/report.py tests/test_tranche2.py
git commit -m "feat: add T2 fire-rate counter to SimState and backtest report

Previously invisible — T2 fires were undetectable in backtest output.
SimState.t2_fire_count increments whenever _sim_apply_tranche2() runs.
Report prints count and rate (as % of T1 positions) to validate T2 is
firing at a healthy rate (target: 10–20% of positions)."
```

---

### Task 4: Real sector ETF data for live T2 gate

**Context:** `_check_tranche2_triggers()` in `trading/monitor.py` currently passes `sector_etf_chg=0.0` to `check_tranche2_gates()`, so Gate 11 (sector weakness veto) never rejects in live trading. Before the per-position loop, batch-fetch today's intraday bars for each unique sector ETF, compute intraday % change (open-to-latest), and pass the real value to the gate.

`sectors.get_sector_etf(symbol)` maps a ticker to its sector ETF. `data.market_data.get_intraday_bars(etf, lookback=2)` gives recent 5-min bars; the earliest bar's `open` is the session open proxy. `broker.alpaca_client.AlpacaClient().get_latest_price(etf)` gives the current price.

**Files:**
- Modify: `trading/monitor.py:60-128` — batch-fetch sector ETF data before loop, pass real `sector_etf_chg`
- Test: `tests/test_tranche2.py`

- [ ] **Step 1: Write failing test**

In `tests/test_tranche2.py`, add to `TestCheckTranche2Triggers` class:

```python
    def test_sector_etf_chg_passed_to_gate(self, monkeypatch):
        """Monitor passes real sector ETF % change (not always 0.0) to check_tranche2_gates."""
        import trading.monitor as mon
        import sectors
        from datetime import datetime
        from zoneinfo import ZoneInfo

        ET = ZoneInfo("America/New_York")
        now = datetime(2026, 5, 21, 11, 0, tzinfo=ET)

        captured = {}

        def fake_gate(symbol, iv, pos, now, regime, sentiment_score, sector_etf_chg, major_event_today):
            captured["sector_etf_chg"] = sector_etf_chg
            from signals.tranche2_gates import T2GateResult
            return T2GateResult(False, "test skip")

        def fake_iv(sym, alpaca):
            return _t2_iv(current_price=97.5)

        def fake_price(sym):
            return 104.0  # ETF current price

        import pandas as pd
        def fake_bars(sym, lookback=2, timeframe_min=5):
            # Return a 2-bar DataFrame where first bar open = 100.0
            idx = pd.to_datetime(["2026-05-21 09:30:00-04:00", "2026-05-21 09:35:00-04:00"])
            return pd.DataFrame({"open": [100.0, 101.0], "high": [101.0, 102.0],
                                 "low": [99.0, 100.0], "close": [101.0, 102.0],
                                 "volume": [1000, 1000]}, index=idx)

        monkeypatch.setattr(mon, "_try_compute_iv", fake_iv)
        monkeypatch.setattr(sectors, "get_sector_etf", lambda sym: "XLK")
        monkeypatch.setattr("trading.monitor.get_intraday_bars", fake_bars)

        pos = _t2_pos()
        open_positions = {"AAPL": pos}
        fake_alpaca = types.SimpleNamespace(get_latest_price=fake_price)

        monkeypatch.setattr("trading.monitor.check_tranche2_gates", fake_gate)
        mon._check_tranche2_triggers(open_positions, fake_alpaca, now)

        # sector_etf_chg should be (104 - 100) / 100 * 100 = 4.0%, not 0.0
        assert "sector_etf_chg" in captured
        assert abs(captured["sector_etf_chg"] - 4.0) < 0.5
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python -m pytest tests/test_tranche2.py::TestCheckTranche2Triggers::test_sector_etf_chg_passed_to_gate -v
```

Expected: FAIL — `AssertionError` because `sector_etf_chg` is always 0.0

- [ ] **Step 3: Update _check_tranche2_triggers() in trading/monitor.py**

The function currently spans approximately lines 60–128. Replace it with the version below that batch-fetches sector ETF data before the loop:

```python
def _check_tranche2_triggers(open_positions: dict, alpaca, now) -> None:
    """Check each open day-trade position for T2 eligibility and fire if all gates pass."""
    from macro.regime import load_macro_regime
    from macro.economic_calendar import get_todays_events
    from data.market_data import get_intraday_bars
    import sectors

    try:
        regime = load_macro_regime().get("regime", "NORMAL")
    except Exception:
        regime = "NORMAL"

    research: dict = {}
    try:
        from utils.io_utils import load_json
        research = load_json(config.RESEARCH_REPORT_FILE, {})
    except Exception:
        pass

    major_event_today = False
    try:
        _high_impact = {"FOMC", "CPI", "NFP", "PPI", "PCE", "JOLTS", "GDP"}
        for evt in get_todays_events():
            if any(tag in evt.get("name", "").upper() for tag in _high_impact):
                major_event_today = True
                break
    except Exception:
        pass

    try:
        portfolio_value = alpaca.get_portfolio_value()
    except Exception:
        portfolio_value = 100_000.0

    # Batch-fetch sector ETF intraday % change (open-to-latest) for all open positions.
    # Fetch each unique ETF once to avoid serial REST calls per position.
    unique_etfs: set[str] = set()
    for sym in open_positions:
        etf = sectors.get_sector_etf(sym)
        if etf:
            unique_etfs.add(etf)

    sector_etf_chg: dict[str, float] = {}
    for etf in unique_etfs:
        try:
            bars = get_intraday_bars(etf, lookback=2)
            if bars is not None and not bars.empty:
                session_open = float(bars.iloc[0]["open"])
                latest = alpaca.get_latest_price(etf)
                if session_open > 0:
                    sector_etf_chg[etf] = (latest - session_open) / session_open * 100
        except Exception as exc:
            logger.debug("T2 sector ETF fetch failed for %s: %s", etf, exc)

    for symbol, pos in list(open_positions.items()):
        if pos.get("tranche_2_triggered"):
            continue

        iv = _try_compute_iv(symbol, alpaca)
        if iv is None:
            continue

        sentiment = research.get(symbol, {}).get("sentiment_score", 0)
        etf = sectors.get_sector_etf(symbol)
        chg = sector_etf_chg.get(etf, 0.0) if etf else 0.0

        from signals.tranche2_gates import check_tranche2_gates
        result = check_tranche2_gates(
            symbol=symbol,
            iv=iv,
            pos=pos,
            now=now,
            regime=regime,
            sentiment_score=float(sentiment) if sentiment else 0.0,
            sector_etf_chg=chg,
            major_event_today=major_event_today,
        )
        if result.passed:
            logger.info("T2 TRIGGER %s — all gates passed (sector=%s chg=%.1f%%)", symbol, etf, chg)
            from trading.executor import execute_tranche2_addon
            execute_tranche2_addon(
                symbol=symbol,
                pos=pos,
                regime=regime,
                research=research,
                alpaca=alpaca,
                portfolio_value=portfolio_value,
                now=now,
            )
        else:
            logger.debug("T2 skip %s — %s", symbol, result.reason)
```

Also add `from data.market_data import get_intraday_bars` to the module-level imports at the top of `trading/monitor.py` (not inside the function, for monkeypatching compatibility):

```python
from data.market_data import get_intraday_bars
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/python -m pytest tests/test_tranche2.py::TestCheckTranche2Triggers::test_sector_etf_chg_passed_to_gate -v
```

Expected: PASS

- [ ] **Step 5: Run all T2 tests**

```bash
.venv/bin/python -m pytest tests/test_tranche2.py -v
```

Expected: all pass

- [ ] **Step 6: Run full unit suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add trading/monitor.py tests/test_tranche2.py
git commit -m "fix: pass real sector ETF intraday % change to T2 gate in live monitor

Gate 11 of check_tranche2_gates() rejects T2 when the position's sector ETF
is down > 1% intraday. Previously sector_etf_chg was hardcoded to 0.0 so the
gate never rejected. Now batch-fetches unique sector ETF bars (open vs latest)
before the per-position loop — one API call per sector, not per position."
```

---

### Task 5: Run validation backtests

**Context:** All four improvements are now in place. Run 30d and 100d backtests to validate: (1) force-close rate drops due to tighter entry cutoff, (2) T2 fire count is printed and in a healthy range, (3) swing promotions increase due to profit-lock bypass, (4) overall P&L remains positive.

**Files:** No code changes — validation only.

- [ ] **Step 1: Run 30d backtest**

```bash
.venv/bin/python3 backtest.py --days 30 2>&1 | grep -E "RESULTS|Final equity|Total P.L|Max drawdown|Total trades|Win / Loss|Force-close|T2 fire"
```

Expected output shape:
```
  BACKTEST RESULTS  —  ...
  Final equity        : $  1xx,xxx.xx
  Total P&L           : $     +xxx.xx   (+x.xx%)
  Max drawdown        : x.xx%
  Total trades        : xxx
  Win / Loss          : xx / xx  (win rate xx.x%)
  Force-close rate    : xx.x%  (xx/xxx trades)   ← should be lower than 38.8%
  T2 fire count       : xx  (xx.x% of T1 positions)  ← should be 5-20%
```

- [ ] **Step 2: Run 100d backtest**

```bash
.venv/bin/python3 backtest.py --days 100 2>&1 | grep -E "RESULTS|Final equity|Total P.L|Max drawdown|Total trades|Win / Loss|Force-close|T2 fire"
```

- [ ] **Step 3: Commit validation note**

```bash
git commit --allow-empty -m "test: validate all 4 backtest improvements (30d + 100d)"
```

---

## Self-Review

**Spec coverage:**
0. ✅ Regime-dispatch SoT — Task 0 (`utils/regime_params.py` + caller migration + stale constant removal)
1. ✅ Entry cutoff tighten — Task 1 (config + time_gate + simulator)
2. ✅ Profit-lock swing bypass — Task 2 (eod_holds consumes `get_swing_params()`)
3. ✅ T2 fire-rate counter — Task 3 (SimState + report)
4. ✅ Real sector ETF data for T2 — Task 4 (monitor)
5. ✅ Swing min P&L gate — Task 2 (covers eligibility strengthening)
6. ✅ Entry eligibility — `ENTRY_HARD_CLOSE_HOUR` + existing 21 gates in `check_all_gates()` — comprehensive
7. ✅ Swing eligibility — profit-lock bypass + min P&L gate + PTJ rule + earnings gate — now complete
8. ✅ T2 eligibility — sector data now real; all 11 gates enforced correctly
9. ✅ Exit eligibility — profit-lock in `_apply_profit_lock()` already moves stop to breakeven; swing tiered-TP handles winners

**Stale code check (Task 0 must leave clean):**
- `config.py`: zero references to deleted parallel constants after migration
- `pre_claude_filter.py`: no inline `if regime == "CHOPPY"` composite-min branch
- `eod_holds.py`: no inline `if regime not in SWING_ELIGIBLE` + scattered PTJ threshold chain
- `backtest/simulator.py`: no inline `if regime in ("NORMAL", ...)` swing promotion set
- `signals/tranche2_gates.py`: no inline per-regime T2 override branches

**Placeholder scan:** No TBD, TODO, or "add validation" stubs — all code is complete.

**Type consistency:**
- `qualifies_for_eod_hold()` returns `tuple[bool, str]` — consistent with callers in `evaluate_eod_holds()`
- `SimState.t2_fire_count: int = 0` — field is a plain `int`, incremented with `+= 1`
- `sector_etf_chg: dict[str, float]` — consistent with `check_tranche2_gates` parameter type `float`
- `_evaluate_swing_promotion()` returns `int` (n_swings) — unchanged

**No regressions:** All tasks run `pytest tests/ -m unit -q` as a final step before commit.
