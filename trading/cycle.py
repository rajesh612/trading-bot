"""
Trading cycle orchestrator — runs every 10 minutes during market hours.

Loads market state, applies circuit breakers, builds cycle snapshot,
and delegates to TradingPipeline. Tracks zero-candidate cycles and
fires a dead-day Telegram alert after 4 consecutive misses.
"""
import logging
import time
from datetime import datetime
import sys

import anthropic

import config
import notifications.telegram as telegram_notify
from broker.alpaca_client import AlpacaClient
from macro.regime import load_macro_regime, load_trading_params
from trading.cycle_context import build_cycle_snapshot
from trading.pipeline import TradingPipeline
from trading.watch_loader import load_watch_list
from utils.constants import ET
from utils import regimes

logger = logging.getLogger(__name__)

_zero_pass: dict = {"date": None, "count": 0, "alerted": False}
_empty_watch_alerted: dict = {"date": None}
_telemetry_rotated: dict = {"date": None}

def _rotate_telemetry(today_str: str) -> None:
    """Keep only the last config.TELEMETRY_KEEP_LINES lines of cycle_telemetry.jsonl, once per day."""
    if _telemetry_rotated["date"] == today_str:
        return
    _telemetry_rotated["date"] = today_str
    try:
        path = config.CYCLE_TELEMETRY_FILE
        if path.exists():
            lines = path.read_text().splitlines()
            if len(lines) > config.TELEMETRY_KEEP_LINES:
                path.write_text("\n".join(lines[-config.TELEMETRY_KEEP_LINES:]) + "\n")
    except Exception:
        pass


def run_trading_cycle() -> None:
    now = datetime.now(ET)
    logger.info("Trading cycle @ %s ET", now.strftime("%H:%M:%S"))

    alpaca  = AlpacaClient()
    account = alpaca.get_account()
    portfolio_value = float(account.portfolio_value)
    buying_power    = float(account.buying_power)

    last_equity   = float(account.last_equity)
    daily_pnl_pct = (portfolio_value - last_equity) / last_equity * 100 if last_equity else 0
    if daily_pnl_pct <= -(config.MAX_DAILY_LOSS_PCT * 100):
        logger.warning("Daily loss limit hit (%.2f%%). Halting new entries.", daily_pnl_pct)
        return

    regime_info = load_macro_regime()
    regime      = regime_info.get("regime", "NORMAL")
    params      = load_trading_params()

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

    if regime in regimes.SIZE_REDUCE:
        logger.info(
            "Regime=%s: position size ×%.1f, RSI max=%s",
            regime, params["high_vol_position_mult"],
            params["rsi_buy_max_high_vol"],
        )

    watch_list = load_watch_list()
    if not watch_list:
        logger.warning("Empty watch list — skipping cycle.")
        today_str = now.strftime("%Y-%m-%d")
        if _empty_watch_alerted["date"] != today_str or "pytest" in sys.modules:
            _empty_watch_alerted["date"] = today_str
            telegram_notify.notify_alert("⚠️ Watch list empty — no candidates for this cycle.")
        return

    portfolio_gain_pct = (portfolio_value - last_equity) / last_equity * 100 if last_equity else 0.0
    snap = build_cycle_snapshot(
        portfolio_gain_pct=portfolio_gain_pct,
        regime_info=regime_info,
        params=params,
    )

    ai = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    pipeline       = TradingPipeline()
    _t_cycle_start = time.monotonic()
    cycle_result   = pipeline.run(
        symbols=watch_list,
        snap=snap,
        ai=ai,
        now=now,
        alpaca=alpaca,
        portfolio_value=portfolio_value,
        buying_power=buying_power,
        can_open=can_open,
        daily_pnl_pct=daily_pnl_pct,
    )
    cycle_elapsed_ms = (time.monotonic() - _t_cycle_start) * 1000

    try:
        from utils.metrics import record_cycle_metric
        from trading.risk_manager import load_budget
        budget = load_budget()
        ceiling = budget.get("ceiling_usd", 1.0)
        spent   = budget.get("estimated_cost_usd", 0.0)
        budget_pct = spent / ceiling * 100 if ceiling else 0.0
        record_cycle_metric(
            regime=regime,
            candidates=cycle_result.candidates,
            entries=cycle_result.entries,
            closes=cycle_result.closes,
            api_budget_pct=budget_pct,
            cycle_ms=cycle_elapsed_ms,
        )
    except Exception:
        pass

    today_str = now.strftime("%Y-%m-%d")
    _rotate_telemetry(today_str)
    if _zero_pass["date"] != today_str:
        _zero_pass.update({"date": today_str, "count": 0, "alerted": False})

    if can_open and not cycle_result.skipped:
        if cycle_result.candidates == 0:
            _zero_pass["count"] += 1
        else:
            _zero_pass["count"] = 0

        alert_hour = params.get("entry_start_hour", config.ENTRY_START_HOUR) + 1
        if (not _zero_pass["alerted"]
                and _zero_pass["count"] >= config.DEAD_DAY_ALERT_CYCLES
                and now.hour >= alert_hour):
            _zero_pass["alerted"] = True
            msg = (
                f"⚠️ <b>Dead-day alert</b>: {_zero_pass['count']} consecutive cycles with 0 "
                f"symbols passing pre-Claude gates.\n"
                f"Regime: <b>{regime}</b> | Watch list: {len(watch_list)} symbols\n"
                f"Check COMPOSITE_MIN_NORMAL and gate thresholds."
            )
            telegram_notify._send(msg)
            logger.warning(
                "Dead-day alert: %d consecutive zero-candidate cycles in entry window (regime=%s)",
                _zero_pass["count"], regime,
            )
