"""
strategy/breakout.py — Four breakout detection strategies.

Each strategy accepts an OHLCV DataFrame and returns a dict with:
    triggered (bool): True if breakout detected
    signal_type (str): name of the strategy
    details (dict): extra metadata for logging / dashboard
"""
import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

try:
    import ta as _ta_lib
    _HAS_TA = True
except ImportError:
    _HAS_TA = False

logger = logging.getLogger(__name__)


@dataclass
class BreakoutSignal:
    triggered: bool
    signal_type: str
    symbol: str
    current_price: float
    resistance_level: float = 0.0
    volume_ratio: float = 0.0
    details: dict = field(default_factory=dict)


# ── 1. Resistance Breakout ─────────────────────────────────────────────────
def check_resistance_breakout(
    df: pd.DataFrame,
    symbol: str,
    lookback: int = 20,
    min_breakout_pct: float = 0.3,
) -> BreakoutSignal:
    """
    Detects when the latest close breaks above the highest high of the last
    `lookback` candles (excluding the current candle).
    """
    if len(df) < lookback + 1:
        return BreakoutSignal(False, "resistance_breakout", symbol, 0)

    prev = df.iloc[-(lookback + 1):-1]
    resistance = prev["high"].max()
    current_close = df.iloc[-1]["close"]
    breakout_pct = ((current_close - resistance) / resistance) * 100

    triggered = breakout_pct >= min_breakout_pct
    return BreakoutSignal(
        triggered=triggered,
        signal_type="resistance_breakout",
        symbol=symbol,
        current_price=current_close,
        resistance_level=resistance,
        details={"breakout_pct": round(breakout_pct, 2), "lookback": lookback},
    )


# ── 2. Volume-Confirmed Breakout ───────────────────────────────────────────
def check_volume_breakout(
    df: pd.DataFrame,
    symbol: str,
    lookback: int = 20,
    volume_multiplier: float = 1.5,
    min_breakout_pct: float = 0.3,
) -> BreakoutSignal:
    """
    Resistance breakout + current volume must be `volume_multiplier` × average
    volume of the lookback window.
    """
    resistance_signal = check_resistance_breakout(df, symbol, lookback, min_breakout_pct)
    if not resistance_signal.triggered:
        return BreakoutSignal(False, "volume_breakout", symbol, resistance_signal.current_price)

    prev = df.iloc[-(lookback + 1):-1]
    avg_volume = prev["volume"].mean()
    current_volume = df.iloc[-1]["volume"]
    volume_ratio = current_volume / avg_volume if avg_volume > 0 else 0

    triggered = volume_ratio >= volume_multiplier
    return BreakoutSignal(
        triggered=triggered,
        signal_type="volume_breakout",
        symbol=symbol,
        current_price=resistance_signal.current_price,
        resistance_level=resistance_signal.resistance_level,
        volume_ratio=round(volume_ratio, 2),
        details={
            "avg_volume": round(avg_volume),
            "current_volume": int(current_volume),
            "volume_ratio": round(volume_ratio, 2),
        },
    )


# ── 3. 52-Week High Breakout ───────────────────────────────────────────────
def check_52week_high_breakout(
    df: pd.DataFrame,
    symbol: str,
    min_breakout_pct: float = 0.1,
) -> BreakoutSignal:
    """
    Detects when price breaks above the 52-week (252-candle for daily, scaled
    for intraday) high for the first time.
    We use the max of all candles except the last as the 52-week high.
    """
    if len(df) < 50:
        return BreakoutSignal(False, "52week_high_breakout", symbol, 0)

    # Use up to 252 daily-equivalent candles
    window = df.iloc[-253:-1] if len(df) >= 253 else df.iloc[:-1]
    high_52w = window["high"].max()
    current_close = df.iloc[-1]["close"]
    breakout_pct = ((current_close - high_52w) / high_52w) * 100

    triggered = breakout_pct >= min_breakout_pct
    return BreakoutSignal(
        triggered=triggered,
        signal_type="52week_high_breakout",
        symbol=symbol,
        current_price=current_close,
        resistance_level=high_52w,
        details={"52w_high": round(high_52w, 2), "breakout_pct": round(breakout_pct, 2)},
    )


