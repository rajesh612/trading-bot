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
from signals.atr_tp import compute_atr_tp_pct
from signals.entry_gates import GateResult
from utils.ticker_quality import TickerQuality
from utils.ticker_quality import derive_mode
from trading.pre_claude_filter import FilterResult

from utils.constants import ET

logger = logging.getLogger(__name__)

# ── Circuit breaker — tripped on 402/429; auto-clears after TTL expires ──
_CLAUDE_DISABLED = threading.Event()
_CLAUDE_DISABLED_REASON: str = ""
_CLAUDE_DISABLED_UNTIL: float = 0.0
_CLAUDE_LOCK = threading.Lock()

# ── Stable context cache — TTL 1 hour; invalidated on regime change (noon refresh) ─
_stable_ctx_text: str = ""
_stable_ctx_time: float = 0.0
_stable_ctx_regime: str = ""   # invalidate when regime changes mid-session
_STABLE_CTX_TTL: float = 3600.0
_STABLE_CTX_LOCK = threading.Lock()


def _get_cached_stable_context(fred_cache: dict, research_meta: dict, regime_info: dict) -> str:
    global _stable_ctx_text, _stable_ctx_time, _stable_ctx_regime
    current_regime = regime_info.get("regime", "NORMAL")
    with _STABLE_CTX_LOCK:
        age = time.monotonic() - _stable_ctx_time
        if (
            _stable_ctx_text
            and age < _STABLE_CTX_TTL
            and _stable_ctx_regime == current_regime
        ):
            return _stable_ctx_text
        _stable_ctx_text  = build_stable_system_context(regime_info, fred_cache, research_meta)
        _stable_ctx_time  = time.monotonic()
        _stable_ctx_regime = current_regime
        return _stable_ctx_text


def _validate_decision(decision: dict, symbol: str) -> dict:
    """Clamp Claude output to safe ranges. Logs a warning if any value was adjusted."""
    original = {k: decision.get(k) for k in ("confidence", "stop_loss_pct", "take_profit_pct")}
    decision["confidence"] = max(0, min(100, int(decision.get("confidence", 0))))
    decision["stop_loss_pct"] = max(0.5, min(10.0, float(decision.get("stop_loss_pct", 4.0))))
    decision["take_profit_pct"] = max(1.0, min(15.0, float(decision.get("take_profit_pct", 5.0))))
    adjusted = {k: decision.get(k) for k in ("confidence", "stop_loss_pct", "take_profit_pct")}
    if adjusted != original:
        logger.warning(
            "%s: Claude output clamped — original=%s adjusted=%s", symbol, original, adjusted
        )
    return decision


def _default_hold(symbol: str) -> dict:
    return {
        "action": "HOLD",
        "confidence": 0,
        "reasoning": "Claude unavailable — defaulting to HOLD.",
        "stop_loss_pct": config.STOP_LOSS_PCT,
        "take_profit_pct": config.TAKE_PROFIT_PCT,
        "indicator_signals": {},
    }


def _is_usage_limit_error(exc: anthropic.APIStatusError) -> bool:
    """Return True when a 400 indicates a monthly usage-cap hit (not a malformed request).

    # Matches: "You have reached your specified API usage limits. You will regain access on..."
    """
    return exc.status_code == 400 and "usage limits" in (exc.message or "").lower()


def _trip_circuit_breaker(ttl: float, reason: str, notify_telegram: bool = True) -> None:
    """Trip the circuit breaker for the given TTL and reason; optionally notify Telegram.

    Caller is responsible for computing the final TTL before calling this helper.
    """
    global _CLAUDE_DISABLED_REASON, _CLAUDE_DISABLED_UNTIL
    with _CLAUDE_LOCK:
        _CLAUDE_DISABLED_REASON = reason[:120]
        _CLAUDE_DISABLED_UNTIL = time.monotonic() + ttl
        _CLAUDE_DISABLED.set()
    logger.error(
        "CLAUDE API ERROR — circuit breaker tripped for %.0fs: %s", ttl, reason[:120]
    )
    if notify_telegram:
        telegram_notify.notify_claude_disabled(reason[:120])


def _estimate_batch_call_cost(batch_size: int) -> float:
    """Quick token-cost estimate for a Claude batch call (Sonnet 4.6 pricing)."""
    system_tokens = 1500 + 400 + 200 * batch_size
    output_tokens = 300 * batch_size
    return system_tokens * 3.00 / 1_000_000 + output_tokens * 15.00 / 1_000_000


