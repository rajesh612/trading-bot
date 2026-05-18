"""
Context-string builders for Claude prompts.
Pure — no I/O, no Alpaca calls; callers supply dicts.
"""
from __future__ import annotations

from datetime import datetime

import config
import macro.economic_calendar as _ec_mod
from indicators.values import IndicatorValues


def build_stock_block(
    symbol: str,
    iv: IndicatorValues,
    research: dict,
    existing_pos: dict | None,
    atr_suggested_tp_pct: float | None = None,
    intraday_news: dict | None = None,
) -> str:
    """Compact per-symbol block sent to Claude."""
    from calendars.earnings import detect_pead_setup
    price           = iv.current_price
    research_sym    = research.get(symbol, {})
    sentiment_score = research_sym.get("sentiment_score", 0)
    sentiment_label = research_sym.get("sentiment_label", "NEUTRAL")
    risk_flag       = research_sym.get("risk_flag") or ""
    catalyst        = research_sym.get("key_catalyst") or ""
    news_summary    = (research_sym.get("summary") or "").strip()

    pos_block = ""
    if existing_pos:
        pnl_pct = (price - existing_pos["entry_price"]) / existing_pos["entry_price"] * 100
        sl = existing_pos.get("stop_loss_pct", config.STOP_LOSS_PCT)
        tp = existing_pos.get("take_profit_pct", config.TAKE_PROFIT_PCT)
        pos_block = (
            f" POS:${existing_pos['entry_price']:.2f}×{existing_pos['qty']:.0f}"
            f" P&L={pnl_pct:+.2f}% SL={sl:.1f}% TP={tp:.1f}%"
        )

    dvol = f"{iv.daily_volume_ratio:.2f}x"
    if iv.daily_volume_ratio < 0.5:
        dvol += "LOW"
    elif iv.daily_volume_ratio > 3.0:
        dvol += "HIGH"

    pead_ok, pead_score = detect_pead_setup(symbol)
    pead_line = f" PEAD={pead_score}/100" if pead_ok else ""

    vwap_dir  = "↑" if price >= iv.vwap else "↓"
    risk_str  = f" risk={risk_flag}" if risk_flag else ""
    cat_str   = f' cat="{catalyst}"' if catalyst else ""
    news_str  = f"\n  {news_summary}" if news_summary else ""

    intraday    = intraday_news or {}
    intra_shock = intraday.get("shock_score", 0)
    intra_sent  = intraday.get("sentiment_score", sentiment_score)

    atr_tp_line = (
        f"\n  ATR-TP={atr_suggested_tp_pct:.1f}% (ATR-based). Override if catalyst justifies."
        if atr_suggested_tp_pct is not None else ""
    )

    return (
        f'<stock symbol="{symbol}">\n'
        f"  composite={iv.composite_score:+.1f} signal={iv.signal}{pos_block}\n"
        f"  P=${price:.2f} VWAP{vwap_dir}({iv.vwap_slope:+.3f})"
        f" RSI={iv.rsi:.1f}({iv.rsi_slope:+.1f})"
        f" MACD={iv.macd_hist:+.4f} BB[{iv.bb_lower:.2f},{iv.bb_upper:.2f}]"
        f" EMA9/21={iv.ema9:.2f}/{iv.ema21:.2f} Stoch={iv.stoch_k:.0f}/{iv.stoch_d:.0f}"
        f" OR={iv.or_position} Vol={dvol}{pead_line}"
        f"{atr_tp_line}\n"
        f"  SENT:{sentiment_label}({sentiment_score:+d})"
        f" intraday:shock={intra_shock} sent={intra_sent:+d}"
        f"{cat_str}{risk_str}"
        f"{news_str}\n"
        f"</stock>"
    )


