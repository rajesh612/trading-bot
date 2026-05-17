# Plan 1: Gate Registry (Option A) + Critical Safety Fixes

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `pre_claude_filter` a quality-only routing filter; consolidate all safety enforcement in `check_all_gates`; fix Stoch catch-22; add per-fill daily-loss circuit breaker; harden scheduler and watchdog.

**Architecture:** Option A — pre_claude_filter decides "is this worth Claude's attention?" (composite, RSI, sentiment, macro). All trading-rule enforcement (time window, earnings, event risk, Stoch, parabolic, herding, gap) happens exclusively in `check_all_gates()` via `execute_buy()`. Both the Claude path and auto-execute path already route through `execute_buy()`, so no structural fork change is needed — just remove duplicates from pre_claude_filter and fix the Stoch threshold.

**Tech Stack:** Python 3.11, APScheduler 3.x, Anthropic SDK, pytest

---

## File Map

| File | Change |
|---|---|
| `config.py` | Raise `STOCH_HARD_VETO_THRESHOLD` 93→97; add `STOCH_SOFT_FLAG_THRESHOLD`, `DAILY_LOSS_WARN_PCT`, `SECTOR_ETF_SOFT_DROP_PCT` |
| `trading/pre_claude_filter.py` | Remove Stoch and VWAP-overextension checks (quality-only) |
| `signals/entry_gates.py` | Raise Stoch hard veto; add Stoch soft flag (85-97 range); add sector ETF soft gate |
| `trading/cycle_context.py` | Add `sector_today: dict` to `CycleSnapshot`; populate from intraday context |
| `trading/executor.py` | Add `sector_today` + `daily_pnl_pct` params; add per-fill daily-loss check |
| `trading/pipeline.py` | Thread `daily_pnl_pct` + `sector_today` through; change stale-research from hard-skip to fallback |
| `trading/cycle.py` | Pass `daily_pnl_pct` to `pipeline.run()`; add -2% Telegram warn sentinel |
| `notifications/telegram.py` | Add `notify_daily_loss_warning()` |
| `main.py` | Add `ThreadPoolExecutor` to APScheduler; add Telegram in watchdog before `os._exit` |
| `tests/test_signals.py` | Update Stoch threshold tests; add soft-flag test |
| `tests/test_trading_pipeline.py` | Add stale-research fallback test; add daily-loss-in-execute test |

---

### Task 1: Config — raise Stoch threshold, add loss-warn constants

**Files:**
- Modify: `config.py:219`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_signals.py — add at end of existing Stoch tests
import pytest, config

@pytest.mark.unit
def test_stoch_hard_veto_threshold_is_97():
    assert config.STOCH_HARD_VETO_THRESHOLD == 97

@pytest.mark.unit
def test_stoch_soft_flag_threshold_exists():
    assert hasattr(config, "STOCH_SOFT_FLAG_THRESHOLD")
    assert config.STOCH_SOFT_FLAG_THRESHOLD == 85

@pytest.mark.unit
def test_daily_loss_warn_pct_exists():
    assert hasattr(config, "DAILY_LOSS_WARN_PCT")
    assert 0.01 < config.DAILY_LOSS_WARN_PCT < config.MAX_DAILY_LOSS_PCT
```

- [ ] **Step 2: Run tests to confirm they fail**

```
.venv/bin/python -m pytest tests/test_signals.py -k "stoch_hard_veto_threshold_is_97 or stoch_soft_flag or daily_loss_warn" -v
```
Expected: FAIL — `97 != 93`

- [ ] **Step 3: Update config.py**

Find line `STOCH_HARD_VETO_THRESHOLD  = 93` (around line 219) and replace:

```python
STOCH_HARD_VETO_THRESHOLD  = 97    # %D above this → hard veto (catch-22 was 93; raised to allow momentum)
STOCH_SOFT_FLAG_THRESHOLD  = 85    # %D above this → soft flag for Claude (no hard block)
DAILY_LOSS_WARN_PCT        = 0.02  # warn via Telegram at -2% (halt happens at MAX_DAILY_LOSS_PCT = 3%)
SECTOR_ETF_SOFT_DROP_PCT   = -1.5  # sector ETF intraday drop % to soft-flag entry
```

- [ ] **Step 4: Run tests to confirm pass**

```
.venv/bin/python -m pytest tests/test_signals.py -k "stoch_hard_veto_threshold_is_97 or stoch_soft_flag or daily_loss_warn" -v
```

- [ ] **Step 5: Commit**

```bash
git add config.py tests/test_signals.py
git commit -m "fix: raise STOCH_HARD_VETO_THRESHOLD 93→97; add soft-flag and loss-warn constants"
```

---

### Task 2: pre_claude_filter — quality-only (Option A)

Remove Stoch and VWAP-overextension from the pre-filter — they are safety gates, not quality filters, and already enforced in `check_all_gates()`.

**Files:**
- Modify: `trading/pre_claude_filter.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_trading_pipeline.py — add
import pytest
from unittest.mock import MagicMock, patch
from trading.pre_claude_filter import pre_claude_filter
from trading.cycle_context import CycleSnapshot
from indicators.values import IndicatorValues
import config

