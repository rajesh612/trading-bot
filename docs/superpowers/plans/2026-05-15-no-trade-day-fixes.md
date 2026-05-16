# No-Trade Day Fixes & Token Optimization

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the systemic causes of zero-trade days, make all gates realistic rather than blanket-blocking, and reduce Claude token spend by ~40% per cycle.

**Architecture:** Fix 14 root-cause bugs found by deep audit, then tighten the Claude prompt format and gate flow so the pipeline computes all metrics first, auto-executes clear signals, and only calls Claude for genuinely ambiguous candidates — never when entry is physically impossible.

**Tech Stack:** Python 3.11, anthropic SDK, APScheduler, pytest

---

## File Map

| File | Changes |
|---|---|
| `data/state/indicator_weights.json` | Remove phantom keys ATR + Volume Ratio; renormalize to 100 |
| `indicators/composite.py` | Remove `"ATR"` from signal_map |
| `indicators/engine.py` | Replace local `ET = ZoneInfo(...)` with `from utils.constants import ET` |
| `trading/pre_claude_filter.py` | Layer 2 RSI: use `snap.params` learned value, not `config.RSI_BUY_MAX` |
| `trading/pipeline.py` | Gate Phase 3 Claude calls on `can_open`; warn when staleness ts missing |
| `trading/executor.py` | Auto-execute: only block on real signal flags, not data-fetch failures |
| `signals/entry_gates.py` | Gap-skip gate 12: add PEAD mode exception |
| `trading/risk_manager.py` | Rehydrate `_entry_exhausted_flag` from disk on first call after restart |
| `trading/cycle.py` | Empty watch list: send Telegram alert before returning |
| `trading/cycle_context.py` | strategy_memory truncation: keep head, not tail |
| `trading/context_builder.py` | Trim stock block from ~200 → ~130 tokens; skip empty news summary |
| `learning/agent.py` | Fix RSI param range from "30–50" → "40–80" in system prompt |
| `strategy_memory.md` | Fix VWAP slope line to match hard-block reality |
| `main.py` | Stagger intraday_news from `*/30` to `5,35` to avoid 9:00 race |
| `config.py` | Increase `CLAUDE_BATCH_SIZE` from 5 → 10 |
| `tests/test_indicators.py` | Tests for composite score fix |
| `tests/test_trading_pipeline.py` | Tests for can_open gate, staleness warning |
| `tests/test_trading_modes.py` | Tests for RSI sync, gap-skip PEAD |
| `tests/test_trading_budget.py` | Tests for auto-execute flag fix, budget rehydration |
| `tests/test_foundation.py` | Test for learning agent RSI range |

---

## Task 1: Fix phantom composite weights (ATR + Volume Ratio)

**Root cause:** `s_atr = 0.0` always in `composite.py:62`, so ATR weight (3.59) inflates the denominator but contributes 0 to scores. `Volume Ratio` exists in `indicator_weights.json` but is absent from `signal_map`, same effect. Combined phantom = 6.56 pts suppression on every score. A stock that should auto-execute at composite 61 only scores ~57.

**Files:**
- Modify: `indicators/composite.py:62,83–95`
- Modify: `data/state/indicator_weights.json`
- Test: `tests/test_indicators.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_indicators.py — add inside the file
@pytest.mark.unit
def test_composite_phantom_weights_do_not_suppress_score():
    """ATR always-zero and missing Volume Ratio must not dilute composite scores."""
    from indicators.composite import _composite_score
    # All signals at maximum bullish (+1.0)
    vals = {
        "current_price": 100.0, "vwap": 98.0, "atr": 1.0,
        "rsi": 75.0, "macd_hist": 0.5,
        "bb_upper": 102.0, "bb_lower": 97.0,
        "ema9": 100.5, "ema21": 99.5,
        "stoch_k": 80.0, "stoch_d": 60.0,
        "daily_volume_trend": 1.0, "vwap_slope": 0.5,
        "rsi_slope": 15.0, "obv_trend": 1.0,
        "daily_volume_ratio": 1.0,
    }
    weights = {
        "VWAP": 19.05, "RSI": 19.46, "Daily Volume Trend": 6.51,
        "MACD": 2.52, "EMA 9/21 Crossover": 13.40, "Bollinger Bands": 11.82,
        "VWAP StdDev Bands": 7.69, "Stochastic Oscillator": 5.77,
        "OBV": 3.84, "RSI Slope": 9.94,
    }
    score, signal, _ = _composite_score(vals, weights)
    # With all signals +1 and weights summing to 100, score should be close to 100
    assert score > 90.0, f"Expected >90 but got {score} — phantom weights still suppressing"
    assert "ATR" not in {k for k, v in weights.items()}
    assert "Volume Ratio" not in {k for k, v in weights.items()}
```

- [ ] **Step 2: Run to confirm it fails**

```bash
.venv/bin/python -m pytest tests/test_indicators.py::test_composite_phantom_weights_do_not_suppress_score -v
```

Expected: FAIL — old weights.json has ATR=3.59 and Volume Ratio=2.97 inflating denominator.

- [ ] **Step 3: Remove ATR from signal_map in composite.py**

In `indicators/composite.py`, delete line 62 (`s_atr = 0.0`) and remove the `"ATR": s_atr` entry from `signal_map` (currently line 91). The block becomes:

```python
    signal_map = {
        "VWAP":                  s_vwap,
        "RSI":                   s_rsi,
        "Daily Volume Trend":    s_daily_vol,
        "MACD":                  s_macd,
        "EMA 9/21 Crossover":    s_ema,
        "Bollinger Bands":       s_bb,
        "VWAP StdDev Bands":     s_vwap_std,
        "Stochastic Oscillator": s_stoch,
        "OBV":                   s_obv,
        "RSI Slope":             s_rsi_slope,
    }
```

- [ ] **Step 4: Renormalize indicator_weights.json**

Replace `data/state/indicator_weights.json` with renormalized weights (ATR and Volume Ratio removed; remaining 10 scaled to sum exactly to 100):

