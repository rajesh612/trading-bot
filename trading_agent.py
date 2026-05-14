"""
Trading agent — thin orchestrator.

Owns: run_trading_cycle(), run_position_monitor(), module-level imports.
All business logic lives in trading.pipeline, trading.closer,
trading.claude_caller, trading.position_sizer, and trading.journal.
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic

import config
import telegram_notify
from alpaca_client import AlpacaClient
from macro_regime import load_macro_regime, load_trading_params
from utils.io_utils import load_json, save_json
from trading.journal import (
    _POSITIONS_LOCK,
    load_open_positions,
    save_open_positions,
    load_daily_closed,
    record_daily_closed,
    reset_daily_closed,
    append_trade,
    archive_old_journal_entries,
    count_todays_losses,
    flush_profit_lock_updates,
)
from trading.closer import close_position, force_close_all, _apply_profit_lock
from trading.cycle_context import build_cycle_snapshot
from trading.pipeline import TradingPipeline
# Flash crash monitor re-exported for backward compatibility
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

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


# ── Persistence helpers ───────────────────────────────────────────────────────

def load_watch_list() -> list[str]:
    if config.WATCH_LIST_FILE.exists():
        return [t.strip().upper() for t in config.WATCH_LIST_FILE.read_text().splitlines() if t.strip()]
    return []


# ── Position monitor (runs every 60 s) ───────────────────────────────────────

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

            entry_price = pos["entry_price"]
            sl_price    = entry_price * (1 - pos.get("stop_loss_pct", config.STOP_LOSS_PCT) / 100)
            tp_price    = entry_price * (1 + pos.get("take_profit_pct", config.TAKE_PROFIT_PCT) / 100)
            try:
                current_price = alpaca.get_latest_price(symbol)
            except Exception:
                current_price = entry_price

            if current_price <= sl_price * 1.005:
                close_reason = "bracket_stop_loss_filled"
                exit_price   = sl_price
            elif current_price >= tp_price * 0.995:
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
            })
            record_daily_closed(symbol)
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

        entry_price = pos["entry_price"]
        pnl_pct     = (current_price - entry_price) / entry_price * 100
        sl_pct      = pos.get("stop_loss_pct", config.STOP_LOSS_PCT)
        tp_pct      = pos.get("take_profit_pct", config.TAKE_PROFIT_PCT)

        if pnl_pct <= -sl_pct:
            logger.warning(
                "MONITOR STOP-LOSS: %s P&L=%+.2f%% breached SL=%.1f%% — closing (mid-cycle)",
                symbol, pnl_pct, -sl_pct,
            )
            close_position(symbol, alpaca, open_positions, current_price, None, "monitor_stop_loss")
        elif pnl_pct >= tp_pct:
            logger.info(
                "MONITOR TAKE-PROFIT: %s P&L=%+.2f%% hit TP=%.1f%% — closing (mid-cycle)",
                symbol, pnl_pct, tp_pct,
            )
            close_position(symbol, alpaca, open_positions, current_price, None, "monitor_take_profit")

    flush_profit_lock_updates(open_positions)

    try:
        from swing_manager import monitor_swing_positions
        monitor_swing_positions(alpaca)
    except Exception as exc:
        logger.warning("Swing position monitor failed: %s", exc)


# ── Main trading cycle ────────────────────────────────────────────────────────

def run_trading_cycle():
    now = datetime.now(ET)
    logger.info("Trading cycle @ %s ET", now.strftime("%H:%M:%S"))

    alpaca  = AlpacaClient()
    account = alpaca.get_account()
    portfolio_value = float(account.portfolio_value)
    buying_power    = float(account.buying_power)

    # Daily loss circuit-breaker (3%)
    last_equity  = float(account.last_equity)
    daily_pnl_pct = (portfolio_value - last_equity) / last_equity * 100 if last_equity else 0
    if daily_pnl_pct <= -(config.MAX_DAILY_LOSS_PCT * 100):
        logger.warning("Daily loss limit hit (%.2f%%). Halting new entries.", daily_pnl_pct)
        return

    regime_info = load_macro_regime()
    regime      = regime_info.get("regime", "NORMAL")
    params      = load_trading_params()

    # Flash crash lockout check
    flash_crash_active = regime_info.get("flash_crash_active", False)
    flash_crash_until  = regime_info.get("flash_crash_until")
    can_open           = True
    if flash_crash_active and flash_crash_until:
        try:
            until_dt = datetime.fromisoformat(flash_crash_until)
            if now < until_dt:
                logger.warning("Flash crash lockout active until %s — no entries.", flash_crash_until)
                can_open = False
            else:
                flash_crash_active = False
        except Exception:
            pass

    if regime == "CRISIS" or flash_crash_active:
        logger.warning(
            "CRISIS/flash-crash regime: VIX=%.1f | health=%+d — skipping all new entries.",
            regime_info.get("vix_current", 0), regime_info.get("macro_health_score", 0),
        )
        can_open = False

    entry_start = now.replace(hour=params["entry_start_hour"], minute=0, second=0, microsecond=0)
    entry_end   = now.replace(hour=params["entry_end_hour"],   minute=0, second=0, microsecond=0)
    if not (entry_start <= now < entry_end):
        logger.debug(
            "Outside entry window (%d–%d ET) — no new entries.",
            params["entry_start_hour"], params["entry_end_hour"],
        )
        can_open = False

    if regime in ("DEFENSIVE", "CHOPPY"):
        logger.info(
            "Regime=%s: position size ×%.1f, RSI max=%s",
            regime, params["high_vol_position_mult"],
            params["rsi_buy_max_high_vol"],
        )

    watch_list = load_watch_list()
    if not watch_list:
        logger.warning("Empty watch list — skipping cycle.")
        return

    portfolio_gain_pct = (portfolio_value - last_equity) / last_equity * 100 if last_equity else 0.0
    snap = build_cycle_snapshot(
        portfolio_gain_pct=portfolio_gain_pct,
        regime_info=regime_info,
        params=params,
    )

    ai = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    pipeline = TradingPipeline()
    pipeline.run(
        symbols=watch_list,
        snap=snap,
        ai=ai,
        now=now,
        alpaca=alpaca,
        portfolio_value=portfolio_value,
        buying_power=buying_power,
        can_open=can_open,
    )


if __name__ == "__main__":
    import logging as _l
    _l.basicConfig(level=_l.INFO)
    run_trading_cycle()
