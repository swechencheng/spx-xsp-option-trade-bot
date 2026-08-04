# SPX Credit Spread Backtest Results (2020–2026)

This document contains the backtest results for the SPX credit spread strategy, analyzing daily data from January 2020 to July 2026. The test measures whether the index stays within a defined buffer (50, 75, 80, 85, 90, 95, or 100 points) on the following trading day, based on the EMA20 regime of the current day.

## EMA20 Yearly Distribution (2020-2025)

| Year | Total Bars | Close > EMA20 | % Above |
| ---- | ---------- | ------------- | ------- |
| 2020 | 253        | 183           | 72.33%  |
| 2021 | 252        | 202           | 80.16%  |
| 2022 | 251        | 99            | 39.44%  |
| 2023 | 250        | 169           | 67.60%  |
| 2024 | 252        | 204           | 80.95%  |
| 2025 | 250        | 181           | 72.40%  |

## Bullish Regime (Close > EMA20)

### Bullish - Surpasses Above (Call Spread Risk)

Total Signals: 1127

| Buffer  | ✅ Stays Within | ❌ Fails (Breaks Buffer) | Win Rate |
| ------- | --------------- | ------------------------ | -------- |
| 50 pts  | 1025            | 102                      | 90.95%   |
| 75 pts  | 1100            | 27                       | 97.60%   |
| 80 pts  | 1104            | 23                       | 97.96%   |
| 85 pts  | 1111            | 16                       | 98.58%   |
| 90 pts  | 1113            | 14                       | 98.76%   |
| 95 pts  | 1116            | 11                       | 99.02%   |
| 100 pts | 1117            | 10                       | 99.11%   |

### Bullish - Drops Below (Put Spread Risk)

Total Signals: 1127

| Buffer  | ✅ Stays Within | ❌ Fails (Breaks Buffer) | Win Rate |
| ------- | --------------- | ------------------------ | -------- |
| 50 pts  | 1026            | 101                      | 91.04%   |
| 75 pts  | 1079            | 48                       | 95.74%   |
| 80 pts  | 1089            | 38                       | 96.63%   |
| 85 pts  | 1094            | 33                       | 97.07%   |
| 90 pts  | 1097            | 30                       | 97.34%   |
| 95 pts  | 1102            | 25                       | 97.78%   |
| 100 pts | 1108            | 19                       | 98.31%   |

## Bearish Regime (Close < EMA20)

### Bearish Simple - Surpasses Above (Call Spread Risk)

Total Signals: 524

| Buffer  | ✅ Stays Within | ❌ Fails (Breaks Buffer) | Win Rate |
| ------- | --------------- | ------------------------ | -------- |
| 50 pts  | 393             | 131                      | 75.00%   |
| 75 pts  | 462             | 62                       | 88.17%   |
| 80 pts  | 468             | 56                       | 89.31%   |
| 85 pts  | 473             | 51                       | 90.27%   |
| 90 pts  | 481             | 43                       | 91.79%   |
| 95 pts  | 489             | 35                       | 93.32%   |
| 100 pts | 494             | 30                       | 94.27%   |

### Bearish Simple - Drops Below (Put Spread Risk)

Total Signals: 524

| Buffer  | ✅ Stays Within | ❌ Fails (Breaks Buffer) | Win Rate |
| ------- | --------------- | ------------------------ | -------- |
| 50 pts  | 424             | 100                      | 80.92%   |
| 75 pts  | 460             | 64                       | 87.79%   |
| 80 pts  | 467             | 57                       | 89.12%   |
| 85 pts  | 470             | 54                       | 89.69%   |
| 90 pts  | 476             | 48                       | 90.84%   |
| 95 pts  | 483             | 41                       | 92.18%   |
| 100 pts | 485             | 39                       | 92.56%   |
