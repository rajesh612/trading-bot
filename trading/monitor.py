"""
Position monitor — runs every 60 s.

Layer 1: Alpaca bracket fill reconciliation — detects bracket-order fills and syncs local state.
Layer 2: Price-level safety net — closes positions that breach SL/TP between cycles.
"""
import logging
import threading
import time
from datetime import datetime

import config
import notifications.telegram as telegram_notify
import sectors
from broker.alpaca_client import AlpacaClient
from data.market_data import get_intraday_bars
from indicators.engine import compute_indicators
from macro.regime import load_macro_regime
from macro.economic_calendar import get_todays_events
from signals.tranche2_gates import check_tranche2_gates
from trading.closer import close_position, _apply_profit_lock
from trading.journal import (
    _POSITIONS_LOCK,
    load_open_positions,
    append_trade,
    record_daily_closed,
    flush_profit_lock_updates,
)
from utils.io_utils import load_json, save_json
from utils.constants import ET
from utils.sl_blacklist import record_sl_hit

logger = logging.getLogger(__name__)

_DRAWDOWN_LOCK: threading.Lock = threading.Lock()
_drawdown_alerted: dict[str, float] = {}  # symbol → epoch of last alert


def _try_compute_iv(symbol: str, alpaca: AlpacaClient):
    """Return fresh IndicatorValues for exit context, or None if bars unavailable."""
    try:
        bars = alpaca.get_intraday_bars(symbol)
        return compute_indicators(symbol, bars)
    except Exception:
        return None


def _check_drawdown_alerts(positions: dict, current_prices: dict) -> None:
    """Fire drawdown alerts with per-position cooldown to avoid Telegram spam."""
    with _DRAWDOWN_LOCK:
        now_ts = time.time()
        cooldown_s = config.DRAWDOWN_ALERT_COOLDOWN_MIN * 60
        # Prune closed positions from cooldown dict
        for sym in list(_drawdown_alerted):
            if sym not in positions:
                del _drawdown_alerted[sym]
        for symbol, pos in positions.items():
            price = current_prices.get(symbol)
            if price is None:
                continue
            entry = pos.get("entry_price", 0)
            if entry <= 0:
                continue
            pnl_pct = (price - entry) / entry * 100
            stop_pct = -pos.get("stop_loss_pct", config.STOP_LOSS_PCT)
            if pnl_pct <= -config.DRAWDOWN_WARN_PCT and pnl_pct > stop_pct:
                last = _drawdown_alerted.get(symbol, 0.0)
                if now_ts - last >= cooldown_s:
                    telegram_notify.notify_position_drawdown(symbol, pnl_pct, stop_pct)
                    _drawdown_alerted[symbol] = now_ts


def _check_tranche2_triggers(open_positions: dict, alpaca, now: datetime) -> None:
    """Check each open position for T2 add-on eligibility. Called after the SL/TP loop."""
    from trading.executor import execute_tranche2_addon

    try:
        regime = load_macro_regime().get("regime", "NORMAL")
    except Exception:
        regime = "NORMAL"

    research: dict = {}
    try:
        research = load_json(config.RESEARCH_REPORT_FILE, {})
    except Exception:
        pass

    major_event_today = False
    try:
        _high_impact = {"FOMC", "CPI", "NFP", "PPI", "PCE", "JOLTS", "GDP"}
        for evt in get_todays_events():
            if any(tag in evt.get("name", "").upper() for tag in _high_impact):
                major_event_today = True
                break
    except Exception:
        pass

    try:
        portfolio_value = alpaca.get_portfolio_value()
    except Exception:
        portfolio_value = 100_000.0

    # Batch-fetch sector ETF intraday % change once per unique ETF
    unique_etfs: set[str] = set()
    for sym in open_positions:
        etf = sectors.get_sector_etf(sym)
        if etf:
            unique_etfs.add(etf)

    sector_etf_chg: dict[str, float] = {}
    for etf in unique_etfs:
        try:
            bars = get_intraday_bars(etf, lookback=config.BARS_LOOKBACK)
            if bars is not None and not bars.empty:
                session_open = float(bars.iloc[0]["open"])
                latest = alpaca.get_latest_price(etf)
                if session_open > 0:
                    sector_etf_chg[etf] = (latest - session_open) / session_open * 100
        except Exception as exc:
            logger.debug("T2 sector ETF fetch failed for %s: %s", etf, exc)

    for symbol, pos in list(open_positions.items()):
        if pos.get("tranche_2_triggered"):
            continue

        # Recover quality unlocks from position record (persisted at entry in executor.py)
        pos_unlocks = frozenset(pos.get("quality_unlocks", []))

        # In CHOPPY, T2 is gated off unless T2_CHOPPY unlock was earned at entry
        if regime == "CHOPPY" and "T2_CHOPPY" not in pos_unlocks:
            logger.debug("T2 skip %s — CHOPPY regime requires T2_CHOPPY unlock", symbol)
            continue

        iv = _try_compute_iv(symbol, alpaca)
        if iv is None:
            continue

        sentiment = research.get(symbol, {}).get("sentiment_score", 0)
        etf = sectors.get_sector_etf(symbol)
        chg = sector_etf_chg.get(etf, 0.0) if etf else 0.0

        result = check_tranche2_gates(
            symbol=symbol,
            iv=iv,
            pos=pos,
            now=now,
            regime=regime,
            sentiment_score=float(sentiment) if sentiment else 0.0,
            sector_etf_chg=chg,
            major_event_today=major_event_today,
        )
        if result.passed:
            logger.info("T2 TRIGGER %s — all gates passed (sector=%s chg=%.1f%%)", symbol, etf, chg)
            execute_tranche2_addon(
                symbol=symbol,
                pos=pos,
                regime=regime,
                research=research,
                alpaca=alpaca,
                portfolio_value=portfolio_value,
                now=now,
            )
        else:
            logger.debug("T2 skip %s — %s", symbol, result.reason)


