"""
Daily Claude API spend tracker with dynamic ceiling enforcement.
Pricing: claude-sonnet-4-6 — input $3/MTok, cache_write $3.75, cache_read $0.30, output $15.
"""
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

# Sonnet 4.6 pricing per token (USD)
_INPUT_COST_PER_TOKEN       = 3.00    / 1_000_000
_CACHE_WRITE_COST_PER_TOKEN = 3.75    / 1_000_000
_CACHE_READ_COST_PER_TOKEN  = 0.30    / 1_000_000
_OUTPUT_COST_PER_TOKEN      = 15.00   / 1_000_000

_ELEVATED_REGIMES = {"CRISIS", "CAPITULATION", "CRASH_RECOVERY"}


def _today_et() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")


def _fresh_budget(date_str: str, ceiling: float) -> dict:
    return {
        "date":               date_str,
        "calls_made":         0,
        "input_tokens":       0,
        "output_tokens":      0,
        "cache_write_tokens": 0,
        "cache_read_tokens":  0,
        "estimated_cost_usd": 0.0,
        "ceiling_usd":        ceiling,
        "ceiling_mode":       "normal",
        "budget_exhausted":   False,
    }


def load_budget() -> dict:
    """Load today's budget. Resets if date != today ET or file is corrupt."""
    today = _today_et()
    try:
        if config.API_BUDGET_FILE.exists():
            with open(config.API_BUDGET_FILE) as f:
                data = json.load(f)
            if data.get("date") == today:
                return data
    except Exception:
        pass
    budget = _fresh_budget(today, config.API_BUDGET_NORMAL_CEILING)
    _save_budget(budget)
    return budget


def _save_budget(budget: dict) -> None:
    with open(config.API_BUDGET_FILE, "w") as f:
        json.dump(budget, f, indent=2)


def get_ceiling(portfolio_gain_pct: float = 0.0, regime: str = "NORMAL") -> float:
    """Return the active ceiling in USD based on portfolio gain and market regime."""
    if portfolio_gain_pct >= 0.8 or regime in _ELEVATED_REGIMES:
        return config.API_BUDGET_ELEVATED_CEILING
    return config.API_BUDGET_NORMAL_CEILING


def check_budget(
    estimated_call_cost: float,
    portfolio_gain_pct: float = 0.0,
    regime: str = "NORMAL",
) -> bool:
    """
    Return True if the estimated call is within budget, False to block it.
    Logs a warning at API_BUDGET_WARNING_PCT of ceiling.
    """
    budget = load_budget()

    if budget.get("budget_exhausted"):
        logger.warning("API budget exhausted for today — blocking Claude call.")
        return False

    ceiling = get_ceiling(portfolio_gain_pct, regime)
    current = budget.get("estimated_cost_usd", 0.0)
    projected = current + estimated_call_cost

    if projected > ceiling:
        logger.warning(
            f"API budget ceiling reached: ${current:.3f} + ${estimated_call_cost:.3f} > ${ceiling:.2f} "
            f"(regime={regime}, gain={portfolio_gain_pct:+.1f}%) — blocking call."
        )
        budget["budget_exhausted"] = True
        _save_budget(budget)
        return False

    warning_threshold = ceiling * config.API_BUDGET_WARNING_PCT
    if current >= warning_threshold and (current - estimated_call_cost) < warning_threshold:
        logger.warning(
            f"API budget at {current / ceiling * 100:.0f}% of ceiling "
            f"(${current:.3f} / ${ceiling:.2f}) — approaching limit."
        )

    return True


def record_usage(
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> None:
    """Accumulate token counts and recompute estimated cost."""
    budget = load_budget()

    budget["calls_made"]         += 1
    budget["input_tokens"]       += input_tokens
    budget["output_tokens"]      += output_tokens
    budget["cache_write_tokens"] += cache_write_tokens
    budget["cache_read_tokens"]  += cache_read_tokens

    cost = (
        budget["input_tokens"]       * _INPUT_COST_PER_TOKEN
        + budget["output_tokens"]    * _OUTPUT_COST_PER_TOKEN
        + budget["cache_write_tokens"] * _CACHE_WRITE_COST_PER_TOKEN
        + budget["cache_read_tokens"]  * _CACHE_READ_COST_PER_TOKEN
    )
    budget["estimated_cost_usd"] = round(cost, 6)

    ceiling = get_ceiling()
    budget["ceiling_usd"] = ceiling
    budget["ceiling_mode"] = "elevated" if ceiling > config.API_BUDGET_NORMAL_CEILING else "normal"

    logger.debug(
        f"API budget: ${budget['estimated_cost_usd']:.4f} / ${ceiling:.2f} | "
        f"calls={budget['calls_made']} "
        f"in={budget['input_tokens']} out={budget['output_tokens']} "
        f"cache_w={budget['cache_write_tokens']} cache_r={budget['cache_read_tokens']}"
    )

    _save_budget(budget)
