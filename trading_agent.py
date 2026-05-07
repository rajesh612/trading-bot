import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import anthropic
import telegram_notify

import api_budget
import config
from alpaca_client import AlpacaClient
import economic_calendar as _ec_mod
from earnings_calendar import detect_pead_setup, is_earnings_imminent
from economic_calendar import is_event_risk_window
from indicator_engine import IndicatorValues, compute_indicators
from macro_regime import load_macro_regime, load_trading_params
from market_data import get_daily_bars, get_intraday_bars, get_latest_price
from tier1_config import is_tier1
from utils.io_utils import load_json, save_json
from signals.time_gate import is_entry_allowed

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

# Prevents duplicate closes when position_monitor and trading_cycle fire simultaneously.
# Held only for the file read-modify-write; never during API calls.
_POSITIONS_LOCK = threading.Lock()


# ── Persistence helpers ──────────────────────────────────────────────────────

def load_watch_list() -> list[str]:
    if config.WATCH_LIST_FILE.exists():
        return [t.strip().upper() for t in config.WATCH_LIST_FILE.read_text().splitlines() if t.strip()]
    return []


def load_research() -> dict:
    data = load_json(config.RESEARCH_REPORT_FILE, {})
    symbols = [k for k in data if k != "__meta__"]
    if not symbols:
        logger.warning("Research report is empty — Claude will have no sentiment context this cycle")
        return data
    try:
        ts_str = data.get("__meta__", {}).get("timestamp") or next(
            (data[s].get("timestamp") for s in symbols if data[s].get("timestamp")), None
        )
        if ts_str:
            report_dt = datetime.fromisoformat(ts_str)
            if report_dt.tzinfo is None:
                report_dt = report_dt.replace(tzinfo=ET)
            age_hours = (datetime.now(ET) - report_dt).total_seconds() / 3600
            if age_hours > 6:
                logger.warning(
                    f"Research report is {age_hours:.1f}h old — sentiment may not reflect current conditions"
                )
    except Exception:
        pass
    return data


def load_open_positions() -> dict:
    return load_json(config.OPEN_POSITIONS_FILE, {})


def save_open_positions(positions: dict):
    save_json(config.OPEN_POSITIONS_FILE, positions)


def load_weights() -> dict:
    return load_json(config.INDICATOR_WEIGHTS_FILE, {})


def load_daily_closed() -> set[str]:
    data = load_json(config.DAILY_CLOSED_FILE, [])
    return set(data)


def record_daily_closed(symbol: str) -> None:
    closed = load_daily_closed()
    closed.add(symbol)
    save_json(config.DAILY_CLOSED_FILE, list(closed))


def reset_daily_closed() -> None:
    save_json(config.DAILY_CLOSED_FILE, [])
    logger.info("Daily closed list reset for new trading day.")


def append_trade(entry: dict):
    journal = load_json(config.TRADE_JOURNAL_FILE, [])
    journal.append(entry)
    save_json(config.TRADE_JOURNAL_FILE, journal)


def archive_old_journal_entries() -> None:
    """
    Move entries older than TRADE_JOURNAL_MAX_DAYS from trade_journal.json to
    trade_journal_archive.json. Keeps the active file small for fast reads.
    """
    from datetime import date, timezone
    cutoff = datetime.now(ET) - timedelta(days=config.TRADE_JOURNAL_MAX_DAYS)
    journal = load_json(config.TRADE_JOURNAL_FILE, [])
    recent = [e for e in journal if _entry_timestamp(e) >= cutoff]
    old    = [e for e in journal if _entry_timestamp(e) < cutoff]
    if not old:
        return
    archive_path = config.BASE_DIR / "trade_journal_archive.json"
    archived = load_json(archive_path, [])
    save_json(archive_path, archived + old)
    save_json(config.TRADE_JOURNAL_FILE, recent)
    logger.info(f"Journal hygiene: archived {len(old)} entries → trade_journal_archive.json")


def _entry_timestamp(entry: dict) -> datetime:
    try:
        ts = entry.get("timestamp", "")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ET)
        return dt
    except (ValueError, TypeError):
        return datetime.min.replace(tzinfo=ET)


# ── EOD close helpers ────────────────────────────────────────────────────────

def _eod_close_reason(pnl_pct: float) -> str:
    if pnl_pct > config.EOD_PROFITABLE_THRESHOLD:
        return "eod_force_profitable"
    if pnl_pct < config.EOD_LOSS_THRESHOLD:
        return "eod_force_loss"
    return "eod_force_flat"


# ── Mode routing ─────────────────────────────────────────────────────────────

_HYBRID_REGIMES     = {"BULL_TREND", "OPPORTUNITY"}
_SHORT_TERM_REGIMES = {"CAPITULATION", "CRASH_RECOVERY"}

_SL_TP_TABLE: dict[tuple[str, str], tuple[float, float]] = {
    ("DAY_TRADE",  "NORMAL"):              (3.0,  5.0),
    ("DAY_TRADE",  "CHOPPY"):              (5.0,  6.0),
    ("DAY_TRADE",  "DEFENSIVE"):           (4.0,  5.0),
    ("DAY_TRADE",  "BULL_TREND_PARABOLIC"):(3.0,  5.0),
    ("HYBRID",     "BULL_TREND"):          (7.0, 15.0),
    ("HYBRID",     "OPPORTUNITY"):         (7.0, 15.0),
    ("SHORT_TERM", "CAPITULATION"):        (12.0, 20.0),
    ("SHORT_TERM", "CRASH_RECOVERY"):      (10.0, 18.0),
    ("PEAD",       "any"):                 (8.0,  8.0),
}

_MODE_MAX_PCT = {
    "DAY_TRADE":  0.05,
    "HYBRID":     0.03,
    "SHORT_TERM": 0.025,
    "PEAD":       0.025,
}


