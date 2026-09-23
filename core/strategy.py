"""
StrategyBase - The clean, intuitive strategy development interface.
Provides standard high-level methods (buy, sell, get_quote, get_history, get_positions)
while abstracting broker interaction, risk management, and safety guardrails.
"""
from abc import ABC, abstractmethod
from typing import Dict, List, Optional
import pandas as pd

from core.models import (
    Order, OrderSide, OrderType, OrderStatus,
    Position, PositionSide, PositionState, Quote, Bar, ScreenerCandidate
)
from core.guardrails import OrderSafetyLock
from core.risk import RiskManager
from core.timeutils import TimeManager


class StrategyBase(ABC):
    """
    Base class for algorithmic trading strategies.
    Implement on_screener_ready(), on_tick(), on_bar(), and on_square_off().
    """
    def __init__(self, name: str):
        self.name = name
        self.broker = None
        self.risk_manager: Optional[RiskManager] = None
        self.guardrails: Optional[OrderSafetyLock] = None
        self.time_manager: Optional[TimeManager] = None
        self.market_data = None
        self.historical_data = None
        self.notifier = None

    def bind_context(
        self,
        broker,
        risk_manager: RiskManager,
        guardrails: OrderSafetyLock,
        time_manager: TimeManager,
        market_data,
        historical_data,
        notifier=None
    ):
        """Injects system dependencies into the strategy."""
        self.broker = broker
        self.risk_manager = risk_manager
        self.guardrails = guardrails
        self.time_manager = time_manager
        self.market_data = market_data
        self.historical_data = historical_data
        self.notifier = notifier

    # =========================================================================
    # High-Level Strategy API
    # =========================================================================

    def buy(
        self,
        symbol: str,
        qty: int,
        order_type: str = "MKT",
        limit_price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        target: Optional[float] = None,
        tag: str = ""
    ) -> Optional[Order]:
        """
        Executes a BUY order with full guardrails and risk verification.
        """
        quote = self.get_quote(symbol)
        price = limit_price or (quote.last_price if quote else 0.0)

        # 1. Guardrails check
        allowed, reason = self.guardrails.validate_new_entry(
            symbol, OrderSide.BUY, qty, price
        )
        if not allowed:
            if self.notifier:
                self.notifier.notify_guardrail_rejection(symbol, "BUY", reason)
            return None

        # 2. Place order via broker
        order = self.broker.place_order(
            symbol=symbol,
            side=OrderSide.BUY,
            qty=qty,
            order_type=OrderType(order_type),
            price=limit_price or 0.0,
            stop_loss=stop_loss,
            target=target,
            tag=tag
        )

        if order and order.status in (OrderStatus.SUBMITTED, OrderStatus.FILLED):
            self.guardrails.set_state(symbol, PositionState.OPEN, PositionSide.LONG)
            if self.notifier:
                self.notifier.notify_trade_entry(order, stop_loss, target)

        return order

    def sell(
        self,
        symbol: str,
        qty: int,
        order_type: str = "MKT",
        limit_price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        target: Optional[float] = None,
        tag: str = ""
    ) -> Optional[Order]:
        """
        Executes a SELL / SHORT order with full guardrails and risk verification.
        """
        quote = self.get_quote(symbol)
        price = limit_price or (quote.last_price if quote else 0.0)

        allowed, reason = self.guardrails.validate_new_entry(
            symbol, OrderSide.SELL, qty, price
        )
        if not allowed:
            if self.notifier:
                self.notifier.notify_guardrail_rejection(symbol, "SELL", reason)
            return None

        order = self.broker.place_order(
            symbol=symbol,
            side=OrderSide.SELL,
            qty=qty,
            order_type=OrderType(order_type),
            price=limit_price or 0.0,
            stop_loss=stop_loss,
            target=target,
            tag=tag
        )

        if order and order.status in (OrderStatus.SUBMITTED, OrderStatus.FILLED):
            self.guardrails.set_state(symbol, PositionState.OPEN, PositionSide.SHORT)
            if self.notifier:
                self.notifier.notify_trade_entry(order, stop_loss, target)

        return order

    def close_position(self, symbol: str, reason: str = "SIGNAL") -> Optional[Order]:
        """
        Closes an open position safely and updates internal state locks.
        """
        pos = self.get_position(symbol)
        if not pos or pos.quantity == 0:
            return None

        allowed, val_msg = self.guardrails.validate_exit(
            symbol, pos.quantity, is_forced=(reason == "FORCED_SQUARE_OFF")
        )
        if not allowed:
            return None

        self.guardrails.set_state(symbol, PositionState.PENDING_EXIT)
        exit_side = OrderSide.SELL if pos.side == PositionSide.LONG else OrderSide.BUY

        order = self.broker.place_order(
            symbol=symbol,
            side=exit_side,
            qty=pos.quantity,
            order_type=OrderType.MARKET,
            tag=f"CLOSE_{reason}"
        )

        if order and order.status in (OrderStatus.SUBMITTED, OrderStatus.FILLED):
            self.guardrails.set_state(symbol, PositionState.CLOSED)
            if self.notifier:
                self.notifier.notify_trade_exit(pos, order, reason)

        return order

    def close_all_positions(self, reason: str = "FORCED_SQUARE_OFF"):
        """Closes all currently open intraday positions."""
        for pos in self.get_positions():
            if pos.quantity > 0:
                self.close_position(pos.symbol, reason=reason)

    def get_quote(self, symbol: str) -> Optional[Quote]:
        """Fetches latest quote from market data layer."""
        if self.market_data:
            return self.market_data.get_quote(symbol)
        return None

    def get_history(self, symbol: str, days: int = 30) -> pd.DataFrame:
        """Fetches historical OHLCV data from 30-day cache manager."""
        if self.historical_data:
            return self.historical_data.get_history(symbol, days=days)
        return pd.DataFrame()

    def get_positions(self) -> List[Position]:
        """Returns all currently active positions from broker."""
        if self.broker:
            return self.broker.get_positions()
        return []

    def get_position(self, symbol: str) -> Optional[Position]:
        """Returns specific position for symbol if open."""
        for p in self.get_positions():
            if p.symbol == symbol:
                return p
        return None

    # =========================================================================
    # Strategy Lifecycle Hooks
    # =========================================================================

    def on_start(self):
        """Called once when trading engine initializes."""
        pass

    def on_screener_ready(self, candidates: List[ScreenerCandidate]):
        """Called at 09:20 IST when the opening range candidates are calculated."""
        pass

    def on_tick(self, symbol: str, quote: Quote):
        """Called on every real-time price tick or quote update."""
        pass

    def on_bar(self, symbol: str, bar: Bar):
        """Called on every candle completion (e.g. 5-minute candle)."""
        pass

    def on_square_off(self):
        """Called at 14:45 IST to enforce closing of all positions."""
        self.close_all_positions(reason="FORCED_SQUARE_OFF")
