"""
Risk Management Engine for position sizing, leverage control, and daily loss circuit breaker.
"""
import logging
from typing import Dict, List, Optional, Tuple
from core.models import Position, PositionSide
from config.settings import RiskConfig

logger = logging.getLogger("RiskManager")


class RiskManager:
    """
    Enforces position sizing, portfolio exposure limits, and capital preservation.
    """
    def __init__(self, config: RiskConfig):
        self.config = config
        self.capital = config.initial_capital
        self.realized_pnl = 0.0
        self.circuit_breaker_triggered = False

    def update_capital(self, new_capital: float):
        self.capital = max(1000.0, new_capital)

    def record_realized_pnl(self, pnl: float):
        self.realized_pnl += pnl

    def compute_intraday_pnl(self, open_positions: List[Position]) -> float:
        unrealized = sum(p.unrealized_pnl for p in open_positions)
        return self.realized_pnl + unrealized

    def check_circuit_breaker(self, open_positions: List[Position]) -> Tuple[bool, float]:
        """
        Checks if the daily loss threshold (e.g. -3%) has been breached.
        Returns: (is_breached, total_pnl)
        """
        total_pnl = self.compute_intraday_pnl(open_positions)
        max_loss_limit = -1.0 * (self.capital * self.config.max_daily_loss_pct)

        if total_pnl <= max_loss_limit:
            self.circuit_breaker_triggered = True
            logger.critical(
                f"🚨 DAILY LOSS CIRCUIT BREAKER BREACHED! "
                f"Current P&L: ₹{total_pnl:,.2f} <= Limit: ₹{max_loss_limit:,.2f}"
            )
            return True, total_pnl

        return False, total_pnl

    def calculate_position_size(
        self,
        symbol: str,
        entry_price: float,
        stop_loss_price: float,
        open_positions: List[Position]
    ) -> Tuple[int, str]:
        """
        Calculates safe share quantity adhering to:
        1. 1% Risk per trade: (Capital * 1%) / Stop_Distance
        2. Max 20% single position capital allocation: (Capital * 20%) / Entry_Price
        3. Portfolio Gross Leverage limit: Total exposure <= 4x Capital
        4. Max open positions limit
        """
        if self.circuit_breaker_triggered:
            return 0, "Circuit breaker is active. Trading halted."

        if len(open_positions) >= self.config.max_open_positions:
            return 0, f"Max open positions reached ({len(open_positions)}/{self.config.max_open_positions})"

        if entry_price <= 0:
            return 0, "Invalid entry price <= 0"

        stop_distance = abs(entry_price - stop_loss_price)
        if stop_distance <= 0:
            return 0, f"Invalid stop loss price: {stop_loss_price}. Stop distance cannot be 0"

        # 1. Size by risk per trade (e.g. 1% of capital)
        risk_budget = self.capital * self.config.risk_per_trade_pct
        shares_by_risk = int(risk_budget / stop_distance)

        # 2. Size by single position allocation cap (e.g. max 20% of capital)
        max_pos_value = self.capital * self.config.max_capital_per_trade_pct
        shares_by_allocation = int(max_pos_value / entry_price)

        shares = min(shares_by_risk, shares_by_allocation)
        if shares <= 0:
            return 0, f"Sized quantity is 0 (price={entry_price}, stop_dist={stop_distance:.2f})"

        # 3. Check Portfolio Gross Leverage
        current_gross_exposure = sum(p.quantity * p.current_price for p in open_positions)
        new_trade_exposure = shares * entry_price
        max_allowed_exposure = self.capital * self.config.max_portfolio_leverage

        if (current_gross_exposure + new_trade_exposure) > max_allowed_exposure:
            # Scale down shares to fit remaining leverage capacity
            remaining_capacity = max(0.0, max_allowed_exposure - current_gross_exposure)
            shares = int(remaining_capacity / entry_price)
            if shares <= 0:
                return 0, (
                    f"Portfolio leverage limit reached: exposure ₹{current_gross_exposure:,.2f} "
                    f"exceeds max ₹{max_allowed_exposure:,.2f}"
                )

        return shares, "OK"
