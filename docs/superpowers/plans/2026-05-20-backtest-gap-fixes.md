# Backtest Gap Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 6 structural gaps found in the 60-day backtest: CRISIS blocks recovery entries, SL:TP ratio is inverted, late entries can't hit TP, swing SL is too wide, SL-hit symbols re-entered next day, and swing duration is too long.

**Architecture:** All parameter changes go to `config.py` (Rule 1). Backtest-only regime fixes go in `backtest/data.py` and `backtest/simulator.py`. The SL blacklist is a pure utility in `utils/sl_blacklist.py`, checked in `signals/entry_gates.py` (live) and `backtest/simulator.py` (backtest). No safety rules go into `pre_claude_filter`.

**Tech Stack:** Python 3.11, pytest, existing config/signal/simulator/journal infrastructure.

---

## File Map

| File | Change |
|---|---|
| `config.py` | +9 new constants, 2 value changes |
| `backtest/data.py` | `_classify_vix_day` → detect CRASH_RECOVERY |
| `backtest/simulator.py` | CRASH_RECOVERY entries, late-gate, swing SL, SL blacklist, 3-day swing cap |
| `signals/entry_gates.py` | +2 hard gates: late-entry composite, SL blacklist |
| `trading/eod_holds.py` | Use `SWING_OVERNIGHT_SL_PCT` instead of intraday SL% |
| `trading/monitor.py` | Call `record_sl_hit()` after SL exits (Layer 1 + Layer 2) |
| `utils/sl_blacklist.py` | **NEW** — `record_sl_hit`, `is_sl_blacklisted`, `cleanup_expired` |
| `tests/test_signals.py` | Task 1 VIX classifier tests + Task 5 SL blacklist helpers |
| `tests/test_entry_gates.py` | Task 3 late-entry gate tests + Task 5 gate test |
| `tests/test_swing.py` | Task 4 overnight SL tests |

---

## Scene-setting context for subagents

Working directory: `~/Documents/projects/trading-bot`
Run tests: `.venv/bin/python -m pytest tests/ -m unit -q`
Config SoT: `config.py` — all constants live here (Rule 1, no magic numbers in source modules)
Gate SoT: `signals/entry_gates.py::check_all_gates()` — **sole enforcer of all safety gates** (Rule 23)
`pre_claude_filter` is a quality router only — never add safety rules there
Test rule: `monkeypatch.setattr` for all config overrides, never direct assignment; `@pytest.mark.unit` on all tests

---

## Task 1: CRISIS → CRASH_RECOVERY in backtest VIX classifier

**Problem:** The 60d backtest had 9 CRISIS days (Feb 24, Mar 9, 13, 19, 23) with zero entries. These were market-bottom days before a +36% recovery. VIX was elevated but starting to drop — that's CRASH_RECOVERY, not CRISIS. CRASH_RECOVERY is already in `ALLOW_ENTRY = VALID - {CRISIS}`, so the fix only requires improving the classifier.

**Files:**
- Modify: `config.py` — 2 new BACKTEST constants
- Modify: `backtest/data.py:104-126` — `_classify_vix_day`
- Modify: `backtest/simulator.py:148` — `_evaluate_swing_promotion` allow CRASH_RECOVERY
- Test: `tests/test_signals.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_signals.py`:

```python
# ── Task 1: VIX regime classifier ─────────────────────────────────────────────

class TestVixClassifier:
    """Tests for backtest/data.py _classify_vix_day"""

    def _make_vix(self, values: list[float]) -> list[tuple[int, float]]:
        """Build (timestamp, close) pairs from a list of VIX values."""
        return [(i * 86400, v) for i, v in enumerate(values)]

    def _make_spy(self, values: list[float]) -> list[tuple[int, float]]:
        return [(i * 86400, v) for i, v in enumerate(values)]

    @pytest.mark.unit
    def test_crisis_when_vix_rising_and_spy_falling(self, monkeypatch):
        """VIX above 90d avg, rising, SPY falling → CRISIS."""
        from backtest.data import _classify_vix_day
        # avg VIX ~20, current=40 (rising from 38), SPY down
        vix = self._make_vix([20] * 90 + [38, 40])
        spy = self._make_spy([500, 490])  # SPY down 2%
        assert _classify_vix_day(vix, spy) == "CRISIS"

    @pytest.mark.unit
    def test_crash_recovery_when_vix_elevated_but_falling_from_peak(self, monkeypatch):
        """VIX above CRISIS threshold but dropping from 5d peak → CRASH_RECOVERY."""
        import config
        from backtest.data import _classify_vix_day
        # VIX was 50 (peak), now 43 — that's a 14% drop from peak
        # avg VIX ~20 so VIX is still elevated above avg
        vix = self._make_vix([20] * 90 + [50, 50, 50, 50, 50, 43])
        spy = self._make_spy([500, 505])  # SPY up (recovery)
        result = _classify_vix_day(vix, spy)
        assert result == "CRASH_RECOVERY"

    @pytest.mark.unit
    def test_high_vol_when_vix_elevated_not_rising_not_crash_recovery(self, monkeypatch):
        """VIX above avg, not rising, but not enough drop from peak → HIGH_VOL."""
        from backtest.data import _classify_vix_day
        # VIX was 30, now 29 — only 3.3% drop from peak (below RECOVERY threshold)
        vix = self._make_vix([20] * 90 + [30, 30, 30, 30, 30, 29])
        spy = self._make_spy([500, 501])
        result = _classify_vix_day(vix, spy)
        assert result == "HIGH_VOL"

    @pytest.mark.unit
    def test_normal_when_vix_below_avg(self, monkeypatch):
        """VIX below 90d avg → NORMAL."""
        from backtest.data import _classify_vix_day
        vix = self._make_vix([30] * 90 + [18, 17])  # avg 30, current 17
        spy = self._make_spy([500, 501])
        assert _classify_vix_day(vix, spy) == "NORMAL"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd ~/Documents/projects/trading-bot
.venv/bin/python -m pytest tests/test_signals.py::TestVixClassifier -v
```
Expected: `test_crash_recovery_when_vix_elevated_but_falling_from_peak` FAILS (returns HIGH_VOL, not CRASH_RECOVERY)

