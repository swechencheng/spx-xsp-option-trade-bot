#!/bin/bash

# Ensure we are in the directory where the script is located
cd "$(dirname "$0")" || exit

# Get current hour, minute, and day of week in US Eastern Time
EST_HOUR=$(TZ="America/New_York" date +"%H")
EST_MIN=$(TZ="America/New_York" date +"%M")
EST_DAY=$(TZ="America/New_York" date +"%u") # 1=Mon, 7=Sun

# Exit immediately if it's a weekend (Saturday=6, Sunday=7)
if [ "$EST_DAY" -gt 5 ]; then
    exit 0
fi

# Check if it is exactly 15:55 (3:55 PM EST)
if [ "$EST_HOUR" -eq 15 ] && [ "$EST_MIN" -eq 55 ]; then
    echo "$(date): Time matched 15:55 EST. Starting trading bot..."
    # Execute the python bot using relative paths
    venv/bin/python xsp_option_trade_bot.py --add-bear --ib-port 4002 --walk-interval 5
    venv/bin/python spx_option_trade_bot.py --add-bear --ib-port 4002 --walk-interval 5 --bull-delta 0.1 --bear-delta 0.1
fi
