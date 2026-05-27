"""
Unified entry gate runner. All BUY candidates pass through check_all_gates()
before execution — no order bypasses these checks for any reason.

Hard gates: block the symbol and return immediately (no Claude call).
Soft gates: pass through but append a flag so Claude sees the warning.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

import config
import macro.economic_calendar as ecc
from indicators.values import IndicatorValues
from signals.time_gate import is_entry_allowed
from signals.relative_strength import rs_rank, passes_rs_filter
from signals.correlation_guard import passes_correlation_guard
from signals.mtf_confirm import passes_mtf_filter
from signals.vwap_guard import passes_vwap_guard
from signals.volume_guard import is_volume_weak
from calendars.earnings import is_earnings_imminent
from utils import regimes
from sectors import get_sub_sector_etf
from utils.regime_params import get_entry_params, get_gate_params
from utils.ticker_quality import TickerQuality


@dataclass(frozen=True)
class GateResult:
    passed: bool
    reason: str = ""
    flags: tuple[str, ...] = ()


def effective_composite_gate(
    iv: IndicatorValues,
    regime: str,
    params: dict | None = None,
    staleness_guard=None,   # StalenessGuard | None
) -> int:
    """Single source of truth for composite entry threshold by regime and signal context.

    Priority: HIGH_VOL safety → squeeze setup → bot herding → CHOPPY → BULL_TREND → NORMAL.
    When params is provided, NORMAL regime reads composite_min_normal from params
    (written by learning agent on zero-trade days).
    When staleness_guard is provided, its composite_penalty is added to the base threshold.
    """
    from utils.data_health import FRESH_GUARD
    guard = staleness_guard or FRESH_GUARD

    if regime in regimes.HIGH_VOL_GATES:
        base = config.COMPOSITE_MIN_HIGH_VOL   # 55
    elif iv.squeeze_setup:
        base = config.SQUEEZE_COMPOSITE_MIN    # strong squeeze signal lowers bar
    elif iv.bot_herding_score >= config.BOT_HERD_ALERT_SCORE:
        base = config.HERD_ENTRY_GATE          # 25
    else:
        ep = get_entry_params(regime)
        # NORMAL regime: honor learning-agent-adjusted floor if present
        if regime == "NORMAL":
            base = (params or {}).get("composite_min_normal", ep.composite_min)
        else:
            base = ep.composite_min

    return base + guard.composite_penalty



def check_all_gates(
    symbol: str,
    iv: IndicatorValues,
    open_positions: dict,
    daily_closed: set[str],
    bars_15min: pd.DataFrame | None,
    now: datetime,
    entries_this_cycle: int,
    spy_ret5d: float = 0.0,
    symbol_ret5d: float = 0.0,
    mode: str = "DAY_TRADE",
    rsi_max: float = config.RSI_BUY_MAX,
    sector_today: dict | None = None,
    symbol_sector_etf: str | None = None,
    advance_decline_ratio: float = 1.0,
    sl_blacklisted: bool = False,
    sector_sentiment_score: float = 0.0,   # NLP sector sentiment from research __meta__
    sentiment_score: float = 0.0,          # symbol-level news sentiment score
    swing_eligible: bool = False,          # True when regime is in SWING_ELIGIBLE
    major_event_today: bool = False,       # True if FOMC/CPI/NFP scheduled today
    regime: str = "NORMAL",               # Current macro regime for hard cutoff lookup
    quality: TickerQuality | None = None,
) -> GateResult:
    """
    Runs all entry gates in priority order. Returns on first hard-gate failure.
    Called for every candidate — no order is placed without passing this.

    open_positions / daily_closed are caller-supplied snapshots; no disk I/O here.
    """
    flags: list[str] = []
    gp = get_gate_params(regime, quality.tier if quality else "MEDIUM")

    # ── Hard gates ────────────────────────────────────────────────────────────

    # 0b. Price cap — hard block expensive stocks (>$750 avg -$55/trade at 28% win rate)
    if config.MAX_ENTRY_PRICE and iv.current_price > config.MAX_ENTRY_PRICE:
        return GateResult(False, f"price ${iv.current_price:.0f} > MAX_ENTRY_PRICE ${config.MAX_ENTRY_PRICE:.0f}")

    # 0c. Major economic event today — no new swing entries (overnight thesis invalidation risk).
    # CATALYST mode is exempt: it is specifically designed for post-event plays.
    if major_event_today and swing_eligible and mode != "CATALYST":
        return GateResult(False, "major economic event today — no new swing-eligible entries")

    # 1. Time gate — block first 30 min, last 35 min, and after per-regime hard cutoff
    if not is_entry_allowed(now, regime):
        return GateResult(False, "outside entry time window")

    # 1b. Late-day composite gate — after 2 PM require higher conviction
    if now.hour >= config.ENTRY_LATE_HOUR and iv.composite_score < config.ENTRY_LATE_COMPOSITE_MIN:
        return GateResult(
            False,
            f"late entry ({now.strftime('%H:%M')} ET): composite {iv.composite_score:.0f} < {config.ENTRY_LATE_COMPOSITE_MIN} required after {config.ENTRY_LATE_HOUR}:00",
        )

    # 1c. SL blacklist — block re-entry for symbols that hit SL recently
    if sl_blacklisted:
        return GateResult(False, f"{symbol} is SL-blacklisted (hit stop-loss recently)")

    # 2. Per-cycle entry cap
    if entries_this_cycle >= config.MAX_ENTRIES_PER_CYCLE:
        return GateResult(False, "per-cycle entry cap reached")

    # 3. Already closed today or already open
    if symbol in daily_closed:
        return GateResult(False, "already closed today")
    if symbol in open_positions:
        return GateResult(False, "position already open")

    # 4. Earnings imminent — PEAD is exempt (post-earnings drift plays require earnings)
    if mode != "PEAD" and is_earnings_imminent(symbol, days=1):
        return GateResult(False, "earnings within 1 day")

    # 5. Event risk window
    event_risk, event_name = ecc.is_event_risk_window(now)
    if event_risk:
        return GateResult(False, f"event risk window: {event_name}")

    # 6. Stop-hunt zone
    if iv.stop_hunt_zone:
        return GateResult(False, "stop-hunt zone (round number or 5d hi/lo proximity)")

    # 7. VWAP overextension
    if not passes_vwap_guard(iv):
        vwap_dev = (iv.current_price - iv.vwap_std2_upper) / iv.vwap_std2_upper * 100
        return GateResult(False, f"price above VWAP+2σ ({vwap_dev:+.2f}% overextended)")

    # 8. Parabolic move
    if config.PARABOLIC_ENTRY_BLOCK and iv.parabolic:
        return GateResult(False, "parabolic move — hard veto (mean-reversion risk)")

    # 9. Bot herding (hard unless squeeze overrides)
    if not iv.squeeze_setup:
        if iv.bot_herding_score >= config.BOT_HERD_HIGH_SCORE:
            return GateResult(False, f"bot herding too high ({iv.bot_herding_score})")
        if iv.momentum_exhaustion and iv.bot_herding_score >= config.BOT_HERD_ALERT_SCORE:
            return GateResult(False, f"momentum exhaustion + herding ({iv.bot_herding_score})")

    # 10. RSI overbought hard skip — saves Claude tokens on conditions that will HOLD anyway
    # Exempted for PEAD: earnings-momentum stocks legitimately run above normal RSI bounds
    if mode != "PEAD" and iv.rsi > rsi_max:
        return GateResult(False, f"RSI overbought hard skip (RSI={iv.rsi:.1f} > {rsi_max:.0f})")

    # 11. Stochastic overbought — hard veto above stoch_hard_veto, soft flag when elevated
    if iv.stoch_d > gp.stoch_hard_veto:
        return GateResult(False, f"Stochastic hard veto (%D={iv.stoch_d:.1f} > {gp.stoch_hard_veto})")
    if iv.stoch_d >= gp.stoch_soft_flag:
        flags.append(f"stoch_elevated(%D={iv.stoch_d:.1f})")

    # 12. Gap-up skip — exempted for PEAD (post-earnings drift stocks gap intentionally)
    if mode != "PEAD" and iv.prev_close > 0 and iv.current_price > iv.prev_close * (1 + config.GAP_SKIP_PCT / 100):
        gap_pct = (iv.current_price / iv.prev_close - 1) * 100
        return GateResult(False, f"gap-up skip ({gap_pct:.1f}% > {config.GAP_SKIP_PCT}%)")

    # ── Soft gates (pass through, flag for Claude) ────────────────────────────

    # 13. Relative strength
    if symbol_ret5d != 0.0:
        ratio = rs_rank(symbol, spy_ret5d, symbol_ret5d)
        if not passes_rs_filter(ratio):
            flags.append(f"rs_weak(ratio={ratio:.2f})")

    # 14. MTF confirmation
    try:
        if bars_15min is not None and not passes_mtf_filter(bars_15min):
            flags.append("mtf_not_confirmed")
    except Exception:
        flags.append("mtf_unavailable")

    # 15. Correlation guard
    if not passes_correlation_guard(symbol, open_positions):
        flags.append("sector_limit_soft")

    # 16. Sector ETF soft gate
    if sector_today and symbol_sector_etf:
        chg = sector_today.get(symbol_sector_etf)
        if chg is not None and chg < config.SECTOR_ETF_SOFT_DROP_PCT:
            flags.append(f"sector_weak({symbol_sector_etf}={chg:+.1f}%)")

    # 17. Relative volume — thin tape increases false-positive rate
    if iv.relative_volume < gp.rel_volume_min:
        flags.append(f"rel_vol_low({iv.relative_volume:.2f}x)")

    # 18. Opening range position — entries inside OR carry reversal risk
    if iv.or_position == "inside":
        flags.append("inside_opening_range")

    # 19. Market breadth — narrow IWM/SPY breadth signals possible bull trap
    if advance_decline_ratio < gp.breadth_weak_threshold:
        flags.append(f"breadth_weak(ad={advance_decline_ratio:.2f})")

    # 20. Sector sentiment soft flag — sector going negative signals headwind
    if sector_sentiment_score < config.SECTOR_ENTRY_MIN_SCORE:
        flags.append(f"sector_sentiment({sector_sentiment_score:.0f}<{config.SECTOR_ENTRY_MIN_SCORE})")

    # 21. News quality soft flag — swing candidates need a catalyst
    if swing_eligible and sentiment_score < config.SWING_NEWS_MIN_SCORE:
        flags.append(f"swing_news_weak({sentiment_score:.0f}<{config.SWING_NEWS_MIN_SCORE})")

    return GateResult(True, "", tuple(flags))


def is_catalyst_active(symbol: str, snap) -> bool:
    """Pure CATALYST gate (Rule 33). Returns True when event settled, sector confirms, surprise passes."""
    window = snap.post_event_window
    if window is None:
        return False
    event_dict, elapsed_min = window
    if elapsed_min < config.CATALYST_SETTLE_MINUTES:
        return False
    sub_etf = get_sub_sector_etf(symbol)
    etf_chg = snap.sector_etf_cache.get(sub_etf, 0.0)
    if abs(etf_chg) < config.CATALYST_ETF_MIN_MOVE_PCT:
        return False
    surprise = getattr(snap, "catalyst_surprise_score", 0.0)
    if surprise < config.CATALYST_MIN_SURPRISE_SCORE:
        return False
    return True


