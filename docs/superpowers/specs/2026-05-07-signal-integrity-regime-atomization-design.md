# Spec: Signal Integrity, Regime Intelligence & Atomic Refactor

**Date:** 2026-05-07  
**Goal:** Fix the seven production bugs that caused today's 0% win rate (3 losses, -$549), eliminate the AUTO-BUY gate-bypass architectural flaw, decompose monolith files into atomic modules, harden regime decision-making with intraday context and breadth signals, and update CLAUDE.md with enforceable coding rules.

**Root causes addressed:**
1. AUTO-BUY bypassed all entry gates (RS, MTF, VWAP, parabolic)
2. SYM entered at VWAP+5.2% (above 2σ) — no overextension gate
3. 3 simultaneous BUYs in one cycle — no per-cycle entry cap
4. `volume_ratio = 0.0` — incomplete bar used for volume computation
5. Parabolic flag advisory-only — not a hard veto
6. Economic calendar race condition — stale data on event-risk days
7. Volatility screener parsing comment lines from `tickers.txt`
8. Regime classifier blind to intraday market bias
9. 10 race conditions identified across `journal.py`, position monitor, force-close

---

## 1. Folder Structure

```
trading-bot/
├── macro/
│   ├── __init__.py
│   ├── health_scorer.py        # compute_macro_health_score() + all _score_* functions
│   ├── regime_classifier.py    # classify_regime(), _is_choppy(), _count_consecutive_direction()
│   ├── breadth_signals.py      # HYG, IWM, XLF, VIX3M scorers — added to 8:55 AM fetch
│   ├── intraday_context.py     # per-cycle SPY/QQQ intraday trend (cached 5 min)
│   └── fred_cache.py           # refresh_fred_cache(), _load_fred_modifier()
├── trading/
│   ├── __init__.py
│   ├── journal.py              # all state I/O: positions, journal, daily_closed — with locks
│   ├── position_sizer.py       # compute_position_size_for_mode(), progressive loss, sentiment mult
│   ├── claude_caller.py        # _ask_claude_batch(), compact prompt builder, cache strategy
│   ├── pipeline.py             # TradingPipeline: 4-phase orchestrator with timing logs
│   └── closer.py               # _close_position(), _apply_profit_lock(), force_close_all()
├── signals/
│   ├── __init__.py
│   ├── entry_gates.py          # NEW: unified GateResult runner for ALL order paths
│   ├── vwap_guard.py           # NEW: price > VWAP+2σ hard block
│   ├── time_gate.py            # existing
│   ├── relative_strength.py    # existing
│   ├── correlation_guard.py    # existing
│   ├── sentiment_sizer.py      # existing
│   └── mtf_confirm.py          # existing
├── macro_regime.py             # thin orchestrator: imports from macro/, writes JSON
├── trading_agent.py            # thin orchestrator: imports from trading/, owns run_position_monitor()
├── tests/
│   ├── test_foundation.py      # keeps: macro, regime, swing, research, earnings (trimmed)
│   ├── test_signals.py         # NEW: all signals/ unit tests
│   ├── test_trading.py         # NEW: pipeline, gates, sizer, closer, journal
│   └── test_indicators.py      # NEW: indicator engine, volume_ratio, plan-2 signals
└── docs/superpowers/specs/     # this file
```

**Line-count targets** (new CLAUDE.md rule: no module > 300 lines, no test file > 1000 lines):

| File | Current | Target |
|---|---|---|
| `trading_agent.py` | 1,360 | ≤ 300 (thin orchestrator) |
| `macro_regime.py` | 630 | ≤ 200 (thin orchestrator) |
| `tests/test_foundation.py` | 4,846 | ≤ 1,000 (trimmed) |
| Each new module | — | ≤ 300 |
| Each new test file | — | ≤ 1,000 |

---

## 2. Bug Fixes

### Bug 1 — AUTO-BUY bypasses all entry gates → unified `signals/entry_gates.py`

**Problem:** At 10:50 ET, SYM and ACHR were AUTO-BUYed (composite ≥ 35) without running the RS filter, MTF filter, VWAP overextension check, or parabolic block. Those gates existed only in the Claude code path.

**Fix:** Remove AUTO-BUY entirely. All candidates pass through `check_all_gates()` first. Claude decides for every symbol that passes gates.

```python
# signals/entry_gates.py
@dataclass(frozen=True)
class GateResult:
    passed: bool
    reason: str   # empty string when passed

def check_all_gates(
    symbol: str,
    iv: IndicatorValues,
    open_positions: dict,        # snapshot, not disk read — no lock needed
    bars_15min: pd.DataFrame | None,
    now: datetime,
    entries_this_cycle: int,
) -> GateResult:
    """
    Runs all entry gates in priority order. Returns on first failure.
    Called for every candidate — no order is placed without passing this.
    """
```

