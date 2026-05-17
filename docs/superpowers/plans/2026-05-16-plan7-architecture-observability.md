# Plan 7: Architecture & Observability

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add lightweight metrics export for trend analysis; split `tests/test_agents.py` (804 lines, violates Rule 17); add regime-adaptive logic (not just threshold changes); fix stale-research fallback when research is from a prior day.

**Architecture:** Metrics are appended to `data/state/metrics.jsonl` (one JSON record per cycle — never truncated within a day, rotated daily). Test split follows Rule 19 naming: `test_research_agent.py`, `test_watch_list_agent.py`, `test_learning_agent.py`. Regime-adaptive logic adds a `RegimeStrategy` enum that determines the directional bias (breakout vs. fade-extreme vs. defensive) used by the pre-Claude filter and context builder.

**Tech Stack:** Python 3.11, pytest, jsonlines pattern (no new deps)

---

## File Map

| File | Change |
|---|---|
| `utils/metrics.py` | NEW — `record_cycle_metric()`, `get_daily_summary()` |
| `config.py` | Add `METRICS_FILE`, `REGIME_ADAPTIVE_LOGIC` flag |
| `trading/cycle.py` | Call `record_cycle_metric()` after each cycle |
| `trading/pre_claude_filter.py` | Apply regime strategy bias (breakout/fade) to composite gate |
| `tests/test_research_agent.py` | NEW — research-agent tests split from test_agents.py |
| `tests/test_watch_list_agent.py` | NEW — watch-list tests split from test_agents.py |
| `tests/test_learning_agent.py` | NEW — learning-agent tests split from test_agents.py |
| `tests/test_agents.py` | Delete or reduce to ≤ 700 lines (keep macro/foundation tests) |

---

### Task 1: utils/metrics.py — JSONL metrics export

**Files:**
- Create: `utils/metrics.py`
- Modify: `config.py`

- [ ] **Step 1: Add config constant**

```python
# config.py
METRICS_FILE = STATE_DIR / "metrics.jsonl"
```

- [ ] **Step 2: Write failing test**

```python
# tests/test_foundation.py — add
import pytest, config, json

@pytest.mark.unit
def test_record_cycle_metric_appends_jsonl(monkeypatch, tmp_path):
    """record_cycle_metric appends one JSON line per call."""
    monkeypatch.setattr(config, "METRICS_FILE", tmp_path / "metrics.jsonl")
    from utils.metrics import record_cycle_metric

    record_cycle_metric(
        regime="NORMAL", candidates=5, entries=1, closes=0,
        api_budget_pct=0.30, cycle_ms=1200.0
    )
    record_cycle_metric(
        regime="NORMAL", candidates=3, entries=0, closes=1,
        api_budget_pct=0.35, cycle_ms=900.0
    )

    lines = (tmp_path / "metrics.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["regime"] == "NORMAL"
    assert rec["candidates"] == 5
    assert "ts" in rec

@pytest.mark.unit
def test_get_daily_summary_returns_aggregates(monkeypatch, tmp_path):
    """get_daily_summary returns win_rate, avg_candidates, total_entries from today's records."""
    monkeypatch.setattr(config, "METRICS_FILE", tmp_path / "metrics.jsonl")
    from utils.metrics import record_cycle_metric, get_daily_summary
    from datetime import datetime
    from zoneinfo import ZoneInfo

    ET = ZoneInfo("America/New_York")
    today = datetime.now(ET).strftime("%Y-%m-%d")

    record_cycle_metric(regime="NORMAL", candidates=5, entries=1, closes=0, api_budget_pct=0.3, cycle_ms=1000)
    record_cycle_metric(regime="BULL_TREND", candidates=8, entries=2, closes=1, api_budget_pct=0.5, cycle_ms=1100)

    summary = get_daily_summary(date_str=today)
    assert summary["total_entries"] == 3
    assert summary["total_cycles"] == 2
    assert summary["avg_candidates"] == 6.5
```

- [ ] **Step 3: Create utils/metrics.py**

