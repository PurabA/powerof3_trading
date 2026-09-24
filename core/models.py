"""
Domain data models for orders, positions, trades, quotes, and screener results.
"""
from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, Any


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MKT"
    LIMIT = "LMT"
    STOP_LOSS = "SL"
    STOP_LOSS_MARKET = "SL-M"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class PositionSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


class PositionState(str, Enum):
    IDLE = "IDLE"
    PENDING_ENTRY = "PENDING_ENTRY"
    OPEN = "OPEN"
    PENDING_EXIT = "PENDING_EXIT"
    CLOSED = "CLOSED"


@dataclass
class Quote:
    symbol: str
    last_price: float
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: int = 0
    timestamp: Optional[datetime] = None


@dataclass
class Bar:
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass
class Order:
    order_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: int
    price: float = 0.0
    trigger_price: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    filled_quantity: int = 0
    average_price: float = 0.0
    created_at: datetime = field(default_factory=datetime.now)
    message: str = ""
    tag: str = ""


@dataclass
class Position:
    symbol: str
    side: PositionSide
    quantity: int
    entry_price: float
    current_price: float
    stop_loss: Optional[float] = None
    trailing_stop: Optional[float] = None
    target: Optional[float] = None
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    highest_price: float = 0.0
    lowest_price: float = 0.0
    entry_time: Optional[datetime] = None

    def update_pnl(self, ltp: float):
        self.current_price = ltp
        if self.side == PositionSide.LONG:
            self.unrealized_pnl = (ltp - self.entry_price) * self.quantity
            if ltp > self.highest_price:
                self.highest_price = ltp
        elif self.side == PositionSide.SHORT:
            self.unrealized_pnl = (self.entry_price - ltp) * self.quantity
            if ltp < self.lowest_price or self.lowest_price == 0.0:
                self.lowest_price = ltp


@dataclass
class Trade:
    trade_id: str
    order_id: str
    symbol: str
    side: OrderSide
    quantity: int
    price: float
    timestamp: datetime = field(default_factory=datetime.now)
    brokerage: float = 0.0
    taxes: float = 0.0
    pnl: float = 0.0


@dataclass
class ScreenerCandidate:
    symbol: str
    neo_symbol: str
    direction: str  # "LONG", "SHORT", "DOJI"
    opening_high: float
    opening_low: float
    opening_open: float
    opening_close: float
    opening_volume: int
    baseline_opening_volume: float
    rvol: float
    atr_14d: float
    avg_vol_14d: float
    avg_turnover_14d: float
    trigger_price: float
    rank: int = 0
    current_ltp: float = 0.0
