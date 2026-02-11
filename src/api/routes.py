"""REST API endpoint handlers — read-only data access."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import structlog
from aiohttp import web

from src.api import ctx_key
from src.shell.truth import compute_truth_benchmarks

log = structlog.get_logger()


def _safe_int(value: str, default: int) -> int:
    """Parse int from query param, returning default on failure."""
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _envelope(data, mode: str) -> dict:
    return {
        "data": data,
        "meta": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": mode,
            "version": "2.0.0",
        },
    }


def _error_envelope(code: str, message: str, mode: str) -> dict:
    return {
        "error": {"code": code, "message": message},
        "meta": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": mode,
            "version": "2.0.0",
        },
    }


async def system_handler(request: web.Request) -> web.Response:
    ctx = request.app[ctx_key]
    config = ctx["config"]
    scan_state = ctx["scan_state"]
    risk = ctx["risk"]

    data = {
        "status": "running",
        "mode": config.mode,
        "uptime_seconds": (datetime.now(timezone.utc) - ctx["started_at"]).total_seconds(),
        "version": "2.0.0",
        "started_at": ctx["started_at"].isoformat(),
        "last_scan": scan_state.get("last_scan"),
        "paused": ctx["commands"].is_paused if ctx.get("commands") else False,
        "halted": risk.is_halted,
        "halt_reason": risk.halt_reason if risk.is_halted else None,
    }
    return web.json_response(_envelope(data, config.mode))


async def portfolio_handler(request: web.Request) -> web.Response:
    ctx = request.app[ctx_key]
    config = ctx["config"]
    portfolio = ctx["portfolio"]
    scan_state = ctx["scan_state"]

    prices = {}
    for sym, sym_data in scan_state.get("symbols", {}).items():
        if "price" in sym_data:
            prices[sym] = sym_data["price"]

    port = await portfolio.get_portfolio(prices)
    total = port.total_value
    cash_pct = (port.cash / total * 100) if total > 0 else 100

    unrealized_pnl = sum(p.unrealized_pnl for p in port.positions)

    data = {
        "total_value": round(total, 2),
        "cash": round(port.cash, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "position_count": len(port.positions),
        "allocation": {
            "cash_pct": round(cash_pct, 1),
            "positions_pct": round(100 - cash_pct, 1),
        },
    }
    return web.json_response(_envelope(data, config.mode))


async def positions_handler(request: web.Request) -> web.Response:
    ctx = request.app[ctx_key]
    config = ctx["config"]
    db = ctx["db"]
    scan_state = ctx["scan_state"]

    rows = await db.fetchall("SELECT * FROM positions")
    positions = []
    for row in rows:
        symbol = row["symbol"]
        current_price = scan_state.get("symbols", {}).get(symbol, {}).get("price")
        entry_price = row["avg_entry"]
        qty = row["qty"]
        unrealized_pnl = ((current_price - entry_price) * qty) if current_price else None
        unrealized_pnl_pct = ((current_price / entry_price - 1) * 100) if current_price and entry_price else None

        positions.append({
            "symbol": symbol,
            "tag": row.get("tag", ""),
            "qty": qty,
            "entry_price": entry_price,
            "current_price": current_price,
            "unrealized_pnl": round(unrealized_pnl, 2) if unrealized_pnl is not None else None,
            "unrealized_pnl_pct": round(unrealized_pnl_pct, 2) if unrealized_pnl_pct is not None else None,
            "stop_loss": row.get("stop_loss"),
            "take_profit": row.get("take_profit"),
            "opened_at": row.get("opened_at"),
        })
    return web.json_response(_envelope(positions, config.mode))


async def trades_handler(request: web.Request) -> web.Response:
    ctx = request.app[ctx_key]
    config = ctx["config"]
    db = ctx["db"]

    limit = max(1, min(_safe_int(request.query.get("limit", "50"), 50), 500))
    since = request.query.get("since")
    until = request.query.get("until")
    symbol = request.query.get("symbol")

    query = "SELECT * FROM trades WHERE 1=1"
    params = []

    if since:
        query += " AND closed_at >= ?"
        params.append(since)
    if until:
        query += " AND closed_at <= ?"
        params.append(until)
    if symbol:
        query += " AND symbol = ?"
        params.append(symbol)

    query += " ORDER BY closed_at DESC LIMIT ?"
    params.append(limit)

    rows = await db.fetchall(query, tuple(params))
    trades = [dict(row) for row in rows]
    return web.json_response(_envelope(trades, config.mode))


async def performance_handler(request: web.Request) -> web.Response:
    ctx = request.app[ctx_key]
    config = ctx["config"]
    db = ctx["db"]

    since = request.query.get("since")
    until = request.query.get("until")
    limit = max(1, min(_safe_int(request.query.get("limit", "365"), 365), 365))

    query = "SELECT * FROM daily_performance WHERE 1=1"
    params = []

    if since:
        query += " AND date >= ?"
        params.append(since)
    if until:
        query += " AND date <= ?"
        params.append(until)

    query += " ORDER BY date DESC LIMIT ?"
    params.append(limit)

    rows = await db.fetchall(query, tuple(params))
    data = [dict(row) for row in rows]
    return web.json_response(_envelope(data, config.mode))


async def risk_handler(request: web.Request) -> web.Response:
    ctx = request.app[ctx_key]
    config = ctx["config"]
    risk = ctx["risk"]
    portfolio = ctx["portfolio"]

    portfolio_value = await portfolio.total_value()
    peak = risk.peak_portfolio or portfolio_value
    drawdown_pct = ((peak - portfolio_value) / peak) if peak > 0 else 0

    data = {
        "limits": {
            "max_position_pct": config.risk.max_position_pct,
            "max_positions": config.risk.max_positions,
            "max_daily_loss_pct": config.risk.max_daily_loss_pct,
            "max_drawdown_pct": config.risk.max_drawdown_pct,
            "max_daily_trades": config.risk.max_daily_trades,
            "max_trade_pct": config.risk.max_trade_pct,
        },
        "current": {
            "daily_pnl": round(risk.daily_pnl, 2),
            "daily_pnl_pct": round(risk.daily_pnl / portfolio_value, 4) if portfolio_value > 0 else 0,
            "daily_trades": risk.daily_trades,
            "consecutive_losses": risk.consecutive_losses,
            "drawdown_pct": round(drawdown_pct, 4),
            "halted": risk.is_halted,
            "halt_reason": risk.halt_reason if risk.is_halted else None,
        },
    }
    return web.json_response(_envelope(data, config.mode))


async def signals_handler(request: web.Request) -> web.Response:
    ctx = request.app[ctx_key]
    config = ctx["config"]
    db = ctx["db"]

    limit = max(1, min(_safe_int(request.query.get("limit", "50"), 50), 500))
    since = request.query.get("since")
    until = request.query.get("until")
    symbol = request.query.get("symbol")
    action = request.query.get("action")

    query = "SELECT * FROM signals WHERE 1=1"
    params = []

    if since:
        query += " AND created_at >= ?"
        params.append(since)
    if until:
        query += " AND created_at <= ?"
        params.append(until)
    if symbol:
        query += " AND symbol = ?"
        params.append(symbol)
    if action:
        query += " AND action = ?"
        params.append(action)

    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    rows = await db.fetchall(query, tuple(params))
    data = [dict(row) for row in rows]
    return web.json_response(_envelope(data, config.mode))


async def strategy_handler(request: web.Request) -> web.Response:
    ctx = request.app[ctx_key]
    config = ctx["config"]
    db = ctx["db"]

    # Active strategy
    active = await db.fetchone(
        "SELECT * FROM strategy_versions WHERE deployed_at IS NOT NULL ORDER BY deployed_at DESC LIMIT 1"
    )

    # Paper test
    paper_test = await db.fetchone(
        "SELECT * FROM paper_tests WHERE status = 'running' ORDER BY started_at DESC LIMIT 1"
    )

    # Recent versions
    versions = await db.fetchall(
        "SELECT * FROM strategy_versions ORDER BY COALESCE(deployed_at, '0') DESC LIMIT 10"
    )

    # Parse JSON string columns to avoid double-encoding
    json_fields = ("backtest_result", "paper_test_result", "result")

    def _parse_json_fields(row_dict: dict) -> dict:
        for field in json_fields:
            val = row_dict.get(field)
            if isinstance(val, str):
                try:
                    row_dict[field] = json.loads(val)
                except (json.JSONDecodeError, ValueError):
                    pass  # Keep as string if not valid JSON
        return row_dict

    data = {
        "active": _parse_json_fields(dict(active)) if active else None,
        "paper_test": _parse_json_fields(dict(paper_test)) if paper_test else None,
        "recent_versions": [_parse_json_fields(dict(v)) for v in versions],
    }
    return web.json_response(_envelope(data, config.mode))


async def ai_usage_handler(request: web.Request) -> web.Response:
    ctx = request.app[ctx_key]
    config = ctx["config"]
    ai = ctx["ai"]

    usage = await ai.get_daily_usage()
    data = {
        "today": {
            "total_tokens": usage.get("used", 0),
            "total_cost_usd": round(usage.get("total_cost", 0), 4),
            "budget_limit": config.ai.daily_token_limit,
            "budget_remaining": ai.tokens_remaining,
            "by_model": usage.get("models", {}),
        },
    }
    return web.json_response(_envelope(data, config.mode))


async def benchmarks_handler(request: web.Request) -> web.Response:
    ctx = request.app[ctx_key]
    config = ctx["config"]
    db = ctx["db"]

    try:
        benchmarks = await compute_truth_benchmarks(db)
    except Exception as e:
        log.error("api.benchmarks_error", error=str(e))
        return web.json_response(
            _error_envelope("benchmark_error", "Failed to compute benchmarks", config.mode),
            status=500,
        )
    return web.json_response(_envelope(benchmarks, config.mode))


def setup_routes(app: web.Application) -> None:
    """Register all REST API routes."""
    app.router.add_get("/v1/system", system_handler)
    app.router.add_get("/v1/portfolio", portfolio_handler)
    app.router.add_get("/v1/positions", positions_handler)
    app.router.add_get("/v1/trades", trades_handler)
    app.router.add_get("/v1/performance", performance_handler)
    app.router.add_get("/v1/risk", risk_handler)
    app.router.add_get("/v1/signals", signals_handler)
    app.router.add_get("/v1/strategy", strategy_handler)
    app.router.add_get("/v1/ai/usage", ai_usage_handler)
    app.router.add_get("/v1/benchmarks", benchmarks_handler)
