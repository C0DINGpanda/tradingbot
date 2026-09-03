"""
data/angel_fetcher.py — Real-time NSE data via Angel One SmartAPI.

Features:
  - Real-time LTP (zero delay via HTTP, tick-by-tick via WebSocket)
  - Full historical OHLCV candle data
  - Nifty 500 symbol → instrument token mapping (auto-downloaded)

Setup:
  1. Open free demat at Angel One: https://www.angelone.in
  2. Create API app at: https://smartapi.angelbroking.com
  3. Add credentials to config.yaml under angel:
       api_key, client_id, password, totp_secret
  4. Set data_source: "angel" in config.yaml

Install: pip install smartapi-python pyotp
"""
import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)

# Angel One interval mapping (Kite → Angel)
INTERVAL_MAP = {
    "minute":    "ONE_MINUTE",
    "3minute":   "THREE_MINUTE",
    "5minute":   "FIVE_MINUTE",
    "15minute":  "FIFTEEN_MINUTE",
    "30minute":  "THIRTY_MINUTE",
    "60minute":  "ONE_HOUR",
    "day":       "ONE_DAY",
}

# Max history days per interval (Angel limits)
INTERVAL_HISTORY_DAYS = {
    "ONE_MINUTE":     30,
    "THREE_MINUTE":   60,
    "FIVE_MINUTE":    100,
    "FIFTEEN_MINUTE": 200,
    "THIRTY_MINUTE":  200,
    "ONE_HOUR":       400,
    "ONE_DAY":        2000,
}

SCRIP_MASTER_URL = (
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
)
SCRIP_MASTER_CACHE = "angel_scrip_master.json"


