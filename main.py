import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
from datetime import datetime
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

import config
import notifications.telegram as telegram_notify
from utils.instance_lock import acquire_instance_lock, kill_existing_instance, write_pid, clear_pid
from utils.startup import validate_state_files, cleanup_stale_files, exit_with_alert
from utils.bot_health import reset_bot_health, check_crash_loop, reconcile_positions
from utils.catchup import catchup_missed_agents, catchup_daily_report
from utils.constants import ET
from scheduler_jobs import (
    set_scheduler,
    set_heartbeat_refs,
    _job_guard,
    job_earnings_calendar,
    job_economic_calendar,
    job_macro_regime,
    job_fred_cache,
    job_volatility_screener,
    job_watch_list,
    job_research,
    job_trading,
    job_position_monitor,
    job_force_close,
    job_evaluate_eod_holds,
    job_swing_gap_check,
    job_market_monitor,
    job_learning,
    job_daily_report,
    job_heartbeat,
    job_intraday_news,
    job_budget_rollover,
    job_midday_check,
)

# Backward-compat aliases so existing tests that import `main._xxx` continue to work.
_check_crash_loop = check_crash_loop
_reset_bot_health = reset_bot_health
_exit_with_alert  = exit_with_alert

# Ensure runtime directories exist before any file I/O
config.STATE_DIR.mkdir(parents=True, exist_ok=True)
(config.BASE_DIR / "logs").mkdir(exist_ok=True)

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"

# Rotate at 5 MB, keep 5 backups (trading_bot.log.1 … .5); oldest auto-deleted
_file_handler = logging.handlers.RotatingFileHandler(
    config.BASE_DIR / "logs" / "trading_bot.log",
    maxBytes=5 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
_file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))

_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.setFormatter(logging.Formatter(_LOG_FORMAT))

logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _stream_handler])
# Force flush after every log record
logging.getLogger().handlers[0].flush = lambda: (_file_handler.stream.flush(), None)[1]

logger = logging.getLogger("main")
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


def _log_capability_summary() -> list[str]:
    """
    Log one line per integration at startup. Returns degraded item descriptions
    for Telegram alert. Called once after hard key validation.
    """
    import json
    degraded: list[str] = []

    # FRED
    if config.FRED_API_KEY:
        try:
            fred_data = json.loads(config.FRED_CACHE_FILE.read_text()) if config.FRED_CACHE_FILE.exists() else {}
            fed = fred_data.get("fed_direction", "unknown")
            cpi = fred_data.get("cpi_trend", "unknown")
            logger.info(f"[✓] FRED         — macro modifier active (Fed {fed}, CPI {cpi})")
        except Exception as exc:
            logger.warning("[~] FRED         — key set, cache unreadable: %s", exc)
    else:
        logger.info("[✗] FRED         — macro regime modifier = 0 (key not set)")
        degraded.append("FRED key missing — macro regime modifier = 0 (neutral)")

    # Polygon
    if config.POLYGON_API_KEY:
        logger.info("[✓] Polygon      — flash-crash dual-source verification active")
    else:
        logger.info("[✗] Polygon      — flash-crash single-source only (SPY two-reading fallback)")
        degraded.append("Polygon key missing — flash-crash confirmation is single-source only")

    # Reddit
    logger.info("[✓] Reddit       — public JSON API, no credentials needed")

    # X / Twitter
    if config.X_BEARER_TOKEN:
        logger.info("[✓] X/Twitter    — tweet sentiment active")
    else:
        logger.info("[~] X/Twitter    — disabled (X_BEARER_TOKEN not set; $100/mo optional)")

    # Email
    if config.EMAIL_SENDER and config.EMAIL_APP_PASSWORD:
        logger.info(f"[✓] Email        — daily report → {config.EMAIL_RECIPIENT or config.EMAIL_SENDER}")
    else:
        logger.info("[✗] Email        — daily report will not be sent (EMAIL_SENDER not configured)")
        degraded.append("Email not configured — daily P&L report will not be sent")

    # Telegram
    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
        logger.info("[✓] Telegram     — notifications active")
    else:
        logger.info("[~] Telegram     — disabled (TELEGRAM_BOT_TOKEN not set)")

    # Market data provider
    logger.info(f"[~] Market data  — provider={config.MARKET_DATA_PROVIDER}")

    return degraded


