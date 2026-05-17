import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent
STATE_DIR = BASE_DIR / "data" / "state"

# Data files
TICKERS_FILE = BASE_DIR / "tickers.txt"
DAILY_TRADEABLE_FILE = BASE_DIR / "daily_tradeable.txt"
WATCH_LIST_FILE = BASE_DIR / "watch_list.txt"
TRADE_JOURNAL_FILE = STATE_DIR / "trade_journal.json"
INDICATOR_WEIGHTS_FILE = STATE_DIR / "indicator_weights.json"
STRATEGY_MEMORY_FILE = BASE_DIR / "strategy_memory.md"
INDICATOR_PERFORMANCE_LOG = STATE_DIR / "indicator_performance_log.md"
RESEARCH_REPORT_FILE = STATE_DIR / "research_report.json"
OPEN_POSITIONS_FILE = STATE_DIR / "open_positions.json"
DAILY_CLOSED_FILE   = STATE_DIR / "daily_closed.json"

# Alpaca — PAPER TRADING ONLY
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_PAPER_URL = "https://paper-api.alpaca.markets"

# Anthropic
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-sonnet-4-6"

# Social media (optional — bot degrades gracefully if not set)
REDDIT_CLIENT_ID     = os.getenv("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET", "")
REDDIT_USER_AGENT    = os.getenv("REDDIT_USER_AGENT", "TradingBot/1.0")
X_BEARER_TOKEN       = os.getenv("X_BEARER_TOKEN", "")   # requires X Basic API ($100/mo)

# Ticker limits
MAX_TICKERS_DIRECT = 50
WATCH_LIST_MIN = 10
WATCH_LIST_MAX = 15

# Claude API cost controls
CLAUDE_BATCH_SIZE = 10           # Stocks per batched Claude call — matches typical watch-list size
STRATEGY_MEMORY_MAX_CHARS = 3500 # tighter — still above 1024-token cache minimum
STRATEGY_MEMORY_MAX_AGE_DAYS = 7  # reset strategy_memory if unchanged for > N days

# Risk parameters
MAX_POSITION_PCT = 0.05        # Max 5% of portfolio per position
RISK_PER_TRADE_PCT = 0.005     # Risk 0.5% of portfolio per trade (ATR-sized)
STOP_LOSS_PCT = 4.0            # Default 4% stop loss
TAKE_PROFIT_PCT = 5.0          # Default 5% take profit
MAX_DAILY_LOSS_PCT = 0.03      # Halt new entries if down 3% on the day
MIN_BUYING_POWER = 500.0       # Minimum buying power to enter new positions
DEAD_DAY_ALERT_CYCLES = 4      # Consecutive zero-candidate cycles before dead-day Telegram alert

# Entry time window (ET)
ENTRY_START_HOUR = 10          # No entries before 10:00 AM (avoid open noise)
ENTRY_END_HOUR = 15            # No entries at or after 3:00 PM

# VIX regime thresholds
RSI_BUY_MAX = 72               # Max RSI for BUY entry (unified across all gate layers)
RSI_BUY_MAX_HIGH_VOL = 65      # Tighter gate in high-volatility regimes
VWAP_SLOPE_MIN = -0.3          # % VWAP drift over 10 bars; below this = downtrend, skip BUY
HIGH_VOL_POSITION_MULT = 0.5   # Halve position size in HIGH_VOL / CRISIS regimes

# Paths
TRADING_PARAMS_FILE = STATE_DIR / "trading_params.json"

# Indicator engine
BAR_TIMEFRAME_MINUTES = 5
BARS_LOOKBACK = 100
DAILY_BARS_LOOKBACK = 30

# Email daily report (optional — leave blank to disable)
EMAIL_SENDER       = os.getenv("EMAIL_SENDER", "")
EMAIL_APP_PASSWORD = os.getenv("EMAIL_APP_PASSWORD", "")
EMAIL_RECIPIENT    = os.getenv("EMAIL_RECIPIENT", "")
DAILY_REPORT_SENT_FILE    = STATE_DIR / "daily_report_sent.json"
WATCH_LIST_NOTIF_FILE     = STATE_DIR / "watch_list_notif_sent.json"

# Telegram notifications (optional — leave blank to disable)
# Setup: message @BotFather → /newbot → get token.
# Then message your bot once and GET api.telegram.org/bot<TOKEN>/getUpdates for chat_id.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Plan 1: Regime-Adaptive Foundation ────────────────────────────────────────

# State file paths
MACRO_REGIME_FILE      = STATE_DIR / "macro_regime.json"
FRED_CACHE_FILE        = STATE_DIR / "fred_cache.json"
ECONOMIC_CALENDAR_FILE = STATE_DIR / "economic_calendar.json"
API_BUDGET_FILE        = STATE_DIR / "api_budget.json"
SWING_POSITIONS_FILE   = STATE_DIR / "swing_positions.json"

