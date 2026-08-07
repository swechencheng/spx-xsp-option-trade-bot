from ib_async import IB, Option, Contract, ComboLeg, LimitOrder


class BaseCreditSpreadTrader:
    """Base Credit Spread Trading Executor (Walk-the-Book automatic repricing)

    Strategy Structure:
      SELL Delta-targeted Option (Collect Premium) + BUY 1-strike-away Option (Pay Premium)
      Net Effect = Receive Credit (Net Premium) on a 1-wide spread

    Order Management:
      1. Submit BAG combo order with theoretical spread as initial Credit limit price
      2. Check fill status every interval seconds
      3. If not filled, reduce Credit by walk step and modify order
      4. Repeat steps 2-3 until filled, or cancel order if Credit < min credit
    """

    def __init__(
        self,
        ib_client: IB,
        walk_step: float = 0.01,
        walk_interval: int = 10,
        min_credit: float = 0.09,
        quantity: int = 1,
    ):
        self.ib = ib_client
        self.walk_step = walk_step
        self.walk_interval = walk_interval
        self.min_credit = min_credit
        self.quantity = quantity

    def execute_spread(
        self,
        strategy_name: str,
        ticker_symbol: str,
        sell_leg_info: dict,
        buy_leg_info: dict,
        trading_class: str = "",
    ):
        """Build and execute a Credit Spread combo order with walk-the-book repricing.

        Args:
            strategy_name:  Name of the strategy (e.g., "Bull Put Spread")
            ticker_symbol:  Ticker symbol (e.g., "XSP")
            sell_leg_info:  Dict from OptionFinder.find_option() — the sell leg
            buy_leg_info:   Dict from OptionFinder.find_option() — the buy leg

        Returns:
            Filled Trade object on success, or None if cancelled / below min credit.
        """
        print("\n" + "=" * 60)
        print(f"  {strategy_name} Trading Executor")
        print("=" * 60)

        # --- Build and qualify IBKR contracts for both legs ---
        sell_leg = Option(
            ticker_symbol,
            sell_leg_info["expiry"],
            sell_leg_info["strike"],
            sell_leg_info["option_type"],
            "CBOE",
            tradingClass=trading_class,
            currency="USD",
        )
        buy_leg = Option(
            ticker_symbol,
            buy_leg_info["expiry"],
            buy_leg_info["strike"],
            buy_leg_info["option_type"],
            "CBOE",
            tradingClass=trading_class,
            currency="USD",
        )
        self.ib.qualifyContracts(sell_leg, buy_leg)

        print(
            f"  Sell Leg (SELL) : {sell_leg.localSymbol}"
            f"  (Strike={sell_leg_info['strike']}, Δ={sell_leg_info['theo_delta']:.4f})"
        )
        print(
            f"  Buy Leg  (BUY)  : {buy_leg.localSymbol}"
            f"  (Strike={buy_leg_info['strike']}, Δ={buy_leg_info['theo_delta']:.4f})"
        )

        # --- Calculate initial Credit ---
        # Spread Credit = Sell Leg Theo Price - Buy Leg Theo Price
        raw_initial_credit = sell_leg_info["theo_price"] - buy_leg_info["theo_price"]
        # Round initial credit to the nearest walk_step increment
        steps = round(raw_initial_credit / self.walk_step)
        initial_credit = max(self.min_credit, steps * self.walk_step)
        initial_credit = round(initial_credit, 2)

        print(
            f"\n  Theoretical Credit: {initial_credit:.2f} (Raw: {raw_initial_credit:.2f})"
        )

        if initial_credit < self.min_credit:
            print(
                f"  ⚠ Theoretical Credit ({initial_credit:.2f}) is below minimum "
                f"threshold ({self.min_credit:.2f}), abandoning trade."
            )
            return None

        # --- Build BAG combo contract ---
        combo = Contract(
            symbol=ticker_symbol,
            secType="BAG",
            exchange="CBOE",
            currency="USD",
            tradingClass=trading_class,
            comboLegs=[
                ComboLeg(conId=sell_leg.conId, ratio=1, action="SELL", exchange="CBOE"),
                ComboLeg(conId=buy_leg.conId, ratio=1, action="BUY", exchange="CBOE"),
            ],
        )

        return self._walk_the_book(combo, initial_credit)

    def _walk_the_book(self, combo: Contract, initial_credit: float):
        """Submit the combo order and walk the limit price down to ensure fill."""
        # --- Submit initial limit order ---
        # IBKR BAG Combo: For credit spreads, a negative limitPrice means net income received
        current_credit = initial_credit
        limit_price = -current_credit  # Negative = net premium received

        order = LimitOrder(
            action="BUY",  # BAG Combo: BUY action = buy combo
            totalQuantity=self.quantity,
            lmtPrice=limit_price,
            tif="DAY",
        )
        order.transmit = True
        order.overridePercentageConstraints = True

        trade = self.ib.placeOrder(combo, order)
        print(
            f"\n  📤 Order submitted | Credit: {current_credit:.2f}"
            f" (limitPrice={limit_price:.2f})"
        )

        # --- Walk-the-Book Loop ---
        while True:
            self.ib.sleep(self.walk_interval)
            self.ib.sleep(0.1)  # Extra tick to let status propagate

            status = trade.orderStatus.status
            print(f"  ⏱  Order status: {status}")

            if status == "Filled":
                fill_price = trade.orderStatus.avgFillPrice
                print(f"\n  ✅ Order filled! Fill price: {fill_price:.2f}")
                print(f"  Actual Credit received: {abs(fill_price):.2f}")
                return trade

            if status in ("Cancelled", "ApiCancelled", "Inactive"):
                # Auto-healing: Check if we were rejected for being too aggressive (Error 202)
                # IBKR message: "We cannot accept an order at a limit price at or more aggressive than -1.375.
                # Please submit your order using a limit price that is closer to the current market price of -1.7."
                log_msgs = [
                    entry.message
                    for entry in trade.log
                    if getattr(entry, "message", None)
                ]
                if log_msgs:
                    last_msg = log_msgs[-1]
                    if (
                        "more aggressive than" in last_msg
                        and "current market price of" in last_msg
                    ):
                        import re

                        m = re.search(
                            r"current market price of (-?\d+\.?\d*)", last_msg
                        )
                        if m:
                            suggested_market_price = float(m.group(1))
                            new_credit = round(abs(suggested_market_price), 2)

                            if new_credit > current_credit:
                                print(
                                    f"\n  [Auto-Healing] Initial credit {current_credit:.2f} was too aggressive!"
                                )
                                print(
                                    f"  [Auto-Healing] Restarting walk-the-book from {new_credit:.2f} down to {self.min_credit:.2f}"
                                )
                                current_credit = new_credit
                                limit_price = -current_credit

                                order = LimitOrder(
                                    action="BUY",
                                    totalQuantity=self.quantity,
                                    lmtPrice=limit_price,
                                    tif="DAY",
                                )
                                order.transmit = True
                                order.overridePercentageConstraints = True
                                trade = self.ib.placeOrder(combo, order)
                                print(
                                    f"  🔄 Repricing order (Auto-Heal) | New Credit: {current_credit:.2f}"
                                    f" (limitPrice={limit_price:.2f})"
                                )
                                continue

                print(f"\n  ❌ Order cancelled or inactive (Status: {status})")
                return None

            # Not filled -> reduce Credit and reprice
            current_credit = round(current_credit - self.walk_step, 2)

            if current_credit < self.min_credit:
                print(
                    f"  ⛔ Credit ({current_credit:.2f}) dropped below minimum"
                    f" threshold ({self.min_credit:.2f}), cancelling order."
                )
                self.ib.cancelOrder(order)
                self.ib.sleep(1)
                return None

            # IBKR sometimes rejects combo in-place modification with Warning 105
            # "Order being modified does not match original order."
            # We will cancel and replace to be perfectly robust.
            self.ib.cancelOrder(order)
            self.ib.sleep(0.5)

            limit_price = -current_credit

            # Create a completely new order object
            order = LimitOrder(
                action="BUY",
                totalQuantity=self.quantity,
                lmtPrice=limit_price,
                tif="DAY",
            )
            order.transmit = True
            order.overridePercentageConstraints = True

            trade = self.ib.placeOrder(combo, order)
            print(
                f"  🔄 Repricing order (Cancel/Replace) | New Credit: {current_credit:.2f}"
                f" (limitPrice={limit_price:.2f})"
            )


