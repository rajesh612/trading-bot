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

import config
from research.intraday import load_intraday_news
from macro_regime import load_macro_regime, load_trading_params
from utils.io_utils import load_json

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
    intraday_news: dict = field(default_factory=dict)  # populated in Task 9


def build_cycle_snapshot(portfolio_gain_pct: float) -> CycleSnapshot:
    """
    Load all cycle data in one place. Called once by run_trading_cycle().
    Subsequent reads within the same cycle use the returned snapshot.
    """
    regime_info = load_macro_regime()
    research    = load_json(config.RESEARCH_REPORT_FILE, {})
    weights     = load_json(config.INDICATOR_WEIGHTS_FILE, {})
    params      = load_trading_params()

    strat_path = Path(str(config.STRATEGY_MEMORY_FILE))
    try:
        raw_mem = strat_path.read_text()
        if len(raw_mem) > config.STRATEGY_MEMORY_MAX_CHARS:
            raw_mem = raw_mem[-config.STRATEGY_MEMORY_MAX_CHARS:]
    except FileNotFoundError:
        raw_mem = ""

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
        intraday_news=load_intraday_news(),
    )
