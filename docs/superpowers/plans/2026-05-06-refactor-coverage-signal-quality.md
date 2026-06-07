# Refactor, Coverage & Signal Quality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract `utils/` and `signals/` packages, reach 80% test coverage, implement 8 signal improvements targeting 55–65% profitable-day rate, and fix 5 production bugs.

**Architecture:** 18 independent tasks in dependency order. Tasks 1–4 lay infrastructure (config + utils). Tasks 5–6 fix production bugs. Tasks 7–14 build and wire signals. Tasks 15–17 push coverage. Task 18 updates CLAUDE.md and verifies 80%. Each task commits on completion.

**Tech Stack:** Python 3.11+, pytest, pandas, zoneinfo, anthropic SDK, existing alpaca_client / market_data / config / telegram_notify modules.

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `config.py` | Modify | Add 12 new constants for signals + EOD classification |
| `utils/__init__.py` | Create | Package marker |
| `utils/io_utils.py` | Create | Shared `load_json` / `save_json` — single implementation |
| `utils/market_monitor.py` | Create | Flash-crash detection, SPY verify, lockout logic |
| `trading_agent.py` | Modify | Import from utils/; remove extracted functions; add profit-lock + progressive loss |
| `swing_manager.py` | Modify | Use `utils.io_utils` instead of ad-hoc json.load/dump |
| `macro_regime.py` | Modify | Use `utils.io_utils`; expose `spy_ret5d` in regime output |
| `learning_agent.py` | Modify | Add `_compute_regime_winrate()`; include in Claude prompt |
| `main.py` | Modify | Add `_exit_with_alert()`, replace bare `sys.exit(1)` calls |
| `signals/__init__.py` | Create | Package marker |
| `signals/time_gate.py` | Create | Minute-accurate entry time filter |
| `signals/relative_strength.py` | Create | RS ratio vs SPY — only enter top performers |
| `signals/correlation_guard.py` | Create | Block entry when sector concentration ≥ 2 |
| `signals/sentiment_sizer.py` | Create | Map sentiment score → position size multiplier |
| `signals/mtf_confirm.py` | Create | 15-min EMA(9) > EMA(21) confirmation |
| `tests/test_foundation.py` | Modify | Add ~120 new `@pytest.mark.unit` tests |
| `CLAUDE.md` | Modify | Add 3 new coding rules |

---

## Task 1: Add config constants

**Files:**
- Modify: `config.py`

- [ ] **Step 1: Append new constants block to config.py**

Open `config.py` and add after the last existing line:

```python
# ── Plan 4: Signal Quality & Refactor ─────────────────────────────────────────

# Time-of-day entry gate (minute-accurate)
ENTRY_GATE_OPEN_MINUTES  = 30   # skip first 30 min after 9:30 AM open (until 10:00)
ENTRY_GATE_CLOSE_MINUTES = 35   # skip last 35 min before 3:55 PM force-close (from 3:20)

# Relative strength filter
RS_MIN_RATIO = 0.0   # symbol 5d-return / SPY 5d-return must exceed this (0 = no underperformers)

# Correlation guard
MAX_SAME_SECTOR_POSITIONS = 2   # block entry if this many open positions share a sector

# Sentiment-weighted position sizing
# List of (min_score, multiplier) pairs checked highest-score-first
SENTIMENT_SIZE_TIERS: list[tuple[int, float]] = [
    (20,  1.25),
    (10,  1.10),
    (-10, 1.00),
    (-20, 0.75),
]
SENTIMENT_SIZE_FLOOR = 0.50   # minimum multiplier regardless of score

# Multi-timeframe confirmation
MTF_BARS_LOOKBACK     = 40   # number of 15-min bars to fetch
MTF_TIMEFRAME_MINUTES = 15   # bar size for trend confirmation

# Profit-lock trailing stop
PROFIT_LOCK_TRIGGER_PCT = 2.0   # once PnL >= this %, move stop to breakeven

# Progressive loss reduction
PROGRESSIVE_LOSS_STEP  = 0.20   # reduce size by this fraction per losing trade today
PROGRESSIVE_LOSS_FLOOR = 0.40   # minimum size multiplier (never below 40% of normal)

# EOD close classification thresholds
EOD_VERIFY_FILLS        = True   # skip journal entry if symbol not in Alpaca positions
EOD_PROFITABLE_THRESHOLD =  0.5  # pnl_pct above this → eod_force_profitable
EOD_LOSS_THRESHOLD       = -0.5  # pnl_pct below this → eod_force_loss
```

- [ ] **Step 2: Run tests to confirm nothing broke**

```bash
cd ~/Documents/projects/trading-bot
.venv/bin/pytest tests/test_foundation.py -q --tb=short 2>&1 | tail -5
```

Expected: `327 passed`

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "feat: add Plan 4 config constants for signals + EOD classification"
```

---

## Task 2: Create `utils/io_utils.py` + tests

**Files:**
- Create: `utils/__init__.py`
- Create: `utils/io_utils.py`
- Modify: `tests/test_foundation.py`

- [ ] **Step 1: Write failing tests**

Add to the bottom of `tests/test_foundation.py`:

```python
@pytest.mark.unit
class TestIoUtils:
    def test_load_json_returns_default_when_missing(self, tmp_path):
        from utils.io_utils import load_json
        result = load_json(tmp_path / "missing.json", {"x": 1})
        assert result == {"x": 1}

    def test_load_json_reads_existing_file(self, tmp_path):
        from utils.io_utils import load_json, save_json
        p = tmp_path / "data.json"
        save_json(p, {"k": 42})
        assert load_json(p, {}) == {"k": 42}

    def test_save_json_atomic_write(self, tmp_path):
        from utils.io_utils import save_json, load_json
        p = tmp_path / "out.json"
        save_json(p, [1, 2, 3])
        assert load_json(p, []) == [1, 2, 3]

    def test_save_json_no_tmp_file_left(self, tmp_path):
        from utils.io_utils import save_json
        p = tmp_path / "out.json"
        save_json(p, {"a": 1})
        assert not (tmp_path / "out.tmp").exists()

    def test_load_json_recovers_corrupt_file(self, tmp_path, monkeypatch):
        import telegram_notify
        monkeypatch.setattr(telegram_notify, "notify_file_corruption", lambda *a: None)
        from utils.io_utils import load_json
        p = tmp_path / "bad.json"
        p.write_text("not json{{{")
        result = load_json(p, {"default": True})
        assert result == {"default": True}

    def test_load_json_corrupt_creates_backup(self, tmp_path, monkeypatch):
        import telegram_notify
        monkeypatch.setattr(telegram_notify, "notify_file_corruption", lambda *a: None)
        from utils.io_utils import load_json
        p = tmp_path / "bad.json"
        p.write_text("not json{{{")
        load_json(p, {})
        assert (tmp_path / "bad.corrupt.json").exists()

    def test_save_json_raises_on_non_serialisable(self, tmp_path):
        from utils.io_utils import save_json
        import pytest
        with pytest.raises(TypeError):
            save_json(tmp_path / "bad.json", {"fn": lambda x: x})

    def test_load_json_returns_list_default(self, tmp_path):
        from utils.io_utils import load_json
        result = load_json(tmp_path / "nope.json", [])
        assert result == []

    def test_save_then_load_roundtrip(self, tmp_path):
        from utils.io_utils import save_json, load_json
        data = {"regime": "BULL_TREND", "score": 42, "items": [1, 2, 3]}
        p = tmp_path / "rt.json"
        save_json(p, data)
        assert load_json(p, {}) == data

    def test_load_json_nested_structure(self, tmp_path):
        from utils.io_utils import save_json, load_json
        data = {"a": {"b": {"c": [1, 2, 3]}}}
        p = tmp_path / "nested.json"
        save_json(p, data)
        assert load_json(p, {})["a"]["b"]["c"] == [1, 2, 3]
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
.venv/bin/pytest tests/test_foundation.py::TestIoUtils -q --tb=short 2>&1 | tail -5
```

Expected: `ImportError: No module named 'utils'`

- [ ] **Step 3: Create `utils/__init__.py`**

```bash
mkdir -p utils
touch utils/__init__.py
```

- [ ] **Step 4: Create `utils/io_utils.py`**

```python
import json
import logging
from pathlib import Path

import telegram_notify

logger = logging.getLogger(__name__)


def load_json(path: Path, default):
    """Read JSON file with corrupt-file recovery. Returns default if file missing."""
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except json.JSONDecodeError:
            backup = path.with_suffix(".corrupt.json")
            try:
                path.rename(backup)
            except Exception:
                pass
            save_json(path, default)
            logger.error(f"Corrupt JSON reset: {path.name} → {backup.name}")
            telegram_notify.notify_file_corruption(path.name)
            return default
    return default


def save_json(path: Path, data) -> None:
    """Validate JSON serialisation before touching disk, then atomic write."""
    tmp = path.with_suffix(".tmp")
    serialized = json.dumps(data, indent=2)
    json.loads(serialized)          # raises TypeError/ValueError on bad data
    with open(tmp, "w") as f:
        f.write(serialized)
    tmp.replace(path)               # atomic on POSIX
```

- [ ] **Step 5: Run tests to confirm they pass**

```bash
.venv/bin/pytest tests/test_foundation.py::TestIoUtils -q --tb=short 2>&1 | tail -5
```

Expected: `10 passed`

- [ ] **Step 6: Commit**

```bash
git add utils/__init__.py utils/io_utils.py tests/test_foundation.py
git commit -m "feat: add utils/io_utils.py with shared load_json/save_json + 10 tests"
```

---

## Task 3: Migrate all callers to `utils.io_utils`

**Files:**
- Modify: `trading_agent.py`
- Modify: `swing_manager.py`
- Modify: `macro_regime.py`
- Modify: `learning_agent.py`

- [ ] **Step 1: Update `trading_agent.py` — replace definitions with import**

Find and replace the `_load_json` and `_save_json` function definitions (lines 35–59) with:

```python
from utils.io_utils import load_json as _load_json, save_json as _save_json
```

All ~20 internal call-sites (`_load_json(...)`, `_save_json(...)`) remain unchanged — they now delegate to `utils.io_utils`.

- [ ] **Step 2: Update `swing_manager.py` — replace ad-hoc json.load/dump**

At the top of `swing_manager.py`, add after existing imports:
```python
from utils.io_utils import load_json as _load_json, save_json as _save_json
```

Replace `load_swing_positions` (lines 25–32):
```python
def load_swing_positions() -> dict:
    return _load_json(config.SWING_POSITIONS_FILE, {})
```

Replace `_save_swing_positions` (lines 35–37):
```python
def _save_swing_positions(positions: dict) -> None:
    _save_json(config.SWING_POSITIONS_FILE, positions)
```

At line 166, replace `with open(...) as f: rr = json.load(f)` with:
```python
rr = _load_json(config.RESEARCH_REPORT_FILE, {})
```

- [ ] **Step 3: Update `macro_regime.py` — replace ad-hoc writes**

Add import at top of `macro_regime.py`:
```python
from utils.io_utils import load_json as _load_json, save_json as _save_json
```

Replace line 335 (`json.dump(cache, f, indent=2)` in `refresh_fred_cache`):
```python
_save_json(config.FRED_CACHE_FILE, cache)
```

Replace lines 343 (`json.load(f)` in `_load_fred_modifier`):
```python
data = _load_json(config.FRED_CACHE_FILE, {})
return int(data.get("fred_modifier", 0))
```

Replace line 600 (`json.dump(result, f, indent=2)` in `fetch_macro_regime`):
```python
_save_json(config.MACRO_REGIME_FILE, result)
```

Replace lines 617 and 652 (json.load calls in `load_macro_regime` and `load_trading_params`):
```python
# load_macro_regime
data = _load_json(config.MACRO_REGIME_FILE, {})

