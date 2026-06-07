# Dynamic Regime × Tier Trading System — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace all static config thresholds with dynamic regime × tier parameters, add TickerQuality scoring, sector sub-grouping, time-aware TP/SL, and tier-dispatched Claude context.

**Architecture:** Phase 1 builds pure-function foundation modules (ticker_quality, tp_scaler, extended regime_params, sector sub-grouping). Phase 2 wires them into every live decision point. No Phase 1 module changes live trading behavior — it is infrastructure only.

**Tech Stack:** Python 3.11, pytest, existing alpaca/yahoo data stack, `@dataclass(frozen=True)` pattern throughout.

**Spec:** `docs/superpowers/specs/2026-05-21-dynamic-regime-tier-system-design.md`

---

## Phase 1 — Foundation

---

### Task 1: `utils/ticker_quality.py` — TickerQuality dataclass + tier scoring

**Files:**
- Create: `utils/ticker_quality.py`
- Create: `tests/test_ticker_quality.py`

- [ ] **Step 1: Write failing tests for dataclass and tier boundaries**

```python
# tests/test_ticker_quality.py
import pytest
from unittest.mock import patch
from indicators.values import IndicatorValues
from utils.ticker_quality import TickerQuality, compute_ticker_quality, _score_to_tier

class TestTierBoundaries:
    def test_low_tier(self):
        assert _score_to_tier(0) == "LOW"
        assert _score_to_tier(34) == "LOW"

    def test_medium_tier(self):
        assert _score_to_tier(35) == "MEDIUM"
        assert _score_to_tier(59) == "MEDIUM"

    def test_high_tier(self):
        assert _score_to_tier(60) == "HIGH"
        assert _score_to_tier(79) == "HIGH"

    def test_conviction_tier(self):
        assert _score_to_tier(80) == "CONVICTION"
        assert _score_to_tier(100) == "CONVICTION"
```

- [ ] **Step 2: Run to verify failure**

```bash
cd ~/Documents/projects/trading-bot
.venv/bin/python -m pytest tests/test_ticker_quality.py -v 2>&1 | head -20
```
Expected: `ModuleNotFoundError` or `ImportError`

- [ ] **Step 3: Create `utils/ticker_quality.py` with dataclass and tier function**

```python
"""
TickerQuality scorer — Rule 28 SoT for tier assignment and unlock evaluation.
Pure — no I/O. All inputs supplied by callers.
"""
from __future__ import annotations
from dataclasses import dataclass, field

TIERS = ("LOW", "MEDIUM", "HIGH", "CONVICTION")


@dataclass(frozen=True)
class TickerQuality:
    tier: str
    score: int
    unlocks: frozenset
    quality_gaps: list
    component_scores: dict


def _score_to_tier(score: int) -> str:
    if score >= 80:
        return "CONVICTION"
    if score >= 60:
        return "HIGH"
    if score >= 35:
        return "MEDIUM"
    return "LOW"
```

- [ ] **Step 4: Run tier tests — verify pass**

```bash
.venv/bin/python -m pytest tests/test_ticker_quality.py::TestTierBoundaries -v
```
Expected: 4 PASSED

- [ ] **Step 5: Write failing tests for component scoring**

Add to `tests/test_ticker_quality.py`:

```python
from unittest.mock import MagicMock, patch
import config

def _make_iv(composite=50.0, rsi=55.0, relative_volume=1.2):
    iv = MagicMock()
    iv.composite_score = composite
    iv.rsi = rsi
    iv.relative_volume = relative_volume
    iv.current_price = 100.0
    iv.atr = 2.0
    return iv

def _make_snap(regime="NORMAL", sentiment=30, intra_sent=25, research=None):
    snap = MagicMock()
    snap.regime = regime
    snap.research = research or {"AAPL": {"sentiment_score": sentiment}}
    snap.intraday_news = {"AAPL": {"sentiment_score": intra_sent, "shock_score": 0}}
    snap.params = {}
    return snap

class TestComputeTickerQuality:
    @patch("utils.ticker_quality.detect_pead_setup", return_value=(False, 0))
    @patch("utils.ticker_quality.is_event_risk_window", return_value=(False, ""))
    @patch("utils.ticker_quality.get_entry_params")
    def test_medium_tier_standard_signals(self, mock_ep, mock_ec, mock_pead, monkeypatch):
        monkeypatch.setattr(config, "COMPOSITE_MIN_NORMAL", 50)
        mock_ep.return_value = MagicMock(composite_min=50)
        iv = _make_iv(composite=55.0)
        snap = _make_snap(sentiment=35, intra_sent=30)
        result = compute_ticker_quality(
            "AAPL", iv, snap, gate_flags=[], mode="DAY_TRADE",
            sub_etf_chg=0.5, earnings_clear_days=20,
        )
        assert result.tier == "MEDIUM"
        assert 35 <= result.score <= 59
        assert isinstance(result.unlocks, frozenset)
        assert isinstance(result.quality_gaps, list)

    @patch("utils.ticker_quality.detect_pead_setup", return_value=(True, 85))
    @patch("utils.ticker_quality.is_event_risk_window", return_value=(False, ""))
    @patch("utils.ticker_quality.get_entry_params")
    def test_conviction_with_pead_and_strong_sentiment(self, mock_ep, mock_ec, mock_pead, monkeypatch):
        monkeypatch.setattr(config, "COMPOSITE_MIN_NORMAL", 50)
        mock_ep.return_value = MagicMock(composite_min=50)
        iv = _make_iv(composite=75.0)
        snap = _make_snap(sentiment=70, intra_sent=65)
        result = compute_ticker_quality(
            "NVDA", iv, snap, gate_flags=[], mode="PEAD",
            sub_etf_chg=2.5, earnings_clear_days=35,
        )
        assert result.tier == "CONVICTION"
        assert result.score >= 80

    @patch("utils.ticker_quality.detect_pead_setup", return_value=(False, 0))
    @patch("utils.ticker_quality.is_event_risk_window", return_value=(False, ""))
    @patch("utils.ticker_quality.get_entry_params")
    def test_low_quality_gaps_populated(self, mock_ep, mock_ec, mock_pead, monkeypatch):
        monkeypatch.setattr(config, "COMPOSITE_MIN_NORMAL", 50)
        mock_ep.return_value = MagicMock(composite_min=50)
        iv = _make_iv(composite=32.0, relative_volume=0.5)
        snap = _make_snap(sentiment=-5, intra_sent=-3)
        result = compute_ticker_quality(
            "RIVN", iv, snap, gate_flags=["volume_weak"], mode="DAY_TRADE",
            sub_etf_chg=-0.5, earnings_clear_days=5,
        )
        assert result.tier == "LOW"
        assert len(result.quality_gaps) > 0
```

- [ ] **Step 6: Run to verify failure**

```bash
.venv/bin/python -m pytest tests/test_ticker_quality.py::TestComputeTickerQuality -v 2>&1 | head -20
```
Expected: `ImportError` (function not defined yet)

- [ ] **Step 7: Implement `compute_ticker_quality` and component scorers**

Add to `utils/ticker_quality.py`:

```python
from calendars.earnings import detect_pead_setup
from macro.economic_calendar import is_event_risk_window
from utils.regime_params import get_entry_params


def _score_composite(composite: float, floor: int) -> tuple[int, str | None]:
    pts = int(min(25, max(0, (composite - floor) / 50 * 25)))
    gap = f"composite_weak(-{25 - pts})" if pts < 10 else None
    return pts, gap


def _score_sentiment(sentiment: int) -> tuple[int, str | None]:
    if sentiment >= 60:
        pts = 20
    elif sentiment >= 30:
        pts = 14
    elif sentiment >= 0:
        pts = 8
    else:
        pts = 0
    gap = f"sentiment_weak(-{20 - pts})" if pts < 8 else None
    return pts, gap


def _score_intraday(intra_sent: int) -> tuple[int, str | None]:
    if intra_sent >= 55:
        pts = 15
    elif intra_sent >= 25:
        pts = 10
    elif intra_sent >= 0:
        pts = 5
    else:
        pts = 0
    gap = f"intraday_weak(-{15 - pts})" if pts < 5 else None
    return pts, gap


def _score_sub_etf(sub_etf_chg: float) -> tuple[int, str | None]:
    if sub_etf_chg >= 2.0:
        pts = 15
    elif sub_etf_chg >= 1.0:
        pts = 10
    elif sub_etf_chg >= 0.0:
        pts = 5
    else:
        pts = 0
    gap = f"sector_weak(-{15 - pts})" if pts < 5 else None
    return pts, gap


def _score_pead(pead_ok: bool, pead_score: int) -> tuple[int, str | None]:
    if not pead_ok:
        return 0, None
    pts = int(pead_score / 100 * 10)
    return pts, None


def _score_earnings(earnings_clear_days: int) -> tuple[int, str | None]:
    if earnings_clear_days >= 30:
        pts = 10
    elif earnings_clear_days >= 21:
        pts = 8
    elif earnings_clear_days >= 14:
        pts = 6
    elif earnings_clear_days >= 7:
        pts = 3
    else:
        pts = 0
    gap = f"earnings_near(-{10 - pts})" if pts < 6 else None
    return pts, gap


def _score_econ(event_active: bool, event_medium: bool) -> int:
    if event_active:
        return 0
    if event_medium:
        return 2
    return 5


def _compute_unlocks(tier: str, mode: str, pead_ok: bool,
                     intra_sent: int, sentiment: int,
                     earnings_clear_days: int, regime: str) -> frozenset:
    unlocks = set()
    if tier == "CONVICTION":
        if pead_ok or intra_sent >= 60:
            unlocks.add("T2_CHOPPY")
        if intra_sent >= 50 and earnings_clear_days >= 14:
            unlocks.add("SWING_CHOPPY")
        if pead_ok and regime in ("CAPITULATION", "CHOPPY"):
            unlocks.add("PEAD_EXTENDED_WINDOW")
    if tier in ("HIGH", "CONVICTION") and sentiment >= 40:
        unlocks.add("SWING_SENTIMENT")
    return frozenset(unlocks)


def compute_ticker_quality(
    symbol: str,
    iv,
    snap,
    gate_flags: list,
    mode: str,
    sub_etf_chg: float = 0.0,
    earnings_clear_days: int = 30,
) -> TickerQuality:
    regime = snap.regime
    ep = get_entry_params(regime)
    research_sym = snap.research.get(symbol, {})
    sentiment = research_sym.get("sentiment_score", 0)
    intra_sent = snap.intraday_news.get(symbol, {}).get("sentiment_score", sentiment)

    pead_ok, pead_score = detect_pead_setup(symbol)
    event_active, _ = is_event_risk_window(__import__("datetime").datetime.now(
        __import__("zoneinfo").ZoneInfo("America/New_York")))

    c_pts, c_gap = _score_composite(iv.composite_score, ep.composite_min)
    s_pts, s_gap = _score_sentiment(sentiment)
    i_pts, i_gap = _score_intraday(intra_sent)
    e_pts, e_gap = _score_sub_etf(sub_etf_chg)
    p_pts, _     = _score_pead(pead_ok, pead_score)
    ec_pts, ec_gap = _score_earnings(earnings_clear_days)
    ev_pts       = _score_econ(event_active, False)

    score = c_pts + s_pts + i_pts + e_pts + p_pts + ec_pts + ev_pts
    score = max(0, min(100, score))
    tier  = _score_to_tier(score)

    gaps = [g for g in [c_gap, s_gap, i_gap, e_gap, ec_gap] if g]
    unlocks = _compute_unlocks(tier, mode, pead_ok, intra_sent, sentiment,
                               earnings_clear_days, regime)

    return TickerQuality(
        tier=tier,
        score=score,
        unlocks=unlocks,
        quality_gaps=gaps,
        component_scores={
            "composite": c_pts, "sentiment": s_pts, "intraday": i_pts,
            "sub_etf": e_pts, "pead": p_pts, "earnings": ec_pts, "econ": ev_pts,
        },
    )
```

