# Plan 2: Risk & Execution Hardening

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix budget rollover TOCTOU race, profit-lock concurrent-close loss, fill-slippage timeout handling, swing qualification race, ATR-aware trailing stop, and progressive-loss in-cycle application.

**Architecture:** All fixes are surgical — no structural changes. Budget rollover uses a journal-lock pattern for atomicity. Monitor profit-lock holds the positions lock across the entire read-mutate-write. Swing qualification re-reads position count after each promotion. Trailing stop uses ATR multiplier (1.5×) instead of 3% hardcode.

**Tech Stack:** Python 3.11, pytest, threading.Lock (existing _POSITIONS_LOCK)

---

## File Map

| File | Change |
|---|---|
| `trading/budget_guard.py` | Atomic rollover via lock; rollover sentinel as separate key outside mutable budget |
| `trading/risk_manager.py` | Rollover read-modify-write under BUDGET_LOCK; fill-timeout returns explicit sentinel |
| `trading/monitor.py` | Hold _POSITIONS_LOCK across alpaca-check + disk-write in profit-lock flush |
| `trading/tiered_tp.py` | Replace 0.97 hardcode with ATR-aware trailing stop (ATR × 1.5) |
| `trading/eod_holds.py` | Re-read swing count after each promotion; add regime-shift CRISIS guard |
| `trading/position_sizer.py` | Apply progressive-loss within same cycle via in-memory loss count |
| `trading/journal.py` | Add BUDGET_LOCK; expose `count_losses_since()` for intra-cycle use |
| `tests/test_trading_budget.py` | Tests for rollover atomicity, fill-timeout, budget-lock |
| `tests/test_trading_lifecycle.py` | Tests for profit-lock race, swing race, trailing stop |

---

### Task 1: Budget rollover — atomic read-modify-write

The bug: `try_rollover_exit_to_entry()` reads budget, modifies in memory, saves twice (once in `apply_exit_rollover`, once in caller). A concurrent cycle write between the two saves wipes `_rollover_applied=True`. Fix: hold a module-level lock for the entire rollover operation.

**Files:**
- Modify: `trading/risk_manager.py`
- Modify: `trading/budget_guard.py`
- Modify: `trading/journal.py` (add BUDGET_LOCK export)

- [ ] **Step 1: Write failing test**

```python
# tests/test_trading_budget.py — add
import threading, pytest
from unittest.mock import patch, MagicMock
import config

@pytest.mark.unit
def test_rollover_not_applied_twice(monkeypatch, tmp_path):
    """Rollover must only apply once even under concurrent writes."""
    monkeypatch.setattr(config, "API_BUDGET_FILE", tmp_path / "budget.json")
    from utils.io_utils import save_json
    from trading import risk_manager

    # Seed a budget where exit pool has $0.10 unused
    budget = {
        "date": "2026-05-16", "calls_made": 2, "entry_calls": 1, "exit_calls": 1,
        "input_tokens": 100, "output_tokens": 50,
        "cache_write_tokens": 0, "cache_read_tokens": 0,
        "estimated_cost_usd": 0.15, "entry_spent_usd": 0.10, "exit_spent_usd": 0.05,
        "ceiling_usd": 1.00,
        "entry_ceiling_usd": 0.70, "exit_ceiling_usd": 0.30,
        "ceiling_mode": "normal",
        "entry_exhausted": False, "exit_exhausted": False,
        "budget_exhausted": False, "escalated": False,
    }
    save_json(config.API_BUDGET_FILE, budget)

    # Call rollover twice concurrently
    results = []
    def do_rollover():
        results.append(risk_manager.try_rollover_exit_to_entry())

    t1 = threading.Thread(target=do_rollover)
    t2 = threading.Thread(target=do_rollover)
    t1.start(); t2.start()
    t1.join(); t2.join()

    # Only one rollover should have applied
    applied = [r for r in results if r is True]
    assert len(applied) <= 1, f"Rollover applied {len(applied)} times — expected ≤1"
```

- [ ] **Step 2: Run to confirm failure (or flaky pass)**

```
.venv/bin/python -m pytest tests/test_trading_budget.py -k "rollover_not_applied_twice" -v
```

- [ ] **Step 3: Add BUDGET_LOCK to journal.py**

```python
# trading/journal.py — add after existing locks
import threading
BUDGET_LOCK = threading.Lock()
```

- [ ] **Step 4: Update risk_manager.py — hold BUDGET_LOCK across rollover**

Import and use the lock:

```python
from trading.journal import _POSITIONS_LOCK, BUDGET_LOCK, append_trade
```

Wrap `try_rollover_exit_to_entry()` with the lock:

```python
def try_rollover_exit_to_entry() -> bool:
    """Roll a fraction of unused exit budget into entry pool. Called once at 12:30 PM.
    Thread-safe: holds BUDGET_LOCK for entire read-modify-write."""
    with BUDGET_LOCK:
        b = load_budget()
        if b.get("_rollover_applied"):
            return False  # idempotent guard inside the lock
        exit_spent = b.get("exit_spent_usd", 0.0)
        exit_ceil  = b.get("exit_ceiling_usd", round(b["ceiling_usd"] * config.EXIT_BUDGET_RATIO, 4))
        unused_exit = max(0.0, exit_ceil - exit_spent)
        if unused_exit < 0.01:
            return False
        rollover = min(unused_exit * config.BUDGET_ROLLOVER_FRACTION, config.EXIT_ROLLOVER_MAX_USD)
        b["entry_ceiling_usd"] = round(b.get("entry_ceiling_usd", 0.0) + rollover, 4)
        b["_rollover_applied"] = True
        _save_budget(b)
        logger.info("Budget rollover: $%.4f unused exit → entry ceiling now $%.4f", rollover, b["entry_ceiling_usd"])
        return True
```

- [ ] **Step 5: Run test**

```
.venv/bin/python -m pytest tests/test_trading_budget.py -k "rollover_not_applied_twice" -v
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 6: Commit**

```bash
git add trading/risk_manager.py trading/budget_guard.py trading/journal.py tests/test_trading_budget.py
git commit -m "fix: budget rollover TOCTOU — hold BUDGET_LOCK across entire read-modify-write"
```

---

### Task 2: Profit-lock — hold _POSITIONS_LOCK across check + write

**Files:**
- Modify: `trading/monitor.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_trading_lifecycle.py — add
import pytest
from unittest.mock import patch, MagicMock
import config

@pytest.mark.unit
def test_profit_lock_not_lost_on_concurrent_close(monkeypatch, tmp_path):
    """flush_profit_lock_updates must log a warning if symbol was closed concurrently."""
    monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "positions.json")
    from utils.io_utils import save_json
    from trading.monitor import flush_profit_lock_updates

    # Symbol not on disk (simulates concurrent close)
    save_json(config.OPEN_POSITIONS_FILE, {})

    warnings = []
    with patch("trading.monitor.logger") as mock_logger:
        flush_profit_lock_updates({"AAPL": {"entry_price": 100, "profit_locked": True}})
        for call in mock_logger.warning.call_args_list:
            warnings.append(str(call))

    assert any("AAPL" in w or "concurrent" in w.lower() or "not found" in w.lower() for w in warnings), \
        "Expected warning about concurrently closed position"
```

- [ ] **Step 2: Run to confirm failure**

```
.venv/bin/python -m pytest tests/test_trading_lifecycle.py -k "profit_lock_not_lost" -v
```

- [ ] **Step 3: Update trading/monitor.py flush_profit_lock_updates()**

Find `flush_profit_lock_updates()` and update to warn when symbol is missing from disk:

```python
def flush_profit_lock_updates(updates: dict) -> None:
    """Persist in-memory profit-lock mutations to disk under the positions lock.
    Logs a warning if a symbol was concurrently closed between check and flush.
    """
    if not updates:
        return
    with _POSITIONS_LOCK:
        disk = load_json(config.OPEN_POSITIONS_FILE, {})
        for sym, pos in updates.items():
            if sym in disk:
                disk[sym] = pos
            else:
                logger.warning(
                    "flush_profit_lock_updates: %s not found on disk — likely closed concurrently; "
                    "profit-lock update discarded.", sym
                )
        save_json(config.OPEN_POSITIONS_FILE, disk)
```

- [ ] **Step 4: Run tests**

```
.venv/bin/python -m pytest tests/test_trading_lifecycle.py -k "profit_lock" -v
.venv/bin/python -m pytest tests/ -m unit -q
```
---

### Task 3: ATR-aware trailing stop

Replace the hardcoded 3% trailing stop in `tiered_tp.py` with an ATR × 1.5 distance, matching the entry sizing philosophy.

**Files:**
- Modify: `trading/tiered_tp.py`
- Modify: `config.py` (add `TRAILING_STOP_ATR_MULT`)

- [ ] **Step 1: Write failing test**

```python
# tests/test_trading_lifecycle.py — add
import pytest
import config

