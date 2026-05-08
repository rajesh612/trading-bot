"""
Context-string builders for Claude prompts.
Pure — no I/O, no Alpaca calls; callers supply dicts.
"""
from __future__ import annotations

from datetime import datetime

import config
import economic_calendar as _ec_mod
from indicator_engine import IndicatorValues


def build_stock_block(
    symbol: str,
    iv: IndicatorValues,
    research: dict,
    existing_pos: dict | None,
) -> str:
    """Full per-symbol block (~200 tokens) sent to Claude."""
    from earnings_calendar import detect_pead_setup
    price           = iv.current_price
    research_sym    = research.get(symbol, {})
    sentiment_score = research_sym.get("sentiment_score", 0)
    sentiment_label = research_sym.get("sentiment_label", "NEUTRAL")
    risk_flag       = research_sym.get("risk_flag") or "None"
    catalyst        = research_sym.get("key_catalyst") or "None"
    news_summary    = research_sym.get("summary") or "No summary."

    pos_block = ""
    if existing_pos:
        pnl_pct = (price - existing_pos["entry_price"]) / existing_pos["entry_price"] * 100
        pos_block = (
            f" | OPEN POS: entry=${existing_pos['entry_price']:.2f} qty={existing_pos['qty']:.0f}"
            f" P&L={pnl_pct:+.2f}% SL={existing_pos.get('stop_loss_pct', config.STOP_LOSS_PCT):.1f}%"
            f" TP={existing_pos.get('take_profit_pct', config.TAKE_PROFIT_PCT):.1f}%"
        )

    dvol = f"{iv.daily_volume_ratio:.2f}x"
    if iv.daily_volume_ratio < 0.5:
        dvol += " LOW"
    elif iv.daily_volume_ratio > 3.0:
        dvol += " HIGH"

    pead_ok, pead_score = detect_pead_setup(symbol)
    pead_line = (
        f"\n  PEAD: {pead_score}/100 conditions met — expect multi-day drift"
        if pead_ok else ""
    )

    return (
        f"=={symbol}== composite={iv.composite_score:+.1f} signal={iv.signal}{pos_block}\n"
        f"  P=${price:.2f} VWAP=${iv.vwap:.2f}({'↑' if price >= iv.vwap else '↓'})"
        f" RSI={iv.rsi:.1f}(Δ{iv.rsi_slope:+.1f}) MACD={iv.macd_hist:.4f}"
        f" BB=[{iv.bb_lower:.2f},{iv.bb_upper:.2f}] EMA9={iv.ema9:.2f} EMA21={iv.ema21:.2f}"
        f" ATR={iv.atr:.4f} Stoch={iv.stoch_k:.1f}/{iv.stoch_d:.1f} Vol={dvol}\n"
        f"  NEWS: {sentiment_label}({sentiment_score:+d}) catalyst={catalyst} risk={risk_flag}\n"
        f"  {news_summary}"
        f"{pead_line}"
    )


def build_market_context_block(
    regime_info: dict,
    fred_cache: dict,
    research_meta: dict,
    now: datetime,
) -> str:
    """
    Assemble market context for the Claude system prompt.
    Includes intraday bias + breadth so Claude has a full picture.
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

    event_active, event_name = _ec_mod.is_event_risk_window(now)
    event_line = f"⚠ ACTIVE: {event_name} — avoid new entries" if event_active else "None active"

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

    from macro.intraday_context import get_intraday_context
    intraday = get_intraday_context(now)
    intraday_lines = intraday.to_context_lines() if intraday else "Intraday bias: unavailable"

    return (
        f"MARKET CONTEXT ({now.strftime('%H:%M')} ET):\n"
        f"Daily regime: {regime} | health={health:+d} | VIX={vix:.1f}{vix_note}\n"
        f"{weekly_line}\n"
        f"{breadth_line}\n"
        f"Sector scores: {sector_score_str}\n"
        f"{intraday_lines}\n"
        f"FRED: Fed {fed} | CPI {cpi} | Unemployment {unemp}\n"
        f"Event risk: {event_line}\n"
        f"Macro shock: {shock_line} | Capitulation watch: {cap_str}"
    )
