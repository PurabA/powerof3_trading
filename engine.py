"""
TradingEngine - Master Intraday State Machine.
Coordinates broker, risk management, safety guardrails, market data,
strategy execution, Telegram broadcasting, and terminal dashboard rendering.
Strictly respects Asia/Kolkata (IST) schedule.
"""
import time
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime

from config.settings import TradingSystemSettings, load_settings
from core.timeutils import TimeManager, now_ist, today_ist_str
from core.models import ScreenerCandidate, Quote, OrderStatus
from core.guardrails import OrderSafetyLock
from core.risk import RiskManager
from broker.neo_auth import KotakNeoAuth
from broker.neo_broker import KotakNeoBroker
from broker.market_data import MarketDataManager
from broker.historical import HistoricalDataManager
from notifier.telegram import TelegramNotifier
from strategies.rvol_orb import RvolOrbStrategy
from ui.terminal import TerminalDashboard

logger = logging.getLogger("TradingEngine")


class TradingEngine:
    """
    Main state machine orchestrating the trading lifecycle.
    """
    def __init__(self, settings: Optional[TradingSystemSettings] = None):
        self.settings = settings or load_settings()
        self.time_manager = TimeManager(self.settings.timings)
        self.guardrails = OrderSafetyLock(self.settings.guardrails, self.time_manager)
        self.risk_manager = RiskManager(self.settings.risk)
        self.auth = KotakNeoAuth(self.settings.neo)
        self.historical_data = HistoricalDataManager(
            universe_config=self.settings.universe_filters,
            strategy_config=self.settings.strategy
        )
        self.notifier = TelegramNotifier(self.settings.telegram)
        self.dashboard = TerminalDashboard()

        # Broker and market data
        self.neo_client = None
        self.broker = KotakNeoBroker(self.settings.app, self.settings.friction)
        self.market_data = MarketDataManager()

        # Strategy
        self.strategy = RvolOrbStrategy(self.settings.strategy)

        # State flags
        self.is_authenticated = False
        self.baselines_ready = False
        self.screener_done = False
        self.squared_off = False
        self.eod_report_sent = False
        self.last_hourly_update = -1
        self.symbols_universe: List[str] = []
        self.candidates: List[ScreenerCandidate] = []

    def initialize(self):
        """Initializes symbols universe, broker connections, and strategy binding."""
        logger.info(f"Initializing {self.settings.app.name} in [{self.settings.app.trading_mode}] mode...")

        # Load Nifty 500 symbol map
        map_path = Path("config/nifty500_scrip_map.json")
        if map_path.exists():
            with open(map_path, "r") as f:
                scrip_map = json.load(f)
                self.symbols_universe = list(scrip_map.keys())
        else:
            self.symbols_universe = ["RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK"]

        logger.info(f"Loaded {len(self.symbols_universe)} universe symbols.")

        # Authenticate with Kotak Neo if LIVE or credentials present
        if self.settings.app.trading_mode == "LIVE" or self.settings.neo.consumer_key:
            try:
                self.neo_client = self.auth.get_authenticated_client()
                if self.neo_client:
                    self.broker.neo_client = self.neo_client
                    self.market_data.neo_client = self.neo_client
                    self.broker.market_data = self.market_data
                    self.is_authenticated = True
                    logger.info("Kotak Neo client initialized and bound to broker.")
            except Exception as e:
                logger.error(f"Authentication failed during initialization: {e}")
                if self.settings.app.trading_mode == "LIVE":
                    raise

        # Ensure broker always has access to market data manager
        self.broker.market_data = self.market_data

        # Bind broker to strategy scope
        self.broker.bind_strategy(self.strategy.name)

        # Sync open positions, orders, and limits from Kotak Neo broker (if live)
        if self.neo_client:
            try:
                limits_resp = self.neo_client.limits()
                data = limits_resp.get("data") if isinstance(limits_resp, dict) else limits_resp
                limits_obj = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})
                avail_cash = 0.0
                for k in ("Net", "availableCash", "cash", "collateral", "marginAvailable"):
                    try:
                        v = float(limits_obj.get(k) or 0.0)
                        if v > 0:
                            avail_cash = v
                            break
                    except (ValueError, TypeError):
                        pass
                if avail_cash > 0:
                    self.risk_manager.capital = round(avail_cash, 2)
                    logger.info(f"💰 Synced live account capital from Kotak Neo limits: ₹{avail_cash:,.2f}")
            except Exception as e:
                logger.debug(f"Limits query skipped: {e}")

            self.broker.sync_positions_from_broker(self.market_data)
            self.broker.sync_orders_from_broker()

        # Bind strategy dependencies
        self.strategy.bind_context(
            broker=self.broker,
            risk_manager=self.risk_manager,
            guardrails=self.guardrails,
            time_manager=self.time_manager,
            market_data=self.market_data,
            historical_data=self.historical_data,
            notifier=self.notifier
        )
        self.strategy.on_start()
        self.strategy.load_traded_symbols_from_cache()

        # Reconcile guardrails and strategy tracking with loaded/synced positions and orders
        from core.models import PositionState
        for sym, pos in self.broker.positions.items():
            self.strategy.mark_symbol_traded(sym)
            if pos.quantity > 0:
                self.guardrails.set_state(pos.symbol, PositionState.OPEN, pos.side)
                ltp = self.market_data.get_ltp(pos.symbol)
                if ltp > 0:
                    pos.update_pnl(ltp)

        for o in self.broker.get_order_book():
            if o.status in (OrderStatus.FILLED, OrderStatus.SUBMITTED):
                self.strategy.mark_symbol_traded(o.symbol)

        # Cross-check executed trades today directly from Kotak Neo belonging to THIS strategy
        if self.neo_client:
            try:
                trd_rep = self.neo_client.trade_report()
                trd_data = trd_rep.get("data") if isinstance(trd_rep, dict) else trd_rep
                if isinstance(trd_data, list):
                    for trd in trd_data:
                        raw_sym = trd.get("trdSym") or trd.get("tradingSymbol") or ""
                        sym = raw_sym.replace("-EQ", "").strip()
                        n_ord_no = str(trd.get("nOrdNo") or trd.get("orderId") or "")
                        tag = str(trd.get("ordTag") or trd.get("tag") or "")

                        # Only mark if this trade matches this strategy's order ID or strategy tag
                        is_strat_trade = (
                            (n_ord_no and n_ord_no in self.broker.orders) or
                            (self.strategy.name.upper() in tag.upper()) or
                            (tag.upper().startswith("ORB")) or
                            (sym in self.strategy.traded_symbols)
                        )
                        if sym and is_strat_trade:
                            self.strategy.mark_symbol_traded(sym)
            except Exception as e:
                logger.debug(f"Trade report query skipped: {e}")

        logger.info(f"Trading engine initialization complete. Traded symbols today: {sorted(list(self.strategy.traded_symbols))}")

    def run_pre_market_prep(self):
        """Precomputes 14-day baselines for the universe before 09:15 IST."""
        if self.baselines_ready:
            return

        logger.info("Running pre-market universe baseline precomputations...")
        eligible_baselines = self.historical_data.precompute_universe_baselines(self.symbols_universe)
        self.baselines_ready = True
        logger.info(f"Pre-market prep done. {len(eligible_baselines)} symbols ready for 09:20 screener.")

    def run_morning_screener(self):
        """
        Executes at 09:20 IST:
        Fetches the 5-minute opening candle (09:15 - 09:20) for all baseline-eligible stocks,
        computes RVOL, applies directional bias, and selects Top-N stocks.
        Caches candidates to disk for fast dashboard and restart performance.
        """
        if self.screener_done:
            return

        from core.storage import resolve_cache_file, save_json_atomic
        cache_file = resolve_cache_file("screener", strategy_name=self.strategy.name)
        if cache_file.exists():
            try:
                with open(cache_file, "r") as f:
                    data = json.load(f)
                loaded_cands = [ScreenerCandidate(**d) for d in data]
                if loaded_cands:
                    self.candidates = loaded_cands
                    self.screener_done = True
                    logger.info(f"Loaded {len(loaded_cands)} screener candidates from cache: {cache_file}")
                    self.strategy.on_screener_ready(loaded_cands, is_live=False)
                    return
                else:
                    logger.info("Cached screener file is empty, running fresh screener...")
            except Exception as e:
                logger.warning(f"Failed to load cached screener ({e}), re-screening...")

        curr_t = self.time_manager.current_time_ist()
        is_opening_minute = (curr_t.hour == 9 and 20 <= curr_t.minute <= 21)
        logger.info(
            f"⏰ Executing Trexquant RVOL ORB Screener for strategy '{self.strategy.name}' "
            f"at {curr_t.strftime('%H:%M:%S')} IST..."
        )
        eligible_map = self.historical_data.stock_baselines
        strat_cfg = self.settings.strategy
        evaluated = []

        # If past 09:21 IST (e.g. late boot or server re-run without cache),
        # live snapshot quotes report cumulative whole-day volume, NOT the opening 5-minute volume!
        # Therefore, fetch the TRUE 09:15-09:20 opening 5-minute candle.
        opening_candles = {}
        if not is_opening_minute and eligible_map:
            logger.info("Fetching exact 09:15-09:20 5m opening candles for universe baselines...")
            opening_candles = self.historical_data.fetch_opening_5m_candles(list(eligible_map.keys()))

        # Live quotes batch for fallback or current LTP
        quotes_dict = {}
        if self.neo_client and eligible_map:
            quotes_dict = self.market_data.fetch_quotes_batch(list(eligible_map.keys()))

        for symbol, base in eligible_map.items():
            base_open_vol = base.get("avg_open_vol_14d", 0)
            if base_open_vol <= 0:
                continue

            op_open = op_high = op_low = op_close = op_vol = 0.0

            # Priority 1: Exact 09:15-09:20 5-minute bar
            if symbol in opening_candles:
                c = opening_candles[symbol]
                op_open = c["open"]
                op_high = c["high"]
                op_low = c["low"]
                op_close = c["close"]
                op_vol = c["volume"]
            # Priority 2: Live snapshot quote (strictly if right at 09:20 opening, or fallback)
            elif symbol in quotes_dict:
                quote = quotes_dict[symbol]
                if quote and quote.last_price > 0:
                    ltp = quote.last_price
                    op_open = quote.open if quote.open > 0 else ltp
                    op_high = quote.high if quote.high > 0 else ltp
                    op_low = quote.low if quote.low > 0 else ltp
                    op_close = ltp
                    op_vol = quote.volume
            # Priority 3: Offline cached historical bars
            else:
                df = self.historical_data.get_history(symbol)
                if not df.empty:
                    last_date = df["Timestamp"].dt.date.iloc[-1]
                    day_df = df[df["Timestamp"].dt.date == last_date]
                    if len(day_df) >= 5:
                        open_5m = day_df.iloc[:5]
                        op_open = open_5m.iloc[0]["Open"]
                        op_high = open_5m["High"].max()
                        op_low = open_5m["Low"].min()
                        op_close = open_5m.iloc[-1]["Close"]
                        op_vol = int(open_5m["Volume"].sum())

            if op_vol <= 0 or op_high <= 0 or op_low <= 0:
                continue

            rvol = op_vol / base_open_vol
            if rvol < strat_cfg.min_rvol_threshold:
                continue

            if op_close > op_open:
                direction = "LONG"
                trigger_price = op_high
            elif op_close < op_open:
                direction = "SHORT"
                trigger_price = op_low
            else:
                direction = "DOJI"
                if strat_cfg.skip_doji:
                    continue
                trigger_price = op_high

            cand = ScreenerCandidate(
                symbol=symbol,
                neo_symbol=f"{symbol}-EQ",
                direction=direction,
                opening_high=float(op_high),
                opening_low=float(op_low),
                opening_open=float(op_open),
                opening_close=float(op_close),
                opening_volume=int(op_vol),
                baseline_opening_volume=float(base_open_vol),
                rvol=round(float(rvol), 2),
                atr_14d=float(base.get("atr_14d", 0.0)),
                avg_vol_14d=float(base.get("avg_vol_14d", 0.0)),
                avg_turnover_14d=float(base.get("avg_turnover_14d", 0.0)),
                trigger_price=float(trigger_price)
            )
            evaluated.append(cand)

        evaluated.sort(key=lambda x: x.rvol, reverse=True)
        top_candidates = evaluated[:strat_cfg.top_n_stocks]
        for i, c in enumerate(top_candidates, 1):
            c.rank = i

        if top_candidates:
            self.candidates = top_candidates
            self.screener_done = True
            logger.info(f"Screener selected {len(top_candidates)} Top RVOL candidates for '{self.strategy.name}'.")

            # Save screener cache to disk atomically
            try:
                save_json_atomic(cache_file, [c.__dict__ for c in top_candidates])
                logger.info(f"Cached screener candidates to {cache_file}")
            except Exception as e:
                logger.warning(f"Failed to cache screener: {e}")

            # Notify strategy and Telegram (only live broadcast at 09:20 market opening)
            self.strategy.on_screener_ready(top_candidates, is_live=is_opening_minute)
        else:
            logger.warning("Screener found 0 candidates passing filters. Will re-attempt on next tick.")

    def process_market_tick(self):
        """
        Polls prices for candidates, triggers breakouts, manages stops,
        and enforces circuit breakers.
        """
        open_positions = self.broker.get_positions()

        # 1. Circuit Breaker Check
        breached, total_pnl = self.risk_manager.check_circuit_breaker(open_positions)
        if breached and not self.guardrails.is_emergency_halted:
            self.guardrails.trigger_emergency_halt("Daily loss limit breached (-3%)")
            self.strategy.close_all_positions(reason="CIRCUIT_BREAKER")
            max_limit = -1.0 * (self.risk_manager.capital * self.settings.risk.max_daily_loss_pct)
            self.notifier.notify_circuit_breaker(total_pnl, max_limit)
            return

        # 2. Check for Forced Square-Off (14:45 IST)
        if self.time_manager.is_square_off_time() and not self.squared_off:
            logger.info("⏰ 14:45 IST reached! Enforcing forced square-off on all positions...")
            self.strategy.on_square_off()
            self.squared_off = True
            return

        # 3. Process candidate price updates and live tracking
        tracked_symbols = set([c.symbol for c in self.candidates] + [p.symbol for p in open_positions])
        if not tracked_symbols:
            return

        # Continuously refetch live quotes from Kotak Neo for all tracked symbols (candidates + open positions)
        if self.neo_client:
            self.market_data.fetch_quotes_batch(list(tracked_symbols))

        # Update candidate prices and process strategy ticks
        for sym in tracked_symbols:
            quote = self.market_data.get_quote(sym)
            if not quote or quote.last_price <= 0:
                # If no live quote in cache, simulate with trigger/entry price or baseline
                cand = next((c for c in self.candidates if c.symbol == sym), None)
                if cand:
                    ltp = cand.trigger_price
                    quote = Quote(symbol=sym, last_price=ltp, timestamp=now_ist())
                    self.market_data.set_quote(quote)

            if quote and quote.last_price > 0:
                cand = next((c for c in self.candidates if c.symbol == sym), None)
                if cand:
                    cand.current_ltp = quote.last_price
                self.strategy.on_tick(sym, quote)

        # 4. Hourly Telegram P&L Update
        curr_hour = now_ist().hour
        if curr_hour != self.last_hourly_update and 10 <= curr_hour <= 15:
            self.last_hourly_update = curr_hour
            day_pnl = self.risk_manager.compute_intraday_pnl(open_positions)
            self.notifier.notify_hourly_pnl(day_pnl, len(open_positions), self.broker.realized_pnl)

    def render_dashboard(self):
        """Refreshes terminal UI."""
        for c in self.candidates:
            q = self.market_data.get_quote(c.symbol)
            if q and q.last_price > 0:
                c.current_ltp = q.last_price

        for p in self.broker.get_positions():
            q = self.market_data.get_quote(p.symbol)
            if q and q.last_price > 0:
                p.update_pnl(q.last_price)

        self.dashboard.print_snapshot(
            capital=self.risk_manager.capital,
            realized_pnl=self.broker.realized_pnl,
            open_positions=self.broker.get_positions(),
            orders=self.broker.get_order_book(),
            candidates=self.candidates,
            charges=self.broker.total_charges,
            trading_mode=self.settings.app.trading_mode,
            phase_str=self.time_manager.get_market_phase_str()
        )

    def step(self):
        """Single tick step of the trading engine state machine."""
        # 1. Pre-market prep
        if not self.baselines_ready:
            self.run_pre_market_prep()

        # 2. Morning screener (runs at 09:20 IST or if market already active)
        if not self.screener_done:
            t = self.time_manager.current_time_ist()
            # If after 09:20 IST or in paper mode with historical bars
            if t >= self.time_manager.opening_range_end or self.settings.app.trading_mode == "PAPER":
                self.run_morning_screener()

        # 3. Market execution ticks
        self.process_market_tick()

        # 4. Dashboard render
        self.render_dashboard()
