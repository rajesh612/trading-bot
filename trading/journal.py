"""
Thread-safe state I/O for all shared JSON files.
All read-modify-write operations hold the corresponding lock.
"""
import logging
import threading
from datetime import datetime, timedelta
import config
from utils.io_utils import load_json, save_json
from utils.constants import ET

logger = logging.getLogger(__name__)

_POSITIONS_LOCK     = threading.Lock()   # open_positions.json
_DAILY_CLOSED_LOCK  = threading.Lock()   # daily_closed.json
_JOURNAL_LOCK       = threading.Lock()   # trade_journal.json
BUDGET_LOCK         = threading.Lock()   # api_budget.json


# ── Open positions ────────────────────────────────────────────────────────────

def load_open_positions() -> dict:
    return load_json(config.OPEN_POSITIONS_FILE, {})


def save_open_positions(positions: dict) -> None:
    save_json(config.OPEN_POSITIONS_FILE, positions)


def flush_profit_lock_updates(updates: dict[str, dict]) -> None:
    """Merge-write profit-lock changes without wiping concurrent BUYs."""
    if not updates:
        return
    with _POSITIONS_LOCK:
        disk = load_json(config.OPEN_POSITIONS_FILE, {})
        for sym, pos in updates.items():
            if sym in disk:
                disk[sym].update(pos)  # field-level merge — preserves concurrent BUY updates
            else:
                logger.warning(
                    "flush_profit_lock_updates: %s not found on disk — likely closed concurrently; "
                    "profit-lock update discarded.", sym
                )
        save_json(config.OPEN_POSITIONS_FILE, disk)


# ── Daily closed ──────────────────────────────────────────────────────────────

def load_daily_closed() -> set[str]:
    return set(load_json(config.DAILY_CLOSED_FILE, []))


def record_daily_closed(symbol: str) -> None:
    with _DAILY_CLOSED_LOCK:
        closed = set(load_json(config.DAILY_CLOSED_FILE, []))
        closed.add(symbol)
        save_json(config.DAILY_CLOSED_FILE, list(closed))


def reset_daily_closed() -> None:
    with _DAILY_CLOSED_LOCK:
        save_json(config.DAILY_CLOSED_FILE, [])
    logger.info("Daily closed list reset for new trading day.")


# ── Trade journal ─────────────────────────────────────────────────────────────

def append_trade(entry: dict) -> None:
    with _JOURNAL_LOCK:
        journal = load_json(config.TRADE_JOURNAL_FILE, [])
        journal.append(entry)
        save_json(config.TRADE_JOURNAL_FILE, journal)


def load_journal() -> list:
    return load_json(config.TRADE_JOURNAL_FILE, [])


def archive_old_journal_entries() -> None:
    """Move entries older than TRADE_JOURNAL_MAX_DAYS to the archive file."""
    cutoff = datetime.now(ET) - timedelta(days=config.TRADE_JOURNAL_MAX_DAYS)
    with _JOURNAL_LOCK:
        journal = load_json(config.TRADE_JOURNAL_FILE, [])
        recent = [e for e in journal if _entry_timestamp(e) >= cutoff]
        old    = [e for e in journal if _entry_timestamp(e) < cutoff]
        if not old:
            return
        archive_path = config.BASE_DIR / "trade_journal_archive.json"
        archived = load_json(archive_path, [])
        save_json(archive_path, archived + old)
        save_json(config.TRADE_JOURNAL_FILE, recent)
    logger.info("Journal hygiene: archived %d entries → trade_journal_archive.json", len(old))


def _entry_timestamp(entry: dict) -> datetime:
    try:
        ts = entry.get("timestamp", "")
        dt = datetime.fromisoformat(ts)
        return dt if dt.tzinfo else dt.replace(tzinfo=ET)
    except (ValueError, TypeError):
        return datetime.min.replace(tzinfo=ET)


def get_recent_force_close_pnls(n: int, date_str: str | None = None) -> list[float]:
    """Return pnl_dollar values for the last n FORCE_CLOSE entries today (oldest first).

    Returns fewer than n items if today has fewer qualifying closes.
    Returns [] if journal is empty or unreadable.
    """
    from datetime import datetime
    target_date = date_str or datetime.now(ET).strftime("%Y-%m-%d")
    try:
        journal = load_journal()
    except Exception:
        return []
    today_fc = [
        e for e in journal
        if e.get("type") == "CLOSE"
        and e.get("close_reason", "").upper() == "FORCE_CLOSE"
        and (e.get("timestamp") or "").startswith(target_date)
    ]
    return [e["pnl_dollar"] for e in today_fc[-n:] if "pnl_dollar" in e]


def count_todays_losses() -> int:
    """Count CLOSE entries in today's journal with negative pnl_pct."""
    today_str = datetime.now(ET).date().isoformat()
    journal = load_journal()
    return sum(
        1 for e in journal
        if e.get("type") == "CLOSE"
        and str(e.get("timestamp", "")).startswith(today_str)
        and float(e.get("pnl_pct", 0)) < 0
    )
