# Trading Agent Modularization + Gate Unification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split `trading_agent.py` into focused modules under `trading/`, eliminate re-export shims, and unify gate thresholds so `pre_claude_filter`, `check_all_gates`, and `executor` use a single source of truth for every concept.

**Architecture:** Three new `trading/` modules replace the root-level file. A shared `effective_composite_gate()` in `signals/entry_gates.py` is the single threshold authority imported by both `pre_claude_filter` and `executor`. Config renames collapse duplicate constants; `fast_gate()` is deleted as dead code.

**Tech Stack:** Python 3.12, pytest, existing `trading/`, `signals/`, `config.py` patterns.

---

## File Map

| File | Action | Reason |
|---|---|---|
| `trading_agent.py` | **Delete** | Split into 3 modules below |
| `trading/watch_loader.py` | **Create** | `load_watch_list()` |
| `trading/monitor.py` | **Create** | `run_position_monitor()` |
| `trading/cycle.py` | **Create** | `run_trading_cycle()` + `_zero_pass` |
| `scheduler_jobs.py` | **Modify** | 5 import paths updated |
| `main.py` | **Modify** | 2 import paths updated |
| `utils/startup.py` | **Modify** | 1 import path updated |
| `utils/market_monitor.py` | **Modify** | 1 import path updated |
| `utils/bot_health.py` | **Modify** | 1 import path updated |
| `trading/eod_holds.py` | **Modify** | 1 import path updated |
| `config.py` | **Modify** | Remove 2 dead constants, rename 2, change RSI_BUY_MAX 75→72 |
| `signals/entry_gates.py` | **Modify** | Delete fast_gate(), remove dupe lines, add effective_composite_gate() |
| `trading/pre_claude_filter.py` | **Modify** | Unified Layer 1+2, add Layer 2.5 |
| `trading/executor.py` | **Modify** | Remove sentiment dupe, use unified composite/RSI |
| `trading/pipeline.py` | **Modify** | rsi_max regime check includes HIGH_VOL |
| `tests/test_signals.py` | **Modify** | Delete TestFastGate class, update RSI_HARD_SKIP refs, add TestEffectiveCompositeGate |
| `tests/test_pipeline.py` | **Modify** | Update constant names, add Layer 2.5 tests |
| `tests/test_learning.py` | **Modify** | Update RSI_BUY_MAX assertion |

---

### Task 1: Create `trading/watch_loader.py`

**Files:**
- Create: `trading/watch_loader.py`
- Test: `tests/test_trading_lifecycle.py`

- [ ] **Step 1: Write the failing test**

```python
# In tests/test_trading_lifecycle.py, add inside an appropriate class or at module level:

@pytest.mark.unit
def test_load_watch_list_returns_symbols(tmp_path):
    from trading.watch_loader import load_watch_list
    import config
    orig = config.WATCH_LIST_FILE
    config.WATCH_LIST_FILE = tmp_path / "watch_list.txt"
    config.WATCH_LIST_FILE.write_text("AAPL\nnvda\n  MSFT  \n")
    result = load_watch_list()
    config.WATCH_LIST_FILE = orig
    assert result == ["AAPL", "NVDA", "MSFT"]


@pytest.mark.unit
def test_load_watch_list_missing_file_returns_empty(tmp_path):
    from trading.watch_loader import load_watch_list
    import config
    orig = config.WATCH_LIST_FILE
    config.WATCH_LIST_FILE = tmp_path / "nonexistent.txt"
    result = load_watch_list()
    config.WATCH_LIST_FILE = orig
    assert result == []
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/python -m pytest tests/test_trading_lifecycle.py::test_load_watch_list_returns_symbols -v
```
Expected: `ModuleNotFoundError: No module named 'trading.watch_loader'`

- [ ] **Step 3: Create `trading/watch_loader.py`**

```python
import config


def load_watch_list() -> list[str]:
    if config.WATCH_LIST_FILE.exists():
        return [t.strip().upper() for t in config.WATCH_LIST_FILE.read_text().splitlines() if t.strip()]
    return []
```

- [ ] **Step 4: Run tests to verify pass**

```bash
.venv/bin/python -m pytest tests/test_trading_lifecycle.py::test_load_watch_list_returns_symbols tests/test_trading_lifecycle.py::test_load_watch_list_missing_file_returns_empty -v
```
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add trading/watch_loader.py tests/test_trading_lifecycle.py
git commit -m "feat: extract load_watch_list into trading/watch_loader"
```

---

### Task 2: Create `trading/monitor.py`

**Files:**
- Create: `trading/monitor.py`
- Test: `tests/test_trading_lifecycle.py`

- [ ] **Step 1: Write the failing test**

```python
# Add to tests/test_trading_lifecycle.py:

@pytest.mark.unit
def test_run_position_monitor_noop_when_no_positions(tmp_path):
    """Monitor returns immediately when there are no open positions."""
    from unittest.mock import patch
    with patch("trading.monitor.load_open_positions", return_value={}):
        from trading.monitor import run_position_monitor
        run_position_monitor()  # should not raise
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/python -m pytest tests/test_trading_lifecycle.py::test_run_position_monitor_noop_when_no_positions -v
```
Expected: `ModuleNotFoundError: No module named 'trading.monitor'`

- [ ] **Step 3: Create `trading/monitor.py`**

Copy `run_position_monitor` verbatim from `trading_agent.py` lines 63–181, updating only the module-level imports:

```python
"""
Position monitor — runs every 60 s.

Layer 1: Alpaca bracket fill reconciliation — detects bracket-order fills and syncs local state.
Layer 2: Price-level safety net — closes positions that breach SL/TP between cycles.
"""
import logging
from datetime import datetime

import config
import telegram_notify
from alpaca_client import AlpacaClient
from trading.closer import close_position, _apply_profit_lock
from trading.journal import (
    _POSITIONS_LOCK,
    load_open_positions,
    append_trade,
    record_daily_closed,
    flush_profit_lock_updates,
)
from utils.io_utils import load_json, save_json
from utils.constants import ET

