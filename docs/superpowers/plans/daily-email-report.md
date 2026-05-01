# Daily Email Report + LaunchAgent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a daily email summary report sent at 4:30 PM ET and a macOS LaunchAgent that auto-starts the bot at 5:30 AM PST on weekdays.

**Architecture:** A standalone `daily_report.py` module handles all email logic. A sentinel file (`daily_report_sent.json`) prevents duplicate sends on restart. A `_catchup_daily_report()` function in `main.py` catches up missed reports on startup. LaunchAgent is installed via `setup_launchagent.sh`.

**Tech Stack:** Python stdlib `smtplib` + `ssl` (no new pip deps), Gmail SMTP port 465, APScheduler CronTrigger, macOS launchd.

---

## File Map

| File | Action | What changes |
|---|---|---|
| `config.py` | Modify | Add `EMAIL_SENDER`, `EMAIL_APP_PASSWORD`, `EMAIL_RECIPIENT`, `DAILY_REPORT_SENT_FILE` |
| `.env.example` | Modify | Add three email vars with comments |
| `daily_report.py` | Create | All email logic: sentinel check, body builder, SMTP send, orchestrator |
| `main.py` | Modify | Add `job_daily_report`, register at 4:30 PM ET, add `_catchup_daily_report()` called from `main()` |
| `com.tradingbot.plist` | Create | LaunchAgent plist with placeholder project path |
| `setup_launchagent.sh` | Create | Install/uninstall script that subs in real path |
| `test_agents.py` | Modify | Append 5 new `@pytest.mark.unit` tests in a `TestDailyReport` class |
| `README.md` | Modify | Fix PID status check, add email setup section, add LaunchAgent section |
| `trading_bot_prompt.md` | Modify | Add 4:30 PM daily report row to schedule table |

---

## Task 1: Config + env vars

**Files:**
- Modify: `config.py:69-71` (after `TRADING_PARAMS_FILE`)
- Modify: `.env.example`

- [ ] **Step 1: Add email config to `config.py`**

Append after the `# Indicator engine` block at the end of `config.py`:

```python
# Email daily report (optional — leave blank to disable)
EMAIL_SENDER       = os.getenv("EMAIL_SENDER", "")
EMAIL_APP_PASSWORD = os.getenv("EMAIL_APP_PASSWORD", "")
EMAIL_RECIPIENT    = os.getenv("EMAIL_RECIPIENT", "")
DAILY_REPORT_SENT_FILE = BASE_DIR / "daily_report_sent.json"
```

- [ ] **Step 2: Add email vars to `.env.example`**

Append to the end of `.env.example`:

```
# ── Daily email report ─────────────────────────────────────────────────────────
# Gmail App Password — NOT your real password.
# Generate: myaccount.google.com → Security → 2-Step Verification → App Passwords
# Leave blank to disable the daily report entirely.
EMAIL_SENDER=
EMAIL_APP_PASSWORD=
EMAIL_RECIPIENT=mail@gmail.com
```

- [ ] **Step 3: Verify config loads cleanly**

```bash
.venv/bin/python3 -c "import config; print('EMAIL_SENDER:', repr(config.EMAIL_SENDER)); print('DAILY_REPORT_SENT_FILE:', config.DAILY_REPORT_SENT_FILE)"
```

Expected output:
```
EMAIL_SENDER: ''
DAILY_REPORT_SENT_FILE: /Users/rajeshkumar/Documents/projects/trading-bot/daily_report_sent.json
```

---

## Task 2: Write failing tests for `daily_report.py`

**Files:**
- Modify: `test_agents.py` (append after line 711)

- [ ] **Step 1: Add import at top of test file**

Find the existing imports block (first ~15 lines of `test_agents.py`) and add `json` if not already present. Check:

```bash
head -10 test_agents.py | grep "^import json"
```

If not present, add `import json` to the imports block.

- [ ] **Step 2: Append `TestDailyReport` class to `test_agents.py`**