def build_exit_stock_block(
    symbol: str,
    iv: IndicatorValues,
    position: dict,
    research: dict,
    intraday_news: dict | None = None,
    now: datetime | None = None,
) -> str:
    """Compact block for thesis-break exit evaluation."""
    entry_price = position.get("entry_price", 0)
    pnl_pct = (iv.current_price - entry_price) / entry_price * 100 if entry_price > 0 else 0.0
    entry_composite = position.get("entry_composite_score", 0)
    composite_delta = iv.composite_score - entry_composite
    sentiment_score = research.get(symbol, {}).get("sentiment_score", 0)
    sl_pct = position.get("stop_loss_pct", config.STOP_LOSS_PCT)

    held_str = ""
    entry_ts = position.get("timestamp", "")
    if entry_ts and now:
        try:
            entry_dt = datetime.fromisoformat(entry_ts)
            if entry_dt.tzinfo is None:
                entry_dt = entry_dt.replace(tzinfo=now.tzinfo)
            held_str = f" held={int((now - entry_dt).total_seconds() / 60)}min"
        except Exception:
            pass

    intraday    = intraday_news or {}
    intra_shock = intraday.get("shock_score", 0)
    intra_sent  = intraday.get("sentiment_score", sentiment_score)

    return (
        f'<stock symbol="{symbol}">\n'
        f"  P=${iv.current_price:.2f} entry=${entry_price:.2f}"
        f" PnL={pnl_pct:+.2f}% SL={sl_pct:.1f}%{held_str}\n"
        f"  composite={iv.composite_score:+.1f} (Δ{composite_delta:+.1f} from entry)"
        f" sentiment={sentiment_score:+d}"
        f" intraday:shock={intra_shock} sent={intra_sent:+d}\n"
        f"</stock>"
    )


def build_stable_system_context(
    regime_info: dict,
    fred_cache: dict,
    research_meta: dict,
) -> str:
    """
    Assemble the stable portion of market context for the Claude system prompt.
    Contains no timestamps or live prices — safe for Anthropic prompt cache all day.
    Pure — callers pass dicts in; no file I/O here.
    """
    regime        = regime_info.get("regime", "NORMAL")
    health        = regime_info.get("macro_health_score", 0)
    vix           = regime_info.get("vix_current", 0.0)
    vix_90d       = regime_info.get("vix_90d_avg", 0.0)
    vix_trend     = regime_info.get("vix_trend", "unknown")
    spy5d         = regime_info.get("spy_5day_return", 0.0)
    green         = regime_info.get("consecutive_green_days", 0)
    red           = regime_info.get("consecutive_red_days", 0)
    sign_chg      = regime_info.get("spy_5day_sign_changes", 0)
    sector_scores = regime_info.get("sector_scores", {})

    fed   = fred_cache.get("fed_direction", "unknown")
    cpi   = fred_cache.get("cpi_trend", "unknown")
    unemp = "rising" if fred_cache.get("unemployment_rising") else "stable"

    shock      = research_meta.get("macro_shock_score", 0)
    shock_line = f"{shock}/100" + (" — elevated macro stress" if shock >= 40 else "")
    caps       = research_meta.get("capitulation_candidates", [])
    cap_str    = ", ".join(caps) if caps else "None"

    vix_note    = f" (90d avg={vix_90d:.1f}, {vix_trend})" if vix_90d > 0 else ""
    weekly_line = (
        f"Weekly: SPY{spy5d:+.1f}% ({green}g/{red}r consecutive)"
        + (f" sign_changes={sign_chg} (choppy)" if sign_chg >= 3 else "")
    )
    sector_score_str = " ".join(f"{k}={v:.0f}" for k, v in sector_scores.items())

    from macro.breadth_signals import build_breadth_context_line
    breadth_line = build_breadth_context_line({
        "hyg_ret5d": regime_info.get("hyg_ret5d", 0.0),
        "iwm_ret5d": regime_info.get("iwm_ret5d", 0.0),
        "spy_ret5d": spy5d,
        "vix_current": vix,
        "vix3m": regime_info.get("vix3m", 0.0),
    })

    return (
        f"Daily regime: {regime} | health={health:+d} | VIX={vix:.1f}{vix_note}\n"
        f"{weekly_line}\n"
        f"{breadth_line}\n"
        f"Sector scores: {sector_score_str}\n"
        f"FRED: Fed {fed} | CPI {cpi} | Unemployment {unemp}\n"
        f"Macro shock: {shock_line} | Capitulation watch: {cap_str}"
    )


def build_dynamic_user_context(now: datetime) -> str:
    """
    Assemble the dynamic (per-call) market snapshot for the Claude user message.
    Contains live intraday bias, event risk, and current time.
    Must NOT be placed in the system block — changes every few minutes.
    """
    from macro.intraday_context import get_intraday_context
    intraday = get_intraday_context(now)
    intraday_lines = intraday.to_context_lines() if intraday else "Intraday bias: unavailable"

    event_active, event_name = _ec_mod.is_event_risk_window(now)
    event_line = f"⚠ ACTIVE: {event_name} — avoid new entries" if event_active else "None active"

    return (
        f"MARKET SNAPSHOT ({now.strftime('%H:%M')} ET):\n"
        f"{intraday_lines}\n"
        f"Event risk: {event_line}"
    )
