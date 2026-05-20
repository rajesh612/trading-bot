# Design: Bug Fixes + Universe Refactor (2026-05-19)

## Scope

Two independent but co-deployed workstreams:

1. **Bug fixes** — 14 bugs identified by the audit (13 original + 1 bonus BRK.B screener bug)
2. **Universe refactor** — replace `tickers.txt` + `tier1_config.py` with a single `tickers.json`
   and a new `sectors.py` module; remove `_SECTOR_ETF_MAP` from `pipeline.py` and
   `_EXTENDED_SECTOR` from `correlation_guard.py`; reorganize `config.py`

---

## Part 1 — Bug Fixes

### B1 · Progressive loss sizing never activates (`journal.py`)

**Root cause:** `count_todays_losses()` checks `e.get("action") == "CLOSE"` but every journal
entry is written with `"type": "CLOSE"` (executor, closer, monitor).  
`count_todays_losses()` always returns 0 → `position_sizer.py` always uses `loss_mult = 1.0`.

**Fix:** Change `"action"` → `"type"` in `trading/journal.py:110`.

---

### B2 · Midday "no trades" alarm always fires (`scheduler_jobs.py`)

**Root cause:** `job_midday_check()` checks `e.get("action") == "BUY"` but journal stores
`"type": "BUY"` → `buys_today` is always empty → `notify_no_trades_midday` fires every noon.

**Fix:** Change `"action"` → `"type"` in `scheduler_jobs.py:209`.

---

### B3 · `_compute_regime_winrate` always returns `{}` (`learning/trade_analysis.py`)

**Root cause:** `_load_todays_closed_trades()` correctly filters with `type == "CLOSE"` but the
resulting dicts have no `"action"` key; `_compute_regime_winrate()` then checks
`trade.get("action") != "CLOSE"` → skips every trade → empty dict → Claude gets no regime
win-rate context.

**Fix:** Change `"action"` → `"type"` in `learning/trade_analysis.py:59`.

---

### B4 · Zero-trade `composite_min_normal` relaxation never applies (`entry_gates.py` + `learning/agent.py`)

**Root cause:** `_handle_zero_trade_day()` writes `composite_min_normal` to
`trading_params.json`, but `effective_composite_gate()` always reads
`config.COMPOSITE_MIN_NORMAL` directly — never from `snap.params`. The relaxation is written
but never consumed.

**Fix:**
- Add `composite_min_normal` to `load_trading_params()` defaults (defaults to
  `config.COMPOSITE_MIN_NORMAL`).
- `effective_composite_gate()` gains an optional `params: dict` argument. When provided,
  returns `params.get("composite_min_normal", config.COMPOSITE_MIN_NORMAL)` for NORMAL regime.
- All callers (`executor.py`, `pre_claude_filter.py`) pass `snap.params`.

---

### B5 · DEFENSIVE regime silently requires composite ≥ 52.5 (`pre_claude_filter.py`)

**Root cause:** Layer 1.5 DEFENSIVE check: `config.COMPOSITE_MIN_HIGH_VOL * 1.5` = 52.5.
Magic multiplier `1.5` is not in config. `check_all_gates` only requires 35 for DEFENSIVE,
so the pre-filter rejects stocks that the gate would have passed.

**Fix:**
- Add `DEFENSIVE_COMPOSITE_MULT = 1.5` to `config.py`.
- Use in `pre_claude_filter.py`: `config.COMPOSITE_MIN_HIGH_VOL * config.DEFENSIVE_COMPOSITE_MULT`.

---

### B6 · Magic number `12` for squeeze composite gate (`entry_gates.py`)

**Root cause:** `effective_composite_gate()` returns literal `12` when `iv.squeeze_setup`.
Violates Rule 1.

**Fix:** Add `SQUEEZE_COMPOSITE_MIN = 12` to `config.py`; replace literal in `entry_gates.py`.

---

### B7 · Stale comment in `entry_gates.py` + `CLAUDE.md` tables

**Root cause:** Inline comment `# 30` on line 44 of `entry_gates.py`; `CHOPPY_COMPOSITE_GATE`
is 35. Also `CLAUDE.md` config table lists `COMPOSITE_MIN_NORMAL = 20` (actual: 30).