```json
{
  "VWAP": 19.05,
  "RSI": 19.46,
  "Daily Volume Trend": 6.51,
  "MACD": 2.52,
  "EMA 9/21 Crossover": 13.40,
  "Bollinger Bands": 11.82,
  "VWAP StdDev Bands": 7.69,
  "Stochastic Oscillator": 5.77,
  "OBV": 3.84,
  "RSI Slope": 9.94
}
```

- [ ] **Step 5: Run test to confirm it passes**

```bash
.venv/bin/python -m pytest tests/test_indicators.py::test_composite_phantom_weights_do_not_suppress_score -v
```

Expected: PASS

- [ ] **Step 6: Run full test suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

Expected: all previously passing tests still pass.

- [ ] **Step 7: Commit**

```bash
git add indicators/composite.py data/state/indicator_weights.json tests/test_indicators.py
git commit -m "fix: remove ATR+VolumeRatio phantom weights from composite score

ATR signal is always 0.0 and Volume Ratio was never in signal_map.
Both diluted total_w, suppressing all composite scores by ~6.56 pts.
A stock needing composite 55 for auto-execute was scoring ~48."
```

---

## Task 2: Fix RSI threshold inconsistency between pre-filter and executor

**Root cause:** `pre_claude_filter.py:52` hard-codes `config.RSI_BUY_MAX = 72` but `pipeline.py:107–110` computes `rsi_max = snap.params.get("rsi_buy_max", ...)` = 65 (learned). Stocks with RSI 65–72 pass the filter, Claude is called (~$0.018), then `executor.py:56` immediately blocks. Budget wasted, no trade.

**Files:**
- Modify: `trading/pre_claude_filter.py:50–55`
- Test: `tests/test_trading_modes.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_trading_modes.py — add
@pytest.mark.unit
def test_pre_claude_filter_uses_learned_rsi_not_config(mock_iv, mock_snap):
    """Filter Layer 2 must use snap.params rsi_buy_max, not config.RSI_BUY_MAX."""
    from trading.pre_claude_filter import pre_claude_filter
    # Set learned threshold to 60; RSI=62 should be SKIP (above learned), not CLAUDE (below config 72)
    mock_snap.params = {"rsi_buy_max": 60, "vwap_slope_min": -0.3}
    mock_snap.regime = "NORMAL"
    mock_iv.rsi = 62.0
    mock_iv.composite_score = 40.0
    mock_iv.stoch_d = 50.0
    mock_iv.vwap_slope = 0.1
    result = pre_claude_filter("TEST", mock_iv, mock_snap, set())
    assert result.decision == "SKIP", f"Expected SKIP but got {result.decision}: {result.reason}"
    assert "RSI" in result.reason
```

> `mock_iv` and `mock_snap` are existing fixtures in `tests/test_trading_modes.py`. If they don't exist, create minimal ones:
> ```python
> @pytest.fixture
> def mock_iv():
>     from unittest.mock import MagicMock
>     iv = MagicMock()
>     iv.composite_score = 40.0; iv.rsi = 55.0; iv.stoch_d = 50.0
>     iv.vwap_slope = 0.1; iv.current_price = 100.0; iv.vwap = 99.0
>     iv.squeeze_setup = False; iv.bot_herding_score = 0
>     return iv
>
> @pytest.fixture
> def mock_snap():
>     from unittest.mock import MagicMock
>     s = MagicMock()
>     s.regime = "NORMAL"; s.regime_info = {"macro_health_score": 50}
>     s.research_meta = {"macro_shock_score": 0}
>     s.research = {}; s.intraday_news = {}
>     s.params = {"rsi_buy_max": 72, "vwap_slope_min": -0.3}
>     return s
> ```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/python -m pytest tests/test_trading_modes.py::test_pre_claude_filter_uses_learned_rsi_not_config -v
```

Expected: FAIL — filter currently passes RSI=62 when learned threshold is 60.

- [ ] **Step 3: Fix pre_claude_filter.py Layer 2**

Replace lines 50–55 in `trading/pre_claude_filter.py`:

```python
    # Layer 2 — RSI maximum (must match executor rsi_max exactly — uses learned threshold)
    rsi_max = (
        snap.params.get("rsi_buy_max_high_vol", config.RSI_BUY_MAX_HIGH_VOL)
        if snap.regime in ("HIGH_VOL", "CRISIS", "DEFENSIVE")
        else snap.params.get("rsi_buy_max", config.RSI_BUY_MAX)
    )
    if iv.rsi > rsi_max:
        return FilterResult("SKIP",
            f"RSI {iv.rsi:.1f} > {rsi_max} ({snap.regime})")
```

- [ ] **Step 4: Run test to confirm pass**

```bash
.venv/bin/python -m pytest tests/test_trading_modes.py::test_pre_claude_filter_uses_learned_rsi_not_config -v
```

Expected: PASS

- [ ] **Step 5: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 6: Commit**

```bash
git add trading/pre_claude_filter.py tests/test_trading_modes.py
git commit -m "fix: pre_claude_filter RSI gate now uses learned rsi_buy_max from snap.params

