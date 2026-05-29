"""
Context-string builders for Claude prompts.
Pure — no I/O, no Alpaca calls; callers supply dicts.
"""
from __future__ import annotations

from datetime import datetime

import config
import macro.economic_calendar as _ec_mod
from indicators.values import IndicatorValues
from signals.tp_scaler import compute_tp_sl
from macro.regime import get_cached_regime
from trading.journal import load_open_positions
from utils.market_monitor import is_flash_crash_active


# Keys this module reads from fred_cache — must be subset of FRED_SCHEMA_KEYS.
# "cpi_trend" is a legacy fallback key that may not be in the schema; excluded intentionally.
FRED_KEYS_CONSUMED: frozenset[str] = frozenset({
    "fed_direction", "cpi_vs_target", "cpi_trend", "unemployment_rising",
    "yield_curve_inverted", "yield_curve_spread",
    "credit_spread_bps", "credit_spread_risk_off",
    "claims_spike",
})


# ── helpers ──────────────────────────────────────────────────────────────────

def _pos_line(iv, existing_pos: dict | None) -> str:
    if not existing_pos:
        return ""
    ep = existing_pos.get("entry_price") or 0.0
    if ep <= 0:
        return ""
    price = iv.current_price
    pnl_pct = (price - ep) / ep * 100
    sl = existing_pos.get("stop_loss_pct", config.STOP_LOSS_PCT)
    tp = existing_pos.get("take_profit_pct", config.TAKE_PROFIT_PCT)
    return (
        f" POS:${ep:.2f}×{existing_pos.get('qty', 0):.0f}"
        f" P&L={pnl_pct:+.2f}% SL={sl:.1f}% TP={tp:.1f}%"
    )


# ── tier-specific private builders ───────────────────────────────────────────

def _block_low(symbol, iv, snap, gate_result, quality, mode, entry_time,
               existing_pos, atr_suggested_tp_pct, intraday_news) -> str:
    if snap.regime == "CRISIS":
        return f'<stock symbol="{symbol}">\n  CRISIS: entries blocked — no evaluation.\n</stock>'
    price = iv.current_price
    vwap_dir = "↑" if price >= iv.vwap else "↓"
    gaps_str = " ".join(quality.quality_gaps) if quality.quality_gaps else "none"
    flags_str = " ".join(gate_result.flags) if gate_result.flags else ""
    pos_block = _pos_line(iv, existing_pos)
    return (
        f'<stock symbol="{symbol}" tier="LOW">\n'
        f"  ⚠ CAUTION: low-quality setup — consider skipping\n"
        f"  quality_gaps: {gaps_str}\n"
        f"  composite={iv.composite_score:+.1f} RSI={iv.rsi:.1f} VWAP{vwap_dir}"
        f" OR={iv.or_position}{pos_block}\n"
        + (f"  flags: {flags_str}\n" if flags_str else "")
        + f"</stock>"
    )


def _block_medium(symbol, iv, snap, gate_result, quality, mode, entry_time,
                  existing_pos, atr_suggested_tp_pct, intraday_news) -> str:
    if snap.regime == "CRISIS":
        return f'<stock symbol="{symbol}">\n  CRISIS: entries blocked — no evaluation.\n</stock>'
    price = iv.current_price
    vwap_dir = "↑" if price >= iv.vwap else "↓"
    research_sym = snap.research.get(symbol, {})
    sentiment_score = research_sym.get("sentiment_score", 0)
    sentiment_label = research_sym.get("sentiment_label", "NEUTRAL")
    risk_flag = research_sym.get("risk_flag") or ""
    catalyst = research_sym.get("key_catalyst") or ""
    news_summary = (research_sym.get("summary") or "").strip()[:120]
    intra = (intraday_news or snap.intraday_news.get(symbol, {}))
    intra_shock = intra.get("shock_score", 0)
    intra_sent = intra.get("sentiment_score", sentiment_score)
    flags_str = " ".join(gate_result.flags) if gate_result.flags else ""
    risk_str = f" risk={risk_flag}" if risk_flag else ""
    cat_str = f' cat="{catalyst}"' if catalyst else ""
    news_str = f"\n  {news_summary}" if news_summary else ""
    atr_line = (f"\n  ATR-TP={atr_suggested_tp_pct:.1f}% (override if catalyst justifies)"
                if atr_suggested_tp_pct is not None else "")
    pos_block = _pos_line(iv, existing_pos)
    return (
        f'<stock symbol="{symbol}" tier="MEDIUM">\n'
        f"  composite={iv.composite_score:+.1f} signal={iv.signal}{pos_block}\n"
        f"  P=${price:.2f} VWAP{vwap_dir}({iv.vwap_slope:+.3f})"
        f" RSI={iv.rsi:.1f} OR={iv.or_position}"
        f" Stoch={iv.stoch_k:.0f}/{iv.stoch_d:.0f}"
        f"{atr_line}\n"
        f"  SENT:{sentiment_label}({sentiment_score:+d})"
        f" intraday:shock={intra_shock} sent={intra_sent:+d}"
        f"{cat_str}{risk_str}"
        + (f"\n  flags: {flags_str}" if flags_str else "")
        + f"{news_str}\n"
        + f"</stock>"
    )


