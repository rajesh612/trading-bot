# Bug Fixes + Universe Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 14 silent bugs (progressive loss sizing, midday alarms, regime learning, composite gates, unbounded files, drawdown spam, and more), replace the three parallel sector maps with a single `tickers.json`/`sectors.py` source of truth, and add a `utils/regimes.py` constants module.

**Architecture:** Bugs are fixed individually in dependency order (config constants first, field-name fixes second, composite logic third). The universe refactor creates `tickers.json` + `sectors.py` before migrating callers — this keeps every task fully green. Dead code is removed last, after the replacements are in place.

**Tech Stack:** Python 3.14, pytest, `utils.io_utils.load_json`, APScheduler, existing test suite (`pytest tests/ -m unit -q`).

---

## File Map

| Action | File | Task |
|--------|------|------|
| Create | `tickers.json` | 10 |
| Create | `sectors.py` | 10 |
| Create | `utils/regimes.py` | 2 |
| Delete | `tickers.txt` | 11 |
| Delete | `tier1_config.py` | 11 |
| Modify | `config.py` | 1, 10, 13 |
| Modify | `trading/journal.py` | 3 |
| Modify | `scheduler_jobs.py` | 3, 11 |
| Modify | `learning/trade_analysis.py` | 3, 6 |
| Modify | `signals/entry_gates.py` | 4, 5, 12 |
| Modify | `trading/pre_claude_filter.py` | 4, 5, 12 |
| Modify | `macro/regime.py` | 4, 6 |
| Modify | `trading/executor.py` | 4 |
| Modify | `learning/agent.py` | 6 |
| Modify | `utils/metrics.py` | 7 |
| Modify | `trading/monitor.py` | 8 |
| Modify | `trading/eod_holds.py` | 9, 11, 12 |
| Modify | `trading/pipeline.py` | 9, 11, 12 |
| Modify | `trading/closer.py` | 12 |
| Modify | `trading/modes.py` | 11, 12 |
| Modify | `trading/cycle.py` | 12 |
| Modify | `agents/volatility_screener.py` | 10 |
| Modify | `agents/watch_list.py` | 10 |
| Modify | `signals/correlation_guard.py` | 11 |
| Modify | `research/agent.py` | 11 |
| Modify | `research/analyzer.py` | 11 |
| Modify | `CLAUDE.md` | 5 |
| Modify | `tests/test_foundation.py` | 2, 10, 11 |
| Modify | `tests/test_learning.py` | 3, 6 |
| Modify | `tests/test_entry_gates.py` | 4 |
| Modify | `tests/test_trading_lifecycle.py` | 12 |

---

## Task 1: Add new config constants

**Files:**
- Modify: `config.py`

Four new constants are needed before bug fixes can reference them.
Also fix a misleading comment on `TELEMETRY_KEEP_LINES`.

- [ ] **Step 1: Add SQUEEZE_COMPOSITE_MIN, DEFENSIVE_COMPOSITE_MULT, METRICS_KEEP_LINES, DRAWDOWN_ALERT_COOLDOWN_MIN**

In `config.py`, locate the `# ── Magic-number extraction` section near the end (~line 314) and add after the existing constants:

```python
# ── Bug-fix constants ─────────────────────────────────────────────────────────
SQUEEZE_COMPOSITE_MIN      = 12    # composite threshold when squeeze_setup is True
DEFENSIVE_COMPOSITE_MULT   = 1.5   # multiplier applied to COMPOSITE_MIN_HIGH_VOL in DEFENSIVE pre-filter
METRICS_KEEP_LINES         = 2000  # max lines kept in metrics.jsonl (≈ 55 trading days at ~36 cycles/day)
DRAWDOWN_ALERT_COOLDOWN_MIN = 30   # minimum minutes between drawdown Telegram alerts for the same position
```

- [ ] **Step 2: Fix the misleading TELEMETRY_KEEP_LINES comment**

In `config.py`, find the line:
```python
TELEMETRY_KEEP_LINES           = 500  # max lines kept in metrics.jsonl before trimming
```
Change the comment to:
```python
TELEMETRY_KEEP_LINES           = 500  # max lines kept in cycle_telemetry.jsonl before trimming
```

- [ ] **Step 3: Run tests to confirm nothing broke**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```
Expected: all tests pass (no code logic changed).

- [ ] **Step 4: Commit**

```bash
git add config.py
git commit -m "config: add bug-fix constants (squeeze min, defensive mult, metrics lines, drawdown cooldown)"
```

---

## Task 2: Create utils/regimes.py + tests

**Files:**
- Create: `utils/regimes.py`
- Modify: `tests/test_foundation.py`

Centralises all regime string constants and common frozenset groupings.

- [ ] **Step 1: Write failing tests in tests/test_foundation.py**

Append this class at the end of `tests/test_foundation.py`:

```python
# ── utils.regimes ─────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestRegimesModule:
    def test_constants_are_strings(self):
        from utils import regimes
        assert regimes.CRISIS == "CRISIS"
        assert regimes.DEFENSIVE == "DEFENSIVE"
        assert regimes.HIGH_VOL == "HIGH_VOL"

    def test_valid_excludes_high_vol(self):
        from utils import regimes
        assert regimes.HIGH_VOL not in regimes.VALID

    def test_allow_entry_excludes_crisis(self):
        from utils import regimes
        assert regimes.CRISIS not in regimes.ALLOW_ENTRY

    def test_allow_entry_is_valid_minus_crisis(self):
        from utils import regimes
        assert regimes.ALLOW_ENTRY == regimes.VALID - {regimes.CRISIS}

    def test_high_vol_gates_contains_defensive_and_high_vol(self):
        from utils import regimes
        assert regimes.DEFENSIVE in regimes.HIGH_VOL_GATES
        assert regimes.HIGH_VOL in regimes.HIGH_VOL_GATES

    def test_size_reduce_members(self):
        from utils import regimes
        assert regimes.HIGH_VOL in regimes.SIZE_REDUCE
        assert regimes.DEFENSIVE in regimes.SIZE_REDUCE
        assert regimes.CHOPPY in regimes.SIZE_REDUCE
        assert regimes.NORMAL not in regimes.SIZE_REDUCE

    def test_swing_eligible_members(self):
        from utils import regimes
        assert regimes.BULL_TREND in regimes.SWING_ELIGIBLE
        assert regimes.OPPORTUNITY in regimes.SWING_ELIGIBLE
        assert regimes.CAPITULATION in regimes.SWING_ELIGIBLE
        assert regimes.CRASH_RECOVERY in regimes.SWING_ELIGIBLE
        assert regimes.NORMAL not in regimes.SWING_ELIGIBLE
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
.venv/bin/python -m pytest tests/test_foundation.py::TestRegimesModule -v
```
Expected: `ModuleNotFoundError: No module named 'utils.regimes'`

- [ ] **Step 3: Create utils/regimes.py**

```python
"""Regime and trade-mode string constants.

Import these instead of bare string literals to prevent silent typo bugs
and enable O(1) frozenset membership tests.
"""

CRISIS         = "CRISIS"
CRASH_RECOVERY = "CRASH_RECOVERY"
BULL_TREND     = "BULL_TREND"
CHOPPY         = "CHOPPY"
DEFENSIVE      = "DEFENSIVE"
NORMAL         = "NORMAL"
OPPORTUNITY    = "OPPORTUNITY"
CAPITULATION   = "CAPITULATION"
# HIGH_VOL: returned by backtest/data.py simplified classifier but never by live classify_regime()
HIGH_VOL       = "HIGH_VOL"

VALID = frozenset({
    CRISIS, CRASH_RECOVERY, BULL_TREND, CHOPPY,
    DEFENSIVE, NORMAL, OPPORTUNITY, CAPITULATION,
})
ALLOW_ENTRY    = VALID - {CRISIS}
HIGH_VOL_GATES = frozenset({HIGH_VOL, DEFENSIVE})
SIZE_REDUCE    = frozenset({HIGH_VOL, DEFENSIVE, CHOPPY})
SWING_ELIGIBLE = frozenset({BULL_TREND, OPPORTUNITY, CAPITULATION, CRASH_RECOVERY})
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
.venv/bin/python -m pytest tests/test_foundation.py::TestRegimesModule -v
```
Expected: all 7 tests PASS.

- [ ] **Step 5: Run full test suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add utils/regimes.py tests/test_foundation.py
git commit -m "feat: add utils/regimes.py — regime string constants and frozenset groupings"
```

---

## Task 3: Fix journal field mismatches (B1, B2, B3)

**Files:**
- Modify: `trading/journal.py` (B1)
- Modify: `scheduler_jobs.py` (B2)
- Modify: `learning/trade_analysis.py` (B3)
- Modify: `tests/test_learning.py` (fix broken test fixtures)

