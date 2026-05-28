import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── PATHS ─────────────────────────────────────────────────────────────────────

BASE_DIR  = Path(__file__).parent
STATE_DIR = BASE_DIR / "data" / "state"

TICKERS_FILE              = BASE_DIR / "tickers.json"
DAILY_TRADEABLE_FILE      = BASE_DIR / "daily_tradeable.txt"
WATCH_LIST_FILE           = BASE_DIR / "watch_list.txt"
STRATEGY_MEMORY_FILE      = BASE_DIR / "strategy_memory.md"

TRADE_JOURNAL_FILE         = STATE_DIR / "trade_journal.json"
INDICATOR_WEIGHTS_FILE     = STATE_DIR / "indicator_weights.json"
INDICATOR_PERFORMANCE_LOG  = STATE_DIR / "indicator_performance_log.md"
RESEARCH_REPORT_FILE       = STATE_DIR / "research_report.json"
OPEN_POSITIONS_FILE        = STATE_DIR / "open_positions.json"
DAILY_CLOSED_FILE          = STATE_DIR / "daily_closed.json"
TRADING_PARAMS_FILE        = STATE_DIR / "trading_params.json"
EARNINGS_CALENDAR_FILE     = STATE_DIR / "earnings_calendar.json"
MACRO_REGIME_FILE          = STATE_DIR / "macro_regime.json"
FRED_CACHE_FILE            = STATE_DIR / "fred_cache.json"
ECONOMIC_CALENDAR_FILE     = STATE_DIR / "economic_calendar.json"
API_BUDGET_FILE            = STATE_DIR / "api_budget.json"
SWING_POSITIONS_FILE       = STATE_DIR / "swing_positions.json"
DAILY_REPORT_SENT_FILE     = STATE_DIR / "daily_report_sent.json"
WATCH_LIST_NOTIF_FILE      = STATE_DIR / "watch_list_notif_sent.json"
BOT_HEALTH_FILE            = STATE_DIR / "bot_health.json"
INTRADAY_NEWS_CACHE_FILE   = STATE_DIR / "intraday_news_cache.json"
CYCLE_TELEMETRY_FILE       = STATE_DIR / "cycle_telemetry.jsonl"
METRICS_FILE               = STATE_DIR / "metrics.jsonl"
SL_BLACKLIST_FILE          = STATE_DIR / "sl_blacklist.json"
SL_BLACKLIST_DAYS          = 1   # days to block re-entry after SL hit

# ── API KEYS & MODELS ─────────────────────────────────────────────────────────

# Alpaca — PAPER TRADING ONLY
ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_PAPER_URL  = "https://paper-api.alpaca.markets"

# Anthropic
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL      = "claude-sonnet-4-6"

# Social media (optional — bot degrades gracefully if not set)
# Reddit uses the public JSON API (no auth required)
X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN", "")   # requires X Basic API ($100/mo)

# FRED API (free, optional — bot defaults to modifier=0 if missing)
FRED_API_KEY = os.getenv("FRED_API_KEY", "")

# Polygon (optional — used only by hybrid mode for SPY flash-crash check)
POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")

# Market data provider: yahoo | alpaca | hybrid
MARKET_DATA_PROVIDER = os.getenv("MARKET_DATA_PROVIDER", "yahoo")

# ── RISK MANAGEMENT ───────────────────────────────────────────────────────────

MAX_POSITION_PCT   = 0.05        # Max 5% of portfolio per position
RISK_PER_TRADE_PCT = 0.005       # Risk 0.5% of portfolio per trade (ATR-sized)
STOP_LOSS_PCT      = 3.0         # Lowered from 4.0 — was 2× realized TP avg (~1.95%), creating inverted R:R
TAKE_PROFIT_PCT    = 5.0         # Default 5% take profit
MAX_DAILY_LOSS_PCT = 0.03        # Halt new entries if down 3% on the day
DAILY_LOSS_WARN_PCT = 0.02       # Telegram warning at -2% daily loss (halt is at MAX_DAILY_LOSS_PCT = 3%)
MIN_BUYING_POWER   = 500.0       # Minimum buying power to enter new positions

PROGRESSIVE_LOSS_STEP  = 0.20   # reduce size by this fraction per loss
PROGRESSIVE_LOSS_FLOOR = 0.40   # minimum size multiplier

GAP_SKIP_PCT             = 3.0   # skip entry if price gapped > this % above prev close
FILL_SLIPPAGE_THRESHOLD  = 0.005 # correct bracket if fill deviates > 0.5% from iv price
FILL_POLL_ATTEMPTS       = 5     # max Alpaca polls for fill price after order placement
FILL_POLL_INTERVAL_S     = 0.5   # seconds between fill poll attempts
BRACKET_FILL_TOLERANCE_PCT = 0.5 # % tolerance when matching price to SL/TP leg in bracket reconciliation
ATR_FALLBACK_PROXY_PCT   = 0.02  # price × this = fallback ATR when ATR is unavailable (2% of price)

