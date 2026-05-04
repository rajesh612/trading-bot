# Bot Resilience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate duplicate BUY orders after crash-restart, add two-reading flash-crash confirmation with optional Massive/Polygon cross-check, auto-clear false-positive lockouts, and detect crash loops before they run wild.

**Architecture:** Four independent layers added to the existing startup sequence and market monitor: (1) crash loop gate using `bot_health.json`, (2) startup orphan-order drain via Alpaca Trading API, (3) two-reading SPY confirmation before setting the 48h lockout, (4) auto-clear lockout in the 8:55 AM `fetch_macro_regime()` job. All layers degrade gracefully — no paid subscription required.

**Tech Stack:** Python 3.11+, alpaca-py (`TradingClient`, `GetOrdersRequest`), `requests` (already in requirements), `json`/`pathlib` for state files, pytest + monkeypatch for unit tests.

**Spec:** `docs/superpowers/specs/2026-05-03-bot-resilience-design.md`

---

## File Map

| File | Role |
|---|---|
| `config.py` | 7 new constants — one source of truth for all thresholds |
| `main.py` | `_reset_bot_health()`, `_check_crash_loop()` helpers; wired into `main()` and `_handle_shutdown()` + orphan drain in `_reconcile_positions()` |
| `alpaca_client.py` | `get_open_buy_orders()`, `cancel_order()` |
| `macro_regime.py` | `_evaluate_lockout_recovery()` pure helper; `check_flash_crash()` uses config constant; `load_macro_regime()` + `fetch_macro_regime()` include `flash_crash_candidate` field |
| `trading_agent.py` | `_verify_spy_with_polygon()`, `_write_flash_crash_candidate()`, `_clear_flash_crash_candidate()`, `_handle_flash_crash_stage2()`, `_trigger_flash_crash_lockout()`; updated `run_market_monitor()`; `_set_crisis_lockout()` clears candidate; add `import requests` |
| `trading_bot_start.plist` | `KeepAlive={SuccessfulExit: false}` + `ThrottleInterval=15` |
| `tests/test_foundation.py` | 6 new test classes appended at end of file |
| `README.md` | Updated Crash Recovery section; remove manual one-liner; add `bot_health.json` to state table; add systemd note |
| `.env.example` | Add `POLYGON_API_KEY=` with massive.com comment |
| `.gitignore` | Add `bot_health.json` |

---

## Task 1: Config constants + .gitignore

**Files:**
- Modify: `config.py` (append after last line)
- Modify: `.gitignore`

- [ ] **Step 1: Add 7 constants to config.py**

Append after the last line of `config.py`:

```python

# ── Crash Recovery ────────────────────────────────────────────────────────────
BOT_HEALTH_FILE              = BASE_DIR / "bot_health.json"
CRASH_LOOP_THRESHOLD         = 3      # restarts within window before backoff triggers
CRASH_LOOP_WINDOW_MINUTES    = 10     # rolling window for crash counting
CRASH_BACKOFF_MINUTES        = 30     # pause before resuming after crash loop
FLASH_CRASH_SPY_DROP_PCT     = -2.5   # replaces hardcoded -2.5 in check_flash_crash()
FLASH_CRASH_VIX_RECOVERY     = 35     # auto-clear lockout if VIX drops below this
FLASH_CRASH_SPY_RECOVERY_PCT = -5.0   # auto-clear lockout if SPY 5d return above this
```

- [ ] **Step 2: Add bot_health.json to .gitignore**

Add to the `# ── Runtime state ──` section of `.gitignore`:

```
bot_health.json
```

- [ ] **Step 3: Commit**

```bash
git add config.py .gitignore
git commit -m "config: add 7 crash-recovery constants + gitignore bot_health.json"
```

---

## Task 2: Crash loop detection helpers + tests

**Files:**
- Modify: `main.py` (add two helpers before `_reconcile_positions`)
- Modify: `tests/test_foundation.py` (append new class)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_foundation.py`:

```python

# ── Crash loop detection ───────────────────────────────────────────────────────

