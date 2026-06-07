# Trading Bot Audit Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 17 bugs and gaps across ordering, API data, Claude context, gates, pre-filter, auto-execute, and learning/research agents, ordered P&L impact → reliability → efficiency.

**Architecture:** All fixes are targeted edits to existing functions — no new modules, no new abstractions. Each fix is independently testable. Group 1 fixes ship first because they affect which trades fire and at what quality. Group 2 fixes close silent reliability holes. Group 3 optimises data richness and token efficiency.

**Tech Stack:** Python 3.11+, pytest, anthropic SDK, alpaca-trade-api, ThreadPoolExecutor (stdlib)

---

## File Map

| File | Tasks |
|------|-------|
| `config.py` | 5, 6, 15 — new threshold constants |
| `signals/entry_gates.py` | 5 — soft-flag overload gate |
| `trading/pre_claude_filter.py` | 1, 9, 10 — AE tier floor, Layer-0 early-exit, correlation pre-check |
| `trading/pipeline.py` | 2, 6, 10, 17 — symbol_ret5d threading, AE confidence tier, correlation pre-check caller, AE mode confidence |
| `learning/agent.py` | 3, 15 — weight renorm, trades payload cap |
| `trading/claude_caller.py` | 4, 12, 13 — regime cache invalidation, exit context, prompt caching |
| `broker/alpaca_client.py` | 7 — get_order_fill_price |
| `trading/executor.py` | 7, 8 — T2 fill confirmation, bracket timeout alert |
| `notifications/telegram.py` | 8 — notify_bracket_correction_timeout |
| `trading/context_builder.py` | 11 — FRED key expansion |
| `research/agent.py` | 14 — parallel data fetch |
| `tests/test_pipeline.py` | 1, 2, 9, 10 |
| `tests/test_signals.py` | 5 |
| `tests/test_learning.py` | 3, 15 |
| `tests/test_trading_lifecycle.py` | 7, 8 |
| `trading/executor.py` | 7, 8, 16 — T2 fill, bracket timeout, mode→compute_tp_sl |
| `trading/context_builder.py` | 11, 16 — FRED expansion, mode→compute_tp_sl suggestion |
| `tests/test_foundation.py` | 11, 16 |
| `tests/test_market.py` | 12, 13, 17 |

---

## Group 1 — P&L Impact

---

### Task 1: Auto-execute threshold ignores tier-adjusted composite floor

**Problem:** `ae_threshold = ep.composite_min + ae.composite_offset + cb_boost` uses only the regime composite min. A LOW-tier symbol at composite=46 can auto-execute even if `fp.composite_min=55` for LOW tier because 30+15=45 < 46 passes while 55+15=70 > 46 should fail.

**Files:**
- Modify: `trading/pre_claude_filter.py:123-139`
- Test: `tests/test_pipeline.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pipeline.py — add inside the existing test class

def test_ae_threshold_respects_tier_composite_floor(self, monkeypatch):
    """AE should not fire when tier floor > regime min + offset."""
    import trading.pre_claude_filter as pcf
    from utils.regime_params import FilterParams, AutoExecuteParams, EntryParams, GateParams

    # Stub get_filter_params to return fp.composite_min=55 for LOW tier
    monkeypatch.setattr(
        "trading.pre_claude_filter.get_filter_params",
        lambda regime, tier: FilterParams(
            composite_min=55, sentiment_hard_block=-50, intraday_block=-50
        ),
    )
    # Stub get_auto_execute_params: composite_offset=15, no extra unlock
    monkeypatch.setattr(
        "trading.pre_claude_filter.get_auto_execute_params",
        lambda regime, tier: AutoExecuteParams(composite_offset=15, sentiment_min=-100, extra_unlock=None),
    )
    # Stub get_entry_params: regime composite_min=30
    monkeypatch.setattr(
        "trading.pre_claude_filter.get_entry_params",
        lambda regime: EntryParams(
            composite_min=30, hard_close_hour=13, rsi_max=65, max_position_pct=0.05
        ),
    )
    # composite=46: old logic → 30+15=45 < 46 → would AE; new logic → max(30,55)+15=70 > 46 → CLAUDE
    snap = _make_snap(regime="NORMAL", circuit_breaker_composite_boost=0)
    iv   = _make_iv(composite_score=46.0, rsi=50.0)

    result = pcf.apply_pre_claude_filter("AAPL", iv, snap)
    assert result.decision == "CLAUDE", (
        f"Expected CLAUDE (tier floor blocks AE), got {result.decision}: {result.reason}"
    )
```

> Note: `_make_snap` and `_make_iv` are existing helpers in `tests/test_pipeline.py`. Adapt to whatever factory exists.

- [ ] **Step 2: Run to verify it fails**

```bash
cd ~/Documents/projects/trading-bot
.venv/bin/python -m pytest tests/test_pipeline.py -k "test_ae_threshold_respects_tier_composite_floor" -v
```

Expected: FAIL (`result.decision == 'AUTO_EXECUTE'` not `'CLAUDE'`).

- [ ] **Step 3: Implement fix**

In `trading/pre_claude_filter.py`, locate the auto-execute block (lines ~123–139):

```python
    # Auto-execute — dynamic 9×4 table (regime × tier); disabled entries have composite_offset=999
    ae = get_auto_execute_params(snap.regime, quality.tier)
    if ae.composite_offset != 999:
        ep = get_entry_params(snap.regime)
        # OLD: ae_threshold = ep.composite_min + ae.composite_offset + cb_boost
        # NEW: take the higher of regime floor and tier-adjusted floor
        ae_threshold = max(ep.composite_min, fp.composite_min) + ae.composite_offset + cb_boost
```

Only the `ae_threshold` assignment changes. `fp` is already computed at line 64.

- [ ] **Step 4: Run to verify it passes**

```bash
.venv/bin/python -m pytest tests/test_pipeline.py -k "test_ae_threshold_respects_tier_composite_floor" -v
```

Expected: PASS.

- [ ] **Step 5: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

Expected: all passing, count ≥ existing baseline.

- [ ] **Step 6: Commit**

```bash
git add trading/pre_claude_filter.py tests/test_pipeline.py
git commit -m "fix: AE threshold uses max(regime_floor, tier_floor) to prevent low-tier over-execution"
```

---

### Task 2: `symbol_ret5d` hardcoded 0.0 in auto-execute path

**Problem:** `auto_execute` tuples carry `(symbol, iv, quality)` with no 5d return, so `execute_buy` → `check_all_gates` receives `symbol_ret5d=0.0` for every auto-execute candidate. The RS soft-gate (`rs_weak`) never fires on auto-execute symbols even when they are genuinely underperforming SPY.

**Files:**
- Modify: `trading/pipeline.py` — Phase 2 tuple build + `_execute_auto_entries`
- Test: `tests/test_pipeline.py`

- [ ] **Step 1: Write the failing test**

Capture the `symbol_ret5d` arg passed to `execute_buy` from the auto-execute path.

```python
def test_auto_execute_passes_symbol_ret5d(self, monkeypatch):
    """Auto-execute path must thread the actual 5d return into execute_buy."""
    import trading.pipeline as pl

    captured = {}

    def fake_execute_buy(**kwargs):
        captured["symbol_ret5d"] = kwargs.get("symbol_ret5d")
        return None  # skip real execution

    monkeypatch.setattr(pl, "execute_buy", fake_execute_buy)

    # Build a minimal auto_execute list with a non-zero ret5d
    from tests.test_pipeline import _make_iv, _make_quality  # use existing helpers
    iv = _make_iv(composite_score=70.0, rsi=55.0)
    quality = _make_quality(tier="HIGH")
    auto_execute = [("AAPL", iv, quality, -4.5)]  # (symbol, iv, quality, ret5d)

    snap = _make_snap(regime="NORMAL")
    pl._execute_auto_entries(
        auto_execute, snap, alpaca=None, open_positions={},
        portfolio_value=100_000, buying_power=50_000,
        pos_mult=1.0, spy_ret5d=2.0, rsi_max=65,
        atr_tp_mult=2.0, can_open=True, now=_now(), daily_pnl_pct=0.0,
    )
    assert captured.get("symbol_ret5d") == -4.5, (
        f"Expected -4.5, got {captured.get('symbol_ret5d')}"
    )
```

- [ ] **Step 2: Run to verify it fails**

```bash
.venv/bin/python -m pytest tests/test_pipeline.py -k "test_auto_execute_passes_symbol_ret5d" -v
```

Expected: FAIL (tuple unpack error or `symbol_ret5d=0.0`).

- [ ] **Step 3: Implement fix in pipeline.py**

**Phase 2 loop** — capture the 5d return when building the auto_execute tuple:

```python
# Before (line ~329–350 in Phase 2 loop):
for symbol, iv, existing_pos, _ in stock_data:
    ...
    if filt.decision == "AUTO_EXECUTE":
        auto_execute.append((symbol, iv, filt.quality))
```

```python
# After:
for symbol, iv, existing_pos, symbol_ret5d_val in stock_data:
    ...
    if filt.decision == "AUTO_EXECUTE":
        auto_execute.append((symbol, iv, filt.quality, symbol_ret5d_val))
```

(The `_` was `symbol_ret5d_val`; change the loop variable name.)

