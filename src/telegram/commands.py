"""Telegram Command Handlers — user interface to the trading system.

Commands show existing system state and calculations.
Only /ask calls Claude on-demand.
"""

from __future__ import annotations

from datetime import datetime

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from src.shell.config import Config
from src.shell.database import Database

log = structlog.get_logger()


class BotCommands:
    """Handles all Telegram bot commands."""

    def __init__(
        self,
        config: Config,
        db: Database,
        scan_state: dict,
        portfolio_tracker=None,
        risk_manager=None,
        ai_client=None,
        reporter=None,
    ) -> None:
        self._config = config
        self._db = db
        self._scan_state = scan_state
        self._portfolio = portfolio_tracker
        self._risk = risk_manager
        self._ai = ai_client
        self._reporter = reporter
        self._paused = False

    @property
    def is_paused(self) -> bool:
        return self._paused

    def _authorized(self, update: Update) -> bool:
        """Check if user is authorized."""
        allowed = self._config.telegram.allowed_user_ids
        if not allowed:
            return True
        return update.effective_user and update.effective_user.id in allowed

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await update.message.reply_text(
            "Trading Brain v2 (IO-Container)\n"
            f"Mode: {self._config.mode}\n"
            f"Symbols: {', '.join(self._config.symbols)}\n\n"
            "Commands:\n"
            "/status - System status\n"
            "/positions - Open positions\n"
            "/trades - Recent trades\n"
            "/report - Latest scan data\n"
            "/risk - Risk utilization\n"
            "/performance - Performance metrics\n"
            "/strategy - Active strategy info\n"
            "/tokens - Token usage\n"
            "/ask <question> - Ask Claude\n"
            "/pause - Pause trading\n"
            "/resume - Resume trading\n"
            "/kill - Emergency stop"
        )

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return

        lines = [f"Mode: {self._config.mode}"]
        lines.append(f"Status: {'PAUSED' if self._paused else 'ACTIVE'}")

        if self._risk and self._risk.is_halted:
            lines.append(f"HALTED: {self._risk.halt_reason}")

        if self._portfolio:
            value = await self._portfolio.total_value()
            lines.append(f"Portfolio: ${value:.2f}")
            lines.append(f"Cash: ${self._portfolio.cash:.2f}")
            lines.append(f"Positions: {self._portfolio.position_count}")

        if self._risk:
            lines.append(f"Daily P&L: ${self._risk.daily_pnl:+.2f}")
            lines.append(f"Daily Trades: {self._risk.daily_trades}")

        # Last scan time
        last_scan = self._scan_state.get("last_scan")
        if last_scan:
            lines.append(f"Last Scan: {last_scan}")

        await update.message.reply_text("\n".join(lines))

    async def cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return

        rows = await self._db.fetchall("SELECT * FROM positions")
        if not rows:
            await update.message.reply_text("No open positions.")
            return

        lines = ["Open Positions:"]
        for p in rows:
            entry = p["avg_entry"]
            current = p.get("current_price", entry)
            qty = p["qty"]
            pnl = (current - entry) * qty
            pnl_pct = ((current - entry) / entry * 100) if entry > 0 else 0

            lines.append(
                f"\n{p['symbol']} ({p.get('intent', 'DAY')})\n"
                f"  Qty: {qty:.6f} @ ${entry:.2f}\n"
                f"  Now: ${current:.2f} ({pnl_pct:+.1f}%)\n"
                f"  P&L: ${pnl:+.2f}\n"
                f"  SL: ${p.get('stop_loss', 0):.2f} | TP: ${p.get('take_profit', 0):.2f}"
            )

        await update.message.reply_text("\n".join(lines))

    async def cmd_trades(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return

        rows = await self._db.fetchall(
            "SELECT * FROM trades WHERE closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT 10"
        )
        if not rows:
            await update.message.reply_text("No completed trades yet.")
            return

        lines = ["Recent Trades:"]
        for t in rows:
            pnl = t.get("pnl", 0)
            pnl_pct = t.get("pnl_pct", 0) * 100
            emoji = "+" if pnl and pnl > 0 else ""
            lines.append(
                f"{t['symbol']} {t['side']} ${pnl:{emoji}.2f} ({pnl_pct:+.1f}%) "
                f"fee=${t.get('fees', 0):.3f}"
            )

        await update.message.reply_text("\n".join(lines))

    async def cmd_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show latest scan results — indicators and signals from system calculations."""
        if not self._authorized(update):
            return

        if not self._scan_state or not self._scan_state.get("symbols"):
            await update.message.reply_text("No scan data yet. Waiting for first scan cycle.")
            return

        lines = ["--- Market Report ---"]
        for symbol, data in self._scan_state.get("symbols", {}).items():
            price = data.get("price", 0)
            regime = data.get("regime", "unknown")
            rsi = data.get("rsi", 0)
            ema_f = data.get("ema_fast", 0)
            ema_s = data.get("ema_slow", 0)
            vol_ratio = data.get("vol_ratio", 0)

            trend = "BULLISH" if ema_f > ema_s else "BEARISH" if ema_f < ema_s else "NEUTRAL"

            lines.append(
                f"\n{symbol}: ${price:,.2f}\n"
                f"  Regime: {regime} | Trend: {trend}\n"
                f"  RSI: {rsi:.1f} | EMA 9/21: {ema_f:.2f}/{ema_s:.2f}\n"
                f"  Vol: {vol_ratio:.1f}x avg"
            )

            signal = data.get("signal")
            if signal:
                lines.append(f"  Signal: {signal['action']} ({signal['confidence']:.0%})")
                lines.append(f"  Reason: {signal['reasoning']}")

        last_scan = self._scan_state.get("last_scan", "unknown")
        lines.append(f"\nLast scan: {last_scan}")

        await update.message.reply_text("\n".join(lines))

    async def cmd_risk(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return

        r = self._config.risk
        lines = [
            "--- Risk Limits ---",
            f"Max per trade: {r.max_trade_pct:.0%}",
            f"Default per trade: {r.default_trade_pct:.0%}",
            f"Max positions: {r.max_positions}",
            f"Max daily loss: {r.max_daily_loss_pct:.0%}",
            f"Max drawdown: {r.max_drawdown_pct:.0%}",
            f"Kill switch: {'ON' if r.kill_switch else 'OFF'}",
        ]

        if self._risk:
            lines.append(f"\n--- Current ---")
            lines.append(f"Daily P&L: ${self._risk.daily_pnl:+.2f}")
            lines.append(f"Daily Trades: {self._risk.daily_trades}/{r.max_daily_trades}")
            lines.append(f"Consecutive Losses: {self._risk.consecutive_losses}")
            lines.append(f"Halted: {'YES - ' + self._risk.halt_reason if self._risk.is_halted else 'NO'}")

        await update.message.reply_text("\n".join(lines))

    async def cmd_performance(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return

        if self._reporter:
            summary = await self._reporter.daily_summary()
            await update.message.reply_text(summary)
        else:
            await update.message.reply_text("Reporter not available.")

    async def cmd_strategy(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return

        from src.strategy.loader import get_strategy_path, get_code_hash

        path = get_strategy_path()
        if path.exists():
            code_hash = get_code_hash(path)
            # Read first 5 lines for description
            lines_all = path.read_text().split("\n")
            desc = "\n".join(lines_all[:6])

            version = await self._db.fetchone(
                "SELECT version, deployed_at FROM strategy_versions ORDER BY created_at DESC LIMIT 1"
            )
            ver_str = version["version"] if version else "v001 (initial)"

            # Paper test status
            test = await self._db.fetchone(
                "SELECT * FROM paper_tests WHERE status = 'running' ORDER BY started_at DESC LIMIT 1"
            )

            lines = [
                f"Strategy: {ver_str}",
                f"Hash: {code_hash}",
                f"\n{desc}",
            ]

            if test:
                lines.append(f"\nPaper Test: tier {test['risk_tier']}, ends {test['ends_at'][:10]}")

            await update.message.reply_text("\n".join(lines))
        else:
            await update.message.reply_text("No active strategy file found.")

    async def cmd_tokens(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return

        if self._ai:
            usage = await self._ai.get_daily_usage()
            lines = [
                "--- Token Usage ---",
                f"Budget: {usage['used']:,} / {usage['daily_limit']:,}",
                f"Cost today: ${usage['total_cost']:.4f}",
            ]
            for model, data in usage.get("models", {}).items():
                short = model.split("-")[1] if "-" in model else model
                lines.append(f"  {short}: {data['calls']} calls, ${data['cost']:.4f}")
            await update.message.reply_text("\n".join(lines))
        else:
            await update.message.reply_text("AI client not available.")

    async def cmd_ask(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """On-demand question to Claude."""
        if not self._authorized(update):
            return

        question = " ".join(context.args) if context.args else ""
        if not question:
            await update.message.reply_text("Usage: /ask <your question>")
            return

        if not self._ai:
            await update.message.reply_text("AI client not available.")
            return

        await update.message.reply_text("Thinking...")
        try:
            answer = await self._ai.ask_sonnet(
                question, max_tokens=500, purpose="user_ask"
            )
            await update.message.reply_text(answer[:4000])
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        self._paused = True
        await update.message.reply_text("Trading PAUSED. Scan loop will skip signal execution.")

    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        self._paused = False
        if self._risk and self._risk.is_halted:
            self._risk.unhalt()
            await update.message.reply_text("Trading RESUMED. Risk halt cleared.")
        else:
            await update.message.reply_text("Trading RESUMED.")

    async def cmd_kill(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Emergency stop — close all positions."""
        if not self._authorized(update):
            return

        self._paused = True
        await update.message.reply_text("EMERGENCY STOP initiated. Closing all positions...")

        # This will be handled by main.py's emergency handler
        self._scan_state["kill_requested"] = True
