# Concurrency & Capital Deployment Design

**Date:** 2026-05-27 (rev 2 — post-audit fixes)
**Goal:** Grow returns and shrink drawdown simultaneously by deploying more capital through regime × tier × mode-gated concurrent positions with a portfolio heat ceiling, while protecting the swing profit engine via a swing-health gate.

---

## Problem Statement

Current daily capital utilization averages 16.4% (peak 37.2%) with `MAX_ENTRIES_PER_CYCLE=1` as the dominant throttle. The bot regularly discards high-quality setups. The solution must increase deployment on high-conviction setups while keeping drawdown bounded and the swing profit engine protected.

---

## Audit Fixes Applied (pre-implementation review)

All 8 issues from the pre-implementation audit are addressed in this revision:

| # | Severity | Issue | Resolution |
|---|---|---|---|
| 1 | CRITICAL | Gate #2 in `check_all_gates()` not updated | Gate #2 removed from `signals/entry_gates.py`; replaced by per-cycle burst cap in pipeline.py |
| 2 | CRITICAL | `size_mult > 1.0` breaks `max_position_pct` cap | Re-clamp qty to max_position_pct after size_mult via dedicated helper |
| 3 | CRITICAL | `projected_qty` unavailable at heat-check time | Use `max_position_pct × portfolio_value / price` as worst-case heat estimate |
| 4 | CRITICAL | Stale `open_positions` within same cycle | Count + heat checks re-read `open_positions.json` inside `_POSITIONS_LOCK` |
| 5 | HIGH | `size_mult` undermines `HIGH_VOL_POSITION_MULT=0.75` | `size_mult` capped at 1.0 for HIGH_VOL, DEFENSIVE, CAPITULATION, CRASH_RECOVERY |
| 6 | HIGH | All slots could fill in one cycle (burst risk) | `MAX_ENTRIES_PER_CYCLE` renamed → `MAX_ENTRIES_PER_CYCLE_BURST=3`; enforced in pipeline.py only |
| 7 | MEDIUM | Swing P&L current price source unspecified | Use `iv.current_price` for watch-list swings; entry_price for non-watch-list; no Alpaca calls |
| 8 | MEDIUM | Sizing chain position vs `TRANCHE1_SIZE_PCT` ambiguous | `size_mult` applied after `apply_regime_multiplier`, before `apply_sentiment_multiplier`, before TRANCHE1 cut |

---

## Design

### 1. Data Model

#### `ConcurrencyParams` (new frozen dataclass in `utils/regime_params.py`)

```python
@dataclass(frozen=True)
class ConcurrencyParams:
    max_open:          int    # hard cap on simultaneous open intraday positions
    size_mult:         float  # multiplier after apply_regime_multiplier(), before sentiment
    heat_weight:       float  # contribution factor toward regime heat ceiling
    swing_health_gate: bool   # if True, collapse max_open to 1 when swing book is stressed
```

All four fields are required — no defaults. This prevents accidentally leaving a row partially configured.

**Safe sentinel value:** `_CONCURRENCY_DISABLED = ConcurrencyParams(max_open=0, size_mult=1.0, heat_weight=1.0, swing_health_gate=True)` used for CRISIS and disabled rows.

**Conservative fallback:** `_CONCURRENCY_CONSERVATIVE = ConcurrencyParams(max_open=1, size_mult=1.0, heat_weight=1.0, swing_health_gate=True)` — returned by `get_concurrency_params()` on any error or unknown regime/tier. **Fail-closed, not fail-open.** This means a lookup error produces today's safe behaviour (1 concurrent position, no size boost) rather than unlocking the full table.

#### `REGIME_HEAT_CEILING` (new dict in `config.py`, under `# ── Concurrency ──`)

