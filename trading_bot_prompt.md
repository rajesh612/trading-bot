# Intraday AI Trading Bot — System Specification

---

## What You Are Building

A multi-agent intraday day trading bot for US equities. It runs on weekdays during market hours, uses Alpaca for paper (or live) trading, and uses Claude as the decision-making intelligence. The system is designed to be cost-efficient — Claude is called only when genuinely needed, using batched prompts and signal pre-filtering.

---

## Tech Stack

- **Language**: Python 3.11+
- **Scheduler**: APScheduler (BlockingScheduler, ET timezone)
- **Broker**: Alpaca (paper trading endpoint; `alpaca-py` library)
- **AI**: Anthropic Claude (`claude-sonnet-4-6`) via `anthropic` Python SDK
- **Data**: Alpaca market data API (5-min OHLCV bars via IEX feed), Yahoo Finance (VIX/SPY), RSS feeds, StockTwits, Reddit (public JSON API), optional X/Twitter (Basic API)
- **State**: JSON files on disk (no database)
- **Single-instance protection**: `fcntl.flock` exclusive lock on `trading_bot.lock`

---

## Daily Schedule (all times US Eastern, weekdays only)

| Time | Job |
|---|---|
| 8:45 AM | Volatility screener — fetch daily bars for universe, score by ATR% and volume spike, write top 50 to `daily_tradeable.txt` |
| 8:55 AM | VIX regime — fetch VIX + SPY from Yahoo Finance, classify market as `NORMAL` / `HIGH_VOL` / `CRISIS`, write to `vix_regime.json` |
| 9:00 AM | Watch list agent — Claude selects 10–15 stocks from `daily_tradeable.txt` based on volatility metrics |
| 9:15 AM | Research agent — for each watch-list stock, fetch news + social sentiment, ask Claude for a sentiment score and strategy notes, write to `research_report.json` |
| Every 10 min (9:30–3:50 PM) | Trading cycle — compute technicals, ask Claude only for new entry candidates, place bracket orders |
| Every 60 sec (9:30–3:50 PM) | Position monitor — two-layer SL/TP protection |
| 3:55 PM | Force close — cancel all orders, close all open positions |
| 4:15 PM | Learning agent — Claude reviews closed trades, updates indicator weights and trading params |
| 4:30 PM | Daily report — Python formats trade summary, P&L, watch list, indicator weights and sends email |
| Every 30 min | Heartbeat log |

---

## Agent 1: Watch List Agent (`watch_list_agent.py`)

**When**: 9:00 AM daily
**Input**: Up to 50 tickers scored by: `vol_spike` (latest volume / 20-day mean), `atr_pct` (14-day ATR as % of price), `gap_pct` (overnight gap %)
**Claude calls**: 1 call per day
**Output**: `watch_list.txt` — 10 to 15 ticker symbols

**Claude system prompt**:
> You are an expert day trader selecting the best stocks to trade today. Select 10–15 stocks from the list provided. Prefer: vol_spike > 1.5, atr_pct > 1.5%, notable gap %. Return ONLY a JSON array of ticker symbols.

**Claude user message**: JSON array of all scored stocks with their metrics.
**Claude output**: `["AAPL", "TSLA", ...]`

---

## Agent 2: Research Agent (`research_agent.py`)

**When**: 9:15 AM daily
**Input**: Each stock on the watch list
**Claude calls**: 1 call per stock (15 calls max), all sharing a **cached system prompt**
**Output**: `research_report.json` — per-stock sentiment scores, catalyst, risk flag, divergence alert, strategy notes

**Data sources per stock**:
- Yahoo Finance RSS + Benzinga RSS (up to 10 stock-specific headlines, summary truncated to 400 chars)
- 5 market-wide RSS feeds (MarketWatch, CNBC, Benzinga, Yahoo, Bloomberg) — top 6 headlines, placed in the **cached system prompt** so it is sent once, not repeated per stock
- StockTwits (top 5 messages, bullish/bearish count)
- Reddit (top 5 posts across 6 subreddits: wallstreetbets, stocks, investing, options, StockMarket, SecurityAnalysis)
- X/Twitter (top 5 tweets by engagement — requires X Basic API at $100/mo, degrades gracefully if absent)

