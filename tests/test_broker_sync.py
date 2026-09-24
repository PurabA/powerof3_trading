"""
Tests for Kotak Neo broker position synchronization, disk persistence,
and double-order/double-sell prevention.
"""
import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock
from datetime import datetime, timezone

from config.settings import AppConfig, FrictionConfig, StrategyConfig
from core.models import Position, PositionSide, OrderSide, OrderType, OrderStatus, PositionState, ScreenerCandidate
from core.timeutils import format_ist_str, IST, now_ist
from broker.neo_broker import KotakNeoBroker
from strategies.rvol_orb import RvolOrbStrategy


def test_format_ist_str_naive_and_aware():
    # Naive UTC datetime: e.g. 04:30:00 UTC -> should format as 10:00:00 IST
    naive_utc = datetime(2026, 9, 24, 4, 30, 0)
    formatted = format_ist_str(naive_utc)
    assert "10:00:00 IST" in formatted

    # Aware IST datetime
    aware_ist = datetime(2026, 9, 24, 10, 0, 0, tzinfo=IST)
    formatted2 = format_ist_str(aware_ist)
    assert "10:00:00 IST" in formatted2


def test_position_disk_cache_persistence(tmp_path):
    app_cfg = AppConfig(trading_mode="PAPER")
    fric_cfg = FrictionConfig()
    broker = KotakNeoBroker(app_cfg, fric_cfg, cache_dir=tmp_path, load_cache=False)

    # Place a simulated order
    broker.place_order(
        symbol="TCS",
        side=OrderSide.BUY,
        qty=50,
        order_type=OrderType.MARKET,
        price=3500.0,
        stop_loss=3450.0,
        target=3600.0,
        tag="TEST_CACHE"
    )

    positions = broker.get_positions()
    assert len(positions) == 1
    pos = positions[0]
    assert pos.symbol == "TCS"
    assert pos.quantity == 50
    assert pos.entry_price > 0
    assert pos.stop_loss == 3450.0
    assert pos.target == 3600.0

    # Initialize a new broker instance reading from the same cache directory
    broker2 = KotakNeoBroker(app_cfg, fric_cfg, cache_dir=tmp_path, load_cache=True)
    loaded_positions = broker2.get_positions()
    assert len(loaded_positions) == 1
    p2 = loaded_positions[0]
    assert p2.symbol == "TCS"
    assert p2.quantity == 50
    assert p2.entry_price == pos.entry_price
    assert p2.stop_loss == 3450.0
    assert p2.target == 3600.0


def test_sync_positions_from_kotak_neo(tmp_path):
    app_cfg = AppConfig(trading_mode="LIVE")
    fric_cfg = FrictionConfig()

    mock_client = MagicMock()
    mock_client.positions.return_value = {
        "data": [
            {
                "trdSym": "BAJFINANCE-EQ",
                "flNetQty": "30",
                "buyAvgPrc": "1045.50",
                "sellAvgPrc": "0.0",
                "rpnl": "0.0"
            }
        ]
    }

    broker = KotakNeoBroker(app_cfg, fric_cfg, neo_client=mock_client, cache_dir=tmp_path, load_cache=False)
    broker.sync_positions_from_broker()

    positions = broker.get_positions()
    assert len(positions) == 1
    baj = positions[0]
    assert baj.symbol == "BAJFINANCE"
    assert baj.quantity == 30
    assert baj.side == PositionSide.LONG
    assert baj.entry_price == 1045.50
    assert baj.current_price == 1045.50


