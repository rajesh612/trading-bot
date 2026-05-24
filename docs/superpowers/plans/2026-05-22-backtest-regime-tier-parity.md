# Backtest Regime×Tier×Mode Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring `backtest/simulator.py` to feature parity with live trading's regime×tier×mode system across 6 identified gaps.

**Architecture:** All 6 gaps live exclusively in `backtest/simulator.py`. Each task closes one gap by wiring an existing pure utility into the simulator — no new modules, no logic duplication. Tests go in a new `tests/test_backtest_simulator.py` (pure unit tests, no network). The fix order is: quality first (Gap 1), then gates (Gap 2), then TP decay (Gap 3), then tiered swing exits (Gap 4), then CHOPPY unlock (Gap 5).

**Tech Stack:** Python 3.11, pytest, `utils/ticker_quality.py`, `utils/regime_params.py`, `signals/tp_scaler.py`, `signals/entry_gates.py`, `trading/tiered_tp.py`

---

## Gap Summary

| # | Gap | Live module | Backtest impact |
|---|-----|-------------|-----------------|
| 1 | No `TickerQuality` scoring — all symbols treated as MEDIUM tier | `utils/ticker_quality.py` | Wrong composite floor per tier; CONVICTION allowed too-high bar, LOW let in too easily |
| 2 | No safety gates — stochastic hard veto, parabolic, gap-up skip missing | `signals/entry_gates.py` | Backtest admits entries live would reject → inflated win count |
| 3 | No time-decay TP — full ATR-TP used for afternoon entries | `signals/tp_scaler.py` | Afternoon TP too wide → backtest overstates P&L vs live |
| 4 | No tiered swing TP — binary TP instead of 40/30/30 + trailing | `trading/tiered_tp.py` | Swing P&L numbers not comparable to live |
| 5 | CHOPPY+CONVICTION swing unlock ignored | `signals/entry_gates.py` | All CHOPPY swings blocked in backtest; live allows CONVICTION at 1% profit-lock |
| 6 | `quality_tier` not stored on positions (blocks tier-aware gap check) | — | Subsumed by Task 1 (store tier on position dict) |

---

## File Changes

| File | Action | Responsibility |
|------|--------|----------------|
| `backtest/simulator.py` | Modify | All 6 gaps — quality computation, gate checks, TP scaler, tiered swing, CHOPPY unlock |
| `tests/test_backtest_simulator.py` | Create | Unit tests for each gap fix (no network, no file I/O) |

---

## Task 1: TickerQuality tier computation (Gap 1 + Gap 6)

**Files:**
- Modify: `backtest/simulator.py:1-20` (imports), `simulate_day` body
- Test: `tests/test_backtest_simulator.py` (new)

### What changes

1. Add a minimal `_BacktestSnap` dataclass used as a stub for `compute_ticker_quality`.  
   It only needs `regime`, `research={}`, `intraday_news={}` — the backtest has no live news so sentiment scores default to 0, and quality will be driven mostly by composite strength.
2. At the start of each day's bar loop, compute `TickerQuality` for every watch-list symbol using their first available `iv` from `precomp`. Store results in `quality_map: dict[str, TickerQuality]`.
3. In `_open_trade`, accept `tier: str = "MEDIUM"` and store `"quality_tier"` on the position dict.
4. In the per-symbol entry signal block, replace the hardcoded `effective_composite_gate` call with `get_filter_params(regime, tier).composite_min` as the composite floor for the per-bar check, then fall back to `effective_composite_gate` for herding/squeeze overrides.
5. Pass `tier` through `pending_signals` dict so `_execute_pending_signals` can forward it to `_open_trade`.

- [ ] **Step 1.1: Write failing tests**

```python
# tests/test_backtest_simulator.py
import pytest
from unittest.mock import MagicMock
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# ── helpers ──────────────────────────────────────────────────────────────────

def _make_iv(composite=55.0, rsi=50.0, vwap_slope=0.1, stoch_d=40.0,
             parabolic=False, relative_volume=1.0, prev_close=0.0,
             current_price=100.0, signal="BUY", atr=2.0):
    iv = MagicMock()
    iv.composite_score = composite
    iv.rsi = rsi
    iv.vwap_slope = vwap_slope
    iv.stoch_d = stoch_d
    iv.parabolic = parabolic
    iv.relative_volume = relative_volume
    iv.prev_close = prev_close
    iv.current_price = current_price
    iv.signal = signal
    iv.atr = atr
    iv.squeeze_setup = False
    iv.bot_herding_score = 0
    iv.stop_hunt_zone = False
    iv.or_position = "outside"
    iv.momentum_exhaustion = False
    return iv


@pytest.mark.unit
class TestBacktestQuality:
    def test_quality_map_built_for_watch_list(self):
        """_build_quality_map returns a TickerQuality for every symbol that has a precomp iv."""
        from backtest.simulator import _build_quality_map, _BacktestSnap
        iv = _make_iv(composite=60.0)
        precomp = {"AAPL": {datetime(2026, 1, 2, 10, 0, tzinfo=ET): iv}}
        snap = _BacktestSnap(regime="NORMAL")
        qmap = _build_quality_map(["AAPL", "MISSING"], precomp, snap)
        assert "AAPL" in qmap
        assert qmap["AAPL"].tier in ("LOW", "MEDIUM", "HIGH", "CONVICTION")

    def test_quality_map_skips_symbol_with_no_precomp(self):
        from backtest.simulator import _build_quality_map, _BacktestSnap
        snap = _BacktestSnap(regime="NORMAL")
        qmap = _build_quality_map(["MISSING"], {}, snap)
        assert "MISSING" not in qmap

    def test_open_trade_stores_quality_tier(self, monkeypatch, tmp_path):
        """Position dict must contain 'quality_tier' after _open_trade."""
        import config
        monkeypatch.setattr(config, "STOP_LOSS_PCT", 4.0)
        from backtest.simulator import SimState, _open_trade
        state = SimState(cash=100_000.0)
        ts_et = datetime(2026, 1, 2, 10, 30, tzinfo=ET)
        _open_trade(state, "AAPL", "2026-01-02 10:30:00", 100.0, 2.0,
                    pos_mult=1.0, composite=55.0, tier="HIGH",
                    regime="NORMAL", entry_time_et=ts_et)
        assert "AAPL" in state.positions
        assert state.positions["AAPL"]["quality_tier"] == "HIGH"
```

