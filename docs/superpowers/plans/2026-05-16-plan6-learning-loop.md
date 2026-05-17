# Plan 6: Learning Loop Hardening

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent threshold drift on multi-day zero-trade streaks; make learning use rolling 5-day window instead of single-day data; clamp indicator weight drift; version strategy_memory with daily backups; make backtest load learned params.

**Architecture:** All learning changes are in `learning/` package. Strategy-memory backup uses date-stamped `.bak` files (max 7, auto-pruned). Rolling trade window uses a new `_load_rolling_trades(days=5)` helper in `learning/data.py`. Weight clamping is expressed as ratio bounds vs. the default weight vector. Backtest loads `trading_params.json` before the simulation run.

**Tech Stack:** Python 3.11, pathlib, pytest

---

## File Map

| File | Change |
|---|---|
| `learning/data.py` | Add `_load_rolling_trades(days=5)` replacing single-day load |
| `learning/trade_analysis.py` | Use rolling trades; add weight-clamping with log on override |
| `learning/agent.py` | Version strategy_memory on every write; use rolling window |
| `backtest.py` | Load `trading_params.json` at start; apply learned RSI/composite thresholds |
| `config.py` | Add `LEARNING_ROLLING_DAYS`, `INDICATOR_WEIGHT_MAX_MULT`, `STRATEGY_MEMORY_BACKUP_MAX` |
| `tests/test_agents.py` | Tests for rolling window, weight clamping, memory versioning |

---

### Task 1: Config constants for learning controls

**Files:**
- Modify: `config.py`

- [ ] **Step 1: Add constants**

```python
# config.py — add near learning section
LEARNING_ROLLING_DAYS        = 5    # days of trade history to analyze (prevents single-day overfit)
INDICATOR_WEIGHT_MAX_MULT    = 2.0  # max multiplier vs. default weight (prevents one indicator dominating)
INDICATOR_WEIGHT_MIN_MULT    = 0.3  # min multiplier vs. default weight
STRATEGY_MEMORY_BACKUP_MAX   = 7    # keep this many daily .bak files before pruning
```

- [ ] **Step 2: Write test**

```python
# tests/test_agents.py — add
import pytest, config

@pytest.mark.unit
def test_learning_config_constants_exist():
    assert hasattr(config, "LEARNING_ROLLING_DAYS") and config.LEARNING_ROLLING_DAYS >= 3
    assert hasattr(config, "INDICATOR_WEIGHT_MAX_MULT") and config.INDICATOR_WEIGHT_MAX_MULT > 1.0
    assert hasattr(config, "INDICATOR_WEIGHT_MIN_MULT") and 0 < config.INDICATOR_WEIGHT_MIN_MULT < 1.0
    assert hasattr(config, "STRATEGY_MEMORY_BACKUP_MAX") and config.STRATEGY_MEMORY_BACKUP_MAX >= 3
```

- [ ] **Step 3: Run test**

```
.venv/bin/python -m pytest tests/test_agents.py -k "learning_config_constants" -v
```

- [ ] **Step 4: Commit**

```bash
git add config.py tests/test_agents.py
git commit -m "feat: add learning loop config constants (rolling days, weight bounds, memory backup count)"
```

---

### Task 2: Rolling trade window in learning/data.py

