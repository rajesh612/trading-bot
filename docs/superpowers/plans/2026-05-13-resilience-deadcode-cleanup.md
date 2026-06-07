# Resilience Fixes + Dead Code Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix six verified bugs that cause zero-trade days and remove all dead code found in the audit — no new modules, only targeted changes to existing files.

**Architecture:** Targeted surgical edits to `risk_manager.py`, `claude_caller.py`, `pipeline.py`, `pre_claude_filter.py`, `cycle_context.py`, and `trading_agent.py`. All new tests land in `tests/test_pipeline.py`.

**Tech Stack:** Python 3.11, `threading.Event`, `time.monotonic()`, pytest, `unittest.mock`

---

## File Map

| File | Changes |
|------|---------|
| `trading/risk_manager.py` | Fix 1 (flag day-reset), Fix 6 (drop `gain_pct` param) |
| `trading/claude_caller.py` | Fix 2 (TTL + backoff), dead code (`is_claude_disabled`) |
| `trading/pipeline.py` | Fix 3 (capture `daily_closed`), Fix 5 (auto-execute outside budget gate) |
| `trading/pre_claude_filter.py` | Fix 3 (Layer 0 daily_closed check) |
| `trading/cycle_context.py` | Fix 4 (accept optional `regime_info`/`params`) |
| `trading_agent.py` | Fix 4 (pass cached values, drop dead fred_cache load), dead code (3 items) |
| `tests/test_pipeline.py` | New test classes + update existing call sites |

---

## Task 1: Fix 6 — Remove `gain_pct` Dead Parameter

**Files:**
- Modify: `trading/risk_manager.py:76`
- Modify: `trading/claude_caller.py:86`
- Modify: `tests/test_pipeline.py` (update any existing `check_entry_budget` call sites)

`gain_pct` is accepted by `check_entry_budget` but never referenced in the body. Remove it from the signature and its one caller in `claude_caller.py`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_pipeline.py` inside class `TestRiskManagerAdditions`:

```python
@pytest.mark.unit
def test_check_entry_budget_no_gain_pct(self, tmp_path):
    """check_entry_budget accepts (cost, regime) — no gain_pct argument."""
    import json
    from unittest.mock import patch
    from trading import risk_manager
    today = datetime.now(ET).strftime("%Y-%m-%d")
    budget = {
        "date": today, "calls_made": 0, "entry_calls": 0, "exit_calls": 0,
        "input_tokens": 0, "output_tokens": 0, "cache_write_tokens": 0,
        "cache_read_tokens": 0, "estimated_cost_usd": 0.0,
        "entry_spent_usd": 0.0, "exit_spent_usd": 0.0,
        "ceiling_usd": 1.0,
        "entry_ceiling_usd": 0.70, "exit_ceiling_usd": 0.30,
        "ceiling_mode": "normal", "entry_exhausted": False,
        "exit_exhausted": False, "budget_exhausted": False, "escalated": False,
    }
    budget_file = tmp_path / "api_budget.json"
    budget_file.write_text(json.dumps(budget))
    with patch("trading.risk_manager.config") as mc, \
         patch("trading.risk_manager.morning_cap_exceeded", return_value=False), \
         patch("trading.risk_manager.midday_cap_exceeded", return_value=False):
        mc.API_BUDGET_FILE = budget_file
        mc.ENTRY_BUDGET_RATIO = 0.7
        mc.EXIT_BUDGET_RATIO  = 0.3
        mc.API_BUDGET_WARNING_PCT = 0.8
        result = risk_manager.check_entry_budget(0.001, "NORMAL")
    assert result is True
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/Documents/projects/trading-bot
.venv/bin/pytest tests/test_pipeline.py::TestRiskManagerAdditions::test_check_entry_budget_no_gain_pct -v
```

Expected: `TypeError: check_entry_budget() takes 3 positional arguments but 2 were given` (or similar).

- [ ] **Step 3: Update `check_entry_budget` signature in `trading/risk_manager.py:76`**

Change:
```python
def check_entry_budget(estimated_cost: float, gain_pct: float, regime: str) -> bool:
```
To:
```python
def check_entry_budget(estimated_cost: float, regime: str) -> bool:
```

- [ ] **Step 4: Update the one caller in `trading/claude_caller.py:86`**

Change:
```python
    if not risk_manager.check_entry_budget(estimated_cost, snap.portfolio_gain_pct, snap.regime):
```
To:
```python
    if not risk_manager.check_entry_budget(estimated_cost, snap.regime):
```

- [ ] **Step 5: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_pipeline.py::TestRiskManagerAdditions -v
```

Expected: all tests in the class PASS.

- [ ] **Step 6: Commit**

```bash
git add trading/risk_manager.py trading/claude_caller.py tests/test_pipeline.py
git commit -m "fix: remove dead gain_pct param from check_entry_budget"
```

---

## Task 2: Fix 1 — Entry Exhausted Flag Day Reset

**Files:**
- Modify: `trading/risk_manager.py:76-121` (`check_entry_budget`)
- Test: `tests/test_pipeline.py`

On continuous (Linux) deployment, `_entry_exhausted_flag` stays `True` from yesterday. `is_entry_budget_exhausted()` returns it without reading disk, so today's fresh budget never clears it.

Fix: inside `check_entry_budget`, after `load_budget()`, sync the flag downward when the disk budget is fresh:

```python
b = load_budget()
if not b.get("entry_exhausted"):
    global _entry_exhausted_flag
    _entry_exhausted_flag = False
```

- [ ] **Step 1: Write the failing test**

Add new class to `tests/test_pipeline.py`:

```python
class TestEntryExhaustedFlagReset:

    @pytest.mark.unit
    def test_flag_syncs_to_false_on_fresh_day_budget(self, tmp_path):
        """Stale True flag is cleared when disk budget shows entry_exhausted=False."""
        import json
        from unittest.mock import patch
        from trading import risk_manager
        risk_manager._entry_exhausted_flag = True  # stale from yesterday

        today = datetime.now(ET).strftime("%Y-%m-%d")
        budget = {
            "date": today, "calls_made": 0, "entry_calls": 0, "exit_calls": 0,
            "input_tokens": 0, "output_tokens": 0, "cache_write_tokens": 0,
            "cache_read_tokens": 0, "estimated_cost_usd": 0.0,
            "entry_spent_usd": 0.0, "exit_spent_usd": 0.0,
            "ceiling_usd": 1.0,
            "entry_ceiling_usd": 0.70, "exit_ceiling_usd": 0.30,
            "ceiling_mode": "normal", "entry_exhausted": False,
            "exit_exhausted": False, "budget_exhausted": False, "escalated": False,
        }
        budget_file = tmp_path / "api_budget.json"
        budget_file.write_text(json.dumps(budget))

        with patch("trading.risk_manager.config") as mc, \
             patch("trading.risk_manager.morning_cap_exceeded", return_value=False), \
             patch("trading.risk_manager.midday_cap_exceeded", return_value=False):
            mc.API_BUDGET_FILE = budget_file
            mc.ENTRY_BUDGET_RATIO = 0.7
            mc.EXIT_BUDGET_RATIO  = 0.3
            mc.API_BUDGET_WARNING_PCT = 0.8
            result = risk_manager.check_entry_budget(0.001, "NORMAL")

        assert result is True
        assert risk_manager._entry_exhausted_flag is False

    @pytest.mark.unit
    def test_flag_stays_true_when_disk_entry_exhausted(self, tmp_path):
        """Flag is not cleared when disk budget confirms entry_exhausted=True."""
        import json
        from unittest.mock import patch
        from trading import risk_manager
        risk_manager._entry_exhausted_flag = True

        today = datetime.now(ET).strftime("%Y-%m-%d")
        budget = {
            "date": today, "calls_made": 5, "entry_calls": 5, "exit_calls": 0,
            "input_tokens": 0, "output_tokens": 0, "cache_write_tokens": 0,
            "cache_read_tokens": 0, "estimated_cost_usd": 0.70,
            "entry_spent_usd": 0.70, "exit_spent_usd": 0.0,
            "ceiling_usd": 1.0,
            "entry_ceiling_usd": 0.70, "exit_ceiling_usd": 0.30,
            "ceiling_mode": "normal", "entry_exhausted": True,
            "exit_exhausted": False, "budget_exhausted": False, "escalated": False,
        }
        budget_file = tmp_path / "api_budget.json"
        budget_file.write_text(json.dumps(budget))

        with patch("trading.risk_manager.config") as mc:
            mc.API_BUDGET_FILE = budget_file
            mc.ENTRY_BUDGET_RATIO = 0.7
            mc.EXIT_BUDGET_RATIO  = 0.3
            result = risk_manager.check_entry_budget(0.001, "NORMAL")

        assert result is False
        assert risk_manager._entry_exhausted_flag is True
        risk_manager._entry_exhausted_flag = False  # cleanup
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_pipeline.py::TestEntryExhaustedFlagReset -v
```

Expected: `test_flag_syncs_to_false_on_fresh_day_budget` FAIL — flag stays `True`.

- [ ] **Step 3: Add flag sync in `trading/risk_manager.py` inside `check_entry_budget`**

Current code at line 78:
```python
    b = load_budget()

    if b.get("entry_exhausted"):
```

Change to:
```python
    b = load_budget()
    if not b.get("entry_exhausted"):
        global _entry_exhausted_flag
        _entry_exhausted_flag = False

    if b.get("entry_exhausted"):
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_pipeline.py::TestEntryExhaustedFlagReset -v
```

Expected: both tests PASS.

- [ ] **Step 5: Run full test suite to catch regressions**