def test_target_profit_and_trailing_stop_auto_exit():
    strat_cfg = StrategyConfig(
        timeframe_minutes=5,
        min_rvol_threshold=2.0,
        top_n_stocks=10,
        atr_stop_loss_pct=0.30,
        use_trailing_stop=True,
        trailing_stop_atr_pct=0.30,
        profit_target_pct=1.0
    )
    strat = RvolOrbStrategy(strat_cfg)
    mock_broker = MagicMock()
    mock_guard = MagicMock()
    mock_guard.validate_exit.return_value = (True, "ALLOWED")

    strat.broker = mock_broker
    strat.guardrails = mock_guard

    # Setup an active position with entry=100, target=110, stop=95
    pos = Position(
        symbol="SBIN",
        side=PositionSide.LONG,
        quantity=100,
        entry_price=100.0,
        current_price=100.0,
        stop_loss=95.0,
        trailing_stop=95.0,
        target=110.0
    )
    mock_broker.get_positions.return_value = [pos]

    # Tick at 105: position manages PnL, trails stop
    from core.models import Quote
    strat.on_tick("SBIN", Quote(symbol="SBIN", last_price=105.0))
    assert pos.highest_price == 105.0
    mock_broker.place_order.assert_not_called()

    # Tick at 110: hits profit target -> triggers take profit exit
    strat.on_tick("SBIN", Quote(symbol="SBIN", last_price=110.5))
    mock_broker.place_order.assert_called_once()
    call_kwargs = mock_broker.place_order.call_args[1]
    assert call_kwargs["symbol"] == "SBIN"
    assert call_kwargs["side"] == OrderSide.SELL
    assert call_kwargs["tag"] == "CLOSE_TAKE_PROFIT"


def test_profit_target_percentage_calculation():
    # 0.02 notation = 2%
    cfg = StrategyConfig(profit_target_pct=0.02)
    strat = RvolOrbStrategy(cfg)
    assert strat.calculate_target_price(1000.0, PositionSide.LONG) == 1020.0
    assert strat.calculate_target_price(1000.0, PositionSide.SHORT) == 980.0

    # 2.0 notation = 2%
    cfg2 = StrategyConfig(profit_target_pct=2.0)
    strat2 = RvolOrbStrategy(cfg2)
    assert strat2.calculate_target_price(1000.0, PositionSide.LONG) == 1020.0
    assert strat2.calculate_target_price(1000.0, PositionSide.SHORT) == 980.0

    # Specific stock test from user logs
    # CARBORUNIV @ 1355.60 with 2% target
    assert strat.calculate_target_price(1355.60, PositionSide.LONG) == 1382.71
    # FIRSTCRY @ 177.02 with 2% target
    assert strat.calculate_target_price(177.02, PositionSide.SHORT) == 173.48


def test_traded_symbols_persistence_and_no_re_entry(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = StrategyConfig(profit_target_pct=0.02)
    strat1 = RvolOrbStrategy(cfg)

    # Mark CARBORUNIV as traded
    strat1.mark_symbol_traded("CARBORUNIV")
    assert "CARBORUNIV" in strat1.traded_symbols

    # New strategy instance on simulated server restart
    strat2 = RvolOrbStrategy(cfg)
    assert "CARBORUNIV" in strat2.traded_symbols

    # Setup candidate
    cand = ScreenerCandidate(
        symbol="CARBORUNIV",
        neo_symbol="CARBORUNIV-EQ",
        direction="LONG",
        opening_high=1315.0,
        opening_low=1257.0,
        opening_open=1260.0,
        opening_close=1315.0,
        opening_volume=50000,
        baseline_opening_volume=20000,
        rvol=47.3,
        atr_14d=30.76,
        avg_vol_14d=200000,
        avg_turnover_14d=100000000,
        trigger_price=1315.0
    )
    strat2.on_screener_ready([cand], is_live=False)

    mock_broker = MagicMock()
    mock_risk = MagicMock()
    strat2.broker = mock_broker
    strat2.risk_manager = mock_risk

    # Price ticks above trigger (1360.0 >= 1315.0)
    from core.models import Quote
    strat2.on_tick("CARBORUNIV", Quote(symbol="CARBORUNIV", last_price=1360.0))

    # Should NOT trigger order because it was already traded today!
    mock_broker.place_order.assert_not_called()
    mock_risk.calculate_position_size.assert_not_called()
