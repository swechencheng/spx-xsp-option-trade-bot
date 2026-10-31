#!/bin/bash

# Ensure we are in the directory where the script is located
cd "$(dirname "$0")" || exit

export PYTHONUNBUFFERED=1

# Check if current minute in America/New_York is the scheduled trading trigger time
# (15:52 ET on regular 16:00 close days, 12:52 ET on early close 13:00 days, exits non-zero on weekends/off-hours)
venv/bin/python market_schedule.py --check-trigger
if [ $? -eq 0 ]; then
    echo "============================================================"
    echo "$(date): Market schedule matched target trigger time in New York. Starting trading bot..."
    echo "============================================================"
    # Execute the python bots with unbuffered output
    # Config is automatically loaded from config.json (Real gateway 4001 for live market data, Paper 4002 for execution)

    # --------------------------------------------------------------------------
    # Active Execution Command (Default: SPX Bull Put Spread / Market Regime mode with Live IB Market Data)
    # --------------------------------------------------------------------------
    venv/bin/python -u spx_option_trade_bot.py --ib-market

    # --------------------------------------------------------------------------
    # Other Example Configurations (Uncomment to enable):
    # --------------------------------------------------------------------------
    # 1. SPX Iron Condor (Simultaneous Put & Call spreads in bullish regimes):
    # venv/bin/python -u spx_option_trade_bot.py --ib-market --iron-condor

    # 2. SPX Always Put Spread (Always execute Bull Put Spread regardless of regime):
    # venv/bin/python -u spx_option_trade_bot.py --ib-market --always-put-spread

    # 3. SPX 10-wide Spread (Spread step 2 = 10 pts wide between short and long legs):
    # venv/bin/python -u spx_option_trade_bot.py --ib-market --spread-step 2

    # 4. SPX Live Real Account Trading (Executes orders on live funded account):
    # venv/bin/python -u spx_option_trade_bot.py --ib-market --real

    # 5. XSP (Mini-SPX) Paper Trading with Real Market Data:
    # venv/bin/python -u xsp_option_trade_bot.py --ib-market

    # 6. Manual Testing / Overrides (Bypass macro events and schedule window checks):
    # venv/bin/python -u spx_option_trade_bot.py --ib-market --ignore-events --ignore-time-check
fi
