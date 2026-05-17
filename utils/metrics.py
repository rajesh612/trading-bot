"""
Lightweight per-cycle metrics JSONL export.
Records one line per cycle to METRICS_FILE for post-hoc trend analysis.
Pure appends — single scheduler thread writes; reads are rare.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import config
from utils.constants import ET

logger = logging.getLogger(__name__)


def record_cycle_metric(
    regime: str,
    candidates: int,
    entries: int,
    closes: int,
    api_budget_pct: float,
    cycle_ms: float,
) -> None:
    """Append one metric record to METRICS_FILE. Never raises."""
    try:
        record = json.dumps({
            "ts": datetime.now(ET).isoformat(),
            "date": datetime.now(ET).strftime("%Y-%m-%d"),
            "regime": regime,
            "candidates": candidates,
            "entries": entries,
            "closes": closes,
            "api_budget_pct": round(api_budget_pct, 3),
            "cycle_ms": round(cycle_ms, 0),
        })
        with open(config.METRICS_FILE, "a") as f:
            f.write(record + "\n")
    except Exception as exc:
        logger.debug("metrics write failed: %s", exc)


def get_daily_summary(date_str: str | None = None) -> dict:
    """Aggregate today's cycle metrics. Returns empty dict if no records."""
    target_date = date_str or datetime.now(ET).strftime("%Y-%m-%d")
    path = Path(str(config.METRICS_FILE))
    if not path.exists():
        return {}
    records = []
    try:
        for line in path.read_text().splitlines():
            try:
                r = json.loads(line)
                if r.get("date") == target_date:
                    records.append(r)
            except Exception:
                pass
    except Exception:
        return {}
    if not records:
        return {}
    total_entries = sum(r.get("entries", 0) for r in records)
    total_closes  = sum(r.get("closes", 0) for r in records)
    avg_candidates = sum(r.get("candidates", 0) for r in records) / len(records)
    regimes = [r.get("regime") for r in records]
    return {
        "total_cycles": len(records),
        "total_entries": total_entries,
        "total_closes": total_closes,
        "avg_candidates": round(avg_candidates, 1),
        "dominant_regime": max(set(regimes), key=regimes.count),
        "regime_flips": len(set(regimes)) - 1,
        "api_budget_pct_final": records[-1].get("api_budget_pct", 0) if records else 0,
    }
