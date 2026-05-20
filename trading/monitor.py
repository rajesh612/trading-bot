"""
Position monitor — runs every 60 s.

Layer 1: Alpaca bracket fill reconciliation — detects bracket-order fills and syncs local state.
Layer 2: Price-level safety net — closes positions that breach SL/TP between cycles.
"""
import logging
import threading
import time
from datetime import datetime

import config
import notifications.telegram as telegram_notify
from broker.alpaca_client import AlpacaClient
from indicators.engine import compute_indicators
from trading.closer import close_position, _apply_profit_lock
from trading.journal import (
    _POSITIONS_LOCK,
    load_open_positions,
    append_trade,
    record_daily_closed,
    flush_profit_lock_updates,
)
from utils.io_utils import load_json, save_json
from utils.constants import ET
from utils.sl_blacklist import record_sl_hit

logger = logging.getLogger(__name__)

_DRAWDOWN_LOCK: threading.Lock = threading.Lock()
_drawdown_alerted: dict[str, float] = {}  # symbol → epoch of last alert


def _try_compute_iv(symbol: str, alpaca: AlpacaClient):
    """Return fresh IndicatorValues for exit context, or None if bars unavailable."""
    try:
        bars = alpaca.get_intraday_bars(symbol)
        return compute_indicators(symbol, bars)
    except Exception:
        return None


def _check_drawdown_alerts(positions: dict, current_prices: dict) -> None:
    """Fire drawdown alerts with per-position cooldown to avoid Telegram spam."""
    with _DRAWDOWN_LOCK:
        now_ts = time.time()
        cooldown_s = config.DRAWDOWN_ALERT_COOLDOWN_MIN * 60
        # Prune closed positions from cooldown dict
        for sym in list(_drawdown_alerted):
            if sym not in positions:
                del _drawdown_alerted[sym]
        for symbol, pos in positions.items():
            price = current_prices.get(symbol)
            if price is None:
                continue
            entry = pos.get("entry_price", 0)
            if entry <= 0:
                continue
            pnl_pct = (price - entry) / entry * 100
            stop_pct = -pos.get("stop_loss_pct", config.STOP_LOSS_PCT)
            if pnl_pct <= -config.DRAWDOWN_WARN_PCT and pnl_pct > stop_pct:
                last = _drawdown_alerted.get(symbol, 0.0)
                if now_ts - last >= cooldown_s:
                    telegram_notify.notify_position_drawdown(symbol, pnl_pct, stop_pct)
                    _drawdown_alerted[symbol] = now_ts


