"""Truth Benchmarks — rigid shell component.

Computes simple, verifiable metrics directly from raw database data.
The orchestrator CANNOT modify this file. These are ground truth.

Every calculation here must be trivially verifiable by inspecting
the raw data. No complex statistics, no heuristics, no interpretations.
"""

from __future__ import annotations

import math

from src.shell.database import Database


async def compute_truth_benchmarks(db: Database) -> dict:
    """Compute all truth benchmarks from raw DB data.

    Returns a flat dict of metrics. All values are either counts,
    sums, or simple ratios directly derivable from the tables.
    """
    benchmarks = {}

    # --- Trade Performance (from trades table) ---
    row = await db.fetchone("""
        SELECT
            COUNT(*) as trade_count,
            COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0) as win_count,
            COALESCE(SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END), 0) as loss_count,
            COALESCE(SUM(pnl), 0) as net_pnl,
            COALESCE(SUM(fees), 0) as total_fees,
            COALESCE(AVG(CASE WHEN pnl > 0 THEN pnl END), 0) as avg_win,
            COALESCE(AVG(CASE WHEN pnl < 0 THEN pnl END), 0) as avg_loss
        FROM trades WHERE closed_at IS NOT NULL
    """)

    trade_count = row["trade_count"]
    win_count = row["win_count"]
    loss_count = row["loss_count"]

    benchmarks["trade_count"] = trade_count
    benchmarks["win_count"] = win_count
    benchmarks["loss_count"] = loss_count
    benchmarks["win_rate"] = win_count / trade_count if trade_count > 0 else 0.0
    benchmarks["net_pnl"] = row["net_pnl"]
    benchmarks["total_fees"] = row["total_fees"]
    benchmarks["avg_win"] = row["avg_win"]
    benchmarks["avg_loss"] = row["avg_loss"]

    # Expectancy: (win_rate * avg_win) + (loss_rate * avg_loss)
    if trade_count > 0:
        win_rate = win_count / trade_count
        loss_rate = loss_count / trade_count
        benchmarks["expectancy"] = (win_rate * row["avg_win"]) + (loss_rate * row["avg_loss"])
    else:
        benchmarks["expectancy"] = 0.0

    # --- Consecutive Losses (current streak from most recent trades) ---
    recent_trades = await db.fetchall(
        "SELECT pnl FROM trades WHERE closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT 50"
    )
    consecutive_losses = 0
    for t in recent_trades:
        if t["pnl"] is not None and t["pnl"] < 0:
            consecutive_losses += 1
        else:
            break
    benchmarks["consecutive_losses"] = consecutive_losses

    # --- Portfolio State ---
    # Current cash from daily_performance (most recent snapshot)
    snapshot = await db.fetchone(
        "SELECT portfolio_value, cash FROM daily_performance ORDER BY date DESC LIMIT 1"
    )
    if snapshot:
        benchmarks["portfolio_value"] = snapshot["portfolio_value"]
        benchmarks["portfolio_cash"] = snapshot["cash"]
    else:
        benchmarks["portfolio_value"] = None
        benchmarks["portfolio_cash"] = None

    # --- Max Drawdown (from daily snapshots) ---
    snapshots = await db.fetchall(
        "SELECT portfolio_value FROM daily_performance ORDER BY date ASC"
    )
    peak = 0.0
    max_drawdown = 0.0
    for s in snapshots:
        val = s["portfolio_value"]
        if val is None:
            continue
        if val > peak:
            peak = val
        if peak > 0:
            dd = (peak - val) / peak
            if dd > max_drawdown:
                max_drawdown = dd
    benchmarks["max_drawdown_pct"] = max_drawdown

    # --- Signal Activity ---
    sig_row = await db.fetchone("""
        SELECT
            COUNT(*) as total_signals,
            COALESCE(SUM(acted_on), 0) as acted_signals
        FROM signals
    """)
    total_signals = sig_row["total_signals"]
    acted_signals = sig_row["acted_signals"]
    benchmarks["total_signals"] = total_signals
    benchmarks["acted_signals"] = acted_signals
    benchmarks["signal_act_rate"] = acted_signals / total_signals if total_signals > 0 else 0.0

    # --- Scan Activity ---
    scan_row = await db.fetchone("SELECT COUNT(*) as total_scans FROM scan_results")
    benchmarks["total_scans"] = scan_row["total_scans"]

    # System uptime: time since first scan
    first_scan = await db.fetchone(
        "SELECT MIN(created_at) as first_scan FROM scan_results"
    )
    benchmarks["first_scan_at"] = first_scan["first_scan"] if first_scan else None

    # Data freshness: time of most recent scan
    last_scan = await db.fetchone(
        "SELECT MAX(created_at) as last_scan FROM scan_results"
    )
    benchmarks["last_scan_at"] = last_scan["last_scan"] if last_scan else None

    # --- Strategy Version ---
    version_row = await db.fetchone(
        "SELECT version FROM strategy_versions WHERE deployed_at IS NOT NULL ORDER BY deployed_at DESC LIMIT 1"
    )
    benchmarks["current_strategy_version"] = version_row["version"] if version_row else None

    # Number of strategy versions deployed
    version_count = await db.fetchone("SELECT COUNT(*) as count FROM strategy_versions")
    benchmarks["strategy_version_count"] = version_count["count"]

    # --- Profit Factor ---
    pf_row = await db.fetchone("""
        SELECT
            COALESCE(SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END), 0) as gross_wins,
            COALESCE(SUM(CASE WHEN pnl < 0 THEN ABS(pnl) ELSE 0 END), 0) as gross_losses
        FROM trades WHERE closed_at IS NOT NULL
    """)
    gross_wins = pf_row["gross_wins"]
    gross_losses = pf_row["gross_losses"]
    benchmarks["profit_factor"] = gross_wins / gross_losses if gross_losses > 0 else (float("inf") if gross_wins > 0 else 0.0)

    # --- Close Reason Breakdown ---
    reason_rows = await db.fetchall("""
        SELECT close_reason, COUNT(*) as cnt
        FROM trades WHERE closed_at IS NOT NULL
        GROUP BY close_reason
    """)
    benchmarks["close_reason_breakdown"] = {
        (r["close_reason"] or "unknown"): r["cnt"] for r in reason_rows
    }

    # --- Average Trade Duration (hours) ---
    duration_row = await db.fetchone("""
        SELECT AVG(
            (julianday(closed_at) - julianday(opened_at)) * 24
        ) as avg_hours
        FROM trades WHERE closed_at IS NOT NULL AND opened_at IS NOT NULL
    """)
    benchmarks["avg_trade_duration_hours"] = duration_row["avg_hours"] if duration_row["avg_hours"] else 0.0

    # --- Best / Worst Trade ---
    extremes = await db.fetchone("""
        SELECT
            MAX(pnl_pct) as best_pnl_pct,
            MIN(pnl_pct) as worst_pnl_pct
        FROM trades WHERE closed_at IS NOT NULL
    """)
    benchmarks["best_trade_pnl_pct"] = extremes["best_pnl_pct"] if extremes["best_pnl_pct"] is not None else 0.0
    benchmarks["worst_trade_pnl_pct"] = extremes["worst_pnl_pct"] if extremes["worst_pnl_pct"] is not None else 0.0

    # --- Sharpe & Sortino Ratios (from daily_performance snapshots) ---
    daily_rows = await db.fetchall(
        "SELECT portfolio_value FROM daily_performance ORDER BY date ASC"
    )
    if len(daily_rows) >= 3:
        values = [r["portfolio_value"] for r in daily_rows if r["portfolio_value"] is not None]
        daily_returns = []
        for i in range(1, len(values)):
            if values[i - 1] > 0:
                daily_returns.append((values[i] - values[i - 1]) / values[i - 1])
        if len(daily_returns) >= 2:
            mean_r = sum(daily_returns) / len(daily_returns)
            variance = sum((r - mean_r) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
            std_r = math.sqrt(variance) if variance > 0 else 0
            benchmarks["sharpe_ratio"] = (mean_r / std_r * math.sqrt(365)) if std_r > 0 else 0.0

            downside = [r for r in daily_returns if r < 0]
            if len(downside) >= 2:
                down_var = sum(r ** 2 for r in downside) / (len(downside) - 1)
                down_std = math.sqrt(down_var) if down_var > 0 else 0
                benchmarks["sortino_ratio"] = (mean_r / down_std * math.sqrt(365)) if down_std > 0 else 0.0
            else:
                benchmarks["sortino_ratio"] = 0.0
        else:
            benchmarks["sharpe_ratio"] = 0.0
            benchmarks["sortino_ratio"] = 0.0
    else:
        benchmarks["sharpe_ratio"] = 0.0
        benchmarks["sortino_ratio"] = 0.0

    return benchmarks