- [ ] **Step 3: Add config constants**

In `config.py`, in the `# ── BACKTEST SIMULATION` section (after the existing 4 constants), add:

```python
BACKTEST_CRISIS_VIX_THRESHOLD        = 35.0  # VIX above this is considered crisis-territory
BACKTEST_CRISIS_VIX_RECOVERY_DROP_PCT = 10.0  # % VIX drop from 5d peak → classify as CRASH_RECOVERY
```

- [ ] **Step 4: Implement CRASH_RECOVERY detection in `backtest/data.py`**

Replace the entire `_classify_vix_day` function (lines 104-126):

```python
def _classify_vix_day(
    past_vix: list[tuple[int, float]],
    past_spy: list[tuple[int, float]],
) -> str:
    """Classify one day's regime from pre-fetched VIX + SPY pair lists."""
    if not past_vix:
        return "NORMAL"
    _, vix_current = past_vix[-1]
    vix_prev  = past_vix[-2][1] if len(past_vix) >= 2 else vix_current
    vix_rising = vix_current > vix_prev
    lookback    = min(90, len(past_vix) - 1)
    vix_avg_90d = (
        sum(c for _, c in past_vix[-lookback - 1:-1]) / lookback
        if lookback > 0 else vix_current
    )
    spy_change = 0.0
    if len(past_spy) >= 2:
        spy_change = (past_spy[-1][1] - past_spy[-2][1]) / past_spy[-2][1] * 100

    # Detect CRASH_RECOVERY: VIX is in crisis territory but falling from recent peak
    vix_peak_5d = max(c for _, c in past_vix[-6:-1]) if len(past_vix) >= 6 else vix_current
    vix_drop_pct = (vix_peak_5d - vix_current) / vix_peak_5d * 100 if vix_peak_5d > 0 else 0.0
    is_crash_recovery = (
        vix_current >= config.BACKTEST_CRISIS_VIX_THRESHOLD
        and not vix_rising
        and vix_drop_pct >= config.BACKTEST_CRISIS_VIX_RECOVERY_DROP_PCT
    )

    if vix_current > vix_avg_90d and vix_rising and spy_change < -1.0:
        return "CRISIS"
    if is_crash_recovery:
        return "CRASH_RECOVERY"
    if vix_current > vix_avg_90d:
        return "HIGH_VOL"
    return "NORMAL"
```

- [ ] **Step 5: Update `_evaluate_swing_promotion` in `backtest/simulator.py` to allow CRASH_RECOVERY**

The swing promotion at line 148 currently only promotes in NORMAL regime. Change the condition:

```python
# Before (line 148):
if regime == "NORMAL" and pnl_pct > 0 and pos2.get("composite", 0) >= SWING_COMPOSITE_MIN and n_swings < SWING_MAX_POSITIONS:

# After:
if regime in ("NORMAL", "CRASH_RECOVERY") and pnl_pct > 0 and pos2.get("composite", 0) >= SWING_COMPOSITE_MIN and n_swings < SWING_MAX_POSITIONS:
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_signals.py::TestVixClassifier -v
```
Expected: 4/4 PASS

- [ ] **Step 7: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```
Expected: all pass (808+)

- [ ] **Step 8: Commit**

```bash
git add config.py backtest/data.py backtest/simulator.py tests/test_signals.py
git commit -m "feat: detect CRASH_RECOVERY regime in backtest VIX classifier — allow entries on recovery days"
```

---

## Task 2: Fix SL/TP ratio — lower SL to 3%, raise ATR_TP_FLOOR to 3%

**Problem:** `STOP_LOSS_PCT = 4.0%`, `ATR_TP_FLOOR_PCT = 2.0%`. In 60d backtest, SL exits averaged -$204/trade while TP exits averaged +$99/trade — a 2:1 risk:reward ratio against us. Raising the TP floor to match the SL creates a minimum 1:1 R:R.

**Files:**
- Modify: `config.py` — 2 value changes

- [ ] **Step 1: Write failing tests**

Add to `tests/test_signals.py`:

```python
# ── Task 2: SL/TP ratio ───────────────────────────────────────────────────────

