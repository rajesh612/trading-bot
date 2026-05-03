import fcntl
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

import config

PID_FILE  = config.BASE_DIR / "trading_bot.pid"
LOCK_FILE = config.BASE_DIR / "trading_bot.lock"
_lock_fh  = None  # held open for the lifetime of this process to keep the lock

# Unbuffered handlers so every log line hits disk immediately
_file_handler = logging.FileHandler(config.BASE_DIR / "trading_bot.log")
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s — %(message)s"))

_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s — %(message)s"))

logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _stream_handler])
# Force flush after every log record
logging.getLogger().handlers[0].flush = lambda: (_file_handler.stream.flush(), None)[1]

logger = logging.getLogger("main")
ET = ZoneInfo("America/New_York")
scheduler = BlockingScheduler(timezone=ET)

# Watchdog: last time the heartbeat ran (as a one-element list to allow mutation in threads)
_last_heartbeat_ts = [time.time()]


def _watchdog():
    """Exit the process if the scheduler freezes on a weekday (missed >1 heartbeat window)."""
    while True:
        time.sleep(120)
        now = datetime.now(ET)
        if now.weekday() >= 5:
            _last_heartbeat_ts[0] = time.time()  # reset over the weekend
            continue
        elapsed = time.time() - _last_heartbeat_ts[0]
        if elapsed > 35 * 60:  # 35 min — more than one 30-min heartbeat window
            logger.critical(
                f"WATCHDOG: Scheduler frozen ({elapsed / 60:.1f} min since last heartbeat). "
                "Forcing exit so launchd can restart."
            )
            _file_handler.stream.flush()
            os._exit(1)


# ── Single-instance lock ─────────────────────────────────────────────────────

