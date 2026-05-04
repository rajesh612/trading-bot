"""
Integration + unit tests for all trading bot agents.

Run all:          pytest test_agents.py -v
Unit tests only:  pytest test_agents.py -v -m unit
Live API tests:   pytest test_agents.py -v -m live
"""

import json
import math
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

import config

ET = ZoneInfo("America/New_York")
TEST_SYMBOL = "AAPL"
TEST_SYMBOLS = ["AAPL", "MSFT", "NVDA"]


# ═══════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════

def _synthetic_bars(n: int = 100, start_price: float = 150.0) -> pd.DataFrame:
    """Generate realistic synthetic OHLCV bars for unit tests."""
    rng = np.random.default_rng(42)
    closes = [start_price]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1 + rng.normal(0, 0.005)))

    closes = np.array(closes)
    highs  = closes * (1 + np.abs(rng.normal(0, 0.003, n)))
    lows   = closes * (1 - np.abs(rng.normal(0, 0.003, n)))
    opens  = np.roll(closes, 1)
    opens[0] = start_price
    volumes = rng.integers(500_000, 5_000_000, n).astype(float)

    idx = pd.date_range(
        start=datetime.now(ET).replace(hour=9, minute=30) - timedelta(minutes=5 * n),
        periods=n,
        freq="5min",
        tz=ET,
    )
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=idx,
    )


@pytest.fixture(scope="module")
def alpaca():
    from alpaca_client import AlpacaClient
    return AlpacaClient()


@pytest.fixture(scope="module")
def live_bars(alpaca):
    return alpaca.get_intraday_bars(TEST_SYMBOL, lookback=100)


@pytest.fixture()
def synthetic_bars():
    return _synthetic_bars(100)


@pytest.fixture(scope="module")
def live_daily_bars(alpaca):
    return alpaca.get_daily_bars(TEST_SYMBOL, days=30)


# ═══════════════════════════════════════════════════════════════════
# 1. Alpaca Client — connectivity
# ═══════════════════════════════════════════════════════════════════

class TestAlpacaClient:

    @pytest.mark.live
    def test_account_connects(self, alpaca):
        account = alpaca.get_account()
        assert account is not None
        assert float(account.portfolio_value) > 0
        print(f"\n  Portfolio value: ${float(account.portfolio_value):,.2f}")
        print(f"  Buying power:   ${float(account.buying_power):,.2f}")

    @pytest.mark.live
    def test_account_is_paper(self, alpaca):
        """Confirm we are on the paper trading account, not live."""
        account = alpaca.get_account()
        # Paper accounts always have id starting with a known pattern,
        # and the base_url in the client must be the paper endpoint.
        from alpaca.trading.client import TradingClient
        assert alpaca._trading._base_url == config.ALPACA_PAPER_URL or \
               "paper" in str(alpaca._trading._base_url).lower(), \
               "SAFETY: client is NOT pointing at paper endpoint!"

    @pytest.mark.live
    def test_get_latest_price(self, alpaca):
        price = alpaca.get_latest_price(TEST_SYMBOL)
        assert isinstance(price, float)
        assert price > 0
        print(f"\n  {TEST_SYMBOL} latest price: ${price:.2f}")

    @pytest.mark.live
    def test_get_intraday_bars_shape(self, live_bars):
        assert live_bars is not None
        assert not live_bars.empty
        assert set(["open", "high", "low", "close", "volume"]).issubset(live_bars.columns)
        print(f"\n  Intraday bars returned: {len(live_bars)} rows")

    @pytest.mark.live
    def test_intraday_bars_ohlcv_validity(self, live_bars):
        assert (live_bars["high"] >= live_bars["low"]).all(), "high < low found"
        assert (live_bars["volume"] >= 0).all(), "negative volume found"
        assert (live_bars["close"] > 0).all(), "non-positive close price found"

    @pytest.mark.live
    def test_get_daily_bars(self, live_daily_bars):
        assert live_daily_bars is not None
        assert len(live_daily_bars) >= 5
        print(f"\n  Daily bars returned: {len(live_daily_bars)} rows")

    @pytest.mark.live
    def test_get_positions(self, alpaca):
        positions = alpaca.get_positions()
        assert isinstance(positions, dict)
        print(f"\n  Open positions: {list(positions.keys()) or 'none'}")

    @pytest.mark.live
    def test_multiple_symbols(self, alpaca):
        for sym in TEST_SYMBOLS:
            price = alpaca.get_latest_price(sym)
            assert price > 0, f"No price for {sym}"
            print(f"\n  {sym}: ${price:.2f}")