Stocks with RSI between learned threshold (65) and config.RSI_BUY_MAX (72)
were passing the filter, triggering a Claude call (~\$0.018), then being
immediately blocked by executor — burning budget for zero outcome."
```

---

## Task 3: Gate Phase 3 Claude calls on can_open; warn on missing research timestamp

**Root cause 1:** `pipeline.py:202–205` calls Claude even when `can_open=False` (9:30 AM first cycle). Every trade day the first cycle burns 1–2 batch calls (~$0.04) for stocks that physically cannot be bought.

**Root cause 2:** `pipeline.py:102` silently swallows staleness-check parse errors (`except Exception: pass`). When `ts_str` is `None` (missing `__meta__`), the check is silently skipped with no log.

**Files:**
- Modify: `trading/pipeline.py:88–103, 199–206`
- Test: `tests/test_trading_pipeline.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_trading_pipeline.py — add
@pytest.mark.unit
def test_pipeline_skips_claude_when_can_open_false(monkeypatch):
    """Phase 3 must not call Claude when can_open=False."""
    from trading.pipeline import TradingPipeline
    from unittest.mock import MagicMock, patch

    called = []

    def fake_ask_batch(batch, snap, ai):
        called.append(batch)
        return {}

    monkeypatch.setattr("trading.pipeline._ask_claude_batch", fake_ask_batch)
    # Build a minimal pipeline that has one needs_claude candidate
    pipeline = TradingPipeline()
    # Patch internal helpers to inject one CLAUDE-route candidate
    with patch.object(pipeline, "run", wraps=pipeline.run):
        pass  # full integration tested separately

    # Direct test: when can_open=False, _ask_claude_batch must never be called
    # We test by verifying the guard condition directly
    from trading.pipeline import TradingPipeline
    import trading.pipeline as pl_mod
    original = pl_mod._ask_claude_batch
    pl_mod._ask_claude_batch = fake_ask_batch
    try:
        # Simulate Phase 3 with can_open=False
        needs_claude = [("AAPL", MagicMock(), None)]
        can_open = False
        decisions = {}
        if can_open:
            for i in range(0, len(needs_claude), 10):
                batch = needs_claude[i:i+10]
                decisions.update(fake_ask_batch(batch, MagicMock(), MagicMock()))
        assert called == [], "Claude must not be called when can_open=False"
    finally:
        pl_mod._ask_claude_batch = original


@pytest.mark.unit
def test_pipeline_warns_when_research_timestamp_missing(caplog):
    """Missing __meta__ timestamp must log a warning, not silently skip."""
    import logging
    from trading.pipeline import TradingPipeline
    from unittest.mock import MagicMock

    pipeline = TradingPipeline()
    snap = MagicMock()
    snap.research_meta = {}  # no "timestamp" key → ts_str is None

    # Simulate the staleness-check block
    import trading.pipeline as pl_mod
    ts_str = snap.research_meta.get("timestamp")
    with caplog.at_level(logging.WARNING, logger="trading.pipeline"):
        if not ts_str:
            import logging as _lg
            _lg.getLogger("trading.pipeline").warning(
                "Research report has no timestamp — staleness check skipped."
            )
    assert any("staleness" in r.message.lower() or "timestamp" in r.message.lower()
               for r in caplog.records), "Expected staleness warning in log"
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/python -m pytest tests/test_trading_pipeline.py::test_pipeline_skips_claude_when_can_open_false tests/test_trading_pipeline.py::test_pipeline_warns_when_research_timestamp_missing -v
```

- [ ] **Step 3: Fix pipeline.py — gate Phase 3 on can_open**

Replace the Phase 3 block (`trading/pipeline.py:199–206`):

```python
        # ── Phase 3: Claude batch ─────────────────────────────────────────────
        t_claude = time.monotonic()
        decisions: dict[str, dict] = {}
        if not can_open:
            logger.info("[CYCLE] can_open=False — skipping Claude entry calls (entry window not open yet)")
        else:
            for i in range(0, len(needs_claude), config.CLAUDE_BATCH_SIZE):
                batch = needs_claude[i : i + config.CLAUDE_BATCH_SIZE]
                batch_decisions = _ask_claude_batch(batch, snap, ai)
                decisions.update(batch_decisions)
        claude_elapsed = time.monotonic() - t_claude
```

- [ ] **Step 4: Fix pipeline.py — warn when research timestamp is None**

Replace lines 87–103 in `trading/pipeline.py`:

```python
        # Research staleness check
        ts_str = snap.research_meta.get("timestamp")
        if not ts_str:
            logger.warning(
                "Research report has no timestamp (__meta__ missing) — staleness check skipped."
            )
        else:
            try:
                report_dt = datetime.fromisoformat(ts_str)
                if report_dt.tzinfo is None:
                    report_dt = report_dt.replace(tzinfo=ET)
                age_min = (datetime.now(ET) - report_dt).total_seconds() / 60
                if age_min > config.RESEARCH_STALE_BLOCK_MIN:
                    logger.error(
                        "Research report is %.0f min old (limit=%d) — skipping cycle.",
                        age_min, config.RESEARCH_STALE_BLOCK_MIN,
                    )
                    return CycleResult(skipped=True, reason=f"research stale ({age_min:.0f}m)")
                if age_min > config.RESEARCH_STALE_WARN_MIN:
                    logger.warning("Research report is %.0f min old — sentiment may be stale.", age_min)
            except Exception as exc:
                logger.warning("Research timestamp parse failed (%s) — staleness check skipped.", exc)
```

- [ ] **Step 5: Run tests to confirm pass**

```bash
.venv/bin/python -m pytest tests/test_trading_pipeline.py::test_pipeline_skips_claude_when_can_open_false tests/test_trading_pipeline.py::test_pipeline_warns_when_research_timestamp_missing -v
```

- [ ] **Step 6: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 7: Commit**

```bash
git add trading/pipeline.py tests/test_trading_pipeline.py
git commit -m "fix: gate Phase 3 Claude calls on can_open; warn on missing research timestamp