**Files:**
- Modify: `learning/data.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_agents.py — add
import pytest, config
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

@pytest.mark.unit
def test_load_rolling_trades_returns_last_n_days(monkeypatch, tmp_path):
    """_load_rolling_trades returns only trades from the last LEARNING_ROLLING_DAYS days."""
    from utils.io_utils import save_json
    monkeypatch.setattr(config, "TRADE_JOURNAL_FILE", tmp_path / "journal.json")
    monkeypatch.setattr(config, "LEARNING_ROLLING_DAYS", 3)

    now = datetime.now(ET)
    trades = [
        {"type": "CLOSE", "symbol": "AAPL", "timestamp": (now - timedelta(days=1)).isoformat(), "pnl_pct": 1.5},
        {"type": "CLOSE", "symbol": "MSFT", "timestamp": (now - timedelta(days=2)).isoformat(), "pnl_pct": -0.8},
        {"type": "CLOSE", "symbol": "NVDA", "timestamp": (now - timedelta(days=5)).isoformat(), "pnl_pct": 2.1},  # too old
        {"type": "BUY",   "symbol": "AMD",  "timestamp": (now - timedelta(days=1)).isoformat(), "pnl_pct": None},  # not close
    ]
    save_json(config.TRADE_JOURNAL_FILE, trades)

    from learning.data import _load_rolling_trades
    result = _load_rolling_trades(days=3)
    symbols = [t["symbol"] for t in result]
    assert "AAPL" in symbols and "MSFT" in symbols
    assert "NVDA" not in symbols, "Trade older than rolling window must be excluded"
    assert "AMD" not in symbols, "BUY records must be excluded (only CLOSE)"

@pytest.mark.unit
def test_load_rolling_trades_empty_journal(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "TRADE_JOURNAL_FILE", tmp_path / "journal.json")
    from learning.data import _load_rolling_trades
    result = _load_rolling_trades(days=5)
    assert result == []
```

- [ ] **Step 2: Run to confirm failure**

```
.venv/bin/python -m pytest tests/test_agents.py -k "load_rolling_trades" -v
```

- [ ] **Step 3: Add _load_rolling_trades() to learning/data.py**

```python
from datetime import datetime, timedelta, timezone
from utils.constants import ET
from utils.io_utils import load_json
import config

def _load_rolling_trades(days: int | None = None) -> list[dict]:
    """Return CLOSE trades from the last N days (default: LEARNING_ROLLING_DAYS).
    Excludes BUY records and trades older than the window.
    """
    n = days if days is not None else config.LEARNING_ROLLING_DAYS
    cutoff = datetime.now(ET) - timedelta(days=n)
    trades = load_json(config.TRADE_JOURNAL_FILE, [])
    result = []
    for t in trades:
        if t.get("type") != "CLOSE":
            continue
        ts_str = t.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=ET)
            if ts >= cutoff:
                result.append(t)
        except Exception:
            pass
    return result
```

- [ ] **Step 4: Run tests**

```
.venv/bin/python -m pytest tests/test_agents.py -k "load_rolling_trades" -v
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 5: Commit**

```bash
git add learning/data.py tests/test_agents.py
git commit -m "feat: _load_rolling_trades(days=N) — learning agent analyzes rolling window, not just today"
```

---

### Task 3: Indicator weight clamping

**Files:**
- Modify: `learning/trade_analysis.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_agents.py — add
import pytest, config

@pytest.mark.unit
def test_indicator_weight_clamped_at_max_mult(monkeypatch, tmp_path):
    """Updated indicator weights must not exceed INDICATOR_WEIGHT_MAX_MULT × default."""
    monkeypatch.setattr(config, "INDICATOR_WEIGHTS_FILE", tmp_path / "weights.json")
    monkeypatch.setattr(config, "INDICATOR_WEIGHT_MAX_MULT", 2.0)
    monkeypatch.setattr(config, "INDICATOR_WEIGHT_MIN_MULT", 0.3)

    from utils.io_utils import save_json, load_json
    # Default weights (1.0 each)
    defaults = {"rsi": 1.0, "macd": 1.0, "vwap": 1.0}
    save_json(config.INDICATOR_WEIGHTS_FILE, defaults)

    from learning.trade_analysis import _apply_weight_update
    # Try to set rsi weight to 5.0 (5× default) — should be clamped to 2.0
    updates = {"rsi": 5.0}
    _apply_weight_update(updates, defaults)

    result = load_json(config.INDICATOR_WEIGHTS_FILE, {})
    assert result["rsi"] <= defaults["rsi"] * config.INDICATOR_WEIGHT_MAX_MULT, \
        f"RSI weight {result['rsi']} exceeds max ({defaults['rsi'] * config.INDICATOR_WEIGHT_MAX_MULT})"

