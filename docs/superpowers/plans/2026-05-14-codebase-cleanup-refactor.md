# Codebase Cleanup & Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate duplication, fix io_utils bypass violations, consolidate HTTP infrastructure, move orphan agents into the `agents/` package, and split an oversized test file — all without changing external behaviour.

**Architecture:** Three new shared modules (`utils/constants.py`, `utils/http.py`, `agents/`) replace scattered duplicates. All callers are updated in-place; backward-compat shims are deleted. Tests are split by domain boundary.

**Tech Stack:** Python 3.11, pytest, requests (Session), existing `utils/io_utils.load_json / save_json`.

---

## File Map

### Created
| File | Role |
|---|---|
| `utils/constants.py` | Single source for `ET` (Eastern timezone) used by every module |
| `utils/http.py` | `yahoo_get(url)` — shared Session + 429-backoff for all Yahoo Finance calls |
| `agents/__init__.py` | Package init (empty) |
| `agents/watch_list.py` | Moved from `watch_list_agent.py` |
| `agents/volatility_screener.py` | Moved from `volatility_screener.py` |
| `tests/test_trading_modes.py` | Split from `test_trading.py` — mode routing + sizing + context (~237 lines) |
| `tests/test_trading_lifecycle.py` | Split from `test_trading.py` — startup, force-close, profit-lock, journal (~350 lines) |
| `tests/test_trading_pipeline.py` | Split from `test_trading.py` — pipeline + context + budget escalation (~370 lines) |
| `tests/test_trading_budget.py` | Split from `test_trading.py` — risk manager, gap guard, fill, auto-execute (~395 lines) |

### Deleted
| File | Reason |
|---|---|
| `watch_list_agent.py` | Moved to `agents/watch_list.py` |
| `volatility_screener.py` | Moved to `agents/volatility_screener.py` |
| `learning_agent.py` | Pure re-export shim; only caller updated to import from `learning.agent` directly |
| `tests/test_trading.py` | Replaced by the four split files above |

### Modified (ET import — all drop local `ET = ZoneInfo(...)` and import from `utils.constants`)
`daily_report.py`, `economic_calendar.py`, `macro_regime.py`, `main.py`, `swing_manager.py`,
`research/agent.py`, `utils/catchup.py`, `trading_agent.py`, `earnings_calendar.py`,
`market_data.py`, `backtest.py`, `scheduler_jobs.py`, `utils/bot_health.py`,
`utils/startup.py`, `utils/market_monitor.py`, `macro/yahoo_fetcher.py`, `macro/intraday_context.py`,
`macro/fred_cache.py`, `trading/journal.py`, `trading/budget_guard.py`, `trading/claude_caller.py`,
`trading/pipeline.py`, `trading/closer.py`, `trading/risk_manager.py`, `trading/eod_holds.py`,
`trading/executor.py`, `tests/test_core.py`, `tests/test_trading.py` (deleted), `tests/test_swing.py`,
`tests/test_market.py`, `tests/test_pipeline.py`, `tests/test_earnings.py`, `tests/test_foundation.py`

### Modified (Yahoo HTTP — use `utils.http.yahoo_get`)
`market_data.py`, `macro/yahoo_fetcher.py`, `earnings_calendar.py`

### Modified (io_utils bypass — replace raw json calls with `load_json` / `save_json`)
`daily_report.py`, `earnings_calendar.py`, `economic_calendar.py`, `backtest.py`

### Modified (import path updates for moved agents)
`scheduler_jobs.py`

---

## Task 1: Create `utils/constants.py`

**Files:**
- Create: `utils/constants.py`
- Modify: `utils/__init__.py`

- [ ] **Step 1: Write `utils/constants.py`**

```python
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
```

- [ ] **Step 2: Verify it imports cleanly**

```bash
cd /Users/rajeshkumar/Documents/projects/trading-bot
.venv/bin/python -c "from utils.constants import ET; print(ET)"
```
Expected: `America/New_York`