# ── POSITION SIZING ───────────────────────────────────────────────────────────

SENTIMENT_SIZE_TIERS: list[tuple[int, float]] = [
    (20, 1.25),
    (10, 1.10),
    (-10, 1.00),
    (-20, 0.75),
]
SENTIMENT_SIZE_FLOOR     = 0.50
HIGH_VOL_POSITION_MULT   = 0.75  # Raised from 0.5 — capture recovery moves in HIGH_VOL regime

SWING_MAX_POSITIONS        = 6    # max concurrent swing positions
SHORT_TERM_MAX_POSITIONS   = 2    # max concurrent short-term positions
SWING_SL_DEFAULT_PCT       = 7.0  # fallback SL % when position has no recorded stop
SWING_OVERNIGHT_SL_PCT     = 3.0  # tighter SL for overnight gap risk
SWING_SL_PCT_CAPITULATION  = 12.0 # stop-loss % in CAPITULATION regime

# ── ENTRY GATES & TIMING ──────────────────────────────────────────────────────

SL_BLACKLIST_ENABLED   = True   # block SL-hit symbols from re-entry next day

ENTRY_START_HOUR       = 10   # No entries before 10:00 AM (avoid open noise)
ENTRY_END_HOUR         = 15   # No entries at or after 3:00 PM
ENTRY_HARD_CLOSE_HOUR  = 13   # Default regime hard cutoff (NORMAL); per-regime values in EntryParams
ENTRY_GATE_OPEN_MINUTES  = 30 # block entries for first N minutes after open
ENTRY_GATE_CLOSE_MINUTES = 35 # block entries for last N minutes before close
ENTRY_LATE_HOUR          = 14   # 2:00 PM ET — after this, require higher composite for entry
ENTRY_LATE_COMPOSITE_MIN = 55   # composite threshold for entries after ENTRY_LATE_HOUR
MARKET_OPEN_HOUR   = 9    # NYSE market open hour (ET)
MARKET_OPEN_MINUTE = 30   # NYSE market open minute (ET)
TRADING_MINUTES_PER_DAY = 390  # 6.5 hours × 60 — used for session-fraction calculations
# ── Concurrency ──────────────────────────────────────────────────────────────
MAX_ENTRIES_PER_CYCLE_BURST: int = 3   # max new entries within one 10-min cycle
SWING_HEALTH_MIN_PNL_PCT:    float = -2.0  # swing book P&L floor; below → gate fires
REGIME_HEAT_CEILING: dict[str, float] = {
    "BULL_TREND":     0.50,
    "OPPORTUNITY":    0.45,
    "NORMAL":         0.35,
    "CRASH_RECOVERY": 0.25,
    "HIGH_VOL":       0.25,
    "CHOPPY":         0.20,
    "DEFENSIVE":      0.10,
    "CAPITULATION":   0.10,
    "CRISIS":         0.00,
}

RSI_BUY_MAX            = 72   # Max RSI for BUY entry (unified across all gate layers)
RSI_BUY_MAX_HIGH_VOL   = 65   # Tighter gate in high-volatility regimes
CHOPPY_RSI_PULLBACK_MAX = 60  # in CHOPPY, only enter if RSI < this (avoid buying the bounce top)

STOCH_HARD_VETO_THRESHOLD = 97  # raised from 93 — avoids catch-22 with high-momentum composites
STOCH_SOFT_FLAG_THRESHOLD = 85  # soft-flag for Claude review when Stoch is elevated but not at hard veto

SECTOR_ETF_SOFT_DROP_PCT = -1.5  # sector ETF intraday drop % threshold for soft-flag entry gate
BREADTH_WEAK_THRESHOLD   = 0.85  # IWM/SPY breadth ratio below this → soft flag
REL_VOLUME_MIN           = 0.60  # current volume < 60% of 20d avg → soft flag (weak tape)
OPENING_RANGE_MINUTES    = 60    # first N minutes of session form the opening range

# ── INDICATOR & COMPOSITE THRESHOLDS ─────────────────────────────────────────

COMPOSITE_MIN_NORMAL   = 50   # raised from 20 — filters noise entries, improves win rate
COMPOSITE_MIN_HIGH_VOL = 55   # raised composite minimum — HIGH_VOL / DEFENSIVE regime
CHOPPY_COMPOSITE_GATE  = 55   # higher bar to enter in CHOPPY regime
COMPOSITE_MIN_BULL_TREND = 45  # BULL_TREND: slightly relaxed, wider TP justifies it
HERD_ENTRY_GATE        = 25   # raise gate when bot-herding detected