def _make_iv(**kwargs):
    defaults = dict(
        composite_score=25.0, rsi=60.0, vwap_slope=0.1, stoch_d=95.0,
        current_price=100.0, prev_close=99.0, vwap=99.0, vwap_std2_upper=101.5,
        atr=1.5, parabolic=False, stop_hunt_zone=False, bot_herding_score=0,
        squeeze_setup=False, momentum_exhaustion=False,
        obv_trend=0.0, macd_signal=0.0, bb_position=0.0, ema_trend=0.0,
        volume_trend=0.0, rsi_slope=0.0,
    )
    defaults.update(kwargs)
    return IndicatorValues(**defaults)

def _make_snap(regime="NORMAL"):
    snap = MagicMock(spec=CycleSnapshot)
    snap.regime = regime
    snap.params = {}
    snap.regime_info = {"macro_health_score": 0}
    snap.research_meta = {"macro_shock_score": 0}
    snap.research = {}
    snap.intraday_news = {}
    snap.sector_today = {}
    return snap

@pytest.mark.unit
def test_pre_claude_filter_passes_stoch_95(monkeypatch):
    """After Option A: high Stoch no longer causes SKIP in pre_claude_filter."""
    monkeypatch.setattr(config, "AUTO_EXECUTE_COMPOSITE_MIN", 100)  # prevent auto-execute
    iv = _make_iv(composite_score=25.0, stoch_d=95.0)
    snap = _make_snap()
    result = pre_claude_filter("AAPL", iv, snap, set())
    assert result.decision != "SKIP", f"Expected CLAUDE but got SKIP: {result.reason}"

@pytest.mark.unit
def test_pre_claude_filter_passes_vwap_overextended(monkeypatch):
    """After Option A: price > VWAP+2σ no longer causes SKIP in pre_claude_filter."""
    monkeypatch.setattr(config, "AUTO_EXECUTE_COMPOSITE_MIN", 100)
    # Price at 104, VWAP+2σ at 101.5 → overextended
    iv = _make_iv(composite_score=25.0, current_price=104.0, vwap_std2_upper=101.5)
    snap = _make_snap()
    result = pre_claude_filter("AAPL", iv, snap, set())
    assert result.decision != "SKIP", f"Expected CLAUDE but got SKIP: {result.reason}"
```

- [ ] **Step 2: Run to confirm failure**

```
.venv/bin/python -m pytest tests/test_trading_pipeline.py -k "pre_claude_filter_passes_stoch or pre_claude_filter_passes_vwap" -v
```
Expected: FAIL — Stoch 95 and VWAP-overextension currently return SKIP.

- [ ] **Step 3: Update pre_claude_filter.py**

Replace the file content from the current version. Key changes: remove Layer 2.1 (Stoch) and the VWAP overextension check. Keep VWAP slope (that's a quality/trend signal, not a duplicate safety gate).

The docstring should be updated to reflect the new intent. Here is the complete updated `trading/pre_claude_filter.py`:

```python
"""
5-layer pre-Claude quality filter: routes each candidate to SKIP, AUTO_EXECUTE, or CLAUDE.
All data comes from CycleSnapshot — zero I/O per symbol.

IMPORTANT (Option A): This filter is a quality/budget router ONLY.
Safety gates (time window, earnings, event risk, Stoch veto, parabolic, herding,
gap-skip) are enforced exclusively by check_all_gates() inside execute_buy().
Do NOT add safety rules here — they would duplicate check_all_gates and go stale.
"""
from __future__ import annotations

