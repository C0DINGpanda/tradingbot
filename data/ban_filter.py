"""
data/ban_filter.py — Filters stocks restricted for intraday trading on NSE.

Three restriction categories:
  1. F&O Ban Period   — NSE bans new F&O positions; cash market trades are
                        risky (no hedging possible), skip these.
  2. ASM Stage II+    — Additional Surveillance Measure; brokers often block
                        intraday or require 100% margin. Skip.
  3. GSM              — Graded Surveillance (severe). Always skip.
  4. T2T / BE series  — Trade-to-Trade, delivery only. Filtered earlier via
                        symbol suffix in WatchlistBuilder universe.

Sources: NSE India public JSON APIs (fetched once per day, cached to disk).
NSE requires a browser-like session (cookie handshake) before API calls.
Falls back to yesterday's cache if network fails.

Usage:
    ban = BanFilter()
    ban.refresh()                    # call once at start of day
    if not ban.is_tradeable("XYZ"):
        skip...
    clean_list = ban.filter(watchlist)
"""
import json
import logging
import time
from datetime import date
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

# ── NSE API endpoints ──────────────────────────────────────────────────────
NSE_HOME        = "https://www.nseindia.com"
NSE_FO_BAN_URL  = "https://www.nseindia.com/api/fo-ban-underlyings"
NSE_ASM_URL     = "https://www.nseindia.com/api/reportASM"
NSE_GSM_URL     = "https://www.nseindia.com/api/reportGSM"

CACHE_FILE = Path("ban_list_cache.json")

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer":         "https://www.nseindia.com/",
    "Connection":      "keep-alive",
}