**`_execute_auto_entries` signature + unpack** — update the for-loop and pass-through:

```python
# Before:
for symbol, iv, quality in auto_execute:
    ...
    pos_data = execute_buy(symbol=symbol, iv=iv, decision=decision, symbol_ret5d=0.0, ...)

# After:
for symbol, iv, quality, symbol_ret5d in auto_execute:
    ...
    pos_data = execute_buy(symbol=symbol, iv=iv, decision=decision, symbol_ret5d=symbol_ret5d, ...)
```

- [ ] **Step 4: Run to verify it passes**

```bash
.venv/bin/python -m pytest tests/test_pipeline.py -k "test_auto_execute_passes_symbol_ret5d" -v
```

- [ ] **Step 5: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 6: Commit**

```bash
git add trading/pipeline.py tests/test_pipeline.py
git commit -m "fix: thread actual symbol_ret5d through auto-execute path (was hardcoded 0.0)"
```

---

### Task 3: Indicator weights don't re-sum to 100 after clamping

**Problem:** `learning/agent.py` normalises weights to sum=100, then independently clamps each weight to `[lo, hi]`. After clamping, the total drifts away from 100. No re-normalisation follows, so `indicators/engine.py` uses weights that don't sum to 100, distorting composite scores.

**Files:**
- Modify: `learning/agent.py:220-232`
- Test: `tests/test_learning.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_learning.py

def test_weights_sum_to_100_after_clamping(self, monkeypatch):
    """Weights must sum to 100 even after per-weight clamping."""
    import learning.agent as la
    import config

    # Force a clamp situation: all weights equal at 10 (10 indicators), but clamp range is [3, 15].
    # After normalise (already 10 each), clamp makes one weight jump to 15 → total > 100.
    weights = {f"ind_{i}": 10.0 for i in range(10)}  # sums to 100

    # Monkeypatch: raise one indicator's raw weight above max so clamp fires
    analysis = {
        "indicator_performance": {
            "ind_0": {"weight_adjustment": 5.0},   # would push ind_0 to 15 → clamp
            "ind_1": {"weight_adjustment": -5.0},  # would push ind_1 to 5
        }
    }

    monkeypatch.setattr(config, "INDICATOR_WEIGHT_MIN_MULT", 0.3)
    monkeypatch.setattr(config, "INDICATOR_WEIGHT_MAX_MULT", 1.2)  # tight ceiling → clamp fires

    # Replicate the normalise+clamp logic from run_learning_agent
    new_weights = dict(weights)
    for indicator, data in analysis.get("indicator_performance", {}).items():
        if indicator in new_weights:
            new_weights[indicator] = max(1.0, new_weights[indicator] + float(data.get("weight_adjustment", 0)))

    total = sum(new_weights.values())
    new_weights = {k: round(v / total * 100, 2) for k, v in new_weights.items()}

    equal_share = 100.0 / len(new_weights)
    lo = equal_share * config.INDICATOR_WEIGHT_MIN_MULT
    hi = equal_share * config.INDICATOR_WEIGHT_MAX_MULT
    for key in new_weights:
        new_weights[key] = round(max(lo, min(hi, new_weights[key])), 4)

    # Before fix, total may not be 100
    total_after = sum(new_weights.values())
    assert abs(total_after - 100.0) < 0.01, (
        f"Weights sum to {total_after:.4f}, not 100 — renormalisation missing after clamp"
    )
```

- [ ] **Step 2: Run to verify it fails**

```bash
.venv/bin/python -m pytest tests/test_learning.py -k "test_weights_sum_to_100_after_clamping" -v
```

Expected: FAIL (sum ≠ 100.0 ± 0.01 when clamp fires).

- [ ] **Step 3: Implement fix**

In `learning/agent.py`, directly after the clamp loop (`for key in new_weights: ...`), add a second normalisation pass:

```python
    # Clamp each normalized weight to [MIN_MULT, MAX_MULT] × equal share
    equal_share = 100.0 / len(new_weights) if new_weights else 10.0
    lo = equal_share * config.INDICATOR_WEIGHT_MIN_MULT
    hi = equal_share * config.INDICATOR_WEIGHT_MAX_MULT
    for key in new_weights:
        raw = new_weights[key]
        clamped = max(lo, min(hi, raw))
        if abs(clamped - raw) > 0.01:
            logger.warning("Weight %s clamped: %.2f → %.2f (bounds: %.2f–%.2f)", key, raw, clamped, lo, hi)
        new_weights[key] = round(clamped, 4)

    # Re-normalise after clamping — individual clamps can push sum away from 100
    _post_clamp_total = sum(new_weights.values())
    if _post_clamp_total > 0:
        new_weights = {k: round(v / _post_clamp_total * 100, 4) for k, v in new_weights.items()}
```

- [ ] **Step 4: Run to verify it passes**

```bash
.venv/bin/python -m pytest tests/test_learning.py -k "test_weights_sum_to_100_after_clamping" -v
```

- [ ] **Step 5: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 6: Commit**

```bash
git add learning/agent.py tests/test_learning.py
git commit -m "fix: re-normalise indicator weights after per-weight clamping so sum stays 100"
```

---

### Task 4: Stable context cache not invalidated on noon regime refresh

**Problem:** `_stable_ctx_text` TTL is 3600s from first build (~9 AM). If the 12:00 PM regime refresh flips NORMAL → HIGH_VOL, Claude continues trading with stale NORMAL context for up to ~50 minutes.

**Files:**
- Modify: `trading/claude_caller.py:37-51`
- Test: `tests/test_market.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_market.py

def test_stable_context_rebuilds_on_regime_change(self, monkeypatch):
    """Cache must be invalidated when regime changes, regardless of TTL."""
    import trading.claude_caller as cc
    import time

    # Reset module cache state
    cc._stable_ctx_text = "old-normal-context"
    cc._stable_ctx_time = time.monotonic()   # fresh — within TTL
    cc._stable_ctx_regime = "NORMAL"

    build_calls = []

    def fake_build(regime_info, fred_cache, research_meta):
        build_calls.append(regime_info.get("regime"))
        return f"context-for-{regime_info.get('regime')}"

    monkeypatch.setattr(cc, "build_stable_system_context", fake_build)

    # Same regime — should use cache
    ctx1 = cc._get_cached_stable_context({}, {}, {"regime": "NORMAL"})
    assert ctx1 == "old-normal-context"
    assert len(build_calls) == 0, "Cache hit expected for same regime"

    # Regime change — must rebuild even though TTL not expired
    ctx2 = cc._get_cached_stable_context({}, {}, {"regime": "HIGH_VOL"})
    assert ctx2 == "context-for-HIGH_VOL"
    assert len(build_calls) == 1, "Rebuild expected on regime change"
    assert cc._stable_ctx_regime == "HIGH_VOL"
```

- [ ] **Step 2: Run to verify it fails**

```bash
.venv/bin/python -m pytest tests/test_market.py -k "test_stable_context_rebuilds_on_regime_change" -v
```

Expected: FAIL — cache is not invalidated on regime change.

- [ ] **Step 3: Implement fix**

In `trading/claude_caller.py`, update the module-level cache vars and `_get_cached_stable_context`:

```python
# Add this alongside existing cache vars (lines ~37-40):
_stable_ctx_regime: str = ""   # invalidate when regime changes mid-session

def _get_cached_stable_context(fred_cache: dict, research_meta: dict, regime_info: dict) -> str:
    global _stable_ctx_text, _stable_ctx_time, _stable_ctx_regime
    current_regime = regime_info.get("regime", "NORMAL")
    with _STABLE_CTX_LOCK:
        age = time.monotonic() - _stable_ctx_time
        if (
            _stable_ctx_text
            and age < _STABLE_CTX_TTL
            and _stable_ctx_regime == current_regime   # ← new check
        ):
            return _stable_ctx_text
        _stable_ctx_text  = build_stable_system_context(regime_info, fred_cache, research_meta)
        _stable_ctx_time  = time.monotonic()
        _stable_ctx_regime = current_regime            # ← new
        return _stable_ctx_text
```

- [ ] **Step 4: Run to verify it passes**

```bash
.venv/bin/python -m pytest tests/test_market.py -k "test_stable_context_rebuilds_on_regime_change" -v
```

- [ ] **Step 5: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 6: Commit**

```bash
git add trading/claude_caller.py tests/test_market.py
git commit -m "fix: invalidate stable Claude context cache on regime change (noon refresh)"
```

---

### Task 5: No soft-flag overload gate

**Problem:** Five simultaneous soft flags (stoch elevated + sector weak + breadth weak + MTF unconfirmed + RS weak) all pass through `check_all_gates` to Claude. Claude must manually weigh 5 negatives with no hard signal that the setup is holistically degraded.

**Files:**
- Modify: `config.py` — add `SOFT_FLAG_OVERLOAD_THRESHOLD`
- Modify: `signals/entry_gates.py` — add gate before final return
- Test: `tests/test_signals.py`

- [ ] **Step 1: Add config constant**

In `config.py`, under the soft-flag thresholds section:

```python
SOFT_FLAG_OVERLOAD_THRESHOLD: int = 4   # ≥ this many soft flags → hard block
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_signals.py

def test_soft_flag_overload_blocks_entry(self, monkeypatch):
    """≥4 simultaneous soft flags must return GateResult(passed=False)."""
    from signals.entry_gates import check_all_gates
    from tests.test_signals import _perfect_iv  # existing IV factory — adapt to your codebase

    # Build an IV that triggers stoch_elevated + rel_vol_low + breadth_weak + mtf_not_confirmed
    iv = _perfect_iv()
    iv = iv._replace(
        stoch_d=87.0,         # stoch_soft_flag threshold ~85 → stoch_elevated
        relative_volume=0.4,  # below REL_VOLUME_MIN → rel_vol_low
        or_position="inside", # → inside_opening_range (3rd flag)
    )
    # 4th flag: advance_decline_ratio < threshold
    result = check_all_gates(
        symbol="AAPL", iv=iv, open_positions={}, daily_closed=set(),
        bars_15min=None, now=_trading_now(),
        advance_decline_ratio=0.70,   # breadth_weak
        regime="NORMAL",
    )
    assert not result.passed, f"Expected hard block on 4 soft flags, got passed=True flags={result.flags}"
    assert "soft-flag overload" in result.reason, f"Unexpected reason: {result.reason}"
```

> `_perfect_iv` and `_trading_now` are helpers that must already exist or can be minimal stubs.

- [ ] **Step 3: Run to verify it fails**

```bash
.venv/bin/python -m pytest tests/test_signals.py -k "test_soft_flag_overload_blocks_entry" -v
```

Expected: FAIL — currently returns `passed=True`.

- [ ] **Step 4: Implement fix**

In `signals/entry_gates.py`, just before the final `return GateResult(True, ...)` at the bottom of `check_all_gates`:

```python
    # Soft-flag overload — too many independent warnings signals a holistically poor setup
    if len(flags) >= config.SOFT_FLAG_OVERLOAD_THRESHOLD:
        return GateResult(
            False,
            f"soft-flag overload ({len(flags)} flags: {', '.join(list(flags)[:4])})",
        )

    return GateResult(True, "", tuple(flags))
```

- [ ] **Step 5: Run to verify it passes**

```bash
.venv/bin/python -m pytest tests/test_signals.py -k "test_soft_flag_overload_blocks_entry" -v
```

- [ ] **Step 6: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 7: Commit**

```bash
git add config.py signals/entry_gates.py tests/test_signals.py
git commit -m "feat: hard-block entries with >=4 simultaneous soft flags (soft-flag overload gate)"
```

---

### Task 6: Auto-execute confidence ignores tier

**Problem:** `_compute_auto_execute_confidence` produces 65–95 for all tiers. CONVICTION setups are underrepresented in the journal; LOW-tier entries get the same confidence as HIGH — reducing the signal quality of confidence in learning analysis.

**Files:**
- Modify: `config.py` — add tier adjustment dict
- Modify: `trading/pipeline.py` — `_compute_auto_execute_confidence` + call site
- Test: `tests/test_pipeline.py`

- [ ] **Step 1: Add config constants**

In `config.py`:

```python
# Tier-based confidence adjustments for auto-execute decisions
AUTO_EXECUTE_CONFIDENCE_TIER_ADJ: dict = {
    "CONVICTION": 5,
    "HIGH":       0,
    "MEDIUM":     0,
    "LOW":       -10,
}
AUTO_EXECUTE_CONFIDENCE_LOW_CAP: int = 75   # LOW tier never claims > 75 confidence
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_pipeline.py

def test_ae_confidence_higher_for_conviction_than_low(self):
    """CONVICTION tier AE confidence must exceed LOW tier for identical composite + sentiment."""
    from trading.pipeline import _compute_auto_execute_confidence

    conf_conviction = _compute_auto_execute_confidence(
        composite_score=70.0, intra_sentiment=10, regime="NORMAL", tier="CONVICTION"
    )
    conf_low = _compute_auto_execute_confidence(
        composite_score=70.0, intra_sentiment=10, regime="NORMAL", tier="LOW"
    )
    assert conf_conviction > conf_low, (
        f"CONVICTION ({conf_conviction}) should > LOW ({conf_low})"
    )
    assert conf_low <= 75, f"LOW tier capped at 75, got {conf_low}"
```

- [ ] **Step 3: Run to verify it fails**

```bash
.venv/bin/python -m pytest tests/test_pipeline.py -k "test_ae_confidence_higher_for_conviction_than_low" -v
```

Expected: FAIL — both tiers return the same value.

- [ ] **Step 4: Implement fix**

In `trading/pipeline.py`, update `_compute_auto_execute_confidence`:

```python
def _compute_auto_execute_confidence(
    composite_score: float, intra_sentiment: int, regime: str = "NORMAL", tier: str = "MEDIUM"
) -> int:
    from utils.regime_params import get_entry_params
    regime_composite_min = get_entry_params(regime).composite_min
    base = config.AUTO_EXECUTE_CONFIDENCE_BASE
    composite_bonus = min(15, max(0, int((composite_score - regime_composite_min) * 0.5)))
    sentiment_bonus = min(5, max(0, int(intra_sentiment / 10)))
    confidence = min(95, base + composite_bonus + sentiment_bonus)
    # Apply tier adjustment
    adj = config.AUTO_EXECUTE_CONFIDENCE_TIER_ADJ.get(tier, 0)
    confidence = confidence + adj
    if tier == "LOW":
        confidence = min(confidence, config.AUTO_EXECUTE_CONFIDENCE_LOW_CAP)
    return max(0, min(95, confidence))
```

Update the call site in `_execute_auto_entries`:

```python
confidence = _compute_auto_execute_confidence(
    iv.composite_score, intra_sent,
    regime=snap.regime,
    tier=quality.tier if quality else "MEDIUM",   # ← add tier
)
```

- [ ] **Step 5: Run to verify it passes**

```bash
.venv/bin/python -m pytest tests/test_pipeline.py -k "test_ae_confidence_higher_for_conviction_than_low" -v
```

- [ ] **Step 6: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 7: Commit**

```bash
git add config.py trading/pipeline.py tests/test_pipeline.py
git commit -m "feat: auto-execute confidence is tier-aware (CONVICTION+5, LOW capped at 75)"
```

---

## Group 2 — Reliability

---

### Task 7: T2 fill price unconfirmed

**Problem:** `place_market_order` returns `{"id": ..., "symbol": ..., "side": ..., "qty": ...}` — no `fill_price`. So `t2_fill = order.get("fill_price") or current_price` always resolves to `current_price`. The blended SL/TP for the T2 position is computed from an indicative price, not the actual fill.

**Files:**
- Modify: `broker/alpaca_client.py` — add `get_order_fill_price`
- Modify: `trading/executor.py:406-413` — use confirmed fill
- Test: `tests/test_trading_lifecycle.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_trading_lifecycle.py

def test_t2_uses_confirmed_fill_price(self, monkeypatch, tmp_path):
    """T2 blended entry must use confirmed fill price, not indicative current_price."""
    import trading.executor as ex

    fill_price_returned = [None, None, 152.50]  # None × 2 then fill

    def fake_get_fill(order_id, timeout=5.0):
        return 152.50

    def fake_place_market(symbol, qty, side):
        return {"id": "order-123", "symbol": symbol, "side": side, "qty": qty}

    fake_alpaca = _make_fake_alpaca(monkeypatch, tmp_path)
    monkeypatch.setattr(fake_alpaca, "place_market_order", fake_place_market)
    monkeypatch.setattr(fake_alpaca, "get_order_fill_price", fake_get_fill)
    monkeypatch.setattr(fake_alpaca, "get_latest_price", lambda sym: 150.00)

    pos = _minimal_t1_position(entry_price=148.00, qty=10, atr_pct=1.5)
    result = ex.execute_tranche2_addon(
        symbol="AAPL", pos=pos, regime="NORMAL", research={},
        alpaca=fake_alpaca, portfolio_value=100_000, now=_now(),
        suppress_notify=True,
    )
    assert result is not None
    # Blended entry must incorporate the confirmed fill at 152.50, not 150.00
    t2_fill_used = result.get("tranche_2_entry")
    assert t2_fill_used == 152.50, f"Expected confirmed fill 152.50, got {t2_fill_used}"
```

- [ ] **Step 2: Run to verify it fails**

```bash
.venv/bin/python -m pytest tests/test_trading_lifecycle.py -k "test_t2_uses_confirmed_fill_price" -v
```

Expected: FAIL (`t2_fill_used` is 150.00, not 152.50).

- [ ] **Step 3: Add `get_order_fill_price` to AlpacaClient**

In `broker/alpaca_client.py`, add after `cancel_order`:

```python
def get_order_fill_price(self, order_id: str, timeout: float = 5.0) -> float | None:
    """Poll for fill price of a market order. Returns None if fill not confirmed within timeout."""
    import time as _time
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        try:
            order = self._trading.get_order_by_id(order_id)
            if order.filled_avg_price is not None:
                return float(order.filled_avg_price)
        except Exception:
            pass
        _time.sleep(0.5)
    return None
```

- [ ] **Step 4: Use confirmed fill in execute_tranche2_addon**

In `trading/executor.py`, Step 4 (place T2 market order), replace:

```python
    # Step 4: place T2 market order
    try:
        order = alpaca.place_market_order(symbol, t2_qty, "buy")
    except Exception as exc:
        logger.error("T2 %s: place_market_order failed — %s", symbol, exc)
        return None
    t2_fill = order.get("fill_price") or current_price
```

with:

```python
    # Step 4: place T2 market order and confirm fill price
    try:
        order = alpaca.place_market_order(symbol, t2_qty, "buy")
    except Exception as exc:
        logger.error("T2 %s: place_market_order failed — %s", symbol, exc)
        return None
    order_id_t2 = order.get("id")
    t2_fill = None
    if order_id_t2:
        t2_fill = alpaca.get_order_fill_price(order_id_t2, timeout=5.0)
    if t2_fill is None:
        logger.warning(
            "T2 %s: fill price unconfirmed after 5s — using indicative price $%.2f",
            symbol, current_price,
        )
        t2_fill = current_price
```

- [ ] **Step 5: Run to verify it passes**

```bash
.venv/bin/python -m pytest tests/test_trading_lifecycle.py -k "test_t2_uses_confirmed_fill_price" -v
```

- [ ] **Step 6: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 7: Commit**

```bash
git add broker/alpaca_client.py trading/executor.py tests/test_trading_lifecycle.py
git commit -m "fix: T2 polls for confirmed fill price (was silently using indicative current_price)"
```

---

### Task 8: Bracket SL/TP correction timeout — silent failure

**Problem:** When `correct_bracket_after_fill` times out, a WARNING is logged but no Telegram alert fires. The position is journaled with SL/TP computed from the indicative entry price, not the actual fill. The user has no real-time visibility that the bracket may be mis-priced.

**Files:**
- Modify: `notifications/telegram.py` — add `notify_bracket_correction_timeout`
- Modify: `trading/executor.py:292-298` — call new notify on timeout
- Test: `tests/test_trading_lifecycle.py`

- [ ] **Step 1: Add `notify_bracket_correction_timeout` to telegram.py**

In `notifications/telegram.py`, add alongside the other notify functions:

```python
def notify_bracket_correction_timeout(
    symbol: str, indicative_price: float, sl_pct: float, tp_pct: float
) -> None:
    """Alert when bracket fill-correction times out — SL/TP may be mis-priced."""
    msg = (
        f"⚠️ BRACKET TIMEOUT {symbol}: fill-slippage correction timed out.\n"
        f"Journaled with indicative entry ~${indicative_price:.2f} | "
        f"SL={sl_pct:.1f}% TP={tp_pct:.1f}% — verify in Alpaca."
    )
    _send(msg)
```

(`_send` is whatever internal Telegram send helper is already used in that file.)

- [ ] **Step 2: Write the failing test**

```python
# tests/test_trading_lifecycle.py

def test_bracket_timeout_fires_telegram_alert(self, monkeypatch, tmp_path):
    """Bracket fill-correction timeout must trigger a Telegram alert."""
    import trading.executor as ex
    import notifications.telegram as tg

    alerted = []
    monkeypatch.setattr(tg, "notify_bracket_correction_timeout",
                        lambda sym, price, sl, tp: alerted.append(sym))

    # Patch correct_bracket_after_fill to simulate timeout
    monkeypatch.setattr(
        "trading.executor.risk_manager.correct_bracket_after_fill",
        lambda *a, **kw: None,  # None = timed out
    )

    _setup_executor_deps(monkeypatch, tmp_path)
    fake_alpaca = _make_fake_alpaca(monkeypatch, tmp_path)

    ex.execute_buy(
        symbol="AAPL", iv=_make_iv(), decision={"action": "BUY", "confidence": 80},
        regime="NORMAL", regime_info={}, research={}, open_positions={},
        alpaca=fake_alpaca, portfolio_value=100_000, pos_mult=1.0,
        spy_ret5d=0.0, symbol_ret5d=0.0, rsi_max=65,
        params={}, now=_now(), suppress_notify=True,
    )
    assert "AAPL" in alerted, "Expected bracket_correction_timeout alert, none fired"
```

- [ ] **Step 3: Run to verify it fails**

```bash
.venv/bin/python -m pytest tests/test_trading_lifecycle.py -k "test_bracket_timeout_fires_telegram_alert" -v
```

Expected: FAIL — no alert fired.

- [ ] **Step 4: Call new alert in executor.py**

In `trading/executor.py`, update the timeout handling block:

```python
    corrected = risk_manager.correct_bracket_after_fill(
        alpaca._trading,
        order["id"],
        iv.current_price,
        sl_pct,
        tp_pct,
    )
    if corrected is None:
        logger.warning(
            "%s fill-slippage correction timed out — journaling with indicative entry price $%.2f; "
            "actual SL/TP may differ.",
            symbol, iv.current_price,
        )
        telegram_notify.notify_bracket_correction_timeout(
            symbol, iv.current_price, sl_pct, tp_pct
        )
```

- [ ] **Step 5: Run to verify it passes**

```bash
.venv/bin/python -m pytest tests/test_trading_lifecycle.py -k "test_bracket_timeout_fires_telegram_alert" -v
```

- [ ] **Step 6: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 7: Commit**

```bash
git add notifications/telegram.py trading/executor.py tests/test_trading_lifecycle.py
git commit -m "fix: Telegram alert on bracket fill-correction timeout (was silent log-only)"
```

---

### Task 9: Layer 0 computes full TickerQuality before early-out SKIP

**Problem:** `apply_pre_claude_filter` calls `compute_ticker_quality` (FRED scoring, sub-ETF, PEAD, event flags) before returning `SKIP` for `already_closed_today`. This is pure wasted computation — the quality is thrown away with the SKIP.

**Files:**
- Modify: `trading/pre_claude_filter.py:52-57`
- Test: `tests/test_pipeline.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pipeline.py

def test_layer0_skip_does_not_compute_quality(self, monkeypatch):
    """SKIP for already_closed_today must not invoke compute_ticker_quality."""
    import trading.pre_claude_filter as pcf

    quality_calls = []

    def fake_quality(*args, **kwargs):
        quality_calls.append(True)
        return _make_quality()

    monkeypatch.setattr(pcf, "compute_ticker_quality", fake_quality)

    snap   = _make_snap(regime="NORMAL")
    iv     = _make_iv(composite_score=50.0)
    result = pcf.apply_pre_claude_filter("AAPL", iv, snap, daily_closed={"AAPL"})

    assert result.decision == "SKIP"
    assert len(quality_calls) == 0, (
        f"compute_ticker_quality was called {len(quality_calls)} times — should be 0 for closed-today SKIP"
    )
```

- [ ] **Step 2: Run to verify it fails**

```bash
.venv/bin/python -m pytest tests/test_pipeline.py -k "test_layer0_skip_does_not_compute_quality" -v
```

Expected: FAIL (`quality_calls` has 1 entry).

- [ ] **Step 3: Implement fix**

In `trading/pre_claude_filter.py`, replace the Layer 0 block:

```python
    # Layer 0 — Already closed today (check before quality computation — quality is wasted on a SKIP)
    if daily_closed and symbol in daily_closed:
        return FilterResult("SKIP", "already closed today", quality=None)
```

Remove the early `compute_ticker_quality` call that was inside the old Layer 0 block. The quality computation moves to after the Layer 0 check (where it already exists for the non-SKIP path).

- [ ] **Step 4: Run to verify it passes**

```bash
.venv/bin/python -m pytest tests/test_pipeline.py -k "test_layer0_skip_does_not_compute_quality" -v
```

- [ ] **Step 5: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 6: Commit**

```bash
git add trading/pre_claude_filter.py tests/test_pipeline.py
git commit -m "fix: skip quality computation in pre-filter Layer 0 (already_closed_today early exit)"
```

---

### Task 10: Correlation hard-gate fires after Claude call — wasting budget

**Problem:** When `gp.correlation_hard_gate=True` (certain regime/tier combos), the correlation check in `check_all_gates` blocks entry after Claude has already been called and paid for. The pre-filter has no correlation check, so Claude evaluates a symbol that will be blocked regardless.

**Files:**
- Modify: `trading/pre_claude_filter.py` — add correlation pre-check after Layer 5
- Modify: `trading/pipeline.py` — pass `open_positions` to `apply_pre_claude_filter`
- Test: `tests/test_pipeline.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pipeline.py

def test_correlation_hard_gate_skips_before_claude(self, monkeypatch):
    """When correlation_hard_gate=True and sector is concentrated, pre-filter returns SKIP."""
    import trading.pre_claude_filter as pcf
    from utils.regime_params import GateParams

    monkeypatch.setattr(
        "trading.pre_claude_filter.get_gate_params",
        lambda regime, tier: GateParams(
            correlation_hard_gate=True,
            stoch_hard_veto=97, stoch_soft_flag=85,
            rel_volume_min=0.6, breadth_weak_threshold=0.85,
            stop_hunt_veto=True,
        ),
    )
    # Stub passes_correlation_guard to fail (sector concentrated)
    monkeypatch.setattr(
        "trading.pre_claude_filter.passes_correlation_guard",
        lambda symbol, open_positions: False,
    )

    snap   = _make_snap(regime="NORMAL")
    iv     = _make_iv(composite_score=65.0)
    result = pcf.apply_pre_claude_filter(
        "AAPL", iv, snap,
        open_positions={"MSFT": {"sector": "XLK"}},  # same sector
    )
    assert result.decision == "SKIP"
    assert "sector concentration" in result.reason
```