# FRED API (free, optional — bot defaults to modifier=0 if missing)
FRED_API_KEY = os.getenv("FRED_API_KEY", "")

# Market data provider: yahoo | alpaca | hybrid
MARKET_DATA_PROVIDER = os.getenv("MARKET_DATA_PROVIDER", "yahoo")

# Polygon (optional — used only by hybrid mode for SPY flash-crash check)
POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")

# Macro regime thresholds
MACRO_HEALTH_BULL_THRESHOLD  =  20   # score above this → bullish regimes eligible
MACRO_HEALTH_BEAR_THRESHOLD  = -20   # score below this → defensive regimes eligible
CONSECUTIVE_BULL_DAYS        =   3   # consecutive green closes to qualify BULL_TREND
CONSECUTIVE_BEAR_DAYS        =   3   # consecutive red closes to qualify DEFENSIVE
CHOPPY_MIN_SIGN_ALTS         =   3   # min sign alternations in last 5 days for CHOPPY
CHOPPY_MAX_NET_MOVE_PCT      = 0.5   # max |net 5-day SPY move| % to confirm CHOPPY

# API budget (daily spend limits, USD)
API_BUDGET_NORMAL_CEILING   = 1.00   # normal daily Claude spend cap
API_BUDGET_ELEVATED_CEILING = 1.50   # elevated cap (CRISIS/CAPITULATION or high-gain day)
API_BUDGET_WARNING_PCT      = 0.80   # warn at 80% of ceiling

# Entry gate composite-score thresholds (used by Plan 3 pre-filter)
CHOPPY_COMPOSITE_GATE  = 30   # higher bar to enter in CHOPPY regime
HERD_ENTRY_GATE        = 25   # raise gate when bot-herding detected

# Counter-bot signal thresholds (used by Plans 2 + 3)
BOT_HERD_ALERT_SCORE     = 60   # herding score that triggers position-size reduction
BOT_HERD_HIGH_SCORE      = 80   # herding score that blocks new entries
SQUEEZE_CONFIDENCE_GATE  = 70   # min confidence to act on a squeeze setup

TRAILING_STOP_ATR_MULT   = 1.5  # trailing stop = ATR × this; 3% fallback if ATR unavailable

# Swing / short-term position limits (enforced by Plan 2 swing_manager)
SWING_MAX_POSITIONS      = 2    # max concurrent swing positions
SWING_MAX_DAYS           = 5    # max trading days to hold a swing position
SHORT_TERM_MAX_POSITIONS = 2    # max concurrent short-term positions
SHORT_TERM_MAX_DAYS      = 21   # max calendar days to hold a short-term position

# ── Plan 2: Swing & Signals ────────────────────────────────────────────────────

# Earnings calendar
EARNINGS_CALENDAR_FILE = STATE_DIR / "earnings_calendar.json"

# Counter-bot / parabolic signal thresholds
PARABOLIC_5DAY_RETURN_PCT     = 15.0   # 5-day return % to flag as parabolic
VOLUME_EXHAUSTION_RATIO       = 0.8    # vol < this × 20d avg at 5d high → exhaustion
STOP_HUNT_ROUND_PROXIMITY_PCT = 0.4    # within 0.4% of round number ($X.00 / $X.50)
STOP_HUNT_HILOW_PROXIMITY_PCT = 0.5    # within 0.5% of 5-day high or low

# PEAD (post-earnings announcement drift) detection thresholds
PEAD_EPS_SURPRISE_MIN   = 5.0    # ≥ 5% EPS beat required
PEAD_DAY_RETURN_MIN     = 3.0    # ≥ 3% stock gain on earnings day
PEAD_CLOSE_POSITION_MIN = 0.7    # closed in upper 30% of day's range
PEAD_VOLUME_RATIO_MIN   = 2.0    # 2× average volume on earnings day
PEAD_SCORE_THRESHOLD    = 80     # need 4 of 5 conditions (each = 20 pts)

# ── Claude token optimization ─────────────────────────────────────────────────

# Research agent: StockTwits pre-filter (skips Claude when sentiment is unambiguous
# AND there is no stock-specific news that could override the social signal)
STOCKTWITS_MIN_TAGGED      = 15   # min tagged messages to trust bullish_pct
QUICK_SENTIMENT_BULL_PCT   = 70   # bullish_pct ≥ this → rule-based BULLISH, skip Claude
QUICK_SENTIMENT_BEAR_PCT   = 30   # bullish_pct ≤ this → rule-based BEARISH, skip Claude
QUICK_SENTIMENT_SCORE_BULL =  55  # combined_sentiment_score assigned to rule-based BULLISH
QUICK_SENTIMENT_SCORE_BEAR = -55  # combined_sentiment_score assigned to rule-based BEARISH

