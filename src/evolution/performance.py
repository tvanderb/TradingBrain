"""Performance metric calculations.

Computes daily performance snapshots from trade data.
"""

from __future__ import annotations

from datetime import date

from src.core.logging import get_logger
from src.storage.database import Database
from src.storage.models import DailyPerformance
from src.storage import queries

log = get_logger("performance")


async def compute_daily_snapshot(
    db: Database,
    date_str: str | None = None,
    portfolio_value: float | None = None,
    token_usage: int = 0,
    token_cost: float = 0.0,
) -> DailyPerformance:
    """Compute and store daily performance metrics."""
    date_str = date_str or date.today().isoformat()
    trades = await queries.get_trades_for_date(db, date_str)

    filled = [t for t in trades if t.status == "filled"]
    wins = [t for t in filled if t.pnl is not None and t.pnl > 0]
    losses = [t for t in filled if t.pnl is not None and t.pnl < 0]

    gross_pnl = sum(t.pnl for t in filled if t.pnl is not None)
    total_commission = sum(t.commission for t in filled)
    net_pnl = gross_pnl - total_commission

    avg_win = sum(t.pnl for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum(t.pnl for t in losses) / len(losses) if losses else 0.0
    win_rate = len(wins) / len(filled) if filled else 0.0

    perf = DailyPerformance(
        date=date_str,
        total_trades=len(filled),
        wins=len(wins),
        losses=len(losses),
        gross_pnl=round(gross_pnl, 2),
        net_pnl=round(net_pnl, 2),
        win_rate=round(win_rate, 4),
        avg_win=round(avg_win, 2),
        avg_loss=round(avg_loss, 2),
        token_usage=token_usage,
        token_cost_usd=round(token_cost, 4),
        portfolio_value=portfolio_value,
    )

    await queries.upsert_daily_performance(db, perf)
    log.info(
        "daily_snapshot",
        date=date_str,
        trades=perf.total_trades,
        net_pnl=perf.net_pnl,
        win_rate=perf.win_rate,
    )
    return perf