- [ ] **Step 3: Commit**

```bash
git add utils/constants.py
git commit -m "feat: add utils/constants.py — single source for ET timezone"
```

---

## Task 2: Create `utils/http.py`

This replaces the three identical Yahoo 429-backoff retry loops in `market_data.py`, `macro/yahoo_fetcher.py`, and `earnings_calendar.py`. Uses a module-level `requests.Session` for TCP connection reuse.

**Files:**
- Create: `utils/http.py`

- [ ] **Step 1: Write `utils/http.py`**

```python
"""Shared HTTP utilities for Yahoo Finance requests."""
import time

import requests

YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0"}
_session = requests.Session()
_session.headers.update(YAHOO_HEADERS)


def yahoo_get(url: str, timeout: int = 10) -> dict:
    """GET a Yahoo Finance URL; retries up to 3× with exponential backoff on 429."""
    for attempt in range(3):
        try:
            resp = _session.get(url, timeout=timeout)
            if resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1)
    return {}
```

- [ ] **Step 2: Verify import**

```bash
.venv/bin/python -c "from utils.http import yahoo_get; print('ok')"
```
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add utils/http.py
git commit -m "feat: add utils/http.py — shared Yahoo session + 429-backoff retry"
```

---

## Task 3: Replace ET definitions — all source files

Replace `ET = ZoneInfo("America/New_York")` and its `from zoneinfo import ZoneInfo` import with `from utils.constants import ET` in every **non-test** source file. Run the exact substitutions below for each file.

**Files (non-test):** `daily_report.py`, `economic_calendar.py`, `macro_regime.py`, `main.py`, `swing_manager.py`, `research/agent.py`, `utils/catchup.py`, `trading_agent.py`, `earnings_calendar.py`, `market_data.py`, `backtest.py`, `scheduler_jobs.py`, `utils/bot_health.py`, `utils/startup.py`, `utils/market_monitor.py`, `macro/yahoo_fetcher.py`, `macro/intraday_context.py`, `macro/fred_cache.py`, `trading/journal.py`, `trading/budget_guard.py`, `trading/claude_caller.py`, `trading/pipeline.py`, `trading/closer.py`, `trading/risk_manager.py`, `trading/eod_holds.py`, `trading/executor.py`

- [ ] **Step 1: For each file, apply two edits**

**Edit A** — remove `ZoneInfo` from the `from zoneinfo import ZoneInfo` line.

If the file imports **only** `ZoneInfo`:
```python
# old
from zoneinfo import ZoneInfo
```
→ delete the line entirely.

If the file imports other things alongside `ZoneInfo` (e.g. `from zoneinfo import ZoneInfo, ZoneInfoNotFoundError`):
→ remove only `ZoneInfo` from the import, leave the rest.

**Edit B** — replace the module-level constant:
```python
# old
ET = ZoneInfo("America/New_York")
```
→ replace with:
```python
from utils.constants import ET
```

- [ ] **Step 2: Run import smoke test across all modified files**

```bash
.venv/bin/python -c "
import daily_report, economic_calendar, macro_regime, main, swing_manager
import market_data, backtest, scheduler_jobs
from research import agent
from utils import catchup, bot_health, market_monitor
from macro import yahoo_fetcher, intraday_context, fred_cache
from trading import journal, budget_guard, claude_caller, pipeline
from trading import closer, risk_manager, eod_holds, executor
import trading_agent
print('all imports OK')
"
```
Expected: `all imports OK`

- [ ] **Step 3: Run full test suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -5
```
Expected: all tests pass (same count as before).

- [ ] **Step 4: Commit**

```bash
git add -u
git commit -m "refactor: replace 26 local ET definitions with utils.constants.ET"
```

---

## Task 4: Replace ET definitions — test files

Same substitution as Task 3 but for test files. Test files use `ET` in assertions and fixtures.

**Files:** `tests/test_core.py`, `tests/test_swing.py`, `tests/test_market.py`, `tests/test_pipeline.py`, `tests/test_earnings.py`, `tests/test_foundation.py`, `test_agents.py` (root-level legacy file)

