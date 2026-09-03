"""
strategy/ema_pullback.py — EMA 9/21 Pullback entry signal.

Logic:
  LONG:  EMA9 > EMA21 (uptrend), price dips to touch EMA21 (pullback),
         then bounces with a bullish candle + elevated volume.
         "Buy the dip in an uptrend" — 58-62% win rate on NSE 5-min charts.

  SHORT: EMA9 < EMA21 (downtrend), price bounces to EMA21 (pullback),
         then rejects with a bearish candle + elevated volume.
         "Short the rally in a downtrend"
"""
import logging
import pandas as pd
from strategy.breakout import BreakoutSignal

logger = logging.getLogger(__name__)


def _compute_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def check_ema_pullback_long(
    df: pd.DataFrame,
    symbol: str,
    fast_period: int = 9,
    slow_period: int = 21,
    proximity_pct: float = 0.3,     # price must be within 0.3% of EMA21
    volume_multiplier: float = 1.2,
) -> BreakoutSignal:
    """Long: uptrend (EMA9 > EMA21), price pulls back to EMA21, bullish bounce."""
    if len(df) < slow_period + 5:
        return BreakoutSignal(symbol=symbol, triggered=False, signal_type="ema_pullback_long", current_price=0.0)

    ema_fast = _compute_ema(df["close"], fast_period)
    ema_slow = _compute_ema(df["close"], slow_period)

    cur_fast = float(ema_fast.iloc[-1])
    cur_slow = float(ema_slow.iloc[-1])
    cur      = df.iloc[-1]
    price    = float(cur["close"])

    # 1) Uptrend: EMA9 > EMA21
    is_uptrend = cur_fast > cur_slow

    # 2) Price near EMA21 (the pullback)
    dist_pct = abs(price - cur_slow) / cur_slow * 100
    near_ema21 = dist_pct <= proximity_pct

    # 3) Bounce: bullish candle
    is_bullish = float(cur["close"]) > float(cur["open"])

    # 4) Volume elevated
    avg_vol   = float(df["volume"].iloc[-20:].mean())
    vol_ratio = float(cur["volume"]) / avg_vol if avg_vol > 0 else 0
    good_vol  = vol_ratio >= volume_multiplier

    # 5) Price above EMA21 (not breaking down through it)
    above_slow = price >= cur_slow

    triggered = is_uptrend and near_ema21 and is_bullish and good_vol and above_slow

    return BreakoutSignal(
        symbol=symbol,
        triggered=triggered,
        signal_type="ema_pullback_long",
        current_price=price,
        volume_ratio=round(vol_ratio, 2),
        details={
            "ema9": round(cur_fast, 2),
            "ema21": round(cur_slow, 2),
            "ema_dist_pct": round(dist_pct, 3),
            "vol_ratio": round(vol_ratio, 2),
        },
    )


def check_ema_pullback_short(
    df: pd.DataFrame,
    symbol: str,
    fast_period: int = 9,
    slow_period: int = 21,
    proximity_pct: float = 0.3,
    volume_multiplier: float = 1.2,
) -> BreakoutSignal:
    """Short: downtrend (EMA9 < EMA21), price rallies to EMA21, bearish rejection."""
    if len(df) < slow_period + 5:
        return BreakoutSignal(symbol=symbol, triggered=False, signal_type="ema_pullback_short", current_price=0.0)

    ema_fast = _compute_ema(df["close"], fast_period)
    ema_slow = _compute_ema(df["close"], slow_period)

    cur_fast = float(ema_fast.iloc[-1])
    cur_slow = float(ema_slow.iloc[-1])
    cur      = df.iloc[-1]
    price    = float(cur["close"])

    is_downtrend = cur_fast < cur_slow
    dist_pct     = abs(price - cur_slow) / cur_slow * 100
    near_ema21   = dist_pct <= proximity_pct
    is_bearish   = float(cur["close"]) < float(cur["open"])

    avg_vol   = float(df["volume"].iloc[-20:].mean())
    vol_ratio = float(cur["volume"]) / avg_vol if avg_vol > 0 else 0
    good_vol  = vol_ratio >= volume_multiplier

    below_slow = price <= cur_slow

    triggered = is_downtrend and near_ema21 and is_bearish and good_vol and below_slow

    return BreakoutSignal(
        symbol=symbol,
        triggered=triggered,
        signal_type="ema_pullback_short",
        current_price=price,
        volume_ratio=round(vol_ratio, 2),
        details={
            "ema9": round(cur_fast, 2),
            "ema21": round(cur_slow, 2),
            "ema_dist_pct": round(dist_pct, 3),
            "vol_ratio": round(vol_ratio, 2),
        },
    )
