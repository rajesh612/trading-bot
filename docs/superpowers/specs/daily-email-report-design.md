# Daily Email Report + macOS LaunchAgent — Design Spec

**Status:** Approved  

---

## Problem

The trading bot runs fully autonomously but provides no passive notification. The user
(PST timezone) must manually check the Alpaca dashboard or tail the log to know how the
day went. Additionally, the bot must be started manually each morning, which is impractical
at 5:30–6:00 AM PST when pre-market agents fire.

---

## Goals

1. Send a daily email summary to `mail@gmail.com` at 4:30 PM ET every trading day.
2. Never miss a report — catch up on startup if the 4:30 PM job was missed.
3. Auto-start the bot at 5:30 AM PST on weekdays via macOS LaunchAgent.
4. Zero additional Claude API token cost.
5. No new pip dependencies (use stdlib `smtplib` + `ssl`).

---

## Non-Goals

- Push notifications, Slack, SMS — email only for now.
- Claude-generated narrative in the email — pure Python formatting only.
- Cloud deployment — local macOS laptop only.

---

## Architecture

### New files

| File | Purpose |
|---|---|
| `daily_report.py` | All email logic — build body, send, sentinel check |
| `com.tradingbot.plist` | macOS LaunchAgent plist |
| `setup_launchagent.sh` | One-command install/uninstall helper |

### Modified files

| File | Change |
|---|---|
| `config.py` | Add `EMAIL_SENDER`, `EMAIL_APP_PASSWORD`, `EMAIL_RECIPIENT` |
| `.env.example` | Add the three email vars with comments |
| `main.py` | Add `job_daily_report` at 4:30 PM ET; add catchup on startup |
| `test_agents.py` | 5 new `@pytest.mark.unit` tests for `daily_report.py` |
| `README.md` | Add email setup section and LaunchAgent setup section |

---

## `daily_report.py` — Module Design

### Functions

**`_already_sent_today() -> bool`**  
Reads `daily_report_sent.json`. Returns `True` if `{"date": "YYYY-MM-DD"}` matches today's
ET date. Prevents duplicate sends when the bot restarts after 4:30 PM.

**`_mark_sent() -> None`**  
Writes `{"date": "YYYY-MM-DD"}` to `daily_report_sent.json`.

**`build_report_body(trades, account, regime, watch_list, weights) -> tuple[str, str]`**  
Pure Python — no Claude, no API calls. Returns `(subject, body)`.  
Input data is passed in (not fetched inside) so the function is fully unit-testable.

**`_send_email(subject: str, body: str) -> None`**  
Thin wrapper: `smtplib.SMTP_SSL("smtp.gmail.com", 465)`. Reads credentials from `config`.
Raises on auth failure so the caller can log the error.

**`send_daily_report() -> None`**  
Orchestrator:
1. Return early if `EMAIL_SENDER` not configured (silent, no crash).
2. Return early if `_already_sent_today()`.
3. Load `trade_journal.json`, `vix_regime.json`, `watch_list.txt`, `indicator_weights.json`.
4. Fetch account snapshot from Alpaca (one lightweight GET call).
5. Call `build_report_body(...)`.
6. Call `_send_email(...)`.
7. Call `_mark_sent()`.
8. Log success or error.

### Sentinel file

`daily_report_sent.json` — written after every successful send.  
Content: `{"date": "YYYY-MM-DD"}`. Checked on every call to `send_daily_report()`.

---

## Email Format

**Subject:**
```
Trading Bot — 2026-04-30 | P&L: +$243.50 (+0.24%) | 7 trades
```
On no-trade days:
```
Trading Bot — 2026-04-30 | No trades today | Portfolio: $100,000.00
```

