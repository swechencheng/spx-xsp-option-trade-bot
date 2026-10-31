"""Utility functions for option pricing.

Provides theoretical price and delta calculations, implied volatility solver,
trading-time T computation, and New York timezone timestamps.
"""

import datetime
import math
from typing import Optional
import pytz
from scipy.optimize import brentq
from scipy.stats import norm

EASTERN_TZ = pytz.timezone("America/New_York")


def get_ny_now() -> datetime.datetime:
    """Return current datetime in America/New_York."""
    return datetime.datetime.now(EASTERN_TZ)


def get_ny_timestamp() -> str:
    """Return formatted timestamp in America/New_York (e.g. '[2026-10-31 15:52:00 EDT]')."""
    return get_ny_now().strftime("[%Y-%m-%d %H:%M:%S %Z]")


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


def calculate_bs_price(S, K, T, r, sigma, option_type="P", q=0.0):
    """Calculate theoretical contract price using local Black-Scholes model with continuous dividend yield."""
    if T <= 0 or sigma <= 0:
        return max(0.0, S - K) if option_type.upper() == "C" else max(0.0, K - S)

    d1 = (math.log(S / K) + (r - q + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)

    if option_type.upper() == "C":
        price = S * math.exp(-q * T) * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(
            d2
        )
    else:
        price = K * math.exp(-r * T) * norm.cdf(-d2) - S * math.exp(-q * T) * norm.cdf(
            -d1
        )

    return price


def calculate_bs_delta(S, K, T, r, sigma, option_type="P", q=0.0):
    """Calculate theoretical contract delta using local Black-Scholes model with continuous dividend yield."""
    if T <= 0 or sigma <= 0:
        return 0.0

    d1 = (math.log(S / K) + (r - q + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    if option_type.upper() == "C":
        return math.exp(-q * T) * norm.cdf(d1)
    else:
        return math.exp(-q * T) * (norm.cdf(d1) - 1.0)


def calculate_implied_vol(
    S: float,
    K: float,
    T: float,
    r: float,
    price: float,
    option_type: str = "P",
    q: float = 0.0,
) -> Optional[float]:
    """Calculate Black-Scholes implied volatility from market option price."""
    if T <= 0 or price <= 0:
        return None

    # Check against theoretical intrinsic lower bound with discount factor
    intrinsic = (
        max(0.0, K * math.exp(-r * T) - S * math.exp(-q * T))
        if option_type.upper() == "P"
        else max(0.0, S * math.exp(-q * T) - K * math.exp(-r * T))
    )
    if price < intrinsic:
        return None

    try:
        def obj(vol):
            return calculate_bs_price(S, K, T, r, vol, option_type, q=q) - price

        f_low = obj(0.001)
        f_high = obj(5.0)
        if f_low * f_high > 0:
            return None

        return float(brentq(obj, 0.001, 5.0, xtol=1e-5))
    except Exception:
        return None
