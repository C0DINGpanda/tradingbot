"""
strategy/sector_filter.py — Sector momentum analysis.

Before placing a trade, checks if the stock's sector index supports
the direction:
  LONG  signal → sector must be trending UP today
  SHORT signal → sector must be trending DOWN today

Sector Momentum Score (-2 to +2):
  +2  Strong up  : sector > prev close AND above 20 EMA
  +1  Weak up    : sector > prev close only
   0  Neutral    : flat
  -1  Weak down  : sector < prev close only
  -2  Strong down: sector < prev close AND below 20 EMA

Usage:
  analyzer = SectorAnalyzer(fetcher)
  analyzer.refresh()                       # call once per scan cycle
  ok = analyzer.is_aligned("HDFCBANK", "long")
"""
import logging
from datetime import datetime
from typing import Optional

import pandas as pd

from data.sector_map import SECTOR_INDICES, get_sector, get_sector_index, get_all_sector_indices

logger = logging.getLogger(__name__)


class SectorAnalyzer:
    def __init__(self, fetcher):
        """
        fetcher: YFinanceFetcher or AngelFetcher — same get_candles interface.
        """
        self.fetcher    = fetcher
        self._scores:   dict[str, int]   = {}   # sector → score (-2..+2)
        self._last_refresh: Optional[datetime] = None
        self._refresh_interval_minutes = 15      # re-fetch sector data every 15 min

    # ── Public API ─────────────────────────────────────────────────────────
    def refresh(self, force: bool = False):
        """
        Fetch latest sector index data and compute momentum scores.
        Call once per scan cycle (or when force=True).
        """
        now = datetime.now()
        if (not force
                and self._last_refresh
                and (now - self._last_refresh).total_seconds() < self._refresh_interval_minutes * 60):
            return  # use cached scores

        logger.info("📊 Refreshing sector momentum scores...")
        for sector, yf_ticker in SECTOR_INDICES.items():
            score = self._compute_score(sector, yf_ticker)
            self._scores[sector] = score

        self._last_refresh = now
        self._log_scores()

    def is_aligned(self, symbol: str, direction: str) -> bool:
        """
        Returns True if the symbol's sector momentum supports the trade direction.

        direction="long"  → sector score >= 0  (neutral or bullish)
        direction="short" → sector score <= 0  (neutral or bearish)

        Neutral (0) is allowed in both directions to avoid over-filtering.
        """
        if not self._scores:
            return True   # no data → don't block trade

        sector = get_sector(symbol)
        score  = self._scores.get(sector, self._scores.get("broad", 0))

        if direction == "long":
            aligned = score >= 0
        else:
            aligned = score <= 0

        if not aligned:
            logger.info(
                f"  🚫 Sector filter: {symbol} ({sector}) score={score:+d} "
                f"does not support {direction} — skipping"
            )
        return aligned

    def get_score(self, symbol: str) -> int:
        """Return sector momentum score for a symbol (-2 to +2)."""
        sector = get_sector(symbol)
        return self._scores.get(sector, 0)

    def strongest_sectors(self, top_n: int = 3) -> list[str]:
        """Return top N sector names by momentum (for logging/dashboard)."""
        return sorted(self._scores, key=self._scores.get, reverse=True)[:top_n]

    def weakest_sectors(self, top_n: int = 3) -> list[str]:
        """Return bottom N sector names by momentum."""
        return sorted(self._scores, key=self._scores.get)[:top_n]

    def summary(self) -> str:
        if not self._scores:
            return "No sector data"
        lines = [f"  {s:<12} {'+' if v>=0 else ''}{v:d}" for s, v in
                 sorted(self._scores.items(), key=lambda x: -x[1])]
        return "Sector Scores:\n" + "\n".join(lines)

    # ── Internal ───────────────────────────────────────────────────────────
    def _compute_score(self, sector: str, yf_ticker: str) -> int:
        """
        Fetch daily candles for the sector index and compute score.
        Score logic:
          prev_close = yesterday's close
          today_open = today's first candle close (or current price)
          ema20      = 20-day EMA of close

          score = 0
          if today > prev_close: score += 1
          if today > ema20:      score += 1
          if today < prev_close: score -= 1
          if today < ema20:      score -= 1
        """
        try:
            # Use the fetcher — works with both yfinance and Angel
            # For yfinance: sector indices need to be fetched as-is (^NSEBANK etc.)
            df = self._fetch_sector(yf_ticker)
            if df is None or len(df) < 22:
                return 0

            prev_close = float(df["close"].iloc[-2])
            today      = float(df["close"].iloc[-1])
            ema20      = float(df["close"].ewm(span=20, adjust=False).mean().iloc[-1])

            score = 0
            if today > prev_close: score += 1
            if today > ema20:      score += 1
            if today < prev_close: score -= 1
            if today < ema20:      score -= 1

            return score

        except Exception as e:
            logger.debug(f"Sector score error for {sector} ({yf_ticker}): {e}")
            return 0

    def _fetch_sector(self, yf_ticker: str) -> Optional[pd.DataFrame]:
        """Fetch daily candles for a sector index."""
        try:
            import yfinance as yf
            df = yf.download(yf_ticker, period="30d", interval="1d",
                             auto_adjust=True, progress=False)
            if df.empty:
                return None
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df.columns = [c.lower() for c in df.columns]
            return df
        except Exception as e:
            logger.debug(f"Sector fetch error for {yf_ticker}: {e}")
            return None

    def _log_scores(self):
        if not self._scores:
            return
        top    = self.strongest_sectors(3)
        bottom = self.weakest_sectors(3)
        logger.info(
            f"Sector momentum — Strong: {top} | Weak: {bottom}"
        )
