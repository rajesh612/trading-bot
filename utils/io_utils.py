from __future__ import annotations
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def load_json(path: Path, default: Any) -> Any:
    """Atomic read with corrupt-file recovery. Returns default if file missing."""
    path = Path(path)
    if not path.exists():
        return default
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError) as exc:
        backup = path.with_suffix(".corrupt.json")
        logger.warning("Corrupt JSON at %s — backing up to %s: %s", path, backup, exc)
        shutil.copy2(path, backup)
        return default


def save_json(path: Path, data: Any) -> None:
    """Validate JSON serialization before touching disk — raises on bad data."""
    path = Path(path)
    serialized = json.dumps(data, indent=2)  # raises TypeError on bad data
    tmp = path.with_suffix(".tmp")
    tmp.write_text(serialized)
    os.replace(tmp, path)


def mark_stale(path: Path, source_id: str, fallback_from: str | None = None) -> None:
    """Inject _stale metadata into an existing JSON state file atomically."""
    from datetime import datetime
    from utils.constants import ET
    data = load_json(path, {}) or {}
    data["_stale"]         = True
    data["_stale_since"]   = datetime.now(ET).isoformat()
    data["_fallback_from"] = fallback_from
    save_json(path, data)


def clear_stale(path: Path) -> None:
    """Remove _stale metadata after a successful fresh fetch."""
    data = load_json(path, {}) or {}
    for key in ("_stale", "_stale_since", "_fallback_from"):
        data.pop(key, None)
    save_json(path, data)
