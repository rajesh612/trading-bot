import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config
from utils.io_utils import load_json, save_json

logger = logging.getLogger("main")
ET = ZoneInfo("America/New_York")


def reset_bot_health() -> None:
    """Write crash_count=0 to bot_health.json. Called on clean shutdown."""
    try:
        data = {
            "crash_count": 0,
            "window_start": datetime.now(ET).isoformat(),
            "backoff_until": None,
        }
        save_json(config.BOT_HEALTH_FILE, data)
    except Exception as exc:
        logger.warning(f"Could not reset bot_health.json: {exc}")


def check_crash_loop() -> None:
    """Increment crash counter and sleep CRASH_BACKOFF_MINUTES if loop detected."""
    now = datetime.now(ET)
    try:
        data = load_json(config.BOT_HEALTH_FILE, {"crash_count": 0, "window_start": now.isoformat(), "backoff_until": None})

        window_start = datetime.fromisoformat(data.get("window_start", now.isoformat()))
        if (now - window_start).total_seconds() / 60 > config.CRASH_LOOP_WINDOW_MINUTES:
            data["crash_count"] = 0
            data["window_start"] = now.isoformat()

        crash_count = data.get("crash_count", 0)
        if crash_count == 0:
            data["window_start"] = now.isoformat()
        crash_count += 1
        data["crash_count"] = crash_count

        if crash_count >= config.CRASH_LOOP_THRESHOLD:
            backoff_until = now + timedelta(minutes=config.CRASH_BACKOFF_MINUTES)
            data["backoff_until"] = backoff_until.isoformat()

        save_json(config.BOT_HEALTH_FILE, data)  # single atomic write

        if crash_count >= config.CRASH_LOOP_THRESHOLD:
            logger.critical(
                f"CRASH LOOP: {crash_count} crashes in {config.CRASH_LOOP_WINDOW_MINUTES} min "
                f"— pausing {config.CRASH_BACKOFF_MINUTES} min until "
                f"{backoff_until.strftime('%H:%M ET')}"
            )
            time.sleep((backoff_until - datetime.now(ET)).total_seconds())
            reset_bot_health()
    except Exception as exc:
        logger.warning(f"Crash loop check failed: {exc}")