logger = logging.getLogger(__name__)


def run_position_monitor():
    """
    Two-layer intra-cycle protection:

    Layer 1 — Alpaca bracket reconciliation
        Detects when Alpaca has already closed a position via bracket fill
        and syncs local state.

    Layer 2 — Price-level safety net
        Manually closes positions that breach SL/TP levels between cycles.
    """
    open_positions = load_open_positions()
    if not open_positions:
        return

    alpaca           = AlpacaClient()
    alpaca_positions = alpaca.get_positions()
    now              = datetime.now(ET)

    for symbol, pos in list(open_positions.items()):

        # ── Layer 1: bracket fill reconciliation ─────────────────────────────
        if symbol not in alpaca_positions:
            with _POSITIONS_LOCK:
                disk_positions = load_json(config.OPEN_POSITIONS_FILE, {})
                if symbol not in disk_positions:
                    open_positions.pop(symbol, None)
                    continue
                pos = disk_positions.pop(symbol)
                open_positions.pop(symbol, None)
                save_json(config.OPEN_POSITIONS_FILE, disk_positions)

            entry_price = pos["entry_price"]
            sl_price    = entry_price * (1 - pos.get("stop_loss_pct", config.STOP_LOSS_PCT) / 100)
            tp_price    = entry_price * (1 + pos.get("take_profit_pct", config.TAKE_PROFIT_PCT) / 100)
            try:
                current_price = alpaca.get_latest_price(symbol)
            except Exception:
                current_price = entry_price

            if current_price <= sl_price * 1.005:
                close_reason = "bracket_stop_loss_filled"
                exit_price   = sl_price
            elif current_price >= tp_price * 0.995:
                close_reason = "bracket_take_profit_filled"
                exit_price   = tp_price
            else:
                close_reason = "bracket_filled_unknown_leg"
                exit_price   = current_price

            pnl_pct    = (exit_price - entry_price) / entry_price * 100
            pnl_dollar = (exit_price - entry_price) * pos["qty"]

            append_trade({
                "type": "CLOSE",
                "symbol": symbol,
                "timestamp": now.isoformat(),
                "exit_price": round(exit_price, 4),
                "entry_price": round(entry_price, 4),
                "qty": pos["qty"],
                "pnl_pct": round(pnl_pct, 2),
                "pnl_dollar": round(pnl_dollar, 2),
                "close_reason": close_reason,
                "entry_composite_score": pos.get("entry_composite_score"),
                "exit_composite_score": None,
                "exit_indicators": {},
                "entry_indicators": pos.get("entry_indicators", {}),
                "entry_sentiment_score": pos.get("entry_sentiment_score"),
                "claude_reasoning": pos.get("claude_reasoning", ""),
                "indicator_signals_at_entry": pos.get("indicator_signals", {}),
            })
            record_daily_closed(symbol)
            logger.info(
                "BRACKET FILL detected: %s closed via %s | P&L=%+.2f%% ($%+.2f) | no re-entry today",
                symbol, close_reason, pnl_pct, pnl_dollar,
            )
            telegram_notify.notify_close(
                symbol=symbol, qty=pos["qty"],
                entry=entry_price, exit_price=exit_price,
                pnl_dollar=round(pnl_dollar, 2), pnl_pct=round(pnl_pct, 2),
                reason=close_reason,
            )
            continue

        # ── Layer 2: price-level safety net ──────────────────────────────────
        try:
            current_price = alpaca.get_latest_price(symbol)
        except Exception as exc:
            logger.warning("Position monitor: could not get price for %s: %s", symbol, exc)
            continue

        pos = _apply_profit_lock(pos, current_price)
        open_positions[symbol] = pos

        entry_price = pos["entry_price"]
        pnl_pct     = (current_price - entry_price) / entry_price * 100
        sl_pct      = pos.get("stop_loss_pct", config.STOP_LOSS_PCT)
        tp_pct      = pos.get("take_profit_pct", config.TAKE_PROFIT_PCT)

        if pnl_pct <= -sl_pct:
            logger.warning(
                "MONITOR STOP-LOSS: %s P&L=%+.2f%% breached SL=%.1f%% — closing (mid-cycle)",
                symbol, pnl_pct, -sl_pct,
            )
            close_position(symbol, alpaca, open_positions, current_price, None, "monitor_stop_loss")
        elif pnl_pct >= tp_pct:
            logger.info(
                "MONITOR TAKE-PROFIT: %s P&L=%+.2f%% hit TP=%.1f%% — closing (mid-cycle)",
                symbol, pnl_pct, tp_pct,
            )
            close_position(symbol, alpaca, open_positions, current_price, None, "monitor_take_profit")

    flush_profit_lock_updates(open_positions)

    try:
        from swing_manager import monitor_swing_positions
        monitor_swing_positions(alpaca)
    except Exception as exc:
        logger.warning("Swing position monitor failed: %s", exc)
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/python -m pytest tests/test_trading_lifecycle.py::test_run_position_monitor_noop_when_no_positions -v
```
Expected: 1 passed

- [ ] **Step 5: Commit**

```bash
git add trading/monitor.py tests/test_trading_lifecycle.py
git commit -m "feat: extract run_position_monitor into trading/monitor"
```

---

### Task 3: Create `trading/cycle.py`

**Files:**
- Create: `trading/cycle.py`
- Test: `tests/test_trading_lifecycle.py`

- [ ] **Step 1: Write the failing test**

```python
# Add to tests/test_trading_lifecycle.py:

@pytest.mark.unit
def test_run_trading_cycle_skips_empty_watch_list(tmp_path):
    """Cycle returns immediately when watch list is empty."""
    from unittest.mock import patch, MagicMock
    mock_account = MagicMock()
    mock_account.portfolio_value = "10000"
    mock_account.buying_power = "5000"
    mock_account.last_equity = "10000"
    with patch("trading.cycle.AlpacaClient") as mock_alpaca_cls, \
         patch("trading.cycle.load_watch_list", return_value=[]), \
         patch("trading.cycle.load_macro_regime", return_value={"regime": "NORMAL"}), \
         patch("trading.cycle.load_trading_params", return_value={
             "entry_start_hour": 10, "entry_end_hour": 15,
             "rsi_buy_max": 72, "rsi_buy_max_high_vol": 65,
             "high_vol_position_mult": 0.5,
         }):
        mock_alpaca_cls.return_value.get_account.return_value = mock_account
        from trading.cycle import run_trading_cycle
        run_trading_cycle()  # should not raise
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/python -m pytest tests/test_trading_lifecycle.py::test_run_trading_cycle_skips_empty_watch_list -v
```
Expected: `ModuleNotFoundError: No module named 'trading.cycle'`

- [ ] **Step 3: Create `trading/cycle.py`**

```python
"""
Trading cycle orchestrator — runs every 10 minutes during market hours.

Loads market state, applies circuit breakers, builds cycle snapshot,
and delegates to TradingPipeline. Tracks zero-candidate cycles and
fires a dead-day Telegram alert after 4 consecutive misses.
"""
import logging
from datetime import datetime

import anthropic

import config
import telegram_notify
from alpaca_client import AlpacaClient
from macro_regime import load_macro_regime, load_trading_params
from trading.journal import load_open_positions  # noqa: F401 — re-used by callers via this module
from trading.closer import force_close_all       # noqa: F401
from trading.cycle_context import build_cycle_snapshot
from trading.pipeline import TradingPipeline
from trading.watch_loader import load_watch_list
from utils.constants import ET

logger = logging.getLogger(__name__)

_zero_pass: dict = {"date": None, "count": 0, "alerted": False}


def run_trading_cycle() -> None:
    now = datetime.now(ET)
    logger.info("Trading cycle @ %s ET", now.strftime("%H:%M:%S"))

    alpaca  = AlpacaClient()
    account = alpaca.get_account()
    portfolio_value = float(account.portfolio_value)
    buying_power    = float(account.buying_power)

    last_equity   = float(account.last_equity)
    daily_pnl_pct = (portfolio_value - last_equity) / last_equity * 100 if last_equity else 0
    if daily_pnl_pct <= -(config.MAX_DAILY_LOSS_PCT * 100):
        logger.warning("Daily loss limit hit (%.2f%%). Halting new entries.", daily_pnl_pct)
        return

    regime_info = load_macro_regime()
    regime      = regime_info.get("regime", "NORMAL")
    params      = load_trading_params()

    flash_crash_active = regime_info.get("flash_crash_active", False)
    flash_crash_until  = regime_info.get("flash_crash_until")
    can_open           = True
    if flash_crash_active and flash_crash_until:
        try:
            until_dt = datetime.fromisoformat(flash_crash_until)
            if now < until_dt:
                logger.warning("Flash crash lockout active until %s — no entries.", flash_crash_until)
                can_open = False
            else:
                flash_crash_active = False
        except Exception:
            pass

    if regime == "CRISIS" or flash_crash_active:
        logger.warning(
            "CRISIS/flash-crash regime: VIX=%.1f | health=%+d — skipping all new entries.",
            regime_info.get("vix_current", 0), regime_info.get("macro_health_score", 0),
        )
        can_open = False

    entry_start = now.replace(hour=params["entry_start_hour"], minute=0, second=0, microsecond=0)
    entry_end   = now.replace(hour=params["entry_end_hour"],   minute=0, second=0, microsecond=0)
    if not (entry_start <= now < entry_end):
        logger.debug(
            "Outside entry window (%d–%d ET) — no new entries.",
            params["entry_start_hour"], params["entry_end_hour"],
        )
        can_open = False

    if regime in ("DEFENSIVE", "CHOPPY"):
        logger.info(
            "Regime=%s: position size ×%.1f, RSI max=%s",
            regime, params["high_vol_position_mult"],
            params["rsi_buy_max_high_vol"],
        )

    watch_list = load_watch_list()
    if not watch_list:
        logger.warning("Empty watch list — skipping cycle.")
        return

    portfolio_gain_pct = (portfolio_value - last_equity) / last_equity * 100 if last_equity else 0.0
    snap = build_cycle_snapshot(
        portfolio_gain_pct=portfolio_gain_pct,
        regime_info=regime_info,
        params=params,
    )

    ai = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    pipeline     = TradingPipeline()
    cycle_result = pipeline.run(
        symbols=watch_list,
        snap=snap,
        ai=ai,
        now=now,
        alpaca=alpaca,
        portfolio_value=portfolio_value,
        buying_power=buying_power,
        can_open=can_open,
    )

    today_str = now.strftime("%Y-%m-%d")
    if _zero_pass["date"] != today_str:
        _zero_pass.update({"date": today_str, "count": 0, "alerted": False})

    if can_open and not cycle_result.skipped:
        if cycle_result.candidates == 0:
            _zero_pass["count"] += 1
        else:
            _zero_pass["count"] = 0

        alert_hour = params.get("entry_start_hour", config.ENTRY_START_HOUR) + 1
        if (not _zero_pass["alerted"]
                and _zero_pass["count"] >= 4
                and now.hour >= alert_hour):
            _zero_pass["alerted"] = True
            msg = (
                f"⚠️ <b>Dead-day alert</b>: {_zero_pass['count']} consecutive cycles with 0 "
                f"symbols passing pre-Claude gates.\n"
                f"Regime: <b>{regime}</b> | Watch list: {len(watch_list)} symbols\n"
                f"Check COMPOSITE_MIN_NORMAL and gate thresholds."
            )
            telegram_notify._send(msg)
            logger.warning(
                "Dead-day alert: %d consecutive zero-candidate cycles in entry window (regime=%s)",
                _zero_pass["count"], regime,
            )
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/python -m pytest tests/test_trading_lifecycle.py::test_run_trading_cycle_skips_empty_watch_list -v
```
Expected: 1 passed

- [ ] **Step 5: Commit**

```bash
git add trading/cycle.py tests/test_trading_lifecycle.py
git commit -m "feat: extract run_trading_cycle into trading/cycle"
```

---

### Task 4: Update all callers and delete `trading_agent.py`

**Files:**
- Modify: `scheduler_jobs.py`
- Modify: `main.py`
- Modify: `utils/startup.py`
- Modify: `utils/market_monitor.py`
- Modify: `utils/bot_health.py`
- Modify: `trading/eod_holds.py`
- Delete: `trading_agent.py`

- [ ] **Step 1: Update `scheduler_jobs.py` — 5 import changes**

Find and replace each lazy import:

```python
# line ~88: reset_daily_closed
# OLD:
from trading_agent import reset_daily_closed
# NEW:
from trading.journal import reset_daily_closed