- [ ] **Step 1.2: Run tests to confirm they fail**

```bash
.venv/bin/python -m pytest tests/test_backtest_simulator.py -m unit -q
```
Expected: `FAILED` with `ImportError` or `AttributeError` (functions don't exist yet).

- [ ] **Step 1.3: Add `_BacktestSnap` and `_build_quality_map` to simulator.py**

Add imports at top of `backtest/simulator.py` (after existing imports):

```python
from dataclasses import dataclass as _dataclass
from utils.ticker_quality import TickerQuality, compute_ticker_quality
from utils.regime_params import get_filter_params, get_gate_params
from signals.entry_gates import get_effective_profit_lock
```

Add new dataclass just before `Trade`:

```python
@_dataclass(frozen=True)
class _BacktestSnap:
    """Minimal snap stub for compute_ticker_quality — no live research/news in backtest."""
    regime: str
    research: dict = None
    intraday_news: dict = None

    def __post_init__(self):
        # frozen dataclass: use object.__setattr__ to initialise mutable defaults
        object.__setattr__(self, "research", self.research if self.research is not None else {})
        object.__setattr__(self, "intraday_news", self.intraday_news if self.intraday_news is not None else {})


def _build_quality_map(
    watch_list: list[str],
    precomp: dict[str, dict],
    snap: "_BacktestSnap",
) -> dict[str, TickerQuality]:
    """Compute TickerQuality for each watch-list symbol using its earliest precomputed iv."""
    qmap: dict[str, TickerQuality] = {}
    for sym in watch_list:
        sym_bars = precomp.get(sym)
        if not sym_bars:
            continue
        iv = next(iter(sym_bars.values()), None)
        if iv is None:
            continue
        qmap[sym] = compute_ticker_quality(sym, iv, snap, "DAY_TRADE")
    return qmap
```

- [ ] **Step 1.4: Modify `_open_trade` to accept `tier`, `regime`, `entry_time_et`**

Replace the existing `_open_trade` signature and `sl`/`tp` computation:

```python
def _open_trade(
    state: SimState, symbol: str, ts: str, bar_open: float,
    atr: float, pos_mult: float = 1.0, composite: float = 0.0,
    tier: str = "MEDIUM", regime: str = "NORMAL",
    entry_time_et=None,
) -> None:
    portfolio_value = state.cash + sum(p["qty"] * p["entry"] for p in state.positions.values())
    entry = _entry_price(bar_open)
    qty = max(1.0, round(_calc_qty(portfolio_value, entry, atr) * pos_mult))
    cost = entry * qty
    if cost > state.cash:
        qty = max(1.0, round(state.cash / entry))
        cost = entry * qty
    if qty < 1 or cost > state.cash:
        return
    base_tp_pct = compute_atr_tp_pct(atr, entry, state.atr_tp_multiplier)
    from signals.tp_scaler import compute_tp_sl as _compute_tp_sl
    tp_pct, sl_pct = _compute_tp_sl(
        base_tp_pct, STOP_LOSS_PCT, entry_time_et or datetime.now(ET), tier, regime
    )
    sl = round(entry * (1 - sl_pct / 100), 4)
    tp = round(entry * (1 + tp_pct / 100), 4)
    state.cash -= cost
    state.positions[symbol] = {
        "qty": qty, "entry": entry, "sl": sl, "tp": tp,
        "entry_ts": ts, "entry_time": ts, "composite": composite,
        "quality_tier": tier,
    }
    logger.debug(f"    OPEN  {symbol:5s} x{qty:.0f} @ ${entry:.2f}  SL=${sl:.2f} TP=${tp:.2f}  [{tier}]")
```

- [ ] **Step 1.5: Update `pending_signals` to carry tier, and thread quality_map through simulate_day**

In `simulate_day`, after computing `precomp` dict:

```python
    # Build quality map for the day (uses earliest iv per symbol)
    _snap = _BacktestSnap(regime=regime)
    quality_map: dict[str, TickerQuality] = _build_quality_map(watch_list, precomp, _snap)
```

In the entry signal block (around line 390), change:
```python
        for sym in watch_list:
            if sym in state.positions or sym in state.daily_closed: continue
            if sym in state.sl_blacklist: continue
            iv = precomp.get(sym, {}).get(ts)
            if iv is None: continue
            quality = quality_map.get(sym)
            tier = quality.tier if quality else "MEDIUM"
            fp = get_filter_params(regime, tier)
            composite_floor = max(
                fp.composite_min,
                effective_composite_gate(iv, regime, params=p),
            )
            if iv.composite_score > composite_floor and iv.signal == "BUY" and iv.rsi <= rsi_max and iv.vwap_slope >= VWAP_SLOPE_MIN:
                if ts_et.hour >= config.ENTRY_LATE_HOUR and iv.composite_score < config.ENTRY_LATE_COMPOSITE_MIN:
                    continue
                pending_signals[sym] = {"score": iv.composite_score, "atr": iv.atr, "tier": tier}
```

In `_execute_pending_signals`, pass tier through to `_open_trade`:
```python
def _execute_pending_signals(
    state: SimState, pending: dict, day_bars: dict, ts, pos_mult: float,
    regime: str = "NORMAL", ts_et=None,
) -> None:
    n_open = len(state.positions)
    for sym, sig in list(pending.items()):
        if n_open >= MAX_POSITIONS:
            break
        if sym in state.positions or sym in state.daily_closed:
            continue
        df_sym = day_bars.get(sym)
        if df_sym is None or ts not in df_sym.index:
            continue
        _open_trade(state, sym, str(ts), float(df_sym.loc[ts, "open"]),
                    sig["atr"], pos_mult, sig["score"],
                    tier=sig.get("tier", "MEDIUM"), regime=regime,
                    entry_time_et=ts_et)
        n_open += 1
    pending.clear()
```

Update the call site in `simulate_day` to pass `regime` and `ts_et`:
```python
        if pending_signals and i > 0 and not halted and in_entry_window:
            _execute_pending_signals(state, pending_signals, day_bars, ts, pos_mult,
                                     regime=regime, ts_et=ts_et)
```

- [ ] **Step 1.6: Run tests**

```bash
.venv/bin/python -m pytest tests/test_backtest_simulator.py -m unit -q
```
Expected: all 3 tests PASS.

- [ ] **Step 1.7: Run full test suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```
Expected: 1026+ passed, 0 failed.

- [ ] **Step 1.8: Commit**

```bash
git add backtest/simulator.py tests/test_backtest_simulator.py
git commit -m "feat(backtest): compute TickerQuality per symbol; store tier on positions"
```

---

## Task 2: Safety gates — stochastic hard veto, parabolic, gap-up skip (Gap 2)

**Files:**
- Modify: `backtest/simulator.py` (entry signal block in `simulate_day`)
- Test: `tests/test_backtest_simulator.py`

### What changes

In the per-bar entry signal check, before adding to `pending_signals`, apply three hard-gate checks that are available from precomputed `IndicatorValues`:
1. **Stochastic hard veto**: `iv.stoch_d > gp.stoch_hard_veto` → skip (uses `get_gate_params(regime, tier)`)
2. **Parabolic hard block**: `iv.parabolic and config.PARABOLIC_ENTRY_BLOCK` → skip
3. **Gap-up skip**: `iv.prev_close > 0 and iv.current_price > iv.prev_close * (1 + config.GAP_SKIP_PCT / 100)` → skip

- [ ] **Step 2.1: Write failing tests**

Add to `tests/test_backtest_simulator.py`:

```python
@pytest.mark.unit
class TestBacktestSafetyGates:
    """Simulate a single bar day and verify gate rejections."""

    def _run_one_signal(self, iv, regime="NORMAL", tier="MEDIUM"):
        """Helper: returns pending_signals after processing one entry bar."""
        from backtest.simulator import _BacktestSnap, _build_quality_map
        # Patch quality_map to inject known tier
        from utils.ticker_quality import TickerQuality
        quality = TickerQuality(tier=tier, score=50, unlocks=frozenset(),
                                quality_gaps=(), component_scores={})
        quality_map = {"AAPL": quality}
        return quality_map, iv

    def test_stoch_hard_veto_blocks_entry(self, monkeypatch):
        """iv.stoch_d above stoch_hard_veto must not produce a pending signal."""
        import config
        from utils.regime_params import get_gate_params
        gp = get_gate_params("NORMAL", "MEDIUM")
        iv = _make_iv(composite=60.0, stoch_d=float(gp.stoch_hard_veto) + 1)
        # We test _sim_entry_signal_passes directly
        from backtest.simulator import _sim_entry_signal_passes
        quality_map, _ = self._run_one_signal(iv)
        result = _sim_entry_signal_passes(iv, "NORMAL", quality_map.get("AAPL"), params={},
                                          rsi_max=74.0)
        assert result is False

    def test_parabolic_blocks_entry(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "PARABOLIC_ENTRY_BLOCK", True)
        iv = _make_iv(composite=60.0, parabolic=True)
        from backtest.simulator import _sim_entry_signal_passes
        from utils.ticker_quality import TickerQuality
        quality = TickerQuality(tier="MEDIUM", score=50, unlocks=frozenset(),
                                quality_gaps=(), component_scores={})
        result = _sim_entry_signal_passes(iv, "NORMAL", quality, params={}, rsi_max=74.0)
        assert result is False

    def test_gap_up_skip_blocks_entry(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "GAP_SKIP_PCT", 3.0)
        iv = _make_iv(composite=60.0, prev_close=100.0, current_price=104.0)
        from backtest.simulator import _sim_entry_signal_passes
        from utils.ticker_quality import TickerQuality
        quality = TickerQuality(tier="MEDIUM", score=50, unlocks=frozenset(),
                                quality_gaps=(), component_scores={})
        result = _sim_entry_signal_passes(iv, "NORMAL", quality, params={}, rsi_max=74.0)
        assert result is False

    def test_clean_signal_passes_all_gates(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "PARABOLIC_ENTRY_BLOCK", True)
        monkeypatch.setattr(config, "GAP_SKIP_PCT", 3.0)
        iv = _make_iv(composite=60.0, stoch_d=40.0, parabolic=False,
                      prev_close=100.0, current_price=100.5)
        from backtest.simulator import _sim_entry_signal_passes
        from utils.ticker_quality import TickerQuality
        quality = TickerQuality(tier="MEDIUM", score=50, unlocks=frozenset(),
                                quality_gaps=(), component_scores={})
        result = _sim_entry_signal_passes(iv, "NORMAL", quality, params={}, rsi_max=74.0)
        assert result is True
```

- [ ] **Step 2.2: Run tests to confirm they fail**

```bash
.venv/bin/python -m pytest tests/test_backtest_simulator.py::TestBacktestSafetyGates -m unit -q
```
Expected: `FAILED` — `_sim_entry_signal_passes` does not exist.

- [ ] **Step 2.3: Add `_sim_entry_signal_passes` helper and update entry loop**

Add new pure function to `backtest/simulator.py` (before `simulate_day`):

```python
def _sim_entry_signal_passes(
    iv,
    regime: str,
    quality: "TickerQuality | None",
    params: dict,
    rsi_max: float,
) -> bool:
    """Return True iff the bar's signals pass all backtest entry gates.

    Mirrors the hard-gate checks in check_all_gates() that are computable
    from precomputed IndicatorValues — no calendar/journal data required.
    """
    tier = quality.tier if quality else "MEDIUM"
    fp = get_filter_params(regime, tier)
    gp = get_gate_params(regime, tier)

    # Composite floor (tier-adjusted, squeeze/herding override via effective_composite_gate)
    composite_floor = max(fp.composite_min, effective_composite_gate(iv, regime, params=params))
    if iv.composite_score <= composite_floor:
        return False
    if iv.signal != "BUY":
        return False
    if iv.rsi > rsi_max:
        return False
    if iv.vwap_slope < VWAP_SLOPE_MIN:
        return False
    # Stochastic hard veto (Gate 11 in check_all_gates)
    if iv.stoch_d > gp.stoch_hard_veto:
        return False
    # Parabolic hard block (Gate 8)
    if config.PARABOLIC_ENTRY_BLOCK and iv.parabolic:
        return False
    # Gap-up skip (Gate 12)
    if iv.prev_close > 0 and iv.current_price > iv.prev_close * (1 + config.GAP_SKIP_PCT / 100):
        return False
    return True
```

Then update the per-bar entry loop in `simulate_day` to use this helper:

```python
        for sym in watch_list:
            if sym in state.positions or sym in state.daily_closed: continue
            if sym in state.sl_blacklist: continue
            iv = precomp.get(sym, {}).get(ts)
            if iv is None: continue
            quality = quality_map.get(sym)
            tier = quality.tier if quality else "MEDIUM"
            if not _sim_entry_signal_passes(iv, regime, quality, p, rsi_max):
                continue
            if ts_et.hour >= config.ENTRY_LATE_HOUR and iv.composite_score < config.ENTRY_LATE_COMPOSITE_MIN:
                continue
            pending_signals[sym] = {"score": iv.composite_score, "atr": iv.atr, "tier": tier}
```

- [ ] **Step 2.4: Run tests**

```bash
.venv/bin/python -m pytest tests/test_backtest_simulator.py -m unit -q
```
Expected: all 7 tests PASS.

- [ ] **Step 2.5: Run full test suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```
Expected: 1026+ passed, 0 failed.

- [ ] **Step 2.6: Commit**

```bash
git add backtest/simulator.py tests/test_backtest_simulator.py
git commit -m "feat(backtest): add stochastic hard veto, parabolic, gap-up safety gates"
```

---

## Task 3: Time-decay TP via `compute_tp_sl` (Gap 3)

**Files:**
- Modify: `backtest/simulator.py` (`_open_trade` — already modified in Task 1)
- Test: `tests/test_backtest_simulator.py`

> Note: `_open_trade` was already modified in Task 1 to call `compute_tp_sl`. This task adds the unit tests to verify the time-decay behaviour is wired correctly.

- [ ] **Step 3.1: Write failing tests**

Add to `tests/test_backtest_simulator.py`:

```python
@pytest.mark.unit
class TestBacktestTPDecay:
    def _open(self, hour: int, minute: int = 0, tier="MEDIUM", regime="NORMAL"):
        import config
        from backtest.simulator import SimState, _open_trade
        state = SimState(cash=100_000.0)
        ts_et = datetime(2026, 1, 2, hour, minute, tzinfo=ET)
        _open_trade(state, "AAPL", str(ts_et), 100.0, 2.0,
                    pos_mult=1.0, composite=55.0, tier=tier,
                    regime=regime, entry_time_et=ts_et)
        return state.positions.get("AAPL")

    def test_morning_entry_full_tp(self):
        """Entry before noon: time multiplier 1.0 — TP should be wider than afternoon."""
        pos_morning = self._open(10, 30)
        pos_afternoon = self._open(14, 0)
        assert pos_morning is not None and pos_afternoon is not None
        assert pos_morning["tp"] > pos_afternoon["tp"], (
            f"Morning TP {pos_morning['tp']} should be > afternoon TP {pos_afternoon['tp']}"
        )

    def test_afternoon_entry_tighter_tp(self):
        """Entry at 14:00 ET uses 0.65× multiplier — TP must be < morning entry."""
        pos_early = self._open(10, 0)
        pos_late = self._open(14, 0)
        assert pos_late["tp"] < pos_early["tp"]

    def test_conviction_tier_widens_tp(self):
        """CONVICTION tier applies 1.20× TP multiplier in safe regime."""
        pos_med = self._open(10, 30, tier="MEDIUM", regime="NORMAL")
        pos_conv = self._open(10, 30, tier="CONVICTION", regime="NORMAL")
        assert pos_conv["tp"] > pos_med["tp"], (
            f"CONVICTION TP {pos_conv['tp']} should exceed MEDIUM TP {pos_med['tp']}"
        )

    def test_conviction_tightens_sl(self):
        """CONVICTION tier applies 0.90× SL multiplier in safe regime."""
        pos_med = self._open(10, 30, tier="MEDIUM", regime="NORMAL")
        pos_conv = self._open(10, 30, tier="CONVICTION", regime="NORMAL")
        assert pos_conv["sl"] > pos_med["sl"], (
            "CONVICTION SL should be tighter (higher price = further from entry)"
        )
```

- [ ] **Step 3.2: Run tests**

```bash
.venv/bin/python -m pytest tests/test_backtest_simulator.py::TestBacktestTPDecay -m unit -q
```
Expected: all 4 PASS (already wired in Task 1).

If any fail, inspect `_open_trade` to confirm `compute_tp_sl` is called with `entry_time_et`. Fix any discrepancy.

- [ ] **Step 3.3: Run full test suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```
Expected: 1026+ passed, 0 failed.

- [ ] **Step 3.4: Commit**

```bash
git add tests/test_backtest_simulator.py
git commit -m "test(backtest): verify time-decay TP and CONVICTION tier multipliers"
```

---

## Task 4: Tiered swing TP (40/30/30 + trailing stop) (Gap 4)

**Files:**
- Modify: `backtest/simulator.py` — `_close_trade`, `_evaluate_swing_promotion`, per-bar position monitoring loop
- Test: `tests/test_backtest_simulator.py`

### What changes

**Partial close support**: `_close_trade` currently pops the entire position. Add optional `qty` param. When `qty < pos["qty"]`, record a partial trade and reduce position qty without removing from `state.positions` or `state.daily_closed`.

**Swing promotion**: When a position is promoted to swing via `_evaluate_swing_promotion`, compute 40/30/30 split and initialize `tp1_qty`, `tp2_qty`, `trailing_qty`, `tp1_hit`, `tp2_hit`, `trailing_stop_active`, `trailing_high_water`, `entry_price` (used by `evaluate_tiered_tp_action`), `stop_loss_pct`, `tp1_pct`, `tp2_pct`.

**Per-bar monitoring for swings**: Replace the current simple `bar["high"] >= pos["tp"]` check for swing positions with a call to `evaluate_tiered_tp_action(pos, bar["close"])`. Handle the 5 action types: `NONE`, `CLOSE_TP1` (partial), `CLOSE_TP2` (partial + activate trailing), `CLOSE_TRAILING`, `CLOSE_HARD_STOP`.

- [ ] **Step 4.1: Write failing tests**

Add to `tests/test_backtest_simulator.py`:

```python
@pytest.mark.unit
class TestBacktestTieredSwingTP:
    def _make_swing_pos(self, qty=10, entry=100.0, sl=97.0, tp1_pct=5.0, tp2_pct=10.0, regime="NORMAL"):
        from trading.tiered_tp import _compute_tier_quantities
        tp1_qty, tp2_qty, trailing_qty = _compute_tier_quantities(qty)
        return {
            "qty": qty, "entry": entry, "entry_price": entry,
            "sl": sl, "tp": round(entry * 1.10, 4),
            "entry_ts": "2026-01-02 10:30:00", "entry_time": "2026-01-02 10:30:00",
            "composite": 60.0, "quality_tier": "MEDIUM", "is_swing": True,
            "swing_days_remaining": 3,
            "tp1_qty": tp1_qty, "tp2_qty": tp2_qty, "trailing_qty": trailing_qty,
            "tp1_hit": False, "tp2_hit": False,
            "trailing_stop_active": False, "trailing_high_water": entry,
            "stop_loss_pct": 3.0, "tp1_pct": tp1_pct, "tp2_pct": tp2_pct,
        }

    def test_swing_promotion_initializes_tiered_fields(self):
        """After swing promotion, position must have tp1_qty/tp2_qty/trailing_qty."""
        from backtest.simulator import SimState
        state = SimState(cash=100_000.0)
        state.positions["AAPL"] = {
            "qty": 10, "entry": 100.0, "sl": 96.0, "tp": 105.0,
            "entry_ts": "2026-01-02 10:30:00", "entry_time": "2026-01-02 10:30:00",
            "composite": 55.0, "quality_tier": "MEDIUM",
        }
        # After promotion these fields must be set
        from backtest.simulator import _init_tiered_swing_fields
        _init_tiered_swing_fields(state.positions["AAPL"], "NORMAL")
        pos = state.positions["AAPL"]
        assert "tp1_qty" in pos
        assert "tp2_qty" in pos
        assert "trailing_qty" in pos
        assert pos["tp1_qty"] + pos["tp2_qty"] + pos["trailing_qty"] == 10
        assert pos["tp1_hit"] is False

    def test_partial_close_reduces_qty(self):
        """_close_trade with qty < pos['qty'] must reduce position, not pop it."""
        from backtest.simulator import SimState, _close_trade
        state = SimState(cash=100_000.0)
        state.positions["AAPL"] = self._make_swing_pos(qty=10, entry=100.0)
        _close_trade(state, "AAPL", "ts", 105.0, "SWING_TP1", "2026-01-02", qty=4)
        assert "AAPL" in state.positions          # still open
        assert state.positions["AAPL"]["qty"] == 6
        assert len(state.trades) == 1
        assert state.trades[0].qty == 4
        assert "AAPL" not in state.daily_closed  # not fully closed

    def test_full_close_removes_position(self):
        from backtest.simulator import SimState, _close_trade
        state = SimState(cash=100_000.0)
        state.positions["AAPL"] = self._make_swing_pos(qty=10, entry=100.0)
        _close_trade(state, "AAPL", "ts", 105.0, "SWING_TP2", "2026-01-02")
        assert "AAPL" not in state.positions
        assert "AAPL" in state.daily_closed
```

- [ ] **Step 4.2: Run tests to confirm they fail**

```bash
.venv/bin/python -m pytest tests/test_backtest_simulator.py::TestBacktestTieredSwingTP -m unit -q
```
Expected: FAILED — `_init_tiered_swing_fields` does not exist; `_close_trade` does not accept `qty`.

- [ ] **Step 4.3: Add `_init_tiered_swing_fields` helper**

Add to `backtest/simulator.py` (before `_evaluate_swing_promotion`):

```python
def _init_tiered_swing_fields(pos: dict, regime: str) -> None:
    """Initialise 40/30/30 tiered TP fields on a position being promoted to swing."""
    from trading.tiered_tp import _compute_tier_quantities
    sp = get_swing_params(regime)
    total_qty = int(pos["qty"])
    tp1_qty, tp2_qty, trailing_qty = _compute_tier_quantities(total_qty)
    pos.update({
        "tp1_qty":             tp1_qty,
        "tp2_qty":             tp2_qty,
        "trailing_qty":        trailing_qty,
        "tp1_hit":             False,
        "tp2_hit":             False,
        "trailing_stop_active": False,
        "trailing_high_water": pos["entry"],
        "entry_price":         pos["entry"],   # required by evaluate_tiered_tp_action
        "stop_loss_pct":       sp.overnight_sl_pct,
        "tp1_pct":             sp.tp1_pct,
        "tp2_pct":             sp.tp2_pct,
    })
```

- [ ] **Step 4.4: Modify `_close_trade` to support partial qty**

Replace the existing `_close_trade` function:

```python
def _close_trade(
    state: SimState, symbol: str, exit_ts: str,
    exit_raw: float, reason: str, date_str: str,
    qty: int | None = None,
) -> None:
    pos = state.positions[symbol]
    close_qty = qty if qty is not None else int(pos["qty"])
    ep = _exit_price(exit_raw)
    proceeds = ep * close_qty
    state.cash += proceeds
    pnl = proceeds - pos["entry"] * close_qty
    pnl_pct = (ep / pos["entry"] - 1) * 100
    state.trades.append(Trade(
        symbol=symbol, date=date_str, entry_ts=pos["entry_ts"], exit_ts=exit_ts,
        entry_price=pos["entry"], exit_price=ep, qty=close_qty,
        exit_reason=reason, pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 2),
    ))
    remaining = pos["qty"] - close_qty
    if remaining > 0:
        pos["qty"] = remaining
    else:
        del state.positions[symbol]
        state.daily_closed.add(symbol)
    logger.debug(
        f"    CLOSE {symbol:5s} x{close_qty:.0f} @ ${ep:.2f}  "
        f"PnL=${pnl:+.2f} ({pnl_pct:+.2f}%)  [{reason}]"
    )