Gate execution order (cheapest first):

| Priority | Gate | Type | Config flag |
|---|---|---|---|
| 1 | Time gate | Hard | `ENTRY_GATE_OPEN_MINUTES`, `ENTRY_GATE_CLOSE_MINUTES` |
| 2 | Per-cycle entry cap | Hard | `MAX_ENTRIES_PER_CYCLE` |
| 3 | Daily closed | Hard | — |
| 4 | Stop-hunt zone | Hard | `STOP_HUNT_ROUND_PROXIMITY_PCT` |
| 5 | VWAP overextension | Hard | `VWAP_OVEREXTENSION_GATE` |
| 6 | Parabolic flag | Hard | `PARABOLIC_ENTRY_BLOCK` |
| 7 | Relative strength | Soft→flag | `RS_MIN_RATIO` |
| 8 | MTF confirmation | Soft→flag | `MTF_TIMEFRAME_MINUTES` |
| 9 | Correlation guard | Soft→flag | `MAX_SAME_SECTOR_POSITIONS` |

Hard gates: block the symbol, log the reason, do not call Claude.  
Soft gates: pass through but add a `flags=[]` annotation to the Claude compact summary so Claude sees the warning and decides.

New config constants:
```python
MAX_ENTRIES_PER_CYCLE   = 1       # one BUY per 10-min cycle maximum
VWAP_OVEREXTENSION_GATE = True    # block price > VWAP+2σ
PARABOLIC_ENTRY_BLOCK   = True    # block parabolic=True symbols
```

### Bug 2 — SYM entered above VWAP+2σ → `signals/vwap_guard.py`

```python
def passes_vwap_guard(iv: IndicatorValues) -> bool:
    """False if price exceeds the upper 2-sigma VWAP band."""
    if not config.VWAP_OVEREXTENSION_GATE:
        return True
    return iv.current_price <= iv.vwap_std2_upper
```

> **Implementation note:** Verify that `IndicatorValues` exposes `vwap_std2_upper`. The indicator engine already computes VWAP bands — confirm the field name in `indicator_engine.py` and add it to the dataclass if missing.

At entry: SYM price=$60.75, VWAP std2 upper=$60.646 → `passes_vwap_guard` returns False → hard block. This gate alone would have prevented the -$184 loss.

### Bug 3 — 3 simultaneous entries → `trading/pipeline.py` + `config.py`

`TradingPipeline._execute()` initialises `entries_this_cycle = 0` at cycle start.  
After each successful BUY, increments. Gate #2 in `check_all_gates()` returns `GateResult(False, "per-cycle entry cap reached")` when `entries_this_cycle >= MAX_ENTRIES_PER_CYCLE`.  
Cap applies to BUY only — CLOSE decisions are never blocked by it.

### Bug 4 — `volume_ratio = 0.0` → `indicator_engine.py`

**Root cause:** `_safe(series)` uses `series.iloc[-1]`. The last 5-min bar at trade time is the in-progress bar (just opened, volume=0). So `volume_ratio = 0/mean = 0.0`.

```python
def _safe_penultimate(series: pd.Series, fallback: float = 0.0) -> float:
    """Use second-to-last bar — the last bar is always the incomplete current bar."""
    if len(series) < 2:
        return fallback
    val = series.iloc[-2]
    return fallback if pd.isna(val) else float(val)
```

Only `volume_ratio` line changes:
```python
"volume_ratio": _safe_penultimate(vol_ratio, 1.0),   # was: _safe(vol_ratio, 1.0)
```

All other indicators continue using `_safe` (price-derived indicators: last bar is correct).

### Bug 5 — Parabolic flag not blocking → `signals/entry_gates.py`

Gate #6 in `check_all_gates()`:
```python
if config.PARABOLIC_ENTRY_BLOCK and iv.parabolic:
    return GateResult(False, "parabolic move — hard veto (mean-reversion risk)")
```

`PARABOLIC_ENTRY_BLOCK = True` in config. When False, reverts to advisory-only (current behaviour) for A/B comparison via learning agent.

### Bug 6 — Economic calendar race condition → `main.py`

**Root cause:** `economic_calendar` job scheduled at 8:30 ET. Bot starts at 8:30 ET. APScheduler does not fire jobs for the exact startup moment.

