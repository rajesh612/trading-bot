# ATR-Based Dynamic TP — Phase 2 (Live Trading) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire ATR-based dynamic take-profit into live trading: inject the calibrated TP suggestion into every Claude prompt, use it directly on the AUTO_EXECUTE path, and store `atr_pct` in every BUY journal record to enable Phase 3 learning.

**Architecture:** Phase 1 validated ATR-based TP in the backtest simulator. Phase 2 adds it to three points in the live pipeline: (1) `context_builder.py` — `build_stock_block` gets an optional `atr_suggested_tp_pct` hint that Claude sees and may override; (2) `claude_caller.py` — computes the hint per-symbol before building the prompt; (3) `pipeline.py` — AUTO_EXECUTE path substitutes `compute_atr_tp_pct` for the flat `config.TAKE_PROFIT_PCT`; (4) `executor.py` — records `atr_pct` in the BUY journal entry so Phase 3's learning agent can grid-search multipliers. The multiplier (`atr_tp_multiplier`) lives in `trading_params.json` and flows through `CycleSnapshot.params`.

**Tech Stack:** Python 3.11+, existing `signals/atr_tp.py` (`compute_atr_tp_pct`), `trading/` package, pytest `@pytest.mark.unit`.

---

## File Map

| Action | File | Change |
|---|---|---|
| Modify | `data/state/trading_params.json` | Add `"atr_tp_multiplier": 2.0` |
| Modify | `trading/context_builder.py` | `build_stock_block` — optional `atr_suggested_tp_pct` param; emit ATR-TP line |
| Modify | `trading/claude_caller.py` | Import `compute_atr_tp_pct`; compute per-symbol suggestion; pass to `build_stock_block` |
| Modify | `trading/pipeline.py` | AUTO_EXECUTE Phase 4a: use `compute_atr_tp_pct` instead of `config.TAKE_PROFIT_PCT` |
| Modify | `trading/executor.py` | Add `atr_pct` field to `append_trade()` BUY record |
| Modify | `tests/test_trading_pipeline.py` | 3 new tests |
| Modify | `tests/test_trading_budget.py` | 1 new test |

**Line-count note:** `tests/test_trading_pipeline.py` is at 734 lines and `tests/test_trading_budget.py` is at 777 lines — both over the 700-line limit. Keep new tests tight. Do not add extraneous tests; append only what is listed here.

---

## Task 1: Add `atr_tp_multiplier` to `trading_params.json`

**Files:**
- Modify: `data/state/trading_params.json`

- [ ] **Step 1: Read the current file**

```bash
cat data/state/trading_params.json
```

Expected (current state):
```json
{
  "entry_start_hour": 10,
  "entry_end_hour": 15,
  "rsi_buy_max": 70,
  "rsi_buy_max_high_vol": 65,
  "vwap_slope_min": -0.2,
  "high_vol_position_mult": 0.75,
  "stop_loss_pct": 4.0,
  "take_profit_pct": 5.0,
  "last_updated": "2026-05-17"
}
```

- [ ] **Step 2: Add `atr_tp_multiplier`**

Add one key before `"last_updated"`:
```json
{
  "entry_start_hour": 10,
  "entry_end_hour": 15,
  "rsi_buy_max": 70,
  "rsi_buy_max_high_vol": 65,
  "vwap_slope_min": -0.2,
  "high_vol_position_mult": 0.75,
  "stop_loss_pct": 4.0,
  "take_profit_pct": 5.0,
  "atr_tp_multiplier": 2.0,
  "last_updated": "2026-05-17"
}
```

- [ ] **Step 3: Verify JSON is valid**

```bash
.venv/bin/python3 -c "import json; print(json.load(open('data/state/trading_params.json'))['atr_tp_multiplier'])"
```
Expected: `2.0`

- [ ] **Step 4: Commit**

```bash
git add data/state/trading_params.json
git commit -m "feat: add atr_tp_multiplier=2.0 to trading_params.json"
```

---

## Task 2: `build_stock_block` ATR-TP hint + test

**Files:**
- Modify: `trading/context_builder.py` (lines 14–62)
- Modify: `tests/test_trading_pipeline.py` (append 1 test)

`build_stock_block` currently takes `(symbol, iv, research, existing_pos)`. We add an optional 5th parameter. When provided, the stock block gains one extra line with the calibrated TP so Claude can reference it.

- [ ] **Step 1: Write the failing test — append to `tests/test_trading_pipeline.py`**

