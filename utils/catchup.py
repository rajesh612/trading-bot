import logging
import threading
from datetime import datetime
import config
from utils.constants import ET
import notifications.telegram as telegram_notify

logger = logging.getLogger("main")


def _seed_tracker_if_fresh(tracker, source_id: str, file_path) -> None:
    """Seed the in-memory health tracker from a file's mtime if the file is from today.

    Called by catchup when skipping an agent because its output is already fresh.
    Without seeding, _run_intraday_retries() creates phantom _entry() state
    with last_success_at=None, causing stale_sources() to report inf-age sources.
    """
    try:
        from pathlib import Path
        path = Path(file_path)
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=ET)
        if mtime.date() == datetime.now(ET).date():
            tracker.record_success(source_id, now=mtime)
    except (FileNotFoundError, OSError):
        pass


def _file_is_from_today(path) -> bool:
    """Return True if the file exists and was last modified on today's ET date."""
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=ET)
        return mtime.date() == datetime.now(ET).date()
    except FileNotFoundError:
        return False


def catchup_missed_agents() -> None:
    """Run any pre-market agents whose output files are missing or stale for today.

    Called once at startup, before the scheduler takes over. Only runs on weekdays
    after 8:45 AM ET. Agents run in dependency order so downstream agents always
    have fresh upstream data.
    """
    # Deferred import to avoid circular reference at module load time
    import scheduler_jobs as _sj
    from utils.data_health import _tracker

    now = datetime.now(ET)
    if now.weekday() >= 5:
        return  # weekend — nothing to do

    market_already_closed = now.hour > 16 or (now.hour == 16 and now.minute >= 15)
    if market_already_closed:
        logger.info("CATCHUP: post-market start — skipping agent catch-up.")
        return

    pre_market_started = now.hour >= 8
    if not pre_market_started:
        logger.info("CATCHUP: too early (before 8:00 AM) — no catch-up needed yet.")
        return

    logger.info("CATCHUP: checking for missed pre-market agents…")

    # 1. Earnings calendar (needed for swing manager EOD hold qualification)
    if not _file_is_from_today(config.EARNINGS_CALENDAR_FILE):
        logger.info("CATCHUP: running earnings calendar (earnings_calendar.json is stale)…")
        _sj.job_earnings_calendar()
    else:
        logger.info("CATCHUP: earnings calendar already ran today — skipping.")
        _seed_tracker_if_fresh(_tracker, "earnings", config.EARNINGS_CALENDAR_FILE)

    # 2. Volatility screener (needed by watch list agent)
    if not _file_is_from_today(config.DAILY_TRADEABLE_FILE):
        logger.info("CATCHUP: running volatility screener (daily_tradeable.txt is stale)…")
        try:
            _sj.job_volatility_screener()
        except Exception as exc:
            # _job_guard inside job_volatility_screener already sent a Telegram alert;
            # swallow here so we can run the post-check and skip downstream agents cleanly.
            logger.error("CATCHUP: volatility screener raised: %s — checking file state", exc)
    else:
        logger.info("CATCHUP: volatility screener already ran today — skipping.")
        _seed_tracker_if_fresh(_tracker, "screener", config.DAILY_TRADEABLE_FILE)

    if not _file_is_from_today(config.DAILY_TRADEABLE_FILE):
        logger.error(
            "CATCHUP: daily_tradeable.txt still missing after screener run — "
            "watch list and research depend on it; skipping downstream agents."
        )
        return

    # 3. Economic calendar (needed for event-risk gate)
    if not _file_is_from_today(config.ECONOMIC_CALENDAR_FILE):
        logger.info("CATCHUP: running economic calendar (economic_calendar.json is stale)…")
        _sj.job_economic_calendar()
    else:
        logger.info("CATCHUP: economic calendar already ran today — skipping.")

    # 4. Macro regime (replaces VIX regime — needed by trading agent for position sizing)
    if not _file_is_from_today(config.MACRO_REGIME_FILE):
        logger.info("CATCHUP: running macro regime (macro_regime.json is stale)…")
        _sj.job_macro_regime()
    else:
        logger.info("CATCHUP: macro regime already ran today — skipping.")
        _seed_tracker_if_fresh(_tracker, "regime", config.MACRO_REGIME_FILE)

    # 5. Watch list agent (needed by research + trading agents)
    if not _file_is_from_today(config.WATCH_LIST_FILE):
        logger.info("CATCHUP: running watch list agent (watch_list.txt is stale)…")
        _sj.job_watch_list()
    else:
        logger.info("CATCHUP: watch list already built today — skipping.")
        _seed_tracker_if_fresh(_tracker, "watch_list", config.WATCH_LIST_FILE)

    # 6. Research agent — run in background thread so it doesn't block the scheduler.
    # Fetching news + 15 Claude calls can take 3-5 minutes; the scheduler must start
    # first so trading cycles don't miss their windows while research is running.
    market_still_open = now.hour < 15 or (now.hour == 15 and now.minute < 50)
    if market_still_open:
        if not _file_is_from_today(config.RESEARCH_REPORT_FILE):
            logger.info("CATCHUP: research report stale — launching in background thread (will not block scheduler)…")
            def _run_research():
                try:
                    _sj.job_research()
                except Exception as exc:
                    logger.exception("CATCHUP: background research thread failed: %s", exc)
                    telegram_notify.notify_job_failure("catchup-research", f"{type(exc).__name__}: {exc}")
            threading.Thread(target=_run_research, daemon=True, name="catchup-research").start()
        else:
            logger.info("CATCHUP: research report already built today — skipping.")
            _seed_tracker_if_fresh(_tracker, "research", config.RESEARCH_REPORT_FILE)
    else:
        logger.info("CATCHUP: market closing soon — skipping research catch-up.")

    logger.info("CATCHUP: done (research may still be running in background).")


def catchup_daily_report() -> None:
    """Send the daily report on startup if it was missed (bot offline at 4:20 PM ET)."""
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return  # weekend
    report_due = now.hour > 16 or (now.hour == 16 and now.minute >= 20)
    if not report_due:
        return
    from notifications.daily_report import send_daily_report
    logger.info("CATCHUP: checking if daily report needs to be sent…")
    send_daily_report()
