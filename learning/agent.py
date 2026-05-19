"""
Learning agent orchestrator — runs post-market to update indicator weights,
trading params, and strategy memory based on today's closed trades.
"""
import json
import logging
import re
from datetime import datetime
import sys

import anthropic

import config
from trading import risk_manager
from macro.regime import load_macro_regime, load_trading_params
from learning.trade_analysis import (
    _aggregate_by_regime,
    _classify_loss_source,
    _compute_regime_winrate,
    _clamp_threshold,
    _tune_atr_tp_multiplier,
)
from learning.data import (
    _load_todays_closed_trades,
    _load_rolling_trades,
    _load_weights,
    _save_weights,
    _save_trading_params,
)

logger = logging.getLogger(__name__)


def _write_strategy_memory(content: str) -> None:
    """Write strategy_memory.md and create a dated backup. Prune old backups."""
    from pathlib import Path
    from utils.constants import ET
    mem_path = Path(str(config.STRATEGY_MEMORY_FILE))
    parent = mem_path.parent
    today = datetime.now(ET).strftime("%Y-%m-%d")

    if mem_path.exists():
        bak_path = parent / f"strategy_memory.{today}.bak.md"
        bak_path.write_text(mem_path.read_text())

    mem_path.write_text(content[:config.STRATEGY_MEMORY_MAX_CHARS])

    bak_files = sorted(parent.glob("strategy_memory.*.bak.md"))
    while len(bak_files) > config.STRATEGY_MEMORY_BACKUP_MAX:
        bak_files[0].unlink()
        bak_files = bak_files[1:]

_ZERO_TRADE_BOUNDS = {
    "rsi_buy_max":          (65, 80),
    "composite_min_normal": (15, 30),
    "vwap_slope_min":       (-0.6, -0.1),
}


def _handle_zero_trade_day(params_path) -> None:
    """Deterministic single-param relaxation on zero-trade days. No Claude call."""
    from utils.io_utils import load_json, save_json
    from datetime import datetime
    from utils.constants import ET
    params = load_json(params_path, {})
    weekday = datetime.now(ET).weekday()
    param_order = ["rsi_buy_max", "composite_min_normal", "vwap_slope_min"]
    target = param_order[weekday % len(param_order)]
    low, high = _ZERO_TRADE_BOUNDS.get(target, (None, None))
    if low is None:
        return
    current = float(params.get(target, high))
    if target == "vwap_slope_min":
        new_val = max(low, current - 0.05)
    else:
        new_val = min(high, current + config.LEARNING_ZERO_TRADE_RSI_STEP)
    if new_val != current:
        params[target] = round(new_val, 2)
        save_json(params_path, params)
        logger.info("Zero-trade-day: relaxed %s %s → %s", target, current, new_val)


def run_learning_agent():
    from utils.constants import ET
    # Skip learning agent on weekends, unless we are running tests
    if datetime.now(ET).weekday() >= 5 and "pytest" not in sys.modules:
        logger.info("Learning agent skipped — weekend (weekday=%d).", datetime.now(ET).weekday())
        return

    today = datetime.now(ET).date().isoformat()
    closed_trades = _load_todays_closed_trades()

    if not closed_trades:
        logger.info("No closed trades today — running deterministic param relaxation.")
        _handle_zero_trade_day(config.TRADING_PARAMS_FILE)
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
                    "- rsi_buy_max: 40–80 (normal regime; current value 65 is the baseline)\n"
                    "- rsi_buy_max_high_vol: 35–65 (high vol regime; tighten only with evidence)\n"
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
                    f"<regime>{json.dumps(regime_info)}</regime>\n\n"
                    f"<performance_by_regime>{json.dumps(regime_agg)}</performance_by_regime>\n\n"
                    f"<regime_winrates>{json.dumps(regime_winrate)}</regime_winrates>\n\n"
                    f"<loss_sources>{json.dumps(loss_sources)}</loss_sources>\n\n"
                    f"<trades>{json.dumps(closed_trades)}</trades>\n\n"
                    f"<weights>{json.dumps(current_weights)}</weights>\n\n"
                    f"<params>{json.dumps(current_params)}</params>\n\n"
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

    usage = analysis_response.usage
    risk_manager.record_usage(
        input_tokens=getattr(usage, "input_tokens", 0),
        output_tokens=getattr(usage, "output_tokens", 0),
        cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0),
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0),
        kind="other",
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

    # Clamp each normalized weight to [MIN_MULT, MAX_MULT] × equal share
    equal_share = 100.0 / len(new_weights) if new_weights else 10.0
    lo = equal_share * config.INDICATOR_WEIGHT_MIN_MULT
    hi = equal_share * config.INDICATOR_WEIGHT_MAX_MULT
    for key in new_weights:
        raw = new_weights[key]
        clamped = max(lo, min(hi, raw))
        if abs(clamped - raw) > 0.01:
            logger.warning("Weight %s clamped: %.2f → %.2f (bounds: %.2f–%.2f)", key, raw, clamped, lo, hi)
        new_weights[key] = round(clamped, 4)

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

    # ── Phase 3: ATR-TP multiplier auto-tuning ──────────────────────────────
    rolling_trades = _load_rolling_trades()
    best_mult = _tune_atr_tp_multiplier(rolling_trades)
    if best_mult is not None:
        new_params["atr_tp_multiplier"] = best_mult
        logger.info("ATR-TP multiplier tuned to %.2f×", best_mult)
    else:
        logger.info(
            "ATR-TP multiplier unchanged — insufficient annotated trades (%d < 15).",
            len([t for t in rolling_trades if t.get("atr_pct")]),
        )

    _save_trading_params(new_params)
    if changed_params:
        logger.info(f"Trading params updated: {', '.join(changed_params)}")
    else:
        logger.info("Trading params unchanged today.")

    # ── Step 4: Append to performance log ───────────────────────────────────
    log_lines = [
        f"\n## {today}\n",
        f"**Regime:** {regime_info.get('regime', 'NORMAL')} | "
        f"VIX={regime_info.get('vix_current', 0):.1f} (90d avg={regime_info.get('vix_90d_avg', 0):.1f}) | "
        f"SPY={regime_info.get('spy_5day_return', 0):+.2f}%",
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
                    f"Today's analysis ({today}):\n<analysis>{json.dumps(analysis)}</analysis>\n\n"
                    f"<updated_weights>{json.dumps(new_weights)}</updated_weights>\n\n"
                    f"<updated_params>{json.dumps(new_params)}</updated_params>\n\n"
                    f"<regime>{json.dumps(regime_info)}</regime>\n\n"
                    f"<current_memory>\n{current_memory or 'No prior memory.'}\n</current_memory>\n\n"
                    "Write the complete updated strategy_memory.md. Keep it under 1000 words."
                ),
            }
        ],
    )

    usage = memory_response.usage
    risk_manager.record_usage(
        input_tokens=getattr(usage, "input_tokens", 0),
        output_tokens=getattr(usage, "output_tokens", 0),
        cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0),
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0),
        kind="other",
    )

    _write_strategy_memory(memory_response.content[0].text.strip())
    logger.info(
        f"Learning agent complete. "
        f"W:{analysis.get('winning_trades')}/L:{analysis.get('losing_trades')}, "
        f"P&L=${analysis.get('total_pnl_dollar', 0):.2f} | "
        f"Regime={regime_info.get('regime')} | "
        f"Params changed: {len(changed_params)}"
    )
