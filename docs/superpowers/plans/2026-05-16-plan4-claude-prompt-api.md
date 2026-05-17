# Plan 4: Claude Prompt Engineering & API Efficiency

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add system prompt + cache to exit-batch calls; enforce output bounds on Claude decisions; fix watch-list cache fragmentation; cap zero-trade-day learning to 1 param change; derive auto-execute confidence dynamically.

**Architecture:** All Claude calls (entry, exit, watch-list, research, learning) share a 2-block stable system structure: `[role+rules (cached)] [strategy_memory (cached)]`. Per-request user content is never cached. Output validation runs after JSON parse on every Claude response, before fields are used. Learning agent uses deterministic single-param relaxation on zero-trade days instead of a Claude call.

**Tech Stack:** Python 3.11, Anthropic SDK (claude-sonnet-4-6), pytest

---

## File Map

| File | Change |
|---|---|
| `trading/claude_caller.py` | Add system + cache to `_ask_claude_exit_batch`; add `_validate_decision()` |
| `agents/watch_list.py` | Always include strategy_memory block (even if empty); uniform system structure |
| `learning/agent.py` | Zero-trade-day: deterministic 1-param relax, skip Claude call |
| `trading/pipeline.py` | Auto-execute confidence = f(composite, sentiment) not hardcoded 90 |
| `config.py` | Add `AUTO_EXECUTE_CONFIDENCE_BASE`, `LEARNING_MAX_PARAMS_PER_DAY` |
| `tests/test_trading_pipeline.py` | Test decision bounds validation |
| `tests/test_agents.py` | Test learning agent zero-trade path |

---

### Task 1: Decision output bounds validation

Claude can return `confidence: 105`, `stop_loss_pct: -1.5`. These pass silently into bracket orders.

**Files:**
- Modify: `trading/claude_caller.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_trading_pipeline.py — add
import pytest
from trading.claude_caller import _validate_decision

@pytest.mark.unit
def test_validate_decision_clamps_confidence():
    raw = {"action": "BUY", "confidence": 150, "stop_loss_pct": 4.0, "take_profit_pct": 5.0, "reasoning": "x"}
    result = _validate_decision(raw, "AAPL")
    assert result["confidence"] == 100

@pytest.mark.unit
def test_validate_decision_clamps_stop_loss_pct():
    raw = {"action": "BUY", "confidence": 75, "stop_loss_pct": -1.5, "take_profit_pct": 5.0, "reasoning": "x"}
    result = _validate_decision(raw, "AAPL")
    assert result["stop_loss_pct"] >= 0.5

@pytest.mark.unit
def test_validate_decision_clamps_take_profit_pct():
    raw = {"action": "BUY", "confidence": 75, "stop_loss_pct": 4.0, "take_profit_pct": 50.0, "reasoning": "x"}
    result = _validate_decision(raw, "AAPL")
    assert result["take_profit_pct"] <= 15.0

@pytest.mark.unit
def test_validate_decision_passthrough_valid():
    raw = {"action": "BUY", "confidence": 75, "stop_loss_pct": 4.0, "take_profit_pct": 5.0, "reasoning": "x"}
    result = _validate_decision(raw, "AAPL")
    assert result == raw
```

- [ ] **Step 2: Run to confirm failure**

```
.venv/bin/python -m pytest tests/test_trading_pipeline.py -k "validate_decision" -v
```

- [ ] **Step 3: Add _validate_decision() to claude_caller.py**

```python
def _validate_decision(decision: dict, symbol: str) -> dict:
    """Clamp Claude output to safe ranges. Logs a warning if any value was adjusted."""
    original = dict(decision)
    decision["confidence"] = max(0, min(100, int(decision.get("confidence", 0))))
    decision["stop_loss_pct"] = max(0.5, min(10.0, float(decision.get("stop_loss_pct", 4.0))))
    decision["take_profit_pct"] = max(1.0, min(15.0, float(decision.get("take_profit_pct", 5.0))))
    if decision != original:
        import logging
        logging.getLogger(__name__).warning(
            "%s: Claude output clamped — original=%s adjusted=%s",
            symbol,
            {k: original.get(k) for k in ("confidence", "stop_loss_pct", "take_profit_pct")},
            {k: decision.get(k) for k in ("confidence", "stop_loss_pct", "take_profit_pct")},
        )
    return decision
```

