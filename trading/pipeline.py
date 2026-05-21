"""TradingPipeline: four-phase per-cycle orchestrator (fetch → filter → Claude → execute)."""
from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
import anthropic

import config
import sectors
from broker.alpaca_client import AlpacaClient
from utils import regimes
from indicators.values import IndicatorValues
from indicators.engine import compute_indicators
import notifications.telegram as telegram_notify
from signals.atr_tp import compute_atr_tp_pct
from trading.cycle_context import CycleSnapshot
from trading.journal import load_daily_closed, load_open_positions
from trading.claude_caller import _ask_claude_batch, _ask_claude_exit_batch
from trading import risk_manager
from trading.closer import close_position
from trading.executor import execute_buy
from trading.pre_claude_filter import pre_claude_filter
from utils.constants import ET

logger = logging.getLogger(__name__)


def _compute_auto_execute_confidence(composite_score: float, intra_sentiment: int) -> int:
    base = config.AUTO_EXECUTE_CONFIDENCE_BASE
    composite_bonus = min(15, int((composite_score - config.AUTO_EXECUTE_COMPOSITE_MIN) * 0.5))
    sentiment_bonus = min(5, max(0, int(intra_sentiment / 10)))
    return min(95, base + composite_bonus + sentiment_bonus)


def _record_telemetry(ts: str, symbol: str, decision: str, reason: str, iv) -> None:
    try:
        record = json.dumps({
            "ts": ts, "symbol": symbol, "decision": decision, "reason": reason,
            "composite": round(iv.composite_score, 1), "rsi": round(iv.rsi, 1),
            "vwap_slope": round(iv.vwap_slope, 3), "stoch_d": round(iv.stoch_d, 1),
            "parabolic": iv.parabolic, "stop_hunt_zone": iv.stop_hunt_zone,
        })
        with open(config.CYCLE_TELEMETRY_FILE, "a") as f:
            f.write(record + "\n")
    except Exception:
        pass


@dataclass
class CycleResult:
    entries: int = 0
    closes: int = 0
    candidates: int = 0   # symbols that passed pre-Claude gates
    skipped: bool = False
    reason: str = ""


def _compute_symbol_5day_return(daily_df) -> float:
    """5-day return from daily OHLCV. Returns 0.0 if insufficient data."""
    if daily_df is None or len(daily_df) < 6:
        return 0.0
    try:
        c = daily_df["close"].values
        return float((c[-1] - c[-6]) / c[-6] * 100)
    except Exception:
        return 0.0


def _fetch_symbol_data(symbol: str, alpaca: AlpacaClient, open_positions: dict):
    bars = alpaca.get_intraday_bars(symbol)
    daily = alpaca.get_daily_bars(symbol, days=8)
    iv = compute_indicators(symbol, bars, daily_df=daily)
    if iv is None:
        return None
    return (symbol, iv, open_positions.get(symbol), _compute_symbol_5day_return(daily))


def _check_research_staleness(snap: CycleSnapshot) -> None:
    ts_str = snap.research_meta.get("timestamp")
    if not ts_str:
        logger.warning("Research report has no timestamp (__meta__ missing) — staleness check skipped.")
        return
    try:
        report_dt = datetime.fromisoformat(ts_str)
        if report_dt.tzinfo is None:
            report_dt = report_dt.replace(tzinfo=ET)
        age_min = (datetime.now(ET) - report_dt).total_seconds() / 60
        if report_dt.date() < datetime.now(ET).date():
            logger.error("Research report is from %s (prior date) — running with neutral sentiment.", report_dt.date())
            telegram_notify.notify_stale_research(age_min)
        elif age_min > config.RESEARCH_STALE_BLOCK_MIN:
            logger.warning("Research report is %.0f min old (same day) — running with stale sentiment.", age_min)
            telegram_notify.notify_stale_research(age_min)
        elif age_min > config.RESEARCH_STALE_WARN_MIN:
            logger.warning("Research report is %.0f min old — sentiment may be stale.", age_min)
    except Exception as exc:
        logger.warning("Research timestamp parse failed (%s) — staleness check skipped.", exc)


