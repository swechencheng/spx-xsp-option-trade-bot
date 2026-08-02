import sys
import argparse
import traceback
from datetime import datetime
import pytz
import html
import pandas as pd
import yfinance as yf
from ib_async import IB

from market_data_fetcher import MarketDataFetcher
from xsp_option_finder_theory import (
    OptionFinder as TheoryOptionFinder,
    calculate_bs_price,
    calculate_bs_delta,
    calculate_trading_time_t,
)
from xsp_option_finder_ibkr import OptionFinder as IbkrOptionFinder
from credit_spread_trader import BullPutSpreadTrader, BearCallSpreadTrader
from telegram_notifier import TelegramNotifier
from iv_provider import IVProvider


def count_consecutive_true(series: pd.Series) -> int:
    """Counts consecutive True values from the end of a boolean pandas Series."""
    count = 0
    for val in series.iloc[::-1]:
        if val:
            count += 1
        else:
            break
    return count


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

        print(f"[*] YF Latest Date: {latest_date} | Today EST: {today_est}")
        return latest_date == today_est
    except Exception as e:
        print(f"[-] Error checking trading day: {e}")
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
            "--ib-host",
            type=str,
            default="127.0.0.1",
            help="IBKR Host (default: 127.0.0.1)",
        )
        parser.add_argument(
            "--ib-port", type=int, default=7497, help="IBKR Port (default: 7497)"
        )
        parser.add_argument(
            "--client-id", type=int, default=15, help="IBKR Client ID (default: 15)"
        )
        parser.add_argument(
            "--bull-delta",
            type=float,
            default=0.20,
            help="Sell leg target delta for Bull Put Spread (default: 0.20)",
        )
        parser.add_argument(
            "--bear-delta",
            type=float,
            default=0.09,
            help="Sell leg target delta for Bear Call Spread (default: 0.09)",
        )
        parser.add_argument(
            "--dte-target",
            type=int,
            default=1,
            help="Target days-to-expiration (default: 1)",
        )
        parser.add_argument(
            "--max-ema-gap",
            type=int,
            default=20,
            help="Max continuous days for EMA gap (default: 20)",
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
            default=10,
            help="Seconds to wait between fill checks (default: 10)",
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
        args = parser.parse_args()

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
            f" {self.ticker_symbol} Automated Trading Bot - Started at {datetime.now()}"
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
            print(f"[!] {reason}. Exiting.")
            msg = self._build_header(now_est) + f"\n📅 <b>No Trade</b>\n{reason}"
            notifier.send_message(msg)
            sys.exit(0)

        # 2. Fetch Market Data & Calculate EMA20
        fetcher = MarketDataFetcher()
        risk_free_rate = fetcher.get_risk_free_rate()

        df = fetcher.fetch_perfect_100_days("^SPX", "SPCFD:SPX")
        if df is None or df.empty:
            reason = "Failed to fetch 100 days of market data"
            print(f"[-] {reason}. Exiting.")
            msg = self._build_header(now_est) + f"\n❌ <b>Error</b>\n{reason}"
            notifier.send_message(msg)
            sys.exit(1)

        df["EMA20"] = df["Close"].ewm(span=20, adjust=False).mean()

        today_close = df["Close"].iloc[-1]
        today_open = df["Open"].iloc[-1]
        today_ema20 = df["EMA20"].iloc[-1]
        yesterday_close = df["Close"].iloc[-2]
        yesterday_ema20 = df["EMA20"].iloc[-2]

        print(
            f"[*] Today's Close: {today_close:.2f} | Today's EMA20: {today_ema20:.2f}"
        )

        # 3. Strategy Logic Evaluation
        strategy_to_execute = None
        abort_reason = None

        if today_close > today_ema20:
            regime = "Bullish"
            gap_streak = count_consecutive_true(df["Low"] > df["EMA20"])
            print(f"[*] Bullish Regime detected. EMA Gap streak: {gap_streak} days.")

            if gap_streak > args.max_ema_gap:
                abort_reason = (
                    f"Overextended uptrend (gap {gap_streak} > {args.max_ema_gap} days)"
                )
                print(f"[!] {abort_reason}. Trade aborted.")
            else:
                strategy_to_execute = "bull"

        elif today_close < today_ema20:
            regime = "Bearish"
            print("[*] Bearish Regime detected.")

            if not args.add_bear:
                abort_reason = "Bear Call Spread disabled (use --add-bear to enable)"
                print(f"[!] {abort_reason}. Trade aborted.")
            elif today_close > today_open:
                abort_reason = f"Today is a bull bar (Close {today_close:.2f} > Open {today_open:.2f})"
                print(f"[!] {abort_reason}. Trade aborted.")
            else:
                gap_streak = count_consecutive_true(df["High"] < df["EMA20"])
                print(f"[*] EMA Gap streak: {gap_streak} days.")
                if gap_streak > args.max_ema_gap:
                    abort_reason = f"Overextended downtrend (gap {gap_streak} > {args.max_ema_gap} days)"
                    print(f"[!] {abort_reason}. Trade aborted.")
                else:
                    strategy_to_execute = "bear"
        else:
            regime = "Neutral"
            abort_reason = "Close equals EMA20 exactly — no trade signal"
            print("[-] Close equals EMA20 exactly. No trade executed.")

        if not strategy_to_execute:
            print("\n=== No valid trade setup today. Exiting cleanly. ===")
            msg = (
                self._build_header(now_est)
                + self._build_market_section(today_close, today_ema20, regime)
                + f"\n⚠️ <b>Trade Aborted</b>\n{abort_reason}"
            )
            notifier.send_message(msg)
            sys.exit(0)

        print(f"\n=== Executing {strategy_to_execute.upper()} strategy ===")

        underlying_spot = today_close * self.spot_multiplier
        iv_provider = IVProvider()

        print("\n--- Connecting to IBKR ---")
        ib = IB()
        try:
            ib.connect(args.ib_host, args.ib_port, clientId=args.client_id)
        except Exception as e:
            reason = f"Failed to connect to IBKR: {e}"
            print(f"[-] {reason}")
            msg = (
                self._build_header(now_est)
                + self._build_market_section(today_close, today_ema20, regime)
                + f"\n❌ <b>Error</b>\n{reason}"
            )
            notifier.send_message(msg)
            sys.exit(1)

        try:
            if args.ib_market:
                finder = IbkrOptionFinder(ib)
            else:
                finder = TheoryOptionFinder(ib)

            option_type = "P" if strategy_to_execute == "bull" else "C"
            strategy_label = (
                "Bull Put Spread"
                if strategy_to_execute == "bull"
                else "Bear Call Spread"
            )

            target_delta = (
                args.bear_delta if strategy_to_execute == "bear" else args.bull_delta
            )

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

            strike_offset = self.strike_offset
            buy_strike = sell_leg_info["strike"] + (
                -strike_offset if option_type == "P" else strike_offset
            )

            T = calculate_trading_time_t(sell_leg_info["expiry"])
            chain_iv = sell_leg_info["theo_iv"]
            buy_theo_price = calculate_bs_price(
                underlying_spot, buy_strike, T, risk_free_rate, chain_iv, option_type
            )
            buy_theo_delta = calculate_bs_delta(
                underlying_spot, buy_strike, T, risk_free_rate, chain_iv, option_type
            )

            buy_leg_info = {
                "strike": buy_strike,
                "expiry": sell_leg_info["expiry"],
                "theo_price": buy_theo_price,
                "theo_delta": buy_theo_delta,
                "option_type": option_type,
            }

            if args.ib_market:
                buy_market = finder.fetch_market_price(
                    ticker_symbol=self.ticker_symbol,
                    trading_class=self.trading_class,
                    expiry=sell_leg_info["expiry"],
                    strike=buy_strike,
                    option_type=option_type,
                    theo_price_fallback=buy_theo_price,
                )
                buy_leg_info["theo_price"] = buy_market["market_price"]

            print(
                f"\n--- Buy leg: {strike_offset}-wide offset ---\n"
                f"  Sell strike: {sell_leg_info['strike']} | Buy strike: {buy_strike}\n"
                f"  Buy Theo Price: {buy_theo_price:.2f} | Buy Theo Delta: {buy_theo_delta:.4f}"
            )

            if strategy_to_execute == "bull":
                trader = BullPutSpreadTrader(
                    ib,
                    walk_step=args.walk_step,
                    walk_interval=args.walk_interval,
                    min_credit=args.min_credit,
                    quantity=args.quantity,
                )
                trade = trader.execute(
                    ticker_symbol=self.ticker_symbol,
                    sell_put_info=sell_leg_info,
                    buy_put_info=buy_leg_info,
                    trading_class=self.trading_class,
                )
            else:
                trader = BearCallSpreadTrader(
                    ib,
                    walk_step=args.walk_step,
                    walk_interval=args.walk_interval,
                    min_credit=args.min_credit,
                    quantity=args.quantity,
                )
                trade = trader.execute(
                    ticker_symbol=self.ticker_symbol,
                    sell_call_info=sell_leg_info,
                    buy_call_info=buy_leg_info,
                    trading_class=self.trading_class,
                )

            credit_label = "Market Credit" if args.ib_market else "Theo Credit"
            credit_val = round(
                sell_leg_info["theo_price"] - buy_leg_info["theo_price"], 2
            )

            legs_section = (
                f"  Sell: {self.ticker_symbol} {sell_leg_info['strike']}{option_type}"
                f" @ Δ{sell_leg_info['theo_delta']:.4f}\n"
                f"  Buy:  {self.ticker_symbol} {buy_leg_info['strike']}{option_type}"
                f" @ Δ{buy_leg_info['theo_delta']:.4f}\n"
                f"  {credit_label}: ${credit_val:.2f}\n"
                f"  Qty: {args.quantity}\n"
            )

            if trade is not None:
                fill_price = abs(trade.orderStatus.avgFillPrice)
                msg = (
                    self._build_header(now_est)
                    + self._build_market_section(today_close, today_ema20, regime)
                    + f"\n✅ <b>{strategy_label} — FILLED</b>\n"
                    + f"<pre>{legs_section}"
                    + f"  Fill: ${fill_price:.2f}</pre>"
                )
            else:
                msg = (
                    self._build_header(now_est)
                    + self._build_market_section(today_close, today_ema20, regime)
                    + f"\n⛔ <b>{strategy_label} — NOT FILLED</b>\n"
                    + f"<pre>{legs_section}</pre>"
                    + "Order cancelled (credit below minimum or rejected)."
                )

            notifier.send_message(msg)

        finally:
            ib.disconnect()
            print("\nSafely disconnected from IBKR.")