class BullPutSpreadTrader(BaseCreditSpreadTrader):
    """Bull Put Spread Trading Executor"""

    def execute(
        self,
        ticker_symbol: str,
        sell_put_info: dict,
        buy_put_info: dict,
        trading_class: str = "",
    ):
        return self.execute_spread(
            strategy_name="Bull Put Spread",
            ticker_symbol=ticker_symbol,
            sell_leg_info=sell_put_info,
            buy_leg_info=buy_put_info,
            trading_class=trading_class,
        )


class BearCallSpreadTrader(BaseCreditSpreadTrader):
    """Bear Call Spread Trading Executor"""

    def execute(
        self,
        ticker_symbol: str,
        sell_call_info: dict,
        buy_call_info: dict,
        trading_class: str = "",
    ):
        return self.execute_spread(
            strategy_name="Bear Call Spread",
            ticker_symbol=ticker_symbol,
            sell_leg_info=sell_call_info,
            buy_leg_info=buy_call_info,
            trading_class=trading_class,
        )


class IronCondorTrader(BaseCreditSpreadTrader):
    """Iron Condor Trading Executor

    An Iron Condor is a simultaneous Bull Put Spread + Bear Call Spread.
    All 4 legs are submitted as a single BAG combo order.
    """

    def execute(
        self,
        ticker_symbol: str,
        sell_put_info: dict,
        buy_put_info: dict,
        sell_call_info: dict,
        buy_call_info: dict,
        trading_class: str = "",
    ):
        """Build and execute an Iron Condor combo order.

        Args:
            ticker_symbol:   Ticker symbol (e.g., "SPX")
            sell_put_info:   Sell put leg dict (higher put strike)
            buy_put_info:    Buy put leg dict (lower put strike)
            sell_call_info:  Sell call leg dict (lower call strike)
            buy_call_info:   Buy call leg dict (higher call strike)
            trading_class:   IBKR trading class (e.g., "SPXW")

        Returns:
            Filled Trade object on success, or None if cancelled / below min credit.
        """
        print("\n" + "=" * 60)
        print("  Iron Condor Trading Executor")
        print("=" * 60)

        # --- Build and qualify all 4 legs ---
        sell_put = Option(
            ticker_symbol,
            sell_put_info["expiry"],
            sell_put_info["strike"],
            "P",
            "CBOE",
            tradingClass=trading_class,
            currency="USD",
        )
        buy_put = Option(
            ticker_symbol,
            buy_put_info["expiry"],
            buy_put_info["strike"],
            "P",
            "CBOE",
            tradingClass=trading_class,
            currency="USD",
        )
        sell_call = Option(
            ticker_symbol,
            sell_call_info["expiry"],
            sell_call_info["strike"],
            "C",
            "CBOE",
            tradingClass=trading_class,
            currency="USD",
        )
        buy_call = Option(
            ticker_symbol,
            buy_call_info["expiry"],
            buy_call_info["strike"],
            "C",
            "CBOE",
            tradingClass=trading_class,
            currency="USD",
        )
        self.ib.qualifyContracts(sell_put, buy_put, sell_call, buy_call)

        print(
            f"  Sell Put  (SELL) : {sell_put.localSymbol}  (Strike={sell_put_info['strike']})"
        )
        print(
            f"  Buy Put   (BUY)  : {buy_put.localSymbol}  (Strike={buy_put_info['strike']})"
        )
        print(
            f"  Sell Call (SELL) : {sell_call.localSymbol}  (Strike={sell_call_info['strike']})"
        )
        print(
            f"  Buy Call  (BUY)  : {buy_call.localSymbol}  (Strike={buy_call_info['strike']})"
        )

        # --- Calculate initial combined Credit ---
        # Theoretical prices should be passed in the dict as 'theo_price'
        put_credit = sell_put_info["theo_price"] - buy_put_info["theo_price"]
        call_credit = sell_call_info["theo_price"] - buy_call_info["theo_price"]
        raw_total_credit = put_credit + call_credit

        steps = round(raw_total_credit / self.walk_step)
        initial_credit = max(self.min_credit, steps * self.walk_step)
        initial_credit = round(
            round(initial_credit / self.walk_step) * self.walk_step, 2
        )

        print(
            f"\n  Put Side Credit: {put_credit:.2f} | Call Side Credit: {call_credit:.2f}"
        )
        print(
            f"  Total Theoretical Credit: {initial_credit:.2f} (Raw: {raw_total_credit:.2f})"
        )

        if initial_credit < self.min_credit:
            print(
                f"  ⚠ Theoretical Credit ({initial_credit:.2f}) is below minimum "
                f"threshold ({self.min_credit:.2f}), abandoning trade."
            )
            return None

        # --- Build 4-leg BAG combo ---
        combo = Contract(
            symbol=ticker_symbol,
            secType="BAG",
            exchange="CBOE",
            currency="USD",
            tradingClass=trading_class,
            comboLegs=[
                ComboLeg(conId=sell_put.conId, ratio=1, action="SELL", exchange="CBOE"),
                ComboLeg(conId=buy_put.conId, ratio=1, action="BUY", exchange="CBOE"),
                ComboLeg(
                    conId=sell_call.conId, ratio=1, action="SELL", exchange="CBOE"
                ),
                ComboLeg(conId=buy_call.conId, ratio=1, action="BUY", exchange="CBOE"),
            ],
        )

        return self._walk_the_book(combo, initial_credit)
