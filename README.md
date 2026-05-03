# Multi-Agent AI Trading Bot

An intraday day-trading bot that uses Claude AI for decisions and Alpaca for paper trade execution. It runs fully autonomously on weekdays, learns from every trade, and improves its own strategy over time.

**Paper trading only — no real money is ever at risk.**

---

## Prerequisites

- macOS (tested on macOS Ventura+)
- Python 3.11 or later (`python3 --version`)
- An [Alpaca](https://app.alpaca.markets) account (free)
- An [Anthropic](https://console.anthropic.com) account
- A [FRED](https://fred.stlouisfed.org/docs/api/api_key.html) API key (free, optional but recommended for macro regime)

---

## First-Time Setup

### 1. Clone and enter the project

```bash
cd /path/to/trading-bot
```

### 2. Create your `.env` file

Copy the example and fill in your keys:

```bash
cp .env.example .env
```

Open `.env` in any editor and set these values:

```
# ── Required ──────────────────────────────────────────────────────────────────
ALPACA_API_KEY=your_alpaca_paper_key
ALPACA_SECRET_KEY=your_alpaca_paper_secret
ANTHROPIC_API_KEY=your_anthropic_key

# ── Recommended (free) ────────────────────────────────────────────────────────
FRED_API_KEY=your_fred_key
MARKET_DATA_PROVIDER=yahoo    # yahoo | alpaca | hybrid

# ── Optional (bot degrades gracefully if blank) ────────────────────────────────
POLYGON_API_KEY=
REDDIT_CLIENT_ID=
REDDIT_CLIENT_SECRET=
REDDIT_USER_AGENT=TradingBot/1.0
X_BEARER_TOKEN=
```

**Where to get the keys:**

| Key | Where to get it | Cost |
|---|---|---|
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | [app.alpaca.markets](https://app.alpaca.markets) → API Keys → **Paper** tab → Generate | Free |
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) → API Keys → Create Key | Per usage (~$0.30–0.55/day) |
| `FRED_API_KEY` | [fred.stlouisfed.org/docs/api/api_key.html](https://fred.stlouisfed.org/docs/api/api_key.html) → instant | Free |
| `POLYGON_API_KEY` (optional) | [polygon.io/dashboard/keys](https://polygon.io/dashboard/keys) → free tier | Free (5 req/min) |
| Reddit keys (optional) | [reddit.com/prefs/apps](https://www.reddit.com/prefs/apps) → Create app → choose **script** type | Free |
| `X_BEARER_TOKEN` (optional) | Requires X Basic API | $100/mo — skip unless needed |

### Market data provider — `MARKET_DATA_PROVIDER`

| Value | Bars | Real-time price | When to use |
|---|---|---|---|
| `yahoo` (default) | Yahoo Finance — full volume, free | 15-min delayed | Best free option for paper trading |
| `alpaca` | Alpaca SIP (live) or IEX (paper, ~2% volume) | Real-time on live, paper on paper | Once you upgrade to live + Algo Trader Plus |
| `hybrid` | Yahoo for bars | Alpaca paper for real-time | Recommended balance during paper trading |

> **Important:** Always use your Alpaca **Paper** keys to start. The bot enforces `paper=True` in all API calls. Switching to live trading is covered in the section below.

### 3. Start the bot

```bash
./start.sh
```

`start.sh` automatically:
- Creates the Python virtualenv (`.venv/`) if it does not exist
- Installs all dependencies from `requirements.txt`
- Starts the bot in the background
- Detects if it was started mid-day and catches up any missed pre-market agents

You do not need to run `pip install` manually.

---

## Starting and Stopping

```bash
./start.sh    # Start the bot in the background
./stop.sh     # Stop the bot gracefully
```

**Check if the bot is running:**
```bash
pgrep -f "python3 main.py" && echo "Bot is running" || echo "Bot is stopped"
```

Note: `trading_bot.pid` is deleted by `stop.sh` and recreated by `start.sh`. Use `pgrep` for a reliable check.

**Watch live output:**
```bash
tail -f trading_bot.log
```

---

## Daily Schedule (ET, weekdays only)

| Time | Agent | What it does |
|---|---|---|
| 8:45 AM | Volatility Screener | Filters your ticker universe down to the 50 most volatile |
| 8:55 AM | VIX Regime | Classifies market as NORMAL / HIGH_VOL / CRISIS |
| 9:00 AM | Watch List Agent | Picks 10–15 best stocks for the day |
| 9:15 AM | Research Agent | Scrapes news (RSS, Reddit, StockTwits), scores sentiment via Claude |
| Every 10 min (9:30–3:50) | Trading Agent | Computes technicals + asks Claude for BUY/HOLD/CLOSE decisions |
| Every 60 sec (9:30–3:50) | Position Monitor | Enforces stop-loss and take-profit on open positions |
| 3:55 PM | Force Close | Cancels orders, closes all positions — no overnight holds |
| 4:15 PM | Learning Agent | Reviews trades, updates indicator weights and trading params |

If you start the bot mid-day, it automatically catches up on any agents it missed.

---

## Customising the Ticker Universe

Edit `tickers.txt` — one ticker per line. The watch list agent selects 10–15 from this list each morning based on volume, volatility, and news.

```
AAPL
NVDA
TSLA
...
```

---

## Changing Risk Parameters

Edit `config.py`:

| Parameter | Default | Description |
|---|---|---|
| `MAX_POSITION_PCT` | `0.05` | Max 5% of portfolio per position |
| `RISK_PER_TRADE_PCT` | `0.005` | Risk 0.5% per trade (ATR-based sizing) |
| `STOP_LOSS_PCT` | `4.0` | Default stop loss (%) — Claude can override per trade |
| `TAKE_PROFIT_PCT` | `5.0` | Default take profit (%) — Claude can override per trade |
| `MAX_DAILY_LOSS_PCT` | `0.03` | Circuit breaker — halts new entries if down 3% on the day |
| `ENTRY_START_HOUR` | `10` | No entries before 10:00 AM (avoids open noise) |
| `ENTRY_END_HOUR` | `15` | No entries at or after 3:00 PM |

Restart the bot after any `config.py` change: `./stop.sh && ./start.sh`

---

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
EMAIL_RECIPIENT=your@email.com
```

`EMAIL_RECIPIENT` defaults to `EMAIL_SENDER` if left blank. Leave `EMAIL_SENDER` blank to disable the report entirely — the bot runs normally without it.

**Restart the bot after adding email credentials:**
```bash
./stop.sh && ./start.sh
```

---

## Auto-Start/Stop

Both launchers call the same `start.sh` / `stop.sh` and schedule identical market hours. Use the one that matches your platform.

| Event | ET | PT (macOS local) |
|---|---|---|
| Auto-start | 8:30 AM | 5:30 AM |
| Auto-stop  | 4:30 PM | 1:30 PM |

### macOS (local machine) — LaunchAgent

Run once to install:

```bash
./setup_launchagent.sh
```

If your laptop is asleep at the scheduled time, launchd fires the job the moment you open the lid. Logs go to `launchd_trading_bot.log`.

```bash
./setup_launchagent.sh uninstall   # Remove both auto-start and auto-stop
```

### Linux (cloud server) — cron

Run once to install:

```bash
./setup_cron.sh
```

Uses `CRON_TZ=America/New_York` so DST shifts are handled automatically and other cron jobs on the server are not affected. Logs go to `cron_trading_bot.log`.

```bash
./setup_cron.sh uninstall   # Remove both auto-start and auto-stop
crontab -l                  # Verify entries are gone
```

> **Note:** `CRON_TZ` requires a modern cron daemon (`cronie` or `vixie-cron`), which is standard on Ubuntu, Debian, RHEL, and Amazon Linux.

---

## State Files

The bot maintains these files in the project directory:

| File | Purpose |
|---|---|
| `open_positions.json` | Currently held positions |
| `trade_journal.json` | Full trade history with P&L and Claude's reasoning |
| `indicator_weights.json` | Per-indicator weights (updated nightly by learning agent) |
| `trading_params.json` | Learned entry thresholds (updated nightly) |
| `strategy_memory.md` | Freeform strategy notes fed to Claude as context |
| `research_report.json` | Today's sentiment scores per ticker |
| `watch_list.txt` | Today's selected tickers |
| `vix_regime.json` | Today's market regime classification |

---

## Running Tests

```bash
.venv/bin/python3 -m pytest test_agents.py -v -m unit    # Unit tests (no API calls)
.venv/bin/python3 -m pytest test_agents.py -v -m live    # Live tests (requires .env)
.venv/bin/python3 -m pytest test_agents.py -v            # All tests
```

---

## Backtesting

```bash
.venv/bin/python3 backtest.py
```

Runs a 7-day backtest and outputs:
- `backtest_trades.csv` — every simulated trade
- `backtest_equity.json` — equity curve

---

## Switching from Paper to Live Trading

When you're ready to trade real money:

### Alpaca fees (live account)

| Item | Cost |
|---|---|
| Stocks: commission per trade | **$0** (Alpaca is commission-free) |
| Stocks: regulatory fees on sells | ~$0.01–$0.05 per typical trade (SEC + TAF, mandatory for all US brokers) |
| Real-time market data (SIP) | $99/mo via **Algo Trader Plus** subscription, or stay on free IEX feed (~2% volume) |
| Margin interest | Only if you enable margin (off by default) |
| Options | $0 commission + per-contract fees ($0.50/contract on basic, $0 on Algo Trader Plus) |
| Crypto | Spread-based pricing (this bot does not trade crypto) |

The largest hidden cost is **market data quality**. Paper Alpaca and free live Alpaca both give you IEX-only data (~2% of US equity volume). For full SIP-quality real-time data you need the **Algo Trader Plus** subscription ($99/mo) — but Yahoo Finance via `MARKET_DATA_PROVIDER=yahoo` already gives you full-volume bars for free, so the subscription is optional unless you need true real-time quotes.

### Pattern Day Trader rule

If your **live** account is below $25,000 and you make more than 3 day trades in 5 business days, FINRA rules will lock the account. This is a regulatory rule, not Alpaca-specific. The bot does not enforce it — manage your day-trade count manually if you start below $25K.

### Switch the keys

1. Generate live keys: [app.alpaca.markets](https://app.alpaca.markets) → API Keys → **Live** tab → Generate
2. Replace `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` in `.env`
3. Edit `config.py` — change `ALPACA_PAPER_URL` to `ALPACA_LIVE_URL = "https://api.alpaca.markets"` and update the `AlpacaClient` constructor to use `paper=False` (this is intentionally manual to prevent accidents)
4. (Recommended) Subscribe to Algo Trader Plus and switch `MARKET_DATA_PROVIDER=alpaca` for SIP-quality real-time data
5. Restart: `./stop.sh && ./start.sh`

> **Strongly recommend** running the bot for 4+ weeks on paper with consistent profitability before switching to live.

---

## Troubleshooting

**Bot won't start — "Another instance is already running"**
```bash
./stop.sh && ./start.sh
```

**Positions out of sync with Alpaca**
The bot reconciles local state with Alpaca every time it starts. If something looks wrong, check the log for `RECONCILE:` lines:
```bash
grep RECONCILE trading_bot.log | tail -20
```

**API key errors**
Double-check your `.env` has no trailing spaces and uses the **Paper** Alpaca keys (not Live). Re-run the bot after fixing.

**Bot ran through the weekend and missed Monday pre-market**
Not a problem — `./start.sh` on Monday morning will catch up all missed agents before the scheduler takes over.