AUTO_EXECUTE_COMPOSITE_MIN   = 70  # used by _compute_auto_execute_confidence for bonus delta
AUTO_EXECUTE_CONFIDENCE      = 90  # confidence score assigned to auto-execute decisions (legacy fallback)
AUTO_EXECUTE_CONFIDENCE_BASE = 75  # base confidence for dynamic auto-execute derivation
SQUEEZE_COMPOSITE_MIN        = 12  # composite threshold when squeeze_setup is True

SQUEEZE_CONFIDENCE_GATE = 70   # min confidence to act on a squeeze setup
SQUEEZE_SL_PCT          = 6.0  # stop-loss % applied when squeeze setup fires
SQUEEZE_TP_PCT          = 5.0  # take-profit % applied when squeeze setup fires

BOT_HERD_ALERT_SCORE = 60  # herding score that triggers position-size reduction
BOT_HERD_HIGH_SCORE  = 80  # herding score that blocks new entries
BOT_HERD_CORR_THRESHOLD = 0.85  # correlation with SPY above this → herding signal

VWAP_SLOPE_MIN       = -0.3   # % VWAP drift over 10 bars; below this = downtrend, skip BUY
VWAP_OVEREXTENSION_GATE = True # block price > VWAP+2σ

PARABOLIC_5DAY_RETURN_PCT = 15.0  # 5-day return % to flag as parabolic
PARABOLIC_ENTRY_BLOCK     = True  # block parabolic=True entries

MAX_ENTRY_PRICE        = 750   # hard block: stocks > $750 excluded (covers CIEN ~$590, META ~$600)
SECTOR_ENTRY_MIN_SCORE = 0     # soft flag threshold: sector NLP sentiment
SWING_NEWS_MIN_SCORE   = 50    # soft flag: news sentiment for swing candidates
MIN_MARKET_CAP_B       = 25    # screener pre-filter: $25B minimum market cap

RS_MIN_RATIO          = 0.0    # symbol must not underperform SPY by more than this
MTF_BARS_LOOKBACK     = 40     # 15-min bars to fetch for multi-timeframe filter
MTF_TIMEFRAME_MINUTES = 15

STOP_HUNT_ROUND_PROXIMITY_PCT = 0.1  # within 0.1% of round number ($X.00)
STOP_HUNT_HILOW_PROXIMITY_PCT = 0.5  # within 0.5% of 5-day high or low
VOLUME_EXHAUSTION_RATIO       = 0.8  # vol < this × 20d avg at 5d high → exhaustion
VOLUME_EXHAUSTION_PRICE_PROXIMITY = 0.995  # within 0.5% of 5-day high counts as "at high"
OBV_PRICE_HIGH_PROXIMITY = 0.999  # OBV bearish divergence: price within 0.1% of intraday high
OBV_PRICE_LOW_PROXIMITY  = 1.001  # OBV bullish divergence: price within 0.1% of intraday low

DEFENSIVE_COMPOSITE_MULT = 1.5  # multiplier applied to COMPOSITE_MIN_HIGH_VOL in DEFENSIVE pre-filter
MAX_SAME_SECTOR_POSITIONS = 2   # block entry if >= this many open positions share sector

# ── REGIME & MACRO ────────────────────────────────────────────────────────────

MACRO_HEALTH_BULL_THRESHOLD = 20   # score above this → bullish regimes eligible
MACRO_HEALTH_BEAR_THRESHOLD = -20  # score below this → defensive regimes eligible
CONSECUTIVE_BULL_DAYS       = 2    # consecutive green closes to qualify BULL_TREND
CONSECUTIVE_BEAR_DAYS       = 3    # consecutive red closes to qualify DEFENSIVE
CRASH_RECOVERY_MIN_GREEN_DAYS = 2  # min consecutive green closes to exit CRASH_RECOVERY
CHOPPY_MIN_SIGN_ALTS        = 3    # min sign alternations in last 5 days for CHOPPY
CHOPPY_MAX_NET_MOVE_PCT     = 0.5  # max |net 5-day SPY move| % to confirm CHOPPY
REGIME_ADAPTIVE_LOGIC       = True # enable directional strategy layer (beyond threshold adjustment)

FLASH_CRASH_SPY_DROP_PCT     = -2.5  # replaces hardcoded -2.5 in check_flash_crash()
FLASH_CRASH_VIX_RECOVERY     = 35    # auto-clear lockout if VIX drops below this
FLASH_CRASH_SPY_RECOVERY_PCT = -5.0  # auto-clear lockout if SPY 5d return above this

MACRO_BREADTH_TICKERS: dict[str, str] = {
    "hyg":  "HYG",
    "iwm":  "IWM",
    "xlf":  "XLF",
    "vix3m": "%5EVIX3M",
}

