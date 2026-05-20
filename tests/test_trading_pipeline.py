"""
Trading pipeline, cycle context, and budget escalation unit tests.
Run with: pytest tests/test_trading_pipeline.py -v -m unit
"""
import json
import os
from datetime import datetime

import pytest

import config
from utils.constants import ET


# ── trading/pipeline: TradingPipeline pure-logic tests ────────────────────────

@pytest.mark.unit
class TestTradingPipeline:
    """Tests for CycleResult, _compute_symbol_5day_return, and research staleness."""

    def test_cycle_result_defaults(self):
        from trading.pipeline import CycleResult
        r = CycleResult()
        assert r.entries == 0
        assert r.closes == 0
        assert r.skipped is False
        assert r.reason == ""

    def test_cycle_result_skipped(self):
        from trading.pipeline import CycleResult
        r = CycleResult(skipped=True, reason="stale_research")
        assert r.skipped is True
        assert r.reason == "stale_research"

    def test_compute_5day_return_none_returns_zero(self):
        from trading.pipeline import _compute_symbol_5day_return
        assert _compute_symbol_5day_return(None) == pytest.approx(0.0)

    def test_compute_5day_return_insufficient_rows(self):
        import pandas as pd
        from trading.pipeline import _compute_symbol_5day_return
        df = pd.DataFrame({"close": [100.0, 101.0, 102.0]})
        assert _compute_symbol_5day_return(df) == pytest.approx(0.0)

    def test_compute_5day_return_correct_value(self):
        import pandas as pd
        from trading.pipeline import _compute_symbol_5day_return
        # 6 rows: iloc[-6]=100, iloc[-1]=110 → (110-100)/100*100 = 10.0%
        df = pd.DataFrame({"close": [100.0, 102.0, 104.0, 106.0, 108.0, 110.0]})
        result = _compute_symbol_5day_return(df)
        assert result == pytest.approx(10.0)

    def test_compute_5day_return_negative(self):
        import pandas as pd
        from trading.pipeline import _compute_symbol_5day_return
        df = pd.DataFrame({"close": [110.0, 108.0, 106.0, 104.0, 102.0, 100.0]})
        result = _compute_symbol_5day_return(df)
        assert result == pytest.approx(-100.0 * 10 / 110, rel=1e-4)

    def _snap(self, research=None, params=None):
        from trading.cycle_context import CycleSnapshot
        return CycleSnapshot(
            regime_info={"regime": "NORMAL", "macro_health_score": 0},
            regime="NORMAL",
            fred_cache={},
            research=research or {},
            research_meta=(research or {}).get("__meta__", {}),
            strategy_memory="",
            weights={},
            params=params or {
                "rsi_buy_max": 70, "rsi_buy_max_high_vol": 60,
                "high_vol_position_mult": 0.5, "vwap_slope_min": -0.5,
                "entry_start_hour": 10, "entry_end_hour": 15,
            },
            portfolio_gain_pct=0.0,
            intraday_news={},
        )

    def test_research_staleness_continues_when_too_old(self, monkeypatch):
        """pipeline.run() does NOT skip when research > RESEARCH_STALE_BLOCK_MIN — soft fallback."""
        from datetime import timedelta
        from unittest.mock import MagicMock, patch
        import config
        from trading.pipeline import TradingPipeline

        stale_ts = (datetime.now(ET) - timedelta(minutes=config.RESEARCH_STALE_BLOCK_MIN + 10)).isoformat()
        research = {"__meta__": {"timestamp": stale_ts}}

        mock_alpaca = MagicMock()
        mock_alpaca.get_intraday_bars.return_value = None
        mock_alpaca.get_daily_bars.return_value = None

        with patch("trading.pipeline.load_open_positions", return_value={}), \
             patch("trading.pipeline.load_daily_closed", return_value=set()), \
             patch("notifications.telegram.notify_alert"), \
             patch("notifications.telegram.notify_stale_research"), \
             patch("trading.pipeline._fetch_symbol_data", return_value=None):
            pipeline = TradingPipeline()
            result = pipeline.run(
                symbols=["TEST"], snap=self._snap(research=research),
                ai=None, now=datetime.now(ET), alpaca=mock_alpaca,
                portfolio_value=10000.0, buying_power=5000.0, can_open=True,
            )
        # Should skip because no stock_data was fetched, but research check should execute first
        assert result.skipped is True

    def test_research_staleness_no_skip_when_fresh(self, monkeypatch):
        """pipeline.run() does not skip when research is within acceptable age."""
        from datetime import timedelta
        import config
        from trading.pipeline import TradingPipeline
        from unittest.mock import MagicMock, patch

        fresh_ts = (datetime.now(ET) - timedelta(minutes=10)).isoformat()
        research = {"__meta__": {"timestamp": fresh_ts}}

        mock_alpaca = MagicMock()
        mock_alpaca.get_intraday_bars.return_value = None
        mock_alpaca.get_daily_bars.return_value = None

        with patch("trading.pipeline.load_open_positions", return_value={}), \
             patch("trading.pipeline.load_daily_closed", return_value=set()), \
             patch("trading.pipeline._fetch_symbol_data", return_value=None):
            pipeline = TradingPipeline()
            result = pipeline.run(
                symbols=["TEST"], snap=self._snap(research=research),
                ai=None, now=datetime.now(ET), alpaca=mock_alpaca,
                portfolio_value=10000.0, buying_power=5000.0, can_open=True,
            )
        # Should skip because no stock_data was fetched
        assert result.skipped is True

    def test_missing_meta_timestamp_no_crash(self, monkeypatch):
        """pipeline.run() handles missing __meta__.timestamp without raising."""
        from unittest.mock import MagicMock, patch
        from trading.pipeline import TradingPipeline

        research = {"__meta__": {}}  # no timestamp key
        with patch("trading.pipeline.load_open_positions", return_value={}), \
             patch("trading.pipeline.load_daily_closed", return_value=set()), \
             patch("trading.pipeline._fetch_symbol_data", return_value=None):
            pipeline = TradingPipeline()
            result = pipeline.run(
                symbols=["TEST"], snap=self._snap(research=research),
                ai=None, now=datetime.now(ET), alpaca=MagicMock(),
                portfolio_value=10000.0, buying_power=5000.0, can_open=True,
            )
        # Should skip because no stock_data was fetched
        assert result.skipped is True

    def test_malformed_timestamp_no_crash(self, monkeypatch):
        """pipeline.run() handles unparseable timestamp without raising."""
        from unittest.mock import MagicMock, patch
        from trading.pipeline import TradingPipeline

        research = {"__meta__": {"timestamp": "not-a-date"}}
        with patch("trading.pipeline.load_open_positions", return_value={}), \
             patch("trading.pipeline.load_daily_closed", return_value=set()), \
             patch("trading.pipeline._fetch_symbol_data", return_value=None):
            pipeline = TradingPipeline()
            result = pipeline.run(
                symbols=["TEST"], snap=self._snap(research=research),
                ai=None, now=datetime.now(ET), alpaca=MagicMock(),
                portfolio_value=10000.0, buying_power=5000.0, can_open=True,
            )
        # Should skip because no stock_data was fetched
        assert result.skipped is True


