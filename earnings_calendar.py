"""
Earnings calendar: daily refresh at 8:00 AM ET.
Provides next-earnings-date lookups and PEAD (post-earnings drift) detection.
"""
import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

import config

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

_YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0"}


def _yahoo_quote_summary(symbol: str, modules: str) -> dict:
    url = (
        f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
        f"?modules={modules}"
    )
    resp = requests.get(url, headers=_YAHOO_HEADERS, timeout=10)
    resp.raise_for_status()
    result = resp.json().get("quoteSummary", {}).get("result", [])
    return result[0] if result else {}


def _fetch_next_earnings_date(symbol: str) -> str | None:
    """Return ISO date string of next earnings, or None."""
    try:
        data = _yahoo_quote_summary(symbol, "calendarEvents")
        earnings = data.get("calendarEvents", {}).get("earnings", {})
        dates = earnings.get("earningsDate", [])
        if dates:
            ts = dates[0].get("raw")
            if ts:
                return datetime.fromtimestamp(ts, tz=ET).strftime("%Y-%m-%d")
    except Exception as exc:
        logger.debug(f"earnings date fetch failed for {symbol}: {exc}")
    return None


def _fetch_earnings_history(symbol: str) -> dict | None:
    """Return the most-recent quarterly earnings result, or None."""
    try:
        data = _yahoo_quote_summary(symbol, "earningsHistory")
        history = data.get("earningsHistory", {}).get("history", [])
        if not history:
            return None
        latest = history[0]

        eps_actual   = latest.get("epsActual",   {}).get("raw")
        eps_estimate = latest.get("epsEstimate", {}).get("raw")
        if eps_actual is None or eps_estimate is None or eps_estimate == 0:
            eps_surprise_pct = None
        else:
            eps_surprise_pct = (eps_actual - eps_estimate) / abs(eps_estimate) * 100

        # Revenue beat (Yahoo doesn't always provide this; treat missing as unknown)
        revenue_actual   = latest.get("revenueActual",   {}).get("raw")
        revenue_estimate = latest.get("revenueEstimate", {}).get("raw")
        if revenue_actual and revenue_estimate and revenue_estimate > 0:
            revenue_beat = revenue_actual >= revenue_estimate
        else:
            revenue_beat = None  # unknown

        # Report date
        period_raw = latest.get("period", {}).get("raw")
        report_date = (
            datetime.fromtimestamp(period_raw, tz=ET).strftime("%Y-%m-%d")
            if period_raw else None
        )

        return {
            "report_date":       report_date,
            "eps_actual":        eps_actual,
            "eps_estimate":      eps_estimate,
            "eps_surprise_pct":  eps_surprise_pct,
            "revenue_beat":      revenue_beat,
        }
    except Exception as exc:
        logger.debug(f"earnings history fetch failed for {symbol}: {exc}")
    return None


def _fetch_earnings_day_reaction(symbol: str, report_date: str) -> dict:
    """Fetch price/volume data for the earnings day itself."""
    result = {
        "earnings_day_return_pct":    None,
        "earnings_day_volume_ratio":  None,
        "earnings_day_close_position": None,
        "guidance_change":             "unknown",
    }
    try:
        from market_data import get_daily_bars
        df = get_daily_bars(symbol, days=30)
        if df.empty:
            return result
        df_str = df.copy()
        df_str.index = df_str.index.strftime("%Y-%m-%d")
        if report_date not in df_str.index:
            return result
        row = df_str.loc[report_date]
        # Approximate prev close
        idx = list(df_str.index).index(report_date)
        if idx > 0:
            prev_close = float(df_str.iloc[idx - 1]["close"])
        else:
            prev_close = float(row["open"])

        ret = (float(row["close"]) - prev_close) / prev_close * 100
        # Close position: (close - low) / (high - low)
        rng = float(row["high"]) - float(row["low"])
        close_pos = (float(row["close"]) - float(row["low"])) / rng if rng > 0 else 0.5

        # Volume ratio vs 20-day mean before earnings
        vol_before = df["volume"].iloc[max(0, idx - 20):idx].mean()
        vol_ratio = float(row["volume"]) / vol_before if vol_before > 0 else 1.0

        result.update({
            "earnings_day_return_pct":   round(ret, 2),
            "earnings_day_volume_ratio": round(vol_ratio, 2),
            "earnings_day_close_position": round(close_pos, 2),
        })
    except Exception as exc:
        logger.debug(f"earnings day reaction fetch failed for {symbol}: {exc}")
    return result


def refresh_earnings_calendar(symbols: list[str]) -> None:
    """Build earnings_calendar.json keyed by symbol. Called at 8:00 AM daily."""
    today = datetime.now(ET).strftime("%Y-%m-%d")
    cache: dict = {}
    cutoff = datetime.now(ET) - timedelta(days=5)

    for symbol in symbols:
        try:
            next_date = _fetch_next_earnings_date(symbol)
            entry: dict = {"next_earnings_date": next_date}

            # Add recently_reported block if earnings happened within 5 trading days
            history = _fetch_earnings_history(symbol)
            if history and history.get("report_date"):
                report_dt = datetime.fromisoformat(history["report_date"])
                if report_dt >= cutoff:
                    reaction = _fetch_earnings_day_reaction(symbol, history["report_date"])
                    entry["recently_reported"] = {**history, **reaction}

            cache[symbol] = entry
        except Exception as exc:
            logger.debug(f"earnings refresh skipped {symbol}: {exc}")
            cache[symbol] = {"next_earnings_date": None}

    payload = {"date": today, "symbols": cache}
    with open(config.EARNINGS_CALENDAR_FILE, "w") as f:
        json.dump(payload, f, indent=2)
    logger.info(f"Earnings calendar refreshed for {len(symbols)} symbols.")


