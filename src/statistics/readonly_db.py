"""ReadOnlyDB — SELECT-only database wrapper for analysis modules.

Wraps an aiosqlite connection and blocks all write operations.
Analysis modules receive this instead of the raw Database object.
"""

from __future__ import annotations

import re

import aiosqlite
import structlog

log = structlog.get_logger()

# Patterns that indicate a write operation
_WRITE_PATTERNS = re.compile(
    r"^\s*(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|ATTACH|DETACH|REINDEX|VACUUM|PRAGMA\s+\w+\s*=|BEGIN|COMMIT|ROLLBACK|SAVEPOINT|RELEASE|LOAD_EXTENSION)",
    re.IGNORECASE,
)

# CTE bypass: WITH ... INSERT/UPDATE/DELETE/etc.
_CTE_WRITE_PATTERN = re.compile(
    r"^\s*WITH\b.*\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|LOAD_EXTENSION)\b",
    re.IGNORECASE | re.DOTALL,
)

# Strip SQL block comments (/* ... */) and line comments (--)
_SQL_COMMENT = re.compile(r"/\*.*?\*/|--[^\n]*", re.DOTALL)


class ReadOnlyDB:
    """Read-only database wrapper. Only allows SELECT queries."""

    def __init__(self, conn: aiosqlite.Connection):
        self.__conn = conn  # name-mangled to prevent direct access from modules

    def __getattr__(self, name: str):
        """Block access to internal connection attributes."""
        if name in ("_conn", "__conn", "_ReadOnlyDB__conn"):
            raise AttributeError("Direct connection access not allowed")
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

    def _check_readonly(self, sql: str) -> None:
        """Raise ValueError if the SQL is not a read-only query."""
        # Strip null bytes to prevent bypass
        sql = sql.replace('\x00', '')
        # Strip comments to prevent bypass via /* comment */ DROP TABLE
        cleaned = _SQL_COMMENT.sub("", sql)
        # Block load_extension() as a SQL function (e.g. SELECT load_extension(...))
        if re.search(r'\bload_extension\s*\(', cleaned, re.IGNORECASE):
            raise ValueError(f"load_extension() blocked in read-only mode: {cleaned[:80]}")
        # Check for CTE write bypass (WITH ... INSERT/UPDATE/DELETE)
        if _CTE_WRITE_PATTERN.search(cleaned):
            raise ValueError(f"Write operation blocked in read-only mode (CTE): {cleaned[:80]}")
        # Check each statement to prevent multi-statement bypass (e.g. "SELECT 1; DROP TABLE")
        for statement in cleaned.split(";"):
            statement = statement.strip()
            if statement and _WRITE_PATTERNS.match(statement):
                raise ValueError(f"Write operation blocked in read-only mode: {statement[:80]}")

    async def execute(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        """Execute a read-only SQL query."""
        self._check_readonly(sql)
        return await self.__conn.execute(sql, params)

    async def fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        """Execute query and return one row as dict, or None."""
        self._check_readonly(sql)
        cursor = await self.__conn.execute(sql, params)
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        """Execute query and return all rows as list of dicts."""
        self._check_readonly(sql)
        cursor = await self.__conn.execute(sql, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


def get_schema_description() -> dict:
    """Return a dict describing all tables, columns, and their purposes.

    This is passed to analysis modules so they know what data is available.
    """
    return {
        "candles": {
            "description": "OHLCV market data, tiered by timeframe",
            "columns": {
                "symbol": "Trading pair (e.g., BTC/USD)",
                "timeframe": "'5m', '1h', or '1d'",
                "timestamp": "ISO 8601 datetime",
                "open": "Opening price", "high": "High price",
                "low": "Low price", "close": "Closing price",
                "volume": "Trade volume",
            },
        },
        "trades": {
            "description": "Completed trades with P&L",
            "columns": {
                "symbol": "Trading pair",
                "tag": "Position tag (unique identifier)",
                "side": "'long' (system is long-only)",
                "qty": "Quantity traded",
                "entry_price": "Entry fill price",
                "exit_price": "Exit fill price",
                "pnl": "Realized profit/loss (USD)",
                "pnl_pct": "P&L as percentage of entry",
                "fees": "Fees paid (USD)",
                "intent": "DAY, SWING, or POSITION",
                "strategy_version": "Strategy version that generated this trade",
                "strategy_regime": "What the strategy thought the regime was (not truth)",
                "opened_at": "Position open time",
                "closed_at": "Position close time",
            },
        },
        "signals": {
            "description": "All signals generated by the strategy",
            "columns": {
                "symbol": "Trading pair",
                "action": "BUY, SELL, CLOSE, or MODIFY",
                "tag": "Position tag (for targeted signals)",
                "size_pct": "Position size as fraction of portfolio",
                "confidence": "Strategy confidence 0.0-1.0",
                "intent": "DAY, SWING, or POSITION",
                "reasoning": "Strategy's reasoning text",
                "strategy_regime": "What the strategy thought the regime was",
                "acted_on": "1 if trade was executed, 0 if rejected",
                "rejected_reason": "Why signal was rejected (if applicable)",
                "created_at": "Signal generation time",
            },
        },
        "scan_results": {
            "description": "Price + spread audit trail from every scan, with signal tracking",
            "columns": {
                "timestamp": "Scan time",
                "symbol": "Trading pair",
                "price": "Current price at scan time",
                "spread": "Bid-ask spread",
                "signal_generated": "1 if a signal was generated this scan",
                "signal_action": "BUY/SELL/CLOSE if signal generated",
                "signal_confidence": "Signal confidence if generated",
            },
        },
        "daily_performance": {
            "description": "Daily portfolio snapshots",
            "columns": {
                "date": "Date (YYYY-MM-DD)",
                "portfolio_value": "Total portfolio value",
                "cash": "Cash balance",
                "total_trades": "Trades completed that day",
                "wins": "Winning trades", "losses": "Losing trades",
                "gross_pnl": "P&L before fees", "net_pnl": "P&L after fees",
                "fees_total": "Total fees that day",
                "win_rate": "Win rate that day",
                "expectancy": "Expectancy that day",
                "strategy_version": "Active strategy version",
            },
        },
        "positions": {
            "description": "Currently open positions (keyed by tag, multiple per symbol allowed)",
            "columns": {
                "symbol": "Trading pair",
                "tag": "Unique position identifier",
                "side": "'long' (system is long-only)",
                "qty": "Position size",
                "avg_entry": "Average entry price",
                "current_price": "Last known price",
                "stop_loss": "Stop-loss price",
                "take_profit": "Take-profit price",
                "intent": "DAY, SWING, or POSITION",
            },
        },
        "fee_schedule": {
            "description": "Fee schedule history from Kraken",
            "columns": {
                "maker_fee_pct": "Maker fee percentage",
                "taker_fee_pct": "Taker fee percentage",
                "checked_at": "When fees were last checked",
            },
        },
        "strategy_versions": {
            "description": "Strategy version history",
            "columns": {
                "version": "Version identifier",
                "parent_version": "Previous version",
                "risk_tier": "1=tweak, 2=restructure, 3=overhaul",
                "description": "What changed",
                "deployed_at": "When deployed",
                "retired_at": "When replaced",
            },
        },
        "capital_events": {
            "description": "Capital deposits, withdrawals, and adjustments",
            "columns": {
                "type": "Event type (deposit, withdrawal, adjustment)",
                "amount": "Amount in USD",
                "timestamp": "When the event occurred",
                "notes": "Optional description",
            },
        },
        "orders": {
            "description": "Exchange order tracking with fill confirmation",
            "columns": {
                "txid": "Kraken transaction ID",
                "tag": "Position tag",
                "symbol": "Trading pair",
                "side": "buy or sell",
                "order_type": "market, limit, stop-loss, take-profit",
                "volume": "Requested volume",
                "status": "pending, filled, timeout, canceled, expired",
                "filled_volume": "Actual filled volume",
                "avg_fill_price": "Actual fill price from exchange",
                "fee": "Exchange fee",
                "cost": "Total cost",
                "purpose": "entry, exit, stop_loss, take_profit",
                "placed_at": "When the order was placed",
                "filled_at": "When the order was filled",
            },
        },
        "conditional_orders": {
            "description": "Exchange-native stop-loss and take-profit orders",
            "columns": {
                "tag": "Position tag (unique identifier)",
                "symbol": "Trading pair",
                "entry_txid": "Transaction ID of the entry order",
                "sl_txid": "Transaction ID of the stop-loss order on exchange",
                "tp_txid": "Transaction ID of the take-profit order on exchange",
                "sl_price": "Stop-loss trigger price",
                "tp_price": "Take-profit trigger price",
                "status": "active, canceled, filled_sl, filled_tp",
            },
        },
    }
