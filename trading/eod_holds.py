"""
EOD hold qualification and promotion logic for swing positions.
"""
import logging
from datetime import datetime, timedelta
import config
from calendars.earnings import is_earnings_imminent
from macro.economic_calendar import is_event_risk_window
from utils.io_utils import load_json
from trading.tiered_tp import _compute_tier_quantities

from utils.constants import ET

logger = logging.getLogger(__name__)

_SWING_ELIGIBLE_REGIMES = {"BULL_TREND", "OPPORTUNITY", "CAPITULATION", "CRASH_RECOVERY"}


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
    """Return (True, '') if position qualifies for overnight hold, (False, reason) otherwise.

    Pure — no Alpaca calls, no file I/O.
    """
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


def evaluate_eod_holds(load_swing_positions, _save_swing_positions, _SWING_LOCK) -> list[str]:
    """
    Called at 3:50 PM ET. Evaluate open day-trade positions for overnight swing hold.
    Accepts persistence callables to avoid circular import with swing_manager.
    Returns list of promoted symbols.
    """
    from trading.journal import load_open_positions
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
        rr = load_json(config.RESEARCH_REPORT_FILE, {})
        sector_sentiment = rr.get("__meta__", {}).get("sector_sentiment", {})
    except Exception:
        pass

    for symbol, pos in open_positions.items():
        try:
            entry = pos["entry_price"]
            from broker.alpaca_client import AlpacaClient
            alpaca = AlpacaClient()
            current_price = alpaca.get_latest_price(symbol)
            pnl_pct = (current_price - entry) / entry * 100

            event_tomorrow, _ = is_event_risk_window(
                now.replace(hour=9, minute=30, second=0) + timedelta(days=1)
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