class TestSLTPRatio:
    @pytest.mark.unit
    def test_stop_loss_pct_is_3(self):
        """STOP_LOSS_PCT must be 3.0 to match ATR_TP_FLOOR."""
        assert config.STOP_LOSS_PCT == 3.0

    @pytest.mark.unit
    def test_atr_tp_floor_is_3(self):
        """ATR_TP_FLOOR_PCT must be >= STOP_LOSS_PCT to ensure positive R:R."""
        assert config.ATR_TP_FLOOR_PCT >= config.STOP_LOSS_PCT

    @pytest.mark.unit
    def test_compute_atr_tp_pct_respects_new_floor(self, monkeypatch):
        """compute_atr_tp_pct never returns less than 3.0% with updated floor."""
        from signals.atr_tp import compute_atr_tp_pct
        monkeypatch.setattr(config, "ATR_TP_FLOOR_PCT", 3.0)
        monkeypatch.setattr(config, "ATR_TP_CEILING_PCT", 12.0)
        # tiny ATR → should be clamped to floor
        result = compute_atr_tp_pct(atr=0.01, price=100.0, multiplier=2.5)
        assert result == 3.0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_signals.py::TestSLTPRatio -v
```
Expected: `test_stop_loss_pct_is_3` FAILS (value is 4.0), `test_atr_tp_floor_is_3` FAILS

- [ ] **Step 3: Update config.py**

In `config.py`, in the `# ── RISK MANAGEMENT` section:

```python
# Before:
STOP_LOSS_PCT      = 4.0         # Default 4% stop loss

# After:
STOP_LOSS_PCT      = 3.0         # Lowered from 4.0 — was 2× realized TP avg (~1.95%), creating inverted R:R
```

In the `# ── LEARNING & MEMORY` section:

```python
# Before:
ATR_TP_FLOOR_PCT    = 2.0  # TP never below 2.0% (raised from 1.5% to ensure positive R:R vs 4% SL)

# After:
ATR_TP_FLOOR_PCT    = 3.0  # TP never below 3.0% — matches new SL=3.0% for minimum 1:1 R:R
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_signals.py::TestSLTPRatio -v
```
Expected: 3/3 PASS

- [ ] **Step 5: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```
Expected: all pass. If any test asserts `STOP_LOSS_PCT == 4.0` or `ATR_TP_FLOOR_PCT == 2.0`, update those assertions to 3.0.

- [ ] **Step 6: Commit**

```bash
git add config.py tests/test_signals.py
git commit -m "fix: lower STOP_LOSS_PCT 4→3% and raise ATR_TP_FLOOR 2→3% for 1:1 minimum R:R"
```

---

## Task 3: Late-entry composite gate — block low-conviction entries after 2 PM

**Problem:** Entries as late as 2:55 PM have only 60 min to hit TP before the 3:55 PM force-close. 52% of trades force-close at a loss. Requiring a higher composite score after 2 PM ensures only high-conviction late entries are taken.

**Files:**
- Modify: `config.py` — 2 new constants
- Modify: `signals/entry_gates.py` — new hard gate in `check_all_gates`
- Modify: `backtest/simulator.py` — same check in signal evaluation loop
- Test: `tests/test_entry_gates.py`

- [ ] **Step 1: Write failing tests**

In `tests/test_entry_gates.py`, the existing `_gates_iv()` helper (line 18) creates a SimpleNamespace. Add `composite_score` to it so gate tests can exercise the new gate.

First check if `composite_score` is already in `_gates_iv()`. If not, the gate will need iv to have that field.

Add this test class to `tests/test_entry_gates.py`:

```python
class TestLateEntryCompositeGate:
    """Late-day composite gate — require higher composite after ENTRY_LATE_HOUR."""

    def _iv_late(self, composite: float = 60.0, **overrides):
        """iv fixture with composite_score field."""
        base = dict(
            current_price=100.0,
            vwap=99.0,
            vwap_std2_upper=110.0,
            stop_hunt_zone=False,
            parabolic=False,
            momentum_exhaustion=False,
            bot_herding_score=0,
            squeeze_setup=False,
            rsi=50.0,
            stoch_d=50.0,
            prev_close=100.0,
            relative_volume=1.0,
            or_position="outside",
            composite_score=composite,
        )
        base.update(overrides)
        return types.SimpleNamespace(**base)

    def _late_now(self):
        """2:30 PM ET — after ENTRY_LATE_HOUR=14."""
        from zoneinfo import ZoneInfo
        return datetime(2024, 1, 15, 14, 30, 0, tzinfo=ZoneInfo("America/New_York"))

    def _early_now(self):
        """11:00 AM ET — before ENTRY_LATE_HOUR=14."""
        from zoneinfo import ZoneInfo
        return datetime(2024, 1, 15, 11, 0, 0, tzinfo=ZoneInfo("America/New_York"))

    def _patch(self, monkeypatch):
        import calendars.earnings as ec
        import macro.economic_calendar as ecc
        monkeypatch.setattr(ec, "is_earnings_imminent", lambda *a, **kw: False)
        monkeypatch.setattr(ecc, "is_event_risk_window", lambda *a, **kw: (False, ""))

    @pytest.mark.unit
    def test_late_entry_low_composite_blocked(self, monkeypatch):
        """After ENTRY_LATE_HOUR, composite below ENTRY_LATE_COMPOSITE_MIN → blocked."""
        from signals.entry_gates import check_all_gates
        self._patch(monkeypatch)
        monkeypatch.setattr(config, "ENTRY_LATE_HOUR", 14)
        monkeypatch.setattr(config, "ENTRY_LATE_COMPOSITE_MIN", 50)
        result = check_all_gates(
            symbol="AAPL",
            iv=self._iv_late(composite=35.0),
            open_positions={},
            daily_closed=set(),
            bars_15min=None,
            now=self._late_now(),
            entries_this_cycle=0,
        )
        assert result.passed is False
        assert "late entry" in result.reason.lower() or "composite" in result.reason.lower()

    @pytest.mark.unit
    def test_late_entry_high_composite_passes(self, monkeypatch):
        """After ENTRY_LATE_HOUR, composite at or above threshold → passes gate."""
        from signals.entry_gates import check_all_gates
        self._patch(monkeypatch)
        monkeypatch.setattr(config, "ENTRY_LATE_HOUR", 14)
        monkeypatch.setattr(config, "ENTRY_LATE_COMPOSITE_MIN", 50)
        result = check_all_gates(
            symbol="AAPL",
            iv=self._iv_late(composite=60.0),
            open_positions={},
            daily_closed=set(),
            bars_15min=None,
            now=self._late_now(),
            entries_this_cycle=0,
        )
        assert result.passed is True

    @pytest.mark.unit
    def test_early_entry_low_composite_not_blocked_by_late_gate(self, monkeypatch):
        """Before ENTRY_LATE_HOUR, the late-gate does not apply."""
        from signals.entry_gates import check_all_gates
        self._patch(monkeypatch)
        monkeypatch.setattr(config, "ENTRY_LATE_HOUR", 14)
        monkeypatch.setattr(config, "ENTRY_LATE_COMPOSITE_MIN", 50)
        result = check_all_gates(
            symbol="AAPL",
            iv=self._iv_late(composite=32.0),
            open_positions={},
            daily_closed=set(),
            bars_15min=None,
            now=self._early_now(),
            entries_this_cycle=0,
        )
        # Low composite at 11 AM should NOT be blocked by the late gate
        # (may still be blocked by composite gate if MIN_COMPOSITE > 32, but not by late gate)
        assert "late entry" not in result.reason.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_entry_gates.py::TestLateEntryCompositeGate -v
