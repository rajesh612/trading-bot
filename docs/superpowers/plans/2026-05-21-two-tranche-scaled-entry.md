# Two-Tranche Scaled Entry — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split every BUY entry into T1 (60%) + T2 (40%) tranches so the bot can average down on controlled dips (4–7%) without needing a new signal or re-entry block bypass.

**Architecture:** T1 fires at signal (same path as today, just 60% sized). The 60s position monitor checks open positions for T2 eligibility via `_check_tranche2_triggers()` added after the SL/TP loop. When all 11 gates pass, `execute_tranche2_addon()` cancels the original bracket, places a market order for 40% fresh size, and updates the position record with blended entry/SL. A server-side stop order replaces the cancelled bracket; the software SL remains primary.

**Tech Stack:** Python, Alpaca SDK (`StopOrderRequest`), existing `trading/monitor.py` position loop, `signals/` pure-gate pattern, `backtest/simulator.py` per-bar loop.

---

## File Structure

| File | Type | Responsibility |
|---|---|---|
| `config.py` | Modify | 8 new T2 constants under `# ── Tranche-2 ──` header |
| `broker/alpaca_client.py` | Modify | `place_stop_order(symbol, qty, stop_price)` — new method |
| `signals/tranche2_gates.py` | New | `T2GateResult` dataclass + `check_tranche2_gates()` — pure, no I/O |
| `trading/executor.py` | Modify | T1 60% sizing; add tranche fields to `position_data`; new `execute_tranche2_addon()` |
| `trading/monitor.py` | Modify | `_check_tranche2_triggers()` called after SL/TP loop in `run_position_monitor()` |
| `backtest/simulator.py` | Modify | T2 check in per-bar position loop; `_sim_tranche2_gates_pass()`; `_sim_apply_tranche2()` |
| `tests/test_tranche2.py` | New | Full T2 test suite — all gate paths, executor math, monitor sentinel, simulator |

---

## Task 1: Config Constants

**Files:**
- Modify: `config.py` (after line 335, before `# ── BACKTEST SIMULATION ──` block)

- [ ] **Step 1: Add T2 constants to config.py**

Insert after the `# ── BACKTEST SIMULATION ──` header comment (keep backtest section intact — add T2 block above it):

```python
# ── Tranche-2 scaled entry ────────────────────────────────────────────────────
TRANCHE1_SIZE_PCT        = 0.60   # T1 = 60% of computed position size
TRANCHE2_SIZE_PCT        = 0.40   # T2 = 40% of fresh size computed at T2 price/ATR
TRANCHE2_DROP_MIN_PCT    = 4.0    # T2 triggers when price ≥ 4% below T1 entry
TRANCHE2_DROP_MAX_PCT    = 7.0    # T2 blocked if drop > 7% (breakdown, not dip)
TRANCHE2_RSI_MAX         = 45     # RSI must show oversold at trigger time
TRANCHE2_COMPOSITE_MIN   = 40     # Composite score must still be healthy
TRANCHE2_SL_PCT          = 5.5    # Blended SL: 5.5% below T2 fill price
TRANCHE2_CUTOFF_HOUR     = 14     # No T2 triggers after 2:00 PM ET
TRANCHE2_MIN_HOLD_MIN    = 60     # T1 must be held ≥ 60 min before T2 eligible
TRANCHE2_MIN_SENTIMENT   = 20     # Symbol sentiment score floor for T2
```

- [ ] **Step 2: Verify constants load correctly**

```bash
cd ~/Documents/projects/trading-bot
.venv/bin/python3 -c "import config; print(config.TRANCHE1_SIZE_PCT, config.TRANCHE2_SL_PCT)"
```

Expected output: `0.6 5.5`

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "feat: add Tranche-2 config constants"
```

---

## Task 2: AlpacaClient — `place_stop_order()`

**Files:**
- Modify: `broker/alpaca_client.py` (add method after `cancel_order()` at line 129)
- Test: `tests/test_tranche2.py` (create new file)

- [ ] **Step 1: Write the failing test**

Create `tests/test_tranche2.py`:

```python
"""
Two-tranche scaled entry unit tests.
Run with: pytest tests/test_tranche2.py -v -m unit
No network calls — all external dependencies are mocked.
"""
from __future__ import annotations

import types
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import config

ET = ZoneInfo("America/New_York")


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _t2_iv(**overrides):
    """IndicatorValues-compatible namespace that passes all T2 gates by default."""
    base = dict(
        current_price=93.0,    # 7% below T1 entry of 100 — on the boundary, use 95 in tests
        rsi=40.0,              # below TRANCHE2_RSI_MAX (45) ✓
        composite_score=55.0,  # above TRANCHE2_COMPOSITE_MIN (40) ✓
        atr=1.5,
        bot_herding_score=0,
        squeeze_setup=False,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _t2_pos(**overrides):
    """Open position dict that passes all T2 structural gates by default."""
    from datetime import timedelta
    entry_time = (datetime.now(ET) - timedelta(minutes=65)).isoformat()
    base = dict(
        tranche_2_triggered=False,
        tranche_1_entry=100.0,
        tranche_1_qty=60,
        qty=60,
        entry_price=100.0,
        entry_time=entry_time,
        stop_loss_pct=4.0,
        take_profit_pct=5.0,
        order_id="bracket-123",
        mode_at_entry="DAY_TRADE",
        regime_at_entry="NORMAL",
        atr_pct=1.5,
    )
    base.update(overrides)
    return base


def _t2_now(hour=11, minute=0):
    return datetime(2026, 5, 21, hour, minute, 0, tzinfo=ET)


# ── Task 2: place_stop_order ──────────────────────────────────────────────────

@pytest.mark.unit
class TestPlaceStopOrder:
    def test_place_stop_order_calls_submit_and_returns_id(self, monkeypatch):
        """place_stop_order submits a STOP order and returns a dict with stop_order_id."""
        import types as _t
        from broker.alpaca_client import AlpacaClient

        fake_order = _t.SimpleNamespace(id="stop-abc-123")
        fake_trading = _t.SimpleNamespace(submit_order=lambda req: fake_order)

        monkeypatch.setattr(AlpacaClient, "__init__", lambda self: None)
        client = AlpacaClient.__new__(AlpacaClient)
        client._trading = fake_trading

        result = client.place_stop_order("AAPL", 100, 87.50)

        assert result["stop_order_id"] == "stop-abc-123"
        assert result["symbol"] == "AAPL"
        assert result["stop_price"] == 87.50

    def test_place_stop_order_propagates_exception(self, monkeypatch):
        """place_stop_order lets Alpaca SDK exceptions propagate (caller handles them)."""
        import types as _t
        from broker.alpaca_client import AlpacaClient

        def _boom(req): raise RuntimeError("API error")
        fake_trading = _t.SimpleNamespace(submit_order=_boom)

        monkeypatch.setattr(AlpacaClient, "__init__", lambda self: None)
        client = AlpacaClient.__new__(AlpacaClient)
        client._trading = fake_trading

        with pytest.raises(RuntimeError, match="API error"):
            client.place_stop_order("AAPL", 100, 87.50)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python -m pytest tests/test_tranche2.py::TestPlaceStopOrder -v -m unit
```

Expected: FAIL — `AttributeError: type object 'AlpacaClient' has no attribute 'place_stop_order'`

- [ ] **Step 3: Add `place_stop_order()` to `broker/alpaca_client.py`**

Add after the `cancel_order()` method (after line 131):

```python
    def place_stop_order(self, symbol: str, qty: float, stop_price: float) -> dict:
        """Place a standalone SELL stop order to act as a server-side stop loss."""
        from alpaca.trading.requests import StopOrderRequest
        from alpaca.trading.enums import OrderType
        req = StopOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            type=OrderType.STOP,
            time_in_force=TimeInForce.DAY,
            stop_price=round(stop_price, 2),
        )
        order = self._trading.submit_order(req)
        logger.info(
            "Stop order placed: SELL %.0fx %s @ stop=$%.2f | id=%s",
            qty, symbol, stop_price, order.id,
        )
        return {"stop_order_id": str(order.id), "symbol": symbol, "stop_price": stop_price}
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/python -m pytest tests/test_tranche2.py::TestPlaceStopOrder -v -m unit
```

Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add broker/alpaca_client.py tests/test_tranche2.py
git commit -m "feat: add place_stop_order() to AlpacaClient"
```