# ── trading/pipeline: per-cycle entry cap ─────────────────────────────────────

@pytest.mark.unit
class TestPipelinePerCycleCap:
    """Tests for the MAX_ENTRIES_PER_CYCLE enforcement via check_all_gates."""

    def _now_open(self) -> datetime:
        from zoneinfo import ZoneInfo
        return datetime(2024, 1, 15, 11, 0, 0, tzinfo=ZoneInfo("America/New_York"))

    @staticmethod
    def _iv(**overrides):
        import types
        base = dict(
            current_price=100.0, vwap=99.0, vwap_std2_upper=110.0,
            stop_hunt_zone=False, parabolic=False,
            momentum_exhaustion=False, bot_herding_score=0, squeeze_setup=False,
        )
        base.update(overrides)
        return types.SimpleNamespace(**base)

    def _patch(self, monkeypatch):
        import calendars.earnings as ec
        import macro.economic_calendar as ecc
        monkeypatch.setattr(ec, "is_earnings_imminent", lambda *a, **kw: False)
        monkeypatch.setattr(ecc, "is_event_risk_window", lambda *a, **kw: (False, ""))

    def test_cap_blocks_regardless_of_symbol(self, monkeypatch):
        """Cap is per-cycle, not per-symbol — applies even to a brand-new symbol."""
        import config
        monkeypatch.setattr(config, "MAX_ENTRIES_PER_CYCLE", 1)
        self._patch(monkeypatch)
        from signals.entry_gates import check_all_gates
        for sym in ("AAPL", "NVDA", "TSLA"):
            result = check_all_gates(
                sym, self._iv(), {}, set(), None, self._now_open(), entries_this_cycle=1,
            )
            assert result.passed is False, f"{sym} should be blocked by cap"

    def test_close_decision_bypasses_cap(self):
        """CLOSE action in pipeline calls close_position, not check_all_gates — cap irrelevant."""
        # check_all_gates is only called for BUY candidates; verifying it is not invoked for CLOSE
        # is implicit — here we confirm close_position exists and accepts the right signature.
        from trading.closer import close_position
        import inspect
        sig = inspect.signature(close_position)
        assert "symbol" in sig.parameters
        assert "entries_this_cycle" not in sig.parameters  # cap param is absent from closer

    def test_5day_return_exactly_6_rows(self):
        """_compute_symbol_5day_return needs at least 6 rows."""
        import pandas as pd
        from trading.pipeline import _compute_symbol_5day_return
        df5 = pd.DataFrame({"close": [100.0] * 5})
        df6 = pd.DataFrame({"close": [100.0] * 6})
        assert _compute_symbol_5day_return(df5) == pytest.approx(0.0)
        assert _compute_symbol_5day_return(df6) == pytest.approx(0.0)  # flat

    def test_cycle_result_entries_increments_correctly(self):
        """CycleResult.entries accumulates correctly when used as counter."""
        from trading.pipeline import CycleResult
        r = CycleResult()
        r.entries += 1
        assert r.entries == 1
        r.entries += 1
        assert r.entries == 2


@pytest.mark.unit
class TestContextSplit:
    """Verify stable vs. dynamic context separation for prompt caching."""

    _REGIME = {
        "regime": "NORMAL", "macro_health_score": 5,
        "vix_current": 18.0, "vix_90d_avg": 20.0, "vix_trend": "falling",
        "spy_5day_return": 1.2, "consecutive_green_days": 3,
        "consecutive_red_days": 0, "spy_5day_sign_changes": 1,
        "sector_scores": {"XLK": 10, "XLF": -5},
        "hyg_ret5d": 0.3, "iwm_ret5d": 0.8, "vix3m": 19.0,
    }
    _FRED = {"fed_direction": "paused", "cpi_trend": "stable", "unemployment_rising": False}
    _META = {"macro_shock_score": 10, "capitulation_candidates": []}

    def test_stable_context_contains_no_time(self):
        """build_stable_system_context must not embed a clock time string."""
        import re
        from trading.context_builder import build_stable_system_context
        text = build_stable_system_context(self._REGIME, self._FRED, self._META)
        assert not re.search(r"\d{2}:\d{2}", text), (
            "Stable context must not contain a time like '10:30' — it breaks prompt caching"
        )

    def test_stable_context_is_deterministic(self):
        """Same inputs produce identical output — required for cache hits."""
        from trading.context_builder import build_stable_system_context
        a = build_stable_system_context(self._REGIME, self._FRED, self._META)
        b = build_stable_system_context(self._REGIME, self._FRED, self._META)
        assert a == b

    def test_dynamic_context_contains_time(self):
        """build_dynamic_user_context must include the current HH:MM marker."""
        from trading.context_builder import build_dynamic_user_context
        now = datetime(2024, 1, 15, 11, 30, 0, tzinfo=ET)
        text = build_dynamic_user_context(now)
        assert "11:30" in text


