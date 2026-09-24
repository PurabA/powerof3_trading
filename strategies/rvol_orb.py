"""
Trexquant 5-Minute Relative Volume (RVOL) Opening Range Breakout Strategy.
Based on: Zarattini, Barbon, Aziz (2024) - Swiss Finance Institute
Spans Nifty 500 equities with strict directional bias, percentage target profits,
trailing stops, and daily traded-symbol persistence across reboots.
"""
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime

from core.strategy import StrategyBase
from core.models import (
    Quote, Bar, Position, PositionSide, ScreenerCandidate, OrderSide
)
from core.timeutils import today_ist_str
from config.settings import StrategyConfig

logger = logging.getLogger("RVOL_ORB_Strategy")


class RvolOrbStrategy(StrategyBase):
    """
    RVOL 5-Minute Opening Range Breakout Strategy implementation.
    """
    def __init__(
        self,
        config: StrategyConfig,
        cache_dir: Optional[Path] = Path("data/cache"),
        load_cache: bool = True
    ):
        super().__init__(name="Trexquant_RVOL_ORB_5m")
        self.config = config
        self.cache_dir = cache_dir or Path("data/cache")
        self.candidates: Dict[str, ScreenerCandidate] = {}
        self.traded_symbols: set = set()
        if load_cache:
            self.load_traded_symbols_from_cache()

    def mark_symbol_traded(self, symbol: str):
        """Marks a symbol as traded today and persists to disk cache."""
        self.traded_symbols.add(symbol)
        self.save_traded_symbols_to_cache()

    def save_traded_symbols_to_cache(self):
        """Persists the set of traded symbols for today."""
        try:
            cache_file = self.cache_dir / f"traded_symbols_{today_ist_str()}.json"
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            with open(cache_file, "w") as f:
                json.dump(sorted(list(self.traded_symbols)), f, indent=2)
        except Exception as e:
            logger.debug(f"Failed to save traded symbols cache: {e}")

    def load_traded_symbols_from_cache(self):
        """Restores traded symbols from disk cache for today."""
        try:
            cache_file = self.cache_dir / f"traded_symbols_{today_ist_str()}.json"
            if cache_file.exists():
                with open(cache_file, "r") as f:
                    symbols = json.load(f)
                if isinstance(symbols, list):
                    self.traded_symbols.update(symbols)
                    logger.info(f"Loaded {len(symbols)} previously traded symbols from disk: {symbols}")
        except Exception as e:
            logger.debug(f"Failed to load traded symbols cache: {e}")

    def calculate_target_price(self, entry_price: float, side: PositionSide) -> Optional[float]:
        """
        Calculates profit target based on profit_target_pct of the asset price.
        Supports both fractional (0.02 = 2%) and percentage (2.0 = 2%) notations.
        """
        if not self.config.profit_target_pct:
            return None
        pct = self.config.profit_target_pct
        if pct >= 1.0:
            pct = pct / 100.0
        target_dist = round(entry_price * pct, 2)
        if side == PositionSide.LONG:
            return round(entry_price + target_dist, 2)
        else:
            return round(entry_price - target_dist, 2)

    def on_screener_ready(self, candidates: List[ScreenerCandidate], is_live: bool = False):
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
        if is_live and self.notifier:
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
            return  # Already took today's trade for this symbol (persisted across re-runs)

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
        target = self.calculate_target_price(ltp, PositionSide.LONG)

        open_positions = self.get_positions()
        shares, reason = self.risk_manager.calculate_position_size(
            symbol, ltp, stop_loss, open_positions
        )
        if shares <= 0:
            logger.warning(f"Sizing rejected long on {symbol}: {reason}")
            return

        tgt_str = f"₹{target:.2f}" if target is not None else "None"
        logger.info(
            f"⚡ LONG BREAKOUT on {symbol} @ ₹{ltp:.2f} (Trigger High: ₹{cand.opening_high:.2f}) | "
            f"Qty: {shares}, SL: ₹{stop_loss:.2f}, Target: {tgt_str}"
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
            self.mark_symbol_traded(symbol)

    def _trigger_short_entry(
        self, symbol: str, cand: ScreenerCandidate, ltp: float, atr: float
    ):
        """Triggers short breakout entry."""
        stop_dist = self.config.atr_stop_loss_pct * atr
        stop_loss = round(ltp + stop_dist, 2)
        target = self.calculate_target_price(ltp, PositionSide.SHORT)

        open_positions = self.get_positions()
        shares, reason = self.risk_manager.calculate_position_size(
            symbol, ltp, stop_loss, open_positions
        )
        if shares <= 0:
            logger.warning(f"Sizing rejected short on {symbol}: {reason}")
            return

        tgt_str = f"₹{target:.2f}" if target is not None else "None"
        logger.info(
            f"⚡ SHORT BREAKOUT on {symbol} @ ₹{ltp:.2f} (Trigger Low: ₹{cand.opening_low:.2f}) | "
            f"Qty: {shares}, SL: ₹{stop_loss:.2f}, Target: {tgt_str}"
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
            self.mark_symbol_traded(symbol)

    def _manage_open_position(self, pos: Position, ltp: float):
        """
        Manages an active position: updates P&L, trails stop loss, and executes exits.
        """
        pos.update_pnl(ltp)
        cand = self.candidates.get(pos.symbol)
        atr = cand.atr_14d if (cand and cand.atr_14d > 0) else (pos.entry_price * 0.01)

        # Auto-initialize stop loss and profit target from config if missing or corrupted
        if pos.stop_loss is None and self.config.atr_stop_loss_pct:
            stop_dist = self.config.atr_stop_loss_pct * atr
            if pos.side == PositionSide.LONG:
                pos.stop_loss = round(pos.entry_price - stop_dist, 2)
            else:
                pos.stop_loss = round(pos.entry_price + stop_dist, 2)
            if pos.trailing_stop is None:
                pos.trailing_stop = pos.stop_loss

        # Calculate expected target based on profit_target_pct of asset price
        if self.config.profit_target_pct:
            expected_target = self.calculate_target_price(pos.entry_price, pos.side)
            expected_dist = abs(expected_target - pos.entry_price)
            # If target missing, or set too tight (< 50% of expected target from old bug)
            if pos.target is None or abs(pos.target - pos.entry_price) < (expected_dist * 0.5):
                pos.target = expected_target

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
                self.mark_symbol_traded(pos.symbol)
                return

            # 2. Profit target check
            if pos.target and ltp >= pos.target:
                logger.info(f"🎯 Profit target hit on {pos.symbol} @ ₹{ltp:.2f} (Target: ₹{pos.target:.2f})")
                self.close_position(pos.symbol, reason="TAKE_PROFIT")
                self.mark_symbol_traded(pos.symbol)
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
                self.mark_symbol_traded(pos.symbol)
                return

            # 2. Profit target check
            if pos.target and ltp <= pos.target:
                logger.info(f"🎯 Profit target hit on {pos.symbol} @ ₹{ltp:.2f} (Target: ₹{pos.target:.2f})")
                self.close_position(pos.symbol, reason="TAKE_PROFIT")
                self.mark_symbol_traded(pos.symbol)
                return
