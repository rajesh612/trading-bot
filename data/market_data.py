"""
Provider-agnostic market data layer.
MARKET_DATA_PROVIDER env var selects: yahoo | alpaca | hybrid
"""
from datetime import datetime, timedelta
import pandas as pd

import config
from utils.constants import ET
from utils.http import yahoo_get


def _alpaca_bars(symbol: str, timeframe, start: datetime, limit: int) -> pd.DataFrame:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    client = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)
    req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=timeframe, start=start, limit=limit)
    bars = client.get_stock_bars(req).get(symbol, [])
    if not bars:
        return pd.DataFrame()
    df = pd.DataFrame(
        [{"open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": float(b.volume)}
         for b in bars],
        index=pd.DatetimeIndex([b.timestamp for b in bars], tz="UTC").tz_convert(ET),
    )
    df.index.name = "timestamp"
    return df


def _yahoo_bars(symbol: str, interval: str, period: str) -> pd.DataFrame:
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?interval={interval}&range={period}"
    )
    data = yahoo_get(url)
    result = data["chart"]["result"][0]
    timestamps = result["timestamp"]
    q = result["indicators"]["quote"][0]
    df = pd.DataFrame(
        {
            "open":   [float(v) if v is not None else float("nan") for v in q["open"]],
            "high":   [float(v) if v is not None else float("nan") for v in q["high"]],
            "low":    [float(v) if v is not None else float("nan") for v in q["low"]],
            "close":  [float(v) if v is not None else float("nan") for v in q["close"]],
            "volume": [float(v) if v is not None else 0.0 for v in q["volume"]],
        },
        index=pd.to_datetime(timestamps, unit="s", utc=True).tz_convert(ET),
    )
    df.index.name = "timestamp"
    return df.dropna(subset=["close"])


def get_intraday_bars(
    symbol: str,
    lookback: int = config.BARS_LOOKBACK,
    timeframe_min: int = config.BAR_TIMEFRAME_MINUTES,
) -> pd.DataFrame:
    """Return up to `lookback` bars of intraday OHLCV with tz-aware DatetimeIndex."""
    provider = config.MARKET_DATA_PROVIDER
    interval = f"{timeframe_min}m"

    if provider in ("yahoo", "hybrid"):
        try:
            df = _yahoo_bars(symbol, interval, "5d")
            return df.tail(lookback) if not df.empty else df
        except Exception:
            if provider != "hybrid":
                return pd.DataFrame()

    # alpaca or hybrid fallback
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    tf = TimeFrame(config.BAR_TIMEFRAME_MINUTES, TimeFrameUnit.Minute)
    start = datetime.now(ET) - timedelta(days=5)
    df = _alpaca_bars(symbol, tf, start, lookback)
    return df.tail(lookback) if not df.empty else df


def get_daily_bars(symbol: str, days: int = config.DAILY_BARS_LOOKBACK) -> pd.DataFrame:
    """Return up to `days` daily OHLCV bars with tz-aware DatetimeIndex."""
    provider = config.MARKET_DATA_PROVIDER

    if provider in ("yahoo", "hybrid"):
        try:
            df = _yahoo_bars(symbol, "1d", f"{days + 10}d")
            return df.tail(days) if not df.empty else df
        except Exception:
            if provider != "hybrid":
                return pd.DataFrame()

    from alpaca.data.timeframe import TimeFrame
    start = datetime.now(ET) - timedelta(days=days + 10)
    df = _alpaca_bars(symbol, TimeFrame.Day, start, days)
    return df.tail(days) if not df.empty else df


def get_latest_price(symbol: str) -> float:
    """Return most-recent trade price. Hybrid/alpaca use Alpaca; yahoo uses 2-min bar."""
    provider = config.MARKET_DATA_PROVIDER

    if provider in ("alpaca", "hybrid"):
        try:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockLatestTradeRequest
            client = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)
            req = StockLatestTradeRequest(symbol_or_symbols=symbol)
            trade = client.get_stock_latest_trade(req)[symbol]
            return float(trade.price)
        except Exception:
            pass

    # Yahoo fallback (15-min delayed on free tier — acceptable for paper trading)
    try:
        df = _yahoo_bars(symbol, "2m", "1d")
        if not df.empty:
            return float(df["close"].iloc[-1])
    except Exception:
        pass

    raise ValueError(f"Cannot fetch latest price for {symbol}")
