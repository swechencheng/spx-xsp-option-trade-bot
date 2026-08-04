import yfinance as yf
import pandas as pd
import matplotlib

matplotlib.use("Agg")
import mplfinance as mpf
import os


def run_backtest():
    print("Downloading ^SPX data...")
    df = yf.download("^SPX", start="2020-01-01", end="2026-08-01", progress=False)

    # yfinance download might return a MultiIndex if multiple tickers, but for one it should be flat.
    # Handle both cases:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.droplevel(1)

    df["EMA20"] = df["Close"].ewm(span=20, adjust=False).mean()
    df["Next_Close"] = df["Close"].shift(-1)
    df["Change"] = df["Next_Close"] - df["Close"]

    df = df.dropna(subset=["EMA20", "Next_Close"])

    buffers = [50, 75, 80, 85, 90, 95, 100]

    # Regimes
    df["Bullish"] = df["Close"] > df["EMA20"]

    df["Bearish_Simple"] = df["Close"] < df["EMA20"]

    # We need to output markdown
    # 1. Bullish Regime, within or surpasses above the 50/75/100-point buffer
    # 2. Bullish Regime, within or drops below the 50/75/100-point buffer

    def evaluate(mask, name, direction):
        subset = df[mask]
        total = len(subset)
        if total == 0:
            return ""

        lines = []
        lines.append(f"### {name}")
        lines.append(f"Total Signals: {total}")
        lines.append("")
        lines.append(
            "| Buffer | ✅ Stays Within | ❌ Fails (Breaks Buffer) | Win Rate |"
        )
        lines.append("|---|---|---|---|")

        for b in buffers:
            if direction == "above":
                fails = subset[subset["Change"] > b]
            else:
                fails = subset[subset["Change"] < -b]

            fail_count = len(fails)
            win = total - fail_count
            win_rate = (win / total) * 100 if total > 0 else 0

            lines.append(f"| {b} pts | {win} | {fail_count} | {win_rate:.2f}% |")

            if b == 100:
                for date, _ in fails.iterrows():
                    try:
                        loc = df.index.get_loc(date)
                        if isinstance(loc, slice):
                            loc = loc.start
                        # We want the failed bar (loc + 1) to be the last bar.
                        # We want 59 bars prior to it, making 60 bars total.
                        end_loc = loc + 2
                        start_loc = max(0, end_loc - 60)
                        plot_df = df.iloc[start_loc:end_loc]

                        safe_name = (
                            name.replace(" ", "_")
                            .replace("(", "")
                            .replace(")", "")
                            .replace("-", "")
                            .replace(">", "gt")
                            .replace("<", "lt")
                        )
                        safe_name = "".join(
                            [c for c in safe_name if c.isalnum() or c == "_"]
                        )
                        # Replace multiple underscores
                        while "__" in safe_name:
                            safe_name = safe_name.replace("__", "_")
                        date_str = date.strftime("%Y-%m-%d")

                        # Save in backtest folder
                        backtest_dir = os.path.dirname(os.path.abspath(__file__))
                        fname = os.path.join(
                            backtest_dir, f"{date_str}_{safe_name}.png"
                        )

                        # Create an addplot for the EMA20 line
                        ap = mpf.make_addplot(plot_df["EMA20"], color="blue")

                        mpf.plot(
                            plot_df,
                            type="candle",
                            style="charles",
                            addplot=ap,
                            title=f"{name}\n{date_str} (Signal)",
                            savefig=dict(
                                fname=fname, format="png", bbox_inches="tight"
                            ),
                        )
                    except Exception as e:
                        print(f"Error plotting {date}: {e}")

        lines.append("")
        return "\n".join(lines)

    out = []
    out.append("## Bullish Regime (Close > EMA20)")
    out.append(
        evaluate(df["Bullish"], "Bullish - Surpasses Above (Call Spread Risk)", "above")
    )
    out.append(
        evaluate(df["Bullish"], "Bullish - Drops Below (Put Spread Risk)", "below")
    )

    out.append("## Bearish Regime (Close < EMA20)")
    out.append(
        evaluate(
            df["Bearish_Simple"],
            "Bearish Simple - Surpasses Above (Call Spread Risk)",
            "above",
        )
    )
    out.append(
        evaluate(
            df["Bearish_Simple"],
            "Bearish Simple - Drops Below (Put Spread Risk)",
            "below",
        )
    )

    with open("backtest_results.md", "w") as f:
        f.write("\n".join(out))
    print("Done. Results in backtest_results.md")


if __name__ == "__main__":
    run_backtest()