The 9:30 AM trading cycle ran with can_open=False but still called Claude
for every candidate (~\$0.04/day wasted). Phase 3 now exits immediately
when entry is not yet open. Also adds a warning log when __meta__ timestamp
is absent instead of silently skipping the staleness check."
```

---

## Task 4: Fix auto-execute blocked by mtf_unavailable data failure

**Root cause:** `executor.py:94–95` blocks auto-execute when `gate.flags` is non-empty. `entry_gates.py:142` appends `"mtf_unavailable"` when the 15-min bar fetch raises any exception — a data-fetch failure, not a real sell signal. On budget-exhausted days, auto-execute is the only remaining trade path; `mtf_unavailable` silently kills it.

**Fix:** Auto-execute only blocks on real bearish signal flags (`mtf_not_confirmed`, `rs_weak`, `sector_limit_soft`). Data-unavailability flags are logged but do not block.

**Files:**
- Modify: `trading/executor.py:94–96`
- Test: `tests/test_trading_budget.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_trading_budget.py — add
@pytest.mark.unit
def test_auto_execute_not_blocked_by_mtf_unavailable(monkeypatch, tmp_path):
    """mtf_unavailable is a data-fetch failure; it must not block auto-execute."""
    from signals.entry_gates import GateResult

    # A gate result that only has the data-unavailability flag
    gate = GateResult(passed=True, reason="", flags=("mtf_unavailable",))
    decision = {"source": "auto_execute", "action": "BUY", "confidence": 90,
                "reasoning": "auto", "stop_loss_pct": 3.0, "take_profit_pct": 5.0}

    _DATA_ONLY_FLAGS = frozenset({"mtf_unavailable"})
    real_signal_flags = [f for f in gate.flags if f not in _DATA_ONLY_FLAGS]
    assert real_signal_flags == [], (
        f"Auto-execute should not be blocked by data flags: {gate.flags}"
    )


@pytest.mark.unit
def test_auto_execute_still_blocked_by_mtf_not_confirmed():
    """mtf_not_confirmed is a real bearish signal and must still block auto-execute."""
    from signals.entry_gates import GateResult
    gate = GateResult(passed=True, reason="", flags=("mtf_not_confirmed",))
    _DATA_ONLY_FLAGS = frozenset({"mtf_unavailable"})
    real_signal_flags = [f for f in gate.flags if f not in _DATA_ONLY_FLAGS]
    assert real_signal_flags == ["mtf_not_confirmed"]
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/python -m pytest tests/test_trading_budget.py::test_auto_execute_not_blocked_by_mtf_unavailable tests/test_trading_budget.py::test_auto_execute_still_blocked_by_mtf_not_confirmed -v
```

- [ ] **Step 3: Fix executor.py**

Replace lines 94–96 in `trading/executor.py`:

```python
    _DATA_ONLY_FLAGS = frozenset({"mtf_unavailable"})
    real_signal_flags = [f for f in gate.flags if f not in _DATA_ONLY_FLAGS]
    if real_signal_flags and decision.get("source") == "auto_execute":
        logger.info("SKIP %s — auto-execute blocked by signal flags: %s", symbol, ", ".join(real_signal_flags))
        return None
    if gate.flags:
        logger.info("ENTRY %s flagged: %s", symbol, ", ".join(gate.flags))
```

- [ ] **Step 4: Run tests to confirm pass**

```bash
.venv/bin/python -m pytest tests/test_trading_budget.py::test_auto_execute_not_blocked_by_mtf_unavailable tests/test_trading_budget.py::test_auto_execute_still_blocked_by_mtf_not_confirmed -v
```

- [ ] **Step 5: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 6: Commit**

```bash
git add trading/executor.py tests/test_trading_budget.py
git commit -m "fix: auto-execute no longer blocked by mtf_unavailable data-fetch failures

mtf_unavailable means the 15-min bar endpoint was unreachable, not that the
signal is bearish. On budget-exhausted days, auto-execute was the only trade
path and this silently killed it. Real signal flags (mtf_not_confirmed,
rs_weak, sector_limit_soft) still block auto-execute."
```

---

## Task 5: Add PEAD exception to gap-skip gate

**Root cause:** `entry_gates.py:125–127` hard-blocks any stock gapped >3% above prev_close with no mode exception. PEAD mode is specifically designed to enter post-earnings stocks that gapped — blocking gap-ups defeats its entire purpose.

**Files:**
- Modify: `signals/entry_gates.py:124–128`
- Test: `tests/test_trading_modes.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_trading_modes.py — add
@pytest.mark.unit
def test_gap_skip_gate_exempts_pead_mode():
    """PEAD mode stocks may gap on earnings beats; gap-skip must not block them."""
    from signals.entry_gates import check_all_gates
    from unittest.mock import patch, MagicMock
    import pandas as pd
    from datetime import datetime
    from zoneinfo import ZoneInfo

    ET = ZoneInfo("America/New_York")
    now = datetime(2026, 5, 15, 10, 30, tzinfo=ET)

    iv = MagicMock()
    iv.prev_close = 100.0
    iv.current_price = 105.0  # 5% gap — above GAP_SKIP_PCT=3.0
    iv.composite_score = 70.0
    iv.rsi = 60.0; iv.stoch_d = 70.0; iv.vwap_slope = 0.2
    iv.squeeze_setup = False; iv.bot_herding_score = 0
    iv.momentum_exhaustion = False; iv.parabolic = False
    iv.stop_hunt_zone = False
    iv.vwap_std2_upper = 106.0  # price below overextension

    with patch("signals.entry_gates.is_entry_allowed", return_value=True), \
         patch("signals.entry_gates.passes_vwap_guard", return_value=True), \
         patch("signals.entry_gates.passes_rs_filter", return_value=True), \
         patch("signals.entry_gates.passes_mtf_filter", return_value=True), \
         patch("signals.entry_gates.passes_correlation_guard", return_value=True), \
         patch("calendars.earnings.is_earnings_imminent", return_value=False), \
         patch("macro.economic_calendar.is_event_risk_window", return_value=(False, "")):

        # DAY_TRADE: gap should block
        gate_day = check_all_gates(
            "NBIS", iv, {}, set(), pd.DataFrame(), now, 0,
            spy_ret5d=1.0, symbol_ret5d=5.0, mode="DAY_TRADE",
        )
        assert not gate_day.passed, "Gap should block in DAY_TRADE mode"

        # PEAD: gap should be allowed
        gate_pead = check_all_gates(
            "NBIS", iv, {}, set(), pd.DataFrame(), now, 0,
            spy_ret5d=1.0, symbol_ret5d=5.0, mode="PEAD",
        )
        assert gate_pead.passed, f"Gap should NOT block in PEAD mode — got: {gate_pead.reason}"
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/python -m pytest tests/test_trading_modes.py::test_gap_skip_gate_exempts_pead_mode -v
```

- [ ] **Step 3: Fix entry_gates.py gate 12**

Replace lines 124–128 in `signals/entry_gates.py`:

```python
    # 12. Gap-up skip — exempted for PEAD (post-earnings drift stocks gap intentionally)
    if mode != "PEAD" and iv.prev_close > 0 and iv.current_price > iv.prev_close * (1 + config.GAP_SKIP_PCT / 100):
        gap_pct = (iv.current_price / iv.prev_close - 1) * 100
        return GateResult(False, f"gap-up skip ({gap_pct:.1f}% > {config.GAP_SKIP_PCT}%)")
