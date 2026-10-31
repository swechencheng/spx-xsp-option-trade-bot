"""Strategy execution via Walk-The-Book Limit Order placement on IBKR.

Handles combo creation (BAG) for:
- 2-leg Credit Spreads (Bull Put Spread / Bear Call Spread)
- 4-leg Iron Condor (Sell Put, Buy Put, Sell Call, Buy Call)

Execution logic:
  1. Submit BAG combo order with theoretical spread as initial Credit limit price
  2. If not filled after walk_interval seconds, cancel & replace with (Credit - walk_step)
  3. Repeat until filled or Credit drops below min_credit (safety threshold)
"""

import math
import time
from typing import Optional
from ib_async import IB, Option, Contract, ComboLeg, LimitOrder
from utils import get_ny_timestamp


class BaseCreditSpreadTrader:
    """Base class providing shared Walk-The-Book order execution logic."""

    def __init__(
        self,
        ib_client: IB,
        walk_step: float = 0.05,
        walk_interval: int = 5,
        min_credit: float = 0.15,
        quantity: int = 1,
    ):
        self.ib = ib_client
        self.walk_step = walk_step
        self.walk_interval = walk_interval
        self.min_credit = max(0.15, min_credit)
        self.quantity = quantity

    def execute_spread(
        self,
        ticker_symbol: str,
        sell_leg_info: dict,
        buy_leg_info: dict,
        trading_class: str,
    ):
        """Constructs and executes a 2-leg Credit Spread."""
        # --- Qualify Option Contracts ---
        sell_leg = Option(
            ticker_symbol,
            sell_leg_info["expiry"],
            sell_leg_info["strike"],
            sell_leg_info["option_type"],
            "SMART",
            tradingClass=trading_class,
        )
        buy_leg = Option(
            ticker_symbol,
            buy_leg_info["expiry"],
            buy_leg_info["strike"],
            buy_leg_info["option_type"],
            "SMART",
            tradingClass=trading_class,
        )

        self.ib.qualifyContracts(sell_leg, buy_leg)

        # Theoretical net credit from selected strikes
        initial_credit = round(
            sell_leg_info["theo_price"] - buy_leg_info["theo_price"], 2
        )
        print(
            f"{get_ny_timestamp()} Theoretical Credit: {initial_credit:.2f}"
            f" (Raw: {sell_leg_info['theo_price'] - buy_leg_info['theo_price']:.2f})"
        )

        if initial_credit < self.min_credit:
            print(
                f"{get_ny_timestamp()} ⛔ Initial theoretical credit ({initial_credit:.2f}) is below minimum"
                f" threshold ({self.min_credit:.2f}), abandoning trade."
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
        # --- Pre-trade BAG market quote verification ---
        try:
            self.ib.reqMarketDataType(1)
            bag_tickers = self.ib.reqTickers(combo)
            if bag_tickers:
                bag_t = bag_tickers[0]
                bid = bag_t.bid if not math.isnan(bag_t.bid) else None
                ask = bag_t.ask if not math.isnan(bag_t.ask) else None
                if bid is not None and ask is not None and (bid != 0 or ask != 0):
                    bag_mid = (bid + ask) / 2.0
                    bag_credit = abs(bag_mid)
                    print(
                        f"{get_ny_timestamp()}   Pre-trade BAG quote: Bid={bid:.2f}, Ask={ask:.2f} (Mid Credit: ${bag_credit:.2f})"
                    )
                    # If market credit is wildly different from our calculated credit
                    diff = abs(bag_credit - initial_credit)
                    if diff > max(0.25, initial_credit * 0.40):
                        print(
                            f"{get_ny_timestamp()}   ⚠️ Warning: BAG market quote credit (${bag_credit:.2f}) differs "
                            f"significantly from leg calculation (${initial_credit:.2f}). Market may be volatile."
                        )
        except Exception as e:
            print(f"{get_ny_timestamp()}   (Note: BAG market quote check skipped: {e})")

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
            f"\n{get_ny_timestamp()}   📤 Order submitted | Credit: {current_credit:.2f}"
            f" (limitPrice={limit_price:.2f})"
        )

        min_credit_wait_start = None

        # --- Walk-the-Book Loop ---
        while True:
            self.ib.sleep(self.walk_interval)
            self.ib.sleep(0.1)  # Extra tick to let status propagate

            status = trade.orderStatus.status
            print(f"{get_ny_timestamp()}   ⏱  Order status: {status}")

            if status == "Filled":
                fill_price = trade.orderStatus.avgFillPrice
                actual_credit = abs(fill_price)
                print(
                    f"\n{get_ny_timestamp()}   ✅ Order filled! Fill price: {fill_price:.2f}"
                )
                print(f"{get_ny_timestamp()}   Actual Credit received: {actual_credit:.2f}")

                # Adverse selection diagnostic: if fill price is substantially higher than limit price
                if actual_credit >= initial_credit + 0.20:
                    print(
                        f"{get_ny_timestamp()}   ⚠️  WARNING: Actual credit received (${actual_credit:.2f}) "
                        f"is higher than limit credit (${initial_credit:.2f}) by ${actual_credit - initial_credit:.2f}! "
                        "This indicates strong adverse market movement / volatility spike at execution."
                    )
                return trade

            if status in ("Cancelled", "ApiCancelled", "Inactive"):
                # Auto-healing: Check if we were rejected for being too aggressive (Error 202)
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
                                    f"\n{get_ny_timestamp()}   [Auto-Healing] Initial credit {current_credit:.2f} was too aggressive!"
                                )
                                print(
                                    f"{get_ny_timestamp()}   [Auto-Healing] Restarting walk-the-book from {new_credit:.2f} down to {self.min_credit:.2f}"
                                )
                                current_credit = new_credit
                                min_credit_wait_start = None
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
                                    f"{get_ny_timestamp()}   🔄 Repricing order (Auto-Heal) | New Credit: {current_credit:.2f}"
                                    f" (limitPrice={limit_price:.2f})"
                                )
                                continue

                print(
                    f"\n{get_ny_timestamp()}   ❌ Order cancelled or inactive (Status: {status})"
                )
                return None

            # If current_credit is at or below the minimum credit floor, hold for 60 seconds
            if current_credit <= self.min_credit + 1e-5:
                if min_credit_wait_start is None:
                    min_credit_wait_start = time.time()
                    print(
                        f"\n{get_ny_timestamp()}   ⏳ Limit price reached minimum credit floor (${self.min_credit:.2f}). "
                        "Holding order for 60 seconds to seek fill..."
                    )

                elapsed_wait = time.time() - min_credit_wait_start
                if elapsed_wait >= 60.0:
                    print(
                        f"\n{get_ny_timestamp()}   ⛔ Order remained unfilled after 60s at minimum credit (${self.min_credit:.2f}). "
                        "Cancelling order."
                    )
                    self.ib.cancelOrder(order)
                    self.ib.sleep(1)
                    return None
                else:
                    remaining = int(round(60.0 - elapsed_wait))
                    print(
                        f"{get_ny_timestamp()}   ⏳ Waiting for fill at min credit (${self.min_credit:.2f})... "
                        f"{remaining}s remaining"
                    )
                    continue

            # Not filled and above min credit -> reduce Credit towards floor
            next_credit = round(current_credit - self.walk_step, 2)
            if next_credit < self.min_credit:
                next_credit = self.min_credit

            current_credit = next_credit
            self.ib.cancelOrder(order)
            self.ib.sleep(0.5)

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
                f"{get_ny_timestamp()}   🔄 Repricing order (Cancel/Replace) | New Credit: {current_credit:.2f}"
                f" (limitPrice={limit_price:.2f})"
            )


