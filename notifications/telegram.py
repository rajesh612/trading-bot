"""
Telegram notification helper.
Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.
All public functions are fire-and-forget: they log a warning on failure but
never raise, so a Telegram outage never interrupts the trading loop.
"""

import logging
from datetime import datetime

import requests

import config
from utils.constants import ET
from utils.io_utils import load_json, save_json

logger = logging.getLogger(__name__)

_BASE = "https://api.telegram.org/bot"
_TIMEOUT = 10


def _send(text: str) -> None:
    token   = config.TELEGRAM_BOT_TOKEN
    chat_id = config.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"{_BASE}{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=_TIMEOUT,
        )
    except Exception as exc:
        logger.warning(f"Telegram send failed: {exc}")


# ── Lifecycle ─────────────────────────────────────────────────────────────────

def notify_startup(regime: str, open_positions: int) -> None:
    _send(
        f"<b>🟢 Bot started</b>\n"
        f"Regime: <b>{regime}</b> | Open positions: {open_positions}"
    )


def notify_shutdown(reason: str = "normal stop") -> None:
    _send(f"<b>🔴 Bot stopped</b> — {reason}")


def notify_degraded_integrations(items: list[str]) -> None:
    """Send one Telegram message listing degraded integrations at startup."""
    if not items:
        return
    bullet_list = "\n".join(f"  • {item}" for item in items)
    _send(f"<b>⚠️ Bot started with degraded integrations:</b>\n{bullet_list}")


# ── Job failures ──────────────────────────────────────────────────────────────

def notify_job_failure(job_name: str, error: str) -> None:
    _send(
        f"<b>❌ Job failed: {job_name}</b>\n"
        f"<code>{error[:300]}</code>"
    )


def notify_file_corruption(filename: str) -> None:
    _send(
        f"<b>⚠️ Corrupt state file reset</b>\n"
        f"<code>{filename}</code> was invalid JSON — reset to default.\n"
        f"Backup saved as <code>{filename}.corrupt.json</code>"
    )


# ── Claude / API ──────────────────────────────────────────────────────────────

def notify_claude_disabled(reason: str) -> None:
    _send(
        f"<b>🚫 Claude disabled — no new trades until restart</b>\n"
        f"<code>{reason[:300]}</code>"
    )


def notify_budget_escalation(spent: float, new_ceiling: float) -> None:
    _send(
        f"<b>⚠️ API budget escalated — exit pool exhausted.</b>\n"
        f"Spent: ${spent:.4f} | New ceiling: ${new_ceiling:.2f}\n"
        f"Budget ceiling raised to ELEVATED mode to protect open positions."
    )


def notify_budget_warning(used_usd: float, ceiling_usd: float) -> None:
    pct = used_usd / ceiling_usd * 100 if ceiling_usd else 0
    _send(
        f"<b>💸 API budget {pct:.0f}% used</b>\n"
        f"${used_usd:.3f} of ${ceiling_usd:.2f} daily ceiling"
    )


_daily_loss_warned: dict = {"date": None}  # warn once per day only

def notify_daily_loss_warning(daily_pnl_pct: float, ceiling_pct: float) -> None:
    """Warn once per day when daily P&L hits -DAILY_LOSS_WARN_PCT (approaching halt)."""
    today = datetime.now(ET).date().isoformat()
    if _daily_loss_warned.get("date") == today:
        return
    _daily_loss_warned["date"] = today
    _send(
        f"<b>⚠️ Daily loss warning: {daily_pnl_pct:.2f}%</b>\n"
        f"Approaching daily halt at -{ceiling_pct:.0f}%. New entries continuing but monitor closely."
    )


# ── Intraday risk alerts ───────────────────────────────────────────────────────

_budget_warn_sent: dict = {"date": None}

def notify_budget_intraday_warning(spent_usd: float, ceiling_usd: float) -> None:
    """Alert once per day when entry pool crosses 75%."""
    today = datetime.now(ET).date().isoformat()
    if _budget_warn_sent.get("date") == today:
        return
    _budget_warn_sent["date"] = today
    pct = spent_usd / ceiling_usd * 100 if ceiling_usd else 0
    _send(
        f"<b>💸 Entry budget {pct:.0f}% used intraday</b>\n"
        f"${spent_usd:.3f} of ${ceiling_usd:.2f} — new entries may slow."
    )


_drawdown_alerted: dict = {}  # {symbol: date}