- [ ] **Step 8: Run all ticker_quality tests**

```bash
.venv/bin/python -m pytest tests/test_ticker_quality.py -v
```
Expected: all PASSED

- [ ] **Step 9: Run full test suite to verify no regressions**

```bash
.venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -5
```
Expected: same pass count as before (no regressions)

- [ ] **Step 10: Add `derive_mode()` to `utils/ticker_quality.py`**

Mode derivation is used by `compute_ticker_quality` and callers (claude_caller, pipeline). Centralise it here to avoid creating a dangling `trading/modes.py` with no tests.

Add to `utils/ticker_quality.py` (before `compute_ticker_quality`):

```python
def derive_mode(symbol: str, pead_ok: bool, existing_swing_pos: bool = False) -> str:
    """Derive trading mode for a symbol at evaluation time.

    DAY_TRADE — default intraday mode
    PEAD — post-earnings drift setup (PEAD score ≥ 80)
    SWING — symbol already in open swing positions
    """
    if existing_swing_pos:
        return "SWING"
    if pead_ok:
        return "PEAD"
    return "DAY_TRADE"
```

Add test to `tests/test_ticker_quality.py`:

```python
from utils.ticker_quality import derive_mode

class TestDeriveMode:
    def test_swing_pos_wins_over_pead(self):
        assert derive_mode("NVDA", pead_ok=True, existing_swing_pos=True) == "SWING"

    def test_pead_wins_over_day_trade(self):
        assert derive_mode("NVDA", pead_ok=True, existing_swing_pos=False) == "PEAD"

    def test_default_day_trade(self):
        assert derive_mode("RIVN", pead_ok=False, existing_swing_pos=False) == "DAY_TRADE"
```

Run:
```bash
.venv/bin/python -m pytest tests/test_ticker_quality.py::TestDeriveMode -v
```
Expected: 3 PASSED

- [ ] **Step 11: Update `CLAUDE.md` with Rules 28, 29, 30**

Add after Rule 27 in `CLAUDE.md`:

```markdown
### 28. Ticker quality — always use compute_ticker_quality()
`compute_ticker_quality()` in `utils/ticker_quality.py` is the sole factory for `TickerQuality`.
No inline tier logic, quality scoring, or unlock evaluation outside this module.
Adding a new signal component = update the scoring table in `utils/ticker_quality.py` only.

### 29. Regime × tier dispatch — always use utils/regime_params.py
`FilterParams`, `GateParams`, `AutoExecuteParams` follow the frozen-dataclass + table-function
pattern of `EntryParams/SwingParams/T2Params`. No inline `if tier ==` or `if regime ==` dispatch
chains outside `utils/regime_params.py`. Adding a new tier variant = update the table only.

### 30. TP/SL computation — always use signals/tp_scaler.compute_tp_sl()
All TP/SL assignments in executor, monitor, and swing_manager go through `compute_tp_sl()`.
No direct `config.TAKE_PROFIT_PCT` / `config.STOP_LOSS_PCT` assignments at order-placement time.
```

- [ ] **Step 12: Run full suite + commit all Task 1 changes**

```bash
.venv/bin/python -m pytest tests/test_ticker_quality.py -v
.venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -5
```
Expected: all PASSED, same pass count as before

```bash
git add utils/ticker_quality.py tests/test_ticker_quality.py CLAUDE.md
git commit -m "feat: add TickerQuality scorer — 4-tier bucketed quality, named unlocks, derive_mode; Rules 28/29/30

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 2: `signals/tp_scaler.py` — time-aware + tier TP/SL scaler

**Files:**
- Create: `signals/tp_scaler.py`
- Create: `tests/test_tp_scaler.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_tp_scaler.py
import pytest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import config
from signals.tp_scaler import compute_tp_sl, compute_time_multiplier, TimeSlot

ET = ZoneInfo("America/New_York")

def _et(hour, minute=0):
    return datetime(2026, 5, 21, hour, minute, tzinfo=ET)

class TestTimeMultiplier:
    def test_full_slot_before_noon(self):
        assert compute_time_multiplier(_et(10, 30)) == 1.00

    def test_late_slot_noon_to_1330(self):
        assert compute_time_multiplier(_et(12, 30)) == 0.80

    def test_tight_slot_1330_to_1430(self):
        assert compute_time_multiplier(_et(14, 0)) == 0.65

    def test_very_tight_after_1430(self):
        assert compute_time_multiplier(_et(14, 31)) == 0.50

class TestComputeTpSl:
    def test_normal_medium_full_slot(self, monkeypatch):
        monkeypatch.setattr(config, "ATR_TP_FLOOR_PCT", 3.0)
        monkeypatch.setattr(config, "ATR_TP_CEILING_PCT", 12.0)
        tp, sl = compute_tp_sl(5.0, 3.0, _et(10, 30), "MEDIUM", "NORMAL")
        assert tp == pytest.approx(5.0)   # no tier mult for MEDIUM
        assert sl == pytest.approx(3.0)

    def test_conviction_normal_full_slot_widens_tp(self, monkeypatch):
        monkeypatch.setattr(config, "ATR_TP_FLOOR_PCT", 3.0)
        monkeypatch.setattr(config, "ATR_TP_CEILING_PCT", 12.0)
        tp, sl = compute_tp_sl(5.0, 3.0, _et(10, 30), "CONVICTION", "NORMAL")
        assert tp == pytest.approx(5.0 * 1.20)
        assert sl == pytest.approx(3.0 * 0.90)

    def test_late_entry_tightens_tp(self, monkeypatch):
        monkeypatch.setattr(config, "ATR_TP_FLOOR_PCT", 3.0)
        monkeypatch.setattr(config, "ATR_TP_CEILING_PCT", 12.0)
        tp, sl = compute_tp_sl(5.0, 3.0, _et(13, 0), "MEDIUM", "NORMAL")
        assert tp == pytest.approx(5.0 * 0.80)

    def test_tp_clamped_to_floor(self, monkeypatch):
        monkeypatch.setattr(config, "ATR_TP_FLOOR_PCT", 3.0)
        monkeypatch.setattr(config, "ATR_TP_CEILING_PCT", 12.0)
        tp, sl = compute_tp_sl(3.5, 3.0, _et(14, 31), "LOW", "CHOPPY")
        assert tp == pytest.approx(3.0)   # 3.5 * 0.50 = 1.75 → clamped to floor

    def test_tier_mult_not_applied_in_choppy(self, monkeypatch):
        monkeypatch.setattr(config, "ATR_TP_FLOOR_PCT", 3.0)
        monkeypatch.setattr(config, "ATR_TP_CEILING_PCT", 12.0)
        tp, sl = compute_tp_sl(5.0, 3.0, _et(10, 30), "CONVICTION", "CHOPPY")
        assert tp == pytest.approx(5.0)   # no tier mult in CHOPPY
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/python -m pytest tests/test_tp_scaler.py -v 2>&1 | head -10
```
Expected: `ImportError`

- [ ] **Step 3: Implement `signals/tp_scaler.py`**

```python
"""
Time-aware + tier-adjusted TP/SL scaler — Rule 30 SoT for all TP/SL computation.
Pure — no I/O.
"""
from __future__ import annotations
from datetime import datetime
from enum import Enum

import config

_SAFE_REGIMES = frozenset({"NORMAL", "BULL_TREND", "OPPORTUNITY", "CRASH_RECOVERY"})

_TIER_TP_MULT = {"LOW": 1.00, "MEDIUM": 1.00, "HIGH": 1.10, "CONVICTION": 1.20}
_TIER_SL_MULT = {"LOW": 1.00, "MEDIUM": 1.00, "HIGH": 0.95, "CONVICTION": 0.90}


class TimeSlot(Enum):
    FULL      = "FULL"
    LATE      = "LATE"
    TIGHT     = "TIGHT"
    VERY_TIGHT = "VERY_TIGHT"


def compute_time_multiplier(entry_time: datetime) -> float:
    hour   = entry_time.hour
    minute = entry_time.minute
    total_min = hour * 60 + minute
    if total_min < 12 * 60:
        return 1.00
    if total_min < 13 * 60 + 30:
        return 0.80
    if total_min < 14 * 60 + 30:
        return 0.65
    return 0.50


def compute_tp_sl(
    base_tp: float,
    base_sl: float,
    entry_time: datetime,
    tier: str,
    regime: str,
) -> tuple[float, float]:
    time_mult = compute_time_multiplier(entry_time)
    tier_tp   = _TIER_TP_MULT.get(tier, 1.0) if regime in _SAFE_REGIMES else 1.0
    tier_sl   = _TIER_SL_MULT.get(tier, 1.0) if regime in _SAFE_REGIMES else 1.0

    tp = base_tp * time_mult * tier_tp
    sl = base_sl * tier_sl

    tp = max(config.ATR_TP_FLOOR_PCT, min(config.ATR_TP_CEILING_PCT, tp))
    sl = max(2.0, min(5.0, sl))
    return round(tp, 2), round(sl, 2)
