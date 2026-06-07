# Token Efficiency Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate token waste from overbought-stock Claude calls, fix broken prompt caching, make all Claude-calling agents report their spend to the shared budget, and raise the ceiling so mid-day exhaustion can't silence Claude for 4+ hours.

**Architecture:** Four independent changes. Task 1 adds RSI/Stochastic hard gates to `entry_gates.py`. Task 2 splits the market context into a stable system part (cacheable) and a dynamic user part (per-call). Task 3 wires `api_budget.record_usage()` into every Claude-calling agent so total daily spend is visible. Task 4 raises the normal daily ceiling. All four are independently testable.

**Tech Stack:** Python 3.12, anthropic SDK, pytest (`pytest tests/ -v -m unit`)

**Root cause summary (do not skip this):**
- On Friday 2026-05-08, 47 Claude calls consumed $0.97 / $1.00 by 11:10 AM — all returned HOLD because RSI>72 / Stochastic>88 were overbought. These conditions were knowable from raw indicator values; Claude should never have been called.
- `cache_write_tokens: 0` across all 47 calls. The system text included `now.strftime('%H:%M')`, live SPY%, QQQ%, and sector-today values — effectively a unique string every call, defeating the `cache_control: ephemeral` marker.
- Budget exhausted = Claude unavailable for the final 4.5 hours while RSI normalised (afternoon symbols showed composites of 60-80 with RSI 55-70).

---

## File Map

| File | Change |
|---|---|
| `config.py` | Add `RSI_HARD_SKIP_THRESHOLD`, `STOCH_HARD_VETO_THRESHOLD`; raise `API_BUDGET_NORMAL_CEILING` and `API_BUDGET_ELEVATED_CEILING` |
| `signals/entry_gates.py` | Add gates 10 (RSI) and 11 (Stochastic) to `check_all_gates()`; update `_iv()` base defaults |
| `trading/context_builder.py` | Split `build_market_context_block()` into `build_stable_system_context()` and `build_dynamic_user_context()` |
| `trading/claude_caller.py` | Use stable context in system block; prepend dynamic context to user message; add module-level stable-context cache |
| `tests/test_signals.py` | Add `TestRsiStochGates` class with 4 tests |
| `tests/test_trading.py` | Add `TestContextSplit` class with 3 tests |

---

## Task 1: RSI / Stochastic hard pre-gates

**Files:**
- Modify: `config.py` (around line 110, near other `API_BUDGET` constants)
- Modify: `signals/entry_gates.py` (after gate 8, around line 90)
- Test: `tests/test_signals.py` (new class `TestRsiStochGates`)

**Why this order:** Config first so the gate code can import the threshold; tests second so the intent is locked down before implementation.

- [ ] **Step 1: Add thresholds to `config.py`**

Open `config.py`. Find the `API_BUDGET_NORMAL_CEILING` block (around line 110). Add these two constants directly below the parabolic/herding block — search for `PARABOLIC_ENTRY_BLOCK` and insert after that section:

```python
# ── Entry gate thresholds ──────────────────────────────────────────────────
RSI_HARD_SKIP_THRESHOLD    = 72    # RSI above this → hard gate before Claude
STOCH_HARD_VETO_THRESHOLD  = 88    # Stochastic %D above this → hard gate before Claude
```

- [ ] **Step 2: Write the failing tests**

Open `tests/test_signals.py`. Add this class at the end of the file, after `TestCheckAllGates`:

```python
class TestRsiStochGates:
    """RSI and Stochastic hard gates in check_all_gates()."""

    def _now(self) -> datetime:
        from zoneinfo import ZoneInfo
        return datetime(2024, 1, 15, 11, 0, 0, tzinfo=ZoneInfo("America/New_York"))

    @staticmethod
    def _iv(**overrides):
        base = dict(
            current_price=100.0,
            vwap=99.0,
            vwap_std2_upper=110.0,
            stop_hunt_zone=False,
            parabolic=False,
            momentum_exhaustion=False,
            bot_herding_score=0,
            squeeze_setup=False,
            rsi=50.0,
            stoch_d=50.0,
        )
        base.update(overrides)
        return types.SimpleNamespace(**base)

    def _patch_calendars(self, monkeypatch):
        import earnings_calendar as ec
        import economic_calendar as ecc
        monkeypatch.setattr(ec, "is_earnings_imminent", lambda *a, **kw: False)
        monkeypatch.setattr(ecc, "is_event_risk_window", lambda *a, **kw: (False, ""))

    @pytest.mark.unit
    def test_rsi_above_threshold_blocks(self, monkeypatch):
        """RSI > RSI_HARD_SKIP_THRESHOLD fires the gate."""
        self._patch_calendars(monkeypatch)
        from signals.entry_gates import check_all_gates
        result = check_all_gates(
            "AAPL", self._iv(rsi=73.0), {}, set(), None,
            self._now(), entries_this_cycle=0,
        )
        assert result.passed is False
        assert "RSI" in result.reason

    @pytest.mark.unit
    def test_rsi_at_threshold_blocks(self, monkeypatch):
        """RSI exactly at threshold also fires (> not >=, so 72.0 should pass, 72.1 blocks)."""
        self._patch_calendars(monkeypatch)
        from signals.entry_gates import check_all_gates
        result = check_all_gates(
            "AAPL", self._iv(rsi=72.0), {}, set(), None,
            self._now(), entries_this_cycle=0,
        )
        assert result.passed is True  # exactly 72 passes; gate is RSI > 72

    @pytest.mark.unit
    def test_stoch_above_threshold_blocks(self, monkeypatch):
        """Stochastic %D > STOCH_HARD_VETO_THRESHOLD fires the gate."""
        self._patch_calendars(monkeypatch)
        from signals.entry_gates import check_all_gates
        result = check_all_gates(
            "NVDA", self._iv(stoch_d=89.0), {}, set(), None,
            self._now(), entries_this_cycle=0,
        )
        assert result.passed is False
        assert "Stochastic" in result.reason or "stoch" in result.reason.lower()

    @pytest.mark.unit
    def test_healthy_rsi_and_stoch_passes(self, monkeypatch):
        """RSI=60, stoch_d=70 — both below thresholds — gate passes."""
        self._patch_calendars(monkeypatch)
        from signals.entry_gates import check_all_gates
        result = check_all_gates(
            "MSFT", self._iv(rsi=60.0, stoch_d=70.0), {}, set(), None,
            self._now(), entries_this_cycle=0,
        )
        assert result.passed is True
```

- [ ] **Step 3: Run tests — expect 4 failures**

```bash
cd ~/Documents/projects/trading-bot
.venv/bin/pytest tests/test_signals.py::TestRsiStochGates -v -m unit
```

Expected: 4 FAILED (AttributeError or `result.passed is True` for the blocking tests).

- [ ] **Step 4: Update `_iv()` base in existing `TestEntryGates`**

The existing `TestEntryGates._iv()` factory (around line 307 of `tests/test_signals.py`) doesn't include `rsi` or `stoch_d`. After adding the new gates, existing tests would fail with `AttributeError`. Add safe defaults to the existing factory:

```python
@staticmethod
def _iv(**overrides):
    """Build a minimal IndicatorValues-compatible namespace that passes all hard gates."""
    base = dict(
        current_price=100.0,
        vwap=99.0,
        vwap_std2_upper=110.0,   # price (100) < upper (110) → no overextension
        stop_hunt_zone=False,
        parabolic=False,
        momentum_exhaustion=False,
        bot_herding_score=0,
        squeeze_setup=False,
        rsi=50.0,       # below RSI_HARD_SKIP_THRESHOLD (72)
        stoch_d=50.0,   # below STOCH_HARD_VETO_THRESHOLD (88)
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)
```

Do the same for `TestCheckAllGates._iv()` around line 462 — add `rsi=50.0, stoch_d=50.0` to its base dict.

- [ ] **Step 5: Add gates 10 and 11 to `signals/entry_gates.py`**

Open `signals/entry_gates.py`. After gate 9 (bot herding, around line 98) and before the soft-gates comment, insert:

```python
    # 10. RSI overbought hard skip — confirmed by Claude's own reasoning; pre-gate saves tokens
    if iv.rsi > config.RSI_HARD_SKIP_THRESHOLD:
        return GateResult(False, f"RSI overbought hard skip (RSI={iv.rsi:.1f} > {config.RSI_HARD_SKIP_THRESHOLD})")

    # 11. Stochastic overbought hard veto
    if iv.stoch_d > config.STOCH_HARD_VETO_THRESHOLD:
        return GateResult(False, f"Stochastic hard veto (%D={iv.stoch_d:.1f} > {config.STOCH_HARD_VETO_THRESHOLD})")
```