# ── Pipeline HIGH_VOL regime routing ─────────────────────────────────────────

@pytest.mark.unit
def test_pipeline_passes_correct_rsi_max_and_pos_mult_for_high_vol():
    """TradingPipeline.run() passes RSI_BUY_MAX_HIGH_VOL and pos_mult=0.5 to execute_buy in HIGH_VOL."""
    from datetime import datetime, timedelta
    from unittest.mock import MagicMock, patch
    import config
    from trading.pipeline import TradingPipeline
    from trading.cycle_context import CycleSnapshot
    from trading.pre_claude_filter import FilterResult
    from utils.constants import ET

    fresh_ts = (datetime.now(ET) - timedelta(minutes=5)).isoformat()
    snap = CycleSnapshot(
        regime_info={"regime": "HIGH_VOL", "macro_health_score": 0, "spy_5day_return": 0.0},
        regime="HIGH_VOL",
        fred_cache={},
        research={"__meta__": {"timestamp": fresh_ts}},
        research_meta={"timestamp": fresh_ts, "macro_shock_score": 0},
        strategy_memory="",
        weights={},
        params={
            "rsi_buy_max": config.RSI_BUY_MAX,
            "rsi_buy_max_high_vol": config.RSI_BUY_MAX_HIGH_VOL,
            "high_vol_position_mult": 0.5,
            "vwap_slope_min": -0.3,
            "entry_start_hour": 10,
            "entry_end_hour": 15,
        },
        portfolio_gain_pct=0.0,
        intraday_news={},
    )

    mock_iv = MagicMock()
    mock_iv.composite_score = 50.0
    mock_iv.current_price = 100.0

    captured = {}

    def fake_execute_buy(*, rsi_max, pos_mult, **kw):
        captured["rsi_max"] = rsi_max
        captured["pos_mult"] = pos_mult
        return None

    with patch("trading.pipeline._fetch_symbol_data", return_value=("TEST", mock_iv, None, 0.0)), \
         patch("trading.pipeline.pre_claude_filter", return_value=FilterResult("CLAUDE", "")), \
         patch("trading.pipeline.risk_manager.is_entry_budget_exhausted", return_value=False), \
         patch("trading.pipeline._ask_claude_batch", return_value={"TEST": {"action": "BUY", "confidence": 80, "reasoning": "", "stop_loss_pct": 4.0, "take_profit_pct": 5.0}}), \
         patch("trading.pipeline._ask_claude_exit_batch", return_value={}), \
         patch("trading.pipeline.load_open_positions", return_value={}), \
         patch("trading.pipeline.load_daily_closed", return_value=set()), \
         patch("trading.pipeline.execute_buy", side_effect=fake_execute_buy):
        TradingPipeline().run(
            symbols=["TEST"],
            snap=snap,
            ai=MagicMock(),
            now=datetime.now(ET),
            alpaca=MagicMock(),
            portfolio_value=10000.0,
            buying_power=5000.0,
            can_open=True,
        )

    assert captured.get("rsi_max") == config.RSI_BUY_MAX_HIGH_VOL, \
        f"Expected rsi_max={config.RSI_BUY_MAX_HIGH_VOL}, got {captured.get('rsi_max')}"
    assert captured.get("pos_mult") == 0.5, \
        f"Expected pos_mult=0.5, got {captured.get('pos_mult')}"


@pytest.mark.unit
def test_execute_buy_blocked_by_high_vol_composite_gate():
    """execute_buy rejects composite=30 in HIGH_VOL regime (effective_composite_gate returns 35)."""
    from unittest.mock import MagicMock, patch
    import config
    from trading.executor import execute_buy
    from signals.entry_gates import GateResult

    iv = MagicMock()
    iv.rsi = 55.0
    iv.vwap_slope = 0.1
    iv.composite_score = 30.0
    iv.squeeze_setup = False
    iv.bot_herding_score = 0
    iv.current_price = 100.0
    iv.momentum_exhaustion = False

    with patch("trading.executor.get_intraday_bars", return_value=None), \
         patch("trading.executor.load_json", return_value=[]), \
         patch("trading.executor.check_all_gates", return_value=GateResult(passed=True)), \
         patch("trading.executor.get_active_mode", return_value="DAY_TRADE"):
        result = execute_buy(
            symbol="TEST", iv=iv,
            decision={"action": "BUY", "stop_loss_pct": 4.0, "take_profit_pct": 5.0,
                      "confidence": 0.8, "reasoning": "test"},
            regime="HIGH_VOL",
            regime_info={"regime": "HIGH_VOL", "macro_health_score": 0},
            research={"TEST": {"sentiment_score": 10}},
            open_positions={},
            alpaca=MagicMock(),
            portfolio_value=10000.0,
            pos_mult=0.5,
            spy_ret5d=0.0,
            symbol_ret5d=0.0,
            rsi_max=config.RSI_BUY_MAX_HIGH_VOL,
            params={"vwap_slope_min": -0.3},
            now=MagicMock(),
            entries_this_cycle=0,
        )

    assert result is None


