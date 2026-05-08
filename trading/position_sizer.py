"""
Pure position-sizing computation — no Alpaca calls, no file I/O.
All thresholds read from config.
"""
from __future__ import annotations

import logging

import config
from signals.sentiment_sizer import sentiment_size_multiplier
from trading.journal import count_todays_losses

logger = logging.getLogger(__name__)

# Mode-level max portfolio-fraction caps (mirrors _MODE_MAX_PCT in trading_agent)
_MODE_MAX_PCT: dict[str, float] = {
    "DAY_TRADE":  0.05,
    "HYBRID":     0.03,
    "SHORT_TERM": 0.025,
    "PEAD":       0.025,
}


def _progressive_loss_multiplier(loss_count: int) -> float:
    """Returns size multiplier: max(PROGRESSIVE_LOSS_FLOOR, 1 - STEP * losses)."""
    multiplier = 1.0 - config.PROGRESSIVE_LOSS_STEP * loss_count
    return max(config.PROGRESSIVE_LOSS_FLOOR, multiplier)


def compute_position_size_for_mode(
    portfolio_value: float,
    price: float,
    atr: float,
    mode: str,
    bot_herding_score: int,
    squeeze_setup: bool,
) -> int:
    """
    Risk-based sizing capped by mode's max % of portfolio.

    Applies three successive multipliers:
    1. Bot-herding reduction (when herding is elevated but not a squeeze)
    2. Progressive loss reduction (after each intraday loss)
    3. Returns final integer share count (minimum 1).
    """
    risk_amount    = portfolio_value * config.RISK_PER_TRADE_PCT
    shares_by_risk = risk_amount / (atr if atr > 0 else price * 0.02)
    max_pct        = _MODE_MAX_PCT.get(mode, config.MAX_POSITION_PCT)
    shares_by_pct  = (portfolio_value * max_pct) / price
    qty = max(1.0, round(min(shares_by_risk, shares_by_pct)))

    # Reduce when bot herding is elevated but no squeeze setup
    if not squeeze_setup and bot_herding_score > config.BOT_HERD_ALERT_SCORE:
        qty = max(1.0, round(qty * 0.70))

    # Scale down after each intraday loss
    loss_count = count_todays_losses()
    loss_mult  = _progressive_loss_multiplier(loss_count)
    if loss_mult < 1.0:
        logger.debug(
            "Progressive loss reduction: %d losses today, multiplier=%.2f",
            loss_count, loss_mult,
        )
    qty = max(1, int(qty * loss_mult))
    return int(qty)


def apply_regime_multiplier(qty: int, regime_mult: float) -> int:
    """Apply a regime-level size multiplier (e.g. 0.5 in DEFENSIVE/CHOPPY)."""
    return max(1, int(round(qty * regime_mult)))


def apply_sentiment_multiplier(qty: int, sentiment_score: int | float) -> int:
    """Apply sentiment-based sizing multiplier from research agent score."""
    mult = sentiment_size_multiplier(int(sentiment_score))
    return max(1, int(round(qty * mult)))


def apply_post_event_multiplier(qty: int, is_post_event: bool) -> int:
    """Reduce position size on the day after a major macro event."""
    if is_post_event:
        qty = max(1, int(round(qty * config.POST_EVENT_POSITION_MULT)))
    return qty
