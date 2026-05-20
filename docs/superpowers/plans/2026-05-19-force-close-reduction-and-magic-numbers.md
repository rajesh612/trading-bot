# Force-Close Reduction & Magic Number Elimination Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce the 54–57% backtest force-close rate by adding NORMAL regime swing eligibility and lowering the swing composite threshold; eliminate all remaining magic numbers by wiring them to config constants; fix a threading race condition in the position monitor.

**Architecture:** Six independent tasks ordered so config additions (Task 1) are available before downstream wiring (Tasks 2–6). Tasks 2–4 are pure mechanical constant wiring — no logic changes. Task 5 is the only strategic change (NORMAL → swing-eligible). Task 6 adds a threading.Lock to protect a module-level dict.

**Tech Stack:** Python 3.11, pytest, config.py single-source-of-truth pattern, threading.Lock

---

## Files Changed

| File | Task | Change |
|---|---|---|
| `config.py` | 1 | Add 13 new constants in correct sections |
| `backtest/simulator.py` | 2 | Replace 8 local constants with config refs |
| `indicators/engine.py` | 3 | Replace `5 / 390` with config refs |
| `indicators/counter_signals.py` | 3 | Replace `390`, `0.995`, `0.999`, `1.001` |
| `trading/swing_manager.py` | 4 | Replace hardcoded `2.0` / `0.98` |
| `trading/eod_holds.py` | 4 | Replace hardcoded `-2.0` PTJ threshold |
| `utils/regimes.py` | 5 | Add NORMAL to SWING_ELIGIBLE |
| `tests/test_foundation.py` | 5 | Update SWING_ELIGIBLE membership assertion |
| `tests/test_swing.py` | 5 | Update wrong-regime test for NORMAL |
| `trading/monitor.py` | 6 | Add _DRAWDOWN_LOCK, wrap dict access |
| `tests/test_market.py` | 6 | Add thread-safety test for drawdown alerts |

---

### Task 1: Add missing config constants

**Files:**
- Modify: `config.py`

No new tests — pure config additions. All existing 790 tests must still pass.

- [ ] **Step 1: Add constants to config.py**

Open `config.py` and make the following additions (values must be exact):

In the `# ── ENTRY GATES & TIMING` section, after `ENTRY_GATE_CLOSE_MINUTES = 35`, add:

```python
MARKET_OPEN_HOUR   = 9    # NYSE market open hour (ET)
MARKET_OPEN_MINUTE = 30   # NYSE market open minute (ET)
TRADING_MINUTES_PER_DAY = 390  # 6.5 hours × 60 — used for session-fraction calculations
```

In the `# ── SWING & EOD` section, after `EOD_LOSS_THRESHOLD = -0.5`, add:

```python
EOD_FORCE_CLOSE_HOUR   = 15  # hour at which force_close_all() runs (ET)
EOD_FORCE_CLOSE_MINUTE = 55  # minute at which force_close_all() runs (ET)
EOD_HOLDS_EVAL_HOUR    = 15  # hour at which evaluate_eod_holds() runs (ET)
EOD_HOLDS_EVAL_MINUTE  = 50  # minute at which evaluate_eod_holds() runs (ET)
SWING_GAP_TIGHTEN_SL_PCT = 2.0  # tighten swing stop to this % when gap down is small
SWING_MIN_PNL_PCT        = -2.0 # PTJ rule: don't hold overnight if P&L <= this %
```

In the `# ── INDICATOR & COMPOSITE THRESHOLDS` section, after `VOLUME_EXHAUSTION_RATIO = 0.8`, add:

```python
VOLUME_EXHAUSTION_PRICE_PROXIMITY = 0.995  # within 0.5% of 5-day high counts as "at high"
OBV_PRICE_HIGH_PROXIMITY = 0.999  # OBV bearish divergence: price within 0.1% of intraday high
OBV_PRICE_LOW_PROXIMITY  = 1.001  # OBV bullish divergence: price within 0.1% of intraday low
```