**Body:**
```
DAILY SUMMARY
Date:             Thursday, April 30 2026
Market Regime:    NORMAL
Portfolio Value:  $100,243.50
Today's P&L:      +$243.50 (+0.24%)

TRADE RESULTS
Closed trades: 7  |  Winners: 5  |  Losers: 2  |  Win rate: 71.4%

INDIVIDUAL TRADES
 #  Symbol  Qty  Entry     Exit      P&L        %       Reason
 1  NVDA     10  $875.20  $903.40  +$282.00  +3.22%  take_profit
 2  AAPL     15  $189.40  $193.45   +$60.75  +2.14%  take_profit
 3  TSLA      8  $248.10  $241.35   -$54.00  -2.72%  stop_loss
    ...

TODAY'S WATCH LIST
NVDA, AAPL, TSLA, META, GOOGL, AMD, INTC, NFLX, AMZN, MSFT

INDICATOR WEIGHTS (updated tonight)
VWAP: 19.44%  RSI: 16.67%  EMA 9/21: 12.96%  Bollinger: 8.33%
Daily Volume: 8.33%  VWAP Bands: 7.41%  MACD: 6.48%  RSI Slope: 6.48%
Stochastic: 5.56%  ATR: 3.70%  OBV: 3.70%  Vol Ratio: 0.93%
```

---

## Scheduler Integration (`main.py`)

### New job
```python
scheduler.add_job(
    job_daily_report,
    CronTrigger(hour=16, minute=30, day_of_week="mon-fri", timezone=ET),
    id="daily_report",
)
```

### Catchup logic in `_catchup_missed_agents()`
After the existing research catchup block, add:
```
If it is past 4:30 PM ET and daily_report_sent.json is not from today → run send_daily_report()
```
This ensures the report fires even if the bot was offline at 4:30 PM. It runs on the next
startup, whether that's 6 PM the same day or 8:45 AM the following morning.

---

## `.env` Additions

```
# ── Daily email report ────────────────────────────────────────────────────────
# Gmail App Password (not your real password).
# Generate at: myaccount.google.com → Security → 2-Step Verification → App Passwords
# Leave blank to disable the daily report.
EMAIL_SENDER=
EMAIL_APP_PASSWORD=
EMAIL_RECIPIENT=mail@gmail.com
```

`EMAIL_RECIPIENT` defaults to `EMAIL_SENDER` if left blank.

---

## macOS LaunchAgent

### File: `com.tradingbot.plist`

Installed to `~/Library/LaunchAgents/com.tradingbot.plist`.

Key settings:
- `StartCalendarInterval`: Monday–Friday at **05:30 local time** (= 8:30 AM ET)
- `Program`: absolute path to `start.sh --foreground`
- `KeepAlive`: `false` — launchd starts it once; the bot's own watchdog handles restarts
- `StandardOutPath` / `StandardErrorPath`: `launchd_trading_bot.log` in project dir
- If laptop is asleep at 5:30 AM, launchd fires the job on next wake

### File: `setup_launchagent.sh`

```bash
./setup_launchagent.sh            # install and load
./setup_launchagent.sh uninstall  # unload and remove
```

Handles:
- Substituting the absolute project path into the plist before installing
- Running `launchctl load` / `launchctl unload`
- Printing confirmation with next scheduled run time

---

## Tests (5 new, all `@pytest.mark.unit`)

| Test | What it verifies |
|---|---|
| `test_pnl_totals_correct` | `build_report_body` computes correct total, win rate, best/worst trade |
| `test_report_body_contains_fields` | Subject line, regime string, watch list present in output |
| `test_sentinel_false_when_missing` | `_already_sent_today()` returns `False` when file absent |
| `test_sentinel_true_for_today` | Returns `True` when file contains today's date |
| `test_send_skipped_if_no_config` | `send_daily_report()` exits silently when `EMAIL_SENDER` is blank |

SMTP is mocked via `unittest.mock.patch`. No real email sent during tests.

---

## PST User Daily Workflow

| Time (PST) | What happens |
|---|---|
| Any time you open laptop | LaunchAgent fires `start.sh`, bot catches up missed agents |
| 6:30 AM – 12:55 PM | Active trading window |
| 12:55 PM | Force-close all positions |
| 1:15 PM | Learning agent runs, updates strategy weights |
| 1:30 PM | Daily email arrives in inbox |
| Any time after 1:30 PM | Safe to close laptop |

---

## Security Notes

- `EMAIL_APP_PASSWORD` lives in `.env` which is gitignored. Never committed.
- Gmail App Passwords grant SMTP send-only access. Revokable instantly at myaccount.google.com.
- For future cloud deployment: move to a dedicated sender Gmail or a transactional email
  service (SendGrid free tier) to avoid Google blocking SMTP from cloud IPs.
