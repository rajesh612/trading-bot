"""
TradingPipeline: four-phase per-cycle orchestrator.

Phase 1  — Parallel data fetch
Phase 2  — Pre-Claude filter (budget pre-check + 5-layer filter)
Phase 2.5— Thesis-break detection + intraday shock trigger
Phase 3  — Claude batch (single API call for symbols that need it)
Phase 4a — Auto-execute (no Claude)
Phase 4  — Execute BUY / CLOSE decisions
"""
from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
import anthropic

import config
from broker.alpaca_client import AlpacaClient
from indicators.values import IndicatorValues
from indicators.engine import compute_indicators
import notifications.telegram as telegram_notify
from signals.atr_tp import compute_atr_tp_pct
from trading.cycle_context import CycleSnapshot
from trading.journal import (
    load_daily_closed,
    load_open_positions,
)
from trading.claude_caller import _ask_claude_batch, _ask_claude_exit_batch
from trading import risk_manager
from trading.closer import close_position
from trading.executor import execute_buy
from trading.pre_claude_filter import pre_claude_filter

from utils.constants import ET

logger = logging.getLogger(__name__)


def _compute_auto_execute_confidence(composite_score: float, intra_sentiment: int) -> int:
    """Derive auto-execute confidence from signal strength. Clamped to [BASE, 95]."""
    base = config.AUTO_EXECUTE_CONFIDENCE_BASE
    composite_bonus = min(15, int((composite_score - config.AUTO_EXECUTE_COMPOSITE_MIN) * 0.5))
    sentiment_bonus = min(5, max(0, int(intra_sentiment / 10)))
    return min(95, base + composite_bonus + sentiment_bonus)


def _record_telemetry(ts: str, symbol: str, decision: str, reason: str, iv) -> None:
    """Append one symbol decision to cycle_telemetry.jsonl for post-hoc debugging."""
    try:
        record = json.dumps({
            "ts": ts, "symbol": symbol, "decision": decision, "reason": reason,
            "composite": round(iv.composite_score, 1),
            "rsi": round(iv.rsi, 1),
            "vwap_slope": round(iv.vwap_slope, 3),
            "stoch_d": round(iv.stoch_d, 1),
            "parabolic": iv.parabolic,
            "stop_hunt_zone": iv.stop_hunt_zone,
        })
        with open(config.CYCLE_TELEMETRY_FILE, "a") as f:
            f.write(record + "\n")
    except Exception:
        pass


