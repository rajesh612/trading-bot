import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent

# Data files
TICKERS_FILE = BASE_DIR / "tickers.txt"
DAILY_TRADEABLE_FILE = BASE_DIR / "daily_tradeable.txt"
WATCH_LIST_FILE = BASE_DIR / "watch_list.txt"
TRADE_JOURNAL_FILE = BASE_DIR / "trade_journal.json"
INDICATOR_WEIGHTS_FILE = BASE_DIR / "indicator_weights.json"
STRATEGY_MEMORY_FILE = BASE_DIR / "strategy_memory.md"
INDICATOR_PERFORMANCE_LOG = BASE_DIR / "indicator_performance_log.md"
RESEARCH_REPORT_FILE = BASE_DIR / "research_report.json"
OPEN_POSITIONS_FILE = BASE_DIR / "open_positions.json"
DAILY_CLOSED_FILE   = BASE_DIR / "daily_closed.json"

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
CLAUDE_BATCH_SIZE = 5            # Stocks per batched Claude call in trading cycle
COMPOSITE_SCORE_THRESHOLD = 15   # Skip Claude if no open position and score <= this
STRATEGY_MEMORY_MAX_CHARS = 4000 # Truncate strategy_memory before sending to Claude

# Risk parameters
MAX_POSITION_PCT = 0.05        # Max 5% of portfolio per position
RISK_PER_TRADE_PCT = 0.005     # Risk 0.5% of portfolio per trade (ATR-sized)
STOP_LOSS_PCT = 4.0            # Default 4% stop loss
TAKE_PROFIT_PCT = 5.0          # Default 5% take profit
MAX_DAILY_LOSS_PCT = 0.03      # Halt new entries if down 3% on the day
MIN_BUYING_POWER = 500.0       # Minimum buying power to enter new positions

# Entry time window (ET)
ENTRY_START_HOUR = 10          # No entries before 10:00 AM (avoid open noise)
ENTRY_END_HOUR = 15            # No entries at or after 3:00 PM

# VIX regime thresholds
RSI_BUY_MAX = 75               # Block extreme overbought only; momentum stocks live at RSI 50-75
RSI_BUY_MAX_HIGH_VOL = 65      # Tighter gate in high-volatility regimes
VWAP_SLOPE_MIN = -0.3          # % VWAP drift over 10 bars; below this = downtrend, skip BUY
HIGH_VOL_POSITION_MULT = 0.5   # Halve position size in HIGH_VOL / CRISIS regimes

# Paths
VIX_REGIME_FILE    = BASE_DIR / "vix_regime.json"
TRADING_PARAMS_FILE = BASE_DIR / "trading_params.json"

# Indicator engine
BAR_TIMEFRAME_MINUTES = 5
BARS_LOOKBACK = 100
DAILY_BARS_LOOKBACK = 30

# Email daily report (optional — leave blank to disable)
EMAIL_SENDER       = os.getenv("EMAIL_SENDER", "")
EMAIL_APP_PASSWORD = os.getenv("EMAIL_APP_PASSWORD", "")
EMAIL_RECIPIENT    = os.getenv("EMAIL_RECIPIENT", "")
DAILY_REPORT_SENT_FILE = BASE_DIR / "daily_report_sent.json"

# ── Plan 1: Regime-Adaptive Foundation ────────────────────────────────────────

# State file paths
MACRO_REGIME_FILE      = BASE_DIR / "macro_regime.json"
FRED_CACHE_FILE        = BASE_DIR / "fred_cache.json"
ECONOMIC_CALENDAR_FILE = BASE_DIR / "economic_calendar.json"
API_BUDGET_FILE        = BASE_DIR / "api_budget.json"
SWING_POSITIONS_FILE   = BASE_DIR / "swing_positions.json"

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

# Swing / short-term position limits (enforced by Plan 2 swing_manager)
SWING_MAX_POSITIONS      = 2    # max concurrent swing positions
SWING_MAX_DAYS           = 5    # max trading days to hold a swing position
SHORT_TERM_MAX_POSITIONS = 2    # max concurrent short-term positions
SHORT_TERM_MAX_DAYS      = 21   # max calendar days to hold a short-term position

# ── Plan 2: Swing & Signals ────────────────────────────────────────────────────

# Earnings calendar
EARNINGS_CALENDAR_FILE = BASE_DIR / "earnings_calendar.json"

# Counter-bot / parabolic signal thresholds
PARABOLIC_5DAY_RETURN_PCT     = 15.0   # 5-day return % to flag as parabolic
VOLUME_EXHAUSTION_RATIO       = 0.8    # vol < this × 20d avg at 5d high → exhaustion
STOP_HUNT_ROUND_PROXIMITY_PCT = 0.4    # within 0.4% of round number ($X.00 / $X.50)
STOP_HUNT_HILOW_PROXIMITY_PCT = 0.3    # within 0.3% of 5-day high or low

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

# Trading agent: auto-approve pre-filter
# composite > this threshold + VWAP↑ + RSI 50–72 + MACD positive → auto-BUY, skip Claude
AUTO_BUY_COMPOSITE_THRESHOLD = 35
