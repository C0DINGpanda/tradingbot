"""
strategy/market_filter.py — Market-wide filters applied before every signal.

India VIX Filter:
  - VIX > 22  → BLOCK all breakout signals (panic market, breakouts fail ~70%)
  - VIX 18-22 → reduce position sizes (volatile but tradeable)
  - VIX < 13  → reduce sizes (too calm, breakouts lack follow-through)
  - VIX 13-18 → ideal trading zone

Nifty 50 Trend Filter:
  - Nifty < -0.5% from open → BLOCK all LONG signals (don't fight the market)
  - Nifty > +0.5% from open → BLOCK all SHORT signals
"""
import logging
from datetime import datetime, date
from typing import Optional

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

_NIFTY_TICKER = "^NSEI"
_VIX_TICKER   = "^INDIAVIX"

# Cache refreshed once per scan cycle (5 min TTL)
_cache: dict = {
    "vix": None,
    "nifty_open": None,
    "nifty_ltp": None,
    "date": None,
    "last_ts": None,
}
_CACHE_TTL_SECONDS = 300


def _cache_stale() -> bool:
    if _cache["date"] != date.today():
        return True
    if _cache["last_ts"] is None:
        return True
    elapsed = (datetime.now() - _cache["last_ts"]).total_seconds()
    return elapsed > _CACHE_TTL_SECONDS


def refresh():
    """Fetch fresh India VIX and Nifty data. Called once per scan cycle."""
    try:
        # India VIX — daily close is fine for intraday decisions
        vix_data = yf.download(_VIX_TICKER, period="2d", interval="1d", progress=False)
        if not vix_data.empty:
            close_col = "Close" if "Close" in vix_data.columns else vix_data.columns[3]
            series = vix_data[close_col]
            if hasattr(series, "iloc"):
                val = series.iloc[-1]
                _cache["vix"] = float(val.iloc[0] if hasattr(val, "iloc") else val)
    except Exception as e:
        logger.warning(f"India VIX fetch failed: {e}")

    try:
        nifty_data = yf.download(_NIFTY_TICKER, period="1d", interval="5m", progress=False)
        if not nifty_data.empty:
            # Flatten multi-level columns if present
            if isinstance(nifty_data.columns, pd.MultiIndex):
                nifty_data.columns = [c[0] for c in nifty_data.columns]
            today_df = nifty_data[nifty_data.index.date == date.today()]
            if not today_df.empty:
                _cache["nifty_open"] = float(today_df["Open"].iloc[0])
                _cache["nifty_ltp"]  = float(today_df["Close"].iloc[-1])
    except Exception as e:
        logger.warning(f"Nifty data fetch failed: {e}")

    _cache["date"]    = date.today()
    _cache["last_ts"] = datetime.now()
    logger.info(
        f"MarketFilter — VIX={_cache['vix']} "
        f"Nifty open={_cache['nifty_open']} ltp={_cache['nifty_ltp']}"
    )


def get_vix() -> Optional[float]:
    if _cache_stale():
        refresh()
    return _cache["vix"]


def get_nifty_change_pct() -> Optional[float]:
    """Returns Nifty % change from today's open. Positive = up."""
    if _cache_stale():
        refresh()
    o = _cache["nifty_open"]
    l = _cache["nifty_ltp"]
    if o and l and o > 0:
        return round((l - o) / o * 100, 3)
    return None


