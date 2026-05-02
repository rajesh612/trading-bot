# Regime-Adaptive Strategy — Plan 1: Foundation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the 3-state `vix_regime.py` with a 9-state `macro_regime.py`, add an economic calendar event gate, and add an API budget governor — without changing any trading entry/exit logic yet.

**Architecture:** `macro_regime.py` fetches 8 Yahoo Finance signals + FRED cache to compute a continuous `macro_health_score` and classify one of 9 regimes, writing `macro_regime.json` (backward-compatible: still has `regime` key). `economic_calendar.py` scrapes/hardcodes today's high-impact events and exposes `is_event_risk_window()`. `api_budget.py` tracks daily Claude API spend in `api_budget.json` and enforces a dynamic $1.00/$1.50 ceiling.

**Tech Stack:** Python 3.11+, `requests`, `beautifulsoup4`, `zoneinfo`, `pytest`, existing `config.py` / `vix_regime.py` patterns.

**Spec:** `docs/superpowers/specs/2026-05-01-regime-adaptive-hybrid-strategy-design.md` (sections 3, 4, 5, 8, 13, 14, 17, 19)

---

## File Map

| Action | File | Responsibility |
|---|---|---|
| Modify | `config.py` | Add 20 new constants (file paths, thresholds, budget limits) |
| Create | `macro_regime.py` | Yahoo signals, health score, 9-state classifier, flash crash check, FRED cache |
| Create | `economic_calendar.py` | Recurring event schedule, ForexFactory scrape, event risk window |
| Create | `api_budget.py` | Daily token spend tracker, dynamic ceiling, record/check functions |
| Modify | `main.py` | Add 3 new jobs (calendar refresh 8:30, macro regime 8:55, FRED refresh 4:00); rename vix job |
| Create | `tests/test_foundation.py` | All unit tests for the 3 new modules |

> `vix_regime.py` stays on disk (it is still imported by `learning_agent.py` via `load_trading_params`). Only the main.py job is switched to `macro_regime.fetch_macro_regime()`.

---

## Task 1: config.py — Add New Constants

**Files:**
- Modify: `config.py`

- [ ] **Step 1: Add the constants block**

Open `config.py` and append after the last line (after `DAILY_REPORT_SENT_FILE`):

```python
# ── Macro regime (replaces VIX_REGIME_FILE for new code) ──────────────────
MACRO_REGIME_FILE        = BASE_DIR / "macro_regime.json"
FRED_CACHE_FILE          = BASE_DIR / "fred_cache.json"
ECONOMIC_CALENDAR_FILE   = BASE_DIR / "economic_calendar.json"
API_BUDGET_FILE          = BASE_DIR / "api_budget.json"
SWING_POSITIONS_FILE     = BASE_DIR / "swing_positions.json"

FRED_API_KEY             = os.getenv("FRED_API_KEY", "")

# Macro health score thresholds
MACRO_HEALTH_BULL_THRESHOLD =  20   # score > +20 → bullish zone
MACRO_HEALTH_BEAR_THRESHOLD = -20   # score < -20 → bearish zone

# Consecutive pattern detection
CONSECUTIVE_BULL_DAYS    = 3        # SPY green closes needed for BULL_TREND
CONSECUTIVE_BEAR_DAYS    = 3        # SPY red closes considered for DEFENSIVE

# Choppy detection
CHOPPY_MIN_SIGN_ALTS     = 3        # sign flips in last 5 SPY days
CHOPPY_MAX_NET_MOVE_PCT  = 0.5      # max abs 5-day SPY net move %

# API budget
API_BUDGET_NORMAL_CEILING   = 1.00  # $/day
API_BUDGET_ELEVATED_CEILING = 1.50  # $/day (high-profit or CRISIS sessions)
API_BUDGET_WARNING_PCT      = 0.80  # switch to rule-based at 80% of ceiling

# Regime-aware entry gates
CHOPPY_COMPOSITE_GATE    = 30       # composite score gate in CHOPPY regime
HERD_ENTRY_GATE          = 25       # composite gate when bot herding detected

# Bot herding
BOT_HERD_ALERT_SCORE     = 60       # raise entry gate above this
BOT_HERD_HIGH_SCORE      = 80       # apply choppy-like rules above this

# Squeeze
SQUEEZE_CONFIDENCE_GATE  = 70       # min confidence to override normal entry gate

# Swing / short-term
SWING_MAX_POSITIONS      = 2
SWING_MAX_DAYS           = 5
SHORT_TERM_MAX_POSITIONS = 2
SHORT_TERM_MAX_DAYS      = 21
```

- [ ] **Step 2: Verify import works**

```bash
cd /Users/rajeshkumar/Documents/projects/trading-bot
.venv/bin/python3 -c "import config; print(config.MACRO_REGIME_FILE)"
```

Expected output: `…/trading-bot/macro_regime.json`

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "feat: add config constants for macro regime, budget, swing"
```

---

## Task 2: macro_regime.py — Yahoo Signal Fetching

**Files:**
- Create: `macro_regime.py`
- Create: `tests/test_foundation.py`

- [ ] **Step 1: Create the test file with a failing test**

Create `tests/test_foundation.py`:

```python
"""
Unit tests for macro_regime, economic_calendar, api_budget.

Run: pytest tests/test_foundation.py -v -m unit
"""
import json
import tempfile
from datetime import datetime, date
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

ET = ZoneInfo("America/New_York")


# ══════════════════════════════════════════════════════════════════════
# macro_regime — signal fetching
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_yahoo_closes_returns_floats():
    """_yahoo_closes should return a non-empty list of positive floats."""
    mock_response = {
        "chart": {"result": [{"indicators": {"quote": [{"close": [100.0, 101.5, None, 103.2]}]}}]}
    }
    with patch("macro_regime.requests.get") as mock_get:
        mock_get.return_value.json.return_value = mock_response
        mock_get.return_value.raise_for_status = MagicMock()
        from macro_regime import _yahoo_closes
        result = _yahoo_closes("SPY", days=10)
    assert result == [100.0, 101.5, 103.2]   # None filtered out
    assert all(isinstance(v, float) for v in result)


@pytest.mark.unit
def test_fetch_yahoo_signals_returns_required_keys():
    """_fetch_yahoo_signals should return a dict with all 8 signal keys."""
    mock_closes = {
        "^VIX":     [20.0] * 95,
        "SPY":      [500.0] * 10,
        "CL=F":     [75.0] * 10,
        "GC=F":     [2000.0] * 10,
        "DX-Y.NYB": [103.0] * 10,
        "^TNX":     [4.3] * 10,
        "SOXX":     [200.0] * 10,
        "QQQ":      [450.0] * 10,
    }
    with patch("macro_regime._yahoo_closes", side_effect=lambda sym, **kw: mock_closes[sym]):
        from macro_regime import _fetch_yahoo_signals
        signals = _fetch_yahoo_signals()
    required = {"vix_ratio", "spy_5day_return", "wti_5day_change",
                "gold_5day_change", "dxy_5day_change", "tnx_1day_change",
                "soxx_5day_return", "qqq_5day_return",
                "vix_current", "spy_closes", "vix_closes"}
    assert required.issubset(signals.keys())
```

- [ ] **Step 2: Run test — expect ImportError (file doesn't exist yet)**

```bash
cd /Users/rajeshkumar/Documents/projects/trading-bot
.venv/bin/python3 -m pytest tests/test_foundation.py::test_yahoo_closes_returns_floats -v -m unit
```

Expected: `FAILED` with `ModuleNotFoundError: No module named 'macro_regime'`

- [ ] **Step 3: Create `macro_regime.py` with Yahoo fetching functions**

```python
"""
Macro regime classifier — 9-state replacement for vix_regime.py.

Called at 8:55 AM ET daily. Writes macro_regime.json (backward-compatible:
always contains a `regime` key that existing code reads).
"""
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

