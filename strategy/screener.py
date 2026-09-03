"""
strategy/screener.py — Scans all watchlist symbols for LONG and SHORT signals.
Features: priority scoring, multi-timeframe confirmation, reversal/short,
          sector momentum filter, Opening Range Breakout, market filters.
"""
import logging
from datetime import date, datetime

from data.fetcher import DataFetcher
from strategy.breakout import (
    BreakoutSignal,
    check_resistance_breakout,
    check_volume_breakout,
    check_52week_high_breakout,
    check_bb_squeeze_breakout,
    check_nr7_breakout,
    compute_supertrend,
)
from strategy.reversal import (
    check_support_breakdown,
    check_volume_breakdown,
    check_52week_low_breakdown,
    check_bb_squeeze_short,
)
from strategy.orb import check_orb_long, check_orb_short
from strategy.gap import check_gap_and_go_long, check_gap_and_go_short
from strategy.vwap_pullback import check_vwap_pullback_long, check_vwap_pullback_short
from strategy.ema_pullback import check_ema_pullback_long, check_ema_pullback_short
from strategy.market_filter import MarketFilter, refresh as mf_refresh
from strategy.sector_filter import SectorAnalyzer
from strategy.indicators import evaluate_indicators
from data.ban_filter import get_ban_filter

logger = logging.getLogger(__name__)

SIGNAL_PRIORITY_LONG  = ["gap_and_go_long", "orb_long", "nr7_breakout", "vwap_pullback_long",
                          "ema_pullback_long", "volume_breakout", "bb_squeeze_breakout",
                          "52week_high_breakout", "resistance_breakout"]
SIGNAL_PRIORITY_SHORT = ["gap_and_go_short", "orb_short", "nr7_breakout", "vwap_pullback_short",
                          "ema_pullback_short", "volume_breakdown_short", "bb_squeeze_short",
                          "52week_low_breakdown", "support_breakdown"]

DEFAULT_SCORES = {
    # Long
    "gap_and_go_long":    5,
    "orb_long":           4,
    "nr7_breakout":       4,
    "vwap_pullback_long": 4,
    "ema_pullback_long":  3,
    "volume_breakout":    3,
    "bb_squeeze_breakout":3,
    "52week_high_breakout":2,
    "resistance_breakout":1,
    # Short
    "gap_and_go_short":    5,
    "orb_short":           4,
    "vwap_pullback_short": 4,
    "ema_pullback_short":  3,
    "volume_breakdown_short":3,
    "bb_squeeze_short":    3,
    "52week_low_breakdown":2,
    "support_breakdown":   1,
}


