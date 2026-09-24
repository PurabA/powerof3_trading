"""
Kotak Neo Execution Engine.
Supports both realistic PAPER trading (simulated fills, slippage, and regulatory fees)
and LIVE order execution via Kotak Neo API.
"""
import uuid
import logging
from typing import Dict, List, Optional
from datetime import datetime

from core.models import (
    Order, OrderSide, OrderType, OrderStatus,
    Position, PositionSide, Trade, Quote
)
from config.settings import AppConfig, FrictionConfig

logger = logging.getLogger("Broker")


class KotakNeoBroker:
    """
    Execution engine managing active orders, positions, and regulatory friction.
    """
    def __init__(self, app_config: AppConfig, friction_config: FrictionConfig, neo_client=None):
        self.app_config = app_config
        self.friction_config = friction_config
        self.neo_client = neo_client
        self.is_paper = (app_config.trading_mode.upper() == "PAPER")

        self.orders: Dict[str, Order] = {}
        self.trades: List[Trade] = []
        self.positions: Dict[str, Position] = {}
        self.realized_pnl: float = 0.0
        self.total_charges: float = 0.0

    def calculate_charges(self, buy_value: float, sell_value: float) -> Dict[str, float]:
        """Calculates exact Indian regulatory charges for intraday cash equity."""
        turnover = buy_value + sell_value
        cfg = self.friction_config

        brokerage = min(cfg.brokerage_per_order, buy_value * cfg.brokerage_pct) + \
                    min(cfg.brokerage_per_order, sell_value * cfg.brokerage_pct)
        stt = sell_value * cfg.stt_pct_sell
        exchange = turnover * cfg.exchange_txn_charge_pct
        sebi = turnover * cfg.sebi_turnover_pct
        stamp_duty = buy_value * cfg.stamp_duty_buy_pct
        gst = (brokerage + exchange + sebi) * cfg.gst_pct
        total = brokerage + stt + exchange + sebi + stamp_duty + gst

        return {
            "brokerage": round(brokerage, 2),
            "stt": round(stt, 2),
            "exchange": round(exchange, 2),
            "sebi": round(sebi, 2),
            "stamp_duty": round(stamp_duty, 2),
            "gst": round(gst, 2),
            "total": round(total, 2)
        }

    def place_order(
        self,
        symbol: str,
        side: OrderSide,
        qty: int,
        order_type: OrderType = OrderType.MARKET,
        price: float = 0.0,
        trigger_price: float = 0.0,
        stop_loss: Optional[float] = None,
        target: Optional[float] = None,
        tag: str = ""
    ) -> Order:
        """Places an order in PAPER or LIVE mode."""
        order_id = f"ORD_{uuid.uuid4().hex[:10].upper()}"
        
        order = Order(
            order_id=order_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=qty,
            price=price,
            trigger_price=trigger_price,
            status=OrderStatus.PENDING,
            tag=tag
        )

        if self.is_paper:
            return self._execute_paper_order(order, stop_loss, target)
        else:
            return self._execute_live_order(order, stop_loss, target)

    def _execute_paper_order(
        self, order: Order, stop_loss: Optional[float] = None, target: Optional[float] = None
    ) -> Order:
        """Simulates an instant market fill with conservative slippage."""
        # Use provided price or default estimate
        fill_price = order.price if order.price > 0 else 100.0
        slippage_mult = 1.001 if order.side == OrderSide.BUY else 0.999
        exec_price = round(fill_price * slippage_mult, 2)

        order.status = OrderStatus.FILLED
        order.filled_quantity = order.quantity
        order.average_price = exec_price
        self.orders[order.order_id] = order

        trade = Trade(
            trade_id=f"TRD_{uuid.uuid4().hex[:8].upper()}",
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price=exec_price
        )
        self.trades.append(trade)

        self._update_position_from_fill(order, stop_loss, target)
        logger.info(
            f"📝 [PAPER] Filled {order.side.value} {order.quantity} {order.symbol} @ ₹{exec_price:.2f}"
        )
        return order

    def _execute_live_order(
        self, order: Order, stop_loss: Optional[float] = None, target: Optional[float] = None
    ) -> Order:
        """Places a real order via Kotak Neo API."""
        if not self.neo_client:
            order.status = OrderStatus.REJECTED
            order.message = "Kotak Neo client not connected"
            return order

        try:
            neo_sym = f"{order.symbol}-EQ"
            ord_type_map = {
                OrderType.MARKET: "MKT",
                OrderType.LIMIT: "L",
                OrderType.STOP_LOSS: "SL",
                OrderType.STOP_LOSS_MARKET: "SL-M"
            }
            tx_type = "B" if order.side == OrderSide.BUY else "S"

            resp = self.neo_client.place_order(
                exchange_segment="nse_cm",
                product="MIS",
                price=str(order.price) if order.order_type == OrderType.LIMIT else "0",
                order_type=ord_type_map.get(order.order_type, "MKT"),
                quantity=str(order.quantity),
                validity="DAY",
                trading_symbol=neo_sym,
                transaction_type=tx_type,
                tag=order.tag[:15] if order.tag else None
            )

            if isinstance(resp, dict) and resp.get("nOrdNo"):
                order.order_id = str(resp["nOrdNo"])
                order.status = OrderStatus.SUBMITTED
                self.orders[order.order_id] = order
                self._update_position_from_fill(order, stop_loss, target)
                logger.info(f"🚀 [LIVE] Order placed successfully: {order.order_id}")
            else:
                order.status = OrderStatus.REJECTED
                order.message = str(resp)
                logger.error(f"❌ [LIVE] Order placement rejected: {resp}")

            return order

        except Exception as e:
            logger.error(f"❌ [LIVE] Order failed: {e}")
            order.status = OrderStatus.REJECTED
            order.message = str(e)
            return order

    def _update_position_from_fill(
        self, order: Order, stop_loss: Optional[float] = None, target: Optional[float] = None
    ):
        symbol = order.symbol
        pos = self.positions.get(symbol)

        if not pos or pos.quantity == 0:
            # Opening new position
            pos_side = PositionSide.LONG if order.side == OrderSide.BUY else PositionSide.SHORT
            self.positions[symbol] = Position(
                symbol=symbol,
                side=pos_side,
                quantity=order.quantity,
                entry_price=order.average_price,
                current_price=order.average_price,
                stop_loss=stop_loss,
                trailing_stop=stop_loss,
                target=target,
                highest_price=order.average_price,
                lowest_price=order.average_price,
                entry_time=datetime.now()
            )
        else:
            # Closing or modifying existing position
            is_closing = (pos.side == PositionSide.LONG and order.side == OrderSide.SELL) or \
                         (pos.side == PositionSide.SHORT and order.side == OrderSide.BUY)
            
            if is_closing:
                pnl = 0.0
                if pos.side == PositionSide.LONG:
                    pnl = (order.average_price - pos.entry_price) * order.quantity
                    charges = self.calculate_charges(pos.entry_price * order.quantity, order.average_price * order.quantity)
                else:
                    pnl = (pos.entry_price - order.average_price) * order.quantity
                    charges = self.calculate_charges(order.average_price * order.quantity, pos.entry_price * order.quantity)

                net_pnl = pnl - charges["total"]
                self.realized_pnl += net_pnl
                self.total_charges += charges["total"]

                pos.realized_pnl += net_pnl
                pos.quantity -= order.quantity
                
                if pos.quantity <= 0:
                    pos.quantity = 0
                    pos.side = PositionSide.FLAT
                    logger.info(
                        f"🏁 Closed {symbol} | Net P&L: ₹{net_pnl:,.2f} "
                        f"(Gross: ₹{pnl:,.2f}, Charges: ₹{charges['total']:.2f})"
                    )

    def get_positions(self) -> List[Position]:
        return [p for p in self.positions.values() if p.quantity > 0]

    def get_order_book(self) -> List[Order]:
        return list(self.orders.values())

    def get_trade_book(self) -> List[Trade]:
        return self.trades
