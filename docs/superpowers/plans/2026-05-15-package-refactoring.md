# Package Refactoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move 8 root-level Python modules into domain packages, 15 JSON state files into `data/state/`, log files into `logs/`, and delete confirmed dead code — with zero logic changes and all 664 tests staying green.

**Architecture:** Move files one module at a time with `git mv`; fix internal imports in the moved file immediately; run a per-module sed script to update all callers; run tests after each module. Config path constants are updated in a single batch (Task 3) before any file moves. The import migration script (Task 2) is reused for every module move.

**Tech Stack:** Python 3, pytest, git, pathlib, macOS/Linux sed (via Python subprocess for portability)

**Baseline:** `664 passed` — verify before starting: `.venv/bin/python -m pytest tests/ -m unit -q --tb=no`

---

## File Structure

### New packages (create `__init__.py`):
- `broker/__init__.py` — broker I/O
- `broker/alpaca_client.py` — moved from root
- `calendars/__init__.py` — earnings/calendar domain
- `calendars/earnings.py` — moved from `earnings_calendar.py`
- `data/__init__.py` — market data
- `data/market_data.py` — moved from root; `get_quote()` deleted
- `notifications/__init__.py` — all outbound notifications
- `notifications/telegram.py` — moved from `telegram_notify.py`
- `notifications/daily_report.py` — moved from `daily_report.py`

### Existing packages (add files):
- `macro/economic_calendar.py` — moved from root
- `macro/regime.py` — moved from `macro_regime.py`
- `trading/swing_manager.py` — moved from root

### Config / infra:
- `config.py` — add `STATE_DIR`, update 15 JSON path constants
- `main.py` — `STATE_DIR.mkdir()` + `logs/` mkdir at startup; update RotatingFileHandler path
- `start.sh` — `LOG_FILE` path updated
- `trading_bot_start.plist` — StandardOutPath/StandardErrorPath updated

### State directory:
- `data/state/` — plain directory (no `__init__.py`); holds all 15 JSON runtime files

### Migration tool (temporary, deleted after use):
- `tools/migrate_imports.py` — rewrites import lines across the codebase for a given module rename

---

## Task 1: Scaffold new packages and directories

**Files:**
- Create: `broker/__init__.py`
- Create: `calendars/__init__.py`
- Create: `data/__init__.py`
- Create: `notifications/__init__.py`
- Create: `data/state/` directory
- Create: `logs/` directory
- Create: `tools/__init__.py` (empty, for migrate script)

- [ ] **Step 1: Create all new directories and `__init__.py` files**

```bash
mkdir -p broker calendars data/state logs notifications tools
touch broker/__init__.py calendars/__init__.py data/__init__.py notifications/__init__.py tools/__init__.py
```

- [ ] **Step 2: Verify directories exist**

```bash
ls broker/ calendars/ data/ notifications/ logs/ tools/
```
Expected: each directory exists and shows `__init__.py`.

- [ ] **Step 3: Commit scaffolding**

```bash
git add broker/__init__.py calendars/__init__.py data/__init__.py notifications/__init__.py tools/__init__.py
git commit -m "chore: scaffold new domain packages — broker, calendars, data, notifications"
```

---

## Task 2: Write the import migration script

**Files:**
- Create: `tools/migrate_imports.py`

This script is run once per module move. It rewrites `import X` and `from X import` statements across all `.py` files.

- [ ] **Step 1: Write the script**

