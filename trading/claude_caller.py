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
import anthropic

import config
from trading import risk_manager
import notifications.telegram as telegram_notify
from indicators.values import IndicatorValues
from trading.cycle_context import CycleSnapshot
from trading.context_builder import build_stable_system_context, build_dynamic_user_context, build_stock_block, build_exit_stock_block

from utils.constants import ET

logger = logging.getLogger(__name__)

# ── Circuit breaker — tripped on 402/429; auto-clears after TTL expires ──
_CLAUDE_DISABLED = threading.Event()
_CLAUDE_DISABLED_REASON: str = ""
_CLAUDE_DISABLED_UNTIL: float = 0.0
_CLAUDE_LOCK = threading.Lock()

# ── Stable context cache — TTL 1 hour; string is identical within a trading day ─
_stable_ctx_text: str = ""
_stable_ctx_time: float = 0.0
_STABLE_CTX_TTL: float = 3600.0
_STABLE_CTX_LOCK = threading.Lock()


def _get_cached_stable_context(fred_cache: dict, research_meta: dict, regime_info: dict) -> str:
    global _stable_ctx_text, _stable_ctx_time
    with _STABLE_CTX_LOCK:
        if _stable_ctx_text and (time.monotonic() - _stable_ctx_time) < _STABLE_CTX_TTL:
            return _stable_ctx_text
        _stable_ctx_text = build_stable_system_context(regime_info, fred_cache, research_meta)
        _stable_ctx_time = time.monotonic()
        return _stable_ctx_text


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


def _ask_claude_batch(
    batch: list[tuple[str, IndicatorValues, dict | None]],
    snap: CycleSnapshot,
    ai: anthropic.Anthropic,
) -> dict[str, dict]:
    """Ask Claude for decisions on multiple stocks in a single API call."""
    global _CLAUDE_DISABLED_REASON, _CLAUDE_DISABLED_UNTIL
    symbols = [sym for sym, _, _ in batch]

    with _CLAUDE_LOCK:
        if _CLAUDE_DISABLED.is_set():
            if time.monotonic() >= _CLAUDE_DISABLED_UNTIL:
                _CLAUDE_DISABLED.clear()
                _CLAUDE_DISABLED_REASON = ""
            else:
                logger.warning("Claude disabled (%s) — HOLD for %s", _CLAUDE_DISABLED_REASON, symbols)
                return {sym: _default_hold(sym) for sym in symbols}

    estimated_cost = _estimate_batch_call_cost(len(batch))
    if not risk_manager.check_entry_budget(estimated_cost, snap.regime):
        logger.warning("API budget blocked — HOLD for %s", symbols)
        return {sym: _default_hold(sym) for sym in symbols}

    stock_blocks = "\n\n".join(
        build_stock_block(sym, iv, snap.research, existing_pos)
        for sym, iv, existing_pos in batch
    )

    now = datetime.now(ET)
    stable_ctx = _get_cached_stable_context(snap.fred_cache, snap.research_meta, snap.regime_info)
    dynamic_ctx = build_dynamic_user_context(now)

    system_text = (
        "You are an expert intraday day trader making final buy/sell decisions.\n"
        "You receive technical indicators, news sentiment, and market context.\n"
        "You may OVERRIDE technical signals when news sentiment strongly contradicts them.\n"
        "Prioritise capital preservation. Never hold overnight.\n"
        f"Current indicator weights: {json.dumps(snap.weights)}\n\n"
        f"{stable_ctx}"
    )
    if snap.strategy_memory:
        system_text += f"\n\nToday's strategy memory:\n{snap.strategy_memory}"

    system_blocks = [
        {
            "type": "text",
            "text": system_text,
            "cache_control": {"type": "ephemeral"},
        }
    ]

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
            base_ttl = 1800.0 if exc.status_code == 402 else 300.0
            with _CLAUDE_LOCK:
                now_m = time.monotonic()
                remaining = max(0.0, _CLAUDE_DISABLED_UNTIL - now_m)
                new_ttl = min(max(base_ttl, remaining * 2), 1800.0)
                _CLAUDE_DISABLED_REASON = f"HTTP {exc.status_code} — {exc.message[:120]}"
                _CLAUDE_DISABLED_UNTIL = now_m + new_ttl
                _CLAUDE_DISABLED.set()
            logger.error(
                "CLAUDE API ERROR — circuit breaker tripped for %.0fs: %s",
                new_ttl, _CLAUDE_DISABLED_REASON,
            )
            if exc.status_code == 402:
                telegram_notify.notify_claude_disabled(_CLAUDE_DISABLED_REASON)
        else:
            logger.error("Claude API error HTTP %d: %s", exc.status_code, exc.message[:200])
        return {sym: _default_hold(sym) for sym in symbols}
    except Exception as exc:
        logger.error("Claude API unexpected error: %s: %s", type(exc).__name__, exc)
        return {sym: _default_hold(sym) for sym in symbols}

    try:
        usage = response.usage
        risk_manager.record_usage(
            input_tokens=getattr(usage, "input_tokens", 0),
            output_tokens=getattr(usage, "output_tokens", 0),
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0),
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0),
            kind="entry",
        )
    except Exception:
        pass

    text = response.content[0].text.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    logger.warning("Claude JSON parse error for batch %s — defaulting to HOLD", symbols)
    return {sym: _default_hold(sym) for sym in symbols}


