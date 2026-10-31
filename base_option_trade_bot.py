import sys

# Ensure line-buffered / unbuffered output for real-time logging
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(line_buffering=True)

import argparse
import datetime
import html
import logging
import math
import time
import traceback
from datetime import datetime
import pandas as pd
import pytz
import yfinance as yf
from ib_async import IB, util

# Suppress harmless IBKR errors during option chain qualification
class IBErrorFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        if (
            "Error 200" in msg
            or "Unknown contract" in msg
            or "Error 10090" in msg
            or "Error 354" in msg
        ):
            return False
        return True


logging.getLogger("ib_async").addFilter(IBErrorFilter())

import config_manager
import event_filter
import market_schedule
from iv_provider import IVProvider
from market_data_fetcher import MarketDataFetcher
from option_finder_ibkr import OptionFinder as IbkrOptionFinder
from option_finder_theory import OptionFinder as TheoryOptionFinder
from strategy_trader import (
    BearCallSpreadTrader,
    BullPutSpreadTrader,
    IronCondorTrader,
)
from telegram_notifier import TelegramNotifier
from utils import (
    calculate_bs_delta,
    calculate_bs_price,
    calculate_trading_time_t,
    get_ny_timestamp,
)


def is_valid_trading_day() -> bool:
    """Checks if today is a valid trading day by comparing US/Eastern date with Yahoo Finance's latest ^SPX data."""
    try:
        spx = yf.Ticker("^SPX")
        hist = spx.history(period="1d")
        if hist.empty:
            return False

        # YFinance returns tz-aware index in America/New_York
        latest_date = hist.index[-1].date()
        today_est = datetime.now(pytz.timezone("US/Eastern")).date()

        print(
            f"{get_ny_timestamp()} [*] YF Latest Date: {latest_date} | Today EST: {today_est}"
        )
        return latest_date == today_est
    except Exception as e:
        print(f"{get_ny_timestamp()} [-] Error checking trading day: {e}")
        return False


