"""
Trading cycle unit tests — dead-day alert diagnostics and catchup tracker seeding.
Run with: pytest tests/test_trading_cycle.py -v -m unit
"""
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

import config
from utils.constants import ET


# ── Dead-day alert diagnostic message ────────────────────────────────────────

@pytest.mark.unit
class TestDeadDayAlertMessage:
    """Dead-day alert fires after DEAD_DAY_ALERT_CYCLES consecutive zero-candidate
    cycles and the message must include can_open / stale / MEDIUM floor context."""

    def _make_guard(self, can_open=True, stale_sources=None, penalty=0):
        from utils.data_health import StalenessGuard
        return StalenessGuard(
            composite_penalty=penalty,
            auto_execute_allowed=True,
            t2_allowed=True,
            swing_fallback_regime=None,
            can_open=can_open,
            claude_warnings=(),
            stale_sources=frozenset(stale_sources or []),
        )

    def test_dead_day_alert_contains_can_open_and_stale_and_floor(self, monkeypatch):
        """After threshold cycles with 0 candidates, Telegram message includes
        can_open status, stale source list, and the MEDIUM composite floor."""
        from unittest.mock import patch, MagicMock
        import trading.cycle as cycle_mod

        today_str = "2026-05-27"
        threshold = config.DEAD_DAY_ALERT_CYCLES
        monkeypatch.setitem(cycle_mod._zero_pass, "date", today_str)
        monkeypatch.setitem(cycle_mod._zero_pass, "count", threshold - 1)
        monkeypatch.setitem(cycle_mod._zero_pass, "alerted", False)

        guard = self._make_guard(
            can_open=True,
            stale_sources=["regime", "research"],
            penalty=10,
        )

        account = MagicMock()
        account.portfolio_value = "100000"
        account.buying_power    = "50000"
        account.last_equity     = "100000"

        cycle_result = MagicMock()
        cycle_result.candidates = 0
        cycle_result.entries    = 0
        cycle_result.closes     = 0
        cycle_result.skipped    = False

        sent_messages = []

        with patch("trading.cycle.AlpacaClient") as mock_alpaca_cls, \
             patch("utils.data_health.get_staleness_guard", return_value=guard), \
             patch("trading.cycle.load_macro_regime",
                   return_value={"regime": "NORMAL", "flash_crash_active": False}), \
             patch("trading.cycle.load_trading_params",
                   return_value={"entry_start_hour": 10, "entry_end_hour": 15,
                                 "high_vol_position_mult": 0.5, "rsi_buy_max_high_vol": 60}), \
             patch("trading.cycle.load_watch_list", return_value=["AAPL", "MSFT"]), \
             patch("trading.cycle.build_cycle_snapshot", return_value=MagicMock()), \
             patch("trading.cycle.TradingPipeline") as mock_pipeline_cls, \
             patch("trading.cycle.telegram_notify._send",
                   side_effect=lambda m: sent_messages.append(m)), \
             patch("trading.cycle.telegram_notify.notify_alert"), \
             patch("trading.cycle._run_intraday_retries"), \
             patch("anthropic.Anthropic"), \
             patch("trading.cycle.datetime") as mock_dt:

            mock_alpaca_cls.return_value.get_account.return_value = account
            mock_pipeline_cls.return_value.run.return_value = cycle_result

            # Freeze time to 11:00 inside entry window
            fake_now = datetime(2026, 5, 27, 11, 0, 0, tzinfo=ET)
            mock_dt.now.return_value = fake_now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            cycle_mod.run_trading_cycle()

        assert len(sent_messages) == 1, "Expected exactly one Telegram message"
        msg = sent_messages[0]
        assert "can_open" in msg, f"'can_open' missing from: {msg}"
        assert "stale" in msg, f"'stale' missing from: {msg}"
        assert "MEDIUM floor" in msg, f"'MEDIUM floor' missing from: {msg}"


# ── Catchup tracker seeding ───────────────────────────────────────────────────

