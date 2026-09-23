"""
execution/portfolio.py — Tracks open positions using SQLite.
Includes: trailing stop-loss tracking, daily loss circuit breaker.
"""
import logging
import sqlite3
from datetime import datetime, date
from typing import Optional

from execution.costs import net_pnl as _net_pnl

logger = logging.getLogger(__name__)

DB_PATH = "trading_bot.db"


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if they don't exist. Migrate existing DB safely."""
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol          TEXT NOT NULL,
                direction       TEXT NOT NULL DEFAULT 'long',  -- long | short
                buy_price       REAL NOT NULL,                 -- entry price (buy for long, sell for short)
                quantity        INTEGER NOT NULL,
                buy_order_id    TEXT,
                sell_order_id   TEXT,
                status          TEXT NOT NULL DEFAULT 'open',
                signal_type     TEXT,
                target_price    REAL,
                stop_loss       REAL,
                highest_seen    REAL,
                bought_at       TEXT NOT NULL,
                sold_at         TEXT,
                sell_price      REAL,                          -- exit price
                gross_pnl       REAL,                         -- before costs
                transaction_costs REAL,                       -- brokerage+STT+fees
                pnl             REAL                          -- net after all costs
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trade_log (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                event     TEXT NOT NULL,
                symbol    TEXT,
                price     REAL,
                quantity  INTEGER,
                order_id  TEXT,
                details   TEXT,
                logged_at TEXT NOT NULL
            )
        """)
        conn.commit()
        for col in ("highest_seen REAL", "direction TEXT DEFAULT 'long'",
                    "gross_pnl REAL", "transaction_costs REAL",
                    "partial_exit_done INTEGER DEFAULT 0",
                    "remaining_quantity INTEGER",
                    "partial_realized_gross REAL DEFAULT 0",
                    "partial_realized_costs REAL DEFAULT 0"):
            try:
                conn.execute(f"ALTER TABLE positions ADD COLUMN {col}")
                conn.commit()
            except Exception:
                pass
    logger.info("Database initialised.")


def open_position(
    symbol: str,
    buy_price: float,
    quantity: int,
    buy_order_id: str,
    signal_type: str,
    target_price: float,
    stop_loss: float,
    direction: str = "long",
) -> int:
    with _get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO positions
               (symbol, direction, buy_price, quantity, buy_order_id, signal_type,
                target_price, stop_loss, highest_seen, bought_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (symbol, direction, buy_price, quantity, buy_order_id, signal_type,
             target_price, stop_loss, buy_price, datetime.now().isoformat()),
        )
        conn.commit()
        pos_id = cur.lastrowid
    log_event(
        f"{'SHORT' if direction=='short' else 'BUY'}",
        symbol, buy_price, quantity, buy_order_id,
        f"direction={direction}, signal={signal_type}, target={target_price}, sl={stop_loss}",
    )
    return pos_id


def update_trailing_stop(pos_id: int, new_stop: float, new_highest: float):
    """Update trailing stop-loss and highest price seen for a position."""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE positions SET stop_loss=?, highest_seen=? WHERE id=?",
            (new_stop, new_highest, pos_id),
        )
        conn.commit()


def close_position(pos_id: int, sell_price: float, sell_order_id: str) -> tuple[float, float, float]:
    """Closes the position and returns (total_gross, total_net, total_costs)
    across the whole position — including any earlier partial-exit leg."""
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM positions WHERE id=?", (pos_id,)).fetchone()
        if row is None:
            logger.warning(f"Position {pos_id} not found")
            return 0.0, 0.0, 0.0
        direction = row["direction"] if row["direction"] else "long"
        # Close only the REMAINING quantity (a prior partial exit may have
        # already booked P&L on part of the position — using the full
        # original quantity here would double-count/ignore that leg).
        final_qty = row["remaining_quantity"] or row["quantity"]
        gross, net, costs = _net_pnl(row["buy_price"], sell_price, final_qty, direction)

        # Fold in any P&L already realised from a partial exit
        prior_gross  = row["partial_realized_gross"] or 0.0
        prior_costs  = row["partial_realized_costs"] or 0.0
        total_gross  = round(gross + prior_gross, 2)
        total_costs  = round(costs["total_cost"] + prior_costs, 2)
        total_net    = round(total_gross - total_costs, 2)

        conn.execute(
            """UPDATE positions SET status='closed', sell_price=?, sell_order_id=?,
               sold_at=?, gross_pnl=?, transaction_costs=?, pnl=? WHERE id=?""",
            (sell_price, sell_order_id, datetime.now().isoformat(),
             total_gross, total_costs, total_net, pos_id),
        )
        conn.commit()
    event = "COVER" if direction == "short" else "SELL"
    log_event(event, row["symbol"], sell_price, final_qty, sell_order_id,
              f"direction={direction}, gross_pnl={total_gross:.2f}, costs={total_costs:.2f}, net_pnl={total_net:.2f}")
    return total_gross, total_net, total_costs


def partial_exit_position(pos_id: int, exit_price: float, exit_qty: int, order_id: str):
    """
    Record a partial exit: books gross/net P&L for the exited lot, reduces
    remaining_quantity, and marks partial_exit_done. The position stays
    'open' until fully closed (final close adds the remaining leg's P&L
    on top of what's accumulated here).
    """
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM positions WHERE id=?", (pos_id,)).fetchone()
        if row is None:
            return
        direction = row["direction"] if row["direction"] else "long"
        remaining = (row["remaining_quantity"] or row["quantity"]) - exit_qty

        gross, net, costs = _net_pnl(row["buy_price"], exit_price, exit_qty, direction)
        prior_gross = row["partial_realized_gross"] or 0.0
        prior_costs = row["partial_realized_costs"] or 0.0
        new_gross   = round(prior_gross + gross, 2)
        new_costs   = round(prior_costs + costs["total_cost"], 2)

        conn.execute(
            """UPDATE positions SET partial_exit_done=1, remaining_quantity=?,
               stop_loss=buy_price, partial_realized_gross=?, partial_realized_costs=?
               WHERE id=?""",
            (remaining, new_gross, new_costs, pos_id),
        )
        conn.commit()
    log_event("PARTIAL_EXIT", row["symbol"], exit_price, exit_qty, order_id,
              f"partial exit, gross={gross:.2f}, remaining={remaining}")


def get_open_positions() -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute("SELECT * FROM positions WHERE status='open'").fetchall()
    return [dict(r) for r in rows]


def count_open_positions() -> int:
    with _get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM positions WHERE status='open'"
        ).fetchone()[0]


def get_today_realised_pnl() -> float:
    """Sum of P&L for all trades closed today (for circuit breaker)."""
    today = date.today().isoformat()
    with _get_conn() as conn:
        result = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM positions WHERE status='closed' AND sold_at LIKE ?",
            (f"{today}%",),
        ).fetchone()[0]
    return float(result)


def get_all_trades(limit: int = 200) -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM positions ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_today_capital_deployed(margin_multiplier: float = 5.0) -> float:
    """Total own capital deployed today = sum(buy_price × qty / margin) across all today's trades."""
    today = date.today().isoformat()
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT buy_price, quantity FROM positions WHERE bought_at LIKE ?",
            (f"{today}%",),
        ).fetchall()
    return sum(r["buy_price"] * r["quantity"] / margin_multiplier for r in rows)


def get_today_traded_symbols() -> set:
    """Returns symbols already traded (open or closed) today."""
    today = date.today().isoformat()
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT symbol FROM positions WHERE bought_at LIKE ?",
            (f"{today}%",),
        ).fetchall()
    return {r["symbol"] for r in rows}


def get_today_trades() -> list[dict]:
    today = date.today().isoformat()
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status='closed' AND sold_at LIKE ?",
            (f"{today}%",),
        ).fetchall()
    return [dict(r) for r in rows]


def log_event(event: str, symbol: str, price: float, quantity: int,
              order_id: str, details: str = ""):
    with _get_conn() as conn:
        conn.execute(
            """INSERT INTO trade_log (event, symbol, price, quantity, order_id, details, logged_at)
               VALUES (?,?,?,?,?,?,?)""",
            (event, symbol, price, quantity, order_id, details, datetime.now().isoformat()),
        )
        conn.commit()

