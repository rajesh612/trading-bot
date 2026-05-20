"""Persistent stop-loss blacklist — blocks re-entry for SL_BLACKLIST_DAYS after SL hit."""
import logging
from datetime import date, timedelta

import config
from utils.io_utils import load_json, save_json

logger = logging.getLogger(__name__)


def record_sl_hit(symbol: str) -> None:
    """Record that symbol hit SL today. Written to SL_BLACKLIST_FILE."""
    if not config.SL_BLACKLIST_ENABLED:
        return
    data = load_json(config.SL_BLACKLIST_FILE, {})
    data[symbol] = date.today().isoformat()
    save_json(config.SL_BLACKLIST_FILE, data)
    logger.info("SL blacklist: %s added (hit SL today)", symbol)


def is_sl_blacklisted(symbol: str) -> bool:
    """Return True if symbol hit SL within the last SL_BLACKLIST_DAYS days."""
    if not config.SL_BLACKLIST_ENABLED:
        return False
    data = load_json(config.SL_BLACKLIST_FILE, {})
    hit_str = data.get(symbol)
    if not hit_str:
        return False
    try:
        hit_date = date.fromisoformat(hit_str)
    except ValueError:
        return False
    return (date.today() - hit_date).days < config.SL_BLACKLIST_DAYS
