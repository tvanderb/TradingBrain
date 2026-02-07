"""Telegram command handlers."""

from __future__ import annotations

from datetime import date, timedelta

from telegram import Update
from telegram.ext import ContextTypes

from src.brains.analyst import AnalystBrain
from src.brains.executor import ExecutorBrain
from src.brains.executive import ExecutiveBrain
from src.core.config import Config
from src.core.logging import get_logger
from src.core.tokens import TokenTracker
from src.storage.database import Database
from src.storage import queries

log = get_logger("commands")


class BotCommands:
    """All Telegram command handlers."""

    def __init__(
        self,
        config: Config,
        db: Database,
        executor: ExecutorBrain,
        analyst: AnalystBrain,
        executive: ExecutiveBrain,
        token_tracker: TokenTracker,
        scan_state: dict | None = None,
    ) -> None:
        self._config = config
        self._db = db
        self._executor = executor
        self._analyst = analyst
        self._executive = executive
        self._tokens = token_tracker
        self._scan_state = scan_state or {}

    def _authorized(self, update: Update) -> bool:
        """Check if user is authorized."""
        allowed = self._config.telegram.allowed_user_ids
        if not allowed:
            return True  # Empty list = allow all
        user_id = update.effective_user.id if update.effective_user else 0
        return user_id in allowed

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await update.message.reply_text(
            "Trading Brain Online\n\n"
            f"Mode: {self._config.mode}\n"
            f"Symbols: {', '.join(self._config.markets.crypto_symbols)}\n\n"
            "Commands:\n"
            "/status - System overview\n"
            "/positions - Open positions\n"
            "/trades - Recent trades\n"
            "/performance - Performance metrics\n"
            "/signals - Recent analyst signals\n"
            "/report - On-demand market analysis\n"
            "/ask <question> - Ask the brain\n"
            "/risk - Risk status\n"
            "/evolution - Latest evolution\n"
            "/tokens - Token usage\n"
            "/pause | /resume - Control trading\n"
            "/kill - Emergency stop"
        )

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        status = self._executor.get_status()
        msg = (
            f"Mode: {status['mode']}\n"
            f"Active: {'Yes' if status['active'] else 'No'}"
            f"{' (PAUSED)' if status['paused'] else ''}\n\n"
            f"Portfolio: ${status['portfolio_value']:.2f}\n"
            f"Cash: ${status['cash_balance']:.2f}\n"
            f"Positions: {status['open_positions']}\n"
            f"Unrealized P&L: ${status['unrealized_pnl']:.2f}\n\n"
            f"Today's P&L: ${status['daily_pnl']:.2f}\n"
            f"Today's Trades: {status['daily_trades']}"
        )
        await update.message.reply_text(msg)

    async def cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        positions = self._executor.position_tracker.open_positions
        if not positions:
            await update.message.reply_text("No open positions.")
            return
        lines = []
        for p in positions:
            pnl = p.unrealized_pnl or 0
            pnl_icon = "+" if pnl >= 0 else ""
            lines.append(
                f"{p.symbol} ({p.side})\n"
                f"  Qty: {p.qty:.6f} @ ${p.avg_entry:.2f}\n"
                f"  Now: ${p.current_price:.2f} | P&L: {pnl_icon}${pnl:.2f}\n"
                f"  SL: ${p.stop_loss:.2f} | TP: ${p.take_profit:.2f}"
            )
        await update.message.reply_text("\n\n".join(lines))

    async def cmd_trades(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        trades = await queries.get_recent_trades(self._db, limit=10)
        if not trades:
            await update.message.reply_text("No trades yet.")
            return
        lines = []
        for t in trades:
            pnl = f"P&L: ${t.pnl:.2f}" if t.pnl is not None else ""
            lines.append(
                f"{t.side.upper()} {t.symbol} | {t.qty:.6f} @ ${t.filled_price or t.price:.2f} "
                f"| {t.status} {pnl}"
            )
        await update.message.reply_text("\n".join(lines))

    async def cmd_performance(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        today = date.today()
        week_ago = (today - timedelta(days=7)).isoformat()
        perf = await queries.get_performance_range(self._db, week_ago, today.isoformat())
        if not perf:
            await update.message.reply_text("No performance data yet.")
            return
        total_pnl = sum(p.net_pnl for p in perf)
        total_trades = sum(p.total_trades for p in perf)
        total_wins = sum(p.wins for p in perf)
        total_token_cost = sum(p.token_cost_usd for p in perf)
        win_rate = (total_wins / total_trades * 100) if total_trades > 0 else 0
        msg = (
            f"7-Day Performance:\n\n"
            f"Net P&L: ${total_pnl:.2f}\n"
            f"Trades: {total_trades}\n"
            f"Win Rate: {win_rate:.1f}%\n"
            f"Token Cost: ${total_token_cost:.2f}\n"
            f"Net (after tokens): ${total_pnl - total_token_cost:.2f}"
        )
        await update.message.reply_text(msg)

    async def cmd_ask(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        question = " ".join(context.args) if context.args else ""
        if not question:
            await update.message.reply_text("Usage: /ask <your question>")
            return
        status = self._executor.get_status()
        answer = await self._analyst.ask_question(question, context=status)
        await update.message.reply_text(answer)

    async def cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        self._executor.pause()
        self._analyst.pause()
        await update.message.reply_text("Trading paused. Use /resume to restart.")

    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        self._executor.resume()
        self._analyst.resume()
        await update.message.reply_text("Trading resumed.")

    async def cmd_risk(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        rm = self._executor.risk_manager
        limits = self._config.risk
        msg = (
            f"Risk Status:\n\n"
            f"Kill Switch: {'ON' if limits.emergency.kill_switch else 'OFF'}\n"
            f"Daily P&L: ${rm.daily_pnl:.2f} (limit: -${limits.daily.max_daily_loss_pct * 100:.0f}%)\n"
            f"Daily Trades: {rm.daily_trades}/{limits.daily.max_daily_trades}\n"
            f"Max Position: {limits.position.max_position_pct * 100:.0f}%\n"
            f"Max Per Trade: {limits.per_trade.max_trade_pct * 100:.0f}%\n"
            f"Stop Loss: {limits.per_trade.default_stop_loss_pct * 100:.0f}%\n"
            f"Take Profit: {limits.per_trade.default_take_profit_pct * 100:.0f}%\n"
            f"Max Drawdown: {limits.emergency.max_drawdown_pct * 100:.0f}%"
        )
        await update.message.reply_text(msg)

    async def cmd_evolution(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        summary = await self._executive.get_latest_evolution_summary()
        await update.message.reply_text(summary)

    async def cmd_tokens(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        costs = await self._tokens.get_daily_cost_summary()
        if not costs:
            await update.message.reply_text("No token usage today.")
            return
        total = sum(costs.values())
        lines = ["Today's Token Costs:\n"]
        for brain, cost in costs.items():
            lines.append(f"  {brain}: ${cost:.4f}")
        lines.append(f"\nTotal: ${total:.4f}")
        await update.message.reply_text("\n".join(lines))

    async def cmd_signals(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show recent analyst signals and AI decisions."""
        if not self._authorized(update):
            return
        signals = await queries.get_recent_signals(self._db, limit=10)
        if not signals:
            await update.message.reply_text("No signals generated yet.")
            return
        lines = ["Recent Signals:\n"]
        for s in signals:
            acted = "ACTED" if s.acted_on else "skipped"
            lines.append(
                f"{s.direction.upper()} {s.symbol} | {s.signal_type}\n"
                f"  Strength: {s.strength:.2f} | {acted}\n"
                f"  {s.reasoning or 'No reasoning'}"
            )
            if s.ai_response:
                # Show first 80 chars of AI response
                snippet = s.ai_response[:80].replace("\n", " ")
                lines.append(f"  AI: {snippet}...")
            lines.append("")
        await update.message.reply_text("\n".join(lines)[:4000])  # Telegram 4096 char limit

    async def cmd_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show latest scan results — indicators, regime, and signals from the system's own calculations."""
        if not self._authorized(update):
            return

        if not self._scan_state:
            await update.message.reply_text("No scan data yet. Wait for the next 5-min scan cycle.")
            return

        last_scan = self._scan_state.get("last_scan_time", "unknown")
        lines = [f"Market Report (scan: {last_scan})\n"]

        for symbol in self._config.markets.crypto_symbols:
            data = self._scan_state.get(symbol)
            if not data:
                lines.append(f"{symbol}: No data\n")
                continue

            price = data.get("price", 0)
            regime = data.get("regime", "unknown")
            regime_desc = data.get("regime_desc", "")
            rsi = data.get("rsi")
            bb_pct = data.get("bb_pct")
            ema_fast = data.get("ema_fast")
            ema_slow = data.get("ema_slow")
            macd_hist = data.get("macd_hist")
            vol_ratio = data.get("vol_ratio")
            signal_dir = data.get("signal_direction")
            signal_str = data.get("signal_strength")
            signal_type = data.get("signal_type")

            ema_align = ""
            if ema_fast and ema_slow:
                ema_align = "bullish" if ema_fast > ema_slow else "bearish"

            lines.append(f"{symbol} — ${price:,.2f}")
            lines.append(f"  Regime: {regime} ({regime_desc})")
            if rsi is not None:
                rsi_label = " (oversold)" if rsi < 30 else " (overbought)" if rsi > 70 else ""
                lines.append(f"  RSI: {rsi:.1f}{rsi_label}")
            if bb_pct is not None:
                bb_label = " (below lower)" if bb_pct < 0 else " (above upper)" if bb_pct > 1 else ""
                lines.append(f"  BB%: {bb_pct:.2f}{bb_label}")
            if ema_align:
                lines.append(f"  EMA: {ema_align} (fast={ema_fast:.2f}, slow={ema_slow:.2f})")
            if macd_hist is not None:
                lines.append(f"  MACD hist: {macd_hist:+.4f}")
            if vol_ratio is not None:
                lines.append(f"  Volume: {vol_ratio:.1f}x avg")
            if signal_dir:
                lines.append(f"  Signal: {signal_dir.upper()} ({signal_type}, strength={signal_str:.2f})")
            else:
                lines.append("  Signal: none")
            lines.append("")

        await update.message.reply_text("\n".join(lines)[:4000])

    async def cmd_kill(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await update.message.reply_text("EMERGENCY: Closing all positions...")
        results = await self._executor.emergency_close_all()
        self._executor.pause()
        self._analyst.pause()
        msg = f"Closed {len(results)} positions. Trading halted.\nUse /resume to restart."
        await update.message.reply_text(msg)
