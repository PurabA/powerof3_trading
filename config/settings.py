"""
Configuration loader and Pydantic validation schema for powerof3_trading.
"""
import os
import yaml
from pathlib import Path
from typing import List, Optional
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# Load .env if present
load_dotenv()
load_dotenv(Path(__file__).parent / ".env")


class AppConfig(BaseModel):
    name: str = "PowerOf3_Trading"
    trading_mode: str = "PAPER"
    log_level: str = "INFO"


class TimingsConfig(BaseModel):
    timezone: str = "Asia/Kolkata"
    pre_market_auth_time: str = "08:45"
    data_sync_time: str = "09:00"
    market_open: str = "09:15"
    opening_range_end: str = "09:20"
    entry_start_time: str = "09:20"
    entry_cutoff_time: str = "14:15"
    forced_square_off_time: str = "14:45"
    market_close: str = "15:30"


class UniverseFiltersConfig(BaseModel):
    min_price: float = 50.0
    max_price: float = 100000.0
    min_avg_volume_14d: int = 100000
    min_avg_turnover_14d: float = 50000000.0
    min_atr_14d: float = 2.0
    min_atr_pct: float = 0.005
    min_history_days: int = 14


class StrategyConfig(BaseModel):
    timeframe_minutes: int = 5
    rvol_lookback_days: int = 14
    min_rvol_threshold: float = 2.0
    top_n_stocks: int = 10
    require_candle_direction: bool = True
    skip_doji: bool = True
    atr_stop_loss_pct: float = 0.30
    use_trailing_stop: bool = True
    trailing_stop_atr_pct: float = 0.30
    profit_target_pct: Optional[float] = None


class RiskConfig(BaseModel):
    initial_capital: float = 1000000.0
    risk_per_trade_pct: float = 0.01
    max_capital_per_trade_pct: float = 0.20
    max_portfolio_leverage: float = 4.0
    max_open_positions: int = 10
    max_daily_loss_pct: float = 0.03


class GuardrailsConfig(BaseModel):
    order_cooldown_seconds: int = 60
    max_order_retries: int = 3
    max_slippage_pct: float = 0.001
    prevent_double_orders: bool = True
    enforce_market_hours: bool = True


class FrictionConfig(BaseModel):
    brokerage_per_order: float = 20.0
    brokerage_pct: float = 0.0003
    stt_pct_sell: float = 0.00025
    exchange_txn_charge_pct: float = 0.0000325
    sebi_turnover_pct: float = 0.000001
    stamp_duty_buy_pct: float = 0.00003
    gst_pct: float = 0.18


class TelegramConfig(BaseModel):
    enabled: bool = True
    send_morning_screener: bool = True
    send_trade_signals: bool = True
    send_hourly_pnl: bool = True
    send_eod_summary: bool = True
    bot_token: str = Field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    chat_ids: List[str] = Field(default_factory=lambda: [
        cid.strip() for cid in os.getenv("TELEGRAM_CHAT_IDS", "").split(",") if cid.strip()
    ])


class KotakNeoCredentials(BaseModel):
    consumer_key: str = Field(default_factory=lambda: os.getenv("KOTAK_NEO_CONSUMER_KEY", ""))
    consumer_secret: str = Field(default_factory=lambda: os.getenv("KOTAK_NEO_CONSUMER_SECRET", ""))
    environment: str = Field(default_factory=lambda: os.getenv("KOTAK_NEO_ENVIRONMENT", "prod"))
    mobile_number: str = Field(default_factory=lambda: os.getenv("KOTAK_NEO_MOBILE_NUMBER", ""))
    ucc: str = Field(default_factory=lambda: os.getenv("KOTAK_NEO_UCC", ""))
    totp_secret: str = Field(default_factory=lambda: os.getenv("KOTAK_NEO_TOTP_SECRET", ""))
    mpin: str = Field(default_factory=lambda: os.getenv("KOTAK_NEO_MPIN", ""))


class TradingSystemSettings(BaseModel):
    app: AppConfig
    timings: TimingsConfig
    universe_filters: UniverseFiltersConfig
    strategy: StrategyConfig
    risk: RiskConfig
    guardrails: GuardrailsConfig
    friction: FrictionConfig
    telegram: TelegramConfig
    neo: KotakNeoCredentials


def load_settings(config_path: Optional[str] = None) -> TradingSystemSettings:
    """Loads and validates configuration from YAML and environment."""
    base_dir = Path(__file__).resolve().parent
    if not config_path:
        cfg_file = base_dir / "config.yaml"
        if not cfg_file.exists():
            example_file = base_dir / "config.yaml.example"
            if example_file.exists():
                import shutil
                shutil.copy(example_file, cfg_file)
                logger_msg = f"Created {cfg_file} from template {example_file}"
                print(f"ℹ️ {logger_msg}")
            else:
                raise FileNotFoundError(f"Neither config.yaml nor config.yaml.example found in {base_dir}")
        config_path = str(cfg_file)

    with open(config_path, "r") as f:
        raw_dict = yaml.safe_load(f)

    # Attach credentials from env
    raw_dict["neo"] = KotakNeoCredentials().model_dump()
    
    # Merge telegram keys
    tg_data = raw_dict.get("telegram", {})
    tg_data["bot_token"] = os.getenv("TELEGRAM_BOT_TOKEN", tg_data.get("bot_token", ""))
    chat_ids_env = os.getenv("TELEGRAM_CHAT_IDS", "")
    if chat_ids_env:
        tg_data["chat_ids"] = [cid.strip() for cid in chat_ids_env.split(",") if cid.strip()]
    raw_dict["telegram"] = tg_data

    return TradingSystemSettings(**raw_dict)
