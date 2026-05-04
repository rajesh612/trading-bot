import logging

import pandas as pd
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    TakeProfitRequest,
    StopLossRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

import config

logger = logging.getLogger(__name__)


class AlpacaClient:
    def __init__(self):
        self._trading = TradingClient(
            config.ALPACA_API_KEY,
            config.ALPACA_SECRET_KEY,
            paper=True,
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
        from market_data import get_latest_price
        return get_latest_price(symbol)

    def get_intraday_bars(self, symbol: str, lookback: int = config.BARS_LOOKBACK) -> pd.DataFrame:
        from market_data import get_intraday_bars
        return get_intraday_bars(symbol, lookback=lookback)

    def get_daily_bars(self, symbol: str, days: int = config.DAILY_BARS_LOOKBACK) -> pd.DataFrame:
        from market_data import get_daily_bars
        return get_daily_bars(symbol, days=days)

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

    def get_open_buy_orders(self) -> list[dict]:
        """Return all pending BUY orders (free Trading API, all Alpaca tiers)."""
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus, OrderSide
        orders = self._trading.get_orders(
            filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, side=OrderSide.BUY)
        )
        return [{"id": str(o.id), "symbol": o.symbol} for o in orders]

    def cancel_order(self, order_id: str) -> None:
        """Cancel a single order by ID."""
        self._trading.cancel_order_by_id(order_id)
