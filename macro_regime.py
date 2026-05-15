"""
9-state macro regime classifier — thin orchestrator.
Computation: macro.health_scorer, macro.regime_classifier, macro.fred_cache, macro.yahoo_fetcher.
Runs as the 8:55 AM scheduler job; writes macro_regime.json.
"""
import logging
from datetime import datetime
import config
from utils.io_utils import load_json, save_json
from macro.health_scorer import compute_macro_health_score, _score_tech
from macro.regime_classifier import classify_regime, _count_consecutive_direction, _is_choppy
from macro.fred_cache import refresh_fred_cache, load_fred_modifier  # noqa: F401 (re-exported)
from macro.yahoo_fetcher import (
    ret5d, load_bot_net_pnl, is_post_crisis_recovery, evaluate_lockout_recovery,
    fetch_closes_for_regime, build_signals_dict,
)

from utils.constants import ET

logger = logging.getLogger(__name__)

_VALID_REGIMES = {
    "BULL_TREND", "BULL_TREND_PARABOLIC", "NORMAL", "CHOPPY",
    "DEFENSIVE", "OPPORTUNITY", "CAPITULATION", "CRISIS", "CRASH_RECOVERY",
}

_TICKERS = {
    "vix":  "%5EVIX", "spy": "SPY", "wti": "CL%3DF", "gold": "GC%3DF",
    "dxy":  "DX-Y.NYB", "tnx": "%5ETNX", "soxx": "SOXX", "qqq": "QQQ",
    **config.MACRO_BREADTH_TICKERS,
}


def _normal_fallback(now: datetime) -> dict:
    return {
        "regime": "NORMAL", "macro_health_score": 0,
        "sector_scores": {"AI_SEMI": 0, "BROAD_TECH": 0, "CYBERSECURITY": 0},
        "consecutive_green_days": 0, "consecutive_red_days": 0,
        "spy_5day_sign_changes": 0, "vix_current": 0.0, "vix_90d_avg": 0.0,
        "vix_trend": "unknown", "spy_5day_return": 0.0,
        "flash_crash_active": False, "flash_crash_until": None,
        "flash_crash_candidate": None, "event_risk_today": False,
        "event_risk_details": "", "capitulation_candidates": [],
        "timestamp": now.isoformat(), "reason": "fetch_failed",
    }


def check_flash_crash(spy_current: float, spy_prev_close: float) -> bool:
    """Return True if SPY has dropped ≥ FLASH_CRASH_SPY_DROP_PCT from previous close intraday."""
    if spy_prev_close <= 0:
        return False
    return (spy_current - spy_prev_close) / spy_prev_close * 100 <= config.FLASH_CRASH_SPY_DROP_PCT


def _sector_scores(soxx_ret5d: float, qqq_ret5d: float) -> dict:
    soxx = _score_tech(soxx_ret5d, 2.0, -1.0) * 10
    qqq  = _score_tech(qqq_ret5d,  1.5, -1.0) * 10
    return {"AI_SEMI": soxx, "BROAD_TECH": qqq, "CYBERSECURITY": 0.8 * qqq}


