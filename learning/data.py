"""
Persistence helpers for the learning agent — JSON file reads and writes only.
"""
from datetime import date

import config
from learning.trade_analysis import _TRADE_SUMMARY_KEYS
from utils.io_utils import load_json, save_json


def _load_todays_closed_trades() -> list[dict]:
    if not config.TRADE_JOURNAL_FILE.exists():
        return []
    journal = load_json(config.TRADE_JOURNAL_FILE, [])
    today = date.today().isoformat()
    # Strip full indicator snapshots — learning agent only needs summary fields
    return [
        {k: v for k, v in t.items() if k in _TRADE_SUMMARY_KEYS}
        for t in journal
        if t.get("type") == "CLOSE" and t.get("timestamp", "").startswith(today)
    ]


def _load_weights() -> dict:
    return load_json(config.INDICATOR_WEIGHTS_FILE, {})


def _save_weights(weights: dict):
    save_json(config.INDICATOR_WEIGHTS_FILE, weights)


def _save_trading_params(params: dict):
    params["last_updated"] = date.today().isoformat()
    save_json(config.TRADING_PARAMS_FILE, params)