```python
REGIME_HEAT_CEILING: dict[str, float] = {
    "BULL_TREND":     0.50,
    "OPPORTUNITY":    0.45,
    "NORMAL":         0.35,
    "CRASH_RECOVERY": 0.25,
    "HIGH_VOL":       0.25,
    "CHOPPY":         0.20,
    "DEFENSIVE":      0.10,
    "CAPITULATION":   0.10,
    "CRISIS":         0.00,
}
```

Regimes match `utils/regime_params.py` REGIMES tuple exactly (9 values).

Heat = sum of `(qty × avg_entry_price) / portfolio_value` across all intraday open positions. Swing positions (`is_swing=True`) are **excluded** — they run a separate overnight budget.

**Default on unknown regime:** `0.20` (CHOPPY-equivalent — conservative, not permissive).

#### New/renamed constants in `config.py`

```python
# ── Concurrency ──
REGIME_HEAT_CEILING:        dict[str, float] = { ... }  # per-regime heat caps
SWING_HEALTH_MIN_PNL_PCT:   float = -2.0                # swing book P&L gate threshold
MAX_ENTRIES_PER_CYCLE_BURST: int  = 3                   # max new entries within one 10-min cycle
```

`MAX_ENTRIES_PER_CYCLE = 1` is **renamed** to `MAX_ENTRIES_PER_CYCLE_BURST = 3`. It is no longer enforced in `check_all_gates()` — only in `pipeline.py`. See Section 5.

---

### 2. Dispatch Table

`get_concurrency_params(regime: str, tier: str, mode: str) -> ConcurrencyParams` added to `utils/regime_params.py`.

```python
def get_concurrency_params(regime: str, tier: str, mode: str) -> ConcurrencyParams:
    try:
        cp = _CONCURRENCY_TABLE.get((regime, tier), _CONCURRENCY_CONSERVATIVE)
        if regime == "CRISIS":
            return _CONCURRENCY_DISABLED
        if mode in ("PEAD", "CATALYST"):
            cp = ConcurrencyParams(
                max_open=cp.max_open + 1,
                size_mult=cp.size_mult,
                heat_weight=cp.heat_weight,
                swing_health_gate=cp.swing_health_gate,
            )
        if mode == "SWING":
            return ConcurrencyParams(max_open=999, size_mult=1.0, heat_weight=0.0, swing_health_gate=False)
        return cp
    except Exception:
        return _CONCURRENCY_CONSERVATIVE  # fail-closed
```

