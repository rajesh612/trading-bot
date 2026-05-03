"""
Provider-agnostic market data layer.
MARKET_DATA_PROVIDER env var selects: yahoo | alpaca | hybrid
"""
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests

import config

ET = ZoneInfo("America/New_York")
_YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0"}


def _yahoo_bars(symbol: str, interval: str, period: str) -> pd.DataFrame:
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?interval={interval}&range={period}"
    )
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=_YAHOO_HEADERS, timeout=10)
            if resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            result = resp.json()["chart"]["result"][0]
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
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1)
    return pd.DataFrame()


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
    from alpaca_client import AlpacaClient
    return AlpacaClient().get_intraday_bars(symbol, lookback)


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

    from alpaca_client import AlpacaClient
    return AlpacaClient().get_daily_bars(symbol, days)


def get_latest_price(symbol: str) -> float:
    """Return most-recent trade price. Hybrid/alpaca use Alpaca; yahoo uses 2-min bar."""
    provider = config.MARKET_DATA_PROVIDER

    if provider in ("alpaca", "hybrid"):
        try:
            from alpaca_client import AlpacaClient
            return AlpacaClient().get_latest_price(symbol)
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


def get_quote(symbol: str) -> dict:
    """Return {bid, ask, last, volume, timestamp}."""
    provider = config.MARKET_DATA_PROVIDER

    if provider in ("alpaca", "hybrid"):
        try:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockLatestQuoteRequest
            client = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)
            req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
            q = client.get_stock_latest_quote(req)[symbol]
            mid = float((q.bid_price + q.ask_price) / 2)
            return {
                "bid": float(q.bid_price),
                "ask": float(q.ask_price),
                "last": mid,
                "volume": 0,
                "timestamp": q.timestamp.isoformat(),
            }
        except Exception:
            pass

    price = get_latest_price(symbol)
    return {
        "bid": price,
        "ask": price,
        "last": price,
        "volume": 0,
        "timestamp": datetime.now(ET).isoformat(),
    }