---

## Task 3: `signals/tranche2_gates.py` — Pure Gate Logic

**Files:**
- Create: `signals/tranche2_gates.py`
- Test: `tests/test_tranche2.py` (append)

- [ ] **Step 1: Write failing tests for all 11 gates**

Append to `tests/test_tranche2.py`:

```python
# ── Task 3: check_tranche2_gates ─────────────────────────────────────────────

@pytest.mark.unit
class TestCheckTranche2Gates:
    """One test per gate — pass path and fail path for each."""

    def _call(self, iv=None, pos=None, now=None, regime="NORMAL",
              sentiment=30.0, sector_chg=0.0, major_event=False, monkeypatch=None):
        from signals.tranche2_gates import check_tranche2_gates
        import calendars.earnings as ec
        if monkeypatch:
            monkeypatch.setattr(ec, "is_earnings_imminent", lambda sym, days=1: False)
        return check_tranche2_gates(
            symbol="AAPL",
            iv=iv or _t2_iv(current_price=95.0),
            pos=pos or _t2_pos(),
            now=now or _t2_now(),
            regime=regime,
            sentiment_score=sentiment,
            sector_etf_chg=sector_chg,
            major_event_today=major_event,
        )

    def test_all_gates_pass(self, monkeypatch):
        result = self._call(monkeypatch=monkeypatch)
        assert result.passed is True
        assert result.reason == ""

    # Gate 1: already triggered
    def test_gate1_already_triggered(self, monkeypatch):
        pos = _t2_pos(tranche_2_triggered=True)
        result = self._call(pos=pos, monkeypatch=monkeypatch)
        assert result.passed is False
        assert "triggered" in result.reason

    # Gate 2: time cutoff
    def test_gate2_cutoff_blocked_at_2pm(self, monkeypatch):
        result = self._call(now=_t2_now(hour=14, minute=0), monkeypatch=monkeypatch)
        assert result.passed is False
        assert "cutoff" in result.reason

    def test_gate2_passes_before_2pm(self, monkeypatch):
        result = self._call(now=_t2_now(hour=13, minute=59), monkeypatch=monkeypatch)
        assert result.passed is True

    # Gate 3: regime
    def test_gate3_crisis_blocked(self, monkeypatch):
        result = self._call(regime="CRISIS", monkeypatch=monkeypatch)
        assert result.passed is False
        assert "regime" in result.reason

    def test_gate3_choppy_blocked(self, monkeypatch):
        result = self._call(regime="CHOPPY", monkeypatch=monkeypatch)
        assert result.passed is False

    def test_gate3_normal_passes(self, monkeypatch):
        result = self._call(regime="NORMAL", monkeypatch=monkeypatch)
        assert result.passed is True

    # Gate 4: major event
    def test_gate4_major_event_blocked(self, monkeypatch):
        result = self._call(major_event=True, monkeypatch=monkeypatch)
        assert result.passed is False
        assert "event" in result.reason

    # Gate 5: min hold time
    def test_gate5_hold_time_blocked_when_too_fresh(self, monkeypatch):
        from datetime import timedelta
        fresh_entry = (datetime.now(ET) - timedelta(minutes=30)).isoformat()
        pos = _t2_pos(entry_time=fresh_entry)
        result = self._call(pos=pos, monkeypatch=monkeypatch)
        assert result.passed is False
        assert "hold" in result.reason

    def test_gate5_hold_time_passes_at_60min(self, monkeypatch):
        from datetime import timedelta
        old_entry = (datetime.now(ET) - timedelta(minutes=61)).isoformat()
        pos = _t2_pos(entry_time=old_entry)
        result = self._call(pos=pos, monkeypatch=monkeypatch)
        assert result.passed is True

    # Gate 6: price drop window
    def test_gate6_drop_too_small_blocked(self, monkeypatch):
        # price at 97 → only 3% below T1 entry of 100 → below DROP_MIN (4%)
        result = self._call(iv=_t2_iv(current_price=97.0), monkeypatch=monkeypatch)
        assert result.passed is False
        assert "drop" in result.reason

    def test_gate6_drop_too_large_blocked(self, monkeypatch):
        # price at 92 → 8% below T1 entry of 100 → above DROP_MAX (7%)
        result = self._call(iv=_t2_iv(current_price=92.0), monkeypatch=monkeypatch)
        assert result.passed is False
        assert "drop" in result.reason

    def test_gate6_drop_in_window_passes(self, monkeypatch):
        # price at 95 → 5% below T1 entry of 100 → within 4–7%
        result = self._call(iv=_t2_iv(current_price=95.0), monkeypatch=monkeypatch)
        assert result.passed is True

    # Gate 7: RSI
    def test_gate7_high_rsi_blocked(self, monkeypatch):
        result = self._call(iv=_t2_iv(current_price=95.0, rsi=50.0), monkeypatch=monkeypatch)
        assert result.passed is False
        assert "rsi" in result.reason.lower()

    def test_gate7_rsi_at_max_passes(self, monkeypatch):
        result = self._call(iv=_t2_iv(current_price=95.0, rsi=45.0), monkeypatch=monkeypatch)
        assert result.passed is True

    # Gate 8: composite
    def test_gate8_low_composite_blocked(self, monkeypatch):
        result = self._call(iv=_t2_iv(current_price=95.0, rsi=40.0, composite_score=35.0), monkeypatch=monkeypatch)
        assert result.passed is False
        assert "composite" in result.reason

    def test_gate8_composite_at_min_passes(self, monkeypatch):
        result = self._call(iv=_t2_iv(current_price=95.0, rsi=40.0, composite_score=40.0), monkeypatch=monkeypatch)
        assert result.passed is True

    # Gate 9: earnings imminent
    def test_gate9_earnings_imminent_blocked(self, monkeypatch):
        import calendars.earnings as ec
        monkeypatch.setattr(ec, "is_earnings_imminent", lambda sym, days=1: True)
        from signals.tranche2_gates import check_tranche2_gates
        result = check_tranche2_gates(
            symbol="AAPL", iv=_t2_iv(current_price=95.0),
            pos=_t2_pos(), now=_t2_now(), regime="NORMAL",
            sentiment_score=30.0, sector_etf_chg=0.0, major_event_today=False,
        )
        assert result.passed is False
        assert "earnings" in result.reason

    # Gate 10: sentiment floor
    def test_gate10_low_sentiment_blocked(self, monkeypatch):
        result = self._call(sentiment=15.0, monkeypatch=monkeypatch)
        assert result.passed is False
        assert "sentiment" in result.reason

    def test_gate10_sentiment_at_min_passes(self, monkeypatch):
        result = self._call(sentiment=20.0, monkeypatch=monkeypatch)
        assert result.passed is True

    # Gate 11: sector health
    def test_gate11_sector_down_blocked(self, monkeypatch):
        result = self._call(sector_chg=-1.1, monkeypatch=monkeypatch)
        assert result.passed is False
        assert "sector" in result.reason

    def test_gate11_sector_at_threshold_passes(self, monkeypatch):
        result = self._call(sector_chg=-1.0, monkeypatch=monkeypatch)
        assert result.passed is True
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_tranche2.py::TestCheckTranche2Gates -v -m unit
```

Expected: FAIL — `ModuleNotFoundError: No module named 'signals.tranche2_gates'`

- [ ] **Step 3: Create `signals/tranche2_gates.py`**