**Fix:** Correct comment in `entry_gates.py`; update `CLAUDE.md` config table.

---

### B8 · Missing params defaults + `choppy_rsi_max` never learned (`macro/regime.py`, `learning/agent.py`)

**Root cause:** `load_trading_params()` defaults dict omits `atr_tp_multiplier` and
`choppy_rsi_max`. The latter is also absent from the learning agent's `param_keys` list, so
it can never be updated from trade data.

**Fix:**
- Add `"atr_tp_multiplier": config.ATR_TP_MULT_DEFAULT` and
  `"choppy_rsi_max": config.CHOPPY_RSI_PULLBACK_MAX` to `load_trading_params()` defaults.
- Add `"choppy_rsi_max"` to `param_keys` in `learning/agent.py`; add it to `_THRESHOLD_BOUNDS`
  in `learning/trade_analysis.py` with bounds `(45, 70)`.

---

### B9 · `metrics.jsonl` grows unbounded (`utils/metrics.py`)

**Root cause:** `record_cycle_metric()` appends indefinitely. `TELEMETRY_KEEP_LINES` comment
says "metrics.jsonl" but only applies to `cycle_telemetry.jsonl`.

**Fix:**
- Add `METRICS_KEEP_LINES = 2000` to `config.py` (≈55 trading days).
- In `record_cycle_metric()`, trim file to last `METRICS_KEEP_LINES` lines once per day (same
  pattern as `_rotate_telemetry()` in `cycle.py`).
- Fix `TELEMETRY_KEEP_LINES` comment in `config.py`.

---

### B10 · Drawdown alerts spam Telegram every 60 s (`trading/monitor.py`)

**Root cause:** `_check_drawdown_alerts()` has no per-position cooldown. A position in drawdown
generates one Telegram message per monitor tick (every 60 s) for its entire lifetime.

**Fix:**
- Add `DRAWDOWN_ALERT_COOLDOWN_MIN = 30` to `config.py`.
- Add module-level `_drawdown_alerted: dict[str, float]` (symbol → epoch of last alert).
- Only fire when symbol not in dict or `time.time() - last > COOLDOWN * 60`.
- Clear symbol on position close (called from `close_position` or reset in `flush_profit_lock_updates`).

---

### B11 · `AlpacaClient()` created per-iteration in EOD hold evaluation (`trading/eod_holds.py`)

**Root cause:** `AlpacaClient()` is instantiated inside the `for symbol, pos in open_positions`
loop. Every position creates a new client object.

**Fix:** Move `alpaca = AlpacaClient()` outside the loop (before the `for` statement).

---

### B12 · `CycleResult.skipped` never set True — dead guard (`trading/pipeline.py`, `trading/cycle.py`)

**Root cause:** `cycle_result.skipped` defaults False and is never set True. The guard
`not cycle_result.skipped` in `cycle.py` is always True and provides no protection.

**Fix:** Set `result.skipped = True` in `pipeline.run()` when `not stock_data` (no bars
returned for any symbol). This properly signals a data-failure cycle vs a genuine zero-candidate cycle.

---

### B13 (Bonus) · `BRK.B` silently dropped by screener (`agents/volatility_screener.py`)

**Root cause:** `t.strip().isalpha()` returns `False` for `BRK.B` (contains `.`) → Berkshire
Hathaway is permanently excluded from the trading universe at the screener stage.

**Fix:** Replace `isalpha()` filter with an explicit comment-strip and alphanumeric-dot
allowlist: `re.match(r'^[A-Z][A-Z0-9.]{0,5}$', t.strip().upper())`.  
This issue is eliminated entirely once the universe loads from `tickers.json`.

---

## Part 2 — Universe Refactor

### 2a · New file: `tickers.json`

Single authoritative source for the entire trading universe. Replaces both `tickers.txt` and
the data section of `tier1_config.py`.

**Structure:** ETF-keyed top-level dict. Humans edit by sector; bot reads O(1) via pre-built
reverse maps.