# line ~116: run_trading_cycle
# OLD:
from trading_agent import run_trading_cycle
# NEW:
from trading.cycle import run_trading_cycle

# line ~128: run_position_monitor
# OLD:
from trading_agent import run_position_monitor
# NEW:
from trading.monitor import run_position_monitor

# line ~134: force_close_all
# OLD:
from trading_agent import force_close_all
# NEW:
from trading.closer import force_close_all

# line ~205: load_open_positions
# OLD:
from trading_agent import load_open_positions
# NEW:
from trading.journal import load_open_positions
```

- [ ] **Step 2: Update `main.py` — 2 import changes**

```python
# line ~278: load_open_positions
# OLD:
from trading_agent import load_open_positions
# NEW:
from trading.journal import load_open_positions
```

Also remove the `trading_agent` re-export block (lines 33–43 of `trading_agent.py` were already in `trading_agent.py`; `main.py` itself has no such block — verify with `grep trading_agent main.py` and fix any remaining references).

- [ ] **Step 3: Update `utils/startup.py`**

```python
# OLD:
from trading_agent import archive_old_journal_entries
# NEW:
from trading.journal import archive_old_journal_entries
```

- [ ] **Step 4: Update `utils/market_monitor.py`**

```python
# OLD:
from trading_agent import force_close_all  # lazy import to avoid circular dependency
# NEW:
from trading.closer import force_close_all
```

- [ ] **Step 5: Update `utils/bot_health.py`**

```python
# OLD:
from trading_agent import load_open_positions, save_open_positions, append_trade
# NEW:
from trading.journal import load_open_positions, save_open_positions, append_trade
```

- [ ] **Step 6: Update `trading/eod_holds.py`**

```python
# OLD:
from trading_agent import load_open_positions
# NEW:
from trading.journal import load_open_positions
```

- [ ] **Step 7: Delete `trading_agent.py`**

```bash
rm ~/Documents/projects/trading-bot/trading_agent.py
```

- [ ] **Step 8: Run full test suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```
Expected: all tests pass (same count as before this task). Fix any remaining `trading_agent` import errors.

- [ ] **Step 9: Commit**

```bash
git add -u
git add trading/cycle.py trading/monitor.py trading/watch_loader.py
git commit -m "refactor: delete trading_agent.py — callers import from canonical sources"
```

---

### Task 5: Move `effective_composite_gate()` to `signals/entry_gates.py`

**Files:**
- Modify: `signals/entry_gates.py`
- Modify: `trading/executor.py` (remove definition, add import)
- Test: `tests/test_signals.py`

- [ ] **Step 1: Write failing tests**

```python
# Add to tests/test_signals.py — new class after TestCheckAllGates:

class TestEffectiveCompositeGate:
    """effective_composite_gate is the single source of truth for composite thresholds."""

    def _iv(self, *, squeeze_setup=False, bot_herding_score=0):
        from tests.test_signals import TestFastGate  # reuse IV factory if present, else inline
        iv = MagicMock()
        iv.squeeze_setup = squeeze_setup
        iv.bot_herding_score = bot_herding_score
        return iv

    @pytest.mark.unit
    def test_high_vol_regime_returns_35(self):
        from signals.entry_gates import effective_composite_gate
        iv = self._iv()
        assert effective_composite_gate(iv, "HIGH_VOL") == 35

    @pytest.mark.unit
    def test_defensive_regime_returns_35(self):
        from signals.entry_gates import effective_composite_gate
        iv = self._iv()
        assert effective_composite_gate(iv, "DEFENSIVE") == 35

    @pytest.mark.unit
    def test_squeeze_setup_returns_12(self):
        from signals.entry_gates import effective_composite_gate
        iv = self._iv(squeeze_setup=True)
        assert effective_composite_gate(iv, "NORMAL") == 12

    @pytest.mark.unit
    def test_herding_returns_25(self):
        from signals.entry_gates import effective_composite_gate
        iv = self._iv(bot_herding_score=60)  # BOT_HERD_ALERT_SCORE
        assert effective_composite_gate(iv, "NORMAL") == 25

    @pytest.mark.unit
    def test_choppy_returns_30(self):
        from signals.entry_gates import effective_composite_gate
        iv = self._iv()
        assert effective_composite_gate(iv, "CHOPPY") == 30

    @pytest.mark.unit
    def test_normal_returns_20(self):
        from signals.entry_gates import effective_composite_gate
        iv = self._iv()
        assert effective_composite_gate(iv, "NORMAL") == 20

    @pytest.mark.unit
    def test_high_vol_overrides_squeeze(self):
        """HIGH_VOL safety takes priority over squeeze setup."""
        from signals.entry_gates import effective_composite_gate
        iv = self._iv(squeeze_setup=True)
        assert effective_composite_gate(iv, "HIGH_VOL") == 35
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/python -m pytest tests/test_signals.py::TestEffectiveCompositeGate -v
```
Expected: `ImportError: cannot import name 'effective_composite_gate'`

- [ ] **Step 3: Add `effective_composite_gate` to `signals/entry_gates.py`**

At the top of `signals/entry_gates.py`, ensure this import is present:
```python
import config
from indicators.values import IndicatorValues
```

Insert the function **before** `fast_gate()` (it will be deleted in Task 6, but add here first):