def _shared_buy_kwargs(symbol, iv, snap, alpaca, open_positions, portfolio_value,
                       pos_mult, spy_ret5d, rsi_max, now, entries_this_cycle, daily_pnl_pct) -> dict:
    return dict(
        regime=snap.regime, regime_info=snap.regime_info,
        research=snap.research, open_positions=open_positions,
        alpaca=alpaca, portfolio_value=portfolio_value,
        pos_mult=pos_mult, spy_ret5d=spy_ret5d, rsi_max=rsi_max,
        params=snap.params, now=now, entries_this_cycle=entries_this_cycle,
        daily_pnl_pct=daily_pnl_pct, sector_today=snap.sector_today,
        symbol_sector_etf=sectors.get_sector_etf(symbol),
        advance_decline_ratio=snap.advance_decline_ratio,
        suppress_notify=True, major_event_today=snap.major_event_today,
        staleness_guard=snap.staleness_guard,
    )


def _evaluate_thesis_exits(stock_data, snap, ai, alpaca, open_positions) -> int:
    """Phase 2.5: thesis-break + intraday shock exit evaluation. Returns number of closes."""
    closes = 0
    exit_candidates: list[tuple] = []
    for symbol, iv, existing_pos, _ in stock_data:
        if existing_pos is not None:
            try:
                if risk_manager.needs_exit_evaluation(iv, existing_pos, snap.research):
                    exit_candidates.append((symbol, iv, existing_pos))
            except Exception as exc:
                logger.warning("needs_exit_evaluation failed for %s: %s", symbol, exc)
    for symbol, iv, existing_pos, _ in stock_data:
        if existing_pos is not None:
            shock = snap.intraday_news.get(symbol, {}).get("shock_score", 0)
            if shock >= config.INTRADAY_NEWS_SHOCK_BLOCK and (symbol, iv, existing_pos) not in exit_candidates:
                exit_candidates.append((symbol, iv, existing_pos))
                logger.info("EXIT-EVAL %s: intraday shock %d", symbol, shock)
    if exit_candidates:
        exit_decisions = _ask_claude_exit_batch(exit_candidates, snap, ai)
        for sym, iv, existing_pos in exit_candidates:
            if exit_decisions.get(sym, {}).get("action") == "CLOSE":
                try:
                    close_position(sym, alpaca, open_positions, iv.current_price, iv, "thesis_break")
                    closes += 1
                except Exception as exc:
                    logger.error("Error closing thesis-break position %s: %s", sym, exc, exc_info=True)
    return closes


def _execute_auto_entries(auto_execute, snap, alpaca, open_positions, portfolio_value,
                          buying_power, pos_mult, spy_ret5d, rsi_max, atr_tp_mult,
                          can_open, now, daily_pnl_pct, entries_so_far) -> tuple:
    """Phase 4a: auto-execute buys. Returns (new_entries, updated_buying_power, batch)."""
    guard = snap.staleness_guard
    if not guard.auto_execute_allowed:
        logger.info(
            "[AUTO-EXEC] blocked — stale sources: %s",
            ", ".join(sorted(guard.stale_sources & config.DATA_DISABLE_AUTO_EXECUTE)),
        )
        return 0, buying_power, []
    new_entries = 0
    batch: list[dict] = []
    for symbol, iv in auto_execute:
        if not can_open or buying_power < config.MIN_BUYING_POWER:
            continue
        try:
            intra_sent = snap.intraday_news.get(symbol, {}).get(
                "sentiment_score",
                snap.research.get(symbol, {}).get("sentiment_score", 0),
            )
            confidence = _compute_auto_execute_confidence(iv.composite_score, intra_sent)
            decision = {
                "action": "BUY", "confidence": confidence, "source": "auto_execute",
                "reasoning": "auto-execute: composite+sentiment threshold met",
                "stop_loss_pct": config.STOP_LOSS_PCT,
                "take_profit_pct": compute_atr_tp_pct(iv.atr, iv.current_price, atr_tp_mult),
            }
            kwargs = _shared_buy_kwargs(symbol, iv, snap, alpaca, open_positions, portfolio_value,
                                        pos_mult, spy_ret5d, rsi_max, now,
                                        entries_so_far + new_entries, daily_pnl_pct)
            pos_data = execute_buy(symbol=symbol, iv=iv, decision=decision, symbol_ret5d=0.0, **kwargs)
            if pos_data is not None:
                new_entries += 1
                buying_power -= iv.current_price * pos_data["qty"]
                batch.append({
                    "symbol": symbol, "qty": pos_data["qty"], "price": pos_data["entry_price"],
                    "sl": pos_data["stop_loss_price"], "tp": pos_data["take_profit_price"],
                    "score": iv.composite_score, "confidence": confidence,
                    "mode": pos_data.get("mode_at_entry", "DAY_TRADE"),
                })
        except Exception as exc:
            logger.error("Auto-execute error for %s: %s", symbol, exc, exc_info=True)
    return new_entries, buying_power, batch


