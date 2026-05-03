# Regime-Adaptive Hybrid Trading Strategy — Design Spec

**Date:** 2026-05-01  
**Status:** Approved for implementation  
**Author:** Brainstorming session — Rajesh Kumar + Claude

---

## 1. Overview

### Problem

The current bot is a pure day trader with a 3-state VIX regime (NORMAL / HIGH_VOL / CRISIS). It has no awareness of:
- Multi-day market trends (consecutive green/red days)
- Macro signals beyond VIX: WTI oil, gold, DXY, 10Y yield, CPI, Fed rate, NFP
- Geopolitical and policy shocks (tariff announcements, wars, Fed surprises)
- Economic calendar events (FOMC, NFP, CPI release days)
- Sector-level capitulation and recovery patterns
- Parabolic momentum in AI/semi names
- Flash crash intraday circuit breakers
- Selective overnight swing positions for tier-1 names

As a result it: overtrades in choppy conditions, misses multi-day bull runs on quality names, has no crash survival mechanism beyond same-day VIX, and cannot distinguish a sector narrative selloff (buy opportunity) from a fundamental breakdown (sell immediately).

### Goal

Enhance the bot into a **regime-aware, adaptive trading system** that dynamically routes capital into three trading modes — Day Trade, Swing Trade, Short-term Position — based on a continuously updated macro health score and a 9-state regime classifier. Do not rewrite working logic; fill the gaps with targeted new modules and enhancements.

### Scope

- **In scope:** New macro regime detection, economic calendar, swing position manager, tiered profit-taking, parabolic momentum rules, sector capitulation detection, flash crash circuit breaker, FRED API integration, learning agent enhancements.
- **Out of scope:** Long-term investment (months+), short selling, options, crypto, fundamental DCF analysis, portfolio rebalancing.

---

## 2. Architecture Overview

```
New / changed modules:

  macro_regime.py        ← replaces vix_regime.py as regime source
  economic_calendar.py   ← new: event risk gate
  swing_manager.py       ← new: overnight position lifecycle
  tier1_config.py        ← new: tier-1 names + qualification rules
  api_budget.py          ← new: daily token spend tracker + dynamic ceiling enforcer

Enhanced modules:

  research_agent.py      ← macro shock scoring, sector sentiment, capitulation detection
  trading_agent.py       ← mode-aware entry/exit, tiered TP, event gate, flash crash monitor,
                            batch Claude calls, rule-based pre-filter, counter-bot signal routing
  indicator_engine.py    ← parabolic flag, volume exhaustion, OBV divergence signals,
                            bot_herding_score, squeeze_setup, stop_hunt_zone
  learning_agent.py      ← per-regime performance tracking, threshold self-correction
  main.py                ← new jobs: market monitor (5 min), swing gap check (9:30 AM),
                            swing EOD decision (3:50 PM), FRED refresh (daily), calendar refresh (daily)
  config.py              ← new constants for swing/short-term parameters, API budget thresholds

Unchanged modules:

  alpaca_client.py       ← no changes needed
  volatility_screener.py ← no changes needed
  watch_list_agent.py    ← no changes needed
  backtest.py            ← update after implementation
```

### Data Flow (daily)

```
8:30 AM  economic_calendar.py  → economic_calendar.json   (event risk for today)
8:45 AM  volatility_screener   → daily_tradeable.txt       (unchanged)
8:55 AM  macro_regime.py       → macro_regime.json         (replaces vix_regime.json)
9:00 AM  watch_list_agent      → watch_list.txt            (unchanged)
9:15 AM  research_agent        → research_report.json      (enhanced with sector/macro scoring)
9:30 AM  swing_manager         → gap risk check, close if breached
9:30–3:50 trading_agent        → mode-aware cycles every 10 min
9:30–3:50 trading_agent        → position_monitor every 60 sec (+ flash crash check every 5 min)
3:50 PM  swing_manager         → EOD swing decision (hold vs close)
3:55 PM  force_close           → close all day trade positions (unchanged)
4:00 PM  FRED refresh          → fred_cache.json
4:15 PM  learning_agent        → regime-tagged trade review (enhanced)
4:30 PM  daily_report          → email summary (unchanged)
```

---

## 3. The 9-State Regime Machine

### Regime Definitions

| # | Regime | Trigger Conditions | Primary Mode |
|---|---|---|---|
| 1 | **BULL_TREND** | SPY 3+ consecutive green closes AND macro_health_score > +20 | Swing (tier-1) + Day (others) |
| 2 | **BULL_TREND_PARABOLIC** | BULL_TREND AND any tier-1 name up > 15% in 5 days | Day only with tiered exits |
| 3 | **NORMAL** | No consecutive pattern, macro_health_score -20 to +20 | Day trade (standard) |
| 4 | **CHOPPY** | 3+ sign alternations in SPY last 5 days AND net 5-day SPY move < 0.5% | Day only, very selective |
| 5 | **DEFENSIVE** | 3+ consecutive SPY red days AND bot net P&L negative > 1% over same period | Day, catalyst-driven only |
| 6 | **OPPORTUNITY** | DEFENSIVE conditions BUT AI/semi sector health score > +15 | Swing (tier-1) + limited Day |
| 7 | **CAPITULATION** | Sector sentiment < -25 AND sector index down > 20% from 60d high AND stock OBV rising | Short-term (tier-1) + Day |
| 8 | **CRISIS** | VIX > 35 OR SPY intraday drop > 2.5% from prev close OR SPY 5-day return < -10% | Cash — all positions closed |
| 9 | **CRASH_RECOVERY** | Post-CRISIS: VIX declining 2+ days from peak > 35 AND SPY 2+ consecutive green closes | Short-term (tier-1) only |

### Regime Transition Rules

