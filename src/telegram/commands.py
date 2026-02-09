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
        notifier=None,
    ) -> None:
        self._config = config
        self._db = db
        self._scan_state = scan_state
        self._portfolio = portfolio_tracker
        self._risk = risk_manager
        self._ai = ai_client
        self._reporter = reporter
        self._notifier = notifier
        self._paused = False

    @property
    def is_paused(self) -> bool:
        return self._paused

    async def _send_long(self, update: Update, text: str, max_len: int = 4000) -> None:
        """Send a message, chunking if it exceeds Telegram's limit."""
        if len(text) <= max_len:
            await update.message.reply_text(text)
            return
        chunks = [text[i:i + max_len] for i in range(0, len(text), max_len)]
        for i, chunk in enumerate(chunks):
            prefix = "" if i == 0 else f"(part {i+1}/{len(chunks)})\n"
            await update.message.reply_text(prefix + chunk)

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
            "/thoughts - Browse orchestrator AI reasoning\n"
            "/thought <cycle> <step> - Full AI response\n"
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

        await self._send_long(update, "\n".join(lines))

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

        await self._send_long(update, "\n".join(lines))

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

    async def cmd_thoughts(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Browse orchestrator thought spool.

        /thoughts       — show latest cycle summary
        /thoughts list  — show recent cycles
        /thoughts <id>  — show steps for a specific cycle
        """
        if not self._authorized(update):
            return

        args = context.args if context.args else []

        if args and args[0] == "list":
            # Show recent cycles
            rows = await self._db.fetchall(
                """SELECT cycle_id, COUNT(*) as steps, MIN(created_at) as started
                   FROM orchestrator_thoughts
                   GROUP BY cycle_id ORDER BY started DESC LIMIT 10"""
            )
            if not rows:
                await update.message.reply_text("No orchestrator cycles recorded yet.")
                return
            lines = ["Recent Orchestrator Cycles:"]
            for r in rows:
                lines.append(f"\n{r['cycle_id']} — {r['steps']} steps ({r['started'][:16]})")
            await update.message.reply_text("\n".join(lines))

        elif args:
            # Show steps for a specific cycle
            cycle_id = args[0]
            rows = await self._db.fetchall(
                """SELECT step, model, LENGTH(full_response) as resp_len, created_at
                   FROM orchestrator_thoughts
                   WHERE cycle_id = ? ORDER BY created_at""",
                (cycle_id,),
            )
            if not rows:
                await update.message.reply_text(f"No thoughts found for cycle '{cycle_id}'.")
                return
            lines = [f"Cycle {cycle_id}:"]
            for r in rows:
                lines.append(f"\n  {r['step']} ({r['model']}) — {r['resp_len']} chars @ {r['created_at'][:16]}")
            lines.append(f"\nUse /thought {cycle_id} <step> to view full response.")
            await update.message.reply_text("\n".join(lines))

        else:
            # Show latest cycle summary
            latest = await self._db.fetchone(
                """SELECT cycle_id FROM orchestrator_thoughts
                   ORDER BY created_at DESC LIMIT 1"""
            )
            if not latest:
                await update.message.reply_text("No orchestrator cycles recorded yet.")
                return
            cycle_id = latest["cycle_id"]
            rows = await self._db.fetchall(
                """SELECT step, model, LENGTH(full_response) as resp_len, created_at
                   FROM orchestrator_thoughts
                   WHERE cycle_id = ? ORDER BY created_at""",
                (cycle_id,),
            )
            lines = [f"Latest Cycle: {cycle_id}"]
            for r in rows:
                lines.append(f"\n  {r['step']} ({r['model']}) — {r['resp_len']} chars")
            lines.append(f"\nUse /thought {cycle_id} <step> to view full response.")
            lines.append("Use /thoughts list to see all cycles.")
            await update.message.reply_text("\n".join(lines))

    async def cmd_thought(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show full AI response for a specific cycle step.

        /thought <cycle_id> <step>
        Chunks long responses for Telegram's 4096 char limit.
        """
        if not self._authorized(update):
            return

        args = context.args if context.args else []
        if len(args) < 2:
            await update.message.reply_text("Usage: /thought <cycle_id> <step>")
            return

        cycle_id = args[0]
        step = args[1]

        row = await self._db.fetchone(
            """SELECT full_response, model, input_summary, parsed_result, created_at
               FROM orchestrator_thoughts
               WHERE cycle_id = ? AND step = ?""",
            (cycle_id, step),
        )
        if not row:
            await update.message.reply_text(f"No thought found for cycle '{cycle_id}', step '{step}'.")
            return

        header = f"Cycle: {cycle_id}\nStep: {step} ({row['model']})\nTime: {row['created_at']}\n"
        if row["input_summary"]:
            header += f"Input: {row['input_summary'][:200]}...\n"
        header += "\n--- Response ---\n"

        text = row["full_response"]
        max_chunk = 4096 - len(header) - 50  # margin for chunk label

        if len(text) <= max_chunk:
            await update.message.reply_text(header + text)
        else:
            # Split into chunks
            chunks = [text[i:i + max_chunk] for i in range(0, len(text), max_chunk)]
            for i, chunk in enumerate(chunks):
                prefix = header if i == 0 else f"(part {i+1}/{len(chunks)})\n"
                await update.message.reply_text(prefix + chunk)

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
            if self._notifier:
                await self._notifier.risk_resumed()
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
