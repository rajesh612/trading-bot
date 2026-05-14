"""
Single-symbol BUY execution: pre-flight checks, sizing, order placement, journaling.
All entry gates must pass before any order is placed.
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime
from zoneinfo import ZoneInfo

import config
import telegram_notify
from alpaca_client import AlpacaClient
from indicators.values import IndicatorValues
from market_data import get_intraday_bars
from utils.io_utils import load_json, save_json
from signals.entry_gates import check_all_gates
from trading.journal import _POSITIONS_LOCK, append_trade
from trading.modes import get_active_mode, get_sl_tp_for_mode_and_regime, _MODE_POSITION_LIMITS
from trading.position_sizer import (
    compute_position_size_for_mode,
    apply_regime_multiplier,
    apply_sentiment_multiplier,
)
from trading import risk_manager

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


def effective_composite_gate(iv: IndicatorValues, regime: str) -> int:
    """Squeeze loosens; herding / choppy raises the composite entry threshold."""
    if iv.squeeze_setup:
        return 12
    if iv.bot_herding_score >= config.BOT_HERD_ALERT_SCORE:
        return config.HERD_ENTRY_GATE
    if regime == "CHOPPY":
        return config.CHOPPY_COMPOSITE_GATE
    return config.COMPOSITE_SCORE_THRESHOLD


def execute_buy(
    *,
    symbol: str,
    iv: IndicatorValues,
    decision: dict,
    regime: str,
    regime_info: dict,
    research: dict,
    open_positions: dict,
    alpaca: AlpacaClient,
    portfolio_value: float,
    pos_mult: float,
    spy_ret5d: float,
    symbol_ret5d: float,
    rsi_max: float,
    params: dict,
    now: datetime,
    entries_this_cycle: int,
) -> dict | None:
    """
    Validate and place a single BUY order.
    Returns position_data dict on success, None if any pre-flight check blocks entry.
    Caller increments entries_this_cycle and result.entries on non-None return.
    """
    if iv.rsi > rsi_max:
        logger.info("SKIP %s — RSI %.1f > %s (regime=%s).", symbol, iv.rsi, rsi_max, regime)
        return None
    if iv.vwap_slope < params["vwap_slope_min"]:
        logger.info("SKIP %s — VWAP slope %.3f%% < %.3f%% (downtrend).", symbol, iv.vwap_slope, params["vwap_slope_min"])
        return None

    mode = get_active_mode(regime, symbol)
    if mode == "CASH":
        logger.info("SKIP %s — CASH mode (CRISIS regime).", symbol)
        return None

    if mode in _MODE_POSITION_LIMITS:
        count = sum(1 for p in open_positions.values() if p.get("mode_at_entry") == mode)
        if count >= _MODE_POSITION_LIMITS[mode]:
            logger.info("SKIP %s — %s at position limit (%d/%d)", symbol, mode, count, _MODE_POSITION_LIMITS[mode])
            return None

    symbol_sentiment = research.get(symbol, {}).get("sentiment_score", 0)
    if symbol_sentiment < config.SENTIMENT_HARD_BLOCK:
        logger.info("SKIP %s — bearish sentiment (%+d) blocks BUY", symbol, symbol_sentiment)
        return None

    try:
        bars_15min = get_intraday_bars(symbol, lookback=config.MTF_BARS_LOOKBACK, timeframe_min=config.MTF_TIMEFRAME_MINUTES)
    except Exception:
        bars_15min = None

    with _POSITIONS_LOCK:
        fresh_daily_closed = set(load_json(config.DAILY_CLOSED_FILE, []))

    gate = check_all_gates(
        symbol=symbol, iv=iv, open_positions=open_positions,
        daily_closed=fresh_daily_closed, bars_15min=bars_15min, now=now,
        entries_this_cycle=entries_this_cycle,
        spy_ret5d=spy_ret5d, symbol_ret5d=symbol_ret5d, mode=mode,
    )
    if not gate.passed:
        logger.info("SKIP %s — gate: %s", symbol, gate.reason)
        return None
    if gate.flags and decision.get("source") == "auto_execute":
        logger.info("SKIP %s — auto-execute blocked by soft flags: %s", symbol, ", ".join(gate.flags))
        return None
    if gate.flags:
        logger.info("ENTRY %s flagged: %s", symbol, ", ".join(gate.flags))

    eff_gate = effective_composite_gate(iv, regime)
    if iv.composite_score < eff_gate:
        logger.info("SKIP %s — composite=%+.1f below effective gate %d (regime=%s)", symbol, iv.composite_score, eff_gate, regime)
        return None

    qty = compute_position_size_for_mode(portfolio_value, iv.current_price, iv.atr, mode, iv.bot_herding_score, iv.squeeze_setup)
    qty = apply_regime_multiplier(qty, pos_mult)
    if isinstance(symbol_sentiment, (int, float)):
        qty = apply_sentiment_multiplier(qty, int(symbol_sentiment))

    sl_pct, tp_pct = get_sl_tp_for_mode_and_regime(mode, regime)
    if decision.get("stop_loss_pct"):
        sl_pct = float(decision["stop_loss_pct"])
    if decision.get("take_profit_pct"):
        tp_pct = float(decision["take_profit_pct"])
    if iv.squeeze_setup and decision.get("confidence", 0) >= config.SQUEEZE_CONFIDENCE_GATE:
        sl_pct, tp_pct = 6.0, 5.0

    # Concurrent guard: re-read disk inside lock immediately before placing order
    with _POSITIONS_LOCK:
        disk_positions = load_json(config.OPEN_POSITIONS_FILE, {})
        disk_closed    = set(load_json(config.DAILY_CLOSED_FILE, []))
        if symbol in disk_positions or symbol in disk_closed:
            logger.info("SKIP %s — concurrent cycle guard (disk check).", symbol)
            open_positions.update(disk_positions)
            return None

    order = alpaca.place_bracket_order(symbol=symbol, qty=qty, entry_price=iv.current_price, stop_loss_pct=sl_pct, take_profit_pct=tp_pct)

    # Correct SL/TP if fill price slippage exceeds threshold
    corrected = risk_manager.correct_bracket_after_fill(
        alpaca._trading,
        order["id"],
        iv.current_price,
        sl_pct,
        tp_pct,
    )

    position_data = {
        "entry_price": iv.current_price, "qty": qty,
        "stop_loss_pct": sl_pct, "take_profit_pct": tp_pct,
        "stop_loss_price": corrected["stop_loss_price"] if corrected else order["stop_loss_price"],
        "take_profit_price": corrected["take_profit_price"] if corrected else order["take_profit_price"],
        "fill_price": corrected["fill_price"] if corrected else iv.current_price,
        "entry_time": now.isoformat(), "entry_composite_score": iv.composite_score,
        "entry_indicators": asdict(iv), "entry_sentiment_score": research.get(symbol, {}).get("sentiment_score"),
        "claude_reasoning": decision.get("reasoning", ""), "indicator_signals": decision.get("indicator_signals", {}),
        "order_id": order.get("id"), "mode_at_entry": mode, "regime_at_entry": regime,
    }
    open_positions[symbol] = position_data

    with _POSITIONS_LOCK:
        disk_positions = load_json(config.OPEN_POSITIONS_FILE, {})
        disk_positions[symbol] = position_data
        save_json(config.OPEN_POSITIONS_FILE, disk_positions)

    append_trade({
        "type": "BUY", "symbol": symbol, "timestamp": now.isoformat(),
        "price": round(iv.current_price, 4), "qty": qty,
        "composite_score": iv.composite_score, "indicators": asdict(iv),
        "sentiment_score": research.get(symbol, {}).get("sentiment_score"),
        "claude_reasoning": decision.get("reasoning", ""), "claude_confidence": decision.get("confidence", 0),
        "indicator_signals": decision.get("indicator_signals", {}),
        "stop_loss_pct": sl_pct, "take_profit_pct": tp_pct,
        "stop_loss_price": corrected["stop_loss_price"] if corrected else order["stop_loss_price"],
        "take_profit_price": corrected["take_profit_price"] if corrected else order["take_profit_price"],
        "vix_regime": regime, "mode_at_entry": mode, "vix_level": regime_info.get("vix_current"),
        "vwap_slope": iv.vwap_slope, "pnl_pct": None, "pnl_dollar": None,
    })
    final_sl = corrected["stop_loss_price"] if corrected else order["stop_loss_price"]
    final_tp = corrected["take_profit_price"] if corrected else order["take_profit_price"]
    logger.info(
        "BUY %.0fx %s @ ~$%.2f | mode=%s | SL=$%s TP=$%s | %s",
        qty, symbol, iv.current_price, mode,
        final_sl, final_tp, decision.get("reasoning", ""),
    )
    telegram_notify.notify_buy(
        symbol=symbol, qty=qty, price=iv.current_price,
        sl=final_sl, tp=final_tp,
        mode=mode, score=iv.composite_score, confidence=decision.get("confidence", 0),
    )
    return position_data
