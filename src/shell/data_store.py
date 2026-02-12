"""Data Store — tiered OHLCV storage with nightly aggregation.

Manages historical candle data with retention tiers:
- 5-min candles: 30 days
- 1-hour candles: 1 year (aggregated from 5m)
- Daily candles: 7 years (aggregated from 1h)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import structlog

from src.shell.config import DataConfig
from src.shell.database import Database

log = structlog.get_logger()


class DataStore:
    """Manages historical OHLCV data with tiered retention."""

    def __init__(self, db: Database, config: DataConfig) -> None:
        self._db = db
        self._config = config

    async def store_candles(self, symbol: str, timeframe: str, df: pd.DataFrame) -> int:
        """Store candles from a DataFrame. Returns count of new rows inserted."""
        if df.empty:
            return 0

        rows = []
        for ts, row in df.iterrows():
            ts_str = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
            rows.append((
                symbol, timeframe, ts_str,
                float(row["open"]), float(row["high"]),
                float(row["low"]), float(row["close"]),
                float(row.get("volume", 0)),
            ))

        cursor = await self._db.executemany(
            """INSERT OR REPLACE INTO candles
               (symbol, timeframe, timestamp, open, high, low, close, volume)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        await self._db.commit()
        return len(rows)  # rowcount unreliable for executemany in SQLite

    async def get_candles(
        self, symbol: str, timeframe: str, limit: int | None = None
    ) -> pd.DataFrame:
        """Get candles as a DataFrame, ordered by time ascending."""
        params: list = [symbol, timeframe]

        if limit:
            # For LIMIT queries, get most recent N rows ordered ascending
            sql = """SELECT * FROM (
                SELECT timestamp, open, high, low, close, volume
                FROM candles WHERE symbol = ? AND timeframe = ?
                ORDER BY timestamp DESC LIMIT ?
            ) ORDER BY timestamp ASC"""
            params.append(limit)
        else:
            sql = """SELECT timestamp, open, high, low, close, volume
                     FROM candles WHERE symbol = ? AND timeframe = ?
                     ORDER BY timestamp ASC"""

        rows = await self._db.fetchall(sql, tuple(params))
        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        df = pd.DataFrame(rows)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df.set_index("timestamp", inplace=True)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)
        return df

    async def get_candle_count(self, symbol: str, timeframe: str) -> int:
        row = await self._db.fetchone(
            "SELECT COUNT(*) as cnt FROM candles WHERE symbol = ? AND timeframe = ?",
            (symbol, timeframe),
        )
        return row["cnt"] if row else 0

    async def aggregate_5m_to_1h(self) -> int:
        """Aggregate 5-minute candles older than retention into 1-hour candles."""
        cutoff_raw = datetime.now(timezone.utc) - timedelta(days=self._config.candle_5m_retention_days)
        # Snap to hour boundary to avoid splitting a clock-hour across two aggregation runs
        cutoff = cutoff_raw.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")

        # Get distinct symbols with 5m data older than cutoff
        symbols = await self._db.fetchall(
            "SELECT DISTINCT symbol FROM candles WHERE timeframe = '5m' AND timestamp < ?",
            (cutoff,),
        )

        total_aggregated = 0
        for sym_row in symbols:
            symbol = sym_row["symbol"]
            rows = await self._db.fetchall(
                """SELECT timestamp, open, high, low, close, volume
                   FROM candles WHERE symbol = ? AND timeframe = '5m' AND timestamp < ?
                   ORDER BY timestamp ASC""",
                (symbol, cutoff),
            )

            if not rows:
                continue

            df = pd.DataFrame(rows)
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            df.set_index("timestamp", inplace=True)
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = df[col].astype(float)

            # Resample to 1-hour
            hourly = df.resample("1h").agg({
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }).dropna()

            if not hourly.empty:
                count = await self.store_candles(symbol, "1h", hourly)
                total_aggregated += count

                # Only delete 5m candles after successful aggregation
                await self._db.execute(
                    "DELETE FROM candles WHERE symbol = ? AND timeframe = '5m' AND timestamp < ?",
                    (symbol, cutoff),
                )

        await self._db.commit()
        if total_aggregated > 0:
            log.info("data.aggregated_5m_to_1h", candles=total_aggregated)
        return total_aggregated

    async def aggregate_1h_to_daily(self) -> int:
        """Aggregate 1-hour candles older than retention into daily candles."""
        cutoff_raw = datetime.now(timezone.utc) - timedelta(days=self._config.candle_1h_retention_days)
        # Snap to day boundary to avoid splitting a clock-day across two aggregation runs
        cutoff = cutoff_raw.replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")

        symbols = await self._db.fetchall(
            "SELECT DISTINCT symbol FROM candles WHERE timeframe = '1h' AND timestamp < ?",
            (cutoff,),
        )

        total_aggregated = 0
        for sym_row in symbols:
            symbol = sym_row["symbol"]
            rows = await self._db.fetchall(
                """SELECT timestamp, open, high, low, close, volume
                   FROM candles WHERE symbol = ? AND timeframe = '1h' AND timestamp < ?
                   ORDER BY timestamp ASC""",
                (symbol, cutoff),
            )

            if not rows:
                continue

            df = pd.DataFrame(rows)
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            df.set_index("timestamp", inplace=True)
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = df[col].astype(float)

            daily = df.resample("1D").agg({
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }).dropna()

            if not daily.empty:
                count = await self.store_candles(symbol, "1d", daily)
                total_aggregated += count

                # Only delete 1h candles after successful aggregation
                await self._db.execute(
                    "DELETE FROM candles WHERE symbol = ? AND timeframe = '1h' AND timestamp < ?",
                    (symbol, cutoff),
                )

        await self._db.commit()
        if total_aggregated > 0:
            log.info("data.aggregated_1h_to_daily", candles=total_aggregated)
        return total_aggregated

    async def prune_old_data(self) -> None:
        """Delete daily candles older than retention limit."""
        years = self._config.candle_1d_retention_years
        cutoff = (datetime.now(timezone.utc) - timedelta(days=years * 365)).strftime("%Y-%m-%dT%H:%M:%S")

        result = await self._db.execute(
            "DELETE FROM candles WHERE timeframe = '1d' AND timestamp < ?",
            (cutoff,),
        )
        await self._db.commit()

        # Prune old token usage logs (aggregate after 3 months)
        token_cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S")
        await self._db.execute(
            "DELETE FROM token_usage WHERE created_at < ?",
            (token_cutoff,),
        )

        # Prune old fee schedule entries (keep last 90 days)
        await self._db.execute(
            "DELETE FROM fee_schedule WHERE checked_at < ?",
            (token_cutoff,),
        )

        # Prune old signal history (6 months)
        signal_cutoff = (datetime.now(timezone.utc) - timedelta(days=180)).strftime("%Y-%m-%d %H:%M:%S")
        await self._db.execute(
            "DELETE FROM signals WHERE created_at < ?",
            (signal_cutoff,),
        )

        # Prune old scan_results (30 days — high-frequency table, ~288 rows/day/symbol)
        scan_cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        await self._db.execute(
            "DELETE FROM scan_results WHERE created_at < ?",
            (scan_cutoff,),
        )

        # Prune old orchestrator_log (1 year)
        orch_cutoff = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%d %H:%M:%S")
        await self._db.execute(
            "DELETE FROM orchestrator_log WHERE date < ?",
            (orch_cutoff,),
        )

        # Activity log (90 days)
        activity_cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S")
        await self._db.execute(
            "DELETE FROM activity_log WHERE timestamp < ?",
            (activity_cutoff,),
        )

        await self._db.commit()

    async def run_nightly_maintenance(self) -> None:
        """Run all data maintenance tasks. Called during orchestration window."""
        log.info("data.maintenance_start")
        await self.aggregate_5m_to_1h()
        await self.aggregate_1h_to_daily()
        await self.prune_old_data()
        log.info("data.maintenance_complete")
