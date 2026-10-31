"""0DTE SPX/XSP Option Delta Monitor Daemon.

Protects against severe 0DTE gamma blowouts and downward strike breaches:
1. Monitors short legs of SPX/XSP option credit spreads from 60 minutes after market open (10:30 ET)
   until market close (16:00 ET, or 13:00 ET on early close days).
2. Scans every 5 minutes.
3. If abs(delta) of short leg > 0.65, confirms a downside breach and immediately initiates
   upward walk-the-book to close the spread from mid price.
4. If closing takes longer than 15 minutes, converts to Market Order to guarantee fill.
5. Continuously retries upon any unexpected errors.
6. Reports status to Telegram immediately upon trigger, every 15 minutes during closing,
   and upon final fill.
7. Reads open position records from data/active_position.json (persisted by trade bots).
"""

from __future__ import annotations

import argparse
import datetime
import logging
import math
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import pytz
from ib_async import IB, ComboLeg, Contract, LimitOrder, MarketOrder, Option, Trade

import config_manager
import market_schedule
from position_manager import ActiveTradeRecord, OptionLegRecord, PositionManager
from telegram_notifier import TelegramNotifier
from utils import calculate_bs_delta, get_ny_now, get_ny_timestamp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("delta_monitor")

EASTERN_TZ = pytz.timezone("America/New_York")


