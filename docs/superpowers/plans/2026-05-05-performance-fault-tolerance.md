# Performance, Fault Tolerance & Claude Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the timedelta crash bug, parallelize all network I/O, send full macro/FRED/economic/PEAD context to Claude in one cached block, add retry to every HTTP fetch, consolidate the legacy vix_regime module, notify Telegram on degraded integrations, and enforce 30-day data hygiene throughout.

**Architecture:** Seven independent tasks, each touching one concern. New pure functions (`_build_market_context_block`, `_fetch_with_retry`, `_log_capability_summary`, `load_trading_params`) are atomic — single responsibility, no file I/O inside, trivially testable. Parallelism uses `ThreadPoolExecutor` scoped within each function call to avoid shared state. All agents consume from `macro_regime.py` as the single source of truth.

**Tech Stack:** Python 3.11+, `concurrent.futures.ThreadPoolExecutor`, `anthropic` SDK prompt caching, `requests`, APScheduler, existing `telegram_notify._send()` pattern.

---

## File Map

| File | Action | What changes |
|---|---|---|
| `trading_agent.py` | Modify | Fix `timedelta` import; add `_build_market_context_block()`; PEAD line in `_build_stock_block()`; parallel Phase 1; staleness warning in `load_research()` |
| `research_agent.py` | Modify | Add `_fetch_with_retry()`; parallel Reddit via `ThreadPoolExecutor`; dedup `_fetch_market_news()`; remove noisy per-symbol log |
| `macro_regime.py` | Modify | Add `load_trading_params()` (moved from `vix_regime.py`) |
| `vix_regime.py` | Delete | Callers migrated to `macro_regime` |
| `learning_agent.py` | Modify | Import `load_macro_regime, load_trading_params` from `macro_regime` |
| `telegram_notify.py` | Modify | Add `notify_degraded_integrations(items: list[str])` |
| `main.py` | Modify | Add `_log_capability_summary()` with Telegram degradation alert; prune stale `research_report.json` in `_cleanup_stale_files()` |
| `config.py` | Modify | Update 4 constants; remove `VIX_REGIME_FILE` |
| `tests/test_foundation.py` | Modify | Add unit tests for all new pure functions |

---

## Task 1: Fix timedelta import + update config constants

**Files:**
- Modify: `trading_agent.py:6`
- Modify: `config.py:44,175,176`
- Modify: `main.py:26-31` (log rotation bytes)

No new tests needed — these are value changes. Verify the journal archival function is reachable.

- [ ] **Step 1: Fix timedelta import in trading_agent.py**

Line 6, change:
```python
from datetime import datetime
```
to:
```python
from datetime import datetime, timedelta
```

- [ ] **Step 2: Update data-hygiene constants in config.py**

Change these four lines:
```python
# Before (lines ~44, 175, 176, and main.py log handler):
STRATEGY_MEMORY_MAX_CHARS = 4000
TRADE_JOURNAL_MAX_DAYS  = 90
CORRUPT_BACKUP_MAX_DAYS = 7
# main.py: maxBytes=10 * 1024 * 1024
```

```python
# After:
STRATEGY_MEMORY_MAX_CHARS = 3500   # tighter — still above 1024-token cache minimum
TRADE_JOURNAL_MAX_DAYS  = 30       # faster journal reads; archive keeps full history
CORRUPT_BACKUP_MAX_DAYS = 3        # .corrupt.json backups useless after 3 days
```

- [ ] **Step 3: Halve log rotation size in main.py**

Find the `RotatingFileHandler` call (around line 27) and change `maxBytes`:
```python
_file_handler = logging.handlers.RotatingFileHandler(
    config.BASE_DIR / "trading_bot.log",
    maxBytes=5 * 1024 * 1024,   # was 10 MB
    backupCount=5,
    encoding="utf-8",
)
```

- [ ] **Step 4: Verify archive function is importable**

```bash
cd /Users/rajeshkumar/Documents/projects/trading-bot
.venv/bin/python3 -c "from trading_agent import archive_old_journal_entries; print('OK')"
```
Expected: `OK` (no ImportError about timedelta).

- [ ] **Step 5: Commit**

```bash
git add trading_agent.py config.py main.py
git commit -m "fix: timedelta import crash + tighten data-hygiene constants"
```

---

## Task 2: Consolidate vix_regime.py → macro_regime.py

`vix_regime.py` contains two functions. `load_vix_regime()` reads the stale `vix_regime.json` (superseded by `macro_regime.json`). `load_trading_params()` reads `trading_params.json` and has no VIX dependency — it belongs in `macro_regime.py`. After this task, `vix_regime.py` is deleted and both agents consume from one source.

**Files:**
- Modify: `macro_regime.py` (add `load_trading_params`)
- Modify: `trading_agent.py` (update import)
- Modify: `learning_agent.py` (update imports, swap `load_vix_regime` → `load_macro_regime`)
- Modify: `config.py` (remove `VIX_REGIME_FILE`)
- Delete: `vix_regime.py`
- Test: `tests/test_foundation.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_foundation.py`:

```python
@pytest.mark.unit
class TestLoadTradingParamsInMacroRegime:
    def test_returns_config_defaults_when_file_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.TRADING_PARAMS_FILE", tmp_path / "trading_params.json")
        from macro_regime import load_trading_params
        params = load_trading_params()
        import config as cfg
        assert params["entry_start_hour"] == cfg.ENTRY_START_HOUR
        assert params["rsi_buy_max"] == cfg.RSI_BUY_MAX
        assert params["stop_loss_pct"] == cfg.STOP_LOSS_PCT

    def test_file_values_override_defaults(self, tmp_path, monkeypatch):
        params_file = tmp_path / "trading_params.json"
        params_file.write_text('{"rsi_buy_max": 60, "entry_start_hour": 11}')
        monkeypatch.setattr("config.TRADING_PARAMS_FILE", params_file)
        from macro_regime import load_trading_params
        params = load_trading_params()
        assert params["rsi_buy_max"] == 60
        assert params["entry_start_hour"] == 11

    def test_load_macro_regime_used_by_learning_agent(self, tmp_path, monkeypatch):
        """learning_agent must call load_macro_regime, not load_vix_regime."""
        import importlib
        import learning_agent
        # Confirm the import succeeds and load_macro_regime is callable from macro_regime
        from macro_regime import load_macro_regime
        result = load_macro_regime()
        assert "regime" in result
```

- [ ] **Step 2: Run tests — expect failure**

```bash
.venv/bin/pytest tests/test_foundation.py::TestLoadTradingParamsInMacroRegime -v
```
Expected: `FAILED` — `load_trading_params` not yet in `macro_regime`.

- [ ] **Step 3: Add load_trading_params to macro_regime.py**

Append to the bottom of `macro_regime.py` (before `if __name__ == "__main__":`) :

```python
def load_trading_params() -> dict:
    """Return learning-agent-adjusted trading params, falling back to config defaults."""
    defaults = {
        "entry_start_hour":       config.ENTRY_START_HOUR,
        "entry_end_hour":         config.ENTRY_END_HOUR,
        "rsi_buy_max":            config.RSI_BUY_MAX,
        "rsi_buy_max_high_vol":   config.RSI_BUY_MAX_HIGH_VOL,
        "vwap_slope_min":         config.VWAP_SLOPE_MIN,
        "high_vol_position_mult": config.HIGH_VOL_POSITION_MULT,
        "stop_loss_pct":          config.STOP_LOSS_PCT,
        "take_profit_pct":        config.TAKE_PROFIT_PCT,
    }
    if config.TRADING_PARAMS_FILE.exists():
        try:
            with open(config.TRADING_PARAMS_FILE) as f:
                saved = json.load(f)
            defaults.update(saved)
        except Exception:
            pass
    return defaults
```

- [ ] **Step 4: Update trading_agent.py import**

Line 22, change:
```python
from vix_regime import load_trading_params
```
to:
```python
from macro_regime import load_trading_params
```

- [ ] **Step 5: Update learning_agent.py import and call**

Line 10, change:
```python
from vix_regime import load_vix_regime, load_trading_params
```
to:
```python
from macro_regime import load_macro_regime, load_trading_params
```

Line 165, change:
```python
regime_info     = load_vix_regime()
```
to:
```python
regime_info     = load_macro_regime()
```

- [ ] **Step 6: Remove VIX_REGIME_FILE from config.py**

Delete this line from `config.py` (around line 65):
```python
VIX_REGIME_FILE    = BASE_DIR / "vix_regime.json"
```

- [ ] **Step 7: Delete vix_regime.py**

```bash
git rm vix_regime.py
```

- [ ] **Step 8: Run tests — expect pass**

```bash
.venv/bin/pytest tests/test_foundation.py::TestLoadTradingParamsInMacroRegime -v
```
Expected: `3 passed`.

- [ ] **Step 9: Run full unit suite — no regressions**

```bash
.venv/bin/pytest tests/test_foundation.py -v -m unit 2>&1 | tail -20
```
Expected: all previously passing tests still pass.

- [ ] **Step 10: Commit**

```bash
git add macro_regime.py trading_agent.py learning_agent.py config.py
git commit -m "refactor: consolidate vix_regime into macro_regime — single regime source"
```

---

## Task 3: Research agent — retry, parallel Reddit, dedup, log cleanup

**Files:**
- Modify: `research_agent.py`
- Test: `tests/test_foundation.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_foundation.py`:

```python
@pytest.mark.unit
class TestFetchWithRetry:
    def test_succeeds_on_first_attempt(self):
        from research_agent import _fetch_with_retry
        calls = []
        def fn():
            calls.append(1)
            return "ok"
        result = _fetch_with_retry(fn)
        assert result == "ok"
        assert len(calls) == 1

    def test_retries_on_failure_then_succeeds(self):
        from research_agent import _fetch_with_retry
        calls = []
        def fn():
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("transient")
            return "recovered"
        result = _fetch_with_retry(fn, attempts=3, base_delay=0)
        assert result == "recovered"
        assert len(calls) == 3

    def test_raises_after_all_attempts_exhausted(self):
        from research_agent import _fetch_with_retry
        def fn():
            raise ConnectionError("down")
        with pytest.raises(ConnectionError):
            _fetch_with_retry(fn, attempts=3, base_delay=0)
```

- [ ] **Step 2: Run tests — expect failure**

```bash
.venv/bin/pytest tests/test_foundation.py::TestFetchWithRetry -v
```
Expected: `FAILED` — `_fetch_with_retry` not yet defined.

- [ ] **Step 3: Add _fetch_with_retry to research_agent.py**

Add after the imports block (after line 18 `REQUEST_HEADERS = ...`):

```python
def _fetch_with_retry(fn, *args, attempts: int = 3, base_delay: float = 1.0, **kwargs):
    """Call fn(*args, **kwargs) up to `attempts` times with exponential backoff."""
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception:
            if i == attempts - 1:
                raise
            time.sleep(base_delay * (2 ** i))
```

- [ ] **Step 4: Run tests — expect pass**

```bash
.venv/bin/pytest tests/test_foundation.py::TestFetchWithRetry -v
```
Expected: `3 passed`.

- [ ] **Step 5: Apply retry to _fetch_rss, _fetch_stocktwits, _fetch_x_posts**

In `_fetch_rss`, change the `requests.get` call:
```python
# Before
resp = requests.get(url, headers=REQUEST_HEADERS, timeout=8)
# After
resp = _fetch_with_retry(requests.get, url, headers=REQUEST_HEADERS, timeout=8)
```

In `_fetch_stocktwits`, change the `requests.get` call:
```python
# Before
resp = requests.get(url, headers=REQUEST_HEADERS, timeout=10)
# After
resp = _fetch_with_retry(requests.get, url, headers=REQUEST_HEADERS, timeout=10)
```

In `_fetch_x_posts`, wrap the `client.search_recent_tweets` call:
```python
# Before
response = client.search_recent_tweets(
    query=query,
    max_results=20,
    tweet_fields=["created_at", "public_metrics", "text", "author_id"],
)
# After
response = _fetch_with_retry(
    client.search_recent_tweets,
    query=query,
    max_results=20,
    tweet_fields=["created_at", "public_metrics", "text", "author_id"],
    attempts=2,
    base_delay=0.5,
)
```

- [ ] **Step 6: Parallelize Reddit subreddit calls**

Add this import at the top of `research_agent.py` (with other imports):
```python
from concurrent.futures import ThreadPoolExecutor, as_completed
```

Replace the entire `_fetch_reddit` function:

```python
def _fetch_reddit(symbol: str) -> dict:
    """
    Fetches Reddit posts using the public JSON API.
    Six subreddit searches run in parallel via ThreadPoolExecutor.
    """
    headers = {"User-Agent": "TradingBot/1.0 (sentiment research; read-only)"}

    def _fetch_sub(sub: str) -> list[dict]:
        url = (
            f"https://www.reddit.com/r/{sub}/search.json"
            f"?q={symbol}+OR+%24{symbol}"
            f"&sort=new&t=day&restrict_sr=true&limit=5"
        )
        resp = _fetch_with_retry(requests.get, url, headers=headers, timeout=10)
        resp.raise_for_status()
        children = resp.json().get("data", {}).get("children", [])
        return [
            {
                "subreddit": sub,
                "title": p.get("title", ""),
                "text": (p.get("selftext", "") or "")[:300],
                "score": p.get("score", 0),
                "upvote_ratio": p.get("upvote_ratio", 0),
                "num_comments": p.get("num_comments", 0),
                "url": p.get("url", ""),
            }
            for child in children
            for p in [child.get("data", {})]
        ]

    posts: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(_fetch_sub, sub): sub for sub in REDDIT_SUBREDDITS}
        for future in as_completed(futures):
            sub = futures[future]
            try:
                posts.extend(future.result())
            except Exception as exc:
                logger.debug(f"Reddit r/{sub} fetch failed for {symbol}: {exc}")

    posts.sort(key=lambda p: p["score"], reverse=True)
    return {"available": True, "post_count": len(posts), "posts": posts[:5]}
```

- [ ] **Step 7: Deduplicate _fetch_market_news and remove noisy log**

In `run_research_agent`, find the two calls to `_fetch_market_news()` and merge them into one. The first call is used to build `market_context_block` (around line 437); the second is at line 530 to compute macro shock. Cache it in a local variable.