def get_active_mode(regime: str, symbol: str | None = None) -> str:
    """
    Returns the active trading mode for this regime/symbol combination.
    PEAD overrides if symbol qualifies and regime is not CRISIS.
    """
    if symbol and regime != "CRISIS":
        pead_ok, _score = detect_pead_setup(symbol)
        if pead_ok and is_tier1(symbol):
            return "PEAD"
    if regime in _HYBRID_REGIMES:
        return "HYBRID"
    if regime in _SHORT_TERM_REGIMES:
        return "SHORT_TERM"
    if regime == "CRISIS":
        return "CASH"
    return "DAY_TRADE"


def get_sl_tp_for_mode_and_regime(mode: str, regime: str) -> tuple[float, float]:
    """Return (stop_loss_pct, take_profit_pct) for the given mode/regime pair."""
    key = (mode, regime)
    if key in _SL_TP_TABLE:
        return _SL_TP_TABLE[key]
    # Try mode with "any" for PEAD
    if (mode, "any") in _SL_TP_TABLE:
        return _SL_TP_TABLE[(mode, "any")]
    return config.STOP_LOSS_PCT, config.TAKE_PROFIT_PCT


def _top_indicators_aligned(iv: IndicatorValues) -> bool:
    """True if price > VWAP, RSI > 50, and MACD histogram positive."""
    return (
        iv.current_price > iv.vwap
        and iv.rsi > 50
        and iv.macd_hist > 0
    )


def _prefilter_decision(iv: IndicatorValues, sentiment_score: int = 0) -> str:
    """
    Returns AUTO_SKIP | AUTO_BUY | ASK_CLAUDE.
    AUTO_BUY when composite exceeds the high-confidence threshold, all top
    indicators are aligned, and sentiment is not bearish.
    Entry gates still apply in Phase 2 before buy.
    """
    if iv.composite_score <= config.COMPOSITE_SCORE_THRESHOLD:
        return "AUTO_SKIP"
    if (
        iv.composite_score > config.AUTO_BUY_COMPOSITE_THRESHOLD
        and _top_indicators_aligned(iv)
        and sentiment_score >= config.AUTO_BUY_MIN_SENTIMENT
    ):
        return "AUTO_BUY"
    return "ASK_CLAUDE"


def _check_entry_gates(
    symbol: str,
    regime: str,
    mode: str,
    iv: IndicatorValues,
) -> tuple[bool, str]:
    """
    Priority gates (first match wins — False = block entry):
    1. Time gate (first ENTRY_GATE_OPEN_MINUTES after open and last ENTRY_GATE_CLOSE_MINUTES before close)
    2. Earnings imminent (earnings-safe modes exempt)
    3. Event risk window (FOMC/NFP)
    4. Stop-hunt zone
    5. Bot herding score HIGH (blocks unless squeeze overrides)
    6. Momentum exhaustion (blocks unless squeeze overrides)
    """
    now = datetime.now(ET)

    # 1. Time gate
    if not is_entry_allowed(now):
        return False, "outside entry time window"

    # 2. Earnings imminent
    earnings_days = 1 if mode not in ("HYBRID", "SHORT_TERM") else 2
    if mode not in ("CAPITULATION", "PEAD") and is_earnings_imminent(symbol, days=earnings_days):
        return False, f"earnings within {earnings_days} day(s)"

    # 3. Event risk window
    event_risk, event_name = is_event_risk_window(now)
    if event_risk:
        return False, f"event risk window: {event_name}"

    # 4. Stop-hunt zone
    if iv.stop_hunt_zone:
        return False, "stop-hunt zone (round number or 5d hi/lo proximity)"

    # 5. Bot herding — blocks high-herding entries unless squeeze
    if not iv.squeeze_setup:
        if iv.bot_herding_score >= config.BOT_HERD_HIGH_SCORE:
            return False, f"bot herding too high ({iv.bot_herding_score})"

        # 6. Momentum exhaustion — skip when herding present
        if iv.momentum_exhaustion and iv.bot_herding_score >= config.BOT_HERD_ALERT_SCORE:
            return False, f"momentum exhaustion + herding ({iv.bot_herding_score})"

    return True, ""


def _build_market_context_block(
    regime_info: dict,
    fred_cache: dict,
    research_meta: dict,
    now: datetime,
) -> str:
    """
    Assemble a compact (~150 token) market context string for the Claude system prompt.
    Pure — no file I/O; callers load the dicts and pass them in.
    """
    regime = regime_info.get("regime", "NORMAL")
    health = regime_info.get("macro_health_score", 0)
    vix    = regime_info.get("vix_current", 0.0)

    fed   = fred_cache.get("fed_direction", "unknown")
    cpi   = fred_cache.get("cpi_trend", "unknown")
    unemp = "rising" if fred_cache.get("unemployment_rising") else "stable"

    event_active, event_name = _ec_mod.is_event_risk_window(now)
    if event_active:
        event_line = f"⚠ ACTIVE: {event_name} — avoid new entries"
    else:
        event_line = "None active"

    shock = research_meta.get("macro_shock_score", 0)
    shock_line = f"{shock}/100" + (" — elevated macro stress" if shock >= 40 else "")

    sectors = research_meta.get("sector_sentiment", {})
    sector_str = " ".join(f"{s}={v:+.0f}" for s, v in sectors.items()) or "N/A"

    caps = research_meta.get("capitulation_candidates", [])
    cap_str = ", ".join(caps) if caps else "None"

    return (
        f"MARKET CONTEXT ({now.strftime('%H:%M')} ET):\n"
        f"Regime: {regime} | health={health:+d} | VIX={vix:.1f}\n"
        f"FRED: Fed {fed} | CPI {cpi} | Unemployment {unemp}\n"
        f"Event risk: {event_line}\n"
        f"Macro shock: {shock_line}\n"
        f"Sector sentiment: {sector_str}\n"
        f"Capitulation watch: {cap_str}"
    )


def compute_position_size_for_mode(
    portfolio_value: float,
    price: float,
    atr: float,
    mode: str,
    bot_herding_score: int,
    squeeze_setup: bool,
) -> int:
    """Risk-based sizing capped by mode's max % of portfolio."""
    risk_amount   = portfolio_value * config.RISK_PER_TRADE_PCT
    shares_by_risk = risk_amount / (atr if atr > 0 else price * 0.02)
    max_pct        = _MODE_MAX_PCT.get(mode, config.MAX_POSITION_PCT)
    shares_by_pct  = (portfolio_value * max_pct) / price
    qty = max(1.0, round(min(shares_by_risk, shares_by_pct)))

    # Reduce size when bot herding is elevated (but not squeeze)
    if not squeeze_setup and bot_herding_score > config.BOT_HERD_ALERT_SCORE:
        qty = max(1.0, round(qty * 0.70))

    return int(qty)



