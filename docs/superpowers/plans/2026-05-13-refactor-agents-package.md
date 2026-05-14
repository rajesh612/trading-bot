# Refactor watch_list_agent & volatility_screener into agents/ Package

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move `watch_list_agent.py` and `volatility_screener.py` from root to a new `agents/` package to enforce CLAUDE.md rule: "Root-level .py agent files are not allowed."

**Architecture:** Create new `agents/` package with two modules: `watch_list.py` and `volatility_screener.py`. Copy full content from root files (no logic changes). Update all import sites in `scheduler_jobs.py` (3 locations). Delete old root files. Verify imports and tests pass.

**Tech Stack:** Python 3.9+, pytest, git

---

## Task 1: Create agents package structure

**Files:**
- Create: `agents/__init__.py`
- Create: `agents/watch_list.py`
- Create: `agents/volatility_screener.py`

- [ ] **Step 1: Create agents directory and __init__.py**

```bash
mkdir -p /Users/rajeshkumar/Documents/projects/trading-bot/agents
```

Create `/Users/rajeshkumar/Documents/projects/trading-bot/agents/__init__.py`:

```python
from agents.watch_list import run_watch_list_agent
from agents.volatility_screener import run_volatility_screener

__all__ = ["run_watch_list_agent", "run_volatility_screener"]
```

- [ ] **Step 2: Copy watch_list_agent.py to agents/watch_list.py**

Copy full content of `/Users/rajeshkumar/Documents/projects/trading-bot/watch_list_agent.py` to `/Users/rajeshkumar/Documents/projects/trading-bot/agents/watch_list.py` — no modifications to logic or imports.

Expected: File has 186 lines, imports anthropic, config, trading.risk_manager, AlpacaClient, indicators.engine.

- [ ] **Step 3: Copy volatility_screener.py to agents/volatility_screener.py**

Copy full content of `/Users/rajeshkumar/Documents/projects/trading-bot/volatility_screener.py` to `/Users/rajeshkumar/Documents/projects/trading-bot/agents/volatility_screener.py` — no modifications to logic or imports.

Expected: File has 87 lines, imports config, AlpacaClient, numpy, pandas.

- [ ] **Step 4: Verify agents package imports work**

```bash
cd /Users/rajeshkumar/Documents/projects/trading-bot
.venv/bin/python3 -c "from agents.watch_list import run_watch_list_agent, _load_universe; print('agents.watch_list: OK')"
.venv/bin/python3 -c "from agents.volatility_screener import run_volatility_screener; print('agents.volatility_screener: OK')"
.venv/bin/python3 -c "from agents import run_watch_list_agent, run_volatility_screener; print('agents.__init__: OK')"
```

Expected: Three "OK" lines, no import errors.

- [ ] **Step 5: Commit package creation**

```bash
cd /Users/rajeshkumar/Documents/projects/trading-bot
git add agents/
git commit -m "refactor: create agents/ package with watch_list.py + volatility_screener.py"
```

Expected: Commit succeeds, staging clean.

---

## Task 2: Update scheduler_jobs.py import sites

**Files:**
- Modify: `scheduler_jobs.py` — 3 import locations

- [ ] **Step 1: Update job_volatility_screener function (line 81)**

In `/Users/rajeshkumar/Documents/projects/trading-bot/scheduler_jobs.py`, line 81:

Old:
```python
from volatility_screener import run_volatility_screener
```

New:
```python
from agents.volatility_screener import run_volatility_screener
```

- [ ] **Step 2: Update job_watch_list function (line 90)**

In `/Users/rajeshkumar/Documents/projects/trading-bot/scheduler_jobs.py`, line 90:

Old:
```python
from watch_list_agent import run_watch_list_agent
```

New:
```python
from agents.watch_list import run_watch_list_agent
```

- [ ] **Step 3: Update job_research function (line 101)**

In `/Users/rajeshkumar/Documents/projects/trading-bot/scheduler_jobs.py`, line 101:

Old:
```python
from watch_list_agent import _load_universe
```

New:
```python
from agents.watch_list import _load_universe
```

- [ ] **Step 4: Verify scheduler_jobs imports work**