Root cause: every journal entry is written with `"type"` field (`"BUY"`, `"CLOSE"`) but three consumers check `"action"` field, which is always absent → silent always-zero/always-fire bugs.

- [ ] **Step 1: Update test_learning.py to use the correct "type" field**

In `tests/test_learning.py`, find `class TestProgressiveLoss` (around line 277).

Change the `_journal_entry` helper (line 278–283) from:
```python
    def _journal_entry(self, action: str, pnl_pct: float, today: bool = True) -> dict:
        """Create a synthetic journal entry with optional date override."""
        from datetime import datetime
        from utils.constants import ET
        ts = datetime.now(ET).date().isoformat() if today else "2020-01-01"
        return {"action": action, "pnl_pct": pnl_pct, "timestamp": f"{ts}T10:00:00"}
```
to:
```python
    def _journal_entry(self, action: str, pnl_pct: float, today: bool = True) -> dict:
        """Create a synthetic journal entry with optional date override."""
        from datetime import datetime
        from utils.constants import ET
        ts = datetime.now(ET).date().isoformat() if today else "2020-01-01"
        return {"type": action, "pnl_pct": pnl_pct, "timestamp": f"{ts}T10:00:00"}
```

Also find the tests for `_compute_regime_winrate` (around lines 507–528) and update all `{"action": "CLOSE"...}` entries to `{"type": "CLOSE"...}`:

```python
    def test_winrate_ignores_trades_without_regime(self):
        from learning.trade_analysis import _compute_regime_winrate
        trades = [
            {"type": "CLOSE", "pnl_pct": 2.0},
            {"type": "CLOSE", "regime_at_entry": "NORMAL", "pnl_pct": 3.0},
        ]
        result = _compute_regime_winrate(trades)
        assert "NORMAL" in result
        assert result["NORMAL"] == pytest.approx(1.0)
        assert len(result) == 1

    def test_winrate_multiple_regimes_independent(self):
        from learning.trade_analysis import _compute_regime_winrate
        trades = [
            {"type": "CLOSE", "regime_at_entry": "BULL_TREND", "pnl_pct": 2.0},
            {"type": "CLOSE", "regime_at_entry": "BULL_TREND", "pnl_pct": -1.0},
            {"type": "CLOSE", "regime_at_entry": "CHOPPY",     "pnl_pct": 1.0},
            {"type": "CLOSE", "regime_at_entry": "CHOPPY",     "pnl_pct": 1.0},
        ]
        result = _compute_regime_winrate(trades)
        assert result["BULL_TREND"] == pytest.approx(0.5)
        assert result["CHOPPY"] == pytest.approx(1.0)
```

- [ ] **Step 2: Run affected tests to confirm they now FAIL (correct — the production code is still broken)**

```bash
.venv/bin/python -m pytest tests/test_learning.py::TestProgressiveLoss::test_count_losses_counts_only_today tests/test_learning.py::TestLearningTradeAnalysis::test_winrate_ignores_trades_without_regime -v
```
Expected: FAIL — count returns 0 because production code checks `"action"`.

- [ ] **Step 3: Fix trading/journal.py B1 — line 110**

In `trading/journal.py`, find `count_todays_losses()` (line 104). Change:
```python
        if e.get("action") == "CLOSE"
```
to:
```python
        if e.get("type") == "CLOSE"
```

- [ ] **Step 4: Fix scheduler_jobs.py B2 — line 209**

In `scheduler_jobs.py`, find `job_midday_check()` (line 202). Change:
```python
        buys_today = [e for e in journal if e.get("action") == "BUY" and e.get("timestamp", "").startswith(today)]
```
to:
```python
        buys_today = [e for e in journal if e.get("type") == "BUY" and e.get("timestamp", "").startswith(today)]
```

- [ ] **Step 5: Fix learning/trade_analysis.py B3 — line 59**

In `learning/trade_analysis.py`, find `_compute_regime_winrate()` (line 50). Change:
```python
        if trade.get("action") != "CLOSE":
```
to:
```python
        if trade.get("type") != "CLOSE":
```

- [ ] **Step 6: Run full test suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```
Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add trading/journal.py scheduler_jobs.py learning/trade_analysis.py tests/test_learning.py
git commit -m "fix: B1/B2/B3 — action→type field in count_todays_losses, midday check, regime winrate"
```

---

## Task 4: Fix composite_min_normal relaxation (B4)

**Files:**
- Modify: `signals/entry_gates.py`
- Modify: `macro/regime.py`
- Modify: `trading/executor.py`
- Modify: `trading/pre_claude_filter.py`
- Modify: `tests/test_entry_gates.py`

The learning agent writes `composite_min_normal` to `trading_params.json` on zero-trade days, but `effective_composite_gate()` ignores `snap.params` entirely — it always reads `config.COMPOSITE_MIN_NORMAL` directly.

- [ ] **Step 1: Add test for the new params behaviour in tests/test_entry_gates.py**

Add these two tests to `class TestEffectiveCompositeGate` (around line 535):

```python
    def test_normal_regime_uses_params_composite_min(self):
        """params['composite_min_normal'] overrides config.COMPOSITE_MIN_NORMAL in NORMAL."""
        from signals.entry_gates import effective_composite_gate
        iv = self._iv()
        params = {"composite_min_normal": 18}
        assert effective_composite_gate(iv, "NORMAL", params=params) == 18

    def test_normal_regime_falls_back_to_config_when_no_params(self):
        """Without params, normal regime returns config.COMPOSITE_MIN_NORMAL."""
        import config
        from signals.entry_gates import effective_composite_gate
        iv = self._iv()
        assert effective_composite_gate(iv, "NORMAL") == config.COMPOSITE_MIN_NORMAL
```

- [ ] **Step 2: Run new tests to confirm they FAIL**

```bash
.venv/bin/python -m pytest tests/test_entry_gates.py::TestEffectiveCompositeGate::test_normal_regime_uses_params_composite_min -v
```
Expected: FAIL — `effective_composite_gate` does not accept `params` argument yet.

- [ ] **Step 3: Update effective_composite_gate signature in signals/entry_gates.py**

Find `effective_composite_gate` (line 33) and replace it:

```python
def effective_composite_gate(iv: IndicatorValues, regime: str, params: dict | None = None) -> int:
    """Single source of truth for composite entry threshold by regime and signal context.

    Priority: HIGH_VOL safety → squeeze setup → bot herding → CHOPPY → NORMAL.
    When params is provided, NORMAL regime reads composite_min_normal from params
    (written by learning agent on zero-trade days).
    """
    if regime in ("HIGH_VOL", "DEFENSIVE"):
        return config.COMPOSITE_MIN_HIGH_VOL
    if iv.squeeze_setup:
        return 12                              # strong squeeze signal lowers bar
    if iv.bot_herding_score >= config.BOT_HERD_ALERT_SCORE:
        return config.HERD_ENTRY_GATE
    if regime == "CHOPPY":
        return config.CHOPPY_COMPOSITE_GATE
    return (params or {}).get("composite_min_normal", config.COMPOSITE_MIN_NORMAL)
```

- [ ] **Step 4: Add composite_min_normal to load_trading_params defaults in macro/regime.py**

In `macro/regime.py`, find `load_trading_params()` (line 171). Add `"composite_min_normal"` to the defaults dict:

```python
    defaults = {
        "entry_start_hour": config.ENTRY_START_HOUR, "entry_end_hour": config.ENTRY_END_HOUR,
        "rsi_buy_max": config.RSI_BUY_MAX, "rsi_buy_max_high_vol": config.RSI_BUY_MAX_HIGH_VOL,
        "vwap_slope_min": config.VWAP_SLOPE_MIN, "high_vol_position_mult": config.HIGH_VOL_POSITION_MULT,
        "stop_loss_pct": config.STOP_LOSS_PCT, "take_profit_pct": config.TAKE_PROFIT_PCT,
        "composite_min_normal": config.COMPOSITE_MIN_NORMAL,
    }
```

- [ ] **Step 5: Update executor.py to pass params to effective_composite_gate**

In `trading/executor.py`, find the call at line 115:
```python
    eff_gate = effective_composite_gate(iv, regime)
```
Change to:
```python
    eff_gate = effective_composite_gate(iv, regime, params=params)
```

- [ ] **Step 6: Update pre_claude_filter.py to pass snap.params**

In `trading/pre_claude_filter.py`, find the call at line 46:
```python
    composite_min = effective_composite_gate(iv, snap.regime)
```
Change to:
```python
    composite_min = effective_composite_gate(iv, snap.regime, params=snap.params)
```