def _block_high(symbol, iv, snap, gate_result, quality, mode, entry_time,
                existing_pos, atr_suggested_tp_pct, intraday_news) -> str:
    if snap.regime == "CRISIS":
        return f'<stock symbol="{symbol}">\n  CRISIS: entries blocked — no evaluation.\n</stock>'
    price = iv.current_price
    vwap_dir = "↑" if price >= iv.vwap else "↓"
    research_sym = snap.research.get(symbol, {})
    sentiment_score = research_sym.get("sentiment_score", 0)
    sentiment_label = research_sym.get("sentiment_label", "NEUTRAL")
    intra = (intraday_news or snap.intraday_news.get(symbol, {}))
    intra_shock = intra.get("shock_score", 0)
    intra_sent = intra.get("sentiment_score", sentiment_score)
    # time-adjusted TP
    base_tp = atr_suggested_tp_pct or config.TAKE_PROFIT_PCT
    adj_tp, _ = compute_tp_sl(base_tp, config.STOP_LOSS_PCT, entry_time, quality.tier, snap.regime, mode=mode)
    # sub-sector ETF
    try:
        from sectors import get_sub_sector_etf
        sub_etf = get_sub_sector_etf(symbol)
    except Exception:
        sub_etf = ""
    sub_etf_str = f" sub_etf={sub_etf}" if sub_etf else ""
    # swing/T2 unlock hints
    swing_ok = "SWING_SENTIMENT" in quality.unlocks or "SWING_CHOPPY" in quality.unlocks
    t2_ok = "T2_CHOPPY" in quality.unlocks
    eligibility = []
    if swing_ok:
        eligibility.append("swing=eligible")
    if t2_ok:
        eligibility.append("T2=eligible")
    elig_str = " ".join(eligibility)
    flags_str = " ".join(gate_result.flags) if gate_result.flags else ""
    pos_block = _pos_line(iv, existing_pos)
    return (
        f'<stock symbol="{symbol}" tier="HIGH">\n'
        f"  composite={iv.composite_score:+.1f} signal={iv.signal}{pos_block}\n"
        f"  P=${price:.2f} VWAP{vwap_dir}({iv.vwap_slope:+.3f})"
        f" RSI={iv.rsi:.1f} OR={iv.or_position}{sub_etf_str}\n"
        f"  adj_tp={adj_tp:.1f}% SENT:{sentiment_label}({sentiment_score:+d})"
        f" intraday:shock={intra_shock} sent={intra_sent:+d}"
        + (f" {elig_str}" if elig_str else "")
        + (f"\n  flags: {flags_str}" if flags_str else "")
        + f"\n</stock>"
    )


def _block_conviction(symbol, iv, snap, gate_result, quality, mode, entry_time,
                      existing_pos, atr_suggested_tp_pct, intraday_news) -> str:
    if snap.regime == "CRISIS":
        return f'<stock symbol="{symbol}">\n  CRISIS: entries blocked — no evaluation.\n</stock>'
    price = iv.current_price
    base_tp = atr_suggested_tp_pct or config.TAKE_PROFIT_PCT
    adj_tp, _ = compute_tp_sl(base_tp, config.STOP_LOSS_PCT, entry_time, quality.tier, snap.regime, mode=mode)
    # sub-sector ETF
    try:
        from sectors import get_sub_sector_etf
        sub_etf = get_sub_sector_etf(symbol)
    except Exception:
        sub_etf = ""
    sub_etf_str = f" sub_etf={sub_etf}" if sub_etf else ""
    # unlocks
    unlocks_str = ",".join(sorted(quality.unlocks)) if quality.unlocks else "none"
    # pead — derived from unlocks to preserve module purity (no disk I/O)
    pead_ok = "PEAD_EXTENDED_WINDOW" in quality.unlocks
    pead_score = quality.component_scores.get("pead", 0)
    pead_str = f" PEAD={pead_score}/100" if pead_ok else ""
    # regime annotation
    regime_note = ""
    if snap.regime == "CAPITULATION":
        pead_line = "PEAD=REQUIRED(unlock)" if "PEAD_EXTENDED_WINDOW" in quality.unlocks else "PEAD=MISSING⚠"
        regime_note = f"\n  ⚠ CAPITULATION: entries restricted — CONVICTION+PEAD only. {pead_line}"
    elif snap.regime == "CHOPPY":
        regime_note = "\n  CHOPPY: auto-exec gated at +15 offset. T2/swing via unlock only."
    pos_block = _pos_line(iv, existing_pos)
    return (
        f'<stock symbol="{symbol}" tier="CONVICTION" mode="{mode}">\n'
        f"  composite={iv.composite_score:+.1f} signal={iv.signal}{pos_block}\n"
        f"  P=${price:.2f} RSI={iv.rsi:.1f}{sub_etf_str}{pead_str}\n"
        f"  unlocks={unlocks_str} adj_tp={adj_tp:.1f}%"
        f"{regime_note}\n"
        f"</stock>"
    )


# ── public API ────────────────────────────────────────────────────────────────

