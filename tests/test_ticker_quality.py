"""
Tests for utils/ticker_quality.py — TickerQuality dataclass + tier scoring.
"""
import pytest
from unittest.mock import MagicMock, patch
import config
from utils.ticker_quality import TickerQuality, compute_ticker_quality, _score_to_tier, derive_mode

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Tier boundary tests
# ---------------------------------------------------------------------------

class TestTierBoundaries:
    def test_low_tier_zero(self):
        assert _score_to_tier(0) == "LOW"

    def test_low_tier_top(self):
        assert _score_to_tier(34) == "LOW"

    def test_medium_tier_bottom(self):
        assert _score_to_tier(35) == "MEDIUM"

    def test_medium_tier_top(self):
        assert _score_to_tier(59) == "MEDIUM"

    def test_high_tier_bottom(self):
        assert _score_to_tier(60) == "HIGH"

    def test_high_tier_top(self):
        assert _score_to_tier(79) == "HIGH"

    def test_conviction_tier_bottom(self):
        assert _score_to_tier(80) == "CONVICTION"

    def test_conviction_tier_max(self):
        assert _score_to_tier(100) == "CONVICTION"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_iv(composite=50.0, rsi=55.0, relative_volume=1.2):
    iv = MagicMock()
    iv.composite_score = composite
    iv.rsi = rsi
    iv.relative_volume = relative_volume
    iv.current_price = 100.0
    iv.atr = 2.0
    return iv


def _make_snap(regime="NORMAL", sentiment=30, intra_sent=25, research=None):
    snap = MagicMock()
    snap.regime = regime
    snap.research = research or {"AAPL": {"sentiment_score": sentiment}}
    snap.intraday_news = {"AAPL": {"sentiment_score": intra_sent, "shock_score": 0}}
    snap.params = {}
    return snap


# ---------------------------------------------------------------------------
# compute_ticker_quality tests
# ---------------------------------------------------------------------------

