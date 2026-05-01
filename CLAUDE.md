# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the Bot

```bash
./start.sh        # Start bot in background (creates .venv if missing, logs to trading_bot.log)
./stop.sh         # Stop bot gracefully (SIGTERM → SIGKILL after 2s)
tail -f trading_bot.log   # Monitor live output
```

The bot runs weekdays only, 9:30 AM – 3:55 PM ET. Use the launchers below for auto-start/stop, or run manually.

**Auto-start/stop — macOS:**
```bash
./setup_launchagent.sh            # Install (starts 8:30 AM ET, stops 4:30 PM ET, Mon–Fri)
./setup_launchagent.sh uninstall  # Remove
```

**Auto-start/stop — Linux cloud:**
```bash
./setup_cron.sh            # Install (CRON_TZ=America/New_York, DST-aware)
./setup_cron.sh uninstall  # Remove
```

## Development Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python3 main.py   # Run directly (foreground)
```

## Testing & Backtesting

```bash
pytest test_agents.py -v               # All tests
pytest test_agents.py -v -m unit       # Unit tests only (no API calls)
pytest test_agents.py -v -m live       # Tests requiring live Alpaca credentials

.venv/bin/python3 backtest.py          # 7-day backtest; outputs backtest_trades.csv + backtest_equity.json
```

No linter or formatter is configured.

## Architecture Overview

The bot is a multi-agent, adaptive intraday day trader using Alpaca paper trading and Claude for decisions.

### Scheduler Flow (`main.py`)

APScheduler drives all jobs on ET timezone, weekdays only:

| Time | Job |
|---|---|
| 8:45 AM | `volatility_screener.py` — filter top 50 most volatile tickers into `daily_tradeable.txt` |
| 8:55 AM | `vix_regime.py` — classify market as NORMAL / HIGH_VOL / CRISIS based on VIX vs 90d avg and SPY move |
| 9:00 AM | `watch_list_agent.py` + `research_agent.py` — Claude selects 20–30 stocks; sentiment fetched from RSS, Reddit, StockTwits |
| 9:15 AM | `research_agent.py` — refresh sentiment |
| Every 10 min (9:30–3:50 PM) | `trading_agent.run_trading_cycle()` — compute technicals + Claude decision for new entry candidates only |
| Every 60 sec (9:30–3:50 PM) | `trading_agent.run_position_monitor()` — enforce SL/TP via two-layer protection |
| 3:55 PM | `trading_agent.force_close_all()` — cancel orders, close all open positions |
| 4:15 PM | `learning_agent.py` — Claude reviews closed trades, updates `indicator_weights.json` and `trading_params.json` |
| Every 30 min | Heartbeat — logs open position count |

### Core Components

**`indicator_engine.py`** — Computes 10+ technical indicators on 5-min bars (VWAP + bands, RSI, MACD, Bollinger Bands, EMA 9/21, ATR, Volume Ratio, Stochastic, OBV). Produces a `composite_score` in [-100, +100] weighted by `indicator_weights.json`. Score > 15 → BUY signal; < -15 → SELL/CLOSE.

**`trading_agent.py`** — Core per-cycle logic:
1. Fetch 100 bars of 5-min data via Alpaca
2. Compute indicators
3. Ask Claude for BUY / HOLD / CLOSE decision, confidence, and custom SL/TP levels
4. BUY → place bracket order, write to `open_positions.json`
5. CLOSE → close manually, write to `trade_journal.json`
6. Position monitor layer 1: detect Alpaca bracket order fills; layer 2: manual close if price breaches SL/TP

**`vix_regime.py`** — Fetches VIX + SPY from Yahoo Finance. CRISIS mode skips all new entries. HIGH_VOL halves position size and tightens RSI threshold.

**`research_agent.py`** — Sentiment from 5 RSS feeds (MarketWatch, CNBC, Benzinga, Yahoo, Bloomberg), 6 Reddit subreddits, and StockTwits. Claude scores each ticker → `research_report.json`.

**`learning_agent.py`** — Post-market: Claude analyzes `trade_journal.json`, adjusts indicator weights (max ±5/indicator/day, sum = 100, min = 0) and trading params (entry hours, RSI thresholds, VWAP slope min, vol multipliers). Persisted to JSON files.

**`alpaca_client.py`** — Thin wrapper around `alpaca-py`. All trading uses paper trading endpoint only.

### Position Sizing

Risk-based: `shares = portfolio × 0.5% / ATR_per_share`, capped at `portfolio × 5% / price`. Scaled down ×0.5 in HIGH_VOL regime; no new entries in CRISIS.

### State / Persistence (all JSON)

| File | Purpose |
|---|---|
| `open_positions.json` | Active positions with entry price, SL, TP |
| `trade_journal.json` | Full transaction history (BUY + CLOSE) |
| `indicator_weights.json` | Learned per-indicator weights (updated daily by learning agent) |
| `trading_params.json` | Learned entry hours, RSI/VWAP thresholds |
| `vix_regime.json` | Today's regime classification |
| `research_report.json` | Cached sentiment scores and catalysts per ticker |
| `daily_closed.json` | Symbols closed today (prevents same-day re-entry) |
| `watch_list.txt` | Today's 20–30 selected tickers |
| `strategy_memory.md` | Freeform notes fed to Claude as context |

### Claude Integration

Model: `claude-sonnet-4-6` (set in `config.py`). All four agents (watch list, trading, research, learning) call Claude with prompt caching (`cache_control: ephemeral`) to reduce API costs. Trading agent expects structured JSON output: `{ action, confidence, stop_loss_pct, take_profit_pct, reasoning }`.

### Key Config (`config.py`)

- `MAX_POSITION_PCT = 0.05` (5% per position)
- `RISK_PER_TRADE_PCT = 0.005` (0.5% ATR-based sizing)
- `STOP_LOSS_PCT = 4.0`, `TAKE_PROFIT_PCT = 5.0` (defaults, overridable by Claude)
- `MAX_DAILY_LOSS_PCT = 0.03` (3% circuit breaker)
- `ENTRY_START_HOUR = 10`, `ENTRY_END_HOUR = 15` (ET)
- `BAR_TIMEFRAME_MINUTES = 5`, `BARS_LOOKBACK = 100`

All API keys loaded from `.env` (Alpaca, Anthropic, Reddit, X/Twitter).