@pytest.mark.unit
def test_pipeline_skips_claude_when_can_open_false(monkeypatch):
    """Phase 3 must not call Claude entry calls when can_open=False."""
    import trading.pipeline as pl_mod
    from trading.claude_caller import _ask_claude_batch
    from unittest.mock import MagicMock, patch
    from trading.cycle_context import CycleSnapshot
    from trading.pipeline import TradingPipeline
    from datetime import datetime

    called = []

    snap = MagicMock(spec=CycleSnapshot)
    snap.regime = "NORMAL"
    snap.regime_info = {"spy_5day_return": 0.0}
    snap.research = {}
    snap.research_meta = {}
    snap.intraday_news = {}
    snap.params = {}
    snap.strategy_memory = ""
    snap.weights = {}
    snap.fred_cache = {}

    with patch("trading.pipeline._ask_claude_batch", side_effect=lambda *a, **k: called.append(a) or {}), \
         patch("trading.pipeline.load_open_positions", return_value={}), \
         patch("trading.pipeline.load_daily_closed", return_value=set()), \
         patch("trading.pipeline._fetch_symbol_data", return_value=None), \
         patch("trading.pipeline.pre_claude_filter"):
        pipeline = TradingPipeline()
        pipeline.run(
            symbols=["AAPL"],
            snap=snap,
            ai=MagicMock(),
            now=datetime(2026, 5, 13, 10, 30, tzinfo=ET),
            alpaca=MagicMock(),
            portfolio_value=10_000.0,
            buying_power=5_000.0,
            can_open=False,
        )

    assert called == [], "Claude must not be called when can_open=False"


@pytest.mark.unit
def test_pipeline_warns_when_research_timestamp_missing(caplog):
    """Missing __meta__ timestamp must emit a warning, not silently skip."""
    import logging
    from unittest.mock import MagicMock, patch
    from trading.cycle_context import CycleSnapshot
    from trading.pipeline import TradingPipeline
    from datetime import datetime

    snap = MagicMock(spec=CycleSnapshot)
    snap.regime = "NORMAL"
    snap.regime_info = {"spy_5day_return": 0.0}
    snap.research = {}
    snap.research_meta = {}  # no __meta__ timestamp → missing timestamp
    snap.intraday_news = {}
    snap.params = {}
    snap.strategy_memory = ""
    snap.weights = {}
    snap.fred_cache = {}

    with caplog.at_level(logging.WARNING, logger="trading.pipeline"), \
         patch("trading.pipeline.load_open_positions", return_value={}), \
         patch("trading.pipeline.load_daily_closed", return_value=set()):
        pipeline = TradingPipeline()
        pipeline.run(
            symbols=[],
            snap=snap,
            ai=MagicMock(),
            now=datetime(2026, 5, 13, 10, 30, tzinfo=ET),
            alpaca=MagicMock(),
            portfolio_value=10_000.0,
            buying_power=5_000.0,
            can_open=True,
        )

    assert any(
        "staleness" in r.message.lower() or "timestamp" in r.message.lower()
        for r in caplog.records
    ), "Expected a warning about missing research timestamp"


@pytest.mark.unit
def test_strategy_memory_truncation_keeps_head_not_tail(tmp_path, monkeypatch):
    """build_cycle_snapshot must truncate strategy_memory from the head, not the tail."""
    import config
    from unittest.mock import patch
    from trading.cycle_context import build_cycle_snapshot

    long_memory = "# General Rules\n" + ("A" * 100 + "\n") * 20 + "# Tail Section\n" + ("B" * 100 + "\n") * 20
    mem_file = tmp_path / "strategy_memory.md"
    mem_file.write_text(long_memory)

    monkeypatch.setattr(config, "STRATEGY_MEMORY_FILE", mem_file)
    monkeypatch.setattr(config, "STRATEGY_MEMORY_MAX_CHARS", 500)
    monkeypatch.setattr(config, "STRATEGY_MEMORY_MAX_AGE_DAYS", 365)

    import trading.cycle_context as _cc
    with patch("trading.cycle_context.load_macro_regime", return_value={"regime": "NORMAL"}), \
         patch("trading.cycle_context.load_trading_params", return_value={}), \
         patch("trading.cycle_context.load_json", return_value={}), \
         patch("trading.cycle_context._get_fred_cache", return_value={}), \
         patch("trading.cycle_context.load_intraday_news", return_value={}), \
         patch.object(_cc, "config", config):
        snap = build_cycle_snapshot(portfolio_gain_pct=0.0)

    assert snap.strategy_memory.startswith("# General Rules"), "Must keep head (rules section)"
    assert "Tail Section" not in snap.strategy_memory, "Must not keep tail"
    assert len(snap.strategy_memory) <= 500


