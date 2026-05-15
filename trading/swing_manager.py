"""
Swing position lifecycle manager.
Handles EOD qualification, morning gap checks, and tiered TP monitoring.
"""
import logging
import threading
from datetime import datetime
import config
from calendars.earnings import is_earnings_imminent
from utils.io_utils import load_json, save_json
from trading.tiered_tp import _compute_tier_quantities, evaluate_tiered_tp_action
from trading.eod_holds import qualifies_for_eod_hold, _SWING_ELIGIBLE_REGIMES

from utils.constants import ET

logger = logging.getLogger(__name__)

_SWING_LOCK = threading.Lock()


# ── Persistence ───────────────────────────────────────────────────────────────

def load_swing_positions() -> dict:
    return load_json(config.SWING_POSITIONS_FILE, {})


def _save_swing_positions(positions: dict) -> None:
    save_json(config.SWING_POSITIONS_FILE, positions)


# ── EOD holds (thin wrapper) ──────────────────────────────────────────────────

def evaluate_eod_holds() -> list[str]:
    """
    Called at 3:50 PM ET. Evaluate open day-trade positions for overnight swing hold.
    Returns list of promoted symbols.
    """
    from trading.eod_holds import evaluate_eod_holds as _eval
    return _eval(load_swing_positions, _save_swing_positions, _SWING_LOCK)


# ── Morning gap check ─────────────────────────────────────────────────────────

def _evaluate_gap_action(position: dict, current_price: float) -> str:
    """
    Evaluate pre-market gap vs stop loss.
    Returns: CLOSE_GAP_DOWN | TIGHTEN_STOP | TAKE_TP1_GAP_UP | NONE
    """
    entry = position["entry_price"]
    gap_pct = (current_price - entry) / entry * 100
    sl_pct = position.get("stop_loss_pct", 7.0)

    if gap_pct <= -sl_pct:
        return "CLOSE_GAP_DOWN"
    elif -sl_pct < gap_pct <= -2.0:
        return "TIGHTEN_STOP"
    elif gap_pct >= 3.0 and not position.get("tp1_hit"):
        return "TAKE_TP1_GAP_UP"
    return "NONE"


def run_gap_check(alpaca_client) -> None:
    """Called at 9:30 AM. Check pre-market price for each swing position."""
    swings = load_swing_positions()
    if not swings:
        return

    for symbol, pos in list(swings.items()):
        try:
            current_price = alpaca_client.get_latest_price(symbol)
            action = _evaluate_gap_action(pos, current_price)

            if action == "CLOSE_GAP_DOWN":
                logger.warning(f"GAP CHECK {symbol}: gap down past stop — closing at open")
                alpaca_client.close_position(symbol)
                with _SWING_LOCK:
                    s = load_swing_positions()
                    s.pop(symbol, None)
                    _save_swing_positions(s)

            elif action == "TIGHTEN_STOP":
                logger.info(f"GAP CHECK {symbol}: gap down 2-5% — tightening trailing stop to 2%")
                with _SWING_LOCK:
                    s = load_swing_positions()
                    if symbol in s:
                        s[symbol]["stop_loss_pct"] = 2.0
                        s[symbol]["stop_loss_price"] = round(current_price * 0.98, 2)
                        _save_swing_positions(s)

            elif action == "TAKE_TP1_GAP_UP":
                tp1_qty = pos.get("tp1_qty", 0)
                if tp1_qty > 0:
                    logger.info(f"GAP CHECK {symbol}: gap up ≥3% — taking TP1 ({tp1_qty} shares)")
                    alpaca_client.place_market_order(symbol, tp1_qty, "SELL")
                    with _SWING_LOCK:
                        s = load_swing_positions()
                        if symbol in s:
                            s[symbol]["tp1_hit"] = True
                            s[symbol]["trailing_stop_active"] = True
                            _save_swing_positions(s)

        except Exception as exc:
            logger.warning(f"GAP CHECK {symbol}: error — {exc}")


# ── Intraday swing monitor (called every 60s from position_monitor) ───────────

