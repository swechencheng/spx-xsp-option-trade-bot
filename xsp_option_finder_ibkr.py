import math
import datetime
from ib_async import IB, Index, Option

# Import theoretical functions to find the strike efficiently
# instead of paying $0.01 per strike to request market snapshots for the entire chain.
from xsp_option_finder_theory import (
    calculate_bs_delta,
    calculate_bs_price,
    calculate_trading_time_t,
)


class OptionFinder:
    """Finds the best-fit option contract for a given delta target using Market Data.

    Uses IBKR's free reqSecDefOptParams to fetch the option chain structure.
    Selects the strike based on theoretical delta. Once the optimal strike is found,
    it requests a regulatory snapshot (NBBO) for that specific option contract to get
    real-time market bid, ask, and mid prices.

    Caveat: This OptionFinder cannot be used with Paper Account, you will get error:
    "Error 10213, reqId x: API access is restricted on regulatory snapshot for XXX."

    Returns:
        dict with keys: strike, expiry, market_price, bid, ask, theo_price, theo_delta, option_type
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
            dict: { "strike", "expiry", "market_price", "bid", "ask", "theo_price", "theo_delta", "theo_iv", "option_type" }
        """
        print(
            f"\n--- Finding optimal {ticker_symbol} market option"
            f" ({option_type} | Target Delta: {target_delta_abs} | {dte_target}DTE) ---"
        )

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

        today = datetime.date.today()
        valid_expirations = sorted(
            [
                exp
                for exp in cboe_chain.expirations
                if datetime.datetime.strptime(exp, "%Y%m%d").date() >= today
            ]
        )

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
        )

        target_signed_delta = (
            target_delta_abs if option_type.upper() == "C" else -target_delta_abs
        )

        best_strike = None
        min_delta_error = float("inf")
        best_theo_delta = 0.0

        for strike in sorted(cboe_chain.strikes):
            if abs(strike - underlying_spot) > (underlying_spot * 0.15):
                continue
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

        theo_price = calculate_bs_price(
            underlying_spot, best_strike, T, risk_free_rate, iv, option_type
        )

        # Build the Option contract to fetch market data
        opt_contract = Option(
            symbol=ticker_symbol,
            lastTradeDateOrContractMonth=selected_expiry,
            strike=best_strike,
            right=option_type,
            exchange="CBOE",
            currency="USD",
            tradingClass=trading_class,
        )
        self.ib.qualifyContracts(opt_contract)

        print(f"  Requesting regulatory snapshot for {opt_contract.localSymbol}...")

        # Request regulatory snapshot (costs $0.01 per request, provides NBBO)
        ticker = self.ib.reqMktData(
            opt_contract, genericTickList="", snapshot=False, regulatorySnapshot=True
        )

        # Wait until bid/ask data is available (can take a few seconds)
        timeout = 10.0
        elapsed = 0.0
        while (math.isnan(ticker.bid) or math.isnan(ticker.ask)) and elapsed < timeout:
            self.ib.sleep(0.1)
            elapsed += 0.1

        # Fallbacks for missing data
        bid = ticker.bid if not math.isnan(ticker.bid) else 0.0
        ask = ticker.ask if not math.isnan(ticker.ask) else 0.0

        # Calculate mid-price or fallback
        if bid > 0 and ask > 0:
            market_price = (bid + ask) / 2.0
        elif bid > 0:
            market_price = bid
        elif ask > 0:
            market_price = ask
        else:
            market_price = (
                ticker.markPrice if not math.isnan(ticker.markPrice) else theo_price
            )

        print(f"  Model Theoretical Price: {theo_price:.2f}")
        print(
            f"  Snapshot Market Price: {market_price:.2f} (Bid: {bid:.2f}, Ask: {ask:.2f})"
        )

        # TODO fix the compatibility issue to switch between theo_price and market_price.
        # So that downstream code like BaseCreditSpreadTrader works out-of-the-box using the market price.
        return {
            "strike": best_strike,
            "expiry": selected_expiry,
            "market_price": market_price,
            "bid": bid,
            "ask": ask,
            "theo_price": theo_price,
            "theo_delta": best_theo_delta,
            "theo_iv": iv,
            "option_type": option_type,
        }

    def fetch_market_price(
        self,
        ticker_symbol: str,
        trading_class: str,
        expiry: str,
        strike: float,
        option_type: str,
        theo_price_fallback: float = 0.0,
    ) -> dict:
        """Fetch NBBO market price for a specific option contract via regulatory snapshot.

        Args:
            ticker_symbol:       Underlying ticker (e.g., "XSP")
            expiry:              Expiration date string (YYYYMMDD)
            strike:              Option strike price
            option_type:         "P" for Put, "C" for Call
            theo_price_fallback: Fallback price if market data is unavailable

        Returns:
            dict: { "market_price", "bid", "ask" }
        """
        opt_contract = Option(
            symbol=ticker_symbol,
            lastTradeDateOrContractMonth=expiry,
            strike=strike,
            right=option_type,
            exchange="CBOE",
            currency="USD",
            tradingClass=trading_class,
        )
        self.ib.qualifyContracts(opt_contract)

        print(f"  Requesting regulatory snapshot for {opt_contract.localSymbol}...")

        ticker = self.ib.reqMktData(
            opt_contract, genericTickList="", snapshot=False, regulatorySnapshot=True
        )

        timeout = 10.0
        elapsed = 0.0
        while (math.isnan(ticker.bid) or math.isnan(ticker.ask)) and elapsed < timeout:
            self.ib.sleep(0.1)
            elapsed += 0.1

        bid = ticker.bid if not math.isnan(ticker.bid) else 0.0
        ask = ticker.ask if not math.isnan(ticker.ask) else 0.0

        if bid > 0 and ask > 0:
            market_price = (bid + ask) / 2.0
        elif bid > 0:
            market_price = bid
        elif ask > 0:
            market_price = ask
        else:
            market_price = (
                ticker.markPrice
                if not math.isnan(ticker.markPrice)
                else theo_price_fallback
            )

        print(
            f"  Snapshot Market Price: {market_price:.2f} (Bid: {bid:.2f}, Ask: {ask:.2f})"
        )

        return {"market_price": market_price, "bid": bid, "ask": ask}