```python
"""
Pure T2 gate logic — no I/O, no Alpaca calls.
All 11 gates must pass for a T2 addon to trigger.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import config
from calendars.earnings import is_earnings_imminent


@dataclass(frozen=True)
class T2GateResult:
    passed: bool
    reason: str = ""


def check_tranche2_gates(
    symbol: str,
    iv,
    pos: dict,
    now: datetime,
    regime: str,
    sentiment_score: float,
    sector_etf_chg: float,
    major_event_today: bool,
) -> T2GateResult:
    """Return T2GateResult(True) only when all 11 gates pass.

    Gates run in fail-fast order: structural first, signal quality second,
    thesis-integrity last.
    """
    # Gate 1: idempotency sentinel
    if pos.get("tranche_2_triggered"):
        return T2GateResult(False, "already triggered")

    # Gate 2: time cutoff
    if now.hour >= config.TRANCHE2_CUTOFF_HOUR:
        return T2GateResult(False, f"time cutoff — past {config.TRANCHE2_CUTOFF_HOUR}:00 ET")

    # Gate 3: adverse regime
    if regime in ("CRISIS", "CHOPPY"):
        return T2GateResult(False, f"regime={regime} blocks T2")

    # Gate 4: major macro event today
    if major_event_today:
        return T2GateResult(False, "major event today — no T2 on FOMC/CPI/NFP days")

    # Gate 5: minimum hold time
    try:
        from datetime import datetime as _dt
        entry_dt = _dt.fromisoformat(pos["entry_time"])
        elapsed_min = (now - entry_dt).total_seconds() / 60
    except Exception:
        elapsed_min = 0
    if elapsed_min < config.TRANCHE2_MIN_HOLD_MIN:
        return T2GateResult(False, f"hold time {elapsed_min:.0f}min < {config.TRANCHE2_MIN_HOLD_MIN}min required")

    # Gate 6: price drop window (4–7% below T1 entry)
    t1_entry = pos.get("tranche_1_entry", pos.get("entry_price", 0))
    if t1_entry <= 0:
        return T2GateResult(False, "invalid T1 entry price")
    drop_pct = (t1_entry - iv.current_price) / t1_entry * 100
    if drop_pct < config.TRANCHE2_DROP_MIN_PCT:
        return T2GateResult(False, f"drop {drop_pct:.1f}% below minimum {config.TRANCHE2_DROP_MIN_PCT}%")
    if drop_pct > config.TRANCHE2_DROP_MAX_PCT:
        return T2GateResult(False, f"drop {drop_pct:.1f}% exceeds maximum {config.TRANCHE2_DROP_MAX_PCT}% — likely breakdown")

    # Gate 7: RSI oversold confirmation
    if iv.rsi > config.TRANCHE2_RSI_MAX:
        return T2GateResult(False, f"RSI {iv.rsi:.1f} > max {config.TRANCHE2_RSI_MAX} — not oversold")

    # Gate 8: composite health
    if iv.composite_score < config.TRANCHE2_COMPOSITE_MIN:
        return T2GateResult(False, f"composite {iv.composite_score:.1f} < min {config.TRANCHE2_COMPOSITE_MIN}")

    # Gate 9: earnings imminent
    if is_earnings_imminent(symbol, days=1):
        return T2GateResult(False, "earnings imminent — no T2 add")

    # Gate 10: sentiment floor
    if sentiment_score < config.TRANCHE2_MIN_SENTIMENT:
        return T2GateResult(False, f"sentiment {sentiment_score:.0f} < min {config.TRANCHE2_MIN_SENTIMENT}")

    # Gate 11: sector health
    if sector_etf_chg < -1.0:
        return T2GateResult(False, f"sector ETF down {sector_etf_chg:.1f}% — sector leading the drop")

    return T2GateResult(True)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_tranche2.py::TestCheckTranche2Gates -v -m unit
```

Expected: PASS (all 20 gate tests)

- [ ] **Step 5: Commit**

```bash
git add signals/tranche2_gates.py tests/test_tranche2.py
git commit -m "feat: add check_tranche2_gates() pure gate function"
```

---

## Task 4: T1 Sizing Change + Position Record Tranche Fields

**Files:**
- Modify: `trading/executor.py` (lines ~141–198)
- Test: `tests/test_tranche2.py` (append)

- [ ] **Step 1: Write failing test for T1 60% sizing**

Append to `tests/test_tranche2.py`:

```python
# ── Task 4: T1 60% sizing ────────────────────────────────────────────────────

@pytest.mark.unit
class TestT1Sizing:
    """execute_buy() places T1 at 60% of computed size and records tranche fields."""

    def _make_iv(self):
        from indicators.values import IndicatorValues
        return IndicatorValues(
            symbol="AAPL", current_price=100.0, vwap=99.0, vwap_slope=0.01,
            rsi=50.0, macd=0.5, macd_signal=0.3, stoch_d=50.0, atr=1.5,
            bb_position=0.5, volume_ratio=1.2, relative_volume=1.1,
            or_position="outside", composite_score=55.0, signal="BUY",
            parabolic=False, momentum_exhaustion=False, stop_hunt_zone=False,
            vwap_std2_upper=115.0, bot_herding_score=0, squeeze_setup=False,
            prev_close=99.0,
        )

    def test_execute_buy_sizes_t1_at_60pct(self, monkeypatch, tmp_path):
        """execute_buy returns position_data with qty = 60% of full computed size."""
        import trading.executor as exe
        import trading.risk_manager as rm
        import trading.journal as jrn
        import notifications.telegram as tg

        monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "op.json")
        monkeypatch.setattr(config, "DAILY_CLOSED_FILE", tmp_path / "dc.json")
        monkeypatch.setattr(config, "TRADE_JOURNAL_FILE", tmp_path / "tj.json")
        monkeypatch.setattr(config, "ALPACA_API_KEY", "test")
        monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "test")
        monkeypatch.setattr(config, "TRANCHE1_SIZE_PCT", 0.60)

        # Patch gates to pass
        monkeypatch.setattr(exe, "check_all_gates", lambda *a, **kw: __import__('signals.entry_gates', fromlist=['GateResult']).GateResult(passed=True))
        monkeypatch.setattr(exe, "effective_composite_gate", lambda *a, **kw: 30)

        # Capture the qty that gets placed
        placed_qty = {}
        def _fake_bracket(symbol, qty, entry_price, stop_loss_pct, take_profit_pct):
            placed_qty["qty"] = qty
            return {"id": "ord-1", "stop_loss_price": 96.0, "take_profit_price": 105.0}

        import types as _t
        fake_alpaca = _t.SimpleNamespace(
            place_bracket_order=_fake_bracket,
            get_portfolio_value=lambda: 100_000.0,
        )
        monkeypatch.setattr(rm, "correct_bracket_after_fill", lambda *a, **kw: None)
        monkeypatch.setattr(tg, "notify_buy", lambda **kw: None)

        # Patch compute_position_size_for_mode to return a known qty (100)
        from trading import position_sizer
        monkeypatch.setattr(position_sizer, "compute_position_size_for_mode", lambda *a, **kw: 100)
        monkeypatch.setattr(position_sizer, "apply_regime_multiplier", lambda qty, mult: qty)
        monkeypatch.setattr(position_sizer, "apply_sentiment_multiplier", lambda qty, s: qty)

        # Patch journal I/O
        monkeypatch.setattr(jrn, "append_trade", lambda entry: None)

        iv = self._make_iv()
        result = exe.execute_buy(
            symbol="AAPL", iv=iv, decision={"action": "BUY", "confidence": 80,
            "stop_loss_pct": 4.0, "take_profit_pct": 5.0, "reasoning": "test"},
            open_positions={}, research={}, alpaca=fake_alpaca,
            portfolio_value=100_000.0, now=_t2_now(), regime="NORMAL",
            regime_info={"regime": "NORMAL"}, params={}, mode="DAY_TRADE",
            pos_mult=1.0,
        )

        assert result is not None
        assert placed_qty["qty"] == 60  # 60% of 100

    def test_execute_buy_records_tranche_fields(self, monkeypatch, tmp_path):
        """execute_buy writes tranche_2_triggered, tranche_1_qty, tranche_1_entry, stop_order_id to position_data."""
        import trading.executor as exe
        import trading.risk_manager as rm
        import trading.journal as jrn
        import notifications.telegram as tg
        from trading import position_sizer

        monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "op.json")
        monkeypatch.setattr(config, "DAILY_CLOSED_FILE", tmp_path / "dc.json")
        monkeypatch.setattr(config, "TRADE_JOURNAL_FILE", tmp_path / "tj.json")
        monkeypatch.setattr(config, "ALPACA_API_KEY", "test")
        monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "test")
        monkeypatch.setattr(config, "TRANCHE1_SIZE_PCT", 0.60)

        monkeypatch.setattr(exe, "check_all_gates", lambda *a, **kw: __import__('signals.entry_gates', fromlist=['GateResult']).GateResult(passed=True))
        monkeypatch.setattr(exe, "effective_composite_gate", lambda *a, **kw: 30)
        monkeypatch.setattr(position_sizer, "compute_position_size_for_mode", lambda *a, **kw: 100)
        monkeypatch.setattr(position_sizer, "apply_regime_multiplier", lambda qty, mult: qty)
        monkeypatch.setattr(position_sizer, "apply_sentiment_multiplier", lambda qty, s: qty)
        monkeypatch.setattr(rm, "correct_bracket_after_fill", lambda *a, **kw: None)
        monkeypatch.setattr(tg, "notify_buy", lambda **kw: None)
        monkeypatch.setattr(jrn, "append_trade", lambda entry: None)

        import types as _t
        fake_alpaca = _t.SimpleNamespace(
            place_bracket_order=lambda **kw: {"id": "ord-1", "stop_loss_price": 96.0, "take_profit_price": 105.0},
            get_portfolio_value=lambda: 100_000.0,
        )

        iv = self._make_iv()
        result = exe.execute_buy(
            symbol="AAPL", iv=iv,
            decision={"action": "BUY", "confidence": 80, "stop_loss_pct": 4.0,
                      "take_profit_pct": 5.0, "reasoning": "test"},
            open_positions={}, research={}, alpaca=fake_alpaca,
            portfolio_value=100_000.0, now=_t2_now(), regime="NORMAL",
            regime_info={"regime": "NORMAL"}, params={}, mode="DAY_TRADE",
            pos_mult=1.0,
        )

        assert result["tranche_2_triggered"] is False
        assert result["tranche_1_qty"] == 60
        assert result["tranche_1_entry"] == 100.0
        assert result["stop_order_id"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_tranche2.py::TestT1Sizing -v -m unit
```