MACRO_SHOCK_SKIP_THRESHOLD  = 50   # skip if research macro_shock_score > this
MACRO_HEALTH_SKIP_THRESHOLD = -20  # skip if macro_health_score < this

# ── SWING & EOD ───────────────────────────────────────────────────────────────

SWING_MAX_DAYS              = 5    # max trading days to hold a swing position
TRAILING_STOP_ATR_MULT      = 1.5  # trailing stop = ATR × this; 3% fallback if ATR unavailable
TRAILING_STOP_PCT_FALLBACK  = 0.03 # 3% trailing stop when ATR unavailable
PROFIT_LOCK_TRIGGER_PCT     = 2.0  # move stop to breakeven when unrealised PnL >= this %

SWING_GAP_TIGHTEN_THRESHOLD = -2.0 # gap% below this (but above -SL) → tighten swing stop
SWING_GAP_TP1_THRESHOLD     = 3.0  # gap% at or above this → take first swing TP tranche

EOD_VERIFY_FILLS        = True  # skip journal entry if symbol not in alpaca positions
EOD_PROFITABLE_THRESHOLD = 0.5  # pnl_pct above this → "eod_force_profitable"
EOD_LOSS_THRESHOLD       = -0.5 # pnl_pct below this → "eod_force_loss"
EOD_FORCE_CLOSE_HOUR   = 15  # hour at which force_close_all() runs (ET)
EOD_FORCE_CLOSE_MINUTE = 55  # minute at which force_close_all() runs (ET)

# HIGH_VOL intraday early exit — proactively close stale losing day trades before EOD force-close.
# Avoids holding broken setups to 3:55 PM in volatile regimes where recovery is less likely.
HIGH_VOL_EARLY_EXIT_HOUR: int = 14        # 2 PM ET: window opens
HIGH_VOL_EARLY_EXIT_MIN_HOLD_H: float = 2.0   # position must be held ≥ 2h to qualify
HIGH_VOL_EARLY_EXIT_MIN_LOSS_PCT: float = 0.5  # must be down ≥ 0.5% (avoids noise exits)

EOD_HOLDS_EVAL_HOUR    = 15  # hour at which evaluate_eod_holds() runs (ET)
EOD_HOLDS_EVAL_MINUTE  = 50  # minute at which evaluate_eod_holds() runs (ET)
SWING_GAP_TIGHTEN_SL_PCT = 2.0  # tighten swing stop to this % when gap down is small
PROFIT_LOCK_SWING_PCT    = 2.5       # default/documentation anchor for profit-lock bypass threshold
SWING_PROMOTE_MIN_PNL_PCT = 0.5     # default/documentation anchor for standard promotion min P&L
SWING_PROFIT_LOCK_MIN_COMPOSITE  = 62    # minimum composite even for PROFIT-LOCK promoted swings

# Overnight swing SL floors per (regime, tier) — wider for LOW-quality (higher overnight variance),
# tighter for CONVICTION (high-confidence entries tolerate less adverse drift before exiting).
# Replaces the previous per-regime dict + CONVICTION_TIGHT subtraction.
SWING_SL_FLOOR_PARAMS: dict[tuple[str, str], float] = {
    # NORMAL: MEDIUM raised 3.5→4.5 — 90d backtest showed all 13 worst SWING_SL hits
    # were MEDIUM at exactly -3.55% (floor + slippage), not genuine reversals.
    # LOW raised above MEDIUM to preserve tier ordering (lower quality = wider overnight stop).
    ("NORMAL",         "LOW"):        5.0,
    ("NORMAL",         "MEDIUM"):     4.5,
    ("NORMAL",         "HIGH"):       3.0,
    ("NORMAL",         "CONVICTION"): 2.5,
    # BULL_TREND: MEDIUM raised 3.5→4.5 (trend regime needs room for intraday retracements)
    ("BULL_TREND",     "LOW"):        5.0,
    ("BULL_TREND",     "MEDIUM"):     4.5,
    ("BULL_TREND",     "HIGH"):       3.0,
    ("BULL_TREND",     "CONVICTION"): 2.5,
    # OPPORTUNITY: MEDIUM raised 3.5→4.5 (same reasoning as NORMAL)
    ("OPPORTUNITY",    "LOW"):        5.0,
    ("OPPORTUNITY",    "MEDIUM"):     4.5,
    ("OPPORTUNITY",    "HIGH"):       3.0,
    ("OPPORTUNITY",    "CONVICTION"): 2.5,
    # CRASH_RECOVERY: wider across all tiers (post-crash variance is elevated)
    ("CRASH_RECOVERY", "LOW"):        4.5,
    ("CRASH_RECOVERY", "MEDIUM"):     4.0,
    ("CRASH_RECOVERY", "HIGH"):       3.5,
    ("CRASH_RECOVERY", "CONVICTION"): 3.0,
    # DEFENSIVE: tighter — defensive names are lower-beta
    ("DEFENSIVE",      "LOW"):        3.5,
    ("DEFENSIVE",      "MEDIUM"):     3.0,
    ("DEFENSIVE",      "HIGH"):       2.5,
    ("DEFENSIVE",      "CONVICTION"): 2.0,
    # CHOPPY: only HIGH/CONVICTION swing-eligible
    ("CHOPPY",         "HIGH"):       3.5,
    ("CHOPPY",         "CONVICTION"): 3.0,
    # HIGH_VOL: widest — elevated overnight variance in volatile regimes
    ("HIGH_VOL",       "MEDIUM"):     4.5,
    ("HIGH_VOL",       "HIGH"):       4.0,
    ("HIGH_VOL",       "CONVICTION"): 3.5,
    # CAPITULATION: extreme volatility needs most room
    ("CAPITULATION",   "HIGH"):       5.0,
    ("CAPITULATION",   "CONVICTION"): 4.0,
}