```python
#!/usr/bin/env python3
"""
Usage: python tools/migrate_imports.py <old_module> <new_dotted_path>

Example:
  python tools/migrate_imports.py telegram_notify notifications.telegram
  python tools/migrate_imports.py macro_regime macro.regime
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
SKIP = {".venv", "__pycache__", ".git", "docs", "tools"}


def _py_files():
    for p in ROOT.rglob("*.py"):
        if not any(s in p.parts for s in SKIP):
            yield p


def migrate(old: str, new: str) -> None:
    alias = old  # preserve original name as alias so call sites need no change
    changed = []
    for path in _py_files():
        src = path.read_text()
        # from <old> import X  →  from <new> import X
        out = re.sub(
            rf"^(from\s+){re.escape(old)}(\s+import\b)",
            rf"\g<1>{new}\2",
            src,
            flags=re.MULTILINE,
        )
        # import <old> as Y  →  import <new> as Y  (preserve explicit alias)
        out = re.sub(
            rf"^(import\s+){re.escape(old)}(\s+as\s+\w+)",
            rf"\g<1>{new}\2",
            out,
            flags=re.MULTILINE,
        )
        # import <old>$  →  import <new> as <alias>
        out = re.sub(
            rf"^(import\s+){re.escape(old)}\s*$",
            rf"\g<1>{new} as {alias}",
            out,
            flags=re.MULTILINE,
        )
        if out != src:
            path.write_text(out)
            changed.append(str(path.relative_to(ROOT)))

    print(f"Updated {len(changed)} files:")
    for f in sorted(changed):
        print(f"  {f}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    migrate(sys.argv[1], sys.argv[2])
```

- [ ] **Step 2: Verify script is syntactically valid**

```bash
.venv/bin/python -c "import tools.migrate_imports; print('ok')"
```
Expected: `ok`

- [ ] **Step 3: Commit the migration tool**

```bash
git add tools/migrate_imports.py
git commit -m "chore: add import migration script for package refactor"
```

---

## Task 3: Update config.py — STATE_DIR and all JSON paths

**Files:**
- Modify: `config.py`

- [ ] **Step 1: Add STATE_DIR and update all JSON path constants**

In `config.py`, directly after the `BASE_DIR` line (line 7), add:
```python
STATE_DIR = BASE_DIR / "data" / "state"
```

Then change every `BASE_DIR / "*.json"` to `STATE_DIR / "*.json"`. The full set of changes:

```python
# Before → After
TRADE_JOURNAL_FILE = BASE_DIR / "trade_journal.json"       → STATE_DIR / "trade_journal.json"
INDICATOR_WEIGHTS_FILE = BASE_DIR / "indicator_weights.json" → STATE_DIR / "indicator_weights.json"
RESEARCH_REPORT_FILE = BASE_DIR / "research_report.json"   → STATE_DIR / "research_report.json"
OPEN_POSITIONS_FILE = BASE_DIR / "open_positions.json"     → STATE_DIR / "open_positions.json"
DAILY_CLOSED_FILE   = BASE_DIR / "daily_closed.json"       → STATE_DIR / "daily_closed.json"
TRADING_PARAMS_FILE = BASE_DIR / "trading_params.json"     → STATE_DIR / "trading_params.json"
DAILY_REPORT_SENT_FILE = BASE_DIR / "daily_report_sent.json" → STATE_DIR / "daily_report_sent.json"
MACRO_REGIME_FILE      = BASE_DIR / "macro_regime.json"    → STATE_DIR / "macro_regime.json"
FRED_CACHE_FILE        = BASE_DIR / "fred_cache.json"      → STATE_DIR / "fred_cache.json"
ECONOMIC_CALENDAR_FILE = BASE_DIR / "economic_calendar.json" → STATE_DIR / "economic_calendar.json"
API_BUDGET_FILE        = BASE_DIR / "api_budget.json"      → STATE_DIR / "api_budget.json"
SWING_POSITIONS_FILE   = BASE_DIR / "swing_positions.json" → STATE_DIR / "swing_positions.json"
EARNINGS_CALENDAR_FILE = BASE_DIR / "earnings_calendar.json" → STATE_DIR / "earnings_calendar.json"
BOT_HEALTH_FILE        = BASE_DIR / "bot_health.json"      → STATE_DIR / "bot_health.json"
INTRADAY_NEWS_CACHE_FILE = BASE_DIR / "intraday_news_cache.json" → STATE_DIR / "intraday_news_cache.json"
```

- [ ] **Step 2: Verify config imports cleanly**

