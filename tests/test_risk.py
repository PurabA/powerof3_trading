"""
Tests for core/risk.py testing 1% risk budgeting, single stock allocation caps,
portfolio gross leverage limits, and daily loss circuit breaker.
"""
import pytest
from core.risk import RiskManager
from core.models import Position, PositionSide
from config.settings import RiskConfig


@pytest.fixture
def risk_manager():
    config = RiskConfig(
        initial_capital=1000000.0,      # ₹10 Lakhs
        risk_per_trade_pct=0.01,        # 1% = ₹10,000 max risk
        max_capital_per_trade_pct=0.20, # 20% = ₹2,00,000 max allocation
        max_portfolio_leverage=4.0,     # ₹40,00,000 max exposure
        max_open_positions=10,
        max_daily_loss_pct=0.03         # 3% = ₹30,000 circuit breaker
    )
    return RiskManager(config)


def test_position_sizing_by_risk(risk_manager):
    # Entry = 1000, Stop Loss = 980 -> Stop Distance = 20
    # Risk budget = 1,000,000 * 0.01 = 10,000
    # Shares by risk = 10,000 / 20 = 500 shares
    # Allocation cap = 200,000 / 1000 = 200 shares
    # Min(500, 200) = 200 shares (allocation capped)
    shares, reason = risk_manager.calculate_position_size("ABC", 1000.0, 980.0, [])
    assert shares == 200
    assert reason == "OK"


def test_position_sizing_when_risk_limits(risk_manager):
    # Entry = 1000, Stop Loss = 900 -> Stop Distance = 100
    # Shares by risk = 10,000 / 100 = 100 shares
    # Allocation cap = 200,000 / 1000 = 200 shares
    # Min(100, 200) = 100 shares (risk limited)
    shares, reason = risk_manager.calculate_position_size("XYZ", 1000.0, 900.0, [])
    assert shares == 100
    assert reason == "OK"


def test_max_open_positions_cap(risk_manager):
    # Create 10 dummy open positions
    open_positions = [
        Position(symbol=f"SYM_{i}", side=PositionSide.LONG, quantity=10, entry_price=100.0, current_price=100.0)
        for i in range(10)
    ]
    shares, reason = risk_manager.calculate_position_size("NEW_SYM", 1000.0, 950.0, open_positions)
    assert shares == 0
    assert "Max open positions reached" in reason


def test_daily_loss_circuit_breaker(risk_manager):
    # Initial P&L is 0
    breached, pnl = risk_manager.check_circuit_breaker([])
    assert not breached

    # Simulate losing trades totaling -₹35,000 (breaching 3% = -₹30,000)
    risk_manager.record_realized_pnl(-35000.0)
    breached, pnl = risk_manager.check_circuit_breaker([])
    assert breached
    assert risk_manager.circuit_breaker_triggered

    # Position sizing after circuit breaker must reject
    shares, reason = risk_manager.calculate_position_size("FAIL_SYM", 500.0, 480.0, [])
    assert shares == 0
    assert "Circuit breaker is active" in reason