def _acquire_instance_lock():
    """Exit immediately if another bot process is already running.

    Uses an exclusive flock so this is race-condition-free regardless of
    whether the competing process was launched by launchd, start.sh, or
    directly. The lock is held for the lifetime of this process via _lock_fh.
    """
    global _lock_fh
    _lock_fh = open(LOCK_FILE, "w")
    try:
        fcntl.flock(_lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fh.write(str(os.getpid()))
        _lock_fh.flush()
    except IOError:
        print(
            "[trading_bot] Another instance is already running — exiting.",
            flush=True,
        )
        sys.exit(0)


# ── PID management ───────────────────────────────────────────────────────────

def _kill_existing_instance():
    if not PID_FILE.exists():
        return
    try:
        old_pid = int(PID_FILE.read_text().strip())
        if old_pid == os.getpid():
            return
        os.kill(old_pid, 0)  # check if alive
        logger.info(f"Killing existing instance PID {old_pid}…")
        os.kill(old_pid, signal.SIGTERM)
        time.sleep(3)
        try:
            os.kill(old_pid, signal.SIGKILL)  # force-kill if still alive
        except ProcessLookupError:
            pass
        logger.info(f"Previous instance PID {old_pid} terminated.")
    except (ValueError, ProcessLookupError):
        pass  # pid file stale or process already gone


def _write_pid():
    PID_FILE.write_text(str(os.getpid()))


def _clear_pid():
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# ── Job wrappers ─────────────────────────────────────────────────────────────

def job_earnings_calendar():
    logger.info("━━━ EARNINGS CALENDAR ━━━")
    from earnings_calendar import refresh_earnings_calendar
    from tier1_config import TIER1_NAMES
    watch_list = []
    if config.WATCH_LIST_FILE.exists():
        watch_list = [t.strip() for t in config.WATCH_LIST_FILE.read_text().splitlines() if t.strip()]
    symbols = list(dict.fromkeys(watch_list + list(TIER1_NAMES.keys())))
    refresh_earnings_calendar(symbols)


def job_economic_calendar():
    logger.info("━━━ ECONOMIC CALENDAR ━━━")
    from economic_calendar import refresh_economic_calendar
    refresh_economic_calendar()


def job_macro_regime():
    logger.info("━━━ MACRO REGIME ━━━")
    from macro_regime import fetch_macro_regime
    fetch_macro_regime()


def job_fred_cache():
    logger.info("━━━ FRED CACHE REFRESH ━━━")
    from macro_regime import refresh_fred_cache
    refresh_fred_cache()


def job_volatility_screener():
    logger.info("━━━ VOLATILITY SCREENER ━━━")
    from volatility_screener import run_volatility_screener
    run_volatility_screener()


def job_watch_list():
    logger.info("━━━ WATCH LIST AGENT ━━━")
    from trading_agent import reset_daily_closed
    reset_daily_closed()
    from watch_list_agent import run_watch_list_agent
    run_watch_list_agent()


def job_research():
    logger.info("━━━ RESEARCH AGENT ━━━")
    from research_agent import run_research_agent
    if config.WATCH_LIST_FILE.exists():
        watch_list = [t.strip() for t in config.WATCH_LIST_FILE.read_text().splitlines() if t.strip()]
    else:
        from watch_list_agent import _load_universe
        watch_list = _load_universe()[: config.WATCH_LIST_MAX]
    run_research_agent(watch_list)


def job_trading():
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return
    if now.hour < 9 or (now.hour == 9 and now.minute < 30):
        return
    if now.hour > 15 or (now.hour == 15 and now.minute >= 55):
        return
    logger.info("━━━ TRADING CYCLE ━━━")
    from trading_agent import run_trading_cycle
    run_trading_cycle()


def job_position_monitor():
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return
    if now.hour < 9 or (now.hour == 9 and now.minute < 30):
        return
    if now.hour > 15 or (now.hour == 15 and now.minute >= 55):
        return
    from trading_agent import run_position_monitor
    run_position_monitor()


def job_force_close():
    logger.info("━━━ FORCE CLOSE ALL POSITIONS ━━━")
    from trading_agent import force_close_all
    force_close_all()


def job_learning():
    logger.info("━━━ LEARNING AGENT ━━━")
    from learning_agent import run_learning_agent
    run_learning_agent()


def job_daily_report():
    logger.info("━━━ DAILY REPORT ━━━")
    from daily_report import send_daily_report
    send_daily_report()


def job_heartbeat():
    now = datetime.now(ET)
    try:
        from trading_agent import load_open_positions
        open_pos = load_open_positions()
        n_pos = len(open_pos)
    except Exception:
        n_pos = "?"
    logger.info(f"♥ HEARTBEAT — bot alive | {now.strftime('%H:%M ET')} | open positions: {n_pos}")
    _file_handler.stream.flush()
    _last_heartbeat_ts[0] = time.time()


# ── Scheduler setup ──────────────────────────────────────────────────────────

def setup_scheduler():
    kw = dict(day_of_week="mon-fri", timezone=ET)

    scheduler.add_job(job_earnings_calendar,   CronTrigger(hour=8,  minute=0,  **kw), id="earnings_calendar",   replace_existing=True)
    scheduler.add_job(job_economic_calendar,   CronTrigger(hour=8,  minute=30, **kw), id="economic_calendar",   replace_existing=True)
    scheduler.add_job(job_volatility_screener, CronTrigger(hour=8,  minute=45, **kw), id="volatility_screener", replace_existing=True)
    scheduler.add_job(job_macro_regime,        CronTrigger(hour=8,  minute=55, **kw), id="macro_regime",        replace_existing=True)
    scheduler.add_job(job_watch_list,          CronTrigger(hour=9,  minute=0,  **kw), id="watch_list")
    scheduler.add_job(job_force_close,         CronTrigger(hour=15, minute=55, **kw), id="force_close",   replace_existing=True)
    scheduler.add_job(job_fred_cache,          CronTrigger(hour=16, minute=0,  **kw), id="fred_cache",    replace_existing=True)
    scheduler.add_job(job_learning,            CronTrigger(hour=16, minute=15, **kw), id="learning",      replace_existing=True)
    scheduler.add_job(job_daily_report,        CronTrigger(hour=16, minute=30, **kw), id="daily_report",  replace_existing=True)

    # Research once at 9:15 AM — after watch list is built, before first trades
    scheduler.add_job(job_research, CronTrigger(hour=9, minute=15, **kw), id="research", replace_existing=True)

    # Trading every 10 min during market hours
    scheduler.add_job(
        job_trading,
        CronTrigger(minute="*/10", hour="9-15", **kw),
        id="trading",
        replace_existing=True,
    )
    # Position monitor every 60 seconds — catches SL/TP hits between trading cycles
    scheduler.add_job(
        job_position_monitor,
        CronTrigger(second="0", minute="*", hour="9-15", **kw),
        id="position_monitor",
        replace_existing=True,
    )
    # Heartbeat every 30 min all day — confirms scheduler is alive
    scheduler.add_job(
        job_heartbeat,
        CronTrigger(minute="*/30", **kw),
        id="heartbeat",
        replace_existing=True,
    )


def _reconcile_positions():
    """Sync local open_positions.json with Alpaca's actual open positions at startup.

    Three discrepancies can occur while the bot is offline:
    1. Bracket SL/TP fired → position closed in Alpaca but still in local state.
    2. Bot crashed mid-entry → position open in Alpaca but absent from local state.
    3. Bot starts after 3:55 PM with positions still open → force-close immediately.
    """
    from trading_agent import load_open_positions, save_open_positions, append_trade
    from alpaca_client import AlpacaClient

    logger.info("RECONCILE: Checking Alpaca positions against local state…")
    try:
        alpaca = AlpacaClient()
        alpaca_positions = alpaca.get_positions()        # {symbol: AlpacaPosition}
        local_positions  = load_open_positions()         # {symbol: dict}
        now   = datetime.now(ET)
        changed = False

        # ── 1. In Alpaca but not locally ──────────────────────────────────
        # Bot crashed between placing the order and writing open_positions.json,
        # or the entry was placed but the bot never wrote local state.
        for symbol, ap in alpaca_positions.items():
            if symbol not in local_positions:
                entry_price = float(ap.avg_entry_price)
                qty         = int(float(ap.qty))
                logger.warning(
                    f"RECONCILE: {symbol} open in Alpaca ({qty}@${entry_price:.2f}) "
                    "but missing from local state — restoring with default SL/TP"
                )
                local_positions[symbol] = {
                    "symbol":               symbol,
                    "qty":                  qty,
                    "entry_price":          entry_price,
                    "stop_loss_pct":        config.STOP_LOSS_PCT,
                    "take_profit_pct":      config.TAKE_PROFIT_PCT,
                    "stop_loss_price":      round(entry_price * (1 - config.STOP_LOSS_PCT  / 100), 2),
                    "take_profit_price":    round(entry_price * (1 + config.TAKE_PROFIT_PCT / 100), 2),
                    "bracket_order_id":     None,
                    "timestamp":            now.isoformat(),
                    "entry_composite_score": None,
                    "entry_sentiment_score": None,
                    "claude_reasoning":     "Restored at startup — original entry data unavailable.",
                    "indicator_signals":    {},
                    "entry_indicators":     {},
                }
                changed = True

        # ── 2. In local state but gone from Alpaca ────────────────────────
        # A bracket SL or TP order fired while the bot was offline.
        for symbol in list(local_positions.keys()):
            if symbol not in alpaca_positions:
                pos = local_positions[symbol]
                try:
                    exit_price = alpaca.get_latest_price(symbol)
                except Exception:
                    exit_price = pos["entry_price"]
                    logger.warning(f"RECONCILE: Cannot fetch last price for {symbol} — using entry price for P&L")

                pnl_pct    = (exit_price - pos["entry_price"]) / pos["entry_price"] * 100
                pnl_dollar = (exit_price - pos["entry_price"]) * pos["qty"]
                logger.warning(
                    f"RECONCILE: {symbol} in local state but gone from Alpaca — "
                    f"bracket fired while offline. P&L≈{pnl_pct:+.2f}% (${pnl_dollar:+.2f})"
                )
                append_trade({
                    "type":                      "CLOSE",
                    "symbol":                    symbol,
                    "timestamp":                 now.isoformat(),
                    "exit_price":                round(exit_price, 4),
                    "entry_price":               round(pos["entry_price"], 4),
                    "qty":                       pos["qty"],
                    "pnl_pct":                   round(pnl_pct, 2),
                    "pnl_dollar":                round(pnl_dollar, 2),
                    "close_reason":              "bracket_filled_while_bot_offline",
                    "entry_composite_score":     pos.get("entry_composite_score"),
                    "exit_composite_score":      None,
                    "exit_indicators":           {},
                    "entry_indicators":          pos.get("entry_indicators", {}),
                    "entry_sentiment_score":     pos.get("entry_sentiment_score"),
                    "claude_reasoning":          pos.get("claude_reasoning", ""),
                    "indicator_signals_at_entry": pos.get("indicator_signals", {}),
                })
                del local_positions[symbol]
                changed = True

        if changed:
            save_open_positions(local_positions)

        n = len(local_positions)
        if not changed:
            logger.info(f"RECONCILE: Positions in sync — {n} open, no discrepancies.")
        else:
            logger.info(f"RECONCILE: State updated — {n} position(s) now active.")

        # ── 3. Starting after 3:55 PM ET with open positions ──────────────
        # The scheduled force_close never ran (bot crashed or laptop closed).
        # Close everything now so we don't hold overnight.
        past_force_close = now.hour > 15 or (now.hour == 15 and now.minute >= 55)
        if past_force_close and local_positions:
            logger.warning(
                f"RECONCILE: Starting after 3:55 PM with {len(local_positions)} open "
                "position(s) — force-closing now to avoid overnight hold"
            )
            job_force_close()

    except Exception as exc:
        logger.error(f"RECONCILE: Failed — {exc}", exc_info=True)
        logger.error("RECONCILE: Local state may be stale — check Alpaca dashboard manually.")


def _file_is_from_today(path) -> bool:
    """Return True if the file exists and was last modified on today's ET date."""
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=ET)
        return mtime.date() == datetime.now(ET).date()
    except FileNotFoundError:
        return False