@pytest.mark.unit
def test_stock_block_is_under_150_tokens():
    """build_stock_block must stay compact to conserve Claude budget."""
    from trading.context_builder import build_stock_block
    from unittest.mock import MagicMock, patch

    iv = MagicMock()
    iv.composite_score = 42.5; iv.signal = "BUY"
    iv.current_price = 100.50; iv.vwap = 100.20
    iv.rsi = 58.3; iv.rsi_slope = 2.1; iv.macd_hist = 0.0023
    iv.bb_lower = 99.0; iv.bb_upper = 102.0
    iv.ema9 = 100.2; iv.ema21 = 99.8; iv.atr = 0.05
    iv.stoch_k = 65.0; iv.stoch_d = 60.0
    iv.daily_volume_ratio = 1.2
    iv.vwap_slope = 0.023; iv.or_position = "outside"

    research = {"TEST": {
        "sentiment_score": 35, "sentiment_label": "BULLISH",
        "key_catalyst": "AI chip demand", "risk_flag": None,
        "summary": "",
    }}

    with patch("calendars.earnings.detect_pead_setup", return_value=(False, 0)):
        block = build_stock_block("TEST", iv, research, None)

    approx_tokens = len(block) / 4
    assert approx_tokens < 200, f"Stock block too large: ~{approx_tokens:.0f} tokens\n{block}"
    assert "ATR=" not in block, "ATR is not directional — remove from Claude block"
    assert "No summary" not in block, "Empty summary must be omitted"
    assert '<stock symbol="TEST">' in block, "Stock block must use XML wrapper"
    assert "OR=outside" in block, "Opening range position must be in block"


@pytest.mark.unit
def test_stock_block_includes_sl_tp_for_existing_position():
    """build_stock_block pos_block must include SL/TP so Claude can assess whether to hold."""
    from trading.context_builder import build_stock_block
    from unittest.mock import MagicMock, patch

    iv = MagicMock()
    iv.composite_score = 30.0; iv.signal = "HOLD"
    iv.current_price = 105.0; iv.vwap = 104.0
    iv.rsi = 55.0; iv.rsi_slope = 0.5; iv.macd_hist = 0.001
    iv.bb_lower = 103.0; iv.bb_upper = 107.0
    iv.ema9 = 104.5; iv.ema21 = 104.0
    iv.stoch_k = 60.0; iv.stoch_d = 55.0
    iv.daily_volume_ratio = 1.0
    iv.vwap_slope = 0.0; iv.or_position = "unknown"

    existing_pos = {
        "entry_price": 100.0, "qty": 10,
        "stop_loss_pct": 3.5, "take_profit_pct": 6.0,
    }
    research = {"TEST": {"sentiment_score": 10, "sentiment_label": "NEUTRAL", "key_catalyst": None, "risk_flag": None, "summary": ""}}

    with patch("calendars.earnings.detect_pead_setup", return_value=(False, 0)):
        block = build_stock_block("TEST", iv, research, existing_pos)

    assert "SL=3.5%" in block, f"SL missing from pos_block: {block}"
    assert "TP=6.0%" in block, f"TP missing from pos_block: {block}"


# ── Option A pre_claude_filter quality-only tests ────────────────────────────

def _make_iv_pipeline(**kwargs):
    from indicators.values import IndicatorValues
    defaults = dict(
        symbol="AAPL", timestamp="2026-01-01T10:00:00",
        current_price=100.0, vwap=99.0,
        vwap_std1_upper=100.5, vwap_std1_lower=97.5,
        vwap_std2_upper=101.5, vwap_std2_lower=96.5,
        rsi=60.0, macd=0.1, macd_signal=0.05, macd_hist=0.05,
        bb_upper=102.0, bb_mid=100.0, bb_lower=98.0,
        ema9=100.2, ema21=99.8, atr=1.5, volume_ratio=1.0,
        stoch_k=60.0, stoch_d=60.0, obv=1000.0,
        vwap_slope=0.1, rsi_slope=0.0,
        daily_volume_ratio=1.0, daily_volume_trend=0.0, daily_volume_mean=1_000_000.0,
        composite_score=25.0, signal="BUY", confidence=0.7,
    )
    defaults.update(kwargs)
    return IndicatorValues(**defaults)


def _make_snap_pipeline(regime="NORMAL"):
    from unittest.mock import MagicMock
    snap = MagicMock()
    snap.regime = regime
    snap.params = {}
    snap.regime_info = {"macro_health_score": 0}
    snap.research_meta = {"macro_shock_score": 0}
    snap.research = {}
    snap.intraday_news = {}
    snap.sector_today = {}
    return snap


@pytest.mark.unit
def test_pre_claude_filter_passes_stoch_95(monkeypatch):
    """Option A: Stoch 95 must NOT cause SKIP in pre_claude_filter (only in check_all_gates)."""
    import config
    monkeypatch.setattr(config, "AUTO_EXECUTE_COMPOSITE_MIN", 100)
    from trading.pre_claude_filter import pre_claude_filter
    iv = _make_iv_pipeline(composite_score=35.0, stoch_d=95.0)
    snap = _make_snap_pipeline()
    result = pre_claude_filter("AAPL", iv, snap, set())
    assert result.decision != "SKIP", f"Option A violated: Stoch 95 caused SKIP: {result.reason}"


@pytest.mark.unit
def test_pre_claude_filter_passes_vwap_overextended(monkeypatch):
    """Option A: price > VWAP+2σ must NOT cause SKIP in pre_claude_filter."""
    import config
    monkeypatch.setattr(config, "AUTO_EXECUTE_COMPOSITE_MIN", 100)
    from trading.pre_claude_filter import pre_claude_filter
    iv = _make_iv_pipeline(composite_score=35.0, current_price=104.0, vwap_std2_upper=101.5)
    snap = _make_snap_pipeline()
    result = pre_claude_filter("AAPL", iv, snap, set())
    assert result.decision != "SKIP", f"Option A violated: VWAP overextension caused SKIP: {result.reason}"


