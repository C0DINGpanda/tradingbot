import yfinance as yf
import pandas as pd

for sym in ("JINDALSTEL.NS", "SBILIFE.NS"):
    df = yf.download(sym, period="2d", interval="5m", progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    df = df.tz_convert("Asia/Kolkata") if df.index.tz else df.tz_localize("UTC").tz_convert("Asia/Kolkata")
    df = df[(df.index >= "2026-09-17 09:15") & (df.index <= "2026-09-17 13:00")]
    print(f"=== {sym} ===")
    print(df[["Open","High","Low","Close"]].to_string())
    print()
