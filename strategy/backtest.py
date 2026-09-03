"""
strategy/backtest.py — Backtesting engine.

Replays historical candles through the breakout strategies and simulates
trades using the same buy/sell logic as the live bot.

Usage:
    python -m strategy.backtest --symbols RELIANCE,TCS --days 180
    python -m strategy.backtest --all-watchlist --days 90
"""
import argparse
import logging
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import numpy as np
import pandas as pd
import yaml

from strategy.breakout import (
    check_resistance_breakout,
    check_volume_breakout,
    check_52week_high_breakout,
    check_bb_squeeze_breakout,
    BreakoutSignal,
)

logger = logging.getLogger("Backtest")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

DEFAULT_SCORES = {
    "volume_breakout": 3,
    "bb_squeeze_breakout": 3,
    "52week_high_breakout": 2,
    "resistance_breakout": 1,
}


def load_config():
    with open("config.yaml") as f:
        return yaml.safe_load(f)


def fetch_candles_for_backtest(kite, symbol: str, interval: str, days: int) -> pd.DataFrame:
    from datetime import datetime, timedelta
    from data.fetcher import DataFetcher
    fetcher = DataFetcher(kite)
    df = fetcher.get_candles(symbol, interval, "NSE")
    return df


def run_backtest(
    df: pd.DataFrame,
    symbol: str,
    cfg: dict,
    profit_pct: float,
    sl_pct: float,
    trail_pct: float,
    use_trailing: bool,
) -> list[dict]:
    """
    Walk-forward simulation: for each candle, check if a breakout signal fires
    using only data up to that candle. If yes, simulate buy. Then simulate sell
    when target/SL is hit on subsequent candles.

    Returns list of trade dicts.
    """
    s_cfg        = cfg["strategy"]
    lookback     = s_cfg.get("lookback_candles", 20)
    volume_mult  = s_cfg.get("volume_multiplier", 1.5)
    min_pct      = s_cfg.get("min_breakout_pct", 0.3)
    bb_period    = s_cfg.get("bb_period", 20)
    bb_std       = s_cfg.get("bb_std", 2.0)
    bb_thresh    = s_cfg.get("bb_squeeze_threshold_pct", 3.0)
    min_required = s_cfg.get("min_signals_required", 2)
    min_score    = s_cfg.get("min_priority_score", 4)
    scores       = {**DEFAULT_SCORES, **s_cfg.get("strategy_scores", {})}

    trades    = []
    in_trade  = False
    buy_price = 0.0
    target    = 0.0
    stop      = 0.0
    highest   = 0.0
    entry_idx = 0

    for i in range(lookback + bb_period + 5, len(df)):
        window = df.iloc[: i + 1]
        close  = float(df.iloc[i]["close"])

        if in_trade:
            # Update trailing SL
            if use_trailing and close > highest:
                highest = close
                stop    = round(highest * (1 - trail_pct / 100), 2)

            hit_target = close >= target
            hit_sl     = close <= stop

            if hit_target or hit_sl:
                pnl    = (close - buy_price) / buy_price * 100
                reason = "target" if hit_target else "stop_loss"
                trades.append({
                    "symbol":     symbol,
                    "entry_date": str(df.index[entry_idx]),
                    "exit_date":  str(df.index[i]),
                    "buy_price":  buy_price,
                    "sell_price": close,
                    "pnl_pct":    round(pnl, 2),
                    "reason":     reason,
                    "bars_held":  i - entry_idx,
                })
                in_trade = False
            continue

        # Check breakout signals on this candle's window
        triggered = []
        checks = [
            check_resistance_breakout(window, symbol, lookback, min_pct),
            check_volume_breakout(window, symbol, lookback, volume_mult, min_pct),
            check_52week_high_breakout(window, symbol),
            check_bb_squeeze_breakout(window, symbol, bb_period, bb_std, bb_thresh),
        ]
        for sig in checks:
            if sig.triggered:
                triggered.append(sig)

        total_score = sum(scores.get(s.signal_type, 1) for s in triggered)
        if len(triggered) >= min_required and total_score >= min_score:
            buy_price = close
            target    = round(buy_price * (1 + profit_pct / 100), 2)
            stop      = round(buy_price * (1 - sl_pct / 100), 2)
            highest   = buy_price
            in_trade  = True
            entry_idx = i

    # Close open trade at end of data
    if in_trade:
        close = float(df.iloc[-1]["close"])
        pnl   = (close - buy_price) / buy_price * 100
        trades.append({
            "symbol":     symbol,
            "entry_date": str(df.index[entry_idx]),
            "exit_date":  str(df.index[-1]),
            "buy_price":  buy_price,
            "sell_price": close,
            "pnl_pct":    round(pnl, 2),
            "reason":     "open_at_end",
            "bars_held":  len(df) - 1 - entry_idx,
        })

    return trades


