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


# Event completion times (Eastern Time)
# CPI: 08:30 ET release
# NFP: 08:30 ET release
# PMI: 10:00 ET release
# FOMC: 14:00 ET rate decision + 14:30 ET press conference (completed by 15:00 ET)
# Elections: results continue overnight (not completed during trading hours)
EVENT_COMPLETION_TIMES: Dict[str, datetime.time] = {
    "cpi": datetime.time(8, 30),
    "nfp": datetime.time(8, 30),
    "non-farm": datetime.time(8, 30),
    "pmi": datetime.time(10, 0),
    "fomc": datetime.time(15, 0),
    "election": datetime.time(23, 59),
    "presidential": datetime.time(23, 59),
    "midterm": datetime.time(23, 59),
}


def get_event_completion_time(event_desc: str) -> datetime.time:
    """Determine standard completion time for a given macro event in Eastern Time."""
    desc_lower = event_desc.lower()
    for key, t in EVENT_COMPLETION_TIMES.items():
        if key in desc_lower:
            return t
    return datetime.time(16, 0)


def is_event_completed(
    event_desc: str,
    event_date: datetime.date,
    current_dt: datetime.datetime,
) -> bool:
    """Check if an event scheduled for event_date has already occurred relative to current_dt."""
    cur_d = current_dt.date()
    if cur_d > event_date:
        return True
    if cur_d < event_date:
        return False
    # Same date: check if current time is past event completion time
    comp_time = get_event_completion_time(event_desc)
    return current_dt.time() >= comp_time


def get_next_trading_day(d: datetime.date) -> datetime.date:
    """Get next weekday date (skips Saturday/Sunday)."""
    next_day = d + datetime.timedelta(days=1)
    while next_day.weekday() >= 5:  # 5=Sat, 6=Sun
        next_day += datetime.timedelta(days=1)
    return next_day


