"""SQLite database — single source of truth for all persistent state."""

from __future__ import annotations

import aiosqlite
import structlog

log = structlog.get_logger()

SCHEMA = """
-- Market data (tiered OHLCV)
CREATE TABLE IF NOT EXISTS candles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,          -- '5m', '1h', '1d'
    timestamp TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    UNIQUE(symbol, timeframe, timestamp)
);

-- Open positions
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL UNIQUE,
    side TEXT NOT NULL DEFAULT 'long',
    qty REAL NOT NULL,
    avg_entry REAL NOT NULL,
    current_price REAL DEFAULT 0,
    unrealized_pnl REAL DEFAULT 0,
    stop_loss REAL,
    take_profit REAL,
    intent TEXT NOT NULL DEFAULT 'DAY',
    opened_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Completed trades
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL,
    pnl REAL,
    pnl_pct REAL,
    fees REAL DEFAULT 0,
    intent TEXT NOT NULL DEFAULT 'DAY',
    strategy_version TEXT,
    strategy_regime TEXT,               -- what the strategy thought the regime was at trade time
    opened_at TEXT DEFAULT (datetime('now')),
    closed_at TEXT,
    notes TEXT
);

-- Signal history
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    size_pct REAL NOT NULL,
    confidence REAL,
    intent TEXT,
    reasoning TEXT,
    strategy_version TEXT,
    strategy_regime TEXT,               -- what the strategy thought the regime was at signal time
    acted_on INTEGER DEFAULT 0,
    rejected_reason TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Daily performance snapshots
CREATE TABLE IF NOT EXISTS daily_performance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    portfolio_value REAL,
    cash REAL,
    total_trades INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    gross_pnl REAL DEFAULT 0,
    net_pnl REAL DEFAULT 0,
    fees_total REAL DEFAULT 0,
    max_drawdown_pct REAL DEFAULT 0,
    win_rate REAL DEFAULT 0,
    expectancy REAL DEFAULT 0,
    sharpe REAL,
    strategy_version TEXT,
    notes TEXT
);

-- Strategy version index
CREATE TABLE IF NOT EXISTS strategy_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT NOT NULL UNIQUE,
    parent_version TEXT,
    code_hash TEXT NOT NULL,
    risk_tier INTEGER DEFAULT 1,       -- 1=tweak, 2=restructure, 3=overhaul
    description TEXT,
    tags TEXT,                          -- JSON array
    backtest_result TEXT,               -- JSON
    paper_test_result TEXT,             -- JSON
    market_conditions TEXT,             -- JSON
    deployed_at TEXT,
    retired_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Orchestrator reports
CREATE TABLE IF NOT EXISTS orchestrator_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    action TEXT NOT NULL,               -- 'no_change', 'tweak', 'restructure', 'overhaul'
    analysis TEXT,                      -- JSON: reasoning
    changes TEXT,                       -- JSON: what changed
    strategy_version_from TEXT,
    strategy_version_to TEXT,
    tokens_used INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Token usage tracking
CREATE TABLE IF NOT EXISTS token_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    purpose TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Fee schedule (updated daily from Kraken, per-pair)
CREATE TABLE IF NOT EXISTS fee_schedule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT,
    maker_fee_pct REAL NOT NULL,
    taker_fee_pct REAL NOT NULL,
    volume_tier TEXT,
    checked_at TEXT DEFAULT (datetime('now'))
);

-- Strategy state persistence
CREATE TABLE IF NOT EXISTS strategy_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    state_json TEXT NOT NULL,
    saved_at TEXT DEFAULT (datetime('now'))
);

-- Paper test tracking
CREATE TABLE IF NOT EXISTS paper_tests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_version TEXT NOT NULL,
    risk_tier INTEGER NOT NULL,
    required_days INTEGER NOT NULL,
    started_at TEXT DEFAULT (datetime('now')),
    ends_at TEXT NOT NULL,
    status TEXT DEFAULT 'running',      -- 'running', 'passed', 'failed', 'terminated'
    result TEXT,                         -- JSON
    completed_at TEXT
);

-- Scan results: raw indicator values (truth) + strategy's regime classification (interpretation)
CREATE TABLE IF NOT EXISTS scan_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    price REAL NOT NULL,
    ema_fast REAL,
    ema_slow REAL,
    rsi REAL,
    volume_ratio REAL,
    spread REAL,
    strategy_regime TEXT,               -- what the strategy classified (fact about decision, not truth)
    signal_generated INTEGER DEFAULT 0,
    signal_action TEXT,
    signal_confidence REAL,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Orchestrator thought spool (full AI responses for browsing)
CREATE TABLE IF NOT EXISTS orchestrator_thoughts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT NOT NULL,
    step TEXT NOT NULL,
    model TEXT NOT NULL,
    input_summary TEXT,
    full_response TEXT NOT NULL,
    parsed_result TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Orchestrator daily observations (rolling window, replaces strategy doc appends)
CREATE TABLE IF NOT EXISTS orchestrator_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    cycle_id TEXT NOT NULL,
    market_summary TEXT,
    strategy_assessment TEXT,
    notable_findings TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(date, cycle_id)
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_candles_symbol_tf ON candles(symbol, timeframe, timestamp);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol, closed_at);
CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);
CREATE INDEX IF NOT EXISTS idx_daily_perf_date ON daily_performance(date);
CREATE INDEX IF NOT EXISTS idx_token_usage_date ON token_usage(created_at);
CREATE INDEX IF NOT EXISTS idx_scan_results_ts ON scan_results(timestamp);
CREATE INDEX IF NOT EXISTS idx_scan_results_symbol ON scan_results(symbol, timestamp);
CREATE INDEX IF NOT EXISTS idx_thoughts_cycle ON orchestrator_thoughts(cycle_id, created_at);
CREATE INDEX IF NOT EXISTS idx_observations_date ON orchestrator_observations(date);
"""