Replace the section around line 437:
```python
# Before (two separate calls):
market_news = _fetch_market_news()
market_context_block = "\n".join(f"- {n['title']}: {n['summary']}" for n in market_news[:6])
# ... (later, around line 530)
market_news_for_shock = _fetch_market_news()
macro_shock_score = _compute_macro_shock_score(market_news_for_shock)

# After (single fetch, reused):
market_news = _fetch_market_news()
market_context_block = "\n".join(f"- {n['title']}: {n['summary']}" for n in market_news[:6])
# ... (later, around line 530 — use cached market_news)
macro_shock_score = _compute_macro_shock_score(market_news)
```

Also remove the `logger.info(f"  Researching {symbol}…")` line inside the per-symbol loop (this is the noisy preamble; the scores line further down is kept).

- [ ] **Step 8: Run full unit suite**

```bash
.venv/bin/pytest tests/test_foundation.py -v -m unit 2>&1 | tail -20
```
Expected: all tests pass.

- [ ] **Step 9: Commit**

```bash
git add research_agent.py tests/test_foundation.py
git commit -m "perf: parallel Reddit fetches + retry wrapper + dedup market news in research agent"
```

---

## Task 4: Telegram degradation alert + startup capability summary

**Files:**
- Modify: `telegram_notify.py` (add `notify_degraded_integrations`)
- Modify: `main.py` (add `_log_capability_summary`, extend `_cleanup_stale_files`)
- Test: `tests/test_foundation.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_foundation.py`:

```python
@pytest.mark.unit
class TestCapabilitySummary:
    def test_degraded_items_identified_when_keys_missing(self, monkeypatch):
        """_log_capability_summary returns list of degraded integration names."""
        import config as cfg
        monkeypatch.setattr(cfg, "FRED_API_KEY", "")
        monkeypatch.setattr(cfg, "POLYGON_API_KEY", "")
        monkeypatch.setattr(cfg, "EMAIL_SENDER", "user@example.com")
        monkeypatch.setattr(cfg, "TELEGRAM_BOT_TOKEN", "tok")
        monkeypatch.setattr(cfg, "TELEGRAM_CHAT_ID", "123")
        from main import _log_capability_summary
        degraded = _log_capability_summary()
        assert any("FRED" in item for item in degraded)
        assert any("Polygon" in item for item in degraded)
        assert not any("Email" in item for item in degraded)

    def test_no_degraded_when_all_keys_present(self, monkeypatch):
        import config as cfg
        monkeypatch.setattr(cfg, "FRED_API_KEY", "abc")
        monkeypatch.setattr(cfg, "POLYGON_API_KEY", "xyz")
        monkeypatch.setattr(cfg, "EMAIL_SENDER", "user@example.com")
        monkeypatch.setattr(cfg, "TELEGRAM_BOT_TOKEN", "tok")
        monkeypatch.setattr(cfg, "TELEGRAM_CHAT_ID", "123")
        from main import _log_capability_summary
        degraded = _log_capability_summary()
        assert degraded == []
```

- [ ] **Step 2: Run tests — expect failure**

```bash
.venv/bin/pytest tests/test_foundation.py::TestCapabilitySummary -v
```
Expected: `FAILED` — `_log_capability_summary` not yet defined.

- [ ] **Step 3: Add notify_degraded_integrations to telegram_notify.py**

Add after `notify_shutdown`:

```python
def notify_degraded_integrations(items: list[str]) -> None:
    """Send one Telegram message listing degraded integrations at startup."""
    if not items:
        return
    bullet_list = "\n".join(f"  • {item}" for item in items)
    _send(f"<b>⚠️ Bot started with degraded integrations:</b>\n{bullet_list}")
```

- [ ] **Step 4: Add _log_capability_summary to main.py**

Add this function before `main()` in `main.py`:

```python
def _log_capability_summary() -> list[str]:
    """
    Log one line per integration showing active/degraded status.
    Returns list of degraded item description strings (for Telegram alert).
    Called once at startup after hard key validation.
    """
    degraded: list[str] = []

    # FRED
    if config.FRED_API_KEY:
        try:
            fred_data = json.loads(config.FRED_CACHE_FILE.read_text()) if config.FRED_CACHE_FILE.exists() else {}
            fed = fred_data.get("fed_direction", "unknown")
            cpi = fred_data.get("cpi_trend", "unknown")
            logger.info(f"[✓] FRED         — macro modifier active (Fed {fed}, CPI {cpi})")
        except Exception:
            logger.info("[✓] FRED         — key set, cache not yet built")
    else:
        logger.info("[✗] FRED         — macro regime modifier = 0 (key not set)")
        degraded.append("FRED key missing — macro regime modifier = 0 (neutral)")

    # Polygon
    if config.POLYGON_API_KEY:
        logger.info("[✓] Polygon      — flash-crash dual-source verification active")
    else:
        logger.info("[✗] Polygon      — flash-crash single-source only (SPY two-reading fallback)")
        degraded.append("Polygon key missing — flash-crash confirmation is single-source only")

    # Reddit (always available via public JSON)
    logger.info("[✓] Reddit       — public JSON API, no credentials needed")

    # X / Twitter
    if config.X_BEARER_TOKEN:
        logger.info("[✓] X/Twitter    — tweet sentiment active")
    else:
        logger.info("[~] X/Twitter    — disabled (X_BEARER_TOKEN not set; $100/mo optional)")

    # Email
    if config.EMAIL_SENDER and config.EMAIL_APP_PASSWORD:
        logger.info(f"[✓] Email        — daily report → {config.EMAIL_RECIPIENT or config.EMAIL_SENDER}")
    else:
        logger.info("[✗] Email        — daily report will not be sent (EMAIL_SENDER not configured)")
        degraded.append("Email not configured — daily P&L report will not be sent")

    # Telegram (self-referential — if we can log this, it works)
    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
        logger.info("[✓] Telegram     — notifications active")
    else:
        logger.info("[~] Telegram     — disabled (TELEGRAM_BOT_TOKEN not set)")

    # Market data provider
    provider = config.MARKET_DATA_PROVIDER
    logger.info(f"[~] Market data  — provider={provider}")

    return degraded
```

