import math
import datetime
from ib_async import IB, Index, Option

from option_finder_theory import calculate_trading_time_t


class OptionFinder:
    """Finds the best-fit option contract for a given delta target using Market Data.

    Uses IBKR's reqTickers to fetch real-time market Greeks and prices
    without incurring the $0.01 regulatory snapshot fee.

    Returns:
        dict with keys: strike, expiry, market_price, bid, ask, theo_price, theo_delta, theo_iv, option_type
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
            iv_provider:      (Unused, kept for signature compatibility)
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

        target_chain = None
        for c in chains:
            if c.exchange in ["SMART", "CBOE"] and (
                not trading_class or c.tradingClass == trading_class
            ):
                target_chain = c
                if c.exchange == "SMART":
                    break

        if not target_chain:
            raise Exception(
                f"No option chain found for {ticker_symbol} with trading class {trading_class}."
            )

        today = datetime.date.today()
        valid_expirations = sorted(
            [
                exp
                for exp in target_chain.expirations
                if datetime.datetime.strptime(exp, "%Y%m%d").date() >= today
            ]
        )

        target_idx = min(dte_target, len(valid_expirations) - 1)
        selected_expiry = valid_expirations[target_idx]

        T = calculate_trading_time_t(selected_expiry)
        print(f"Locked expiration: {selected_expiry} (T={T:.4f} trading years)")

        if underlying_spot and not math.isnan(underlying_spot) and underlying_spot > 0:
            filtered_strikes = [
                s
                for s in target_chain.strikes
                if underlying_spot - 120 <= s <= underlying_spot + 120
            ]
        else:
            mid_idx = len(target_chain.strikes) // 2
            filtered_strikes = target_chain.strikes[
                max(0, mid_idx - 50) : min(len(target_chain.strikes), mid_idx + 50)
            ]

        contracts = []
        for s in filtered_strikes:
            contracts.append(
                Option(
                    ticker_symbol,
                    selected_expiry,
                    s,
                    option_type,
                    "SMART",
                    tradingClass=trading_class,
                )
            )

        self.ib.qualifyContracts(*contracts)
        valid_contracts = [c for c in contracts if c.conId != 0]

        self.ib.reqMarketDataType(
            4
        )  # Fallback to delayed-frozen if market is closed or not subscribed
        tickers = self.ib.reqTickers(*valid_contracts)

        print(f"Waiting for option Greeks to populate ({len(tickers)} contracts)...")
        timeout = 30.0
        elapsed = 0.0
        while elapsed < timeout:
            populated = sum(
                1
                for t in tickers
                if t.modelGreeks
                and t.modelGreeks.delta is not None
                and not math.isnan(t.modelGreeks.delta)
            )
            if populated >= len(tickers):
                break
            self.ib.sleep(0.1)
            elapsed += 0.1

        # Log population stats for diagnostics
        populated_count = sum(
            1
            for t in tickers
            if t.modelGreeks
            and t.modelGreeks.delta is not None
            and not math.isnan(t.modelGreeks.delta)
        )
        unpopulated = [
            t.contract.strike
            for t in tickers
            if not t.modelGreeks
            or t.modelGreeks.delta is None
            or math.isnan(t.modelGreeks.delta)
        ]
        print(f"  Greeks populated: {populated_count}/{len(tickers)}")
        if unpopulated:
            print(f"  ⚠️  Missing Greeks for strikes: {unpopulated}")

        # Abort if too many contracts failed to populate — the strike
        # selection would be unreliable and could pick a dangerous delta.
        min_population_rate = 0.95
        if len(tickers) > 0 and populated_count / len(tickers) < min_population_rate:
            raise Exception(
                f"Greeks population too low: {populated_count}/{len(tickers)} "
                f"({populated_count / len(tickers) * 100:.0f}% < {min_population_rate * 100:.0f}% minimum). "
                f"Aborting to avoid unreliable strike selection. "
                f"Check your IBKR market data subscriptions."
            )

        target_signed_delta = (
            target_delta_abs if option_type.upper() == "C" else -target_delta_abs
        )

        best_strike = None
        min_delta_error = float("inf")
        best_ticker = None

        for t in tickers:
            if not t.modelGreeks or t.modelGreeks.delta is None:
                continue
            delta = t.modelGreeks.delta
            if math.isnan(delta):
                continue

            error = abs(delta - target_signed_delta)

            if error < min_delta_error:
                min_delta_error = error
                best_strike = t.contract.strike
                best_ticker = t

        if not best_ticker:
            raise Exception("Failed to find any option with populated Greeks.")

        print(
            f"Best strike found: [{best_strike}]"
            f" (Market Delta: {best_ticker.modelGreeks.delta:.4f} vs Target: {target_signed_delta})"
        )

        bid = best_ticker.bid if not math.isnan(best_ticker.bid) else 0.0
        ask = best_ticker.ask if not math.isnan(best_ticker.ask) else 0.0

        if bid > 0 and ask > 0:
            market_price = (bid + ask) / 2.0
        elif bid > 0:
            market_price = bid
        elif ask > 0:
            market_price = ask
        else:
            market_price = (
                best_ticker.markPrice
                if not math.isnan(best_ticker.markPrice)
                else best_ticker.modelGreeks.optPrice
            )

        theo_price = (
            best_ticker.modelGreeks.optPrice
            if best_ticker.modelGreeks
            and not math.isnan(best_ticker.modelGreeks.optPrice)
            else market_price
        )
        theo_iv = (
            best_ticker.modelGreeks.impliedVol
            if best_ticker.modelGreeks
            and not math.isnan(best_ticker.modelGreeks.impliedVol)
            else 0.15
        )

        print(f"  Model Theoretical Price: {theo_price:.2f}")
        print(
            f"  Snapshot Market Price: {market_price:.2f} (Bid: {bid:.2f}, Ask: {ask:.2f})"
        )

        return {
            "strike": best_strike,
            "expiry": selected_expiry,
            "market_price": market_price,
            "bid": bid,
            "ask": ask,
            "theo_price": theo_price,
            "theo_delta": best_ticker.modelGreeks.delta,
            "theo_iv": theo_iv,
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
        """Fetch NBBO market price for a specific option contract using standard market data.

        Args:
            ticker_symbol:       Underlying ticker (e.g., "XSP")
            trading_class:       Trading class (e.g., "SPXW")
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
            exchange="SMART",
            currency="USD",
            tradingClass=trading_class,
        )
        self.ib.qualifyContracts(opt_contract)

        print(f"  Requesting market data for {opt_contract.localSymbol}...")

        self.ib.reqMarketDataType(4)
        tickers = self.ib.reqTickers(opt_contract)
        ticker = tickers[0]

        timeout = 5.0
        elapsed = 0.0
        while (
            math.isnan(ticker.bid)
            or math.isnan(ticker.ask)
            or not ticker.modelGreeks
            or ticker.modelGreeks.delta is None
            or math.isnan(ticker.modelGreeks.delta)
        ) and elapsed < timeout:
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

        market_delta = None
        market_iv = None
        if ticker.modelGreeks:
            if ticker.modelGreeks.delta is not None and not math.isnan(
                ticker.modelGreeks.delta
            ):
                market_delta = ticker.modelGreeks.delta
            if ticker.modelGreeks.impliedVol is not None and not math.isnan(
                ticker.modelGreeks.impliedVol
            ):
                market_iv = ticker.modelGreeks.impliedVol

        print(
            f"  Snapshot Market Price: {market_price:.2f} (Bid: {bid:.2f}, Ask: {ask:.2f})"
        )

        return {
            "market_price": market_price,
            "bid": bid,
            "ask": ask,
            "market_delta": market_delta,
            "market_iv": market_iv,
        }