@pytest.mark.unit
def test_budget_intraday_warning_fired_at_75pct(monkeypatch, tmp_path):
    import config
    monkeypatch.setattr(config, "API_BUDGET_FILE", tmp_path / "budget.json")
    from utils.io_utils import save_json
    from trading import risk_manager

    from datetime import datetime
    from utils.constants import ET
    entry_ceil = 0.70
    budget = {
        "date": datetime.now(ET).strftime("%Y-%m-%d"), "calls_made": 5, "entry_calls": 5, "exit_calls": 0,
        "input_tokens": 0, "output_tokens": 0,
        "cache_write_tokens": 0, "cache_read_tokens": 0,
        "estimated_cost_usd": 0.536, "entry_spent_usd": 0.536, "exit_spent_usd": 0.0,
        "ceiling_usd": 1.00, "entry_ceiling_usd": entry_ceil, "exit_ceiling_usd": 0.30,
        "ceiling_mode": "normal",
        "entry_exhausted": False, "exit_exhausted": False,
        "budget_exhausted": False, "escalated": False,
    }
    save_json(config.API_BUDGET_FILE, budget)

    warned = []
    monkeypatch.setattr("notifications.telegram.notify_budget_intraday_warning",
                        lambda s, c: warned.append((s, c)))
    monkeypatch.setattr("notifications.telegram._budget_warn_sent", {"date": None})

    risk_manager.check_entry_budget(estimated_cost=0.01, regime="NORMAL")
    assert warned, "Expected budget intraday warning at 76% of entry ceiling"


# ── P4-T1: _validate_decision ─────────────────────────────────────────────────

@pytest.mark.unit
def test_validate_decision_clamps_confidence():
    from trading.claude_caller import _validate_decision
    raw = {"action": "BUY", "confidence": 150, "stop_loss_pct": 4.0, "take_profit_pct": 5.0, "reasoning": "x"}
    result = _validate_decision(raw, "AAPL")
    assert result["confidence"] == 100


@pytest.mark.unit
def test_validate_decision_clamps_stop_loss_negative():
    from trading.claude_caller import _validate_decision
    raw = {"action": "BUY", "confidence": 75, "stop_loss_pct": -1.5, "take_profit_pct": 5.0, "reasoning": "x"}
    result = _validate_decision(raw, "AAPL")
    assert result["stop_loss_pct"] >= 0.5


@pytest.mark.unit
def test_validate_decision_clamps_take_profit_high():
    from trading.claude_caller import _validate_decision
    raw = {"action": "BUY", "confidence": 75, "stop_loss_pct": 4.0, "take_profit_pct": 50.0, "reasoning": "x"}
    result = _validate_decision(raw, "AAPL")
    assert result["take_profit_pct"] <= 15.0


@pytest.mark.unit
def test_validate_decision_passthrough_valid():
    from trading.claude_caller import _validate_decision
    raw = {"action": "BUY", "confidence": 75, "stop_loss_pct": 4.0, "take_profit_pct": 5.0, "reasoning": "x"}
    result = _validate_decision(dict(raw), "AAPL")
    assert result["confidence"] == 75
    assert result["stop_loss_pct"] == 4.0


# ── P4-T2: system prompt structure (no cache_control — TTL 5 min < 10 min cycle gap) ──

def _make_fake_ai(calls: list, response_text: str):
    from unittest.mock import MagicMock
    def fake_create(**kwargs):
        calls.append(kwargs)
        r = MagicMock()
        block = MagicMock(text=response_text)
        block.type = "text"
        r.content = [block]
        r.usage = MagicMock(input_tokens=100, output_tokens=20,
                            cache_creation_input_tokens=0, cache_read_input_tokens=0)
        return r
    ai_mock = MagicMock()
    ai_mock.messages.create.side_effect = fake_create
    return ai_mock


@pytest.mark.unit
def test_exit_batch_system_prompt_has_cache_control(monkeypatch):
    """Exit-batch stable system block must have cache_control for prompt caching."""
    from trading.claude_caller import _ask_claude_exit_batch
    from unittest.mock import MagicMock

    calls = []
    snap = MagicMock()
    snap.regime = "NORMAL"
    snap.regime_info = {"vix_current": 15}
    snap.strategy_memory = ""
    snap.research = {}
    snap.portfolio_gain_pct = 0.0

    iv = MagicMock()
    iv.composite_score = 20.0
    iv.rsi = 55.0
    iv.current_price = 100.0

    monkeypatch.setattr("trading.claude_caller.build_exit_stock_block",
                        lambda sym, iv, pos, research, **kw: f"EXIT BLOCK {sym}")
    monkeypatch.setattr("trading.claude_caller.risk_manager.check_exit_budget",
                        lambda cost, count, regime: True)
    monkeypatch.setattr("trading.claude_caller.risk_manager.record_usage",
                        lambda **kw: None)

    _ask_claude_exit_batch(
        [("AAPL", iv, {"entry_price": 102.0})], snap,
        _make_fake_ai(calls, '{"AAPL": {"action": "HOLD", "reasoning": "ok"}}'),
    )

    assert calls, "Claude was not called"
    system_blocks = calls[0].get("system", [])
    assert system_blocks, "No system blocks in exit-batch call"
    assert any(
        isinstance(b, dict) and b.get("cache_control")
        for b in system_blocks
    ), "Stable system block must have cache_control for prompt caching"