# Swing hold parameters per (regime, tier): (max_days, profit_lock_pct, min_pnl_pct)
# max_days raised +1 for NORMAL/BULL_TREND (capture more of trending moves).
# profit_lock lowered so more positions stay as swings when profitable.
# min_pnl lowered so more positions qualify for EOD promotion.
SWING_HOLD_PARAMS: dict[tuple[str, str], tuple[int, float, float]] = {
    # max_days raised for MEDIUM to let winners run longer.
    # profit_lock_pct lowered so more positions pass the profit-lock bypass (1.5% instead of 2.0%).
    # min_pnl_pct raised for NORMAL MEDIUM (0.8→1.2) to avoid overnight risk on marginal positions.
    ("NORMAL",         "LOW"):        (5,  3.0, 1.0),
    ("NORMAL",         "MEDIUM"):     (7,  1.5, 1.2),
    ("NORMAL",         "HIGH"):       (7,  1.2, 0.2),
    ("NORMAL",         "CONVICTION"): (8,  1.0, 0.1),
    ("BULL_TREND",     "LOW"):        (6,  2.5, 0.5),
    ("BULL_TREND",     "MEDIUM"):     (8,  1.2, 0.5),
    ("BULL_TREND",     "HIGH"):       (9,  0.8, 0.1),
    ("BULL_TREND",     "CONVICTION"): (12, 0.5, 0.1),
    ("OPPORTUNITY",    "LOW"):        (5,  3.0, 0.7),
    ("OPPORTUNITY",    "MEDIUM"):     (7,  1.5, 0.6),
    ("OPPORTUNITY",    "HIGH"):       (8,  1.2, 0.2),
    ("OPPORTUNITY",    "CONVICTION"): (9,  1.0, 0.1),
    ("CRASH_RECOVERY", "LOW"):        (3,  3.5, 1.0),
    ("CRASH_RECOVERY", "MEDIUM"):     (4,  2.5, 0.7),
    ("CRASH_RECOVERY", "HIGH"):       (5,  2.0, 0.3),
    ("CRASH_RECOVERY", "CONVICTION"): (6,  1.5, 0.2),
    ("DEFENSIVE",      "LOW"):        (2,  3.5, 1.0),
    ("DEFENSIVE",      "MEDIUM"):     (3,  3.0, 0.7),
    ("DEFENSIVE",      "HIGH"):       (4,  2.5, 0.3),
    ("DEFENSIVE",      "CONVICTION"): (5,  2.0, 0.2),
    ("CHOPPY",         "HIGH"):       (3,  2.0, 0.7),
    ("CHOPPY",         "CONVICTION"): (4,  1.0, 0.3),
    ("HIGH_VOL",       "MEDIUM"):     (2,  3.0, 0.7),
    ("HIGH_VOL",       "HIGH"):       (3,  2.5, 0.3),
    ("HIGH_VOL",       "CONVICTION"): (4,  2.0, 0.2),
    ("CAPITULATION",   "HIGH"):       (5,  3.0, -5.0),
    ("CAPITULATION",   "CONVICTION"): (7,  2.0, -5.0),
}
SWING_SENTIMENT_MIN_NORMAL  = -20    # symbol sentiment floor for NORMAL swing promotion
SWING_SENTIMENT_MIN_BULL    = -30    # wider floor in BULL_TREND (trend offsets weak sentiment)
SWING_SENTIMENT_MIN_OPP     = -10    # tighter floor in OPPORTUNITY (thesis depends on sentiment)
SWING_SENTIMENT_MIN_CRASH   = 0      # neutral+ required in CRASH_RECOVERY
SWING_SENTIMENT_MIN_CHOPPY  = 10     # positive required in CHOPPY (sentiment must confirm)
SWING_SECTOR_FLOOR_NORMAL   = -1.0   # sector ETF must not be down more than 1% for NORMAL swing
SWING_SECTOR_FLOOR_BULL     = -2.0   # wider tolerance in BULL_TREND
SWING_SECTOR_FLOOR_CHOPPY   = 0.5    # sector must be up in CHOPPY (confirming upward bias)

