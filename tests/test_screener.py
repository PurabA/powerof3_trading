"""
Unit tests for live screener and MarketDataManager batch quoting.
"""
from unittest.mock import MagicMock
from datetime import datetime

from core.models import Quote, ScreenerCandidate
from broker.market_data import MarketDataManager
from engine import TradingEngine
from config.settings import load_settings


def test_market_data_batch_quoting():
    mock_client = MagicMock()
    mock_client.quotes.return_value = [
        {
            "exchange_token": "2885",
            "display_symbol": "RELIANCE-EQ",
            "ltp": "1240.50",
            "last_volume": "1050000",
            "ohlc": {
                "open": "1236.00",
                "high": "1242.00",
                "low": "1235.00",
                "close": "1235.00"
            }
        }
    ]

    md = MarketDataManager(neo_client=mock_client)
    # Ensure mapping has RELIANCE
    md.symbol_to_token["RELIANCE"] = "2885"
    md.token_to_symbol["2885"] = "RELIANCE"

    quotes = md.fetch_quotes_batch(["RELIANCE"])
    assert "RELIANCE" in quotes
    q = quotes["RELIANCE"]
    assert q.last_price == 1240.50
    assert q.volume == 1050000
    assert q.high == 1242.00


def test_empty_screener_cache_ignored(tmp_path):
    settings = load_settings()
    engine = TradingEngine(settings)
    engine.historical_data.cache_dir = tmp_path

    # Simulate an empty cache on disk
    cache_file = tmp_path / "screener_test.json"
    cache_file.write_text("[]")

    assert engine.candidates == []
    assert not engine.screener_done
