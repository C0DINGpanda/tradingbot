"""
execution/orders.py — Places buy/sell orders via Zerodha Kite.

Features:
  - dry_run / paper trading mode
  - actual fill price verification
  - trailing stop-loss
  - daily loss circuit breaker
  - Telegram alerts
"""
import logging
import math
import time
from typing import Optional

from kiteconnect import KiteConnect

from execution.portfolio import (
    open_position,
    close_position,
    partial_exit_position,
    get_open_positions,
    count_open_positions,
    update_trailing_stop,
    get_today_realised_pnl,
    get_today_traded_symbols,
    get_today_capital_deployed,
    log_event,
)
from execution.costs import net_pnl as _net_pnl
from execution.alerts import Alerter
from strategy.breakout import BreakoutSignal

logger = logging.getLogger(__name__)

_FILL_POLL_INTERVAL = 1.0
_FILL_MAX_RETRIES   = 8


class OrderManager:
    def __init__(self, kite: KiteConnect, cfg: dict):
        self.kite     = kite
        self.cfg      = cfg
        self.t_cfg    = cfg["trade"]
        self.dry_run  = cfg.get("dry_run", False)
        self.alerter  = Alerter(cfg)
        self._circuit_broken = False   # reset each day in main.py
        self.fetcher  = None           # set after init: order_mgr.fetcher = fetcher
        self.fallback_fetcher = None   # yfinance fallback when primary (Angel) has no token
        self.market_filter = None      # set after init: order_mgr.market_filter = mf

        tsl = self.t_cfg.get("trailing_stop_loss", {})
        self.use_trailing = tsl.get("enabled", False)
        self.trail_pct    = tsl.get("trail_pct", 1.5)

        if self.dry_run:
            logger.warning("🟡 DRY RUN MODE — no real orders will be placed")

    # ── Circuit breaker ────────────────────────────────────────────────────
    def _check_circuit_breaker(self) -> bool:
        """Returns True if daily loss limit is breached → stop trading."""
        if self._circuit_broken:
            return True
        max_loss_pct = self.t_cfg.get("max_daily_loss_pct", 3.0)
        total_cap    = self.t_cfg.get("total_capital", 50000)
        limit        = total_cap * max_loss_pct / 100
        daily_loss   = get_today_realised_pnl()   # negative = loss
        if daily_loss < -limit:
            self._circuit_broken = True
            logger.critical(
                f"🚨 CIRCUIT BREAKER: daily loss ₹{daily_loss:.2f} "
                f"exceeded limit -₹{limit:.2f}. No more trades today."
            )
            self.alerter.circuit_breaker(abs(daily_loss), limit)
            return True
        return False

    def reset_circuit_breaker(self):
        self._circuit_broken = False

    # ── Buy ────────────────────────────────────────────────────────────────
    def try_buy(self, signal: BreakoutSignal) -> bool:
        """Open a LONG or SHORT position based on signal direction."""
        direction = signal.details.get("direction", "long")

        if self._check_circuit_breaker():
            return False

        max_pos = self.t_cfg.get("max_open_positions", 5)
        if count_open_positions() >= max_pos:
            logger.info(f"Max positions ({max_pos}) reached. Skipping {signal.symbol}.")
            return False

        if any(p["symbol"] == signal.symbol for p in get_open_positions()):
            logger.info(f"Already have open position in {signal.symbol}. Skipping.")
            return False

        if signal.symbol in get_today_traded_symbols():
            logger.info(f"Already traded {signal.symbol} today — skipping to avoid repeat trade.")
            return False

        signal_price    = signal.current_price
        capital         = self.t_cfg.get("max_capital_per_trade", 10000)
        margin_mult     = self.t_cfg.get("mis_margin_multiplier", 5.0)
        daily_cap_limit = self.t_cfg.get("max_daily_capital", 10000)

        deployed = get_today_capital_deployed(margin_mult)
        if deployed >= daily_cap_limit:
            logger.info(
                f"Daily capital limit reached: ₹{deployed:.0f} / ₹{daily_cap_limit} deployed. "
                f"No more trades today."
            )
            return False
        # Limit this trade so we don't exceed the daily cap
        remaining_cap = daily_cap_limit - deployed
        min_trade_cap = self.t_cfg.get("min_capital_per_trade", 3000)
        if remaining_cap < min_trade_cap:
            logger.info(
                f"Skipping {signal.symbol} — only ₹{remaining_cap:.0f} remaining today "
                f"(minimum required: ₹{min_trade_cap})."
            )
            return False
        capital = min(capital, remaining_cap)

        effective_cap   = capital * margin_mult

        # VIX-based size reduction — only in HIGH-VIX regimes (low VIX = calm market, full size)
        size_factor = 1.0
        if self.market_filter:
            size_factor = self.market_filter.size_factor()
            if size_factor < 1.0:
                logger.info(f"  VIX size reduction: {size_factor:.0%} of normal size (high-volatility regime)")

        # ATR-based position sizing (risk fixed ₹ per trade)
        ps_cfg      = self.t_cfg.get("position_sizing", {})
        sizing_method = ps_cfg.get("method", "fixed_capital")
        atr          = signal.details.get("atr")
        atr_mult_sig = signal.details.get("atr_multiplier", 2.0)

        if sizing_method == "atr_risk" and atr and atr > 0:
            total_cap     = self.t_cfg.get("total_capital", 50000)
            risk_pct      = ps_cfg.get("risk_pct_per_trade", 1.0)
            max_risk      = ps_cfg.get("max_risk_per_trade", 500)
            # Risk amount scales with margin — with 5× MIS you can risk more shares per ₹ of capital
            risk_amount   = min(total_cap * margin_mult * risk_pct / 100, max_risk * margin_mult) * size_factor
            risk_per_share = atr * atr_mult_sig
            quantity = math.floor(risk_amount / risk_per_share) if risk_per_share > 0 else 0
            # Hard cap: never exceed the effective capital limit (margin already factored in)
            max_qty_by_cap = math.floor(effective_cap / signal_price)
            quantity = min(quantity, max_qty_by_cap)
            logger.info(
                f"  ATR sizing: risk=₹{risk_amount:.0f} (margin×{margin_mult}) / (ATR={atr:.2f}×{atr_mult_sig}) "
                f"= {quantity} shares (cap_max={max_qty_by_cap})"
            )
        else:
            quantity = math.floor(effective_cap * size_factor / signal_price)
        if quantity < 1:
            logger.warning(f"Capital ₹{capital} × {margin_mult}x = ₹{effective_cap} insufficient for {signal.symbol} @ ₹{signal_price}")
            return False

        # SHORT: SELL to open. LONG: BUY to open.
        open_side = "SELL" if direction == "short" else "BUY"
        order_id  = self._place_order(signal.symbol, open_side, quantity)
        if order_id is None:
            return False

        fill_price = self._verify_fill(order_id, signal.symbol, open_side,
                                       signal_price=signal.current_price)
        if fill_price is None:
            logger.error(f"Order {order_id} not filled — position NOT recorded")
            log_event("ORDER_REJECTED", signal.symbol, 0, quantity, order_id, "not filled")
            return False

        profit_pct = self.t_cfg.get("profit_target_pct", 5.0)
        trail_pct  = self.trail_pct if self.use_trailing else self.t_cfg.get("stop_loss_pct", 2.0)

        # Signal-specific profit target override
        sig_targets = self.t_cfg.get("signal_profit_targets", {})
        if signal.signal_type in sig_targets:
            profit_pct = sig_targets[signal.signal_type]

        # ORB trailing-only mode: dynamically decided by ADX if enabled
        # Strong ADX = trending market → trail the move; weak ADX = choppy → take fixed target
        orb_trail_only   = self.t_cfg.get("orb_trailing_only", False)
        adx_trail_dynamic = self.t_cfg.get("orb_adx_trailing_dynamic", True)
        is_orb = signal.signal_type in ("orb_long", "orb_short")

        if is_orb and adx_trail_dynamic:
            adx_value  = signal.details.get("adx")   # None if not computed
            adx_strong = self.cfg.get("indicators", {}).get("adx", {}).get("strong_trend", 25)
            # Only trail if ADX is actually available AND strong — default to fixed target
            use_trail_only = (adx_value is not None) and (adx_value >= adx_strong)
            logger.info(
                f"  ADX={adx_value} (threshold={adx_strong}) → "
                f"{'trailing-only' if use_trail_only else f'fixed {profit_pct}% target'} exit for {signal.symbol}"
            )
        else:
            use_trail_only = orb_trail_only and is_orb

        # ATR-based SL: use ATR from signal details if available and enabled
        atr         = signal.details.get("atr")
        use_atr_sl  = signal.details.get("use_atr_sl", False)
        atr_mult    = signal.details.get("atr_multiplier", 1.5)
        atr_sl_dist = round(atr * atr_mult, 2) if (atr and use_atr_sl) else None

        if direction == "short":
            target_price = round(fill_price * (1 - profit_pct / 100), 2) if not use_trail_only else 0.01
            if atr_sl_dist:
                initial_stop = round(fill_price + atr_sl_dist, 2)
            else:
                initial_stop = round(fill_price * (1 + trail_pct / 100), 2)
        else:
            target_price = round(fill_price * (1 + profit_pct / 100), 2) if not use_trail_only else fill_price * 10
            if atr_sl_dist:
                initial_stop = round(fill_price - atr_sl_dist, 2)
            else:
                initial_stop = round(fill_price * (1 - trail_pct / 100), 2)

        open_position(
            symbol=signal.symbol,
            buy_price=fill_price,
            quantity=quantity,
            buy_order_id=order_id,
            signal_type=signal.signal_type,
            target_price=target_price,
            stop_loss=initial_stop,
            direction=direction,
        )
        tag      = "📉 SHORT" if direction == "short" else "📈 LONG"
        slippage = round(fill_price - signal_price, 2)
        sl_mode  = f"ATR×{atr_mult}(₹{atr_sl_dist})" if atr_sl_dist else f"{trail_pct}%"
        logger.info(
            f"✅ {'[DRY] ' if self.dry_run else ''}{tag} {quantity} × {signal.symbol} "
            f"@ ₹{fill_price} (slip ₹{slippage:+}) | margin={margin_mult}x cap=₹{capital}→₹{effective_cap:.0f} | "
            f"target=₹{target_price} | SL=₹{initial_stop} [{sl_mode}]"
        )
        self.alerter.buy(
            signal.symbol, quantity, fill_price, target_price, initial_stop,
            signal.signal_type, signal.details.get("priority_score", 0),
        )
        return True

    # ── Sell monitor ───────────────────────────────────────────────────────
    def check_and_sell(self, ltp_map: dict[str, float]):
        """Check each open position for exit conditions (long and short)."""
        from datetime import datetime as _dt
        now_t = _dt.now().time()
        # Tighten trail after 12:30 PM — trades still open are losing momentum
        tsl_cfg     = self.t_cfg.get("trailing_stop_loss", {})
        tighten_after = tsl_cfg.get("tighten_after_time", "12:30")
        tighten_pct   = tsl_cfg.get("tighten_trail_pct", 0.3)
        tighten_t = _dt.strptime(tighten_after, "%H:%M").time()
        effective_trail = tighten_pct if now_t >= tighten_t else self.trail_pct

        for pos in get_open_positions():
            symbol    = pos["symbol"]
            ltp       = ltp_map.get(symbol)
            direction = pos.get("direction", "long")
            if ltp is None or ltp <= 0:
                continue

            # Effective quantity (may differ from original after partial exit)
            active_qty = pos.get("remaining_quantity") or pos["quantity"]

            if direction == "short":
                if self.use_trailing:
                    lowest = pos.get("highest_seen") or pos["buy_price"]
                    if ltp < lowest:
                        new_stop = round(ltp * (1 + effective_trail / 100), 2)
                        update_trailing_stop(pos["id"], new_stop, ltp)
                        logger.info(f"  📉 {symbol} new low ₹{ltp} → trailing SL → ₹{new_stop} (trail={effective_trail}%)")
                        pos = {**pos, "stop_loss": new_stop, "highest_seen": ltp}

                hit_target = ltp <= pos["target_price"]
                hit_sl     = ltp >= pos["stop_loss"]
            else:
                if self.use_trailing:
                    highest = pos.get("highest_seen") or pos["buy_price"]
                    if ltp > highest:
                        new_stop = round(ltp * (1 - effective_trail / 100), 2)
                        update_trailing_stop(pos["id"], new_stop, ltp)
                        logger.info(f"  📈 {symbol} new high ₹{ltp} → trailing SL → ₹{new_stop} (trail={effective_trail}%)")
                        pos = {**pos, "stop_loss": new_stop, "highest_seen": ltp}

                hit_target = ltp >= pos["target_price"]
                hit_sl     = ltp <= pos["stop_loss"]

            # ── Partial exit: exit 50% at 1× risk, move SL to breakeven ──────
            partial_cfg = self.t_cfg.get("partial_exit", {})
            if (partial_cfg.get("enabled", True)
                    and not pos.get("partial_exit_done")
                    and active_qty >= 2):
                entry   = pos["buy_price"]
                sl_dist = abs(entry - pos["stop_loss"])
                if sl_dist > 0:
                    if direction == "long" and ltp >= entry + sl_dist:
                        half_qty = active_qty // 2
                        logger.info(
                            f"  🎯 {symbol} partial exit {half_qty}×@₹{ltp} "
                            f"(1×risk hit) → SL→breakeven ₹{entry}"
                        )
                        oid = self._place_order(symbol, "SELL", half_qty)
                        if oid:
                            partial_exit_position(pos["id"], ltp, half_qty, oid)
                            pos = {**pos, "partial_exit_done": 1,
                                   "remaining_quantity": active_qty - half_qty,
                                   "stop_loss": entry}
                    elif direction == "short" and ltp <= entry - sl_dist:
                        half_qty = active_qty // 2
                        logger.info(
                            f"  🎯 {symbol} partial exit {half_qty}×@₹{ltp} "
                            f"(1×risk hit) → SL→breakeven ₹{entry}"
                        )
                        oid = self._place_order(symbol, "BUY", half_qty)
                        if oid:
                            partial_exit_position(pos["id"], ltp, half_qty, oid)
                            pos = {**pos, "partial_exit_done": 1,
                                   "remaining_quantity": active_qty - half_qty,
                                   "stop_loss": entry}
                        continue

            if hit_target:
                reason = f"target ₹{pos['target_price']}"
            elif hit_sl:
                reason = f"SL ₹{pos['stop_loss']}"
            else:
                continue

            tag = "📉" if direction == "short" else "📈"
            logger.info(f"🔔 {tag} {symbol} [{direction}] @ ₹{ltp} → closing ({reason})")
            self._execute_sell(pos, reason, ltp=ltp)

    def force_close_all(self, ltp_map: dict[str, float] = {}):
        """Close all MIS positions before 3:15 PM EOD penalty.

        Improvements vs plain market-close:
        1. Fetch live LTP before closing (accurate P&L, avoid buy-price fill).
        2. Staged exit: close losing positions first so profitable ones get a
           few extra seconds of favourable price movement.
        3. Profit protection: if a position is in profit, tighten trail to
           0.1% and let check_and_sell handle it; only hard-close if it still
           hasn't triggered within `profit_protection_window_s` seconds.
        """
        positions = get_open_positions()
        if not positions:
            return
        logger.warning(f"⏰ Force closing {len(positions)} position(s) before market close...")

        # ── 1. Fetch live LTP ──────────────────────────────────────────────
        if not ltp_map and self.fetcher:
            syms = [p["symbol"] for p in positions]
            try:
                ltp_map = self.fetcher.get_ltp(syms, self.t_cfg.get("exchange", "NSE"))
                logger.info(f"Force-close LTP fetched for: {list(ltp_map.keys())}")
            except Exception as e:
                logger.warning(f"LTP fetch failed for force-close: {e}")

        missing = [p["symbol"] for p in positions if ltp_map.get(p["symbol"], 0) <= 0]
        if missing and self.fallback_fetcher:
            try:
                fallback_map = self.fallback_fetcher.get_ltp(missing, self.t_cfg.get("exchange", "NSE"))
                ltp_map.update(fallback_map)
                logger.info(f"Force-close fallback LTP for: {list(fallback_map.keys())}")
            except Exception as e:
                logger.warning(f"Fallback LTP fetch failed: {e}")

        fc_cfg = self.t_cfg.get("force_close", {})
        protect_profit   = fc_cfg.get("protect_profit", True)
        protect_window_s = fc_cfg.get("profit_protection_window_s", 60)
        tight_trail_pct  = fc_cfg.get("profit_protection_trail_pct", 0.1)

        # ── 2. Staged exit: losers first, winners last ─────────────────────
        def unrealised_pnl(pos):
            ltp   = ltp_map.get(pos["symbol"], pos["buy_price"])
            entry = pos["buy_price"]
            qty   = pos.get("remaining_quantity") or pos["quantity"]
            if pos.get("direction", "long") == "long":
                return (ltp - entry) * qty
            else:
                return (entry - ltp) * qty

        positions_sorted = sorted(positions, key=unrealised_pnl)  # losers first

        deferred = []  # profitable positions given profit-protection window

        for pos in positions_sorted:
            ltp    = ltp_map.get(pos["symbol"], 0.0)
            pnl    = unrealised_pnl(pos)
            symbol = pos["symbol"]

            # ── 3. Profit protection ────────────────────────────────────────
            if protect_profit and ltp > 0 and pnl > 0:
                # Tighten trail to 0.1% and let the normal sell-monitor fire
                entry   = pos["buy_price"]
                if pos.get("direction", "long") == "long":
                    new_stop = round(ltp * (1 - tight_trail_pct / 100), 2)
                    new_stop = max(new_stop, entry)  # never below breakeven
                else:
                    new_stop = round(ltp * (1 + tight_trail_pct / 100), 2)
                    new_stop = min(new_stop, entry)  # never above breakeven
                update_trailing_stop(pos["id"], new_stop, ltp)
                logger.info(
                    f"  💰 {symbol} in profit ₹{pnl:+.0f} — tightened trail SL to ₹{new_stop} "
                    f"({tight_trail_pct}%) | deferring force-close {protect_window_s}s"
                )
                deferred.append(pos)
            else:
                if ltp <= 0:
                    logger.warning(f"⚠️ No LTP for {symbol} — force-close uses buy price (P&L≈₹0)")
                self._execute_sell(pos, "force_close_eod", ltp=ltp)

        # Give profitable positions a short window for tight trail to trigger
        if deferred and protect_window_s > 0:
            logger.info(f"  ⏳ Waiting {protect_window_s}s for {len(deferred)} profitable position(s)...")
            time.sleep(protect_window_s)
            # Re-check: only close those still open
            still_open_ids = {p["id"] for p in get_open_positions()}
            for pos in deferred:
                if pos["id"] in still_open_ids:
                    ltp = ltp_map.get(pos["symbol"], 0.0)
                    logger.info(f"  ⏰ {pos['symbol']} still open after window — force-closing now")
                    self._execute_sell(pos, "force_close_eod", ltp=ltp)
                else:
                    logger.info(f"  ✅ {pos['symbol']} already closed by trail SL — no force-close needed")

    # ── Internal ───────────────────────────────────────────────────────────
    def _execute_sell(self, pos: dict, reason: str, ltp: float = 0.0):
        direction  = pos.get("direction", "long")
        close_side = "BUY" if direction == "short" else "SELL"
        order_id   = self._place_order(pos["symbol"], close_side, pos["quantity"])
        if not order_id:
            return
        # Pass current LTP as signal_price so dry-run exits use the real market price,
        # not the entry price (which caused PNL to always be 0 in paper trading).
        fill_price = self._verify_fill(order_id, pos["symbol"], close_side,
                                       signal_price=ltp if ltp > 0 else pos.get("buy_price", 0.0))
        if fill_price is None:
            logger.error(f"Close order for {pos['symbol']} not confirmed — check manually!")
            self.alerter.error(f"Close order not confirmed for {pos['symbol']}!")
            return
        close_position(pos["id"], fill_price, order_id)
        gross, net, costs = _net_pnl(pos["buy_price"], fill_price, pos["quantity"], direction)
        tag = "📉 COVERED" if direction == "short" else "📈 SOLD"
        logger.info(
            f"✅ {'[DRY] ' if self.dry_run else ''}{tag} {pos['quantity']} × {pos['symbol']} "
            f"@ ₹{fill_price} | gross=₹{gross:+.2f} costs=₹{costs['total_cost']:.2f} net=₹{net:+.2f} | {reason}"
        )
        self.alerter.sell(pos["symbol"], pos["quantity"], fill_price, net, reason)

    def _place_order(self, symbol: str, transaction_type: str, quantity: int) -> Optional[str]:
        if self.dry_run:
            fake_id = f"DRY_{symbol}_{transaction_type}_{int(time.time())}"
            logger.info(f"[DRY] {transaction_type} {quantity} × {symbol} → id={fake_id}")
            return fake_id
        try:
            order_id = self.kite.place_order(
                variety=KiteConnect.VARIETY_REGULAR,
                exchange=self.t_cfg.get("exchange", "NSE"),
                tradingsymbol=symbol,
                transaction_type=transaction_type,
                quantity=quantity,
                order_type=self.t_cfg.get("order_type", "MARKET"),
                product=self.t_cfg.get("product", "MIS"),
            )
            return str(order_id)
        except Exception as e:
            logger.error(f"Order failed [{transaction_type} {symbol} ×{quantity}]: {e}")
            log_event("ORDER_ERROR", symbol, 0, quantity, "", str(e))
            self.alerter.error(f"Order failed: {transaction_type} {symbol} × {quantity} — {e}")
            return None

    def _verify_fill(self, order_id: str, symbol: str, side: str,
                     signal_price: float = 0.0) -> Optional[float]:
        if self.dry_run or order_id.startswith("DRY_"):
            # Use the actual signal price in paper trading
            return signal_price if signal_price > 0 else 100.0

        for attempt in range(_FILL_MAX_RETRIES):
            try:
                orders = self.kite.orders()
                order  = next((o for o in orders if str(o["order_id"]) == str(order_id)), None)
                if order is None:
                    time.sleep(_FILL_POLL_INTERVAL)
                    continue
                status = order.get("status", "")
                if status == "COMPLETE":
                    avg = float(order.get("average_price") or order.get("price") or 0)
                    return avg if avg > 0 else None
                elif status in ("REJECTED", "CANCELLED"):
                    logger.error(f"Order {order_id} {status}: {order.get('status_message','')}")
                    return None
                time.sleep(_FILL_POLL_INTERVAL)
            except Exception as e:
                logger.error(f"Error polling order {order_id}: {e}")
                time.sleep(_FILL_POLL_INTERVAL)

        logger.error(f"Order {order_id} fill timeout after {_FILL_MAX_RETRIES}s")
        return None

