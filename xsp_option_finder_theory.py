import math
import datetime
import warnings
from scipy.stats import norm
from ib_async import IB, Index, Option
import pytz

# Suppress yfinance timezone warnings to keep console clean
warnings.filterwarnings("ignore", category=FutureWarning)


# ==================== Option Calculation Core ====================
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


class OptionFinder:
    """Finds the best-fit option contract for a given delta target using Black-Scholes.

    Uses IBKR's free reqSecDefOptParams to fetch the option chain structure (strikes
    and expirations), then applies the local B-S model to select the strike whose
    theoretical delta is closest to the target. No market data subscription required.

    Returns:
        dict with keys: strike, expiry, theo_price, theo_delta, option_type
    """

    def __init__(self, ib_client: IB):
        self.ib = ib_client

    def find_option(
        self,
        ticker_symbol: str,
        trading_class: str,
        underlying_spot: float,
        iv_provider,
        risk_free_rate: float,
        option_type: str,
        target_delta_abs: float,
        dte_target: int,
    ) -> dict:
        """Find the option strike closest to target_delta_abs for the nearest DTE.

        Args:
            ticker_symbol:    Underlying ticker (e.g., "XSP" or "SPX")
            trading_class:    The specific trading class (e.g. "SPXW")
            underlying_spot:  Current spot price of underlying
            iv_provider:      IVProvider instance to fetch sigma
            risk_free_rate:   Annualized risk-free rate (decimal)
            option_type:      "P" for Put, "C" for Call
            target_delta_abs: Absolute value of target delta (e.g., 0.20)
            dte_target:       Target days-to-expiration

        Returns:
            dict: { "strike", "expiry", "theo_price", "theo_delta", "theo_iv", "option_type" }
        """
        print(
            f"\n--- Finding optimal {ticker_symbol} option"
            f" ({option_type} | Target Delta: {target_delta_abs} | {dte_target}DTE) ---"
        )

        # Get option chain structure (free static data, no subscription needed)
        contract = Index(ticker_symbol, "CBOE")
        self.ib.qualifyContracts(contract)
        chains = self.ib.reqSecDefOptParams(
            contract.symbol, "", contract.secType, contract.conId
        )
        cboe_chain = next(
            c
            for c in chains
            if c.exchange == "CBOE"
            and (not trading_class or c.tradingClass == trading_class)
        )

        # Lock target expiration date based on trading days (using actual CBOE expirations)
        today = datetime.date.today()
        # Sort expirations and filter out any that have already passed
        valid_expirations = sorted(
            [
                exp
                for exp in cboe_chain.expirations
                if datetime.datetime.strptime(exp, "%Y%m%d").date() >= today
            ]
        )

        # Select the expiration by index (0 = today, 1 = next trading day, etc.)
        # This inherently skips weekends and market holidays perfectly
        target_idx = min(dte_target, len(valid_expirations) - 1)
        selected_expiry = valid_expirations[target_idx]

        T = calculate_trading_time_t(selected_expiry)

        # Fetch generic ATM IV for the selected expiry
        if ticker_symbol == "SPX":
            atm_strike = round(underlying_spot / 5) * 5
        else:
            atm_strike = round(underlying_spot)
        atm_contract = Option(
            ticker_symbol,
            selected_expiry,
            strike=atm_strike,
            right=option_type,
            exchange="SMART",
            tradingClass=trading_class,
        )
        self.ib.qualifyContracts(atm_contract)

        iv_data = self.ib.run(
            iv_provider.get_iv(self.ib, atm_contract, ticker_symbol, selected_expiry)
        )
        iv = iv_data.get("model_iv") or iv_data.get("bid_iv") or iv_data.get("ask_iv")
        if not iv:
            raise Exception("Failed to fetch IV from IVProvider")

        print(
            f"Locked expiration: {selected_expiry} (T={T:.4f} trading years) | IV: {iv:.4f}"
        )  # Signed target delta: negative for puts, positive for calls
        target_signed_delta = (
            target_delta_abs if option_type.upper() == "C" else -target_delta_abs
        )

        best_strike = None
        min_delta_error = float("inf")
        best_theo_delta = 0.0

        for strike in sorted(cboe_chain.strikes):
            # Narrow search range: skip deep OTM/ITM to save compute
            if abs(strike - underlying_spot) > (underlying_spot * 0.15):
                continue
            # Skip non-integer strikes (reqSecDefOptParams returns union of all expirations;
            # half-point strikes may not exist for the selected date)
            if strike % 1 != 0:
                continue

            calc_delta = calculate_bs_delta(
                underlying_spot, strike, T, risk_free_rate, iv, option_type
            )
            error = abs(calc_delta - target_signed_delta)

            if error < min_delta_error:
                min_delta_error = error
                best_strike = strike
                best_theo_delta = calc_delta

        print(
            f"Best strike found: [{best_strike}]"
            f" (Theo Delta: {best_theo_delta:.4f} vs Target: {target_signed_delta})"
        )

        best_theo_price = calculate_bs_price(
            underlying_spot, best_strike, T, risk_free_rate, iv, option_type
        )
        print(f"  Model Theoretical Price: {best_theo_price:.2f}")

        return {
            "strike": best_strike,
            "expiry": selected_expiry,
            "theo_price": best_theo_price,
            "theo_delta": best_theo_delta,
            "theo_iv": iv,
            "option_type": option_type,
        }
