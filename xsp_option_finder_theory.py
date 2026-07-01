import math
import datetime
import warnings
from scipy.stats import norm
from ib_async import IB, Index

# Suppress yfinance timezone warnings to keep console clean
warnings.filterwarnings("ignore", category=FutureWarning)


# ==================== Option Calculation Core ====================
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
        xsp_spot: float,
        iv: float,
        risk_free_rate: float,
        option_type: str,
        target_delta_abs: float,
        dte_target: int,
    ) -> dict:
        """Find the option strike closest to target_delta_abs for the nearest DTE.

        Args:
            ticker_symbol:    Underlying ticker (e.g., "XSP")
            xsp_spot:         Current spot price of XSP
            iv:               Implied volatility (decimal, e.g., 0.16 for 16%)
            risk_free_rate:   Annualized risk-free rate (decimal)
            option_type:      "P" for Put, "C" for Call
            target_delta_abs: Absolute value of target delta (e.g., 0.20)
            dte_target:       Target days-to-expiration

        Returns:
            dict: { "strike", "expiry", "theo_price", "theo_delta", "option_type" }
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
        cboe_chain = next(c for c in chains if c.exchange == "CBOE")

        # Lock target expiration date and calculate annualized time to expiry (T)
        target_date = (
            datetime.datetime.now() + datetime.timedelta(days=dte_target)
        ).date()
        selected_expiry = min(
            cboe_chain.expirations,
            key=lambda x: abs(
                datetime.datetime.strptime(x, "%Y%m%d").date() - target_date
            ),
        )

        exp_date_obj = datetime.datetime.strptime(selected_expiry, "%Y%m%d").date()
        days_left = (exp_date_obj - datetime.date.today()).days
        T = max(days_left, 1) / 365.0
        print(
            f"Locked expiration: {selected_expiry} ({days_left} days from now, T={T:.4f})"
        )

        # Signed target delta: negative for puts, positive for calls
        target_signed_delta = (
            target_delta_abs if option_type.upper() == "C" else -target_delta_abs
        )

        best_strike = None
        min_delta_error = float("inf")
        best_theo_delta = 0.0

        for strike in sorted(cboe_chain.strikes):
            # Narrow search range: skip deep OTM/ITM to save compute
            if abs(strike - xsp_spot) > (xsp_spot * 0.15):
                continue
            # Skip non-integer strikes (reqSecDefOptParams returns union of all expirations;
            # half-point strikes may not exist for the selected date)
            if strike % 1 != 0:
                continue

            calc_delta = calculate_bs_delta(
                xsp_spot, strike, T, risk_free_rate, iv, option_type
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

        theo_price = calculate_bs_price(
            xsp_spot, best_strike, T, risk_free_rate, iv, option_type
        )
        print(f"  Model Theoretical Price: {theo_price:.2f}")

        return {
            "strike": best_strike,
            "expiry": selected_expiry,
            "theo_price": theo_price,
            "theo_delta": best_theo_delta,
            "option_type": option_type,
        }