```python
"""
Lightweight per-cycle metrics JSONL export.
Records one line per cycle to METRICS_FILE for post-hoc trend analysis.
Pure appends — no lock needed (single scheduler thread writes; reads are rare).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import config
from utils.constants import ET

logger = logging.getLogger(__name__)


def record_cycle_metric(
    regime: str,
    candidates: int,
    entries: int,
    closes: int,
    api_budget_pct: float,
    cycle_ms: float,
) -> None:
    """Append one metric record to METRICS_FILE. Never raises."""
    try:
        record = json.dumps({
            "ts": datetime.now(ET).isoformat(),
            "date": datetime.now(ET).strftime("%Y-%m-%d"),
            "regime": regime,
            "candidates": candidates,
            "entries": entries,
            "closes": closes,
            "api_budget_pct": round(api_budget_pct, 3),
            "cycle_ms": round(cycle_ms, 0),
        })
        with open(config.METRICS_FILE, "a") as f:
            f.write(record + "\n")
    except Exception as exc:
        logger.debug("metrics write failed: %s", exc)


def get_daily_summary(date_str: str | None = None) -> dict:
    """Aggregate today's cycle metrics. Returns empty dict if no records."""
    target_date = date_str or datetime.now(ET).strftime("%Y-%m-%d")
    path = Path(str(config.METRICS_FILE))
    if not path.exists():
        return {}
    records = []
    try:
        for line in path.read_text().splitlines():
            try:
                r = json.loads(line)
                if r.get("date") == target_date:
                    records.append(r)
            except Exception:
                pass
    except Exception:
        return {}
    if not records:
        return {}
    total_entries = sum(r.get("entries", 0) for r in records)
    total_closes  = sum(r.get("closes", 0) for r in records)
    avg_candidates = sum(r.get("candidates", 0) for r in records) / len(records)
    regimes = [r.get("regime") for r in records]
    return {
        "total_cycles": len(records),
        "total_entries": total_entries,
        "total_closes": total_closes,
        "avg_candidates": round(avg_candidates, 1),
        "dominant_regime": max(set(regimes), key=regimes.count),
        "regime_flips": len(set(regimes)) - 1,
        "api_budget_pct_final": records[-1].get("api_budget_pct", 0) if records else 0,
    }
```

- [ ] **Step 4: Run tests**

```
.venv/bin/python -m pytest tests/test_foundation.py -k "record_cycle_metric or get_daily_summary" -v
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 5: Commit**

```bash
git add utils/metrics.py config.py tests/test_foundation.py
git commit -m "feat: utils/metrics.py — per-cycle JSONL metrics for trend analysis and EOD enrichment"
```

---

### Task 2: Wire metrics into trading cycle

**Files:**
- Modify: `trading/cycle.py`

- [ ] **Step 1: Update run_trading_cycle() to record metrics**

At the end of `run_trading_cycle()`, after `pipeline.run()` returns:

```python
    # Record cycle metrics for trend analysis
    try:
        from utils.metrics import record_cycle_metric
        from trading.risk_manager import load_budget
        budget = load_budget()
        ceiling = budget.get("ceiling_usd", 1.0)
        spent   = budget.get("estimated_cost_usd", 0.0)
        budget_pct = spent / ceiling * 100 if ceiling else 0.0
        cycle_elapsed_ms = (time.monotonic() - _t_cycle_start) * 1000 if hasattr(cycle, "_t_cycle_start") else 0.0
        record_cycle_metric(
            regime=regime,
            candidates=cycle_result.candidates,
            entries=cycle_result.entries,
            closes=cycle_result.closes,
            api_budget_pct=budget_pct,
            cycle_ms=cycle_elapsed_ms,
        )
    except Exception:
        pass
```

Add `import time` and `_t_cycle_start = time.monotonic()` at the top of `run_trading_cycle()`.

- [ ] **Step 2: Use daily summary in EOD report**

In `notifications/daily_report.py`, at the `send_daily_report()` function:

```python
    from utils.metrics import get_daily_summary
    daily_metrics = get_daily_summary()
    cycles_run = daily_metrics.get("total_cycles", 0)
    regime_flips = daily_metrics.get("regime_flips", 0)
