"""Unit tests for backtest/data.py — pure functions and VIX classification."""
import pytest
import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


@pytest.mark.unit
def test_select_watch_list_for_day_ranks_by_atr():
    """HIGHVOL (10% ATR) should rank above LOWVOL (1% ATR)."""
    from backtest.data import select_watch_list_for_day

    day = datetime(2026, 5, 4, tzinfo=ET)  # Monday
    dates = pd.date_range("2026-04-06", periods=20, freq="B", tz="UTC")

    highvol = pd.DataFrame(
        {"open": [10.0]*20, "high": [11.0]*20, "low": [9.0]*20,
         "close": [10.0]*20, "volume": [5000]*20},
        index=dates,
    )
    lowvol = pd.DataFrame(
        {"open": [100.0]*20, "high": [100.5]*20, "low": [99.5]*20,
         "close": [100.0]*20, "volume": [5000]*20},
        index=dates,
    )

    result = select_watch_list_for_day({"HIGHVOL": highvol, "LOWVOL": lowvol}, day, n=10)

    assert result[0] == "HIGHVOL"
    assert "LOWVOL" in result


@pytest.mark.unit
def test_select_watch_list_for_day_excludes_future_bars():
    """Bars on `day` itself must not be used — only prior_day and earlier."""
    from backtest.data import select_watch_list_for_day

    day = datetime(2026, 5, 5, tzinfo=ET)
    dates_past = pd.date_range("2026-04-07", periods=20, freq="B", tz="UTC")
    dates_future = pd.date_range("2026-05-05", periods=5, freq="B", tz="UTC")

    # SYM_A: low ATR in past bars, extreme ATR in future bars
    past_df = pd.DataFrame(
        {"open": [10.0]*20, "high": [10.5]*20, "low": [9.5]*20,
         "close": [10.0]*20, "volume": [1000]*20},
        index=dates_past,
    )
    future_df = pd.DataFrame(
        {"open": [10.0]*5, "high": [20.0]*5, "low": [1.0]*5,
         "close": [10.0]*5, "volume": [1000]*5},
        index=dates_future,
    )
    sym_a = pd.concat([past_df, future_df])

    # SYM_B: consistent moderate ATR
    sym_b = pd.DataFrame(
        {"open": [100.0]*20, "high": [103.0]*20, "low": [97.0]*20,
         "close": [100.0]*20, "volume": [5000]*20},
        index=dates_past,
    )

    result = select_watch_list_for_day({"SYM_A": sym_a, "SYM_B": sym_b}, day, n=5)

    # SYM_B should rank first; future bars for SYM_A must not inflate its score
    assert result[0] == "SYM_B"


@pytest.mark.unit
def test_select_watch_list_for_day_respects_n():
    """Returns at most n symbols."""
    from backtest.data import select_watch_list_for_day

    day = datetime(2026, 5, 4, tzinfo=ET)
    dates = pd.date_range("2026-04-06", periods=20, freq="B", tz="UTC")

    daily_bars = {}
    for i in range(10):
        sym = f"SYM{i}"
        daily_bars[sym] = pd.DataFrame(
            {"open": [10.0]*20, "high": [11.0]*20, "low": [9.0]*20,
             "close": [10.0]*20, "volume": [1000]*20},
            index=dates,
        )

    result = select_watch_list_for_day(daily_bars, day, n=3)

    assert len(result) == 3


@pytest.mark.unit
def test_classify_vix_day_normal():
    """VIX below 90d average → NORMAL."""
    from backtest.data import _classify_vix_day

    # 91 bars at 20.0 (avg=20), then current at 15.0 (below avg)
    past_vix = [(i, 20.0) for i in range(91)] + [(91, 15.0)]
    past_spy = [(0, 400.0), (1, 400.0)]

    assert _classify_vix_day(past_vix, past_spy) == "NORMAL"


@pytest.mark.unit
def test_classify_vix_day_high_vol():
    """VIX above 90d avg but SPY not down > 1% → HIGH_VOL."""
    from backtest.data import _classify_vix_day

    # avg of past 90 = 15, current = 25 (above), prev = 20 (rising), SPY flat
    past_vix = [(i, 15.0) for i in range(90)] + [(90, 20.0), (91, 25.0)]
    past_spy = [(0, 400.0), (1, 400.0)]

    assert _classify_vix_day(past_vix, past_spy) == "HIGH_VOL"


@pytest.mark.unit
def test_classify_vix_day_crisis():
    """VIX above avg, rising, and SPY down > 1% → CRISIS."""
    from backtest.data import _classify_vix_day

    past_vix = [(i, 15.0) for i in range(90)] + [(90, 20.0), (91, 25.0)]
    past_spy = [(0, 400.0), (1, 392.0)]  # -2% drop

    assert _classify_vix_day(past_vix, past_spy) == "CRISIS"


@pytest.mark.unit
def test_classify_vix_day_empty_returns_normal():
    """Empty VIX data → falls back to NORMAL."""
    from backtest.data import _classify_vix_day

    assert _classify_vix_day([], []) == "NORMAL"