class AngelFetcher:
    """
    Real-time NSE data fetcher using Angel One SmartAPI.
    Same interface as YFinanceFetcher / DataFetcher (Kite).

    Candle caching strategy:
      - First call for a symbol: fetch full history (100 days)
      - Subsequent calls within same session: fetch only last 60 min,
        append to cache → 1 small request instead of 100-day pull
    """

    def __init__(self, cfg: dict):
        self.cfg    = cfg
        self._cache: dict[str, pd.DataFrame] = {}   # symbol+interval → DataFrame
        self.a_cfg  = cfg.get("angel", {})
        self._obj   = None
        self._tokens: dict[str, str] = {}   # symbol → instrument token
        self._login()
        self._load_scrip_master()

    # ── Login ──────────────────────────────────────────────────────────────
    def _login(self):
        try:
            import pyotp
            from SmartApi import SmartConnect
        except ImportError:
            raise ImportError(
                "Install Angel One dependencies:\n"
                "  pip install smartapi-python pyotp"
            )

        api_key     = self.a_cfg["api_key"]
        client_id   = self.a_cfg["client_id"]
        password    = self.a_cfg["password"]
        totp_secret = self.a_cfg.get("totp_secret", "")

        totp = pyotp.TOTP(totp_secret).now() if totp_secret else input("Enter Angel One TOTP: ")

        self._obj = SmartConnect(api_key=api_key)
        session   = self._obj.generateSession(client_id, password, totp)

        if not session.get("status"):
            raise RuntimeError(f"Angel One login failed: {session.get('message')}")

        self._auth_token = session["data"]["jwtToken"]
        self._feed_token = self._obj.getfeedToken()
        logger.info(f"✅ Angel One logged in as {client_id}")

    # ── Scrip master (symbol → token map) ─────────────────────────────────
    def _load_scrip_master(self):
        """Download Angel scrip master once per day and cache locally."""
        refresh = True
        if os.path.exists(SCRIP_MASTER_CACHE):
            age = time.time() - os.path.getmtime(SCRIP_MASTER_CACHE)
            if age < 86400:   # less than 24 hours old
                refresh = False

        if refresh:
            logger.info("Downloading Angel scrip master...")
            try:
                resp = requests.get(SCRIP_MASTER_URL, timeout=30)
                resp.raise_for_status()
                with open(SCRIP_MASTER_CACHE, "w") as f:
                    f.write(resp.text)
                logger.info("Scrip master downloaded and cached.")
            except Exception as e:
                logger.error(f"Failed to download scrip master: {e}")
                if not os.path.exists(SCRIP_MASTER_CACHE):
                    return

        with open(SCRIP_MASTER_CACHE) as f:
            data = json.load(f)

        # Build symbol → token map for NSE EQ instruments
        for item in data:
            if item.get("exch_seg") == "NSE" and item.get("instrumenttype") in ("", "EQ"):
                sym = item["symbol"].replace("-EQ", "").strip()
                self._tokens[sym] = item["token"]

        logger.info(f"Loaded {len(self._tokens)} NSE symbols from scrip master")

    def _get_token(self, symbol: str) -> Optional[str]:
        # Angel scrip master uses some different symbol names
        SYMBOL_ALIASES = {
            "TATAMOTORS": "TATAMOTORS",
            "LTIM":       "LTIMINDTECH",
            "NIFTY":      "Nifty 50",
            "BANKNIFTY":  "Nifty Bank",
        }
        symbol = SYMBOL_ALIASES.get(symbol, symbol)
        token  = self._tokens.get(symbol)
        if not token:
            token = self._tokens.get(f"{symbol}-EQ")
        if not token:
            # Last resort: case-insensitive scan
            sym_upper = symbol.upper()
            for k, v in self._tokens.items():
                if k.upper() == sym_upper:
                    token = v
                    break
        if not token:
            logger.warning(f"No Angel token for symbol: {symbol}")
        return token

    # ── Historical candles ─────────────────────────────────────────────────
    def get_candles(self, symbol: str, interval: str, exchange: str = "NSE") -> Optional[pd.DataFrame]:
        """
        Fetch OHLCV candles with session-level caching.
        First call: full history. Subsequent calls: last 60 min only (incremental).
        This reduces API calls from ~100-day pulls to tiny top-ups each scan.
        """
        cache_key = f"{symbol}_{interval}"
        angel_interval = INTERVAL_MAP.get(interval, "FIFTEEN_MINUTE")

        if cache_key in self._cache:
            # Incremental: only fetch last 60 minutes
            new_df = self._fetch_range(symbol, angel_interval, exchange, days=1)
            if new_df is not None and not new_df.empty:
                combined = pd.concat([self._cache[cache_key], new_df])
                combined = combined[~combined.index.duplicated(keep="last")].sort_index()
                self._cache[cache_key] = combined
            return self._cache[cache_key]
        else:
            # First call: full history
            max_days = INTERVAL_HISTORY_DAYS.get(angel_interval, 200)
            df = self._fetch_range(symbol, angel_interval, exchange, days=max_days)
            if df is not None:
                self._cache[cache_key] = df
            return df

    def _fetch_range(self, symbol: str, angel_interval: str,
                     exchange: str, days: int) -> Optional[pd.DataFrame]:
        """Core fetch with rate-limit retry. Fetches `days` of candle data."""
        token = self._get_token(symbol)
        if not token:
            return None

        to_date   = datetime.now()
        from_date = to_date - timedelta(days=days)

        params = {
            "exchange":    exchange,
            "symboltoken": token,
            "interval":    angel_interval,
            "fromdate":    from_date.strftime("%Y-%m-%d %H:%M"),
            "todate":      to_date.strftime("%Y-%m-%d %H:%M"),
        }

        for attempt in range(3):
            time.sleep(1.2 + attempt * 2)   # 1.2s, 3.2s, 5.2s
            try:
                resp = self._obj.getCandleData(params)
                if not isinstance(resp, dict):
                    time.sleep(5)
                    continue

                if resp.get("status") and resp.get("data"):
                    df = pd.DataFrame(
                        resp["data"],
                        columns=["date", "open", "high", "low", "close", "volume"],
                    )
                    df["date"] = pd.to_datetime(df["date"])
                    df.set_index("date", inplace=True)
                    return df.astype({"open": float, "high": float, "low": float,
                                      "close": float, "volume": float})

                msg = resp.get("message", "")
                if "Too many" in msg or "AB1021" in str(resp):
                    wait = 20 * (attempt + 1)
                    logger.warning(f"Rate limit for {symbol} — waiting {wait}s (attempt {attempt+1}/3)")
                    time.sleep(wait)
                    continue

                logger.warning(f"No candle data for {symbol}: {msg}")
                return None

            except Exception as e:
                err = str(e)
                if "Access denied" in err or "rate" in err.lower() or "Too many" in err:
                    wait = 20 * (attempt + 1)
                    logger.warning(f"Rate limit (CDN) for {symbol} — waiting {wait}s")
                    time.sleep(wait)
                    continue
                logger.error(f"Angel candle fetch error for {symbol}: {e}")
                return None

        logger.warning(f"Giving up on {symbol} after 3 rate-limit retries")
        return None

    # ── Real-time LTP ──────────────────────────────────────────────────────
    def get_ltp(self, symbols: list[str], exchange: str = "NSE") -> dict[str, float]:
        """
        Real-time LTP with zero delay via Angel One REST API.
        Much faster than yfinance (no 15-min lag).
        """
        result = {}
        for symbol in symbols:
            token = self._get_token(symbol)
            if not token:
                continue
            try:
                resp = self._obj.ltpData(exchange, f"{symbol}-EQ", token)
                if resp.get("status") and resp.get("data"):
                    result[symbol] = float(resp["data"]["ltp"])
            except Exception as e:
                logger.error(f"Angel LTP error for {symbol}: {e}")
        return result

    def get_instrument_token(self, symbol: str, exchange: str = "NSE") -> Optional[str]:
        return self._get_token(symbol)


