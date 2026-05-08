# Multi-Agent AI Trading Bot

An autonomous intraday and swing trading bot that uses Claude AI for decisions and Alpaca for paper trade execution. It runs weekdays, adapts to market regimes, holds overnight swing positions, learns from every trade, and improves its own strategy over time.

**Paper trading only — no real money is ever at risk.**

---

## Prerequisites

- macOS or Linux
- Python 3.11 or later (`python3 --version`)
- An [Alpaca](https://app.alpaca.markets) account (free)
- An [Anthropic](https://console.anthropic.com) account
- A [FRED](https://fred.stlouisfed.org/docs/api/api_key.html) API key (free, optional but recommended)

---

## First-Time Setup

### 1. Clone and enter the project

```bash
cd /path/to/trading-bot
```

### 2. Create your `.env` file

```bash
cp .env.example .env
```

Fill in your keys:

| Key | Where to get it | Cost |
|---|---|---|
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | [app.alpaca.markets](https://app.alpaca.markets) → API Keys → **Paper** tab | Free |
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) → API Keys | ~$0.30–0.55/day |
| `FRED_API_KEY` | [fred.stlouisfed.org/docs/api/api_key.html](https://fred.stlouisfed.org/docs/api/api_key.html) | Free |
| `POLYGON_API_KEY` | [polygon.io/dashboard/keys](https://polygon.io/dashboard/keys) (free tier) | Free (optional) |
| Reddit keys | [reddit.com/prefs/apps](https://www.reddit.com/prefs/apps) → script type | Free (optional) |
| `X_BEARER_TOKEN` | X Basic API | $100/mo (optional, skip unless needed) |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | @BotFather → /newbot; see Telegram setup below | Free (optional) |

### Market data provider — `MARKET_DATA_PROVIDER`

| Value | Bars | Real-time price | When to use |
|---|---|---|---|
| `yahoo` (default) | Yahoo Finance — full volume, free | 15-min delayed | Best free option |
| `alpaca` | Alpaca SIP (live) or IEX (paper, ~2% volume) | Real-time on live | Upgrade to live + Algo Trader Plus |
| `hybrid` | Yahoo for bars | Alpaca for real-time | Recommended balance during paper trading |

### 3. Start the bot

```bash
./start.sh
```

`start.sh` automatically creates `.venv/`, installs dependencies, starts the bot in the background, and catches up any missed pre-market agents if started mid-day.

---

## Starting and Stopping

```bash
./start.sh    # Start in background
./stop.sh     # Stop gracefully
```

```bash
pgrep -f "python3 main.py" && echo "Running" || echo "Stopped"
tail -f trading_bot.log
```

**Log files** — `trading_bot.log` rotates automatically at 10 MB (up to 5 backups: `.log.1` … `.log.5`). No manual cleanup needed. Corrupt state file backups (`.corrupt.json`) older than 7 days and leftover `.tmp` files are deleted at every startup. Trade journal entries older than 90 days are moved to `trade_journal_archive.json`.

---

## Daily Schedule (ET, weekdays only)

| Time | Job | What it does |
|---|---|---|
| 8:00 AM | Earnings Calendar | Fetches next-earnings dates + PEAD detection for watch list + tier-1 stocks |
| 8:30 AM | Economic Calendar | Loads FOMC/NFP/CPI event-risk windows for the day |
| 8:45 AM | Volatility Screener | Filters ticker universe to top 50 most volatile |
| 8:55 AM | Macro Regime | 9-state regime classification (CRISIS / BULL_TREND / CHOPPY / etc.) |
| 9:00 AM | Watch List Agent | Claude picks 10–15 stocks for the day |
| 9:15 AM | Research Agent | RSS + Reddit + StockTwits sentiment; writes macro shock score |
| 9:30 AM | Swing Gap Check | Evaluates pre-market gaps on held overnight swing positions |
| Every 10 min (9:30–3:50) | Trading Cycle | Technicals + Claude BUY/HOLD/CLOSE; mode-aware sizing and SL/TP |
| Every 60 sec (9:30–3:50) | Position Monitor | Enforces SL/TP on day-trades; tiered TP on swing positions |
| Every 5 min (9:30–3:50) | Market Monitor | Flash-crash detection via SPY (free, no Claude calls) |
| Every 30 min | Heartbeat | Confirms scheduler is alive; watchdog exits if missed |
| 3:50 PM | Swing EOD Evaluation | Promotes qualifying positions to overnight swing holds |
| 3:55 PM | Force Close | Cancels orders, closes all day-trade positions (swing positions preserved) |
| 4:00 PM | FRED Cache Refresh | Updates Fed/CPI/unemployment macro modifiers |
| 4:15 PM | Learning Agent | Reviews closed trades, updates indicator weights and trading params |
| 4:20 PM | Daily Email Report | P&L summary, trade log, updated weights (optional — needs Gmail config) |

If you start the bot mid-day, it automatically catches up any missed pre-market agents.

---

## Trading Modes

The bot selects a mode per trade based on the macro regime and symbol:

| Mode | Triggered by | SL% | TP% | Max position |
|---|---|---|---|---|
| DAY_TRADE | NORMAL / CHOPPY / DEFENSIVE | 3–5% | 5–6% | 5% portfolio |
| HYBRID | BULL_TREND / OPPORTUNITY | 7% | 15% | 3% portfolio |
| SHORT_TERM | CAPITULATION / CRASH_RECOVERY | 10–12% | 18–20% | 2.5% portfolio |
| PEAD | Tier-1 stock with recent strong earnings beat | 8% | 8% tiered | 2.5% portfolio |
| CASH | CRISIS / flash crash | — no entries — | — | — |

---

## Overnight Swing Positions

At 3:50 PM the swing evaluator may promote a day-trade to an overnight hold if it passes 7 gates (trend alignment, volume, sector sentiment, macro regime, earnings safety, max-days limit). Swing positions use tiered take-profit (40%/30%/30% of position size) and a trailing stop after TP2 fires.

A morning gap check at 9:30 AM re-evaluates each swing position against the pre-market gap and closes it if necessary.

---

## Crash Recovery and Overnight Operation

The bot is designed to run continuously (overnight, weekends) and recover gracefully:

- **Single-instance lock** — prevents two bot processes from running simultaneously.
- **PID + watchdog** — watchdog exits the process if the scheduler freezes; launchd/systemd restarts it.
- **Crash loop gate** — `bot_health.json` tracks restart frequency. If the bot crashes ≥ 3 times within 10 minutes, startup pauses 30 minutes before resuming. A clean `stop.sh` resets the counter.
- **Orphan-order drain** — on every startup, any pending BUY orders left in Alpaca from a crashed cycle are cancelled before the trading cycle resumes. Prevents duplicate bracket orders.
- **Position reconciliation** — syncs `open_positions.json` with Alpaca's actual state. Handles bracket fills that happened while offline and crashed mid-entry restores.
- **Flash crash confirmation** — requires two consecutive SPY readings ≥ −2.5% (5 min apart) before triggering a 48h lockout. If `POLYGON_API_KEY` is set, a Massive API cross-check replaces the 5-min wait. A single bad data tick no longer locks the bot out.
- **Flash crash auto-recovery** — the 8:55 AM `fetch_macro_regime()` job auto-clears the lockout if VIX < 35 and SPY 5-day return > −5%, without manual intervention.
- **Swing position safety** — reconciliation skips swing positions (intentional holds); force-close at 3:55 PM also skips them.
- **Daily closed list** — `daily_closed.json` prevents re-entering a symbol on the same day, even across crash/restart cycles.

**macOS crash recovery:** The LaunchAgent has `KeepAlive` disabled (`<false/>`) to prevent log-flooding if TCC blocks bash from reading scripts in `~/Documents`. The bot's internal watchdog thread and crash-loop gate in `main.py` handle intra-session recovery; the `StartCalendarInterval` handles the next-day restart.

**Linux crash recovery:** Use a systemd unit instead of cron for intra-day crash recovery:
```ini
[Service]
ExecStart=/path/to/trading-bot/start.sh --foreground
Restart=on-failure
RestartSec=15
```
The provided `setup_cron.sh` handles scheduled start/stop only and does not restart on crash.

---

## Customising the Ticker Universe

Edit `tickers.txt` — one ticker per line. The watch list agent selects 10–15 each morning.

---

## Changing Risk Parameters

Edit `config.py`. Key parameters:

| Parameter | Default | Description |
|---|---|---|
| `MAX_POSITION_PCT` | `0.05` | Max 5% of portfolio per day-trade position |
| `RISK_PER_TRADE_PCT` | `0.005` | Risk 0.5% per trade (ATR-based sizing) |
| `STOP_LOSS_PCT` | `4.0` | Fallback stop loss % (overridden by mode/regime table) |
| `TAKE_PROFIT_PCT` | `5.0` | Fallback take profit % (overridden by mode/regime table) |
| `MAX_DAILY_LOSS_PCT` | `0.03` | Circuit breaker — halts new entries if down 3% |
| `AUTO_BUY_COMPOSITE_THRESHOLD` | `35` | composite > this + aligned indicators → skip Claude, auto-BUY |
| `AUTO_BUY_MIN_SENTIMENT` | `-10` | Sentiment score floor for any BUY — blocks entries on bad-news drops regardless of technicals |
| `API_BUDGET_NORMAL_CEILING` | `$1.00` | Daily Claude spend cap (normal conditions) |
| `API_BUDGET_ELEVATED_CEILING` | `$1.50` | Daily Claude spend cap (CRISIS / high-gain day) |
| `TRADE_JOURNAL_MAX_DAYS` | `90` | Journal entries older than this are archived to `trade_journal_archive.json` at startup |
| `CORRUPT_BACKUP_MAX_DAYS` | `7` | `.corrupt.json` backup files older than this are deleted at startup |

Restart after any `config.py` change: `./stop.sh && ./start.sh`

---

## Setting Up Notifications

At 4:20 PM ET each trading day the bot sends a P&L summary. You can enable email, Telegram, both, or neither.

### Email (Gmail)

**Create a Gmail App Password:**
1. [myaccount.google.com](https://myaccount.google.com) → Security → 2-Step Verification → App Passwords → Create
2. Add to `.env`:
   ```
   EMAIL_SENDER=your.gmail@gmail.com
   EMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
   EMAIL_RECIPIENT=your@email.com
   ```

Leave `EMAIL_SENDER` blank to disable email.

---

### Telegram

To activate Telegram (3 steps):
                                      
1. Open Telegram → search @BotFather → send /newbot → follow prompts → copy the token it gives you.
2. Message your new bot (send it any text — e.g. "hello"), then run:
   ```
   ! curl "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates"
   ```
Find "id" inside "chat" — that's your chat_id.
3. Add both to .env:
   ```
   TELEGRAM_BOT_TOKEN=<token from BotFather>
   TELEGRAM_CHAT_ID=<chat id from getUpdates>
   ```

What you'll receive on Telegram:                                
  ┌─────────────────┬─────────────────────────────────────────────────────┐
  │      Event      │                       Message                       │
  ├─────────────────┼─────────────────────────────────────────────────────┤
  │ Bot starts      │ 🟢 Bot started — Regime: BULL_TREND | Open: 0       │
  ├─────────────────┼─────────────────────────────────────────────────────┤
  │ Bot stops       │ 🔴 Bot stopped                                      │
  ├─────────────────┼─────────────────────────────────────────────────────┤
  │ BUY order       │ 📈 BUY NVDA — 15 shares @ $890.20, SL $856, TP $935 │
  ├─────────────────┼─────────────────────────────────────────────────────┤
  │ CLOSE (win)     │ ✅ CLOSE NVDA — +$234.00 (+3.8%)                    │
  ├─────────────────┼─────────────────────────────────────────────────────┤
  │ CLOSE (loss)    │ 🔻 CLOSE AAPL — -$45.00 (-1.2%)                     │
  ├─────────────────┼─────────────────────────────────────────────────────┤
  │ Claude disabled │ 🚫 Claude disabled — no trades until restart        │
  ├─────────────────┼─────────────────────────────────────────────────────┤
  │ Job failure     │ ❌ Job failed: force_close — JSONDecodeError        │
  ├─────────────────┼─────────────────────────────────────────────────────┤
  │ Corrupt file    │ ⚠️  Corrupt state file reset: trade_journal.json    │
  ├─────────────────┼─────────────────────────────────────────────────────┤
  │ Regime shift    │ 🚨 Regime change: NORMAL → CRISIS                   │
  ├─────────────────┼─────────────────────────────────────────────────────┤
  │ Flash crash     │ 🚨 FLASH CRASH detected — CRISIS lockout            │
  ├─────────────────┼─────────────────────────────────────────────────────┤
  │ EOD summary     │ 📊 P&L: +$127.40 (+1.27%) | 4 trades | 75% win      │
  └─────────────────┴─────────────────────────────────────────────────────┘

---
## Auto-Start/Stop

| Event | PT (local) | ET |
|---|---|---|
| Auto-start | 5:30 AM | 8:30 AM |
| Auto-stop | 1:30 PM | 4:30 PM |

Once installed, the bot starts and stops **automatically every weekday — no daily manual action needed.** If your laptop is asleep at the scheduled time, launchd fires as soon as you open the lid.

### macOS — LaunchAgent

**Step 1 — Install (one-time):**
```bash
./setup_launchagent.sh
```

**Step 2 — Grant Full Disk Access to bash (one-time, required if project is in ~/Documents):**

System Settings → Privacy & Security → Full Disk Access → click `+` → press `Cmd+Shift+G` → type `/bin` → select `bash` → click Open

Without this, launchd silently fails with a permissions error and produces no log output.

**Step 3 — Verify installation:**
```bash
launchctl list | grep trading_bot
# Expected output (two lines, exit code 0 means loaded):
# -    0    trading_bot_stop
# -    0    trading_bot_start
```

**After this, do nothing.** The bot will auto-start at 5:30 AM PT weekdays and auto-stop at 1:30 PM PT. You only need `./stop.sh` or `./start.sh` for manual overrides.

**Uninstall:**
```bash
./setup_launchagent.sh uninstall
```

---

### Testing the LaunchAgent outside market hours

If you want to confirm it works right now (without waiting until tomorrow morning), trigger it manually:

```bash
launchctl start trading_bot_start
sleep 5
tail -f launchd_trading_bot.log
```

You should see startup log lines within 5 seconds and a Telegram "🟢 Bot started" message if Telegram is configured. Press `Ctrl+C` to stop tailing. Run `./stop.sh` when done testing.

> **This is for testing only — you do NOT need to run `launchctl start` each day.**

**Two log files — know the difference:**

| File | Written by | When |
|---|---|---|
| `launchd_trading_bot.log` | LaunchAgent (auto-start) | When launchd fires the bot |
| `trading_bot.log` | Manual `./start.sh` | When you start the bot yourself |

If you started via LaunchAgent but see nothing in `launchd_trading_bot.log`, Full Disk Access is likely the issue (Step 2 above).

---

### Linux — cron

```bash
./setup_cron.sh            # Install (CRON_TZ=America/New_York, DST-aware)
./setup_cron.sh uninstall  # Remove
```

---

## State Files

| File | Purpose |
|---|---|
| `open_positions.json` | Active day-trade positions (entry price, SL, TP, mode) |
| `swing_positions.json` | Active overnight swing positions (tiered TP state) |
| `trade_journal.json` | Recent trade history — entries older than 90 days archived at startup |
| `trade_journal_archive.json` | Long-term trade archive (auto-populated from `trade_journal.json`) |
| `indicator_weights.json` | Per-indicator weights (updated nightly by learning agent) |
| `trading_params.json` | Learned entry thresholds (updated nightly, hard-bounded) |
| `macro_regime.json` | Today's 9-state regime + macro health score + flash-crash lockout |
| `earnings_calendar.json` | Next earnings dates + PEAD data per symbol (refreshed 8:00 AM) |
| `economic_calendar.json` | Today's FOMC/NFP/CPI events + 45-min risk windows |
| `research_report.json` | Sentiment scores + macro shock score + sector sentiment |
| `api_budget.json` | Daily Claude token spend tracker (enforces $1.00/$1.50 cap) |
| `strategy_memory.md` | Freeform strategy notes written by learning agent, fed to Claude |
| `watch_list.txt` | Today's 10–15 selected tickers |
| `daily_closed.json` | Symbols closed today — prevents same-day re-entry after crash |
| `bot_health.json` | Crash loop counter — restart frequency tracker (auto-created, reset on clean stop) |

---

## Running Tests

```bash
# Fast unit tests — no API calls, no .env required
.venv/bin/pytest tests/ -v -m unit

# Legacy unit tests
.venv/bin/pytest test_agents.py -v -m unit

# Live tests — requires valid .env with Alpaca credentials
.venv/bin/pytest test_agents.py -v -m live
```

---

## Backtesting

```bash
.venv/bin/python3 backtest.py
```

Outputs `backtest_trades.csv` and `backtest_equity.json`.

---

## Switching from Paper to Live Trading

**Strongly recommend 4+ weeks of consistent paper profitability first.**

1. Generate live keys: [app.alpaca.markets](https://app.alpaca.markets) → **Live** tab
2. Replace `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` in `.env`
3. In `alpaca_client.py`, change `paper=True` to `paper=False` (intentionally manual to prevent accidents)
4. (Recommended) Subscribe to Algo Trader Plus ($99/mo) and set `MARKET_DATA_PROVIDER=alpaca` for SIP real-time data
5. Restart: `./stop.sh && ./start.sh`

**Pattern Day Trader rule:** Live accounts below $25,000 are limited to 3 day trades per 5 business days (FINRA rule). The bot does not enforce this — manage your day-trade count manually.

---

## Troubleshooting

**"Another instance is already running"**
```bash
./stop.sh && ./start.sh
```

**Positions out of sync with Alpaca**
```bash
grep RECONCILE trading_bot.log | tail -20
```
The bot reconciles at every startup. If something looks wrong, check these lines.

**Flash crash lockout active after a false positive**

The bot now auto-recovers: the 8:55 AM macro regime job clears the lockout if VIX < 35 and SPY 5-day return > −5%. Wait until the next trading morning — no manual action needed.

If you need to clear it immediately:
```bash
./stop.sh && ./start.sh
```
The startup `fetch_macro_regime()` will evaluate conditions and clear automatically.

**No Telegram notification after bot starts**

Check in this order:
1. Confirm `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set in `.env` (not blank).
2. Confirm `ANTHROPIC_API_KEY` is set — if it is missing the bot exits before sending any Telegram alert (known limitation, fixed in next release).
3. Check the correct log file: `launchd_trading_bot.log` if started via LaunchAgent, `trading_bot.log` if started manually via `./start.sh`.
4. If the log file is empty after a LaunchAgent start, Full Disk Access for `/bin/bash` is likely missing — see the macOS LaunchAgent setup steps above.
5. Send a test message to confirm Telegram credentials work:
   ```bash
   curl -s "https://api.telegram.org/bot<YOUR_TOKEN>/sendMessage?chat_id=<YOUR_CHAT_ID>&text=test"
   ```

**API key errors**
Double-check `.env` uses the **Paper** Alpaca keys (not Live) and has no trailing spaces.

**Bot missed Monday pre-market**
Not a problem — `./start.sh` on Monday will catch up all missed agents automatically.

**Bot skipping BUYs with "bearish sentiment blocks BUY"**
The research agent scored that symbol below −10 (bearish news). This is intentional — the bot won't buy into bad-news drops even when technicals look strong. Adjust `AUTO_BUY_MIN_SENTIMENT` in `config.py` if you want a looser gate (e.g., −20).

**`trade_journal.json` was reset / corrupt file backup appeared**
A `.corrupt.json` backup was created automatically. The journal is reset to an empty list and trading continues. Backups older than 7 days are deleted at next startup. The learning agent will work correctly from the reset file.
