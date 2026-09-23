# 🚀 PowerOf3 Trading

A production-ready, safety-first algorithmic trading framework built for Indian equities (**NSE Nifty 500**). Integrates the **Kotak Neo Trading API (`kotakneoapi==3.0.7`)**, automated or prompt-assisted **2FA morning authentication**, enterprise **order safety guardrails**, strict **Asia/Kolkata (IST) schedule enforcement** with forced square-off at **14:45 IST (2:45 PM)**, multi-chat **Telegram broadcasting**, a modern **Rich terminal dashboard**, and the **Trexquant 5-Minute Relative Volume (RVOL) Opening Range Breakout (ORB)** strategy.

---

## 🏛️ Architecture & Clean Modularity

The codebase uses a clean, pragmatic hexagonal modular architecture:
- **`core/strategy.py`**: Clean, intuitive high-level strategy API with standard functions (`buy()`, `sell()`, `get_quote()`, `get_positions()`, `close_position()`).
- **`core/guardrails.py`**: Idempotency engine, position state machine, double-order blocker, 60-second order cooldown, and time gatekeeper.
- **`core/risk.py`**: 1% risk budgeting per trade, single position capital cap (max 20%), portfolio gross leverage cap (4x), max open positions cap (10), and daily loss circuit breaker (-3% to -10%).
- **`core/timeutils.py`**: Strict `Asia/Kolkata` timezone enforcement across all clocks, schedules, and timers.
- **`broker/neo_auth.py`**: Kotak Neo API v3 authentication. Supports automated zero-touch TOTP generation via `pyotp` OR interactive terminal prompt fallback, with daily session persistence (`data/sessions/neo_session.json`).
- **`broker/neo_broker.py`**: Dual-mode execution engine supporting both realistic **`PAPER`** mode (simulated fills, slippage, and exact Indian regulatory charges: STT, GST, SEBI, stamp duty) and **`LIVE`** execution via Kotak Neo.
- **`broker/historical.py`**: 30-day Nifty 500 historical data caching and 14-day baseline computation (ATR, average volume, opening volume baseline) with bundled seed fallback.
- **`notifier/telegram.py`**: Multi-chat notification broadcaster supporting up to 3 Telegram chat IDs.
- **`strategies/rvol_orb.py`**: Trexquant 5-minute RVOL ORB strategy implementation with trailing stops.
- **`ui/terminal.py` & `ui/formatters.py`**: Rich interactive terminal dashboard.

---

## ⚙️ Dynamic Server Configurations

To allow dynamic adjustments on live servers without git merge conflicts:
- **`config/config.yaml.example`** is committed to Git as the template.
- **`config/config.yaml`** is git-ignored and lives dynamically on each server/machine (just like `.env`).
- If `config/config.yaml` does not exist, the system automatically creates it from `.example` on first run.

```yaml
app:
  name: "PowerOf3_Trading"
  trading_mode: "LIVE"          # "PAPER" or "LIVE"

timings:
  timezone: "Asia/Kolkata"
  pre_market_auth_time: "08:45" # Daily automated Kotak Neo login
  data_sync_time: "09:00"       # Refresh 30-day baseline stats
  market_open: "09:15"          # NSE cash market open
  opening_range_end: "09:20"    # 5-minute opening candle closes
  entry_start_time: "09:20"     # Earliest breakout order entry
  entry_cutoff_time: "14:15"    # No new entries allowed after 2:15 PM IST
  forced_square_off_time: "14:45" # FORCED INTRADAY SQUARE OFF (2:45 PM IST)
  market_close: "15:30"         # NSE cash market close

strategy:
  timeframe_minutes: 5          # 5-minute opening range
  rvol_lookback_days: 14        # Lookback for 9:15-9:20 volume baseline
  min_rvol_threshold: 2.0       # Minimum Relative Volume (2.0x baseline)
  top_n_stocks: 10              # Candidates traded per day (ranked by RVOL)
  require_candle_direction: true# Bullish candle -> Long; Bearish -> Short
  skip_doji: true               # Skip stocks where Open == Close
  atr_stop_loss_pct: 0.30       # Stop Loss = 0.30 * 14d ATR
  use_trailing_stop: true       # Ratchet trailing stop upward (Long) or downward (Short)
  trailing_stop_atr_pct: 0.30   # Trailing stop step = 0.30 * 14d ATR

risk:
  initial_capital: 100000.0     # Account capital
  risk_per_trade_pct: 0.01      # 1% risk per trade
  max_capital_per_trade_pct: 0.20 # Max 20% capital in single position
  max_portfolio_leverage: 4.0   # Max 4.0x total exposure
  max_open_positions: 10        # Max simultaneous positions
  max_daily_loss_pct: 0.10      # Circuit Breaker drawdown halt
```

---

## 🔐 Credentials Setup (`.env`)

Copy `config/.env.example` to `.env` in the project root:
```bash
cp config/.env.example .env
```
Fill in your credentials:
```ini
# Kotak Neo API v3 Credentials
# In Kotak Neo API v3, your API Access Token from the portal is the consumer key.
KOTAK_NEO_CONSUMER_KEY="your_api_access_token_here"
KOTAK_NEO_CONSUMER_SECRET=""
KOTAK_NEO_ENVIRONMENT="prod"

# Account Credentials
KOTAK_NEO_MOBILE_NUMBER="+91XXXXXXXXXX"
KOTAK_NEO_UCC="YOUR_UCC"
KOTAK_NEO_MPIN="123456"

# Automated 2FA TOTP Secret (Base32 Key)
# If provided, the system auto-generates TOTP at startup.
# If empty, the terminal prompts you once interactively for the 6-digit TOTP code.
KOTAK_NEO_TOTP_SECRET=""

# Telegram Notifications (broadcasts to 3 chat IDs)
TELEGRAM_BOT_TOKEN="your_bot_token"
TELEGRAM_CHAT_IDS="chat_id_1,chat_id_2,chat_id_3"
```