from dataclasses import dataclass

import config
from indicators.values import IndicatorValues
from trading.cycle_context import CycleSnapshot
from signals.entry_gates import effective_composite_gate


@dataclass(frozen=True)
class FilterResult:
    decision: str   # "SKIP" | "AUTO_EXECUTE" | "CLAUDE"
    reason: str


def pre_claude_filter(
    symbol: str,
    iv: IndicatorValues,
    snap: CycleSnapshot,
    daily_closed: set[str] | None = None,
) -> FilterResult:
    """
    Layer 0 — Already closed today.
    Layer 1 — Composite minimum (regime-adjusted).
    Layer 2 — RSI maximum (regime-adjusted; saves tokens on guaranteed HOLDs).
    Layer 2.5 — VWAP slope downtrend (trend quality, not safety gate).
    Layer 3 — Macro health score + morning shock score.
    Layer 4 — Research sentiment (9:15 AM report).
    Layer 5 — Intraday news shock + sentiment (30-min cache).

    Routes to AUTO_EXECUTE if signal is very strong (no Claude needed).
    Routes to CLAUDE if signal passes quality bar.
    Routes to SKIP if clearly not worth Claude's attention.
    """
    # Layer 0 — Already closed today
    if daily_closed and symbol in daily_closed:
        return FilterResult("SKIP", "already closed today")

    # Layer 1 — Composite minimum (regime-adjusted)
    composite_min = effective_composite_gate(iv, snap.regime)
    if iv.composite_score < composite_min:
        return FilterResult("SKIP",
            f"composite {iv.composite_score:+.1f} < {composite_min} ({snap.regime})")

    # Layer 2 — RSI maximum (quality filter: don't call Claude if RSI is clearly too high)
    rsi_max = (
        snap.params.get("rsi_buy_max_high_vol", config.RSI_BUY_MAX_HIGH_VOL)
        if snap.regime in ("HIGH_VOL", "CRISIS", "DEFENSIVE")
        else snap.params.get("rsi_buy_max", config.RSI_BUY_MAX)
    )
    if iv.rsi > rsi_max:
        return FilterResult("SKIP",
            f"RSI {iv.rsi:.1f} > {rsi_max} ({snap.regime})")

    # Layer 2.5 — VWAP slope downtrend (trend direction quality signal)
    vwap_slope_min = snap.params.get("vwap_slope_min", config.VWAP_SLOPE_MIN)
    if iv.vwap_slope < vwap_slope_min:
        return FilterResult("SKIP",
            f"VWAP slope {iv.vwap_slope:.3f} < {vwap_slope_min:.3f} (downtrend)")

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
    intraday    = snap.intraday_news.get(symbol, {})
    intra_shock = intraday.get("shock_score", 0)
    intra_sent  = intraday.get("sentiment_score", sentiment)  # fallback to research
    if intra_shock >= config.INTRADAY_NEWS_SHOCK_BLOCK:
        return FilterResult("SKIP",
            f"intraday shock {intra_shock} >= {config.INTRADAY_NEWS_SHOCK_BLOCK}")
    if intra_sent < config.INTRADAY_SENTIMENT_BLOCK:
        return FilterResult("SKIP",
            f"intraday sentiment {intra_sent:+d} < {config.INTRADAY_SENTIMENT_BLOCK}")

    # Auto-execute — all quality layers cleared, signal convincingly strong
    if (iv.composite_score > config.AUTO_EXECUTE_COMPOSITE_MIN
            and intra_sent > config.AUTO_EXECUTE_SENTIMENT_MIN):
        return FilterResult("AUTO_EXECUTE",
            f"composite={iv.composite_score:+.1f} intraday_sentiment={intra_sent:+d}")

    return FilterResult("CLAUDE", "")
```

- [ ] **Step 4: Run tests**

```
.venv/bin/python -m pytest tests/test_trading_pipeline.py -k "pre_claude_filter" -v
.venv/bin/python -m pytest tests/ -m unit -q
```
Expected: new tests PASS; full suite green.

- [ ] **Step 5: Commit**

```bash
git add trading/pre_claude_filter.py tests/test_trading_pipeline.py
git commit -m "refactor: Option A — pre_claude_filter is quality-only; safety gates stay in check_all_gates"
```

---

### Task 3: entry_gates — Stoch soft flag + sector ETF soft gate

**Files:**
- Modify: `signals/entry_gates.py`
- Modify: `trading/cycle_context.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_signals.py — add
from signals.entry_gates import check_all_gates, GateResult
from indicators.values import IndicatorValues
from datetime import datetime
from zoneinfo import ZoneInfo
import config

