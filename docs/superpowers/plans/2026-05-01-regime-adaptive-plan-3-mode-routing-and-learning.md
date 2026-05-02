# Plan 3: Mode Routing & Learning

**Goal:** Wire Plans 1+2 into the trading and learning agents. Add mode-aware routing (Day/Hybrid/Short-Term/Cash), regime-aware SL/TP, event-risk gate, flash-crash circuit breaker, counter-bot routing (squeeze/herding/stop-hunt), API budget governor enforcement, per-regime learning, source-of-loss classification, and bounded threshold tuning.

**Spec:** §11, 12, 13, 15, 18, 19, 20

**Depends on:** Plans 1 and 2 complete.

---

## File Map

| Action | File | Responsibility |
|---|---|---|
| Modify | `trading_agent.py` | mode routing, regime-aware SL/TP table, rule-based pre-filter, entry gates, counter-bot adjustments, API budget integration, flash-crash monitor, lockout enforcement |
| Modify | `main.py` | jobs: `run_market_monitor` (every 5 min), `run_gap_check` (9:30 AM), `evaluate_eod_holds` (3:50 PM); wire `monitor_swing_positions` into existing `run_position_monitor` |
| Modify | `learning_agent.py` | per-regime aggregation, bounded threshold clamping, source-of-loss classifier, regime context into Claude prompt |
| Modify | `tests/test_foundation.py` | append unit tests |

---

## Key Signatures

```python
# trading_agent.py
def get_active_mode(regime: str) -> str
    # HYBRID for {BULL_TREND, OPPORTUNITY}; SHORT_TERM for {CAPITULATION, CRASH_RECOVERY};
    # CASH for CRISIS; DAY_TRADE otherwise (incl. BULL_TREND_PARABOLIC, NORMAL, CHOPPY, DEFENSIVE).
def get_sl_tp_for_mode_and_regime(mode: str, regime: str) -> tuple[float, float]
def compute_position_size_for_mode(portfolio_value, price, atr, mode, regime,
                                    bot_herding_score, squeeze_setup) -> int
def _prefilter_decision(composite_score, top_indicators_aligned) -> str
    # AUTO_SKIP | AUTO_BUY | ASK_CLAUDE
def _check_entry_gates(symbol, regime, stop_hunt_zone, momentum_exhaustion,
                       squeeze_setup, bot_herding_score) -> tuple[bool, str]
def run_market_monitor() -> None    # called every 5 min — flash-crash check
def _estimate_batch_call_cost(batch_size, system_prompt_tokens=1500,
                              strategy_memory_tokens=400) -> float

# learning_agent.py
def _aggregate_by_regime(closed_trades) -> dict
def _classify_loss_source(trade) -> str
    # market_structure | fundamental | sector_narrative | momentum_exhaustion |
    # macro_pressure | unknown
def _clamp_threshold(key: str, value) -> value    # respects _THRESHOLD_BOUNDS
```

## Routing Tables

**Mode by regime:**
| Regime | Mode |
|---|---|
| BULL_TREND, OPPORTUNITY | HYBRID (swing tier-1 + day others) |
| CAPITULATION, CRASH_RECOVERY | SHORT_TERM |
| CRISIS | CASH (no entries) |
| NORMAL, CHOPPY, DEFENSIVE, BULL_TREND_PARABOLIC | DAY_TRADE |

**SL/TP by (mode, regime):**
| Mode | Regime | SL% | TP% |
|---|---|---|---|
| DAY_TRADE | NORMAL | 3 | 5 |
| DAY_TRADE | CHOPPY | 5 | 6 |
| DAY_TRADE | DEFENSIVE | 4 | 5 |
| DAY_TRADE | BULL_TREND_PARABOLIC | 3 | 5 |
| HYBRID | BULL_TREND, OPPORTUNITY | 7 | 15 |
| SHORT_TERM | CAPITULATION | 12 | 20 |
| SHORT_TERM | CRASH_RECOVERY | 10 | 18 |

**Position size by mode (% of portfolio per position):**
DAY_TRADE 5%, HYBRID 3% (swing), SHORT_TERM 2.5%. Combined max exposure 36%.

**Bounded thresholds (`_THRESHOLD_BOUNDS`):**
`choppy_composite_gate (25, 40)`, `bull_trend_rsi_max_tier1 (76, 88)`, `swing_max_days (3, 7)`, `capitulation_drawdown_threshold (15, 30)`, `squeeze_confidence_gate (60, 80)`, `herd_size_reduction (0.15, 0.50)`.