# load_trading_params
saved = _load_json(config.TRADING_PARAMS_FILE, {})
```

- [ ] **Step 4: Update `learning_agent.py` — replace ad-hoc writes**

Add import at top:
```python
from utils.io_utils import load_json as _load_json, save_json as _save_json
```

Replace `_load_weights` (line 141):
```python
def _load_weights() -> dict:
    return _load_json(config.INDICATOR_WEIGHTS_FILE, {})
```

Replace `_save_weights` (line 144):
```python
def _save_weights(weights: dict):
    _save_json(config.INDICATOR_WEIGHTS_FILE, weights)
```

Replace `_save_trading_params` (line 149):
```python
def _save_trading_params(params: dict):
    _save_json(config.TRADING_PARAMS_FILE, params)
```

- [ ] **Step 5: Run full test suite**

```bash
.venv/bin/pytest tests/test_foundation.py -q --tb=short 2>&1 | tail -5
```

Expected: `327 passed` (or more if TestIoUtils counted)

- [ ] **Step 6: Commit**

```bash
git add trading_agent.py swing_manager.py macro_regime.py learning_agent.py
git commit -m "refactor: migrate all json I/O to utils.io_utils — single implementation"
```

---

## Task 4: Create `utils/market_monitor.py` + tests

**Files:**
- Create: `utils/market_monitor.py`
- Modify: `trading_agent.py`
- Modify: `tests/test_foundation.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_foundation.py`:

```python
@pytest.mark.unit
class TestMarketMonitor:
    def _write_regime(self, tmp_path, data):
        import json
        p = tmp_path / "macro_regime.json"
        p.write_text(json.dumps(data))
        return p

    def test_set_crisis_lockout_writes_file(self, tmp_path, monkeypatch):
        import config as cfg
        monkeypatch.setattr(cfg, "MACRO_REGIME_FILE", tmp_path / "macro_regime.json")
        from utils.market_monitor import set_crisis_lockout
        set_crisis_lockout(hours=48)
        import json
        data = json.loads((tmp_path / "macro_regime.json").read_text())
        assert data["flash_crash_active"] is True
        assert data["regime"] == "CRISIS"

    def test_clear_flash_crash_candidate(self, tmp_path, monkeypatch):
        import config as cfg, json
        p = self._write_regime(tmp_path, {"flash_crash_candidate": {"spy_pct": -3.0}})
        monkeypatch.setattr(cfg, "MACRO_REGIME_FILE", p)
        from utils.market_monitor import clear_flash_crash_candidate
        clear_flash_crash_candidate()
        assert json.loads(p.read_text())["flash_crash_candidate"] is None

    def test_write_flash_crash_candidate(self, tmp_path, monkeypatch):
        import config as cfg, json
        from datetime import datetime
        from zoneinfo import ZoneInfo
        p = tmp_path / "macro_regime.json"
        monkeypatch.setattr(cfg, "MACRO_REGIME_FILE", p)
        from utils.market_monitor import write_flash_crash_candidate
        write_flash_crash_candidate(-3.1, datetime.now(ZoneInfo("America/New_York")))
        data = json.loads(p.read_text())
        assert data["flash_crash_candidate"]["spy_pct"] == -3.1

    def test_verify_spy_with_polygon_returns_none_when_no_key(self, monkeypatch):
        import config as cfg
        monkeypatch.setattr(cfg, "POLYGON_API_KEY", "")
        from utils.market_monitor import verify_spy_with_polygon
        assert verify_spy_with_polygon(-3.0) is None

    def test_verify_spy_with_polygon_returns_none_on_request_error(self, monkeypatch):
        import config as cfg, requests
        monkeypatch.setattr(cfg, "POLYGON_API_KEY", "testkey")
        monkeypatch.setattr(requests, "get", lambda *a, **kw: (_ for _ in ()).throw(Exception("timeout")))
        from utils.market_monitor import verify_spy_with_polygon
        assert verify_spy_with_polygon(-3.0) is None

    def test_get_spy_prev_close_returns_zero_on_error(self, monkeypatch):
        import market_data
        monkeypatch.setattr(market_data, "get_daily_bars", lambda *a, **kw: __import__('pandas').DataFrame())
        from utils.market_monitor import get_spy_prev_close
        assert get_spy_prev_close() == 0.0

    def test_set_crisis_lockout_sets_until_timestamp(self, tmp_path, monkeypatch):
        import config as cfg, json
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        monkeypatch.setattr(cfg, "MACRO_REGIME_FILE", tmp_path / "macro_regime.json")
        from utils.market_monitor import set_crisis_lockout
        before = datetime.now(ZoneInfo("America/New_York"))
        set_crisis_lockout(hours=24)
        data = json.loads((tmp_path / "macro_regime.json").read_text())
        until = datetime.fromisoformat(data["flash_crash_until"])
        assert until > before + timedelta(hours=23)

    def test_clear_flash_crash_candidate_no_op_when_file_missing(self, tmp_path, monkeypatch):
        import config as cfg
        monkeypatch.setattr(cfg, "MACRO_REGIME_FILE", tmp_path / "missing.json")
        from utils.market_monitor import clear_flash_crash_candidate
        clear_flash_crash_candidate()  # should not raise

    def test_run_market_monitor_skips_when_spy_prev_zero(self, monkeypatch):
        from utils import market_monitor as mm
        monkeypatch.setattr(mm, "get_spy_prev_close", lambda: 0.0)
        monkeypatch.setattr(mm, "_get_latest_price", lambda s: 500.0)
        mm.run_market_monitor()  # should return early without raising

    def test_run_market_monitor_clears_candidate_when_spy_ok(self, tmp_path, monkeypatch):
        import config as cfg
        monkeypatch.setattr(cfg, "MACRO_REGIME_FILE", tmp_path / "macro_regime.json")
        from utils import market_monitor as mm
        monkeypatch.setattr(mm, "get_spy_prev_close", lambda: 500.0)
        monkeypatch.setattr(mm, "_get_latest_price", lambda s: 498.0)  # -0.4% — no crash
        cleared = []
        monkeypatch.setattr(mm, "clear_flash_crash_candidate", lambda: cleared.append(True))
        mm.run_market_monitor()
        assert cleared

    def test_run_market_monitor_writes_candidate_on_drop(self, tmp_path, monkeypatch):
        import config as cfg
        monkeypatch.setattr(cfg, "MACRO_REGIME_FILE", tmp_path / "macro_regime.json")
        monkeypatch.setattr(cfg, "POLYGON_API_KEY", "")
        from utils import market_monitor as mm
        monkeypatch.setattr(mm, "get_spy_prev_close", lambda: 500.0)
        monkeypatch.setattr(mm, "_get_latest_price", lambda s: 484.0)  # -3.2% crash candidate
        written = []
        monkeypatch.setattr(mm, "write_flash_crash_candidate", lambda pct, now: written.append(pct))
        monkeypatch.setattr(mm, "_read_candidate", lambda: None)
        mm.run_market_monitor()
        assert written

    def test_run_market_monitor_triggers_lockout_on_stage2(self, tmp_path, monkeypatch):
        import config as cfg
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        monkeypatch.setattr(cfg, "MACRO_REGIME_FILE", tmp_path / "macro_regime.json")
        monkeypatch.setattr(cfg, "POLYGON_API_KEY", "")
        from utils import market_monitor as mm
        monkeypatch.setattr(mm, "get_spy_prev_close", lambda: 500.0)
        monkeypatch.setattr(mm, "_get_latest_price", lambda s: 484.0)
        stale_ts = (datetime.now(ZoneInfo("America/New_York")) - timedelta(minutes=6)).isoformat()
        monkeypatch.setattr(mm, "_read_candidate", lambda: {"spy_pct": -3.1, "detected_at": stale_ts})
        locked = []
        monkeypatch.setattr(mm, "_trigger_flash_crash_lockout", lambda: locked.append(True))
        mm.run_market_monitor()
        assert locked

    def test_run_market_monitor_overwrites_stale_candidate(self, tmp_path, monkeypatch):
        import config as cfg
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        monkeypatch.setattr(cfg, "MACRO_REGIME_FILE", tmp_path / "macro_regime.json")
        monkeypatch.setattr(cfg, "POLYGON_API_KEY", "")
        from utils import market_monitor as mm
        monkeypatch.setattr(mm, "get_spy_prev_close", lambda: 500.0)
        monkeypatch.setattr(mm, "_get_latest_price", lambda s: 484.0)
        stale_ts = (datetime.now(ZoneInfo("America/New_York")) - timedelta(minutes=20)).isoformat()
        monkeypatch.setattr(mm, "_read_candidate", lambda: {"spy_pct": -3.1, "detected_at": stale_ts})
        written = []
        monkeypatch.setattr(mm, "write_flash_crash_candidate", lambda pct, now: written.append(pct))
        monkeypatch.setattr(mm, "_trigger_flash_crash_lockout", lambda: None)
        mm.run_market_monitor()
        assert written

    def test_run_market_monitor_handles_exception_silently(self, monkeypatch):
        from utils import market_monitor as mm
        monkeypatch.setattr(mm, "get_spy_prev_close", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        mm.run_market_monitor()  # must not raise
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/pytest tests/test_foundation.py::TestMarketMonitor -q --tb=line 2>&1 | tail -5
```

Expected: `ImportError: cannot import name ... from utils.market_monitor`

- [ ] **Step 3: Create `utils/market_monitor.py`**

```python
import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

import config
import telegram_notify

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


def _get_latest_price(symbol: str) -> float:
    from market_data import get_latest_price
    return get_latest_price(symbol)


def get_spy_prev_close() -> float:
    try:
        from market_data import get_daily_bars
        df = get_daily_bars("SPY", days=5)
        if len(df) >= 2:
            return float(df["close"].iloc[-2])
    except Exception:
        pass
    return 0.0


def verify_spy_with_polygon(spy_pct: float) -> bool | None:
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


def write_flash_crash_candidate(spy_pct: float, now: datetime) -> None:
    try:
        data = json.loads(config.MACRO_REGIME_FILE.read_text()) if config.MACRO_REGIME_FILE.exists() else {}
        data["flash_crash_candidate"] = {"spy_pct": round(spy_pct, 2), "detected_at": now.isoformat()}
        with open(config.MACRO_REGIME_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        logger.warning(f"Could not write flash_crash_candidate: {exc}")


def clear_flash_crash_candidate() -> None:
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


def _read_candidate() -> dict | None:
    try:
        if config.MACRO_REGIME_FILE.exists():
            data = json.loads(config.MACRO_REGIME_FILE.read_text())
            return data.get("flash_crash_candidate")
    except Exception:
        pass
    return None


def set_crisis_lockout(hours: int = 48) -> None:
    try:
        data = json.loads(config.MACRO_REGIME_FILE.read_text()) if config.MACRO_REGIME_FILE.exists() else {}
        until = (datetime.now(ET) + timedelta(hours=hours)).isoformat()
        data["flash_crash_active"] = True
        data["flash_crash_until"] = until
        data["regime"] = "CRISIS"
        data["flash_crash_candidate"] = None
        with open(config.MACRO_REGIME_FILE, "w") as f:
            json.dump(data, f, indent=2)
        logger.critical(f"FLASH CRASH LOCKOUT set — CRISIS regime active until {until}")
    except Exception as exc:
        logger.error(f"Could not write flash crash lockout: {exc}")


def _trigger_flash_crash_lockout() -> None:
    from trading_agent import force_close_all  # lazy — avoids circular import at module load
    force_close_all()
    try:
        from swing_manager import load_swing_positions, _execute_full_close
        from alpaca_client import AlpacaClient
        alpaca = AlpacaClient()
        for sym, pos in list(load_swing_positions().items()):
            _execute_full_close(sym, pos, alpaca, "flash_crash")
    except Exception as exc:
        logger.warning(f"flash crash: swing close failed: {exc}")
    set_crisis_lockout(hours=48)
    telegram_notify.notify_regime_change("NORMAL", "CRISIS")


def _handle_flash_crash_stage2(spy_pct: float) -> None:
    now = datetime.now(ET)
    try:
        candidate = _read_candidate()
        if candidate is None:
            write_flash_crash_candidate(spy_pct, now)
            logger.warning("Flash crash Stage 1 — awaiting Stage 2 confirmation in 5 min")
            return
        age_min = (now - datetime.fromisoformat(candidate["detected_at"])).total_seconds() / 60
        if age_min <= 15:
            logger.critical(f"Flash crash Stage 2 confirmed — SPY still at {spy_pct:+.1f}%")
            _trigger_flash_crash_lockout()
        else:
            write_flash_crash_candidate(spy_pct, now)
            logger.warning("Flash crash stale candidate overwritten — awaiting fresh Stage 2")
    except Exception as exc:
        logger.warning(f"Flash crash stage 2 check failed: {exc}")


def run_market_monitor() -> None:
    try:
        spy_current = _get_latest_price("SPY")
        spy_prev = get_spy_prev_close()
        if spy_prev <= 0:
            return
        spy_pct = (spy_current - spy_prev) / spy_prev * 100

        if spy_pct > config.FLASH_CRASH_SPY_DROP_PCT:
            clear_flash_crash_candidate()
            return

        logger.warning(f"FLASH CRASH CANDIDATE: SPY={spy_current:.2f} prev={spy_prev:.2f} ({spy_pct:+.1f}%)")

        if config.POLYGON_API_KEY:
            result = verify_spy_with_polygon(spy_pct)
            if result is True:
                logger.critical("Polygon confirmed flash crash — triggering lockout immediately")
                _trigger_flash_crash_lockout()
                return
            if result is False:
                logger.info("Polygon disagrees — bad SPY tick, discarding candidate")
                clear_flash_crash_candidate()
                return

        _handle_flash_crash_stage2(spy_pct)
    except Exception as exc:
        logger.debug(f"run_market_monitor: {exc}")
```

- [ ] **Step 4: Update `trading_agent.py` — replace extracted functions with imports**

Remove the 7 functions (lines ~779–928: `_get_spy_prev_close`, `_verify_spy_with_polygon`, `_write_flash_crash_candidate`, `_clear_flash_crash_candidate`, `_trigger_flash_crash_lockout`, `_handle_flash_crash_stage2`, `_set_crisis_lockout`, `run_market_monitor`) and replace with:

```python
from utils.market_monitor import (
    get_spy_prev_close as _get_spy_prev_close,
    verify_spy_with_polygon as _verify_spy_with_polygon,
    write_flash_crash_candidate as _write_flash_crash_candidate,
    clear_flash_crash_candidate as _clear_flash_crash_candidate,
    set_crisis_lockout as _set_crisis_lockout,
    run_market_monitor,
)
```

- [ ] **Step 5: Run all tests**

```bash
.venv/bin/pytest tests/test_foundation.py -q --tb=short 2>&1 | tail -5
```

Expected: `342 passed` (327 + 15 new)

- [ ] **Step 6: Commit**

```bash
git add utils/market_monitor.py trading_agent.py tests/test_foundation.py
git commit -m "refactor: extract utils/market_monitor.py from trading_agent + 15 tests"
```

---

## Task 5: Bug fix — `_exit_with_alert` in `main.py` + tests

**Files:**
- Modify: `main.py`
- Modify: `tests/test_foundation.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_foundation.py`:

```python
@pytest.mark.unit
class TestStartupExitAlerts:
    def test_telegram_called_before_exit_missing_alpaca(self, monkeypatch):
        import config as cfg, sys
        import telegram_notify
        monkeypatch.setattr(cfg, "ALPACA_API_KEY", "")
        monkeypatch.setattr(cfg, "ALPACA_SECRET_KEY", "")
        call_order = []
        monkeypatch.setattr(telegram_notify, "_send", lambda msg: call_order.append("telegram"))
        monkeypatch.setattr(sys, "exit", lambda code=1: call_order.append("exit"))
        from main import _exit_with_alert
        _exit_with_alert("Missing ALPACA keys")
        assert call_order.index("telegram") < call_order.index("exit")

    def test_telegram_called_before_exit_missing_anthropic(self, monkeypatch):
        import sys, telegram_notify
        call_order = []
        monkeypatch.setattr(telegram_notify, "_send", lambda msg: call_order.append("telegram"))
        monkeypatch.setattr(sys, "exit", lambda code=1: call_order.append("exit"))
        from main import _exit_with_alert
        _exit_with_alert("Missing ANTHROPIC_API_KEY")
        assert call_order.index("telegram") < call_order.index("exit")

    def test_exit_with_alert_message_contains_text(self, monkeypatch):
        import sys, telegram_notify
        sent = []
        monkeypatch.setattr(telegram_notify, "_send", lambda msg: sent.append(msg))
        monkeypatch.setattr(sys, "exit", lambda code=1: None)
        from main import _exit_with_alert
        _exit_with_alert("Missing XYZ key")
        assert any("XYZ" in m for m in sent)

    def test_exit_with_alert_telegram_failure_still_exits(self, monkeypatch):
        import sys, telegram_notify
        monkeypatch.setattr(telegram_notify, "_send", lambda msg: (_ for _ in ()).throw(Exception("net error")))
        exited = []
        monkeypatch.setattr(sys, "exit", lambda code=1: exited.append(code))
        from main import _exit_with_alert
        _exit_with_alert("some error")
        assert exited  # sys.exit still called even if Telegram fails
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/pytest tests/test_foundation.py::TestStartupExitAlerts -q --tb=line 2>&1 | tail -5
```

Expected: `ImportError: cannot import name '_exit_with_alert' from 'main'`

- [ ] **Step 3: Add `_exit_with_alert` to `main.py`**

Find the `main()` function definition and add this helper just before it:

```python
def _exit_with_alert(msg: str, code: int = 1) -> None:
    """Send Telegram alert then exit. Always call this instead of bare sys.exit()."""
    logger.error(msg)
    try:
        telegram_notify._send(f"🚨 Bot failed to start: {msg}")
    except Exception:
        pass  # never let Telegram failure prevent the exit
    sys.exit(code)
```

- [ ] **Step 4: Replace bare `sys.exit(1)` calls in `main()`**

Replace lines 763–768:
```python
# Before:
if not config.ALPACA_API_KEY or not config.ALPACA_SECRET_KEY:
    logger.error("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY in .env")
    sys.exit(1)
if not config.ANTHROPIC_API_KEY:
    logger.error("Missing ANTHROPIC_API_KEY in .env")
    sys.exit(1)

# After:
if not config.ALPACA_API_KEY or not config.ALPACA_SECRET_KEY:
    _exit_with_alert("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY in .env")
if not config.ANTHROPIC_API_KEY:
    _exit_with_alert("Missing ANTHROPIC_API_KEY in .env")
```

- [ ] **Step 5: Run tests**

```bash
.venv/bin/pytest tests/test_foundation.py::TestStartupExitAlerts -q --tb=short 2>&1 | tail -5
```

Expected: `4 passed`

- [ ] **Step 6: Commit**

```bash
git add main.py tests/test_foundation.py
git commit -m "fix: send Telegram alert before sys.exit for missing API keys + 4 tests"
```

---

## Task 6: Bug fixes — `force_close_all` retry + phantom journal + `close_reason`

**Files:**
- Modify: `trading_agent.py`
- Modify: `tests/test_foundation.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_foundation.py`:

```python
@pytest.mark.unit
class TestEodCloseReason:
    def test_profitable_above_threshold(self):
        from trading_agent import _eod_close_reason
        assert _eod_close_reason(1.0) == "eod_force_profitable"

    def test_loss_below_threshold(self):
        from trading_agent import _eod_close_reason
        assert _eod_close_reason(-1.0) == "eod_force_loss"

    def test_flat_between_thresholds(self):
        from trading_agent import _eod_close_reason
        assert _eod_close_reason(0.0) == "eod_force_flat"

    def test_exactly_at_profitable_threshold(self):
        from trading_agent import _eod_close_reason
        assert _eod_close_reason(0.5) == "eod_force_profitable"

    def test_exactly_at_loss_threshold(self):
        from trading_agent import _eod_close_reason
        assert _eod_close_reason(-0.5) == "eod_force_loss"

    def test_just_inside_flat_range(self):
        from trading_agent import _eod_close_reason
        assert _eod_close_reason(0.49) == "eod_force_flat"
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/pytest tests/test_foundation.py::TestEodCloseReason -q --tb=line 2>&1 | tail -5
```

Expected: `ImportError: cannot import name '_eod_close_reason'`

- [ ] **Step 3: Add `_eod_close_reason` to `trading_agent.py`**

Add this pure function just before `force_close_all`:

```python
def _eod_close_reason(pnl_pct: float) -> str:
    if pnl_pct >= config.EOD_PROFITABLE_THRESHOLD:
        return "eod_force_profitable"
    if pnl_pct <= config.EOD_LOSS_THRESHOLD:
        return "eod_force_loss"
    return "eod_force_flat"
```

- [ ] **Step 4: Update `force_close_all` — retry + phantom skip + classified reason**

Replace the body of `force_close_all()` with:

```python
def force_close_all():
    logger.info("Force-closing all day-trade positions @ 3:55 PM ET")
    alpaca = AlpacaClient()

    alpaca.cancel_all_orders()
    import time as _time
    _time.sleep(1)  # let Alpaca process cancellations before closing

    try:
        from swing_manager import load_swing_positions
        swing_symbols = set(load_swing_positions().keys())
    except Exception:
        swing_symbols = set()
    if swing_symbols:
        logger.info(f"Preserving {len(swing_symbols)} swing position(s): {sorted(swing_symbols)}")

    # Fetch live Alpaca positions once for phantom-entry detection
    try:
        live_alpaca = alpaca.get_positions() if config.EOD_VERIFY_FILLS else None
    except Exception:
        live_alpaca = None

    open_positions = load_open_positions()
    failed_closes: list[str] = []

    for symbol, pos in list(open_positions.items()):
        if symbol in swing_symbols:
            continue

        # Skip phantom entries — position was never filled in Alpaca
        if live_alpaca is not None and symbol not in live_alpaca:
            logger.warning(f"EOD: {symbol} not in Alpaca positions — entry likely unfilled, skipping journal")
            continue

        try:
            price = alpaca.get_latest_price(symbol)
        except Exception:
            price = pos["entry_price"]
            logger.warning(f"Could not get price for {symbol} — using entry price for P&L calc.")

        pnl_pct = (price - pos["entry_price"]) / pos["entry_price"] * 100
        pnl_dollar = (price - pos["entry_price"]) * pos["qty"]

        # Close in Alpaca with retry
        closed = False
        for attempt in range(2):
            try:
                alpaca.close_position(symbol)
                closed = True
                break
            except Exception as exc:
                if attempt == 0:
                    import time as _time
                    _time.sleep(2)
                else:
                    logger.warning(f"EOD: could not close {symbol} after retry: {exc}")
                    failed_closes.append(symbol)

        append_trade({
            "type": "CLOSE",
            "symbol": symbol,
            "timestamp": datetime.now(ET).isoformat(),
            "exit_price": round(price, 4),
            "entry_price": round(pos["entry_price"], 4),
            "qty": pos["qty"],
            "pnl_pct": round(pnl_pct, 2),
            "pnl_dollar": round(pnl_dollar, 2),
            "close_reason": _eod_close_reason(pnl_pct),
            "entry_composite_score": pos.get("entry_composite_score"),
            "exit_composite_score": None,
            "exit_indicators": {},
            "entry_indicators": pos.get("entry_indicators", {}),
            "entry_sentiment_score": pos.get("entry_sentiment_score"),
            "claude_reasoning": pos.get("claude_reasoning", ""),
            "indicator_signals_at_entry": pos.get("indicator_signals", {}),
            "mode_at_entry": pos.get("mode_at_entry"),
            "regime_at_entry": pos.get("regime_at_entry"),
        })
        logger.info(f"EOD close {symbol}: P&L={pnl_pct:+.2f}% (${pnl_dollar:+.2f})")

    # Sweep untracked Alpaca positions
    try:
        alpaca_positions = alpaca.get_positions()
        for sym in list(alpaca_positions.keys()):
            if sym not in swing_symbols and sym not in open_positions:
                try:
                    alpaca.close_position(sym)
                    logger.warning(f"EOD: closed untracked Alpaca position {sym}")
                except Exception as exc:
                    logger.warning(f"EOD: could not close {sym} in Alpaca: {exc}")
                    failed_closes.append(sym)
    except Exception as exc:
        logger.warning(f"EOD: get_positions failed: {exc}")

    # Fallback: nuclear close if any symbols still failed
    if failed_closes:
        logger.warning(f"EOD: falling back to close_all_positions for {failed_closes}")
        try:
            alpaca.close_all_positions()
        except Exception as exc:
            logger.error(f"EOD: close_all_positions also failed: {exc}")
        try:
            telegram_notify._send(f"⚠️ EOD close failed for {failed_closes} — manual check needed")
        except Exception:
            pass

    remaining = {sym: pos for sym, pos in open_positions.items() if sym in swing_symbols}
    save_open_positions(remaining)
    logger.info(f"Day-trade positions force-closed. {len(swing_symbols)} swing position(s) preserved.")
```

- [ ] **Step 5: Run tests**

```bash
.venv/bin/pytest tests/test_foundation.py::TestEodCloseReason -q --tb=short 2>&1 | tail -5
```

Expected: `6 passed`

- [ ] **Step 6: Run full suite**

```bash
.venv/bin/pytest tests/test_foundation.py -q --tb=short 2>&1 | tail -5
```

Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add trading_agent.py tests/test_foundation.py
git commit -m "fix: EOD close retry+fallback, phantom journal skip, classified close_reason + 6 tests"
```

---

## Task 7: `signals/time_gate.py` + wire into `run_trading_cycle`

**Files:**
- Create: `signals/__init__.py`
- Create: `signals/time_gate.py`
- Modify: `trading_agent.py`
- Modify: `tests/test_foundation.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_foundation.py`:

```python
@pytest.mark.unit
class TestTimeGate:
    def _dt(self, hour, minute):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime(2026, 5, 6, hour, minute, 0, tzinfo=ZoneInfo("America/New_York"))

    def test_allowed_at_1030(self):
        from signals.time_gate import is_entry_allowed
        assert is_entry_allowed(self._dt(10, 30)) is True

    def test_blocked_at_930(self):
        from signals.time_gate import is_entry_allowed
        assert is_entry_allowed(self._dt(9, 30)) is False

    def test_blocked_at_945(self):
        from signals.time_gate import is_entry_allowed
        assert is_entry_allowed(self._dt(9, 45)) is False

    def test_allowed_at_1000(self):
        from signals.time_gate import is_entry_allowed
        assert is_entry_allowed(self._dt(10, 0)) is True

    def test_blocked_at_1520(self):
        from signals.time_gate import is_entry_allowed
        assert is_entry_allowed(self._dt(15, 20)) is False

    def test_allowed_at_1319(self):
        from signals.time_gate import is_entry_allowed
        assert is_entry_allowed(self._dt(13, 19)) is True

    def test_blocked_before_market_open(self):
        from signals.time_gate import is_entry_allowed
        assert is_entry_allowed(self._dt(8, 0)) is False

    def test_blocked_after_force_close(self):
        from signals.time_gate import is_entry_allowed
        assert is_entry_allowed(self._dt(16, 0)) is False
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/pytest tests/test_foundation.py::TestTimeGate -q --tb=line 2>&1 | tail -5
```

Expected: `ImportError: No module named 'signals'`

- [ ] **Step 3: Create `signals/__init__.py` and `signals/time_gate.py`**

```bash
mkdir -p signals
touch signals/__init__.py
```

Create `signals/time_gate.py`:

```python
from datetime import datetime, time
from zoneinfo import ZoneInfo

import config

ET = ZoneInfo("America/New_York")

# Market open / force-close anchors (ET)
_MARKET_OPEN  = time(9, 30)
_FORCE_CLOSE  = time(15, 55)


def is_entry_allowed(now: datetime) -> bool:
    """
    Returns True only when now is within the allowed entry window:
      After  9:30 + ENTRY_GATE_OPEN_MINUTES  (default 10:00)
      Before 15:55 - ENTRY_GATE_CLOSE_MINUTES (default 15:20)
    Pure function — no I/O.
    """
    t = now.time()
    gate_open_h, gate_open_m = divmod(9 * 60 + 30 + config.ENTRY_GATE_OPEN_MINUTES, 60)
    gate_close_total = 15 * 60 + 55 - config.ENTRY_GATE_CLOSE_MINUTES
    gate_close_h, gate_close_m = divmod(gate_close_total, 60)

    open_time  = time(gate_open_h,  gate_open_m)
    close_time = time(gate_close_h, gate_close_m)

    return open_time <= t < close_time
```

- [ ] **Step 4: Wire into `run_trading_cycle` in `trading_agent.py`**

Add import near the top of `trading_agent.py` (with other imports):
```python
from signals.time_gate import is_entry_allowed as _is_entry_allowed
```

In `run_trading_cycle()`, replace the existing time-of-day block (lines ~982–986):
```python
# Before:
entry_start = now.replace(hour=params["entry_start_hour"], minute=0, second=0, microsecond=0)
entry_end   = now.replace(hour=params["entry_end_hour"],   minute=0, second=0, microsecond=0)
if not (entry_start <= now < entry_end):
    logger.debug(f"Outside entry window ({params['entry_start_hour']}–{params['entry_end_hour']} ET) — no new entries.")
    can_open = False

# After:
if not _is_entry_allowed(now):
    logger.debug(f"Outside entry window (time gate) — no new entries.")
    can_open = False
```

- [ ] **Step 5: Run tests**

```bash
.venv/bin/pytest tests/test_foundation.py::TestTimeGate -q --tb=short 2>&1 | tail -5
```

Expected: `8 passed`

- [ ] **Step 6: Commit**

```bash
git add signals/__init__.py signals/time_gate.py trading_agent.py tests/test_foundation.py
git commit -m "feat: signals/time_gate.py with minute-accurate entry window + 8 tests"
```

---

## Task 8: `signals/relative_strength.py` + wire

**Files:**
- Create: `signals/relative_strength.py`
- Modify: `macro_regime.py`
- Modify: `trading_agent.py`
- Modify: `tests/test_foundation.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_foundation.py`:

```python
@pytest.mark.unit
class TestRelativeStrength:
    def test_outperforming_spy_passes(self):
        from signals.relative_strength import rs_rank, passes_rs_filter
        ratio = rs_rank("NVDA", spy_ret5d=2.0, symbol_ret5d=5.0)
        assert ratio == pytest.approx(2.5)
        assert passes_rs_filter(ratio) is True

    def test_underperforming_spy_fails(self):
        from signals.relative_strength import rs_rank, passes_rs_filter
        ratio = rs_rank("AAPL", spy_ret5d=3.0, symbol_ret5d=-1.0)
        assert ratio < 0
        assert passes_rs_filter(ratio) is False

    def test_zero_spy_return_returns_zero_ratio(self):
        from signals.relative_strength import rs_rank
        assert rs_rank("X", spy_ret5d=0.0, symbol_ret5d=5.0) == 0.0

    def test_equal_performance_passes(self):
        from signals.relative_strength import rs_rank, passes_rs_filter
        ratio = rs_rank("X", spy_ret5d=2.0, symbol_ret5d=2.0)
        assert passes_rs_filter(ratio) is True  # RS_MIN_RATIO=0.0 → exactly 1.0 passes

    def test_custom_threshold(self):
        from signals.relative_strength import passes_rs_filter
        assert passes_rs_filter(0.8, threshold=1.0) is False
        assert passes_rs_filter(1.2, threshold=1.0) is True

    def test_negative_spy_return_inverts_ratio(self):
        from signals.relative_strength import rs_rank, passes_rs_filter
        # SPY down 2%, symbol down 1% → outperforming → ratio > 1
        ratio = rs_rank("X", spy_ret5d=-2.0, symbol_ret5d=-1.0)
        assert passes_rs_filter(ratio) is True
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/pytest tests/test_foundation.py::TestRelativeStrength -q --tb=line 2>&1 | tail -5
```

Expected: `ImportError`

- [ ] **Step 3: Create `signals/relative_strength.py`**

```python
import config


def rs_rank(symbol: str, spy_ret5d: float, symbol_ret5d: float) -> float:
    """
    RS ratio = symbol_ret5d / spy_ret5d.
    > 1.0 means outperforming SPY. Returns 0.0 when SPY return is zero.
    Pure function — no I/O.
    """
    if spy_ret5d == 0.0:
        return 0.0
    return symbol_ret5d / spy_ret5d


def passes_rs_filter(rs_ratio: float, threshold: float = config.RS_MIN_RATIO) -> bool:
    """True if symbol is not underperforming SPY by more than threshold."""
    return rs_ratio >= threshold
```

- [ ] **Step 4: Expose `spy_ret5d` in `macro_regime.json` output**

In `macro_regime.py`, find `fetch_macro_regime()` and add `spy_ret5d` to the `result` dict just before the `_save_json` call:

```python
result["spy_ret5d"] = round(_ret5d(signals.get("SPY", [])), 4)
```

- [ ] **Step 5: Wire into `run_trading_cycle` in `trading_agent.py`**

Add import:
```python
from signals.relative_strength import rs_rank, passes_rs_filter as _passes_rs_filter
```

In `run_trading_cycle()`, after `regime_info = load_macro_regime()`, read the SPY return:
```python
spy_ret5d = regime_info.get("spy_ret5d", 0.0)
```

In the BUY action block (after the existing sentiment veto, around line 1150), add:
```python
# Relative strength filter — skip laggards vs SPY
try:
    from market_data import get_daily_bars
    sym_bars = get_daily_bars(symbol, days=7)
    if len(sym_bars) >= 6:
        sym_ret5d = (float(sym_bars["close"].iloc[-1]) - float(sym_bars["close"].iloc[-6])) / float(sym_bars["close"].iloc[-6]) * 100
        rs = rs_rank(symbol, spy_ret5d, sym_ret5d)
        if not _passes_rs_filter(rs):
            logger.info(f"SKIP {symbol} — RS ratio {rs:.2f} below {config.RS_MIN_RATIO} (underperforming SPY)")
            continue
except Exception:
    pass  # data unavailable — allow entry
```

- [ ] **Step 6: Run tests**

```bash
.venv/bin/pytest tests/test_foundation.py::TestRelativeStrength -q --tb=short 2>&1 | tail -5
```

Expected: `6 passed`

- [ ] **Step 7: Commit**

```bash
git add signals/relative_strength.py macro_regime.py trading_agent.py tests/test_foundation.py
git commit -m "feat: signals/relative_strength.py RS filter vs SPY + 6 tests"
```

---

## Task 9: `signals/correlation_guard.py` + wire

**Files:**
- Create: `signals/correlation_guard.py`
- Modify: `trading_agent.py`
- Modify: `tests/test_foundation.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_foundation.py`:

```python
@pytest.mark.unit
class TestCorrelationGuard:
    def _pos(self, symbol):
        return {symbol: {"mode_at_entry": "DAY_TRADE"}}

    def test_passes_with_no_open_positions(self):
        from signals.correlation_guard import passes_correlation_guard
        assert passes_correlation_guard("NVDA", {}) is True

    def test_passes_with_one_same_sector(self):
        from signals.correlation_guard import passes_correlation_guard
        assert passes_correlation_guard("NVDA", {"AMD": {}}) is True

    def test_blocked_with_two_same_sector(self):
        from signals.correlation_guard import passes_correlation_guard
        assert passes_correlation_guard("NVDA", {"AMD": {}, "AVGO": {}}) is False

    def test_passes_different_sectors(self):
        from signals.correlation_guard import passes_correlation_guard
        assert passes_correlation_guard("NVDA", {"JPM": {}, "GS": {}}) is True

    def test_unknown_sector_treated_as_other(self):
        from signals.correlation_guard import passes_correlation_guard, sector_of
        assert sector_of("ZZZZZ") == "OTHER"
        assert passes_correlation_guard("ZZZZZ", {"ZZZZY": {}, "ZZZZX": {}}) is True

    def test_sector_of_tier1_returns_correct_sector(self):
        from signals.correlation_guard import sector_of
        assert sector_of("NVDA") == "AI_SEMI"
        assert sector_of("JPM") == "FINANCIALS"

    def test_sector_of_case_insensitive(self):
        from signals.correlation_guard import sector_of
        assert sector_of("nvda") == sector_of("NVDA")

    def test_blocked_at_exact_limit(self):
        from signals.correlation_guard import passes_correlation_guard
        # MAX_SAME_SECTOR_POSITIONS=2: exactly 2 same-sector open → block
        assert passes_correlation_guard("NVDA", {"AMD": {}, "AVGO": {}}) is False
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/pytest tests/test_foundation.py::TestCorrelationGuard -q --tb=line 2>&1 | tail -5
```

Expected: `ImportError`

- [ ] **Step 3: Create `signals/correlation_guard.py`**

```python
import config
from tier1_config import TIER1_NAMES

# Sector map for non-tier-1 tickers in tickers.txt
_EXTENDED_SECTOR: dict[str, str] = {
    # Cloud / SaaS
    "SNOW": "TECHNOLOGY", "HUBS": "TECHNOLOGY", "DDOG": "TECHNOLOGY",
    "TTD":  "TECHNOLOGY", "SHOP": "TECHNOLOGY", "NET":  "TECHNOLOGY",
    "ZS":   "TECHNOLOGY", "CRWD": "TECHNOLOGY", "PANW": "TECHNOLOGY",
    # Consumer Discretionary
    "AMZN": "CONSUMER_DISC", "TSLA": "CONSUMER_DISC", "NKE": "CONSUMER_DISC",
    "HD":   "CONSUMER_DISC", "MCD":  "CONSUMER_DISC", "SBUX": "CONSUMER_DISC",
    # Media / Streaming
    "SPOT": "MEDIA", "NFLX": "MEDIA", "DIS": "MEDIA",
    # Social / Ads
    "PINS": "MEDIA", "RDDT": "MEDIA", "SNAP": "MEDIA",
    # Fintech / Payments
    "PYPL": "FINANCIALS", "SQ": "FINANCIALS", "COIN": "FINANCIALS",
    # Healthcare
    "ISRG": "PHARMA_HEALTH", "DXCM": "PHARMA_HEALTH", "MRNA": "PHARMA_HEALTH",
    # Industrials / Equipment
    "URI": "INDUSTRIAL", "DE": "INDUSTRIAL",
    # Energy
    "SLB": "ENERGY", "HAL": "ENERGY",
}


def sector_of(symbol: str) -> str:
    """Return sector string. Falls back to 'OTHER' for unknown symbols."""
    sym = symbol.upper()
    entry = TIER1_NAMES.get(sym)
    if entry:
        return entry["sector"]
    return _EXTENDED_SECTOR.get(sym, "OTHER")


def passes_correlation_guard(symbol: str, open_positions: dict) -> bool:
    """
    Returns False if there are already MAX_SAME_SECTOR_POSITIONS or more open
    positions in the same sector as symbol. 'OTHER' sector is never blocked.
    Pure function — no I/O.
    """
    my_sector = sector_of(symbol)
    if my_sector == "OTHER":
        return True
    count = sum(1 for sym in open_positions if sector_of(sym) == my_sector)
    return count < config.MAX_SAME_SECTOR_POSITIONS
```

- [ ] **Step 4: Wire into `run_trading_cycle` in `trading_agent.py`**

Add import:
```python
from signals.correlation_guard import passes_correlation_guard as _passes_correlation_guard
```

In the BUY action block, after the RS filter and before `_check_entry_gates`, add:
```python
# Correlation guard — block sector concentration
if not _passes_correlation_guard(symbol, open_positions):
    logger.info(f"SKIP {symbol} — sector concentration limit reached")
    continue
```

- [ ] **Step 5: Run tests**

```bash
.venv/bin/pytest tests/test_foundation.py::TestCorrelationGuard -q --tb=short 2>&1 | tail -5
```

Expected: `8 passed`

- [ ] **Step 6: Commit**

```bash
git add signals/correlation_guard.py trading_agent.py tests/test_foundation.py
git commit -m "feat: signals/correlation_guard.py sector concentration gate + 8 tests"
```

---

## Task 10: `signals/sentiment_sizer.py` + wire

**Files:**
- Create: `signals/sentiment_sizer.py`
- Modify: `trading_agent.py`
- Modify: `tests/test_foundation.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_foundation.py`:

```python
@pytest.mark.unit
class TestSentimentSizer:
    def test_high_positive_returns_125(self):
        from signals.sentiment_sizer import sentiment_size_multiplier
        assert sentiment_size_multiplier(25) == pytest.approx(1.25)

    def test_moderate_positive_returns_110(self):
        from signals.sentiment_sizer import sentiment_size_multiplier
        assert sentiment_size_multiplier(15) == pytest.approx(1.10)

    def test_neutral_returns_100(self):
        from signals.sentiment_sizer import sentiment_size_multiplier
        assert sentiment_size_multiplier(0) == pytest.approx(1.00)

    def test_moderate_negative_returns_075(self):
        from signals.sentiment_sizer import sentiment_size_multiplier
        assert sentiment_size_multiplier(-15) == pytest.approx(0.75)

    def test_very_negative_returns_050(self):
        from signals.sentiment_sizer import sentiment_size_multiplier
        assert sentiment_size_multiplier(-30) == pytest.approx(0.50)

    def test_extreme_positive_capped_at_125(self):
        from signals.sentiment_sizer import sentiment_size_multiplier
        assert sentiment_size_multiplier(100) == pytest.approx(1.25)

    def test_extreme_negative_floored_at_050(self):
        from signals.sentiment_sizer import sentiment_size_multiplier
        assert sentiment_size_multiplier(-100) == pytest.approx(0.50)

    def test_boundary_at_20(self):
        from signals.sentiment_sizer import sentiment_size_multiplier
        assert sentiment_size_multiplier(20) == pytest.approx(1.25)

    def test_boundary_at_10(self):
        from signals.sentiment_sizer import sentiment_size_multiplier
        assert sentiment_size_multiplier(10) == pytest.approx(1.10)

    def test_boundary_at_minus_10(self):
        from signals.sentiment_sizer import sentiment_size_multiplier
        assert sentiment_size_multiplier(-10) == pytest.approx(1.00)
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/pytest tests/test_foundation.py::TestSentimentSizer -q --tb=line 2>&1 | tail -5
```

Expected: `ImportError`

- [ ] **Step 3: Create `signals/sentiment_sizer.py`**

```python
import config


def sentiment_size_multiplier(sentiment_score: int) -> float:
    """
    Maps research sentiment score to a position size multiplier.
    Tiers defined in config.SENTIMENT_SIZE_TIERS (highest score first).
    Pure function — no I/O.
    """
    for min_score, multiplier in sorted(config.SENTIMENT_SIZE_TIERS, reverse=True):
        if sentiment_score >= min_score:
            return multiplier
    return config.SENTIMENT_SIZE_FLOOR
```

- [ ] **Step 4: Wire into `compute_position_size_for_mode` in `trading_agent.py`**

Add import at top:
```python
from signals.sentiment_sizer import sentiment_size_multiplier as _sentiment_size_multiplier
```

Update `compute_position_size_for_mode` signature to accept `sentiment_score`:
```python
def compute_position_size_for_mode(
    portfolio_value: float,
    price: float,
    atr: float,
    mode: str,
    bot_herding_score: int,
    squeeze_setup: bool,
    sentiment_score: int = 0,
) -> int:
```

At the end of the function, before `return int(qty)`, add:
```python
    # Sentiment-weighted sizing
    qty = max(1.0, round(qty * _sentiment_size_multiplier(sentiment_score)))
```

Update the call-site in `run_trading_cycle` (line ~1176) to pass sentiment:
```python
qty = compute_position_size_for_mode(
    portfolio_value, iv.current_price, iv.atr, mode,
    iv.bot_herding_score, iv.squeeze_setup,
    sentiment_score=research.get(symbol, {}).get("sentiment_score", 0),
)
```

- [ ] **Step 5: Run tests**

```bash
.venv/bin/pytest tests/test_foundation.py::TestSentimentSizer -q --tb=short 2>&1 | tail -5
```

Expected: `10 passed`

- [ ] **Step 6: Commit**

```bash
git add signals/sentiment_sizer.py trading_agent.py tests/test_foundation.py
git commit -m "feat: signals/sentiment_sizer.py sentiment→position-size multiplier + 10 tests"
```

---

## Task 11: `signals/mtf_confirm.py` + wire

**Files:**
- Create: `signals/mtf_confirm.py`
- Modify: `trading_agent.py`
- Modify: `tests/test_foundation.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_foundation.py`:

```python
@pytest.mark.unit
class TestMtfConfirm:
    def _bars(self, ema9_vals, ema21_vals):
        """Build a minimal DataFrame with pre-computed EMA columns."""
        import pandas as pd
        import numpy as np
        n = max(len(ema9_vals), len(ema21_vals))
        closes = list(range(100, 100 + n))
        df = pd.DataFrame({"close": closes})
        # Patch computed EMAs by writing close column such that EMA results match
        # Simpler: just test the pure logic directly with a synthetic close series
        return df

    def test_ema9_above_ema21_passes(self):
        import pandas as pd
        from signals.mtf_confirm import passes_mtf_filter
        # Ascending close series → EMA9 > EMA21 (faster EMA leads)
        closes = list(range(80, 130))  # 50 bars of rising prices
        df = pd.DataFrame({"close": closes})
        assert passes_mtf_filter(df) is True

    def test_ema9_below_ema21_fails(self):
        import pandas as pd
        from signals.mtf_confirm import passes_mtf_filter
        # Descending close series → EMA9 < EMA21
        closes = list(range(130, 80, -1))  # 50 bars of falling prices
        df = pd.DataFrame({"close": closes})
        assert passes_mtf_filter(df) is False

    def test_insufficient_data_returns_true(self):
        import pandas as pd
        from signals.mtf_confirm import passes_mtf_filter
        df = pd.DataFrame({"close": [100.0] * 5})
        assert passes_mtf_filter(df) is True  # allow entry when data unavailable

    def test_empty_dataframe_returns_true(self):
        import pandas as pd
        from signals.mtf_confirm import passes_mtf_filter
        assert passes_mtf_filter(pd.DataFrame()) is True

    def test_flat_prices_returns_true(self):
        import pandas as pd
        from signals.mtf_confirm import passes_mtf_filter
        df = pd.DataFrame({"close": [100.0] * 40})
        assert passes_mtf_filter(df) is True  # EMA9 == EMA21 on flat → passes

    def test_missing_close_column_returns_true(self):
        import pandas as pd
        from signals.mtf_confirm import passes_mtf_filter
        df = pd.DataFrame({"open": [100.0] * 40})
        assert passes_mtf_filter(df) is True
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/pytest tests/test_foundation.py::TestMtfConfirm -q --tb=line 2>&1 | tail -5
```

Expected: `ImportError`

- [ ] **Step 3: Create `signals/mtf_confirm.py`**

```python
import pandas as pd

import config


def passes_mtf_filter(bars_15min: pd.DataFrame) -> bool:
    """
    True if 15-min EMA(9) >= EMA(21) — trend confirmation for long entries.
    Returns True (allow entry) when data is insufficient to compute EMAs.
    Pure function — no I/O, no network calls.
    """
    try:
        if bars_15min.empty or "close" not in bars_15min.columns or len(bars_15min) < 21:
            return True
        closes = bars_15min["close"]
        ema9  = float(closes.ewm(span=9,  adjust=False).mean().iloc[-1])
        ema21 = float(closes.ewm(span=21, adjust=False).mean().iloc[-1])
        return ema9 >= ema21
    except Exception:
        return True  # allow entry when data unavailable
```

- [ ] **Step 4: Wire into `run_trading_cycle` in `trading_agent.py`**

Add import:
```python
from signals.mtf_confirm import passes_mtf_filter as _passes_mtf_filter
```

In the BUY action block, after the correlation guard and before `_check_entry_gates`, add:
```python
# Multi-timeframe confirmation — 15-min trend must agree
try:
    from market_data import get_intraday_bars
    bars_15m = get_intraday_bars(symbol, lookback=config.MTF_BARS_LOOKBACK, timeframe_min=config.MTF_TIMEFRAME_MINUTES)
    if not _passes_mtf_filter(bars_15m):
        logger.info(f"SKIP {symbol} — 15-min EMA9 < EMA21 (downtrend on higher timeframe)")
        continue
except Exception:
    pass  # data unavailable — allow entry
```

- [ ] **Step 5: Run tests**

```bash
.venv/bin/pytest tests/test_foundation.py::TestMtfConfirm -q --tb=short 2>&1 | tail -5
```

Expected: `6 passed`

- [ ] **Step 6: Commit**

```bash
git add signals/mtf_confirm.py trading_agent.py tests/test_foundation.py
git commit -m "feat: signals/mtf_confirm.py 15-min EMA trend confirmation + 6 tests"
```

---

## Task 12: Profit-lock trailing stop in `run_position_monitor`

**Files:**
- Modify: `trading_agent.py`
- Modify: `tests/test_foundation.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_foundation.py`:

```python
@pytest.mark.unit
class TestProfitLockTrailingStop:
    def _pos(self, entry, stop_pct):
        return {"entry_price": entry, "stop_loss_pct": stop_pct, "qty": 10,
                "take_profit_pct": 5.0, "mode_at_entry": "DAY_TRADE",
                "regime_at_entry": "NORMAL", "claude_reasoning": "",
                "entry_composite_score": 30, "entry_indicators": {}, "entry_sentiment_score": 0}

    def test_stop_moved_to_breakeven_when_profit_triggers(self):
        from trading_agent import _apply_profit_lock
        pos = self._pos(entry=100.0, stop_pct=4.0)
        updated = _apply_profit_lock(pos, current_price=102.5)  # +2.5% > PROFIT_LOCK_TRIGGER_PCT
        assert updated["stop_loss_pct"] <= 0.0  # stop at or above entry

    def test_stop_not_moved_below_trigger(self):
        from trading_agent import _apply_profit_lock
        pos = self._pos(entry=100.0, stop_pct=4.0)
        updated = _apply_profit_lock(pos, current_price=101.0)  # +1% < trigger
        assert updated["stop_loss_pct"] == 4.0  # unchanged

    def test_stop_never_moved_backward(self):
        from trading_agent import _apply_profit_lock
        pos = self._pos(entry=100.0, stop_pct=-1.0)  # already at breakeven
        updated = _apply_profit_lock(pos, current_price=103.0)
        assert updated["stop_loss_pct"] <= -1.0  # not worsened

    def test_returns_original_pos_when_below_trigger(self):
        from trading_agent import _apply_profit_lock
        pos = self._pos(entry=100.0, stop_pct=4.0)
        updated = _apply_profit_lock(pos, current_price=100.5)
        assert updated is pos  # same object — no copy made

    def test_exactly_at_trigger_moves_stop(self):
        from trading_agent import _apply_profit_lock
        pos = self._pos(entry=100.0, stop_pct=4.0)
        updated = _apply_profit_lock(pos, current_price=102.0)  # exactly 2.0%
        assert updated["stop_loss_pct"] <= 0.0

    def test_returns_dict_with_all_original_keys(self):
        from trading_agent import _apply_profit_lock
        pos = self._pos(entry=100.0, stop_pct=4.0)
        updated = _apply_profit_lock(pos, current_price=103.0)
        for key in pos:
            assert key in updated
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/pytest tests/test_foundation.py::TestProfitLockTrailingStop -q --tb=line 2>&1 | tail -5
```

Expected: `ImportError: cannot import name '_apply_profit_lock'`

- [ ] **Step 3: Add `_apply_profit_lock` to `trading_agent.py`**

Add this pure helper just before `run_position_monitor`:

```python
def _apply_profit_lock(pos: dict, current_price: float) -> dict:
    """
    If unrealised PnL >= PROFIT_LOCK_TRIGGER_PCT, move stop_loss_pct to
    at most breakeven (0%). Returns the same dict object if no change needed.
    """
    entry = pos["entry_price"]
    pnl_pct = (current_price - entry) / entry * 100
    if pnl_pct < config.PROFIT_LOCK_TRIGGER_PCT:
        return pos
    current_stop_pct = pos.get("stop_loss_pct", config.STOP_LOSS_PCT)
    # Move stop to breakeven (0%) if current stop is still below breakeven
    new_stop_pct = min(current_stop_pct, 0.0)
    if new_stop_pct == current_stop_pct:
        return pos
    updated = dict(pos)
    updated["stop_loss_pct"] = new_stop_pct
    return updated
```

- [ ] **Step 4: Call `_apply_profit_lock` inside Layer 2 of `run_position_monitor`**

In `run_position_monitor()`, inside the Layer 2 loop, after fetching `current_price` and before the SL/TP check, add:

```python
        # Profit-lock: move stop to breakeven once PnL >= PROFIT_LOCK_TRIGGER_PCT
        pos = _apply_profit_lock(pos, current_price)
        if pos is not open_positions.get(symbol):
            open_positions[symbol] = pos
```

- [ ] **Step 5: Run tests**

```bash
.venv/bin/pytest tests/test_foundation.py::TestProfitLockTrailingStop -q --tb=short 2>&1 | tail -5
```

Expected: `6 passed`

- [ ] **Step 6: Commit**

```bash
git add trading_agent.py tests/test_foundation.py
git commit -m "feat: profit-lock trailing stop moves to breakeven at +2% PnL + 6 tests"
```

---

## Task 13: Progressive loss reduction in `compute_position_size_for_mode`

**Files:**
- Modify: `trading_agent.py`
- Modify: `tests/test_foundation.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_foundation.py`:

```python
@pytest.mark.unit
class TestProgressiveLossReduction:
    def test_no_losses_returns_full_size(self):
        from trading_agent import _progressive_loss_multiplier
        assert _progressive_loss_multiplier(0) == pytest.approx(1.0)

    def test_one_loss_reduces_by_step(self):
        from trading_agent import _progressive_loss_multiplier
        assert _progressive_loss_multiplier(1) == pytest.approx(0.80)

    def test_two_losses(self):
        from trading_agent import _progressive_loss_multiplier
        assert _progressive_loss_multiplier(2) == pytest.approx(0.60)

    def test_three_losses(self):
        from trading_agent import _progressive_loss_multiplier
        assert _progressive_loss_multiplier(3) == pytest.approx(0.40)

    def test_floor_enforced_at_four_losses(self):
        from trading_agent import _progressive_loss_multiplier
        assert _progressive_loss_multiplier(4) == pytest.approx(0.40)

    def test_floor_enforced_at_ten_losses(self):
        from trading_agent import _progressive_loss_multiplier
        assert _progressive_loss_multiplier(10) == pytest.approx(0.40)

    def test_negative_loss_count_treated_as_zero(self):
        from trading_agent import _progressive_loss_multiplier
        assert _progressive_loss_multiplier(-1) == pytest.approx(1.0)

    def test_multiplier_applied_in_compute_size(self, tmp_path, monkeypatch):
        import config as cfg
        monkeypatch.setattr(cfg, "TRADE_JOURNAL_FILE", tmp_path / "journal.json")
        import json
        (tmp_path / "journal.json").write_text(json.dumps([
            {"type": "CLOSE", "pnl_pct": -1.5, "timestamp": "2026-05-06T10:00:00"},
            {"type": "CLOSE", "pnl_pct": -2.0, "timestamp": "2026-05-06T11:00:00"},
        ]))
        from trading_agent import compute_position_size_for_mode
        # 2 losses → 0.6× multiplier → qty should be smaller than baseline
        qty_with_losses = compute_position_size_for_mode(100_000, 100.0, 2.0, "DAY_TRADE", 0, False, 0)
        monkeypatch.setattr(cfg, "TRADE_JOURNAL_FILE", tmp_path / "empty.json")
        qty_no_losses = compute_position_size_for_mode(100_000, 100.0, 2.0, "DAY_TRADE", 0, False, 0)
        assert qty_with_losses <= qty_no_losses
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/pytest tests/test_foundation.py::TestProgressiveLossReduction -q --tb=line 2>&1 | tail -5
```

Expected: `ImportError: cannot import name '_progressive_loss_multiplier'`

- [ ] **Step 3: Add `_progressive_loss_multiplier` and `_count_todays_losses` to `trading_agent.py`**

Add before `compute_position_size_for_mode`:

```python
def _count_todays_losses() -> int:
    """Count CLOSE entries in today's journal with pnl_pct < 0."""
    from datetime import date
    today = date.today().isoformat()
    journal = _load_json(config.TRADE_JOURNAL_FILE, [])
    return sum(
        1 for t in journal
        if t.get("type") == "CLOSE"
        and (t.get("pnl_pct") or 0) < 0
        and (t.get("timestamp") or "").startswith(today)
    )


def _progressive_loss_multiplier(loss_count: int) -> float:
    """
    Returns size multiplier based on number of losing trades today.
    Reduces by PROGRESSIVE_LOSS_STEP per loss, floored at PROGRESSIVE_LOSS_FLOOR.
    """
    if loss_count <= 0:
        return 1.0
    raw = 1.0 - config.PROGRESSIVE_LOSS_STEP * loss_count
    return max(config.PROGRESSIVE_LOSS_FLOOR, raw)
```

- [ ] **Step 4: Apply multiplier inside `compute_position_size_for_mode`**

At the end of `compute_position_size_for_mode`, before `return int(qty)`, add:

```python
    # Progressive loss reduction — shrink size as losing trades accumulate today
    loss_mult = _progressive_loss_multiplier(_count_todays_losses())
    qty = max(1.0, round(qty * loss_mult))
```

- [ ] **Step 5: Run tests**

```bash
.venv/bin/pytest tests/test_foundation.py::TestProgressiveLossReduction -q --tb=short 2>&1 | tail -5
```

Expected: `8 passed`

- [ ] **Step 6: Commit**

```bash
git add trading_agent.py tests/test_foundation.py
git commit -m "feat: progressive loss reduction reduces size 20% per loss, floor 40% + 8 tests"
```

---

## Task 14: Per-regime win-rate context in `learning_agent`

**Files:**
- Modify: `learning_agent.py`
- Modify: `tests/test_foundation.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_foundation.py`:

```python
@pytest.mark.unit
class TestRegimeWinRate:
    def _trades(self, entries):
        return [
            {"type": "CLOSE", "regime_at_entry": regime, "pnl_pct": pnl}
            for regime, pnl in entries
        ]

    def test_empty_trades_returns_empty(self):
        from learning_agent import _compute_regime_winrate
        assert _compute_regime_winrate([]) == {}

    def test_single_winner(self):
        from learning_agent import _compute_regime_winrate
        trades = self._trades([("BULL_TREND", 2.0)])
        result = _compute_regime_winrate(trades)
        assert result["BULL_TREND"] == pytest.approx(100.0)

    def test_single_loser(self):
        from learning_agent import _compute_regime_winrate
        trades = self._trades([("NORMAL", -1.5)])
        result = _compute_regime_winrate(trades)
        assert result["NORMAL"] == pytest.approx(0.0)

    def test_mixed_regime(self):
        from learning_agent import _compute_regime_winrate
        trades = self._trades([("CHOPPY", 1.0), ("CHOPPY", -0.5), ("CHOPPY", 2.0)])
        result = _compute_regime_winrate(trades)
        assert result["CHOPPY"] == pytest.approx(66.7, abs=0.1)

    def test_multiple_regimes(self):
        from learning_agent import _compute_regime_winrate
        trades = self._trades([("BULL_TREND", 1.0), ("NORMAL", -1.0)])
        result = _compute_regime_winrate(trades)
        assert "BULL_TREND" in result
        assert "NORMAL" in result
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/pytest tests/test_foundation.py::TestRegimeWinRate -q --tb=line 2>&1 | tail -5
```

Expected: `ImportError: cannot import name '_compute_regime_winrate'`

- [ ] **Step 3: Add `_compute_regime_winrate` to `learning_agent.py`**

Add after `_aggregate_by_regime`:

```python
def _compute_regime_winrate(closed_trades: list[dict]) -> dict[str, float]:
    """Return {regime: win_rate_pct} for each regime seen in closed_trades."""
    by_regime: dict[str, list[float]] = {}
    for t in closed_trades:
        regime = t.get("regime_at_entry") or "UNKNOWN"
        by_regime.setdefault(regime, []).append(t.get("pnl_pct") or 0.0)
    return {
        regime: round(sum(1 for p in pnls if p > 0) / len(pnls) * 100, 1)
        for regime, pnls in by_regime.items()
    }
```

- [ ] **Step 4: Pass win-rate to Claude prompt in `run_learning_agent`**

In `run_learning_agent()`, after computing `regime_agg`, add:
```python
    regime_winrate = _compute_regime_winrate(closed_trades)
```

In the Claude `messages` content string, add after the existing `regime_agg` line:
```python
                    f"Per-regime win rate (today):\n{json.dumps(regime_winrate, indent=2)}\n\n"
```

- [ ] **Step 5: Run tests**

```bash
.venv/bin/pytest tests/test_foundation.py::TestRegimeWinRate -q --tb=short 2>&1 | tail -5
```

Expected: `5 passed`

- [ ] **Step 6: Commit**

```bash
git add learning_agent.py tests/test_foundation.py
git commit -m "feat: per-regime win-rate added to learning agent Claude prompt + 5 tests"
```

---

## Task 15: Coverage push — `research_agent` + `macro_regime`

**Files:**
- Modify: `tests/test_foundation.py`

- [ ] **Step 1: Add `research_agent` orchestration tests**

Add to `tests/test_foundation.py`:

```python
@pytest.mark.unit
class TestResearchAgentOrchestration:
    def test_compute_macro_shock_score_zero_on_no_keywords(self):
        from research_agent import _compute_macro_shock_score
        assert _compute_macro_shock_score([{"title": "stocks rally"}]) == 0

    def test_compute_macro_shock_score_nonzero_on_crisis_keyword(self):
        from research_agent import _compute_macro_shock_score
        score = _compute_macro_shock_score([{"title": "Fed raises rates sharply — recession fears spike"}])
        assert score > 0

    def test_compute_sector_sentiment_empty_report(self):
        from research_agent import _compute_sector_sentiment
        result = _compute_sector_sentiment({})
        assert isinstance(result, dict)

    def test_detect_capitulation_candidate_false_on_positive_sentiment(self):
        from research_agent import _detect_capitulation_candidate
        result = _detect_capitulation_candidate("NVDA", {"sentiment_score": 40, "sentiment_label": "BULLISH"}, regime="NORMAL")
        assert result is False

    def test_detect_capitulation_candidate_true_on_crisis_bearish(self):
        from research_agent import _detect_capitulation_candidate
        result = _detect_capitulation_candidate(
            "NVDA",
            {"sentiment_score": -60, "sentiment_label": "BEARISH"},
            regime="CRISIS",
        )
        assert result is True

    def test_quick_sentiment_result_returns_bullish_on_high_pct(self, monkeypatch):
        import config as cfg
        monkeypatch.setattr(cfg, "STOCKTWITS_MIN_TAGGED", 10)
        from research_agent import _quick_sentiment_result
        st = {"bullish_pct": 80, "total_tagged": 20}
        result = _quick_sentiment_result(st, [])
        assert result is not None
        assert result["sentiment_label"] == "BULLISH"

    def test_quick_sentiment_result_none_on_insufficient_data(self):
        from research_agent import _quick_sentiment_result
        result = _quick_sentiment_result({"bullish_pct": 80, "total_tagged": 2}, [])
        assert result is None

    def test_fetch_with_retry_succeeds_on_first_try(self):
        from research_agent import _fetch_with_retry
        result = _fetch_with_retry(lambda: 42)
        assert result == 42

    def test_fetch_with_retry_retries_on_failure(self):
        from research_agent import _fetch_with_retry
        calls = [0]
        def flaky():
            calls[0] += 1
            if calls[0] < 2:
                raise ValueError("temporary")
            return "ok"
        result = _fetch_with_retry(flaky, attempts=3, base_delay=0.01)
        assert result == "ok"

    def test_fetch_with_retry_raises_after_exhausting_attempts(self):
        from research_agent import _fetch_with_retry
        import pytest
        with pytest.raises(ValueError):
            _fetch_with_retry(lambda: (_ for _ in ()).throw(ValueError("always")), attempts=2, base_delay=0.01)
```

- [ ] **Step 2: Add `macro_regime` orchestration tests**

Add to `tests/test_foundation.py`:

```python
@pytest.mark.unit
class TestMacroRegimeOrchestration:
    def test_load_fred_modifier_returns_zero_when_file_missing(self, tmp_path, monkeypatch):
        import config as cfg
        monkeypatch.setattr(cfg, "FRED_CACHE_FILE", tmp_path / "missing.json")
        from macro_regime import _load_fred_modifier
        assert _load_fred_modifier() == 0

    def test_check_flash_crash_true_on_large_drop(self):
        from macro_regime import check_flash_crash
        assert check_flash_crash(spy_current=470.0, spy_prev_close=500.0) is True

    def test_check_flash_crash_false_on_small_drop(self):
        from macro_regime import check_flash_crash
        assert check_flash_crash(spy_current=498.0, spy_prev_close=500.0) is False

    def test_load_macro_regime_returns_dict_when_file_missing(self, tmp_path, monkeypatch):
        import config as cfg
        monkeypatch.setattr(cfg, "MACRO_REGIME_FILE", tmp_path / "missing.json")
        from macro_regime import load_macro_regime
        result = load_macro_regime()
        assert isinstance(result, dict)

    def test_is_choppy_true_on_alternating_closes(self):
        from macro_regime import _is_choppy
        closes = [100, 102, 99, 103, 98, 101]
        assert _is_choppy(closes) is True

    def test_is_choppy_false_on_trending_closes(self):
        from macro_regime import _is_choppy
        closes = [100, 102, 104, 106, 108, 110]
        assert _is_choppy(closes) is False

    def test_count_consecutive_direction_bull(self):
        from macro_regime import _count_consecutive_direction
        closes = [100, 101, 102, 103, 104]
        assert _count_consecutive_direction(closes, "up") >= 4

    def test_compute_macro_health_score_returns_int(self):
        from macro_regime import compute_macro_health_score
        signals = {"VIX": [20.0]*5, "SPY": [500.0]*5}
        result = compute_macro_health_score(signals)
        assert isinstance(result, int)
```

- [ ] **Step 3: Run new tests**

```bash
.venv/bin/pytest tests/test_foundation.py::TestResearchAgentOrchestration tests/test_foundation.py::TestMacroRegimeOrchestration -q --tb=short 2>&1 | tail -8
```

Expected: 18 passed

- [ ] **Step 4: Commit**

```bash
git add tests/test_foundation.py
git commit -m "test: add research_agent + macro_regime orchestration tests for coverage"
```

---

## Task 16: Coverage push — `swing_manager` + `learning_agent`

**Files:**
- Modify: `tests/test_foundation.py`

- [ ] **Step 1: Add `swing_manager` orchestration tests**

Add to `tests/test_foundation.py`:

```python
@pytest.mark.unit
class TestSwingManagerOrchestration:
    def _pos(self, **overrides):
        base = {
            "entry_price": 100.0, "qty": 10, "stop_loss_pct": 8.0,
            "take_profit_pct": 8.0, "tp1_pct": 4.0, "tp2_pct": 6.0,
            "tp1_done": False, "tp2_done": False, "trailing_active": False,
            "trailing_stop_price": None, "tp1_qty": 4, "tp2_qty": 3, "trailing_qty": 3,
        }
        base.update(overrides)
        return base

    def test_evaluate_tiered_tp_none_when_flat(self):
        from swing_manager import evaluate_tiered_tp_action
        result = evaluate_tiered_tp_action(self._pos(), current_price=100.0)
        assert result["action"] == "NONE"

    def test_evaluate_tiered_tp_close_tp1_when_at_tp1(self):
        from swing_manager import evaluate_tiered_tp_action
        result = evaluate_tiered_tp_action(self._pos(entry_price=100.0, tp1_pct=4.0), current_price=104.5)
        assert result["action"] == "CLOSE_TP1"

    def test_evaluate_tiered_tp_hard_stop(self):
        from swing_manager import evaluate_tiered_tp_action
        result = evaluate_tiered_tp_action(self._pos(entry_price=100.0, stop_loss_pct=8.0), current_price=91.0)
        assert result["action"] == "CLOSE_HARD_STOP"

    def test_qualifies_for_swing_fails_without_tier1(self):
        from tier1_config import qualifies_for_swing
        ok, reason = qualifies_for_swing("ZZZZZ", 30, 60, 1.5, 25, "BULL_TREND", False, False)
        assert ok is False
        assert "tier-1" in reason

    def test_qualifies_for_swing_fails_low_composite(self):
        from tier1_config import qualifies_for_swing
        ok, reason = qualifies_for_swing("NVDA", 20, 60, 1.5, 25, "BULL_TREND", False, False)
        assert ok is False

    def test_qualifies_for_swing_passes(self):
        from tier1_config import qualifies_for_swing
        ok, _ = qualifies_for_swing("NVDA", 30, 60, 1.5, 25, "BULL_TREND", False, False)
        assert ok is True

    def test_compute_tier_quantities_sums_to_total(self):
        from swing_manager import _compute_tier_quantities
        for total in [3, 5, 10, 20]:
            t1, t2, t3 = _compute_tier_quantities(total)
            assert t1 + t2 + t3 == total

    def test_evaluate_gap_action_hard_stop_on_gap_down(self):
        from swing_manager import _evaluate_gap_action
        pos = self._pos(entry_price=100.0, stop_loss_pct=8.0)
        action = _evaluate_gap_action(pos, current_price=88.0)
        assert action == "CLOSE_HARD_STOP"
```

- [ ] **Step 2: Add `learning_agent` orchestration tests**

Add to `tests/test_foundation.py`:

```python
@pytest.mark.unit
class TestLearningAgentOrchestration:
    def _trade(self, pnl, regime="NORMAL", mode="DAY_TRADE", reason="monitor_stop_loss"):
        return {
            "type": "CLOSE", "symbol": "AAPL", "pnl_pct": pnl, "pnl_dollar": pnl * 10,
            "regime_at_entry": regime, "mode_at_entry": mode, "close_reason": reason,
            "entry_composite_score": 30, "entry_indicators": {}, "entry_sentiment_score": 0,
        }

    def test_aggregate_by_regime_groups_correctly(self):
        from learning_agent import _aggregate_by_regime
        trades = [self._trade(1.0, "BULL_TREND"), self._trade(-1.0, "BULL_TREND")]
        result = _aggregate_by_regime(trades)
        assert result["by_regime"]["BULL_TREND"]["count"] == 2
        assert result["by_regime"]["BULL_TREND"]["win_rate_pct"] == pytest.approx(50.0)

    def test_classify_loss_source_market_structure(self):
        from learning_agent import _classify_loss_source
        t = self._trade(-1.0, regime="CRISIS")
        assert _classify_loss_source(t) == "market_structure"

    def test_classify_loss_source_earnings(self):
        from learning_agent import _classify_loss_source
        t = self._trade(-1.0, reason="earnings_surprise")
        assert _classify_loss_source(t) == "earnings_surprise"

    def test_clamp_threshold_caps_rsi_max(self):
        from learning_agent import _clamp_threshold
        assert _clamp_threshold("rsi_buy_max", 99) <= 50

    def test_clamp_threshold_floors_rsi_max(self):
        from learning_agent import _clamp_threshold
        assert _clamp_threshold("rsi_buy_max", 1) >= 30

    def test_load_todays_closed_trades_empty_when_no_file(self, tmp_path, monkeypatch):
        import config as cfg
        monkeypatch.setattr(cfg, "TRADE_JOURNAL_FILE", tmp_path / "missing.json")
        from learning_agent import _load_todays_closed_trades
        assert _load_todays_closed_trades() == []

    def test_compute_regime_winrate_correct_pct(self):
        from learning_agent import _compute_regime_winrate
        trades = [self._trade(1.0), self._trade(1.0), self._trade(-1.0)]
        result = _compute_regime_winrate(trades)
        assert result["NORMAL"] == pytest.approx(66.7, abs=0.1)

    def test_aggregate_handles_missing_regime(self):
        from learning_agent import _aggregate_by_regime
        trade = {"type": "CLOSE", "pnl_pct": 1.0, "pnl_dollar": 10.0}
        result = _aggregate_by_regime([trade])
        assert "UNKNOWN" in result["by_regime"]
```

- [ ] **Step 3: Run new tests**

```bash
.venv/bin/pytest tests/test_foundation.py::TestSwingManagerOrchestration tests/test_foundation.py::TestLearningAgentOrchestration -q --tb=short 2>&1 | tail -8
```

Expected: 16 passed

- [ ] **Step 4: Commit**

```bash
git add tests/test_foundation.py
git commit -m "test: add swing_manager + learning_agent orchestration tests for coverage"
```

---

## Task 17: Coverage push — `earnings_calendar`, `economic_calendar`, `daily_report`

**Files:**
- Modify: `tests/test_foundation.py`

- [ ] **Step 1: Add earnings + economic calendar tests**

Add to `tests/test_foundation.py`:

```python
@pytest.mark.unit
class TestEarningsCalendarCoverage:
    def test_is_earnings_imminent_false_when_no_file(self, tmp_path, monkeypatch):
        import config as cfg
        monkeypatch.setattr(cfg, "EARNINGS_CALENDAR_FILE", tmp_path / "missing.json")
        from earnings_calendar import is_earnings_imminent
        assert is_earnings_imminent("NVDA") is False

    def test_detect_pead_setup_false_when_no_data(self, tmp_path, monkeypatch):
        import config as cfg
        monkeypatch.setattr(cfg, "EARNINGS_CALENDAR_FILE", tmp_path / "missing.json")
        from earnings_calendar import detect_pead_setup
        ok, score = detect_pead_setup("NVDA")
        assert ok is False
        assert score == 0

    def test_refresh_earnings_calendar_writes_file(self, tmp_path, monkeypatch):
        import config as cfg, json
        monkeypatch.setattr(cfg, "EARNINGS_CALENDAR_FILE", tmp_path / "earnings.json")
        monkeypatch.setattr(cfg, "WATCH_LIST_FILE", tmp_path / "wl.txt")
        (tmp_path / "wl.txt").write_text("AAPL\nMSFT\n")
        import requests
        class FakeResp:
            status_code = 200
            def json(self): return {"earnings": {"earningsDate": []}}
            def raise_for_status(self): pass
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResp())
        from earnings_calendar import refresh_earnings_calendar
        refresh_earnings_calendar()
        assert (tmp_path / "earnings.json").exists()


@pytest.mark.unit
class TestEconomicCalendarCoverage:
    def test_is_event_risk_window_false_when_no_file(self, tmp_path, monkeypatch):
        import config as cfg
        from datetime import datetime
        from zoneinfo import ZoneInfo
        monkeypatch.setattr(cfg, "ECONOMIC_CALENDAR_FILE", tmp_path / "missing.json")
        from economic_calendar import is_event_risk_window
        result, name = is_event_risk_window(datetime.now(ZoneInfo("America/New_York")))
        assert result is False

    def test_refresh_economic_calendar_writes_file(self, tmp_path, monkeypatch):
        import config as cfg, requests
        monkeypatch.setattr(cfg, "ECONOMIC_CALENDAR_FILE", tmp_path / "econ.json")
        class FakeResp:
            status_code = 200
            text = "Date,Time,Currency,Impact,Event\n2026-05-06,08:30,USD,High,NFP\n"
            def raise_for_status(self): pass
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResp())
        from economic_calendar import refresh_economic_calendar
        refresh_economic_calendar()
        assert (tmp_path / "econ.json").exists()


@pytest.mark.unit
class TestDailyReportCoverage:
    def test_build_report_body_returns_string(self, tmp_path, monkeypatch):
        import config as cfg
        monkeypatch.setattr(cfg, "TRADE_JOURNAL_FILE", tmp_path / "journal.json")
        monkeypatch.setattr(cfg, "MACRO_REGIME_FILE", tmp_path / "regime.json")
        import json
        (tmp_path / "journal.json").write_text(json.dumps([]))
        (tmp_path / "regime.json").write_text(json.dumps({"regime": "NORMAL"}))
        from daily_report import _build_report_body
        body = _build_report_body()
        assert isinstance(body, str)
        assert len(body) > 0

    def test_daily_report_sent_flag_roundtrip(self, tmp_path, monkeypatch):
        import config as cfg
        from datetime import date
        monkeypatch.setattr(cfg, "DAILY_REPORT_SENT_FILE", tmp_path / "sent.json")
        from daily_report import _mark_sent, _already_sent_today
        assert _already_sent_today() is False
        _mark_sent()
        assert _already_sent_today() is True
```

- [ ] **Step 2: Run new tests**

```bash
.venv/bin/pytest tests/test_foundation.py::TestEarningsCalendarCoverage tests/test_foundation.py::TestEconomicCalendarCoverage tests/test_foundation.py::TestDailyReportCoverage -q --tb=short 2>&1 | tail -8
```

Expected: all pass (some may skip if network-dependent — that's acceptable)

- [ ] **Step 3: Commit**

```bash
git add tests/test_foundation.py
git commit -m "test: add earnings_calendar + economic_calendar + daily_report coverage tests"
```

---

## Task 18: CLAUDE.md additions + final coverage verification

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add three coding rules to `CLAUDE.md`**

In the `## Coding Practices` section, add after the existing `### Comments` subsection:

```markdown
### Exit paths must alert before quitting

Never call `sys.exit()` directly in `main.py`. Always use `_exit_with_alert(msg)` so Telegram fires before the process dies. This ensures operators are paged when the bot fails to start.

### Signal functions must be pure

All functions in `signals/` must be pure: no Alpaca calls, no file I/O, no side effects. This is what enables Approach C's backtest threshold tuner to call them in bulk without side effects.

### JSON file I/O goes through `utils.io_utils`

Never use bare `json.load` / `json.dump` in module bodies. Always use `utils.io_utils.load_json` / `save_json`. This ensures consistent corrupt-file recovery and atomic writes across all modules.
```

- [ ] **Step 2: Run full test suite**

```bash
.venv/bin/pytest tests/test_foundation.py -q --tb=short 2>&1 | tail -8
```

Expected: 450+ passed, 0 failed

- [ ] **Step 3: Run coverage report**

```bash
.venv/bin/pytest tests/test_foundation.py --cov=. --cov-report=term-missing --cov-config=.coveragerc -q --tb=no 2>&1 | grep -E "TOTAL|trading_agent|research_agent|macro_regime|swing_manager|learning|signals|utils"
```

Expected: TOTAL ≥ 80%

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: add 3 coding rules (exit-alert, pure signals, io_utils) to CLAUDE.md"
```

- [ ] **Step 5: Final check — confirm trading_agent.py line count reduced**

```bash
wc -l trading_agent.py
```

Expected: ≤ 1,100 lines (down from 1,370)

---

## Self-Review

**Spec coverage check:**

| Spec section | Task(s) |
|---|---|
| utils/io_utils.py | Task 2, 3 |
| utils/market_monitor.py | Task 4 |
| Bug 1 — _exit_with_alert | Task 5 |
| Bugs 2-4 — force_close_all | Task 6 |
| signals/time_gate.py | Task 7 |
| signals/relative_strength.py | Task 8 |
| signals/correlation_guard.py | Task 9 |
| signals/sentiment_sizer.py | Task 10 |
| signals/mtf_confirm.py | Task 11 |
| Profit-lock trailing stop | Task 12 |
| Progressive loss reduction | Task 13 |
| Per-regime win-rate | Task 14 |
| research_agent coverage | Task 15 |
| macro_regime coverage | Task 15 |
| swing_manager coverage | Task 16 |
| learning_agent coverage | Task 16 |
| earnings + econ + daily_report coverage | Task 17 |
| CLAUDE.md rules | Task 18 |
| config constants (all thresholds) | Task 1 |

All 7 spec sections covered. No gaps.

**Type consistency check:** All function signatures defined in Task N are used correctly in later tasks — `load_json(path, default)`, `save_json(path, data)`, `is_entry_allowed(now: datetime) -> bool`, `rs_rank(symbol, spy_ret5d, symbol_ret5d) -> float`, `passes_correlation_guard(symbol, open_positions) -> bool`, `sentiment_size_multiplier(score) -> float`, `passes_mtf_filter(df) -> bool`, `_apply_profit_lock(pos, current_price) -> dict`, `_progressive_loss_multiplier(loss_count) -> float`, `_compute_regime_winrate(trades) -> dict`.
