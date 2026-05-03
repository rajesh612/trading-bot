"""
9-state macro regime classifier.
Replaces vix_regime.py as the daily regime source (8:55 AM job).
"""
import json
import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

import config

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
_YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0"}

_VALID_REGIMES = {
    "BULL_TREND", "BULL_TREND_PARABOLIC", "NORMAL", "CHOPPY",
    "DEFENSIVE", "OPPORTUNITY", "CAPITULATION", "CRISIS", "CRASH_RECOVERY",
}


# ── Yahoo data helpers ────────────────────────────────────────────────────────

def _yahoo_closes(symbol: str, days: int = 120) -> list[float]:
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?interval=1d&range={days}d"
    )
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=_YAHOO_HEADERS, timeout=10)
            if resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            quotes = resp.json()["chart"]["result"][0]["indicators"]["quote"][0]
            return [c for c in quotes["close"] if c is not None]
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1)
    return []


# ── Signal scoring ────────────────────────────────────────────────────────────

def _ret5d(closes: list[float]) -> float:
    """5-day return in %."""
    if len(closes) < 6:
        return 0.0
    return (closes[-1] - closes[-6]) / closes[-6] * 100


def _score_vix(vix_closes: list[float]) -> float:
    """VIX vs 90d avg: < 90% of avg → +10, 90–120% → linear, > 120% → -10."""
    if len(vix_closes) < 10:
        return 0.0
    lookback = min(90, len(vix_closes) - 1)
    avg = sum(vix_closes[-lookback - 1:-1]) / lookback
    if avg <= 0:
        return 0.0
    ratio = vix_closes[-1] / avg
    if ratio < 0.9:
        return 10.0
    elif ratio > 1.2:
        return -10.0
    else:
        return 10.0 - (ratio - 0.9) / 0.3 * 20.0


def _score_spy(ret5d: float) -> float:
    """SPY 5-day return: > +1.5% → +10, -0.5% to +1.5% → linear, < -0.5% → -10."""
    if ret5d > 1.5:
        return 10.0
    elif ret5d >= -0.5:
        return (ret5d + 0.5) / 2.0 * 10.0 - 2.5  # -2.5 at -0.5%, +7.5 at 1.5%
    else:
        return -10.0


def _score_wti(ret5d: float) -> float:
    """WTI Oil: stable ±2% → +10, ±2–5% → 0, spike > +5% or crash < -8% → -10."""
    if abs(ret5d) <= 2.0:
        return 10.0
    elif ret5d > 5.0 or ret5d < -8.0:
        return -10.0
    else:
        return 0.0


def _score_gold(ret5d: float) -> float:
    """Gold: flat/mild → +10, surge > +2% (flight to safety) → -10."""
    if ret5d > 3.0:
        return -10.0
    elif ret5d > 2.0:
        return -5.0
    elif ret5d < -2.0:
        return +5.0  # declining gold = risk-on
    else:
        return +8.0  # flat/mild = bullish signal


def _score_dxy(ret5d: float) -> float:
    """DXY: mild/stable → +5, sharp rise → -10 (risk-off)."""
    if ret5d > 2.0:
        return -10.0
    elif ret5d > 0.5:
        return -5.0
    elif ret5d < -0.5:
        return +5.0
    else:
        return +3.0


def _score_tnx(ret5d: float) -> float:
    """10Y yield: declining/stable → +10, rapid rise → -10."""
    if ret5d > 5.0:
        return -10.0
    elif ret5d > 2.0:
        return -5.0
    elif ret5d < -2.0:
        return +10.0
    else:
        return +3.0


def _score_tech(ret5d: float, bull_thresh: float, bear_thresh: float) -> float:
    """Generic tech/sector ETF scorer."""
    if ret5d > bull_thresh:
        return +10.0
    elif ret5d < bear_thresh:
        return -10.0
    elif ret5d >= 0:
        return ret5d / bull_thresh * 10.0
    else:
        return ret5d / abs(bear_thresh) * 10.0


