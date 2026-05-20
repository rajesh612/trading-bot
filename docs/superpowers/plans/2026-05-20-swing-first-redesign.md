# Swing-First Redesign + Parameter Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current day-trade-default / 7-gate-AND exit model with asymmetric entry (all signals, strict) / exit (thesis-break vetoes, patient), raise composite floors, add price cap, add market-cap screener filter.

**Architecture:** Each task is self-contained and testable. Config first (foundation), then entry gates (pure signal functions), then CycleSnapshot plumbing, then executor fields, then EOD hold logic (largest change), then screener. All gate logic stays in `signals/`; orchestration stays in `trading/`. No circular imports introduced.

**Tech Stack:** Python, pytest, yfinance (market cap), existing `config.py` / `signals/entry_gates.py` / `trading/eod_holds.py` / `agents/volatility_screener.py`.

---

## File Map

| File | What changes |
|---|---|
| `config.py` | 7 updated constants + 7 new constants |
| `signals/entry_gates.py` | `effective_composite_gate()`: BULL_TREND branch; `check_all_gates()`: 4 new params + 4 new gates |
| `trading/cycle_context.py` | Add `major_event_today: bool` field to `CycleSnapshot`; populate in `build_cycle_snapshot()` |
| `trading/executor.py` | Add `entry_sector_sentiment` + `entry_news_sentiment` to position record; pass 4 new params to `check_all_gates()` |
| `trading/eod_holds.py` | New `qualifies_for_eod_hold()` signature (8 params) + regime-aware veto matrix; update `evaluate_eod_holds()` call site |
| `agents/volatility_screener.py` | Add `_fetch_market_cap()` helper + pre-filter step before scoring |
| `tests/test_entry_gates.py` | Tests for BULL_TREND composite, price cap, sector flag, news flag, event-day gate |
| `tests/test_foundation.py` | Tests for all 8 regime paths through new `qualifies_for_eod_hold()` |
| `tests/test_signals.py` | Update any tests that pass composite thresholds by value |
| `tests/test_core.py` | Test market-cap filter in screener |
| `tests/test_pipeline.py` | Test `major_event_today` in `CycleSnapshot` |

---

## Task 1: Config constants

**Files:**
- Modify: `config.py`

- [ ] **Step 1: Update and add constants in `config.py`**

Find the `# ── Entry gates` and `# ── Composite minimums` sections and apply these changes:

```python
# ── Composite minimums (updated) ─────────────────────────────────────────────
COMPOSITE_MIN_NORMAL   = 50   # raised from 30 — live data: 56% win rate at ≥50
COMPOSITE_MIN_HIGH_VOL = 55   # raised from 35
COMPOSITE_MIN_BULL_TREND = 45 # new — BULL_TREND slightly relaxed; wider TP justifies it
CHOPPY_COMPOSITE_GATE  = 55   # raised from 35 — CHOPPY is worst win-rate regime

AUTO_EXECUTE_COMPOSITE_MIN   = 70   # raised from 65
ENTRY_LATE_COMPOSITE_MIN     = 55   # raised from 50
BACKTEST_SWING_COMPOSITE_MIN = 50   # raised from 40 — match live floor

# ── Swing positions (updated) ─────────────────────────────────────────────────
SWING_MAX_POSITIONS = 6   # raised from 2 — was always full, blocking promotion

# ── Swing PTJ rule thresholds by regime (new) ────────────────────────────────
SWING_MIN_PNL_PCT_BULL         = -3.0   # BULL_TREND: wider (SL is 7%)
SWING_MIN_PNL_PCT_CAPITULATION = -5.0   # CAPITULATION: widest (SL is 12%)
SWING_MIN_PNL_PCT_CRASH        = -3.0   # CRASH_RECOVERY: wider (SL is 10%)

# ── Entry screening (new) ─────────────────────────────────────────────────────
MAX_ENTRY_PRICE      = 400   # hard block: stocks > $400 excluded (>$500 lose -$55/trade avg)
SECTOR_ENTRY_MIN_SCORE = 0   # soft flag: sector sentiment below this → flag for Claude
SWING_NEWS_MIN_SCORE   = 50  # soft flag: news sentiment below this for swing candidates
MIN_MARKET_CAP_B       = 25  # screener pre-filter: drop symbols below $25B market cap
```

- [ ] **Step 2: Verify no tests break from constant changes**

```bash
.venv/bin/python -m pytest tests/ -m unit -q --tb=short 2>&1 | tail -20
```

Expected: some tests may fail if they hardcode old thresholds (30, 35, 65). Note failing tests — they are fixed in Task 3.

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "config: raise composite floors, add swing PTJ thresholds and entry screening constants"
```

---

## Task 2: BULL_TREND branch in effective_composite_gate

**Files:**
- Modify: `signals/entry_gates.py:34-50`
- Test: `tests/test_entry_gates.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_entry_gates.py` inside the existing test class:

```python
@pytest.mark.unit
def test_effective_gate_bull_trend_returns_45(self, monkeypatch):
    """BULL_TREND regime uses COMPOSITE_MIN_BULL_TREND (45), not NORMAL (50)."""
    monkeypatch.setattr(config, "COMPOSITE_MIN_BULL_TREND", 45)
    monkeypatch.setattr(config, "COMPOSITE_MIN_NORMAL", 50)
    from signals.entry_gates import effective_composite_gate
    iv = _make_iv(composite_score=47.0, squeeze_setup=False, bot_herding_score=0)
    result = effective_composite_gate(iv, "BULL_TREND")
    assert result == 45

@pytest.mark.unit
def test_effective_gate_bull_trend_not_normal(self, monkeypatch):
    """BULL_TREND must not fall through to NORMAL threshold."""
    monkeypatch.setattr(config, "COMPOSITE_MIN_BULL_TREND", 45)
    monkeypatch.setattr(config, "COMPOSITE_MIN_NORMAL", 50)
    from signals.entry_gates import effective_composite_gate
    iv = _make_iv(composite_score=47.0, squeeze_setup=False, bot_herding_score=0)
    normal_gate = effective_composite_gate(iv, "NORMAL")
    bull_gate   = effective_composite_gate(iv, "BULL_TREND")
    assert bull_gate < normal_gate