@pytest.mark.unit
class TestCrashLoopDetection:
    def _write_health(self, tmp_path, **overrides):
        base = {
            "crash_count": 0,
            "window_start": datetime.now(ET).isoformat(),
            "backoff_until": None,
        }
        base.update(overrides)
        (tmp_path / "bot_health.json").write_text(json.dumps(base))

    def test_missing_file_creates_count_of_one(self, tmp_path):
        import config
        from main import _check_crash_loop
        config.BOT_HEALTH_FILE = tmp_path / "bot_health.json"
        config.CRASH_LOOP_THRESHOLD = 5
        config.CRASH_LOOP_WINDOW_MINUTES = 10
        config.CRASH_BACKOFF_MINUTES = 30
        _check_crash_loop()
        data = json.loads((tmp_path / "bot_health.json").read_text())
        assert data["crash_count"] == 1
        assert data["backoff_until"] is None

    def test_count_increments_within_window(self, tmp_path):
        import config
        from main import _check_crash_loop
        config.BOT_HEALTH_FILE = tmp_path / "bot_health.json"
        config.CRASH_LOOP_THRESHOLD = 5
        config.CRASH_LOOP_WINDOW_MINUTES = 10
        config.CRASH_BACKOFF_MINUTES = 30
        self._write_health(tmp_path, crash_count=2)
        _check_crash_loop()
        data = json.loads((tmp_path / "bot_health.json").read_text())
        assert data["crash_count"] == 3
        assert data["backoff_until"] is None

    def test_expired_window_resets_before_increment(self, tmp_path):
        import config
        from datetime import timedelta
        from main import _check_crash_loop
        config.BOT_HEALTH_FILE = tmp_path / "bot_health.json"
        config.CRASH_LOOP_THRESHOLD = 5
        config.CRASH_LOOP_WINDOW_MINUTES = 10
        config.CRASH_BACKOFF_MINUTES = 30
        old = (datetime.now(ET) - timedelta(minutes=15)).isoformat()
        self._write_health(tmp_path, crash_count=4, window_start=old)
        _check_crash_loop()
        data = json.loads((tmp_path / "bot_health.json").read_text())
        assert data["crash_count"] == 1  # reset to 0, then incremented

    def test_threshold_triggers_backoff_then_resets(self, tmp_path, monkeypatch):
        import config
        import main as main_mod
        from main import _check_crash_loop
        config.BOT_HEALTH_FILE = tmp_path / "bot_health.json"
        config.CRASH_LOOP_THRESHOLD = 3
        config.CRASH_LOOP_WINDOW_MINUTES = 10
        config.CRASH_BACKOFF_MINUTES = 30
        monkeypatch.setattr(main_mod.time, "sleep", lambda _: None)
        self._write_health(tmp_path, crash_count=2)
        _check_crash_loop()
        data = json.loads((tmp_path / "bot_health.json").read_text())
        # After backoff completes, _reset_bot_health() sets count=0
        assert data["crash_count"] == 0
        assert data["backoff_until"] is None

    def test_reset_writes_zero_count(self, tmp_path):
        import config
        from main import _reset_bot_health
        config.BOT_HEALTH_FILE = tmp_path / "bot_health.json"
        self._write_health(tmp_path, crash_count=5, backoff_until="sometime")
        _reset_bot_health()
        data = json.loads((tmp_path / "bot_health.json").read_text())
        assert data["crash_count"] == 0
        assert data["backoff_until"] is None
```

- [ ] **Step 2: Run tests — confirm they fail**

```bash
.venv/bin/pytest tests/test_foundation.py::TestCrashLoopDetection -v
```

Expected: `ImportError` or `AttributeError` — `_check_crash_loop` does not exist yet.

- [ ] **Step 3: Add imports to main.py**

main.py already imports `time`, `datetime`, `os`, `json` is missing. Add `json` and `timedelta`:

```python
# Change line 8 from:
from datetime import datetime
# To:
from datetime import datetime, timedelta
```

Also add `import json` after `import os` (line 3):

```python
import json
```

- [ ] **Step 4: Add helpers to main.py**

Add both functions before the `_reconcile_positions` function (around line 299):

```python
def _reset_bot_health() -> None:
    """Write crash_count=0 to bot_health.json. Called on clean shutdown."""
    try:
        data = {
            "crash_count": 0,
            "window_start": datetime.now(ET).isoformat(),
            "backoff_until": None,
        }
        config.BOT_HEALTH_FILE.write_text(json.dumps(data, indent=2))
    except Exception as exc:
        logger.warning(f"Could not reset bot_health.json: {exc}")


def _check_crash_loop() -> None:
    """Increment crash counter and sleep CRASH_BACKOFF_MINUTES if loop detected."""
    now = datetime.now(ET)
    try:
        if config.BOT_HEALTH_FILE.exists():
            data = json.loads(config.BOT_HEALTH_FILE.read_text())
        else:
            data = {"crash_count": 0, "window_start": now.isoformat(), "backoff_until": None}

        window_start = datetime.fromisoformat(data.get("window_start", now.isoformat()))
        if (now - window_start).total_seconds() / 60 > config.CRASH_LOOP_WINDOW_MINUTES:
            data["crash_count"] = 0
            data["window_start"] = now.isoformat()

        crash_count = data.get("crash_count", 0)
        if crash_count == 0:
            data["window_start"] = now.isoformat()
        crash_count += 1
        data["crash_count"] = crash_count
        config.BOT_HEALTH_FILE.write_text(json.dumps(data, indent=2))

        if crash_count >= config.CRASH_LOOP_THRESHOLD:
            backoff_until = now + timedelta(minutes=config.CRASH_BACKOFF_MINUTES)
            data["backoff_until"] = backoff_until.isoformat()
            config.BOT_HEALTH_FILE.write_text(json.dumps(data, indent=2))
            logger.critical(
                f"CRASH LOOP: {crash_count} crashes in {config.CRASH_LOOP_WINDOW_MINUTES} min "
                f"— pausing {config.CRASH_BACKOFF_MINUTES} min until "
                f"{backoff_until.strftime('%H:%M ET')}"
            )
            time.sleep((backoff_until - datetime.now(ET)).total_seconds())
            _reset_bot_health()
    except Exception as exc:
        logger.warning(f"Crash loop check failed: {exc}")
```

- [ ] **Step 5: Run tests — confirm they pass**

```bash
.venv/bin/pytest tests/test_foundation.py::TestCrashLoopDetection -v
```

Expected: 5 PASSED.

- [ ] **Step 6: Commit**

```bash
git add main.py tests/test_foundation.py
git commit -m "feat: add crash loop detection helpers (_check_crash_loop, _reset_bot_health)"
```

---

## Task 3: Wire crash loop gate into main() and _handle_shutdown()

**Files:**
- Modify: `main.py`

No new unit tests — helpers are covered by Task 2. Wiring is verified manually.

- [ ] **Step 1: Call `_check_crash_loop()` at top of `main()`**

In `main()`, add the call immediately after `_acquire_instance_lock()` (around line 531):

```python
def main():
    _acquire_instance_lock()
    _check_crash_loop()          # ← add this line
    logger.info("Trading Bot initialising…")