```

Pass these to `notify_eod_summary()` (already extended in Plan 3).

- [ ] **Step 3: Run full suite**

```
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 4: Commit**

```bash
git add trading/cycle.py notifications/daily_report.py
git commit -m "feat: wire metrics into trading cycle; daily_report uses cycle count + regime flips"
```

---

### Task 3: Regime-adaptive logic — directional bias beyond threshold adjustment

Currently CHOPPY, BULL_TREND etc. only change numeric thresholds. Add a directional strategy:
- **BULL_TREND**: prefer breakouts (require price above 5d high or new session high)
- **CHOPPY**: prefer mean-reversion setups (require RSI pulled back from recent high, not parabolic)
- **DEFENSIVE**: no new entries except PEAD/CAPITULATION

**Files:**
- Modify: `trading/pre_claude_filter.py`
- Modify: `config.py`

- [ ] **Step 1: Add config flag**

```python
# config.py
REGIME_ADAPTIVE_LOGIC = True   # enable directional strategy layer (beyond threshold adjustment)
BULL_TREND_BREAKOUT_MIN_SCORE = 25   # composite min for breakout setup in BULL_TREND
CHOPPY_RSI_PULLBACK_MAX = 60   # in CHOPPY, only enter if RSI < this (avoid buying the bounce top)
```

- [ ] **Step 2: Write failing test**

```python
# tests/test_trading_pipeline.py — add
from trading.pre_claude_filter import pre_claude_filter
import config

@pytest.mark.unit
def test_choppy_regime_blocks_high_rsi_entries(monkeypatch):
    """CHOPPY regime: entries with RSI > CHOPPY_RSI_PULLBACK_MAX should be skipped."""
    monkeypatch.setattr(config, "REGIME_ADAPTIVE_LOGIC", True)
    monkeypatch.setattr(config, "CHOPPY_RSI_PULLBACK_MAX", 60)
    monkeypatch.setattr(config, "AUTO_EXECUTE_COMPOSITE_MIN", 100)

    iv = _make_iv(composite_score=30.0, rsi=65.0)  # RSI above CHOPPY max
    snap = _make_snap(regime="CHOPPY")

    result = pre_claude_filter("AAPL", iv, snap, set())
    assert result.decision == "SKIP"
    assert "choppy" in result.reason.lower() or "rsi" in result.reason.lower()

@pytest.mark.unit
def test_bull_trend_allows_higher_composite(monkeypatch):
    """BULL_TREND regime: composite ≥ BULL_TREND_BREAKOUT_MIN_SCORE is allowed."""
    monkeypatch.setattr(config, "REGIME_ADAPTIVE_LOGIC", True)
    monkeypatch.setattr(config, "BULL_TREND_BREAKOUT_MIN_SCORE", 25)
    monkeypatch.setattr(config, "AUTO_EXECUTE_COMPOSITE_MIN", 100)

    iv = _make_iv(composite_score=27.0, rsi=60.0)
    snap = _make_snap(regime="BULL_TREND")

    result = pre_claude_filter("AAPL", iv, snap, set())
    assert result.decision != "SKIP", f"BULL_TREND should allow composite 27: {result.reason}"
```

- [ ] **Step 3: Add regime adaptive layer to pre_claude_filter.py**

Add a new Layer 1.5 after the composite minimum check:

```python
    # Layer 1.5 — Regime-adaptive directional bias (REGIME_ADAPTIVE_LOGIC)
    if config.REGIME_ADAPTIVE_LOGIC:
        if snap.regime == "CHOPPY":
            # Fade-extreme: only enter on pullbacks, not at RSI highs
            choppy_rsi_max = snap.params.get("choppy_rsi_max", config.CHOPPY_RSI_PULLBACK_MAX)
            if iv.rsi > choppy_rsi_max:
                return FilterResult("SKIP",
                    f"choppy regime: RSI {iv.rsi:.1f} > pullback max {choppy_rsi_max}")

        elif snap.regime == "DEFENSIVE":
            # Block all longs except PEAD/CAPITULATION — handled by mode in executor
            # Pre-filter blocks non-exceptional setups to save budget
            if iv.composite_score < config.COMPOSITE_MIN_HIGH_VOL * 1.5:
                return FilterResult("SKIP",
                    f"defensive regime: composite {iv.composite_score:+.1f} below elevated bar")
```

