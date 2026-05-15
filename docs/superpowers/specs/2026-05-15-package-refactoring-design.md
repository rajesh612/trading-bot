# Package Refactoring Design
*2026-05-15*

## Goals

1. Move 8 root-level Python modules into domain packages per CLAUDE.md Rule #6.
2. Move 15 JSON state files from root into `data/state/`.
3. Move log files from root into `logs/`.
4. Delete confirmed dead code.
5. Update all ~150 import sites and path constants — no re-export shims.

---

## Python Module Moves

| Source | Destination | Package |
|---|---|---|
| `macro_regime.py` | `macro/regime.py` | Existing |
| `economic_calendar.py` | `macro/economic_calendar.py` | Existing |
| `earnings_calendar.py` | `calendars/earnings.py` | New |
| `market_data.py` | `data/market_data.py` | New |
| `swing_manager.py` | `trading/swing_manager.py` | Existing |
| `telegram_notify.py` | `notifications/telegram.py` | New |
| `daily_report.py` | `notifications/daily_report.py` | New |
| `alpaca_client.py` | `broker/alpaca_client.py` | New |

New packages require a bare `__init__.py`: `calendars/`, `data/`, `notifications/`, `broker/`.

No re-export shims. All callers updated directly.

### Dead Code Removed

- `market_data.get_quote()` — defined but has zero callers anywhere in the codebase. Delete during move.

---

## JSON State Files → `data/state/`

`data/state/` is a plain directory (not a Python package — no `__init__.py`).

Files moved:
```
open_positions.json
swing_positions.json
trade_journal.json
daily_closed.json
macro_regime.json
fred_cache.json
economic_calendar.json
earnings_calendar.json
research_report.json
api_budget.json
indicator_weights.json
trading_params.json
intraday_news_cache.json
bot_health.json
daily_report_sent.json
```

`config.py` change — one new constant, 15 path updates:
```python
STATE_DIR = BASE_DIR / "data" / "state"
# All *.json paths change from BASE_DIR / "x.json" to STATE_DIR / "x.json"
```

`STATE_DIR.mkdir(parents=True, exist_ok=True)` called once at startup in `main.py` before any file I/O.

Files that stay at root (source/config, not state):
`tickers.txt`, `daily_tradeable.txt`, `watch_list.txt`, `strategy_memory.md`, `indicator_performance_log.md`

---

## Log Files → `logs/`

| File | New path |
|---|---|
| `trading_bot.log` (+ rotated backups) | `logs/trading_bot.log` |
| `launchd_trading_bot.log` | `logs/launchd_trading_bot.log` |

Three changes:
1. `main.py` — RotatingFileHandler path: `config.BASE_DIR / "logs" / "trading_bot.log"`; `logs/` mkdir before handler creation.
2. `start.sh` — `LOG_FILE="$SCRIPT_DIR/logs/trading_bot.log"`
3. `trading_bot_start.plist` — StandardOutPath/StandardErrorPath updated to `PLACEHOLDER_PROJECT_DIR/logs/launchd_trading_bot.log`

---

## Import Site Strategy

~150 import sites updated by a scripted sed pass per module, then manually verified. Order:

1. Create new packages + `__init__.py` files.
2. Copy each source file to destination.
3. Update internal imports inside moved files (e.g., `macro_regime.py` imports `economic_calendar` → `macro.economic_calendar`).
4. Run sed pass to rewrite all external callers.
5. Delete original root-level files.
6. Run full test suite — must stay green.

### Import Rename Map

| Old import | New import |
|---|---|
| `from macro_regime import …` | `from macro.regime import …` |
| `import macro_regime` | `import macro.regime as macro_regime` |
| `from economic_calendar import …` | `from macro.economic_calendar import …` |
| `import economic_calendar` | `import macro.economic_calendar as economic_calendar` |
| `from earnings_calendar import …` | `from calendars.earnings import …` |
| `import earnings_calendar` | `import calendars.earnings as earnings_calendar` |
| `from market_data import …` | `from data.market_data import …` |
| `import market_data` | `import data.market_data as market_data` |
| `from swing_manager import …` | `from trading.swing_manager import …` |
| `import swing_manager` | `import trading.swing_manager as swing_manager` |
| `from telegram_notify import …` | `from notifications.telegram import …` |
| `import telegram_notify` | `import notifications.telegram as telegram_notify` |
| `from daily_report import …` | `from notifications.daily_report import …` |
| `import daily_report` | `import notifications.daily_report as daily_report` |
| `from alpaca_client import …` | `from broker.alpaca_client import …` |
| `import alpaca_client` | `import broker.alpaca_client as alpaca_client` |

---

## CLAUDE.md Rule #6 Update

Add to the domain package table:

| Domain | Package |
|---|---|
| Broker I/O, order routing | `broker/` |
| Earnings dates, PEAD detection | `calendars/` |
| Market data, OHLCV providers | `data/` |
| Telegram + email notifications | `notifications/` |

---

## Test File Impact

Tests import by old module paths. Same sed pass covers test files. No test logic changes — only import paths. All tests must pass after the rename.

`daily_report.DAILY_REPORT_SENT_FILE` module-level attribute is patched directly by several tests using `monkeypatch.setattr(daily_report, "DAILY_REPORT_SENT_FILE", ...)` — this continues to work after the move because the attribute is still on the module object.

---

## Out of Scope

- `tier1_config.py` stays at root — pure config/data, used by 5+ packages.
- `backtest.py`, `config.py`, `main.py`, `scheduler_jobs.py` stay at root.
- No logic changes. Pure mechanical move + path updates.
