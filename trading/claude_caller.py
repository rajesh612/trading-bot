"""
Claude API interaction for the trading agent — batch decisions, circuit breaker,
budget gate, and system-block caching.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic

import api_budget
import config
import telegram_notify
from alpaca_client import AlpacaClient
from indicator_engine import IndicatorValues
from macro_regime import load_macro_regime
from trading.context_builder import build_stable_system_context, build_dynamic_user_context, build_stock_block

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

# ── Circuit breaker ───────────────────────────────────────────────────────────
# Set when a hard API error (402/429) is received.
# Prevents hammering a dead endpoint every 5 minutes.
_CLAUDE_DISABLED = threading.Event()
_CLAUDE_DISABLED_REASON: str = ""
_CLAUDE_LOCK = threading.Lock()

# ── Stable context cache ───────────────────────────────────────────────────────
# Keyed on fred+meta content; TTL 1 hour. The stable context string is identical
# for every call within a trading day — caching avoids re-building the string.
_stable_ctx_text: str = ""
_stable_ctx_time: float = 0.0
_STABLE_CTX_TTL: float = 3600.0
_STABLE_CTX_LOCK = threading.Lock()


def _get_cached_stable_context(fred_cache: dict, research_meta: dict) -> str:
    global _stable_ctx_text, _stable_ctx_time
    with _STABLE_CTX_LOCK:
        if _stable_ctx_text and (time.monotonic() - _stable_ctx_time) < _STABLE_CTX_TTL:
            return _stable_ctx_text
        _stable_ctx_text = build_stable_system_context(
            _get_current_regime_info(), fred_cache, research_meta
        )
        _stable_ctx_time = time.monotonic()
        return _stable_ctx_text


def is_claude_disabled() -> bool:
    return _CLAUDE_DISABLED.is_set()


def _default_hold(symbol: str) -> dict:
    return {
        "action": "HOLD",
        "confidence": 0,
        "reasoning": "Claude unavailable — defaulting to HOLD.",
        "stop_loss_pct": config.STOP_LOSS_PCT,
        "take_profit_pct": config.TAKE_PROFIT_PCT,
        "indicator_signals": {},
    }


def _estimate_batch_call_cost(batch_size: int) -> float:
    """Quick token-cost estimate for a Claude batch call (Sonnet 4.6 pricing)."""
    system_tokens = 1500 + 400 + 200 * batch_size
    output_tokens = 300 * batch_size
    return system_tokens * 3.00 / 1_000_000 + output_tokens * 15.00 / 1_000_000


def _get_portfolio_intraday_gain() -> float:
    """Best-effort intraday gain % for budget ceiling decision."""
    try:
        alpaca = AlpacaClient()
        account = alpaca.get_account()
        last_equity = float(account.last_equity)
        portfolio   = float(account.portfolio_value)
        if last_equity > 0:
            return (portfolio - last_equity) / last_equity * 100
    except Exception:
        pass
    return 0.0


def _get_current_regime() -> str:
    try:
        return load_macro_regime().get("regime", "NORMAL")
    except Exception:
        return "NORMAL"


def _get_current_regime_info() -> dict:
    try:
        return load_macro_regime()
    except Exception:
        return {"regime": "NORMAL", "macro_health_score": 0, "vix_current": 0.0}


def _ask_claude_batch(
    batch: list[tuple[str, IndicatorValues, dict | None]],
    research: dict,
    strategy_memory: str,
    weights: dict,
    ai: anthropic.Anthropic,
    fred_cache: dict,
    research_meta: dict,
) -> dict[str, dict]:
    """Ask Claude for decisions on multiple stocks in a single API call."""
    global _CLAUDE_DISABLED_REASON
    symbols = [sym for sym, _, _ in batch]

    with _CLAUDE_LOCK:
        if _CLAUDE_DISABLED.is_set():
            logger.warning("Claude disabled (%s) — HOLD for %s", _CLAUDE_DISABLED_REASON, symbols)
            return {sym: _default_hold(sym) for sym in symbols}

    estimated_cost = _estimate_batch_call_cost(len(batch))
    gain_pct = _get_portfolio_intraday_gain()
    regime   = _get_current_regime()
    if not api_budget.check_budget(estimated_cost, gain_pct, regime):
        logger.warning("API budget blocked — HOLD for %s", symbols)
        return {sym: _default_hold(sym) for sym in symbols}

    stock_blocks = "\n\n".join(
        build_stock_block(sym, iv, research, existing_pos)
        for sym, iv, existing_pos in batch
    )

    now = datetime.now(ET)
    stable_ctx = _get_cached_stable_context(fred_cache, research_meta)
    dynamic_ctx = build_dynamic_user_context(now)

    # Stable system block — no timestamps or live prices, so Anthropic cache hits every call.
    system_text = (
        "You are an expert intraday day trader making final buy/sell decisions.\n"
        "You receive technical indicators, news sentiment, and market context.\n"
        "You may OVERRIDE technical signals when news sentiment strongly contradicts them.\n"
        "Prioritise capital preservation. Never hold overnight.\n"
        f"Current indicator weights: {json.dumps(weights)}\n\n"
        f"{stable_ctx}"
    )
    if strategy_memory:
        system_text += f"\n\nToday's strategy memory:\n{strategy_memory}"

    system_blocks = [
        {
            "type": "text",
            "text": system_text,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    # Dynamic context (live prices/time) prepended to user message so system block stays stable.
    user_msg = (
        f"{dynamic_ctx}\n\n"
        f"Decisions needed for {len(batch)} stock(s):\n\n"
        f"{stock_blocks}\n\n"
        "Rules: BUY only if no open position and signal clear. "
        "CLOSE if open position and thesis broken. HOLD otherwise.\n\n"
        "Respond in JSON keyed by symbol:\n"
        '{\n  "<SYMBOL>": {\n'
        '    "action": "<BUY|HOLD|CLOSE>",\n'
        '    "confidence": <0-100>,\n'
        '    "reasoning": "<1 sentence>",\n'
        '    "stop_loss_pct": <number>,\n'
        '    "take_profit_pct": <number>,\n'
        '    "indicator_signals": {"VWAP":"<correct|incorrect|neutral>",'
        '"RSI":"<correct|incorrect|neutral>","MACD":"<correct|incorrect|neutral>",'
        '"BB":"<correct|incorrect|neutral>","EMA":"<correct|incorrect|neutral>",'
        '"Volume":"<correct|incorrect|neutral>"}\n'
        "  }\n}"
    )

    try:
        response = ai.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=300 * len(batch),
            system=system_blocks,
            messages=[{"role": "user", "content": user_msg}],
        )
    except anthropic.APIStatusError as exc:
        if exc.status_code in (402, 429):
            with _CLAUDE_LOCK:
                _CLAUDE_DISABLED_REASON = f"HTTP {exc.status_code} — {exc.message[:120]}"
                _CLAUDE_DISABLED.set()
            logger.error(
                "CLAUDE API ERROR — circuit breaker tripped: %s\n"
                "  Resolve at https://console.anthropic.com — all Claude calls suspended until restart.",
                _CLAUDE_DISABLED_REASON,
            )
            telegram_notify.notify_claude_disabled(_CLAUDE_DISABLED_REASON)
        else:
            logger.error("Claude API error HTTP %d: %s", exc.status_code, exc.message[:200])
        return {sym: _default_hold(sym) for sym in symbols}
    except Exception as exc:
        logger.error("Claude API unexpected error: %s: %s", type(exc).__name__, exc)
        return {sym: _default_hold(sym) for sym in symbols}

    try:
        usage = response.usage
        api_budget.record_usage(
            input_tokens=getattr(usage, "input_tokens", 0),
            output_tokens=getattr(usage, "output_tokens", 0),
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0),
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0),
        )
    except Exception:
        pass

    text  = response.content[0].text.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    logger.warning("Claude JSON parse error for batch %s — defaulting to HOLD", symbols)
    return {sym: _default_hold(sym) for sym in symbols}