def _load_cache() -> dict:
    """Load today's earnings cache; return {} on error or stale file."""
    try:
        if not config.EARNINGS_CALENDAR_FILE.exists():
            return {}
        with open(config.EARNINGS_CALENDAR_FILE) as f:
            data = json.load(f)
        today = datetime.now(ET).strftime("%Y-%m-%d")
        if data.get("date") != today:
            return {}
        return data.get("symbols", {})
    except Exception:
        return {}


def days_until_earnings(symbol: str) -> int | None:
    """Return calendar days until next earnings, or None if unknown."""
    cache = _load_cache()
    entry = cache.get(symbol.upper(), {})
    next_date = entry.get("next_earnings_date")
    if not next_date:
        return None
    try:
        from datetime import date as _date
        delta = _date.fromisoformat(next_date) - datetime.now(ET).date()
        return max(0, delta.days)
    except Exception:
        return None


def is_earnings_imminent(symbol: str, days: int = 1) -> bool:
    """Return True if earnings are within `days` calendar days. False on any error."""
    try:
        d = days_until_earnings(symbol)
        if d is None:
            return False
        return d <= days
    except Exception:
        return False


def recently_reported(symbol: str, days: int = 5) -> dict | None:
    """
    Return the recently_reported block if earnings happened within `days` trading days.
    Returns None if not recently reported or data unavailable.
    """
    cache = _load_cache()
    entry = cache.get(symbol.upper(), {})
    block = entry.get("recently_reported")
    if not block:
        return None
    report_date = block.get("report_date")
    if not report_date:
        return None
    try:
        cutoff_date = (datetime.now(ET) - timedelta(days=days)).date()
        if datetime.fromisoformat(report_date).date() >= cutoff_date:
            return block
    except Exception:
        pass
    return None


def _fetch_forward_eps_revision(symbol: str) -> float | None:
    """30-day revision of next-quarter EPS consensus from Yahoo earningsTrend."""
    try:
        data = _yahoo_quote_summary(symbol, "earningsTrend")
        trend = data.get("earningsTrend", {}).get("trend", [])
        current_q = next((t for t in trend if t.get("period") == "0q"), None)
        if not current_q:
            return None
        eps_trend = current_q.get("epsTrend", {})
        current   = eps_trend.get("current",   {}).get("raw")
        ago_30d   = eps_trend.get("30daysAgo", {}).get("raw")
        if current is None or ago_30d is None or ago_30d == 0:
            return None
        return (current - ago_30d) / abs(ago_30d) * 100
    except Exception:
        return None


def detect_pead_setup(symbol: str) -> tuple[bool, int]:
    """
    PEAD (post-earnings announcement drift) qualification.
    Score threshold: 80 raw points.
    Max possible: 130 (blowout EPS=30 + 5 binary conditions × 20 each).
    Returns (qualifies, score).
    """
    block = recently_reported(symbol, days=5)
    if not block:
        return False, 0

    score = 0

    # Condition 1: EPS beat — graded by magnitude
    eps_surp = block.get("eps_surprise_pct")
    if eps_surp is not None:
        if eps_surp >= config.PEAD_EPS_BEAT_STRONG:
            score += 30   # blowout ≥ 20%
        elif eps_surp >= config.PEAD_EPS_BEAT_SOLID:
            score += 25   # strong ≥ 10%
        elif eps_surp >= config.PEAD_EPS_SURPRISE_MIN:
            score += 20   # solid ≥ 3%
        elif eps_surp >= 1.0:
            score += 10   # marginal beat

    # Condition 2: Revenue beat
    if block.get("revenue_beat") is True:
        score += 20

    # Condition 3: Guidance not lowered ("unknown" is OK)
    guidance = block.get("guidance_change", "unknown")
    if guidance not in ("lowered",):
        score += 20

    # Condition 4: Earnings-day return ≥ PEAD_DAY_RETURN_MIN AND closed in upper 30% of range
    er = block.get("earnings_day_return_pct")
    cp = block.get("earnings_day_close_position")
    if er is not None and cp is not None:
        if er >= config.PEAD_DAY_RETURN_MIN and cp >= config.PEAD_CLOSE_POSITION_MIN:
            score += 20

    # Condition 5: Volume ratio ≥ PEAD_VOLUME_RATIO_MIN
    vr = block.get("earnings_day_volume_ratio")
    if vr is not None and vr >= config.PEAD_VOLUME_RATIO_MIN:
        score += 20

    # Condition 6: Forward EPS estimates revised upward post-earnings
    fwd_rev = _fetch_forward_eps_revision(symbol)
    if fwd_rev is not None and fwd_rev >= config.PEAD_FWD_REVISION_MIN:
        score += 20

    return score >= config.PEAD_SCORE_THRESHOLD, score
