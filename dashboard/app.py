"""
dashboard/app.py — Streamlit dashboard to monitor the bot live.
Run with: streamlit run dashboard/app.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import sqlite3
import pandas as pd
import streamlit as st

DB_PATH = "trading_bot.db"

st.set_page_config(page_title="NSE Breakout Bot", page_icon="📈", layout="wide")
st.title("📈 NSE Breakout Trading Bot — Dashboard")

def load_positions(status_filter=None):
    if not os.path.exists(DB_PATH):
        return pd.DataFrame()
    with sqlite3.connect(DB_PATH) as conn:
        if status_filter:
            df = pd.read_sql(f"SELECT * FROM positions WHERE status=? ORDER BY id DESC", conn, params=(status_filter,))
        else:
            df = pd.read_sql("SELECT * FROM positions ORDER BY id DESC LIMIT 200", conn)
    return df

def load_logs():
    if not os.path.exists(DB_PATH):
        return pd.DataFrame()
    with sqlite3.connect(DB_PATH) as conn:
        return pd.read_sql("SELECT * FROM trade_log ORDER BY id DESC LIMIT 100", conn)

# ── Summary metrics ────────────────────────────────────────────────────────
all_trades = load_positions()
open_trades = load_positions("open")
closed_trades = load_positions("closed")

c1, c2, c3, c4 = st.columns(4)
c1.metric("Open Positions", len(open_trades))
c2.metric("Closed Trades", len(closed_trades))

if not closed_trades.empty and "pnl" in closed_trades.columns:
    total_net_pnl   = closed_trades["pnl"].sum()
    total_gross_pnl = closed_trades["gross_pnl"].sum() if "gross_pnl" in closed_trades.columns else total_net_pnl
    total_costs     = closed_trades["transaction_costs"].sum() if "transaction_costs" in closed_trades.columns else 0.0
    wins = (closed_trades["pnl"] > 0).sum()
    win_rate = (wins / len(closed_trades) * 100) if len(closed_trades) > 0 else 0
    c3.metric("Net P&L (₹)", f"{total_net_pnl:,.2f}", delta=f"{total_net_pnl:+.2f}")
    c4.metric("Win Rate", f"{win_rate:.1f}%")
else:
    total_gross_pnl = total_costs = 0.0
    c3.metric("Net P&L (₹)", "—")
    c4.metric("Win Rate", "—")

st.divider()

# ── Cost summary banner ────────────────────────────────────────────────────
if total_costs > 0:
    cc1, cc2, cc3 = st.columns(3)
    cc1.metric("Gross P&L (₹)", f"{total_gross_pnl:,.2f}")
    cc2.metric("Transaction Costs (₹)", f"-{total_costs:,.2f}")
    cc3.metric("Net P&L after costs (₹)", f"{total_net_pnl:,.2f}", delta=f"{total_net_pnl:+.2f}")

st.divider()

# ── Open positions ─────────────────────────────────────────────────────────
st.subheader("🟢 Open Positions")
if open_trades.empty:
    st.info("No open positions.")
else:
    cols_show = ["symbol", "direction", "buy_price", "quantity", "target_price", "stop_loss", "signal_type", "bought_at"]
    display_open = open_trades[[c for c in cols_show if c in open_trades.columns]].copy()
    # Show "Trailing 🎯" instead of dummy 0.01 / huge number for trailing-only trades
    def fmt_target(row):
        t = row.get("target_price", 0)
        d = row.get("direction", "long")
        if d == "short" and t <= 1:
            return "Trailing 🎯"
        if d == "long" and t > row.get("buy_price", 0) * 5:
            return "Trailing 🎯"
        return f"₹{t:,.2f}"
    display_open["target_price"] = open_trades[[c for c in ["direction","buy_price","target_price"] if c in open_trades.columns]].apply(fmt_target, axis=1)
    st.dataframe(display_open, use_container_width=True)
# ── Closed trades ──────────────────────────────────────────────────────────
st.subheader("📋 Closed Trades")
if closed_trades.empty:
    st.info("No closed trades yet.")
else:
    cols_show = ["symbol", "buy_price", "sell_price", "quantity",
                 "gross_pnl", "transaction_costs", "pnl", "signal_type", "bought_at", "sold_at"]
    display = closed_trades[[c for c in cols_show if c in closed_trades.columns]].copy()
    for col in ("gross_pnl", "pnl"):
        if col in display.columns:
            display[col] = display[col].map(lambda x: f"₹{x:,.2f}" if pd.notna(x) else "—")
    if "transaction_costs" in display.columns:
        display["transaction_costs"] = display["transaction_costs"].map(
            lambda x: f"₹{x:,.2f}" if pd.notna(x) else "—"
        )
    display = display.rename(columns={
        "gross_pnl": "Gross P&L",
        "transaction_costs": "Costs",
        "pnl": "Net P&L",
    })
    st.dataframe(display, use_container_width=True)

# ── P&L chart ──────────────────────────────────────────────────────────────
if not closed_trades.empty and "pnl" in closed_trades.columns and len(closed_trades) > 1:
    st.subheader("📊 Cumulative P&L")
    chart_data = closed_trades[["sold_at", "pnl"]].dropna().sort_values("sold_at")
    chart_data["cumulative_pnl"] = chart_data["pnl"].cumsum()
    st.line_chart(chart_data.set_index("sold_at")["cumulative_pnl"])

# ── Trade log ──────────────────────────────────────────────────────────────
with st.expander("🗒️ Trade Log"):
    logs = load_logs()
    if logs.empty:
        st.info("No log entries yet.")
    else:
        st.dataframe(logs, use_container_width=True)

st.caption("Auto-refresh: press F5 or use Streamlit's rerun button.")
