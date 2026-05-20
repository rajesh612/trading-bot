# Swing-First Redesign + Parameter Hardening

**Date:** 2026-05-20  
**Status:** Approved for implementation

---

## Background & Motivation

30-day backtest (174 trades, 2026-04-09 – 2026-05-19) and live-journal analysis expose a structural mismatch:

| Engine | Trades | Net $ | Avg $/trade | Win rate |
|---|---|---|---|---|
| Day trades | 157 | -$2,224 | -$14 | 41% |
| Swing trades | 17 | +$1,574 | +$93 | 47% |

- **55% of all trades force-closed at 3:55pm** at avg -0.28% — never reaching SL or TP.
- SL fires more than TP (19% vs 17%): intraday 5-min noise tags the closer level first.
- Composite floor too low: live trades with composite < 50 → 0–22% win rate; ≥ 50 → 56% win rate.
- Stocks > $500: -$55/trade avg at 28% win rate. LITE alone: -$709 over 10 trades.

The swing engine is already built. It is under-used because the 7-gate AND logic in `qualifies_for_eod_hold` almost always rejects candidates, and `SWING_MAX_POSITIONS = 2` fills immediately.

---

## Core Principle: Asymmetric Entry / Exit

**Entry: AND of all signals — be ruthlessly strict.**  
The cost of a false-positive entry is a losing trade. Every available signal (technicals, sector performance, news, earnings calendar, economic calendar, regime) should vote before opening a position.

**Exit / swing-hold: OR of thesis-break vetoes — be patient.**  
The cost of exiting on noise is leaving a winner early (the 55% force-close problem). Only exit when the *specific reason you entered is no longer true*: regime collapsed to CRISIS, earnings binary risk overnight, thesis invalidated by fundamental news, or the position is already deep underwater.

This replaces the current backwards design: loose entry (composite ≥ 30) + strict exit (7-gate AND).

---

## Scope

Five change areas, all Phase 1:

1. **Asymmetric entry gates** — add sector, news-quality, and economic-calendar-day gates at entry
2. **Swing-first hold logic** — replace 7-gate AND with regime-aware thesis-break vetoes
3. **Composite floor raise** — NORMAL 30 → 50; HIGH_VOL 35 → 55; CHOPPY 35 → 55
4. **Entry price cap** — new `MAX_ENTRY_PRICE = 400`
5. **Ticker prune** — remove 8 persistent losers from `tickers.json`

Phase 2 (scaled entry / buy-the-dip) is out of scope.

---

## Regime Matrix

The 9 regimes drive different entry strictness and swing-hold tolerance. This table is the single source of truth for both.

### Regime definitions (existing, no change)

| Regime | Trigger condition | Mode |
|---|---|---|
| CRISIS | VIX > 35 or SPY 5d < -10% | CASH |
| CRASH_RECOVERY | Post-CRISIS + 2+ green days | SHORT_TERM |
| BULL_TREND | 3+ consecutive green days + health ≥ threshold | HYBRID |
| OPPORTUNITY | DEFENSIVE conditions + AI/semi sector score > 15 | HYBRID |
| CHOPPY | 3+ sign alternations in 5d, net move < 0.5% | DAY_TRADE |
| DEFENSIVE | 3+ red days + health ≤ threshold + bot PnL < -1% | DAY_TRADE |
| NORMAL | Default (no other condition matches) | DAY_TRADE |
| CAPITULATION | OBV divergence + drawdown (externally detected) | SHORT_TERM |

### Entry rules by regime (new)

| Regime | Entries | Composite min | Sector gate | News gate | Economic calendar | Swing eligible |
|---|---|---|---|---|---|---|
| **CRISIS** | **BLOCKED** | N/A | N/A | N/A | N/A | NO |
| **CHOPPY** | Day-trade only | **55** (raised from 35) | ≥ -10 (not collapsing) | None | Normal 60-min window | NO |
| **DEFENSIVE** | Day-trade only | 52.5 (existing mult) | ≥ 0 | ≥ 40 | Normal 60-min window | NO |
| **NORMAL** | Swing default | **50** (raised from 30) | ≥ 0 | ≥ 50 for swing | Block day-of major event | YES |
| **BULL_TREND** | Swing default | **45** (slightly relaxed — trend has momentum) | ≥ 0 | ≥ 40 | Normal 60-min window | YES |
| **OPPORTUNITY** | Swing default | **50** | ≥ **10** (sector must be positive — thesis requires it) | ≥ 55 | Normal 60-min window | YES |
| **CAPITULATION** | Swing default | **55** + RSI ≤ 35 (oversold confirmation) | Any | None (panic = no news) | Ignore | YES |
| **CRASH_RECOVERY** | Swing default | **50** | Any | None | Normal 60-min window | YES |