```

- [ ] **Step 4.5: Update `_evaluate_swing_promotion` to call `_init_tiered_swing_fields`**

Replace the swing promotion block inside `_evaluate_swing_promotion` that currently sets `pos2["sl"]` and `pos2["tp"]`:

```python
        if profit_lock_bypass or standard_qualify:
            pos2["is_swing"] = True
            pos2["swing_days_remaining"] = SWING_MAX_DAYS
            _init_tiered_swing_fields(pos2, regime)
            n_swings += 1
            tag = "PROFIT-LOCK" if profit_lock_bypass else "STANDARD"
            logger.info(
                f"    SWING PROMOTE {sym2} [{tag}]: P&L={pnl_pct:+.2f}% composite={pos2['composite']:.0f}"
                f" | SL={pos2['sl']:.2f} TP1={pos2.get('tp1_pct',0):.1f}% TP2={pos2.get('tp2_pct',0):.1f}%"
            )
```

Note: `_init_tiered_swing_fields` sets `stop_loss_pct` but not `sl` (the price). Also update `sl` price after calling it:

```python
            _init_tiered_swing_fields(pos2, regime)
            pos2["sl"] = round(pos2["entry"] * (1 - pos2["stop_loss_pct"] / 100), 4)
```

- [ ] **Step 4.6: Replace simple swing bar-check with `evaluate_tiered_tp_action`**

In the per-bar position monitoring loop in `simulate_day`, replace:

```python
            if bar["high"] >= pos["tp"]:
                reason, exit_price = ("SWING_TP" if is_sw else "TP"), pos["tp"]
            elif bar["low"] <= pos["sl"]:
                reason, exit_price = ("SWING_SL" if is_sw else "SL"), pos["sl"]
            elif is_force and not is_sw:
                reason, exit_price = "FORCE_CLOSE", bar["close"]

            if reason:
                _close_trade(state, sym, str(ts), exit_price, reason, date_str)
