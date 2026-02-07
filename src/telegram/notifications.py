"""Outbound Telegram notifications.

Sends alerts for trades, stop triggers, daily summaries, and system events.
"""

from __future__ import annotations

from telegram import Bot

from src.core.config import Config
from src.core.logging import get_logger

log = get_logger("notifications")


class Notifier:
    """Sends Telegram notifications for system events."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._bot: Bot | None = None
        self._chat_id = config.telegram.chat_id

        if config.telegram.enabled and config.telegram.bot_token:
            self._bot = Bot(token=config.telegram.bot_token)

    async def send(self, message: str) -> None:
        """Send a message to the configured chat."""
        if not self._bot or not self._chat_id:
            log.info("notification_skipped", msg=message[:50])
            return
        try:
            await self._bot.send_message(chat_id=self._chat_id, text=message)
        except Exception as e:
            log.error("notification_error", error=str(e))

    async def trade_executed(self, result: object, signal: object) -> None:
        await self.send(
            f"Trade Executed\n"
            f"{'BUY' if signal.direction == 'long' else 'SELL'} {signal.symbol}\n"
            f"Price: ${result.filled_price:.2f}\n"
            f"Signal: {signal.signal_type} (strength={signal.strength:.2f})\n"
            f"Commission: ${result.commission:.4f}"
        )

    async def stop_triggered(self, symbol: str, pnl: float) -> None:
        icon = "+" if pnl >= 0 else ""
        trigger_type = "Take Profit" if pnl >= 0 else "Stop Loss"
        await self.send(f"{trigger_type}: {symbol}\nP&L: {icon}${pnl:.2f}")

    async def daily_summary(self, status: dict) -> None:
        await self.send(
            f"Daily Summary\n\n"
            f"Portfolio: ${status['portfolio_value']:.2f}\n"
            f"Day P&L: ${status['daily_pnl']:.2f}\n"
            f"Trades: {status['daily_trades']}\n"
            f"Positions: {status['open_positions']}"
        )

    async def evolution_complete(self, summary: dict) -> None:
        analysis = summary.get("analysis", {})
        changes = summary.get("changes", {})
        n_changes = len(changes.get("adjustments", {})) if changes else 0
        await self.send(
            f"Brain Evolution Complete\n\n"
            f"{analysis.get('overall_assessment', 'No assessment')}\n"
            f"Parameter changes: {n_changes}"
        )

    async def error(self, context: str, error: str) -> None:
        await self.send(f"ERROR [{context}]\n{error[:200]}")

    async def fee_update(self, maker: float, taker: float, tier: str) -> None:
        await self.send(
            f"Fee Schedule Updated\n"
            f"Maker: {maker}%\n"
            f"Taker: {taker}%\n"
            f"Tier: {tier}"
        )