**Composite min in CHOPPY:** `COMPOSITE_MIN_HIGH_VOL` (55) applies — existing `effective_composite_gate()` already routes CHOPPY to HIGH_VOL_GATES, so raising `COMPOSITE_MIN_HIGH_VOL` 35 → 55 handles this automatically.

**BULL_TREND composite at 45:** BULL_TREND uses HYBRID mode (SL 7% / TP 15%). The wider TP justifies slightly lower composite bar — trend entries need less conviction because the holding window is longer. Implemented as a new constant `COMPOSITE_MIN_BULL_TREND = 45` read inside `effective_composite_gate()`.

**Day-of major event block (NORMAL):** Economic calendar already tracks FOMC/CPI/NFP. New gate: if `is_event_risk_window()` returns true for the *current session morning* (not just within 60 min), block new swing-eligible entries for the whole day. Day-trade entries in CHOPPY/DEFENSIVE unaffected — their shorter hold doesn't carry overnight event risk.

### Swing-hold veto rules by regime (new)

At 3:50pm `evaluate_eod_holds()` evaluates each open position. The current entry_sector_sentiment and entry_news_sentiment are stored in the position record at entry time (new field: `entry_sector_sentiment`, `entry_news_sentiment`).

| Regime at 3:50pm | Hard vetoes (force-close) | Rationale |
|---|---|---|
| **CRISIS** | Always close — no exceptions | Capital preservation |
| **CHOPPY** | No new swing promotions. Existing swings: PTJ rule only (PnL < -2%) | Short-term oscillation doesn't break multi-day thesis |
| **DEFENSIVE** | No new swing promotions. Existing swings: PTJ rule + earnings tomorrow | Regime shift ≠ thesis break for existing positions |
| **NORMAL** | Earnings tomorrow + CRISIS + PnL < -2% + sector reversed (< -20, was > +10 at entry) | Core regime; sector reversal = thesis break |
| **BULL_TREND** | CRISIS + earnings tomorrow + PnL < **-3%** (wider — SL is 7%) | Trend intact; only hard structural breaks veto |
| **OPPORTUNITY** | CRISIS + earnings tomorrow + PnL < -2% + sector score < 0 (was > 10 at entry) | Thesis requires sector health; sector going negative = thesis break |
| **CAPITULATION** | CRISIS + PnL < **-5%** (SL is 12%) + VIX declining sharply (recovery → CRASH_RECOVERY regime shift means exit the cap-trade) | Capitulation holds are speculative; keep unless VIX collapses |
| **CRASH_RECOVERY** | CRISIS + PnL < **-3%** + 3+ consecutive red days (recovery stalled) | Recovery thesis fails if market resumes decline |

**Regime shift mid-hold:** If a position was entered in NORMAL and regime shifts to DEFENSIVE or CHOPPY during the hold, existing swing is NOT force-closed on the regime shift alone. Only the veto rules for the *current* regime at 3:50pm apply. Exception: CRISIS always closes everything immediately (handled in `trading/cycle.py` already).

**"Sector reversed" definition:** At entry, `entry_sector_sentiment` is stored. At exit eval: if current sector_sentiment < -20 AND entry value was > +10, veto. If sector was already negative at entry (CAPITULATION, CRASH_RECOVERY), no sector veto applies.

---

## Detailed Changes

### 1. Asymmetric entry gates

**New gates added to `signals/entry_gates.py` → `check_all_gates()`** (pure, no I/O):

**A. Sector performance gate**
```python
if iv.sector_sentiment is not None and iv.sector_sentiment < config.SECTOR_ENTRY_MIN_SCORE:
    # soft flag — not a hard block; Claude sees it as context
    soft_flags.append(f"sector sentiment {iv.sector_sentiment:.0f} < {config.SECTOR_ENTRY_MIN_SCORE}")
```
`SECTOR_ENTRY_MIN_SCORE = 0` — sector must not be net negative. Passed as soft flag (not hard block) so Claude can override with strong stock-specific catalyst.

**B. News quality gate for swing candidates**
```python
if snap.swing_eligible and iv.sentiment_score < config.SWING_NEWS_MIN_SCORE:
    soft_flags.append(f"swing: sentiment {iv.sentiment_score} < {config.SWING_NEWS_MIN_SCORE}")
```
`SWING_NEWS_MIN_SCORE = 50` — swing positions need a catalyst. In CAPITULATION/CRASH_RECOVERY, this gate is skipped (panic markets have no readable news).

**C. Economic calendar day-of block (new hard gate for swing-eligible in NORMAL)**
```python
if snap.regime == "NORMAL" and snap.swing_eligible and snap.major_event_today:
    return GateResult(False, "major economic event today — no new swing entries")
```
`snap.major_event_today` is a bool derived from economic_calendar at cycle start (added to `CycleSnapshot`). Only blocks *new* swing-eligible entries in NORMAL; doesn't affect CHOPPY/DEFENSIVE day-trades.

