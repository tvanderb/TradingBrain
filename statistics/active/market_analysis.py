"""Market Analysis Module v001 — hand-written starting point.

Analyzes exchange/indicator data from scan_results and candles.
The orchestrator will rewrite this over time as it learns what context it needs.

This module runs independently — it does NOT see trade performance output.
The orchestrator cross-references both reports.
"""

from src.shell.contract import AnalysisBase


class Analysis(AnalysisBase):

    async def analyze(self, db, schema: dict) -> dict:
        report = {}

        # --- Price Summary (per symbol) ---
        symbols_row = await db.fetchall(
            "SELECT DISTINCT symbol FROM scan_results"
        )
        symbols = [r["symbol"] for r in symbols_row]

        price_summary = {}
        for symbol in symbols:
            latest = await db.fetchone(
                "SELECT price, ema_fast, ema_slow, rsi, volume_ratio, spread, strategy_regime "
                "FROM scan_results WHERE symbol = ? ORDER BY created_at DESC LIMIT 1",
                (symbol,),
            )
            if not latest:
                continue

            # 24h price change
            price_24h_ago = await db.fetchone(
                "SELECT price FROM scan_results WHERE symbol = ? "
                "AND created_at <= datetime('now', '-1 day') ORDER BY created_at DESC LIMIT 1",
                (symbol,),
            )
            # 7d price change
            price_7d_ago = await db.fetchone(
                "SELECT price FROM scan_results WHERE symbol = ? "
                "AND created_at <= datetime('now', '-7 days') ORDER BY created_at DESC LIMIT 1",
                (symbol,),
            )

            current_price = latest["price"]
            change_24h = None
            change_7d = None
            if price_24h_ago and price_24h_ago["price"]:
                change_24h = (current_price - price_24h_ago["price"]) / price_24h_ago["price"]
            if price_7d_ago and price_7d_ago["price"]:
                change_7d = (current_price - price_7d_ago["price"]) / price_7d_ago["price"]

            price_summary[symbol] = {
                "current_price": current_price,
                "change_24h_pct": change_24h,
                "change_7d_pct": change_7d,
                "ema_fast": latest["ema_fast"],
                "ema_slow": latest["ema_slow"],
                "ema_alignment": "bullish" if (latest["ema_fast"] or 0) > (latest["ema_slow"] or 0) else "bearish",
                "rsi": latest["rsi"],
                "volume_ratio": latest["volume_ratio"],
                "spread": latest["spread"],
                "strategy_regime": latest["strategy_regime"],
            }

        report["price_summary"] = price_summary

        # --- Indicator Distributions (last 24h) ---
        indicator_stats = {}
        for symbol in symbols:
            rows = await db.fetchall(
                "SELECT rsi, volume_ratio, ema_fast, ema_slow FROM scan_results "
                "WHERE symbol = ? AND created_at >= datetime('now', '-1 day')",
                (symbol,),
            )
            if not rows:
                continue

            rsi_values = [r["rsi"] for r in rows if r["rsi"] is not None]
            vol_values = [r["volume_ratio"] for r in rows if r["volume_ratio"] is not None]

            rsi_overbought = sum(1 for v in rsi_values if v > 70) if rsi_values else 0
            rsi_oversold = sum(1 for v in rsi_values if v < 30) if rsi_values else 0
            ema_bullish = sum(1 for r in rows if (r["ema_fast"] or 0) > (r["ema_slow"] or 0))

            indicator_stats[symbol] = {
                "scan_count_24h": len(rows),
                "rsi_avg": sum(rsi_values) / len(rsi_values) if rsi_values else None,
                "rsi_overbought_pct": rsi_overbought / len(rsi_values) if rsi_values else 0,
                "rsi_oversold_pct": rsi_oversold / len(rsi_values) if rsi_values else 0,
                "volume_ratio_avg": sum(vol_values) / len(vol_values) if vol_values else None,
                "ema_bullish_pct": ema_bullish / len(rows) if rows else 0,
            }

        report["indicator_stats_24h"] = indicator_stats

        # --- Signal Proximity (how close to generating signals) ---
        proximity = {}
        for symbol in symbols:
            latest = await db.fetchone(
                "SELECT ema_fast, ema_slow, rsi, volume_ratio FROM scan_results "
                "WHERE symbol = ? ORDER BY created_at DESC LIMIT 1",
                (symbol,),
            )
            if not latest or latest["ema_fast"] is None or latest["ema_slow"] is None:
                continue

            ema_gap_pct = abs(latest["ema_fast"] - latest["ema_slow"]) / latest["ema_slow"] if latest["ema_slow"] else 0
            rsi_distance_to_trigger = min(abs((latest["rsi"] or 50) - 30), abs((latest["rsi"] or 50) - 70))

            proximity[symbol] = {
                "ema_gap_pct": ema_gap_pct,
                "ema_cross_near": ema_gap_pct < 0.005,
                "rsi_distance_to_extreme": rsi_distance_to_trigger,
                "volume_above_avg": (latest["volume_ratio"] or 0) > 1.2,
            }

        report["signal_proximity"] = proximity

        # --- Data Quality ---
        total_scans = await db.fetchone("SELECT COUNT(*) as cnt FROM scan_results")
        first_scan = await db.fetchone("SELECT MIN(created_at) as ts FROM scan_results")
        last_scan = await db.fetchone("SELECT MAX(created_at) as ts FROM scan_results")

        # Check for gaps: count scans in last hour (should be ~12 at 5-min intervals)
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
