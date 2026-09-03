"""
strategy/orb.py — Opening Range Breakout (ORB)

The #1 professional intraday strategy:

  09:15 ─────────────────────────── opening range high
            (first N minutes)
  09:15 ─────────────────────────── opening range low

  After range is set:
    Price > ORB high + volume spike  →  BUY  (long)
    Price < ORB low  + volume spike  →  SHORT

The opening range (usually first 15 or 30 minutes) captures the
initial sentiment. A breakout with volume = strong directional move.
"""
import logging
from datetime import datetime, time as dtime

import pandas as pd

from strategy.breakout import BreakoutSignal

logger = logging.getLogger(__name__)


def _today_candles(df: pd.DataFrame) -> pd.DataFrame:
    """Filter df to today's session candles only."""
    today = datetime.now().date()
    mask  = df.index.date == today
    return df[mask]


def get_opening_range(df: pd.DataFrame, orb_minutes: int = 15) -> tuple[float, float] | None:
    """
    Returns (orb_high, orb_low) from the first `orb_minutes` of today's session.
    Returns None if not enough candles yet.
    """
    today_df = _today_candles(df)
    if today_df.empty:
        return None

    # Market opens at 09:15; ORB ends at 09:15 + orb_minutes
    market_open = dtime(9, 15)
    orb_end_min = 15 + orb_minutes
    orb_end     = dtime(9, orb_end_min) if orb_end_min < 60 else dtime(10, orb_end_min - 60)

    orb_candles = today_df[today_df.index.time <= orb_end]
    if orb_candles.empty:
        return None

    return float(orb_candles["high"].max()), float(orb_candles["low"].min())


def check_orb_long(
    df: pd.DataFrame,
    symbol: str,
    orb_minutes: int = 15,
    volume_multiplier: float = 1.5,
    min_breakout_pct: float = 0.2,
    max_entry_delay_minutes: int = 30,
) -> BreakoutSignal:
    """
    LONG signal: current candle closes above opening range high with volume.

    Requirements:
      1. Opening range must be established (orb_minutes have passed)
      2. Current time is AFTER ORB period
      3. Close > ORB high by min_breakout_pct%
      4. Volume >= volume_multiplier × previous N candles average
    """
    orb = get_opening_range(df, orb_minutes)
    if orb is None:
        return BreakoutSignal(False, "orb_long", symbol, 0)

    orb_high, orb_low = orb
    today_df   = _today_candles(df)

    # Must be past the ORB window but within max_entry_delay_minutes
    now = datetime.now().time()
    orb_end_min = 15 + orb_minutes
    orb_end = dtime(9, orb_end_min) if orb_end_min < 60 else dtime(10, orb_end_min - 60)
    if now <= orb_end:
        return BreakoutSignal(False, "orb_long", symbol, 0)

    # Block entries too far after ORB — stale breakouts have low follow-through
    from datetime import datetime as _dt, timedelta
    orb_end_dt  = _dt.combine(_dt.today(), orb_end)
    max_entry_t = (orb_end_dt + timedelta(minutes=max_entry_delay_minutes)).time()
    if now > max_entry_t:
        logger.debug(f"  {symbol} ORB long skipped — entry too late ({now} > max {max_entry_t})")
        return BreakoutSignal(False, "orb_long", symbol, 0)

    if today_df.empty:
        return BreakoutSignal(False, "orb_long", symbol, 0)

    current_candle = today_df.iloc[-1]
    close          = float(current_candle["close"])
    cur_volume     = float(current_candle["volume"])

    # ── Candle close confirmation ──────────────────────────────────────────
    # Require the PREVIOUS completed candle to have closed above ORB high.
    # This filters fake breakouts where price only poked above ORB for a
    # few seconds before reversing (bull trap).
    if len(today_df) < 2:
        return BreakoutSignal(False, "orb_long", symbol, close)
    prev_candle       = today_df.iloc[-2]
    prev_close        = float(prev_candle["close"])
    prev_volume       = float(prev_candle["volume"])
    candle_confirmed  = prev_close > orb_high   # previous candle closed above ORB
    still_above       = close > orb_high         # current price hasn't reversed

    if not candle_confirmed or not still_above:
        return BreakoutSignal(False, "orb_long", symbol, close)

    breakout_pct   = ((prev_close - orb_high) / orb_high) * 100
    if breakout_pct < min_breakout_pct:
        return BreakoutSignal(False, "orb_long", symbol, close)

    # Volume check on the breakout candle (prev) and current candle
    prev_df    = df[df.index.date < datetime.now().date()]
    avg_volume = float(prev_df["volume"].tail(50).mean()) if not prev_df.empty else 0
    vol_ratio  = max(prev_volume, cur_volume) / avg_volume if avg_volume > 0 else 0

    # ── Reversal candle filter ─────────────────────────────────────────────
    # If a high-volume BEARISH candle appeared after the breakout, skip the long.
    post_breakout = today_df[today_df.index.time > orb_end]
    reversal_detected = False
    if avg_volume > 0 and len(post_breakout) >= 2:
        for _, candle in post_breakout.iterrows():
            is_bearish       = float(candle["close"]) < float(candle["open"])
            candle_vol_ratio = float(candle["volume"]) / avg_volume
            if is_bearish and candle_vol_ratio >= 2.0:
                reversal_detected = True
                logger.info(
                    f"  🚫 {symbol} ORB long blocked — reversal candle detected "
                    f"(bearish vol×{candle_vol_ratio:.1f} after breakout)"
                )
                break

    triggered  = vol_ratio >= volume_multiplier and not reversal_detected

    return BreakoutSignal(
        triggered=triggered,
        signal_type="orb_long",
        symbol=symbol,
        current_price=close,
        resistance_level=orb_high,
        volume_ratio=round(vol_ratio, 2),
        details={
            "orb_high": round(orb_high, 2),
            "orb_low":  round(orb_low, 2),
            "breakout_pct": round(breakout_pct, 2),
            "volume_ratio": round(vol_ratio, 2),
            "candle_confirmed": candle_confirmed,
            "prev_close": round(prev_close, 2),
            "orb_minutes": orb_minutes,
            "direction": "long",
        },
    )