# ── Claude decision (batched) ────────────────────────────────────────────────

def _build_stock_block(
    symbol: str,
    iv: IndicatorValues,
    research: dict,
    existing_pos: dict | None,
) -> str:
    price = iv.current_price
    research_sym = research.get(symbol, {})
    sentiment_score = research_sym.get("sentiment_score", 0)
    sentiment_label = research_sym.get("sentiment_label", "NEUTRAL")
    risk_flag = research_sym.get("risk_flag") or "None"
    catalyst = research_sym.get("key_catalyst") or "None"
    news_summary = research_sym.get("summary") or "No summary."

    pos_block = ""
    if existing_pos:
        pnl_pct = (price - existing_pos["entry_price"]) / existing_pos["entry_price"] * 100
        pos_block = (
            f" | OPEN POS: entry=${existing_pos['entry_price']:.2f} qty={existing_pos['qty']:.0f}"
            f" P&L={pnl_pct:+.2f}% SL={existing_pos.get('stop_loss_pct', config.STOP_LOSS_PCT):.1f}%"
            f" TP={existing_pos.get('take_profit_pct', config.TAKE_PROFIT_PCT):.1f}%"
        )

    dvol = f"{iv.daily_volume_ratio:.2f}x"
    if iv.daily_volume_ratio < 0.5:
        dvol += " LOW"
    elif iv.daily_volume_ratio > 3.0:
        dvol += " HIGH"

    pead_ok, pead_score = detect_pead_setup(symbol)
    pead_line = (
        f"\n  PEAD: {pead_score}/100 conditions met — expect multi-day drift"
        if pead_ok else ""
    )

    return (
        f"=={symbol}== composite={iv.composite_score:+.1f} signal={iv.signal}{pos_block}\n"
        f"  P=${price:.2f} VWAP=${iv.vwap:.2f}({'↑' if price>=iv.vwap else '↓'})"
        f" RSI={iv.rsi:.1f}(Δ{iv.rsi_slope:+.1f}) MACD={iv.macd_hist:.4f}"
        f" BB=[{iv.bb_lower:.2f},{iv.bb_upper:.2f}] EMA9={iv.ema9:.2f} EMA21={iv.ema21:.2f}"
        f" ATR={iv.atr:.4f} Stoch={iv.stoch_k:.1f}/{iv.stoch_d:.1f} Vol={dvol}\n"
        f"  NEWS: {sentiment_label}({sentiment_score:+d}) catalyst={catalyst} risk={risk_flag}\n"
        f"  {news_summary}"
        f"{pead_line}"
    )


# ── Claude circuit breaker ───────────────────────────────────────────────────
# Set when the API returns a hard error (credit exhausted, invalid key, etc.)
# Prevents hammering a dead API every 5 minutes.
_claude_disabled: bool = False
_claude_disabled_reason: str = ""


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
    system_prompt_tokens  = 1500
    strategy_mem_tokens   = 400
    stock_block_tokens    = 200 * batch_size
    output_tokens         = 300 * batch_size
    input_tokens = system_prompt_tokens + strategy_mem_tokens + stock_block_tokens
    # Cache read assumed for system (after first call)
    return (
        input_tokens   * 3.00    / 1_000_000
        + output_tokens * 15.00  / 1_000_000
    )


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
    """Best-effort regime read for budget ceiling decision."""
    try:
        return load_macro_regime().get("regime", "NORMAL")
    except Exception:
        return "NORMAL"


def _get_current_regime_info() -> dict:
    """Best-effort regime read — returns full dict for context block building."""
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
    global _claude_disabled, _claude_disabled_reason
    symbols = [sym for sym, _, _ in batch]

    if _claude_disabled:
        logger.warning(f"Claude disabled ({_claude_disabled_reason}) — HOLD for {symbols}")
        return {sym: _default_hold(sym) for sym in symbols}

    # Budget check before API call
    estimated_cost = _estimate_batch_call_cost(len(batch))
    gain_pct = _get_portfolio_intraday_gain()
    regime   = _get_current_regime()
    if not api_budget.check_budget(estimated_cost, gain_pct, regime):
        logger.warning(f"API budget blocked — HOLD for {symbols}")
        return {sym: _default_hold(sym) for sym in symbols}

    stock_blocks = "\n\n".join(
        _build_stock_block(sym, iv, research, existing_pos)
        for sym, iv, existing_pos in batch
    )

    user_msg = (
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

    now = datetime.now(ET)
    market_ctx = _build_market_context_block(
        _get_current_regime_info(), fred_cache, research_meta, now
    )
    system_blocks = [
        {
            "type": "text",
            "text": (
                "You are an expert intraday day trader making final buy/sell decisions.\n"
                "You receive technical indicators, news sentiment, and market context.\n"
                "You may OVERRIDE technical signals when news sentiment strongly contradicts them.\n"
                "Prioritise capital preservation. Never hold overnight.\n"
                f"Current indicator weights: {json.dumps(weights)}\n\n"
                f"{market_ctx}"
            ),
        }
    ]
    if strategy_memory:
        block: dict = {
            "type": "text",
            "text": f"Today's strategy memory:\n{strategy_memory}",
        }
        # Only request caching when the block is large enough (~1024+ tokens ≈ 4000 chars)
        if len(strategy_memory) >= 3000:
            block["cache_control"] = {"type": "ephemeral"}
        system_blocks.append(block)

    try:
        response = ai.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=300 * len(batch),
            system=system_blocks,
            messages=[{"role": "user", "content": user_msg}],
        )
    except anthropic.APIStatusError as exc:
        if exc.status_code in (402, 429):
            # Payment required or rate limit — disable Claude for the session
            _claude_disabled = True
            _claude_disabled_reason = f"HTTP {exc.status_code} — {exc.message[:120]}"
            logger.error(
                f"CLAUDE API ERROR — circuit breaker tripped: {_claude_disabled_reason}\n"
                f"  Resolve at https://console.anthropic.com — all Claude calls suspended until restart."
            )
            telegram_notify.notify_claude_disabled(_claude_disabled_reason)
        else:
            # HTTP 400 and other errors are transient (bad request, format issue) — log only
            logger.error(f"Claude API error HTTP {exc.status_code}: {exc.message[:200]}")
        return {sym: _default_hold(sym) for sym in symbols}
    except Exception as exc:
        logger.error(f"Claude API unexpected error: {type(exc).__name__}: {exc}")
        return {sym: _default_hold(sym) for sym in symbols}

    # Record usage for budget tracking
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

    text = response.content[0].text.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    logger.warning(f"Claude JSON parse error for batch {symbols} — defaulting to HOLD")
    return {sym: _default_hold(sym) for sym in symbols}