**D. Price cap gate** (hard block)
```python
if config.MAX_ENTRY_PRICE and iv.current_price > config.MAX_ENTRY_PRICE:
    return GateResult(False, f"price ${iv.current_price:.0f} > MAX_ENTRY_PRICE ${config.MAX_ENTRY_PRICE}")
```

### 2. Entry fields stored at position creation

`trading/executor.py` → `execute_buy()`: add two fields to the position record:
- `entry_sector_sentiment`: sector_sentiment score at entry time (from `CycleSnapshot.research`)
- `entry_news_sentiment`: overall sentiment score at entry time

These are needed by EOD hold logic to detect thesis reversal (current vs entry sector sentiment).

### 3. Swing-first hold logic

**File:** `trading/eod_holds.py` → `qualifies_for_eod_hold()`

New signature — regime-aware, thesis-driven:
```python
def qualifies_for_eod_hold(
    symbol: str,
    regime: str,
    earnings_tomorrow: bool,
    current_pnl_pct: float,
    entry_sector_sentiment: float,
    current_sector_sentiment: float,
    vix_consecutive_decline: bool,   # for CAPITULATION exit
    consecutive_red_days: int,       # for CRASH_RECOVERY exit
) -> tuple[bool, str]:
```

Logic per regime as specified in the matrix above. Removes: `is_tier1`, `event_risk_tomorrow`, `existing_swing_count` from the pure function (count check stays in `_promote_to_swing()` under lock).

### 4. Composite floor and BULL_TREND constant

**File:** `config.py`

| Constant | Old | New |
|---|---|---|
| `COMPOSITE_MIN_NORMAL` | 30 | **50** |
| `COMPOSITE_MIN_HIGH_VOL` | 35 | **55** |
| `COMPOSITE_MIN_BULL_TREND` | *(new)* | **45** |
| `AUTO_EXECUTE_COMPOSITE_MIN` | 65 | **70** |
| `ENTRY_LATE_COMPOSITE_MIN` | 50 | **55** |
| `BACKTEST_SWING_COMPOSITE_MIN` | 40 | **50** |
| `SWING_MAX_POSITIONS` | 2 | **6** |
| `MAX_ENTRY_PRICE` | *(new)* | **400** |
| `SECTOR_ENTRY_MIN_SCORE` | *(new)* | **0** |
| `SWING_NEWS_MIN_SCORE` | *(new)* | **50** |
| `MIN_MARKET_CAP_B` | *(new)* | **25** |

**File:** `signals/entry_gates.py` → `effective_composite_gate()`: add BULL_TREND branch returning `config.COMPOSITE_MIN_BULL_TREND`.

### 5. CycleSnapshot — add major_event_today

**File:** `trading/cycle_context.py` → `CycleSnapshot`: add field `major_event_today: bool`.  
**File:** `trading/cycle.py` → `run_trading_cycle()`: populate from economic calendar before building snapshot.

### 6. Dynamic screener — market cap filter (replaces static ticker prune)

Static ticker pruning is the wrong tool: a stock that lost in one regime (MU in DEFENSIVE, ZS during rotation) is a valid swing candidate in another (MU post-earnings recovery, ZS in BULL_TREND). Permanently banning tickers punishes companies for a bad month. The `MAX_ENTRY_PRICE = $400` cap already eliminates the structural price-level losers (LITE, COHR).

Instead, add a **market cap pre-filter** to `agents/volatility_screener.py` so low-quality names never reach the watch list in the first place.

**New constant:** `MIN_MARKET_CAP_B = 25` ($ billions)

**Why $25B:** Large-cap threshold. Includes all established institutional-grade names (NVDA, AAPL, MSFT, PLTR, MRVL, NET, APP, MU, QCOM, SHOP, ARM, RBLX all qualify at current market caps). Excludes micro/small-caps that are too thin and manipulable for the bot's SL/TP structure. Large-caps also have stronger fundamentals coverage, analyst consensus, and liquidity — all of which the Claude watch-list agent already reasons about.

**Implementation:** In `agents/volatility_screener.py`, after fetching ticker metadata, filter out any symbol whose market cap is below `config.MIN_MARKET_CAP_B * 1e9`. Market cap is available from Yahoo Finance via `yfinance` (`ticker.info["marketCap"]`). Fetch in batch with a single call per ticker (already done for other screener fields). Symbols that fail the cap check are dropped before the top-50 ranking step.

**Follow-on (Phase 2, out of scope now):** Add sector relative-strength filter — only include tickers whose sector ETF is outperforming SPY over 5 days. This wires the existing `sector_scores` from `macro_regime.json` into the screener.

---

## SL/TP Table (existing — no change, shown for completeness)