```

Where `_make_iv` is the existing helper in that test file. If it doesn't exist yet, add:

```python
def _make_iv(**overrides):
    from indicators.values import IndicatorValues
    defaults = dict(
        symbol="TEST", timestamp="2026-01-01 10:00:00-05:00",
        current_price=100.0, vwap=100.0, vwap_std1_upper=101.0,
        vwap_std1_lower=99.0, vwap_std2_upper=102.0, vwap_std2_lower=98.0,
        rsi=50.0, macd=0.1, macd_signal=0.05, macd_hist=0.05,
        bb_upper=105.0, bb_mid=100.0, bb_lower=95.0, ema9=100.0, ema21=99.0,
        atr=1.0, volume_ratio=1.2, stoch_k=50.0, stoch_d=50.0,
        obv=1000000.0, vwap_slope=0.1, rsi_slope=0.0,
        daily_volume_ratio=1.0, daily_volume_trend=1.0, daily_volume_mean=1000000.0,
        composite_score=50.0, signal="BUY", confidence=60.0,
        parabolic=False, volume_exhaustion=False, obv_divergence="none",
        momentum_exhaustion=False, bot_herding_score=0, squeeze_setup=False,
        squeeze_confidence=0, stop_hunt_zone=False,
        prev_close=99.0, or_high=101.0, or_low=99.0, or_position="outside",
        relative_volume=1.0,
    )
    defaults.update(overrides)
    return IndicatorValues(**defaults)
```

- [ ] **Step 2: Run test — expect FAIL**

```bash
.venv/bin/python -m pytest tests/test_entry_gates.py::TestEffectiveCompositeGate -v 2>&1 | tail -15
```

Expected: `AttributeError: module 'config' has no attribute 'COMPOSITE_MIN_BULL_TREND'` — confirms test is wired correctly (constant added in Task 1 so this should already pass after Task 1; if Task 1 is done, run after adding the branch).

- [ ] **Step 3: Add BULL_TREND branch to `effective_composite_gate()`**

In `signals/entry_gates.py`, update `effective_composite_gate()`:

```python
def effective_composite_gate(iv: IndicatorValues, regime: str, params: dict | None = None) -> int:
    """Single source of truth for composite entry threshold by regime and signal context.

    Priority: HIGH_VOL safety → squeeze setup → bot herding → CHOPPY → BULL_TREND → NORMAL.
    """
    if regime in regimes.HIGH_VOL_GATES:
        return config.COMPOSITE_MIN_HIGH_VOL
    if iv.squeeze_setup:
        return config.SQUEEZE_COMPOSITE_MIN
    if iv.bot_herding_score >= config.BOT_HERD_ALERT_SCORE:
        return config.HERD_ENTRY_GATE
    if regime == "CHOPPY":
        return config.CHOPPY_COMPOSITE_GATE
    if regime == "BULL_TREND":
        return config.COMPOSITE_MIN_BULL_TREND
    return (params or {}).get("composite_min_normal", config.COMPOSITE_MIN_NORMAL)
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
.venv/bin/python -m pytest tests/test_entry_gates.py -v -k "bull_trend" 2>&1 | tail -10
```

- [ ] **Step 5: Fix any tests broken by composite floor raises**

Run full suite and fix tests that hardcode old thresholds using `monkeypatch.setattr`:

```bash
.venv/bin/python -m pytest tests/ -m unit -q --tb=short 2>&1 | grep FAIL
```

For any failing test that checks `composite_score < 30` or similar, add `monkeypatch.setattr(config, "COMPOSITE_MIN_NORMAL", 30)` to preserve old behaviour in that specific test.

- [ ] **Step 6: Commit**

```bash
git add signals/entry_gates.py tests/test_entry_gates.py tests/test_signals.py
git commit -m "feat: add BULL_TREND composite gate (45) to effective_composite_gate"
```

---

## Task 3: Price cap hard gate in check_all_gates

**Files:**
- Modify: `signals/entry_gates.py:53+` (`check_all_gates` function body)
- Test: `tests/test_entry_gates.py`

- [ ] **Step 1: Write failing tests**

```python
@pytest.mark.unit
def test_price_cap_blocks_expensive_stock(self, monkeypatch):
    """Stock price above MAX_ENTRY_PRICE is hard-blocked."""
    monkeypatch.setattr(config, "MAX_ENTRY_PRICE", 400)
    from signals.entry_gates import check_all_gates
    iv = _make_iv(current_price=450.0, composite_score=60.0)
    result = check_all_gates(
        symbol="LITE", iv=iv, open_positions={}, daily_closed=set(),
        bars_15min=None, now=_market_open(), entries_this_cycle=0,
    )
    assert not result.passed
    assert "MAX_ENTRY_PRICE" in result.reason

@pytest.mark.unit
def test_price_cap_allows_stock_at_limit(self, monkeypatch):
    """Stock price exactly at MAX_ENTRY_PRICE passes the gate."""
    monkeypatch.setattr(config, "MAX_ENTRY_PRICE", 400)
    from signals.entry_gates import check_all_gates
    iv = _make_iv(current_price=400.0, composite_score=60.0)
    result = check_all_gates(
        symbol="MRVL", iv=iv, open_positions={}, daily_closed=set(),
        bars_15min=None, now=_market_open(), entries_this_cycle=0,
    )
    # Gate should not fail on price (may fail on other gates — we only check price reason)
    assert "MAX_ENTRY_PRICE" not in result.reason

@pytest.mark.unit
def test_price_cap_zero_disables_gate(self, monkeypatch):
    """MAX_ENTRY_PRICE = 0 disables the price cap entirely."""
    monkeypatch.setattr(config, "MAX_ENTRY_PRICE", 0)
    from signals.entry_gates import check_all_gates
    iv = _make_iv(current_price=2000.0, composite_score=60.0)
    result = check_all_gates(
        symbol="BRK", iv=iv, open_positions={}, daily_closed=set(),
        bars_15min=None, now=_market_open(), entries_this_cycle=0,
    )
    assert "MAX_ENTRY_PRICE" not in result.reason
```

Add `_market_open()` helper if not already present:

```python
def _market_open():
    from datetime import datetime
    from utils.constants import ET
    return datetime(2026, 5, 20, 10, 30, tzinfo=ET)
