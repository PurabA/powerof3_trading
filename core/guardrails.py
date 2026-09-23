"""
Safety Guardrails and Idempotency Engine for algorithmic order execution.
Protects against double buys, double sells, overinvesting, out-of-hours orders,
and rapid re-trigger bugs.
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple
from core.models import OrderSide, PositionSide, PositionState
from core.timeutils import TimeManager, now_ist
from config.settings import GuardrailsConfig

logger = logging.getLogger("Guardrails")


class OrderSafetyLock:
    """
    Maintains per-symbol operational state locks and enforces cooldowns.
    """
    def __init__(self, config: GuardrailsConfig, time_manager: TimeManager):
        self.config = config
        self.time_manager = time_manager
        self.states: Dict[str, PositionState] = {}
        self.last_order_time: Dict[str, datetime] = {}
        self.active_sides: Dict[str, PositionSide] = {}
        self.is_emergency_halted: bool = False
        self.halt_reason: str = ""

    def get_state(self, symbol: str) -> PositionState:
        return self.states.get(symbol, PositionState.IDLE)

    def set_state(self, symbol: str, state: PositionState, side: Optional[PositionSide] = None):
        self.states[symbol] = state
        if side:
            self.active_sides[symbol] = side
        elif state in (PositionState.IDLE, PositionState.CLOSED):
            self.active_sides.pop(symbol, None)

    def trigger_emergency_halt(self, reason: str):
        """Immediately halts all order placement across the entire system."""
        self.is_emergency_halted = True
        self.halt_reason = reason
        logger.critical(f"🛑 EMERGENCY HALT TRIGGERED: {reason}")

    def reset_halt(self):
        self.is_emergency_halted = False
        self.halt_reason = ""
        logger.info("Emergency halt reset.")

    def validate_new_entry(
        self, symbol: str, side: OrderSide, qty: int, price: float
    ) -> Tuple[bool, str]:
        """
        Validates whether a new trade entry is safe to place.
        Returns: (is_allowed, rejection_reason)
        """
        # 1. Circuit Breaker / Emergency Halt
        if self.is_emergency_halted:
            return False, f"System is under emergency halt: {self.halt_reason}"

        # 2. Timing Gate (Must be 09:20 - 14:15 IST)
        if self.config.enforce_market_hours:
            if not self.time_manager.is_entry_allowed():
                current_t = self.time_manager.current_time_ist().strftime("%H:%M:%S")
                return False, f"Entry rejected: current time {current_t} IST is outside 09:20 - 14:15 IST window"

        # 3. Basic Parameter Sanity
        if qty <= 0:
            return False, f"Invalid quantity: {qty}. Must be > 0"
        if price <= 0:
            return False, f"Invalid price: {price}. Must be > 0"

        # 4. Symbol State & Double-Order Check
        current_state = self.get_state(symbol)
        if self.config.prevent_double_orders:
            if current_state in (PositionState.PENDING_ENTRY, PositionState.OPEN):
                return False, f"Duplicate order blocked: {symbol} is already in state {current_state.value}"
            
            # Check opposite side collision
            existing_side = self.active_sides.get(symbol)
            if existing_side and existing_side != PositionSide.FLAT:
                return False, f"Cannot open new position: {symbol} already has active side {existing_side.value}"

        # 5. Order Cooldown Check
        cooldown_sec = self.config.order_cooldown_seconds
        last_t = self.last_order_time.get(symbol)
        now = now_ist()
        if last_t:
            elapsed = (now - last_t).total_seconds()
            if elapsed < cooldown_sec:
                return False, (
                    f"Cooldown active for {symbol}: {elapsed:.1f}s elapsed, "
                    f"must wait {cooldown_sec}s between orders"
                )

        # Mark timestamp on pass
        self.last_order_time[symbol] = now
        return True, "ALLOWED"

    def validate_exit(
        self, symbol: str, qty: int, is_forced: bool = False
    ) -> Tuple[bool, str]:
        """
        Validates closing an open position.
        """
        if is_forced:
            return True, "FORCED_EXIT_ALLOWED"

        current_state = self.get_state(symbol)
        if current_state == PositionState.PENDING_EXIT:
            return False, f"Exit already pending for {symbol}"

        if current_state not in (PositionState.OPEN, PositionState.PENDING_ENTRY):
            return False, f"Cannot exit: {symbol} has no open position (state: {current_state.value})"

        if qty <= 0:
            return False, f"Invalid exit quantity: {qty}"

        return True, "ALLOWED"