# ── Close helper ─────────────────────────────────────────────────────────────

def _close_position(
    symbol: str,
    alpaca: AlpacaClient,
    open_positions: dict,
    current_price: float,
    iv: IndicatorValues | None,
    reason: str,
):
    # Hold the lock only for the file read-modify-write — not during the API call.
    # Re-reading from disk inside the lock is the key: if another thread already
    # removed this symbol, we see that immediately and abort instead of double-closing.
    with _POSITIONS_LOCK:
        disk_positions = load_json(config.OPEN_POSITIONS_FILE, {})
        if symbol not in disk_positions:
            open_positions.pop(symbol, None)  # sync caller's in-memory dict
            logger.debug(f"_close_position: {symbol} already closed by concurrent thread — skip.")
            return
        pos = disk_positions.pop(symbol)
        open_positions.pop(symbol, None)  # sync caller's in-memory dict
        save_json(config.OPEN_POSITIONS_FILE, disk_positions)

    # API call and journaling are outside the lock to avoid blocking other threads.
    alpaca.close_position(symbol)

    entry_price = pos["entry_price"]
    qty = pos["qty"]
    pnl_pct = (current_price - entry_price) / entry_price * 100
    pnl_dollar = (current_price - entry_price) * qty

    append_trade({
        "type": "CLOSE",
        "symbol": symbol,
        "timestamp": datetime.now(ET).isoformat(),
        "exit_price": round(current_price, 4),
        "entry_price": round(entry_price, 4),
        "qty": qty,
        "pnl_pct": round(pnl_pct, 2),
        "pnl_dollar": round(pnl_dollar, 2),
        "close_reason": reason,
        "entry_composite_score": pos.get("entry_composite_score"),
        "exit_composite_score": iv.composite_score if iv else None,
        "exit_indicators": asdict(iv) if iv else {},
        "entry_indicators": pos.get("entry_indicators", {}),
        "entry_sentiment_score": pos.get("entry_sentiment_score"),
        "claude_reasoning": pos.get("claude_reasoning", ""),
        "indicator_signals_at_entry": pos.get("indicator_signals", {}),
        "mode_at_entry": pos.get("mode_at_entry"),
        "regime_at_entry": pos.get("regime_at_entry"),
    })

    record_daily_closed(symbol)
    logger.info(f"CLOSE {symbol}: P&L={pnl_pct:+.2f}% (${pnl_dollar:+.2f}) | {reason} | no re-entry today")
    telegram_notify.notify_close(
        symbol=symbol, qty=qty,
        entry=entry_price, exit_price=current_price,
        pnl_dollar=round(pnl_dollar, 2), pnl_pct=round(pnl_pct, 2),
        reason=reason,
    )


# ── Position monitor (runs every 60 s) ───────────────────────────────────────

