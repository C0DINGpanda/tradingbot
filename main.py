"""
main.py — Entry point. Starts the scheduler that runs the trading loop.
"""
import logging
import logging.config
import os
import sys
from datetime import datetime

import yaml
from apscheduler.schedulers.blocking import BlockingScheduler
from kiteconnect import KiteConnect

from data.fetcher import DataFetcher
from data.yf_fetcher import YFinanceFetcher
from data.angel_fetcher import AngelFetcher, AngelWebSocketMonitor
from data.watchlist_builder import DYNAMIC_WATCHLIST_FILE, WatchlistBuilder, load_dynamic_watchlist
from strategy.screener import Screener
from execution.orders import OrderManager
from execution.portfolio import init_db, get_open_positions, get_today_trades
from execution.alerts import Alerter

# NSE trading holidays (add each year's holidays here)
NSE_HOLIDAYS = {
    "2025": ["2025-01-26", "2025-02-26", "2025-03-14", "2025-04-10",
             "2025-04-14", "2025-04-18", "2025-05-01", "2025-08-15",
             "2025-08-27", "2025-10-02", "2025-10-02", "2025-10-21",
             "2025-10-24", "2025-11-05", "2025-12-25"],
    "2026": ["2026-01-26", "2026-03-20", "2026-04-02", "2026-04-03",
             "2026-04-14", "2026-05-01", "2026-08-15", "2026-10-02",
             "2026-10-20", "2026-11-14", "2026-12-25"],
}

# ── Logging setup ──────────────────────────────────────────────────────────
def setup_logging(cfg: dict):
    level    = cfg.get("logging", {}).get("level", "INFO")
    log_file = cfg.get("logging", {}).get("log_file", "logs/bot.log")
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    # Force UTF-8 on Windows console so emojis don't crash logging
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )


# ── Kite client ────────────────────────────────────────────────────────────
def build_kite(cfg: dict) -> KiteConnect:
    kite = KiteConnect(api_key=cfg["kite"]["api_key"])
    token_file = cfg["kite"]["access_token_file"]
    if not os.path.exists(token_file):
        print(f"\n❌  Access token file '{token_file}' not found.")
        print("   Run `python auth.py` first to log in and generate the token.\n")
        sys.exit(1)
    with open(token_file) as f:
        kite.set_access_token(f.read().strip())
    return kite


# ── Market hours check ─────────────────────────────────────────────────────
def is_market_open(cfg: dict) -> bool:
    sched = cfg.get("scheduler", {})
    now   = datetime.now()

    if sched.get("skip_weekends", True) and now.weekday() >= 5:
        return False

    today = now.strftime("%Y-%m-%d")
    year  = now.strftime("%Y")
    if today in NSE_HOLIDAYS.get(year, []):
        return False

    open_t  = datetime.strptime(sched.get("market_open",  "09:15"), "%H:%M").time()
    close_t = datetime.strptime(sched.get("market_close", "15:20"), "%H:%M").time()
    return open_t <= now.time() <= close_t


def can_open_new_position(cfg: dict, signal_type: str = "") -> bool:
    """
    Intraday time filter:
      - Don't trade in first N minutes (opening volatility)
      - ORB signals: hard cutoff at no_new_positions_after (11:00)
      - Non-ORB signals (VWAP, EMA, NR7): extended cutoff (13:00)
      - Never enter within min_time_before_close minutes of force close (1:30 PM)
    """
    now   = datetime.now().time()
    logger = logging.getLogger("Main")
    sched = cfg.get("scheduler", {})
    from datetime import timedelta

    skip_open_min      = sched.get("skip_opening_minutes", 15)
    no_new_after       = sched.get("no_new_positions_after", "11:00")
    extended_after     = sched.get("no_new_positions_after_non_orb", "13:00")
    force_close_time   = "13:30"   # 1:30 PM force close
    min_mins_before_fc = sched.get("min_minutes_before_force_close", 20)

    open_t    = datetime.strptime(sched.get("market_open", "09:15"), "%H:%M").time()
    cutoff_t  = datetime.strptime(no_new_after, "%H:%M").time()
    ext_t     = datetime.strptime(extended_after, "%H:%M").time()
    fc_t      = datetime.strptime(force_close_time, "%H:%M")
    min_entry = (fc_t - timedelta(minutes=min_mins_before_fc)).time()
    earliest  = (datetime.combine(datetime.today(), open_t)
                 .replace(second=0, microsecond=0) + timedelta(minutes=skip_open_min)).time()

    if now < earliest:
        logger.debug(f"Waiting for opening {skip_open_min}-min cool-off (until {earliest})")
        return False

    # Never enter too close to force close — not enough time to reach target
    if now >= min_entry:
        logger.debug(f"Too close to force close ({force_close_time}) — no new entries after {min_entry}")
        return False

    is_orb = signal_type in ("orb_long", "orb_short", "")
    active_cutoff = cutoff_t if is_orb else ext_t

    if now > active_cutoff:
        label = "ORB" if is_orb else "non-ORB"
        logger.debug(f"Past {label} cutoff {active_cutoff} — skipping")
        return False
    return True