- [ ] **Step 7: Run full test suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```
Expected: all tests pass including the two new ones.

- [ ] **Step 8: Commit**

```bash
git add signals/entry_gates.py macro/regime.py trading/executor.py trading/pre_claude_filter.py tests/test_entry_gates.py
git commit -m "fix: B4 — composite_min_normal relaxation now consumed from snap.params"
```

---

## Task 5: Fix DEFENSIVE gate + magic numbers + stale comment (B5, B6, B7)

**Files:**
- Modify: `signals/entry_gates.py` (B6: replace `12` literal, B7: fix comment)
- Modify: `trading/pre_claude_filter.py` (B5: replace `* 1.5` literal)
- Modify: `CLAUDE.md` (B7: fix stale config table value)

- [ ] **Step 1: Fix B6 — replace magic number 12 in entry_gates.py**

In `signals/entry_gates.py`, in `effective_composite_gate()`, change:
```python
    if iv.squeeze_setup:
        return 12                              # strong squeeze signal lowers bar
```
to:
```python
    if iv.squeeze_setup:
        return config.SQUEEZE_COMPOSITE_MIN    # strong squeeze signal lowers bar
```

- [ ] **Step 2: Fix B7 — fix stale comment in entry_gates.py**

In `signals/entry_gates.py`, find:
```python
    if regime == "CHOPPY":
        return config.CHOPPY_COMPOSITE_GATE   # 30
```
Change the comment:
```python
    if regime == "CHOPPY":
        return config.CHOPPY_COMPOSITE_GATE   # 35
```

- [ ] **Step 3: Fix B5 — replace magic multiplier 1.5 in pre_claude_filter.py**

In `trading/pre_claude_filter.py`, find:
```python
            if iv.composite_score < config.COMPOSITE_MIN_HIGH_VOL * 1.5:
```
Change to:
```python
            if iv.composite_score < config.COMPOSITE_MIN_HIGH_VOL * config.DEFENSIVE_COMPOSITE_MULT:
```

- [ ] **Step 4: Fix B7 — update CLAUDE.md config table**

In `CLAUDE.md`, find the Key Config table entry:
```
| `COMPOSITE_MIN_NORMAL` / `COMPOSITE_MIN_HIGH_VOL` | 20 / 35 | Pre-Claude composite threshold (regime-adjusted) |
```
Change `20` to `30`:
```
| `COMPOSITE_MIN_NORMAL` / `COMPOSITE_MIN_HIGH_VOL` | 30 / 35 | Pre-Claude composite threshold (regime-adjusted) |
```

- [ ] **Step 5: Run full test suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```
Expected: all tests pass. Note: `test_squeeze_setup_returns_12` still passes because `config.SQUEEZE_COMPOSITE_MIN == 12`.

- [ ] **Step 6: Commit**

```bash
git add signals/entry_gates.py trading/pre_claude_filter.py CLAUDE.md
git commit -m "fix: B5/B6/B7 — DEFENSIVE_COMPOSITE_MULT config, SQUEEZE_COMPOSITE_MIN, correct comment"
```

---

## Task 6: Fix missing params defaults + choppy_rsi_max learning (B8)

**Files:**
- Modify: `macro/regime.py`
- Modify: `learning/agent.py`
- Modify: `learning/trade_analysis.py`

Three related gaps: `atr_tp_multiplier` and `choppy_rsi_max` have no defaults in `load_trading_params()`, and `choppy_rsi_max` is missing from the learning agent's `param_keys` and `_THRESHOLD_BOUNDS`, so the learning agent can never tune it.

- [ ] **Step 1: Add atr_tp_multiplier and choppy_rsi_max to load_trading_params defaults**

In `macro/regime.py`, find the `defaults` dict in `load_trading_params()` (line 173). Add:

```python
    defaults = {
        "entry_start_hour": config.ENTRY_START_HOUR, "entry_end_hour": config.ENTRY_END_HOUR,
        "rsi_buy_max": config.RSI_BUY_MAX, "rsi_buy_max_high_vol": config.RSI_BUY_MAX_HIGH_VOL,
        "vwap_slope_min": config.VWAP_SLOPE_MIN, "high_vol_position_mult": config.HIGH_VOL_POSITION_MULT,
        "stop_loss_pct": config.STOP_LOSS_PCT, "take_profit_pct": config.TAKE_PROFIT_PCT,
        "composite_min_normal": config.COMPOSITE_MIN_NORMAL,
        "atr_tp_multiplier": config.ATR_TP_MULT_DEFAULT,
        "choppy_rsi_max": config.CHOPPY_RSI_PULLBACK_MAX,
    }
```

- [ ] **Step 2: Add choppy_rsi_max to param_keys in learning/agent.py**

In `learning/agent.py`, find `param_keys` (line 237):
```python
    param_keys = [
        "entry_start_hour", "entry_end_hour", "rsi_buy_max", "rsi_buy_max_high_vol",
        "vwap_slope_min", "high_vol_position_mult", "stop_loss_pct", "take_profit_pct",
    ]
```
Change to:
```python
    param_keys = [
        "entry_start_hour", "entry_end_hour", "rsi_buy_max", "rsi_buy_max_high_vol",
        "vwap_slope_min", "high_vol_position_mult", "stop_loss_pct", "take_profit_pct",
        "choppy_rsi_max",
    ]
```

- [ ] **Step 3: Add choppy_rsi_max to _THRESHOLD_BOUNDS in learning/trade_analysis.py**

In `learning/trade_analysis.py`, find `_THRESHOLD_BOUNDS` (line 21). Add entry:
```python
_THRESHOLD_BOUNDS: dict[str, tuple] = {
    "choppy_composite_gate":          (25,   40),
    "bull_trend_rsi_max_tier1":       (76,   88),
    "swing_max_days":                 (3,    7),
    "capitulation_drawdown_threshold":(15,   30),
    "squeeze_confidence_gate":        (60,   80),
    "herd_size_reduction":            (0.15, 0.50),
    "stop_loss_pct":                  (1.5,  12.0),
    "take_profit_pct":                (2.0,  25.0),
    "rsi_buy_max":                    (30,   80),
    "rsi_buy_max_high_vol":           (25,   70),
    "vwap_slope_min":                 (-2.0, 0.0),
    "high_vol_position_mult":         (0.20, 0.75),
    "choppy_rsi_max":                 (45,   70),
}
```

- [ ] **Step 4: Run full test suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```
Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add macro/regime.py learning/agent.py learning/trade_analysis.py
git commit -m "fix: B8 — add atr_tp_multiplier + choppy_rsi_max to params defaults and learning bounds"
```

---

## Task 7: Fix metrics.jsonl unbounded growth (B9)

**Files:**
- Modify: `utils/metrics.py`

`record_cycle_metric()` appends forever. Add once-per-day trim to last `METRICS_KEEP_LINES` lines.

- [ ] **Step 1: Update utils/metrics.py to trim once per day**

Replace the full content of `utils/metrics.py` with:

```python
"""
Lightweight per-cycle metrics JSONL export.
Records one line per cycle to METRICS_FILE for post-hoc trend analysis.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import config
from utils.constants import ET

logger = logging.getLogger(__name__)

_last_trim_date: str = ""


def record_cycle_metric(
    regime: str,
    candidates: int,
    entries: int,
    closes: int,
    api_budget_pct: float,
    cycle_ms: float,
) -> None:
    """Append one metric record to METRICS_FILE. Trims to METRICS_KEEP_LINES once per day."""
    global _last_trim_date
    try:
        now = datetime.now(ET)
        record = json.dumps({
            "ts": now.isoformat(),
            "date": now.strftime("%Y-%m-%d"),
            "regime": regime,
            "candidates": candidates,
            "entries": entries,
            "closes": closes,
            "api_budget_pct": round(api_budget_pct, 3),
            "cycle_ms": round(cycle_ms, 0),
        })
        with open(config.METRICS_FILE, "a") as f:
            f.write(record + "\n")
        today = now.strftime("%Y-%m-%d")
        if today != _last_trim_date:
            _trim_metrics()
            _last_trim_date = today
    except Exception as exc:
        logger.debug("metrics write failed: %s", exc)


def _trim_metrics() -> None:
    """Keep only the last METRICS_KEEP_LINES lines of METRICS_FILE."""
    path = Path(str(config.METRICS_FILE))
    if not path.exists():
        return
    try:
        lines = path.read_text().splitlines()
        if len(lines) > config.METRICS_KEEP_LINES:
            path.write_text("\n".join(lines[-config.METRICS_KEEP_LINES:]) + "\n")
    except Exception as exc:
        logger.debug("metrics trim failed: %s", exc)


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

- [ ] **Step 2: Run full test suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```
Expected: all tests pass.

- [ ] **Step 3: Commit**