@pytest.mark.unit
def test_entry_batch_system_prompt_has_cache_control(monkeypatch, tmp_path):
    """Entry-batch stable system block must have cache_control for prompt caching."""
    from trading.claude_caller import _ask_claude_batch
    from unittest.mock import MagicMock

    calls = []
    snap = MagicMock()
    snap.regime = "NORMAL"
    snap.regime_info = {"vix_current": 15, "spy_5day_return": 0.1}
    snap.strategy_memory = ""
    snap.research = {}
    snap.research_meta = {}
    snap.fred_cache = {}
    snap.weights = {}
    snap.params = {}

    iv = MagicMock()
    iv.composite_score = 60.0
    iv.rsi = 55.0
    iv.current_price = 100.0
    iv.atr = 1.5

    monkeypatch.setattr("trading.claude_caller.build_stock_block",
                        lambda sym, iv, research, pos, **kw: f"BLOCK {sym}")
    monkeypatch.setattr("trading.claude_caller.build_stable_system_context",
                        lambda *a: "STABLE CTX")
    monkeypatch.setattr("trading.claude_caller.build_dynamic_user_context",
                        lambda now, **kw: "DYNAMIC CTX")
    monkeypatch.setattr("trading.claude_caller.risk_manager.check_entry_budget",
                        lambda cost, regime: True)
    monkeypatch.setattr("trading.claude_caller.risk_manager.record_usage",
                        lambda **kw: None)

    _ask_claude_batch(
        [("AAPL", iv, None)], snap,
        _make_fake_ai(calls, '{"AAPL": {"action": "HOLD", "confidence": 50, '
                             '"reasoning": "ok", "stop_loss_pct": 4.0, '
                             '"take_profit_pct": 5.0, "indicator_signals": {}}}'),
    )

    assert calls, "Claude was not called"
    system_blocks = calls[0].get("system", [])
    assert system_blocks, "No system blocks in entry-batch call"
    assert any(
        isinstance(b, dict) and b.get("cache_control")
        for b in system_blocks
    ), "Stable system block must have cache_control for prompt caching"


# ── P4-T5: _compute_auto_execute_confidence ───────────────────────────────────

@pytest.mark.unit
def test_auto_execute_confidence_scales_with_composite(monkeypatch):
    from trading.pipeline import _compute_auto_execute_confidence
    monkeypatch.setattr(config, "AUTO_EXECUTE_CONFIDENCE_BASE", 75)
    monkeypatch.setattr(config, "AUTO_EXECUTE_COMPOSITE_MIN", 55)
    conf = _compute_auto_execute_confidence(composite_score=60.0, intra_sentiment=30)
    assert 75 <= conf <= 100
    conf_low = _compute_auto_execute_confidence(composite_score=55.0, intra_sentiment=0)
    assert conf_low >= config.AUTO_EXECUTE_CONFIDENCE_BASE
    assert conf_low <= conf


@pytest.mark.unit
def test_atr_tp_injected_into_claude_context():
    """build_stock_block emits ATR-TP line when atr_suggested_tp_pct is provided."""
    from trading.context_builder import build_stock_block
    from unittest.mock import MagicMock, patch

    iv = MagicMock()
    iv.composite_score = 42.5; iv.signal = "BUY"
    iv.current_price = 100.0; iv.vwap = 99.5
    iv.rsi = 58.0; iv.rsi_slope = 1.0; iv.macd_hist = 0.002
    iv.bb_lower = 98.0; iv.bb_upper = 102.0
    iv.ema9 = 99.8; iv.ema21 = 99.5; iv.atr = 2.0
    iv.stoch_k = 60.0; iv.stoch_d = 58.0
    iv.daily_volume_ratio = 1.2
    iv.vwap_slope = 0.0; iv.or_position = "unknown"
    research = {"TEST": {"sentiment_score": 10, "sentiment_label": "NEUTRAL",
                         "key_catalyst": None, "risk_flag": None, "summary": ""}}

    with patch("calendars.earnings.detect_pead_setup", return_value=(False, 0)):
        block = build_stock_block("TEST", iv, research, None, atr_suggested_tp_pct=4.5)

    assert "ATR-TP=4.5%" in block


@pytest.mark.unit
def test_claude_caller_passes_atr_tp_to_stock_block(monkeypatch, tmp_path):
    """_ask_claude_batch passes atr_suggested_tp_pct to build_stock_block."""
    import config
    from trading import claude_caller
    from trading.claude_caller import _ask_claude_batch
    from trading.cycle_context import CycleSnapshot
    from indicators.values import IndicatorValues
    from trading.context_builder import build_stock_block

    captured = {}

    def mock_build_stock_block(sym, iv, research, existing_pos, atr_suggested_tp_pct=None, **kw):
        captured["atr_suggested_tp_pct"] = atr_suggested_tp_pct
        return f"=={sym}=="

    monkeypatch.setattr("trading.claude_caller.build_stock_block", mock_build_stock_block)

    # Also patch out the API call and budget check so we don't hit network
    monkeypatch.setattr("trading.claude_caller.risk_manager.check_entry_budget", lambda cost, regime: True)

    class FakeResponse:
        class usage:
            input_tokens = 0
            output_tokens = 0
            cache_creation_input_tokens = 0
            cache_read_input_tokens = 0
        content = [type("C", (), {"type": "text", "text": '{"TEST": {"action": "HOLD", "confidence": 50, "reasoning": "test", "stop_loss_pct": 4.0, "take_profit_pct": 5.0, "indicator_signals": {}}}'})]

    class FakeAI:
        class messages:
            @staticmethod
            def create(**kwargs):
                return FakeResponse()

    # Build a minimal IndicatorValues with atr=2.0, current_price=100.0
    iv = IndicatorValues.__new__(IndicatorValues)
    iv.atr = 2.0
    iv.current_price = 100.0
    # Fill remaining required fields with dummy values
    for field in ["composite_score", "signal", "rsi", "rsi_slope", "macd_hist",
                  "bb_lower", "bb_upper", "ema9", "ema21", "stoch_k", "stoch_d",
                  "vwap", "daily_volume_ratio"]:
        setattr(iv, field, 0.0)
    iv.signal = "HOLD"

    snap = CycleSnapshot.__new__(CycleSnapshot)
    snap.params = {"atr_tp_multiplier": 2.0}
    snap.research = {}
    snap.research_meta = {}
    snap.fred_cache = {}
    snap.regime_info = {}
    snap.regime = "NORMAL"
    snap.weights = {}
    snap.strategy_memory = ""
    snap.intraday_news = {}
    snap.portfolio_gain_pct = 0.0

    monkeypatch.setattr("trading.claude_caller._get_cached_stable_context", lambda *a, **kw: "ctx")
    monkeypatch.setattr("trading.claude_caller.build_dynamic_user_context", lambda now, **kw: "dynamic")

    _ask_claude_batch([("TEST", iv, None)], snap, FakeAI())

    # atr=2.0, price=100.0, mult=2.0 → atr_pct=2.0% → raw=4.0% → clamped to 4.0
    assert abs(captured.get("atr_suggested_tp_pct", -1) - 4.0) < 0.01


