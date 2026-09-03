import sqlite3, os

if not os.path.exists("trading_bot.db"):
    print("No trading_bot.db found — no trades recorded yet.")
    exit()

conn = sqlite3.connect("trading_bot.db")

trades = conn.execute(
    "SELECT symbol, direction, buy_price, sell_price, quantity, pnl, signal_type "
    "FROM positions WHERE status='closed' ORDER BY rowid DESC"
).fetchall()

print(f"{'Symbol':<15} {'Dir':<6} {'Qty':>4} {'Entry':>10} {'Exit':>10} {'PnL':>10} {'Signal'}")
print("-" * 75)
for t in trades:
    sym, dire, bp, sp, qty, pnl, sig = t
    print(f"{sym:<15} {dire:<6} {qty:>4} {bp:>10.2f} {sp:>10.2f} {(pnl or 0):>+10.2f}  {sig}")

total  = sum(t[5] or 0 for t in trades)
wins   = sum(1 for t in trades if (t[5] or 0) > 0)
losses = sum(1 for t in trades if (t[5] or 0) < 0)
even   = len(trades) - wins - losses

print("-" * 75)
print(f"Total P&L  : Rs {total:+.2f}")
print(f"Win/Loss   : {wins}W / {losses}L / {even} flat")
print(f"Win Rate   : {wins/len(trades)*100:.1f}%" if trades else "No trades")

open_pos = conn.execute(
    "SELECT symbol, direction, buy_price, quantity, target_price, stop_loss "
    "FROM positions WHERE status='open'"
).fetchall()

print()
print(f"Open positions: {len(open_pos)}")
for p in open_pos:
    print(f"  {p[0]:<15} {p[1]:<6} qty={p[3]:>4}  entry={p[2]:>10.2f}  target={p[4]:>10.2f}  SL={p[5]:>10.2f}")

conn.close()