# Migrations for existing databases (columns added after initial schema)
MIGRATIONS = [
    # Add strategy_regime to trades (if column doesn't exist yet)
    ("trades", "strategy_regime", "ALTER TABLE trades ADD COLUMN strategy_regime TEXT"),
    # Add strategy_regime to signals (if column doesn't exist yet)
    ("signals", "strategy_regime", "ALTER TABLE signals ADD COLUMN strategy_regime TEXT"),
    # Add per-pair symbol to fee_schedule
    ("fee_schedule", "symbol", "ALTER TABLE fee_schedule ADD COLUMN symbol TEXT"),
]


class Database:
    def __init__(self, db_path: str):
        self._path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA)
        await self._run_migrations()
        await self._conn.commit()
        log.info("database.connected", path=self._path)

    async def _run_migrations(self) -> None:
        """Apply column additions to existing databases."""
        for table, column, sql in MIGRATIONS:
            # Check if column already exists
            cursor = await self._conn.execute(f"PRAGMA table_info({table})")
            columns = [row[1] for row in await cursor.fetchall()]
            if column not in columns:
                await self._conn.execute(sql)
                log.info("database.migration", table=table, column=column)

    async def close(self) -> None:
        if self._conn:
            await self._conn.commit()
            await self._conn.close()
            self._conn = None
            log.info("database.closed")

    @property
    def conn(self) -> aiosqlite.Connection:
        if not self._conn:
            raise RuntimeError("Database not connected")
        return self._conn

    async def execute(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        return await self.conn.execute(sql, params)

    async def executemany(self, sql: str, params: list[tuple]) -> aiosqlite.Cursor:
        return await self.conn.executemany(sql, params)

    async def fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        cursor = await self.conn.execute(sql, params)
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        cursor = await self.conn.execute(sql, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def commit(self) -> None:
        await self.conn.commit()