import config

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

# Yahoo ticker map
_TICKERS = {
    "vix":  "%5EVIX",
    "spy":  "SPY",
    "wti":  "CL=F",
    "gold": "GC=F",
    "dxy":  "DX-Y.NYB",
    "tnx":  "%5ETNX",
    "soxx": "SOXX",
    "qqq":  "QQQ",
}


def _yahoo_closes(symbol: str, days: int = 120) -> list[float]:
    """Fetch daily close prices from Yahoo Finance. Filters out None values."""
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?interval=1d&range={days}d"
    )
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    resp.raise_for_status()
    quotes = resp.json()["chart"]["result"][0]["indicators"]["quote"][0]
    return [c for c in quotes["close"] if c is not None]


def _pct_change(closes: list[float], n: int = 5) -> float:
    """Return n-day % change in closes. Returns 0.0 if insufficient data."""
    if len(closes) < n + 1:
        return 0.0
    return (closes[-1] - closes[-(n + 1)]) / closes[-(n + 1)] * 100


def _fetch_yahoo_signals() -> dict:
    """
    Fetch all 8 macro signals from Yahoo Finance.
    Returns a dict of computed signal values used for health score and regime.
    """
    vix_closes  = _yahoo_closes(_TICKERS["vix"],  days=120)
    spy_closes  = _yahoo_closes(_TICKERS["spy"],  days=10)
    wti_closes  = _yahoo_closes(_TICKERS["wti"],  days=10)
    gold_closes = _yahoo_closes(_TICKERS["gold"], days=10)
    dxy_closes  = _yahoo_closes(_TICKERS["dxy"],  days=10)
    tnx_closes  = _yahoo_closes(_TICKERS["tnx"],  days=10)
    soxx_closes = _yahoo_closes(_TICKERS["soxx"], days=10)
    qqq_closes  = _yahoo_closes(_TICKERS["qqq"],  days=10)

    vix_current = vix_closes[-1]
    lookback    = min(90, len(vix_closes) - 1)
    vix_90d_avg = sum(vix_closes[-lookback - 1: -1]) / lookback
    vix_ratio   = vix_current / vix_90d_avg if vix_90d_avg else 1.0

    # TNX is in percent (e.g. 4.30 = 4.30%), 1-day change in basis points
    tnx_1day_change = (
        (tnx_closes[-1] - tnx_closes[-2]) * 100  # bps
        if len(tnx_closes) >= 2 else 0.0
    )

    return {
        "vix_current":      vix_current,
        "vix_90d_avg":      vix_90d_avg,
        "vix_ratio":        vix_ratio,
        "spy_5day_return":  _pct_change(spy_closes, 5),
        "wti_5day_change":  _pct_change(wti_closes, 5),
        "gold_5day_change": _pct_change(gold_closes, 5),
        "dxy_5day_change":  _pct_change(dxy_closes, 5),
        "tnx_1day_change":  tnx_1day_change,
        "soxx_5day_return": _pct_change(soxx_closes, 5),
        "qqq_5day_return":  _pct_change(qqq_closes, 5),
        "vix_closes":       vix_closes,
        "spy_closes":       spy_closes,
    }
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
.venv/bin/python3 -m pytest tests/test_foundation.py -v -m unit -k "yahoo"
```

Expected: `2 passed`

---

## Task 3: macro_regime.py — Health Score Computation

**Files:**
- Modify: `macro_regime.py` (append functions)
- Modify: `tests/test_foundation.py` (append tests)

- [ ] **Step 1: Add failing tests**

Append to `tests/test_foundation.py`:

```python
@pytest.mark.unit
def test_score_signal_bullish():
    from macro_regime import _score_signal
    assert _score_signal(0.85, bullish=0.90, bearish=1.20, lower_is_bullish=True) == 10


@pytest.mark.unit
def test_score_signal_bearish():
    from macro_regime import _score_signal
    assert _score_signal(1.25, bullish=0.90, bearish=1.20, lower_is_bullish=True) == -10


@pytest.mark.unit
def test_score_signal_neutral():
    from macro_regime import _score_signal
    assert _score_signal(1.05, bullish=0.90, bearish=1.20, lower_is_bullish=True) == 0


@pytest.mark.unit
def test_compute_macro_health_score_bullish():
    """Fully bullish signals should produce a score well above +20."""
    from macro_regime import compute_macro_health_score
    signals = {
        "vix_ratio":        0.80,   # bullish: < 0.90
        "spy_5day_return":  2.0,    # bullish: > +1.5%
        "wti_5day_change":  1.0,    # bullish: ±2%
        "gold_5day_change": 0.5,    # bullish: flat
        "dxy_5day_change":  0.2,    # bullish: stable
        "tnx_1day_change":  2.0,    # bullish: < 10bps
        "soxx_5day_return": 3.0,    # bullish: > +2%
        "qqq_5day_return":  2.0,    # bullish: > +1.5%
    }
    score = compute_macro_health_score(signals, fred_modifier=0)
    assert score > 20


@pytest.mark.unit
def test_compute_macro_health_score_bearish():
    """Fully bearish signals should produce a score below -20."""
    from macro_regime import compute_macro_health_score
    signals = {
        "vix_ratio":        1.30,   # bearish: > 1.20
        "spy_5day_return":  -1.5,   # bearish: < -0.5%
        "wti_5day_change":  6.0,    # bearish: > +5%
        "gold_5day_change": 3.0,    # bearish: > +2%
        "dxy_5day_change":  2.5,    # bearish: sharp rise
        "tnx_1day_change":  12.0,   # bearish: > 10bps
        "soxx_5day_return": -2.0,   # bearish: < -1%
        "qqq_5day_return":  -1.5,   # bearish: < -1%
    }
    score = compute_macro_health_score(signals, fred_modifier=0)
    assert score < -20


@pytest.mark.unit
def test_compute_macro_health_score_bounds():
    """Score must stay in [-100, +100]."""
    from macro_regime import compute_macro_health_score
    signals = {k: 999.0 for k in
               ["vix_ratio", "spy_5day_return", "wti_5day_change",
                "gold_5day_change", "dxy_5day_change", "tnx_1day_change",
                "soxx_5day_return", "qqq_5day_return"]}
    score = compute_macro_health_score(signals, fred_modifier=0)
    assert -100 <= score <= 100
```

- [ ] **Step 2: Run tests — expect FAIL**

```bash
.venv/bin/python3 -m pytest tests/test_foundation.py -v -m unit -k "score"
```

Expected: `FAILED` with `ImportError: cannot import name '_score_signal'`

- [ ] **Step 3: Append scoring functions to `macro_regime.py`**

```python
# ── Signal scoring weights (must sum to 1.0) ──────────────────────────────
_SIGNAL_WEIGHTS = {
    "vix_ratio":        0.20,
    "spy_5day_return":  0.20,
    "wti_5day_change":  0.10,
    "gold_5day_change": 0.08,
    "dxy_5day_change":  0.08,
    "tnx_1day_change":  0.09,
    "soxx_5day_return": 0.15,
    "qqq_5day_return":  0.10,
}