def notify_position_drawdown(symbol: str, pnl_pct: float, stop_pct: float) -> None:
    """Alert once per symbol per day when position drawdown > 2% but above stop."""
    today = datetime.now(ET).date().isoformat()
    if _drawdown_alerted.get(symbol) == today:
        return
    _drawdown_alerted[symbol] = today
    _send(
        f"<b>⚠️ {symbol} drawdown {pnl_pct:.1f}%</b>\n"
        f"Stop at {stop_pct:.1f}% — monitor for cascade risk."
    )


_sector_flip_alerted: dict = {}  # {sector: date}

def notify_sector_flip(sector_etf: str, change_pct: float, affected_symbols: list[str]) -> None:
    """Alert once per sector per day on intraday sector reversal."""
    today = datetime.now(ET).date().isoformat()
    if _sector_flip_alerted.get(sector_etf) == today:
        return
    _sector_flip_alerted[sector_etf] = today
    syms = ", ".join(affected_symbols[:5])
    _send(
        f"<b>📉 Sector flip: {sector_etf} {change_pct:+.1f}%</b>\n"
        f"Open positions at risk: {syms}"
    )


def notify_stale_research(age_min: float) -> None:
    """Alert when research report is stale."""
    _send(
        f"<b>⏰ Stale research report ({age_min:.0f} min old)</b>\n"
        "Research job may have failed. Sentiment signals are degraded."
    )


def notify_correlated_exposure(sector: str, count: int, symbols: list[str]) -> None:
    """Alert when sector concentration is high."""
    syms = ", ".join(symbols)
    _send(
        f"<b>⚠️ Concentrated exposure: {sector} ({count} positions)</b>\n"
        f"Symbols: {syms}\nSector move could trigger correlated cascade."
    )


# ── Regime ────────────────────────────────────────────────────────────────────

def notify_regime_change(old: str, new: str, health: int) -> None:
    emoji = "🚨" if new in ("CRISIS", "CAPITULATION") else "📊"
    _send(
        f"{emoji} <b>Regime change: {old} → {new}</b>\n"
        f"Health score: {health:+d}"
    )


def notify_flash_crash(symbol: str, drop_pct: float, until: str) -> None:
    _send(
        f"<b>🚨 FLASH CRASH detected</b>\n"
        f"{symbol} dropped {drop_pct:.1f}%\n"
        f"CRISIS lockout active until {until}"
    )


# ── Orders ────────────────────────────────────────────────────────────────────

def notify_buy(
    symbol: str, qty: float, price: float,
    sl: float, tp: float, mode: str, score: float, confidence: int,
) -> None:
    _send(
        f"<b>📈 BUY {symbol}</b>\n"
        f"{qty:.0f} shares @ ${price:.2f}\n"
        f"SL ${sl:.2f} | TP ${tp:.2f}\n"
        f"Mode: {mode} | Score: {score:+.1f} | Confidence: {confidence}%"
    )


def notify_tranche2_add(
    symbol: str, qty: float, t2_price: float,
    blended_entry: float, blended_sl_price: float, blended_sl_pct: float,
    stop_order_placed: bool,
) -> None:
    stop_status = "placed" if stop_order_placed else "software-only"
    _send(
        f"<b>📊 T2 ADD {symbol}</b>\n"
        f"{qty:.0f} shares @ ${t2_price:.2f}\n"
        f"Blended entry: ${blended_entry:.2f} | SL ${blended_sl_price:.2f} ({blended_sl_pct:.1f}%)\n"
        f"Stop order: {stop_status}"
    )


def notify_close(
    symbol: str, qty: float,
    entry: float, exit_price: float,
    pnl_dollar: float, pnl_pct: float,
    reason: str,
) -> None:
    emoji = "✅" if pnl_dollar >= 0 else "🔻"
    _send(
        f"{emoji} <b>CLOSE {symbol}</b>\n"
        f"{qty:.0f} shares | Entry ${entry:.2f} → Exit ${exit_price:.2f}\n"
        f"P&L: <b>${pnl_dollar:+.2f} ({pnl_pct:+.2f}%)</b>\n"
        f"Reason: {reason}"
    )


# ── Batch buy notification ────────────────────────────────────────────────────

def notify_buy_batch(entries: list[dict], regime: str) -> None:
    """Single summary Telegram for multiple entries in one cycle."""
    if not entries:
        return
    if len(entries) == 1:
        e = entries[0]
        notify_buy(symbol=e["symbol"], qty=e["qty"], price=e["price"],
                   sl=e["sl"], tp=e["tp"], mode=e.get("mode", "DAY_TRADE"),
                   score=e["score"], confidence=e["confidence"])
        return
    lines = [f"<b>📈 {len(entries)} entries — {regime}</b>"]
    for e in entries:
        lines.append(
            f"  • <b>{e['symbol']}</b> {e['qty']:.0f}sh @${e['price']:.2f} "
            f"SL${e['sl']:.2f} TP${e['tp']:.2f} score={e['score']:+.0f}"
        )
    _send("\n".join(lines))


