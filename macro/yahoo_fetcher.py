"""
Yahoo Finance data fetchers for macro regime. Boundary module — HTTP calls are intentional.
"""
import logging
from datetime import datetime, timedelta

import config
from utils.io_utils import load_json
from utils.constants import ET
from utils.http import yahoo_get

logger = logging.getLogger(__name__)


def yahoo_closes(symbol: str, days: int = 120) -> list[float]:
    """Fetch daily closing prices from Yahoo Finance."""
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?interval=1d&range={days}d"
    )
    data = yahoo_get(url)
    quotes = data["chart"]["result"][0]["indicators"]["quote"][0]
    return [c for c in quotes["close"] if c is not None]


def ret5d(closes: list[float]) -> float:
    """5-day return in %."""
    if len(closes) < 6:
        return 0.0
    return (closes[-1] - closes[-6]) / closes[-6] * 100


def load_bot_net_pnl() -> float:
    """Load recent 3-day P&L from trade journal; returns 0.0 on any error."""
    try:
        if not config.TRADE_JOURNAL_FILE.exists():
            return 0.0
        journal = load_json(config.TRADE_JOURNAL_FILE, [])
        cutoff = datetime.now(ET) - timedelta(days=3)
        recent = [
            t["pnl_dollar"] for t in journal
            if t.get("type") == "CLOSE"
            and t.get("pnl_dollar") is not None
            and t.get("timestamp", "") > cutoff.isoformat()[:10]
        ]
        if not recent:
            return 0.0
        total = sum(recent)
        try:
            from broker.alpaca_client import AlpacaClient
            portfolio = AlpacaClient().get_portfolio_value()
        except Exception:
            portfolio = 100_000.0
        return (total / portfolio) * 100 if portfolio > 0 else 0.0
    except Exception:
        return 0.0


def is_post_crisis_recovery() -> bool:
    """Check if VIX peaked above 35 in last 5 days and is now declining."""
    try:
        vix_closes = yahoo_closes("%5EVIX", days=10)
        if len(vix_closes) < 4:
            return False
        peak = max(vix_closes[-5:])
        current = vix_closes[-1]
        if peak > 35 and current < vix_closes[-3]:
            return True
    except Exception:
        pass
    return False


def fetch_closes_for_regime(tickers: dict[str, str]) -> dict[str, list[float]]:
    """Fetch 30-day daily closes for each ticker. Missing tickers default to []."""
    import logging
    _log = logging.getLogger(__name__)
    closes: dict[str, list[float]] = {}
    for key, ticker in tickers.items():
        try:
            closes[key] = yahoo_closes(ticker, days=30)
        except Exception as exc:
            _log.warning(f"macro_regime: could not fetch {ticker}: {exc}")
            closes[key] = []
    return closes


def build_signals_dict(closes: dict[str, list[float]], vix_current: float, vix3m_current: float) -> dict:
    """Assemble the signals dict passed to compute_macro_health_score."""
    return {
        "vix_closes":   closes.get("vix", []),
        "vix_current":  vix_current,
        "spy_ret5d":    ret5d(closes.get("spy", [])),
        "wti_ret5d":    ret5d(closes.get("wti", [])),
        "gold_ret5d":   ret5d(closes.get("gold", [])),
        "dxy_ret5d":    ret5d(closes.get("dxy", [])),
        "tnx_ret5d":    ret5d(closes.get("tnx", [])),
        "soxx_ret5d":   ret5d(closes.get("soxx", [])),
        "qqq_ret5d":    ret5d(closes.get("qqq", [])),
        "hyg_ret5d":    ret5d(closes.get("hyg", [])),
        "iwm_ret5d":    ret5d(closes.get("iwm", [])),
        "vix3m":        vix3m_current,
    }


def evaluate_lockout_recovery(
    flash_crash_active: bool,
    flash_crash_until: str | None,
    vix_current: float,
    spy_5day_return: float,
    now: datetime,
) -> tuple[bool, str | None]:
    """Return updated (flash_crash_active, flash_crash_until). Pure — no side effects."""
    if not (flash_crash_active and flash_crash_until):
        return flash_crash_active, flash_crash_until
    until_dt = datetime.fromisoformat(flash_crash_until)
    if now >= until_dt:
        return False, None
    if (vix_current < config.FLASH_CRASH_VIX_RECOVERY
            and spy_5day_return > config.FLASH_CRASH_SPY_RECOVERY_PCT):
        return False, None
    return True, flash_crash_until
