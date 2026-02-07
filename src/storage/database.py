"""Async SQLite database layer.

Provides a simple async wrapper around aiosqlite with
connection management and schema initialization.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aiosqlite

from src.core.logging import get_logger

log = get_logger("database")

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('buy', 'sell')),
    qty REAL NOT NULL,
    price REAL NOT NULL,
    filled_price REAL,
    status TEXT NOT NULL DEFAULT 'pending',
    order_type TEXT NOT NULL,
    signal_id INTEGER REFERENCES signals(id),
    exchange_order_id TEXT,
    pnl REAL,
    commission REAL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    filled_at TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL UNIQUE,
    side TEXT NOT NULL DEFAULT 'long',
    qty REAL NOT NULL,
    avg_entry REAL NOT NULL,
    current_price REAL,
    unrealized_pnl REAL,
    stop_loss REAL,
    take_profit REAL,
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    strength REAL NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('long', 'short', 'close')),
    reasoning TEXT,
    ai_response TEXT,
    acted_on INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS daily_performance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    total_trades INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    gross_pnl REAL DEFAULT 0,
    net_pnl REAL DEFAULT 0,
    max_drawdown_pct REAL DEFAULT 0,
    win_rate REAL DEFAULT 0,
    avg_win REAL DEFAULT 0,
    avg_loss REAL DEFAULT 0,
    token_usage INTEGER DEFAULT 0,
    token_cost_usd REAL DEFAULT 0,
    portfolio_value REAL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS evolution_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    analysis_json TEXT NOT NULL,
    changes_json TEXT,
    patterns_json TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS token_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brain TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    purpose TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS fee_schedule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    maker_fee_pct REAL NOT NULL,
    taker_fee_pct REAL NOT NULL,
    volume_30d_usd REAL,
    fee_tier TEXT,
    checked_at TEXT DEFAULT (datetime('now'))
);
"""


class Database:
    """Async SQLite database wrapper."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._path = db_path or (DATA_DIR / "brain.db")
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> Database:
        """Open connection and initialize schema."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self._path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()
        log.info("database_connected", path=str(self._path))
        return self

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        """Execute a write query and return lastrowid."""
        assert self._conn is not None
        cursor = await self._conn.execute(sql, params)
        await self._conn.commit()
        return cursor.lastrowid or 0

    async def fetchone(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> aiosqlite.Row | None:
        assert self._conn is not None
        cursor = await self._conn.execute(sql, params)
        return await cursor.fetchone()

    async def fetchall(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> list[aiosqlite.Row]:
        assert self._conn is not None
        cursor = await self._conn.execute(sql, params)
        return await cursor.fetchall()