class TestComputeTickerQuality:
    @patch("utils.ticker_quality.get_entry_params")
    def test_medium_tier_standard_signals(self, mock_ep, monkeypatch):
        monkeypatch.setattr(config, "COMPOSITE_MIN_NORMAL", 50)
        mock_ep.return_value = MagicMock(composite_min=50)
        iv = _make_iv(composite=55.0)
        snap = _make_snap(sentiment=35, intra_sent=30)
        result = compute_ticker_quality(
            "AAPL", iv, snap, mode="DAY_TRADE",
            sub_etf_chg=0.5, earnings_clear_days=20,
            pead_ok=False, pead_score=0, event_active=False,
        )
        assert result.tier == "MEDIUM"
        assert 35 <= result.score <= 59
        assert isinstance(result.unlocks, frozenset)
        assert isinstance(result.quality_gaps, tuple)

    @patch("utils.ticker_quality.get_entry_params")
    def test_conviction_with_pead_and_strong_sentiment(self, mock_ep, monkeypatch):
        monkeypatch.setattr(config, "COMPOSITE_MIN_NORMAL", 50)
        mock_ep.return_value = MagicMock(composite_min=50)
        iv = _make_iv(composite=75.0)
        snap = MagicMock()
        snap.regime = "NORMAL"
        snap.research = {"NVDA": {"sentiment_score": 70}}
        snap.intraday_news = {"NVDA": {"sentiment_score": 65, "shock_score": 0}}
        result = compute_ticker_quality(
            "NVDA", iv, snap, mode="PEAD",
            sub_etf_chg=2.5, earnings_clear_days=35,
            pead_ok=True, pead_score=85, event_active=False,
        )
        assert result.tier == "CONVICTION"
        assert result.score >= 80

    @patch("utils.ticker_quality.get_entry_params")
    def test_low_quality_gaps_populated(self, mock_ep, monkeypatch):
        monkeypatch.setattr(config, "COMPOSITE_MIN_NORMAL", 50)
        mock_ep.return_value = MagicMock(composite_min=50)
        iv = _make_iv(composite=32.0, relative_volume=0.5)
        snap = _make_snap(sentiment=-5, intra_sent=-3)
        result = compute_ticker_quality(
            "RIVN", iv, snap, mode="DAY_TRADE",
            sub_etf_chg=-0.5, earnings_clear_days=5,
            pead_ok=False, pead_score=0, event_active=False,
        )
        assert result.tier == "LOW"
        assert len(result.quality_gaps) > 0

    @patch("utils.ticker_quality.get_entry_params")
    def test_returns_ticker_quality_dataclass(self, mock_ep, monkeypatch):
        monkeypatch.setattr(config, "COMPOSITE_MIN_NORMAL", 50)
        mock_ep.return_value = MagicMock(composite_min=50)
        iv = _make_iv(composite=55.0)
        snap = _make_snap()
        result = compute_ticker_quality(
            "AAPL", iv, snap, mode="DAY_TRADE",
            pead_ok=False, pead_score=0, event_active=False,
        )
        assert isinstance(result, TickerQuality)

    @patch("utils.ticker_quality.get_entry_params")
    def test_component_scores_keys_present(self, mock_ep, monkeypatch):
        monkeypatch.setattr(config, "COMPOSITE_MIN_NORMAL", 50)
        mock_ep.return_value = MagicMock(composite_min=50)
        iv = _make_iv(composite=55.0)
        snap = _make_snap()
        result = compute_ticker_quality(
            "AAPL", iv, snap, mode="DAY_TRADE",
            pead_ok=False, pead_score=0, event_active=False,
        )
        expected_keys = {"composite", "sentiment", "intraday", "sub_etf", "pead", "earnings", "econ_fred"}
        assert expected_keys == set(result.component_scores.keys())

    @patch("utils.ticker_quality.get_entry_params")
    def test_econ_event_active_scores_zero(self, mock_ep, monkeypatch):
        monkeypatch.setattr(config, "COMPOSITE_MIN_NORMAL", 50)
        mock_ep.return_value = MagicMock(composite_min=50)
        iv = _make_iv(composite=55.0)
        snap = _make_snap()
        result = compute_ticker_quality(
            "AAPL", iv, snap, mode="DAY_TRADE",
            pead_ok=False, pead_score=0, event_active=True,
        )
        assert result.component_scores["econ_fred"] == 0

    @patch("utils.ticker_quality.get_entry_params")
    def test_score_clamped_to_100(self, mock_ep, monkeypatch):
        monkeypatch.setattr(config, "COMPOSITE_MIN_NORMAL", 50)
        mock_ep.return_value = MagicMock(composite_min=50)
        iv = _make_iv(composite=100.0)
        snap = _make_snap(sentiment=100, intra_sent=100)
        result = compute_ticker_quality(
            "AAPL", iv, snap, mode="DAY_TRADE",
            sub_etf_chg=5.0, earnings_clear_days=60,
            pead_ok=False, pead_score=0, event_active=False,
        )
        assert result.score <= 100

    @patch("utils.ticker_quality.get_entry_params")
    def test_conviction_tier_unlocks_t2_choppy(self, mock_ep, monkeypatch):
        monkeypatch.setattr(config, "COMPOSITE_MIN_NORMAL", 50)
        mock_ep.return_value = MagicMock(composite_min=50)
        iv = _make_iv(composite=80.0)
        snap = MagicMock()
        snap.regime = "CHOPPY"
        snap.research = {"NVDA": {"sentiment_score": 65}}
        snap.intraday_news = {"NVDA": {"sentiment_score": 65, "shock_score": 0}}
        result = compute_ticker_quality(
            "NVDA", iv, snap, mode="PEAD",
            sub_etf_chg=2.5, earnings_clear_days=35,
            pead_ok=True, pead_score=90, event_active=False,
        )
        assert result.tier == "CONVICTION"
        assert "T2_CHOPPY" in result.unlocks

    @patch("utils.ticker_quality.get_entry_params")
    def test_missing_symbol_in_research_defaults_to_zero(self, mock_ep, monkeypatch):
        monkeypatch.setattr(config, "COMPOSITE_MIN_NORMAL", 50)
        mock_ep.return_value = MagicMock(composite_min=50)
        iv = _make_iv(composite=55.0)
        snap = MagicMock()
        snap.regime = "NORMAL"
        snap.research = {}  # symbol not present
        snap.intraday_news = {}
        result = compute_ticker_quality(
            "AAPL", iv, snap, mode="DAY_TRADE",
            pead_ok=False, pead_score=0, event_active=False,
        )
        assert isinstance(result, TickerQuality)


# ---------------------------------------------------------------------------
# derive_mode tests
# ---------------------------------------------------------------------------

class TestDeriveMode:
    def test_swing_pos_wins_over_pead(self):
        assert derive_mode("NVDA", pead_ok=True, existing_swing_pos=True) == "SWING"

    def test_pead_wins_over_day_trade(self):
        assert derive_mode("NVDA", pead_ok=True, existing_swing_pos=False) == "PEAD"

    def test_default_day_trade(self):
        assert derive_mode("RIVN", pead_ok=False, existing_swing_pos=False) == "DAY_TRADE"
