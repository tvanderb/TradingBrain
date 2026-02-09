"""Telegram Notifications — proactive alerts to the user.

Sends notifications for:
- Trade executed (entry/exit)
- Stop-loss / take-profit hit
- Daily P&L summary
- Weekly performance report
- Strategy changes
- Rollback alerts
- System errors
"""

from __future__ import annotations

import structlog
from telegram.ext import Application

log = structlog.get_logger()


class Notifier:
    """Sends proactive Telegram notifications."""

    def __init__(self, chat_id: str, app: Application | None = None) -> None:
        self._chat_id = chat_id
        self._app = app

    def set_app(self, app: Application) -> None:
        self._app = app

    async def _send(self, text: str) -> None:
        if not self._app or not self._chat_id:
            log.debug("notifier.skip", reason="no app or chat_id")
            return
        try:
            await self._app.bot.send_message(chat_id=self._chat_id, text=text[:4096])
        except Exception as e:
            log.error("notifier.send_failed", error=str(e))

    async def trade_executed(self, trade: dict) -> None:
        action = trade.get("action", "?")
        symbol = trade.get("symbol", "?")
        qty = trade.get("qty", 0)
        price = trade.get("price", 0)
        fee = trade.get("fee", 0)
        intent = trade.get("intent", "DAY")

        lines = [
            f"Trade: {action} {symbol}",
            f"Qty: {qty:.6f} @ ${price:,.2f}",
            f"Fee: ${fee:.4f}",
            f"Intent: {intent}",
        ]

        pnl = trade.get("pnl")
        if pnl is not None:
            lines.append(f"P&L: ${pnl:+.2f} ({trade.get('pnl_pct', 0)*100:+.1f}%)")

        await self._send("\n".join(lines))

    async def stop_triggered(self, symbol: str, reason: str, price: float) -> None:
        await self._send(f"Stop Triggered: {symbol}\nReason: {reason}\nPrice: ${price:,.2f}")

    async def daily_summary(self, summary: str) -> None:
        await self._send(summary)

    async def weekly_report(self, report: str) -> None:
        await self._send(report)

    async def strategy_change(self, version: str, tier: int, changes: str) -> None:
        tier_name = {1: "Tweak", 2: "Restructure", 3: "Overhaul"}.get(tier, "Unknown")
        await self._send(
            f"Strategy Change: {version}\n"
            f"Type: {tier_name} (tier {tier})\n"
            f"Changes: {changes[:500]}"
        )

    async def rollback_alert(self, reason: str, version: str) -> None:
        await self._send(f"ROLLBACK TRIGGERED\nReason: {reason}\nRolled back to: {version}")

    async def system_error(self, error: str) -> None:
        await self._send(f"System Error: {error[:500]}")

    async def websocket_failed(self) -> None:
        await self._send(
            "WARNING: WebSocket permanently disconnected after max retries.\n"
            "Live price feed is down. Position monitor using REST fallback."
        )

    async def system_online(self, portfolio_value: float, positions: int) -> None:
        await self._send(
            f"System Online\n"
            f"Portfolio: ${portfolio_value:.2f}\n"
            f"Positions: {positions}"
        )
