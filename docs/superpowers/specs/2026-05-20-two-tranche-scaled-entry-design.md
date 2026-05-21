# Two-Tranche Scaled Entry — Design Spec

**Date:** 2026-05-20  
**Status:** Approved  
**Scope:** Intraday + swing positions; paper trading only (live-trading extensions noted inline)

---

## Problem

The bot enters full position size at signal and uses a fixed 4% SL. When a good-thesis stock dips 4–7% intraday (technical pullback, not breakdown), the bot is already stopped out and cannot re-enter (daily-closed block). This leaves the highest-conviction recovery trades unreachable.

## Solution

Split every entry into two tranches:
- **Tranche 1 (T1):** 60% of computed position size — placed at signal, same as today
- **Tranche 2 (T2):** 40% of a fresh position size computed at T2 price — triggered automatically by the position monitor when a controlled dip occurs, all gates pass, and thesis is intact

The blended cost basis from T1+T2 lowers the break-even point and makes swing promotion more viable on strong setups.

---

## Config Constants

```python
# ── Tranche-2 scaled entry ────────────────────────────────────────────────────
TRANCHE1_SIZE_PCT        = 0.60   # T1 = 60% of computed position size
TRANCHE2_SIZE_PCT        = 0.40   # T2 = 40% of fresh size computed at T2 price/ATR
TRANCHE2_DROP_MIN_PCT    = 4.0    # T2 triggers when price ≥ 4% below T1 entry
TRANCHE2_DROP_MAX_PCT    = 7.0    # T2 blocked if drop > 7% (breakdown, not dip)
TRANCHE2_RSI_MAX         = 45     # RSI must show oversold at trigger time
TRANCHE2_COMPOSITE_MIN   = 40     # Composite score must still be healthy
TRANCHE2_SL_PCT          = 5.5    # Blended SL: 5.5% below T2 fill price
TRANCHE2_CUTOFF_HOUR     = 14     # No T2 triggers after 2:00 PM ET
TRANCHE2_MIN_HOLD_MIN    = 60     # T1 must be held ≥ 60 min before T2 eligible
TRANCHE2_MIN_SENTIMENT   = 20     # Symbol sentiment score floor for T2
```

---

## Architecture

### Component Map

| Component | File | Role |
|---|---|---|
| T2 gate function | `signals/tranche2_gates.py` (new) | Pure gate logic — no I/O, no Alpaca |
| T1 sizing | `trading/executor.py` | Apply `TRANCHE1_SIZE_PCT` multiplier at entry |
| T2 execution | `trading/executor.py` | `execute_tranche2_addon()` — market order + position update |
| T2 trigger loop | `trading/monitor.py` | `_check_tranche2_triggers()` after SL/TP loop |
| Backtest support | `backtest/simulator.py` | T2 check in per-bar loop using `bar["low"]` |
| Config | `config.py` | All constants under `# ── Tranche-2 ──` header |
| Tests | `tests/test_tranche2.py` (new) | All T2 gate paths + executor + simulator |

### Data Flow

```
run_position_monitor() [every 60s]
  ├── Layer 1: bracket reconciliation (existing, unchanged)
  ├── Layer 2: software SL/TP enforcement (existing, unchanged)
  └── _check_tranche2_triggers(open_positions, alpaca, now) [NEW]
        Load once before loop:
          research   = load_json(RESEARCH_REPORT_FILE, {})
          sector_today = get_intraday_context(now).sector_today or {}
          major_event_today = _is_major_event_today()  ← reads ECONOMIC_CALENDAR_FILE
          regime     = load_macro_regime()["regime"]

        for each open position where tranche_2_triggered == False:
          iv = _try_compute_iv(symbol, alpaca)          ← already used in monitor
          sentiment = research.get(symbol, {}).get("sentiment_score", 0)
          sector_etf = sectors.get_sector_etf(symbol)
          sector_chg = sector_today.get(sector_etf, 0.0)

          run check_tranche2_gates(iv, pos, now, regime, sentiment, sector_chg, major_event_today)
          if passed:
            write tranche_2_triggered=True under _POSITIONS_LOCK  ← idempotency sentinel
            cancel original bracket order (alpaca.cancel_order(order_id))
            execute_tranche2_addon() → place_market_order() for T2 qty
            update position record: blended entry, blended SL, combined qty
            append TRANCHE2_ADD journal entry
```

---

## T1 Sizing Change

`execute_buy()` applies `TRANCHE1_SIZE_PCT = 0.60` to the computed qty before placing the bracket order:

```python
qty = compute_position_size_for_mode(...)  # full size
qty = apply_regime_multiplier(qty, pos_mult)
qty = apply_sentiment_multiplier(qty, sentiment)
qty = max(1, int(qty * config.TRANCHE1_SIZE_PCT))  # ← 60% for T1
```

The `tranche_1_qty` and `tranche_1_entry` fields are written to `position_data` at this point for use by the T2 blended calculation.

---

## Position Record Schema

New fields added at T1 entry:

```python
position_data = {
    # ... all existing fields unchanged ...
    "tranche_2_triggered": False,     # sentinel — set True before T2 order placed
    "tranche_1_qty":       qty,       # T1 shares (for blended calc and learning)
    "tranche_1_entry":     entry_price,  # T1 fill price
}
```

Fields updated atomically under `_POSITIONS_LOCK` when T2 fires:

```python
{
    "tranche_2_triggered": True,
    "tranche_2_qty":       t2_qty,
    "tranche_2_entry":     t2_fill_price,
    "qty":                 t1_qty + t2_qty,           # combined
    "entry_price":         blended_entry,             # weighted avg cost basis
    "stop_loss_price":     t2_fill * (1 - TRANCHE2_SL_PCT/100),  # absolute
    "stop_loss_pct":       blended_sl_pct,            # pct from blended entry
    "order_id":            None,                      # bracket cancelled
}
```

**Why both layers of monitor.py work correctly after update:**
- Layer 1 reads `stop_loss_pct` and `entry_price` → recomputes absolute SL → matches `stop_loss_price`
- Layer 2 reads `stop_loss_pct` and computes `pnl_pct` from `entry_price` → both use blended basis

---

## T2 Gate Function (`signals/tranche2_gates.py`)

```python
@dataclass(frozen=True)
class T2GateResult:
    passed: bool
    reason: str = ""

def check_tranche2_gates(
    symbol: str,
    iv: IndicatorValues,
    pos: dict,
    now: datetime,
    regime: str,
    sentiment_score: float,
    sector_etf_chg: float,
    major_event_today: bool,
) -> T2GateResult:
```

Gates run in fail-fast order:

| # | Gate | Threshold | Blocks when |
|---|---|---|---|
| 1 | Already triggered | `pos["tranche_2_triggered"]` | True |
| 2 | Time cutoff | `now.hour >= TRANCHE2_CUTOFF_HOUR` | After 2 PM ET |
| 3 | Regime | `regime in ("CRISIS", "CHOPPY")` | Adverse regime |
| 4 | Major event today | `major_event_today` | FOMC/CPI/NFP day |
| 5 | Min hold time | `elapsed < TRANCHE2_MIN_HOLD_MIN` | T1 held < 60 min |
| 6 | Price drop window | `drop_pct < DROP_MIN or drop_pct > DROP_MAX` | Outside 4–7% range |
| 7 | RSI oversold | `iv.rsi > TRANCHE2_RSI_MAX` | RSI > 45 |
| 8 | Composite health | `iv.composite_score < TRANCHE2_COMPOSITE_MIN` | Score < 40 |
| 9 | Earnings imminent | `is_earnings_imminent(symbol, days=1)` | Earnings tomorrow |
| 10 | Sentiment floor | `sentiment_score < TRANCHE2_MIN_SENTIMENT` | Score < 20 |
| 11 | Sector health | `sector_etf_chg < -1.0` | Sector down > 1% |

All gates must pass. Returns `T2GateResult(True, "")` on success.

**Gate rationale:**
- Gates 1–5: structural/timing constraints
- Gates 6–8: signal quality (the dip must look like capitulation, not cascade)
- Gates 9–11: thesis integrity (news clean, sentiment positive, sector not leading the drop)

---

## `execute_tranche2_addon()`

```python
def execute_tranche2_addon(
    *,
    symbol: str,
    pos: dict,
    regime: str,
    research: dict,
    alpaca: AlpacaClient,
    portfolio_value: float,
    now: datetime,
    suppress_notify: bool = False,
) -> dict | None:
```

Steps:
1. Write `tranche_2_triggered=True` under `_POSITIONS_LOCK` (idempotency — prevents double-execution if monitor fires twice)
2. Cancel original bracket: `alpaca.cancel_order(pos["order_id"])` — wrapped in try/except, log warning on failure but continue
3. Compute T2 qty: `compute_position_size_for_mode(portfolio_value, current_price, current_atr, mode) × TRANCHE2_SIZE_PCT`
4. Place market order: `alpaca.place_market_order(symbol, t2_qty, "buy")`
5. Compute blended values:
   - `blended_entry = (t1_qty × t1_entry + t2_qty × t2_fill) / (t1_qty + t2_qty)`
   - `blended_sl_price = t2_fill × (1 - TRANCHE2_SL_PCT / 100)`
   - `blended_sl_pct = (blended_entry - blended_sl_price) / blended_entry × 100`