- Transitions evaluated at 8:55 AM daily by `macro_regime.py` and stored in `macro_regime.json`
- Flash crash (SPY intraday -2.5%) triggers immediate CRISIS override mid-session — checked every 5 minutes by `run_market_monitor()` in `trading_agent.py`
- CHOPPY and BULL_TREND are mutually exclusive; BULL_TREND requires clear directional signal
- DEFENSIVE requires BOTH market signal AND bot performance confirmation (not just a bad market day)
- OPPORTUNITY is a sub-state of DEFENSIVE — both conditions must be checked simultaneously
- CAPITULATION can coexist with DEFENSIVE or NORMAL but not with CRISIS

### Regime Persistence

```json
{
  "regime": "BULL_TREND",
  "macro_health_score": 47,
  "sector_scores": {
    "AI_SEMI": 62,
    "CYBERSECURITY": -18,
    "BROAD_TECH": 31
  },
  "consecutive_green_days": 3,
  "consecutive_red_days": 0,
  "vix_current": 16.8,
  "vix_trend": "declining",
  "spy_5day_return": 2.4,
  "spy_5day_sign_changes": 1,
  "flash_crash_active": false,
  "event_risk_today": false,
  "event_risk_details": "",
  "capitulation_candidates": ["TEAM", "ZS"],
  "timestamp": "2026-05-01T08:55:00-04:00",
  "reason": "3 consecutive SPY green closes, macro health +47, VIX declining"
}
```

---

## 4. macro_health_score Computation

The `macro_health_score` is a continuous score from -100 (maximum macro stress) to +100 (maximum macro health). It drives smooth regime transitions and position sizing rather than hard state snaps.

### Component Signals (Yahoo Finance — real-time)

| Signal | Bullish | Neutral | Bearish | Weight |
|---|---|---|---|---|
| VIX vs 90d avg | < 90% of avg | 90–120% | > 120% | 20% |
| SPY 5-day return | > +1.5% | -0.5% to +1.5% | < -0.5% | 20% |
| WTI Oil (`CL=F`) | Stable ±2% | ±2–5% | Spike > +5% or crash > -8% | 10% |
| Gold (`GC=F`) | Flat / mild rise | — | Surge > +2% (flight to safety) | 8% |
| DXY (`DX-Y.NYB`) | Mild / stable | — | Sharp rise (risk-off signal) | 8% |
| 10Y Yield (`^TNX`) | Declining / stable | — | Rapid rise > 10bps/day | 9% |
| AI/Semi sector ETF (`SOXX`) 5-day | > +2% | -1% to +2% | < -1% | 15% |
| Broad tech ETF (`QQQ`) 5-day | > +1.5% | -1% to +1.5% | < -1% | 10% |

### Component Signals (FRED API — daily cache)

| Signal | Bullish | Neutral | Bearish | Weight |
|---|---|---|---|---|
| Fed Funds Rate trend | Cutting cycle | Paused | Hiking cycle | — (modifier only) |
| CPI YoY trend | Declining toward 2% | Stable | Rising > 3.5% | — (modifier only) |
| Unemployment rate trend | Declining / stable | — | Rising > 0.3% in 1 month | — (modifier only) |

FRED signals act as **regime modifiers** rather than scored components — they shift the macro_health_score thresholds. A Fed cutting cycle raises all bullish thresholds by +5 points (more room to be bullish). A hiking cycle lowers them by -5 (more conservative).

### Score Calculation

Each Yahoo signal scores -10 to +10 based on the bullish/neutral/bearish bands above. Weighted sum gives macro_health_score. FRED modifiers are then applied as ±5 adjustments.

---

## 5. New Module: `economic_calendar.py`

### Purpose

Fetch upcoming high-impact macro events and gate new position entries in the 45 minutes before and after each event.

### Events Tracked

- FOMC rate decision and minutes
- Non-Farm Payroll (NFP) — first Friday of each month
- CPI release — mid-month
- PCE inflation — monthly
- GDP releases — quarterly
- Tariff announcement windows (heuristic: elevated WTI + DXY + political news score)

### Data Source

ForexFactory web scrape (`https://www.forexfactory.com/calendar`) or Nasdaq Data Link calendar endpoint — fetched daily at 8:30 AM. Cached in `economic_calendar.json` with a 24-hour TTL. ForexFactory has no public API; use `requests` + `BeautifulSoup` scrape with a 10-second timeout and a 3-retry backoff. If the scrape fails, fall back to a hardcoded list of known recurring events (first-Friday NFP, mid-month CPI, quarterly GDP, FOMC schedule from the Fed website).

### Interface

```python
def is_event_risk_window(now: datetime) -> tuple[bool, str]:
    """Returns (True, event_name) if within 45 min of a high-impact event, else (False, '')."""

def get_todays_events() -> list[dict]:
    """Returns list of today's high-impact events with time, name, impact level."""
```

`trading_agent.run_trading_cycle()` calls `is_event_risk_window()` before any new entry. If True, the entry is skipped with a log entry: `SKIP {symbol} — event risk window: {event_name}`.

### Tariff / Breaking News Heuristic

If no scheduled event exists but the research agent detects a macro shock keyword cluster (tariff, sanction, war escalation, Fed emergency) in the last 30 minutes of RSS/Reddit scans — flag as `unscheduled_event_risk = True`. Same 45-minute entry lockout applies.

---

## 6. New Module: `swing_manager.py`

### Purpose

Manage the full lifecycle of tier-1 overnight positions: qualification at 3:50 PM, gap risk check at 9:30 AM, and intraday monitoring with trailing stops.

### Swing Position Store

`swing_positions.json` — separate from `open_positions.json` (day trades). Structure mirrors open_positions with additional swing-specific fields:

