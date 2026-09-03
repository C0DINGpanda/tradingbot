"""
data/watchlist_builder.py — Pre-market smart screener.

Runs before 9:15 AM to auto-build today's watchlist by:
  1. Getting candidate universe (from yfinance or Kite)
  2. Filtering by liquidity, trend, and momentum criteria
  3. Saving top candidates to dynamic_watchlist.txt
"""
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Optional, Union

import pandas as pd

logger = logging.getLogger(__name__)

DYNAMIC_WATCHLIST_FILE = os.path.join(
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
    "dynamic_watchlist.txt",
)


class WatchlistBuilder:
    def __init__(self, fetcher, cfg: dict):
        """
        fetcher: YFinanceFetcher or DataFetcher (Kite) — same interface.
        """
        self.fetcher       = fetcher
        self.cfg           = cfg
        self.w_cfg         = cfg.get("watchlist_builder", {})
        self._sector_scores: dict = {}   # sector → score, populated lazily

    def build(self) -> list[str]:
        logger.info("🔍 Building smart watchlist...")

        # Pre-load sector momentum so _compute_score can apply bonus
        self._load_sector_scores()

        # Fetch today's ban/ASM/GSM lists
        from data.ban_filter import get_ban_filter
        ban = get_ban_filter()
        ban.refresh()

        candidates = self._get_candidate_universe()
        if not candidates:
            logger.warning("No candidates — falling back to config watchlist")
            return self.cfg.get("watchlist", [])

        logger.info(f"Universe: {len(candidates)} stocks to screen")

        scored = self._score_candidates(candidates)
        top_n  = self.w_cfg.get("max_watchlist_size", 30)
        top    = scored.head(top_n)["symbol"].tolist()

        # Remove any banned/restricted stocks
        top = get_ban_filter().filter(top)

        self._save(top)
        logger.info(f"✅ Watchlist ready: {len(top)} stocks → {top}")
        return top

    # ── Universe ───────────────────────────────────────────────────────────
    def _get_candidate_universe(self) -> list[str]:
        """Return list of NSE symbols to screen."""
        from data.nse_symbols import NIFTY_500
        universe_size = self.w_cfg.get("universe", "nifty500")
        if universe_size == "nifty50":
            from data.nse_symbols import get_nifty50
            return get_nifty50()
        elif universe_size == "nifty100":
            from data.nse_symbols import get_nifty100
            return get_nifty100()
        return NIFTY_500

    # ── Score candidates ───────────────────────────────────────────────────
    def _score_candidates(self, symbols: list[str]) -> pd.DataFrame:
        min_price     = self.w_cfg.get("min_price", 50)
        max_price     = self.w_cfg.get("max_price", 10000)
        min_avg_vol   = self.w_cfg.get("min_avg_daily_volume", 200000)
        ema_period    = self.w_cfg.get("ema_period", 200)
        rs_period     = self.w_cfg.get("rs_lookback_days", 90)
        batch_size    = self.w_cfg.get("batch_size", 20)
        batch_delay   = self.w_cfg.get("batch_delay_seconds", 1.0)

        rows  = []
        total = len(symbols)

        # Process in batches — fetch each batch in parallel via yfinance multi-ticker
        import yfinance as yf

        for batch_start in range(0, total, batch_size):
            batch = symbols[batch_start: batch_start + batch_size]
            try:
                # Fetch all tickers in batch in one API call
                tickers = [f"{s}.NS" for s in batch]
                raw = yf.download(
                    tickers,
                    period="400d",
                    interval="1d",
                    auto_adjust=True,
                    progress=False,
                    group_by="ticker",
                )
                for symbol, ticker in zip(batch, tickers):
                    try:
                        if len(batch) == 1:
                            df = raw.copy()
                        else:
                            df = raw[ticker].copy() if ticker in raw.columns.get_level_values(0) else None
                        if df is None or df.empty or len(df) < 50:
                            continue
                        # Flatten columns
                        if isinstance(df.columns, pd.MultiIndex):
                            df.columns = [c[0].lower() for c in df.columns]
                        else:
                            df.columns = [c.lower() for c in df.columns]
                        df = df[["open", "high", "low", "close", "volume"]].dropna()
                        if len(df) < 50:
                            continue
                        score, meta = self._compute_score(
                            df, symbol, min_price, max_price, min_avg_vol, ema_period, rs_period
                        )
                        if score > 0:
                            rows.append({"symbol": symbol, "score": score, **meta})
                    except Exception as e:
                        logger.debug(f"  Skipping {symbol}: {e}")

            except Exception as e:
                logger.warning(f"Batch {batch_start//batch_size + 1} failed: {e}")

            done = min(batch_start + batch_size, total)
            logger.info(f"  Screened {done}/{total} — {len(rows)} passing so far...")
            time.sleep(batch_delay)

        result = pd.DataFrame(rows)
        if result.empty:
            return result
        return result.sort_values("score", ascending=False).reset_index(drop=True)

    def _compute_score(self, df, symbol, min_price, max_price,
                       min_avg_vol, ema_period, rs_period):
        close   = df["close"]
        volume  = df["volume"]
        current = float(close.iloc[-1])

        if not (min_price <= current <= max_price):
            return 0, {}

        avg_vol_50 = float(volume.tail(50).mean())
        if avg_vol_50 < min_avg_vol:
            return 0, {}

        score = 4   # base: price range + liquidity OK
        meta  = {"price": round(current, 2), "avg_vol_50d": int(avg_vol_50)}

        if len(df) >= ema_period:
            ema200 = float(close.ewm(span=ema_period, adjust=False).mean().iloc[-1])
            meta["ema200"] = round(ema200, 2)
            meta["above_ema200"] = current > ema200
            if current > ema200:
                score += 2

        high_52w      = float(df["high"].tail(min(252, len(df))).max())
        pct_from_high = (high_52w - current) / high_52w * 100
        meta["pct_from_52w_high"] = round(pct_from_high, 2)
        if pct_from_high <= 10:
            score += 2

        recent_vol = float(volume.tail(5).mean())
        vol_ratio  = recent_vol / avg_vol_50 if avg_vol_50 > 0 else 0
        meta["recent_vol_ratio"] = round(vol_ratio, 2)
        if vol_ratio >= 1.5:
            score += 2

        # Sector momentum bonus
        sector_bonus = self.cfg.get("sector_filter", {}).get("sector_bonus", 2)
        if sector_bonus > 0:
            from data.sector_map import get_sector
            sector = get_sector(symbol)
            sec_score = self._sector_scores.get(sector, 0)
            if sec_score >= 1:    # sector trending up → bonus
                score += sector_bonus
                meta["sector_bonus"] = sector_bonus
            elif sec_score <= -1: # sector trending down → slight penalty
                score = max(0, score - 1)

        return score, meta

    def _load_sector_scores(self):
        """Fetch sector momentum scores before pre-market screening."""
        try:
            from strategy.sector_filter import SectorAnalyzer
            analyzer = SectorAnalyzer(self.fetcher)
            analyzer.refresh(force=True)
            # Store {sector: score} for use in _compute_score
            from data.sector_map import SECTOR_INDICES
            for sector in SECTOR_INDICES:
                self._sector_scores[sector] = analyzer._scores.get(sector, 0)
            logger.info(f"Sector scores loaded for watchlist: {self._sector_scores}")
        except Exception as e:
            logger.warning(f"Could not load sector scores: {e}")

    # ── Kite-specific (legacy, only used if data_source=kite) ─────────────
    def _fetch_daily_by_token(self, token: int, days: int = 400):
        return None  # handled by fetcher.get_candles now

    # ── Save / Load ────────────────────────────────────────────────────────
    def _save(self, symbols: list[str]):
        with open(DYNAMIC_WATCHLIST_FILE, "w") as f:
            f.write(f"# Generated: {datetime.now().isoformat()}\n")
            f.write("\n".join(symbols))
        logger.info(f"Saved to {DYNAMIC_WATCHLIST_FILE}")


def load_dynamic_watchlist(fallback: list[str]) -> list[str]:
    try:
        with open(DYNAMIC_WATCHLIST_FILE) as f:
            lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]
        if lines:
            logger.info(f"Loaded dynamic watchlist: {len(lines)} stocks")
            return lines
    except FileNotFoundError:
        pass
    logger.info("No dynamic watchlist found — using config watchlist")
    return fallback