```python
def effective_composite_gate(iv: IndicatorValues, regime: str) -> int:
    """Single source of truth for composite entry threshold by regime and signal context.

    Priority: HIGH_VOL safety → squeeze setup → bot herding → CHOPPY → NORMAL.
    """
    if regime in ("HIGH_VOL", "DEFENSIVE"):
        return config.COMPOSITE_MIN_HIGH_VOL   # 35
    if iv.squeeze_setup:
        return 12                              # strong squeeze signal lowers bar
    if iv.bot_herding_score >= config.BOT_HERD_ALERT_SCORE:
        return config.HERD_ENTRY_GATE          # 25
    if regime == "CHOPPY":
        return config.CHOPPY_COMPOSITE_GATE   # 30
    return config.COMPOSITE_MIN_NORMAL        # 20
```

**Note:** `COMPOSITE_MIN_HIGH_VOL` and `COMPOSITE_MIN_NORMAL` are new constant names from Task 7. Since Task 7 comes after this, temporarily reference the old names and update in Task 7:
- Use `config.HIGH_VOL_COMPOSITE_MIN` instead of `config.COMPOSITE_MIN_HIGH_VOL`
- Use `config.PRE_CLAUDE_COMPOSITE_MIN` instead of `config.COMPOSITE_MIN_NORMAL`

Then update executor to import from signals instead of defining locally:

In `trading/executor.py`, replace:
```python
def effective_composite_gate(iv: IndicatorValues, regime: str) -> int:
    """Squeeze loosens; herding / choppy raises the composite entry threshold."""
    if iv.squeeze_setup:
        return 12
    if iv.bot_herding_score >= config.BOT_HERD_ALERT_SCORE:
        return config.HERD_ENTRY_GATE
    if regime == "CHOPPY":
        return config.CHOPPY_COMPOSITE_GATE
    return config.COMPOSITE_SCORE_THRESHOLD
```

With:
```python
from signals.entry_gates import effective_composite_gate
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/python -m pytest tests/test_signals.py::TestEffectiveCompositeGate -v
```
Expected: 7 passed

- [ ] **Step 5: Run full suite to catch regressions**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add signals/entry_gates.py trading/executor.py tests/test_signals.py
git commit -m "refactor: move effective_composite_gate to signals/entry_gates — single source of truth"
```

---

### Task 6: Config constant cleanup

**Files:**
- Modify: `config.py`
- Modify: all files that reference the renamed/deleted constants

- [ ] **Step 1: Update `config.py`**

Make the following changes in `config.py`:

```python
# REMOVE this line entirely:
COMPOSITE_SCORE_THRESHOLD = 15   # Skip Claude if no open position and score <= this

# REMOVE this line entirely:
RSI_HARD_SKIP_THRESHOLD    = 72    # RSI above this → hard gate before Claude

# CHANGE value 75 → 72:
RSI_BUY_MAX = 72               # Max RSI for BUY entry (unified across all gate layers)

# RENAME PRE_CLAUDE_COMPOSITE_MIN → COMPOSITE_MIN_NORMAL:
COMPOSITE_MIN_NORMAL          = 20    # composite entry minimum — NORMAL regime

# RENAME HIGH_VOL_COMPOSITE_MIN → COMPOSITE_MIN_HIGH_VOL:
COMPOSITE_MIN_HIGH_VOL        = 35    # raised composite minimum — HIGH_VOL / DEFENSIVE regime
```

Keep unchanged:
```python
RSI_BUY_MAX_HIGH_VOL = 65      # tighter gate in high-volatility regimes
VWAP_SLOPE_MIN = -0.3          # % VWAP drift; below this = downtrend
CHOPPY_COMPOSITE_GATE = 30     # composite minimum — CHOPPY regime
HERD_ENTRY_GATE = 25           # composite minimum when bot herding detected
```

- [ ] **Step 2: Update all usages of renamed/deleted constants**

Run these greps to find all occurrences, then update each:

```bash
grep -rn "PRE_CLAUDE_COMPOSITE_MIN\|HIGH_VOL_COMPOSITE_MIN\|RSI_HARD_SKIP_THRESHOLD\|COMPOSITE_SCORE_THRESHOLD" \
  ~/Documents/projects/trading-bot --include="*.py" | grep -v __pycache__
```

Expected matches and fixes:

| File | Old | New |
|---|---|---|
| `signals/entry_gates.py` | `config.HIGH_VOL_COMPOSITE_MIN` | `config.COMPOSITE_MIN_HIGH_VOL` |
| `signals/entry_gates.py` | `config.PRE_CLAUDE_COMPOSITE_MIN` | `config.COMPOSITE_MIN_NORMAL` |
| `trading/pre_claude_filter.py` | `config.PRE_CLAUDE_COMPOSITE_MIN` | `config.COMPOSITE_MIN_NORMAL` |
| `trading/pre_claude_filter.py` | `config.HIGH_VOL_COMPOSITE_MIN` | `config.COMPOSITE_MIN_HIGH_VOL` |
| `trading/pre_claude_filter.py` | `config.RSI_HARD_SKIP_THRESHOLD` | `config.RSI_BUY_MAX` |
| `tests/test_pipeline.py` | `PRE_CLAUDE_COMPOSITE_MIN` | `COMPOSITE_MIN_NORMAL` |
| `tests/test_pipeline.py` | `HIGH_VOL_COMPOSITE_MIN` | `COMPOSITE_MIN_HIGH_VOL` |
| `tests/test_signals.py` | `RSI_HARD_SKIP_THRESHOLD` | `RSI_BUY_MAX` |
| `tests/test_learning.py` | `RSI_BUY_MAX` assertion value `75` | update expected value to `72` |

- [ ] **Step 3: Run full test suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```
Expected: all pass (fix any `AttributeError: module 'config' has no attribute ...` errors)

- [ ] **Step 4: Commit**

```bash
git add config.py signals/entry_gates.py trading/pre_claude_filter.py \
        trading/executor.py tests/test_pipeline.py tests/test_signals.py tests/test_learning.py
git commit -m "refactor: unify composite and RSI config constants — remove dead COMPOSITE_SCORE_THRESHOLD and RSI_HARD_SKIP_THRESHOLD"
```

---

