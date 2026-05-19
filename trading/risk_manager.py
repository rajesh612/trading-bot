"""
Risk manager: daily API budget pools, gap guard, thesis-break heuristic,
and fill-price correction after bracket placement.

Budget is split into an entry pool (ENTRY_BUDGET_RATIO of daily ceiling)
and an exit pool (EXIT_BUDGET_RATIO) so open positions always have budget
reserved for thesis-break evaluation even when entries are exhausted.
"""
import logging
import time
from datetime import datetime
import config
import notifications.telegram as telegram_notify
from indicators.values import IndicatorValues
from trading.budget_guard import apply_exit_rollover
from trading.journal import BUDGET_LOCK
from utils.io_utils import load_json, save_json

from utils.constants import ET

logger = logging.getLogger(__name__)

# Sonnet 4.6 pricing per token (USD)
_INPUT_COST_PER_TOKEN       = 3.00    / 1_000_000
_CACHE_WRITE_COST_PER_TOKEN = 3.75    / 1_000_000
_CACHE_READ_COST_PER_TOKEN  = 0.30    / 1_000_000
_OUTPUT_COST_PER_TOKEN      = 15.00   / 1_000_000

_entry_exhausted_flag: bool = False
_disk_rehydrated: bool = False  # True after one-time disk check on first call after restart


# ── Budget management ─────────────────────────────────────────────────────────

def _fresh_budget() -> dict:
    """Return a zeroed budget dict for today."""
    today = datetime.now(ET).strftime("%Y-%m-%d")
    ceiling = config.API_BUDGET_NORMAL_CEILING
    return {
        "date": today, "calls_made": 0, "entry_calls": 0, "exit_calls": 0,
        "input_tokens": 0, "output_tokens": 0,
        "cache_write_tokens": 0, "cache_read_tokens": 0,
        "estimated_cost_usd": 0.0, "entry_spent_usd": 0.0, "exit_spent_usd": 0.0,
        "ceiling_usd": ceiling,
        "entry_ceiling_usd": round(ceiling * config.ENTRY_BUDGET_RATIO, 4),
        "exit_ceiling_usd": round(ceiling * config.EXIT_BUDGET_RATIO, 4),
        "ceiling_mode": "normal",
        "entry_exhausted": False, "exit_exhausted": False,
        "budget_exhausted": False, "escalated": False,
    }


def load_budget() -> dict:
    """Load today's budget from disk, resetting if stale or corrupt."""
    today = datetime.now(ET).strftime("%Y-%m-%d")
    data = load_json(config.API_BUDGET_FILE, {})
    if isinstance(data, dict) and data.get("date") == today:
        return data
    budget = _fresh_budget()
    _save_budget(budget)
    return budget


def _save_budget(b: dict) -> None:
    """Persist budget dict to API_BUDGET_FILE via save_json."""
    save_json(config.API_BUDGET_FILE, b)


def get_ceiling() -> float:
    """Return the active daily ceiling (normal or elevated)."""
    b = load_budget()
    if b.get("ceiling_mode") == "elevated":
        return config.API_BUDGET_ELEVATED_CEILING
    return config.API_BUDGET_NORMAL_CEILING


def check_entry_budget(estimated_cost: float, regime: str) -> bool:
    """Return True if entry call is within the 70% entry pool."""
    global _entry_exhausted_flag
    b = load_budget()
    if not b.get("entry_exhausted"):
        _entry_exhausted_flag = False

    if b.get("entry_exhausted"):
        logger.warning("Entry budget pool exhausted — blocking entry call.")
        return False

    entry_ceil = b.get("entry_ceiling_usd", round(b["ceiling_usd"] * config.ENTRY_BUDGET_RATIO, 4))
    spent = b.get("entry_spent_usd", 0.0)
    projected = spent + estimated_cost

    if projected > entry_ceil:
        logger.warning(
            "Entry budget ceiling reached: $%.4f + $%.4f > $%.4f — blocking entry.",
            spent, estimated_cost, entry_ceil,
        )
        b["entry_exhausted"] = True
        _entry_exhausted_flag = True
        _check_both_exhausted(b)
        _save_budget(b)
        return False

    if not b.get("entry_exhausted") and entry_ceil > 0 and spent / entry_ceil >= config.ENTRY_BUDGET_WARNING_PCT:
        telegram_notify.notify_budget_intraday_warning(spent, entry_ceil)

    warning_threshold = entry_ceil * config.API_BUDGET_WARNING_PCT
    if spent >= warning_threshold and (spent - estimated_cost) < warning_threshold:
        logger.warning(
            "Entry budget at %.0f%% of pool ($%.4f / $%.4f) — approaching limit.",
            spent / entry_ceil * 100, spent, entry_ceil,
        )

    return True