6. Update position under `_POSITIONS_LOCK` (atomic write of all blended fields)
7. Append `TRANCHE2_ADD` journal entry with full blended position state
8. Telegram notify (unless `suppress_notify`)
9. Return updated position dict

**Bracket cancellation failure handling:** If `cancel_order()` raises, log a warning but do NOT abort T2. The original bracket SL may fire at the wrong level, but the software SL is now set at the blended level. The monitor's Layer 1 will reconcile any bracket fill on the next 60s cycle.

---

## Backtest Simulator Changes (`backtest/simulator.py`)

In `simulate_day()`'s per-bar loop, after the existing SL/TP check:

```python
# T2 check — use bar["low"] as worst intrabar price proxy
for sym, pos in list(state.positions.items()):
    if pos.get("tranche_2_triggered"):
        continue
    t1_entry = pos.get("tranche_1_entry", pos["entry"])
    drop_pct = (t1_entry - bar["low"]) / t1_entry * 100
    if config.TRANCHE2_DROP_MIN_PCT <= drop_pct <= config.TRANCHE2_DROP_MAX_PCT:
        if _sim_tranche2_gates_pass(pos, iv, regime, now):
            _sim_apply_tranche2(pos, bar["low"], state)
```

`_sim_tranche2_gates_pass()` checks a simplified subset of gates available at bar time: hold time, regime, cutoff hour, RSI, composite. Does not check earnings/sentiment (not available in simulator per-bar context).

`_sim_apply_tranche2()` updates `pos["entry"]` (blended), `pos["sl"]` (5.5% below bar["low"]), `pos["qty"]` (combined), `pos["tranche_2_triggered"] = True` in-place. Simulation continues using blended values for all subsequent bars.

---

## Swing Promotion Compatibility

No changes needed to `evaluate_eod_holds()` or `qualifies_for_eod_hold()`.

After T2:
- `entry_price` = blended cost basis → PTJ rule (`pnl_pct ≥ SWING_MIN_PNL_PCT`) measured from blended entry
- `stop_loss_pct` = recalculated from blended entry → swing manager uses it correctly
- `entry_sector_sentiment` = already stored at T1, carried forward unchanged

**Natural filter:** At T2 trigger time, position is −3.5% to −4% from blended entry. For PTJ to pass (`pnl_pct ≥ −2.0%`), price must recover ~1.5–2% before 3:50 PM. Only genuine recovery setups qualify for swing — late-day T2 positions that don't recover force-close at 3:55 PM.

---

## Risk Bounds

| Scenario | Dollar loss | Notes |
|---|---|---|
| T1 only, SL hit | `t1_qty × 4%` of entry | Same as today |
| T1 + T2, blended SL hit | `(t1+t2) qty × ~8.5%` of t1 entry | ~3× normal — intentional, bounded |
| T2 fires at 1:50 PM, no recovery | Force-close at 3:55 PM at small loss | Time gate prevents; PTJ filters swing |
| Bracket cancellation fails, bracket SL fires at T1 level | Position closed at T1 SL level | Safe fallback — bounded loss |

---

## Testing Plan

`tests/test_tranche2.py` (new file, `@pytest.mark.unit` throughout):

- `check_tranche2_gates()` — one test per gate path (pass + fail per gate)
- `execute_tranche2_addon()` — mock AlpacaClient, verify: blended entry math, position fields updated, journal entry written
- `_check_tranche2_triggers()` — mock IV fetch and gate, verify: trigger fires, sentinel prevents double-execution
- `_sim_apply_tranche2()` — verify blended SL/entry in simulator position dict
- Integration: T1 sizing at 60% verified via `execute_buy()` test

---

## Live Trading Extension (future, not in scope)

After T2 fills, place a server-side `stop_limit` order at the blended SL price for the full combined qty. This gives Alpaca-side protection if the monitor goes down. Not implemented in this phase — the software SL is sufficient for paper trading.

---

## Files Changed

| File | Type | Change |
|---|---|---|
| `config.py` | Modify | 10 new constants under `# ── Tranche-2 ──` |
| `signals/tranche2_gates.py` | New | `T2GateResult`, `check_tranche2_gates()` |
| `trading/executor.py` | Modify | T1 sizing × 0.60; `execute_tranche2_addon()` |
| `trading/monitor.py` | Modify | `_check_tranche2_triggers()` after SL/TP loop |
| `backtest/simulator.py` | Modify | T2 check in per-bar loop; `_sim_apply_tranche2()` |
| `tests/test_tranche2.py` | New | Full T2 test suite |
