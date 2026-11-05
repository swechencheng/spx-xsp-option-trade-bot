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
- **Macro Economic Events (FOMC, CPI, NFP, US Elections, ISM PMI)**:
  - **Full-Week Blackout**: If any major macro announcement occurs on any day of the trading week (Monday through Friday), trading is completely blocked for that entire week.
  - **Eve of Event**: If the next trading day (e.g. across a weekend) has an event, trading is blocked on the eve.
- **Major Tech Earnings (Magnificent 7: AAPL, MSFT, NVDA, AMZN, GOOGL, GOOG, META, TSLA)**:
  - **Full-Week Blackout**: If any monitored mega-cap tech company reports earnings anytime during the trading week (Monday through Friday), trading is completely blocked for that entire week.
  - **Eve of Earnings**: If the next trading day has tech earnings, trading is blocked on the eve.

### 2. Model Price vs Market Price Consistency & Adverse Selection Protection
To prevent trading during extreme volatility dislocations or selling mispriced options into severe tail-risk events:
- The bot computes the market-implied IV and delta directly from the NBBO mid price using Black-Scholes inversion.
- **Asymmetric Adverse Selection Check**: If the market spread credit (or sell leg market price) exceeds the model theoretical price by more than **30%** (`market_credit > theo_credit * 1.30`), the trade is **immediately aborted**. This protects against market makers pricing in severe downside risk/volatility spikes that lag in the theoretical model (the primary cause of the Oct 29 loss).
- **Bid-Ask Friction Tolerance**: If the market mid credit is lower than theoretical credit due to standard bid-ask friction (e.g. theoretical credit of $0.30 vs market mid of $0.15), it is **allowed** as long as it satisfies the minimum credit floor ($\ge \$0.09$). The walk-the-book algorithm can walk down to $0.09$ and will hold for 60 seconds before canceling.

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
- `--quantity`: Number of spread contracts to trade (default: 1).
- `--min-credit`: Minimum acceptable net credit (default: 0.09, floor: 0.09).
- `--ib-market`: Use real-time live IBKR option chain Greeks and prices from the real gateway.
- `--iron-condor`: Execute an Iron Condor in bullish regimes (mutually exclusive with `--always-put-spread`).
- `--always-put-spread`: Always execute a Put Credit Spread regardless of regime (mutually exclusive with `--iron-condor`).
- `--add-bear`: Enable Bear Call Credit Spreads in bearish regimes (disabled by default).
- `--bull-delta`: Sell leg target delta for Put Spread (default: 0.05).
- `--bear-delta`: Sell leg target delta for Call Spread (default: 0.05).
- `--dte-target`: Target days-to-expiration (default: 1).
- `--spread-step`: Strike step multiplier between short and long legs (default: 1; e.g. 2 = 10-wide for SPX).
- `--walk-step`: Price reduction step per repricing round (default: 0.05).
- `--walk-interval`: Time interval in seconds between walk steps (default: 5).
- `--max-model-market-diff`: Maximum allowable divergence between model price and market price (default: 0.30, i.e. 30%).
- `--ignore-events`: Bypass FOMC, CPI, NFP, US Election, PMI, and tech earnings blackout filters.
- `--ignore-time-check`: Bypass trading schedule timing check.

### Fully Automated Setup (Cron or Systemd Timer)

#### Option A: `cron`
Run `bot_launcher.sh` every minute via `crontab -e`:

```bash
* * * * * /home/ubuntu/spx-xsp-option-trade-bot/bot_launcher.sh >> /tmp/bot_output.log 2>&1
```

`bot_launcher.sh` queries `market_schedule.py --check-trigger` every minute in `America/New_York` time. It automatically wakes up at **15:52 ET** on regular days (or **12:52 ET** on early close days), runs the strategy, and exits silently on off-hours or weekends.

#### Option B: `systemd` Timer + Service (Recommended on modern Linux)

1. Create `/etc/systemd/system/spx-xsp-trading-bot.service`:

```ini
[Unit]
Description=SPX/XSP Option Trading Bot Execution Service
After=network.target

[Service]
Type=oneshot
User=ubuntu
Group=ubuntu
WorkingDirectory=/home/ubuntu/spx-xsp-option-trade-bot
Environment="PYTHONUNBUFFERED=1"
Environment="PATH=/home/ubuntu/spx-xsp-option-trade-bot/venv/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=/bin/bash /home/ubuntu/spx-xsp-option-trade-bot/bot_launcher.sh
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

2. Create `/etc/systemd/system/spx-xsp-trading-bot.timer`:

```ini
[Unit]
Description=Timer to trigger SPX/XSP Option Trading Bot during US market hours
RefersTo=spx-xsp-trading-bot.service

[Timer]
OnCalendar=Mon..Fri *-*-* *:*:00
AccuracySec=1s
Persistent=false

[Install]
WantedBy=timers.target
```

Enable and start with:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now spx-xsp-trading-bot.timer
```

---

## 0DTE Delta Monitor Daemon (Gamma Crash Protection)

To avoid catastrophic gamma blowouts and downward strike breaches on the day of expiration (0DTE), the system includes a dedicated intraday monitor: [`delta_monitor_daemon.py`](delta_monitor_daemon.py).

### How It Works

1. **Active Position Tracking**: Whenever a trade executes successfully, [`base_option_trade_bot.py`](base_option_trade_bot.py) automatically records the open legs to `data/active_position.json` via [`position_manager.py`](position_manager.py).
2. **Monitoring Window**:
   - Commences **60 minutes after market open** (**10:30 ET**) to avoid opening-bell volatility.
   - Runs until market close (**16:00 ET**, or **13:00 ET** on early close days).
   - Scans every 5 minutes (configurable via `--interval`).
3. **Breach Condition**:
   - Inspects the short leg's live delta via the Real Gateway (market data subscription).
   - If $|\Delta| > 0.65$, confirms downside breach risk.
4. **Emergency Close Execution**:
   - Constructs a reverse BAG combo contract (BUY short leg, SELL long leg).
   - Immediately starts walking UP from current **Mid Price** (`current_limit = mid_price`, stepping up by `--walk-step 0.05` every `--walk-interval 5s`).
   - If closing remains unfilled after **15 minutes**, automatically transitions to **Market Order** to guarantee execution and eliminate gamma risk.
   - Continuously retries upon any unexpected network or IBKR errors.
5. **Telegram Notifications**:
   - Sends an immediate alert upon breach detection.
   - Sends periodic progress reports every 15 minutes during the closing process.
   - Sends a final fill summary with execution price, fees, and elapsed time, and marks the trade record as `CLOSED`.

### Launching the Daemon

```bash
# Continuous daemon execution (Paper trading execution, Real market data):
./run_delta_monitor.sh

# Or directly via Python:
venv/bin/python delta_monitor_daemon.py

# Live funded account execution:
venv/bin/python delta_monitor_daemon.py --real

# Single-shot scan (useful for debugging):
venv/bin/python delta_monitor_daemon.py --check-once
```

### Systemd Service Configuration (Linux)

Create `/etc/systemd/system/spx-xsp-delta-monitor.service`:

```ini
[Unit]
Description=0DTE SPX/XSP Option Delta Monitor Daemon
After=network.target

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=/home/ubuntu/spx-xsp-option-trade-bot
Environment="PYTHONUNBUFFERED=1"
ExecStart=/home/ubuntu/spx-xsp-option-trade-bot/venv/bin/python -u delta_monitor_daemon.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start with:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now spx-xsp-delta-monitor.service
sudo journalctl -u spx-xsp-delta-monitor.service -f
```