# ── BUDGET & API COST ─────────────────────────────────────────────────────────

API_BUDGET_NORMAL_CEILING   = 1.00  # normal daily Claude spend cap
API_BUDGET_ELEVATED_CEILING = 1.50  # elevated cap (CRISIS/CAPITULATION or high-gain day)
API_BUDGET_WARNING_PCT      = 0.80  # warn at 80% of ceiling

ENTRY_BUDGET_RATIO       = 0.70  # fraction of daily ceiling for new entry calls
EXIT_BUDGET_RATIO        = 0.30  # fraction reserved for thesis-break exit calls
EXIT_EVAL_COMPOSITE_DROP = 10    # composite must drop >= this from entry score to trigger eval
EXIT_EVAL_SENTIMENT_FLOOR = -30  # sentiment below this triggers exit eval

BUDGET_ROLLOVER_FRACTION = 0.5   # fraction of unused exit budget to roll into entry
EXIT_ROLLOVER_MAX_USD    = 0.15  # cap on a single rollover
BUDGET_ROLLOVER_HOUR     = 12    # ET hour to evaluate rollover
BUDGET_ROLLOVER_MINUTE   = 30    # ET minute to evaluate rollover
ENTRY_BUDGET_WARNING_PCT = 0.75  # alert when entry budget spend reaches this fraction of ceiling

CLAUDE_BATCH_SIZE  = 10          # Stocks per batched Claude call — matches typical watch-list size
CLAUDE_CB_402_TTL  = 1800.0      # circuit breaker cooldown (s) on 402 Payment Required
CLAUDE_CB_429_TTL  = 300.0       # circuit breaker cooldown (s) on 429 Rate Limited

# ── LEARNING & MEMORY ─────────────────────────────────────────────────────────

LEARNING_ZERO_TRADE_RSI_STEP = 2   # relax RSI_BUY_MAX by this on zero-trade days
LEARNING_ROLLING_DAYS        = 5   # days of trade history to analyze (prevents single-day overfit)
INDICATOR_WEIGHT_MAX_MULT    = 2.0 # max multiplier vs. default weight (prevents one indicator dominating)
INDICATOR_WEIGHT_MIN_MULT    = 0.3 # min multiplier vs. default weight
STRATEGY_MEMORY_BACKUP_MAX   = 7   # keep this many daily .bak files before pruning
STRATEGY_MEMORY_MAX_CHARS    = 3500 # tighter — still above 1024-token cache minimum
STRATEGY_MEMORY_MAX_AGE_DAYS = 7   # reset strategy_memory if unchanged for > N days
TRADE_JOURNAL_MAX_DAYS       = 30  # faster journal reads; archive keeps full history
CORRUPT_BACKUP_MAX_DAYS      = 3   # .corrupt.json backups useless after 3 days

ATR_TP_MULT_DEFAULT = 2.5  # TP = ATR% × this multiplier; raised from 2.0 (avg win was ~1.7% vs 4% SL)
ATR_TP_MULT_MIN     = 1.0  # learning agent lower bound
ATR_TP_MULT_MAX     = 4.0  # learning agent upper bound

METRICS_KEEP_LINES = 2000  # max lines kept in metrics.jsonl (≈ 55 trading days at ~36 cycles/day)

# ── NOTIFICATIONS, OPS & FEATURE FLAGS ───────────────────────────────────────

# Email daily report (optional — leave blank to disable)
EMAIL_SENDER       = os.getenv("EMAIL_SENDER", "")
EMAIL_APP_PASSWORD = os.getenv("EMAIL_APP_PASSWORD", "")
EMAIL_RECIPIENT    = os.getenv("EMAIL_RECIPIENT", "")

# Telegram notifications (optional — leave blank to disable)
# Setup: message @BotFather → /newbot → get token.
# Then message your bot once and GET api.telegram.org/bot<TOKEN>/getUpdates for chat_id.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

DEAD_DAY_ALERT_CYCLES          = 4        # Consecutive zero-candidate cycles before dead-day Telegram alert
WATCHDOG_HEARTBEAT_TIMEOUT_S   = 35 * 60  # scheduler frozen alert if no heartbeat for this many seconds
TELEMETRY_KEEP_LINES           = 500      # max lines kept in cycle_telemetry.jsonl before trimming

CRASH_LOOP_THRESHOLD      = 3   # restarts within window before backoff triggers
CRASH_LOOP_WINDOW_MINUTES = 10  # rolling window for crash counting
CRASH_BACKOFF_MINUTES     = 30  # pause before resuming after crash loop

