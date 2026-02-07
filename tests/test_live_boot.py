"""Boot the full system for 20 seconds then shut down cleanly."""

import asyncio
import signal
import sys

from src.main import TradingBrainApp


async def timed_boot():
    app = TradingBrainApp()

    async def shutdown_after_delay():
        await asyncio.sleep(20)
        print("\n--- 20s timer: requesting shutdown ---")
        app.request_shutdown()

    # Run app with a 20s auto-shutdown
    asyncio.create_task(shutdown_after_delay())
    await app.start()


if __name__ == "__main__":
    asyncio.run(timed_boot())