@pytest.mark.unit
def test_trailing_stop_uses_atr_not_hardcode(monkeypatch):
    """compute_trailing_stop_price must use ATR × TRAILING_STOP_ATR_MULT, not 0.97 hardcode."""
    monkeypatch.setattr(config, "TRAILING_STOP_ATR_MULT", 1.5)
    from trading.tiered_tp import compute_trailing_stop_price

    high_water = 110.0
    atr = 2.0
    result = compute_trailing_stop_price(high_water=high_water, atr=atr)
    expected = high_water - (atr * config.TRAILING_STOP_ATR_MULT)
    assert abs(result - expected) < 0.01, f"Expected {expected}, got {result}"

@pytest.mark.unit
def test_trailing_stop_falls_back_to_3pct_without_atr(monkeypatch):
    """Without ATR, trailing stop uses 3% fallback."""
    from trading.tiered_tp import compute_trailing_stop_price
    high_water = 110.0
    result = compute_trailing_stop_price(high_water=high_water, atr=None)
    expected = high_water * 0.97
    assert abs(result - expected) < 0.01
```

- [ ] **Step 2: Add config constant**

```python
# config.py — add near tiered_tp section
TRAILING_STOP_ATR_MULT = 1.5   # trailing stop distance = ATR × this; 3% fallback if ATR unavailable
```

- [ ] **Step 3: Update tiered_tp.py**

Find the trailing stop calculation (currently `high_water * 0.97`) and replace with:

```python
def compute_trailing_stop_price(high_water: float, atr: float | None) -> float:
    """ATR-aware trailing stop. Falls back to 3% if ATR is unavailable."""
    import config
    if atr and atr > 0:
        return high_water - (atr * config.TRAILING_STOP_ATR_MULT)
    return high_water * 0.97
```

Update all callers of the trailing stop in `tiered_tp.py` and `swing_manager.py` to pass the position's ATR (stored as `entry_indicators.atr` in the position dict).

In `swing_manager.py`, where trailing stop is applied:
```python
atr = pos.get("entry_indicators", {}).get("atr")
new_stop = compute_trailing_stop_price(high_water=pos["high_water_price"], atr=atr)
```

- [ ] **Step 4: Run tests**

```
.venv/bin/python -m pytest tests/test_trading_lifecycle.py -k "trailing_stop" -v
.venv/bin/python -m pytest tests/ -m unit -q
```
---

### Task 4: Swing qualification — no race on SWING_MAX_POSITIONS

**Files:**
- Modify: `trading/eod_holds.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_trading_lifecycle.py — add
import pytest
from unittest.mock import patch, MagicMock
import config

@pytest.mark.unit
def test_swing_max_positions_enforced_strictly(monkeypatch, tmp_path):
    """At most SWING_MAX_POSITIONS=2 should be promoted even if 3 qualify simultaneously."""
    monkeypatch.setattr(config, "SWING_MAX_POSITIONS", 2)
    monkeypatch.setattr(config, "SWING_POSITIONS_FILE", tmp_path / "swing.json")
    monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "positions.json")
    from utils.io_utils import save_json
    save_json(config.SWING_POSITIONS_FILE, {})

    # This test requires reading eod_holds logic — verify count is re-read per promotion
    from trading.eod_holds import _promote_to_swing
    promoted = []
    for i in range(3):
        result = _promote_to_swing(
            symbol=f"SYM{i}",
            position={"entry_price": 100.0, "qty": 10, "mode_at_entry": "DAY_TRADE",
                      "regime_at_entry": "NORMAL", "entry_composite_score": 30.0},
            pnl_pct=0.8,
        )
        if result:
            promoted.append(f"SYM{i}")

    assert len(promoted) <= config.SWING_MAX_POSITIONS, \
        f"Too many swing positions promoted: {promoted}"
```

- [ ] **Step 2: Update eod_holds.py — add _promote_to_swing with re-read guard**

Extract the promotion logic into a function that re-reads the swing file inside a lock:

```python
from trading.journal import _POSITIONS_LOCK

def _promote_to_swing(symbol: str, position: dict, pnl_pct: float) -> bool:
    """Promote a day-trade position to swing. Re-reads swing count inside lock to prevent race."""
    with _POSITIONS_LOCK:
        swing_positions = load_json(config.SWING_POSITIONS_FILE, {})
        if len(swing_positions) >= config.SWING_MAX_POSITIONS:
            logger.info("SWING SKIP %s — swing limit %d reached (re-read)", symbol, config.SWING_MAX_POSITIONS)
            return False
        swing_positions[symbol] = {**position, "swing_promoted_at": datetime.now(ET).isoformat(), "pnl_at_promote_pct": pnl_pct}
        save_json(config.SWING_POSITIONS_FILE, swing_positions)
        return True
