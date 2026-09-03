"""
data/yf_fetcher.py — Free NSE data via Yahoo Finance (yfinance).

Drop-in replacement for data/fetcher.py (Kite).
NSE symbols get .NS suffix automatically: RELIANCE → RELIANCE.NS

Interval mapping (Kite → yfinance):
  minute    → 1m   (max 7 days history)
  3minute   → 5m   (max 60 days)
  5minute   → 5m   (max 60 days)
  15minute  → 15m  (max 60 days)
  30minute  → 30m  (max 60 days)
  60minute  → 60m  (max 730 days)
  day       → 1d   (unlimited)

LTP: uses most recent 1m candle close (~1-3 min delay, free tier).
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import yfinance as yf

# Suppress yfinance's own noisy download-failure messages
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

logger = logging.getLogger(__name__)

# Kite interval → (yfinance interval, max history days)
INTERVAL_MAP = {
    "minute":    ("1m",  7),
    "3minute":   ("5m",  60),
    "5minute":   ("5m",  60),
    "15minute":  ("15m", 60),
    "30minute":  ("30m", 60),
    "60minute":  ("60m", 730),
    "day":       ("1d",  1800),
}


def _yf_symbol(symbol: str, exchange: str = "NSE") -> str:
    """Convert NSE symbol to Yahoo Finance ticker."""
    suffix = ".NS" if exchange == "NSE" else ".BO"
    return f"{symbol}{suffix}"


class YFinanceFetcher:
    """
    Same public interface as DataFetcher (Kite) but uses yfinance.
    Swap in main.py by setting data_source: "yfinance" in config.yaml.
    """

    def get_candles(self, symbol: str, interval: str, exchange: str = "NSE") -> Optional[pd.DataFrame]:
        """
        Fetch historical OHLCV candles.
        Returns DataFrame with columns: open, high, low, close, volume.
        """
        yf_interval, max_days = INTERVAL_MAP.get(interval, ("15m", 60))
        ticker = _yf_symbol(symbol, exchange)

        # Use period string for intraday (yfinance requirement)
        if yf_interval.endswith("m") or yf_interval.endswith("h"):
            period = f"{min(max_days, 59)}d"
            kwargs = dict(period=period, interval=yf_interval)
        else:
            end   = datetime.now()
            start = end - timedelta(days=max_days)
            kwargs = dict(start=start.strftime("%Y-%m-%d"),
                          end=end.strftime("%Y-%m-%d"),
                          interval=yf_interval)

        try:
            df = yf.download(
                ticker,
                auto_adjust=True,
                progress=False,
                **kwargs,
            )
            # Retry with shorter period if first attempt fails (yfinance flakiness)
            if df.empty and "period" in kwargs:
                shorter = {"5m": "30d", "15m": "30d", "30m": "30d", "60m": "60d"}.get(yf_interval)
                if shorter and kwargs.get("period") != shorter:
                    logger.debug(f"Retrying {ticker} with period={shorter}")
                    df = yf.download(ticker, interval=yf_interval, period=shorter,
                                     auto_adjust=True, progress=False)
            if df.empty:
                logger.warning(f"No data from yfinance for {ticker}")
                return None

            # Flatten multi-level columns if present
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            df.columns = [c.lower() for c in df.columns]
            df.index   = pd.to_datetime(df.index)

            # Convert to IST (Yahoo returns UTC for intraday)
            if df.index.tzinfo is not None:
                df.index = df.index.tz_convert("Asia/Kolkata").tz_localize(None)

            return df[["open", "high", "low", "close", "volume"]].dropna()

        except Exception as e:
            logger.warning(f"yfinance fetch skipped for {symbol} ({yf_interval}): {e}")
            return None

    def get_ltp(self, symbols: list[str], exchange: str = "NSE") -> dict[str, float]:
        """
        Get last traded price using the most recent 1m candle.
        ~1-3 minute delay on free tier — sufficient for 5-min scan loops.
        """
        result = {}
        tickers = [_yf_symbol(s, exchange) for s in symbols]
        try:
            data = yf.download(
                tickers,
                period="1d",
                interval="1m",
                auto_adjust=True,
                progress=False,
            )
            if data.empty:
                return result

            close = data["Close"] if "Close" in data.columns else data.get("close")
            if close is None:
                return result

            # Multi-ticker download returns multi-level columns
            if isinstance(close, pd.DataFrame):
                for sym, ticker in zip(symbols, tickers):
                    col = ticker if ticker in close.columns else sym
                    if col in close.columns:
                        last = close[col].dropna().iloc[-1] if not close[col].dropna().empty else None
                        if last:
                            result[sym] = float(last)
            else:
                # Single ticker
                last = close.dropna().iloc[-1] if not close.dropna().empty else None
                if last and symbols:
                    result[symbols[0]] = float(last)

        except Exception as e:
            logger.error(f"yfinance LTP error: {e}")
        return result

    def get_instrument_token(self, symbol: str, exchange: str = "NSE") -> Optional[int]:
        """Not applicable for yfinance — returns None (only needed for Kite)."""
        return None