class MarketFilter:
    def __init__(self, cfg: dict):
        mf_cfg = cfg.get("market_filters", {})
        vix_cfg = mf_cfg.get("india_vix", {})
        nf_cfg  = mf_cfg.get("nifty_trend", {})

        self.vix_enabled      = vix_cfg.get("enabled", True)
        self.vix_block_above  = vix_cfg.get("block_above", 22)
        self.vix_reduce_above = vix_cfg.get("reduce_size_above", 18)
        self.vix_reduce_below = vix_cfg.get("reduce_size_below", 13)
        self.vix_size_factor  = vix_cfg.get("reduce_size_factor", 0.5)

        self.nifty_enabled     = nf_cfg.get("enabled", True)
        self.nifty_long_block  = nf_cfg.get("block_longs_below_pct", -0.5)
        self.nifty_short_block = nf_cfg.get("block_shorts_above_pct", 0.5)
        # ORB-specific: minimum Nifty move required for ORB signals to be valid
        # If Nifty is flat (between -min and +min), ORB breakouts lack follow-through
        self.orb_min_move      = nf_cfg.get("min_move_for_orb_pct", 0.3)
        # Extend the same flat-market gate to ALL breakout-type strategies, not
        # just ORB. On flat/choppy days (Sep 2026 regime), bb_squeeze, gap_and_go,
        # volume_breakout/breakdown etc. all lack follow-through the same way ORB
        # does — they were quietly bleeding money while ORB sat correctly blocked.
        # ema_pullback is exempt: it's a trend-CONTINUATION signal (confirms an
        # already-established move), not a fresh breakout needing index confirmation.
        self.flat_market_block_all = nf_cfg.get("flat_market_block_all_breakouts", True)
        self.flat_market_exempt = set(nf_cfg.get(
            "flat_market_exempt_signals",
            ["ema_pullback_long", "ema_pullback_short"],
        ))

    def should_block(self, direction: str, signal_type: str = "") -> tuple[bool, str]:
        """
        Returns (blocked, reason).
        blocked=True means skip this signal entirely.
        """
        if self.vix_enabled:
            vix = get_vix()
            if vix is not None and vix > self.vix_block_above:
                return True, f"India VIX={vix:.1f} > {self.vix_block_above} — market panic, skipping breakouts"

        if self.nifty_enabled:
            chg = get_nifty_change_pct()
            if chg is not None:
                # Hard directional blocks (extreme moves)
                if direction == "long" and chg < self.nifty_long_block:
                    return True, f"Nifty {chg:+.2f}% — blocking longs on down market"
                if direction == "short" and chg > self.nifty_short_block:
                    return True, f"Nifty {chg:+.2f}% — blocking shorts on up market"

                # Flat-market filter — skip breakout-type signals if Nifty hasn't
                # moved enough (ORB always gated; other breakout strategies gated
                # too when flat_market_block_all is enabled). Continuation signals
                # like ema_pullback are exempt.
                is_orb = signal_type in ("orb_long", "orb_short")
                is_gated_breakout = is_orb or (
                    self.flat_market_block_all and signal_type not in self.flat_market_exempt
                )
                if is_gated_breakout and abs(chg) < self.orb_min_move:
                    label = "ORB" if is_orb else signal_type
                    return True, (
                        f"Nifty flat ({chg:+.2f}%, need ±{self.orb_min_move}%) "
                        f"— {label} signals unreliable on flat days"
                    )

        return False, ""

    def size_factor(self) -> float:
        """
        Returns a multiplier (0–1) to apply to position size.
        1.0 = normal, 0.5 = half size (high or low VIX regime).
        """
        if not self.vix_enabled:
            return 1.0
        vix = get_vix()
        if vix is None:
            return 1.0
        # Only reduce size in HIGH-VIX (volatile/panic) — low VIX means calm market, full size is fine
        if vix > self.vix_reduce_above:
            return self.vix_size_factor
        return 1.0

    def log_status(self):
        vix = get_vix()
        chg = get_nifty_change_pct()
        if vix:
            regime = "🔴 PANIC" if vix > self.vix_block_above else \
                     "🟡 VOLATILE" if vix > self.vix_reduce_above else \
                     "🟡 LOW-VOL" if vix < self.vix_reduce_below else "🟢 IDEAL"
            logger.info(f"  VIX={vix:.1f} [{regime}]  Nifty={chg:+.2f}%" if chg else f"  VIX={vix:.1f} [{regime}]")
