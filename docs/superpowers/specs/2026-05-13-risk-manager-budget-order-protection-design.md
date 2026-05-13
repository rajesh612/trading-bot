# Risk Manager: Budget Reservation + Order Protection Design

**Date:** 2026-05-13
**Scope:** Part A (budget-aware exit reservation) + Part B (gap guard + fill-price correction)
**Follow-on:** Part C (intraday news / sporadic event monitoring) — separate spec

---

## Problem Statement

Two independent capital-protection failures currently exist:

**A — Budget exhaustion silences exits.** Claude is never called for open positions during the 10-min trading cycle; all exits are mechanical (Alpaca bracket SL/TP). When the token budget is exhausted, thesis-break situations (news shock, composite collapse) go undetected until a bracket leg triggers — which may be far below where a Claude-driven close would have acted.

**B — Bracket orders anchored to stale prices.** `place_bracket_order()` computes SL/TP from `iv.current_price` (last 5-min bar close). If a market order fills at a higher price due to a gap or intraday spike, the SL is relatively too deep and the TP is too close — creating asymmetric risk the bot does not correct. Additionally, there is no guard against entering a stock that has already gapped up significantly.

---

## Architecture

### New module: `trading/risk_manager.py`

Single authority for all risk decisions. Absorbs `api_budget.py` entirely — that file is deleted. All callers updated to import from `trading.risk_manager`.

```
trading/risk_manager.py
  ├── Budget pool management        (absorbed from api_budget.py)
  │     ├── check_entry_budget()
  │     ├── check_exit_budget()     ← new: auto-escalates with Telegram alert
  │     └── record_usage(kind)      ← new: routes to entry or exit field
  ├── Thesis-break heuristic        ← new
  │     └── needs_exit_evaluation()
  ├── Gap guard                     ← new
  │     └── is_gap_skip()
  └── Fill price correction         ← new
        └── correct_bracket_after_fill()
```

### Files changed / deleted

| File | Change |
|---|---|
| `trading/risk_manager.py` | **NEW** — all risk logic lives here |
| `api_budget.py` | **DELETED** — absorbed into risk_manager |
| `config.py` | Add 8 new constants (see Config section) |
| `indicator_engine.py` | Add `prev_close: float = 0.0` to `IndicatorValues` |
| `trading/pipeline.py` | Add Phase 2.5 (thesis-break eval); update budget call kind |
| `trading/claude_caller.py` | Import from risk_manager; pass `kind` to budget check |
| `trading/executor.py` | Add gap guard call; add fill correction after order placement |
| `signals/entry_gates.py` | Add gate 12 (gap guard); add `prev_close` parameter |
| `alpaca_client.py` | Add `get_order_by_id()` helper for fill polling |
| `watch_list_agent.py` | Update import to risk_manager |
| `research/analyzer.py` | Update import to risk_manager |
| `learning/agent.py` | Update import to risk_manager |
| `tests/test_market.py` | Update all `api_budget` imports to `trading.risk_manager` |
| `telegram_notify.py` | Add `notify_budget_escalation()` function |
| `tests/test_trading.py` | Update patches; add 4 new test classes |

---

## Section A: Budget Reservation

### JSON schema — `api_budget.json`

Existing fields preserved. New fields added:

```json
{
  "date": "2026-05-13",
  "calls_made": 0,
  "entry_calls": 0,
  "exit_calls": 0,
  "input_tokens": 0,
  "output_tokens": 0,
  "cache_write_tokens": 0,
  "cache_read_tokens": 0,
  "estimated_cost_usd": 0.0,
  "entry_spent_usd": 0.0,
  "exit_spent_usd": 0.0,
  "ceiling_usd": 1.00,
  "entry_ceiling_usd": 0.70,
  "exit_ceiling_usd": 0.30,
  "ceiling_mode": "normal",
  "entry_exhausted": false,
  "exit_exhausted": false,
  "budget_exhausted": false,
  "escalated": false
}
```

`budget_exhausted` remains True only when BOTH pools are exhausted after escalation, so existing callers that check this flag continue to work correctly.

### `check_entry_budget(estimated_cost, gain_pct, regime) → bool`

- Returns False if `entry_spent_usd + estimated_cost > entry_ceiling_usd`
- Logs warning at `API_BUDGET_WARNING_PCT` of entry ceiling
- Sets `entry_exhausted = True` when blocked

### `check_exit_budget(estimated_cost, open_position_count, regime) → bool`

