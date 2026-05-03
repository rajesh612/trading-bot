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
| 4:30 PM | Daily Email Report | P&L summary, trade log, updated weights (optional — needs Gmail config) |

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
- **Position reconciliation at startup** — syncs `open_positions.json` with Alpaca's actual state. Handles bracket fills that happened while offline and crashed mid-entry restores.
- **Swing position safety** — reconciliation skips swing positions (intentional holds); force-close at 3:55 PM also skips them.
- **Daily closed list** — `daily_closed.json` prevents re-entering a symbol on the same day, even across crash/restart cycles.

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
| `API_BUDGET_NORMAL_CEILING` | `$1.00` | Daily Claude spend cap (normal conditions) |
| `API_BUDGET_ELEVATED_CEILING` | `$1.50` | Daily Claude spend cap (CRISIS / high-gain day) |

Restart after any `config.py` change: `./stop.sh && ./start.sh`

---

## Setting Up the Daily Email Report

At 4:30 PM ET each trading day the bot emails a P&L summary.

**Create a Gmail App Password:**
1. [myaccount.google.com](https://myaccount.google.com) → Security → 2-Step Verification → App Passwords → Create
2. Add to `.env`:
   ```
   EMAIL_SENDER=your.gmail@gmail.com
   EMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
   EMAIL_RECIPIENT=your@email.com
   ```

Leave `EMAIL_SENDER` blank to disable.

---

## Auto-Start/Stop

| Event | ET |
|---|---|
| Auto-start | 8:30 AM |
| Auto-stop | 4:30 PM |

### macOS — LaunchAgent

```bash
./setup_launchagent.sh            # Install
./setup_launchagent.sh uninstall  # Remove
```

If your laptop is asleep at the scheduled time, launchd fires when you open the lid.

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
| `trade_journal.json` | Full trade history with P&L, Claude reasoning, regime at entry |
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

---

## Running Tests

```bash
# Fast unit tests — no API calls, no .env required
.venv/bin/pytest tests/test_foundation.py -v -m unit

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
```bash
# Manually clear the lockout in macro_regime.json:
python3 -c "
import json; f=open('macro_regime.json')
d=json.load(f); f.close()
d['flash_crash_active']=False; d.pop('flash_crash_until',None)
json.dump(d,open('macro_regime.json','w'),indent=2)
print('Lockout cleared.')
"
./stop.sh && ./start.sh
```

**API key errors**
Double-check `.env` uses the **Paper** Alpaca keys (not Live) and has no trailing spaces.

**Bot missed Monday pre-market**
Not a problem — `./start.sh` on Monday will catch up all missed agents automatically.