```

- [ ] **Step 2: Run test — expect FAIL**

```bash
.venv/bin/python -m pytest tests/test_entry_gates.py -v -k "price_cap" 2>&1 | tail -10
```

- [ ] **Step 3: Add price cap gate to `check_all_gates()`**

In `signals/entry_gates.py`, insert after gate 0 (CRISIS check) and before gate 1 (time gate):

```python
    # 0b. Price cap — hard block expensive stocks (>$500 avg -$55/trade at 28% win rate)
    if config.MAX_ENTRY_PRICE and iv.current_price > config.MAX_ENTRY_PRICE:
        return GateResult(False, f"price ${iv.current_price:.0f} > MAX_ENTRY_PRICE ${config.MAX_ENTRY_PRICE:.0f}")
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
.venv/bin/python -m pytest tests/test_entry_gates.py -v -k "price_cap" 2>&1 | tail -10
```

- [ ] **Step 5: Commit**

```bash
git add signals/entry_gates.py tests/test_entry_gates.py
git commit -m "feat: add MAX_ENTRY_PRICE hard gate to check_all_gates"
```

---

## Task 4: Sector sentiment soft flag + news quality soft flag

**Files:**
- Modify: `signals/entry_gates.py` — `check_all_gates()` signature + body
- Modify: `trading/executor.py` — pass new params to `check_all_gates()`
- Test: `tests/test_entry_gates.py`

- [ ] **Step 1: Write failing tests**

```python
@pytest.mark.unit
def test_sector_soft_flag_when_negative(self, monkeypatch):
    """Sector sentiment below SECTOR_ENTRY_MIN_SCORE adds a soft flag, not a hard block."""
    monkeypatch.setattr(config, "SECTOR_ENTRY_MIN_SCORE", 0)
    monkeypatch.setattr(config, "MAX_ENTRY_PRICE", 0)
    from signals.entry_gates import check_all_gates
    iv = _make_iv(current_price=100.0, composite_score=60.0)
    result = check_all_gates(
        symbol="NVDA", iv=iv, open_positions={}, daily_closed=set(),
        bars_15min=None, now=_market_open(), entries_this_cycle=0,
        sector_sentiment_score=-15.0,
    )
    assert result.passed
    assert any("sector_sentiment" in f for f in result.flags)

@pytest.mark.unit
def test_sector_soft_flag_absent_when_positive(self, monkeypatch):
    """No sector flag when sector sentiment is non-negative."""
    monkeypatch.setattr(config, "SECTOR_ENTRY_MIN_SCORE", 0)
    monkeypatch.setattr(config, "MAX_ENTRY_PRICE", 0)
    from signals.entry_gates import check_all_gates
    iv = _make_iv(current_price=100.0, composite_score=60.0)
    result = check_all_gates(
        symbol="NVDA", iv=iv, open_positions={}, daily_closed=set(),
        bars_15min=None, now=_market_open(), entries_this_cycle=0,
        sector_sentiment_score=20.0,
    )
    assert not any("sector_sentiment" in f for f in result.flags)

@pytest.mark.unit
def test_news_quality_soft_flag_for_swing_candidate(self, monkeypatch):
    """News sentiment below SWING_NEWS_MIN_SCORE adds soft flag when swing_eligible=True."""
    monkeypatch.setattr(config, "SWING_NEWS_MIN_SCORE", 50)
    monkeypatch.setattr(config, "MAX_ENTRY_PRICE", 0)
    from signals.entry_gates import check_all_gates
    iv = _make_iv(current_price=100.0, composite_score=60.0)
    result = check_all_gates(
        symbol="NVDA", iv=iv, open_positions={}, daily_closed=set(),
        bars_15min=None, now=_market_open(), entries_this_cycle=0,
        swing_eligible=True, sentiment_score=30.0,
    )
    assert result.passed
    assert any("swing_news" in f for f in result.flags)

@pytest.mark.unit
def test_news_quality_flag_skipped_for_day_trade(self, monkeypatch):
    """No news flag when swing_eligible=False even if sentiment is low."""
    monkeypatch.setattr(config, "SWING_NEWS_MIN_SCORE", 50)
    monkeypatch.setattr(config, "MAX_ENTRY_PRICE", 0)
    from signals.entry_gates import check_all_gates
    iv = _make_iv(current_price=100.0, composite_score=60.0)
    result = check_all_gates(
        symbol="NVDA", iv=iv, open_positions={}, daily_closed=set(),
        bars_15min=None, now=_market_open(), entries_this_cycle=0,
        swing_eligible=False, sentiment_score=10.0,
    )
    assert not any("swing_news" in f for f in result.flags)
```

- [ ] **Step 2: Run — expect FAIL**

```bash
.venv/bin/python -m pytest tests/test_entry_gates.py -v -k "sector_soft or news_quality" 2>&1 | tail -15
```

- [ ] **Step 3: Add params to `check_all_gates()` signature**

In `signals/entry_gates.py`, extend the signature:

```python
def check_all_gates(
    symbol: str,
    iv: IndicatorValues,
    open_positions: dict,
    daily_closed: set[str],
    bars_15min: pd.DataFrame | None,
    now: datetime,
    entries_this_cycle: int,
    spy_ret5d: float = 0.0,
    symbol_ret5d: float = 0.0,
    mode: str = "DAY_TRADE",
    rsi_max: float = config.RSI_BUY_MAX,
    sector_today: dict | None = None,
    symbol_sector_etf: str | None = None,
    advance_decline_ratio: float = 1.0,
    sl_blacklisted: bool = False,
    sector_sentiment_score: float = 0.0,   # new: NLP sector sentiment from research __meta__
    sentiment_score: float = 0.0,          # new: symbol-level news sentiment score
    swing_eligible: bool = False,          # new: True when regime is in SWING_ELIGIBLE
    major_event_today: bool = False,       # new: True if FOMC/CPI/NFP scheduled today
) -> GateResult:
```

- [ ] **Step 4: Add soft gates in `check_all_gates()` body**

After gate 19 (breadth) and before the final `return GateResult(True, ...)`, add:

```python
    # 20. Sector sentiment soft flag — sector going negative signals headwind
    if sector_sentiment_score < config.SECTOR_ENTRY_MIN_SCORE:
        flags.append(f"sector_sentiment({sector_sentiment_score:.0f}<{config.SECTOR_ENTRY_MIN_SCORE})")

    # 21. News quality soft flag — swing candidates need a catalyst
    if swing_eligible and sentiment_score < config.SWING_NEWS_MIN_SCORE:
        flags.append(f"swing_news_weak({sentiment_score:.0f}<{config.SWING_NEWS_MIN_SCORE})")
