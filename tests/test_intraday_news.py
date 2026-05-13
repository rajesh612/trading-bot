"""Unit tests for intraday_news module."""
import json
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
import pytest


class TestLoadIntradayNews:

    @pytest.mark.unit
    def test_returns_empty_dict_when_file_missing(self, tmp_path):
        from research.intraday import load_intraday_news
        with patch("research.intraday.config") as mc:
            mc.INTRADAY_NEWS_CACHE_FILE = tmp_path / "missing.json"
            mc.INTRADAY_NEWS_STALE_MIN = 45
            result = load_intraday_news()
        assert result == {}

    @pytest.mark.unit
    def test_returns_empty_dict_when_stale(self, tmp_path):
        from research.intraday import load_intraday_news
        stale_time = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()
        cache = {"__updated_at": stale_time, "NVDA": {"sentiment_score": 10, "shock_score": 0}}
        cache_file = tmp_path / "intraday_news_cache.json"
        cache_file.write_text(json.dumps(cache))
        with patch("research.intraday.config") as mc:
            mc.INTRADAY_NEWS_CACHE_FILE = cache_file
            mc.INTRADAY_NEWS_STALE_MIN = 45
            result = load_intraday_news()
        assert result == {}

    @pytest.mark.unit
    def test_returns_cache_when_fresh(self, tmp_path):
        from research.intraday import load_intraday_news
        fresh_time = datetime.now(timezone.utc).isoformat()
        cache = {
            "__updated_at": fresh_time,
            "NVDA": {"sentiment_score": 22, "shock_score": 0, "headlines": ["NVDA up"]},
        }
        cache_file = tmp_path / "intraday_news_cache.json"
        cache_file.write_text(json.dumps(cache))
        with patch("research.intraday.config") as mc:
            mc.INTRADAY_NEWS_CACHE_FILE = cache_file
            mc.INTRADAY_NEWS_STALE_MIN = 45
            result = load_intraday_news()
        assert result["NVDA"]["sentiment_score"] == 22
        assert "__updated_at" not in result  # meta key stripped


class TestComputeSymbolSentiment:

    @pytest.mark.unit
    def test_positive_keywords_raise_score(self):
        from research.intraday import _compute_symbol_sentiment
        headlines = [{"title": "NVDA beats earnings record upgrade"}]
        stocktwits = {"bullish_percent": 70}
        score = _compute_symbol_sentiment(headlines, stocktwits)
        assert score > 0

    @pytest.mark.unit
    def test_negative_keywords_lower_score(self):
        from research.intraday import _compute_symbol_sentiment
        headlines = [{"title": "TSLA recall warning downgrade cut"}]
        stocktwits = {"bullish_percent": 30}
        score = _compute_symbol_sentiment(headlines, stocktwits)
        assert score < 0

    @pytest.mark.unit
    def test_empty_inputs_return_zero(self):
        from research.intraday import _compute_symbol_sentiment
        assert _compute_symbol_sentiment([], {}) == 0
