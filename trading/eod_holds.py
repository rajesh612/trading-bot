"""
EOD hold qualification and promotion logic for swing positions.
"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import config
from calendars.earnings import is_earnings_imminent
from utils import regimes
from utils.io_utils import load_json, save_json
from trading.tiered_tp import _compute_tier_quantities

from utils.constants import ET

logger = logging.getLogger(__name__)


def _promote_to_swing(symbol: str, position: dict, pnl_pct: float) -> bool:
    """Promote day-trade to swing. Re-reads count under lock to prevent race."""
    from trading.journal import _POSITIONS_LOCK
    with _POSITIONS_LOCK:
        swing_positions = load_json(config.SWING_POSITIONS_FILE, {})
        if len(swing_positions) >= config.SWING_MAX_POSITIONS:
            logger.info("SWING SKIP %s — limit %d reached (re-read under lock)", symbol, config.SWING_MAX_POSITIONS)
            return False
        swing_positions[symbol] = {
            **position,
            "swing_promoted_at": datetime.now(ET).isoformat(),
            "pnl_at_promote_pct": pnl_pct,
        }
        save_json(config.SWING_POSITIONS_FILE, swing_positions)
        return True


def qualifies_for_eod_hold(
    symbol: str,
    regime: str,
    earnings_tomorrow: bool,
    current_pnl_pct: float,
    entry_sector_sentiment: float,
    current_sector_sentiment: float,
    consecutive_red_days: int,
    vix_trend: str,
) -> tuple[bool, str]:
    """Return (True, '') if position qualifies for overnight swing hold, (False, reason) otherwise.

    Pure — no Alpaca calls, no file I/O.
    Asymmetric design: fail-fast on thesis-break signals only.
    """
    # Universal veto 1: CRISIS — no overnight exposure under any circumstances
    if regime == "CRISIS":
        return False, "CRISIS regime — no overnight exposure"

    # Universal veto 2: earnings TOMORROW — binary risk
    if earnings_tomorrow:
        return False, "earnings tomorrow — overnight binary risk"

    # Any non-swing-eligible regime — block new swing promotions
    if regime not in regimes.SWING_ELIGIBLE:
        return False, f"{regime} regime — no new swing promotions"

    # PTJ rule: don't hold losers overnight — threshold varies by regime
    ptj_threshold = _ptj_threshold(regime)
    if current_pnl_pct < ptj_threshold:
        return False, f"PTJ rule: P&L {current_pnl_pct:.1f}% < {ptj_threshold}% ({regime})"

    # NORMAL: veto on sector reversal (current < -20 AND entry was > +10)
    if regime == "NORMAL":
        if current_sector_sentiment < -20 and entry_sector_sentiment > 10:
            return False, (
                f"sector reversed: current {current_sector_sentiment:.0f} < -20, "
                f"entry was {entry_sector_sentiment:.0f}"
            )

    # OPPORTUNITY: thesis requires sector health — veto if sector went negative
    if regime == "OPPORTUNITY":
        if current_sector_sentiment < 0 and entry_sector_sentiment > 10:
            return False, (
                f"OPPORTUNITY thesis broken: sector {current_sector_sentiment:.0f} < 0 "
                f"(was {entry_sector_sentiment:.0f} at entry)"
            )

    # CRASH_RECOVERY: veto if recovery stalled (3+ consecutive red days)
    if regime == "CRASH_RECOVERY":
        if consecutive_red_days >= 3:
            return False, f"CRASH_RECOVERY: recovery stalled ({consecutive_red_days} consecutive red days)"

    return True, ""


def _ptj_threshold(regime: str) -> float:
    """Return PTJ rule P&L floor for overnight hold by regime."""
    if regime == "BULL_TREND":
        return config.SWING_MIN_PNL_PCT_BULL
    if regime == "CAPITULATION":
        return config.SWING_MIN_PNL_PCT_CAPITULATION
    if regime == "CRASH_RECOVERY":
        return config.SWING_MIN_PNL_PCT_CRASH
    return config.SWING_MIN_PNL_PCT   # NORMAL, OPPORTUNITY (and any unrecognized regime): -2.0%


def evaluate_eod_holds(load_swing_positions, _save_swing_positions, _SWING_LOCK) -> list[str]:
    """
    Called at 3:50 PM ET. Evaluate open day-trade positions for overnight swing hold.
    Accepts persistence callables to avoid circular import with swing_manager.
    Returns list of promoted symbols.
    """
    from trading.journal import load_open_positions
    from macro.regime import load_macro_regime
    import sectors

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

    from broker.alpaca_client import AlpacaClient
    alpaca = AlpacaClient()

    # Batch-fetch prices for all open positions before the loop to avoid
    # serial REST calls at the time-critical 3:50 PM window.
    symbols = list(open_positions.keys())
    prices: dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        future_to_sym = {pool.submit(alpaca.get_latest_price, sym): sym for sym in symbols}
        for fut in as_completed(future_to_sym):
            sym = future_to_sym[fut]
            try:
                prices[sym] = fut.result()
            except Exception as exc:
                logger.warning("eod_holds: price fetch failed for %s: %s", sym, exc)

    for symbol, pos in open_positions.items():
        try:
            entry = pos["entry_price"]
            current_price = prices.get(symbol)
            if current_price is None:
                logger.info("SWING SKIP %s — price unavailable", symbol)
                continue
            pnl_pct = (current_price - entry) / entry * 100

            earnings_tomorrow = is_earnings_imminent(symbol, days=1)
            sector = sectors.get_sector(symbol)
            current_sector_score = float(sector_sentiment.get(sector, 0))
            entry_sector_score = float(pos.get("entry_sector_sentiment", 0))

            qualifies, reason = qualifies_for_eod_hold(
                symbol=symbol,
                regime=regime,
                earnings_tomorrow=earnings_tomorrow,
                current_pnl_pct=pnl_pct,
                entry_sector_sentiment=entry_sector_score,
                current_sector_sentiment=current_sector_score,
                consecutive_red_days=regime_info.get("consecutive_red_days", 0),
                vix_trend=regime_info.get("vix_trend", "stable"),
            )
            if not qualifies:
                logger.info(f"SWING SKIP {symbol}: {reason}")
                continue

            from utils.regime_params import get_swing_params
            sp = get_swing_params(regime)
            tp1_pct = sp.tp1_pct
            tp2_pct = sp.tp2_pct
            sl_pct  = sp.sl_pct
            capitulation = regime == "CAPITULATION"

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

            if not _promote_to_swing(symbol, swing_pos, pnl_pct):
                continue

            promoted.append(symbol)
            logger.info(
                f"SWING PROMOTE {symbol}: P&L={pnl_pct:+.2f}% | "
                f"TP1={tp1_pct}% TP2={tp2_pct}% SL={sl_pct}% | {regime} regime"
            )
        except Exception as exc:
            logger.warning(f"evaluate_eod_holds: error for {symbol}: {exc}")

    return promoted