```

- [ ] **Step 5: Update `executor.py` call site**

In `trading/executor.py`, find the `check_all_gates(...)` call (around line 101) and add the new kwargs:

```python
    gate = check_all_gates(
        symbol=symbol, iv=iv, open_positions=open_positions,
        daily_closed=fresh_daily_closed, bars_15min=bars_15min, now=now,
        entries_this_cycle=entries_this_cycle,
        spy_ret5d=spy_ret5d, symbol_ret5d=symbol_ret5d, mode=mode,
        rsi_max=rsi_max,
        sector_today=sector_today,
        symbol_sector_etf=symbol_sector_etf,
        advance_decline_ratio=advance_decline_ratio,
        sl_blacklisted=sl_blacklisted,
        sector_sentiment_score=_get_sector_sentiment(snap, symbol),   # new
        sentiment_score=symbol_sentiment,                             # new (already computed above)
        swing_eligible=(regime in regimes.SWING_ELIGIBLE),            # new
        major_event_today=snap.major_event_today,                     # new — added in Task 6
    )
```

Add helper `_get_sector_sentiment` near the top of `trading/executor.py`:

```python
def _get_sector_sentiment(snap, symbol: str) -> float:
    """Return sector-level NLP sentiment score for symbol's sector."""
    try:
        import sectors as _sectors
        sector = _sectors.get_sector(symbol)
        return float(snap.research_meta.get("sector_sentiment", {}).get(sector, 0))
    except Exception:
        return 0.0
```

Note: `snap.major_event_today` is added in Task 6 — use `getattr(snap, "major_event_today", False)` until Task 6 is complete.

- [ ] **Step 6: Run tests — expect PASS**

```bash
.venv/bin/python -m pytest tests/test_entry_gates.py -v -k "sector_soft or news_quality" 2>&1 | tail -10
.venv/bin/python -m pytest tests/ -m unit -q --tb=short 2>&1 | tail -5
```

- [ ] **Step 7: Commit**

```bash
git add signals/entry_gates.py trading/executor.py tests/test_entry_gates.py
git commit -m "feat: add sector sentiment soft flag and swing news quality soft flag to entry gates"
```

---

## Task 5: Event-day hard gate for swing entries in NORMAL

**Files:**
- Modify: `signals/entry_gates.py` — add gate 22
- Test: `tests/test_entry_gates.py`

Note: `major_event_today` is already in the signature from Task 4. This task adds the gate logic and tests.

- [ ] **Step 1: Write failing tests**

```python
@pytest.mark.unit
def test_event_day_blocks_swing_entry_in_normal(self, monkeypatch):
    """Major event today blocks new swing-eligible entries in NORMAL regime."""
    monkeypatch.setattr(config, "MAX_ENTRY_PRICE", 0)
    from signals.entry_gates import check_all_gates
    iv = _make_iv(current_price=100.0, composite_score=60.0)
    result = check_all_gates(
        symbol="NVDA", iv=iv, open_positions={}, daily_closed=set(),
        bars_15min=None, now=_market_open(), entries_this_cycle=0,
        swing_eligible=True, major_event_today=True, mode="DAY_TRADE",
    )
    assert not result.passed
    assert "major economic event" in result.reason.lower()

@pytest.mark.unit
def test_event_day_allows_day_trade_in_choppy(self, monkeypatch):
    """Major event today does NOT block day-trade entries (CHOPPY regime, swing_eligible=False)."""
    monkeypatch.setattr(config, "MAX_ENTRY_PRICE", 0)
    from signals.entry_gates import check_all_gates
    iv = _make_iv(current_price=100.0, composite_score=60.0)
    result = check_all_gates(
        symbol="NVDA", iv=iv, open_positions={}, daily_closed=set(),
        bars_15min=None, now=_market_open(), entries_this_cycle=0,
        swing_eligible=False, major_event_today=True, mode="DAY_TRADE",
    )
    assert "major economic event" not in result.reason

@pytest.mark.unit
def test_event_day_no_block_when_no_event(self, monkeypatch):
    """No block when major_event_today=False."""
    monkeypatch.setattr(config, "MAX_ENTRY_PRICE", 0)
    from signals.entry_gates import check_all_gates
    iv = _make_iv(current_price=100.0, composite_score=60.0)
    result = check_all_gates(
        symbol="NVDA", iv=iv, open_positions={}, daily_closed=set(),
        bars_15min=None, now=_market_open(), entries_this_cycle=0,
        swing_eligible=True, major_event_today=False,
    )
    assert "major economic event" not in result.reason
```

- [ ] **Step 2: Run — expect FAIL**

```bash
.venv/bin/python -m pytest tests/test_entry_gates.py -v -k "event_day" 2>&1 | tail -10
```

- [ ] **Step 3: Add gate to `check_all_gates()` body**

After gate 0b (price cap) and before gate 1 (time gate):

```python
    # 0c. Major economic event today — no new swing entries (overnight thesis invalidation risk)
    if major_event_today and swing_eligible:
        return GateResult(False, "major economic event today — no new swing-eligible entries")
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
.venv/bin/python -m pytest tests/test_entry_gates.py -v -k "event_day" 2>&1 | tail -10
```

- [ ] **Step 5: Commit**

```bash
git add signals/entry_gates.py tests/test_entry_gates.py
git commit -m "feat: add major-event-day hard gate for swing entries"
```

---

## Task 6: CycleSnapshot — add major_event_today

**Files:**
- Modify: `trading/cycle_context.py` — add field + populate in `build_cycle_snapshot()`
- Test: `tests/test_pipeline.py`

- [ ] **Step 1: Write failing test**

```python
@pytest.mark.unit
def test_cycle_snapshot_has_major_event_today_field(self, monkeypatch, tmp_path):
    """CycleSnapshot.major_event_today is True when a high-impact event is scheduled today."""
    import config
    monkeypatch.setattr(config, "RESEARCH_REPORT_FILE", tmp_path / "rr.json")
    monkeypatch.setattr(config, "INDICATOR_WEIGHTS_FILE", tmp_path / "iw.json")
    monkeypatch.setattr(config, "STRATEGY_MEMORY_FILE", tmp_path / "sm.md")
    monkeypatch.setattr(config, "FRED_CACHE_FILE", tmp_path / "fred.json")
    monkeypatch.setattr(config, "ECONOMIC_CALENDAR_FILE", tmp_path / "ec.json")

    from utils.io_utils import save_json
    from datetime import datetime
    from utils.constants import ET
    today = datetime.now(ET).strftime("%Y-%m-%d")
    save_json(tmp_path / "ec.json", {
        "date": today,
        "events": [{"name": "FOMC Rate Decision", "time": "14:00"}],
    })

    monkeypatch.setattr("macro.regime.load_macro_regime", lambda: {"regime": "NORMAL", "macro_health_score": 0})
    monkeypatch.setattr("macro.regime.load_trading_params", lambda: {})
    monkeypatch.setattr("macro.intraday_context.get_intraday_context", lambda dt: None)
    monkeypatch.setattr("macro.breadth_signals.get_advance_decline_ratio", lambda: 1.0)
    monkeypatch.setattr("research.intraday.load_intraday_news", lambda: {})

    from trading.cycle_context import build_cycle_snapshot
    snap = build_cycle_snapshot(portfolio_gain_pct=0.0)
    assert snap.major_event_today is True