def run_position_monitor():
    """
    Two-layer intra-cycle protection:

    Layer 1 — Alpaca bracket reconciliation
        When we enter, we place a native bracket order (stop-loss + take-profit)
        directly with Alpaca. Alpaca executes these server-side the instant
        price crosses either level — no polling delay at all.
        This monitor detects when Alpaca has already closed a position via a
        bracket fill and syncs our local state accordingly.

    Layer 2 — Price-level safety net
        Even if bracket orders are pending or partially filled, this monitor
        checks live prices against our stored SL/TP levels every 60 seconds
        and manually closes anything that has breached a level.
    """
    open_positions = load_open_positions()
    if not open_positions:
        return

    alpaca = AlpacaClient()
    alpaca_positions = alpaca.get_positions()  # symbols currently held by Alpaca
    now = datetime.now(ET)

    for symbol, pos in list(open_positions.items()):

        # ── Layer 1: bracket fill reconciliation ──────────────────────────
        # If Alpaca no longer holds the position, a bracket order filled it.
        if symbol not in alpaca_positions:
            # Use the same lock + disk-re-read pattern to prevent duplicate journaling
            # when trading_cycle fires simultaneously and also tries to close this symbol.
            with _POSITIONS_LOCK:
                disk_positions = load_json(config.OPEN_POSITIONS_FILE, {})
                if symbol not in disk_positions:
                    open_positions.pop(symbol, None)
                    continue  # Already handled by a concurrent thread
                pos = disk_positions.pop(symbol)
                open_positions.pop(symbol, None)
                save_json(config.OPEN_POSITIONS_FILE, disk_positions)

            entry_price = pos["entry_price"]
            sl_price = entry_price * (1 - pos.get("stop_loss_pct", config.STOP_LOSS_PCT) / 100)
            tp_price = entry_price * (1 + pos.get("take_profit_pct", config.TAKE_PROFIT_PCT) / 100)

            try:
                current_price = alpaca.get_latest_price(symbol)
            except Exception:
                current_price = entry_price

            if current_price <= sl_price * 1.005:
                close_reason = "bracket_stop_loss_filled"
                exit_price = sl_price
            elif current_price >= tp_price * 0.995:
                close_reason = "bracket_take_profit_filled"
                exit_price = tp_price
            else:
                close_reason = "bracket_filled_unknown_leg"
                exit_price = current_price

            pnl_pct = (exit_price - entry_price) / entry_price * 100
            pnl_dollar = (exit_price - entry_price) * pos["qty"]

            append_trade({
                "type": "CLOSE",
                "symbol": symbol,
                "timestamp": now.isoformat(),
                "exit_price": round(exit_price, 4),
                "entry_price": round(entry_price, 4),
                "qty": pos["qty"],
                "pnl_pct": round(pnl_pct, 2),
                "pnl_dollar": round(pnl_dollar, 2),
                "close_reason": close_reason,
                "entry_composite_score": pos.get("entry_composite_score"),
                "exit_composite_score": None,
                "exit_indicators": {},
                "entry_indicators": pos.get("entry_indicators", {}),
                "entry_sentiment_score": pos.get("entry_sentiment_score"),
                "claude_reasoning": pos.get("claude_reasoning", ""),
                "indicator_signals_at_entry": pos.get("indicator_signals", {}),
            })

            record_daily_closed(symbol)
            logger.info(
                f"BRACKET FILL detected: {symbol} closed via {close_reason} | "
                f"P&L={pnl_pct:+.2f}% (${pnl_dollar:+.2f}) | no re-entry today"
            )
            telegram_notify.notify_close(
                symbol=symbol, qty=pos["qty"],
                entry=entry_price, exit_price=exit_price,
                pnl_dollar=round(pnl_dollar, 2), pnl_pct=round(pnl_pct, 2),
                reason=close_reason,
            )
            continue

        # ── Layer 2: price-level safety net ───────────────────────────────
        # Position still open in Alpaca — check live price against SL/TP.
        try:
            current_price = alpaca.get_latest_price(symbol)
        except Exception as exc:
            logger.warning(f"Position monitor: could not get price for {symbol}: {exc}")
            continue

        entry_price = pos["entry_price"]
        pnl_pct = (current_price - entry_price) / entry_price * 100
        sl_pct = pos.get("stop_loss_pct", config.STOP_LOSS_PCT)
        tp_pct = pos.get("take_profit_pct", config.TAKE_PROFIT_PCT)

        if pnl_pct <= -sl_pct:
            logger.warning(
                f"MONITOR STOP-LOSS: {symbol} P&L={pnl_pct:+.2f}% breached SL={-sl_pct:.1f}% "
                f"— closing immediately (mid-cycle)"
            )
            _close_position(symbol, alpaca, open_positions, current_price, None, "monitor_stop_loss")

        elif pnl_pct >= tp_pct:
            logger.info(
                f"MONITOR TAKE-PROFIT: {symbol} P&L={pnl_pct:+.2f}% hit TP={tp_pct:.1f}% "
                f"— closing immediately (mid-cycle)"
            )
            _close_position(symbol, alpaca, open_positions, current_price, None, "monitor_take_profit")

    save_open_positions(open_positions)

    # ── Swing position tiered TP / trailing stop ──────────────────────────────
    try:
        from swing_manager import monitor_swing_positions
        monitor_swing_positions(alpaca)
    except Exception as exc:
        logger.warning(f"Swing position monitor failed: {exc}")


# ── Flash crash market monitor (every 5 min) ─────────────────────────────────
# Moved to utils/market_monitor.py — re-exported here for backward compatibility.
from utils.market_monitor import (  # noqa: F401
    run_market_monitor,
    _get_spy_prev_close,
    _verify_spy_with_polygon,
    _write_flash_crash_candidate,
    _clear_flash_crash_candidate,
    _read_flash_crash_candidate,
    _trigger_flash_crash_lockout,
    _handle_flash_crash_stage2,
    _set_crisis_lockout,
)


# ── Main trading cycle ────────────────────────────────────────────────────────