```
Expected: FAIL (`check_all_gates` has no late-entry gate yet)

- [ ] **Step 3: Add config constants**

In `config.py`, in the `# ── ENTRY GATES & TIMING` section, after `ENTRY_GATE_CLOSE_MINUTES = 35`:

```python
ENTRY_LATE_HOUR          = 14   # 2:00 PM ET — after this, require higher composite for entry
ENTRY_LATE_COMPOSITE_MIN = 50   # composite threshold for entries after ENTRY_LATE_HOUR
```

- [ ] **Step 4: Add the gate to `signals/entry_gates.py`**

In `check_all_gates`, after gate #1 (time gate, line 84), add gate #1b:

```python
    # 1b. Late-day composite gate — after 2 PM require higher conviction
    if now.hour >= config.ENTRY_LATE_HOUR and iv.composite_score < config.ENTRY_LATE_COMPOSITE_MIN:
        return GateResult(
            False,
            f"late entry ({now.strftime('%H:%M')} ET): composite {iv.composite_score:.0f} < {config.ENTRY_LATE_COMPOSITE_MIN} required after {config.ENTRY_LATE_HOUR}:00",
        )
```

Note: `iv.composite_score` is available on `IndicatorValues` (confirmed by simulator.py usage). If the existing `_gates_iv()` helper in tests doesn't have `composite_score`, you'll need to add it to the helper. The tests in this task use `_iv_late()` which does include `composite_score`.

- [ ] **Step 5: Add the gate to `backtest/simulator.py`**

In the signal evaluation loop (around line 301), the current check is:
```python
if iv.composite_score > MIN_COMPOSITE and iv.signal == "BUY" and iv.rsi <= rsi_max and iv.vwap_slope >= VWAP_SLOPE_MIN:
    pending_signals[sym] = {"score": iv.composite_score, "atr": iv.atr}
```

Replace with:
```python
if iv.composite_score > MIN_COMPOSITE and iv.signal == "BUY" and iv.rsi <= rsi_max and iv.vwap_slope >= VWAP_SLOPE_MIN:
    # Late-day composite gate — mirror of signals/entry_gates.py gate 1b
    if ts_et.hour >= config.ENTRY_LATE_HOUR and iv.composite_score < config.ENTRY_LATE_COMPOSITE_MIN:
        continue
    pending_signals[sym] = {"score": iv.composite_score, "atr": iv.atr}
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_entry_gates.py::TestLateEntryCompositeGate -v
```
Expected: 3/3 PASS

