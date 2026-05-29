"""Tests for trading/monitor.py — position monitoring and exit strategies."""
from datetime import datetime

import pytest


@pytest.mark.unit
class TestMiddayProfitFloor:
    """run_position_monitor locks gains ≥1% for DAY_TRADE positions after 2 PM in NORMAL regime."""

    def _make_position(self, entry_time_str: str, mode_at_entry: str = "DAY_TRADE",
                       is_swing: bool = False) -> dict:
        return {
            "entry_price": 100.0,
            "qty": 10,
            "stop_loss_pct": 4.0,
            "take_profit_pct": 5.0,
            "entry_time": entry_time_str,
            "mode_at_entry": mode_at_entry,
            "is_swing": is_swing,
        }

    def _run_monitor(self, monkeypatch, *, regime: str, now_hour: int, now_minute: int,
                     pnl_pct: float, hold_hours: float, is_swing: bool = False,
                     mode_at_entry: str = "DAY_TRADE"):
        """Shared test driver — returns the mock close_position."""
        from unittest.mock import MagicMock, patch
        from zoneinfo import ZoneInfo

        ET_zone = ZoneInfo("America/New_York")
        now = datetime(2026, 5, 26, now_hour, now_minute, 0, tzinfo=ET_zone)
        from datetime import timedelta
        entry_dt = now - timedelta(hours=hold_hours)
        entry_time_str = entry_dt.isoformat()

        entry_price = 100.0
        current_price = entry_price * (1 + pnl_pct / 100)

        position = self._make_position(entry_time_str, mode_at_entry=mode_at_entry,
                                       is_swing=is_swing)
        position["entry_price"] = entry_price

        mock_alpaca = MagicMock()
        mock_alpaca.get_positions.return_value = {"MSFT": MagicMock()}
        mock_alpaca.get_latest_price.return_value = current_price

        mock_close = MagicMock()

        with patch("trading.monitor.load_open_positions", return_value={"MSFT": position}), \
             patch("trading.monitor.AlpacaClient", return_value=mock_alpaca), \
             patch("trading.monitor.load_macro_regime", return_value={"regime": regime}), \
             patch("trading.monitor.close_position", mock_close), \
             patch("trading.monitor.datetime") as mock_dt, \
             patch("trading.monitor.flush_profit_lock_updates"), \
             patch("trading.monitor._apply_profit_lock", side_effect=lambda pos, _: pos), \
             patch("trading.monitor._try_compute_iv", return_value=None), \
             patch("trading.monitor._check_drawdown_alerts"), \
             patch("utils.data_health.get_staleness_guard", side_effect=Exception("skip")):
            mock_dt.now.return_value = now
            mock_dt.fromisoformat.side_effect = datetime.fromisoformat
            from trading.monitor import run_position_monitor
            run_position_monitor()

        return mock_close

    def test_triggers_on_qualifying_position(self, monkeypatch):
        """NORMAL regime, +1.5% gain, held 2.5h, 2:30 PM → midday_profit_floor fires."""
        mock_close = self._run_monitor(
            monkeypatch,
            regime="NORMAL",
            now_hour=14,
            now_minute=30,
            pnl_pct=1.5,
            hold_hours=2.5,
        )
        mock_close.assert_called_once()
        _, _, _, _, _, reason = mock_close.call_args[0]
        assert reason == "midday_profit_floor"

    def test_does_not_trigger_before_exit_hour(self, monkeypatch):
        """1:30 PM (before MIDDAY_EXIT_HOUR=14) — must NOT fire."""
        mock_close = self._run_monitor(
            monkeypatch,
            regime="NORMAL",
            now_hour=13,
            now_minute=30,
            pnl_pct=1.5,
            hold_hours=2.5,
        )
        mock_close.assert_not_called()

    def test_does_not_trigger_for_swing_positions(self, monkeypatch):
        """is_swing=True — midday profit-floor is day-trade only, must NOT fire."""
        mock_close = self._run_monitor(
            monkeypatch,
            regime="NORMAL",
            now_hour=14,
            now_minute=30,
            pnl_pct=1.5,
            hold_hours=2.5,
            is_swing=True,
        )
        mock_close.assert_not_called()

    def test_does_not_trigger_after_max_hour(self, monkeypatch):
        """Should NOT fire at or after MIDDAY_EXIT_MAX_HOUR (3 PM)."""
        import config
        import trading.monitor as mon
        monkeypatch.setattr(config, "MIDDAY_PROFIT_FLOOR_PCT", 1.0)
        monkeypatch.setattr(config, "MIDDAY_EXIT_HOUR", 14)
        monkeypatch.setattr(config, "MIDDAY_EXIT_MAX_HOUR", 15)
        monkeypatch.setattr(config, "MIDDAY_EXIT_MIN_HOLD_H", 2.0)

        # now = 3:00 PM, position up 1.5%, held 2.5h — qualifies on everything except hour
        mock_close = self._run_monitor(
            monkeypatch,
            regime="NORMAL",
            now_hour=15,
            now_minute=0,
            pnl_pct=1.5,
            hold_hours=2.5,
        )
        mock_close.assert_not_called()