# ── 4. Bollinger Band Squeeze Breakout ────────────────────────────────────
def _get_today_open(df: pd.DataFrame) -> float | None:
    """Return the open price of the first candle of today's session."""
    today = pd.Timestamp.now().date()
    today_candles = df[df.index.date == today]
    if today_candles.empty:
        return None
    return float(today_candles.iloc[0]["open"])


def check_bb_squeeze_breakout(
    df: pd.DataFrame,
    symbol: str,
    period: int = 20,
    std: float = 2.0,
    squeeze_threshold_pct: float = 3.0,
    max_move_from_open_pct: float = 2.0,
    require_pullback: bool = True,
) -> BreakoutSignal:
    """
    Bollinger Band Squeeze:
      1. Band width contracts below `squeeze_threshold_pct` % of price (squeeze).
      2. Current candle closes above the upper band (bullish breakout).
      3. Price has not already moved more than `max_move_from_open_pct` from open
         (avoids entering exhausted moves).
      4. If `require_pullback` is True, at least one prior candle must have
         touched or crossed back below the upper band (retest confirmation).
    """
    if len(df) < period + 5:
        return BreakoutSignal(False, "bb_squeeze_breakout", symbol, 0)

    close = df["close"]
    rolling_mean = close.rolling(period).mean()
    rolling_std = close.rolling(period).std()
    upper_band = rolling_mean + std * rolling_std
    lower_band = rolling_mean - std * rolling_std
    band_width = ((upper_band - lower_band) / rolling_mean) * 100

    # Check squeeze: previous N candles had narrow bands
    squeeze_window = 5
    prev_bw = band_width.iloc[-(squeeze_window + 1):-1]
    was_squeezed = (prev_bw < squeeze_threshold_pct).all()

    current_close = float(close.iloc[-1])
    current_upper = float(upper_band.iloc[-1])
    current_bw = float(band_width.iloc[-1])

    # Guard: skip if price already moved too far from today's open (exhausted move)
    today_open = _get_today_open(df)
    move_from_open_pct = 0.0
    if today_open and today_open > 0:
        move_from_open_pct = ((current_close - today_open) / today_open) * 100
        if move_from_open_pct > max_move_from_open_pct:
            return BreakoutSignal(
                False, "bb_squeeze_breakout", symbol, current_close,
                details={"blocked": "move_from_open_exceeded",
                         "move_from_open_pct": round(move_from_open_pct, 2),
                         "max_allowed": max_move_from_open_pct},
            )

    # Guard: require at least one pullback candle (retest) before entry
    pullback_confirmed = True
    if require_pullback and len(close) >= period + 8:
        recent_closes = close.iloc[-(squeeze_window + 3):-1]
        recent_upper = upper_band.iloc[-(squeeze_window + 3):-1]
        pullback_confirmed = (recent_closes <= recent_upper).any()

    triggered = was_squeezed and current_close > current_upper and pullback_confirmed
    return BreakoutSignal(
        triggered=triggered,
        signal_type="bb_squeeze_breakout",
        symbol=symbol,
        current_price=current_close,
        resistance_level=round(current_upper, 2),
        details={
            "upper_band": round(current_upper, 2),
            "band_width_pct": round(current_bw, 2),
            "was_squeezed": was_squeezed,
            "squeeze_threshold_pct": squeeze_threshold_pct,
            "move_from_open_pct": round(move_from_open_pct, 2),
            "pullback_confirmed": pullback_confirmed,
        },
    )