```

- [ ] **Step 2: Call `_reset_bot_health()` in `_handle_shutdown()`**

`_handle_shutdown()` currently reads:

```python
def _handle_shutdown(signum, frame):
    logger.info("Shutdown signal received — stopping.")
    _clear_pid()
    scheduler.shutdown(wait=False)
    sys.exit(0)
```

Change to:

```python
def _handle_shutdown(signum, frame):
    logger.info("Shutdown signal received — stopping.")
    _clear_pid()
    scheduler.shutdown(wait=False)
    _reset_bot_health()          # ← add: write crash_count=0 before exit
    sys.exit(0)
```

- [ ] **Step 3: Manual verification**

```bash
./start.sh
# Wait 5 seconds
./stop.sh
cat bot_health.json
```

Expected output: `"crash_count": 0, "backoff_until": null`

- [ ] **Step 4: Commit**

```bash
git add main.py
git commit -m "feat: wire crash loop gate into main() startup and clean shutdown reset"
```

---

## Task 4: Orphan-order drain — alpaca_client + _reconcile_positions + tests

**Files:**
- Modify: `alpaca_client.py`
- Modify: `main.py` (`_reconcile_positions`)
- Modify: `tests/test_foundation.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_foundation.py`:

```python

# ── Orphan-order drain ────────────────────────────────────────────────────────

@pytest.mark.unit
class TestOrphanOrderDrain:
    def _make_client(self, monkeypatch, orders):
        """Return an AlpacaClient with a mocked _trading that returns given orders."""
        from unittest.mock import MagicMock
        import config
        config.ALPACA_API_KEY = "test-key"
        config.ALPACA_SECRET_KEY = "test-secret"
        config.PAPER_TRADING = True
        from alpaca_client import AlpacaClient
        client = AlpacaClient.__new__(AlpacaClient)
        mock_trading = MagicMock()
        mock_trading.get_orders.return_value = orders
        client._trading = mock_trading
        return client

    def _make_order(self, order_id, symbol):
        from unittest.mock import MagicMock
        o = MagicMock()
        o.id = order_id
        o.symbol = symbol
        return o

    def test_get_open_buy_orders_returns_list(self, monkeypatch):
        order = self._make_order("id-1", "AAPL")
        client = self._make_client(monkeypatch, [order])
        result = client.get_open_buy_orders()
        assert result == [{"id": "id-1", "symbol": "AAPL"}]

    def test_get_open_buy_orders_empty_when_none(self, monkeypatch):
        client = self._make_client(monkeypatch, [])
        result = client.get_open_buy_orders()
        assert result == []

    def test_cancel_order_delegates_to_alpaca(self, monkeypatch):
        from unittest.mock import MagicMock
        import config
        config.ALPACA_API_KEY = "test-key"
        config.ALPACA_SECRET_KEY = "test-secret"
        from alpaca_client import AlpacaClient
        client = AlpacaClient.__new__(AlpacaClient)
        mock_trading = MagicMock()
        client._trading = mock_trading
        client.cancel_order("order-xyz")
        mock_trading.cancel_order_by_id.assert_called_once_with("order-xyz")
```

- [ ] **Step 2: Run tests — confirm they fail**

```bash
.venv/bin/pytest tests/test_foundation.py::TestOrphanOrderDrain -v
```

Expected: `AttributeError` — `get_open_buy_orders` does not exist yet.

- [ ] **Step 3: Add methods to alpaca_client.py**

Add after `cancel_all_orders()` (after line 114):

```python
    def get_open_buy_orders(self) -> list[dict]:
        """Return all pending BUY orders (free Trading API, all Alpaca tiers)."""
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus, OrderSide
        orders = self._trading.get_orders(
            filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, side=OrderSide.BUY)
        )
        return [{"id": str(o.id), "symbol": o.symbol} for o in orders]

    def cancel_order(self, order_id: str) -> None:
        """Cancel a single order by ID."""
        self._trading.cancel_order_by_id(order_id)
```

- [ ] **Step 4: Run tests — confirm they pass**

```bash
.venv/bin/pytest tests/test_foundation.py::TestOrphanOrderDrain -v
```

Expected: 3 PASSED.

- [ ] **Step 5: Add orphan drain step 4 to `_reconcile_positions()` in main.py**

Inside `_reconcile_positions()`, after the existing `if changed: save_open_positions(local_positions)` block and before the `# ── 3. Starting after 3:55 PM ET` comment, add:

```python
        # ── 4. Orphan BUY order drain ─────────────────────────────────────────
        # Cancel any open BUY orders left in Alpaca from a crashed trading cycle.
        # Filled bracket BUY legs have status=filled (not open) — safe to cancel all open BUYs.
        try:
            open_buys = alpaca.get_open_buy_orders()
            cancelled = 0
            for order in open_buys:
                try:
                    alpaca.cancel_order(order["id"])
                    logger.warning(
                        f"RECONCILE: Cancelled orphan BUY order {order['id']} for {order['symbol']}"
                    )
                    cancelled += 1
                except Exception as exc:
                    logger.warning(f"RECONCILE: Could not cancel order {order['id']}: {exc}")
            if cancelled:
                logger.info(f"RECONCILE: Drained {cancelled} orphan BUY order(s).")
            else:
                logger.info("RECONCILE: No orphan BUY orders found.")
        except Exception as exc:
            logger.error(f"RECONCILE: Orphan drain failed — {exc}")
```

- [ ] **Step 6: Commit**

```bash
git add alpaca_client.py main.py tests/test_foundation.py
git commit -m "feat: orphan-order drain at startup — cancel open BUY orders after crash"
```

---

