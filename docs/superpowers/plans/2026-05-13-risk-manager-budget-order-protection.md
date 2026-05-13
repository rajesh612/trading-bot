# Risk Manager: Budget Reservation + Order Protection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create `trading/risk_manager.py` as the single risk authority (absorbing `api_budget.py`), add a 70/30 entry/exit budget split with auto-escalation, add a thesis-break exit evaluation phase in the trading pipeline, add a gap-up entry guard, and correct bracket SL/TP prices after fill.

**Architecture:** `trading/risk_manager.py` owns all risk decisions — budget pool management, gap guard, thesis-break heuristic, and fill correction. `api_budget.py` is deleted; all 6 callers updated. A new Phase 2.5 in `trading/pipeline.py` evaluates open positions for thesis breaks before new entry candidates hit Claude. Exit calls use a compact ~300-token prompt from the reserved 30% budget pool and never burn entry budget. Fill correction polls Alpaca after every bracket order placement and replaces child SL/TP legs if actual fill price deviates > 0.5% from the indicator price.

**Tech Stack:** Python 3.12, anthropic SDK, alpaca-py, pytest (`pytest tests/ -v -m unit`)

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `config.py` | Modify | Add 8 new risk-manager constants |
| `indicators/values.py` | Modify | Add `prev_close: float = 0.0` field to `IndicatorValues` |
| `indicators/engine.py` | Modify | Populate `prev_close` from `daily_df` in `compute_indicators()` |
| `telegram_notify.py` | Modify | Add `notify_budget_escalation(spent, new_ceiling)` |
| `trading/risk_manager.py` | **Create** | Budget pools, gap guard, thesis heuristic, fill correction |
| `signals/entry_gates.py` | Modify | Add gate 12: gap-up guard reading `iv.prev_close` |
| `trading/context_builder.py` | Modify | Add `build_exit_stock_block()` for compact exit prompts |
| `trading/claude_caller.py` | Modify | Add `_ask_claude_exit_batch()`; use `check_entry_budget` |
| `trading/pipeline.py` | Modify | Add Phase 2.5 (thesis-break eval); wire exit decisions in Phase 4 |
| `trading/executor.py` | Modify | Call `risk_manager.correct_bracket_after_fill()` after BUY order |
| `api_budget.py` | **Delete** | Absorbed into `trading/risk_manager.py` |
| `watch_list_agent.py` | Modify | `import api_budget` → `from trading import risk_manager` |
| `research/analyzer.py` | Modify | `import api_budget` → `from trading import risk_manager` |
| `learning/agent.py` | Modify | `import api_budget` → `from trading import risk_manager` |
| `tests/test_market.py` | Modify | Update all `api_budget` imports/calls to `risk_manager` |
| `tests/test_trading.py` | Modify | Add 4 new test classes; update existing `api_budget` patches |

---

## Task 1: Config constants

**Files:**
- Modify: `config.py` (after line 198, after `PROFIT_LOCK_TRIGGER_PCT`)

- [ ] **Step 1: Add the 8 constants to `config.py`**

Open `config.py`. Find `PROFIT_LOCK_TRIGGER_PCT = 2.0` (last line). Add after it:

```python

# ── Risk Manager ──────────────────────────────────────────────────────────────
ENTRY_BUDGET_RATIO          = 0.70   # fraction of daily ceiling allocated to new entry calls
EXIT_BUDGET_RATIO           = 0.30   # fraction reserved for thesis-break exit evaluation calls
EXIT_EVAL_COMPOSITE_DROP    = 10     # composite must drop >= this pts from entry score to trigger eval
EXIT_EVAL_SENTIMENT_FLOOR   = -30    # sentiment below this triggers exit evaluation
GAP_SKIP_PCT                = 3.0    # skip entry if current_price gapped > this % above prev close
FILL_SLIPPAGE_THRESHOLD     = 0.005  # correct bracket legs if fill deviates > 0.5% from iv price
FILL_POLL_ATTEMPTS          = 5      # max Alpaca polls for fill price confirmation after placement
FILL_POLL_INTERVAL_S        = 0.5    # seconds between fill poll attempts (total max wait = 2.5s)
```

- [ ] **Step 2: Run the test suite to confirm nothing broke**

```bash
pytest tests/ -v -m unit -x -q 2>&1 | tail -5
```

Expected: all existing tests pass (no new tests yet).

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "feat: add risk-manager config constants (budget ratios, gap guard, fill correction)"
```

---

## Task 2: `IndicatorValues.prev_close` + engine population

**Files:**
- Modify: `indicators/values.py` (end of dataclass)
- Modify: `indicators/engine.py` (before the `return IndicatorValues(...)` block, around line 153)
- Test: `tests/test_indicators.py`

- [ ] **Step 1: Write failing test in `tests/test_indicators.py`**

Open `tests/test_indicators.py`. Add this test class at the end of the file:

```python
@pytest.mark.unit
class TestPrevCloseField:
    """IndicatorValues carries prev_close from daily bars."""

    def _make_bars(self, n: int = 60) -> pd.DataFrame:
        import numpy as np
        from zoneinfo import ZoneInfo
        idx = pd.date_range("2024-01-15 09:30", periods=n, freq="5min", tz="America/New_York")
        rng = np.random.default_rng(42)
        close = 100.0 + rng.standard_normal(n).cumsum()
        return pd.DataFrame({
            "open": close - 0.1, "high": close + 0.3,
            "low": close - 0.3, "close": close, "volume": np.full(n, 500_000),
        }, index=idx)

    def _make_daily(self, closes: list) -> pd.DataFrame:
        import numpy as np
        idx = pd.date_range("2024-01-08", periods=len(closes), freq="B")
        c = np.array(closes, dtype=float)
        return pd.DataFrame({
            "open": c * 0.99, "high": c * 1.01, "low": c * 0.98,
            "close": c, "volume": np.full(len(closes), 10_000_000),
        }, index=idx)

    def test_prev_close_populated_from_daily_bars(self):
        from indicator_engine import compute_indicators
        bars = self._make_bars()
        daily = self._make_daily([95.0, 97.0, 98.5, 99.0, 101.0])
        iv = compute_indicators("TEST", bars, daily_df=daily)
        assert iv is not None
        # prev_close is daily_df["close"].iloc[-2] = 99.0
        assert abs(iv.prev_close - 99.0) < 0.01

    def test_prev_close_zero_when_daily_missing(self):
        from indicator_engine import compute_indicators
        bars = self._make_bars()
        iv = compute_indicators("TEST", bars, daily_df=None)
        assert iv is not None
        assert iv.prev_close == 0.0

    def test_prev_close_zero_when_only_one_daily_bar(self):
        from indicator_engine import compute_indicators
        bars = self._make_bars()
        daily = self._make_daily([100.0])
        iv = compute_indicators("TEST", bars, daily_df=daily)
        assert iv is not None
        assert iv.prev_close == 0.0
```

- [ ] **Step 2: Run to verify it fails**

```bash
pytest tests/test_indicators.py::TestPrevCloseField -v 2>&1 | tail -10
```

Expected: `AttributeError: 'IndicatorValues' object has no attribute 'prev_close'`

- [ ] **Step 3: Add `prev_close` field to `indicators/values.py`**

Open `indicators/values.py`. Find `stop_hunt_zone: bool = False` (last line). Add after it:

```python
    prev_close: float = 0.0            # daily T-1 close; 0.0 when daily bars unavailable