def run_trading_cycle():
    now = datetime.now(ET)
    logger.info(f"Trading cycle @ {now.strftime('%H:%M:%S')} ET")

    alpaca = AlpacaClient()
    account = alpaca.get_account()
    portfolio_value = float(account.portfolio_value)
    buying_power = float(account.buying_power)

    # Daily loss circuit-breaker (3%)
    last_equity = float(account.last_equity)
    daily_pnl_pct = (portfolio_value - last_equity) / last_equity * 100 if last_equity else 0
    if daily_pnl_pct <= -(config.MAX_DAILY_LOSS_PCT * 100):
        logger.warning(f"Daily loss limit hit ({daily_pnl_pct:.2f}%). Halting new entries.")
        return

    # Macro regime + learned trading params
    regime_info = load_macro_regime()
    regime      = regime_info.get("regime", "NORMAL")
    params      = load_trading_params()

    try:
        fred_cache = load_json(config.FRED_CACHE_FILE, {})
    except Exception:
        fred_cache = {}

    # Flash crash lockout check
    flash_crash_active = regime_info.get("flash_crash_active", False)
    flash_crash_until  = regime_info.get("flash_crash_until")
    if flash_crash_active and flash_crash_until:
        try:
            until_dt = datetime.fromisoformat(flash_crash_until)
            if now < until_dt:
                logger.warning(f"Flash crash lockout active until {flash_crash_until} — no entries.")
                can_open = False
            else:
                flash_crash_active = False
        except Exception:
            pass

    if regime == "CRISIS" or flash_crash_active:
        logger.warning(
            f"CRISIS/flash-crash regime: VIX={regime_info.get('vix_current', 0):.1f} | "
            f"health={regime_info.get('macro_health_score', 0):+d} — skipping all new entries."
        )
        can_open = False
    else:
        can_open = True

    # Time-of-day gate: only open new positions 10 AM – 3 PM ET
    entry_start = now.replace(hour=params["entry_start_hour"], minute=0, second=0, microsecond=0)
    entry_end   = now.replace(hour=params["entry_end_hour"],   minute=0, second=0, microsecond=0)
    if not (entry_start <= now < entry_end):
        logger.debug(f"Outside entry window ({params['entry_start_hour']}–{params['entry_end_hour']} ET) — no new entries.")
        can_open = False

    # RSI threshold — still use learned params
    rsi_max = (
        params["rsi_buy_max_high_vol"] if regime in ("CRISIS", "DEFENSIVE")
        else params["rsi_buy_max"]
    )

    # Position multiplier — DEFENSIVE/CHOPPY use 0.5× sizing
    pos_mult = params["high_vol_position_mult"] if regime in ("DEFENSIVE", "CHOPPY") else 1.0
    if regime in ("DEFENSIVE", "CHOPPY"):
        logger.info(f"Regime={regime}: position size ×{pos_mult}, RSI max={rsi_max}")

    watch_list = load_watch_list()
    if not watch_list:
        logger.warning("Empty watch list — skipping cycle.")
        return

    research = load_research()
    research_meta = research.get("__meta__", {})
    open_positions = load_open_positions()
    daily_closed = load_daily_closed()
    weights = load_weights()
    strategy_memory = (
        config.STRATEGY_MEMORY_FILE.read_text() if config.STRATEGY_MEMORY_FILE.exists() else ""
    )
    # Truncate strategy memory to avoid token bloat from months of accumulated entries
    if len(strategy_memory) > config.STRATEGY_MEMORY_MAX_CHARS:
        strategy_memory = strategy_memory[-config.STRATEGY_MEMORY_MAX_CHARS:]

    ai = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    # ── Phase 1: Fetch data and compute indicators for all watch-list stocks ──
    def _fetch_symbol_data(symbol: str) -> tuple[str, IndicatorValues, dict | None] | None:
        bars = alpaca.get_intraday_bars(symbol)
        daily_bars = alpaca.get_daily_bars(symbol, days=8)
        iv = compute_indicators(symbol, bars, daily_df=daily_bars)
        if iv is None:
            return None
        return (symbol, iv, open_positions.get(symbol))

    stock_data: list[tuple[str, IndicatorValues, dict | None]] = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(_fetch_symbol_data, sym): sym for sym in watch_list}
        for future in as_completed(futures):
            sym = futures[future]
            try:
                result = future.result()
                if result is not None:
                    stock_data.append(result)
            except Exception as exc:
                logger.error(f"Error fetching data for {sym}: {exc}", exc_info=True)

    if not stock_data:
        logger.warning("No valid indicator data — skipping cycle.")
        return

    # ── Phase 2: Filter to stocks that need a Claude decision ─────────────────
    # Open positions: Alpaca bracket orders enforce SL/TP server-side; the
    # position monitor handles the safety-net layer every 60 s.  Asking Claude
    # every 10 min for a HOLD/CLOSE opinion on an already-bracketed position
    # burns tokens without adding value — the view doesn't change that fast.
    # Claude is only needed to validate potential NEW entries.
    #
    # AUTO_BUY path: composite > AUTO_BUY_COMPOSITE_THRESHOLD + all top indicators
    # aligned → skip Claude, use mode-default SL/TP.  Entry gates still run to
    # block earnings, event-risk, stop-hunt, and bot-herding scenarios.
    needs_claude:      list[tuple[str, IndicatorValues, dict | None]] = []
    auto_buy_decisions: dict[str, dict] = {}

    for symbol, iv, existing_pos in stock_data:
        if existing_pos:
            pnl_pct = (iv.current_price - existing_pos["entry_price"]) / existing_pos["entry_price"] * 100
            logger.info(
                f"BRACKET-MANAGED {symbol}: P&L={pnl_pct:+.2f}%, "
                f"composite={iv.composite_score:+.1f} — Alpaca bracket active, skipping Claude"
            )
        else:
            sentiment_score = research.get(symbol, {}).get("sentiment_score", 0)
            path = _prefilter_decision(iv, sentiment_score)
            if path == "AUTO_BUY":
                mode = get_active_mode(regime, symbol)
                gate_ok, gate_reason = _check_entry_gates(symbol, regime, mode, iv)
                if gate_ok:
                    sl_pct, tp_pct = get_sl_tp_for_mode_and_regime(mode, regime)
                    auto_buy_decisions[symbol] = {
                        "action": "BUY",
                        "confidence": 75,
                        "reasoning": (
                            f"Auto-approved: composite={iv.composite_score:+.1f} "
                            f"with VWAP↑ RSI={iv.rsi:.0f} MACD positive."
                        ),
                        "stop_loss_pct": sl_pct,
                        "take_profit_pct": tp_pct,
                        "indicator_signals": {
                            "VWAP": "correct", "RSI": "correct", "MACD": "correct",
                            "BB": "neutral", "EMA": "correct", "Volume": "neutral",
                        },
                    }
                    logger.info(
                        f"AUTO-BUY {symbol}: composite={iv.composite_score:+.1f} "
                        f"sentiment={sentiment_score:+d} — all top indicators aligned, skipping Claude"
                    )
                else:
                    logger.info(f"AUTO-BUY blocked {symbol}: {gate_reason}")
            elif path == "ASK_CLAUDE":
                needs_claude.append((symbol, iv, None))
            else:
                logger.info(
                    f"SKIP-CLAUDE {symbol}: composite={iv.composite_score:+.1f} — below entry threshold"
                )

    # ── Phase 3: Batch Claude calls (CLAUDE_BATCH_SIZE stocks per call) ───────
    decisions: dict[str, dict] = dict(auto_buy_decisions)  # seed with pre-approved entries
    for i in range(0, len(needs_claude), config.CLAUDE_BATCH_SIZE):
        batch = needs_claude[i : i + config.CLAUDE_BATCH_SIZE]
        batch_decisions = _ask_claude_batch(batch, research, strategy_memory, weights, ai, fred_cache, research_meta)
        decisions.update(batch_decisions)

    # ── Phase 4: Act on decisions ─────────────────────────────────────────────
    for symbol, iv, existing_pos in stock_data:
        try:
            decision = decisions.get(symbol)
            if decision is None:
                # Filtered out in Phase 2 — no position, weak signal → implicit HOLD
                continue

            action = decision.get("action", "HOLD").upper()

            if action == "BUY" and not existing_pos and buying_power >= config.MIN_BUYING_POWER and can_open:
                # No re-entry same day
                if symbol in daily_closed:
                    logger.info(f"SKIP {symbol} — already closed today.")
                    continue
                # RSI overbought filter (regime-adjusted)
                if iv.rsi > rsi_max:
                    logger.info(f"SKIP {symbol} — RSI {iv.rsi:.1f} > {rsi_max} (regime={regime}).")
                    continue
                # VWAP slope filter: skip if VWAP is trending sharply down (downtrend day)
                if iv.vwap_slope < params["vwap_slope_min"]:
                    logger.info(f"SKIP {symbol} — VWAP slope {iv.vwap_slope:.3f}% < {params['vwap_slope_min']}% (downtrend).")
                    continue

                # Mode routing: PEAD > regime-based; CASH blocks all entries
                mode = get_active_mode(regime, symbol)
                if mode == "CASH":
                    logger.info(f"SKIP {symbol} — CASH mode (CRISIS regime).")
                    continue

                # Per-mode concurrent position limits (SHORT_TERM and PEAD are capital-intensive)
                _mode_limits = {"SHORT_TERM": config.SHORT_TERM_MAX_POSITIONS, "PEAD": config.SHORT_TERM_MAX_POSITIONS}
                if mode in _mode_limits:
                    _count = sum(1 for p in open_positions.values() if p.get("mode_at_entry") == mode)
                    _limit = _mode_limits[mode]
                    if _count >= _limit:
                        logger.info(f"SKIP {symbol} — {mode} at position limit ({_count}/{_limit})")
                        continue

                # Bearish sentiment hard veto — blocks even Claude-approved BUYs on bad news
                symbol_sentiment = research.get(symbol, {}).get("sentiment_score", 0)
                if symbol_sentiment < config.AUTO_BUY_MIN_SENTIMENT:
                    logger.info(
                        f"SKIP {symbol} — bearish sentiment ({symbol_sentiment:+d}) blocks BUY"
                    )
                    continue

                # Entry gates: earnings, event risk, stop-hunt, bot herding
                # (For AUTO_BUY these already ran in Phase 2; re-check here is cheap + safe)
                gate_ok, gate_reason = _check_entry_gates(symbol, regime, mode, iv)
                if not gate_ok:
                    logger.info(f"SKIP {symbol} — entry gate: {gate_reason}")
                    continue

                # Effective composite gate — squeeze loosens, herding/choppy tightens
                if iv.squeeze_setup:
                    effective_gate = 12
                elif iv.bot_herding_score >= config.BOT_HERD_ALERT_SCORE:
                    effective_gate = config.HERD_ENTRY_GATE
                elif regime == "CHOPPY":
                    effective_gate = config.CHOPPY_COMPOSITE_GATE
                else:
                    effective_gate = config.COMPOSITE_SCORE_THRESHOLD
                if iv.composite_score < effective_gate:
                    logger.info(
                        f"SKIP {symbol} — composite={iv.composite_score:+.1f} "
                        f"below effective gate {effective_gate} (regime={regime})"
                    )
                    continue

                # Position sizing: mode-aware max %, herding reduction, regime multiplier
                qty = compute_position_size_for_mode(
                    portfolio_value, iv.current_price, iv.atr, mode,
                    iv.bot_herding_score, iv.squeeze_setup,
                )
                qty = max(1, int(round(qty * pos_mult)))

                # SL/TP: mode/regime defaults, Claude override if provided, squeeze fast-exit
                sl_pct, tp_pct = get_sl_tp_for_mode_and_regime(mode, regime)
                if decision.get("stop_loss_pct"):
                    sl_pct = float(decision["stop_loss_pct"])
                if decision.get("take_profit_pct"):
                    tp_pct = float(decision["take_profit_pct"])
                if iv.squeeze_setup and decision.get("confidence", 0) >= config.SQUEEZE_CONFIDENCE_GATE:
                    sl_pct, tp_pct = 6.0, 5.0  # fast trailing exit for squeeze plays

                # Guard against concurrent cycles (same process multiple threads, or multiple
                # processes from bot restarts) both seeing an empty slot and both buying.
                # Re-read from disk inside the lock — same pattern as _close_position.
                with _POSITIONS_LOCK:
                    disk_positions = load_json(config.OPEN_POSITIONS_FILE, {})
                    if symbol in disk_positions:
                        logger.info(f"SKIP {symbol} — already in open positions (concurrent cycle guard).")
                        open_positions[symbol] = disk_positions[symbol]
                        continue

                # Bracket order: Alpaca enforces SL and TP server-side instantly
                order = alpaca.place_bracket_order(
                    symbol=symbol,
                    qty=qty,
                    entry_price=iv.current_price,
                    stop_loss_pct=sl_pct,
                    take_profit_pct=tp_pct,
                )
                position_data = {
                    "entry_price": iv.current_price,
                    "qty": qty,
                    "stop_loss_pct": sl_pct,
                    "take_profit_pct": tp_pct,
                    "stop_loss_price": order["stop_loss_price"],
                    "take_profit_price": order["take_profit_price"],
                    "entry_time": now.isoformat(),
                    "entry_composite_score": iv.composite_score,
                    "entry_indicators": asdict(iv),
                    "entry_sentiment_score": research.get(symbol, {}).get("sentiment_score"),
                    "claude_reasoning": decision.get("reasoning", ""),
                    "indicator_signals": decision.get("indicator_signals", {}),
                    "order_id": order.get("id"),
                    "mode_at_entry": mode,
                    "regime_at_entry": regime,
                }
                open_positions[symbol] = position_data
                buying_power -= iv.current_price * qty

                # Merge-write: read current disk state, add new position, write back.
                # Avoids overwriting concurrent closes made by position_monitor.
                with _POSITIONS_LOCK:
                    disk_positions = load_json(config.OPEN_POSITIONS_FILE, {})
                    disk_positions[symbol] = position_data
                    save_json(config.OPEN_POSITIONS_FILE, disk_positions)

                append_trade({
                    "type": "BUY",
                    "symbol": symbol,
                    "timestamp": now.isoformat(),
                    "price": round(iv.current_price, 4),
                    "qty": qty,
                    "composite_score": iv.composite_score,
                    "indicators": asdict(iv),
                    "sentiment_score": research.get(symbol, {}).get("sentiment_score"),
                    "claude_reasoning": decision.get("reasoning", ""),
                    "claude_confidence": decision.get("confidence", 0),
                    "indicator_signals": decision.get("indicator_signals", {}),
                    "stop_loss_pct": sl_pct,
                    "take_profit_pct": tp_pct,
                    "stop_loss_price": order["stop_loss_price"],
                    "take_profit_price": order["take_profit_price"],
                    "vix_regime": regime,
                    "mode_at_entry": mode,
                    "vix_level": regime_info.get("vix_current"),
                    "vwap_slope": iv.vwap_slope,
                    "pnl_pct": None,
                    "pnl_dollar": None,
                })
                logger.info(
                    f"BUY {qty:.0f}x {symbol} @ ~${iv.current_price:.2f} | mode={mode} | "
                    f"SL=${order['stop_loss_price']} TP=${order['take_profit_price']} | "
                    f"{decision.get('reasoning', '')}"
                )
                telegram_notify.notify_buy(
                    symbol=symbol, qty=qty, price=iv.current_price,
                    sl=order["stop_loss_price"], tp=order["take_profit_price"],
                    mode=mode, score=iv.composite_score,
                    confidence=decision.get("confidence", 0),
                )

            elif action == "CLOSE" and existing_pos:
                # _close_position handles its own locked merge-write; no extra save needed.
                _close_position(symbol, alpaca, open_positions, iv.current_price, iv, "claude_decision")
            elif action == "HOLD":
                logger.info(
                    f"HOLD {symbol}: composite={iv.composite_score:+.1f}, RSI={iv.rsi:.1f} | "
                    f"{decision.get('reasoning', '')[:120]}"
                )

        except Exception as exc:
            logger.error(f"Error processing {symbol}: {exc}", exc_info=True)


