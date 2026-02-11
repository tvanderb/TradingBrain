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

-- Open positions (keyed by tag, multiple positions per symbol allowed)
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    tag TEXT NOT NULL,
    side TEXT NOT NULL DEFAULT 'long',
    qty REAL NOT NULL,
    avg_entry REAL NOT NULL,
    current_price REAL DEFAULT 0,
    unrealized_pnl REAL DEFAULT 0,
    entry_fee REAL DEFAULT 0,
    stop_loss REAL,
    take_profit REAL,
    intent TEXT NOT NULL DEFAULT 'DAY',
    strategy_version TEXT,
    opened_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE(tag)
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

-- Scan results: price + spread audit trail, signal tracking
CREATE TABLE IF NOT EXISTS scan_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    price REAL NOT NULL,
    spread REAL,
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

-- Capital events (deposits, withdrawals, adjustments)
CREATE TABLE IF NOT EXISTS capital_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    amount REAL NOT NULL,
    timestamp TEXT DEFAULT (datetime('now')),
    notes TEXT
);

-- Exchange orders (fill confirmation tracking)
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    txid TEXT NOT NULL UNIQUE,
    tag TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT NOT NULL,
    volume REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    filled_volume REAL DEFAULT 0,
    avg_fill_price REAL,
    fee REAL DEFAULT 0,
    cost REAL DEFAULT 0,
    placed_at TEXT DEFAULT (datetime('now')),
    filled_at TEXT,
    kraken_response TEXT,
    purpose TEXT DEFAULT 'entry',
    created_at TEXT DEFAULT (datetime('now'))
);

-- Conditional orders (exchange-native SL/TP)
CREATE TABLE IF NOT EXISTS conditional_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tag TEXT NOT NULL UNIQUE,
    symbol TEXT NOT NULL,
    entry_txid TEXT,
    sl_txid TEXT,
    tp_txid TEXT,
    sl_price REAL,
    tp_price REAL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Indexes for common queries