> **Note on TOTP Setup:**
> - If you already have TOTP enabled in your authenticator app without the base32 key, leave `KOTAK_NEO_TOTP_SECRET=""`. The engine will ask you to enter the 6-digit TOTP once from your terminal, and cache the session token in `data/sessions/neo_session.json` for the day.
> - If you want 100% headless automated login without any manual prompt, open your Kotak Neo app, de-register TOTP using your MPIN, re-register, and copy the Base32 setup key into `KOTAK_NEO_TOTP_SECRET`.

---

## 🖥️ CLI Commands

```bash
# 1. Run live trading daemon with real-time dashboard
python main.py start

# 2. View instantaneous terminal dashboard snapshot
python main.py dashboard

# 3. Place a manual discretionary order (interactive wizard or flags)
python main.py order
# Or via CLI arguments:
python main.py order --symbol RELIANCE --side BUY --qty 10 --type MKT

# 4. Authenticate with Kotak Neo and inspect account limits
python main.py login

# 5. Send test alert to all configured Telegram chat IDs
python main.py test-alert

# 6. Precompute / refresh 14-day baselines for Nifty 500 universe
python main.py sync-data
```

---

## 🛒 Manual Order Placement Wizard (`python main.py order`)

The framework includes a safe discretionary manual order execution tool:
- Interactively asks for:
  1. **Symbol** (e.g. `RELIANCE`, `TCS`, `INFY`)
  2. **Action** (`BUY` or `SELL`)
  3. **Order Type** (`MARKET` or `LIMIT`)
  4. **Quantity** (Shares)
  5. **Limit Price** (if Limit order)
  6. **Optional Stop Loss & Target**
- Displays an **Order Confirmation Table** summarizing the risk and order details.
- Requires explicit user confirmation (`y/n`).
- Submits order via Kotak Neo (`MIS` Intraday), prints the broker order status, and broadcasts the trade entry to your Telegram channels.

---

## ⏰ What Happens If You Start at 6:00 AM IST?

You can start the daemon anytime before market hours (e.g., at **6:00 AM IST**):

```bash
python main.py start
```

Here is the exact autonomous lifecycle:
1. **06:00 AM - 09:15 AM (Pre-Market)**:
   - Authenticates with Kotak Neo and caches the session token for the day.
   - Loads the 14-day baseline statistics for all 500 Nifty stocks from disk cache or baseline seed.
   - Guardrails are active: **all order placements are strictly blocked** before 09:20 IST.
   - Terminal dashboard displays `📍 Phase: 🌙 PRE-MARKET (Opens 09:15 IST)`.
2. **09:15 AM - 09:20 AM (Opening 5M Range)**:
   - Cash equity market opens.
   - The engine monitors the market as the initial 5-minute candle forms.
3. **09:20 AM (Morning Screener Trigger)**:
   - Screener calculates Relative Volume (RVOL) against the 14-day 09:15-09:20 baseline.
   - Filters stocks by liquidity and direction (Bullish candle -> LONG, Bearish candle -> SHORT).
   - Ranks and selects the **Top 10 RVOL stocks**.
   - Caches the candidates to disk and broadcasts the morning watchlist to Telegram.
   - Arms breakout trigger levels (`opening_high` for Longs, `opening_low` for Shorts).
4. **09:20 AM - 14:15 PM (Active Intraday Trading Window)**:
   - Listens for price breakout triggers.
   - On breakout: validates risk budget (1% capital risk), ensures maximum single position cap (20%), checks leverage (max 4x), checks 60-second cooldown, places intraday order, and sends Telegram alert.
   - Continuously ratchets dynamic trailing stops.
   - Broadcasts hourly P&L summaries to Telegram.
   - Enforces the daily drawdown circuit breaker (-3% to -10%).
5. **14:15 PM (Entry Cutoff)**:
   - No new positions are permitted. Only trailing stop exits or profit taking allowed.
6. **14:45 PM IST (Forced Square-Off)**:
   - **Forced liquidation**: every remaining open position is immediately closed with market orders.
   - Closes out the trading day safely and sends end-of-day P&L report.

---

## 🛡️ Anti-Bug & Safety Guardrails

1. **State Machine Idempotency**:
   Symbols track state (`IDLE`, `PENDING_ENTRY`, `OPEN`, `PENDING_EXIT`, `CLOSED`). Multiple buy or sell triggers cannot enter redundant positions.
2. **Double-Order Lockout**:
   60-second cooldown per stock prevents rapid-fire duplicate orders due to tick fluttering.
3. **Strict Time Gates**:
   - Entries strictly between **09:20 and 14:15 IST**.
   - Forced Square-off strictly at **14:45 IST (2:45 PM)**.
4. **Daily Drawdown Circuit Breaker**:
   If intraday loss hits the configured limit (e.g., -10%), trading halts immediately, all open positions are liquidated, and alerts are broadcast to Telegram.

---

## ☁️ GCloud Server Deployment (`deploy.sh`)

To deploy on a Google Cloud Compute Engine VM (Ubuntu/Debian):

```bash
git clone https://github.com/PurabA/powerof3_trading.git
cd powerof3_trading

# Configure secrets & dynamic server parameters
cp config/.env.example .env
nano .env

# Deploy (pulls latest code with hard reset, sets up virtualenv, installs Kotak SDK, launches Tmux)
chmod +x deploy.sh
./deploy.sh

# Attach to live dashboard inside tmux:
tmux attach -t powerof3

# Detach safely:
Press Ctrl+B, then D
```
