"""Backtest orchestration: trading days, param loading, and run loop."""
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import config
from backtest import data as _data
from backtest import report as _report
from backtest import simulator as _sim
from utils.constants import ET
from utils.io_utils import load_json

logger = logging.getLogger(__name__)

# US market holidays (NYSE) — extend annually
_NYSE_HOLIDAYS = {
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26),
    date(2026, 12, 25),
    date(2025, 1, 1), date(2025, 1, 20), date(2025, 2, 17), date(2025, 4, 18),
    date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4), date(2025, 9, 1),
    date(2025, 11, 27), date(2025, 12, 25),
}


def _get_trading_days(start: datetime, end: datetime) -> list[datetime]:
    """Return market-open datetimes for weekdays excluding NYSE holidays in [start, end]."""
    days: list[datetime] = []
    d = start.date() if hasattr(start, "date") else start
    end_d = end.date() if hasattr(end, "date") else end
    while d <= end_d:
        if d.weekday() < 5 and d not in _NYSE_HOLIDAYS:
            days.append(datetime(d.year, d.month, d.day, tzinfo=ET))
        d += timedelta(days=1)
    return days


def _load_tickers() -> list[str]:
    """Read tickers from TICKERS_FILE; return list of symbols."""
    try:
        with open(config.TICKERS_FILE) as f:
            tickers = [
                line.strip().upper() for line in f
                if line.strip() and not line.strip().startswith("#")
            ]
    except FileNotFoundError:
        logger.warning("TICKERS_FILE not found: %s", config.TICKERS_FILE)
        return []
    if not tickers:
        logger.warning("TICKERS_FILE is empty: %s", config.TICKERS_FILE)
    return tickers


def _load_backtest_params() -> dict:
    """Load live-bot params from config; overlay learned params from trading_params.json."""
    base: dict = {
        "rsi_buy_max":              config.RSI_BUY_MAX,
        "rsi_buy_max_high_vol":     config.RSI_BUY_MAX_HIGH_VOL,
        "vwap_slope_min":           config.VWAP_SLOPE_MIN,
        "stoch_soft_threshold":     config.STOCH_SOFT_FLAG_THRESHOLD,
        "composite_min_normal":     config.COMPOSITE_MIN_NORMAL,
        "composite_min_high_vol":   config.COMPOSITE_MIN_HIGH_VOL,
        "auto_execute_composite_min": config.AUTO_EXECUTE_COMPOSITE_MIN,
        "stop_loss_pct":            config.STOP_LOSS_PCT,
        "take_profit_pct":          config.TAKE_PROFIT_PCT,
        "max_position_pct":         config.MAX_POSITION_PCT,
        "risk_per_trade_pct":       config.RISK_PER_TRADE_PCT,
        "high_vol_position_mult":   config.HIGH_VOL_POSITION_MULT,
        "max_positions":            config.SWING_MAX_POSITIONS,
    }
    learned = load_json(config.TRADING_PARAMS_FILE, {})
    if learned:
        logger.info("Backtest: overlaying learned params: %s", learned)
        for key in list(base.keys()):
            if learned.get(key) is not None:
                base[key] = learned[key]
    return base


def run_backtest(start: datetime, end: datetime) -> None:
    """Orchestrate a full backtest from start to end (inclusive)."""
    trading_days = _get_trading_days(start, end)
    if not trading_days:
        logger.warning("No trading days in range %s – %s — nothing to backtest.", start.date(), end.date())
        return

    tickers = _load_tickers()
    if not tickers:
        logger.warning("No tickers loaded — aborting backtest.")
        return

    fetch_start = trading_days[0] - timedelta(days=5)
    fetch_end = trading_days[-1] + timedelta(days=1)

    logger.info(
        "Fetching 5-min bars %s → %s for %d tickers…",
        fetch_start.date(), fetch_end.date(), len(tickers),
    )
    all_bars = _data.fetch_5min_bars(tickers, fetch_start, fetch_end)
    logger.info("  Bars received for %d symbols.", len(all_bars))

    logger.info("Fetching daily bars for watch-list selection…")
    daily_bars = _data.fetch_daily_bars_range(tickers, fetch_start, fetch_end)

    logger.info("Fetching VIX regime for each trading day…")
    regimes = _data.fetch_vix_regimes(trading_days)

    params = _load_backtest_params()
    state = _sim.SimState()

    for day in trading_days:
        date_str = day.strftime("%Y-%m-%d")
        watch = _data.select_watch_list_for_day(daily_bars, day)
        watch = [s for s in watch if s in all_bars]
        if not watch:
            logger.warning("  %s: no watch-list symbols with bars — skipping.", date_str)
            continue
        regime = regimes.get(date_str, "NORMAL")
        _sim.simulate_day(day, watch, all_bars, state, regime=regime, params=params)

    _report.print_report(state)
