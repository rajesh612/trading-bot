"""
Swing position lifecycle manager.
Handles EOD qualification, morning gap checks, and tiered TP monitoring.
"""
import json
import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import config
from earnings_calendar import is_earnings_imminent
from economic_calendar import is_event_risk_window

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

_SWING_LOCK = threading.Lock()

_SWING_ELIGIBLE_REGIMES = {"BULL_TREND", "OPPORTUNITY", "CAPITULATION", "CRASH_RECOVERY"}


# ── Persistence ───────────────────────────────────────────────────────────────

def load_swing_positions() -> dict:
    try:
        if config.SWING_POSITIONS_FILE.exists():
            with open(config.SWING_POSITIONS_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_swing_positions(positions: dict) -> None:
    with open(config.SWING_POSITIONS_FILE, "w") as f:
        json.dump(positions, f, indent=2)


# ── Tiered quantity split ─────────────────────────────────────────────────────

def _compute_tier_quantities(total_qty: int) -> tuple[int, int, int]:
    """
    Split total_qty into (tp1_qty, tp2_qty, trailing_qty) at 40/30/30.
    Guarantees whole shares that sum to total_qty.
    """
    if total_qty <= 1:
        return total_qty, 0, 0
    if total_qty == 2:
        return 1, 1, 0
    tp1 = max(1, round(total_qty * 0.4))
    tp2 = max(1, round(total_qty * 0.3))
    trailing = total_qty - tp1 - tp2
    if trailing <= 0:
        tp2 = max(1, total_qty - tp1 - 1)
        trailing = total_qty - tp1 - tp2
    if trailing <= 0:
        trailing = 1
        tp2 = total_qty - tp1 - trailing
    return tp1, tp2, trailing


# ── Tiered TP evaluation (pure function) ─────────────────────────────────────

def evaluate_tiered_tp_action(position: dict, current_price: float) -> dict:
    """
    Priority: hard stop > TP1 > TP2 (activates trailing) > trailing drawdown.
    Returns {action, qty, activate_trailing, reason}.
    action ∈ NONE | CLOSE_TP1 | CLOSE_TP2 | CLOSE_TRAILING | CLOSE_HARD_STOP
    """
    entry = position["entry_price"]
    pnl_pct = (current_price - entry) / entry * 100

    sl_pct = position.get("stop_loss_pct", 7.0)
    tp1_pct = position.get("tp1_pct", 5.0)
    tp2_pct = position.get("tp2_pct", 10.0)

    # Hard stop (entire remaining position)
    remaining_qty = (
        position.get("trailing_qty", 0)
        + (0 if position.get("tp1_hit") else position.get("tp1_qty", 0))
        + (0 if position.get("tp2_hit") else position.get("tp2_qty", 0))
    )
    if pnl_pct <= -sl_pct:
        return {"action": "CLOSE_HARD_STOP", "qty": remaining_qty, "activate_trailing": False,
                "reason": f"hard stop at {pnl_pct:.2f}%"}

    # TP1: close tp1_qty shares
    if not position.get("tp1_hit") and pnl_pct >= tp1_pct:
        tp1_qty = position.get("tp1_qty", 0)
        if tp1_qty > 0:
            return {"action": "CLOSE_TP1", "qty": tp1_qty, "activate_trailing": False,
                    "reason": f"TP1 at {pnl_pct:.2f}%"}

    # TP2: close tp2_qty and activate trailing
    if position.get("tp1_hit") and not position.get("tp2_hit") and pnl_pct >= tp2_pct:
        tp2_qty = position.get("tp2_qty", 0)
        if tp2_qty > 0:
            return {"action": "CLOSE_TP2", "qty": tp2_qty, "activate_trailing": True,
                    "reason": f"TP2 at {pnl_pct:.2f}%, activating trailing stop"}

    # Trailing stop: 3% drawdown from high water after TP2 hit
    if position.get("tp2_hit") and position.get("trailing_stop_active"):
        high_water = position.get("trailing_high_water", entry)
        trailing_qty = position.get("trailing_qty", 0)
        if trailing_qty > 0 and current_price <= high_water * 0.97:
            return {"action": "CLOSE_TRAILING", "qty": trailing_qty, "activate_trailing": False,
                    "reason": f"trailing stop at {pnl_pct:.2f}% ({current_price:.2f} vs HW {high_water:.2f})"}

    return {"action": "NONE", "qty": 0, "activate_trailing": False, "reason": ""}


# ── EOD qualification ─────────────────────────────────────────────────────────

def qualifies_for_eod_hold(
    symbol: str,
    is_tier1: bool,
    regime: str,
    event_risk_tomorrow: bool,
    earnings_in_2_days: bool,
    current_pnl_pct: float,
    existing_swing_count: int,
    sector_sentiment_score: float,
) -> tuple[bool, str]:
    """Return (True, '') if position qualifies for overnight hold, (False, reason) otherwise."""
    if not is_tier1:
        return False, f"{symbol} not in tier-1 list"
    if regime not in _SWING_ELIGIBLE_REGIMES:
        return False, f"regime {regime} not eligible for swing"
    if event_risk_tomorrow:
        return False, "high-impact event tomorrow morning"
    if earnings_in_2_days and regime != "CAPITULATION":
        return False, "earnings within 2 sessions (not CAPITULATION mode)"
    if current_pnl_pct <= -2.0:
        return False, f"PTJ rule: P&L {current_pnl_pct:.1f}% ≤ -2% (don't hold losers overnight)"
    if existing_swing_count >= config.SWING_MAX_POSITIONS:
        return False, f"swing limit reached ({existing_swing_count}/{config.SWING_MAX_POSITIONS})"
    if sector_sentiment_score < 10:
        return False, f"sector sentiment {sector_sentiment_score:.0f} < +10"
    return True, ""


def evaluate_eod_holds() -> list[str]:
    """
    Called at 3:50 PM ET. Evaluate open day-trade positions for overnight swing hold.
    Returns list of promoted symbols.
    """
    from trading_agent import load_open_positions
    from macro_regime import load_macro_regime
    from tier1_config import is_tier1, get_sector

    regime_info = load_macro_regime()
    regime = regime_info.get("regime", "NORMAL")
    now = datetime.now(ET)

    open_positions = load_open_positions()
    existing_swings = load_swing_positions()
    existing_count = len(existing_swings)
    promoted = []

    # Load sector sentiment from research report
    sector_sentiment: dict = {}
    try:
        if config.RESEARCH_REPORT_FILE.exists():
            with open(config.RESEARCH_REPORT_FILE) as f:
                rr = json.load(f)
            sector_sentiment = rr.get("__meta__", {}).get("sector_sentiment", {})
    except Exception:
        pass

    for symbol, pos in open_positions.items():
        try:
            entry = pos["entry_price"]
            from alpaca_client import AlpacaClient
            alpaca = AlpacaClient()
            current_price = alpaca.get_latest_price(symbol)
            pnl_pct = (current_price - entry) / entry * 100

            event_tomorrow, _ = is_event_risk_window(
                now.replace(hour=9, minute=30, second=0) + __import__("datetime").timedelta(days=1)
            )
            earnings_soon = is_earnings_imminent(symbol, days=2)
            sector = get_sector(symbol)
            sect_score = sector_sentiment.get(sector, 0)

            qualifies, reason = qualifies_for_eod_hold(
                symbol=symbol,
                is_tier1=is_tier1(symbol),
                regime=regime,
                event_risk_tomorrow=event_tomorrow,
                earnings_in_2_days=earnings_soon,
                current_pnl_pct=pnl_pct,
                existing_swing_count=existing_count + len(promoted),
                sector_sentiment_score=sect_score,
            )
            if not qualifies:
                logger.info(f"SWING SKIP {symbol}: {reason}")
                continue

            # Use capitulation TP tiers if in CAPITULATION regime
            capitulation = regime == "CAPITULATION"
            tp1_pct  = 10.0 if capitulation else 5.0
            tp2_pct  = 20.0 if capitulation else 10.0
            sl_pct   = 12.0 if capitulation else pos.get("stop_loss_pct", 7.0)

            total_qty = int(pos["qty"])
            tp1_qty, tp2_qty, trailing_qty = _compute_tier_quantities(total_qty)
            max_days = config.SWING_MAX_DAYS

            swing_pos = {
                "symbol":              symbol,
                "entry_price":         entry,
                "qty":                 total_qty,
                "entry_time":          pos.get("entry_time", now.isoformat()),
                "stop_loss_pct":       sl_pct,
                "take_profit_pct":     tp2_pct,
                "stop_loss_price":     round(entry * (1 - sl_pct / 100), 2),
                "take_profit_price":   round(entry * (1 + tp2_pct / 100), 2),
                "tp1_pct":             tp1_pct,
                "tp1_qty":             tp1_qty,
                "tp1_hit":             False,
                "tp2_pct":             tp2_pct,
                "tp2_qty":             tp2_qty,
                "tp2_hit":             False,
                "trailing_qty":        trailing_qty,
                "trailing_stop_active": False,
                "trailing_high_water": current_price,
                "swing_days_held":     0,
                "max_swing_days":      max_days,
                "regime_at_entry":     regime,
                "capitulation_hold":   capitulation,
                "hard_deadline":       None,
                "claude_reasoning":    pos.get("claude_reasoning", ""),
                "order_id":            pos.get("order_id"),
                "mode_at_entry":       "HYBRID" if not capitulation else "SHORT_TERM",
            }

            with _SWING_LOCK:
                swings = load_swing_positions()
                swings[symbol] = swing_pos
                _save_swing_positions(swings)

            promoted.append(symbol)
            logger.info(
                f"SWING PROMOTE {symbol}: P&L={pnl_pct:+.2f}% | "
                f"TP1={tp1_pct}% TP2={tp2_pct}% SL={sl_pct}% | {regime} regime"
            )
        except Exception as exc:
            logger.warning(f"evaluate_eod_holds: error for {symbol}: {exc}")

    return promoted


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
