"""
Trading pipeline, cycle context, and budget escalation unit tests.
Run with: pytest tests/test_trading_pipeline.py -v -m unit
"""
import json
import os
from datetime import datetime

import pytest

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

    def test_research_staleness_skip_when_too_old(self, monkeypatch):
        """pipeline.run() returns skipped=True when research > RESEARCH_STALE_BLOCK_MIN."""
        from datetime import timedelta
        import config
        from trading.pipeline import TradingPipeline

        stale_ts = (datetime.now(ET) - timedelta(minutes=config.RESEARCH_STALE_BLOCK_MIN + 10)).isoformat()
        research = {"__meta__": {"timestamp": stale_ts}}

        pipeline = TradingPipeline()
        result = pipeline.run(
            symbols=[], snap=self._snap(research=research),
            ai=None, now=datetime.now(ET), alpaca=None,
            portfolio_value=10000.0, buying_power=5000.0, can_open=True,
        )
        assert result.skipped is True
        assert "stale" in result.reason

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
             patch("trading.pipeline.load_daily_closed", return_value=set()):
            pipeline = TradingPipeline()
            result = pipeline.run(
                symbols=[], snap=self._snap(research=research),
                ai=None, now=datetime.now(ET), alpaca=mock_alpaca,
                portfolio_value=10000.0, buying_power=5000.0, can_open=True,
            )
        assert result.skipped is False

    def test_missing_meta_timestamp_no_crash(self, monkeypatch):
        """pipeline.run() handles missing __meta__.timestamp without raising."""
        from unittest.mock import MagicMock, patch
        from trading.pipeline import TradingPipeline

        research = {"__meta__": {}}  # no timestamp key
        with patch("trading.pipeline.load_open_positions", return_value={}), \
             patch("trading.pipeline.load_daily_closed", return_value=set()):
            pipeline = TradingPipeline()
            result = pipeline.run(
                symbols=[], snap=self._snap(research=research),
                ai=None, now=datetime.now(ET), alpaca=MagicMock(),
                portfolio_value=10000.0, buying_power=5000.0, can_open=True,
            )
        assert result.skipped is False

    def test_malformed_timestamp_no_crash(self, monkeypatch):
        """pipeline.run() handles unparseable timestamp without raising."""
        from unittest.mock import MagicMock, patch
        from trading.pipeline import TradingPipeline

        research = {"__meta__": {"timestamp": "not-a-date"}}
        with patch("trading.pipeline.load_open_positions", return_value={}), \
             patch("trading.pipeline.load_daily_closed", return_value=set()):
            pipeline = TradingPipeline()
            result = pipeline.run(
                symbols=[], snap=self._snap(research=research),
                ai=None, now=datetime.now(ET), alpaca=MagicMock(),
                portfolio_value=10000.0, buying_power=5000.0, can_open=True,
            )
        assert result.skipped is False


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
        import earnings_calendar as ec
        import economic_calendar as ecc
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


