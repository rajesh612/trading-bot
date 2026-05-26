"""
Flash-crash / market-monitor logic.

Called every 5 min during market hours by the APScheduler job in main.py.
Checks SPY for a rapid intraday drop and, when confirmed, force-closes all
positions and sets a 48h CRISIS lockout in macro_regime.json.
"""
import logging
from datetime import datetime, timedelta
import requests

import config
import notifications.telegram as telegram_notify
from data.market_data import get_latest_price
from utils.io_utils import load_json, save_json
from utils.constants import ET

logger = logging.getLogger(__name__)


def is_flash_crash_active() -> bool:
    """Return True if a flash-crash CRISIS lockout is currently in effect."""
    data = load_json(config.MACRO_REGIME_FILE, {})
    return bool(data.get("flash_crash_active", False))


# ── SPY data helpers ─────────────────────────────────────────────────────────

def _get_spy_prev_close() -> float:
    """Get SPY previous daily close via Yahoo Finance."""
    try:
        from data.market_data import get_daily_bars
        df = get_daily_bars("SPY", days=5)
        if len(df) >= 2:
            return float(df["close"].iloc[-2])
    except Exception:
        pass
    return 0.0


def _verify_spy_with_polygon(spy_pct: float) -> bool | None:
    """
    Cross-check SPY drop percentage with Polygon/Massive API.

    Returns True (confirmed crash), False (no crash), or None (unavailable).
    """
    if not config.POLYGON_API_KEY:
        return None
    try:
        resp = requests.get(
            "https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers/SPY",
            headers={"Authorization": f"Bearer {config.POLYGON_API_KEY}"},
            timeout=5,
        )
        resp.raise_for_status()
        polygon_pct = float(resp.json()["ticker"]["todaysChangePerc"])
        return polygon_pct <= config.FLASH_CRASH_SPY_DROP_PCT
    except Exception as exc:
        logger.warning(f"Polygon/Massive check failed ({exc}) — falling back to two-reading confirmation")
        return None


# ── Flash crash state helpers ────────────────────────────────────────────────

def _write_flash_crash_candidate(spy_pct: float, now: datetime) -> None:
    """Write Stage 1 candidate to macro_regime.json."""
    try:
        data = load_json(config.MACRO_REGIME_FILE, {})
        data["flash_crash_candidate"] = {"spy_pct": round(spy_pct, 2), "detected_at": now.isoformat()}
        save_json(config.MACRO_REGIME_FILE, data)
    except Exception as exc:
        logger.warning(f"Could not write flash_crash_candidate: {exc}")


def _clear_flash_crash_candidate() -> None:
    """Clear flash_crash_candidate in macro_regime.json."""
    try:
        if not config.MACRO_REGIME_FILE.exists():
            return
        data = load_json(config.MACRO_REGIME_FILE, {})
        if data.get("flash_crash_candidate") is not None:
            data["flash_crash_candidate"] = None
            save_json(config.MACRO_REGIME_FILE, data)
    except Exception as exc:
        logger.warning(f"Could not clear flash_crash_candidate: {exc}")


def _read_flash_crash_candidate() -> dict | None:
    """Return the current flash_crash_candidate dict, or None if absent/unset."""
    if not config.MACRO_REGIME_FILE.exists():
        return None
    data = load_json(config.MACRO_REGIME_FILE, {})
    candidate = data.get("flash_crash_candidate")
    if not candidate:
        return None
    return candidate


def _set_crisis_lockout(hours: int = 48) -> None:
    """Write flash_crash_active=True with flash_crash_until timestamp to macro_regime.json."""
    try:
        data = load_json(config.MACRO_REGIME_FILE, {})
        until = (datetime.now(ET) + timedelta(hours=hours)).isoformat()
        data["flash_crash_active"] = True
        data["flash_crash_until"]  = until
        data["regime"] = "CRISIS"
        data["flash_crash_candidate"] = None
        save_json(config.MACRO_REGIME_FILE, data)
        logger.critical(f"FLASH CRASH LOCKOUT set — CRISIS regime active until {until}")
    except Exception as exc:
        logger.error(f"Could not write flash crash lockout: {exc}")


def _trigger_flash_crash_lockout() -> None:
    """Force-close all positions and set 48h CRISIS lockout."""
    from trading.closer import force_close_all  # lazy import to avoid circular dependency
    force_close_all()
    try:
        from trading.swing_manager import load_swing_positions, _execute_full_close
        from broker.alpaca_client import AlpacaClient
        alpaca = AlpacaClient()
        for sym, pos in list(load_swing_positions().items()):
            _execute_full_close(sym, pos, alpaca, "flash_crash")
    except Exception as exc:
        logger.warning(f"flash crash: swing close failed: {exc}")
    telegram_notify.notify_flash_crash_lockout() if hasattr(telegram_notify, "notify_flash_crash_lockout") else None
    _set_crisis_lockout(hours=48)


# ── Stage 2 confirmation ─────────────────────────────────────────────────────

def _handle_flash_crash_stage2(spy_pct: float) -> None:
    """Two-reading confirmation path (no Polygon key, or Polygon returned None)."""
    now = datetime.now(ET)
    try:
        candidate = _read_flash_crash_candidate()

        if candidate is None:
            _write_flash_crash_candidate(spy_pct, now)
            logger.warning("Flash crash Stage 1 — awaiting Stage 2 confirmation in 5 min")
            return

        age_min = (now - datetime.fromisoformat(candidate["detected_at"])).total_seconds() / 60
        if age_min <= 15:
            logger.critical(f"Flash crash Stage 2 confirmed — SPY still at {spy_pct:+.1f}%")
            _trigger_flash_crash_lockout()
        else:
            _write_flash_crash_candidate(spy_pct, now)
            logger.warning("Flash crash stale candidate overwritten — awaiting fresh Stage 2")
    except Exception as exc:
        logger.warning(f"Flash crash stage 2 check failed: {exc}")


# ── Top-level entrypoint ─────────────────────────────────────────────────────

def run_market_monitor() -> None:
    """
    Called every 5 min during market hours. Checks for SPY flash crash.
    Requires two consecutive readings (or one Polygon-confirmed reading) before lockout.
    """
    try:
        spy_current = get_latest_price("SPY")
        spy_prev    = _get_spy_prev_close()
        if spy_prev <= 0:
            return
        spy_pct = (spy_current - spy_prev) / spy_prev * 100

        if spy_pct > config.FLASH_CRASH_SPY_DROP_PCT:
            _clear_flash_crash_candidate()
            return

        logger.warning(
            f"FLASH CRASH CANDIDATE: SPY={spy_current:.2f} prev={spy_prev:.2f} ({spy_pct:+.1f}%)"
        )

        if config.POLYGON_API_KEY:
            result = _verify_spy_with_polygon(spy_pct)
            if result is True:
                logger.critical("Polygon confirmed flash crash — triggering lockout immediately")
                _trigger_flash_crash_lockout()
                return
            if result is False:
                logger.info("Polygon disagrees — bad SPY tick, discarding candidate")
                _clear_flash_crash_candidate()
                return
            # result is None → Polygon unavailable → fall through to Stage 2

        _handle_flash_crash_stage2(spy_pct)

    except Exception as exc:
        logger.debug(f"run_market_monitor: {exc}")