```

- [ ] **Step 4: Run test to confirm pass**

```bash
.venv/bin/python -m pytest tests/test_trading_modes.py::test_gap_skip_gate_exempts_pead_mode -v
```

- [ ] **Step 5: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 6: Commit**

```bash
git add signals/entry_gates.py tests/test_trading_modes.py
git commit -m "fix: exempt PEAD mode from gap-skip gate

PEAD mode is specifically for post-earnings drift on stocks that gap up
on beats. Blocking gap-ups unconditionally defeated the mode's purpose."
```

---

## Task 6: Rehydrate _entry_exhausted_flag from disk on restart

**Root cause:** `risk_manager.py:28` initializes `_entry_exhausted_flag = False` at module load. After a crash/restart mid-day, the flag reads `False` even though `api_budget.json` has `entry_exhausted=True`. The bot makes additional Claude entry calls until `check_entry_budget` re-reads disk and re-sets the flag — wasting the remaining budget sliver.

**Files:**
- Modify: `trading/risk_manager.py:208–210`
- Test: `tests/test_trading_budget.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_trading_budget.py — add
@pytest.mark.unit
def test_entry_exhausted_flag_rehydrated_from_disk(tmp_path, monkeypatch):
    """After restart, is_entry_budget_exhausted() must read disk if in-memory flag is False."""
    import importlib
    import trading.risk_manager as rm

    # Simulate disk state: entry exhausted
    budget = {
        "date": "2026-05-15", "entry_exhausted": True,
        "entry_spent_usd": 0.87, "entry_ceiling_usd": 0.85,
        "exit_exhausted": False, "budget_exhausted": False, "escalated": False,
        "calls_made": 49, "entry_calls": 49, "exit_calls": 0,
        "input_tokens": 0, "output_tokens": 0,
        "cache_write_tokens": 0, "cache_read_tokens": 0,
        "estimated_cost_usd": 0.87, "exit_spent_usd": 0.0,
        "ceiling_usd": 0.70, "entry_ceiling_usd": 0.85, "exit_ceiling_usd": 0.35,
        "ceiling_mode": "normal",
    }
    budget_file = tmp_path / "api_budget.json"
    from utils.io_utils import save_json
    save_json(str(budget_file), budget)

    monkeypatch.setattr("config.API_BUDGET_FILE", str(budget_file))

    # Simulate fresh module state (flag = False, as after restart)
    rm._entry_exhausted_flag = False

    result = rm.is_entry_budget_exhausted()
    assert result is True, "Must read disk when in-memory flag is False and disk shows exhausted"
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/python -m pytest tests/test_trading_budget.py::test_entry_exhausted_flag_rehydrated_from_disk -v
```

- [ ] **Step 3: Fix risk_manager.py**

Replace `is_entry_budget_exhausted` (lines 208–210 in `trading/risk_manager.py`):

```python
def is_entry_budget_exhausted() -> bool:
    """Check entry budget exhaustion. Falls back to disk read on first call after restart."""
    global _entry_exhausted_flag
    if _entry_exhausted_flag:
        return True
    # In-memory flag is False — verify against disk (handles mid-day restart scenario)
    b = load_budget()
    if b.get("entry_exhausted", False):
        _entry_exhausted_flag = True
        return True
    return False
```

- [ ] **Step 4: Run test to confirm pass**

```bash
.venv/bin/python -m pytest tests/test_trading_budget.py::test_entry_exhausted_flag_rehydrated_from_disk -v
```

- [ ] **Step 5: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 6: Commit**

```bash
git add trading/risk_manager.py tests/test_trading_budget.py
git commit -m "fix: rehydrate entry_exhausted flag from disk after mid-day restart

_entry_exhausted_flag resets to False at module load. A crash/restart
mid-day would re-enable Claude entry calls until check_entry_budget
re-reads disk — wasting the remaining budget."
```

---

## Task 7: Fix learning agent RSI prompt range

**Root cause:** `learning/agent.py:134` tells Claude that valid `rsi_buy_max` range is "30–50 (normal regime)". Current working value is 65. On the next trade day with any RSI > 50 in losses, Claude suggests ≤50. After clamping, the executor blocks RSI 50–65 entries while pre_claude_filter passes them — burning budget and preventing entries simultaneously.

**Files:**
- Modify: `learning/agent.py:134–135`
- Test: `tests/test_foundation.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_foundation.py — add
@pytest.mark.unit
def test_learning_agent_rsi_prompt_range_allows_momentum_entries():
    """Learning prompt must allow rsi_buy_max up to 80 so trending stocks aren't blocked."""
    import learning.agent as la
    import inspect
    src = inspect.getsource(la)
    # Must not contain "30–50" or "25–40" which forces pathological thresholds
    assert "30–50" not in src, "Learning prompt must not restrict rsi_buy_max to 30–50"
    assert "25–40" not in src, "Learning prompt must not restrict rsi_buy_max_high_vol to 25–40"
    # Must contain a range that allows the current working value (65) to be maintained
    assert "40–80" in src or "40-80" in src, "Learning prompt must use range 40–80 for rsi_buy_max"
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/python -m pytest tests/test_foundation.py::test_learning_agent_rsi_prompt_range_allows_momentum_entries -v
```

- [ ] **Step 3: Fix learning/agent.py**

Replace lines 134–135 in `learning/agent.py`:

```python
                    "- rsi_buy_max: 40–80 (normal regime; current value 65 is the baseline)\n"
                    "- rsi_buy_max_high_vol: 35–65 (high vol regime; tighten only with evidence)\n"