def run_position_monitor():
    """
    Two-layer intra-cycle protection:

    Layer 1 — Alpaca bracket reconciliation
        Detects when Alpaca has already closed a position via bracket fill
        and syncs local state.

    Layer 2 — Price-level safety net
        Manually closes positions that breach SL/TP levels between cycles.
    """
    open_positions = load_open_positions()
    if not open_positions:
        return

    alpaca           = AlpacaClient()
    alpaca_positions = alpaca.get_positions()
    now              = datetime.now(ET)

    try:
        regime = load_macro_regime().get("regime", "NORMAL")
    except Exception:
        regime = "NORMAL"

    for symbol, pos in list(open_positions.items()):

        # ── Layer 1: bracket fill reconciliation ─────────────────────────────
        if symbol not in alpaca_positions:
            with _POSITIONS_LOCK:
                disk_positions = load_json(config.OPEN_POSITIONS_FILE, {})
                if symbol not in disk_positions:
                    open_positions.pop(symbol, None)
                    continue
                pos = disk_positions.pop(symbol)
                open_positions.pop(symbol, None)
                save_json(config.OPEN_POSITIONS_FILE, disk_positions)

                entry_price = pos.get("entry_price", 0)
                if entry_price <= 0:
                    logger.warning("Position monitor: %s has zero/missing entry_price — skipping bracket reconciliation", symbol)
                    continue
                sl_price    = entry_price * (1 - pos.get("stop_loss_pct", config.STOP_LOSS_PCT) / 100)
                tp_price    = entry_price * (1 + pos.get("take_profit_pct", config.TAKE_PROFIT_PCT) / 100)
                try:
                    current_price = alpaca.get_latest_price(symbol)
                except Exception:
                    current_price = entry_price

                tol = config.BRACKET_FILL_TOLERANCE_PCT / 100
                if current_price <= sl_price * (1 + tol):
                    close_reason = "bracket_stop_loss_filled"
                    exit_price   = sl_price
                elif current_price >= tp_price * (1 - tol):
                    close_reason = "bracket_take_profit_filled"
                    exit_price   = tp_price
                else:
                    close_reason = "bracket_filled_unknown_leg"
                    exit_price   = current_price

                pnl_pct    = (exit_price - entry_price) / entry_price * 100
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
                    "atr_pct": pos.get("atr_pct", 0.0),
                })
                record_daily_closed(symbol)
                if close_reason == "bracket_stop_loss_filled":
                    record_sl_hit(symbol)
            logger.info(
                "BRACKET FILL detected: %s closed via %s | P&L=%+.2f%% ($%+.2f) | no re-entry today",
                symbol, close_reason, pnl_pct, pnl_dollar,
            )
            telegram_notify.notify_close(
                symbol=symbol, qty=pos["qty"],
                entry=entry_price, exit_price=exit_price,
                pnl_dollar=round(pnl_dollar, 2), pnl_pct=round(pnl_pct, 2),
                reason=close_reason,
            )
            continue

        # ── Layer 2: price-level safety net ──────────────────────────────────
        try:
            current_price = alpaca.get_latest_price(symbol)
        except Exception as exc:
            logger.warning("Position monitor: could not get price for %s: %s", symbol, exc)
            continue

        pos = _apply_profit_lock(pos, current_price)
        open_positions[symbol] = pos

        entry_price = pos.get("entry_price", 0)
        if entry_price <= 0:
            logger.warning("Position monitor: %s has zero/missing entry_price — skipping SL/TP check", symbol)
            continue
        pnl_pct     = (current_price - entry_price) / entry_price * 100
        sl_pct      = pos.get("stop_loss_pct", config.STOP_LOSS_PCT)
        tp_pct      = pos.get("take_profit_pct", config.TAKE_PROFIT_PCT)

        if pnl_pct <= -sl_pct:
            logger.warning(
                "MONITOR STOP-LOSS: %s P&L=%+.2f%% breached SL=%.1f%% — closing (mid-cycle)",
                symbol, pnl_pct, -sl_pct,
            )
            close_position(symbol, alpaca, open_positions, current_price, _try_compute_iv(symbol, alpaca), "monitor_stop_loss")
            record_sl_hit(symbol)
        elif pnl_pct >= tp_pct:
            logger.info(
                "MONITOR TAKE-PROFIT: %s P&L=%+.2f%% hit TP=%.1f%% — closing (mid-cycle)",
                symbol, pnl_pct, tp_pct,
            )
            close_position(symbol, alpaca, open_positions, current_price, _try_compute_iv(symbol, alpaca), "monitor_take_profit")
        elif (
            regime == "HIGH_VOL"
            and now.hour >= config.HIGH_VOL_EARLY_EXIT_HOUR
            and pnl_pct < -config.HIGH_VOL_EARLY_EXIT_MIN_LOSS_PCT
            and not pos.get("is_swing", False)
        ):
            entry_ts_str = pos.get("entry_time")
            if entry_ts_str:
                try:
                    from datetime import timezone  # noqa: F401 — keep for clarity
                    entry_dt = datetime.fromisoformat(entry_ts_str)
                    if entry_dt.tzinfo is None:
                        entry_dt = entry_dt.replace(tzinfo=ET)
                    hold_hours = (now - entry_dt).total_seconds() / 3600
                    if hold_hours >= config.HIGH_VOL_EARLY_EXIT_MIN_HOLD_H:
                        logger.info(
                            "HIGH_VOL EARLY EXIT: %s held %.1fh, P&L=%+.2f%% — closing stale losing day trade",
                            symbol, hold_hours, pnl_pct,
                        )
                        close_position(
                            symbol, alpaca, open_positions, current_price,
                            _try_compute_iv(symbol, alpaca), "highvol_early_exit",
                        )
                except Exception as exc:
                    logger.debug("HIGH_VOL early exit: entry_time parse failed for %s: %s", symbol, exc)
        elif (
            pos.get("mode_at_entry") == "DAY_TRADE"
            and not pos.get("is_swing", False)
            and config.MOMENTUM_REVERSAL_MIN_LOSS_PCT <= -pnl_pct <= config.MOMENTUM_REVERSAL_MAX_LOSS_PCT
            and (pos.get("entry_indicators") or {}).get("macd_hist", 0.0) > 0
        ):
            # MACD flipped negative after a bullish entry — momentum has reversed.
            # Cut the loss at -0.5% to -1.5% rather than waiting for the -2% SL ceiling.
            current_iv = _try_compute_iv(symbol, alpaca)
            if current_iv and current_iv.macd_hist < 0:
                entry_macd = (pos.get("entry_indicators") or {}).get("macd_hist", 0.0)
                logger.info(
                    "MOMENTUM REVERSAL EXIT: %s P&L=%+.2f%% MACD flipped (%+.4f→%+.4f) — cutting loss early",
                    symbol, pnl_pct, entry_macd, current_iv.macd_hist,
                )
                close_position(
                    symbol, alpaca, open_positions, current_price,
                    current_iv, "momentum_reversal",
                )

    flush_profit_lock_updates(open_positions)

    # Collect current prices from positions still in open_positions for drawdown check
    current_prices_for_drawdown: dict[str, float] = {}
    for symbol, pos in open_positions.items():
        try:
            current_prices_for_drawdown[symbol] = alpaca.get_latest_price(symbol)
        except Exception:
            pass
    _check_drawdown_alerts(open_positions, current_prices_for_drawdown)

    try:
        from trading.swing_manager import monitor_swing_positions
        monitor_swing_positions(alpaca)
    except Exception as exc:
        logger.warning("Swing position monitor failed: %s", exc)

    try:
        from utils.data_health import get_staleness_guard
        regime_info = load_macro_regime()
        guard       = get_staleness_guard(regime_info)
        if guard.t2_allowed:
            _check_tranche2_triggers(open_positions, alpaca, now)
        else:
            logger.debug(
                "T2 blocked by staleness guard — stale: %s",
                ", ".join(sorted(guard.stale_sources & config.DATA_DISABLE_T2)),
            )
    except Exception as exc:
        logger.warning("T2 trigger check failed: %s", exc)