### Task 7: Delete `fast_gate()` and clean duplicate lines in `check_all_gates`

**Files:**
- Modify: `signals/entry_gates.py`
- Modify: `tests/test_signals.py`

- [ ] **Step 1: Delete `TestFastGate` class from `tests/test_signals.py`**

Remove the entire `TestFastGate` class (search for `class TestFastGate:` and delete through the last test method in that class). This class tests a function that no longer exists.

- [ ] **Step 2: Delete `fast_gate()` from `signals/entry_gates.py`**

Remove the entire function (lines 31–47 in the original; search for `def fast_gate(` and delete through `return GateResult(True)`).

Also remove its import reference in `signals/entry_gates.py` if `GateResult` is no longer needed at the top level (it still is — used by `check_all_gates`).

- [ ] **Step 3: Remove duplicate RSI + Stochastic lines from `check_all_gates`**

In `signals/entry_gates.py`, find and remove these two blocks inside `check_all_gates`:

```python
    # 10. RSI overbought hard skip — saves Claude tokens on conditions that will HOLD anyway
    if iv.rsi > config.RSI_HARD_SKIP_THRESHOLD:
        return GateResult(False, f"RSI overbought hard skip (RSI={iv.rsi:.1f} > {config.RSI_HARD_SKIP_THRESHOLD})")

    # 11. Stochastic overbought hard veto
    if iv.stoch_d > config.STOCH_HARD_VETO_THRESHOLD:
        return GateResult(False, f"Stochastic hard veto (%D={iv.stoch_d:.1f} > {config.STOCH_HARD_VETO_THRESHOLD})")
```

These are the lines that duplicated `fast_gate`. The Stochastic check that belongs in `check_all_gates` is the one at approximately line 99 (the original pre-fast_gate check). Verify there is exactly **one** Stochastic check remaining and **one** RSI check remaining in `check_all_gates` (the ones using `RSI_BUY_MAX` / `STOCH_HARD_VETO_THRESHOLD`).

- [ ] **Step 4: Run full test suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```
Expected: all pass (fewer tests now that TestFastGate is deleted)

- [ ] **Step 5: Commit**

```bash
git add signals/entry_gates.py tests/test_signals.py
git commit -m "refactor: delete dead fast_gate() and remove duplicate RSI/Stoch checks from check_all_gates"
```

---

### Task 8: Update `pre_claude_filter.py` — unified Layer 1+2, add Layer 2.5

**Files:**
- Modify: `trading/pre_claude_filter.py`
- Test: `tests/test_pipeline.py`

- [ ] **Step 1: Write failing tests**

```python
# Add to tests/test_pipeline.py — inside TestPreClaudeFilter class:

@pytest.mark.unit
def test_choppy_composite_gate_blocks_below_30(self):
    """In CHOPPY regime, composite 25 < 30 should be SKIP."""
    from trading.pre_claude_filter import pre_claude_filter
    snap = self._snap(regime_info={"regime": "CHOPPY", "macro_health_score": 0}, regime="CHOPPY")
    result = pre_claude_filter("TEST", self._iv(composite=25.0), snap)
    assert result.decision == "SKIP"
    assert "composite" in result.reason

@pytest.mark.unit
def test_choppy_composite_gate_passes_at_30(self):
    """In CHOPPY regime, composite 30 should pass Layer 1."""
    from trading.pre_claude_filter import pre_claude_filter
    snap = self._snap(regime_info={"regime": "CHOPPY", "macro_health_score": 0}, regime="CHOPPY")
    result = pre_claude_filter("TEST", self._iv(composite=30.0), snap)
    assert result.decision != "SKIP" or "composite" not in result.reason

@pytest.mark.unit
def test_layer_2_5_blocks_vwap_slope_downtrend(self):
    """VWAP slope below minimum blocks before Claude."""
    from trading.pre_claude_filter import pre_claude_filter
    snap = self._snap()
    snap = dataclasses.replace(snap, params={**snap.params, "vwap_slope_min": -0.3})
    iv = self._iv(composite=25.0)
    iv = dataclasses.replace(iv, vwap_slope=-0.5)  # below -0.3
    result = pre_claude_filter("TEST", iv, snap)
    assert result.decision == "SKIP"
    assert "VWAP slope" in result.reason

@pytest.mark.unit
def test_layer_2_5_blocks_vwap_overextension(self):
    """Price above VWAP+2σ blocks before Claude."""
    from trading.pre_claude_filter import pre_claude_filter
    from unittest.mock import patch
    snap = self._snap()
    iv = self._iv(composite=25.0)
    with patch("trading.pre_claude_filter.passes_vwap_guard", return_value=False):
        result = pre_claude_filter("TEST", iv, snap)
    assert result.decision == "SKIP"
    assert "VWAP" in result.reason
```

Note: `dataclasses.replace` requires adding `import dataclasses` at the top of the test file if not already present. Alternatively patch `iv.vwap_slope` via `unittest.mock.patch.object`.

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/python -m pytest tests/test_pipeline.py::TestPreClaudeFilter::test_choppy_composite_gate_blocks_below_30 tests/test_pipeline.py::TestPreClaudeFilter::test_layer_2_5_blocks_vwap_slope_downtrend -v
```
Expected: FAIL (behavior not yet implemented)

- [ ] **Step 3: Update `trading/pre_claude_filter.py`**

Replace the entire file content with:

```python
"""
5-layer pre-Claude filter: routes each candidate to SKIP, AUTO_EXECUTE, or CLAUDE.
All data comes from CycleSnapshot — zero I/O per symbol.
"""
from __future__ import annotations

from dataclasses import dataclass

import config
from indicators.values import IndicatorValues
from signals.entry_gates import effective_composite_gate
from signals.vwap_guard import passes_vwap_guard
from trading.cycle_context import CycleSnapshot


@dataclass(frozen=True)
class FilterResult:
    decision: str   # "SKIP" | "AUTO_EXECUTE" | "CLAUDE"
    reason: str


def pre_claude_filter(
    symbol: str,
    iv: IndicatorValues,
    snap: CycleSnapshot,
    daily_closed: set[str] | None = None,
) -> FilterResult:
    """
    Layer 0 — Already closed today.
    Layer 1 — Composite minimum (regime-adjusted via effective_composite_gate).
    Layer 2 — RSI maximum (regime-adjusted, same constant as executor).
    Layer 2.5 — VWAP: slope downtrend + price overextension.
    Layer 3 — Macro health score + morning shock score.
    Layer 4 — Research sentiment (9:15 AM report).
    Layer 5 — Intraday news (30-min cache; graceful fallback if empty).

    If all layers pass and signal is very strong → AUTO_EXECUTE (skip Claude).
    Otherwise → CLAUDE.
    """
    # Layer 0 — Already closed today
    if daily_closed and symbol in daily_closed:
        return FilterResult("SKIP", "already closed today")

    # Layer 1 — Composite minimum (unified with executor)
    composite_min = effective_composite_gate(iv, snap.regime)
    if iv.composite_score < composite_min:
        return FilterResult("SKIP",
            f"composite {iv.composite_score:+.1f} < {composite_min} ({snap.regime})")

    # Layer 2 — RSI maximum (same constants as executor rsi_max)
    rsi_max = (
        config.RSI_BUY_MAX_HIGH_VOL
        if snap.regime in ("HIGH_VOL", "DEFENSIVE")
        else config.RSI_BUY_MAX
    )
    if iv.rsi > rsi_max:
        return FilterResult("SKIP",
            f"RSI {iv.rsi:.1f} > {rsi_max} ({snap.regime})")

    # Layer 2.5 — VWAP checks (same criteria as executor)
    vwap_slope_min = snap.params.get("vwap_slope_min", config.VWAP_SLOPE_MIN)
    if iv.vwap_slope < vwap_slope_min:
        return FilterResult("SKIP",
            f"VWAP slope {iv.vwap_slope:.3f} < {vwap_slope_min:.3f} (downtrend)")
    if not passes_vwap_guard(iv):
        return FilterResult("SKIP", "VWAP overextension (price > VWAP+2σ)")

    # Layer 3 — Macro health + shock
    health = snap.regime_info.get("macro_health_score", 0)
    shock  = snap.research_meta.get("macro_shock_score", 0)
    if health < config.MACRO_HEALTH_SKIP_THRESHOLD:
        return FilterResult("SKIP",
            f"macro health {health} < {config.MACRO_HEALTH_SKIP_THRESHOLD}")
    if shock > config.MACRO_SHOCK_SKIP_THRESHOLD:
        return FilterResult("SKIP",
            f"macro shock {shock} > {config.MACRO_SHOCK_SKIP_THRESHOLD}")

    # Layer 4 — Research sentiment
    sentiment = snap.research.get(symbol, {}).get("sentiment_score", 0)
    if sentiment < config.SENTIMENT_HARD_BLOCK:
        return FilterResult("SKIP",
            f"sentiment {sentiment:+d} < hard block {config.SENTIMENT_HARD_BLOCK}")
    if sentiment < 0 and iv.composite_score < config.SENTIMENT_WEAK_COMPOSITE_MIN:
        return FilterResult("SKIP",
            f"negative sentiment ({sentiment:+d}) + composite {iv.composite_score:+.1f}"
            f" < {config.SENTIMENT_WEAK_COMPOSITE_MIN}")

    # Layer 5 — Intraday news (falls back to research sentiment if cache empty)
    intraday    = snap.intraday_news.get(symbol, {})
    intra_shock = intraday.get("shock_score", 0)
    intra_sent  = intraday.get("sentiment_score", sentiment)
    if intra_shock >= config.INTRADAY_NEWS_SHOCK_BLOCK:
        return FilterResult("SKIP",
            f"intraday shock {intra_shock} >= {config.INTRADAY_NEWS_SHOCK_BLOCK}")
    if intra_sent < config.INTRADAY_SENTIMENT_BLOCK:
        return FilterResult("SKIP",
            f"intraday sentiment {intra_sent:+d} < {config.INTRADAY_SENTIMENT_BLOCK}")

    # Auto-execute — all gates cleared, signal convincingly strong
    if (iv.composite_score > config.AUTO_EXECUTE_COMPOSITE_MIN
            and intra_sent > config.AUTO_EXECUTE_SENTIMENT_MIN):
        return FilterResult("AUTO_EXECUTE",
            f"composite={iv.composite_score:+.1f} intraday_sentiment={intra_sent:+d}")

    return FilterResult("CLAUDE", "")
```

- [ ] **Step 4: Update the `_snap` helper in `TestPreClaudeFilter` to include `params`**

The test helper `_snap()` must supply a `params` dict so Layer 2.5 can read `vwap_slope_min`. Find `_snap` in `TestPreClaudeFilter` and add `params` to the default snapshot:

```python
def _snap(self, regime_info=None, regime="NORMAL", research=None,
          research_meta=None, intraday_news=None, params=None):
    from trading.cycle_context import CycleSnapshot
    return CycleSnapshot(
        regime_info=regime_info or {"regime": "NORMAL", "macro_health_score": 0},
        regime=regime,
        fred_cache={},
        research=research or {"TEST": {"sentiment_score": 25}},
        research_meta=research_meta or {"macro_shock_score": 0},
        strategy_memory="",
        weights={},
        params=params or {
            "rsi_buy_max": 72, "rsi_buy_max_high_vol": 65,
            "high_vol_position_mult": 0.5,
            "entry_start_hour": 10, "entry_end_hour": 15,
            "vwap_slope_min": -0.3,
        },
        portfolio_gain_pct=0.0,
        intraday_news=intraday_news or {},
    )
```

Also update `_iv` helper to include `vwap_slope` and `vwap_std2_upper` / `current_price` fields needed by `passes_vwap_guard`:

```python
def _iv(self, composite=50.0, rsi=55.0, stoch_d=50.0, vwap_slope=0.1,
        vwap_std2_upper=105.0, current_price=100.0, **kw):
    # ... existing fields plus:
    iv.vwap_slope = vwap_slope
    iv.vwap_std2_upper = vwap_std2_upper
    iv.current_price = current_price
```