@pytest.mark.unit
class TestAgentBudgetRecording:
    """Verify that watch_list_agent and research/analyzer call risk_manager.record_usage()."""

    def _fake_response(self, input_tok=100, output_tok=50, cache_write=200, cache_read=0):
        """Build a minimal mock of the anthropic Messages response."""
        from unittest.mock import MagicMock
        usage = MagicMock()
        usage.input_tokens = input_tok
        usage.output_tokens = output_tok
        usage.cache_creation_input_tokens = cache_write
        usage.cache_read_input_tokens = cache_read
        resp = MagicMock()
        resp.usage = usage
        resp.content = [MagicMock(text='["AAPL", "NVDA"]')]
        return resp

    def test_watch_list_agent_records_budget(self, monkeypatch, tmp_path):
        """run_watch_list_agent records token usage after its Claude call."""
        import types
        from agents import watch_list as watch_list_agent

        fake_resp = self._fake_response(input_tok=300, output_tok=80, cache_write=500)

        # Patch Alpaca, file paths, and the Claude client
        fake_ticker = tmp_path / "tickers.txt"
        fake_ticker.write_text("AAPL\n")
        monkeypatch.setattr("config.DAILY_TRADEABLE_FILE", tmp_path / "nonexistent.txt")
        monkeypatch.setattr("config.TICKERS_FILE", fake_ticker)
        monkeypatch.setattr("config.WATCH_LIST_FILE", tmp_path / "watch_list.txt")
        monkeypatch.setattr("config.STRATEGY_MEMORY_FILE", tmp_path / "strategy_memory.md")
        monkeypatch.setattr("config.WATCH_LIST_MAX", 15)

        from unittest.mock import MagicMock, patch
        mock_alpaca = MagicMock()
        mock_alpaca.get_daily_bars.return_value = None  # _score_stock returns None → scored=[]

        recorded = {}
        def fake_record(input_tokens, output_tokens, cache_write_tokens=0, cache_read_tokens=0, **kwargs):
            recorded["input"] = input_tokens
            recorded["output"] = output_tokens
            recorded["cache_write"] = cache_write_tokens

        with patch("agents.watch_list.AlpacaClient", return_value=mock_alpaca), \
             patch("anthropic.Anthropic") as MockAI, \
             patch("trading.risk_manager.record_usage", side_effect=fake_record):
            MockAI.return_value.messages.create.return_value = fake_resp
            # scored list is empty so Claude picks from fallback — still calls messages.create
            # Force scored to have one entry so the Claude call is reached
            monkeypatch.setattr(watch_list_agent, "_load_universe", lambda: ["AAPL"])
            mock_score = {"symbol": "AAPL", "price": 100.0, "vol_spike": 2.0, "atr_pct": 2.0, "gap_pct": 0.5}
            monkeypatch.setattr(watch_list_agent, "_score_stock", lambda sym, client: mock_score)
            watch_list_agent.run_watch_list_agent()

        assert recorded.get("input") == 300
        assert recorded.get("output") == 80
        assert recorded.get("cache_write") == 500

    def test_research_analyzer_records_budget(self, monkeypatch):
        """_analyze_stock records token usage after its Claude call."""
        import types
        from unittest.mock import MagicMock, patch
        from research.analyzer import _analyze_stock

        usage = MagicMock()
        usage.input_tokens = 400
        usage.output_tokens = 60
        usage.cache_creation_input_tokens = 800
        usage.cache_read_input_tokens = 0
        resp = MagicMock()
        resp.usage = usage
        resp.content = [MagicMock(text='{"news_sentiment_score": 10, "news_sentiment_label": "BULLISH", "social_sentiment_score": 5, "social_sentiment_label": "BULLISH", "combined_sentiment_score": 8, "combined_sentiment_label": "BULLISH", "key_catalyst": null, "risk_flag": null, "divergence_alert": null, "summary": "ok", "strategy": "hold"}')]

        ai = MagicMock()
        ai.messages.create.return_value = resp

        recorded = {}
        def fake_record(input_tokens, output_tokens, cache_write_tokens=0, cache_read_tokens=0, **kwargs):
            recorded["input"] = input_tokens
            recorded["output"] = output_tokens
            recorded["cache_write"] = cache_write_tokens

        with patch("trading.risk_manager.record_usage", side_effect=fake_record):
            _analyze_stock("AAPL", [], "ctx", {}, {}, {}, ai, [])

        assert recorded.get("input") == 400
        assert recorded.get("output") == 60
        assert recorded.get("cache_write") == 800


# ── Budget escalation notification ───────────────────────────────────────────

@pytest.mark.unit
class TestBudgetEscalationNotify:
    def _patch_no_token(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")

    def _patch_fake_token(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "fake_token")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "12345")

    def test_notify_budget_escalation_calls_send(self, monkeypatch):
        self._patch_fake_token(monkeypatch)
        from unittest.mock import patch
        import telegram_notify
        with patch("requests.post") as mock_post:
            telegram_notify.notify_budget_escalation(0.25, 1.50)
            mock_post.assert_called_once()
            text = mock_post.call_args[1]["json"]["text"]
            assert "escalated" in text.lower()

    def test_notify_budget_escalation_swallows_exceptions(self, monkeypatch):
        self._patch_fake_token(monkeypatch)
        from unittest.mock import patch
        import telegram_notify
        with patch("requests.post", side_effect=Exception("network error")):
            telegram_notify.notify_budget_escalation(0.25, 1.50)
            # Must not raise — function should return normally
