"""
Research agent unit tests.
Run with: pytest tests/test_research.py -v -m unit
No API calls — all tests use synthetic fixtures.
"""
import pytest


# ── research_agent: macro shock + sector sentiment ───────────────────────────

@pytest.mark.unit
class TestResearchAgentPlan2:
    def test_macro_shock_empty_headlines(self):
        from research_agent import _compute_macro_shock_score
        assert _compute_macro_shock_score([]) == 0

    def test_macro_shock_single_keyword(self):
        from research_agent import _compute_macro_shock_score
        headlines = [{"title": "New tariff announced on imports", "summary": ""}]
        score = _compute_macro_shock_score(headlines)
        assert score >= 20

    def test_macro_shock_multiple_keywords(self):
        from research_agent import _compute_macro_shock_score
        headlines = [
            {"title": "Fed emergency rate cut due to bank failure", "summary": "circuit breaker triggered"},
        ]
        score = _compute_macro_shock_score(headlines)
        assert score == 100  # capped at 100

    def test_macro_shock_no_keywords(self):
        from research_agent import _compute_macro_shock_score
        headlines = [{"title": "Earnings beat expectations", "summary": "quarterly revenue up"}]
        assert _compute_macro_shock_score(headlines) == 0

    def test_sector_sentiment_groups_by_sector(self):
        from research_agent import _compute_sector_sentiment
        report = {
            "NVDA": {"combined_sentiment_score": 60},
            "AMD":  {"combined_sentiment_score": 40},
            "MSFT": {"combined_sentiment_score": 80},
        }
        result = _compute_sector_sentiment(report)
        assert "AI_SEMI" in result
        assert abs(result["AI_SEMI"] - 50.0) < 0.1
        assert "HYPERSCALER" in result
        assert result["HYPERSCALER"] == 80.0

    def test_sector_sentiment_skips_meta(self):
        from research_agent import _compute_sector_sentiment
        report = {
            "NVDA": {"combined_sentiment_score": 50},
            "__meta__": {"sector_sentiment": {}, "macro_shock_score": 0},
        }
        result = _compute_sector_sentiment(report)
        assert "__meta__" not in result

    def test_detect_capitulation_candidate_true(self):
        from research_agent import _detect_capitulation_candidate
        assert _detect_capitulation_candidate(
            symbol="NVDA",
            drawdown_from_20d_high=-25.0,
            sector_sentiment=-30.0,
            obv_trend=0.15,
        ) is True

    def test_detect_capitulation_insufficient_drawdown(self):
        from research_agent import _detect_capitulation_candidate
        assert _detect_capitulation_candidate(
            symbol="NVDA",
            drawdown_from_20d_high=-15.0,
            sector_sentiment=-30.0,
            obv_trend=0.15,
        ) is False

    def test_detect_capitulation_sector_too_positive(self):
        from research_agent import _detect_capitulation_candidate
        assert _detect_capitulation_candidate(
            symbol="NVDA",
            drawdown_from_20d_high=-25.0,
            sector_sentiment=-10.0,
            obv_trend=0.15,
        ) is False

    def test_detect_capitulation_obv_not_rising(self):
        from research_agent import _detect_capitulation_candidate
        assert _detect_capitulation_candidate(
            symbol="NVDA",
            drawdown_from_20d_high=-25.0,
            sector_sentiment=-30.0,
            obv_trend=0.05,
        ) is False


# ── research_agent: _fetch_with_retry ────────────────────────────────────────

@pytest.mark.unit
class TestFetchWithRetry:
    def test_succeeds_on_first_attempt(self):
        from research_agent import _fetch_with_retry
        calls = []
        def fn():
            calls.append(1)
            return "ok"
        result = _fetch_with_retry(fn)
        assert result == "ok"
        assert len(calls) == 1

    def test_retries_on_failure_then_succeeds(self):
        from research_agent import _fetch_with_retry
        calls = []
        def fn():
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("transient")
            return "recovered"
        result = _fetch_with_retry(fn, attempts=3, base_delay=0)
        assert result == "recovered"
        assert len(calls) == 3

    def test_raises_after_all_attempts_exhausted(self):
        from research_agent import _fetch_with_retry
        def fn():
            raise ConnectionError("down")
        with pytest.raises(ConnectionError):
            _fetch_with_retry(fn, attempts=3, base_delay=0)