- [ ] **Step 5: Call _log_capability_summary in main() and send Telegram alert**

In `main()`, after the hard key checks (after line `if not config.ANTHROPIC_API_KEY: sys.exit(1)`), add:

```python
degraded_integrations = _log_capability_summary()
if degraded_integrations:
    telegram_notify.notify_degraded_integrations(degraded_integrations)
```

- [ ] **Step 6: Prune stale research_report.json in _cleanup_stale_files**

In `_cleanup_stale_files()` in `main.py`, after the `for f in config.BASE_DIR.glob("*.tmp"):` block, add:

```python
# Prune research_report.json if it is from a prior trading day.
# research_agent rewrites the full file each run; a stale file from yesterday
# means yesterday's symbol scores would be used for today's watch list.
try:
    if config.RESEARCH_REPORT_FILE.exists():
        import json as _json
        rr = _json.loads(config.RESEARCH_REPORT_FILE.read_text())
        meta_ts = rr.get("__meta__", {}).get("timestamp", "")
        if meta_ts:
            report_dt = datetime.fromisoformat(meta_ts)
            if report_dt.tzinfo is None:
                report_dt = report_dt.replace(tzinfo=ET)
            today_start = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0)
            if report_dt < today_start:
                config.RESEARCH_REPORT_FILE.unlink()
                logger.info("Deleted stale research_report.json (from prior trading day)")
except Exception as exc:
    logger.debug(f"research_report.json prune skipped: {exc}")
```

- [ ] **Step 7: Run tests — expect pass**

```bash
.venv/bin/pytest tests/test_foundation.py::TestCapabilitySummary -v
```
Expected: `2 passed`.

- [ ] **Step 8: Run full unit suite**

```bash
.venv/bin/pytest tests/test_foundation.py -v -m unit 2>&1 | tail -20
```
Expected: all tests pass.

- [ ] **Step 9: Commit**

```bash
git add telegram_notify.py main.py tests/test_foundation.py
git commit -m "feat: startup capability summary with Telegram degradation alert + stale research pruning"
```

---

## Task 5: Trading agent — market context block, PEAD line, parallel Phase 1, staleness warning

This is the highest-impact task: Claude gets full macro context in every trading decision.

**Files:**
- Modify: `trading_agent.py`
- Test: `tests/test_foundation.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_foundation.py`:

```python
@pytest.mark.unit
class TestBuildMarketContextBlock:
    def _regime(self, **overrides) -> dict:
        base = {
            "regime": "BULL_TREND",
            "macro_health_score": 32,
            "vix_current": 18.2,
        }
        base.update(overrides)
        return base

    def _fred(self, **overrides) -> dict:
        base = {
            "fed_direction": "cutting",
            "cpi_trend": "falling",
            "unemployment_rising": False,
        }
        base.update(overrides)
        return base

    def _meta(self, **overrides) -> dict:
        base = {
            "macro_shock_score": 0,
            "sector_sentiment": {"AI_SEMI": 28.0, "DATA_CENTER": 15.0},
            "capitulation_candidates": [],
        }
        base.update(overrides)
        return base

    def test_contains_all_six_fields(self, monkeypatch):
        monkeypatch.setattr(
            "economic_calendar.is_event_risk_window", lambda now: (False, "")
        )
        from trading_agent import _build_market_context_block
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime(2026, 5, 5, 10, 30, tzinfo=ZoneInfo("America/New_York"))
        block = _build_market_context_block(self._regime(), self._fred(), self._meta(), now)
        assert "BULL_TREND" in block
        assert "cutting" in block
        assert "falling" in block
        assert "None active" in block
        assert "AI_SEMI" in block
        assert "Capitulation watch" in block

    def test_shock_score_omitted_when_zero(self, monkeypatch):
        monkeypatch.setattr(
            "economic_calendar.is_event_risk_window", lambda now: (False, "")
        )
        from trading_agent import _build_market_context_block
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime(2026, 5, 5, 10, 30, tzinfo=ZoneInfo("America/New_York"))
        block = _build_market_context_block(self._regime(), self._fred(), self._meta(macro_shock_score=0), now)
        assert "0/100" in block
        assert "elevated" not in block

    def test_shock_score_flagged_when_elevated(self, monkeypatch):
        monkeypatch.setattr(
            "economic_calendar.is_event_risk_window", lambda now: (False, "")
        )
        from trading_agent import _build_market_context_block
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime(2026, 5, 5, 10, 30, tzinfo=ZoneInfo("America/New_York"))
        block = _build_market_context_block(
            self._regime(), self._fred(), self._meta(macro_shock_score=55), now
        )
        assert "55/100" in block
        assert "elevated" in block

    def test_active_event_risk_flagged(self, monkeypatch):
        monkeypatch.setattr(
            "economic_calendar.is_event_risk_window",
            lambda now: (True, "Non-Farm Payrolls"),
        )
        from trading_agent import _build_market_context_block
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime(2026, 5, 5, 8, 35, tzinfo=ZoneInfo("America/New_York"))
        block = _build_market_context_block(self._regime(), self._fred(), self._meta(), now)
        assert "Non-Farm Payrolls" in block
        assert "avoid" in block

    def test_capitulation_candidates_listed(self, monkeypatch):
        monkeypatch.setattr(
            "economic_calendar.is_event_risk_window", lambda now: (False, "")
        )
        from trading_agent import _build_market_context_block
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime(2026, 5, 5, 10, 30, tzinfo=ZoneInfo("America/New_York"))
        block = _build_market_context_block(
            self._regime(), self._fred(), self._meta(capitulation_candidates=["SMCI", "PLTR"]), now
        )
        assert "SMCI" in block
        assert "PLTR" in block
```

- [ ] **Step 2: Run tests — expect failure**

```bash
.venv/bin/pytest tests/test_foundation.py::TestBuildMarketContextBlock -v
```
Expected: `FAILED` — `_build_market_context_block` not yet defined.

- [ ] **Step 3: Add _build_market_context_block to trading_agent.py**

Add this function after the `_prefilter_decision` function (around line 213), before `_check_entry_gates`:

```python
def _build_market_context_block(
    regime_info: dict,
    fred_cache: dict,
    research_meta: dict,
    now: datetime,
) -> str:
    """
    Assemble a compact (~150 token) market context string for the Claude system prompt.
    Pure — no file I/O; callers load the dicts and pass them in.
    """
    regime = regime_info.get("regime", "NORMAL")
    health = regime_info.get("macro_health_score", 0)
    vix    = regime_info.get("vix_current", 0.0)

    fed   = fred_cache.get("fed_direction", "unknown")
    cpi   = fred_cache.get("cpi_trend", "unknown")
    unemp = "rising" if fred_cache.get("unemployment_rising") else "stable"

    event_active, event_name = is_event_risk_window(now)
    if event_active:
        event_line = f"⚠ ACTIVE: {event_name} — avoid new entries"
    else:
        event_line = "None active"

    shock = research_meta.get("macro_shock_score", 0)
    shock_line = f"{shock}/100" + (" — elevated macro stress" if shock >= 40 else "")

    sectors = research_meta.get("sector_sentiment", {})
    sector_str = " ".join(f"{s}={v:+.0f}" for s, v in sectors.items()) or "N/A"

    caps = research_meta.get("capitulation_candidates", [])
    cap_str = ", ".join(caps) if caps else "None"

    return (
        f"MARKET CONTEXT ({now.strftime('%H:%M')} ET):\n"
        f"Regime: {regime} | health={health:+d} | VIX={vix:.1f}\n"
        f"FRED: Fed {fed} | CPI {cpi} | Unemployment {unemp}\n"
        f"Event risk: {event_line}\n"
        f"Macro shock: {shock_line}\n"
        f"Sector sentiment: {sector_str}\n"
        f"Capitulation watch: {cap_str}"
    )
```

Also add the import for `is_event_risk_window` — it's already imported at line 17:
```python
from economic_calendar import is_event_risk_window
```
No change needed there.

- [ ] **Step 4: Run tests — expect pass**

```bash
.venv/bin/pytest tests/test_foundation.py::TestBuildMarketContextBlock -v
```
Expected: `5 passed`.

- [ ] **Step 5: Load fred_cache.json in run_trading_cycle and pass to _ask_claude_batch**

In `run_trading_cycle()` (around line 863, after `regime_info = load_macro_regime()`), add:

```python
# Load FRED cache for Claude context block
try:
    fred_cache = json.loads(config.FRED_CACHE_FILE.read_text()) if config.FRED_CACHE_FILE.exists() else {}
except Exception:
    fred_cache = {}

research_meta = research.get("__meta__", {})
```

