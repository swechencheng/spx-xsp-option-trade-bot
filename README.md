# SPX & XSP Automated Options Trading Bot

> [!CAUTION]
> **Disclaimer & Caveat**: This software is strictly for **educational and personal study purposes only**. It is **NOT** intended for commercial use, and it does **NOT** constitute financial advice. Options trading involves significant risk of loss. The author assumes no liability for any financial losses incurred. Use entirely at your own risk.

An end-to-end automated trading bot for Interactive Brokers (IBKR) that trades next-day (1DTE) Credit Spreads on the SPX indices and XSP (Mini-SPX).

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

1. **Bullish Regime (Close > EMA20)**: Executes a **Bull Put Credit Spread** (or an **Iron Condor** if `--iron-condor` is set, which is the default for SPX).
2. **Bearish Regime (Close < EMA20)**: Executes a **Bear Call Credit Spread** _(disabled by default; enable with `--add-bear`)_.

The bot is strictly defensive. It will **abort** the trade under the following conditions:

- **Overextended Trends**: If the price has gapped completely above or below the EMA20 for more than 20 consecutive days, the bot pauses trading to avoid mean-reversion whipsaws.
- **Bull Bar Filter**: For a bearish setup, the current daily bar cannot be bullish (`Close > Open`).
- **Non-Trading Days**: The bot uses Yahoo Finance to verify if the current date is a valid US trading day. If it's a weekend or holiday, it gracefully exits.

---

## The Mechanism

When a valid trend is detected, the bot performs the following steps:

1. **Live Data Fetching**: By default, it pulls high-precision live SPX and VIX snapshots from TradingView and combines them with historical data from Yahoo Finance. (If the `--ib-market` flag is used, it completely bypasses TradingView and fetches the live SPX spot directly through your IBKR data feed).
2. **Option Discovery**: By default, the bot calculates theoretical option prices using the Black-Scholes equation. If `--ib-market` is specified, it scans real-time live model Greeks directly from IBKR (`reqTickers`) without incurring regulatory snapshot fees, remaining fully compatible with paper trading accounts. It scans to find:
   - A "Sell" leg with an absolute Delta of **0.20**
   - A "Buy" leg placed one strike away from the sell leg (e.g., $5 wide for SPX, $1 wide for XSP), creating a tight credit spread
   - **Holiday-Aware Expiration Selection**: Instead of blindly adding calendar days, the bot downloads the list of live expirations directly from CBOE and sorts them. This allows it to perfectly calculate the next _trading day_ for 1DTE expirations, automatically skipping weekends and exchange holidays.
3. **Execution (Walk the Book)**:
   - The bot connects to the IBKR TWS/Gateway API.
   - It constructs a `BAG` combo order (a multi-leg spread).
   - It submits an initial Limit Order at the theoretical mid-price (maximizing credit).
   - If the order isn't filled within 5 seconds, it cleanly cancels the order and resubmits a new one with the credit reduced by $0.01 (avoiding IBKR combo modification Warning 105). This "Walk the Book" process repeats until the order fills or the credit drops below the minimum acceptable threshold ($0.09).

---

## Why Is This Strategy Profitable?

This strategy relies on the core mathematical advantages of option selling:

1. **High Probability of Success**: By selling options at the 0.20 Delta, there is an approximate **80% statistical probability** that the sold option will expire Out-Of-The-Money (OOTM) and be completely worthless.
2. **Rapid Theta Decay**: Trading options close to expiration (1DTE) means that the time value of the option decays exponentially fast.
3. **Trend Following**: By filtering trades using the EMA20, the bot ensures you are always trading _with_ the broader market momentum. You are placing bets that the market will not sharply reverse against the current short-term trend.
4. **Defined Risk**: Buying the option one strike away from the sell leg creates a tight "Credit Spread". This caps your maximum potential loss to the spread width (e.g., $5 for SPX, $1 for XSP minus the credit received), making the strategy highly capital efficient.

---

## Potential Risks

While highly probable, this strategy is not without risks. You must be aware of the market conditions where it can lose money:

1. **Sharp Mean Reversions (Whipsaws)**: The strategy uses the EMA20 to trade _with_ the trend. If the market is chopping sideways or experiences a violent intraday reversal (e.g., a sudden 1-2% drop after an uptrend), the underlying index can quickly crash through your short strike.
2. **High Gamma (Pin Risk)**: Short-duration options (1DTE) have extremely high Gamma. This means that if the index gets close to your strike price on expiration day, the delta will change very rapidly. A small index movement can instantly turn a safe position into a max-loss position.
3. **Overnight Gap Risk**: Because the strategy executes at 3:55 PM for expiration on the next trading day, holding the position overnight exposes you to gap risk. Unforeseen macroeconomic news (CPI drops, Fed announcements, or geopolitical events) can cause the market to gap open the next morning far past your strike prices, leaving no room to manage the trade.
4. **Asymmetric Risk/Reward**: Because you are trading high-probability setups (selling 0.20 Deltas) with tight 1-wide spreads, the premium you collect is small relative to the maximum possible loss ($1 spread width). A single max-loss event can wipe out the profits of several successful trades.