```bash
git add utils/metrics.py
git commit -m "fix: B9 — trim metrics.jsonl to METRICS_KEEP_LINES once per day"
```

---

## Task 8: Fix drawdown alert spam (B10)

**Files:**
- Modify: `trading/monitor.py`

`_check_drawdown_alerts()` fires one Telegram message per 60-second monitor tick for every position in drawdown, with no cooldown.

- [ ] **Step 1: Add module-level cooldown dict and update _check_drawdown_alerts**

In `trading/monitor.py`, add the module-level dict immediately after the imports (before the first function definition):

```python
import time

_drawdown_alerted: dict[str, float] = {}  # symbol → epoch of last alert
```

Then replace `_check_drawdown_alerts` (line 37–49) with:

```python
def _check_drawdown_alerts(positions: dict, current_prices: dict) -> None:
    """Fire drawdown alerts with per-position cooldown to avoid Telegram spam."""
    now_ts = time.time()
    cooldown_s = config.DRAWDOWN_ALERT_COOLDOWN_MIN * 60
    for symbol, pos in positions.items():
        price = current_prices.get(symbol)
        if price is None:
            continue
        entry = pos.get("entry_price", 0)
        if entry <= 0:
            continue
        pnl_pct = (price - entry) / entry * 100
        stop_pct = -pos.get("stop_loss_pct", config.STOP_LOSS_PCT)
        if pnl_pct <= -config.DRAWDOWN_WARN_PCT and pnl_pct > stop_pct:
            last = _drawdown_alerted.get(symbol, 0.0)
            if now_ts - last >= cooldown_s:
                telegram_notify.notify_position_drawdown(symbol, pnl_pct, stop_pct)
                _drawdown_alerted[symbol] = now_ts
```

Also add a helper to clear the cooldown when a position is closed. Find `close_position` usage patterns and add a call to clear the dict entry. The cleanest place is at the top of `run_position_monitor()` after loading positions — clear stale symbols:

```python
def _clear_drawdown_cooldown(symbol: str) -> None:
    """Remove symbol from drawdown cooldown dict when its position closes."""
    _drawdown_alerted.pop(symbol, None)
```

Then in any location in `monitor.py` that calls the close logic, add `_clear_drawdown_cooldown(symbol)`. Search for `close_position(` calls in `run_position_monitor` and add the clear call after each close.

Alternatively, add a simpler stale-cleanup in `_check_drawdown_alerts` — remove symbols no longer in positions:

```python
def _check_drawdown_alerts(positions: dict, current_prices: dict) -> None:
    """Fire drawdown alerts with per-position cooldown to avoid Telegram spam."""
    now_ts = time.time()
    cooldown_s = config.DRAWDOWN_ALERT_COOLDOWN_MIN * 60
    # Prune closed positions from cooldown dict
    for sym in list(_drawdown_alerted):
        if sym not in positions:
            del _drawdown_alerted[sym]
    for symbol, pos in positions.items():
        price = current_prices.get(symbol)
        if price is None:
            continue
        entry = pos.get("entry_price", 0)
        if entry <= 0:
            continue
        pnl_pct = (price - entry) / entry * 100
        stop_pct = -pos.get("stop_loss_pct", config.STOP_LOSS_PCT)
        if pnl_pct <= -config.DRAWDOWN_WARN_PCT and pnl_pct > stop_pct:
            last = _drawdown_alerted.get(symbol, 0.0)
            if now_ts - last >= cooldown_s:
                telegram_notify.notify_position_drawdown(symbol, pnl_pct, stop_pct)
                _drawdown_alerted[symbol] = now_ts
```

- [ ] **Step 2: Run full test suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```
Expected: all tests pass.

- [ ] **Step 3: Commit**

```bash
git add trading/monitor.py
git commit -m "fix: B10 — drawdown alerts throttled to DRAWDOWN_ALERT_COOLDOWN_MIN per position"
```

---

## Task 9: Fix EOD AlpacaClient loop + CycleResult.skipped (B11, B12)

**Files:**
- Modify: `trading/eod_holds.py` (B11)
- Modify: `trading/pipeline.py` (B12)

- [ ] **Step 1: Fix B11 — move AlpacaClient() outside the loop in eod_holds.py**

In `trading/eod_holds.py`, find `evaluate_eod_holds()` (around line 74). The loop starts:

```python
    for symbol, pos in open_positions.items():
        try:
            entry = pos["entry_price"]
            from broker.alpaca_client import AlpacaClient
            alpaca = AlpacaClient()
            current_price = alpaca.get_latest_price(symbol)
```

Change to move client creation before the loop:

```python
    from broker.alpaca_client import AlpacaClient
    alpaca = AlpacaClient()

    for symbol, pos in open_positions.items():
        try:
            entry = pos["entry_price"]
            current_price = alpaca.get_latest_price(symbol)
```

- [ ] **Step 2: Fix B12 — set result.skipped = True when stock_data is empty in pipeline.py**

In `trading/pipeline.py`, find the empty-data guard (line 223):

```python
        if not stock_data:
            logger.warning("No valid indicator data — skipping cycle.")
            return result
```

Change to:

```python
        if not stock_data:
            logger.warning("No valid indicator data — skipping cycle.")
            result.skipped = True
            return result