---

## Tasks

### Task 1: trading_agent.py — mode + SL/TP tables
Add `get_active_mode`, `get_sl_tp_for_mode_and_regime`, `_HYBRID_REGIMES`, `_SHORT_TERM_REGIMES`, `_SL_TP_TABLE`. Pure functions. Fall back to `(STOP_LOSS_PCT, TAKE_PROFIT_PCT)` config defaults on unknown combos.

### Task 2: trading_agent.py — pre-filter + entry gates
`_prefilter_decision`: composite < 10 → AUTO_SKIP; composite > 35 AND `_top_indicators_aligned` (price > VWAP, RSI > 50, MACD hist > 0) → AUTO_BUY; else ASK_CLAUDE. `_check_entry_gates` priority: event_risk_window (FOMC/NFP) > stop_hunt_zone > bot_herding > BOT_HERD_HIGH (no squeeze override) > momentum_exhaustion (no squeeze override). Squeeze setup short-circuits the herding and exhaustion gates.

### Task 3: trading_agent.py — position sizing
`compute_position_size_for_mode` — risk-based (portfolio × 0.5% / ATR) capped by mode % cap. If `bot_herding_score > BOT_HERD_ALERT_SCORE` AND not `squeeze_setup`, multiply by 0.70. Returns int ≥ 1.

### Task 4: trading_agent.py — Claude budget integration + cache TTL fix
`_estimate_batch_call_cost(batch_size)` — quick token-cost estimate (Sonnet 4.6 pricing). In `_ask_claude_batch`: BEFORE the API call, run `api_budget.check_budget(estimated, portfolio_gain_pct, regime)`; on False, return `_default_hold` for the batch. AFTER successful response, call `api_budget.record_usage` from `response.usage` (handle missing `cache_creation_input_tokens`/`cache_read_input_tokens` gracefully — Anthropic SDK fields). **Cache TTL fix:** change all `cache_control: {"type": "ephemeral"}` to `{"type": "ephemeral", "ttl": "1h"}` so cache survives across 10-min cycles. Add `_get_portfolio_intraday_gain` and `_get_current_regime` helpers (best-effort, default 0.0 / "NORMAL").

### Task 5: trading_agent.py — wire pre-filter + gates into run_trading_cycle
Replace existing Phase 2 filter loop. Three buckets: `auto_buy_candidates`, `needs_claude`, skipped. After Claude batch loop, synthesise `decisions[symbol]` for auto-buys (action=BUY, confidence=75, reasoning="Auto-BUY", SL/TP=None — let regime defaults fill). In Phase 4 BUY branch, BEFORE position sizing: run `_check_entry_gates` (skip on False); resolve `mode = get_active_mode(regime)`; if CASH → skip; compute `effective_gate` (squeeze=12 < herd_alert=HERD_ENTRY_GATE < choppy=CHOPPY_COMPOSITE_GATE < default=COMPOSITE_SCORE_THRESHOLD) and skip if composite below it. Replace position sizing with `compute_position_size_for_mode` × `pos_mult`. Replace SL/TP resolution with `get_sl_tp_for_mode_and_regime` defaults; squeeze override → SL=6, TP=5 (fast trailing).

### Task 6: trading_agent.py — flash crash monitor
`run_market_monitor()` — single SPY price fetch + `check_flash_crash`. On True: `force_close_all()`, also close all swing positions via `swing_manager`, set `flash_crash_active=True` and `flash_crash_until = now + 48h` in macro_regime.json. `_get_spy_prev_close` reuses Yahoo; `_set_crisis_lockout(hours=48)` writes the regime file. Cheap function (one HTTP call) — safe to run every 5 min.

### Task 7: trading_agent.py — regime + lockout integration in run_trading_cycle
Replace `regime_info = load_vix_regime()` with `load_macro_regime()`. Compute `flash_crash_in_effect` from `flash_crash_active + flash_crash_until` (parse ISO; safe-default False). `can_open = False` if regime=="CRISIS" OR `flash_crash_in_effect`. Existing daily-loss circuit breaker (3% MAX_DAILY_LOSS_PCT) preserved unchanged.

### Task 8: main.py — schedule wiring + position monitor extension
Add three jobs (cron, ET, mon–fri):
- 9:30 AM `swing_manager.run_gap_check(AlpacaClient())` (lambda)
- Every 5 min, hours 9–15 `trading_agent.run_market_monitor`
- 3:50 PM `swing_manager.evaluate_eod_holds`