# ── Morning watch list ────────────────────────────────────────────────────────

def notify_watch_list(symbols: list[str], regime: str) -> None:
    today = datetime.now(ET).date().isoformat()
    sentinel = load_json(config.WATCH_LIST_NOTIF_FILE, {})
    if sentinel.get("date") == today:
        logger.info("notify_watch_list: already sent today — suppressed duplicate.")
        return
    save_json(config.WATCH_LIST_NOTIF_FILE, {"date": today})

    if not symbols:
        _send("<b>⚠️ Watch list empty</b> — no tradeable candidates found for today.")
        return
    tickers = "  ".join(symbols)
    _send(
        f"<b>📋 Watch list ({len(symbols)} stocks) — {regime}</b>\n"
        f"<code>{tickers}</code>"
    )


# ── Midday health check ───────────────────────────────────────────────────────

_degraded_alerted: dict[str, bool] = {}   # {source_id_date: True}


def notify_data_degraded(source_id: str, guard) -> None:
    """One-shot Telegram alert when a data source activates stale fallback.

    Deduped per source per calendar day.
    guard is a StalenessGuard instance.
    """
    today = datetime.now(ET).strftime("%Y-%m-%d")
    sentinel = f"{source_id}_{today}"
    if _degraded_alerted.get(sentinel):
        return
    _degraded_alerted[sentinel] = True

    penalties = [
        f"+{v} composite"
        for k, v in config.DATA_COMPOSITE_PENALTY.items()
        if k in guard.stale_sources and v > 0
    ]
    blocks = []
    if not guard.auto_execute_allowed:
        blocks.append("auto-execute")
    if not guard.t2_allowed:
        blocks.append("T2")

    msg = (
        f"⚠️ <b>DATA DEGRADED — {source_id.upper()}</b>\n"
        f"Fallback active. "
        f"Penalties: {', '.join(penalties) or 'none'}. "
        f"Blocked: {', '.join(blocks) or 'none'}."
    )
    notify_alert(msg)


def notify_alert(message: str) -> None:
    """Send a generic one-off alert message."""
    _send(message)


def notify_no_trades_midday(regime: str, cycles_run: int, budget_pct: float) -> None:
    _send(
        f"<b>⚠️ No trades by 12:00 PM ET</b>\n"
        f"Regime: <b>{regime}</b> | Cycles run: {cycles_run}\n"
        f"API budget used: {budget_pct:.0f}%\n"
        f"Bot is running but all candidates are being filtered."
    )


# ── EOD summary ───────────────────────────────────────────────────────────────

def notify_eod_summary(
    closes: list,
    day_pnl: float,
    day_pnl_pct: float,
    portfolio_value: float,
    regime: str,
    api_budget_pct: float = 0.0,
    cycles_run: int = 0,
    candidates_passed: int = 0,
    regime_flips: int = 0,
) -> None:
    if not closes:
        _send(
            f"<b>📊 EOD Summary — no trades today</b>\n"
            f"Regime: {regime} | Portfolio: ${portfolio_value:,.2f}\n"
            f"API budget: {api_budget_pct:.0f}% used | Cycles: {cycles_run} | "
            f"Candidates: {candidates_passed} | Regime flips: {regime_flips}"
        )
        return

    winners = [t for t in closes if t.get("pnl_dollar", 0) > 0]
    win_rate = len(winners) / len(closes) * 100

    best  = max(closes, key=lambda t: t.get("pnl_dollar", 0))
    worst = min(closes, key=lambda t: t.get("pnl_dollar", 0))

    lines = [
        f"<b>📊 EOD Summary</b>",
        f"P&L: <b>${day_pnl:+,.2f} ({day_pnl_pct:+.2f}%)</b>",
        f"Trades: {len(closes)} | Win rate: {win_rate:.0f}%",
        f"Best: {best['symbol']} ${best.get('pnl_dollar',0):+.2f} | "
        f"Worst: {worst['symbol']} ${worst.get('pnl_dollar',0):+.2f}",
        f"Regime: {regime} | Portfolio: ${portfolio_value:,.2f}",
        f"API budget: {api_budget_pct:.0f}% used | Cycles: {cycles_run} | "
        f"Candidates: {candidates_passed} | Regime flips: {regime_flips}",
    ]
    _send("\n".join(lines))