MAX_TICKERS_DIRECT = 50
WATCH_LIST_MIN     = 10
WATCH_LIST_MAX     = 15

DRAWDOWN_WARN_PCT          = 2.0 # alert when position unrealised loss >= this % (but above stop)
DRAWDOWN_ALERT_COOLDOWN_MIN = 30 # minimum minutes between drawdown Telegram alerts for the same position

INTRADAY_CONTEXT_TTL_SEC  = 300  # cache SPY intraday context 5 min
RESEARCH_STALE_WARN_MIN   = 120  # warn if research report > 2h old
RESEARCH_STALE_BLOCK_MIN  = 600  # alert and continue (not block) if research > 10h old
FETCH_WORKERS             = 6    # parallel fetch threads in pipeline Phase 1

ECONOMIC_CALENDAR_HOUR   = 8
ECONOMIC_CALENDAR_MINUTE = 20    # moved from 8:30 → 8:20 to avoid race with startup

INTRADAY_NEWS_SHOCK_BLOCK = 60   # shock_score >= this → hard-block entry

WATCH_LIST_RSI_FILTER       = 70 # exclude if RSI > this at 9 AM snapshot
WATCH_LIST_STOCH_FILTER     = 80 # exclude if Stochastic %D > this at 9 AM snapshot
WATCH_LIST_MIN_AFTER_FILTER = 6  # relax filter thresholds if fewer symbols remain

SENTIMENT_HARD_BLOCK         = -15 # combined_sentiment_score below this = hard block
SENTIMENT_WEAK_COMPOSITE_MIN = 30  # if sentiment < 0, require composite >= this
INTRADAY_SENTIMENT_BLOCK     = -10 # intraday sentiment_score below this = skip

# Research agent: StockTwits pre-filter (skips Claude when sentiment is unambiguous
# AND there is no stock-specific news that could override the social signal)
STOCKTWITS_MIN_TAGGED      = 15   # min tagged messages to trust bullish_pct
QUICK_SENTIMENT_BULL_PCT   = 70   # bullish_pct ≥ this → rule-based BULLISH, skip Claude
QUICK_SENTIMENT_BEAR_PCT   = 30   # bullish_pct ≤ this → rule-based BEARISH, skip Claude
QUICK_SENTIMENT_SCORE_BULL =  55  # combined_sentiment_score assigned to rule-based BULLISH
QUICK_SENTIMENT_SCORE_BEAR = -55  # combined_sentiment_score assigned to rule-based BEARISH

BAR_TIMEFRAME_MINUTES = 5
BARS_LOOKBACK         = 100
DAILY_BARS_LOOKBACK   = 30

# PEAD (post-earnings announcement drift) detection thresholds
PEAD_EPS_SURPRISE_MIN   = 5.0   # ≥ 5% EPS beat required
PEAD_DAY_RETURN_MIN     = 3.0   # ≥ 3% stock gain on earnings day
PEAD_CLOSE_POSITION_MIN = 0.7   # closed in upper 30% of day's range
PEAD_VOLUME_RATIO_MIN   = 2.0   # 2× average volume on earnings day
PEAD_SCORE_THRESHOLD    = 80    # need 4 of 5 conditions (each = 20 pts)
PEAD_EPS_BEAT_STRONG    = 20.0  # blowout beat threshold (30 pts)
PEAD_EPS_BEAT_SOLID     = 10.0  # strong beat threshold (25 pts)
PEAD_FWD_REVISION_MIN   = 3.0   # forward EPS revision % to qualify as 6th condition

# ── Tranche-2 scaled entry ────────────────────────────────────────────────────
TRANCHE1_SIZE_PCT        = 0.60   # T1 = 60% of computed position size
TRANCHE2_SIZE_PCT        = 0.40   # T2 = 40% of fresh size computed at T2 price/ATR
TRANCHE2_DROP_MIN_PCT    = 1.5    # T2 triggers when price ≥ 1.5% below T1 entry (pullback zone, above SL)
TRANCHE2_DROP_MAX_PCT    = 3.5    # T2 blocked if drop > 3.5% (approaching SL — likely breakdown)
TRANCHE2_RSI_MAX         = 60     # RSI ceiling at trigger (pullback, not necessarily deeply oversold)
TRANCHE2_COMPOSITE_MIN   = 10     # Composite floor at trigger (drops naturally on price decline; just avoid deeply negative)
TRANCHE2_SL_PCT          = 5.5    # Blended SL: 5.5% below T2 fill price
TRANCHE2_CUTOFF_HOUR     = 14     # No T2 triggers after 2:00 PM ET
TRANCHE2_MIN_HOLD_MIN    = 60     # T1 must be held ≥ 60 min before T2 eligible
TRANCHE2_MIN_SENTIMENT   = 20     # Symbol sentiment score floor for T2
TRANCHE2_SECTOR_WEAK_PCT = -1.0  # Sector ETF must not be down more than 1% for T2

