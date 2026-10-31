"""Event Filter for Macro Announcements and Major Tech Earnings.

Enforces trading blackouts:
1. Macro Events (FOMC, CPI, NFP):
   - No trade on the eve of the event (the previous trading day evening).
   - No trade on the day of the event (the event day evening).
2. Major Tech Earnings:
   - No trade if a monitored mega-cap tech stock reports earnings tomorrow pre-market (BMO)
     or today after-hours (AMC).
"""

import argparse
import datetime
import json
import logging
import os
from typing import Dict, List, Optional, Set, Tuple
import pytz
import requests

logger = logging.getLogger(__name__)

EASTERN_TZ = pytz.timezone("America/New_York")

DEFAULT_MONITORED_TECH: List[str] = [
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "GOOGL",
    "GOOG",
    "META",
    "TSLA",
]

# Official Federal Reserve FOMC Statement Release Dates (Day 2 of 2-day meeting)
FOMC_DATES: Dict[datetime.date, str] = {
    # 2024
    datetime.date(2024, 1, 31): "FOMC Rate Decision",
    datetime.date(2024, 3, 20): "FOMC Rate Decision",
    datetime.date(2024, 5, 1): "FOMC Rate Decision",
    datetime.date(2024, 6, 12): "FOMC Rate Decision",
    datetime.date(2024, 7, 31): "FOMC Rate Decision",
    datetime.date(2024, 9, 18): "FOMC Rate Decision",
    datetime.date(2024, 11, 7): "FOMC Rate Decision",
    datetime.date(2024, 12, 18): "FOMC Rate Decision",
    # 2025
    datetime.date(2025, 1, 29): "FOMC Rate Decision",
    datetime.date(2025, 3, 19): "FOMC Rate Decision",
    datetime.date(2025, 5, 7): "FOMC Rate Decision",
    datetime.date(2025, 6, 18): "FOMC Rate Decision",
    datetime.date(2025, 7, 30): "FOMC Rate Decision",
    datetime.date(2025, 9, 17): "FOMC Rate Decision",
    datetime.date(2025, 10, 29): "FOMC Rate Decision",
    datetime.date(2025, 12, 10): "FOMC Rate Decision",
    # 2026
    datetime.date(2026, 1, 28): "FOMC Rate Decision",
    datetime.date(2026, 3, 18): "FOMC Rate Decision",
    datetime.date(2026, 4, 29): "FOMC Rate Decision",
    datetime.date(2026, 6, 17): "FOMC Rate Decision",
    datetime.date(2026, 7, 29): "FOMC Rate Decision",
    datetime.date(2026, 9, 16): "FOMC Rate Decision",
    datetime.date(2026, 10, 28): "FOMC Rate Decision",
    datetime.date(2026, 12, 9): "FOMC Rate Decision",
    # 2027
    datetime.date(2027, 1, 27): "FOMC Rate Decision",
    datetime.date(2027, 3, 17): "FOMC Rate Decision",
    datetime.date(2027, 4, 28): "FOMC Rate Decision",
    datetime.date(2027, 6, 16): "FOMC Rate Decision",
    datetime.date(2027, 7, 28): "FOMC Rate Decision",
    datetime.date(2027, 9, 22): "FOMC Rate Decision",
    datetime.date(2027, 11, 3): "FOMC Rate Decision",
    datetime.date(2027, 12, 15): "FOMC Rate Decision",
}