| Mode | Regime | SL% | TP% |
|---|---|---|---|
| DAY_TRADE | NORMAL | 3.0 | 5.0 |
| DAY_TRADE | CHOPPY | 5.0 | 6.0 |
| DAY_TRADE | DEFENSIVE | 4.0 | 5.0 |
| HYBRID | BULL_TREND | 7.0 | 15.0 |
| HYBRID | OPPORTUNITY | 7.0 | 15.0 |
| SHORT_TERM | CAPITULATION | 12.0 | 20.0 |
| SHORT_TERM | CRASH_RECOVERY | 10.0 | 18.0 |
| PEAD | any | 8.0 | 8.0 |

Swing overnight SL: `SWING_OVERNIGHT_SL_PCT = 3.0%` (existing, no change). TP tiers: `SWING_TP1_PCT = 5.0%`, `SWING_TP2_PCT = 10.0%` (no change).

---

## File Change Summary

| File | Change |
|---|---|
| `config.py` | 7 constant changes + 4 new constants |
| `signals/entry_gates.py` | 4 new gates (sector, news-quality, event-day, price-cap); BULL_TREND branch in `effective_composite_gate()` |
| `trading/cycle_context.py` | Add `major_event_today: bool` to `CycleSnapshot` |
| `trading/cycle.py` | Populate `major_event_today` from economic calendar |
| `trading/executor.py` | Store `entry_sector_sentiment` + `entry_news_sentiment` in position record |
| `trading/eod_holds.py` | `qualifies_for_eod_hold()`: new signature + regime-aware veto matrix |
| `trading/eod_holds.py` | `evaluate_eod_holds()`: pass new args; remove old args |
| `agents/volatility_screener.py` | Add market-cap pre-filter (drop symbols < $25B) |
| `tests/test_entry_gates.py` | Tests for all 4 new gates |
| `tests/test_signals.py` | Update composite-threshold tests for new floors |
| `tests/test_foundation.py` | New tests for regime-aware veto matrix (8 regimes × key veto paths) |
| `tests/test_pipeline.py` | Test `major_event_today` propagation in `CycleSnapshot` |
| `tests/test_core.py` | Test market-cap filter in screener |

---

## Testing Requirements

**Entry gates:**
- Price > $400 → blocked; price ≤ $400 → passes; `MAX_ENTRY_PRICE = 0` → gate disabled
- Sector < 0 → soft flag present in result; sector ≥ 0 → no soft flag
- `swing_eligible=True` + sentiment < 50 → soft flag; CAPITULATION regime → gate skipped
- `major_event_today=True` + NORMAL + swing_eligible → hard block; CHOPPY → no block

**Composite floor:**
- BULL_TREND + composite 46 → passes (≥ 45); composite 44 → blocked
- CHOPPY + composite 54 → blocked (< 55); composite 56 → passes

**Swing-hold veto matrix:**
- CRISIS regime → always veto regardless of PnL
- CHOPPY + PnL -1.5% → passes (PTJ threshold -2%); PnL -2.5% → veto
- NORMAL + sector reversed (entry +15, current -25) → veto; sector soft drop (entry +15, current +5) → no veto
- BULL_TREND + PnL -2.9% → passes (threshold -3%); PnL -3.1% → veto
- OPPORTUNITY + sector current < 0 (was > 10) → veto
- CAPITULATION + PnL -4.9% → passes; PnL -5.1% → veto
- CRASH_RECOVERY + 3 consecutive red days → veto; 2 red days → no veto
- Non-tier1 symbol → no veto (tier1 gate removed)
- Event risk tomorrow (not earnings) → no veto (removed)

**Screener market-cap filter:**
- Symbol with marketCap $30B → included; $20B → excluded; `MIN_MARKET_CAP_B = 0` → filter disabled
- Missing marketCap data → symbol excluded (fail-safe, not fail-open)

**Backtest regression:** 30d re-run after implementation; swing % expected to rise from 10% → 25%+ of trades.

---

## Risk & Rollback

| Risk | Mitigation | Emergency rollback |
|---|---|---|
| Higher composite floor → zero-trade days | Expected; quality over quantity | Lower `COMPOSITE_MIN_NORMAL` to 40 in config |
| Swing-first increases overnight exposure | PTJ rule (-2%), `SWING_OVERNIGHT_SL_PCT = 3%`, CRISIS hard-veto | Set `SWING_MAX_POSITIONS = 0` in config |
| Sector/news gates reduce entries too much | Both are soft flags — Claude still decides | Set `SECTOR_ENTRY_MIN_SCORE = -999`, `SWING_NEWS_MIN_SCORE = 0` |
| Market cap filter too aggressive → few candidates | $25B covers ~500 US stocks; watch list has enough | Lower `MIN_MARKET_CAP_B` to 10 in config |