class Screener:
    def __init__(self, fetcher: DataFetcher, cfg: dict):
        self.fetcher  = fetcher
        self.cfg      = cfg
        self.s_cfg    = cfg["strategy"]
        self.sector   = SectorAnalyzer(fetcher) if cfg.get("sector_filter", {}).get("enabled", True) else None
        self.mf       = MarketFilter(cfg)
        # Daily rejection cache: {(symbol, direction): date} — cleared each new trading day
        self._rejected_cache: dict[tuple, date] = {}
        self._cache_date: date = date.today()

    def scan(self, watchlist: list[str]) -> list[BreakoutSignal]:
        """
        Scans each symbol for both LONG and SHORT signals.
        Applies sector momentum filter before confirming trades.
        """
        # Reset rejection cache on new trading day
        today = date.today()
        if today != self._cache_date:
            self._rejected_cache.clear()
            self._cache_date = today

        # Refresh sector scores once per scan cycle
        if self.sector:
            self.sector.refresh()

        # Refresh market filters (VIX + Nifty) once per scan cycle
        mf_refresh()
        self.mf.log_status()

        confirmed: list[BreakoutSignal] = []
        min_required  = self.s_cfg.get("min_signals_required", 2)
        min_score     = self.s_cfg.get("min_priority_score", 4)
        scores        = {**DEFAULT_SCORES, **self.s_cfg.get("strategy_scores", {})}
        allow_short   = self.cfg.get("trade", {}).get("allow_short", False)
        mtf_cfg       = self.cfg.get("trade", {}).get("multi_timeframe", {})
        confirm_interval = mtf_cfg.get("confirm_interval", "60minute")
        ban           = get_ban_filter()

        for symbol in watchlist:
            # Skip banned / ASM / GSM stocks immediately
            if not ban.is_tradeable(symbol):
                continue
            try:
                # ── LONG scan ──────────────────────────────────────────────
                if (symbol, "long") not in self._rejected_cache:
                    long_triggered = self._scan_symbol(symbol)
                    best_long = self._confirm_signal(
                        symbol, long_triggered, min_required, min_score,
                        scores, SIGNAL_PRIORITY_LONG,
                        mtf_cfg.get("enabled", False), confirm_interval, "long",
                    )
                    if best_long:
                        confirmed.append(best_long)

                # ── SHORT scan ─────────────────────────────────────────────
                if allow_short and (symbol, "short") not in self._rejected_cache:
                    short_triggered = self._scan_symbol_short(symbol)
                    best_short = self._confirm_signal(
                        symbol, short_triggered, min_required, min_score,
                        scores, SIGNAL_PRIORITY_SHORT,
                        mtf_cfg.get("enabled", False), confirm_interval, "short",
                    )
                    if best_short:
                        confirmed.append(best_short)

            except Exception as e:
                logger.error(f"Error scanning {symbol}: {e}")

        long_count  = sum(1 for s in confirmed if s.details.get("direction") != "short")
        short_count = sum(1 for s in confirmed if s.details.get("direction") == "short")
        logger.info(
            f"Scan complete: {len(watchlist)} symbols → "
            f"{long_count} long + {short_count} short signals"
        )
        return confirmed

    def _confirm_signal(
        self, symbol, triggered, min_required, min_score,
        scores, priority_order, check_mtf, confirm_interval, direction,
    ):
        """Common confirmation logic for both long and short signals."""
        if not triggered:
            return None

        # ── Market filter (VIX + Nifty trend) ─────────────────────────────
        # Pick best signal first so we can pass signal_type to market filter
        best_for_filter = next(
            (s for p in priority_order for s in triggered if s.signal_type == p),
            triggered[0],
        )
        blocked, block_reason = self.mf.should_block(direction, best_for_filter.signal_type)
        if blocked:
            logger.info(f"🚫 {symbol} [{direction}] blocked by market filter — {block_reason}")
            return None

        # ── Dead zone: 11:00–12:00 AM → raise min score by 1 ─────────────
        sched_cfg = self.cfg.get("scheduler", {})
        dz_start  = sched_cfg.get("dead_zone_start", "11:00")
        dz_end    = sched_cfg.get("dead_zone_end", "12:00")
        now_t     = datetime.now().time()
        from datetime import time as dtime
        dz_s = datetime.strptime(dz_start, "%H:%M").time()
        dz_e = datetime.strptime(dz_end,   "%H:%M").time()
        effective_min_score = min_score + (1 if dz_s <= now_t <= dz_e else 0)

        total_score = sum(scores.get(s.signal_type, 1) for s in triggered)
        fired_names = [s.signal_type for s in triggered]

        best = next(
            (s for p in priority_order for s in triggered if s.signal_type == p),
            triggered[0],
        )

        if len(triggered) < min_required or total_score < effective_min_score:
            reason = []
            if len(triggered) < min_required:
                reason.append(f"{len(triggered)}/{min_required} strategies")
            if total_score < effective_min_score:
                reason.append(f"score {total_score}/{effective_min_score}{' [dead zone]' if effective_min_score > min_score else ''}")
            logger.info(f"⚠️  {symbol} [{direction}] skipped — {', '.join(reason)}")
            return None

        # Sector momentum filter — cache rejection for rest of day
        if self.sector and not self.sector.is_aligned(symbol, direction):
            self._rejected_cache[(symbol, direction)] = date.today()
            logger.info(f"🚫 {symbol} [{direction}] rejected — sector not aligned")
            return None

        # Indicator filters — hard block (RSI overbought/oversold, ADX choppy)
        ind_blocked = best.details.get("indicator_blocked", False)
        if ind_blocked:
            reason = best.details.get("indicator_block_reason", "indicator filter")
            logger.info(f"🚫 {symbol} [{direction}] blocked — {reason}")
            return None

        # Apply indicator score bonus to total
        ind_delta    = best.details.get("indicator_score_delta", 0)
        adj_score    = total_score + ind_delta
        if adj_score < min_score:
            logger.info(f"⚠️  {symbol} [{direction}] skipped after indicators — score {adj_score}/{min_score}")
            return None

        # MTF check — skip for ORB signals (they are already time-gated)
        skip_mtf_signals = set(
            self.s_cfg.get("skip_mtf_for_signals", ["orb_long", "orb_short"])
        )
        should_check_mtf = check_mtf and best.signal_type not in skip_mtf_signals

        if should_check_mtf and not self._confirm_higher_tf(symbol, confirm_interval, direction):
            logger.info(f"⏱️  {symbol} [{direction}] failed MTF check — caching for today")
            self._rejected_cache[(symbol, direction)] = date.today()
            return None
        best.details["confirmed_by"]    = fired_names
        best.details["priority_score"]  = total_score
        best.details["direction"]       = direction
        if self.sector:
            best.details["sector_score"] = self.sector.get_score(symbol)
        tag = "📈" if direction == "long" else "📉"
        logger.info(
            f"✅ {tag} {symbol} [{direction.upper()}] CONFIRMED — "
            f"score={total_score}{f'+{ind_delta}={adj_score}' if ind_delta else ''} | "
            f"{fired_names} | VWAP={'✅' if best.details.get('vwap_aligned') else '—'} "
            f"RSI={best.details.get('rsi','—')} ADX={best.details.get('adx','—')} | "
            f"₹{best.current_price}"
        )
        return best

    def _confirm_higher_tf(self, symbol: str, interval: str, direction: str = "long") -> bool:
        """Multi-timeframe check for both long and short directions."""
        df_htf = self.fetcher.get_candles(symbol, interval, self.cfg["trade"]["exchange"])
        if df_htf is None or df_htf.empty:
            return True   # allow through if no data

        lookback = self.s_cfg.get("lookback_candles", 20)
        min_pct  = self.s_cfg.get("min_breakout_pct", 0.3)
        v_mult   = self.s_cfg.get("volume_multiplier", 1.5)

        if direction == "short":
            checks = [
                check_support_breakdown(df_htf, symbol, lookback, min_pct),
                check_volume_breakdown(df_htf, symbol, lookback, v_mult, min_pct),
                check_52week_low_breakdown(df_htf, symbol),
                check_bb_squeeze_short(
                    df_htf, symbol,
                    self.s_cfg.get("bb_period", 20),
                    self.s_cfg.get("bb_std", 2.0),
                    self.s_cfg.get("bb_squeeze_threshold_pct", 3.0),
                    max_move_from_open_pct=self.s_cfg.get("bb_max_move_from_open_pct", 2.0),
                    require_pullback=self.s_cfg.get("bb_require_pullback", True),
                ),
            ]
        else:
            checks = [
                check_resistance_breakout(df_htf, symbol, lookback, min_pct),
                check_volume_breakout(df_htf, symbol, lookback, v_mult, min_pct),
                check_52week_high_breakout(df_htf, symbol),
                check_bb_squeeze_breakout(
                    df_htf, symbol,
                    self.s_cfg.get("bb_period", 20),
                    self.s_cfg.get("bb_std", 2.0),
                    self.s_cfg.get("bb_squeeze_threshold_pct", 3.0),
                    max_move_from_open_pct=self.s_cfg.get("bb_max_move_from_open_pct", 2.0),
                    require_pullback=self.s_cfg.get("bb_require_pullback", True),
                ),
            ]
        confirmed = any(c.triggered for c in checks)
        logger.debug(f"  MTF {interval} [{direction}] {symbol}: {'✅' if confirmed else '❌'}")
        return confirmed

    def _scan_symbol_short(self, symbol: str) -> list[BreakoutSignal]:
        """Scan for SHORT (bearish reversal) signals."""
        interval   = self.s_cfg.get("interval", "15minute")
        lookback   = self.s_cfg.get("lookback_candles", 20)
        v_mult     = self.s_cfg.get("volume_multiplier", 1.5)
        min_pct    = self.s_cfg.get("min_breakout_pct", 0.3)
        bb_period  = self.s_cfg.get("bb_period", 20)
        bb_std     = self.s_cfg.get("bb_std", 2.0)
        bb_thresh  = self.s_cfg.get("bb_squeeze_threshold_pct", 3.0)

        df = self.fetcher.get_candles(symbol, interval, self.cfg["trade"]["exchange"])
        if df is None or df.empty:
            return []

        triggered = []

        sig = check_support_breakdown(df, symbol, lookback, min_pct)
        if sig.triggered:
            logger.info(f"  📉 {symbol} — support_breakdown (score=1) @ ₹{sig.current_price}")
            triggered.append(sig)

        sig = check_volume_breakdown(df, symbol, lookback, v_mult, min_pct)
        if sig.triggered:
            logger.info(f"  📉 {symbol} — volume_breakdown (score=3) vol×{sig.volume_ratio} @ ₹{sig.current_price}")
            triggered.append(sig)

        sig = check_52week_low_breakdown(df, symbol, min_breakdown_pct=0.1)
        if sig.triggered:
            logger.info(f"  📉 {symbol} — 52week_low_breakdown (score=2) @ ₹{sig.current_price}")
            triggered.append(sig)

        sig = check_bb_squeeze_short(df, symbol, bb_period, bb_std, bb_thresh,
                                     max_move_from_open_pct=self.s_cfg.get("bb_max_move_from_open_pct", 2.0),
                                     require_pullback=self.s_cfg.get("bb_require_pullback", True))
        if sig.triggered and self.s_cfg.get("enable_bb_squeeze_short", True):
            logger.info(f"  📉 {symbol} — bb_squeeze_short (score=3) @ ₹{sig.current_price}")
            triggered.append(sig)

        # ORB short
        if self.s_cfg.get("enable_orb", True):
            orb_min = self.s_cfg.get("orb_minutes", 15)
            orb_vol = self.s_cfg.get("orb_volume_multiplier", 1.5)
            orb_delay = self.s_cfg.get("orb_max_entry_delay_minutes", 30)
            sig = check_orb_short(df, symbol, orb_min, orb_vol, max_entry_delay_minutes=orb_delay)
            if sig.triggered:
                logger.info(f"  🔻 {symbol} — orb_short (score=4) ORB break down @ ₹{sig.current_price}")
                triggered.append(sig)

        # Gap & Go short
        if self.s_cfg.get("enable_gap_and_go", True):
            gap_cfg = self.s_cfg.get("gap_and_go", {})
            sig = check_gap_and_go_short(
                df, symbol,
                min_gap_pct=gap_cfg.get("min_gap_pct", 1.0),
                max_gap_pct=gap_cfg.get("max_gap_pct", 5.0),
                volume_multiplier=gap_cfg.get("volume_multiplier", 1.5),
            )
            if sig.triggered:
                logger.info(f"  🔻 {symbol} — gap_and_go_short (score=5) gap={sig.details.get('gap_pct')}% @ ₹{sig.current_price}")
                triggered.append(sig)

        # VWAP Pullback short
        if self.s_cfg.get("enable_vwap_pullback", True):
            vp_cfg = self.s_cfg.get("vwap_pullback", {})
            sig = check_vwap_pullback_short(
                df, symbol,
                pullback_min_pct=vp_cfg.get("pullback_min_pct", 0.8),
                proximity_pct=vp_cfg.get("proximity_pct", 0.15),
                volume_multiplier=vp_cfg.get("volume_multiplier", 1.3),
            )
            if sig.triggered:
                logger.info(f"  📊 {symbol} — vwap_pullback_short (score=4) @ ₹{sig.current_price}")
                triggered.append(sig)

        # EMA 9/21 Pullback short
        if self.s_cfg.get("enable_ema_pullback", True):
            ep_cfg = self.s_cfg.get("ema_pullback", {})
            sig = check_ema_pullback_short(
                df, symbol,
                fast_period=ep_cfg.get("fast_period", 9),
                slow_period=ep_cfg.get("slow_period", 21),
                proximity_pct=ep_cfg.get("proximity_pct", 0.3),
                volume_multiplier=ep_cfg.get("volume_multiplier", 1.2),
            )
            if sig.triggered:
                logger.info(f"  📉 {symbol} — ema_pullback_short (score=3) EMA9<{sig.details.get('ema21')} @ ₹{sig.current_price}")
                triggered.append(sig)

        # SuperTrend filter for short signals
        if triggered and self.s_cfg.get("supertrend", {}).get("enabled", True):
            st_cfg = self.s_cfg.get("supertrend", {})
            st_dir = compute_supertrend(df, st_cfg.get("period", 7), st_cfg.get("multiplier", 3.0))
            for sig in triggered:
                sig.details["supertrend"] = st_dir
                sig.details["supertrend_aligned"] = (st_dir == "bearish")

        # Attach indicator data to all triggered signals
        if triggered:
            self._attach_indicators(df, triggered, "short")

        return triggered

    def _scan_symbol(self, symbol: str) -> list[BreakoutSignal]:
        interval = self.s_cfg.get("interval", "15minute")
        lookback = self.s_cfg.get("lookback_candles", 20)
        volume_mult = self.s_cfg.get("volume_multiplier", 1.5)
        min_pct = self.s_cfg.get("min_breakout_pct", 0.3)
        bb_period = self.s_cfg.get("bb_period", 20)
        bb_std = self.s_cfg.get("bb_std", 2.0)
        bb_squeeze_thresh = self.s_cfg.get("bb_squeeze_threshold_pct", 3.0)

        df = self.fetcher.get_candles(symbol, interval, self.cfg["trade"]["exchange"])
        if df is None or df.empty:
            logger.warning(f"Skipping {symbol}: no data")
            return []

        triggered = []

        if self.s_cfg.get("enable_resistance_breakout", True):
            sig = check_resistance_breakout(df, symbol, lookback, min_pct)
            if sig.triggered:
                logger.info(f"  📈 {symbol} — resistance_breakout (score=1) @ ₹{sig.current_price}")
                triggered.append(sig)

        if self.s_cfg.get("enable_volume_breakout", True):
            sig = check_volume_breakout(df, symbol, lookback, volume_mult, min_pct)
            if sig.triggered:
                logger.info(f"  📊 {symbol} — volume_breakout (score=3) vol×{sig.volume_ratio} @ ₹{sig.current_price}")
                triggered.append(sig)

        if self.s_cfg.get("enable_52week_high_breakout", True):
            sig = check_52week_high_breakout(df, symbol, min_breakout_pct=0.1)
            if sig.triggered:
                logger.info(f"  🏆 {symbol} — 52week_high_breakout (score=2) @ ₹{sig.current_price}")
                triggered.append(sig)

        if self.s_cfg.get("enable_bb_squeeze_breakout", True):
            sig = check_bb_squeeze_breakout(df, symbol, bb_period, bb_std, bb_squeeze_thresh,
                                            max_move_from_open_pct=self.s_cfg.get("bb_max_move_from_open_pct", 2.0),
                                            require_pullback=self.s_cfg.get("bb_require_pullback", True))
            if sig.triggered:
                logger.info(f"  🔵 {symbol} — bb_squeeze_breakout (score=3) @ ₹{sig.current_price}")
                triggered.append(sig)

        # ORB long
        if self.s_cfg.get("enable_orb", True):
            orb_min = self.s_cfg.get("orb_minutes", 15)
            orb_vol = self.s_cfg.get("orb_volume_multiplier", 1.5)
            orb_delay = self.s_cfg.get("orb_max_entry_delay_minutes", 30)
            sig = check_orb_long(df, symbol, orb_min, orb_vol, max_entry_delay_minutes=orb_delay)
            if sig.triggered:
                logger.info(f"  🚀 {symbol} — orb_long (score=4) ORB break @ ₹{sig.current_price}")
                triggered.append(sig)

        # Gap & Go long
        if self.s_cfg.get("enable_gap_and_go", True):
            gap_cfg = self.s_cfg.get("gap_and_go", {})
            sig = check_gap_and_go_long(
                df, symbol,
                min_gap_pct=gap_cfg.get("min_gap_pct", 1.0),
                max_gap_pct=gap_cfg.get("max_gap_pct", 5.0),
                volume_multiplier=gap_cfg.get("volume_multiplier", 1.5),
            )
            if sig.triggered:
                logger.info(f"  🚀 {symbol} — gap_and_go_long (score=5) gap={sig.details.get('gap_pct')}% @ ₹{sig.current_price}")
                triggered.append(sig)

        # NR7 breakout
        if self.s_cfg.get("enable_nr7", True):
            sig = check_nr7_breakout(df, symbol)
            if sig.triggered:
                logger.info(f"  💥 {symbol} — nr7_breakout (score=4) expansion×{sig.details.get('expansion_ratio')} @ ₹{sig.current_price}")
                triggered.append(sig)

        # VWAP Pullback long
        if self.s_cfg.get("enable_vwap_pullback", True):
            vp_cfg = self.s_cfg.get("vwap_pullback", {})
            sig = check_vwap_pullback_long(
                df, symbol,
                pullback_min_pct=vp_cfg.get("pullback_min_pct", 0.8),
                proximity_pct=vp_cfg.get("proximity_pct", 0.15),
                volume_multiplier=vp_cfg.get("volume_multiplier", 1.3),
            )
            if sig.triggered:
                logger.info(f"  📊 {symbol} — vwap_pullback_long (score=4) @ ₹{sig.current_price}")
                triggered.append(sig)

        # EMA 9/21 Pullback long
        if self.s_cfg.get("enable_ema_pullback", True):
            ep_cfg = self.s_cfg.get("ema_pullback", {})
            sig = check_ema_pullback_long(
                df, symbol,
                fast_period=ep_cfg.get("fast_period", 9),
                slow_period=ep_cfg.get("slow_period", 21),
                proximity_pct=ep_cfg.get("proximity_pct", 0.3),
                volume_multiplier=ep_cfg.get("volume_multiplier", 1.2),
            )
            if sig.triggered:
                logger.info(f"  📈 {symbol} — ema_pullback_long (score=3) EMA9>{sig.details.get('ema21')} @ ₹{sig.current_price}")
                triggered.append(sig)

        # SuperTrend direction filter: add score bonus or penalise counter-trend signals
        if triggered and self.s_cfg.get("supertrend", {}).get("enabled", True):
            st_cfg = self.s_cfg.get("supertrend", {})
            st_dir = compute_supertrend(df, st_cfg.get("period", 7), st_cfg.get("multiplier", 3.0))
            for sig in triggered:
                sig.details["supertrend"] = st_dir
                if st_dir == "bullish":
                    sig.details["supertrend_aligned"] = True
                elif st_dir == "bearish":
                    sig.details["supertrend_aligned"] = False
                    # Counter-trend — penalise score by marking in details (screener sums from scores dict)

        # Attach indicator data to all triggered signals
        if triggered:
            self._attach_indicators(df, triggered, "long")

        return triggered

    def _attach_indicators(self, df, triggered: list, direction: str):
        """Compute indicators once per symbol and attach to all triggered signals."""
        symbol = getattr(triggered[0], "symbol", "UNKNOWN")
        ind_cfg     = self.cfg.get("indicators", {})
        best_signal = next(
            (s for p in (SIGNAL_PRIORITY_LONG if direction == "long" else SIGNAL_PRIORITY_SHORT)
             for s in triggered if s.signal_type == p),
            triggered[0],
        )
        ind = evaluate_indicators(df, direction, best_signal.signal_type, ind_cfg)
        logger.debug(
            f"  Indicators [{direction}] for {symbol}: blocked={ind.blocked} "
            f"score_delta={ind.score_delta} details={ind.details}"
        )
        for sig in triggered:
            sig.details["indicator_score_delta"] = ind.score_delta
            sig.details["indicator_blocked"]     = ind.blocked
            sig.details["indicator_block_reason"] = ind.block_reason
            sig.details.update(ind.details)