```

with:

```python
            if is_sw:
                from trading.tiered_tp import evaluate_tiered_tp_action, compute_trailing_stop_price
                # Update high water mark
                if pos.get("trailing_stop_active") and bar["high"] > pos.get("trailing_high_water", 0):
                    pos["trailing_high_water"] = float(bar["high"])
                result = evaluate_tiered_tp_action(pos, float(bar["close"]))
                action = result["action"]
                if action == "CLOSE_TP1":
                    _close_trade(state, sym, str(ts), float(bar["close"]), "SWING_TP1", date_str,
                                 qty=result["qty"])
                    pos["tp1_hit"] = True
                elif action == "CLOSE_TP2":
                    _close_trade(state, sym, str(ts), float(bar["close"]), "SWING_TP2", date_str,
                                 qty=result["qty"])
                    if sym in state.positions:   # may have been fully closed
                        pos["tp2_hit"] = True
                        pos["trailing_stop_active"] = True
                        pos["trailing_high_water"] = float(bar["close"])
                elif action in ("CLOSE_TRAILING", "CLOSE_HARD_STOP"):
                    exit_price = pos["sl"] if action == "CLOSE_HARD_STOP" else float(bar["close"])
                    reason_str = "SWING_SL" if action == "CLOSE_HARD_STOP" else "SWING_TRAILING"
                    _close_trade(state, sym, str(ts), exit_price, reason_str, date_str)
                    if reason_str == "SWING_SL":
                        state.sl_blacklist_pending.add(sym)
                # non-swing SL/TP/FORCE_CLOSE
            else:
                reason = None
                exit_price = None
                if bar["high"] >= pos["tp"]:
                    reason, exit_price = "TP", pos["tp"]
                elif bar["low"] <= pos["sl"]:
                    reason, exit_price = "SL", pos["sl"]
                elif is_force:
                    reason, exit_price = "FORCE_CLOSE", bar["close"]
                if reason:
                    _close_trade(state, sym, str(ts), exit_price, reason, date_str)
                    if reason == "SL":
                        state.sl_blacklist_pending.add(sym)