def run_position_monitor():
    """
    Two-layer intra-cycle protection:

    Layer 1 — Alpaca bracket reconciliation
        Detects when Alpaca has already closed a position via bracket fill
        and syncs local state.

    Layer 2 — Price-level safety net
        Manually closes positions that breach SL/TP levels between cycles.
    """
    open_positions = load_open_positions()
    if not open_positions:
        return

    alpaca           = AlpacaClient()
    alpaca_positions = alpaca.get_positions()
    now              = datetime.now(ET)

    for symbol, pos in list(open_positions.items()):

        # ── Layer 1: bracket fill reconciliation ─────────────────────────────
        if symbol not in alpaca_positions:
            with _POSITIONS_LOCK:
                disk_positions = load_json(config.OPEN_POSITIONS_FILE, {})
                if symbol not in disk_positions:
                    open_positions.pop(symbol, None)
                    continue
                pos = disk_positions.pop(symbol)
                open_positions.pop(symbol, None)
                save_json(config.OPEN_POSITIONS_FILE, disk_positions)

                entry_price = pos.get("entry_price", 0)
                if entry_price <= 0:
                    logger.warning("Position monitor: %s has zero/missing entry_price — skipping bracket reconciliation", symbol)
                    continue
                sl_price    = entry_price * (1 - pos.get("stop_loss_pct", config.STOP_LOSS_PCT) / 100)
                tp_price    = entry_price * (1 + pos.get("take_profit_pct", config.TAKE_PROFIT_PCT) / 100)
                try:
                    current_price = alpaca.get_latest_price(symbol)
                except Exception:
                    current_price = entry_price

                tol = config.BRACKET_FILL_TOLERANCE_PCT / 100
                if current_price <= sl_price * (1 + tol):
                    close_reason = "bracket_stop_loss_filled"
                    exit_price   = sl_price
                elif current_price >= tp_price * (1 - tol):
                    close_reason = "bracket_take_profit_filled"
                    exit_price   = tp_price
                else:
                    close_reason = "bracket_filled_unknown_leg"
                    exit_price   = current_price

                pnl_pct    = (exit_price - entry_price) / entry_price * 100
                pnl_dollar = (exit_price - entry_price) * pos["qty"]

                append_trade({
                    "type": "CLOSE",
                    "symbol": symbol,
                    "timestamp": now.isoformat(),
                    "exit_price": round(exit_price, 4),
                    "entry_price": round(entry_price, 4),
                    "qty": pos["qty"],
                    "pnl_pct": round(pnl_pct, 2),
                    "pnl_dollar": round(pnl_dollar, 2),
                    "close_reason": close_reason,
                    "entry_composite_score": pos.get("entry_composite_score"),
                    "exit_composite_score": None,
                    "exit_indicators": {},
                    "entry_indicators": pos.get("entry_indicators", {}),
                    "entry_sentiment_score": pos.get("entry_sentiment_score"),
                    "claude_reasoning": pos.get("claude_reasoning", ""),
                    "indicator_signals_at_entry": pos.get("indicator_signals", {}),
                    "atr_pct": pos.get("atr_pct", 0.0),
                })
                record_daily_closed(symbol)
            logger.info(
                "BRACKET FILL detected: %s closed via %s | P&L=%+.2f%% ($%+.2f) | no re-entry today",
                symbol, close_reason, pnl_pct, pnl_dollar,
            )
            telegram_notify.notify_close(
                symbol=symbol, qty=pos["qty"],
                entry=entry_price, exit_price=exit_price,
                pnl_dollar=round(pnl_dollar, 2), pnl_pct=round(pnl_pct, 2),
                reason=close_reason,
            )
            continue

        # ── Layer 2: price-level safety net ──────────────────────────────────
        try:
            current_price = alpaca.get_latest_price(symbol)
        except Exception as exc:
            logger.warning("Position monitor: could not get price for %s: %s", symbol, exc)
            continue

        pos = _apply_profit_lock(pos, current_price)
        open_positions[symbol] = pos

        entry_price = pos.get("entry_price", 0)
        if entry_price <= 0:
            logger.warning("Position monitor: %s has zero/missing entry_price — skipping SL/TP check", symbol)
            continue
        pnl_pct     = (current_price - entry_price) / entry_price * 100
        sl_pct      = pos.get("stop_loss_pct", config.STOP_LOSS_PCT)
        tp_pct      = pos.get("take_profit_pct", config.TAKE_PROFIT_PCT)

        if pnl_pct <= -sl_pct:
            logger.warning(
                "MONITOR STOP-LOSS: %s P&L=%+.2f%% breached SL=%.1f%% — closing (mid-cycle)",
                symbol, pnl_pct, -sl_pct,
            )
            close_position(symbol, alpaca, open_positions, current_price, _try_compute_iv(symbol, alpaca), "monitor_stop_loss")
            record_sl_hit(symbol)
        elif pnl_pct >= tp_pct:
            logger.info(
                "MONITOR TAKE-PROFIT: %s P&L=%+.2f%% hit TP=%.1f%% — closing (mid-cycle)",
                symbol, pnl_pct, tp_pct,
            )
            close_position(symbol, alpaca, open_positions, current_price, _try_compute_iv(symbol, alpaca), "monitor_take_profit")

    flush_profit_lock_updates(open_positions)

    # Collect current prices from positions still in open_positions for drawdown check
    current_prices_for_drawdown: dict[str, float] = {}
    for symbol, pos in open_positions.items():
        try:
            current_prices_for_drawdown[symbol] = alpaca.get_latest_price(symbol)
        except Exception:
            pass
    _check_drawdown_alerts(open_positions, current_prices_for_drawdown)

    try:
        from trading.swing_manager import monitor_swing_positions
        monitor_swing_positions(alpaca)
    except Exception as exc:
        logger.warning("Swing position monitor failed: %s", exc)