def _execute_claude_entries(stock_data, decisions, snap, alpaca, open_positions,
                            portfolio_value, buying_power, pos_mult, spy_ret5d, rsi_max,
                            can_open, now, daily_pnl_pct, entries_so_far) -> tuple:
    """Phase 4: Claude-decided BUYs and CLOSEs. Returns (entries, closes, buying_power, batch)."""
    new_entries = 0
    new_closes = 0
    batch: list[dict] = []
    for symbol, iv, existing_pos, symbol_ret5d in stock_data:
        try:
            decision = decisions.get(symbol)
            if decision is None:
                continue
            action = decision.get("action", "HOLD").upper()
            if action == "BUY" and not existing_pos and buying_power >= config.MIN_BUYING_POWER and can_open:
                kwargs = _shared_buy_kwargs(symbol, iv, snap, alpaca, open_positions, portfolio_value,
                                            pos_mult, spy_ret5d, rsi_max, now,
                                            entries_so_far + new_entries, daily_pnl_pct)
                pos_data = execute_buy(symbol=symbol, iv=iv, decision=decision,
                                       symbol_ret5d=symbol_ret5d, **kwargs)
                if pos_data is not None:
                    new_entries += 1
                    buying_power -= iv.current_price * pos_data["qty"]
                    batch.append({
                        "symbol": symbol, "qty": pos_data["qty"], "price": pos_data["entry_price"],
                        "sl": pos_data["stop_loss_price"], "tp": pos_data["take_profit_price"],
                        "score": iv.composite_score, "confidence": decision.get("confidence", 0),
                        "mode": pos_data.get("mode_at_entry", "DAY_TRADE"),
                    })
            elif action == "CLOSE" and existing_pos:
                close_position(symbol, alpaca, open_positions, iv.current_price, iv, "claude_decision")
                new_closes += 1
            elif action == "HOLD":
                logger.info("HOLD %s: composite=%+.1f, RSI=%.1f | %s",
                            symbol, iv.composite_score, iv.rsi, decision.get("reasoning", "")[:120])
        except Exception as exc:
            logger.error("Error processing %s: %s", symbol, exc, exc_info=True)
    return new_entries, new_closes, buying_power, batch


