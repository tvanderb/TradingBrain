"""Trading Brain v2 — IO-Container Architecture

Main entry point. Wires all components, manages lifecycle, runs the scan loop.

Startup: load config -> connect DB -> load strategy -> connect Kraken -> start Telegram -> start scheduler
Shutdown: stop scheduler -> save strategy state -> cancel orders -> stop WS -> stop Telegram -> close DB
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.shell.config import load_config, Config
from src.shell.contract import Action, Intent, OrderType, RiskLimits, Signal, SymbolData, Portfolio
from src.shell.database import Database
from src.shell.data_store import DataStore
from src.shell.kraken import KrakenREST, KrakenWebSocket
from src.shell.portfolio import PortfolioTracker
from src.shell.risk import RiskManager
from src.orchestrator.ai_client import AIClient
from src.orchestrator.orchestrator import Orchestrator
from src.orchestrator.reporter import Reporter
from src.candidates.manager import CandidateManager
from src.strategy.loader import load_strategy, load_strategy_with_fallback, get_strategy_path, get_code_hash
from src.shell.activity import ActivityLogger
from src.telegram.bot import TelegramBot
from src.telegram.commands import BotCommands
from src.telegram.notifications import Notifier
from aiohttp import web

from src.api.server import create_app as create_api_app
from src.utils.logging import setup_logging

log = structlog.get_logger()


class TradingBrain:
    """Main application — orchestrates all components."""

    def __init__(self) -> None:
        self._config: Config | None = None
        self._db: Database | None = None
        self._kraken: KrakenREST | None = None
        self._ws: KrakenWebSocket | None = None
        self._portfolio: PortfolioTracker | None = None
        self._risk: RiskManager | None = None
        self._strategy = None
        self._ai: AIClient | None = None
        self._orchestrator: Orchestrator | None = None
        self._reporter: Reporter | None = None
        self._data_store: DataStore | None = None
        self._telegram: TelegramBot | None = None
        self._notifier: Notifier | None = None
        self._scheduler: AsyncIOScheduler | None = None
        self._scan_state: dict = {}
        self._commands: BotCommands | None = None
        self._pair_fees: dict[str, tuple[float, float]] = {}  # symbol -> (maker, taker)
        self._activity: ActivityLogger | None = None
        self._candidate_manager: CandidateManager | None = None
        self._api_runner: web.AppRunner | None = None
        self._ws_task: asyncio.Task | None = None
        self._trade_lock = asyncio.Lock()  # Serializes trade execution across scan/monitor/emergency
        self._analyzing = False  # Flag to prevent strategy callbacks during analyze() executor
        self._running = False

    async def start(self) -> None:
        """Full startup sequence."""
        log.info("brain.starting")

        # 1. Config
        self._config = load_config()
        setup_logging(self._config.log_level)
        log.info("config.loaded", mode=self._config.mode, symbols=self._config.symbols)

        # 1b. Validate credentials for live mode
        if not self._config.is_paper():
            if not self._config.kraken.api_key or not self._config.kraken.secret_key:
                raise RuntimeError("Live mode requires KRAKEN_API_KEY and KRAKEN_SECRET_KEY in .env")

        # 2. Database
        self._db = Database(self._config.db_path)
        await self._db.connect()

        # 2b. Activity logger (must be before any events)
        self._activity = ActivityLogger(self._db)

        # 3. Shell components
        self._kraken = KrakenREST(self._config.kraken)
        self._risk = RiskManager(self._config.risk)
        self._portfolio = PortfolioTracker(self._config, self._db, self._kraken)
        self._data_store = DataStore(self._db, self._config.data)
        await self._portfolio.initialize()
        await self._risk.initialize(self._db, tz_name=self._config.timezone)

        # 3b. Evaluate halt conditions (L2: must run before any trading)
        portfolio_value = await self._portfolio.total_value()
        self._risk.evaluate_halt_state(portfolio_value, self._portfolio.daily_start_value)
        if self._risk.is_halted:
            log.warning("brain.halted_on_startup", reason=self._risk.halt_reason)
            await self._activity.risk(
                f"System started HALTED: {self._risk.halt_reason}", severity="warning")

        # 3c. Orphaned position detection (L3)
        config_symbols = set(self._config.symbols)
        position_symbols = {pos["symbol"] for pos in self._portfolio.positions.values()}
        orphaned = position_symbols - config_symbols
        if orphaned:
            log.error("portfolio.orphaned_positions", symbols=list(orphaned))
            self._scan_state["orphaned_positions"] = list(orphaned)
            await self._activity.system(
                f"Orphaned positions: {', '.join(orphaned)}", severity="warning")

        # 3d. Reconcile unfilled orders from previous session (live mode only)
        if not self._config.is_paper():
            await self._reconcile_orders()

        # 3e. Bootstrap historical data if DB is sparse
        await self._bootstrap_historical_data()

        # 4. Strategy (L4: fallback chain with paused mode)
        self._strategy = await load_strategy_with_fallback(self._db)
        if self._strategy:
            risk_limits = RiskLimits(
                max_trade_pct=self._config.risk.max_trade_pct,
                default_trade_pct=self._config.risk.default_trade_pct,
                max_positions=self._config.risk.max_positions,
                max_daily_loss_pct=self._config.risk.max_daily_loss_pct,
                max_drawdown_pct=self._config.risk.max_drawdown_pct,
                max_position_pct=self._config.risk.max_position_pct,
                max_daily_trades=self._config.risk.max_daily_trades,
                rollback_consecutive_losses=self._config.risk.rollback_consecutive_losses,
            )
            self._strategy.initialize(risk_limits, self._config.symbols)

            # Restore state
            state_row = await self._db.fetchone(
                "SELECT state_json FROM strategy_state ORDER BY saved_at DESC LIMIT 1"
            )
            if state_row:
                self._strategy.load_state(json.loads(state_row["state_json"]))
                log.info("strategy.state_restored")

            # Initialize strategy hash so first nightly cycle doesn't trigger unnecessary reload
            self._scan_state["strategy_hash"] = get_code_hash(get_strategy_path())

            # Track current deployed version for trade recording
            version_row = await self._db.fetchone(
                "SELECT version FROM strategy_versions WHERE deployed_at IS NOT NULL ORDER BY deployed_at DESC LIMIT 1"
            )
            self._scan_state["strategy_version"] = version_row["version"] if version_row else None
            await self._activity.strategy(
                f"Strategy v{version_row['version'] if version_row else '?'} loaded")
        else:
            log.error("brain.paused_mode", reason="Strategy failed to load from all sources")
            self._scan_state["paused"] = True
            await self._activity.strategy(
                "Strategy load FAILED — paused mode", severity="error")

        # 4b. Analysis module health check (L5)
        from src.statistics.loader import MODULE_FILES, get_module_path
        for name in MODULE_FILES:
            if not get_module_path(name).exists():
                log.warning("analysis.module_missing", module=name)

        # 5. AI client
        self._ai = AIClient(self._config.ai, self._db)
        if self._config.ai.anthropic_api_key or self._config.ai.vertex_project_id:
            try:
                await self._ai.initialize()
            except Exception as e:
                # System can still trade without AI — orchestration will be unavailable
                log.error("ai.init_failed", error=str(e),
                          note="Nightly orchestration will be unavailable")

        # 6. Reporter + Notifier + Telegram
        self._reporter = Reporter(self._db)
        self._notifier = Notifier(
            self._config.telegram.chat_id,
            tg_filter=self._config.telegram.notifications,
        )
        self._notifier.set_activity_logger(self._activity)
        self._commands = BotCommands(
            config=self._config,
            db=self._db,
            scan_state=self._scan_state,
            portfolio_tracker=self._portfolio,
            risk_manager=self._risk,
            ai_client=self._ai,
            reporter=self._reporter,
            notifier=self._notifier,
            activity_logger=self._activity,
        )
        self._telegram = TelegramBot(self._config.telegram, self._commands)
        await self._telegram.start()
        if self._telegram.app:
            self._notifier.set_app(self._telegram.app)

        # 7. Candidate Manager
        self._candidate_manager = CandidateManager(self._config, self._db)
        await self._candidate_manager.initialize()

        # 7b. Orchestrator (after notifier and candidate manager)
        self._orchestrator = Orchestrator(
            self._config, self._db, self._ai, self._reporter, self._data_store,
            notifier=self._notifier,
            candidate_manager=self._candidate_manager,
        )
        self._orchestrator.set_close_all_callback(self._close_all_positions_for_promotion)
        self._orchestrator.set_scan_state(self._scan_state)

        self._commands.set_orchestrator(self._orchestrator)
        self._commands.set_candidate_manager(self._candidate_manager)

        # 8. WebSocket
        self._ws = KrakenWebSocket(self._config.kraken.ws_url, self._config.symbols)
        self._ws.set_on_failure(self._on_ws_failure)

        # 8b. API Server
        if self._config.api.enabled:
            api_app, ws_manager, activity_ws = create_api_app(
                config=self._config,
                db=self._db,
                portfolio=self._portfolio,
                risk=self._risk,
                ai=self._ai,
                scan_state=self._scan_state,
                commands=self._commands,
                activity_logger=self._activity,
                candidate_manager=self._candidate_manager,
            )
            self._notifier.set_ws_manager(ws_manager)
            self._activity.set_ws_manager(activity_ws)
            self._api_runner = web.AppRunner(api_app)
            await self._api_runner.setup()
            site = web.TCPSite(
                self._api_runner, self._config.api.host, self._config.api.port,
            )
            await site.start()
            log.info("api.started", host=self._config.api.host, port=self._config.api.port)

        # 9. Scheduler (use configured timezone for all cron jobs)
        self._scheduler = AsyncIOScheduler(timezone=self._config.timezone)
        self._setup_jobs()
        self._scheduler.start()

        # 10. Portfolio peak tracking — refresh prices first to avoid stale baseline
        startup_prices = {}
        for symbol in self._config.symbols:
            try:
                ticker = await self._kraken.get_ticker(symbol)
                startup_prices[symbol] = float(ticker["c"][0])
            except Exception as e:
                log.warning("startup.price_refresh_failed", symbol=symbol, error=str(e))
        if startup_prices:
            self._portfolio.refresh_prices(startup_prices)
        portfolio_value = await self._portfolio.total_value()
        self._risk.update_portfolio_peak(portfolio_value)

        # 11. Notify
        await self._notifier.system_online(portfolio_value, self._portfolio.position_count)

        # Startup alerts (L2 halt, L3 orphans)
        if self._risk.is_halted:
            await self._notifier.system_error(f"System started HALTED: {self._risk.halt_reason}")
        if self._scan_state.get("orphaned_positions"):
            await self._notifier.system_error(
                f"WARNING: Unmonitored positions for: {', '.join(self._scan_state['orphaned_positions'])}. "
                "Add symbols to config or close positions.")
        if self._scan_state.get("paused"):
            await self._notifier.system_error(
                "System started in PAUSED mode: strategy failed to load. "
                "Scan loop and position monitor are disabled. Nightly orchestration will attempt to deploy a strategy.")

        self._running = True
        log.info("brain.started", portfolio=f"${portfolio_value:.2f}",
                 positions=self._portfolio.position_count, mode=self._config.mode)

        # Run WebSocket in background (store reference to prevent silent exception loss)
        self._ws_task = asyncio.create_task(self._ws.connect())
        self._ws_task.add_done_callback(self._on_ws_done)

        # Keep alive
        while self._running:
            await asyncio.sleep(1)

            # Check kill switch
            if self._scan_state.get("kill_requested"):
                success = await self._emergency_stop()
                if success:
                    self._scan_state["kill_requested"] = False
                # If failed, flag stays True — retries next iteration

            # Check manual orchestration trigger
            if self._scan_state.get("orchestrate_requested"):
                self._scan_state["orchestrate_requested"] = False
                asyncio.create_task(self._nightly_orchestration())

    def _on_ws_done(self, task: asyncio.Task) -> None:
        """Handle WebSocket task completion — log any unexpected errors."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            log.error("websocket.task_failed", error=str(exc), type=type(exc).__name__)

    def _setup_jobs(self) -> None:
        """Configure all scheduled jobs."""
        paused = self._scan_state.get("paused")
        scan_interval = self._strategy.scan_interval_minutes if self._strategy else 5

        # Strategy scan + position monitor (skip if paused — no strategy loaded)
        if not paused:
            self._scheduler.add_job(
                self._scan_loop, IntervalTrigger(minutes=scan_interval),
                id="scan", name="Strategy Scan",
                next_run_time=datetime.now() + timedelta(seconds=10),
            )
            self._scheduler.add_job(
                self._position_monitor, IntervalTrigger(seconds=30),
                id="position_monitor", name="Position Monitor",
            )
        else:
            log.warning("scheduler.paused_mode", reason="No strategy — scan + monitor disabled")

        # Fee check
        self._scheduler.add_job(
            self._check_fees, IntervalTrigger(hours=self._config.fees.check_interval_hours),
            id="fee_check", name="Fee Check",
            next_run_time=datetime.now() + timedelta(minutes=1),
        )

        # Daily P&L snapshot
        self._scheduler.add_job(
            self._daily_snapshot, CronTrigger(hour=23, minute=59),
            id="daily_snapshot", name="Daily Snapshot",
        )

        # Daily risk reset
        self._scheduler.add_job(
            self._daily_reset, CronTrigger(hour=0, minute=0),
            id="daily_reset", name="Daily Reset",
        )

        # Nightly orchestration
        self._scheduler.add_job(
            self._nightly_orchestration,
            CronTrigger(hour=self._config.orchestrator.start_hour,
                        minute=self._config.orchestrator.start_minute),
            id="orchestration", name="Nightly Orchestration",
        )

        # Weekly report
        self._scheduler.add_job(
            self._weekly_report, CronTrigger(day_of_week="sun", hour=20, minute=0),
            id="weekly_report", name="Weekly Report",
        )

        log.info("scheduler.configured", scan_interval=scan_interval)

    async def _bootstrap_historical_data(self) -> None:
        """Fetch historical candles (5m, 1h, 1d) from Kraken if DB is sparse.

        Runs once on startup. Paginates the OHLC API (720 candles/request).
        """
        # (timeframe_label, kraken_interval_minutes, lookback_days, min_threshold)
        timeframes = [
            ("5m", 5, 30, 8000),      # ~28 days — close to full 30d retention
            ("1h", 60, 365, 8000),     # ~333 days — close to full 1y retention
            ("1d", 1440, 2555, 2000),  # 7 year lookback, skip at ~5.5 years
        ]

        for symbol in self._config.symbols:
            for tf_label, interval, lookback_days, threshold in timeframes:
                count = await self._data_store.get_candle_count(symbol, tf_label)
                if count >= threshold:
                    continue

                log.info("bootstrap.fetching", symbol=symbol, timeframe=tf_label, existing=count)
                since = int((datetime.now() - timedelta(days=lookback_days)).timestamp())
                total = 0

                while True:
                    try:
                        df = await asyncio.wait_for(
                            self._kraken.get_ohlc(symbol, interval=interval, since=since),
                            timeout=30,
                        )
                    except asyncio.TimeoutError:
                        log.warning("bootstrap.fetch_timeout", symbol=symbol, timeframe=tf_label)
                        break
                    except Exception as e:
                        log.warning("bootstrap.fetch_failed", symbol=symbol, timeframe=tf_label, error=str(e))
                        break

                    if df.empty:
                        break

                    stored = await self._data_store.store_candles(symbol, tf_label, df)
                    total += stored

                    # Use last candle timestamp for next page
                    last_ts = int(df.index[-1].timestamp())
                    if last_ts <= since:
                        break  # No progress
                    since = last_ts

                    if len(df) < 720:
                        break  # Last page

                    await asyncio.sleep(1)  # Rate limit

                log.info("bootstrap.complete", symbol=symbol, timeframe=tf_label, candles=total)

    async def _reconcile_orders(self) -> None:
        """Reconcile orders from previous session (live mode only).

        Checks pending/open orders and conditional orders against Kraken.
        """
        # 1. Reconcile regular orders
        pending_orders = await self._db.fetchall(
            "SELECT * FROM orders WHERE status IN ('pending', 'open')"
        )
        for order in pending_orders:
            txid = order["txid"]
            try:
                order_info = await self._kraken.query_order(txid)
                status = order_info.get("status", "")

                if status == "closed":
                    fill_price = float(order_info.get("price", 0))
                    filled_volume = float(order_info.get("vol_exec", 0))
                    fee = float(order_info.get("fee", 0))
                    cost = float(order_info.get("cost", 0))
                    await self._db.execute(
                        """UPDATE orders SET status = 'filled', filled_volume = ?,
                           avg_fill_price = ?, fee = ?, cost = ?, filled_at = ? WHERE txid = ?""",
                        (filled_volume, fill_price, fee, cost, datetime.now(timezone.utc).isoformat(), txid),
                    )
                    log.warning("reconcile.order_filled_while_down", txid=txid,
                                symbol=order["symbol"], fill_price=fill_price)
                    # Process the fill into portfolio/trades
                    purpose = order.get("purpose", "entry")
                    order_tag = order.get("tag")
                    if purpose == "exit" and order_tag:
                        result = await self._portfolio.record_exchange_fill(
                            order_tag, fill_price, filled_volume, fee,
                            close_reason="reconciliation",
                        )
                        if result:
                            if result.get("pnl") is not None:
                                self._risk.record_trade_result(result["pnl"])
                            log.warning("reconcile.exit_fill_processed", tag=order_tag, pnl=result.get("pnl"))
                    elif purpose == "entry" and order_tag and filled_volume > 0:
                        log.warning("reconcile.entry_fill_while_down",
                                    tag=order_tag, symbol=order["symbol"],
                                    note="Entry fill during downtime — position may need manual reconciliation")
                elif status in ("canceled", "expired"):
                    await self._db.execute(
                        "UPDATE orders SET status = ? WHERE txid = ?", (status, txid),
                    )
                    log.info("reconcile.order_ended", txid=txid, status=status)
                else:
                    # Still open — cancel stale orders
                    try:
                        await self._kraken.cancel_order(txid)
                    except Exception:
                        pass
                    await self._db.execute(
                        "UPDATE orders SET status = 'canceled' WHERE txid = ?", (txid,),
                    )
                    log.warning("reconcile.stale_order_canceled", txid=txid, symbol=order["symbol"])
            except Exception as e:
                log.warning("reconcile.query_failed", txid=txid, error=str(e))

        # 2. Reconcile conditional orders
        active_conditionals = await self._db.fetchall(
            "SELECT * FROM conditional_orders WHERE status = 'active'"
        )
        for cond in active_conditionals:
            cond_tag = cond["tag"]
            # Check if position still exists
            pos = await self._db.fetchone(
                "SELECT * FROM positions WHERE tag = ?", (cond_tag,)
            )
            if not pos:
                # Position gone — cancel orphaned conditional orders
                for txid_key in ("sl_txid", "tp_txid"):
                    txid = cond.get(txid_key)
                    if txid:
                        try:
                            await self._kraken.cancel_order(txid)
                        except Exception:
                            pass
                        await self._db.execute(
                            "UPDATE orders SET status = 'canceled' WHERE txid = ?", (txid,),
                        )
                await self._db.execute(
                    "UPDATE conditional_orders SET status = 'canceled', updated_at = ? WHERE tag = ?",
                    (datetime.now(timezone.utc).isoformat(), cond_tag),
                )
                log.warning("reconcile.orphan_conditional_canceled", tag=cond_tag)
                continue

            # Position exists — verify SL/TP orders still live on Kraken
            needs_replace = False
            found_fill = False
            for txid_key in ("sl_txid", "tp_txid"):
                txid = cond.get(txid_key)
                if not txid:
                    continue
                try:
                    order_info = await self._kraken.query_order(txid)
                    status = order_info.get("status", "")
                    if status == "closed":
                        log.warning("reconcile.conditional_filled_while_down",
                                    tag=cond_tag, txid=txid, type=txid_key)
                        found_fill = True
                        # Will be handled by _check_conditional_orders on next cycle
                    elif status in ("canceled", "expired"):
                        log.warning("reconcile.conditional_order_gone",
                                    tag=cond_tag, txid=txid, type=txid_key, status=status)
                        needs_replace = True
                except Exception as e:
                    log.warning("reconcile.conditional_query_failed",
                                tag=cond_tag, txid=txid, error=str(e))

            # Re-place SL/TP if orders expired/canceled but no fill detected
            if needs_replace and not found_fill:
                if pos.get("stop_loss") or pos.get("take_profit"):
                    log.info("reconcile.replacing_conditional_orders", tag=cond_tag)
                    await self._portfolio._cancel_exchange_sl_tp(cond_tag)
                    await self._portfolio._place_exchange_sl_tp(
                        cond_tag, pos["symbol"], pos["qty"],
                        pos.get("stop_loss"), pos.get("take_profit"),
                    )

        await self._db.commit()
        total_checked = len(pending_orders) + len(active_conditionals)
        log.info("reconcile.complete",
                 orders=len(pending_orders), conditionals=len(active_conditionals))
        if self._activity and total_checked > 0:
            await self._activity.system(f"Order reconciliation: {total_checked} checked")

    async def _on_ws_failure(self) -> None:
        """Called when WebSocket permanently fails after max retries."""
        await self._notifier.websocket_failed()

    async def _scan_loop(self) -> None:
        """Main scan loop — fetch data, run strategy, execute signals.

        When halted, still runs strategy to process exit signals (SELL/CLOSE).
        Risk manager allows exits during halt — we must not short-circuit before it.
        """
        if self._commands and self._commands.is_paused:
            return
        halted = self._risk and self._risk.is_halted
        self._scan_count = getattr(self, "_scan_count", 0) + 1

        log.info("scan.start", halted=halted)
        try:
            prices = {}
            markets = {}
            scan_symbols = {}

            for symbol in self._config.symbols:
                try:
                    ticker = await self._kraken.get_ticker(symbol)
                    price = float(ticker["c"][0])  # Last trade price
                    prices[symbol] = price

                    # Fetch fresh 5m candles from Kraken every scan
                    fresh_5m = await self._kraken.get_ohlc(symbol, interval=5)
                    if not fresh_5m.empty:
                        await self._data_store.store_candles(symbol, "5m", fresh_5m)
                    df_5m = await self._data_store.get_candles(symbol, "5m", limit=8640)

                    # Refresh 1h candles every hour (12 scans), 1d every day (288 scans)
                    if self._scan_count % 12 == 1:
                        fresh_1h = await self._kraken.get_ohlc(symbol, interval=60)
                        if not fresh_1h.empty:
                            await self._data_store.store_candles(symbol, "1h", fresh_1h)
                    if self._scan_count % 288 == 1:
                        fresh_1d = await self._kraken.get_ohlc(symbol, interval=1440)
                        if not fresh_1d.empty:
                            await self._data_store.store_candles(symbol, "1d", fresh_1d)

                    df_1h = await self._data_store.get_candles(symbol, "1h", limit=8760)
                    df_1d = await self._data_store.get_candles(symbol, "1d", limit=2555)

                    spread = await self._kraken.get_spread(symbol)
                    vol_24h = float(ticker.get("v", [0, 0])[1])

                    pair_fees = self._pair_fees.get(symbol)
                    if df_1h.empty:
                        log.warning("scan.candle_fallback", symbol=symbol, timeframe="1h", fallback="5m")
                    if df_1d.empty:
                        log.warning("scan.candle_fallback", symbol=symbol, timeframe="1d", fallback="5m")

                    markets[symbol] = SymbolData(
                        symbol=symbol,
                        current_price=price,
                        candles_5m=df_5m,
                        candles_1h=df_1h if not df_1h.empty else df_5m,
                        candles_1d=df_1d if not df_1d.empty else df_5m,
                        spread=spread,
                        volume_24h=vol_24h,
                        maker_fee_pct=pair_fees[0] if pair_fees else self._config.kraken.maker_fee_pct,
                        taker_fee_pct=pair_fees[1] if pair_fees else self._config.kraken.taker_fee_pct,
                    )

                    scan_symbols[symbol] = {
                        "price": price,
                        "spread": spread,
                    }

                    # Store scan results (price + spread for audit trail)
                    await self._db.execute(
                        """INSERT INTO scan_results
                           (timestamp, symbol, price, spread)
                           VALUES (?, ?, ?, ?)""",
                        (datetime.now(timezone.utc).isoformat(), symbol, price, spread),
                    )

                except Exception as e:
                    log.warning("scan.symbol_error", symbol=symbol, error=str(e))

            if not markets:
                return

            # Build portfolio snapshot
            portfolio = await self._portfolio.get_portfolio(prices)
            portfolio_value = portfolio.total_value

            # Run strategy (with timeout to catch infinite loops in AI-rewritten code)
            executor_future = None
            try:
                self._analyzing = True
                loop = asyncio.get_running_loop()
                executor_future = loop.run_in_executor(
                    None, self._strategy.analyze, dict(markets), portfolio, datetime.now()
                )
                signals = await asyncio.wait_for(asyncio.shield(executor_future), timeout=30)
            except asyncio.TimeoutError:
                log.error("scan.strategy_timeout", note="strategy.analyze() took >30s")
                if self._notifier:
                    await self._notifier.system_error("Strategy analyze() timed out (>30s) — possible infinite loop")
                # Thread is still running — clear _analyzing only when it finishes
                if executor_future is not None:
                    async def _wait_for_executor():
                        try:
                            await executor_future
                        except Exception:
                            pass
                        finally:
                            self._analyzing = False
                    asyncio.create_task(_wait_for_executor())
                else:
                    self._analyzing = False
                return
            finally:
                # Only clear if we didn't timeout (timeout path handles it via task)
                if executor_future is not None and executor_future.done():
                    self._analyzing = False

            # Process signals
            executed_symbols = set()
            halt_notified = False
            for signal in signals:
                # Risk check (refresh portfolio_value after each execution for TOCTOU safety)
                is_new = not signal.tag or signal.tag not in self._portfolio._positions
                check = self._risk.check_signal(
                    signal, portfolio_value, self._portfolio.position_count,
                    self._portfolio.get_position_value(signal.symbol),
                    daily_start_value=self._portfolio.daily_start_value,
                    is_new_position=is_new,
                )

                if not check.passed:
                    log.info("scan.signal_rejected", symbol=signal.symbol, reason=check.reason)
                    await self._notifier.signal_rejected(
                        signal.symbol, signal.action.value, check.reason,
                    )
                    if self._risk.is_halted and not halt_notified:
                        await self._notifier.risk_halt(self._risk.halt_reason)
                        halt_notified = True
                    await self._db.execute(
                        "INSERT INTO signals (symbol, action, size_pct, confidence, intent, reasoning, strategy_regime, tag, rejected_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (signal.symbol, signal.action.value, signal.size_pct, signal.confidence,
                         signal.intent.value, signal.reasoning, None, signal.tag, check.reason),
                    )
                    continue

                # Clamp to risk limits
                signal = self._risk.clamp_signal(signal, portfolio_value)

                # Execute (use per-pair fees if available)
                price = prices.get(signal.symbol, 0)
                if price <= 0:
                    log.warning("scan.invalid_price", symbol=signal.symbol, price=price)
                    await self._db.execute(
                        "INSERT INTO signals (symbol, action, size_pct, confidence, intent, reasoning, strategy_regime, tag, acted_on, rejected_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'invalid_price')",
                        (signal.symbol, signal.action.value, signal.size_pct, signal.confidence,
                         signal.intent.value, signal.reasoning, None, signal.tag),
                    )
                    continue
                sym_fees = self._pair_fees.get(signal.symbol)
                async with self._trade_lock:
                    raw_result = await self._portfolio.execute_signal(
                        signal, price,
                        sym_fees[0] if sym_fees else self._config.kraken.maker_fee_pct,
                        sym_fees[1] if sym_fees else self._config.kraken.taker_fee_pct,
                        strategy_regime=None,
                        strategy_version=self._scan_state.get("strategy_version"),
                    )

                # Normalize to list (multi-close returns list)
                if raw_result is None:
                    results = []
                elif isinstance(raw_result, list):
                    results = raw_result
                else:
                    results = [raw_result]

                # Record failed signals in audit trail
                if not results:
                    await self._db.execute(
                        "INSERT INTO signals (symbol, action, size_pct, confidence, intent, reasoning, strategy_regime, tag, acted_on, rejected_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'execution_failed')",
                        (signal.symbol, signal.action.value, signal.size_pct, signal.confidence,
                         signal.intent.value, signal.reasoning, None, signal.tag),
                    )

                if results:
                    executed_symbols.add(signal.symbol)
                    # Record signal
                    if len(results) == 1:
                        result_tag = results[0].get("tag")
                    else:
                        # Multi-close: join all tags for audit trail
                        result_tag = ",".join(r.get("tag", "") for r in results if r.get("tag"))
                    await self._db.execute(
                        "INSERT INTO signals (symbol, action, size_pct, confidence, intent, reasoning, strategy_regime, tag, acted_on) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
                        (signal.symbol, signal.action.value, signal.size_pct, signal.confidence,
                         signal.intent.value, signal.reasoning, None, result_tag),
                    )

                    for result in results:
                        # Track P&L
                        if result.get("pnl") is not None:
                            self._risk.record_trade_result(result["pnl"])
                            try:
                                self._strategy.on_position_closed(
                                    result["symbol"], result["pnl"], result.get("pnl_pct", 0),
                                    tag=result.get("tag", ""),
                                )
                            except (TypeError, RuntimeError):
                                try:
                                    self._strategy.on_position_closed(
                                        result["symbol"], result["pnl"], result.get("pnl_pct", 0),
                                    )
                                except (TypeError, RuntimeError):
                                    pass

                        try:
                            self._strategy.on_fill(
                                signal.symbol, signal.action, result["qty"], result["price"],
                                signal.intent, tag=result.get("tag", ""),
                            )
                        except (TypeError, RuntimeError):
                            try:
                                self._strategy.on_fill(
                                    signal.symbol, signal.action, result["qty"], result["price"],
                                    signal.intent,
                                )
                            except (TypeError, RuntimeError):
                                pass

                        # Notify
                        await self._notifier.trade_executed(result)

                    # Check rollback triggers (once after all results)
                    new_value = await self._portfolio.total_value()
                    rollback = self._risk.check_rollback_triggers(
                        new_value, self._portfolio.daily_start_value
                    )
                    if not rollback.passed:
                        await self._notifier.rollback_alert(rollback.reason, "previous")
                        await self._notifier.risk_halt(rollback.reason)
                        log.warning("scan.rollback_triggered", reason=rollback.reason)

                    # Update portfolio peak
                    self._risk.update_portfolio_peak(new_value)

                    # Refresh portfolio_value for next signal's risk check (TOCTOU)
                    portfolio_value = new_value

                    # Update scan_state with signal info
                    if signal.symbol in scan_symbols:
                        scan_symbols[signal.symbol]["signal"] = {
                            "action": signal.action.value,
                            "confidence": signal.confidence,
                            "reasoning": signal.reasoning,
                        }

            # Update scan_results with signal info for symbols that had executed signals
            for symbol in executed_symbols:
                sig_data = scan_symbols.get(symbol, {}).get("signal")
                if sig_data:
                    await self._db.execute(
                        """UPDATE scan_results SET signal_generated = 1, signal_action = ?, signal_confidence = ?
                           WHERE symbol = ? AND id = (SELECT MAX(id) FROM scan_results WHERE symbol = ?)""",
                        (sig_data["action"], sig_data["confidence"], symbol, symbol),
                    )

            await self._db.commit()

            # Update scan state
            self._scan_state["symbols"] = scan_symbols
            self._scan_state["last_scan"] = datetime.now().strftime("%H:%M:%S")
            self._scan_state["last_scan_at"] = datetime.now(timezone.utc)
            log.info("scan.complete", symbols=len(scan_symbols), signals=len(signals))
            await self._notifier.scan_complete(len(scan_symbols), len(signals))

            # Save strategy state periodically (keep last 10)
            state = self._strategy.get_state()
            await self._db.execute(
                "INSERT INTO strategy_state (state_json) VALUES (?)",
                (json.dumps(state, default=str),),
            )
            await self._db.execute(
                """DELETE FROM strategy_state WHERE id NOT IN (
                    SELECT id FROM strategy_state ORDER BY saved_at DESC LIMIT 10
                )"""
            )
            await self._db.commit()

            # Run candidate strategies (paper simulation alongside active strategy)
            if self._candidate_manager and self._candidate_manager.get_active_slots():
                try:
                    await self._candidate_manager.run_scans(markets, datetime.now(timezone.utc))
                    await self._candidate_manager.persist_state()
                except Exception as e:
                    log.error("scan.candidate_error", error=str(e))

        except Exception as e:
            import traceback
            log.error("scan.failed", error=str(e), traceback=traceback.format_exc())
            if self._notifier:
                await self._notifier.system_error(f"Scan loop failed: {e}")

    async def _position_monitor(self) -> None:
        """Check stop-loss and take-profit on open positions.

        Dual-path: Live mode checks exchange-native orders first,
        then falls back to client-side SL/TP. Paper mode uses client-side only.
        """
        if not self._ws:
            return

        prices = dict(self._ws.prices)
        # Check staleness per-symbol — fall back to REST for stale prices (>5 min)
        stale_symbols = [s for s in self._config.symbols if self._ws.price_age(s) > 300]
        if not prices or stale_symbols:
            fetch_symbols = stale_symbols if prices else self._config.symbols
            for symbol in fetch_symbols:
                try:
                    ticker = await self._kraken.get_ticker(symbol)
                    prices[symbol] = float(ticker["c"][0])
                except Exception as e:
                    log.warning("position_monitor.price_fetch_failed", symbol=symbol, error=str(e))

        if not prices and self._portfolio.position_count > 0:
            log.error("position_monitor.no_prices", positions=self._portfolio.position_count,
                      note="SL/TP checks skipped — no price data available")
            if self._notifier:
                await self._notifier.system_error(
                    f"Position monitor: no prices available. {self._portfolio.position_count} open positions not monitored."
                )
            return

        # Live mode: check exchange-native conditional orders for fills
        if not self._config.is_paper():
            async with self._trade_lock:
                await self._check_conditional_orders()

        # All modes: client-side SL/TP checking (primary for paper, fallback for live)
        triggered = await self._portfolio.update_prices(prices)

        # In live mode, skip client-side triggers for positions with active exchange orders
        if not self._config.is_paper():
            active_tags = set()
            active_conds = await self._db.fetchall(
                "SELECT tag FROM conditional_orders WHERE status = 'active'"
            )
            for c in active_conds:
                active_tags.add(c["tag"])
            triggered = [t for t in triggered if t["tag"] not in active_tags]

        for t in triggered:
            async with self._trade_lock:
                await self._handle_sl_tp_trigger(t)

        # Check candidate SL/TP (paper simulation — no exchange orders)
        if self._candidate_manager and self._candidate_manager.get_active_slots():
            try:
                await self._candidate_manager.check_sl_tp(prices)
                await self._candidate_manager.persist_state()
            except Exception as e:
                log.error("position_monitor.candidate_error", error=str(e))

    async def _handle_sl_tp_trigger(self, t: dict) -> None:
        """Process a single SL/TP trigger — shared between client-side and exchange-fill paths."""
        symbol = t["symbol"]
        tag = t["tag"]
        reason = t["reason"]
        price = t["price"]

        # Use the position's actual intent (not default DAY) for accurate callbacks
        pos = self._portfolio._positions.get(tag) if tag else None
        try:
            pos_intent = Intent[pos.get("intent", "DAY")] if pos else Intent.DAY
        except KeyError:
            pos_intent = Intent.DAY

        signal = Signal(
            symbol=symbol, action=Action.CLOSE, size_pct=1.0,
            intent=pos_intent, confidence=1.0,
            reasoning=f"{reason} triggered at ${price:.2f}",
            tag=tag,
        )
        sym_fees = self._pair_fees.get(symbol)
        result = await self._portfolio.execute_signal(
            signal, price,
            sym_fees[0] if sym_fees else self._config.kraken.maker_fee_pct,
            sym_fees[1] if sym_fees else self._config.kraken.taker_fee_pct,
            strategy_regime=None,
            close_reason=reason,
        )
        if result:
            results = result if isinstance(result, list) else [result]
            for r in results:
                if r.get("pnl") is not None:
                    self._risk.record_trade_result(r["pnl"])
                await self._notifier.stop_triggered(symbol, reason, price, tag=tag)
                await self._notifier.trade_executed(r)

                # Strategy callbacks (skip if analyze() is running in executor to avoid thread-safety issues)
                if not self._analyzing and self._strategy:
                    if r.get("pnl") is not None:
                        try:
                            self._strategy.on_position_closed(
                                r["symbol"], r["pnl"], r.get("pnl_pct", 0),
                                tag=r.get("tag", ""),
                            )
                        except (TypeError, RuntimeError):
                            try:
                                self._strategy.on_position_closed(
                                    r["symbol"], r["pnl"], r.get("pnl_pct", 0),
                                )
                            except (TypeError, RuntimeError):
                                pass
                    try:
                        self._strategy.on_fill(
                            symbol, Action.CLOSE, r["qty"], r["price"], pos_intent,
                            tag=r.get("tag", ""),
                        )
                    except (TypeError, RuntimeError):
                        try:
                            self._strategy.on_fill(
                                symbol, Action.CLOSE, r["qty"], r["price"], pos_intent,
                            )
                        except (TypeError, RuntimeError):
                            pass

            # Rollback triggers + peak update (once after all results)
            new_value = await self._portfolio.total_value()
            rollback = self._risk.check_rollback_triggers(
                new_value, self._portfolio.daily_start_value
            )
            if not rollback.passed:
                await self._notifier.rollback_alert(rollback.reason, "previous")
                await self._notifier.risk_halt(rollback.reason)
            self._risk.update_portfolio_peak(new_value)

    async def _check_conditional_orders(self) -> None:
        """Poll Kraken for SL/TP order fills (live mode only)."""
        active_conds = await self._db.fetchall(
            "SELECT * FROM conditional_orders WHERE status = 'active'"
        )
        for cond in active_conds:
            tag = cond["tag"]
            symbol = cond["symbol"]

            for txid_key, reason, other_key in [
                ("sl_txid", "stop_loss", "tp_txid"),
                ("tp_txid", "take_profit", "sl_txid"),
            ]:
                txid = cond.get(txid_key)
                if not txid:
                    continue

                try:
                    order_info = await self._kraken.query_order(txid)
                except Exception as e:
                    log.warning("check_conditional.query_failed", tag=tag, txid=txid, error=str(e))
                    continue

                if order_info.get("status") != "closed":
                    continue

                # This SL or TP order filled on Kraken
                fill_price = float(order_info.get("price", 0))
                filled_volume = float(order_info.get("vol_exec", 0))
                fee = float(order_info.get("fee", 0))

                log.info("check_conditional.filled", tag=tag, reason=reason,
                         fill_price=fill_price, volume=filled_volume)

                # Update order record
                now = datetime.now(timezone.utc).isoformat()
                await self._db.execute(
                    """UPDATE orders SET status = 'filled', filled_volume = ?,
                       avg_fill_price = ?, fee = ?, filled_at = ? WHERE txid = ?""",
                    (filled_volume, fill_price, fee, now, txid),
                )

                # Cancel the other order
                other_txid = cond.get(other_key)
                if other_txid:
                    try:
                        await self._kraken.cancel_order(other_txid)
                    except Exception:
                        pass
                    await self._db.execute(
                        "UPDATE orders SET status = 'canceled' WHERE txid = ?", (other_txid,),
                    )

                # Update conditional_orders status
                fill_status = f"filled_{reason}"
                await self._db.execute(
                    "UPDATE conditional_orders SET status = ?, updated_at = ? WHERE tag = ?",
                    (fill_status, now, tag),
                )
                await self._db.commit()

                # Record trade via PortfolioTracker (handles full + partial fills)
                result = await self._portfolio.record_exchange_fill(
                    tag, fill_price, filled_volume, fee,
                    close_reason=reason,
                )
                if result:
                    self._risk.record_trade_result(result["pnl"])
                    await self._notifier.stop_triggered(symbol, reason, fill_price, tag=tag)
                    await self._notifier.trade_executed(result)

                    # Strategy callbacks (skip if analyze() in executor — thread-safety)
                    if not self._analyzing and self._strategy:
                        try:
                            actual_intent = Intent[result.get("intent", "DAY")]
                        except KeyError:
                            actual_intent = Intent.DAY
                        try:
                            self._strategy.on_position_closed(
                                result["symbol"], result["pnl"], result["pnl_pct"], tag=tag,
                            )
                        except (TypeError, RuntimeError):
                            try:
                                self._strategy.on_position_closed(
                                    result["symbol"], result["pnl"], result["pnl_pct"],
                                )
                            except (TypeError, RuntimeError):
                                pass
                        try:
                            self._strategy.on_fill(
                                symbol, Action.CLOSE, result["qty"], result["price"],
                                actual_intent, tag=tag,
                            )
                        except (TypeError, RuntimeError):
                            try:
                                self._strategy.on_fill(
                                    symbol, Action.CLOSE, result["qty"], result["price"],
                                    actual_intent,
                                )
                            except (TypeError, RuntimeError):
                                pass

                    # Rollback triggers + peak update
                    new_value = await self._portfolio.total_value()
                    rollback = self._risk.check_rollback_triggers(
                        new_value, self._portfolio.daily_start_value
                    )
                    if not rollback.passed:
                        await self._notifier.rollback_alert(rollback.reason, "previous")
                        await self._notifier.risk_halt(rollback.reason)
                    self._risk.update_portfolio_peak(new_value)

                break  # Only one of SL/TP can fill

    async def _check_fees(self) -> None:
        """Update fee schedule from Kraken for all pairs."""
        if not self._config.kraken.api_key:
            return
        try:
            for symbol in self._config.symbols:
                maker, taker = await self._kraken.get_fee_schedule(symbol)
                self._pair_fees[symbol] = (maker, taker)
                await self._db.execute(
                    "DELETE FROM fee_schedule WHERE symbol = ?", (symbol,)
                )
                await self._db.execute(
                    "INSERT INTO fee_schedule (symbol, maker_fee_pct, taker_fee_pct) VALUES (?, ?, ?)",
                    (symbol, maker, taker),
                )
            await self._db.commit()
            log.info("fees.updated", pairs=len(self._pair_fees))
            if self._activity:
                await self._activity.system(f"Fee schedule updated for {len(self._pair_fees)} pairs")
        except Exception as e:
            log.warning("fees.check_failed", error=str(e))

    async def _daily_snapshot(self) -> None:
        await self._portfolio.snapshot_daily()
        if self._activity:
            await self._activity.system("Daily performance snapshot saved")

    async def _daily_reset(self) -> None:
        async with self._trade_lock:
            self._risk.reset_daily()
            self._portfolio.reset_daily()
            self._ai.reset_daily_tokens()
            # Refresh daily start value for accurate daily P&L tracking
            self._portfolio._daily_start_value = await self._portfolio.total_value()
        if self._activity:
            await self._activity.system("Daily reset: counters cleared")

    async def _nightly_orchestration(self) -> None:
        """Run the nightly AI review cycle with timeout enforcement."""
        # Enforce end_hour window (e.g., 3 hours from start_hour to end_hour)
        window_hours = self._config.orchestrator.end_hour - self._config.orchestrator.start_hour
        if window_hours <= 0:
            window_hours += 24  # Handle wrap-around (e.g., start=23, end=2)
        timeout_seconds = window_hours * 3600

        try:
            report = await asyncio.wait_for(
                self._orchestrator.run_nightly_cycle(), timeout=timeout_seconds,
            )

            # Reload strategy if it was changed
            new_hash = get_code_hash(get_strategy_path())
            if self._scan_state.get("strategy_hash") != new_hash:
                self._strategy = load_strategy()
                risk_limits = RiskLimits(
                    max_trade_pct=self._config.risk.max_trade_pct,
                    default_trade_pct=self._config.risk.default_trade_pct,
                    max_positions=self._config.risk.max_positions,
                    max_daily_loss_pct=self._config.risk.max_daily_loss_pct,
                    max_drawdown_pct=self._config.risk.max_drawdown_pct,
                    max_position_pct=self._config.risk.max_position_pct,
                    max_daily_trades=self._config.risk.max_daily_trades,
                    rollback_consecutive_losses=self._config.risk.rollback_consecutive_losses,
                )
                self._strategy.initialize(risk_limits, self._config.symbols)
                # Attempt to restore strategy state after hot-reload
                try:
                    state_row = await self._db.fetchone(
                        "SELECT state_json FROM strategy_state ORDER BY saved_at DESC LIMIT 1"
                    )
                    if state_row:
                        self._strategy.load_state(json.loads(state_row["state_json"]))
                except Exception:
                    pass  # New version may not accept old state — that's OK
                self._scan_state["strategy_hash"] = new_hash

                # Update strategy version for trade recording
                version_row = await self._db.fetchone(
                    "SELECT version FROM strategy_versions WHERE deployed_at IS NOT NULL ORDER BY deployed_at DESC LIMIT 1"
                )
                self._scan_state["strategy_version"] = version_row["version"] if version_row else None

                # Update scan interval if strategy changed it
                new_interval = self._strategy.scan_interval_minutes
                try:
                    job = self._scheduler.get_job("scan")
                    if job:
                        self._scheduler.reschedule_job(
                            "scan", trigger=IntervalTrigger(minutes=new_interval)
                        )
                        log.info("strategy.scan_interval_updated", minutes=new_interval)
                except Exception:
                    pass  # Scheduler job may not exist yet
                log.info("strategy.reloaded_after_orchestration")
                if self._activity:
                    ver = self._scan_state.get("strategy_version", "?")
                    await self._activity.strategy(f"Strategy reloaded: v{ver}")

            await self._notifier.daily_summary(report)
        except asyncio.TimeoutError:
            log.error("orchestration.timeout", window_hours=window_hours)
            await self._notifier.system_error(
                f"Orchestration timed out after {window_hours}h window"
            )
        except Exception as e:
            log.error("orchestration.failed", error=str(e))
            # Orchestrator already sends system_error before re-raising

    async def _weekly_report(self) -> None:
        try:
            report = await self._reporter.weekly_report()
            await self._notifier.weekly_report(report)
        except Exception as e:
            log.error("weekly_report.failed", error=str(e))

    async def _emergency_stop(self) -> bool:
        """Close all positions immediately. Returns True if all positions closed."""
        log.warning("brain.emergency_stop")
        if self._activity:
            await self._activity.system("Emergency stop initiated", severity="error")
        await self._notifier.system_error("Emergency stop initiated — closing all positions")

        # Pause position monitor AND scan loop to avoid concurrent trades during emergency
        if self._scheduler:
            for job_id in ("position_monitor", "scan"):
                try:
                    self._scheduler.pause_job(job_id)
                except Exception:
                    pass

        # Cancel all exchange-native conditional orders first
        if not self._config.is_paper():
            active_conds = await self._db.fetchall(
                "SELECT * FROM conditional_orders WHERE status = 'active'"
            )
            for cond in active_conds:
                for txid_key in ("sl_txid", "tp_txid"):
                    txid = cond.get(txid_key)
                    if txid:
                        try:
                            await self._kraken.cancel_order(txid)
                        except Exception:
                            pass
                        await self._db.execute(
                            "UPDATE orders SET status = 'canceled' WHERE txid = ?", (txid,),
                        )
                await self._db.execute(
                    "UPDATE conditional_orders SET status = 'canceled', updated_at = ? WHERE tag = ?",
                    (datetime.now(timezone.utc).isoformat(), cond["tag"]),
                )
            await self._db.commit()

        positions = await self._db.fetchall("SELECT * FROM positions")
        for pos in positions:
            for attempt in range(3):
                try:
                    ticker = await self._kraken.get_ticker(pos["symbol"])
                    price = float(ticker["c"][0])
                    signal = Signal(
                        symbol=pos["symbol"], action=Action.CLOSE, size_pct=1.0,
                        intent=Intent.DAY, confidence=1.0, reasoning="Emergency stop",
                        tag=pos.get("tag"),
                    )
                    sym_fees = self._pair_fees.get(pos["symbol"])
                    async with self._trade_lock:
                        result = await self._portfolio.execute_signal(
                            signal, price,
                            sym_fees[0] if sym_fees else self._config.kraken.maker_fee_pct,
                            sym_fees[1] if sym_fees else self._config.kraken.taker_fee_pct,
                            close_reason="emergency",
                        )
                    # Update risk counters so daily loss limit reflects emergency closes
                    if result:
                        results = result if isinstance(result, list) else [result]
                        for r in results:
                            if r.get("pnl") is not None:
                                self._risk.record_trade_result(r["pnl"])
                                await self._notifier.trade_executed(r)
                    break  # Success
                except Exception as e:
                    log.error("emergency.close_failed", symbol=pos["symbol"],
                              attempt=attempt + 1, error=str(e))
                    if attempt < 2:
                        await asyncio.sleep(2)

        # Verify all positions were closed
        remaining = await self._db.fetchall("SELECT symbol, tag FROM positions")

        # Check if any remaining positions were closed by exchange SL/TP during stop
        if remaining and not self._config.is_paper():
            for r in list(remaining):
                tag = r.get("tag")
                if not tag:
                    continue
                orders = await self._db.fetchall(
                    "SELECT txid FROM orders WHERE tag = ? AND purpose IN ('stop_loss', 'take_profit')",
                    (tag,),
                )
                for order in orders:
                    try:
                        info = await self._kraken.query_order(order["txid"])
                        if info.get("status") == "closed":
                            fill_price = float(info.get("price", 0))
                            filled_volume = float(info.get("vol_exec", 0))
                            fee_val = float(info.get("fee", 0))
                            result = await self._portfolio.record_exchange_fill(
                                tag, fill_price, filled_volume, fee_val,
                                close_reason="emergency",
                            )
                            if result:
                                log.info("emergency.conditional_filled_during_stop",
                                         tag=tag, price=fill_price)
                                remaining = [x for x in remaining if x.get("tag") != tag]
                            break
                    except Exception:
                        pass

        # Resume position monitor and scan loop
        if self._scheduler:
            for job_id in ("position_monitor", "scan"):
                try:
                    self._scheduler.resume_job(job_id)
                except Exception:
                    pass

        if remaining:
            symbols = [r["symbol"] for r in remaining]
            log.error("emergency.positions_remaining", symbols=symbols)
            if self._activity:
                await self._activity.system(
                    f"Emergency stop incomplete: {', '.join(symbols)} remaining", severity="error")
            await self._notifier.system_error(
                f"Emergency stop incomplete — positions remaining: {', '.join(symbols)}"
            )
            return False
        else:
            log.info("emergency.all_positions_closed")
            if self._activity:
                await self._activity.system("Emergency stop complete", severity="error")
            await self._notifier.system_error("Emergency stop complete — all positions closed")
            return True

    async def _close_all_positions_for_promotion(self) -> None:
        """Close all fund positions for strategy promotion (clean slate)."""
        positions = list(self._portfolio.positions.items())
        if not positions:
            return
        for tag, pos in positions:
            try:
                ticker = await self._kraken.get_ticker(pos["symbol"])
                price = float(ticker["c"][0])
            except Exception:
                price = pos.get("current_price", pos["avg_entry"])
            signal = Signal(
                symbol=pos["symbol"], action=Action.CLOSE, size_pct=1.0,
                intent=Intent.DAY, confidence=1.0,
                reasoning="Positions closed for strategy promotion", tag=tag,
            )
            sym_fees = self._pair_fees.get(pos["symbol"])
            async with self._trade_lock:
                result = await self._portfolio.execute_signal(
                    signal, price,
                    sym_fees[0] if sym_fees else self._config.kraken.maker_fee_pct,
                    sym_fees[1] if sym_fees else self._config.kraken.taker_fee_pct,
                    close_reason="promotion",
                )
                if result:
                    results = result if isinstance(result, list) else [result]
                    for r in results:
                        if r.get("pnl") is not None:
                            self._risk.record_trade_result(r["pnl"])
                        await self._notifier.trade_executed(r)
        log.info("promotion.all_positions_closed", count=len(positions))

    async def stop(self) -> None:
        """Graceful shutdown sequence."""
        log.info("brain.stopping")
        self._running = False

        if self._notifier:
            await self._notifier.system_shutdown()

        # 1. Stop scheduler
        if self._scheduler:
            self._scheduler.shutdown(wait=False)

        # 2. Save strategy state
        if self._strategy:
            state = self._strategy.get_state()
            await self._db.execute(
                "INSERT INTO strategy_state (state_json) VALUES (?)",
                (json.dumps(state, default=str),),
            )
            await self._db.commit()
            log.info("strategy.state_saved")

        # 3. Cancel unfilled orders (live mode)
        if not self._config.is_paper() and self._kraken:
            # Cancel conditional orders individually first (more reliable than CancelAll)
            try:
                active_conds = await self._db.fetchall(
                    "SELECT * FROM conditional_orders WHERE status = 'active'"
                )
                for cond in active_conds:
                    for txid_key in ("sl_txid", "tp_txid"):
                        txid = cond.get(txid_key)
                        if txid:
                            try:
                                await self._kraken.cancel_order(txid)
                            except Exception:
                                pass
            except Exception as e:
                log.warning("shutdown.cancel_conditionals_failed", error=str(e))
            try:
                await self._kraken.cancel_all_orders()
            except Exception as e:
                log.warning("shutdown.cancel_orders_failed", error=str(e))

            # Mark all active conditional orders as canceled in DB
            try:
                await self._db.execute(
                    "UPDATE conditional_orders SET status = 'canceled', updated_at = ? WHERE status = 'active'",
                    (datetime.now(timezone.utc).isoformat(),),
                )
                await self._db.execute(
                    "UPDATE orders SET status = 'canceled' WHERE status IN ('pending', 'open')",
                )
                await self._db.commit()
            except Exception as e:
                log.warning("shutdown.db_cleanup_failed", error=str(e))

        # 4. Stop API server
        if self._api_runner:
            await self._api_runner.cleanup()

        # 5. Stop WebSocket
        if self._ws:
            await self._ws.stop()

        # 5b. Stop Telegram
        if self._telegram:
            await self._telegram.stop()

        # 6. Close Kraken REST
        if self._kraken:
            await self._kraken.close()

        # 7. Close database
        if self._db:
            await self._db.close()

        log.info("brain.stopped")