# Bureau of Labor Statistics (BLS) CPI Release Dates (8:30 AM ET)
CPI_DATES: Dict[datetime.date, str] = {
    # 2024
    datetime.date(2024, 1, 11): "CPI Inflation Report",
    datetime.date(2024, 2, 13): "CPI Inflation Report",
    datetime.date(2024, 3, 12): "CPI Inflation Report",
    datetime.date(2024, 4, 10): "CPI Inflation Report",
    datetime.date(2024, 5, 15): "CPI Inflation Report",
    datetime.date(2024, 6, 12): "CPI Inflation Report",
    datetime.date(2024, 7, 11): "CPI Inflation Report",
    datetime.date(2024, 8, 14): "CPI Inflation Report",
    datetime.date(2024, 9, 11): "CPI Inflation Report",
    datetime.date(2024, 10, 10): "CPI Inflation Report",
    datetime.date(2024, 11, 13): "CPI Inflation Report",
    datetime.date(2024, 12, 11): "CPI Inflation Report",
    # 2025
    datetime.date(2025, 1, 15): "CPI Inflation Report",
    datetime.date(2025, 2, 12): "CPI Inflation Report",
    datetime.date(2025, 3, 12): "CPI Inflation Report",
    datetime.date(2025, 4, 10): "CPI Inflation Report",
    datetime.date(2025, 5, 13): "CPI Inflation Report",
    datetime.date(2025, 6, 11): "CPI Inflation Report",
    datetime.date(2025, 7, 11): "CPI Inflation Report",
    datetime.date(2025, 8, 13): "CPI Inflation Report",
    datetime.date(2025, 9, 11): "CPI Inflation Report",
    datetime.date(2025, 10, 15): "CPI Inflation Report",
    datetime.date(2025, 11, 12): "CPI Inflation Report",
    datetime.date(2025, 12, 10): "CPI Inflation Report",
    # 2026
    datetime.date(2026, 1, 14): "CPI Inflation Report",
    datetime.date(2026, 2, 11): "CPI Inflation Report",
    datetime.date(2026, 3, 11): "CPI Inflation Report",
    datetime.date(2026, 4, 14): "CPI Inflation Report",
    datetime.date(2026, 5, 12): "CPI Inflation Report",
    datetime.date(2026, 6, 10): "CPI Inflation Report",
    datetime.date(2026, 7, 14): "CPI Inflation Report",
    datetime.date(2026, 8, 12): "CPI Inflation Report",
    datetime.date(2026, 9, 11): "CPI Inflation Report",
    datetime.date(2026, 10, 14): "CPI Inflation Report",
    datetime.date(2026, 11, 12): "CPI Inflation Report",
    datetime.date(2026, 12, 10): "CPI Inflation Report",
    # 2027
    datetime.date(2027, 1, 13): "CPI Inflation Report",
    datetime.date(2027, 2, 10): "CPI Inflation Report",
    datetime.date(2027, 3, 10): "CPI Inflation Report",
    datetime.date(2027, 4, 13): "CPI Inflation Report",
    datetime.date(2027, 5, 12): "CPI Inflation Report",
    datetime.date(2027, 6, 10): "CPI Inflation Report",
    datetime.date(2027, 7, 14): "CPI Inflation Report",
    datetime.date(2027, 8, 11): "CPI Inflation Report",
    datetime.date(2027, 9, 14): "CPI Inflation Report",
    datetime.date(2027, 10, 13): "CPI Inflation Report",
    datetime.date(2027, 11, 10): "CPI Inflation Report",
    datetime.date(2027, 12, 9): "CPI Inflation Report",
}

