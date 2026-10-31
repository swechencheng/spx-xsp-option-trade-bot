"""US Equity / Options Market Schedule and Early Close Manager.

Handles regular close (16:00 ET) vs Early Close (13:00 ET, e.g. Black Friday,
Christmas Eve, July 3rd) and dynamically determines the target strategy
trigger time (15:52 ET on regular days, 12:52 ET on early close days).
"""

import argparse
import datetime
import sys
from typing import Optional, Tuple
import pytz

EASTERN_TZ = pytz.timezone("America/New_York")


def get_thanksgiving_date(year: int) -> datetime.date:
    """Thanksgiving is the 4th Thursday in November."""
    nov1 = datetime.date(year, 11, 1)
    # 0=Monday, 3=Thursday
    first_thu = 1 + (3 - nov1.weekday()) % 7
    return datetime.date(year, 11, first_thu + 21)


def is_early_close(d: datetime.date) -> bool:
    """Return True if date d is an Early Close (13:00 ET) day on NYSE / CBOE.

    Standard Early Close days:
    1. Black Friday (day after Thanksgiving)
    2. Christmas Eve (Dec 24, when Christmas Day is a weekday)
    3. Day before Independence Day (July 3, when July 4 is Tue/Wed/Thu/Fri)
    """
    if d.weekday() >= 5:
        return False

    year = d.year

    # 1. Black Friday
    thanksgiving = get_thanksgiving_date(year)
    black_friday = thanksgiving + datetime.timedelta(days=1)
    if d == black_friday:
        return True

    # 2. Christmas Eve (Dec 24)
    # If Dec 24 is a weekday and Dec 25 is not a weekend:
    # (If Dec 25 is Sat, Friday Dec 24 is Christmas Day observed = Full holiday, closed all day)
    dec25 = datetime.date(year, 12, 25)
    if d.month == 12 and d.day == 24:
        if dec25.weekday() != 5:  # Not Saturday
            return True

    # 3. Day before July 4th (July 3)
    # Only if July 4 is Tue, Wed, Thu, Fri.
    # (If July 4 is Saturday, July 3 is Independence Day observed = Full holiday, closed all day.
    #  If July 4 is Sunday, July 5 is observed holiday.
    #  If July 4 is Monday, July 3 is Sunday.)
    july4 = datetime.date(year, 7, 4)
    if d.month == 7 and d.day == 3:
        if july4.weekday() in (1, 2, 3, 4):  # Tue, Wed, Thu, Fri
            return True

    return False


def get_market_close_time(d: datetime.date) -> datetime.time:
    """Return market close time in US/Eastern (13:00 for early close, 16:00 for regular)."""
    return datetime.time(13, 0) if is_early_close(d) else datetime.time(16, 0)


def get_target_trigger_time(d: datetime.date) -> datetime.time:
    """Return trigger time for automated bot (8 minutes before close: 12:52 or 15:52 ET)."""
    return datetime.time(12, 52) if is_early_close(d) else datetime.time(15, 52)


def is_target_trigger_time(now_dt: Optional[datetime.datetime] = None) -> bool:
    """Check if now_dt matches the target trigger hour and minute in America/New_York."""
    now = now_dt or datetime.datetime.now(EASTERN_TZ)
    if now.tzinfo is None:
        now = EASTERN_TZ.localize(now)
    else:
        now = now.astimezone(EASTERN_TZ)

    # Weekend check: 0=Mon, 4=Fri, 5=Sat, 6=Sun
    if now.weekday() >= 5:
        return False

    target_time = get_target_trigger_time(now.date())
    return now.hour == target_time.hour and now.minute == target_time.minute


def get_minutes_until_close(now_dt: Optional[datetime.datetime] = None) -> float:
    """Calculate minutes until close on the given day."""
    now = now_dt or datetime.datetime.now(EASTERN_TZ)
    if now.tzinfo is None:
        now = EASTERN_TZ.localize(now)
    else:
        now = now.astimezone(EASTERN_TZ)

    close_time = get_market_close_time(now.date())
    close_dt = now.replace(
        hour=close_time.hour, minute=close_time.minute, second=0, microsecond=0
    )
    diff = (close_dt - now).total_seconds() / 60.0
    return diff


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Market Schedule Checker")
    parser.add_argument(
        "--check-trigger",
        action="store_true",
        help="Exit with 0 if current minute is trigger minute, else 1",
    )
    parser.add_argument(
        "--info",
        action="store_true",
        help="Print market schedule info for today",
    )
    args = parser.parse_args()

    now = datetime.datetime.now(EASTERN_TZ)
    today = now.date()

    if args.check_trigger:
        if is_target_trigger_time(now):
            sys.exit(0)
        else:
            sys.exit(1)

    close_time = get_market_close_time(today)
    trigger_time = get_target_trigger_time(today)
    early = is_early_close(today)
    print(f"Date: {today} ({now.strftime('%A')})")
    print(f"Current Eastern Time: {now.strftime('%H:%M:%S %Z')}")
    print(f"Early Close: {early}")
    print(f"Market Close Time: {close_time.strftime('%H:%M')} ET")
    print(f"Target Bot Trigger Time: {trigger_time.strftime('%H:%M')} ET")
    print(f"Minutes Until Close: {get_minutes_until_close(now):.1f} min")

