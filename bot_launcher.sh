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

# Check if it is exactly 16:00 (4:00 PM EST)
if [ "$EST_HOUR" == "16" ] && [ "$EST_MIN" == "00" ]; then
    echo "$(date): Time matched 16:00 EST. Starting trading bot..."
    # Execute the python bot using relative paths
    venv/bin/python spx_option_trade_bot.py --ib-market --ib-port 4002
    venv/bin/python xsp_option_trade_bot.py --ib-market --ib-port 4002
fi