@pytest.mark.unit
def test_cycle_snapshot_major_event_today_false_when_no_event(self, monkeypatch, tmp_path):
    """CycleSnapshot.major_event_today is False when no events scheduled."""
    import config
    monkeypatch.setattr(config, "RESEARCH_REPORT_FILE", tmp_path / "rr.json")
    monkeypatch.setattr(config, "INDICATOR_WEIGHTS_FILE", tmp_path / "iw.json")
    monkeypatch.setattr(config, "STRATEGY_MEMORY_FILE", tmp_path / "sm.md")
    monkeypatch.setattr(config, "FRED_CACHE_FILE", tmp_path / "fred.json")
    monkeypatch.setattr(config, "ECONOMIC_CALENDAR_FILE", tmp_path / "ec.json")

    from datetime import datetime
    from utils.constants import ET
    today = datetime.now(ET).strftime("%Y-%m-%d")
    from utils.io_utils import save_json
    save_json(tmp_path / "ec.json", {"date": today, "events": []})

    monkeypatch.setattr("macro.regime.load_macro_regime", lambda: {"regime": "NORMAL", "macro_health_score": 0})
    monkeypatch.setattr("macro.regime.load_trading_params", lambda: {})
    monkeypatch.setattr("macro.intraday_context.get_intraday_context", lambda dt: None)
    monkeypatch.setattr("macro.breadth_signals.get_advance_decline_ratio", lambda: 1.0)
    monkeypatch.setattr("research.intraday.load_intraday_news", lambda: {})

    from trading.cycle_context import build_cycle_snapshot
    snap = build_cycle_snapshot(portfolio_gain_pct=0.0)
    assert snap.major_event_today is False
```

- [ ] **Step 2: Run — expect FAIL**

```bash
.venv/bin/python -m pytest tests/test_pipeline.py -v -k "major_event_today" 2>&1 | tail -10
```

- [ ] **Step 3: Add field to `CycleSnapshot` and populate it**

In `trading/cycle_context.py`, add field to the dataclass:

```python
@dataclass
class CycleSnapshot:
    regime_info: dict
    regime: str
    fred_cache: dict
    research: dict
    research_meta: dict
    strategy_memory: str
    weights: dict
    params: dict
    portfolio_gain_pct: float
    intraday_news: dict = field(default_factory=dict)
    sector_today: dict = field(default_factory=dict)
    advance_decline_ratio: float = 1.0
    major_event_today: bool = False   # new: True if FOMC/CPI/NFP scheduled today
```

In `build_cycle_snapshot()`, compute `major_event_today` before the return:

```python
    major_event_today = False
    try:
        from macro.economic_calendar import get_todays_events
        high_impact = {"FOMC", "NFP", "CPI", "PCE", "JOLTS"}
        for evt in get_todays_events():
            if any(tag in evt.get("name", "").upper() for tag in high_impact):
                major_event_today = True
                break
    except Exception:
        pass

    return CycleSnapshot(
        ...
        major_event_today=major_event_today,
    )
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
.venv/bin/python -m pytest tests/test_pipeline.py -v -k "major_event_today" 2>&1 | tail -10
.venv/bin/python -m pytest tests/ -m unit -q --tb=short 2>&1 | tail -5
```

- [ ] **Step 5: Commit**

```bash
git add trading/cycle_context.py tests/test_pipeline.py
git commit -m "feat: add major_event_today to CycleSnapshot"
```

---

## Task 7: Store entry_sector_sentiment in position record

**Files:**
- Modify: `trading/executor.py` — add two fields to `position_data`
- Test: `tests/test_trading_budget.py` (or nearest executor test file)

- [ ] **Step 1: Write failing test**

```python
@pytest.mark.unit
def test_execute_buy_stores_entry_sector_sentiment(self, monkeypatch, tmp_path):
    """execute_buy() persists entry_sector_sentiment in the position record."""
    # ... standard executor test setup (see existing tests for pattern) ...
    # After a successful buy, load open_positions and assert field exists
    from utils.io_utils import load_json
    positions = load_json(tmp_path / "open_positions.json", {})
    assert "NVDA" in positions
    assert "entry_sector_sentiment" in positions["NVDA"]
    assert "entry_news_sentiment" in positions["NVDA"]