```python
@pytest.mark.unit
def test_atr_tp_injected_into_claude_context():
    """build_stock_block emits ATR-TP line when atr_suggested_tp_pct is provided."""
    from trading.context_builder import build_stock_block
    from unittest.mock import MagicMock, patch

    iv = MagicMock()
    iv.composite_score = 42.5; iv.signal = "BUY"
    iv.current_price = 100.0; iv.vwap = 99.5
    iv.rsi = 58.0; iv.rsi_slope = 1.0; iv.macd_hist = 0.002
    iv.bb_lower = 98.0; iv.bb_upper = 102.0
    iv.ema9 = 99.8; iv.ema21 = 99.5; iv.atr = 2.0
    iv.stoch_k = 60.0; iv.stoch_d = 58.0
    iv.daily_volume_ratio = 1.2
    research = {"TEST": {"sentiment_score": 10, "sentiment_label": "NEUTRAL",
                         "key_catalyst": None, "risk_flag": None, "summary": ""}}

    with patch("calendars.earnings.detect_pead_setup", return_value=(False, 0)):
        block = build_stock_block("TEST", iv, research, None, atr_suggested_tp_pct=4.5)

    assert "ATR-TP=4.5%" in block
```

- [ ] **Step 2: Run test to confirm it fails**

```bash
.venv/bin/python -m pytest tests/test_trading_pipeline.py::test_atr_tp_injected_into_claude_context -v
```
Expected: `FAILED` — `AssertionError: "ATR-TP=4.5%" not in block` (parameter doesn't exist yet)

- [ ] **Step 3: Update `build_stock_block` in `trading/context_builder.py`**

Change the function signature (line 14) from:
```python
def build_stock_block(
    symbol: str,
    iv: IndicatorValues,
    research: dict,
    existing_pos: dict | None,
) -> str:
```
To:
```python
def build_stock_block(
    symbol: str,
    iv: IndicatorValues,
    research: dict,
    existing_pos: dict | None,
    atr_suggested_tp_pct: float | None = None,
) -> str:
```

Add one line after the `dvol` and `pead_line` variables (before the `return`):
```python
atr_tp_line = (
    f"\n  ATR-TP={atr_suggested_tp_pct:.1f}% (ATR-based). Override if catalyst justifies."
    if atr_suggested_tp_pct is not None else ""
)
```

Update the `return` statement to include `{atr_tp_line}` between the indicators line and the sentiment line:
```python
return (
    f"=={symbol}== composite={iv.composite_score:+.1f} signal={iv.signal}{pos_block}\n"
    f"  P=${price:.2f} VWAP{vwap_dir} RSI={iv.rsi:.1f}({iv.rsi_slope:+.1f})"
    f" MACD={iv.macd_hist:+.4f} BB[{iv.bb_lower:.2f},{iv.bb_upper:.2f}]"
    f" EMA9/21={iv.ema9:.2f}/{iv.ema21:.2f} Stoch={iv.stoch_k:.0f}/{iv.stoch_d:.0f}"
    f" Vol={dvol}{pead_line}"
    f"{atr_tp_line}\n"
    f"  SENT:{sentiment_label}({sentiment_score:+d}){cat_str}{risk_str}"
    f"{news_str}"
)
```

- [ ] **Step 4: Run the test — must pass**

```bash
.venv/bin/python -m pytest tests/test_trading_pipeline.py::test_atr_tp_injected_into_claude_context -v
```
Expected: `PASSED`

- [ ] **Step 5: Verify `test_stock_block_is_under_150_tokens` still passes** (it calls without `atr_suggested_tp_pct` — no ATR line added, token budget unchanged)

```bash
.venv/bin/python -m pytest tests/test_trading_pipeline.py::test_stock_block_is_under_150_tokens -v
```
Expected: `PASSED`

- [ ] **Step 6: Run full suite — no regressions**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 7: Commit**

```bash
git add trading/context_builder.py tests/test_trading_pipeline.py
git commit -m "feat: build_stock_block accepts atr_suggested_tp_pct for Claude context"
```

---

## Task 3: `_ask_claude_batch` injects ATR TP per symbol + test

**Files:**
- Modify: `trading/claude_caller.py` (around line 104–107)
- Modify: `tests/test_trading_pipeline.py` (append 1 test)

`_ask_claude_batch` builds stock blocks for each symbol in the batch. We add: read multiplier from `snap.params`, compute ATR TP per symbol, pass to `build_stock_block`.

- [ ] **Step 1: Write the failing test — append to `tests/test_trading_pipeline.py`**

```python
@pytest.mark.unit
def test_claude_caller_passes_atr_tp_to_stock_block(monkeypatch):
    """_ask_claude_batch computes atr_suggested_tp_pct and passes it to build_stock_block."""
    from trading.claude_caller import _ask_claude_batch
    from unittest.mock import MagicMock, patch, call
    from indicators.values import IndicatorValues
    import config

    monkeypatch.setattr(config, "ATR_TP_MULT_DEFAULT", 2.0)

    iv = MagicMock(spec=IndicatorValues)
    iv.atr = 2.0
    iv.current_price = 100.0

    snap = MagicMock()
    snap.params = {"atr_tp_multiplier": 2.0}
    snap.research = {}
    snap.regime_info = {}
    snap.fred_cache = {}
    snap.research_meta = {}
    snap.weights = {}

    captured_kwargs = {}

    def fake_build_stock_block(sym, iv_, research, existing_pos, atr_suggested_tp_pct=None):
        captured_kwargs["atr_suggested_tp_pct"] = atr_suggested_tp_pct
        return f"=={sym}== mock block"

    with patch("trading.claude_caller.build_stock_block", side_effect=fake_build_stock_block), \
         patch("trading.claude_caller.risk_manager.check_entry_budget", return_value=True), \
         patch("trading.claude_caller._CLAUDE_DISABLED") as mock_disabled, \
         patch("trading.claude_caller.build_stable_system_context", return_value="stable"), \
         patch("trading.claude_caller.build_dynamic_user_context", return_value="dynamic"):
        mock_disabled.is_set.return_value = False
        mock_ai = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text='{"TEST": {"action": "HOLD", "confidence": 0, "stop_loss_pct": 4.0, "take_profit_pct": 5.0, "reasoning": "test"}}')]
        mock_ai.messages.create.return_value = mock_response
        _ask_claude_batch([("TEST", iv, None)], snap, mock_ai)

    assert captured_kwargs.get("atr_suggested_tp_pct") is not None
    assert abs(captured_kwargs["atr_suggested_tp_pct"] - 4.0) < 0.1  # atr=2, price=100, mult=2 → 4%
```

- [ ] **Step 2: Run test to confirm it fails**

```bash
.venv/bin/python -m pytest tests/test_trading_pipeline.py::test_claude_caller_passes_atr_tp_to_stock_block -v
```
Expected: `FAILED` — `captured_kwargs["atr_suggested_tp_pct"]` is `None` (not yet computed)

- [ ] **Step 3: Update `_ask_claude_batch` in `trading/claude_caller.py`**

Add import near top of file (after existing imports):
```python
from signals.atr_tp import compute_atr_tp_pct
```

Find the `stock_blocks` construction (around line 104–107):
```python
stock_blocks = "\n\n".join(
    build_stock_block(sym, iv, snap.research, existing_pos)
    for sym, iv, existing_pos in batch
)
```

Replace with:
```python
_atr_mult = snap.params.get("atr_tp_multiplier", config.ATR_TP_MULT_DEFAULT)
stock_blocks = "\n\n".join(
    build_stock_block(
        sym, iv, snap.research, existing_pos,
        atr_suggested_tp_pct=compute_atr_tp_pct(iv.atr, iv.current_price, _atr_mult),
    )
    for sym, iv, existing_pos in batch
)
```

- [ ] **Step 4: Run the test — must pass**

```bash
.venv/bin/python -m pytest tests/test_trading_pipeline.py::test_claude_caller_passes_atr_tp_to_stock_block -v
```
Expected: `PASSED`

- [ ] **Step 5: Run full suite — no regressions**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 6: Commit**

```bash
git add trading/claude_caller.py tests/test_trading_pipeline.py
git commit -m "feat: inject atr_suggested_tp_pct into Claude stock block per symbol"
```

---

## Task 4: AUTO_EXECUTE path uses ATR TP + test

**Files:**
- Modify: `trading/pipeline.py` (Phase 4a, around lines 278–318)
- Modify: `tests/test_trading_budget.py` (append 1 test)

The AUTO_EXECUTE decision dict currently hardcodes `"take_profit_pct": config.TAKE_PROFIT_PCT`. We replace it with the ATR-based value computed once per cycle (outside the per-symbol loop).

- [ ] **Step 1: Write the failing test — append to `tests/test_trading_budget.py`**

```python
@pytest.mark.unit
def test_auto_execute_uses_atr_tp(monkeypatch, tmp_path):
    """AUTO_EXECUTE decision dict uses ATR-based TP, not flat config.TAKE_PROFIT_PCT."""
    from trading.pipeline import TradingPipeline
    from trading.cycle_context import CycleSnapshot
    from trading.pre_claude_filter import FilterResult
    from unittest.mock import MagicMock, patch
    import config
    from datetime import datetime

    monkeypatch.setattr(config, "TAKE_PROFIT_PCT", 5.0)
    monkeypatch.setattr(config, "ATR_TP_MULT_DEFAULT", 2.0)
    monkeypatch.setattr(config, "MIN_BUYING_POWER", 100.0)

    # iv with atr=2.0, price=100.0 → compute_atr_tp_pct(2.0, 100.0, 2.0) = 4.0% ≠ 5.0%
    iv = MagicMock()
    iv.composite_score = 65.0; iv.current_price = 100.0; iv.atr = 2.0
    iv.vwap_slope = 0.5; iv.signal = "BUY"; iv.rsi = 55.0

    snap = MagicMock(spec=CycleSnapshot)
    snap.regime = "NORMAL"
    snap.regime_info = {"spy_5day_return": 0.0, "macro_health_score": 0}
    snap.research = {}
    snap.research_meta = {"timestamp": datetime.now(ET).isoformat()}
    snap.intraday_news = {}
    snap.params = {"atr_tp_multiplier": 2.0, "high_vol_position_mult": 1.0,
                   "rsi_buy_max": 72.0, "rsi_buy_max_high_vol": 65.0}
    snap.weights = {}
    snap.fred_cache = {}
    snap.sector_today = {}
    snap.advance_decline_ratio = 1.0

    captured_decisions = []

    def capture_execute_buy(*, decision, **kwargs):
        captured_decisions.append(decision)
        return None

    with patch("trading.pipeline._fetch_symbol_data", return_value=("TEST", iv, None, 0.0)), \
         patch("trading.pipeline.load_open_positions", return_value={}), \
         patch("trading.pipeline.load_daily_closed", return_value=set()), \
         patch("trading.pipeline.pre_claude_filter",
               return_value=FilterResult(decision="AUTO_EXECUTE", reason="composite high")), \
         patch("trading.pipeline.execute_buy", side_effect=capture_execute_buy), \
         patch("trading.pipeline.risk_manager.needs_exit_evaluation", return_value=False), \
         patch("trading.pipeline.telegram_notify"):

        pipeline = TradingPipeline()
        pipeline.run(
            symbols=["TEST"], snap=snap, ai=MagicMock(),
            now=datetime(2026, 5, 18, 11, 0, tzinfo=ET),
            alpaca=MagicMock(), portfolio_value=100_000.0,
            buying_power=10_000.0, can_open=True,
        )

    assert len(captured_decisions) == 1
    assert captured_decisions[0]["take_profit_pct"] != config.TAKE_PROFIT_PCT
    assert abs(captured_decisions[0]["take_profit_pct"] - 4.0) < 0.1
```

- [ ] **Step 2: Run test to confirm it fails**

```bash
.venv/bin/python -m pytest tests/test_trading_budget.py::test_auto_execute_uses_atr_tp -v
```
Expected: `FAILED` — `captured_decisions[0]["take_profit_pct"] == 5.0` (still the flat value)

- [ ] **Step 3: Update `trading/pipeline.py`**

Add import at top of file (after existing imports, with other `signals/` imports):
```python
from signals.atr_tp import compute_atr_tp_pct
```

In the Phase 4a AUTO_EXECUTE loop (currently starting around line 279), add one line **before** the `for symbol, iv in auto_execute:` loop to compute the multiplier once:
```python
_atr_tp_multiplier = snap.params.get("atr_tp_multiplier", config.ATR_TP_MULT_DEFAULT)
```

Inside the loop, find the `execute_buy` call's `decision` dict. Change:
```python
"take_profit_pct": config.TAKE_PROFIT_PCT,
```
To:
```python
"take_profit_pct": compute_atr_tp_pct(iv.atr, iv.current_price, _atr_tp_multiplier),
```

The full updated Phase 4a section should look like:
```python
# ── Phase 4a: Auto-execute (no Claude) ───────────────────────────────
_atr_tp_multiplier = snap.params.get("atr_tp_multiplier", config.ATR_TP_MULT_DEFAULT)
for symbol, iv in auto_execute:
    if not can_open or buying_power < config.MIN_BUYING_POWER:
        continue
    try:
        intra_sent = snap.research.get("__meta__", {}).get("shock_score", 0)
        confidence = _compute_auto_execute_confidence(iv.composite_score, intra_sent)
        pos_data = execute_buy(
            symbol=symbol, iv=iv,
            decision={"action": "BUY", "confidence": confidence,
                      "reasoning": "auto-execute: composite+sentiment threshold met",
                      "stop_loss_pct": config.STOP_LOSS_PCT,
                      "take_profit_pct": compute_atr_tp_pct(iv.atr, iv.current_price, _atr_tp_multiplier),
                      "source": "auto_execute"},
            ...
        )
```

- [ ] **Step 4: Run the test — must pass**

```bash
.venv/bin/python -m pytest tests/test_trading_budget.py::test_auto_execute_uses_atr_tp -v
```
Expected: `PASSED`

- [ ] **Step 5: Run full suite — no regressions**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 6: Commit**

```bash
git add trading/pipeline.py tests/test_trading_budget.py
git commit -m "feat: AUTO_EXECUTE path uses compute_atr_tp_pct instead of flat TAKE_PROFIT_PCT"
```

---

## Task 5: Store `atr_pct` in BUY journal record + test

**Files:**
- Modify: `trading/executor.py` (around lines 177–189)
- Modify: `tests/test_trading_pipeline.py` (append 1 test)

Every BUY `append_trade()` call needs an `atr_pct` field. Phase 3's learning agent filters journal records to those with `atr_pct` set to compute the optimal multiplier. `atr_pct = (iv.atr / iv.current_price) * 100` — the same dollar→percent conversion used in `compute_atr_tp_pct`.

- [ ] **Step 1: Write the failing test — append to `tests/test_trading_pipeline.py`**

```python
@pytest.mark.unit
def test_journal_stores_atr_pct(monkeypatch, tmp_path):
    """BUY journal record must include atr_pct for Phase 3 learning agent."""
    from trading.executor import execute_buy
    from signals.entry_gates import GateResult
    from unittest.mock import MagicMock, patch
    from tests.test_signals import make_full_iv
    import config

    monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "pos.json")
    monkeypatch.setattr(config, "DAILY_CLOSED_FILE", tmp_path / "closed.json")
    monkeypatch.setattr("notifications.telegram.notify_buy", lambda **kw: None)
    monkeypatch.setattr("notifications.telegram.notify_daily_loss_warning", lambda *a: None)

    # atr=2.0, price=100.0 → atr_pct = 2.0%
    iv = make_full_iv(symbol="AAPL", composite_score=60.0, rsi=55.0, stoch_d=50.0,
                       current_price=100.0, atr=2.0)

    captured = []
    gate = GateResult(passed=True, reason="", flags=())

    with patch("trading.executor.check_all_gates", return_value=gate), \
         patch("trading.executor.get_intraday_bars", return_value=None), \
         patch("trading.executor.load_json", return_value={}), \
         patch("trading.executor.get_active_mode", return_value="DAY_TRADE"), \
         patch("trading.executor._MODE_POSITION_LIMITS", {}), \
         patch("trading.executor.compute_position_size_for_mode", return_value=10), \
         patch("trading.executor.apply_regime_multiplier", return_value=10), \
         patch("trading.executor.apply_sentiment_multiplier", return_value=10), \
         patch("trading.executor.get_sl_tp_for_mode_and_regime", return_value=(4.0, 5.0)), \
         patch("trading.executor.append_trade", side_effect=lambda d: captured.append(d)), \
         patch("trading.executor.save_json"), \
         patch("trading.executor.risk_manager.correct_bracket_after_fill", return_value=None), \
         patch("trading.executor.telegram_notify"), \
         patch("trading.executor._POSITIONS_LOCK"):
        mock_alpaca = MagicMock()
        mock_alpaca.place_bracket_order.return_value = {
            "id": "ord1", "stop_loss_price": 96.0, "take_profit_price": 105.0,
        }
        execute_buy(
            symbol="AAPL", iv=iv,
            decision={"action": "BUY", "confidence": 85, "stop_loss_pct": 4.0,
                      "take_profit_pct": 5.0, "source": "auto_execute"},
            regime="NORMAL", regime_info={"regime": "NORMAL", "spy_5day_return": 0.0},
            research={}, open_positions={}, alpaca=mock_alpaca,
            portfolio_value=100_000.0, pos_mult=1.0,
            spy_ret5d=0.0, symbol_ret5d=0.0, rsi_max=72.0,
            params={"vwap_slope_min": -0.5},
            now=datetime.now(ET).replace(hour=11),
            entries_this_cycle=0,
        )

    assert len(captured) == 1
    assert "atr_pct" in captured[0]
    assert abs(captured[0]["atr_pct"] - 2.0) < 0.01
```

- [ ] **Step 2: Run test to confirm it fails**

```bash
.venv/bin/python -m pytest tests/test_trading_pipeline.py::test_journal_stores_atr_pct -v
```
Expected: `FAILED` — `KeyError: 'atr_pct'` (field not yet in journal record)

- [ ] **Step 3: Update `append_trade()` call in `trading/executor.py`**

Find the `append_trade({...})` call (around lines 177–189). Add one key to the dict after `"vwap_slope": iv.vwap_slope`:

```python
"atr_pct": round((iv.atr / iv.current_price) * 100, 2) if iv.current_price > 0 else 0.0,
```

The full updated `append_trade` call:
```python
append_trade({
    "type": "BUY", "symbol": symbol, "timestamp": now.isoformat(),
    "price": round(iv.current_price, 4), "qty": qty,
    "composite_score": iv.composite_score, "indicators": asdict(iv),
    "sentiment_score": research.get(symbol, {}).get("sentiment_score"),
    "claude_reasoning": decision.get("reasoning", ""), "claude_confidence": decision.get("confidence", 0),
    "indicator_signals": decision.get("indicator_signals", {}),
    "stop_loss_pct": sl_pct, "take_profit_pct": tp_pct,
    "stop_loss_price": corrected["stop_loss_price"] if corrected else order["stop_loss_price"],
    "take_profit_price": corrected["take_profit_price"] if corrected else order["take_profit_price"],
    "vix_regime": regime, "mode_at_entry": mode, "vix_level": regime_info.get("vix_current"),
    "vwap_slope": iv.vwap_slope,
    "atr_pct": round((iv.atr / iv.current_price) * 100, 2) if iv.current_price > 0 else 0.0,
    "pnl_pct": None, "pnl_dollar": None,
})
```

- [ ] **Step 4: Run the test — must pass**

```bash
.venv/bin/python -m pytest tests/test_trading_pipeline.py::test_journal_stores_atr_pct -v
```
Expected: `PASSED`

- [ ] **Step 5: Run full suite — no regressions**

```bash
.venv/bin/python -m pytest tests/ -m unit -q
```

- [ ] **Step 6: Commit**

```bash
git add trading/executor.py tests/test_trading_pipeline.py
git commit -m "feat: store atr_pct in BUY journal record for Phase 3 learning agent"
```

---

## Self-Review

**Spec coverage:**
- ✓ `trading/pipeline.py` — `compute_atr_tp_pct()` injected into AUTO_EXECUTE path — Task 4
- ✓ Claude prompt context — ATR suggestion in `build_stock_block` — Tasks 2 + 3
- ✓ `trading/executor.py` — `atr_pct` in BUY journal record — Task 5
- ✓ `data/state/trading_params.json` — `atr_tp_multiplier: 2.0` — Task 1
- ✓ Test: `test_atr_tp_injected_into_claude_context` — Task 2
- ✓ Test: `test_claude_caller_passes_atr_tp_to_stock_block` — Task 3 (additional; useful for regression)
- ✓ Test: `test_auto_execute_uses_atr_tp` — Task 4
- ✓ Test: `test_journal_stores_atr_pct` — Task 5

**Gate preservation:** No entry gate, safety rule, or `check_all_gates()` logic is modified. The ATR TP is injected as context and as the TP price — the entry decision pipeline is untouched.

**Backward compatibility:** `build_stock_block`'s new 5th param has a default of `None` — all existing call sites (exit block, tests) continue to work without changes.

**Type consistency:**
- `compute_atr_tp_pct(atr: float, price: float, multiplier: float) -> float` — used in Tasks 3, 4
- `atr_suggested_tp_pct: float | None = None` — param in `build_stock_block`; matches Task 3 call site
- `atr_pct: float` — journal field; Tasks 5 + Phase 3 spec requirement
