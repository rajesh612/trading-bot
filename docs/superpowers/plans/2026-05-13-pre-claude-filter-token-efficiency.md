# Pre-Claude Filter Stack + Token Efficiency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate wasteful Claude API calls by running a 5-layer pre-Claude filter (composite gate, fast technical veto, macro health, research sentiment, intraday news) so Claude only sees genuinely ambiguous setups — cutting token spend by ~70% while unlocking the afternoon trading window.

**Architecture:** A new `CycleSnapshot` dataclass loads all daily-static data (regime, FRED, research, weights) once per cycle with a FRED TTL cache so FRED/CPI/unemployment data is read from disk at most every 2 hours. A `pre_claude_filter()` function consumes the snapshot per-symbol with zero I/O and routes each candidate to SKIP / AUTO_EXECUTE / CLAUDE. Budget time-of-day caps reserve 65% of the entry pool for the afternoon window; unused exit budget rolls over at 12:30 PM. Watch list candidates are pre-filtered for technical exhaustion at selection time.

**Tech Stack:** Python 3.11+, `threading.Lock`, `zoneinfo.ZoneInfo`, `dataclasses`, `pytest` with `@pytest.mark.unit`

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `config.py` | Modify | 13 new constants; remove 2 stale |
| `trading/cycle_context.py` | **Create** | CycleSnapshot dataclass + FRED TTL cache + `build_cycle_snapshot()` |
| `signals/entry_gates.py` | Modify | Add `fast_gate(iv)` — pure indicator-based hard veto, no I/O |
| `trading/pre_claude_filter.py` | **Create** | `pre_claude_filter(symbol, iv, snap)` — 5-layer filter, returns SKIP/AUTO_EXECUTE/CLAUDE |
| `trading/budget_guard.py` | **Create** | Time-of-day cap helpers + exit rollover helper |
| `trading/risk_manager.py` | Modify | Add `is_entry_budget_exhausted()` flag + call budget_guard in `check_entry_budget()` + `try_rollover_exit_to_entry()` |
| `watch_list_agent.py` | Modify | RSI/Stochastic pre-filter after Claude selects candidates |
| `intraday_news.py` | **Create** | 30-min RSS + StockTwits refresh; `load_intraday_news()` with staleness guard |
| `trading/pipeline.py` | Modify | Use CycleSnapshot; budget pre-check before Phase 2; call pre_claude_filter per symbol; auto-execute tier |
| `trading/claude_caller.py` | Modify | Accept `CycleSnapshot`; remove `_get_current_regime_info()` disk re-reads |
| `trading_agent.py` | Modify | Call `build_cycle_snapshot()`; simplify `run_trading_cycle()` |
| `main.py` | Modify | Add 30-min intraday news job; 12:30 PM rollover job; call `invalidate_fred_cache()` after 4 PM FRED refresh |
| `tests/test_signals.py` | Modify | `fast_gate` unit tests |
| `tests/test_pipeline.py` | **Create** | Filter, pipeline tier, budget_guard, cycle_context tests |
| `tests/test_intraday_news.py` | **Create** | Intraday news module tests |

> **Note:** `tests/test_trading.py` is already at 1272 lines (over the 1000-line limit). All new trading-related tests go in `tests/test_pipeline.py` instead.

---

## Task 1: Config Constants

**Files:**
- Modify: `config.py`

- [ ] **Step 1: Add constants under a new section header**

Open `config.py`. Add after the existing `# ── Plan …` sections:

```python
# ── Pre-Claude Filter + Token Efficiency ──────────────────────────────────────
PRE_CLAUDE_COMPOSITE_MIN      = 40    # pipeline Phase 2: below this never reaches Claude
HIGH_VOL_COMPOSITE_MIN        = 50    # raised bar in HIGH_VOL / DEFENSIVE regime
MACRO_HEALTH_SKIP_THRESHOLD   = -20   # skip if macro_health_score < this
MACRO_SHOCK_SKIP_THRESHOLD    = 50    # skip if research macro_shock_score > this
SENTIMENT_HARD_BLOCK          = -15   # combined_sentiment_score below this = hard block
SENTIMENT_WEAK_COMPOSITE_MIN  = 45    # if sentiment < 0, require composite >= this
INTRADAY_SENTIMENT_BLOCK      = -10   # intraday sentiment_score below this = skip

# ── Auto-Execute (skip Claude for textbook setups) ────────────────────────────
AUTO_EXECUTE_COMPOSITE_MIN    = 45    # composite must exceed this for auto-execute
AUTO_EXECUTE_SENTIMENT_MIN    = 15    # intraday sentiment_score must exceed this

# ── Budget Time-of-Day Caps ───────────────────────────────────────────────────
BUDGET_MORNING_CAP_PCT        = 0.35  # max 35% of entry_ceiling by 11:30 AM ET
BUDGET_MIDDAY_CAP_PCT         = 0.65  # max 65% of entry_ceiling by 1:00 PM ET
BUDGET_ROLLOVER_FRACTION      = 0.5   # fraction of unused exit budget to roll into entry
EXIT_ROLLOVER_MAX_USD         = 0.15  # cap on a single rollover
BUDGET_ROLLOVER_HOUR          = 12    # ET hour to evaluate rollover
BUDGET_ROLLOVER_MINUTE        = 30    # ET minute to evaluate rollover

# ── Intraday News ─────────────────────────────────────────────────────────────
INTRADAY_NEWS_REFRESH_MIN     = 30    # scheduler cadence in minutes
INTRADAY_NEWS_SHOCK_BLOCK     = 60    # shock_score >= this → hard-block entry
INTRADAY_NEWS_STALE_MIN       = 45    # cache older than this → treat as missing

# ── Watch List Technical Pre-Filter ──────────────────────────────────────────
WATCH_LIST_RSI_FILTER         = 70    # exclude if RSI > this at 9 AM snapshot
WATCH_LIST_STOCH_FILTER       = 80    # exclude if Stochastic %D > this at 9 AM snapshot
WATCH_LIST_MIN_AFTER_FILTER   = 6     # relax filter thresholds if fewer symbols remain
```

- [ ] **Step 2: Remove stale unwired constants**

Delete these two lines (they were never wired and are superseded):
```python
AUTO_BUY_COMPOSITE_THRESHOLD = 35
AUTO_BUY_MIN_SENTIMENT = -10
```

- [ ] **Step 3: Verify no import breaks**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -c "import config; print('ok')"
```
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add config.py
git commit -m "config: add pre-claude filter, budget caps, intraday news, watch list constants"
```

---

## Task 2: `trading/cycle_context.py` — In-Memory Cycle Snapshot

**Files:**
- Create: `trading/cycle_context.py`
- Modify: `tests/test_pipeline.py` (created fresh in this task)

**Why:** Every 10-min cycle re-reads macro_regime, FRED, research, weights from disk. FRED changes at most once per day (4 PM refresh). With a 2-hour TTL cache, disk reads for FRED drop from 36×/day to 1×/day. All other data is read once per cycle and passed through without additional re-reads.

- [ ] **Step 1: Write the failing tests (create test file)**

Create `tests/test_pipeline.py`:

```python
"""Tests for cycle_context, pre_claude_filter, budget_guard, and pipeline tiers."""
import json
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch, MagicMock
import pytest

ET = ZoneInfo("America/New_York")


# ─── CycleSnapshot / FRED TTL cache ──────────────────────────────────────────

class TestFredTTLCache:

    @pytest.mark.unit
    def test_fred_cache_not_reloaded_within_ttl(self, tmp_path):
        """Second call within TTL returns cached dict without re-reading disk."""
        from trading import cycle_context
        cycle_context._fred_cache = {}
        cycle_context._fred_loaded_at = 0.0

        fred_file = tmp_path / "fred_cache.json"
        fred_file.write_text('{"cpi_trend": "stable"}')

        with patch("trading.cycle_context.config") as mock_cfg:
            mock_cfg.FRED_CACHE_FILE = fred_file
            first = cycle_context._get_fred_cache()
            # Modify file on disk — TTL should prevent re-read
            fred_file.write_text('{"cpi_trend": "rising"}')
            second = cycle_context._get_fred_cache()

        assert first == {"cpi_trend": "stable"}
        assert second == {"cpi_trend": "stable"}  # still cached

    @pytest.mark.unit
    def test_fred_cache_reloaded_after_invalidate(self, tmp_path):
        """invalidate_fred_cache() forces reload on next access."""
        from trading import cycle_context
        cycle_context._fred_cache = {"cpi_trend": "stable"}
        cycle_context._fred_loaded_at = time.monotonic()

        fred_file = tmp_path / "fred_cache.json"
        fred_file.write_text('{"cpi_trend": "rising"}')

        cycle_context.invalidate_fred_cache()

        with patch("trading.cycle_context.config") as mock_cfg:
            mock_cfg.FRED_CACHE_FILE = fred_file
            result = cycle_context._get_fred_cache()

        assert result == {"cpi_trend": "rising"}

    @pytest.mark.unit
    def test_fred_cache_thread_safe(self, tmp_path):
        """Concurrent reads don't corrupt the cache."""
        from trading import cycle_context
        cycle_context._fred_cache = {}
        cycle_context._fred_loaded_at = 0.0

        fred_file = tmp_path / "fred_cache.json"
        fred_file.write_text('{"cpi_trend": "stable"}')
        results = []

        def reader():
            with patch("trading.cycle_context.config") as mock_cfg:
                mock_cfg.FRED_CACHE_FILE = fred_file
                results.append(cycle_context._get_fred_cache())

        threads = [threading.Thread(target=reader) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(r == {"cpi_trend": "stable"} for r in results)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest tests/test_pipeline.py -v -m unit 2>&1 | tail -5
```
Expected: `ModuleNotFoundError` or `ImportError` (cycle_context doesn't exist yet).

- [ ] **Step 3: Create `trading/cycle_context.py`**

```python
"""
CycleSnapshot: all trading-cycle data loaded once per cycle.
No I/O happens inside the pipeline — everything is pre-loaded here.

FRED data is TTL-cached (2 h) — it only changes at 4 PM post-market.
All other fields are read fresh each cycle (every 10 min is fine for
regime, research, weights).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import config
from macro_regime import load_macro_regime, load_trading_params
from utils.io_utils import load_json

# ── FRED TTL cache — stable all trading day ───────────────────────────────────
_fred_cache: dict = {}
_fred_loaded_at: float = 0.0
_FRED_TTL: float = 7200.0   # 2 hours
_FRED_LOCK = threading.Lock()


def _get_fred_cache() -> dict:
    global _fred_cache, _fred_loaded_at
    with _FRED_LOCK:
        if _fred_cache and (time.monotonic() - _fred_loaded_at) < _FRED_TTL:
            return _fred_cache
        _fred_cache = load_json(config.FRED_CACHE_FILE, {})
        _fred_loaded_at = time.monotonic()
        return _fred_cache


def invalidate_fred_cache() -> None:
    """Call after refresh_fred_cache() at 4 PM to force reload on next access."""
    global _fred_loaded_at
    with _FRED_LOCK:
        _fred_loaded_at = 0.0


# ── CycleSnapshot ─────────────────────────────────────────────────────────────

@dataclass
class CycleSnapshot:
    """
    Immutable snapshot of all data needed for one trading cycle.
    Built once at cycle start; passed through pipeline and claude_caller
    without any further disk reads.
    """
    regime_info: dict
    regime: str                   # shortcut: regime_info["regime"]
    fred_cache: dict              # TTL-cached — at most 1 disk read per 2 h
    research: dict                # research_report.json — fresh each cycle
    research_meta: dict           # research["__meta__"] convenience accessor
    strategy_memory: str
    weights: dict
    params: dict
    portfolio_gain_pct: float
    intraday_news: dict = field(default_factory=dict)  # populated in Task 9


def build_cycle_snapshot(portfolio_gain_pct: float) -> CycleSnapshot:
    """
    Load all cycle data in one place. Called once by run_trading_cycle().
    Subsequent reads within the same cycle use the returned snapshot.
    """
    regime_info = load_macro_regime()
    research    = load_json(config.RESEARCH_REPORT_FILE, {})
    weights     = load_json(config.INDICATOR_WEIGHTS_FILE, {})
    params      = load_trading_params()

    strat_path = Path(config.STRATEGY_MEMORY_FILE) if hasattr(config.STRATEGY_MEMORY_FILE, "__fspath__") \
        else Path(str(config.STRATEGY_MEMORY_FILE))
    try:
        raw_mem = strat_path.read_text()
        if len(raw_mem) > config.STRATEGY_MEMORY_MAX_CHARS:
            raw_mem = raw_mem[-config.STRATEGY_MEMORY_MAX_CHARS:]
    except FileNotFoundError:
        raw_mem = ""

    return CycleSnapshot(
        regime_info=regime_info,
        regime=regime_info.get("regime", "NORMAL"),
        fred_cache=_get_fred_cache(),
        research=research,
        research_meta=research.get("__meta__", {}),
        strategy_memory=raw_mem,
        weights=weights,
        params=params,
        portfolio_gain_pct=portfolio_gain_pct,
    )
```

- [ ] **Step 4: Run tests — expect pass**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest tests/test_pipeline.py::TestFredTTLCache -v -m unit
```
Expected: `3 passed`

- [ ] **Step 5: Commit**

```bash
git add trading/cycle_context.py tests/test_pipeline.py
git commit -m "feat: add CycleSnapshot with FRED TTL cache — eliminates per-cycle disk re-reads"
```

---

## Task 3: `fast_gate()` in `signals/entry_gates.py`

**Files:**
- Modify: `signals/entry_gates.py`
- Modify: `tests/test_signals.py`

**Why:** RSI and Stochastic hard vetos exist in `check_all_gates()` but that only runs post-Claude. `fast_gate()` exposes them as a pre-Claude check with zero I/O, fulfilling the design intent already documented in the code comment ("saves Claude tokens on conditions that will HOLD anyway").

- [ ] **Step 1: Write failing tests**

Append to `tests/test_signals.py`:

```python
# ─── fast_gate ────────────────────────────────────────────────────────────────

class TestFastGate:

    def _iv(self, **overrides):
        """Minimal IndicatorValues that passes fast_gate by default."""
        from indicators.values import IndicatorValues
        defaults = dict(
            symbol="TEST", current_price=100.0, vwap=100.0,
            vwap_std2_upper=105.0, vwap_std2_lower=95.0,
            rsi=55.0, rsi_delta=1.0, stoch_k=50.0, stoch_d=50.0,
            composite_score=50.0, atr=1.0, prev_close=99.0,
            parabolic=False, stop_hunt_zone=False,
            momentum_exhaustion=False, bot_herding_score=0,
            squeeze_setup=False, obv_divergence=False,
            volume_exhaustion=False, volume_ratio=1.0,
            macd=0.1, macd_signal=0.05, bb_upper=105.0, bb_lower=95.0,
            ema9=100.0, ema21=99.0,
        )
        defaults.update(overrides)
        return IndicatorValues(**defaults)

    @pytest.mark.unit
    def test_passes_clean_setup(self):
        from signals.entry_gates import fast_gate
        result = fast_gate(self._iv())
        assert result.passed is True

    @pytest.mark.unit
    def test_blocks_rsi_above_threshold(self):
        from signals.entry_gates import fast_gate
        import config
        result = fast_gate(self._iv(rsi=config.RSI_HARD_SKIP_THRESHOLD + 1))
        assert result.passed is False
        assert "RSI" in result.reason

    @pytest.mark.unit
    def test_blocks_stochastic_above_threshold(self):
        from signals.entry_gates import fast_gate
        import config
        result = fast_gate(self._iv(stoch_d=config.STOCH_HARD_VETO_THRESHOLD + 1))
        assert result.passed is False
        assert "Stochastic" in result.reason

    @pytest.mark.unit
    def test_blocks_rsi_delta_collapse(self):
        from signals.entry_gates import fast_gate
        import config
        result = fast_gate(self._iv(rsi_delta=config.RSI_DELTA_HARD_BLOCK - 1))
        assert result.passed is False
        assert "delta" in result.reason.lower()

    @pytest.mark.unit
    def test_blocks_parabolic(self):
        from signals.entry_gates import fast_gate
        import config
        with patch("signals.entry_gates.config") as mock_cfg:
            mock_cfg.RSI_HARD_SKIP_THRESHOLD = 72
            mock_cfg.STOCH_HARD_VETO_THRESHOLD = 88
            mock_cfg.RSI_DELTA_HARD_BLOCK = -7
            mock_cfg.PARABOLIC_ENTRY_BLOCK = True
            result = fast_gate(self._iv(parabolic=True))
        assert result.passed is False
        assert "parabolic" in result.reason

    @pytest.mark.unit
    def test_blocks_stop_hunt_zone(self):
        from signals.entry_gates import fast_gate
        import config
        result = fast_gate(self._iv(stop_hunt_zone=True))
        assert result.passed is False
        assert "stop" in result.reason.lower()
```

- [ ] **Step 2: Run to verify failure**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest tests/test_signals.py::TestFastGate -v -m unit 2>&1 | tail -5
```
Expected: `ImportError: cannot import name 'fast_gate'`

- [ ] **Step 3: Add `fast_gate()` to `signals/entry_gates.py`**

Check that `config.RSI_DELTA_HARD_BLOCK` exists — add to config.py if missing:
```python
RSI_DELTA_HARD_BLOCK = -7   # RSI falling faster than this per bar → hard skip
```

Then add to `signals/entry_gates.py` after the imports and before `check_all_gates()`:

```python
def fast_gate(iv: IndicatorValues) -> GateResult:
    """
    Pure indicator-based hard vetos — no I/O, no external data.
    Called in pipeline Phase 2 PRE-Claude to eliminate guaranteed HOLDs
    without spending a single API token.
    """
    if iv.rsi > config.RSI_HARD_SKIP_THRESHOLD:
        return GateResult(False, f"RSI hard skip (RSI={iv.rsi:.1f} > {config.RSI_HARD_SKIP_THRESHOLD})")
    if iv.stoch_d > config.STOCH_HARD_VETO_THRESHOLD:
        return GateResult(False, f"Stochastic hard veto (%D={iv.stoch_d:.1f} > {config.STOCH_HARD_VETO_THRESHOLD})")
    if iv.rsi_delta <= config.RSI_DELTA_HARD_BLOCK:
        return GateResult(False, f"RSI delta hard block ({iv.rsi_delta:.1f} <= {config.RSI_DELTA_HARD_BLOCK})")
    if config.PARABOLIC_ENTRY_BLOCK and iv.parabolic:
        return GateResult(False, "parabolic move hard veto")
    if iv.stop_hunt_zone:
        return GateResult(False, "stop-hunt zone hard veto")
    return GateResult(True)
```

- [ ] **Step 4: Run tests — expect pass**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest tests/test_signals.py::TestFastGate -v -m unit
```
Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
git add signals/entry_gates.py config.py tests/test_signals.py
git commit -m "feat: add fast_gate() pre-Claude RSI/Stoch/parabolic/stop-hunt veto"
```

---

## Task 4: `trading/pre_claude_filter.py` — 5-Layer Filter

**Files:**
- Create: `trading/pre_claude_filter.py`
- Modify: `tests/test_pipeline.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_pipeline.py`:

```python
# ─── pre_claude_filter ───────────────────────────────────────────────────────

class TestPreClaudeFilter:

    def _snap(self, **overrides) -> "CycleSnapshot":
        from trading.cycle_context import CycleSnapshot
        defaults = dict(
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
        defaults.update(overrides)
        return CycleSnapshot(**defaults)

    def _iv(self, composite=50.0, rsi=55.0, stoch_d=50.0, **kw):
        from indicators.values import IndicatorValues
        return IndicatorValues(
            symbol="TEST", current_price=100.0, vwap=100.0,
            vwap_std2_upper=105.0, vwap_std2_lower=95.0,
            rsi=rsi, rsi_delta=1.0, stoch_k=stoch_d, stoch_d=stoch_d,
            composite_score=composite, atr=1.0, prev_close=99.0,
            parabolic=False, stop_hunt_zone=False,
            momentum_exhaustion=False, bot_herding_score=0,
            squeeze_setup=False, obv_divergence=False,
            volume_exhaustion=False, volume_ratio=1.0,
            macd=0.1, macd_signal=0.05, bb_upper=105.0, bb_lower=95.0,
            ema9=100.0, ema21=99.0, **kw,
        )

    @pytest.mark.unit
    def test_skip_composite_below_minimum(self):
        from trading.pre_claude_filter import pre_claude_filter
        result = pre_claude_filter("TEST", self._iv(composite=30.0), self._snap())
        assert result.decision == "SKIP"
        assert "composite" in result.reason

    @pytest.mark.unit
    def test_skip_rsi_hard_veto(self):
        from trading.pre_claude_filter import pre_claude_filter
        result = pre_claude_filter("TEST", self._iv(composite=50.0, rsi=75.0), self._snap())
        assert result.decision == "SKIP"
        assert "RSI" in result.reason

    @pytest.mark.unit
    def test_skip_macro_shock(self):
        from trading.pre_claude_filter import pre_claude_filter
        snap = self._snap(research_meta={"macro_shock_score": 60})
        result = pre_claude_filter("TEST", self._iv(), snap)
        assert result.decision == "SKIP"
        assert "shock" in result.reason

    @pytest.mark.unit
    def test_skip_macro_health_too_low(self):
        from trading.pre_claude_filter import pre_claude_filter
        snap = self._snap(regime_info={"regime": "NORMAL", "macro_health_score": -30})
        result = pre_claude_filter("TEST", self._iv(), snap)
        assert result.decision == "SKIP"
        assert "macro health" in result.reason

    @pytest.mark.unit
    def test_skip_bearish_sentiment_hard_block(self):
        from trading.pre_claude_filter import pre_claude_filter
        snap = self._snap(research={"TEST": {"sentiment_score": -20}})
        result = pre_claude_filter("TEST", self._iv(), snap)
        assert result.decision == "SKIP"
        assert "sentiment" in result.reason

    @pytest.mark.unit
    def test_skip_negative_sentiment_weak_composite(self):
        from trading.pre_claude_filter import pre_claude_filter
        snap = self._snap(research={"TEST": {"sentiment_score": -5}})
        result = pre_claude_filter("TEST", self._iv(composite=42.0), snap)
        assert result.decision == "SKIP"

    @pytest.mark.unit
    def test_skip_intraday_shock(self):
        from trading.pre_claude_filter import pre_claude_filter
        snap = self._snap(intraday_news={"TEST": {"shock_score": 65, "sentiment_score": 10}})
        result = pre_claude_filter("TEST", self._iv(), snap)
        assert result.decision == "SKIP"
        assert "shock" in result.reason

    @pytest.mark.unit
    def test_auto_execute_strong_signal(self):
        from trading.pre_claude_filter import pre_claude_filter
        snap = self._snap(intraday_news={"TEST": {"shock_score": 0, "sentiment_score": 20}})
        result = pre_claude_filter("TEST", self._iv(composite=50.0), snap)
        assert result.decision == "AUTO_EXECUTE"

    @pytest.mark.unit
    def test_claude_path_moderate_signal(self):
        from trading.pre_claude_filter import pre_claude_filter
        # composite 42, sentiment 5 — passes all gates but not high enough for auto-execute
        snap = self._snap(intraday_news={"TEST": {"shock_score": 0, "sentiment_score": 5}})
        result = pre_claude_filter("TEST", self._iv(composite=42.0), snap)
        assert result.decision == "CLAUDE"

    @pytest.mark.unit
    def test_claude_path_no_intraday_cache(self):
        """Falls back to research sentiment when intraday cache is empty."""
        from trading.pre_claude_filter import pre_claude_filter
        snap = self._snap(
            research={"TEST": {"sentiment_score": 25}},
            intraday_news={},  # stale/missing
        )
        result = pre_claude_filter("TEST", self._iv(composite=42.0), snap)
        assert result.decision == "CLAUDE"

    @pytest.mark.unit
    def test_high_vol_regime_raises_composite_bar(self):
        from trading.pre_claude_filter import pre_claude_filter
        import config
        snap = self._snap(
            regime_info={"regime": "HIGH_VOL", "macro_health_score": 0},
            regime="HIGH_VOL",
        )
        # composite=45 passes NORMAL (>=40) but fails HIGH_VOL (>=50)
        result = pre_claude_filter("TEST", self._iv(composite=45.0), snap)
        assert result.decision == "SKIP"
```

- [ ] **Step 2: Run to verify failure**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest tests/test_pipeline.py::TestPreClaudeFilter -v -m unit 2>&1 | tail -5
```
Expected: `ModuleNotFoundError: No module named 'trading.pre_claude_filter'`

- [ ] **Step 3: Create `trading/pre_claude_filter.py`**

```python
"""
5-layer pre-Claude filter: routes each candidate to SKIP, AUTO_EXECUTE, or CLAUDE.
All data comes from CycleSnapshot — zero I/O per symbol.
"""
from __future__ import annotations

from dataclasses import dataclass

import config
from indicators.values import IndicatorValues
from signals.entry_gates import fast_gate
from trading.cycle_context import CycleSnapshot


@dataclass(frozen=True)
class FilterResult:
    decision: str   # "SKIP" | "AUTO_EXECUTE" | "CLAUDE"
    reason: str


def pre_claude_filter(symbol: str, iv: IndicatorValues, snap: CycleSnapshot) -> FilterResult:
    """
    Layer 1 — Composite minimum (regime-adjusted).
    Layer 2 — Technical fast gate (RSI, Stochastic, parabolic, stop-hunt).
    Layer 3 — Macro health score + morning shock score.
    Layer 4 — Research sentiment (9:15 AM report).
    Layer 5 — Intraday news (30-min cache; graceful fallback if empty).

    If all layers pass and signal is very strong → AUTO_EXECUTE (skip Claude).
    Otherwise → CLAUDE.
    """
    # Layer 1 — Composite minimum
    composite_min = (
        config.HIGH_VOL_COMPOSITE_MIN
        if snap.regime in ("HIGH_VOL", "DEFENSIVE")
        else config.PRE_CLAUDE_COMPOSITE_MIN
    )
    if iv.composite_score < composite_min:
        return FilterResult("SKIP",
            f"composite {iv.composite_score:+.1f} < {composite_min} ({snap.regime})")

    # Layer 2 — Technical fast gate
    fg = fast_gate(iv)
    if not fg.passed:
        return FilterResult("SKIP", fg.reason)

    # Layer 3 — Macro health + shock
    health = snap.regime_info.get("macro_health_score", 0)
    shock  = snap.research_meta.get("macro_shock_score", 0)
    if health < config.MACRO_HEALTH_SKIP_THRESHOLD:
        return FilterResult("SKIP",
            f"macro health {health} < {config.MACRO_HEALTH_SKIP_THRESHOLD}")
    if shock > config.MACRO_SHOCK_SKIP_THRESHOLD:
        return FilterResult("SKIP",
            f"macro shock {shock} > {config.MACRO_SHOCK_SKIP_THRESHOLD}")

    # Layer 4 — Research sentiment
    sentiment = snap.research.get(symbol, {}).get("sentiment_score", 0)
    if sentiment < config.SENTIMENT_HARD_BLOCK:
        return FilterResult("SKIP",
            f"sentiment {sentiment:+d} < hard block {config.SENTIMENT_HARD_BLOCK}")
    if sentiment < 0 and iv.composite_score < config.SENTIMENT_WEAK_COMPOSITE_MIN:
        return FilterResult("SKIP",
            f"negative sentiment ({sentiment:+d}) + composite {iv.composite_score:+.1f}"
            f" < {config.SENTIMENT_WEAK_COMPOSITE_MIN}")

    # Layer 5 — Intraday news (falls back to research sentiment if cache empty)
    intraday     = snap.intraday_news.get(symbol, {})
    intra_shock  = intraday.get("shock_score", 0)
    intra_sent   = intraday.get("sentiment_score", sentiment)  # fallback to research
    if intra_shock >= config.INTRADAY_NEWS_SHOCK_BLOCK:
        return FilterResult("SKIP",
            f"intraday shock {intra_shock} >= {config.INTRADAY_NEWS_SHOCK_BLOCK}")
    if intra_sent < config.INTRADAY_SENTIMENT_BLOCK:
        return FilterResult("SKIP",
            f"intraday sentiment {intra_sent:+d} < {config.INTRADAY_SENTIMENT_BLOCK}")

    # Auto-execute — all gates cleared, signal convincingly strong
    if (iv.composite_score > config.AUTO_EXECUTE_COMPOSITE_MIN
            and intra_sent > config.AUTO_EXECUTE_SENTIMENT_MIN):
        return FilterResult("AUTO_EXECUTE",
            f"composite={iv.composite_score:+.1f} intraday_sentiment={intra_sent:+d}")

    return FilterResult("CLAUDE", "")
```

- [ ] **Step 4: Run tests — expect pass**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest tests/test_pipeline.py::TestPreClaudeFilter -v -m unit
```
Expected: `11 passed`

- [ ] **Step 5: Commit**

```bash
git add trading/pre_claude_filter.py tests/test_pipeline.py
git commit -m "feat: add 5-layer pre_claude_filter — composite/RSI/macro/sentiment/news"
```

---

## Task 5: `trading/budget_guard.py` — Time-of-Day Caps + Rollover

**Files:**
- Create: `trading/budget_guard.py`
- Modify: `tests/test_pipeline.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_pipeline.py`:

```python
# ─── budget_guard ────────────────────────────────────────────────────────────

class TestBudgetGuard:

    def _now_et(self, hour, minute=0):
        return datetime(2026, 5, 13, hour, minute, tzinfo=ET)

    @pytest.mark.unit
    def test_morning_cap_blocks_at_35pct_during_morning(self):
        from trading.budget_guard import morning_cap_exceeded
        # 10:00 AM ET, spent 36% of ceiling
        result = morning_cap_exceeded(
            entry_spent=0.36, entry_ceiling=1.0, now=self._now_et(10, 0)
        )
        assert result is True

    @pytest.mark.unit
    def test_morning_cap_allows_under_35pct(self):
        from trading.budget_guard import morning_cap_exceeded
        result = morning_cap_exceeded(
            entry_spent=0.34, entry_ceiling=1.0, now=self._now_et(10, 0)
        )
        assert result is False

    @pytest.mark.unit
    def test_morning_cap_inactive_after_1130(self):
        """Cap only applies 9:30–11:30 AM ET."""
        from trading.budget_guard import morning_cap_exceeded
        result = morning_cap_exceeded(
            entry_spent=0.99, entry_ceiling=1.0, now=self._now_et(11, 45)
        )
        assert result is False

    @pytest.mark.unit
    def test_midday_cap_blocks_at_65pct(self):
        from trading.budget_guard import midday_cap_exceeded
        result = midday_cap_exceeded(
            entry_spent=0.66, entry_ceiling=1.0, now=self._now_et(12, 0)
        )
        assert result is True

    @pytest.mark.unit
    def test_midday_cap_inactive_after_1pm(self):
        from trading.budget_guard import midday_cap_exceeded
        result = midday_cap_exceeded(
            entry_spent=0.99, entry_ceiling=1.0, now=self._now_et(13, 30)
        )
        assert result is False

    @pytest.mark.unit
    def test_rollover_applied_at_1230(self):
        from trading.budget_guard import apply_exit_rollover
        import config
        b = {
            "entry_ceiling_usd": 0.70,
            "exit_ceiling_usd": 0.30,
            "exit_spent_usd": 0.0,
        }
        result = apply_exit_rollover(b, self._now_et(12, 30))
        assert result is True
        # 50% of $0.30 unused = $0.15 rolled → entry ceiling = $0.85
        assert b["entry_ceiling_usd"] == pytest.approx(0.85, abs=0.01)
        assert b["_rollover_applied"] is True

    @pytest.mark.unit
    def test_rollover_not_applied_twice(self):
        from trading.budget_guard import apply_exit_rollover
        b = {
            "entry_ceiling_usd": 0.70,
            "exit_ceiling_usd": 0.30,
            "exit_spent_usd": 0.0,
            "_rollover_applied": True,
        }
        result = apply_exit_rollover(b, self._now_et(12, 30))
        assert result is False
        assert b["entry_ceiling_usd"] == 0.70  # unchanged

    @pytest.mark.unit
    def test_rollover_not_applied_wrong_time(self):
        from trading.budget_guard import apply_exit_rollover
        b = {"entry_ceiling_usd": 0.70, "exit_ceiling_usd": 0.30, "exit_spent_usd": 0.0}
        result = apply_exit_rollover(b, self._now_et(11, 0))
        assert result is False

    @pytest.mark.unit
    def test_rollover_skipped_if_exit_already_spent(self):
        from trading.budget_guard import apply_exit_rollover
        b = {
            "entry_ceiling_usd": 0.70,
            "exit_ceiling_usd": 0.30,
            "exit_spent_usd": 0.30,  # all used
        }
        result = apply_exit_rollover(b, self._now_et(12, 30))
        assert result is False
```

- [ ] **Step 2: Run to verify failure**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest tests/test_pipeline.py::TestBudgetGuard -v -m unit 2>&1 | tail -5
```
Expected: `ModuleNotFoundError: No module named 'trading.budget_guard'`

- [ ] **Step 3: Create `trading/budget_guard.py`**

```python
"""
Time-of-day budget caps and exit-to-entry rollover.
Pure functions — all state is passed in; no disk I/O.
Imported by trading.risk_manager.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import config

ET = ZoneInfo("America/New_York")


def morning_cap_exceeded(entry_spent: float, entry_ceiling: float, now: datetime) -> bool:
    """
    True if morning spend cap is reached.
    Cap applies 9:30–11:30 AM ET only.
    """
    et = now.astimezone(ET)
    h, m = et.hour, et.minute
    in_window = (h == 9 and m >= 30) or h == 10 or (h == 11 and m < 30)
    if not in_window:
        return False
    return entry_spent >= entry_ceiling * config.BUDGET_MORNING_CAP_PCT


def midday_cap_exceeded(entry_spent: float, entry_ceiling: float, now: datetime) -> bool:
    """
    True if midday spend cap is reached.
    Cap applies 11:30 AM–1:00 PM ET only.
    """
    et = now.astimezone(ET)
    h, m = et.hour, et.minute
    in_window = (h == 11 and m >= 30) or h == 12 or (h == 13 and m == 0)
    if not in_window:
        return False
    return entry_spent >= entry_ceiling * config.BUDGET_MIDDAY_CAP_PCT


def apply_exit_rollover(b: dict, now: datetime) -> bool:
    """
    Roll a fraction of unused exit budget into entry pool at 12:30 PM ET.
    Modifies b in-place. Returns True if rollover was applied.
    Idempotent: second call on same budget dict is a no-op.
    """
    et = now.astimezone(ET)
    if et.hour != config.BUDGET_ROLLOVER_HOUR or et.minute != config.BUDGET_ROLLOVER_MINUTE:
        return False
    if b.get("_rollover_applied"):
        return False
    exit_ceil  = b.get("exit_ceiling_usd", 0.0)
    exit_spent = b.get("exit_spent_usd",   0.0)
    unused = exit_ceil - exit_spent
    if unused <= 0:
        return False
    rollover = min(unused * config.BUDGET_ROLLOVER_FRACTION, config.EXIT_ROLLOVER_MAX_USD)
    b["entry_ceiling_usd"] = round(b.get("entry_ceiling_usd", 0.0) + rollover, 4)
    b["_rollover_applied"] = True
    return True
```

- [ ] **Step 4: Run tests — expect pass**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest tests/test_pipeline.py::TestBudgetGuard -v -m unit
```
Expected: `9 passed`

- [ ] **Step 5: Commit**

```bash
git add trading/budget_guard.py tests/test_pipeline.py
git commit -m "feat: add budget_guard — time-of-day caps and exit-to-entry rollover"
```

---

## Task 6: Wire `budget_guard` into `trading/risk_manager.py`

**Files:**
- Modify: `trading/risk_manager.py`
- Modify: `tests/test_pipeline.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_pipeline.py`:

```python
# ─── risk_manager additions ───────────────────────────────────────────────────

class TestRiskManagerAdditions:

    @pytest.mark.unit
    def test_is_entry_budget_exhausted_false_by_default(self):
        from trading import risk_manager
        risk_manager._entry_exhausted_flag = False
        assert risk_manager.is_entry_budget_exhausted() is False

    @pytest.mark.unit
    def test_is_entry_budget_exhausted_true_after_set(self):
        from trading import risk_manager
        risk_manager._entry_exhausted_flag = True
        assert risk_manager.is_entry_budget_exhausted() is True
        risk_manager._entry_exhausted_flag = False  # cleanup

    @pytest.mark.unit
    def test_try_rollover_writes_to_json(self, tmp_path):
        from trading import risk_manager
        import json
        budget = {
            "date": "2026-05-13",
            "entry_ceiling_usd": 0.70,
            "exit_ceiling_usd": 0.30,
            "exit_spent_usd": 0.0,
            "entry_spent_usd": 0.60,
        }
        budget_file = tmp_path / "api_budget.json"
        budget_file.write_text(json.dumps(budget))

        now = datetime(2026, 5, 13, 12, 30, tzinfo=ET)
        with patch("trading.risk_manager.config") as mc:
            mc.API_BUDGET_FILE = budget_file
            mc.BUDGET_ROLLOVER_HOUR = 12
            mc.BUDGET_ROLLOVER_MINUTE = 30
            mc.BUDGET_ROLLOVER_FRACTION = 0.5
            mc.EXIT_ROLLOVER_MAX_USD = 0.15
            from trading.risk_manager import try_rollover_exit_to_entry
            try_rollover_exit_to_entry(now)

        updated = json.loads(budget_file.read_text())
        assert updated["entry_ceiling_usd"] == pytest.approx(0.85, abs=0.01)
```

- [ ] **Step 2: Run to verify failure**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest tests/test_pipeline.py::TestRiskManagerAdditions -v -m unit 2>&1 | tail -5
```
Expected: `AttributeError: module 'trading.risk_manager' has no attribute '_entry_exhausted_flag'`

- [ ] **Step 3: Add to `trading/risk_manager.py`**

Add at the top of `risk_manager.py` after existing imports:

```python
from trading.budget_guard import morning_cap_exceeded, midday_cap_exceeded, apply_exit_rollover
```

Add module-level flag after existing module-level variables:

```python
_entry_exhausted_flag: bool = False
```

Add these two functions after `record_usage()`:

```python
def is_entry_budget_exhausted() -> bool:
    """O(1) check — no disk I/O. Set by check_entry_budget() when ceiling hit."""
    return _entry_exhausted_flag


def try_rollover_exit_to_entry(now: datetime) -> None:
    """
    Called at 12:30 PM ET by scheduler. Rolls unused exit budget into entry pool.
    Writes result to api_budget.json if rollover applied.
    """
    global _entry_exhausted_flag
    b = load_budget()
    if apply_exit_rollover(b, now):
        _save_budget(b)
        if _entry_exhausted_flag:
            _entry_exhausted_flag = False
            b["entry_exhausted"] = False
            _save_budget(b)
        logger.info(
            "Exit-to-entry rollover applied: entry_ceiling now $%.4f",
            b["entry_ceiling_usd"],
        )
```

In `check_entry_budget()`, find the block where `entry_exhausted` is set and add flag update:

```python
# After: b["entry_exhausted"] = True
global _entry_exhausted_flag
_entry_exhausted_flag = True
```

In `check_entry_budget()`, add time-of-day cap checks BEFORE the existing ceiling check. Find the line that reads the entry spend and add:

```python
from datetime import datetime as _dt
from zoneinfo import ZoneInfo as _ZI
_now = _dt.now(_ZI("America/New_York"))
if morning_cap_exceeded(entry_spent, entry_ceil, _now):
    logger.warning(
        "Morning budget cap reached: $%.4f / $%.4f (35%% of $%.4f) — preserving afternoon window.",
        entry_spent, entry_ceil * config.BUDGET_MORNING_CAP_PCT, entry_ceil,
    )
    return False
if midday_cap_exceeded(entry_spent, entry_ceil, _now):
    logger.warning(
        "Midday budget cap reached: $%.4f / $%.4f (65%% of $%.4f).",
        entry_spent, entry_ceil * config.BUDGET_MIDDAY_CAP_PCT, entry_ceil,
    )
    return False
```

- [ ] **Step 4: Run tests — expect pass**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest tests/test_pipeline.py::TestRiskManagerAdditions -v -m unit
```
Expected: `3 passed`

- [ ] **Step 5: Run full test suite — no regressions**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest tests/ -v -m unit --tb=short 2>&1 | tail -15
```
Expected: All prior tests still passing.

- [ ] **Step 6: Commit**

```bash
git add trading/risk_manager.py tests/test_pipeline.py
git commit -m "feat: wire budget_guard into risk_manager — time caps, rollover, exhaustion flag"
```

---

## Task 7: Watch List Technical Pre-Filter

**Files:**
- Modify: `watch_list_agent.py`
- Modify: `tests/test_pipeline.py`

**Why:** The watch list agent runs at 9:00 AM and picks 10–15 tickers with no technical check. On May 13 the entire list had Stochastic >88 at open. Pre-filtering at selection time prevents an entire day of guaranteed-HOLD calls.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_pipeline.py`:

```python
# ─── watch list filter ────────────────────────────────────────────────────────

class TestWatchListFilter:

    @pytest.mark.unit
    def test_filters_overbought_symbols(self):
        from watch_list_agent import _apply_technical_filter
        snapshots = {
            "NVDA": {"rsi": 75.0, "stoch_d": 60.0},   # RSI too high → out
            "AAPL": {"rsi": 55.0, "stoch_d": 85.0},   # Stoch too high → out
            "MSFT": {"rsi": 55.0, "stoch_d": 55.0},   # clean → in
            "AMZN": {"rsi": 60.0, "stoch_d": 65.0},   # clean → in
        }
        result = _apply_technical_filter(
            ["NVDA", "AAPL", "MSFT", "AMZN"], snapshots,
            rsi_limit=70, stoch_limit=80,
        )
        assert "MSFT" in result
        assert "AMZN" in result
        assert "NVDA" not in result
        assert "AAPL" not in result

    @pytest.mark.unit
    def test_relaxes_thresholds_if_too_few_pass(self):
        """If < WATCH_LIST_MIN_AFTER_FILTER pass, retry with relaxed limits."""
        from watch_list_agent import _apply_technical_filter
        # All overbought at 70/80 but clean at 76/87
        snapshots = {s: {"rsi": 72.0, "stoch_d": 83.0} for s in ["A", "B", "C", "D", "E", "F"]}
        result = _apply_technical_filter(
            list(snapshots.keys()), snapshots,
            rsi_limit=70, stoch_limit=80,
        )
        # relaxed: rsi_limit=76, stoch_limit=87 → all pass
        assert len(result) >= 6

    @pytest.mark.unit
    def test_missing_snapshot_treated_as_clean(self):
        """Symbol with no snapshot data is not filtered out."""
        from watch_list_agent import _apply_technical_filter
        snapshots = {}  # no data
        result = _apply_technical_filter(["NVDA"], snapshots, rsi_limit=70, stoch_limit=80)
        assert "NVDA" in result
```

- [ ] **Step 2: Run to verify failure**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest tests/test_pipeline.py::TestWatchListFilter -v -m unit 2>&1 | tail -5
```
Expected: `ImportError: cannot import name '_apply_technical_filter'`

- [ ] **Step 3: Add `_apply_technical_filter()` to `watch_list_agent.py`**

Add this function before `run_watch_list_agent()`:

```python
def _apply_technical_filter(
    symbols: list[str],
    snapshots: dict[str, dict],
    rsi_limit: float,
    stoch_limit: float,
) -> list[str]:
    """
    Remove symbols that are already technically exhausted at watch-list selection time.
    If fewer than WATCH_LIST_MIN_AFTER_FILTER survive, retry with relaxed thresholds
    (+6 RSI, +7 Stochastic) so the list is never catastrophically short.
    """
    def _passes(sym: str, rsi_lim: float, stoch_lim: float) -> bool:
        snap = snapshots.get(sym, {})
        if not snap:
            return True  # no data → assume clean
        return snap.get("rsi", 0) <= rsi_lim and snap.get("stoch_d", 0) <= stoch_lim

    filtered = [s for s in symbols if _passes(s, rsi_limit, stoch_limit)]
    if len(filtered) < config.WATCH_LIST_MIN_AFTER_FILTER:
        filtered = [s for s in symbols if _passes(s, rsi_limit + 6, stoch_limit + 7)]
    return filtered
```

Then modify `run_watch_list_agent()` to call it after building the watch list. Find where `watch_list` is finalized (after `watch_list = watch_list[:config.WATCH_LIST_MAX]`) and add:

```python
# Fetch quick RSI/Stoch snapshot per symbol (5-min bars, last bar only)
from alpaca_client import AlpacaClient
from indicators.engine import compute_indicators
_alpaca = AlpacaClient()
_snapshots: dict[str, dict] = {}
for _sym in watch_list:
    try:
        _bars = _alpaca.get_intraday_bars(_sym)
        _iv = compute_indicators(_sym, _bars)
        if _iv:
            _snapshots[_sym] = {"rsi": _iv.rsi, "stoch_d": _iv.stoch_d}
    except Exception:
        pass  # missing snapshot → symbol kept (see _apply_technical_filter)

watch_list = _apply_technical_filter(
    watch_list, _snapshots,
    rsi_limit=config.WATCH_LIST_RSI_FILTER,
    stoch_limit=config.WATCH_LIST_STOCH_FILTER,
)
logger.info("Watch list after technical filter (%d stocks): %s", len(watch_list), watch_list)
```

- [ ] **Step 4: Run tests — expect pass**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest tests/test_pipeline.py::TestWatchListFilter -v -m unit
```
Expected: `3 passed`

- [ ] **Step 5: Commit**

```bash
git add watch_list_agent.py tests/test_pipeline.py
git commit -m "feat: watch list technical pre-filter removes overbought candidates at selection"
```

---

## Task 8: `intraday_news.py` — 30-Minute News Cache

**Files:**
- Create: `intraday_news.py`
- Create: `tests/test_intraday_news.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_intraday_news.py`:

```python
"""Unit tests for intraday_news module."""
import json
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
import pytest


class TestLoadIntradayNews:

    @pytest.mark.unit
    def test_returns_empty_dict_when_file_missing(self, tmp_path):
        from intraday_news import load_intraday_news
        with patch("intraday_news.config") as mc:
            mc.INTRADAY_NEWS_CACHE_FILE = tmp_path / "missing.json"
            mc.INTRADAY_NEWS_STALE_MIN = 45
            result = load_intraday_news()
        assert result == {}

    @pytest.mark.unit
    def test_returns_empty_dict_when_stale(self, tmp_path):
        from intraday_news import load_intraday_news
        stale_time = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()
        cache = {"__updated_at": stale_time, "NVDA": {"sentiment_score": 10, "shock_score": 0}}
        cache_file = tmp_path / "intraday_news_cache.json"
        cache_file.write_text(json.dumps(cache))
        with patch("intraday_news.config") as mc:
            mc.INTRADAY_NEWS_CACHE_FILE = cache_file
            mc.INTRADAY_NEWS_STALE_MIN = 45
            result = load_intraday_news()
        assert result == {}

    @pytest.mark.unit
    def test_returns_cache_when_fresh(self, tmp_path):
        from intraday_news import load_intraday_news
        fresh_time = datetime.now(timezone.utc).isoformat()
        cache = {
            "__updated_at": fresh_time,
            "NVDA": {"sentiment_score": 22, "shock_score": 0, "headlines": ["NVDA up"]},
        }
        cache_file = tmp_path / "intraday_news_cache.json"
        cache_file.write_text(json.dumps(cache))
        with patch("intraday_news.config") as mc:
            mc.INTRADAY_NEWS_CACHE_FILE = cache_file
            mc.INTRADAY_NEWS_STALE_MIN = 45
            result = load_intraday_news()
        assert result["NVDA"]["sentiment_score"] == 22
        assert "__updated_at" not in result  # meta key stripped


class TestComputeSymbolSentiment:

    @pytest.mark.unit
    def test_positive_keywords_raise_score(self):
        from intraday_news import _compute_symbol_sentiment
        headlines = [{"title": "NVDA beats earnings record upgrade"}]
        stocktwits = {"bullish_percent": 70}
        score = _compute_symbol_sentiment(headlines, stocktwits)
        assert score > 0

    @pytest.mark.unit
    def test_negative_keywords_lower_score(self):
        from intraday_news import _compute_symbol_sentiment
        headlines = [{"title": "TSLA recall warning downgrade cut"}]
        stocktwits = {"bullish_percent": 30}
        score = _compute_symbol_sentiment(headlines, stocktwits)
        assert score < 0

    @pytest.mark.unit
    def test_empty_inputs_return_zero(self):
        from intraday_news import _compute_symbol_sentiment
        assert _compute_symbol_sentiment([], {}) == 0
```

- [ ] **Step 2: Run to verify failure**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest tests/test_intraday_news.py -v -m unit 2>&1 | tail -5
```
Expected: `ModuleNotFoundError: No module named 'intraday_news'`

- [ ] **Step 3: Create `intraday_news.py`**

```python
"""
Intraday news cache — refreshed every 30 min by scheduler.
No Claude calls. Uses existing fetchers from research/fetchers.py.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

import config
from research.fetchers import _fetch_stock_news, _fetch_stocktwits
from research.analyzer import _compute_macro_shock_score
from utils.io_utils import load_json, save_json

logger = logging.getLogger(__name__)

_POSITIVE_KEYWORDS = {"beat", "upgrade", "approval", "surge", "record", "strong", "breakout", "raised"}
_NEGATIVE_KEYWORDS = {"miss", "downgrade", "recall", "warning", "cut", "weak", "fail", "suspend", "fraud"}


def _compute_symbol_sentiment(headlines: list[dict], stocktwits: dict) -> int:
    """
    Keyword-weighted score: +5 per positive hit, -5 per negative hit,
    plus StockTwits bull ratio scaled to [-15, +15].
    Clamped to [-100, +100].
    """
    if not headlines and not stocktwits:
        return 0

    text = " ".join(
        (h.get("title", "") + " " + h.get("summary", "")).lower()
        for h in headlines
    )
    score = 0
    for kw in _POSITIVE_KEYWORDS:
        if kw in text:
            score += 5
    for kw in _NEGATIVE_KEYWORDS:
        if kw in text:
            score -= 5

    bull_pct = stocktwits.get("bullish_percent", 50)
    score += round((bull_pct - 50) * 0.3)   # 50% neutral = 0, 100% bullish = +15

    return max(-100, min(100, score))


def refresh_intraday_news(watch_list: list[str]) -> None:
    """
    Fetch fresh headlines + StockTwits for each symbol.
    Writes intraday_news_cache.json. No Claude calls.
    """
    cache: dict = {"__updated_at": datetime.now(timezone.utc).isoformat()}
    for symbol in watch_list:
        try:
            headlines  = _fetch_stock_news(symbol)
            stocktwits = _fetch_stocktwits(symbol)
            shock      = _compute_macro_shock_score(headlines)
            sentiment  = _compute_symbol_sentiment(headlines, stocktwits)
            top3       = [h.get("title", "") for h in headlines[:3]]
            cache[symbol] = {
                "sentiment_score": sentiment,
                "shock_score":     shock,
                "headlines":       top3,
            }
        except Exception as exc:
            logger.warning("intraday_news: failed to fetch %s: %s", symbol, exc)
            cache[symbol] = {"sentiment_score": 0, "shock_score": 0, "headlines": []}

    save_json(config.INTRADAY_NEWS_CACHE_FILE, cache)
    logger.info("Intraday news cache updated for %d symbols.", len(watch_list))


def load_intraday_news() -> dict:
    """
    Load cache. Returns {} (not an error) if file missing or older than
    INTRADAY_NEWS_STALE_MIN minutes — callers fall back to research sentiment.
    Strips __updated_at meta key from returned dict.
    """
    raw = load_json(config.INTRADAY_NEWS_CACHE_FILE, {})
    if not raw:
        return {}

    ts_str = raw.get("__updated_at")
    if ts_str:
        try:
            updated = datetime.fromisoformat(ts_str)
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            age_min = (datetime.now(timezone.utc) - updated).total_seconds() / 60
            if age_min > config.INTRADAY_NEWS_STALE_MIN:
                logger.debug("Intraday news cache stale (%.0f min) — treating as missing.", age_min)
                return {}
        except Exception:
            return {}

    return {k: v for k, v in raw.items() if k != "__updated_at"}
```

Add `INTRADAY_NEWS_CACHE_FILE` to `config.py` (in the file paths section):
```python
INTRADAY_NEWS_CACHE_FILE = BASE_DIR / "intraday_news_cache.json"
```

- [ ] **Step 4: Run tests — expect pass**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest tests/test_intraday_news.py -v -m unit
```
Expected: `5 passed`

- [ ] **Step 5: Commit**

```bash
git add intraday_news.py tests/test_intraday_news.py config.py
git commit -m "feat: add intraday_news module — 30-min RSS+StockTwits cache, no Claude"
```

---

## Task 9: Add Intraday News to `CycleSnapshot`

**Files:**
- Modify: `trading/cycle_context.py`
- Modify: `tests/test_pipeline.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_pipeline.py`:

```python
class TestCycleSnapshotIntradayNews:

    @pytest.mark.unit
    def test_build_snapshot_includes_intraday_news(self, tmp_path):
        """build_cycle_snapshot() populates intraday_news from cache."""
        import json
        from datetime import timezone
        from trading.cycle_context import build_cycle_snapshot, invalidate_fred_cache

        fresh_ts = datetime.now(timezone.utc).isoformat()
        news_cache = {"__updated_at": fresh_ts, "NVDA": {"sentiment_score": 15, "shock_score": 0, "headlines": []}}
        news_file = tmp_path / "intraday_news_cache.json"
        news_file.write_text(json.dumps(news_cache))

        with patch("trading.cycle_context.load_macro_regime", return_value={"regime": "NORMAL", "macro_health_score": 0}), \
             patch("trading.cycle_context.load_trading_params", return_value={}), \
             patch("trading.cycle_context.load_json", side_effect=lambda f, d: {} if "fred" not in str(f).lower() else d), \
             patch("trading.cycle_context.load_intraday_news", return_value={"NVDA": {"sentiment_score": 15, "shock_score": 0}}):
            snap = build_cycle_snapshot(portfolio_gain_pct=0.0)

        assert snap.intraday_news.get("NVDA", {}).get("sentiment_score") == 15
```

- [ ] **Step 2: Run to verify failure**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest tests/test_pipeline.py::TestCycleSnapshotIntradayNews -v -m unit 2>&1 | tail -5
```
Expected: Test fails — `intraday_news` field is empty.

- [ ] **Step 3: Wire `load_intraday_news()` into `build_cycle_snapshot()`**

In `trading/cycle_context.py`, add import:
```python
from intraday_news import load_intraday_news
```

In `build_cycle_snapshot()`, add `intraday_news=load_intraday_news()` to the `CycleSnapshot(...)` constructor call.

- [ ] **Step 4: Run test — expect pass**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest tests/test_pipeline.py::TestCycleSnapshotIntradayNews -v -m unit
```
Expected: `1 passed`

- [ ] **Step 5: Commit**

```bash
git add trading/cycle_context.py tests/test_pipeline.py
git commit -m "feat: wire intraday_news into CycleSnapshot"
```

---

## Task 10: Rewire `trading/pipeline.py`

**Files:**
- Modify: `trading/pipeline.py`
- Modify: `tests/test_pipeline.py`

This is the critical task. It replaces the single `composite_score > 15` check with:
1. Budget pre-check (skip Phase 2/3 entirely when exhausted)
2. `pre_claude_filter()` per symbol → SKIP / AUTO_EXECUTE / CLAUDE
3. Auto-execute path for strong signals
4. Intraday news shock trigger for exit evaluation
5. Pass `CycleSnapshot` through to `claude_caller`

- [ ] **Step 1: Write failing pipeline tier tests**

Append to `tests/test_pipeline.py`:

```python
class TestPipelinePhase2Tiers:

    def _make_iv(self, symbol, composite, rsi=55.0, stoch_d=50.0, current_price=100.0):
        from indicators.values import IndicatorValues
        return IndicatorValues(
            symbol=symbol, current_price=current_price, vwap=100.0,
            vwap_std2_upper=110.0, vwap_std2_lower=90.0,
            rsi=rsi, rsi_delta=1.0, stoch_k=stoch_d, stoch_d=stoch_d,
            composite_score=composite, atr=1.0, prev_close=99.0,
            parabolic=False, stop_hunt_zone=False,
            momentum_exhaustion=False, bot_herding_score=0,
            squeeze_setup=False, obv_divergence=False,
            volume_exhaustion=False, volume_ratio=1.0,
            macd=0.1, macd_signal=0.05, bb_upper=110.0, bb_lower=90.0,
            ema9=100.0, ema21=99.0,
        )

    def _snap(self, research=None, intraday_news=None):
        from trading.cycle_context import CycleSnapshot
        return CycleSnapshot(
            regime_info={"regime": "NORMAL", "macro_health_score": 0},
            regime="NORMAL",
            fred_cache={},
            research=research or {"TEST": {"sentiment_score": 25}},
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
    def test_budget_exhausted_skips_claude_phase(self):
        from trading.pipeline import TradingPipeline
        from trading import risk_manager
        risk_manager._entry_exhausted_flag = True
        pipeline = TradingPipeline()
        # Should return immediately without calling Claude
        with patch("trading.pipeline._ask_claude_batch") as mock_claude:
            result = pipeline.run(
                symbols=["TEST"], snap=self._snap(),
                ai=MagicMock(), now=datetime(2026,5,13,10,0,tzinfo=ET),
                alpaca=MagicMock(), portfolio_value=10000, buying_power=5000,
                can_open=True,
            )
        mock_claude.assert_not_called()
        risk_manager._entry_exhausted_flag = False

    @pytest.mark.unit
    def test_low_composite_skipped_no_claude(self):
        from trading.pipeline import TradingPipeline
        iv = self._make_iv("TEST", composite=25.0)
        with patch("trading.pipeline._fetch_symbol_data", return_value=("TEST", iv, None, 0.0)), \
             patch("trading.pipeline._ask_claude_batch") as mock_claude:
            TradingPipeline().run(
                symbols=["TEST"], snap=self._snap(),
                ai=MagicMock(), now=datetime(2026,5,13,10,0,tzinfo=ET),
                alpaca=MagicMock(), portfolio_value=10000, buying_power=5000,
                can_open=True,
            )
        mock_claude.assert_not_called()

    @pytest.mark.unit
    def test_auto_execute_tier_skips_claude(self):
        from trading.pipeline import TradingPipeline
        iv = self._make_iv("TEST", composite=50.0)
        snap = self._snap(intraday_news={"TEST": {"sentiment_score": 25, "shock_score": 0}})
        with patch("trading.pipeline._fetch_symbol_data", return_value=("TEST", iv, None, 0.0)), \
             patch("trading.pipeline._ask_claude_batch") as mock_claude, \
             patch("trading.pipeline.execute_buy", return_value={"qty": 10, "entry_price": 100.0}), \
             patch("trading.pipeline.check_all_gates", return_value=MagicMock(passed=True, flags=())):
            TradingPipeline().run(
                symbols=["TEST"], snap=snap,
                ai=MagicMock(), now=datetime(2026,5,13,10,0,tzinfo=ET),
                alpaca=MagicMock(), portfolio_value=10000, buying_power=5000,
                can_open=True,
            )
        mock_claude.assert_not_called()
```

- [ ] **Step 2: Run to verify failure**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest tests/test_pipeline.py::TestPipelinePhase2Tiers -v -m unit 2>&1 | tail -5
```
Expected: Failures due to new `snap` parameter not yet in `TradingPipeline.run()`.

- [ ] **Step 3: Rewrite `trading/pipeline.py` Phase 2**

Replace Phase 2 in `TradingPipeline.run()`. The method signature changes to accept `snap: CycleSnapshot` instead of the individual `research, strategy_memory, weights, fred_cache, regime_info, params` parameters.

New `run()` signature:
```python
def run(
    self,
    symbols: list[str],
    snap: CycleSnapshot,
    ai: anthropic.Anthropic,
    now: datetime,
    alpaca: AlpacaClient,
    portfolio_value: float,
    buying_power: float,
    can_open: bool,
) -> CycleResult:
```

New Phase 2 (replaces the existing gate check block):
```python
# ── Phase 2: Pre-Claude filter (budget pre-check + 5-layer filter) ────────────
t_gates = time.monotonic()
needs_claude:   list[tuple[str, IndicatorValues, dict | None]] = []
auto_execute:   list[tuple[str, IndicatorValues]] = []

if risk_manager.is_entry_budget_exhausted():
    logger.info("Entry budget exhausted — skipping Claude phase entirely.")
else:
    for symbol, iv, existing_pos, _ret5d in stock_data:
        if existing_pos:
            pnl_pct = (iv.current_price - existing_pos["entry_price"]) / existing_pos["entry_price"] * 100
            logger.info("BRACKET-MANAGED %s: P&L=%+.2f%% composite=%+.1f", symbol, pnl_pct, iv.composite_score)
            continue
        filt = pre_claude_filter(symbol, iv, snap)
        if filt.decision == "SKIP":
            logger.info("SKIP-PRECLAUDE %s: composite=%+.1f — %s", symbol, iv.composite_score, filt.reason)
        elif filt.decision == "AUTO_EXECUTE":
            auto_execute.append((symbol, iv))
            logger.info("AUTO-EXECUTE %s: composite=%+.1f — %s", symbol, iv.composite_score, filt.reason)
        else:
            needs_claude.append((symbol, iv, None))
```

Add import at top of pipeline.py:
```python
from trading.cycle_context import CycleSnapshot
from trading.pre_claude_filter import pre_claude_filter
```

In Phase 3, update the `_ask_claude_batch` call to pass `snap` instead of individual params:
```python
batch_decisions = _ask_claude_batch(batch, snap, ai)
```

In Phase 4, add auto-execute execution before the Claude decisions loop:
```python
# ── Phase 4a: Auto-execute (no Claude) ───────────────────────────────────────
for symbol, iv in auto_execute:
    if not can_open or buying_power < config.MIN_BUYING_POWER:
        continue
    try:
        pos_data = execute_buy(
            symbol=symbol, iv=iv,
            decision={"action": "BUY", "confidence": 90,
                      "reasoning": "auto-execute: composite+sentiment threshold met",
                      "stop_loss_pct": config.STOP_LOSS_PCT,
                      "take_profit_pct": config.TAKE_PROFIT_PCT,
                      "source": "auto_execute"},
            regime=snap.regime, regime_info=snap.regime_info,
            research=snap.research, open_positions=open_positions,
            alpaca=alpaca, portfolio_value=portfolio_value,
            pos_mult=pos_mult, spy_ret5d=spy_ret5d,
            symbol_ret5d=0.0, rsi_max=rsi_max,
            params=snap.params, now=now,
            entries_this_cycle=entries_this_cycle,
        )
        if pos_data is not None:
            entries_this_cycle += 1
            result.entries += 1
            buying_power -= iv.current_price * pos_data["qty"]
    except Exception as exc:
        logger.error("Auto-execute error for %s: %s", symbol, exc, exc_info=True)
```

Also add intraday shock trigger to Phase 2.5 exit candidates:
```python
# Intraday shock trigger — add to exit eval regardless of composite drop
for symbol, iv, existing_pos, _ret5d in stock_data:
    if existing_pos is not None:
        intra = snap.intraday_news.get(symbol, {})
        if intra.get("shock_score", 0) >= config.INTRADAY_NEWS_SHOCK_BLOCK:
            if (symbol, iv, existing_pos) not in exit_candidates:
                exit_candidates.append((symbol, iv, existing_pos))
                logger.info("EXIT-EVAL %s: intraday shock %d", symbol, intra["shock_score"])
```

- [ ] **Step 4: Run pipeline tests — expect pass**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest tests/test_pipeline.py -v -m unit
```
Expected: All pipeline tests pass.

- [ ] **Step 5: Commit**

```bash
git add trading/pipeline.py tests/test_pipeline.py
git commit -m "feat: rewire pipeline — 5-layer pre-Claude filter, auto-execute tier, budget pre-check"
```

---

## Task 11: Refactor `trading/claude_caller.py` — Remove Redundant Disk Reads

**Files:**
- Modify: `trading/claude_caller.py`

**Why:** `_ask_claude_batch()` calls `_get_current_regime_info()` which reads `macro_regime.json` from disk again even though `regime_info` is already in `CycleSnapshot`. Removing this eliminates 36+ redundant disk reads per trading day.

- [ ] **Step 1: Update `_ask_claude_batch()` signature to accept `CycleSnapshot`**

Change the function signature from:
```python
def _ask_claude_batch(
    batch, research, strategy_memory, weights, ai, fred_cache, research_meta,
) -> dict[str, dict]:
```
to:
```python
def _ask_claude_batch(
    batch: list[tuple[str, IndicatorValues, dict | None]],
    snap: CycleSnapshot,
    ai: anthropic.Anthropic,
) -> dict[str, dict]:
```

Add import at top:
```python
from trading.cycle_context import CycleSnapshot
```

- [ ] **Step 2: Replace all uses of individual params with `snap.*` fields**

Inside `_ask_claude_batch()`:
- `research` → `snap.research`
- `strategy_memory` → `snap.strategy_memory`
- `weights` → `snap.weights`
- `fred_cache` → `snap.fred_cache`
- `research_meta` → `snap.research_meta`
- `regime = _get_current_regime()` → `regime = snap.regime`
- `_get_cached_stable_context(fred_cache, research_meta)` → `_get_cached_stable_context(snap.fred_cache, snap.research_meta, snap.regime_info)`
- `gain_pct = _get_portfolio_intraday_gain()` → `gain_pct = snap.portfolio_gain_pct`

Update `_get_cached_stable_context()` to accept `regime_info` instead of calling `_get_current_regime_info()`:
```python
def _get_cached_stable_context(fred_cache: dict, research_meta: dict, regime_info: dict) -> str:
    global _stable_ctx_text, _stable_ctx_time
    with _STABLE_CTX_LOCK:
        if _stable_ctx_text and (time.monotonic() - _stable_ctx_time) < _STABLE_CTX_TTL:
            return _stable_ctx_text
        _stable_ctx_text = build_stable_system_context(regime_info, fred_cache, research_meta)
        _stable_ctx_time = time.monotonic()
        return _stable_ctx_text
```

Similarly update `_ask_claude_exit_batch()` to accept `snap: CycleSnapshot` instead of `research` and replace `_get_current_regime()` with `snap.regime`.

Remove now-unused functions: `_get_current_regime()`, `_get_current_regime_info()`, `_get_portfolio_intraday_gain()`.

- [ ] **Step 3: Verify file stays under 300 lines**

```bash
wc -l ~/Documents/projects/trading-bot/trading/claude_caller.py
```
Expected: Under 300. (Removing 3 functions + simplifying parameter passing frees ~40 lines.)

- [ ] **Step 4: Run full test suite**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest tests/ -v -m unit --tb=short 2>&1 | tail -20
```
Expected: All tests passing.

- [ ] **Step 5: Commit**

```bash
git add trading/claude_caller.py
git commit -m "refactor: claude_caller accepts CycleSnapshot — removes 36+ disk re-reads per day"
```

---

## Task 12: Update `trading_agent.py` — Use `build_cycle_snapshot()`

**Files:**
- Modify: `trading_agent.py`

- [ ] **Step 1: Add imports and replace per-cycle loads**

Add import:
```python
from trading.cycle_context import build_cycle_snapshot
```

In `run_trading_cycle()`, replace the block that loads regime_info, params, fred_cache, research, weights, strategy_memory individually:

```python
# Before (remove these lines):
regime_info = load_macro_regime()
params      = load_trading_params()
try:
    fred_cache = load_json(config.FRED_CACHE_FILE, {})
except Exception:
    fred_cache = {}
research        = load_research()
weights         = load_weights()
strategy_memory = (...)

# After (single call):
snap = build_cycle_snapshot(portfolio_gain_pct=gain_pct)
```

Where `gain_pct` is already computed from account equity above.

Update the `TradingPipeline().run()` call to match the new signature:
```python
result = TradingPipeline().run(
    symbols=watch_list,
    snap=snap,
    ai=ai,
    now=now,
    regime_info=snap.regime_info,   # still needed for can_open CRISIS check above
    alpaca=alpaca,
    portfolio_value=portfolio_value,
    buying_power=buying_power,
    can_open=can_open,
)
```

Remove `load_research()` and `load_weights()` local function definitions if they're only used here (they move to cycle_context.py).

- [ ] **Step 2: Smoke test**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -c "
from trading_agent import run_trading_cycle
print('import ok')
"
```
Expected: `import ok`

- [ ] **Step 3: Run full test suite**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest tests/ -v -m unit --tb=short 2>&1 | tail -20
```
Expected: All tests passing.

- [ ] **Step 4: Commit**

```bash
git add trading_agent.py
git commit -m "refactor: trading_agent uses build_cycle_snapshot — single load point per cycle"
```

---

## Task 13: Wire Scheduler Jobs in `main.py`

**Files:**
- Modify: `main.py`

- [ ] **Step 1: Add intraday news refresh job**

In `main.py`, add import at the top alongside the other scheduler_jobs imports:
```python
from intraday_news import refresh_intraday_news
```

`scheduler_jobs.py` already reads `config.WATCH_LIST_FILE` in two places (lines 48, 93) — use the same pattern. Add a module-level helper in `scheduler_jobs.py` near the top (after imports):
```python
def _read_watch_list() -> list[str]:
    try:
        return [s.strip() for s in config.WATCH_LIST_FILE.read_text().splitlines() if s.strip()]
    except FileNotFoundError:
        return []
```

Add a job function to `scheduler_jobs.py`:
```python
def job_intraday_news():
    _job_guard("intraday_news", lambda: refresh_intraday_news(_read_watch_list()))
```

Export it in `main.py`'s import from `scheduler_jobs`:
```python
from scheduler_jobs import (
    ...
    job_intraday_news,
)
```

Add scheduler job in `main.py` using `CronTrigger` (same pattern as `job_trading` which uses `minute="*/10"`):
```python
scheduler.add_job(
    job_intraday_news,
    CronTrigger(minute="*/30", hour="9-15", **kw),
    id="intraday_news",
    replace_existing=True,
    max_instances=1,
    misfire_grace_time=30,
)
```

- [ ] **Step 2: Add 12:30 PM rollover job**

```python
from trading.risk_manager import try_rollover_exit_to_entry
from datetime import datetime as _dt
from zoneinfo import ZoneInfo as _ZI

scheduler.add_job(
    lambda: try_rollover_exit_to_entry(_dt.now(_ZI("America/New_York"))),
    CronTrigger(hour=12, minute=30, **kw),
    id="budget_rollover",
    replace_existing=True,
    max_instances=1,
    misfire_grace_time=300,
)
```

- [ ] **Step 3: Invalidate FRED cache after 4 PM refresh**

Find the existing `job_fred_cache` scheduler job. After it runs, call `invalidate_fred_cache()`. The cleanest way is to wrap the existing call:

```python
from trading.cycle_context import invalidate_fred_cache as _invalidate_fred

def _refresh_fred_and_invalidate():
    macro_regime.refresh_fred_cache()
    _invalidate_fred()

scheduler.add_job(
    _refresh_fred_and_invalidate,
    CronTrigger(hour=16, minute=0, **kw),
    id="fred_cache",
    replace_existing=True,
    max_instances=1,
    misfire_grace_time=300,
)
```

- [ ] **Step 4: Verify no import errors**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -c "import main; print('ok')"
```
Expected: `ok`

- [ ] **Step 5: Run full test suite — final verification**

```bash
cd ~/Documents/projects/trading-bot && .venv/bin/python -m pytest tests/ -v -m unit --tb=short 2>&1 | tail -20
```
Expected: All tests passing, no regressions.

- [ ] **Step 6: Commit**

```bash
git add main.py
git commit -m "feat: add intraday news, budget rollover, FRED invalidation scheduler jobs"
```

---

## Expected Outcome

| Metric | Before | After |
|---|---|---|
| Claude calls on a day like May 13 | 42 | 6–10 |
| Entry budget remaining at 1 PM ET | $0.00 | $0.40+ |
| Disk reads per Claude batch call | 2 (macro_regime.json) | 0 |
| FRED reads per trading day | 36 | 1 (TTL cached) |
| Stocks queued for Claude when overbought | All above composite 15 | 0 (fast_gate blocks) |
| Afternoon window (1–3 PM ET) | Dead | Active (time-of-day cap preserved) |
| Auto-execute on strong setups | Never | When composite > 45 + sentiment > 15 |