def monitor_swing_positions(alpaca_client) -> None:
    """Check tiered TP/trailing and max-days limit for all swing positions."""
    swings = load_swing_positions()
    if not swings:
        return

    now = datetime.now(ET)

    for symbol, pos in list(swings.items()):
        try:
            current_price = alpaca_client.get_latest_price(symbol)

            # Update high water mark if trailing is active
            if pos.get("trailing_stop_active"):
                if current_price > pos.get("trailing_high_water", 0):
                    with _SWING_LOCK:
                        s = load_swing_positions()
                        if symbol in s:
                            s[symbol]["trailing_high_water"] = current_price
                            _save_swing_positions(s)
                    pos["trailing_high_water"] = current_price

            # Check earnings before close (close EOD if earnings imminent, unless CAPITULATION)
            if not pos.get("capitulation_hold") and is_earnings_imminent(symbol, days=1):
                logger.warning(f"SWING {symbol}: earnings imminent — closing to avoid overnight risk")
                _execute_full_close(symbol, pos, alpaca_client, "earnings_imminent_close")
                continue

            # Max days check
            entry_time = pos.get("entry_time", "")
            if entry_time:
                try:
                    entry_dt = datetime.fromisoformat(entry_time)
                    days_held = (now - entry_dt).days
                    if days_held >= pos.get("max_swing_days", config.SWING_MAX_DAYS):
                        logger.info(f"SWING {symbol}: max days ({days_held}) reached — closing")
                        remaining = _remaining_qty(pos)
                        if remaining > 0:
                            alpaca_client.place_market_order(symbol, remaining, "SELL")
                        with _SWING_LOCK:
                            s = load_swing_positions()
                            s.pop(symbol, None)
                            _save_swing_positions(s)
                        continue
                except Exception:
                    pass

            result = evaluate_tiered_tp_action(pos, current_price)
            action = result["action"]
            qty    = result["qty"]

            if action == "NONE":
                continue

            if action == "CLOSE_HARD_STOP":
                logger.warning(f"SWING {symbol}: hard stop triggered — closing {qty} shares")
                _execute_full_close(symbol, pos, alpaca_client, "hard_stop")
            elif action in ("CLOSE_TP1", "CLOSE_TP2", "CLOSE_TRAILING"):
                _execute_partial_close(symbol, action, qty, alpaca_client, current_price)

        except Exception as exc:
            logger.warning(f"monitor_swing_positions: error for {symbol}: {exc}")


def _execute_partial_close(
    symbol: str,
    action: str,
    qty: int,
    alpaca_client,
    current_price: float,
) -> None:
    """Submit a partial market sell and update swing state under _SWING_LOCK.

    action ∈ CLOSE_TP1 | CLOSE_TP2 | CLOSE_TRAILING
    Removes the position from swing_positions.json when all shares are sold.
    """
    if qty > 0:
        try:
            alpaca_client.place_market_order(symbol, qty, "SELL")
        except Exception as exc:
            logger.warning(f"_execute_partial_close {symbol}: order failed — {exc}")
            return

    with _SWING_LOCK:
        swings = load_swing_positions()
        if symbol not in swings:
            return
        pos = swings[symbol]

        if action == "CLOSE_TP1":
            pos["tp1_hit"] = True
        elif action == "CLOSE_TP2":
            pos["tp2_hit"] = True
            pos["trailing_stop_active"] = True
            pos["trailing_high_water"] = current_price
        elif action == "CLOSE_TRAILING":
            swings.pop(symbol, None)
            _save_swing_positions(swings)
            logger.info(f"SWING {symbol}: CLOSE_TRAILING — sold {qty} shares @ {current_price:.2f}")
            return

        if _remaining_qty(pos) <= 0:
            swings.pop(symbol, None)
        else:
            swings[symbol] = pos
        _save_swing_positions(swings)

    logger.info(f"SWING {symbol}: {action} — sold {qty} shares @ {current_price:.2f}")


def _remaining_qty(pos: dict) -> int:
    qty = pos.get("qty", 0)
    if pos.get("tp1_hit"):
        qty -= pos.get("tp1_qty", 0)
    if pos.get("tp2_hit"):
        qty -= pos.get("tp2_qty", 0)
    return max(0, qty)


def _execute_full_close(symbol: str, pos: dict, alpaca_client, reason: str) -> None:
    remaining = _remaining_qty(pos)
    if remaining > 0:
        try:
            alpaca_client.place_market_order(symbol, remaining, "SELL")
        except Exception as exc:
            logger.warning(f"_execute_full_close {symbol}: {exc}")
    with _SWING_LOCK:
        s = load_swing_positions()
        s.pop(symbol, None)
        _save_swing_positions(s)
    logger.info(f"SWING CLOSED {symbol}: {remaining} shares — {reason}")