```

- [ ] **Step 4: Populate `prev_close` in `indicators/engine.py`**

Open `indicators/engine.py`. Find the block starting `daily_closes: list[float] = []` (around line 134). Add the `prev_close` computation immediately after that block:

```python
    prev_close = 0.0
    if daily_df is not None and len(daily_df) >= 2:
        try:
            prev_close = float(daily_df["close"].iloc[-2])
        except Exception:
            pass
```

Then find the `return IndicatorValues(` block (around line 153). Add `prev_close=prev_close,` as an explicit keyword just before the `**{k: v ...}` line:

```python
    return IndicatorValues(
        symbol=symbol,
        timestamp=ts,
        composite_score=composite_score,
        signal=signal,
        confidence=confidence,
        parabolic=is_parabolic,
        volume_exhaustion=vol_exhaustion,
        obv_divergence=obv_div,
        momentum_exhaustion=momentum_exhaust,
        bot_herding_score=0,
        squeeze_setup=_squeeze,
        squeeze_confidence=_squeeze_conf,
        stop_hunt_zone=stop_hunt,
        prev_close=prev_close,
        **{k: v for k, v in vals.items() if k not in ("obv_trend",)},
    )
```

- [ ] **Step 5: Run tests to verify passing**

```bash
pytest tests/test_indicators.py::TestPrevCloseField -v 2>&1 | tail -10
```

Expected: 3 tests PASSED.

- [ ] **Step 6: Run full suite to check for regressions**

```bash
pytest tests/ -v -m unit -x -q 2>&1 | tail -5
```

Expected: all existing tests still pass.

- [ ] **Step 7: Commit**

```bash
git add indicators/values.py indicators/engine.py tests/test_indicators.py
git commit -m "feat: add prev_close to IndicatorValues; populate from daily_df T-1 close"
```

---

## Task 3: `telegram_notify.notify_budget_escalation()`

**Files:**
- Modify: `telegram_notify.py` (after `notify_claude_disabled`)
- Test: `tests/test_trading.py`

- [ ] **Step 1: Write failing test in `tests/test_trading.py`**

Open `tests/test_trading.py`. Add this class at the end of the file:

```python
@pytest.mark.unit
class TestBudgetEscalationNotify:
    """telegram_notify.notify_budget_escalation sends expected message text."""

    def test_notify_sends_message(self, monkeypatch):
        sent = []
        monkeypatch.setattr("telegram_notify._send", lambda text: sent.append(text))
        import telegram_notify
        telegram_notify.notify_budget_escalation(spent=0.32, new_ceiling=1.50)
        assert len(sent) == 1
        assert "escalated" in sent[0].lower() or "budget" in sent[0].lower()
        assert "1.50" in sent[0]

    def test_notify_no_raise_on_missing_config(self, monkeypatch):
        import config, telegram_notify
        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
        telegram_notify.notify_budget_escalation(spent=0.32, new_ceiling=1.50)
        # must not raise
```

- [ ] **Step 2: Run to verify it fails**

```bash
pytest tests/test_trading.py::TestBudgetEscalationNotify -v 2>&1 | tail -5
```

Expected: `AttributeError: module 'telegram_notify' has no attribute 'notify_budget_escalation'`

- [ ] **Step 3: Add function to `telegram_notify.py`**

Open `telegram_notify.py`. Find `notify_claude_disabled` function. Add after it (maintain fire-and-forget pattern):

```python
def notify_budget_escalation(spent: float, new_ceiling: float) -> None:
    _send(
        f"<b>⚠️ API Budget Escalated</b>\n"
        f"Exit pool exhausted with open positions — ceiling raised to <b>${new_ceiling:.2f}</b>\n"
        f"Total spent so far: ${spent:.3f}"
    )
```

- [ ] **Step 4: Run tests to verify passing**

```bash
pytest tests/test_trading.py::TestBudgetEscalationNotify -v 2>&1 | tail -5
```

Expected: 2 tests PASSED.

- [ ] **Step 5: Commit**

```bash
git add telegram_notify.py tests/test_trading.py
git commit -m "feat: add notify_budget_escalation to telegram_notify"
```

---

## Task 4: `trading/risk_manager.py` — budget core

**Files:**
- Create: `trading/risk_manager.py`
- Test: `tests/test_trading.py`

- [ ] **Step 1: Write failing tests in `tests/test_trading.py`**

Add this class after `TestBudgetEscalationNotify`:

```python
@pytest.mark.unit
class TestRiskManagerBudget:
    """Budget pool split, escalation, and record_usage routing."""

    def _setup(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "API_BUDGET_FILE", tmp_path / "api_budget.json")
        monkeypatch.setattr(config, "API_BUDGET_NORMAL_CEILING", 1.00)
        monkeypatch.setattr(config, "API_BUDGET_ELEVATED_CEILING", 1.50)
        monkeypatch.setattr(config, "ENTRY_BUDGET_RATIO", 0.70)
        monkeypatch.setattr(config, "EXIT_BUDGET_RATIO", 0.30)
        monkeypatch.setattr(config, "API_BUDGET_WARNING_PCT", 0.80)

    def test_entry_budget_allows_when_under_limit(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        from trading import risk_manager
        assert risk_manager.check_entry_budget(0.30) is True

    def test_entry_budget_blocks_when_entry_pool_spent(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        from trading import risk_manager
        risk_manager.record_usage(input_tokens=233_333, output_tokens=0, kind="entry")  # ~$0.70
        assert risk_manager.check_entry_budget(0.05) is False

    def test_exit_budget_escalates_with_open_positions(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        alerts = []
        monkeypatch.setattr("telegram_notify.notify_budget_escalation",
                            lambda spent, new_ceiling: alerts.append(new_ceiling))
        from trading import risk_manager
        risk_manager.record_usage(input_tokens=80_000, output_tokens=0, kind="exit")  # ~$0.24 > $0.30×0.8
        risk_manager.record_usage(input_tokens=20_000, output_tokens=0, kind="exit")  # push over $0.30
        result = risk_manager.check_exit_budget(0.05, open_position_count=2)
        assert result is True          # escalated — new exit pool has room
        assert len(alerts) == 1
        assert alerts[0] == 1.50

    def test_exit_escalation_is_idempotent(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        alerts = []
        monkeypatch.setattr("telegram_notify.notify_budget_escalation",
                            lambda spent, new_ceiling: alerts.append(new_ceiling))
        from trading import risk_manager
        # Spend past exit pool twice
        for _ in range(3):
            risk_manager.record_usage(input_tokens=33_333, output_tokens=0, kind="exit")
        risk_manager.check_exit_budget(0.01, open_position_count=1)
        risk_manager.check_exit_budget(0.01, open_position_count=1)
        assert len(alerts) == 1  # alert fires only once

    def test_record_usage_entry_routes_correctly(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        from trading import risk_manager
        risk_manager.record_usage(input_tokens=10_000, output_tokens=1_000, kind="entry")
        b = risk_manager.load_budget()
        assert b["entry_spent_usd"] > 0
        assert b["exit_spent_usd"] == 0.0
        assert b["entry_calls"] == 1
        assert b["exit_calls"] == 0

    def test_record_usage_exit_routes_correctly(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        from trading import risk_manager
        risk_manager.record_usage(input_tokens=5_000, output_tokens=500, kind="exit")
        b = risk_manager.load_budget()
        assert b["exit_spent_usd"] > 0
        assert b["entry_spent_usd"] == 0.0
        assert b["exit_calls"] == 1

    def test_budget_exhausted_only_when_both_pools_gone(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        monkeypatch.setattr("telegram_notify.notify_budget_escalation", lambda *a: None)
        from trading import risk_manager
        # Exhaust entry pool
        risk_manager.record_usage(input_tokens=233_333, output_tokens=0, kind="entry")
        risk_manager.check_entry_budget(0.10)
        b = risk_manager.load_budget()
        assert b["budget_exhausted"] is False  # exit pool still active

    def test_record_usage_default_kind_is_entry(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        from trading import risk_manager
        risk_manager.record_usage(input_tokens=1_000, output_tokens=100)  # no kind
        b = risk_manager.load_budget()
        assert b["entry_calls"] == 1
        assert b["exit_calls"] == 0
```

- [ ] **Step 2: Run to verify failures**

```bash
pytest tests/test_trading.py::TestRiskManagerBudget -v 2>&1 | tail -10
```

Expected: all fail with `ModuleNotFoundError` or `ImportError`.

- [ ] **Step 3: Create `trading/risk_manager.py`**

```python
"""
trading/risk_manager.py — single authority for all risk decisions.

Absorbs api_budget.py. Owns: budget pool management (entry 70% / exit 30%),
auto-escalation to elevated ceiling, gap-up guard, thesis-break heuristic,
and post-fill bracket SL/TP correction.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import config
from utils.io_utils import load_json, save_json

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

# Sonnet 4.6 pricing per token (USD)
_INPUT_COST_PER_TOKEN       = 3.00    / 1_000_000
_CACHE_WRITE_COST_PER_TOKEN = 3.75    / 1_000_000
_CACHE_READ_COST_PER_TOKEN  = 0.30    / 1_000_000
_OUTPUT_COST_PER_TOKEN      = 15.00   / 1_000_000

_ELEVATED_REGIMES = {"CRISIS", "CAPITULATION", "CRASH_RECOVERY"}


def _today_et() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")


def _fresh_budget(date_str: str, ceiling: float) -> dict:
    entry_c = round(ceiling * config.ENTRY_BUDGET_RATIO, 4)
    exit_c  = round(ceiling * config.EXIT_BUDGET_RATIO,  4)
    return {
        "date":               date_str,
        "calls_made":         0,
        "entry_calls":        0,
        "exit_calls":         0,
        "input_tokens":       0,
        "output_tokens":      0,
        "cache_write_tokens": 0,
        "cache_read_tokens":  0,
        "estimated_cost_usd": 0.0,
        "entry_spent_usd":    0.0,
        "exit_spent_usd":     0.0,
        "ceiling_usd":        ceiling,
        "entry_ceiling_usd":  entry_c,
        "exit_ceiling_usd":   exit_c,
        "ceiling_mode":       "normal",
        "entry_exhausted":    False,
        "exit_exhausted":     False,
        "budget_exhausted":   False,
        "escalated":          False,
    }


def _save_budget(budget: dict) -> None:
    save_json(config.API_BUDGET_FILE, budget)


def load_budget() -> dict:
    """Load today's budget; resets if date changed or file is corrupt."""
    today = _today_et()
    try:
        data = load_json(config.API_BUDGET_FILE, {})
        if data.get("date") == today and "entry_spent_usd" in data:
            return data
    except Exception:
        pass
    budget = _fresh_budget(today, config.API_BUDGET_NORMAL_CEILING)
    _save_budget(budget)
    return budget


def get_ceiling(portfolio_gain_pct: float = 0.0, regime: str = "NORMAL") -> float:
    if portfolio_gain_pct >= 0.8 or regime in _ELEVATED_REGIMES:
        return config.API_BUDGET_ELEVATED_CEILING
    return config.API_BUDGET_NORMAL_CEILING


def check_entry_budget(
    estimated_cost: float,
    gain_pct: float = 0.0,
    regime: str = "NORMAL",
) -> bool:
    """Return True if the estimated entry call fits within the entry pool (70%)."""
    budget = load_budget()
    if budget.get("entry_exhausted"):
        logger.warning("Entry budget exhausted — blocking Claude entry call.")
        return False

    ceiling       = get_ceiling(gain_pct, regime)
    entry_ceiling = round(ceiling * config.ENTRY_BUDGET_RATIO, 4)
    current       = budget.get("entry_spent_usd", 0.0)

    if current + estimated_cost > entry_ceiling:
        logger.warning(
            "Entry budget ceiling reached: $%.3f + $%.3f > $%.2f — blocking.",
            current, estimated_cost, entry_ceiling,
        )
        budget["entry_exhausted"] = True
        if budget.get("exit_exhausted"):
            budget["budget_exhausted"] = True
        _save_budget(budget)
        return False

    warn = entry_ceiling * config.API_BUDGET_WARNING_PCT
    if current >= warn > (current - estimated_cost):
        logger.warning(
            "Entry budget at %.0f%% ($%.3f / $%.2f) — approaching limit.",
            current / entry_ceiling * 100, current, entry_ceiling,
        )
    return True


def check_exit_budget(
    estimated_cost: float,
    open_position_count: int = 0,
    regime: str = "NORMAL",
) -> bool:
    """Return True if the estimated exit call fits within the exit pool (30%).
    Auto-escalates to ELEVATED_CEILING (once) when exit pool exhausted with open positions.
    """
    budget      = load_budget()
    exit_ceiling = budget.get("exit_ceiling_usd",
                              round(config.API_BUDGET_NORMAL_CEILING * config.EXIT_BUDGET_RATIO, 4))
    current     = budget.get("exit_spent_usd", 0.0)

    if current + estimated_cost <= exit_ceiling:
        return True

    # Exit pool exhausted — try escalation if positions are open
    if open_position_count > 0 and not budget.get("escalated"):
        new_ceiling = config.API_BUDGET_ELEVATED_CEILING
        entry_c     = round(new_ceiling * config.ENTRY_BUDGET_RATIO, 4)
        exit_c      = round(new_ceiling * config.EXIT_BUDGET_RATIO,  4)
        budget.update({
            "ceiling_usd":      new_ceiling,
            "entry_ceiling_usd": entry_c,
            "exit_ceiling_usd": exit_c,
            "ceiling_mode":     "elevated",
            "escalated":        True,
        })
        _save_budget(budget)
        try:
            import telegram_notify
            total_spent = budget.get("entry_spent_usd", 0.0) + current
            telegram_notify.notify_budget_escalation(total_spent, new_ceiling)
        except Exception:
            pass
        logger.warning(
            "Exit budget exhausted with %d open position(s) — escalating ceiling to $%.2f",
            open_position_count, new_ceiling,
        )
        if current + estimated_cost <= exit_c:
            return True

    logger.warning(
        "Exit budget ceiling reached: $%.3f + $%.3f — blocking exit call.",
        current, estimated_cost,
    )
    budget["exit_exhausted"] = True
    if budget.get("entry_exhausted"):
        budget["budget_exhausted"] = True
    _save_budget(budget)
    return False


def record_usage(
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int = 0,
    cache_read_tokens: int  = 0,
    kind: str = "entry",
) -> None:
    """Accumulate token spend; route to entry or exit pool based on kind."""
    budget = load_budget()
    cost = (
        input_tokens       * _INPUT_COST_PER_TOKEN
        + output_tokens    * _OUTPUT_COST_PER_TOKEN
        + cache_write_tokens * _CACHE_WRITE_COST_PER_TOKEN
        + cache_read_tokens  * _CACHE_READ_COST_PER_TOKEN
    )
    budget["calls_made"]         += 1
    budget["input_tokens"]       += input_tokens
    budget["output_tokens"]      += output_tokens
    budget["cache_write_tokens"] += cache_write_tokens
    budget["cache_read_tokens"]  += cache_read_tokens
    budget["estimated_cost_usd"]  = round(budget["estimated_cost_usd"] + cost, 6)

    if kind == "exit":
        budget["exit_calls"]     = budget.get("exit_calls", 0) + 1
        budget["exit_spent_usd"] = round(budget.get("exit_spent_usd", 0.0) + cost, 6)
    else:
        budget["entry_calls"]     = budget.get("entry_calls", 0) + 1
        budget["entry_spent_usd"] = round(budget.get("entry_spent_usd", 0.0) + cost, 6)

    ceiling = get_ceiling()
    budget["ceiling_usd"]  = ceiling
    budget["ceiling_mode"] = "elevated" if ceiling > config.API_BUDGET_NORMAL_CEILING else "normal"
    _save_budget(budget)


# ── Gap guard ──────────────────────────────────────────────────────────────────

def is_gap_skip(current_price: float, prev_close: float) -> bool:
    """Return True when current_price has gapped up > GAP_SKIP_PCT above prev close."""
    if prev_close <= 0:
        return False
    return current_price > prev_close * (1 + config.GAP_SKIP_PCT / 100)


# ── Thesis-break heuristic ─────────────────────────────────────────────────────

def needs_exit_evaluation(iv, position: dict, research: dict) -> bool:
    """Pure heuristic — no I/O. Returns True when an open position warrants a Claude exit check."""
    try:
        entry_composite = position.get("entry_composite_score", 0)
        if iv.composite_score < entry_composite - config.EXIT_EVAL_COMPOSITE_DROP:
            return True

        sentiment = research.get(iv.symbol, {}).get("sentiment_score", 0)
        if isinstance(sentiment, (int, float)) and sentiment < config.EXIT_EVAL_SENTIMENT_FLOOR:
            return True

        entry_price = position.get("entry_price", iv.current_price)
        sl_pct      = position.get("stop_loss_pct", config.STOP_LOSS_PCT)
        pnl_pct     = (iv.current_price - entry_price) / entry_price * 100
        if pnl_pct < -(sl_pct - 1.0):
            return True
    except Exception:
        pass
    return False


# ── Fill price correction ──────────────────────────────────────────────────────

def correct_bracket_after_fill(
    trading_client,
    order_id: str,
    estimated_price: float,
    sl_pct: float,
    tp_pct: float,
) -> dict | None:
    """
    Poll Alpaca for actual fill price; replace child SL/TP legs if fill deviates
    more than FILL_SLIPPAGE_THRESHOLD from estimated_price.

    Returns corrected {stop_loss_price, take_profit_price, fill_price} or None.
    """
    from alpaca.trading.requests import ReplaceOrderRequest

    for _ in range(config.FILL_POLL_ATTEMPTS):
        try:
            order = trading_client.get_order_by_id(order_id)
            if str(order.status) == "filled":
                fill_price = float(order.filled_avg_price)
                slippage   = abs(fill_price - estimated_price) / max(estimated_price, 0.01)
                if slippage <= config.FILL_SLIPPAGE_THRESHOLD:
                    return None  # within tolerance — no correction needed
                new_sl = round(fill_price * (1 - sl_pct / 100), 2)
                new_tp = round(fill_price * (1 + tp_pct / 100), 2)
                for leg in (order.legs or []):
                    try:
                        leg_type = str(getattr(leg, "order_type", getattr(leg, "type", ""))).lower()
                        if "stop" in leg_type:
                            trading_client.replace_order_by_id(
                                str(leg.id), ReplaceOrderRequest(stop_price=new_sl)
                            )
                        elif "limit" in leg_type:
                            trading_client.replace_order_by_id(
                                str(leg.id), ReplaceOrderRequest(limit_price=new_tp)
                            )
                    except Exception as exc:
                        logger.error("Failed to replace bracket leg %s: %s", leg.id, exc)
                logger.info(
                    "Fill correction %s: est=%.2f fill=%.2f slip=%.2f%% → SL=%.2f TP=%.2f",
                    order_id, estimated_price, fill_price, slippage * 100, new_sl, new_tp,
                )
                return {"stop_loss_price": new_sl, "take_profit_price": new_tp, "fill_price": fill_price}
        except Exception as exc:
            logger.debug("Fill poll attempt failed for %s: %s", order_id, exc)
        time.sleep(config.FILL_POLL_INTERVAL_S)

    logger.warning("Fill poll timed out for order %s — keeping estimated bracket prices", order_id)
    return None
```

- [ ] **Step 4: Run budget tests to verify passing**

```bash
pytest tests/test_trading.py::TestRiskManagerBudget -v 2>&1 | tail -15
```

Expected: all 8 tests PASSED.

- [ ] **Step 5: Run full suite for regressions**

```bash
pytest tests/ -v -m unit -x -q 2>&1 | tail -5
```

Expected: all existing tests pass (api_budget still exists — no breakage yet).

- [ ] **Step 6: Commit**

```bash
git add trading/risk_manager.py tests/test_trading.py
git commit -m "feat: create trading/risk_manager.py — budget pool split, gap guard, thesis heuristic, fill correction"
```

---

## Task 5: Gap guard + thesis-break tests

**Files:**
- Test: `tests/test_trading.py`

- [ ] **Step 1: Add gap guard and thesis-break test classes to `tests/test_trading.py`**

```python
@pytest.mark.unit
class TestRiskManagerGapGuard:
    """is_gap_skip correctly identifies gap-up entries to block."""

    def test_blocks_when_gap_exceeds_threshold(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "GAP_SKIP_PCT", 3.0)
        from trading import risk_manager
        # 100 → 104 is a 4% gap — should block
        assert risk_manager.is_gap_skip(current_price=104.0, prev_close=100.0) is True

    def test_allows_when_gap_below_threshold(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "GAP_SKIP_PCT", 3.0)
        from trading import risk_manager
        # 100 → 102 is a 2% gap — should allow
        assert risk_manager.is_gap_skip(current_price=102.0, prev_close=100.0) is False

    def test_allows_when_prev_close_zero(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "GAP_SKIP_PCT", 3.0)
        from trading import risk_manager
        # Missing daily data — must not block
        assert risk_manager.is_gap_skip(current_price=150.0, prev_close=0.0) is False

    def test_allows_exactly_at_threshold(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "GAP_SKIP_PCT", 3.0)
        from trading import risk_manager
        # Exactly 3% gap — boundary is exclusive (> not >=)
        assert risk_manager.is_gap_skip(current_price=103.0, prev_close=100.0) is False


@pytest.mark.unit
class TestThesisBreakHeuristic:
    """needs_exit_evaluation correctly flags distressed positions."""

    def _iv(self, composite: float, price: float = 100.0, symbol: str = "AAPL"):
        import types
        return types.SimpleNamespace(
            symbol=symbol, composite_score=composite, current_price=price
        )

    def _position(self, entry_composite: float = 30.0, entry_price: float = 100.0,
                  sl_pct: float = 4.0):
        return {
            "entry_composite_score": entry_composite,
            "entry_price": entry_price,
            "stop_loss_pct": sl_pct,
        }

    def test_flags_on_composite_drop(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "EXIT_EVAL_COMPOSITE_DROP", 10)
        monkeypatch.setattr(config, "EXIT_EVAL_SENTIMENT_FLOOR", -30)
        monkeypatch.setattr(config, "STOP_LOSS_PCT", 4.0)
        from trading import risk_manager
        iv  = self._iv(composite=15.0)   # 30 - 15 = 15 drop ≥ 10
        pos = self._position(entry_composite=30.0)
        assert risk_manager.needs_exit_evaluation(iv, pos, {}) is True

    def test_flags_on_negative_sentiment(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "EXIT_EVAL_COMPOSITE_DROP", 10)
        monkeypatch.setattr(config, "EXIT_EVAL_SENTIMENT_FLOOR", -30)
        monkeypatch.setattr(config, "STOP_LOSS_PCT", 4.0)
        from trading import risk_manager
        iv  = self._iv(composite=28.0)   # only 2-pt drop — not threshold
        pos = self._position(entry_composite=30.0)
        research = {"AAPL": {"sentiment_score": -40}}
        assert risk_manager.needs_exit_evaluation(iv, pos, research) is True

    def test_flags_when_pnl_near_stop(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "EXIT_EVAL_COMPOSITE_DROP", 10)
        monkeypatch.setattr(config, "EXIT_EVAL_SENTIMENT_FLOOR", -30)
        monkeypatch.setattr(config, "STOP_LOSS_PCT", 4.0)
        from trading import risk_manager
        # Entry $100, current $96.50 → PnL -3.5% which is < -(4.0 - 1.0) = -3.0
        iv  = self._iv(composite=28.0, price=96.5)
        pos = self._position(entry_composite=30.0, entry_price=100.0, sl_pct=4.0)
        assert risk_manager.needs_exit_evaluation(iv, pos, {}) is True

    def test_passes_healthy_position(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "EXIT_EVAL_COMPOSITE_DROP", 10)
        monkeypatch.setattr(config, "EXIT_EVAL_SENTIMENT_FLOOR", -30)
        monkeypatch.setattr(config, "STOP_LOSS_PCT", 4.0)
        from trading import risk_manager
        iv  = self._iv(composite=28.0, price=102.0)  # 2-pt drop, up 2%, neutral sentiment
        pos = self._position(entry_composite=30.0, entry_price=100.0, sl_pct=4.0)
        assert risk_manager.needs_exit_evaluation(iv, pos, {}) is False
```

- [ ] **Step 2: Run to verify passing (logic already in risk_manager)**

```bash
pytest tests/test_trading.py::TestRiskManagerGapGuard tests/test_trading.py::TestThesisBreakHeuristic -v 2>&1 | tail -15
```

Expected: all 8 tests PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/test_trading.py
git commit -m "test: add gap guard and thesis-break heuristic unit tests"
```

---

## Task 6: Fill correction tests + `alpaca_client.get_order_by_id()`

**Files:**
- Modify: `alpaca_client.py` (end of class)
- Test: `tests/test_trading.py`

- [ ] **Step 1: Write failing fill correction tests in `tests/test_trading.py`**

```python
@pytest.mark.unit
class TestFillCorrection:
    """correct_bracket_after_fill updates SL/TP when fill deviates > 0.5%."""

    def _make_order(self, status: str, fill_price: float | None, legs: list):
        import types
        return types.SimpleNamespace(
            status=status,
            filled_avg_price=fill_price,
            legs=legs,
        )

    def _make_leg(self, leg_id: str, order_type: str):
        import types
        return types.SimpleNamespace(id=leg_id, order_type=order_type)

    def test_corrects_sl_tp_when_fill_exceeds_threshold(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "FILL_SLIPPAGE_THRESHOLD", 0.005)
        monkeypatch.setattr(config, "FILL_POLL_ATTEMPTS", 1)
        monkeypatch.setattr(config, "FILL_POLL_INTERVAL_S", 0.0)
        replaced = []
        from alpaca.trading.requests import ReplaceOrderRequest

        leg_stop  = self._make_leg("leg1", "stop")
        leg_limit = self._make_leg("leg2", "limit")
        order = self._make_order("filled", 102.0, [leg_stop, leg_limit])

        client = types.SimpleNamespace(
            get_order_by_id=lambda oid: order,
            replace_order_by_id=lambda lid, req: replaced.append((lid, req)),
        )
        import types
        from trading import risk_manager
        result = risk_manager.correct_bracket_after_fill(
            client, "ord1", estimated_price=100.0, sl_pct=4.0, tp_pct=5.0
        )
        assert result is not None
        assert abs(result["fill_price"] - 102.0) < 0.01
        assert abs(result["stop_loss_price"]  - round(102.0 * 0.96, 2)) < 0.01
        assert abs(result["take_profit_price"] - round(102.0 * 1.05, 2)) < 0.01
        assert len(replaced) == 2

    def test_skips_correction_when_within_threshold(self, monkeypatch):
        import config, types
        monkeypatch.setattr(config, "FILL_SLIPPAGE_THRESHOLD", 0.005)
        monkeypatch.setattr(config, "FILL_POLL_ATTEMPTS", 1)
        monkeypatch.setattr(config, "FILL_POLL_INTERVAL_S", 0.0)
        leg = self._make_leg("leg1", "stop")
        order = self._make_order("filled", 100.3, [leg])  # 0.3% slip < 0.5%
        client = types.SimpleNamespace(
            get_order_by_id=lambda oid: order,
            replace_order_by_id=lambda *a: None,
        )
        from trading import risk_manager
        result = risk_manager.correct_bracket_after_fill(
            client, "ord1", estimated_price=100.0, sl_pct=4.0, tp_pct=5.0
        )
        assert result is None

    def test_returns_none_on_poll_timeout(self, monkeypatch):
        import config, types
        monkeypatch.setattr(config, "FILL_SLIPPAGE_THRESHOLD", 0.005)
        monkeypatch.setattr(config, "FILL_POLL_ATTEMPTS", 2)
        monkeypatch.setattr(config, "FILL_POLL_INTERVAL_S", 0.0)
        order = self._make_order("pending_new", None, [])
        client = types.SimpleNamespace(
            get_order_by_id=lambda oid: order,
            replace_order_by_id=lambda *a: None,
        )
        from trading import risk_manager
        result = risk_manager.correct_bracket_after_fill(
            client, "ord1", estimated_price=100.0, sl_pct=4.0, tp_pct=5.0
        )
        assert result is None
```

- [ ] **Step 2: Run to verify they pass**

The tests mock `trading_client` as a `SimpleNamespace` — no real Alpaca calls are made. `correct_bracket_after_fill` is already implemented in Task 4.

```bash
pytest tests/test_trading.py::TestFillCorrection -v 2>&1 | tail -10
```

Expected: 3 tests PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/test_trading.py
git commit -m "test: add fill correction unit tests"
```

---

## Task 7: Gate 12 — gap-up guard in `signals/entry_gates.py`

**Files:**
- Modify: `signals/entry_gates.py` (after gate 11, before soft gates section)
- Test: `tests/test_signals.py`

- [ ] **Step 1: Write failing test in `tests/test_signals.py`**

Open `tests/test_signals.py`. Add at the end of the file:

```python
@pytest.mark.unit
class TestGapUpGate:
    """Gate 12: gap-up skip using iv.prev_close."""

    def _now(self):
        from zoneinfo import ZoneInfo
        return datetime(2024, 1, 15, 11, 0, 0, tzinfo=ZoneInfo("America/New_York"))

    @staticmethod
    def _iv(**overrides):
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
            prev_close=0.0,
        )
        base.update(overrides)
        return types.SimpleNamespace(**base)

    def _patch_calendars(self, monkeypatch):
        monkeypatch.setattr("earnings_calendar.is_earnings_imminent", lambda *a, **kw: False)
        monkeypatch.setattr("economic_calendar.is_event_risk_window", lambda now: (False, ""))

    def test_gate_blocks_gap_above_threshold(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "GAP_SKIP_PCT", 3.0)
        self._patch_calendars(monkeypatch)
        from signals.entry_gates import check_all_gates
        iv = self._iv(current_price=104.0, prev_close=100.0)  # 4% gap
        result = check_all_gates(
            symbol="AAPL", iv=iv, open_positions={}, daily_closed=set(),
            bars_15min=None, now=self._now(), entries_this_cycle=0,
        )
        assert result.passed is False
        assert "gap" in result.reason.lower()

    def test_gate_allows_gap_below_threshold(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "GAP_SKIP_PCT", 3.0)
        self._patch_calendars(monkeypatch)
        from signals.entry_gates import check_all_gates
        iv = self._iv(current_price=102.0, prev_close=100.0)  # 2% gap — allowed
        result = check_all_gates(
            symbol="AAPL", iv=iv, open_positions={}, daily_closed=set(),
            bars_15min=None, now=self._now(), entries_this_cycle=0,
        )
        # may fail on other gates but NOT on gap
        assert "gap" not in result.reason.lower()

    def test_gate_skips_when_prev_close_zero(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "GAP_SKIP_PCT", 3.0)
        self._patch_calendars(monkeypatch)
        from signals.entry_gates import check_all_gates
        iv = self._iv(current_price=150.0, prev_close=0.0)  # no daily data
        result = check_all_gates(
            symbol="AAPL", iv=iv, open_positions={}, daily_closed=set(),
            bars_15min=None, now=self._now(), entries_this_cycle=0,
        )
        assert "gap" not in result.reason.lower()
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_signals.py::TestGapUpGate -v 2>&1 | tail -10
```

Expected: `test_gate_blocks_gap_above_threshold` FAILS (gap not blocked yet).

- [ ] **Step 3: Add gate 12 to `signals/entry_gates.py`**

Open `signals/entry_gates.py`. Find gate 11 (Stochastic hard veto, around line 104):

```python
    # 11. Stochastic overbought hard veto
    if iv.stoch_d > config.STOCH_HARD_VETO_THRESHOLD:
        return GateResult(False, f"Stochastic hard veto (%D={iv.stoch_d:.1f} > {config.STOCH_HARD_VETO_THRESHOLD})")
```

Add gate 12 immediately after it:

```python
    # 12. Gap-up skip — prev_close sourced from IndicatorValues.prev_close (daily T-1)
    if iv.prev_close > 0 and iv.current_price > iv.prev_close * (1 + config.GAP_SKIP_PCT / 100):
        gap_pct = (iv.current_price / iv.prev_close - 1) * 100
        return GateResult(False, f"gap-up skip ({gap_pct:.1f}% > {config.GAP_SKIP_PCT}%)")
```

- [ ] **Step 4: Run tests to verify passing**

```bash
pytest tests/test_signals.py::TestGapUpGate -v 2>&1 | tail -10
```

Expected: 3 tests PASSED.

- [ ] **Step 5: Run full suite for regressions**

```bash
pytest tests/ -v -m unit -x -q 2>&1 | tail -5
```

- [ ] **Step 6: Commit**

```bash
git add signals/entry_gates.py tests/test_signals.py
git commit -m "feat: add gate 12 gap-up skip to entry_gates using iv.prev_close"
```

---

## Task 8: Delete `api_budget.py` + update all callers

**Files:**
- Delete: `api_budget.py`
- Modify: `watch_list_agent.py` (line 8)
- Modify: `research/analyzer.py` (line 7)
- Modify: `learning/agent.py` (line 12)
- Modify: `trading/claude_caller.py` (line 17, 127, 208)
- Modify: `tests/test_market.py` (all `api_budget` references)
- Modify: `tests/test_trading.py` (any `api_budget` patches)

- [ ] **Step 1: Update `watch_list_agent.py`**

Open `watch_list_agent.py`. Find `import api_budget` (line 8). Replace with:

```python
from trading import risk_manager
```

Find `api_budget.record_usage(` (line 113). Replace with:

```python
        risk_manager.record_usage(
```

- [ ] **Step 2: Update `research/analyzer.py`**

Open `research/analyzer.py`. Find `import api_budget` (line 7). Replace with:

```python
from trading import risk_manager
```

Find `api_budget.record_usage(` (line 191). Replace with:

```python
        risk_manager.record_usage(
```

- [ ] **Step 3: Update `learning/agent.py`**

Open `learning/agent.py`. Find `import api_budget` (line 12). Replace with:

```python
from trading import risk_manager
```

Find both `api_budget.record_usage(` occurrences (lines 131 and 273). Replace each with:

```python
    risk_manager.record_usage(
```

- [ ] **Step 4: Update `trading/claude_caller.py`**

Open `trading/claude_caller.py`.

Replace `import api_budget` (line 17) with:

```python
from trading import risk_manager
```

Replace `if not api_budget.check_budget(estimated_cost, gain_pct, regime):` (line 127) with:

```python
    if not risk_manager.check_entry_budget(estimated_cost, gain_pct, regime):
```

Replace the `api_budget.record_usage(` block (lines 208–214) with:

```python
        risk_manager.record_usage(
            input_tokens=getattr(usage, "input_tokens", 0),
            output_tokens=getattr(usage, "output_tokens", 0),
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0),
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0),
            kind="entry",
        )
```

- [ ] **Step 5: Update `tests/test_market.py`**

Open `tests/test_market.py`. Do a find-and-replace across the entire file:

- `import api_budget` → `from trading import risk_manager`
- `from api_budget import` → `from trading.risk_manager import`
- `api_budget.load_budget()` → `risk_manager.load_budget()`
- `api_budget.check_budget(` → `risk_manager.check_entry_budget(`
- `api_budget.record_usage(` → `risk_manager.record_usage(`
- `api_budget.get_ceiling` → `risk_manager.get_ceiling`

- [ ] **Step 6: Update `tests/test_trading.py` patches**

Search `tests/test_trading.py` for `patch("api_budget.record_usage"`. Replace each with `patch("trading.risk_manager.record_usage"`.

- [ ] **Step 7: Delete `api_budget.py`**

```bash
git rm api_budget.py
```

- [ ] **Step 8: Run full suite**

```bash
pytest tests/ -v -m unit -x -q 2>&1 | tail -10
```

Expected: all tests pass. No `api_budget` import errors.

- [ ] **Step 9: Commit**

```bash
git add watch_list_agent.py research/analyzer.py learning/agent.py \
        trading/claude_caller.py tests/test_market.py tests/test_trading.py
git commit -m "refactor: delete api_budget.py — all callers updated to trading.risk_manager"
```

---

## Task 9: Compact exit prompt + `_ask_claude_exit_batch()`

**Files:**
- Modify: `trading/context_builder.py` (add `build_exit_stock_block`)
- Modify: `trading/claude_caller.py` (add `_ask_claude_exit_batch`)

- [ ] **Step 1: Add `build_exit_stock_block()` to `trading/context_builder.py`**

Open `trading/context_builder.py`. Add at the end of the file:

```python
def build_exit_stock_block(
    symbol: str,
    iv: IndicatorValues,
    position: dict,
    research: dict,
) -> str:
    """Compact per-symbol block (~100 tokens) for thesis-break exit evaluation."""
    entry_price     = position.get("entry_price", iv.current_price)
    pnl_pct         = (iv.current_price - entry_price) / max(entry_price, 0.01) * 100
    composite_delta = iv.composite_score - position.get("entry_composite_score", 0)
    sentiment       = research.get(symbol, {}).get("sentiment_score", 0)
    sl_pct          = position.get("stop_loss_pct", config.STOP_LOSS_PCT)
    tp_pct          = position.get("take_profit_pct", config.TAKE_PROFIT_PCT)
    return (
        f"=== {symbol} (OPEN — THESIS CHECK) ===\n"
        f"Entry: ${entry_price:.2f} | Now: ${iv.current_price:.2f} | PnL: {pnl_pct:+.2f}%\n"
        f"SL: {sl_pct:.1f}% | TP: {tp_pct:.1f}% | RSI: {iv.rsi:.1f}\n"
        f"Composite: {iv.composite_score:+.1f} (Δ{composite_delta:+.1f} from entry)\n"
        f"Sentiment: {sentiment:+}\n"
    )
```

- [ ] **Step 2: Add `_ask_claude_exit_batch()` to `trading/claude_caller.py`**

Open `trading/claude_caller.py`. Update the import from `context_builder`:

```python
from trading.context_builder import (
    build_stable_system_context,
    build_dynamic_user_context,
    build_stock_block,
    build_exit_stock_block,
)
```

Add `_ask_claude_exit_batch` after `_ask_claude_batch`:

```python
def _ask_claude_exit_batch(
    batch: list[tuple[str, object, dict]],
    research: dict,
    ai: anthropic.Anthropic,
    open_position_count: int = 0,
) -> dict[str, dict]:
    """Ask Claude to evaluate open positions for thesis-break CLOSE decisions.
    Uses compact prompt and draws from the exit budget pool.
    """
    global _CLAUDE_DISABLED_REASON
    symbols = [sym for sym, _, _ in batch]

    with _CLAUDE_LOCK:
        if _CLAUDE_DISABLED.is_set():
            return {sym: _default_hold(sym) for sym in symbols}

    estimated_cost = _estimate_batch_call_cost(len(batch))
    if not risk_manager.check_exit_budget(estimated_cost, open_position_count):
        logger.warning("Exit budget blocked — skipping thesis-break eval for %s", symbols)
        return {sym: _default_hold(sym) for sym in symbols}

    now = datetime.now(ET)
    exit_blocks = "\n\n".join(
        build_exit_stock_block(sym, iv, pos, research)
        for sym, iv, pos in batch
    )

    system_text = (
        "You are an expert intraday trader reviewing open positions for thesis breaks.\n"
        "Evaluate whether each position should be CLOSED (thesis broken) or HELD.\n"
        "CLOSE only if composite score collapsed, sentiment turned very negative, "
        "or price is dangerously near stop-loss. Preserve capital but avoid premature exits."
    )
    user_msg = (
        f"Time: {now.strftime('%H:%M ET')}\n\n"
        f"{exit_blocks}\n\n"
        "Respond in JSON keyed by symbol:\n"
        '{\n  "<SYMBOL>": {"action": "<HOLD|CLOSE>", "confidence": <0-100>, '
        '"reasoning": "<1 sentence>", "stop_loss_pct": <number>, "take_profit_pct": <number>, '
        '"indicator_signals": {}}\n}'
    )

    try:
        response = ai.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=200 * len(batch),
            system=[{"type": "text", "text": system_text}],
            messages=[{"role": "user", "content": user_msg}],
        )
    except anthropic.APIStatusError as exc:
        if exc.status_code in (402, 429):
            with _CLAUDE_LOCK:
                _CLAUDE_DISABLED_REASON = f"HTTP {exc.status_code} — {exc.message[:120]}"
                _CLAUDE_DISABLED.set()
            logger.error("Claude circuit breaker tripped during exit eval: %s", _CLAUDE_DISABLED_REASON)
            telegram_notify.notify_claude_disabled(_CLAUDE_DISABLED_REASON)
        else:
            logger.error("Claude exit batch API error HTTP %d: %s", exc.status_code, exc.message[:200])
        return {sym: _default_hold(sym) for sym in symbols}
    except Exception as exc:
        logger.error("Claude exit batch unexpected error: %s", exc)
        return {sym: _default_hold(sym) for sym in symbols}

    try:
        usage = response.usage
        risk_manager.record_usage(
            input_tokens=getattr(usage, "input_tokens", 0),
            output_tokens=getattr(usage, "output_tokens", 0),
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0),
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0),
            kind="exit",
        )
    except Exception:
        pass

    text  = response.content[0].text.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    logger.warning("Claude exit batch JSON parse error for %s — defaulting to HOLD", symbols)
    return {sym: _default_hold(sym) for sym in symbols}
```

- [ ] **Step 3: Run full test suite**

```bash
pytest tests/ -v -m unit -x -q 2>&1 | tail -5
```

Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add trading/context_builder.py trading/claude_caller.py
git commit -m "feat: add build_exit_stock_block and _ask_claude_exit_batch for compact thesis-break eval"
```

---

## Task 10: Pipeline Phase 2.5 — thesis-break evaluation

**Files:**
- Modify: `trading/pipeline.py` (Phase 2.5 + Phase 4 exit branch)

- [ ] **Step 1: Add Phase 2.5 and wire exit decisions in Phase 4**

Open `trading/pipeline.py`. 

**Add import at top** (after the existing `from trading.claude_caller import _ask_claude_batch`):

```python
from trading.claude_caller import _ask_claude_batch, _ask_claude_exit_batch
from trading.risk_manager import needs_exit_evaluation
```

**Replace the Phase 2 gate-check block** (the `for symbol, iv, existing_pos, symbol_ret5d in stock_data:` loop that builds `needs_claude`) with the version below. The existing loop starts around line 138:

```python
        # ── Phase 2: Gate checks ──────────────────────────────────────────────
        t_gates = time.monotonic()
        needs_claude: list[tuple[str, IndicatorValues, dict | None]] = []
        exit_candidates: list[tuple[str, IndicatorValues, dict]]     = []

        for symbol, iv, existing_pos, symbol_ret5d in stock_data:
            if existing_pos:
                if needs_exit_evaluation(iv, existing_pos, research):
                    exit_candidates.append((symbol, iv, existing_pos))
                else:
                    pnl_pct = (iv.current_price - existing_pos["entry_price"]) / existing_pos["entry_price"] * 100
                    logger.info(
                        "BRACKET-MANAGED %s: P&L=%+.2f%%, composite=%+.1f — thesis intact",
                        symbol, pnl_pct, iv.composite_score,
                    )
            elif iv.composite_score > config.COMPOSITE_SCORE_THRESHOLD:
                needs_claude.append((symbol, iv, None))
            else:
                logger.info(
                    "SKIP-CLAUDE %s: composite=%+.1f — below entry threshold",
                    symbol, iv.composite_score,
                )

        gates_elapsed = time.monotonic() - t_gates

        # ── Phase 2.5: Thesis-break exit evaluation ───────────────────────────
        exit_decisions: dict[str, dict] = {}
        if exit_candidates:
            logger.info(
                "THESIS-CHECK: %d open position(s) flagged for Claude exit eval: %s",
                len(exit_candidates), [s for s, _, _ in exit_candidates],
            )
            exit_decisions = _ask_claude_exit_batch(
                exit_candidates, research, ai,
                open_position_count=len(open_positions),
            )
```

**Replace Phase 4 execute loop** (the `for symbol, iv, existing_pos, symbol_ret5d in stock_data:` loop in Phase 4) with:

```python
        # ── Phase 4: Execute ──────────────────────────────────────────────────
        t_exec = time.monotonic()
        entries_this_cycle = 0

        for symbol, iv, existing_pos, symbol_ret5d in stock_data:
            try:
                if existing_pos:
                    exit_dec = exit_decisions.get(symbol)
                    if exit_dec and exit_dec.get("action", "HOLD").upper() == "CLOSE":
                        close_position(
                            symbol, alpaca, open_positions, iv.current_price, iv,
                            "claude_exit_decision",
                        )
                        result.closes += 1
                        logger.info(
                            "THESIS-CLOSE %s: %s", symbol, exit_dec.get("reasoning", "")[:120]
                        )
                    continue

                decision = decisions.get(symbol)
                if decision is None:
                    continue

                action = decision.get("action", "HOLD").upper()

                if action == "BUY" and not existing_pos and buying_power >= config.MIN_BUYING_POWER and can_open:
                    pos_data = execute_buy(
                        symbol=symbol, iv=iv, decision=decision,
                        regime=regime, regime_info=regime_info,
                        research=research, open_positions=open_positions,
                        alpaca=alpaca, portfolio_value=portfolio_value,
                        pos_mult=pos_mult, spy_ret5d=spy_ret5d,
                        symbol_ret5d=symbol_ret5d, rsi_max=rsi_max,
                        params=params, now=now,
                        entries_this_cycle=entries_this_cycle,
                    )
                    if pos_data is not None:
                        entries_this_cycle += 1
                        result.entries += 1
                        buying_power -= iv.current_price * pos_data["qty"]

                elif action == "CLOSE" and existing_pos:
                    close_position(symbol, alpaca, open_positions, iv.current_price, iv, "claude_decision")
                    result.closes += 1

                elif action == "HOLD":
                    logger.info(
                        "HOLD %s: composite=%+.1f, RSI=%.1f | %s",
                        symbol, iv.composite_score, iv.rsi, decision.get("reasoning", "")[:120],
                    )

            except Exception as exc:
                logger.error("Error processing %s: %s", symbol, exc, exc_info=True)
```

- [ ] **Step 2: Run full test suite**

```bash
pytest tests/ -v -m unit -x -q 2>&1 | tail -10
```

Expected: all tests pass.

- [ ] **Step 3: Commit**

```bash
git add trading/pipeline.py
git commit -m "feat: add pipeline Phase 2.5 thesis-break exit evaluation with exit budget pool"
```

---

## Task 11: Wire fill correction in `trading/executor.py`

**Files:**
- Modify: `trading/executor.py` (after `place_bracket_order` call, around line 136)

- [ ] **Step 1: Add fill correction import and call**

Open `trading/executor.py`. Find the existing imports block at the top. Add:

```python
from trading import risk_manager
```

Find the `order = alpaca.place_bracket_order(...)` call (around line 136). Replace the block from that line through `position_data = {` with:

```python
    order = alpaca.place_bracket_order(symbol=symbol, qty=qty, entry_price=iv.current_price, stop_loss_pct=sl_pct, take_profit_pct=tp_pct)

    # Correct bracket SL/TP to actual fill price if slippage exceeded threshold
    # Pass alpaca._trading (raw TradingClient) — has both get_order_by_id and replace_order_by_id natively
    corrected = risk_manager.correct_bracket_after_fill(
        alpaca._trading, order["id"],
        estimated_price=iv.current_price,
        sl_pct=sl_pct,
        tp_pct=tp_pct,
    )
    if corrected:
        order["stop_loss_price"]  = corrected["stop_loss_price"]
        order["take_profit_price"] = corrected["take_profit_price"]

    position_data = {
        "entry_price": iv.current_price, "qty": qty,
        "stop_loss_pct": sl_pct, "take_profit_pct": tp_pct,
        "stop_loss_price": order["stop_loss_price"], "take_profit_price": order["take_profit_price"],
        "fill_price": corrected["fill_price"] if corrected else iv.current_price,
        "entry_time": now.isoformat(), "entry_composite_score": iv.composite_score,
        "entry_indicators": asdict(iv), "entry_sentiment_score": research.get(symbol, {}).get("sentiment_score"),
        "claude_reasoning": decision.get("reasoning", ""), "indicator_signals": decision.get("indicator_signals", {}),
        "order_id": order.get("id"), "mode_at_entry": mode, "regime_at_entry": regime,
    }
```

- [ ] **Step 2: Run full test suite**

```bash
pytest tests/ -v -m unit -x -q 2>&1 | tail -10
```

Expected: all tests pass.

- [ ] **Step 3: Commit**

```bash
git add trading/executor.py
git commit -m "feat: wire fill-price correction in executor after bracket order placement"
```

---

## Task 12: Final verification

- [ ] **Step 1: Run complete test suite**

```bash
pytest tests/ -v -m unit 2>&1 | tail -20
```

Expected: all tests pass. Zero failures. Note the count — it should be higher than before this plan.

- [ ] **Step 2: Verify `api_budget.py` is gone**

```bash
ls api_budget.py 2>&1
```

Expected: `No such file or directory`

- [ ] **Step 3: Verify no remaining `api_budget` imports**

```bash
grep -r "import api_budget\|from api_budget" . --include="*.py" | grep -v __pycache__
```

Expected: no output.

- [ ] **Step 4: Verify risk_manager exports all expected symbols**

```bash
.venv/bin/python3 -c "
from trading.risk_manager import (
    load_budget, get_ceiling, check_entry_budget, check_exit_budget,
    record_usage, is_gap_skip, needs_exit_evaluation, correct_bracket_after_fill
)
print('All risk_manager symbols importable OK')
"
```

Expected: `All risk_manager symbols importable OK`

- [ ] **Step 5: Final commit**

```bash
git add -A
git commit -m "feat: complete risk-manager implementation — budget split, gap guard, fill correction, thesis-break eval"
```