def check_exit_budget(estimated_cost: float, open_position_count: int, regime: str) -> bool:
    """Return True if exit call is within the 30% exit pool. Auto-escalates once."""
    b = load_budget()

    exit_ceil = b.get("exit_ceiling_usd", round(b["ceiling_usd"] * config.EXIT_BUDGET_RATIO, 4))
    spent = b.get("exit_spent_usd", 0.0)
    projected = spent + estimated_cost

    if projected > exit_ceil and not b.get("escalated") and open_position_count > 0:
        # Escalate to elevated ceiling and recompute pools
        new_total = config.API_BUDGET_ELEVATED_CEILING
        new_exit_ceil = round(new_total * config.EXIT_BUDGET_RATIO, 4)
        b["ceiling_usd"] = new_total
        b["entry_ceiling_usd"] = round(new_total * config.ENTRY_BUDGET_RATIO, 4)
        b["exit_ceiling_usd"] = new_exit_ceil
        b["escalated"] = True
        b["ceiling_mode"] = "elevated"
        _save_budget(b)
        try:
            telegram_notify.notify_budget_escalation(spent, new_exit_ceil)
        except Exception as exc:
            logger.warning("Telegram escalation notify failed: %s", exc)

        exit_ceil = new_exit_ceil
        projected = spent + estimated_cost

    if projected > exit_ceil:
        logger.warning(
            "Exit budget ceiling reached: $%.4f + $%.4f > $%.4f — blocking exit call.",
            spent, estimated_cost, exit_ceil,
        )
        b["exit_exhausted"] = True
        _check_both_exhausted(b)
        _save_budget(b)
        return False

    return True


def _check_both_exhausted(b: dict) -> None:
    """Set budget_exhausted when both pools are spent (mutates b in place)."""
    if b.get("entry_exhausted") and b.get("exit_exhausted"):
        b["budget_exhausted"] = True


def record_usage(
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
    kind: str = "entry",
) -> None:
    """Route token cost to entry or exit pool.
    kind='entry' → entry pool; 'exit' → exit pool; 'other' → total only (no pool deduction).
    """
    global _entry_exhausted_flag
    b = load_budget()

    b["calls_made"]         += 1
    b["input_tokens"]       += input_tokens
    b["output_tokens"]      += output_tokens
    b["cache_write_tokens"] += cache_write_tokens
    b["cache_read_tokens"]  += cache_read_tokens

    call_cost = (
        input_tokens       * _INPUT_COST_PER_TOKEN
        + output_tokens    * _OUTPUT_COST_PER_TOKEN
        + cache_write_tokens * _CACHE_WRITE_COST_PER_TOKEN
        + cache_read_tokens  * _CACHE_READ_COST_PER_TOKEN
    )
    b["estimated_cost_usd"] = round(b["estimated_cost_usd"] + call_cost, 6)

    if kind == "exit":
        b["exit_calls"]    += 1
        b["exit_spent_usd"] = round(b.get("exit_spent_usd", 0.0) + call_cost, 6)
    elif kind == "entry":
        b["entry_calls"]    += 1
        b["entry_spent_usd"] = round(b.get("entry_spent_usd", 0.0) + call_cost, 6)
    # kind="other" → only estimated_cost_usd updated; no pool deduction

    # Sync in-process flag to disk state so flag never outlives a budget reset (new day)
    if not b.get("entry_exhausted"):
        _entry_exhausted_flag = False

    logger.debug(
        "API budget: $%.4f total | entry=$%.4f exit=$%.4f | calls=%d",
        b["estimated_cost_usd"], b["entry_spent_usd"], b["exit_spent_usd"], b["calls_made"],
    )
    _save_budget(b)