```

- [ ] **Step 3: Run full test suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```
Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add trading/eod_holds.py trading/pipeline.py
git commit -m "fix: B11 AlpacaClient outside EOD loop; B12 CycleResult.skipped set on data failure"
```

---

## Task 10: Create tickers.json + sectors.py + update screener/watchlist (B13, 2a–2g partial)

**Files:**
- Create: `tickers.json`
- Create: `sectors.py`
- Modify: `config.py` (TICKERS_FILE path)
- Modify: `agents/volatility_screener.py` (B13 + 2f)
- Modify: `agents/watch_list.py` (2g)
- Modify: `tests/test_foundation.py`

- [ ] **Step 1: Create tickers.json at project root**

Create `/Users/rajeshkumar/Documents/projects/trading-bot/tickers.json` with the following content (all 126 symbols, grouped by sector ETF):

```json
{
  "XLK": {
    "name": "Technology",
    "symbols": [
      {"symbol": "NVDA",  "tier1": true,  "theme": "AI_GPU"},
      {"symbol": "AMD",   "tier1": true,  "theme": "AI_GPU"},
      {"symbol": "AVGO",  "tier1": true,  "theme": "AI_NETWORKING"},
      {"symbol": "ARM",   "tier1": true,  "theme": "AI_CHIP_ARCHITECTURE"},
      {"symbol": "MRVL",  "tier1": true,  "theme": "AI_NETWORKING"},
      {"symbol": "QCOM",  "tier1": true,  "theme": "AI_EDGE"},
      {"symbol": "MU",    "tier1": true,  "theme": "MEMORY"},
      {"symbol": "STX",   "tier1": true,  "theme": "MEMORY"},
      {"symbol": "TSM",   "tier1": true,  "theme": "SEMICONDUCTOR_FOUNDRY"},
      {"symbol": "INTC",  "tier1": true,  "theme": "SEMICONDUCTOR_FOUNDRY"},
      {"symbol": "ASML",  "tier1": true,  "theme": "SEMICONDUCTOR_EQUIPMENT"},
      {"symbol": "AMAT",  "tier1": true,  "theme": "SEMICONDUCTOR_EQUIPMENT"},
      {"symbol": "LRCX",  "tier1": true,  "theme": "SEMICONDUCTOR_EQUIPMENT"},
      {"symbol": "KLAC",  "tier1": true,  "theme": "SEMICONDUCTOR_EQUIPMENT"},
      {"symbol": "ON",    "tier1": false, "theme": "SEMICONDUCTOR"},
      {"symbol": "DELL",  "tier1": true,  "theme": "AI_SERVER_INFRASTRUCTURE"},
      {"symbol": "TXN",   "tier1": true,  "theme": "ANALOG_SEMI"},
      {"symbol": "ADI",   "tier1": true,  "theme": "ANALOG_SEMI"},
      {"symbol": "ANET",  "tier1": true,  "theme": "NETWORKING"},
      {"symbol": "VRT",   "tier1": true,  "theme": "COOLING_POWER"},
      {"symbol": "EQIX",  "tier1": true,  "theme": "COLOCATION"},
      {"symbol": "GLW",   "tier1": false, "theme": "OPTICAL_FIBER"},
      {"symbol": "COHR",  "tier1": true,  "theme": "OPTICAL_INTERCONNECT"},
      {"symbol": "LITE",  "tier1": true,  "theme": "OPTICAL_INTERCONNECT"},
      {"symbol": "CIEN",  "tier1": false, "theme": "NETWORKING"},
      {"symbol": "NET",   "tier1": false, "theme": "CLOUD"},
      {"symbol": "MSFT",  "tier1": true,  "theme": "CLOUD_AI"},
      {"symbol": "GOOGL", "tier1": true,  "theme": "CLOUD_AI"},
      {"symbol": "AMZN",  "tier1": true,  "theme": "CLOUD_ECOMMERCE"},
      {"symbol": "META",  "tier1": true,  "theme": "SOCIAL_AI"},
      {"symbol": "ORCL",  "tier1": true,  "theme": "CLOUD_DATABASE"},
      {"symbol": "IBM",   "tier1": true,  "theme": "ENTERPRISE_AI"},
      {"symbol": "PLTR",  "tier1": true,  "theme": "AI_SOFTWARE"},
      {"symbol": "CRM",   "tier1": true,  "theme": "AI_AGENTS_CRM"},
      {"symbol": "NOW",   "tier1": true,  "theme": "ENTERPRISE_AI_WORKFLOW"},
      {"symbol": "ADBE",  "tier1": true,  "theme": "AI_CREATIVE_TOOLS"},
      {"symbol": "INTU",  "tier1": true,  "theme": "AI_FINTECH_SAAS"},
      {"symbol": "SNPS",  "tier1": true,  "theme": "EDA_TOOLS"},
      {"symbol": "MDB",   "tier1": false, "theme": "CLOUD"},
      {"symbol": "CRWD",  "tier1": true,  "theme": "CYBERSECURITY"},
      {"symbol": "PANW",  "tier1": true,  "theme": "CYBERSECURITY"},
      {"symbol": "ZS",    "tier1": false, "theme": "CYBERSECURITY"},
      {"symbol": "APP",   "tier1": true,  "theme": "AI_ADTECH"},
      {"symbol": "SHOP",  "tier1": true,  "theme": "AI_ECOMMERCE"},
      {"symbol": "SNOW",  "tier1": false, "theme": "CLOUD"},
      {"symbol": "HUBS",  "tier1": false, "theme": "CLOUD"},
      {"symbol": "DDOG",  "tier1": true,  "theme": "AI_OBSERVABILITY"},
      {"symbol": "TTD",   "tier1": false, "theme": "ADTECH"},
      {"symbol": "AAPL",  "tier1": true,  "theme": "CONSUMER_ELECTRONICS"},
      {"symbol": "TSLA",  "tier1": true,  "theme": "EV_ROBOTICS_AI"},
      {"symbol": "NFLX",  "tier1": true,  "theme": "AI_CONTENT_STREAMING"},
      {"symbol": "DIS",   "tier1": true,  "theme": "AI_CONTENT_STREAMING"},
      {"symbol": "UBER",  "tier1": true,  "theme": "AI_MOBILITY"},
      {"symbol": "SPOT",  "tier1": true,  "theme": "AI_AUDIO_STREAMING"}
    ]
  },
  "XLF": {
    "name": "Financials",
    "symbols": [
      {"symbol": "JPM",   "tier1": true,  "theme": "BANKING"},
      {"symbol": "GS",    "tier1": true,  "theme": "INVESTMENT_BANKING"},
      {"symbol": "MS",    "tier1": true,  "theme": "WEALTH_MANAGEMENT"},
      {"symbol": "BRK.B", "tier1": true,  "theme": "INVESTMENT_HOLDING"},
      {"symbol": "V",     "tier1": true,  "theme": "DIGITAL_PAYMENTS"},
      {"symbol": "MA",    "tier1": true,  "theme": "DIGITAL_PAYMENTS"},
      {"symbol": "AXP",   "tier1": true,  "theme": "PREMIUM_PAYMENTS"},
      {"symbol": "PYPL",  "tier1": false, "theme": "DIGITAL_PAYMENTS"},
      {"symbol": "COIN",  "tier1": true,  "theme": "CRYPTO_EXCHANGE"},
      {"symbol": "SPGI",  "tier1": true,  "theme": "FINANCIAL_DATA"},
      {"symbol": "BLK",   "tier1": true,  "theme": "ASSET_MANAGEMENT"}
    ]
  },
  "XLY": {
    "name": "Consumer Discretionary",
    "symbols": [
      {"symbol": "BKNG",  "tier1": true,  "theme": "ONLINE_TRAVEL"},
      {"symbol": "ABNB",  "tier1": true,  "theme": "HOME_SHARING"},
      {"symbol": "MAR",   "tier1": true,  "theme": "HOSPITALITY"},
      {"symbol": "HLT",   "tier1": true,  "theme": "HOSPITALITY"}
    ]
  },
  "XLV": {
    "name": "Healthcare",
    "symbols": [
      {"symbol": "LLY",   "tier1": true,  "theme": "PHARMACEUTICALS"},
      {"symbol": "UNH",   "tier1": true,  "theme": "HEALTH_INSURANCE"},
      {"symbol": "AMGN",  "tier1": true,  "theme": "BIOTECHNOLOGY"},
      {"symbol": "ISRG",  "tier1": true,  "theme": "SURGICAL_ROBOTICS"},
      {"symbol": "DXCM",  "tier1": false, "theme": "MEDTECH"},
      {"symbol": "CRSP",  "tier1": false, "theme": "GENE_EDITING"},
      {"symbol": "JNJ",   "tier1": true,  "theme": "DIVERSIFIED_HEALTHCARE"},
      {"symbol": "ABBV",  "tier1": true,  "theme": "PHARMACEUTICALS"},
      {"symbol": "MRK",   "tier1": true,  "theme": "PHARMACEUTICALS"},
      {"symbol": "REGN",  "tier1": true,  "theme": "BIOTECHNOLOGY"}
    ]
  },
  "XLI": {
    "name": "Industrials",
    "symbols": [
      {"symbol": "CAT",   "tier1": true,  "theme": "AUTONOMOUS_EQUIPMENT"},
      {"symbol": "HON",   "tier1": true,  "theme": "INDUSTRIAL_AI"},
      {"symbol": "GE",    "tier1": true,  "theme": "AEROSPACE_POWER"},
      {"symbol": "GEV",   "tier1": true,  "theme": "GRID_ELECTRIFICATION"},
      {"symbol": "BA",    "tier1": true,  "theme": "COMMERCIAL_AEROSPACE"},
      {"symbol": "GD",    "tier1": true,  "theme": "DEFENSE_SYSTEMS"},
      {"symbol": "LHX",   "tier1": true,  "theme": "DEFENSE_ELECTRONICS"},
      {"symbol": "TDG",   "tier1": true,  "theme": "AEROSPACE_COMPONENTS"},
      {"symbol": "HWM",   "tier1": true,  "theme": "AEROSPACE_COMPONENTS"},
      {"symbol": "HEI",   "tier1": true,  "theme": "AEROSPACE_PARTS"},
      {"symbol": "RTX",   "tier1": true,  "theme": "DEFENSE"},
      {"symbol": "LMT",   "tier1": true,  "theme": "DEFENSE"},
      {"symbol": "NOC",   "tier1": true,  "theme": "DEFENSE"}
    ]
  },
  "XLE": {
    "name": "Energy",
    "symbols": [
      {"symbol": "VST",   "tier1": true,  "theme": "POWER_GENERATION"},
      {"symbol": "CEG",   "tier1": true,  "theme": "NUCLEAR_POWER"},
      {"symbol": "NEE",   "tier1": true,  "theme": "RENEWABLE_ENERGY"},
      {"symbol": "XOM",   "tier1": true,  "theme": "INTEGRATED_OIL"},
      {"symbol": "CVX",   "tier1": true,  "theme": "INTEGRATED_OIL"},
      {"symbol": "MPC",   "tier1": false, "theme": "REFINING"}
    ]
  },
  "XLP": {
    "name": "Consumer Staples",
    "symbols": [
      {"symbol": "WMT",   "tier1": true,  "theme": "RETAIL"},
      {"symbol": "COST",  "tier1": true,  "theme": "RETAIL"},
      {"symbol": "PG",    "tier1": true,  "theme": "CONSUMER_GOODS"},
      {"symbol": "KO",    "tier1": true,  "theme": "BEVERAGES"}
    ]
  },
  "XLB": {
    "name": "Materials",
    "symbols": [
      {"symbol": "NEM",   "tier1": true,  "theme": "GOLD_MINER"},
      {"symbol": "GLD",   "tier1": true,  "theme": "GOLD_ETF", "screener_eligible": false}
    ]
  },
  "_regime_signals": ["SPY", "QQQ", "VOOG", "SMH"]
}
```

- [ ] **Step 2: Update config.py TICKERS_FILE path**

In `config.py`, change line 11 from:
```python
TICKERS_FILE = BASE_DIR / "tickers.txt"
```
to:
```python
TICKERS_FILE = BASE_DIR / "tickers.json"
```

- [ ] **Step 3: Create sectors.py at project root**

```python
"""
Trading universe — single source of truth.
Loads tickers.json once at import via load_json and builds O(1) lookup dicts.
All callers import from here; tier1_config.py and tickers.txt are deleted once
all callers are migrated.
"""
from __future__ import annotations
import logging
import config
from utils.io_utils import load_json