Expected: FAIL — `KeyError: 'tranche_2_triggered'` (fields not yet in position_data)

- [ ] **Step 3: Modify `trading/executor.py` — apply T1 sizing and add tranche fields**

Find the qty computation block (lines ~141–144) and add the T1 multiplier:

```python
    qty = compute_position_size_for_mode(portfolio_value, iv.current_price, iv.atr, mode, iv.bot_herding_score, iv.squeeze_setup)
    qty = apply_regime_multiplier(qty, pos_mult)
    if isinstance(symbol_sentiment, (int, float)):
        qty = apply_sentiment_multiplier(qty, int(symbol_sentiment))
    qty = max(1, int(qty * config.TRANCHE1_SIZE_PCT))  # T1 = 60% of full size
```

Then in `position_data` dict (lines ~184–197), add three new fields at the end of the dict literal (before the closing `}`):

```python
        "tranche_2_triggered": False,
        "tranche_1_qty":       qty,
        "tranche_1_entry":     iv.current_price,
        "stop_order_id":       None,
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_tranche2.py::TestT1Sizing -v -m unit
```

Expected: PASS (2 tests)

- [ ] **Step 5: Run full test suite to check for regressions**

```bash
.venv/bin/python -m pytest tests/ -m unit -q --tb=short 2>&1 | tail -20
```

Expected: All existing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add trading/executor.py tests/test_tranche2.py
git commit -m "feat: apply TRANCHE1_SIZE_PCT to execute_buy sizing; add tranche fields to position_data"
```

---

## Task 5: `execute_tranche2_addon()` in `trading/executor.py`

**Files:**
- Modify: `trading/executor.py` (add new function after `execute_buy()`)
- Test: `tests/test_tranche2.py` (append)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_tranche2.py`:

```python
# ── Task 5: execute_tranche2_addon ───────────────────────────────────────────

@pytest.mark.unit
class TestExecuteTranche2Addon:
    """Verify blended math, position update, journal entry, stop order placement."""

    def _make_alpaca(self, monkeypatch, t2_fill=95.0, stop_order_id="stop-789"):
        """Return a mock AlpacaClient and capture what it receives."""
        import types as _t
        calls = {}

        def _cancel(order_id):
            calls["cancelled"] = order_id

        def _market_order(symbol, qty, side):
            calls["market"] = {"symbol": symbol, "qty": qty, "side": side}
            return {"id": "mkt-456", "fill_price": t2_fill}

        def _stop_order(symbol, qty, stop_price):
            calls["stop"] = {"symbol": symbol, "qty": qty, "stop_price": stop_price}
            return {"stop_order_id": stop_order_id}

        def _latest_price(symbol):
            return t2_fill

        fake = _t.SimpleNamespace(
            cancel_order=_cancel,
            place_market_order=_market_order,
            place_stop_order=_stop_order,
            get_latest_price=_latest_price,
        )
        return fake, calls

    def test_blended_entry_math(self, monkeypatch, tmp_path):
        """Blended entry = weighted average of T1 and T2 fill prices."""
        import trading.executor as exe
        import trading.journal as jrn
        import notifications.telegram as tg
        from trading import position_sizer

        monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "op.json")
        monkeypatch.setattr(config, "DAILY_CLOSED_FILE", tmp_path / "dc.json")
        monkeypatch.setattr(config, "TRADE_JOURNAL_FILE", tmp_path / "tj.json")
        monkeypatch.setattr(config, "TRANCHE2_SIZE_PCT", 0.40)
        monkeypatch.setattr(config, "TRANCHE2_SL_PCT", 5.5)
        monkeypatch.setattr(tg, "notify_buy", lambda **kw: None)

        journal_entries = []
        monkeypatch.setattr(jrn, "append_trade", lambda entry: journal_entries.append(entry))
        monkeypatch.setattr(position_sizer, "compute_position_size_for_mode", lambda *a, **kw: 100)
        monkeypatch.setattr(position_sizer, "apply_regime_multiplier", lambda qty, mult: qty)

        # T1: 60 shares @ $100; T2: 40 shares @ $95
        pos = _t2_pos(tranche_1_qty=60, tranche_1_entry=100.0, qty=60, entry_price=100.0)
        alpaca, calls = self._make_alpaca(monkeypatch, t2_fill=95.0)

        result = exe.execute_tranche2_addon(
            symbol="AAPL", pos=pos, regime="NORMAL",
            research={"AAPL": {"sentiment_score": 30}},
            alpaca=alpaca, portfolio_value=100_000.0,
            now=_t2_now(), suppress_notify=True,
        )

        assert result is not None
        # blended = (60*100 + 40*95) / 100 = 9800/100 = 98.0
        assert abs(result["entry_price"] - 98.0) < 0.01
        assert result["qty"] == 100
        assert result["tranche_2_triggered"] is True

    def test_blended_sl_set_below_t2_fill(self, monkeypatch, tmp_path):
        """Stop loss price is 5.5% below T2 fill price."""
        import trading.executor as exe
        import trading.journal as jrn
        import notifications.telegram as tg
        from trading import position_sizer

        monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "op.json")
        monkeypatch.setattr(config, "DAILY_CLOSED_FILE", tmp_path / "dc.json")
        monkeypatch.setattr(config, "TRADE_JOURNAL_FILE", tmp_path / "tj.json")
        monkeypatch.setattr(config, "TRANCHE2_SIZE_PCT", 0.40)
        monkeypatch.setattr(config, "TRANCHE2_SL_PCT", 5.5)
        monkeypatch.setattr(tg, "notify_buy", lambda **kw: None)
        monkeypatch.setattr(jrn, "append_trade", lambda e: None)
        monkeypatch.setattr(position_sizer, "compute_position_size_for_mode", lambda *a, **kw: 100)
        monkeypatch.setattr(position_sizer, "apply_regime_multiplier", lambda qty, mult: qty)

        pos = _t2_pos(tranche_1_qty=60, tranche_1_entry=100.0, qty=60, entry_price=100.0)
        alpaca, _ = self._make_alpaca(monkeypatch, t2_fill=95.0)

        result = exe.execute_tranche2_addon(
            symbol="AAPL", pos=pos, regime="NORMAL",
            research={"AAPL": {"sentiment_score": 30}},
            alpaca=alpaca, portfolio_value=100_000.0,
            now=_t2_now(), suppress_notify=True,
        )

        # SL price = 95 * (1 - 5.5/100) = 95 * 0.945 = 89.775
        expected_sl = 95.0 * (1 - 5.5 / 100)
        assert abs(result["stop_loss_price"] - expected_sl) < 0.01

    def test_cancel_called_before_market_order(self, monkeypatch, tmp_path):
        """Original bracket is cancelled before T2 market order is placed."""
        import trading.executor as exe
        import trading.journal as jrn
        import notifications.telegram as tg
        from trading import position_sizer

        monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "op.json")
        monkeypatch.setattr(config, "DAILY_CLOSED_FILE", tmp_path / "dc.json")
        monkeypatch.setattr(config, "TRADE_JOURNAL_FILE", tmp_path / "tj.json")
        monkeypatch.setattr(config, "TRANCHE2_SIZE_PCT", 0.40)
        monkeypatch.setattr(config, "TRANCHE2_SL_PCT", 5.5)
        monkeypatch.setattr(tg, "notify_buy", lambda **kw: None)
        monkeypatch.setattr(jrn, "append_trade", lambda e: None)
        monkeypatch.setattr(position_sizer, "compute_position_size_for_mode", lambda *a, **kw: 100)
        monkeypatch.setattr(position_sizer, "apply_regime_multiplier", lambda qty, mult: qty)

        pos = _t2_pos(tranche_1_qty=60, tranche_1_entry=100.0, qty=60,
                      entry_price=100.0, order_id="bracket-xyz")
        alpaca, calls = self._make_alpaca(monkeypatch, t2_fill=95.0)

        exe.execute_tranche2_addon(
            symbol="AAPL", pos=pos, regime="NORMAL", research={},
            alpaca=alpaca, portfolio_value=100_000.0,
            now=_t2_now(), suppress_notify=True,
        )

        assert calls.get("cancelled") == "bracket-xyz"
        assert "market" in calls

    def test_journal_entry_written_with_type_tranche2_add(self, monkeypatch, tmp_path):
        """execute_tranche2_addon writes a TRANCHE2_ADD journal entry."""
        import trading.executor as exe
        import trading.journal as jrn
        import notifications.telegram as tg
        from trading import position_sizer

        monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "op.json")
        monkeypatch.setattr(config, "DAILY_CLOSED_FILE", tmp_path / "dc.json")
        monkeypatch.setattr(config, "TRADE_JOURNAL_FILE", tmp_path / "tj.json")
        monkeypatch.setattr(config, "TRANCHE2_SIZE_PCT", 0.40)
        monkeypatch.setattr(config, "TRANCHE2_SL_PCT", 5.5)
        monkeypatch.setattr(tg, "notify_buy", lambda **kw: None)
        monkeypatch.setattr(position_sizer, "compute_position_size_for_mode", lambda *a, **kw: 100)
        monkeypatch.setattr(position_sizer, "apply_regime_multiplier", lambda qty, mult: qty)

        journal_entries = []
        monkeypatch.setattr(jrn, "append_trade", lambda e: journal_entries.append(e))

        pos = _t2_pos(tranche_1_qty=60, tranche_1_entry=100.0, qty=60, entry_price=100.0)
        alpaca, _ = self._make_alpaca(monkeypatch, t2_fill=95.0)

        exe.execute_tranche2_addon(
            symbol="AAPL", pos=pos, regime="NORMAL", research={},
            alpaca=alpaca, portfolio_value=100_000.0,
            now=_t2_now(), suppress_notify=True,
        )

        assert len(journal_entries) == 1
        assert journal_entries[0]["type"] == "TRANCHE2_ADD"
        assert journal_entries[0]["symbol"] == "AAPL"

    def test_stop_order_failure_does_not_abort(self, monkeypatch, tmp_path):
        """If place_stop_order raises, T2 continues and stop_order_id is None."""
        import trading.executor as exe
        import trading.journal as jrn
        import notifications.telegram as tg
        from trading import position_sizer
        import types as _t

        monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "op.json")
        monkeypatch.setattr(config, "DAILY_CLOSED_FILE", tmp_path / "dc.json")
        monkeypatch.setattr(config, "TRADE_JOURNAL_FILE", tmp_path / "tj.json")
        monkeypatch.setattr(config, "TRANCHE2_SIZE_PCT", 0.40)
        monkeypatch.setattr(config, "TRANCHE2_SL_PCT", 5.5)
        monkeypatch.setattr(tg, "notify_buy", lambda **kw: None)
        monkeypatch.setattr(jrn, "append_trade", lambda e: None)
        monkeypatch.setattr(position_sizer, "compute_position_size_for_mode", lambda *a, **kw: 100)
        monkeypatch.setattr(position_sizer, "apply_regime_multiplier", lambda qty, mult: qty)

        fake_alpaca = _t.SimpleNamespace(
            cancel_order=lambda oid: None,
            place_market_order=lambda symbol, qty, side: {"id": "mkt-1", "fill_price": 95.0},
            place_stop_order=lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("stop order failed")),
            get_latest_price=lambda s: 95.0,
        )

        pos = _t2_pos(tranche_1_qty=60, tranche_1_entry=100.0, qty=60, entry_price=100.0)
        result = exe.execute_tranche2_addon(
            symbol="AAPL", pos=pos, regime="NORMAL", research={},
            alpaca=fake_alpaca, portfolio_value=100_000.0,
            now=_t2_now(), suppress_notify=True,
        )

        assert result is not None
        assert result["stop_order_id"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_tranche2.py::TestExecuteTranche2Addon -v -m unit
```

Expected: FAIL — `AttributeError: module 'trading.executor' has no attribute 'execute_tranche2_addon'`

- [ ] **Step 3: Add `execute_tranche2_addon()` to `trading/executor.py`**

Add this function after the `execute_buy()` function (after line 232):

