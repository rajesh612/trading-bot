import json
import logging
import sys
from datetime import datetime, timedelta
import config
import telegram_notify
from utils.io_utils import load_json, save_json
from utils.constants import ET

logger = logging.getLogger("main")


def exit_with_alert(msg: str, code: int = 1) -> None:
    """Exit with a Telegram alert. Logs error, sends Telegram, then exits."""
    logger.error(msg)
    try:
        telegram_notify._send(f"🚨 Bot failed to start: {msg}")
    except Exception:
        pass
    sys.exit(code)


def validate_state_files() -> None:
    """Check JSON state files on startup; reset corrupt ones and alert via Telegram."""
    candidates = [
        (config.TRADE_JOURNAL_FILE,      []),
        (config.OPEN_POSITIONS_FILE,     {}),
        (config.SWING_POSITIONS_FILE,    {}),
        (config.INDICATOR_WEIGHTS_FILE,  {}),
    ]
    for path, default in candidates:
        if not path.exists():
            continue
        try:
            json.loads(path.read_text())
        except (json.JSONDecodeError, ValueError):
            backup = path.with_suffix(".corrupt.json")
            path.rename(backup)
            save_json(path, default)
            logger.error(f"CORRUPT state file reset: {path.name} → backup {backup.name}")
            telegram_notify.notify_file_corruption(path.name)


def cleanup_stale_files() -> None:
    """
    Housekeeping on startup:
    - Delete .corrupt.json backups older than CORRUPT_BACKUP_MAX_DAYS
    - Archive trade_journal.json entries older than TRADE_JOURNAL_MAX_DAYS
    - Delete leftover .tmp files from crashed writes
    - Delete stale research_report.json from a prior trading day
    """
    cutoff = datetime.now(ET) - timedelta(days=config.CORRUPT_BACKUP_MAX_DAYS)
    for f in config.BASE_DIR.glob("*.corrupt.json"):
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=ET)
            if mtime < cutoff:
                f.unlink()
                logger.info(f"Deleted stale backup: {f.name}")
        except Exception as exc:
            logger.warning(f"Could not delete {f.name}: {exc}")

    for f in config.BASE_DIR.glob("*.tmp"):
        try:
            f.unlink()
            logger.info(f"Deleted leftover tmp file: {f.name}")
        except Exception as exc:
            logger.warning(f"Could not delete {f.name}: {exc}")

    # Prune research_report.json if it is from a prior trading day.
    try:
        if config.RESEARCH_REPORT_FILE.exists():
            rr = load_json(config.RESEARCH_REPORT_FILE, {})
            meta_ts = rr.get("__meta__", {}).get("timestamp", "")
            if meta_ts:
                report_dt = datetime.fromisoformat(meta_ts)
                if report_dt.tzinfo is None:
                    report_dt = report_dt.replace(tzinfo=ET)
                today_start = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0)
                if report_dt < today_start:
                    config.RESEARCH_REPORT_FILE.unlink()
                    logger.info("Deleted stale research_report.json (from prior trading day)")
    except Exception as exc:
        logger.debug(f"research_report.json prune skipped: {exc}")

    try:
        from trading.journal import archive_old_journal_entries
        archive_old_journal_entries()
    except Exception as exc:
        logger.warning(f"Journal archival failed: {exc}")
