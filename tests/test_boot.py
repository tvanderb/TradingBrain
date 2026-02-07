"""Test that the system initializes all components without crashing."""

import asyncio

from src.core.config import Config
from src.core.logging import setup_logging, get_logger
from src.core.tokens import TokenTracker
from src.storage.database import Database
from src.market.data_feed import DataFeed
from src.market.signals import SignalGenerator
from src.brains.executor import ExecutorBrain
from src.brains.analyst import AnalystBrain
from src.brains.executive import ExecutiveBrain
from src.telegram.commands import BotCommands
from src.telegram.notifications import Notifier
from pathlib import Path
import os


async def test_boot():
    config = Config.load()
    setup_logging("WARNING")

    print("=== Boot Test ===\n")

    # DB
    test_db = Path("/tmp/test_boot.db")
    if test_db.exists():
        test_db.unlink()
    db = Database(test_db)
    await db.connect()
    print("1. Database: OK")

    # Token tracker
    tokens = TokenTracker(db)
    print("2. Token tracker: OK")

    # Notifier (won't send without bot token)
    notifier = Notifier(config)
    print("3. Notifier: OK")

    # Data feed
    data_feed = DataFeed(config)
    print("4. Data feed: OK")

    # Signal generator
    sig_gen = SignalGenerator(config.strategy)
    print("5. Signal generator: OK")

    # Brains
    executor = ExecutorBrain(config, db, data_feed)
    await executor.start()
    print(f"6. Executor brain: OK (mode={config.mode})")

    analyst = AnalystBrain(config, db, tokens)
    await analyst.start()
    print(f"7. Analyst brain: OK (model={config.analyst.model})")

    executive = ExecutiveBrain(config, db, tokens)
    await executive.start()
    print(f"8. Executive brain: OK (model={config.executive.model})")

    # Telegram commands (won't poll without token)
    commands = BotCommands(config, db, executor, analyst, executive, tokens)
    print("9. Telegram commands: OK")

    # Status check
    status = executor.get_status()
    print(f"\n10. System status:")
    for k, v in status.items():
        print(f"    {k}: {v}")

    # Cleanup
    await executor.stop()
    await db.close()
    print("\n=== BOOT TEST PASSED ===")


if __name__ == "__main__":
    asyncio.run(test_boot())
