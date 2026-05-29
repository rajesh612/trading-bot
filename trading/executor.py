"""
Single-symbol BUY execution: pre-flight checks, sizing, order placement, journaling.
All entry gates must pass before any order is placed.
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime
import config
import notifications.telegram as telegram_notify
from broker.alpaca_client import AlpacaClient
from indicators.values import IndicatorValues
from data.market_data import get_intraday_bars
from utils.io_utils import load_json, save_json
from signals.entry_gates import check_all_gates, effective_composite_gate
from signals.tp_scaler import compute_tp_sl
from utils.ticker_quality import TickerQuality
from trading.journal import _POSITIONS_LOCK, append_trade
from utils.ticker_quality import derive_mode
from signals.entry_gates import is_catalyst_active
from utils.regime_params import get_concurrency_params, get_entry_params
from trading.position_sizer import (
    compute_position_size_for_mode,
    apply_regime_multiplier,
    apply_sentiment_multiplier,
)
from trading import risk_manager
from utils import regimes
from utils.sl_blacklist import is_sl_blacklisted
from utils.constants import ET

logger = logging.getLogger(__name__)


def _get_sector_sentiment(research_meta: dict, symbol: str) -> float:
    """Return sector-level NLP sentiment score for symbol's sector from research __meta__."""
    try:
        import sectors as _sectors
        sector = _sectors.get_sector(symbol)
        return float(research_meta.get("sector_sentiment", {}).get(sector, 0))
    except Exception:
        return 0.0


