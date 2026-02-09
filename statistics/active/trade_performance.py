"""Trade Performance Module v001 — hand-written starting point.

Analyzes trade execution quality and strategy effectiveness.
The orchestrator will rewrite this over time as it learns what metrics matter.

This module runs independently — it does NOT see market analysis output.
The orchestrator cross-references both reports.
"""

from src.shell.contract import AnalysisBase


class Analysis(AnalysisBase):

    async def analyze(self, db, schema: dict) -> dict:
        report = {}

        # --- Performance by Symbol ---
        symbols = await db.fetchall(
            "SELECT DISTINCT symbol FROM trades WHERE closed_at IS NOT NULL"
        )

        by_symbol = {}
        for row in symbols:
            sym = row["symbol"]
            stats = await db.fetchone(
                """SELECT
                    COUNT(*) as trades,
                    COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0) as wins,
                    COALESCE(SUM(pnl), 0) as net_pnl,
                    COALESCE(SUM(fees), 0) as total_fees,
                    COALESCE(AVG(pnl), 0) as avg_pnl,
                    COALESCE(AVG(CASE WHEN pnl > 0 THEN pnl END), 0) as avg_win,
                    COALESCE(AVG(CASE WHEN pnl <= 0 THEN pnl END), 0) as avg_loss
                FROM trades WHERE symbol = ? AND closed_at IS NOT NULL""",
                (sym,),
            )
            if stats and stats["trades"] > 0:
                win_rate = stats["wins"] / stats["trades"]
                loss_rate = 1 - win_rate
                expectancy = (win_rate * stats["avg_win"]) + (loss_rate * stats["avg_loss"])
                by_symbol[sym] = {
                    "trades": stats["trades"],
                    "wins": stats["wins"],
                    "win_rate": win_rate,
                    "net_pnl": stats["net_pnl"],
                    "total_fees": stats["total_fees"],
                    "avg_pnl": stats["avg_pnl"],
                    "expectancy": expectancy,
                }

        report["by_symbol"] = by_symbol

        # --- Performance by Strategy Regime ---
        regimes = await db.fetchall(
            "SELECT DISTINCT strategy_regime FROM trades WHERE strategy_regime IS NOT NULL AND closed_at IS NOT NULL"
        )

        by_regime = {}
        for row in regimes:
            regime = row["strategy_regime"]
            stats = await db.fetchone(
                """SELECT
                    COUNT(*) as trades,
                    COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0) as wins,
                    COALESCE(SUM(pnl), 0) as net_pnl,
                    COALESCE(AVG(pnl), 0) as avg_pnl
                FROM trades WHERE strategy_regime = ? AND closed_at IS NOT NULL""",
                (regime,),
            )
            if stats and stats["trades"] > 0:
                by_regime[regime] = {
                    "trades": stats["trades"],
                    "wins": stats["wins"],
                    "win_rate": stats["wins"] / stats["trades"],
                    "net_pnl": stats["net_pnl"],
                    "avg_pnl": stats["avg_pnl"],
                }

        report["by_regime"] = by_regime

        # --- Performance by Strategy Version ---
        versions = await db.fetchall(
            "SELECT DISTINCT strategy_version FROM trades WHERE strategy_version IS NOT NULL AND closed_at IS NOT NULL"
        )

        by_version = {}
        for row in versions:
            ver = row["strategy_version"]
            stats = await db.fetchone(
                """SELECT
                    COUNT(*) as trades,
                    COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0) as wins,
                    COALESCE(SUM(pnl), 0) as net_pnl,
                    COALESCE(SUM(fees), 0) as total_fees,
                    COALESCE(AVG(pnl), 0) as avg_pnl,
                    COALESCE(AVG(CASE WHEN pnl > 0 THEN pnl END), 0) as avg_win,
                    COALESCE(AVG(CASE WHEN pnl <= 0 THEN pnl END), 0) as avg_loss,
                    MIN(opened_at) as first_trade,
                    MAX(closed_at) as last_trade
                FROM trades WHERE strategy_version = ? AND closed_at IS NOT NULL""",
                (ver,),
            )
            if stats and stats["trades"] > 0:
                win_rate = stats["wins"] / stats["trades"]
                loss_rate = 1 - win_rate
                expectancy = (win_rate * stats["avg_win"]) + (loss_rate * stats["avg_loss"])
                by_version[ver] = {
                    "trades": stats["trades"],
                    "wins": stats["wins"],
                    "win_rate": win_rate,
                    "net_pnl": stats["net_pnl"],
                    "total_fees": stats["total_fees"],
                    "avg_pnl": stats["avg_pnl"],
                    "expectancy": expectancy,
                    "first_trade": stats["first_trade"],
                    "last_trade": stats["last_trade"],
                }

        report["by_version"] = by_version

        # --- Signal Analysis ---
        signal_stats = await db.fetchone(
            """SELECT
                COUNT(*) as total,
                COALESCE(SUM(acted_on), 0) as acted,
                COALESCE(SUM(CASE WHEN acted_on = 0 THEN 1 ELSE 0 END), 0) as rejected
            FROM signals"""
        )

        if signal_stats and signal_stats["total"] > 0:
            report["signals"] = {
                "total": signal_stats["total"],
                "acted": signal_stats["acted"],
                "rejected": signal_stats["rejected"],
                "act_rate": signal_stats["acted"] / signal_stats["total"],
            }

            # Top rejection reasons
            rejections = await db.fetchall(
                """SELECT rejected_reason, COUNT(*) as cnt
                FROM signals WHERE rejected_reason IS NOT NULL
                GROUP BY rejected_reason ORDER BY cnt DESC LIMIT 5"""
            )
            report["signals"]["top_rejections"] = [
                {"reason": r["rejected_reason"], "count": r["cnt"]} for r in rejections
            ]
        else:
            report["signals"] = {"total": 0, "acted": 0, "rejected": 0, "act_rate": 0}

        # --- Fee Impact ---
        fee_row = await db.fetchone(
            """SELECT
                COALESCE(SUM(fees), 0) as total_fees,
                COALESCE(SUM(CASE WHEN pnl > 0 THEN pnl + fees ELSE 0 END), 0) as gross_wins
            FROM trades WHERE closed_at IS NOT NULL"""
        )
        if fee_row:
            report["fee_impact"] = {
                "total_fees_paid": fee_row["total_fees"],
                "fees_as_pct_of_gross_wins": (
                    fee_row["total_fees"] / fee_row["gross_wins"]
                    if fee_row["gross_wins"] > 0 else None
                ),
            }

            # Current fee schedule
            fee_sched = await db.fetchone(
                "SELECT maker_fee_pct, taker_fee_pct FROM fee_schedule ORDER BY checked_at DESC LIMIT 1"
            )
            if fee_sched:
                # Break-even move required (round-trip: buy taker + sell taker)
                round_trip_pct = (fee_sched["taker_fee_pct"] * 2) / 100
                report["fee_impact"]["round_trip_fee_pct"] = round_trip_pct
                report["fee_impact"]["break_even_move_pct"] = round_trip_pct * 1.5  # need ~1.5x fees to profit
        else:
            report["fee_impact"] = {"total_fees_paid": 0}

        # --- Holding Duration ---
        duration_stats = await db.fetchall(
            """SELECT
                symbol, intent,
                (julianday(closed_at) - julianday(opened_at)) * 24 as hours_held,
                pnl
            FROM trades WHERE closed_at IS NOT NULL AND opened_at IS NOT NULL
            ORDER BY closed_at DESC LIMIT 50"""
        )

        if duration_stats:
            hours = [d["hours_held"] for d in duration_stats if d["hours_held"] is not None]
            winning_hours = [d["hours_held"] for d in duration_stats if d["hours_held"] and d["pnl"] and d["pnl"] > 0]
            losing_hours = [d["hours_held"] for d in duration_stats if d["hours_held"] and d["pnl"] and d["pnl"] <= 0]

            report["holding_duration"] = {
                "avg_hours": sum(hours) / len(hours) if hours else 0,
                "avg_winning_hours": sum(winning_hours) / len(winning_hours) if winning_hours else 0,
                "avg_losing_hours": sum(losing_hours) / len(losing_hours) if losing_hours else 0,
            }
        else:
            report["holding_duration"] = {"avg_hours": 0, "avg_winning_hours": 0, "avg_losing_hours": 0}

        # --- Rolling Metrics (7d, 30d) ---
        for period_name, days in [("7d", 7), ("30d", 30)]:
            period_stats = await db.fetchone(
                f"""SELECT
                    COUNT(*) as trades,
                    COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0) as wins,
                    COALESCE(SUM(pnl), 0) as net_pnl,
                    COALESCE(SUM(fees), 0) as fees
                FROM trades
                WHERE closed_at IS NOT NULL
                AND closed_at >= datetime('now', '-{days} days')""",
            )
            if period_stats and period_stats["trades"] > 0:
                report[f"rolling_{period_name}"] = {
                    "trades": period_stats["trades"],
                    "win_rate": period_stats["wins"] / period_stats["trades"],
                    "net_pnl": period_stats["net_pnl"],
                    "fees": period_stats["fees"],
                }
            else:
                report[f"rolling_{period_name}"] = {"trades": 0, "win_rate": 0, "net_pnl": 0, "fees": 0}

        return report