```

Also remove the old `state.sl_blacklist_pending.add(sym)` call that was below the old `if reason:` block (since we now handle it inside each branch).

- [ ] **Step 4.7: Run tests**

```bash
.venv/bin/python -m pytest tests/test_backtest_simulator.py -m unit -q
```
Expected: all tests PASS.

- [ ] **Step 4.8: Run full test suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```
Expected: 1026+ passed, 0 failed.

- [ ] **Step 4.9: Commit**

```bash
git add backtest/simulator.py tests/test_backtest_simulator.py
git commit -m "feat(backtest): tiered swing TP 40/30/30 + partial close support"
```

---

## Task 5: CHOPPY swing unlock for CONVICTION tier (Gap 5)

**Files:**
- Modify: `backtest/simulator.py` (`_evaluate_swing_promotion` + `simulate_day` — pass `quality_map` through)
- Test: `tests/test_backtest_simulator.py`

### What changes

`_evaluate_swing_promotion` currently checks `sp.eligible` which is `False` for CHOPPY, blocking all CHOPPY swings. Live code uses `get_effective_profit_lock(regime, quality)` to lower the profit-lock threshold for CONVICTION, and allows CHOPPY swing when `"SWING_CHOPPY" in quality.unlocks`.

1. Change `_evaluate_swing_promotion` signature to accept `quality_map: dict`.
2. Look up `quality = quality_map.get(sym2)` per symbol.
3. Use `get_effective_profit_lock(regime, quality)` instead of `sp.profit_lock_pct`.
4. For CHOPPY: allow standard qualify when `quality and "SWING_CHOPPY" in quality.unlocks`.
5. Update the call site in `simulate_day` to pass `quality_map`.

