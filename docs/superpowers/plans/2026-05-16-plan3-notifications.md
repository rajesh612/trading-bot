# Plan 3: Notification Completeness & Throttling

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add all missing intraday alerts; throttle trade pings to prevent spam; enrich EOD summary with capacity/budget metrics.

**Architecture:** All notifications are fire-and-forget (`_send()` never raises). New alerts use date-keyed sentinel dicts (same pattern as `_daily_loss_warned`) to prevent duplicates. Trade notifications are batched per-cycle via a module-level queue flushed once per 10-min cycle. EOD summary is extended with new fields from the budget and telemetry files.

**Tech Stack:** Python 3.11, pytest, requests (existing)

---

## File Map

| File | Change |
|---|---|
| `notifications/telegram.py` | Add 8 new alert functions; add trade-batch queue; add throttle sentinel pattern |
| `trading/cycle.py` | Call budget-warning intraday; call drawdown-alert check |
| `trading/monitor.py` | Call widening-drawdown alert when position > -2% but above stop |
| `trading/risk_manager.py` | Call intraday budget-warning when entry pool crosses 75% |
| `notifications/daily_report.py` | Extend EOD Telegram with capacity%, budget-used, cycle count |
| `tests/test_foundation.py` | Tests for new alert throttle logic |

---

### Task 1: Add missing Telegram alert functions

**Files:**
- Modify: `notifications/telegram.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_foundation.py — add
import pytest
from unittest.mock import patch
from datetime import datetime
import notifications.telegram as tg

@pytest.mark.unit
def test_notify_budget_intraday_throttled(monkeypatch):
    """notify_budget_intraday fires only once per day."""
    import notifications.telegram as t
    monkeypatch.setattr(t, "_budget_warn_sent", {"date": None})
    sent = []
    monkeypatch.setattr(t, "_send", lambda msg: sent.append(msg))
    t.notify_budget_intraday_warning(0.75, 1.0)
    t.notify_budget_intraday_warning(0.80, 1.0)
    assert len(sent) == 1, "Should only send once per day"

@pytest.mark.unit
def test_notify_position_drawdown_throttled_per_symbol(monkeypatch):
    """notify_position_drawdown fires once per symbol per day."""
    import notifications.telegram as t
    monkeypatch.setattr(t, "_drawdown_alerted", {})
    sent = []
    monkeypatch.setattr(t, "_send", lambda msg: sent.append(msg))
    t.notify_position_drawdown("AAPL", -2.5, -4.0)
    t.notify_position_drawdown("AAPL", -2.8, -4.0)  # same symbol, second call
    t.notify_position_drawdown("MSFT", -2.1, -4.0)  # different symbol
    assert len(sent) == 2, f"Expected 2 alerts (one per symbol), got {len(sent)}"

@pytest.mark.unit
def test_notify_sector_flip_throttled(monkeypatch):
    """notify_sector_flip fires once per sector per day."""
    import notifications.telegram as t
    monkeypatch.setattr(t, "_sector_flip_alerted", {})
    sent = []
    monkeypatch.setattr(t, "_send", lambda msg: sent.append(msg))
    t.notify_sector_flip("XLK", -1.8, ["AAPL", "NVDA"])
    t.notify_sector_flip("XLK", -2.1, ["AAPL"])  # same sector, second call
    assert len(sent) == 1
```

- [ ] **Step 2: Run to confirm failure**

```
.venv/bin/python -m pytest tests/test_foundation.py -k "notify_budget_intraday or notify_position_drawdown or notify_sector_flip" -v
```

- [ ] **Step 3: Add new functions to telegram.py**

Add after the existing `notify_budget_warning()` function:

```python
# ── Intraday risk alerts ───────────────────────────────────────────────────────

_budget_warn_sent: dict = {"date": None}

def notify_budget_intraday_warning(spent_usd: float, ceiling_usd: float) -> None:
    """Alert once per day when entry pool crosses 75%."""
    from utils.constants import ET
    today = datetime.now(ET).date().isoformat()
    if _budget_warn_sent.get("date") == today:
        return
    _budget_warn_sent["date"] = today
    pct = spent_usd / ceiling_usd * 100 if ceiling_usd else 0
    _send(
        f"<b>💸 Entry budget {pct:.0f}% used intraday</b>\n"
        f"${spent_usd:.3f} of ${ceiling_usd:.2f} — new entries may slow."
    )


_drawdown_alerted: dict = {}  # {symbol: date}

def notify_position_drawdown(symbol: str, pnl_pct: float, stop_pct: float) -> None:
    """Alert once per symbol per day when position drawdown > 2% but above stop."""
    from utils.constants import ET
    today = datetime.now(ET).date().isoformat()
    if _drawdown_alerted.get(symbol) == today:
        return
    _drawdown_alerted[symbol] = today
    _send(
        f"<b>⚠️ {symbol} drawdown {pnl_pct:.1f}%</b>\n"
        f"Stop at {stop_pct:.1f}% — monitor for cascade risk."
    )


_sector_flip_alerted: dict = {}  # {sector: date}

def notify_sector_flip(sector_etf: str, change_pct: float, affected_symbols: list[str]) -> None:
    """Alert once per sector per day on intraday sector reversal ≥ SECTOR_ETF_SOFT_DROP_PCT."""
    from utils.constants import ET
    today = datetime.now(ET).date().isoformat()
    if _sector_flip_alerted.get(sector_etf) == today:
        return
    _sector_flip_alerted[sector_etf] = today
    syms = ", ".join(affected_symbols[:5])
    _send(
        f"<b>📉 Sector flip: {sector_etf} {change_pct:+.1f}%</b>\n"
        f"Open positions at risk: {syms}"
    )


def notify_stale_research(age_min: float) -> None:
    """Fire once when research report exceeds RESEARCH_STALE_WARN_MIN."""
    _send(
        f"<b>⏰ Stale research report ({age_min:.0f} min old)</b>\n"
        "Research job may have failed. Sentiment signals are degraded."
    )


def notify_correlated_exposure(sector: str, count: int, symbols: list[str]) -> None:
    """Alert when sector concentration exceeds MAX_SAME_SECTOR_POSITIONS."""
    syms = ", ".join(symbols)
    _send(
        f"<b>⚠️ Concentrated exposure: {sector} ({count} positions)</b>\n"
        f"Symbols: {syms}\n"
        "Sector move could trigger correlated cascade."
    )
```

- [ ] **Step 4: Run tests**

```
.venv/bin/python -m pytest tests/test_foundation.py -k "notify_budget_intraday or notify_position_drawdown or notify_sector_flip" -v
.venv/bin/python -m pytest tests/ -m unit -q
```
---

### Task 2: Wire budget warning into intraday cycle

**Files:**
- Modify: `trading/risk_manager.py`
- Modify: `trading/cycle.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_trading_budget.py — add
@pytest.mark.unit
def test_budget_intraday_warning_fired_at_75pct(monkeypatch, tmp_path):
    """check_entry_budget fires notify_budget_intraday_warning when entry spent ≥ 75%."""
    monkeypatch.setattr(config, "API_BUDGET_FILE", tmp_path / "budget.json")
    from utils.io_utils import save_json
    from trading import risk_manager

    # Seed budget at 76% of entry ceiling
    entry_ceil = 0.70
    budget = {
        "date": "2026-05-16", "calls_made": 5, "entry_calls": 5, "exit_calls": 0,
        "input_tokens": 0, "output_tokens": 0,
        "cache_write_tokens": 0, "cache_read_tokens": 0,
        "estimated_cost_usd": 0.536, "entry_spent_usd": 0.536, "exit_spent_usd": 0.0,
        "ceiling_usd": 1.00,
        "entry_ceiling_usd": entry_ceil, "exit_ceiling_usd": 0.30,
        "ceiling_mode": "normal",
        "entry_exhausted": False, "exit_exhausted": False,
        "budget_exhausted": False, "escalated": False,
    }
    save_json(config.API_BUDGET_FILE, budget)

    warned = []
    monkeypatch.setattr("notifications.telegram.notify_budget_intraday_warning",
                        lambda s, c: warned.append((s, c)))
    monkeypatch.setattr("notifications.telegram._budget_warn_sent", {"date": None})

    risk_manager.check_entry_budget(estimated_cost=0.01, regime="NORMAL")
    assert warned, "Expected budget intraday warning when at 76% of entry ceiling"
```

- [ ] **Step 2: Update risk_manager.py check_entry_budget()**

After checking `projected > entry_ceil`, and also when the projection is still within ceiling but spent ratio ≥ 75%:

```python
    # Intraday budget warning at 75% of entry ceiling
    if not b.get("entry_exhausted"):
        entry_warn_pct = 0.75
        if spent / entry_ceil >= entry_warn_pct:
            telegram_notify.notify_budget_intraday_warning(spent, entry_ceil)
```

Place this check before the `projected > entry_ceil` block.

- [ ] **Step 3: Run tests**

```
.venv/bin/python -m pytest tests/test_trading_budget.py -k "budget_intraday_warning" -v
.venv/bin/python -m pytest tests/ -m unit -q
```
---

### Task 3: Wire drawdown + sector-flip alerts into monitor

**Files:**
- Modify: `trading/monitor.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_trading_lifecycle.py — add
@pytest.mark.unit
def test_monitor_fires_drawdown_alert_at_minus_2pct(monkeypatch, tmp_path):
    """Position monitor sends drawdown alert when unrealised P&L <= -2% above stop."""
    monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "positions.json")
    from utils.io_utils import save_json

    position = {
        "entry_price": 100.0, "qty": 10,
        "stop_loss_pct": 4.0, "stop_loss_price": 96.0,
        "take_profit_price": 105.0,
        "entry_composite_score": 30.0,
    }
    save_json(config.OPEN_POSITIONS_FILE, {"AAPL": position})

    alerts = []
    monkeypatch.setattr("notifications.telegram.notify_position_drawdown",
                        lambda sym, pnl, stop: alerts.append(sym))
    monkeypatch.setattr("notifications.telegram._drawdown_alerted", {})

    from trading.monitor import _check_drawdown_alerts
    # AAPL at 97.5 = -2.5% from entry, above stop at 96.0
    _check_drawdown_alerts({"AAPL": position}, current_prices={"AAPL": 97.5})
    assert "AAPL" in alerts
```

- [ ] **Step 2: Add _check_drawdown_alerts() to monitor.py**

Add a new helper function:

```python
_DRAWDOWN_WARN_PCT = -2.0  # alert when unrealised PnL < this % but above stop

def _check_drawdown_alerts(positions: dict, current_prices: dict) -> None:
    """Fire drawdown alerts for positions with unrealised PnL <= -2% but above stop."""
    for symbol, pos in positions.items():
        price = current_prices.get(symbol)
        if price is None:
            continue
        entry = pos.get("entry_price", 0)
        if entry <= 0:
            continue
        pnl_pct = (price - entry) / entry * 100
        stop_pct = -pos.get("stop_loss_pct", 4.0)
        if pnl_pct <= _DRAWDOWN_WARN_PCT and pnl_pct > stop_pct:
            telegram_notify.notify_position_drawdown(symbol, pnl_pct, stop_pct)
```

Call `_check_drawdown_alerts()` inside the position monitor loop after fetching current prices from Alpaca, passing in the open positions dict and current prices dict.

- [ ] **Step 3: Run tests**

```
.venv/bin/python -m pytest tests/test_trading_lifecycle.py -k "drawdown_alert" -v
.venv/bin/python -m pytest tests/ -m unit -q
```
---

### Task 4: Trade notification batching (spam prevention)

Replace per-trade `notify_buy()` calls with a cycle-level batch summary when more than 1 entry per cycle.

**Files:**
- Modify: `notifications/telegram.py`
- Modify: `trading/pipeline.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_foundation.py — add
@pytest.mark.unit
def test_notify_buy_batch_sends_one_message_for_multiple_entries(monkeypatch):
    """notify_buy_batch sends a single summary message for multiple entries."""
    import notifications.telegram as t
    sent = []
    monkeypatch.setattr(t, "_send", lambda msg: sent.append(msg))

    entries = [
        {"symbol": "AAPL", "qty": 10, "price": 182.0, "sl": 174.7, "tp": 191.1, "score": 35.0, "confidence": 75},
        {"symbol": "NVDA", "qty": 5,  "price": 820.0, "sl": 787.2, "tp": 861.0, "score": 42.0, "confidence": 80},
    ]
    t.notify_buy_batch(entries, regime="NORMAL")
    assert len(sent) == 1
    assert "AAPL" in sent[0]
    assert "NVDA" in sent[0]
```

- [ ] **Step 2: Add notify_buy_batch to telegram.py**