logger = logging.getLogger(__name__)


def _build() -> tuple:
    data = load_json(config.TICKERS_FILE, {})
    if not data:
        logger.critical(
            "sectors: %s missing or empty — trading universe is empty. "
            "Screener will run with 0 tickers and send a Telegram alert.",
            config.TICKERS_FILE,
        )
        return {}, frozenset(), {}, {}, [], []

    symbol_to_etf: dict[str, str] = {}
    tier1_set: set[str] = set()
    symbol_to_theme: dict[str, str] = {}
    etf_to_symbols: dict[str, list[str]] = {}
    screener_symbols: list[str] = []

    for etf_key, sector_data in data.items():
        if etf_key.startswith("_"):
            continue
        symbols = sector_data.get("symbols", [])
        etf_to_symbols[etf_key] = []
        for entry in symbols:
            sym = entry["symbol"]
            symbol_to_etf[sym] = etf_key
            if entry.get("tier1", False):
                tier1_set.add(sym)
            symbol_to_theme[sym] = entry.get("theme", "")
            if entry.get("screener_eligible", True):
                screener_symbols.append(sym)
                etf_to_symbols[etf_key].append(sym)

    tier1_symbols = [s for s in screener_symbols if s in tier1_set]
    return symbol_to_etf, frozenset(tier1_set), symbol_to_theme, etf_to_symbols, screener_symbols, tier1_symbols


_sym_to_etf, TIER1_SET, _sym_to_theme, ETF_TO_SYMBOLS, SCREENER_SYMBOLS, TIER1_SYMBOLS = _build()

SYMBOL_TO_ETF    = _sym_to_etf
SYMBOL_TO_SECTOR = _sym_to_etf   # alias for clarity in sector-aware callers
SYMBOL_TO_THEME  = _sym_to_theme


def is_tier1(symbol: str) -> bool:
    return symbol.upper() in TIER1_SET


def get_sector_etf(symbol: str) -> str:
    """Returns the sector ETF key ('XLK', 'XLF', …) or 'OTHER' for unknown symbols."""
    return SYMBOL_TO_ETF.get(symbol.upper(), "OTHER")


def get_sector(symbol: str) -> str:
    """Alias of get_sector_etf for backward compatibility with tier1_config.get_sector()."""
    return get_sector_etf(symbol)


def get_theme(symbol: str) -> str:
    return SYMBOL_TO_THEME.get(symbol.upper(), "")


def sector_of(symbol: str) -> str:
    """Used by correlation_guard. Same as get_sector_etf."""
    return get_sector_etf(symbol)


def load_universe() -> list[str]:
    """Return all screener-eligible symbols in tickers.json order."""
    return list(SCREENER_SYMBOLS)
```

- [ ] **Step 4: Fix volatility_screener.py (B13 + 2f)**

In `agents/volatility_screener.py`, find `run_volatility_screener()` (line 54). Replace the ticker-loading block:

```python
    raw = config.TICKERS_FILE.read_text().strip().splitlines()
    tickers = [
        t.strip().upper() for t in raw
        if t.strip() and not t.strip().startswith("#") and t.strip().isalpha()
    ]
    dropped = [t.strip() for t in raw if t.strip() and t.strip().upper() not in tickers]
    if dropped:
        logger.info("Screener: skipping %d non-alpha/comment lines: %s", len(dropped), dropped[:5])
```

with:

```python
    import sectors
    tickers = sectors.load_universe()
```

- [ ] **Step 5: Fix watch_list.py (2g)**

In `agents/watch_list.py`, replace `_load_universe()` (line 16–18):

```python
def _load_universe() -> list[str]:
    src = config.DAILY_TRADEABLE_FILE if config.DAILY_TRADEABLE_FILE.exists() else config.TICKERS_FILE
    return [t.strip().upper() for t in src.read_text().splitlines() if t.strip()]
```

with:

```python
def _load_universe() -> list[str]:
    if config.DAILY_TRADEABLE_FILE.exists():
        return [t.strip().upper() for t in config.DAILY_TRADEABLE_FILE.read_text().splitlines() if t.strip()]
    import sectors
    return sectors.load_universe()
```

- [ ] **Step 6: Add sectors tests to tests/test_foundation.py**

Append this class to `tests/test_foundation.py`:

```python
# ── sectors ───────────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestSectors:
    """Integration tests against the real tickers.json on disk."""

    def test_nvda_is_tier1(self):
        import sectors
        assert sectors.is_tier1("NVDA") is True

    def test_case_insensitive(self):
        import sectors
        assert sectors.is_tier1("nvda") is True

    def test_on_is_not_tier1(self):
        import sectors
        assert sectors.is_tier1("ON") is False

    def test_unknown_is_not_tier1(self):
        import sectors
        assert sectors.is_tier1("ZZZFAKE") is False

    def test_nvda_maps_to_xlk(self):
        import sectors
        assert sectors.get_sector_etf("NVDA") == "XLK"

    def test_brk_b_maps_to_xlf(self):
        import sectors
        assert sectors.get_sector_etf("BRK.B") == "XLF"

    def test_jpm_maps_to_xlf(self):
        import sectors
        assert sectors.get_sector_etf("JPM") == "XLF"

    def test_lly_maps_to_xlv(self):
        import sectors
        assert sectors.get_sector_etf("LLY") == "XLV"

    def test_unknown_returns_other(self):
        import sectors
        assert sectors.get_sector_etf("ZZZUNKNOWN") == "OTHER"

    def test_get_sector_is_alias_of_get_sector_etf(self):
        import sectors
        assert sectors.get_sector("NVDA") == sectors.get_sector_etf("NVDA")

    def test_load_universe_excludes_regime_signals(self):
        import sectors
        universe = sectors.load_universe()
        assert "SPY" not in universe
        assert "QQQ" not in universe
        assert "SMH" not in universe

    def test_gld_excluded_from_screener(self):
        import sectors
        universe = sectors.load_universe()
        assert "GLD" not in universe

    def test_brk_b_in_universe(self):
        """BRK.B must be in the screener universe — was previously dropped by isalpha() bug."""
        import sectors
        universe = sectors.load_universe()
        assert "BRK.B" in universe

    def test_tier1_symbols_are_subset_of_universe(self):
        import sectors
        universe_set = set(sectors.load_universe())
        for sym in sectors.TIER1_SYMBOLS:
            assert sym in universe_set, f"{sym} in TIER1_SYMBOLS but not in screener universe"

    def test_etf_to_symbols_xlk_contains_nvda(self):
        import sectors
        assert "NVDA" in sectors.ETF_TO_SYMBOLS.get("XLK", [])
```

- [ ] **Step 7: Run full test suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```
Expected: all tests pass including new sectors tests.

- [ ] **Step 8: Commit**

```bash
git add tickers.json sectors.py config.py agents/volatility_screener.py agents/watch_list.py tests/test_foundation.py
git commit -m "feat: tickers.json + sectors.py universe refactor; fix B13 BRK.B screener drop"
```

---

## Task 11: Migrate all tier1_config callers → sectors.py; delete old files

**Files:**
- Modify: `scheduler_jobs.py` (2c)
- Modify: `research/agent.py` (2c)
- Modify: `research/analyzer.py` (2c)
- Modify: `signals/correlation_guard.py` (2e)
- Modify: `trading/modes.py` (2c)
- Modify: `trading/eod_holds.py` (2c)
- Modify: `trading/pipeline.py` (2d)
- Modify: `tests/test_foundation.py` (update TestTier1Config)
- Delete: `tier1_config.py`
- Delete: `tickers.txt`

- [ ] **Step 1: Update scheduler_jobs.py**

In `scheduler_jobs.py`, find the `TIER1_NAMES` import and usage (around line 50):
```python
        from tier1_config import TIER1_NAMES
        ...
        symbols = list(dict.fromkeys(watch_list + list(TIER1_NAMES.keys())))
```
Change to:
```python
        import sectors
        ...
        symbols = list(dict.fromkeys(watch_list + sectors.TIER1_SYMBOLS))
```

- [ ] **Step 2: Update research/agent.py**

In `research/agent.py`, find:
```python
from tier1_config import is_tier1
```
Change to:
```python
import sectors
```
Find the usage:
```python
        if is_tier1(symbol) and data.get("sentiment_score", 0) < -30:
```
Change to:
```python
        if sectors.is_tier1(symbol) and data.get("sentiment_score", 0) < -30:
```

