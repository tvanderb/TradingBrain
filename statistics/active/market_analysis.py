"""Market Analysis Module v001 — hand-written starting point.

Analyzes raw OHLCV data from the candles table.
The orchestrator will rewrite this over time as it learns what context it needs.

This module runs independently — it does NOT see trade performance output.
The orchestrator cross-references both reports.
"""

from src.shell.contract import AnalysisBase


class Analysis(AnalysisBase):

    async def analyze(self, db, schema: dict) -> dict:
        report = {}

        # --- Discover symbols from candles ---
        symbols_row = await db.fetchall(
            "SELECT DISTINCT symbol FROM candles"
        )
        symbols = [r["symbol"] for r in symbols_row]

        # --- Price Summary (per symbol, from candle close prices) ---
        price_summary = {}
        for symbol in symbols:
            # Latest 1d candle for current reference price
            latest = await db.fetchone(
                "SELECT close, volume FROM candles WHERE symbol = ? AND timeframe = '1d' "
                "ORDER BY timestamp DESC LIMIT 1",
                (symbol,),
            )
            if not latest:
                # Fall back to 1h
                latest = await db.fetchone(
                    "SELECT close, volume FROM candles WHERE symbol = ? AND timeframe = '1h' "
                    "ORDER BY timestamp DESC LIMIT 1",
                    (symbol,),
                )
            if not latest:
                continue

            current_price = latest["close"]

            # 24h price change from 1h candles
            price_24h_ago = await db.fetchone(
                "SELECT close FROM candles WHERE symbol = ? AND timeframe = '1h' "
                "AND timestamp <= datetime('now', '-1 day') ORDER BY timestamp DESC LIMIT 1",
                (symbol,),
            )
            # 7d price change from 1d candles
            price_7d_ago = await db.fetchone(
                "SELECT close FROM candles WHERE symbol = ? AND timeframe = '1d' "
                "AND timestamp <= datetime('now', '-7 days') ORDER BY timestamp DESC LIMIT 1",
                (symbol,),
            )

            change_24h = None
            change_7d = None
            if price_24h_ago and price_24h_ago["close"]:
                change_24h = (current_price - price_24h_ago["close"]) / price_24h_ago["close"]
            if price_7d_ago and price_7d_ago["close"]:
                change_7d = (current_price - price_7d_ago["close"]) / price_7d_ago["close"]

            # Volatility from recent 1h returns
            recent_candles = await db.fetchall(
                "SELECT close FROM candles WHERE symbol = ? AND timeframe = '1h' "
                "ORDER BY timestamp DESC LIMIT 48",
                (symbol,),
            )
            volatility = None
            if len(recent_candles) >= 2:
                closes = [r["close"] for r in reversed(recent_candles)]
                returns = [(closes[i] - closes[i-1]) / closes[i-1]
                           for i in range(1, len(closes)) if closes[i-1] > 0]
                if returns:
                    mean_r = sum(returns) / len(returns)
                    variance = sum((r - mean_r) ** 2 for r in returns) / len(returns)
                    volatility = variance ** 0.5

            # Volume trend (compare recent 24h volume to prior 24h)
            vol_recent = await db.fetchone(
                "SELECT SUM(volume) as vol FROM candles WHERE symbol = ? AND timeframe = '1h' "
                "AND timestamp >= datetime('now', '-1 day')",
                (symbol,),
            )
            vol_prior = await db.fetchone(
                "SELECT SUM(volume) as vol FROM candles WHERE symbol = ? AND timeframe = '1h' "
                "AND timestamp >= datetime('now', '-2 days') AND timestamp < datetime('now', '-1 day')",
                (symbol,),
            )
            volume_trend = None
            if vol_recent and vol_prior and vol_prior["vol"] and vol_prior["vol"] > 0:
                volume_trend = vol_recent["vol"] / vol_prior["vol"]

            price_summary[symbol] = {
                "current_price": current_price,
                "change_24h_pct": change_24h,
                "change_7d_pct": change_7d,
                "volatility_1h": volatility,
                "volume_trend": volume_trend,
            }

        report["price_summary"] = price_summary

        # --- Candle Data Depth (how much history per symbol/timeframe) ---
        data_depth = {}
        for symbol in symbols:
            depth = {}
            for tf in ("5m", "1h", "1d"):
                count_row = await db.fetchone(
                    "SELECT COUNT(*) as cnt, MIN(timestamp) as earliest, MAX(timestamp) as latest "
                    "FROM candles WHERE symbol = ? AND timeframe = ?",
                    (symbol, tf),
                )
                depth[tf] = {
                    "count": count_row["cnt"],
                    "earliest": count_row["earliest"],
                    "latest": count_row["latest"],
                }
            data_depth[symbol] = depth

        report["data_depth"] = data_depth

        # --- Data Quality (scan frequency from scan_results) ---
        total_scans = await db.fetchone("SELECT COUNT(*) as cnt FROM scan_results")
        first_scan = await db.fetchone("SELECT MIN(created_at) as ts FROM scan_results")
        last_scan = await db.fetchone("SELECT MAX(created_at) as ts FROM scan_results")

        recent_scans = await db.fetchone(
            "SELECT COUNT(*) as cnt FROM scan_results WHERE created_at >= datetime('now', '-1 hour')"
        )

        report["data_quality"] = {
            "total_scans": total_scans["cnt"],
            "first_scan": first_scan["ts"],
            "last_scan": last_scan["ts"],
            "scans_last_hour": recent_scans["cnt"],
            "expected_scans_per_hour": 12 * len(symbols),
        }

        return report
