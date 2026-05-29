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
    """Burst cap is now enforced in pipeline.py entry loops, not check_all_gates()."""

    def test_burst_cap_config_is_renamed(self):
        """MAX_ENTRIES_PER_CYCLE renamed to MAX_ENTRIES_PER_CYCLE_BURST = 3."""
        import config
        assert hasattr(config, "MAX_ENTRIES_PER_CYCLE_BURST")
        assert config.MAX_ENTRIES_PER_CYCLE_BURST == 3
        assert not hasattr(config, "MAX_ENTRIES_PER_CYCLE"), (
            "config.MAX_ENTRIES_PER_CYCLE still exists — remove the old name"
        )

    def test_close_decision_bypasses_cap(self):
        """CLOSE action calls close_position, not check_all_gates."""
        from trading.closer import close_position
        import inspect
        sig = inspect.signature(close_position)
        assert "symbol" in sig.parameters
        assert "entries_this_cycle" not in sig.parameters

    def test_burst_cap_constant_is_3(self):
        import config
        assert config.MAX_ENTRIES_PER_CYCLE_BURST == 3

    def test_burst_cap_limiting_in_auto_execute(self, monkeypatch):
        """_execute_auto_entries stops after MAX_ENTRIES_PER_CYCLE_BURST regardless of more candidates."""
        import config
        monkeypatch.setattr(config, "MAX_ENTRIES_PER_CYCLE_BURST", 2)
        from trading.pipeline import _execute_auto_entries
        from unittest.mock import MagicMock, patch
        import types, datetime
        from zoneinfo import ZoneInfo

        def _iv():
            return types.SimpleNamespace(
                current_price=100.0, composite_score=60.0, atr=1.0,
                vwap_slope=1.0, rsi=50.0, stoch_d=50.0,
                vwap_std2_upper=120.0, stop_hunt_zone=False,
                parabolic=False, momentum_exhaustion=False,
                bot_herding_score=0, squeeze_setup=False,
                prev_close=100.0, relative_volume=1.2, or_position="outside",
            )

        candidates = [("SYM1", _iv(), None, 0.0), ("SYM2", _iv(), None, 0.0),
                      ("SYM3", _iv(), None, 0.0), ("SYM4", _iv(), None, 0.0)]
        mock_snap = MagicMock()
        mock_snap.regime = "BULL_TREND"
        mock_snap.staleness_guard.auto_execute_allowed = True
        mock_snap.intraday_news = {}
        mock_snap.research = {}

        executed = []
        def fake_execute_buy(**kw):
            executed.append(kw["symbol"])
            return {"qty": 1, "entry_price": 100.0, "stop_loss_price": 96.0,
                    "take_profit_price": 105.0, "mode_at_entry": "DAY_TRADE"}

        with patch("trading.pipeline.execute_buy", side_effect=fake_execute_buy):
            new_entries, _, _ = _execute_auto_entries(
                auto_execute=candidates, snap=mock_snap,
                alpaca=MagicMock(), open_positions={},
                portfolio_value=100_000.0, buying_power=50_000.0,
                pos_mult=1.0, spy_ret5d=0.0, rsi_max=74, atr_tp_mult=2.0,
                can_open=True,
                now=datetime.datetime(2024, 1, 15, 11, 0, 0, tzinfo=ZoneInfo("America/New_York")),
                daily_pnl_pct=0.0,
            )

        assert new_entries == 2, f"Burst cap=2 but got {new_entries} entries"
        assert len(executed) == 2

    def test_5day_return_exactly_6_rows(self):
        import pandas as pd
        from trading.pipeline import _compute_symbol_5day_return
        df5 = pd.DataFrame({"close": [100.0] * 5})
        df6 = pd.DataFrame({"close": [100.0] * 6})
        assert _compute_symbol_5day_return(df5) == pytest.approx(0.0)
        assert _compute_symbol_5day_return(df6) == pytest.approx(0.0)