- [ ] **Step 3: Update research/analyzer.py**

In `research/analyzer.py`, find:
```python
from tier1_config import get_sector
```
Change to:
```python
import sectors
```
Replace all `get_sector(` calls with `sectors.get_sector(`.

- [ ] **Step 4: Update signals/correlation_guard.py (2e)**

Replace the entire file content with:

```python
"""
Correlation guard: prevents opening multiple positions in the same sector ETF group.
Sector assignment is O(1) via sectors.py pre-built dict.
"""
from __future__ import annotations
import config
import sectors


def sector_of(symbol: str) -> str:
    """Returns the sector ETF key for a symbol, or 'OTHER' if unknown."""
    return sectors.get_sector_etf(symbol)


def passes_correlation_guard(symbol: str, open_positions: dict) -> bool:
    """Returns False if MAX_SAME_SECTOR_POSITIONS or more open positions share this symbol's sector."""
    my_sector = sector_of(symbol)
    if my_sector == "OTHER":
        return True  # never block unknown sectors

    same_sector_count = sum(
        1 for sym in open_positions
        if sector_of(sym) == my_sector
    )
    return same_sector_count < config.MAX_SAME_SECTOR_POSITIONS
```

- [ ] **Step 5: Update trading/modes.py**

In `trading/modes.py`, find:
```python
        from tier1_config import is_tier1
```
Change to:
```python
        import sectors
```
Find:
```python
        if pead_ok and is_tier1(symbol):
```
Change to:
```python
        if pead_ok and sectors.is_tier1(symbol):
```

- [ ] **Step 6: Update trading/eod_holds.py**

In `trading/eod_holds.py`, find:
```python
    from tier1_config import is_tier1, get_sector
```
Change to:
```python
    import sectors
```
Replace all `is_tier1(` calls with `sectors.is_tier1(` and all `get_sector(` calls with `sectors.get_sector(`.

- [ ] **Step 7: Update trading/pipeline.py (2d) — remove _SECTOR_ETF_MAP**

In `trading/pipeline.py`, delete the entire `_SECTOR_ETF_MAP` dict (lines 69–106, the 38-line dict).

Add import at the top of the file (after existing imports):
```python
import sectors
```

Find all `_SECTOR_ETF_MAP.get(symbol)` calls (there are 2, around lines 331 and 370) and replace each with:
```python
sectors.get_sector_etf(symbol)
```

- [ ] **Step 8: Update tests/test_foundation.py — replace TestTier1Config**

In `tests/test_foundation.py`, find and DELETE the entire `class TestTier1Config` block (lines 65–166) — this covers `is_tier1`, `get_sector`, and `qualifies_for_swing` tests that all reference the now-deleted `tier1_config`.

Replace with `class TestSectorsLegacyCompat` — verifying that sectors.py provides the same contract:

```python
# ── tier1_config → sectors.py migration ──────────────────────────────────────

@pytest.mark.unit
class TestSectorsLegacyCompat:
    """Verify sectors.py provides the same contract as the deleted tier1_config.py."""

    def test_known_symbol_is_tier1(self):
        import sectors
        assert sectors.is_tier1("NVDA") is True

    def test_case_insensitive(self):
        import sectors
        assert sectors.is_tier1("nvda") is True

    def test_unknown_symbol_is_not_tier1(self):
        import sectors
        assert sectors.is_tier1("FAKE") is False

    def test_get_sector_known_returns_etf_key(self):
        import sectors
        assert sectors.get_sector("NVDA") == "XLK"
        assert sectors.get_sector("JPM") == "XLF"
        assert sectors.get_sector("NEM") == "XLB"

    def test_get_sector_unknown_returns_other(self):
        import sectors
        assert sectors.get_sector("FAKE") == "OTHER"
```

- [ ] **Step 9: Delete tier1_config.py and tickers.txt**

```bash
rm tier1_config.py tickers.txt
```

- [ ] **Step 10: Run full test suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```
Expected: all tests pass. If any test imports from `tier1_config`, fix it to use `sectors`.

- [ ] **Step 11: Commit**

```bash
git add -A
git commit -m "refactor: migrate all callers to sectors.py; delete tier1_config.py + tickers.txt"
```

---

## Task 12: Dead code removal (D2, D3, D4)

**Files:**
- Modify: `trading/closer.py` (D2)
- Modify: `signals/entry_gates.py` (D3)
- Modify: `trading/pre_claude_filter.py` (D3)
- Modify: `trading/pipeline.py` (D3)
- Modify: `trading/cycle.py` (D3)
- Modify: `trading/modes.py` (D4)
- Modify: `trading/eod_holds.py` (D4)
- Modify: `tests/test_trading_lifecycle.py`

- [ ] **Step 1: D2 — Remove force_close_all scheduler param and pause/resume block**

In `trading/closer.py`, replace `force_close_all`:

```python
def force_close_all() -> None:
    """End-of-day: cancel orders, close all non-swing day-trade positions."""
    _force_close_body()
```

- [ ] **Step 2: Add a test for D2 in tests/test_trading_lifecycle.py**

Append to `tests/test_trading_lifecycle.py`:

```python
@pytest.mark.unit
class TestForceCloseAllSignature:
    def test_force_close_all_takes_no_args(self):
        """Signature check — scheduler param was dead code and has been removed."""
        import inspect
        from trading.closer import force_close_all
        sig = inspect.signature(force_close_all)
        assert len(sig.parameters) == 0, f"Expected 0 params, got: {list(sig.parameters)}"
```

- [ ] **Step 3: D3 — Replace inline HIGH_VOL tuple literals with regimes frozensets**

First add the import at the top of each affected file: `from utils import regimes`

**signals/entry_gates.py** — in `effective_composite_gate`, change:
```python
    if regime in ("HIGH_VOL", "DEFENSIVE"):
```
to:
```python
    if regime in regimes.HIGH_VOL_GATES:
```

**trading/pre_claude_filter.py** — change:
```python
        if snap.regime in ("HIGH_VOL", "CRISIS", "DEFENSIVE")
```
to:
```python
        if snap.regime in (regimes.HIGH_VOL_GATES | {regimes.CRISIS})
```
And change:
```python
        if regime in ("HIGH_VOL", "DEFENSIVE", "CHOPPY"):
```
Wait, that's in cycle.py. Let me specify exactly:

In `trading/pre_claude_filter.py`, line 54:
```python
        if snap.regime in ("HIGH_VOL", "CRISIS", "DEFENSIVE")
```
→
```python
        if snap.regime in regimes.HIGH_VOL_GATES | {regimes.CRISIS}
```

**trading/pipeline.py** — around line 192–193:
```python
            if regime in ("HIGH_VOL", "CRISIS", "DEFENSIVE")
```
→
```python
            if regime in regimes.HIGH_VOL_GATES | {regimes.CRISIS}
```

Around line 197–198:
```python
            if regime in ("HIGH_VOL", "DEFENSIVE", "CHOPPY")
```
→
```python
            if regime in regimes.SIZE_REDUCE
```

**trading/cycle.py** — around line 94:
```python
    if regime in ("HIGH_VOL", "DEFENSIVE", "CHOPPY"):
```
→
```python
    if regime in regimes.SIZE_REDUCE:
```

- [ ] **Step 4: D4 — Replace local frozensets in modes.py with regimes imports**

In `trading/modes.py`, add `from utils import regimes` at the top.

Delete:
```python
_HYBRID_REGIMES     = frozenset({"BULL_TREND", "OPPORTUNITY"})
_SHORT_TERM_REGIMES = frozenset({"CAPITULATION", "CRASH_RECOVERY"})
```

Replace usages:
```python
    if regime in _HYBRID_REGIMES:
```
→
```python
    if regime in regimes.SWING_ELIGIBLE and regime in (regimes.BULL_TREND, regimes.OPPORTUNITY):
