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
from indicators.values import IndicatorValues
from signals.time_gate import is_entry_allowed
from signals.relative_strength import rs_rank, passes_rs_filter
from signals.correlation_guard import passes_correlation_guard
from signals.mtf_confirm import passes_mtf_filter
from signals.vwap_guard import passes_vwap_guard


@dataclass(frozen=True)
class GateResult:
    passed: bool
    reason: str = ""
    flags: tuple[str, ...] = ()


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
) -> GateResult:
    """
    Runs all entry gates in priority order. Returns on first hard-gate failure.
    Called for every candidate — no order is placed without passing this.

    open_positions / daily_closed are caller-supplied snapshots; no disk I/O here.
    """
    flags: list[str] = []

    # ── Hard gates ────────────────────────────────────────────────────────────

    # 1. Time gate — block first 30 min and last 35 min
    if not is_entry_allowed(now):
        return GateResult(False, "outside entry time window")

    # 2. Per-cycle entry cap
    if entries_this_cycle >= config.MAX_ENTRIES_PER_CYCLE:
        return GateResult(False, "per-cycle entry cap reached")

    # 3. Already closed today or already open
    if symbol in daily_closed:
        return GateResult(False, "already closed today")
    if symbol in open_positions:
        return GateResult(False, "position already open")

    # 4. Earnings imminent — imported lazily to avoid circular import
    from earnings_calendar import is_earnings_imminent
    earnings_days = 1 if mode not in ("HYBRID", "SHORT_TERM") else 2
    if mode not in ("CAPITULATION", "PEAD") and is_earnings_imminent(symbol, days=earnings_days):
        return GateResult(False, f"earnings within {earnings_days} day(s)")

    # 5. Event risk window
    from economic_calendar import is_event_risk_window
    event_risk, event_name = is_event_risk_window(now)
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
    if iv.rsi > config.RSI_HARD_SKIP_THRESHOLD:
        return GateResult(False, f"RSI overbought hard skip (RSI={iv.rsi:.1f} > {config.RSI_HARD_SKIP_THRESHOLD})")

    # 11. Stochastic overbought hard veto
    if iv.stoch_d > config.STOCH_HARD_VETO_THRESHOLD:
        return GateResult(False, f"Stochastic hard veto (%D={iv.stoch_d:.1f} > {config.STOCH_HARD_VETO_THRESHOLD})")

    # 12. Gap-up skip — prev_close sourced from IndicatorValues.prev_close (daily T-1)
    if iv.prev_close > 0 and iv.current_price > iv.prev_close * (1 + config.GAP_SKIP_PCT / 100):
        gap_pct = (iv.current_price / iv.prev_close - 1) * 100
        return GateResult(False, f"gap-up skip ({gap_pct:.1f}% > {config.GAP_SKIP_PCT}%)")

    # ── Soft gates (pass through, flag for Claude) ────────────────────────────

    # 10. Relative strength
    if symbol_ret5d != 0.0:
        ratio = rs_rank(symbol, spy_ret5d, symbol_ret5d)
        if not passes_rs_filter(ratio):
            flags.append(f"rs_weak(ratio={ratio:.2f})")

    # 11. MTF confirmation
    try:
        if bars_15min is not None and not passes_mtf_filter(bars_15min):
            flags.append("mtf_not_confirmed")
    except Exception:
        flags.append("mtf_unavailable")

    # 12. Correlation guard
    if not passes_correlation_guard(symbol, open_positions):
        flags.append("sector_limit_soft")

    return GateResult(True, "", tuple(flags))