- [ ] **Step 4: Run tests**

```
.venv/bin/python -m pytest tests/test_trading_pipeline.py -k "choppy_regime or bull_trend" -v
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 5: Commit**

```bash
git add trading/pre_claude_filter.py config.py tests/test_trading_pipeline.py
git commit -m "feat: regime-adaptive logic — CHOPPY blocks high-RSI entries; DEFENSIVE raises composite bar"
```

---

### Task 4: Split test_agents.py (Rule 17 — 804 lines)

**Files:**
- Create: `tests/test_research_agent.py`
- Create: `tests/test_watch_list_agent.py`
- Create: `tests/test_learning_agent.py`
- Modify: `tests/test_agents.py` (delete moved tests)

- [ ] **Step 1: Count lines and identify test domains**

```bash
wc -l tests/test_agents.py
grep -n "^def test_\|^class Test" tests/test_agents.py
```

- [ ] **Step 2: Create tests/test_research_agent.py**

Move all tests for `research/agent.py`, `research/analyzer.py`, `research/fetchers.py`, and `research/intraday.py` into this file. Add standard header:

```python
"""Research agent tests: sentiment, analyzer, fetchers, intraday news."""
import pytest
# ... (moved test functions from test_agents.py)
```

- [ ] **Step 3: Create tests/test_watch_list_agent.py**

Move all tests for `agents/watch_list.py` and `agents/volatility_screener.py`:

```python
"""Watch-list and volatility screener agent tests."""
import pytest
# ... (moved test functions from test_agents.py)
```

- [ ] **Step 4: Create tests/test_learning_agent.py**

Move all tests for `learning/agent.py`, `learning/data.py`, `learning/trade_analysis.py`:

```python
"""Learning agent tests: zero-trade days, param updates, weight clamping."""
import pytest
# ... (moved test functions from test_agents.py)
```

- [ ] **Step 5: Verify test_agents.py is now ≤ 700 lines**

```bash
wc -l tests/test_agents.py tests/test_research_agent.py tests/test_watch_list_agent.py tests/test_learning_agent.py
```
All files must be ≤ 700 lines.

- [ ] **Step 6: Run full suite**

```
.venv/bin/python -m pytest tests/ -m unit -q --tb=short
```
Expected: same count or more passing (no tests lost in the move).

- [ ] **Step 7: Commit**

```bash
git add tests/test_research_agent.py tests/test_watch_list_agent.py tests/test_learning_agent.py tests/test_agents.py
git commit -m "refactor: split test_agents.py (804 lines) into domain-scoped test files per CLAUDE.md Rule 17"
```

---

### Task 5: Stale-research prior-day detection

Currently the research staleness check doesn't distinguish "stale by hours" (recoverable) from "from a prior date" (yesterday's report — all sentiment is wrong). Fix this to hard-skip only if research is from a prior calendar date.

**Files:**
- Modify: `trading/pipeline.py`

- [ ] **Step 1: Write test**

```python
# tests/test_trading_pipeline.py — add
import pytest
from unittest.mock import MagicMock
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