# Bureau of Labor Statistics (BLS) Employment Situation / NFP (8:30 AM ET)
NFP_DATES: Dict[datetime.date, str] = {
    # 2024
    datetime.date(2024, 1, 5): "Non-Farm Payrolls (NFP)",
    datetime.date(2024, 2, 2): "Non-Farm Payrolls (NFP)",
    datetime.date(2024, 3, 8): "Non-Farm Payrolls (NFP)",
    datetime.date(2024, 4, 5): "Non-Farm Payrolls (NFP)",
    datetime.date(2024, 5, 3): "Non-Farm Payrolls (NFP)",
    datetime.date(2024, 6, 7): "Non-Farm Payrolls (NFP)",
    datetime.date(2024, 7, 5): "Non-Farm Payrolls (NFP)",
    datetime.date(2024, 8, 2): "Non-Farm Payrolls (NFP)",
    datetime.date(2024, 9, 6): "Non-Farm Payrolls (NFP)",
    datetime.date(2024, 10, 4): "Non-Farm Payrolls (NFP)",
    datetime.date(2024, 11, 1): "Non-Farm Payrolls (NFP)",
    datetime.date(2024, 12, 6): "Non-Farm Payrolls (NFP)",
    # 2025
    datetime.date(2025, 1, 10): "Non-Farm Payrolls (NFP)",
    datetime.date(2025, 2, 7): "Non-Farm Payrolls (NFP)",
    datetime.date(2025, 3, 7): "Non-Farm Payrolls (NFP)",
    datetime.date(2025, 4, 4): "Non-Farm Payrolls (NFP)",
    datetime.date(2025, 5, 2): "Non-Farm Payrolls (NFP)",
    datetime.date(2025, 6, 6): "Non-Farm Payrolls (NFP)",
    datetime.date(2025, 7, 3): "Non-Farm Payrolls (NFP)",
    datetime.date(2025, 8, 1): "Non-Farm Payrolls (NFP)",
    datetime.date(2025, 9, 5): "Non-Farm Payrolls (NFP)",
    datetime.date(2025, 10, 3): "Non-Farm Payrolls (NFP)",
    datetime.date(2025, 11, 7): "Non-Farm Payrolls (NFP)",
    datetime.date(2025, 12, 5): "Non-Farm Payrolls (NFP)",
    # 2026
    datetime.date(2026, 1, 9): "Non-Farm Payrolls (NFP)",
    datetime.date(2026, 2, 6): "Non-Farm Payrolls (NFP)",
    datetime.date(2026, 3, 6): "Non-Farm Payrolls (NFP)",
    datetime.date(2026, 4, 3): "Non-Farm Payrolls (NFP)",
    datetime.date(2026, 5, 8): "Non-Farm Payrolls (NFP)",
    datetime.date(2026, 6, 5): "Non-Farm Payrolls (NFP)",
    datetime.date(2026, 7, 2): "Non-Farm Payrolls (NFP)",
    datetime.date(2026, 8, 7): "Non-Farm Payrolls (NFP)",
    datetime.date(2026, 9, 4): "Non-Farm Payrolls (NFP)",
    datetime.date(2026, 10, 2): "Non-Farm Payrolls (NFP)",
    datetime.date(2026, 11, 6): "Non-Farm Payrolls (NFP)",
    datetime.date(2026, 12, 4): "Non-Farm Payrolls (NFP)",
    # 2027
    datetime.date(2027, 1, 8): "Non-Farm Payrolls (NFP)",
    datetime.date(2027, 2, 5): "Non-Farm Payrolls (NFP)",
    datetime.date(2027, 3, 5): "Non-Farm Payrolls (NFP)",
    datetime.date(2027, 4, 2): "Non-Farm Payrolls (NFP)",
    datetime.date(2027, 5, 7): "Non-Farm Payrolls (NFP)",
    datetime.date(2027, 6, 4): "Non-Farm Payrolls (NFP)",
    datetime.date(2027, 7, 2): "Non-Farm Payrolls (NFP)",
    datetime.date(2027, 8, 6): "Non-Farm Payrolls (NFP)",
    datetime.date(2027, 9, 3): "Non-Farm Payrolls (NFP)",
    datetime.date(2027, 10, 8): "Non-Farm Payrolls (NFP)",
    datetime.date(2027, 11, 5): "Non-Farm Payrolls (NFP)",
    datetime.date(2027, 12, 3): "Non-Farm Payrolls (NFP)",
}


def get_all_macro_events() -> Dict[datetime.date, str]:
    """Combine all built-in macro events and optional events_calendar.json overrides."""
    events: Dict[datetime.date, str] = {}
    events.update(FOMC_DATES)
    events.update(CPI_DATES)
    events.update(NFP_DATES)

    # Optional custom file: events_calendar.json
    custom_file = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "events_calendar.json"
    )
    if os.path.exists(custom_file):
        try:
            with open(custom_file, "r", encoding="utf-8") as f:
                custom_data = json.load(f)
            for date_str, desc in custom_data.items():
                d = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
                events[d] = desc
        except Exception as e:
            logger.warning("Failed to load custom events_calendar.json: %s", e)

    return events


def get_next_trading_day(d: datetime.date) -> datetime.date:
    """Get next weekday date (skips Saturday/Sunday)."""
    next_day = d + datetime.timedelta(days=1)
    while next_day.weekday() >= 5:  # 5=Sat, 6=Sun
        next_day += datetime.timedelta(days=1)
    return next_day