```json
{
  "NVDA": {
    "entry_price": 875.50,
    "qty": 5,
    "entry_time": "2026-05-01T14:23:00-04:00",
    "stop_loss_pct": 7.0,
    "take_profit_pct": 15.0,
    "stop_loss_price": 814.22,
    "take_profit_price": 1006.83,
    "tp1_pct": 5.0,
    "tp1_qty": 2,
    "tp1_hit": false,
    "tp2_pct": 10.0,
    "tp2_qty": 2,
    "tp2_hit": false,
    "trailing_qty": 1,
    "trailing_stop_active": false,
    "trailing_high_water": 875.50,
    "swing_days_held": 0,
    "max_swing_days": 5,
    "regime_at_entry": "BULL_TREND",
    "capitulation_hold": false,
    "hard_deadline": null,
    "claude_reasoning": "...",
    "order_id": "abc123"
  }
}
```

### Qualification for Overnight Hold (3:50 PM check)

A day trade position can be converted to a swing hold if ALL of the following are true:

1. Symbol is on the tier-1 list
2. Current regime is BULL_TREND, OPPORTUNITY, or CAPITULATION
3. `is_event_risk_window()` is False for the next morning's open
4. No major earnings announcement for this stock in the next 2 trading days (unless CAPITULATION hold is intentional)
5. Current P&L > -2% (not converting a losing day trade into a swing hope — PTJ rule)
6. Fewer than 2 swing positions already open
7. macro_health_score for the stock's sector > +10

If conditions 1–7 are met, `swing_manager.evaluate_eod_holds()` is called at 3:50 PM. It asks Claude for a hold/close decision with the swing context, then either excludes the position from force-close (hold) or lets it be closed at 3:55 PM (close).

### Morning Gap Risk Check (9:30 AM)

`swing_manager.run_gap_check()` runs at 9:30 AM:

1. Fetch pre-market price for each swing position
2. If pre-market price shows gap down > swing stop loss % → close immediately at market open
3. If gap down 2–5% but above stop → tighten trailing stop to 2% for the session
4. If gap up > 3% → immediately take TP1 (partial close), activate trailing stop for remainder

### Tiered TP Execution

Tiered TP is managed by `run_position_monitor()` (already runs every 60 seconds) extended to check swing positions:

| Level | Trigger | Action |
|---|---|---|
| TP1 | P&L reaches +5% | Close tp1_qty shares (40% of position), set tp1_hit=True |
| TP2 | P&L reaches +10% | Close tp2_qty shares (30% of position), set tp2_hit=True |
| Trailing | After TP2 hits | Track trailing_high_water, close trailing_qty if price drops 3% from high |
| Hard stop | P&L ≤ -stop_loss_pct | Close entire remaining position immediately |
| Max days | swing_days_held ≥ max_swing_days | Close at next opportunity regardless of P&L |

### Capitulation Hold Rules (Special Case)

When a short-term position is held under CAPITULATION regime:
- Stop widens to 12% (thesis: sector fear, not fundamentals)
- TP1 at +10%, TP2 at +20%, trailing for remainder
- `hard_deadline` set to earnings date + 3 sessions
- If `hard_deadline` passes with no TP2 hit → close regardless of P&L
- Claude reviews thesis validity weekly via learning agent

---

## 7. New Module: `tier1_config.py`

### Tier-1 Name List

Tier-1 names are eligible for swing and short-term position modes. Defined in `tier1_config.py` as a structured list with sector tagging:

```python
TIER1_NAMES = {
    # AI Infrastructure
    "NVDA": {"sector": "AI_SEMI", "theme": "GPU/AI compute"},
    "AVGO": {"sector": "AI_SEMI", "theme": "AI networking chips"},
    "AMD":  {"sector": "AI_SEMI", "theme": "GPU/CPU AI"},
    "MRVL": {"sector": "AI_SEMI", "theme": "AI networking"},
    "ASML": {"sector": "AI_SEMI", "theme": "EUV lithography"},
    "AMAT": {"sector": "AI_SEMI", "theme": "Semiconductor equipment"},
    "LRCX": {"sector": "AI_SEMI", "theme": "Semiconductor equipment"},
    "MU":   {"sector": "AI_SEMI", "theme": "HBM/AI memory"},
    # Hyperscalers / AI Cloud
    "MSFT": {"sector": "HYPERSCALER", "theme": "Azure/Copilot"},
    "GOOGL":{"sector": "HYPERSCALER", "theme": "Cloud/Gemini AI"},
    "AMZN": {"sector": "HYPERSCALER", "theme": "AWS/AI infra"},
    "META": {"sector": "HYPERSCALER", "theme": "AI infrastructure"},
    # AI Optics / Data Center
    "LITE": {"sector": "AI_OPTICS",   "theme": "Optical transceivers"},
    "COHR": {"sector": "AI_OPTICS", "theme": "Laser/optical (Coherent Corp)"},
    "VST":  {"sector": "DATA_CENTER", "theme": "Power for AI data centers"},
    "CEG":  {"sector": "DATA_CENTER", "theme": "Nuclear power for AI"},
    "EQIX": {"sector": "DATA_CENTER", "theme": "Colocation"},
    # Geopolitical Hedges (activated during DEFENSIVE/CRISIS)
    "GLD":  {"sector": "GEO_HEDGE", "theme": "Gold ETF"},
    "NEM":  {"sector": "GEO_HEDGE", "theme": "Gold miner"},
    "LMT":  {"sector": "GEO_HEDGE", "theme": "Defense"},
    "RTX":  {"sector": "GEO_HEDGE", "theme": "Defense/aerospace"},
}
```

### Dynamic Qualification

Beyond being on the list, a name must pass swing qualification at time of entry:
- Composite score > +25 (higher than day trade threshold of +15)
- RSI 50–82 in BULL_TREND (50–72 in NORMAL/OPPORTUNITY)
- Volume ratio > 1.3× (institutional participation confirmed)
- Sentiment score > +20 from research agent
- Not flagged with `parabolic=True` AND `momentum_exhaustion=True` simultaneously

---

## 8. Enhanced Module: `macro_regime.py`

