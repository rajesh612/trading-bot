import logging
import time
from datetime import datetime
import config
import notifications.telegram as telegram_notify
from utils.constants import ET

logger = logging.getLogger("main")

# Set by main.py after the scheduler is created, so job_force_close can pause/resume jobs.
_scheduler = None

# Set by main.py so job_heartbeat can flush the log file and update the watchdog timestamp.
_file_handler = None
_last_heartbeat_ts = None


def set_scheduler(sched) -> None:
    global _scheduler
    _scheduler = sched


def set_heartbeat_refs(file_handler, last_heartbeat_ts) -> None:
    """Wire up logging file handler and watchdog timestamp list from main.py."""
    global _file_handler, _last_heartbeat_ts
    _file_handler = file_handler
    _last_heartbeat_ts = last_heartbeat_ts


def _job_guard(name: str, fn) -> None:
    """Run fn(); send a Telegram alert on exception, then re-raise so APScheduler logs it."""
    try:
        fn()
    except Exception as exc:
        telegram_notify.notify_job_failure(name, f"{type(exc).__name__}: {exc}")
        raise


def _read_watch_list() -> list[str]:
    try:
        return [s.strip() for s in config.WATCH_LIST_FILE.read_text().splitlines() if s.strip()]
    except FileNotFoundError:
        return []


def job_earnings_calendar():
    logger.info("━━━ EARNINGS CALENDAR ━━━")
    def _run():
        from calendars.earnings import refresh_earnings_calendar
        from tier1_config import TIER1_NAMES
        watch_list = _read_watch_list()
        symbols = list(dict.fromkeys(watch_list + list(TIER1_NAMES.keys())))
        refresh_earnings_calendar(symbols)
    _job_guard("earnings_calendar", _run)


def job_economic_calendar():
    logger.info("━━━ ECONOMIC CALENDAR ━━━")
    from macro.economic_calendar import refresh_economic_calendar
    _job_guard("economic_calendar", refresh_economic_calendar)


def job_macro_regime():
    logger.info("━━━ MACRO REGIME ━━━")
    from macro_regime import fetch_macro_regime
    _job_guard("macro_regime", fetch_macro_regime)


def job_fred_cache():
    logger.info("━━━ FRED CACHE REFRESH ━━━")
    def _run():
        from macro_regime import refresh_fred_cache
        from trading.cycle_context import invalidate_fred_cache
        refresh_fred_cache()
        invalidate_fred_cache()
    _job_guard("fred_cache", _run)


def job_volatility_screener():
    logger.info("━━━ VOLATILITY SCREENER ━━━")
    from agents.volatility_screener import run_volatility_screener
    _job_guard("volatility_screener", run_volatility_screener)


def job_watch_list():
    logger.info("━━━ WATCH LIST AGENT ━━━")
    def _run():
        from trading.journal import reset_daily_closed
        reset_daily_closed()
        from agents.watch_list import run_watch_list_agent
        run_watch_list_agent()
    _job_guard("watch_list", _run)


def job_research():
    logger.info("━━━ RESEARCH AGENT ━━━")
    def _run():
        from research.agent import run_research_agent
        watch_list = _read_watch_list()
        if not watch_list:
            from agents.watch_list import _load_universe
            watch_list = _load_universe()[: config.WATCH_LIST_MAX]
        run_research_agent(watch_list)
    _job_guard("research", _run)


def job_trading():
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return
    if now.hour < 9 or (now.hour == 9 and now.minute < 30):
        return
    if now.hour > 15 or (now.hour == 15 and now.minute >= 55):
        return
    logger.info("━━━ TRADING CYCLE ━━━")
    from trading.cycle import run_trading_cycle
    _job_guard("trading_cycle", run_trading_cycle)


def job_position_monitor():
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return
    if now.hour < 9 or (now.hour == 9 and now.minute < 30):
        return
    if now.hour > 15 or (now.hour == 15 and now.minute >= 55):
        return
    from trading.monitor import run_position_monitor
    _job_guard("position_monitor", run_position_monitor)