**Fixes:**
1. Move job: `hour=8, minute=30` → `hour=8, minute=20` (10 min earlier, never races startup)
2. Lower catchup threshold: `before 8:45 AM` → `before 8:25 AM`
3. Add `economic_calendar` to `_catchup_missed_agents` jobs checked against calendar date staleness
4. Add `prior_events` key: when refreshing, carry forward today's events to `prior_events` for post-event-day detection

New config:
```python
ECONOMIC_CALENDAR_HOUR   = 8
ECONOMIC_CALENDAR_MINUTE = 20
POST_EVENT_POSITION_MULT = 0.6   # 60% sizing on day-after FOMC/NFP/CPI
```

### Bug 7 — Volatility screener comment lines → `volatility_screener.py`

One filter before the screening loop:
```python
tickers = [
    t.strip() for t in raw_tickers
    if t.strip() and not t.strip().startswith("#") and t.strip().isalpha()
]
```

`isalpha()` drops comment lines AND `BRK.B` (dot-ticker, unsupported by Alpaca paper trading). Log once at startup:
```python
dropped = [t for t in raw_tickers if not t.strip().isalpha()]
if dropped:
    logger.info(f"Volatility screener: skipping {len(dropped)} non-alpha tickers: {dropped[:5]}…")
```

---

## 3. Token-Efficient Claude Caller (`trading/claude_caller.py`)

### Remove AUTO-BUY entirely

The auto-buy shortcut existed only to save tokens. It created the exact bug that caused today's losses. Removing it eliminates the gate-bypass. Token savings come from a leaner prompt, not from skipping Claude.

### Compact per-stock summary (~60 tokens vs current ~180 tokens)

```python
def _build_compact_stock_line(
    symbol: str,
    iv: IndicatorValues,
    research: dict,
    gate_flags: list[str],          # soft gate warnings to surface
    existing_pos: dict | None,
) -> str:
    """Single-line compact format. ~60 tokens per stock."""
    rsi_dir = "↑" if iv.rsi_slope > 0 else "↓"
    vwap_pct = (iv.current_price - iv.vwap) / iv.vwap * 100
    sent = research.get(symbol, {}).get("sentiment_score", 0)
    pos = f" POS:entry={existing_pos['entry_price']:.2f}" if existing_pos else ""
    flags = f" flags=[{','.join(gate_flags)}]" if gate_flags else ""
    return (
        f"{symbol:<6} cs={iv.composite_score:+.1f} rsi={iv.rsi:.1f}{rsi_dir}"
        f" vwap={vwap_pct:+.1f}% macd={iv.macd_hist:+.4f}"
        f" stoch={iv.stoch_k:.0f}/{iv.stoch_d:.0f}"
        f" vol={iv.volume_ratio:.2f}x sent={sent:+d}{pos}{flags}"
    )
```

Example output:
```
DRAM   cs=+26.9 rsi=49.7↓ vwap=+0.6% macd=+0.025 stoch=69/74 vol=1.75x sent=+80 flags=[rs_weak]
ACHR   cs=+45.4 rsi=57.4↑ vwap=+0.6% macd=+0.003 stoch=78/75 vol=1.40x sent=+55
```

Hard-blocked symbols never appear in the batch. Soft-flagged symbols appear with `flags=[]` context.

### Full system prompt caching

The entire system block (market context + indicator weights + strategy memory) is stable within a trading cycle. Cache the full block:

```python
system_blocks = [
    {
        "type": "text",
        "text": f"{market_ctx}\n\nIndicator weights: {json.dumps(weights)}\n\n{strategy_memory}",
        "cache_control": {"type": "ephemeral"},   # always cache — stable per cycle
    }
]
```

Cache hit rate improves from ~30% (strategy memory only) to ~85% (full system stable per cycle).

### Per-cycle token cost comparison

| | Current | New |
|---|---|---|
| System (uncached) | ~1,500 tokens | ~0 (cached after first call) |
| System (cache read) | ~800 tokens | ~1,500 tokens |
| Per stock (5 stocks) | 5 × 180 = 900 tokens | 5 × 60 = 300 tokens |
| Total first call | ~2,900 | ~1,800 |
| Total subsequent calls | ~2,300 | ~300 (cache hit) |

### Claude output schema unchanged

`{ action, confidence, stop_loss_pct, take_profit_pct, reasoning, indicator_signals }` — no change to response parsing.

---

## 4. Phased Pipeline (`trading/pipeline.py`)

