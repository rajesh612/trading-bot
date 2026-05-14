import config


def load_watch_list() -> list[str]:
    """
    Load watch list symbols from config.WATCH_LIST_FILE.

    Returns:
        List of uppercase symbol strings, stripped of whitespace.
        Returns empty list if file does not exist.
    """
    if config.WATCH_LIST_FILE.exists():
        return [t.strip().upper() for t in config.WATCH_LIST_FILE.read_text().splitlines() if t.strip()]
    return []