class BullPutSpreadTrader(BaseCreditSpreadTrader):
    """Bull Put Spread Trading Executor"""

    def execute(
        self,
        ticker_symbol: str,
        sell_put_info: dict,
        buy_put_info: dict,
        trading_class: str,
    ):
        print("=" * 60)
        print("  Bull Put Spread Trading Executor")
        print("=" * 60)
        print(
            f"  Sell Leg (SELL) : SPXW  {sell_put_info['expiry'][2:]}P"
            f"{int(sell_put_info['strike'] * 1000):08d}  "
            f"(Strike={sell_put_info['strike']}, Δ={sell_put_info['theo_delta']:.4f})"
        )
        print(
            f"  Buy Leg  (BUY)  : SPXW  {buy_put_info['expiry'][2:]}P"
            f"{int(buy_put_info['strike'] * 1000):08d}  "
            f"(Strike={buy_put_info['strike']}, Δ={buy_put_info['theo_delta']:.4f})"
        )

        return self.execute_spread(
            ticker_symbol, sell_put_info, buy_put_info, trading_class
        )


class BearCallSpreadTrader(BaseCreditSpreadTrader):
    """Bear Call Spread Trading Executor"""

    def execute(
        self,
        ticker_symbol: str,
        sell_call_info: dict,
        buy_call_info: dict,
        trading_class: str,
    ):
        print("=" * 60)
        print("  Bear Call Spread Trading Executor")
        print("=" * 60)
        print(
            f"  Sell Leg (SELL) : SPXW  {sell_call_info['expiry'][2:]}C"
            f"{int(sell_call_info['strike'] * 1000):08d}  "
            f"(Strike={sell_call_info['strike']}, Δ={sell_call_info['theo_delta']:.4f})"
        )
        print(
            f"  Buy Leg  (BUY)  : SPXW  {buy_call_info['expiry'][2:]}C"
            f"{int(buy_call_info['strike'] * 1000):08d}  "
            f"(Strike={buy_call_info['strike']}, Δ={buy_call_info['theo_delta']:.4f})"
        )

        return self.execute_spread(
            ticker_symbol, sell_call_info, buy_call_info, trading_class
        )