@pytest.mark.unit
def test_position_monitor_skips_t2_when_guard_disallows(monkeypatch, tmp_path):
    """run_position_monitor skips T2 check when guard.t2_allowed is False."""
    import config
    from unittest.mock import patch, MagicMock
    from utils.data_health import StalenessGuard

    stale_guard = StalenessGuard(
        composite_penalty=10, auto_execute_allowed=False,
        t2_allowed=False, swing_fallback_regime="NORMAL",
        can_open=True, claude_warnings=(), stale_sources=frozenset({"regime"}),
    )

    monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "open.json")
    monkeypatch.setattr(config, "SWING_POSITIONS_FILE", tmp_path / "swing.json")

    # Non-empty positions so monitor doesn't early-return before reaching T2 logic
    fake_positions = {
        "AAPL": {
            "qty": 10, "entry_price": 150.0,
            "stop_loss_pct": 4.0, "take_profit_pct": 5.0,
        }
    }

    t2_called = []

    mock_alpaca = MagicMock()
    mock_alpaca.get_positions.return_value = {"AAPL": {"qty": 10}}
    mock_alpaca.get_latest_price.return_value = 152.0
    mock_alpaca.get_portfolio_value.return_value = 100_000.0

    with patch("trading.monitor.load_macro_regime",
               return_value={"regime": "NORMAL", "_stale": True}), \
         patch("utils.data_health.get_staleness_guard", return_value=stale_guard), \
         patch("trading.monitor.load_open_positions", return_value=fake_positions), \
         patch("trading.monitor._check_tranche2_triggers",
               side_effect=lambda *a, **kw: t2_called.append(True)), \
         patch("trading.monitor.flush_profit_lock_updates"), \
         patch("trading.monitor._check_drawdown_alerts"), \
         patch("trading.monitor.AlpacaClient", return_value=mock_alpaca):
        from trading.monitor import run_position_monitor
        run_position_monitor()

    assert len(t2_called) == 0, f"T2 should not fire when guard.t2_allowed is False, called {len(t2_called)} times"


@pytest.mark.unit
def test_evaluate_eod_holds_uses_normal_params_when_regime_stale(monkeypatch, tmp_path):
    """evaluate_eod_holds uses NORMAL swing params when guard.swing_fallback_regime is set."""
    import config
    from unittest.mock import patch, MagicMock
    from utils.data_health import StalenessGuard

    stale_guard = StalenessGuard(
        composite_penalty=10, auto_execute_allowed=False,
        t2_allowed=False, swing_fallback_regime="NORMAL",
        can_open=True, claude_warnings=(), stale_sources=frozenset({"regime"}),
    )

    captured_regime = []

    def fake_get_swing_params(regime):
        captured_regime.append(regime)
        from utils.regime_params import SwingParams
        return SwingParams(False)

    monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "open.json")
    monkeypatch.setattr(config, "SWING_POSITIONS_FILE", tmp_path / "swing.json")

    with patch("trading.swing_manager.load_macro_regime",
               return_value={"regime": "BULL_TREND", "_stale": True}), \
         patch("utils.data_health.get_staleness_guard", return_value=stale_guard), \
         patch("trading.swing_manager.get_swing_params", side_effect=fake_get_swing_params), \
         patch("trading.swing_manager.load_open_positions", return_value={}):
        from trading.swing_manager import evaluate_eod_holds
        evaluate_eod_holds()

    assert "NORMAL" in captured_regime, (
        f"Expected NORMAL (fallback), got {captured_regime}"
    )


# ── sectors: GICS sub-sector ETF lookup ──────────────────────────────────────

@pytest.mark.unit
class TestSubSectorEtf:
    def test_nvidia_returns_soxx(self):
        import sectors
        result = sectors.get_sub_sector_etf("NVDA")
        assert result == "SOXX"

    def test_crwd_returns_cibr(self):
        import sectors
        result = sectors.get_sub_sector_etf("CRWD")
        assert result == "CIBR"

    def test_unknown_symbol_falls_back_to_sector_etf(self):
        import sectors
        result = sectors.get_sub_sector_etf("ZZZZ_UNKNOWN")
        assert result == "OTHER"

    def test_symbol_without_sub_etf_falls_back_to_sector_etf(self):
        import sectors
        # JPM is XLF — no sub_etf, fallback to sector
        result = sectors.get_sub_sector_etf("JPM")
        assert result == sectors.get_sector_etf("JPM")

    def test_existing_get_sector_etf_unchanged(self):
        import sectors
        assert sectors.get_sector_etf("NVDA") == "XLK"