# ── Config hot-reload ──────────────────────────────────────────────────────
def load_config() -> dict:
    with open("config.yaml") as f:
        return yaml.safe_load(f)


# ── Main trading loop ──────────────────────────────────────────────────────
def trading_loop(fetcher, screener: Screener, order_mgr: OrderManager,
                  cfg_ref: list, ws_monitor=None, ltp_fetcher=None):
    logger = logging.getLogger("TradingLoop")

    # Hot-reload config on every cycle — no restart needed
    try:
        new_cfg = load_config()
        cfg_ref[0] = new_cfg
        screener.cfg   = new_cfg
        screener.s_cfg = new_cfg["strategy"]
        order_mgr.cfg  = new_cfg
        order_mgr.t_cfg = new_cfg["trade"]
    except Exception as e:
        logger.warning(f"Config reload failed — using last good config: {e}")

    cfg = cfg_ref[0]

    if not is_market_open(cfg):
        logger.info(f"Market closed at {datetime.now().strftime('%H:%M:%S')} — skipping scan.")
        return

    logger.info("-" * 60)
    logger.info(f"Scan started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    watchlist = load_dynamic_watchlist(cfg.get("watchlist", []))

    # Step 1: check open positions
    # Use Angel One for real-time LTP if available, else fall back to fetcher
    open_positions = get_open_positions()
    open_syms      = [p["symbol"] for p in open_positions]
    if open_syms and ws_monitor is None:
        price_source = ltp_fetcher if ltp_fetcher else fetcher
        ltp_map = price_source.get_ltp(open_syms, cfg["trade"]["exchange"])
        order_mgr.check_and_sell(ltp_map)

    # Step 2: scan for breakout signals
    signals = screener.scan(watchlist)

    # Step 3: buy confirmed signals (only if within new-position window)
    if signals:
        for sig in signals:
            if can_open_new_position(cfg, sig.signal_type):
                bought = order_mgr.try_buy(sig)
                if bought and ws_monitor:
                    new_open = [p["symbol"] for p in get_open_positions()]
                    ws_monitor.start(new_open)
            else:
                logger.info(f"⏰ {sig.symbol} [{sig.signal_type}] outside entry window — skipped")

    logger.info("Scan finished.")


# ── Entry ──────────────────────────────────────────────────────────────────
def main():
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)

    setup_logging(cfg)
    logger = logging.getLogger("Main")
    logger.info("🤖 Trading Bot starting...")

    init_db()

    # Only load Kite if data_source=kite OR real order execution needed
    data_source = cfg.get("data_source", "yfinance")
    dry_run     = cfg.get("dry_run", True)
    needs_kite  = (data_source == "kite") or (not dry_run)

    kite = None
    if needs_kite:
        kite = build_kite(cfg)
    else:
        logger.info("Kite login skipped (dry_run=true + data_source=%s)", data_source)

    # Pick data source
    data_source = cfg.get("data_source", "yfinance")
    angel_fetcher_inst = None

    if data_source == "angel":
        fetcher = AngelFetcher(cfg)
        angel_fetcher_inst = fetcher
        logger.info("Data source: Angel One SmartAPI (real-time)")
        ws_monitor = None
        if cfg.get("angel", {}).get("use_websocket", False):
            ws_monitor = AngelWebSocketMonitor(fetcher, None)
    elif data_source == "yfinance":
        fetcher    = YFinanceFetcher()
        ws_monitor = None
        logger.info("Data source: Yahoo Finance (candle scanning, no rate limits)")
        # Try to also load Angel One for real-time LTP on open positions
        angel_cfg = cfg.get("angel", {})
        if angel_cfg.get("api_key") and angel_cfg.get("api_key") != "YOUR_ANGEL_API_KEY":
            try:
                angel_fetcher_inst = AngelFetcher(cfg)
                logger.info("Angel One loaded for real-time LTP on open positions")
            except Exception as e:
                logger.warning(f"Angel One optional load failed (LTP will use yfinance): {e}")
    else:
        fetcher    = DataFetcher(kite)
        ws_monitor = None
        logger.info("Data source: Zerodha Kite API")

    screener  = Screener(fetcher, cfg)
    order_mgr = OrderManager(kite, cfg)
    order_mgr.fetcher = fetcher   # enables live LTP fetch in force_close_all

    # Wire market filter to order manager for VIX-based size reduction
    from strategy.market_filter import MarketFilter
    order_mgr.market_filter = MarketFilter(cfg)

    # Mutable config reference — trading_loop hot-reloads this every cycle
    cfg_ref = [cfg]

    # If Angel One is primary fetcher, set yfinance as fallback for symbols with no token
    if data_source == "angel":
        from data.yf_fetcher import YFinanceFetcher as _YF
        order_mgr.fallback_fetcher = _YF()
        logger.info("yfinance fallback fetcher set for force-close LTP on unknown symbols")

    # Wire WebSocket monitor to order manager if enabled
    if ws_monitor:
        ws_monitor.order_mgr = order_mgr

    interval_minutes = cfg.get("scheduler", {}).get("scan_interval_minutes", 5)

    scheduler = BlockingScheduler(timezone="Asia/Kolkata")

    # Pre-market job: build smart watchlist at 9:00 AM every day
    if cfg.get("watchlist_builder", {}).get("enabled", False):
        builder = WatchlistBuilder(fetcher, cfg)   # uses same fetcher (yfinance or Kite)
        scheduler.add_job(
            builder.build,
            trigger="cron",
            hour=9, minute=0,
            id="watchlist_builder",
        )
        logger.info("Smart watchlist builder scheduled at 09:00 AM daily.")

        if not os.path.exists(DYNAMIC_WATCHLIST_FILE):
            logger.info("No dynamic watchlist found yet — building it now.")
            try:
                builder.build()
            except Exception as e:
                logger.error(f"Failed to build dynamic watchlist at startup: {e}")
    scheduler.add_job(
        trading_loop,
        trigger="interval",
        minutes=interval_minutes,
        args=[fetcher, screener, order_mgr, cfg_ref, ws_monitor, angel_fetcher_inst],
        next_run_time=datetime.now(),
        id="trading_loop",
    )

    # Also trigger a scan exactly at 9:30 AM (after ORB window closes)
    scheduler.add_job(
        trading_loop,
        trigger="cron",
        hour=9, minute=30,
        args=[fetcher, screener, order_mgr, cfg_ref, ws_monitor, angel_fetcher_inst],
        id="first_scan_930",
    )

    # Force-close all positions at 1:30 PM (backtested optimal exit time)
    # Keeps a safety net close at 3:10 PM for any positions that somehow remain open
    if cfg.get("trade", {}).get("product", "MIS") == "MIS":
        scheduler.add_job(
            order_mgr.force_close_all,
            trigger="cron",
            hour=13, minute=30,
            id="force_close_2pm",
        )
        scheduler.add_job(
            order_mgr.force_close_all,
            trigger="cron",
            hour=15, minute=10,
            id="force_close_eod",
        )
        logger.info("Force-close jobs scheduled at 01:30 PM and 03:10 PM.")

    # Daily summary alert + circuit breaker reset at 3:30 PM
    alerter = Alerter(cfg)
    def end_of_day():
        trades = get_today_trades()
        alerter.daily_summary(trades)
        order_mgr.reset_circuit_breaker()
        logger.info(f"EOD: {len(trades)} trades today. Circuit breaker reset.")

    scheduler.add_job(end_of_day, trigger="cron", hour=15, minute=30, id="eod_summary")

    # Morning reset at 9:10 AM — clear stale state before market opens
    def morning_reset():
        logger.info("🌅 Morning reset: clearing circuit breaker and rejection cache...")
        order_mgr.reset_circuit_breaker()
        order_mgr._circuit_broken = False
        screener._rejected_cache.clear()
        screener._cache_date = date.today()
        # Reload config fresh at market open
        try:
            new_cfg = load_config()
            cfg_ref[0] = new_cfg
            screener.cfg   = new_cfg
            screener.s_cfg = new_cfg["strategy"]
            order_mgr.cfg  = new_cfg
            order_mgr.t_cfg = new_cfg["trade"]
            logger.info("Config reloaded at market open.")
        except Exception as e:
            logger.warning(f"Morning config reload failed: {e}")
        logger.info("Morning reset complete — ready to trade.")

    from datetime import date
    scheduler.add_job(morning_reset, trigger="cron", hour=9, minute=10, id="morning_reset")

    logger.info(f"Scheduler started. Scanning every {interval_minutes} minutes.")
    logger.info("Press Ctrl+C to stop.\n")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped by user.")


if __name__ == "__main__":
    main()