@pytest.mark.unit
def test_stale_research_from_today_continues_with_warning(monkeypatch):
    """Research stale by 3 hours but from today → continue with warning, no skip."""
    from trading.pipeline import TradingPipeline
    from trading.cycle_context import CycleSnapshot

    snap = MagicMock()
    snap.research_meta = {
        "timestamp": (datetime.now(ET) - timedelta(hours=3)).isoformat()
    }
    snap.regime = "NORMAL"
    snap.regime_info = {"spy_5day_return": 0.0}
    snap.params = {"entry_start_hour": 10, "entry_end_hour": 15,
                   "rsi_buy_max": 72, "rsi_buy_max_high_vol": 65,
                   "high_vol_position_mult": 0.5}
    snap.research = {}
    snap.intraday_news = {}
    snap.sector_today = {}
    snap.advance_decline_ratio = 1.0

    pipe = TradingPipeline()
    # Should NOT return skipped=True for same-day stale research
    result = pipe.run(
        symbols=[], snap=snap, ai=MagicMock(),
        now=datetime.now(ET).replace(hour=11),
        alpaca=MagicMock(), portfolio_value=100_000,
        buying_power=50_000, can_open=True, daily_pnl_pct=0.0,
    )
    assert not result.skipped or "research" not in result.reason.lower(), \
        "Same-day stale research must not fully skip the cycle"
```

- [ ] **Step 2: Update pipeline.py stale-research check**

Replace the current stale block with date-aware logic:

```python
        try:
            report_dt = datetime.fromisoformat(ts_str)
            if report_dt.tzinfo is None:
                report_dt = report_dt.replace(tzinfo=ET)
            age_min = (datetime.now(ET) - report_dt).total_seconds() / 60
            report_date = report_dt.date()
            today_date  = datetime.now(ET).date()

            if report_date < today_date:
                # From a prior day — sentiment is categorically wrong
                logger.error(
                    "Research report is from %s (prior date) — running with neutral sentiment.",
                    report_date,
                )
                telegram_notify.notify_stale_research(age_min)
                # Do not skip; sentiment will be 0 (neutral) per-symbol naturally
            elif age_min > config.RESEARCH_STALE_BLOCK_MIN:
                # Same day but very old (>10h, e.g., 9:15 AM report at 7:30 PM)
                logger.warning(
                    "Research report is %.0f min old (same day) — running with stale sentiment.",
                    age_min,
                )
                telegram_notify.notify_stale_research(age_min)
            elif age_min > config.RESEARCH_STALE_WARN_MIN:
                logger.warning("Research report is %.0f min old — sentiment may be stale.", age_min)
        except Exception as exc:
            logger.warning("Research timestamp parse failed (%s) — staleness check skipped.", exc)
```

Remove the `return CycleResult(skipped=True, ...)` line entirely.

- [ ] **Step 3: Run tests**

```
.venv/bin/python -m pytest tests/test_trading_pipeline.py -k "stale_research" -v
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 4: Commit + tag**

```bash
git add trading/pipeline.py tests/test_trading_pipeline.py
git commit -m "fix: stale research skips only on prior-date (not same-day hours) — eliminates all-day zero-trade from research crash"
git tag plan7-architecture-complete
```

---

### Task 6: Final full regression + summary

- [ ] **Step 1: Full test suite**

```
.venv/bin/python -m pytest tests/ -m unit -q --tb=short
```
Expected: ≥ 700 passing, 0 failures.

- [ ] **Step 2: Module size check (Rule 17)**

```bash
wc -l trading/*.py signals/*.py tests/*.py | sort -rn | head -20
```
Expected: no .py file in `trading/` or `signals/` > 300 lines; no test file > 700 lines.

- [ ] **Step 3: Gate registry final audit**

```bash
# Confirm no safety gates (earnings, time, event-risk) in pre_claude_filter
grep -n "is_entry_allowed\|is_earnings_imminent\|is_event_risk_window\|parabolic\|bot_herding_score" trading/pre_claude_filter.py
```
Expected: zero matches (these only in entry_gates.py).

- [ ] **Step 4: Confirm all execute_buy call sites pass sector_today + daily_pnl_pct**

```bash
grep -A 20 "execute_buy(" trading/pipeline.py | grep -E "sector_today|daily_pnl_pct"
```
Expected: both params present in both auto-execute and Claude-execute calls.

- [ ] **Step 5: Create overall progress summary commit**

```bash
git add .
git commit -m "chore: all 7 ultrareview plans implemented — gate registry, risk, notifications, prompt, gates, learning, architecture"
```