@pytest.mark.unit
def test_fetch_vix_regimes_uses_yahoo_get_once(monkeypatch):
    """fetch_vix_regimes must call yahoo_get exactly twice (VIX + SPY), not once per day."""
    call_urls: list[str] = []

    def mock_yahoo_get(url: str) -> dict:
        call_urls.append(url)
        ts = list(range(1_700_000_000, 1_700_000_200))
        prices = [15.0] * 200
        return {"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [{"close": prices}]}}]}}

    monkeypatch.setattr("backtest.data.yahoo_get", mock_yahoo_get)

    from backtest.data import fetch_vix_regimes
    days = [
        datetime(2026, 5, 1, tzinfo=ET),
        datetime(2026, 5, 2, tzinfo=ET),
        datetime(2026, 5, 5, tzinfo=ET),
    ]
    fetch_vix_regimes(days)

    assert len(call_urls) == 2  # one VIX, one SPY — NOT 6 (2 × 3 days)


@pytest.mark.unit
def test_fetch_vix_regimes_fallback_on_error(monkeypatch):
    """If Yahoo fetch raises, all days fall back to NORMAL."""
    def mock_yahoo_get(url: str) -> dict:
        raise ConnectionError("network down")

    monkeypatch.setattr("backtest.data.yahoo_get", mock_yahoo_get)

    from backtest.data import fetch_vix_regimes
    days = [datetime(2026, 5, 1, tzinfo=ET), datetime(2026, 5, 2, tzinfo=ET)]
    result = fetch_vix_regimes(days)

    assert result == {"2026-05-01": "NORMAL", "2026-05-02": "NORMAL"}


@pytest.mark.unit
def test_cache_miss_fetches_and_saves_parquet(tmp_path, monkeypatch):
    """Cold fetch: _fetch_one_batch is called and a .parquet file is written."""
    import pandas as pd
    from datetime import datetime, timezone

    mock_df = pd.DataFrame(
        {"open": [100.0], "high": [101.0], "low": [99.0],
         "close": [100.5], "volume": [1000]},
        index=pd.DatetimeIndex(
            [pd.Timestamp("2026-05-01 14:30:00", tz="UTC")], name="timestamp"
        ),
    )
    call_count = {"n": 0}

    def mock_fetch_one_batch(client, symbols, start, end):
        call_count["n"] += 1
        return {"AAPL": mock_df}

    monkeypatch.setattr("backtest.data._fetch_one_batch", mock_fetch_one_batch)
    monkeypatch.setattr(
        "backtest.data.StockHistoricalDataClient",
        lambda *a, **kw: None,
    )

    from backtest.data import fetch_5min_bars
    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    end   = datetime(2026, 5, 2, tzinfo=timezone.utc)

    result = fetch_5min_bars(["AAPL"], start, end, cache_dir=tmp_path)

    assert "AAPL" in result
    parquet_files = list(tmp_path.glob("*.parquet"))
    assert len(parquet_files) == 1
    assert call_count["n"] >= 1  # network was called


@pytest.mark.unit
def test_cache_hit_skips_network(tmp_path, monkeypatch):
    """Warm fetch: _fetch_one_batch is NOT called on second invocation."""
    import pandas as pd
    from datetime import datetime, timezone

    mock_df = pd.DataFrame(
        {"open": [100.0], "high": [101.0], "low": [99.0],
         "close": [100.5], "volume": [1000]},
        index=pd.DatetimeIndex(
            [pd.Timestamp("2026-05-01 14:30:00", tz="UTC")], name="timestamp"
        ),
    )
    call_count = {"n": 0}

    def mock_fetch_one_batch(client, symbols, start, end):
        call_count["n"] += 1
        return {"AAPL": mock_df}

    monkeypatch.setattr("backtest.data._fetch_one_batch", mock_fetch_one_batch)
    monkeypatch.setattr(
        "backtest.data.StockHistoricalDataClient",
        lambda *a, **kw: None,
    )

    from backtest.data import fetch_5min_bars
    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    end   = datetime(2026, 5, 2, tzinfo=timezone.utc)

    fetch_5min_bars(["AAPL"], start, end, cache_dir=tmp_path)  # cold
    calls_after_cold = call_count["n"]
    fetch_5min_bars(["AAPL"], start, end, cache_dir=tmp_path)  # warm

    assert call_count["n"] == calls_after_cold  # no additional network calls on warm hit


@pytest.mark.unit
def test_cache_key_differs_for_different_date_ranges(tmp_path):
    """Different date ranges must produce different cache files — no cross-contamination."""
    from datetime import datetime, timezone
    from backtest.data import _cache_path

    symbols = ["AAPL", "TSLA"]
    start_a = datetime(2026, 4, 1, tzinfo=timezone.utc)
    end_a   = datetime(2026, 4, 30, tzinfo=timezone.utc)
    start_b = datetime(2026, 5, 1, tzinfo=timezone.utc)
    end_b   = datetime(2026, 5, 31, tzinfo=timezone.utc)

    path_a = _cache_path(symbols, start_a, end_a, tmp_path)
    path_b = _cache_path(symbols, start_b, end_b, tmp_path)

    assert path_a != path_b


@pytest.mark.unit
def test_cache_key_differs_for_different_symbols(tmp_path):
    """Different symbol sets must produce different cache files."""
    from datetime import datetime, timezone
    from backtest.data import _cache_path

    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    end   = datetime(2026, 5, 31, tzinfo=timezone.utc)

    path_a = _cache_path(["AAPL", "TSLA"], start, end, tmp_path)
    path_b = _cache_path(["AAPL", "NVDA"], start, end, tmp_path)

    assert path_a != path_b
