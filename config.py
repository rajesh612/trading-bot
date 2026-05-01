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
