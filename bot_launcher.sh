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
    venv/bin/python -u spx_option_trade_bot.py --ib-market --iron-condor
    venv/bin/python -u xsp_option_trade_bot.py --ib-market
fi
