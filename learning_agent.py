import json
import logging
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

import anthropic

import config
from macro_regime import load_macro_regime, load_trading_params
from utils.io_utils import load_json, save_json

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


_TRADE_SUMMARY_KEYS = {
    "symbol", "timestamp", "pnl_pct", "pnl_dollar", "close_reason",
    "entry_composite_score", "exit_composite_score",
    "indicator_signals_at_entry", "claude_reasoning", "entry_sentiment_score",
    "mode_at_entry", "regime_at_entry",
}

# Hard bounds on learnable thresholds — prevents Claude from disabling stops or
# removing entire regimes.  Values come from the Plan 3 spec §18.
_THRESHOLD_BOUNDS: dict[str, tuple] = {
    "choppy_composite_gate":          (25,   40),
    "bull_trend_rsi_max_tier1":       (76,   88),
    "swing_max_days":                 (3,    7),
    "capitulation_drawdown_threshold":(15,   30),
    "squeeze_confidence_gate":        (60,   80),
    "herd_size_reduction":            (0.15, 0.50),
    "stop_loss_pct":                  (1.5,  12.0),
    "take_profit_pct":                (2.0,  25.0),
    "rsi_buy_max":                    (30,   80),
    "rsi_buy_max_high_vol":           (25,   70),
    "vwap_slope_min":                 (-2.0, 0.0),
    "high_vol_position_mult":         (0.20, 0.75),
}


def _clamp_threshold(key: str, value) -> float | int:
    """Clamp `value` to the hard bounds for `key`.  Returns value unchanged if key unknown."""
    if key not in _THRESHOLD_BOUNDS:
        return value
    lo, hi = _THRESHOLD_BOUNDS[key]
    try:
        v = float(value)
        clamped = max(lo, min(hi, v))
        return int(clamped) if isinstance(lo, int) else clamped
    except (TypeError, ValueError):
        return value


def _compute_regime_winrate(closed_trades: list) -> dict[str, float]:
    """
    Returns win-rate (0.0–1.0) per regime for closed trades.
    Only CLOSE entries with a 'regime_at_entry' field are counted.
    """
    regime_totals: dict[str, int] = {}
    regime_wins: dict[str, int] = {}

    for trade in closed_trades:
        if trade.get("action") != "CLOSE":
            continue
        regime = trade.get("regime_at_entry")
        if not regime:
            continue
        regime_totals[regime] = regime_totals.get(regime, 0) + 1
        if float(trade.get("pnl_pct", 0)) > 0:
            regime_wins[regime] = regime_wins.get(regime, 0) + 1

    return {
        regime: regime_wins.get(regime, 0) / total
        for regime, total in regime_totals.items()
    }


def _aggregate_by_regime(closed_trades: list[dict]) -> dict:
    """
    Group today's closed trades by regime_at_entry and mode_at_entry.
    Returns a dict with per-regime and per-mode performance summaries.
    """
    by_regime: dict[str, list[dict]] = {}
    by_mode:   dict[str, list[dict]] = {}
    for t in closed_trades:
        regime = t.get("regime_at_entry") or "UNKNOWN"
        mode   = t.get("mode_at_entry")   or "UNKNOWN"
        by_regime.setdefault(regime, []).append(t)
        by_mode.setdefault(mode,   []).append(t)

    def _summarise(trades: list[dict]) -> dict:
        winners = [t for t in trades if (t.get("pnl_pct") or 0) > 0]
        losers  = [t for t in trades if (t.get("pnl_pct") or 0) <= 0]
        total_pnl = sum(t.get("pnl_dollar") or 0 for t in trades)
        avg_pnl   = sum(t.get("pnl_pct") or 0 for t in trades) / len(trades) if trades else 0
        return {
            "count":           len(trades),
            "winners":         len(winners),
            "losers":          len(losers),
            "win_rate_pct":    round(len(winners) / len(trades) * 100, 1) if trades else 0,
            "avg_pnl_pct":     round(avg_pnl, 2),
            "total_pnl_dollar":round(total_pnl, 2),
        }

    return {
        "by_regime": {k: _summarise(v) for k, v in by_regime.items()},
        "by_mode":   {k: _summarise(v) for k, v in by_mode.items()},
    }


