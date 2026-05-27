# Concurrency & Capital Deployment Design

**Date:** 2026-05-27
**Goal:** Grow returns and shrink drawdown simultaneously by deploying more capital through regime × tier × mode-gated concurrent positions with a portfolio heat ceiling, while protecting the swing profit engine via a swing-health gate.

---

## Problem Statement

Current daily capital utilization averages 16.4% (peak 37.2%) with `MAX_ENTRIES_PER_CYCLE=1` as the dominant throttle. The bot regularly has capacity to take more high-quality setups but discards them. However, blindly allowing more entries risks degrading overall P&L if intraday win rates are lower than swing win rates (81% WR). The solution must increase deployment on high-conviction setups while keeping drawdown bounded.

---

## Design

### 1. Data Model

#### `ConcurrencyParams` (new frozen dataclass in `utils/regime_params.py`)

```python
@dataclass(frozen=True)
class ConcurrencyParams:
    max_open:          int    # hard cap on simultaneous open intraday positions
    size_mult:         float  # multiplier applied after compute_position_size_for_mode()
    heat_weight:       float  # contribution factor toward regime heat ceiling (from Approach C)
    swing_health_gate: bool   # if True, collapse max_open to 1 when swing book is stressed
```

All four fields are required — no defaults. This prevents accidentally leaving a row partially configured.

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

Regimes match `utils/regime_params.py` REGIMES tuple exactly (9 values). `BULL_TREND_PARABOLIC` does not exist in `regime_params.py` — the high-volatility variant is `HIGH_VOL`.

Heat = sum of `(qty × avg_entry_price) / portfolio_value` across all intraday open positions. Swing positions are **excluded** from heat computation — they run a separate overnight budget.

#### Swing-health threshold (new constant in `config.py`)

```python
SWING_HEALTH_MIN_PNL_PCT: float = -2.0  # % unrealised P&L floor across swing book
```

When the swing book's aggregate unrealised P&L drops below this, the swing-health gate fires for any `ConcurrencyParams` row with `swing_health_gate=True`, collapsing `max_open` to 1.

---

### 2. Dispatch Table

`get_concurrency_params(regime, tier, mode) -> ConcurrencyParams` added to `utils/regime_params.py`.

Primary dispatch is `(regime, tier)`. Mode applies post-lookup modifiers:
- **PEAD or CATALYST**: `max_open += 1` applied to the total intraday open count (special-signal setups get one extra slot). For CATALYST, the existing `CATALYST_MAX_POSITIONS=2` cap in `config.py` remains as an independent CATALYST-specific guard checked separately in `executor.py` — it is not replaced.
- **SWING**: return `ConcurrencyParams(max_open=999, size_mult=1.0, heat_weight=0.0, swing_health_gate=False)` — swing is exempt from intraday concurrency and heat entirely
- **CRISIS** (any tier/mode): `max_open=0` — enforced at regime level, no entries

Representative table values (full table in implementation):

