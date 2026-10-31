#!/bin/bash

# Ensure we are in the directory where the script is located
cd "$(dirname "$0")" || exit

export PYTHONUNBUFFERED=1

echo "============================================================"
echo "$(date): Launching 0DTE Option Delta Monitor Daemon..."
echo "============================================================"

# Runs continuous daemon monitoring 10:30 ET to close every 5 minutes
# Default: monitors 0DTE short leg delta; aborts on |delta| > 0.65 via walk-the-book
venv/bin/python -u delta_monitor_daemon.py "$@"