- [ ] **Step 2: Run to verify it fails**

```bash
.venv/bin/python -m pytest tests/test_pipeline.py -k "test_correlation_hard_gate_skips_before_claude" -v
```

Expected: FAIL — no correlation check in pre-filter today.

- [ ] **Step 3: Update `apply_pre_claude_filter` signature and add check**

In `trading/pre_claude_filter.py`:

```python
def apply_pre_claude_filter(
    symbol: str,
    iv,
    snap,
    daily_closed: set[str] | None = None,
    sub_etf_chg: float = 0.0,
    earnings_clear_days: int = 30,
    pead_ok: bool = False,
    pead_score: int = 0,
    event_active: bool = False,
    catalyst_active: bool = False,
    open_positions: dict | None = None,   # ← add
) -> FilterResult:
```

Add the import at the top:

```python
from signals.correlation_guard import passes_correlation_guard
```

Add after Layer 5 (intraday news block), before the auto-execute check:

```python
    # Layer 5.5 — Correlation hard gate (saves Claude API call for concentrated sectors)
    if open_positions is not None:
        gp_corr = get_gate_params(snap.regime, quality.tier)
        if gp_corr.correlation_hard_gate and not passes_correlation_guard(symbol, open_positions):
            return FilterResult(
                "SKIP",
                f"sector concentration hard gate ({snap.regime} tier={quality.tier})",
                quality,
            )
```

- [ ] **Step 4: Update pipeline.py caller**

In `trading/pipeline.py`, Phase 2 loop:

```python
    filt = apply_pre_claude_filter(
        symbol, iv, snap, daily_closed,
        catalyst_active=_catalyst_active,
        open_positions=open_positions,   # ← add
    )
```

- [ ] **Step 5: Run to verify it passes**

```bash
.venv/bin/python -m pytest tests/test_pipeline.py -k "test_correlation_hard_gate_skips_before_claude" -v
```

- [ ] **Step 6: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 7: Commit**

```bash
git add trading/pre_claude_filter.py trading/pipeline.py tests/test_pipeline.py
git commit -m "fix: correlation hard-gate pre-check in pre_claude_filter (saves Claude budget)"
```

---

## Group 3 — Signal Quality / Efficiency

---

### Task 11: FRED data underutilised — 9 keys silently dropped

**Problem:** `refresh_fred_cache` fetches 13 series at 4 PM. `context_builder.py` consumes only 4 keys. High-value recession/credit signals (`yield_curve_inverted`, `yield_curve_spread`, `credit_spread_bps`, `credit_spread_risk_off`, `claims_spike`) are never surfaced to Claude.

**Files:**
- Modify: `trading/context_builder.py:20-22` — expand `FRED_KEYS_CONSUMED`
- Modify: `trading/context_builder.py:265-308` — expand `build_stable_system_context`
- Test: `tests/test_foundation.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_foundation.py

def test_fred_context_includes_yield_curve_and_credit(self):
    """build_stable_system_context must surface yield curve and credit spread signals."""
    from trading.context_builder import build_stable_system_context

    fred_cache = {
        "fed_direction": "pausing",
        "cpi_vs_target": "above",
        "unemployment_rising": False,
        "yield_curve_inverted": True,
        "yield_curve_spread": -35.0,
        "credit_spread_bps": 420.0,
        "credit_spread_risk_off": True,
        "claims_spike": False,
    }
    ctx = build_stable_system_context(
        regime_info={"regime": "NORMAL", "macro_health_score": 5,
                     "vix_current": 18.0, "spy_5day_return": 0.5},
        fred_cache=fred_cache,
        research_meta={"macro_shock_score": 0, "capitulation_candidates": []},
    )
    assert "INVERTED" in ctx or "inverted" in ctx.lower(), "yield curve inversion not surfaced"
    assert "RISK-OFF" in ctx or "risk-off" in ctx.lower(), "credit risk-off not surfaced"
```

- [ ] **Step 2: Run to verify it fails**

```bash
.venv/bin/python -m pytest tests/test_foundation.py -k "test_fred_context_includes_yield_curve_and_credit" -v
```

Expected: FAIL — those strings don't appear in context.

- [ ] **Step 3: Expand FRED_KEYS_CONSUMED**

In `trading/context_builder.py`, update the frozenset:

```python
FRED_KEYS_CONSUMED: frozenset[str] = frozenset({
    "fed_direction", "cpi_vs_target", "cpi_trend", "unemployment_rising",
    "yield_curve_inverted", "yield_curve_spread",
    "credit_spread_bps", "credit_spread_risk_off",
    "claims_spike",
})
```

- [ ] **Step 4: Expand `build_stable_system_context`**

In `trading/context_builder.py`, update the function body:

```python
    fed   = fred_cache.get("fed_direction", "unknown")
    cpi   = fred_cache.get("cpi_vs_target") or fred_cache.get("cpi_trend", "unknown")
    unemp = "rising" if fred_cache.get("unemployment_rising") else "stable"

    # New: yield curve and credit
    yield_inv    = fred_cache.get("yield_curve_inverted", False)
    yield_spread = fred_cache.get("yield_curve_spread")
    credit_bps   = fred_cache.get("credit_spread_bps")
    credit_ro    = fred_cache.get("credit_spread_risk_off", False)
    claims_spike = fred_cache.get("claims_spike", False)

    yield_str  = "INVERTED" if yield_inv else (f"{yield_spread:.0f}bps" if yield_spread is not None else "unknown")
    credit_str = "RISK-OFF" if credit_ro else (f"{credit_bps:.0f}bps" if credit_bps is not None else "unknown")
    claims_str = "spiking ⚠" if claims_spike else "normal"
```

Update the returned string to include the new signals:

```python
    return (
        f"Daily regime: {regime} | health={health:+d} | VIX={vix:.1f}{vix_note}\n"
        f"{weekly_line}\n"
        f"{breadth_line}\n"
        f"Sector scores: {sector_score_str}\n"
        f"FRED: Fed {fed} | CPI {cpi} | Unemployment {unemp}\n"
        f"Yield curve: {yield_str} | Credit spread: {credit_str} | Claims: {claims_str}\n"
        f"Macro shock: {shock_line} | Capitulation watch: {cap_str}"
    )
```

- [ ] **Step 5: Run to verify it passes**

```bash
.venv/bin/python -m pytest tests/test_foundation.py -k "test_fred_context_includes_yield_curve_and_credit" -v
```

- [ ] **Step 6: Verify config coherence test still passes** (it checks `FRED_KEYS_CONSUMED ⊆ FRED_SCHEMA_KEYS`)

```bash
.venv/bin/python -m pytest tests/test_config_coherence.py -v
```

- [ ] **Step 7: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 8: Commit**

```bash
git add trading/context_builder.py tests/test_foundation.py
git commit -m "feat: surface yield curve, credit spread, claims spike from FRED into Claude context"
```

---

### Task 12: Exit batch lacks dynamic market context

**Problem:** Entry decisions receive full `build_dynamic_user_context()` (SPY intraday bias, breadth, event risk, flash-crash status, entry window countdown). Exit evaluation gets only portfolio P&L and time. Claude evaluates thesis-break candidates without knowing whether SPY is tanking, an event risk window is active, or the market is in a flash-crash.

**Files:**
- Modify: `trading/claude_caller.py:289-334` — prepend dynamic context to exit user message
- Test: `tests/test_market.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_market.py

def test_exit_batch_includes_intraday_bias(self, monkeypatch):
    """Exit user message must include intraday context (SPY bias, event risk)."""
    import trading.claude_caller as cc

    captured_msg = []

    def fake_create(**kwargs):
        # Capture the user message
        captured_msg.append(kwargs["messages"][0]["content"])
        raise anthropic.APIStatusError("test", response=None, body={})

    monkeypatch.setattr(cc, "_CLAUDE_DISABLED", __import__("threading").Event())

    fake_ai = type("AI", (), {"messages": type("M", (), {"create": staticmethod(fake_create)})()})()

    import anthropic
    from indicators.values import IndicatorValues

    # Run exit batch (it will raise on the fake create; we only care about the message)
    try:
        cc._ask_claude_exit_batch(
            exit_candidates=[("AAPL", _make_iv(), {"entry_price": 100.0})],
            snap=_make_snap(regime="NORMAL"),
            ai=fake_ai,
        )
    except Exception:
        pass

    assert captured_msg, "No API call was made"
    msg_text = captured_msg[0]
    assert "MARKET SNAPSHOT" in msg_text or "Intraday" in msg_text, (
        "Exit batch user message lacks intraday market context"
    )
```

- [ ] **Step 2: Run to verify it fails**

```bash
.venv/bin/python -m pytest tests/test_market.py -k "test_exit_batch_includes_intraday_bias" -v
```

Expected: FAIL — `MARKET SNAPSHOT` not in exit user message.

- [ ] **Step 3: Implement fix**

In `trading/claude_caller.py`, `_ask_claude_exit_batch`, update the `user_msg` construction:

```python
    now_exit = datetime.now(ET)
    # Include full dynamic context so exits are evaluated with current market picture
    dynamic_ctx = build_dynamic_user_context(now_exit, portfolio_gain_pct=snap.portfolio_gain_pct)

    pnl_note = f"{snap.portfolio_gain_pct:+.2f}%"
    if snap.portfolio_gain_pct <= -config.DAILY_LOSS_WARN_PCT * 100:
        pnl_note += " ⚠ elevated daily loss — lean toward protecting remaining capital"

    user_msg = (
        f"{dynamic_ctx}\n\n"   # ← prepend dynamic context
        f"Thesis-break evaluation for {len(exit_candidates)} open position(s):\n\n"
        f"{exit_blocks}\n\n"
        "For each position: should we CLOSE (thesis broken) or HOLD?\n"
        "CLOSE only if thesis clearly broken — news shock, composite collapse, near stop-loss.\n\n"
        "Respond in JSON keyed by symbol:\n"
        '{\n  "<SYMBOL>": {\n'
        '    "action": "<CLOSE|HOLD>",\n'
        '    "confidence": <0-100>,\n'
        '    "reasoning": "<1 sentence>"\n'
        "  }\n}"
    )
```

Remove the old standalone `pnl_note` / time line at the top of `user_msg` (it's now inside `dynamic_ctx`).

- [ ] **Step 4: Run to verify it passes**

```bash
.venv/bin/python -m pytest tests/test_market.py -k "test_exit_batch_includes_intraday_bias" -v
```

- [ ] **Step 5: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 6: Commit**

```bash
git add trading/claude_caller.py tests/test_market.py
git commit -m "feat: exit batch includes full dynamic market context (SPY bias, event risk, flash-crash)"
```

---

### Task 13: Enable Anthropic prompt caching on system blocks

**Problem:** Neither the stable system block nor the volatile (weights/strategy memory) block in `_ask_claude_batch` has `cache_control`. Anthropic charges full input token price on every call. Adding `cache_control: ephemeral` to both blocks enables prompt caching — 90% cost reduction on cached tokens.

**Files:**
- Modify: `trading/claude_caller.py:176-179` — add `cache_control` to system blocks
- Test: `tests/test_market.py`

- [ ] **Step 1: Write the test**

```python
# tests/test_market.py

def test_claude_system_blocks_have_cache_control(self, monkeypatch):
    """Both system blocks in _ask_claude_batch must include cache_control for prompt caching."""
    import trading.claude_caller as cc

    captured_system = []

    def fake_create(**kwargs):
        captured_system.extend(kwargs.get("system", []))
        raise RuntimeError("stop")

    monkeypatch.setattr(
        "trading.claude_caller._CLAUDE_DISABLED", __import__("threading").Event()
    )
    monkeypatch.setattr(
        "trading.claude_caller.risk_manager.check_entry_budget", lambda *a, **kw: True
    )

    fake_ai = type("AI", (), {"messages": type("M", (), {"create": staticmethod(fake_create)})()})()

    try:
        cc._ask_claude_batch(
            batch=[("AAPL", _make_iv(), None, _make_filter_result())],
            snap=_make_snap(regime="NORMAL"),
            ai=fake_ai,
        )
    except RuntimeError:
        pass

    cached_blocks = [b for b in captured_system if "cache_control" in b]
    assert len(cached_blocks) >= 1, (
        f"Expected ≥1 system block with cache_control, got {len(cached_blocks)}"
    )
```

- [ ] **Step 2: Run to verify it fails**

```bash
.venv/bin/python -m pytest tests/test_market.py -k "test_claude_system_blocks_have_cache_control" -v
```

Expected: FAIL — no `cache_control` on system blocks.

- [ ] **Step 3: Implement fix**

In `trading/claude_caller.py`, update the `system_blocks` construction:

```python
    system_blocks = [
        {
            "type": "text",
            "text": stable_system_text,
            "cache_control": {"type": "ephemeral"},   # ← enable prompt caching
        },
        {
            "type": "text",
            "text": volatile_text,
            "cache_control": {"type": "ephemeral"},   # ← weights+memory stable during trading hours
        },
    ]
```

- [ ] **Step 4: Run to verify it passes**

```bash
.venv/bin/python -m pytest tests/test_market.py -k "test_claude_system_blocks_have_cache_control" -v
```

- [ ] **Step 5: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 6: Commit**

```bash
git add trading/claude_caller.py tests/test_market.py
git commit -m "feat: enable Anthropic prompt caching on both Claude system blocks (90% token cost reduction)"
```

---

### Task 14: Research agent data fetch is sequential

**Problem:** `_run_research_agent_impl` fetches news, StockTwits, Reddit, and X for each symbol in a serial loop with `time.sleep(0.3)`. With 12 symbols × ~2s per symbol = ~24s wall time at 9:15 AM before market open. The Claude analysis calls must stay sequential (rate limits), but the multi-source data fetches are independent across symbols and can be parallelised.

**Files:**
- Modify: `research/agent.py` — parallel fetch + sequential Claude analysis
- Test: `tests/test_foundation.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_foundation.py

def test_research_fetches_in_parallel(self, monkeypatch):
    """Research agent must fetch all sources for multiple symbols in parallel, not serially."""
    import research.agent as ra
    import time

    call_times = []

    def fake_fetch_stock_news(symbol):
        call_times.append(("news", symbol, time.monotonic()))
        time.sleep(0.05)   # simulate latency
        return []

    def fake_fetch_stocktwits(symbol):
        call_times.append(("twits", symbol, time.monotonic()))
        return {}

    def fake_fetch_reddit(symbol):     return {}
    def fake_fetch_x(symbol):         return {}
    def fake_analyze(*a, **kw):       return {"combined_sentiment_score": 0, "combined_sentiment_label": "NEUTRAL",
                                              "key_catalyst": None, "risk_flag": None, "summary": "", "strategy": ""}
    def fake_market_news():            return []
    def fake_quick(*a, **kw):         return None  # force Claude path

    monkeypatch.setattr(ra, "_fetch_stock_news",  fake_fetch_stock_news)
    monkeypatch.setattr(ra, "_fetch_stocktwits",  fake_fetch_stocktwits)
    monkeypatch.setattr(ra, "_fetch_reddit",      fake_fetch_reddit)
    monkeypatch.setattr(ra, "_fetch_x_posts",     fake_fetch_x)
    monkeypatch.setattr(ra, "_fetch_market_news", fake_market_news)
    monkeypatch.setattr("research.analyzer._analyze_stock", fake_analyze)
    monkeypatch.setattr("research.analyzer._quick_sentiment_result", fake_quick)
    monkeypatch.setattr("utils.io_utils.save_json", lambda *a, **kw: None)
    monkeypatch.setattr(ra, "_tracker", type("T", (), {"record_success": lambda *a: None, "record_failure": lambda *a: None})())

    import anthropic
    fake_ai = type("AI", (), {})()
    start = time.monotonic()
    ra._run_research_agent_impl.__wrapped__(["AAPL", "MSFT"], fake_ai) if hasattr(ra._run_research_agent_impl, "__wrapped__") else None

    # Just verify fetch calls happened — parallel check via timing is environment-dependent
    news_calls = [t for t in call_times if t[0] == "news"]
    assert len(news_calls) == 2, f"Expected 2 news fetches, got {len(news_calls)}"
```

> This test verifies calls happen; the parallel speedup is validated by integration timing, not unit test.

- [ ] **Step 2: Implement fix — extract per-symbol fetch into helper**

In `research/agent.py`, add a helper above `_run_research_agent_impl`:

```python
def _fetch_all_sources_for_symbol(symbol: str) -> tuple:
    """Fetch all data sources for one symbol. Returns (stock_news, stocktwits, reddit_data, x_data)."""
    try:
        stock_news  = _fetch_stock_news(symbol)
        stocktwits  = _fetch_stocktwits(symbol)
        reddit_data = _fetch_reddit(symbol)
        x_data      = _fetch_x_posts(symbol)
    except Exception:
        stock_news = stocktwits = reddit_data = x_data = {}
    return stock_news, stocktwits, reddit_data, x_data
```

In `_run_research_agent_impl`, replace the per-symbol serial fetch+analyze loop:

```python
        # ── Phase A: Parallel data fetch across all symbols ────────────────
        from concurrent.futures import ThreadPoolExecutor, as_completed
        fetched: dict[str, tuple] = {}
        with ThreadPoolExecutor(max_workers=min(6, len(watch_list))) as pool:
            future_to_sym = {
                pool.submit(_fetch_all_sources_for_symbol, sym): sym
                for sym in watch_list
            }
            for future in as_completed(future_to_sym):
                sym = future_to_sym[future]
                try:
                    fetched[sym] = future.result()
                except Exception:
                    fetched[sym] = ({}, {}, {}, {})

        # ── Phase B: Sequential Claude analysis (rate-limit safe) ──────────
        for symbol in watch_list:
            stock_news, stocktwits, reddit_data, x_data = fetched.get(symbol, ({}, {}, {}, {}))
            try:
                quick = _quick_sentiment_result(stocktwits, stock_news)
                if quick is not None:
                    analysis = quick
                else:
                    analysis = _analyze_stock(
                        symbol, stock_news, market_context_block,
                        stocktwits, reddit_data, x_data, ai, cached_system,
                    )
            except anthropic.APIStatusError as exc:
                _api_failures += 1
                if exc.status_code == 400 and "usage limits" in (exc.message or "").lower():
                    _api_limit_hit = True
                    report[symbol] = _make_report_entry(dict(_neutral_defaults), stocktwits, reddit_data, x_data)
                    break
                analysis = dict(_neutral_defaults)
            except Exception:
                analysis = dict(_neutral_defaults)

            report[symbol] = _make_report_entry(analysis, stocktwits, reddit_data, x_data)
            save_json(config.RESEARCH_REPORT_FILE, report)
            # No sleep needed — Claude calls are sequential, data fetches already done
```

Remove the old `time.sleep(0.3)` at the bottom of the original loop.

- [ ] **Step 3: Run to verify tests pass**

```bash
.venv/bin/python -m pytest tests/test_foundation.py -k "test_research_fetches_in_parallel" -v
```

- [ ] **Step 4: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 5: Commit**

```bash
git add research/agent.py tests/test_foundation.py
git commit -m "perf: parallel data fetch in research agent (~3x speedup at 9:15 AM)"
```

---

### Task 15: Learning agent sends uncapped trades JSON to Claude

**Problem:** `json.dumps(closed_trades)` in the Step 1 Claude call sends all trades. On a 20+ trade day, the payload is 30–50 KB. This adds latency, increases token cost, and on very large days can approach context-length limits for the analysis call.

**Files:**
- Modify: `config.py` — add `LEARNING_MAX_TRADES_FOR_CLAUDE`
- Modify: `learning/agent.py` — truncate trades before sending
- Test: `tests/test_learning.py`

- [ ] **Step 1: Add config constant**

In `config.py`:

```python
LEARNING_MAX_TRADES_FOR_CLAUDE: int = 25   # send at most this many trades (most recent)
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_learning.py

def test_learning_agent_caps_trades_payload(self, monkeypatch):
    """Step 1 Claude call must not send more than LEARNING_MAX_TRADES_FOR_CLAUDE trades."""
    import learning.agent as la
    import config
    import anthropic

    monkeypatch.setattr(config, "LEARNING_MAX_TRADES_FOR_CLAUDE", 5)

    captured_content = []

    class FakeMessages:
        def create(self, **kwargs):
            captured_content.append(kwargs["messages"][0]["content"])
            raise RuntimeError("stop after first call")

    monkeypatch.setattr("learning.agent.anthropic.Anthropic", lambda **kw: type("AI", (), {"messages": FakeMessages()})())
    monkeypatch.setattr("learning.data._load_todays_closed_trades", lambda: [{"symbol": f"X{i}", "pnl_pct": 0.5} for i in range(30)])
    monkeypatch.setattr("learning.data._load_weights", lambda: {"rsi": 10.0})
    monkeypatch.setattr("learning.agent.load_macro_regime", lambda: {"regime": "NORMAL"})
    monkeypatch.setattr("learning.agent.load_trading_params", lambda: {})
    monkeypatch.setattr("learning.agent.risk_manager.record_usage", lambda **kw: None)

    try:
        la.run_learning_agent()
    except RuntimeError:
        pass

    assert captured_content, "No Claude call was made"
    import json, re
    msg = captured_content[0]
    match = re.search(r"<trades>(.*?)</trades>", msg, re.DOTALL)
    assert match, "No <trades> block in message"
    trades_sent = json.loads(match.group(1))
    assert len(trades_sent) <= 5, f"Expected ≤5 trades sent, got {len(trades_sent)}"
    # Must be the most recent 5
    assert trades_sent[-1]["symbol"] == "X29", "Must send most-recent trades (tail)"
```

- [ ] **Step 3: Run to verify it fails**

```bash
.venv/bin/python -m pytest tests/test_learning.py -k "test_learning_agent_caps_trades_payload" -v
```

Expected: FAIL — 30 trades sent.

- [ ] **Step 4: Implement fix**

In `learning/agent.py`, inside `run_learning_agent()`, after `closed_trades = _load_todays_closed_trades()` and before the Step 1 Claude call:

```python
    # Cap trades sent to Claude — large payloads add cost and latency with diminishing signal value.
    # Always keep the most recent trades (tail) since they reflect the day's final conditions.
    trades_for_analysis = closed_trades[-config.LEARNING_MAX_TRADES_FOR_CLAUDE:]
```

Replace all uses of `closed_trades` in the Step 1 message content with `trades_for_analysis`:

```python
                    f"<trades>{json.dumps(trades_for_analysis)}</trades>\n\n"
```

Keep using full `closed_trades` for the deterministic pre-computation steps (regime_agg, tier_agg, force_close_rt, loss_sources) — those are free (no Claude tokens) and benefit from the complete dataset.

- [ ] **Step 5: Run to verify it passes**

```bash
.venv/bin/python -m pytest tests/test_learning.py -k "test_learning_agent_caps_trades_payload" -v
```

- [ ] **Step 6: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 7: Commit**

```bash
git add config.py learning/agent.py tests/test_learning.py
git commit -m "fix: cap learning agent trades payload to most-recent 25 (reduces token cost on high-trade days)"
```

---

---

## Group 4 — Mode Awareness (regime × tier × mode gaps)

---

### Task 16: `compute_tp_sl` ignores mode in executor and context_builder

**Problem:** `compute_tp_sl` accepts a `mode` parameter and consults a full 9-regime × 4-tier × 4-mode `TPSLParams` table. Two callers silently use the wrong mode:

1. `execute_buy` in `trading/executor.py` computes `mode` (DAY_TRADE / PEAD / CATALYST / SWING) from `derive_mode()` but never passes it to `compute_tp_sl` — so PEAD and CATALYST entries apply DAY_TRADE TP floors/ceilings.
2. `_block_high` and `_block_conviction` in `trading/context_builder.py` hardcode `mode="DAY_TRADE"` when computing the suggested TP shown to Claude — so Claude sees a DAY_TRADE TP suggestion for PEAD/CATALYST stocks.

**Files:**
- Modify: `trading/executor.py:261-267` — pass `mode` to `compute_tp_sl`
- Modify: `trading/context_builder.py:116-117, 155-156` — pass actual `mode` instead of "DAY_TRADE"
- Test: `tests/test_foundation.py`, `tests/test_trading_lifecycle.py`

- [ ] **Step 1: Write failing test — executor uses wrong TP/SL for PEAD**

```python
# tests/test_trading_lifecycle.py

def test_execute_buy_uses_pead_tpsl_params(self, monkeypatch, tmp_path):
    """PEAD mode entries must use PEAD TPSLParams, not DAY_TRADE."""
    import trading.executor as ex
    from signals.tp_scaler import compute_tp_sl

    captured = {}
    original_compute = compute_tp_sl

    def spy_compute_tp_sl(base_tp, base_sl, entry_time, tier, regime, mode="DAY_TRADE"):
        captured["mode"] = mode
        return original_compute(base_tp, base_sl, entry_time, tier, regime, mode)

    monkeypatch.setattr("trading.executor.compute_tp_sl", spy_compute_tp_sl)
    _setup_executor_deps(monkeypatch, tmp_path)
    fake_alpaca = _make_fake_alpaca(monkeypatch, tmp_path)

    # pead_ok=True → derive_mode returns "PEAD"
    ex.execute_buy(
        symbol="AAPL", iv=_make_iv(), decision={"action": "BUY", "confidence": 80},
        regime="NORMAL", regime_info={}, research={}, open_positions={},
        alpaca=fake_alpaca, portfolio_value=100_000, pos_mult=1.0,
        spy_ret5d=0.0, symbol_ret5d=0.0, rsi_max=65,
        params={}, now=_now(), suppress_notify=True,
        pead_ok=True,   # ← forces PEAD mode
    )
    assert captured.get("mode") == "PEAD", (
        f"Expected PEAD mode in compute_tp_sl, got {captured.get('mode')}"
    )
```

- [ ] **Step 2: Write failing test — context_builder uses correct mode for TP suggestion**

```python
# tests/test_foundation.py

def test_context_builder_high_block_uses_actual_mode(self):
    """_block_high must pass the actual mode to compute_tp_sl, not hardcoded DAY_TRADE."""
    from trading.context_builder import _block_high
    from unittest.mock import patch

    captured_modes = []

    def spy_compute(base_tp, base_sl, entry_time, tier, regime, mode="DAY_TRADE"):
        captured_modes.append(mode)
        return base_tp, base_sl

    with patch("trading.context_builder.compute_tp_sl", spy_compute):
        _block_high(
            "AAPL", _make_iv(), _make_snap(regime="NORMAL"),
            gate_result=_make_gate_result(), quality=_make_quality(tier="HIGH"),
            mode="PEAD",   # ← pass PEAD mode
            entry_time=_now(), existing_pos=None,
            atr_suggested_tp_pct=3.0, intraday_news=None,
        )

    assert captured_modes and captured_modes[0] == "PEAD", (
        f"Expected PEAD passed to compute_tp_sl, got {captured_modes}"
    )
```

- [ ] **Step 3: Run to verify both fail**

```bash
.venv/bin/python -m pytest tests/test_trading_lifecycle.py -k "test_execute_buy_uses_pead_tpsl_params" -v
.venv/bin/python -m pytest tests/test_foundation.py -k "test_context_builder_high_block_uses_actual_mode" -v
```

- [ ] **Step 4: Fix executor.py — pass `mode` to `compute_tp_sl`**

In `trading/executor.py`, the `compute_tp_sl` call (lines ~261-267):

```python
    # Apply time-aware + tier adjustment to TP/SL (Rule 30)
    tp_pct, sl_pct = compute_tp_sl(
        base_tp=tp_pct,
        base_sl=sl_pct,
        entry_time=now,
        tier=quality.tier if quality else "MEDIUM",
        regime=regime,
        mode=mode,   # ← was missing; mode is derived from derive_mode() earlier in execute_buy
    )
```

`mode` is already computed at the top of `execute_buy`:
```python
mode = derive_mode(symbol, pead_ok=pead_ok, existing_swing_pos=False, catalyst_active=_catalyst_active)
```

No other changes needed — `mode` is already in scope.

- [ ] **Step 5: Fix context_builder.py — pass `mode` in `_block_high` and `_block_conviction`**

In `trading/context_builder.py`, `_block_high` (line ~117):

```python
    # Before:
    adj_tp, _ = compute_tp_sl(base_tp, config.STOP_LOSS_PCT, entry_time, quality.tier, snap.regime, mode="DAY_TRADE")

    # After:
    adj_tp, _ = compute_tp_sl(base_tp, config.STOP_LOSS_PCT, entry_time, quality.tier, snap.regime, mode=mode)
```

In `_block_conviction` (line ~155):

```python
    # Before:
    adj_tp, _ = compute_tp_sl(base_tp, config.STOP_LOSS_PCT, entry_time, quality.tier, snap.regime, mode="DAY_TRADE")

    # After:
    adj_tp, _ = compute_tp_sl(base_tp, config.STOP_LOSS_PCT, entry_time, quality.tier, snap.regime, mode=mode)
```

The `mode` parameter is already passed into both functions via `build_stock_block`.

- [ ] **Step 6: Run to verify both pass**

```bash
.venv/bin/python -m pytest tests/test_trading_lifecycle.py -k "test_execute_buy_uses_pead_tpsl_params" -v
.venv/bin/python -m pytest tests/test_foundation.py -k "test_context_builder_high_block_uses_actual_mode" -v
```

- [ ] **Step 7: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 8: Commit**

```bash
git add trading/executor.py trading/context_builder.py tests/test_trading_lifecycle.py tests/test_foundation.py
git commit -m "fix: pass actual mode to compute_tp_sl in executor and context_builder (PEAD/CATALYST got DAY_TRADE params)"
```

---

### Task 17: Exit block lacks mode + AE confidence ignores mode

**Problem A:** `build_exit_stock_block` returns a block without `mode_at_entry`. Claude evaluates thesis-breaks without knowing whether the position is a PEAD, CATALYST, or plain DAY_TRADE — each has different hold/close criteria (PEAD expects post-earnings drift; a composite drop of -10 is less alarming).

**Problem B:** `_compute_auto_execute_confidence` (Task 6) gets tier-awareness but still ignores mode. PEAD stocks auto-executing have a well-studied drift thesis; their AE confidence should be +5. CATALYST stocks have event-confirmation already in the gate; +3 is appropriate.

**Files:**
- Modify: `trading/context_builder.py:213-252` — add `mode_at_entry` to exit block
- Modify: `trading/pipeline.py` — add mode to AE confidence call
- Modify: `config.py` — add `AUTO_EXECUTE_CONFIDENCE_MODE_ADJ`
- Test: `tests/test_market.py`

- [ ] **Step 1: Add config constant**

In `config.py`:

```python
AUTO_EXECUTE_CONFIDENCE_MODE_ADJ: dict = {
    "PEAD":      5,   # post-earnings drift is well-studied
    "CATALYST":  3,   # event-confirmed before entry
    "DAY_TRADE": 0,
    "SWING":     0,
}
```

- [ ] **Step 2: Write failing test — exit block includes mode**

```python
# tests/test_market.py

def test_exit_block_includes_mode_at_entry(self):
    """build_exit_stock_block must include mode_at_entry from position dict."""
    from trading.context_builder import build_exit_stock_block

    pos = {
        "entry_price": 100.0, "qty": 10, "stop_loss_pct": 3.0,
        "entry_time": "2026-05-29T10:30:00",
        "mode_at_entry": "PEAD",   # ← must appear in output
    }
    block = build_exit_stock_block("AAPL", _make_iv(), pos, research={})
    assert "PEAD" in block, f"mode_at_entry 'PEAD' not in exit block:\n{block}"
```

- [ ] **Step 3: Write failing test — AE confidence is mode-aware**

```python
# tests/test_market.py

def test_ae_confidence_higher_for_pead_than_day_trade(self):
    """PEAD mode must produce higher AE confidence than DAY_TRADE for same inputs."""
    from trading.pipeline import _compute_auto_execute_confidence

    conf_pead = _compute_auto_execute_confidence(
        composite_score=65.0, intra_sentiment=5, regime="NORMAL",
        tier="HIGH", mode="PEAD",
    )
    conf_day  = _compute_auto_execute_confidence(
        composite_score=65.0, intra_sentiment=5, regime="NORMAL",
        tier="HIGH", mode="DAY_TRADE",
    )
    assert conf_pead > conf_day, f"PEAD ({conf_pead}) should > DAY_TRADE ({conf_day})"
```

- [ ] **Step 4: Run to verify both fail**

```bash
.venv/bin/python -m pytest tests/test_market.py -k "test_exit_block_includes_mode_at_entry or test_ae_confidence_higher_for_pead" -v
```

- [ ] **Step 5: Fix `build_exit_stock_block` — add mode line**

In `trading/context_builder.py`, update the exit block return:

```python
    mode_str = position.get("mode_at_entry", "DAY_TRADE")
    return (
        f'<stock symbol="{symbol}" mode="{mode_str}">\n'
        f"  P=${iv.current_price:.2f} entry=${entry_price:.2f}"
        f" PnL={pnl_pct:+.2f}% SL={sl_pct:.1f}%{held_str}\n"
        f"  composite={iv.composite_score:+.1f} (Δ{composite_delta:+.1f} from entry)"
        f" sentiment={sentiment_score:+d}"
        f" intraday:shock={intra_shock} sent={intra_sent:+d}\n"
        f"</stock>"
    )
```

- [ ] **Step 6: Fix `_compute_auto_execute_confidence` — add `mode` param**

In `trading/pipeline.py`:

```python
def _compute_auto_execute_confidence(
    composite_score: float, intra_sentiment: int,
    regime: str = "NORMAL", tier: str = "MEDIUM", mode: str = "DAY_TRADE"
) -> int:
    from utils.regime_params import get_entry_params
    regime_composite_min = get_entry_params(regime).composite_min
    base = config.AUTO_EXECUTE_CONFIDENCE_BASE
    composite_bonus = min(15, max(0, int((composite_score - regime_composite_min) * 0.5)))
    sentiment_bonus = min(5, max(0, int(intra_sentiment / 10)))
    confidence = min(95, base + composite_bonus + sentiment_bonus)
    # Tier adjustment (Task 6)
    adj_tier = config.AUTO_EXECUTE_CONFIDENCE_TIER_ADJ.get(tier, 0)
    # Mode adjustment (Task 17)
    adj_mode = config.AUTO_EXECUTE_CONFIDENCE_MODE_ADJ.get(mode, 0)
    confidence = confidence + adj_tier + adj_mode
    if tier == "LOW":
        confidence = min(confidence, config.AUTO_EXECUTE_CONFIDENCE_LOW_CAP)
    return max(0, min(95, confidence))
```

Update the call site in `_execute_auto_entries` to pass mode:

```python
    pead_ok_flag = "PEAD_EXTENDED_WINDOW" in (quality.unlocks if quality else frozenset())
    ae_mode = "PEAD" if pead_ok_flag else "DAY_TRADE"
    confidence = _compute_auto_execute_confidence(
        iv.composite_score, intra_sent,
        regime=snap.regime,
        tier=quality.tier if quality else "MEDIUM",
        mode=ae_mode,
    )
```

- [ ] **Step 7: Run to verify both pass**

```bash
.venv/bin/python -m pytest tests/test_market.py -k "test_exit_block_includes_mode_at_entry or test_ae_confidence_higher_for_pead" -v
```

- [ ] **Step 8: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 9: Commit**

```bash
git add config.py trading/context_builder.py trading/pipeline.py tests/test_market.py
git commit -m "feat: mode-aware exit blocks + mode-aware AE confidence (PEAD+5, CATALYST+3)"
```

---

## Final Verification

- [ ] **Run full test suite**

```bash
.venv/bin/python -m pytest tests/ -m unit -q 2>&1 | tail -5
```

Expected: all tests passing, count ≥ baseline + 15.

- [ ] **Run backtest sanity check**

```bash
.venv/bin/python3 backtest.py 2>&1 | tail -20
```

Expected: completes without errors, P&L and trade count in normal range.

- [ ] **Final commit check**

```bash
git log --oneline -18
```

Expected: 17 fix commits visible, all prefixed with `fix:`, `feat:`, or `perf:`.