class TradingPipeline:
    def run(
        self,
        symbols: list[str],
        snap: CycleSnapshot,
        ai: anthropic.Anthropic,
        now: datetime,
        alpaca: AlpacaClient,
        portfolio_value: float,
        buying_power: float,
        can_open: bool,
        daily_pnl_pct: float = 0.0,
    ) -> CycleResult:
        t0 = time.monotonic()
        result = CycleResult()
        _check_research_staleness(snap)

        regime    = snap.regime
        spy_ret5d = snap.regime_info.get("spy_5day_return", 0.0)
        rsi_max   = (
            snap.params.get("rsi_buy_max_high_vol", config.RSI_BUY_MAX_HIGH_VOL)
            if regime in regimes.HIGH_VOL_GATES | {regimes.CRISIS}
            else snap.params.get("rsi_buy_max", config.RSI_BUY_MAX)
        )
        pos_mult = (
            snap.params.get("high_vol_position_mult", config.HIGH_VOL_POSITION_MULT)
            if regime in regimes.SIZE_REDUCE else 1.0
        )
        open_positions = load_open_positions()
        daily_closed   = load_daily_closed()

        # ── Phase 1: Parallel data fetch ──────────────────────────────────────
        t_fetch = time.monotonic()
        stock_data: list[tuple[str, IndicatorValues, dict | None, float]] = []
        with ThreadPoolExecutor(max_workers=config.FETCH_WORKERS) as executor:
            futures = {executor.submit(_fetch_symbol_data, sym, alpaca, open_positions): sym for sym in symbols}
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
            result.skipped = True
            return result

        # ── Phase 2: Pre-Claude filter ────────────────────────────────────────
        t_gates = time.monotonic()
        needs_claude: list[tuple[str, IndicatorValues, dict | None]] = []
        auto_execute: list[tuple[str, IndicatorValues]] = []
        ts_str = now.isoformat()
        for symbol, iv, existing_pos, _ in stock_data:
            if existing_pos:
                _ep = existing_pos.get("entry_price", 0)
                pnl_pct = (iv.current_price - _ep) / _ep * 100 if _ep > 0 else 0.0
                logger.info("BRACKET-MANAGED %s: P&L=%+.2f%% composite=%+.1f", symbol, pnl_pct, iv.composite_score)
                continue
            filt = pre_claude_filter(symbol, iv, snap, daily_closed)
            if filt.decision == "SKIP":
                logger.info("SKIP-PRECLAUDE %s: composite=%+.1f — %s", symbol, iv.composite_score, filt.reason)
                _record_telemetry(ts_str, symbol, "SKIP", filt.reason, iv)
            elif filt.decision == "AUTO_EXECUTE":
                auto_execute.append((symbol, iv))
                logger.info("AUTO-EXECUTE %s: composite=%+.1f — %s", symbol, iv.composite_score, filt.reason)
                _record_telemetry(ts_str, symbol, "AUTO_EXECUTE", filt.reason, iv)
            elif not risk_manager.is_entry_budget_exhausted():
                needs_claude.append((symbol, iv, None))
                _record_telemetry(ts_str, symbol, "CLAUDE", "", iv)
            else:
                logger.info("BUDGET-EXHAUSTED HOLD %s: would need Claude", symbol)
                _record_telemetry(ts_str, symbol, "BUDGET_HOLD", "entry_budget_exhausted", iv)
        result.candidates = len(needs_claude) + len(auto_execute)
        gates_elapsed = time.monotonic() - t_gates

        # ── Phase 2.5: Thesis-break detection ────────────────────────────────
        result.closes += _evaluate_thesis_exits(stock_data, snap, ai, alpaca, open_positions)

        # ── Phase 3: Claude batch ─────────────────────────────────────────────
        t_claude = time.monotonic()
        decisions: dict[str, dict] = {}
        if not can_open:
            logger.info("[CYCLE] can_open=False — skipping Claude entry calls (entry window not open yet)")
        else:
            for i in range(0, len(needs_claude), config.CLAUDE_BATCH_SIZE):
                decisions.update(_ask_claude_batch(needs_claude[i : i + config.CLAUDE_BATCH_SIZE], snap, ai))
        claude_elapsed = time.monotonic() - t_claude

        # ── Phase 4: Execute ──────────────────────────────────────────────────
        t_exec = time.monotonic()
        atr_tp_mult = snap.params.get("atr_tp_multiplier", config.ATR_TP_MULT_DEFAULT)

        auto_n, buying_power, auto_batch = _execute_auto_entries(
            auto_execute, snap, alpaca, open_positions, portfolio_value, buying_power,
            pos_mult, spy_ret5d, rsi_max, atr_tp_mult, can_open, now, daily_pnl_pct, 0,
        )
        result.entries += auto_n

        claude_n, claude_c, buying_power, claude_batch = _execute_claude_entries(
            stock_data, decisions, snap, alpaca, open_positions, portfolio_value, buying_power,
            pos_mult, spy_ret5d, rsi_max, can_open, now, daily_pnl_pct, auto_n,
        )
        result.entries += claude_n
        result.closes  += claude_c

        telegram_notify.notify_buy_batch(auto_batch + claude_batch, snap.regime)
        exec_elapsed = time.monotonic() - t_exec
        total = time.monotonic() - t0
        stale_tag = (
            f"[STALE:{','.join(sorted(snap.staleness_guard.stale_sources))}] "
            if snap.staleness_guard.stale_sources else ""
        )
        logger.info(
            "%s[CYCLE] %d symbols | fetch=%.1fs gates=%.1fs (%d passed) claude=%.1fs execute=%.1fs | total=%.1fs",
            stale_tag, len(symbols), fetch_elapsed, gates_elapsed, len(needs_claude), claude_elapsed, exec_elapsed, total,
        )
        return result