```json
{
  "XLK": {
    "name": "Technology",
    "symbols": [
      {"symbol": "NVDA", "tier1": true,  "theme": "AI_GPU"},
      {"symbol": "BRK.B", ...}
    ]
  },
  "XLF": { ... },
  "_regime_signals": ["SPY", "QQQ", "SMH", "VOOG"]
}
```

- `tier1: true` marks symbols eligible for swing / wider stops.
- `_regime_signals` lists ETF tickers used for regime/macro only (excluded from screener).
- `screener_eligible` defaults `true`; set `false` for GLD, regime signals.
- ETF key IS the sector ETF used for the health gate.

**Location:** `tickers.json` at project root. Loaded via `utils.io_utils.load_json`.

**Config change:** `TICKERS_FILE = BASE_DIR / "tickers.json"` (was `.txt`).

---

### 2b · New module: `sectors.py` (root level, same level as `tier1_config.py`)

Loads `tickers.json` **once at import** via `load_json`. Builds five O(1) dicts:

| Dict | Key → Value | Used by |
|---|---|---|
| `SYMBOL_TO_ETF` | `"NVDA"` → `"XLK"` | `pipeline.py`, `executor.py`, `entry_gates.py` |
| `SYMBOL_TO_SECTOR` | `"NVDA"` → `"XLK"` (same; named for clarity) | `correlation_guard.py`, `analyzer.py` |
| `TIER1_SET` | `frozenset{"NVDA", "AMD", …}` | `modes.py`, `eod_holds.py`, `research/agent.py` |
| `SYMBOL_TO_THEME` | `"NVDA"` → `"AI_GPU"` | future use |
| `ETF_TO_SYMBOLS` | `"XLK"` → `["NVDA", "AMD", …]` | `scheduler_jobs.py`, screener |
| `SCREENER_SYMBOLS` | all symbols where `screener_eligible != false` | screener |
| `TIER1_SYMBOLS` | ordered list of tier1 symbols | `scheduler_jobs.py` |

**Public functions:**

```python
def is_tier1(symbol: str) -> bool
def get_sector_etf(symbol: str) -> str       # "XLK", "XLF", … or "OTHER"
def get_sector(symbol: str) -> str           # alias of get_sector_etf for backward compat
def get_theme(symbol: str) -> str            # "AI_GPU", … or ""
def sector_of(symbol: str) -> str            # used by correlation_guard; same as get_sector_etf
def load_universe() -> list[str]             # all screener-eligible symbols, in-order
```

**Error handling:** If `tickers.json` is missing at import, log a critical error and expose
empty dicts. Bot degrades to "no universe" (screener runs with 0 tickers → sends Telegram).

---

### 2c · Updated `tier1_config.py` → deleted

All 6 callers updated to import from `sectors.py` directly. `qualifies_for_swing` (dead code,
no callers) removed. `tier1_config.py` deleted per Rule 7.

**Callers to update:**
| File | Old import | New import |
|---|---|---|
| `scheduler_jobs.py` | `TIER1_NAMES.keys()` | `sectors.TIER1_SYMBOLS` |
| `research/agent.py` | `is_tier1` | `sectors.is_tier1` |
| `research/analyzer.py` | `get_sector` | `sectors.get_sector` |
| `signals/correlation_guard.py` | `TIER1_NAMES` + `_EXTENDED_SECTOR` | `sectors.sector_of` |
| `trading/modes.py` | `is_tier1` | `sectors.is_tier1` |
| `trading/eod_holds.py` | `is_tier1, get_sector` | `sectors.is_tier1, sectors.get_sector` |

---

### 2d · `pipeline.py` — remove `_SECTOR_ETF_MAP`

Replace every `_SECTOR_ETF_MAP.get(symbol)` call with `sectors.get_sector_etf(symbol)`.
Delete the 38-line inline dict.

---

### 2e · `correlation_guard.py` — remove `_EXTENDED_SECTOR`

Replace `sector_of()` body with single `return sectors.get_sector_etf(symbol)`.
Delete `_EXTENDED_SECTOR` dict.

---

### 2f · `agents/volatility_screener.py` — load from `tickers.json`

Replace text-file parse + `isalpha()` filter with `sectors.load_universe()`. Fixes B13.

---