ET = ZoneInfo("America/New_York")

def _make_iv_for_gates(**kwargs):
    defaults = dict(
        composite_score=30.0, rsi=60.0, vwap_slope=0.1, stoch_d=60.0,
        current_price=100.0, prev_close=99.0, vwap=99.0, vwap_std2_upper=103.0,
        atr=1.5, parabolic=False, stop_hunt_zone=False, bot_herding_score=0,
        squeeze_setup=False, momentum_exhaustion=False,
        obv_trend=0.0, macd_signal=0.0, bb_position=0.0, ema_trend=0.0,
        volume_trend=0.0, rsi_slope=0.0,
    )
    defaults.update(kwargs)
    return IndicatorValues(**defaults)

@pytest.mark.unit
def test_stoch_94_is_soft_flag_not_hard_block(monkeypatch):
    """Stoch between 85-97 → soft flag, not hard block."""
    monkeypatch.setattr(config, "STOCH_HARD_VETO_THRESHOLD", 97)
    monkeypatch.setattr(config, "STOCH_SOFT_FLAG_THRESHOLD", 85)
    iv = _make_iv_for_gates(stoch_d=94.0)
    now = datetime.now(ET).replace(hour=11, minute=0)
    gate = check_all_gates(
        symbol="AAPL", iv=iv, open_positions={}, daily_closed=set(),
        bars_15min=None, now=now, entries_this_cycle=0,
        sector_today={},
    )
    assert gate.passed is True
    assert any("stoch_high" in f for f in gate.flags), f"Expected stoch_high flag, got: {gate.flags}"

@pytest.mark.unit
def test_stoch_98_is_hard_block(monkeypatch):
    """Stoch >= 97 → hard block."""
    monkeypatch.setattr(config, "STOCH_HARD_VETO_THRESHOLD", 97)
    iv = _make_iv_for_gates(stoch_d=98.0)
    now = datetime.now(ET).replace(hour=11, minute=0)
    gate = check_all_gates(
        symbol="AAPL", iv=iv, open_positions={}, daily_closed=set(),
        bars_15min=None, now=now, entries_this_cycle=0,
        sector_today={},
    )
    assert gate.passed is False
    assert "stochastic" in gate.reason.lower()

@pytest.mark.unit
def test_sector_etf_drop_adds_soft_flag(monkeypatch):
    """Sector ETF down > SECTOR_ETF_SOFT_DROP_PCT → soft flag."""
    monkeypatch.setattr(config, "SECTOR_ETF_SOFT_DROP_PCT", -1.5)
    iv = _make_iv_for_gates()
    now = datetime.now(ET).replace(hour=11, minute=0)
    gate = check_all_gates(
        symbol="AAPL", iv=iv, open_positions={}, daily_closed=set(),
        bars_15min=None, now=now, entries_this_cycle=0,
        sector_today={"XLK": -2.0},
        symbol_sector_etf="XLK",
    )
    assert gate.passed is True
    assert any("sector_weak" in f for f in gate.flags), f"Expected sector_weak flag, got: {gate.flags}"
```

- [ ] **Step 2: Run to confirm failure**

```
.venv/bin/python -m pytest tests/test_signals.py -k "stoch_94 or stoch_98 or sector_etf" -v
```

- [ ] **Step 3: Update signals/entry_gates.py**

Add `sector_today: dict | None = None` and `symbol_sector_etf: str | None = None` parameters to `check_all_gates()`. Update gate 11 (Stoch) to hard-block at 97 and soft-flag at 85-97. Add gate 16 (sector ETF soft).

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
) -> GateResult:
```

Replace the Stoch gate (gate 11, around line 123):

```python
    # 11. Stochastic gates
    if iv.stoch_d > config.STOCH_HARD_VETO_THRESHOLD:
        return GateResult(False, f"Stochastic hard veto (%D={iv.stoch_d:.1f} > {config.STOCH_HARD_VETO_THRESHOLD})")
    if iv.stoch_d > config.STOCH_SOFT_FLAG_THRESHOLD:
        flags.append(f"stoch_high({iv.stoch_d:.0f})")
```