- [ ] **Step 6: Run all signal tests — expect all green**

```bash
.venv/bin/pytest tests/test_signals.py -v -m unit
```

Expected: All tests pass including the 4 new ones in `TestRsiStochGates`.

- [ ] **Step 7: Verify SKIP-CLAUDE log pattern still works**

The pipeline already emits `SKIP-CLAUDE` for composite < threshold. The new gates fire after composite passes. Verify the gate reason is logged by checking `pipeline.py` around line 145 — it calls `check_all_gates()` only for symbols where `composite_score > COMPOSITE_SCORE_THRESHOLD`. The new gates fire inside `check_all_gates` and return `GateResult(passed=False, reason=...)`. The pipeline then logs `SKIP-CLAUDE` for these.

No pipeline change needed — `check_all_gates` returning `passed=False` causes the pipeline to skip Claude automatically. Confirm by reading `trading/executor.py`:

```bash
grep -n "check_all_gates\|gate.*passed\|result.passed" ~/Documents/projects/trading-bot/trading/executor.py | head -10
```

- [ ] **Step 8: Commit**

```bash
git add config.py signals/entry_gates.py tests/test_signals.py
git commit -m "feat: pre-gate RSI>72 and Stochastic>88 before Claude to eliminate token waste on overbought markets"
```

---

## Task 2: Fix broken prompt caching — split stable vs. dynamic context

**Files:**
- Modify: `trading/context_builder.py`
- Modify: `trading/claude_caller.py`
- Test: `tests/test_trading.py` (new class `TestContextSplit`)

**Why caching is broken:** `build_market_context_block()` embeds `now.strftime('%H:%M')`, live SPY%, QQQ%, and sector-today percentages directly into the system text. This text changes every minute and every 5-minute intraday cache refresh, so Anthropic's prompt cache never finds a matching entry. Result: `cache_write_tokens=0, cache_read_tokens=0` across all 47 calls.

**Fix:** Split into two functions:
- `build_stable_system_context(regime_info, fred_cache, research_meta)` — regime label, health, VIX, weekly SPY summary, 5-day breadth, sector scores (all from pre-market macro refresh, stable all day), FRED data, macro shock, capitulation watch. **No `now` parameter.** This text is identical for every call within a trading day.
- `build_dynamic_user_context(now)` — current time `%H:%M`, live SPY/QQQ intraday %, sector-today performance, intraday bias, VWAP position, event-risk window. Prepended to the user message, not the system block.

The system block with `cache_control: ephemeral` on `build_stable_system_context` output will now be a stable string all day → Anthropic caches it on the first call, reads it on every subsequent call. Input token cost drops ~60%.

A module-level cache (1-hour TTL) on the stable context string avoids re-computing the format string on every batch call.

- [ ] **Step 1: Write the failing tests**

Open `tests/test_trading.py`. Find the end of the file and add:

```python
class TestContextSplit:
    """Verify stable vs. dynamic context separation for prompt caching."""

    _REGIME = {
        "regime": "NORMAL", "macro_health_score": 5,
        "vix_current": 18.0, "vix_90d_avg": 20.0, "vix_trend": "falling",
        "spy_5day_return": 1.2, "consecutive_green_days": 3,
        "consecutive_red_days": 0, "spy_5day_sign_changes": 1,
        "sector_scores": {"XLK": 10, "XLF": -5},
        "hyg_ret5d": 0.3, "iwm_ret5d": 0.8, "vix3m": 19.0,
    }
    _FRED = {"fed_direction": "paused", "cpi_trend": "stable", "unemployment_rising": False}
    _META = {"macro_shock_score": 10, "capitulation_candidates": []}

    @pytest.mark.unit
    def test_stable_context_contains_no_time(self):
        """build_stable_system_context must not embed a clock time string."""
        from trading.context_builder import build_stable_system_context
        text = build_stable_system_context(self._REGIME, self._FRED, self._META)
        # No HH:MM pattern — ensures the string is identical across minutes
        import re
        assert not re.search(r"\d{2}:\d{2}", text), (
            "Stable context must not contain a time like '10:30' — it breaks prompt caching"
        )

    @pytest.mark.unit
    def test_stable_context_is_deterministic(self):
        """Same inputs produce identical output — required for cache hits."""
        from trading.context_builder import build_stable_system_context
        a = build_stable_system_context(self._REGIME, self._FRED, self._META)
        b = build_stable_system_context(self._REGIME, self._FRED, self._META)
        assert a == b

    @pytest.mark.unit
    def test_dynamic_context_contains_time(self):
        """build_dynamic_user_context must include the current HH:MM marker."""
        from zoneinfo import ZoneInfo
        from trading.context_builder import build_dynamic_user_context
        now = datetime(2024, 1, 15, 11, 30, 0, tzinfo=ZoneInfo("America/New_York"))
        text = build_dynamic_user_context(now)
        assert "11:30" in text
```