_SECTOR_ETF_MAP: dict[str, str] = {
    # XLK — Technology
    "AAPL": "XLK", "MSFT": "XLK", "NVDA": "XLK", "AMD": "XLK", "INTC": "XLK",
    "GOOGL": "XLK", "META": "XLK", "AVGO": "XLK", "QCOM": "XLK", "MU": "XLK",
    "AMAT": "XLK", "KLAC": "XLK", "LRCX": "XLK", "MRVL": "XLK", "ON": "XLK",
    "AMZN": "XLK", "TSLA": "XLK",
    "ADI": "XLK", "ADBE": "XLK", "ANET": "XLK", "APP": "XLK", "ARM": "XLK",
    "ASML": "XLK", "CIEN": "XLK", "COHR": "XLK", "CRM": "XLK", "CRWD": "XLK",
    "DDOG": "XLK", "DELL": "XLK", "EQIX": "XLK", "GLW": "XLK", "HUBS": "XLK",
    "IBM": "XLK", "INTU": "XLK", "LITE": "XLK", "MDB": "XLK", "NET": "XLK",
    "NFLX": "XLK", "NOW": "XLK", "ORCL": "XLK", "PANW": "XLK", "PLTR": "XLK",
    "SHOP": "XLK", "SNPS": "XLK", "SNOW": "XLK", "SPOT": "XLK", "STX": "XLK",
    "TSM": "XLK", "TTD": "XLK", "TXN": "XLK", "UBER": "XLK", "VRT": "XLK",
    "ZS": "XLK",
    # XLF — Financials
    "JPM": "XLF", "BAC": "XLF", "WFC": "XLF", "GS": "XLF", "MS": "XLF",
    "C": "XLF", "BLK": "XLF", "AXP": "XLF", "V": "XLF", "MA": "XLF",
    "BRK.B": "XLF", "COIN": "XLF", "PYPL": "XLF", "SPGI": "XLF",
    # XLE — Energy
    "XOM": "XLE", "CVX": "XLE", "COP": "XLE", "EOG": "XLE", "SLB": "XLE",
    "MPC": "XLE",
    # XLV — Healthcare
    "JNJ": "XLV", "UNH": "XLV", "PFE": "XLV", "ABBV": "XLV", "MRK": "XLV",
    "LLY": "XLV", "AMGN": "XLV", "GILD": "XLV",
    "CRSP": "XLV", "DXCM": "XLV", "ISRG": "XLV", "REGN": "XLV",
    # XLI — Industrials/Defense/Aerospace
    "GE": "XLI", "CAT": "XLI", "BA": "XLI", "HON": "XLI", "UPS": "XLI",
    "GD": "XLI", "GEV": "XLI", "HEI": "XLI", "HWM": "XLI", "LHX": "XLI",
    "LMT": "XLI", "NOC": "XLI", "RTX": "XLI", "TDG": "XLI",
    # XLU — Utilities
    "CEG": "XLU", "NEE": "XLU", "VST": "XLU",
    # XLY — Consumer Discretionary
    "ABNB": "XLY", "BKNG": "XLY", "DIS": "XLY", "HLT": "XLY", "MAR": "XLY",
    # XLP — Consumer Staples
    "COST": "XLP", "KO": "XLP", "PG": "XLP", "WMT": "XLP",
    # XLB — Materials
    "NEM": "XLB",
}


@dataclass
class CycleResult:
    entries: int = 0
    closes: int = 0
    candidates: int = 0   # symbols that passed pre-Claude gates (reached Claude or auto-execute)
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