# Each entry: (bullish_threshold, bearish_threshold, lower_is_bullish)
_SIGNAL_THRESHOLDS = {
    "vix_ratio":        (0.90,  1.20,  True),   # vix/90d avg
    "spy_5day_return":  (1.5,  -0.5,   False),  # % return
    "wti_5day_change":  (2.0,   5.0,   True),   # abs % change (stable = bullish)
    "gold_5day_change": (1.0,   2.0,   True),   # abs % change (stable = bullish)
    "dxy_5day_change":  (1.0,   2.0,   True),   # abs % change (stable = bullish)
    "tnx_1day_change":  (5.0,  10.0,   True),   # bps (low change = bullish)
    "soxx_5day_return": (2.0,  -1.0,   False),  # % return
    "qqq_5day_return":  (1.5,  -1.0,   False),  # % return
}


def _score_signal(value: float, bullish: float, bearish: float,
                  lower_is_bullish: bool) -> int:
    """Return -10 (bearish), 0 (neutral), or +10 (bullish) for one signal."""
    if lower_is_bullish:
        if value <= bullish:
            return 10
        if value >= bearish:
            return -10
        return 0
    else:
        if value >= bullish:
            return 10
        if value <= bearish:
            return -10
        return 0


def compute_macro_health_score(signals: dict, fred_modifier: int = 0) -> int:
    """
    Compute macro_health_score in [-100, +100].
    signals: dict from _fetch_yahoo_signals().
    fred_modifier: -5 (hiking cycle), 0 (neutral), +5 (cutting cycle).
    """
    raw = 0.0
    for key, weight in _SIGNAL_WEIGHTS.items():
        bullish, bearish, lower = _SIGNAL_THRESHOLDS[key]
        score = _score_signal(signals[key], bullish, bearish, lower)
        raw += score * weight  # score is ±10, so max raw = ±10.0
    # Scale to [-100, +100]
    scaled = int(raw * 10) + fred_modifier
    return max(-100, min(100, scaled))
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
.venv/bin/python3 -m pytest tests/test_foundation.py -v -m unit -k "score"
```

Expected: `5 passed`

---

## Task 4: macro_regime.py — Consecutive Day Tracking + Regime Classification

**Files:**
- Modify: `macro_regime.py` (append)
- Modify: `tests/test_foundation.py` (append)

- [ ] **Step 1: Add failing tests**

Append to `tests/test_foundation.py`:

```python
@pytest.mark.unit
def test_consecutive_green_days():
    from macro_regime import _count_consecutive_direction
    closes = [100.0, 101.0, 102.0, 103.0, 104.0]
    assert _count_consecutive_direction(closes, direction="green") == 4


@pytest.mark.unit
def test_consecutive_red_days():
    from macro_regime import _count_consecutive_direction
    closes = [105.0, 104.0, 103.0, 102.0, 101.0]
    assert _count_consecutive_direction(closes, direction="red") == 4


@pytest.mark.unit
def test_choppy_detection_true():
    from macro_regime import _is_choppy
    # alternating: up, down, up, down, up
    closes = [100.0, 101.0, 100.5, 101.2, 100.8, 101.0]
    assert _is_choppy(closes) is True


@pytest.mark.unit
def test_choppy_detection_false_for_trend():
    from macro_regime import _is_choppy
    closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
    assert _is_choppy(closes) is False


@pytest.mark.unit
@pytest.mark.parametrize("macro_score,consec_green,consec_red,vix,spy_5d,choppy,expected", [
    ( 47,  3, 0, 16.0, 2.4,  False, "BULL_TREND"),
    ( 10,  0, 0, 18.0, 0.5,  False, "NORMAL"),
    (  5,  0, 0, 18.0, 0.2,  True,  "CHOPPY"),
    (-25,  0, 3, 22.0, -2.0, False, "DEFENSIVE"),
    ( 40,  0, 3, 22.0, -2.0, False, "OPPORTUNITY"),   # DEFENSIVE + AI/semi sector > +15
    ( 10,  0, 0, 36.0, -3.0, False, "CRISIS"),        # VIX > 35
    (-10,  0, 5, 25.0, -12.0,False, "CRISIS"),        # 5-day SPY < -10%
])
def test_classify_regime(macro_score, consec_green, consec_red, vix, spy_5d, choppy, expected):
    from macro_regime import classify_regime
    regime = classify_regime(
        macro_health_score=macro_score,
        consecutive_green_days=consec_green,
        consecutive_red_days=consec_red,
        vix_current=vix,
        spy_5day_return=spy_5d,
        is_choppy=choppy,
        ai_semi_sector_score=20 if expected == "OPPORTUNITY" else 5,
        bot_net_pnl_pct=-1.5 if consec_red >= 3 else 0.0,
        post_crisis_recovery=False,
    )
    assert regime == expected
```

- [ ] **Step 2: Run tests — expect FAIL**

```bash
.venv/bin/python3 -m pytest tests/test_foundation.py -v -m unit -k "consecutive or choppy or classify"
```

Expected: `FAILED` with `ImportError`

- [ ] **Step 3: Append pattern detection + classifier to `macro_regime.py`**

```python
def _count_consecutive_direction(closes: list[float], direction: str) -> int:
    """
    Count consecutive green (up) or red (down) closes at the end of the list.
    'direction' is "green" or "red".
    """
    count = 0
    for i in range(len(closes) - 1, 0, -1):
        if direction == "green" and closes[i] > closes[i - 1]:
            count += 1
        elif direction == "red" and closes[i] < closes[i - 1]:
            count += 1
        else:
            break
    return count


def _is_choppy(closes: list[float]) -> bool:
    """
    Return True if last 5 closes show >= 3 direction alternations
    AND abs(net 5-day move) < CHOPPY_MAX_NET_MOVE_PCT.
    """
    if len(closes) < 6:
        return False
    tail = closes[-6:]
    sign_changes = sum(
        1 for i in range(1, len(tail) - 1)
        if (tail[i] - tail[i - 1]) * (tail[i + 1] - tail[i]) < 0
    )
    net_move_pct = abs((tail[-1] - tail[0]) / tail[0] * 100) if tail[0] else 0.0
    return (sign_changes >= config.CHOPPY_MIN_SIGN_ALTS
            and net_move_pct < config.CHOPPY_MAX_NET_MOVE_PCT)


def _compute_sector_scores(signals: dict) -> dict:
    """
    Approximate per-sector health scores from available ETF signals.
    AI_SEMI ≈ SOXX; BROAD_TECH ≈ QQQ; CYBERSECURITY estimated from QQQ with discount.
    """
    soxx = signals.get("soxx_5day_return", 0.0)
    qqq  = signals.get("qqq_5day_return",  0.0)
    ai_semi       = int(max(-100, min(100, soxx * 10)))
    broad_tech    = int(max(-100, min(100, qqq  * 10)))
    cybersecurity = int(max(-100, min(100, qqq  *  8)))   # discount vs broad tech
    return {
        "AI_SEMI":       ai_semi,
        "BROAD_TECH":    broad_tech,
        "CYBERSECURITY": cybersecurity,
    }


