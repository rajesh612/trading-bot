"""
Telegram notification helper.
Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.
All public functions are fire-and-forget: they log a warning on failure but
never raise, so a Telegram outage never interrupts the trading loop.
"""

import logging

import requests

import config

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


# ── Midday health check ───────────────────────────────────────────────────────

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
) -> None:
    if not closes:
        _send(
            f"<b>📊 EOD Summary — no trades today</b>\n"
            f"Regime: {regime} | Portfolio: ${portfolio_value:,.2f}"
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
    ]
    _send("\n".join(lines))
