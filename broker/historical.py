"""
30-Day Historical Data Manager.
Maintains rolling daily and 5-minute bars for Nifty 500 symbols,
computing 14-day True Range, ATR(14), Average Turnover, and
14-day 5-minute Opening Volume baseline for RVOL calculation.
"""
import os
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional
import pandas as pd
import numpy as np

from config.settings import UniverseFiltersConfig, StrategyConfig

logger = logging.getLogger("HistoricalData")


class HistoricalDataManager:
    """
    Manages 30-day cached price bars and pre-market universe baselines.
    """
    def __init__(
        self,
        cache_dir: Optional[Path] = None,
        universe_config: Optional[UniverseFiltersConfig] = None,
        strategy_config: Optional[StrategyConfig] = None
    ):
        self.cache_dir = cache_dir or Path("data/cache")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.universe_config = universe_config or UniverseFiltersConfig()
        self.strategy_config = strategy_config or StrategyConfig()
        self.stock_baselines: Dict[str, Dict] = {}

    def get_history(self, symbol: str, days: int = 30) -> pd.DataFrame:
        """Loads cached historical bars for a symbol."""
        csv_file = self.cache_dir / f"{symbol}_1min.csv"
        
        # Fallback to Trexquant historical data if available
        if not csv_file.exists():
            trex_file = Path(f"/Users/purabagarwal/Desktop/Quant/Trexquant/historical_data/{symbol}_1min.csv")
            if trex_file.exists():
                csv_file = trex_file

        if not csv_file.exists():
            return pd.DataFrame()

        try:
            df = pd.read_csv(csv_file)
            df["Timestamp"] = pd.to_datetime(df["Timestamp"])
            df = df.sort_values("Timestamp").reset_index(drop=True)
            return df
        except Exception as e:
            logger.warning(f"Failed to read historical data for {symbol}: {e}")
            return pd.DataFrame()

    def compute_baseline_stats(self, symbol: str) -> Optional[Dict]:
        """
        Computes 14-day ATR, average daily volume, turnover, and
        the average 5-minute opening volume baseline (09:15 - 09:20).
        """
        df = self.get_history(symbol)
        if df.empty:
            return None

        lookback = self.strategy_config.rvol_lookback_days
        df["Date"] = df["Timestamp"].dt.date
        df["Time"] = df["Timestamp"].dt.time

        daily_rows = []
        for date, group in df.groupby("Date"):
            day_open = group.iloc[0]["Open"]
            day_high = group["High"].max()
            day_low = group["Low"].min()
            day_close = group.iloc[-1]["Close"]
            day_vol = group["Volume"].sum()

            # 5-minute opening candle (first 5 bars 09:15 to 09:19 inclusive)
            open_5m = group.iloc[:5]
            open_vol = open_5m["Volume"].sum()

            daily_rows.append({
                "Date": date,
                "DayOpen": day_open,
                "DayHigh": day_high,
                "DayLow": day_low,
                "DayClose": day_close,
                "DayVolume": day_vol,
                "OpeningVolume": open_vol
            })

        if len(daily_rows) < lookback + 1:
            return None

        ddf = pd.DataFrame(daily_rows).sort_values("Date").reset_index(drop=True)

        # 1. True Range
        prev_close = ddf["DayClose"].shift(1)
        h_l = ddf["DayHigh"] - ddf["DayLow"]
        h_pc = (ddf["DayHigh"] - prev_close).abs()
        l_pc = (ddf["DayLow"] - prev_close).abs()
        tr = pd.concat([h_l, h_pc, l_pc], axis=1).max(axis=1)

        # 2. Rolling 14-day metrics shifted by 1 (anti-lookahead)
        atr_14d = tr.shift(1).rolling(lookback).mean().iloc[-1]
        avg_vol_14d = ddf["DayVolume"].shift(1).rolling(lookback).mean().iloc[-1]
        turnover = ddf["DayClose"] * ddf["DayVolume"]
        avg_turnover_14d = turnover.shift(1).rolling(lookback).mean().iloc[-1]
        avg_open_vol_14d = ddf["OpeningVolume"].shift(1).rolling(lookback).mean().iloc[-1]

        last_close = ddf["DayClose"].iloc[-1]

        stats = {
            "symbol": symbol,
            "last_close": float(last_close),
            "atr_14d": float(atr_14d) if not np.isnan(atr_14d) else 0.0,
            "avg_vol_14d": float(avg_vol_14d) if not np.isnan(avg_vol_14d) else 0.0,
            "avg_turnover_14d": float(avg_turnover_14d) if not np.isnan(avg_turnover_14d) else 0.0,
            "avg_open_vol_14d": float(avg_open_vol_14d) if not np.isnan(avg_open_vol_14d) else 0.0,
            "history_days": len(ddf)
        }
        self.stock_baselines[symbol] = stats
        return stats

    def precompute_universe_baselines(self, symbols: List[str]) -> Dict[str, Dict]:
        """
        Pre-computes and caches baseline statistics for all eligible symbols.
        Uses cached disk file for today's date to avoid redundant recalculation.
        """
        from core.storage import resolve_cache_file, save_json_atomic
        cache_file = resolve_cache_file("baselines", base_dir=self.cache_dir)

        if cache_file.exists():
            try:
                with open(cache_file, "r") as f:
                    eligible = json.load(f)
                self.stock_baselines = eligible
                logger.info(f"Loaded {len(eligible)} precomputed baselines from cache: {cache_file.name}")
                return eligible
            except Exception as e:
                logger.warning(f"Failed to load cached baselines ({e}), recomputing...")

        logger.info(f"Computing 14-day baselines for {len(symbols)} symbols...")
        eligible = {}
        filters = self.universe_config

        for sym in symbols:
            stats = self.compute_baseline_stats(sym)
            if not stats:
                continue

            # Universe Liquidity Filters
            if stats["last_close"] < filters.min_price or stats["last_close"] > filters.max_price:
                continue
            if stats["avg_vol_14d"] < filters.min_avg_volume_14d:
                continue
            if stats["avg_turnover_14d"] < filters.min_avg_turnover_14d:
                continue
            if stats["atr_14d"] < filters.min_atr_14d:
                continue
            if (stats["atr_14d"] / stats["last_close"]) < filters.min_atr_pct:
                continue
            if stats["history_days"] < filters.min_history_days:
                continue

            eligible[sym] = stats

        # If no CSVs available (e.g. freshly cloned VM), load seed baselines from config
        if not eligible:
            default_seed = Path("config/default_baselines.json")
            if default_seed.exists():
                try:
                    with open(default_seed, "r") as f:
                        eligible = json.load(f)
                    logger.info(f"Loaded {len(eligible)} baselines from baseline seed: {default_seed}")
                except Exception as e:
                    logger.warning(f"Failed to load baseline seed: {e}")

        # Save to disk cache
        try:
            save_json_atomic(cache_file, eligible)
            logger.info(f"Cached {len(eligible)} baselines to {cache_file}")
        except Exception as e:
            logger.warning(f"Failed to write baseline cache: {e}")

        self.stock_baselines = eligible
        logger.info(f"Pre-market universe filtering complete: {len(eligible)} eligible stocks.")
        return eligible

    def fetch_opening_5m_candles(self, symbols: List[str]) -> Dict[str, Dict[str, float]]:
        """
        Fetches the exact 09:15 - 09:20 opening 5-minute bar for today.
        Guarantees that re-running the engine at 10:00, 11:30, or later uses the true
        09:15-09:20 candle rather than cumulative day volume or day high/low.
        """
        import yfinance as yf
        from datetime import datetime

        results = {}
        if not symbols:
            return results

        chunk_size = 100
        for i in range(0, len(symbols), chunk_size):
            chunk = symbols[i:i + chunk_size]
            tickers = [f"{s}.NS" for s in chunk]
            try:
                df = yf.download(tickers, period="1d", interval="5m", progress=False)
                if df.empty:
                    continue

                mask = (df.index.time == datetime.strptime("09:15", "%H:%M").time())
                bars_0915 = df[mask]
                if bars_0915.empty:
                    bars_0915 = df.iloc[:1]

                if not bars_0915.empty:
                    bar = bars_0915.iloc[0]
                    for s in chunk:
                        t = f"{s}.NS"
                        try:
                            if isinstance(df.columns, pd.MultiIndex):
                                op_o = float(bar["Open"][t])
                                op_h = float(bar["High"][t])
                                op_l = float(bar["Low"][t])
                                op_c = float(bar["Close"][t])
                                op_v = float(bar["Volume"][t])
                            else:
                                op_o = float(bar["Open"])
                                op_h = float(bar["High"])
                                op_l = float(bar["Low"])
                                op_c = float(bar["Close"])
                                op_v = float(bar["Volume"])

                            if not np.isnan(op_o) and op_o > 0 and not np.isnan(op_v) and op_v > 0:
                                results[s] = {
                                    "open": op_o,
                                    "high": op_h,
                                    "low": op_l,
                                    "close": op_c,
                                    "volume": int(op_v)
                                }
                        except (KeyError, TypeError, ValueError):
                            continue
            except Exception as e:
                logger.warning(f"Error fetching 5m opening candles chunk {i}: {e}")

        logger.info(f"Retrieved exact 09:15-09:20 opening candles for {len(results)}/{len(symbols)} symbols.")
        return results

