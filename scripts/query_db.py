import sqlite3
import json
from datetime import date
DB_PATH = 'trading_bot.db'

def rows_to_list(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]

conn = sqlite3.connect(DB_PATH)
conn.row_factory = None
cur = conn.cursor()

cur.execute("SELECT * FROM positions WHERE status='open'")
open_positions = rows_to_list(cur)

today = date.today().isoformat()
cur.execute("SELECT * FROM positions WHERE status='closed' AND sold_at LIKE ?", (f"{today}%",))
closed_today = rows_to_list(cur)

cur.execute("SELECT COUNT(*) FROM trade_log WHERE logged_at LIKE ?", (f"{today}%",))
trade_log_count = cur.fetchone()[0]

print(json.dumps({
    'open_positions': open_positions,
    'closed_today': closed_today,
    'trade_log_count': trade_log_count
}, default=str, indent=2))
conn.close()