def print_report(all_trades: list[dict]):
    if not all_trades:
        print("\n⚠️  No trades generated in backtest period.")
        return

    df = pd.DataFrame(all_trades)
    wins    = df[df["pnl_pct"] > 0]
    losses  = df[df["pnl_pct"] <= 0]
    wr      = len(wins) / len(df) * 100
    avg_win = wins["pnl_pct"].mean() if not wins.empty else 0
    avg_los = losses["pnl_pct"].mean() if not losses.empty else 0
    expectancy = (wr / 100 * avg_win) + ((1 - wr / 100) * avg_los)

    # Max drawdown
    cumulative = (1 + df["pnl_pct"] / 100).cumprod()
    rolling_max = cumulative.cummax()
    drawdown = ((cumulative - rolling_max) / rolling_max * 100)
    max_dd  = drawdown.min()

    print("\n" + "═" * 55)
    print("          📊  BACKTEST REPORT")
    print("═" * 55)
    print(f"  Total Trades   : {len(df)}")
    print(f"  Wins           : {len(wins)}  ({wr:.1f}%)")
    print(f"  Losses         : {len(losses)}  ({100 - wr:.1f}%)")
    print(f"  Avg Win        : +{avg_win:.2f}%")
    print(f"  Avg Loss       :  {avg_los:.2f}%")
    print(f"  Expectancy     : {expectancy:+.2f}% per trade")
    print(f"  Max Drawdown   : {max_dd:.2f}%")
    print(f"  Avg Bars Held  : {df['bars_held'].mean():.1f} candles")
    print("─" * 55)
    print("  By Symbol:")
    for sym, grp in df.groupby("symbol"):
        sym_wr = (grp["pnl_pct"] > 0).mean() * 100
        sym_pnl = grp["pnl_pct"].sum()
        print(f"    {sym:<15} {len(grp):>3} trades | WR {sym_wr:.0f}% | Total {sym_pnl:+.1f}%")
    print("═" * 55)

    # Save CSV
    out = "backtest_results.csv"
    df.to_csv(out, index=False)
    print(f"\n  Full results saved to: {out}\n")


def main():
    parser = argparse.ArgumentParser(description="Breakout Bot Backtester")
    parser.add_argument("--symbols", type=str, help="Comma-separated symbols e.g. RELIANCE,TCS")
    parser.add_argument("--days",    type=int, default=180, help="Lookback days (default 180)")
    parser.add_argument("--interval",type=str, default=None, help="Override candle interval")
    parser.add_argument("--all-watchlist", action="store_true", help="Use all symbols from config.yaml")
    args = parser.parse_args()

    cfg = load_config()

    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=cfg["kite"]["api_key"])
    token_file = cfg["kite"]["access_token_file"]
    if not os.path.exists(token_file):
        print("❌ Run auth.py first to generate access token.")
        sys.exit(1)
    with open(token_file) as f:
        kite.set_access_token(f.read().strip())

    if args.all_watchlist:
        symbols = cfg.get("watchlist", [])
    elif args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",")]
    else:
        print("Specify --symbols RELIANCE,TCS or --all-watchlist")
        sys.exit(1)

    interval     = args.interval or cfg["strategy"]["interval"]
    profit_pct   = cfg["trade"]["profit_target_pct"]
    sl_pct       = cfg["trade"]["stop_loss_pct"]
    tsl          = cfg["trade"].get("trailing_stop_loss", {})
    use_trailing = tsl.get("enabled", False)
    trail_pct    = tsl.get("trail_pct", 1.5)

    print(f"\n🔄 Backtesting {len(symbols)} symbols | interval={interval} | "
          f"days={args.days} | trailing_SL={use_trailing}")

    all_trades = []
    for sym in symbols:
        print(f"  Processing {sym}...", end=" ", flush=True)
        df = fetch_candles_for_backtest(kite, sym, interval, args.days)
        if df is None or len(df) < 60:
            print("⚠️  insufficient data")
            continue
        trades = run_backtest(df, sym, cfg, profit_pct, sl_pct, trail_pct, use_trailing)
        all_trades.extend(trades)
        print(f"{len(trades)} trades")

    print_report(all_trades)


if __name__ == "__main__":
    main()
