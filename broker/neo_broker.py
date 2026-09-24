"""
Kotak Neo Execution Engine.
Supports both realistic PAPER trading (simulated fills, slippage, and regulatory fees)
and LIVE order execution via Kotak Neo API.
Includes persistent position & order caching and real-time broker position synchronization.
"""
import json
import time
import uuid
import logging
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime

from core.models import (
    Order, OrderSide, OrderType, OrderStatus,
    Position, PositionSide, Trade, Quote
)
from core.timeutils import now_ist, today_ist_str
from config.settings import AppConfig, FrictionConfig

logger = logging.getLogger("Broker")


class KotakNeoBroker:
    """
    Execution engine managing active orders, positions, regulatory friction,
    and broker synchronization.
    """
    def __init__(
        self,
        app_config: AppConfig,
        friction_config: FrictionConfig,
        neo_client=None,
        cache_dir: Optional[Path] = Path("data/cache"),
        load_cache: bool = True
    ):
        self.app_config = app_config
        self.friction_config = friction_config
        self.neo_client = neo_client
        self.cache_dir = cache_dir or Path("data/cache")
        self.is_paper = (app_config.trading_mode.upper() == "PAPER")
        self.market_data = None

        self.strategy_name: Optional[str] = None
        self.orders: Dict[str, Order] = {}
        self.trades: List[Trade] = []
        self.positions: Dict[str, Position] = {}
        self.realized_pnl: float = 0.0
        self.total_charges: float = 0.0

        # Load persisted positions and orders from disk
        if load_cache:
            self.load_positions_from_cache()

    def bind_strategy(self, strategy_name: str):
        """Binds this broker instance to a specific strategy for scoped caching and isolation."""
        self.strategy_name = strategy_name
        self.load_positions_from_cache()

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

        est_price = price
        if est_price <= 0 and self.market_data:
            est_price = self.market_data.get_ltp(symbol)

        order = Order(
            order_id=order_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=qty,
            price=price,
            trigger_price=trigger_price,
            status=OrderStatus.PENDING,
            tag=tag or "MANUAL",
            average_price=est_price,
            created_at=now_ist()
        )

        if self.is_paper:
            return self._execute_paper_order(order, stop_loss, target)
        else:
            return self._execute_live_order(order, stop_loss, target)

    def _execute_paper_order(
        self, order: Order, stop_loss: Optional[float] = None, target: Optional[float] = None
    ) -> Order:
        """Simulates an instant market fill with conservative slippage."""
        fill_price = order.price if order.price > 0 else (order.average_price if order.average_price > 0 else 100.0)
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
            price=exec_price,
            timestamp=now_ist()
        )
        self.trades.append(trade)

        self._update_position_from_fill(order, stop_loss, target)
        self.save_positions_to_cache()
        logger.info(
            f"📝 [PAPER] Filled {order.side.value} {order.quantity} {order.symbol} @ ₹{exec_price:.2f}"
        )
        return order

    def cancel_order(self, order_id: str) -> bool:
        """Cancels an order on Kotak Neo or in paper simulation."""
        if self.is_paper:
            if order_id in self.orders:
                self.orders[order_id].status = OrderStatus.CANCELLED
                self.save_positions_to_cache()
                return True
            return False

        if not self.neo_client:
            return False

        try:
            resp = self.neo_client.cancel_order(order_id=order_id)
            logger.info(f"🗑 Cancelled order {order_id}: {resp}")
            if order_id in self.orders:
                self.orders[order_id].status = OrderStatus.CANCELLED
                self.save_positions_to_cache()
            return True
        except Exception as e:
            logger.warning(f"Failed to cancel order {order_id}: {e}")
            return False

    def cancel_open_orders_for_symbol(self, symbol: str):
        """Cancels any pending/resting open orders for a symbol on Kotak Neo before exit."""
        if not self.neo_client:
            return

        try:
            all_orders = self.neo_client.order_report()
            rep_data = all_orders.get("data") if isinstance(all_orders, dict) else all_orders
            if isinstance(rep_data, list):
                for item in rep_data:
                    raw_sym = item.get("trdSym") or item.get("tradingSymbol") or ""
                    if symbol in raw_sym:
                        ord_st = str(item.get("ordSt") or "").lower()
                        if ord_st in ("open", "pending", "trigger_pending", "opn"):
                            oid = str(item.get("nOrdNo") or item.get("orderId") or "")
                            if oid:
                                logger.info(f"Cancelling pending resting order {oid} for {symbol} before exit")
                                self.cancel_order(oid)
        except Exception as e:
            logger.debug(f"Error checking open orders for {symbol}: {e}")

    def _execute_live_order(
        self, order: Order, stop_loss: Optional[float] = None, target: Optional[float] = None
    ) -> Order:
        """Places a real order via Kotak Neo API with unique client order ID and fill price resolution."""
        if not self.neo_client:
            order.status = OrderStatus.REJECTED
            order.message = "Kotak Neo client not connected"
            return order

        try:
            # 1. If this is an exit order closing a position, cancel resting open orders first
            # to prevent accidental double-execution or locked quantity conflicts
            existing_pos = self.positions.get(order.symbol)
            if existing_pos and existing_pos.quantity > 0:
                is_exit = (existing_pos.side == PositionSide.LONG and order.side == OrderSide.SELL) or \
                          (existing_pos.side == PositionSide.SHORT and order.side == OrderSide.BUY)
                if is_exit:
                    self.cancel_open_orders_for_symbol(order.symbol)

            neo_sym = f"{order.symbol}-EQ"
            ord_type_map = {
                OrderType.MARKET: "MKT",
                OrderType.LIMIT: "L",
                OrderType.STOP_LOSS: "SL",
                OrderType.STOP_LOSS_MARKET: "SL-M"
            }
            tx_type = "B" if order.side == OrderSide.BUY else "S"
            base_tag = (order.tag or "ORD")[:6]
            unique_tag = f"{base_tag}_{uuid.uuid4().hex[:7]}"

            resp = self.neo_client.place_order(
                exchange_segment="nse_cm",
                product="MIS",
                price=str(order.price) if order.order_type == OrderType.LIMIT else "0",
                order_type=ord_type_map.get(order.order_type, "MKT"),
                quantity=str(order.quantity),
                validity="DAY",
                trading_symbol=neo_sym,
                transaction_type=tx_type,
                tag=unique_tag
            )

            if isinstance(resp, dict) and resp.get("nOrdNo"):
                order.order_id = str(resp["nOrdNo"])
                order.status = OrderStatus.SUBMITTED

                # Poll immediate fill price from order history and report
                time.sleep(0.5)
                fill_price = 0.0

                # Attempt 1: order_history(order_id=...)
                try:
                    hist = self.neo_client.order_history(order_id=order.order_id)
                    hist_data = hist.get("data") if isinstance(hist, dict) else hist
                    if isinstance(hist_data, list) and hist_data:
                        last_item = hist_data[-1]
                        fill_price = float(last_item.get("avgPrc") or last_item.get("avgPrice") or 0.0)
                        ord_st = str(last_item.get("ordSt") or "").lower()
                        if ord_st in ("complete", "traded", "filled", "trad"):
                            order.status = OrderStatus.FILLED
                    elif isinstance(hist_data, dict):
                        fill_price = float(hist_data.get("avgPrc") or hist_data.get("avgPrice") or 0.0)
                except Exception as e:
                    logger.debug(f"order_history poll error: {e}")

                # Attempt 2: order_report() matching nOrdNo
                if fill_price <= 0:
                    try:
                        rep = self.neo_client.order_report()
                        rep_data = rep.get("data") if isinstance(rep, dict) else rep
                        if isinstance(rep_data, list):
                            for item in rep_data:
                                if str(item.get("nOrdNo")) == order.order_id:
                                    fill_price = float(item.get("avgPrc") or item.get("avgPrice") or item.get("prc") or 0.0)
                                    ord_st = str(item.get("ordSt") or "").lower()
                                    if ord_st in ("complete", "traded", "filled", "trad"):
                                        order.status = OrderStatus.FILLED
                                    break
                    except Exception as e:
                        logger.debug(f"order_report poll error: {e}")

                # Attempt 3: Query live quote from market data layer
                if fill_price <= 0 and self.market_data:
                    fill_price = self.market_data.get_ltp(order.symbol)
                    if fill_price <= 0:
                        q_map = self.market_data.fetch_quotes_batch([order.symbol])
                        q = q_map.get(order.symbol)
                        if q and q.last_price > 0:
                            fill_price = q.last_price

                # Attempt 4: Limit order price if specified
                if fill_price <= 0 and order.price > 0:
                    fill_price = order.price

                order.average_price = round(fill_price, 2) if fill_price > 0 else 0.0
                self.orders[order.order_id] = order
                self._update_position_from_fill(order, stop_loss, target)
                self.save_positions_to_cache()
                logger.info(f"🚀 [LIVE] Order placed: {order.order_id} @ ₹{order.average_price:.2f}")
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

        # Fallback to live LTP if fill price is not yet available so entry_price is never 0
        avg_price = order.average_price
        if avg_price <= 0 and self.market_data:
            avg_price = self.market_data.get_ltp(symbol)
            if avg_price <= 0:
                q_map = self.market_data.fetch_quotes_batch([symbol])
                q = q_map.get(symbol)
                if q and q.last_price > 0:
                    avg_price = q.last_price

        if avg_price <= 0 and order.price > 0:
            avg_price = order.price
        if avg_price <= 0:
            avg_price = 100.0

        if not pos or pos.quantity == 0:
            # Opening new position
            pos_side = PositionSide.LONG if order.side == OrderSide.BUY else PositionSide.SHORT
            self.positions[symbol] = Position(
                symbol=symbol,
                side=pos_side,
                quantity=order.quantity,
                entry_price=round(avg_price, 2),
                current_price=round(avg_price, 2),
                stop_loss=stop_loss,
                trailing_stop=stop_loss,
                target=target,
                highest_price=round(avg_price, 2),
                lowest_price=round(avg_price, 2),
                entry_time=now_ist()
            )
        else:
            # Closing or modifying existing position
            is_closing = (pos.side == PositionSide.LONG and order.side == OrderSide.SELL) or \
                         (pos.side == PositionSide.SHORT and order.side == OrderSide.BUY)

            if is_closing:
                pnl = 0.0
                if pos.side == PositionSide.LONG:
                    pnl = (avg_price - pos.entry_price) * order.quantity
                    charges = self.calculate_charges(pos.entry_price * order.quantity, avg_price * order.quantity)
                else:
                    pnl = (pos.entry_price - avg_price) * order.quantity
                    charges = self.calculate_charges(avg_price * order.quantity, pos.entry_price * order.quantity)

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
            else:
                # Adding to existing position
                new_qty = pos.quantity + order.quantity
                avg_entry = ((pos.entry_price * pos.quantity) + (avg_price * order.quantity)) / new_qty
                pos.entry_price = round(avg_entry, 2)
                pos.quantity = new_qty
                if stop_loss:
                    pos.stop_loss = stop_loss
                    pos.trailing_stop = stop_loss
                if target:
                    pos.target = target

        self.save_positions_to_cache()

    def sync_positions_from_broker(self, market_data_manager=None):
        """
        Synchronizes open positions directly from Kotak Neo API.
        Extracts real-time net quantity, actual average buy/sell prices, and updates positions.
        """
        if not self.neo_client:
            return

        try:
            resp = self.neo_client.positions()
            data = resp.get("data") if isinstance(resp, dict) else resp
            if isinstance(data, list):
                for item in data:
                    raw_sym = item.get("trdSym") or item.get("tradingSymbol") or item.get("sym") or ""
                    sym = raw_sym.replace("-EQ", "").strip()
                    if not sym:
                        continue
                    # 1. Product check: Ignore delivery/CNC holdings
                    prod = str(item.get("prod") or item.get("product") or "").upper()
                    if prod in ("CNC", "DELIVERY"):
                        continue

                    # 2. Strategy isolation: If bound to a strategy, only track positions
                    # belonging to this strategy (already known, or in strategy order history)
                    if self.strategy_name and sym not in self.positions:
                        has_strat_order = any(o.symbol == sym for o in self.orders.values())
                        if not has_strat_order:
                            continue

                    raw_qty = item.get("flNetQty") or item.get("netQty") or item.get("netTrdQtyLot") or 0
                    net_qty = int(raw_qty)

                    buy_avg = 0.0
                    for k in ("buyAvg", "buyAvgPrc", "curBuyAvgPrc", "avgPrice"):
                        try:
                            v = float(item.get(k) or 0.0)
                            if v > 0:
                                buy_avg = v
                                break
                        except (ValueError, TypeError):
                            pass

                    sell_avg = 0.0
                    for k in ("sellAvg", "sellAvgPrc", "curSellAvgPrc"):
                        try:
                            v = float(item.get(k) or 0.0)
                            if v > 0:
                                sell_avg = v
                                break
                        except (ValueError, TypeError):
                            pass

                    # Fallback to total amount / filled qty if avg price is 0
                    if buy_avg <= 0:
                        try:
                            fl_bqty = float(item.get("flBuyQty") or item.get("totalBuyQty") or 0.0)
                            b_amt = float(item.get("buyAmt") or 0.0)
                            if fl_bqty > 0 and b_amt > 0:
                                buy_avg = b_amt / fl_bqty
                        except (ValueError, TypeError):
                            pass

                    if sell_avg <= 0:
                        try:
                            fl_sqty = float(item.get("flSellQty") or item.get("totalSellQty") or 0.0)
                            s_amt = float(item.get("sellAmt") or 0.0)
                            if fl_sqty > 0 and s_amt > 0:
                                sell_avg = s_amt / fl_sqty
                        except (ValueError, TypeError):
                            pass

                    rpnl = 0.0
                    try:
                        rpnl = float(item.get("rpnl") or item.get("realized") or 0.0)
                    except (ValueError, TypeError):
                        pass

                    if net_qty == 0:
                        if sym in self.positions:
                            self.positions[sym].quantity = 0
                            self.positions[sym].realized_pnl = rpnl
                        continue

                    side = PositionSide.LONG if net_qty > 0 else PositionSide.SHORT
                    entry_price = buy_avg if side == PositionSide.LONG else sell_avg

                    # Fallback 1: existing cached entry price if already known and > 0
                    existing = self.positions.get(sym)
                    if entry_price <= 0 and existing and existing.entry_price > 0:
                        entry_price = existing.entry_price

                    # Fallback 2: live quote LTP from market data manager
                    md = market_data_manager or self.market_data
                    if entry_price <= 0 and md:
                        entry_price = md.get_ltp(sym)
                        if entry_price <= 0:
                            q_map = md.fetch_quotes_batch([sym])
                            q = q_map.get(sym)
                            if q and q.last_price > 0:
                                entry_price = q.last_price

                    # Fallback 3: default safe value if still 0
                    if entry_price <= 0:
                        entry_price = 100.0

                    # Fetch live LTP for current_price and accurate P&L calculation
                    ltp = entry_price
                    if md:
                        live_ltp = md.get_ltp(sym)
                        if live_ltp > 0:
                            ltp = live_ltp

                    sl = existing.stop_loss if existing else None
                    tsl = existing.trailing_stop if existing else None
                    tgt = existing.target if existing else None

                    pos = Position(
                        symbol=sym,
                        side=side,
                        quantity=abs(net_qty),
                        entry_price=round(entry_price, 2),
                        current_price=round(ltp, 2),
                        stop_loss=sl,
                        trailing_stop=tsl,
                        target=tgt,
                        highest_price=round(max(entry_price, ltp), 2),
                        lowest_price=round(min(entry_price, ltp), 2),
                        entry_time=existing.entry_time if (existing and existing.entry_time) else now_ist()
                    )
                    pos.update_pnl(ltp)
                    self.positions[sym] = pos
                    logger.info(
                        f"🔄 Synced {sym} from Kotak Neo: {side.value} {abs(net_qty)} @ ₹{entry_price:.2f} "
                        f"(LTP: ₹{ltp:.2f}, P&L: ₹{pos.unrealized_pnl:,.2f})"
                    )

                # Sync total realized P&L directly from Kotak Neo positions
                broker_rpnl = sum(float(item.get("rpnl") or item.get("realized") or 0.0) for item in data)
                if broker_rpnl != 0.0:
                    self.realized_pnl = round(broker_rpnl, 2)

                self.save_positions_to_cache()
        except Exception as e:
            logger.warning(f"Failed to sync positions from Kotak Neo: {e}")

    def sync_orders_from_broker(self):
        """Fetches and syncs all orders placed today directly from Kotak Neo."""
        if not self.neo_client:
            return

        try:
            ord_rep = self.neo_client.order_report()
            data = ord_rep.get("data") if isinstance(ord_rep, dict) else ord_rep
            if isinstance(data, list):
                for item in data:
                    oid = str(item.get("nOrdNo") or item.get("orderId") or "")
                    if not oid:
                        continue
                    raw_sym = item.get("trdSym") or item.get("tradingSymbol") or ""
                    sym = raw_sym.replace("-EQ", "").strip()
                    if not sym:
                        continue

                    tx_type = str(item.get("trnsTp") or item.get("transactionType") or "B").upper()
                    side = OrderSide.BUY if tx_type in ("B", "BUY") else OrderSide.SELL

                    ord_st_raw = str(item.get("ordSt") or item.get("status") or "").lower()
                    if ord_st_raw in ("complete", "traded", "filled", "trad"):
                        status = OrderStatus.FILLED
                    elif ord_st_raw in ("rejected", "rej"):
                        status = OrderStatus.REJECTED
                    elif ord_st_raw in ("cancelled", "can"):
                        status = OrderStatus.CANCELLED
                    else:
                        status = OrderStatus.SUBMITTED

                    qty = int(item.get("qty") or item.get("quantity") or 0)
                    avg_prc = float(item.get("avgPrc") or item.get("avgPrice") or item.get("prc") or 0.0)
                    tag = str(item.get("tag") or item.get("ig") or item.get("ordTag") or "")

                    # Strategy filter: Ignore orders belonging to other strategies or manual trading
                    if self.strategy_name:
                        strat_prefix = self.strategy_name.upper()
                        is_match = (
                            strat_prefix in tag.upper() or
                            tag.upper().startswith("ORB") or
                            "ORB_" in tag.upper() or
                            oid in self.orders
                        )
                        if not is_match:
                            continue

                    if oid not in self.orders or self.orders[oid].status != OrderStatus.FILLED:
                        self.orders[oid] = Order(
                            order_id=oid,
                            symbol=sym,
                            side=side,
                            order_type=OrderType.MARKET,
                            quantity=qty,
                            average_price=avg_prc,
                            status=status,
                            tag=tag,
                            created_at=now_ist()
                        )
                self.save_positions_to_cache()
                logger.info(f"📋 Synced {len(self.orders)} orders from Kotak Neo order report.")
        except Exception as e:
            logger.debug(f"Failed to sync orders from Kotak Neo: {e}")

    def save_positions_to_cache(self):
        """Persists open positions, orders, and P&L summary into data/cache/{date}/{strategy}/."""
        try:
            from core.storage import resolve_cache_file, save_json_atomic
            pos_file = resolve_cache_file("positions", strategy_name=self.strategy_name, base_dir=self.cache_dir)
            ord_file = resolve_cache_file("orders", strategy_name=self.strategy_name, base_dir=self.cache_dir)
            summary_file = resolve_cache_file("summary", strategy_name=self.strategy_name, base_dir=self.cache_dir)

            pos_data = {}
            for sym, p in self.positions.items():
                pos_data[sym] = {
                    "symbol": p.symbol,
                    "side": p.side.value,
                    "quantity": p.quantity,
                    "entry_price": p.entry_price,
                    "current_price": p.current_price,
                    "stop_loss": p.stop_loss,
                    "trailing_stop": p.trailing_stop,
                    "target": p.target,
                    "unrealized_pnl": p.unrealized_pnl,
                    "realized_pnl": p.realized_pnl,
                    "highest_price": p.highest_price,
                    "lowest_price": p.lowest_price,
                    "entry_time": p.entry_time.isoformat() if p.entry_time else None
                }
            save_json_atomic(pos_file, pos_data)

            ord_data = []
            for oid, o in self.orders.items():
                ord_data.append({
                    "order_id": o.order_id,
                    "symbol": o.symbol,
                    "side": o.side.value,
                    "order_type": o.order_type.value,
                    "quantity": o.quantity,
                    "price": o.price,
                    "trigger_price": o.trigger_price,
                    "average_price": o.average_price,
                    "filled_quantity": o.filled_quantity,
                    "status": o.status.value,
                    "message": o.message,
                    "tag": o.tag,
                    "created_at": o.created_at.isoformat() if o.created_at else None
                })
            save_json_atomic(ord_file, ord_data)

            save_json_atomic(summary_file, {
                "realized_pnl": self.realized_pnl,
                "total_charges": self.total_charges
            })
        except Exception as e:
            logger.warning(f"Failed to persist positions/orders: {e}")

    def load_positions_from_cache(self):
        """Restores positions and orders from disk cache."""
        try:
            from core.storage import resolve_cache_file
            pos_file = resolve_cache_file("positions", strategy_name=self.strategy_name, base_dir=self.cache_dir)
            ord_file = resolve_cache_file("orders", strategy_name=self.strategy_name, base_dir=self.cache_dir)
            summary_file = resolve_cache_file("summary", strategy_name=self.strategy_name, base_dir=self.cache_dir)

            if summary_file.exists():
                try:
                    with open(summary_file, "r") as f:
                        sdata = json.load(f)
                    self.realized_pnl = float(sdata.get("realized_pnl", 0.0))
                    self.total_charges = float(sdata.get("total_charges", 0.0))
                except Exception as e:
                    logger.debug(f"Failed to load summary cache: {e}")

            if pos_file.exists():
                with open(pos_file, "r") as f:
                    pos_data = json.load(f)
                for sym, p in pos_data.items():
                    side = PositionSide.LONG if p["side"] == "LONG" else PositionSide.SHORT
                    entry_dt = datetime.fromisoformat(p["entry_time"]) if p.get("entry_time") else now_ist()
                    self.positions[sym] = Position(
                        symbol=p["symbol"],
                        side=side,
                        quantity=p["quantity"],
                        entry_price=p["entry_price"],
                        current_price=p["current_price"],
                        stop_loss=p.get("stop_loss"),
                        trailing_stop=p.get("trailing_stop"),
                        target=p.get("target"),
                        unrealized_pnl=p.get("unrealized_pnl", 0.0),
                        realized_pnl=p.get("realized_pnl", 0.0),
                        highest_price=p.get("highest_price", 0.0),
                        lowest_price=p.get("lowest_price", 0.0),
                        entry_time=entry_dt
                    )
                logger.info(f"Loaded {len(self.positions)} positions from disk cache.")

            if ord_file.exists():
                with open(ord_file, "r") as f:
                    ord_data = json.load(f)
                for o in ord_data:
                    side = OrderSide.BUY if o["side"] == "BUY" else OrderSide.SELL
                    ot = OrderType(o["order_type"])
                    st = OrderStatus(o["status"])
                    created_dt = datetime.fromisoformat(o["created_at"]) if o.get("created_at") else now_ist()
                    self.orders[o["order_id"]] = Order(
                        order_id=o["order_id"],
                        symbol=o["symbol"],
                        side=side,
                        order_type=ot,
                        quantity=o["quantity"],
                        price=o["price"],
                        trigger_price=o.get("trigger_price", 0.0),
                        average_price=o.get("average_price", 0.0),
                        filled_quantity=o.get("filled_quantity", 0),
                        status=st,
                        message=o.get("message", ""),
                        tag=o.get("tag", ""),
                        created_at=created_dt
                    )
                logger.info(f"Loaded {len(self.orders)} orders from disk cache.")
        except Exception as e:
            logger.warning(f"Failed to load positions/orders from cache: {e}")

    def get_positions(self) -> List[Position]:
        return [p for p in self.positions.values() if p.quantity > 0]

    def get_order_book(self) -> List[Order]:
        return list(self.orders.values())

    def get_trade_book(self) -> List[Trade]:
        return self.trades
