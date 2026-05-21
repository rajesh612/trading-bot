import config
from utils.io_utils import load_json


def load_watch_list() -> list[str]:
    """Return the symbol set for the trading cycle.

    Watch list stocks come first; open and swing position tickers are appended
    (deduplicated) so held positions are always evaluated regardless of watch
    list state.
    """
    if config.WATCH_LIST_FILE.exists():
        watch = [
            t.strip().upper()
            for t in config.WATCH_LIST_FILE.read_text().splitlines()
            if t.strip()
        ]
    else:
        watch = []

    held = _load_held_tickers()
    seen = set(watch)
    for ticker in held:
        if ticker not in seen:
            watch.append(ticker)
            seen.add(ticker)
    return watch


def _load_held_tickers() -> list[str]:
    open_pos  = load_json(config.OPEN_POSITIONS_FILE,  {}) or {}
    swing_pos = load_json(config.SWING_POSITIONS_FILE, {}) or {}
    return list({*open_pos.keys(), *swing_pos.keys()})


def get_watch_list_health() -> dict:
    """Return watch_list_health.json content, empty dict if missing."""
    return load_json(config.DATA_WATCH_LIST_HEALTH_FILE, {}) or {}