Replaces `vix_regime.py`. Fully backward-compatible: still writes a `macro_regime.json` with `regime` key that existing code reads. New fields are additive.

### New Responsibilities

1. Compute `macro_health_score` from Yahoo Finance signals (real-time at 8:55 AM)
2. Fetch and cache FRED data (Fed rate, CPI, unemployment) daily at 4:00 PM
3. Detect consecutive green/red day patterns in SPY
4. Detect choppy pattern (sign alternation count)
5. Compute per-sector health scores (AI_SEMI, CYBERSECURITY, BROAD_TECH)
6. Classify regime into 9 states
7. Detect flash crash signal (called by market monitor every 5 minutes intraday)

### Flash Crash Detection (Intraday)

```python
def check_flash_crash(alpaca: AlpacaClient) -> bool:
    """Returns True if SPY has dropped > 2.5% from previous close during today's session."""
    spy_prev_close = get_prev_close("SPY")
    spy_current = alpaca.get_latest_price("SPY")
    intraday_move = (spy_current - spy_prev_close) / spy_prev_close * 100
    return intraday_move <= -2.5
```

Called every 5 minutes by `run_market_monitor()` in main.py. If True: triggers immediate force-close of all day trade AND swing positions, sets 48-hour entry lockout flag in `macro_regime.json`.

---

## 9. Enhanced Module: `research_agent.py`

### New: Macro Shock Scoring

Each RSS/Reddit fetch now runs a secondary Claude call to score macro shock risk for the session:

```
Macro shock keywords tracked:
  - Tariff / trade war / sanction / import duty
  - War escalation / military strike / conflict
  - Fed emergency / rate surprise / FOMC unscheduled
  - Bank failure / credit crisis / liquidity
  - Inflation surprise / CPI miss / PCE shock
  - Earnings catastrophe / revenue miss / guidance cut
```

Output: `macro_shock_score` (0–100) per session in `research_report.json`. If > 60, sets `unscheduled_event_risk = True` in macro_regime.json. Trading agent skips new entries until score drops below 40.

### New: Sector Sentiment Aggregation

Rolling 20-day sector sentiment computed from individual stock scores:
- AI/Semi: aggregate of NVDA, AMD, AVGO, MU, AMAT, LRCX
- Cybersecurity: CRWD, PANW, ZS, FTNT
- Hyperscaler: MSFT, GOOGL, AMZN, META
- Broad Tech: QQQ composition sentiment

Sector scores stored in `research_report.json["sector_sentiment"]`.

### New: Capitulation Detection

For each watch-list stock, compute:
1. Drawdown from 20-day high (in %)
2. Sector sentiment for that stock's sector
3. OBV trend (rising while price falling = accumulation)
4. Analyst estimate trend (FRED/Yahoo analyst data)

If: stock down > 20% from 20-day high AND sector sentiment < -20 AND OBV rising:
→ Tag stock as `capitulation_candidate: true` in research_report.json

---

## 10. Enhanced Module: `indicator_engine.py`

### Three New Signals (Original)

**Parabolic Flag**
```python
parabolic: bool  # True if 5-day return > 15%
```

**Volume Exhaustion**
```python
volume_exhaustion: bool  # True if price at 5-day high but volume < 0.8× 20-day avg volume
```

**OBV Divergence**
```python
obv_divergence: str  # "bearish" if price making new 5d high but OBV flat/declining
                     # "bullish" if price making new 5d low but OBV rising
                     # "none" otherwise
```

**Momentum Exhaustion Signal**
Composite of the above: set `momentum_exhaustion = True` if 2+ of (parabolic, volume_exhaustion, obv_divergence=="bearish") are True simultaneously.

These are added to the `IndicatorValues` dataclass and included in all trade journal entries.

### Three New Signals (Counter-Bot)

**`bot_herding_score` (0–100)**
Measures synchronized bot activity across the watch list:
```python
bot_herding_score: int  # 0 = fully uncorrelated market, 100 = full bot synchronization
```
Computed as a weighted average of:
- Rolling 10-bar pairwise correlation across all watch-list stocks vs SPY. Avg correlation > 0.85 → score 60+
- Timing uniformity: if > 50% of watch-list stocks cross BUY threshold in same 10-min bar → score 80+
- Volume uniformity: coefficient of variation of volume across unrelated stocks < 0.15 → score rises 10 pts

**`squeeze_setup` (bool + confidence 0–100)**
Short squeeze preconditions:
```python
squeeze_setup: bool          # True if confidence >= 70
squeeze_confidence: int      # 0–100
```
Conditions scored:
- Float turnover ratio > 3× over 3 days (+30 pts)
- Price within 2% below or breaking 5-day resistance (+25 pts)
- Volume surge > 2.5× 20-day average on current bar (+25 pts)
- OBV sharply rising while price was falling/flat last 3 bars (+20 pts)

**`stop_hunt_zone` (bool)**
Identifies HFT/bot liquidity grab patterns:
```python
stop_hunt_zone: bool  # True if conditions below are met
```
Conditions: price within 0.4% of a round number (X.00 or X.50) OR within 0.3% of the 5-day low/high, AND the dip was recovered within 3 candles. Entry is skipped when True — wait one bar.

All three signals are added to `IndicatorValues` and included in trade journal entries.

---

## 11. Enhanced Module: `trading_agent.py`

### Mode-Aware Entry Logic

At the start of each cycle, `run_trading_cycle()` determines `active_mode` based on current regime:

```python
def get_active_mode(regime: str) -> str:
    if regime in ("BULL_TREND", "OPPORTUNITY"):
        return "HYBRID"          # swing eligible for tier-1, day for others
    elif regime in ("CAPITULATION", "CRASH_RECOVERY"):
        return "SHORT_TERM"      # tier-1 short-term positions only
    elif regime == "CRISIS":
        return "CASH"            # no new entries
    else:
        return "DAY_TRADE"       # NORMAL, CHOPPY, DEFENSIVE
```