## Task 5: Flash crash config constant + candidate field in defaults

**Files:**
- Modify: `macro_regime.py`
- Modify: `tests/test_foundation.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_foundation.py`:

```python

# ── Flash crash config constant ───────────────────────────────────────────────

@pytest.mark.unit
class TestFlashCrashConfig:
    def test_check_flash_crash_uses_config_threshold(self, monkeypatch):
        import config
        from macro_regime import check_flash_crash
        monkeypatch.setattr(config, "FLASH_CRASH_SPY_DROP_PCT", -5.0)
        # -4% drop should NOT trigger when threshold is -5%
        assert check_flash_crash(spy_current=96.0, spy_prev_close=100.0) is False
        # -6% drop SHOULD trigger
        assert check_flash_crash(spy_current=94.0, spy_prev_close=100.0) is True

    def test_load_macro_regime_default_has_candidate_field(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "MACRO_REGIME_FILE", tmp_path / "macro_regime.json")
        from macro_regime import load_macro_regime
        result = load_macro_regime()
        assert "flash_crash_candidate" in result
        assert result["flash_crash_candidate"] is None
```

- [ ] **Step 2: Run tests — confirm they fail**

```bash
.venv/bin/pytest tests/test_foundation.py::TestFlashCrashConfig -v
```

Expected: both fail — `check_flash_crash` uses hardcoded literal, `load_macro_regime` default is missing the field.

- [ ] **Step 3: Update `check_flash_crash()` in macro_regime.py**

Current (line ~355):

```python
def check_flash_crash(spy_current: float, spy_prev_close: float) -> bool:
    """Return True if SPY has dropped ≥ 2.5% from previous close intraday."""
    if spy_prev_close <= 0:
        return False
    return (spy_current - spy_prev_close) / spy_prev_close * 100 <= -2.5
```

Replace with:

```python
def check_flash_crash(spy_current: float, spy_prev_close: float) -> bool:
    """Return True if SPY has dropped ≥ FLASH_CRASH_SPY_DROP_PCT from previous close intraday."""
    if spy_prev_close <= 0:
        return False
    return (spy_current - spy_prev_close) / spy_prev_close * 100 <= config.FLASH_CRASH_SPY_DROP_PCT
```

- [ ] **Step 4: Add `flash_crash_candidate` to `load_macro_regime()` default**

In `load_macro_regime()` (around line 599), add to the default return dict:

```python
    return {
        "regime": "NORMAL",
        "macro_health_score": 0,
        "vix_current": 0.0,
        "spy_5day_return": 0.0,
        "flash_crash_active": False,
        "flash_crash_until": None,
        "flash_crash_candidate": None,   # ← add this line
        "event_risk_today": False,
        "event_risk_details": "",
        "capitulation_candidates": [],
        "sector_scores": {"AI_SEMI": 0, "BROAD_TECH": 0, "CYBERSECURITY": 0},
    }
```

- [ ] **Step 5: Add `flash_crash_candidate` to `fetch_macro_regime()` result dict**

In `fetch_macro_regime()`, inside the main `result = { ... }` dict (around line 535), add:

```python
        result = {
            "regime":                  regime,
            ...
            "flash_crash_active":      flash_crash_active,
            "flash_crash_until":       flash_crash_until,
            "flash_crash_candidate":   None,   # ← add: explicitly null on each 8:55 AM run
            ...
        }
```

Also add to the exception fallback `result` dict near line 565:

```python
        result = {
            ...
            "flash_crash_active":      False,
            "flash_crash_until":       None,
            "flash_crash_candidate":   None,   # ← add here too
            ...
        }
```

- [ ] **Step 6: Run tests — confirm they pass**

```bash
.venv/bin/pytest tests/test_foundation.py::TestFlashCrashConfig tests/test_foundation.py::TestFlashCrash -v
```

Expected: all pass (including the original TestFlashCrash tests — threshold value unchanged).

- [ ] **Step 7: Commit**

```bash
git add macro_regime.py tests/test_foundation.py
git commit -m "feat: check_flash_crash uses config constant; add flash_crash_candidate field to regime defaults"
```

---

## Task 6: Polygon helper + two-reading run_market_monitor + lockout candidate clear

**Files:**
- Modify: `trading_agent.py` (add `import requests`; 5 new helpers; rewrite `run_market_monitor`; patch `_set_crisis_lockout`)
- Modify: `tests/test_foundation.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_foundation.py`:

```python

# ── Polygon helper ────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestVerifySpyWithPolygon:
    def test_no_key_returns_none(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "POLYGON_API_KEY", "")
        from trading_agent import _verify_spy_with_polygon
        assert _verify_spy_with_polygon(-3.0) is None

    def test_confirmed_drop_returns_true(self, monkeypatch):
        import config
        from unittest.mock import MagicMock
        monkeypatch.setattr(config, "POLYGON_API_KEY", "test-key")
        monkeypatch.setattr(config, "FLASH_CRASH_SPY_DROP_PCT", -2.5)
        mock_resp = MagicMock()
        mock_resp.raise_for_status = lambda: None
        mock_resp.json.return_value = {"ticker": {"todaysChangePerc": -3.1}}
        import trading_agent
        monkeypatch.setattr(trading_agent.requests, "get", lambda *a, **kw: mock_resp)
        assert _verify_spy_with_polygon(-3.1) is True

    def test_bad_tick_returns_false(self, monkeypatch):
        import config
        from unittest.mock import MagicMock
        monkeypatch.setattr(config, "POLYGON_API_KEY", "test-key")
        monkeypatch.setattr(config, "FLASH_CRASH_SPY_DROP_PCT", -2.5)
        mock_resp = MagicMock()
        mock_resp.raise_for_status = lambda: None
        mock_resp.json.return_value = {"ticker": {"todaysChangePerc": -0.5}}
        import trading_agent
        monkeypatch.setattr(trading_agent.requests, "get", lambda *a, **kw: mock_resp)
        from trading_agent import _verify_spy_with_polygon
        assert _verify_spy_with_polygon(-2.6) is False

    def test_network_error_returns_none(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "POLYGON_API_KEY", "test-key")
        import trading_agent
        monkeypatch.setattr(trading_agent.requests, "get",
                            lambda *a, **kw: (_ for _ in ()).throw(Exception("timeout")))
        from trading_agent import _verify_spy_with_polygon
        assert _verify_spy_with_polygon(-3.0) is None


@pytest.mark.unit
class TestFlashCrashTwoReading:
    def _write_regime(self, tmp_path, candidate=None):
        data = {
            "regime": "NORMAL",
            "flash_crash_active": False,
            "flash_crash_until": None,
            "flash_crash_candidate": candidate,
        }
        (tmp_path / "macro_regime.json").write_text(json.dumps(data))

    def test_no_candidate_writes_stage1(self, tmp_path, monkeypatch):
        import config
        config.MACRO_REGIME_FILE = tmp_path / "macro_regime.json"
        self._write_regime(tmp_path, candidate=None)
        from trading_agent import _handle_flash_crash_stage2
        _handle_flash_crash_stage2(-2.7)
        data = json.loads((tmp_path / "macro_regime.json").read_text())
        assert data["flash_crash_candidate"] is not None
        assert data["flash_crash_candidate"]["spy_pct"] == -2.7

    def test_fresh_candidate_triggers_lockout(self, tmp_path, monkeypatch):
        import config
        config.MACRO_REGIME_FILE = tmp_path / "macro_regime.json"
        fresh = {"spy_pct": -2.7, "detected_at": datetime.now(ET).isoformat()}
        self._write_regime(tmp_path, candidate=fresh)
        triggered = []
        import trading_agent
        monkeypatch.setattr(trading_agent, "_trigger_flash_crash_lockout", lambda: triggered.append(1))
        from trading_agent import _handle_flash_crash_stage2
        _handle_flash_crash_stage2(-3.0)
        assert len(triggered) == 1

    def test_stale_candidate_overwritten_no_lockout(self, tmp_path, monkeypatch):
        import config
        from datetime import timedelta
        config.MACRO_REGIME_FILE = tmp_path / "macro_regime.json"
        old_time = (datetime.now(ET) - timedelta(minutes=20)).isoformat()
        stale = {"spy_pct": -2.7, "detected_at": old_time}
        self._write_regime(tmp_path, candidate=stale)
        triggered = []
        import trading_agent
        monkeypatch.setattr(trading_agent, "_trigger_flash_crash_lockout", lambda: triggered.append(1))
        from trading_agent import _handle_flash_crash_stage2
        _handle_flash_crash_stage2(-3.0)
        assert len(triggered) == 0
        data = json.loads((tmp_path / "macro_regime.json").read_text())
        # Fresh candidate written to replace the stale one
        new_ts = datetime.fromisoformat(data["flash_crash_candidate"]["detected_at"])
        assert (datetime.now(ET) - new_ts).total_seconds() < 5

    def test_spy_recovery_clears_candidate(self, tmp_path, monkeypatch):
        import config
        config.MACRO_REGIME_FILE = tmp_path / "macro_regime.json"
        fresh = {"spy_pct": -2.7, "detected_at": datetime.now(ET).isoformat()}
        self._write_regime(tmp_path, candidate=fresh)
        from trading_agent import _clear_flash_crash_candidate
        _clear_flash_crash_candidate()
        data = json.loads((tmp_path / "macro_regime.json").read_text())
        assert data["flash_crash_candidate"] is None

    def test_set_crisis_lockout_clears_candidate(self, tmp_path, monkeypatch):
        import config
        config.MACRO_REGIME_FILE = tmp_path / "macro_regime.json"
        candidate = {"spy_pct": -3.0, "detected_at": datetime.now(ET).isoformat()}
        self._write_regime(tmp_path, candidate=candidate)
        from trading_agent import _set_crisis_lockout
        _set_crisis_lockout(hours=1)
        data = json.loads((tmp_path / "macro_regime.json").read_text())
        assert data["flash_crash_active"] is True
        assert data["flash_crash_candidate"] is None
```

- [ ] **Step 2: Run tests — confirm they fail**

```bash
.venv/bin/pytest tests/test_foundation.py::TestVerifySpyWithPolygon tests/test_foundation.py::TestFlashCrashTwoReading -v
```

Expected: `ImportError` or `AttributeError` — helpers don't exist yet.

- [ ] **Step 3: Add `import requests` to trading_agent.py**

Add after the existing imports (after line 8 `from zoneinfo import ZoneInfo`):

```python
import requests
```

- [ ] **Step 4: Add 5 new helpers to trading_agent.py**

Add before the existing `_set_crisis_lockout` function:

```python
def _verify_spy_with_polygon(spy_pct: float) -> bool | None:
    """Cross-check SPY drop with Massive API. Returns True/False/None (None = unavailable)."""
    if not config.POLYGON_API_KEY:
        return None
    try:
        resp = requests.get(
            "https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers/SPY",
            headers={"Authorization": f"Bearer {config.POLYGON_API_KEY}"},
            timeout=5,
        )
        resp.raise_for_status()
        polygon_pct = float(resp.json()["ticker"]["todaysChangePerc"])
        return polygon_pct <= config.FLASH_CRASH_SPY_DROP_PCT
    except Exception as exc:
        logger.warning(f"Polygon/Massive check failed ({exc}) — falling back to two-reading confirmation")
        return None


def _write_flash_crash_candidate(spy_pct: float, now: datetime) -> None:
    """Write Stage 1 candidate to macro_regime.json."""
    try:
        data = json.loads(config.MACRO_REGIME_FILE.read_text()) if config.MACRO_REGIME_FILE.exists() else {}
        data["flash_crash_candidate"] = {"spy_pct": round(spy_pct, 2), "detected_at": now.isoformat()}
        with open(config.MACRO_REGIME_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        logger.warning(f"Could not write flash_crash_candidate: {exc}")


def _clear_flash_crash_candidate() -> None:
    """Clear flash_crash_candidate in macro_regime.json."""
    try:
        if not config.MACRO_REGIME_FILE.exists():
            return
        data = json.loads(config.MACRO_REGIME_FILE.read_text())
        if data.get("flash_crash_candidate") is not None:
            data["flash_crash_candidate"] = None
            with open(config.MACRO_REGIME_FILE, "w") as f:
                json.dump(data, f, indent=2)
    except Exception as exc:
        logger.warning(f"Could not clear flash_crash_candidate: {exc}")


def _trigger_flash_crash_lockout() -> None:
    """Force-close all positions and set 48h CRISIS lockout."""
    force_close_all()
    try:
        from swing_manager import load_swing_positions, _execute_full_close
        alpaca = AlpacaClient()
        for sym, pos in list(load_swing_positions().items()):
            _execute_full_close(sym, pos, alpaca, "flash_crash")
    except Exception as exc:
        logger.warning(f"flash crash: swing close failed: {exc}")
    _set_crisis_lockout(hours=48)


def _handle_flash_crash_stage2(spy_pct: float) -> None:
    """Two-reading confirmation path (no Polygon key, or Polygon returned None)."""
    now = datetime.now(ET)
    try:
        candidate = None
        if config.MACRO_REGIME_FILE.exists():
            data = json.loads(config.MACRO_REGIME_FILE.read_text())
            candidate = data.get("flash_crash_candidate")

        if candidate is None:
            _write_flash_crash_candidate(spy_pct, now)
            logger.warning("Flash crash Stage 1 — awaiting Stage 2 confirmation in 5 min")
            return

        age_min = (now - datetime.fromisoformat(candidate["detected_at"])).total_seconds() / 60
        if age_min <= 15:
            logger.critical(f"Flash crash Stage 2 confirmed — SPY still at {spy_pct:+.1f}%")
            _trigger_flash_crash_lockout()
        else:
            _write_flash_crash_candidate(spy_pct, now)
            logger.warning("Flash crash stale candidate overwritten — awaiting fresh Stage 2")
    except Exception as exc:
        logger.warning(f"Flash crash stage 2 check failed: {exc}")
```

- [ ] **Step 5: Update `_set_crisis_lockout()` to clear candidate**

In `_set_crisis_lockout()`, add one line after the existing regime assignment:

```python
        data["flash_crash_active"] = True
        data["flash_crash_until"]  = until
        data["regime"] = "CRISIS"
        data["flash_crash_candidate"] = None   # ← add this line
```

- [ ] **Step 6: Replace `run_market_monitor()` body**

Replace the existing `run_market_monitor()` function body:

```python
def run_market_monitor() -> None:
    """
    Called every 5 min during market hours. Checks for SPY flash crash.
    Requires two consecutive readings (or one Polygon-confirmed reading) before lockout.
    """
    try:
        spy_current = get_latest_price("SPY")
        spy_prev    = _get_spy_prev_close()
        if spy_prev <= 0:
            return
        spy_pct = (spy_current - spy_prev) / spy_prev * 100

        if spy_pct > config.FLASH_CRASH_SPY_DROP_PCT:
            _clear_flash_crash_candidate()
            return

        logger.warning(
            f"FLASH CRASH CANDIDATE: SPY={spy_current:.2f} prev={spy_prev:.2f} ({spy_pct:+.1f}%)"
        )

        if config.POLYGON_API_KEY:
            result = _verify_spy_with_polygon(spy_pct)
            if result is True:
                logger.critical("Polygon confirmed flash crash — triggering lockout immediately")
                _trigger_flash_crash_lockout()
                return
            if result is False:
                logger.info("Polygon disagrees — bad SPY tick, discarding candidate")
                _clear_flash_crash_candidate()
                return
            # result is None → Polygon unavailable → fall through to Stage 2

        _handle_flash_crash_stage2(spy_pct)

    except Exception as exc:
        logger.debug(f"run_market_monitor: {exc}")
```

- [ ] **Step 7: Run tests — confirm they pass**

```bash
.venv/bin/pytest tests/test_foundation.py::TestVerifySpyWithPolygon tests/test_foundation.py::TestFlashCrashTwoReading -v
```

Expected: all PASSED.

- [ ] **Step 8: Run full test suite to check for regressions**

```bash
.venv/bin/pytest tests/test_foundation.py -v -m unit
```

Expected: all existing tests still pass.

- [ ] **Step 9: Commit**

```bash
git add trading_agent.py tests/test_foundation.py
git commit -m "feat: two-reading flash crash confirmation with optional Massive/Polygon cross-check"
```

---

## Task 7: Auto-clear lockout via pure helper + tests

**Files:**
- Modify: `macro_regime.py`
- Modify: `tests/test_foundation.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_foundation.py`:

```python

# ── Auto-clear lockout ────────────────────────────────────────────────────────

@pytest.mark.unit
class TestEvaluateLockoutRecovery:
    def _future(self, hours=24):
        from datetime import timedelta
        return (datetime.now(ET) + timedelta(hours=hours)).isoformat()

    def _past(self, hours=1):
        from datetime import timedelta
        return (datetime.now(ET) - timedelta(hours=hours)).isoformat()

    def test_no_lockout_unchanged(self):
        from macro_regime import _evaluate_lockout_recovery
        active, until = _evaluate_lockout_recovery(False, None, 20.0, 1.0, datetime.now(ET))
        assert active is False
        assert until is None

    def test_naturally_expired_clears(self):
        from macro_regime import _evaluate_lockout_recovery
        active, until = _evaluate_lockout_recovery(
            True, self._past(1), 50.0, -10.0, datetime.now(ET)
        )
        assert active is False
        assert until is None

    def test_conditions_recovered_clears(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "FLASH_CRASH_VIX_RECOVERY", 35)
        monkeypatch.setattr(config, "FLASH_CRASH_SPY_RECOVERY_PCT", -5.0)
        from macro_regime import _evaluate_lockout_recovery
        active, until = _evaluate_lockout_recovery(
            True, self._future(24), 30.0, -2.0, datetime.now(ET)
        )
        assert active is False
        assert until is None

    def test_vix_still_high_preserves_lockout(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "FLASH_CRASH_VIX_RECOVERY", 35)
        monkeypatch.setattr(config, "FLASH_CRASH_SPY_RECOVERY_PCT", -5.0)
        from macro_regime import _evaluate_lockout_recovery
        future = self._future(24)
        active, until = _evaluate_lockout_recovery(
            True, future, 42.0, -2.0, datetime.now(ET)
        )
        assert active is True
        assert until == future

    def test_spy_still_weak_preserves_lockout(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "FLASH_CRASH_VIX_RECOVERY", 35)
        monkeypatch.setattr(config, "FLASH_CRASH_SPY_RECOVERY_PCT", -5.0)
        from macro_regime import _evaluate_lockout_recovery
        future = self._future(24)
        active, until = _evaluate_lockout_recovery(
            True, future, 28.0, -8.0, datetime.now(ET)
        )
        assert active is True
        assert until == future
```

- [ ] **Step 2: Run tests — confirm they fail**

```bash
.venv/bin/pytest tests/test_foundation.py::TestEvaluateLockoutRecovery -v
```

Expected: `ImportError` — `_evaluate_lockout_recovery` does not exist yet.

- [ ] **Step 3: Add pure helper to macro_regime.py**

Add before `fetch_macro_regime()`:

```python
def _evaluate_lockout_recovery(
    flash_crash_active: bool,
    flash_crash_until: str | None,
    vix_current: float,
    spy_5day_return: float,
    now: datetime,
) -> tuple[bool, str | None]:
    """Return updated (flash_crash_active, flash_crash_until). Pure — no side effects."""
    if not (flash_crash_active and flash_crash_until):
        return flash_crash_active, flash_crash_until
    until_dt = datetime.fromisoformat(flash_crash_until)
    if now >= until_dt:
        return False, None
    if (vix_current < config.FLASH_CRASH_VIX_RECOVERY
            and spy_5day_return > config.FLASH_CRASH_SPY_RECOVERY_PCT):
        return False, None
    return True, flash_crash_until
```

- [ ] **Step 4: Wire into `fetch_macro_regime()` — replace existing lockout carry-forward block**

The existing block in `fetch_macro_regime()` reads (around line 508):

```python
                flash_crash_active = prev.get("flash_crash_active", False)
                flash_crash_until  = prev.get("flash_crash_until")
                if flash_crash_active and flash_crash_until:
                    until_dt = datetime.fromisoformat(flash_crash_until)
                    if now < until_dt:
                        regime = "CRISIS"
                    else:
                        flash_crash_active = False
                        flash_crash_until  = None
```

Replace with:

```python
                flash_crash_active = prev.get("flash_crash_active", False)
                flash_crash_until  = prev.get("flash_crash_until")
                flash_crash_active, flash_crash_until = _evaluate_lockout_recovery(
                    flash_crash_active, flash_crash_until, vix_current, spy_5day_return, now
                )
                if flash_crash_active:
                    regime = "CRISIS"
                    logger.info("Flash crash lockout still active — CRISIS regime carried forward")
                elif prev.get("flash_crash_active") and not flash_crash_active:
                    logger.info(
                        f"Flash crash lockout auto-cleared — "
                        f"VIX={vix_current:.1f}, SPY5d={spy_5day_return:+.2f}%"
                    )
```

- [ ] **Step 5: Run tests — confirm they pass**

```bash
.venv/bin/pytest tests/test_foundation.py::TestEvaluateLockoutRecovery -v
```

Expected: 5 PASSED.

- [ ] **Step 6: Run full suite**

```bash
.venv/bin/pytest tests/test_foundation.py -v -m unit
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add macro_regime.py tests/test_foundation.py
git commit -m "feat: auto-clear flash crash lockout when VIX/SPY conditions recover"
```

---

## Task 8: Auto-launcher plist + docs

**Files:**
- Modify: `trading_bot_start.plist`
- Modify: `README.md`
- Modify: `.env.example`

- [ ] **Step 1: Update trading_bot_start.plist**

Replace:

```xml
    <key>KeepAlive</key>
    <false/>
```

With:

```xml
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>15</integer>
```

- [ ] **Step 2: Reinstall the LaunchAgent (macOS only)**

```bash
./setup_launchagent.sh uninstall
./setup_launchagent.sh
```