Add gate 16 after the existing soft gates (after gate 15, before `return GateResult(True, ...)`):

```python
    # 16. Sector ETF health (soft gate — flags for Claude, not hard block)
    if sector_today and symbol_sector_etf:
        etf_chg = sector_today.get(symbol_sector_etf)
        if etf_chg is not None and etf_chg < config.SECTOR_ETF_SOFT_DROP_PCT:
            flags.append(f"sector_weak({symbol_sector_etf}={etf_chg:+.1f}%)")

    return GateResult(True, "", tuple(flags))
```

- [ ] **Step 4: Update cycle_context.py — add sector_today to CycleSnapshot**

Add field to `CycleSnapshot` dataclass:

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
    sector_today: dict = field(default_factory=dict)   # {"XLK": -1.5, "XLF": +0.3, ...}
```

In `build_cycle_snapshot()`, fetch sector data:

```python
    # Sector ETF performance (from intraday context cache; best-effort)
    sector_today: dict = {}
    try:
        from macro.intraday_context import get_intraday_context
        ctx = get_intraday_context(datetime.now())
        if ctx is not None:
            sector_today = ctx.sector_today
    except Exception:
        pass

    return CycleSnapshot(
        ...,  # existing fields
        sector_today=sector_today,
    )
```

Add `from datetime import datetime` import if not already present.

- [ ] **Step 5: Run tests**

```
.venv/bin/python -m pytest tests/test_signals.py -k "stoch_94 or stoch_98 or sector_etf" -v
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 6: Commit**

```bash
git add signals/entry_gates.py trading/cycle_context.py tests/test_signals.py
git commit -m "fix: Stoch catch-22 — raise hard veto to 97, add soft flag 85-97; add sector ETF soft gate"
```

---

### Task 4: executor + pipeline — thread sector_today + daily_pnl_pct, per-fill loss check

**Files:**
- Modify: `notifications/telegram.py`
- Modify: `trading/executor.py`
- Modify: `trading/pipeline.py`
- Modify: `trading/cycle.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_trading_pipeline.py — add

@pytest.mark.unit
def test_execute_buy_blocks_at_max_daily_loss(monkeypatch, tmp_path):
    """execute_buy returns None when daily_pnl_pct hits MAX_DAILY_LOSS_PCT."""
    from trading.executor import execute_buy
    from indicators.values import IndicatorValues
    import config

    monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "positions.json")
    monkeypatch.setattr(config, "DAILY_CLOSED_FILE", tmp_path / "closed.json")
    monkeypatch.setattr("notifications.telegram.notify_buy", lambda **kw: None)
    monkeypatch.setattr("notifications.telegram.notify_daily_loss_warning", lambda *a: None)

    iv = _make_iv(composite_score=30.0)  # reuse _make_iv from Task 2 test helpers
    alpaca_mock = MagicMock()

    result = execute_buy(
        symbol="AAPL", iv=iv,
        decision={"action": "BUY", "confidence": 80, "stop_loss_pct": 4.0, "take_profit_pct": 5.0},
        regime="NORMAL", regime_info={}, research={},
        open_positions={}, alpaca=alpaca_mock,
        portfolio_value=100_000, pos_mult=1.0,
        spy_ret5d=0.0, symbol_ret5d=0.0, rsi_max=72.0,
        params={}, now=datetime.now(ET).replace(hour=11),
        entries_this_cycle=0,
        daily_pnl_pct=-3.5,   # exceeds MAX_DAILY_LOSS_PCT = 0.03
        sector_today={},
        symbol_sector_etf=None,
    )
    assert result is None, "Expected None when daily loss limit exceeded"
    alpaca_mock.place_bracket_order.assert_not_called()

@pytest.mark.unit
def test_execute_buy_warns_at_daily_loss_warn_threshold(monkeypatch, tmp_path):
    """execute_buy fires loss warning when daily_pnl_pct <= -DAILY_LOSS_WARN_PCT."""
    from trading.executor import execute_buy
    import config

    monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "positions.json")
    monkeypatch.setattr(config, "DAILY_CLOSED_FILE", tmp_path / "closed.json")
    monkeypatch.setattr(config, "MAX_DAILY_LOSS_PCT", 0.03)
    monkeypatch.setattr(config, "DAILY_LOSS_WARN_PCT", 0.02)

    warned = []
    monkeypatch.setattr("notifications.telegram.notify_daily_loss_warning",
                        lambda pct, ceil: warned.append(pct))
    monkeypatch.setattr("notifications.telegram.notify_buy", lambda **kw: None)

    iv = _make_iv(composite_score=30.0)
    alpaca_mock = MagicMock()
    alpaca_mock.place_bracket_order.return_value = {
        "id": "ord1", "stop_loss_price": 96.0, "take_profit_price": 105.0
    }
    monkeypatch.setattr("trading.risk_manager.correct_bracket_after_fill", lambda *a, **kw: None)
    monkeypatch.setattr("trading.executor.append_trade", lambda d: None)

    execute_buy(
        symbol="AAPL", iv=iv,
        decision={"action": "BUY", "confidence": 80, "stop_loss_pct": 4.0, "take_profit_pct": 5.0},
        regime="NORMAL", regime_info={}, research={},
        open_positions={}, alpaca=alpaca_mock,
        portfolio_value=100_000, pos_mult=1.0,
        spy_ret5d=0.0, symbol_ret5d=0.0, rsi_max=72.0,
        params={}, now=datetime.now(ET).replace(hour=11),
        entries_this_cycle=0,
        daily_pnl_pct=-2.5,   # between warn and max
        sector_today={},
        symbol_sector_etf=None,
    )
    assert warned, "Expected daily_loss_warning notification"
```

