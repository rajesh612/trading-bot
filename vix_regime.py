"""
VIX regime classifier — called once at 9:00 AM ET each trading day.

Regimes:
  NORMAL   — VIX <= 90-day average → full position size, RSI ≤ 45
  HIGH_VOL — VIX > 90-day average  → half position size, RSI ≤ 35
  CRISIS   — VIX > 90-day avg AND VIX rising AND SPY down >1% → skip all new entries
"""

import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

import config

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


def _yahoo_closes(symbol: str, days: int = 120) -> list[float]:
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?interval=1d&range={days}d"
    )
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    resp.raise_for_status()
    quotes = resp.json()["chart"]["result"][0]["indicators"]["quote"][0]
    return [c for c in quotes["close"] if c is not None]


def fetch_vix_regime() -> dict:
    """
    Fetch VIX and SPY, classify today's regime, write vix_regime.json.
    Returns the regime dict.
    """
    try:
        vix_closes = _yahoo_closes("%5EVIX", days=120)
        spy_closes = _yahoo_closes("SPY",   days=10)

        vix_current  = vix_closes[-1]
        lookback     = min(90, len(vix_closes) - 1)
        vix_avg_90d  = sum(vix_closes[-lookback - 1 : -1]) / lookback
        vix_prev     = vix_closes[-2] if len(vix_closes) >= 2 else vix_current
        vix_rising   = vix_current > vix_prev

        spy_change_pct = 0.0
        if len(spy_closes) >= 2:
            spy_change_pct = (spy_closes[-1] - spy_closes[-2]) / spy_closes[-2] * 100

        if vix_current > vix_avg_90d and vix_rising and spy_change_pct < -1.0:
            regime = "CRISIS"
        elif vix_current > vix_avg_90d:
            regime = "HIGH_VOL"
        else:
            regime = "NORMAL"

        result = {
            "regime":          regime,
            "vix_current":     round(vix_current,    2),
            "vix_avg_90d":     round(vix_avg_90d,    2),
            "vix_rising":      vix_rising,
            "spy_change_pct":  round(spy_change_pct, 2),
            "updated_at":      datetime.now(ET).isoformat(),
        }

    except Exception as exc:
        logger.warning(f"VIX regime fetch failed: {exc} — defaulting to NORMAL")
        result = {
            "regime":      "NORMAL",
            "vix_current": 0.0,
            "vix_avg_90d": 0.0,
            "vix_rising":  False,
            "spy_change_pct": 0.0,
            "updated_at":  datetime.now(ET).isoformat(),
        }

    with open(config.VIX_REGIME_FILE, "w") as f:
        json.dump(result, f, indent=2)

    logger.info(
        f"VIX regime: {result['regime']} | "
        f"VIX={result['vix_current']:.1f} (90d avg={result['vix_avg_90d']:.1f}) | "
        f"SPY={result['spy_change_pct']:+.2f}%"
    )
    return result


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


if __name__ == "__main__":
    import logging as _l
    _l.basicConfig(level=_l.INFO)
    fetch_vix_regime()