- [ ] **Step 5.1: Write failing tests**

Add to `tests/test_backtest_simulator.py`:

```python
@pytest.mark.unit
class TestBacktestChoppySwingUnlock:
    def _make_conviction_quality(self):
        from utils.ticker_quality import TickerQuality
        return TickerQuality(
            tier="CONVICTION", score=85,
            unlocks=frozenset({"SWING_CHOPPY", "SWING_SENTIMENT"}),
            quality_gaps=(), component_scores={},
        )

    def _make_medium_quality(self):
        from utils.ticker_quality import TickerQuality
        return TickerQuality(
            tier="MEDIUM", score=40, unlocks=frozenset(),
            quality_gaps=(), component_scores={},
        )

    def test_choppy_medium_blocked(self):
        """MEDIUM tier in CHOPPY must NOT be promoted to swing."""
        from backtest.simulator import SimState, _evaluate_swing_promotion
        state = SimState(cash=100_000.0)
        state.positions["AAPL"] = {
            "qty": 5, "entry": 100.0, "sl": 97.0, "tp": 105.0,
            "entry_ts": "ts", "entry_time": "ts", "composite": 60.0,
            "quality_tier": "MEDIUM",
        }
        import pandas as pd
        ts = pd.Timestamp("2026-01-02 15:50:00+00:00")
        day_bars = {"AAPL": pd.DataFrame({"close": [101.5]}, index=[ts])}
        quality_map = {"AAPL": self._make_medium_quality()}
        n = _evaluate_swing_promotion(state, day_bars, ts, "CHOPPY", 0, quality_map)
        assert n == 0
        assert not state.positions["AAPL"].get("is_swing", False)

    def test_choppy_conviction_unlocked(self):
        """CONVICTION tier with SWING_CHOPPY unlock MUST be promoted in CHOPPY."""
        from backtest.simulator import SimState, _evaluate_swing_promotion
        state = SimState(cash=100_000.0)
        state.positions["AAPL"] = {
            "qty": 5, "entry": 100.0, "sl": 97.0, "tp": 105.0,
            "entry_ts": "ts", "entry_time": "ts", "composite": 60.0,
            "quality_tier": "CONVICTION",
        }
        import pandas as pd
        ts = pd.Timestamp("2026-01-02 15:50:00+00:00")
        # Price shows 1.5% gain (above 1.0% SWING_CHOPPY profit-lock threshold)
        day_bars = {"AAPL": pd.DataFrame({"close": [101.5]}, index=[ts])}
        quality_map = {"AAPL": self._make_conviction_quality()}
        n = _evaluate_swing_promotion(state, day_bars, ts, "CHOPPY", 0, quality_map)
        assert n == 1
        assert state.positions["AAPL"].get("is_swing", False)

    def test_effective_profit_lock_used_for_normal_regime(self):
        """In NORMAL regime, profit_lock_pct drives profit-lock bypass."""
        from backtest.simulator import SimState, _evaluate_swing_promotion
        import config
        state = SimState(cash=100_000.0)
        # P&L = 2.6% — above SWING_PROFIT_LOCK_NORMAL (2.5%)
        state.positions["AAPL"] = {
            "qty": 5, "entry": 100.0, "sl": 97.0, "tp": 105.0,
            "entry_ts": "ts", "entry_time": "ts", "composite": 60.0,
            "quality_tier": "MEDIUM",
        }
        import pandas as pd
        ts = pd.Timestamp("2026-01-02 15:50:00+00:00")
        day_bars = {"AAPL": pd.DataFrame({"close": [102.6]}, index=[ts])}
        quality_map = {"AAPL": self._make_medium_quality()}
        n = _evaluate_swing_promotion(state, day_bars, ts, "NORMAL", 0, quality_map)
        assert n == 1
        assert state.positions["AAPL"].get("is_swing", False)
```