def _catchup_missed_agents():
    """Run any pre-market agents whose output files are missing or stale for today.

    Called once at startup, before the scheduler takes over. Only runs on weekdays
    after 8:45 AM ET. Agents run in dependency order so downstream agents always
    have fresh upstream data.
    """
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return  # weekend — nothing to do

    market_already_closed = now.hour > 16 or (now.hour == 16 and now.minute >= 15)
    if market_already_closed:
        logger.info("CATCHUP: post-market start — skipping agent catch-up.")
        return

    pre_market_started = now.hour > 8 or (now.hour == 8 and now.minute >= 45)
    if not pre_market_started:
        logger.info("CATCHUP: too early (before 8:45 AM) — no catch-up needed yet.")
        return

    logger.info("CATCHUP: checking for missed pre-market agents…")

    # 1. Earnings calendar (needed for swing manager EOD hold qualification)
    if not _file_is_from_today(config.EARNINGS_CALENDAR_FILE):
        logger.info("CATCHUP: running earnings calendar (earnings_calendar.json is stale)…")
        job_earnings_calendar()
    else:
        logger.info("CATCHUP: earnings calendar already ran today — skipping.")

    # 2. Volatility screener (needed by watch list agent)
    if not _file_is_from_today(config.DAILY_TRADEABLE_FILE):
        logger.info("CATCHUP: running volatility screener (daily_tradeable.txt is stale)…")
        job_volatility_screener()
    else:
        logger.info("CATCHUP: volatility screener already ran today — skipping.")

    # 2. Economic calendar (needed for event-risk gate)
    if not _file_is_from_today(config.ECONOMIC_CALENDAR_FILE):
        logger.info("CATCHUP: running economic calendar (economic_calendar.json is stale)…")
        job_economic_calendar()
    else:
        logger.info("CATCHUP: economic calendar already ran today — skipping.")

    # 3. Macro regime (replaces VIX regime — needed by trading agent for position sizing)
    if not _file_is_from_today(config.MACRO_REGIME_FILE):
        logger.info("CATCHUP: running macro regime (macro_regime.json is stale)…")
        job_macro_regime()
    else:
        logger.info("CATCHUP: macro regime already ran today — skipping.")

    # 4. Watch list agent (needed by research + trading agents)
    if not _file_is_from_today(config.WATCH_LIST_FILE):
        logger.info("CATCHUP: running watch list agent (watch_list.txt is stale)…")
        job_watch_list()
    else:
        logger.info("CATCHUP: watch list already built today — skipping.")

    # 5. Research agent — run in background thread so it doesn't block the scheduler.
    # Fetching news + 15 Claude calls can take 3-5 minutes; the scheduler must start
    # first so trading cycles don't miss their windows while research is running.
    market_still_open = now.hour < 15 or (now.hour == 15 and now.minute < 50)
    if market_still_open:
        if not _file_is_from_today(config.RESEARCH_REPORT_FILE):
            logger.info("CATCHUP: research report stale — launching in background thread (will not block scheduler)…")
            threading.Thread(target=job_research, daemon=True, name="catchup-research").start()
        else:
            logger.info("CATCHUP: research report already built today — skipping.")
    else:
        logger.info("CATCHUP: market closing soon — skipping research catch-up.")

    logger.info("CATCHUP: done (research may still be running in background).")


