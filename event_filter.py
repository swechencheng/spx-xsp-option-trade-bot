"""Event Filter for Macro Announcements and Major Tech Earnings.

Enforces strict weekly trading blackouts:
1. Macro Events (FOMC, CPI, NFP, US Elections, ISM PMI):
   - If any macro event occurs in a week (Monday to Friday), no trading for that entire week.
   - If the next trading day has an event (e.g. across weekend), no trading on the eve.
2. Major Tech Earnings (AAPL, MSFT, NVDA, AMZN, GOOGL, GOOG, META, TSLA):
   - If any monitored tech company reports earnings in a week (Monday to Friday), no trading for that entire week.
   - If the next trading day has earnings, no trading on the eve.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
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


# US General Elections (Presidential and Midterm Elections - 1st Tuesday after 1st Monday in Nov)
ELECTION_DATES: Dict[datetime.date, str] = {
    datetime.date(2024, 11, 5): "US Presidential Election",
    datetime.date(2026, 11, 3): "US Midterm Election",
    datetime.date(2028, 11, 7): "US Presidential Election",
    datetime.date(2030, 11, 5): "US Midterm Election",
}


def get_pmi_dates(year: int) -> Dict[datetime.date, str]:
    """Generate ISM Manufacturing (1st business day) and Services (3rd business day) PMI dates."""
    dates: Dict[datetime.date, str] = {}
    for m in range(1, 13):
        d = datetime.date(year, m, 1)
        count = 0
        b1, b3 = None, None
        while True:
            is_hol = (
                (d.month == 1 and d.day == 1)
                or (d.month == 7 and d.day == 4)
                or (d.month == 12 and d.day == 25)
            )
            if d.weekday() < 5 and not is_hol:
                count += 1
                if count == 1:
                    b1 = d
                elif count == 3:
                    b3 = d
                    break
            d += datetime.timedelta(days=1)
        if b1:
            dates[b1] = "ISM Manufacturing PMI"
        if b3:
            dates[b3] = "ISM Services PMI"
    return dates


def get_all_macro_events() -> Dict[datetime.date, str]:
    """Combine all built-in macro events (FOMC, CPI, NFP, Elections, PMI) and custom overrides."""
    events: Dict[datetime.date, str] = {}
    events.update(FOMC_DATES)
    events.update(CPI_DATES)
    events.update(NFP_DATES)
    events.update(ELECTION_DATES)

    # Add PMI dates for past, current, and upcoming years
    current_year = datetime.date.today().year
    for y in range(current_year - 1, current_year + 3):
        events.update(get_pmi_dates(y))

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


def normalize_date(d: Optional[datetime.date | datetime.datetime]) -> datetime.date:
    """Normalize input date or datetime to a date object in Eastern Time."""
    if d is None:
        return datetime.datetime.now(EASTERN_TZ).date()
    if isinstance(d, datetime.datetime):
        if d.tzinfo is not None:
            return d.astimezone(EASTERN_TZ).date()
        return d.date()
    return d


def get_week_bounds(d: datetime.date) -> Tuple[datetime.date, datetime.date]:
    """Return (monday, friday) bounding the trading week of date d."""
    monday = d - datetime.timedelta(days=d.weekday())
    friday = monday + datetime.timedelta(days=4)
    return monday, friday


def get_week_trading_days(d: datetime.date) -> List[datetime.date]:
    """Return all trading days (Monday to Friday) of the week containing date d."""
    monday = d - datetime.timedelta(days=d.weekday())
    return [monday + datetime.timedelta(days=i) for i in range(5)]


def get_next_trading_day(d: datetime.date) -> datetime.date:
    """Get next weekday date (skips Saturday/Sunday)."""
    next_day = d + datetime.timedelta(days=1)
    while next_day.weekday() >= 5:  # 5=Sat, 6=Sun
        next_day += datetime.timedelta(days=1)
    return next_day


def check_macro_event_filter(
    today: Optional[datetime.date | datetime.datetime] = None,
) -> Tuple[bool, Optional[str]]:
    """Check if the week containing today or the next trading day has a major macro event.

    Strict weekly rule: If any day in the trading week (Monday through Friday) has a major macro event
    (FOMC, CPI, NFP, US Elections, ISM PMI), trading is blocked for the ENTIRE week.
    Additionally, if the next trading day (e.g. across a weekend) has an event, trading is blocked on the eve of the event.
    """
    d = normalize_date(today)
    events = get_all_macro_events()

    monday, friday = get_week_bounds(d)
    week_days = [monday + datetime.timedelta(days=i) for i in range(7)]

    # 1. Check if any day in the current week has a macro event
    events_this_week = []
    for day in week_days:
        if day in events:
            events_this_week.append(f"{events[day]} on {day}")

    if events_this_week:
        events_str = "; ".join(events_this_week)
        return (
            True,
            f"Macro event ({events_str}) occurs this week ({monday} to {friday}). "
            f"Trading blocked for the entire week.",
        )

    # 2. Check if next trading day (e.g. Monday following Friday trade) has a macro event
    next_td = get_next_trading_day(d)
    if next_td in events:
        event_name = events[next_td]
        return (
            True,
            f"Macro event ({event_name} on {next_td}) occurs on next trading day. "
            f"Trading blocked on eve of event.",
        )

    return False, None


def check_tech_earnings_filter(
    today: Optional[datetime.date | datetime.datetime] = None,
    monitored_symbols: Optional[List[str]] = None,
) -> Tuple[bool, Optional[str]]:
    """Check if major tech stocks report earnings anytime during the current trading week or next trading day.

    Strict weekly rule: If any monitored mega-cap tech stock reports earnings during the current
    week (Monday through Friday), trading is blocked for the ENTIRE week.
    Additionally, if the next trading day (e.g. next Monday) has earnings, trading is blocked on the eve.
    """
    d = normalize_date(today)
    symbols = set(monitored_symbols or DEFAULT_MONITORED_TECH)
    monday, friday = get_week_bounds(d)
    week_trading_days = get_week_trading_days(d)
    next_td = get_next_trading_day(d)

    # Days to inspect: all trading days of this week, plus next trading day if outside this week
    target_dates = list(week_trading_days)
    if next_td not in target_dates:
        target_dates.append(next_td)

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

    # Parallel fetch across all target dates
    nasdaq_results: Dict[datetime.date, List[dict]] = {}
    with ThreadPoolExecutor(max_workers=min(len(target_dates), 6)) as executor:
        future_to_date = {executor.submit(query_nasdaq, dt): dt for dt in target_dates}
        for future in future_to_date:
            dt = future_to_date[future]
            try:
                nasdaq_results[dt] = future.result()
            except Exception as e:
                logger.debug("Error retrieving Nasdaq earnings for %s: %s", dt, e)
                nasdaq_results[dt] = []

    # Check for earnings matches in Nasdaq results
    for dt in target_dates:
        rows = nasdaq_results.get(dt, [])
        for row in rows:
            sym = row.get("symbol")
            if sym in symbols:
                name = row.get("name") or sym
                time_str = (row.get("time") or "").lower()
                time_label = f" ({time_str})" if time_str else ""
                if dt in week_trading_days:
                    return (
                        True,
                        f"Major tech stock {sym} ({name}) reports earnings this week on {dt}{time_label} "
                        f"(week of {monday} to {friday}). Trading blocked for the entire week.",
                    )
                else:
                    return (
                        True,
                        f"Major tech stock {sym} ({name}) reports earnings on next trading day {dt}{time_label}. "
                        f"Trading blocked on eve of earnings.",
                    )

    # Fallback: yfinance calendar check
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
                            if ed_date in week_trading_days:
                                return (
                                    True,
                                    f"Major tech stock {sym} reports earnings this week on {ed_date} "
                                    f"(week of {monday} to {friday}) (detected via yfinance). "
                                    f"Trading blocked for the entire week.",
                                )
                            elif ed_date == next_td:
                                return (
                                    True,
                                    f"Major tech stock {sym} reports earnings on next trading day {ed_date} "
                                    f"(detected via yfinance). Trading blocked on eve of earnings.",
                                )
            except Exception:
                pass
    except Exception:
        pass

    return False, None


def should_skip_trade(
    today: Optional[datetime.date | datetime.datetime] = None,
    monitored_symbols: Optional[List[str]] = None,
) -> Tuple[bool, Optional[str]]:
    """Master filter: returns (True, reason) if trading should be aborted due to events.

    Strict weekly rule: If any day in the current trading week has a major macro economic
    event (FOMC, CPI, NFP, US Elections, ISM PMI) or Mega-Cap Tech earnings (AAPL, MSFT, NVDA,
    AMZN, GOOGL, GOOG, META, TSLA), trading is blocked for the ENTIRE week.
    """
    d = normalize_date(today)

    # 1. Macro Economic Events Check (FOMC, CPI, NFP, Elections, PMI)
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
        print("✅ No blocking macro events or major tech earnings detected this week. Trading allowed.")