# ═══════════════════════════════════════════════════════════════════
# 2. Indicator Engine — unit tests (synthetic data)
# ═══════════════════════════════════════════════════════════════════

class TestIndicatorEngine:

    @pytest.mark.unit
    def test_vwap_is_positive(self, synthetic_bars):
        from indicator_engine import _compute_vwap
        vwap = _compute_vwap(synthetic_bars)
        assert (vwap > 0).all()
        assert not vwap.isna().all()

    @pytest.mark.unit
    def test_rsi_bounds(self, synthetic_bars):
        from indicator_engine import _compute_rsi
        rsi = _compute_rsi(synthetic_bars["close"])
        valid = rsi.dropna()
        assert (valid >= 0).all() and (valid <= 100).all(), "RSI out of [0, 100]"

    @pytest.mark.unit
    def test_rsi_mean_reversion(self):
        """Strongly trending up data should push RSI toward overbought (>60)."""
        from indicator_engine import _compute_rsi
        close = pd.Series([100 + i * 0.5 for i in range(50)])
        rsi = _compute_rsi(close).dropna()
        assert rsi.iloc[-1] > 60

    @pytest.mark.unit
    def test_macd_components(self, synthetic_bars):
        from indicator_engine import _compute_macd
        macd, signal, hist = _compute_macd(synthetic_bars["close"])
        # Histogram must equal MACD minus signal
        diff = (macd - signal - hist).dropna().abs()
        assert (diff < 1e-10).all(), "MACD histogram != MACD - signal"

    @pytest.mark.unit
    def test_bollinger_bands_ordering(self, synthetic_bars):
        from indicator_engine import _compute_bollinger
        upper, mid, lower = _compute_bollinger(synthetic_bars["close"])
        valid = ~upper.isna()
        assert (upper[valid] >= mid[valid]).all(), "upper < mid"
        assert (mid[valid] >= lower[valid]).all(), "mid < lower"

    @pytest.mark.unit
    def test_atr_positive(self, synthetic_bars):
        from indicator_engine import _compute_atr
        atr = _compute_atr(synthetic_bars)
        valid = atr.dropna()
        assert (valid > 0).all(), "ATR must be positive"

    @pytest.mark.unit
    def test_volume_ratio_baseline(self, synthetic_bars):
        from indicator_engine import _compute_volume_ratio
        vr = _compute_volume_ratio(synthetic_bars["volume"])
        valid = vr.dropna()
        # With random volume, ratio should cluster around 1.0
        assert 0.1 < float(valid.mean()) < 5.0

    @pytest.mark.unit
    def test_stochastic_bounds(self, synthetic_bars):
        from indicator_engine import _compute_stochastic
        k, d = _compute_stochastic(synthetic_bars)
        valid_k = k.dropna()
        valid_d = d.dropna()
        assert (valid_k >= 0).all() and (valid_k <= 100).all()
        assert (valid_d >= 0).all() and (valid_d <= 100).all()

    @pytest.mark.unit
    def test_obv_cumulative(self, synthetic_bars):
        from indicator_engine import _compute_obv
        obv = _compute_obv(synthetic_bars)
        assert len(obv) == len(synthetic_bars)
        assert not obv.isna().any()

    @pytest.mark.unit
    def test_vwap_bands_ordering(self, synthetic_bars):
        from indicator_engine import _compute_vwap, _compute_vwap_bands
        vwap = _compute_vwap(synthetic_bars)
        s1u, s1l, s2u, s2l = _compute_vwap_bands(synthetic_bars, vwap)
        valid = ~s2u.isna()
        assert (s2u[valid] >= s1u[valid]).all(), "2σ upper < 1σ upper"
        assert (s1l[valid] >= s2l[valid]).all(), "1σ lower < 2σ lower"

    @pytest.mark.unit
    def test_composite_score_range(self, synthetic_bars):
        from indicator_engine import compute_indicators
        iv = compute_indicators(TEST_SYMBOL, synthetic_bars)
        assert iv is not None
        assert -100 <= iv.composite_score <= 100
        assert iv.signal in ("BUY", "SELL", "HOLD")
        assert 0 <= iv.confidence <= 100

    @pytest.mark.unit
    def test_compute_indicators_returns_all_fields(self, synthetic_bars):
        from indicator_engine import compute_indicators, IndicatorValues
        import dataclasses
        iv = compute_indicators(TEST_SYMBOL, synthetic_bars)
        assert iv is not None
        fields = {f.name for f in dataclasses.fields(IndicatorValues)}
        for field in fields:
            val = getattr(iv, field)
            assert val is not None, f"Field '{field}' is None"

    @pytest.mark.unit
    def test_insufficient_bars_returns_none(self):
        from indicator_engine import compute_indicators
        tiny = _synthetic_bars(10)
        result = compute_indicators(TEST_SYMBOL, tiny)
        assert result is None

    @pytest.mark.unit
    def test_weight_loading(self):
        from indicator_engine import load_weights
        weights = load_weights()
        assert len(weights) >= 10
        total = sum(weights.values())
        assert abs(total - 100.0) < 0.1, f"Weights sum to {total}, expected 100"

    @pytest.mark.unit
    def test_buy_signal_on_bullish_data(self):
        """Strongly bullish synthetic data should produce a BUY signal."""
        from indicator_engine import compute_indicators
        # Price consistently above VWAP, high volume
        n = 80
        rng = np.random.default_rng(0)
        closes = np.linspace(100, 120, n) + rng.normal(0, 0.1, n)
        highs  = closes + 0.5
        lows   = closes - 0.5
        opens  = np.roll(closes, 1); opens[0] = 100
        volumes = np.linspace(1_000_000, 3_000_000, n)
        idx = pd.date_range("2024-01-02 09:30", periods=n, freq="5min", tz=ET)
        df = pd.DataFrame({"open": opens, "high": highs, "low": lows,
                           "close": closes, "volume": volumes}, index=idx)
        iv = compute_indicators("TEST", df)
        assert iv is not None
        assert iv.composite_score > 0, f"Expected positive score, got {iv.composite_score}"

    @pytest.mark.live
    def test_live_indicators(self, live_bars):
        from indicator_engine import compute_indicators
        iv = compute_indicators(TEST_SYMBOL, live_bars)
        assert iv is not None
        assert iv.current_price > 0
        assert iv.signal in ("BUY", "SELL", "HOLD")
        print(f"\n  {TEST_SYMBOL} live indicators:")
        print(f"    Price:     ${iv.current_price:.2f}")
        print(f"    VWAP:      ${iv.vwap:.2f}")
        print(f"    RSI:       {iv.rsi:.1f}")
        print(f"    MACD hist: {iv.macd_hist:.4f}")
        print(f"    ATR:       ${iv.atr:.4f}")
        print(f"    VolRatio:  {iv.volume_ratio:.2f}x")
        print(f"    Signal:    {iv.signal}  (score={iv.composite_score:+.1f}, conf={iv.confidence:.0f}%)")