- [ ] **Step 1: Apply same two edits as Task 3 to each test file**

Remove `from zoneinfo import ZoneInfo` (if sole import) and replace `ET = ZoneInfo("America/New_York")` with `from utils.constants import ET`.

- [ ] **Step 2: Run tests**

```bash
.venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -5
```
Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add -u
git commit -m "refactor: replace ET definitions in test files with utils.constants.ET"
```

---

## Task 5: Migrate Yahoo callers to `utils.http.yahoo_get`

Replace the three independent `for attempt in range(3): requests.get(...)` retry loops with `yahoo_get`.

**Files:**
- Modify: `market_data.py`
- Modify: `macro/yahoo_fetcher.py`
- Modify: `earnings_calendar.py`

### `market_data.py`

- [ ] **Step 1: Update imports — add `yahoo_get`, remove `requests` and `_YAHOO_HEADERS`**

Remove:
```python
import requests
...
_YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0"}
```

Add to imports:
```python
from utils.http import yahoo_get
```

- [ ] **Step 2: Replace `_yahoo_bars` function body**

Old function (lines 18–49):
```python
def _yahoo_bars(symbol: str, interval: str, period: str) -> pd.DataFrame:
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?interval={interval}&range={period}"
    )
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=_YAHOO_HEADERS, timeout=10)
            if resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            result = resp.json()["chart"]["result"][0]
            timestamps = result["timestamp"]
            q = result["indicators"]["quote"][0]
            df = pd.DataFrame(
                {
                    "open":   [float(v) if v is not None else float("nan") for v in q["open"]],
                    "high":   [float(v) if v is not None else float("nan") for v in q["high"]],
                    "low":    [float(v) if v is not None else float("nan") for v in q["low"]],
                    "close":  [float(v) if v is not None else float("nan") for v in q["close"]],
                    "volume": [float(v) if v is not None else 0.0 for v in q["volume"]],
                },
                index=pd.to_datetime(timestamps, unit="s", utc=True).tz_convert(ET),
            )
            df.index.name = "timestamp"
            return df.dropna(subset=["close"])
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1)
    return pd.DataFrame()
```

New function:
```python
def _yahoo_bars(symbol: str, interval: str, period: str) -> pd.DataFrame:
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?interval={interval}&range={period}"
    )
    data = yahoo_get(url)
    result = data["chart"]["result"][0]
    timestamps = result["timestamp"]
    q = result["indicators"]["quote"][0]
    df = pd.DataFrame(
        {
            "open":   [float(v) if v is not None else float("nan") for v in q["open"]],
            "high":   [float(v) if v is not None else float("nan") for v in q["high"]],
            "low":    [float(v) if v is not None else float("nan") for v in q["low"]],
            "close":  [float(v) if v is not None else float("nan") for v in q["close"]],
            "volume": [float(v) if v is not None else 0.0 for v in q["volume"]],
        },
        index=pd.to_datetime(timestamps, unit="s", utc=True).tz_convert(ET),
    )
    df.index.name = "timestamp"
    return df.dropna(subset=["close"])
```

Also remove `import time` if it is no longer used in this file (check the rest of the file first).

### `macro/yahoo_fetcher.py`

- [ ] **Step 3: Update imports**

Remove:
```python
import time
import requests
...
_YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0"}
```

Add:
```python
from utils.http import yahoo_get
```

- [ ] **Step 4: Replace `yahoo_closes` function body**

Old:
```python
def yahoo_closes(symbol: str, days: int = 120) -> list[float]:
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?interval=1d&range={days}d"
    )
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=_YAHOO_HEADERS, timeout=10)
            if resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            quotes = resp.json()["chart"]["result"][0]["indicators"]["quote"][0]
            return [c for c in quotes["close"] if c is not None]
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1)
    return []