**`size_mult` is capped at 1.0 for volatile/defensive regimes** (fix #5): HIGH_VOL, DEFENSIVE, CAPITULATION, and CRASH_RECOVERY rows in the table all have `size_mult ≤ 1.0`. This preserves `HIGH_VOL_POSITION_MULT=0.75` and other protective regime reductions.

**Full dispatch table:**

| regime | tier | max_open | size_mult | heat_weight | swing_health_gate |
|---|---|---|---|---|---|
| BULL_TREND | CONVICTION | 5 | 1.35 | 0.90 | False |
| BULL_TREND | HIGH | 4 | 1.15 | 1.00 | False |
| BULL_TREND | MEDIUM | 3 | 1.00 | 1.00 | True |
| BULL_TREND | LOW | 2 | 0.80 | 1.20 | True |
| NORMAL | CONVICTION | 4 | 1.25 | 0.90 | False |
| NORMAL | HIGH | 3 | 1.10 | 1.00 | True |
| NORMAL | MEDIUM | 2 | 1.00 | 1.00 | True |
| NORMAL | LOW | 1 | 0.80 | 1.20 | True |
| OPPORTUNITY | CONVICTION | 4 | 1.25 | 0.90 | False |
| OPPORTUNITY | HIGH | 3 | 1.10 | 1.00 | True |
| OPPORTUNITY | MEDIUM | 2 | 1.00 | 1.00 | True |
| OPPORTUNITY | LOW | 1 | 0.80 | 1.20 | True |
| CHOPPY | CONVICTION | 3 | 1.00 | 0.90 | True |
| CHOPPY | HIGH | 2 | 0.90 | 1.00 | True |
| CHOPPY | MEDIUM | 1 | 0.80 | 1.10 | True |
| CHOPPY | LOW | 1 | 0.70 | 1.30 | True |
| HIGH_VOL | CONVICTION | 3 | 1.00 | 0.90 | True |
| HIGH_VOL | HIGH | 2 | 0.90 | 1.00 | True |
| HIGH_VOL | MEDIUM | 1 | 0.80 | 1.10 | True |
| HIGH_VOL | LOW | 1 | 0.70 | 1.30 | True |
| CRASH_RECOVERY | CONVICTION | 3 | 1.00 | 0.90 | True |
| CRASH_RECOVERY | HIGH | 2 | 0.90 | 1.00 | True |
| CRASH_RECOVERY | MEDIUM | 1 | 0.90 | 1.00 | True |
| CRASH_RECOVERY | LOW | 1 | 0.80 | 1.20 | True |
| DEFENSIVE | CONVICTION | 2 | 1.00 | 1.00 | True |
| DEFENSIVE | HIGH | 1 | 0.90 | 1.00 | True |
| DEFENSIVE | MEDIUM | 1 | 0.80 | 1.10 | True |
| DEFENSIVE | LOW | 0 | 1.00 | 1.00 | True |
| CAPITULATION | CONVICTION | 2 | 1.00 | 1.00 | True |
| CAPITULATION | HIGH | 1 | 0.90 | 1.00 | True |
| CAPITULATION | MEDIUM | 0 | 1.00 | 1.00 | True |
| CAPITULATION | LOW | 0 | 1.00 | 1.00 | True |
| CRISIS | * | 0 | 1.00 | 1.00 | True |

**Notes:**
- `size_mult > 1.0` only in BULL_TREND, NORMAL, OPPORTUNITY — regimes with positive macro health where larger conviction sizing improves expected value
- LOW tier `max_open=0` rows (DEFENSIVE, CAPITULATION, CRISIS) use `size_mult=1.0` — value is irrelevant but must be a valid float for the frozen dataclass
- CATALYST's existing `CATALYST_MAX_POSITIONS=2` cap in `executor.py` is **not removed** — it is an independent per-mode cap that coexists with `max_open`

---

### 3. Enforcement — `trading/executor.py`

Four checks added inside `execute_buy()`, positioned **after** the CRISIS regime check (line 87) and **before** `check_all_gates()` (line 131). All four are wrapped in a single try/except that fails closed.

```python
# ── Concurrency + heat guard ─────────────────────────────────────────────────
try:
    cp = get_concurrency_params(regime, quality.tier if quality else "MEDIUM", mode)

    # A. Max-open check + swing-health gate — re-read disk inside lock
    with _POSITIONS_LOCK:
        disk_positions = load_json(config.OPEN_POSITIONS_FILE, {})
    intraday_open = [p for p in disk_positions.values() if not p.get("is_swing")]
    intraday_count = len(intraday_open)

    effective_max_open = cp.max_open
    if cp.swing_health_gate:
        swing_pnl = _compute_swing_book_pnl(stock_data_prices, portfolio_value)
        if swing_pnl < config.SWING_HEALTH_MIN_PNL_PCT:
            effective_max_open = min(effective_max_open, 1)
            logger.info(
                "CONCURRENCY %s — swing book stressed (%.1f%%) → max_open capped at 1",
                symbol, swing_pnl,
            )

    if intraday_count >= effective_max_open:
        logger.info("SKIP %s — concurrency limit %d/%d (regime=%s tier=%s)",
                    symbol, intraday_count, effective_max_open, regime,
                    quality.tier if quality else "?")
        return None

    # B. Heat ceiling check — use max_position_pct as worst-case projection (fix #3)
    if portfolio_value > 0:
        max_pct = get_entry_params(regime).max_position_pct
        projected_heat = (portfolio_value * max_pct / iv.current_price
                          * iv.current_price / portfolio_value) * cp.heat_weight
        current_heat = sum(
            p.get("qty", 0) * p.get("entry_price", 0)
            for p in intraday_open
        ) / portfolio_value
        ceiling = config.REGIME_HEAT_CEILING.get(regime, 0.20)  # conservative default
        if current_heat + projected_heat > ceiling:
            logger.info(
                "SKIP %s — heat ceiling %.0f%% would be breached (current=%.1f%% + projected=%.1f%%)",
                symbol, ceiling * 100, current_heat * 100, projected_heat * 100,
            )
            return None

    # C. Daily-loss downshift — approaching loss limit → collapse max_open to 1
    if daily_pnl_pct <= -(config.DAILY_LOSS_WARN_PCT * 100):
        if intraday_count >= 1:
            logger.info("SKIP %s — near daily loss limit (%.2f%%), allowing max 1 position", symbol, daily_pnl_pct)
            return None

except Exception as exc:
    logger.warning("Concurrency guard raised unexpectedly for %s — blocking entry: %s", symbol, exc)
    return None  # fail-closed: any error blocks the entry
# ── end concurrency guard ────────────────────────────────────────────────────
```

**`_compute_swing_book_pnl(stock_data_prices: dict, portfolio_value: float) -> float`** — new private helper in `executor.py`:

```
1. Read swing_positions.json via load_json (io_utils — no direct json.load)
2. On any read error → return -999.0  (fail-closed: treat book as maximally stressed)
3. For each swing position:
   - If symbol is in stock_data_prices dict → use that as current_price (zero latency)
   - Else → use entry_price stored in swing position (conservative; assumes no gain)
4. unrealised_pnl_pct = sum((current_price - entry_price) / entry_price * qty * entry_price)
                         / portfolio_value * 100
5. Return unrealised_pnl_pct
6. On any computation error → return -999.0  (fail-closed)
```

`stock_data_prices` is a `dict[str, float]` of `{symbol: iv.current_price}` built in `execute_buy()` from the `iv` argument — zero extra API calls.

**Fail-closed matrix for Section 3:**

| Failure | Behaviour |
|---|---|
| `get_concurrency_params()` raises | Entry BLOCKED — conservative fallback logs and returns None |
| `load_json(OPEN_POSITIONS_FILE)` fails | Entry BLOCKED — treat as count unknown |
| `_compute_swing_book_pnl` read error | Swing gate fires — effective_max_open collapses to 1 |
| `_compute_swing_book_pnl` compute error | Swing gate fires — effective_max_open collapses to 1 |
| `portfolio_value == 0` | Heat check skipped — no div-by-zero; max_open check still runs |
| Unknown regime in REGIME_HEAT_CEILING | Uses 0.20 default (CHOPPY-equivalent, conservative) |
| Unknown (regime, tier) in dispatch table | `_CONCURRENCY_CONSERVATIVE` returned (max_open=1) |

---

### 4. Position Sizing Integration

Corrected sizing chain in `trading/executor.py` (fix #2 and #8):

```python
# Step 1: ATR-based sizing with max_position_pct cap + progressive loss reduction
qty = compute_position_size_for_mode(
    portfolio_value, iv.current_price, iv.atr,
    iv.bot_herding_score, iv.squeeze_setup, regime=regime
)

# Step 2: Regime multiplier (e.g. HIGH_VOL = 0.75x)
qty = apply_regime_multiplier(qty, pos_mult)

# Step 3: Concurrency size multiplier — applied here, then re-clamped to max_position_pct
qty = max(1, int(round(qty * cp.size_mult)))
max_qty_by_pct = int((portfolio_value * get_entry_params(regime).max_position_pct) / iv.current_price)
qty = min(qty, max(1, max_qty_by_pct))  # re-clamp: size_mult cannot breach regime size cap

# Step 4: Sentiment multiplier
qty = apply_sentiment_multiplier(qty, symbol_sentiment)

# Step 5: T1 tranche (60% of sized position)
qty = max(1, int(qty * config.TRANCHE1_SIZE_PCT))
```

**Why this order is safe:**
- `size_mult > 1.0` only applies in BULL_TREND/NORMAL/OPPORTUNITY (positive regimes)
- Re-clamp after size_mult ensures the regime's `max_position_pct` is never breached regardless of size_mult value
- HIGH_VOL's `pos_mult=0.75` is applied at Step 2; HIGH_VOL table has `size_mult ≤ 1.0`, so the 0.75 reduction is fully preserved
- TRANCHE1 cut is the last step, so T1 = 60% of the conviction-adjusted, capped, sentiment-scaled size

---

### 5. Per-Cycle Burst Cap — `MAX_ENTRIES_PER_CYCLE_BURST`

`MAX_ENTRIES_PER_CYCLE = 1` is **renamed** to `MAX_ENTRIES_PER_CYCLE_BURST = 3` in `config.py`.

**Gate #2 (`entries_this_cycle >= MAX_ENTRIES_PER_CYCLE`) is removed from `signals/entry_gates.py`:**
- Remove parameter `entries_this_cycle: int` from `check_all_gates()` signature
- Remove Gate #2 body check (lines 130-132)
- Remove `entries_this_cycle` from all callers (`executor.py` call site at line 134)

**Burst cap enforcement moves to `trading/pipeline.py`** — in both `_execute_auto_entries` and `_execute_claude_entries`, track `entries_this_cycle` locally and `break` the loop once `entries_this_cycle >= config.MAX_ENTRIES_PER_CYCLE_BURST`:

```python
# pipeline.py — inside _execute_auto_entries / _execute_claude_entries
for symbol, iv, quality in candidates:
    if entries_this_cycle >= config.MAX_ENTRIES_PER_CYCLE_BURST:
        logger.info("[BURST-CAP] %d entries this cycle — remaining candidates skipped", entries_this_cycle)
        break
    ...
    if pos_data is not None:
        entries_this_cycle += 1
```

**Why this is safer than full removal:**
- `max_open` governs total concurrent positions across all cycles (portfolio-level)
- `MAX_ENTRIES_PER_CYCLE_BURST=3` governs entries within one 10-minute window (burst protection)
- Even in BULL_TREND with max_open=5 and 2 existing positions, at most 3 new entries per cycle — never all 5 in a single window
- Prevents correlated entries at the same price point if watch list all signals simultaneously

---

### 6. `signals/entry_gates.py` Changes (fix #1)

```python
# REMOVE from check_all_gates() signature:
entries_this_cycle: int,

# REMOVE Gate #2 body:
# 2. Per-cycle entry cap
if entries_this_cycle >= config.MAX_ENTRIES_PER_CYCLE:
    return GateResult(False, "per-cycle entry cap reached")
```

All test files that pass `entries_this_cycle` to `check_all_gates()` must be updated to remove that argument. Search: `grep -rn "entries_this_cycle" tests/`.

---

### 7. Config Changes (`config.py`)

**Add** under `# ── Concurrency ──`:
```python
REGIME_HEAT_CEILING:         dict[str, float] = { ... }
SWING_HEALTH_MIN_PNL_PCT:    float = -2.0
MAX_ENTRIES_PER_CYCLE_BURST: int   = 3
```

**Remove/rename:**
```python
MAX_ENTRIES_PER_CYCLE = 1   # → renamed to MAX_ENTRIES_PER_CYCLE_BURST = 3
```

---

### 8. Data Flow Summary

```
pipeline.py
  ↓ Phase 4: calls execute_buy() for each CLAUDE/AUTO_EXECUTE candidate
  ↓ breaks loop when entries_this_cycle >= MAX_ENTRIES_PER_CYCLE_BURST (=3)

executor.py::execute_buy()
  A. VWAP slope check (existing)
  B. CRISIS regime check (existing)
  C. [NEW] Concurrency guard (all wrapped in try/except → fail-closed)
     C1. get_concurrency_params(regime, tier, mode)    [regime_params.py, fail→conservative]
     C2. Re-read open_positions.json inside _POSITIONS_LOCK
     C3. Swing-health gate (fail→ -999 → max_open collapses to 1)
     C4. Max-open check (intraday count vs effective_max_open)
     C5. Heat ceiling check (worst-case projection vs REGIME_HEAT_CEILING)
     C6. Daily-loss downshift (near DAILY_LOSS_WARN_PCT → cap at 1 position)
  D. Daily loss halt check (existing)
  E. CATALYST position limit (existing, unchanged)
  F. check_all_gates() — sole safety enforcer (entries_this_cycle param removed)
  G. effective_composite_gate() (existing)
  H. compute_position_size_for_mode()               [ATR + max_position_pct cap]
  I. apply_regime_multiplier()                       [HIGH_VOL 0.75x etc.]
  J. [NEW] apply cp.size_mult + re-clamp to max_position_pct
  K. apply_sentiment_multiplier()
  L. × TRANCHE1_SIZE_PCT (0.60)
  M. Concurrent guard re-read inside _POSITIONS_LOCK (existing, duplicate detection)
  N. place_bracket_order()
```

---

### 9. Testing

New tests in `tests/test_trading_modes.py` and `tests/test_signals.py`:

| Test | File | Asserts |
|---|---|---|
| `test_concurrency_blocks_at_max_open` | trading_modes | 4th entry blocked when NORMAL×HIGH has max_open=3 |
| `test_concurrency_lock_reread_open_positions` | trading_modes | Count check uses disk state, not stale in-memory dict |
| `test_heat_ceiling_blocks_entry` | trading_modes | Entry blocked when projected heat exceeds ceiling |
| `test_heat_uses_worst_case_projection` | trading_modes | Heat estimated from max_position_pct, not actual qty |
| `test_swing_health_gate_collapses_max_open` | trading_modes | swing P&L < -2% → effective_max_open=1 |
| `test_swing_health_gate_fail_closed` | trading_modes | swing_positions.json unreadable → entry blocked |
| `test_swing_health_gate_skipped_conviction_bull` | trading_modes | BULL_TREND×CONVICTION skips swing file read |
| `test_pead_mode_gets_plus1_slot` | trading_modes | PEAD mode gets max_open one higher than DAY_TRADE row |
| `test_swing_mode_exempt_from_heat` | trading_modes | SWING position not counted in heat computation |
| `test_size_mult_reclamped_to_max_position_pct` | trading_modes | size_mult=1.35 never produces qty > max_position_pct |
| `test_size_mult_chain_order` | trading_modes | size_mult applied after regime_mult, before sentiment, before TRANCHE1 |
| `test_high_vol_size_mult_at_most_one` | trading_modes | HIGH_VOL rows have size_mult ≤ 1.0 |
| `test_crisis_max_open_zero` | trading_modes | CRISIS returns max_open=0 for all tiers |
| `test_concurrency_fail_closed_on_error` | trading_modes | Any exception in concurrency guard blocks entry |
| `test_daily_loss_downshift` | trading_modes | Near DAILY_LOSS_WARN_PCT → max 1 position allowed |
| `test_burst_cap_in_pipeline` | trading_pipeline | Pipeline stops after MAX_ENTRIES_PER_CYCLE_BURST entries |
| `test_gate2_removed_from_check_all_gates` | signals | check_all_gates() has no entries_this_cycle parameter |

All tests: `@pytest.mark.unit`, no network calls, `monkeypatch` for all config values, `tmp_path` for file I/O.

---

### 10. Out of Scope

- Swing position sizing changes — swing TP/SL table and SwingParams are unchanged.
- Learning agent weight tuning — no changes to `learning/agent.py`.
- API budget pools — entry 70% / exit 30% split unchanged.
- Telegram alert thresholds — no changes.
- T2 tranche heat accounting — T2 is a pullback entry on an existing position; it does not create a new `open_positions` entry and does not affect the intraday count or heat check.