def _ask_claude_exit_batch(
    exit_candidates: list[tuple[str, IndicatorValues, dict]],
    snap: CycleSnapshot,
    ai: anthropic.Anthropic,
) -> dict[str, dict]:
    """Ask Claude to evaluate thesis-break candidates using compact exit prompts."""
    global _CLAUDE_DISABLED_REASON, _CLAUDE_DISABLED_UNTIL
    symbols = [sym for sym, _, _ in exit_candidates]

    with _CLAUDE_LOCK:
        if _CLAUDE_DISABLED.is_set():
            if time.monotonic() >= _CLAUDE_DISABLED_UNTIL:
                _CLAUDE_DISABLED.clear()
                _CLAUDE_DISABLED_REASON = ""
            else:
                return {sym: _default_hold(sym) for sym in symbols}

    open_count = len(exit_candidates)
    estimated_cost = (300 * open_count) * 3.00 / 1_000_000

    if not risk_manager.check_exit_budget(estimated_cost, open_count, snap.regime):
        logger.warning("Exit budget blocked for thesis-break eval — HOLD for %s", symbols)
        return {sym: _default_hold(sym) for sym in symbols}

    exit_blocks = "\n\n".join(
        build_exit_stock_block(sym, iv, pos, snap.research)
        for sym, iv, pos in exit_candidates
    )

    user_msg = (
        f"Thesis-break evaluation for {len(exit_candidates)} open position(s):\n\n"
        f"{exit_blocks}\n\n"
        "For each position: should we CLOSE (thesis broken) or HOLD?\n"
        "CLOSE only if thesis clearly broken — news shock, composite collapse, near stop-loss.\n\n"
        "Respond in JSON keyed by symbol:\n"
        '{\n  "<SYMBOL>": {\n'
        '    "action": "<CLOSE|HOLD>",\n'
        '    "confidence": <0-100>,\n'
        '    "reasoning": "<1 sentence>"\n'
        "  }\n}"
    )

    try:
        response = ai.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=150 * len(exit_candidates),
            messages=[{"role": "user", "content": user_msg}],
        )
    except anthropic.APIStatusError as exc:
        if exc.status_code in (402, 429):
            base_ttl = 1800.0 if exc.status_code == 402 else 300.0
            with _CLAUDE_LOCK:
                now_m = time.monotonic()
                remaining = max(0.0, _CLAUDE_DISABLED_UNTIL - now_m)
                new_ttl = min(max(base_ttl, remaining * 2), 1800.0)
                _CLAUDE_DISABLED_REASON = f"HTTP {exc.status_code} — {exc.message[:120]}"
                _CLAUDE_DISABLED_UNTIL = now_m + new_ttl
                _CLAUDE_DISABLED.set()
            if exc.status_code == 402:
                telegram_notify.notify_claude_disabled(_CLAUDE_DISABLED_REASON)
        logger.error("Claude exit API error HTTP %d: %s", exc.status_code, exc.message[:200])
        return {sym: _default_hold(sym) for sym in symbols}
    except Exception as exc:
        logger.error("Claude exit API unexpected error: %s: %s", type(exc).__name__, exc)
        return {sym: _default_hold(sym) for sym in symbols}

    try:
        usage = response.usage
        risk_manager.record_usage(
            input_tokens=getattr(usage, "input_tokens", 0),
            output_tokens=getattr(usage, "output_tokens", 0),
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0),
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0),
            kind="exit",
        )
    except Exception:
        pass

    text = response.content[0].text.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    logger.warning("Claude exit JSON parse error for %s — defaulting to HOLD", symbols)
    return {sym: _default_hold(sym) for sym in symbols}