# ═══════════════════════════════════════════════════════════════════
# 3. Volatility Screener
# ═══════════════════════════════════════════════════════════════════

class TestVolatilityScreener:

    @pytest.mark.unit
    def test_passthrough_when_under_limit(self, tmp_path, monkeypatch):
        """≤50 tickers must pass through without scoring."""
        from volatility_screener import run_volatility_screener

        tickers = [f"SYM{i}" for i in range(10)]
        tickers_file = tmp_path / "tickers.txt"
        tickers_file.write_text("\n".join(tickers))
        daily_file = tmp_path / "daily_tradeable.txt"

        monkeypatch.setattr(config, "TICKERS_FILE", tickers_file)
        monkeypatch.setattr(config, "DAILY_TRADEABLE_FILE", daily_file)

        with patch("volatility_screener.AlpacaClient") as mock_alpaca:
            result = run_volatility_screener()

        assert set(result) == set(tickers)
        assert daily_file.exists()
        mock_alpaca.assert_not_called()  # Alpaca should NOT be called

    @pytest.mark.live
    def test_scores_real_tickers(self, alpaca):
        from volatility_screener import _score_ticker
        score = _score_ticker(TEST_SYMBOL, alpaca)
        assert isinstance(score, float)
        assert score >= 0
        print(f"\n  {TEST_SYMBOL} volatility score: {score:.2f}")

    @pytest.mark.live
    def test_top50_selection(self, alpaca, monkeypatch, tmp_path):
        """With >50 tickers, should return exactly 50."""
        from volatility_screener import run_volatility_screener

        # Build a fake list of 55 tickers (use real ones + fake ones)
        tickers = TEST_SYMBOLS + [f"FAKE{i}" for i in range(52)]
        tickers_file = tmp_path / "tickers.txt"
        tickers_file.write_text("\n".join(tickers))
        daily_file = tmp_path / "daily_tradeable.txt"

        monkeypatch.setattr(config, "TICKERS_FILE", tickers_file)
        monkeypatch.setattr(config, "DAILY_TRADEABLE_FILE", daily_file)

        # Fake tickers score 0.0, real ones score > 0
        result = run_volatility_screener()
        assert len(result) == 50