Call `_validate_decision(decision, symbol)` in `_ask_claude_batch()` after parsing each symbol's JSON response, before adding to the decisions dict.

- [ ] **Step 4: Run tests**

```
.venv/bin/python -m pytest tests/test_trading_pipeline.py -k "validate_decision" -v
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 5: Commit**

```bash
git add trading/claude_caller.py tests/test_trading_pipeline.py
git commit -m "fix: validate/clamp Claude decision outputs (confidence 0-100, SL 0.5-10%, TP 1-15%)"
```

---

### Task 2: Exit-batch — add system prompt + cache

`_ask_claude_exit_batch()` currently sends no system prompt. Every thesis-break evaluation pays full cold-cache cost.

**Files:**
- Modify: `trading/claude_caller.py`

- [ ] **Step 1: Write test**

```python
# tests/test_trading_pipeline.py — add
@pytest.mark.unit
def test_exit_batch_includes_system_prompt(monkeypatch):
    """_ask_claude_exit_batch must send a system block with cache_control."""
    from trading.claude_caller import _ask_claude_exit_batch
    from trading.cycle_context import CycleSnapshot
    from unittest.mock import MagicMock, patch

    calls = []
    def fake_create(**kwargs):
        calls.append(kwargs)
        r = MagicMock()
        r.content = [MagicMock(text='{"AAPL": {"action": "HOLD", "reasoning": "ok"}}')]
        r.usage = MagicMock(input_tokens=100, output_tokens=20, cache_creation_input_tokens=0, cache_read_input_tokens=0)
        return r

    snap = MagicMock(spec=CycleSnapshot)
    snap.regime = "NORMAL"
    snap.regime_info = {"vix_current": 15}
    snap.strategy_memory = ""
    snap.research = {}

    ai_mock = MagicMock()
    ai_mock.messages.create.side_effect = fake_create

    from indicators.values import IndicatorValues
    iv = MagicMock(spec=IndicatorValues)
    iv.composite_score = 20.0
    iv.rsi = 55.0
    iv.current_price = 100.0

    _ask_claude_exit_batch([("AAPL", iv, {"entry_price": 102.0})], snap, ai_mock)

    assert calls, "Claude was not called"
    call_kwargs = calls[0]
    assert "system" in call_kwargs, "No system block in exit-batch call"
    system_blocks = call_kwargs["system"]
    assert any(b.get("cache_control") for b in system_blocks if isinstance(b, dict)), \
        "System blocks must have cache_control for cache hits"
```

- [ ] **Step 2: Run to confirm failure**

```
.venv/bin/python -m pytest tests/test_trading_pipeline.py -k "exit_batch_includes_system" -v
```

- [ ] **Step 3: Update _ask_claude_exit_batch() in claude_caller.py**

Find the function and add a system prompt with cache:

```python
def _ask_claude_exit_batch(
    candidates: list[tuple[str, IndicatorValues, dict]],
    snap: CycleSnapshot,
    ai: anthropic.Anthropic,
) -> dict[str, dict]:
    if not candidates:
        return {}

    system_text = (
        "You are an expert intraday trader evaluating thesis-break candidates.\n"
        "CLOSE only if: (1) composite score dropped ≥10pts from entry AND thesis broken, "
        "(2) intraday news shock ≥60, (3) position near stop-loss, OR (4) clear regime shift to CRISIS.\n"
        "Default to HOLD — premature exits destroy returns. Capital preservation matters, but so does "
        "letting winners run.\n"
        f"Current regime: {snap.regime} | VIX: {snap.regime_info.get('vix_current', 'N/A')}\n"
        "Respond in JSON: {\"<symbol>\": {\"action\": \"HOLD\"|\"CLOSE\", \"reasoning\": \"<brief>\"}}"
    )
    mem = snap.strategy_memory or "(no strategy memory yet)"

    system_blocks = [
        {"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": f"Strategy memory:\n{mem}", "cache_control": {"type": "ephemeral"}},
    ]

    # Build user message with all candidates
    parts = []
    for symbol, iv, pos in candidates:
        entry = pos.get("entry_price", 0)
        pnl_pct = (iv.current_price - entry) / entry * 100 if entry else 0
        parts.append(
            f"{symbol}: composite={iv.composite_score:+.1f} RSI={iv.rsi:.1f} "
            f"price=${iv.current_price:.2f} entry=${entry:.2f} PnL={pnl_pct:+.2f}%"
        )
    user_text = "Evaluate thesis-break for:\n" + "\n".join(parts)

    try:
        resp = ai.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=512,
            system=system_blocks,
            messages=[{"role": "user", "content": user_text}],
        )
        raw = resp.content[0].text.strip()
        return json.loads(raw)
    except Exception as exc:
        logger.warning("exit batch failed: %s", exc)
        return {}
