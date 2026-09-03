# 📈 NSE Intraday Trading Bot

An automated intraday trading bot for **NSE Indian markets** built in Python.  
Scans a dynamic watchlist every few minutes, detects high-probability setups, and automatically places and manages MIS (intraday) orders via **Zerodha Kite API**.  
Real-time market data is sourced for free via **Angel One SmartAPI**.

---

## 🏆 Live Performance (as of Sep 2026)

| Metric | Value |
|---|---|
| Total closed trades | 23 |
| **Win rate** | **52.2%** |
| Total net P&L | ₹1,845 |
| Avg winning trade | ₹+396 |
| Avg losing trade | ₹−264 |
| Win/Loss ratio | **1.5×** |
| Max open positions | 4 |
| Capital per trade | ₹15,000 (5× MIS margin = ₹75,000 exposure) |

### Per-Strategy Breakdown

| Strategy | Trades | Win Rate | Net P&L |
|---|---|---|---|
| ORB Long | 7 | 57% | ₹+1,059 |
| ORB Short | 12 | 50% | ₹+908 |
| NR7 Breakout | 1 | 100% | ₹+210 |
| BB Squeeze Short | 2 | 50% | ₹−33 |
| Gap & Go Long | 1 | 0% | ₹−299 |

> ⚠️ Small sample size — performance will stabilise over more trades.

---

## 🧠 How It Works

### Strategies

| Strategy | Score | Description |
|---|---|---|
| **Gap & Go** | 5 | Stock gaps up/down ≥1.5% at open with high volume — momentum continuation |
| **ORB (Opening Range Breakout)** | 4 | Breakout above/below the first 15-min candle range, with candle-close confirmation |
| **VWAP Pullback** | 4 | Price pulls back to VWAP then bounces — continuation of intraday trend |
| **EMA Pullback** | 3 | Price dips to EMA9/21 confluence then recovers |
| **NR7 Breakout** | 3 | Narrowest range in 7 days — volatility expansion breakout |
| **BB Squeeze** | 2 | Bollinger Band squeeze then expansion |

Signals are scored and ranked. Higher score = higher priority when capital is limited.

### Market Filters (applied before every trade)
- **India VIX > 22** → block all trades (panic market)
- **India VIX 18–22** → reduce position size by 50%
- **Nifty < −0.5% from open** → block all LONG signals
- **Nifty > +0.5% from open** → block all SHORT signals
- **Nifty flat (±0.3%)** → block all ORB signals (no momentum = fake breakouts)

### Entry Rules
- ORB signals: only valid **9:30–10:00 AM** (momentum fades after 30 min)
- VWAP/EMA/NR7: valid until **1:00 PM**
- Dead zone **11:00–12:00 AM**: score threshold raised by +1 (low liquidity)
- No new entries within **20 min** of force-close

### Exit Rules
- **Profit target**: fixed % per signal type (configurable per strategy)
- **Trailing stop-loss**: 0.5% trail, tightens to 0.3% after 12:30 PM
- **Partial exit**: close 50% at 1× risk → move SL to breakeven
- **Force close at 1:30 PM**: staged exit — losers first, profitable positions get a 60-second tight trail window before hard close
- **Safety close at 3:10 PM**: catches anything remaining

### Position Sizing
- ATR-based sizing (risk ₹500 per trade, adjusted for 5× MIS margin)
- Hard cap: ₹15,000 capital per trade, ₹60,000 per day, max 4 open positions
- No duplicate symbols on the same day

---

## 🚀 Quick Start

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure
```bash
cp config.example.yaml config.yaml
```
Edit `config.yaml` and fill in:
- **Angel One SmartAPI** credentials (free real-time data)
- **Zerodha Kite** API key + secret (for order placement)
- Your watchlist

### 3. Login (once per trading day)
```bash
python auth.py
```
Opens Zerodha in your browser → saves your access token to `access_token.txt`.

### 4. Run the bot
```bash
python main.py
```

### 5. View the dashboard
```bash
python -m streamlit run dashboard/app.py
```
Open [http://localhost:8501](http://localhost:8501)

---

## 📐 Project Structure

```
trading-bot/
├── config.yaml               # All settings (DO NOT commit — see .gitignore)
├── config.example.yaml       # Template with placeholders — share this
├── auth.py                   # Daily Zerodha login helper
├── main.py                   # Bot entry point + APScheduler jobs
│
├── strategy/
│   ├── screener.py           # Scans watchlist, applies market filters
│   ├── orb.py                # Opening Range Breakout
│   ├── breakout.py           # NR7, BB Squeeze, SuperTrend
│   ├── gap.py                # Gap & Go
│   ├── vwap_pullback.py      # VWAP Pullback
│   ├── ema_pullback.py       # EMA 9/21 Pullback
│   ├── market_filter.py      # India VIX + Nifty trend filter
│   └── indicators.py         # RSI, VWAP, ATR, BB, SuperTrend helpers
│
├── execution/
│   ├── orders.py             # Order placement, trailing SL, partial exit
│   ├── portfolio.py          # SQLite position tracking
│   ├── costs.py              # NSE MIS transaction cost calculator
│   └── alerts.py             # Telegram alerts
│
├── data/
│   ├── angel_fetcher.py      # Angel One SmartAPI (real-time, free)
│   ├── yf_fetcher.py         # yfinance fallback (15-min delay)
│   └── watchlist_builder.py  # Dynamic watchlist from NSE universe
│
├── dashboard/
│   └── app.py                # Streamlit live dashboard
│
├── trading_bot.db            # SQLite trade database (auto-created)
└── requirements.txt
```

---

## ⚙️ Key Config Options

```yaml
dry_run: true                  # ALWAYS start with this — no real orders placed

trade:
  max_capital_per_trade: 15000  # Max ₹ per trade (5× margin = ₹75k exposure)
  max_daily_capital: 60000      # Stop opening new positions after this
  max_open_positions: 4         # Maximum simultaneous positions
  mis_margin_multiplier: 5.0    # Zerodha MIS intraday margin

scheduler:
  no_new_positions_after: "11:00"          # ORB cutoff
  no_new_positions_after_non_orb: "13:00"  # VWAP/EMA/NR7 cutoff

market_filters:
  india_vix:
    block_above: 22       # Block all trades when VIX > 22
    reduce_size_above: 18 # Half size when VIX 18–22
  nifty_trend:
    block_longs_below_pct: -0.5   # No longs when Nifty down >0.5%
    block_shorts_above_pct: 0.5   # No shorts when Nifty up >0.5%
    min_move_for_orb_pct: 0.3     # ORB needs Nifty moving ±0.3%
```

---

## 📊 Dashboard

Run `python -m streamlit run dashboard/app.py` to see:
- Live open positions with P&L
- Closed trade history with gross/net P&L and transaction costs
- Cumulative P&L chart
- Trade log

---

## 🔐 Security

- **Never commit `config.yaml`** — it contains your API keys and passwords
- `config.yaml` and `access_token.txt` are in `.gitignore`
- Share `config.example.yaml` instead — it has placeholder values only
- Use `dry_run: true` until you have verified the bot behaves correctly

---

## ⚠️ Disclaimer

This bot places **real money orders on a live brokerage account**.

- Start with `dry_run: true` — paper trade for at least 1–2 weeks
- Monitor the bot actively while it runs
- Past performance (shown above) is on a small sample and does not guarantee future results
- You are **solely responsible** for any financial losses
- This is not financial advice

