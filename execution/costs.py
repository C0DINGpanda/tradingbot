"""
execution/costs.py — NSE transaction cost calculator for MIS (intraday) trades.

Charges applied per trade (buy + sell combined):
  ─────────────────────────────────────────────────────────────
  Brokerage          Zerodha flat ₹20 per executed order (× 2)
  STT                0.025% on sell turnover only (MIS equity)
  Exchange txn fee   0.00297% on total turnover (both legs)
  SEBI fee           ₹10 per crore turnover (0.000001 × turnover)
  GST                18% on (brokerage + exchange fee + SEBI fee)
  Stamp duty         0.003% on buy turnover only
  ─────────────────────────────────────────────────────────────

Reference: https://zerodha.com/charges/
All rates as of 2024. Adjust BROKERAGE_PER_ORDER for other brokers.
"""

BROKERAGE_PER_ORDER = 20.0        # ₹20 flat per order (Zerodha)
STT_SELL_PCT        = 0.025 / 100  # 0.025% on sell side only (MIS)
EXCHANGE_TXN_PCT    = 0.00297 / 100  # NSE equity (both sides)
SEBI_FEE_PCT        = 10 / 1e7     # ₹10 per crore = 0.000001
GST_PCT             = 0.18          # 18% on statutory charges
STAMP_DUTY_BUY_PCT  = 0.003 / 100  # 0.003% on buy turnover


def calculate_trade_costs(
    buy_price: float,
    sell_price: float,
    quantity: int,
    direction: str = "long",
) -> dict:
    """
    Returns a breakdown of all transaction costs for one round-trip trade.

    For SHORT trades:
      - buy_price  = entry (the short-sell price)
      - sell_price = exit  (the cover/buy-back price)
      STT is charged on the "sell" leg:
        - for long:  the closing sell
        - for short: the opening sell (entry)
    """
    buy_turnover  = buy_price  * quantity
    sell_turnover = sell_price * quantity
    total_turnover = buy_turnover + sell_turnover

    brokerage = BROKERAGE_PER_ORDER * 2  # one order each side

    # STT: charged on sell turnover
    if direction == "short":
        stt_turnover = buy_turnover   # opening leg is the "sell"
    else:
        stt_turnover = sell_turnover  # closing leg is the "sell"
    stt = stt_turnover * STT_SELL_PCT

    exchange_txn = total_turnover * EXCHANGE_TXN_PCT
    sebi_fee     = total_turnover * SEBI_FEE_PCT
    stamp_duty   = buy_turnover   * STAMP_DUTY_BUY_PCT

    # GST applies on brokerage + exchange fee + SEBI fee (not STT/stamp)
    gst = (brokerage + exchange_txn + sebi_fee) * GST_PCT

    total_cost = brokerage + stt + exchange_txn + sebi_fee + stamp_duty + gst

    return {
        "brokerage":    round(brokerage,    2),
        "stt":          round(stt,          2),
        "exchange_txn": round(exchange_txn, 2),
        "sebi_fee":     round(sebi_fee,     4),
        "stamp_duty":   round(stamp_duty,   2),
        "gst":          round(gst,          2),
        "total_cost":   round(total_cost,   2),
    }


def net_pnl(
    buy_price: float,
    sell_price: float,
    quantity: int,
    direction: str = "long",
) -> tuple[float, float, dict]:
    """
    Returns (gross_pnl, net_pnl, cost_breakdown).
    """
    if direction == "short":
        gross = (buy_price - sell_price) * quantity
    else:
        gross = (sell_price - buy_price) * quantity

    costs = calculate_trade_costs(buy_price, sell_price, quantity, direction)
    net   = round(gross - costs["total_cost"], 2)
    return round(gross, 2), net, costs