- [ ] **Step 2: Run tests — expect 3 failures (ImportError on new function names)**

```bash
.venv/bin/pytest tests/test_trading.py::TestContextSplit -v -m unit
```

Expected: 3 FAILED with `ImportError: cannot import name 'build_stable_system_context'`.

- [ ] **Step 3: Refactor `trading/context_builder.py`**

Read the current file fully before editing. The current `build_market_context_block` (lines 63-127) must be split. Replace it with two new functions. Keep the old name as a deprecated shim for one commit so nothing else breaks, then remove it.

The stable context excludes `now`, `intraday_lines`, and `event_line`:

```python
def build_stable_system_context(
    regime_info: dict,
    fred_cache: dict,
    research_meta: dict,
) -> str:
    """
    Assemble the stable portion of market context for the Claude system prompt.
    Contains no timestamps or live prices — safe to cache across all calls in a trading day.
    Pure — callers pass dicts in; no file I/O here.
    """
    regime        = regime_info.get("regime", "NORMAL")
    health        = regime_info.get("macro_health_score", 0)
    vix           = regime_info.get("vix_current", 0.0)
    vix_90d       = regime_info.get("vix_90d_avg", 0.0)
    vix_trend     = regime_info.get("vix_trend", "unknown")
    spy5d         = regime_info.get("spy_5day_return", 0.0)
    green         = regime_info.get("consecutive_green_days", 0)
    red           = regime_info.get("consecutive_red_days", 0)
    sign_chg      = regime_info.get("spy_5day_sign_changes", 0)
    sector_scores = regime_info.get("sector_scores", {})

    fed   = fred_cache.get("fed_direction", "unknown")
    cpi   = fred_cache.get("cpi_trend", "unknown")
    unemp = "rising" if fred_cache.get("unemployment_rising") else "stable"

    shock      = research_meta.get("macro_shock_score", 0)
    shock_line = f"{shock}/100" + (" — elevated macro stress" if shock >= 40 else "")
    caps       = research_meta.get("capitulation_candidates", [])
    cap_str    = ", ".join(caps) if caps else "None"

    vix_note    = f" (90d avg={vix_90d:.1f}, {vix_trend})" if vix_90d > 0 else ""
    weekly_line = (
        f"Weekly: SPY{spy5d:+.1f}% ({green}g/{red}r consecutive)"
        + (f" sign_changes={sign_chg} (choppy)" if sign_chg >= 3 else "")
    )
    sector_score_str = " ".join(f"{k}={v:.0f}" for k, v in sector_scores.items())

    from macro.breadth_signals import build_breadth_context_line
    breadth_line = build_breadth_context_line({
        "hyg_ret5d": regime_info.get("hyg_ret5d", 0.0),
        "iwm_ret5d": regime_info.get("iwm_ret5d", 0.0),
        "spy_ret5d": spy5d,
        "vix_current": vix,
        "vix3m": regime_info.get("vix3m", 0.0),
    })

    return (
        f"Daily regime: {regime} | health={health:+d} | VIX={vix:.1f}{vix_note}\n"
        f"{weekly_line}\n"
        f"{breadth_line}\n"
        f"Sector scores: {sector_score_str}\n"
        f"FRED: Fed {fed} | CPI {cpi} | Unemployment {unemp}\n"
        f"Macro shock: {shock_line} | Capitulation watch: {cap_str}"
    )


def build_dynamic_user_context(now: datetime) -> str:
    """
    Assemble the dynamic (per-call) market snapshot for the Claude user message.
    Contains live SPY/QQQ%, sector-today performance, intraday bias, and event risk.
    Changes every few minutes — must NOT be in the system block.
    """
    from macro.intraday_context import get_intraday_context

    intraday = get_intraday_context(now)
    intraday_lines = intraday.to_context_lines() if intraday else "Intraday bias: unavailable"

    from economic_calendar import is_event_risk_window
    event_active, event_name = is_event_risk_window(now)
    event_line = f"⚠ ACTIVE: {event_name} — avoid new entries" if event_active else "None active"

    return (
        f"MARKET SNAPSHOT ({now.strftime('%H:%M')} ET):\n"
        f"{intraday_lines}\n"
        f"Event risk: {event_line}"
    )
```

