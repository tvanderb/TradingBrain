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
    "candidate_trade_executed":    ("CANDIDATE", "info"),
    "candidate_stop_triggered":    ("CANDIDATE", "warning"),
    "orchestrator_observation":     ("ORCH",      "info"),
    "reflection_completed":        ("ORCH",      "info"),
    "signal_drought":              ("SCAN",      "warning"),
    "config_reloaded":             ("SYSTEM",    "info"),
}


def _format_hold_duration(opened_at: str | None) -> str:
    """Format hold duration from opened_at timestamp to now."""
    if not opened_at:
        return "?"
    try:
        opened = datetime.fromisoformat(opened_at).replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - opened
        days = delta.days
        hours = delta.seconds // 3600
        if days > 0:
            return f"{days}d {hours}h"
        return f"{hours}h"
    except (ValueError, TypeError):
        return "?"


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
        return "Trading resumed \u2014 halt cleared"

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
        return "Orchestration cycle started"

    if event_name == "orchestrator_cycle_completed":
        decision = data.get("decision_type", "?")
        return f"Orchestration complete: {decision}"

    if event_name == "orchestrator_observation":
        market = data.get("market_observations", "")
        reasoning = data.get("reasoning", "")
        summary_text = market or reasoning
        if summary_text:
            return f"Observation: {summary_text[:120]}"
        return None

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
        return "WebSocket feed lost \u2014 REST fallback active"

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
        return f"Candidate promoted: slot {slot} \u2192 {version}"

    if event_name == "candidate_trade_executed":
        slot = data.get("slot", "?")
        action = data.get("action", "?")
        qty = data.get("qty", 0)
        symbol = data.get("symbol", "?")
        tag = data.get("tag", "")
        price = data.get("price", 0)
        tag_str = f" [{tag}]" if tag else ""
        parts = [f"[C{slot}] {action} {qty:.8f} {symbol}{tag_str} @ ${price:,.2f}"]
        pnl = data.get("pnl")
        if pnl is not None:
            parts.append(f"P&L ${pnl:+.2f}")
        return " ".join(parts)

    if event_name == "candidate_stop_triggered":
        slot = data.get("slot", "?")
        symbol = data.get("symbol", "?")
        reason = data.get("close_reason", "stop_loss")
        price = data.get("price", 0)
        return f"[C{slot}] {reason.upper()} triggered on {symbol} @ ${price:,.2f}"

    if event_name == "reflection_completed":
        graded = data.get("predictions_graded", 0)
        new = data.get("new_predictions", 0)
        correct = data.get("correct")
        if correct is not None:
            incorrect = data.get("incorrect", 0)
            uncertain = data.get("uncertain", 0)
            return f"Reflection: {graded} graded ({correct}\u2713 {incorrect}\u2717 {uncertain}?), {new} new predictions"
        return f"Reflection: {graded} predictions graded, {new} new predictions"

    if event_name == "signal_drought":
        hours = data.get("hours", 0)
        return f"Signal drought: {hours}h without signals"

    if event_name == "config_reloaded":
        changes = data.get("changes", [])
        return f"Config reloaded: {len(changes)} field(s) updated" if changes else "Config reloaded: no changes"

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

    def reload_notification_config(self, config: NotificationConfig) -> None:
        """Hot-reload notification filter config."""
        self._tg_filter = config

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

        if action == "BUY":
            size_pct = trade.get("size_pct")
            size_str = f" ({size_pct*100:.1f}%)" if size_pct else ""
            sl = trade.get("stop_loss")
            tp = trade.get("take_profit")

            lines = [
                f"\U0001F4C8 BUY {qty:.6f} {symbol}{tag_str}",
                f"Entry: ${price:,.2f} | Size: ${qty * price:,.2f}{size_str}",
            ]
            if sl or tp:
                sl_parts = []
                if sl:
                    sl_pct = (sl - price) / price * 100 if price else 0
                    sl_parts.append(f"SL: ${sl:,.2f} ({sl_pct:+.1f}%)")
                if tp:
                    tp_pct = (tp - price) / price * 100 if price else 0
                    sl_parts.append(f"TP: ${tp:,.2f} ({tp_pct:+.1f}%)")
                lines.append(" | ".join(sl_parts))
            lines.append(f"Intent: {intent} | Fee: ${fee:.2f}")
            # Portfolio context (added by call site)
            pv = trade.get("portfolio_value")
            pc = trade.get("position_count")
            mp = trade.get("max_positions")
            if pv is not None:
                ctx = f"Portfolio: ${pv:,.2f}"
                if pc is not None and mp is not None:
                    ctx += f" | Positions: {pc}/{mp}"
                lines.append(ctx)
        else:
            # SELL / CLOSE
            entry = trade.get("entry_price")
            pnl = trade.get("pnl")
            pnl_pct = trade.get("pnl_pct", 0) * 100
            close_reason = trade.get("close_reason", "signal")
            opened_at = trade.get("opened_at")
            hold = _format_hold_duration(opened_at)

            lines = [f"\U0001F4C9 {action} {qty:.6f} {symbol}{tag_str}"]
            exit_str = f"Exit: ${price:,.2f}"
            if entry:
                exit_str += f" | Entry: ${entry:,.2f}"
            lines.append(exit_str)
            if pnl is not None:
                lines.append(f"P&L: ${pnl:+.2f} ({pnl_pct:+.1f}%) | Fee: ${fee:.2f}")
            lines.append(f"Hold: {hold} | Close: {close_reason}")
            pv = trade.get("portfolio_value")
            cash = trade.get("cash")
            if pv is not None:
                ctx = f"Portfolio: ${pv:,.2f}"
                if cash is not None:
                    ctx += f" | Cash: ${cash:,.2f}"
                lines.append(ctx)

        await self._dispatch("trade_executed", trade, "\n".join(lines))

    async def stop_triggered(
        self, symbol: str, reason: str, price: float, tag: str = "",
        *, context: dict | None = None,
    ) -> None:
        tag_str = f" [{tag}]" if tag else ""
        data = {"symbol": symbol, "reason": reason, "price": price, "tag": tag}

        if reason == "take_profit":
            header = f"\U0001F3AF TAKE PROFIT \u2014 {symbol}{tag_str}"
        else:
            header = f"\U0001F6D1 STOP LOSS \u2014 {symbol}{tag_str}"
        lines = [header]
        exit_str = f"Exit: ${price:,.2f}"
        if context:
            data.update(context)
            entry = context.get("entry_price")
            if entry:
                exit_str += f" | Entry: ${entry:,.2f}"
            lines.append(exit_str)
            pnl = context.get("pnl")
            fee = context.get("fee", 0)
            if pnl is not None:
                pnl_pct = context.get("pnl_pct", 0) * 100
                pnl_str = f"P&L: ${pnl:+.2f} ({pnl_pct:+.1f}%)"
                if fee:
                    pnl_str += f" | Fee: ${fee:.2f}"
                lines.append(pnl_str)
            hold = _format_hold_duration(context.get("opened_at"))
            lines.append(f"Hold: {hold}")
            pv = context.get("portfolio_value")
            pc = context.get("position_count")
            mp = context.get("max_positions")
            cash = context.get("cash")
            if pv is not None:
                ctx = f"Portfolio: ${pv:,.2f}"
                if cash is not None:
                    ctx += f" | Cash: ${cash:,.2f}"
                elif pc is not None and mp is not None:
                    ctx += f" | Positions: {pc}/{mp}"
                lines.append(ctx)
        else:
            lines.append(exit_str)
            lines.append(f"Reason: {reason}")

        await self._dispatch("stop_triggered", data, "\n".join(lines))

    async def signal_rejected(
        self, symbol: str, action: str, reason: str,
        *, confidence: float | None = None, size_pct: float | None = None,
    ) -> None:
        data = {"symbol": symbol, "action": action, "reason": reason}
        lines = [
            f"\u26D4 Signal Rejected: {action} {symbol}",
            f"Reason: {reason}",
        ]
        extras = []
        if confidence is not None:
            data["confidence"] = confidence
            extras.append(f"Confidence: {confidence:.2f}")
        if size_pct is not None:
            data["size_pct"] = size_pct
            extras.append(f"Size: {size_pct*100:.1f}%")
        if extras:
            lines.append(" | ".join(extras))
        await self._dispatch("signal_rejected", data, "\n".join(lines))

    # --- Risk Events ---

    async def risk_halt(
        self, reason: str,
        *, daily_pnl: float | None = None, position_count: int | None = None,
    ) -> None:
        data: dict = {"reason": reason}
        lines = [
            "\U0001F6A8 TRADING HALTED",
            f"Reason: {reason}",
        ]
        extras = []
        if daily_pnl is not None:
            data["daily_pnl"] = daily_pnl
            extras.append(f"Daily P&L: ${daily_pnl:+.2f}")
        if position_count is not None:
            data["position_count"] = position_count
            extras.append(f"Positions: {position_count} open")
        if extras:
            lines.append(" | ".join(extras))
        await self._dispatch("risk_halt", data, "\n".join(lines))

    async def risk_resumed(
        self,
        *, portfolio_value: float | None = None, position_count: int | None = None,
    ) -> None:
        data: dict = {}
        lines = ["\u2705 Trading Resumed \u2014 halt cleared"]
        if portfolio_value is not None:
            data["portfolio_value"] = portfolio_value
            ctx = f"Portfolio: ${portfolio_value:,.2f}"
            if position_count is not None:
                data["position_count"] = position_count
                ctx += f" | Positions: {position_count}"
            lines.append(ctx)
        await self._dispatch("risk_resumed", data, "\n".join(lines))

    async def rollback_alert(
        self, reason: str, version: str,
        *, portfolio_value: float | None = None,
    ) -> None:
        data: dict = {"reason": reason, "version": version}
        lines = [
            "\u26A0\uFE0F STRATEGY ROLLBACK",
            f"Reason: {reason}",
            f"Rolled back to: {version}",
        ]
        if portfolio_value is not None:
            data["portfolio_value"] = portfolio_value
            lines.append(f"Portfolio: ${portfolio_value:,.2f}")
        await self._dispatch("strategy_rollback", data, "\n".join(lines))

    # --- Scan Events ---

    async def scan_complete(
        self, symbol_count: int, signal_count: int,
        *, executed_count: int | None = None, rejected_count: int | None = None,
        portfolio_value: float | None = None,
    ) -> None:
        data: dict = {"symbol_count": symbol_count, "signal_count": signal_count}
        lines = [f"\U0001F50D Scan: {symbol_count} symbols, {signal_count} signals"]
        if executed_count is not None or rejected_count is not None:
            parts = []
            if executed_count is not None:
                data["executed_count"] = executed_count
                parts.append(f"Executed: {executed_count}")
            if rejected_count is not None:
                data["rejected_count"] = rejected_count
                parts.append(f"Rejected: {rejected_count}")
            lines.append(" | ".join(parts))
        if portfolio_value is not None:
            data["portfolio_value"] = portfolio_value
            lines.append(f"Portfolio: ${portfolio_value:,.2f}")
        await self._dispatch("scan_complete", data, "\n".join(lines))

    # --- Strategy Events ---

    async def strategy_deployed(self, version: str, tier: int, changes: str) -> None:
        tier_name = {1: "Tweak", 2: "Restructure", 3: "Overhaul"}.get(tier, "Unknown")
        await self._dispatch(
            "strategy_deployed",
            {"version": version, "tier": tier, "tier_name": tier_name, "changes": changes[:500]},
            f"\U0001F680 Strategy Deployed: {version}\n{changes[:200]}",
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
        # WS + activity only — suppress Telegram
        await self._dispatch(
            "orchestrator_cycle_started",
            {},
            None,
        )

    async def orchestrator_cycle_completed(
        self, decision_type: str,
        *, strategy_version: str | None = None,
        candidate_count: int | None = None, max_candidates: int | None = None,
    ) -> None:
        data: dict = {"decision_type": decision_type}
        lines = [
            "\U0001F504 Cycle Complete",
            f"Actions: {decision_type}",
        ]
        parts = []
        if strategy_version:
            data["strategy_version"] = strategy_version
            parts.append(f"Active: {strategy_version}")
        if candidate_count is not None and max_candidates is not None:
            data["candidate_count"] = candidate_count
            data["max_candidates"] = max_candidates
            parts.append(f"Candidates: {candidate_count}/{max_candidates}")
        if parts:
            lines.append(" | ".join(parts))
        await self._dispatch("orchestrator_cycle_completed", data, "\n".join(lines))

    async def orchestrator_observation(
        self,
        market_observations: str,
        reasoning: str,
        cross_reference_findings: str,
        *,
        doc_flag: bool = False,
        flag_reason: str = "",
    ) -> None:
        data: dict = {
            "market_observations": market_observations[:2000],
            "reasoning": reasoning[:2000],
            "cross_reference_findings": cross_reference_findings[:2000],
        }
        lines = ["\U0001F9E0 Orchestrator Observation"]
        if market_observations:
            lines.append(f"Market: {market_observations[:300]}")
        if cross_reference_findings:
            lines.append(f"Cross-ref: {cross_reference_findings[:300]}")
        if doc_flag:
            data["doc_flag"] = True
            data["flag_reason"] = flag_reason[:500]
            lines.append(f"\u26A0\uFE0F Doc flag: {flag_reason[:200]}")
        await self._dispatch("orchestrator_observation", data, "\n".join(lines))

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

    async def system_online(
        self, portfolio_value: float, positions: int,
        *, mode: str | None = None, strategy_version: str | None = None,
        cash: float | None = None, status: str | None = None,
    ) -> None:
        data: dict = {"portfolio_value": portfolio_value, "positions": positions}
        lines = ["\U0001F7E2 System Online"]
        if mode or strategy_version:
            parts = []
            if mode:
                data["mode"] = mode
                parts.append(f"Mode: {mode}")
            if strategy_version:
                data["strategy_version"] = strategy_version
                parts.append(f"Strategy: {strategy_version}")
            lines.append(" | ".join(parts))
        ctx = f"Portfolio: ${portfolio_value:,.2f}"
        if cash is not None:
            data["cash"] = cash
            ctx += f" | Cash: ${cash:,.2f}"
        lines.append(ctx)
        pos_str = f"Positions: {positions}"
        if status:
            data["status"] = status
            pos_str += f" | Status: {status}"
        lines.append(pos_str)
        await self._dispatch("system_online", data, "\n".join(lines))

    async def system_shutdown(
        self,
        *, portfolio_value: float | None = None, position_count: int | None = None,
    ) -> None:
        data: dict = {}
        lines = ["\U0001F534 System Shutting Down"]
        if portfolio_value is not None:
            data["portfolio_value"] = portfolio_value
            ctx = f"Portfolio: ${portfolio_value:,.2f}"
            if position_count is not None:
                data["position_count"] = position_count
                ctx += f" | Positions: {position_count}"
            lines.append(ctx)
        await self._dispatch("system_shutdown", data, "\n".join(lines))

    async def system_error(self, error: str) -> None:
        await self._dispatch(
            "system_error",
            {"message": error[:500]},
            f"\u274C System Error: {error[:500]}",
        )

    async def websocket_failed(self) -> None:
        await self._dispatch(
            "websocket_feed_lost",
            {},
            "\u26A0\uFE0F WebSocket Feed Lost\n"
            "Live price feed down \u2014 REST fallback active",
        )

    # --- Candidate Events ---

    async def candidate_created(self, slot: int, version: str, eval_days: int | None = None) -> None:
        eval_str = f"{eval_days}d" if eval_days else "indefinite"
        await self._dispatch(
            "candidate_created",
            {"slot": slot, "version": version, "eval_days": eval_days},
            f"\U0001F9EA Candidate Created \u2014 Slot {slot}\nVersion: {version}\nEvaluation: {eval_str}",
        )

    async def candidate_canceled(self, slot: int, *, reason: str = "") -> None:
        data: dict = {"slot": slot}
        lines = [f"\u274C Candidate Canceled \u2014 Slot {slot}"]
        if reason:
            data["reason"] = reason
            lines.append(f"Reason: {reason[:200]}")
        await self._dispatch("candidate_canceled", data, "\n".join(lines))

    async def candidate_promoted(self, slot: int, version: str, *, position_handling: str = "") -> None:
        data: dict = {"slot": slot, "version": version}
        lines = [f"\U0001F3C6 Candidate Promoted \u2014 Slot {slot} \u2192 {version}"]
        if position_handling:
            data["position_handling"] = position_handling
            handling_str = "kept" if position_handling == "keep" else "closed"
            lines.append(f"Fund positions: {handling_str}")
        await self._dispatch("candidate_promoted", data, "\n".join(lines))

    async def candidate_trade_executed(self, slot: int, trade: dict) -> None:
        action = trade.get("action", "?")
        symbol = trade.get("symbol", "?")
        qty = trade.get("qty", 0)
        price = trade.get("price", 0)
        fee = trade.get("fee", 0)
        intent = trade.get("intent", "DAY")
        tag = trade.get("tag", "")
        tag_str = f" [{tag}]" if tag else ""

        if action == "BUY":
            lines = [
                f"\U0001F4C8 [C{slot}] BUY {qty:.6f} {symbol}{tag_str}",
                f"Entry: ${price:,.2f} | Fee: ${fee:.2f}",
                f"Intent: {intent}",
            ]
        else:
            pnl = trade.get("pnl")
            pnl_pct = trade.get("pnl_pct", 0) * 100
            entry = trade.get("entry_price")
            lines = [f"\U0001F4C9 [C{slot}] {action} {qty:.6f} {symbol}{tag_str}"]
            exit_str = f"Exit: ${price:,.2f}"
            if entry:
                exit_str += f" | Entry: ${entry:,.2f}"
            lines.append(exit_str)
            if pnl is not None:
                lines.append(f"P&L: ${pnl:+.2f} ({pnl_pct:+.1f}%) | Fee: ${fee:.2f}")

        data = {**trade, "slot": slot}
        await self._dispatch("candidate_trade_executed", data, "\n".join(lines))

    async def reflection_completed(
        self, predictions_graded: int, new_predictions: int, summary: str,
        *, correct: int | None = None, incorrect: int | None = None, uncertain: int | None = None,
        key_learnings: list[str] | None = None,
    ) -> None:
        data: dict = {
            "predictions_graded": predictions_graded,
            "new_predictions": new_predictions,
            "summary": summary[:500],
        }
        lines = ["\U0001F52C Reflection Complete"]
        if correct is not None and incorrect is not None and uncertain is not None:
            data.update(correct=correct, incorrect=incorrect, uncertain=uncertain)
            lines.append(f"Graded: {predictions_graded} ({correct}\u2713 {incorrect}\u2717 {uncertain}?)")
        else:
            lines.append(f"Predictions graded: {predictions_graded}")
        lines.append(f"New predictions: {new_predictions}")
        if key_learnings:
            data["key_learnings"] = key_learnings[:10]
            lines.append("Key learnings:")
            for learning in key_learnings[:5]:
                lines.append(f"  \u2022 {learning[:200]}")
        lines.append("Strategy doc updated")
        await self._dispatch("reflection_completed", data, "\n".join(lines))

    async def candidate_stop_triggered(self, slot: int, trade: dict) -> None:
        symbol = trade.get("symbol", "?")
        reason = trade.get("close_reason", "stop_loss")
        price = trade.get("price", 0)
        tag = trade.get("tag", "")
        tag_str = f" [{tag}]" if tag else ""
        pnl = trade.get("pnl")
        pnl_pct = trade.get("pnl_pct", 0) * 100
        fee = trade.get("fee", 0)
        entry = trade.get("entry_price")

        if reason == "take_profit":
            header = f"\U0001F3AF [C{slot}] TAKE PROFIT \u2014 {symbol}{tag_str}"
        else:
            header = f"\U0001F6D1 [C{slot}] STOP LOSS \u2014 {symbol}{tag_str}"
        lines = [header]
        exit_str = f"Exit: ${price:,.2f}"
        if entry:
            exit_str += f" | Entry: ${entry:,.2f}"
        lines.append(exit_str)
        if pnl is not None:
            pnl_str = f"P&L: ${pnl:+.2f} ({pnl_pct:+.1f}%)"
            if fee:
                pnl_str += f" | Fee: ${fee:.2f}"
            lines.append(pnl_str)

        data = {**trade, "slot": slot}
        await self._dispatch("candidate_stop_triggered", data, "\n".join(lines))

    # --- Config Reload ---

    async def config_reloaded(
        self, changes: list[str], refused: list[str], errors: list[str],
    ) -> None:
        data = {"changes": changes, "refused": refused, "errors": errors}
        lines = ["\u2699\uFE0F Config Reloaded"]
        if changes:
            lines.append(f"Updated: {', '.join(changes)}")
        if refused:
            lines.append(f"Refused: {', '.join(refused)}")
        if errors:
            lines.append(f"Errors: {', '.join(errors)}")
        if not changes and not errors:
            lines.append("No changes detected")
        await self._dispatch("config_reloaded", data, "\n".join(lines))

    # --- Signal Drought ---

    async def signal_drought(
        self, hours: int, scan_count: int, strategy_version: str,
        last_signal_time: str,
    ) -> None:
        data = {
            "hours": hours,
            "scan_count": scan_count,
            "strategy_version": strategy_version,
            "last_signal_time": last_signal_time,
        }
        await self._dispatch(
            "signal_drought",
            data,
            f"\u23F3 Signal Drought \u2014 {hours}h\n"
            f"No signals generated in {hours} hours\n"
            f"Scans: {scan_count} | Strategy: {strategy_version}\n"
            f"Last signal: {last_signal_time}",
        )
