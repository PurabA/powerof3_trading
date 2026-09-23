"""
Tests for core/timeutils.py ensuring strict Asia/Kolkata (IST) schedule enforcement.
"""
from datetime import datetime
import zoneinfo
import pytest

from core.timeutils import TimeManager, IST
from config.settings import TimingsConfig


@pytest.fixture
def time_manager():
    config = TimingsConfig(
        timezone="Asia/Kolkata",
        market_open="09:15",
        opening_range_end="09:20",
        entry_start_time="09:20",
        entry_cutoff_time="14:15",
        forced_square_off_time="14:45",
        market_close="15:30"
    )
    return TimeManager(config)


def test_market_open_timing(time_manager):
    # 09:14 IST - Not open yet
    t_before = datetime(2026, 9, 23, 9, 14, 0, tzinfo=IST)
    assert not time_manager.is_market_open(t_before)

    # 09:15 IST - Market open
    t_open = datetime(2026, 9, 23, 9, 15, 0, tzinfo=IST)
    assert time_manager.is_market_open(t_open)

    # 15:30 IST - Market closed
    t_closed = datetime(2026, 9, 23, 15, 30, 0, tzinfo=IST)
    assert not time_manager.is_market_open(t_closed)


def test_opening_range_timing(time_manager):
    # 09:17 IST - Opening range active
    t_orb = datetime(2026, 9, 23, 9, 17, 0, tzinfo=IST)
    assert time_manager.is_opening_range_active(t_orb)

    # 09:20 IST - Opening range finished
    t_orb_end = datetime(2026, 9, 23, 9, 20, 0, tzinfo=IST)
    assert not time_manager.is_opening_range_active(t_orb_end)


def test_entry_allowed_window(time_manager):
    # 09:19 IST - Too early for entries
    t_early = datetime(2026, 9, 23, 9, 19, 59, tzinfo=IST)
    assert not time_manager.is_entry_allowed(t_early)

    # 09:20 IST - Entry allowed
    t_entry = datetime(2026, 9, 23, 9, 20, 0, tzinfo=IST)
    assert time_manager.is_entry_allowed(t_entry)

    # 14:14 IST - Entry allowed
    t_mid = datetime(2026, 9, 23, 14, 14, 0, tzinfo=IST)
    assert time_manager.is_entry_allowed(t_mid)

    # 14:15 IST - Cutoff passed, no new entries
    t_cutoff = datetime(2026, 9, 23, 14, 15, 0, tzinfo=IST)
    assert not time_manager.is_entry_allowed(t_cutoff)


def test_forced_square_off_time(time_manager):
    # 14:44 IST - Not yet square-off
    t_before_sq = datetime(2026, 9, 23, 14, 44, 59, tzinfo=IST)
    assert not time_manager.is_square_off_time(t_before_sq)

    # 14:45 IST - Forced Square-Off!
    t_sq = datetime(2026, 9, 23, 14, 45, 0, tzinfo=IST)
    assert time_manager.is_square_off_time(t_sq)

    # 15:00 IST - Still square-off
    t_after_sq = datetime(2026, 9, 23, 15, 0, 0, tzinfo=IST)
    assert time_manager.is_square_off_time(t_after_sq)