```python
def notify_buy_batch(entries: list[dict], regime: str) -> None:
    """Single summary Telegram for multiple entries in one cycle. Prefer over per-symbol notify_buy."""
    if not entries:
        return
    if len(entries) == 1:
        e = entries[0]
        notify_buy(symbol=e["symbol"], qty=e["qty"], price=e["price"],
                   sl=e["sl"], tp=e["tp"], mode=e.get("mode", "DAY_TRADE"),
                   score=e["score"], confidence=e["confidence"])
        return
    lines = [f"<b>📈 {len(entries)} entries — {regime}</b>"]
    for e in entries:
        lines.append(
            f"  • <b>{e['symbol']}</b> {e['qty']:.0f}sh @${e['price']:.2f} "
            f"SL${e['sl']:.2f} TP${e['tp']:.2f} score={e['score']:+.0f}"
        )
    _send("\n".join(lines))
```

- [ ] **Step 3: Update pipeline.py to use batch notification**

Collect entries during Phase 4 into a list, then call `notify_buy_batch()` at the end of the cycle:

```python
        # Collect for batch notification instead of per-entry notify_buy in executor
        _entries_this_cycle_log.append({
            "symbol": symbol, "qty": pos_data["qty"], "price": pos_data["entry_price"],
            "sl": pos_data["stop_loss_price"], "tp": pos_data["take_profit_price"],
            "score": iv.composite_score, "confidence": decision.get("confidence", 0),
            "mode": pos_data.get("mode_at_entry", "DAY_TRADE"),
        })
```

At end of run(), call `telegram_notify.notify_buy_batch(_entries_this_cycle_log, snap.regime)`.

In `executor.py`, suppress the `notify_buy()` call (comment it out or guard behind a flag) — the pipeline now handles batch notification. Or: pass `suppress_telegram: bool = False` to `execute_buy()` and set it True when the pipeline will batch-notify.

- [ ] **Step 4: Run tests**

```
.venv/bin/python -m pytest tests/test_foundation.py -k "notify_buy_batch" -v
.venv/bin/python -m pytest tests/ -m unit -q
```
---

### Task 5: Enrich EOD Telegram summary

**Files:**
- Modify: `notifications/daily_report.py`
- Modify: `notifications/telegram.py`

- [ ] **Step 1: Update notify_eod_summary() signature and content**

Add new parameters: `api_budget_pct`, `cycles_run`, `candidates_passed`, `regime_flips`:

```python
def notify_eod_summary(
    closes: list,
    day_pnl: float,
    day_pnl_pct: float,
    portfolio_value: float,
    regime: str,
    api_budget_pct: float = 0.0,
    cycles_run: int = 0,
    candidates_passed: int = 0,
    regime_flips: int = 0,
) -> None:
    ...
    lines = [
        f"<b>📊 EOD Summary</b>",
        f"P&L: <b>${day_pnl:+,.2f} ({day_pnl_pct:+.2f}%)</b>",
        f"Trades: {len(closes)} | Win rate: {win_rate:.0f}%",
        f"Best: {best['symbol']} ${best.get('pnl_dollar',0):+.2f} | "
        f"Worst: {worst['symbol']} ${worst.get('pnl_dollar',0):+.2f}",
        f"Regime: {regime} ({regime_flips} flip(s)) | Portfolio: ${portfolio_value:,.2f}",
        f"API budget: {api_budget_pct:.0f}% used | Candidates: {candidates_passed}/{cycles_run*15:.0f} est.",
    ]
    _send("\n".join(lines))
```

- [ ] **Step 2: Update daily_report.py to pass new fields**

In `send_daily_report()`, load the budget file for `api_budget_pct`:

```python
    from trading.risk_manager import load_budget
    budget = load_budget()
    ceiling = budget.get("ceiling_usd", 1.0)
    spent   = budget.get("estimated_cost_usd", 0.0)
    api_budget_pct = spent / ceiling * 100 if ceiling else 0.0
```

Pass `api_budget_pct=api_budget_pct` when calling `notify_eod_summary()`.

- [ ] **Step 3: Run tests**

```
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 4: Commit**

```bash
git add notifications/telegram.py notifications/daily_report.py
git commit -m "feat: EOD summary includes API budget %, candidate funnel, regime flip count"
```

---

### Task 6: Full regression

- [ ] **Step 1: Run full suite**

```
.venv/bin/python -m pytest tests/ -m unit -q --tb=short
```
Expected: all green.

- [ ] **Step 2: Verify no test imports telegram without patching**

```bash
grep -rn "telegram_notify\." tests/ | grep -v "monkeypatch\|patch\|mock" | grep -v "^Binary"
```
Expected: zero unpatched calls.