def classify_regime(
    macro_health_score: int,
    consecutive_green_days: int,
    consecutive_red_days: int,
    vix_current: float,
    spy_5day_return: float,
    is_choppy: bool,
    ai_semi_sector_score: int,
    bot_net_pnl_pct: float,
    post_crisis_recovery: bool,
) -> str:
    """
    Classify market into one of 9 regime states.
    Priority order: CRISIS > CRASH_RECOVERY > BULL_TREND_PARABOLIC >
                    BULL_TREND > CAPITULATION > OPPORTUNITY > DEFENSIVE >
                    CHOPPY > NORMAL
    """
    # 1. CRISIS: hard overrides
    if vix_current > 35 or spy_5day_return < -10.0:
        return "CRISIS"

    # 2. CRASH_RECOVERY: post-crisis stabilisation
    if post_crisis_recovery and consecutive_green_days >= 2:
        return "CRASH_RECOVERY"

    # 3. BULL_TREND / BULL_TREND_PARABOLIC (parabolic detected separately by indicator_engine)
    if (consecutive_green_days >= config.CONSECUTIVE_BULL_DAYS
            and macro_health_score > config.MACRO_HEALTH_BULL_THRESHOLD):
        return "BULL_TREND"

    # 4. CHOPPY
    if is_choppy:
        return "CHOPPY"

    # 5. DEFENSIVE / OPPORTUNITY (red-day + bot pain)
    defensive = (consecutive_red_days >= config.CONSECUTIVE_BEAR_DAYS
                 and bot_net_pnl_pct < -1.0)
    if defensive:
        # OPPORTUNITY if AI/semi still healthy despite broad market weakness
        if ai_semi_sector_score > 15:
            return "OPPORTUNITY"
        return "DEFENSIVE"

    # 6. NORMAL
    if config.MACRO_HEALTH_BEAR_THRESHOLD <= macro_health_score <= config.MACRO_HEALTH_BULL_THRESHOLD:
        return "NORMAL"

    # 7. Scored but outside neutral band
    return "NORMAL"
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
.venv/bin/python3 -m pytest tests/test_foundation.py -v -m unit -k "consecutive or choppy or classify"
```

Expected: `10 passed`

---

## Task 5: macro_regime.py — Entry Point, Load, Flash Crash, FRED

**Files:**
- Modify: `macro_regime.py` (append)
- Modify: `tests/test_foundation.py` (append)

- [ ] **Step 1: Add failing tests**

Append to `tests/test_foundation.py`:

```python
@pytest.mark.unit
def test_check_flash_crash_true():
    from macro_regime import check_flash_crash
    assert check_flash_crash(spy_current=486.0, spy_prev_close=500.0) is True   # -2.8%


@pytest.mark.unit
def test_check_flash_crash_false():
    from macro_regime import check_flash_crash
    assert check_flash_crash(spy_current=499.0, spy_prev_close=500.0) is False  # -0.2%


@pytest.mark.unit
def test_fetch_macro_regime_writes_file(tmp_path):
    """fetch_macro_regime should write macro_regime.json with required keys."""
    import macro_regime as mr
    fake_signals = {
        "vix_current": 18.5, "vix_90d_avg": 20.0, "vix_ratio": 0.925,
        "spy_5day_return": 1.8, "wti_5day_change": 0.5, "gold_5day_change": 0.3,
        "dxy_5day_change": 0.1, "tnx_1day_change": 3.0,
        "soxx_5day_return": 2.5, "qqq_5day_return": 1.8,
        "vix_closes": [20.0] * 95 + [18.5],
        "spy_closes": [498.0, 499.0, 500.0, 501.5, 503.0, 504.8],
    }
    with (
        patch("macro_regime._fetch_yahoo_signals", return_value=fake_signals),
        patch("macro_regime._load_fred_modifier", return_value=0),
        patch("macro_regime._load_bot_net_pnl", return_value=0.0),
        patch("macro_regime._load_post_crisis_flag", return_value=False),
        patch.object(mr.config, "MACRO_REGIME_FILE", tmp_path / "macro_regime.json"),
    ):
        result = mr.fetch_macro_regime()
    assert "regime" in result
    assert "macro_health_score" in result
    assert result["regime"] in {
        "BULL_TREND", "BULL_TREND_PARABOLIC", "NORMAL", "CHOPPY",
        "DEFENSIVE", "OPPORTUNITY", "CAPITULATION", "CRISIS", "CRASH_RECOVERY"
    }
    assert (tmp_path / "macro_regime.json").exists()


@pytest.mark.unit
def test_load_macro_regime_defaults_to_normal(tmp_path):
    """load_macro_regime should return NORMAL if file missing."""
    import macro_regime as mr
    with patch.object(mr.config, "MACRO_REGIME_FILE", tmp_path / "missing.json"):
        result = mr.load_macro_regime()
    assert result["regime"] == "NORMAL"
```

- [ ] **Step 2: Run tests — expect FAIL**

```bash
.venv/bin/python3 -m pytest tests/test_foundation.py -v -m unit -k "flash_crash or macro_regime_writes or load_macro"
```

Expected: `FAILED` with `ImportError`

- [ ] **Step 3: Append remaining functions to `macro_regime.py`**

```python
def check_flash_crash(spy_current: float, spy_prev_close: float) -> bool:
    """Return True if SPY has dropped > 2.5% intraday from previous close."""
    if spy_prev_close <= 0:
        return False
    return (spy_current - spy_prev_close) / spy_prev_close * 100 <= -2.5


def _load_fred_modifier() -> int:
    """Load FRED modifier (-5, 0, +5) from fred_cache.json. Returns 0 if unavailable."""
    try:
        data = json.loads(config.FRED_CACHE_FILE.read_text())
        return int(data.get("fred_modifier", 0))
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return 0


def _load_bot_net_pnl() -> float:
    """Return today's bot net P&L % from trade_journal.json. Returns 0.0 if unavailable."""
    try:
        trades = json.loads(config.TRADE_JOURNAL_FILE.read_text())
        today = datetime.now(ET).date().isoformat()
        todays = [t for t in trades if t.get("timestamp", "").startswith(today)
                  and t.get("type") == "CLOSE"]
        if not todays:
            return 0.0
        total_pnl = sum(t.get("pnl_dollar", 0.0) for t in todays)
        portfolio = 100_000.0  # fallback; real value from Alpaca account
        try:
            from alpaca_client import AlpacaClient
            acct = AlpacaClient().get_account()
            portfolio = float(acct.portfolio_value)
        except Exception:
            pass
        return total_pnl / portfolio * 100 if portfolio else 0.0
    except (FileNotFoundError, json.JSONDecodeError):
        return 0.0


def _load_post_crisis_flag() -> bool:
    """
    Return True if yesterday's regime was CRISIS and VIX is now declining.
    Persisted in macro_regime.json across days.
    """
    try:
        prev = json.loads(config.MACRO_REGIME_FILE.read_text())
        return prev.get("regime") == "CRISIS"
    except (FileNotFoundError, json.JSONDecodeError):
        return False