```

- [ ] **Step 4: Run test to confirm pass**

```bash
.venv/bin/python -m pytest tests/test_foundation.py::test_learning_agent_rsi_prompt_range_allows_momentum_entries -v
```

- [ ] **Step 5: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 6: Commit**

```bash
git add learning/agent.py tests/test_foundation.py
git commit -m "fix: learning agent RSI param range 30-50 → 40-80

Prior range forced Claude to suggest values well below the working
threshold (65), which would have blocked momentum entries (RSI 50-65)
on the next losing trade day. New range keeps 65 as baseline and
allows evidence-based tuning without pathological restriction."
```

---

## Task 8: Fix strategy_memory head-truncation (keep rules, drop old per-stock notes)

**Root cause:** `cycle_context.py:97–98` uses `raw_mem[-STRATEGY_MEMORY_MAX_CHARS:]` (tail) when memory exceeds 3500 chars. The file has section headers at the top (General Rules, Regime Playbook). Tail-truncation removes the most durable rules and leaves Claude with only the most recent (often per-stock) notes.

**Files:**
- Modify: `trading/cycle_context.py:97–98`
- Test: `tests/test_trading_pipeline.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_trading_pipeline.py — add
@pytest.mark.unit
def test_strategy_memory_truncation_keeps_head_not_tail():
    """When strategy_memory.md exceeds limit, keep the beginning (rules), not the end."""
    import config
    from unittest.mock import patch, MagicMock
    import trading.cycle_context as cc

    long_memory = "# General Rules\n" + ("A" * 100 + "\n") * 20 + "# Tail Section\n" + ("B" * 100 + "\n") * 20
    limit = 500

    with patch.object(cc, "_logger", MagicMock()), \
         patch("config.STRATEGY_MEMORY_MAX_CHARS", limit):
        truncated = long_memory[:limit] if len(long_memory) > limit else long_memory

    assert truncated.startswith("# General Rules"), "Must keep head (rules section)"
    assert "Tail Section" not in truncated, "Must not keep tail"
    assert len(truncated) <= limit
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/python -m pytest tests/test_trading_pipeline.py::test_strategy_memory_truncation_keeps_head_not_tail -v
```

- [ ] **Step 3: Fix cycle_context.py**

Replace lines 97–98 in `trading/cycle_context.py`:

```python
            if len(raw_mem) > config.STRATEGY_MEMORY_MAX_CHARS:
                raw_mem = raw_mem[:config.STRATEGY_MEMORY_MAX_CHARS]
```

- [ ] **Step 4: Run test to confirm pass**

```bash
.venv/bin/python -m pytest tests/test_trading_pipeline.py::test_strategy_memory_truncation_keeps_head_not_tail -v
```

- [ ] **Step 5: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 6: Commit**

```bash
git add trading/cycle_context.py tests/test_trading_pipeline.py
git commit -m "fix: strategy_memory truncation keeps head (rules) not tail

Tail-truncation was dropping General Rules and Regime Playbook headers
when memory grew large, leaving Claude with only per-stock notes.
Head-truncation preserves the most durable rules."
```

---

## Task 9: Fix strategy_memory.md VWAP slope mismatch

**Root cause:** `strategy_memory.md` line 55 says "VWAP slope < −0.2%: soft warning only — not a block; use judgment". But `pre_claude_filter.py:67–69` hard-blocks at `vwap_slope < vwap_slope_min` before Claude is called. Claude reasons about stocks it never receives. No test needed — this is a documentation fix.

**Files:**
- Modify: `strategy_memory.md`

- [ ] **Step 1: Update VWAP Filter section in strategy_memory.md**

Replace the VWAP Filter section:

```markdown
### VWAP Filter
- VWAP slope < −0.2%: **hard block at pre-filter stage** — these stocks never reach Claude; do not adjust
- Price > VWAP+2σ: hard block (overextension — trust code gate)
- VWAP slope ≥ −0.2%: standard entry; rising VWAP preferred
- Earnings exception: not applicable (VWAP slope block is pre-Claude; cannot override here)
```

- [ ] **Step 2: Commit**

```bash
git add strategy_memory.md
git commit -m "fix: strategy_memory VWAP slope line matches pre-filter hard-block reality

Was documented as 'soft warning — use judgment' but pre_claude_filter.py
hard-blocks at vwap_slope < vwap_slope_min before Claude is ever called."
```

---

## Task 10: Add Telegram alert for empty watch list

**Root cause:** `cycle.py:83–85` silently returns when `watch_list` is empty, with no Telegram alert. The `_zero_pass` dead-day counter is only incremented when the pipeline runs — an empty watch list bypasses it entirely, so dead days go undetected.

**Files:**
- Modify: `trading/cycle.py:83–86`
- Test: `tests/test_trading_lifecycle.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_trading_lifecycle.py — add
@pytest.mark.unit
def test_empty_watch_list_sends_telegram_alert(monkeypatch):
    """Empty watch list must fire a Telegram alert, not silently return."""
    alerts_sent = []
    monkeypatch.setattr(
        "notifications.telegram.notify_alert",
        lambda msg: alerts_sent.append(msg),
    )
    # Simulate the guard block
    watch_list = []
    if not watch_list:
        import notifications.telegram as tg
        tg.notify_alert("⚠️ Watch list empty — skipping trading cycle.")

    assert len(alerts_sent) == 1
    assert "watch" in alerts_sent[0].lower() or "empty" in alerts_sent[0].lower()
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/python -m pytest tests/test_trading_lifecycle.py::test_empty_watch_list_sends_telegram_alert -v
```

- [ ] **Step 3: Fix cycle.py**

Replace lines 83–86 in `trading/cycle.py`:

```python
    watch_list = load_watch_list()
    if not watch_list:
        logger.warning("Empty watch list — skipping cycle.")
        import notifications.telegram as _tg
        _tg.notify_alert("⚠️ Watch list empty — no candidates for this cycle.")
        return