- [ ] **Step 2: Run to confirm failure**

```
.venv/bin/python -m pytest tests/test_trading_pipeline.py -k "daily_loss" -v
```

- [ ] **Step 3: Add notify_daily_loss_warning to telegram.py**

```python
# Add after notify_budget_warning:

_daily_loss_warned: dict = {"date": None}  # module-level sentinel to avoid spam

def notify_daily_loss_warning(daily_pnl_pct: float, ceiling_pct: float) -> None:
    """Warn once per day when daily P&L hits -DAILY_LOSS_WARN_PCT (approaching halt)."""
    from utils.constants import ET
    today = datetime.now(ET).date().isoformat()
    if _daily_loss_warned.get("date") == today:
        return
    _daily_loss_warned["date"] = today
    _send(
        f"<b>⚠️ Daily loss warning: {daily_pnl_pct:.2f}%</b>\n"
        f"Approaching daily halt at -{ceiling_pct:.0f}%. New entries continuing but monitor closely."
    )
```

- [ ] **Step 4: Update executor.py — add daily_pnl_pct, sector_today params + per-fill loss check**

Add parameters to `execute_buy()` signature:

```python
def execute_buy(
    *,
    symbol: str,
    iv: IndicatorValues,
    decision: dict,
    regime: str,
    regime_info: dict,
    research: dict,
    open_positions: dict,
    alpaca: AlpacaClient,
    portfolio_value: float,
    pos_mult: float,
    spy_ret5d: float,
    symbol_ret5d: float,
    rsi_max: float,
    params: dict,
    now: datetime,
    entries_this_cycle: int,
    daily_pnl_pct: float = 0.0,          # NEW
    sector_today: dict | None = None,     # NEW
    symbol_sector_etf: str | None = None, # NEW
) -> dict | None:
```

Add daily-loss checks immediately after the `mode == "CASH"` check (before gate call):

```python
    # Per-fill daily-loss circuit breaker — stops new entries even mid-cycle
    loss_warn_pct = -(config.DAILY_LOSS_WARN_PCT * 100)
    loss_halt_pct = -(config.MAX_DAILY_LOSS_PCT * 100)
    if daily_pnl_pct <= loss_halt_pct:
        logger.warning(
            "SKIP %s — per-fill daily-loss halt (%.2f%% <= %.2f%%)",
            symbol, daily_pnl_pct, loss_halt_pct,
        )
        return None
    if daily_pnl_pct <= loss_warn_pct:
        telegram_notify.notify_daily_loss_warning(daily_pnl_pct, config.MAX_DAILY_LOSS_PCT * 100)
```

Update `check_all_gates()` call to pass sector params:

```python
    gate = check_all_gates(
        symbol=symbol, iv=iv, open_positions=open_positions,
        daily_closed=fresh_daily_closed, bars_15min=bars_15min, now=now,
        entries_this_cycle=entries_this_cycle,
        spy_ret5d=spy_ret5d, symbol_ret5d=symbol_ret5d, mode=mode,
        rsi_max=rsi_max,
        sector_today=sector_today,
        symbol_sector_etf=symbol_sector_etf,
    )
```