### 2g · `agents/watch_list.py` — update fallback

Replace `config.TICKERS_FILE` fallback read with `sectors.load_universe()`.

---

## Part 3 — Regime String Constants (`utils/regimes.py`)

### 3a · New module: `utils/regimes.py`

All regime and trade-mode strings are currently scattered as bare literals across ~15 files.
A single typo silently creates a new never-matched branch. Centralising them makes the
contract explicit and lets IDEs/grep find every usage.

**Constants (regime strings):**

```python
CRISIS          = "CRISIS"
CRASH_RECOVERY  = "CRASH_RECOVERY"
BULL_TREND      = "BULL_TREND"
CHOPPY          = "CHOPPY"
DEFENSIVE       = "DEFENSIVE"
NORMAL          = "NORMAL"
OPPORTUNITY     = "OPPORTUNITY"
CAPITULATION    = "CAPITULATION"
# HIGH_VOL is returned by the backtest simplified classifier (backtest/data.py) but
# never by the live classify_regime(). Defined here for backtest parity.
HIGH_VOL        = "HIGH_VOL"
```

**Pre-built frozensets** (replaces ad-hoc inline tuples at call sites):

```python
VALID: frozenset           # all valid live-classifier outputs (excludes HIGH_VOL)
ALLOW_ENTRY: frozenset     # regimes where new positions are allowed
HIGH_VOL_GATES: frozenset  # regimes that trigger the tighter COMPOSITE_MIN_HIGH_VOL gate
SIZE_REDUCE: frozenset     # regimes where position sizing is multiplied down
SWING_ELIGIBLE: frozenset  # regimes where EOD swing holds are evaluated
```

**Values:**

| Frozenset | Members |
|---|---|
| `VALID` | CRISIS, CRASH_RECOVERY, BULL_TREND, CHOPPY, DEFENSIVE, NORMAL, OPPORTUNITY, CAPITULATION |
| `ALLOW_ENTRY` | VALID − CRISIS |
| `HIGH_VOL_GATES` | HIGH_VOL, DEFENSIVE |
| `SIZE_REDUCE` | HIGH_VOL, DEFENSIVE, CHOPPY |
| `SWING_ELIGIBLE` | BULL_TREND, OPPORTUNITY, CAPITULATION, CRASH_RECOVERY |

**Migration:** Update inline string literals and tuple membership tests across callers.
The existing module-level frozensets `_HYBRID_REGIMES` / `_SHORT_TERM_REGIMES` in
`trading/modes.py` and `_SWING_ELIGIBLE_REGIMES` in `trading/eod_holds.py` are replaced
by the shared constants. No logic changes — rename only.

---

## Part 3b — Dead Code Removal

### D1 · `qualifies_for_swing` (`tier1_config.py`)

Zero callers anywhere in the codebase. The live swing gate is `qualifies_for_eod_hold()`
in `trading/eod_holds.py`. Deleted with `tier1_config.py` (see Part 2c).

### D2 · `force_close_all(scheduler=None)` pause/resume block (`trading/closer.py`)

The `scheduler` parameter is never passed by any caller (`scheduler_jobs.py` uses
`_job_guard("force_close", force_close_all)`, `market_monitor.py` calls
`force_close_all()` directly). The conditional pause/resume block is unreachable.

**Fix:** Remove the `scheduler` parameter and the entire `if scheduler:` block.

### D3 · `HIGH_VOL` unreachable branches in live pipeline

`backtest/data.py` returns `"HIGH_VOL"` from its simplified single-function classifier.
The live `classify_regime()` never does — it returns only the 8 strings in `VALID`.
All live-code checks like `regime in ("HIGH_VOL", "DEFENSIVE")` therefore have a dead
`HIGH_VOL` branch.

**Fix:** Replace bare tuple literals with the `regimes.HIGH_VOL_GATES` frozenset
(which retains HIGH_VOL for backtest parity). No behaviour change; the dead branch
is now correctly labeled by the frozenset name rather than silently misleading.
The backtest simulator is left unchanged.

### D4 · Inline frozensets in `trading/modes.py` and `trading/eod_holds.py`

