"""
Market Data Engine for Kotak Neo.
Provides batch snapshot quotes, WebSocket streaming, and price caching.
"""
import logging
from typing import Dict, List, Optional
from datetime import datetime

from core.models import Quote

logger = logging.getLogger("MarketData")


class MarketDataManager:
    """
    Tracks real-time prices and manages quotes via Kotak Neo API or feeds.
    """
    def __init__(self, neo_client=None):
        self.neo_client = neo_client
        self.quotes_cache: Dict[str, Quote] = {}

    def set_quote(self, quote: Quote):
        self.quotes_cache[quote.symbol] = quote

    def get_quote(self, symbol: str) -> Optional[Quote]:
        return self.quotes_cache.get(symbol)

    def get_ltp(self, symbol: str) -> float:
        q = self.quotes_cache.get(symbol)
        return q.last_price if q else 0.0

    def fetch_quotes_batch(self, symbols: List[str]) -> Dict[str, Quote]:
        """
        Fetches snapshot quotes for a batch of symbols via Kotak Neo client.
        """
        if not self.neo_client:
            return self.quotes_cache

        try:
            tokens = []
            for s in symbols:
                tokens.append({
                    "instrument_token": s,
                    "exchange_segment": "nse_cm"
                })

            resp = self.neo_client.quotes(instrument_tokens=tokens, quote_type="LTP")
            if isinstance(resp, dict) and "data" in resp:
                for item in resp["data"]:
                    sym = item.get("trading_symbol", "").replace("-EQ", "")
                    ltp = float(item.get("last_price", 0.0))
                    if sym and ltp > 0:
                        quote = Quote(
                            symbol=sym,
                            last_price=ltp,
                            timestamp=datetime.now()
                        )
                        self.quotes_cache[sym] = quote

            return self.quotes_cache

        except Exception as e:
            logger.error(f"Error fetching batch quotes: {e}")
            return self.quotes_cache
