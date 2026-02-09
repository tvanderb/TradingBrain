"""Trading Brain v2 — IO-Container Architecture

Main entry point. Wires all components, manages lifecycle, runs the scan loop.

Startup: load config -> connect DB -> load strategy -> connect Kraken -> start Telegram -> start scheduler
Shutdown: stop scheduler -> save strategy state -> cancel orders -> stop WS -> stop Telegram -> close DB
"""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import signal
import sys
from datetime import datetime, timedelta
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
from src.strategy.loader import load_strategy, get_strategy_path, get_code_hash
from src.telegram.bot import TelegramBot
from src.telegram.commands import BotCommands
from src.telegram.notifications import Notifier
from src.utils.logging import setup_logging
from strategy.skills import compute_indicators

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
        self._running = False

    async def start(self) -> None:
        """Full startup sequence."""
        log.info("brain.starting")

        # 1. Config
        self._config = load_config()
        setup_logging(self._config.log_level)
        log.info("config.loaded", mode=self._config.mode, symbols=self._config.symbols)

        # 2. Database
        self._db = Database(self._config.db_path)
        await self._db.connect()

        # 3. Shell components
        self._kraken = KrakenREST(self._config.kraken)
        self._risk = RiskManager(self._config.risk)
        self._portfolio = PortfolioTracker(self._config, self._db, self._kraken)
        self._data_store = DataStore(self._db, self._config.data)
        await self._portfolio.initialize()

        # 3b. Bootstrap historical data if DB is sparse
        await self._bootstrap_historical_data()

        # 4. Strategy
        try:
            self._strategy = load_strategy()
            risk_limits = RiskLimits(
                max_trade_pct=self._config.risk.max_trade_pct,
                default_trade_pct=self._config.risk.default_trade_pct,
                max_positions=self._config.risk.max_positions,
                max_daily_loss_pct=self._config.risk.max_daily_loss_pct,
                max_drawdown_pct=self._config.risk.max_drawdown_pct,
            )
            self._strategy.initialize(risk_limits, self._config.symbols)

            # Restore state
            state_row = await self._db.fetchone(
                "SELECT state_json FROM strategy_state ORDER BY saved_at DESC LIMIT 1"
            )
            if state_row:
                self._strategy.load_state(json.loads(state_row["state_json"]))
                log.info("strategy.state_restored")
        except Exception as e:
            log.error("strategy.load_failed", error=str(e))
            raise

        # 5. AI client
        self._ai = AIClient(self._config.ai, self._db)
        if self._config.ai.anthropic_api_key or self._config.ai.vertex_project_id:
            try:
                await self._ai.initialize()
            except Exception as e:
                # System can still trade without AI — orchestration will be unavailable
                log.error("ai.init_failed", error=str(e),
                          note="Nightly orchestration will be unavailable")

        # 6. Reporter & Orchestrator
        self._reporter = Reporter(self._db)
        self._orchestrator = Orchestrator(
            self._config, self._db, self._ai, self._reporter, self._data_store
        )

        # 7. Telegram
        self._notifier = Notifier(self._config.telegram.chat_id)
        self._commands = BotCommands(
            config=self._config,
            db=self._db,
            scan_state=self._scan_state,
            portfolio_tracker=self._portfolio,
            risk_manager=self._risk,
            ai_client=self._ai,
            reporter=self._reporter,
        )
        self._telegram = TelegramBot(self._config.telegram, self._commands)
        await self._telegram.start()
        if self._telegram.app:
            self._notifier.set_app(self._telegram.app)

        # 8. WebSocket
        self._ws = KrakenWebSocket(self._config.kraken.ws_url, self._config.symbols)
        self._ws.set_on_failure(self._on_ws_failure)

        # 9. Scheduler
        self._scheduler = AsyncIOScheduler()
        self._setup_jobs()
        self._scheduler.start()

        # 10. Portfolio peak tracking
        portfolio_value = await self._portfolio.total_value()
        self._risk.update_portfolio_peak(portfolio_value)

        # 11. Notify
        await self._notifier.system_online(portfolio_value, self._portfolio.position_count)

        self._running = True
        log.info("brain.started", portfolio=f"${portfolio_value:.2f}",
                 positions=self._portfolio.position_count, mode=self._config.mode)

        # Run WebSocket in background
        asyncio.create_task(self._ws.connect())

        # Keep alive
        while self._running:
            await asyncio.sleep(1)

            # Check kill switch
            if self._scan_state.get("kill_requested"):
                await self._emergency_stop()
                self._scan_state["kill_requested"] = False

    def _setup_jobs(self) -> None:
        """Configure all scheduled jobs."""
        scan_interval = self._strategy.scan_interval_minutes if self._strategy else 5

        # Strategy scan
        self._scheduler.add_job(
            self._scan_loop, IntervalTrigger(minutes=scan_interval),
            id="scan", name="Strategy Scan",
            next_run_time=datetime.now() + timedelta(seconds=10),
        )

        # Position monitor (stop-loss / take-profit)
        self._scheduler.add_job(
            self._position_monitor, IntervalTrigger(seconds=30),
            id="position_monitor", name="Position Monitor",
        )

        # Fee check
        self._scheduler.add_job(
            self._check_fees, IntervalTrigger(hours=self._config.fees.check_interval_hours),
            id="fee_check", name="Fee Check",
            next_run_time=datetime.now() + timedelta(minutes=1),
        )

        # Daily P&L snapshot
        self._scheduler.add_job(
            self._daily_snapshot, CronTrigger(hour=23, minute=55),
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
            CronTrigger(hour=self._config.orchestrator.start_hour, minute=0),
            id="orchestration", name="Nightly Orchestration",
        )

        # Weekly report
        self._scheduler.add_job(
            self._weekly_report, CronTrigger(day_of_week="sun", hour=20, minute=0),
            id="weekly_report", name="Weekly Report",
        )

        log.info("scheduler.configured", scan_interval=scan_interval)

    async def _bootstrap_historical_data(self) -> None:
        """Fetch ~30 days of 5m candles from Kraken if DB is sparse.

        Runs once on startup. Paginates the OHLC API (720 candles/request).
        """
        for symbol in self._config.symbols:
            count = await self._data_store.get_candle_count(symbol, "5m")
            if count >= 1000:
                continue

            log.info("bootstrap.fetching", symbol=symbol, existing=count)
            since = int((datetime.now() - timedelta(days=30)).timestamp())
            total = 0

            while True:
                try:
                    df = await self._kraken.get_ohlc(symbol, interval=5, since=since)
                except Exception as e:
                    log.warning("bootstrap.fetch_failed", symbol=symbol, error=str(e))
                    break

                if df.empty:
                    break

                stored = await self._data_store.store_candles(symbol, "5m", df)
                total += stored

                # Use last candle timestamp for next page
                last_ts = int(df.index[-1].timestamp())
                if last_ts <= since:
                    break  # No progress
                since = last_ts

                if len(df) < 720:
                    break  # Last page

                await asyncio.sleep(1)  # Rate limit

            log.info("bootstrap.complete", symbol=symbol, candles=total)

    async def _on_ws_failure(self) -> None:
        """Called when WebSocket permanently fails after max retries."""
        await self._notifier.websocket_failed()

    async def _scan_loop(self) -> None:
        """Main scan loop — fetch data, run strategy, execute signals."""
        if self._commands and self._commands.is_paused:
            return
        if self._risk and self._risk.is_halted:
            return

        log.info("scan.start")
        try:
            prices = {}
            markets = {}
            scan_symbols = {}

            for symbol in self._config.symbols:
                try:
                    ticker = await self._kraken.get_ticker(symbol)
                    price = float(ticker["c"][0])  # Last trade price
                    prices[symbol] = price

                    # Fetch recent candles for strategy
                    df_5m = await self._data_store.get_candles(symbol, "5m", limit=8640)

                    # If we don't have enough stored data, fetch from Kraken
                    if len(df_5m) < 30:
                        df_5m = await self._kraken.get_ohlc(symbol, interval=5)
                        if not df_5m.empty:
                            await self._data_store.store_candles(symbol, "5m", df_5m)

                    df_1h = await self._data_store.get_candles(symbol, "1h", limit=8760)
                    df_1d = await self._data_store.get_candles(symbol, "1d", limit=2555)

                    spread = await self._kraken.get_spread(symbol)
                    vol_24h = float(ticker.get("v", [0, 0])[1])

                    markets[symbol] = SymbolData(
                        symbol=symbol,
                        current_price=price,
                        candles_5m=df_5m,
                        candles_1h=df_1h if not df_1h.empty else df_5m,
                        candles_1d=df_1d if not df_1d.empty else df_5m,
                        spread=spread,
                        volume_24h=vol_24h,
                    )

                    # Compute indicators for scan_state (used by /report)
                    indicators = compute_indicators(df_5m) if len(df_5m) >= 30 else {}

                    scan_symbols[symbol] = {
                        "price": price,
                        "spread": spread,
                        "vol_ratio": indicators.get("vol_ratio", 0),
                        "rsi": indicators.get("rsi", 0),
                        "ema_fast": indicators.get("ema_fast", 0),
                        "ema_slow": indicators.get("ema_slow", 0),
                        "regime": indicators.get("regime", "unknown"),
                    }

                    # Store scan results (raw indicator values + strategy's regime interpretation)
                    await self._db.execute(
                        """INSERT INTO scan_results
                           (timestamp, symbol, price, ema_fast, ema_slow, rsi, volume_ratio, spread, strategy_regime)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (datetime.now().isoformat(), symbol, price,
                         indicators.get("ema_fast"), indicators.get("ema_slow"),
                         indicators.get("rsi"), indicators.get("vol_ratio"),
                         spread, indicators.get("regime")),
                    )

                except Exception as e:
                    log.warning("scan.symbol_error", symbol=symbol, error=str(e))

            if not markets:
                return

            # Build portfolio snapshot
            portfolio = await self._portfolio.get_portfolio(prices)
            portfolio_value = portfolio.total_value

            # Run strategy
            signals = self._strategy.analyze(markets, portfolio, datetime.now())

            # Process signals
            for signal in signals:
                # Risk check
                check = self._risk.check_signal(
                    signal, portfolio_value, self._portfolio.position_count,
                    self._portfolio.get_position_value(signal.symbol),
                )

                if not check.passed:
                    log.info("scan.signal_rejected", symbol=signal.symbol, reason=check.reason)
                    regime = scan_symbols.get(signal.symbol, {}).get("regime")
                    await self._db.execute(
                        "INSERT INTO signals (symbol, action, size_pct, confidence, intent, reasoning, strategy_regime, rejected_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (signal.symbol, signal.action.value, signal.size_pct, signal.confidence,
                         signal.intent.value, signal.reasoning, regime, check.reason),
                    )
                    continue

                # Clamp to risk limits
                signal = self._risk.clamp_signal(signal, portfolio_value)

                # Execute
                price = prices.get(signal.symbol, 0)
                regime = scan_symbols.get(signal.symbol, {}).get("regime")
                result = await self._portfolio.execute_signal(
                    signal, price,
                    self._config.kraken.maker_fee_pct,
                    self._config.kraken.taker_fee_pct,
                    strategy_regime=regime,
                )

                if result:
                    # Record signal
                    await self._db.execute(
                        "INSERT INTO signals (symbol, action, size_pct, confidence, intent, reasoning, strategy_regime, acted_on) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                        (signal.symbol, signal.action.value, signal.size_pct, signal.confidence,
                         signal.intent.value, signal.reasoning, regime),
                    )

                    # Track P&L
                    if result.get("pnl") is not None:
                        self._risk.record_trade_result(result["pnl"])
                        self._strategy.on_position_closed(
                            result["symbol"], result["pnl"], result.get("pnl_pct", 0)
                        )

                    if signal.action == Action.BUY:
                        self._strategy.on_fill(
                            signal.symbol, Action.BUY, result["qty"], result["price"], signal.intent
                        )

                    # Notify
                    await self._notifier.trade_executed(result)

                    # Check rollback triggers
                    new_value = await self._portfolio.total_value()
                    rollback = self._risk.check_rollback_triggers(
                        new_value, self._portfolio.daily_start_value
                    )
                    if not rollback.passed:
                        await self._notifier.rollback_alert(rollback.reason, "previous")
                        log.warning("scan.rollback_triggered", reason=rollback.reason)

                    # Update portfolio peak
                    self._risk.update_portfolio_peak(new_value)

                    # Update scan_state with signal info
                    if signal.symbol in scan_symbols:
                        scan_symbols[signal.symbol]["signal"] = {
                            "action": signal.action.value,
                            "confidence": signal.confidence,
                            "reasoning": signal.reasoning,
                        }

            # Update scan_results with signal info for symbols that generated signals
            for signal in signals:
                sym_data = scan_symbols.get(signal.symbol, {})
                if sym_data:
                    await self._db.execute(
                        """UPDATE scan_results SET signal_generated = 1, signal_action = ?, signal_confidence = ?
                           WHERE symbol = ? AND id = (SELECT MAX(id) FROM scan_results WHERE symbol = ?)""",
                        (signal.action.value, signal.confidence, signal.symbol, signal.symbol),
                    )

            await self._db.commit()

            # Update scan state
            self._scan_state["symbols"] = scan_symbols
            self._scan_state["last_scan"] = datetime.now().strftime("%H:%M:%S")
            log.info("scan.complete", symbols=len(scan_symbols), signals=len(signals))

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

        except Exception as e:
            import traceback
            log.error("scan.failed", error=str(e), traceback=traceback.format_exc())
            if self._notifier:
                await self._notifier.system_error(f"Scan loop failed: {e}")

    async def _position_monitor(self) -> None:
        """Check stop-loss and take-profit on open positions."""
        if not self._ws:
            return

        prices = self._ws.prices
        if not prices:
            # Fallback: get prices via REST
            for symbol in self._config.symbols:
                try:
                    ticker = await self._kraken.get_ticker(symbol)
                    prices[symbol] = float(ticker["c"][0])
                except Exception as e:
                    log.warning("position_monitor.price_fetch_failed", symbol=symbol, error=str(e))

        triggered = await self._portfolio.update_prices(prices)
        for t in triggered:
            symbol = t["symbol"]
            reason = t["reason"]
            price = t["price"]

            signal = Signal(
                symbol=symbol, action=Action.CLOSE, size_pct=1.0,
                intent=Intent.DAY, confidence=1.0,
                reasoning=f"{reason} triggered at ${price:.2f}",
            )
            # Use most recent scan's regime for this symbol
            regime = self._scan_state.get("symbols", {}).get(symbol, {}).get("regime")
            result = await self._portfolio.execute_signal(
                signal, price,
                self._config.kraken.maker_fee_pct,
                self._config.kraken.taker_fee_pct,
                strategy_regime=regime,
            )
            if result:
                self._risk.record_trade_result(result.get("pnl", 0))
                await self._notifier.stop_triggered(symbol, reason, price)
                await self._notifier.trade_executed(result)

    async def _check_fees(self) -> None:
        """Update fee schedule from Kraken."""
        try:
            if self._config.kraken.api_key:
                maker, taker = await self._kraken.get_fee_schedule(self._config.symbols[0])
                self._config.kraken.maker_fee_pct = maker
                self._config.kraken.taker_fee_pct = taker
                await self._db.execute(
                    "INSERT INTO fee_schedule (maker_fee_pct, taker_fee_pct) VALUES (?, ?)",
                    (maker, taker),
                )
                await self._db.commit()
                log.info("fees.updated", maker=maker, taker=taker)
        except Exception as e:
            log.warning("fees.check_failed", error=str(e))

    async def _daily_snapshot(self) -> None:
        await self._portfolio.snapshot_daily()

    async def _daily_reset(self) -> None:
        self._risk.reset_daily()
        self._portfolio.reset_daily()
        self._ai.reset_daily_tokens()

    async def _nightly_orchestration(self) -> None:
        """Run the nightly AI review cycle."""
        try:
            report = await self._orchestrator.run_nightly_cycle()

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
                )
                self._strategy.initialize(risk_limits, self._config.symbols)
                self._scan_state["strategy_hash"] = new_hash
                log.info("strategy.reloaded_after_orchestration")

            await self._notifier.daily_summary(report)
        except Exception as e:
            log.error("orchestration.failed", error=str(e))
            await self._notifier.system_error(f"Orchestration failed: {e}")

    async def _weekly_report(self) -> None:
        try:
            report = await self._reporter.weekly_report()
            await self._notifier.weekly_report(report)
        except Exception as e:
            log.error("weekly_report.failed", error=str(e))

    async def _emergency_stop(self) -> None:
        """Close all positions immediately."""
        log.warning("brain.emergency_stop")
        await self._notifier.system_error("Emergency stop initiated — closing all positions")

        positions = await self._db.fetchall("SELECT * FROM positions")
        for pos in positions:
            try:
                ticker = await self._kraken.get_ticker(pos["symbol"])
                price = float(ticker["c"][0])
                signal = Signal(
                    symbol=pos["symbol"], action=Action.CLOSE, size_pct=1.0,
                    intent=Intent.DAY, confidence=1.0, reasoning="Emergency stop",
                )
                await self._portfolio.execute_signal(
                    signal, price,
                    self._config.kraken.maker_fee_pct,
                    self._config.kraken.taker_fee_pct,
                )
            except Exception as e:
                log.error("emergency.close_failed", symbol=pos["symbol"], error=str(e))

        # Verify all positions were closed
        remaining = await self._db.fetchall("SELECT symbol FROM positions")
        if remaining:
            symbols = [r["symbol"] for r in remaining]
            log.error("emergency.positions_remaining", symbols=symbols)
            await self._notifier.system_error(
                f"Emergency stop incomplete — positions remaining: {', '.join(symbols)}"
            )
        else:
            log.info("emergency.all_positions_closed")
            await self._notifier.system_error("Emergency stop complete — all positions closed")

    async def stop(self) -> None:
        """Graceful shutdown sequence."""
        log.info("brain.stopping")
        self._running = False

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
            try:
                await self._kraken.cancel_all_orders()
            except Exception as e:
                log.warning("shutdown.cancel_orders_failed", error=str(e))

        # 4. Stop WebSocket
        if self._ws:
            await self._ws.stop()

        # 5. Stop Telegram
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
    if LOCK_FILE.exists():
        old_pid = int(LOCK_FILE.read_text().strip())
        # Check if the old process is still alive
        try:
            os.kill(old_pid, 0)  # signal 0 = just check existence
            print(f"ERROR: Another instance is running (PID {old_pid}). Exiting.", file=sys.stderr)
            sys.exit(1)
        except (ProcessLookupError, PermissionError):
            # Stale lockfile — previous process died without cleanup
            log.warning("lockfile.stale", old_pid=old_pid)

    LOCK_FILE.write_text(str(os.getpid()))
    atexit.register(_release_lock)


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
    loop = asyncio.get_event_loop()

    def signal_handler():
        asyncio.create_task(brain.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)

    try:
        await brain.start()
    except KeyboardInterrupt:
        pass
    finally:
        await brain.stop()
        _release_lock()


def run() -> None:
    """Entry point for pyproject.toml script."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