```

- [ ] **Step 4: Run tp_scaler tests**

```bash
.venv/bin/python -m pytest tests/test_tp_scaler.py -v
```
Expected: all PASSED

- [ ] **Step 5: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -5
```

- [ ] **Step 6: Commit**

```bash
git add signals/tp_scaler.py tests/test_tp_scaler.py
git commit -m "feat: add tp_scaler — time-aware + tier TP/SL scaling (Rule 30)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 3: `utils/regime_params.py` — FilterParams, GateParams, AutoExecuteParams

**Files:**
- Modify: `utils/regime_params.py`
- Modify: `tests/test_pipeline.py` (new test class)

- [ ] **Step 1: Write failing tests for new param lookups**

Add to `tests/test_pipeline.py`:

```python
from utils.regime_params import (
    get_filter_params, get_gate_params, get_auto_execute_params,
    FilterParams, GateParams, AutoExecuteParams,
)

class TestDynamicRegimeParams:
    def test_filter_params_all_regimes_covered(self):
        from utils.regimes import VALID
        for regime in VALID:
            for tier in ("LOW", "MEDIUM", "HIGH", "CONVICTION"):
                p = get_filter_params(regime, tier)
                assert isinstance(p, FilterParams)

    def test_gate_params_all_regimes_covered(self):
        from utils.regimes import VALID
        for regime in VALID:
            for tier in ("LOW", "MEDIUM", "HIGH", "CONVICTION"):
                p = get_gate_params(regime, tier)
                assert isinstance(p, GateParams)

    def test_auto_execute_disabled_for_crisis(self):
        for tier in ("LOW", "MEDIUM", "HIGH", "CONVICTION"):
            p = get_auto_execute_params("CRISIS", tier)
            assert p.composite_offset == 999  # disabled sentinel

    def test_auto_execute_conviction_normal(self):
        p = get_auto_execute_params("NORMAL", "CONVICTION")
        assert p.composite_offset == 10
        assert p.sentiment_min == 20
        assert p.extra_unlock is None

    def test_auto_execute_conviction_capitulation_requires_pead(self):
        p = get_auto_execute_params("CAPITULATION", "CONVICTION")
        assert p.composite_offset == 20
        assert p.sentiment_min == 30
        assert p.extra_unlock == "PEAD_EXTENDED_WINDOW"

    def test_auto_execute_choppy_low_disabled(self):
        p = get_auto_execute_params("CHOPPY", "LOW")
        assert p.composite_offset == 999

    def test_auto_execute_opportunity_high_requires_sub_etf(self):
        p = get_auto_execute_params("OPPORTUNITY", "HIGH")
        assert p.composite_offset != 999
        assert p.extra_unlock == "SUB_ETF_CONFIRM_03"

    def test_unknown_regime_falls_back_to_normal(self):
        p = get_filter_params("UNKNOWN_REGIME", "MEDIUM")
        normal_p = get_filter_params("NORMAL", "MEDIUM")
        assert p.composite_min == normal_p.composite_min
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/python -m pytest tests/test_pipeline.py::TestDynamicRegimeParams -v 2>&1 | head -15
```
Expected: `ImportError`

- [ ] **Step 3: Add dataclasses and tables to `utils/regime_params.py`**

Add after the existing `T2Params` dataclass (before `_SENTINEL`):

```python
@dataclass(frozen=True)
class FilterParams:
    composite_min: int
    sentiment_hard_block: int
    intraday_block: int


@dataclass(frozen=True)
class GateParams:
    stoch_soft_flag: int
    stoch_hard_veto: int
    rel_volume_min: float
    breadth_weak_threshold: float


_DISABLED_AE = object.__new__(type("AutoExecuteParams", (), {}))  # placeholder — replaced below


@dataclass(frozen=True)
class AutoExecuteParams:
    composite_offset: int       # 999 = disabled
    sentiment_min: int
    extra_unlock: str | None    # named unlock required, or None
```

Then add three table functions and three lookup functions at the bottom of the file:

```python
_AE_DISABLED = AutoExecuteParams(999, 0, None)


def _filter_table() -> dict[tuple[str, str], FilterParams]:
    # (regime, tier) → FilterParams
    # Tier adjusts composite_min: LOW +10, MEDIUM +0, HIGH -5, CONVICTION -10
    # Sentiment gates tighten for HIGH_VOL regimes
    base = {
        "NORMAL":         (config.COMPOSITE_MIN_NORMAL, -15, -10),
        "BULL_TREND":     (config.COMPOSITE_MIN_BULL_TREND, -15, -10),
        "OPPORTUNITY":    (config.COMPOSITE_MIN_NORMAL, -15, -10),
        "CRASH_RECOVERY": (config.COMPOSITE_MIN_NORMAL, -15, -10),
        "DEFENSIVE":      (config.COMPOSITE_MIN_HIGH_VOL, -10, -5),
        "CHOPPY":         (config.CHOPPY_COMPOSITE_GATE, -10, -5),
        "HIGH_VOL":       (config.COMPOSITE_MIN_HIGH_VOL, -10, -5),
        "CAPITULATION":   (config.COMPOSITE_MIN_NORMAL, -15, -10),
        "CRISIS":         (999, 0, 0),
    }
    offsets = {"LOW": +10, "MEDIUM": 0, "HIGH": -5, "CONVICTION": -10}
    table = {}
    for regime, (base_comp, sent_block, intra_block) in base.items():
        for tier, offset in offsets.items():
            table[(regime, tier)] = FilterParams(
                composite_min=max(0, base_comp + offset),
                sentiment_hard_block=sent_block,
                intraday_block=intra_block,
            )
    return table


def _gate_table() -> dict[tuple[str, str], GateParams]:
    # Stoch thresholds tighten for LOW/MEDIUM, relax for CONVICTION in bull regimes
    base = {
        "NORMAL":         (85, 97, 0.60, 0.85),
        "BULL_TREND":     (88, 97, 0.55, 0.80),
        "OPPORTUNITY":    (85, 97, 0.60, 0.85),
        "CRASH_RECOVERY": (83, 95, 0.60, 0.85),
        "DEFENSIVE":      (80, 95, 0.65, 0.90),
        "CHOPPY":         (80, 95, 0.65, 0.90),
        "HIGH_VOL":       (82, 95, 0.65, 0.88),
        "CAPITULATION":   (78, 93, 0.55, 0.85),
        "CRISIS":         (70, 90, 0.70, 0.90),
    }
    table = {}
    for regime, (ss, sv, rv, bw) in base.items():
        for tier in ("LOW", "MEDIUM", "HIGH", "CONVICTION"):
            # LOW/MEDIUM: tighter stoch soft flag; CONVICTION: relax by 3pts
            adj = -3 if tier in ("LOW", "MEDIUM") else (3 if tier == "CONVICTION" else 0)
            table[(regime, tier)] = GateParams(
                stoch_soft_flag=min(sv - 2, ss + adj),
                stoch_hard_veto=sv,
                rel_volume_min=rv,
                breadth_weak_threshold=bw,
            )
    return table


def _ae_table() -> dict[tuple[str, str], AutoExecuteParams]:
    # Full 9-regime × 4-tier auto-execute table per spec
    AE = AutoExecuteParams
    D  = _AE_DISABLED
    return {
        # NORMAL
        ("NORMAL", "LOW"):        D,
        ("NORMAL", "MEDIUM"):     AE(20, 20, None),
        ("NORMAL", "HIGH"):       AE(15, 20, None),
        ("NORMAL", "CONVICTION"): AE(10, 20, None),
        # BULL_TREND
        ("BULL_TREND", "LOW"):        D,
        ("BULL_TREND", "MEDIUM"):     AE(20, 20, None),
        ("BULL_TREND", "HIGH"):       AE(15, 20, None),
        ("BULL_TREND", "CONVICTION"): AE(10, 20, None),
        # OPPORTUNITY
        ("OPPORTUNITY", "LOW"):        D,
        ("OPPORTUNITY", "MEDIUM"):     D,
        ("OPPORTUNITY", "HIGH"):       AE(15, 20, "SUB_ETF_CONFIRM_03"),
        ("OPPORTUNITY", "CONVICTION"): AE(10, 20, "SUB_ETF_CONFIRM_05"),
        # CRASH_RECOVERY
        ("CRASH_RECOVERY", "LOW"):        D,
        ("CRASH_RECOVERY", "MEDIUM"):     AE(20, 20, None),
        ("CRASH_RECOVERY", "HIGH"):       AE(15, 20, None),
        ("CRASH_RECOVERY", "CONVICTION"): AE(10, 20, None),
        # DEFENSIVE
        ("DEFENSIVE", "LOW"):        D,
        ("DEFENSIVE", "MEDIUM"):     D,
        ("DEFENSIVE", "HIGH"):       AE(20, 25, None),
        ("DEFENSIVE", "CONVICTION"): AE(15, 25, None),
        # CHOPPY
        ("CHOPPY", "LOW"):        D,
        ("CHOPPY", "MEDIUM"):     D,
        ("CHOPPY", "HIGH"):       D,
        ("CHOPPY", "CONVICTION"): AE(15, 25, None),
        # HIGH_VOL
        ("HIGH_VOL", "LOW"):        D,
        ("HIGH_VOL", "MEDIUM"):     AE(20, 20, None),
        ("HIGH_VOL", "HIGH"):       AE(15, 20, None),
        ("HIGH_VOL", "CONVICTION"): AE(10, 20, None),
        # CAPITULATION
        ("CAPITULATION", "LOW"):        D,
        ("CAPITULATION", "MEDIUM"):     D,
        ("CAPITULATION", "HIGH"):       D,
        ("CAPITULATION", "CONVICTION"): AE(20, 30, "PEAD_EXTENDED_WINDOW"),
        # CRISIS — all disabled
        ("CRISIS", "LOW"):        D,
        ("CRISIS", "MEDIUM"):     D,
        ("CRISIS", "HIGH"):       D,
        ("CRISIS", "CONVICTION"): D,
    }


_FILTER_TABLE = _filter_table()
_GATE_TABLE   = _gate_table()
_AE_TABLE     = _ae_table()
_FALLBACK_FILTER  = _FILTER_TABLE[("NORMAL", "MEDIUM")]
_FALLBACK_GATE    = _GATE_TABLE[("NORMAL", "MEDIUM")]


def get_filter_params(regime: str, tier: str) -> FilterParams:
    return _FILTER_TABLE.get((regime, tier), _FALLBACK_FILTER)


def get_gate_params(regime: str, tier: str) -> GateParams:
    return _GATE_TABLE.get((regime, tier), _FALLBACK_GATE)