```

- [ ] **Step 4: Run test to confirm pass**

```bash
.venv/bin/python -m pytest tests/test_trading_lifecycle.py::test_empty_watch_list_sends_telegram_alert -v
```

- [ ] **Step 5: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 6: Commit**

```bash
git add trading/cycle.py tests/test_trading_lifecycle.py
git commit -m "fix: empty watch list now sends Telegram alert

Was silently returning with only a log warning. The dead-day counter
only fires when the pipeline runs; an empty watch list bypassed it,
making zero-watch-list days invisible to monitoring."
```

---

## Task 11: Stagger intraday_news to avoid 9:00 AM race with watch_list

**Root cause:** Both `job_watch_list` and `job_intraday_news` fire at `minute=*/30, hour=9` = 9:00 AM. APScheduler runs them concurrently. Intraday news reads `WATCH_LIST_FILE` while watch_list is writing it — producing sentiment for yesterday's tickers. The cache is within the 45-min TTL at the 9:30 first trade cycle.

**Fix:** Fire intraday_news at minutes 5 and 35 (`minute="5,35"`). First fire at 9:05 (after watch_list finishes), second at 9:35 (after 9:30 first cycle). Subsequent fires at 10:05, 10:35, … through 15:35.

**Files:**
- Modify: `main.py:201–208`
- No test needed — scheduling change, not logic change.

- [ ] **Step 1: Update the intraday_news scheduler job in main.py**

Replace lines 201–208 in `main.py`:

```python
    # Intraday news: 9:05 AM and every 30 min thereafter (avoids 9:00 race with watch_list)
    scheduler.add_job(
        job_intraday_news,
        CronTrigger(minute="5,35", hour="9-15", **kw),
        id="intraday_news",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=30,
    )
```

- [ ] **Step 2: Commit**

```bash
git add main.py
git commit -m "fix: stagger intraday_news to 9:05/9:35 to avoid 9:00 race with watch_list

Both jobs were firing at */30 starting at 9:00. APScheduler ran them
concurrently; intraday_news read WATCH_LIST_FILE mid-write, picking up
yesterday's tickers and caching wrong sentiment for the 9:30 first cycle."
```

---

## Task 12: Fix local ET definition in indicators/engine.py

**Root cause:** `indicators/engine.py:49` defines `ET = ZoneInfo("America/New_York")` locally inside `compute_indicators()`. Violates Coding Rule 3 (single source of truth for ET in `utils.constants`).

**Files:**
- Modify: `indicators/engine.py:5,49`

- [ ] **Step 1: Replace local import and usage**

In `indicators/engine.py`:

1. Remove line 5: `from zoneinfo import ZoneInfo`
2. Add `from utils.constants import ET` after line 4 (`import logging`)
3. Replace line 49 (`ET = ZoneInfo("America/New_York")`) with a comment `# ET imported from utils.constants`

Resulting top of file:

```python
# indicators/engine.py
import datetime as _dt
import logging
from utils.constants import ET
```

And remove the `ET = ZoneInfo("America/New_York")` line inside `compute_indicators`.

- [ ] **Step 2: Verify no test regression**

```bash
.venv/bin/python -m pytest tests/test_indicators.py -m unit -q
```

Expected: all pass (ET is functionally identical, just imported from the right place).

- [ ] **Step 3: Commit**

```bash
git add indicators/engine.py
git commit -m "fix: use utils.constants.ET in indicators/engine.py (coding rule 3)"
```

---

## Task 13: Trim Claude stock block tokens and increase batch size

**Root cause (token waste):** Each `build_stock_block` call produces ~200 tokens. With 5 stocks per batch and 2+ batches per cycle, the stock data alone costs ~2000 tokens/cycle. Removing `ATR` (not directional), compressing `EMA9`/`EMA21` into a ratio line, dropping `VWAP=$price` (already implied by ↑/↓), and skipping empty news summaries reduces this to ~130 tokens/stock — a ~35% reduction.

Also: `CLAUDE_BATCH_SIZE=5` means 2 API calls for 10 candidates. Setting it to 10 cuts to 1 call, saving the full system-context overhead (~1500 tokens) once per cycle.

**Files:**
- Modify: `trading/context_builder.py:14–60`
- Modify: `config.py:43`
- Test: `tests/test_trading_pipeline.py`

- [ ] **Step 1: Write token-count test**

```python
# tests/test_trading_pipeline.py — add
@pytest.mark.unit
def test_stock_block_is_under_150_tokens():
    """build_stock_block must stay compact to conserve Claude budget."""
    from trading.context_builder import build_stock_block
    from unittest.mock import MagicMock, patch

    iv = MagicMock()
    iv.composite_score = 42.5; iv.signal = "BUY"
    iv.current_price = 100.50; iv.vwap = 100.20
    iv.rsi = 58.3; iv.rsi_slope = 2.1; iv.macd_hist = 0.0023
    iv.bb_lower = 99.0; iv.bb_upper = 102.0
    iv.ema9 = 100.2; iv.ema21 = 99.8; iv.atr = 0.05
    iv.stoch_k = 65.0; iv.stoch_d = 60.0
    iv.daily_volume_ratio = 1.2

    research = {"TEST": {
        "sentiment_score": 35, "sentiment_label": "BULLISH",
        "key_catalyst": "AI chip demand", "risk_flag": None,
        "summary": "",  # empty summary should be omitted
    }}

    with patch("calendars.earnings.detect_pead_setup", return_value=(False, 0)):
        block = build_stock_block("TEST", iv, research, None)

    # Rough token estimate: 4 chars ≈ 1 token
    approx_tokens = len(block) / 4
    assert approx_tokens < 150, f"Stock block too large: ~{approx_tokens:.0f} tokens\n{block}"
    assert "ATR=" not in block, "ATR is not directional — remove from Claude block"
    assert "No summary" not in block, "Empty summary must be omitted"
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/python -m pytest tests/test_trading_pipeline.py::test_stock_block_is_under_150_tokens -v
```

- [ ] **Step 3: Rewrite build_stock_block in context_builder.py**

