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
from ib_async import IB, ComboLeg, Contract, Index, LimitOrder, MarketOrder, Option, Trade

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

    def get_active_0dte_trades(self) -> List[ActiveTradeRecord]:
        """Discover live 0DTE short option positions directly from IBKR.

        Queries order_ib.positions() to inspect actual holdings. If positions exist,
        they are matched to local trade records or dynamically synthesized and persisted.
        """
        self.ensure_connected()

        now_ny = get_ny_now()
        today_str = now_ny.strftime("%Y%m%d")

        try:
            positions = self.order_ib.positions()
            if not isinstance(positions, (list, tuple)):
                positions = []
        except Exception as e:
            logger.error("Failed to query live positions from order_ib: %s", e)
            positions = []

        target_cfg = self.config.real if self.use_real_account else self.config.paper
        logger.info(
            "Retrieved %d position(s) from %s account (port %d).",
            len(positions),
            "Real" if self.use_real_account else "Paper",
            target_cfg.port,
        )

        if len(positions) == 0:
            logger.info(
                "💡 Tip: 0 positions found in %s account. If your active trade is held in the %s account, "
                "ensure the daemon is running with '%s'.",
                "Real" if self.use_real_account else "Paper",
                "Paper" if self.use_real_account else "Real",
                "--paper" if self.use_real_account else "--real",
            )

        # Filter option positions for SPX / XSP expiring today
        matching_positions = []
        for p in positions:
            c = getattr(p, "contract", None)
            if not c:
                continue
            sec_type = getattr(c, "secType", "")
            sym = getattr(c, "symbol", "")
            pos_qty = getattr(p, "position", 0)

            if sec_type == "OPT" and sym in ("SPX", "XSP") and pos_qty != 0:
                raw_exp = str(getattr(c, "lastTradeDateOrContractMonth", "") or "")
                exp_clean = raw_exp.replace("-", "").strip()
                if exp_clean == today_str:
                    matching_positions.append(p)

        local_trade = self.pm.load_active_trade()

        if not matching_positions:
            # Fallback to local record if order_ib has no positions (e.g. testing or disconnected)
            if local_trade and local_trade.status == "OPEN" and local_trade.expiry == today_str:
                # If order_ib is connected and returned other positions explicitly, this trade was closed in IBKR
                if self.order_ib.isConnected() and isinstance(positions, list) and len(positions) > 0:
                    logger.info(
                        "Local record for %s %s was marked OPEN, but 0 matching 0DTE positions remain in IBKR. Marking CLOSED.",
                        local_trade.symbol,
                        local_trade.strategy,
                    )
                    self.pm.mark_trade_closed(close_price=0.0, reason="EXPIRED_OR_CLOSED_EXTERNALLY")
                    return []
                # Otherwise, allow local_trade as fallback (useful for offline mock tests)
                return [local_trade]
            return []

        # Group matching positions by (symbol, expiry)
        by_group: Dict[Tuple[str, str], List[Any]] = {}
        for p in matching_positions:
            c = p.contract
            key = (c.symbol, str(getattr(c, "lastTradeDateOrContractMonth", "")).replace("-", "").strip())
            by_group.setdefault(key, []).append(p)

        active_trades: List[ActiveTradeRecord] = []

        for (sym, exp_str), group in by_group.items():
            short_positions = [p for p in group if p.position < 0]
            long_positions = [p for p in group if p.position > 0]

            if not short_positions:
                logger.debug("Group %s %s has %d long positions but no short legs. Skipping.", sym, exp_str, len(long_positions))
                continue

            for sp in short_positions:
                sc = sp.contract
                short_strike = float(sc.strike)
                short_right = sc.right
                short_qty = abs(int(sp.position))
                short_tclass = getattr(sc, "tradingClass", None) or ("SPXW" if sym == "SPX" else "XSP")

                # Try to pair with a matching long leg
                matched_long = None
                if short_right == "P":
                    candidate_longs = [
                        lp for lp in long_positions
                        if lp.contract.right == "P" and float(lp.contract.strike) < short_strike
                    ]
                    if candidate_longs:
                        candidate_longs.sort(key=lambda x: float(x.contract.strike), reverse=True)
                        matched_long = candidate_longs[0]
                elif short_right == "C":
                    candidate_longs = [
                        lp for lp in long_positions
                        if lp.contract.right == "C" and float(lp.contract.strike) > short_strike
                    ]
                    if candidate_longs:
                        candidate_longs.sort(key=lambda x: float(x.contract.strike))
                        matched_long = candidate_longs[0]

                # Check if local record matches this short leg
                if (
                    local_trade
                    and local_trade.status == "OPEN"
                    and local_trade.symbol == sym
                    and local_trade.expiry == exp_str
                    and local_trade.short_leg
                    and local_trade.short_leg.strike == short_strike
                    and local_trade.short_leg.option_type == short_right
                ):
                    trade_to_use = local_trade
                    trade_to_use.quantity = short_qty
                else:
                    detected_strategy = "bull" if short_right == "P" else "bear"
                    if matched_long is None:
                        detected_strategy = f"single_short_{short_right.lower()}ut" if short_right == "P" else f"single_short_{short_right.lower()}all"

                    long_leg_rec = None
                    if matched_long:
                        lc = matched_long.contract
                        long_leg_rec = OptionLegRecord(
                            strike=float(lc.strike),
                            option_type=lc.right,
                            expiry=exp_str,
                            action="BUY",
                            con_id=getattr(lc, "conId", None),
                            trading_class=getattr(lc, "tradingClass", None) or short_tclass,
                        )

                    short_leg_rec = OptionLegRecord(
                        strike=short_strike,
                        option_type=short_right,
                        expiry=exp_str,
                        action="SELL",
                        con_id=getattr(sc, "conId", None),
                        trading_class=short_tclass,
                    )

                    fill_price = abs(float(sp.avgCost / 100.0)) if getattr(sp, "avgCost", 0) else 0.0

                    trade_to_use = ActiveTradeRecord(
                        symbol=sym,
                        trading_class=short_tclass,
                        strategy=detected_strategy,
                        opened_at=now_ny.isoformat(),
                        expiry=exp_str,
                        quantity=short_qty,
                        fill_price=fill_price,
                        status="OPEN",
                        short_leg=short_leg_rec,
                        long_leg=long_leg_rec,
                    )
                    self.pm.save_active_trade(trade_to_use)
                    logger.info(
                        "✅ Discovered live 0DTE position from IBKR: %s %s %.1f%s (Qty: %d, Expiry: %s). Synced to local records.",
                        sym,
                        detected_strategy,
                        short_strike,
                        short_right,
                        short_qty,
                        exp_str,
                    )

                active_trades.append(trade_to_use)

        return active_trades

    def get_active_0dte_trade(self) -> Optional[ActiveTradeRecord]:
        """Load active position and verify it is a 0DTE contract expiring today."""
        trades = self.get_active_0dte_trades()
        return trades[0] if trades else None

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

            if spot <= 0:
                # Query index contract directly as spot fallback
                try:
                    idx_contract = Index(trade.symbol, "CBOE")
                    self.data_ib.qualifyContracts(idx_contract)
                    idx_ticker = self.data_ib.reqTickers(idx_contract)[0]
                    spot = float(idx_ticker.marketPrice() or idx_ticker.last or idx_ticker.close or 0.0)
                except Exception as ex:
                    logger.debug("Failed to fetch spot from index: %s", ex)

            # Mid price
            bid = ticker.bid if ticker.bid and not math.isnan(ticker.bid) else 0.0
            ask = ticker.ask if ticker.ask and not math.isnan(ticker.ask) else 0.0
            mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else (ticker.close or ticker.last or 0.0)

            # Fallback delta estimation via Black-Scholes if IBKR modelGreeks empty
            if delta is None and spot > 0:
                logger.warning(
                    "IBKR modelGreeks missing for %s %.1f%s. Estimating via Black-Scholes (Spot=%.2f)...",
                    trade.symbol,
                    leg.strike,
                    leg.option_type,
                    spot,
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
        """Construct the closing contract (either 2-leg BAG combo or single Option) to buy back position.

        Returns (combo_contract, quantity, estimated_initial_mid)
        """
        self.ensure_connected()

        # Identify long leg to pair with breached short leg
        short_leg = breached_leg
        long_leg = None

        if trade.strategy == "iron_condor" and trade.iron_condor_legs:
            if short_leg.option_type == "P":
                bp = trade.iron_condor_legs.get("buy_put")
                if bp:
                    long_leg = OptionLegRecord(
                        strike=bp["strike"],
                        option_type="P",
                        expiry=bp["expiry"],
                        action="BUY",
                        trading_class=trade.trading_class,
                    )
            else:
                bc = trade.iron_condor_legs.get("buy_call")
                if bc:
                    long_leg = OptionLegRecord(
                        strike=bc["strike"],
                        option_type="C",
                        expiry=bc["expiry"],
                        action="BUY",
                        trading_class=trade.trading_class,
                    )
        else:
            long_leg = trade.long_leg

        short_contract = Option(
            trade.symbol,
            short_leg.expiry,
            short_leg.strike,
            short_leg.option_type,
            "SMART",
            tradingClass=short_leg.trading_class or trade.trading_class,
        )
        self.order_ib.qualifyContracts(short_contract)

        # Estimate short leg mid price
        self.data_ib.reqMarketDataType(1)
        s_tickers = self.data_ib.reqTickers(short_contract)
        short_mid = 0.0
        if s_tickers:
            s_t = s_tickers[0]
            s_b = s_t.bid if s_t.bid and not math.isnan(s_t.bid) else 0.0
            s_a = s_t.ask if s_t.ask and not math.isnan(s_t.ask) else 0.0
            short_mid = (s_b + s_a) / 2.0 if s_b > 0 and s_a > 0 else (s_t.close or s_t.last or 0.0)

        # Case 1: Spread with matching long leg -> 2-leg BAG Combo
        if long_leg is not None:
            long_contract = Option(
                trade.symbol,
                long_leg.expiry,
                long_leg.strike,
                long_leg.option_type,
                "SMART",
                tradingClass=long_leg.trading_class or trade.trading_class,
            )
            self.order_ib.qualifyContracts(long_contract)

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

            l_tickers = self.data_ib.reqTickers(long_contract)
            long_mid = 0.0
            if l_tickers:
                l_t = l_tickers[0]
                l_b = l_t.bid if l_t.bid and not math.isnan(l_t.bid) else 0.0
                l_a = l_t.ask if l_t.ask and not math.isnan(l_t.ask) else 0.0
                long_mid = (l_b + l_a) / 2.0 if l_b > 0 and l_a > 0 else (l_t.close or l_t.last or 0.0)

            estimated_mid = max(0.05, round(short_mid - long_mid, 2))
            return combo, trade.quantity, estimated_mid

        # Case 2: Single short option leg -> Direct Option contract (BUY action)
        # Note: Single-leg BAG orders are silently dropped by IBKR, so must use Option contract directly!
        estimated_mid = max(0.05, round(short_mid, 2))
        return short_contract, trade.quantity, estimated_mid

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

    def scan_once(self, force: bool = False) -> Dict[str, Any]:
        """Perform a single scan of the 0DTE short leg delta."""
        now_ny = get_ny_now()
        if not force:
            is_in_win, reason = market_schedule.is_monitoring_window(now_ny)
            if not is_in_win:
                logger.info("Outside monitoring window: %s", reason)
                return {"status": "outside_window", "reason": reason}

        trades = self.get_active_0dte_trades()
        if not trades:
            logger.info("No active 0DTE position found in records or live IBKR account. Scanning again next cycle.")
            return {"status": "no_active_0dte_trade"}

        overall_status = "healthy"
        for trade in trades:
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
                continue

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

                # Check breach condition: |delta| > self.delta_threshold
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
        return {"status": overall_status}

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
    parser.add_argument("--force", action="store_true", help="Bypass 10:30 ET window check and scan immediately")
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
            daemon.scan_once(force=args.force)
        finally:
            daemon.disconnect()
    else:
        daemon.run_daemon()


if __name__ == "__main__":
    main()