def fetch_macro_regime() -> dict:
    """
    Main entry point. Fetches all signals, classifies 9-state regime,
    writes macro_regime.json. Called at 8:55 AM ET by main.py.
    Returns the regime dict.
    """
    try:
        signals      = _fetch_yahoo_signals()
        fred_mod     = _load_fred_modifier()
        bot_pnl      = _load_bot_net_pnl()
        post_crisis  = _load_post_crisis_flag()

        health_score = compute_macro_health_score(signals, fred_modifier=fred_mod)
        spy_closes   = signals["spy_closes"]
        cons_green   = _count_consecutive_direction(spy_closes, "green")
        cons_red     = _count_consecutive_direction(spy_closes, "red")
        choppy       = _is_choppy(spy_closes)
        sector_scores = _compute_sector_scores(signals)

        regime = classify_regime(
            macro_health_score=health_score,
            consecutive_green_days=cons_green,
            consecutive_red_days=cons_red,
            vix_current=signals["vix_current"],
            spy_5day_return=signals["spy_5day_return"],
            is_choppy=choppy,
            ai_semi_sector_score=sector_scores["AI_SEMI"],
            bot_net_pnl_pct=bot_pnl,
            post_crisis_recovery=post_crisis,
        )

        # Sign alternation count for choppy metadata
        spy_tail = spy_closes[-6:]
        sign_alts = sum(
            1 for i in range(1, len(spy_tail) - 1)
            if (spy_tail[i] - spy_tail[i - 1]) * (spy_tail[i + 1] - spy_tail[i]) < 0
        ) if len(spy_tail) >= 3 else 0

        result = {
            "regime":                  regime,
            "macro_health_score":      health_score,
            "sector_scores":           sector_scores,
            "consecutive_green_days":  cons_green,
            "consecutive_red_days":    cons_red,
            "spy_5day_sign_changes":   sign_alts,
            "vix_current":             round(signals["vix_current"], 2),
            "vix_90d_avg":             round(signals["vix_90d_avg"], 2),
            "vix_trend":               "rising" if signals.get("vix_ratio", 1) > 1 else "declining",
            "spy_5day_return":         round(signals["spy_5day_return"], 2),
            "flash_crash_active":      False,
            "event_risk_today":        False,
            "event_risk_details":      "",
            "capitulation_candidates": [],
            "timestamp":               datetime.now(ET).isoformat(),
            "reason": (
                f"{cons_green} consecutive green / {cons_red} red | "
                f"health={health_score} | VIX={signals['vix_current']:.1f}"
            ),
        }

    except Exception as exc:
        logger.warning(f"macro_regime fetch failed: {exc} — defaulting to NORMAL")
        result = {
            "regime": "NORMAL", "macro_health_score": 0,
            "sector_scores": {"AI_SEMI": 0, "BROAD_TECH": 0, "CYBERSECURITY": 0},
            "consecutive_green_days": 0, "consecutive_red_days": 0,
            "spy_5day_sign_changes": 0, "vix_current": 0.0, "vix_90d_avg": 0.0,
            "vix_trend": "unknown", "spy_5day_return": 0.0,
            "flash_crash_active": False, "event_risk_today": False,
            "event_risk_details": "", "capitulation_candidates": [],
            "timestamp": datetime.now(ET).isoformat(), "reason": f"fetch failed: {exc}",
        }

    config.MACRO_REGIME_FILE.write_text(json.dumps(result, indent=2))
    logger.info(
        f"MACRO REGIME: {result['regime']} | health={result['macro_health_score']} | "
        f"VIX={result['vix_current']:.1f} | SPY 5d={result['spy_5day_return']:+.2f}%"
    )
    return result


def load_macro_regime() -> dict:
    """Return today's cached regime. Defaults to NORMAL if file missing."""
    try:
        return json.loads(config.MACRO_REGIME_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "regime": "NORMAL", "macro_health_score": 0,
            "sector_scores": {"AI_SEMI": 0, "BROAD_TECH": 0, "CYBERSECURITY": 0},
            "flash_crash_active": False, "event_risk_today": False,
        }


def refresh_fred_cache() -> None:
    """
    Fetch FRED macro data (Fed funds rate, CPI, unemployment) and compute
    fred_modifier (-5, 0, +5). Called at 4:00 PM ET by main.py.
    Requires FRED_API_KEY in .env; skips gracefully if not set.
    """
    if not config.FRED_API_KEY:
        logger.info("FRED: API key not set — skipping FRED cache refresh.")
        return

    def _fred_obs(series_id: str, limit: int = 3) -> list[float]:
        url = (
            f"https://api.stlouisfed.org/fred/series/observations"
            f"?series_id={series_id}&api_key={config.FRED_API_KEY}"
            f"&limit={limit}&sort_order=desc&file_type=json"
        )
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        obs = resp.json().get("observations", [])
        return [float(o["value"]) for o in obs if o["value"] != "."]

    try:
        fed_rates     = _fred_obs("DFF",      limit=3)   # daily fed funds rate
        cpi_readings  = _fred_obs("CPIAUCSL", limit=3)   # monthly CPI level
        unemp_readings = _fred_obs("UNRATE",  limit=3)   # monthly unemployment

        # Determine direction for each
        fed_direction = (
            "cutting" if len(fed_rates) >= 2 and fed_rates[0] < fed_rates[-1]
            else "hiking" if len(fed_rates) >= 2 and fed_rates[0] > fed_rates[-1]
            else "paused"
        )
        cpi_trend = (
            "declining" if len(cpi_readings) >= 2 and cpi_readings[0] < cpi_readings[-1]
            else "rising"
        )
        unemp_rising = (
            len(unemp_readings) >= 2
            and (unemp_readings[0] - unemp_readings[-1]) > 0.3
        )

        # Fred modifier: cutting cycle → +5, hiking cycle → -5, else 0
        fred_modifier = 5 if fed_direction == "cutting" else (-5 if fed_direction == "hiking" else 0)
        if unemp_rising:
            fred_modifier -= 3  # rising unemployment is a drag

        data = {
            "fed_funds_rate":    fed_rates[0] if fed_rates else None,
            "fed_direction":     fed_direction,
            "cpi_trend":         cpi_trend,
            "unemployment_rising": unemp_rising,
            "fred_modifier":     fred_modifier,
            "updated_at":        datetime.now(ET).isoformat(),
        }
        config.FRED_CACHE_FILE.write_text(json.dumps(data, indent=2))
        logger.info(f"FRED cache refreshed: fed={fed_direction}, cpi={cpi_trend}, modifier={fred_modifier:+d}")

    except Exception as exc:
        logger.warning(f"FRED cache refresh failed: {exc}")


if __name__ == "__main__":
    import logging as _l
    _l.basicConfig(level=_l.INFO)
    fetch_macro_regime()
```

- [ ] **Step 4: Run all macro_regime tests**

```bash
.venv/bin/python3 -m pytest tests/test_foundation.py -v -m unit -k "macro"
```

Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
git add macro_regime.py tests/test_foundation.py
git commit -m "feat: add macro_regime.py — 9-state classifier + health score + FRED cache"
```

---

## Task 6: economic_calendar.py

**Files:**
- Create: `economic_calendar.py`
- Modify: `tests/test_foundation.py` (append)

- [ ] **Step 1: Add failing tests**

Append to `tests/test_foundation.py`:

```python
# ══════════════════════════════════════════════════════════════════════
# economic_calendar
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_no_event_risk_on_quiet_day():
    """A normal Tuesday at 10 AM should not be in an event risk window."""
    from economic_calendar import is_event_risk_window
    # Pick a known quiet date (Wednesday May 6, 2026 at 10:00 AM ET)
    quiet = datetime(2026, 5, 6, 10, 0, tzinfo=ET)
    with patch("economic_calendar._load_calendar", return_value=[]):
        risk, name = is_event_risk_window(quiet)
    assert risk is False
    assert name == ""


@pytest.mark.unit
def test_event_risk_45_min_before():
    """Should flag risk 44 minutes before a scheduled event."""
    from economic_calendar import is_event_risk_window
    event_time = datetime(2026, 5, 2, 8, 30, tzinfo=ET)   # NFP at 8:30 AM
    check_time  = datetime(2026, 5, 2, 7, 46, tzinfo=ET)  # 44 min before
    fake_events = [{"time": event_time.isoformat(), "name": "NFP", "impact": "high"}]
    with patch("economic_calendar._load_calendar", return_value=fake_events):
        risk, name = is_event_risk_window(check_time)
    assert risk is True
    assert "NFP" in name


@pytest.mark.unit
def test_no_risk_outside_window():
    """Should NOT flag risk 60 minutes before an event (outside 45-min window)."""
    from economic_calendar import is_event_risk_window
    event_time = datetime(2026, 5, 2, 8, 30, tzinfo=ET)
    check_time  = datetime(2026, 5, 2, 7, 29, tzinfo=ET)  # 61 min before
    fake_events = [{"time": event_time.isoformat(), "name": "NFP", "impact": "high"}]
    with patch("economic_calendar._load_calendar", return_value=fake_events):
        risk, name = is_event_risk_window(check_time)
    assert risk is False


@pytest.mark.unit
def test_hardcoded_nfp_first_friday():
    """NFP should appear on the first Friday of every month."""
    from economic_calendar import _hardcoded_events
    events = _hardcoded_events(2026, 5)
    nfp_events = [e for e in events if "NFP" in e["name"] or "Non-Farm" in e["name"]]
    assert len(nfp_events) >= 1
    # First Friday of May 2026 is May 1 (it's a Friday)
    nfp_dates = [datetime.fromisoformat(e["time"]).date() for e in nfp_events]
    assert date(2026, 5, 1) in nfp_dates


@pytest.mark.unit
def test_get_todays_events_returns_list():
    from economic_calendar import get_todays_events
    fake_events = [
        {"time": datetime(2026, 5, 1, 8, 30, tzinfo=ET).isoformat(),
         "name": "NFP", "impact": "high"},
        {"time": datetime(2026, 5, 1, 14, 0, tzinfo=ET).isoformat(),
         "name": "Some Low Event", "impact": "low"},
    ]
    with patch("economic_calendar._load_calendar", return_value=fake_events):
        events = get_todays_events()
    # Only high-impact events should be returned
    assert all(e["impact"] == "high" for e in events)
```