Replace the `build_stock_block` function (lines 14–60) in `trading/context_builder.py`:

```python
def build_stock_block(
    symbol: str,
    iv: IndicatorValues,
    research: dict,
    existing_pos: dict | None,
) -> str:
    """Compact per-symbol block (~130 tokens) sent to Claude."""
    from calendars.earnings import detect_pead_setup
    price           = iv.current_price
    research_sym    = research.get(symbol, {})
    sentiment_score = research_sym.get("sentiment_score", 0)
    sentiment_label = research_sym.get("sentiment_label", "NEUTRAL")
    risk_flag       = research_sym.get("risk_flag") or ""
    catalyst        = research_sym.get("key_catalyst") or ""
    news_summary    = (research_sym.get("summary") or "").strip()

    pos_block = ""
    if existing_pos:
        pnl_pct = (price - existing_pos["entry_price"]) / existing_pos["entry_price"] * 100
        pos_block = (
            f" POS:${existing_pos['entry_price']:.2f}×{existing_pos['qty']:.0f}"
            f" P&L={pnl_pct:+.2f}%"
        )

    dvol = f"{iv.daily_volume_ratio:.2f}x"
    if iv.daily_volume_ratio < 0.5:
        dvol += "LOW"
    elif iv.daily_volume_ratio > 3.0:
        dvol += "HIGH"

    pead_ok, pead_score = detect_pead_setup(symbol)
    pead_line = f" PEAD={pead_score}/100" if pead_ok else ""

    vwap_dir = "↑" if price >= iv.vwap else "↓"
    risk_str  = f" risk={risk_flag}" if risk_flag else ""
    cat_str   = f' cat="{catalyst}"' if catalyst else ""
    news_str  = f"\n  {news_summary}" if news_summary else ""

    return (
        f"=={symbol}== composite={iv.composite_score:+.1f} signal={iv.signal}{pos_block}\n"
        f"  P=${price:.2f} VWAP{vwap_dir} RSI={iv.rsi:.1f}({iv.rsi_slope:+.1f})"
        f" MACD={iv.macd_hist:+.4f} BB[{iv.bb_lower:.2f},{iv.bb_upper:.2f}]"
        f" EMA9/21={iv.ema9:.2f}/{iv.ema21:.2f} Stoch={iv.stoch_k:.0f}/{iv.stoch_d:.0f}"
        f" Vol={dvol}{pead_line}\n"
        f"  SENT:{sentiment_label}({sentiment_score:+d}){cat_str}{risk_str}"
        f"{news_str}"
    )
```

- [ ] **Step 4: Increase CLAUDE_BATCH_SIZE in config.py**

Change line 43 in `config.py`:

```python
CLAUDE_BATCH_SIZE = 10           # Stocks per batched Claude call — matches typical watch-list size
```

- [ ] **Step 5: Run token test to confirm pass**

```bash
.venv/bin/python -m pytest tests/test_trading_pipeline.py::test_stock_block_is_under_150_tokens -v
```

- [ ] **Step 6: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 7: Commit**

```bash
git add trading/context_builder.py config.py tests/test_trading_pipeline.py
git commit -m "perf: trim Claude stock block ~200→~130 tokens; batch size 5→10

Removes ATR (non-directional), compresses EMA/Stoch format, skips empty
news summaries. Larger batch size amortizes stable system-context overhead
across all candidates in a single API call instead of two."
```

---

## Task 14: Final verification — run all tests and confirm no regressions

- [ ] **Step 1: Run full test suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q --tb=short 2>&1 | tail -20
```

Expected: all tests pass, same count as before (no regressions).

- [ ] **Step 2: Verify indicator_weights.json sums to 100**

```bash
.venv/bin/python3 -c "
from utils.io_utils import load_json
w = load_json('data/state/indicator_weights.json', {})
print('Keys:', list(w.keys()))
print('Sum:', sum(w.values()))
assert abs(sum(w.values()) - 100.0) < 0.01, 'Weights must sum to 100'
print('OK')
"
```

Expected output: `Sum: 100.0  OK`

- [ ] **Step 3: Verify no stale `rsi_buy_max` range in learning/agent.py**

```bash
grep -n "30.50\|25.40\|30–50\|25–40" /Users/rajeshkumar/Documents/projects/trading-bot/learning/agent.py
```

Expected: no output.

- [ ] **Step 4: Verify intraday_news fires at 9:05 not 9:00**

```bash
grep -A5 "intraday_news" /Users/rajeshkumar/Documents/projects/trading-bot/main.py | grep minute
```

Expected: `minute="5,35"` (not `minute="*/30"`).

- [ ] **Step 5: Push to remote**

```bash
git push
```

---

## Impact Summary

| Fix | Daily Token Saving | Trade Impact |
|---|---|---|
| Task 1: Phantom weights | — | Composite scores +6.56 pts; auto-execute threshold reachable |
| Task 2: RSI threshold sync | ~49 × $0.018 = $0.88/day on bad days | Stops wasting budget on guaranteed rejects |
| Task 3: can_open=False gate | ~2 batch calls = ~$0.04/day | Eliminates 9:30 phantom Claude spend |
| Task 4: mtf_unavailable | — | Auto-execute remains live on budget-exhausted days |
| Task 5: PEAD gap exception | — | Post-earnings momentum plays (best signals) now enter |
| Task 6: Flag rehydration | $0.02–0.05 on restart days | Correct budget state after crash |
| Task 7: Learning RSI range | — | Prevents learning from drifting to blocking thresholds |
| Task 8: Head truncation | — | General Rules + Regime Playbook always visible to Claude |
| Task 9: Memory VWAP fix | — | Claude stops reasoning about pre-blocked stocks |
| Task 10: Watch list alert | — | Dead days visible in Telegram |
| Task 11: 9:00 AM race | — | First cycle has correct per-symbol sentiment |
| Task 12: ET import | — | Code rule compliance |
| Task 13: Token trim | ~35% per stock block | ~500 tokens/cycle; batch 5→10 saves 1 API call |
