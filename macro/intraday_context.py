"""
Per-cycle intraday market context fetched once every INTRADAY_CONTEXT_TTL_SEC.
Provides SPY/QQQ bias and sector performance for the Claude context block.
"""
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
import config
from utils.constants import ET

logger = logging.getLogger(__name__)

_SECTOR_ETFS = ["XLF", "XLK", "XLE", "XLV", "XLI"]
_CACHE_LOCK = threading.Lock()
_cache: "IntradayContext | None" = None
_cache_time: float = 0.0


@dataclass
class IntradayContext:
    spy_today_pct:     float
    spy_vwap_position: str           # "above" | "below"
    spy_vwap_slope:    float
    qqq_today_pct:     float
    opening_gap_pct:   float
    post_event_day:    bool
    sector_today:      dict          # {"XLF": +0.3, "XLK": -0.8, ...}
    bias:              str           # "BULLISH" | "NEUTRAL" | "BEARISH"

    def to_context_lines(self) -> str:
        sector_str = " ".join(f"{k}={v:+.1f}%" for k, v in self.sector_today.items())
        post = " ⚠ POST-EVENT DAY — elevated volatility, gap-fill tendency" if self.post_event_day else ""
        return (
            f"Intraday bias: {self.bias} | SPY={self.spy_today_pct:+.1f}% "
            f"{'above' if self.spy_vwap_position == 'above' else 'below'} VWAP "
            f"(slope={self.spy_vwap_slope:+.2f}%) | QQQ={self.qqq_today_pct:+.1f}%"
            f" gap_open={self.opening_gap_pct:+.1f}%{post}\n"
            f"Sectors today: {sector_str}"
        )


def get_intraday_context(now: datetime) -> "IntradayContext | None":
    """Fetch SPY/QQQ/sector intraday bars. Result cached for INTRADAY_CONTEXT_TTL_SEC."""
    global _cache, _cache_time
    with _CACHE_LOCK:
        if _cache is not None and (time.monotonic() - _cache_time) < config.INTRADAY_CONTEXT_TTL_SEC:
            return _cache
        ctx = _fetch_intraday_context(now)
        if ctx is not None:
            _cache = ctx
            _cache_time = time.monotonic()
        return ctx


def _fetch_intraday_context(now: datetime) -> "IntradayContext | None":
    try:
        from data.market_data import get_intraday_bars, get_daily_bars
        from macro.economic_calendar import is_post_major_event_day

        spy_bars = get_intraday_bars("SPY", lookback=80)
        qqq_bars = get_intraday_bars("QQQ", lookback=80)
        if spy_bars is None or len(spy_bars) < 10:
            return None

        spy_prev_close = _prev_close_from_daily("SPY")
        qqq_prev_close = _prev_close_from_daily("QQQ")
        spy_open       = float(spy_bars["open"].iloc[0])
        spy_current    = float(spy_bars["close"].iloc[-1])

        spy_today_pct  = (spy_current - spy_prev_close) / spy_prev_close * 100 if spy_prev_close else 0.0
        qqq_today_pct  = 0.0
        if qqq_bars is not None and len(qqq_bars) >= 2 and qqq_prev_close:
            qqq_today_pct = (float(qqq_bars["close"].iloc[-1]) - qqq_prev_close) / qqq_prev_close * 100

        opening_gap_pct = (spy_open - spy_prev_close) / spy_prev_close * 100 if spy_prev_close else 0.0

        # VWAP position and slope from intraday bars
        from indicators.math_utils import _compute_vwap
        spy_vwap_series = _compute_vwap(spy_bars)
        spy_vwap = float(spy_vwap_series.iloc[-1]) if spy_vwap_series is not None else spy_current
        spy_vwap_position = "above" if spy_current >= spy_vwap else "below"
        spy_vwap_slope = 0.0
        if spy_vwap_series is not None and len(spy_vwap_series) >= 10:
            spy_vwap_slope = (float(spy_vwap_series.iloc[-1]) - float(spy_vwap_series.iloc[-10])) / float(spy_vwap_series.iloc[-10]) * 100

        # Sector performance
        sector_today: dict[str, float] = {}
        for etf in _SECTOR_ETFS:
            try:
                bars = get_intraday_bars(etf, lookback=20)
                if bars is not None and len(bars) >= 2:
                    prev = _prev_close_from_daily(etf)
                    if prev:
                        sector_today[etf] = round((float(bars["close"].iloc[-1]) - prev) / prev * 100, 1)
            except Exception:
                pass

        # Post-event-day detection
        post_event, _ = is_post_major_event_day(now)

        # Compute bias
        if spy_today_pct < -0.3 and spy_vwap_position == "below":
            bias = "BEARISH"
        elif spy_today_pct > 0.3 and spy_vwap_position == "above":
            bias = "BULLISH"
        else:
            bias = "NEUTRAL"

        return IntradayContext(
            spy_today_pct=round(spy_today_pct, 2),
            spy_vwap_position=spy_vwap_position,
            spy_vwap_slope=round(spy_vwap_slope, 3),
            qqq_today_pct=round(qqq_today_pct, 2),
            opening_gap_pct=round(opening_gap_pct, 2),
            post_event_day=post_event,
            sector_today=sector_today,
            bias=bias,
        )
    except Exception as exc:
        logger.warning("Intraday context fetch failed: %s", exc)
        return None


def _prev_close_from_daily(symbol: str) -> float:
    """Get yesterday's closing price from daily bars."""
    try:
        from data.market_data import get_daily_bars
        daily = get_daily_bars(symbol, days=3)
        if daily is not None and len(daily) >= 2:
            return float(daily["close"].iloc[-2])
    except Exception:
        pass
    return 0.0
