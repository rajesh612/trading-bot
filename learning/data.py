"""
Persistence helpers for the learning agent — JSON file reads and writes only.
"""
from datetime import datetime, timedelta

import config
from learning.trade_analysis import _TRADE_SUMMARY_KEYS
from utils.constants import ET
from utils.io_utils import load_json, save_json


def _load_todays_closed_trades() -> list[dict]:
    if not config.TRADE_JOURNAL_FILE.exists():
        return []
    journal = load_json(config.TRADE_JOURNAL_FILE, [])
    today = datetime.now(ET).date().isoformat()
    # Strip full indicator snapshots — learning agent only needs summary fields
    return [
        {k: v for k, v in t.items() if k in _TRADE_SUMMARY_KEYS}
        for t in journal
        if t.get("type") == "CLOSE" and t.get("timestamp", "").startswith(today)
    ]


def _load_rolling_trades(days: int | None = None) -> list[dict]:
    """Return CLOSE trades from the last N days (default: LEARNING_ROLLING_DAYS).

    Excludes BUY records and trades older than the rolling window.
    """
    n = days if days is not None else config.LEARNING_ROLLING_DAYS
    cutoff = datetime.now(ET) - timedelta(days=n)
    trades = load_json(config.TRADE_JOURNAL_FILE, [])
    result = []
    for t in trades:
        if t.get("type") != "CLOSE":
            continue
        ts_str = t.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=ET)
            if ts >= cutoff:
                result.append({k: v for k, v in t.items() if k in _TRADE_SUMMARY_KEYS})
        except Exception:
            pass
    return result


def _load_weights() -> dict:
    return load_json(config.INDICATOR_WEIGHTS_FILE, {})


def _save_weights(weights: dict):
    save_json(config.INDICATOR_WEIGHTS_FILE, weights)


def _save_trading_params(params: dict):
    params["last_updated"] = datetime.now(ET).date().isoformat()
    save_json(config.TRADING_PARAMS_FILE, params)