def _classify_loss_source(trade: dict) -> str:
    """
    Classify the root cause of a losing trade.  Priority order matches spec §19.
    Returns one of: earnings_surprise | market_structure | momentum_exhaustion |
                    fundamental | macro_pressure | sector_narrative | unknown
    """
    close_reason  = (trade.get("close_reason") or "").lower()
    entry_ind     = trade.get("entry_indicators") or {}
    entry_score   = trade.get("entry_composite_score") or 0

    # Earnings surprise: earnings-driven close reason
    if any(kw in close_reason for kw in ("earnings", "pead")):
        return "earnings_surprise"

    # Market structure: broker forced close or extreme regime
    regime = (trade.get("regime_at_entry") or "").upper()
    if regime in ("CRISIS", "CRASH_RECOVERY") or "flash_crash" in close_reason:
        return "market_structure"

    # Momentum exhaustion flagged at entry
    if entry_ind.get("momentum_exhaustion"):
        return "momentum_exhaustion"

    # Fundamental news driver
    if any(kw in close_reason for kw in ("fundamental", "guidance", "downgrade")):
        return "fundamental"

    # Macro pressure: high shock score at entry
    if (entry_ind.get("entry_macro_shock_score") or 0) >= 60:
        return "macro_pressure"

    # Sector narrative: negative sentiment at entry
    if (trade.get("entry_sentiment_score") or 0) <= -25:
        return "sector_narrative"

    return "unknown"


def _load_todays_closed_trades() -> list[dict]:
    if not config.TRADE_JOURNAL_FILE.exists():
        return []
    journal = load_json(config.TRADE_JOURNAL_FILE, [])
    today = date.today().isoformat()
    # Strip full indicator snapshots — learning agent only needs summary fields
    return [
        {k: v for k, v in t.items() if k in _TRADE_SUMMARY_KEYS}
        for t in journal
        if t.get("type") == "CLOSE" and t.get("timestamp", "").startswith(today)
    ]


def _load_weights() -> dict:
    return load_json(config.INDICATOR_WEIGHTS_FILE, {})


def _save_weights(weights: dict):
    save_json(config.INDICATOR_WEIGHTS_FILE, weights)


def _save_trading_params(params: dict):
    params["last_updated"] = date.today().isoformat()
    save_json(config.TRADING_PARAMS_FILE, params)