- Returns False if `exit_spent_usd + estimated_cost > exit_ceiling_usd`
- **Auto-escalation:** when exit pool is exhausted AND `open_position_count > 0` AND `escalated = False`:
  1. Switch ceiling to `API_BUDGET_ELEVATED_CEILING`
  2. Recompute `entry_ceiling_usd` and `exit_ceiling_usd` from new total
  3. Set `escalated = True`, `ceiling_mode = "elevated"`
  4. Fire `telegram_notify.notify_budget_escalation()` (new notify function, best-effort)
  5. Re-evaluate — return True if new exit pool has room
- When both pools exhausted post-escalation: set `budget_exhausted = True`

### `record_usage(input_tokens, output_tokens, cache_write_tokens, cache_read_tokens, kind="entry") → None`

- `kind`: `"entry"` (default) or `"exit"` — default preserves backward compatibility for callers that do not pass `kind` (`watch_list_agent`, `research/analyzer`, `learning/agent`)
- Routes token cost to `entry_spent_usd` or `exit_spent_usd`
- Also accumulates totals in `estimated_cost_usd` (unchanged — existing dashboards read this)
- Increments `entry_calls` or `exit_calls` accordingly
- Only `trading/claude_caller.py` passes `kind="exit"` explicitly for exit batch calls

### Pipeline Phase 2.5 — thesis-break detection

Inserted between Phase 2 (gate checks) and Phase 3 (Claude batch) in `trading/pipeline.py`.

For each `(symbol, iv, existing_pos, ...)` where `existing_pos is not None`:

```python
if risk_manager.needs_exit_evaluation(iv, existing_pos, research):
    exit_candidates.append((symbol, iv, existing_pos))
```

`needs_exit_evaluation(iv, position, research) → bool` — pure function, no I/O:

Returns True when ANY condition holds:
- `iv.composite_score < position["entry_composite_score"] - config.EXIT_EVAL_COMPOSITE_DROP`
- `research.get(symbol, {}).get("sentiment_score", 0) < config.EXIT_EVAL_SENTIMENT_FLOOR`
- PnL within 1% of stop-loss: `(iv.current_price - position["entry_price"]) / position["entry_price"] * 100 > -(position["stop_loss_pct"] - 1.0)`

Exit candidates are evaluated first in Phase 3 using a **compact exit prompt** (~300 input tokens vs ~800 for entry calls). The compact prompt contains only: symbol, current price, entry price, PnL%, composite score delta, and sentiment score. No strategy memory, no stable context block — exit calls are intentionally minimal to stay within the 30% exit pool.

---

## Section B: Order Pricing Protection

### Gate 12 — gap guard in `signals/entry_gates.py`

No new parameter needed — gate reads `iv.prev_close` directly from the dataclass.

```python
# 12. Gap-up skip
if iv.prev_close > 0 and iv.current_price > iv.prev_close * (1 + config.GAP_SKIP_PCT / 100):
    gap_pct = (iv.current_price / iv.prev_close - 1) * 100
    return GateResult(False, f"gap-up skip ({gap_pct:.1f}% > {config.GAP_SKIP_PCT}%)")
```

`iv.prev_close` populated in `compute_indicators()` from `daily_df["close"].iloc[-2]` when `daily_df` has ≥ 2 rows. Gate runs before Claude is called — zero token cost.

### `IndicatorValues` — new field

```python
@dataclass
class IndicatorValues:
    ...
    prev_close: float = 0.0   # daily T-1 close; 0.0 when daily bars unavailable
```

Populated in `compute_indicators()` from `daily_df["close"].iloc[-2]` when `daily_df` has ≥ 2 rows.

### Fill price correction in `trading/risk_manager.py`

`correct_bracket_after_fill(trading_client, order_id, estimated_price, sl_pct, tp_pct) → dict | None`

`estimated_price` is `iv.current_price` at the time the order was placed — used as the slippage baseline.

```
1. Poll trading_client.get_order_by_id(order_id) up to FILL_POLL_ATTEMPTS times
   (sleep FILL_POLL_INTERVAL_S between attempts)
2. If status == 'filled':
   fill_price = float(order.filled_avg_price)
   slippage = abs(fill_price - estimated_price) / estimated_price
   if slippage > FILL_SLIPPAGE_THRESHOLD:
       new_sl = round(fill_price * (1 - sl_pct / 100), 2)
       new_tp = round(fill_price * (1 + tp_pct / 100), 2)
       for leg in order.legs:
           if leg.type == 'stop': replace with new_sl
           if leg.type == 'limit': replace with new_tp
       return {"stop_loss_price": new_sl, "take_profit_price": new_tp, "fill_price": fill_price}
3. If poll exhausted: log warning, return None (executor keeps estimated prices)
```

