"""
strategy/indicators.py — Technical indicator calculations.

VWAP  : Intraday Volume Weighted Average Price
RSI   : Relative Strength Index (momentum / overbought-oversold)
ATR   : Average True Range (volatility — used for dynamic SL sizing)
ADX   : Average Directional Index (trend strength — filters choppy markets)

All functions accept a standard OHLCV DataFrame (columns: open, high, low, close, volume)
and return a float or None (when insufficient data).
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class IndicatorResult:
    """Holds computed indicator values and derived score/block signals."""
    vwap:              float | None = None
    rsi:               float | None = None
    atr:               float | None = None
    adx:               float | None = None

    score_delta:       int  = 0      # added to signal score (+/-)
    blocked:           bool = False  # True → hard block, do not trade
    block_reason:      str  = ""

    details:           dict = field(default_factory=dict)


# ── VWAP ───────────────────────────────────────────────────────────────────
def compute_vwap(df: pd.DataFrame) -> float | None:
    """
    Compute VWAP for today's intraday session only.
    VWAP = Σ(typical_price × volume) / Σ(volume)
    Resets every trading day at 09:15 AM.
    """
    today = datetime.now().date()
    today_df = df[df.index.date == today]
    if today_df.empty or len(today_df) < 2:
        return None
    typical = (today_df["high"] + today_df["low"] + today_df["close"]) / 3
    vwap = (typical * today_df["volume"]).cumsum() / today_df["volume"].cumsum()
    val = float(vwap.iloc[-1])
    return val if not np.isnan(val) else None


# ── RSI ────────────────────────────────────────────────────────────────────
def compute_rsi_series(df: pd.DataFrame, period: int = 14) -> pd.Series | None:
    """Full RSI series (0-100) for the given lookback — lets callers inspect
    recent bars, not just the latest value (e.g. to detect a bounce off
    oversold rather than a stable oversold reading)."""
    if len(df) < period + 1:
        return None
    delta = df["close"].diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    rsi   = 100 - (100 / (1 + rs))
    return rsi


def compute_rsi(df: pd.DataFrame, period: int = 14) -> float | None:
    """
    Relative Strength Index (0–100).
      > 60 : momentum, aligned with longs
      > 75 : overbought — avoid new longs
      < 40 : bearish momentum, aligned with shorts
      < 25 : oversold — avoid new shorts
    """
    rsi = compute_rsi_series(df, period)
    if rsi is None:
        return None
    val = float(rsi.iloc[-1])
    return val if not np.isnan(val) else None


# ── ATR ────────────────────────────────────────────────────────────────────
def compute_atr(df: pd.DataFrame, period: int = 14) -> float | None:
    """
    Average True Range — average candle-to-candle volatility.
    Used to set dynamic SL: SL = entry ± (multiplier × ATR)
    instead of a fixed percentage.
    """
    if len(df) < period + 1:
        return None
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"]  - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(period).mean().iloc[-1]
    return float(atr) if not np.isnan(atr) else None


# ── ADX ────────────────────────────────────────────────────────────────────
def compute_adx(df: pd.DataFrame, period: int = 14) -> float | None:
    """
    Average Directional Index (0–100) — measures trend STRENGTH, not direction.
      > 25 : strong trend → momentum strategies work well
      < 20 : choppy / sideways market → ORB signals are unreliable
    """
    if len(df) < period * 2 + 2:
        return None

    high  = df["high"]
    low   = df["low"]
    close = df["close"]

    plus_dm  = high.diff().clip(lower=0)
    minus_dm = (-low.diff()).clip(lower=0)
    # Only keep the dominant directional move
    mask = plus_dm >= minus_dm
    plus_dm[~mask]  = 0
    minus_dm[mask]  = 0

    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)

    atr_smooth     = tr.rolling(period).mean().replace(0, np.nan)
    plus_di        = 100 * plus_dm.rolling(period).mean()  / atr_smooth
    minus_di       = 100 * minus_dm.rolling(period).mean() / atr_smooth
    di_sum         = (plus_di + minus_di).replace(0, np.nan)
    dx             = 100 * (plus_di - minus_di).abs() / di_sum
    adx            = dx.rolling(period).mean().iloc[-1]
    return float(adx) if not np.isnan(adx) else None


# ── Composite evaluation ───────────────────────────────────────────────────
def evaluate_indicators(
    df: pd.DataFrame,
    direction: str,
    signal_type: str,
    ind_cfg: dict,
) -> IndicatorResult:
    """
    Compute all enabled indicators and derive:
      - score_delta  : total score adjustment (+/- integer)
      - blocked      : hard block flag
      - block_reason : why it was blocked

    ind_cfg is the `indicators:` block from config.yaml.
    """
    result = IndicatorResult()

    current_price = float(df["close"].iloc[-1])

    # ── VWAP ────────────────────────────────────────────────────────────────
    vwap_cfg = ind_cfg.get("vwap", {})
    if vwap_cfg.get("enabled", True):
        vwap = compute_vwap(df)
        result.vwap = vwap
        if vwap:
            bonus = vwap_cfg.get("score_bonus", 1)
            if direction == "long" and current_price > vwap:
                result.score_delta += bonus
                result.details["vwap_aligned"] = True
            elif direction == "short" and current_price < vwap:
                result.score_delta += bonus
                result.details["vwap_aligned"] = True
            else:
                result.details["vwap_aligned"] = False
            result.details["vwap"] = round(vwap, 2)

    # ── RSI ─────────────────────────────────────────────────────────────────
    rsi_cfg = ind_cfg.get("rsi", {})
    if rsi_cfg.get("enabled", True):
        rsi_series = compute_rsi_series(df, rsi_cfg.get("period", 14))
        rsi        = float(rsi_series.iloc[-1]) if rsi_series is not None and not np.isnan(rsi_series.iloc[-1]) else None
        result.rsi = rsi
        if rsi is not None:
            overbought = rsi_cfg.get("overbought", 75)
            oversold   = rsi_cfg.get("oversold",   25)
            bonus      = rsi_cfg.get("score_bonus", 1)

            # Was RSI recently at the opposite extreme and is now bouncing
            # back through it? A stock just ticking above `oversold` after
            # sitting below it is still in reversal-bounce territory, not a
            # confirmed downtrend continuation — dangerous to short into.
            # (e.g. SHRIRAMFIN 2026-09-08: RSI 18→27 in ~35 min, shorted right
            # into the bounce, stopped out shortly after.)
            check_bounce = rsi_cfg.get("block_recent_extreme_reversal", True)
            lookback     = rsi_cfg.get("reversal_lookback_bars", 5)
            recent       = rsi_series.iloc[-(lookback + 1):-1] if len(rsi_series) > lookback else rsi_series.iloc[:-1]

            if direction == "long":
                if rsi > overbought:
                    result.blocked      = True
                    result.block_reason = f"RSI={rsi:.0f} overbought (>{overbought})"
                elif check_bounce and rsi <= overbought and not recent.empty and (recent > overbought).any():
                    result.blocked      = True
                    result.block_reason = f"RSI={rsi:.0f} just bounced down from overbought — reversal risk, not confirmed"
                elif rsi >= rsi_cfg.get("momentum_min", 60):
                    result.score_delta += bonus   # strong momentum
            elif direction == "short":
                if rsi < oversold:
                    result.blocked      = True
                    result.block_reason = f"RSI={rsi:.0f} oversold (<{oversold})"
                elif check_bounce and rsi >= oversold and not recent.empty and (recent < oversold).any():
                    result.blocked      = True
                    result.block_reason = f"RSI={rsi:.0f} just bounced up from oversold — reversal risk, not confirmed"
                elif rsi <= rsi_cfg.get("momentum_max", 40):
                    result.score_delta += bonus   # strong bearish momentum
            result.details["rsi"] = round(rsi, 1)

    # ── ATR ─────────────────────────────────────────────────────────────────
    atr_cfg = ind_cfg.get("atr", {})
    if atr_cfg.get("enabled", True):
        atr = compute_atr(df, atr_cfg.get("period", 14))
        result.atr = atr
        if atr is not None:
            result.details["atr"]            = round(atr, 2)
            result.details["atr_multiplier"] = atr_cfg.get("multiplier", 1.5)
            result.details["use_atr_sl"]     = atr_cfg.get("use_for_sl", True)

    # ── ADX ─────────────────────────────────────────────────────────────────
    adx_cfg = ind_cfg.get("adx", {})
    if adx_cfg.get("enabled", True):
        adx = compute_adx(df, adx_cfg.get("period", 14))
        result.adx = adx
        if adx is not None:
            min_trend = adx_cfg.get("min_trending", 20)
            bonus     = adx_cfg.get("score_bonus", 1)
            is_orb    = signal_type in ("orb_long", "orb_short")

            if is_orb and adx < min_trend:
                result.blocked      = True
                result.block_reason = f"ADX={adx:.0f} choppy market (<{min_trend}) — ORB unreliable"
            elif adx >= adx_cfg.get("strong_trend", 25):
                result.score_delta += bonus
            result.details["adx"] = round(adx, 1)

    logger.debug(
        f"  Indicators [{direction}]: "
        f"VWAP={'✅' if result.details.get('vwap_aligned') else '—'} "
        f"RSI={result.details.get('rsi', '—')} "
        f"ATR={result.details.get('atr', '—')} "
        f"ADX={result.details.get('adx', '—')} "
        f"→ Δscore={result.score_delta:+d} blocked={result.blocked}"
    )
    return result