```

Replace the inline promotion block in `evaluate_eod_holds()` with a call to `_promote_to_swing()`.

- [ ] **Step 3: Run tests**

```
.venv/bin/python -m pytest tests/test_trading_lifecycle.py -k "swing_max" -v
.venv/bin/python -m pytest tests/ -m unit -q
```
---

### Task 5: Fill-slippage timeout — explicit sentinel return

**Files:**
- Modify: `trading/risk_manager.py`
- Modify: `trading/executor.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_trading_budget.py — add
import pytest
from unittest.mock import MagicMock, patch
import config

@pytest.mark.unit
def test_correct_bracket_timeout_returns_explicit_none(monkeypatch):
    """correct_bracket_after_fill returns None on timeout; caller must log it."""
    monkeypatch.setattr(config, "FILL_POLL_ATTEMPTS", 2)
    monkeypatch.setattr(config, "FILL_POLL_INTERVAL_S", 0.01)

    fake_order = MagicMock()
    fake_order.status = "accepted"  # never becomes "filled"
    fake_order.filled_avg_price = None

    fake_trading = MagicMock()
    fake_trading.get_order_by_id.return_value = fake_order

    from trading.risk_manager import correct_bracket_after_fill
    result = correct_bracket_after_fill(fake_trading, "ord-123", 100.0, 4.0, 5.0)
    assert result is None

@pytest.mark.unit
def test_executor_logs_warning_on_slippage_timeout(monkeypatch, tmp_path, caplog):
    """execute_buy logs a warning when fill-slippage correction times out."""
    import logging
    monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "positions.json")
    monkeypatch.setattr(config, "DAILY_CLOSED_FILE", tmp_path / "closed.json")
    monkeypatch.setattr("notifications.telegram.notify_buy", lambda **kw: None)
    monkeypatch.setattr("notifications.telegram.notify_daily_loss_warning", lambda *a: None)
    monkeypatch.setattr("trading.risk_manager.correct_bracket_after_fill", lambda *a, **kw: None)
    monkeypatch.setattr("trading.executor.append_trade", lambda d: None)

    alpaca_mock = MagicMock()
    alpaca_mock.place_bracket_order.return_value = {
        "id": "ord1", "stop_loss_price": 96.0, "take_profit_price": 105.0
    }

    from trading.executor import execute_buy
    from tests.test_trading_pipeline import _make_iv  # reuse helper

    with caplog.at_level(logging.WARNING, logger="trading.executor"):
        execute_buy(
            symbol="AAPL", iv=_make_iv(composite_score=30.0),
            decision={"action": "BUY", "confidence": 80, "stop_loss_pct": 4.0, "take_profit_pct": 5.0},
            regime="NORMAL", regime_info={}, research={},
            open_positions={}, alpaca=alpaca_mock,
            portfolio_value=100_000, pos_mult=1.0,
            spy_ret5d=0.0, symbol_ret5d=0.0, rsi_max=72.0,
            params={}, now=__import__("datetime").datetime.now(__import__("zoneinfo").ZoneInfo("America/New_York")).replace(hour=11),
            entries_this_cycle=0, daily_pnl_pct=0.0,
            sector_today={}, symbol_sector_etf=None,
        )

    assert any("slippage" in r.message.lower() or "fill" in r.message.lower()
               for r in caplog.records), "Expected fill-slippage warning in logs"
```

- [ ] **Step 2: Update executor.py — log warning when correction returns None**

After the `correct_bracket_after_fill()` call in `execute_buy()`:

```python
    corrected = risk_manager.correct_bracket_after_fill(
        alpaca._trading,
        order["id"],
        iv.current_price,
        sl_pct,
        tp_pct,
    )
    if corrected is None:
        logger.warning(
            "AAPL fill-slippage correction timed out — journaling with indicative entry price $%.2f; "
            "actual SL/TP may be off by up to %.1f%%.",
            iv.current_price, config.FILL_SLIPPAGE_THRESHOLD * 100,
        )
```

Replace the hardcoded `AAPL` with `symbol` in the log.

- [ ] **Step 3: Run tests**

```
.venv/bin/python -m pytest tests/test_trading_budget.py -k "fill" -v
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 4: Commit**

```bash
git add trading/risk_manager.py trading/executor.py tests/test_trading_budget.py
git commit -m "fix: log warning when fill-slippage correction times out; executor distinguishes None from correction dict"
```

---

### Task 6: Full regression

- [ ] **Step 1: Run full suite**

```
.venv/bin/python -m pytest tests/ -m unit -q --tb=short
```
Expected: all green.

- [ ] **Step 2: Verify swing _promote_to_swing exists and is tested**

```bash
grep -n "_promote_to_swing" trading/eod_holds.py tests/test_trading_lifecycle.py
```