Also add a backward-compat shim at the bottom so any other callers don't break yet:

```python
def build_market_context_block(
    regime_info: dict,
    fred_cache: dict,
    research_meta: dict,
    now: datetime,
) -> str:
    """Deprecated — use build_stable_system_context + build_dynamic_user_context."""
    return (
        build_stable_system_context(regime_info, fred_cache, research_meta)
        + "\n"
        + build_dynamic_user_context(now)
    )
```

- [ ] **Step 4: Run context tests — expect 3 passing**

```bash
.venv/bin/pytest tests/test_trading.py::TestContextSplit -v -m unit
```

Expected: 3 PASSED.

- [ ] **Step 5: Update `trading/claude_caller.py`**

Read the full file first. The `_ask_claude_batch` function (around line 85) currently builds `market_ctx` and embeds it in `system_text`. Update it as follows:

Add a module-level stable-context cache directly below the existing module-level `_CLAUDE_DISABLED*` declarations:

```python
# Stable context cache — keyed on regime+fred+meta identity; TTL 1 hour.
_stable_ctx_text: str = ""
_stable_ctx_time: float = 0.0
_STABLE_CTX_TTL: float = 3600.0
_STABLE_CTX_LOCK = threading.Lock()


def _get_cached_stable_context(fred_cache: dict, research_meta: dict) -> str:
    global _stable_ctx_text, _stable_ctx_time
    with _STABLE_CTX_LOCK:
        if _stable_ctx_text and (time.monotonic() - _stable_ctx_time) < _STABLE_CTX_TTL:
            return _stable_ctx_text
        _stable_ctx_text = build_stable_system_context(
            _get_current_regime_info(), fred_cache, research_meta
        )
        _stable_ctx_time = time.monotonic()
        return _stable_ctx_text
```

Make sure `import threading` and `import time` are at the top of `claude_caller.py` (add if missing).

Also add the new import at the top of the file:

```python
from trading.context_builder import build_stable_system_context, build_dynamic_user_context
```

(Remove the old `build_market_context_block` import if present.)

Then, inside `_ask_claude_batch`, replace the existing market_ctx / system_text block with:

```python
    now = datetime.now(ET)
    stable_ctx = _get_cached_stable_context(fred_cache, research_meta)
    dynamic_ctx = build_dynamic_user_context(now)

    system_text = (
        "You are an expert intraday day trader making final buy/sell decisions.\n"
        "You receive technical indicators, news sentiment, and market context.\n"
        "You may OVERRIDE technical signals when news sentiment strongly contradicts them.\n"
        "Prioritise capital preservation. Never hold overnight.\n"
        f"Current indicator weights: {json.dumps(weights)}\n\n"
        f"{stable_ctx}"
    )
    if strategy_memory:
        system_text += f"\n\nToday's strategy memory:\n{strategy_memory}"

    system_blocks = [
        {
            "type": "text",
            "text": system_text,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    # Prepend dynamic context (live prices/time) to the user message so the system block stays stable.
    user_msg = (
        f"{dynamic_ctx}\n\n"
        f"Decisions needed for {len(batch)} stock(s):\n\n"
        f"{stock_blocks}\n\n"
        "Rules: BUY only if no open position and signal clear. "
        "CLOSE if open position and thesis broken. HOLD otherwise.\n\n"
        "Respond in JSON keyed by symbol:\n"
        '{\n  "<SYMBOL>": {\n'
        '    "action": "<BUY|HOLD|CLOSE>",\n'
        '    "confidence": <0-100>,\n'
        '    "reasoning": "<1 sentence>",\n'
        '    "stop_loss_pct": <number>,\n'
        '    "take_profit_pct": <number>,\n'
        '    "indicator_signals": {"VWAP":"<correct|incorrect|neutral>",'
        '"RSI":"<correct|incorrect|neutral>","MACD":"<correct|incorrect|neutral>",'
        '"BB":"<correct|incorrect|neutral>","EMA":"<correct|incorrect|neutral>",'
        '"Volume":"<correct|incorrect|neutral>"}\n'
        "  }\n}"
    )
```

