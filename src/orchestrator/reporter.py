"""Reporter — generates daily and weekly performance reports.

Creates structured reports from performance data for:
1. Telegram notifications
2. Strategy document updates
3. Orchestrator decision context
"""

from __future__ import annotations

from datetime import datetime, timedelta

import structlog

from src.shell.database import Database

log = structlog.get_logger()


class Reporter:
    """Generates trading performance reports from stored data."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def daily_summary(self) -> str:
        """Generate a daily P&L summary for Telegram."""
        perf = await self._db.fetchone(
            "SELECT * FROM daily_performance WHERE date = date('now')"
        )

        positions = await self._db.fetchall("SELECT * FROM positions")
        trades_today = await self._db.fetchall(
            "SELECT * FROM trades WHERE closed_at >= date('now')"
        )

        if not perf:
            return "No performance data for today."

        value = perf.get("portfolio_value", 0)
        pnl = perf.get("net_pnl", 0)
        total_trades = perf.get("total_trades", 0)
        wins = perf.get("wins", 0)
        losses = perf.get("losses", 0)
        wr = perf.get("win_rate", 0)
        fees = perf.get("fees_total", 0)

        lines = [
            "--- Daily Summary ---",
            f"Portfolio: ${value:.2f}",
            f"P&L: ${pnl:+.2f}",
            f"Trades: {total_trades} ({wins}W/{losses}L)",
            f"Win Rate: {wr:.0%}" if total_trades > 0 else "Win Rate: N/A",
            f"Fees: ${fees:.2f}",
            f"Open Positions: {len(positions)}",
        ]

        # Individual trades
        if trades_today:
            lines.append("")
            lines.append("Trades:")
            for t in trades_today:
                pnl_str = f"${t['pnl']:+.2f}" if t.get("pnl") is not None else "open"
                lines.append(f"  {t['symbol']} {t['side']} {pnl_str}")

        return "\n".join(lines)

    async def weekly_report(self) -> str:
        """Generate a weekly performance report."""
        week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

        perfs = await self._db.fetchall(
            "SELECT * FROM daily_performance WHERE date >= ? ORDER BY date",
            (week_ago,),
        )

        trades = await self._db.fetchall(
            "SELECT * FROM trades WHERE closed_at >= ? ORDER BY closed_at ASC",
            (week_ago,),
        )

        if not perfs:
            return "No performance data for this week."

        total_pnl = sum(p.get("net_pnl", 0) for p in perfs)
        total_trades = sum(p.get("total_trades", 0) for p in perfs)
        total_wins = sum(p.get("wins", 0) for p in perfs)
        total_fees = sum(p.get("fees_total", 0) for p in perfs)
        latest_value = perfs[-1].get("portfolio_value", 0) if perfs else 0

        wr = total_wins / total_trades if total_trades > 0 else 0

        # Win/loss streaks
        if trades:
            pnls = [t.get("pnl", 0) for t in trades if t.get("pnl") is not None]
            max_win_streak = max_loss_streak = 0
            current_streak = 0
            for p in pnls:
                if p > 0:
                    if current_streak > 0:
                        current_streak += 1
                    else:
                        current_streak = 1
                    max_win_streak = max(max_win_streak, current_streak)
                else:
                    if current_streak < 0:
                        current_streak -= 1
                    else:
                        current_streak = -1
                    max_loss_streak = max(max_loss_streak, abs(current_streak))
        else:
            max_win_streak = max_loss_streak = 0

        lines = [
            "=== Weekly Report ===",
            f"Portfolio: ${latest_value:.2f}",
            f"Week P&L: ${total_pnl:+.2f}",
            f"Trades: {total_trades} ({total_wins}W/{total_trades - total_wins}L)",
            f"Win Rate: {wr:.0%}" if total_trades > 0 else "Win Rate: N/A",
            f"Fees: ${total_fees:.2f}",
            f"Best Win Streak: {max_win_streak}",
            f"Worst Loss Streak: {max_loss_streak}",
        ]

        # Daily breakdown
        if perfs:
            lines.append("")
            lines.append("Daily Breakdown:")
            for p in perfs:
                day_pnl = p.get("net_pnl", 0)
                day_trades = p.get("total_trades", 0)
                lines.append(f"  {p['date']}: ${day_pnl:+.2f} ({day_trades} trades)")

        return "\n".join(lines)

    async def strategy_performance(self, version: str | None = None, days: int = 30) -> dict:
        """Get performance metrics for a strategy version."""
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

        if version:
            trades = await self._db.fetchall(
                "SELECT * FROM trades WHERE strategy_version = ? AND closed_at >= ?",
                (version, cutoff),
            )
        else:
            trades = await self._db.fetchall(
                "SELECT * FROM trades WHERE closed_at >= ?",
                (cutoff,),
            )

        if not trades:
            return {"trades": 0, "win_rate": 0, "expectancy": 0, "net_pnl": 0}

        pnls = [t.get("pnl", 0) for t in trades if t.get("pnl") is not None]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]

        total = len(pnls)
        win_rate = len(wins) / total if total > 0 else 0
        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = abs(sum(losses) / len(losses)) if losses else 0
        expectancy = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)

        return {
            "trades": total,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": win_rate,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "expectancy": expectancy,
            "net_pnl": sum(pnls),
            "total_fees": sum(t.get("fees", 0) for t in trades),
            "profit_factor": sum(wins) / sum(abs(l) for l in losses) if losses else None,
        }
