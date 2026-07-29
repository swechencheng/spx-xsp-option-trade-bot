import sys
import argparse
import traceback
from datetime import datetime
import pytz
import pandas as pd
import yfinance as yf
from ib_async import IB

from market_data_fetcher import MarketDataFetcher
from xsp_option_finder_theory import (
    OptionFinder as TheoryOptionFinder,
    calculate_bs_price,
    calculate_bs_delta,
)
from xsp_option_finder_ibkr import OptionFinder as IbkrOptionFinder
from xsp_option_trader import BullPutSpreadTrader, BearCallSpreadTrader
from telegram_notifier import TelegramNotifier


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


def _build_header(now_est: datetime) -> str:
    """Build the common Telegram message header."""
    return (
        "🤖 <b>XSP Trading Bot Report</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 {now_est.strftime('%Y-%m-%d %H:%M %Z')}\n"
    )


def _build_market_section(today_close: float, today_ema20: float, regime: str) -> str:
    """Build the market data section of the Telegram message."""
    return (
        f"📈 SPX Close: {today_close:.2f}\n"
        f"📊 EMA20: {today_ema20:.2f}\n"
        f"📌 Regime: {regime}\n"
    )


def main():
    parser = argparse.ArgumentParser(description="XSP Automated Options Trading Bot")
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
        "--high-delta",
        type=float,
        default=0.20,
        help="Sell leg target delta for Bull Put Spread (default: 0.20)",
    )
    parser.add_argument(
        "--low-delta",
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
        default=0.01,
        help="Credit reduction per repricing round (default: 0.01)",
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
    now_est = datetime.now(pytz.timezone("US/Eastern"))

    # Initialize Telegram notifier (never crashes on failure)
    notifier = TelegramNotifier()

    print("=" * 60)
    print(f" XSP Automated Trading Bot - Started at {datetime.now()}")
    print("=" * 60)

    try:
        _run_strategy(args, now_est, notifier)
    except Exception as e:
        # Catch-all for any unexpected exception
        tb = traceback.format_exc()
        print(f"[!] Unexpected exception:\n{tb}")
        msg = (
            _build_header(now_est)
            + "\n🔥 <b>UNEXPECTED EXCEPTION</b>\n"
            + f"<pre>{tb[-500:]}</pre>"
        )
        notifier.send_message(msg)
        sys.exit(1)


def _run_strategy(args, now_est: datetime, notifier: TelegramNotifier):
    """Core strategy logic, separated for clean error handling."""

    # 1. Validation: Ensure it's a trading day
    if not is_valid_trading_day():
        reason = "Not a valid trading day (market closed or holiday)"
        print(f"[!] {reason}. Exiting.")
        msg = _build_header(now_est) + f"\n📅 <b>No Trade</b>\n{reason}"
        notifier.send_message(msg)
        sys.exit(0)

    # 2. Fetch Market Data & Calculate EMA20
    fetcher = MarketDataFetcher()
    risk_free_rate = fetcher.get_risk_free_rate()

    df = fetcher.fetch_perfect_100_days("^SPX", "SPCFD:SPX")
    if df is None or df.empty:
        reason = "Failed to fetch 100 days of market data"
        print(f"[-] {reason}. Exiting.")
        msg = _build_header(now_est) + f"\n❌ <b>Error</b>\n{reason}"
        notifier.send_message(msg)
        sys.exit(1)

    # Calculate 20-day EMA
    df["EMA20"] = df["Close"].ewm(span=20, adjust=False).mean()

    # Get today's and yesterday's metrics
    today_close = df["Close"].iloc[-1]
    today_open = df["Open"].iloc[-1]
    today_ema20 = df["EMA20"].iloc[-1]

    yesterday_close = df["Close"].iloc[-2]
    yesterday_ema20 = df["EMA20"].iloc[-2]

    print(f"[*] Today's Close: {today_close:.2f} | Today's EMA20: {today_ema20:.2f}")

    # 3. Strategy Logic Evaluation
    strategy_to_execute = None
    abort_reason = None

    if today_close > today_ema20:
        # Bull Put Credit Spread Candidate
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
        # Bear Call Credit Spread Candidate
        regime = "Bearish"
        print("[*] Bearish Regime detected.")

        if not args.add_bear:
            abort_reason = "Bear Call Spread disabled (use --add-bear to enable)"
            print(f"[!] {abort_reason}. Trade aborted.")

        elif today_close > today_open:
            abort_reason = (
                f"Today is a bull bar (Close {today_close:.2f} > Open {today_open:.2f})"
            )
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

    # 4. If no valid setup, notify and exit
    if not strategy_to_execute:
        print("\n=== No valid trade setup today. Exiting cleanly. ===")
        msg = (
            _build_header(now_est)
            + _build_market_section(today_close, today_ema20, regime)
            + f"\n⚠️ <b>Trade Aborted</b>\n{abort_reason}"
        )
        notifier.send_message(msg)
        sys.exit(0)

    print(f"\n=== Executing {strategy_to_execute.upper()} strategy ===")

    # Get live VIX
    vix_data = fetcher.fetch_tradingview_live(tv_ticker="TVC:VIX")
    if vix_data is None:
        reason = "Failed to fetch live VIX data"
        print(f"[-] {reason}. Exiting.")
        msg = (
            _build_header(now_est)
            + _build_market_section(today_close, today_ema20, regime)
            + f"\n❌ <b>Error</b>\n{reason}"
        )
        notifier.send_message(msg)
        sys.exit(1)

    xsp_spot = today_close / 10.0
    iv = vix_data["Close"] / 100.0

    print("\n--- Connecting to IBKR ---")
    ib = IB()
    try:
        ib.connect(args.ib_host, args.ib_port, clientId=args.client_id)
    except Exception as e:
        reason = f"Failed to connect to IBKR: {e}"
        print(f"[-] {reason}")
        msg = (
            _build_header(now_est)
            + _build_market_section(today_close, today_ema20, regime)
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
            "Bull Put Spread" if strategy_to_execute == "bull" else "Bear Call Spread"
        )

        target_delta = (
            args.low_delta if strategy_to_execute == "bear" else args.high_delta
        )

        sell_leg_info = finder.find_option(
            ticker_symbol="XSP",
            xsp_spot=xsp_spot,
            iv=iv,
            risk_free_rate=risk_free_rate,
            option_type=option_type,
            target_delta_abs=target_delta,
            dte_target=args.dte_target,
        )

        if args.ib_market:
            # Overwrite 'theo_price' with 'market_price' so trader module uses market pricing seamlessly
            sell_leg_info["theo_price"] = sell_leg_info["market_price"]

        # Buy leg = one strike step away from sell leg (1-wide spread)
        # Puts: buy a lower strike put (further OTM protection)
        # Calls: buy a higher strike call (further OTM protection)
        buy_strike = sell_leg_info["strike"] + (-1 if option_type == "P" else 1)

        # Calculate real theoretical price and delta for the buy leg
        T = max(args.dte_target, 1) / 252.0
        buy_theo_price = calculate_bs_price(
            xsp_spot, buy_strike, T, risk_free_rate, iv, option_type
        )
        buy_theo_delta = calculate_bs_delta(
            xsp_spot, buy_strike, T, risk_free_rate, iv, option_type
        )

        buy_leg_info = {
            "strike": buy_strike,
            "expiry": sell_leg_info["expiry"],
            "theo_price": buy_theo_price,
            "theo_delta": buy_theo_delta,
            "option_type": option_type,
        }

        if args.ib_market:
            # Fetch real market price for the buy leg via regulatory snapshot
            buy_market = finder.fetch_market_price(
                ticker_symbol="XSP",
                expiry=sell_leg_info["expiry"],
                strike=buy_strike,
                option_type=option_type,
                theo_price_fallback=buy_theo_price,
            )
            buy_leg_info["theo_price"] = buy_market["market_price"]

        print(
            f"\n--- Buy leg: 1-wide offset ---\n"
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
                ticker_symbol="XSP",
                sell_put_info=sell_leg_info,
                buy_put_info=buy_leg_info,
            )
        elif strategy_to_execute == "bear":
            trader = BearCallSpreadTrader(
                ib,
                walk_step=args.walk_step,
                walk_interval=args.walk_interval,
                min_credit=args.min_credit,
                quantity=args.quantity,
            )
            trade = trader.execute(
                ticker_symbol="XSP",
                sell_call_info=sell_leg_info,
                buy_call_info=buy_leg_info,
            )

        # Build trade result message
        credit_label = "Market Credit" if args.ib_market else "Theo Credit"
        credit_val = round(sell_leg_info["theo_price"] - buy_leg_info["theo_price"], 2)

        legs_section = (
            f"  Sell: XSP {sell_leg_info['strike']}{option_type}"
            f" @ Δ{sell_leg_info['theo_delta']:.4f}\n"
            f"  Buy:  XSP {buy_leg_info['strike']}{option_type}"
            f" @ Δ{buy_leg_info['theo_delta']:.4f}\n"
            f"  {credit_label}: ${credit_val:.2f}\n"
            f"  Qty: {args.quantity}\n"
        )

        if trade is not None:
            fill_price = abs(trade.orderStatus.avgFillPrice)
            msg = (
                _build_header(now_est)
                + _build_market_section(today_close, today_ema20, regime)
                + f"\n✅ <b>{strategy_label} — FILLED</b>\n"
                + f"<pre>{legs_section}"
                + f"  Fill: ${fill_price:.2f}</pre>"
            )
        else:
            msg = (
                _build_header(now_est)
                + _build_market_section(today_close, today_ema20, regime)
                + f"\n⛔ <b>{strategy_label} — NOT FILLED</b>\n"
                + f"<pre>{legs_section}</pre>"
                + "Order cancelled (credit below minimum or rejected)."
            )

        notifier.send_message(msg)

    finally:
        ib.disconnect()
        print("\nSafely disconnected from IBKR.")


if __name__ == "__main__":
    main()
