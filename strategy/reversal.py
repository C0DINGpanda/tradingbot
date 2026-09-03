"""
strategy/reversal.py — Four bearish reversal/breakdown signal detectors.

Mirror of breakout.py but for SHORT positions:
  price breaks DOWN through support → sell first, buy back cheaper.

Each function returns a BreakoutSignal with direction="short" in details.
"""
import logging
from dataclasses import dataclass, field

import pandas as pd

from strategy.breakout import BreakoutSignal, _get_today_open   # reuse the same dataclass

logger = logging.getLogger(__name__)


# ── 1. Support Breakdown ───────────────────────────────────────────────────
def check_support_breakdown(
    df: pd.DataFrame,
    symbol: str,
    lookback: int = 20,
    min_breakdown_pct: float = 0.3,
) -> BreakoutSignal:
    """
    Triggers when close drops BELOW the lowest low of the last `lookback`
    candles by at least `min_breakdown_pct` %.

    Visual:
      ──────────────────── support (lowest low of last 20 candles)
           ↓
      ────────────── price breaks below → SHORT signal
    """
    if len(df) < lookback + 1:
        return BreakoutSignal(False, "support_breakdown", symbol, 0)

    prev      = df.iloc[-(lookback + 1):-1]
    support   = prev["low"].min()
    close     = float(df.iloc[-1]["close"])
    breakdown_pct = ((support - close) / support) * 100   # positive when below

    triggered = breakdown_pct >= min_breakdown_pct
    return BreakoutSignal(
        triggered=triggered,
        signal_type="support_breakdown",
        symbol=symbol,
        current_price=close,
        resistance_level=support,
        details={
            "support": round(support, 2),
            "breakdown_pct": round(breakdown_pct, 2),
            "direction": "short",
        },
    )


# ── 2. Volume-Confirmed Breakdown ──────────────────────────────────────────
def check_volume_breakdown(
    df: pd.DataFrame,
    symbol: str,
    lookback: int = 20,
    volume_multiplier: float = 1.5,
    min_breakdown_pct: float = 0.3,
) -> BreakoutSignal:
    """
    Support breakdown + current volume >= `volume_multiplier` × avg volume.
    Filters out weak/false breakdowns — real sellers show up in volume.
    """
    support_sig = check_support_breakdown(df, symbol, lookback, min_breakdown_pct)
    if not support_sig.triggered:
        return BreakoutSignal(False, "volume_breakdown_short", symbol,
                              support_sig.current_price)

    prev        = df.iloc[-(lookback + 1):-1]
    avg_volume  = prev["volume"].mean()
    cur_volume  = float(df.iloc[-1]["volume"])
    vol_ratio   = cur_volume / avg_volume if avg_volume > 0 else 0

    triggered = vol_ratio >= volume_multiplier
    return BreakoutSignal(
        triggered=triggered,
        signal_type="volume_breakdown_short",
        symbol=symbol,
        current_price=support_sig.current_price,
        resistance_level=support_sig.resistance_level,
        volume_ratio=round(vol_ratio, 2),
        details={
            "avg_volume": round(avg_volume),
            "current_volume": int(cur_volume),
            "volume_ratio": round(vol_ratio, 2),
            "direction": "short",
        },
    )


# ── 3. 52-Week Low Breakdown ───────────────────────────────────────────────
def check_52week_low_breakdown(
    df: pd.DataFrame,
    symbol: str,
    min_breakdown_pct: float = 0.1,
) -> BreakoutSignal:
    """
    Triggers when price breaks below its 52-week (or available history) low.
    Strong bearish momentum — institutional selling.
    """
    if len(df) < 50:
        return BreakoutSignal(False, "52week_low_breakdown", symbol, 0)

    window   = df.iloc[-253:-1] if len(df) >= 253 else df.iloc[:-1]
    low_52w  = float(window["low"].min())
    close    = float(df.iloc[-1]["close"])
    breakdown_pct = ((low_52w - close) / low_52w) * 100

    triggered = breakdown_pct >= min_breakdown_pct
    return BreakoutSignal(
        triggered=triggered,
        signal_type="52week_low_breakdown",
        symbol=symbol,
        current_price=close,
        resistance_level=low_52w,
        details={
            "52w_low": round(low_52w, 2),
            "breakdown_pct": round(breakdown_pct, 2),
            "direction": "short",
        },
    )


# ── 4. BB Squeeze Bearish Breakdown ───────────────────────────────────────
def check_bb_squeeze_short(
    df: pd.DataFrame,
    symbol: str,
    period: int = 20,
    std: float = 2.0,
    squeeze_threshold_pct: float = 3.0,
    max_move_from_open_pct: float = 2.0,
    require_pullback: bool = True,
) -> BreakoutSignal:
    """
    Bollinger Band squeeze followed by price breaking BELOW the lower band.
    Opposite of bb_squeeze_breakout — signals explosive downward move.

    Visual:
      ─── upper band ──────────────────────────
           price squeezes (low volatility)
      ─── lower band ────────────────\\─────────
                                      ↓ price breaks lower band → SHORT

    Guards:
      - Skip if price already moved more than `max_move_from_open_pct` down
        from today's open (avoids entering exhausted drops late in the move).
      - If `require_pullback` is True, at least one prior candle must have
        touched or bounced back above the lower band before the breakdown
        (retest confirmation reduces false entries near intraday lows).
    """
    if len(df) < period + 5:
        return BreakoutSignal(False, "bb_squeeze_short", symbol, 0)

    close        = df["close"]
    rolling_mean = close.rolling(period).mean()
    rolling_std  = close.rolling(period).std()
    upper_band   = rolling_mean + std * rolling_std
    lower_band   = rolling_mean - std * rolling_std
    band_width   = ((upper_band - lower_band) / rolling_mean) * 100

    squeeze_window = 5
    prev_bw     = band_width.iloc[-(squeeze_window + 1):-1]
    was_squeezed = (prev_bw < squeeze_threshold_pct).all()

    cur_close  = float(close.iloc[-1])
    cur_lower  = float(lower_band.iloc[-1])
    cur_bw     = float(band_width.iloc[-1])

    # Guard: skip if price has already dropped too far from today's open
    today_open = _get_today_open(df)
    move_from_open_pct = 0.0
    if today_open and today_open > 0:
        move_from_open_pct = ((today_open - cur_close) / today_open) * 100
        if move_from_open_pct > max_move_from_open_pct:
            return BreakoutSignal(
                False, "bb_squeeze_short", symbol, cur_close,
                details={"blocked": "move_from_open_exceeded",
                         "move_from_open_pct": round(move_from_open_pct, 2),
                         "max_allowed": max_move_from_open_pct},
            )

    # Guard: require at least one pullback candle (retest) before breakdown entry
    pullback_confirmed = True
    if require_pullback and len(close) >= period + 8:
        recent_closes = close.iloc[-(squeeze_window + 3):-1]
        recent_lower  = lower_band.iloc[-(squeeze_window + 3):-1]
        pullback_confirmed = (recent_closes >= recent_lower).any()

    triggered = was_squeezed and cur_close < cur_lower and pullback_confirmed
    return BreakoutSignal(
        triggered=triggered,
        signal_type="bb_squeeze_short",
        symbol=symbol,
        current_price=cur_close,
        resistance_level=round(cur_lower, 2),
        details={
            "lower_band": round(cur_lower, 2),
            "band_width_pct": round(cur_bw, 2),
            "was_squeezed": was_squeezed,
            "move_from_open_pct": round(move_from_open_pct, 2),
            "pullback_confirmed": pullback_confirmed,
            "direction": "short",
        },
    )
