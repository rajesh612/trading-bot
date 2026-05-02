# Plan 1: Foundation

**Goal:** Replace 3-state `vix_regime.py` with 9-state `macro_regime.py`, add `economic_calendar.py` event gate, add `api_budget.py` daily spend governor. No trading-behavior change yet.

**Spec:** `docs/superpowers/specs/2026-05-01-regime-adaptive-hybrid-strategy-design.md` §3, 4, 5, 8, 13, 14, 17, 19

**Depends on:** —

---

## File Map

| Action | File | Responsibility |
|---|---|---|
| Modify | `config.py` | Add ~20 constants (paths, regime thresholds, budget caps, herding/squeeze gates) |
| Create | `macro_regime.py` | Yahoo signals → health score → 9-state regime; FRED cache; flash crash check |
| Create | `economic_calendar.py` | Hardcoded recurring events + ForexFactory scrape; `is_event_risk_window()` |
| Create | `api_budget.py` | Daily token spend tracker; dynamic $1.00/$1.50 ceiling |
| Modify | `main.py` | Add jobs at 8:30 AM (calendar), 8:55 AM (regime, replaces vix), 4:00 PM (FRED) |
| Create | `tests/test_foundation.py` | Unit tests, marker `-m unit` |

---

## Key Signatures (lock these — Plans 2/3 depend on them)

```python
# macro_regime.py
def fetch_macro_regime() -> dict        # writes macro_regime.json, returns it
def load_macro_regime() -> dict          # cached read; defaults to NORMAL
def check_flash_crash(spy_current: float, spy_prev_close: float) -> bool
def refresh_fred_cache() -> None         # writes fred_cache.json
def compute_macro_health_score(signals: dict, fred_modifier: int = 0) -> int  # [-100, 100]
def classify_regime(macro_health_score, consecutive_green_days, consecutive_red_days,
                    vix_current, spy_5day_return, is_choppy,
                    ai_semi_sector_score, bot_net_pnl_pct, post_crisis_recovery) -> str

# economic_calendar.py
def refresh_economic_calendar() -> None
def is_event_risk_window(now: datetime) -> tuple[bool, str]    # (risk, event_name)
def get_todays_events() -> list[dict]    # high-impact only

# api_budget.py
def load_budget() -> dict
def check_budget(estimated_call_cost: float,
                 portfolio_gain_pct: float = 0.0, regime: str = "NORMAL") -> bool
def record_usage(input_tokens, output_tokens,
                 cache_write_tokens=0, cache_read_tokens=0) -> None
def get_ceiling(portfolio_gain_pct: float = 0.0, regime: str = "NORMAL") -> float
```

## Persistence Schemas

**`macro_regime.json`** — keys: `regime, macro_health_score, sector_scores{AI_SEMI, BROAD_TECH, CYBERSECURITY}, consecutive_green_days, consecutive_red_days, spy_5day_sign_changes, vix_current, vix_90d_avg, vix_trend, spy_5day_return, flash_crash_active, flash_crash_until, event_risk_today, event_risk_details, capitulation_candidates, timestamp, reason`. Backward-compat: existing code reads `regime` only.

**`api_budget.json`** — keys: `date, calls_made, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens, estimated_cost_usd, ceiling_usd, ceiling_mode, budget_exhausted`. Resets when `date != today_ET`.

**`economic_calendar.json`** — `{date, events: [{time, name, impact}], source}`. 24-hr TTL.

**`fred_cache.json`** — `{fed_funds_rate, fed_direction, cpi_trend, unemployment_rising, fred_modifier, updated_at}`. `fred_modifier ∈ {-5, -3, 0, 5}`.

---

## Tasks

### Task 1: config.py constants
Add: file paths (`MACRO_REGIME_FILE`, `FRED_CACHE_FILE`, `ECONOMIC_CALENDAR_FILE`, `API_BUDGET_FILE`, `SWING_POSITIONS_FILE`); `FRED_API_KEY` env; `MACRO_HEALTH_BULL/BEAR_THRESHOLD = ±20`; `CONSECUTIVE_BULL/BEAR_DAYS = 3`; `CHOPPY_MIN_SIGN_ALTS = 3`, `CHOPPY_MAX_NET_MOVE_PCT = 0.5`; `API_BUDGET_NORMAL_CEILING = 1.00`, `API_BUDGET_ELEVATED_CEILING = 1.50`, `API_BUDGET_WARNING_PCT = 0.80`; `CHOPPY_COMPOSITE_GATE = 30`, `HERD_ENTRY_GATE = 25`; `BOT_HERD_ALERT_SCORE = 60`, `BOT_HERD_HIGH_SCORE = 80`; `SQUEEZE_CONFIDENCE_GATE = 70`; `SWING_MAX_POSITIONS = 2`, `SWING_MAX_DAYS = 5`, `SHORT_TERM_MAX_POSITIONS = 2`, `SHORT_TERM_MAX_DAYS = 21`. **Acceptance:** `python -c "import config; print(config.MACRO_REGIME_FILE)"` works.

### Task 2: macro_regime.py — Yahoo signals + health score
Reuse `_yahoo_closes` pattern from `vix_regime.py`. Fetch 8 signals: `^VIX, SPY, CL=F, GC=F, DX-Y.NYB, ^TNX, SOXX, QQQ`. Score each ±10 with weights (VIX 20%, SPY 20%, SOXX 15%, QQQ 10%, WTI 10%, TNX 9%, Gold 8%, DXY 8%). Scale to ±100, add FRED modifier, clamp. **Acceptance:** unit tests for scoring (bullish/bearish/neutral cases) and bounds.

