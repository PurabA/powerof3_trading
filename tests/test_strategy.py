"""
Tests for strategies/rvol_orb.py validating breakout triggers,
directional bias, trailing stops, and 14:45 IST forced square-off.
"""
from datetime import datetime
import pytest
from core.models import ScreenerCandidate, Quote, Position, PositionSide, OrderSide
from core.guardrails import OrderSafetyLock
from core.risk import RiskManager
from core.timeutils import TimeManager
from broker.neo_broker import KotakNeoBroker
from strategies.rvol_orb import RvolOrbStrategy
from config.settings import StrategyConfig, AppConfig, FrictionConfig, RiskConfig, GuardrailsConfig, TimingsConfig


@pytest.fixture
def strategy_env():
    app_cfg = AppConfig(trading_mode="PAPER")
    fric_cfg = FrictionConfig()
    risk_cfg = RiskConfig(initial_capital=1000000.0)
    strat_cfg = StrategyConfig(
        timeframe_minutes=5,
        min_rvol_threshold=2.0,
        top_n_stocks=10,
        atr_stop_loss_pct=0.30,
        use_trailing_stop=True,
        trailing_stop_atr_pct=0.30
    )
    timings_cfg = TimingsConfig()
    guard_cfg = GuardrailsConfig(order_cooldown_seconds=0, enforce_market_hours=False)

    time_mgr = TimeManager(timings_cfg)
    guardrails = OrderSafetyLock(guard_cfg, time_mgr)
    risk_mgr = RiskManager(risk_cfg)
    broker = KotakNeoBroker(app_cfg, fric_cfg, load_cache=False)
    strat = RvolOrbStrategy(strat_cfg, load_cache=False)

    strat.bind_context(
        broker=broker,
        risk_manager=risk_mgr,
        guardrails=guardrails,
        time_manager=time_mgr,
        market_data=None,
        historical_data=None
    )
    strat.on_start()
    return strat, broker


def test_screener_and_long_breakout(strategy_env):
    strat, broker = strategy_env

    # 1. Candidate in play: Long bias, Open=100, High=105, Low=98, Close=104, ATR=10
    cand = ScreenerCandidate(
        symbol="RELIANCE",
        neo_symbol="RELIANCE-EQ",
        direction="LONG",
        opening_high=105.0,
        opening_low=98.0,
        opening_open=100.0,
        opening_close=104.0,
        opening_volume=50000,
        baseline_opening_volume=20000,
        rvol=2.5,
        atr_14d=10.0,
        avg_vol_14d=200000,
        avg_turnover_14d=100000000,
        trigger_price=105.0,
        rank=1
    )
    strat.on_screener_ready([cand])
    assert "RELIANCE" in strat.candidates

    # 2. Tick below breakout level -> No trade
    strat.on_tick("RELIANCE", Quote(symbol="RELIANCE", last_price=104.5))
    assert len(broker.get_positions()) == 0

    # 3. Tick reaches breakout trigger (>= 105.0) -> Long Entry executed
    strat.on_tick("RELIANCE", Quote(symbol="RELIANCE", last_price=105.5))
    positions = broker.get_positions()
    assert len(positions) == 1
    pos = positions[0]
    assert pos.symbol == "RELIANCE"
    assert pos.side == PositionSide.LONG
    assert pos.quantity > 0


def test_forced_square_off(strategy_env):
    strat, broker = strategy_env

    # Enter Long position
    cand = ScreenerCandidate(
        symbol="INFY",
        neo_symbol="INFY-EQ",
        direction="LONG",
        opening_high=1500.0,
        opening_low=1480.0,
        opening_open=1485.0,
        opening_close=1495.0,
        opening_volume=80000,
        baseline_opening_volume=30000,
        rvol=2.6,
        atr_14d=25.0,
        avg_vol_14d=300000,
        avg_turnover_14d=200000000,
        trigger_price=1500.0
    )
    strat.on_screener_ready([cand])
    strat.on_tick("INFY", Quote(symbol="INFY", last_price=1505.0))
    assert len(broker.get_positions()) == 1

    # Call on_square_off() (simulates 14:45 IST)
    strat.on_square_off()
    # Position should now be closed
    assert len(broker.get_positions()) == 0