```

- [ ] **Step 4: Run tests**

```
.venv/bin/python -m pytest tests/test_trading_pipeline.py -k "exit_batch" -v
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 5: Commit**

```bash
git add trading/claude_caller.py tests/test_trading_pipeline.py
git commit -m "feat: exit-batch Claude call gets system prompt + cache_control (was cold-cache every call)"
```

---

### Task 3: Watch-list — uniform cache structure

**Files:**
- Modify: `agents/watch_list.py`

- [ ] **Step 1: Write test**

```python
# tests/test_agents.py — add or update watch_list test
@pytest.mark.unit
def test_watch_list_always_sends_two_system_blocks(monkeypatch):
    """Watch-list agent sends exactly 2 system blocks (role + memory) even with empty strategy_memory."""
    from agents.watch_list import _build_watch_list_system
    blocks = _build_watch_list_system(strategy_memory="")
    assert len(blocks) == 2, f"Expected 2 system blocks, got {len(blocks)}"
    assert all(b.get("cache_control") for b in blocks), "All blocks must have cache_control"

@pytest.mark.unit
def test_watch_list_system_blocks_with_memory(monkeypatch):
    """System blocks include memory content when present."""
    from agents.watch_list import _build_watch_list_system
    blocks = _build_watch_list_system(strategy_memory="RSI max = 72")
    mem_block = blocks[1]["text"]
    assert "RSI max = 72" in mem_block
```

- [ ] **Step 2: Extract _build_watch_list_system() in watch_list.py**

```python
def _build_watch_list_system(strategy_memory: str) -> list[dict]:
    """Build the stable 2-block system prompt for consistent cache hits across calls."""
    role_block = {
        "type": "text",
        "text": (
            "You are an expert intraday US-equity trader. Select 10-15 stocks from the candidates "
            "that have the strongest technical + sentiment setup for today's trading session.\n"
            "Return a JSON array of ticker symbols only: [\"AAPL\", \"NVDA\", ...].\n"
            "Exclude: earnings within 2 days, parabolic moves (>15% in 5d), or CRISIS regime stocks."
        ),
        "cache_control": {"type": "ephemeral"},
    }
    mem_text = strategy_memory if strategy_memory else "(no strategy memory yet)"
    mem_block = {
        "type": "text",
        "text": f"Strategy memory (learned trading rules):\n{mem_text}",
        "cache_control": {"type": "ephemeral"},
    }
    return [role_block, mem_block]
```

Update the existing watch-list agent call to use `_build_watch_list_system(snap.strategy_memory)`.

- [ ] **Step 3: Run tests**

```
.venv/bin/python -m pytest tests/test_agents.py -k "watch_list_always_sends or watch_list_system_blocks" -v
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 4: Commit**

```bash
git add agents/watch_list.py tests/test_agents.py
git commit -m "fix: watch-list always sends 2 stable system blocks — improves cache hit rate"
```

---

### Task 4: Learning agent — zero-trade-day deterministic relaxation

On zero-trade days, calling Claude wastes $0.50-$1.00 to "review rules". Replace with deterministic single-param relaxation logic.

**Files:**
- Modify: `learning/agent.py`
- Modify: `config.py`

- [ ] **Step 1: Add config constant**

```python
# config.py
LEARNING_MAX_PARAMS_PER_DAY = 1    # max param changes on zero-trade days (prevents spiral)
LEARNING_ZERO_TRADE_RSI_STEP = 2   # relax RSI_BUY_MAX by this many points per zero-trade day
LEARNING_ZERO_TRADE_COMPOSITE_STEP = 2  # relax COMPOSITE_MIN_NORMAL by this many points
```

- [ ] **Step 2: Write failing tests**

```python
# tests/test_agents.py — add
import pytest, config
from unittest.mock import patch, MagicMock