def compute_macro_health_score(signals: dict, fred_modifier: int = 0) -> int:
    """
    Compute macro_health_score in [-100, 100] from Yahoo signals + FRED modifier.
    signals: {vix_closes, spy_ret5d, wti_ret5d, gold_ret5d, dxy_ret5d,
              tnx_ret5d, soxx_ret5d, qqq_ret5d}
    """
    weights = {
        "vix":  0.20,
        "spy":  0.20,
        "soxx": 0.15,
        "qqq":  0.10,
        "wti":  0.10,
        "tnx":  0.09,
        "gold": 0.08,
        "dxy":  0.08,
    }

    raw_scores = {
        "vix":  _score_vix(signals.get("vix_closes", [])),
        "spy":  _score_spy(signals.get("spy_ret5d", 0.0)),
        "soxx": _score_tech(signals.get("soxx_ret5d", 0.0), 2.0, -1.0),
        "qqq":  _score_tech(signals.get("qqq_ret5d", 0.0),  1.5, -1.0),
        "wti":  _score_wti(signals.get("wti_ret5d", 0.0)),
        "tnx":  _score_tnx(signals.get("tnx_ret5d", 0.0)),
        "gold": _score_gold(signals.get("gold_ret5d", 0.0)),
        "dxy":  _score_dxy(signals.get("dxy_ret5d", 0.0)),
    }

    weighted_sum = sum(weights[k] * raw_scores[k] for k in weights)
    # weighted_sum is in [-10, +10]; scale to [-100, +100]
    score = weighted_sum * 10.0 + fred_modifier
    return int(max(-100, min(100, round(score))))


# ── Pattern detection ─────────────────────────────────────────────────────────

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


def _compute_sector_scores(soxx_ret5d: float, qqq_ret5d: float) -> dict:
    soxx_score = _score_tech(soxx_ret5d, 2.0, -1.0) * 10
    qqq_score  = _score_tech(qqq_ret5d,  1.5, -1.0) * 10
    return {
        "AI_SEMI":       soxx_score,
        "BROAD_TECH":    qqq_score,
        "CYBERSECURITY": 0.8 * qqq_score,
    }


# ── Regime classifier ────────────────────────────────────────────────────────

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
    is_defensive_market = consecutive_red_days >= config.CONSECUTIVE_BEAR_DAYS and macro_health_score <= config.MACRO_HEALTH_BEAR_THRESHOLD
    is_defensive_performance = bot_net_pnl_pct < -1.0

    if is_defensive_market and is_defensive_performance:
        # OPPORTUNITY is a sub-state: DEFENSIVE conditions + AI/semi sector health
        if ai_semi_sector_score > 15:
            return "OPPORTUNITY"
        return "DEFENSIVE"

    # CAPITULATION — handled externally (requires OBV + drawdown from research agent)
    # Falls through to NORMAL if no other condition matches

    return "NORMAL"


# ── FRED cache ────────────────────────────────────────────────────────────────