def check_macro_event_filter(today: datetime.date) -> Tuple[bool, Optional[str]]:
    """Check if today or the next trading day has a major macro event (FOMC, CPI, NFP).

    Rule: Do not trade on the evening of the event (today), OR the evening before (eve of event).
    """
    events = get_all_macro_events()

    # 1. Event is Today
    if today in events:
        event_name = events[today]
        return True, f"Macro event TODAY ({event_name}). Trading blocked on event day."

    # 2. Event is Next Trading Day (Eve of event)
    next_td = get_next_trading_day(today)
    if next_td in events:
        event_name = events[next_td]
        return (
            True,
            f"Macro event TOMORROW / next trading day ({event_name} on {next_td}). "
            "Trading blocked on eve of event.",
        )

    return False, None


def check_tech_earnings_filter(
    today: datetime.date,
    monitored_symbols: Optional[List[str]] = None,
) -> Tuple[bool, Optional[str]]:
    """Check if major tech stocks have pre-market earnings next day or after-hours today.

    Uses the Nasdaq Earnings Calendar API with a yfinance fallback.
    """
    symbols = set(monitored_symbols or DEFAULT_MONITORED_TECH)
    next_td = get_next_trading_day(today)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }

    # Helper: Query Nasdaq calendar API
    def query_nasdaq(target_date: datetime.date) -> List[dict]:
        try:
            url = f"https://api.nasdaq.com/api/calendar/earnings?date={target_date.strftime('%Y-%m-%d')}"
            res = requests.get(url, headers=headers, timeout=5)
            if res.status_code == 200:
                data = res.json()
                return data.get("data", {}).get("rows", []) or []
        except Exception as e:
            logger.debug("Nasdaq API query failed for %s: %s", target_date, e)
        return []

    # 1. Check Tomorrow / Next Trading Day Pre-Market Earnings
    rows_next = query_nasdaq(next_td)
    for row in rows_next:
        sym = row.get("symbol")
        time_str = (row.get("time") or "").lower()
        if sym in symbols:
            # If pre-market or time not specified (assume conservative blocking)
            if "pre-market" in time_str or "time-not-supplied" in time_str or not time_str:
                name = row.get("name") or sym
                return (
                    True,
                    f"Major tech stock {sym} ({name}) reports earnings TOMORROW pre-market "
                    f"({next_td}). Trading blocked.",
                )

    # 2. Check Today After-Hours Earnings
    rows_today = query_nasdaq(today)
    for row in rows_today:
        sym = row.get("symbol")
        time_str = (row.get("time") or "").lower()
        if sym in symbols:
            if "after-hours" in time_str or "time-not-supplied" in time_str or not time_str:
                name = row.get("name") or sym
                return (
                    True,
                    f"Major tech stock {sym} ({name}) reports earnings TODAY after-hours "
                    f"({today}). Trading blocked.",
                )

    # 3. Fallback: yfinance calendar check
    try:
        import yfinance as yf

        for sym in symbols:
            try:
                ticker = yf.Ticker(sym)
                cal = ticker.calendar
                if cal and "Earnings Date" in cal:
                    e_dates = cal["Earnings Date"]
                    if isinstance(e_dates, list):
                        for ed in e_dates:
                            ed_date = ed if isinstance(ed, datetime.date) else ed.date()
                            if ed_date in (today, next_td):
                                return (
                                    True,
                                    f"Major tech stock {sym} reports earnings on {ed_date} "
                                    "(detected via yfinance). Trading blocked.",
                                )
            except Exception:
                pass
    except Exception:
        pass

    return False, None


def should_skip_trade(
    today: Optional[datetime.date] = None,
    monitored_symbols: Optional[List[str]] = None,
) -> Tuple[bool, Optional[str]]:
    """Master filter: returns (True, reason) if trading should be aborted due to events."""
    d = today or datetime.datetime.now(EASTERN_TZ).date()

    # 1. Macro Economic Events Check (FOMC, CPI, NFP)
    skip_macro, reason_macro = check_macro_event_filter(d)
    if skip_macro:
        return True, reason_macro

    # 2. Major Tech Earnings Check
    skip_tech, reason_tech = check_tech_earnings_filter(d, monitored_symbols)
    if skip_tech:
        return True, reason_tech

    return False, None


if __name__ == "__main__":
    now_d = datetime.datetime.now(EASTERN_TZ).date()
    print(f"Checking event filters for today: {now_d} (Eastern Time)...")
    skip, reason = should_skip_trade(now_d)
    if skip:
        print(f"🚫 TRADING BLOCKED: {reason}")
    else:
        print("✅ No blocking macro events or major tech earnings detected. Trading allowed.")