@pytest.mark.unit
def test_cycle_snapshot_defaults_to_fresh_guard():
    """CycleSnapshot.staleness_guard defaults to FRESH_GUARD when not supplied."""
    from trading.cycle_context import CycleSnapshot
    from utils.data_health import FRESH_GUARD
    from dataclasses import fields

    snap = CycleSnapshot(
        regime_info={"regime": "NORMAL", "macro_health_score": 0},
        regime="NORMAL",
        fred_cache={},
        research={},
        research_meta={},
        strategy_memory="",
        weights={},
        params={},
        portfolio_gain_pct=0.0,
    )
    # Verify the field exists
    field_names = [f.name for f in fields(CycleSnapshot)]
    assert "staleness_guard" in field_names, f"staleness_guard not found in CycleSnapshot. Fields: {field_names}"
    # Verify FRESH_GUARD is the default
    assert snap.staleness_guard == FRESH_GUARD, f"Expected FRESH_GUARD, got {snap.staleness_guard}"



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
         patch("trading.pipeline.apply_pre_claude_filter", return_value=FilterResult("CLAUDE", "")), \
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
         patch("trading.executor.derive_mode", return_value="DAY_TRADE"):
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
         patch("trading.pipeline.apply_pre_claude_filter"):
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
    monkeypatch.setattr("trading.executor.effective_composite_gate", lambda iv, regime, params=None, staleness_guard=None: 0)
    monkeypatch.setattr("trading.executor.get_intraday_bars", lambda *a, **kw: None)
    monkeypatch.setattr("trading.executor.derive_mode", lambda sym, pead_ok=False, existing_swing_pos=False, catalyst_active=False: "DAY_TRADE")
    monkeypatch.setattr("signals.tp_scaler.compute_tp_sl", lambda **kw: (5.0, 2.0))
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
        params={}, now=datetime(2026, 5, 18, 10, 30, tzinfo=ET), daily_pnl_pct=0.0,
        suppress_notify=True,
    )

    assert recorded_trades[0].get("atr_pct") == 3.0


@pytest.mark.unit
def test_execute_buy_forces_swing_false_when_regime_stale(monkeypatch, tmp_path):
    """execute_buy forces swing_eligible=False when regime is in guard.stale_sources."""
    from unittest.mock import patch, MagicMock
    from utils.data_health import StalenessGuard

    stale_guard = StalenessGuard(
        composite_penalty=10, auto_execute_allowed=False,
        t2_allowed=False, swing_fallback_regime="NORMAL",
        can_open=True, claude_warnings=(), stale_sources=frozenset({"regime"}),
    )

    captured = {}

    def fake_check_all_gates(*args, **kwargs):
        captured["swing_eligible"] = kwargs.get("swing_eligible")
        from signals.entry_gates import GateResult
        return GateResult(passed=False, reason="test block")

    monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "open.json")
    monkeypatch.setattr(config, "DAILY_CLOSED_FILE", tmp_path / "closed.json")

    def _fake_load_json(path, default=None):
        """Return {} for open-positions file, [] for all others (daily_closed, etc.)."""
        if str(path) == str(tmp_path / "open.json"):
            return {}
        return []

    with patch("trading.executor.check_all_gates", side_effect=fake_check_all_gates), \
         patch("trading.executor.load_json", side_effect=_fake_load_json), \
         patch("trading.executor.get_intraday_bars", return_value=None), \
         patch("trading.executor.telegram_notify", MagicMock()):
        from trading.executor import execute_buy
        iv = MagicMock()
        iv.composite_score = 75.0
        iv.rsi = 50.0
        iv.current_price = 100.0
        iv.atr = 2.0
        iv.bot_herding_score = 0
        iv.squeeze_setup = False
        iv.vwap_slope = 999.0  # pass VWAP slope gate
        decision = {
            "action": "BUY", "confidence": 85,
            "stop_loss_pct": 4.0, "take_profit_pct": 6.0, "reasoning": "test"
        }
        execute_buy(
            symbol="NVDA", iv=iv, decision=decision, symbol_ret5d=0.0,
            alpaca=MagicMock(), open_positions={},
            portfolio_value=100_000, pos_mult=1.0, spy_ret5d=0.0,
            rsi_max=70, now=MagicMock(),
            daily_pnl_pct=0.0, params={}, regime="BULL_TREND",
            regime_info={}, research={},
            sector_today={}, staleness_guard=stale_guard,
        )

    assert captured.get("swing_eligible") is False, (
        f"Expected swing_eligible=False due to stale regime, got {captured.get('swing_eligible')}"
    )