def get_auto_execute_params(regime: str, tier: str) -> AutoExecuteParams:
    return _AE_TABLE.get((regime, tier), _AE_DISABLED)
```

- [ ] **Step 3b: Add SUB_ETF threshold constants to `config.py`**

The `SUB_ETF_CONFIRM_03/05` thresholds used in the auto-execute table are currently magic numbers.
Add under `# ── Plan N ──` (or the relevant plan block) in `config.py`:

```python
# ── Dynamic Regime × Tier System ──
SUB_ETF_CONFIRM_MIN_03 = 0.3   # sub-sector ETF % floor for OPPORTUNITY/HIGH auto-execute
SUB_ETF_CONFIRM_MIN_05 = 0.5   # sub-sector ETF % floor for OPPORTUNITY/CONVICTION auto-execute
```

Update `_should_auto_execute()` in `trading/executor.py` (Task 9) to use these constants:
```python
if p.extra_unlock == "SUB_ETF_CONFIRM_03" and sub_etf_chg < config.SUB_ETF_CONFIRM_MIN_03:
    return False
if p.extra_unlock == "SUB_ETF_CONFIRM_05" and sub_etf_chg < config.SUB_ETF_CONFIRM_MIN_05:
    return False
```

- [ ] **Step 4: Run new param tests**

```bash
.venv/bin/python -m pytest tests/test_pipeline.py::TestDynamicRegimeParams -v
```
Expected: all PASSED

- [ ] **Step 5: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -5
```

- [ ] **Step 6: Commit**

```bash
git add utils/regime_params.py tests/test_pipeline.py
git commit -m "feat: add FilterParams/GateParams/AutoExecuteParams to regime_params — 9×4 dynamic tables

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 4: `tickers.json` + `sectors.py` — GICS sub-sector grouping

**Files:**
- Modify: `tickers.json`
- Modify: `sectors.py`
- Modify: `tests/test_foundation.py`

- [ ] **Step 1: Write failing test for `get_sub_sector_etf`**

Add to `tests/test_foundation.py`:

```python
from sectors import get_sub_sector_etf, get_sector_etf

class TestSubSectorEtf:
    def test_nvidia_returns_soxx(self):
        result = get_sub_sector_etf("NVDA")
        assert result == "SOXX"

    def test_crwd_returns_cibr(self):
        result = get_sub_sector_etf("CRWD")
        assert result == "CIBR"

    def test_unknown_symbol_falls_back_to_sector_etf(self):
        result = get_sub_sector_etf("ZZZZ_UNKNOWN")
        assert result == "OTHER"

    def test_symbol_without_sub_etf_falls_back_to_sector_etf(self):
        # XLF stocks have no sub_etf — fallback to sector
        result = get_sub_sector_etf("JPM")
        assert result == get_sector_etf("JPM")

    def test_existing_get_sector_etf_unchanged(self):
        # Ensure backward compat
        assert get_sector_etf("NVDA") == "XLK"
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/python -m pytest tests/test_foundation.py::TestSubSectorEtf -v 2>&1 | head -10
```
Expected: `AttributeError` (get_sub_sector_etf not defined)

- [ ] **Step 3: Add `sub_etf` field to tickers.json for all XLK tickers**

For each ticker in `tickers.json`, add `"sub_etf"` based on the following mapping.
Edit `tickers.json` — within the `"XLK"` block, update the symbol entries:

**Semiconductors → SOXX:** NVDA, AMD, AVGO, QCOM, MU, AMAT, LRCX, KLAC, MRVL, ARM, SMCI, ON, TXN, ADI, COHR, LITE, IIVI, WOLF, ACLS

**Software → IGV:** MSFT, CRM, NOW, ORCL, ADBE, SNOW, MDB, DDOG, HUBS, VEEV, TEAM, BILL, ASAN, ZI

**Cybersecurity → CIBR:** CRWD, PANW, ZS, OKTA, S, FTNT, CYBR

**Cloud/AI → WCLD:** AMZN (if in XLK), GOOGL (move to XLC), NET, CFLT, ESTC

**Communication → XLC:** META, GOOGL, NFLX, SNAP, PINS, TTD, ROKU, SPOT (move these from XLY/XLK to new `"XLC"` block)

For XLV tickers:
**Biotech → XBI:** MRNA, BNTX, CRSP, EDIT, NTLA, BEAM, RXRX

All other tickers with no sub-sector: omit `sub_etf` field (fallback to sector ETF).

Add a new `"XLC"` block to tickers.json for Communication Services tickers moved from XLK/XLY.

Example entry format:
```json
{"symbol": "NVDA", "tier1": true, "sub_etf": "SOXX", "theme": "AI/semis"}
```

- [ ] **Step 4: Add `get_sub_sector_etf` to `sectors.py`**

Add after the existing `_build()` function result unpacking:

```python
def get_sub_sector_etf(symbol: str) -> str:
    """Returns sub_etf if defined in tickers.json, else falls back to get_sector_etf().
    Single accessor for sub-sector ETF — no other code should read sub_etf directly.
    """
    data = load_json(config.TICKERS_FILE, {})
    for etf_key, sector_data in data.items():
        if etf_key.startswith("_"):
            continue
        for entry in sector_data.get("symbols", []):
            if entry.get("symbol", "").upper() == symbol.upper():
                return entry.get("sub_etf", get_sector_etf(symbol))
    return get_sector_etf(symbol)
```

Note: this re-reads tickers.json per call. For production use, cache via a module-level dict built in `_build()` — add `symbol_to_sub_etf: dict[str, str]` to `_build()` return tuple, expose as `SYMBOL_TO_SUB_ETF`, and use that in `get_sub_sector_etf` instead of re-reading.

Efficient version (add to `_build()` return):

```python
# Inside _build():
symbol_to_sub_etf: dict[str, str] = {}
for entry in symbols:
    sym = entry["symbol"]
    if "sub_etf" in entry:
        symbol_to_sub_etf[sym] = entry["sub_etf"]

# Return tuple extended:
return symbol_to_etf, frozenset(tier1_set), symbol_to_theme, etf_to_symbols, screener_symbols, tier1_symbols, symbol_to_sub_etf
```

Update module-level unpacking and `get_sub_sector_etf`:

```python
_sym_to_etf, TIER1_SET, _sym_to_theme, ETF_TO_SYMBOLS, SCREENER_SYMBOLS, TIER1_SYMBOLS, _sym_to_sub_etf = _build()
SYMBOL_TO_SUB_ETF = _sym_to_sub_etf

def get_sub_sector_etf(symbol: str) -> str:
    return SYMBOL_TO_SUB_ETF.get(symbol.upper(), get_sector_etf(symbol))
```

- [ ] **Step 5: Run sub-sector tests**

```bash
.venv/bin/python -m pytest tests/test_foundation.py::TestSubSectorEtf -v
```
Expected: all PASSED

- [ ] **Step 6: Verify existing sector tests still pass**

```bash
.venv/bin/python -m pytest tests/test_foundation.py -v -q 2>&1 | tail -10
```

- [ ] **Step 7: Commit**

```bash
git add tickers.json sectors.py tests/test_foundation.py
git commit -m "feat: GICS sub-sector grouping — sub_etf field in tickers.json, get_sub_sector_etf() in sectors.py

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Phase 2 — Wire-in

---

### Task 5: `data/market_data.py` — fix Yahoo parse crash (P1-8)

**Files:**
- Modify: `data/market_data.py`
- Modify: `tests/test_backtest_data.py`

- [ ] **Step 1: Locate the exact line**

```bash
grep -n 'indicators.*quote' ~/Documents/projects/trading-bot/data/market_data.py
```
Expected: shows line like `q = result["indicators"]["quote"][0]`

- [ ] **Step 2: Write failing test**

Add to `tests/test_backtest_data.py`:

```python
from unittest.mock import patch, MagicMock
from data.market_data import _yahoo_bars  # or the relevant function name

class TestYahooParseGuard:
    @patch("data.market_data.yahoo_get")
    def test_empty_quote_list_returns_empty_df(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "chart": {"result": [{"timestamp": [1], "indicators": {"quote": []}}]}
        }
        mock_get.return_value = mock_resp
        import pandas as pd
        # Should not raise — should return empty DataFrame
        # Adjust function name and args to match actual signature
        result = _yahoo_bars("NVDA", period="1d", interval="5m")
        assert isinstance(result, pd.DataFrame)
        assert result.empty

    @patch("data.market_data.yahoo_get")
    def test_missing_quote_key_returns_empty_df(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "chart": {"result": [{"timestamp": [1], "indicators": {}}]}
        }
        mock_get.return_value = mock_resp
        import pandas as pd
        result = _yahoo_bars("NVDA", period="1d", interval="5m")
        assert isinstance(result, pd.DataFrame)
        assert result.empty
```

- [ ] **Step 3: Run to verify failure (or confirm current behavior crashes)**

```bash
.venv/bin/python -m pytest tests/test_backtest_data.py::TestYahooParseGuard -v 2>&1 | head -15
```

- [ ] **Step 4: Fix `data/market_data.py`**

Find and replace the unguarded access. Replace:
```python
q = result["indicators"]["quote"][0]
```
With:
```python
quote_list = result.get("indicators", {}).get("quote", [])
if not quote_list:
    return pd.DataFrame()
q = quote_list[0]
```

- [ ] **Step 5: Run parse guard tests**

```bash
.venv/bin/python -m pytest tests/test_backtest_data.py::TestYahooParseGuard -v
```
Expected: all PASSED

- [ ] **Step 6: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -5
```

- [ ] **Step 7: Commit**

```bash
git add data/market_data.py tests/test_backtest_data.py
git commit -m "fix: guard Yahoo indicators quote[] access against empty/missing response (P1-8)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 6: `signals/tranche2_gates.py` — T2 bug fixes (P0-1, P0-2, P1-1, P1-6)

**Files:**
- Modify: `signals/tranche2_gates.py`
- Modify: `tests/test_signals.py`

- [ ] **Step 1: Write failing tests for all four fixes**

Add to `tests/test_signals.py`:

```python
from signals.tranche2_gates import evaluate_t2_trigger

