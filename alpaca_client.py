import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    GetOrdersRequest,
    TakeProfitRequest,
    StopLossRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus, OrderClass
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

import config

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


class AlpacaClient:
    def __init__(self):
        self._trading = TradingClient(
            config.ALPACA_API_KEY,
            config.ALPACA_SECRET_KEY,
            paper=True,
        )
        self._data = StockHistoricalDataClient(
            config.ALPACA_API_KEY,
            config.ALPACA_SECRET_KEY,
        )

    def get_account(self):
        return self._trading.get_account()

    def get_portfolio_value(self) -> float:
        return float(self.get_account().portfolio_value)

    def get_buying_power(self) -> float:
        return float(self.get_account().buying_power)

    def get_positions(self) -> dict:
        return {p.symbol: p for p in self._trading.get_all_positions()}

    def get_latest_price(self, symbol: str) -> float:
        req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        quote = self._data.get_stock_latest_quote(req)[symbol]
        return float((quote.ask_price + quote.bid_price) / 2)

    def get_intraday_bars(self, symbol: str, lookback: int = 100) -> pd.DataFrame:
        end = datetime.now(ET)
        start = end - timedelta(days=5)
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame(5, TimeFrameUnit.Minute),
            start=start,
            end=end,
            feed="iex",
        )
        bars = self._data.get_stock_bars(req)
        df = bars.df
        df = self._extract_symbol(df, symbol)
        return df.tail(lookback) if df is not None else pd.DataFrame()

    def get_daily_bars(self, symbol: str, days: int = 30) -> pd.DataFrame:
        end = datetime.now(ET)
        start = end - timedelta(days=days + 10)
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            feed="iex",
        )
        bars = self._data.get_stock_bars(req)
        df = bars.df
        df = self._extract_symbol(df, symbol)
        return df.tail(days) if df is not None else pd.DataFrame()

    @staticmethod
    def _extract_symbol(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
        if df is None or df.empty:
            return pd.DataFrame()
        if isinstance(df.index, pd.MultiIndex):
            if symbol in df.index.get_level_values("symbol"):
                return df.xs(symbol, level="symbol")
            return pd.DataFrame()
        return df

    def place_market_order(self, symbol: str, qty: float, side: str) -> dict:
        req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY if side.upper() == "BUY" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        order = self._trading.submit_order(req)
        logger.info(f"Order placed: {side} {qty:.0f}x {symbol} | id={order.id}")
        return {"id": str(order.id), "symbol": symbol, "side": side, "qty": qty}

    def place_bracket_order(
        self,
        symbol: str,
        qty: float,
        entry_price: float,
        stop_loss_pct: float,
        take_profit_pct: float,
    ) -> dict:
        """
        Places a BUY bracket order. Alpaca attaches a stop-loss and take-profit
        order server-side — they fire the instant price crosses either level,
        regardless of our polling interval.
        """
        sl_price  = round(entry_price * (1 - stop_loss_pct  / 100), 2)
        tp_price  = round(entry_price * (1 + take_profit_pct / 100), 2)

        req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=tp_price),
            stop_loss=StopLossRequest(stop_price=sl_price),
        )
        order = self._trading.submit_order(req)
        logger.info(
            f"Bracket order: BUY {qty:.0f}x {symbol} | "
            f"SL=${sl_price:.2f} TP=${tp_price:.2f} | id={order.id}"
        )
        return {
            "id": str(order.id),
            "symbol": symbol,
            "side": "BUY",
            "qty": qty,
            "stop_loss_price": sl_price,
            "take_profit_price": tp_price,
        }

    def close_position(self, symbol: str) -> None:
        try:
            self._trading.close_position(symbol)
            logger.info(f"Position closed: {symbol}")
        except Exception as exc:
            logger.warning(f"Could not close {symbol}: {exc}")

    def close_all_positions(self) -> None:
        try:
            self._trading.close_all_positions(cancel_orders=True)
            logger.info("All positions closed.")
        except Exception as exc:
            logger.error(f"Error closing all positions: {exc}")

    def cancel_all_orders(self) -> None:
        self._trading.cancel_orders()
