"""
Economic calendar: daily refresh at 8:30 AM ET.
Gates new entries in the 45-minute window before/after high-impact events.
"""
import json
import logging
from calendar import monthcalendar
from datetime import datetime, timedelta, time as dt_time
import requests

import config
from utils.constants import ET

logger = logging.getLogger(__name__)

_RISK_WINDOW_MINUTES = 45
_FOMC_2026_DATES = [
    "2026-01-29", "2026-03-19", "2026-05-07", "2026-06-18",
    "2026-07-30", "2026-09-17", "2026-10-29", "2026-12-10",
]


# ── Hardcoded recurring events ────────────────────────────────────────────────

def _first_friday(year: int, month: int) -> int:
    for week in monthcalendar(year, month):
        if week[4] != 0:
            return week[4]
    return 7


def _hardcoded_events(year: int, month: int) -> list[dict]:
    events = []

    # NFP — first Friday 8:30 AM ET
    nfp_day = _first_friday(year, month)
    events.append({
        "date": f"{year}-{month:02d}-{nfp_day:02d}",
        "time": "08:30",
        "name": "Non-Farm Payrolls",
        "impact": "high",
    })

    # CPI — approximately 12th of each month 8:30 AM ET
    events.append({
        "date": f"{year}-{month:02d}-12",
        "time": "08:30",
        "name": "CPI Release",
        "impact": "high",
    })

    # FOMC rate decisions
    for date_str in _FOMC_2026_DATES:
        if date_str.startswith(f"{year}-{month:02d}"):
            events.append({
                "date": date_str,
                "time": "14:00",
                "name": "FOMC Rate Decision",
                "impact": "high",
            })

    return events


# ── ForexFactory scrape ───────────────────────────────────────────────────────

def _scrape_forexfactory(date: datetime) -> list[dict]:
    """Best-effort scrape of ForexFactory calendar for today. Returns [] on any failure."""
    try:
        from bs4 import BeautifulSoup
        date_str = date.strftime("%b%d.%Y").lower()
        url = f"https://www.forexfactory.com/calendar?day={date_str}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select("tr.calendar__row")
        events = []
        for row in rows:
            impact_el = row.select_one(".calendar__impact")
            if not impact_el:
                continue
            impact_classes = impact_el.get("class", [])
            if not any("high" in c for c in impact_classes):
                continue
            time_el  = row.select_one(".calendar__time")
            title_el = row.select_one(".calendar__event-title")
            if not title_el:
                continue
            event_time = time_el.get_text(strip=True) if time_el else ""
            events.append({
                "date":   date.strftime("%Y-%m-%d"),
                "time":   event_time,
                "name":   title_el.get_text(strip=True),
                "impact": "high",
            })
        return events
    except Exception as exc:
        logger.debug(f"ForexFactory scrape failed: {exc}")
        return []


# ── Refresh / load ────────────────────────────────────────────────────────────

def refresh_economic_calendar() -> None:
    """Fetch today's high-impact events and write economic_calendar.json."""
    now = datetime.now(ET)
    today_str = now.strftime("%Y-%m-%d")

    # Try scrape first; fall back to hardcoded
    events = _scrape_forexfactory(now)
    source = "forexfactory"
    if not events:
        events = _hardcoded_events(now.year, now.month)
        source = "hardcoded"

    # Filter to today's events only
    todays = [e for e in events if e.get("date") == today_str]

    # Carry forward previous events for post-event-day detection
    try:
        from utils.io_utils import load_json
        prev_cache = load_json(config.ECONOMIC_CALENDAR_FILE, {})
        prior_events = prev_cache.get("events", [])
    except Exception:
        prior_events = []

    payload = {
        "date":         today_str,
        "events":       todays,
        "prior_events": prior_events,
        "source":       source,
    }
    with open(config.ECONOMIC_CALENDAR_FILE, "w") as f:
        json.dump(payload, f, indent=2)

    logger.info(f"Economic calendar: {len(todays)} high-impact events today (source={source})")


def is_post_major_event_day(now: datetime) -> tuple[bool, str]:
    """Returns (True, event_name) if yesterday had a FOMC/NFP/CPI/PCE/JOLTS event."""
    try:
        with open(config.ECONOMIC_CALENDAR_FILE) as f:
            data = json.load(f)
        prior = data.get("prior_events", [])
    except Exception:
        return False, ""

    high_impact = ["FOMC", "NFP", "CPI", "PCE", "JOLTS"]
    for evt in prior:
        name = evt.get("name", "").upper()
        if any(tag in name for tag in high_impact):
            return True, evt.get("name", "")
    return False, ""


def _load_todays_events() -> list[dict]:
    """Load today's events; return [] on any error or if file is stale."""
    try:
        if not config.ECONOMIC_CALENDAR_FILE.exists():
            return []
        with open(config.ECONOMIC_CALENDAR_FILE) as f:
            data = json.load(f)
        today_str = datetime.now(ET).strftime("%Y-%m-%d")
        if data.get("date") != today_str:
            return []
        return data.get("events", [])
    except Exception:
        return []


def get_todays_events() -> list[dict]:
    """Return today's high-impact events (empty list if calendar unavailable)."""
    return _load_todays_events()


def is_event_risk_window(now: datetime) -> tuple[bool, str]:
    """
    Return (True, event_name) if within RISK_WINDOW_MINUTES of a high-impact event.
    Returns (False, '') otherwise.
    """
    events = _load_todays_events()
    if not events:
        return False, ""

    for event in events:
        time_str = event.get("time", "")
        if not time_str:
            continue
        try:
            # Parse event time (HH:MM or am/pm format)
            time_str_clean = time_str.strip().upper()
            for fmt in ("%I:%M%p", "%H:%M"):
                try:
                    parsed = datetime.strptime(time_str_clean, fmt)
                    event_dt = now.replace(
                        hour=parsed.hour,
                        minute=parsed.minute,
                        second=0,
                        microsecond=0,
                    )
                    delta = abs((now - event_dt).total_seconds()) / 60
                    if delta <= _RISK_WINDOW_MINUTES:
                        return True, event.get("name", "Unknown event")
                    break
                except ValueError:
                    continue
        except Exception:
            continue

    return False, ""


if __name__ == "__main__":
    import logging as _l
    _l.basicConfig(level=_l.INFO)
    refresh_economic_calendar()
    print(get_todays_events())
    print(is_event_risk_window(datetime.now(ET)))