@pytest.mark.unit
def test_indicator_weight_clamped_at_min_mult(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "INDICATOR_WEIGHTS_FILE", tmp_path / "weights.json")
    monkeypatch.setattr(config, "INDICATOR_WEIGHT_MAX_MULT", 2.0)
    monkeypatch.setattr(config, "INDICATOR_WEIGHT_MIN_MULT", 0.3)

    from utils.io_utils import save_json, load_json
    defaults = {"rsi": 1.0, "macd": 1.0}
    save_json(config.INDICATOR_WEIGHTS_FILE, defaults)

    from learning.trade_analysis import _apply_weight_update
    updates = {"rsi": 0.01}   # near zero — should be clamped to 0.3
    _apply_weight_update(updates, defaults)

    result = load_json(config.INDICATOR_WEIGHTS_FILE, {})
    assert result["rsi"] >= defaults["rsi"] * config.INDICATOR_WEIGHT_MIN_MULT
```

- [ ] **Step 2: Add _apply_weight_update() to learning/trade_analysis.py**

```python
import logging
import config
from utils.io_utils import load_json, save_json

logger = logging.getLogger(__name__)

def _apply_weight_update(updates: dict, defaults: dict) -> None:
    """Apply weight updates with bounds clamping. Logs any override."""
    existing = load_json(config.INDICATOR_WEIGHTS_FILE, dict(defaults))
    clamped_any = False
    for key, raw_val in updates.items():
        default = defaults.get(key, 1.0)
        lo = default * config.INDICATOR_WEIGHT_MIN_MULT
        hi = default * config.INDICATOR_WEIGHT_MAX_MULT
        clamped = max(lo, min(hi, float(raw_val)))
        if abs(clamped - float(raw_val)) > 0.01:
            logger.warning(
                "Weight %s clamped: %.3f → %.3f (bounds: %.3f–%.3f)",
                key, raw_val, clamped, lo, hi,
            )
            clamped_any = True
        existing[key] = round(clamped, 4)
    save_json(config.INDICATOR_WEIGHTS_FILE, existing)
    if clamped_any:
        logger.info("Weight update applied with clamping — check learning quality.")
```

- [ ] **Step 3: Run tests**

```
.venv/bin/python -m pytest tests/test_agents.py -k "weight_clamped" -v
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 4: Commit**

```bash
git add learning/trade_analysis.py tests/test_agents.py
git commit -m "fix: indicator weights clamped to [MIN_MULT × default, MAX_MULT × default] on every update"
```

---

### Task 4: Strategy memory versioning

**Files:**
- Modify: `learning/agent.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_agents.py — add
import pytest, config
from pathlib import Path

@pytest.mark.unit
def test_strategy_memory_backup_created_on_write(monkeypatch, tmp_path):
    """Writing strategy_memory should create a dated .bak file."""
    mem_file = tmp_path / "strategy_memory.md"
    mem_file.write_text("old rules")
    monkeypatch.setattr(config, "STRATEGY_MEMORY_FILE", mem_file)
    monkeypatch.setattr(config, "STRATEGY_MEMORY_BACKUP_MAX", 7)

    from learning.agent import _write_strategy_memory
    _write_strategy_memory("new rules")

    bak_files = list(tmp_path.glob("strategy_memory.*.bak.md"))
    assert bak_files, "Expected at least one .bak file after write"
    assert mem_file.read_text() == "new rules"

@pytest.mark.unit
def test_strategy_memory_backup_pruned_to_max(monkeypatch, tmp_path):
    """Old backups beyond STRATEGY_MEMORY_BACKUP_MAX are deleted."""
    mem_file = tmp_path / "strategy_memory.md"
    mem_file.write_text("rules")
    monkeypatch.setattr(config, "STRATEGY_MEMORY_FILE", mem_file)
    monkeypatch.setattr(config, "STRATEGY_MEMORY_BACKUP_MAX", 3)

    # Pre-create 5 old backups
    for i in range(5):
        (tmp_path / f"strategy_memory.2026-05-{10+i}.bak.md").write_text(f"backup {i}")

    from learning.agent import _write_strategy_memory
    _write_strategy_memory("new rules")

    bak_files = sorted(tmp_path.glob("strategy_memory.*.bak.md"))
    assert len(bak_files) <= config.STRATEGY_MEMORY_BACKUP_MAX, \
        f"Too many backups: {len(bak_files)}"
```

- [ ] **Step 2: Add _write_strategy_memory() to learning/agent.py**