def check_macro_event_filter(
    current_dt: Optional[datetime.datetime | datetime.date] = None,
    current_time: Optional[datetime.time] = None,
) -> Tuple[bool, Optional[str]]:
    """Check if macro events block trading based on occurrence and holding window timing.

    Rules:
    1. If a macro event is scheduled for TODAY:
       - If it has ALREADY occurred / completed (e.g. CPI/NFP released at 8:30 AM, PMI at 10:00 AM, FOMC completed by 15:00 ET):
         It does NOT block trading by itself!
       - If it is PENDING (has not occurred yet today, e.g. checked at 13:00 ET before 14:00 FOMC, or Election Day results pending tonight):
         Trading is BLOCKED.
    2. If a macro event is scheduled for TOMORROW / next trading day (eve of event):
       - Since 1DTE positions hold overnight until tomorrow close, any event tomorrow falls into the holding period:
         Trading is BLOCKED.
    """
    if current_dt is None:
        dt = datetime.datetime.now(EASTERN_TZ)
    elif isinstance(current_dt, datetime.datetime):
        if current_dt.tzinfo is None:
            dt = EASTERN_TZ.localize(current_dt)
        else:
            dt = current_dt.astimezone(EASTERN_TZ)
    elif isinstance(current_dt, datetime.date):
        t = current_time or datetime.time(15, 52)
        dt = EASTERN_TZ.localize(datetime.datetime.combine(current_dt, t))
    else:
        dt = datetime.datetime.now(EASTERN_TZ)

    today = dt.date()
    events = get_all_macro_events()

    # 1. Check if an event scheduled for TODAY is still pending
    if today in events:
        event_name = events[today]
        if not is_event_completed(event_name, today, dt):
            comp_time = get_event_completion_time(event_name)
            return (
                True,
                f"Macro event TODAY has NOT completed yet ({event_name}, completion at ~{comp_time.strftime('%H:%M')} ET). "
                "Trading blocked.",
            )
        else:
            logger.info(
                "Macro event TODAY (%s) has already occurred / concluded by %s ET. "
                "Proceeding to check subsequent events.",
                event_name,
                dt.strftime("%H:%M"),
            )

    # 2. Check if next trading day has a macro event (Eve of event)
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
    current_dt: Optional[datetime.datetime | datetime.date] = None,
    monitored_symbols: Optional[List[str]] = None,
    current_time: Optional[datetime.time] = None,
) -> Tuple[bool, Optional[str]]:
    """Check if major tech stocks have pending earnings (today after-hours or tomorrow pre-market/intraday).

    Already completed earnings (e.g. today pre-market BMO, when checked after market open) do NOT block trading.
    """
    if current_dt is None:
        dt = datetime.datetime.now(EASTERN_TZ)
    elif isinstance(current_dt, datetime.datetime):
        if current_dt.tzinfo is None:
            dt = EASTERN_TZ.localize(current_dt)
        else:
            dt = current_dt.astimezone(EASTERN_TZ)
    elif isinstance(current_dt, datetime.date):
        t = current_time or datetime.time(15, 52)
        dt = EASTERN_TZ.localize(datetime.datetime.combine(current_dt, t))
    else:
        dt = datetime.datetime.now(EASTERN_TZ)

    today = dt.date()
    cur_time = dt.time()
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

    # 1. Check Tomorrow / Next Trading Day Earnings (pre-market, intraday, or after-hours)
    rows_next = query_nasdaq(next_td)
    for row in rows_next:
        sym = row.get("symbol")
        time_str = (row.get("time") or "").lower()
        if sym in symbols:
            name = row.get("name") or sym
            return (
                True,
                f"Major tech stock {sym} ({name}) reports earnings TOMORROW ({next_td}, {time_str or 'time unconfirmed'}). "
                "Trading blocked.",
            )

    # 2. Check Today Earnings
    rows_today = query_nasdaq(today)
    for row in rows_today:
        sym = row.get("symbol")
        time_str = (row.get("time") or "").lower()
        if sym in symbols:
            # If after-hours (AMC), it has NOT happened yet at 15:52 ET!
            if "after-hours" in time_str:
                name = row.get("name") or sym
                return (
                    True,
                    f"Major tech stock {sym} ({name}) reports earnings TODAY after-hours ({today}). "
                    "Trading blocked.",
                )
            # If pre-market (BMO), check if already occurred
            elif "pre-market" in time_str:
                # If before 09:30 AM, market has not opened to absorb it yet
                if cur_time < datetime.time(9, 30):
                    name = row.get("name") or sym
                    return (
                        True,
                        f"Major tech stock {sym} ({name}) reports earnings TODAY pre-market (pending market open). "
                        "Trading blocked.",
                    )
                else:
                    logger.info(
                        "Major tech stock %s (%s) reported earnings TODAY pre-market (already absorbed by market).",
                        sym,
                        row.get("name") or sym,
                    )
            elif not time_str or "time-not-supplied" in time_str:
                # Unknown time today: conservative block
                name = row.get("name") or sym
                return (
                    True,
                    f"Major tech stock {sym} ({name}) reports earnings TODAY (unconfirmed time). "
                    "Trading blocked.",
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
                            if ed_date == next_td:
                                return (
                                    True,
                                    f"Major tech stock {sym} reports earnings TOMORROW on {ed_date} "
                                    "(detected via yfinance). Trading blocked.",
                                )
                            elif ed_date == today:
                                bmo_confirmed = any(
                                    r.get("symbol") == sym and "pre-market" in (r.get("time") or "").lower()
                                    for r in rows_today
                                )
                                if not bmo_confirmed or cur_time < datetime.time(9, 30):
                                    return (
                                        True,
                                        f"Major tech stock {sym} reports earnings TODAY on {ed_date} "
                                        "(detected via yfinance). Trading blocked.",
                                    )
            except Exception:
                pass
    except Exception:
        pass

    return False, None


def should_skip_trade(
    current_dt: Optional[datetime.datetime | datetime.date] = None,
    monitored_symbols: Optional[List[str]] = None,
    current_time: Optional[datetime.time] = None,
) -> Tuple[bool, Optional[str]]:
    """Master filter: returns (True, reason) if trading should be aborted due to events.

    If an event scheduled for today has already occurred (e.g. CPI/NFP at 8:30 AM, PMI at 10:00 AM, FOMC by 15:00 ET),
    and there are no other events today after-hours, tomorrow pre-market, or tomorrow intraday,
    trading is allowed (returns (False, None)).
    """
    # 1. Macro Economic Events Check
    skip_macro, reason_macro = check_macro_event_filter(current_dt, current_time)
    if skip_macro:
        return True, reason_macro

    # 2. Major Tech Earnings Check
    skip_tech, reason_tech = check_tech_earnings_filter(current_dt, monitored_symbols, current_time)
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