def refresh_fred_cache() -> None:
    """Fetch Fed funds, CPI, unemployment from FRED API. Skip silently if key missing."""
    if not config.FRED_API_KEY:
        logger.info("FRED_API_KEY not set — skipping FRED cache refresh.")
        return

    base = "https://api.stlouisfed.org/fred/series/observations"
    params_common = {
        "api_key": config.FRED_API_KEY,
        "file_type": "json",
        "sort_order": "desc",
        "limit": "3",
    }

    def _fetch(series_id: str) -> list[float]:
        try:
            resp = requests.get(
                base, params={"series_id": series_id, **params_common}, timeout=10
            )
            resp.raise_for_status()
            obs = resp.json().get("observations", [])
            return [float(o["value"]) for o in obs if o.get("value") not in (".", "")]
        except Exception as exc:
            logger.warning(f"FRED fetch {series_id} failed: {exc}")
            return []

    dff   = _fetch("DFF")    # Fed funds rate
    cpi   = _fetch("CPIAUCSL")  # CPI
    urate = _fetch("UNRATE")    # Unemployment rate

    # Fed direction
    if len(dff) >= 2:
        if dff[0] < dff[1]:
            fed_direction = "cutting"
        elif dff[0] > dff[1]:
            fed_direction = "hiking"
        else:
            fed_direction = "paused"
    else:
        fed_direction = "unknown"

    # CPI trend (YoY, 3-month direction)
    cpi_trend = "stable"
    if len(cpi) >= 3:
        if cpi[0] > cpi[2] * 1.01:
            cpi_trend = "rising"
        elif cpi[0] < cpi[2] * 0.99:
            cpi_trend = "falling"

    # Unemployment trend
    unemployment_rising = len(urate) >= 2 and (urate[0] - urate[1]) >= 0.3

    # Compute modifier: cutting=+5, hiking=-5, rising unemployment=-3
    fred_modifier = 0
    if fed_direction == "cutting":
        fred_modifier += 5
    elif fed_direction == "hiking":
        fred_modifier -= 5
    if unemployment_rising:
        fred_modifier -= 3

    cache = {
        "fed_funds_rate":     dff[0] if dff else None,
        "fed_direction":      fed_direction,
        "cpi_trend":          cpi_trend,
        "unemployment_rising": unemployment_rising,
        "fred_modifier":      fred_modifier,
        "updated_at":         datetime.now(ET).isoformat(),
    }
    with open(config.FRED_CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)
    logger.info(f"FRED cache updated: fed={fed_direction}, cpi={cpi_trend}, unemp_rising={unemployment_rising}, modifier={fred_modifier:+d}")


def _load_fred_modifier() -> int:
    try:
        if config.FRED_CACHE_FILE.exists():
            with open(config.FRED_CACHE_FILE) as f:
                return int(json.load(f).get("fred_modifier", 0))
    except Exception:
        pass
    return 0


# ── Flash crash ───────────────────────────────────────────────────────────────

def check_flash_crash(spy_current: float, spy_prev_close: float) -> bool:
    """Return True if SPY has dropped ≥ 2.5% from previous close intraday."""
    if spy_prev_close <= 0:
        return False
    return (spy_current - spy_prev_close) / spy_prev_close * 100 <= -2.5


def _load_bot_net_pnl() -> float:
    """Load recent P&L from trade journal; swallows all exceptions → 0.0."""
    try:
        if not config.TRADE_JOURNAL_FILE.exists():
            return 0.0
        with open(config.TRADE_JOURNAL_FILE) as f:
            journal = json.load(f)
        # Last 3 days of P&L
        cutoff = datetime.now(ET) - timedelta(days=3)
        recent = [
            t["pnl_dollar"] for t in journal
            if t.get("type") == "CLOSE"
            and t.get("pnl_dollar") is not None
            and t.get("timestamp", "") > cutoff.isoformat()[:10]
        ]
        if not recent:
            return 0.0
        total = sum(recent)
        # Approximate portfolio value for pct calculation
        try:
            from alpaca_client import AlpacaClient
            portfolio = AlpacaClient().get_portfolio_value()
        except Exception:
            portfolio = 100_000.0
        return (total / portfolio) * 100 if portfolio > 0 else 0.0
    except Exception:
        return 0.0


def _is_post_crisis_recovery() -> bool:
    """Check if VIX peaked above 35 in last 5 days and is now declining."""
    try:
        vix_closes = _yahoo_closes("%5EVIX", days=10)
        if len(vix_closes) < 4:
            return False
        peak = max(vix_closes[-5:])
        current = vix_closes[-1]
        # Peak above 35, now declining for 2+ days
        if peak > 35 and current < vix_closes[-3]:
            return True
    except Exception:
        pass
    return False


# ── Main orchestration ────────────────────────────────────────────────────────