### Event Risk Gate

Before any BUY decision is executed:
```python
event_risk, event_name = is_event_risk_window(now)
if event_risk:
    logger.info(f"SKIP {symbol} — event risk window: {event_name}")
    continue
```

### Flash Crash Monitor

New job `run_market_monitor()` runs every 5 minutes (separate from position_monitor):
```python
def run_market_monitor():
    if check_flash_crash(alpaca):
        logger.critical("FLASH CRASH DETECTED — triggering immediate force close")
        force_close_all()
        force_close_swings()  # swing_manager
        set_crisis_lockout(hours=48)
```

### Regime-Aware Stop / TP Parameters

| Mode | Stop Loss | TP1 | TP2 | Trailing |
|---|---|---|---|---|
| Day trade — NORMAL | 3% | — | 5% (single) | — |
| Day trade — CHOPPY | 5% | — | 6% (single) | — |
| Day trade — DEFENSIVE | 4% | — | 5% (single) | — |
| Day trade — BULL_TREND_PARABOLIC | 3% trailing | 5% | 10% | 2% after TP1 |
| Swing — BULL_TREND | 7% | 5% (40%) | 10% (30%) | 3% after TP2 |
| Swing — OPPORTUNITY | 7% | 5% (40%) | 10% (30%) | 3% after TP2 |
| Short-term — CAPITULATION | 12% | 10% (40%) | 20% (30%) | 4% after TP2 |
| Short-term — CRASH_RECOVERY | 10% | 8% (40%) | 18% (30%) | 3% after TP2 |

### Position Sizing Across Modes

| Mode | Max Positions | Portfolio % Each | Max Total Exposure |
|---|---|---|---|
| Day trade | 5 | 5% | 25% |
| Swing (tier-1) | 2 | 3% | 6% |
| Short-term (tier-1) | 2 | 2.5% | 5% |
| Combined max | — | — | 36% |

Capital allocation across modes is capped at 36% of portfolio in any condition, ensuring at least 64% in cash/buying power at all times.

---

## 12. Enhanced Module: `learning_agent.py`

### Per-Regime Performance Tracking

Each trade in `trade_journal.json` is tagged with the regime at entry. The learning agent now computes per-regime statistics:

```
regime_performance = {
  "BULL_TREND":    { win_rate, avg_pnl, best_mode, worst_mode },
  "NORMAL":        { ... },
  "CHOPPY":        { ... },
  ...
}
```

This allows the learning agent to tune thresholds per regime. For example: if CHOPPY win rate < 40%, it raises the composite gate for CHOPPY from +30 to +35.

### Sell/Hold Decision Review

For each closed position, the learning agent classifies:
- Was the loss-source correctly identified (sector fear vs. fundamental vs. market structure)?
- Did the hold/sell decision match the regime rule?
- Flag contradictions for strategy_memory.md updates

### Self-Correcting Thresholds

The learning agent can now adjust (within bounds):
- `choppy_composite_gate`: 25–40
- `bull_trend_rsi_max_tier1`: 76–88
- `swing_max_days`: 3–7
- `capitulation_drawdown_threshold`: 15%–30%

Bounds are hard-coded — the learning agent cannot disable a regime or remove a stop loss.

---

## 13. Updated `main.py` Schedule

| Time (ET) | Job | New? |
|---|---|---|
| 8:30 AM | `economic_calendar.refresh()` | New |
| 8:45 AM | `volatility_screener` | Unchanged |
| 8:55 AM | `macro_regime.fetch_macro_regime()` | Enhanced |
| 9:00 AM | `watch_list_agent` + `reset_daily_closed` | Unchanged |
| 9:15 AM | `research_agent` (enhanced) | Enhanced |
| 9:30 AM | `swing_manager.run_gap_check()` | New |
| Every 10 min (9:30–3:50) | `trading_agent.run_trading_cycle()` | Enhanced |
| Every 60 sec (9:30–3:50) | `trading_agent.run_position_monitor()` | Enhanced |
| Every 5 min (9:30–3:55) | `trading_agent.run_market_monitor()` | New |
| 3:50 PM | `swing_manager.evaluate_eod_holds()` | New |
| 3:55 PM | `trading_agent.force_close_all()` (day trades only) | Enhanced |
| 4:00 PM | `macro_regime.refresh_fred_cache()` | New |
| 4:15 PM | `learning_agent` (enhanced) | Enhanced |
| 4:30 PM | `daily_report` | Unchanged |
| Every 30 min | Heartbeat | Unchanged |

---

## 14. New Persistence Files

| File | Purpose |
|---|---|
| `macro_regime.json` | Full macro health score, regime, sector scores, consecutive day counts |
| `economic_calendar.json` | Today's high-impact events with times |
| `swing_positions.json` | Active overnight swing/short-term positions |
| `fred_cache.json` | FRED macro data (Fed rate, CPI, unemployment) — daily refresh |
| `api_budget.json` | Daily token spend tracker (resets at midnight ET) |
| `daily_report_sent.json` | Unchanged |

`vix_regime.json` → renamed/replaced by `macro_regime.json`. `config.py` updated to point to new file path. Backward compatibility maintained: `macro_regime.json` always contains a `regime` key that existing code reads.

---

## 15. Source-of-Loss Classification

The sell/hold decision for losing positions is governed by the drawdown source, classified daily by the research agent:

| Source | Identified By | Sell or Hold |
|---|---|---|
| Sector narrative fear | Sector sentiment < -25, OBV rising on the stock | Hold (CAPITULATION regime) |
| Macro pressure (tariff, yield) | macro_shock_score > 60, broad tech down | Conditional hold in OPPORTUNITY, sell in DEFENSIVE |
| Fundamental weakness | Revenue miss, guidance cut, CEO departure in news | Sell immediately, all regimes |
| Market structure (crash) | Flash crash detected, VIX > 35 | Sell everything, CRISIS |
| Momentum exhaustion | momentum_exhaustion = True | Sell at trailing stop |