### Task 3: macro_regime.py — pattern detection + classifier
`_count_consecutive_direction(closes, "green"|"red")`, `_is_choppy(closes)` (≥3 sign-changes in last 5 + |net move| < 0.5%), `_compute_sector_scores` (AI_SEMI from SOXX, BROAD_TECH from QQQ, CYBERSECURITY = 0.8×QQQ). `classify_regime` priority: CRISIS > CRASH_RECOVERY > BULL_TREND > CHOPPY > OPPORTUNITY > DEFENSIVE > NORMAL. Hard rules: VIX > 35 OR SPY 5-day < -10% → CRISIS; DEFENSIVE requires both ≥3 red days AND bot P&L < -1%; OPPORTUNITY = DEFENSIVE conditions + AI_SEMI > +15. **Acceptance:** parametrised test covers all 9 states.

### Task 4: macro_regime.py — orchestration + flash crash + FRED
`fetch_macro_regime()` orchestrates fetch → score → classify → write JSON; falls back to NORMAL on fetch failure. `check_flash_crash(spy_current, spy_prev_close)` — pure function, returns True if intraday drop ≤ -2.5%. `refresh_fred_cache()` — fetches `DFF, CPIAUCSL, UNRATE` from `api.stlouisfed.org`; computes `fred_modifier` (cutting=+5, hiking=-5, rising unemployment=-3); skips silently if `FRED_API_KEY` empty. **Acceptance:** smoke test writes valid JSON; flash-crash math correct at boundary.

### Task 5: economic_calendar.py
`_hardcoded_events(year, month)` returns NFP (first Friday 8:30 AM), FOMC (8 known 2026 dates 14:00 PM), CPI (~12th 8:30 AM). `_scrape_forexfactory(date)` — best-effort scrape with `bs4`, 10s timeout, returns `[]` on any failure. `refresh_economic_calendar()` prefers scrape, falls back to hardcoded. `is_event_risk_window(now)` — returns True if `|now - event_time| ≤ 45 min`. **Edge cases:** missing file → `[]`; stale (date != today) → `[]`; corrupt JSON → `[]`. **Add:** `beautifulsoup4` to requirements.txt. **Acceptance:** test 45-min window boundary on both sides; NFP first-Friday-of-month logic.

### Task 6: api_budget.py
Sonnet 4.6 pricing: input $3/MTok, cache_write $3.75, cache_read $0.30, output $15. `load_budget()` — reset to zeros if `date != today_ET` (handles overnight gaps and DST). `get_ceiling()` — returns $1.50 when `portfolio_gain_pct ≥ 0.8` OR `regime ∈ {CRISIS, CAPITULATION, CRASH_RECOVERY}`, else $1.00. `check_budget()` — blocks if `current + estimated > ceiling`; warns at 80%. `record_usage()` — accumulates and saves. **Edge cases:** corrupt JSON → fresh budget; clock skew → use ET date authoritatively. **Acceptance:** budget resets across days; ceiling responds to inputs; block fires at boundary.

### Task 7: main.py schedule wiring
Add three `scheduler.add_job()` entries (cron, ET, mon–fri): 8:30 AM `economic_calendar.refresh_economic_calendar`; 8:55 AM `macro_regime.fetch_macro_regime` (replaces existing vix job); 4:00 PM `macro_regime.refresh_fred_cache`. Keep `vix_regime.py` on disk — `learning_agent.py` still imports `load_trading_params` from it. **Acceptance:** `timeout 5 python main.py` shows all jobs registered, no import errors.

### Task 8: integration smoke
Run morning sequence end-to-end against live Yahoo: `economic_calendar.refresh_economic_calendar()`, `macro_regime.fetch_macro_regime()`. Verify `macro_regime.json` has all schema keys and a valid `regime`. Run existing `test_agents.py -m unit` — must still pass.

---

## Worst-Case / Corner Cases

| Risk | Mitigation |
|---|---|
| Yahoo Finance returns None / empty | `_yahoo_closes` filters None; outer try/except defaults to NORMAL |
| ForexFactory layout changes / 403 | Hardcoded fallback (NFP/FOMC/CPI) always available |
| FRED key missing | Skip silently, modifier=0 (rest of system unaffected) |
| Clock skew across days | Use ET date in `_today()`; budget reset is idempotent |
| Existing vix_regime.json consumers | macro_regime.json contains `regime` key; old code keeps reading it |
| Scheduler double-registers job on restart | `replace_existing=True` on every job |
| Bot net P&L import requires Alpaca | `_load_bot_net_pnl` swallows all exceptions → 0.0 |
| FOMC date table goes stale (post-2026) | Document in code; learning agent flags missed events |

---

## Out of Scope (handled by Plan 2/3)

- swing_positions.json (file path is in config; module is Plan 2)
- `run_market_monitor` (lives in trading_agent.py — Plan 3)
- Wiring `is_event_risk_window` into trading decisions (Plan 3)
- Wiring `check_budget` into Claude callers (Plan 3)
- Counter-bot signals / parabolic / OBV divergence (Plan 2)