```python


class TestDailyReport:

    @pytest.mark.unit
    def test_pnl_totals_correct(self):
        from daily_report import build_report_body
        trades = [
            {
                "type": "CLOSE", "symbol": "AAPL", "qty": 10,
                "entry_price": 100.0, "exit_price": 105.0,
                "pnl_dollar": 50.0, "pnl_pct": 5.0,
                "close_reason": "take_profit", "timestamp": "2026-04-30T12:00:00",
            },
            {
                "type": "CLOSE", "symbol": "TSLA", "qty": 5,
                "entry_price": 200.0, "exit_price": 192.0,
                "pnl_dollar": -40.0, "pnl_pct": -4.0,
                "close_reason": "stop_loss", "timestamp": "2026-04-30T13:00:00",
            },
        ]
        account = {"portfolio_value": 100010.0, "last_equity": 100000.0}
        subject, body = build_report_body(trades, account, "NORMAL", "AAPL, TSLA", {})
        assert "2 trades" in subject
        assert "AAPL" in body
        assert "TSLA" in body
        assert "take_profit" in body
        assert "stop_loss" in body

    @pytest.mark.unit
    def test_report_body_contains_fields(self):
        from daily_report import build_report_body
        account = {"portfolio_value": 100000.0, "last_equity": 100000.0}
        subject, body = build_report_body(
            [], account, "HIGH_VOL", "NVDA, AMD", {"RSI": 50.0, "VWAP": 50.0}
        )
        assert "HIGH_VOL" in body
        assert "NVDA, AMD" in body
        assert "RSI" in body
        assert "No trades closed today" in body
        assert "No trades today" in subject

    @pytest.mark.unit
    def test_sentinel_false_when_missing(self, tmp_path, monkeypatch):
        import daily_report
        monkeypatch.setattr(
            daily_report, "DAILY_REPORT_SENT_FILE", tmp_path / "daily_report_sent.json"
        )
        assert daily_report._already_sent_today() is False

    @pytest.mark.unit
    def test_sentinel_true_for_today(self, tmp_path, monkeypatch):
        import daily_report
        from datetime import datetime
        from zoneinfo import ZoneInfo
        sentinel = tmp_path / "daily_report_sent.json"
        today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        sentinel.write_text(json.dumps({"date": today}))
        monkeypatch.setattr(daily_report, "DAILY_REPORT_SENT_FILE", sentinel)
        assert daily_report._already_sent_today() is True

    @pytest.mark.unit
    def test_send_skipped_if_no_config(self, monkeypatch):
        import config as cfg
        monkeypatch.setattr(cfg, "EMAIL_SENDER", "")
        monkeypatch.setattr(cfg, "EMAIL_APP_PASSWORD", "")
        from daily_report import send_daily_report
        send_daily_report()  # must not raise
```

- [ ] **Step 3: Run tests — confirm they all fail with ImportError**

```bash
.venv/bin/python3 -m pytest test_agents.py::TestDailyReport -v -m unit
```

Expected: 5 tests collected, all fail with `ModuleNotFoundError: No module named 'daily_report'`

---

## Task 3: Implement `daily_report.py`

**Files:**
- Create: `daily_report.py`

- [ ] **Step 1: Create `daily_report.py`**