def job_force_close():
    logger.info("━━━ FORCE CLOSE ALL POSITIONS ━━━")
    from trading.closer import force_close_all
    # Pause position_monitor to prevent double-close / double-journal during EOD sweep (RC-4)
    try:
        if _scheduler is not None:
            _scheduler.pause_job("position_monitor")
    except Exception:
        pass
    try:
        _job_guard("force_close", force_close_all)
    finally:
        try:
            if _scheduler is not None:
                _scheduler.resume_job("position_monitor")
        except Exception:
            pass


def job_evaluate_eod_holds():
    logger.info("━━━ SWING EOD EVALUATION ━━━")
    from swing_manager import evaluate_eod_holds
    _job_guard("evaluate_eod_holds", evaluate_eod_holds)


def job_swing_gap_check():
    logger.info("━━━ SWING GAP CHECK ━━━")
    def _run():
        from swing_manager import run_gap_check
        from broker.alpaca_client import AlpacaClient
        run_gap_check(AlpacaClient())
    _job_guard("swing_gap_check", _run)


def job_market_monitor():
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return
    if now.hour < 9 or (now.hour == 9 and now.minute < 30):
        return
    if now.hour >= 16:
        return
    from utils.market_monitor import run_market_monitor
    _job_guard("market_monitor", run_market_monitor)


def job_learning():
    logger.info("━━━ LEARNING AGENT ━━━")
    from learning.agent import run_learning_agent
    _job_guard("learning_agent", run_learning_agent)


def job_daily_report():
    logger.info("━━━ DAILY REPORT ━━━")
    from daily_report import send_daily_report
    _job_guard("daily_report", send_daily_report)


def job_intraday_news():
    logger.info("━━━ INTRADAY NEWS ━━━")
    from research.intraday import refresh_intraday_news
    _job_guard("intraday_news", lambda: refresh_intraday_news(_read_watch_list()))


def job_budget_rollover():
    logger.info("━━━ BUDGET ROLLOVER ━━━")
    from trading.risk_manager import try_rollover_exit_to_entry
    _job_guard("budget_rollover", lambda: try_rollover_exit_to_entry(datetime.now(ET)))


def job_midday_check():
    """Fire at noon ET: alert if zero BUY trades have executed today."""
    now = datetime.now(ET)
    try:
        from trading.journal import load_journal
        today = now.date().isoformat()
        journal = load_journal()
        buys_today = [e for e in journal if e.get("action") == "BUY" and e.get("timestamp", "").startswith(today)]
        if buys_today:
            return  # trades already executed — no alert needed

        from macro_regime import load_macro_regime
        regime_data = load_macro_regime()
        regime = regime_data.get("regime", "UNKNOWN")

        from utils.io_utils import load_json
        budget = load_json(config.API_BUDGET_FILE, {})
        spent = budget.get("estimated_cost_usd", 0.0)
        ceiling = budget.get("ceiling_usd", 1.0)
        budget_pct = spent / ceiling * 100 if ceiling else 0

        # Estimate cycles run: 9:30→noon every 10 min = ~15 cycles
        entry_start = now.replace(hour=9, minute=30, second=0, microsecond=0)
        elapsed_min = (now - entry_start).total_seconds() / 60
        cycles_run = max(0, int(elapsed_min / 10))

        telegram_notify.notify_no_trades_midday(regime, cycles_run, budget_pct)
        logger.warning("Midday check: 0 trades executed after %d cycles — Telegram alert sent.", cycles_run)
    except Exception as exc:
        logger.warning("job_midday_check failed: %s", exc)


def job_heartbeat():
    now = datetime.now(ET)
    try:
        from trading.journal import load_open_positions
        open_pos = load_open_positions()
        n_pos = len(open_pos)
    except Exception:
        n_pos = "?"
    logger.info(f"♥ HEARTBEAT — bot alive | {now.strftime('%H:%M ET')} | open positions: {n_pos}")
    if _file_handler is not None:
        _file_handler.stream.flush()
    if _last_heartbeat_ts is not None:
        _last_heartbeat_ts[0] = time.time()
