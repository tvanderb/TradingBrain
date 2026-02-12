"""Prometheus /metrics endpoint — exports portfolio, risk, and position gauges."""

from __future__ import annotations

import time
from datetime import datetime, timezone

import structlog
from aiohttp import web
from prometheus_client import CollectorRegistry, Gauge, Info, generate_latest

from src.api import ctx_key
from src.shell.truth import compute_truth_benchmarks

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

# --- Truth benchmark gauges ---
tb_total_return_pct = Gauge("tb_total_return_pct", "Total return percentage", registry=registry)
tb_win_rate = Gauge("tb_win_rate", "Win rate (0.0-1.0)", registry=registry)
tb_trade_count = Gauge("tb_trade_count", "Total closed trades", registry=registry)
tb_win_count = Gauge("tb_win_count", "Winning trades", registry=registry)
tb_loss_count = Gauge("tb_loss_count", "Losing trades", registry=registry)
tb_net_pnl_usd = Gauge("tb_net_pnl_usd", "All-time net P&L in USD", registry=registry)
tb_total_fees_usd = Gauge("tb_total_fees_usd", "All-time fees paid in USD", registry=registry)
tb_avg_win_usd = Gauge("tb_avg_win_usd", "Average winning trade in USD", registry=registry)
tb_avg_loss_usd = Gauge("tb_avg_loss_usd", "Average losing trade in USD", registry=registry)
tb_expectancy_usd = Gauge("tb_expectancy_usd", "Expected value per trade in USD", registry=registry)
tb_profit_factor = Gauge("tb_profit_factor", "Gross wins / gross losses", registry=registry)
tb_sharpe_ratio = Gauge("tb_sharpe_ratio", "Annualized Sharpe ratio", registry=registry)
tb_sortino_ratio = Gauge("tb_sortino_ratio", "Annualized Sortino ratio", registry=registry)
tb_max_drawdown_pct = Gauge("tb_max_drawdown_pct", "Historical max drawdown (%)", registry=registry)
tb_avg_trade_duration_hours = Gauge("tb_avg_trade_duration_hours", "Average trade hold time in hours", registry=registry)
tb_best_trade_pct = Gauge("tb_best_trade_pct", "Best trade return %", registry=registry)
tb_worst_trade_pct = Gauge("tb_worst_trade_pct", "Worst trade return %", registry=registry)
tb_signal_act_rate = Gauge("tb_signal_act_rate", "Signals acted on / total signals", registry=registry)
tb_total_signals = Gauge("tb_total_signals", "Total signals generated", registry=registry)
tb_total_scans = Gauge("tb_total_scans", "Total scan cycles", registry=registry)
tb_strategy_version_count = Gauge("tb_strategy_version_count", "Strategy versions deployed", registry=registry)

# --- AI gauges ---
tb_ai_daily_cost_usd = Gauge("tb_ai_daily_cost_usd", "AI spend today in USD", registry=registry)
tb_ai_daily_tokens = Gauge("tb_ai_daily_tokens", "Tokens used today", registry=registry)
tb_ai_token_budget_pct = Gauge("tb_ai_token_budget_pct", "Percent of daily token budget consumed", registry=registry)

# --- Scan & system gauges ---
tb_scan_age_seconds = Gauge("tb_scan_age_seconds", "Seconds since last scan", registry=registry)
tb_uptime_seconds = Gauge("tb_uptime_seconds", "System uptime in seconds", registry=registry)
tb_symbol_price_usd = Gauge("tb_symbol_price_usd", "Per-symbol price in USD", ["symbol"], registry=registry)
tb_portfolio_allocation_pct = Gauge("tb_portfolio_allocation_pct", "Percent of portfolio invested in positions", registry=registry)

# --- Truth benchmark cache ---
_truth_cache: dict = {"data": None, "expires_at": 0.0}
TRUTH_CACHE_TTL = 300  # 5 minutes


async def _get_cached_truth(db):
    """Return cached truth benchmarks, refreshing if stale."""
    now = time.monotonic()
    if _truth_cache["data"] is not None and now < _truth_cache["expires_at"]:
        return _truth_cache["data"]
    benchmarks = await compute_truth_benchmarks(db)
    _truth_cache["data"] = benchmarks
    _truth_cache["expires_at"] = now + TRUTH_CACHE_TTL
    return benchmarks


