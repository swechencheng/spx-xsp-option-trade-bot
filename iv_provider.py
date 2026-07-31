"""IV Provider module with IBKR and yfinance strategies.

On startup, probes IBKR for market data subscription availability.
If error 10091 is detected, falls back to yfinance for IV data.
"""

import asyncio
import logging

import yfinance as yf
from ib_async import Option, Stock

logger = logging.getLogger(__name__)

# Sentinel used to detect error 10091 during the probe
_SUBSCRIPTION_ERROR_CODE = 10091


class IVProvider:
    """Provides implied volatility from IBKR or yfinance."""

    def __init__(self):
        self._use_yfinance = False

    @property
    def source(self):
        return "yfinance" if self._use_yfinance else "ibkr"

    async def get_iv(self, ib_client, contract, symbol, exp_date, timeout_sec=15):
        """Get implied volatility using the active strategy.

        Args:
            ib_client:   Connected ib_async.IB instance.
            contract:    Qualified ib_async.Option contract.
            symbol:      Underlying ticker symbol (e.g. 'TSLA').
            exp_date:    Expiration date as 'YYYY-MM-DD'.
            timeout_sec: Max seconds to wait for IBKR Greeks data.

        Returns:
            dict with keys 'model_iv', 'bid_iv', 'ask_iv'.
            Values are float or None.
        """
        if self._use_yfinance:
            return self._get_iv_from_yfinance(
                symbol=symbol,
                strike=float(contract.strike),
                exp_date=exp_date,
                right=contract.right,
            )

        iv_data = await self._get_iv_from_ibkr(ib_client, contract, timeout_sec)

        # If IBKR strategy encountered a subscription error, we fall back to yfinance
        # for this call, and future calls will use yfinance automatically.
        if self._use_yfinance:
            logger.info(f"[IVProvider] Retrying {symbol} IV using yfinance fallback...")
            return self._get_iv_from_yfinance(
                symbol=symbol,
                strike=float(contract.strike),
                exp_date=exp_date,
                right=contract.right,
            )

        return iv_data

    # ── IBKR strategy ────────────────────────────────────────────

    async def _get_iv_from_ibkr(self, ib_client, contract, timeout_sec=15):
        """Fetch IV from IBKR market data (tick 106)."""
        got_error = False

        def _on_error(reqId, errorCode, errorString, err_contract):
            nonlocal got_error
            if errorCode == _SUBSCRIPTION_ERROR_CODE:
                got_error = True
                self._use_yfinance = True

        ib_client.errorEvent += _on_error

        ib_client.reqMarketDataType(4)

        try:
            ticker = ib_client.reqMktData(
                contract, genericTickList="106", snapshot=False
            )

            for _ in range(timeout_sec):
                if got_error:
                    logger.info(
                        "[IVProvider] ⚠️  IBKR market data subscription NOT available "
                        "(error 10091). Switching to yfinance for IV globally."
                    )
                    break
                await asyncio.sleep(1)
                if ticker.modelGreeks or ticker.bidGreeks or ticker.askGreeks:
                    break

            iv_data = {"model_iv": None, "bid_iv": None, "ask_iv": None}

            if not got_error:
                if ticker.modelGreeks and ticker.modelGreeks.impliedVol is not None:
                    iv_data["model_iv"] = ticker.modelGreeks.impliedVol

                if ticker.bidGreeks and ticker.bidGreeks.impliedVol is not None:
                    iv_data["bid_iv"] = ticker.bidGreeks.impliedVol

                if ticker.askGreeks and ticker.askGreeks.impliedVol is not None:
                    iv_data["ask_iv"] = ticker.askGreeks.impliedVol

                logger.info(
                    f"[IVProvider/IBKR] IV for {contract.localSymbol}: "
                    f"model={iv_data['model_iv']}, bid={iv_data['bid_iv']}, "
                    f"ask={iv_data['ask_iv']}"
                )

            ib_client.cancelMktData(contract)
            return iv_data
        finally:
            ib_client.errorEvent -= _on_error

    # ── yfinance strategy ────────────────────────────────────────

    def _get_iv_from_yfinance(self, symbol, strike, exp_date, right):
        """Fetch IV from the yfinance options chain.

        Args:
            symbol:   Underlying ticker (e.g. 'TSLA').
            strike:   Strike price as float.
            exp_date: Expiration date as 'YYYY-MM-DD'.
            right:    'C' or 'P'.

        Returns:
            dict with 'model_iv' set to yfinance impliedVolatility,
            'bid_iv' and 'ask_iv' as None.
        """
        iv_data = {"model_iv": None, "bid_iv": None, "ask_iv": None}

        try:
            tk = yf.Ticker(symbol)
            chain = tk.option_chain(exp_date)

            if right.upper() == "C":
                df = chain.calls
            else:
                df = chain.puts

            # Match the strike price (float comparison with small tolerance)
            row = df[abs(df["strike"] - strike) < 0.01]

            if row.empty:
                logger.warning(
                    f"[IVProvider/yfinance] No matching option found for "
                    f"{symbol} {right} {strike} {exp_date}"
                )
                return iv_data

            iv = float(row.iloc[0]["impliedVolatility"])
            iv_data["model_iv"] = iv

            logger.info(
                f"[IVProvider/yfinance] IV for {symbol} {right} "
                f"${strike} {exp_date}: {iv:.4f}"
            )
        except Exception as e:
            logger.error(f"[IVProvider/yfinance] Error fetching IV: {e}")

        return iv_data
