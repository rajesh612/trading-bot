# Plan 2: Swing & Signals

**Goal:** Add tier-1 swing/short-term position infrastructure, six new technical signals (parabolic, volume_exhaustion, OBV divergence, bot_herding_score, squeeze_setup, stop_hunt_zone), enhanced research agent (macro shock score, sector sentiment, capitulation detection), and the swing position manager (gap check, EOD hold, tiered TP, capitulation hold).

**Spec:** §6, 7, 9, 10, 14, 20

**Depends on:** Plan 1 complete.

---

## File Map

| Action | File | Responsibility |
|---|---|---|
| Create | `tier1_config.py` | Static dict of 21 tier-1 names + sector/theme; `is_tier1`, `get_sector`, `qualifies_for_swing` |
| Modify | `indicator_engine.py` | 8 new fields on `IndicatorValues`; per-stock signals; cycle-level `bot_herding_score` populated by trading_agent |
| Modify | `research_agent.py` | `_compute_macro_shock_score`, `_compute_sector_sentiment`, `_detect_capitulation_candidate`; write `__meta__` block in research_report.json |
| Create | `swing_manager.py` | Lifecycle: `evaluate_eod_holds()`, `run_gap_check()`, `monitor_swing_positions()`; tiered TP logic |
| Create | `earnings_calendar.py` | Yahoo Finance + Alpaca earnings data; `is_earnings_imminent`, `recently_reported`, `detect_pead_setup` |
| Modify | `config.py` | Plan-2 thresholds: parabolic 5d return, volume exhaustion ratio, squeeze gates, stop-hunt proximities, PEAD thresholds |
| Modify | `main.py` | Add 8:00 AM job: `earnings_calendar.refresh_earnings_calendar(watch_list + tier1)` |
| Modify | `tests/test_foundation.py` | Append unit tests for new functions |

---

## Key Signatures

```python
# tier1_config.py
TIER1_NAMES: dict[str, dict]   # symbol → {sector, theme}
def is_tier1(symbol: str) -> bool
def get_sector(symbol: str) -> str    # returns "OTHER" if not tier-1
def qualifies_for_swing(symbol, composite_score, rsi, volume_ratio,
                        sentiment_score, regime,
                        parabolic, momentum_exhaustion) -> tuple[bool, str]

# indicator_engine.py — appended to IndicatorValues
parabolic: bool
volume_exhaustion: bool
obv_divergence: str        # "bearish" | "bullish" | "none"
momentum_exhaustion: bool   # 2+ of (parabolic, vol_exhaustion, obv=="bearish")
bot_herding_score: int      # 0-100; populated cycle-level
squeeze_setup: bool
squeeze_confidence: int     # 0-100
stop_hunt_zone: bool

def compute_squeeze_setup(float_turnover_3d, price_vs_resistance_pct,
                          volume_surge, obv_rising_with_falling_price) -> tuple[bool, int]
def compute_bot_herding_score(series_dict, spy_closes,
                              buy_signal_count, total_watch, volume_cv) -> int

# research_agent.py
def _compute_macro_shock_score(headlines: list[dict]) -> int    # 0-100
def _compute_sector_sentiment(per_stock_report: dict) -> dict    # sector -> mean score
def _detect_capitulation_candidate(symbol, drawdown_from_20d_high,
                                    sector_sentiment, obv_trend) -> bool

# swing_manager.py
def load_swing_positions() -> dict
def evaluate_tiered_tp_action(position: dict, current_price: float) -> dict
    # returns {action, qty, activate_trailing, reason}
    # action ∈ NONE | CLOSE_TP1 | CLOSE_TP2 | CLOSE_TRAILING | CLOSE_HARD_STOP
def qualifies_for_eod_hold(symbol, is_tier1, regime, event_risk_tomorrow,
                           earnings_in_2_days, current_pnl_pct,
                           existing_swing_count, sector_sentiment_score) -> tuple[bool, str]
def evaluate_eod_holds() -> list[str]                         # called 3:50 PM
def run_gap_check(alpaca_client) -> None                       # called 9:30 AM
def monitor_swing_positions(alpaca_client) -> None             # called every 60s by Plan 3

# earnings_calendar.py
def refresh_earnings_calendar(symbols: list[str]) -> None      # called 8:00 AM daily
def days_until_earnings(symbol: str) -> int | None
def is_earnings_imminent(symbol: str, days: int = 1) -> bool
def recently_reported(symbol: str, days: int = 5) -> dict | None
    # {report_date, eps_actual, eps_estimate, eps_surprise_pct, revenue_beat,
    #  guidance_change, earnings_day_return_pct, earnings_day_volume_ratio,
    #  earnings_day_close_position}
def detect_pead_setup(symbol: str) -> tuple[bool, int]         # (qualifies, score 0-100)
```

## Data Sources (earnings_calendar.py)

