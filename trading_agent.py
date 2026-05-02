import json
import logging
import re
import threading
from dataclasses import asdict
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic

import config
from alpaca_client import AlpacaClient
from indicator_engine import IndicatorValues, compute_indicators
from vix_regime import load_vix_regime, load_trading_params

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

# Prevents duplicate closes when position_monitor and trading_cycle fire simultaneously.
# Held only for the file read-modify-write; never during API calls.
_POSITIONS_LOCK = threading.Lock()


# ── Persistence helpers ──────────────────────────────────────────────────────

def _load_json(path, default):
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return default


def _save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_watch_list() -> list[str]:
    if config.WATCH_LIST_FILE.exists():
        return [t.strip().upper() for t in config.WATCH_LIST_FILE.read_text().splitlines() if t.strip()]
    return []


def load_research() -> dict:
    return _load_json(config.RESEARCH_REPORT_FILE, {})


def load_open_positions() -> dict:
    return _load_json(config.OPEN_POSITIONS_FILE, {})


def save_open_positions(positions: dict):
    _save_json(config.OPEN_POSITIONS_FILE, positions)


def load_weights() -> dict:
    return _load_json(config.INDICATOR_WEIGHTS_FILE, {})


def load_daily_closed() -> set[str]:
    data = _load_json(config.DAILY_CLOSED_FILE, [])
    return set(data)


def record_daily_closed(symbol: str) -> None:
    closed = load_daily_closed()
    closed.add(symbol)
    _save_json(config.DAILY_CLOSED_FILE, list(closed))


def reset_daily_closed() -> None:
    _save_json(config.DAILY_CLOSED_FILE, [])
    logger.info("Daily closed list reset for new trading day.")


def append_trade(entry: dict):
    journal = _load_json(config.TRADE_JOURNAL_FILE, [])
    journal.append(entry)
    _save_json(config.TRADE_JOURNAL_FILE, journal)


# ── Position sizing ──────────────────────────────────────────────────────────

