# XSP & SPX Automated Options Trading Strategy

An automated Python trading bot designed to trade daily **0DTE / 1DTE Credit Spreads** and **Iron Condors** on the **Mini-SPX Index (XSP)** and **S&P 500 Index (SPX)** using the Interactive Brokers (`ib_async`) API.

---

## Strategy Overview

The bot evaluates the broader market trend using the **20-day Exponential Moving Average (EMA20)** of the S&P 500 (`^SPX`) to identify market regimes and execute the corresponding strategy:

1. **Bullish Regime (`Close > EMA20`)**:
   - **Bull Put Spread**: Sells an out-of-the-money (OTM) Put at approximately **-0.05 Delta** and buys an offsetting Put with a lower strike.
   - **Iron Condor (Optional via `--iron-condor`)**: Simultaneously executes a Bull Put Spread (-0.05 Delta) and a Bear Call Spread (+0.05 Delta).
2. **Bearish Regime (`Close < EMA20`)**:
   - Optional Bear Call Spread (enabled via `--add-bear`) when today's bar closes lower than it opened (bear bar).
3. **Always Put Spread (Optional via `--always-put-spread`)**:
   - Executes a Bull Put Spread regardless of regime (bullish or bearish), mutually exclusive with `--iron-condor`.

---

## Dual Gateway Architecture (`config.json`)

To ensure optimal execution and real-time market data integrity without paying for duplicate market data subscriptions across multiple accounts, the bot supports a **Dual-Gateway Configuration** via `config.json` (identical to the TastyAgent architecture):

1. **Market Data Gateway (Real Account, e.g. Port `4001`)**:
   - Connected strictly to your live funded IBKR account with active market data subscriptions.
   - Streams real-time market Greeks (tick 106), live SPX/XSP spot price, and option bid/ask quotes (`marketDataType=1`).
   - If market data connection or quote population fails more than **5 times**, the bot cleanly aborts the trade.
2. **Order Execution Gateway (Paper or Real Account, e.g. Port `4002`)**:
   - Connected to your paper trading simulation account (or live account if `--real` is specified).
   - Places the BAG combo orders and executes the walk-the-book algorithm.

### Configuration Setup

Create a `config.json` file in the project root (see `config.json.example`):

```json
{
    "IBKR": {
        "real": {
            "host": "127.0.0.1",
            "port": 4001,
            "client_id": 15,
            "account": "U12345678"
        },
        "paper": {
            "host": "127.0.0.1",
            "port": 4002,
            "client_id": 16
        }
    }
}
```

---

## Safety Mechanisms & Risk Controls

### 1. Macro Event & Tech Earnings Blackout Filter
The bot automatically skips trading (`event_filter.py`) on high-volatility event windows to avoid catastrophic gamma spikes:
- **Macro Economic Events (FOMC, CPI, NFP)**:
  - **Eve of Event**: No trade on the evening preceding FOMC rate decisions, CPI releases, or Non-Farm Payrolls reports.
  - **Day of Event**: No trade on the evening of the event day.
- **Major Tech Earnings (Magnificent 7: AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA)**:
  - **Pre-Market Earnings**: No trade if a mega-cap tech stock reports tomorrow before market open.
  - **After-Hours Earnings**: No trade if a mega-cap tech stock reports today after the close.

### 2. Model Price vs Market Price Consistency Check
To prevent trading during extreme volatility dislocations or stale quotes:
- The bot computes the market-implied IV and delta directly from the NBBO mid price using Black-Scholes inversion.
- If the theoretical model price (`modelGreeks.optPrice`) diverges from the market mid price by more than **30%** (`--max-model-market-diff 0.30`), the trade is **immediately aborted**.

### 3. Pre-Trade BAG Combo Verification & Adverse Selection Detection
- Before placing combo orders, the bot queries market quotes for the synthetic BAG contract.
- If the execution fills at an actual credit significantly higher than the initial limit price (e.g. `actual >= limit + 0.20`), an adverse selection warning is logged.

### 4. Market Schedule & Early Close (Half-Day) Management
- **Regular Trading Days**: Strategy executes 8 minutes before the 16:00 ET close at **15:52 ET**.
- **Early Close Days (13:00 ET Close)**: On Black Friday, Christmas Eve, and July 3rd, the strategy dynamically shifts execution 8 minutes before early close to **12:52 ET**.

---

## Installation & Setup

### 1. Prerequisites
- **Python 3.10+**
- **Interactive Brokers TWS or IB Gateway** running.
- TWS/Gateway API settings: Ensure "Enable ActiveX and Socket Clients" is checked.

### 2. Install Dependencies
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. IBKR Market Data Subscriptions (For `--ib-market`)

If you intend to run the bot with the `--ib-market` flag to utilize real-time model Greeks and the live SPX spot price, you **must** have active market data subscriptions on your real IBKR account to avoid "No security definition" or missing data errors.

**You need BOTH of the following subscriptions:**

1. **US Securities Snapshot and Futures Value Bundle (NP,L1)**: This is the mandatory foundational base package required by IBKR for streaming US market data.
2. **Cboe One Add-On Bundle (NP,L1)**: This add-on provides the real-time index spot pricing specifically for CBOE indices (like SPX and VIX).

_(Note: IBKR requires the "US Securities Snapshot" bundle as a prerequisite before it allows you to purchase the "Cboe One Add-On")._

---

## Usage & Automation

### Manual Execution

```bash
# SPX Paper Trading with Real Market Data:
venv/bin/python spx_option_trade_bot.py --ib-market

# SPX Live Real Trading:
venv/bin/python spx_option_trade_bot.py --ib-market --real

# XSP Paper Trading:
venv/bin/python xsp_option_trade_bot.py --ib-market
```

Available CLI arguments:
- `--config`: Path to custom `config.json` (default: `config.json` in project root).
- `--real`: Execute orders on the real account (default: paper account).
- `--ib-market`: Use real-time live IBKR option chain Greeks and prices from the real gateway.
- `--iron-condor`: Execute an Iron Condor in bullish regimes (mutually exclusive with `--always-put-spread`).
- `--always-put-spread`: Always execute a Put Credit Spread regardless of regime (mutually exclusive with `--iron-condor`).
- `--bull-delta`: Sell leg target delta for Put Spread (default: 0.05).
- `--bear-delta`: Sell leg target delta for Call Spread (default: 0.05).
- `--dte-target`: Target days-to-expiration (default: 1).
- `--spread-step`: Strike step multiplier between short and long legs (default: 1; e.g. 2 = 10-wide for SPX).
- `--max-model-market-diff`: Maximum allowable divergence between model price and market price (default: 0.30, i.e. 30%).
- `--ignore-events`: Bypass FOMC, CPI, NFP, and tech earnings blackout filters.
- `--ignore-time-check`: Bypass trading schedule timing check.

### Fully Automated Setup (`cron`)

Run `bot_launcher.sh` every minute via `crontab -e`:

```bash
* * * * * /path/to/bot_launcher.sh >> /tmp/bot_output.log 2>&1
```

`bot_launcher.sh` queries `market_schedule.py --check-trigger` every minute in `America/New_York` time. It automatically wakes up at **15:52 ET** on regular days (or **12:52 ET** on early close days), runs the strategy, and exits silently on off-hours or weekends.
