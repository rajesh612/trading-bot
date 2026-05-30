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
tail -f logs/trading_bot.log
```

**Log files** — `logs/trading_bot.log` rotates automatically at 10 MB (up to 5 backups: `.log.1` … `.log.5`). No manual cleanup needed. Corrupt state file backups (`.corrupt.json`) older than 7 days and leftover `.tmp` files are deleted at every startup. Trade journal entries older than 90 days are moved to `trade_journal_archive.json`.

---

## Daily Schedule (ET, weekdays only)

| Time | Job | What it does |
|---|---|---|
| 8:00 AM | Earnings Calendar | Fetches next-earnings dates + PEAD detection for watch list + tier-1 stocks |
| 8:20 AM | Economic Calendar | Loads FOMC/NFP/CPI event-risk windows for the day |
| 8:45 AM | Volatility Screener | Filters ticker universe to top 50 most volatile |
| 8:55 AM | Macro Regime | 9-state regime classification (CRISIS / BULL_TREND / CHOPPY / etc.) |
| 9:00 AM | Watch List Agent | Claude picks 10–15 stocks for the day |
| 9:15 AM | Research Agent | RSS + Reddit + StockTwits sentiment; writes macro shock score |
| 9:30 AM | Swing Gap Check | Evaluates pre-market gaps on held overnight swing positions |
| Every 10 min (9:30–3:50) | Trading Cycle | Technicals + Claude BUY/HOLD/CLOSE; mode-aware sizing and SL/TP |
| Every 60 sec (9:30–3:50) | Position Monitor | Enforces SL/TP on day-trades; tiered TP on swing positions |
| Every 5 min (9:30–3:50) | Market Monitor | Flash-crash detection via SPY (free, no Claude calls) |
| Every 30 min (9:05–2:35) | Intraday News | Refreshes headlines for active watch list; updates intraday sentiment |
| Every 30 min | Heartbeat | Confirms scheduler is alive; watchdog exits if missed |
| 12:00 PM | Macro Regime Refresh | Midday re-fetch keeps regime data fresh through market close |
| 12:00 PM | Midday Check | Telegram alert if zero BUY trades executed by noon |
| 3:50 PM | Swing EOD Evaluation | Promotes qualifying positions to overnight swing holds |
| 3:55 PM | Force Close | Cancels orders, closes all day-trade positions (swing positions preserved) |
| 4:00 PM | FRED Cache Refresh | Updates Fed/CPI/unemployment macro modifiers |
| 4:15 PM | Learning Agent | Reviews closed trades, updates indicator weights and trading params |
| 4:20 PM | Daily Report | Telegram EOD P&L summary always fires; email report optional (needs Gmail config) |

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
| CATALYST | Post-event momentum (FOMC/CPI/NFP settled); sector confirms, surprise score passes | 5% | 8% | 2.5% portfolio |
| CASH | CRISIS / flash crash | — no entries — | — | — |

---

## Overnight Swing Positions

At 3:50 PM the swing evaluator promotes qualifying day-trades to overnight holds. Eligibility is fully regime-dynamic via `get_swing_params(regime)` (`utils/regime_params.py`):

- **Profit-lock bypass** — positions with unrealised P&L ≥ `profit_lock_pct` (2.0–3.0% by regime) qualify in any non-CRISIS regime, even CHOPPY/DEFENSIVE
- **Min P&L gate** — standard promotion requires ≥ `min_pnl_pct` (0.3% BULL_TREND → 1.0% CRASH_RECOVERY; prevents overnight risk on flat positions)
- **Regime eligibility** — CHOPPY/DEFENSIVE/CRISIS block standard promotion; profit-lock bypass still applies for CHOPPY/DEFENSIVE
- **Sector/sentiment gates** — per-regime floors on sector ETF % change and symbol sentiment

Swing positions use tiered take-profit (40%/30%/30% of position size) and a trailing stop after TP2 fires.

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
The provided `scripts/setup_cron.sh` handles scheduled start/stop only and does not restart on crash.

---

## Customising the Ticker Universe

Edit `tickers.txt` — one ticker per line. The watch list agent selects 10–15 each morning.

---

## Changing Risk Parameters

Edit `config.py`. Key parameters:

| Parameter | Default | Description |
|---|---|---|
| `MAX_POSITION_PCT` | `0.05` | Max 5% of portfolio per day-trade position |
| `MAX_POSITION_USD` | `10_000` | Hard dollar cap per position (0 = disabled) — prevents oversizing on cheap stocks |
| `RISK_PER_TRADE_PCT` | `0.005` | Risk 0.5% per trade (ATR-based sizing) |
| `STOP_LOSS_PCT` | `3.0` | Fallback stop loss % (overridden by mode/regime table) |
| `TAKE_PROFIT_PCT` | `5.0` | Fallback take profit % (overridden by mode/regime table) |
| `ATR_TP_MULT_DEFAULT` | `2.0` | ATR-based TP multiplier — TP = ATR% × this; tuned nightly by learning agent (bounds: 1.0×–4.0×) |
| `MAX_DAILY_LOSS_PCT` | `0.03` | Circuit breaker — halts new entries if down 3% |
| `DAILY_LOSS_WARN_PCT` | `0.02` | Telegram alert when intraday loss hits 2% |
| `AUTO_EXECUTE_COMPOSITE_MIN` | `70` | Reference for auto-execute confidence delta; actual AE threshold accounts for tier floor and regime |
| `COMPOSITE_MIN_NORMAL` | `50` | Minimum composite score to pass pre-Claude filter (NORMAL regime) |
| `COMPOSITE_MIN_HIGH_VOL` | `55` | Minimum composite score in HIGH_VOL / DEFENSIVE regime |
| `SOFT_FLAG_OVERLOAD_THRESHOLD` | `4` | ≥ this many simultaneous soft flags (volume, breadth, Stoch, OR) → hard block |
| `ENTRY_HARD_CLOSE_HOUR` | `13` | Default per-regime hard entry cutoff (1 PM NORMAL; CHOPPY/DEFENSIVE=12, BULL_TREND/OPPORTUNITY=14). Overrides `ENTRY_END_HOUR` earlier. |
| `PROFIT_LOCK_SWING_PCT` | `2.5` | Positions with P&L ≥ this qualify for swing in any non-CRISIS regime (per-regime override in `SwingParams`) |
| `SWING_PROMOTE_MIN_PNL_PCT` | `0.5` | Standard swing promotion floor (per-regime: 0.3 BULL_TREND, 1.0 CRASH_RECOVERY) |
| `STOCH_HARD_VETO_THRESHOLD` | `97` | Stochastic ≥ this → hard block regardless of other signals |
| `STOCH_SOFT_FLAG_THRESHOLD` | `85` | Stochastic ≥ this → soft flag appended to Claude context |
| `REL_VOLUME_MIN` | `0.60` | Volume < 60% of 20-day average → soft flag |
| `BREADTH_WEAK_THRESHOLD` | `0.85` | IWM/SPY session return ratio < this → breadth soft flag |
| `OPENING_RANGE_MINUTES` | `60` | First N minutes define the opening range for position detection |
| `SENTIMENT_HARD_BLOCK` | `-15` | Research sentiment below this → hard skip |
| `API_BUDGET_NORMAL_CEILING` | `$1.00` | Daily Claude spend cap (normal conditions) |
| `API_BUDGET_ELEVATED_CEILING` | `$1.50` | Daily Claude spend cap (CRISIS / high-gain day) |
| `DRAWDOWN_WARN_PCT` | `2.0` | Alert when a position's unrealised loss ≥ this % (above stop) |
| `TRAILING_STOP_PCT_FALLBACK` | `0.03` | 3% trailing stop used when ATR is unavailable |
| `LEARNING_ROLLING_DAYS` | `5` | Days of trade history used by learning agent (prevents single-day overfit) |
| `TRADE_JOURNAL_MAX_DAYS` | `90` | Journal entries older than this are archived at startup |
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
  ┌──────────────────────┬──────────────────────────────────────────────────────────┐
  │        Event         │                         Message                          │
  ├──────────────────────┼──────────────────────────────────────────────────────────┤
  │ Bot starts           │ 🟢 Bot started — Regime: BULL_TREND | Open: 0            │
  ├──────────────────────┼──────────────────────────────────────────────────────────┤
  │ Bot stops            │ 🔴 Bot stopped                                           │
  ├──────────────────────┼──────────────────────────────────────────────────────────┤
  │ BUY order(s)         │ 📈 BUY NVDA — 15 shares @ $890.20, SL $856, TP $935      │
  │                      │    (batched: one message per cycle for multiple entries) │
  ├──────────────────────┼──────────────────────────────────────────────────────────┤
  │ CLOSE (win)          │ ✅ CLOSE NVDA — +$234.00 (+3.8%)                         │
  ├──────────────────────┼──────────────────────────────────────────────────────────┤
  │ CLOSE (loss)         │ 🔻 CLOSE AAPL — -$45.00 (-1.2%)                          │
  ├──────────────────────┼──────────────────────────────────────────────────────────┤
  │ Position drawdown    │ ⚠️ NVDA drawdown -2.1% — approaching stop               │
  ├──────────────────────┼──────────────────────────────────────────────────────────┤
  │ API budget 75%       │ ⚠️ API budget at 75% ($0.75 / $1.00)                    │
  ├──────────────────────┼──────────────────────────────────────────────────────────┤
  │ Bracket timeout      │ ⚠️ BRACKET TIMEOUT AAPL: fill-slippage correction timed  │
  │                      │    out. Journaled with indicative entry ~$XXX — verify   │
  ├──────────────────────┼──────────────────────────────────────────────────────────┤
  │ Stale research       │ ⚠️ Research report is 95 min old — sentiment may drift   │
  ├──────────────────────┼──────────────────────────────────────────────────────────┤
  │ Sector flip          │ 🔄 Sector rotation detected: SMH -1.8%                  │
  ├──────────────────────┼──────────────────────────────────────────────────────────┤
  │ Correlated exposure  │ ⚠️ Correlated exposure: NVDA + AMD both open (semis)     │
  ├──────────────────────┼──────────────────────────────────────────────────────────┤
  │ Dead-day alert       │ ⚠️ Dead-day: 4 consecutive cycles with 0 candidates      │
  ├──────────────────────┼──────────────────────────────────────────────────────────┤
  │ Claude disabled      │ 🚫 Claude disabled — no trades until restart             │
  ├──────────────────────┼──────────────────────────────────────────────────────────┤
  │ Job failure          │ ❌ Job failed: force_close — JSONDecodeError             │
  ├──────────────────────┼──────────────────────────────────────────────────────────┤
  │ Corrupt file         │ ⚠️ Corrupt state file reset: trade_journal.json         │
  ├──────────────────────┼──────────────────────────────────────────────────────────┤
  │ Regime shift         │ 🚨 Regime change: NORMAL → CRISIS                        │
  ├──────────────────────┼──────────────────────────────────────────────────────────┤
  │ Flash crash          │ 🚨 FLASH CRASH detected — CRISIS lockout                 │
  ├──────────────────────┼──────────────────────────────────────────────────────────┤
  │ EOD summary          │ 📊 P&L: +$127.40 (+1.27%) | 4 trades | 75% win          │
  │                      │    Budget: 68% | Cycles: 24 | Regime flips: 1           │
  └──────────────────────┴──────────────────────────────────────────────────────────┘

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
./scripts/setup_launchagent.sh
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
./scripts/setup_launchagent.sh uninstall
```

