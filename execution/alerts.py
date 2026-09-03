"""
execution/alerts.py — Telegram notifications for trade events.

Setup:
  1. Message @BotFather on Telegram → /newbot → copy token
  2. Message @userinfobot → copy your chat_id
  3. Set telegram.enabled: true in config.yaml
"""
import logging
from datetime import datetime
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class Alerter:
    def __init__(self, cfg: dict):
        t_cfg = cfg.get("telegram", {})
        self.enabled   = t_cfg.get("enabled", False)
        self.token     = t_cfg.get("bot_token", "")
        self.chat_id   = t_cfg.get("chat_id", "")
        self.on_buy    = t_cfg.get("alert_on_buy", True)
        self.on_sell   = t_cfg.get("alert_on_sell", True)
        self.on_error  = t_cfg.get("alert_on_error", True)
        self.on_daily  = t_cfg.get("alert_daily_summary", True)
        self.dry_run   = cfg.get("dry_run", False)

    def buy(self, symbol: str, qty: int, price: float, target: float, sl: float,
            signal_type: str, score: int = 0):
        if not self.on_buy:
            return
        tag = "🟡 [DRY RUN] " if self.dry_run else ""
        self._send(
            f"{tag}✅ *BUY* `{symbol}`\n"
            f"  Qty: {qty} @ ₹{price:.2f}\n"
            f"  Target: ₹{target:.2f} | SL: ₹{sl:.2f}\n"
            f"  Signal: `{signal_type}` | Score: {score}"
        )

    def sell(self, symbol: str, qty: int, price: float, pnl: float, reason: str):
        if not self.on_sell:
            return
        tag    = "🟡 [DRY RUN] " if self.dry_run else ""
        emoji  = "🟢" if pnl >= 0 else "🔴"
        self._send(
            f"{tag}{emoji} *SELL* `{symbol}`\n"
            f"  Qty: {qty} @ ₹{price:.2f}\n"
            f"  P&L: ₹{pnl:+.2f} | Reason: {reason}"
        )

    def error(self, message: str):
        if not self.on_error:
            return
        self._send(f"⚠️ *BOT ERROR*\n`{message}`")

    def circuit_breaker(self, daily_loss: float, limit: float):
        self._send(
            f"🚨 *CIRCUIT BREAKER TRIGGERED*\n"
            f"  Daily loss ₹{daily_loss:.2f} exceeded limit ₹{limit:.2f}\n"
            f"  No new trades will be placed today."
        )

    def daily_summary(self, trades: list[dict]):
        if not self.on_daily:
            return
        total_pnl = sum(t.get("pnl") or 0 for t in trades)
        wins      = sum(1 for t in trades if (t.get("pnl") or 0) > 0)
        losses    = sum(1 for t in trades if (t.get("pnl") or 0) < 0)
        win_rate  = (wins / len(trades) * 100) if trades else 0
        self._send(
            f"📊 *Daily Summary* — {datetime.now().strftime('%d %b %Y')}\n"
            f"  Trades: {len(trades)} (✅{wins} / ❌{losses})\n"
            f"  Win Rate: {win_rate:.1f}%\n"
            f"  Total P&L: ₹{total_pnl:+.2f}"
        )

    def _send(self, text: str):
        if not self.enabled:
            logger.debug(f"[ALERT suppressed] {text[:80]}")
            return
        if not self.token or not self.chat_id:
            logger.warning("Telegram token/chat_id not set — alert skipped")
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            resp = requests.post(
                url,
                json={"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"},
                timeout=5,
            )
            if not resp.ok:
                logger.warning(f"Telegram alert failed: {resp.text}")
        except Exception as e:
            logger.warning(f"Telegram send error: {e}")