| regime | tier | max_open | size_mult | heat_weight | swing_health_gate |
|---|---|---|---|---|---|
| BULL_TREND | CONVICTION | 5 | 1.40 | 0.90 | False |
| BULL_TREND | HIGH | 4 | 1.20 | 1.00 | False |
| BULL_TREND | MEDIUM | 3 | 1.00 | 1.00 | True |
| BULL_TREND | LOW | 2 | 0.80 | 1.20 | True |
| NORMAL | CONVICTION | 4 | 1.30 | 0.90 | False |
| NORMAL | HIGH | 3 | 1.10 | 1.00 | True |
| NORMAL | MEDIUM | 2 | 1.00 | 1.00 | True |
| NORMAL | LOW | 1 | 0.80 | 1.20 | True |
| OPPORTUNITY | CONVICTION | 4 | 1.30 | 0.90 | False |
| OPPORTUNITY | HIGH | 3 | 1.10 | 1.00 | True |
| OPPORTUNITY | MEDIUM | 2 | 1.00 | 1.00 | True |
| OPPORTUNITY | LOW | 1 | 0.80 | 1.20 | True |
| CHOPPY | CONVICTION | 3 | 1.10 | 0.90 | True |
| CHOPPY | HIGH | 2 | 0.90 | 1.00 | True |
| CHOPPY | MEDIUM | 1 | 0.80 | 1.10 | True |
| CHOPPY | LOW | 1 | 0.70 | 1.30 | True |
| HIGH_VOL | CONVICTION | 3 | 1.10 | 0.90 | True |
| HIGH_VOL | HIGH | 2 | 0.90 | 1.00 | True |
| HIGH_VOL | MEDIUM | 1 | 0.80 | 1.10 | True |
| HIGH_VOL | LOW | 1 | 0.70 | 1.30 | True |
| CRASH_RECOVERY | CONVICTION | 3 | 1.10 | 0.90 | True |
| CRASH_RECOVERY | HIGH | 2 | 1.00 | 1.00 | True |
| CRASH_RECOVERY | MEDIUM | 1 | 0.90 | 1.00 | True |
| CRASH_RECOVERY | LOW | 1 | 0.80 | 1.20 | True |
| DEFENSIVE | CONVICTION | 2 | 1.00 | 1.00 | True |
| DEFENSIVE | HIGH | 1 | 0.90 | 1.00 | True |
| DEFENSIVE | MEDIUM | 1 | 0.80 | 1.10 | True |
| DEFENSIVE | LOW | 0 | — | — | True |
| CAPITULATION | CONVICTION | 2 | 1.00 | 1.00 | True |
| CAPITULATION | HIGH | 1 | 0.90 | 1.00 | True |
| CAPITULATION | MEDIUM | 0 | — | — | True |
| CAPITULATION | LOW | 0 | — | — | True |
| CRISIS | * | 0 | — | — | True |

**Design rationale for `size_mult`:**
- CONVICTION tiers get > 1.0 multiplier (more capital on highest-edge setups)
- LOW tiers get < 1.0 (more positions allowed in bull trend, but individually smaller)
- The heat ceiling acts as the absolute portfolio-level backstop regardless of size_mult

---

### 3. Enforcement — `trading/executor.py`

Three sequential checks added inside `execute_buy()`, after the existing CRISIS regime check and before `check_all_gates()`:

```
Step 1 — Get concurrency params
    cp = get_concurrency_params(regime, quality.tier, mode)

Step 2 — Swing-health gate
    if cp.swing_health_gate:
        swing_pnl = _compute_swing_book_pnl(open_swing_positions, portfolio_value)
        if swing_pnl < config.SWING_HEALTH_MIN_PNL_PCT:
            effective_max_open = 1
        else:
            effective_max_open = cp.max_open
    else:
        effective_max_open = cp.max_open

Step 3 — Max-open check
    intraday_count = len([p for p in open_positions.values() if not p.get("is_swing")])
    if intraday_count >= effective_max_open:
        SKIP

Step 4 — Heat ceiling check
    current_heat = sum(p["qty"] * p["avg_entry_price"] for p in open_positions.values()
                       if not p.get("is_swing")) / portfolio_value
    new_heat = (projected_qty * price / portfolio_value) * cp.heat_weight
    if current_heat + new_heat > config.REGIME_HEAT_CEILING.get(regime, 0.35):
        SKIP
```

`_compute_swing_book_pnl()` is a new private helper in `executor.py` that reads `swing_positions.json` and returns aggregate unrealised P&L as a percentage of portfolio value. It is called only when `cp.swing_health_gate=True`, so the file read is skipped for CONVICTION tier in bull regimes.

---

### 4. Position Sizing Integration

`size_mult` is applied in `trading/executor.py` after `compute_position_size_for_mode()` returns but before `apply_sentiment_multiplier()`:

```
qty = compute_position_size_for_mode(...)
qty = max(1, int(round(qty * cp.size_mult)))   # concurrency size multiplier
qty = apply_sentiment_multiplier(qty, sentiment_score)
```

This ordering means:
- ATR-based sizing and regime max_position_pct are applied first (floor)
- Concurrency size_mult scales up/down on top (conviction boost or low-tier reduction)
- Sentiment multiplier is applied last (unchanged)

---

### 5. `MAX_ENTRIES_PER_CYCLE` — retirement

`MAX_ENTRIES_PER_CYCLE` in `config.py` is replaced by `max_open` from `ConcurrencyParams`. The `entries_this_cycle` counter and its gate in `trading/pipeline.py` are removed. The pipeline now calls `execute_buy()` for every CLAUDE/AUTO_EXECUTE candidate and lets the concurrency + heat checks decide per-symbol.

The `entries_this_cycle` parameter in `execute_buy()` signature is removed. Callers (`_execute_auto_entries`, `_execute_claude_entries` in `pipeline.py`) are updated accordingly.

---

### 6. Config Changes (`config.py`)

New section `# ── Concurrency ──`:

```python
REGIME_HEAT_CEILING: dict[str, float] = { ... }   # per-regime heat caps
SWING_HEALTH_MIN_PNL_PCT: float = -2.0             # swing book P&L gate threshold
```

Remove:
```python
MAX_ENTRIES_PER_CYCLE = 1
```

---

### 7. Error Handling

- If `quality` is `None` when `get_concurrency_params` is called, default to `("NORMAL", "MEDIUM", "DAY_TRADE")` params — same conservative behaviour as today.
- If `swing_positions.json` is unreadable, `_compute_swing_book_pnl` returns `0.0` (treat book as healthy — fail open rather than blocking all entries on a file error).
- If `regime` is not in `REGIME_HEAT_CEILING`, default ceiling is `0.35` (NORMAL equivalent).

---

### 8. Testing

New tests in `tests/test_trading_modes.py`:

| Test | Asserts |
|---|---|
| `test_concurrency_blocks_at_max_open` | 4th entry blocked when NORMAL×HIGH has max_open=3 |
| `test_heat_ceiling_blocks_entry` | entry blocked when current_heat + new_heat > ceiling |
| `test_swing_health_gate_collapses_max_open` | swing P&L < -2% → effective_max_open=1 even for HIGH tier |
| `test_swing_health_gate_skipped_for_conviction_bull` | BULL_TREND×CONVICTION does not read swing file |
| `test_pead_mode_gets_plus1_slot` | PEAD mode gets max_open one higher than base DAY_TRADE row |
| `test_swing_mode_exempt_from_heat` | SWING position not counted in heat computation |
| `test_size_mult_applied_before_sentiment` | size_mult applied before sentiment multiplier |
| `test_crisis_max_open_zero` | CRISIS regime returns max_open=0 for all tiers |

All tests: `@pytest.mark.unit`, no network calls, `monkeypatch` for all config values, `tmp_path` for file I/O.

---

### 9. Data Flow Summary

```
pipeline.py
  ↓ calls execute_buy() for each CLAUDE/AUTO_EXECUTE candidate
executor.py::execute_buy()
  1. get_concurrency_params(regime, quality.tier, mode)   [regime_params.py]
  2. swing-health gate → effective_max_open               [swing_positions.json]
  3. max-open check   → SKIP if at limit
  4. heat ceiling check → SKIP if over budget             [open_positions.json]
  5. check_all_gates() — unchanged, still sole safety enforcer
  6. compute_position_size_for_mode()
  7. apply size_mult
  8. apply_sentiment_multiplier()
  9. place order
```

---

### 10. Out of Scope

- Swing position sizing changes — swing TP/SL table and SwingParams are unchanged.
- Learning agent weight tuning — no changes to `learning/agent.py`.
- API budget pools — entry 70% / exit 30% split unchanged.
- Telegram alert thresholds — no changes.