---

### Testing the LaunchAgent outside market hours

If you want to confirm it works right now (without waiting until tomorrow morning), trigger it manually:

```bash
launchctl start trading_bot_start
sleep 5
tail -f logs/launchd_trading_bot.log
```

You should see startup log lines within 5 seconds and a Telegram "🟢 Bot started" message if Telegram is configured. Press `Ctrl+C` to stop tailing. Run `./stop.sh` when done testing.

> **This is for testing only — you do NOT need to run `launchctl start` each day.**

**Two log files — know the difference:**

| File | Written by | When |
|---|---|---|
| `logs/launchd_trading_bot.log` | LaunchAgent (auto-start) | When launchd fires the bot |
| `logs/trading_bot.log` | Manual `./start.sh` | When you start the bot yourself |

If you started via LaunchAgent but see nothing in `logs/launchd_trading_bot.log`, Full Disk Access is likely the issue (Step 2 above).

---

### Linux — cron

```bash
./scripts/setup_cron.sh            # Install (CRON_TZ=America/New_York, DST-aware)
./scripts/setup_cron.sh uninstall  # Remove
```

---

## State Files

All state files live under `data/state/`.

| File | Purpose |
|---|---|
| `open_positions.json` | Active day-trade positions (entry price, SL, TP, mode) |
| `swing_positions.json` | Active overnight swing positions (tiered TP state) |
| `trade_journal.json` | Recent trade history — entries older than 90 days archived at startup |
| `trade_journal_archive.json` | Long-term trade archive (auto-populated from `trade_journal.json`) |
| `indicator_weights.json` | Per-indicator weights (updated nightly; clamped to 0.3×–2.0× defaults) |
| `trading_params.json` | Learned entry thresholds + `atr_tp_multiplier` (updated nightly from 5-day rolling window) |
| `macro_regime.json` | Today's 9-state regime + macro health score + flash-crash lockout |
| `earnings_calendar.json` | Next earnings dates + PEAD data per symbol (refreshed 8:00 AM) |
| `economic_calendar.json` | Today's FOMC/NFP/CPI events + 45-min risk windows |
| `research_report.json` | Sentiment scores + macro shock score + sector sentiment |
| `api_budget.json` | Daily Claude token spend tracker (TOCTOU-safe; enforces $1.00/$1.50 cap) |
| `metrics.jsonl` | Per-cycle telemetry: regime, candidates, entries, closes, budget%, latency |
| `strategy_memory.md` | Freeform strategy notes written by learning agent, fed to Claude |
| `daily_closed.json` | Symbols closed today — prevents same-day re-entry after crash |
| `bot_health.json` | Crash loop counter — restart frequency tracker (auto-created, reset on clean stop) |