```bash
.venv/bin/pytest tests/test_pipeline.py -v
```

Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add trading/risk_manager.py tests/test_pipeline.py
git commit -m "fix: sync _entry_exhausted_flag from disk on each budget check (day-reset)"
```

---

## Task 3: Fix 2 — Circuit Breaker TTL + Exponential Backoff

**Files:**
- Modify: `trading/claude_caller.py` (module state, both call sites, both exception handlers, remove `is_claude_disabled`)
- Test: `tests/test_pipeline.py`

`_CLAUDE_DISABLED` is a `threading.Event` that is set on 402/429 and **never cleared**. Any rate-limit hit kills all Claude calls for the rest of the day.

Replace with a monotonic timestamp (`_CLAUDE_DISABLED_UNTIL`) and TTL-based auto-recovery:
- 429 → 5-min TTL
- 402 → 30-min TTL + Telegram alert
- At each check: if TTL expired, clear event and allow the call as a probe
- On probe failure: new TTL = `min(max(base_ttl, remaining * 2), 1800)`

- [ ] **Step 1: Write the failing tests**

Add new class to `tests/test_pipeline.py`:

```python
class TestCircuitBreakerTTL:

    def setup_method(self):
        from trading import claude_caller
        claude_caller._CLAUDE_DISABLED.clear()
        claude_caller._CLAUDE_DISABLED_UNTIL = 0.0
        claude_caller._CLAUDE_DISABLED_REASON = ""

    def teardown_method(self):
        from trading import claude_caller
        claude_caller._CLAUDE_DISABLED.clear()
        claude_caller._CLAUDE_DISABLED_UNTIL = 0.0
        claude_caller._CLAUDE_DISABLED_REASON = ""

    @pytest.mark.unit
    def test_429_sets_5min_ttl(self):
        """HTTP 429 trips circuit breaker with ~300s TTL."""
        import anthropic
        from unittest.mock import MagicMock, patch
        from trading import claude_caller

        mock_ai = MagicMock()
        exc = anthropic.APIStatusError.__new__(anthropic.APIStatusError)
        exc.status_code = 429
        exc.message = "rate limit exceeded"
        mock_ai.messages.create.side_effect = exc

        snap = MagicMock()
        snap.regime = "NORMAL"
        snap.portfolio_gain_pct = 0.0
        snap.research = {}
        snap.research_meta = {}
        snap.regime_info = {}
        snap.fred_cache = {}
        snap.weights = {}
        snap.strategy_memory = ""

        t_before = time.monotonic()
        with patch("trading.claude_caller.risk_manager.check_entry_budget", return_value=True), \
             patch("trading.claude_caller.build_stock_block", return_value="block"), \
             patch("trading.claude_caller._get_cached_stable_context", return_value="ctx"), \
             patch("trading.claude_caller.build_dynamic_user_context", return_value="dyn"), \
             patch("trading.claude_caller.telegram_notify"):
            claude_caller._ask_claude_batch([("TEST", MagicMock(), None)], snap, mock_ai)

        assert claude_caller._CLAUDE_DISABLED.is_set()
        ttl = claude_caller._CLAUDE_DISABLED_UNTIL - t_before
        assert 290 <= ttl <= 310  # ~5 min

    @pytest.mark.unit
    def test_402_sets_30min_ttl(self):
        """HTTP 402 trips circuit breaker with ~1800s TTL."""
        import anthropic
        from unittest.mock import MagicMock, patch
        from trading import claude_caller

        mock_ai = MagicMock()
        exc = anthropic.APIStatusError.__new__(anthropic.APIStatusError)
        exc.status_code = 402
        exc.message = "billing error"
        mock_ai.messages.create.side_effect = exc

        snap = MagicMock()
        snap.regime = "NORMAL"
        snap.portfolio_gain_pct = 0.0
        snap.research = {}
        snap.research_meta = {}
        snap.regime_info = {}
        snap.fred_cache = {}
        snap.weights = {}
        snap.strategy_memory = ""

        t_before = time.monotonic()
        with patch("trading.claude_caller.risk_manager.check_entry_budget", return_value=True), \
             patch("trading.claude_caller.build_stock_block", return_value="block"), \
             patch("trading.claude_caller._get_cached_stable_context", return_value="ctx"), \
             patch("trading.claude_caller.build_dynamic_user_context", return_value="dyn"), \
             patch("trading.claude_caller.telegram_notify") as mock_tg:
            claude_caller._ask_claude_batch([("TEST", MagicMock(), None)], snap, mock_ai)

        assert claude_caller._CLAUDE_DISABLED.is_set()
        ttl = claude_caller._CLAUDE_DISABLED_UNTIL - t_before
        assert 1790 <= ttl <= 1810  # ~30 min
        mock_tg.notify_claude_disabled.assert_called_once()

    @pytest.mark.unit
    def test_circuit_breaker_clears_after_ttl_expires(self):
        """When TTL is expired, _ask_claude_batch clears the event and allows call."""
        from unittest.mock import MagicMock, patch
        from trading import claude_caller

        # Rig circuit breaker with already-expired TTL
        claude_caller._CLAUDE_DISABLED.set()
        claude_caller._CLAUDE_DISABLED_UNTIL = time.monotonic() - 1.0  # expired

        mock_ai = MagicMock()
        mock_ai.messages.create.return_value = MagicMock(
            content=[MagicMock(text='{"TEST": {"action": "HOLD", "confidence": 0, '
                                    '"reasoning": "ok", "stop_loss_pct": 4.0, '
                                    '"take_profit_pct": 5.0, "indicator_signals": {}}}')],
            usage=MagicMock(input_tokens=100, output_tokens=50,
                            cache_creation_input_tokens=0, cache_read_input_tokens=0),
        )

        snap = MagicMock()
        snap.regime = "NORMAL"
        snap.portfolio_gain_pct = 0.0
        snap.research = {}
        snap.research_meta = {}
        snap.regime_info = {}
        snap.fred_cache = {}
        snap.weights = {}
        snap.strategy_memory = ""

        with patch("trading.claude_caller.risk_manager.check_entry_budget", return_value=True), \
             patch("trading.claude_caller.risk_manager.record_usage"), \
             patch("trading.claude_caller.build_stock_block", return_value="block"), \
             patch("trading.claude_caller._get_cached_stable_context", return_value="ctx"), \
             patch("trading.claude_caller.build_dynamic_user_context", return_value="dyn"):
            result = claude_caller._ask_claude_batch([("TEST", MagicMock(), None)], snap, mock_ai)

        # Event cleared; API was called
        assert not claude_caller._CLAUDE_DISABLED.is_set()
        mock_ai.messages.create.assert_called_once()

    @pytest.mark.unit
    def test_backoff_doubles_on_probe_failure(self):
        """Re-trip while TTL has remaining time doubles the TTL."""
        import anthropic
        from unittest.mock import MagicMock, patch
        from trading import claude_caller

        # Set circuit breaker with 200s remaining
        now = time.monotonic()
        claude_caller._CLAUDE_DISABLED_UNTIL = now + 200.0
        claude_caller._CLAUDE_DISABLED.set()
        # Manually clear event to simulate probe (TTL just expired during this test)
        claude_caller._CLAUDE_DISABLED.clear()

        mock_ai = MagicMock()
        exc = anthropic.APIStatusError.__new__(anthropic.APIStatusError)
        exc.status_code = 429
        exc.message = "rate limit"
        mock_ai.messages.create.side_effect = exc

        snap = MagicMock()
        snap.regime = "NORMAL"
        snap.portfolio_gain_pct = 0.0
        snap.research = {}
        snap.research_meta = {}
        snap.regime_info = {}
        snap.fred_cache = {}
        snap.weights = {}
        snap.strategy_memory = ""

        t_before = time.monotonic()
        with patch("trading.claude_caller.risk_manager.check_entry_budget", return_value=True), \
             patch("trading.claude_caller.build_stock_block", return_value="block"), \
             patch("trading.claude_caller._get_cached_stable_context", return_value="ctx"), \
             patch("trading.claude_caller.build_dynamic_user_context", return_value="dyn"), \
             patch("trading.claude_caller.telegram_notify"):
            claude_caller._ask_claude_batch([("TEST", MagicMock(), None)], snap, mock_ai)

        new_ttl = claude_caller._CLAUDE_DISABLED_UNTIL - t_before
        # remaining was ~200s → doubled ≥ 400s (but base is 300, so max(300, 400) = 400)
        assert new_ttl >= 390
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_pipeline.py::TestCircuitBreakerTTL -v
```

Expected: all 4 tests FAIL — circuit breaker has no TTL yet.

- [ ] **Step 3: Add `_CLAUDE_DISABLED_UNTIL` module variable to `trading/claude_caller.py`**

Current module-level state (lines 28-30):
```python
_CLAUDE_DISABLED = threading.Event()
_CLAUDE_DISABLED_REASON: str = ""
_CLAUDE_LOCK = threading.Lock()
```

Change to:
```python
_CLAUDE_DISABLED = threading.Event()
_CLAUDE_DISABLED_REASON: str = ""
_CLAUDE_DISABLED_UNTIL: float = 0.0
_CLAUDE_LOCK = threading.Lock()
```

- [ ] **Step 4: Remove `is_claude_disabled()` (dead code, lines 49-51)**

Delete:
```python
def is_claude_disabled() -> bool:
    return _CLAUDE_DISABLED.is_set()
