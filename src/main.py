"""Trading Brain - Main entry point.

Initializes all components and runs the async event loop
that coordinates the three brains, Telegram bot, and market data.
"""

from __future__ import annotations

import asyncio
import signal
import sys

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.brains.analyst import AnalystBrain
from src.brains.executive import ExecutiveBrain
from src.brains.executor import ExecutorBrain
from src.core.config import Config
from src.core.logging import get_logger, setup_logging
from src.core.tokens import TokenTracker
from src.evolution.performance import compute_daily_snapshot
from src.market.data_feed import DataFeed
from src.market.regime import classify_regime
from src.market.signals import SignalGenerator, compute_indicators
from src.storage import queries
from src.storage.database import Database
from src.telegram.bot import TelegramBot
from src.telegram.commands import BotCommands
from src.telegram.notifications import Notifier

log = get_logger("main")


class TradingBrainApp:
    """Top-level application that wires all components together."""

    def __init__(self) -> None:
        self._shutdown_event = asyncio.Event()
        self._scheduler: AsyncIOScheduler | None = None

    async def start(self) -> None:
        """Initialize and run all subsystems."""
        # Load config
        config = Config.load()
        setup_logging(config.log_level)

        log.info("starting", mode=config.mode, symbols=config.markets.crypto_symbols)

        # Connect database
        db = Database()
        await db.connect()

        # Core infrastructure
        token_tracker = TokenTracker(db)
        notifier = Notifier(config)
        data_feed = DataFeed(config)
        signal_gen = SignalGenerator(config.strategy)

        # Initialize brains
        executor = ExecutorBrain(config, db, data_feed)
        analyst = AnalystBrain(config, db, token_tracker)
        executive = ExecutiveBrain(config, db, token_tracker)

        # Wire up notification callbacks
        executor.on_trade_executed = notifier.trade_executed
        executor.on_stop_triggered = notifier.stop_triggered
        executive.on_evolution_complete = notifier.evolution_complete

        # Start brains
        await executor.start()
        await analyst.start()
        await executive.start()

        # Telegram bot
        # Shared scan state: updated by scan loop, read by /report command
        scan_state: dict = {}

        commands = BotCommands(config, db, executor, analyst, executive, token_tracker,
                               scan_state=scan_state)
        telegram = TelegramBot(config, commands)

        # Scheduler
        self._scheduler = AsyncIOScheduler()

        # Analyst scan: every N minutes, generate signals and validate
        async def analyst_scan() -> None:
            log.info("scan_started")
            import numpy as np
            from datetime import datetime as dt
            scan_state["last_scan_time"] = dt.now().strftime("%H:%M:%S")

            for symbol in config.markets.crypto_symbols:
                try:
                    df = await data_feed.load_historical(symbol, interval=5)
                    if df.empty:
                        log.info("scan_no_data", symbol=symbol)
                        continue

                    # Compute indicators and regime for /report
                    df_ind = compute_indicators(df.copy())
                    regime = classify_regime(df)
                    last = df_ind.iloc[-1]
                    price = float(last["close"])

                    # Store scan results for /report command
                    scan_state[symbol] = {
                        "price": round(price, 2),
                        "rsi": round(float(last["rsi"]), 1) if not np.isnan(last["rsi"]) else None,
                        "bb_pct": round(float(last["bb_pct"]), 2) if not np.isnan(last["bb_pct"]) else None,
                        "ema_fast": round(float(last["ema_fast"]), 2) if not np.isnan(last["ema_fast"]) else None,
                        "ema_slow": round(float(last["ema_slow"]), 2) if not np.isnan(last["ema_slow"]) else None,
                        "macd_hist": round(float(last["macd_hist"]), 4) if not np.isnan(last["macd_hist"]) else None,
                        "vol_ratio": round(float(last["vol_ratio"]), 2) if not np.isnan(last["vol_ratio"]) else None,
                        "regime": regime.regime.value,
                        "regime_desc": regime.description,
                        "regime_confidence": round(regime.confidence, 2),
                        "signal_direction": None,
                        "signal_strength": None,
                        "signal_type": None,
                    }

                    # Generate technical signal
                    raw_signal = signal_gen.generate(df, symbol)
                    if raw_signal is None:
                        log.info("scan_no_signal", symbol=symbol, price=round(price, 2))
                        continue

                    # Store signal info in scan state
                    scan_state[symbol]["signal_direction"] = raw_signal.direction
                    scan_state[symbol]["signal_strength"] = round(raw_signal.strength, 3)
                    scan_state[symbol]["signal_type"] = raw_signal.signal_type

                    log.info("scan_signal", symbol=symbol, direction=raw_signal.direction,
                             strength=round(raw_signal.strength, 3), type=raw_signal.signal_type)

                    # Check if worth analyzing with AI
                    if not await analyst.should_analyze(raw_signal):
                        log.info("scan_below_threshold", symbol=symbol, strength=round(raw_signal.strength, 3))
                        continue

                    # AI validation
                    result = await analyst.validate_signal(raw_signal, regime)

                    if result.valid and result.confidence > 0.5:
                        await executor.execute_signal(raw_signal, ai_validated=True)
                    else:
                        log.info("scan_ai_rejected", symbol=symbol, valid=result.valid,
                                 confidence=round(result.confidence, 2))

                except Exception as e:
                    log.error("scan_error", symbol=symbol, error=str(e))
                    await notifier.error(f"scan_{symbol}", str(e))
            log.info("scan_complete")

        # Position monitoring
        async def monitor_positions() -> None:
            try:
                await executor.monitor_positions()
            except Exception as e:
                log.error("monitor_error", error=str(e))

        # Fee check
        async def check_fees() -> None:
            try:
                fees = await data_feed.check_fees()
                executor.update_fees(fees)
                await queries.insert_fee_check(db, fees)
                await notifier.fee_update(
                    fees.maker_fee_pct, fees.taker_fee_pct, fees.fee_tier or ""
                )
                log.info(
                    "fee_check_complete",
                    maker=fees.maker_fee_pct,
                    taker=fees.taker_fee_pct,
                )
            except Exception as e:
                log.error("fee_check_error", error=str(e))

        # Daily performance snapshot
        async def daily_snapshot() -> None:
            try:
                prices = data_feed.latest_prices
                portfolio_value = executor.order_manager.get_portfolio_value(prices)
                costs = await token_tracker.get_daily_cost_summary()
                total_cost = sum(costs.values())
                await compute_daily_snapshot(
                    db,
                    portfolio_value=portfolio_value,
                    token_cost=total_cost,
                )
                status = executor.get_status()
                await notifier.daily_summary(status)
                executor.risk_manager.reset_daily()
            except Exception as e:
                log.error("snapshot_error", error=str(e))

        # Daily evolution
        async def daily_evolution() -> None:
            try:
                await executive.daily_evolution_cycle()
            except Exception as e:
                log.error("evolution_error", error=str(e))
                await notifier.error("evolution", str(e))

        # Schedule jobs (run first scan immediately via next_run_time)
        from datetime import datetime
        self._scheduler.add_job(
            analyst_scan, "interval", minutes=config.analyst.scan_interval_minutes,
            next_run_time=datetime.now(),
        )
        self._scheduler.add_job(monitor_positions, "interval", seconds=30)
        self._scheduler.add_job(
            check_fees, "interval", hours=config.fees.check_interval_hours
        )
        self._scheduler.add_job(
            daily_snapshot, "cron", hour=23, minute=55
        )  # End of day
        self._scheduler.add_job(
            daily_evolution,
            "cron",
            hour=config.executive.evolution_hour,
            minute=config.executive.evolution_minute,
        )

        self._scheduler.start()
        log.info("scheduler_started", jobs=len(self._scheduler.get_jobs()))

        # Run initial fee check
        asyncio.create_task(check_fees())

        log.info("system_online", mode=config.mode)

        # Start long-running tasks
        try:
            await asyncio.gather(
                telegram.start(),
                data_feed.stream(),
                self._shutdown_event.wait(),
            )
        finally:
            # Graceful shutdown
            log.info("shutting_down")
            data_feed.stop()
            if self._scheduler:
                self._scheduler.shutdown(wait=False)
            await telegram.stop()
            await executor.stop()
            await data_feed._rest.close()
            await db.close()

    def request_shutdown(self) -> None:
        self._shutdown_event.set()


def run() -> None:
    """Entry point for the trading-brain command."""
    app = TradingBrainApp()

    loop = asyncio.new_event_loop()

    def handle_signal(sig: int, frame: object) -> None:
        log.info("signal_received", signal=sig)
        app.request_shutdown()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        loop.run_until_complete(app.start())
    except KeyboardInterrupt:
        app.request_shutdown()
    finally:
        loop.close()


if __name__ == "__main__":
    run()