---

## Running Tests

```bash
# Fast unit tests — no API calls, no .env required
.venv/bin/pytest tests/ -v -m unit

# Quiet mode (less output)
.venv/bin/pytest tests/ -m unit -q
```

---

## Backtesting & Tuning

Two tools, two jobs — use them in order:

1. **`backtest.py`** — verify performance with current config
2. **`param_sweep.py`** — find better params across N combinations; then re-run `backtest.py` to confirm

### Verify (backtest.py)

```bash
# Last 30 trading days (default) — tee to a file so the full output is preserved
.venv/bin/python3 backtest.py 2>&1 | tee /tmp/bt_30d.txt

# Last N trading days (60d recommended — covers multiple regime transitions)
.venv/bin/python3 backtest.py --days 60 2>&1 | tee /tmp/bt_60d.txt

# Specific date range
.venv/bin/python3 backtest.py --start 2026-04-01 --end 2026-04-30 2>&1 | tee /tmp/bt_custom.txt
```

Weekends and NYSE holidays are automatically excluded. No Claude API calls are made. Picks up `trading_params.json` automatically so the backtest reflects your bot's current learned tuning.

**Outputs** (written to project root, overwritten each run — not state files):
- `backtest_trades.csv` — every trade with entry/exit timestamps, P&L, and exit reason
- `backtest_equity.json` — daily equity curve