class BanFilter:
    def __init__(self):
        self._fo_ban: set[str] = set()   # F&O ban period
        self._asm:    set[str] = set()   # ASM (Additional Surveillance)
        self._gsm:    set[str] = set()   # GSM (Graded Surveillance)
        self._fetch_date: date | None = None
        self._session = requests.Session()
        self._session.headers.update(BROWSER_HEADERS)

    # ── Public API ─────────────────────────────────────────────────────────
    def refresh(self, force: bool = False):
        """
        Fetch today's ban/ASM/GSM lists from NSE.
        Skips if already fetched today (unless force=True).
        Falls back to disk cache if network unavailable.
        """
        today = date.today()
        if not force and self._fetch_date == today:
            return

        logger.info("🚫 Fetching NSE ban/ASM/GSM lists...")
        self._init_nse_session()

        fo_ok  = self._fetch_fo_ban()
        asm_ok = self._fetch_asm()
        gsm_ok = self._fetch_gsm()

        if fo_ok or asm_ok or gsm_ok:
            self._fetch_date = today
            self._save_cache()
            logger.info(
                f"Ban lists loaded — F&O ban: {len(self._fo_ban)}, "
                f"ASM: {len(self._asm)}, GSM: {len(self._gsm)}"
            )
        else:
            logger.warning("All NSE fetches failed — trying disk cache")
            self._load_cache()

        total = len(self.all_restricted())
        if total:
            logger.info(f"  Restricted today: {sorted(self.all_restricted())}")

    def is_tradeable(self, symbol: str) -> bool:
        """
        Returns True if the stock can be traded intraday today.
        Returns False (and logs why) if it's restricted.
        """
        if symbol in self._gsm:
            logger.debug(f"  🚫 {symbol} in GSM list — skip")
            return False
        if symbol in self._asm:
            logger.debug(f"  ⚠️  {symbol} in ASM list — skip")
            return False
        if symbol in self._fo_ban:
            logger.debug(f"  🔴 {symbol} in F&O ban — skip")
            return False
        return True

    def filter(self, symbols: list[str]) -> list[str]:
        """Remove restricted symbols from a watchlist."""
        clean = [s for s in symbols if self.is_tradeable(s)]
        removed = len(symbols) - len(clean)
        if removed:
            logger.info(f"  Ban filter removed {removed} stocks from watchlist")
        return clean

    def all_restricted(self) -> set[str]:
        return self._fo_ban | self._asm | self._gsm

    # ── NSE fetch helpers ─────────────────────────────────────────────────
    def _init_nse_session(self):
        """
        Hit the NSE homepage first to pick up cookies.
        NSE APIs return 403/empty without a valid session cookie.
        """
        try:
            self._session.get(NSE_HOME, timeout=10)
            time.sleep(0.5)   # brief pause before API calls
        except Exception as e:
            logger.debug(f"NSE session init: {e}")

    def _fetch_fo_ban(self) -> bool:
        """
        F&O Ban Period list — stocks in ban period cannot have NEW F&O positions.
        Response format: {"data": [{"symbol": "BANDHANBNK", ...}, ...]}
        """
        try:
            r = self._session.get(NSE_FO_BAN_URL, timeout=10)
            if r.status_code != 200:
                logger.debug(f"F&O ban: HTTP {r.status_code}")
                return False
            data = r.json()
            if isinstance(data, dict):
                items = data.get("data", [])
            else:
                items = data   # sometimes a direct list
            self._fo_ban = {
                item.get("symbol", item) if isinstance(item, dict) else item
                for item in items
                if item
            }
            return True
        except Exception as e:
            logger.debug(f"F&O ban fetch error: {e}")
            return False

    def _fetch_asm(self) -> bool:
        """
        ASM list — stocks under Additional Surveillance Measure.
        NSE applies Stage I (warning) or Stage II+ (broker restrictions).
        We block ALL ASM stages for safety.
        Response format: {"data": [{"symbol": "XYZ", "stage": "Stage II", ...}]}
        """
        try:
            r = self._session.get(NSE_ASM_URL, timeout=10)
            if r.status_code != 200:
                return False
            data = r.json()
            items = data.get("data", []) if isinstance(data, dict) else data
            self._asm = {
                item.get("symbol", "")
                for item in items
                if isinstance(item, dict) and item.get("symbol")
            }
            return True
        except Exception as e:
            logger.debug(f"ASM fetch error: {e}")
            return False

    def _fetch_gsm(self) -> bool:
        """
        GSM list — Graded Surveillance Measure.
        Very high-risk stocks; some in Stage V/VI can't even be bought.
        """
        try:
            r = self._session.get(NSE_GSM_URL, timeout=10)
            if r.status_code != 200:
                return False
            data = r.json()
            items = data.get("data", []) if isinstance(data, dict) else data
            self._gsm = {
                item.get("symbol", "")
                for item in items
                if isinstance(item, dict) and item.get("symbol")
            }
            return True
        except Exception as e:
            logger.debug(f"GSM fetch error: {e}")
            return False

    # ── Cache ─────────────────────────────────────────────────────────────
    def _save_cache(self):
        try:
            cache = {
                "date":   date.today().isoformat(),
                "fo_ban": sorted(self._fo_ban),
                "asm":    sorted(self._asm),
                "gsm":    sorted(self._gsm),
            }
            CACHE_FILE.write_text(json.dumps(cache, indent=2))
        except Exception as e:
            logger.debug(f"Cache save error: {e}")

    def _load_cache(self):
        try:
            if not CACHE_FILE.exists():
                return
            cache = json.loads(CACHE_FILE.read_text())
            cached_date = cache.get("date", "")
            self._fo_ban = set(cache.get("fo_ban", []))
            self._asm    = set(cache.get("asm",    []))
            self._gsm    = set(cache.get("gsm",    []))
            freshness = "today" if cached_date == date.today().isoformat() else f"from {cached_date}"
            logger.info(
                f"Loaded ban cache ({freshness}): "
                f"F&O ban={len(self._fo_ban)}, ASM={len(self._asm)}, GSM={len(self._gsm)}"
            )
        except Exception as e:
            logger.warning(f"Could not load ban cache: {e}")


# ── Module-level singleton (shared across screener + watchlist builder) ────
_ban_filter: BanFilter | None = None


def get_ban_filter() -> BanFilter:
    """Return the shared BanFilter instance (lazy init)."""
    global _ban_filter
    if _ban_filter is None:
        _ban_filter = BanFilter()
    return _ban_filter