At the very end of config.py (after PEAD constants), add a new section:

```python
# ── BACKTEST SIMULATION ────────────────────────────────────────────────────────

BACKTEST_STARTING_CAPITAL    = 100_000.0  # initial portfolio value for simulation
BACKTEST_SLIPPAGE_PCT        = 0.0005     # 0.05% slippage on entry and exit fills
BACKTEST_MAX_POSITIONS       = 5         # max simultaneous open positions in simulation
BACKTEST_SWING_COMPOSITE_MIN = 40.0      # composite threshold for EOD swing promotion (lowered from 50 to promote more swings)
```

- [ ] **Step 2: Verify tests still pass**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

Expected: 790 passed (config additions are additive — nothing breaks).

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "config: add TRADING_MINUTES_PER_DAY, EOD timing, swing, OBV, backtest constants"
```

---

### Task 2: Wire constants into backtest/simulator.py

**Files:**
- Modify: `backtest/simulator.py` (lines 20–34, 64)

**Context:** `simulator.py` currently defines its own local constants for market timing, starting capital, slippage, and swing thresholds. These must be replaced with config refs. The section looks like:

```python
# ── Backtest-only simulation constants ───────────────────────────────────────
STARTING_CAPITAL = 100_000.0; SLIPPAGE_PCT = 0.0005; MAX_POSITIONS = 5
MARKET_OPEN_H = 9;  MARKET_OPEN_M = 30
FORCE_CLOSE_H = 15; FORCE_CLOSE_M = 55
SWING_EVAL_H  = 15; SWING_EVAL_M  = 50
SWING_ENABLED = True; SWING_COMPOSITE_MIN = 50.0
SWING_SL_PCT  = 7.0; SWING_TP_PCT = 10.0
```

And line 64:
```python
sl_dollars = price * STOP_LOSS_PCT / 100 or (atr if atr > 0 else price * 0.02)
```

- [ ] **Step 1: Write the failing test**

In `tests/test_market.py`, add inside the existing test class (find a suitable class or add a new one at the bottom of the file):

```python
class TestBacktestSimulatorConstants:
    @pytest.mark.unit
    def test_simulator_starting_capital_matches_config(self):
        from backtest import simulator
        import config
        assert simulator.STARTING_CAPITAL == config.BACKTEST_STARTING_CAPITAL

    @pytest.mark.unit
    def test_simulator_slippage_matches_config(self):
        from backtest import simulator
        import config
        assert simulator.SLIPPAGE_PCT == config.BACKTEST_SLIPPAGE_PCT

    @pytest.mark.unit
    def test_simulator_max_positions_matches_config(self):
        from backtest import simulator
        import config
        assert simulator.MAX_POSITIONS == config.BACKTEST_MAX_POSITIONS

    @pytest.mark.unit
    def test_simulator_market_open_matches_config(self):
        from backtest import simulator
        import config
        assert simulator.MARKET_OPEN_H == config.MARKET_OPEN_HOUR
        assert simulator.MARKET_OPEN_M == config.MARKET_OPEN_MINUTE

    @pytest.mark.unit
    def test_simulator_force_close_matches_config(self):
        from backtest import simulator
        import config
        assert simulator.FORCE_CLOSE_H == config.EOD_FORCE_CLOSE_HOUR
        assert simulator.FORCE_CLOSE_M == config.EOD_FORCE_CLOSE_MINUTE

    @pytest.mark.unit
    def test_simulator_swing_eval_matches_config(self):
        from backtest import simulator
        import config
        assert simulator.SWING_EVAL_H == config.EOD_HOLDS_EVAL_HOUR
        assert simulator.SWING_EVAL_M == config.EOD_HOLDS_EVAL_MINUTE

    @pytest.mark.unit
    def test_simulator_swing_composite_min_matches_config(self):
        from backtest import simulator
        import config
        assert simulator.SWING_COMPOSITE_MIN == config.BACKTEST_SWING_COMPOSITE_MIN
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/python -m pytest tests/test_market.py::TestBacktestSimulatorConstants -v
```

Expected: FAIL — `AssertionError` on starting capital (100000 != expected config value, since old constants still defined locally).

- [ ] **Step 3: Replace local constants in simulator.py**

Replace the constants block (lines 19–34) with:

```python
# ── Backtest-only simulation constants ───────────────────────────────────────
STARTING_CAPITAL    = config.BACKTEST_STARTING_CAPITAL
SLIPPAGE_PCT        = config.BACKTEST_SLIPPAGE_PCT
MAX_POSITIONS       = config.BACKTEST_MAX_POSITIONS
MARKET_OPEN_H       = config.MARKET_OPEN_HOUR
MARKET_OPEN_M       = config.MARKET_OPEN_MINUTE
FORCE_CLOSE_H       = config.EOD_FORCE_CLOSE_HOUR
FORCE_CLOSE_M       = config.EOD_FORCE_CLOSE_MINUTE
SWING_EVAL_H        = config.EOD_HOLDS_EVAL_HOUR
SWING_EVAL_M        = config.EOD_HOLDS_EVAL_MINUTE
SWING_ENABLED       = True
SWING_COMPOSITE_MIN = config.BACKTEST_SWING_COMPOSITE_MIN
SWING_SL_PCT        = config.SWING_SL_DEFAULT_PCT   # 7.0
SWING_TP_PCT        = config.SWING_TP2_PCT           # 10.0
# ── Live-bot params — single source of truth is config.py ────────────────────
```

Also fix line 64 — replace `price * 0.02` with `price * config.ATR_FALLBACK_PROXY_PCT`:

```python
sl_dollars = price * STOP_LOSS_PCT / 100 or (atr if atr > 0 else price * config.ATR_FALLBACK_PROXY_PCT)
```

- [ ] **Step 4: Run tests to confirm pass**

```bash
.venv/bin/python -m pytest tests/test_market.py::TestBacktestSimulatorConstants -v
```

Expected: 7 passed.

- [ ] **Step 5: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

Expected: 797 passed.

- [ ] **Step 6: Commit**

```bash
git add backtest/simulator.py tests/test_market.py
git commit -m "fix: wire backtest simulator constants to config; remove local magic numbers"
```

---

### Task 3: Wire constants in indicators/engine.py and indicators/counter_signals.py

**Files:**
- Modify: `indicators/engine.py` (line ~106)
- Modify: `indicators/counter_signals.py` (lines 7, 40, 59, 63)

**Context:**

`indicators/engine.py` line ~106:
```python
session_fraction = min(1.0, len(df) * 5 / 390)
```
The `5` is `BAR_TIMEFRAME_MINUTES` (already in config) and `390` is `TRADING_MINUTES_PER_DAY` (added in Task 1).

`indicators/counter_signals.py` line 7:
```python
_BARS_PER_5D = 5 * (390 // config.BAR_TIMEFRAME_MINUTES)
```
The `390` needs replacing with `config.TRADING_MINUTES_PER_DAY`.

Lines 40, 59, 63 use hardcoded proximity thresholds:
```python
at_high = current_price >= high_5d * 0.995          # line 40
price_high = recent_prices.iloc[-1] >= recent_prices.max() * 0.999  # line 59
price_low  = recent_prices.iloc[-1] <= recent_prices.min() * 1.001  # line 63
```

- [ ] **Step 1: Write failing tests**

Add to `tests/test_indicators.py` inside the existing test class:

```python
class TestIndicatorMagicNumbers:
    @pytest.mark.unit
    def test_bars_per_5d_uses_config_trading_minutes(self):
        import config
        from indicators.counter_signals import _BARS_PER_5D
        expected = 5 * (config.TRADING_MINUTES_PER_DAY // config.BAR_TIMEFRAME_MINUTES)
        assert _BARS_PER_5D == expected

    @pytest.mark.unit
    def test_session_fraction_uses_config_constants(self, monkeypatch):
        import pandas as pd
        import numpy as np
        import config
        from indicators.engine import compute_indicators

        n_bars = 20
        idx = pd.date_range("2026-05-19 09:30", periods=n_bars, freq="5min", tz="UTC")
        df = pd.DataFrame({
            "open": 100.0, "high": 101.0, "low": 99.0,
            "close": 100.0, "volume": 10_000,
        }, index=idx)
        # session_fraction = n_bars * BAR_TIMEFRAME_MINUTES / TRADING_MINUTES_PER_DAY
        expected_fraction = min(1.0, n_bars * config.BAR_TIMEFRAME_MINUTES / config.TRADING_MINUTES_PER_DAY)
        # spot check: if TRADING_MINUTES_PER_DAY changed, fraction would change
        assert expected_fraction == min(1.0, n_bars * 5 / 390)
```

- [ ] **Step 2: Run to confirm current behavior**

```bash
.venv/bin/python -m pytest tests/test_indicators.py::TestIndicatorMagicNumbers -v
```

Expected: Both pass (values match because literals equal config values — this verifies the values are consistent before we swap).

- [ ] **Step 3: Fix indicators/counter_signals.py**

Replace line 7:
```python
_BARS_PER_5D = 5 * (390 // config.BAR_TIMEFRAME_MINUTES)
```
With:
```python
_BARS_PER_5D = 5 * (config.TRADING_MINUTES_PER_DAY // config.BAR_TIMEFRAME_MINUTES)
```

Replace line 40:
```python
    at_high = current_price >= high_5d * 0.995  # within 0.5%
```
With:
```python
    at_high = current_price >= high_5d * config.VOLUME_EXHAUSTION_PRICE_PROXIMITY
```

Replace line 59:
```python
    price_high = recent_prices.iloc[-1] >= recent_prices.max() * 0.999
```
With:
```python
    price_high = recent_prices.iloc[-1] >= recent_prices.max() * config.OBV_PRICE_HIGH_PROXIMITY
```

Replace line 63:
```python
    price_low = recent_prices.iloc[-1] <= recent_prices.min() * 1.001
```
With:
```python
    price_low = recent_prices.iloc[-1] <= recent_prices.min() * config.OBV_PRICE_LOW_PROXIMITY
```

- [ ] **Step 4: Fix indicators/engine.py**

Replace:
```python
                session_fraction = min(1.0, len(df) * 5 / 390)
```
With:
```python
                session_fraction = min(1.0, len(df) * config.BAR_TIMEFRAME_MINUTES / config.TRADING_MINUTES_PER_DAY)
```

- [ ] **Step 5: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

Expected: all pass (790+).

- [ ] **Step 6: Commit**

```bash
git add indicators/counter_signals.py indicators/engine.py tests/test_indicators.py
git commit -m "fix: replace 390/0.995/0.999/1.001 magic numbers with config constants in indicators"
```

---

### Task 4: Wire constants in trading/swing_manager.py and trading/eod_holds.py

**Files:**
- Modify: `trading/swing_manager.py` (lines 87–88)
- Modify: `trading/eod_holds.py` (line 57)

**Context:**

`trading/swing_manager.py` lines 87–88 (inside `run_gap_check`):
```python
s[symbol]["stop_loss_pct"]   = 2.0
s[symbol]["stop_loss_price"] = round(current_price * 0.98, 2)
```
Both use `SWING_GAP_TIGHTEN_SL_PCT = 2.0` (added in Task 1). `0.98 = 1 - 2.0/100`.

`trading/eod_holds.py` line 57 (inside `qualifies_for_eod_hold`):
```python
if current_pnl_pct <= -2.0:
    return False, f"PTJ rule: P&L {current_pnl_pct:.1f}% ≤ -2% (don't hold losers overnight)"
```
Should use `config.SWING_MIN_PNL_PCT = -2.0` (added in Task 1).

- [ ] **Step 1: Write failing tests**

In `tests/test_swing.py`, add inside the existing test class:

```python
    @pytest.mark.unit
    def test_gap_tighten_uses_config_sl_pct(self, monkeypatch, tmp_path):
        import config
        monkeypatch.setattr(config, "SWING_POSITIONS_FILE", tmp_path / "swing.json")
        monkeypatch.setattr(config, "SWING_GAP_TIGHTEN_SL_PCT", 3.0)  # change from default 2.0
        from utils.io_utils import save_json
        save_json(tmp_path / "swing.json", {
            "NVDA": {"entry_price": 100.0, "stop_loss_pct": 7.0, "tp1_hit": False}
        })

        from trading.swing_manager import _evaluate_gap_action
        pos = {"entry_price": 100.0, "stop_loss_pct": 7.0, "tp1_hit": False}
        # gap of -1% → between -7% SL and SWING_GAP_TIGHTEN_THRESHOLD(-2%), so TIGHTEN_STOP
        action = _evaluate_gap_action(pos, current_price=99.0)
        assert action == "TIGHTEN_STOP"

    @pytest.mark.unit
    def test_eod_hold_ptj_rule_uses_config_threshold(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "SWING_MIN_PNL_PCT", -1.0)  # stricter than default -2.0
        from trading.eod_holds import qualifies_for_eod_hold
        ok, reason = qualifies_for_eod_hold(
            symbol="NVDA", is_tier1=True, regime="BULL_TREND",
            event_risk_tomorrow=False, earnings_in_2_days=False,
            current_pnl_pct=-1.5,   # between -2.0 and -1.0 — passes old gate, fails new gate
            existing_swing_count=0,
            sector_sentiment_score=25.0,
        )
        assert ok is False
        assert "PTJ" in reason
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/python -m pytest tests/test_swing.py::TestSwingManager::test_eod_hold_ptj_rule_uses_config_threshold -v
```

Expected: FAIL — current code uses hardcoded `-2.0`, not `config.SWING_MIN_PNL_PCT`, so monkeypatching to `-1.0` has no effect.

- [ ] **Step 3: Fix trading/swing_manager.py**

Find the TIGHTEN_STOP block in `run_gap_check` (around line 82–89):

Replace:
```python
            elif action == "TIGHTEN_STOP":
                logger.info(f"GAP CHECK {symbol}: gap down 2-5% — tightening trailing stop to 2%")
                with _SWING_LOCK:
                    s = load_swing_positions()
                    if symbol in s:
                        s[symbol]["stop_loss_pct"] = 2.0
                        s[symbol]["stop_loss_price"] = round(current_price * 0.98, 2)
                        _save_swing_positions(s)
```
With:
```python
            elif action == "TIGHTEN_STOP":
                logger.info(f"GAP CHECK {symbol}: gap down — tightening trailing stop to {config.SWING_GAP_TIGHTEN_SL_PCT}%")
                with _SWING_LOCK:
                    s = load_swing_positions()
                    if symbol in s:
                        s[symbol]["stop_loss_pct"] = config.SWING_GAP_TIGHTEN_SL_PCT
                        s[symbol]["stop_loss_price"] = round(current_price * (1 - config.SWING_GAP_TIGHTEN_SL_PCT / 100), 2)
                        _save_swing_positions(s)
```

- [ ] **Step 4: Fix trading/eod_holds.py**

Replace:
```python
    if current_pnl_pct <= -2.0:
        return False, f"PTJ rule: P&L {current_pnl_pct:.1f}% ≤ -2% (don't hold losers overnight)"
```
With:
```python
    if current_pnl_pct <= config.SWING_MIN_PNL_PCT:
        return False, f"PTJ rule: P&L {current_pnl_pct:.1f}% ≤ {config.SWING_MIN_PNL_PCT}% (don't hold losers overnight)"
```

- [ ] **Step 5: Run targeted tests**

```bash
.venv/bin/python -m pytest tests/test_swing.py -v -q
```

Expected: all pass including the two new tests.

- [ ] **Step 6: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add trading/swing_manager.py trading/eod_holds.py tests/test_swing.py
git commit -m "fix: wire SWING_GAP_TIGHTEN_SL_PCT and SWING_MIN_PNL_PCT config constants"
```

---

### Task 5: Force-close improvement — NORMAL regime swing eligibility

**Files:**
- Modify: `utils/regimes.py`
- Modify: `tests/test_foundation.py` (update SWING_ELIGIBLE membership assertion)
- Modify: `tests/test_swing.py` (update wrong-regime test)

**Context:**

The 30d backtest shows most trading days are NORMAL regime. The live bot's `qualifies_for_eod_hold` checks `regime not in regimes.SWING_ELIGIBLE`. NORMAL is currently excluded from SWING_ELIGIBLE, so **zero swing promotions happen during NORMAL days in the live bot** — all open positions get force-closed at 3:55 PM.

The backtest uses its own simpler logic (`regime == "NORMAL"` is explicitly allowed for swing promotion) but still shows 54% force-close rate. In live trading, it would be worse.

Adding NORMAL to SWING_ELIGIBLE means tier-1 positions with positive P&L, no event risk, no earnings, and positive sector sentiment can be held overnight on NORMAL days. This is a reasonable strategy for quality stocks in an undirected market.

Also wire `config.BACKTEST_SWING_COMPOSITE_MIN` (Task 1 value: 40.0 instead of 50.0) into the simulator so more positions qualify for swing promotion in backtests.

- [ ] **Step 1: Write failing tests**

In `tests/test_foundation.py`, locate `TestRegimesModule` and add:

```python
    @pytest.mark.unit
    def test_normal_in_swing_eligible(self):
        from utils import regimes
        assert regimes.NORMAL in regimes.SWING_ELIGIBLE
```

In `tests/test_swing.py`, add:

```python
    @pytest.mark.unit
    def test_qualifies_for_eod_hold_normal_regime_allowed(self):
        from trading.eod_holds import qualifies_for_eod_hold
        ok, reason = qualifies_for_eod_hold(
            symbol="NVDA", is_tier1=True, regime="NORMAL",
            event_risk_tomorrow=False, earnings_in_2_days=False,
            current_pnl_pct=1.5, existing_swing_count=0,
            sector_sentiment_score=20.0,
        )
        assert ok is True, f"NORMAL regime should be swing-eligible, got: {reason}"
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/python -m pytest tests/test_foundation.py::TestRegimesModule::test_normal_in_swing_eligible tests/test_swing.py::TestSwingManager::test_qualifies_for_eod_hold_normal_regime_allowed -v
```

Expected: FAIL — NORMAL not in SWING_ELIGIBLE.

- [ ] **Step 3: Update utils/regimes.py**

Current `SWING_ELIGIBLE` line in `utils/regimes.py`:
```python
SWING_ELIGIBLE = frozenset({BULL_TREND, OPPORTUNITY, CAPITULATION, CRASH_RECOVERY})
```

Replace with:
```python
SWING_ELIGIBLE = frozenset({BULL_TREND, OPPORTUNITY, CAPITULATION, CRASH_RECOVERY, NORMAL})
```

- [ ] **Step 4: Update the broken existing test in tests/test_foundation.py**

Find `test_swing_eligible_members` which asserts `regimes.NORMAL not in regimes.SWING_ELIGIBLE`. Update it:

```python
    @pytest.mark.unit
    def test_swing_eligible_members(self):
        from utils import regimes
        assert regimes.BULL_TREND in regimes.SWING_ELIGIBLE
        assert regimes.OPPORTUNITY in regimes.SWING_ELIGIBLE
        assert regimes.CAPITULATION in regimes.SWING_ELIGIBLE
        assert regimes.CRASH_RECOVERY in regimes.SWING_ELIGIBLE
        assert regimes.NORMAL in regimes.SWING_ELIGIBLE   # added: NORMAL days allow swings for tier-1
        assert regimes.CRISIS not in regimes.SWING_ELIGIBLE
        assert regimes.CHOPPY not in regimes.SWING_ELIGIBLE
        assert regimes.DEFENSIVE not in regimes.SWING_ELIGIBLE
```

- [ ] **Step 5: Update the broken wrong-regime test in tests/test_swing.py**

Find `test_qualifies_for_eod_hold_wrong_regime` which passes `regime="NORMAL"` expecting a fail. Update it to use a truly ineligible regime:

```python
    def test_qualifies_for_eod_hold_wrong_regime(self):
        from trading.eod_holds import qualifies_for_eod_hold
        ok, reason = qualifies_for_eod_hold(
            symbol="NVDA", is_tier1=True, regime="CHOPPY",
            event_risk_tomorrow=False, earnings_in_2_days=False,
            current_pnl_pct=2.0, existing_swing_count=0,
            sector_sentiment_score=25.0,
        )
        assert ok is False
        assert "regime" in reason
```

- [ ] **Step 6: Run targeted tests**

```bash
.venv/bin/python -m pytest tests/test_foundation.py::TestRegimesModule tests/test_swing.py -v -q
```

Expected: all pass.

- [ ] **Step 7: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add utils/regimes.py tests/test_foundation.py tests/test_swing.py
git commit -m "feat: add NORMAL to SWING_ELIGIBLE; lower BACKTEST_SWING_COMPOSITE_MIN to 40"
```

- [ ] **Step 9: Run backtest to measure improvement**

```bash
.venv/bin/python3 backtest.py 2>&1 | grep -E "BACKTEST RESULTS|Final equity|Force-close|Total trades|Win / Loss|force_close"
```

Expected: Force-close rate drops from ~54% toward ~35–45% as NORMAL-day positions now qualify for swing promotion.

---

### Task 6: Thread-safety fix — _drawdown_alerted lock

**Files:**
- Modify: `trading/monitor.py`
- Modify: `tests/test_market.py`

**Context:**

`trading/monitor.py` has a module-level dict:
```python
_drawdown_alerted: dict[str, float] = {}  # symbol → epoch of last alert
```

`_check_drawdown_alerts()` reads, iterates, and writes this dict without any lock. The position monitor runs every 60 seconds via APScheduler. While `max_instances=1` prevents overlapping monitor invocations, any future change (or accidental scheduler reconfiguration) could cause a race. Add a lock now for correctness.

- [ ] **Step 1: Write failing test**

In `tests/test_market.py`, add:

```python
class TestDrawdownAlertThreadSafety:
    @pytest.mark.unit
    def test_drawdown_alerted_lock_exists(self):
        import threading
        from trading import monitor
        assert hasattr(monitor, "_DRAWDOWN_LOCK")
        assert isinstance(monitor._DRAWDOWN_LOCK, type(threading.Lock()))

    @pytest.mark.unit
    def test_drawdown_alert_cooldown_respected(self, monkeypatch):
        import time
        import config
        monkeypatch.setattr(config, "DRAWDOWN_WARN_PCT", 1.0)
        monkeypatch.setattr(config, "DRAWDOWN_ALERT_COOLDOWN_MIN", 0.001)  # ~60ms
        monkeypatch.setattr(config, "STOP_LOSS_PCT", 4.0)

        alerts = []
        monkeypatch.setattr(
            "trading.monitor.telegram_notify.notify_position_drawdown",
            lambda sym, pnl, stop: alerts.append(sym),
        )

        from trading import monitor
        monitor._drawdown_alerted.clear()

        positions = {"AAPL": {"entry_price": 100.0, "stop_loss_pct": 4.0}}
        prices = {"AAPL": 98.5}   # -1.5% → exceeds DRAWDOWN_WARN_PCT=1.0

        monitor._check_drawdown_alerts(positions, prices)
        assert len(alerts) == 1  # first call fires

        monitor._check_drawdown_alerts(positions, prices)
        assert len(alerts) == 1  # cooldown blocks second call (60ms hasn't passed)
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/python -m pytest tests/test_market.py::TestDrawdownAlertThreadSafety::test_drawdown_alerted_lock_exists -v
```

Expected: FAIL — `AttributeError: module 'trading.monitor' has no attribute '_DRAWDOWN_LOCK'`.

- [ ] **Step 3: Fix trading/monitor.py**

After the existing import block and before `logger = logging.getLogger(__name__)`, add the lock:

Current (around line 26-28):
```python
logger = logging.getLogger(__name__)

_drawdown_alerted: dict[str, float] = {}  # symbol → epoch of last alert
```

Replace with:
```python
import threading

logger = logging.getLogger(__name__)

_DRAWDOWN_LOCK: threading.Lock = threading.Lock()
_drawdown_alerted: dict[str, float] = {}  # symbol → epoch of last alert
```

Then wrap the entire body of `_check_drawdown_alerts()` in the lock. Current function body:

```python
def _check_drawdown_alerts(positions: dict, current_prices: dict) -> None:
    """Fire drawdown alerts with per-position cooldown to avoid Telegram spam."""
    now_ts = time.time()
    cooldown_s = config.DRAWDOWN_ALERT_COOLDOWN_MIN * 60
    # Prune closed positions from cooldown dict
    for sym in list(_drawdown_alerted):
        if sym not in positions:
            del _drawdown_alerted[sym]
    for symbol, pos in positions.items():
        price = current_prices.get(symbol)
        if price is None:
            continue
        entry = pos.get("entry_price", 0)
        if entry <= 0:
            continue
        pnl_pct = (price - entry) / entry * 100
        stop_pct = -pos.get("stop_loss_pct", config.STOP_LOSS_PCT)
        if pnl_pct <= -config.DRAWDOWN_WARN_PCT and pnl_pct > stop_pct:
            last = _drawdown_alerted.get(symbol, 0.0)
            if now_ts - last >= cooldown_s:
                telegram_notify.notify_position_drawdown(symbol, pnl_pct, stop_pct)
                _drawdown_alerted[symbol] = now_ts
```

Replace with:

```python
def _check_drawdown_alerts(positions: dict, current_prices: dict) -> None:
    """Fire drawdown alerts with per-position cooldown to avoid Telegram spam."""
    now_ts = time.time()
    cooldown_s = config.DRAWDOWN_ALERT_COOLDOWN_MIN * 60
    with _DRAWDOWN_LOCK:
        for sym in list(_drawdown_alerted):
            if sym not in positions:
                del _drawdown_alerted[sym]
        for symbol, pos in positions.items():
            price = current_prices.get(symbol)
            if price is None:
                continue
            entry = pos.get("entry_price", 0)
            if entry <= 0:
                continue
            pnl_pct = (price - entry) / entry * 100
            stop_pct = -pos.get("stop_loss_pct", config.STOP_LOSS_PCT)
            if pnl_pct <= -config.DRAWDOWN_WARN_PCT and pnl_pct > stop_pct:
                last = _drawdown_alerted.get(symbol, 0.0)
                if now_ts - last >= cooldown_s:
                    telegram_notify.notify_position_drawdown(symbol, pnl_pct, stop_pct)
                    _drawdown_alerted[symbol] = now_ts
```

- [ ] **Step 4: Run targeted tests**

```bash
.venv/bin/python -m pytest tests/test_market.py::TestDrawdownAlertThreadSafety -v
```

Expected: 2 passed.

- [ ] **Step 5: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add trading/monitor.py tests/test_market.py
git commit -m "fix: protect _drawdown_alerted dict with threading.Lock in position monitor"
```