def reconcile_positions() -> None:
    """Sync local open_positions.json with Alpaca's actual open positions at startup.

    Three discrepancies can occur while the bot is offline:
    1. Bracket SL/TP fired → position closed in Alpaca but still in local state.
    2. Bot crashed mid-entry → position open in Alpaca but absent from local state.
    3. Bot starts after 3:55 PM with positions still open → force-close immediately.
    Swing positions (swing_positions.json) are intentional overnight holds and are
    always skipped — swing_manager owns them.
    """
    from trading_agent import load_open_positions, save_open_positions, append_trade
    from alpaca_client import AlpacaClient
    # Deferred import to avoid circular reference at module load
    import scheduler_jobs as _sj

    logger.info("RECONCILE: Checking Alpaca positions against local state…")
    try:
        alpaca = AlpacaClient()
        alpaca_positions = alpaca.get_positions()        # {symbol: AlpacaPosition}
        local_positions  = load_open_positions()         # {symbol: dict}
        now   = datetime.now(ET)
        changed = False

        # Swing positions are held overnight intentionally — skip them in all checks
        try:
            from swing_manager import load_swing_positions
            swing_symbols = set(load_swing_positions().keys())
        except Exception:
            swing_symbols = set()
        if swing_symbols:
            logger.info(f"RECONCILE: {len(swing_symbols)} swing position(s) active — skipping: {sorted(swing_symbols)}")

        # ── 1. In Alpaca but not locally ──────────────────────────────────
        # Bot crashed between placing the order and writing open_positions.json,
        # or the entry was placed but the bot never wrote local state.
        for symbol, ap in alpaca_positions.items():
            if symbol not in local_positions:
                if symbol in swing_symbols:
                    continue  # swing hold — swing_manager owns it, not a crash
                entry_price = float(ap.avg_entry_price)
                qty         = int(float(ap.qty))
                logger.warning(
                    f"RECONCILE: {symbol} open in Alpaca ({qty}@${entry_price:.2f}) "
                    "but missing from local state — restoring with default SL/TP"
                )
                local_positions[symbol] = {
                    "symbol":               symbol,
                    "qty":                  qty,
                    "entry_price":          entry_price,
                    "stop_loss_pct":        config.STOP_LOSS_PCT,
                    "take_profit_pct":      config.TAKE_PROFIT_PCT,
                    "stop_loss_price":      round(entry_price * (1 - config.STOP_LOSS_PCT  / 100), 2),
                    "take_profit_price":    round(entry_price * (1 + config.TAKE_PROFIT_PCT / 100), 2),
                    "bracket_order_id":     None,
                    "timestamp":            now.isoformat(),
                    "entry_composite_score": None,
                    "entry_sentiment_score": None,
                    "claude_reasoning":     "Restored at startup — original entry data unavailable.",
                    "indicator_signals":    {},
                    "entry_indicators":     {},
                }
                changed = True

        # ── 2. In local state but gone from Alpaca ────────────────────────
        # A bracket SL or TP order fired while the bot was offline.
        for symbol in list(local_positions.keys()):
            if symbol not in alpaca_positions:
                pos = local_positions[symbol]
                try:
                    exit_price = alpaca.get_latest_price(symbol)
                except Exception:
                    exit_price = pos["entry_price"]
                    logger.warning(f"RECONCILE: Cannot fetch last price for {symbol} — using entry price for P&L")

                pnl_pct    = (exit_price - pos["entry_price"]) / pos["entry_price"] * 100
                pnl_dollar = (exit_price - pos["entry_price"]) * pos["qty"]
                logger.warning(
                    f"RECONCILE: {symbol} in local state but gone from Alpaca — "
                    f"bracket fired while offline. P&L≈{pnl_pct:+.2f}% (${pnl_dollar:+.2f})"
                )
                append_trade({
                    "type":                      "CLOSE",
                    "symbol":                    symbol,
                    "timestamp":                 now.isoformat(),
                    "exit_price":                round(exit_price, 4),
                    "entry_price":               round(pos["entry_price"], 4),
                    "qty":                       pos["qty"],
                    "pnl_pct":                   round(pnl_pct, 2),
                    "pnl_dollar":                round(pnl_dollar, 2),
                    "close_reason":              "bracket_filled_while_bot_offline",
                    "entry_composite_score":     pos.get("entry_composite_score"),
                    "exit_composite_score":      None,
                    "exit_indicators":           {},
                    "entry_indicators":          pos.get("entry_indicators", {}),
                    "entry_sentiment_score":     pos.get("entry_sentiment_score"),
                    "claude_reasoning":          pos.get("claude_reasoning", ""),
                    "indicator_signals_at_entry": pos.get("indicator_signals", {}),
                })
                del local_positions[symbol]
                changed = True

        if changed:
            save_open_positions(local_positions)

        n = len(local_positions)
        if not changed:
            logger.info(f"RECONCILE: Positions in sync — {n} open, no discrepancies.")
        else:
            logger.info(f"RECONCILE: State updated — {n} position(s) now active.")

        # ── 4. Orphan BUY order drain ─────────────────────────────────────────
        # Cancel any open BUY orders left in Alpaca from a crashed trading cycle.
        # Filled bracket BUY legs have status=filled (not open) — safe to cancel all open BUYs.
        try:
            open_buys = alpaca.get_open_buy_orders()
            cancelled = 0
            for order in open_buys:
                try:
                    alpaca.cancel_order(order["id"])
                    logger.warning(
                        f"RECONCILE: Cancelled orphan BUY order {order['id']} for {order['symbol']}"
                    )
                    cancelled += 1
                except Exception as exc:
                    logger.warning(f"RECONCILE: Could not cancel order {order['id']}: {exc}")
            if cancelled:
                logger.info(f"RECONCILE: Drained {cancelled} orphan BUY order(s).")
            else:
                logger.info("RECONCILE: No orphan BUY orders found.")
        except Exception as exc:
            logger.error(f"RECONCILE: Orphan drain failed — {exc}")

        # ── 3. Starting after 3:55 PM ET with open day-trade positions ──────
        # The scheduled force_close never ran (bot crashed or laptop closed).
        # Close non-swing positions now so we don't hold day-trades overnight.
        # Swing positions are intentional holds — job_force_close already skips them.
        past_force_close = now.hour > 15 or (now.hour == 15 and now.minute >= 55)
        day_trade_open = {s: p for s, p in local_positions.items() if s not in swing_symbols}
        if past_force_close and day_trade_open:
            logger.warning(
                f"RECONCILE: Starting after 3:55 PM with {len(day_trade_open)} open "
                "day-trade position(s) — force-closing now to avoid overnight hold"
            )
            _sj.job_force_close()

    except Exception as exc:
        logger.error(f"RECONCILE: Failed — {exc}", exc_info=True)
        logger.error("RECONCILE: Local state may be stale — check Alpaca dashboard manually.")
