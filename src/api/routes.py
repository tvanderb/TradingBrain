"""REST API endpoint handlers — read-only data access."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

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


def _validate_datetime(value: str, param_name: str, mode: str) -> str:
    """Validate ISO 8601 datetime string; raise 400 if invalid."""
    try:
        datetime.fromisoformat(value)
        return value
    except (ValueError, TypeError):
        raise web.HTTPBadRequest(
            text=json.dumps(_error_envelope("invalid_param",
                f"Invalid {param_name}: {value!r}. Expected ISO 8601 format.", mode)),
            content_type="application/json",
        )


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
        "last_scan": _last.isoformat() if (_last := scan_state.get("last_scan_at")) is not None else None,
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

    rows = await db.fetchall(
        """SELECT symbol, tag, qty, avg_entry, stop_loss, take_profit,
                  intent, strategy_version, opened_at
           FROM positions"""
    )
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
            "tag": row["tag"],
            "qty": qty,
            "entry_price": entry_price,
            "current_price": current_price,
            "unrealized_pnl": round(unrealized_pnl, 2) if unrealized_pnl is not None else None,
            "unrealized_pnl_pct": round(unrealized_pnl_pct, 2) if unrealized_pnl_pct is not None else None,
            "stop_loss": row["stop_loss"],
            "take_profit": row["take_profit"],
            "opened_at": row["opened_at"],
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

    query = """SELECT id, symbol, tag, side, qty, entry_price, exit_price, pnl, pnl_pct,
                      fees, intent, strategy_version, strategy_regime, close_reason,
                      max_adverse_excursion, opened_at, closed_at
               FROM trades WHERE 1=1"""
    params = []

    if since:
        _validate_datetime(since, "since", config.mode)
        query += " AND closed_at >= ?"
        params.append(since)
    if until:
        _validate_datetime(until, "until", config.mode)
        query += " AND closed_at <= ?"
        params.append(until)
    if symbol:
        query += " AND symbol = ?"
        params.append(symbol)

    query += " ORDER BY closed_at DESC LIMIT ?"
    params.append(limit)

    rows = await db.fetchall(query, tuple(params))
    trades = []
    for row in rows:
        t = dict(row)
        if t.get("pnl_pct") is not None:
            t["pnl_pct"] = round(t["pnl_pct"] * 100, 4)
        if t.get("max_adverse_excursion") is not None:
            t["max_adverse_excursion"] = round(t["max_adverse_excursion"], 6)
        trades.append(t)
    return web.json_response(_envelope(trades, config.mode))


async def performance_handler(request: web.Request) -> web.Response:
    ctx = request.app[ctx_key]
    config = ctx["config"]
    db = ctx["db"]

    since = request.query.get("since")
    until = request.query.get("until")
    limit = max(1, min(_safe_int(request.query.get("limit", "365"), 365), 365))

    query = """SELECT date, portfolio_value, cash, total_trades, wins, losses,
                      gross_pnl, net_pnl, fees_total, max_drawdown_pct, win_rate,
                      expectancy, sharpe, strategy_version
               FROM daily_performance WHERE 1=1"""
    params = []

    if since:
        _validate_datetime(since, "since", config.mode)
        query += " AND date >= ?"
        params.append(since)
    if until:
        _validate_datetime(until, "until", config.mode)
        query += " AND date <= ?"
        params.append(until)

    query += " ORDER BY date DESC LIMIT ?"
    params.append(limit)

    rows = await db.fetchall(query, tuple(params))
    data = []
    for row in rows:
        d = dict(row)
        if d.get("max_drawdown_pct") is not None:
            d["max_drawdown_pct"] = round(d["max_drawdown_pct"] * 100, 4)
        if d.get("win_rate") is not None:
            d["win_rate"] = round(d["win_rate"] * 100, 2)
        data.append(d)
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
            "max_position_pct": round(config.risk.max_position_pct * 100, 2),
            "max_positions": config.risk.max_positions,
            "max_daily_loss_pct": round(config.risk.max_daily_loss_pct * 100, 2),
            "max_drawdown_pct": round(config.risk.max_drawdown_pct * 100, 2),
            "max_daily_trades": config.risk.max_daily_trades,
            "max_trade_pct": round(config.risk.max_trade_pct * 100, 2),
        },
        "current": {
            "daily_pnl": round(risk.daily_pnl, 2),
            "daily_pnl_pct": round(risk.daily_pnl / portfolio_value * 100, 2) if portfolio_value > 0 else 0,
            "daily_trades": risk.daily_trades,
            "consecutive_losses": risk.consecutive_losses,
            "drawdown_pct": round(drawdown_pct * 100, 2),
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

    query = """SELECT id, symbol, action, size_pct, confidence, intent, reasoning,
                      strategy_version, strategy_regime, acted_on, rejected_reason, tag, created_at
               FROM signals WHERE 1=1"""
    params = []

    if since:
        _validate_datetime(since, "since", config.mode)
        query += " AND created_at >= ?"
        params.append(since)
    if until:
        _validate_datetime(until, "until", config.mode)
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
    data = []
    for row in rows:
        s = dict(row)
        if s.get("size_pct") is not None:
            s["size_pct"] = round(s["size_pct"] * 100, 4)
        data.append(s)
    return web.json_response(_envelope(data, config.mode))


async def strategy_handler(request: web.Request) -> web.Response:
    ctx = request.app[ctx_key]
    config = ctx["config"]
    db = ctx["db"]

    _sv_cols = """version, parent_version, code_hash, risk_tier, description, tags,
                  backtest_result, paper_test_result, market_conditions,
                  deployed_at, retired_at, created_at"""

    # Active strategy
    active = await db.fetchone(
        f"SELECT {_sv_cols} FROM strategy_versions WHERE deployed_at IS NOT NULL ORDER BY deployed_at DESC LIMIT 1"
    )

    # Recent versions
    versions = await db.fetchall(
        f"SELECT {_sv_cols} FROM strategy_versions ORDER BY COALESCE(deployed_at, '0') DESC LIMIT 10"
    )

    # Parse JSON string columns to avoid double-encoding
    json_fields = ("backtest_result", "paper_test_result", "market_conditions", "tags")

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

    # Shallow copy to avoid mutating cached truth dict; normalize fractions → percentages
    benchmarks = dict(benchmarks)
    for pct_key in ("win_rate", "signal_act_rate", "max_drawdown_pct",
                     "best_trade_pnl_pct", "worst_trade_pnl_pct"):
        if benchmarks.get(pct_key) is not None:
            benchmarks[pct_key] = round(benchmarks[pct_key] * 100, 4)

    return web.json_response(_envelope(benchmarks, config.mode))


_VALID_ACTIVITY_CATEGORIES = {"TRADE", "RISK", "SYSTEM", "SCAN", "ORCH", "STRATEGY", "CANDIDATE"}
_VALID_ACTIVITY_SEVERITIES = {"info", "warning", "error"}


async def activity_handler(request: web.Request) -> web.Response:
    ctx = request.app[ctx_key]
    config = ctx["config"]
    activity_logger = ctx.get("activity_logger")

    if not activity_logger:
        return web.json_response(
            _error_envelope("unavailable", "Activity logger not configured", config.mode),
            status=503,
        )

    limit = max(1, min(_safe_int(request.query.get("limit", "50"), 50), 500))
    since = request.query.get("since")
    until = request.query.get("until")
    category = request.query.get("category")
    severity = request.query.get("severity")

    if since:
        _validate_datetime(since, "since", config.mode)
    if until:
        _validate_datetime(until, "until", config.mode)

    if category and category not in _VALID_ACTIVITY_CATEGORIES:
        return web.json_response(
            _error_envelope("invalid_param", f"Invalid category. Must be one of: {', '.join(sorted(_VALID_ACTIVITY_CATEGORIES))}", config.mode),
            status=400,
        )
    if severity and severity not in _VALID_ACTIVITY_SEVERITIES:
        return web.json_response(
            _error_envelope("invalid_param", f"Invalid severity. Must be one of: {', '.join(sorted(_VALID_ACTIVITY_SEVERITIES))}", config.mode),
            status=400,
        )

    rows = await activity_logger.query(
        limit=limit, since=since, until=until, category=category, severity=severity,
    )

    # Parse detail JSON for each entry
    entries = []
    for row in rows:
        entry = dict(row)
        detail = entry.get("detail")
        if isinstance(detail, str):
            try:
                entry["detail"] = json.loads(detail)
            except (json.JSONDecodeError, ValueError):
                pass
        entries.append(entry)

    return web.json_response(_envelope(entries, config.mode))


async def candidates_handler(request: web.Request) -> web.Response:
    ctx = request.app[ctx_key]
    config = ctx["config"]
    candidate_manager = ctx.get("candidate_manager")

    if not candidate_manager:
        return web.json_response(
            _error_envelope("unavailable", "Candidate system not configured", config.mode),
            status=503,
        )

    try:
        slots = await candidate_manager.get_context_for_orchestrator()
    except Exception as e:
        log.error("api.candidates_error", error=str(e))
        return web.json_response(
            _error_envelope("internal_error", "Failed to fetch candidate data", config.mode),
            status=500,
        )
    return web.json_response(_envelope(slots, config.mode))


async def predictions_handler(request: web.Request) -> web.Response:
    ctx = request.app[ctx_key]
    config = ctx["config"]
    db = ctx["db"]

    limit = max(1, min(_safe_int(request.query.get("limit", "50"), 50), 500))
    graded_param = request.query.get("graded")

    _pred_cols = """id, cycle_id, claim, evidence, falsification, confidence,
                    evaluation_timeframe, category, graded_at, grade, grade_evidence,
                    grade_learning, created_at"""

    try:
        if graded_param == "true":
            rows = await db.fetchall(
                f"SELECT {_pred_cols} FROM predictions WHERE graded_at IS NOT NULL ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        elif graded_param == "false":
            rows = await db.fetchall(
                f"SELECT {_pred_cols} FROM predictions WHERE graded_at IS NULL ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        else:
            rows = await db.fetchall(
                f"SELECT {_pred_cols} FROM predictions ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
    except Exception as e:
        log.error("api.predictions_error", error=str(e))
        return web.json_response(
            _error_envelope("internal_error", "Failed to fetch predictions", config.mode),
            status=500,
        )
    return web.json_response(_envelope([dict(r) for r in rows], config.mode))


async def strategy_doc_versions_handler(request: web.Request) -> web.Response:
    ctx = request.app[ctx_key]
    config = ctx["config"]
    db = ctx["db"]

    limit = max(1, min(_safe_int(request.query.get("limit", "20"), 20), 100))

    try:
        rows = await db.fetchall(
            """SELECT id, version, reflection_cycle_id, created_at, LENGTH(content) as content_length
               FROM strategy_doc_versions
               ORDER BY version DESC LIMIT ?""",
            (limit,),
        )
    except Exception as e:
        log.error("api.strategy_doc_versions_error", error=str(e))
        return web.json_response(
            _error_envelope("internal_error", "Failed to fetch strategy doc versions", config.mode),
            status=500,
        )
    return web.json_response(_envelope([dict(r) for r in rows], config.mode))


async def decisions_handler(request: web.Request) -> web.Response:
    """GET /v1/decisions — orchestrator decision history."""
    ctx = request.app[ctx_key]
    config = ctx["config"]
    db = ctx["db"]

    limit = max(1, min(_safe_int(request.query.get("limit", "20"), 20), 100))
    since = request.query.get("since")
    until = request.query.get("until")

    query = """SELECT id, date, action, strategy_version_from, strategy_version_to,
                      tokens_used, cost_usd, outcome, analysis, created_at
               FROM orchestrator_log WHERE 1=1"""
    params = []

    if since:
        _validate_datetime(since, "since", config.mode)
        query += " AND date >= ?"
        params.append(since)
    if until:
        _validate_datetime(until, "until", config.mode)
        query += " AND date <= ?"
        params.append(until)

    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    rows = await db.fetchall(query, tuple(params))
    decisions = []
    for row in rows:
        d = dict(row)
        if isinstance(d.get("analysis"), str):
            try:
                d["analysis"] = json.loads(d["analysis"])
            except (json.JSONDecodeError, ValueError):
                pass
        decisions.append(d)
    return web.json_response(_envelope(decisions, config.mode))


async def thoughts_list_handler(request: web.Request) -> web.Response:
    """GET /v1/thoughts — list orchestrator cycles."""
    ctx = request.app[ctx_key]
    config = ctx["config"]
    db = ctx["db"]

    limit = max(1, min(_safe_int(request.query.get("limit", "10"), 10), 50))

    rows = await db.fetchall(
        """SELECT cycle_id, COUNT(*) as step_count, MIN(created_at) as started_at,
                  GROUP_CONCAT(DISTINCT model) as models
           FROM orchestrator_thoughts GROUP BY cycle_id ORDER BY MIN(created_at) DESC LIMIT ?""",
        (limit,),
    )
    return web.json_response(_envelope([dict(r) for r in rows], config.mode))


async def thoughts_cycle_handler(request: web.Request) -> web.Response:
    """GET /v1/thoughts/{cycle_id} — steps in a cycle."""
    ctx = request.app[ctx_key]
    config = ctx["config"]
    db = ctx["db"]
    cycle_id = request.match_info["cycle_id"]

    rows = await db.fetchall(
        """SELECT id, step, model, LENGTH(full_response) as response_length, created_at
           FROM orchestrator_thoughts WHERE cycle_id = ? ORDER BY id ASC""",
        (cycle_id,),
    )
    if not rows:
        return web.json_response(
            _error_envelope("not_found", f"No thoughts found for cycle {cycle_id!r}", config.mode),
            status=404,
        )
    return web.json_response(_envelope([dict(r) for r in rows], config.mode))


async def thoughts_detail_handler(request: web.Request) -> web.Response:
    """GET /v1/thoughts/{cycle_id}/{step} — full thought detail."""
    ctx = request.app[ctx_key]
    config = ctx["config"]
    db = ctx["db"]
    cycle_id = request.match_info["cycle_id"]
    step = request.match_info["step"]

    row = await db.fetchone(
        """SELECT step, model, input_summary, full_response, parsed_result, created_at
           FROM orchestrator_thoughts WHERE cycle_id = ? AND step = ?""",
        (cycle_id, step),
    )
    if not row:
        return web.json_response(
            _error_envelope("not_found", f"Thought step {step!r} not found in cycle {cycle_id!r}", config.mode),
            status=404,
        )

    d = dict(row)
    if isinstance(d.get("parsed_result"), str):
        try:
            d["parsed_result"] = json.loads(d["parsed_result"])
        except (json.JSONDecodeError, ValueError):
            pass
    return web.json_response(_envelope(d, config.mode))


async def strategy_doc_handler(request: web.Request) -> web.Response:
    """GET /v1/strategy-doc — current live strategy document."""
    ctx = request.app[ctx_key]
    config = ctx["config"]

    doc_path = Path(__file__).parent.parent.parent / "strategy" / "strategy_document.md"
    if not doc_path.exists():
        return web.json_response(
            _error_envelope("not_found", "Strategy document not found", config.mode),
            status=404,
        )

    content = doc_path.read_text()
    data = {"content": content, "length": len(content)}
    return web.json_response(_envelope(data, config.mode))


async def strategy_doc_version_handler(request: web.Request) -> web.Response:
    """GET /v1/strategy-doc/versions/{version} — version content from DB."""
    ctx = request.app[ctx_key]
    config = ctx["config"]
    db = ctx["db"]

    version_str = request.match_info["version"]
    try:
        version = int(version_str)
    except (ValueError, TypeError):
        return web.json_response(
            _error_envelope("invalid_param", f"Version must be an integer, got {version_str!r}", config.mode),
            status=400,
        )

    row = await db.fetchone(
        "SELECT id, version, content, reflection_cycle_id, created_at FROM strategy_doc_versions WHERE version = ?",
        (version,),
    )
    if not row:
        return web.json_response(
            _error_envelope("not_found", f"Strategy doc version {version} not found", config.mode),
            status=404,
        )
    return web.json_response(_envelope(dict(row), config.mode))


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
    app.router.add_get("/v1/activity", activity_handler)
    app.router.add_get("/v1/candidates", candidates_handler)
    app.router.add_get("/v1/predictions", predictions_handler)
    app.router.add_get("/v1/strategy-doc/versions", strategy_doc_versions_handler)
    app.router.add_get("/v1/decisions", decisions_handler)
    app.router.add_get("/v1/thoughts", thoughts_list_handler)
    app.router.add_get("/v1/thoughts/{cycle_id}", thoughts_cycle_handler)
    app.router.add_get("/v1/thoughts/{cycle_id}/{step}", thoughts_detail_handler)
    app.router.add_get("/v1/strategy-doc", strategy_doc_handler)
    app.router.add_get("/v1/strategy-doc/versions/{version}", strategy_doc_version_handler)