def setup_scheduler():
    kw = dict(day_of_week="mon-fri", timezone=ET)
    # Pre-market jobs: can fire up to 5 min late (slow startup or network delay) and still run.
    PRE_MARKET_KW = dict(**kw, max_instances=1, misfire_grace_time=300)

    # economic_calendar at 8:20 to avoid race condition with 8:30 bot startup
    scheduler.add_job(job_earnings_calendar,   CronTrigger(hour=8,  minute=0,  **kw), id="earnings_calendar",   replace_existing=True, max_instances=1, misfire_grace_time=300)
    scheduler.add_job(job_economic_calendar,   CronTrigger(hour=config.ECONOMIC_CALENDAR_HOUR, minute=config.ECONOMIC_CALENDAR_MINUTE, **kw), id="economic_calendar", replace_existing=True, max_instances=1, misfire_grace_time=300)
    scheduler.add_job(job_volatility_screener, CronTrigger(hour=8,  minute=45, **kw), id="volatility_screener", replace_existing=True, **{k: v for k, v in PRE_MARKET_KW.items() if k not in kw})
    scheduler.add_job(job_macro_regime,        CronTrigger(hour=8,  minute=55, **kw), id="macro_regime",        replace_existing=True, max_instances=1, misfire_grace_time=300)
    scheduler.add_job(job_watch_list,          CronTrigger(hour=9,  minute=0,  **kw), id="watch_list",          replace_existing=True, max_instances=1, misfire_grace_time=300)
    scheduler.add_job(job_swing_gap_check,     CronTrigger(hour=9,  minute=30, **kw), id="swing_gap_check",     replace_existing=True, max_instances=1, misfire_grace_time=120)
    scheduler.add_job(job_evaluate_eod_holds,  CronTrigger(hour=15, minute=50, **kw), id="evaluate_eod_holds",  replace_existing=True, max_instances=1, misfire_grace_time=30)
    scheduler.add_job(job_force_close,         CronTrigger(hour=15, minute=55, **kw), id="force_close",         replace_existing=True, max_instances=1, misfire_grace_time=30)
    scheduler.add_job(job_fred_cache,          CronTrigger(hour=16, minute=0,  **kw), id="fred_cache",          replace_existing=True, max_instances=1, misfire_grace_time=300)
    scheduler.add_job(job_learning,            CronTrigger(hour=16, minute=15, **kw), id="learning",            replace_existing=True, max_instances=1, misfire_grace_time=300)
    scheduler.add_job(job_daily_report,        CronTrigger(hour=16, minute=20, **kw), id="daily_report",        replace_existing=True, max_instances=1, misfire_grace_time=300)

    # Research once at 9:15 AM — after watch list is built, before first trades
    scheduler.add_job(job_research, CronTrigger(hour=9, minute=15, **kw), id="research", replace_existing=True, max_instances=1, misfire_grace_time=300)

    # Trading every 10 min during market hours
    scheduler.add_job(
        job_trading,
        CronTrigger(minute="*/10", hour="9-15", **kw),
        id="trading",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=30,
    )
    # Position monitor every 60 seconds — catches SL/TP hits between trading cycles
    scheduler.add_job(
        job_position_monitor,
        CronTrigger(second="0", minute="*", hour="9-15", **kw),
        id="position_monitor",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=30,
    )
    # Flash-crash market monitor every 5 min — one SPY call, no Claude
    scheduler.add_job(
        job_market_monitor,
        CronTrigger(minute="*/5", hour="9-15", **kw),
        id="market_monitor",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=60,
    )
    # Intraday news refresh every 30 min during market hours
    scheduler.add_job(
        job_intraday_news,
        CronTrigger(minute="*/30", hour="9-15", **kw),
        id="intraday_news",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=30,
    )
    # Midday health check at 12:00 PM ET — alerts if zero trades by noon
    scheduler.add_job(
        job_midday_check,
        CronTrigger(hour=12, minute=0, **kw),
        id="midday_check",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=300,
    )
    # Budget rollover at 12:30 PM ET
    scheduler.add_job(
        job_budget_rollover,
        CronTrigger(hour=config.BUDGET_ROLLOVER_HOUR, minute=config.BUDGET_ROLLOVER_MINUTE, **kw),
        id="budget_rollover",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=300,
    )
    # Heartbeat every 30 min all day — confirms scheduler is alive
    scheduler.add_job(
        job_heartbeat,
        CronTrigger(minute="*/30", **kw),
        id="heartbeat",
        replace_existing=True,
        max_instances=1,
    )


def _handle_shutdown(signum, frame):
    logger.info("Shutdown signal received — stopping.")
    clear_pid()
    scheduler.shutdown(wait=False)
    reset_bot_health()
    telegram_notify.notify_shutdown()
    sys.exit(0)


def main():
    # Acquire exclusive lock first — exits immediately if another instance is live.
    # This prevents launchd + start.sh races from spawning two bots simultaneously.
    acquire_instance_lock()
    check_crash_loop()

    logger.info("Trading Bot initialising…")

    if not config.ALPACA_API_KEY or not config.ALPACA_SECRET_KEY:
        exit_with_alert("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY in .env")
    if not config.ANTHROPIC_API_KEY:
        exit_with_alert("Missing ANTHROPIC_API_KEY in .env")

    degraded_integrations = _log_capability_summary()
    if degraded_integrations:
        telegram_notify.notify_degraded_integrations(degraded_integrations)

    kill_existing_instance()
    write_pid()

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT,  _handle_shutdown)

    validate_state_files()
    cleanup_stale_files()

    # Wire scheduler and heartbeat refs into scheduler_jobs before reconcile
    # (reconcile may call job_force_close which needs _scheduler)
    set_scheduler(scheduler)
    set_heartbeat_refs(_file_handler, _last_heartbeat_ts)

    reconcile_positions()
    catchup_missed_agents()
    catchup_daily_report()

    setup_scheduler()

    logger.info("Scheduled jobs (all times ET, weekdays only):")
    for job in scheduler.get_jobs():
        logger.info(f"  [{job.id}] {job.trigger}")

    threading.Thread(target=_watchdog, daemon=True, name="watchdog").start()
    logger.info("Watchdog thread started.")
    logger.info("Bot is live. Press Ctrl+C to stop.")

    from macro.regime import load_macro_regime
    from trading.journal import load_open_positions
    try:
        regime = load_macro_regime().get("regime", "UNKNOWN")
        n_pos  = len(load_open_positions())
    except Exception:
        regime, n_pos = "UNKNOWN", 0
    telegram_notify.notify_startup(regime, n_pos)

    scheduler.start()


if __name__ == "__main__":
    main()