- [ ] **Step 2: Run tests — expect FAIL**

```bash
.venv/bin/python3 -m pytest tests/test_foundation.py -v -m unit -k "event or nfp or calendar"
```

Expected: `FAILED` with `ModuleNotFoundError: No module named 'economic_calendar'`

- [ ] **Step 3: Create `economic_calendar.py`**

```python
"""
Economic calendar — event risk gate for trading entries.

Provides is_event_risk_window(now) to block new entries 45 min before/after
high-impact macro events. Data source: hardcoded recurring schedule + optional
ForexFactory web scrape, cached in economic_calendar.json for 24 hours.
"""
import json
import logging
from calendar import monthcalendar
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

import config

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

_RISK_WINDOW_MINUTES = 45


def _first_weekday_of_month(year: int, month: int, weekday: int) -> int:
    """Return the day number of the first occurrence of weekday (0=Mon…6=Sun) in month."""
    for week in monthcalendar(year, month):
        if week[weekday] != 0:
            return week[weekday]
    return 1  # should never reach here


def _hardcoded_events(year: int, month: int) -> list[dict]:
    """
    Return a list of known recurring high-impact events for the given year/month.
    All times are ET. This is the fallback when the web scrape fails.
    """
    events = []

    # NFP: first Friday of each month at 8:30 AM ET
    first_friday = _first_weekday_of_month(year, month, 4)  # 4 = Friday
    events.append({
        "time":   datetime(year, month, first_friday, 8, 30, tzinfo=ET).isoformat(),
        "name":   "NFP (Non-Farm Payrolls)",
        "impact": "high",
    })

    # FOMC decision: known 2026 dates (8 per year, announced in advance)
    fomc_2026 = [
        (1, 29), (3, 18), (5, 6), (6, 17), (7, 29),
        (9, 16), (10, 28), (12, 9),
    ]
    for m, d in fomc_2026:
        if m == month:
            events.append({
                "time":   datetime(year, month, d, 14, 0, tzinfo=ET).isoformat(),
                "name":   "FOMC Rate Decision",
                "impact": "high",
            })

    # CPI: typically the second or third Wednesday at 8:30 AM
    # Approximate: 12th of each month (close enough for a ±45 min gate)
    try:
        events.append({
            "time":   datetime(year, month, 12, 8, 30, tzinfo=ET).isoformat(),
            "name":   "CPI Release (approx)",
            "impact": "high",
        })
    except ValueError:
        pass

    return events


def _scrape_forexfactory(target_date: datetime) -> list[dict]:
    """
    Attempt to scrape ForexFactory for high-impact USD events on target_date.
    Returns empty list on any failure (caller falls back to hardcoded).
    """
    try:
        date_str = target_date.strftime("%b%d.%Y").lower()  # e.g. "may01.2026"
        url = f"https://www.forexfactory.com/calendar?day={date_str}"
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        resp.raise_for_status()
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "html.parser")
        events = []
        for row in soup.select("tr.calendar__row"):
            impact_el = row.select_one(".impact span")
            if not impact_el:
                continue
            impact = impact_el.get("title", "").lower()
            if "high" not in impact:
                continue
            currency_el = row.select_one(".currency")
            if not currency_el or "USD" not in currency_el.text:
                continue
            time_el = row.select_one(".time")
            title_el = row.select_one(".event")
            if not time_el or not title_el:
                continue
            time_str = time_el.text.strip()
            try:
                event_time = datetime.strptime(
                    f"{target_date.strftime('%Y-%m-%d')} {time_str}", "%Y-%m-%d %I:%M%p"
                ).replace(tzinfo=ET)
            except ValueError:
                continue
            events.append({
                "time":   event_time.isoformat(),
                "name":   title_el.text.strip(),
                "impact": "high",
            })
        return events
    except Exception as exc:
        logger.debug(f"ForexFactory scrape failed: {exc} — using hardcoded events")
        return []


def refresh_economic_calendar() -> None:
    """
    Fetch today's high-impact events. Tries ForexFactory scrape first;
    falls back to hardcoded schedule. Writes economic_calendar.json.
    Called at 8:30 AM ET by main.py.
    """
    now = datetime.now(ET)
    scraped = _scrape_forexfactory(now)
    hardcoded = _hardcoded_events(now.year, now.month)

    # Merge: prefer scraped; use hardcoded as supplement if scrape empty
    events = scraped if scraped else hardcoded

    # Filter to today only
    today_str = now.strftime("%Y-%m-%d")
    todays = [e for e in events if e["time"].startswith(today_str)]

    data = {
        "date":   today_str,
        "events": todays,
        "source": "forexfactory" if scraped else "hardcoded",
    }
    config.ECONOMIC_CALENDAR_FILE.write_text(json.dumps(data, indent=2))
    logger.info(
        f"ECONOMIC CALENDAR: {len(todays)} high-impact events today "
        f"(source: {data['source']})"
    )


def _load_calendar() -> list[dict]:
    """Load today's cached events. Returns empty list if file missing or stale."""
    try:
        data = json.loads(config.ECONOMIC_CALENDAR_FILE.read_text())
        today_str = datetime.now(ET).strftime("%Y-%m-%d")
        if data.get("date") != today_str:
            return []
        return data.get("events", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def is_event_risk_window(now: datetime) -> tuple[bool, str]:
    """
    Return (True, event_name) if `now` is within 45 min before or after
    any high-impact event today. Otherwise (False, '').
    """
    events = _load_calendar()
    window = timedelta(minutes=_RISK_WINDOW_MINUTES)
    for event in events:
        try:
            event_time = datetime.fromisoformat(event["time"])
        except ValueError:
            continue
        if abs(now - event_time) <= window:
            return True, event["name"]
    return False, ""


def get_todays_events() -> list[dict]:
    """Return today's high-impact events (impact == 'high')."""
    return [e for e in _load_calendar() if e.get("impact") == "high"]
```

- [ ] **Step 4: Install beautifulsoup4 if not present**

```bash
.venv/bin/pip install beautifulsoup4
```