@pytest.mark.unit
class TestCatchupTrackerSeeding:
    """When catchup skips a fresh file, it must seed the tracker so subsequent
    calls to get_staleness_guard() do not falsely flag that source as stale.

    Regression for: post-restart phantom [STALE:regime,research] tags caused by
    _run_intraday_retries() creating tracker entries before record_success() is
    ever called in the new session.
    """

    def _fresh_regime_file(self, tmp_path):
        from utils.io_utils import save_json
        path = tmp_path / "macro_regime.json"
        save_json(path, {"regime": "BULL_TREND"})
        return path

    def _fresh_research_file(self, tmp_path):
        from utils.io_utils import save_json
        path = tmp_path / "research_report.json"
        save_json(path, {"__meta__": {"timestamp": datetime.now(ET).isoformat()}})
        return path

    def test_catchup_seeds_tracker_for_fresh_regime(self, tmp_path, monkeypatch):
        """After catchup skips fresh regime, tracker.stale_sources() excludes regime."""
        from utils.data_health import DataHealthTracker
        tracker = DataHealthTracker()

        regime_path = self._fresh_regime_file(tmp_path)
        monkeypatch.setattr(config, "MACRO_REGIME_FILE", regime_path)

        from utils import catchup
        catchup._seed_tracker_if_fresh(tracker, "regime", regime_path)

        assert "regime" not in tracker.stale_sources()
        assert tracker.age_minutes("regime") < 5  # just written — should be fresh

    def test_catchup_seeds_tracker_for_fresh_research(self, tmp_path, monkeypatch):
        """After catchup skips fresh research, tracker.stale_sources() excludes research."""
        from utils.data_health import DataHealthTracker
        tracker = DataHealthTracker()

        research_path = self._fresh_research_file(tmp_path)
        monkeypatch.setattr(config, "RESEARCH_REPORT_FILE", research_path)

        from utils import catchup
        catchup._seed_tracker_if_fresh(tracker, "research", research_path)

        assert "research" not in tracker.stale_sources()

    def test_seed_noop_for_missing_file(self, tmp_path):
        """Missing file must not crash or add tracker entry."""
        from utils.data_health import DataHealthTracker
        tracker = DataHealthTracker()
        missing = tmp_path / "does_not_exist.json"

        from utils import catchup
        catchup._seed_tracker_if_fresh(tracker, "regime", missing)

        # No entry created — stale_sources stays clean
        assert "regime" not in tracker.stale_sources()


# ── Pipeline can_open=False log message ──────────────────────────────────────

@pytest.mark.unit
class TestCanOpenLogMessage:
    """The can_open=False log message must not say 'not open yet' since that
    phrase implies pre-window; the condition also covers post-hard-close."""

    def test_can_open_false_log_does_not_say_not_open_yet(self, caplog):
        """When can_open=False, the pipeline log must not emit 'not open yet'."""
        import logging
        from trading.pipeline import TradingPipeline
        from trading.cycle_context import CycleSnapshot
        from utils.data_health import FRESH_GUARD

        snap = CycleSnapshot(
            regime_info={"regime": "NORMAL", "macro_health_score": 0},
            regime="NORMAL", fred_cache={}, research={},
            research_meta={"macro_shock_score": 0},
            strategy_memory="", weights={},
            params={
                "rsi_buy_max": 72, "rsi_buy_max_high_vol": 65,
                "high_vol_position_mult": 0.5,
                "entry_start_hour": 10, "entry_end_hour": 15,
            },
            portfolio_gain_pct=0.0, intraday_news={},
            staleness_guard=FRESH_GUARD,
        )

        from tests.test_signals import make_full_iv
        iv = make_full_iv(symbol="AAPL", composite_score=70.0)
        fetch_result = ("AAPL", iv, None, 0.5)

        with patch("trading.pipeline._fetch_symbol_data", return_value=fetch_result), \
             patch("trading.pipeline._evaluate_thesis_exits", return_value=0), \
             patch("trading.pipeline.load_open_positions", return_value={}), \
             patch("trading.pipeline.is_catalyst_active", return_value=False), \
             caplog.at_level(logging.INFO, logger="trading.pipeline"):
            TradingPipeline().run(
                symbols=["AAPL"], snap=snap,
                ai=MagicMock(), now=datetime(2026, 5, 27, 14, 10, tzinfo=ET),
                alpaca=MagicMock(), portfolio_value=100_000, buying_power=50_000,
                can_open=False,
            )

        can_open_logs = [r.message for r in caplog.records if "can_open=False" in r.message]
        assert can_open_logs, "Expected at least one can_open=False log line"
        for msg in can_open_logs:
            assert "not open yet" not in msg, f"Misleading 'not open yet' in: {msg}"