def _compute_swing_book_pnl(stock_data_prices: dict[str, float], portfolio_value: float) -> float:
    """Unrealised P&L of swing book as % of portfolio_value.

    Returns -999.0 on any read or computation error (fail-closed — triggers swing gate).
    stock_data_prices: {symbol: current_price} built from iv.current_price — no API calls.
    """
    try:
        swing_positions = load_json(config.SWING_POSITIONS_FILE, None)
        if swing_positions is None:
            return -999.0
        if not swing_positions or portfolio_value <= 0:
            return 0.0
        total_pnl = 0.0
        for sym, pos in swing_positions.items():
            entry_price = pos.get("entry_price", 0)
            qty         = pos.get("qty", 0)
            if entry_price <= 0 or qty <= 0:
                continue
            current_price = stock_data_prices.get(sym, entry_price)
            total_pnl += (current_price - entry_price) * qty
        return total_pnl / portfolio_value * 100
    except Exception:
        return -999.0


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
    daily_pnl_pct: float = 0.0,
    sector_today: dict | None = None,
    symbol_sector_etf: str | None = None,
    advance_decline_ratio: float = 1.0,
    suppress_notify: bool = False,
    major_event_today: bool = False,
    staleness_guard=None,
    quality: TickerQuality | None = None,
    pead_ok: bool = False,
    snap=None,
) -> dict | None:
    """
    Validate and place a single BUY order.
    Returns position_data dict on success, None if any pre-flight check blocks entry.
    """
    if regime == "CRISIS":
        logger.info("SKIP %s — CRISIS regime, no entries.", symbol)
        return None

    _catalyst_active = is_catalyst_active(symbol, snap) if snap is not None else False
    mode = derive_mode(symbol, pead_ok=pead_ok, existing_swing_pos=False, catalyst_active=_catalyst_active)

    # Per-fill daily-loss circuit breaker
    loss_warn_pct = -(config.DAILY_LOSS_WARN_PCT * 100)
    loss_halt_pct = -(config.MAX_DAILY_LOSS_PCT * 100)
    if daily_pnl_pct <= loss_halt_pct:
        logger.warning(
            "SKIP %s — per-fill daily-loss halt (%.2f%% <= %.2f%%)",
            symbol, daily_pnl_pct, loss_halt_pct,
        )
        return None
    if daily_pnl_pct <= loss_warn_pct:
        telegram_notify.notify_daily_loss_warning(daily_pnl_pct, config.MAX_DAILY_LOSS_PCT * 100)

    if mode == "CATALYST":
        cat_count = sum(1 for p in open_positions.values() if p.get("mode_at_entry") == "CATALYST")
        if cat_count >= config.CATALYST_MAX_POSITIONS:
            logger.info("SKIP %s — CATALYST at position limit (%d/%d)", symbol, cat_count, config.CATALYST_MAX_POSITIONS)
            return None

    # ── Concurrency + heat guard ─────────────────────────────────────────────
    try:
        _q_tier = quality.tier if quality else "MEDIUM"
        cp = get_concurrency_params(regime, _q_tier, mode)

        # A. Max-open check — re-read disk inside lock (avoids stale in-memory state)
        with _POSITIONS_LOCK:
            disk_positions = load_json(config.OPEN_POSITIONS_FILE, {})
        intraday_open  = [p for p in disk_positions.values() if not p.get("is_swing")]
        intraday_count = len(intraday_open)

        effective_max_open = cp.max_open
        if cp.swing_health_gate:
            swing_pnl = _compute_swing_book_pnl({symbol: iv.current_price}, portfolio_value)
            if swing_pnl < config.SWING_HEALTH_MIN_PNL_PCT:
                effective_max_open = min(effective_max_open, 1)
                logger.info(
                    "CONCURRENCY %s — swing book stressed (%.1f%%) → max_open capped at 1",
                    symbol, swing_pnl,
                )

        if intraday_count >= effective_max_open:
            logger.info(
                "SKIP %s — concurrency limit %d/%d (regime=%s tier=%s)",
                symbol, intraday_count, effective_max_open, regime, _q_tier,
            )
            return None

        # B. Heat ceiling check — worst-case: max_position_pct × portfolio
        if portfolio_value > 0 and iv.current_price > 0:
            max_pct      = get_entry_params(regime).max_position_pct
            proj_heat    = max_pct * cp.heat_weight
            current_heat = sum(
                p.get("qty", 0) * p.get("entry_price", 0) for p in intraday_open
            ) / portfolio_value
            ceiling = config.REGIME_HEAT_CEILING.get(regime, 0.20)
            if current_heat + proj_heat > ceiling:
                logger.info(
                    "SKIP %s — heat ceiling %.0f%% would be breached "
                    "(current=%.1f%% + projected=%.1f%%)",
                    symbol, ceiling * 100, current_heat * 100, proj_heat * 100,
                )
                return None

        # C. Daily-loss downshift — near loss limit → cap at 1 concurrent position
        if daily_pnl_pct <= -(config.DAILY_LOSS_WARN_PCT * 100):
            if intraday_count >= 1:
                logger.info(
                    "SKIP %s — near daily loss limit (%.2f%%), allowing max 1 position",
                    symbol, daily_pnl_pct,
                )
                return None

    except Exception as exc:
        logger.warning(
            "Concurrency guard raised unexpectedly for %s — blocking entry: %s", symbol, exc,
        )
        return None
    # ── end concurrency guard ─────────────────────────────────────────────────

    symbol_sentiment = research.get(symbol, {}).get("sentiment_score", 0)

    try:
        bars_15min = get_intraday_bars(symbol, lookback=config.MTF_BARS_LOOKBACK, timeframe_min=config.MTF_TIMEFRAME_MINUTES)
    except Exception:
        bars_15min = None

    with _POSITIONS_LOCK:
        fresh_daily_closed = set(load_json(config.DAILY_CLOSED_FILE, []))

    sl_blacklisted = is_sl_blacklisted(symbol)

    sector_sentiment = _get_sector_sentiment(research.get("__meta__", {}), symbol)

    from utils.data_health import FRESH_GUARD
    guard = staleness_guard or FRESH_GUARD

    # Stale regime → unknown swing params → block swing promotion
    swing_eligible_raw = regime in regimes.SWING_ELIGIBLE
    swing_eligible = swing_eligible_raw and ("regime" not in guard.stale_sources)

    gate = check_all_gates(
        symbol=symbol, iv=iv, open_positions=open_positions,
        daily_closed=fresh_daily_closed, bars_15min=bars_15min, now=now,
        spy_ret5d=spy_ret5d, symbol_ret5d=symbol_ret5d, mode=mode,
        rsi_max=rsi_max,
        sector_today=sector_today,
        symbol_sector_etf=symbol_sector_etf,
        advance_decline_ratio=advance_decline_ratio,
        sl_blacklisted=sl_blacklisted,
        sector_sentiment_score=sector_sentiment,
        sentiment_score=float(symbol_sentiment) if isinstance(symbol_sentiment, (int, float)) else 0.0,
        swing_eligible=swing_eligible,
        major_event_today=major_event_today,
        regime=regime,
        quality=quality,
    )
    if not gate.passed:
        logger.info("SKIP %s — gate: %s", symbol, gate.reason)
        return None
    if gate.flags:
        logger.info("ENTRY %s flagged: %s", symbol, ", ".join(gate.flags))

    eff_gate = effective_composite_gate(iv, regime, params=params, staleness_guard=guard)
    if iv.composite_score < eff_gate:
        logger.info("SKIP %s — composite=%+.1f below effective gate %d (regime=%s)", symbol, iv.composite_score, eff_gate, regime)
        return None

    qty = compute_position_size_for_mode(
        portfolio_value, iv.current_price, iv.atr,
        iv.bot_herding_score, iv.squeeze_setup, regime=regime,
    )
    qty = apply_regime_multiplier(qty, pos_mult)
    qty = max(1, int(round(qty * cp.size_mult)))
    if iv.current_price > 0:
        _max_qty = int((portfolio_value * get_entry_params(regime).max_position_pct) / iv.current_price)
        qty = min(qty, max(1, _max_qty))
    if isinstance(symbol_sentiment, (int, float)):
        qty = apply_sentiment_multiplier(qty, int(symbol_sentiment))
    qty = max(1, int(qty * config.TRANCHE1_SIZE_PCT))  # T1 = 60% of full size
    if config.MAX_POSITION_USD and iv.current_price > 0:
        qty = min(qty, max(1, int(config.MAX_POSITION_USD / iv.current_price)))

    sl_pct = config.STOP_LOSS_PCT
    tp_pct = config.TAKE_PROFIT_PCT
    if decision.get("stop_loss_pct"):
        sl_pct = float(decision["stop_loss_pct"])
    if decision.get("take_profit_pct"):
        tp_pct = float(decision["take_profit_pct"])
    if iv.squeeze_setup and decision.get("confidence", 0) >= config.SQUEEZE_CONFIDENCE_GATE:
        sl_pct, tp_pct = config.SQUEEZE_SL_PCT, config.SQUEEZE_TP_PCT

    # Apply time-aware + tier adjustment to TP/SL (Rule 30)
    tp_pct, sl_pct = compute_tp_sl(
        base_tp=tp_pct,
        base_sl=sl_pct,
        entry_time=now,
        tier=quality.tier if quality else "MEDIUM",
        regime=regime,
    )

    # Concurrent guard: re-read disk inside lock immediately before placing order
    with _POSITIONS_LOCK:
        disk_positions = load_json(config.OPEN_POSITIONS_FILE, {})
        disk_closed    = set(load_json(config.DAILY_CLOSED_FILE, []))
        if symbol in disk_positions or symbol in disk_closed:
            logger.info("SKIP %s — concurrent cycle guard (disk check).", symbol)
            open_positions.update(disk_positions)
            return None

    try:
        order = alpaca.place_bracket_order(symbol=symbol, qty=qty, entry_price=iv.current_price, stop_loss_pct=sl_pct, take_profit_pct=tp_pct)
    except Exception as exc:
        logger.error("ORDER FAILED %s — place_bracket_order raised: %s", symbol, exc)
        return None

    # Correct SL/TP if fill price slippage exceeds threshold
    corrected = risk_manager.correct_bracket_after_fill(
        alpaca._trading,
        order["id"],
        iv.current_price,
        sl_pct,
        tp_pct,
    )
    if corrected is None:
        logger.warning(
            "%s fill-slippage correction timed out — journaling with indicative entry price $%.2f; "
            "actual SL/TP may differ.",
            symbol, iv.current_price,
        )

    position_data = {
        "entry_price": iv.current_price, "qty": qty,
        "stop_loss_pct": sl_pct, "take_profit_pct": tp_pct,
        "stop_loss_price": corrected["stop_loss_price"] if corrected else order["stop_loss_price"],
        "take_profit_price": corrected["take_profit_price"] if corrected else order["take_profit_price"],
        "fill_price": corrected["fill_price"] if corrected else iv.current_price,
        "entry_time": now.isoformat(), "entry_hour": now.hour, "entry_composite_score": iv.composite_score,
        "entry_indicators": asdict(iv), "entry_sentiment_score": research.get(symbol, {}).get("sentiment_score"),
        "entry_sector_sentiment": sector_sentiment,
        "entry_news_sentiment": float(symbol_sentiment) if isinstance(symbol_sentiment, (int, float)) else 0.0,
        "claude_reasoning": decision.get("reasoning", ""), "indicator_signals": decision.get("indicator_signals", {}),
        "order_id": order.get("id"), "mode_at_entry": mode, "regime_at_entry": regime,
        "atr_pct": round((iv.atr / iv.current_price) * 100, 2) if iv.atr > 0 and iv.current_price > 0 else None,
        "tranche_2_triggered": False,
        "tranche_1_qty":       qty,
        "tranche_1_entry":     iv.current_price,
        "stop_order_id":       None,
        "quality_tier":        quality.tier    if quality else "MEDIUM",
        "quality_unlocks":     list(quality.unlocks) if quality else [],
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
        "atr_pct": round((iv.atr / iv.current_price) * 100, 2) if iv.atr > 0 and iv.current_price > 0 else None,
    })
    final_sl = corrected["stop_loss_price"] if corrected else order["stop_loss_price"]
    final_tp = corrected["take_profit_price"] if corrected else order["take_profit_price"]
    logger.info(
        "BUY %.0fx %s @ ~$%.2f | mode=%s | SL=$%s TP=$%s | %s",
        qty, symbol, iv.current_price, mode,
        final_sl, final_tp, decision.get("reasoning", ""),
    )
    if not suppress_notify:
        telegram_notify.notify_buy(
            symbol=symbol, qty=qty, price=iv.current_price,
            sl=final_sl, tp=final_tp,
            mode=mode, score=iv.composite_score, confidence=decision.get("confidence", 0),
        )
    return position_data