In `trading_agent.force_close_all()`: skip symbols present in `swing_manager.load_swing_positions()` so promoted holds survive 3:55 PM. In `trading_agent.run_position_monitor()`, append a try/except call to `swing_manager.monitor_swing_positions(alpaca)` so swings get their tiered TP/trailing checked at 60s cadence too.

### Task 9: learning_agent.py — per-regime aggregation
`_aggregate_by_regime(closed_trades)` — group by `vix_regime` field (already written in trade journal). Output per regime: `count, winners, losers, avg_pnl_pct, win_rate, total_pnl_dollar`. `_classify_loss_source(trade)` priority: VIX > 35 OR regime=="CRISIS" → market_structure; entry_indicators.momentum_exhaustion → momentum_exhaustion; close_reason contains earnings/guidance/fundamental → fundamental; entry_macro_shock_score ≥ 60 → macro_pressure; entry_sector_sentiment ≤ -25 → sector_narrative; else unknown.

### Task 10: learning_agent.py — bounded clamping + Claude context
Inject regime aggregation + loss-source breakdown into Claude prompt as a context block before the request. After Claude returns new params, iterate keys and `_clamp_threshold(key, value)` before saving. Hard bounds stop Claude from disabling stops or removing entire regimes.

### Task 11: integration smoke
Run full `test_foundation.py` and `test_agents.py -m unit` — must pass. `timeout 120 python main.py | head -120` shows all 6 new jobs registered (3 from Plan 1 + 3 from Plan 3). One-shot `run_trading_cycle()` off-hours — should log regime, prefilter outcomes, budget log; no exceptions.

---

## Worst-Case / Corner Cases

| Risk | Mitigation |
|---|---|
| Claude API outage during cycle | Existing `_claude_disabled` circuit breaker preserved; auto-buy candidates still go through (rule-based) |
| API budget exhausted mid-day | `check_budget` returns False → `_default_hold` for remaining batches; positions still managed by Alpaca brackets |
| Cache TTL fix breaks Anthropic SDK | Fall back to no-TTL `ephemeral` if SDK rejects; cost rises but bot keeps running |
| Auto-BUY misfire (composite > 35 but bad setup) | `_check_entry_gates` runs after pre-filter — stop_hunt/event/herding all still apply |
| Squeeze override pushes through bad entry | Tighter exits (SL=6, TP=5, fast trailing) cap the damage; require confidence ≥ 70 to trigger at all |
| Flash crash false positive (e.g. SPY data glitch) | 48-hour lockout is conservative but reversible — manually clear `flash_crash_active` in macro_regime.json |
| Mode mis-routes high-volume tier-1 to DAY_TRADE | OK — DAY_TRADE still works for tier-1; HYBRID just unlocks the swing path |
| Swing position closed by 3:55 PM force-close | `force_close_all` skips symbols in swing_positions.json |
| Position monitor + swing monitor double-close | Locked merge-write on both files; load → mutate → save under lock |
| Learning agent removes a stop loss | `_THRESHOLD_BOUNDS` clamp prevents — can only adjust within hard ranges |
| Alpaca partial close fails after TP1 fires | Logged; swing pos unchanged; next monitor cycle retries |
| Pre-filter AUTO_BUY count explodes (e.g. 30 entries) | Daily exposure cap (36%) + per-mode position count caps (DAY 5, swing 2, short 2) |
| Concurrent processes after restart | Existing `fcntl.flock` instance lock + `_POSITIONS_LOCK` + new `_SWING_LOCK` |
| run_market_monitor adds API cost | One Alpaca call per 5 min (free) + zero Claude calls — negligible |
| Regime flips mid-cycle (BULL_TREND → CHOPPY) | Cycle uses snapshot read at start; next cycle picks up new regime within 10 min |
| Learning agent prompt grows with closed trades | Existing `_TRADE_SUMMARY_KEYS` already strips full indicator snapshots; capped naturally |

---

## Final Cost Targets

| Scenario | Daily Claude spend |
|---|---|
| Single instance, no optimisation | ~$1.00 |
| With Plan 3 optimisations (batching, prefilter, 1-h cache) | ~$0.30–0.55 |
| Elevated ceiling day (high gain or CRISIS) | ≤ $1.50 (hard cap) |