def check_orb_short(
    df: pd.DataFrame,
    symbol: str,
    orb_minutes: int = 15,
    volume_multiplier: float = 1.5,
    min_breakdown_pct: float = 0.2,
    max_entry_delay_minutes: int = 30,
) -> BreakoutSignal:
    """
    SHORT signal: current candle closes below opening range low with volume.
    """
    orb = get_opening_range(df, orb_minutes)
    if orb is None:
        return BreakoutSignal(False, "orb_short", symbol, 0)

    orb_high, orb_low = orb
    today_df = _today_candles(df)

    now = datetime.now().time()
    orb_end_min = 15 + orb_minutes
    orb_end = dtime(9, orb_end_min) if orb_end_min < 60 else dtime(10, orb_end_min - 60)
    if now <= orb_end:
        return BreakoutSignal(False, "orb_short", symbol, 0)

    # Block entries too far after ORB — stale breakdowns have low follow-through
    from datetime import datetime as _dt, timedelta
    orb_end_dt  = _dt.combine(_dt.today(), orb_end)
    max_entry_t = (orb_end_dt + timedelta(minutes=max_entry_delay_minutes)).time()
    if now > max_entry_t:
        logger.debug(f"  {symbol} ORB short skipped — entry too late ({now} > max {max_entry_t})")
        return BreakoutSignal(False, "orb_short", symbol, 0)

    if today_df.empty:
        return BreakoutSignal(False, "orb_short", symbol, 0)

    current_candle = today_df.iloc[-1]
    close          = float(current_candle["close"])
    cur_volume     = float(current_candle["volume"])

    # ── Candle close confirmation ──────────────────────────────────────────
    # Require the PREVIOUS completed candle to have closed below ORB low.
    # Filters bear traps where price only poked below ORB for a few seconds.
    if len(today_df) < 2:
        return BreakoutSignal(False, "orb_short", symbol, close)
    prev_candle      = today_df.iloc[-2]
    prev_close       = float(prev_candle["close"])
    prev_volume      = float(prev_candle["volume"])
    candle_confirmed = prev_close < orb_low    # previous candle closed below ORB
    still_below      = close < orb_low          # current price hasn't reversed

    if not candle_confirmed or not still_below:
        return BreakoutSignal(False, "orb_short", symbol, close)

    breakdown_pct = ((orb_low - prev_close) / orb_low) * 100
    if breakdown_pct < min_breakdown_pct:
        return BreakoutSignal(False, "orb_short", symbol, close)

    prev_df    = df[df.index.date < datetime.now().date()]
    avg_volume = float(prev_df["volume"].tail(50).mean()) if not prev_df.empty else 0
    vol_ratio  = max(prev_volume, cur_volume) / avg_volume if avg_volume > 0 else 0

    # ── Reversal candle filter ─────────────────────────────────────────────
    # If a high-volume BULLISH candle appeared after the breakdown, the move
    # is likely exhausted / reversing — skip the short.
    # e.g. GNFC: broke down at 9:35, then 9:45 saw a massive green candle
    # (volume 55k vs avg 15k) — entering short after that = chasing a dead move.
    post_breakdown = today_df[today_df.index.time > orb_end]
    reversal_detected = False
    if avg_volume > 0 and len(post_breakdown) >= 2:
        for _, candle in post_breakdown.iterrows():
            is_bullish     = float(candle["close"]) > float(candle["open"])
            candle_vol_ratio = float(candle["volume"]) / avg_volume
            if is_bullish and candle_vol_ratio >= 2.0:
                reversal_detected = True
                logger.info(
                    f"  🚫 {symbol} ORB short blocked — reversal candle detected "
                    f"(bullish vol×{candle_vol_ratio:.1f} after breakdown)"
                )
                break

    triggered = vol_ratio >= volume_multiplier and not reversal_detected

    return BreakoutSignal(
        triggered=triggered,
        signal_type="orb_short",
        symbol=symbol,
        current_price=close,
        resistance_level=orb_low,
        volume_ratio=round(vol_ratio, 2),
        details={
            "orb_high": round(orb_high, 2),
            "orb_low":  round(orb_low, 2),
            "breakdown_pct": round(breakdown_pct, 2),
            "volume_ratio": round(vol_ratio, 2),
            "candle_confirmed": candle_confirmed,
            "prev_close": round(prev_close, 2),
            "orb_minutes": orb_minutes,
            "direction": "short",
        },
    )