class DeltaMonitorDaemon:
    """Intraday 0DTE delta risk monitor and emergency closing coordinator."""

    def __init__(
        self,
        config_path: Optional[str] = None,
        use_real_account: bool = False,
        delta_threshold: float = 0.65,
        scan_interval: int = 300,  # 5 minutes
        walk_step: float = 0.05,
        walk_interval: int = 5,  # 5 seconds per repricing
        timeout_minutes: int = 15,  # Force market order after 15 minutes
        record_file: Optional[str] = None,
        data_client_id: int = 25,
        order_client_id: int = 26,
    ):
        self.config = config_manager.load_config(config_path)
        self.use_real_account = use_real_account
        self.delta_threshold = delta_threshold
        self.scan_interval = scan_interval
        self.walk_step = walk_step
        self.walk_interval = walk_interval
        self.timeout_seconds = timeout_minutes * 60
        self.data_client_id = data_client_id
        self.order_client_id = order_client_id

        self.pm = PositionManager(active_trade_file=record_file) if record_file else PositionManager()
        self.notifier = TelegramNotifier()

        self.data_ib = IB()
        self.order_ib = IB()

    def connect(self) -> None:
        """Establish dual gateway connections to IBKR."""
        # 1. Real Gateway for Live Streaming Market Data & Greeks
        real_cfg = self.config.real
        if not self.data_ib.isConnected():
            logger.info(
                "Connecting data_ib to Real Gateway at %s:%d (clientId=%d)...",
                real_cfg.host,
                real_cfg.port,
                self.data_client_id,
            )
            self.data_ib.connect(
                real_cfg.host,
                real_cfg.port,
                clientId=self.data_client_id,
                timeout=20,
            )
            logger.info("✅ data_ib connected to Real Gateway.")

        # 2. Trading Gateway (Paper or Real) for Position & Orders
        target_cfg = self.config.real if self.use_real_account else self.config.paper
        if not self.order_ib.isConnected():
            logger.info(
                "Connecting order_ib to %s Gateway at %s:%d (clientId=%d)...",
                "Real" if self.use_real_account else "Paper",
                target_cfg.host,
                target_cfg.port,
                self.order_client_id,
            )
            self.order_ib.connect(
                target_cfg.host,
                target_cfg.port,
                clientId=self.order_client_id,
                timeout=20,
            )
            logger.info("✅ order_ib connected to Trading Gateway.")

    def disconnect(self) -> None:
        """Safely disconnect IBKR sessions."""
        try:
            if self.data_ib and self.data_ib.isConnected():
                self.data_ib.disconnect()
        except Exception:
            pass
        try:
            if self.order_ib and self.order_ib.isConnected():
                self.order_ib.disconnect()
        except Exception:
            pass
        logger.info("Disconnected IBKR sessions.")

    def ensure_connected(self) -> None:
        """Verify connections and reconnect if dropped."""
        if not self.data_ib.isConnected() or not self.order_ib.isConnected():
            logger.warning("Connection lost. Reconnecting...")
            self.disconnect()
            self.connect()

    def get_active_0dte_trade(self) -> Optional[ActiveTradeRecord]:
        """Load active position and verify it is a 0DTE contract expiring today."""
        trade = self.pm.load_active_trade()
        if not trade:
            return None

        if trade.status != "OPEN":
            return None

        now_ny = get_ny_now()
        today_str = now_ny.strftime("%Y%m%d")

        # Expiry format is YYYYMMDD
        if trade.expiry != today_str:
            logger.debug(
                "Trade in record expires on %s, not today (%s). Not 0DTE yet.",
                trade.expiry,
                today_str,
            )
            return None

        return trade

    def inspect_short_leg_delta(
        self,
        trade: ActiveTradeRecord,
    ) -> List[Tuple[OptionLegRecord, float, float, float]]:
        """Fetch real-time Greeks and delta for short leg(s).

        Returns list of tuples: (leg_record, delta, market_mid, underlying_spot)
        """
        self.ensure_connected()
        self.data_ib.reqMarketDataType(1)  # Real-time streaming

        results = []
        legs_to_check: List[OptionLegRecord] = []

        if trade.strategy == "iron_condor" and trade.iron_condor_legs:
            # Check both short put and short call
            sp = trade.iron_condor_legs.get("sell_put")
            sc = trade.iron_condor_legs.get("sell_call")
            if sp:
                legs_to_check.append(
                    OptionLegRecord(
                        strike=sp["strike"],
                        option_type="P",
                        expiry=sp["expiry"],
                        action="SELL",
                        trading_class=trade.trading_class,
                    )
                )
            if sc:
                legs_to_check.append(
                    OptionLegRecord(
                        strike=sc["strike"],
                        option_type="C",
                        expiry=sc["expiry"],
                        action="SELL",
                        trading_class=trade.trading_class,
                    )
                )
        elif trade.short_leg:
            legs_to_check.append(trade.short_leg)

        for leg in legs_to_check:
            contract = Option(
                trade.symbol,
                leg.expiry,
                leg.strike,
                leg.option_type,
                "SMART",
                tradingClass=leg.trading_class or trade.trading_class,
            )
            self.data_ib.qualifyContracts(contract)

            ticker = self.data_ib.reqTickers(contract)[0]
            # Wait briefly for greeks
            for _ in range(30):
                if ticker.modelGreeks and ticker.modelGreeks.delta is not None and not math.isnan(ticker.modelGreeks.delta):
                    break
                self.data_ib.sleep(0.1)

            # Read delta and market prices
            delta = None
            if ticker.modelGreeks and ticker.modelGreeks.delta is not None and not math.isnan(ticker.modelGreeks.delta):
                delta = float(ticker.modelGreeks.delta)

            # Underlying spot
            spot = 0.0
            if ticker.modelGreeks and getattr(ticker.modelGreeks, "undPrice", None):
                spot = float(ticker.modelGreeks.undPrice)

            # Mid price
            bid = ticker.bid if ticker.bid and not math.isnan(ticker.bid) else 0.0
            ask = ticker.ask if ticker.ask and not math.isnan(ticker.ask) else 0.0
            mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else (ticker.close or ticker.last or 0.0)

            # Fallback delta estimation via Black-Scholes if IBKR modelGreeks empty
            if delta is None and spot > 0:
                logger.warning(
                    "IBKR modelGreeks missing for %s %.1f%s. Estimating via Black-Scholes...",
                    trade.symbol,
                    leg.strike,
                    leg.option_type,
                )
                try:
                    delta = calculate_bs_delta(
                        spot=spot,
                        strike=leg.strike,
                        t_years=0.001,  # 0DTE intraday
                        r=0.045,
                        sigma=0.20,
                        option_type=leg.option_type,
                    )
                except Exception as ex:
                    logger.error("BS calculation failed: %s", ex)
                    delta = 0.0

            results.append((leg, delta or 0.0, mid, spot))

        return results

    def build_closing_combo(
        self,
        trade: ActiveTradeRecord,
        breached_leg: OptionLegRecord,
    ) -> Tuple[Contract, int, float]:
        """Construct the reverse BAG combo contract to buy back the spread.

        Returns (combo_contract, quantity, estimated_initial_mid)
        """
        self.ensure_connected()

        # Identify long leg to pair with breached short leg
        short_leg = breached_leg
        long_leg = None

        if trade.strategy == "iron_condor" and trade.iron_condor_legs:
            if short_leg.option_type == "P":
                bp = trade.iron_condor_legs.get("buy_put")
                long_leg = OptionLegRecord(
                    strike=bp["strike"],
                    option_type="P",
                    expiry=bp["expiry"],
                    action="BUY",
                    trading_class=trade.trading_class,
                )
            else:
                bc = trade.iron_condor_legs.get("buy_call")
                long_leg = OptionLegRecord(
                    strike=bc["strike"],
                    option_type="C",
                    expiry=bc["expiry"],
                    action="BUY",
                    trading_class=trade.trading_class,
                )
        else:
            long_leg = trade.long_leg

        if not long_leg:
            raise ValueError("Cannot find corresponding long leg to close spread safely.")

        short_contract = Option(
            trade.symbol,
            short_leg.expiry,
            short_leg.strike,
            short_leg.option_type,
            "SMART",
            tradingClass=short_leg.trading_class or trade.trading_class,
        )
        long_contract = Option(
            trade.symbol,
            long_leg.expiry,
            long_leg.strike,
            long_leg.option_type,
            "SMART",
            tradingClass=long_leg.trading_class or trade.trading_class,
        )

        self.order_ib.qualifyContracts(short_contract, long_contract)

        # To close: BUY back short leg, SELL long leg
        combo = Contract(
            symbol=trade.symbol,
            secType="BAG",
            exchange="CBOE",
            currency="USD",
            tradingClass=trade.trading_class,
            comboLegs=[
                ComboLeg(conId=short_contract.conId, ratio=1, action="BUY", exchange="CBOE"),
                ComboLeg(conId=long_contract.conId, ratio=1, action="SELL", exchange="CBOE"),
            ],
        )

        # Estimate mid price
        self.data_ib.reqMarketDataType(1)
        tickers = self.data_ib.reqTickers(short_contract, long_contract)
        short_mid = 0.0
        long_mid = 0.0
        if len(tickers) == 2:
            s_t, l_t = tickers[0], tickers[1]
            s_b = s_t.bid if s_t.bid and not math.isnan(s_t.bid) else 0.0
            s_a = s_t.ask if s_t.ask and not math.isnan(s_t.ask) else 0.0
            short_mid = (s_b + s_a) / 2.0 if s_b > 0 and s_a > 0 else (s_t.close or s_t.last or 0.0)

            l_b = l_t.bid if l_t.bid and not math.isnan(l_t.bid) else 0.0
            l_a = l_t.ask if l_t.ask and not math.isnan(l_t.ask) else 0.0
            long_mid = (l_b + l_a) / 2.0 if l_b > 0 and l_a > 0 else (l_t.close or l_t.last or 0.0)

        estimated_mid = max(0.05, round(short_mid - long_mid, 2))
        return combo, trade.quantity, estimated_mid

    def execute_emergency_close(
        self,
        trade: ActiveTradeRecord,
        breached_leg: OptionLegRecord,
        breach_delta: float,
        spot_price: float,
    ) -> Trade:
        """Execute upward walk-the-book to close spread, with 15-min Market Order fallback,

        continuous retries on error, and 15-min periodic Telegram reporting.
        """
        start_time = time.time()
        last_tg_report_time = start_time

        # 1. Send immediate Alert to Telegram
        init_msg = (
            f"🚨 <b>[0DTE Delta Breach Alert] Emergency Close Triggered!</b>\n"
            f"Symbol: {trade.symbol} ({trade.trading_class})\n"
            f"Breached Leg: {breached_leg.strike}{breached_leg.option_type} (Action: {breached_leg.action})\n"
            f"Live Delta: <b>{breach_delta:.4f}</b> (Breached threshold |Δ| > {self.delta_threshold:.2f})\n"
            f"Underlying Spot: ${spot_price:.2f}\n"
            f"Quantity: {trade.quantity}\n"
            f"Action: Walking UP from Mid Price via Walk-the-book; will convert to Market Order after 15m to guarantee fill!"
        )
        logger.warning(init_msg)
        self.notifier.send_message(init_msg)

        combo, quantity, mid_price = self.build_closing_combo(trade, breached_leg)
        current_limit = max(0.05, mid_price)
        logger.info(
            "Starting emergency close: Combo constructed. Initial Mid Price: $%.2f. Walking UP by $%.2f every %ds.",
            current_limit,
            self.walk_step,
            self.walk_interval,
        )

        active_order = None
        is_market_order = False
        active_trade_obj: Optional[Trade] = None

        while True:
            try:
                self.ensure_connected()
                now_ts = time.time()
                elapsed = now_ts - start_time

                # Check Telegram periodic 15-minute report
                if now_ts - last_tg_report_time >= 15 * 60:
                    status_text = (
                        active_trade_obj.orderStatus.status if active_trade_obj else "Initializing"
                    )
                    periodic_msg = (
                        f"⏳ <b>[0DTE Close Status Update]</b>\n"
                        f"Symbol: {trade.symbol} {breached_leg.strike}{breached_leg.option_type}\n"
                        f"Elapsed: {elapsed / 60.0:.1f} minutes\n"
                        f"Target Price: ${current_limit:.2f} ({'Market Order' if is_market_order else 'Limit Walk-the-book'})\n"
                        f"Order Status: {status_text}\n"
                        f"Actively seeking fill..."
                    )
                    self.notifier.send_message(periodic_msg)
                    last_tg_report_time = now_ts

                # Check 15-minute timeout -> Convert to Market Order
                if elapsed >= self.timeout_seconds and not is_market_order:
                    logger.warning(
                        "⚠️ 15 minutes elapsed since breach without fill (elapsed=%.1fs). "
                        "Cancelling limit order and submitting MARKET ORDER to guarantee fill!",
                        elapsed,
                    )
                    if active_trade_obj and active_trade_obj.orderStatus.status in ("Submitted", "PreSubmitted"):
                        self.order_ib.cancelOrder(active_order)
                        self.order_ib.sleep(1.0)

                    market_order = MarketOrder(action="BUY", totalQuantity=quantity, tif="DAY")
                    market_order.transmit = True
                    market_order.overridePercentageConstraints = True

                    active_order = market_order
                    active_trade_obj = self.order_ib.placeOrder(combo, market_order)
                    is_market_order = True
                    logger.info("📤 Market Order placed for closing combo.")

                # Initial limit order placement
                if active_order is None and not is_market_order:
                    limit_order = LimitOrder(
                        action="BUY",
                        totalQuantity=quantity,
                        lmtPrice=current_limit,
                        tif="DAY",
                    )
                    limit_order.transmit = True
                    limit_order.overridePercentageConstraints = True
                    active_order = limit_order
                    active_trade_obj = self.order_ib.placeOrder(combo, limit_order)
                    logger.info("📤 Initial Limit Order placed: $%.2f", current_limit)

                # Wait interval
                self.order_ib.sleep(self.walk_interval)

                # Check status
                if active_trade_obj:
                    status = active_trade_obj.orderStatus.status
                    logger.info(
                        "Order status: %s | Elapsed: %.1fs | Mode: %s | Limit: $%.2f",
                        status,
                        elapsed,
                        "MKT" if is_market_order else "LMT",
                        current_limit,
                    )

                    if status == "Filled":
                        logger.info("✅ Closing order FILLED!")
                        break

                    if status in ("Cancelled", "Inactive"):
                        logger.warning("Order %s unexpectedly! Re-submitting...", status)
                        active_order = None
                        active_trade_obj = None
                        continue

                    # If not market order and still open, walk UP
                    if not is_market_order:
                        current_limit = round(current_limit + self.walk_step, 2)
                        active_order.lmtPrice = current_limit
                        active_trade_obj = self.order_ib.placeOrder(combo, active_order)
                        logger.info("🔄 Repriced closing order UP -> $%.2f", current_limit)

            except Exception as e:
                logger.error("Unexpected error during emergency close: %s. Retrying in 2 seconds...", e)
                time.sleep(2.0)

        # Post-Fill Completion
        fill_price = abs(active_trade_obj.orderStatus.avgFillPrice) if active_trade_obj else current_limit
        total_cost = fill_price * 100 * quantity
        total_time_min = (time.time() - start_time) / 60.0

        # Extract commission if available
        commissions = [
            fill.commissionReport.commission
            for fill in getattr(active_trade_obj, "fills", [])
            if getattr(fill, "commissionReport", None) and fill.commissionReport.commission is not None
        ]
        total_comm = sum(commissions) if commissions else 0.0

        # Mark closed in position manager
        self.pm.mark_trade_closed(
            close_price=fill_price,
            reason="0DTE_DELTA_BREACH_STOP_LOSS",
            extra_info={
                "breach_delta": breach_delta,
                "spot_at_breach": spot_price,
                "elapsed_minutes": round(total_time_min, 2),
                "is_market_order": is_market_order,
                "commission": total_comm,
            },
        )

        fill_msg = (
            f"✅ <b>[0DTE Emergency Close Filled]</b>\n"
            f"Symbol: {trade.symbol} ({trade.trading_class})\n"
            f"Bought Back: {breached_leg.strike}{breached_leg.option_type}\n"
            f"Average Fill Price: <b>${fill_price:.2f}/share</b>\n"
            f"Total Closing Cost: ${total_cost:.2f}"
            + (f" (Commission: ${total_comm:.2f})\n" if total_comm > 0 else "\n")
            + f"Total Duration: {total_time_min:.1f} minutes\n"
            f"Execution Type: {'Market Order (15m Timeout Forced)' if is_market_order else 'Limit Walk-the-book'}\n"
            f"Position fully closed. Gamma risk eliminated!"
        )
        logger.info(fill_msg)
        self.notifier.send_message(fill_msg)
        return active_trade_obj

    def scan_once(self) -> Dict[str, Any]:
        """Perform a single scan of the 0DTE short leg delta."""
        now_ny = get_ny_now()
        is_in_win, reason = market_schedule.is_monitoring_window(now_ny)
        if not is_in_win:
            logger.info("Outside monitoring window: %s", reason)
            return {"status": "outside_window", "reason": reason}

        trade = self.get_active_0dte_trade()
        if not trade:
            logger.info("No active 0DTE position found in records. Scanning again next cycle.")
            return {"status": "no_active_0dte_trade"}

        logger.info(
            "Found active 0DTE trade: %s %s (Expiry: %s, Qty: %d). Inspecting Greeks...",
            trade.symbol,
            trade.strategy,
            trade.expiry,
            trade.quantity,
        )

        leg_results = self.inspect_short_leg_delta(trade)
        if not leg_results:
            logger.warning("Could not evaluate short legs for trade.")
            return {"status": "greeks_unavailable"}

        for leg, delta, mid, spot in leg_results:
            logger.info(
                "Short Leg: %s %.1f%s | Live Delta: %.4f | Mid: $%.2f | Underlying Spot: $%.2f",
                trade.symbol,
                leg.strike,
                leg.option_type,
                delta,
                mid,
                spot,
            )

            # Check breach condition: |delta| > 0.65
            if abs(delta) > self.delta_threshold:
                logger.warning(
                    "❌ BREACH DETECTED: %s %.1f%s Delta %.4f exceeds threshold |Δ| > %.2f!",
                    trade.symbol,
                    leg.strike,
                    leg.option_type,
                    delta,
                    self.delta_threshold,
                )
                self.execute_emergency_close(trade, leg, delta, spot)
                return {"status": "breached_and_closed", "leg": leg.strike, "delta": delta}

        logger.info("All short leg deltas within safety threshold (|Δ| <= %.2f).", self.delta_threshold)
        return {"status": "healthy"}

    def run_daemon(self) -> None:
        """Run continuous daemon loop scanning every scan_interval seconds."""
        logger.info("============================================================")
        logger.info("Starting 0DTE Delta Monitor Daemon")
        logger.info("  Delta Threshold: |Δ| > %.2f", self.delta_threshold)
        logger.info("  Scan Interval:   %d seconds (5 minutes)", self.scan_interval)
        logger.info("  Monitoring Hours: 10:30 ET - Market Close (16:00 / 13:00 Early Close)")
        logger.info("  Account:         %s", "Real (Port 4001)" if self.use_real_account else "Paper (Port 4002)")
        logger.info("============================================================")

        try:
            self.connect()
        except Exception as e:
            logger.error("Initial IBKR connection failed: %s. Will retry in loop...", e)

        while True:
            try:
                now_ny = get_ny_now()
                is_in_win, reason = market_schedule.is_monitoring_window(now_ny)

                if is_in_win:
                    self.scan_once()
                    time.sleep(self.scan_interval)
                else:
                    logger.info("Status: %s. Checking again in 60 seconds...", reason)
                    time.sleep(60)

            except KeyboardInterrupt:
                logger.info("Delta monitor stopped by user.")
                break
            except Exception as e:
                logger.error("Error in daemon loop: %s. Retrying in 10 seconds...", e)
                time.sleep(10)

        self.disconnect()


def main():
    parser = argparse.ArgumentParser(description="0DTE SPX/XSP Option Delta Monitor Daemon")
    parser.add_argument("--real", action="store_true", help="Execute orders on Real account (default is Paper)")
    parser.add_argument("--threshold", type=float, default=0.65, help="Delta breach threshold (default 0.65)")
    parser.add_argument("--interval", type=int, default=300, help="Scan interval in seconds (default 300)")
    parser.add_argument("--check-once", action="store_true", help="Perform a single check and exit")
    parser.add_argument("--record-file", type=str, default=None, help="Custom path to active_position.json")
    parser.add_argument("--config", type=str, default=None, help="Custom path to config.json")
    args = parser.parse_args()

    daemon = DeltaMonitorDaemon(
        config_path=args.config,
        use_real_account=args.real,
        delta_threshold=args.threshold,
        scan_interval=args.interval,
        record_file=args.record_file,
    )

    if args.check_once:
        try:
            daemon.connect()
            daemon.scan_once()
        finally:
            daemon.disconnect()
    else:
        daemon.run_daemon()


if __name__ == "__main__":
    main()
