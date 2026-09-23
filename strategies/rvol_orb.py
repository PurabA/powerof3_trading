"""
Trexquant 5-Minute Relative Volume (RVOL) Opening Range Breakout Strategy.
Based on: Zarattini, Barbon, Aziz (2024) - Swiss Finance Institute
Spans Nifty 500 equities with strict directional bias and trailing stops.
"""
import logging
from typing import Dict, List, Optional
from datetime import datetime

from core.strategy import StrategyBase
from core.models import (
    Quote, Bar, Position, PositionSide, ScreenerCandidate, OrderSide
)
from config.settings import StrategyConfig

logger = logging.getLogger("RVOL_ORB_Strategy")


class RvolOrbStrategy(StrategyBase):
    """
    RVOL 5-Minute Opening Range Breakout Strategy implementation.
    """
    def __init__(self, config: StrategyConfig):
        super().__init__(name="Trexquant_RVOL_ORB_5m")
        self.config = config
        self.candidates: Dict[str, ScreenerCandidate] = {}
        self.traded_symbols: set = set()

    def on_screener_ready(self, candidates: List[ScreenerCandidate]):
        """
        Invoked at 09:20 IST after the first 5-minute candle has formed.
        Stores Top-N candidates ranked by RVOL.
        """
        self.candidates.clear()
        for cand in candidates:
            self.candidates[cand.symbol] = cand
        logger.info(
            f"🎯 Screener loaded {len(self.candidates)} stocks in play. "
            f"Listening for breakout triggers..."
        )
        if self.notifier:
            self.notifier.notify_morning_screener(candidates)

    def on_tick(self, symbol: str, quote: Quote):
        """
        Evaluates real-time price ticks for breakout triggers and active stop trailing.
        """
        ltp = quote.last_price
        if ltp <= 0:
            return

        # 1. Manage Existing Open Position (Trailing Stop / Target / Stop Loss)
        pos = self.get_position(symbol)
        if pos and pos.quantity > 0:
            self._manage_open_position(pos, ltp)
            return

        # 2. Check for New Breakout Entry (between 09:20 and 14:15 IST)
        if symbol not in self.candidates:
            return

        if symbol in self.traded_symbols:
            return  # Already took today's trade for this symbol

        cand = self.candidates[symbol]
        direction = cand.direction
        atr = cand.atr_14d

        if direction == "LONG" and ltp >= cand.opening_high:
            self._trigger_long_entry(symbol, cand, ltp, atr)

        elif direction == "SHORT" and ltp <= cand.opening_low:
            self._trigger_short_entry(symbol, cand, ltp, atr)

    def _trigger_long_entry(
        self, symbol: str, cand: ScreenerCandidate, ltp: float, atr: float
    ):
        """Triggers long breakout entry."""
        stop_dist = self.config.atr_stop_loss_pct * atr
        stop_loss = round(ltp - stop_dist, 2)
        target = round(ltp + (self.config.profit_target_pct * atr), 2) if self.config.profit_target_pct else None

        open_positions = self.get_positions()
        shares, reason = self.risk_manager.calculate_position_size(
            symbol, ltp, stop_loss, open_positions
        )
        if shares <= 0:
            logger.warning(f"Sizing rejected long on {symbol}: {reason}")
            return

        logger.info(
            f"⚡ LONG BREAKOUT on {symbol} @ ₹{ltp:.2f} (Trigger High: ₹{cand.opening_high:.2f}) | "
            f"Qty: {shares}, SL: ₹{stop_loss:.2f}"
        )
        order = self.buy(
            symbol=symbol,
            qty=shares,
            order_type="MKT",
            limit_price=ltp,
            stop_loss=stop_loss,
            target=target,
            tag="ORB_LONG"
        )
        if order:
            self.traded_symbols.add(symbol)

    def _trigger_short_entry(
        self, symbol: str, cand: ScreenerCandidate, ltp: float, atr: float
    ):
        """Triggers short breakout entry."""
        stop_dist = self.config.atr_stop_loss_pct * atr
        stop_loss = round(ltp + stop_dist, 2)
        target = round(ltp - (self.config.profit_target_pct * atr), 2) if self.config.profit_target_pct else None

        open_positions = self.get_positions()
        shares, reason = self.risk_manager.calculate_position_size(
            symbol, ltp, stop_loss, open_positions
        )
        if shares <= 0:
            logger.warning(f"Sizing rejected short on {symbol}: {reason}")
            return

        logger.info(
            f"⚡ SHORT BREAKOUT on {symbol} @ ₹{ltp:.2f} (Trigger Low: ₹{cand.opening_low:.2f}) | "
            f"Qty: {shares}, SL: ₹{stop_loss:.2f}"
        )
        order = self.sell(
            symbol=symbol,
            qty=shares,
            order_type="MKT",
            limit_price=ltp,
            stop_loss=stop_loss,
            target=target,
            tag="ORB_SHORT"
        )
        if order:
            self.traded_symbols.add(symbol)

    def _manage_open_position(self, pos: Position, ltp: float):
        """
        Manages an active position: updates P&L, trails stop loss, and executes exits.
        """
        pos.update_pnl(ltp)
        cand = self.candidates.get(pos.symbol)
        atr = cand.atr_14d if cand else (pos.entry_price * 0.01)

        # LONG POSITION CHECKS
        if pos.side == PositionSide.LONG:
            # Trailing stop update (ratchet upwards only)
            if self.config.use_trailing_stop:
                trail_step = self.config.trailing_stop_atr_pct * atr
                candidate_stop = round(pos.highest_price - trail_step, 2)
                if pos.trailing_stop is None or candidate_stop > pos.trailing_stop:
                    pos.trailing_stop = candidate_stop

            active_stop = pos.trailing_stop if pos.trailing_stop else pos.stop_loss

            # 1. Stop loss check
            if active_stop and ltp <= active_stop:
                logger.info(f"🛑 Trailing stop hit on {pos.symbol} @ ₹{ltp:.2f} (SL: ₹{active_stop:.2f})")
                self.close_position(pos.symbol, reason="TRAILING_STOP")
                return

            # 2. Profit target check
            if pos.target and ltp >= pos.target:
                logger.info(f"🎯 Profit target hit on {pos.symbol} @ ₹{ltp:.2f} (Target: ₹{pos.target:.2f})")
                self.close_position(pos.symbol, reason="TAKE_PROFIT")
                return

        # SHORT POSITION CHECKS
        elif pos.side == PositionSide.SHORT:
            # Trailing stop update (ratchet downwards only)
            if self.config.use_trailing_stop:
                trail_step = self.config.trailing_stop_atr_pct * atr
                candidate_stop = round(pos.lowest_price + trail_step, 2)
                if pos.trailing_stop is None or candidate_stop < pos.trailing_stop:
                    pos.trailing_stop = candidate_stop

            active_stop = pos.trailing_stop if pos.trailing_stop else pos.stop_loss

            # 1. Stop loss check
            if active_stop and ltp >= active_stop:
                logger.info(f"🛑 Trailing stop hit on {pos.symbol} @ ₹{ltp:.2f} (SL: ₹{active_stop:.2f})")
                self.close_position(pos.symbol, reason="TRAILING_STOP")
                return

            # 2. Profit target check
            if pos.target and ltp <= pos.target:
                logger.info(f"🎯 Profit target hit on {pos.symbol} @ ₹{ltp:.2f} (Target: ₹{pos.target:.2f})")
                self.close_position(pos.symbol, reason="TAKE_PROFIT")
                return
