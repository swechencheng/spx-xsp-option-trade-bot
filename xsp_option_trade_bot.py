import sys
import argparse
from datetime import datetime
import pytz
import pandas as pd
import yfinance as yf
from ib_async import IB

from market_data_fetcher import MarketDataFetcher
from xsp_option_finder_theory import OptionFinder
from xsp_option_trader import BullPutSpreadTrader, BearCallSpreadTrader


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
        help="Sell leg target delta (default: 0.20)",
    )
    parser.add_argument(
        "--low-delta",
        type=float,
        default=0.06,
        help="Buy leg target delta (default: 0.06)",
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
        default=0.03,
        help="Credit reduction per repricing round (default: 0.03)",
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

    args = parser.parse_args()

    print("=" * 60)
    print(f" XSP Automated Trading Bot - Started at {datetime.now()}")
    print("=" * 60)

    # 1. Validation: Ensure it's a trading day
    if not is_valid_trading_day():
        print(
            "[!] Today is not a valid trading day (market closed or holiday). Exiting."
        )
        sys.exit(0)

    # 2. Fetch Market Data & Calculate EMA20
    fetcher = MarketDataFetcher()
    risk_free_rate = fetcher.get_risk_free_rate()

    df = fetcher.fetch_perfect_100_days("^SPX", "SPCFD:SPX")
    if df is None or df.empty:
        print("[-] Failed to fetch 100 days of data. Exiting.")
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

    if today_close > today_ema20:
        # Bull Put Credit Spread Candidate
        gap_streak = count_consecutive_true(df["Low"] > df["EMA20"])
        print(f"[*] Bullish Regime detected. EMA Gap streak: {gap_streak} days.")

        if gap_streak > args.max_ema_gap:
            print(
                f"[!] Overextended uptrend (gap > {args.max_ema_gap} days). Trade aborted."
            )
        else:
            strategy_to_execute = "bull"

    elif today_close < today_ema20:
        # Bear Call Credit Spread Candidate
        print("[*] Bearish Regime detected.")

        # Condition 1: Must be below EMA20 for at least 1 day (yesterday was also below)
        if yesterday_close >= yesterday_ema20:
            print(
                "[!] Close just crossed below EMA20 today (no 1-day confirmation). Trade aborted."
            )

        # Condition 2: Not a bull bar (Close <= Open)
        elif today_close > today_open:
            print(
                f"[!] Today is a bull bar (Close {today_close:.2f} > Open {today_open:.2f}). Trade aborted."
            )

        else:
            # Condition 3: EMA20 gap (High < EMA20) continuous for > 20 days
            gap_streak = count_consecutive_true(df["High"] < df["EMA20"])
            print(f"[*] EMA Gap streak: {gap_streak} days.")
            if gap_streak > args.max_ema_gap:
                print(
                    f"[!] Overextended downtrend (gap > {args.max_ema_gap} days). Trade aborted."
                )
            else:
                strategy_to_execute = "bear"
    else:
        print("[-] Close equals EMA20 exactly. No trade executed.")

    # 4. Execute Trade if conditions are met
    if not strategy_to_execute:
        print("\n=== No valid trade setup today. Exiting cleanly. ===")
        sys.exit(0)

    print(f"\n=== Executing {strategy_to_execute.upper()} strategy ===")

    # Get live VIX
    vix_data = fetcher.fetch_tradingview_live(tv_ticker="TVC:VIX")
    if vix_data is None:
        print("[-] Failed to fetch live VIX data. Exiting.")
        sys.exit(1)

    xsp_spot = today_close / 10.0
    iv = vix_data["Close"] / 100.0

    print("\n--- Connecting to IBKR ---")
    ib = IB()
    try:
        ib.connect(args.ib_host, args.ib_port, clientId=args.client_id)
    except Exception as e:
        print(f"[-] Failed to connect to IBKR: {e}")
        sys.exit(1)

    try:
        finder = OptionFinder(ib)
        option_type = "P" if strategy_to_execute == "bull" else "C"

        sell_leg_info = finder.find_option(
            ticker_symbol="XSP",
            xsp_spot=xsp_spot,
            iv=iv,
            risk_free_rate=risk_free_rate,
            option_type=option_type,
            target_delta_abs=args.high_delta,
            dte_target=args.dte_target,
        )

        buy_leg_info = finder.find_option(
            ticker_symbol="XSP",
            xsp_spot=xsp_spot,
            iv=iv,
            risk_free_rate=risk_free_rate,
            option_type=option_type,
            target_delta_abs=args.low_delta,
            dte_target=args.dte_target,
        )

        if strategy_to_execute == "bull":
            trader = BullPutSpreadTrader(
                ib,
                walk_step=args.walk_step,
                walk_interval=args.walk_interval,
                min_credit=args.min_credit,
                quantity=args.quantity,
            )
            trader.execute(
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
            trader.execute(
                ticker_symbol="XSP",
                sell_call_info=sell_leg_info,
                buy_call_info=buy_leg_info,
            )

    finally:
        ib.disconnect()
        print("\nSafely disconnected from IBKR.")


if __name__ == "__main__":
    main()