```

New:
```python
def yahoo_closes(symbol: str, days: int = 120) -> list[float]:
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?interval=1d&range={days}d"
    )
    data = yahoo_get(url)
    quotes = data["chart"]["result"][0]["indicators"]["quote"][0]
    return [c for c in quotes["close"] if c is not None]
```

### `earnings_calendar.py`

- [ ] **Step 5: Update imports**

Remove:
```python
import requests
...
_YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0"}
```

Add:
```python
from utils.http import yahoo_get
```

- [ ] **Step 6: Replace `_yahoo_quote_summary` function body**

Old:
```python
def _yahoo_quote_summary(symbol: str, modules: str) -> dict:
    url = (
        f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
        f"?modules={modules}"
    )
    resp = requests.get(url, headers=_YAHOO_HEADERS, timeout=10)
    resp.raise_for_status()
    result = resp.json().get("quoteSummary", {}).get("result", [])
    return result[0] if result else {}
```

New:
```python
def _yahoo_quote_summary(symbol: str, modules: str) -> dict:
    url = (
        f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
        f"?modules={modules}"
    )
    data = yahoo_get(url)
    result = data.get("quoteSummary", {}).get("result", [])
    return result[0] if result else {}
```

- [ ] **Step 7: Run tests**

```bash
.venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -5
```

- [ ] **Step 8: Commit**

```bash
git add market_data.py macro/yahoo_fetcher.py earnings_calendar.py utils/http.py
git commit -m "refactor: replace 3 duplicate Yahoo retry loops with utils.http.yahoo_get"
```

---

## Task 6: Fix `json.load/dump` bypasses in `daily_report.py`

Replace all raw `json.loads(file.read_text())` / `file.write_text(json.dumps(...))` with `load_json` / `save_json` from `utils.io_utils`.

**Files:**
- Modify: `daily_report.py`

- [ ] **Step 1: Update imports**

Remove `import json` (it will no longer be needed after all replacements).

Add:
```python
from utils.io_utils import load_json, save_json
```

- [ ] **Step 2: Replace `_already_sent_today`**

Old:
```python
def _already_sent_today() -> bool:
    try:
        data = json.loads(DAILY_REPORT_SENT_FILE.read_text())
        return data.get("date") == datetime.now(ET).date().isoformat()
    except (FileNotFoundError, json.JSONDecodeError):
        return False
```

New:
```python
def _already_sent_today() -> bool:
    data = load_json(DAILY_REPORT_SENT_FILE, {})
    return data.get("date") == datetime.now(ET).date().isoformat()
```

- [ ] **Step 3: Replace `_mark_sent`**

Old:
```python
def _mark_sent() -> None:
    DAILY_REPORT_SENT_FILE.write_text(
        json.dumps({"date": datetime.now(ET).date().isoformat()})
    )
```

New:
```python
def _mark_sent() -> None:
    save_json(DAILY_REPORT_SENT_FILE, {"date": datetime.now(ET).date().isoformat()})