| Need | Primary | Fallback |
|---|---|---|
| Next earnings date | Yahoo `quoteSummary?modules=calendarEvents` | Alpaca `get_corporate_action_announcements` |
| EPS actuals + estimates (last 4 Q) | Yahoo `quoteSummary?modules=earningsHistory` | Finnhub free tier |
| Revenue actuals + estimates | Yahoo `quoteSummary?modules=earningsHistory` | — |
| Forward guidance proxy | Yahoo `quoteSummary?modules=earningsTrend` (analyst revision direction) | Treat as `"unknown"` if missing — does not auto-disqualify PEAD |
| Earnings-day price/volume reaction | Alpaca daily bars (already in use) | Yahoo daily bars |

All free, no key required (Yahoo, Alpaca paper). FRED unaffected.

## Persistence Schemas

**`swing_positions.json`** — per symbol:
```
entry_price, qty, entry_time, stop_loss_pct, take_profit_pct,
stop_loss_price, take_profit_price,
tp1_pct, tp1_qty, tp1_hit, tp2_pct, tp2_qty, tp2_hit,
trailing_qty, trailing_stop_active, trailing_high_water,
swing_days_held, max_swing_days, regime_at_entry,
capitulation_hold, hard_deadline, claude_reasoning, order_id
```

**`research_report.json["__meta__"]`** — `{sector_sentiment{}, macro_shock_score, unscheduled_event_risk, timestamp}`.

---

## Tier-1 Names (21)

AI_SEMI: NVDA, AVGO, AMD, MRVL, ASML, AMAT, LRCX, MU
HYPERSCALER: MSFT, GOOGL, AMZN, META
AI_OPTICS: LITE, COHR
DATA_CENTER: VST, CEG, EQIX
GEO_HEDGE: GLD, NEM, LMT, RTX

---

## Tasks

### Task 1: tier1_config.py
Static data + 3 helpers. `qualifies_for_swing` gates: tier-1 only; composite ≥ 25; RSI 50–82 (BULL_TREND) or 50–72 else; volume_ratio ≥ 1.3; sentiment ≥ 20; reject if parabolic AND momentum_exhaustion.

### Task 2: indicator_engine.py — 6 new signals
`_is_parabolic(daily_closes)` — 5-day return ≥ `PARABOLIC_5DAY_RETURN_PCT` (15%). `_is_volume_exhaustion` — at 5d high (within 0.5%) AND volume < 0.8× 20d avg. `_obv_divergence` — over last 5 bars; bearish = new price high + OBV declining; bullish = new low + OBV rising. `_is_stop_hunt_zone` — within 0.4% of round number ($X.00 / $X.50) OR within 0.3% of 5-day hi/lo, plus quick recovery pattern. `compute_squeeze_setup` — 4 weighted conditions summing to 0–100; threshold = `SQUEEZE_CONFIDENCE_GATE`. `compute_bot_herding_score` — combines avg correlation vs SPY, fraction of watch list crossing BUY threshold this bar, volume CV. **Wire into `compute_indicators()`** — populate the 7 per-stock fields; `bot_herding_score` stays 0 here (set by trading_agent at cycle level after all stocks fetched). **Backward compat:** add safe defaults to any direct `IndicatorValues(...)` constructions in existing tests.

### Task 3: research_agent.py
`_compute_macro_shock_score` — keyword-weighted scan of market headlines (tariff/war/Fed-emergency/bank-failure/CPI-shock); cap at 100. `_compute_sector_sentiment` — group `combined_sentiment_score` by `tier1_config.get_sector()`; mean per bucket. `_detect_capitulation_candidate` — drawdown ≤ -20% AND sector_sentiment < -20 AND obv_trend > 0.10. After per-stock loop in `run_research_agent`, write a `__meta__` block with `sector_sentiment`, `macro_shock_score`, `unscheduled_event_risk = (score >= 60)`, timestamp.

### Task 4: swing_manager.py — schema + tiered TP
`_compute_tier_quantities(total_qty)` splits 40/30/30; handles 1-share and 2-share edge cases (no fractional). `evaluate_tiered_tp_action` priority: hard stop (full close) > TP1 > TP2 (activates trailing) > trailing drawdown (3% from high water). Pure function — testable without Alpaca.

### Task 5: swing_manager.py — gap check + EOD qualification
`_evaluate_gap_action`: gap ≤ -stop_loss → CLOSE_GAP_DOWN; gap ∈ [-5%, -2%] → TIGHTEN_STOP to 2%; gap > +3% → TAKE_TP1_GAP_UP. `qualifies_for_eod_hold` — 7 gates from spec §6: tier-1, regime ∈ {BULL_TREND, OPPORTUNITY, CAPITULATION, CRASH_RECOVERY}, no event tomorrow, no earnings within 2 sessions (unless CAPITULATION), current P&L > -2% (PTJ rule), existing_swing_count < SWING_MAX_POSITIONS, sector sentiment ≥ +10.