# ═══════════════════════════════════════════════════════════════════
# 4. Watch List Agent
# ═══════════════════════════════════════════════════════════════════

class TestWatchListAgent:

    @pytest.mark.live
    def test_score_stock_returns_dict(self, alpaca):
        from watch_list_agent import _score_stock
        result = _score_stock(TEST_SYMBOL, alpaca)
        assert result is not None
        assert "symbol" in result
        assert "vol_spike" in result
        assert "atr_pct" in result
        assert "gap_pct" in result
        assert result["vol_spike"] > 0
        print(f"\n  {TEST_SYMBOL} watch-list score: {result}")

    @pytest.mark.live
    def test_full_watch_list_generation(self, tmp_path, monkeypatch):
        from watch_list_agent import run_watch_list_agent

        tickers_file = tmp_path / "tickers.txt"
        tickers_file.write_text("\n".join(TEST_SYMBOLS))
        watch_file = tmp_path / "watch_list.txt"

        monkeypatch.setattr(config, "TICKERS_FILE", tickers_file)
        monkeypatch.setattr(config, "DAILY_TRADEABLE_FILE", tickers_file)
        monkeypatch.setattr(config, "WATCH_LIST_FILE", watch_file)
        monkeypatch.setattr(config, "STRATEGY_MEMORY_FILE", tmp_path / "strategy_memory.md")

        result = run_watch_list_agent()
        assert isinstance(result, list)
        assert len(result) > 0
        assert all(isinstance(s, str) for s in result)
        print(f"\n  Watch list generated: {result}")


# ═══════════════════════════════════════════════════════════════════
# 5. Research Agent
# ═══════════════════════════════════════════════════════════════════

