"""
Position close helpers and EOD force-close logic.
All state reads/writes hold _POSITIONS_LOCK from trading.journal.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict
from datetime import datetime
import config
import notifications.telegram as telegram_notify
from broker.alpaca_client import AlpacaClient
from indicators.values import IndicatorValues
from utils.io_utils import load_json, save_json
from trading.journal import (
    _POSITIONS_LOCK,
    append_trade,
    record_daily_closed,
)

from utils.constants import ET

logger = logging.getLogger(__name__)


def _eod_close_reason(pnl_pct: float) -> str:
    if pnl_pct > config.EOD_PROFITABLE_THRESHOLD:
        return "eod_force_profitable"
    if pnl_pct < config.EOD_LOSS_THRESHOLD:
        return "eod_force_loss"
    return "eod_force_flat"


def _apply_profit_lock(pos: dict, current_price: float) -> dict:
    """
    If unrealised PnL >= PROFIT_LOCK_TRIGGER_PCT, move stop_loss_price to at least entry_price.
    Returns the updated position dict (modifies in-place and returns).
    """
    entry_price = pos.get("entry_price", 0.0)
    if entry_price <= 0:
        return pos
    pnl_pct = (current_price - entry_price) / entry_price * 100
    if pnl_pct >= config.PROFIT_LOCK_TRIGGER_PCT:
        current_stop = pos.get("stop_loss_price", 0.0)
        new_stop     = max(current_stop, entry_price)
        if new_stop > current_stop:
            pos["stop_loss_price"] = new_stop
            logger.info(
                "Profit lock: %s stop moved to %.2f (entry=%.2f, pnl=%.2f%%)",
                pos.get("symbol", "?"), new_stop, entry_price, pnl_pct,
            )
    return pos


def close_position(
    symbol: str,
    alpaca: AlpacaClient,
    open_positions: dict,
    current_price: float,
    iv: IndicatorValues | None,
    reason: str,
) -> None:
    """
    Close a single position: locked disk read-modify-write, then Alpaca API call
    and journal entry outside the lock. Idempotent — no-ops if already removed.
    """
    with _POSITIONS_LOCK:
        disk_positions = load_json(config.OPEN_POSITIONS_FILE, {})
        if symbol not in disk_positions:
            open_positions.pop(symbol, None)
            logger.debug("close_position: %s already closed by concurrent thread — skip.", symbol)
            return
        pos = disk_positions.pop(symbol)
        open_positions.pop(symbol, None)
        save_json(config.OPEN_POSITIONS_FILE, disk_positions)

    # Cancel server-side stop order placed by T2 addon (best-effort)
    stop_order_id = pos.get("stop_order_id") if pos else None
    if stop_order_id:
        try:
            alpaca.cancel_order(stop_order_id)
        except Exception as exc:
            logger.warning("close_position %s: cancel stop order %s failed — %s", symbol, stop_order_id, exc)

    alpaca.close_position(symbol)

    entry_price = pos["entry_price"]
    qty         = pos["qty"]
    if not entry_price or entry_price <= 0:
        logger.warning("close_position: %s has invalid entry_price %s — P&L set to 0.0", symbol, entry_price)
        entry_price = 0.0
        pnl_pct    = 0.0
        pnl_dollar = 0.0
    else:
        pnl_pct    = (current_price - entry_price) / entry_price * 100
        pnl_dollar = (current_price - entry_price) * qty

    append_trade({
        "type": "CLOSE",
        "symbol": symbol,
        "timestamp": datetime.now(ET).isoformat(),
        "exit_price": round(current_price, 4),
        "entry_price": round(entry_price, 4),
        "qty": qty,
        "pnl_pct": round(pnl_pct, 2),
        "pnl_dollar": round(pnl_dollar, 2),
        "close_reason": reason,
        "entry_composite_score": pos.get("entry_composite_score"),
        "exit_composite_score": iv.composite_score if iv else None,
        "exit_indicators": asdict(iv) if iv else {},
        "entry_indicators": pos.get("entry_indicators", {}),
        "entry_sentiment_score": pos.get("entry_sentiment_score"),
        "claude_reasoning": pos.get("claude_reasoning", ""),
        "indicator_signals_at_entry": pos.get("indicator_signals", {}),
        "mode_at_entry": pos.get("mode_at_entry"),
        "regime_at_entry": pos.get("regime_at_entry"),
        "atr_pct": pos.get("atr_pct", 0.0),
    })

    record_daily_closed(symbol)
    logger.info(
        "CLOSE %s: P&L=%+.2f%% ($%+.2f) | %s | no re-entry today",
        symbol, pnl_pct, pnl_dollar, reason,
    )
    telegram_notify.notify_close(
        symbol=symbol, qty=qty,
        entry=entry_price, exit_price=current_price,
        pnl_dollar=round(pnl_dollar, 2), pnl_pct=round(pnl_pct, 2),
        reason=reason,
    )


def force_close_all() -> None:
    """End-of-day: cancel orders, close all non-swing day-trade positions."""
    _force_close_body()


def _force_close_body() -> None:
    logger.info("Force-closing all day-trade positions @ 3:55 PM ET")
    alpaca = AlpacaClient()
    alpaca.cancel_all_orders()

    try:
        from trading.swing_manager import load_swing_positions
        swing_symbols = set(load_swing_positions().keys())
    except Exception:
        swing_symbols = set()
    if swing_symbols:
        logger.info("Preserving %d swing position(s): %s", len(swing_symbols), sorted(swing_symbols))

    with _POSITIONS_LOCK:
        open_positions = load_json(config.OPEN_POSITIONS_FILE, {})

    live_symbols: set[str] | None = None
    if config.EOD_VERIFY_FILLS:
        try:
            live_symbols = set(alpaca.get_positions().keys())
        except Exception as exc:
            logger.warning("EOD: could not fetch live positions for fill verification: %s", exc)

    now = datetime.now(ET)
    to_remove: list[str] = []

    for symbol, pos in list(open_positions.items()):
        if symbol in swing_symbols:
            continue

        if live_symbols is not None and symbol not in live_symbols:
            logger.warning(
                "EOD: %s not in Alpaca positions (phantom entry) — skipping journal", symbol
            )
            to_remove.append(symbol)
            continue

        try:
            price = alpaca.get_latest_price(symbol)
        except Exception:
            price = pos["entry_price"]
            logger.warning("Could not get price for %s — using entry price for P&L calc.", symbol)

        entry_price = pos["entry_price"]
        if not entry_price or entry_price <= 0:
            logger.warning("EOD: %s has invalid entry_price %s — using 0.0 for P&L", symbol, entry_price)
            entry_price = None
        pnl_pct    = (price - entry_price) / entry_price * 100 if entry_price else 0.0
        pnl_dollar = (price - entry_price) * pos["qty"] if entry_price else 0.0

        try:
            alpaca.close_position(symbol)
        except Exception as e:
            logger.warning("EOD close failed for %s, retrying: %s", symbol, e)
            time.sleep(2)
            try:
                alpaca.close_position(symbol)
            except Exception as e2:
                logger.error("EOD close retry failed for %s: %s", symbol, e2)
                try:
                    alpaca.close_all_positions()
                except Exception:
                    pass
                try:
                    remaining = list(alpaca.get_positions().keys())
                    if remaining:
                        telegram_notify._send(f"⚠️ EOD close failed, positions still open: {remaining}")
                except Exception:
                    pass

        append_trade({
            "type": "CLOSE",
            "symbol": symbol,
            "timestamp": now.isoformat(),
            "exit_price": round(price, 4),
            "entry_price": round(entry_price or 0.0, 4),
            "qty": pos["qty"],
            "pnl_pct": round(pnl_pct, 2),
            "pnl_dollar": round(pnl_dollar, 2),
            "close_reason": _eod_close_reason(pnl_pct),
            "entry_composite_score": pos.get("entry_composite_score"),
            "exit_composite_score": None,
            "exit_indicators": {},
            "entry_indicators": pos.get("entry_indicators", {}),
            "entry_sentiment_score": pos.get("entry_sentiment_score"),
            "claude_reasoning": pos.get("claude_reasoning", ""),
            "indicator_signals_at_entry": pos.get("indicator_signals", {}),
            "mode_at_entry": pos.get("mode_at_entry"),
            "regime_at_entry": pos.get("regime_at_entry"),
            "atr_pct": pos.get("atr_pct", 0.0),
        })
        to_remove.append(symbol)
        logger.info("EOD close %s: P&L=%+.2f%% ($%+.2f)", symbol, pnl_pct, pnl_dollar)

    # Sweep untracked Alpaca positions (reconciliation gap)
    try:
        alpaca_positions = alpaca.get_positions()
        for sym in list(alpaca_positions.keys()):
            if sym not in swing_symbols and sym not in open_positions:
                try:
                    alpaca.close_position(sym)
                    logger.warning("EOD: closed untracked Alpaca position %s", sym)
                except Exception as exc:
                    logger.warning("EOD: could not close %s in Alpaca: %s", sym, exc)
    except Exception as exc:
        logger.warning("EOD: get_positions failed: %s", exc)

    # Clear local state — swing positions remain in swing_positions.json
    with _POSITIONS_LOCK:
        disk = load_json(config.OPEN_POSITIONS_FILE, {})
        remaining = {sym: pos for sym, pos in disk.items() if sym in swing_symbols}
        save_json(config.OPEN_POSITIONS_FILE, remaining)

    logger.info(
        "Day-trade positions force-closed. %d swing position(s) preserved.",
        len(swing_symbols),
    )