def _position_size(portfolio_value: float, price: float, atr: float) -> float:
    risk_amount = portfolio_value * config.RISK_PER_TRADE_PCT
    shares_by_risk = risk_amount / (atr if atr > 0 else price * 0.02)
    shares_by_pct = (portfolio_value * config.MAX_POSITION_PCT) / price
    return max(1.0, round(min(shares_by_risk, shares_by_pct)))


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

    return (
        f"=={symbol}== composite={iv.composite_score:+.1f} signal={iv.signal}{pos_block}\n"
        f"  P=${price:.2f} VWAP=${iv.vwap:.2f}({'↑' if price>=iv.vwap else '↓'})"
        f" RSI={iv.rsi:.1f}(Δ{iv.rsi_slope:+.1f}) MACD={iv.macd_hist:.4f}"
        f" BB=[{iv.bb_lower:.2f},{iv.bb_upper:.2f}] EMA9={iv.ema9:.2f} EMA21={iv.ema21:.2f}"
        f" ATR={iv.atr:.4f} Stoch={iv.stoch_k:.1f}/{iv.stoch_d:.1f} Vol={dvol}\n"
        f"  NEWS: {sentiment_label}({sentiment_score:+d}) catalyst={catalyst} risk={risk_flag}\n"
        f"  {news_summary}"
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


def _ask_claude_batch(
    batch: list[tuple[str, IndicatorValues, dict | None]],
    research: dict,
    strategy_memory: str,
    weights: dict,
    ai: anthropic.Anthropic,
) -> dict[str, dict]:
    """Ask Claude for decisions on multiple stocks in a single API call."""
    global _claude_disabled, _claude_disabled_reason
    symbols = [sym for sym, _, _ in batch]

    if _claude_disabled:
        logger.warning(f"Claude disabled ({_claude_disabled_reason}) — HOLD for {symbols}")
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

    system_blocks = [
        {
            "type": "text",
            "text": (
                "You are an expert intraday day trader making final buy/sell decisions.\n"
                "You receive technical indicators, news sentiment, and a strategy memory.\n"
                "You may OVERRIDE technical signals when news sentiment strongly contradicts them.\n"
                "Prioritise capital preservation. Never hold overnight.\n"
                f"Current indicator weights: {json.dumps(weights)}"
            ),
            "cache_control": {"type": "ephemeral"},
        }
    ]
    if strategy_memory:
        system_blocks.append(
            {
                "type": "text",
                "text": f"Today's strategy memory:\n{strategy_memory}",
                "cache_control": {"type": "ephemeral"},
            }
        )

    try:
        response = ai.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=300 * len(batch),
            system=system_blocks,
            messages=[{"role": "user", "content": user_msg}],
        )
    except anthropic.APIStatusError as exc:
        if exc.status_code in (400, 402, 429):
            _claude_disabled = True
            _claude_disabled_reason = f"HTTP {exc.status_code} — {exc.message[:120]}"
            logger.error(
                f"CLAUDE API ERROR — circuit breaker tripped: {_claude_disabled_reason}\n"
                f"  Resolve at https://console.anthropic.com — all Claude calls suspended until restart."
            )
        else:
            logger.error(f"Claude API error HTTP {exc.status_code}: {exc.message[:200]}")
        return {sym: _default_hold(sym) for sym in symbols}
    except Exception as exc:
        logger.error(f"Claude API unexpected error: {type(exc).__name__}: {exc}")
        return {sym: _default_hold(sym) for sym in symbols}

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
        disk_positions = _load_json(config.OPEN_POSITIONS_FILE, {})
        if symbol not in disk_positions:
            open_positions.pop(symbol, None)  # sync caller's in-memory dict
            logger.debug(f"_close_position: {symbol} already closed by concurrent thread — skip.")
            return
        pos = disk_positions.pop(symbol)
        open_positions.pop(symbol, None)  # sync caller's in-memory dict
        _save_json(config.OPEN_POSITIONS_FILE, disk_positions)

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
    })

    record_daily_closed(symbol)
    logger.info(f"CLOSE {symbol}: P&L={pnl_pct:+.2f}% (${pnl_dollar:+.2f}) | {reason} | no re-entry today")


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
                disk_positions = _load_json(config.OPEN_POSITIONS_FILE, {})
                if symbol not in disk_positions:
                    open_positions.pop(symbol, None)
                    continue  # Already handled by a concurrent thread
                pos = disk_positions.pop(symbol)
                open_positions.pop(symbol, None)
                _save_json(config.OPEN_POSITIONS_FILE, disk_positions)

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

    # VIX regime + learned trading params
    regime_info = load_vix_regime()
    regime = regime_info.get("regime", "NORMAL")
    params = load_trading_params()

    if regime == "CRISIS":
        logger.warning(
            f"CRISIS regime: VIX={regime_info['vix_current']:.1f} rising, "
            f"SPY={regime_info['spy_change_pct']:+.2f}% — skipping all new entries."
        )
        # Still run the position monitor logic below; just skip new BUYs
        can_open = False
    else:
        can_open = True

    # Time-of-day gate: only open new positions 10 AM – 3 PM ET
    entry_start = now.replace(hour=params["entry_start_hour"], minute=0, second=0, microsecond=0)
    entry_end   = now.replace(hour=params["entry_end_hour"],   minute=0, second=0, microsecond=0)
    in_entry_window = entry_start <= now < entry_end
    if not in_entry_window:
        logger.debug(f"Outside entry window ({params['entry_start_hour']}–{params['entry_end_hour']} ET) — no new entries.")
        can_open = False

    # RSI threshold depends on regime
    rsi_max = (
        params["rsi_buy_max_high_vol"] if regime in ("HIGH_VOL", "CRISIS")
        else params["rsi_buy_max"]
    )

    # Position size multiplier (halved in HIGH_VOL / CRISIS)
    pos_mult = params["high_vol_position_mult"] if regime in ("HIGH_VOL", "CRISIS") else 1.0
    if regime in ("HIGH_VOL", "CRISIS"):
        logger.info(f"Regime={regime}: position size ×{pos_mult}, RSI max={rsi_max}")

    watch_list = load_watch_list()
    if not watch_list:
        logger.warning("Empty watch list — skipping cycle.")
        return

    research = load_research()
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
    stock_data: list[tuple[str, IndicatorValues, dict | None]] = []
    for symbol in watch_list:
        try:
            bars = alpaca.get_intraday_bars(symbol)
            daily_bars = alpaca.get_daily_bars(symbol, days=8)
            iv = compute_indicators(symbol, bars, daily_df=daily_bars)
            if iv is None:
                continue
            stock_data.append((symbol, iv, open_positions.get(symbol)))
        except Exception as exc:
            logger.error(f"Error fetching data for {symbol}: {exc}", exc_info=True)

    if not stock_data:
        logger.warning("No valid indicator data — skipping cycle.")
        return

    # ── Phase 2: Filter to stocks that need a Claude decision ─────────────────
    # Open positions: Alpaca bracket orders enforce SL/TP server-side; the
    # position monitor handles the safety-net layer every 60 s.  Asking Claude
    # every 10 min for a HOLD/CLOSE opinion on an already-bracketed position
    # burns tokens without adding value — the view doesn't change that fast.
    # Claude is only needed to validate potential NEW entries.
    needs_claude: list[tuple[str, IndicatorValues, dict | None]] = []
    for symbol, iv, existing_pos in stock_data:
        if existing_pos:
            pnl_pct = (iv.current_price - existing_pos["entry_price"]) / existing_pos["entry_price"] * 100
            logger.info(
                f"BRACKET-MANAGED {symbol}: P&L={pnl_pct:+.2f}%, "
                f"composite={iv.composite_score:+.1f} — Alpaca bracket active, skipping Claude"
            )
        elif iv.composite_score > config.COMPOSITE_SCORE_THRESHOLD:
            needs_claude.append((symbol, iv, None))
        else:
            logger.info(
                f"SKIP-CLAUDE {symbol}: composite={iv.composite_score:+.1f} — no position, below entry threshold"
            )

    # ── Phase 3: Batch Claude calls (CLAUDE_BATCH_SIZE stocks per call) ───────
    decisions: dict[str, dict] = {}
    for i in range(0, len(needs_claude), config.CLAUDE_BATCH_SIZE):
        batch = needs_claude[i : i + config.CLAUDE_BATCH_SIZE]
        batch_decisions = _ask_claude_batch(batch, research, strategy_memory, weights, ai)
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

                qty = _position_size(portfolio_value, iv.current_price, iv.atr)
                qty = max(1.0, round(qty * pos_mult))
                sl_pct = float(decision.get("stop_loss_pct") or config.STOP_LOSS_PCT)
                tp_pct = float(decision.get("take_profit_pct") or config.TAKE_PROFIT_PCT)

                # Guard against concurrent cycles (same process multiple threads, or multiple
                # processes from bot restarts) both seeing an empty slot and both buying.
                # Re-read from disk inside the lock — same pattern as _close_position.
                with _POSITIONS_LOCK:
                    disk_positions = _load_json(config.OPEN_POSITIONS_FILE, {})
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
                }
                open_positions[symbol] = position_data
                buying_power -= iv.current_price * qty

                # Merge-write: read current disk state, add new position, write back.
                # Avoids overwriting concurrent closes made by position_monitor.
                with _POSITIONS_LOCK:
                    disk_positions = _load_json(config.OPEN_POSITIONS_FILE, {})
                    disk_positions[symbol] = position_data
                    _save_json(config.OPEN_POSITIONS_FILE, disk_positions)

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
                    "vix_level": regime_info.get("vix_current"),
                    "vwap_slope": iv.vwap_slope,
                    "pnl_pct": None,
                    "pnl_dollar": None,
                })
                logger.info(
                    f"BUY {qty:.0f}x {symbol} @ ~${iv.current_price:.2f} | "
                    f"SL=${order['stop_loss_price']} TP=${order['take_profit_price']} | "
                    f"{decision.get('reasoning', '')}"
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
    logger.info("Force-closing all positions @ 3:55 PM ET")
    alpaca = AlpacaClient()

    # Cancel all open orders first (including any pending bracket legs)
    alpaca.cancel_all_orders()

    open_positions = load_open_positions()

    for symbol, pos in list(open_positions.items()):
        try:
            price = alpaca.get_latest_price(symbol)
        except Exception:
            price = pos["entry_price"]
            logger.warning(f"Could not get price for {symbol} — using entry price for P&L calc.")

        pnl_pct = (price - pos["entry_price"]) / pos["entry_price"] * 100
        pnl_dollar = (price - pos["entry_price"]) * pos["qty"]

        append_trade({
            "type": "CLOSE",
            "symbol": symbol,
            "timestamp": datetime.now(ET).isoformat(),
            "exit_price": round(price, 4),
            "entry_price": round(pos["entry_price"], 4),
            "qty": pos["qty"],
            "pnl_pct": round(pnl_pct, 2),
            "pnl_dollar": round(pnl_dollar, 2),
            "close_reason": "end_of_day_force_close",
            "entry_composite_score": pos.get("entry_composite_score"),
            "exit_composite_score": None,
            "exit_indicators": {},
            "entry_indicators": pos.get("entry_indicators", {}),
            "entry_sentiment_score": pos.get("entry_sentiment_score"),
            "claude_reasoning": pos.get("claude_reasoning", ""),
            "indicator_signals_at_entry": pos.get("indicator_signals", {}),
        })
        logger.info(f"EOD close {symbol}: P&L={pnl_pct:+.2f}% (${pnl_dollar:+.2f})")

    alpaca.close_all_positions()
    save_open_positions({})
    logger.info("All positions force-closed.")


if __name__ == "__main__":
    import logging as _l
    _l.basicConfig(level=_l.INFO)
    run_trading_cycle()