class BaseOptionTradeBot:
    """Abstract base class for options trading bots (e.g. XSP, SPX)."""

    @property
    def ticker_symbol(self) -> str:
        raise NotImplementedError()

    @property
    def trading_class(self) -> str:
        raise NotImplementedError()

    @property
    def spot_multiplier(self) -> float:
        raise NotImplementedError()

    @property
    def default_walk_step(self) -> float:
        raise NotImplementedError()

    @property
    def strike_offset(self) -> int:
        raise NotImplementedError()

    @property
    def use_iron_condor(self) -> bool:
        """If True, execute an iron condor when the regime is bullish."""
        return False

    def _build_header(self, now_est: datetime) -> str:
        """Build the common Telegram message header."""
        return (
            f"🤖 <b>{self.ticker_symbol} Trading Bot Report</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 {now_est.strftime('%Y-%m-%d %H:%M %Z')}\n"
        )

    def _build_market_section(
        self, today_close: float, today_ema20: float, regime: str
    ) -> str:
        """Build the market data section of the Telegram message."""
        return (
            f"📈 SPX Close: {today_close:.2f}\n"
            f"📊 EMA20: {today_ema20:.2f}\n"
            f"📌 Regime: {regime}\n"
        )

    def parse_args(self):
        parser = argparse.ArgumentParser(
            description=f"{self.ticker_symbol} Automated Options Trading Bot"
        )
        parser.add_argument(
            "--config",
            type=str,
            default=None,
            help="Path to config.json (default: search current dir)",
        )
        parser.add_argument(
            "--real",
            action="store_true",
            help="Execute orders on IBKR Real account (default: Paper account)",
        )
        parser.add_argument(
            "--ib-host",
            type=str,
            default=None,
            help="IBKR Host override for trading account (default: from config.json)",
        )
        parser.add_argument(
            "--ib-port",
            type=int,
            default=None,
            help="IBKR Port override for trading account (default: from config.json)",
        )
        parser.add_argument(
            "--client-id",
            type=int,
            default=None,
            help="IBKR Client ID override for trading account",
        )
        parser.add_argument(
            "--bull-delta",
            type=float,
            default=0.05,
            help="Sell leg target delta for Bull Put Spread (default: 0.05)",
        )
        parser.add_argument(
            "--bear-delta",
            type=float,
            default=0.05,
            help="Sell leg target delta for Bear Call Spread (default: 0.05)",
        )
        parser.add_argument(
            "--dte-target",
            type=int,
            default=1,
            help="Target days-to-expiration (default: 1)",
        )
        parser.add_argument(
            "--walk-step",
            type=float,
            default=self.default_walk_step,
            help=f"Credit reduction per repricing round (default: {self.default_walk_step})",
        )
        parser.add_argument(
            "--walk-interval",
            type=int,
            default=5,
            help="Seconds to wait between fill checks (default: 5)",
        )
        strategy_group = parser.add_mutually_exclusive_group()
        strategy_group.add_argument(
            "--iron-condor",
            action="store_true",
            default=None,
            help="If set, executes an Iron Condor in bullish regimes (default depends on subclass)",
        )
        strategy_group.add_argument(
            "--always-put-spread",
            action="store_true",
            default=False,
            help="If set, always executes a Put Spread regardless of regime (bullish or bearish)",
        )
        parser.add_argument(
            "--min-credit",
            type=float,
            default=0.09,
            help="Minimum acceptable net credit (default: 0.09)",
        )
        parser.add_argument(
            "--quantity",
            type=int,
            default=1,
            help="Number of spread contracts to trade (default: 1)",
        )
        parser.add_argument(
            "--ib-market",
            action="store_true",
            help="Use IBKR market data for option pricing instead of theory",
        )
        parser.add_argument(
            "--add-bear",
            action="store_true",
            help="Enable Bear Call Credit Spreads (disabled by default)",
        )
        parser.add_argument(
            "--spread-step",
            type=int,
            default=1,
            help="Number of strike steps between short and long legs (default: 1, e.g. 2 = 10-wide for SPX)",
        )
        parser.add_argument(
            "--max-model-market-diff",
            type=float,
            default=0.30,
            help="Max allowable divergence between model price and market price (default: 0.30, i.e. 30%%)",
        )
        parser.add_argument(
            "--ignore-events",
            action="store_true",
            help="Skip macro and tech earnings event blackout checks",
        )
        parser.add_argument(
            "--ignore-time-check",
            action="store_true",
            help="Skip trading window schedule timing check",
        )
        args = parser.parse_args()

        if args.always_put_spread:
            args.iron_condor = False
        elif args.iron_condor is None:
            args.iron_condor = self.use_iron_condor

        # Enforce default_walk_step increments
        steps = round(args.walk_step / self.default_walk_step)
        new_walk_step = max(self.default_walk_step, steps * self.default_walk_step)
        if abs(args.walk_step - new_walk_step) > 1e-5:
            print(
                f"[!] Warning: walk_step {args.walk_step} is not a multiple of "
                f"{self.default_walk_step}. Overriding to {new_walk_step:.2f}."
            )
            args.walk_step = round(new_walk_step, 2)

        return args

    def run(self):
        args = self.parse_args()
        now_est = datetime.now(pytz.timezone("US/Eastern"))

        notifier = TelegramNotifier()

        print("=" * 60)
        print(
            f" {self.ticker_symbol} Automated Trading Bot - Started at {get_ny_timestamp()}"
        )
        print("=" * 60)

        try:
            self._run_strategy(args, now_est, notifier)
        except Exception as e:
            tb = traceback.format_exc()
            print(f"[!] Unexpected exception:\n{tb}")
            msg = (
                self._build_header(now_est)
                + "\n🔥 <b>UNEXPECTED EXCEPTION</b>\n"
                + f"<pre>{html.escape(tb[-2000:])}</pre>"
            )
            notifier.send_message(msg)
            sys.exit(1)

    def _run_strategy(self, args, now_est: datetime, notifier: TelegramNotifier):
        # 1. Validation: Ensure it's a trading day
        if not is_valid_trading_day():
            reason = "Not a valid trading day (market closed or holiday)"
            print(f"{get_ny_timestamp()} [!] {reason}. Exiting.")
            msg = self._build_header(now_est) + f"\n📅 <b>No Trade</b>\n{reason}"
            notifier.send_message(msg)
            sys.exit(0)

        # 2. Market Schedule & Early Close Verification
        today_date = now_est.date()
        is_early = market_schedule.is_early_close(today_date)
        close_time = market_schedule.get_market_close_time(today_date)
        trigger_time = market_schedule.get_target_trigger_time(today_date)
        mins_left = market_schedule.get_minutes_until_close(now_est)

        print(
            f"{get_ny_timestamp()} [*] Market Schedule: Close at {close_time.strftime('%H:%M')} ET "
            f"(Early Close: {is_early}) | Target Trigger: {trigger_time.strftime('%H:%M')} ET | Minutes until close: {mins_left:.1f}m"
        )

        if not args.ignore_time_check and mins_left < 0:
            reason = f"Market already closed for today at {close_time.strftime('%H:%M')} ET."
            print(f"{get_ny_timestamp()} [!] {reason} Exiting.")
            msg = self._build_header(now_est) + f"\n⛔ <b>No Trade</b>\n{reason}"
            notifier.send_message(msg)
            sys.exit(0)

        # 3. Macro Event and Tech Earnings Filter
        if not args.ignore_events:
            print(
                f"{get_ny_timestamp()} [*] Checking Macro Events (FOMC, CPI, NFP) and Major Tech Earnings..."
            )
            skip_event, event_reason = event_filter.should_skip_trade(today_date)
            if skip_event:
                print(
                    f"{get_ny_timestamp()} 🚫 [Event Filter Block] {event_reason}. Aborting trade."
                )
                msg = (
                    self._build_header(now_est)
                    + f"\n📅 <b>No Trade (Event Filter)</b>\n{event_reason}"
                )
                notifier.send_message(msg)
                sys.exit(0)
            else:
                print(
                    f"{get_ny_timestamp()} [+] Event filter passed: No blocking macro events or earnings."
                )

        # 4. Load Gateway Configurations (Dual connection: Real for Data, Paper/Real for Trading)
        cfg = config_manager.load_config(args.config)
        real_host = cfg.real.host
        real_port = cfg.real.port
        real_client_id = cfg.real.client_id

        # Determine trading gateway
        if args.real:
            trading_host = args.ib_host or cfg.real.host
            trading_port = args.ib_port or cfg.real.port
            trading_client_id = (
                args.client_id or (real_client_id + 1 if trading_port == real_port else cfg.real.client_id)
            )
            mode_label = "REAL Account Live Trading"
        else:
            trading_host = args.ib_host or cfg.paper.host
            trading_port = args.ib_port or cfg.paper.port
            trading_client_id = (
                args.client_id or (real_client_id + 1 if trading_port == real_port else cfg.paper.client_id)
            )
            mode_label = "PAPER Account Simulation Trading"

        print(
            f"{get_ny_timestamp()} [*] Configuration Loaded | Execution Mode: {mode_label}\n"
            f"    Market Data Gateway (Real): {real_host}:{real_port} (clientId={real_client_id})\n"
            f"    Order Trading Gateway:      {trading_host}:{trading_port} (clientId={trading_client_id})"
        )

        data_ib = None
        trading_ib = None

        # 5. Connect to Real Account Gateway for Market Data (Strictly Real Gateway, Max 5 Retries)
        data_ib = IB()
        max_conn_attempts = 5
        for attempt in range(1, max_conn_attempts + 1):
            try:
                print(
                    f"{get_ny_timestamp()} [*] Connecting to IBKR Real Account for Market Data at "
                    f"{real_host}:{real_port} (Attempt {attempt}/{max_conn_attempts})..."
                )
                data_ib.connect(real_host, real_port, clientId=real_client_id, timeout=10)
                if data_ib.isConnected():
                    print(
                        f"{get_ny_timestamp()} [+] Successfully connected to IBKR Real Account (Market Data)."
                    )
                    break
            except Exception as e:
                print(
                    f"{get_ny_timestamp()} [-] Real Account connection attempt {attempt}/{max_conn_attempts} failed: {e}"
                )
                if attempt < max_conn_attempts:
                    time.sleep(2)
        else:
            reason = (
                f"Failed to connect to IBKR Real Account (Market Data) at {real_host}:{real_port} "
                f"after {max_conn_attempts} attempts. Aborting trade."
            )
            print(f"{get_ny_timestamp()} ❌ {reason}")
            msg = self._build_header(now_est) + f"\n❌ <b>Market Data Gateway Error</b>\n{reason}"
            notifier.send_message(msg)
            sys.exit(1)

        # 6. Fetch live spot price from Real Account session
        ib_spot = None
        fetcher = MarketDataFetcher()
        risk_free_rate = fetcher.get_risk_free_rate()

        if args.ib_market:
            from ib_async import Index

            spx = Index("SPX", "CBOE")
            data_ib.qualifyContracts(spx)
            data_ib.reqMarketDataType(1)  # Request real-time live streaming data

            for spot_attempt in range(1, max_conn_attempts + 1):
                spx_ticker = data_ib.reqMktData(spx, "", False, False)
                data_ib.sleep(2)
                ib_spot = spx_ticker.marketPrice()
                spot_source = "marketPrice"
                if math.isnan(ib_spot) or ib_spot <= 0:
                    if (
                        hasattr(spx_ticker, "last")
                        and not math.isnan(spx_ticker.last)
                        and spx_ticker.last > 0
                    ):
                        ib_spot = spx_ticker.last
                        spot_source = "last trade"
                    elif (
                        hasattr(spx_ticker, "close")
                        and not math.isnan(spx_ticker.close)
                        and spx_ticker.close > 0
                    ):
                        ib_spot = spx_ticker.close
                        spot_source = "previous close"

                if ib_spot is not None and not math.isnan(ib_spot) and ib_spot > 0:
                    print(
                        f"{get_ny_timestamp()} [*] Fetched live SPX spot from Real IBKR: {ib_spot:.2f} (via {spot_source})"
                    )
                    break
                else:
                    print(
                        f"{get_ny_timestamp()} [⚠️] Spot attempt {spot_attempt}/{max_conn_attempts} unavailable. Retrying..."
                    )
                    data_ib.sleep(1)
            else:
                reason = (
                    f"Failed to fetch live SPX spot from IBKR Real Account after {max_conn_attempts} attempts. "
                    "Aborting trade."
                )
                print(f"{get_ny_timestamp()} ❌ {reason}")
                msg = self._build_header(now_est) + f"\n❌ <b>Spot Price Error</b>\n{reason}"
                notifier.send_message(msg)
                sys.exit(1)

        df = fetcher.fetch_perfect_100_days("^SPX", "SPCFD:SPX", ibkr_spot=ib_spot)
        if df is None or df.empty:
            reason = "Failed to fetch 100 days of market data"
            print(f"{get_ny_timestamp()} [-] {reason}. Exiting.")
            msg = self._build_header(now_est) + f"\n❌ <b>Error</b>\n{reason}"
            notifier.send_message(msg)
            sys.exit(1)

        df["EMA20"] = df["Close"].ewm(span=20, adjust=False).mean()

        today_close = df["Close"].iloc[-1]
        today_open = df["Open"].iloc[-1]
        today_ema20 = df["EMA20"].iloc[-1]

        print(
            f"{get_ny_timestamp()} [*] Today's Close: {today_close:.2f} | Today's EMA20: {today_ema20:.2f}"
        )

        # 7. Strategy Logic Evaluation
        strategy_to_execute = None
        abort_reason = None

        if today_close > today_ema20:
            regime = "Bullish"
            print(f"{get_ny_timestamp()} [*] Bullish Regime detected.")
        elif today_close < today_ema20:
            regime = "Bearish"
            print(f"{get_ny_timestamp()} [*] Bearish Regime detected.")
        else:
            regime = "Neutral"
            print(
                f"{get_ny_timestamp()} [-] Neutral Regime detected (Close equals EMA20)."
            )

        if args.always_put_spread:
            print(
                f"{get_ny_timestamp()} [*] --always-put-spread enabled: executing Put Spread regardless of regime."
            )
            strategy_to_execute = "bull"
        elif regime == "Bullish":
            strategy_to_execute = "iron_condor" if args.iron_condor else "bull"
        elif regime == "Bearish":
            if not args.add_bear:
                abort_reason = (
                    "Bear Call Spread disabled (use --add-bear to enable)"
                )
                print(f"{get_ny_timestamp()} [!] {abort_reason}. Trade aborted.")
            elif today_close > today_open:
                abort_reason = f"Today is a bull bar (Close {today_close:.2f} > Open {today_open:.2f})"
                print(f"{get_ny_timestamp()} [!] {abort_reason}. Trade aborted.")
            else:
                strategy_to_execute = "bear"
        else:
            abort_reason = "Close equals EMA20 exactly — no trade signal"
            print(
                f"{get_ny_timestamp()} [-] Close equals EMA20 exactly. No trade executed."
            )

        if not strategy_to_execute:
            print(
                f"\n{get_ny_timestamp()} === No valid trade setup today. Exiting cleanly. ==="
            )
            msg = (
                self._build_header(now_est)
                + self._build_market_section(today_close, today_ema20, regime)
                + f"\n⚠️ <b>Trade Aborted</b>\n{abort_reason}"
            )
            notifier.send_message(msg)
            sys.exit(0)

        print(
            f"\n{get_ny_timestamp()} === Executing {strategy_to_execute.upper()} strategy ==="
        )

        underlying_spot = today_close * self.spot_multiplier
        iv_provider = IVProvider()

        # 8. Connect to Order Trading Gateway
        trading_ib = IB()
        try:
            print(
                f"{get_ny_timestamp()} [*] Connecting to Order Trading Gateway at {trading_host}:{trading_port} "
                f"(clientId={trading_client_id})..."
            )
            trading_ib.connect(trading_host, trading_port, clientId=trading_client_id, timeout=10)
            print(f"{get_ny_timestamp()} [+] Connected to Order Trading Gateway.")
        except Exception as e:
            reason = f"Failed to connect to Order Trading Gateway at {trading_host}:{trading_port}: {e}"
            print(f"{get_ny_timestamp()} [-] {reason}")
            msg = (
                self._build_header(now_est)
                + self._build_market_section(today_close, today_ema20, regime)
                + f"\n❌ <b>Trading Gateway Error</b>\n{reason}"
            )
            notifier.send_message(msg)
            sys.exit(1)

        try:
            # Option finder uses data_ib (real gateway for live data)
            if args.ib_market:
                finder = IbkrOptionFinder(
                    data_ib, max_model_market_diff=args.max_model_market_diff
                )
            else:
                finder = TheoryOptionFinder(data_ib)

            if strategy_to_execute == "iron_condor":
                strategy_label = "Iron Condor"
                print(
                    f"\n{get_ny_timestamp()} --- Finding Bull Put Spread legs ---"
                )
                sell_put, buy_put = self._find_spread_legs(
                    args,
                    finder,
                    iv_provider,
                    data_ib,
                    underlying_spot,
                    risk_free_rate,
                    "P",
                    args.bull_delta,
                )
                print(
                    f"\n{get_ny_timestamp()} --- Finding Bear Call Spread legs ---"
                )
                sell_call, buy_call = self._find_spread_legs(
                    args,
                    finder,
                    iv_provider,
                    data_ib,
                    underlying_spot,
                    risk_free_rate,
                    "C",
                    args.bear_delta,
                )

                # Trader uses trading_ib (submits to paper or real account)
                trader = IronCondorTrader(
                    trading_ib,
                    walk_step=args.walk_step,
                    walk_interval=args.walk_interval,
                    min_credit=args.min_credit,
                    quantity=args.quantity,
                )
                trade = trader.execute(
                    ticker_symbol=self.ticker_symbol,
                    sell_put_info=sell_put,
                    buy_put_info=buy_put,
                    sell_call_info=sell_call,
                    buy_call_info=buy_call,
                    trading_class=self.trading_class,
                )

                credit_label = (
                    "Market Credit" if args.ib_market else "Theo Credit"
                )
                credit_val = round(
                    (sell_put["theo_price"] - buy_put["theo_price"])
                    + (sell_call["theo_price"] - buy_call["theo_price"]),
                    2,
                )

                legs_section = (
                    f"  Sell Put:  {self.ticker_symbol} {sell_put['strike']}P @ Δ{sell_put['theo_delta']:.4f}\n"
                    f"  Buy Put:   {self.ticker_symbol} {buy_put['strike']}P @ Δ{buy_put['theo_delta']:.4f}\n"
                    f"  Sell Call: {self.ticker_symbol} {sell_call['strike']}C @ Δ{sell_call['theo_delta']:.4f}\n"
                    f"  Buy Call:  {self.ticker_symbol} {buy_call['strike']}C @ Δ{buy_call['theo_delta']:.4f}\n"
                    f"  {credit_label}: ${credit_val:.2f}\n"
                    f"  Qty: {args.quantity}\n"
                )
            else:
                option_type = "P" if strategy_to_execute == "bull" else "C"
                strategy_label = (
                    "Bull Put Spread"
                    if strategy_to_execute == "bull"
                    else "Bear Call Spread"
                )
                target_delta = (
                    args.bear_delta
                    if strategy_to_execute == "bear"
                    else args.bull_delta
                )

                sell_leg, buy_leg = self._find_spread_legs(
                    args,
                    finder,
                    iv_provider,
                    data_ib,
                    underlying_spot,
                    risk_free_rate,
                    option_type,
                    target_delta,
                )

                if strategy_to_execute == "bull":
                    trader = BullPutSpreadTrader(
                        trading_ib,
                        walk_step=args.walk_step,
                        walk_interval=args.walk_interval,
                        min_credit=args.min_credit,
                        quantity=args.quantity,
                    )
                    trade = trader.execute(
                        ticker_symbol=self.ticker_symbol,
                        sell_put_info=sell_leg,
                        buy_put_info=buy_leg,
                        trading_class=self.trading_class,
                    )
                else:
                    trader = BearCallSpreadTrader(
                        trading_ib,
                        walk_step=args.walk_step,
                        walk_interval=args.walk_interval,
                        min_credit=args.min_credit,
                        quantity=args.quantity,
                    )
                    trade = trader.execute(
                        ticker_symbol=self.ticker_symbol,
                        sell_call_info=sell_leg,
                        buy_call_info=buy_leg,
                        trading_class=self.trading_class,
                    )

                credit_label = (
                    "Market Credit" if args.ib_market else "Theo Credit"
                )
                credit_val = round(
                    sell_leg["theo_price"] - buy_leg["theo_price"], 2
                )

                legs_section = (
                    f"  Sell: {self.ticker_symbol} {sell_leg['strike']}{option_type} @ Δ{sell_leg['theo_delta']:.4f}\n"
                    f"  Buy:  {self.ticker_symbol} {buy_leg['strike']}{option_type} @ Δ{buy_leg['theo_delta']:.4f}\n"
                    f"  {credit_label}: ${credit_val:.2f}\n"
                    f"  Qty: {args.quantity}\n"
                )

            # Build Telegram report
            if trade and trade.orderStatus.status == "Filled":
                fill_price = abs(trade.orderStatus.avgFillPrice)
                total_credit_received = fill_price * 100 * args.quantity
                msg = (
                    self._build_header(now_est)
                    + self._build_market_section(
                        today_close, today_ema20, regime
                    )
                    + f"\n⚡ <b>Strategy Executed: {strategy_label} ({mode_label})</b>\n"
                    + f"<pre>{legs_section}</pre>"
                    + f"✅ <b>Order Filled!</b>\n"
                    + f"  Net Credit: ${fill_price:.2f}/share\n"
                    + f"  Total Premium Collected: ${total_credit_received:.2f}"
                )
            else:
                msg = (
                    self._build_header(now_est)
                    + self._build_market_section(
                        today_close, today_ema20, regime
                    )
                    + f"\n⚡ <b>Strategy Aborted: {strategy_label} ({mode_label})</b>\n"
                    + f"<pre>{legs_section}</pre>"
                    + "\nWalk-the-book aborted (credit dropped below minimum threshold or unfilled)."
                )
            notifier.send_message(msg)

        except Exception as e:
            tb = traceback.format_exc()
            reason = f"Execution error: {e}"
            print(f"{get_ny_timestamp()} [-] {reason}")
            print(tb)

            safe_reason = html.escape(str(reason))
            msg = (
                self._build_header(now_est)
                + self._build_market_section(today_close, today_ema20, regime)
                + f"\n❌ <b>Error</b>\n{safe_reason}"
            )
            notifier.send_message(msg)
            sys.exit(1)
        finally:
            if data_ib and data_ib.isConnected():
                data_ib.disconnect()
            if trading_ib and trading_ib.isConnected():
                trading_ib.disconnect()

    def _find_spread_legs(
        self,
        args,
        finder,
        iv_provider,
        ib,
        underlying_spot,
        risk_free_rate,
        option_type,
        target_delta,
    ):
        """Helper to find the delta-based sell leg and strike-offset buy leg."""
        sell_leg_info = finder.find_option(
            ticker_symbol=self.ticker_symbol,
            trading_class=self.trading_class,
            underlying_spot=underlying_spot,
            iv_provider=iv_provider,
            risk_free_rate=risk_free_rate,
            option_type=option_type,
            target_delta_abs=target_delta,
            dte_target=args.dte_target,
        )

        if args.ib_market:
            sell_leg_info["theo_price"] = sell_leg_info["market_price"]

        strike_offset = self.strike_offset * args.spread_step
        buy_strike = sell_leg_info["strike"] + (
            -strike_offset if option_type == "P" else strike_offset
        )

        T = calculate_trading_time_t(sell_leg_info["expiry"])

        # Fetch IV for buy leg
        buy_iv = sell_leg_info["theo_iv"]
        if args.ib_market:
            from ib_async import Option

            buy_contract = Option(
                self.ticker_symbol,
                sell_leg_info["expiry"],
                buy_strike,
                option_type,
                "SMART",
                tradingClass=self.trading_class,
            )
            ib.qualifyContracts(buy_contract)
            if buy_contract.conId:
                iv_data = ib.run(
                    iv_provider.get_iv(
                        ib, buy_contract, self.ticker_symbol, sell_leg_info["expiry"]
                    )
                )
                if iv_data.get("model_iv") is not None:
                    buy_iv = iv_data["model_iv"]
                elif iv_data.get("ask_iv") is not None:
                    buy_iv = iv_data["ask_iv"]
        else:
            # Theoretical mode: fetch per-leg IV from yfinance
            iv_data = iv_provider._get_iv_from_yfinance(
                symbol=self.ticker_symbol,
                strike=float(buy_strike),
                exp_date=sell_leg_info["expiry"],
                right=option_type,
            )
            if iv_data.get("model_iv") is not None:
                buy_iv = iv_data["model_iv"]

        q = 0.014 if self.ticker_symbol in ["SPX", "XSP"] else 0.0
        buy_theo_price = calculate_bs_price(
            underlying_spot, buy_strike, T, risk_free_rate, buy_iv, option_type, q=q
        )
        buy_theo_delta = calculate_bs_delta(
            underlying_spot, buy_strike, T, risk_free_rate, buy_iv, option_type, q=q
        )

        buy_leg_info = {
            "strike": buy_strike,
            "expiry": sell_leg_info["expiry"],
            "theo_price": buy_theo_price,
            "theo_delta": buy_theo_delta,
            "option_type": option_type,
            "theo_iv": buy_iv,
        }

        if args.ib_market:
            buy_market = finder.fetch_market_price(
                ticker_symbol=self.ticker_symbol,
                trading_class=self.trading_class,
                expiry=sell_leg_info["expiry"],
                strike=buy_strike,
                option_type=option_type,
                theo_price_fallback=buy_theo_price,
                underlying_spot=underlying_spot,
                risk_free_rate=risk_free_rate,
            )
            buy_leg_info["theo_price"] = buy_market["market_price"]
            if buy_market.get("market_delta") is not None:
                buy_leg_info["theo_delta"] = buy_market["market_delta"]
                buy_theo_delta = buy_market["market_delta"]
            if buy_market.get("market_iv") is not None:
                buy_leg_info["theo_iv"] = buy_market["market_iv"]
                buy_iv = buy_market["market_iv"]

        print(
            f"\n{get_ny_timestamp()} --- Buy leg: {strike_offset}-wide offset ---\n"
            f"  Sell strike: {sell_leg_info['strike']} | Buy strike: {buy_strike}\n"
            f"  Buy Price (Theo/Market): {buy_leg_info['theo_price']:.2f} | Buy Delta (Theo/Market): {buy_theo_delta:.4f} | IV: {buy_iv:.4f}"
        )
        return sell_leg_info, buy_leg_info