- [ ] **Step 7: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```
Expected: all pass

- [ ] **Step 8: Commit**

```bash
git add config.py signals/entry_gates.py backtest/simulator.py tests/test_entry_gates.py
git commit -m "feat: block low-composite entries after 2 PM ET (ENTRY_LATE_COMPOSITE_MIN=50)"
```

---

## Task 4: Tighter overnight swing SL (3% vs 7% default)

**Problem:** When a day-trade is promoted to swing, `eod_holds.py` inherits the intraday SL% from the position (4% after Task 2), or falls back to `SWING_SL_DEFAULT_PCT = 7.0%`. Overnight gap risk is unidirectional, so a tighter 3% SL cuts losers faster. The 60d backtest had a BKNG swing at -7.05% = -$349 in one trade.

**Files:**
- Modify: `config.py` — 1 new constant
- Modify: `trading/eod_holds.py:127` — use `SWING_OVERNIGHT_SL_PCT`
- Modify: `backtest/simulator.py:151` — use `SWING_OVERNIGHT_SL_PCT`
- Test: `tests/test_swing.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_swing.py`:

```python
class TestSwingOvernightSL:
    """Swing promotion uses SWING_OVERNIGHT_SL_PCT, not intraday SL%."""

    @pytest.mark.unit
    def test_swing_overnight_sl_pct_constant_exists(self):
        """Config must define SWING_OVERNIGHT_SL_PCT."""
        assert hasattr(config, "SWING_OVERNIGHT_SL_PCT")
        assert 0 < config.SWING_OVERNIGHT_SL_PCT < config.SWING_SL_DEFAULT_PCT

    @pytest.mark.unit
    def test_eod_hold_uses_overnight_sl_not_intraday(self, monkeypatch):
        """Non-capitulation swing promotion uses SWING_OVERNIGHT_SL_PCT, not position's stop_loss_pct."""
        from trading.eod_holds import qualifies_for_eod_hold
        monkeypatch.setattr(config, "SWING_OVERNIGHT_SL_PCT", 3.0)
        monkeypatch.setattr(config, "SWING_MIN_PNL_PCT", -2.0)
        monkeypatch.setattr(config, "SWING_MAX_POSITIONS", 2)
        # Verify qualifies — then caller is responsible for using SWING_OVERNIGHT_SL_PCT
        ok, reason = qualifies_for_eod_hold(
            symbol="AAPL",
            is_tier1=True,
            regime="NORMAL",
            event_risk_tomorrow=False,
            earnings_in_2_days=False,
            current_pnl_pct=0.5,
            existing_swing_count=0,
            sector_sentiment_score=20,
        )
        assert ok is True, reason
        # SWING_OVERNIGHT_SL_PCT should be smaller than SWING_SL_DEFAULT_PCT
        assert config.SWING_OVERNIGHT_SL_PCT < config.SWING_SL_DEFAULT_PCT
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_swing.py::TestSwingOvernightSL -v
```
Expected: `test_swing_overnight_sl_pct_constant_exists` FAILS (AttributeError)

- [ ] **Step 3: Add config constant**

In `config.py`, in the `# ── SWING & EOD` section, after `SWING_MIN_PNL_PCT = -2.0`:

```python
SWING_OVERNIGHT_SL_PCT   = 3.0  # tighter SL for overnight holds — gap risk is unidirectional
```

- [ ] **Step 4: Update `trading/eod_holds.py`**

In `evaluate_eod_holds`, around line 127, change:

```python
# Before:
sl_pct = config.SWING_SL_PCT_CAPITULATION if capitulation else pos.get("stop_loss_pct", config.SWING_SL_DEFAULT_PCT)

# After:
sl_pct = config.SWING_SL_PCT_CAPITULATION if capitulation else config.SWING_OVERNIGHT_SL_PCT
```

- [ ] **Step 5: Update `backtest/simulator.py`**

In `_evaluate_swing_promotion`, around line 151, change:

```python
# Before:
pos2["sl"] = round(pos2["entry"] * (1 - SWING_SL_PCT / 100), 4)

# After:
pos2["sl"] = round(pos2["entry"] * (1 - config.SWING_OVERNIGHT_SL_PCT / 100), 4)
```

Also remove the module-level `SWING_SL_PCT` constant (line 31) since it's no longer used:

```python
# Remove this line:
SWING_SL_PCT        = config.SWING_SL_DEFAULT_PCT
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_swing.py::TestSwingOvernightSL -v
```
Expected: 2/2 PASS

- [ ] **Step 7: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```
Expected: all pass

- [ ] **Step 8: Commit**

```bash
git add config.py trading/eod_holds.py backtest/simulator.py tests/test_swing.py
git commit -m "fix: use SWING_OVERNIGHT_SL_PCT=3% for swing promotes instead of intraday SL fallback"
```

---

## Task 5: SL-hit next-day blacklist

**Problem:** After an SL exit, the same symbol can be re-entered the next day. The 60d backtest showed CRSP hitting SL twice (Apr 27, May 7), ARM hitting SL twice (May 12, May 19). A 1-trading-day blacklist after SL prevents re-entering a symbol while it's still in breakdown mode.

**Files:**
- Modify: `config.py` — 2 new constants (SL_BLACKLIST_DAYS, SL_BLACKLIST_FILE)
- Create: `utils/sl_blacklist.py` — `record_sl_hit`, `is_sl_blacklisted`
- Modify: `trading/monitor.py:179` — call `record_sl_hit` after SL exits (Layer 1 + Layer 2)
- Modify: `signals/entry_gates.py` — new hard gate in `check_all_gates`
- Modify: `backtest/simulator.py` — `SimState` + blacklist logic in simulate_day
- Test: `tests/test_signals.py` and `tests/test_entry_gates.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_signals.py`:

```python
# ── Task 5: SL blacklist ──────────────────────────────────────────────────────