def fetch_macro_regime() -> dict:
    """
    Fetch 8 Yahoo signals → compute macro_health_score → classify regime.
    Writes macro_regime.json and returns the dict.
    Falls back to NORMAL on any fetch failure.
    """
    _TICKERS = {
        "vix":  "%5EVIX",
        "spy":  "SPY",
        "wti":  "CL%3DF",
        "gold": "GC%3DF",
        "dxy":  "DX-Y.NYB",
        "tnx":  "%5ETNX",
        "soxx": "SOXX",
        "qqq":  "QQQ",
    }

    now = datetime.now(ET)

    try:
        closes: dict[str, list[float]] = {}
        for key, ticker in _TICKERS.items():
            try:
                closes[key] = _yahoo_closes(ticker, days=30)
            except Exception as exc:
                logger.warning(f"macro_regime: could not fetch {ticker}: {exc}")
                closes[key] = []

        fred_modifier = _load_fred_modifier()

        vix_closes = closes.get("vix", [])
        spy_closes = closes.get("spy", [])

        vix_current = vix_closes[-1] if vix_closes else 20.0
        lookback = min(90, len(vix_closes) - 1)
        vix_90d_avg = (
            sum(vix_closes[-(lookback + 1):-1]) / lookback
            if lookback > 0 else vix_current
        )
        vix_trend = "declining" if len(vix_closes) >= 3 and vix_closes[-1] < vix_closes[-3] else "rising"

        spy_5day_return = _ret5d(spy_closes)
        sign_changes = 0
        if len(spy_closes) >= 6:
            diffs = [spy_closes[i] - spy_closes[i - 1] for i in range(max(len(spy_closes) - 5, 1), len(spy_closes))]
            sign_changes = sum(1 for i in range(1, len(diffs)) if (diffs[i] > 0) != (diffs[i - 1] > 0))

        consecutive_green = _count_consecutive_direction(spy_closes, "green")
        consecutive_red   = _count_consecutive_direction(spy_closes, "red")
        is_choppy         = _is_choppy(spy_closes)

        soxx_ret5d = _ret5d(closes.get("soxx", []))
        qqq_ret5d  = _ret5d(closes.get("qqq", []))

        signals = {
            "vix_closes":   vix_closes,
            "spy_ret5d":    spy_5day_return,
            "wti_ret5d":    _ret5d(closes.get("wti", [])),
            "gold_ret5d":   _ret5d(closes.get("gold", [])),
            "dxy_ret5d":    _ret5d(closes.get("dxy", [])),
            "tnx_ret5d":    _ret5d(closes.get("tnx", [])),
            "soxx_ret5d":   soxx_ret5d,
            "qqq_ret5d":    qqq_ret5d,
        }

        macro_health = compute_macro_health_score(signals, fred_modifier)
        sector_scores = _compute_sector_scores(soxx_ret5d, qqq_ret5d)
        bot_net_pnl = _load_bot_net_pnl()
        post_crisis = _is_post_crisis_recovery()

        regime = classify_regime(
            macro_health_score=macro_health,
            consecutive_green_days=consecutive_green,
            consecutive_red_days=consecutive_red,
            vix_current=vix_current,
            spy_5day_return=spy_5day_return,
            is_choppy=is_choppy,
            ai_semi_sector_score=sector_scores["AI_SEMI"],
            bot_net_pnl_pct=bot_net_pnl,
            post_crisis_recovery=post_crisis,
        )

        # Check capitulation candidates from research report
        capitulation_candidates: list[str] = []
        try:
            if config.RESEARCH_REPORT_FILE.exists():
                with open(config.RESEARCH_REPORT_FILE) as f:
                    rr = json.load(f)
                meta = rr.get("__meta__", {})
                capitulation_candidates = meta.get("capitulation_candidates", [])
                # Override regime if capitulation detected
                if capitulation_candidates and regime in ("DEFENSIVE", "NORMAL", "CHOPPY"):
                    regime = "CAPITULATION"
        except Exception:
            pass

        # Check for existing flash crash lockout
        flash_crash_active = False
        flash_crash_until  = None
        try:
            if config.MACRO_REGIME_FILE.exists():
                with open(config.MACRO_REGIME_FILE) as f:
                    prev = json.load(f)
                flash_crash_active = prev.get("flash_crash_active", False)
                flash_crash_until  = prev.get("flash_crash_until")
                if flash_crash_active and flash_crash_until:
                    until_dt = datetime.fromisoformat(flash_crash_until)
                    if now < until_dt:
                        regime = "CRISIS"
                    else:
                        flash_crash_active = False
                        flash_crash_until  = None
        except Exception:
            pass

        # Event risk from calendar
        event_risk_today   = False
        event_risk_details = ""
        try:
            from economic_calendar import is_event_risk_window
            event_risk_today, event_risk_details = is_event_risk_window(now)
        except Exception:
            pass

        result = {
            "regime":                  regime,
            "macro_health_score":      macro_health,
            "sector_scores":           sector_scores,
            "consecutive_green_days":  consecutive_green,
            "consecutive_red_days":    consecutive_red,
            "spy_5day_sign_changes":   sign_changes,
            "vix_current":             round(vix_current, 2),
            "vix_90d_avg":             round(vix_90d_avg, 2),
            "vix_trend":               vix_trend,
            "spy_5day_return":         round(spy_5day_return, 2),
            "flash_crash_active":      flash_crash_active,
            "flash_crash_until":       flash_crash_until,
            "event_risk_today":        event_risk_today,
            "event_risk_details":      event_risk_details,
            "capitulation_candidates": capitulation_candidates,
            "timestamp":               now.isoformat(),
            "reason": (
                f"{consecutive_green}g/{consecutive_red}r consecutive days, "
                f"macro_health={macro_health:+d}, VIX={vix_current:.1f} ({vix_trend})"
            ),
        }

    except Exception as exc:
        logger.warning(f"macro_regime fetch failed: {exc} — defaulting to NORMAL")
        result = {
            "regime":                  "NORMAL",
            "macro_health_score":      0,
            "sector_scores":           {"AI_SEMI": 0, "BROAD_TECH": 0, "CYBERSECURITY": 0},
            "consecutive_green_days":  0,
            "consecutive_red_days":    0,
            "spy_5day_sign_changes":   0,
            "vix_current":             0.0,
            "vix_90d_avg":             0.0,
            "vix_trend":               "unknown",
            "spy_5day_return":         0.0,
            "flash_crash_active":      False,
            "flash_crash_until":       None,
            "event_risk_today":        False,
            "event_risk_details":      "",
            "capitulation_candidates": [],
            "timestamp":               now.isoformat(),
            "reason":                  "fetch_failed",
        }

    with open(config.MACRO_REGIME_FILE, "w") as f:
        json.dump(result, f, indent=2)

    logger.info(
        f"Macro regime: {result['regime']} | "
        f"health={result['macro_health_score']:+d} | "
        f"VIX={result['vix_current']:.1f} | "
        f"SPY5d={result['spy_5day_return']:+.2f}% | "
        f"green={result['consecutive_green_days']} red={result['consecutive_red_days']}"
    )
    return result


def load_macro_regime() -> dict:
    """Return today's cached regime. Defaults to NORMAL if missing or unreadable."""
    try:
        if config.MACRO_REGIME_FILE.exists():
            with open(config.MACRO_REGIME_FILE) as f:
                data = json.load(f)
            if data.get("regime") in _VALID_REGIMES:
                return data
    except Exception:
        pass
    return {
        "regime": "NORMAL",
        "macro_health_score": 0,
        "vix_current": 0.0,
        "spy_5day_return": 0.0,
        "flash_crash_active": False,
        "flash_crash_until": None,
        "event_risk_today": False,
        "event_risk_details": "",
        "capitulation_candidates": [],
        "sector_scores": {"AI_SEMI": 0, "BROAD_TECH": 0, "CYBERSECURITY": 0},
    }


if __name__ == "__main__":
    import logging as _l
    _l.basicConfig(level=_l.INFO)
    fetch_macro_regime()
