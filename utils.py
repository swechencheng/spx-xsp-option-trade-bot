"""Utility functions for option pricing.

Provides theoretical price and delta calculations, plus trading-time T computation
used for initial credit estimation in the walk-the-book algorithm.
"""

import math
import datetime
import pytz
from scipy.stats import norm


def calculate_trading_time_t(exp_date_str: str) -> float:
    """Calculate T for Black-Scholes using exact trading minutes remaining (US/Eastern)."""
    eastern = pytz.timezone("US/Eastern")
    now_utc = datetime.datetime.now(pytz.utc)
    now_local = now_utc.astimezone(eastern)

    exp_date = datetime.datetime.strptime(exp_date_str, "%Y%m%d").date()

    # If already expired
    if exp_date < now_local.date():
        return 1.0 / (252 * 6.75 * 60.0)  # Prevent div by zero

    def get_trading_minutes_left_today(dt: datetime.datetime) -> float:
        market_open = dt.replace(hour=9, minute=30, second=0, microsecond=0)
        market_close = dt.replace(hour=16, minute=15, second=0, microsecond=0)

        if dt < market_open:
            return 6.75 * 60.0
        elif dt >= market_close:
            return 0.0
        else:
            diff = market_close - dt
            return diff.total_seconds() / 60.0

    total_minutes = 0.0
    current_date = now_local.date()

    if current_date.weekday() < 5:
        total_minutes += get_trading_minutes_left_today(now_local)

    current_date += datetime.timedelta(days=1)
    while current_date <= exp_date:
        if current_date.weekday() < 5:
            total_minutes += 6.75 * 60.0
        current_date += datetime.timedelta(days=1)

    total_minutes = max(total_minutes, 1.0)
    return total_minutes / (252 * 6.75 * 60.0)


def calculate_bs_price(S, K, T, r, sigma, option_type="P"):
    """Calculate theoretical contract price using local Black-Scholes model."""
    if T <= 0 or sigma <= 0:
        return max(0.0, S - K) if option_type.upper() == "C" else max(0.0, K - S)

    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)

    if option_type.upper() == "C":
        price = S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    else:
        price = K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)

    return price


def calculate_bs_delta(S, K, T, r, sigma, option_type="P"):
    """Calculate theoretical contract delta using local Black-Scholes model."""
    if T <= 0 or sigma <= 0:
        return 0.0

    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    if option_type.upper() == "C":
        return norm.cdf(d1)
    else:
        return norm.cdf(d1) - 1.0
