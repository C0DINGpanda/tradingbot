import os
import sys
import yaml
import logging
from datetime import datetime

root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if root not in sys.path:
    sys.path.insert(0, root)

from execution.orders import OrderManager
from execution.portfolio import init_db, get_open_positions, count_open_positions
from strategy.breakout import BreakoutSignal

with open(os.path.join(root, 'config.yaml')) as f:
    cfg = yaml.safe_load(f)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger('test_buy')

init_db()
manager = OrderManager(None, cfg)
manager.fetcher = None

signal = BreakoutSignal(
    triggered=True,
    signal_type='orb_long',
    symbol='TCS',
    current_price=100.0,
    details={
        'direction': 'long',
        'priority_score': 4,
        'indicator_blocked': False,
    }
)

logger.info('Before buy open positions=%s', count_open_positions())
result = manager.try_buy(signal)
logger.info('Buy result=%s', result)
logger.info('After buy open positions=%s', count_open_positions())