# ── Crash Recovery ────────────────────────────────────────────────────────
BOT_HEALTH_FILE              = STATE_DIR / "bot_health.json"
CRASH_LOOP_THRESHOLD         = 3      # restarts within window before backoff triggers
CRASH_LOOP_WINDOW_MINUTES    = 10     # rolling window for crash counting
CRASH_BACKOFF_MINUTES        = 30     # pause before resuming after crash loop
FLASH_CRASH_SPY_DROP_PCT     = -2.5   # replaces hardcoded -2.5 in check_flash_crash()
FLASH_CRASH_VIX_RECOVERY     = 35     # auto-clear lockout if VIX drops below this
FLASH_CRASH_SPY_RECOVERY_PCT = -5.0   # auto-clear lockout if SPY 5d return above this

# ── State file hygiene ────────────────────────────────────────────────────────
TRADE_JOURNAL_MAX_DAYS  = 30   # faster journal reads; archive keeps full history
CORRUPT_BACKUP_MAX_DAYS = 3    # .corrupt.json backups useless after 3 days

# ── Plan 4 ────────────────────────────────────────────────────────────────────

ENTRY_GATE_OPEN_MINUTES = 30      # block entries for first N minutes after open
ENTRY_GATE_CLOSE_MINUTES = 35     # block entries for last N minutes before close

RS_MIN_RATIO = 0.0                # symbol must not underperform SPY by more than this

MAX_SAME_SECTOR_POSITIONS = 2     # block entry if >= this many open positions share sector

SENTIMENT_SIZE_TIERS: list[tuple[int, float]] = [
    (20, 1.25),
    (10, 1.10),
    (-10, 1.00),
    (-20, 0.75),
]
SENTIMENT_SIZE_FLOOR = 0.50

MTF_BARS_LOOKBACK = 40            # 15-min bars to fetch for multi-timeframe filter
MTF_TIMEFRAME_MINUTES = 15

PROFIT_LOCK_TRIGGER_PCT = 2.0     # move stop to breakeven when unrealised PnL >= this %

# ── Risk Manager ──────────────────────────────────────────────────────────────
ENTRY_BUDGET_RATIO          = 0.70   # fraction of daily ceiling for new entry calls
EXIT_BUDGET_RATIO           = 0.30   # fraction reserved for thesis-break exit calls
EXIT_EVAL_COMPOSITE_DROP    = 10     # composite must drop >= this from entry score to trigger eval
EXIT_EVAL_SENTIMENT_FLOOR   = -30    # sentiment below this triggers exit eval
GAP_SKIP_PCT                = 3.0    # skip entry if price gapped > this % above prev close
FILL_SLIPPAGE_THRESHOLD     = 0.005  # correct bracket if fill deviates > 0.5% from iv price
FILL_POLL_ATTEMPTS          = 5      # max Alpaca polls for fill price after order placement
FILL_POLL_INTERVAL_S        = 0.5    # seconds between fill poll attempts

PROGRESSIVE_LOSS_STEP = 0.20      # reduce size by this fraction per loss
PROGRESSIVE_LOSS_FLOOR = 0.40     # minimum size multiplier

EOD_VERIFY_FILLS = True           # skip journal entry if symbol not in alpaca positions
EOD_PROFITABLE_THRESHOLD = 0.5    # pnl_pct above this → "eod_force_profitable"
EOD_LOSS_THRESHOLD = -0.5         # pnl_pct below this → "eod_force_loss"

# ── Signal Integrity (2026-05-07) ─────────────────────────────────────────────
MAX_ENTRIES_PER_CYCLE       = 1       # one BUY per trading cycle maximum
VWAP_OVEREXTENSION_GATE     = True    # block price > VWAP+2σ
PARABOLIC_ENTRY_BLOCK       = True    # block parabolic=True entries

# ── Entry gate overbought thresholds ──────────────────────────────────────────
STOCH_HARD_VETO_THRESHOLD  = 97    # raised from 93 — avoids catch-22 with high-momentum composites
STOCH_SOFT_FLAG_THRESHOLD  = 85    # soft-flag for Claude review when Stoch is elevated but not at hard veto
DAILY_LOSS_WARN_PCT        = 0.02  # Telegram warning at -2% daily loss (halt is at MAX_DAILY_LOSS_PCT = 3%)
SECTOR_ETF_SOFT_DROP_PCT   = -1.5  # sector ETF intraday drop % threshold for soft-flag entry gate
POST_EVENT_POSITION_MULT    = 0.6     # 60% sizing on post-FOMC/NFP/CPI days
INTRADAY_CONTEXT_TTL_SEC    = 300     # cache SPY intraday context 5 min
RESEARCH_STALE_WARN_MIN     = 120     # warn if research report > 2h old
RESEARCH_STALE_BLOCK_MIN    = 600     # alert and continue (not block) if research > 10h old
FETCH_WORKERS               = 6       # parallel fetch threads in pipeline Phase 1
ECONOMIC_CALENDAR_HOUR      = 8
ECONOMIC_CALENDAR_MINUTE    = 20      # moved from 8:30 → 8:20 to avoid race with startup