def _catchup_daily_report():
    """Send the daily report on startup if it was missed (bot offline at 4:30 PM ET)."""
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return  # weekend
    report_due = now.hour > 16 or (now.hour == 16 and now.minute >= 30)
    if not report_due:
        return
    from daily_report import send_daily_report
    logger.info("CATCHUP: checking if daily report needs to be sent…")
    send_daily_report()


def _handle_shutdown(signum, frame):
    logger.info("Shutdown signal received — stopping.")
    _clear_pid()
    scheduler.shutdown(wait=False)
    sys.exit(0)


def main():
    # Acquire exclusive lock first — exits immediately if another instance is live.
    # This prevents launchd + start.sh races from spawning two bots simultaneously.
    _acquire_instance_lock()

    logger.info("Trading Bot initialising…")

    if not config.ALPACA_API_KEY or not config.ALPACA_SECRET_KEY:
        logger.error("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY in .env")
        sys.exit(1)
    if not config.ANTHROPIC_API_KEY:
        logger.error("Missing ANTHROPIC_API_KEY in .env")
        sys.exit(1)

    _kill_existing_instance()
    _write_pid()

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT,  _handle_shutdown)

    _reconcile_positions()
    _catchup_missed_agents()
    _catchup_daily_report()

    setup_scheduler()

    logger.info("Scheduled jobs (all times ET, weekdays only):")
    for job in scheduler.get_jobs():
        logger.info(f"  [{job.id}] {job.trigger}")

    threading.Thread(target=_watchdog, daemon=True, name="watchdog").start()
    logger.info("Watchdog thread started.")
    logger.info("Bot is live. Press Ctrl+C to stop.")
    scheduler.start()


if __name__ == "__main__":
    main()
