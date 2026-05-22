"""
CycleSnapshot: all trading-cycle data loaded once per cycle.
No I/O happens inside the pipeline — everything is pre-loaded here.

FRED data is TTL-cached (2 h) — it only changes at 4 PM post-market.
All other fields are read fresh each cycle (every 10 min is fine for
regime, research, weights).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import logging
from datetime import datetime

import config
from research.intraday import load_intraday_news
from macro.regime import load_macro_regime, load_trading_params
from macro.intraday_context import get_intraday_context
from utils.constants import ET
from utils.io_utils import load_json
from utils.data_health import StalenessGuard, FRESH_GUARD

_logger = logging.getLogger(__name__)

# ── FRED TTL cache — stable all trading day ───────────────────────────────────
_fred_cache: dict = {}
_fred_loaded_at: float = 0.0
_FRED_TTL: float = 7200.0   # 2 hours
_FRED_LOCK = threading.Lock()


def _get_fred_cache() -> dict:
    global _fred_cache, _fred_loaded_at
    with _FRED_LOCK:
        if _fred_cache and (time.monotonic() - _fred_loaded_at) < _FRED_TTL:
            return _fred_cache
        _fred_cache = load_json(config.FRED_CACHE_FILE, {})
        _fred_loaded_at = time.monotonic()
        return _fred_cache


def invalidate_fred_cache() -> None:
    """Call after refresh_fred_cache() at 4 PM to force reload on next access."""
    global _fred_loaded_at
    with _FRED_LOCK:
        _fred_loaded_at = 0.0


# ── CycleSnapshot ─────────────────────────────────────────────────────────────

@dataclass
class CycleSnapshot:
    """
    Immutable snapshot of all data needed for one trading cycle.
    Built once at cycle start; passed through pipeline and claude_caller
    without any further disk reads.
    """
    regime_info: dict
    regime: str                   # shortcut: regime_info["regime"]
    fred_cache: dict              # TTL-cached — at most 1 disk read per 2 h
    research: dict                # research_report.json — fresh each cycle
    research_meta: dict           # research["__meta__"] convenience accessor
    strategy_memory: str
    weights: dict
    params: dict
    portfolio_gain_pct: float
    staleness_guard: StalenessGuard = field(default_factory=lambda: FRESH_GUARD)
    intraday_news: dict = field(default_factory=dict)  # populated in Task 9
    sector_today: dict = field(default_factory=dict)
    advance_decline_ratio: float = 1.0   # IWM/SPY breadth ratio; < 0.85 → narrow breadth
    major_event_today: bool = False       # True if FOMC/CPI/NFP/PCE/JOLTS scheduled today
    sector_etf_cache: dict = field(default_factory=dict)
    # Maps sub_etf symbol (e.g. "SOXX") → intraday % change float


def build_cycle_snapshot(
    portfolio_gain_pct: float,
    *,
    regime_info: dict | None = None,
    params: dict | None = None,
    staleness_guard: StalenessGuard | None = None,
) -> CycleSnapshot:
    """
    Load all cycle data in one place. Called once by run_trading_cycle().
    Subsequent reads within the same cycle use the returned snapshot.
    """
    regime_info = regime_info if regime_info is not None else load_macro_regime()
    params      = params if params is not None else load_trading_params()
    research    = load_json(config.RESEARCH_REPORT_FILE, {})
    weights     = load_json(config.INDICATOR_WEIGHTS_FILE, {})

    strat_path = Path(str(config.STRATEGY_MEMORY_FILE))
    raw_mem = ""
    try:
        age_days = (time.time() - strat_path.stat().st_mtime) / 86400
        if age_days > config.STRATEGY_MEMORY_MAX_AGE_DAYS:
            _logger.warning(
                "strategy_memory.md is %.0f days old (> %d) — skipping stale rules; "
                "learning agent will refresh it tonight.",
                age_days, config.STRATEGY_MEMORY_MAX_AGE_DAYS,
            )
        else:
            raw_mem = strat_path.read_text()
            if len(raw_mem) > config.STRATEGY_MEMORY_MAX_CHARS:
                raw_mem = raw_mem[:config.STRATEGY_MEMORY_MAX_CHARS]
    except FileNotFoundError:
        pass

    intraday_ctx = get_intraday_context(datetime.now(ET))
    sector_today = intraday_ctx.sector_today if intraday_ctx is not None else {}

    advance_decline_ratio = 1.0
    try:
        from macro.breadth_signals import get_advance_decline_ratio
        ad = get_advance_decline_ratio()
        if ad is not None:
            advance_decline_ratio = ad
    except Exception:
        pass

    major_event_today = False
    try:
        from macro.economic_calendar import get_todays_events
        _high_impact = {"FOMC", "NFP", "CPI", "PCE", "JOLTS"}
        for _evt in get_todays_events():
            if any(tag in _evt.get("name", "").upper() for tag in _high_impact):
                major_event_today = True
                break
    except Exception:
        pass

    return CycleSnapshot(
        regime_info=regime_info,
        regime=regime_info.get("regime", "NORMAL"),
        fred_cache=_get_fred_cache(),
        research=research,
        research_meta=research.get("__meta__", {}),
        strategy_memory=raw_mem,
        weights=weights,
        params=params,
        portfolio_gain_pct=portfolio_gain_pct,
        staleness_guard=staleness_guard or FRESH_GUARD,
        intraday_news=load_intraday_news(),
        sector_today=sector_today,
        advance_decline_ratio=advance_decline_ratio,
        major_event_today=major_event_today,
    )