Check `requirements.txt` — if `beautifulsoup4` is not listed, append it:
```bash
grep -q "beautifulsoup4" requirements.txt || echo "beautifulsoup4>=4.12" >> requirements.txt
```

- [ ] **Step 5: Run economic_calendar tests**

```bash
.venv/bin/python3 -m pytest tests/test_foundation.py -v -m unit -k "event or nfp or calendar"
```

Expected: `5 passed`

- [ ] **Step 6: Commit**

```bash
git add economic_calendar.py tests/test_foundation.py requirements.txt
git commit -m "feat: add economic_calendar.py — event risk gate with hardcoded fallback"
```

---

## Task 7: api_budget.py — Daily Token Spend Tracker

**Files:**
- Create: `api_budget.py`
- Modify: `tests/test_foundation.py` (append)

- [ ] **Step 1: Add failing tests**

Append to `tests/test_foundation.py`:

```python
# ══════════════════════════════════════════════════════════════════════
# api_budget
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_initial_budget_is_zero(tmp_path):
    import api_budget as ab
    with patch.object(ab.config, "API_BUDGET_FILE", tmp_path / "budget.json"):
        budget = ab.load_budget()
    assert budget["estimated_cost_usd"] == 0.0
    assert budget["budget_exhausted"] is False


@pytest.mark.unit
def test_record_usage_accumulates(tmp_path):
    import api_budget as ab
    with patch.object(ab.config, "API_BUDGET_FILE", tmp_path / "budget.json"):
        ab.record_usage(input_tokens=2000, output_tokens=300,
                        cache_write_tokens=0, cache_read_tokens=0)
        ab.record_usage(input_tokens=2000, output_tokens=300,
                        cache_write_tokens=0, cache_read_tokens=0)
        budget = ab.load_budget()
    # 2 calls × (2000×$3/MTok + 300×$15/MTok) = 2 × ($0.006 + $0.0045) = $0.021
    assert abs(budget["estimated_cost_usd"] - 0.021) < 0.001
    assert budget["calls_made"] == 2


@pytest.mark.unit
def test_check_budget_blocks_when_exhausted(tmp_path):
    import api_budget as ab
    with patch.object(ab.config, "API_BUDGET_FILE", tmp_path / "budget.json"):
        # Manually set cost near ceiling
        data = ab.load_budget()
        data["estimated_cost_usd"] = 0.99
        ab._save_budget(data)
        # A $0.02 call would push over $1.00 ceiling
        allowed = ab.check_budget(estimated_call_cost=0.02,
                                  portfolio_gain_pct=0.0, regime="NORMAL")
    assert allowed is False


@pytest.mark.unit
def test_elevated_ceiling_for_crisis(tmp_path):
    import api_budget as ab
    with patch.object(ab.config, "API_BUDGET_FILE", tmp_path / "budget.json"):
        data = ab.load_budget()
        data["estimated_cost_usd"] = 1.20   # over normal $1.00 but under elevated $1.50
        ab._save_budget(data)
        allowed = ab.check_budget(estimated_call_cost=0.02,
                                  portfolio_gain_pct=0.0, regime="CRISIS")
    assert allowed is True


@pytest.mark.unit
def test_elevated_ceiling_for_high_profit(tmp_path):
    import api_budget as ab
    with patch.object(ab.config, "API_BUDGET_FILE", tmp_path / "budget.json"):
        data = ab.load_budget()
        data["estimated_cost_usd"] = 1.10
        ab._save_budget(data)
        allowed = ab.check_budget(estimated_call_cost=0.02,
                                  portfolio_gain_pct=1.2, regime="NORMAL")
    assert allowed is True


@pytest.mark.unit
def test_get_ceiling_normal():
    import api_budget as ab
    assert ab.get_ceiling(portfolio_gain_pct=0.0, regime="NORMAL") == 1.00


@pytest.mark.unit
def test_get_ceiling_elevated_for_high_profit():
    import api_budget as ab
    assert ab.get_ceiling(portfolio_gain_pct=1.0, regime="NORMAL") == 1.50


@pytest.mark.unit
def test_get_ceiling_elevated_for_capitulation():
    import api_budget as ab
    assert ab.get_ceiling(portfolio_gain_pct=0.0, regime="CAPITULATION") == 1.50


@pytest.mark.unit
def test_budget_resets_on_new_day(tmp_path):
    import api_budget as ab
    yesterday = "2026-04-30"
    stale = {
        "date": yesterday, "calls_made": 50,
        "estimated_cost_usd": 0.85, "budget_exhausted": False,
        "ceiling_usd": 1.0, "ceiling_mode": "normal",
    }
    budget_file = tmp_path / "budget.json"
    budget_file.write_text(json.dumps(stale))
    with patch.object(ab.config, "API_BUDGET_FILE", budget_file):
        budget = ab.load_budget()
    assert budget["calls_made"] == 0
    assert budget["estimated_cost_usd"] == 0.0
```

- [ ] **Step 2: Run tests — expect FAIL**

```bash
.venv/bin/python3 -m pytest tests/test_foundation.py -v -m unit -k "budget"
```

Expected: `FAILED` with `ModuleNotFoundError`

- [ ] **Step 3: Create `api_budget.py`**

```python
"""
API token budget governor.

Tracks daily Claude API spend in api_budget.json.
Enforces a $1.00/day normal ceiling and $1.50/day elevated ceiling.
All Claude callers must call check_budget() before sending and record_usage() after.
"""
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

# Claude Sonnet 4.6 pricing (per million tokens)
_INPUT_RATE       = 3.00    # $/MTok
_CACHE_WRITE_RATE = 3.75    # $/MTok (5-min TTL write)
_CACHE_READ_RATE  = 0.30    # $/MTok
_OUTPUT_RATE      = 15.00   # $/MTok


def _cost(input_t: int, output_t: int,
          cache_write_t: int, cache_read_t: int) -> float:
    """Compute estimated cost in dollars for a single API call."""
    return (
        input_t       / 1_000_000 * _INPUT_RATE
        + cache_write_t / 1_000_000 * _CACHE_WRITE_RATE
        + cache_read_t  / 1_000_000 * _CACHE_READ_RATE
        + output_t      / 1_000_000 * _OUTPUT_RATE
    )


def _today() -> str:
    return datetime.now(ET).date().isoformat()


def load_budget() -> dict:
    """Load api_budget.json, resetting to zero if it's a new day."""
    today = _today()
    try:
        data = json.loads(config.API_BUDGET_FILE.read_text())
        if data.get("date") != today:
            raise ValueError("stale")
        return data
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return {
            "date":              today,
            "calls_made":        0,
            "input_tokens":      0,
            "output_tokens":     0,
            "cache_write_tokens": 0,
            "cache_read_tokens": 0,
            "estimated_cost_usd": 0.0,
            "ceiling_usd":       config.API_BUDGET_NORMAL_CEILING,
            "ceiling_mode":      "normal",
            "budget_exhausted":  False,
        }


def _save_budget(data: dict) -> None:
    config.API_BUDGET_FILE.write_text(json.dumps(data, indent=2))


def get_ceiling(portfolio_gain_pct: float = 0.0, regime: str = "NORMAL") -> float:
    """
    Return the active daily ceiling.
    Elevated ($1.50) when: portfolio gain > +0.8% intraday OR regime is CRISIS/CAPITULATION.
    """
    elevated_regime  = regime in ("CRISIS", "CAPITULATION", "CRASH_RECOVERY")
    elevated_profit  = portfolio_gain_pct >= 0.8
    if elevated_regime or elevated_profit:
        return config.API_BUDGET_ELEVATED_CEILING
    return config.API_BUDGET_NORMAL_CEILING


def check_budget(
    estimated_call_cost: float,
    portfolio_gain_pct: float = 0.0,
    regime: str = "NORMAL",
) -> bool:
    """
    Return True if the call is permitted (within budget ceiling).
    Also returns False (warn only) when approaching 80% of ceiling.
    """
    budget  = load_budget()
    ceiling = get_ceiling(portfolio_gain_pct, regime)
    current = budget["estimated_cost_usd"]

    if current + estimated_call_cost > ceiling:
        logger.warning(
            f"API BUDGET: call blocked — would reach ${current + estimated_call_cost:.3f} "
            f"(ceiling=${ceiling:.2f})"
        )
        return False

    if current >= ceiling * config.API_BUDGET_WARNING_PCT:
        logger.warning(
            f"API BUDGET: {current / ceiling * 100:.0f}% used "
            f"(${current:.3f} / ${ceiling:.2f}) — switching to rule-based mode"
        )

    return True


def record_usage(
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> None:
    """Called after every Claude API response to update the daily spend tracker."""
    call_cost = _cost(input_tokens, output_tokens, cache_write_tokens, cache_read_tokens)
    budget = load_budget()
    budget["calls_made"]         += 1
    budget["input_tokens"]       += input_tokens
    budget["output_tokens"]      += output_tokens
    budget["cache_write_tokens"] += cache_write_tokens
    budget["cache_read_tokens"]  += cache_read_tokens
    budget["estimated_cost_usd"] = round(budget["estimated_cost_usd"] + call_cost, 6)
    ceiling = get_ceiling()
    budget["ceiling_usd"]        = ceiling
    budget["budget_exhausted"]   = budget["estimated_cost_usd"] >= ceiling
    _save_budget(budget)
    logger.debug(
        f"API BUDGET: call #{budget['calls_made']} cost=${call_cost:.4f} "
        f"total=${budget['estimated_cost_usd']:.3f}"
    )
```