# ── End-of-day force close ───────────────────────────────────────────────────

def force_close_all():
    logger.info("Force-closing all day-trade positions @ 3:55 PM ET")
    alpaca = AlpacaClient()

    # Cancel all open orders first (including any pending bracket legs)
    alpaca.cancel_all_orders()

    # Symbols promoted to overnight swing survive 3:55 PM — swing_manager owns them
    try:
        from swing_manager import load_swing_positions
        swing_symbols = set(load_swing_positions().keys())
    except Exception:
        swing_symbols = set()
    if swing_symbols:
        logger.info(f"Preserving {len(swing_symbols)} swing position(s): {sorted(swing_symbols)}")

    open_positions = load_open_positions()

    for symbol, pos in list(open_positions.items()):
        if symbol in swing_symbols:
            continue  # promoted to swing — do not journal or close

        # Fix A — Phantom journal skip: verify symbol exists in live Alpaca positions
        if config.EOD_VERIFY_FILLS:
            live_symbols = set(alpaca.get_positions().keys())
            if symbol not in live_symbols:
                logger.warning(
                    "EOD: %s not in Alpaca positions (phantom entry) — skipping journal", symbol
                )
                open_positions.pop(symbol, None)
                continue

        try:
            price = alpaca.get_latest_price(symbol)
        except Exception:
            price = pos["entry_price"]
            logger.warning(f"Could not get price for {symbol} — using entry price for P&L calc.")

        pnl_pct = (price - pos["entry_price"]) / pos["entry_price"] * 100
        pnl_dollar = (price - pos["entry_price"]) * pos["qty"]

        # Fix C — Retry on close failure
        try:
            alpaca.close_position(symbol)
        except Exception as e:
            logger.warning("EOD close failed for %s, retrying: %s", symbol, e)
            time.sleep(2)
            try:
                alpaca.close_position(symbol)
            except Exception as e2:
                logger.error("EOD close retry failed for %s: %s", symbol, e2)
                try:
                    alpaca.close_all_positions()
                except Exception:
                    pass
                try:
                    remaining = list(alpaca.get_positions().keys())
                    if remaining:
                        telegram_notify._send(f"⚠️ EOD close failed, positions still open: {remaining}")
                except Exception:
                    pass

        # Fix B — Close reason classification
        append_trade({
            "type": "CLOSE",
            "symbol": symbol,
            "timestamp": datetime.now(ET).isoformat(),
            "exit_price": round(price, 4),
            "entry_price": round(pos["entry_price"], 4),
            "qty": pos["qty"],
            "pnl_pct": round(pnl_pct, 2),
            "pnl_dollar": round(pnl_dollar, 2),
            "close_reason": _eod_close_reason(pnl_pct),
            "entry_composite_score": pos.get("entry_composite_score"),
            "exit_composite_score": None,
            "exit_indicators": {},
            "entry_indicators": pos.get("entry_indicators", {}),
            "entry_sentiment_score": pos.get("entry_sentiment_score"),
            "claude_reasoning": pos.get("claude_reasoning", ""),
            "indicator_signals_at_entry": pos.get("indicator_signals", {}),
            "mode_at_entry": pos.get("mode_at_entry"),
            "regime_at_entry": pos.get("regime_at_entry"),
        })
        logger.info(f"EOD close {symbol}: P&L={pnl_pct:+.2f}% (${pnl_dollar:+.2f})")

    # Also sweep any Alpaca positions not in open_positions.json (reconciliation gap)
    try:
        alpaca_positions = alpaca.get_positions()
        for sym in list(alpaca_positions.keys()):
            if sym not in swing_symbols and sym not in open_positions:
                try:
                    alpaca.close_position(sym)
                    logger.warning(f"EOD: closed untracked Alpaca position {sym}")
                except Exception as exc:
                    logger.warning(f"EOD: could not close {sym} in Alpaca: {exc}")
    except Exception as exc:
        logger.warning(f"EOD: get_positions failed: {exc}")

    # Clear local state — swing positions remain in swing_positions.json
    remaining = {sym: pos for sym, pos in open_positions.items() if sym in swing_symbols}
    save_open_positions(remaining)
    logger.info(
        f"Day-trade positions force-closed. "
        f"{len(swing_symbols)} swing position(s) preserved."
    )


if __name__ == "__main__":
    import logging as _l
    _l.basicConfig(level=_l.INFO)
    run_trading_cycle()