```

Follow the existing executor test pattern in `tests/test_trading_budget.py` for mocking AlpacaClient, research, and file paths.

- [ ] **Step 2: Run — expect FAIL**

```bash
.venv/bin/python -m pytest tests/test_trading_budget.py -v -k "entry_sector_sentiment" 2>&1 | tail -10
```

- [ ] **Step 3: Add fields to `position_data` in `executor.py`**

In `trading/executor.py`, in `execute_buy()`, add to `position_data`:

```python
    position_data = {
        "entry_price": iv.current_price, "qty": qty,
        "stop_loss_pct": sl_pct, "take_profit_pct": tp_pct,
        "stop_loss_price": corrected["stop_loss_price"] if corrected else order["stop_loss_price"],
        "take_profit_price": corrected["take_profit_price"] if corrected else order["take_profit_price"],
        "fill_price": corrected["fill_price"] if corrected else iv.current_price,
        "entry_time": now.isoformat(), "entry_composite_score": iv.composite_score,
        "entry_indicators": asdict(iv), "entry_sentiment_score": research.get(symbol, {}).get("sentiment_score"),
        "entry_sector_sentiment": _get_sector_sentiment(snap, symbol),   # new
        "entry_news_sentiment": float(symbol_sentiment) if isinstance(symbol_sentiment, (int, float)) else 0.0,  # new
        "claude_reasoning": decision.get("reasoning", ""), "indicator_signals": decision.get("indicator_signals", {}),
        "order_id": order.get("id"), "mode_at_entry": mode, "regime_at_entry": regime,
        "atr_pct": round((iv.atr / iv.current_price) * 100, 2) if iv.atr > 0 and iv.current_price > 0 else None,
    }
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
.venv/bin/python -m pytest tests/ -m unit -q --tb=short 2>&1 | tail -5
```

- [ ] **Step 5: Commit**

```bash
git add trading/executor.py tests/test_trading_budget.py
git commit -m "feat: store entry_sector_sentiment and entry_news_sentiment in position record"
```

---

## Task 8: EOD holds — regime-aware thesis-break veto matrix

This is the largest change. Replace the 7-gate AND with a regime-specific fail-fast veto.

**Files:**
- Modify: `trading/eod_holds.py` — `qualifies_for_eod_hold()` + `evaluate_eod_holds()`
- Test: `tests/test_foundation.py`

- [ ] **Step 1: Write failing tests — one per regime path**

Add a new test class `TestQualifiesForEodHoldRegimeMatrix` in `tests/test_foundation.py`:

```python
class TestQualifiesForEodHoldRegimeMatrix:
    """Tests for the regime-aware swing-hold veto matrix."""

    def _call(self, regime="NORMAL", earnings_tomorrow=False, current_pnl_pct=1.0,
              entry_sector_sentiment=20.0, current_sector_sentiment=15.0,
              consecutive_red_days=0, vix_trend="stable"):
        from trading.eod_holds import qualifies_for_eod_hold
        return qualifies_for_eod_hold(
            symbol="TEST", regime=regime, earnings_tomorrow=earnings_tomorrow,
            current_pnl_pct=current_pnl_pct,
            entry_sector_sentiment=entry_sector_sentiment,
            current_sector_sentiment=current_sector_sentiment,
            consecutive_red_days=consecutive_red_days,
            vix_trend=vix_trend,
        )

    @pytest.mark.unit
    def test_crisis_always_vetoes(self):
        ok, reason = self._call(regime="CRISIS", current_pnl_pct=5.0)
        assert not ok
        assert "CRISIS" in reason

    @pytest.mark.unit
    def test_earnings_tomorrow_vetoes_all_regimes(self):
        for regime in ("NORMAL", "BULL_TREND", "OPPORTUNITY", "CRASH_RECOVERY"):
            ok, reason = self._call(regime=regime, earnings_tomorrow=True)
            assert not ok, f"{regime} should veto on earnings tomorrow"
            assert "earnings" in reason.lower()

    @pytest.mark.unit
    def test_choppy_blocks_new_promotions(self):
        ok, reason = self._call(regime="CHOPPY", current_pnl_pct=2.0)
        assert not ok
        assert "CHOPPY" in reason

    @pytest.mark.unit
    def test_defensive_blocks_new_promotions(self):
        ok, reason = self._call(regime="DEFENSIVE", current_pnl_pct=2.0)
        assert not ok
        assert "DEFENSIVE" in reason

    @pytest.mark.unit
    def test_normal_ptj_veto_at_minus_2(self, monkeypatch):
        monkeypatch.setattr(config, "SWING_MIN_PNL_PCT", -2.0)
        ok, _ = self._call(regime="NORMAL", current_pnl_pct=-2.1)
        assert not ok

    @pytest.mark.unit
    def test_normal_passes_above_ptj(self, monkeypatch):
        monkeypatch.setattr(config, "SWING_MIN_PNL_PCT", -2.0)
        ok, _ = self._call(regime="NORMAL", current_pnl_pct=-1.9)
        assert ok

    @pytest.mark.unit
    def test_normal_sector_reversal_vetoes(self):
        """Sector reversed: current < -20 AND entry was > +10."""
        ok, reason = self._call(
            regime="NORMAL", entry_sector_sentiment=25.0, current_sector_sentiment=-25.0
        )
        assert not ok
        assert "sector" in reason.lower()

    @pytest.mark.unit
    def test_normal_sector_soft_drop_does_not_veto(self):
        """Sector dropped but not reversed (current=+5, entry=+15) — no veto."""
        ok, _ = self._call(
            regime="NORMAL", entry_sector_sentiment=15.0, current_sector_sentiment=5.0
        )
        assert ok

    @pytest.mark.unit
    def test_bull_trend_wider_ptj_threshold(self, monkeypatch):
        monkeypatch.setattr(config, "SWING_MIN_PNL_PCT_BULL", -3.0)
        ok_at_minus_2_9, _ = self._call(regime="BULL_TREND", current_pnl_pct=-2.9)
        ok_at_minus_3_1, _ = self._call(regime="BULL_TREND", current_pnl_pct=-3.1)
        assert ok_at_minus_2_9
        assert not ok_at_minus_3_1

    @pytest.mark.unit
    def test_opportunity_vetoes_when_sector_goes_negative(self):
        ok, reason = self._call(
            regime="OPPORTUNITY", entry_sector_sentiment=15.0, current_sector_sentiment=-5.0
        )
        assert not ok
        assert "sector" in reason.lower()

    @pytest.mark.unit
    def test_opportunity_passes_when_sector_still_positive(self):
        ok, _ = self._call(
            regime="OPPORTUNITY", entry_sector_sentiment=15.0, current_sector_sentiment=5.0
        )
        assert ok

    @pytest.mark.unit
    def test_capitulation_wider_ptj_threshold(self, monkeypatch):
        monkeypatch.setattr(config, "SWING_MIN_PNL_PCT_CAPITULATION", -5.0)
        ok_at_minus_4_9, _ = self._call(regime="CAPITULATION", current_pnl_pct=-4.9)
        ok_at_minus_5_1, _ = self._call(regime="CAPITULATION", current_pnl_pct=-5.1)
        assert ok_at_minus_4_9
        assert not ok_at_minus_5_1

    @pytest.mark.unit
    def test_crash_recovery_vetoes_on_3_red_days(self, monkeypatch):
        monkeypatch.setattr(config, "SWING_MIN_PNL_PCT_CRASH", -3.0)
        ok_2_red, _ = self._call(regime="CRASH_RECOVERY", consecutive_red_days=2)
        ok_3_red, reason = self._call(regime="CRASH_RECOVERY", consecutive_red_days=3)
        assert ok_2_red
        assert not ok_3_red
        assert "red" in reason.lower()

    @pytest.mark.unit
    def test_non_tier1_no_longer_vetoes(self):
        """is_tier1 gate removed — non-tier1 symbols now qualify in NORMAL."""
        ok, _ = self._call(regime="NORMAL", current_pnl_pct=1.0)
        assert ok  # would have failed before with is_tier1=False

    @pytest.mark.unit
    def test_event_risk_tomorrow_no_longer_vetoes(self):
        """event_risk_tomorrow gate removed — only earnings_tomorrow matters."""
        ok, _ = self._call(regime="NORMAL", earnings_tomorrow=False, current_pnl_pct=1.0)
        assert ok  # event_risk_tomorrow was a separate param — no longer exists
