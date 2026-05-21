"""
Pure T2 gate logic — no I/O, no Alpaca calls.
All 11 gates must pass for a T2 addon to trigger.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import config
import calendars.earnings as _earnings_mod


@dataclass(frozen=True)
class T2GateResult:
    passed: bool
    reason: str = ""


def check_tranche2_gates(
    symbol: str,
    iv,
    pos: dict,
    now: datetime,
    regime: str,
    sentiment_score: float,
    sector_etf_chg: float,
    major_event_today: bool,
) -> T2GateResult:
    """Return T2GateResult(True) only when all 11 gates pass.

    Gates run in fail-fast order: structural first, signal quality second,
    thesis-integrity last.
    """
    # Gate 1: idempotency sentinel
    if pos.get("tranche_2_triggered"):
        return T2GateResult(False, "already triggered")

    # Gate 2: time cutoff
    if now.hour >= config.TRANCHE2_CUTOFF_HOUR:
        return T2GateResult(False, f"time cutoff — past {config.TRANCHE2_CUTOFF_HOUR}:00 ET")

    # Gate 3: adverse regime
    from utils.regime_params import get_t2_params
    if get_t2_params(regime).cutoff_hour == 0:
        return T2GateResult(False, f"regime={regime} blocks T2")

    # Gate 4: major macro event today
    if major_event_today:
        return T2GateResult(False, "major event today — no T2 on FOMC/CPI/NFP days")

    # Gate 5: minimum hold time
    try:
        from datetime import datetime as _dt
        entry_dt = _dt.fromisoformat(pos["entry_time"])
        elapsed_min = (now - entry_dt).total_seconds() / 60
    except Exception:
        elapsed_min = 0
    if elapsed_min < config.TRANCHE2_MIN_HOLD_MIN:
        return T2GateResult(False, f"hold time {elapsed_min:.0f}min < {config.TRANCHE2_MIN_HOLD_MIN}min required")

    # Gate 6: price drop window (4–7% below T1 entry)
    t1_entry = pos.get("tranche_1_entry", pos.get("entry_price", 0))
    if t1_entry <= 0:
        return T2GateResult(False, "invalid T1 entry price")
    drop_pct = (t1_entry - iv.current_price) / t1_entry * 100
    if drop_pct < config.TRANCHE2_DROP_MIN_PCT:
        return T2GateResult(False, f"drop {drop_pct:.1f}% below minimum {config.TRANCHE2_DROP_MIN_PCT}%")
    if drop_pct > config.TRANCHE2_DROP_MAX_PCT:
        return T2GateResult(False, f"drop {drop_pct:.1f}% exceeds maximum {config.TRANCHE2_DROP_MAX_PCT}% — likely breakdown")

    # Gate 7: RSI oversold confirmation
    if iv.rsi > config.TRANCHE2_RSI_MAX:
        return T2GateResult(False, f"RSI {iv.rsi:.1f} > max {config.TRANCHE2_RSI_MAX} — not oversold")

    # Gate 8: composite health
    if iv.composite_score < config.TRANCHE2_COMPOSITE_MIN:
        return T2GateResult(False, f"composite {iv.composite_score:.1f} < min {config.TRANCHE2_COMPOSITE_MIN}")

    # Gate 9: earnings imminent
    if _earnings_mod.is_earnings_imminent(symbol, days=1):
        return T2GateResult(False, "earnings imminent — no T2 add")

    # Gate 10: sentiment floor
    if sentiment_score < config.TRANCHE2_MIN_SENTIMENT:
        return T2GateResult(False, f"sentiment {sentiment_score:.0f} < min {config.TRANCHE2_MIN_SENTIMENT}")

    # Gate 11: sector health
    if sector_etf_chg < config.TRANCHE2_SECTOR_WEAK_PCT:
        return T2GateResult(False, f"sector ETF down {sector_etf_chg:.1f}% — sector leading the drop")

    return T2GateResult(True)
