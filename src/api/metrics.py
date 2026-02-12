"""Prometheus /metrics endpoint — exports portfolio, risk, and position gauges."""

from __future__ import annotations

import structlog
from aiohttp import web
from prometheus_client import CollectorRegistry, Gauge, Info, generate_latest

from src.api import ctx_key

log = structlog.get_logger()

# Custom registry avoids pytest conflicts with the global default registry.
registry = CollectorRegistry()

# --- Fund-level gauges ---
portfolio_value = Gauge("tb_portfolio_value_usd", "Total portfolio value in USD", registry=registry)
cash_usd = Gauge("tb_cash_usd", "Available cash in USD", registry=registry)
position_count = Gauge("tb_position_count", "Number of open positions", registry=registry)
portfolio_peak = Gauge("tb_portfolio_peak_usd", "All-time portfolio high-water mark", registry=registry)
drawdown_pct = Gauge("tb_drawdown_pct", "Current drawdown from peak (%)", registry=registry)

# --- Risk gauges ---
daily_trades = Gauge("tb_daily_trades", "Number of trades today", registry=registry)
daily_pnl = Gauge("tb_daily_pnl_usd", "Daily P&L in USD", registry=registry)
consecutive_losses = Gauge("tb_consecutive_losses", "Consecutive losing trades", registry=registry)
halted = Gauge("tb_halted", "Trading halted (1=yes, 0=no)", registry=registry)
fees_today = Gauge("tb_fees_today_usd", "Fees paid today in USD", registry=registry)

# --- Per-position gauges ---
position_value = Gauge("tb_position_value_usd", "Position value in USD", ["symbol", "tag"], registry=registry)
position_pnl = Gauge("tb_position_pnl_usd", "Position unrealized P&L in USD", ["symbol", "tag"], registry=registry)

# --- System info ---
system_info = Info("tb_system", "Trading system metadata", registry=registry)


async def metrics_handler(request: web.Request) -> web.Response:
    """Prometheus scrape endpoint. Reads current state and returns text metrics."""
    ctx = request.app[ctx_key]
    portfolio = ctx["portfolio"]
    risk = ctx["risk"]
    config = ctx["config"]

    try:
        total = await portfolio.total_value()
        peak = risk.peak_portfolio or total

        # Fund-level
        portfolio_value.set(total)
        cash_usd.set(portfolio.cash)
        position_count.set(portfolio.position_count)
        portfolio_peak.set(peak)
        drawdown_pct.set((peak - total) / peak * 100 if peak > 0 else 0)

        # Risk
        daily_trades.set(risk.daily_trades)
        daily_pnl.set(risk.daily_pnl)
        consecutive_losses.set(risk.consecutive_losses)
        halted.set(1 if risk.is_halted else 0)
        fees_today.set(portfolio._fees_today)

        # Per-position: clear stale labels then set current
        position_value._metrics.clear()
        position_pnl._metrics.clear()
        for tag, pos in portfolio.positions.items():
            symbol = pos["symbol"]
            entry = pos["avg_entry"]
            current = pos.get("current_price", entry)
            qty = pos["qty"]
            position_value.labels(symbol=symbol, tag=tag).set(current * qty)
            position_pnl.labels(symbol=symbol, tag=tag).set((current - entry) * qty)

        # System info (static labels)
        system_info.info({"mode": config.mode, "version": "2.0.0"})

    except Exception as e:
        log.error("metrics.collect_error", error=str(e), error_type=type(e).__name__)

    output = generate_latest(registry)
    resp = web.Response(body=output)
    resp.content_type = "text/plain"
    resp.headers["Content-Type"] = "text/plain; version=0.0.4; charset=utf-8"
    return resp