```

- [ ] **Step 5: Update the check site in `_ask_claude_batch` (lines 80-83)**

Current:
```python
    with _CLAUDE_LOCK:
        if _CLAUDE_DISABLED.is_set():
            logger.warning("Claude disabled (%s) — HOLD for %s", _CLAUDE_DISABLED_REASON, symbols)
            return {sym: _default_hold(sym) for sym in symbols}
```

Change to:
```python
    with _CLAUDE_LOCK:
        if _CLAUDE_DISABLED.is_set():
            if time.monotonic() >= _CLAUDE_DISABLED_UNTIL:
                _CLAUDE_DISABLED.clear()
                _CLAUDE_DISABLED_REASON = ""
            else:
                logger.warning("Claude disabled (%s) — HOLD for %s", _CLAUDE_DISABLED_REASON, symbols)
                return {sym: _default_hold(sym) for sym in symbols}
```

- [ ] **Step 6: Update the exception handler in `_ask_claude_batch` (lines 145-158)**

Current:
```python
    except anthropic.APIStatusError as exc:
        if exc.status_code in (402, 429):
            with _CLAUDE_LOCK:
                _CLAUDE_DISABLED_REASON = f"HTTP {exc.status_code} — {exc.message[:120]}"
                _CLAUDE_DISABLED.set()
            logger.error(
                "CLAUDE API ERROR — circuit breaker tripped: %s\n"
                "  Resolve at https://console.anthropic.com — all Claude calls suspended until restart.",
                _CLAUDE_DISABLED_REASON,
            )
            telegram_notify.notify_claude_disabled(_CLAUDE_DISABLED_REASON)
        else:
            logger.error("Claude API error HTTP %d: %s", exc.status_code, exc.message[:200])
        return {sym: _default_hold(sym) for sym in symbols}
```

Change to:
```python
    except anthropic.APIStatusError as exc:
        if exc.status_code in (402, 429):
            global _CLAUDE_DISABLED_UNTIL
            base_ttl = 1800.0 if exc.status_code == 402 else 300.0
            with _CLAUDE_LOCK:
                remaining = max(0.0, _CLAUDE_DISABLED_UNTIL - time.monotonic())
                new_ttl = min(max(base_ttl, remaining * 2), 1800.0)
                _CLAUDE_DISABLED_REASON = f"HTTP {exc.status_code} — {exc.message[:120]}"
                _CLAUDE_DISABLED_UNTIL = time.monotonic() + new_ttl
                _CLAUDE_DISABLED.set()
            logger.error(
                "CLAUDE API ERROR — circuit breaker tripped for %.0fs: %s",
                new_ttl, _CLAUDE_DISABLED_REASON,
            )
            if exc.status_code == 402:
                telegram_notify.notify_claude_disabled(_CLAUDE_DISABLED_REASON)
        else:
            logger.error("Claude API error HTTP %d: %s", exc.status_code, exc.message[:200])
        return {sym: _default_hold(sym) for sym in symbols}
```

- [ ] **Step 7: Apply identical changes to `_ask_claude_exit_batch`**

Update the check site in `_ask_claude_exit_batch` (lines 196-198):

Current:
```python
    with _CLAUDE_LOCK:
        if _CLAUDE_DISABLED.is_set():
            return {sym: _default_hold(sym) for sym in symbols}
```

Change to:
```python
    with _CLAUDE_LOCK:
        if _CLAUDE_DISABLED.is_set():
            if time.monotonic() >= _CLAUDE_DISABLED_UNTIL:
                _CLAUDE_DISABLED.clear()
                _CLAUDE_DISABLED_REASON = ""
            else:
                return {sym: _default_hold(sym) for sym in symbols}