class TestResearchAgent:

    @pytest.mark.live
    def test_rss_fetch_returns_list(self):
        from research_agent import _fetch_rss
        items = _fetch_rss("https://feeds.marketwatch.com/marketwatch/topstories/")
        assert isinstance(items, list)
        if items:
            assert "title" in items[0]
            assert "summary" in items[0]
        print(f"\n  MarketWatch RSS items fetched: {len(items)}")

    @pytest.mark.unit
    def test_rss_fetch_bad_url_doesnt_crash(self):
        from research_agent import _fetch_rss
        # feedparser swallows errors and returns an empty feed — must not raise
        result = _fetch_rss("https://this-url-does-not-exist-xyz.invalid/feed")
        assert isinstance(result, list)

    @pytest.mark.live
    def test_fetch_stock_news(self):
        from research_agent import _fetch_stock_news
        items = _fetch_stock_news(TEST_SYMBOL)
        assert isinstance(items, list)
        print(f"\n  {TEST_SYMBOL} news items fetched: {len(items)}")

    @pytest.mark.live
    def test_full_research_report(self, tmp_path, monkeypatch):
        from research_agent import run_research_agent

        report_file = tmp_path / "research_report.json"
        monkeypatch.setattr(config, "RESEARCH_REPORT_FILE", report_file)

        report = run_research_agent([TEST_SYMBOL])

        assert isinstance(report, dict)
        assert TEST_SYMBOL in report

        entry = report[TEST_SYMBOL]
        assert "sentiment_score" in entry
        assert "sentiment_label" in entry
        assert entry["sentiment_label"] in ("BULLISH", "BEARISH", "NEUTRAL")
        assert -100 <= entry["sentiment_score"] <= 100
        assert "strategy" in entry

        print(f"\n  {TEST_SYMBOL} sentiment: {entry['sentiment_label']} ({entry['sentiment_score']:+d})")
        print(f"  Catalyst: {entry.get('key_catalyst')}")
        print(f"  Strategy snippet: {entry['strategy'][:100]}…")


# ═══════════════════════════════════════════════════════════════════
# 6. Trading Agent
# ═══════════════════════════════════════════════════════════════════

class TestTradingAgent:

    @pytest.mark.unit
    def test_open_positions_persistence(self, tmp_path, monkeypatch):
        from trading_agent import save_open_positions, load_open_positions
        pos_file = tmp_path / "open_positions.json"
        monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", pos_file)

        data = {"AAPL": {"entry_price": 150.0, "qty": 10.0}}
        save_open_positions(data)
        loaded = load_open_positions()
        assert loaded == data

    @pytest.mark.unit
    def test_trade_journal_append(self, tmp_path, monkeypatch):
        from trading_agent import append_trade, load_open_positions
        journal_file = tmp_path / "trade_journal.json"
        journal_file.write_text("[]")
        monkeypatch.setattr(config, "TRADE_JOURNAL_FILE", journal_file)

        from trading_agent import append_trade
        append_trade({"type": "BUY", "symbol": "AAPL", "price": 150.0})
        append_trade({"type": "CLOSE", "symbol": "AAPL", "price": 155.0})

        with open(journal_file) as f:
            journal = json.load(f)
        assert len(journal) == 2
        assert journal[0]["type"] == "BUY"
        assert journal[1]["type"] == "CLOSE"

    @pytest.mark.live
    def test_claude_decision_structure(self, tmp_path, monkeypatch):
        """Claude must return a valid structured decision for a real indicator snapshot."""
        from trading_agent import _ask_claude_batch
        from indicator_engine import compute_indicators
        from alpaca_client import AlpacaClient
        import anthropic as ant

        alpaca = AlpacaClient()
        bars = alpaca.get_intraday_bars(TEST_SYMBOL)
        iv = compute_indicators(TEST_SYMBOL, bars)
        assert iv is not None

        ai = ant.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        weights = json.loads(config.INDICATOR_WEIGHTS_FILE.read_text())

        decisions = _ask_claude_batch(
            batch=[(TEST_SYMBOL, iv, None)],
            research={},
            strategy_memory="",
            weights=weights,
            ai=ai,
        )
        decision = decisions[TEST_SYMBOL]

        assert "action" in decision
        assert decision["action"] in ("BUY", "HOLD", "CLOSE")
        assert "confidence" in decision
        assert 0 <= decision["confidence"] <= 100
        assert "reasoning" in decision
        assert "stop_loss_pct" in decision
        assert "take_profit_pct" in decision

        print(f"\n  Claude decision for {TEST_SYMBOL}:")
        print(f"    Action:     {decision['action']}")
        print(f"    Confidence: {decision['confidence']}%")
        print(f"    Reasoning:  {decision['reasoning']}")

    @pytest.mark.live
    def test_full_trading_cycle_no_orders(self, tmp_path, monkeypatch):
        """Run a full trading cycle but intercept order placement."""
        from trading_agent import run_trading_cycle

        watch_file = tmp_path / "watch_list.txt"
        watch_file.write_text(TEST_SYMBOL)
        pos_file = tmp_path / "open_positions.json"
        pos_file.write_text("{}")
        journal_file = tmp_path / "trade_journal.json"
        journal_file.write_text("[]")

        monkeypatch.setattr(config, "WATCH_LIST_FILE", watch_file)
        monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", pos_file)
        monkeypatch.setattr(config, "TRADE_JOURNAL_FILE", journal_file)

        orders_placed = []

        original_place = None
        from alpaca_client import AlpacaClient

        def mock_place(self, symbol, qty, side):
            orders_placed.append({"symbol": symbol, "qty": qty, "side": side})
            return {"id": "mock-order-id", "symbol": symbol, "side": side, "qty": qty}

        with patch.object(AlpacaClient, "place_market_order", mock_place):
            run_trading_cycle()

        # Cycle completed without exception — orders may or may not have been placed
        print(f"\n  Trading cycle completed. Orders intercepted: {orders_placed or 'none (HOLD)'}")