# ── 5. NR7 Breakout ────────────────────────────────────────────────────────
def check_nr7_breakout(
    df: pd.DataFrame,
    symbol: str,
    nr_period: int = 7,
    expansion_factor: float = 1.5,
) -> BreakoutSignal:
    """
    NR7 (Narrowest Range in last N candles) Breakout:
      1. The previous completed candle had the narrowest range of last nr_period candles.
      2. Current candle's range is >= expansion_factor × NR7 range (energy release).
      3. Current candle closes in the top 25% of its range (bullish expansion).

    NR7 = compressed energy before explosive moves. 65%+ win rate on NSE (Connors research).
    """
    if len(df) < nr_period + 3:
        return BreakoutSignal(False, "nr7_breakout", symbol, 0)

    # Look at completed candles only (exclude current)
    recent = df.iloc[-(nr_period + 1):-1]
    candle_ranges = recent["high"] - recent["low"]
    nr7_range     = float(candle_ranges.min())

    cur        = df.iloc[-1]
    cur_range  = float(cur["high"]) - float(cur["low"])
    price      = float(cur["close"])

    if nr7_range <= 0:
        return BreakoutSignal(False, "nr7_breakout", symbol, price)

    is_expansion  = cur_range >= nr7_range * expansion_factor
    closes_near_high = (float(cur["high"]) - price) / cur_range < 0.25 if cur_range > 0 else False

    # Volume check: elevated vs recent average
    avg_vol   = float(recent["volume"].mean())
    vol_ratio = float(cur["volume"]) / avg_vol if avg_vol > 0 else 0
    good_vol  = vol_ratio >= 1.5

    triggered = is_expansion and closes_near_high and good_vol

    return BreakoutSignal(
        triggered=triggered,
        signal_type="nr7_breakout",
        symbol=symbol,
        current_price=price,
        volume_ratio=round(vol_ratio, 2),
        details={
            "nr7_range": round(nr7_range, 2),
            "cur_range": round(cur_range, 2),
            "expansion_ratio": round(cur_range / nr7_range, 2),
            "vol_ratio": round(vol_ratio, 2),
        },
    )


# ── 6. SuperTrend ──────────────────────────────────────────────────────────
def compute_supertrend(
    df: pd.DataFrame,
    period: int = 7,
    multiplier: float = 3.0,
) -> str:
    """
    Compute SuperTrend direction for the latest candle.
    Returns: "bullish" | "bearish" | "unknown"

    When bullish (price above SuperTrend): prefer LONG signals.
    When bearish (price below SuperTrend): prefer SHORT signals.
    """
    if len(df) < period * 2:
        return "unknown"

    high  = df["high"]
    low   = df["low"]
    close = df["close"]

    # True Range
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()

    hl2 = (high + low) / 2
    upper_band = hl2 + multiplier * atr
    lower_band = hl2 - multiplier * atr

    # Iterative SuperTrend calculation
    supertrend = pd.Series(index=df.index, dtype=float)
    direction  = pd.Series(index=df.index, dtype=int)  # 1=bullish, -1=bearish

    for i in range(period, len(df)):
        ub = upper_band.iloc[i]
        lb = lower_band.iloc[i]
        c  = float(close.iloc[i])
        pc = float(close.iloc[i - 1]) if i > 0 else c

        # Carry forward bands
        prev_ub = float(upper_band.iloc[i - 1]) if i > 0 else ub
        prev_lb = float(lower_band.iloc[i - 1]) if i > 0 else lb
        prev_st = float(supertrend.iloc[i - 1]) if i > period else lb
        prev_dir = int(direction.iloc[i - 1]) if i > period else 1

        ub = min(ub, prev_ub) if pc <= prev_ub else ub
        lb = max(lb, prev_lb) if pc >= prev_lb else lb

        if prev_dir == -1 and c > prev_st:
            direction.iloc[i] = 1
            supertrend.iloc[i] = lb
        elif prev_dir == 1 and c < prev_st:
            direction.iloc[i] = -1
            supertrend.iloc[i] = ub
        else:
            direction.iloc[i] = prev_dir
            supertrend.iloc[i] = lb if prev_dir == 1 else ub

    last_dir = int(direction.iloc[-1]) if not direction.empty else 0
    return "bullish" if last_dir == 1 else "bearish" if last_dir == -1 else "unknown"