```

Update the exception handler in `_ask_claude_exit_batch` (lines 231-238):

Current:
```python
    except anthropic.APIStatusError as exc:
        if exc.status_code in (402, 429):
            with _CLAUDE_LOCK:
                _CLAUDE_DISABLED_REASON = f"HTTP {exc.status_code} — {exc.message[:120]}"
                _CLAUDE_DISABLED.set()
            telegram_notify.notify_claude_disabled(_CLAUDE_DISABLED_REASON)
        logger.error("Claude exit API error HTTP %d: %s", exc.status_code, exc.message[:200])
        return {sym: _default_hold(sym) for sym in symbols}
```

Change to:
```python
    except anthropic.APIStatusError as exc:
        if exc.status_code in (402, 429):
            base_ttl = 1800.0 if exc.status_code == 402 else 300.0
            with _CLAUDE_LOCK:
                remaining = max(0.0, _CLAUDE_DISABLED_UNTIL - time.monotonic())
                new_ttl = min(max(base_ttl, remaining * 2), 1800.0)
                _CLAUDE_DISABLED_REASON = f"HTTP {exc.status_code} — {exc.message[:120]}"
                _CLAUDE_DISABLED_UNTIL = time.monotonic() + new_ttl
                _CLAUDE_DISABLED.set()
            if exc.status_code == 402:
                telegram_notify.notify_claude_disabled(_CLAUDE_DISABLED_REASON)
        logger.error("Claude exit API error HTTP %d: %s", exc.status_code, exc.message[:200])
        return {sym: _default_hold(sym) for sym in symbols}
```

- [ ] **Step 8: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_pipeline.py::TestCircuitBreakerTTL -v
```

Expected: all 4 tests PASS.

- [ ] **Step 9: Run full pipeline test suite**

```bash
.venv/bin/pytest tests/test_pipeline.py -v
```

Expected: all tests PASS.

- [ ] **Step 10: Commit**

```bash
git add trading/claude_caller.py tests/test_pipeline.py
git commit -m "fix: circuit breaker TTL — 429→5min, 402→30min, exponential backoff, auto-recovery"
```

---

## Task 4: Fix 3 — `daily_closed` as Layer 0 in Pre-Claude Filter

**Files:**
- Modify: `trading/pre_claude_filter.py:21` (new signature + Layer 0)
- Modify: `trading/pipeline.py:113` (capture return value)
- Modify: `trading/pipeline.py:151` (pass `daily_closed` to filter)
- Modify: `tests/test_pipeline.py` (update existing `TestPreClaudeFilter` call sites + new class)

Currently `pipeline.py:113` calls `load_daily_closed()` and discards the return value. Symbols already closed today reach Claude before being blocked in `executor.py` — wasting API budget.

- [ ] **Step 1: Write new tests**

Add new class to `tests/test_pipeline.py`:

```python
class TestPreClaudeFilterDailyClosed:

    def _snap(self):
        from trading.cycle_context import CycleSnapshot
        return CycleSnapshot(
            regime_info={"regime": "NORMAL", "macro_health_score": 0},
            regime="NORMAL",
            fred_cache={},
            research={"TEST": {"sentiment_score": 20}},
            research_meta={"macro_shock_score": 0},
            strategy_memory="",
            weights={},
            params={},
            portfolio_gain_pct=0.0,
            intraday_news={},
        )

    def _iv(self, composite=50.0):
        from tests.test_signals import TestFastGate
        return TestFastGate()._iv(composite_score=composite, rsi=55.0, stoch_d=50.0)

    @pytest.mark.unit
    def test_layer0_skips_already_closed_symbol(self):
        """Symbol in daily_closed returns SKIP before any other layer."""
        from trading.pre_claude_filter import pre_claude_filter
        result = pre_claude_filter("TEST", self._iv(composite=80.0), self._snap(),
                                   daily_closed={"TEST"})
        assert result.decision == "SKIP"
        assert "already closed" in result.reason

    @pytest.mark.unit
    def test_layer0_passes_symbol_not_in_closed(self):
        """Symbol absent from daily_closed proceeds through remaining layers."""
        from trading.pre_claude_filter import pre_claude_filter
        result = pre_claude_filter("TEST", self._iv(composite=50.0), self._snap(),
                                   daily_closed={"OTHER"})
        assert result.decision != "SKIP" or "composite" in result.reason or "closed" not in result.reason

    @pytest.mark.unit
    def test_layer0_skips_with_empty_set_parameter(self):
        """Passing an empty set never triggers Layer 0 SKIP."""
        from trading.pre_claude_filter import pre_claude_filter
        result = pre_claude_filter("TEST", self._iv(composite=50.0), self._snap(),
                                   daily_closed=set())
        assert result.decision in ("AUTO_EXECUTE", "CLAUDE", "SKIP")
        if result.decision == "SKIP":
            assert "already closed" not in result.reason

    @pytest.mark.unit
    def test_layer0_passes_when_daily_closed_is_none(self):
        """daily_closed=None (default) is safe — Layer 0 is skipped entirely."""
        from trading.pre_claude_filter import pre_claude_filter
        result = pre_claude_filter("TEST", self._iv(composite=50.0), self._snap())
        assert result.decision in ("AUTO_EXECUTE", "CLAUDE", "SKIP")
        if result.decision == "SKIP":
            assert "already closed" not in result.reason
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_pipeline.py::TestPreClaudeFilterDailyClosed -v
```

Expected: `test_layer0_skips_already_closed_symbol` FAIL — `pre_claude_filter` doesn't accept `daily_closed` yet.

- [ ] **Step 3: Update `pre_claude_filter` signature and add Layer 0 in `trading/pre_claude_filter.py`**

