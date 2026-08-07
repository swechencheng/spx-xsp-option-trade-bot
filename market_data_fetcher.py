import requests
import yfinance as yf
import pandas as pd
from datetime import datetime
import warnings
import time

# Suppress yfinance timezone warnings for cleaner console output
warnings.filterwarnings("ignore", category=FutureWarning)


class MarketDataFetcher:
    def __init__(self):
        self.tv_scanner_url = "https://scanner.tradingview.com/global/scan"
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Content-Type": "application/json",
        }

    def get_risk_free_rate(self):
        """从 Yahoo Finance 抓取美国 13 周国库券收益率 (^IRX) 作为无风险利率"""
        try:
            time.sleep(1)  # Prevent yfinance rate limit
            irx = yf.Ticker("^IRX")
            hist = irx.history(period="1d")
            if not hist.empty:
                rate = float(hist["Close"].iloc[-1]) / 100
                print(
                    f"[*] Fetched live risk-free rate (^IRX): {rate:.4f} ({rate*100:.2f}%)"
                )
                return rate
        except Exception as e:
            print(f"[-] Failed to fetch live risk-free rate, using default 4.2%: {e}")
        return 0.042

    def fetch_tradingview_live(self, tv_ticker):
        """
        Fetches the real-time daily OHLC snapshot from TradingView.
        """
        payload = {
            "symbols": {"tickers": [tv_ticker]},
            "columns": ["name", "open", "high", "low", "close"],
        }

        try:
            response = requests.post(
                self.tv_scanner_url, json=payload, headers=self.headers, timeout=10
            )
            response.raise_for_status()
            data = response.json()

            if "data" in data and len(data["data"]) > 0:
                values = data["data"][0]["d"]
                return {
                    "Open": values[1],
                    "High": values[2],
                    "Low": values[3],
                    "Close": values[4],
                }
            return None
        except Exception as e:
            print(f"Error fetching live data for {tv_ticker}: {e}")
            return None

    def fetch_perfect_100_days(self, yf_ticker, tv_ticker, ibkr_spot=None):
        """
        Combines 99 days of YF history with today's live snapshot (from IBKR or TradingView).
        """
        print(f"--- Processing {yf_ticker} / {tv_ticker} ---")

        # 1. Fetch historical data from Yahoo Finance (grab extra days to account for weekends/holidays)
        print(f"[*] Downloading history for {yf_ticker} from Yahoo Finance...")
        time.sleep(1)  # Prevent yfinance rate limit
        ticker_obj = yf.Ticker(yf_ticker)
        history_df = ticker_obj.history(period="6mo")

        if history_df.empty:
            print(f"[!] Failed to fetch history for {yf_ticker}")
            return None

        # Standardize the index to timezone-naive dates
        history_df.index = history_df.index.tz_localize(None).normalize()

        # Remove today's row if YF already generated a partial daily candle
        today_date = pd.Timestamp.today().normalize()
        if today_date in history_df.index:
            history_df = history_df.drop(index=today_date)

        # Keep only the last 99 completed trading days and the required columns
        past_99_days = history_df.iloc[-99:][["Open", "High", "Low", "Close"]]

        # 2. Fetch the real-time snapshot for today
        if ibkr_spot is not None and not pd.isna(ibkr_spot):
            print(f"[*] Using live IBKR spot price: {ibkr_spot}")
            live_snapshot = {
                "Open": ibkr_spot,
                "High": ibkr_spot,
                "Low": ibkr_spot,
                "Close": ibkr_spot,
            }
        elif tv_ticker:
            print(f"[*] Fetching live snapshot for {tv_ticker} from TradingView...")
            live_snapshot = self.fetch_tradingview_live(tv_ticker)
        else:
            live_snapshot = None

        if live_snapshot:
            # 3. Combine history with the live snapshot
            today_df = pd.DataFrame([live_snapshot], index=[today_date])
            final_df = pd.concat([past_99_days, today_df])
            print(
                f"[+] Successfully generated 100-day dataset (99 historical + 1 live)."
            )
            return final_df
        else:
            print(f"[-] Live fetch failed. Returning 99 days of historical data only.")
            return past_99_days