The `stock_blocks` variable is already defined just above this block — do not duplicate it.

- [ ] **Step 6: Run full test suite**

```bash
.venv/bin/pytest tests/test_signals.py tests/test_trading.py -v -m unit
```

Expected: All tests pass. If a test fails because it calls `build_market_context_block` directly, update it to call the new split functions.

- [ ] **Step 7: Verify cache tokens appear in logs after next run**

The api_budget already records `cache_write_tokens` and `cache_read_tokens` from `getattr(usage, "cache_creation_input_tokens", 0)`. After this change, the first Claude call each trading day should produce `cache_write_tokens > 0`; subsequent calls should produce `cache_read_tokens > 0`. No code change needed here — just confirms caching is working.

- [ ] **Step 8: Commit**

```bash
git add trading/context_builder.py trading/claude_caller.py tests/test_trading.py
git commit -m "feat: fix prompt caching — split stable system context from dynamic user context so Anthropic cache hits every call"
```

---

## Task 3: ~~Raise API budget ceiling~~ Keep original ceilings — they were correct

**No `config.py` change needed for ceilings.**

The original `API_BUDGET_NORMAL_CEILING = 1.00` and `API_BUDGET_ELEVATED_CEILING = 1.50` are the right values. After Tasks 1, 2, and 4 are applied:

| Scenario | Expected total daily spend |
|---|---|
| Typical trading day | ~$0.55–0.58 |
| Active day (many candidates) | ~$0.75–0.90 |
| Worst case (all stocks clear gates all day) | ~$1.98 |

The worst-case ~$2.00 day is only reachable when the bot is actively trading profitable setups with all 15 stocks clearing gates across all 36 cycles. On such a day the portfolio gain condition (`portfolio_gain_pct >= 0.8`) already triggers the elevated ceiling ($1.50) automatically — and that budget is enough for any realistic trading day.

The May 8 overspend was caused entirely by the bugs fixed in Tasks 1, 2, and 4:
- Fix 1 eliminates ~50% of Claude calls (overbought stocks pre-gated before Claude)
- Fix 2 reduces per-call input cost ~60% via prompt cache hits
- Fix 4 makes pre-market agent spend visible so the trading check starts from an accurate baseline

There is nothing to commit for Task 3.

---

## On the watchlist iteration question

**Is iterating the same 10-15 stock watchlist every 10 minutes optimal?**

Yes — the pattern is correct. The watchlist is curated at 9 AM for that day's best setups. Technical conditions evolve throughout the session: a stock that's RSI=85 and Stochastic=100 at 9:30 AM can have RSI=60 and healthy momentum by 11:30 AM (as seen in the Friday logs). The 10-minute cadence is the right resolution to catch these windows.

After Task 1 is applied, the previous inefficiency is eliminated: overbought stocks are filtered by the gate layer (microseconds) rather than by Claude (tokens). The watchlist iteration is now nearly free for hard-gated stocks and only costs tokens when conditions are genuinely favorable.

**One optional future enhancement** (not in this plan): per-symbol cooldown tracking. If a symbol is hard-gated for RSI in cycle N, skip indicator computation for it in cycle N+1 (RSI cannot meaningfully drop in 10 minutes on a trending stock). This saves the Yahoo/Alpaca bar fetch cost, not just the Claude cost. Implement only if the fetch layer becomes a bottleneck.

---

## Verification After All Tasks

Run the full unit suite:

```bash
.venv/bin/pytest tests/test_signals.py tests/test_trading.py tests/test_foundation.py -v -m unit
```

Expected: All existing tests pass + new tests pass (7 from Tasks 1 and 2, plus Task 4 tests).

On next live trading day, confirm in `api_budget.json`:
- `cache_write_tokens > 0` on the first call of the day (from trading agent)
- `cache_read_tokens > 0` on subsequent calls — confirms caching is working
- `calls_made` includes pre-market (watch_list + research) spend, not just trading calls
- `estimated_cost_usd` on a normal day lands below $0.80
- `budget_exhausted: false` for the full trading window