- [ ] **Step 5.2: Run tests to confirm they fail**

```bash
.venv/bin/python -m pytest tests/test_backtest_simulator.py::TestBacktestChoppySwingUnlock -m unit -q
```
Expected: FAILED — `_evaluate_swing_promotion` wrong signature.

- [ ] **Step 5.3: Update `_evaluate_swing_promotion` to accept and use `quality_map`**

Replace the current function:

```python
def _evaluate_swing_promotion(
    state: SimState, day_bars: dict, ts, regime: str, n_swings: int,
    quality_map: "dict[str, TickerQuality] | None" = None,
) -> int:
    qmap = quality_map or {}
    for sym2 in list(state.positions.keys()):
        pos2 = state.positions[sym2]
        if pos2.get("is_swing"):
            continue
        df2 = day_bars.get(sym2)
        if df2 is None or df2.empty:
            continue
        avail2 = df2[df2.index <= ts]
        eod_price = float(avail2["close"].iloc[-1]) if not avail2.empty else pos2["entry"]
        pnl_pct = (eod_price - pos2["entry"]) / pos2["entry"] * 100
        sp = get_swing_params(regime)
        quality = qmap.get(sym2)

        effective_lock = get_effective_profit_lock(regime, quality)
        profit_lock_bypass = (
            pnl_pct >= effective_lock
            and regime != "CRISIS"
            and n_swings < SWING_MAX_POSITIONS
        )
        choppy_conviction_ok = (
            regime == "CHOPPY"
            and quality is not None
            and "SWING_CHOPPY" in quality.unlocks
            and n_swings < SWING_MAX_POSITIONS
        )
        standard_qualify = (
            (sp.eligible or choppy_conviction_ok)
            and pnl_pct >= sp.min_pnl_pct
            and pos2.get("composite", 0) >= SWING_COMPOSITE_MIN
            and n_swings < SWING_MAX_POSITIONS
        )
        if profit_lock_bypass or standard_qualify:
            pos2["is_swing"] = True
            pos2["swing_days_remaining"] = SWING_MAX_DAYS
            _init_tiered_swing_fields(pos2, regime)
            pos2["sl"] = round(pos2["entry"] * (1 - pos2["stop_loss_pct"] / 100), 4)
            n_swings += 1
            tag = "PROFIT-LOCK" if profit_lock_bypass else "STANDARD"
            logger.info(
                f"    SWING PROMOTE {sym2} [{tag}]: P&L={pnl_pct:+.2f}% composite={pos2['composite']:.0f}"
                f" | SL={pos2['sl']:.2f} TP1={pos2.get('tp1_pct', 0):.1f}% TP2={pos2.get('tp2_pct', 0):.1f}%"
            )
    return n_swings
```

