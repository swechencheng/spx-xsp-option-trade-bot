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

        # Determine anchor offset based on underlying ticker
        if ticker_symbol.upper() == "SPX":
            anchor_offset = 50.0
        elif ticker_symbol.upper() == "XSP":
            anchor_offset = 5.0
        else:
            anchor_offset = (
                50.0 if (underlying_spot and underlying_spot > 1000) else 5.0
            )

        spot = underlying_spot
        if not spot or math.isnan(spot) or spot <= 0:
            sorted_all = sorted(target_chain.strikes)
            spot = sorted_all[len(sorted_all) // 2]

        is_put = option_type.upper() == "P"
        if is_put:
            start_strike = spot - anchor_offset
            # Downward list: strikes <= start_strike, sorted descending (closest to start_strike first)
            candidate_down = sorted(
                [s for s in target_chain.strikes if s <= start_strike],
                reverse=True,
            )
            # Upward list: strikes > start_strike, sorted ascending (closest to start_strike first)
            candidate_up = sorted([s for s in target_chain.strikes if s > start_strike])
            current_direction = "down"
        else:
            start_strike = spot + anchor_offset
            # Upward list: strikes >= start_strike, sorted ascending (closest to start_strike first)
            candidate_up = sorted(
                [s for s in target_chain.strikes if s >= start_strike]
            )
            # Downward list: strikes < start_strike, sorted descending (closest to start_strike first)
            candidate_down = sorted(
                [s for s in target_chain.strikes if s < start_strike],
                reverse=True,
            )
            current_direction = "up"

        print(
            f"Anchor strike: {start_strike:.2f} (Spot: {spot:.2f}, Offset: ±{anchor_offset:.1f}) | "
            f"Initial search direction: {current_direction.upper()}"
        )

        page_size = 25
        cursor_down = 0
        cursor_up = 0

        def get_next_batch(direction: str, count: int = page_size):
            nonlocal cursor_down, cursor_up
            valid_batch = []
            if direction == "down":
                while len(valid_batch) < count and cursor_down < len(candidate_down):
                    chunk_size = count - len(valid_batch)
                    chunk_strikes = candidate_down[
                        cursor_down : cursor_down + chunk_size
                    ]
                    cursor_down += len(chunk_strikes)
                    chunk_contracts = [
                        Option(
                            ticker_symbol,
                            selected_expiry,
                            s,
                            option_type,
                            "SMART",
                            tradingClass=trading_class,
                        )
                        for s in chunk_strikes
                    ]
                    self.ib.qualifyContracts(*chunk_contracts)
                    valid_batch.extend([c for c in chunk_contracts if c.conId != 0])
            else:
                while len(valid_batch) < count and cursor_up < len(candidate_up):
                    chunk_size = count - len(valid_batch)
                    chunk_strikes = candidate_up[cursor_up : cursor_up + chunk_size]
                    cursor_up += len(chunk_strikes)
                    chunk_contracts = [
                        Option(
                            ticker_symbol,
                            selected_expiry,
                            s,
                            option_type,
                            "SMART",
                            tradingClass=trading_class,
                        )
                        for s in chunk_strikes
                    ]
                    self.ib.qualifyContracts(*chunk_contracts)
                    valid_batch.extend([c for c in chunk_contracts if c.conId != 0])
            return valid_batch

        evaluated_tickers = {}
        max_pages = 10
        page_num = 0

        while page_num < max_pages:
            page_num += 1
            batch = get_next_batch(current_direction, page_size)
            if not batch:
                print(
                    f"No more strikes available in {current_direction.upper()} direction."
                )
                break

            print(
                f"\n[Page {page_num}] Fetching {len(batch)} contracts in {current_direction.upper()} direction "
                f"(strikes: {min(c.strike for c in batch):.1f} - {max(c.strike for c in batch):.1f})..."
            )
            page_tickers = self._fetch_page_greeks(batch)
            evaluated_tickers.update(page_tickers)

            all_deltas = {
                s: abs(t.modelGreeks.delta)
                for s, t in evaluated_tickers.items()
                if t.modelGreeks
                and t.modelGreeks.delta is not None
                and not math.isnan(t.modelGreeks.delta)
            }

            if not all_deltas:
                raise Exception("No valid Greeks returned for fetched batch.")

            min_d = min(all_deltas.values())
            max_d = max(all_deltas.values())

            print(
                f"  Evaluated delta range so far: [{min_d:.4f}, {max_d:.4f}] "
                f"(Target: {target_delta_abs:.4f})"
            )

            # Check if target delta is within range
            if min_d <= target_delta_abs <= max_d:
                print(
                    f"  🎯 Target delta {target_delta_abs:.4f} is within range [{min_d:.4f}, {max_d:.4f}]!"
                )
                break

            # Decide next direction
            if target_delta_abs < min_d:
                # Target delta is smaller than all evaluated contracts (need further OTM)
                next_direction = "down" if is_put else "up"
                print(
                    f"  Target delta {target_delta_abs:.4f} < min evaluated {min_d:.4f}. "
                    f"Fetching next {page_size} contracts {next_direction.upper()}..."
                )
            else:
                # Target delta is larger than all evaluated contracts (need closer to ATM)
                next_direction = "up" if is_put else "down"
                print(
                    f"  Target delta {target_delta_abs:.4f} > max evaluated {max_d:.4f}. "
                    f"Fetching next {page_size} contracts {next_direction.upper()}..."
                )

            # Check if we can continue in next_direction
            if next_direction == "down" and cursor_down >= len(candidate_down):
                print("  No more downward strikes available. Stopping search.")
                break
            elif next_direction == "up" and cursor_up >= len(candidate_up):
                print("  No more upward strikes available. Stopping search.")
                break

            current_direction = next_direction

        target_signed_delta = (
            target_delta_abs if option_type.upper() == "C" else -target_delta_abs
        )

        best_strike = None
        min_delta_error = float("inf")
        best_ticker = None

        for strike, t in evaluated_tickers.items():
            if not t.modelGreeks or t.modelGreeks.delta is None:
                continue
            delta = t.modelGreeks.delta
            if math.isnan(delta):
                continue

            error = abs(delta - target_signed_delta)

            if error < min_delta_error:
                min_delta_error = error
                best_strike = strike
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

    def _fetch_page_greeks(self, valid_contracts: list) -> dict:
        max_greeks_attempts = 6
        min_population_rate = 1.0
        tickers = []

        for greeks_attempt in range(1, max_greeks_attempts + 1):
            self.ib.reqMarketDataType(4)
            tickers = self.ib.reqTickers(*valid_contracts)

            print(
                f"Waiting for option Greeks to populate ({len(tickers)} contracts)... "
                f"[attempt {greeks_attempt}/{max_greeks_attempts}]"
            )
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

            if len(tickers) > 0 and populated_count == len(tickers):
                for c in valid_contracts:
                    self.ib.cancelMktData(c)
                break

            print(
                f"  ⚠️  Greeks population incomplete "
                f"({populated_count}/{len(tickers)} < 100%). "
                f"Cancelling and retrying..."
            )
            for c in valid_contracts:
                self.ib.cancelMktData(c)
            self.ib.sleep(2)
        else:
            raise Exception(
                f"Greeks population failed to reach 100% after {max_greeks_attempts} attempts: "
                f"{populated_count}/{len(tickers)} populated. "
                f"Aborting to avoid unreliable strike selection. "
                f"Check your IBKR market data subscriptions."
            )

        return {t.contract.strike: t for t in tickers}

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

        self.ib.cancelMktData(opt_contract)

        return {
            "market_price": market_price,
            "bid": bid,
            "ask": ask,
            "market_delta": market_delta,
            "market_iv": market_iv,
        }
