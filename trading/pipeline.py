"""
TradingPipeline: four-phase per-cycle orchestrator.

Phase 1 — Parallel data fetch
Phase 2 — Gate checks (check_all_gates per symbol, no disk I/O)
Phase 3 — Claude batch (single API call for all passed symbols)
Phase 4 — Execute BUY / CLOSE decisions
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic

import config
from alpaca_client import AlpacaClient
from indicator_engine import IndicatorValues, compute_indicators
from utils.io_utils import load_json
from trading.journal import (
    load_daily_closed,
    load_open_positions,
)
from trading.claude_caller import _ask_claude_batch, _ask_claude_exit_batch
from trading import risk_manager
from trading.closer import close_position
from trading.executor import execute_buy

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


@dataclass
class CycleResult:
    entries: int = 0
    closes: int = 0
    skipped: bool = False
    reason: str = ""


def _compute_symbol_5day_return(daily_df) -> float:
    """5-day return from daily OHLCV. Returns 0.0 if insufficient data."""
    if daily_df is None or len(daily_df) < 6:
        return 0.0
    try:
        closes = daily_df["close"].values
        return float((closes[-1] - closes[-6]) / closes[-6] * 100)
    except Exception:
        return 0.0


class TradingPipeline:
    def run(
        self,
        symbols: list[str],
        research: dict,
        strategy_memory: str,
        weights: dict,
        ai: anthropic.Anthropic,
        now: datetime,
        regime_info: dict,
        fred_cache: dict,
        alpaca: AlpacaClient,
        portfolio_value: float,
        buying_power: float,
        params: dict,
        can_open: bool,
    ) -> CycleResult:
        t0 = time.monotonic()
        result = CycleResult()

        # Research staleness check
        research_meta = research.get("__meta__", {})
        ts_str = research_meta.get("timestamp")
        if ts_str:
            try:
                report_dt = datetime.fromisoformat(ts_str)
                if report_dt.tzinfo is None:
                    report_dt = report_dt.replace(tzinfo=ET)
                age_min = (datetime.now(ET) - report_dt).total_seconds() / 60
                if age_min > config.RESEARCH_STALE_BLOCK_MIN:
                    logger.error(
                        "Research report is %.0f min old (limit=%d) — skipping cycle.",
                        age_min, config.RESEARCH_STALE_BLOCK_MIN,
                    )
                    return CycleResult(skipped=True, reason=f"research stale ({age_min:.0f}m)")
                if age_min > config.RESEARCH_STALE_WARN_MIN:
                    logger.warning("Research report is %.0f min old — sentiment may be stale.", age_min)
            except Exception:
                pass

        regime    = regime_info.get("regime", "NORMAL")
        spy_ret5d = regime_info.get("spy_5day_return", 0.0)
        rsi_max   = (
            params["rsi_buy_max_high_vol"] if regime in ("CRISIS", "DEFENSIVE")
            else params["rsi_buy_max"]
        )
        pos_mult  = params["high_vol_position_mult"] if regime in ("DEFENSIVE", "CHOPPY") else 1.0
        open_positions = load_open_positions()
        daily_closed   = load_daily_closed()

        # ── Phase 1: Parallel data fetch ──────────────────────────────────────
        t_fetch = time.monotonic()

        def _fetch(symbol: str):
            bars       = alpaca.get_intraday_bars(symbol)
            daily_bars = alpaca.get_daily_bars(symbol, days=8)
            iv = compute_indicators(symbol, bars, daily_df=daily_bars)
            if iv is None:
                return None
            ret5d = _compute_symbol_5day_return(daily_bars)
            return (symbol, iv, open_positions.get(symbol), ret5d)

        stock_data: list[tuple[str, IndicatorValues, dict | None, float]] = []
        with ThreadPoolExecutor(max_workers=config.FETCH_WORKERS) as executor:
            futures = {executor.submit(_fetch, sym): sym for sym in symbols}
            for future in as_completed(futures):
                sym = futures[future]
                try:
                    res = future.result()
                    if res is not None:
                        stock_data.append(res)
                except Exception as exc:
                    logger.error("Error fetching data for %s: %s", sym, exc, exc_info=True)

        fetch_elapsed = time.monotonic() - t_fetch
        if not stock_data:
            logger.warning("No valid indicator data — skipping cycle.")
            return result

        # ── Phase 2: Gate checks ──────────────────────────────────────────────
        t_gates = time.monotonic()
        needs_claude: list[tuple[str, IndicatorValues, dict | None]] = []

        for symbol, iv, existing_pos, symbol_ret5d in stock_data:
            if existing_pos:
                pnl_pct = (iv.current_price - existing_pos["entry_price"]) / existing_pos["entry_price"] * 100
                logger.info(
                    "BRACKET-MANAGED %s: P&L=%+.2f%%, composite=%+.1f — Alpaca bracket active, skipping Claude",
                    symbol, pnl_pct, iv.composite_score,
                )
            elif iv.composite_score > config.COMPOSITE_SCORE_THRESHOLD:
                needs_claude.append((symbol, iv, None))
            else:
                logger.info(
                    "SKIP-CLAUDE %s: composite=%+.1f — below entry threshold",
                    symbol, iv.composite_score,
                )

        gates_elapsed = time.monotonic() - t_gates

        # ── Phase 2.5: Thesis-break detection ────────────────────────────────
        exit_candidates: list[tuple[str, IndicatorValues, dict]] = []
        for symbol, iv, existing_pos, _ret5d in stock_data:
            if existing_pos is not None:
                try:
                    if risk_manager.needs_exit_evaluation(iv, existing_pos, research):
                        exit_candidates.append((symbol, iv, existing_pos))
                except Exception as exc:
                    logger.warning("needs_exit_evaluation failed for %s: %s", symbol, exc)

        if exit_candidates:
            exit_decisions = _ask_claude_exit_batch(exit_candidates, research, ai)
            for sym, iv, existing_pos in exit_candidates:
                decision = exit_decisions.get(sym, {})
                if decision.get("action") == "CLOSE":
                    try:
                        close_position(sym, alpaca, open_positions, iv.current_price, iv, "thesis_break")
                        result.closes += 1
                    except Exception as exc:
                        logger.error("Error closing thesis-break position %s: %s", sym, exc, exc_info=True)

        # ── Phase 3: Claude batch ─────────────────────────────────────────────
        t_claude = time.monotonic()
        decisions: dict[str, dict] = {}
        for i in range(0, len(needs_claude), config.CLAUDE_BATCH_SIZE):
            batch = needs_claude[i : i + config.CLAUDE_BATCH_SIZE]
            batch_decisions = _ask_claude_batch(
                batch, research, strategy_memory, weights, ai, fred_cache, research_meta
            )
            decisions.update(batch_decisions)
        claude_elapsed = time.monotonic() - t_claude

        # ── Phase 4: Execute ──────────────────────────────────────────────────
        t_exec = time.monotonic()
        entries_this_cycle = 0

        for symbol, iv, existing_pos, symbol_ret5d in stock_data:
            try:
                decision = decisions.get(symbol)
                if decision is None:
                    continue

                action = decision.get("action", "HOLD").upper()

                if action == "BUY" and not existing_pos and buying_power >= config.MIN_BUYING_POWER and can_open:
                    pos_data = execute_buy(
                        symbol=symbol, iv=iv, decision=decision,
                        regime=regime, regime_info=regime_info,
                        research=research, open_positions=open_positions,
                        alpaca=alpaca, portfolio_value=portfolio_value,
                        pos_mult=pos_mult, spy_ret5d=spy_ret5d,
                        symbol_ret5d=symbol_ret5d, rsi_max=rsi_max,
                        params=params, now=now,
                        entries_this_cycle=entries_this_cycle,
                    )
                    if pos_data is not None:
                        entries_this_cycle += 1
                        result.entries += 1
                        buying_power -= iv.current_price * pos_data["qty"]

                elif action == "CLOSE" and existing_pos:
                    close_position(symbol, alpaca, open_positions, iv.current_price, iv, "claude_decision")
                    result.closes += 1

                elif action == "HOLD":
                    logger.info(
                        "HOLD %s: composite=%+.1f, RSI=%.1f | %s",
                        symbol, iv.composite_score, iv.rsi, decision.get("reasoning", "")[:120],
                    )

            except Exception as exc:
                logger.error("Error processing %s: %s", symbol, exc, exc_info=True)

        exec_elapsed = time.monotonic() - t_exec
        total = time.monotonic() - t0
        logger.info(
            "[CYCLE] %d symbols | fetch=%.1fs gates=%.1fs (%d passed) claude=%.1fs execute=%.1fs | total=%.1fs",
            len(symbols), fetch_elapsed, gates_elapsed, len(needs_claude),
            claude_elapsed, exec_elapsed, total,
        )
        return result