# ═══════════════════════════════════════════════════════════════════
# 7. Learning Agent
# ═══════════════════════════════════════════════════════════════════

class TestLearningAgent:

    @pytest.fixture()
    def sample_trades(self):
        today = datetime.now(ET).date().isoformat()
        return [
            {
                "type": "CLOSE", "symbol": "AAPL", "timestamp": f"{today}T15:30:00",
                "exit_price": 155.0, "entry_price": 150.0, "qty": 10,
                "pnl_pct": 3.33, "pnl_dollar": 50.0, "close_reason": "take_profit",
                "entry_composite_score": 45.0, "exit_composite_score": 60.0,
                "entry_indicators": {"rsi": 42, "vwap": 149.5, "volume_ratio": 2.1},
                "indicator_signals_at_entry": {"VWAP": "correct", "RSI": "correct"},
                "claude_reasoning": "Strong VWAP + volume breakout.",
            },
            {
                "type": "CLOSE", "symbol": "TSLA", "timestamp": f"{today}T14:00:00",
                "exit_price": 195.0, "entry_price": 200.0, "qty": 5,
                "pnl_pct": -2.5, "pnl_dollar": -25.0, "close_reason": "stop_loss",
                "entry_composite_score": 20.0, "exit_composite_score": -30.0,
                "entry_indicators": {"rsi": 65, "vwap": 198.0, "volume_ratio": 0.9},
                "indicator_signals_at_entry": {"VWAP": "incorrect", "RSI": "incorrect"},
                "claude_reasoning": "Stochastic oversold but volume was weak.",
            },
        ]

    @pytest.mark.unit
    def test_weight_normalisation(self):
        """After applying adjustments, weights must sum to 100."""
        weights = {
            "VWAP": 20.0, "RSI": 12.0, "Volume Ratio": 12.0,
            "MACD": 10.0, "EMA 9/21 Crossover": 10.0, "Bollinger Bands": 10.0,
            "VWAP StdDev Bands": 10.0, "ATR": 8.0, "Stochastic Oscillator": 5.0, "OBV": 3.0,
        }
        adjustments = {"VWAP": 3.0, "RSI": -2.0, "Stochastic Oscillator": -1.0}
        for k, adj in adjustments.items():
            weights[k] = max(1.0, weights[k] + adj)
        total = sum(weights.values())
        normalised = {k: round(v / total * 100, 2) for k, v in weights.items()}
        assert abs(sum(normalised.values()) - 100.0) < 0.1

    @pytest.mark.live
    def test_full_learning_cycle(self, sample_trades, tmp_path, monkeypatch):
        from learning_agent import run_learning_agent

        journal_file = tmp_path / "trade_journal.json"
        journal_file.write_text(json.dumps(sample_trades))
        weights_file = tmp_path / "indicator_weights.json"
        weights_file.write_text(config.INDICATOR_WEIGHTS_FILE.read_text())
        perf_log = tmp_path / "indicator_performance_log.md"
        memory_file = tmp_path / "strategy_memory.md"

        monkeypatch.setattr(config, "TRADE_JOURNAL_FILE", journal_file)
        monkeypatch.setattr(config, "INDICATOR_WEIGHTS_FILE", weights_file)
        monkeypatch.setattr(config, "INDICATOR_PERFORMANCE_LOG", perf_log)
        monkeypatch.setattr(config, "STRATEGY_MEMORY_FILE", memory_file)

        run_learning_agent()

        # Weights file should be updated and still sum to 100
        new_weights = json.loads(weights_file.read_text())
        total = sum(new_weights.values())
        assert abs(total - 100.0) < 0.2, f"Weights sum to {total} after learning"

        # Performance log should have an entry
        assert perf_log.exists()
        log_content = perf_log.read_text()
        assert len(log_content) > 50

        # Strategy memory should be updated
        assert memory_file.exists()
        assert len(memory_file.read_text()) > 100

        print(f"\n  Updated weights: {new_weights}")
        print(f"  Performance log preview:\n{log_content[:300]}")