### Tune (param_sweep.py)

When exit metrics suggest a threshold is off — composite floor too low, TP too wide, SL ceiling wrong — use the sweep to search instead of hand-editing and re-running manually. Data is fetched once; all combinations run in-process (~7 min for 28 combos on 30d).

```bash
# 30d sweep (28 combos) — ranked CSV + terminal table
.venv/bin/python scripts/param_sweep.py

# 60d sweep, show top 15
.venv/bin/python scripts/param_sweep.py --days 60 --top 15

# Custom date range, custom output file
.venv/bin/python scripts/param_sweep.py --start 2026-04-01 --end 2026-05-28 --out sweep_apr_may.csv
```

Edit `SWEEP_AXES` at the top of `scripts/param_sweep.py` to define the search space. Each axis is a list of values; the script runs the cartesian product plus a no-override baseline. Results are written to `sweep_results.csv` (or `--out` path) ranked by total P&L.

Overrides are applied via `unittest.mock.patch` — production code is never modified during a sweep run. Only `NORMAL/MEDIUM` day-trade parameters are wired by default; extend the override factories in the script to sweep other regimes or modes.

After applying a sweep winner to `config.py`, re-run `backtest.py --days 60` to confirm the gain is real and max drawdown hasn't worsened.

### Interpreting Results