Expected output: `Installed. Auto-start: 5:30 AM PT…`

- [ ] **Step 3: Update README.md Crash Recovery section**

Replace the existing `## Crash Recovery and Overnight Operation` section with:

```markdown
## Crash Recovery and Overnight Operation

The bot is designed to run continuously (overnight, weekends) and recover gracefully:

- **Single-instance lock** — prevents two bot processes from running simultaneously.
- **PID + watchdog** — watchdog exits the process if the scheduler freezes; launchd/systemd restarts it.
- **Crash loop gate** — `bot_health.json` tracks restart frequency. If the bot crashes ≥ 3 times within 10 minutes, startup pauses 30 minutes before resuming. A clean `stop.sh` resets the counter.
- **Orphan-order drain** — on every startup, any pending BUY orders left in Alpaca from a crashed cycle are cancelled before the trading cycle resumes. Prevents duplicate bracket orders.
- **Position reconciliation** — syncs `open_positions.json` with Alpaca's actual state. Handles bracket fills that happened while offline and crashed mid-entry restores.
- **Flash crash confirmation** — requires two consecutive SPY readings ≥ −2.5% (5 min apart) before triggering a 48h lockout. If `POLYGON_API_KEY` is set, a Massive API cross-check replaces the 5-min wait. A single bad data tick no longer locks the bot out.
- **Flash crash auto-recovery** — the 8:55 AM `fetch_macro_regime()` job auto-clears the lockout if VIX < 35 and SPY 5-day return > −5%, without manual intervention.
- **Swing position safety** — reconciliation skips swing positions (intentional holds); force-close at 3:55 PM also skips them.
- **Daily closed list** — `daily_closed.json` prevents re-entering a symbol on the same day, even across crash/restart cycles.

**macOS crash recovery:** The LaunchAgent uses `KeepAlive={SuccessfulExit: false}` — launchd restarts the bot automatically on crash but not on a clean `stop.sh` shutdown. A `ThrottleInterval` of 15 seconds ensures a minimum gap between restarts.

**Linux crash recovery:** Use a systemd unit instead of cron for intra-day crash recovery:
```ini
[Service]
ExecStart=/path/to/trading-bot/start.sh --foreground
Restart=on-failure
RestartSec=15
```
The provided `setup_cron.sh` handles scheduled start/stop only and does not restart on crash.
```

- [ ] **Step 4: Update State / Persistence table in README.md**

Add a row to the state table:

```markdown
| `bot_health.json` | Crash loop counter — restart frequency tracker (auto-created, reset on clean stop) |
```

- [ ] **Step 5: Remove manual Python one-liner from README.md troubleshooting**

Remove the entire `**Flash crash lockout active after a false positive**` block (the Python one-liner and the `./stop.sh && ./start.sh` lines). Replace with:

```markdown
**Flash crash lockout active after a false positive**

The bot now auto-recovers: the 8:55 AM macro regime job clears the lockout if VIX < 35 and SPY 5-day return > −5%. Wait until the next trading morning — no manual action needed.

If you need to clear it immediately:
```bash
./stop.sh && ./start.sh
```
The startup `fetch_macro_regime()` will evaluate conditions and clear automatically.
```

- [ ] **Step 6: Update .env.example**

Find the existing `POLYGON_API_KEY` line (around line 35) and update:

```bash
# Optional: cross-checks SPY flash-crash readings via Massive API (ex-Polygon.io).
# Get free key at https://massive.com/dashboard/keys
POLYGON_API_KEY=
```

- [ ] **Step 7: Run full test suite one final time**

```bash
.venv/bin/pytest tests/test_foundation.py -v -m unit
```

Expected: all pass, no regressions.

- [ ] **Step 8: Commit everything**

```bash
git add trading_bot_start.plist README.md .env.example
git commit -m "docs: update crash recovery docs; plist KeepAlive+ThrottleInterval; .env.example Massive API"
```

---

## Self-Review Checklist

- [x] Spec §Layer 1 crash loop → Task 2 + Task 3
- [x] Spec §Layer 2 orphan drain → Task 4
- [x] Spec §flash crash config constant → Task 5 (`check_flash_crash` + config)
- [x] Spec §candidate field in defaults → Task 5 (`load_macro_regime` + `fetch_macro_regime`)
- [x] Spec §`_verify_spy_with_polygon` three-way return → Task 6
- [x] Spec §two-reading `run_market_monitor` + explicit None branch → Task 6
- [x] Spec §`_set_crisis_lockout` clears candidate → Task 6
- [x] Spec §`_evaluate_lockout_recovery` pure helper → Task 7
- [x] Spec §auto-clear wired into `fetch_macro_regime` → Task 7
- [x] Spec §`trading_bot_start.plist` KeepAlive + ThrottleInterval → Task 8
- [x] Spec §`_handle_shutdown` reset between `scheduler.shutdown` and `sys.exit(0)` → Task 3
- [x] Spec §README + .env.example updates → Task 8
- [x] Spec §bot_health.json in .gitignore → Task 1
- [x] All 9 test spec requirements covered across Tasks 2, 4, 5, 6, 7
- [x] `import requests` added to trading_agent.py before using it → Task 6 Step 3
- [x] `import json` + `timedelta` added to main.py → Task 2 Step 3
- [x] No placeholder text — every step has complete code
- [x] Method names consistent across all tasks (`_verify_spy_with_polygon`, `_handle_flash_crash_stage2`, `_trigger_flash_crash_lockout`, `_clear_flash_crash_candidate`, `_write_flash_crash_candidate`, `_evaluate_lockout_recovery`, `_check_crash_loop`, `_reset_bot_health`)