- [ ] **Step 5: Run full test suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add trading/pre_claude_filter.py tests/test_pipeline.py
git commit -m "feat: unified pre_claude_filter — Layer 1 via effective_composite_gate, Layer 2 RSI aligned, Layer 2.5 VWAP"
```

---

### Task 9: Clean up `executor.py` and `pipeline.py`

**Files:**
- Modify: `trading/executor.py`
- Modify: `trading/pipeline.py`
- Test: `tests/test_trading_pipeline.py`

- [ ] **Step 1: Write failing test for HIGH_VOL composite gate in executor**

```python
# Add to tests/test_trading_pipeline.py or tests/test_trading_modes.py:

@pytest.mark.unit
def test_execute_buy_blocked_by_high_vol_composite_gate():
    """executor rejects composite=30 in HIGH_VOL regime (gate=35)."""
    from unittest.mock import MagicMock, patch
    from trading.executor import execute_buy

    iv = MagicMock()
    iv.rsi = 55.0
    iv.vwap_slope = 0.1
    iv.composite_score = 30.0  # below HIGH_VOL gate of 35
    iv.squeeze_setup = False
    iv.bot_herding_score = 0
    iv.current_price = 100.0
    iv.prev_close = 97.0
    iv.stop_hunt_zone = False
    iv.parabolic = False
    iv.stoch_d = 50.0
    iv.rsi_delta = 1.0
    iv.atr = 1.0
    iv.vwap_std2_upper = 110.0

    with patch("trading.executor.check_all_gates") as mock_gate, \
         patch("trading.executor.get_active_mode", return_value="DAY_TRADE"), \
         patch("trading.executor.compute_position_size_for_mode", return_value=10):
        mock_gate.return_value = MagicMock(passed=True, flags=())
        result = execute_buy(
            symbol="TEST", iv=iv, decision={"action": "BUY", "stop_loss_pct": 4.0,
                                             "take_profit_pct": 5.0, "confidence": 0.8,
                                             "reasoning": "test"},
            regime="HIGH_VOL", regime_info={"regime": "HIGH_VOL", "macro_health_score": 0},
            research={}, open_positions={}, alpaca=MagicMock(),
            portfolio_value=10000, pos_mult=0.5, spy_ret5d=0.0,
            symbol_ret5d=0.0,
            rsi_max=65,  # HIGH_VOL RSI max
            params={"vwap_slope_min": -0.3, "entry_start_hour": 10, "entry_end_hour": 15},
            now=MagicMock(), entries_this_cycle=0,
        )
    assert result is None
```

- [ ] **Step 2: Run to verify failure** (composite gate in executor currently uses old `effective_composite_gate` that doesn't handle HIGH_VOL)

```bash
.venv/bin/python -m pytest tests/test_trading_pipeline.py::test_execute_buy_blocked_by_high_vol_composite_gate -v
```
Expected: FAIL — `result` is not `None` because old function returned `COMPOSITE_SCORE_THRESHOLD=15` for all non-CHOPPY regimes

- [ ] **Step 3: Update `trading/executor.py`**

a) Remove the local `effective_composite_gate` function definition entirely (it now lives in `signals/entry_gates.py` from Task 5).

b) Verify the import at the top of executor.py has:
```python
from signals.entry_gates import effective_composite_gate, check_all_gates
```

c) Remove the duplicate sentiment hard block (find and delete):
```python
    if research.get(symbol, {}).get("sentiment_score", 0) < config.SENTIMENT_HARD_BLOCK:
        logger.info("SKIP %s — sentiment hard block.", symbol)
        return None
```

- [ ] **Step 4: Update `trading/pipeline.py` — fix `rsi_max` regime list**

Find the `rsi_max` computation (line ~107–110) and add `HIGH_VOL` to the regime check:

```python
        rsi_max   = (
            snap.params.get("rsi_buy_max_high_vol", config.RSI_BUY_MAX_HIGH_VOL)
            if regime in ("HIGH_VOL", "CRISIS", "DEFENSIVE")
            else snap.params.get("rsi_buy_max", config.RSI_BUY_MAX)
        )
```

- [ ] **Step 5: Run full test suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add trading/executor.py trading/pipeline.py tests/test_trading_pipeline.py
git commit -m "fix: align executor composite gate and pipeline rsi_max with unified config constants"
```

---

### Task 10: Final verification and test count check

- [ ] **Step 1: Run complete test suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q --tb=short
```
Expected: ≥653 tests pass (the TestFastGate deletion reduces count; new tests from Tasks 1, 5, 8, 9 add back more).

- [ ] **Step 2: Verify no `trading_agent` references remain**

```bash
grep -rn "trading_agent" ~/Documents/projects/trading-bot --include="*.py" | grep -v __pycache__ | grep -v ".md"
```
Expected: **zero results**

- [ ] **Step 3: Verify no dead constants remain**

```bash
grep -rn "COMPOSITE_SCORE_THRESHOLD\|RSI_HARD_SKIP_THRESHOLD\|PRE_CLAUDE_COMPOSITE_MIN\|HIGH_VOL_COMPOSITE_MIN" \
  ~/Documents/projects/trading-bot --include="*.py" | grep -v __pycache__
```
Expected: **zero results**

- [ ] **Step 4: Verify `fast_gate` is gone**

```bash
grep -rn "fast_gate\|FastGate" ~/Documents/projects/trading-bot --include="*.py" | grep -v __pycache__
```
Expected: **zero results**

- [ ] **Step 5: Smoke-check imports of new modules**

```bash
.venv/bin/python -c "
from trading.watch_loader import load_watch_list
from trading.monitor import run_position_monitor
from trading.cycle import run_trading_cycle
from signals.entry_gates import effective_composite_gate
print('All new imports OK')
"
```
Expected: `All new imports OK`

- [ ] **Step 6: Final commit**

```bash
git add -A
git commit -m "test: final verification — all gate constants unified, trading_agent deleted"
```