LOCK_FILE = Path(__file__).resolve().parent.parent / "data" / "brain.pid"


def _acquire_lock() -> None:
    """Ensure only one instance runs. Write PID to lockfile."""
    current_pid = os.getpid()
    if LOCK_FILE.exists():
        try:
            old_pid = int(LOCK_FILE.read_text().strip())
        except (ValueError, OSError):
            log.warning("lockfile.corrupt")
            LOCK_FILE.unlink(missing_ok=True)
            old_pid = None

        if old_pid is not None:
            if old_pid == current_pid:
                # Container restart — same PID (typically 1), stale lock
                log.warning("lockfile.stale_container_restart", old_pid=old_pid)
            else:
                try:
                    os.kill(old_pid, 0)  # signal 0 = just check existence
                    print(f"ERROR: Another instance is running (PID {old_pid}). Exiting.", file=sys.stderr)
                    sys.exit(1)
                except (ProcessLookupError, PermissionError):
                    # Stale lockfile — previous process died without cleanup
                    log.warning("lockfile.stale", old_pid=old_pid)

    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(str(current_pid))


def _release_lock() -> None:
    """Remove PID lockfile on exit."""
    try:
        if LOCK_FILE.exists() and LOCK_FILE.read_text().strip() == str(os.getpid()):
            LOCK_FILE.unlink()
    except OSError:
        pass


async def main() -> None:
    _acquire_lock()

    brain = TradingBrain()

    # Handle SIGTERM/SIGINT for graceful shutdown
    loop = asyncio.get_running_loop()

    _stop_task = None

    def signal_handler():
        nonlocal _stop_task
        if _stop_task is None:
            _stop_task = asyncio.create_task(brain.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)

    try:
        await brain.start()
    except KeyboardInterrupt:
        pass
    finally:
        if brain._running:
            await brain.stop()
        _release_lock()


def run() -> None:
    """Entry point for pyproject.toml script."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