# ═══════════════════════════════════════════════════════════════════
# 8. End-to-end smoke test
# ═══════════════════════════════════════════════════════════════════

class TestEndToEnd:

    @pytest.mark.live
    def test_indicator_pipeline_aapl(self):
        """Pull live bars → compute indicators → verify sane values."""
        from alpaca_client import AlpacaClient
        from indicator_engine import compute_indicators

        client = AlpacaClient()
        bars = client.get_intraday_bars("AAPL", lookback=100)
        assert not bars.empty

        iv = compute_indicators("AAPL", bars)
        assert iv is not None

        price = iv.current_price
        assert iv.bb_lower < price * 1.1, "BB lower unreasonably high"
        assert iv.bb_upper > price * 0.9, "BB upper unreasonably low"
        assert iv.ema9 > 0
        assert iv.ema21 > 0
        assert iv.atr > 0
        assert math.isfinite(iv.composite_score)

        print("\n  ✓ Full indicator pipeline smoke test passed for AAPL")
        print(f"    Signal: {iv.signal} | Score: {iv.composite_score:+.1f} | Confidence: {iv.confidence:.0f}%")

    @pytest.mark.live
    def test_config_env_loaded(self):
        assert config.ALPACA_API_KEY, "ALPACA_API_KEY not loaded from .env"
        assert config.ALPACA_SECRET_KEY, "ALPACA_SECRET_KEY not loaded from .env"
        assert config.ANTHROPIC_API_KEY, "ANTHROPIC_API_KEY not loaded from .env"
        print("\n  ✓ All API keys loaded from .env")