# ── Optional: Real-time WebSocket price monitor ────────────────────────────
class AngelWebSocketMonitor:
    """
    Subscribes to open positions via WebSocket for instant SL/target triggers.
    Runs in a background thread alongside the main bot.

    Usage:
        monitor = AngelWebSocketMonitor(angel_fetcher, order_mgr)
        monitor.start(["RELIANCE", "TCS"])
        monitor.stop()
    """

    def __init__(self, fetcher: AngelFetcher, order_mgr):
        self.fetcher   = fetcher
        self.order_mgr = order_mgr
        self._ws       = None

    def start(self, symbols: list[str]):
        try:
            from SmartApi.SmartWebSocket import SmartWebSocket
        except ImportError:
            logger.warning("SmartWebSocket not available — falling back to polling")
            return

        token_list = [
            {"exchangeType": 1, "tokens": [self.fetcher._get_token(s)]}
            for s in symbols if self.fetcher._get_token(s)
        ]
        if not token_list:
            return

        def on_data(wsapp, message):
            try:
                token = message.get("token")
                ltp   = float(message.get("last_traded_price", 0)) / 100  # paise → rupees
                # reverse-lookup symbol from token
                sym_map = {v: k for k, v in self.fetcher._tokens.items()}
                symbol  = sym_map.get(token)
                if symbol and ltp > 0:
                    self.order_mgr.check_and_sell({symbol: ltp})
            except Exception as e:
                logger.error(f"WebSocket message error: {e}")

        def on_error(wsapp, error):
            logger.error(f"Angel WebSocket error: {error}")

        def on_close(wsapp):
            logger.info("Angel WebSocket closed")

        self._ws = SmartWebSocket(
            self.fetcher._auth_token,
            self.fetcher.a_cfg["api_key"],
            self.fetcher.a_cfg["client_id"],
            self.fetcher._feed_token,
        )
        self._ws.on_open    = lambda ws: self._ws.subscribe("mws_1", 3, token_list)
        self._ws.on_data    = on_data
        self._ws.on_error   = on_error
        self._ws.on_close   = on_close
        self._ws.connect()
        logger.info(f"✅ Angel WebSocket started for {symbols}")

    def stop(self):
        if self._ws:
            try:
                self._ws.close_connection()
            except Exception:
                pass
