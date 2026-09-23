"""
Time and market schedule utilities strictly enforcing Asia/Kolkata (IST).
Guarantees consistent market timing regardless of server timezone (e.g. UTC on GCloud).
"""
import zoneinfo
from datetime import datetime, time, timedelta
from typing import Optional
from config.settings import TimingsConfig

IST = zoneinfo.ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    """Returns the current datetime in Asia/Kolkata timezone."""
    return datetime.now(IST)


def today_ist_str() -> str:
    """Returns today's date formatted as YYYY-MM-DD in IST."""
    return now_ist().strftime("%Y-%m-%d")


def parse_time_str(time_str: str) -> time:
    """Parses 'HH:MM' string to a datetime.time object."""
    parts = time_str.split(":")
    return time(hour=int(parts[0]), minute=int(parts[1]))


class TimeManager:
    """
    Evaluates market schedules and timing gates against strict Asia/Kolkata clock.
    """
    def __init__(self, config: TimingsConfig):
        self.config = config
        self.market_open = parse_time_str(config.market_open)
        self.opening_range_end = parse_time_str(config.opening_range_end)
        self.entry_start = parse_time_str(config.entry_start_time)
        self.entry_cutoff = parse_time_str(config.entry_cutoff_time)
        self.square_off = parse_time_str(config.forced_square_off_time)
        self.market_close = parse_time_str(config.market_close)

    def current_time_ist(self) -> time:
        return now_ist().time()

    def is_weekday(self, dt: Optional[datetime] = None) -> bool:
        target = dt or now_ist()
        return target.weekday() < 5  # 0=Mon, 4=Fri

    def is_market_open(self, dt: Optional[datetime] = None) -> bool:
        t = (dt or now_ist()).time()
        return self.market_open <= t < self.market_close

    def is_opening_range_active(self, dt: Optional[datetime] = None) -> bool:
        """True between 09:15 and 09:20 IST."""
        t = (dt or now_ist()).time()
        return self.market_open <= t < self.opening_range_end

    def is_entry_allowed(self, dt: Optional[datetime] = None) -> bool:
        """True strictly between 09:20 IST and 14:15 IST (2:15 PM)."""
        t = (dt or now_ist()).time()
        return self.entry_start <= t < self.entry_cutoff

    def is_square_off_time(self, dt: Optional[datetime] = None) -> bool:
        """True when time has reached or passed 14:45 IST (2:45 PM)."""
        t = (dt or now_ist()).time()
        return t >= self.square_off

    def is_market_closed(self, dt: Optional[datetime] = None) -> bool:
        t = (dt or now_ist()).time()
        return t >= self.market_close

    def seconds_until(self, target_time_str: str) -> float:
        """Returns seconds from now until target HH:MM in IST today."""
        target_t = parse_time_str(target_time_str)
        now = now_ist()
        target_dt = now.replace(
            hour=target_t.hour, minute=target_t.minute, second=0, microsecond=0
        )
        delta = (target_dt - now).total_seconds()
        return max(0.0, delta)

    def get_market_phase_str(self, dt: Optional[datetime] = None) -> str:
        """Returns human-readable current market phase in IST."""
        t = (dt or now_ist()).time()
        if t < self.market_open:
            return "🌙 PRE-MARKET (Opens 09:15 IST)"
        elif t < self.opening_range_end:
            return "📊 OPENING 5M CANDLE (09:15 - 09:20 IST)"
        elif t < self.entry_cutoff:
            return "🟢 ACTIVE TRADING (09:20 - 14:15 IST)"
        elif t < self.square_off:
            return "⚠️ EXIT ONLY (Cutoff passed)"
        elif t < self.market_close:
            return "🛑 FORCED SQUARE-OFF (14:45 IST)"
        else:
            return "🌙 MARKET CLOSED (Post 15:30 IST)"