def execute_tranche2_addon(
    *,
    symbol: str,
    pos: dict,
    regime: str,
    research: dict,
    alpaca,
    portfolio_value: float,
    now: datetime,
    suppress_notify: bool = False,
) -> dict | None:
    """Place T2 add-on order, cancel original bracket, update position with blended values."""
    from trading.position_sizer import compute_position_size_for_mode, apply_regime_multiplier

    # Step 1: idempotency sentinel — write tranche_2_triggered=True before any Alpaca call
    with _POSITIONS_LOCK:
        disk_positions = load_json(config.OPEN_POSITIONS_FILE, {})
        if symbol not in disk_positions:
            # Position exists in memory but not yet flushed; seed from caller's pos dict
            disk_positions[symbol] = dict(pos)
        if disk_positions[symbol].get("tranche_2_triggered"):
            logger.info("T2 abort %s — already triggered (concurrent guard)", symbol)
            return None
        disk_positions[symbol]["tranche_2_triggered"] = True
        save_json(config.OPEN_POSITIONS_FILE, disk_positions)

    # Step 2: cancel original bracket (best-effort)
    order_id = pos.get("order_id")
    if order_id:
        try:
            alpaca.cancel_order(order_id)
        except Exception as exc:
            logger.warning("T2 %s: cancel_order(%s) failed — %s; continuing", symbol, order_id, exc)

    # Step 3: compute T2 qty (fresh sizing based on current price)
    try:
        current_price = alpaca.get_latest_price(symbol)
    except Exception:
        current_price = pos.get("entry_price") or pos.get("tranche_1_entry", 0)

    atr_pct = pos.get("atr_pct") or 0
    current_atr = current_price * atr_pct / 100 if atr_pct > 0 else current_price * config.ATR_FALLBACK_PROXY_PCT
    full_qty = compute_position_size_for_mode(
        portfolio_value, current_price, current_atr, 0, False, regime=regime,
    )
    full_qty = apply_regime_multiplier(full_qty, 1.0)
    t2_qty = max(1, int(full_qty * config.TRANCHE2_SIZE_PCT))
    if config.MAX_POSITION_USD and current_price > 0:
        t2_qty = min(t2_qty, max(1, int(config.MAX_POSITION_USD / current_price)))

    # Step 4: place T2 market order
    try:
        order = alpaca.place_market_order(symbol, t2_qty, "buy")
    except Exception as exc:
        logger.error("T2 %s: place_market_order failed — %s", symbol, exc)
        return None
    t2_fill = order.get("fill_price") or current_price

    # Step 5: blended values
    t1_qty   = pos.get("tranche_1_qty") or pos.get("qty", 0)
    t1_entry = pos.get("tranche_1_entry") or pos.get("entry_price", 0)
    combined_qty   = t1_qty + t2_qty
    blended_entry  = (t1_qty * t1_entry + t2_qty * t2_fill) / combined_qty
    blended_sl_price = t2_fill * (1 - config.TRANCHE2_SL_PCT / 100)
    blended_sl_pct   = (blended_entry - blended_sl_price) / blended_entry * 100

    # Step 6: server-side stop order (best-effort)
    stop_order_id = None
    try:
        stop_result = alpaca.place_stop_order(symbol, combined_qty, blended_sl_price)
        stop_order_id = stop_result.get("stop_order_id")
    except Exception as exc:
        logger.warning("T2 %s: place_stop_order failed — %s; software SL remains primary", symbol, exc)

    # Step 7: update position record atomically
    with _POSITIONS_LOCK:
        disk_positions = load_json(config.OPEN_POSITIONS_FILE, {})
        if symbol not in disk_positions:
            logger.warning("T2 %s: position disappeared before update", symbol)
            return None
        disk_positions[symbol].update({
            "tranche_2_triggered": True,
            "tranche_2_qty":       t2_qty,
            "tranche_2_entry":     t2_fill,
            "qty":                 combined_qty,
            "entry_price":         round(blended_entry, 4),
            "stop_loss_price":     round(blended_sl_price, 4),
            "stop_loss_pct":       round(blended_sl_pct, 4),
            "order_id":            None,
            "stop_order_id":       stop_order_id,
        })
        updated_pos = disk_positions[symbol]
        save_json(config.OPEN_POSITIONS_FILE, disk_positions)

    # Step 8: journal — call via module so tests can monkeypatch trading.journal.append_trade
    import trading.journal as _jrn
    _jrn.append_trade({
        "type":             "TRANCHE2_ADD",
        "symbol":           symbol,
        "timestamp":        now.isoformat(),
        "t1_qty":           t1_qty,
        "t1_entry":         t1_entry,
        "t2_qty":           t2_qty,
        "t2_fill":          round(t2_fill, 4),
        "blended_entry":    round(blended_entry, 4),
        "blended_sl_pct":   round(blended_sl_pct, 2),
        "blended_sl_price": round(blended_sl_price, 4),
        "combined_qty":     combined_qty,
        "stop_order_id":    stop_order_id,
        "regime":           regime,
    })

    # Step 9: notify
    if not suppress_notify:
        telegram_notify.notify_tranche2_add(
            symbol=symbol, qty=t2_qty, t2_price=t2_fill,
            blended_entry=round(blended_entry, 2),
            blended_sl_price=round(blended_sl_price, 2),
            blended_sl_pct=round(blended_sl_pct, 1),
            stop_order_placed=stop_order_id is not None,
        )
    logger.info(
        "TRANCHE2_ADD %.0fx %s @ $%.2f | blended_entry=$%.2f SL=$%.2f (%.1f%%) | stop_order=%s",
        t2_qty, symbol, t2_fill, blended_entry, blended_sl_price, blended_sl_pct,
        stop_order_id or "none",
    )
    return updated_pos
