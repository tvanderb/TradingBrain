"""Notifications — dual dispatch to Telegram and WebSocket.

Every system event flows through here. WebSocket always gets everything.
Telegram is filtered by config (telegram.notifications section).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from src.api.websocket import WebSocketManager
    from src.shell.config import NotificationConfig
    from telegram.ext import Application

log = structlog.get_logger()


class Notifier:
    """Dual-dispatch event system: WebSocket (all events) + Telegram (filtered)."""

    def __init__(
        self,
        chat_id: str,
        tg_filter: NotificationConfig | None = None,
        app: Application | None = None,
    ) -> None:
        self._chat_id = chat_id
        self._app = app
        self._tg_filter = tg_filter
        self._ws_manager: WebSocketManager | None = None

    def set_app(self, app: Application) -> None:
        self._app = app

    def set_ws_manager(self, ws_manager: WebSocketManager) -> None:
        self._ws_manager = ws_manager

    def _should_telegram(self, event_name: str) -> bool:
        if self._tg_filter is None:
            return True
        return getattr(self._tg_filter, event_name, True)

    async def _send_telegram(self, text: str) -> None:
        if not self._app or not self._chat_id:
            return
        for attempt in range(3):
            try:
                await self._app.bot.send_message(chat_id=self._chat_id, text=text[:4096])
                return
            except Exception as e:
                if attempt < 2:
                    log.warning("notifier.send_retry", attempt=attempt + 1, error=str(e))
                    await asyncio.sleep(2 ** attempt)
                else:
                    log.error("notifier.send_failed", error=str(e))

    async def _broadcast_ws(self, event: str, data: dict) -> None:
        if self._ws_manager:
            try:
                await self._ws_manager.broadcast({
                    "event": event,
                    "data": data,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
            except Exception as e:
                log.error("notifier.ws_broadcast_failed", event=event, error=str(e))

    async def _dispatch(self, event_name: str, data: dict, telegram_text: str | None = None) -> None:
        """Send to WebSocket (always) and Telegram (if configured, non-blocking)."""
        await self._broadcast_ws(event_name, data)
        if telegram_text and self._should_telegram(event_name):
            asyncio.create_task(self._send_telegram(telegram_text))

    # --- Trade Events ---

    async def trade_executed(self, trade: dict) -> None:
        action = trade.get("action", "?")
        symbol = trade.get("symbol", "?")
        qty = trade.get("qty", 0)
        price = trade.get("price", 0)
        fee = trade.get("fee", 0)
        intent = trade.get("intent", "DAY")
        tag = trade.get("tag", "")

        tag_str = f" [{tag}]" if tag else ""
        lines = [
            f"Trade: {action} {symbol}{tag_str}",
            f"Qty: {qty:.6f} @ ${price:,.2f}",
            f"Fee: ${fee:.4f}",
            f"Intent: {intent}",
        ]
        pnl = trade.get("pnl")
        if pnl is not None:
            lines.append(f"P&L: ${pnl:+.2f} ({trade.get('pnl_pct', 0)*100:+.1f}%)")

        await self._dispatch("trade_executed", trade, "\n".join(lines))

    async def stop_triggered(self, symbol: str, reason: str, price: float, tag: str = "") -> None:
        tag_str = f" [{tag}]" if tag else ""
        await self._dispatch(
            "stop_triggered",
            {"symbol": symbol, "reason": reason, "price": price, "tag": tag},
            f"Stop Triggered: {symbol}{tag_str}\nReason: {reason}\nPrice: ${price:,.2f}",
        )

    async def signal_rejected(self, symbol: str, action: str, reason: str) -> None:
        await self._dispatch(
            "signal_rejected",
            {"symbol": symbol, "action": action, "reason": reason},
            f"Signal Rejected: {action} {symbol}\nReason: {reason}",
        )

    # --- Risk Events ---

    async def risk_halt(self, reason: str) -> None:
        await self._dispatch(
            "risk_halt",
            {"reason": reason},
            f"TRADING HALTED\nReason: {reason}",
        )

    async def risk_resumed(self) -> None:
        await self._dispatch(
            "risk_resumed",
            {},
            "Trading Resumed — halt cleared",
        )

    async def rollback_alert(self, reason: str, version: str) -> None:
        await self._dispatch(
            "strategy_rollback",
            {"reason": reason, "version": version},
            f"ROLLBACK TRIGGERED\nReason: {reason}\nRolled back to: {version}",
        )

    # --- Scan Events ---

    async def scan_complete(self, symbol_count: int, signal_count: int) -> None:
        await self._dispatch(
            "scan_complete",
            {"symbol_count": symbol_count, "signal_count": signal_count},
            f"Scan Complete: {symbol_count} symbols, {signal_count} signals",
        )

    # --- Strategy Events ---

    async def strategy_deployed(self, version: str, tier: int, changes: str) -> None:
        tier_name = {1: "Tweak", 2: "Restructure", 3: "Overhaul"}.get(tier, "Unknown")
        await self._dispatch(
            "strategy_deployed",
            {"version": version, "tier": tier, "tier_name": tier_name, "changes": changes[:500]},
            f"Strategy Deployed: {version}\nType: {tier_name} (tier {tier})\nChanges: {changes[:500]}",
        )

    async def paper_test_started(self, version: str, days: int) -> None:
        await self._dispatch(
            "paper_test_started",
            {"version": version, "days": days},
            f"Paper Test Started: {version} ({days} days)",
        )

    async def paper_test_completed(self, version: str, passed: bool, results: dict) -> None:
        status = "PASSED" if passed else "FAILED"
        await self._dispatch(
            "paper_test_completed",
            {"version": version, "passed": passed, "results": results},
            f"Paper Test {status}: {version}\nTrades: {results.get('trades', 0)}, P&L: ${results.get('pnl', 0):+.2f}",
        )

    # --- Orchestrator Events ---

    async def orchestrator_cycle_started(self) -> None:
        await self._dispatch(
            "orchestrator_cycle_started",
            {},
            "Orchestrator: Nightly cycle started",
        )

    async def orchestrator_cycle_completed(self, decision_type: str) -> None:
        await self._dispatch(
            "orchestrator_cycle_completed",
            {"decision_type": decision_type},
            f"Orchestrator: Cycle complete — {decision_type}",
        )

    async def daily_summary(self, summary: str) -> None:
        await self._dispatch(
            "daily_summary",
            {"summary": summary},
            summary,
        )

    async def weekly_report(self, report: str) -> None:
        await self._dispatch(
            "weekly_report",
            {"report": report},
            report,
        )

    # --- System Events ---

    async def system_online(self, portfolio_value: float, positions: int) -> None:
        await self._dispatch(
            "system_online",
            {"portfolio_value": portfolio_value, "positions": positions},
            f"System Online\nPortfolio: ${portfolio_value:.2f}\nPositions: {positions}",
        )

    async def system_shutdown(self) -> None:
        await self._dispatch(
            "system_shutdown",
            {},
            "System Shutting Down",
        )

    async def system_error(self, error: str) -> None:
        await self._dispatch(
            "system_error",
            {"message": error[:500]},
            f"System Error: {error[:500]}",
        )

    async def websocket_failed(self) -> None:
        await self._dispatch(
            "websocket_feed_lost",
            {},
            "WARNING: WebSocket permanently disconnected after max retries.\n"
            "Live price feed is down. Position monitor using REST fallback.",
        )
