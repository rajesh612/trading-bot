"""
Pure T2 gate logic — no I/O, no Alpaca calls.
All 11 gates must pass for a T2 addon to trigger.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import config
import calendars.earnings as _earnings_mod
from utils.regime_params import get_t2_params


@dataclass(frozen=True)
class T2GateResult:
    passed: bool
    reason: str = ""


@dataclass(frozen=True)
class T2TriggerResult:
    trigger: bool
    reason: str = ""


def evaluate_t2_trigger(
    symbol: str,
    position: dict,
    current_price: float,
    composite_score: float,
    sector_etf_chg: float | None,
) -> T2TriggerResult:
    """Simplified T2 entry-point for the 4 new safety pre-checks.

    These run before the full 11-gate check and protect against:
    P1-1: None sector_etf_chg (defaults to -2.0)
    P0-2: Corrupted entry_time (explicit fail)
    P0-1: T1 underwater (no T2 on losing T1)
    P1-6: T2 drop >= T1 SL (SL fires before T2 helps)
    """
    # Fix P1-1: None sector_etf_chg → assume weak
    if sector_etf_chg is None:
        sector_etf_chg = -2.0

    # Fix P0-2: entry_time parse guard
    timestamp_str = position.get("timestamp") or position.get("entry_time", "")
    try:
        from zoneinfo import ZoneInfo
        entry_dt = datetime.fromisoformat(timestamp_str)
        if entry_dt.tzinfo is None:
            entry_dt = entry_dt.replace(tzinfo=ZoneInfo("America/New_York"))
    except Exception:
        return T2TriggerResult(False, "entry_time missing or invalid — cannot evaluate hold gate")

    # Fix P1-6: T2 drop must not exceed T1 SL (SL fires first — check before underwater)
    t1_entry = position.get("tranche_1_entry", position.get("entry_price", 0))
    if t1_entry > 0:
        price_drop_pct = (t1_entry - current_price) / t1_entry * 100
        t1_sl_pct = position.get("stop_loss_pct", config.STOP_LOSS_PCT)
        if price_drop_pct >= t1_sl_pct:
            return T2TriggerResult(False, f"T2 drop {price_drop_pct:.1f}% >= T1 SL {t1_sl_pct:.1f}% — SL fires before T2 adds value")

    # Fix P0-1: T1 must be profitable before T2 fires
    if t1_entry > 0:
        t1_pnl_pct = (current_price - t1_entry) / t1_entry * 100
        if t1_pnl_pct < 0:
            return T2TriggerResult(False, f"T1 underwater ({t1_pnl_pct:+.1f}%) — no T2 on losing position")

    # Sector ETF gate
    if sector_etf_chg < config.TRANCHE2_SECTOR_WEAK_PCT:
        return T2TriggerResult(False, f"sector ETF {sector_etf_chg:.1f}% — sector leading the drop")

    return T2TriggerResult(True, "pre-checks passed")


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
    # Fix P1-1: None sector_etf_chg → assume weak
    if sector_etf_chg is None:
        sector_etf_chg = -2.0

    # Gate 1: idempotency sentinel
    if pos.get("tranche_2_triggered"):
        return T2GateResult(False, "already triggered")

    # Gate 2: time cutoff
    if now.hour >= config.TRANCHE2_CUTOFF_HOUR:
        return T2GateResult(False, f"time cutoff — past {config.TRANCHE2_CUTOFF_HOUR}:00 ET")

    # Gate 3: adverse regime
    # cutoff_hour=0 is the sentinel value for regimes that fully block T2 (CRISIS, CHOPPY)
    if get_t2_params(regime).cutoff_hour == 0:
        return T2GateResult(False, f"regime={regime} blocks T2")

    # Gate 4: major macro event today
    if major_event_today:
        return T2GateResult(False, "major event today — no T2 on FOMC/CPI/NFP days")

    # Gate 5: minimum hold time — explicit fail on bad entry_time
    entry_time_str = pos.get("entry_time", "")
    try:
        entry_dt = datetime.fromisoformat(entry_time_str)
        elapsed_min = (now - entry_dt).total_seconds() / 60
    except Exception:
        return T2GateResult(False, "entry_time missing or invalid — cannot evaluate hold gate")
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