```python
class TradingPipeline:
    def run(self, symbols: list[str], ...) -> CycleResult:
        t0 = time.monotonic()

        # Phase 1: Parallel data fetch (ThreadPoolExecutor, max_workers=FETCH_WORKERS)
        fetch_results = self._fetch_all(symbols)         # ~3s

        # Phase 2: Gate checks — pure functions, open_positions snapshot passed in
        gate_results  = self._run_gates(fetch_results)   # ~0.1s
        passed        = [(s, r) for s, r in gate_results.items() if r.passed]

        # Phase 3: Claude batch — single API call for all passed symbols
        decisions = {}
        if passed:
            decisions = self._call_claude(passed, fetch_results)  # ~5-10s

        # Phase 4: Execute — BUY/CLOSE per decision, per-cycle cap enforced
        result = self._execute(decisions, fetch_results)  # ~1s

        p1, p2, p3, p4 = self._phase_times
        logger.info(
            f"[CYCLE] {len(symbols)} symbols | fetch={p1:.1f}s "
            f"gates={p2:.1f}s ({len(passed)} passed) "
            f"claude={p3:.1f}s execute={p4:.1f}s"
        )
        return result
```

New config:
```python
FETCH_WORKERS              = 6      # parallel threads for Phase 1
INTRADAY_CONTEXT_TTL_SEC   = 300    # cache intraday regime context 5 min
RESEARCH_STALE_WARN_MIN    = 120    # warn if research > 2h old
RESEARCH_STALE_BLOCK_MIN   = 480    # skip cycle if research > 8h old
```

`RESEARCH_STALE_WARN_MIN` and `RESEARCH_STALE_BLOCK_MIN` are checked at the top of `TradingPipeline.run()` before Phase 1 begins. `run()` reads the `"timestamp"` field from `research_report.json`, computes age in minutes, logs a warning at WARN_MIN, and returns `CycleResult(skipped=True, reason="stale_research")` at BLOCK_MIN.

---

## 5. Scheduler Hardening (`main.py`)

### `max_instances=1` and `misfire_grace_time` for all jobs

```python
PRE_MARKET_KW = dict(day_of_week="mon-fri", timezone=ET, max_instances=1, misfire_grace_time=300)
INTRADAY_KW   = dict(day_of_week="mon-fri", timezone=ET, max_instances=1, misfire_grace_time=30)
```