---

## Architecture & Modules

- `market_data_fetcher.py`: Handles data ingestion. Scrapes real-time snapshot data from TradingView (or directly from IBKR if `--ib-market` is used) and merges it with historical YFinance data to create a perfect 100-day OHLC dataset.
- `option_finder_ibkr.py` & `option_finder_theory.py`: The quantitative engines. They find the exact sell-leg strike price that matches the target Delta using either the Black-Scholes math (theory) or IBKR's live model Greeks (market).
- `strategy_trader.py`: The execution engine. Contains the `BaseCreditSpreadTrader` and `IronCondorTrader` classes, handling the IBKR asynchronous API, order creation, and the automated limit-price repricing loop (Walk the Book).
- `base_option_trade_bot.py`: The core brain. Calculates the EMA20, evaluates the bullish/bearish rules, and makes the final decision to trade or abort.
- `spx_option_trade_bot.py` & `xsp_option_trade_bot.py`: The entry points that subclass the base bot and set index-specific constants (like default walk steps, contract multipliers, and symbol names).
- `iv_provider.py`: Handles fetching implied volatility from either IBKR directly or defaulting to Yahoo Finance chains for theoretical calculations.
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

### 3. IBKR Market Data Subscriptions (For `--ib-market`)

If you intend to run the bot with the `--ib-market` flag to utilize real-time model Greeks and the live SPX spot price, you **must** have active market data subscriptions to avoid "No security definition" or missing data errors.

**You need BOTH of the following subscriptions:**

1. **US Securities Snapshot and Futures Value Bundle (NP,L1)**: This is the mandatory foundational base package required by IBKR for streaming US market data.
2. **Cboe One Add-On Bundle (NP,L1)**: This add-on provides the real-time index spot pricing specifically for CBOE indices (like SPX and VIX).

_(Note: IBKR requires the "US Securities Snapshot" bundle as a prerequisite before it allows you to purchase the "Cboe One Add-On")._

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
# For SPX:
venv/bin/python spx_option_trade_bot.py
# For XSP:
venv/bin/python xsp_option_trade_bot.py
```

You can customize the bot's behavior using command-line arguments. For example, to run on the live trading port with a custom quantity, pulling actual market data:

```bash
venv/bin/python spx_option_trade_bot.py --ib-port 7496 --quantity 2 --ib-market
```

Available arguments (all default to the original strategy constants):

- `--ib-host`: IBKR Host (default: 127.0.0.1)
- `--ib-port`: IBKR Port (default: 7497)
- `--client-id`: IBKR Client ID (default: 15)
- `--bull-delta`: Sell leg target delta for Bull Put Spread (default: 0.05)
- `--bear-delta`: Sell leg target delta for Bear Call Spread (default: 0.05)
- `--dte-target`: Target days-to-expiration (default: 1)
- `--walk-step`: Credit reduction per repricing round (default: 0.01)
- `--walk-interval`: Seconds to wait between fill checks (default: 5)
- `--min-credit`: Minimum acceptable net credit (default: 0.09)
- `--quantity`: Number of spread contracts to trade (default: 1)
- `--add-bear`: Enable Bear Call Credit Spreads (disabled by default)
- `--spread-step`: Number of strike steps between short and long legs (default: 1). For example, `--spread-step 2` creates a 10-wide spread for SPX or a 2-wide spread for XSP.
- `--ib-market`: Use IBKR live option chain model Greeks instead of Black-Scholes theory.

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

## Known Limitations

### Theoretical Volatility Skew Underestimation

By default, the bot uses a theoretical pricing model (`TheoryOptionFinder`) to compute option prices and deltas without heavily relying on IBKR's market data subscriptions.

To accomplish this efficiently, it probes the At-The-Money (ATM) contract to fetch a single Implied Volatility (IV) and applies it universally across the entire option chain using the Black-Scholes formula. However, equity indices like SPX and XSP exhibit a pronounced **Volatility Skew** (puts have significantly higher IV the further out-of-the-money they are due to downside crash protection demand).

Because the bot applies the artificially low ATM IV to deep out-of-the-money options, it severely underestimates their theoretical price and delta. As a result, when you ask the theoretical finder for a `-0.1` delta put, it will be forced to select a strike much closer to the current spot price than a real market options chain would suggest.

**Solution:**
If you require precision that matches live trading platforms (like IBKR Mobile) and have active market data subscriptions, you should start the bot with the `--ib-market` flag. This forces the bot to fetch the true, live model Greeks for every individual strike using standard data streams (avoiding regulatory snapshot fees) and accurately accounts for the real market's volatility skew. It is fully compatible with both Live and Paper accounts.

---

## License

This project is licensed under the **Creative Commons Attribution-NonCommercial 4.0 International (CC BY-NC 4.0)** License.

You are free to view, copy, and modify this code for your own **personal study and educational use**. However, you may **not** use this material for commercial purposes (e.g., you cannot sell this software, run it as a paid service, or use it to manage client funds).
