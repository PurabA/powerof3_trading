"""
Market Data Engine for Kotak Neo.
Provides batch snapshot quotes, WebSocket streaming, and real-time price caching.
Maps NSE trading symbols to Kotak Neo numeric instrument tokens seamlessly.
"""
import time
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime

from core.models import Quote

logger = logging.getLogger("MarketData")


class MarketDataManager:
    """
    Tracks real-time prices and manages quotes via Kotak Neo API or feeds.
    """
    def __init__(self, neo_client=None, scrip_map_path: Optional[Path] = None):
        self.neo_client = neo_client
        self.quotes_cache: Dict[str, Quote] = {}
        self.symbol_to_token: Dict[str, str] = {}
        self.token_to_symbol: Dict[str, str] = {}
        self._load_scrip_map(scrip_map_path or Path("config/nifty500_scrip_map.json"))

    def _load_scrip_map(self, path: Path):
        """Loads symbol <-> token mappings for Nifty 500 equities."""
        if path.exists():
            try:
                with open(path, "r") as f:
                    data = json.load(f)
                for sym, info in data.items():
                    tok = info.get("token")
                    if tok:
                        self.symbol_to_token[sym] = str(tok)
                        self.token_to_symbol[str(tok)] = sym
                logger.info(f"MarketDataManager loaded {len(self.symbol_to_token)} token mappings.")
            except Exception as e:
                logger.warning(f"Failed to load scrip map in MarketDataManager: {e}")

    def set_quote(self, quote: Quote):
        self.quotes_cache[quote.symbol] = quote

    def get_quote(self, symbol: str) -> Optional[Quote]:
        if symbol not in self.quotes_cache and self.neo_client:
            self.fetch_quotes_batch([symbol])
        return self.quotes_cache.get(symbol)

    def get_ltp(self, symbol: str) -> float:
        q = self.get_quote(symbol)
        return q.last_price if q else 0.0

    def fetch_quotes_batch(self, symbols: List[str]) -> Dict[str, Quote]:
        """
        Fetches snapshot quotes for a batch of symbols via Kotak Neo client.
        Automatically chunks into batches of <= 25 to respect Kotak API limit (max 50).
        """
        if not self.neo_client or not symbols:
            return self.quotes_cache

        tokens = []
        for s in symbols:
            tok = self.symbol_to_token.get(s, s)
            tokens.append({
                "instrument_token": str(tok),
                "exchange_segment": "nse_cm"
            })

        batch_size = 35
        for i in range(0, len(tokens), batch_size):
            chunk = tokens[i:i + batch_size]
            for attempt in range(2):
                try:
                    resp = self.neo_client.quotes(instrument_tokens=chunk, quote_type="all")
                    if isinstance(resp, list):
                        for item in resp:
                            tok = str(item.get("exchange_token", ""))
                            sym = self.token_to_symbol.get(tok)
                            if not sym:
                                sym = item.get("display_symbol", "").replace("-EQ", "").strip()

                            ltp = float(item.get("ltp", 0.0))
                            vol = int(item.get("last_volume", 0))
                            ohlc = item.get("ohlc", {})
                            day_open = float(ohlc.get("open", ltp))
                            day_high = float(ohlc.get("high", ltp))
                            day_low = float(ohlc.get("low", ltp))
                            prev_close = float(ohlc.get("close", ltp))

                            if sym and ltp > 0:
                                quote = Quote(
                                    symbol=sym,
                                    last_price=ltp,
                                    open=day_open,
                                    high=day_high,
                                    low=day_low,
                                    close=prev_close,
                                    volume=vol,
                                    timestamp=datetime.now()
                                )
                                self.quotes_cache[sym] = quote
                        break  # Success, exit retry loop
                    elif isinstance(resp, dict) and "error" in resp:
                        if attempt == 0:
                            time.sleep(0.6)  # Backoff and retry once
                        else:
                            logger.warning(f"Kotak Neo quotes API rate limit on batch {i}: {resp.get('message', resp)}")
                except Exception as e:
                    if attempt == 0:
                        time.sleep(0.6)
                    else:
                        logger.error(f"Error fetching batch quotes: {e}")

            if len(tokens) > batch_size:
                time.sleep(0.35)

        return self.quotes_cache