def run_learning_agent():
    today = date.today().isoformat()
    closed_trades = _load_todays_closed_trades()

    if not closed_trades:
        logger.info("No closed trades today — nothing to learn from.")
        return

    current_weights = _load_weights()
    current_params  = load_trading_params()
    regime_info     = load_macro_regime()
    ai = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    # Pre-compute regime/mode aggregation and loss classification (free — no Claude tokens)
    regime_agg = _aggregate_by_regime(closed_trades)
    regime_winrate = _compute_regime_winrate(closed_trades)
    loss_sources = {
        t["symbol"]: _classify_loss_source(t)
        for t in closed_trades
        if (t.get("pnl_pct") or 0) < 0
    }

    # ── Step 1: Performance + regime analysis ────────────────────────────────
    analysis_response = ai.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=2000,
        system=[
            {
                "type": "text",
                "text": (
                    "You are a quantitative trading analyst reviewing today's closed trades.\n"
                    "Your job is two-fold:\n"
                    "1. Analyse which indicators were reliable and suggest weight adjustments.\n"
                    "2. Evaluate whether the trading parameters (entry window, RSI thresholds,\n"
                    "   VWAP slope filter, position size) should be tuned based on today's regime.\n\n"
                    "Weight adjustment rules:\n"
                    "- Max ±5 per indicator per day\n"
                    "- Adjustments must keep total weights summing to 100\n"
                    "- Only adjust based on evidence from today's trades\n\n"
                    "Trading params adjustment rules:\n"
                    "- entry_start_hour: 9–11 (integer)\n"
                    "- entry_end_hour: 13–15 (integer)\n"
                    "- rsi_buy_max: 30–50 (normal regime)\n"
                    "- rsi_buy_max_high_vol: 25–40 (high vol regime)\n"
                    "- vwap_slope_min: -1.0 to 0.0 (negative = more permissive)\n"
                    "- high_vol_position_mult: 0.25–0.75\n"
                    "- Only suggest param changes if you have strong evidence from today.\n"
                    "  If uncertain, keep the current value.\n\n"
                    "Respond in strict JSON."
                ),
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {
                "role": "user",
                "content": (
                    f"Date: {today}\n"
                    f"VIX regime today: {json.dumps(regime_info, indent=2)}\n\n"
                    f"Performance by regime/mode:\n{json.dumps(regime_agg, indent=2)}\n\n"
                    f"Regime win rates:\n{json.dumps(regime_winrate, indent=2)}\n\n"
                    f"Loss source classification:\n{json.dumps(loss_sources, indent=2)}\n\n"
                    f"Closed trades:\n{json.dumps(closed_trades, indent=2)}\n\n"
                    f"Current indicator weights:\n{json.dumps(current_weights, indent=2)}\n\n"
                    f"Current trading params:\n{json.dumps(current_params, indent=2)}\n\n"
                    "Return JSON:\n"
                    "{\n"
                    '  "total_trades": <int>,\n'
                    '  "winning_trades": <int>,\n'
                    '  "losing_trades": <int>,\n'
                    '  "total_pnl_dollar": <float>,\n'
                    '  "win_rate_pct": <float>,\n'
                    '  "market_conditions": "<trending|ranging|high_vol|low_vol|mixed>",\n'
                    '  "regime_impact": "<how did the VIX regime affect performance today>",\n'
                    '  "indicator_performance": {\n'
                    '    "<indicator_name>": {\n'
                    '      "accuracy_pct": <0-100>,\n'
                    '      "weight_adjustment": <-5 to 5>,\n'
                    '      "notes": "<one sentence>"\n'
                    '    }\n'
                    "  },\n"
                    '  "suggested_params": {\n'
                    '    "entry_start_hour": <int>,\n'
                    '    "entry_end_hour": <int>,\n'
                    '    "rsi_buy_max": <int>,\n'
                    '    "rsi_buy_max_high_vol": <int>,\n'
                    '    "vwap_slope_min": <float>,\n'
                    '    "high_vol_position_mult": <float>,\n'
                    '    "stop_loss_pct": <float>,\n'
                    '    "take_profit_pct": <float>\n'
                    "  },\n"
                    '  "strategy_learnings": ["<lesson1>", "..."],\n'
                    '  "per_stock_notes": {"<SYMBOL>": "<observation>"}\n'
                    "}"
                ),
            }
        ],
    )

    text = analysis_response.content[0].text.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        logger.error("Could not parse learning agent analysis.")
        return

    analysis = json.loads(match.group())

    # ── Step 2: Update indicator weights ────────────────────────────────────
    new_weights = dict(current_weights)
    for indicator, data in analysis.get("indicator_performance", {}).items():
        if indicator in new_weights:
            adj = float(data.get("weight_adjustment", 0))
            new_weights[indicator] = max(1.0, new_weights[indicator] + adj)

    total = sum(new_weights.values())
    if total > 0:
        new_weights = {k: round(v / total * 100, 2) for k, v in new_weights.items()}

    _save_weights(new_weights)
    logger.info(f"Weights updated: {new_weights}")

    # ── Step 3: Update trading params (with hard-bound clamping) ────────────
    suggested = analysis.get("suggested_params", {})
    new_params = dict(current_params)
    param_keys = [
        "entry_start_hour", "entry_end_hour", "rsi_buy_max", "rsi_buy_max_high_vol",
        "vwap_slope_min", "high_vol_position_mult", "stop_loss_pct", "take_profit_pct",
    ]
    changed_params = []
    for key in param_keys:
        if key in suggested and suggested[key] != current_params.get(key):
            old_val = current_params.get(key)
            new_val = _clamp_threshold(key, suggested[key])  # hard bounds prevent extreme values
            if new_val != old_val:
                new_params[key] = new_val
                changed_params.append(f"{key}: {old_val} → {new_val}")

    _save_trading_params(new_params)
    if changed_params:
        logger.info(f"Trading params updated: {', '.join(changed_params)}")
    else:
        logger.info("Trading params unchanged today.")

    # ── Step 4: Append to performance log ───────────────────────────────────
    log_lines = [
        f"\n## {today}\n",
        f"**Regime:** {regime_info.get('regime', 'NORMAL')} | "
        f"VIX={regime_info.get('vix_current', 0):.1f} (90d avg={regime_info.get('vix_avg_90d', 0):.1f}) | "
        f"SPY={regime_info.get('spy_change_pct', 0):+.2f}%",
        f"**Trades:** {analysis.get('total_trades', 0)} total | "
        f"W:{analysis.get('winning_trades', 0)} L:{analysis.get('losing_trades', 0)} | "
        f"Win rate: {analysis.get('win_rate_pct', 0):.1f}% | "
        f"P&L: ${analysis.get('total_pnl_dollar', 0):.2f}",
        f"**Market conditions:** {analysis.get('market_conditions', 'unknown')} | "
        f"Regime impact: {analysis.get('regime_impact', 'n/a')}\n",
        "### Indicator Performance\n",
    ]
    for indicator, data in analysis.get("indicator_performance", {}).items():
        old_w = current_weights.get(indicator, 0)
        new_w = new_weights.get(indicator, 0)
        adj   = data.get("weight_adjustment", 0)
        adj_str = f"+{adj}" if adj > 0 else str(adj)
        log_lines.append(
            f"- **{indicator}**: accuracy={data.get('accuracy_pct', 0):.0f}% | "
            f"weight {old_w}% → {new_w}% ({adj_str}%) | {data.get('notes', '')}"
        )

    if changed_params:
        log_lines.append("\n### Param Changes\n")
        for c in changed_params:
            log_lines.append(f"- {c}")

    log_lines.append("\n### Strategy Learnings\n")
    for lesson in analysis.get("strategy_learnings", []):
        log_lines.append(f"- {lesson}")

    log_lines.append("\n### Per-Stock Notes\n")
    for sym, note in analysis.get("per_stock_notes", {}).items():
        log_lines.append(f"- **{sym}**: {note}")

    log_lines.append("\n---")
    entry = "\n".join(log_lines) + "\n"
    existing = (
        config.INDICATOR_PERFORMANCE_LOG.read_text()
        if config.INDICATOR_PERFORMANCE_LOG.exists()
        else "# Indicator Performance Log\n"
    )
    config.INDICATOR_PERFORMANCE_LOG.write_text(existing + entry)

    # ── Step 5: Update strategy memory ──────────────────────────────────────
    current_memory = config.STRATEGY_MEMORY_FILE.read_text() if config.STRATEGY_MEMORY_FILE.exists() else ""
    # Cap memory sent to Claude — it grows daily and old entries are stale
    if len(current_memory) > config.STRATEGY_MEMORY_MAX_CHARS:
        current_memory = current_memory[-config.STRATEGY_MEMORY_MAX_CHARS:]

    memory_response = ai.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=1500,
        system=[
            {
                "type": "text",
                "text": (
                    "You maintain a living trading strategy memory document.\n"
                    "Update it with today's learnings. Keep it concise, data-backed, and actionable.\n"
                    "Target length: under 1000 words. Summarise older entries if needed.\n\n"
                    "Required sections:\n"
                    "## General Rules\n"
                    "## Market Regime Playbook (NORMAL / HIGH_VOL / CRISIS)\n"
                    "## Entry Filter Rules (time-of-day, RSI, VWAP slope)\n"
                    "## Per-Stock Notes\n"
                    "## Weight Rationale\n"
                    "## Recent Lessons (last 5 trading days)\n\n"
                    "Preserve valuable prior learnings. Remove stale or contradicted entries. "
                    "Use numbers and conditions, not vague prose."
                ),
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {
                "role": "user",
                "content": (
                    f"Today's analysis ({today}):\n{json.dumps(analysis, indent=2)}\n\n"
                    f"Updated weights:\n{json.dumps(new_weights, indent=2)}\n\n"
                    f"Updated params:\n{json.dumps(new_params, indent=2)}\n\n"
                    f"VIX regime: {json.dumps(regime_info)}\n\n"
                    f"Current memory:\n{current_memory or 'No prior memory.'}\n\n"
                    "Write the complete updated strategy_memory.md. Keep it under 1000 words."
                ),
            }
        ],
    )

    config.STRATEGY_MEMORY_FILE.write_text(memory_response.content[0].text.strip())
    logger.info(
        f"Learning agent complete. "
        f"W:{analysis.get('winning_trades')}/L:{analysis.get('losing_trades')}, "
        f"P&L=${analysis.get('total_pnl_dollar', 0):.2f} | "
        f"Regime={regime_info.get('regime')} | "
        f"Params changed: {len(changed_params)}"
    )


if __name__ == "__main__":
    import logging as _l
    _l.basicConfig(level=_l.INFO)
    run_learning_agent()
