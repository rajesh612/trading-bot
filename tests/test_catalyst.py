import pytest
from unittest.mock import patch
from datetime import datetime, timedelta
from utils.constants import ET


def _make_snap(post_event_window=None, sector_etf_cache=None, catalyst_surprise_score=0.0):
    from trading.cycle_context import CycleSnapshot
    return CycleSnapshot(
        regime_info={"regime": "NORMAL"},
        regime="NORMAL",
        fred_cache={},
        research={},
        research_meta={},
        strategy_memory="",
        weights={},
        params={},
        portfolio_gain_pct=0.0,
        post_event_window=post_event_window,
        sector_etf_cache=sector_etf_cache or {},
        catalyst_surprise_score=catalyst_surprise_score,
    )


class TestIsCatalystActive:
    @pytest.mark.unit
    def test_none_window_returns_false(self):
        from signals.entry_gates import is_catalyst_active
        snap = _make_snap(post_event_window=None)
        assert not is_catalyst_active("AAPL", snap)

    @pytest.mark.unit
    def test_too_soon_after_event_returns_false(self):
        from signals.entry_gates import is_catalyst_active
        import config
        event = {"type": "CPI", "impact": "HIGH"}
        snap = _make_snap(
            post_event_window=(event, config.CATALYST_SETTLE_MINUTES - 1),
            sector_etf_cache={"XLK": 1.0},
        )
        assert not is_catalyst_active("AAPL", snap)

    @pytest.mark.unit
    def test_etf_move_too_small_returns_false(self):
        from signals.entry_gates import is_catalyst_active
        import config
        event = {"type": "CPI", "impact": "HIGH"}
        snap = _make_snap(
            post_event_window=(event, config.CATALYST_SETTLE_MINUTES + 10),
            sector_etf_cache={"XLK": 0.1},  # below CATALYST_ETF_MIN_MOVE_PCT
        )
        with patch("signals.entry_gates.get_sub_sector_etf", return_value="XLK"):
            assert not is_catalyst_active("AAPL", snap)

    @pytest.mark.unit
    def test_valid_catalyst_returns_true(self):
        from signals.entry_gates import is_catalyst_active
        import config
        event = {"type": "CPI", "impact": "HIGH"}
        snap = _make_snap(
            post_event_window=(event, config.CATALYST_SETTLE_MINUTES + 30),
            sector_etf_cache={"XLK": 0.8},
            catalyst_surprise_score=0.3,
        )
        with patch("signals.entry_gates.get_sub_sector_etf", return_value="XLK"):
            assert is_catalyst_active("AAPL", snap)

    @pytest.mark.unit
    def test_low_surprise_score_blocks(self):
        from signals.entry_gates import is_catalyst_active
        import config
        event = {"type": "CPI", "impact": "HIGH"}
        snap = _make_snap(
            post_event_window=(event, config.CATALYST_SETTLE_MINUTES + 30),
            sector_etf_cache={"XLK": 0.8},
            catalyst_surprise_score=-0.5,  # below CATALYST_MIN_SURPRISE_SCORE = -0.1
        )
        with patch("signals.entry_gates.get_sub_sector_etf", return_value="XLK"):
            assert not is_catalyst_active("AAPL", snap)


class TestGetPostEventWindow:
    @pytest.mark.unit
    def test_returns_none_when_no_events(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "ECONOMIC_CALENDAR_FILE", str(tmp_path / "econ.json"))
        from utils.io_utils import save_json
        save_json(str(tmp_path / "econ.json"), {"events": []})
        from macro.economic_calendar import get_post_event_window
        result = get_post_event_window(datetime.now(ET))
        assert result is None

    @pytest.mark.unit
    def test_returns_event_within_window(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "ECONOMIC_CALENDAR_FILE", str(tmp_path / "econ.json"))
        from utils.io_utils import save_json
        now = datetime.now(ET)
        event_time = (now - timedelta(minutes=45)).isoformat()
        save_json(str(tmp_path / "econ.json"), {
            "events": [{"type": "CPI", "impact": "HIGH", "time": event_time}]
        })
        from macro.economic_calendar import get_post_event_window
        result = get_post_event_window(now)
        assert result is not None
        event_dict, elapsed = result
        assert 40 <= elapsed <= 50