---

## 16. Testing Considerations

### Unit Tests (no API calls, `-m unit`)

- `macro_regime.py`: health score calculation with mock Yahoo Finance data
- `economic_calendar.py`: event window detection with mock calendar data
- `swing_manager.py`: tiered TP logic, gap check logic, hard deadline enforcement
- `indicator_engine.py`: parabolic flag, volume exhaustion, OBV divergence signals
- Regime transition logic: each of 9 states triggered by correct conditions

### Integration Tests (live Alpaca credentials, `-m live`)

- Full trading cycle in each regime mode
- Swing position lifecycle: entry → overnight → gap check → tiered exit
- Flash crash trigger: mock SPY drop, verify force-close fires

### Backtest Validation

After implementation, run `backtest.py` over:
- April 2025 tariff shock period (flash crash test)
- October 2025–April 2026 tech selloff (capitulation detection test)
- Current AI/semi bull run (parabolic momentum and tiered TP test)
- A choppy period (reduced entry, wider stops test)

---

## 17. API Keys Required

| Service | Key Name in .env | Cost |
|---|---|---|
| Alpaca | `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | Free (paper trading) |
| Anthropic | `ANTHROPIC_API_KEY` | Per usage — already in `.env`, Claude Code uses Pro subscription separately |
| Reddit | `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` | Free |
| **FRED** | `FRED_API_KEY` | **Free** — register at fred.stlouisfed.org |
| Economic Calendar | None (scrape) or `INVESTING_COM_API_KEY` | Free scrape or paid API |

> **Note:** Claude Code authenticates via `claude.ai` Pro subscription — confirmed by `claude auth status`. The `ANTHROPIC_API_KEY` in `.env` is already used exclusively by the bot. No key change required.

---

## 18. Implementation Phases

### Phase 1 — Foundation (3–4 days)
`macro_regime.py` (Yahoo signals + consecutive day detection + FRED cache), `economic_calendar.py`, `config.py` updates, `main.py` new job schedule. Bot now has 9-state regime with event gate. Day trading behavior unchanged.

### Phase 2 — Swing Infrastructure (2–3 days)
`swing_manager.py` (gap check + EOD hold decision + tiered TP in position_monitor), `tier1_config.py`. Selective overnight holds now live for tier-1 names in BULL_TREND and OPPORTUNITY.

### Phase 3 — Enhanced Signals (2 days)
`indicator_engine.py` new signals (parabolic, volume exhaustion, OBV divergence), `research_agent.py` sector sentiment + macro shock scoring + capitulation detection. Research report now feeds regime-aware decisions.

### Phase 4 — Full Mode Routing (2 days)
`trading_agent.py` mode-aware entry/exit, regime-aware stop/TP parameters, flash crash monitor, event risk gate. All 9 regimes now fully operational.

### Phase 5 — Learning Agent + Backtesting (2 days)
`learning_agent.py` per-regime tracking and self-correcting thresholds. Backtest validation across the four test periods. Strategy memory updates.

### Phase 6 — API Budget + Counter-Bot Signals (2 days)
`api_budget.py` + `ANTHROPIC_BOT_API_KEY` isolation. Batch trading calls (4–5 stocks/call), rule-based pre-filter, 1-hour prompt cache TTL, dynamic ceiling logic. `indicator_engine.py` counter-bot signals (bot_herding_score, squeeze_setup, stop_hunt_zone). `trading_agent.py` routing logic for squeeze and herding conditions.

**Total estimated: 13–15 days of implementation.**

---

---

## 19. API Token Budget System

### Problem

The trading bot shares `ANTHROPIC_API_KEY` with personal Claude Code usage, making cost measurement unreliable. The bot also makes one Claude call per stock per cycle — with duplicate instances or a large watch list this compounds to $3–4/day. Target: ≤ $1.00/day normal, ≤ $1.50/day elevated.

### New Module: `api_budget.py`

Tracks daily token spend in `api_budget.json` and gates all outgoing Claude calls.

```json
{
  "date": "2026-05-01",
  "calls_made": 47,
  "input_tokens_used": 94000,
  "output_tokens_used": 14100,
  "estimated_cost_usd": 0.494,
  "ceiling_usd": 1.00,
  "ceiling_mode": "normal",
  "budget_exhausted": false
}
```

```python
def check_budget(estimated_call_cost: float) -> bool:
    """Returns True if the call is permitted under today's budget."""

def record_usage(input_tokens: int, output_tokens: int, cache_write_tokens: int, cache_read_tokens: int) -> None:
    """Called after every Claude API response to update api_budget.json."""

def get_ceiling() -> float:
    """Returns current ceiling ($1.00 or $1.50) based on today's conditions."""
```

### Dynamic Ceiling Logic

| Condition | Ceiling |
|---|---|
| Default | **$1.00** |
| Portfolio intraday gain > +0.8% at time of call | **$1.50** (high-profit session) |
| Regime is CRISIS or CAPITULATION | **$1.50** (defense mode — every decision matters) |
| Both conditions above | **$1.50** (cap, not additive) |

When estimated daily spend reaches **80% of ceiling**: remaining trading cycles switch to rule-based decisions only (no Claude calls). When **ceiling is hit**: log `BUDGET EXHAUSTED — rule-based mode for remainder of session`.

### API Key Isolation

The bot already uses `ANTHROPIC_API_KEY` from `.env` exclusively. Claude Code uses the `claude.ai` Pro subscription (confirmed via `claude auth status`) and does not touch the bot's API key. No configuration change needed — budget tracking in `api_budget.json` accurately reflects only bot spend.

### Batch Trading Calls (4–5 stocks per call)

Instead of one Claude call per stock candidate, `run_trading_cycle()` batches 4–5 candidates into a single prompt:

```
Prompt: "Analyze the following 5 stocks and return a JSON array of decisions:
[{symbol, composite_score, indicators, regime, sentiment}, ...]"