`_HYBRID_REGIMES`, `_SHORT_TERM_REGIMES`, `_SWING_ELIGIBLE_REGIMES` are local
re-definitions of groupings now canonical in `utils/regimes.py`.

**Fix:** Delete the local sets; import from `regimes` directly.

---

## Part 4 — `config.py` Reorganization

Group 324 lines into 11 labeled sections. No values changed except additions.

```
# ── PATHS ─────────────────────────────────────────────────────────────────────
# ── API KEYS & MODELS ─────────────────────────────────────────────────────────
# ── RISK MANAGEMENT ───────────────────────────────────────────────────────────
# ── POSITION SIZING ───────────────────────────────────────────────────────────
# ── ENTRY GATES & TIMING ──────────────────────────────────────────────────────
# ── INDICATOR & COMPOSITE THRESHOLDS ─────────────────────────────────────────
# ── REGIME & MACRO ────────────────────────────────────────────────────────────
# ── SWING & EOD ───────────────────────────────────────────────────────────────
# ── BUDGET & API COST ─────────────────────────────────────────────────────────
# ── LEARNING & MEMORY ─────────────────────────────────────────────────────────
# ── NOTIFICATIONS, OPS & FEATURE FLAGS ───────────────────────────────────────
```

**New constants added:**
- `SQUEEZE_COMPOSITE_MIN = 12`
- `DEFENSIVE_COMPOSITE_MULT = 1.5`
- `METRICS_KEEP_LINES = 2000`
- `DRAWDOWN_ALERT_COOLDOWN_MIN = 30`

**Comment fixed:** `TELEMETRY_KEEP_LINES` — clarify it applies to `cycle_telemetry.jsonl`.

---

## File Change Summary

| Action | File |
|--------|------|
| **Create** | `tickers.json` |
| **Create** | `sectors.py` |
| **Create** | `utils/regimes.py` (Part 3) |
| **Delete** | `tickers.txt` |
| **Delete** | `tier1_config.py` |
| **Modify** | `config.py` |
| **Modify** | `trading/journal.py` (B1) |
| **Modify** | `scheduler_jobs.py` (B2 + 2c) |
| **Modify** | `learning/trade_analysis.py` (B3 + B8) |
| **Modify** | `signals/entry_gates.py` (B4 + B6 + B7 + D3) |
| **Modify** | `trading/pre_claude_filter.py` (B4 + B5 + D3) |
| **Modify** | `macro/regime.py` (B8) |
| **Modify** | `learning/agent.py` (B8) |
| **Modify** | `utils/metrics.py` (B9) |
| **Modify** | `trading/monitor.py` (B10) |
| **Modify** | `trading/eod_holds.py` (B11 + 2c + D4) |
| **Modify** | `trading/pipeline.py` (B12 + 2d + D3) |
| **Modify** | `trading/closer.py` (D2) |
| **Modify** | `trading/modes.py` (2c + D4) |
| **Modify** | `trading/cycle.py` (D3) |
| **Modify** | `agents/volatility_screener.py` (B13 + 2f) |
| **Modify** | `agents/watch_list.py` (2g) |
| **Modify** | `signals/correlation_guard.py` (2e) |
| **Modify** | `research/agent.py` (2c) |
| **Modify** | `research/analyzer.py` (2c) |
| **Modify** | `CLAUDE.md` (B7) |

---

## Testing Strategy

- All existing tests must still pass (`pytest tests/ -m unit -q`).
- New tests for `sectors.py`: `test_foundation.py` — cover `is_tier1`, `get_sector_etf`,
  `load_universe`, ETF→symbols reverse map.
- New tests for `utils/regimes.py`: `test_foundation.py` — verify frozenset membership
  (e.g. CRISIS not in ALLOW_ENTRY, DEFENSIVE in HIGH_VOL_GATES, all SWING_ELIGIBLE in VALID).
- New tests for `count_todays_losses` fix: `test_trading_budget.py` — assert progressive sizing
  activates after a simulated loss.
- Midday check test: `test_market.py` — assert no alarm when journal contains `type=BUY` entry.
- `force_close_all` test: `test_trading_lifecycle.py` — verify it no longer accepts a `scheduler`
  argument (call signature check).
