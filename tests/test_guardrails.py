"""
Tests for core/guardrails.py protecting against double orders, cooldown violations,
and out-of-market-hours trading.
"""
from datetime import datetime, timedelta
import pytest
from core.guardrails import OrderSafetyLock
from core.models import OrderSide, PositionSide, PositionState
from core.timeutils import TimeManager, IST
from config.settings import GuardrailsConfig, TimingsConfig


@pytest.fixture
def guardrails():
    timings = TimingsConfig(
        timezone="Asia/Kolkata",
        market_open="09:15",
        opening_range_end="09:20",
        entry_start_time="09:20",
        entry_cutoff_time="14:15",
        forced_square_off_time="14:45",
        market_close="15:30"
    )
    time_mgr = TimeManager(timings)
    config = GuardrailsConfig(
        order_cooldown_seconds=60,
        max_order_retries=3,
        max_slippage_pct=0.001,
        prevent_double_orders=True,
        enforce_market_hours=False  # Disabled for unit testing logic independently
    )
    return OrderSafetyLock(config, time_mgr)


def test_valid_entry(guardrails):
    allowed, reason = guardrails.validate_new_entry("RELIANCE", OrderSide.BUY, 10, 2500.0)
    assert allowed
    assert reason == "ALLOWED"


def test_invalid_qty_and_price(guardrails):
    allowed, _ = guardrails.validate_new_entry("RELIANCE", OrderSide.BUY, 0, 2500.0)
    assert not allowed

    allowed, _ = guardrails.validate_new_entry("RELIANCE", OrderSide.BUY, 10, -5.0)
    assert not allowed


def test_cooldown_rejection(guardrails):
    # First order succeeds
    allowed, _ = guardrails.validate_new_entry("RELIANCE", OrderSide.BUY, 10, 2500.0)
    assert allowed

    # Immediate second order for same symbol rejected by 60s cooldown
    allowed, reason = guardrails.validate_new_entry("RELIANCE", OrderSide.BUY, 10, 2500.0)
    assert not allowed
    assert "Cooldown active" in reason


def test_double_order_prevention(guardrails):
    # Place initial order and set position to OPEN
    guardrails.validate_new_entry("TCS", OrderSide.BUY, 5, 3500.0)
    guardrails.set_state("TCS", PositionState.OPEN, PositionSide.LONG)

    # Bypass cooldown timer to test state guard
    guardrails.last_order_time["TCS"] = datetime.now() - timedelta(seconds=120)

    # Attempt second buy on open position
    allowed, reason = guardrails.validate_new_entry("TCS", OrderSide.BUY, 5, 3500.0)
    assert not allowed
    assert "Duplicate order blocked" in reason


def test_emergency_halt(guardrails):
    guardrails.trigger_emergency_halt("Test Drawdown Trigger")
    allowed, reason = guardrails.validate_new_entry("INFY", OrderSide.BUY, 15, 1400.0)
    assert not allowed
    assert "emergency halt" in reason
