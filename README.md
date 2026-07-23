# XSP Automated Options Trading Bot

> [!CAUTION]
> **Disclaimer & Caveat**: This software is strictly for **educational and personal study purposes only**. It is **NOT** intended for commercial use, and it does **NOT** constitute financial advice. Options trading involves significant risk of loss. The author assumes no liability for any financial losses incurred. Use entirely at your own risk.

An end-to-end automated trading bot for Interactive Brokers (IBKR) that trades next-day (1DTE) Credit Spreads on the XSP (Mini-SPX) index.

This bot analyzes live market data, calculates theoretical option prices using the Black-Scholes model, automatically identifies the optimal sell strike based on target delta, and constructs a 1-wide credit spread by placing the buy leg one strike away from the sell leg. It then executes the trade using a "Walk-the-Book" algorithm to ensure the best possible fill price.

## Table of Contents

- [How It Works](#how-it-works)
- [The Mechanism](#the-mechanism)
- [Why Is This Strategy Profitable?](#why-is-this-strategy-profitable)
- [Backtest Results (2020–2025)](#backtest-results-20202025)
- [Architecture & Modules](#architecture--modules)
- [Installation & Setup](#installation--setup)
- [Usage & Automation](#usage--automation)
- [License](#license)

---

## How It Works

The bot runs on a daily schedule, specifically timed for **3:55 PM EST** (just before market close). It evaluates the overall trend of the S&P 500 using the **20-day Exponential Moving Average (EMA20)**.

Depending on the market regime, it executes one of two strategies:

1. **Bullish Regime (Close > EMA20)**: Executes a **Bull Put Credit Spread**.
2. **Bearish Regime (Close < EMA20)**: Executes a **Bear Call Credit Spread** _(disabled by default; enable with `--add-bear`)_.

The bot is strictly defensive. It will **abort** the trade under the following conditions:

- **Overextended Trends**: If the price has gapped completely above or below the EMA20 for more than 20 consecutive days, the bot pauses trading to avoid mean-reversion whipsaws.
- **Bull Bar Filter**: For a bearish setup, the current daily bar cannot be bullish (`Close > Open`).
- **Non-Trading Days**: The bot uses Yahoo Finance to verify if the current date is a valid US trading day. If it's a weekend or holiday, it gracefully exits.

---

## The Mechanism

When a valid trend is detected, the bot performs the following steps:

1. **Live Data Fetching**: Pulls high-precision live SPX and VIX snapshots from TradingView and combines them with historical data from Yahoo Finance.
2. **Option Discovery (Black-Scholes)**: Instead of requesting delayed option chains from IBKR, the bot uses the live VIX and SPX prices to calculate theoretical option prices using the Black-Scholes equation. It scans the theoretical chain to find:
   - A "Sell" leg with an absolute Delta of **0.20**
   - A "Buy" leg placed **one strike ($1) away** from the sell leg, creating a 1-wide spread
   - **Holiday-Aware Expiration Selection**: Instead of blindly adding calendar days, the bot downloads the list of live expirations directly from CBOE and sorts them. This allows it to perfectly calculate the next _trading day_ for 1DTE expirations, automatically skipping weekends and exchange holidays.
3. **Execution (Walk the Book)**:
   - The bot connects to the IBKR TWS/Gateway API.
   - It constructs a `BAG` combo order (a multi-leg spread).
   - It submits an initial Limit Order at the theoretical mid-price (maximizing credit).
   - If the order isn't filled within 5 seconds, it automatically cancels, reduces the credit required by $0.01, and resubmits. This "Walk the Book" process repeats until the order fills or the credit drops below the minimum acceptable threshold ($0.09).

---

## Why Is This Strategy Profitable?

This strategy relies on the core mathematical advantages of option selling:

1. **High Probability of Success**: By selling options at the 0.20 Delta, there is an approximate **80% statistical probability** that the sold option will expire Out-Of-The-Money (OOTM) and be completely worthless.
2. **Rapid Theta Decay**: Trading options close to expiration (1DTE) means that the time value of the option decays exponentially fast.
3. **Trend Following**: By filtering trades using the EMA20, the bot ensures you are always trading _with_ the broader market momentum. You are placing bets that the market will not sharply reverse against the current short-term trend.
4. **Defined Risk**: Buying the option one strike away from the sell leg creates a tight 1-wide "Credit Spread." This caps your maximum potential loss to just $1 per spread (minus the credit received), making the strategy highly capital efficient.

---

## Potential Risks

While highly probable, this strategy is not without risks. You must be aware of the market conditions where it can lose money:

1. **Sharp Mean Reversions (Whipsaws)**: The strategy uses the EMA20 to trade _with_ the trend. If the market is chopping sideways or experiences a violent intraday reversal (e.g., a sudden 1-2% drop after an uptrend), the underlying index can quickly crash through your short strike.
2. **High Gamma (Pin Risk)**: Short-duration options (1DTE) have extremely high Gamma. This means that if the index gets close to your strike price on expiration day, the delta will change very rapidly. A small index movement can instantly turn a safe position into a max-loss position.
3. **Overnight Gap Risk**: Because the strategy executes at 3:55 PM for expiration on the next trading day, holding the position overnight exposes you to gap risk. Unforeseen macroeconomic news (CPI drops, Fed announcements, or geopolitical events) can cause the market to gap open the next morning far past your strike prices, leaving no room to manage the trade.
4. **Asymmetric Risk/Reward**: Because you are trading high-probability setups (selling 0.20 Deltas) with tight 1-wide spreads, the premium you collect is small relative to the maximum possible loss ($1 spread width). A single max-loss event can wipe out the profits of several successful trades.

---

## Backtest Results (2020–2025)

We backtested the core assumption of this strategy against **1,507 trading days** of SPX daily data from January 2020 to December 2025. The test checks whether, given the EMA20 regime on day _i_, the index stays within a **50-point buffer** on day _i+1_ — simulating whether a 0.20 Delta credit spread (approximately 50 SPX points OTM) would have expired safely.

### Bullish Regime (Close > EMA20)

| Period    | Total Signals | ✅ Pass | ❌ Fail | Win Rate  |
| --------- | :-----------: | :-----: | :-----: | :-------: |
| 2020–2025 |     1,044     |   956   |   88    | **91.6%** |
| 2025 only |      180      |   165   |   15    | **91.7%** |

### Bearish Regime (Close < EMA20)

We tested three variants of the bearish entry filter:

| Variant                                  | Signals | ✅ Pass | ❌ Fail | Win Rate |
| ---------------------------------------- | :-----: | :-----: | :-----: | :------: |
| Simple (Close < EMA20 only)              |   462   |   346   |   116   |  74.9%   |
| Bear bar (+ Close < Open)                |   289   |   218   |   71    |  75.4%   |
| Strict (+ bear bar + 1-day confirmation) |   209   |   151   |   58    |  72.2%   |

### Conclusion

The bullish side delivers a consistent **~92% win rate** across all time scales, making it a reliable default strategy. The bearish side, regardless of filtering, tops out at **~75%** — significantly lower and more exposed to sharp bounce-back rallies (e.g., April 2025 tariff reversal: +424 points overnight). Adding stricter filters reduces signal count without meaningfully improving the win rate.

For this reason, the bot **defaults to Bull Put Spreads only**. Bear Call Spreads can be enabled with the `--add-bear` flag for users who accept the lower win probability.

---

## Architecture & Modules

- `market_data_fetcher.py`: Handles data ingestion. Scrapes real-time snapshot data from TradingView and merges it with historical YFinance data to create a perfect 100-day OHLC dataset.
- `xsp_option_finder_theory.py`: The quantitative engine. Implements the Black-Scholes math (Norm distributions, d1/d2) to find the exact sell-leg strike price that matches the target 0.20 Delta without relying on live IBKR market data subscriptions. The buy leg is then placed one strike away.
- `xsp_option_trader.py`: The execution engine. Contains the `BaseCreditSpreadTrader` class, handling the IBKR asynchronous API, order creation, and the automated limit-price repricing loop.
- `xsp_option_trade_bot.py`: The brain. Orchestrates the modules above, calculates the EMA20, evaluates the bullish/bearish rules, and makes the final decision to trade or abort.
- `telegram_notifier.py`: Sends a Telegram message after every bot execution — regardless of outcome (trade filled, aborted, error). Reads credentials from `.tg_bot_secret.json`.
- `bot_launcher.sh`: The automation wrapper that ensures the script executes exactly at 3:55 PM EST, regardless of the physical timezone of your computer.

---

## Installation & Setup

### 1. Prerequisites

- **Python 3.10+**
- **Interactive Brokers TWS or IB Gateway** installed and running.
- TWS must be configured to allow API connections on port `7497` (Paper) or `7496` (Live).
- IB Gateway uses port `4002` (Paper) or `4001` (Live).

### 2. Install Dependencies

Clone the repository and install the required Python packages:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

_Note: Ensure your TWS API settings have "Enable ActiveX and Socket Clients" checked._

---

## Telegram Notifications

The bot sends a summary message to a Telegram chat after every execution — whether the trade was filled, aborted, or an error occurred.

### Setup

1. Create a Telegram bot via [@BotFather](https://t.me/BotFather) and obtain your **Bot Token**.
2. Send a message to your bot and retrieve your **Chat ID** (you can use the `getUpdates` API or [@userinfobot](https://t.me/userinfobot)).
3. Create a `.tg_bot_secret.json` file in the project root:

   ```json
   {
     "telegram_bot_token": "YOUR_BOT_TOKEN",
     "telegram_chat_id": "YOUR_CHAT_ID"
   }
   ```

> [!CAUTION]
> **Never commit** `.tg_bot_secret.json` to version control. It is already listed in `.gitignore`.

If the secret file is missing or malformed, the bot will log a warning and continue operating normally — notifications will simply be skipped.

---

## Usage & Automation

### Manual Execution

You can manually run the bot at any time to evaluate the current market and execute a trade if conditions are met:

```bash
venv/bin/python xsp_option_trade_bot.py
```

You can customize the bot's behavior using command-line arguments. For example, to run on the live trading port with a custom quantity and credit threshold:

```bash
venv/bin/python xsp_option_trade_bot.py --ib-port 7496 --quantity 2 --min-credit 0.10
```

Available arguments (all default to the original strategy constants):

- `--ib-host`: IBKR Host (default: 127.0.0.1)
- `--ib-port`: IBKR Port (default: 7497)
- `--client-id`: IBKR Client ID (default: 15)
- `--high-delta`: Sell leg target delta (default: 0.20)
- `--dte-target`: Target days-to-expiration (default: 1)
- `--max-ema-gap`: Max continuous days for EMA gap (default: 20)
- `--walk-step`: Credit reduction per repricing round (default: 0.01)
- `--walk-interval`: Seconds to wait between fill checks (default: 10)
- `--min-credit`: Minimum acceptable net credit (default: 0.09)
- `--quantity`: Number of spread contracts to trade (default: 1)
- `--add-bear`: Enable Bear Call Credit Spreads (disabled by default)

### Fully Automated Setup (macOS & Linux)

To run the bot entirely hands-off every trading day at exactly **3:55 PM EST**, you can use the provided `bot_launcher.sh` script combined with `cron`. The launcher tracks the exact New York time internally, completely ignoring your local system timezone (perfect for travelers).

1. Make the launcher executable:

   ```bash
   chmod +x bot_launcher.sh
   ```

2. Open your crontab editor:

   ```bash
   crontab -e
   ```

3. Add the following line to run the launcher every minute (replace the paths with your actual absolute paths):
   ```bash
   * * * * * /path/to/bot_launcher.sh >> /tmp/bot_output.log 2>&1
   ```

The cron job will silently wake up every minute and trigger the script. The bash script will check if the time in New York is exactly 3:55 PM, and if so, execute the trading strategy. Logs will be automatically generated in `bot_output.log`.

---

## License

This project is licensed under the **Creative Commons Attribution-NonCommercial 4.0 International (CC BY-NC 4.0)** License.

You are free to view, copy, and modify this code for your own **personal study and educational use**. However, you may **not** use this material for commercial purposes (e.g., you cannot sell this software, run it as a paid service, or use it to manage client funds).