-- Note: idx_positions_tag and idx_positions_symbol created after special migrations
CREATE INDEX IF NOT EXISTS idx_candles_symbol_tf ON candles(symbol, timeframe, timestamp);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol, closed_at);
CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);
CREATE INDEX IF NOT EXISTS idx_daily_perf_date ON daily_performance(date);
CREATE INDEX IF NOT EXISTS idx_token_usage_date ON token_usage(created_at);
CREATE INDEX IF NOT EXISTS idx_scan_results_ts ON scan_results(timestamp);
CREATE INDEX IF NOT EXISTS idx_scan_results_symbol ON scan_results(symbol, timestamp);
CREATE INDEX IF NOT EXISTS idx_thoughts_cycle ON orchestrator_thoughts(cycle_id, created_at);
CREATE INDEX IF NOT EXISTS idx_observations_date ON orchestrator_observations(date);
CREATE INDEX IF NOT EXISTS idx_orders_txid ON orders(txid);
CREATE INDEX IF NOT EXISTS idx_orders_tag ON orders(tag);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_conditional_orders_tag ON conditional_orders(tag);
CREATE INDEX IF NOT EXISTS idx_conditional_orders_status ON conditional_orders(status);
"""

# Migrations for existing databases (columns added after initial schema)
MIGRATIONS = [
    # Add strategy_regime to trades (if column doesn't exist yet)
    ("trades", "strategy_regime", "ALTER TABLE trades ADD COLUMN strategy_regime TEXT"),
    # Add strategy_regime to signals (if column doesn't exist yet)
    ("signals", "strategy_regime", "ALTER TABLE signals ADD COLUMN strategy_regime TEXT"),
    # Add per-pair symbol to fee_schedule
    ("fee_schedule", "symbol", "ALTER TABLE fee_schedule ADD COLUMN symbol TEXT"),
    # Persist entry_fee on positions (for accurate P&L on restart)
    ("positions", "entry_fee", "ALTER TABLE positions ADD COLUMN entry_fee REAL DEFAULT 0"),
    # Persist strategy_version on positions (for SL/TP trade attribution)
    ("positions", "strategy_version", "ALTER TABLE positions ADD COLUMN strategy_version TEXT"),
    # Tag columns for multi-position support
    ("trades", "tag", "ALTER TABLE trades ADD COLUMN tag TEXT"),
    ("signals", "tag", "ALTER TABLE signals ADD COLUMN tag TEXT"),
    # Close reason tracking (signal, stop_loss, take_profit, emergency, reconciliation)
    ("trades", "close_reason", "ALTER TABLE trades ADD COLUMN close_reason TEXT"),
]


class Database:
    def __init__(self, db_path: str):
        self._path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        try:
            self._conn.row_factory = aiosqlite.Row
            await self._conn.execute("PRAGMA journal_mode=WAL")
            await self._conn.execute("PRAGMA foreign_keys=ON")
            await self._conn.executescript(SCHEMA)
            await self._run_migrations()
            await self._run_special_migrations()
            # Position indexes created after special migrations (tag column may not exist before)
            await self._conn.execute("CREATE INDEX IF NOT EXISTS idx_positions_tag ON positions(tag)")
            await self._conn.execute("CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol)")
            await self._conn.commit()
            log.info("database.connected", path=self._path)
        except Exception:
            await self._conn.close()
            self._conn = None
            raise

    async def _run_migrations(self) -> None:
        """Apply column additions to existing databases."""
        for table, column, sql in MIGRATIONS:
            # Check if column already exists
            cursor = await self._conn.execute(f"PRAGMA table_info({table})")
            columns = [row[1] for row in await cursor.fetchall()]
            if column not in columns:
                await self._conn.execute(sql)
                log.info("database.migration", table=table, column=column)

    async def _run_special_migrations(self) -> None:
        """Handle migrations that can't be done with simple ALTER TABLE.

        - Positions table: remove UNIQUE(symbol), add tag column with UNIQUE(tag).
          SQLite doesn't support DROP CONSTRAINT, so we recreate the table.
        """
        # Check if positions table needs migration (has tag column?)
        cursor = await self._conn.execute("PRAGMA table_info(positions)")
        columns = {row[1]: row for row in await cursor.fetchall()}

        if "tag" not in columns:
            log.info("database.special_migration", migration="positions_add_tag")

            # Read existing positions
            existing = await self._conn.execute("SELECT * FROM positions")
            rows = [dict(r) for r in await existing.fetchall()]

            # Drop old table and indexes
            await self._conn.execute("DROP TABLE IF EXISTS positions")

            # Recreate with new schema (from SCHEMA above, already has tag + UNIQUE(tag))
            await self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    tag TEXT NOT NULL,
                    side TEXT NOT NULL DEFAULT 'long',
                    qty REAL NOT NULL,
                    avg_entry REAL NOT NULL,
                    current_price REAL DEFAULT 0,
                    unrealized_pnl REAL DEFAULT 0,
                    entry_fee REAL DEFAULT 0,
                    stop_loss REAL,
                    take_profit REAL,
                    intent TEXT NOT NULL DEFAULT 'DAY',
                    strategy_version TEXT,
                    opened_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now')),
                    UNIQUE(tag)
                );
                CREATE INDEX IF NOT EXISTS idx_positions_tag ON positions(tag);
                CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol);
            """)

            # Backfill existing positions with auto-generated tags
            tag_counters: dict[str, int] = {}
            for row in rows:
                symbol = row["symbol"]
                tag_counters[symbol] = tag_counters.get(symbol, 0) + 1
                tag = f"auto_{symbol.replace('/', '')}_{tag_counters[symbol]:03d}"
                await self._conn.execute(
                    """INSERT INTO positions
                       (symbol, tag, side, qty, avg_entry, current_price, unrealized_pnl,
                        entry_fee, stop_loss, take_profit, intent, strategy_version, opened_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (symbol, tag, row.get("side", "long"), row["qty"], row["avg_entry"],
                     row.get("current_price", 0), row.get("unrealized_pnl", 0),
                     row.get("entry_fee", 0), row.get("stop_loss"), row.get("take_profit"),
                     row.get("intent", "DAY"), row.get("strategy_version"),
                     row.get("opened_at"), row.get("updated_at")),
                )
            await self._conn.commit()
            log.info("database.special_migration.complete",
                     migration="positions_add_tag", backfilled=len(rows))

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