Response: [
  {"symbol": "NVDA", "action": "BUY", "confidence": 78, ...},
  {"symbol": "AMD",  "action": "HOLD", "confidence": 55, ...},
  ...
]
```

Reduces ~114 calls/day to ~25 calls/day. Saves ~$0.80/day.

### Rule-Based Pre-Filter (skip Claude for clear cases)

Before batching, each candidate is screened:

| composite_score | All top-3 indicators aligned? | Decision |
|---|---|---|
| < 10 | — | Auto-SKIP (no Claude call) |
| > 35 | Yes | Auto-BUY (no Claude call, use default SL/TP) |
| 10–35 | — | Include in Claude batch |
| > 35 | No (conflict) | Include in Claude batch (conflicting signals need judgment) |

### 1-Hour Prompt Cache TTL

The static portion of every trading prompt (strategy rules, indicator weights, regime description, tier-1 list) is sent with `cache_control: {"type": "ephemeral", "ttl": 3600}`. The current 5-minute TTL expires between 10-minute trading cycles, eliminating all cache reads. Switching to 1-hour TTL means the static context is read from cache for all ~38 intraday cycles — estimated saving: ~$0.30/day.

### Estimated Daily Cost After Optimization

| Component | Before | After |
|---|---|---|
| Trading agent (~38 cycles) | $0.90 | $0.20 (batch + filter + cache) |
| Research agent (2 calls) | $0.05 | $0.03 (batch all stocks in 2 calls) |
| Watch list agent (1 call) | $0.03 | $0.03 |
| Learning agent (1 call) | $0.05 | $0.05 |
| **Total (normal day)** | **~$1.03** | **~$0.31** |
| **Total (elevated ceiling day)** | — | **≤ $1.50** |

---

## 20. AI Bot Market Detection (Counter-Bot Playbook)

### Problem

As AI trading bots proliferate, markets increasingly exhibit bot-specific patterns: synchronized herding (all bots trigger the same signal simultaneously), short squeezes (many bots short the same stock, then scramble to cover), and stop-hunt dips (HFT bots push price through clustered stop levels then reverse). A bot that blindly follows technical momentum in these conditions gets run over. The goal is to detect these patterns and exploit them rather than become a victim.

### Counter-Bot Behavior in `trading_agent.py`

Three new signals from `indicator_engine.py` gate and modify entry decisions:

**When `bot_herding_score` > 60:**
- Raise composite entry gate from +15 to +25 for all non-squeeze candidates
- Reduce position size by 30% (herding moves reverse violently)
- Tag trade with `"bot_herd": true` in trade journal for learning agent analysis
- Log: `BOT_HERD detected (score={score}) — tightening entry gate`

**When `bot_herding_score` > 80:**
- Apply CHOPPY-equivalent rules: catalyst-driven entries only, no pure-technical plays
- Skip any stock whose BUY signal appeared in the same 10-minute bar as 3+ other watch-list stocks

**When `squeeze_setup` confidence ≥ 70:**
- Allow entry even if composite_score is only +12 (squeeze overrides the normal +15 gate)
- Apply BULL_TREND_PARABOLIC exit rules: TP1 at +5% (close 40%), activate trailing stop 2% immediately after TP1 — do NOT wait for TP2 (squeezes reverse in minutes, not hours)
- Widen stop to 6% (squeeze can dip before launching)
- Tag trade with `"squeeze": true` in trade journal

**When `stop_hunt_zone` = True:**
- Skip entry on this bar regardless of score
- Re-evaluate same stock on the next bar (one-bar cooldown only)
- Log: `STOP_HUNT zone — skipping {symbol}, will re-evaluate next bar`

**When `bot_herding_score` > 60 AND `momentum_exhaustion` = True (existing signal):**
- Treat as CLOSE candidate for any existing position in that stock
- Bots are stampeding out — exit before the crowd

### How This Exploits Bot Behavior

| Bot Market Pattern | Our Detection | Our Response |
|---|---|---|
| Herding (all bots BUY simultaneously) | bot_herding_score > 60 | Raise gate, reduce size — ride early, exit before reversal |
| Short squeeze | squeeze_setup ≥ 70 | Enter early, use aggressive trailing TP, no waiting for TP2 |
| Stop-hunt dip | stop_hunt_zone = True | Skip the dip entry, re-enter after bounce confirmed |
| Exhaustion top (bots all at TP simultaneously) | momentum_exhaustion (existing) | Close position ahead of the exit stampede |
| Synchronized exit (all bots sell together) | bot_herding_score > 80 + exhaustion | Flip to CHOPPY rules, sit out the flush |

### Learning Agent Integration

The learning agent tags each closed trade's `bot_herd` and `squeeze` fields and computes:
- Win rate for squeeze entries vs normal entries per regime
- Average slippage on bot-herd entries (to calibrate the 30% size reduction)
- Whether stop-hunt cooldown prevented a good entry or a bad one (to tune the 0.4% zone width)

Results feed back into `trading_params.json` as `squeeze_confidence_gate` (default 70, adjustable 60–80) and `herd_size_reduction` (default 0.30, adjustable 0.15–0.50).

---

---

## 21. Earnings Calendar + Post-Earnings Drift (PEAD) Strategy

### Problem

The bot ignores earnings entirely. Two costs:
1. **Pre-earnings risk:** A 3% stop is meaningless when earnings can move a stock 10–15% overnight. Day-trade BUYs entered the day before earnings effectively bypass risk management.
2. **Missed opportunity (PEAD):** Stocks that beat with raised guidance drift higher for 2–4 weeks (well-documented anomaly — institutions can't rebuild positions in one day). INTC's recent +10% gap-up and continued drift is the canonical pattern.

### Earnings Calendar Module

New `earnings_calendar.py` — daily 8:00 AM job, refreshes per-ticker earnings data for the watch list and tier-1 universe.

```python
def refresh_earnings_calendar(symbols: list[str]) -> None     # writes earnings_calendar.json
def days_until_earnings(symbol: str) -> int | None
def is_earnings_imminent(symbol: str, days: int = 1) -> bool
def recently_reported(symbol: str, days: int = 5) -> dict | None
def detect_pead_setup(symbol: str) -> tuple[bool, int]        # (qualifies, score 0-100)
```

`recently_reported` returns:
```json
{
  "report_date": "2026-04-25",
  "eps_actual": 0.45, "eps_estimate": 0.41, "eps_surprise_pct": 9.8,
  "revenue_beat": true,
  "guidance_change": "raised",
  "earnings_day_return_pct": 8.4,
  "earnings_day_volume_ratio": 3.1,
  "earnings_day_close_position": 0.92
}
```

### Pre-Entry Earnings Gate

`_check_entry_gates` adds an earnings check BEFORE the squeeze override:

| Condition | Action |
|---|---|
| `is_earnings_imminent(symbol, days=1)` AND mode != CAPITULATION | Skip BUY |
| `is_earnings_imminent(symbol, days=2)` AND mode in {HYBRID, SHORT_TERM} | Skip BUY (no overnight risk over earnings) |

CAPITULATION exempts the rule because earnings is often the catalyst the hold is waiting for.

### PEAD Setup Detection

Qualifies if **4 of 5** conditions hit in the last 5 trading days:

1. `eps_surprise_pct ≥ 5.0`
2. `revenue_beat == True`
3. `guidance_change in ("raised", "maintained")` (lowered = disqualify)
4. `earnings_day_return_pct ≥ 3.0` AND `earnings_day_close_position ≥ 0.7`
5. `earnings_day_volume_ratio ≥ 2.0`

Each met condition adds 20 points; threshold = 80 (4 of 5).

### PEAD Mode

A new trading mode that overrides the regime default for tier-1 names:

| Property | Value |
|---|---|
| Universe | Tier-1 only |
| Activation | `detect_pead_setup` returns True AND `is_tier1(symbol)` AND regime ∉ {CRISIS} |
| Position size | 2.5% of portfolio |
| Stop loss | 8% |
| Take profit (TP1) | +8% (close 50%) |
| TP2 / Trailing | 5% trailing stop on remaining 50% after TP1 |
| Max hold | 15 trading days |
| Early exit | 3 consecutive lower closes after day 5 |
| Hard kill | Flash crash detected OR regime flips to CRISIS |

Stored in `swing_positions.json` with new field `mode_at_entry: "PEAD"`. Existing swing manager TP/trailing/hard-stop logic handles execution; only the entry decision is new.

### `get_active_mode` Update

Two-step resolution:

```python
def get_active_mode(regime: str, symbol: str | None = None) -> str:
    if symbol and is_tier1(symbol) and regime != "CRISIS":
        ok, _ = detect_pead_setup(symbol)
        if ok:
            return "PEAD"
    # ... existing regime-based logic