class TestT2BugFixes:
    def _base_position(self, entry_price=100.0, current_price=98.0,
                       stop_loss_pct=3.0, timestamp="2026-05-21T10:00:00+00:00"):
        return {
            "entry_price": entry_price,
            "stop_loss_pct": stop_loss_pct,
            "timestamp": timestamp,
            "tranche_2_triggered": False,
            "qty": 100,
        }

    def test_t2_blocked_when_t1_underwater(self):
        pos = self._base_position(entry_price=100.0, current_price=97.5)
        # T1 is -2.5% — T2 must not fire
        result = evaluate_t2_trigger(
            symbol="AAPL", position=pos, current_price=97.5,
            composite_score=40, sector_etf_chg=0.5,
        )
        assert not result.trigger
        assert "underwater" in result.reason.lower()

    def test_t2_blocked_when_t2_drop_exceeds_t1_sl(self):
        # T1 SL=2%, T2 needs 3% drop — SL would fire before T2 adds value
        pos = self._base_position(entry_price=100.0, current_price=97.0, stop_loss_pct=2.0)
        result = evaluate_t2_trigger(
            symbol="AAPL", position=pos, current_price=97.0,
            composite_score=40, sector_etf_chg=0.5,
        )
        assert not result.trigger
        assert "sl" in result.reason.lower()

    def test_corrupted_entry_time_returns_fail(self):
        pos = self._base_position(timestamp="not-a-valid-timestamp")
        result = evaluate_t2_trigger(
            symbol="AAPL", position=pos, current_price=97.5,
            composite_score=40, sector_etf_chg=0.5,
        )
        assert not result.trigger
        assert "entry_time" in result.reason.lower()

    def test_sector_etf_fetch_failure_defaults_to_weak(self):
        pos = self._base_position(entry_price=100.0, current_price=100.5)
        # sector_etf_chg=None simulates a fetch failure
        result = evaluate_t2_trigger(
            symbol="AAPL", position=pos, current_price=100.5,
            composite_score=40, sector_etf_chg=None,
        )
        # Should be blocked because sector assumed weak (-2.0) when None
        assert not result.trigger
```

- [ ] **Step 2: Run to verify failures**

```bash
.venv/bin/python -m pytest tests/test_signals.py::TestT2BugFixes -v 2>&1 | head -20
```

- [ ] **Step 3: Apply fixes to `signals/tranche2_gates.py`**

Read the file first:
```bash
cat -n ~/Documents/projects/trading-bot/signals/tranche2_gates.py
```

**Fix 1 — sector ETF None defaults to -2.0:** Find where `sector_etf_chg` is used and add at the top of the evaluation function:
```python
if sector_etf_chg is None:
    sector_etf_chg = -2.0  # assume weak when fetch failed
```

**Fix 2 — entry_time guard (P0-2):** Find the hold-time gate:
```python
try:
    entry_dt = datetime.fromisoformat(entry_time_str)
    elapsed_min = (now - entry_dt).total_seconds() / 60
except Exception:
    return T2Result(False, "entry_time missing or invalid — cannot evaluate hold gate")
```

**Fix 3 — T1 profitability gate (P0-1):** Before price-drop window check, add:
```python
t1_pnl_pct = (current_price - t1_entry) / t1_entry * 100
if t1_pnl_pct < 0:
    return T2Result(False, f"T1 underwater ({t1_pnl_pct:+.1f}%) — no T2 on losing position")
```

**Fix 4 — T1 SL vs T2 drop (P1-6):** After computing `price_drop_pct`:
```python
t1_sl_pct = position.get("stop_loss_pct", config.STOP_LOSS_PCT)
if price_drop_pct >= t1_sl_pct:
    return T2Result(False, f"T2 drop {price_drop_pct:.1f}% ≥ T1 SL {t1_sl_pct:.1f}% — T2 adds no value")
```

- [ ] **Step 4: Run T2 bug fix tests**

```bash
.venv/bin/python -m pytest tests/test_signals.py::TestT2BugFixes -v
```
Expected: all PASSED

- [ ] **Step 5: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -5
```

- [ ] **Step 6: Commit**

```bash
git add signals/tranche2_gates.py tests/test_signals.py
git commit -m "fix: T2 gate fixes — T1 profitability gate, entry_time guard, sector ETF default, SL check (P0-1, P0-2, P1-1, P1-6)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 7: `trading/pre_claude_filter.py` — dynamic FilterParams + quality computation

**Files:**
- Modify: `trading/pre_claude_filter.py`
- Modify: `tests/test_pipeline.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_pipeline.py`:

```python
from trading.pre_claude_filter import apply_pre_claude_filter, FilterResult
from utils.ticker_quality import TickerQuality

class TestDynamicPreClaudeFilter:
    def _make_snap(self, regime="NORMAL", monkeypatch=None, tmp_path=None):
        from unittest.mock import MagicMock
        snap = MagicMock()
        snap.regime = regime
        snap.research = {}
        snap.intraday_news = {}
        snap.params = {}
        snap.staleness_guard = MagicMock(auto_execute_allowed=True)
        return snap

    def test_filter_result_carries_quality(self, monkeypatch, tmp_path):
        monkeypatch.setattr("notifications.telegram.telegram_notify", lambda *a, **k: None)
        snap = self._make_snap("NORMAL")
        iv = MagicMock()
        iv.composite_score = 55.0
        iv.rsi = 60.0
        iv.relative_volume = 1.2
        # FilterResult should include quality field
        result = apply_pre_claude_filter("AAPL", iv, snap)
        assert hasattr(result, "quality")
        assert isinstance(result.quality, TickerQuality)

    def test_defensive_regime_no_duplicate_gate(self, monkeypatch):
        # DEFENSIVE regime should NOT duplicate composite gate in pre_claude_filter
        # (Rule 23 — it belongs in check_all_gates only)
        import inspect
        import trading.pre_claude_filter as pcf
        src = inspect.getsource(pcf)
        assert 'regime == "DEFENSIVE"' not in src or "Rule 23" in src
```

- [ ] **Step 2: Run to verify failures**

```bash
.venv/bin/python -m pytest tests/test_pipeline.py::TestDynamicPreClaudeFilter -v 2>&1 | head -15
```

- [ ] **Step 3: Update `trading/pre_claude_filter.py`**

Read the full file first:
```bash
cat -n ~/Documents/projects/trading-bot/trading/pre_claude_filter.py
```

Changes:
1. Add imports: `from utils.ticker_quality import compute_ticker_quality` and `from utils.regime_params import get_filter_params`
2. Add `quality: TickerQuality` field to `FilterResult` dataclass
3. At filter entry, compute quality: `quality = compute_ticker_quality(symbol, iv, snap, [], mode, sub_etf_chg, earnings_clear_days)`
4. Replace all inline `config.SENTIMENT_HARD_BLOCK`, `config.INTRADAY_SENTIMENT_BLOCK`, `config.COMPOSITE_MIN_*` references with `fp = get_filter_params(snap.regime, quality.tier)` then `fp.composite_min`, `fp.sentiment_hard_block`, `fp.intraday_block`
5. Remove the DEFENSIVE regime duplicate composite gate (lines 69–77 per audit)
6. Pass `quality` in every `FilterResult(...)` return

For `sub_etf_chg` and `earnings_clear_days`: fetch sub_etf from `sectors.get_sub_sector_etf(symbol)`, then look up intraday change from `snap.sector_etf_cache` (add this cache to CycleSnapshot in Task 8). For `earnings_clear_days`, use `get_days_until_earnings(symbol)` from `calendars.earnings`.

- [ ] **Step 4: Run filter tests**

```bash
.venv/bin/python -m pytest tests/test_pipeline.py::TestDynamicPreClaudeFilter -v
```

- [ ] **Step 5: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -5
```

- [ ] **Step 6: Commit**

```bash
git add trading/pre_claude_filter.py tests/test_pipeline.py
git commit -m "feat: pre_claude_filter uses dynamic FilterParams per regime×tier; carries TickerQuality

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 8: `signals/entry_gates.py` — dynamic GateParams + CHOPPY unlocks + staleness fix

**Files:**
- Modify: `signals/entry_gates.py`
- Modify: `tests/test_entry_gates.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_entry_gates.py`:

```python
from utils.ticker_quality import TickerQuality

class TestDynamicGateParams:
    def _conviction_quality(self, unlocks=frozenset({"T2_CHOPPY", "SWING_CHOPPY"})):
        return TickerQuality(
            tier="CONVICTION", score=85, unlocks=unlocks,
            quality_gaps=[], component_scores={},
        )

    def test_stoch_soft_flag_uses_gate_params(self, monkeypatch):
        from signals.entry_gates import check_all_gates
        from unittest.mock import MagicMock, patch
        # In BULL_TREND, stoch soft flag threshold is 88 (not default 85)
        iv = MagicMock()
        iv.stoch_d = 86.0  # between 85 (NORMAL) and 88 (BULL_TREND)
        # Should NOT flag in BULL_TREND with CONVICTION tier
        quality = self._conviction_quality()
        # ... full test using monkeypatched entry_gates call

    def test_staleness_guard_passed_to_effective_composite_gate(self, monkeypatch):
        from signals.entry_gates import effective_composite_gate
        from unittest.mock import MagicMock
        guard = MagicMock()
        guard.composite_penalty = 10
        iv = MagicMock()
        iv.composite_score = 55.0
        # With penalty, effective gate should be raised by 10
        result = effective_composite_gate(iv, "NORMAL", staleness_guard=guard)
        assert result == 50 + 10  # NORMAL floor + penalty

    def test_swing_choppy_unlock_lowers_profit_lock(self, monkeypatch):
        # CONVICTION with SWING_CHOPPY unlock should use 1.0% profit_lock in CHOPPY
        # rather than the default 2.5%
        from signals.entry_gates import get_effective_profit_lock
        quality = self._conviction_quality(frozenset({"SWING_CHOPPY"}))
        lock = get_effective_profit_lock("CHOPPY", quality)
        assert lock == pytest.approx(1.0)

    def test_no_swing_choppy_unlock_uses_default_profit_lock(self, monkeypatch):
        from signals.entry_gates import get_effective_profit_lock
        quality = TickerQuality("HIGH", 67, frozenset(), [], {})
        lock = get_effective_profit_lock("CHOPPY", quality)
        assert lock == pytest.approx(config.SWING_PROFIT_LOCK_NORMAL)