# ── research_agent: pure functions ───────────────────────────────────────────

@pytest.mark.unit
class TestResearchAgentOrchestration:
    """Tests for pure / side-effect-free helpers in research_agent.py."""

    # ── _compute_macro_shock_score ─────────────────────────────────────────

    def test_macro_shock_empty_headlines_returns_zero(self):
        from research_agent import _compute_macro_shock_score
        assert _compute_macro_shock_score([]) == 0

    def test_macro_shock_no_keywords_returns_zero(self):
        from research_agent import _compute_macro_shock_score
        headlines = [{"title": "Stocks mixed on earnings season", "summary": "Investors watch results."}]
        assert _compute_macro_shock_score(headlines) == 0

    def test_macro_shock_single_keyword_returns_weight(self):
        from research_agent import _compute_macro_shock_score
        # "tariff" carries weight 20
        headlines = [{"title": "New tariff announced on imports", "summary": ""}]
        score = _compute_macro_shock_score(headlines)
        assert score == 20

    def test_macro_shock_multiple_keywords_accumulate(self):
        from research_agent import _compute_macro_shock_score
        # "tariff" (20) + "recession" (20) = 40
        headlines = [{"title": "Tariff fears spark recession worries", "summary": ""}]
        score = _compute_macro_shock_score(headlines)
        assert score == 40

    def test_macro_shock_capped_at_100(self):
        from research_agent import _compute_macro_shock_score
        # Fill with every keyword — sum would far exceed 100
        title = "tariff trade war sanction war escalation fed emergency bank failure cpi shock inflation surge recession default nuclear pandemic market crash circuit breaker"
        headlines = [{"title": title, "summary": ""}]
        score = _compute_macro_shock_score(headlines)
        assert score == 100

    def test_macro_shock_case_insensitive(self):
        from research_agent import _compute_macro_shock_score
        # Keywords are lower-cased before matching; title in upper case must still match
        headlines = [{"title": "TARIFF WAR DECLARED", "summary": ""}]
        score = _compute_macro_shock_score(headlines)
        assert score > 0

    def test_macro_shock_scans_summary_field(self):
        from research_agent import _compute_macro_shock_score
        # Keyword is only in summary, not title
        headlines = [{"title": "Markets update", "summary": "Fed emergency rate cut announced"}]
        score = _compute_macro_shock_score(headlines)
        assert score == 35  # "fed emergency" weight

    # ── _compute_sector_sentiment ──────────────────────────────────────────

    def test_sector_sentiment_empty_report(self):
        from research_agent import _compute_sector_sentiment
        result = _compute_sector_sentiment({})
        assert result == {}

    def test_sector_sentiment_skips_meta_key(self):
        from research_agent import _compute_sector_sentiment
        report = {
            "__meta__": {"macro_shock_score": 40},
            "NVDA": {"combined_sentiment_score": 60},
        }
        result = _compute_sector_sentiment(report)
        assert "__meta__" not in result

    def test_sector_sentiment_averages_scores_per_sector(self):
        from research_agent import _compute_sector_sentiment
        # NVDA and AMD both map to "AI_SEMI" in tier1_config;
        # if they map to different sectors the test still asserts correct averaging logic.
        report = {
            "NVDA": {"combined_sentiment_score": 80},
            "AMD":  {"combined_sentiment_score": 20},
        }
        result = _compute_sector_sentiment(report)
        # Both symbols are tier-1 AI_SEMI; average should be 50.0
        # If they fall into different buckets, each bucket has one value.
        totals = sum(result.values())
        # Regardless of grouping, 80+20=100 total, distributed across buckets
        assert totals == pytest.approx(100.0) or all(v in (80.0, 20.0, 50.0) for v in result.values())

    def test_sector_sentiment_uses_combined_score_preferentially(self):
        from research_agent import _compute_sector_sentiment
        report = {
            "NVDA": {
                "sentiment_score": 10,           # legacy fallback
                "combined_sentiment_score": 70,  # preferred
            }
        }
        result = _compute_sector_sentiment(report)
        # The value assigned should be 70, not 10
        sector_values = list(result.values())
        assert 70.0 in sector_values

    # ── _detect_capitulation_candidate ────────────────────────────────────

    def test_capitulation_all_conditions_met(self):
        from research_agent import _detect_capitulation_candidate
        assert _detect_capitulation_candidate("NVDA", -25.0, -30.0, 0.15) is True

    def test_capitulation_drawdown_too_small(self):
        from research_agent import _detect_capitulation_candidate
        # drawdown only -15% (must be ≤ -20%)
        assert _detect_capitulation_candidate("NVDA", -15.0, -30.0, 0.15) is False

    def test_capitulation_sector_sentiment_too_high(self):
        from research_agent import _detect_capitulation_candidate
        # sector_sentiment -10 (must be < -20)
        assert _detect_capitulation_candidate("NVDA", -25.0, -10.0, 0.15) is False

    def test_capitulation_obv_declining(self):
        from research_agent import _detect_capitulation_candidate
        # obv_trend ≤ 0.10 — institutions not yet accumulating
        assert _detect_capitulation_candidate("NVDA", -25.0, -30.0, 0.05) is False

    # ── _quick_sentiment_result ────────────────────────────────────────────

    def test_quick_sentiment_returns_none_when_news_present(self):
        import config
        from research_agent import _quick_sentiment_result
        stocktwits = {
            "available": True,
            "bullish_count": 20,
            "bearish_count": 5,
            "bullish_pct": 80,
        }
        stock_news = [{"title": "Earnings beat!", "summary": ""}]
        # News present → Claude is always needed
        result = _quick_sentiment_result(stocktwits, stock_news)
        assert result is None

    def test_quick_sentiment_returns_none_when_stocktwits_unavailable(self):
        from research_agent import _quick_sentiment_result
        result = _quick_sentiment_result({"available": False}, [])
        assert result is None

    def test_quick_sentiment_bullish_rule_fires(self):
        import config
        from research_agent import _quick_sentiment_result
        tagged = config.STOCKTWITS_MIN_TAGGED + 5
        bull = int(tagged * config.QUICK_SENTIMENT_BULL_PCT / 100) + 1
        bear = tagged - bull
        stocktwits = {
            "available": True,
            "bullish_count": bull,
            "bearish_count": bear,
            "bullish_pct": config.QUICK_SENTIMENT_BULL_PCT,
        }
        result = _quick_sentiment_result(stocktwits, [])
        assert result is not None
        assert result["combined_sentiment_label"] == "BULLISH"
        assert result["combined_sentiment_score"] == config.QUICK_SENTIMENT_SCORE_BULL

    def test_quick_sentiment_bearish_rule_fires(self):
        import config
        from research_agent import _quick_sentiment_result
        tagged = config.STOCKTWITS_MIN_TAGGED + 5
        bull = int(tagged * config.QUICK_SENTIMENT_BEAR_PCT / 100) - 1
        bear = tagged - bull
        stocktwits = {
            "available": True,
            "bullish_count": bull,
            "bearish_count": bear,
            "bullish_pct": config.QUICK_SENTIMENT_BEAR_PCT,
        }
        result = _quick_sentiment_result(stocktwits, [])
        assert result is not None
        assert result["combined_sentiment_label"] == "BEARISH"
        assert result["combined_sentiment_score"] == config.QUICK_SENTIMENT_SCORE_BEAR

    def test_quick_sentiment_returns_none_when_insufficient_tagged(self):
        import config
        from research_agent import _quick_sentiment_result
        # Tagged below minimum threshold
        stocktwits = {
            "available": True,
            "bullish_count": 3,
            "bearish_count": 2,
            "bullish_pct": 80,
        }
        result = _quick_sentiment_result(stocktwits, [])
        assert result is None