```

### Learning Agent Integration

New loss-source category: `earnings_surprise` — trade closed within 2 days of earnings release. Helps detect whether the earnings gate is missing edge cases.

---

## 22. Data Sources (All Free)

| Need | Primary | Fallback | Notes |
|---|---|---|---|
| Intraday bars | **Yahoo Finance** (`query1.finance.yahoo.com`) | Alpaca paper IEX | Yahoo primary on paper (Alpaca paper covers ~2% of volume via IEX-only); switch to `MARKET_DATA_PROVIDER=alpaca` when upgrading to live SIP |
| Daily bars | **Yahoo Finance** | Alpaca | Yahoo unlimited history; Alpaca daily fine on either tier |
| Real-time quotes | Alpaca paper (good enough for paper trading) | Polygon free tier (5/min, SPY-only) | Live SIP via Alpaca subscription replaces this when user upgrades |
| VIX, SPY, sector ETFs (SOXX, QQQ) | Yahoo Finance (`query1.finance.yahoo.com`) | — | Unauthenticated, no key |
| WTI, Gold, DXY, 10Y yield | Yahoo Finance | — | Same endpoint |
| Fed funds rate, CPI, unemployment | FRED API (`api.stlouisfed.org`) | — | Free with `FRED_API_KEY` from fred.stlouisfed.org |
| Economic calendar (FOMC, NFP, CPI) | ForexFactory (web scrape) | Hardcoded recurring schedule | Fallback covers when scrape fails |
| **Earnings dates** | **Yahoo Finance `quoteSummary?modules=calendarEvents`** | **Alpaca `get_corporate_action_announcements`** | Both free |
| **Earnings actuals + estimates** | **Yahoo Finance `quoteSummary?modules=earningsHistory`** | **Finnhub free tier (60 calls/min)** | Yahoo provides last 4 quarters |
| **Forward guidance** | Yahoo `quoteSummary?modules=earningsTrend` (analyst revisions as proxy) | Treat as `unknown` if unavailable | Real guidance text needs paid API; analyst revisions in 7 days post-report are a free proxy |
| News headlines | RSS (MarketWatch, CNBC, Benzinga, Yahoo, Bloomberg) | — | Already in use |
| Social sentiment | Reddit public JSON, StockTwits public JSON | X API ($100/mo, optional) | Already in use |
| Short interest data | — (not free at quality) | Approximated via float turnover ratio | See `squeeze_setup` in §10 |

**Cost:** $0/month for everything required. X (Twitter) Basic API at $100/mo is the only paid option, and it's optional — bot degrades gracefully without it.

---

*End of spec. Proceed to implementation plan via writing-plans skill.*