Then in `_ask_claude_batch`, add `fred_cache: dict` and `research_meta: dict` parameters and pass them to `_build_market_context_block`.

Full updated signature for `_ask_claude_batch`:
```python
def _ask_claude_batch(
    batch: list[tuple[str, IndicatorValues, dict | None]],
    research: dict,
    strategy_memory: str,
    weights: dict,
    ai: anthropic.Anthropic,
    fred_cache: dict,
    research_meta: dict,
) -> dict[str, dict]:
```

- [ ] **Step 6: Add market context block to Claude system prompt in _ask_claude_batch**

Inside `_ask_claude_batch`, after the existing `system_blocks` definition (around line 424), add the context block as a new system block:

```python
now = datetime.now(ET)
market_ctx = _build_market_context_block(
    _get_current_regime_info(), fred_cache, research_meta, now
)
# Insert as the FIRST system block (before indicator weights) so it qualifies
# for prompt caching together with strategy_memory when total > 1024 tokens.
system_blocks = [
    {
        "type": "text",
        "text": (
            "You are an expert intraday day trader making final buy/sell decisions.\n"
            "You receive technical indicators, news sentiment, and market context.\n"
            "You may OVERRIDE technical signals when news sentiment strongly contradicts them.\n"
            "Prioritise capital preservation. Never hold overnight.\n"
            f"Current indicator weights: {json.dumps(weights)}\n\n"
            f"{market_ctx}"
        ),
    }
]
```

Add a helper to avoid a second `load_macro_regime()` call (it was already called in `run_trading_cycle`):

```python
def _get_current_regime_info() -> dict:
    """Best-effort regime read — used inside _ask_claude_batch."""
    try:
        return load_macro_regime()
    except Exception:
        return {"regime": "NORMAL", "macro_health_score": 0, "vix_current": 0.0}
```

Update the call site in `run_trading_cycle` to pass `fred_cache` and `research_meta`:
```python
batch_decisions = _ask_claude_batch(
    batch, research, strategy_memory, weights, ai, fred_cache, research_meta
)
```

- [ ] **Step 7: Add PEAD line to _build_stock_block**

In `_build_stock_block` (around line 283), add PEAD detection. After the existing `pos_block` computation, add:

```python
pead_ok, pead_score = detect_pead_setup(symbol)
pead_line = (
    f"\n  PEAD: {pead_score}/100 conditions met — expect multi-day drift"
    if pead_ok else ""
)
```

At the end of the return statement, append `pead_line`:
```python
return (
    f"=={symbol}== composite={iv.composite_score:+.1f} signal={iv.signal}{pos_block}\n"
    f"  P=${price:.2f} VWAP=${iv.vwap:.2f}({'↑' if price>=iv.vwap else '↓'})"
    f" RSI={iv.rsi:.1f}(Δ{iv.rsi_slope:+.1f}) MACD={iv.macd_hist:.4f}"
    f" BB=[{iv.bb_lower:.2f},{iv.bb_upper:.2f}] EMA9={iv.ema9:.2f} EMA21={iv.ema21:.2f}"
    f" ATR={iv.atr:.4f} Stoch={iv.stoch_k:.1f}/{iv.stoch_d:.1f} Vol={dvol}\n"
    f"  NEWS: {sentiment_label}({sentiment_score:+d}) catalyst={catalyst} risk={risk_flag}\n"
    f"  {news_summary}"
    f"{pead_line}"
)
```

`detect_pead_setup` is already imported at line 16. No new import needed.

- [ ] **Step 8: Parallelize Phase 1 bar fetching**

Add this import at the top of `trading_agent.py` (with other imports):
```python
from concurrent.futures import ThreadPoolExecutor, as_completed
```

Replace the sequential Phase 1 loop (around line 927–937):

```python
# Before:
stock_data: list[tuple[str, IndicatorValues, dict | None]] = []
for symbol in watch_list:
    try:
        bars = alpaca.get_intraday_bars(symbol)
        daily_bars = alpaca.get_daily_bars(symbol, days=8)
        iv = compute_indicators(symbol, bars, daily_df=daily_bars)
        if iv is None:
            continue
        stock_data.append((symbol, iv, open_positions.get(symbol)))
    except Exception as exc:
        logger.error(f"Error fetching data for {symbol}: {exc}", exc_info=True)
```

```python
# After:
def _fetch_symbol_data(symbol: str) -> tuple[str, IndicatorValues, dict | None] | None:
    bars      = alpaca.get_intraday_bars(symbol)
    daily_bars = alpaca.get_daily_bars(symbol, days=8)
    iv = compute_indicators(symbol, bars, daily_df=daily_bars)
    if iv is None:
        return None
    return (symbol, iv, open_positions.get(symbol))

stock_data: list[tuple[str, IndicatorValues, dict | None]] = []
with ThreadPoolExecutor(max_workers=8) as executor:
    futures = {executor.submit(_fetch_symbol_data, sym): sym for sym in watch_list}
    for future in as_completed(futures):
        sym = futures[future]
        try:
            result = future.result()
            if result is not None:
                stock_data.append(result)
        except Exception as exc:
            logger.error(f"Error fetching data for {sym}: {exc}", exc_info=True)
```