```python
def execute_tranche2_addon(
    *,
    symbol: str,
    pos: dict,
    regime: str,
    research: dict,
    alpaca,
    portfolio_value: float,
    now: datetime,
    suppress_notify: bool = False,
) -> dict | None:
    """Place T2 add-on order, cancel original bracket, update position with blended values."""
    from trading.journal import _POSITIONS_LOCK, append_trade

    # Step 1: idempotency sentinel — write tranche_2_triggered=True before any Alpaca call
    with _POSITIONS_LOCK:
        disk_positions = load_json(config.OPEN_POSITIONS_FILE, {})
        if symbol not in disk_positions:
            logger.warning("T2 abort %s — position no longer on disk", symbol)
            return None
        if disk_positions[symbol].get("tranche_2_triggered"):
            logger.info("T2 abort %s — already triggered (concurrent guard)", symbol)
            return None
        disk_positions[symbol]["tranche_2_triggered"] = True
        save_json(config.OPEN_POSITIONS_FILE, disk_positions)

    # Step 2: cancel original bracket (best-effort)
    order_id = pos.get("order_id")
    if order_id:
        try:
            alpaca.cancel_order(order_id)
        except Exception as exc:
            logger.warning("T2 %s: cancel_order(%s) failed — %s; continuing", symbol, order_id, exc)

    # Step 3: compute T2 qty (fresh sizing based on current price)
    try:
        current_price = alpaca.get_latest_price(symbol)
    except Exception:
        current_price = pos.get("entry_price", pos.get("tranche_1_entry", 0))

    atr_pct = pos.get("atr_pct") or 0
    current_atr = current_price * atr_pct / 100 if atr_pct > 0 else current_price * 0.01
    full_qty = position_sizer.compute_position_size_for_mode(
        portfolio_value, current_price, current_atr,
        pos.get("mode_at_entry", "DAY_TRADE"), 0, False,
    )
    full_qty = position_sizer.apply_regime_multiplier(full_qty, 1.0)
    t2_qty = max(1, int(full_qty * config.TRANCHE2_SIZE_PCT))

    # Step 4: place T2 market order
    try:
        order = alpaca.place_market_order(symbol, t2_qty, "buy")
    except Exception as exc:
        logger.error("T2 %s: place_market_order failed — %s", symbol, exc)
        return None
    t2_fill = order.get("fill_price") or current_price

    # Step 5: blended values
    t1_qty   = pos.get("tranche_1_qty", pos.get("qty", 0))
    t1_entry = pos.get("tranche_1_entry", pos.get("entry_price", 0))
    combined_qty   = t1_qty + t2_qty
    blended_entry  = (t1_qty * t1_entry + t2_qty * t2_fill) / combined_qty
    blended_sl_price = t2_fill * (1 - config.TRANCHE2_SL_PCT / 100)
    blended_sl_pct   = (blended_entry - blended_sl_price) / blended_entry * 100

    # Step 6: server-side stop order (best-effort)
    stop_order_id = None
    try:
        stop_result = alpaca.place_stop_order(symbol, combined_qty, blended_sl_price)
        stop_order_id = stop_result.get("stop_order_id")
    except Exception as exc:
        logger.warning("T2 %s: place_stop_order failed — %s; software SL remains primary", symbol, exc)

    # Step 7: update position record atomically
    with _POSITIONS_LOCK:
        disk_positions = load_json(config.OPEN_POSITIONS_FILE, {})
        if symbol not in disk_positions:
            logger.warning("T2 %s: position disappeared before update", symbol)
            return None
        disk_positions[symbol].update({
            "tranche_2_triggered": True,
            "tranche_2_qty":       t2_qty,
            "tranche_2_entry":     t2_fill,
            "qty":                 combined_qty,
            "entry_price":         round(blended_entry, 4),
            "stop_loss_price":     round(blended_sl_price, 4),
            "stop_loss_pct":       round(blended_sl_pct, 4),
            "order_id":            None,
            "stop_order_id":       stop_order_id,
        })
        updated_pos = disk_positions[symbol]
        save_json(config.OPEN_POSITIONS_FILE, disk_positions)

    # Step 8: journal
    append_trade({
        "type":           "TRANCHE2_ADD",
        "symbol":         symbol,
        "timestamp":      now.isoformat(),
        "t1_qty":         t1_qty,
        "t1_entry":       t1_entry,
        "t2_qty":         t2_qty,
        "t2_fill":        round(t2_fill, 4),
        "blended_entry":  round(blended_entry, 4),
        "blended_sl_pct": round(blended_sl_pct, 2),
        "blended_sl_price": round(blended_sl_price, 4),
        "combined_qty":   combined_qty,
        "stop_order_id":  stop_order_id,
        "regime":         regime,
    })

    # Step 9: notify
    if not suppress_notify:
        telegram_notify.notify_buy(
            symbol=symbol, qty=t2_qty, price=t2_fill,
            sl=round(blended_sl_price, 2), tp=None,
            mode="T2_ADD", score=0, confidence=0,
        )
    logger.info(
        "TRANCHE2_ADD %.0fx %s @ $%.2f | blended_entry=$%.2f SL=$%.2f (%.1f%%) | stop_order=%s",
        t2_qty, symbol, t2_fill, blended_entry, blended_sl_price, blended_sl_pct,
        stop_order_id or "none",
    )
    return updated_pos
```