async def metrics_handler(request: web.Request) -> web.Response:
    """Prometheus scrape endpoint. Reads current state and returns text metrics."""
    ctx = request.app[ctx_key]
    portfolio = ctx["portfolio"]
    risk = ctx["risk"]
    config = ctx["config"]
    db = ctx["db"]
    ai = ctx["ai"]
    scan_state = ctx["scan_state"]

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

        # --- Truth benchmarks (cached 5 min) ---
        truth = await _get_cached_truth(db)
        starting_capital = config.paper_balance_usd
        net_pnl = truth.get("net_pnl", 0) or 0
        tb_total_return_pct.set(net_pnl / starting_capital * 100 if starting_capital > 0 else 0)
        tb_win_rate.set(truth.get("win_rate", 0) or 0)
        tb_trade_count.set(truth.get("trade_count", 0) or 0)
        tb_win_count.set(truth.get("win_count", 0) or 0)
        tb_loss_count.set(truth.get("loss_count", 0) or 0)
        tb_net_pnl_usd.set(net_pnl)
        tb_total_fees_usd.set(truth.get("total_fees", 0) or 0)
        tb_avg_win_usd.set(truth.get("avg_win", 0) or 0)
        tb_avg_loss_usd.set(truth.get("avg_loss", 0) or 0)
        tb_expectancy_usd.set(truth.get("expectancy", 0) or 0)
        pf = truth.get("profit_factor", 0)
        tb_profit_factor.set(pf if pf != float("inf") else 0)
        tb_sharpe_ratio.set(truth.get("sharpe_ratio", 0) or 0)
        tb_sortino_ratio.set(truth.get("sortino_ratio", 0) or 0)
        tb_max_drawdown_pct.set((truth.get("max_drawdown_pct", 0) or 0) * 100)
        tb_avg_trade_duration_hours.set(truth.get("avg_trade_duration_hours", 0) or 0)
        tb_best_trade_pct.set((truth.get("best_trade_pnl_pct", 0) or 0) * 100)
        tb_worst_trade_pct.set((truth.get("worst_trade_pnl_pct", 0) or 0) * 100)
        tb_signal_act_rate.set(truth.get("signal_act_rate", 0) or 0)
        tb_total_signals.set(truth.get("total_signals", 0) or 0)
        tb_total_scans.set(truth.get("total_scans", 0) or 0)
        tb_strategy_version_count.set(truth.get("strategy_version_count", 0) or 0)

        # --- AI usage ---
        try:
            usage = await ai.get_daily_usage()
            tb_ai_daily_cost_usd.set(usage.get("total_cost", 0) or 0)
            tb_ai_daily_tokens.set(usage.get("used", 0) or 0)
            limit = usage.get("daily_limit", 0) or 0
            used = usage.get("used", 0) or 0
            tb_ai_token_budget_pct.set(used / limit * 100 if limit > 0 else 0)
        except Exception:
            pass  # AI may not be initialized

        # --- Scan age & uptime ---
        now_utc = datetime.now(timezone.utc)
        last_scan_at = scan_state.get("last_scan_at")
        if last_scan_at:
            tb_scan_age_seconds.set((now_utc - last_scan_at).total_seconds())

        started_at = ctx.get("started_at")
        if started_at:
            tb_uptime_seconds.set((now_utc - started_at).total_seconds())

        # --- Per-symbol prices ---
        tb_symbol_price_usd._metrics.clear()
        symbols = scan_state.get("symbols", {})
        for sym, data in symbols.items():
            price = data.get("price")
            if price is not None:
                tb_symbol_price_usd.labels(symbol=sym).set(price)

        # --- Portfolio allocation ---
        tb_portfolio_allocation_pct.set(
            (total - portfolio.cash) / total * 100 if total > 0 else 0
        )

    except Exception as e:
        log.error("metrics.collect_error", error=str(e), error_type=type(e).__name__)

    output = generate_latest(registry)
    resp = web.Response(body=output)
    resp.content_type = "text/plain"
    resp.headers["Content-Type"] = "text/plain; version=0.0.4; charset=utf-8"
    return resp