```python
import json
import logging
import smtplib
import ssl
from datetime import datetime
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

DAILY_REPORT_SENT_FILE = config.DAILY_REPORT_SENT_FILE


def _already_sent_today() -> bool:
    try:
        data = json.loads(DAILY_REPORT_SENT_FILE.read_text())
        return data.get("date") == datetime.now(ET).date().isoformat()
    except (FileNotFoundError, json.JSONDecodeError):
        return False


def _mark_sent() -> None:
    DAILY_REPORT_SENT_FILE.write_text(
        json.dumps({"date": datetime.now(ET).date().isoformat()})
    )


def build_report_body(
    trades: list,
    account: dict,
    regime: str,
    watch_list: str,
    weights: dict,
) -> tuple[str, str]:
    today_label = datetime.now(ET).strftime("%A, %B %-d %Y")
    today_date  = datetime.now(ET).strftime("%Y-%m-%d")

    closes = [t for t in trades if t.get("type") == "CLOSE"]

    portfolio_value = account.get("portfolio_value", 0.0)
    last_equity     = account.get("last_equity", portfolio_value)
    day_pnl         = portfolio_value - last_equity
    day_pnl_pct     = (day_pnl / last_equity * 100) if last_equity else 0.0

    if closes:
        subject = (
            f"Trading Bot — {today_date} | "
            f"P&L: ${day_pnl:+,.2f} ({day_pnl_pct:+.2f}%) | {len(closes)} trades"
        )
    else:
        subject = (
            f"Trading Bot — {today_date} | No trades today | "
            f"Portfolio: ${portfolio_value:,.2f}"
        )

    winners  = [t for t in closes if t.get("pnl_dollar", 0) > 0]
    losers   = [t for t in closes if t.get("pnl_dollar", 0) <= 0]
    win_rate = len(winners) / len(closes) * 100 if closes else 0.0

    lines = [
        "DAILY SUMMARY",
        f"Date:             {today_label}",
        f"Market Regime:    {regime}",
        f"Portfolio Value:  ${portfolio_value:,.2f}",
        f"Today's P&L:      ${day_pnl:+,.2f} ({day_pnl_pct:+.2f}%)",
        "",
    ]

    if closes:
        lines += [
            "TRADE RESULTS",
            (
                f"Closed trades: {len(closes)}  |  Winners: {len(winners)}  |  "
                f"Losers: {len(losers)}  |  Win rate: {win_rate:.1f}%"
            ),
            "",
            "INDIVIDUAL TRADES",
            f" {'#':>2}  {'Symbol':<7} {'Qty':>5}  {'Entry':>8}  {'Exit':>8}  "
            f"{'P&L':>10}  {'%':>7}  Reason",
        ]
        for i, t in enumerate(
            sorted(closes, key=lambda x: x.get("pnl_dollar", 0), reverse=True), 1
        ):
            lines.append(
                f" {i:>2}  {t.get('symbol','?'):<7} {t.get('qty', 0):>5}  "
                f"${t.get('entry_price', 0):>7.2f}  ${t.get('exit_price', 0):>7.2f}  "
                f"${t.get('pnl_dollar', 0):>+9,.2f}  {t.get('pnl_pct', 0):>+6.2f}%  "
                f"{t.get('close_reason', '')}"
            )
        lines.append("")
    else:
        lines += ["TRADE RESULTS", "No trades closed today.", ""]

    lines += [
        "TODAY'S WATCH LIST",
        watch_list if watch_list else "(not available)",
        "",
    ]

    if weights:
        weight_parts = [
            f"{k}: {v:.2f}%"
            for k, v in sorted(weights.items(), key=lambda x: x[1], reverse=True)
        ]
        lines += [
            "INDICATOR WEIGHTS (updated tonight)",
            "  ".join(weight_parts),
        ]

    return subject, "\n".join(lines)


def _send_email(subject: str, body: str) -> None:
    recipient = config.EMAIL_RECIPIENT or config.EMAIL_SENDER
    message   = (
        f"Subject: {subject}\r\n"
        f"From: {config.EMAIL_SENDER}\r\n"
        f"To: {recipient}\r\n"
        f"\r\n"
        f"{body}"
    )
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(config.EMAIL_SENDER, config.EMAIL_APP_PASSWORD)
        server.sendmail(config.EMAIL_SENDER, recipient, message)


def send_daily_report() -> None:
    if not config.EMAIL_SENDER or not config.EMAIL_APP_PASSWORD:
        logger.info("DAILY REPORT: EMAIL_SENDER not configured — skipping.")
        return

    if _already_sent_today():
        logger.info("DAILY REPORT: Already sent today — skipping.")
        return

    logger.info("DAILY REPORT: Building daily summary…")

    account = {"portfolio_value": 0.0, "last_equity": 0.0}
    try:
        from alpaca_client import AlpacaClient
        acct = AlpacaClient().get_account()
        account = {
            "portfolio_value": float(acct.portfolio_value),
            "last_equity":     float(acct.last_equity),
        }
    except Exception as exc:
        logger.warning(f"DAILY REPORT: Could not fetch Alpaca account — {exc}")

    trades = []
    try:
        trades = json.loads(config.TRADE_JOURNAL_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    today       = datetime.now(ET).date().isoformat()
    todays_trades = [t for t in trades if t.get("timestamp", "").startswith(today)]

    regime = "UNKNOWN"
    try:
        regime = json.loads(config.VIX_REGIME_FILE.read_text()).get("regime", "UNKNOWN")
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    watch_list = ""
    try:
        watch_list = ", ".join(config.WATCH_LIST_FILE.read_text().strip().splitlines())
    except FileNotFoundError:
        pass

    weights = {}
    try:
        weights = json.loads(config.INDICATOR_WEIGHTS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    subject, body = build_report_body(todays_trades, account, regime, watch_list, weights)

    try:
        _send_email(subject, body)
        _mark_sent()
        logger.info(f"DAILY REPORT: Sent — '{subject}'")
    except Exception as exc:
        logger.error(f"DAILY REPORT: Failed to send — {exc}")
```

- [ ] **Step 2: Run tests — all 5 should pass**

```bash
.venv/bin/python3 -m pytest test_agents.py::TestDailyReport -v -m unit
```

Expected:
```
test_agents.py::TestDailyReport::test_pnl_totals_correct PASSED
test_agents.py::TestDailyReport::test_report_body_contains_fields PASSED
test_agents.py::TestDailyReport::test_sentinel_false_when_missing PASSED
test_agents.py::TestDailyReport::test_sentinel_true_for_today PASSED
test_agents.py::TestDailyReport::test_send_skipped_if_no_config PASSED
5 passed
```