def _fetch_symbol_data(symbol: str, alpaca: AlpacaClient, open_positions: dict):
    """Fetch bars and compute indicators for one symbol. Returns tuple or None."""
    bars       = alpaca.get_intraday_bars(symbol)
    daily_bars = alpaca.get_daily_bars(symbol, days=8)
    iv = compute_indicators(symbol, bars, daily_df=daily_bars)
    if iv is None:
        return None
    ret5d = _compute_symbol_5day_return(daily_bars)
    return (symbol, iv, open_positions.get(symbol), ret5d)


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

        # Research staleness check
        ts_str = snap.research_meta.get("timestamp")
        if not ts_str:
            logger.warning(
                "Research report has no timestamp (__meta__ missing) — staleness check skipped."
            )
        else:
            try:
                report_dt = datetime.fromisoformat(ts_str)
                if report_dt.tzinfo is None:
                    report_dt = report_dt.replace(tzinfo=ET)
                age_min     = (datetime.now(ET) - report_dt).total_seconds() / 60
                report_date = report_dt.date()
                today_date  = datetime.now(ET).date()
                if report_date < today_date:
                    # Prior calendar date — all sentiment context is categorically wrong
                    logger.error(
                        "Research report is from %s (prior date) — running with neutral sentiment.",
                        report_date,
                    )
                    telegram_notify.notify_stale_research(age_min)
                elif age_min > config.RESEARCH_STALE_BLOCK_MIN:
                    # Same day but very old (> 10 h)
                    logger.warning(
                        "Research report is %.0f min old (same day) — running with stale sentiment.",
                        age_min,
                    )
                    telegram_notify.notify_stale_research(age_min)
                elif age_min > config.RESEARCH_STALE_WARN_MIN:
                    logger.warning("Research report is %.0f min old — sentiment may be stale.", age_min)
            except Exception as exc:
                logger.warning("Research timestamp parse failed (%s) — staleness check skipped.", exc)

        regime    = snap.regime
        spy_ret5d = snap.regime_info.get("spy_5day_return", 0.0)
        rsi_max   = (
            snap.params.get("rsi_buy_max_high_vol", config.RSI_BUY_MAX_HIGH_VOL)
            if regime in ("HIGH_VOL", "CRISIS", "DEFENSIVE")
            else snap.params.get("rsi_buy_max", config.RSI_BUY_MAX)
        )
        pos_mult  = (
            snap.params.get("high_vol_position_mult", config.HIGH_VOL_POSITION_MULT)
            if regime in ("HIGH_VOL", "DEFENSIVE", "CHOPPY")
            else 1.0
        )
        open_positions = load_open_positions()
        daily_closed = load_daily_closed()

        # ── Phase 1: Parallel data fetch ──────────────────────────────────────
        t_fetch = time.monotonic()

        stock_data: list[tuple[str, IndicatorValues, dict | None, float]] = []
        with ThreadPoolExecutor(max_workers=config.FETCH_WORKERS) as executor:
            futures = {
                executor.submit(_fetch_symbol_data, sym, alpaca, open_positions): sym
                for sym in symbols
            }
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
        # Auto-execute is always collected (zero API budget cost).
        # Claude candidates are only collected when entry budget allows.
        t_gates = time.monotonic()
        needs_claude:  list[tuple[str, IndicatorValues, dict | None]] = []
        auto_execute:  list[tuple[str, IndicatorValues]] = []

        ts_str = now.isoformat()
        for symbol, iv, existing_pos, _ret5d in stock_data:
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
        exit_candidates: list[tuple[str, IndicatorValues, dict]] = []
        for symbol, iv, existing_pos, _ret5d in stock_data:
            if existing_pos is not None:
                try:
                    if risk_manager.needs_exit_evaluation(iv, existing_pos, snap.research):
                        exit_candidates.append((symbol, iv, existing_pos))
                except Exception as exc:
                    logger.warning("needs_exit_evaluation failed for %s: %s", symbol, exc)

        # Intraday shock trigger — add to exit eval regardless of composite drop
        for symbol, iv, existing_pos, _ret5d in stock_data:
            if existing_pos is not None:
                intra = snap.intraday_news.get(symbol, {})
                if intra.get("shock_score", 0) >= config.INTRADAY_NEWS_SHOCK_BLOCK:
                    if (symbol, iv, existing_pos) not in exit_candidates:
                        exit_candidates.append((symbol, iv, existing_pos))
                        logger.info("EXIT-EVAL %s: intraday shock %d", symbol, intra["shock_score"])

        if exit_candidates:
            exit_decisions = _ask_claude_exit_batch(exit_candidates, snap, ai)
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
        if not can_open:
            logger.info("[CYCLE] can_open=False — skipping Claude entry calls (entry window not open yet)")
        else:
            for i in range(0, len(needs_claude), config.CLAUDE_BATCH_SIZE):
                batch = needs_claude[i : i + config.CLAUDE_BATCH_SIZE]
                batch_decisions = _ask_claude_batch(batch, snap, ai)
                decisions.update(batch_decisions)
        claude_elapsed = time.monotonic() - t_claude

        # ── Phase 4: Execute ──────────────────────────────────────────────────
        t_exec = time.monotonic()
        entries_this_cycle = 0
        _batch_entries: list[dict] = []

        _atr_tp_multiplier = snap.params.get("atr_tp_multiplier", config.ATR_TP_MULT_DEFAULT)

        # ── Phase 4a: Auto-execute (no Claude) ───────────────────────────────
        for symbol, iv in auto_execute:
            if not can_open or buying_power < config.MIN_BUYING_POWER:
                continue
            try:
                intra_sent = snap.research.get("__meta__", {}).get("shock_score", 0)
                confidence = _compute_auto_execute_confidence(iv.composite_score, intra_sent)
                pos_data = execute_buy(
                    symbol=symbol, iv=iv,
                    decision={"action": "BUY", "confidence": confidence,
                              "reasoning": "auto-execute: composite+sentiment threshold met",
                              "stop_loss_pct": config.STOP_LOSS_PCT,
                              "take_profit_pct": compute_atr_tp_pct(iv.atr, iv.current_price, _atr_tp_multiplier),
                              "source": "auto_execute"},
                    regime=snap.regime, regime_info=snap.regime_info,
                    research=snap.research, open_positions=open_positions,
                    alpaca=alpaca, portfolio_value=portfolio_value,
                    pos_mult=pos_mult, spy_ret5d=spy_ret5d,
                    symbol_ret5d=0.0, rsi_max=rsi_max,
                    params=snap.params, now=now,
                    entries_this_cycle=entries_this_cycle,
                    daily_pnl_pct=daily_pnl_pct,
                    sector_today=snap.sector_today,
                    symbol_sector_etf=_SECTOR_ETF_MAP.get(symbol),
                    advance_decline_ratio=snap.advance_decline_ratio,
                    suppress_notify=True,
                )
                if pos_data is not None:
                    entries_this_cycle += 1
                    result.entries += 1
                    buying_power -= iv.current_price * pos_data["qty"]
                    _batch_entries.append({
                        "symbol": symbol, "qty": pos_data["qty"],
                        "price": pos_data["entry_price"],
                        "sl": pos_data["stop_loss_price"], "tp": pos_data["take_profit_price"],
                        "score": iv.composite_score,
                        "confidence": confidence,
                        "mode": pos_data.get("mode_at_entry", "DAY_TRADE"),
                    })
            except Exception as exc:
                logger.error("Auto-execute error for %s: %s", symbol, exc, exc_info=True)

        for symbol, iv, existing_pos, symbol_ret5d in stock_data:
            try:
                decision = decisions.get(symbol)
                if decision is None:
                    continue

                action = decision.get("action", "HOLD").upper()

                if action == "BUY" and not existing_pos and buying_power >= config.MIN_BUYING_POWER and can_open:
                    pos_data = execute_buy(
                        symbol=symbol, iv=iv, decision=decision,
                        regime=snap.regime, regime_info=snap.regime_info,
                        research=snap.research, open_positions=open_positions,
                        alpaca=alpaca, portfolio_value=portfolio_value,
                        pos_mult=pos_mult, spy_ret5d=spy_ret5d,
                        symbol_ret5d=symbol_ret5d, rsi_max=rsi_max,
                        params=snap.params, now=now,
                        entries_this_cycle=entries_this_cycle,
                        daily_pnl_pct=daily_pnl_pct,
                        sector_today=snap.sector_today,
                        symbol_sector_etf=_SECTOR_ETF_MAP.get(symbol),
                        advance_decline_ratio=snap.advance_decline_ratio,
                        suppress_notify=True,
                    )
                    if pos_data is not None:
                        entries_this_cycle += 1
                        result.entries += 1
                        buying_power -= iv.current_price * pos_data["qty"]
                        _batch_entries.append({
                            "symbol": symbol, "qty": pos_data["qty"],
                            "price": pos_data["entry_price"],
                            "sl": pos_data["stop_loss_price"], "tp": pos_data["take_profit_price"],
                            "score": iv.composite_score,
                            "confidence": decision.get("confidence", 0),
                            "mode": pos_data.get("mode_at_entry", "DAY_TRADE"),
                        })

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

        telegram_notify.notify_buy_batch(_batch_entries, snap.regime)

        exec_elapsed = time.monotonic() - t_exec
        total = time.monotonic() - t0
        logger.info(
            "[CYCLE] %d symbols | fetch=%.1fs gates=%.1fs (%d passed) claude=%.1fs execute=%.1fs | total=%.1fs",
            len(symbols), fetch_elapsed, gates_elapsed, len(needs_claude),
            claude_elapsed, exec_elapsed, total,
        )
        return result