```

- [ ] **Step 4: Replace the three `json.loads(...read_text())` calls in `send_daily_report`**

Old (trades):
```python
    trades = []
    try:
        trades = json.loads(config.TRADE_JOURNAL_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        pass
```

New:
```python
    trades = load_json(config.TRADE_JOURNAL_FILE, [])
```

Old (regime):
```python
    regime = "UNKNOWN"
    try:
        regime = json.loads(config.MACRO_REGIME_FILE.read_text()).get("regime", "UNKNOWN")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
```

New:
```python
    regime = load_json(config.MACRO_REGIME_FILE, {}).get("regime", "UNKNOWN")
```

Old (weights):
```python
    weights = {}
    try:
        weights = json.loads(config.INDICATOR_WEIGHTS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        pass
```

New:
```python
    weights = load_json(config.INDICATOR_WEIGHTS_FILE, {})
```

- [ ] **Step 5: Run tests**

```bash
.venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -5
```

- [ ] **Step 6: Commit**

```bash
git add daily_report.py
git commit -m "fix: daily_report.py — replace raw json.load/dump with io_utils"
```

---

## Task 7: Fix `json.load/dump` bypasses in `earnings_calendar.py`

**Files:**
- Modify: `earnings_calendar.py`

- [ ] **Step 1: Add `save_json` to imports**

`earnings_calendar.py` already imports nothing from `utils.io_utils` (io_utils is not imported). Add:
```python
from utils.io_utils import load_json, save_json
```

- [ ] **Step 2: Replace `refresh_earnings_calendar` write**

Old (lines 157–160):
```python
    payload = {"date": today, "symbols": cache}
    with open(config.EARNINGS_CALENDAR_FILE, "w") as f:
        json.dump(payload, f, indent=2)
    logger.info(f"Earnings calendar refreshed for {len(symbols)} symbols.")
```

New:
```python
    save_json(config.EARNINGS_CALENDAR_FILE, {"date": today, "symbols": cache})
    logger.info(f"Earnings calendar refreshed for {len(symbols)} symbols.")
```

- [ ] **Step 3: Replace `_load_cache` reads**

Old (lines 163–175):
```python
def _load_cache() -> dict:
    try:
        if not config.EARNINGS_CALENDAR_FILE.exists():
            return {}
        with open(config.EARNINGS_CALENDAR_FILE) as f:
            data = json.load(f)
        today = datetime.now(ET).strftime("%Y-%m-%d")
        if data.get("date") != today:
            return {}
        return data.get("symbols", {})
    except Exception:
        return {}
```

New:
```python
def _load_cache() -> dict:
    data = load_json(config.EARNINGS_CALENDAR_FILE, {})
    today = datetime.now(ET).strftime("%Y-%m-%d")
    if data.get("date") != today:
        return {}
    return data.get("symbols", {})
```

- [ ] **Step 4: Remove `import json` if no longer used**

Search for any remaining `json.` usage in the file. If none remain, remove `import json`.

- [ ] **Step 5: Run tests**

```bash
.venv/bin/python -m pytest tests/test_earnings.py -m unit -q 2>&1 | tail -5
```

- [ ] **Step 6: Commit**

```bash
git add earnings_calendar.py
git commit -m "fix: earnings_calendar.py — replace raw json.load/dump with io_utils"
```

---

## Task 8: Fix `json.load/dump` bypasses in `economic_calendar.py`

**Files:**
- Modify: `economic_calendar.py`

- [ ] **Step 1: Add `load_json`, `save_json` to imports**

Add:
```python
from utils.io_utils import load_json, save_json
```

- [ ] **Step 2: Replace `refresh_economic_calendar` write**

Old (lines 140–141):
```python
    with open(config.ECONOMIC_CALENDAR_FILE, "w") as f:
        json.dump(payload, f, indent=2)
```

New:
```python
    save_json(config.ECONOMIC_CALENDAR_FILE, payload)
```

- [ ] **Step 3: Replace `is_post_major_event_day` read**

Old (lines 149–152):
```python
    try:
        with open(config.ECONOMIC_CALENDAR_FILE) as f:
            data = json.load(f)
        prior = data.get("prior_events", [])
    except Exception:
        return False, ""
```

New:
```python
    data = load_json(config.ECONOMIC_CALENDAR_FILE, {})
    prior = data.get("prior_events", [])
```

- [ ] **Step 4: Replace `_load_todays_events` read**

Old (lines 165–174):
```python
def _load_todays_events() -> list[dict]:
    try:
        if not config.ECONOMIC_CALENDAR_FILE.exists():
            return []
        with open(config.ECONOMIC_CALENDAR_FILE) as f:
            data = json.load(f)
        today_str = datetime.now(ET).strftime("%Y-%m-%d")
        if data.get("date") != today_str:
            return []
        return data.get("events", [])
    except Exception:
        return []
```

New:
```python
def _load_todays_events() -> list[dict]:
    data = load_json(config.ECONOMIC_CALENDAR_FILE, {})
    today_str = datetime.now(ET).strftime("%Y-%m-%d")
    if data.get("date") != today_str:
        return []
    return data.get("events", [])
```

- [ ] **Step 5: Remove `import json` if no longer used, and the inline `from utils.io_utils import load_json` that was already inside `refresh_economic_calendar` (lines 128–129 — it was importing conditionally inside the function body)**

Check: `economic_calendar.py` had `from utils.io_utils import load_json` inside `refresh_economic_calendar`. Remove that inline import and keep only the top-level import added in Step 1.

- [ ] **Step 6: Run tests**

```bash
.venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -5
```

- [ ] **Step 7: Commit**

```bash
git add economic_calendar.py
git commit -m "fix: economic_calendar.py — replace raw json.load/dump with io_utils"
```

---

## Task 9: Fix `json.dump` bypass in `backtest.py`

`backtest.py` uses `json.dump` to write the equity curve (line ~545). Replace with `save_json`.

**Files:**
- Modify: `backtest.py`

- [ ] **Step 1: Find the `json.dump` call**

```bash
grep -n "json.dump\|json.load" backtest.py
```

Note: `backtest.py` also uses `json.loads` inline for string parsing inside Claude response handling — only replace the **file write** calls, not the in-memory parsing.

- [ ] **Step 2: Add `save_json` import**

Add at top of file:
```python
from utils.io_utils import save_json
```

- [ ] **Step 3: Replace file-write `json.dump` calls**

For each `with open(..., "w") as f: json.dump(data, f, indent=2)` pattern:

```python
# old
with open(path, "w") as f:
    json.dump(data, f, indent=2)
```

```python
# new
save_json(path, data)
```

Do **not** replace `json.dumps(...)` calls used for building strings (e.g. for Claude prompts).

- [ ] **Step 4: Remove `import json` only if no json.* calls remain**

Check with:
```bash
grep -n "json\." backtest.py
```
If `json.dumps` or `json.loads` calls remain (for string building), keep `import json`.

- [ ] **Step 5: Run backtest import check**

```bash
.venv/bin/python -c "import backtest; print('ok')"
```

- [ ] **Step 6: Commit**

```bash
git add backtest.py
git commit -m "fix: backtest.py — replace json.dump file writes with io_utils.save_json"
```

---

## Task 10: Create `agents/` package and move `watch_list_agent.py`

**Files:**
- Create: `agents/__init__.py`
- Create: `agents/watch_list.py` (content from `watch_list_agent.py`)
- Delete: `watch_list_agent.py`
- Modify: `scheduler_jobs.py`

- [ ] **Step 1: Create `agents/__init__.py`**

```python
from agents.watch_list import run_watch_list_agent
from agents.volatility_screener import run_volatility_screener

__all__ = ["run_watch_list_agent", "run_volatility_screener"]
```

- [ ] **Step 2: Create `agents/watch_list.py`**

Copy the full content of `watch_list_agent.py` to `agents/watch_list.py`. No other changes — the module's internal logic is identical.

- [ ] **Step 3: Update `scheduler_jobs.py` — two import sites**

Old (job_volatility_screener, line ~83):
```python
    from volatility_screener import run_volatility_screener
```

New:
```python
    from agents.volatility_screener import run_volatility_screener
```

Old (job_watch_list, line ~92):
```python
        from watch_list_agent import run_watch_list_agent
```
and (job_research, line ~103):
```python
            from watch_list_agent import _load_universe
```

New:
```python
        from agents.watch_list import run_watch_list_agent
```
and:
```python
            from agents.watch_list import _load_universe
```

- [ ] **Step 4: Delete `watch_list_agent.py`**

```bash
git rm watch_list_agent.py
```

- [ ] **Step 5: Verify import**

```bash
.venv/bin/python -c "from agents.watch_list import run_watch_list_agent; print('ok')"
```

- [ ] **Step 6: Run tests**

```bash
.venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -5
```

- [ ] **Step 7: Commit**

```bash
git add agents/ scheduler_jobs.py
git commit -m "refactor: move watch_list_agent.py → agents/watch_list.py"
```

---

## Task 11: Move `volatility_screener.py` into `agents/`

**Files:**
- Create: `agents/volatility_screener.py` (content from `volatility_screener.py`)
- Delete: `volatility_screener.py`

- [ ] **Step 1: Create `agents/volatility_screener.py`**

Copy the full content of `volatility_screener.py` to `agents/volatility_screener.py`. No other changes.

- [ ] **Step 2: Delete `volatility_screener.py`**

```bash
git rm volatility_screener.py
```

- [ ] **Step 3: Verify import (scheduler_jobs.py was already updated in Task 10)**

```bash
.venv/bin/python -c "from agents.volatility_screener import run_volatility_screener; print('ok')"
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -5
```

- [ ] **Step 5: Commit**

```bash
git add agents/volatility_screener.py volatility_screener.py scheduler_jobs.py
git commit -m "refactor: move volatility_screener.py → agents/volatility_screener.py"
```

---

## Task 12: Remove `learning_agent.py` shim

The only scheduler caller (`scheduler_jobs.py:job_learning`) imports `run_learning_agent` from `learning_agent`. Update it to import from `learning.agent` directly, then delete the shim.

**Files:**
- Modify: `scheduler_jobs.py`
- Delete: `learning_agent.py`

- [ ] **Step 1: Update `scheduler_jobs.py`**

Old (line ~182):
```python
    from learning_agent import run_learning_agent
```

New:
```python
    from learning.agent import run_learning_agent
```

- [ ] **Step 2: Check for any other callers of `learning_agent`**

```bash
grep -rn "from learning_agent\|import learning_agent" --include="*.py" .
```
Expected: zero results (only `scheduler_jobs.py` was calling it, now updated).

- [ ] **Step 3: Delete `learning_agent.py`**

```bash
git rm learning_agent.py
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/python -m pytest tests/test_learning.py -m unit -q 2>&1 | tail -5
```

- [ ] **Step 5: Commit**

```bash
git add scheduler_jobs.py learning_agent.py
git commit -m "refactor: delete learning_agent.py shim — scheduler imports from learning.agent directly"
```

---

## Task 13: Split `tests/test_trading.py` (1371 lines → 4 files)

Split by domain. Each resulting file must be under 1000 lines.

**Files:**
- Create: `tests/test_trading_modes.py` (classes at lines 19–259: TestGetActiveMode, TestGetSlTpForModeAndRegime, TestComputePositionSizeForMode, TestBuildMarketContextBlock)
- Create: `tests/test_trading_lifecycle.py` (classes at lines 260–609: TestStartupExitAlerts, TestForceCloseAll, TestProfitLock, TestJournalLocks, TestFlushProfitLock)
- Create: `tests/test_trading_pipeline.py` (classes at lines 610–977: TestTradingPipeline, TestPipelinePerCycleCap, TestContextSplit, TestAgentBudgetRecording, TestBudgetEscalationNotify)
- Create: `tests/test_trading_budget.py` (classes at lines 978–1371: TestRiskManagerBudget, TestRiskManagerGapGuard, TestThesisBreakHeuristic, TestFillCorrection, TestAutoExecuteSoftFlagGuard)
- Delete: `tests/test_trading.py`

Each new file needs:
1. The shared header (module docstring + `import json`, `import os`, `from datetime import datetime`, `import pytest`, `from utils.constants import ET`).
2. Only the test classes that belong to it (copy verbatim from `test_trading.py`).

- [ ] **Step 1: Create `tests/test_trading_modes.py`**

Header:
```python
"""
Mode routing, SL/TP table, position sizing, and context-block unit tests.
Run with: pytest tests/test_trading_modes.py -v -m unit
"""
import json
import os
from datetime import datetime

import pytest

from utils.constants import ET
```

Then paste classes: `TestGetActiveMode`, `TestGetSlTpForModeAndRegime`, `TestComputePositionSizeForMode`, `TestBuildMarketContextBlock` (verbatim from `test_trading.py` lines 19–259).

- [ ] **Step 2: Create `tests/test_trading_lifecycle.py`**

Header:
```python
"""
Startup alerts, force-close, profit-lock, and journal lock unit tests.
Run with: pytest tests/test_trading_lifecycle.py -v -m unit
"""
import json
import os
from datetime import datetime

import pytest

from utils.constants import ET
```

Then paste classes: `TestStartupExitAlerts`, `TestForceCloseAll`, `TestProfitLock`, `TestJournalLocks`, `TestFlushProfitLock` (verbatim from `test_trading.py` lines 260–609).

- [ ] **Step 3: Create `tests/test_trading_pipeline.py`**

Header:
```python
"""
Trading pipeline, cycle context, and budget escalation unit tests.
Run with: pytest tests/test_trading_pipeline.py -v -m unit
"""
import json
import os
from datetime import datetime

import pytest

from utils.constants import ET
```

Then paste classes: `TestTradingPipeline`, `TestPipelinePerCycleCap`, `TestContextSplit`, `TestAgentBudgetRecording`, `TestBudgetEscalationNotify` (verbatim from `test_trading.py` lines 609–977).

- [ ] **Step 4: Create `tests/test_trading_budget.py`**

Header:
```python
"""
Risk manager budget, gap guard, thesis-break, fill correction, and auto-execute unit tests.
Run with: pytest tests/test_trading_budget.py -v -m unit
"""
import json
import os
from datetime import datetime

import pytest

from utils.constants import ET
```

Then paste classes: `TestRiskManagerBudget`, `TestRiskManagerGapGuard`, `TestThesisBreakHeuristic`, `TestFillCorrection`, `TestAutoExecuteSoftFlagGuard` (verbatim from `test_trading.py` lines 978–1371).

- [ ] **Step 5: Verify all 4 new files pass**

```bash
.venv/bin/python -m pytest tests/test_trading_modes.py tests/test_trading_lifecycle.py tests/test_trading_pipeline.py tests/test_trading_budget.py -v -m unit 2>&1 | tail -20
```
Expected: same test count as original `test_trading.py`.

- [ ] **Step 6: Verify no test is missing (count must match)**

```bash
# Count from original (should be captured before deletion):
grep -c "def test_" tests/test_trading.py
# Count from new files combined:
grep -c "def test_" tests/test_trading_modes.py tests/test_trading_lifecycle.py tests/test_trading_pipeline.py tests/test_trading_budget.py | grep "total"
```
Expected: same number.

- [ ] **Step 7: Delete original**

```bash
git rm tests/test_trading.py
```

- [ ] **Step 8: Full suite run**

```bash
.venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -5
```
Expected: all pass, no regressions.

- [ ] **Step 9: Commit**

```bash
git add tests/
git commit -m "refactor: split test_trading.py (1371 lines) into 4 domain-scoped files"
```

---

## Final Verification

- [ ] **Run full test suite one last time**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```
Expected: same pass count as before starting this plan.

- [ ] **Verify no stray ET definitions remain in source files**

```bash
grep -rn "^ET = ZoneInfo" --include="*.py" . | grep -v ".venv"
```
Expected: zero results.

- [ ] **Verify no stray YAHOO_HEADERS remain**

```bash
grep -rn "_YAHOO_HEADERS\s*=" --include="*.py" . | grep -v ".venv\|utils/http"
```
Expected: zero results (only `utils/http.py` defines it).

- [ ] **Verify no `json.load/dump` bypass remains in tracked files**

```bash
grep -rn "json\.dump\b\|json\.load(" --include="*.py" . \
  | grep -v ".venv\|test_\|tests/\|backtest\|learning/\|watch_list\|research\|trading/claude_caller\|#"
```
Expected: zero results for file-write patterns (remaining `json.dumps` for string building are OK).

- [ ] **Commit summary tag**

```bash
git tag refactor/cleanup-2026-05-14
```