- [ ] **Step 3: Run full unit suite — confirm no regressions**

```bash
.venv/bin/python3 -m pytest test_agents.py -v -m unit
```

Expected: 28 passed, 0 failed.

---

## Task 4: Wire `daily_report` into `main.py`

**Files:**
- Modify: `main.py`

- [ ] **Step 1: Add `job_daily_report` function after `job_learning` (after line 180)**

Insert after the `job_learning` function (line 180 ends with `run_learning_agent()`):

```python

def job_daily_report():
    logger.info("━━━ DAILY REPORT ━━━")
    from daily_report import send_daily_report
    send_daily_report()
```

- [ ] **Step 2: Register the job in `setup_scheduler` (line 205)**

In `setup_scheduler()`, after the line that adds `job_learning`:
```python
scheduler.add_job(job_learning, CronTrigger(hour=16, minute=15, **kw), id="learning")
```

Add immediately below it:
```python
    scheduler.add_job(job_daily_report, CronTrigger(hour=16, minute=30, **kw), id="daily_report")
```

- [ ] **Step 3: Add `_catchup_daily_report` function before `_handle_shutdown` (before line 411)**

Insert before `def _handle_shutdown(signum, frame):`:

```python

def _catchup_daily_report():
    """Send the daily report on startup if it was missed (bot offline at 4:30 PM ET)."""
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return  # weekend
    report_due = now.hour > 16 or (now.hour == 16 and now.minute >= 30)
    if not report_due:
        return
    from daily_report import send_daily_report
    logger.info("CATCHUP: checking if daily report needs to be sent…")
    send_daily_report()

```

- [ ] **Step 4: Call `_catchup_daily_report()` in `main()` (after line 439)**

In `main()`, after the existing `_catchup_missed_agents()` call:
```python
    _reconcile_positions()
    _catchup_missed_agents()
```

Add:
```python
    _catchup_daily_report()
```

- [ ] **Step 5: Verify bot starts cleanly**

```bash
.venv/bin/python3 -c "
import main
print('job_daily_report defined:', callable(main.job_daily_report))
print('_catchup_daily_report defined:', callable(main._catchup_daily_report))
"
```

Expected:
```
job_daily_report defined: True
_catchup_daily_report defined: True
```

---

## Task 5: LaunchAgent files

**Files:**
- Create: `com.tradingbot.plist`
- Create: `setup_launchagent.sh`

- [ ] **Step 1: Create `com.tradingbot.plist`**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.tradingbot</string>

    <key>ProgramArguments</key>
    <array>
        <string>PLACEHOLDER_PROJECT_DIR/start.sh</string>
        <string>--foreground</string>
    </array>

    <!-- Fire Mon–Fri at 05:30 local time (= 08:30 ET).
         If the laptop is asleep, launchd fires on next wake. -->
    <key>StartCalendarInterval</key>
    <array>
        <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>5</integer><key>Minute</key><integer>30</integer></dict>
        <dict><key>Weekday</key><integer>2</integer><key>Hour</key><integer>5</integer><key>Minute</key><integer>30</integer></dict>
        <dict><key>Weekday</key><integer>3</integer><key>Hour</key><integer>5</integer><key>Minute</key><integer>30</integer></dict>
        <dict><key>Weekday</key><integer>4</integer><key>Hour</key><integer>5</integer><key>Minute</key><integer>30</integer></dict>
        <dict><key>Weekday</key><integer>5</integer><key>Hour</key><integer>5</integer><key>Minute</key><integer>30</integer></dict>
    </array>

    <key>StandardOutPath</key>
    <string>PLACEHOLDER_PROJECT_DIR/launchd_trading_bot.log</string>
    <key>StandardErrorPath</key>
    <string>PLACEHOLDER_PROJECT_DIR/launchd_trading_bot.log</string>

    <!-- launchd starts once; the bot's own watchdog handles restarts within a session -->
    <key>KeepAlive</key>
    <false/>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
```

- [ ] **Step 2: Create `setup_launchagent.sh`**

```bash
#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_SOURCE="$SCRIPT_DIR/com.tradingbot.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/com.tradingbot.plist"

if [ "${1}" = "uninstall" ]; then
    echo "[setup_launchagent] Uninstalling LaunchAgent…"
    launchctl unload "$PLIST_DEST" 2>/dev/null || true
    rm -f "$PLIST_DEST"
    echo "[setup_launchagent] Done. Bot will no longer auto-start."
    exit 0
fi