- [ ] **Step 4: Run all api_budget tests**

```bash
.venv/bin/python3 -m pytest tests/test_foundation.py -v -m unit -k "budget or ceiling"
```

Expected: `9 passed`

- [ ] **Step 5: Commit**

```bash
git add api_budget.py tests/test_foundation.py
git commit -m "feat: add api_budget.py — daily token spend tracker with dynamic $1/$1.50 ceiling"
```

---

## Task 8: main.py — Updated Job Schedule

**Files:**
- Modify: `main.py`

The current `main.py` has a job that calls `vix_regime.fetch_vix_regime()` at 8:55 AM. We add 3 jobs and update that one call. Do NOT remove any existing jobs.

- [ ] **Step 1: Read the scheduler setup section of main.py**

```bash
grep -n "vix_regime\|fetch_vix\|scheduler.add_job\|8:55\|8:30\|4:00\|4:15" \
  /Users/rajeshkumar/Documents/projects/trading-bot/main.py
```

Note the exact line numbers for the `fetch_vix_regime` job and the `setup_scheduler` function.

- [ ] **Step 2: Add the 3 new imports at the top of main.py**

Find the existing import block (after `import vix_regime`). Add:

```python
import economic_calendar
import macro_regime
import api_budget  # noqa: F401 — imported so other modules can use it at startup
```

- [ ] **Step 3: Replace the vix_regime job and add 3 new jobs**

In `setup_scheduler()`, find:
```python
scheduler.add_job(
    vix_regime.fetch_vix_regime,
    ...  # 8:55 AM job
)
```

Replace that job with:
```python
# 8:30 AM — economic calendar (must run before regime classification)
scheduler.add_job(
    economic_calendar.refresh_economic_calendar,
    CronTrigger(hour=8, minute=30, day_of_week="mon-fri", timezone=ET),
    id="economic_calendar_refresh",
    name="Economic Calendar Refresh",
    replace_existing=True,
)

# 8:55 AM — macro regime (replaces vix_regime job)
scheduler.add_job(
    macro_regime.fetch_macro_regime,
    CronTrigger(hour=8, minute=55, day_of_week="mon-fri", timezone=ET),
    id="macro_regime_fetch",
    name="Macro Regime Fetch",
    replace_existing=True,
)
```

Then find the `4:15 PM` learning agent job and add BEFORE it:

```python
# 4:00 PM — FRED cache refresh (after market, before learning agent)
scheduler.add_job(
    macro_regime.refresh_fred_cache,
    CronTrigger(hour=16, minute=0, day_of_week="mon-fri", timezone=ET),
    id="fred_cache_refresh",
    name="FRED Cache Refresh",
    replace_existing=True,
)
```

- [ ] **Step 4: Verify main.py starts without error**

```bash
cd /Users/rajeshkumar/Documents/projects/trading-bot
timeout 5 .venv/bin/python3 main.py 2>&1 | head -20 || true
```

Expected: Scheduler initialisation messages, no `ImportError` or `AttributeError`. The process will be killed by timeout — that is expected.

- [ ] **Step 5: Commit everything together**

```bash
git add main.py economic_calendar.py macro_regime.py api_budget.py \
        tests/test_foundation.py config.py requirements.txt
git commit -m "feat: Plan 1 complete — macro regime, economic calendar, API budget"
```

---

## Task 9: Full Test Run + Smoke Check

- [ ] **Step 1: Run all unit tests**

```bash
cd /Users/rajeshkumar/Documents/projects/trading-bot
.venv/bin/python3 -m pytest tests/test_foundation.py -v -m unit
```

Expected: All tests pass. Count should be ≥ 20.

- [ ] **Step 2: Run existing test suite to check for regressions**

```bash
.venv/bin/python3 -m pytest test_agents.py -v -m unit
```

Expected: All previously passing tests still pass. If any fail, check whether `vix_regime` references in tests need updating (existing code still imports it, so it should be fine).

- [ ] **Step 3: Manual smoke test — run the morning sequence manually**

```bash
.venv/bin/python3 -c "
import logging
logging.basicConfig(level=logging.INFO)
import economic_calendar
import macro_regime
economic_calendar.refresh_economic_calendar()
regime = macro_regime.fetch_macro_regime()
print('Regime:', regime['regime'])
print('Health:', regime['macro_health_score'])
print('Sector scores:', regime['sector_scores'])
"
```

Expected: Fetches live data, prints a valid regime name and health score. No exceptions.

- [ ] **Step 4: Verify macro_regime.json was written**

```bash
cat /Users/rajeshkumar/Documents/projects/trading-bot/macro_regime.json
```

Expected: Valid JSON with `regime`, `macro_health_score`, `sector_scores`, `consecutive_green_days` keys.

---

## What Plan 2 Covers

Plan 1 is complete. The bot now runs with a 9-state regime (but still day-trades only — no behavior changes yet).

**Plan 2** (`2026-05-01-regime-adaptive-plan-2-swing-and-signals.md`) covers:
- `tier1_config.py` — tier-1 name list + dynamic qualification
- `indicator_engine.py` — 6 new signals: parabolic, volume_exhaustion, OBV divergence, bot_herding_score, squeeze_setup, stop_hunt_zone
- `research_agent.py` — macro shock scoring, sector sentiment aggregation, capitulation detection
- `swing_manager.py` — overnight position lifecycle, gap check, tiered TP

**Plan 3** (`2026-05-01-regime-adaptive-plan-3-mode-routing-and-learning.md`) covers:
- `trading_agent.py` — mode-aware entry/exit, batch calls, rule-based filter, event gate, flash crash monitor, counter-bot routing
- `learning_agent.py` — per-regime performance tracking, self-correcting thresholds
