"""
Telegram Multi-Chat Notification Engine.
Broadcasts formatted trade alerts, morning screener results, hourly P&L,
and emergency circuit breaker triggers to configured chat IDs.
"""
import logging
from typing import List, Optional, Dict
import requests

from core.models import Order, Position, ScreenerCandidate
from config.settings import TelegramConfig

logger = logging.getLogger("TelegramNotifier")


class TelegramNotifier:
    """
    Broadcasts real-time trading notifications to up to 3 Telegram chat IDs.
    """
    def __init__(self, config: TelegramConfig):
        self.config = config
        self.bot_token = config.bot_token
        self.chat_ids = config.chat_ids

    def send_broadcast(self, message: str) -> bool:
        """Sends markdown formatted message to all configured chat IDs."""
        if not self.config.enabled or not self.bot_token or not self.chat_ids:
            logger.debug("Telegram notifications disabled or credentials missing.")
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        success_count = 0

        for chat_id in self.chat_ids:
            payload = {
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True
            }
            try:
                resp = requests.post(url, json=payload, timeout=8)
                if resp.status_code == 200:
                    success_count += 1
                else:
                    logger.warning(f"Telegram send failed for {chat_id}: {resp.text}")
            except Exception as e:
                logger.error(f"Error sending Telegram message to {chat_id}: {e}")

        return success_count > 0

    def notify_morning_screener(self, candidates: List[ScreenerCandidate]):
        """Broadcasts the 09:20 IST Top RVOL Candidates in Play."""
        if not self.config.send_morning_screener or not candidates:
            return

        lines = [
            "🌅 *POWER OF 3 — MORNING SCREENER (09:20 IST)* 🌅",
            f"🎯 *Top {len(candidates)} RVOL Stocks in Play:*\n"
        ]

        for i, c in enumerate(candidates, 1):
            dir_emoji = "🟢 LONG" if c.direction == "LONG" else "🔴 SHORT"
            lines.append(
                f"*{i}. {c.symbol}* | {dir_emoji}\n"
                f"   ⚡ Trigger: `₹{c.trigger_price:.2f}` | RVOL: `{c.rvol:.1f}x`\n"
                f"   📊 14d ATR: `₹{c.atr_14d:.2f}` | Range: `₹{c.opening_low:.2f} - ₹{c.opening_high:.2f}`"
            )

        lines.append("\n⏱ _Breakout orders active until 14:15 IST | Square-off at 14:45 IST_")
        self.send_broadcast("\n".join(lines))

    def notify_trade_entry(
        self, order: Order, stop_loss: Optional[float] = None, target: Optional[float] = None
    ):
        """Broadcasts a new position entry fill."""
        if not self.config.send_trade_signals:
            return

        side_emoji = "🚀 BUY (LONG)" if order.side.value == "BUY" else "🔻 SELL (SHORT)"
        msg = (
            f"⚡ *TRADE EXECUTED* ⚡\n\n"
            f"📈 *Symbol:* `{order.symbol}`\n"
            f"🎯 *Action:* *{side_emoji}*\n"
            f"📦 *Quantity:* `{order.quantity}` shares\n"
            f"💵 *Executed Price:* `₹{order.average_price:.2f}`\n"
            f"🛑 *Stop Loss:* `₹{stop_loss:.2f}`" if stop_loss else ""
        )
        if target:
            msg += f"\n🎯 *Target:* `₹{target:.2f}`"
        msg += f"\n🔖 *Order ID:* `{order.order_id}`\n⏰ *Time:* `{order.created_at.strftime('%H:%M:%S IST')}`"

        self.send_broadcast(msg)

    def notify_trade_exit(self, position: Position, exit_order: Order, reason: str):
        """Broadcasts a position exit and realized P&L."""
        if not self.config.send_trade_signals:
            return

        pnl = position.realized_pnl
        pnl_emoji = "🟢" if pnl >= 0 else "🔴"
        reason_label = {
            "TRAILING_STOP": "Trailing Stop Hit 🛑",
            "STOP_LOSS": "Stop Loss Hit 🛑",
            "TAKE_PROFIT": "Target Achieved 🎯",
            "FORCED_SQUARE_OFF": "Forced 14:45 Square-Off ⏰",
            "SIGNAL": "Strategy Signal 🏁"
        }.get(reason, reason)

        msg = (
            f"{pnl_emoji} *POSITION CLOSED* | {reason_label}\n\n"
            f"📈 *Symbol:* `{position.symbol}` ({position.side.value})\n"
            f"📦 *Qty:* `{exit_order.quantity}` | Exit: `₹{exit_order.average_price:.2f}`\n"
            f"💰 *Net P&L:* *₹{pnl:,.2f}*\n"
            f"⏰ *Time:* `{exit_order.created_at.strftime('%H:%M:%S IST')}`"
        )
        self.send_broadcast(msg)

    def notify_hourly_pnl(self, total_pnl: float, open_count: int, realized_pnl: float):
        """Broadcasts hourly portfolio status."""
        if not self.config.send_hourly_pnl:
            return

        pnl_emoji = "🟢" if total_pnl >= 0 else "🔴"
        msg = (
            f"📊 *HOURLY STATUS UPDATE*\n\n"
            f"💼 *Open Positions:* `{open_count}`\n"
            f"💵 *Realized P&L:* `₹{realized_pnl:,.2f}`\n"
            f"{pnl_emoji} *Total Day P&L:* *₹{total_pnl:,.2f}*\n"
        )
        self.send_broadcast(msg)

    def notify_circuit_breaker(self, total_pnl: float, max_loss_limit: float):
        """Broadcasts emergency circuit breaker halt."""
        msg = (
            f"🚨🚨 *EMERGENCY: CIRCUIT BREAKER TRIGGERED* 🚨🚨\n\n"
            f"⚠️ Total Day Loss (*₹{total_pnl:,.2f}*) reached risk limit (*₹{max_loss_limit:,.2f}*)!\n"
            f"🛑 All trading is HALTED immediately.\n"
            f"⏰ All open positions are being squared off."
        )
        self.send_broadcast(msg)

    def notify_guardrail_rejection(self, symbol: str, side: str, reason: str):
        """Broadcasts when an invalid or double order was blocked by guardrails."""
        logger.warning(f"Guardrail blocked order: {symbol} {side} - {reason}")