class TestDailyReport:

    @pytest.mark.unit
    def test_pnl_totals_correct(self):
        from daily_report import build_report_body
        trades = [
            {
                "type": "CLOSE", "symbol": "AAPL", "qty": 10,
                "entry_price": 100.0, "exit_price": 105.0,
                "pnl_dollar": 50.0, "pnl_pct": 5.0,
                "close_reason": "take_profit", "timestamp": "2026-04-30T12:00:00",
            },
            {
                "type": "CLOSE", "symbol": "TSLA", "qty": 5,
                "entry_price": 200.0, "exit_price": 192.0,
                "pnl_dollar": -40.0, "pnl_pct": -4.0,
                "close_reason": "stop_loss", "timestamp": "2026-04-30T13:00:00",
            },
        ]
        account = {"portfolio_value": 100010.0, "last_equity": 100000.0}
        subject, body = build_report_body(trades, account, "NORMAL", "AAPL, TSLA", {})
        assert "2 trades" in subject
        assert "AAPL" in body
        assert "TSLA" in body
        assert "take_profit" in body
        assert "stop_loss" in body

    @pytest.mark.unit
    def test_report_body_contains_fields(self):
        from daily_report import build_report_body
        account = {"portfolio_value": 100000.0, "last_equity": 100000.0}
        subject, body = build_report_body(
            [], account, "HIGH_VOL", "NVDA, AMD", {"RSI": 50.0, "VWAP": 50.0}
        )
        assert "HIGH_VOL" in body
        assert "NVDA, AMD" in body
        assert "RSI" in body
        assert "No trades closed today" in body
        assert "No trades today" in subject

    @pytest.mark.unit
    def test_sentinel_false_when_missing(self, tmp_path, monkeypatch):
        import daily_report
        monkeypatch.setattr(
            daily_report, "DAILY_REPORT_SENT_FILE", tmp_path / "daily_report_sent.json"
        )
        assert daily_report._already_sent_today() is False

    @pytest.mark.unit
    def test_sentinel_true_for_today(self, tmp_path, monkeypatch):
        import daily_report
        from datetime import datetime
        from zoneinfo import ZoneInfo
        sentinel = tmp_path / "daily_report_sent.json"
        today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        sentinel.write_text(json.dumps({"date": today}))
        monkeypatch.setattr(daily_report, "DAILY_REPORT_SENT_FILE", sentinel)
        assert daily_report._already_sent_today() is True

    @pytest.mark.unit
    def test_send_skipped_if_no_config(self, monkeypatch):
        import config as cfg
        monkeypatch.setattr(cfg, "EMAIL_SENDER", "")
        monkeypatch.setattr(cfg, "EMAIL_APP_PASSWORD", "")
        from daily_report import send_daily_report
        send_daily_report()  # must not raise

    @pytest.mark.unit
    def test_send_email_unicode_subject(self, monkeypatch):
        """_send_email must handle non-ASCII characters (e.g. em dash) without raising."""
        import config as cfg
        monkeypatch.setattr(cfg, "EMAIL_SENDER", "bot@example.com")
        monkeypatch.setattr(cfg, "EMAIL_APP_PASSWORD", "secret")
        monkeypatch.setattr(cfg, "EMAIL_RECIPIENT", "user@example.com")

        captured = {}

        class FakeSMTP:
            def __init__(self, *a, **kw): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def login(self, user, pwd): pass
            def send_message(self, msg):
                captured["subject"] = msg["Subject"]
                captured["body"]    = msg.get_content()

        with patch("smtplib.SMTP_SSL", FakeSMTP):
            from daily_report import _send_email
            # Em dash in subject — this is what broke the old raw-string approach
            _send_email("Trading Bot — Daily Report", "P&L: $0.00")

        assert captured["subject"] == "Trading Bot — Daily Report"
        assert "P&L" in captured["body"]

    @pytest.mark.unit
    def test_send_email_uses_certifi(self, monkeypatch):
        """SSL context must load certifi's CA bundle via load_verify_locations."""
        import config as cfg
        import ssl
        monkeypatch.setattr(cfg, "EMAIL_SENDER", "bot@example.com")
        monkeypatch.setattr(cfg, "EMAIL_APP_PASSWORD", "secret")
        monkeypatch.setattr(cfg, "EMAIL_RECIPIENT", "user@example.com")

        captured = {}
        real_create = ssl.create_default_context

        def fake_create_context(*args, **kwargs):
            ctx = real_create(*args, **kwargs)
            real_load = ctx.load_verify_locations
            def capturing_load(*a, **kw):
                captured["cafile"] = kw.get("cafile")
                return real_load(*a, **kw)
            ctx.load_verify_locations = capturing_load
            return ctx

        class FakeSMTP:
            def __init__(self, *a, **kw): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def login(self, *a): pass
            def send_message(self, msg): pass

        with patch("ssl.create_default_context", fake_create_context), \
             patch("smtplib.SMTP_SSL", FakeSMTP):
            from daily_report import _send_email
            _send_email("Test", "body")

        import certifi
        assert captured["cafile"] == certifi.where()