Current signature (line 21):
```python
def pre_claude_filter(symbol: str, iv: IndicatorValues, snap: CycleSnapshot) -> FilterResult:
```

Change to:
```python
def pre_claude_filter(
    symbol: str,
    iv: IndicatorValues,
    snap: CycleSnapshot,
    daily_closed: set[str] | None = None,
) -> FilterResult:
```

Add Layer 0 as the very first check inside the function (before Layer 1):
```python
    # Layer 0 — Already closed today
    if daily_closed and symbol in daily_closed:
        return FilterResult("SKIP", "already closed today")
```

- [ ] **Step 4: Capture `daily_closed` in `trading/pipeline.py:113`**

Current (line 113):
```python
        load_daily_closed()
```

Change to:
```python
        daily_closed = load_daily_closed()
```

- [ ] **Step 5: Pass `daily_closed` to `pre_claude_filter` in `trading/pipeline.py:151`**

Current:
```python
                filt = pre_claude_filter(symbol, iv, snap)
```

Change to:
```python
                filt = pre_claude_filter(symbol, iv, snap, daily_closed)
```

Note: `daily_closed` is only defined inside the `else` block (when budget is not exhausted). In Task 5 we will restructure that loop — at that point `daily_closed` will be in scope for the entire symbol loop.

- [ ] **Step 6: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_pipeline.py::TestPreClaudeFilterDailyClosed -v
```

Expected: all 4 tests PASS.

- [ ] **Step 7: Update existing `TestPreClaudeFilter` tests to use the new default parameter**

The existing tests call `pre_claude_filter(symbol, iv, snap)` — they continue to work because `daily_closed` defaults to `None`. No call-site changes needed.

Verify no existing tests broke:

```bash
.venv/bin/pytest tests/test_pipeline.py::TestPreClaudeFilter -v
```

Expected: all tests PASS.

- [ ] **Step 8: Commit**

```bash
git add trading/pre_claude_filter.py trading/pipeline.py tests/test_pipeline.py
git commit -m "fix: daily_closed as Layer 0 in pre_claude_filter — blocks re-entries before Claude"
```

---

## Task 5: Fix 5 — Auto-Execute Outside Budget Gate

**Files:**
- Modify: `trading/pipeline.py:143-159` (Phase 2 restructure)
- Test: `tests/test_pipeline.py`

Currently the entire pre-filter loop (including auto-execute) is wrapped in `if not is_entry_budget_exhausted()`. When budget is exhausted, auto-execute candidates — which cost $0 in Claude API budget — are never collected.

Restructure: always run the filter loop; only gate `needs_claude` appends on budget.

- [ ] **Step 1: Write the failing test**

Add new class to `tests/test_pipeline.py`:

```python
class TestAutoExecuteWhenBudgetExhausted:

    def _make_iv(self, symbol, composite, rsi=55.0, stoch_d=50.0, current_price=100.0):
        from tests.test_signals import TestFastGate
        return TestFastGate()._iv(
            symbol=symbol, composite_score=composite, rsi=rsi,
            stoch_d=stoch_d, current_price=current_price,
        )

    def _snap(self, intraday_news=None):
        from trading.cycle_context import CycleSnapshot
        return CycleSnapshot(
            regime_info={"regime": "NORMAL", "macro_health_score": 0, "spy_5day_return": 0.0},
            regime="NORMAL",
            fred_cache={},
            research={"AUTO": {"sentiment_score": 25}},
            research_meta={"macro_shock_score": 0},
            strategy_memory="",
            weights={},
            params={
                "rsi_buy_max": 72, "rsi_buy_max_high_vol": 65,
                "high_vol_position_mult": 0.5,
                "entry_start_hour": 10, "entry_end_hour": 15,
            },
            portfolio_gain_pct=0.0,
            intraday_news=intraday_news or {},
        )

    @pytest.mark.unit
    def test_auto_execute_runs_when_budget_exhausted(self):
        """AUTO_EXECUTE candidates are collected and execute_buy is called even when entry budget exhausted."""
        from trading.pipeline import TradingPipeline
        from trading import risk_manager
        import config

        # Stock that will pass all gates and hit AUTO_EXECUTE threshold
        iv = self._make_iv("AUTO", composite=float(config.AUTO_EXECUTE_COMPOSITE_MIN + 5))
        snap = self._snap(intraday_news={"AUTO": {
            "shock_score": 0,
            "sentiment_score": config.AUTO_EXECUTE_SENTIMENT_MIN + 5,
        }})

        risk_manager._entry_exhausted_flag = True
        try:
            with patch("trading.pipeline._fetch_symbol_data", return_value=("AUTO", iv, None, 0.0)), \
                 patch("trading.pipeline._ask_claude_batch") as mock_claude, \
                 patch("trading.pipeline.execute_buy", return_value=None) as mock_buy, \
                 patch("trading.pipeline.load_open_positions", return_value={}), \
                 patch("trading.pipeline.load_daily_closed", return_value=set()), \
                 patch("trading.pipeline._ask_claude_exit_batch", return_value={}):
                TradingPipeline().run(
                    symbols=["AUTO"], snap=snap,
                    ai=MagicMock(), now=datetime(2026, 5, 13, 10, 30, tzinfo=ET),
                    alpaca=MagicMock(), portfolio_value=10000, buying_power=5000,
                    can_open=True,
                )
            mock_claude.assert_not_called()
            mock_buy.assert_called_once()
        finally:
            risk_manager._entry_exhausted_flag = False

    @pytest.mark.unit
    def test_claude_candidates_blocked_when_budget_exhausted(self):
        """Symbols that would go to Claude are held when entry budget exhausted."""
        from trading.pipeline import TradingPipeline
        from trading import risk_manager

        iv = self._make_iv("HOLD_ME", composite=42.0)
        snap = self._snap(intraday_news={"HOLD_ME": {"shock_score": 0, "sentiment_score": 5}})

        risk_manager._entry_exhausted_flag = True
        try:
            with patch("trading.pipeline._fetch_symbol_data", return_value=("HOLD_ME", iv, None, 0.0)), \
                 patch("trading.pipeline._ask_claude_batch") as mock_claude, \
                 patch("trading.pipeline.load_open_positions", return_value={}), \
                 patch("trading.pipeline.load_daily_closed", return_value=set()), \
                 patch("trading.pipeline._ask_claude_exit_batch", return_value={}):
                TradingPipeline().run(
                    symbols=["HOLD_ME"], snap=snap,
                    ai=MagicMock(), now=datetime(2026, 5, 13, 10, 30, tzinfo=ET),
                    alpaca=MagicMock(), portfolio_value=10000, buying_power=5000,
                    can_open=True,
                )
            mock_claude.assert_not_called()
        finally:
            risk_manager._entry_exhausted_flag = False
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_pipeline.py::TestAutoExecuteWhenBudgetExhausted -v
```

Expected: `test_auto_execute_runs_when_budget_exhausted` FAIL — `execute_buy` is never called when flag is True.

- [ ] **Step 3: Restructure Phase 2 in `trading/pipeline.py:138-159`**

Current Phase 2 block:
```python
        # ── Phase 2: Pre-Claude filter (budget pre-check + 5-layer filter) ────
        t_gates = time.monotonic()
        needs_claude:  list[tuple[str, IndicatorValues, dict | None]] = []
        auto_execute:  list[tuple[str, IndicatorValues]] = []

        if risk_manager.is_entry_budget_exhausted():
            logger.info("Entry budget exhausted — skipping Claude phase entirely.")
        else:
            for symbol, iv, existing_pos, _ret5d in stock_data:
                if existing_pos:
                    pnl_pct = (iv.current_price - existing_pos["entry_price"]) / existing_pos["entry_price"] * 100
                    logger.info("BRACKET-MANAGED %s: P&L=%+.2f%% composite=%+.1f", symbol, pnl_pct, iv.composite_score)
                    continue
                filt = pre_claude_filter(symbol, iv, snap, daily_closed)
                if filt.decision == "SKIP":
                    logger.info("SKIP-PRECLAUDE %s: composite=%+.1f — %s", symbol, iv.composite_score, filt.reason)
                elif filt.decision == "AUTO_EXECUTE":
                    auto_execute.append((symbol, iv))
                    logger.info("AUTO-EXECUTE %s: composite=%+.1f — %s", symbol, iv.composite_score, filt.reason)
                else:
                    needs_claude.append((symbol, iv, None))
