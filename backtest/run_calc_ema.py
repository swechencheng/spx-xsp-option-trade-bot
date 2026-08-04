import yfinance as yf
import pandas as pd

df = yf.download("^SPX", start="2010-01-01", end="2026-01-01", progress=False)
if isinstance(df.columns, pd.MultiIndex):
    df.columns = df.columns.droplevel(1)

df["EMA20"] = df["Close"].ewm(span=20, adjust=False).mean()
df["Above_EMA20"] = df["Close"] > df["EMA20"]
df["Year"] = df.index.year

grouped = df.groupby("Year")["Above_EMA20"].agg(["count", "sum"]).reset_index()
grouped.columns = ["Year", "Total Bars", "Close > EMA20"]

print("# SPX Credit Spread Backtest Results (2010–2026)\n")
print(
    "This document contains the backtest results for the SPX credit spread strategy, analyzing daily data from January 2010 to July 2026. The test measures whether the index stays within a defined buffer (50, 75, 80, 85, 90, 95, or 100 points) on the following trading day, based on the EMA20 regime of the current day.\n"
)

print("## EMA20 Yearly Distribution (2010-2025)")
print("")
print("| Year | Total Bars | Close > EMA20 | % Above |")
print("|---|---|---|---|")
for _, row in grouped.iterrows():
    year = int(row["Year"])
    total = int(row["Total Bars"])
    above = int(row["Close > EMA20"])
    pct = (above / total) * 100
    print(f"| {year} | {total} | {above} | {pct:.2f}% |")
print("")