**Caching strategy**: Market-wide news is embedded in the system prompt with `cache_control: ephemeral`. All 15 per-stock calls reuse the same cached system prompt, paying cache-read price (~10% of input) for calls 2–15.

**Claude system prompt** (cached, built once):
> You are a financial analyst combining institutional news with retail social media sentiment. Source weighting: News > StockTwits > Reddit > X. Flag divergence (news bearish + social very bullish) as a potential squeeze setup. [Today's market context: {top 6 market headlines}]

**Claude user message** (per stock, stock-specific only):
```
Symbol: AAPL
=== STOCK NEWS === ...
=== STOCKTWITS === ... (5 snippets)
=== REDDIT === ... (5 posts)
=== X === ... (5 tweets)
```

**Claude output** (JSON, max 800 tokens):
```json
{
  "news_sentiment_score": -40,
  "news_sentiment_label": "BEARISH",
  "social_sentiment_score": 60,
  "social_sentiment_label": "BULLISH",
  "combined_sentiment_score": 15,
  "combined_sentiment_label": "NEUTRAL",
  "key_catalyst": "Earnings beat Q1",
  "risk_flag": "Tariff exposure",
  "divergence_alert": "Potential short squeeze — news bearish but WSB very bullish",
  "summary": "Two sentence summary.",
  "strategy": "2-3 bullet points on intraday approach"
}
```

---

## Indicator Engine (`indicator_engine.py`)

Computes 10+ indicators on 100 bars of 5-minute OHLCV data from Alpaca, plus 8 days of daily bars for volume context. Produces a `composite_score` in [-100, +100] and a `signal` string.

| Indicator | Notes |
|---|---|
| VWAP + ±1σ/±2σ bands | Intraday anchor |
| RSI (14) + slope | Rate of change over last 5 bars |
| MACD histogram | 12/26/9 |
| Bollinger Bands | 20-period, 2σ |
| EMA 9 / EMA 21 | Trend direction |
| ATR (14) | Used for position sizing |
| Stochastic %K/%D | Overbought/oversold |
| OBV | Volume-weighted price direction |
| VWAP slope | % drift over last 10 bars |
| Daily volume ratio | Latest day volume / 5-day mean |

**Composite score**: weighted sum of individual indicator signals, weights loaded from `indicator_weights.json` (updated nightly by learning agent). Score > 15 → BUY signal. Score < -15 → SELL signal.

---

## Agent 3: Trading Agent — Core Cycle (`trading_agent.py`)

**When**: Every 10 minutes, 9:30 AM – 3:50 PM
**Clock**: APScheduler cron `minute="*/10"`, ET timezone

### The cycle has 4 phases:

**Phase 1 — Fetch and compute**
For every stock on the watch list: fetch 100 bars of 5-min data + 8 days of daily bars from Alpaca, compute all indicators. Collect `(symbol, IndicatorValues, existing_position)` tuples.

**Phase 2 — Filter (critical for token cost)**
Three-way split:

| Condition | Action | Claude called? |
|---|---|---|
| Has open position with bracket order | Log `BRACKET-MANAGED` with P&L | **No** — Alpaca bracket + position monitor handle exits |
| No position, composite score > 15 | Add to `needs_claude` list | **Yes** — price has moved, re-evaluate |
| No position, composite score ≤ 15 | Log `SKIP-CLAUDE` | **No** — signal too weak, answer is always HOLD |

**Phase 3 — Batched Claude call**
Stocks in `needs_claude` are grouped into batches of up to 5. One API call per batch.

**System prompt** (same for all batches in a day, ephemeral-cached):
> You are an expert intraday day trader making final buy/sell decisions. You receive technical indicators, news sentiment, and a strategy memory. You may OVERRIDE technical signals when news sentiment strongly contradicts them. Prioritise capital preservation. Never hold overnight. Current indicator weights: {json}. [Optional: Today's strategy memory: {text, capped at 4000 chars}]

**User message** (all stocks in batch, compact format):
```
Decisions needed for N stocks:

==AAPL== composite=+42.1 signal=BUY
  P=$185.32 VWAP=$183.40(↑) RSI=61.2(Δ+3.1) MACD=0.0412
  BB=[182.10,188.40] EMA9=184.20 EMA21=182.80 ATR=1.2340
  Stoch=68.4/65.2 Vol=1.8x
  NEWS: BULLISH(+45) catalyst=Earnings beat risk=Tariff exposure
  Strong Q1 beat, raised guidance.

==TSLA== composite=+28.3 signal=BUY
  ...

Rules: BUY only if no open position and signal clear. HOLD otherwise.
Respond in JSON keyed by symbol: {"AAPL": {"action":"BUY", "confidence":80,
"reasoning":"1 sentence", "stop_loss_pct":3.5, "take_profit_pct":5.0,
"indicator_signals":{"VWAP":"correct","RSI":"correct",...}}}
```

**Max tokens**: 300 × batch size

**Phase 4 — Act on decisions**
- `BUY`: place Alpaca bracket order (stop-loss + take-profit as limit/stop legs), record in `open_positions.json` and `trade_journal.json`
- `HOLD` or filtered out: log and move on
- `CLOSE`: only reachable if open position was somehow sent to Claude (guard exists); calls `_close_position`

**Circuit breaker**: if the Anthropic API returns HTTP 400, 402, or 429, a module-level flag `_claude_disabled` is set. All subsequent batch calls immediately return HOLD for all stocks without hitting the API. Clears on process restart.

---

## Position Monitor (`trading_agent.run_position_monitor`)

**When**: Every 60 seconds, 9:30 AM – 3:50 PM. Does **not** call Claude.

**Layer 1 — Bracket fill reconciliation**: If Alpaca no longer holds a position, a bracket leg (SL or TP) filled it. Detect this, log the fill, record in `trade_journal.json`, mark symbol as closed for the day (no re-entry).

**Layer 2 — Price safety net**: For positions still open in Alpaca, fetch live price. If price has breached the stored SL or TP percentage, call `alpaca.close_position()` immediately. This is a safety net for cases where bracket orders are pending or delayed.

---

## Position Sizing

```python
risk_amount    = portfolio_value * 0.005           # risk 0.5% per trade
shares_by_risk = risk_amount / ATR                 # ATR-based sizing
shares_by_pct  = (portfolio_value * 0.05) / price  # max 5% per position
qty = min(shares_by_risk, shares_by_pct)
qty = qty * 0.5  # if regime is HIGH_VOL or CRISIS
```

---

## VIX Regime (`vix_regime.py`)

Fetches VIX and SPY daily data from Yahoo Finance at 8:55 AM.

| Regime | Condition | Effect |
|---|---|---|
| `NORMAL` | VIX ≤ 90-day average and SPY change > -1.5% | Full position size, RSI max 75 |
| `HIGH_VOL` | VIX > 90-day average or SPY < -1.5% | Half position size, RSI max 65 |
| `CRISIS` | VIX > 90-day average AND SPY < -1.5% | No new entries at all |

---

## Agent 4: Learning Agent (`learning_agent.py`)

**When**: 4:15 PM daily (after force close)
**Claude calls**: 2 calls per day
**Input**: Today's closed trades (summary fields only — full indicator snapshots stripped to save tokens)

**Call 1 — Performance analysis** (max 3500 tokens output):
Claude reviews all closed trades, judges each indicator's accuracy, and returns JSON with weight adjustments (max ±5 per indicator, must sum to 100) and suggested parameter changes.

**Call 2 — Strategy memory update** (max 1500 tokens output):
Claude rewrites `strategy_memory.md` incorporating today's learnings. Kept under 1000 words. The existing memory is capped at 4000 chars before being sent.

**Outputs written to disk**:
- `indicator_weights.json` — updated weights for next day's composite score
- `trading_params.json` — entry hours, RSI thresholds, VWAP slope min, position multipliers
- `strategy_memory.md` — living strategy notes fed to trading agent system prompt
- `indicator_performance_log.md` — append-only daily log

---

## State Files (all JSON/Markdown on disk)

| File | Purpose |
|---|---|
| `watch_list.txt` | Today's 10–15 tickers |
| `research_report.json` | Per-stock sentiment, catalyst, strategy notes |
| `open_positions.json` | Active positions: entry price, qty, SL%, TP%, bracket prices |
| `trade_journal.json` | Full trade history (BUY + CLOSE entries) |
| `indicator_weights.json` | Per-indicator weights, learned nightly |
| `trading_params.json` | Entry hours, RSI/VWAP thresholds, position multipliers |
| `vix_regime.json` | Today's regime classification |
| `daily_closed.json` | Symbols closed today — prevents same-day re-entry |
| `strategy_memory.md` | Freeform living notes fed to Claude as context |
| `indicator_performance_log.md` | Append-only daily performance log |
| `trading_bot.pid` | PID of running process (for stop.sh) |
| `trading_bot.lock` | `fcntl` exclusive lock — prevents two instances |

---

## Single-Instance Protection

`main.py` calls `fcntl.flock(LOCK_FILE, LOCK_EX | LOCK_NB)` as the very first thing in `main()`. If a second process starts (e.g., two manual starts in quick succession), it fails to acquire the lock and exits immediately with a message. The lock is released automatically when the process exits.

---

## Key Guard Rails

- **Daily loss circuit breaker**: if portfolio P&L drops below -3% on the day, `can_open = False` — no new entries for the rest of the day
- **Entry window**: only open new positions 10:00 AM – 3:00 PM ET (avoids open noise and late-day risk)
- **No re-entry**: once a symbol is closed intraday, it goes into `daily_closed.json` and cannot be re-entered the same day
- **RSI overbought filter**: no BUY if RSI > 75 (NORMAL) or > 65 (HIGH_VOL)
- **VWAP slope filter**: no BUY if VWAP is trending down more than -0.3% over 10 bars
- **Force close**: all positions closed at 3:55 PM regardless — never holds overnight
- **Min buying power**: won't enter if buying power < $500

---

## Token Cost Design Principles

1. **Signal pre-filter**: Claude is only called when composite score > 15 and no open position. Stocks with weak signals or open bracket orders never touch the API.
2. **Batching**: up to 5 stocks sent in one API call (not 1 call per stock).
3. **No Claude for open positions**: bracket orders + 60-second position monitor handle all exits. Claude's view on a bracketed position doesn't change meaningfully every 10 minutes.
4. **Cached system prompt**: indicator weights and strategy memory (stable all day) are marked `cache_control: ephemeral`. Inter-cycle cache hits reduce system prompt cost to ~10%.
5. **Research caching**: market-wide news is in the system prompt (sent once, cached for all 15 per-stock calls).
6. **Capped memory**: `strategy_memory.md` truncated to 4000 chars before sending to Claude.
7. **Slim trade data**: learning agent strips full indicator snapshots from trade records before sending to Claude.
8. **Short outputs**: trading agent responses capped at 300 tokens × batch size; research at 800 tokens; memory update at 1500 tokens.

---

## `.env` Required Keys

```
ALPACA_API_KEY=
ALPACA_SECRET_KEY=
ANTHROPIC_API_KEY=
REDDIT_CLIENT_ID=       # optional
REDDIT_CLIENT_SECRET=   # optional
REDDIT_USER_AGENT=      # optional
X_BEARER_TOKEN=         # optional, requires X Basic API $100/mo
```

---

## Running It

```bash
./start.sh        # start in background (creates .venv if missing)
./stop.sh         # stop all instances cleanly
tail -f logs/trading_bot.log   # monitor live
```

The bot is fully manual — no auto-start on login. Start and stop it yourself on trading days.