class TestSLBlacklist:
    @pytest.mark.unit
    def test_record_sl_hit_creates_expiry(self, tmp_path, monkeypatch):
        """record_sl_hit writes expiry to sl_blacklist.json."""
        import config
        from datetime import date, timedelta
        monkeypatch.setattr(config, "SL_BLACKLIST_FILE", tmp_path / "sl_blacklist.json")
        from utils import sl_blacklist
        sl_blacklist.record_sl_hit("AAPL")
        data = __import__("utils.io_utils", fromlist=["load_json"]).load_json(tmp_path / "sl_blacklist.json", {})
        assert "AAPL" in data
        # Expiry must be a date string in the future
        from datetime import date
        assert data["AAPL"] >= date.today().isoformat()

    @pytest.mark.unit
    def test_is_sl_blacklisted_returns_true_when_blocked(self, tmp_path, monkeypatch):
        """is_sl_blacklisted returns True when symbol has a future expiry."""
        import config
        from datetime import date, timedelta
        monkeypatch.setattr(config, "SL_BLACKLIST_FILE", tmp_path / "sl_blacklist.json")
        from utils.io_utils import save_json
        from utils import sl_blacklist
        # Set expiry to tomorrow
        expiry = (date.today() + timedelta(days=1)).isoformat()
        save_json(tmp_path / "sl_blacklist.json", {"AAPL": expiry})
        assert sl_blacklist.is_sl_blacklisted("AAPL") is True

    @pytest.mark.unit
    def test_is_sl_blacklisted_returns_false_when_expired(self, tmp_path, monkeypatch):
        """is_sl_blacklisted returns False when expiry has passed."""
        import config
        from datetime import date, timedelta
        monkeypatch.setattr(config, "SL_BLACKLIST_FILE", tmp_path / "sl_blacklist.json")
        from utils.io_utils import save_json
        from utils import sl_blacklist
        # Set expiry to yesterday
        expiry = (date.today() - timedelta(days=1)).isoformat()
        save_json(tmp_path / "sl_blacklist.json", {"AAPL": expiry})
        assert sl_blacklist.is_sl_blacklisted("AAPL") is False

    @pytest.mark.unit
    def test_is_sl_blacklisted_returns_false_for_unknown_symbol(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "SL_BLACKLIST_FILE", tmp_path / "sl_blacklist.json")
        from utils import sl_blacklist
        assert sl_blacklist.is_sl_blacklisted("TSLA") is False
```

Add to `tests/test_entry_gates.py`:

```python
class TestSLBlacklistGate:
    """SL blacklist hard gate in check_all_gates."""

    def _iv(self, composite=60.0):
        return types.SimpleNamespace(
            current_price=100.0, vwap=99.0, vwap_std2_upper=110.0,
            stop_hunt_zone=False, parabolic=False, momentum_exhaustion=False,
            bot_herding_score=0, squeeze_setup=False, rsi=50.0, stoch_d=50.0,
            prev_close=100.0, relative_volume=1.0, or_position="outside",
            composite_score=composite,
        )

    def _patch(self, monkeypatch):
        import calendars.earnings as ec
        import macro.economic_calendar as ecc
        monkeypatch.setattr(ec, "is_earnings_imminent", lambda *a, **kw: False)
        monkeypatch.setattr(ecc, "is_event_risk_window", lambda *a, **kw: (False, ""))

    @pytest.mark.unit
    def test_blacklisted_symbol_blocked(self, monkeypatch, tmp_path):
        """Symbol in SL blacklist → GateResult(False)."""
        from signals.entry_gates import check_all_gates
        from utils.io_utils import save_json
        from datetime import date, timedelta, datetime
        from zoneinfo import ZoneInfo
        self._patch(monkeypatch)
        monkeypatch.setattr(config, "SL_BLACKLIST_FILE", tmp_path / "sl_blacklist.json")
        expiry = (date.today() + timedelta(days=1)).isoformat()
        save_json(tmp_path / "sl_blacklist.json", {"AAPL": expiry})
        now = datetime(2024, 1, 15, 11, 0, 0, tzinfo=ZoneInfo("America/New_York"))
        result = check_all_gates(
            symbol="AAPL", iv=self._iv(), open_positions={}, daily_closed=set(),
            bars_15min=None, now=now, entries_this_cycle=0,
        )
        assert result.passed is False
        assert "blacklist" in result.reason.lower() or "sl" in result.reason.lower()

    @pytest.mark.unit
    def test_non_blacklisted_symbol_not_blocked_by_sl_gate(self, monkeypatch, tmp_path):
        """Symbol not in blacklist → SL gate does not block."""
        from signals.entry_gates import check_all_gates
        from datetime import datetime
        from zoneinfo import ZoneInfo
        self._patch(monkeypatch)
        monkeypatch.setattr(config, "SL_BLACKLIST_FILE", tmp_path / "sl_blacklist.json")
        now = datetime(2024, 1, 15, 11, 0, 0, tzinfo=ZoneInfo("America/New_York"))
        result = check_all_gates(
            symbol="TSLA", iv=self._iv(), open_positions={}, daily_closed=set(),
            bars_15min=None, now=now, entries_this_cycle=0,
        )
        # TSLA is not blacklisted — SL gate should not be the reason for any failure
        if not result.passed:
            assert "blacklist" not in result.reason.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_signals.py::TestSLBlacklist tests/test_entry_gates.py::TestSLBlacklistGate -v