```

Replace with:
```python
        # ── Phase 2: Pre-Claude filter ────────────────────────────────────────
        # Auto-execute is always collected (zero API budget cost).
        # Claude candidates are only collected when entry budget allows.
        t_gates = time.monotonic()
        needs_claude:  list[tuple[str, IndicatorValues, dict | None]] = []
        auto_execute:  list[tuple[str, IndicatorValues]] = []

        for symbol, iv, existing_pos, _ret5d in stock_data:
            if existing_pos:
                pnl_pct = (iv.current_price - existing_pos["entry_price"]) / existing_pos["entry_price"] * 100
                logger.info("BRACKET-MANAGED %s: P&L=%+.2f%% composite=%+.1f", symbol, pnl_pct, iv.composite_score)
                continue
            filt = pre_claude_filter(symbol, iv, snap, daily_closed)
            if filt.decision == "SKIP":
                logger.info("SKIP-PRECLAUDE %s: composite=%+.1f — %s", symbol, iv.composite_score, filt.reason)
            elif filt.decision == "AUTO_EXECUTE":
                auto_execute.append((symbol, iv))
                logger.info("AUTO-EXECUTE %s: composite=%+.1f — %s", symbol, iv.composite_score, filt.reason)
            elif not risk_manager.is_entry_budget_exhausted():
                needs_claude.append((symbol, iv, None))
            else:
                logger.info("BUDGET-EXHAUSTED HOLD %s: would need Claude", symbol)
```

Note: `daily_closed` is now defined unconditionally at line 113 (from Task 4 Step 4), so it is always in scope here.

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_pipeline.py::TestAutoExecuteWhenBudgetExhausted -v
```

Expected: both tests PASS.

- [ ] **Step 5: Run full pipeline tests**

```bash
.venv/bin/pytest tests/test_pipeline.py -v
```

Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add trading/pipeline.py tests/test_pipeline.py
git commit -m "fix: auto-execute collected outside budget gate — zero-API-cost trades run even when budget exhausted"
```

---

## Task 6: Fix 4 + Dead Code — Eliminate Redundant Disk Reads

**Files:**
- Modify: `trading/cycle_context.py:66` (accept optional `regime_info`, `params`)
- Modify: `trading_agent.py:213-266` (pass cached values, remove dead fred_cache load)
- Modify: `trading_agent.py:34` (remove dead import)
- Modify: `trading_agent.py:61-69` (remove dead `load_research` / `load_weights` functions)

`run_trading_cycle()` loads `regime_info` and `params` at lines 213/215, then `build_cycle_snapshot()` loads them again internally — 2 duplicate disk reads per cycle = 72 extra reads/day. The `fred_cache` load at lines 217-220 is pure dead code (variable never used after assignment).

Additionally three dead items exist in `trading_agent.py`: an unused import, and two functions that were superseded by `build_cycle_snapshot`.

- [ ] **Step 1: Extend `build_cycle_snapshot` signature in `trading/cycle_context.py`**

Current (line 66):
```python
def build_cycle_snapshot(portfolio_gain_pct: float) -> CycleSnapshot:
```

Change to:
```python
def build_cycle_snapshot(
    portfolio_gain_pct: float,
    *,
    regime_info: dict | None = None,
    params: dict | None = None,
) -> CycleSnapshot:
```

Update the body (lines 71-74) to use the optional passed values:

Current:
```python
    regime_info = load_macro_regime()
    research    = load_json(config.RESEARCH_REPORT_FILE, {})
    weights     = load_json(config.INDICATOR_WEIGHTS_FILE, {})
    params      = load_trading_params()