def is_entry_budget_exhausted() -> bool:
    """O(1) after first call. Reads disk exactly once after restart to sync with pre-crash state."""
    global _entry_exhausted_flag, _disk_rehydrated
    with BUDGET_LOCK:
        if _entry_exhausted_flag:
            return True
        if not _disk_rehydrated:
            _disk_rehydrated = True
            b = load_budget()
            if b.get("entry_exhausted", False):
                _entry_exhausted_flag = True
                return True
        return False


def try_rollover_exit_to_entry(now: datetime) -> None:
    """
    Called at 12:30 PM ET by scheduler. Rolls unused exit budget into entry pool.
    Writes result to api_budget.json if rollover applied.
    Holds BUDGET_LOCK for the entire read-modify-write to prevent TOCTOU races.
    """
    global _entry_exhausted_flag
    with BUDGET_LOCK:
        b = load_budget()
        if b.get("_rollover_applied"):
            return  # idempotent — already rolled over today
        if apply_exit_rollover(b, now):
            b["_rollover_applied"] = True
            _save_budget(b)
            if _entry_exhausted_flag:
                _entry_exhausted_flag = False
                b["entry_exhausted"] = False
                _save_budget(b)
            logger.info(
                "Exit-to-entry rollover applied: entry_ceiling now $%.4f",
                b["entry_ceiling_usd"],
            )


# ── Gap guard ─────────────────────────────────────────────────────────────────

def is_gap_skip(current_price: float, prev_close: float) -> bool:
    """Return True if stock gapped up more than GAP_SKIP_PCT above prev_close."""
    if prev_close <= 0:
        return False
    return current_price > prev_close * (1 + config.GAP_SKIP_PCT / 100)


# ── Thesis-break heuristic ────────────────────────────────────────────────────

def needs_exit_evaluation(iv: IndicatorValues, position: dict, research: dict) -> bool:
    """Pure heuristic — returns True when any thesis-break condition holds."""
    symbol = iv.symbol

    entry_composite = position.get("entry_composite_score", 0)
    if iv.composite_score < entry_composite - config.EXIT_EVAL_COMPOSITE_DROP:
        return True

    sentiment = research.get(symbol, {}).get("sentiment_score", 0)
    if sentiment < config.EXIT_EVAL_SENTIMENT_FLOOR:
        return True

    entry_price = position.get("entry_price", 0)
    sl_pct = position.get("stop_loss_pct", config.STOP_LOSS_PCT)
    if entry_price > 0:
        pnl_pct = (iv.current_price - entry_price) / entry_price * 100
        if pnl_pct < -(sl_pct - 1.0):
            return True

    return False


# ── Fill price correction ─────────────────────────────────────────────────────

def correct_bracket_after_fill(
    trading_client,
    order_id: str,
    estimated_price: float,
    sl_pct: float,
    tp_pct: float,
) -> dict | None:
    """Poll for fill price and correct bracket SL/TP legs if slippage exceeds threshold."""
    for _ in range(config.FILL_POLL_ATTEMPTS):
        try:
            order = trading_client.get_order_by_id(order_id)
            if order.status == "filled" and order.filled_avg_price:
                fill_price = float(order.filled_avg_price)
                slippage = abs(fill_price - estimated_price) / estimated_price if estimated_price else 0
                if slippage <= config.FILL_SLIPPAGE_THRESHOLD:
                    return None
                new_sl = round(fill_price * (1 - sl_pct / 100), 2)
                new_tp = round(fill_price * (1 + tp_pct / 100), 2)
                for leg in (order.legs or []):
                    try:
                        if leg.type == "stop":
                            trading_client.replace_order_by_id(leg.id, stop_price=new_sl)
                        elif leg.type == "limit":
                            trading_client.replace_order_by_id(leg.id, limit_price=new_tp)
                    except Exception as exc:
                        logger.error("Failed to replace bracket leg %s: %s", leg.id, exc)
                        return None
                return {"stop_loss_price": new_sl, "take_profit_price": new_tp, "fill_price": fill_price}
        except Exception as exc:
            logger.debug("Fill poll error for %s: %s", order_id, exc)
        time.sleep(config.FILL_POLL_INTERVAL_S)

    logger.warning("Fill poll timed out for order %s after %d attempts.", order_id, config.FILL_POLL_ATTEMPTS)
    return None