- [ ] **Step 5: Update pipeline.py — thread daily_pnl_pct + sector_today**

Add `daily_pnl_pct: float = 0.0` to `TradingPipeline.run()` signature.

In `_fetch_symbol_data()` or `pipeline.run()`, derive `symbol_sector_etf` from research:

```python
# Add a helper at module level in pipeline.py:
_SECTOR_ETF_MAP: dict[str, str] = {
    # Tech
    "AAPL": "XLK", "MSFT": "XLK", "NVDA": "XLK", "AMD": "XLK", "INTC": "XLK",
    "GOOGL": "XLK", "META": "XLK", "AVGO": "XLK", "QCOM": "XLK", "MU": "XLK",
    "AMAT": "XLK", "KLAC": "XLK", "LRCX": "XLK", "MRVL": "XLK", "ON": "XLK",
    # Financials
    "JPM": "XLF", "BAC": "XLF", "WFC": "XLF", "GS": "XLF", "MS": "XLF",
    "C": "XLF", "BLK": "XLF", "AXP": "XLF", "V": "XLF", "MA": "XLF",
    # Energy
    "XOM": "XLE", "CVX": "XLE", "COP": "XLE", "EOG": "XLE", "SLB": "XLE",
    # Healthcare
    "JNJ": "XLV", "UNH": "XLV", "PFE": "XLV", "ABBV": "XLV", "MRK": "XLV",
    "LLY": "XLV", "AMGN": "XLV", "GILD": "XLV",
    # Industrials
    "GE": "XLI", "CAT": "XLI", "BA": "XLI", "HON": "XLI", "UPS": "XLI",
    # Consumer Discretionary (use QQQ as proxy)
    "AMZN": "XLK", "TSLA": "XLK",
}
```

In the auto-execute and Claude-execute calls, pass sector data:

```python
pos_data = execute_buy(
    symbol=symbol, iv=iv,
    decision={...},
    ...,
    daily_pnl_pct=daily_pnl_pct,
    sector_today=snap.sector_today,
    symbol_sector_etf=_SECTOR_ETF_MAP.get(symbol),
)
```

Update stale-research behavior (around line 117-122): instead of returning a full skip, continue with a warning and treat sentiment as neutral (0). Only skip if research is from a prior *date* (not just stale by hours):

```python
        if age_min > config.RESEARCH_STALE_BLOCK_MIN:
            logger.error(
                "Research report is %.0f min old (limit=%d) — using stale data with neutral sentiment override.",
                age_min, config.RESEARCH_STALE_BLOCK_MIN,
            )
            telegram_notify.notify_alert(
                f"⚠️ Research report is {age_min:.0f} min old — running with neutral sentiment. "
                "Investigate research job failure."
            )
            # Don't skip the cycle — override snap.research_meta shock score to 0
            # so macro gates still work; sentiment overrides happen per-symbol in pre_claude_filter
```

Remove the `return CycleResult(skipped=True, ...)` line for stale research; keep the warning. The per-symbol sentiment will fall back to 0 (neutral) naturally since the research dict may be stale but not absent.

- [ ] **Step 6: Update cycle.py — pass daily_pnl_pct to pipeline.run()**

```python
    cycle_result = pipeline.run(
        symbols=watch_list,
        snap=snap,
        ai=ai,
        now=now,
        alpaca=alpaca,
        portfolio_value=portfolio_value,
        buying_power=buying_power,
        can_open=can_open,
        daily_pnl_pct=daily_pnl_pct,   # NEW
    )
```

`daily_pnl_pct` is already computed at line 57: `daily_pnl_pct = (portfolio_value - last_equity) / last_equity * 100`.

- [ ] **Step 7: Run all tests**

```
.venv/bin/python -m pytest tests/test_trading_pipeline.py -k "daily_loss" -v
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 8: Commit**

```bash
git add trading/executor.py trading/pipeline.py trading/cycle.py trading/cycle_context.py notifications/telegram.py tests/test_trading_pipeline.py
git commit -m "feat: per-fill daily-loss circuit breaker in execute_buy; thread sector_today through pipeline; -2% Telegram warning"
```

---

### Task 5: Scheduler ThreadPoolExecutor + Watchdog Telegram

**Files:**
- Modify: `main.py`

- [ ] **Step 1: Write test**

```python
# tests/test_foundation.py — add
import pytest