class IronCondorTrader(BaseCreditSpreadTrader):
    """Iron Condor Trading Executor.

    All 4 legs are submitted as a single BAG combo order.
    """

    def execute(
        self,
        ticker_symbol: str,
        sell_put_info: dict,
        buy_put_info: dict,
        sell_call_info: dict,
        buy_call_info: dict,
        trading_class: str,
    ):
        print("=" * 60)
        print("  Iron Condor Trading Executor")
        print("=" * 60)
        print(
            f"  Sell Put  (SELL) : SPXW  {sell_put_info['expiry'][2:]}P"
            f"{int(sell_put_info['strike'] * 1000):08d}  (Strike={sell_put_info['strike']})"
        )
        print(
            f"  Buy Put   (BUY)  : SPXW  {buy_put_info['expiry'][2:]}P"
            f"{int(buy_put_info['strike'] * 1000):08d}  (Strike={buy_put_info['strike']})"
        )
        print(
            f"  Sell Call (SELL) : SPXW  {sell_call_info['expiry'][2:]}C"
            f"{int(sell_call_info['strike'] * 1000):08d}  (Strike={sell_call_info['strike']})"
        )
        print(
            f"  Buy Call  (BUY)  : SPXW  {buy_call_info['expiry'][2:]}C"
            f"{int(buy_call_info['strike'] * 1000):08d}  (Strike={buy_call_info['strike']})"
        )

        # --- Qualify Option Contracts ---
        sell_put = Option(
            ticker_symbol,
            sell_put_info["expiry"],
            sell_put_info["strike"],
            sell_put_info["option_type"],
            "SMART",
            tradingClass=trading_class,
        )
        buy_put = Option(
            ticker_symbol,
            buy_put_info["expiry"],
            buy_put_info["strike"],
            buy_put_info["option_type"],
            "SMART",
            tradingClass=trading_class,
        )
        sell_call = Option(
            ticker_symbol,
            sell_call_info["expiry"],
            sell_call_info["strike"],
            sell_call_info["option_type"],
            "SMART",
            tradingClass=trading_class,
        )
        buy_call = Option(
            ticker_symbol,
            buy_call_info["expiry"],
            buy_call_info["strike"],
            buy_call_info["option_type"],
            "SMART",
            tradingClass=trading_class,
        )

        self.ib.qualifyContracts(sell_put, buy_put, sell_call, buy_call)

        put_side_credit = sell_put_info["theo_price"] - buy_put_info["theo_price"]
        call_side_credit = (
            sell_call_info["theo_price"] - buy_call_info["theo_price"]
        )
        raw_initial_credit = put_side_credit + call_side_credit
        initial_credit = round(raw_initial_credit, 2)

        print(
            f"{get_ny_timestamp()}   Put Side Credit: {put_side_credit:.2f} | Call Side Credit: {call_side_credit:.2f}"
        )
        print(
            f"{get_ny_timestamp()}   Total Theoretical Credit: {initial_credit:.2f} (Raw: {raw_initial_credit:.2f})"
        )

        if initial_credit < self.min_credit:
            print(
                f"{get_ny_timestamp()} ⛔ Initial theoretical credit ({initial_credit:.2f}) is below minimum"
                f" threshold ({self.min_credit:.2f}), abandoning trade."
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