```python
from datetime import datetime
from pathlib import Path
import config
from utils.constants import ET

def _write_strategy_memory(content: str) -> None:
    """Write strategy_memory.md and create a dated backup. Prune old backups."""
    mem_path = Path(str(config.STRATEGY_MEMORY_FILE))
    parent = mem_path.parent
    today = datetime.now(ET).strftime("%Y-%m-%d")

    # Create backup of current content (before overwrite)
    if mem_path.exists():
        bak_name = f"strategy_memory.{today}.bak.md"
        bak_path = parent / bak_name
        bak_path.write_text(mem_path.read_text())

    # Write new content
    mem_path.write_text(content[:config.STRATEGY_MEMORY_MAX_CHARS])

    # Prune old backups beyond STRATEGY_MEMORY_BACKUP_MAX
    bak_files = sorted(parent.glob("strategy_memory.*.bak.md"))
    while len(bak_files) > config.STRATEGY_MEMORY_BACKUP_MAX:
        bak_files[0].unlink()
        bak_files = bak_files[1:]
```

Replace all `config.STRATEGY_MEMORY_FILE.write_text(...)` calls in `learning/agent.py` with `_write_strategy_memory(content)`.

- [ ] **Step 3: Run tests**

```
.venv/bin/python -m pytest tests/test_agents.py -k "strategy_memory_backup" -v
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 4: Commit**

```bash
git add learning/agent.py tests/test_agents.py
git commit -m "feat: strategy_memory versioning — dated .bak on every write, pruned to max 7 backups"
```

---

### Task 5: Backtest loads learned params

**Files:**
- Modify: `backtest.py`

- [ ] **Step 1: Write test**

```python
# tests/test_foundation.py — add (or new test file if backtest.py has tests)
import pytest, config
from unittest.mock import patch

@pytest.mark.unit
def test_backtest_loads_trading_params(monkeypatch, tmp_path):
    """Backtest must load trading_params.json before simulation if it exists."""
    monkeypatch.setattr(config, "TRADING_PARAMS_FILE", tmp_path / "params.json")
    from utils.io_utils import save_json
    save_json(config.TRADING_PARAMS_FILE, {"rsi_buy_max": 68})

    loaded = []
    import backtest
    orig = getattr(backtest, "_load_backtest_params", None)
    if orig is None:
        pytest.skip("_load_backtest_params not yet implemented — write it first")
    with patch.object(backtest, "_load_backtest_params", wraps=backtest._load_backtest_params) as mock:
        backtest._load_backtest_params()
        mock.assert_called()
```

- [ ] **Step 2: Add _load_backtest_params() to backtest.py**

At the top of `backtest.py`, add:

```python
def _load_backtest_params() -> dict:
    """Load learned trading params for backtest; fall back to config defaults if missing."""
    from utils.io_utils import load_json
    try:
        params = load_json(config.TRADING_PARAMS_FILE, {})
        if params:
            logger.info("Backtest: using learned params from trading_params.json: %s", params)
            return params
    except Exception as exc:
        logger.warning("Backtest: could not load trading_params.json (%s) — using defaults", exc)
    return {
        "rsi_buy_max": config.RSI_BUY_MAX,
        "composite_min_normal": config.COMPOSITE_MIN_NORMAL,
        "entry_start_hour": config.ENTRY_START_HOUR,
        "entry_end_hour": config.ENTRY_END_HOUR,
        "high_vol_position_mult": config.HIGH_VOL_POSITION_MULT,
        "rsi_buy_max_high_vol": config.RSI_BUY_MAX_HIGH_VOL,
        "vwap_slope_min": config.VWAP_SLOPE_MIN,
    }
```

At the start of the backtest `main()` / simulation function, call `params = _load_backtest_params()` and use `params["rsi_buy_max"]` etc. instead of `config.RSI_BUY_MAX` directly.

- [ ] **Step 3: Run tests**

```
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 4: Commit + tag**

```bash
git add backtest.py tests/test_foundation.py
git commit -m "fix: backtest loads trading_params.json (learned thresholds) instead of always using config defaults"
git tag plan6-learning-loop-complete
```