```

- [ ] **Step 2: Run — expect FAIL**

```bash
.venv/bin/python -m pytest tests/test_foundation.py -v -k "TestQualifiesForEodHoldRegimeMatrix" 2>&1 | tail -20
```

- [ ] **Step 3: Replace `qualifies_for_eod_hold()` in `trading/eod_holds.py`**

Replace the entire function with the new implementation:

```python
def qualifies_for_eod_hold(
    symbol: str,
    regime: str,
    earnings_tomorrow: bool,
    current_pnl_pct: float,
    entry_sector_sentiment: float,
    current_sector_sentiment: float,
    consecutive_red_days: int,
    vix_trend: str,
) -> tuple[bool, str]:
    """Return (True, '') if position qualifies for overnight swing hold, (False, reason) otherwise.

    Pure — no Alpaca calls, no file I/O.
    Asymmetric design: fail-fast on thesis-break signals only.
    """
    # Universal veto 1: CRISIS — no overnight exposure under any circumstances
    if regime == "CRISIS":
        return False, "CRISIS regime — no overnight exposure"

    # Universal veto 2: earnings TOMORROW — binary binary risk
    if earnings_tomorrow:
        return False, "earnings tomorrow — overnight binary risk"

    # CHOPPY / DEFENSIVE — block new swing promotions (volatile intraday regimes)
    if regime in ("CHOPPY", "DEFENSIVE"):
        return False, f"{regime} regime — no new swing promotions"

    # PTJ rule: don't hold losers overnight — threshold varies by regime
    ptj_threshold = _ptj_threshold(regime)
    if current_pnl_pct < ptj_threshold:
        return False, f"PTJ rule: P&L {current_pnl_pct:.1f}% < {ptj_threshold}% ({regime})"

    # NORMAL: veto on sector reversal (current < -20 AND entry was > +10)
    if regime == "NORMAL":
        if current_sector_sentiment < -20 and entry_sector_sentiment > 10:
            return False, (
                f"sector reversed: current {current_sector_sentiment:.0f} < -20, "
                f"entry was {entry_sector_sentiment:.0f}"
            )

    # OPPORTUNITY: thesis requires sector health — veto if sector went negative
    if regime == "OPPORTUNITY":
        if current_sector_sentiment < 0 and entry_sector_sentiment > 10:
            return False, (
                f"OPPORTUNITY thesis broken: sector {current_sector_sentiment:.0f} < 0 "
                f"(was {entry_sector_sentiment:.0f} at entry)"
            )

    # CRASH_RECOVERY: veto if recovery stalled (3+ consecutive red days)
    if regime == "CRASH_RECOVERY":
        if consecutive_red_days >= 3:
            return False, f"CRASH_RECOVERY: recovery stalled ({consecutive_red_days} consecutive red days)"

    return True, ""


def _ptj_threshold(regime: str) -> float:
    """Return PTJ rule P&L floor for overnight hold by regime."""
    if regime == "BULL_TREND":
        return config.SWING_MIN_PNL_PCT_BULL
    if regime == "CAPITULATION":
        return config.SWING_MIN_PNL_PCT_CAPITULATION
    if regime in ("CRASH_RECOVERY",):
        return config.SWING_MIN_PNL_PCT_CRASH
    return config.SWING_MIN_PNL_PCT   # NORMAL, OPPORTUNITY, HYBRID: -2.0%
```

- [ ] **Step 4: Update `evaluate_eod_holds()` call site**

Replace the `qualifies_for_eod_hold(...)` call inside `evaluate_eod_holds()`:

```python
        earnings_tomorrow = is_earnings_imminent(symbol, days=1)
        sector = sectors.get_sector(symbol)
        current_sector_score = float(sector_sentiment.get(sector, 0))
        entry_sector_score = float(pos.get("entry_sector_sentiment", 0))

        qualifies, reason = qualifies_for_eod_hold(
            symbol=symbol,
            regime=regime,
            earnings_tomorrow=earnings_tomorrow,
            current_pnl_pct=pnl_pct,
            entry_sector_sentiment=entry_sector_score,
            current_sector_sentiment=current_sector_score,
            consecutive_red_days=regime_info.get("consecutive_red_days", 0),
            vix_trend=regime_info.get("vix_trend", "stable"),
        )
```

Remove the now-unused imports: `is_event_risk_window`, `timedelta` (if no longer used elsewhere in the file), `sectors.is_tier1`.

- [ ] **Step 5: Run tests — expect PASS**

```bash
.venv/bin/python -m pytest tests/test_foundation.py -v -k "TestQualifiesForEodHoldRegimeMatrix" 2>&1 | tail -20
.venv/bin/python -m pytest tests/ -m unit -q --tb=short 2>&1 | tail -5
```

- [ ] **Step 6: Commit**

```bash
git add trading/eod_holds.py tests/test_foundation.py
git commit -m "feat: swing-first EOD holds — regime-aware thesis-break veto matrix replaces 7-gate AND"
```

---

## Task 9: Market cap screener filter

**Files:**
- Modify: `agents/volatility_screener.py`
- Test: `tests/test_core.py`

- [ ] **Step 1: Write failing tests**

```python
@pytest.mark.unit
def test_screener_excludes_small_cap(self, monkeypatch):
    """Symbols with marketCap below MIN_MARKET_CAP_B are filtered out before scoring."""
    monkeypatch.setattr(config, "MIN_MARKET_CAP_B", 25)
    monkeypatch.setattr(config, "MAX_TICKERS_DIRECT", 0)  # force screening

    from agents.volatility_screener import _fetch_market_cap
    # Mock yfinance to return a small-cap
    import yfinance as yf
    monkeypatch.setattr(yf, "Ticker", lambda s: _MockTicker(s, market_cap=5_000_000_000))  # $5B

    cap = _fetch_market_cap("SMALLCO")
    assert cap == 5_000_000_000
    assert cap < 25 * 1e9  # below threshold