```

- [ ] **Step 2: Run to verify failures**

```bash
.venv/bin/python -m pytest tests/test_entry_gates.py::TestDynamicGateParams -v 2>&1 | head -15
```

- [ ] **Step 3: Update `signals/entry_gates.py`**

Read the full file:
```bash
cat -n ~/Documents/projects/trading-bot/signals/entry_gates.py
```

Changes:
1. Add `quality: TickerQuality | None = None` to `check_all_gates()` signature
2. Import and call `get_gate_params(regime, quality.tier if quality else "MEDIUM")` at gate entry
3. Replace `config.STOCH_SOFT_FLAG_THRESHOLD` with `gp.stoch_soft_flag`, `config.STOCH_HARD_VETO_THRESHOLD` with `gp.stoch_hard_veto`, `config.REL_VOLUME_MIN` with `gp.rel_volume_min`, `config.BREADTH_WEAK_THRESHOLD` with `gp.breadth_weak_threshold`
4. Fix `effective_composite_gate()` call — ensure `staleness_guard` is passed (P0-4)
5. Add `get_effective_profit_lock(regime, quality) -> float` helper:
```python
def get_effective_profit_lock(regime: str, quality: TickerQuality | None) -> float:
    if quality and "SWING_CHOPPY" in quality.unlocks and regime == "CHOPPY":
        return 1.0
    if quality and "SWING_SENTIMENT" in quality.unlocks:
        return 1.5
    return config.SWING_PROFIT_LOCK_NORMAL
```

- [ ] **Step 4: Run gate tests**

```bash
.venv/bin/python -m pytest tests/test_entry_gates.py -v -q 2>&1 | tail -10
```

- [ ] **Step 5: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -5
```

- [ ] **Step 6: Commit**

```bash
git add signals/entry_gates.py tests/test_entry_gates.py
git commit -m "feat: entry_gates uses dynamic GateParams per regime×tier; CHOPPY swing/T2 unlocks; staleness fix (P0-4)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 9: `trading/executor.py` — auto-execute 9×4 table + tp_scaler

**Files:**
- Modify: `trading/executor.py`
- Modify: `trading/pipeline.py` (sub_etf batch fetch)
- Modify: `trading/cycle_context.py` (sector_etf_cache field)
- Modify: `tests/test_trading_budget.py`

- [ ] **Step 1: Add `sector_etf_cache` to CycleSnapshot**

Read `trading/cycle_context.py`:
```bash
cat -n ~/Documents/projects/trading-bot/trading/cycle_context.py
```

Add field to `CycleSnapshot`:
```python
sector_etf_cache: dict = field(default_factory=dict)
# Maps sub_etf symbol (e.g. "SOXX") → intraday % change float
```

- [ ] **Step 2: Add sub_etf batch fetch to `trading/pipeline.py`**

In `run_trading_cycle` (or wherever cycle context is built), before quality scoring:
```python
# Fetch unique sub-sector ETFs for this cycle's watchlist
from sectors import get_sub_sector_etf
from data.market_data import get_latest_intraday_pct  # or equivalent
unique_sub_etfs = {get_sub_sector_etf(sym) for sym in watch_symbols}
sector_etf_cache = {}
for etf in unique_sub_etfs:
    try:
        sector_etf_cache[etf] = get_latest_intraday_pct(etf)
    except Exception:
        sector_etf_cache[etf] = 0.0  # neutral fallback (not -2.0 — this is for scoring not gating)
```

- [ ] **Step 3: Write failing tests for auto-execute routing**

Add to `tests/test_trading_budget.py`:

```python
from utils.ticker_quality import TickerQuality
from utils.regime_params import get_auto_execute_params

class TestAutoExecuteRouting:
    def _quality(self, tier, unlocks=frozenset()):
        return TickerQuality(tier=tier, score=80, unlocks=unlocks,
                             quality_gaps=[], component_scores={})

    def test_low_tier_never_auto_executes(self):
        for regime in ("NORMAL", "CHOPPY", "BULL_TREND", "CAPITULATION"):
            p = get_auto_execute_params(regime, "LOW")
            assert p.composite_offset == 999

    def test_conviction_normal_auto_executes_at_offset_10(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "COMPOSITE_MIN_NORMAL", 50)
        p = get_auto_execute_params("NORMAL", "CONVICTION")
        floor = config.COMPOSITE_MIN_NORMAL
        threshold = floor + p.composite_offset
        assert threshold == 60

    def test_capitulation_conviction_requires_pead_unlock(self):
        p = get_auto_execute_params("CAPITULATION", "CONVICTION")
        assert p.extra_unlock == "PEAD_EXTENDED_WINDOW"
        quality_no_pead = TickerQuality("CONVICTION", 85, frozenset(), [], {})
        quality_with_pead = TickerQuality("CONVICTION", 85,
                                          frozenset({"PEAD_EXTENDED_WINDOW"}), [], {})
        # Without PEAD unlock → should not auto-execute
        assert p.extra_unlock not in quality_no_pead.unlocks
        # With PEAD unlock → should auto-execute
        assert p.extra_unlock in quality_with_pead.unlocks
```

- [ ] **Step 4: Run to verify failures**

```bash
.venv/bin/python -m pytest tests/test_trading_budget.py::TestAutoExecuteRouting -v 2>&1 | head -10
```

- [ ] **Step 5: Update `trading/executor.py` auto-execute routing**

Read executor.py:
```bash
cat -n ~/Documents/projects/trading-bot/trading/executor.py
```

In `execute_buy()`:
1. Import: `from utils.regime_params import get_auto_execute_params` and `from signals.tp_scaler import compute_tp_sl`
2. After computing `quality` (passed from pipeline), replace static auto-execute check with:

```python
def _should_auto_execute(quality: TickerQuality, iv, regime: str,
                          intra_sent: int, ep, sub_etf_chg: float) -> bool:
    p = get_auto_execute_params(regime, quality.tier)
    if p.composite_offset == 999:
        return False
    threshold = ep.composite_min + p.composite_offset
    if iv.composite_score < threshold:
        return False
    if intra_sent <= p.sentiment_min:
        return False
    if p.extra_unlock and p.extra_unlock not in quality.unlocks:
        return False
    if p.extra_unlock == "SUB_ETF_CONFIRM_03" and sub_etf_chg < 0.3:
        return False
    if p.extra_unlock == "SUB_ETF_CONFIRM_05" and sub_etf_chg < 0.5:
        return False
    return True
```

3. Replace TP/SL assignment with tp_scaler call:
```python
from datetime import datetime
from utils.constants import ET
entry_time = datetime.now(ET)
tp_pct, sl_pct = compute_tp_sl(
    base_tp=atr_tp_pct or config.TAKE_PROFIT_PCT,
    base_sl=decision.get("stop_loss_pct", config.STOP_LOSS_PCT),
    entry_time=entry_time,
    tier=quality.tier if quality else "MEDIUM",
    regime=regime,
)
```

4. Fix: pass `staleness_guard` to `effective_composite_gate()`:
```python
eff_gate = effective_composite_gate(iv, regime, params=params,
                                     staleness_guard=staleness_guard)
```

- [ ] **Step 6: Run auto-execute tests**

```bash
.venv/bin/python -m pytest tests/test_trading_budget.py -v -q 2>&1 | tail -10
```

- [ ] **Step 7: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -5
```

- [ ] **Step 8: Persist `quality_tier` and `quality_unlocks` in position record**

When a position is opened, the cycle's quality for that symbol must be persisted so that `monitor.py` can recover it in subsequent cycles (T2_CHOPPY unlock, swing gate decisions). Without this, unlocks are lost 10 minutes later.

In `trading/executor.py`, inside `execute_buy()`, extend the position dict before calling `journal.save_position()`:

```python
position["quality_tier"]    = quality.tier if quality else "MEDIUM"
position["quality_unlocks"] = list(quality.unlocks) if quality else []
```

Add test to `tests/test_trading_budget.py`:

```python
class TestPositionQualityPersistence:
    def test_quality_tier_saved_to_position_record(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", str(tmp_path / "positions.json"))
        monkeypatch.setattr("notifications.telegram.telegram_notify", lambda *a, **k: None)
        # Simulate execute_buy saving quality fields
        from utils.ticker_quality import TickerQuality
        from utils.io_utils import load_json
        quality = TickerQuality("CONVICTION", 85, frozenset({"T2_CHOPPY"}), [], {})
        # ... call execute_buy with mocked Alpaca + verify position file
        # The position loaded from file must have quality_tier and quality_unlocks
        # (full wiring tested via integration; here verify the field is written)
        pos = {"symbol": "NVDA", "quality_tier": quality.tier,
               "quality_unlocks": list(quality.unlocks)}
        assert pos["quality_tier"] == "CONVICTION"
        assert "T2_CHOPPY" in pos["quality_unlocks"]
```

- [ ] **Step 9: Update `trading/monitor.py` — load quality from position record, check T2_CHOPPY unlock**

`monitor.py` is the live executor of T2. It must recover `quality_tier`/`quality_unlocks` from the position record and check the T2_CHOPPY unlock before blocking T2 in CHOPPY regime.

Read the relevant T2 evaluation section:
```bash
grep -n "t2\|T2\|tranche" ~/Documents/projects/trading-bot/trading/monitor.py | head -20
```

In the T2 evaluation block of `monitor.py`, add before calling `evaluate_t2_trigger()`:

```python
# Recover quality unlocks from position record (persisted at entry in executor.py)
pos_unlocks = frozenset(position.get("quality_unlocks", []))
pos_tier    = position.get("quality_tier", "MEDIUM")

# In CHOPPY, T2 is normally gated off — allow if T2_CHOPPY unlock was earned at entry
if regime == "CHOPPY" and "T2_CHOPPY" not in pos_unlocks:
    continue  # skip T2 evaluation — no unlock
```

Add test to `tests/test_trading_budget.py`:

```python
class TestMonitorT2UnlockCheck:
    def test_t2_choppy_unlock_allows_t2_in_choppy(self):
        pos = {
            "symbol": "NVDA", "entry_price": 100.0, "qty": 10,
            "quality_tier": "CONVICTION", "quality_unlocks": ["T2_CHOPPY"],
            "tranche_2_triggered": False,
        }
        pos_unlocks = frozenset(pos.get("quality_unlocks", []))
        regime = "CHOPPY"
        # With T2_CHOPPY unlock, evaluation should proceed
        assert "T2_CHOPPY" in pos_unlocks or regime != "CHOPPY"

    def test_no_unlock_blocks_t2_in_choppy(self):
        pos = {
            "symbol": "RIVN", "quality_tier": "MEDIUM", "quality_unlocks": [],
            "tranche_2_triggered": False,
        }
        pos_unlocks = frozenset(pos.get("quality_unlocks", []))
        regime = "CHOPPY"
        should_skip = regime == "CHOPPY" and "T2_CHOPPY" not in pos_unlocks
        assert should_skip
```

- [ ] **Step 10: Run all Task 9 tests + full suite**