echo "[setup_launchagent] Installing Trading Bot LaunchAgent…"
sed "s|PLACEHOLDER_PROJECT_DIR|$SCRIPT_DIR|g" "$PLIST_SOURCE" > "$PLIST_DEST"
launchctl unload "$PLIST_DEST" 2>/dev/null || true
launchctl load "$PLIST_DEST"

echo "[setup_launchagent] Installed."
echo "  Auto-start: 5:30 AM local time, Monday–Friday"
echo "  If laptop is asleep at 5:30 AM, fires on next wake."
echo "  Logs: $SCRIPT_DIR/launchd_trading_bot.log"
echo ""
echo "  To uninstall: ./setup_launchagent.sh uninstall"
```

- [ ] **Step 3: Make executable**

```bash
chmod +x setup_launchagent.sh
```

- [ ] **Step 4: Verify plist is valid XML**

```bash
plutil -lint com.tradingbot.plist
```

Expected: `com.tradingbot.plist: OK`

---

## Task 6: Update README.md and trading_bot_prompt.md

**Files:**
- Modify: `README.md`
- Modify: `trading_bot_prompt.md`

- [ ] **Step 1: Fix the "Check if running" section in README.md**

Find this block in README.md:
```markdown
**Check if the bot is running:**
```bash
cat trading_bot.pid          # Shows the PID if running
kill -0 $(cat trading_bot.pid) 2>/dev/null && echo "running" || echo "stopped"
```
```

Replace with:
```markdown
**Check if the bot is running:**
```bash
pgrep -f "python3 main.py" && echo "Bot is running" || echo "Bot is stopped"
```

Note: `trading_bot.pid` is deleted by `stop.sh` and recreated by `start.sh`. Use `pgrep` for a reliable check.
```

- [ ] **Step 2: Add Email Setup section to README.md**

After the "Changing Risk Parameters" section, add:

```markdown
## Setting Up the Daily Email Report

At 4:30 PM ET each trading day, the bot emails a full summary of trades, P&L, watch list, and updated indicator weights to your inbox.

**Step 1 — Create a Gmail App Password**

1. Go to [myaccount.google.com](https://myaccount.google.com) → Security → 2-Step Verification → App Passwords
2. Create a new App Password (name it "Trading Bot")
3. Copy the 16-character password shown

**Step 2 — Add to `.env`**

```
EMAIL_SENDER=your.gmail@gmail.com
EMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
EMAIL_RECIPIENT=mail@gmail.com
```

`EMAIL_RECIPIENT` defaults to `EMAIL_SENDER` if left blank.

Leave `EMAIL_SENDER` blank to disable the report entirely — the bot runs normally without it.

**Restart the bot after adding email credentials:**
```bash
./stop.sh && ./start.sh
```

---

## Auto-Start with macOS LaunchAgent

Run once to install — the bot will auto-start at **5:30 AM local time every weekday**:

```bash
./setup_launchagent.sh
```

If your laptop is asleep at 5:30 AM, launchd fires the job the moment you open the lid.

```bash
./setup_launchagent.sh uninstall   # Remove auto-start
```

LaunchAgent logs go to `launchd_trading_bot.log` in the project folder.
```

- [ ] **Step 3: Add daily_report row to schedule table in `trading_bot_prompt.md`**

Find the schedule table row:
```
| 4:15 PM | Learning agent — Claude reviews closed trades, updates indicator weights and trading params |
```

Add a new row after it:
```
| 4:30 PM | Daily report — Python formats trade summary, P&L, watch list, indicator weights and sends email |
```

- [ ] **Step 4: Run full test suite one final time**

```bash
.venv/bin/python3 -m pytest test_agents.py -v -m unit
```

Expected: 28 passed, 0 failed.

---

## Task 7: Commit everything

- [ ] **Step 1: Stage and commit**

```bash
git add daily_report.py com.tradingbot.plist setup_launchagent.sh \
        config.py .env.example main.py test_agents.py README.md trading_bot_prompt.md \
        docs/superpowers/specs/daily-email-report-design.md \
        docs/superpowers/plans/daily-email-report.md
git commit -m "$(cat <<'EOF'
feat: add daily email report and macOS LaunchAgent auto-start

- daily_report.py: post-market email with full trade breakdown, P&L,
  watch list, and indicator weights — zero Claude token cost
- LaunchAgent fires at 5:30 AM PST Mon–Fri; fires on lid-open if asleep
- Catchup logic sends missed report on next bot startup after 4:30 PM ET
- Sentinel file prevents duplicate sends on restart
- 5 new unit tests; README updated with email setup and status check fix

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```