@pytest.mark.unit
def test_screener_includes_large_cap(self, monkeypatch):
    """Symbols with marketCap >= MIN_MARKET_CAP_B pass the filter."""
    monkeypatch.setattr(config, "MIN_MARKET_CAP_B", 25)
    import yfinance as yf
    monkeypatch.setattr(yf, "Ticker", lambda s: _MockTicker(s, market_cap=500_000_000_000))  # $500B

    from agents.volatility_screener import _fetch_market_cap
    cap = _fetch_market_cap("NVDA")
    assert cap >= 25 * 1e9

@pytest.mark.unit
def test_screener_excludes_missing_market_cap(self, monkeypatch):
    """Symbols where marketCap is missing/None are excluded (fail-safe)."""
    monkeypatch.setattr(config, "MIN_MARKET_CAP_B", 25)
    import yfinance as yf
    monkeypatch.setattr(yf, "Ticker", lambda s: _MockTicker(s, market_cap=None))

    from agents.volatility_screener import _fetch_market_cap
    cap = _fetch_market_cap("MYSTERY")
    assert cap is None  # None means exclude
```

Add mock helper:

```python
class _MockTicker:
    def __init__(self, symbol, market_cap):
        self.info = {"marketCap": market_cap}
```

- [ ] **Step 2: Run — expect FAIL**

```bash
.venv/bin/python -m pytest tests/test_core.py -v -k "screener" 2>&1 | tail -15
```

- [ ] **Step 3: Add market cap filter to `agents/volatility_screener.py`**

Add helper function after existing imports:

```python
def _fetch_market_cap(symbol: str) -> float | None:
    """Return market cap in dollars, or None if unavailable."""
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).info
        cap = info.get("marketCap")
        return float(cap) if cap else None
    except Exception:
        return None
```

Update `run_volatility_screener()` to add the pre-filter step:

```python
def run_volatility_screener() -> list[str]:
    import sectors
    tickers = sectors.load_universe()

    # Pre-filter: exclude symbols below market cap threshold
    if config.MIN_MARKET_CAP_B > 0:
        min_cap = config.MIN_MARKET_CAP_B * 1e9
        eligible = []
        for ticker in tickers:
            cap = _fetch_market_cap(ticker)
            if cap is not None and cap >= min_cap:
                eligible.append(ticker)
            else:
                logger.debug(f"SCREEN OUT {ticker}: market cap ${cap:,.0f} < ${min_cap:,.0f}" if cap else f"SCREEN OUT {ticker}: market cap unavailable")
        logger.info(f"Market cap filter ({config.MIN_MARKET_CAP_B}B): {len(tickers)} → {len(eligible)} tickers")
        tickers = eligible

    if len(tickers) <= config.MAX_TICKERS_DIRECT:
        logger.info(f"{len(tickers)} tickers ≤ {config.MAX_TICKERS_DIRECT} — no volatility scoring needed.")
        config.DAILY_TRADEABLE_FILE.write_text("\n".join(tickers))
        return tickers

    logger.info(f"Screening {len(tickers)} tickers for top 50 by volatility…")
    client = AlpacaClient()

    scores: dict[str, float] = {}
    for ticker in tickers:
        scores[ticker] = _score_ticker(ticker, client)
        logger.debug(f"  {ticker}: {scores[ticker]:.2f}")

    top50 = sorted(scores, key=lambda s: scores[s], reverse=True)[:50]
    config.DAILY_TRADEABLE_FILE.write_text("\n".join(top50))
    logger.info(f"Volatility screen done. Top 50: {top50}")
    return top50
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
.venv/bin/python -m pytest tests/test_core.py -v -k "screener" 2>&1 | tail -10
.venv/bin/python -m pytest tests/ -m unit -q --tb=short 2>&1 | tail -5
```

- [ ] **Step 5: Commit**

```bash
git add agents/volatility_screener.py tests/test_core.py
git commit -m "feat: add \$25B market cap pre-filter to volatility screener"
```

---

## Task 10: Full test run + 30d backtest regression

**Files:** none — validation only

- [ ] **Step 1: Run full test suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -10
```

Expected: all tests pass. Fix any remaining failures with `monkeypatch.setattr` for changed thresholds.

- [ ] **Step 2: Run 30-day backtest**

```bash
.venv/bin/python3 backtest.py --days 30 2>&1 | tail -40
```

Expected improvements vs baseline (174 trades, -$649, 41% win rate):
- Swing % of trades: from 10% → 25%+
- Total PnL: net positive or significantly less negative
- Force-close count: < 40% of trades (was 55%)

- [ ] **Step 3: Commit final validation note**

```bash
git commit --allow-empty -m "test: swing-first Phase 1 validated — 30d backtest regression complete"
```

---

## Self-Review

**Spec coverage check:**

| Spec requirement | Task |
|---|---|
| Composite floor NORMAL 30→50 | Task 1 |
| Composite floor HIGH_VOL 35→55 | Task 1 |
| CHOPPY composite 35→55 | Task 1 (CHOPPY_COMPOSITE_GATE) |
| BULL_TREND composite 45 (new) | Task 1 + Task 2 |
| AUTO_EXECUTE 65→70 | Task 1 |
| ENTRY_LATE 50→55 | Task 1 |
| BACKTEST_SWING 40→50 | Task 1 |
| SWING_MAX_POSITIONS 2→6 | Task 1 |
| MAX_ENTRY_PRICE = 400 | Task 1 + Task 3 |
| SECTOR_ENTRY_MIN_SCORE = 0 | Task 1 + Task 4 |
| SWING_NEWS_MIN_SCORE = 50 | Task 1 + Task 4 |
| MIN_MARKET_CAP_B = 25 | Task 1 + Task 9 |
| PTJ thresholds by regime | Task 1 + Task 8 |
| Price cap hard gate | Task 3 |
| Sector sentiment soft flag | Task 4 |
| News quality soft flag for swing | Task 4 |
| swing_eligible + major_event_today params | Task 4 + Task 5 |
| major_event_today hard gate | Task 5 |
| CycleSnapshot.major_event_today | Task 6 |
| entry_sector_sentiment stored at buy | Task 7 |
| entry_news_sentiment stored at buy | Task 7 |
| EOD: 3 hard-veto regime-aware matrix | Task 8 |
| EOD: remove is_tier1 gate | Task 8 |
| EOD: remove event_risk_tomorrow gate | Task 8 |
| EOD: remove sector_sentiment < 10 gate | Task 8 |
| EOD: OPPORTUNITY sector veto | Task 8 |
| EOD: CRASH_RECOVERY 3-red-day veto | Task 8 |
| EOD: regime-specific PTJ thresholds | Task 8 |
| Market cap screener pre-filter | Task 9 |
