"""
Regime classification. All functions are pure — no file I/O.
"""
import config


def _count_consecutive_direction(closes: list[float], direction: str) -> int:
    """Count consecutive green (direction='green') or red ('red') closes from the end."""
    count = 0
    for i in range(len(closes) - 1, 0, -1):
        diff = closes[i] - closes[i - 1]
        if direction == "green" and diff > 0:
            count += 1
        elif direction == "red" and diff < 0:
            count += 1
        else:
            break
    return count


def _is_choppy(closes: list[float]) -> bool:
    """3+ sign alternations in last 5 closes AND |net move| < 0.5%."""
    if len(closes) < 6:
        return False
    last5 = closes[-6:]
    diffs = [last5[i] - last5[i - 1] for i in range(1, 6)]
    sign_changes = sum(
        1 for i in range(1, len(diffs)) if (diffs[i] > 0) != (diffs[i - 1] > 0)
    )
    net_move_pct = abs(last5[-1] - last5[0]) / last5[0] * 100 if last5[0] > 0 else 0
    return sign_changes >= config.CHOPPY_MIN_SIGN_ALTS and net_move_pct < config.CHOPPY_MAX_NET_MOVE_PCT


def classify_regime(
    macro_health_score: int,
    consecutive_green_days: int,
    consecutive_red_days: int,
    vix_current: float,
    spy_5day_return: float,
    is_choppy: bool,
    ai_semi_sector_score: float,
    bot_net_pnl_pct: float,
    post_crisis_recovery: bool,
) -> str:
    """
    Priority: CRISIS > CRASH_RECOVERY > BULL_TREND > CHOPPY > OPPORTUNITY > DEFENSIVE > NORMAL
    """
    # CRISIS — hard rules from spec
    if vix_current > 35 or spy_5day_return < -10.0:
        return "CRISIS"

    # CRASH_RECOVERY — post-CRISIS VIX declining + 2+ consecutive green
    if post_crisis_recovery and consecutive_green_days >= 2:
        return "CRASH_RECOVERY"

    # BULL_TREND / PARABOLIC — handled by caller after classify (parabolic is sector-level)
    if consecutive_green_days >= config.CONSECUTIVE_BULL_DAYS and macro_health_score >= config.MACRO_HEALTH_BULL_THRESHOLD:
        return "BULL_TREND"

    # CHOPPY
    if is_choppy:
        return "CHOPPY"

    # DEFENSIVE / OPPORTUNITY — need both market signal AND bot P&L confirmation
    is_defensive_market = (
        consecutive_red_days >= config.CONSECUTIVE_BEAR_DAYS
        and macro_health_score <= config.MACRO_HEALTH_BEAR_THRESHOLD
    )
    is_defensive_performance = bot_net_pnl_pct < -1.0

    if is_defensive_market and is_defensive_performance:
        # OPPORTUNITY is a sub-state: DEFENSIVE conditions + AI/semi sector health
        if ai_semi_sector_score > 15:
            return "OPPORTUNITY"
        return "DEFENSIVE"

    # CAPITULATION — handled externally (requires OBV + drawdown from research agent)
    # Falls through to NORMAL if no other condition matches

    return "NORMAL"