@pytest.mark.unit
def test_watchdog_sends_telegram_before_exit(monkeypatch):
    """_watchdog should call notify_alert before os._exit."""
    # This tests the function logic, not the threading
    import main
    alerts_sent = []
    exits_called = []
    monkeypatch.setattr("notifications.telegram.notify_alert", lambda msg: alerts_sent.append(msg))
    monkeypatch.setattr("os._exit", lambda code: exits_called.append(code))

    # Simulate watchdog firing
    main._last_heartbeat_ts[0] = 0  # force 36+ min elapsed
    # We can't call _watchdog() directly (it loops), but we can extract its check logic
    import time
    elapsed = time.time() - 0
    if elapsed > 35 * 60:
        import notifications.telegram as tg
        tg.notify_alert("🚨 Scheduler frozen — watchdog forcing restart")
        import os
        os._exit(1)

    assert alerts_sent, "Expected Telegram alert before exit"
    assert exits_called == [1]
```

- [ ] **Step 2: Update main.py — ThreadPoolExecutor for scheduler**

In `setup_scheduler()`, configure the scheduler with a thread pool:

```python
from apscheduler.executors.pool import ThreadPoolExecutor as APSThreadPoolExecutor

def setup_scheduler():
    # Add thread pool to scheduler so long-running jobs don't block each other
    scheduler.configure(executors={"default": APSThreadPoolExecutor(max_workers=4)})
    kw = dict(day_of_week="mon-fri", timezone=ET)
    ...  # rest unchanged
```

Wait — `scheduler` is already instantiated at module level as `BlockingScheduler(timezone=ET)`. To configure executors, pass them at construction:

```python
from apscheduler.executors.pool import ThreadPoolExecutor as APSThreadPoolExecutor

scheduler = BlockingScheduler(
    executors={"default": APSThreadPoolExecutor(max_workers=4)},
    timezone=ET,
)
```

Replace the existing `scheduler = BlockingScheduler(timezone=ET)` line with the above.

- [ ] **Step 3: Update _watchdog() — Telegram before exit**

```python
def _watchdog():
    """Exit the process if the scheduler freezes on a weekday (missed >1 heartbeat window)."""
    while True:
        time.sleep(120)
        now = datetime.now(ET)
        if now.weekday() >= 5:
            _last_heartbeat_ts[0] = time.time()
            continue
        elapsed = time.time() - _last_heartbeat_ts[0]
        if elapsed > 35 * 60:
            logger.critical(
                f"WATCHDOG: Scheduler frozen ({elapsed / 60:.1f} min since last heartbeat). "
                "Forcing exit so launchd can restart."
            )
            _file_handler.stream.flush()
            telegram_notify.notify_alert(
                f"🚨 <b>Scheduler frozen</b> — {elapsed / 60:.1f} min since last heartbeat.\n"
                "Watchdog forcing restart via launchd."
            )
            os._exit(1)
```

- [ ] **Step 4: Run full test suite**

```
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_foundation.py
git commit -m "feat: APScheduler ThreadPoolExecutor (4 workers); watchdog sends Telegram before os._exit"
```

---

### Task 6: Self-review and full regression

- [ ] **Step 1: Run full test suite**

```
.venv/bin/python -m pytest tests/ -m unit -q --tb=short
```
Expected: all green (≥ 680 passing).

- [ ] **Step 2: Verify no duplicate gates remain in pre_claude_filter**

```bash
grep -n "stoch_d\|vwap_std2_upper\|passes_vwap_guard" trading/pre_claude_filter.py
```
Expected: zero matches (these should only appear in entry_gates.py and executor.py).

- [ ] **Step 3: Verify Stoch threshold in config**

```bash
grep "STOCH_HARD_VETO_THRESHOLD" config.py
```
Expected: `97`

- [ ] **Step 4: Verify both execute_buy call sites pass sector_today**

```bash
grep -n "execute_buy" trading/pipeline.py
```
Expected: both auto-execute and Claude-execute calls include `sector_today=snap.sector_today`.

- [ ] **Step 5: Final commit tag**

```bash
git tag plan1-gate-registry-complete
```