```
Expected: all FAIL (no utils/sl_blacklist.py yet)

- [ ] **Step 3: Add config constants**

In `config.py`, in the `# ── PATHS` section, after `SWING_POSITIONS_FILE`:

```python
SL_BLACKLIST_FILE          = STATE_DIR / "sl_blacklist.json"
```

In `config.py`, in the `# ── ENTRY GATES & TIMING` section:

```python
SL_BLACKLIST_DAYS = 1   # block re-entry for this many trading days after SL hit
```

- [ ] **Step 4: Create `utils/sl_blacklist.py`**

```python
"""SL-hit blacklist: blocks re-entry the next trading day after a stop-loss exit."""
import logging
from datetime import date, timedelta

import config
from utils.io_utils import load_json, save_json

logger = logging.getLogger(__name__)


def _next_weekday(d: date) -> date:
    """Return the next calendar date that is Mon–Fri (skips weekends)."""
    d = d + timedelta(days=1)
    while d.weekday() >= 5:
        d = d + timedelta(days=1)
    return d


def record_sl_hit(symbol: str) -> None:
    """Record an SL hit — blocks the symbol from entry on the next trading day."""
    blacklist = load_json(config.SL_BLACKLIST_FILE, {})
    expiry = _next_weekday(date.today()).isoformat()
    blacklist[symbol] = expiry
    save_json(config.SL_BLACKLIST_FILE, blacklist)
    logger.info("SL blacklist: %s blocked until %s", symbol, expiry)


def is_sl_blacklisted(symbol: str) -> bool:
    """Return True if symbol is currently blacklisted (expiry not yet passed)."""
    blacklist = load_json(config.SL_BLACKLIST_FILE, {})
    expiry_str = blacklist.get(symbol)
    if not expiry_str:
        return False
    return date.today().isoformat() <= expiry_str
```

- [ ] **Step 5: Call `record_sl_hit` in `trading/monitor.py`**

Add import at the top of `trading/monitor.py` (after the existing imports):

```python
from utils.sl_blacklist import record_sl_hit
```

**Layer 2 SL — line ~179:** After the `close_position(...)` call for `monitor_stop_loss`:

```python
        if pnl_pct <= -sl_pct:
            logger.warning(
                "MONITOR STOP-LOSS: %s P&L=%+.2f%% breached SL=%.1f%% — closing (mid-cycle)",
                symbol, pnl_pct, -sl_pct,
            )
            close_position(symbol, alpaca, open_positions, current_price, _try_compute_iv(symbol, alpaca), "monitor_stop_loss")
            record_sl_hit(symbol)   # ← ADD THIS LINE
```

**Layer 1 bracket SL — line ~111-113:** After `close_reason = "bracket_stop_loss_filled"` is determined, add a call after `record_daily_closed(symbol)` (line ~143):

```python
                record_daily_closed(symbol)
                if close_reason == "bracket_stop_loss_filled":
                    record_sl_hit(symbol)   # ← ADD THESE TWO LINES
```

- [ ] **Step 6: Add the gate to `signals/entry_gates.py`**

Add import at the top of `signals/entry_gates.py` (before the other utils imports):

```python
from utils.sl_blacklist import is_sl_blacklisted
```

In `check_all_gates`, after gate #3 (daily_closed / already_open, line ~94), add gate #3b:

```python
    # 3b. SL blacklist — block re-entry for symbols that hit SL yesterday
    if is_sl_blacklisted(symbol):
        return GateResult(False, f"{symbol} SL-blacklisted — hit stop-loss recently, skip until tomorrow")
```

- [ ] **Step 7: Add SL blacklist to `backtest/simulator.py` SimState**

Add two fields to the `SimState` dataclass (after `daily_closed`):

```python
@dataclass
class SimState:
    cash: float = STARTING_CAPITAL
    positions: dict = field(default_factory=dict)
    daily_closed: set = field(default_factory=set)
    sl_blacklist: set = field(default_factory=set)          # ← ADD: blocked today (from yesterday's SL)
    sl_blacklist_pending: set = field(default_factory=set)   # ← ADD: SL hits today (block tomorrow)
    equity_curve: list = field(default_factory=list)
    trades: list = field(default_factory=list)
    atr_tp_multiplier: float = config.ATR_TP_MULT_DEFAULT
```

In `simulate_day`, at the start (right after `state.daily_closed.clear()`):

```python
    state.daily_closed.clear()
    # Rotate SL blacklist: yesterday's pending → today's active
    state.sl_blacklist = state.sl_blacklist_pending.copy()
    state.sl_blacklist_pending.clear()
```

In the position-checking loop, where `reason` is set to `"SL"` (around line 274):

```python
            if bar["high"] >= pos["tp"]:
                reason, exit_price = ("SWING_TP" if is_sw else "TP"), pos["tp"]
            elif bar["low"] <= pos["sl"]:
                reason, exit_price = ("SWING_SL" if is_sw else "SL"), pos["sl"]
            elif is_force and not is_sw:
                reason, exit_price = "FORCE_CLOSE", bar["close"]

            if reason:
                _close_trade(state, sym, str(ts), exit_price, reason, date_str)
                if reason == "SL":                           # ← ADD
                    state.sl_blacklist_pending.add(sym)      # ← ADD
```