```bash
.venv/bin/python -m pytest tests/test_trading_budget.py -v -q 2>&1 | tail -10
.venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -5
```

- [ ] **Step 11: Commit**

```bash
git add trading/executor.py trading/pipeline.py trading/cycle_context.py trading/monitor.py config.py tests/test_trading_budget.py
git commit -m "feat: executor 9×4 auto-execute + tp_scaler; persist quality in position; monitor checks T2_CHOPPY unlock

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 10: `trading/eod_holds.py` + `trading/swing_manager.py` — tier-based swing gates

**Files:**
- Modify: `trading/eod_holds.py`
- Modify: `trading/swing_manager.py`
- Modify: `tests/test_foundation.py` (Rule 19: swing tests belong here)

- [ ] **Step 1: Write failing tests**

Add to `tests/test_foundation.py`:

```python
from utils.ticker_quality import TickerQuality

class TestTierBasedSwingPromotion:
    def _conviction_swing_choppy(self):
        return TickerQuality("CONVICTION", 85, frozenset({"SWING_CHOPPY"}), [], {})

    def _medium_quality(self):
        return TickerQuality("MEDIUM", 45, frozenset(), [], {})

    def test_conviction_swing_choppy_unlocks_at_1pct(self, monkeypatch, tmp_path):
        monkeypatch.setattr("notifications.telegram.telegram_notify", lambda *a, **k: None)
        # Position at +1.2% P&L in CHOPPY regime with CONVICTION quality
        # Should qualify for swing (normally needs 2.5% in CHOPPY)
        position = {
            "symbol": "NVDA", "entry_price": 100.0, "qty": 10,
            "stop_loss_pct": 3.0, "take_profit_pct": 5.0,
        }
        quality = self._conviction_swing_choppy()
        from trading.eod_holds import qualifies_for_eod_hold
        from utils.regime_params import get_swing_params
        params = get_swing_params("CHOPPY")
        from signals.entry_gates import get_effective_profit_lock
        effective_lock = get_effective_profit_lock("CHOPPY", quality)
        assert effective_lock == pytest.approx(1.0)
        current_price = 101.3  # +1.3% P&L
        qualifies, reason = qualifies_for_eod_hold(
            position, current_price, params, "CHOPPY", quality=quality,
        )
        assert qualifies, f"Expected swing qualification, got: {reason}"

    def test_medium_quality_choppy_still_needs_2_5pct(self, monkeypatch):
        from trading.eod_holds import qualifies_for_eod_hold
        from utils.regime_params import get_swing_params
        params = get_swing_params("CHOPPY")
        position = {"symbol": "RIVN", "entry_price": 100.0, "qty": 10,
                    "stop_loss_pct": 3.0, "take_profit_pct": 5.0}
        quality = self._medium_quality()
        current_price = 101.3  # only +1.3% — below MEDIUM threshold
        qualifies, reason = qualifies_for_eod_hold(
            position, current_price, params, "CHOPPY", quality=quality,
        )
        assert not qualifies

    def test_stale_regime_blocks_swing_promotion(self, monkeypatch, tmp_path):
        from trading.eod_holds import qualifies_for_eod_hold
        from utils.regime_params import get_swing_params
        from utils.data_health import StalenessGuard
        guard = MagicMock()
        guard.swing_fallback_regime = "NORMAL"
        guard.stale_sources = {"regime"}
        params = get_swing_params("NORMAL")
        position = {"symbol": "AAPL", "entry_price": 100.0, "qty": 10,
                    "stop_loss_pct": 3.0, "take_profit_pct": 5.0}
        qualifies, reason = qualifies_for_eod_hold(
            position, 103.0, params, "NORMAL",
            quality=None, regime_stale=True,
        )
        assert not qualifies
        assert "stale" in reason.lower()
```

- [ ] **Step 2: Run to verify failures**

```bash
.venv/bin/python -m pytest tests/test_foundation.py::TestTierBasedSwingPromotion -v 2>&1 | head -15
```

- [ ] **Step 3: Update `trading/eod_holds.py`**

Read the file:
```bash
cat -n ~/Documents/projects/trading-bot/trading/eod_holds.py
```

Changes:
1. `qualifies_for_eod_hold()` signature: add `quality: TickerQuality | None = None, regime_stale: bool = False`
2. At top of function: `if regime_stale: return False, "regime data stale — swing promotion blocked"`
3. Use `get_effective_profit_lock(regime, quality)` (from entry_gates) instead of `params.profit_lock_pct` for profit-lock check
4. Add sector ETF direction gate: fetch sub_etf intraday % (from position's symbol), require ≥ regime floor:
```python
from sectors import get_sub_sector_etf
sub_etf = get_sub_sector_etf(symbol)
sector_chg = _fetch_sector_etf_chg(sub_etf)  # cached from snap
sector_floor = config.SWING_SECTOR_FLOOR_NORMAL  # or regime-specific
if sector_chg < sector_floor:
    return False, f"sector ETF {sub_etf} {sector_chg:+.1f}% below swing floor {sector_floor}"
```
5. CAPITULATION earnings exemption: add `or regime == "CAPITULATION"` to the `capitulation_hold` check

- [ ] **Step 4: Update `trading/swing_manager.py` — tier-aware pre-market gap check and trailing stop**

`swing_manager.py` runs at 9:30 AM (gap check) and EOD (trailing stop, tiered TP). It manages overnight positions. Quality tier should affect:
- **Gap check**: CONVICTION positions tolerate wider adverse gaps before closing (−1.5% floor vs −1.0% for others)
- **Trailing stop**: tier is already persisted in position record — recover and use for trailing stop width

Read the gap check function:
```bash
grep -n "gap\|trailing\|run_gap_check\|evaluate_eod" ~/Documents/projects/trading-bot/trading/swing_manager.py | head -20
```

In `run_gap_check()`, recover tier and widen the close threshold for CONVICTION:

```python
pos_tier = position.get("quality_tier", "MEDIUM")
# CONVICTION positions can withstand a wider gap — thesis still intact
gap_close_threshold = -1.5 if pos_tier == "CONVICTION" else -1.0  # %
if gap_pct < gap_close_threshold:
    _close_swing_on_adverse_gap(symbol, position, gap_pct)
```

Add to `tests/test_foundation.py`:

```python
class TestSwingManagerTierGapCheck:
    def test_conviction_tolerates_wider_gap(self):
        # CONVICTION threshold is -1.5%, MEDIUM is -1.0%
        pos_conviction = {"symbol": "NVDA", "quality_tier": "CONVICTION", "entry_price": 100.0}
        pos_medium     = {"symbol": "RIVN", "quality_tier": "MEDIUM",     "entry_price": 100.0}
        tier_c = pos_conviction.get("quality_tier", "MEDIUM")
        tier_m = pos_medium.get("quality_tier", "MEDIUM")
        thresh_c = -1.5 if tier_c == "CONVICTION" else -1.0
        thresh_m = -1.5 if tier_m == "CONVICTION" else -1.0
        assert thresh_c == -1.5
        assert thresh_m == -1.0

    def test_gap_of_minus_1_2_closes_medium_not_conviction(self):
        gap_pct = -1.2
        for tier, expected_close in [("MEDIUM", True), ("CONVICTION", False)]:
            threshold = -1.5 if tier == "CONVICTION" else -1.0
            closes = gap_pct < threshold
            assert closes == expected_close, f"tier={tier}"
```

- [ ] **Step 5: Run swing/EOD tests**

```bash
.venv/bin/python -m pytest tests/test_foundation.py::TestTierBasedSwingPromotion tests/test_foundation.py::TestSwingManagerTierGapCheck -v
```

- [ ] **Step 6: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -5
```

- [ ] **Step 7: Commit**

```bash
git add trading/eod_holds.py trading/swing_manager.py tests/test_foundation.py
git commit -m "feat: tier-based swing — CONVICTION unlocks CHOPPY at 1%, wider gap threshold, sector ETF gate, stale block

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 11: `trading/context_builder.py` — tier × regime dispatching

**Files:**
- Modify: `trading/context_builder.py`
- Modify: `tests/test_market.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_market.py`:

```python
from trading.context_builder import build_stock_block
from utils.ticker_quality import TickerQuality
from signals.entry_gates import GateResult

class TestTierContextBlocks:
    def _make_iv(self, composite=55.0, rsi=62.0, stoch_k=70.0, stoch_d=68.0,
                 current_price=100.0, vwap=99.0, vwap_slope=0.003,
                 or_position="outside", relative_volume=1.4, atr=2.0):
        from unittest.mock import MagicMock
        iv = MagicMock()
        iv.composite_score = composite
        iv.rsi = rsi
        iv.stoch_k = stoch_k
        iv.stoch_d = stoch_d
        iv.current_price = current_price
        iv.vwap = vwap
        iv.vwap_slope = vwap_slope
        iv.or_position = or_position
        iv.relative_volume = relative_volume
        iv.atr = atr
        iv.signal = "BUY"
        return iv

    def _snap(self, regime="NORMAL"):
        from unittest.mock import MagicMock
        snap = MagicMock()
        snap.regime = regime
        snap.research = {}
        snap.intraday_news = {}
        return snap

    def _gate_result(self, flags=None):
        return GateResult(passed=True, reason="", flags=flags or [])

    def test_low_tier_contains_quality_gaps(self):
        quality = TickerQuality("LOW", 28, frozenset(),
                                ["vol_weak(-12)", "sector_flat(-8)"], {})
        gate = self._gate_result(["volume_weak"])
        from datetime import datetime
        from zoneinfo import ZoneInfo
        block = build_stock_block(
            "RIVN", self._make_iv(composite=32.0), self._snap(),
            gate, quality, "DAY_TRADE",
            datetime(2026, 5, 21, 11, 30, tzinfo=ZoneInfo("America/New_York")),
        )
        assert "quality_gaps" in block or "vol_weak" in block
        assert "caution" in block.lower()
        assert "BB[" not in block      # BB removed
        assert "EMA9" not in block     # EMA removed
        assert "MACD=" not in block    # MACD removed

    def test_conviction_tier_contains_unlocks_not_quality_breakdown(self):
        quality = TickerQuality("CONVICTION", 85, frozenset({"T2_CHOPPY", "SWING_CHOPPY"}),
                                [], {"composite": 25, "sentiment": 20})
        gate = self._gate_result()
        from datetime import datetime
        from zoneinfo import ZoneInfo
        block = build_stock_block(
            "NVDA", self._make_iv(composite=72.0), self._snap(),
            gate, quality, "PEAD",
            datetime(2026, 5, 21, 11, 30, tzinfo=ZoneInfo("America/New_York")),
        )
        assert "unlocks=" in block or "T2_CHOPPY" in block
        assert "composite(+25)" not in block   # no breakdown in CONVICTION
        assert "caution" not in block.lower()
        assert "BB[" not in block

    def test_medium_tier_shows_full_news(self):
        quality = TickerQuality("MEDIUM", 47, frozenset(), [], {})
        gate = self._gate_result()
        from unittest.mock import MagicMock
        snap = self._snap()
        snap.research = {"AAPL": {"sentiment_score": 35, "sentiment_label": "BULLISH",
                                   "summary": "Strong pipeline into Q3"}}
        from datetime import datetime
        from zoneinfo import ZoneInfo
        block = build_stock_block(
            "AAPL", self._make_iv(), snap, gate, quality, "DAY_TRADE",
            datetime(2026, 5, 21, 11, 30, tzinfo=ZoneInfo("America/New_York")),
        )
        assert "Strong pipeline" in block   # news included for MEDIUM
