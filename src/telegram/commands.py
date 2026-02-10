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

# System prompt for the /ask Haiku assistant
ASK_SYSTEM_PROMPT = (
    "You are the investor relations assistant for an autonomous crypto trading fund. "
    "You explain system behavior, recent decisions, and current state in clear, concise terms. "
    "You are grounded in the data provided — do not speculate beyond what the data shows. "
    "If the data doesn't answer the question, say so honestly."
)


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
        """Check if user is authorized. Rejects all users if no IDs configured."""
        allowed = self._config.telegram.allowed_user_ids
        if not allowed:
            return False  # No configured users = locked down
        return update.effective_user and update.effective_user.id in allowed

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await update.message.reply_text(
            "Trading Brain v2 (IO-Container)\n"
            f"Mode: {self._config.mode}\n"
            f"Symbols: {', '.join(self._config.symbols)}\n\n"
            "Commands:\n"
            "/status - System health\n"
            "/health - Fund performance\n"
            "/outlook - Orchestrator's market view\n"
            "/positions - Open positions\n"
            "/trades - Recent trades\n"
            "/risk - Risk utilization\n"
            "/daily_performance - Daily performance\n"
            "/strategy - Active strategy info\n"
            "/tokens - Token usage\n"
            "/ask <question> - Ask about the system\n"
            "/thoughts - Browse orchestrator AI reasoning\n"
            "/thought <cycle> <step> - Full AI response\n"
            "/pause - Pause trading\n"
            "/resume - Resume trading\n"
            "/kill - Emergency stop"
        )

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """System health only — mode, status, last scan, uptime."""
        if not self._authorized(update):
            return

        lines = [f"Mode: {self._config.mode}"]

        # Status line
        if self._risk and self._risk.is_halted:
            lines.append(f"Status: HALTED — {self._risk.halt_reason}")
        elif self._paused:
            lines.append("Status: PAUSED")
        else:
            lines.append("Status: ACTIVE")

        # Last scan time
        last_scan = self._scan_state.get("last_scan")
        if last_scan:
            lines.append(f"Last Scan: {last_scan}")

        # Uptime from first scan
        first_scan = await self._db.fetchone(
            "SELECT MIN(created_at) as first_scan FROM scan_results"
        )
        if first_scan and first_scan["first_scan"]:
            try:
                started = datetime.fromisoformat(first_scan["first_scan"])
                delta = datetime.utcnow() - started
                days = delta.days
                hours = delta.seconds // 3600
                mins = (delta.seconds % 3600) // 60
                if days > 0:
                    lines.append(f"Uptime: {days}d {hours}h {mins}m")
                elif hours > 0:
                    lines.append(f"Uptime: {hours}h {mins}m")
                else:
                    lines.append(f"Uptime: {mins}m")
            except (ValueError, TypeError):
                lines.append("Uptime: unknown")
        else:
            lines.append("Uptime: No scans yet")

        await update.message.reply_text("\n".join(lines))

    async def cmd_health(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Long-term fund health — portfolio, returns, trade stats."""
        if not self._authorized(update):
            return

        from src.shell.truth import compute_truth_benchmarks

        truth = await compute_truth_benchmarks(self._db)

        lines = ["--- Fund Health ---"]

        # Live portfolio state
        if self._portfolio:
            value = await self._portfolio.total_value()
            lines.append(f"Portfolio: ${value:.2f}")
            lines.append(f"Cash: ${self._portfolio.cash:.2f}")
            lines.append(f"Positions: {self._portfolio.position_count}")

            # Total return
            initial = self._config.paper_balance_usd
            ret = value - initial
            ret_pct = (ret / initial * 100) if initial > 0 else 0
            lines.append(f"\nTotal Return: ${ret:+.2f} ({ret_pct:+.1f}%)")
        else:
            lines.append("Portfolio: unavailable")

        # Drawdown
        if self._risk and self._risk.peak_portfolio is not None and self._risk.peak_portfolio > 0:
            if self._portfolio:
                current_dd = (self._risk.peak_portfolio - value) / self._risk.peak_portfolio * 100
                lines.append(f"Current Drawdown: {current_dd:.1f}% from peak")
        lines.append(f"Max Drawdown: {truth['max_drawdown_pct'] * 100:.1f}%")

        # Trade stats
        tc = truth["trade_count"]
        wc = truth["win_count"]
        lc = truth["loss_count"]
        lines.append(f"\nTrades: {tc} ({wc}W/{lc}L)")
        lines.append(f"Win Rate: {truth['win_rate'] * 100:.0f}%")
        lines.append(f"Expectancy: ${truth['expectancy']:.2f}")
        lines.append(f"Total Fees: ${truth['total_fees']:.2f}")

        # Strategy + orchestrator
        ver = truth.get("current_strategy_version") or "none"
        lines.append(f"\nStrategy: {ver}")

        last_cycle = await self._db.fetchone(
            "SELECT date FROM orchestrator_observations ORDER BY date DESC LIMIT 1"
        )
        if last_cycle:
            lines.append(f"Last Orchestrator Cycle: {last_cycle['date']}")
        else:
            lines.append("Last Orchestrator Cycle: none")

        last_trade = await self._db.fetchone(
            "SELECT closed_at FROM trades WHERE closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT 1"
        )
        if last_trade and last_trade["closed_at"]:
            try:
                trade_dt = datetime.fromisoformat(last_trade["closed_at"])
                days_since = (datetime.utcnow() - trade_dt).days
                lines.append(f"Days Since Last Trade: {days_since}")
            except (ValueError, TypeError):
                lines.append("Days Since Last Trade: unknown")
        else:
            lines.append("Days Since Last Trade: N/A")

        await self._send_long(update, "\n".join(lines))

    async def cmd_outlook(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Orchestrator's most recent observations from nightly cycle."""
        if not self._authorized(update):
            return

        obs = await self._db.fetchone(
            "SELECT * FROM orchestrator_observations ORDER BY date DESC LIMIT 1"
        )
        if not obs:
            await update.message.reply_text("No orchestrator cycles have run yet.")
            return

        lines = [
            "--- Orchestrator Outlook ---",
            f"From nightly cycle on {obs['date']}",
        ]

        if obs["market_summary"]:
            lines.append(f"\nMarket Summary:\n{obs['market_summary']}")

        if obs["strategy_assessment"]:
            lines.append(f"\nStrategy Assessment:\n{obs['strategy_assessment']}")

        if obs["notable_findings"]:
            lines.append(f"\nNotable Findings:\n{obs['notable_findings']}")

        await self._send_long(update, "\n".join(lines))

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
            pnl = t.get("pnl") or 0
            pnl_pct = (t.get("pnl_pct") or 0) * 100
            fees = t.get("fees") or 0
            sign = "+" if pnl > 0 else ""
            lines.append(
                f"{t['symbol']} {t['side']} ${sign}{pnl:.2f} ({pnl_pct:+.1f}%) "
                f"fee=${fees:.3f}"
            )

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

    async def cmd_daily_performance(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        """Context-aware question to Haiku — assembles system state as context."""
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
            # Assemble context
            ctx_parts = []

            # Portfolio state
            if self._portfolio:
                value = await self._portfolio.total_value()
                ctx_parts.append(
                    f"Portfolio: ${value:.2f}, Cash: ${self._portfolio.cash:.2f}, "
                    f"Positions: {self._portfolio.position_count}"
                )

            # Risk state
            if self._risk:
                ctx_parts.append(
                    f"Risk: Daily P&L ${self._risk.daily_pnl:+.2f}, "
                    f"Halted: {self._risk.is_halted}, "
                    f"Consecutive Losses: {self._risk.consecutive_losses}"
                )

            # Recent trades
            trades = await self._db.fetchall(
                "SELECT symbol, side, pnl, pnl_pct, fees, closed_at FROM trades "
                "WHERE closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT 5"
            )
            if trades:
                trade_lines = []
                for t in trades:
                    pnl = t.get("pnl") or 0
                    trade_lines.append(
                        f"  {t['symbol']} {t['side']} P&L=${pnl:+.2f} ({t['closed_at'][:10]})"
                    )
                ctx_parts.append("Recent trades:\n" + "\n".join(trade_lines))

            # Latest orchestrator observations
            obs = await self._db.fetchone(
                "SELECT market_summary, strategy_assessment FROM orchestrator_observations "
                "ORDER BY date DESC LIMIT 1"
            )
            if obs:
                if obs["market_summary"]:
                    ctx_parts.append(f"Market summary: {obs['market_summary']}")
                if obs["strategy_assessment"]:
                    ctx_parts.append(f"Strategy assessment: {obs['strategy_assessment']}")

            # Strategy version
            ver = await self._db.fetchone(
                "SELECT version FROM strategy_versions ORDER BY deployed_at DESC LIMIT 1"
            )
            if ver:
                ctx_parts.append(f"Strategy version: {ver['version']}")

            context_str = "\n\n".join(ctx_parts)
            prompt = f"Current system state:\n{context_str}\n\nUser question: {question}"

            answer = await self._ai.ask_haiku(
                prompt, system=ASK_SYSTEM_PROMPT, max_tokens=1000, purpose="user_ask"
            )
            await self._send_long(update, answer)
        except Exception as e:
            log.error("telegram.ask_failed", error=str(e))
            await update.message.reply_text("Sorry, an error occurred processing your question.")

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
