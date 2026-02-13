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
    from src.shell.activity import ActivityLogger
    from src.shell.config import NotificationConfig
    from telegram.ext import Application

log = structlog.get_logger()

# Event-to-activity mapping: event_name -> (category, severity)
_EVENT_ACTIVITY: dict[str, tuple[str, str]] = {
    "trade_executed":              ("TRADE",    "info"),
    "stop_triggered":              ("TRADE",    "warning"),
    "signal_rejected":             ("RISK",     "info"),
    "risk_halt":                   ("RISK",     "error"),
    "risk_resumed":                ("RISK",     "info"),
    "strategy_rollback":           ("RISK",     "error"),
    "scan_complete":               ("SCAN",     "info"),
    "strategy_deployed":           ("STRATEGY", "info"),
    "paper_test_started":          ("STRATEGY", "info"),
    "paper_test_completed":        ("STRATEGY", "info"),
    "orchestrator_cycle_started":  ("ORCH",     "info"),
    "orchestrator_cycle_completed": ("ORCH",    "info"),
    "daily_summary":               ("ORCH",     "info"),
    "weekly_report":               ("ORCH",     "info"),
    "system_online":               ("SYSTEM",   "info"),
    "system_shutdown":             ("SYSTEM",   "info"),
    "system_error":                ("SYSTEM",   "error"),
    "websocket_feed_lost":         ("SYSTEM",   "error"),
    "candidate_created":           ("STRATEGY", "info"),
    "candidate_canceled":          ("STRATEGY", "info"),
    "candidate_promoted":          ("STRATEGY", "info"),
}


def _format_activity(event_name: str, data: dict) -> str | None:
    """Format an event into a one-line activity summary. Returns None to skip."""
    if event_name == "trade_executed":
        action = data.get("action", "?")
        qty = data.get("qty", 0)
        symbol = data.get("symbol", "?")
        tag = data.get("tag", "")
        price = data.get("price", 0)
        tag_str = f" [{tag}]" if tag else ""
        parts = [f"{action} {qty:.8f} {symbol}{tag_str} @ ${price:,.2f}"]
        pnl = data.get("pnl")
        if pnl is not None:
            parts.append(f"P&L ${pnl:+.2f}")
        return " ".join(parts)

    if event_name == "stop_triggered":
        symbol = data.get("symbol", "?")
        reason = data.get("reason", "SL")
        price = data.get("price", 0)
        return f"{reason.upper()} triggered on {symbol} @ ${price:,.2f}"

    if event_name == "signal_rejected":
        action = data.get("action", "?")
        symbol = data.get("symbol", "?")
        reason = data.get("reason", "unknown")
        return f"{action} {symbol} rejected: {reason}"

    if event_name == "risk_halt":
        reason = data.get("reason", "unknown")
        return f"TRADING HALTED: {reason}"

    if event_name == "risk_resumed":
        return "Trading resumed — halt cleared"

    if event_name == "strategy_rollback":
        version = data.get("version", "?")
        reason = data.get("reason", "")
        return f"ROLLBACK to {version}: {reason}"

    if event_name == "scan_complete":
        signal_count = data.get("signal_count", 0)
        if signal_count == 0:
            return None  # Skip empty scans
        symbol_count = data.get("symbol_count", 0)
        return f"Scan: {symbol_count} symbols, {signal_count} signals"

    if event_name == "strategy_deployed":
        version = data.get("version", "?")
        tier_name = data.get("tier_name", "?")
        tier = data.get("tier", 0)
        return f"Strategy {version} deployed (tier {tier}: {tier_name})"

    if event_name == "paper_test_started":
        version = data.get("version", "?")
        days = data.get("days", "?")
        return f"Paper test started: {version} ({days} days)"

    if event_name == "paper_test_completed":
        version = data.get("version", "?")
        passed = data.get("passed", False)
        results = data.get("results", {})
        status = "PASSED" if passed else "FAILED"
        trades = results.get("trades", 0)
        pnl = results.get("pnl", 0)
        return f"Paper test {status}: {version} ({trades} trades, ${pnl:+.2f})"

    if event_name == "orchestrator_cycle_started":
        return "Nightly orchestration cycle started"

    if event_name == "orchestrator_cycle_completed":
        decision = data.get("decision_type", "?")
        return f"Orchestration complete: {decision}"

    if event_name == "daily_summary":
        summary = data.get("summary", "")
        return f"Daily summary: {summary[:120]}"

    if event_name == "weekly_report":
        report = data.get("report", "")
        return f"Weekly report: {report[:120]}"

    if event_name == "system_online":
        pv = data.get("portfolio_value", 0)
        pos = data.get("positions", 0)
        return f"System online: ${pv:.2f} portfolio, {pos} positions"

    if event_name == "system_shutdown":
        return "System shutting down"

    if event_name == "system_error":
        msg = data.get("message", "unknown")
        return f"ERROR: {msg[:200]}"

    if event_name == "websocket_feed_lost":
        return "WebSocket feed lost — REST fallback active"

    if event_name == "candidate_created":
        slot = data.get("slot", "?")
        version = data.get("version", "?")
        eval_days = data.get("eval_days")
        eval_str = f"{eval_days}d" if eval_days else "indefinite"
        return f"Candidate created: slot {slot}, {version} ({eval_str} eval)"

    if event_name == "candidate_canceled":
        slot = data.get("slot", "?")
        return f"Candidate canceled: slot {slot}"

    if event_name == "candidate_promoted":
        slot = data.get("slot", "?")
        version = data.get("version", "?")
        return f"Candidate promoted: slot {slot} → {version}"

    return None


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
        self._activity_logger: ActivityLogger | None = None

    def set_app(self, app: Application) -> None:
        self._app = app

    def set_ws_manager(self, ws_manager: WebSocketManager) -> None:
        self._ws_manager = ws_manager

    def set_activity_logger(self, logger: ActivityLogger) -> None:
        self._activity_logger = logger

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

        # Activity log hook
        if self._activity_logger:
            meta = _EVENT_ACTIVITY.get(event_name)
            if meta:
                summary = _format_activity(event_name, data)
                if summary is not None:
                    try:
                        await self._activity_logger.log(meta[0], summary, meta[1], detail=data)
                    except Exception:
                        pass  # Activity log must never break notifications

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

    # --- Candidate Events ---

    async def candidate_created(self, slot: int, version: str, eval_days: int | None = None) -> None:
        eval_str = f"{eval_days}d" if eval_days else "indefinite"
        await self._dispatch(
            "candidate_created",
            {"slot": slot, "version": version, "eval_days": eval_days},
            f"Candidate Created: slot {slot}\nVersion: {version}\nEvaluation: {eval_str}",
        )

    async def candidate_canceled(self, slot: int) -> None:
        await self._dispatch(
            "candidate_canceled",
            {"slot": slot},
            f"Candidate Canceled: slot {slot}",
        )

    async def candidate_promoted(self, slot: int, version: str) -> None:
        await self._dispatch(
            "candidate_promoted",
            {"slot": slot, "version": version},
            f"Candidate Promoted: slot {slot} → {version}",
        )