# ── BACKTEST SIMULATION ────────────────────────────────────────────────────────

BACKTEST_STARTING_CAPITAL    = 100_000.0  # initial portfolio value for simulation
BACKTEST_SLIPPAGE_PCT        = 0.0005     # 0.05% slippage on entry and exit fills
BACKTEST_MAX_POSITIONS       = 5         # max simultaneous open positions in simulation
BACKTEST_CRISIS_VIX_THRESHOLD        = 35.0  # VIX above this is considered crisis-territory for recovery detection
BACKTEST_CRISIS_VIX_RECOVERY_DROP_PCT = 10.0  # % VIX drop from 5d peak → classify as CRASH_RECOVERY
BACKTEST_BULL_TREND_SPY_5D_PCT        = 1.5   # SPY 5-day return threshold for backtest BULL_TREND

# ── Data Resilience ──────────────────────────────────────────────────────────
DATA_RETRY_MAX                       = 3
DATA_RETRY_BACKOFF_PREMARKET         = [1, 3, 8]   # minutes; before 9:30 ET
DATA_RETRY_BACKOFF_INTRADAY          = [1, 2, 5]   # minutes; during market hours

DATA_STALE_THRESHOLD_MINUTES: dict[str, int] = {
    "regime":             420,   # regime fetched once at 8:55 AM; 7h covers full session
    "research":           360,
    "screener":            90,
    "earnings":           480,
    "economic_calendar":  480,
    "watch_list":          90,
}
DATA_COMPOSITE_PENALTY: dict[str, int] = {
    "regime":    10,
    "research":   5,
    "screener":   0,
    "earnings":   0,
    "watch_list": 0,
}
DATA_DISABLE_AUTO_EXECUTE            = frozenset({"regime"})
DATA_DISABLE_T2                      = frozenset({"regime"})
DATA_REGIME_CANOPEN_MAX_MINUTES      = 480   # 8h covers full day from 8:55 AM to 4:55 PM
DATA_SWING_STALE_FALLBACK_REGIME     = "NORMAL"
DATA_SCREENER_FAILURE_RATE_THRESHOLD = 0.80
DATA_WATCH_LIST_HEALTH_FILE          = STATE_DIR / "watch_list_health.json"

# ── Dynamic Regime × Tier System ──────────────────────────────────────────────
SUB_ETF_CONFIRM_MIN_03 = 0.3   # sub-sector ETF % floor for OPPORTUNITY/HIGH auto-execute
SUB_ETF_CONFIRM_MIN_05 = 0.5   # sub-sector ETF % floor for OPPORTUNITY/CONVICTION auto-execute

# ── Plan: Profitability Audit ──────────────────────────────────────────────────

# Track 0 — CATALYST mode
CATALYST_SETTLE_MINUTES     = 30     # min after event before CATALYST entries open
CATALYST_WINDOW_MINUTES     = 120    # post_event_window lookback; expires after this
CATALYST_ETF_MIN_MOVE_PCT   = 0.5   # sub-sector ETF must move >= this % to confirm
CATALYST_MAX_POSITIONS      = 2     # max simultaneous CATALYST-mode open positions
CATALYST_MIN_SURPRISE_SCORE = -0.1  # FRED surprise gate; negative still passes

# Track 4 — intraday staleness
INTRADAY_NEWS_STALE_MINUTES = 45

# Track 6 — FRED signal enrichment
FRED_CPI_TARGET_PCT              = 2.0    # Fed inflation target
FRED_CPI_OBS_LOOKBACK            = 13     # CPIAUCSL observations for 12-month YoY
FRED_YIELD_CURVE_SERIES          = "T10Y2Y"
FRED_CREDIT_SPREAD_SERIES        = "BAMLH0A0HYM2"
FRED_BREAKEVEN_SERIES            = "T10YIE"
FRED_CLAIMS_SERIES               = "ICSA"
FRED_YIELD_CURVE_INVERSION_THRESHOLD = 0.0   # T10Y2Y < 0 → inverted → DEFENSIVE
FRED_CREDIT_SPREAD_RISK_OFF      = 5.0    # FRED reports BAMLH0A0HYM2 in % (e.g. 5.0 = 500 bps); >= this → risk-off
FRED_BREAKEVEN_HIGH_THRESHOLD    = 3.0   # % breakeven > this → modifier -2
FRED_CLAIMS_SPIKE_THRESHOLD      = 30000 # WoW initial claims increase >= this
FRED_MODIFIER_MIN                = -15   # expanded clamp (was effectively ±8)
FRED_MODIFIER_MAX                = 15