Currently only 4 of 15 jobs have `max_instances=1`. Applying to all jobs prevents concurrent instances when a run overruns its interval (root cause of the repeated volatility screener runs seen in today's log).

`misfire_grace_time=300` for pre-market: job fires up to 5 minutes late (slow network) and still runs. `misfire_grace_time=30` for intraday: a trading cycle stale by >30s uses bad data and is correctly dropped.

### Job schedule changes

| Job | Old time | New time | Reason |
|---|---|---|---|
| `economic_calendar` | 8:30 ET | 8:20 ET | Avoid race with bot startup at 8:30 |

### Force-close coordination

`force_close_all()` pauses the `position_monitor` job before executing to prevent double-close and double-journal:

```python
def force_close_all(scheduler=None):
    if scheduler:
        scheduler.pause_job("position_monitor")
    try:
        with _POSITIONS_LOCK:
            _force_close_body(alpaca, swing_symbols)
    finally:
        if scheduler:
            scheduler.resume_job("position_monitor")
```

---

## 6. Race Condition Fixes (`trading/journal.py`)

All state I/O consolidated in `trading/journal.py` with three module-level locks:

```python
_POSITIONS_LOCK     = threading.Lock()   # open_positions.json read/write
_DAILY_CLOSED_LOCK  = threading.Lock()   # daily_closed.json read-modify-write
_JOURNAL_LOCK       = threading.Lock()   # trade_journal.json append
```

### RC-1: `record_daily_closed` — unguarded read-modify-write

```python
def record_daily_closed(symbol: str) -> None:
    with _DAILY_CLOSED_LOCK:
        closed = load_json(config.DAILY_CLOSED_FILE, [])
        closed_set = set(closed)
        closed_set.add(symbol)
        save_json(config.DAILY_CLOSED_FILE, list(closed_set))
```

### RC-2: `append_trade` — unguarded read-modify-write

```python
def append_trade(entry: dict) -> None:
    with _JOURNAL_LOCK:
        journal = load_json(config.TRADE_JOURNAL_FILE, [])
        journal.append(entry)
        save_json(config.TRADE_JOURNAL_FILE, journal)
```

### RC-3: Position monitor final save overwrites in-flight BUYs

Replace `save_open_positions(open_positions)` at the end of `run_position_monitor()` with a targeted merge-write:

```python
def flush_profit_lock_updates(updates: dict[str, dict]) -> None:
    """Push only profit-lock field changes. Never wipe keys added by concurrent BUYs."""
    with _POSITIONS_LOCK:
        disk = load_json(config.OPEN_POSITIONS_FILE, {})
        for sym, pos in updates.items():
            if sym in disk:      # skip symbols closed between snapshot and now
                disk[sym] = pos
        save_json(config.OPEN_POSITIONS_FILE, disk)
```

### RC-4: `force_close_all` vs `run_position_monitor`

Handled by pausing `position_monitor` job (see Section 5) and wrapping the main loop in `_POSITIONS_LOCK`.

### CC-1: Stale `daily_closed` in trading cycle

In `TradingPipeline._execute()`, re-read `daily_closed` from disk inside `_POSITIONS_LOCK` immediately before placing each BUY — not from the cycle-start snapshot:

```python
with _POSITIONS_LOCK:
    disk_positions = load_json(config.OPEN_POSITIONS_FILE, {})
    disk_closed    = set(load_json(config.DAILY_CLOSED_FILE, []))
    if symbol in disk_positions or symbol in disk_closed:
        continue   # closed or entered by a concurrent path
    disk_positions[symbol] = position_data
    save_json(config.OPEN_POSITIONS_FILE, disk_positions)
```

### CC-2: `force_close_all` calls `get_positions()` inside the loop

```python
# Before loop:
live_symbols = set(alpaca.get_positions().keys()) if config.EOD_VERIFY_FILLS else None
# Inside loop — no more per-symbol API call
for symbol, pos in list(open_positions.items()):
    if live_symbols is not None and symbol not in live_symbols:
        ...
```

### CC-3: `MAX_ENTRIES_PER_CYCLE` cap is BUY-only

`entries_this_cycle` increments only on BUY execution. CLOSE decisions bypass the counter entirely.

### CC-4: `check_all_gates()` receives snapshot — no disk read, no lock

`TradingPipeline` loads `open_positions` once before Phase 2 and passes it as an argument. `check_all_gates()` never calls `load_open_positions()`. Correlation guard works on the snapshot — conservatively correct.

### CC-5: MTF fetch failure is a soft flag, not a hard block

```python
# In check_all_gates():
try:
    if bars_15min is not None and not passes_mtf_filter(bars_15min):
        gate_flags.append("mtf_not_confirmed")   # soft: Claude decides
except Exception:
    gate_flags.append("mtf_unavailable")         # soft: Claude decides
```

### CC-6: `_claude_disabled` replaced with `threading.Event`

```python
# trading/claude_caller.py
_CLAUDE_DISABLED        = threading.Event()    # set() = disabled
_CLAUDE_DISABLED_REASON = ""
_CLAUDE_LOCK            = threading.Lock()
```

---

## 7. Regime Intelligence (`macro/` subpackage)

### 7a. `macro/breadth_signals.py` — four new signals added to 8:55 AM fetch

New tickers in `fetch_macro_regime()`:

```python
"hyg":  "HYG",       # High yield bonds — credit spread proxy
"iwm":  "IWM",       # Small caps — risk appetite vs large caps
"xlf":  "XLF",       # Financials — market leadership indicator
"vix3m": "%5EVIX3M", # 3-month VIX — term structure
```

New scorer functions (all pure, in `macro/breadth_signals.py`):

```python
def _score_hyg(ret5d: float) -> float:
    if ret5d < -1.5: return -10.0
    if ret5d < -0.5: return  -5.0
    if ret5d >  0.5: return  +8.0
    return +3.0

def _score_iwm_vs_spy(iwm_ret: float, spy_ret: float) -> float:
    spread = iwm_ret - spy_ret
    if spread < -1.5: return -10.0
    if spread < -0.5: return  -5.0
    if spread >  1.0: return  +8.0
    return 0.0

def _score_vix_term_structure(vix_current: float, vix3m: float) -> float:
    if vix3m <= 0: return 0.0
    ratio = vix_current / vix3m
    if ratio > 1.1:  return -10.0   # backwardation — immediate fear elevated
    if ratio > 1.02: return  -5.0
    if ratio < 0.92: return  +8.0   # contango — calm
    return 0.0
```

Updated `compute_macro_health_score` weights (sum = 1.0):

| Signal | Old | New | Change |
|---|---|---|---|
| VIX | 0.20 | 0.15 | -0.05 (term structure added) |
| SPY | 0.20 | 0.17 | -0.03 |
| HYG | — | 0.12 | new: credit stress leading indicator |
| SOXX | 0.15 | 0.12 | -0.03 |
| IWM vs SPY | — | 0.08 | new: risk appetite breadth |
| QQQ | 0.10 | 0.08 | -0.02 |
| VIX term structure | — | 0.06 | new: immediate vs forward fear |
| WTI | 0.10 | 0.07 | -0.03 |
| TNX | 0.09 | 0.07 | -0.02 |
| Gold | 0.08 | 0.05 | -0.03 |
| DXY | 0.08 | 0.03 | -0.05 |

### 7b. `macro/intraday_context.py` — per-cycle lightweight fetch

```python
@dataclass(frozen=True)
class IntradayContext:
    spy_today_pct:    float    # SPY % from yesterday's close
    spy_vwap_position: str    # "above" | "below"
    spy_vwap_slope:   float   # SPY's own VWAP slope
    qqq_today_pct:    float   # QQQ % from yesterday's close
    opening_gap_pct:  float   # SPY open vs prior close
    post_event_day:   bool    # yesterday was FOMC/NFP/CPI
    sector_today:     dict    # {"XLF": +0.3, "XLK": -0.8, "XLE": +1.2}
    bias:             str     # "BULLISH" | "NEUTRAL" | "BEARISH"

def get_intraday_context(now: datetime) -> IntradayContext:
    """Fetch SPY + QQQ + XLF/XLK/XLE 5-min bars. Cached INTRADAY_CONTEXT_TTL_SEC."""
```

Bias logic:
```python
if spy_today_pct < -0.3 and spy_vwap_position == "below":
    bias = "BEARISH"
elif spy_today_pct > 0.3 and spy_vwap_position == "above":
    bias = "BULLISH"
else:
    bias = "NEUTRAL"
```

Uses `get_intraday_bars("SPY")` — already available via `market_data.py`. Cached for 5 minutes so a 10-min trading cycle pays this cost at most once every other cycle.

### 7c. Post-event-day detection in `economic_calendar.py`

When `refresh_economic_calendar()` writes today's data, it carries forward the previous day's events:

```python
prev_cache = load_json(config.ECONOMIC_CALENDAR_FILE, {})
cache["prior_events"] = prev_cache.get("events", [])
cache["events"] = today_events
```

New function:

```python
def is_post_major_event_day(now: datetime) -> tuple[bool, str]:
    """Returns (True, event_name) if yesterday had a high-impact scheduled event."""
    prior = load_json(config.ECONOMIC_CALENDAR_FILE, {}).get("prior_events", [])
    high_impact = ["FOMC", "NFP", "CPI", "PCE", "JOLTS"]
    for evt in prior:
        if any(tag in evt.get("name", "").upper() for tag in high_impact):
            return True, evt["name"]
    return False, ""
```

Applied in `trading/position_sizer.py`:

```python
post_event, _ = is_post_major_event_day(now)
if post_event:
    size_multiplier *= config.POST_EVENT_POSITION_MULT   # 0.6× on post-FOMC/NFP/CPI days
```

### 7d. Enhanced `_build_market_context_block` — full context to Claude

Before (~100 tokens, 4 fields sent of 18 computed):
```
Regime: NORMAL | health=+52 | VIX=17.4
FRED: Fed paused | CPI rising | Unemployment stable
Event risk: None active
Macro shock: 20/100
```

After (~200 tokens, system-cached so marginal cost ≈ 0):
```
MARKET CONTEXT (10:50 ET):
Daily regime: NORMAL | health=+52 | VIX=17.4 (90d avg=20.5, declining, complacency)
Weekly: SPY+3.1% (2g/0r consecutive) | sign_changes=2 (choppy week despite net positive)
Breadth: HYG=-0.8% | IWM vs SPY=-1.2% (small cap lagging) | VIX term=backwardation
Sector scores: AI_SEMI=100 BROAD_TECH=100 (both stretched — mean-reversion risk)
Intraday bias: BEARISH | SPY=-0.8% below VWAP (slope=-0.15%) | QQQ=-1.2% | gap_open=-0.5%
Sectors today: XLF=+0.3% XLK=-0.9% XLE=+1.1%
FRED: Fed paused | CPI rising | Unemployment stable
Event risk: None today ⚠ POST-FOMC DAY — elevated volatility, gap-fill tendency
Macro shock: 20/100 | Capitulation watch: ARM
```

With this context today, Claude would have seen: SPY +3% week (stretched), VIX below 90d avg (complacency), intraday BEARISH bias, POST-FOMC day flag, both sector scores maxed. Three simultaneous longs would not have been approved.

---

## 8. Analyst Revisions in PEAD Detection (`earnings_calendar.py`)

### 8a. Fetch forward estimate revision (no new API)

Add `earningsTrend` to the existing `_yahoo_quote_summary` call in `_fetch_earnings_history()`:

```python
def _fetch_forward_eps_revision(symbol: str) -> float | None:
    """
    30-day revision of next-quarter EPS consensus.
    Returns % change (positive = analysts raising estimates).
    """
    try:
        data = _yahoo_quote_summary(symbol, "earningsTrend")
        trend = data.get("earningsTrend", {}).get("trend", [])
        current_q = next((t for t in trend if t.get("period") == "0q"), None)
        if not current_q:
            return None
        eps_trend = current_q.get("epsTrend", {})
        current   = eps_trend.get("current",   {}).get("raw")
        ago_30d   = eps_trend.get("30daysAgo", {}).get("raw")
        if current is None or ago_30d is None or ago_30d == 0:
            return None
        return (current - ago_30d) / abs(ago_30d) * 100
    except Exception:
        return None
```

Added as field `"forward_eps_revision_30d_pct"` in `recently_reported` block.

### 8b. PEAD 6th condition

```python
# Condition 6: Forward EPS estimates revised upward post-earnings
fwd_rev = block.get("forward_eps_revision_30d_pct")
if fwd_rev is not None and fwd_rev >= config.PEAD_FWD_REVISION_MIN:   # 3.0%
    score += 20
```

New config: `PEAD_FWD_REVISION_MIN = 3.0`.

**Score scale:** With graded EPS (max 30) + 5 binary conditions at 20 each + 6th condition at 20, the maximum possible score is 30 + 5×20 = 130. The raw **qualification threshold stays at 80 points** (not 80%). This makes the bar slightly easier to reach than the old 80/100 — intentional, since the graded EPS condition is harder to score full marks on (≥20% beat for 30 pts vs any beat for 20 pts). A stock that only hits a marginal EPS beat (10 pts) still needs 3 other full-score conditions.

### 8c. Graded EPS beat magnitude (replaces binary condition 1)

```python
if eps_surp is not None:
    if   eps_surp >= config.PEAD_EPS_BEAT_STRONG:    score += 30   # blowout ≥ 20%
    elif eps_surp >= config.PEAD_EPS_BEAT_SOLID:      score += 25   # strong ≥ 10%
    elif eps_surp >= config.PEAD_EPS_SURPRISE_MIN:    score += 20   # solid ≥ 3%
    elif eps_surp >= 1.0:                              score += 10   # marginal beat
```

New config: `PEAD_EPS_BEAT_STRONG = 20.0`, `PEAD_EPS_BEAT_SOLID = 10.0`.

### 8d. Revision flag in research agent compact summary

`research_report.json` already stores per-symbol data. Add `forward_eps_revision_30d_pct` as an optional field. In the Claude compact line:

```
DRAM  cs=+26.9 ... sent=+80 eps_rev=+6.4%   ← analysts raised estimates 6.4% in 30 days
```

---

## 9. Test File Decomposition

### `tests/test_signals.py` (new, ~600 lines)

Extract from `test_foundation.py` and add new tests:

| Class | Tests |
|---|---|
| `TestTimeGate` | 8 — open, first 30min, last 35min, boundaries |
| `TestRelativeStrength` | 6 — outperform, underperform, zero SPY return |
| `TestCorrelationGuard` | 8 — 0/1/2/3 same-sector, unknown sector |
| `TestSentimentSizer` | 10 — all 5 tier boundaries |
| `TestMtfConfirm` | 6 — EMA9>21, EMA9<21, insufficient data |
| `TestVwapGuard` | 6 — at band, above, below, flag disabled |
| `TestEntryGates` | 16 — each gate pass/fail + ordering |
| `TestCheckAllGates` | 12 — integration: multiple gates in sequence |

### `tests/test_trading.py` (new, ~800 lines)

| Class | Tests |
|---|---|
| `TestTradingPipeline` | 12 — all 4 phases, empty batch, Claude fail |
| `TestPositionSizer` | 10 — regime mult, progressive loss, sentiment, post-event |
| `TestJournalLocks` | 8 — concurrent append_trade, record_daily_closed |
| `TestFlushProfitLock` | 6 — merge-write correctness, concurrent BUY survives |
| `TestForceCloseAll` | 10 — phantom skip, retry, pause/resume monitor |
| `TestPipelinePerCycleCap` | 6 — cap=1 allows one BUY, blocks second, never blocks CLOSE |

### `tests/test_indicators.py` (new, ~500 lines)

Extract from `test_foundation.py`:

| Class | Tests |
|---|---|
| `TestVolumePenultimate` | 6 — confirms `_safe_penultimate` uses second-to-last bar |
| `TestIndicatorEnginePlan2` | moved from test_foundation.py |
| `TestVolumeExhaustion` | moved |
| `TestOBVDivergence` | moved |
| `TestStopHuntZone` | moved |

### `tests/test_foundation.py` (trimmed to ≤ 1,000 lines)

Keeps: `TestMacroHealthScore`, `TestClassifyRegime`, `TestFlashCrash`, `TestEconomicCalendar`, `TestResearchAgentPlan2`, `TestSwingManager`, `TestEarningsCalendar`, `TestIntradayContext` (new), `TestBreadthSignals` (new), `TestPostEventDay` (new).

---

## 10. CLAUDE.md Additions

### Module size limits
```
Module size: No source file may exceed 300 lines. No test file may exceed 1,000 lines.
If a file is approaching the limit, extract a focused submodule before adding more code.
```

### Test file naming convention
```
Test files are domain-scoped, not monolithic:
  tests/test_signals.py     — signals/ package unit tests
  tests/test_trading.py     — trading/ package unit tests
  tests/test_indicators.py  — indicator_engine.py unit tests
  tests/test_foundation.py  — macro, regime, swing, research, earnings
Never add a new test class to test_foundation.py if it belongs in a more specific file.
```

### No AUTO-BUY shortcuts
```
All BUY orders must pass check_all_gates() before execution. No order bypasses gate
checks for any reason — not for high composite scores, not for high sentiment.
The gate-bypass pattern is the root cause of multiple production losses.
```

### Locks for all shared state
```
Any read-modify-write on a JSON state file shared between scheduler jobs must hold
the corresponding lock (trading/journal.py: _POSITIONS_LOCK, _DAILY_CLOSED_LOCK,
_JOURNAL_LOCK). Never read-modify-write shared state without a lock.
```

### Intraday context is required for Claude
```
_build_market_context_block() must include IntradayContext (spy_today_pct, bias,
intraday sector performance) and post-event-day flag. Claude cannot make informed
decisions with only the daily regime snapshot.
```

### Scheduler job defaults
```
Every scheduler.add_job() call must specify max_instances=1 and misfire_grace_time.
Use PRE_MARKET_KW (grace=300s) for jobs scheduled before 9:15 ET.
Use INTRADAY_KW (grace=30s) for jobs scheduled during market hours.
```

---

## 11. New `config.py` Constants

```python
# ── Signal Integrity (2026-05-07) ─────────────────────────────────────────────
MAX_ENTRIES_PER_CYCLE       = 1       # one BUY per trading cycle
VWAP_OVEREXTENSION_GATE     = True    # block price > VWAP+2σ
PARABOLIC_ENTRY_BLOCK       = True    # block parabolic=True entries
POST_EVENT_POSITION_MULT    = 0.6     # 60% sizing on post-FOMC/NFP/CPI days
INTRADAY_CONTEXT_TTL_SEC    = 300     # cache SPY intraday context 5 min
RESEARCH_STALE_WARN_MIN     = 120     # warn if research > 2h old
RESEARCH_STALE_BLOCK_MIN    = 480     # skip cycle if research > 8h old
FETCH_WORKERS               = 6       # parallel fetch threads in pipeline Phase 1
ECONOMIC_CALENDAR_HOUR      = 8       # moved from 8:30 → 8:20 ET
ECONOMIC_CALENDAR_MINUTE    = 20

# ── Breadth Signals ───────────────────────────────────────────────────────────
MACRO_BREADTH_TICKERS = {"hyg": "HYG", "iwm": "IWM", "xlf": "XLF", "vix3m": "%5EVIX3M"}

# ── PEAD Enhancements ─────────────────────────────────────────────────────────
PEAD_EPS_BEAT_STRONG        = 20.0    # blowout beat threshold
PEAD_EPS_BEAT_SOLID         = 10.0    # strong beat threshold
PEAD_FWD_REVISION_MIN       = 3.0     # forward EPS revision % to qualify as condition 6
```

---

## 12. Approach C Compatibility

Every design decision prepares for Approach C (backtest-driven threshold tuning):

| Element | How C extends it |
|---|---|
| `signals/entry_gates.py` with `config.*` thresholds | C sweeps `VWAP_OVEREXTENSION_GATE`, `RS_MIN_RATIO`, `PARABOLIC_ENTRY_BLOCK` |
| `macro/health_scorer.py` pure function | C tunes breadth weights without touching orchestration |
| `macro/intraday_context.py` cached dataclass | C replaces with replay from historical 5-min bars |
| Graded PEAD EPS beat score | C tunes `PEAD_EPS_BEAT_STRONG` / `PEAD_EPS_BEAT_SOLID` thresholds |
| `trading/pipeline.py` phase timing logs | C uses timing data to identify bottlenecks |
| `close_reason` classification | C measures signal quality by exit type |
