"""
strategy/gap.py — Gap & Go strategy for intraday trading.

Gap & Go: stock opens with a significant gap from previous close,
first candle confirms the direction with volume → trade the momentum.

  Gap Up   (>1%) + bullish first candle + volume → LONG
  Gap Down (>1%) + bearish first candle + volume → SHORT

This is one of the highest win-rate intraday setups on NSE, especially
in the 9:15–10:30 AM window when institutional momentum is strongest.
"""
import logging
from datetime import datetime, time as dtime

import pandas as pd

from strategy.breakout import BreakoutSignal

logger = logging.getLogger(__name__)


def _today_candles(df: pd.DataFrame) -> pd.DataFrame:
    today = datetime.now().date()
    return df[df.index.date == today]


def _prev_close(df: pd.DataFrame) -> float | None:
    """Last close from the previous trading session."""
    today = datetime.now().date()
    prev = df[df.index.date < today]
    if prev.empty:
        return None
    return float(prev.iloc[-1]["close"])


def check_gap_and_go_long(
    df: pd.DataFrame,
    symbol: str,
    min_gap_pct: float = 1.0,
    max_gap_pct: float = 5.0,
    volume_multiplier: float = 1.5,
) -> BreakoutSignal:
    """
    GAP & GO LONG:
      1. Today's open is >min_gap_pct% above yesterday's close (gap up)
      2. Gap is not too large (<=max_gap_pct) — huge gaps often reverse
      3. First 5-min candle (9:15) closes ABOVE its open (bullish candle)
      4. Current price is still above today's open (gap holding)
      5. Volume is elevated — confirms institutional participation
    """
    today_df = _today_candles(df)
    if today_df.empty or len(today_df) < 2:
        return BreakoutSignal(False, "gap_and_go_long", symbol, 0)

    prev_close = _prev_close(df)
    if not prev_close or prev_close <= 0:
        return BreakoutSignal(False, "gap_and_go_long", symbol, 0)

    today_open   = float(today_df.iloc[0]["open"])
    gap_pct      = ((today_open - prev_close) / prev_close) * 100

    # Gap must be positive and within range
    if gap_pct < min_gap_pct or gap_pct > max_gap_pct:
        return BreakoutSignal(False, "gap_and_go_long", symbol, 0)

    # First candle must be bullish (closed above its open)
    first_candle        = today_df.iloc[0]
    first_candle_bull   = float(first_candle["close"]) > float(first_candle["open"])
    if not first_candle_bull:
        return BreakoutSignal(False, "gap_and_go_long", symbol, 0)

    # Current price must still be above today's open (gap is holding)
    current_candle = today_df.iloc[-1]
    cur_close      = float(current_candle["close"])
    cur_volume     = float(current_candle["volume"])
    if cur_close <= today_open:
        return BreakoutSignal(False, "gap_and_go_long", symbol, cur_close)

    # Volume confirmation
    prev_df    = df[df.index.date < datetime.now().date()]
    avg_volume = float(prev_df["volume"].tail(50).mean()) if not prev_df.empty else 0
    vol_ratio  = cur_volume / avg_volume if avg_volume > 0 else 0
    triggered  = vol_ratio >= volume_multiplier

    return BreakoutSignal(
        triggered=triggered,
        signal_type="gap_and_go_long",
        symbol=symbol,
        current_price=cur_close,
        resistance_level=round(today_open, 2),
        volume_ratio=round(vol_ratio, 2),
        details={
            "gap_pct":         round(gap_pct, 2),
            "prev_close":      round(prev_close, 2),
            "today_open":      round(today_open, 2),
            "first_candle_bull": first_candle_bull,
            "volume_ratio":    round(vol_ratio, 2),
            "direction":       "long",
        },
    )


def check_gap_and_go_short(
    df: pd.DataFrame,
    symbol: str,
    min_gap_pct: float = 1.0,
    max_gap_pct: float = 5.0,
    volume_multiplier: float = 1.5,
) -> BreakoutSignal:
    """
    GAP & GO SHORT:
      1. Today's open is >min_gap_pct% BELOW yesterday's close (gap down)
      2. Gap is not too large (<=max_gap_pct)
      3. First 5-min candle closes BELOW its open (bearish candle)
      4. Current price is still below today's open (gap holding)
      5. Volume is elevated
    """
    today_df = _today_candles(df)
    if today_df.empty or len(today_df) < 2:
        return BreakoutSignal(False, "gap_and_go_short", symbol, 0)

    prev_close = _prev_close(df)
    if not prev_close or prev_close <= 0:
        return BreakoutSignal(False, "gap_and_go_short", symbol, 0)

    today_open = float(today_df.iloc[0]["open"])
    gap_pct    = ((prev_close - today_open) / prev_close) * 100

    if gap_pct < min_gap_pct or gap_pct > max_gap_pct:
        return BreakoutSignal(False, "gap_and_go_short", symbol, 0)

    # First candle must be bearish
    first_candle       = today_df.iloc[0]
    first_candle_bear  = float(first_candle["close"]) < float(first_candle["open"])
    if not first_candle_bear:
        return BreakoutSignal(False, "gap_and_go_short", symbol, 0)

    # Current price must still be below today's open
    current_candle = today_df.iloc[-1]
    cur_close      = float(current_candle["close"])
    cur_volume     = float(current_candle["volume"])
    if cur_close >= today_open:
        return BreakoutSignal(False, "gap_and_go_short", symbol, cur_close)

    prev_df    = df[df.index.date < datetime.now().date()]
    avg_volume = float(prev_df["volume"].tail(50).mean()) if not prev_df.empty else 0
    vol_ratio  = cur_volume / avg_volume if avg_volume > 0 else 0
    triggered  = vol_ratio >= volume_multiplier

    return BreakoutSignal(
        triggered=triggered,
        signal_type="gap_and_go_short",
        symbol=symbol,
        current_price=cur_close,
        resistance_level=round(today_open, 2),
        volume_ratio=round(vol_ratio, 2),
        details={
            "gap_pct":          round(gap_pct, 2),
            "prev_close":       round(prev_close, 2),
            "today_open":       round(today_open, 2),
            "first_candle_bear": first_candle_bear,
            "volume_ratio":     round(vol_ratio, 2),
            "direction":        "short",
        },
    )