@pytest.mark.unit
def test_zero_trade_day_skips_claude_call(monkeypatch, tmp_path):
    """On zero-trade days, learning agent must NOT call Claude — uses deterministic relax."""
    monkeypatch.setattr(config, "TRADING_PARAMS_FILE", tmp_path / "params.json")
    from utils.io_utils import save_json
    save_json(config.TRADING_PARAMS_FILE, {
        "rsi_buy_max": 72, "composite_min_normal": 20,
        "entry_start_hour": 10, "entry_end_hour": 15,
        "high_vol_position_mult": 0.5, "rsi_buy_max_high_vol": 65,
        "vwap_slope_min": -0.3,
    })

    ai_mock = MagicMock()

    from learning.agent import _handle_zero_trade_day
    _handle_zero_trade_day(params_path=config.TRADING_PARAMS_FILE, ai=ai_mock)

    ai_mock.messages.create.assert_not_called()

@pytest.mark.unit
def test_zero_trade_day_relaxes_exactly_one_param(monkeypatch, tmp_path):
    """Zero-trade relax changes at most 1 param per day."""
    monkeypatch.setattr(config, "TRADING_PARAMS_FILE", tmp_path / "params.json")
    monkeypatch.setattr(config, "LEARNING_MAX_PARAMS_PER_DAY", 1)
    monkeypatch.setattr(config, "LEARNING_ZERO_TRADE_RSI_STEP", 2)
    from utils.io_utils import save_json, load_json
    original = {
        "rsi_buy_max": 70, "composite_min_normal": 22,
        "entry_start_hour": 10, "entry_end_hour": 15,
        "high_vol_position_mult": 0.5, "rsi_buy_max_high_vol": 65,
        "vwap_slope_min": -0.3,
    }
    save_json(config.TRADING_PARAMS_FILE, original)

    from learning.agent import _handle_zero_trade_day
    _handle_zero_trade_day(params_path=config.TRADING_PARAMS_FILE, ai=MagicMock())

    updated = load_json(config.TRADING_PARAMS_FILE, {})
    changed = [k for k in original if updated.get(k) != original[k]]
    assert len(changed) <= config.LEARNING_MAX_PARAMS_PER_DAY, \
        f"Too many params changed: {changed}"
    assert updated["rsi_buy_max"] <= 80, "RSI max must not exceed 80 (clamped)"
```

- [ ] **Step 3: Add _handle_zero_trade_day() to learning/agent.py**

```python
# Bounds for zero-trade-day relaxation
_ZERO_TRADE_BOUNDS = {
    "rsi_buy_max":           (65, 80),
    "composite_min_normal":  (15, 30),
    "vwap_slope_min":        (-0.6, -0.1),
}

def _handle_zero_trade_day(params_path, ai) -> None:
    """Deterministic single-param relaxation on zero-trade days. No Claude call.
    Rotates through params to avoid repeatedly relaxing the same one.
    """
    from utils.io_utils import load_json, save_json
    params = load_json(params_path, {})

    # Determine which param to relax today (rotate by weekday)
    from datetime import datetime
    from utils.constants import ET
    weekday = datetime.now(ET).weekday()  # 0=Mon, 4=Fri

    param_order = ["rsi_buy_max", "composite_min_normal", "vwap_slope_min"]
    target = param_order[weekday % len(param_order)]

    low, high = _ZERO_TRADE_BOUNDS.get(target, (None, None))
    if low is None:
        return

    current = float(params.get(target, high))
    if target == "vwap_slope_min":
        new_val = max(low, current - 0.05)  # more permissive = more negative
    else:
        new_val = min(high, current + config.LEARNING_ZERO_TRADE_RSI_STEP)

    if new_val != current:
        params[target] = round(new_val, 2)
        save_json(params_path, params)
        logger.info("Zero-trade-day: relaxed %s %s → %s (bounds: %s-%s)",
                    target, current, new_val, low, high)
    else:
        logger.info("Zero-trade-day: %s already at ceiling %s — no change", target, current)
