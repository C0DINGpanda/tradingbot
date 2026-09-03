"""Run a single trading loop scan (dry-run) for verification."""
from datetime import datetime
import yaml
import logging
import os
import sys

# Ensure project root is on sys.path
root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if root not in sys.path:
    sys.path.insert(0, root)

from data.yf_fetcher import YFinanceFetcher
from data.angel_fetcher import AngelFetcher
from strategy.screener import Screener
from execution.orders import OrderManager
from execution.portfolio import init_db
from main import setup_logging, trading_loop

with open('config.yaml') as f:
    cfg = yaml.safe_load(f)

setup_logging(cfg)
logger = logging.getLogger('run_once')
logger.info('Running a single dry-run scan')

# init DB
init_db()

# Choose fetcher based on config (use yfinance for single-run scanning)
fetcher = YFinanceFetcher()
# create screener and order manager
screener = Screener(fetcher, cfg)
order_mgr = OrderManager(None, cfg)
order_mgr.fetcher = fetcher

watchlist = []
try:
    from data.watchlist_builder import load_dynamic_watchlist
    watchlist = load_dynamic_watchlist(cfg.get('watchlist', []))
except Exception:
    watchlist = cfg.get('watchlist', [])

# Run trading_loop once
trading_loop(fetcher, screener, order_mgr, cfg, ws_monitor=None, ltp_fetcher=None)
logger.info('Single scan complete')