```bash
cd /Users/rajeshkumar/Documents/projects/trading-bot
.venv/bin/python3 -c "import scheduler_jobs; print('scheduler_jobs: OK')"
```

Expected: "scheduler_jobs: OK" with no import errors.

- [ ] **Step 5: Commit scheduler_jobs updates**

```bash
cd /Users/rajeshkumar/Documents/projects/trading-bot
git add scheduler_jobs.py
git commit -m "refactor: update scheduler_jobs imports to agents.watch_list + agents.volatility_screener"
```

Expected: Commit succeeds, only scheduler_jobs.py modified.

---

## Task 3: Delete old root-level files and run tests

**Files:**
- Delete: `watch_list_agent.py`
- Delete: `volatility_screener.py`

- [ ] **Step 1: Delete old root-level files**

```bash
cd /Users/rajeshkumar/Documents/projects/trading-bot
git rm watch_list_agent.py volatility_screener.py
```

Expected: Two files staged for deletion.

- [ ] **Step 2: Verify old files are gone from working tree**

```bash
ls /Users/rajeshkumar/Documents/projects/trading-bot/watch_list_agent.py 2>&1
ls /Users/rajeshkumar/Documents/projects/trading-bot/volatility_screener.py 2>&1
```

Expected: Two "No such file or directory" errors.

- [ ] **Step 3: Run unit tests to verify no import breakage**

```bash
cd /Users/rajeshkumar/Documents/projects/trading-bot
.venv/bin/python3 -m pytest tests/ -m unit -q 2>&1 | tail -10
```

Expected: No import errors; tests pass or show only pre-existing failures (not related to agents module).

- [ ] **Step 4: Run a final import verification**

```bash
cd /Users/rajeshkumar/Documents/projects/trading-bot
.venv/bin/python3 << 'EOF'
import sys
try:
    from agents.watch_list import run_watch_list_agent, _load_universe
    from agents.volatility_screener import run_volatility_screener
    from agents import run_watch_list_agent as rwa, run_volatility_screener as rvs
    from scheduler_jobs import job_watch_list, job_volatility_screener, job_research
    print("All imports: OK")
    sys.exit(0)
except Exception as e:
    print(f"Import failed: {e}", file=sys.stderr)
    sys.exit(1)
EOF
```

Expected: "All imports: OK" with exit code 0.

- [ ] **Step 5: Commit deletions**

```bash
cd /Users/rajeshkumar/Documents/projects/trading-bot
git commit -m "refactor: remove old watch_list_agent.py + volatility_screener.py from root"
```

Expected: Commit succeeds; `git log --oneline -3` shows three refactor commits.

---

## Task 4: Final verification and summary

**Files:**
- No files created or deleted in this task

- [ ] **Step 1: Verify final git status**

```bash
cd /Users/rajeshkumar/Documents/projects/trading-bot
git status
```

Expected: Working tree clean; three commits in log.

- [ ] **Step 2: Verify directory structure**

```bash
find /Users/rajeshkumar/Documents/projects/trading-bot/agents -type f -name "*.py" | sort
```

Expected:
```
/Users/rajeshkumar/Documents/projects/trading-bot/agents/__init__.py
/Users/rajeshkumar/Documents/projects/trading-bot/agents/volatility_screener.py
/Users/rajeshkumar/Documents/projects/trading-bot/agents/watch_list.py
```

- [ ] **Step 3: Confirm root no longer has agent files**

```bash
ls /Users/rajeshkumar/Documents/projects/trading-bot/*.py | grep -E "(watch_list_agent|volatility_screener)" | wc -l
```

Expected: 0 (no matches).

- [ ] **Step 4: Log final commit hashes**

```bash
cd /Users/rajeshkumar/Documents/projects/trading-bot
git log --oneline -3
```

Expected: Output shows three "refactor:" commits.

---

## Acceptance Criteria

✓ `agents/` package exists with `__init__.py`, `watch_list.py`, `volatility_screener.py`  
✓ All imports work: `from agents.watch_list import ...`, `from agents.volatility_screener import ...`  
✓ `scheduler_jobs.py` updated (3 sites) and imports succeed  
✓ Old root files deleted via `git rm`  
✓ Working tree clean; no import errors in tests  
✓ Three commits: create package, update scheduler, delete old files