```

Actually that's verbose. Better: create module-level aliases to avoid changing the `_SL_TP_TABLE` dict entries.

Replace:
```python
_HYBRID_REGIMES     = frozenset({"BULL_TREND", "OPPORTUNITY"})
_SHORT_TERM_REGIMES = frozenset({"CAPITULATION", "CRASH_RECOVERY"})
```
with:
```python
from utils import regimes as _r
_HYBRID_REGIMES     = frozenset({_r.BULL_TREND, _r.OPPORTUNITY})
_SHORT_TERM_REGIMES = frozenset({_r.CAPITULATION, _r.CRASH_RECOVERY})
```
(Keep the local names so the membership checks don't need to change.)

- [ ] **Step 5: D4 — Replace _SWING_ELIGIBLE_REGIMES in eod_holds.py**

In `trading/eod_holds.py`, add `from utils import regimes` at the top.

Delete:
```python
_SWING_ELIGIBLE_REGIMES = {"BULL_TREND", "OPPORTUNITY", "CAPITULATION", "CRASH_RECOVERY"}
```

Replace all usages of `_SWING_ELIGIBLE_REGIMES` with `regimes.SWING_ELIGIBLE`.

- [ ] **Step 6: Run full test suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```
Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add trading/closer.py signals/entry_gates.py trading/pre_claude_filter.py trading/pipeline.py trading/cycle.py trading/modes.py trading/eod_holds.py tests/test_trading_lifecycle.py
git commit -m "refactor: D2/D3/D4 — remove dead scheduler param, replace HIGH_VOL tuples with regimes frozensets"
```

---

## Task 13: config.py reorganization into 11 sections

**Files:**
- Modify: `config.py`

Rewrite config.py in-place, grouping into 11 labeled sections. No values change. Additions from Task 1 are already in the file.

- [ ] **Step 1: Rewrite config.py with section headers**

The 11 sections in order:

```
# ── PATHS ─────────────────────────────────────────────────────────────────────
```
Contains: BASE_DIR, STATE_DIR, all `*_FILE` and `*_DIR` path constants.

```
# ── API KEYS & MODELS ─────────────────────────────────────────────────────────
```
Contains: ALPACA_*, ANTHROPIC_API_KEY, CLAUDE_MODEL, X_BEARER_TOKEN, POLYGON_API_KEY, FRED_API_KEY, MARKET_DATA_PROVIDER.

```
# ── RISK MANAGEMENT ───────────────────────────────────────────────────────────
```
Contains: MAX_POSITION_PCT, RISK_PER_TRADE_PCT, STOP_LOSS_PCT, TAKE_PROFIT_PCT, MAX_DAILY_LOSS_PCT, DAILY_LOSS_WARN_PCT, MIN_BUYING_POWER, MAX_DAILY_LOSS_PCT-related, PROGRESSIVE_LOSS_*.

```
# ── POSITION SIZING ───────────────────────────────────────────────────────────
```
Contains: sentiment sizing tiers, POST_EVENT_POSITION_MULT, HIGH_VOL_POSITION_MULT, SWING_MAX_POSITIONS, SHORT_TERM_MAX_POSITIONS, SWING_TP*/SL* constants.

```
# ── ENTRY GATES & TIMING ──────────────────────────────────────────────────────
```
Contains: ENTRY_START/END_HOUR, ENTRY_GATE_OPEN/CLOSE_MINUTES, MAX_ENTRIES_PER_CYCLE, RSI_BUY_MAX, RSI_BUY_MAX_HIGH_VOL, STOCH_*, GAP_SKIP_PCT, CHOPPY_RSI_PULLBACK_MAX, SECTOR_ETF_SOFT_DROP_PCT, BREADTH_WEAK_THRESHOLD, REL_VOLUME_MIN, OPENING_RANGE_MINUTES.

```
# ── INDICATOR & COMPOSITE THRESHOLDS ─────────────────────────────────────────
```
Contains: COMPOSITE_MIN_NORMAL, COMPOSITE_MIN_HIGH_VOL, CHOPPY_COMPOSITE_GATE, HERD_ENTRY_GATE, AUTO_EXECUTE_*, SQUEEZE_*, BOT_HERD_*, VWAP_SLOPE_MIN, VWAP_OVEREXTENSION_GATE, PARABOLIC_*, RS_MIN_RATIO, MTF_*, STOP_HUNT_*, VOLUME_EXHAUSTION_RATIO, DEFENSIVE_COMPOSITE_MULT, SQUEEZE_COMPOSITE_MIN.

```
# ── REGIME & MACRO ────────────────────────────────────────────────────────────
```
Contains: MACRO_HEALTH_*, CONSECUTIVE_BULL/BEAR_DAYS, CHOPPY_MIN_SIGN_ALTS, CHOPPY_MAX_NET_MOVE_PCT, REGIME_ADAPTIVE_LOGIC, FLASH_CRASH_*, MACRO_BREADTH_TICKERS, MACRO_SHOCK_SKIP_THRESHOLD, MACRO_HEALTH_SKIP_THRESHOLD.

```
# ── SWING & EOD ───────────────────────────────────────────────────────────────
```
Contains: SWING_*, EOD_*, TRAILING_STOP_*, PROFIT_LOCK_TRIGGER_PCT, SWING_GAP_*.

```
# ── BUDGET & API COST ─────────────────────────────────────────────────────────
```
Contains: API_BUDGET_*, ENTRY_BUDGET_RATIO, EXIT_BUDGET_RATIO, EXIT_EVAL_*, BUDGET_ROLLOVER_*, ENTRY_BUDGET_WARNING_PCT, CLAUDE_BATCH_SIZE, CLAUDE_CB_*.

```
# ── LEARNING & MEMORY ─────────────────────────────────────────────────────────
```
Contains: LEARNING_*, INDICATOR_WEIGHT_*, STRATEGY_MEMORY_*, TRADE_JOURNAL_MAX_DAYS, ATR_TP_*, CORRUPT_BACKUP_MAX_DAYS, METRICS_KEEP_LINES.

```
# ── NOTIFICATIONS, OPS & FEATURE FLAGS ───────────────────────────────────────
```
Contains: EMAIL_*, TELEGRAM_*, DEAD_DAY_ALERT_CYCLES, WATCHDOG_HEARTBEAT_TIMEOUT_S, TELEMETRY_KEEP_LINES, CYCLE_TELEMETRY_FILE, BOT_HEALTH_FILE, CRASH_LOOP_*, CRASH_BACKOFF_*, MAX_TICKERS_DIRECT, WATCH_LIST_*, DRAWDOWN_WARN_PCT, DRAWDOWN_ALERT_COOLDOWN_MIN, INTRADAY_*, RESEARCH_STALE_*, FETCH_WORKERS, FILL_SLIPPAGE_THRESHOLD, FILL_POLL_*, BAR_TIMEFRAME_MINUTES, BARS_LOOKBACK, DAILY_BARS_LOOKBACK.

- [ ] **Step 2: Run full test suite after reorganization**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```
Expected: all tests pass (only comments and ordering changed, no value changes).

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "refactor: reorganize config.py into 11 labeled sections"
```

---

## Self-Review

**Spec coverage check:**

| Spec item | Covered by task |
|-----------|----------------|
| B1 journal action→type | Task 3 |
| B2 midday check action→type | Task 3 |
| B3 regime winrate action→type | Task 3 |
| B4 composite_min_normal relaxation | Task 4 |
| B5 DEFENSIVE 52.5 floor | Task 5 |
| B6 magic number 12 | Task 5 |
| B7 stale comment + CLAUDE.md | Task 5 |
| B8 missing params defaults + choppy_rsi_max | Task 6 |
| B9 metrics.jsonl rotation | Task 7 |
| B10 drawdown cooldown | Task 8 |
| B11 AlpacaClient outside loop | Task 9 |
| B12 CycleResult.skipped | Task 9 |
| B13 BRK.B isalpha() drop | Task 10 |
| 2a tickers.json | Task 10 |
| 2b sectors.py | Task 10 |
| 2c tier1_config callers | Task 11 |
| 2d pipeline _SECTOR_ETF_MAP removed | Task 11 |
| 2e correlation_guard _EXTENDED_SECTOR removed | Task 11 |
| 2f screener loads from sectors | Task 10 |
| 2g watch_list fallback updated | Task 10 |
| 3a utils/regimes.py | Task 2 |
| D1 qualifies_for_swing dead code | Deleted with tier1_config in Task 11 |
| D2 force_close_all scheduler param | Task 12 |
| D3 HIGH_VOL unreachable branches | Task 12 |
| D4 inline frozensets in modes/eod_holds | Task 12 |
| Part 4 config.py reorganization | Task 13 |
| Testing: sectors tests | Task 10 |
| Testing: regimes tests | Task 2 |
| Testing: B1 progressive loss | Task 3 (existing tests updated) |
| Testing: B2 midday test | Task 3 (production fix; test in scheduler_jobs.py call path) |
| Testing: force_close_all signature | Task 12 |

**Placeholder scan:** No TBDs, no incomplete sections.

**Type consistency:** `effective_composite_gate(iv, regime, params=None)` — params used consistently in Tasks 4 and 12. `sectors.get_sector_etf()` returns `str` — used consistently in Tasks 10, 11, 12. `regimes.HIGH_VOL_GATES` is `frozenset` — used with `in` operator correctly.

**Note on test_market.py and test_trading_budget.py:** Both files are already over 700 lines. The new tests in Task 3 update existing test fixtures (no new lines added). The new tests in Task 12 go into `test_trading_lifecycle.py` (515 lines — within limit).