def fetch_macro_regime() -> dict:
    """Fetch Yahoo signals → classify regime → write macro_regime.json. Falls back to NORMAL."""
    now = datetime.now(ET)
    try:
        closes = fetch_closes_for_regime(_TICKERS)
        vix_closes = closes.get("vix", [])
        spy_closes = closes.get("spy", [])
        vix3m_closes = closes.get("vix3m", [])

        vix_current   = vix_closes[-1] if vix_closes else 20.0
        vix3m_current = vix3m_closes[-1] if vix3m_closes else 0.0
        lookback      = min(90, len(vix_closes) - 1)
        vix_90d_avg   = sum(vix_closes[-(lookback + 1):-1]) / lookback if lookback > 0 else vix_current
        vix_trend     = "declining" if len(vix_closes) >= 3 and vix_closes[-1] < vix_closes[-3] else "rising"

        spy_5day_return   = ret5d(spy_closes)
        sign_changes      = 0
        if len(spy_closes) >= 6:
            diffs = [spy_closes[i] - spy_closes[i - 1] for i in range(max(len(spy_closes) - 5, 1), len(spy_closes))]
            sign_changes = sum(1 for i in range(1, len(diffs)) if (diffs[i] > 0) != (diffs[i - 1] > 0))
        consecutive_green = _count_consecutive_direction(spy_closes, "green")
        consecutive_red   = _count_consecutive_direction(spy_closes, "red")

        signals      = build_signals_dict(closes, vix_current, vix3m_current)
        macro_health = compute_macro_health_score(signals, load_fred_modifier())
        ss           = _sector_scores(ret5d(closes.get("soxx", [])), ret5d(closes.get("qqq", [])))

        regime = classify_regime(
            macro_health_score=macro_health, consecutive_green_days=consecutive_green,
            consecutive_red_days=consecutive_red, vix_current=vix_current,
            spy_5day_return=spy_5day_return, is_choppy=_is_choppy(spy_closes),
            ai_semi_sector_score=ss["AI_SEMI"], bot_net_pnl_pct=load_bot_net_pnl(),
            post_crisis_recovery=is_post_crisis_recovery(),
        )

        cap_candidates: list[str] = []
        try:
            meta = load_json(config.RESEARCH_REPORT_FILE, {}).get("__meta__", {})
            cap_candidates = meta.get("capitulation_candidates", [])
            if cap_candidates and regime in ("DEFENSIVE", "NORMAL", "CHOPPY"):
                regime = "CAPITULATION"
            cap_candidates = cap_candidates  # satisfy linter
        except Exception:
            cap_candidates = []

        flash_active, flash_until = False, None
        try:
            prev = load_json(config.MACRO_REGIME_FILE, {})
            if prev:
                flash_active, flash_until = evaluate_lockout_recovery(
                    prev.get("flash_crash_active", False), prev.get("flash_crash_until"),
                    vix_current, spy_5day_return, now,
                )
                if flash_active:
                    regime = "CRISIS"
                    logger.info("Flash crash lockout still active — CRISIS regime carried forward")
                elif prev.get("flash_crash_active") and not flash_active:
                    logger.info(f"Flash crash lockout auto-cleared — VIX={vix_current:.1f}, SPY5d={spy_5day_return:+.2f}%")
        except Exception:
            pass

        event_risk, event_details = False, ""
        try:
            from macro.economic_calendar import is_event_risk_window
            event_risk, event_details = is_event_risk_window(now)
        except Exception:
            pass

        result = {
            "regime": regime, "macro_health_score": macro_health, "sector_scores": ss,
            "consecutive_green_days": consecutive_green, "consecutive_red_days": consecutive_red,
            "spy_5day_sign_changes": sign_changes,
            "vix_current": round(vix_current, 2), "vix_90d_avg": round(vix_90d_avg, 2),
            "vix_trend": vix_trend, "spy_5day_return": round(spy_5day_return, 2),
            "hyg_ret5d": round(ret5d(closes.get("hyg", [])), 2),
            "iwm_ret5d": round(ret5d(closes.get("iwm", [])), 2),
            "vix3m": round(vix3m_current, 2),
            "flash_crash_active": flash_active, "flash_crash_until": flash_until,
            "flash_crash_candidate": None, "event_risk_today": event_risk,
            "event_risk_details": event_details, "capitulation_candidates": cap_candidates,
            "timestamp": now.isoformat(),
            "reason": (
                f"{consecutive_green}g/{consecutive_red}r consecutive days, "
                f"macro_health={macro_health:+d}, VIX={vix_current:.1f} ({vix_trend})"
            ),
        }
    except Exception as exc:
        logger.warning(f"macro_regime fetch failed: {exc} — defaulting to NORMAL")
        result = _normal_fallback(now)

    save_json(config.MACRO_REGIME_FILE, result)
    logger.info(
        f"Macro regime: {result['regime']} | health={result['macro_health_score']:+d} | "
        f"VIX={result['vix_current']:.1f} | SPY5d={result['spy_5day_return']:+.2f}% | "
        f"green={result['consecutive_green_days']} red={result['consecutive_red_days']}"
    )
    return result


def load_macro_regime() -> dict:
    """Return today's cached regime. Defaults to NORMAL if missing or unreadable."""
    try:
        data = load_json(config.MACRO_REGIME_FILE, {})
        if data.get("regime") in _VALID_REGIMES:
            return data
    except Exception:
        pass
    return _normal_fallback(datetime.now(ET))


def load_trading_params() -> dict:
    """Return learning-agent-adjusted trading params, falling back to config defaults."""
    defaults = {
        "entry_start_hour": config.ENTRY_START_HOUR, "entry_end_hour": config.ENTRY_END_HOUR,
        "rsi_buy_max": config.RSI_BUY_MAX, "rsi_buy_max_high_vol": config.RSI_BUY_MAX_HIGH_VOL,
        "vwap_slope_min": config.VWAP_SLOPE_MIN, "high_vol_position_mult": config.HIGH_VOL_POSITION_MULT,
        "stop_loss_pct": config.STOP_LOSS_PCT, "take_profit_pct": config.TAKE_PROFIT_PCT,
    }
    try:
        saved = load_json(config.TRADING_PARAMS_FILE, {})
        if saved:
            defaults.update(saved)
    except Exception as exc:
        logger.warning("load_trading_params: failed to read %s — using defaults (%s)", config.TRADING_PARAMS_FILE, exc)
    return defaults


if __name__ == "__main__":
    import logging as _l
    _l.basicConfig(level=_l.INFO)
    fetch_macro_regime()