```bash
.venv/bin/python -c "import config; print(config.STATE_DIR)"
```
Expected: `.../trading-bot/data/state`

- [ ] **Step 3: Run tests — expect failures for missing state dir (that's fine for now)**

```bash
.venv/bin/python -m pytest tests/ -m unit -q --tb=no 2>&1 | tail -3
```
Expected: still 664 passed (tests use `tmp_path` for file I/O, not real state dir).

- [ ] **Step 4: Commit**

```bash
git add config.py
git commit -m "refactor: add STATE_DIR, route all JSON paths through data/state/"
```

---

## Task 4: Update main.py and start.sh for new directories

**Files:**
- Modify: `main.py` (STATE_DIR mkdir, logs mkdir, RotatingFileHandler path)
- Modify: `start.sh`
- Modify: `trading_bot_start.plist`

- [ ] **Step 1: Update main.py**

At the top of `main.py`, after `import config` (around line 10), add directory creation before the logging setup:

```python
# Ensure runtime directories exist before any file I/O
config.STATE_DIR.mkdir(parents=True, exist_ok=True)
(config.BASE_DIR / "logs").mkdir(exist_ok=True)
```

Change the RotatingFileHandler line from:
```python
_file_handler = logging.handlers.RotatingFileHandler(
    config.BASE_DIR / "trading_bot.log",
```
to:
```python
_file_handler = logging.handlers.RotatingFileHandler(
    config.BASE_DIR / "logs" / "trading_bot.log",
```

- [ ] **Step 2: Update start.sh**

Change line 7 from:
```bash
LOG_FILE="$SCRIPT_DIR/trading_bot.log"
```
to:
```bash
LOG_FILE="$SCRIPT_DIR/logs/trading_bot.log"
```

- [ ] **Step 3: Update trading_bot_start.plist**

Change both StandardOutPath and StandardErrorPath from:
```xml
<string>PLACEHOLDER_PROJECT_DIR/launchd_trading_bot.log</string>
```
to:
```xml
<string>PLACEHOLDER_PROJECT_DIR/logs/launchd_trading_bot.log</string>
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/python -m pytest tests/ -m unit -q --tb=no 2>&1 | tail -3
```
Expected: 664 passed

- [ ] **Step 5: Commit**

```bash
git add main.py start.sh trading_bot_start.plist
git commit -m "refactor: move logs to logs/, create state/logs dirs at startup"
```

---

## Task 5: Move market_data.py → data/market_data.py (+ delete dead code)

**Files:**
- Move: `market_data.py` → `data/market_data.py`
- Delete: `get_quote()` function (zero callers confirmed)
- Callers to update: `alpaca_client.py`, `earnings_calendar.py`, `trading/executor.py`, `macro/intraday_context.py`, `utils/market_monitor.py`, `tests/test_market.py`

- [ ] **Step 1: Move the file**

```bash
git mv market_data.py data/market_data.py
```

- [ ] **Step 2: Delete `get_quote()` from `data/market_data.py`**

Open `data/market_data.py` and remove the entire `get_quote` function (lines 122–150 approx — the function defined there with zero external callers). Verify nothing in the file itself calls `get_quote`.

```bash
grep -n "get_quote" data/market_data.py
```
Expected: no output after deletion.

- [ ] **Step 3: Run the migration script**

```bash
.venv/bin/python tools/migrate_imports.py market_data data.market_data
```
Expected output: lists files updated (alpaca_client.py, earnings_calendar.py, trading/executor.py, macro/intraday_context.py, utils/market_monitor.py, tests/test_market.py).

- [ ] **Step 4: Verify the import is gone from root**

```bash
grep -rn "from market_data import\|import market_data" --include="*.py" . | grep -v __pycache__ | grep -v "data/market_data"
```
Expected: no output.

- [ ] **Step 5: Smoke test**

```bash
.venv/bin/python -c "from data.market_data import get_intraday_bars, get_daily_bars, get_latest_price; print('ok')"
```
Expected: `ok`

- [ ] **Step 6: Run tests**

```bash
.venv/bin/python -m pytest tests/ -m unit -q --tb=no 2>&1 | tail -3
```
Expected: 664 passed

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor: market_data.py → data/market_data.py, delete dead get_quote()"
```

---

## Task 6: Move economic_calendar.py → macro/economic_calendar.py

**Files:**
- Move: `economic_calendar.py` → `macro/economic_calendar.py`
- Callers to update: `macro_regime.py` (lazy import line 124), `macro/intraday_context.py`, `trading/context_builder.py`, `signals/entry_gates.py`, `trading/eod_holds.py`, `scheduler_jobs.py`, `tests/test_foundation.py`, `tests/test_signals.py`, `tests/test_trading_pipeline.py`

- [ ] **Step 1: Move the file**

```bash
git mv economic_calendar.py macro/economic_calendar.py
```

- [ ] **Step 2: Run the migration script**

```bash
.venv/bin/python tools/migrate_imports.py economic_calendar macro.economic_calendar
```

- [ ] **Step 3: Fix the lazy import inside `macro_regime.py` (not caught by script — it's inside a function body)**

`macro_regime.py` line 124 has: `from economic_calendar import is_event_risk_window`
Change it to: `from macro.economic_calendar import is_event_risk_window`

Verify:
```bash
grep -n "economic_calendar" macro_regime.py
```
Expected: `from macro.economic_calendar import is_event_risk_window`

- [ ] **Step 4: Verify no stale imports remain**

```bash
grep -rn "from economic_calendar\|import economic_calendar" --include="*.py" . | grep -v __pycache__ | grep -v "macro/economic_calendar"
```
Expected: no output.

- [ ] **Step 5: Smoke test**

```bash
.venv/bin/python -c "from macro.economic_calendar import is_event_risk_window, is_post_major_event_day; print('ok')"
```
Expected: `ok`

- [ ] **Step 6: Run tests**

```bash
.venv/bin/python -m pytest tests/ -m unit -q --tb=no 2>&1 | tail -3
```
Expected: 664 passed

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor: economic_calendar.py → macro/economic_calendar.py"
```

---

## Task 7: Move telegram_notify.py → notifications/telegram.py

**Files:**
- Move: `telegram_notify.py` → `notifications/telegram.py`
- Callers to update: `main.py`, `scheduler_jobs.py`, `trading/claude_caller.py`, `trading/closer.py`, `trading/cycle.py`, `trading/executor.py`, `trading/monitor.py`, `trading/risk_manager.py`, `utils/market_monitor.py`, `utils/startup.py`, `agents/watch_list.py`, `tests/test_market.py`, `tests/test_trading_pipeline.py`

- [ ] **Step 1: Move the file**

```bash
git mv telegram_notify.py notifications/telegram.py
```

- [ ] **Step 2: Run the migration script**

```bash
.venv/bin/python tools/migrate_imports.py telegram_notify notifications.telegram
```

- [ ] **Step 3: Verify no stale imports remain**

```bash
grep -rn "from telegram_notify\|import telegram_notify" --include="*.py" . | grep -v __pycache__ | grep -v "notifications/telegram"
```
Expected: no output.

- [ ] **Step 4: Smoke test**

```bash
.venv/bin/python -c "import notifications.telegram as telegram_notify; print(telegram_notify._send.__name__)"
```
Expected: `_send`

- [ ] **Step 5: Run tests**

```bash
.venv/bin/python -m pytest tests/ -m unit -q --tb=no 2>&1 | tail -3
```
Expected: 664 passed

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor: telegram_notify.py → notifications/telegram.py"
```

---

## Task 8: Move earnings_calendar.py → calendars/earnings.py

**Files:**
- Move: `earnings_calendar.py` → `calendars/earnings.py`
- Internal import to fix: `from market_data import get_daily_bars` → `from data.market_data import get_daily_bars`
- Callers to update: `scheduler_jobs.py`, `signals/entry_gates.py`, `swing_manager.py`, `trading/context_builder.py`, `trading/eod_holds.py`, `trading/modes.py`, `tests/test_earnings.py`, `tests/test_signals.py`, `tests/test_trading_pipeline.py`

- [ ] **Step 1: Move the file**

```bash
git mv earnings_calendar.py calendars/earnings.py
```

- [ ] **Step 2: Fix the internal lazy import inside `calendars/earnings.py`**

Find the line (around line 93) with `from market_data import get_daily_bars` and change it to:
```python
from data.market_data import get_daily_bars
```

Verify:
```bash
grep -n "market_data" calendars/earnings.py
```
Expected: `from data.market_data import get_daily_bars`

- [ ] **Step 3: Run the migration script**

```bash
.venv/bin/python tools/migrate_imports.py earnings_calendar calendars.earnings
```

- [ ] **Step 4: Verify no stale imports remain**

```bash
grep -rn "from earnings_calendar\|import earnings_calendar" --include="*.py" . | grep -v __pycache__ | grep -v "calendars/earnings"
```
Expected: no output.

- [ ] **Step 5: Smoke test**

```bash
.venv/bin/python -c "from calendars.earnings import is_earnings_imminent, detect_pead_setup, recently_reported; print('ok')"
```
Expected: `ok`

- [ ] **Step 6: Run tests**

```bash
.venv/bin/python -m pytest tests/ -m unit -q --tb=no 2>&1 | tail -3
```
Expected: 664 passed

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor: earnings_calendar.py → calendars/earnings.py"
```

---

## Task 9: Move alpaca_client.py → broker/alpaca_client.py

**Files:**
- Move: `alpaca_client.py` → `broker/alpaca_client.py`
- Internal imports to fix (lazy, inside methods): `from market_data import ...` → `from data.market_data import ...`
- Callers to update: `agents/volatility_screener.py`, `agents/watch_list.py`, `daily_report.py`, `macro/yahoo_fetcher.py`, `scheduler_jobs.py`, `trading/closer.py`, `trading/cycle.py`, `trading/executor.py`, `trading/monitor.py`, `trading/pipeline.py`, `utils/bot_health.py`, `utils/market_monitor.py`, `tests/test_core.py`, `test_agents.py`, `tests/test_market.py`

- [ ] **Step 1: Move the file**

```bash
git mv alpaca_client.py broker/alpaca_client.py
```

- [ ] **Step 2: Fix internal lazy imports in `broker/alpaca_client.py`**

Three lines inside methods reference `from market_data import ...`. Change all three:
```python
# Line ~38 (inside get_latest_price method):
from data.market_data import get_latest_price
# Line ~42 (inside get_intraday_bars method):
from data.market_data import get_intraday_bars
# Line ~46 (inside get_daily_bars method):
from data.market_data import get_daily_bars
```

Verify:
```bash
grep -n "market_data" broker/alpaca_client.py
```
Expected: all three lines show `data.market_data`.

- [ ] **Step 3: Run the migration script**

```bash
.venv/bin/python tools/migrate_imports.py alpaca_client broker.alpaca_client
```

- [ ] **Step 4: Verify no stale imports remain**

```bash
grep -rn "from alpaca_client\|import alpaca_client" --include="*.py" . | grep -v __pycache__ | grep -v "broker/alpaca_client"
```
Expected: no output.

- [ ] **Step 5: Smoke test**

```bash
.venv/bin/python -c "from broker.alpaca_client import AlpacaClient; print('ok')"
```
Expected: `ok`

- [ ] **Step 6: Run tests**

```bash
.venv/bin/python -m pytest tests/ -m unit -q --tb=no 2>&1 | tail -3
```
Expected: 664 passed

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor: alpaca_client.py → broker/alpaca_client.py"
```

---

## Task 10: Move macro_regime.py → macro/regime.py

**Files:**
- Move: `macro_regime.py` → `macro/regime.py`
- Internal import to fix (lazy, line 124): `from economic_calendar import ...` → `from macro.economic_calendar import ...` (already updated in Task 6 — verify it's still correct after move)
- Callers to update: `agents/watch_list.py`, `learning/agent.py`, `scheduler_jobs.py`, `trading/cycle.py`, `trading/cycle_context.py`, `trading/eod_holds.py`, `main.py`, `tests/test_foundation.py`, `tests/test_learning.py`

- [ ] **Step 1: Move the file**

```bash
git mv macro_regime.py macro/regime.py
```

- [ ] **Step 2: Verify the internal lazy import is correct**

```bash
grep -n "economic_calendar" macro/regime.py
```
Expected: `from macro.economic_calendar import is_event_risk_window` (updated in Task 6).

- [ ] **Step 3: Run the migration script**

```bash
.venv/bin/python tools/migrate_imports.py macro_regime macro.regime
```

- [ ] **Step 4: Verify no stale imports remain**

```bash
grep -rn "from macro_regime\|import macro_regime" --include="*.py" . | grep -v __pycache__ | grep -v "macro/regime"
```
Expected: no output.

- [ ] **Step 5: Smoke test**

```bash
.venv/bin/python -c "from macro.regime import load_macro_regime, fetch_macro_regime, load_trading_params; print('ok')"
```
Expected: `ok`

- [ ] **Step 6: Run tests**

```bash
.venv/bin/python -m pytest tests/ -m unit -q --tb=no 2>&1 | tail -3
```
Expected: 664 passed

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor: macro_regime.py → macro/regime.py"
```

---

## Task 11: Move swing_manager.py → trading/swing_manager.py

**Files:**
- Move: `swing_manager.py` → `trading/swing_manager.py`
- Internal import to fix: `from earnings_calendar import is_earnings_imminent` → `from calendars.earnings import is_earnings_imminent`
- Callers to update: `scheduler_jobs.py`, `trading/closer.py`, `trading/monitor.py`, `utils/bot_health.py`, `utils/market_monitor.py`, `tests/test_swing.py`

- [ ] **Step 1: Move the file**

```bash
git mv swing_manager.py trading/swing_manager.py
```

- [ ] **Step 2: Fix the internal top-level import in `trading/swing_manager.py`**

Change line 9 from:
```python
from earnings_calendar import is_earnings_imminent
```
to:
```python
from calendars.earnings import is_earnings_imminent
```

Verify:
```bash
grep -n "earnings" trading/swing_manager.py | head -5
```
Expected: `from calendars.earnings import is_earnings_imminent`

- [ ] **Step 3: Run the migration script**

```bash
.venv/bin/python tools/migrate_imports.py swing_manager trading.swing_manager
```

- [ ] **Step 4: Verify no stale imports remain**

```bash
grep -rn "from swing_manager\|import swing_manager" --include="*.py" . | grep -v __pycache__ | grep -v "trading/swing_manager"
```
Expected: no output.

- [ ] **Step 5: Smoke test**

```bash
.venv/bin/python -c "from trading.swing_manager import load_swing_positions, evaluate_eod_holds; print('ok')"
```
Expected: `ok`

- [ ] **Step 6: Run tests**

```bash
.venv/bin/python -m pytest tests/ -m unit -q --tb=no 2>&1 | tail -3
```
Expected: 664 passed

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor: swing_manager.py → trading/swing_manager.py"
```

---

## Task 12: Move daily_report.py → notifications/daily_report.py

**Files:**
- Move: `daily_report.py` → `notifications/daily_report.py`
- Internal import to fix: `import telegram_notify` → `import notifications.telegram as telegram_notify`
- Callers to update: `scheduler_jobs.py`, `utils/catchup.py`, `tests/test_agents.py`, `tests/test_market.py`

- [ ] **Step 1: Move the file**

```bash
git mv daily_report.py notifications/daily_report.py
```

- [ ] **Step 2: Fix the internal top-level import in `notifications/daily_report.py`**

Change line 9 from:
```python
import telegram_notify
```
to:
```python
import notifications.telegram as telegram_notify
```

Also fix the `alpaca_client` lazy import inside the file:
```bash
grep -n "alpaca_client" notifications/daily_report.py
```
If it shows `from alpaca_client import AlpacaClient`, change to `from broker.alpaca_client import AlpacaClient`.

Verify:
```bash
grep -n "telegram_notify\|alpaca_client" notifications/daily_report.py
```
Expected: both lines show new paths.

- [ ] **Step 3: Run the migration script**

```bash
.venv/bin/python tools/migrate_imports.py daily_report notifications.daily_report
```

- [ ] **Step 4: Verify no stale imports remain**

```bash
grep -rn "from daily_report\|import daily_report" --include="*.py" . | grep -v __pycache__ | grep -v "notifications/daily_report"
```
Expected: no output.

- [ ] **Step 5: Smoke test**

```bash
.venv/bin/python -c "from notifications.daily_report import build_report_body, send_daily_report; print('ok')"
```
Expected: `ok`

- [ ] **Step 6: Run tests**

```bash
.venv/bin/python -m pytest tests/ -m unit -q --tb=no 2>&1 | tail -3
```
Expected: 664 passed

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor: daily_report.py → notifications/daily_report.py"
```

---

## Task 13: Migrate JSON state files to data/state/

All 15 JSON runtime files move from root into `data/state/`. Config already points there (Task 3). This makes the paths match reality.

**Files:**
- `git mv` 15 JSON files

- [ ] **Step 1: Move all JSON state files**

```bash
git mv open_positions.json data/state/
git mv swing_positions.json data/state/
git mv trade_journal.json data/state/
git mv daily_closed.json data/state/
git mv macro_regime.json data/state/
git mv fred_cache.json data/state/
git mv economic_calendar.json data/state/
git mv earnings_calendar.json data/state/
git mv research_report.json data/state/
git mv api_budget.json data/state/
git mv indicator_weights.json data/state/
git mv trading_params.json data/state/
git mv intraday_news_cache.json data/state/
git mv bot_health.json data/state/
git mv daily_report_sent.json data/state/
```

- [ ] **Step 2: Verify root is clean of JSON state files**

```bash
ls *.json 2>/dev/null || echo "no JSON at root"
```
Expected: `no JSON at root`

- [ ] **Step 3: Verify data/state/ has all files**

```bash
ls data/state/ | wc -l
```
Expected: `15`

- [ ] **Step 4: Verify config paths resolve correctly**

```bash
.venv/bin/python -c "
import config
files = [
    config.OPEN_POSITIONS_FILE, config.TRADE_JOURNAL_FILE,
    config.API_BUDGET_FILE, config.MACRO_REGIME_FILE,
]
for f in files:
    print(f.exists(), f)
"
```
Expected: all print `True data/state/...`

- [ ] **Step 5: Run tests**

```bash
.venv/bin/python -m pytest tests/ -m unit -q --tb=no 2>&1 | tail -3
```
Expected: 664 passed (tests use `tmp_path`, not live state files).

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor: move JSON state files to data/state/"
```

---

## Task 14: Move log files to logs/

**Files:**
- `git mv trading_bot.log logs/` (if it exists)
- `git mv launchd_trading_bot.log logs/` (if it exists)

- [ ] **Step 1: Move any existing log files**

```bash
[ -f trading_bot.log ] && git mv trading_bot.log logs/ || echo "trading_bot.log not present (will be created there on next run)"
[ -f launchd_trading_bot.log ] && git mv launchd_trading_bot.log logs/ || echo "launchd_trading_bot.log not present"
```

- [ ] **Step 2: Add logs/ to .gitignore if not already there**

```bash
grep -q "^logs/" .gitignore 2>/dev/null || echo "logs/" >> .gitignore
grep -q "^data/state/" .gitignore 2>/dev/null || echo "data/state/" >> .gitignore
```

Note: state files and logs are runtime artifacts — they should be in `.gitignore`.

- [ ] **Step 3: Run tests**

```bash
.venv/bin/python -m pytest tests/ -m unit -q --tb=no 2>&1 | tail -3
```
Expected: 664 passed

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor: move log files to logs/, add logs/ and data/state/ to .gitignore"
```

---

## Task 15: Update CLAUDE.md Rule #6 and clean up migration tool

**Files:**
- Modify: `CLAUDE.md`
- Delete: `tools/migrate_imports.py` (purpose complete)

- [ ] **Step 1: Update Rule #6 domain package table in CLAUDE.md**

Find the table under "### 6. Package placement — no root-level agent files" and add three rows:

```markdown
| Broker I/O, order routing | `broker/` |
| Earnings dates, PEAD detection | `calendars/` |
| Market data, OHLCV providers | `data/` |
| Telegram + email notifications | `notifications/` |
```

- [ ] **Step 2: Update the scheduler table in CLAUDE.md**

Update the module paths in the Architecture → Scheduler and Key Modules tables:

| Old reference | New reference |
|---|---|
| `trading_agent.run_trading_cycle()` | (already updated in prior refactor) |
| `swing_manager.evaluate_eod_holds()` | `trading.swing_manager.evaluate_eod_holds()` |
| `trading_agent.force_close_all()` | (already updated) |
| `macro_regime.fetch_macro_regime()` | `macro.regime.fetch_macro_regime()` |
| `macro_regime.py` in Key Modules | `macro/regime.py` |

Also update the Key Modules table rows for: `macro_regime.py → macro/regime.py`, `market_data.py → data/market_data.py`, `swing_manager.py → trading/swing_manager.py`.

- [ ] **Step 3: Delete the migration tool**

```bash
git rm tools/migrate_imports.py
rmdir tools 2>/dev/null || true  # remove if empty
```

- [ ] **Step 4: Final verification — complete test run**

```bash
.venv/bin/python -m pytest tests/ -m unit -q --tb=short 2>&1 | tail -10
```
Expected: 664 passed, 0 failed.

- [ ] **Step 5: Verify no root-level stale imports remain anywhere**

```bash
grep -rn "from macro_regime\|from economic_calendar\|from earnings_calendar\|from market_data\|from swing_manager\|from daily_report\|from telegram_notify\|from alpaca_client\|import macro_regime\|import economic_calendar\|import earnings_calendar\|import market_data\|import swing_manager\|import daily_report\|import telegram_notify\|import alpaca_client" --include="*.py" . | grep -v __pycache__ | grep -v ".venv"
```
Expected: no output.

- [ ] **Step 6: Final commit**

```bash
git add -A
git commit -m "refactor: update CLAUDE.md package table, remove migration tool"
```

---

## Self-Review

**Spec coverage check:**
- ✓ 8 Python modules moved (Tasks 5–12)
- ✓ `get_quote()` dead code deleted (Task 5)
- ✓ `STATE_DIR` added to config.py (Task 3)
- ✓ 15 JSON files moved to `data/state/` (Task 13)
- ✓ Log files moved to `logs/` (Task 14)
- ✓ `main.py` creates dirs at startup (Task 4)
- ✓ `start.sh` + plist updated (Task 4)
- ✓ CLAUDE.md Rule #6 updated (Task 15)
- ✓ No re-export shims — all callers updated directly
- ✓ 664 tests must stay green (verified after each task)

**Internal import chain verified:**
- `broker/alpaca_client.py` → `data.market_data` ✓
- `calendars/earnings.py` → `data.market_data` ✓
- `macro/regime.py` → `macro.economic_calendar` ✓
- `trading/swing_manager.py` → `calendars.earnings` ✓
- `notifications/daily_report.py` → `notifications.telegram`, `broker.alpaca_client` ✓

**Migration script coverage:** handles `import X`, `import X as Y`, `from X import`. The one gap — lazy imports inside function bodies that use `from X import` — is handled manually in each task (Tasks 6, 8, 9, 12) with explicit grep-and-verify steps.
