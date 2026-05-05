"""
VIX regime persistence helpers.
fetch_vix_regime() was superseded by macro_regime.py (9-state classifier).
This module is kept for load_vix_regime() (learning_agent fallback) and
load_trading_params() (shared by trading_agent and learning_agent).
"""

import json
import logging

import config

logger = logging.getLogger(__name__)


def load_vix_regime() -> dict:
    """Return today's cached regime (set at 9 AM). Defaults to NORMAL if missing."""
    if config.VIX_REGIME_FILE.exists():
        with open(config.VIX_REGIME_FILE) as f:
            return json.load(f)
    return {"regime": "NORMAL", "vix_current": 0.0, "vix_avg_90d": 0.0}


def load_trading_params() -> dict:
    """Return learning-agent-adjusted trading params, falling back to config defaults."""
    defaults = {
        "entry_start_hour":        config.ENTRY_START_HOUR,
        "entry_end_hour":          config.ENTRY_END_HOUR,
        "rsi_buy_max":             config.RSI_BUY_MAX,
        "rsi_buy_max_high_vol":    config.RSI_BUY_MAX_HIGH_VOL,
        "vwap_slope_min":          config.VWAP_SLOPE_MIN,
        "high_vol_position_mult":  config.HIGH_VOL_POSITION_MULT,
        "stop_loss_pct":           config.STOP_LOSS_PCT,
        "take_profit_pct":         config.TAKE_PROFIT_PCT,
    }
    if config.TRADING_PARAMS_FILE.exists():
        with open(config.TRADING_PARAMS_FILE) as f:
            saved = json.load(f)
        defaults.update(saved)
    return defaults