### Task 6.5: earnings_calendar.py
Daily 8:00 AM job. `refresh_earnings_calendar(symbols)` builds `earnings_calendar.json` keyed by symbol, with `next_earnings_date` and (if reported within 5 trading days) the full `recently_reported` block. **Yahoo first, Alpaca fallback** — keep the network calls per-symbol but cache the whole file daily so subsequent reads are local.

`detect_pead_setup(symbol)` qualifies on **4 of 5** conditions (each = 20 points, threshold 80):
1. `eps_surprise_pct ≥ 5.0`
2. `revenue_beat == True`
3. `guidance_change in {"raised", "maintained", "unknown"}` — `"unknown"` allowed (proxy data not always available); only `"lowered"` disqualifies
4. `earnings_day_return_pct ≥ 3.0` AND `earnings_day_close_position ≥ 0.7`
5. `earnings_day_volume_ratio ≥ 2.0`

`is_earnings_imminent(symbol, days=1)` — boolean lookup against `next_earnings_date`. Conservative: returns False on any error so the bot doesn't get stuck if Yahoo fails.

**Wire `swing_manager._earnings_in_2_days(symbol)` to call `is_earnings_imminent(symbol, days=2)`.**

**Acceptance:** PEAD detection on a known beat (use INTC's last quarter or build a fixture); imminent flag fires within 1 trading day; gracefully returns False on missing data.

### Task 7: swing_manager.py — orchestration
`evaluate_eod_holds()` — iterate `open_positions`, run qualification, on pass: build swing_pos with regime-aware TP tiers (CAPITULATION uses 10/20/12 instead of 5/10/7), `_compute_tier_quantities`, save under `_SWING_LOCK`. Returns promoted symbols. `run_gap_check(alpaca)` — for each swing pos: fetch pre-market price, dispatch `_evaluate_gap_action`, execute via `_execute_partial_close` or `close_position`. `monitor_swing_positions(alpaca)` — for each swing pos: update trailing high water, run `evaluate_tiered_tp_action`, dispatch close. `_execute_partial_close` — submits Alpaca market sell, locked merge-write to update tp1_hit/tp2_hit/trailing_qty; removes position when fully closed.

---

## Worst-Case / Corner Cases

| Risk | Mitigation |
|---|---|
| 1-share or 2-share swing position | `_compute_tier_quantities` returns whole-share split that still sums to total |
| Trailing stop never activates | Only after `tp2_hit`; before that, stop_loss_pct governs |
| Concurrent TP1 hit + monitor fires twice | `_SWING_LOCK` + locked merge-write |
| Swing position remains during 3:55 PM force-close | Plan 3 modifies `force_close_all` to skip symbols in `swing_positions.json` |
| Pre-market price unavailable | `_evaluate_gap_action` exception caught at orchestration layer; logged, position untouched |
| Earnings calendar not yet wired | `_earnings_in_2_days` stub returns False; capitulation hold can still open |
| Capitulation hold runs to deadline | `hard_deadline = entry_time + (max_swing_days)`; monitor closes when reached regardless of P&L |
| Sector_sentiment missing for OTHER sector | `_load_research_meta` falls back to `{}`; qualification computes 0 → fails the +10 gate |
| OBV divergence on flat price | Returns "none" (the `price_high` / `price_low` checks both fail) |
| Bot herding metric across thinly-traded stocks | Volume CV check requires actual volume; if all zero, vol_score = 0 (fails open) |
| Squeeze score crosses 70 from one signal alone | Cannot — max single-signal weight is 30; needs ≥ 3 signals |
| Stop-hunt false positive every bar near round numbers | One-bar cooldown handled in Plan 3 (`_check_entry_gates`) — re-evaluates next bar |
| Capitulation hold deepens 12% loss | Stop fires; loss capped; learning agent classifies as "sector_narrative" or "fundamental" |
| Yahoo earnings endpoint changes / 403 | `refresh_earnings_calendar` per-symbol try/except; missing entries treated as "unknown earnings date" → conservative behavior (no imminent gate, no PEAD) |
| PEAD detection misses a beat (data gap) | Setup simply doesn't trigger — equivalent to current behavior. No false positives; any false negatives are acceptable. |
| Guidance proxy unreliable | "unknown" treated as neutral so PEAD can still trigger on the other 4 signals — only explicit "lowered" disqualifies |
| Earnings hits during open swing position | `monitor_swing_positions` reads `is_earnings_imminent`; if True and not CAPITULATION mode, closes the position end-of-day before earnings |
| Concurrent earnings refresh + read | `earnings_calendar.json` is read-only after 8:00 AM job; readers tolerate missing/stale → False |

---

## Out of Scope

- Wiring counter-bot signals into trading decisions (Plan 3 `_check_entry_gates`)
- Wiring swing_manager entry points into scheduler (Plan 3)
- Earnings calendar feed (stub stays in place; future enhancement)
