"""SQL query functions for the trading system.

Provides typed query functions that return model objects.
"""

from __future__ import annotations

from src.storage.database import Database
from src.storage.models import (
    DailyPerformance,
    FeeSchedule,
    Position,
    Signal,
    Trade,
)


# -- Trades --

async def insert_trade(db: Database, trade: Trade) -> int:
    return await db.execute(
        """INSERT INTO trades (symbol, side, qty, price, order_type, status, signal_id, exchange_order_id, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (trade.symbol, trade.side, trade.qty, trade.price, trade.order_type,
         trade.status, trade.signal_id, trade.exchange_order_id, trade.notes),
    )


async def update_trade_fill(
    db: Database, trade_id: int, filled_price: float, exchange_order_id: str, commission: float
) -> None:
    await db.execute(
        """UPDATE trades SET status = 'filled', filled_price = ?, exchange_order_id = ?,
           commission = ?, filled_at = datetime('now') WHERE id = ?""",
        (filled_price, exchange_order_id, commission, trade_id),
    )


async def update_trade_pnl(db: Database, trade_id: int, pnl: float) -> None:
    await db.execute("UPDATE trades SET pnl = ? WHERE id = ?", (pnl, trade_id))


async def get_recent_trades(db: Database, limit: int = 10) -> list[Trade]:
    rows = await db.fetchall(
        "SELECT * FROM trades ORDER BY created_at DESC LIMIT ?", (limit,)
    )
    return [Trade(**dict(row)) for row in rows]


async def get_trades_for_date(db: Database, date_str: str) -> list[Trade]:
    rows = await db.fetchall(
        "SELECT * FROM trades WHERE DATE(created_at) = ? ORDER BY created_at",
        (date_str,),
    )
    return [Trade(**dict(row)) for row in rows]


# -- Positions --

async def upsert_position(db: Database, pos: Position) -> int:
    return await db.execute(
        """INSERT INTO positions (symbol, side, qty, avg_entry, current_price, unrealized_pnl, stop_loss, take_profit)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(symbol) DO UPDATE SET
             qty = excluded.qty, avg_entry = excluded.avg_entry,
             current_price = excluded.current_price, unrealized_pnl = excluded.unrealized_pnl,
             stop_loss = excluded.stop_loss, take_profit = excluded.take_profit,
             updated_at = datetime('now')""",
        (pos.symbol, pos.side, pos.qty, pos.avg_entry, pos.current_price,
         pos.unrealized_pnl, pos.stop_loss, pos.take_profit),
    )


async def get_open_positions(db: Database) -> list[Position]:
    rows = await db.fetchall(
        "SELECT * FROM positions WHERE qty > 0 ORDER BY symbol"
    )
    return [Position(**dict(row)) for row in rows]


async def remove_position(db: Database, symbol: str) -> None:
    await db.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))


# -- Signals --

async def insert_signal(db: Database, signal: Signal) -> int:
    return await db.execute(
        """INSERT INTO signals (symbol, signal_type, strength, direction, reasoning, ai_response)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (signal.symbol, signal.signal_type, signal.strength,
         signal.direction, signal.reasoning, signal.ai_response),
    )


async def mark_signal_acted(db: Database, signal_id: int) -> None:
    await db.execute("UPDATE signals SET acted_on = 1 WHERE id = ?", (signal_id,))


async def get_recent_signals(db: Database, limit: int = 10) -> list[Signal]:
    rows = await db.fetchall(
        "SELECT * FROM signals ORDER BY created_at DESC LIMIT ?", (limit,)
    )
    return [Signal(**{k: v for k, v in dict(row).items() if k != "id" and k != "acted_on"}
                    | {"id": dict(row)["id"], "acted_on": bool(dict(row)["acted_on"])})
            for row in rows]


# -- Daily Performance --

async def upsert_daily_performance(db: Database, perf: DailyPerformance) -> None:
    await db.execute(
        """INSERT INTO daily_performance
           (date, total_trades, wins, losses, gross_pnl, net_pnl, max_drawdown_pct,
            win_rate, avg_win, avg_loss, token_usage, token_cost_usd, portfolio_value, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(date) DO UPDATE SET
             total_trades=excluded.total_trades, wins=excluded.wins, losses=excluded.losses,
             gross_pnl=excluded.gross_pnl, net_pnl=excluded.net_pnl,
             max_drawdown_pct=excluded.max_drawdown_pct, win_rate=excluded.win_rate,
             avg_win=excluded.avg_win, avg_loss=excluded.avg_loss,
             token_usage=excluded.token_usage, token_cost_usd=excluded.token_cost_usd,
             portfolio_value=excluded.portfolio_value, notes=excluded.notes""",
        (perf.date, perf.total_trades, perf.wins, perf.losses, perf.gross_pnl,
         perf.net_pnl, perf.max_drawdown_pct, perf.win_rate, perf.avg_win,
         perf.avg_loss, perf.token_usage, perf.token_cost_usd,
         perf.portfolio_value, perf.notes),
    )


async def get_performance_range(
    db: Database, start_date: str, end_date: str
) -> list[DailyPerformance]:
    rows = await db.fetchall(
        "SELECT * FROM daily_performance WHERE date BETWEEN ? AND ? ORDER BY date",
        (start_date, end_date),
    )
    return [DailyPerformance(**{k: v for k, v in dict(row).items() if k != "id"}) for row in rows]


# -- Fee Schedule --

async def insert_fee_check(db: Database, fee: FeeSchedule) -> None:
    await db.execute(
        """INSERT INTO fee_schedule (maker_fee_pct, taker_fee_pct, volume_30d_usd, fee_tier)
           VALUES (?, ?, ?, ?)""",
        (fee.maker_fee_pct, fee.taker_fee_pct, fee.volume_30d_usd, fee.fee_tier),
    )


async def get_latest_fees(db: Database) -> FeeSchedule | None:
    row = await db.fetchone(
        "SELECT * FROM fee_schedule ORDER BY checked_at DESC LIMIT 1"
    )
    if row is None:
        return None
    d = dict(row)
    return FeeSchedule(
        maker_fee_pct=d["maker_fee_pct"],
        taker_fee_pct=d["taker_fee_pct"],
        volume_30d_usd=d["volume_30d_usd"],
        fee_tier=d["fee_tier"],
        checked_at=d["checked_at"],
    )


# -- Evolution Log --

async def insert_evolution_log(
    db: Database, date_str: str, analysis: str, changes: str | None, patterns: str | None
) -> int:
    return await db.execute(
        """INSERT INTO evolution_log (date, analysis_json, changes_json, patterns_json)
           VALUES (?, ?, ?, ?)""",
        (date_str, analysis, changes, patterns),
    )


async def get_latest_evolution(db: Database) -> dict | None:
    row = await db.fetchone(
        "SELECT * FROM evolution_log ORDER BY created_at DESC LIMIT 1"
    )
    return dict(row) if row else None