def _ask_claude_batch(
    batch: list[tuple[str, IndicatorValues, dict | None, FilterResult]],
    snap: CycleSnapshot,
    ai: anthropic.Anthropic,
) -> dict[str, dict]:
    """Ask Claude for decisions on multiple stocks in a single API call."""
    global _CLAUDE_DISABLED_REASON, _CLAUDE_DISABLED_UNTIL
    symbols = [sym for sym, _, _, _ in batch]

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

    _atr_mult = snap.params.get("atr_tp_multiplier", config.ATR_TP_MULT_DEFAULT)
    now = datetime.now(ET)
    _default_quality = TickerQuality("MEDIUM", 50, frozenset(), (), {})
    _default_gate = GateResult(passed=True, reason="", flags=())

    stock_blocks = "\n\n".join(
        build_stock_block(
            sym, iv, snap,
            gate_result=fr.gate_result or _default_gate,
            quality=fr.quality or _default_quality,
            mode=derive_mode(
                sym,
                pead_ok="PEAD_EXTENDED_WINDOW" in (fr.quality.unlocks if fr.quality else frozenset()),
            ),
            entry_time=now,
            existing_pos=existing_pos,
            atr_suggested_tp_pct=compute_atr_tp_pct(iv.atr, iv.current_price, _atr_mult),
            intraday_news=snap.intraday_news.get(sym, {}),
        )
        for sym, iv, existing_pos, fr in batch
    )
    stable_ctx = _get_cached_stable_context(snap.fred_cache, snap.research_meta, snap.regime_info)
    dynamic_ctx = build_dynamic_user_context(now, portfolio_gain_pct=snap.portfolio_gain_pct)

    stable_system_text = (
        "You are an expert intraday day trader making final buy/sell decisions.\n"
        "You receive technical indicators, news sentiment, and market context.\n\n"
        "<rules>\n"
        "Override technical signals when news sentiment strongly contradicts them — "
        "news leads price by minutes while technicals lag.\n"
        "Prioritise capital preservation — all positions close at 3:55 PM ET; "
        "intraday losses have no recovery window.\n"
        "Do not close positions near end-of-day to avoid overnight exposure — "
        "the swing promotion system evaluates EOD holds automatically at 3:50 PM; "
        "your decisions are based on signal quality only, not time remaining.\n"
        "</rules>\n\n"
        f"{stable_ctx}"
    )
    volatile_text = f"Current indicator weights: {json.dumps(snap.weights)}"
    if snap.strategy_memory:
        volatile_text += f"\n\nToday's strategy memory:\n{snap.strategy_memory}"

    system_blocks = [
        {"type": "text", "text": stable_system_text, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": volatile_text, "cache_control": {"type": "ephemeral"}},
    ]

    user_msg = (
        f"{dynamic_ctx}\n\n"
        f"<stocks>\n{stock_blocks}\n</stocks>\n\n"
        "<rules>\n"
        "BUY only if no open position and signal is clear — overexposure to one name amplifies drawdown.\n"
        "CLOSE if open position and thesis broken — locking a small loss prevents a large one.\n"
        "HOLD when uncertain — missing a trade is better than a losing one.\n"
        "</rules>\n\n"
        "<example>\n"
        'Stock: NVDA composite=+52.1 RSI=54.2(+1.5) MACD=+0.0031 SENT:BULLISH(+20) cat="AI beat"\n'
        "Response: "
        '{"NVDA": {"action": "BUY", "confidence": 74, "reasoning": "Strong composite with bullish catalyst, RSI not overbought", "stop_loss_pct": 3.5, "take_profit_pct": 5.5, "indicator_signals": {"VWAP": "correct", "RSI": "correct", "MACD": "correct", "BB": "neutral", "EMA": "correct", "Volume": "correct"}}}\n'
        "</example>\n\n"
        f"Decisions needed for {len(batch)} stock(s). Respond in JSON keyed by symbol:\n"
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
            output_config={"effort": "medium"},
        )
    except anthropic.APIStatusError as exc:
        if _is_usage_limit_error(exc):
            _trip_circuit_breaker(
                ttl=config.CLAUDE_CB_LIMIT_TTL,
                reason=f"HTTP 400 usage limit — {exc.message[:200]}",
            )
        elif exc.status_code in (402, 429):
            base_ttl = config.CLAUDE_CB_402_TTL if exc.status_code == 402 else config.CLAUDE_CB_429_TTL
            now_m = time.monotonic()
            with _CLAUDE_LOCK:
                remaining = max(0.0, _CLAUDE_DISABLED_UNTIL - now_m)
            new_ttl = min(max(base_ttl, remaining * 2), config.CLAUDE_CB_402_TTL)
            _trip_circuit_breaker(
                ttl=new_ttl,
                reason=f"HTTP {exc.status_code} — {exc.message[:120]}",
                notify_telegram=(exc.status_code == 402),
            )
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

    text = next((b.text for b in response.content if b.type == "text"), "")
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        logger.warning("Claude response contained no JSON object for batch %s — defaulting to HOLD", symbols)
        return {sym: _default_hold(sym) for sym in symbols}
    try:
        parsed = json.loads(match.group())
        return {sym: _validate_decision(dec, sym) for sym, dec in parsed.items()}
    except Exception:
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

    now_exit = datetime.now(ET)
    exit_blocks = "\n\n".join(
        build_exit_stock_block(
            sym, iv, pos, snap.research,
            intraday_news=snap.intraday_news.get(sym, {}),
            now=now_exit,
        )
        for sym, iv, pos in exit_candidates
    )

    system_text = (
        "You are an expert intraday trader evaluating thesis-break candidates.\n\n"
        "<rules>\n"
        "CLOSE only if one of these is true — each criterion has a clear rationale:\n"
        "(1) Composite score dropped ≥10pts from entry AND thesis broken — "
        "the original edge is gone and holding adds risk with no edge.\n"
        "(2) Intraday news shock ≥60 — a material event has changed the risk profile.\n"
        "(3) Position within 0.5% of stop-loss — better to exit cleanly than be stopped out on a spike.\n"
        "(4) Regime shift to CRISIS — capital preservation overrides all trade theses.\n"
        "Default to HOLD — premature exits destroy returns; the stop-loss exists precisely to cap downside.\n"
        "</rules>\n\n"
        f"Current regime: {snap.regime} | VIX: {snap.regime_info.get('vix_current', 'N/A')}\n"
        'Respond in JSON: {"<symbol>": {"action": "HOLD"|"CLOSE", "confidence": <0-100>, "reasoning": "<brief>"}}'
    )
    system_blocks = [
        {"type": "text", "text": system_text},
        {"type": "text", "text": f"Strategy memory:\n{snap.strategy_memory or '(no strategy memory yet)'}"},
    ]

    dynamic_ctx = build_dynamic_user_context(now_exit, portfolio_gain_pct=snap.portfolio_gain_pct)

    user_msg = (
        f"{dynamic_ctx}\n\n"
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
            system=system_blocks,
            messages=[{"role": "user", "content": user_msg}],
            output_config={"effort": "medium"},
        )
    except anthropic.APIStatusError as exc:
        if _is_usage_limit_error(exc):
            _trip_circuit_breaker(
                ttl=config.CLAUDE_CB_LIMIT_TTL,
                reason=f"HTTP 400 usage limit — {exc.message[:200]}",
            )
        elif exc.status_code in (402, 429):
            base_ttl = config.CLAUDE_CB_402_TTL if exc.status_code == 402 else config.CLAUDE_CB_429_TTL
            now_m = time.monotonic()
            with _CLAUDE_LOCK:
                remaining = max(0.0, _CLAUDE_DISABLED_UNTIL - now_m)
            new_ttl = min(max(base_ttl, remaining * 2), config.CLAUDE_CB_402_TTL)
            _trip_circuit_breaker(
                ttl=new_ttl,
                reason=f"HTTP {exc.status_code} — {exc.message[:120]}",
                notify_telegram=(exc.status_code == 402),
            )
        else:
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

    text = next((b.text for b in response.content if b.type == "text"), "")
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        logger.warning("Claude exit response contained no JSON object for %s — defaulting to HOLD", symbols)
        return {sym: _default_hold(sym) for sym in symbols}
    try:
        parsed = json.loads(match.group())
    except Exception:
        logger.warning("Claude exit JSON parse error for %s — defaulting to HOLD", symbols)
        return {sym: _default_hold(sym) for sym in symbols}
    for sym, dec in parsed.items():
        if isinstance(dec, dict):
            dec["confidence"] = max(0, min(100, int(dec.get("confidence", 0))))
            if dec.get("action") not in ("CLOSE", "HOLD"):
                logger.warning("%s: invalid exit action %r — defaulting to HOLD", sym, dec.get("action"))
                dec["action"] = "HOLD"
    return parsed