```

Change to:
```python
    regime_info = regime_info if regime_info is not None else load_macro_regime()
    params      = params if params is not None else load_trading_params()
    research    = load_json(config.RESEARCH_REPORT_FILE, {})
    weights     = load_json(config.INDICATOR_WEIGHTS_FILE, {})
```

- [ ] **Step 2: Update `run_trading_cycle` in `trading_agent.py` to pass cached values**

Current lines 213-266:
```python
    regime_info = load_macro_regime()
    regime      = regime_info.get("regime", "NORMAL")
    params      = load_trading_params()

    try:
        fred_cache = load_json(config.FRED_CACHE_FILE, {})
    except Exception:
        fred_cache = {}

    # Flash crash lockout check
    ...

    portfolio_gain_pct = (portfolio_value - last_equity) / last_equity * 100 if last_equity else 0.0
    snap = build_cycle_snapshot(portfolio_gain_pct=portfolio_gain_pct)
```

Remove the dead `fred_cache` block (the 4-line try/except) and pass `regime_info`/`params` to `build_cycle_snapshot`:

```python
    regime_info = load_macro_regime()
    regime      = regime_info.get("regime", "NORMAL")
    params      = load_trading_params()

    # Flash crash lockout check
    ...

    portfolio_gain_pct = (portfolio_value - last_equity) / last_equity * 100 if last_equity else 0.0
    snap = build_cycle_snapshot(
        portfolio_gain_pct=portfolio_gain_pct,
        regime_info=regime_info,
        params=params,
    )
```

- [ ] **Step 3: Remove three dead code items from `trading_agent.py`**

**3a. Remove dead import (line 34):**

Delete:
```python
from signals.entry_gates import check_all_gates
```

**3b. Remove dead `load_research` and `load_weights` functions (lines 61-69):**

Delete:
```python
def load_research() -> dict:
    data = load_json(config.RESEARCH_REPORT_FILE, {})
    if not any(k for k in data if k != "__meta__"):
        logger.warning("Research report is empty — Claude will have no sentiment context this cycle")
    return data


def load_weights() -> dict:
    return load_json(config.INDICATOR_WEIGHTS_FILE, {})
```

- [ ] **Step 4: Run the full test suite to verify no regressions**

```bash
.venv/bin/pytest tests/test_pipeline.py tests/test_trading.py tests/test_foundation.py -v
```

Expected: all tests PASS. If `load_research` or `load_weights` are imported by any test, those imports will fail — fix by updating the test import.

- [ ] **Step 5: Verify `trading_agent.py` has no remaining references to removed items**

```bash
grep -n "check_all_gates\|load_research\|load_weights\|fred_cache" ~/Documents/projects/trading-bot/trading_agent.py
```

Expected: no output (all removed).

- [ ] **Step 6: Commit**

```bash
git add trading/cycle_context.py trading_agent.py
git commit -m "fix: eliminate 2 redundant disk reads/cycle + remove 3 dead code items in trading_agent"
```

---

## Task 7: Final Verification

**Files:** None — verification only.

- [ ] **Step 1: Run the full test suite**

```bash
cd ~/Documents/projects/trading-bot
.venv/bin/pytest tests/ -v -m unit 2>&1 | tail -30
```

Expected: all unit tests PASS with no failures or errors.

- [ ] **Step 2: Check no dead identifiers remain**

```bash
grep -rn "is_claude_disabled\|gain_pct\|load_research\|load_weights\|check_all_gates" \
  --include="*.py" \
  | grep -v "test_\|\.pyc\|# " \
  | grep -v "docs/"
```

Expected: no output except possibly references inside docstrings or comments being cleaned up. Any source-file match is a missed removal.

- [ ] **Step 3: Spot-check Phase 2 loop structure in `pipeline.py`**

```bash
grep -n "is_entry_budget_exhausted\|auto_execute\|needs_claude\|daily_closed" \
  ~/Documents/projects/trading-bot/trading/pipeline.py
```

Expected output (rough):
```
112: ...
113:         daily_closed = load_daily_closed()
...
143:         for symbol, iv, existing_pos, _ret5d in stock_data:
...
155:             elif filt.decision == "AUTO_EXECUTE":
...
158:             elif not risk_manager.is_entry_budget_exhausted():
...
```

The `is_entry_budget_exhausted()` call must be *inside* the for-loop (not wrapping it).

- [ ] **Step 4: Final commit summary**

```bash
git log --oneline -6
```

Expected output (in order, newest first):
```
<sha>  fix: eliminate 2 redundant disk reads/cycle + remove 3 dead code items in trading_agent
<sha>  fix: auto-execute collected outside budget gate — zero-API-cost trades run even when budget exhausted
<sha>  fix: daily_closed as Layer 0 in pre_claude_filter — blocks re-entries before Claude
<sha>  fix: circuit breaker TTL — 429→5min, 402→30min, exponential backoff, auto-recovery
<sha>  fix: sync _entry_exhausted_flag from disk on each budget check (day-reset)
<sha>  fix: remove dead gain_pct param from check_entry_budget
```