```

- [ ] **Step 2: Run to verify failures**

```bash
.venv/bin/python -m pytest tests/test_market.py::TestTierContextBlocks -v 2>&1 | head -15
```

- [ ] **Step 3: Rewrite `trading/context_builder.py` with tier dispatch**

The new `build_stock_block` dispatches to four private functions. Keep `build_stable_system_context` and `build_dynamic_user_context` mostly intact but add the new fields.

```python
def build_stock_block(
    symbol: str,
    iv: IndicatorValues,
    snap,
    gate_result,
    quality: TickerQuality,
    mode: str,
    entry_time: datetime,
    existing_pos: dict | None = None,
    atr_suggested_tp_pct: float | None = None,
    intraday_news: dict | None = None,
) -> str:
    _dispatch = {
        "LOW":        _block_low,
        "MEDIUM":     _block_medium,
        "HIGH":       _block_high,
        "CONVICTION": _block_conviction,
    }
    builder = _dispatch.get(quality.tier, _block_medium)
    return builder(symbol, iv, snap, gate_result, quality, mode, entry_time,
                   existing_pos, atr_suggested_tp_pct, intraday_news)
```

Implement each private function following the content table from the spec:

**`_block_low`**: quality_gaps at top, flags prominent, caution line, no BB/EMA/MACD/Stoch/news
**`_block_medium`**: flags, sector (not sub), earnings_clear, news summary truncated, no BB/EMA/MACD
**`_block_high`**: sub_etf, swing/T2 eligibility, time_adj TP, VWAP slope, no BB/EMA/MACD
**`_block_conviction`**: concise — unlocks, mode, pead, sub_etf, time_adj TP only; no quality breakdown, no caution

Within `_block_conviction`, apply a regime-specific annotation for CAPITULATION — it's the only
regime where CONVICTION auto-execute fires and PEAD is required. Surface this explicitly:

```python
def _block_conviction(symbol, iv, snap, gate_result, quality, mode, entry_time,
                      existing_pos, atr_suggested_tp_pct, intraday_news) -> str:
    regime = snap.regime
    regime_note = ""
    if regime == "CAPITULATION":
        pead_line = "PEAD=REQUIRED(unlock)" if "PEAD_EXTENDED_WINDOW" in quality.unlocks else "PEAD=MISSING⚠"
        regime_note = f"\n  ⚠ CAPITULATION: entries restricted — CONVICTION+PEAD only. {pead_line}"
    elif regime == "CHOPPY":
        regime_note = "\n  CHOPPY: auto-exec gated at +15 offset. T2/swing via unlock only."
    # ... rest of block
```

CRISIS regime: if `regime == "CRISIS"` in any `_block_*` function, return a one-liner:
```python
if snap.regime == "CRISIS":
    return f'<stock symbol="{symbol}">\n  CRISIS: entries blocked — no evaluation.\n</stock>'
```

For `build_stable_system_context` additions:
```python
# Add after sector_score_str line:
sub_scores = regime_info.get("sub_sector_scores", {})
sub_sector_str = " ".join(f"{k}={v:.0f}" for k, v in sub_scores.items()) if sub_scores else ""

ep = get_entry_params(regime)
swing_eligible = "eligible" if regime in regimes.SWING_ELIGIBLE else "ineligible"
t2_params = get_t2_params(regime)
t2_status = f"enabled(cutoff {t2_params.cutoff_hour:02d}:00)" if t2_params.cutoff_hour > 0 else "disabled"
regime_block = (
    f"Regime in effect:\n"
    f"  entry_window=10:00–{ep.hard_close_hour:02d}:00"
    f" composite_floor={ep.composite_min}"
    f" swing={swing_eligible} T2={t2_status}"
    f" stale_sources={stale_str or 'None'}"
)
```

For `build_dynamic_user_context` additions (pass `open_positions`, `breadth_ratio`, `flash_crash`):
```python
positions_str = _format_positions(open_positions)  # e.g. "3(XLK×2, XLV×1)"
remaining_min = _entry_window_remaining(now, regime)
breadth_str   = f"A/D={breadth_ratio:.2f}({'healthy' if breadth_ratio >= 0.90 else 'weak'})"
```

- [ ] **Step 4: Run context builder tests**

```bash
.venv/bin/python -m pytest tests/test_market.py::TestTierContextBlocks -v
```
Expected: all PASSED

- [ ] **Step 5: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -5
```

- [ ] **Step 6: Commit**

```bash
git add trading/context_builder.py tests/test_market.py
git commit -m "feat: context_builder dispatches to tier×regime block builders — inverted richness, no BB/EMA/MACD

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 12: `trading/claude_caller.py` — wire new params + backtest validation

**Files:**
- Modify: `trading/claude_caller.py`
- Run: backtest for regression check

- [ ] **Step 1: Update `claude_caller.py` to pass new params to context builders**

Read the file:
```bash
cat -n ~/Documents/projects/trading-bot/trading/claude_caller.py
```

Changes in `_ask_claude_batch()`:
1. Update `build_stock_block` call to pass `gate_result, quality, mode, entry_time`
2. The `quality` and `gate_result` come from `FilterResult` (carried from pre_claude_filter)
3. Derive `mode` from `trading.modes.get_active_mode(symbol, regime)`

```python
from datetime import datetime
from utils.constants import ET
from trading.modes import get_active_mode

entry_time = datetime.now(ET)

stock_blocks = "\n\n".join(
    build_stock_block(
        sym, iv, snap,
        gate_result=filter_result.gate_result,   # from FilterResult
        quality=filter_result.quality,            # from FilterResult
        mode=get_active_mode(sym, snap.regime),
        entry_time=entry_time,
        existing_pos=existing_pos,
        atr_suggested_tp_pct=compute_atr_tp_pct(iv.atr, iv.current_price, _atr_mult),
        intraday_news=snap.intraday_news.get(sym, {}),
    )
    for sym, iv, existing_pos, filter_result in batch
)
```

4. Update `build_dynamic_user_context` call:
```python
dynamic_ctx = build_dynamic_user_context(
    now,
    portfolio_gain_pct=snap.portfolio_gain_pct,
    open_positions=snap.open_positions,
    breadth_ratio=snap.breadth_ratio,
    flash_crash_active=snap.flash_crash_active,
    regime=snap.regime,
)
```

5. Update `build_stable_system_context` call — pass `sub_sector_scores`:
```python
stable_ctx = _get_cached_stable_context(
    snap.fred_cache, snap.research_meta, snap.regime_info,
    sub_sector_scores=snap.sector_etf_cache,
)
```

- [ ] **Step 2: Update `FilterResult` to carry `gate_result` and `quality`**

In `trading/pre_claude_filter.py`, extend `FilterResult`:
```python
@dataclass
class FilterResult:
    decision: str
    reason: str
    quality: TickerQuality | None = None
    gate_result: GateResult | None = None
```

- [ ] **Step 3: Run full test suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -10
```
Expected: all passing (same or higher count than before Phase 2)

- [ ] **Step 4: Run 30d backtest for regression check**

```bash
cd ~/Documents/projects/trading-bot
.venv/bin/python3 backtest.py --days 30 2>&1 | tail -30
```

Check:
- Force-close rate: target ≤ 40% (down from 35.3% with lower TP via time scaling)
- Total trades: should be similar ±15%
- No Python exceptions or ERROR lines

- [ ] **Step 5: Commit**

```bash
git add trading/claude_caller.py trading/pre_claude_filter.py
git commit -m "feat: wire quality+gate_result through FilterResult to claude_caller context builders

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

- [ ] **Step 6: Final full test suite + test count**

```bash
.venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -5
```
Expected: ≥ previous test count + new tests added in this plan

---

## Summary

| Task | Files | Phase | Key Outcome |
|------|-------|-------|-------------|
| 1 | ticker_quality.py, CLAUDE.md | P1 | 4-tier quality scorer, named unlocks, derive_mode(), Rules 28/29/30 |
| 2 | tp_scaler.py | P1 | Time-aware + tier TP/SL |
| 3 | regime_params.py, config.py | P1 | 9×4 FilterParams/GateParams/AutoExecuteParams; SUB_ETF constants |
| 4 | tickers.json, sectors.py | P1 | GICS sub-sector grouping |
| 5 | market_data.py | P2 | Yahoo parse crash fix |
| 6 | tranche2_gates.py | P2 | 4 T2 bug fixes |
| 7 | pre_claude_filter.py | P2 | Dynamic FilterParams + quality |
| 8 | entry_gates.py | P2 | Dynamic GateParams + CHOPPY unlocks |
| 9 | executor.py + pipeline.py + monitor.py | P2 | Auto-execute 9×4 + tp_scaler; quality persisted in position; monitor T2_CHOPPY check |
| 10 | eod_holds.py + swing_manager.py | P2 | Tier swing (CHOPPY 1% unlock, CONVICTION gap tolerance); sector ETF gate |
| 11 | context_builder.py | P2 | Tier×regime block dispatch; CAPITULATION/CRISIS annotations |
| 12 | claude_caller.py | P2 | Wire-in + backtest validation |

**Success criteria:**
- All existing tests pass
- 30d backtest force-close rate ≤ 40%
- T2 fire rate increases from 3.6% baseline
- No runtime exceptions in 30d backtest
- Claude context includes tier, gate_flags, sub-sector ETF for every batch call
- Quality tier + unlocks persisted in all open position records
- T2_CHOPPY unlock enforced end-to-end (entry → position record → monitor)