# ── Breadth Signals ───────────────────────────────────────────────────────────
MACRO_BREADTH_TICKERS: dict[str, str] = {
    "hyg":  "HYG",
    "iwm":  "IWM",
    "xlf":  "XLF",
    "vix3m": "%5EVIX3M",
}

# ── PEAD Enhancements ─────────────────────────────────────────────────────────
PEAD_EPS_BEAT_STRONG        = 20.0    # blowout beat threshold (30 pts)
PEAD_EPS_BEAT_SOLID         = 10.0    # strong beat threshold (25 pts)
PEAD_FWD_REVISION_MIN       = 3.0     # forward EPS revision % to qualify as 6th condition

# ── Pre-Claude Filter + Token Efficiency ──────────────────────────────────────
COMPOSITE_MIN_NORMAL          = 20    # composite entry minimum — NORMAL regime
COMPOSITE_MIN_HIGH_VOL        = 35    # raised composite minimum — HIGH_VOL / DEFENSIVE regime
MACRO_HEALTH_SKIP_THRESHOLD   = -20   # skip if macro_health_score < this
MACRO_SHOCK_SKIP_THRESHOLD    = 50    # skip if research macro_shock_score > this
SENTIMENT_HARD_BLOCK          = -15   # combined_sentiment_score below this = hard block
SENTIMENT_WEAK_COMPOSITE_MIN  = 30    # if sentiment < 0, require composite >= this
INTRADAY_SENTIMENT_BLOCK      = -10   # intraday sentiment_score below this = skip

# ── Auto-Execute (skip Claude for textbook setups) ────────────────────────────
AUTO_EXECUTE_COMPOSITE_MIN    = 55    # composite must exceed this for auto-execute
AUTO_EXECUTE_SENTIMENT_MIN    = 20    # intraday sentiment_score must exceed this
AUTO_EXECUTE_CONFIDENCE       = 90    # confidence score assigned to auto-execute decisions (legacy fallback)
AUTO_EXECUTE_CONFIDENCE_BASE  = 75    # base confidence for dynamic auto-execute derivation
# Flags that indicate data-fetch failures (not bearish signals) — safe to ignore in auto-execute
AUTO_EXECUTE_DATA_ONLY_FLAGS: frozenset = frozenset({"mtf_unavailable"})

# ── Budget Rollover ───────────────────────────────────────────────────────────
BUDGET_ROLLOVER_FRACTION      = 0.5   # fraction of unused exit budget to roll into entry
EXIT_ROLLOVER_MAX_USD         = 0.15  # cap on a single rollover
BUDGET_ROLLOVER_HOUR          = 12    # ET hour to evaluate rollover
BUDGET_ROLLOVER_MINUTE        = 30    # ET minute to evaluate rollover

# ── Intraday News ─────────────────────────────────────────────────────────────
INTRADAY_NEWS_SHOCK_BLOCK     = 60    # shock_score >= this → hard-block entry
INTRADAY_NEWS_STALE_MIN       = 45    # cache older than this → treat as missing

# ── Watch List Technical Pre-Filter ──────────────────────────────────────────
WATCH_LIST_RSI_FILTER         = 70    # exclude if RSI > this at 9 AM snapshot
WATCH_LIST_STOCH_FILTER       = 80    # exclude if Stochastic %D > this at 9 AM snapshot
WATCH_LIST_MIN_AFTER_FILTER   = 6     # relax filter thresholds if fewer symbols remain

INTRADAY_NEWS_CACHE_FILE      = STATE_DIR / "intraday_news_cache.json"
CYCLE_TELEMETRY_FILE          = STATE_DIR / "cycle_telemetry.jsonl"

# ── Plan 3: Notification Completeness & Throttling ────────────────────────────
DRAWDOWN_WARN_PCT = 2.0  # alert when position unrealised loss >= this % (but above stop)

# ── Learning Agent ─────────────────────────────────────────────────────────────
LEARNING_MAX_PARAMS_PER_DAY        = 1   # max param changes on zero-trade days
LEARNING_ZERO_TRADE_RSI_STEP       = 2   # relax RSI_BUY_MAX by this on zero-trade days
LEARNING_ZERO_TRADE_COMPOSITE_STEP = 2   # relax COMPOSITE_MIN_NORMAL by this