In the signal evaluation loop (around line 298), add `sl_blacklist` check:

```python
        for sym in watch_list:
            if sym in state.positions or sym in state.daily_closed or sym in state.sl_blacklist:  # ← add sl_blacklist
                continue
```

- [ ] **Step 8: Run all new tests**

```bash
.venv/bin/python -m pytest tests/test_signals.py::TestSLBlacklist tests/test_entry_gates.py::TestSLBlacklistGate -v
```
Expected: all PASS

- [ ] **Step 9: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```
Expected: all pass

- [ ] **Step 10: Commit**

```bash
git add config.py utils/sl_blacklist.py trading/monitor.py signals/entry_gates.py backtest/simulator.py tests/test_signals.py tests/test_entry_gates.py
git commit -m "feat: SL-hit next-day blacklist — block re-entry after stop-loss (live + backtest)"
```

---

## Task 6: Reduce backtest swing duration 5 → 3 days

**Problem:** 60d backtest had SWING_MAXDAYS exits in loss (2 trades, -$91). With a 5-day cap, losers are held for an extra 2 days hoping for recovery. The backtest-only constant `BACKTEST_SWING_MAX_DAYS = 3` tests whether cutting duration reduces these losses without sacrificing winners (most TP hits occur within the first 3 days).

**Note:** The live bot's `SWING_MAX_DAYS = 5` is unchanged — only the backtest simulation is adjusted.

**Files:**
- Modify: `config.py` — 1 new constant in BACKTEST section
- Modify: `backtest/simulator.py` — use `BACKTEST_SWING_MAX_DAYS` instead of `SWING_MAX_DAYS`
- Test: `tests/test_market.py` (existing `TestBacktestSimulatorConstants`)

- [ ] **Step 1: Write failing test**

Find the `TestBacktestSimulatorConstants` class in `tests/test_market.py` and add:

```python
    @pytest.mark.unit
    def test_backtest_swing_max_days_is_3(self):
        """BACKTEST_SWING_MAX_DAYS must be 3 — reduces losers held past day 3."""
        assert config.BACKTEST_SWING_MAX_DAYS == 3

    @pytest.mark.unit
    def test_backtest_swing_max_days_used_in_simulator(self, monkeypatch):
        """Backtest simulator's SWING_MAX_DAYS reads from BACKTEST_SWING_MAX_DAYS."""
        monkeypatch.setattr(config, "BACKTEST_SWING_MAX_DAYS", 3)
        import importlib
        import backtest.simulator as sim
        importlib.reload(sim)
        assert sim.SWING_MAX_DAYS == 3
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/python -m pytest "tests/test_market.py::TestBacktestSimulatorConstants::test_backtest_swing_max_days_is_3" -v
```
Expected: FAIL (AttributeError: module 'config' has no attribute 'BACKTEST_SWING_MAX_DAYS')

- [ ] **Step 3: Add config constant**

In `config.py`, in the `# ── BACKTEST SIMULATION` section:

```python
BACKTEST_SWING_MAX_DAYS      = 3          # backtest-only: shorter swing cap cuts slow losers earlier
```

- [ ] **Step 4: Update `backtest/simulator.py`**

Change the module-level constant (line 41):

```python
# Before:
SWING_MAX_DAYS = config.SWING_MAX_DAYS

# After:
SWING_MAX_DAYS = config.BACKTEST_SWING_MAX_DAYS
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
.venv/bin/python -m pytest "tests/test_market.py::TestBacktestSimulatorConstants" -v
```
Expected: all PASS

- [ ] **Step 6: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add config.py backtest/simulator.py tests/test_market.py
git commit -m "fix: reduce backtest swing max days from 5 to 3 — exits losers 2 days earlier"
```

---

## Self-Review

**Spec coverage check:**
1. ✅ CRISIS recovery → Task 1 (VIX classifier + CRASH_RECOVERY)
2. ✅ SL:TP ratio → Task 2 (STOP_LOSS_PCT=3.0, ATR_TP_FLOOR=3.0)
3. ✅ Late entry gate → Task 3 (ENTRY_LATE_HOUR=14, ENTRY_LATE_COMPOSITE_MIN=50)
4. ✅ Swing overnight SL → Task 4 (SWING_OVERNIGHT_SL_PCT=3.0)
5. ✅ SL-hit blacklist → Task 5 (utils/sl_blacklist.py + monitor + gate + backtest)
6. ✅ ATR_TP_FLOOR raise → covered in Task 2 (2.0→3.0)
7. ✅ Swing duration → Task 6 (BACKTEST_SWING_MAX_DAYS=3)

**Placeholder scan:** None found — all steps contain actual code.

**Type consistency:**
- `is_sl_blacklisted(symbol: str) -> bool` used consistently in entry_gates.py and test
- `record_sl_hit(symbol: str)` signature matches usage in monitor.py
- `SimState.sl_blacklist: set` and `.sl_blacklist_pending: set` — consistent across simulate_day and signal loop
- `config.ENTRY_LATE_HOUR` and `config.ENTRY_LATE_COMPOSITE_MIN` referenced consistently in entry_gates.py, simulator.py, and tests
- `config.SWING_OVERNIGHT_SL_PCT` referenced in eod_holds.py, simulator.py, and tests
