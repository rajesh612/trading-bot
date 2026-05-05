import json
import logging
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import certifi

import config

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

DAILY_REPORT_SENT_FILE = config.DAILY_REPORT_SENT_FILE


def _already_sent_today() -> bool:
    try:
        data = json.loads(DAILY_REPORT_SENT_FILE.read_text())
        return data.get("date") == datetime.now(ET).date().isoformat()
    except (FileNotFoundError, json.JSONDecodeError):
        return False


def _mark_sent() -> None:
    DAILY_REPORT_SENT_FILE.write_text(
        json.dumps({"date": datetime.now(ET).date().isoformat()})
    )


def build_report_body(
    trades: list,
    account: dict,
    regime: str,
    watch_list: str,
    weights: dict,
) -> tuple[str, str]:
    today_label = datetime.now(ET).strftime("%A, %B %-d %Y")
    today_date  = datetime.now(ET).strftime("%Y-%m-%d")

    closes = [t for t in trades if t.get("type") == "CLOSE"]

    portfolio_value = account.get("portfolio_value", 0.0)
    last_equity     = account.get("last_equity", portfolio_value)
    day_pnl         = portfolio_value - last_equity
    day_pnl_pct     = (day_pnl / last_equity * 100) if last_equity else 0.0

    if closes:
        subject = (
            f"Trading Bot — {today_date} | "
            f"P&L: ${day_pnl:+,.2f} ({day_pnl_pct:+.2f}%) | {len(closes)} trades"
        )
    else:
        subject = (
            f"Trading Bot — {today_date} | No trades today | "
            f"Portfolio: ${portfolio_value:,.2f}"
        )

    winners  = [t for t in closes if t.get("pnl_dollar", 0) > 0]
    losers   = [t for t in closes if t.get("pnl_dollar", 0) <= 0]
    win_rate = len(winners) / len(closes) * 100 if closes else 0.0

    lines = [
        "DAILY SUMMARY",
        f"Date:             {today_label}",
        f"Market Regime:    {regime}",
        f"Portfolio Value:  ${portfolio_value:,.2f}",
        f"Today's P&L:      ${day_pnl:+,.2f} ({day_pnl_pct:+.2f}%)",
        "",
    ]

    if closes:
        lines += [
            "TRADE RESULTS",
            (
                f"Closed trades: {len(closes)}  |  Winners: {len(winners)}  |  "
                f"Losers: {len(losers)}  |  Win rate: {win_rate:.1f}%"
            ),
            "",
            "INDIVIDUAL TRADES",
            f" {'#':>2}  {'Symbol':<7} {'Qty':>5}  {'Entry':>8}  {'Exit':>8}  "
            f"{'P&L':>10}  {'%':>7}  Reason",
        ]
        for i, t in enumerate(
            sorted(closes, key=lambda x: x.get("pnl_dollar", 0), reverse=True), 1
        ):
            lines.append(
                f" {i:>2}  {t.get('symbol','?'):<7} {t.get('qty', 0):>5}  "
                f"${t.get('entry_price', 0):>7.2f}  ${t.get('exit_price', 0):>7.2f}  "
                f"${t.get('pnl_dollar', 0):>+9,.2f}  {t.get('pnl_pct', 0):>+6.2f}%  "
                f"{t.get('close_reason', '')}"
            )
        lines.append("")
    else:
        lines += ["TRADE RESULTS", "No trades closed today.", ""]

    lines += [
        "TODAY'S WATCH LIST",
        watch_list if watch_list else "(not available)",
        "",
    ]

    if weights:
        weight_parts = [
            f"{k}: {v:.2f}%"
            for k, v in sorted(weights.items(), key=lambda x: x[1], reverse=True)
        ]
        lines += [
            "INDICATOR WEIGHTS (updated tonight)",
            "  ".join(weight_parts),
        ]

    return subject, "\n".join(lines)


def _send_email(subject: str, body: str) -> None:
    recipient = config.EMAIL_RECIPIENT or config.EMAIL_SENDER
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"]    = config.EMAIL_SENDER
    msg["To"]      = recipient
    msg.set_content(body)
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=certifi.where())
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(config.EMAIL_SENDER, config.EMAIL_APP_PASSWORD)
        server.send_message(msg)


def send_daily_report() -> None:
    if not config.EMAIL_SENDER or not config.EMAIL_APP_PASSWORD:
        logger.info("DAILY REPORT: Email credentials not configured — skipping.")
        return

    if _already_sent_today():
        logger.info("DAILY REPORT: Already sent today — skipping.")
        return

    logger.info("DAILY REPORT: Building daily summary…")

    account = {"portfolio_value": 0.0, "last_equity": 0.0}
    try:
        from alpaca_client import AlpacaClient
        acct = AlpacaClient().get_account()
        account = {
            "portfolio_value": float(acct.portfolio_value),
            "last_equity":     float(acct.last_equity),
        }
    except Exception as exc:
        logger.warning(f"DAILY REPORT: Could not fetch Alpaca account — {exc}")

    trades = []
    try:
        trades = json.loads(config.TRADE_JOURNAL_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    today         = datetime.now(ET).date().isoformat()
    todays_trades = [t for t in trades if t.get("timestamp", "").startswith(today)]

    regime = "UNKNOWN"
    try:
        regime = json.loads(config.VIX_REGIME_FILE.read_text()).get("regime", "UNKNOWN")
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    watch_list = ""
    try:
        watch_list = ", ".join(config.WATCH_LIST_FILE.read_text().strip().splitlines())
    except FileNotFoundError:
        pass

    weights = {}
    try:
        weights = json.loads(config.INDICATOR_WEIGHTS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    subject, body = build_report_body(todays_trades, account, regime, watch_list, weights)

    # Compact Telegram EOD summary — sent regardless of email outcome
    try:
        import telegram_notify
        closes = [t for t in todays_trades if t.get("type") == "CLOSE"]
        pv  = account.get("portfolio_value", 0.0)
        le  = account.get("last_equity", 0.0)
        day_pnl     = pv - le
        day_pnl_pct = (day_pnl / le * 100) if le else 0.0
        telegram_notify.notify_eod_summary(closes, day_pnl, day_pnl_pct, pv, regime)
    except Exception as exc:
        logger.warning(f"DAILY REPORT: Telegram summary failed — {exc}")

    try:
        _send_email(subject, body)
        _mark_sent()
        logger.info(f"DAILY REPORT: Sent — '{subject}'")
    except Exception as exc:
        logger.error(f"DAILY REPORT: Failed to send — {exc}")