`alpaca_client.py` gains `get_order_by_id(order_id) → Order` — thin wrapper around `self._trading.get_order_by_id(order_id)`.

In `trading/executor.py`, after `place_bracket_order()`:

```python
corrected = risk_manager.correct_bracket_after_fill(
    alpaca._trading, order["id"], sl_pct, tp_pct
)
if corrected:
    position_data["stop_loss_price"]  = corrected["stop_loss_price"]
    position_data["take_profit_price"] = corrected["take_profit_price"]
    position_data["fill_price"]        = corrected["fill_price"]
```

---

## Token Budget Impact

| Change | Token impact |
|---|---|
| Gap guard (gate 12) | Saves tokens — blocks entries before Claude call |
| `needs_exit_evaluation` heuristic | Zero tokens — pure Python |
| Exit Claude calls | Uses pre-reserved 30% exit pool; compact prompt ~300 tokens |
| Fill correction | Zero tokens — Alpaca API only |
| **Net** | Neutral to positive (better-targeted calls, no new budget consumed) |

---

## Config Additions (`config.py`)

```python
# ── Risk Manager ──────────────────────────────────────────────────────────────
ENTRY_BUDGET_RATIO          = 0.70   # fraction of daily ceiling for new entry calls
EXIT_BUDGET_RATIO           = 0.30   # fraction reserved for thesis-break exit calls
EXIT_EVAL_COMPOSITE_DROP    = 10     # composite must drop >= this from entry score to trigger eval
EXIT_EVAL_SENTIMENT_FLOOR   = -30    # sentiment below this triggers exit eval
GAP_SKIP_PCT                = 3.0    # skip entry if price gapped > this % above prev close
FILL_SLIPPAGE_THRESHOLD     = 0.005  # correct bracket if fill deviates > 0.5% from iv price
FILL_POLL_ATTEMPTS          = 5      # max Alpaca polls for fill price after order placement
FILL_POLL_INTERVAL_S        = 0.5    # seconds between fill poll attempts
```

---

## Error Handling

| Failure | Behavior |
|---|---|
| Fill poll timeout (not filled in 2.5s) | Log warning; keep estimated SL/TP; position tracked normally |
| `replace_order_by_id` fails on bracket leg | Log error; original bracket legs remain active |
| Escalation Telegram alert fails | Log error; escalation still completes |
| `needs_exit_evaluation` raises | Catch per-symbol; log; skip that symbol — cycle continues |
| `risk_manager` JSON corrupt | Reset to fresh budget for today (same as current `load_budget`) |
| `prev_close` unavailable (< 2 daily bars) | `prev_close = 0.0`; gap guard skipped (guard only fires when `prev_close > 0`) |

---

## Testing

All new tests in `tests/test_trading.py`, decorated `@pytest.mark.unit`. No network calls.

### `TestRiskManagerBudget`
- Entry pool blocks when 70% of ceiling spent
- Entry pool allows when under 70%
- Exit pool escalates when 30% spent with open positions; Telegram called once
- Escalation is idempotent (second call does not re-alert)
- `record_usage(kind="entry")` routes to `entry_spent_usd`
- `record_usage(kind="exit")` routes to `exit_spent_usd`
- `budget_exhausted` only True when both pools exhausted post-escalation

### `TestRiskManagerGapGuard`
- `is_gap_skip` returns True when gap > `GAP_SKIP_PCT`
- Returns False when gap < `GAP_SKIP_PCT`
- Returns False when `prev_close = 0.0` (missing data — do not block)

### `TestFillCorrection`
- Corrects SL/TP when fill price differs > 0.5% from entry price
- Skips correction when fill is within threshold
- Returns None on poll timeout; executor continues with estimated prices
- New SL/TP anchored to actual fill price, not `iv.current_price`

### `TestThesisBreakHeuristic`
- Flags position when composite dropped ≥ `EXIT_EVAL_COMPOSITE_DROP`
- Flags position when sentiment < `EXIT_EVAL_SENTIMENT_FLOOR`
- Flags position when PnL within 1% of stop-loss
- Returns False for healthy position (all conditions clear)

### Updated `tests/test_market.py`
- All `import api_budget` → `from trading import risk_manager`
- All `api_budget.*` calls → `risk_manager.*`
- Existing test assertions unchanged