- [ ] **Step 5.4: Update call site in `simulate_day` to pass `quality_map`**

Find the call to `_evaluate_swing_promotion` in `simulate_day`:

```python
            _evaluate_swing_promotion(state, day_bars, ts, regime, n_swings)
```

Replace with:

```python
            n_swings = _evaluate_swing_promotion(state, day_bars, ts, regime, n_swings, quality_map)
```

(Note: already returns the updated `n_swings` — ensure the assignment is present.)

- [ ] **Step 5.5: Run tests**

```bash
.venv/bin/python -m pytest tests/test_backtest_simulator.py -m unit -q
```
Expected: all tests PASS.

- [ ] **Step 5.6: Run full test suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```
Expected: 1026+ passed, 0 failed.

- [ ] **Step 5.7: Commit**

```bash
git add backtest/simulator.py tests/test_backtest_simulator.py
git commit -m "feat(backtest): CHOPPY swing unlock for CONVICTION tier via get_effective_profit_lock"
```

---

## Task 6: Smoke-test — run 30d backtest and verify no regressions

**Files:**
- No code changes — smoke test only

- [ ] **Step 6.1: Run 30d backtest**

```bash
.venv/bin/python3 backtest.py --days 30 2>&1 | tail -40
```

Expected output contains:
- `Total trades :` with a non-zero count
- No `ERROR` or `Traceback` lines
- `SWING_TP1` and/or `SWING_TP2` in exit-reason breakdown (confirms tiered TP is firing)
- Force-close rate lower than or equal to the pre-fix 31.6% (safety gates reject more entries)
- All swing promotions log `TP1=5.0% TP2=10.0%` (not the old `TP=...` format)

- [ ] **Step 6.2: Confirm report prints cleanly**

```bash
.venv/bin/python3 backtest.py --days 30 2>&1 | grep -E "SWING_TP1|SWING_TP2|SWING_TRAILING|ERROR|Traceback"
```

Expected: `SWING_TP1` or `SWING_TP2` lines visible, zero `ERROR`/`Traceback` lines.

- [ ] **Step 6.3: Run full unit suite one final time**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```
Expected: 1026+ passed, 0 failed.

- [ ] **Step 6.4: Final commit**

```bash
git add -p   # review any final residual changes
git commit -m "fix(backtest): regime×tier parity — 6 gaps closed (quality, gates, TP decay, tiered swing, CHOPPY unlock)"
```

---

## Self-Review

### Spec coverage check

| Gap | Task | Status |
|-----|------|--------|
| 1. TickerQuality / MEDIUM-only tier | Task 1 | ✅ `_build_quality_map` + `get_filter_params` composite floor |
| 2. Safety gates (stoch, parabolic, gap-up) | Task 2 | ✅ `_sim_entry_signal_passes` |
| 3. Time-decay TP via `compute_tp_sl` | Task 1 + Task 3 | ✅ wired in `_open_trade`, tested in Task 3 |
| 4. Tiered swing 40/30/30 + trailing | Task 4 | ✅ `_init_tiered_swing_fields` + `evaluate_tiered_tp_action` |
| 5. CHOPPY+CONVICTION swing unlock | Task 5 | ✅ `get_effective_profit_lock` + `SWING_CHOPPY` check |
| 6. quality_tier stored on positions | Task 1 | ✅ stored in `_open_trade` as `"quality_tier"` |

### Type consistency check

- `_evaluate_swing_promotion` signature: `(state, day_bars, ts, regime, n_swings, quality_map=None)` — Task 5 matches.
- `_close_trade` signature: `(state, symbol, exit_ts, exit_raw, reason, date_str, qty=None)` — Task 4 partial close matches.
- `_open_trade` signature: `(state, symbol, ts, bar_open, atr, pos_mult, composite, tier, regime, entry_time_et)` — Task 1/3 match.
- `_init_tiered_swing_fields(pos, regime)` — used in Task 4 (promotion) and Task 5 (updated promotion block). Consistent.
- `_sim_entry_signal_passes(iv, regime, quality, params, rsi_max)` — Task 2 defines and uses consistently.

### Placeholder scan

No TBDs, no "similar to Task N" references. All test code is complete. All implementation code shown in full.

### Dead-code / ordering notes

- The existing `SWING_SL_PCT` and `SWING_TP_PCT` module-level constants in `simulator.py` become unused after Task 4 (replaced by `SwingParams.overnight_sl_pct`, `.tp1_pct`, `.tp2_pct`). Remove them from the module constants block in Task 4 to keep the file clean.
- The existing `reason` / `exit_price` local variables in the per-bar loop are split into the `if is_sw: ... else: ...` branches in Task 4. Remove any dangling `reason = None` initialization that no longer applies.