Note: `_fetch_symbol_data` is defined as a nested closure inside `run_trading_cycle` so it captures `alpaca` and `open_positions` from the enclosing scope. This is safe — `open_positions` is read-only at this point and `alpaca` uses stateless HTTP calls.

- [ ] **Step 9: Add research staleness warning to load_research()**

Replace the existing `load_research()` function:

```python
def load_research() -> dict:
    data = _load_json(config.RESEARCH_REPORT_FILE, {})
    symbols = [k for k in data if k != "__meta__"]
    if not symbols:
        logger.warning("Research report is empty — Claude will have no sentiment context this cycle")
        return data
    try:
        ts_str = data.get("__meta__", {}).get("timestamp") or next(
            (data[s].get("timestamp") for s in symbols if data[s].get("timestamp")), None
        )
        if ts_str:
            report_dt = datetime.fromisoformat(ts_str)
            if report_dt.tzinfo is None:
                report_dt = report_dt.replace(tzinfo=ET)
            age_hours = (datetime.now(ET) - report_dt).total_seconds() / 3600
            if age_hours > 6:
                logger.warning(
                    f"Research report is {age_hours:.1f}h old — sentiment may not reflect current conditions"
                )
    except Exception:
        pass
    return data
```

- [ ] **Step 10: Run tests**

```bash
.venv/bin/pytest tests/test_foundation.py -v -m unit 2>&1 | tail -30
```
Expected: all tests pass including the 5 new `TestBuildMarketContextBlock` tests.

- [ ] **Step 11: Commit**

```bash
git add trading_agent.py tests/test_foundation.py
git commit -m "feat: Claude gets full macro/FRED/event/PEAD context; parallel Phase 1 bar fetching"
```

---

## Task 6: Final verification

- [ ] **Step 1: Run the complete unit suite**

```bash
.venv/bin/pytest tests/test_foundation.py -v -m unit
```
Expected output ends with something like:
```
============ N passed in X.XXs ============
```
No failures, no errors.

- [ ] **Step 2: Verify no stray vix_regime imports remain**

```bash
grep -r "vix_regime" /Users/rajeshkumar/Documents/projects/trading-bot/*.py
```
Expected: no output (vix_regime.py is deleted; all callers updated).

- [ ] **Step 3: Smoke-check all module imports**

```bash
cd /Users/rajeshkumar/Documents/projects/trading-bot
.venv/bin/python3 -c "
import trading_agent
import research_agent
import macro_regime
import learning_agent
import main
print('All imports OK')
"
```
Expected: `All imports OK`

- [ ] **Step 4: Final commit**

```bash
git add -A
git status  # confirm only expected files changed
git commit -m "chore: verify all imports clean after vix_regime removal"
```

---

## Self-Review Checklist

**Spec coverage:**
- [x] A1 timedelta import — Task 1 Step 1
- [x] A2 `_build_market_context_block` — Task 5 Steps 3–6
- [x] A3 PEAD line in stock block — Task 5 Step 7
- [x] A4 Parallel Phase 1 — Task 5 Step 8
- [x] A5 Research staleness warning — Task 5 Step 9
- [x] B1 `_fetch_with_retry` — Task 3 Steps 2–4
- [x] B2 Parallel Reddit — Task 3 Step 6
- [x] B3 Deduplicate market_news — Task 3 Step 7
- [x] B4 Remove per-symbol log noise — Task 3 Step 7
- [x] C1 `_log_capability_summary` + Telegram alert — Task 4 Steps 3–5
- [x] D config constants (4 values) — Task 1 Step 2
- [x] D1 Stale research_report.json pruning — Task 4 Step 6
- [x] E1–E4 vix_regime consolidation + deletion — Task 2
- [x] Unit tests for all pure functions — Tasks 2, 3, 4, 5

**Placeholder scan:** No TBDs, no "similar to above", all code blocks complete.

**Type consistency:**
- `_build_market_context_block(regime_info, fred_cache, research_meta, now)` — defined Task 5 Step 3, tested Task 5 Step 1 ✓
- `_fetch_with_retry(fn, *args, attempts, base_delay, **kwargs)` — defined Task 3 Step 3, tested Task 3 Step 1 ✓
- `load_trading_params()` — defined Task 2 Step 3, tested Task 2 Step 1 ✓
- `notify_degraded_integrations(items: list[str])` — defined Task 4 Step 3, called Task 4 Step 5 ✓
- `_log_capability_summary() -> list[str]` — defined Task 4 Step 4, tested Task 4 Step 1 ✓