def build_stock_block(
    symbol: str,
    iv,                            # IndicatorValues
    snap,                          # CycleSnapshot
    gate_result,                   # GateResult
    quality,                       # TickerQuality
    mode: str,                     # "DAY_TRADE" | "PEAD" | "SWING"
    entry_time,                    # datetime
    existing_pos: dict | None = None,
    atr_suggested_tp_pct: float | None = None,
    intraday_news: dict | None = None,
) -> str:
    """Compact per-symbol block sent to Claude, dispatched by tier."""
    _dispatch = {
        "LOW":        _block_low,
        "MEDIUM":     _block_medium,
        "HIGH":       _block_high,
        "CONVICTION": _block_conviction,
    }
    builder = _dispatch.get(quality.tier, _block_medium)
    return builder(symbol, iv, snap, gate_result, quality, mode, entry_time,
                   existing_pos, atr_suggested_tp_pct, intraday_news)


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
    entry_ts = position.get("entry_time", "")
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

    mode_str = position.get("mode_at_entry", "DAY_TRADE")
    return (
        f'<stock symbol="{symbol}" mode="{mode_str}">\n'
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
    cpi   = fred_cache.get("cpi_vs_target") or fred_cache.get("cpi_trend", "unknown")
    unemp = "rising" if fred_cache.get("unemployment_rising") else "stable"

    yield_inv    = fred_cache.get("yield_curve_inverted", False)
    yield_spread = fred_cache.get("yield_curve_spread")
    credit_bps   = fred_cache.get("credit_spread_bps")
    credit_ro    = fred_cache.get("credit_spread_risk_off", False)
    claims_spike = fred_cache.get("claims_spike", False)
    yield_str  = "INVERTED" if yield_inv else (f"{yield_spread:.0f}bps" if yield_spread is not None else "unknown")
    credit_str = "RISK-OFF" if credit_ro else (f"{credit_bps:.0f}bps" if credit_bps is not None else "unknown")
    claims_str = "spiking ⚠" if claims_spike else "normal"

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
        f"Yield curve: {yield_str} | Credit spread: {credit_str} | Claims: {claims_str}\n"
        f"Macro shock: {shock_line} | Capitulation watch: {cap_str}"
    )


def build_dynamic_user_context(now: datetime, portfolio_gain_pct: float = 0.0) -> str:
    """
    Assemble the dynamic (per-call) market snapshot for the Claude user message.
    Contains live intraday bias, event risk, current time, and today's P&L.
    Must NOT be placed in the system block — changes every few minutes.
    """
    from macro.intraday_context import get_intraday_context
    intraday = get_intraday_context(now)
    intraday_lines = intraday.to_context_lines() if intraday else "Intraday bias: unavailable"

    event_active, event_name = _ec_mod.is_event_risk_window(now)
    event_line = f"⚠ ACTIVE: {event_name} — avoid new entries" if event_active else "None active"

    pnl_note = f"{portfolio_gain_pct:+.2f}%"
    if portfolio_gain_pct <= -config.DAILY_LOSS_WARN_PCT * 100:
        pnl_note += " ⚠ elevated daily loss — tighten criteria"

    # Portfolio sector concentration
    from sectors import get_sector_etf
    open_pos  = load_open_positions()
    held      = [get_sector_etf(s) for s in open_pos]
    conc_line = (
        "Positions: " + ", ".join(
            f"{s}×{held.count(s)}" for s in sorted(set(held))
        ) if held else "Positions: none"
    )

    # Entry window countdown
    regime       = get_cached_regime()
    from utils.regime_params import get_entry_params
    hard_close_h = get_entry_params(regime).hard_close_hour
    remaining_m  = max(0, hard_close_h * 60 - now.hour * 60 - now.minute)
    window_line  = f"Entry window: {remaining_m // 60}h{remaining_m % 60:02d}m remaining"

    # Flash-crash status
    flash_line = (
        "Flash crash: ACTIVE — entries blocked"
        if is_flash_crash_active()
        else "Flash crash: clear"
    )

    # Concurrency state — computed from open_pos and regime already in scope
    from utils.regime_params import get_concurrency_params
    _intraday_positions = [p for p in open_pos.values() if not p.get("is_swing")]
    _open_count = len(_intraday_positions)
    _cp_ctx = get_concurrency_params(regime, "MEDIUM", "DAY_TRADE")
    _available = max(0, _cp_ctx.max_open - _open_count)
    _ceiling_pct = config.REGIME_HEAT_CEILING.get(regime, 0.20) * 100
    _concurrency_line = (
        f"Open intraday positions: {_open_count} "
        f"| Available slots (MEDIUM tier): {_available} "
        f"| Heat ceiling: {_ceiling_pct:.0f}% (regime={regime})"
    )

    return (
        f"MARKET SNAPSHOT ({now.strftime('%H:%M')} ET):\n"
        f"{intraday_lines}\n"
        f"Portfolio P&L today: {pnl_note}\n"
        f"Event risk: {event_line}\n"
        f"{conc_line}\n"
        f"{window_line}\n"
        f"{flash_line}\n"
        f"{_concurrency_line}"
    )