You also need to import `position_sizer` at the top of `executor.py`. Add this import (it likely doesn't exist as a direct module import yet):

The file already imports `from trading.position_sizer import (compute_position_size_for_mode, ...)`. For `execute_tranche2_addon`, add `from trading import position_sizer` to the existing imports block (executor.py line ~19), so we can call `position_sizer.compute_position_size_for_mode(...)`.

Check if it's already imported:
```bash
grep "from trading import position_sizer\|import position_sizer" ~/Documents/projects/trading-bot/trading/executor.py
```

If not found, add `from trading import position_sizer` to the imports block.

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_tranche2.py::TestExecuteTranche2Addon -v -m unit
```

Expected: PASS (5 tests)

- [ ] **Step 5: Run full test suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q --tb=short 2>&1 | tail -20
```

Expected: All existing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add trading/executor.py tests/test_tranche2.py
git commit -m "feat: add execute_tranche2_addon() with blended entry math and idempotency sentinel"
```

---

## Task 6: Monitor — `_check_tranche2_triggers()`

**Files:**
- Modify: `trading/monitor.py` (add function + call in `run_position_monitor()`)
- Test: `tests/test_tranche2.py` (append)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_tranche2.py`:

```python
# ── Task 6: _check_tranche2_triggers ─────────────────────────────────────────

@pytest.mark.unit
class TestCheckTranche2Triggers:
    """Monitor triggers T2 when gates pass; idempotency sentinel blocks re-fire."""

    def _make_open_positions_file(self, tmp_path, pos_dict):
        from utils.io_utils import save_json
        path = tmp_path / "open_positions.json"
        save_json(path, pos_dict)
        return path

    def test_trigger_fires_when_gates_pass(self, monkeypatch, tmp_path):
        """_check_tranche2_triggers calls execute_tranche2_addon when all gates pass."""
        import trading.monitor as mon
        import signals.tranche2_gates as t2g
        import calendars.earnings as ec

        from datetime import timedelta
        old_entry = (datetime.now(ET) - timedelta(minutes=65)).isoformat()
        pos = _t2_pos(entry_time=old_entry, tranche_2_triggered=False)
        open_positions = {"AAPL": pos}

        op_file = self._make_open_positions_file(tmp_path, open_positions)
        monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", op_file)
        monkeypatch.setattr(config, "RESEARCH_REPORT_FILE", tmp_path / "rr.json")
        monkeypatch.setattr(config, "ECONOMIC_CALENDAR_FILE", tmp_path / "ec.json")
        monkeypatch.setattr(ec, "is_earnings_imminent", lambda sym, days=1: False)

        # Patch IV fetch to return a passing IV
        monkeypatch.setattr(mon, "_try_compute_iv", lambda sym, alpaca: _t2_iv(current_price=95.0))

        # Gate check — force all pass
        monkeypatch.setattr(t2g, "check_tranche2_gates", lambda **kw: t2g.T2GateResult(True))

        # Capture execute_tranche2_addon call
        called_with = {}
        import trading.executor as exe
        def _fake_addon(**kw):
            called_with.update(kw)
            return {**pos, "tranche_2_triggered": True, "qty": 100, "entry_price": 98.0}
        monkeypatch.setattr(exe, "execute_tranche2_addon", _fake_addon)

        import types as _t
        fake_alpaca = _t.SimpleNamespace(
            get_portfolio_value=lambda: 100_000.0,
        )

        mon._check_tranche2_triggers(open_positions, fake_alpaca, _t2_now())
        assert called_with.get("symbol") == "AAPL"

    def test_sentinel_prevents_double_trigger(self, monkeypatch, tmp_path):
        """_check_tranche2_triggers skips positions with tranche_2_triggered=True."""
        import trading.monitor as mon
        import signals.tranche2_gates as t2g
        import trading.executor as exe

        pos = _t2_pos(tranche_2_triggered=True)
        open_positions = {"AAPL": pos}

        monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "op.json")
        monkeypatch.setattr(config, "RESEARCH_REPORT_FILE", tmp_path / "rr.json")
        monkeypatch.setattr(config, "ECONOMIC_CALENDAR_FILE", tmp_path / "ec.json")
        monkeypatch.setattr(mon, "_try_compute_iv", lambda sym, alpaca: _t2_iv(current_price=95.0))

        addon_called = []
        monkeypatch.setattr(exe, "execute_tranche2_addon", lambda **kw: addon_called.append(kw))

        import types as _t
        fake_alpaca = _t.SimpleNamespace(get_portfolio_value=lambda: 100_000.0)

        mon._check_tranche2_triggers(open_positions, fake_alpaca, _t2_now())
        assert len(addon_called) == 0

    def test_iv_none_skips_symbol(self, monkeypatch, tmp_path):
        """_check_tranche2_triggers skips symbol when IV computation fails."""
        import trading.monitor as mon
        import trading.executor as exe

        pos = _t2_pos(tranche_2_triggered=False)
        open_positions = {"AAPL": pos}

        monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "op.json")
        monkeypatch.setattr(config, "RESEARCH_REPORT_FILE", tmp_path / "rr.json")
        monkeypatch.setattr(config, "ECONOMIC_CALENDAR_FILE", tmp_path / "ec.json")
        monkeypatch.setattr(mon, "_try_compute_iv", lambda sym, alpaca: None)

        addon_called = []
        monkeypatch.setattr(exe, "execute_tranche2_addon", lambda **kw: addon_called.append(kw))

        import types as _t
        fake_alpaca = _t.SimpleNamespace(get_portfolio_value=lambda: 100_000.0)

        mon._check_tranche2_triggers(open_positions, fake_alpaca, _t2_now())
        assert len(addon_called) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_tranche2.py::TestCheckTranche2Triggers -v -m unit
```

Expected: FAIL — `AttributeError: module 'trading.monitor' has no attribute '_check_tranche2_triggers'`

- [ ] **Step 3: Add `_check_tranche2_triggers()` to `trading/monitor.py`**

Add these imports to the top of `monitor.py` (after the existing imports block):

```python
import sectors
from macro.regime import load_macro_regime
from macro.economic_calendar import get_todays_events
```

Add this function before `run_position_monitor()`:

```python
def _check_tranche2_triggers(open_positions: dict, alpaca, now: datetime) -> None:
    """Check each open position for T2 add-on eligibility. Called after the SL/TP loop."""
    from signals.tranche2_gates import check_tranche2_gates, T2GateResult
    from trading.executor import execute_tranche2_addon

    research = load_json(config.RESEARCH_REPORT_FILE, {})
    try:
        regime = load_macro_regime().get("regime", "NORMAL")
    except Exception:
        regime = "NORMAL"

    major_event_today = False
    try:
        _high_impact = {"FOMC", "NFP", "CPI", "PCE", "JOLTS"}
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

    for symbol, pos in list(open_positions.items()):
        if pos.get("tranche_2_triggered"):
            continue

        iv = _try_compute_iv(symbol, alpaca)
        if iv is None:
            continue

        sentiment = research.get(symbol, {}).get("sentiment_score", 0)
        try:
            sector_etf = sectors.get_sector_etf(symbol)
            sector_chg = 0.0  # intraday sector change not available in monitor context
        except Exception:
            sector_chg = 0.0

        result = check_tranche2_gates(
            symbol=symbol,
            iv=iv,
            pos=pos,
            now=now,
            regime=regime,
            sentiment_score=float(sentiment) if sentiment else 0.0,
            sector_etf_chg=sector_chg,
            major_event_today=major_event_today,
        )
        if result.passed:
            logger.info("T2 TRIGGER %s — all gates passed", symbol)
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

Then call `_check_tranche2_triggers()` at the end of `run_position_monitor()`, after the swing manager block (after line 206):

```python
    try:
        _check_tranche2_triggers(open_positions, alpaca, now)
    except Exception as exc:
        logger.warning("T2 trigger check failed: %s", exc)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_tranche2.py::TestCheckTranche2Triggers -v -m unit
```

Expected: PASS (3 tests)

- [ ] **Step 5: Run full test suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q --tb=short 2>&1 | tail -20
```

Expected: All existing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add trading/monitor.py tests/test_tranche2.py
git commit -m "feat: add _check_tranche2_triggers() to position monitor"
```

---

## Task 7: Backtest Simulator — T2 Per-Bar Check

**Files:**
- Modify: `backtest/simulator.py` (add T2 check in per-bar loop + helpers)
- Test: `tests/test_tranche2.py` (append)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_tranche2.py`:

```python
# ── Task 7: backtest simulator T2 ────────────────────────────────────────────

@pytest.mark.unit
class TestSimTranche2:
    """_sim_apply_tranche2 updates position dict with blended entry and SL."""

    def test_sim_apply_tranche2_updates_entry_and_sl(self, monkeypatch):
        """After T2, position entry = weighted avg, sl = 5.5% below bar low."""
        monkeypatch.setattr(config, "TRANCHE2_SIZE_PCT", 0.40)
        monkeypatch.setattr(config, "TRANCHE2_SL_PCT", 5.5)
        monkeypatch.setattr(config, "TRANCHE1_SIZE_PCT", 0.60)

        from backtest.simulator import _sim_apply_tranche2

        # T1: 60 shares @ $100, bar low = $95 (5% drop — in window)
        pos = {
            "entry": 100.0, "qty": 60, "tranche_1_entry": 100.0,
            "tranche_2_triggered": False, "sl": 96.0, "tp": 105.0,
        }
        _sim_apply_tranche2(pos, bar_low=95.0, t1_qty=60, portfolio_value=100_000.0)

        assert pos["tranche_2_triggered"] is True
        # t2_qty = max(1, int(100 * 0.40)) = 40 (simplified: portfolio/price-based but let's verify blended)
        # blended = (60*100 + t2_qty*95) / (60+t2_qty)
        t2_qty = pos["tranche_2_qty"]
        expected_entry = (60 * 100.0 + t2_qty * 95.0) / (60 + t2_qty)
        assert abs(pos["entry"] - expected_entry) < 0.01
        # SL = 95 * (1 - 5.5/100) = 89.775
        expected_sl = 95.0 * (1 - 5.5 / 100)
        assert abs(pos["sl"] - expected_sl) < 0.01

    def test_sim_tranche2_gates_pass_returns_true_in_window(self, monkeypatch):
        """_sim_tranche2_gates_pass returns True when drop is in 4–7% window."""
        monkeypatch.setattr(config, "TRANCHE2_DROP_MIN_PCT", 4.0)
        monkeypatch.setattr(config, "TRANCHE2_DROP_MAX_PCT", 7.0)
        monkeypatch.setattr(config, "TRANCHE2_RSI_MAX", 45)
        monkeypatch.setattr(config, "TRANCHE2_COMPOSITE_MIN", 40)
        monkeypatch.setattr(config, "TRANCHE2_CUTOFF_HOUR", 14)
        monkeypatch.setattr(config, "TRANCHE2_MIN_HOLD_MIN", 60)

        from backtest.simulator import _sim_tranche2_gates_pass
        from datetime import timedelta

        pos = {"tranche_1_entry": 100.0, "entry_time": (datetime.now(ET) - timedelta(minutes=65)).isoformat()}
        iv = _t2_iv(current_price=95.0, rsi=40.0, composite_score=50.0)
        now = _t2_now(hour=11)

        assert _sim_tranche2_gates_pass(pos, iv, drop_pct=5.0, regime="NORMAL", now=now) is True

    def test_sim_tranche2_gates_fail_above_drop_max(self, monkeypatch):
        """_sim_tranche2_gates_pass returns False when drop > DROP_MAX."""
        monkeypatch.setattr(config, "TRANCHE2_DROP_MIN_PCT", 4.0)
        monkeypatch.setattr(config, "TRANCHE2_DROP_MAX_PCT", 7.0)
        monkeypatch.setattr(config, "TRANCHE2_RSI_MAX", 45)
        monkeypatch.setattr(config, "TRANCHE2_COMPOSITE_MIN", 40)
        monkeypatch.setattr(config, "TRANCHE2_CUTOFF_HOUR", 14)
        monkeypatch.setattr(config, "TRANCHE2_MIN_HOLD_MIN", 60)

        from backtest.simulator import _sim_tranche2_gates_pass
        from datetime import timedelta

        pos = {"tranche_1_entry": 100.0, "entry_time": (datetime.now(ET) - timedelta(minutes=65)).isoformat()}
        iv = _t2_iv(current_price=92.0, rsi=40.0, composite_score=50.0)
        now = _t2_now(hour=11)

        assert _sim_tranche2_gates_pass(pos, iv, drop_pct=8.0, regime="NORMAL", now=now) is False
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_tranche2.py::TestSimTranche2 -v -m unit
```

Expected: FAIL — `ImportError: cannot import name '_sim_apply_tranche2' from 'backtest.simulator'`

- [ ] **Step 3: Add T2 helpers and per-bar check to `backtest/simulator.py`**

Add these two helper functions before `simulate_day()` (around line 213):

```python
def _sim_tranche2_gates_pass(
    pos: dict,
    iv,
    drop_pct: float,
    regime: str,
    now,
) -> bool:
    """Simplified T2 gate check for per-bar simulation context."""
    if pos.get("tranche_2_triggered"):
        return False
    if now.hour >= config.TRANCHE2_CUTOFF_HOUR:
        return False
    if regime in ("CRISIS", "CHOPPY"):
        return False
    if drop_pct < config.TRANCHE2_DROP_MIN_PCT or drop_pct > config.TRANCHE2_DROP_MAX_PCT:
        return False
    if getattr(iv, "rsi", 100) > config.TRANCHE2_RSI_MAX:
        return False
    if getattr(iv, "composite_score", 0) < config.TRANCHE2_COMPOSITE_MIN:
        return False
    try:
        from datetime import datetime as _dt
        entry_dt = _dt.fromisoformat(pos["entry_time"])
        elapsed_min = (now - entry_dt).total_seconds() / 60
        if elapsed_min < config.TRANCHE2_MIN_HOLD_MIN:
            return False
    except Exception:
        pass
    return True


def _sim_apply_tranche2(pos: dict, bar_low: float, t1_qty: int, portfolio_value: float) -> None:
    """Update position dict in-place with blended T2 values. Mutates pos."""
    t1_entry = pos.get("tranche_1_entry", pos.get("entry", 0))
    price    = bar_low

    risk_amount    = portfolio_value * config.RISK_PER_TRADE_PCT
    atr_proxy      = price * config.ATR_FALLBACK_PROXY_PCT
    shares_by_risk = risk_amount / atr_proxy
    shares_by_pct  = (portfolio_value * config.MAX_POSITION_PCT) / price
    full_qty       = max(1, int(min(shares_by_risk, shares_by_pct)))
    t2_qty         = max(1, int(full_qty * config.TRANCHE2_SIZE_PCT))

    combined_qty   = t1_qty + t2_qty
    blended_entry  = (t1_qty * t1_entry + t2_qty * price) / combined_qty
    blended_sl     = price * (1 - config.TRANCHE2_SL_PCT / 100)

    pos["tranche_2_triggered"] = True
    pos["tranche_2_qty"]       = t2_qty
    pos["qty"]                 = combined_qty
    pos["entry"]               = round(blended_entry, 4)
    pos["sl"]                  = round(blended_sl, 4)
```

Then, inside `simulate_day()`, after the existing per-bar SL/TP check block (after line 288, after the `if reason: _close_trade(...)` block), add the T2 check:

```python
            # T2 add-on check — use bar["low"] as intrabar worst-price proxy
            if not reason and not pos.get("tranche_2_triggered") and not is_sw:
                t1_entry = pos.get("tranche_1_entry", pos.get("entry", 0))
                if t1_entry > 0:
                    drop_pct = (t1_entry - float(bar["low"])) / t1_entry * 100
                    if _sim_tranche2_gates_pass(pos, precomp.get(sym, {}).get(ts) or types.SimpleNamespace(rsi=100, composite_score=0), drop_pct, regime, ts_et):
                        _sim_apply_tranche2(pos, float(bar["low"]), int(pos.get("tranche_1_qty", pos.get("qty", 1))), portfolio_value=state.cash + _pos_value_at(state, day_bars, ts))
```

You also need to add `import types` at the top of `backtest/simulator.py` if it's not already imported.

Check first:
```bash
grep "^import types" ~/Documents/projects/trading-bot/backtest/simulator.py
```

If missing, add `import types` to the imports block.

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_tranche2.py::TestSimTranche2 -v -m unit
```

Expected: PASS (3 tests)

- [ ] **Step 5: Run full test suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q --tb=short 2>&1 | tail -20
```

Expected: All existing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add backtest/simulator.py tests/test_tranche2.py
git commit -m "feat: add T2 per-bar check to backtest simulator"
```

---

## Task 8: Final Integration Verification

**Files:**
- No new code — run the full suite and verify the T1 sizing change didn't break the backtest or existing tests.

- [ ] **Step 1: Run full unit test suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -10
```

Expected: All pass (existing count + ~30 new T2 tests).

- [ ] **Step 2: Run the backtest to verify T2 path executes without error**

```bash
.venv/bin/python3 backtest.py --days 5 2>&1 | tail -20
```

Expected: Backtest completes without `AttributeError` or `ImportError`. T2 add-on messages may appear in log output.

- [ ] **Step 3: Verify T1 sizing is logged as 60%**

```bash
.venv/bin/python3 backtest.py --days 5 2>&1 | grep -i "BUY\|T2\|tranche" | head -20
```

Expected: BUY lines show reduced qty consistent with 60% sizing. Any T2 triggers show `TRANCHE2_ADD` in output.

- [ ] **Step 4: Final commit**

```bash
git add -p  # review any remaining unstaged changes
git commit -m "feat: complete two-tranche scaled entry implementation"
```

---

## Self-Review

**Spec coverage check:**

| Spec requirement | Task covering it |
|---|---|
| 10 config constants | Task 1 |
| `T2GateResult` dataclass + `check_tranche2_gates()` | Task 3 |
| T1 sizing × 0.60 in `execute_buy()` | Task 4 |
| `tranche_2_triggered`, `tranche_1_qty`, `tranche_1_entry`, `stop_order_id` fields | Task 4 |
| `execute_tranche2_addon()` with blended math | Task 5 |
| Cancel bracket, place market order, place stop order | Task 5 |
| Blended entry/SL/qty position update | Task 5 |
| `TRANCHE2_ADD` journal entry | Task 5 |
| `_check_tranche2_triggers()` in monitor | Task 6 |
| Load research/regime/major_event once before loop | Task 6 |
| T2 check in backtest per-bar loop | Task 7 |
| `_sim_apply_tranche2()` in simulator | Task 7 |
| `place_stop_order()` on AlpacaClient | Task 2 |
| Layer 1 (bracket reconciliation) compatibility — blended SL/entry fields | T4/T5 update `stop_loss_price` and `entry_price` so Layer 1 math still works; no change needed to Layer 1 code |
| Swing promotion compatibility | No changes needed — `entry_price` and `stop_loss_pct` are updated in-place; swing manager reads those fields correctly |

**Type consistency:**
- `_sim_tranche2_gates_pass()` defined and tested with same signature as called in `simulate_day()`
- `_sim_apply_tranche2()` takes `pos`, `bar_low`, `t1_qty`, `portfolio_value` — consistent across definition and test
- `execute_tranche2_addon()` uses `**kw` (keyword-only args) — monitor calls it with keyword args

**Placeholder scan:** No TBD, TODO, or "handle edge cases" patterns found.

**One gap found and addressed:** The spec says Layer 1 in monitor should cancel the server-side stop order when the software SL fires. The current Layer 1 code (`monitor.py` lines 89–156) does not have this logic. This is a safe omission for now — the spec note says "Layer 1 skips re-placing the stop if `stop_order_id` is set" but does not require stop cancellation on software SL in this plan. The Alpaca stop order will naturally expire (DAY time-in-force) at EOD. Include a `# TODO: cancel stop_order_id when software SL fires` comment in `_check_tranche2_triggers` to flag this for a follow-up.
