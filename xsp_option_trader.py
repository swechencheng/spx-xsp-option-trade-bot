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
            currency="USD",
        )
        buy_leg = Option(
            ticker_symbol,
            buy_leg_info["expiry"],
            buy_leg_info["strike"],
            buy_leg_info["option_type"],
            "CBOE",
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
        initial_credit = round(
            sell_leg_info["theo_price"] - buy_leg_info["theo_price"], 2
        )
        print(f"\n  Theoretical Credit: {initial_credit:.2f}")

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
            comboLegs=[
                ComboLeg(conId=sell_leg.conId, ratio=1, action="SELL", exchange="CBOE"),
                ComboLeg(conId=buy_leg.conId, ratio=1, action="BUY", exchange="CBOE"),
            ],
        )

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

            # Modify order price in-place (same orderId, IBKR treats as modification)
            limit_price = -current_credit
            order.lmtPrice = limit_price
            trade = self.ib.placeOrder(combo, order)
            print(
                f"  🔄 Repricing order | New Credit: {current_credit:.2f}"
                f" (limitPrice={limit_price:.2f})"
            )


class BullPutSpreadTrader(BaseCreditSpreadTrader):
    """Bull Put Spread Trading Executor"""

    def execute(
        self,
        ticker_symbol: str,
        sell_put_info: dict,
        buy_put_info: dict,
    ):
        return self.execute_spread(
            strategy_name="Bull Put Spread",
            ticker_symbol=ticker_symbol,
            sell_leg_info=sell_put_info,
            buy_leg_info=buy_put_info,
        )


class BearCallSpreadTrader(BaseCreditSpreadTrader):
    """Bear Call Spread Trading Executor"""

    def execute(
        self,
        ticker_symbol: str,
        sell_call_info: dict,
        buy_call_info: dict,
    ):
        return self.execute_spread(
            strategy_name="Bear Call Spread",
            ticker_symbol=ticker_symbol,
            sell_leg_info=sell_call_info,
            buy_leg_info=buy_call_info,
        )