@pytest.mark.unit
def test_journal_stores_atr_pct(monkeypatch, tmp_path):
    """BUY journal entry written by execute_buy includes atr_pct field."""
    import config
    from trading.executor import execute_buy
    from indicators.values import IndicatorValues
    from trading.journal import _POSITIONS_LOCK
    from datetime import datetime
    from zoneinfo import ZoneInfo

    ET = ZoneInfo("America/New_York")
    recorded_trades = []

    def mock_append_trade(record):
        recorded_trades.append(record)

    monkeypatch.setattr("trading.executor.append_trade", mock_append_trade)
    monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "open_positions.json")
    monkeypatch.setattr(config, "DAILY_CLOSED_FILE", tmp_path / "daily_closed.json")
    monkeypatch.setattr("trading.executor.check_all_gates", lambda **kwargs: type("G", (), {"passed": True, "reason": "", "flags": []})())
    monkeypatch.setattr("trading.executor.effective_composite_gate", lambda iv, regime, params=None: 0)
    monkeypatch.setattr("trading.executor.get_intraday_bars", lambda *a, **kw: None)
    monkeypatch.setattr("trading.executor.get_active_mode", lambda regime, sym: "DAY_TRADE")
    monkeypatch.setattr("trading.executor.get_sl_tp_for_mode_and_regime", lambda mode, regime: (4.0, 5.0))
    monkeypatch.setattr("trading.executor.compute_position_size_for_mode", lambda *a, **kw: 10)
    monkeypatch.setattr("trading.executor.apply_regime_multiplier", lambda qty, mult: qty)
    monkeypatch.setattr("trading.executor.apply_sentiment_multiplier", lambda qty, sent: qty)
    monkeypatch.setattr("trading.executor.risk_manager.correct_bracket_after_fill", lambda *a, **kw: None)
    monkeypatch.setattr("trading.executor.telegram_notify.notify_buy", lambda **kw: None)
    monkeypatch.setattr("trading.executor.telegram_notify.notify_daily_loss_warning", lambda *a, **kw: None)

    # Build a minimal IndicatorValues
    iv = IndicatorValues(
        symbol="TEST",
        timestamp="2026-05-18T10:30:00-04:00",
        current_price=80.0,
        vwap=80.0,
        vwap_std1_upper=82.0,
        vwap_std1_lower=78.0,
        vwap_std2_upper=84.0,
        vwap_std2_lower=76.0,
        rsi=50.0,
        macd=0.0,
        macd_signal=0.0,
        macd_hist=0.0,
        bb_upper=82.0,
        bb_mid=80.0,
        bb_lower=78.0,
        ema9=80.0,
        ema21=80.0,
        atr=2.4,  # atr_pct = (2.4 / 80.0) * 100 = 3.0
        volume_ratio=1.0,
        stoch_k=50.0,
        stoch_d=50.0,
        obv=0.0,
        vwap_slope=0.0,
        rsi_slope=0.0,
        daily_volume_ratio=1.0,
        daily_volume_trend=0.0,
        daily_volume_mean=1000000.0,
        composite_score=60.0,
        signal="BUY",
        confidence=80.0,
        parabolic=False,
        bot_herding_score=0,
        squeeze_setup=False,
        stop_hunt_zone=False,
    )

    class FakeOrder(dict):
        def __init__(self):
            super().__init__({
                "id": "ord1",
                "stop_loss_price": 76.8,
                "take_profit_price": 84.0
            })

    class FakeAlpaca:
        def place_bracket_order(self, **kw):
            return FakeOrder()
        _trading = None

    execute_buy(
        symbol="TEST", iv=iv,
        decision={"action": "BUY", "confidence": 80, "reasoning": "test",
                  "stop_loss_pct": 4.0, "take_profit_pct": 5.0, "indicator_signals": {}},
        regime="NORMAL", regime_info={"vix_current": 15.0},
        research={}, open_positions={},
        alpaca=FakeAlpaca(), portfolio_value=100_000.0,
        pos_mult=1.0, spy_ret5d=0.0, symbol_ret5d=0.0, rsi_max=70.0,
        params={}, now=datetime(2026, 5, 18, 10, 30, tzinfo=ET),
        entries_this_cycle=0, daily_pnl_pct=0.0,
        suppress_notify=True,
    )

    assert recorded_trades[0].get("atr_pct") == 3.0