| Metric | What to look for |
|---|---|
| Force-close % | Target 40–50%. Above 55% means TP is too wide for intraday hold times |
| Win rate | Healthy range: 52–65%. Below 50% = entry quality issue |
| SL count / avg loss | Watch avg SL loss magnitude — lowering SL ceiling reduces per-trade loss but increases hit frequency |
| Swing win rate | Swing setups are selective — 70%+ at 5–10% avg winner is healthy |
| Max drawdown | Target < 3% (matches `MAX_DAILY_LOSS_PCT` circuit breaker) |
| Alpha vs benchmark | Printed automatically — compares total return to equal-weight buy-hold of the same watch list |

**Tip:** Run 60d minimum for parameter validation. The 30d window covers only NORMAL and BULL_TREND regimes; HIGH_VOL / CHOPPY / DEFENSIVE effects only appear in longer windows. Use 30d for fast iteration, 60d to confirm before committing a change.

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
grep RECONCILE logs/trading_bot.log | tail -20
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
2. Confirm `ANTHROPIC_API_KEY` is set — if it is missing the bot exits before sending any Telegram alert.
3. Check the correct log file: `logs/launchd_trading_bot.log` if started via LaunchAgent, `logs/trading_bot.log` if started manually via `./start.sh`.
4. If the log file is empty after a LaunchAgent start, Full Disk Access for `/bin/bash` is likely missing — see the macOS LaunchAgent setup steps above.
5. Send a test message to confirm Telegram credentials work:
   ```bash
   curl -s "https://api.telegram.org/bot<YOUR_TOKEN>/sendMessage?chat_id=<YOUR_CHAT_ID>&text=test"
   ```

**API key errors**
Double-check `.env` uses the **Paper** Alpaca keys (not Live) and has no trailing spaces.

**Bot missed Monday pre-market**
Not a problem — `./start.sh` on Monday will catch up all missed agents automatically.

**Bot skipping BUYs with "sentiment < hard block"**
The research agent scored that symbol below `SENTIMENT_HARD_BLOCK` (default −2). This is intentional — the bot won't buy into bad-news drops even when technicals look strong. Adjust `SENTIMENT_HARD_BLOCK` in `config.py` for a looser gate.

**`trade_journal.json` was reset / corrupt file backup appeared**
A `.corrupt.json` backup was created automatically. The journal is reset to an empty list and trading continues. Backups older than 7 days are deleted at next startup. The learning agent will work correctly from the reset file.