```

In `run_learning_agent()`, replace the zero-trade Claude call with `_handle_zero_trade_day(config.TRADING_PARAMS_FILE, ai)`.

- [ ] **Step 4: Run tests**

```
.venv/bin/python -m pytest tests/test_agents.py -k "zero_trade_day" -v
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 5: Commit**

```bash
git add learning/agent.py config.py tests/test_agents.py
git commit -m "fix: zero-trade-day learning uses deterministic 1-param relax — no Claude call; prevents spiral"
```

---

### Task 5: Auto-execute confidence — dynamic derivation

Replace the hardcoded `"confidence": 90` with a function of composite score and intraday sentiment.

**Files:**
- Modify: `trading/pipeline.py`
- Modify: `config.py`

- [ ] **Step 1: Add config constant**

```python
# config.py
AUTO_EXECUTE_CONFIDENCE_BASE = 75   # base confidence for auto-execute decisions
```

- [ ] **Step 2: Write test**

```python
# tests/test_trading_pipeline.py — add
@pytest.mark.unit
def test_auto_execute_confidence_scales_with_composite(monkeypatch):
    """Auto-execute confidence must scale with composite score, not be hardcoded 90."""
    from trading.pipeline import _compute_auto_execute_confidence
    monkeypatch.setattr(config, "AUTO_EXECUTE_CONFIDENCE_BASE", 75)
    monkeypatch.setattr(config, "AUTO_EXECUTE_COMPOSITE_MIN", 55)

    # composite = 60, intra_sentiment = 30 → above min → higher than base
    conf = _compute_auto_execute_confidence(composite_score=60.0, intra_sentiment=30)
    assert 75 <= conf <= 100

    # composite = 55 (at floor) → should be close to base
    conf_low = _compute_auto_execute_confidence(composite_score=55.0, intra_sentiment=20)
    assert conf_low >= config.AUTO_EXECUTE_CONFIDENCE_BASE
    assert conf_low <= conf
```

- [ ] **Step 3: Add _compute_auto_execute_confidence() to pipeline.py**

```python
def _compute_auto_execute_confidence(composite_score: float, intra_sentiment: int) -> int:
    """Derive auto-execute confidence from signal strength. Clamped to [BASE, 95]."""
    base = config.AUTO_EXECUTE_CONFIDENCE_BASE
    composite_bonus = min(15, int((composite_score - config.AUTO_EXECUTE_COMPOSITE_MIN) * 0.5))
    sentiment_bonus = min(5, max(0, int(intra_sentiment / 10)))
    return min(95, base + composite_bonus + sentiment_bonus)
```

Update Phase 4a in pipeline.py to use it:

```python
confidence = _compute_auto_execute_confidence(iv.composite_score, intra_sent)
pos_data = execute_buy(
    ...,
    decision={"action": "BUY", "confidence": confidence, ...},
    ...
)
```

- [ ] **Step 4: Run tests**

```
.venv/bin/python -m pytest tests/test_trading_pipeline.py -k "auto_execute_confidence" -v
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 5: Commit**

```bash
git add trading/pipeline.py config.py tests/test_trading_pipeline.py
git commit -m "fix: auto-execute confidence derived from composite+sentiment (was hardcoded 90)"
```

---

### Task 6: Full regression

- [ ] **Step 1: Run full suite**

```
.venv/bin/python -m pytest tests/ -m unit -q --tb=short
```
Expected: all green.

- [ ] **Step 2: Verify no hardcoded 90 confidence in auto-execute**

```bash
grep -n '"confidence": 90' trading/pipeline.py
```
Expected: no matches.

- [ ] **Step 3: Verify exit-batch has system blocks**

```bash
grep -n "cache_control" trading/claude_caller.py | head -20
```
Expected: cache_control appears in both entry and exit batch functions.

- [ ] **Step 4: Tag**

```bash
git tag plan4-claude-prompt-complete
```
