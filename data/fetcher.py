"""
data/fetcher.py — Fetches OHLCV candle data from Zerodha Kite API.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
from kiteconnect import KiteConnect

logger = logging.getLogger(__name__)


class DataFetcher:
    # Maps human-readable interval to number of days of history to fetch
    INTERVAL_HISTORY_DAYS = {
        "minute":    3,
        "3minute":   5,
        "5minute":   7,
        "15minute":  30,
        "30minute":  60,
        "60minute":  90,
        "day":       400,
    }

    def __init__(self, kite: KiteConnect):
        self.kite = kite
        self._instruments: Optional[pd.DataFrame] = None

    def _get_instruments(self) -> pd.DataFrame:
        """Cache and return NSE instrument list."""
        if self._instruments is None:
            logger.info("Fetching NSE instrument list...")
            instruments = self.kite.instruments("NSE")
            self._instruments = pd.DataFrame(instruments)
        return self._instruments

    def get_instrument_token(self, symbol: str, exchange: str = "NSE") -> Optional[int]:
        """Return the numeric instrument token for a symbol."""
        df = self._get_instruments()
        match = df[(df["tradingsymbol"] == symbol) & (df["exchange"] == exchange)]
        if match.empty:
            logger.warning(f"Symbol not found: {symbol}")
            return None
        return int(match.iloc[0]["instrument_token"])

    def get_candles(self, symbol: str, interval: str, exchange: str = "NSE") -> Optional[pd.DataFrame]:
        """
        Fetch historical OHLCV candles for a symbol.
        Returns a DataFrame with columns: date, open, high, low, close, volume.
        """
        token = self.get_instrument_token(symbol, exchange)
        if token is None:
            return None

        days = self.INTERVAL_HISTORY_DAYS.get(interval, 30)
        to_date = datetime.now()
        from_date = to_date - timedelta(days=days)

        try:
            data = self.kite.historical_data(
                instrument_token=token,
                from_date=from_date.strftime("%Y-%m-%d %H:%M:%S"),
                to_date=to_date.strftime("%Y-%m-%d %H:%M:%S"),
                interval=interval,
                continuous=False,
            )
            if not data:
                logger.warning(f"No candle data returned for {symbol}")
                return None

            df = pd.DataFrame(data)
            df.rename(columns={"date": "date"}, inplace=True)
            df.set_index("date", inplace=True)
            df.index = pd.to_datetime(df.index)
            return df

        except Exception as e:
            logger.error(f"Error fetching candles for {symbol}: {e}")
            return None

    def get_ltp(self, symbols: list[str], exchange: str = "NSE") -> dict[str, float]:
        """Return last traded prices for a list of symbols."""
        keys = [f"{exchange}:{s}" for s in symbols]
        try:
            quotes = self.kite.ltp(keys)
            return {s: quotes[f"{exchange}:{s}"]["last_price"] for s in symbols if f"{exchange}:{s}" in quotes}
        except Exception as e:
            logger.error(f"Error fetching LTP: {e}")
            return {}
