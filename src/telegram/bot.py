"""Telegram Bot — setup and lifecycle management."""

from __future__ import annotations

import structlog
from telegram.ext import Application, CommandHandler

from src.shell.config import TelegramConfig
from src.telegram.commands import BotCommands

log = structlog.get_logger()


class TelegramBot:
    """Manages the Telegram bot application lifecycle."""

    def __init__(self, config: TelegramConfig, commands: BotCommands) -> None:
        self._config = config
        self._commands = commands
        self._app: Application | None = None

    async def start(self) -> None:
        """Initialize and start the Telegram bot."""
        if not self._config.enabled or not self._config.bot_token:
            log.info("telegram.disabled")
            return

        self._app = (
            Application.builder()
            .token(self._config.bot_token)
            .build()
        )

        # Register command handlers
        handlers = {
            "start": self._commands.cmd_start,
            "status": self._commands.cmd_status,
            "positions": self._commands.cmd_positions,
            "trades": self._commands.cmd_trades,
            "report": self._commands.cmd_report,
            "risk": self._commands.cmd_risk,
            "performance": self._commands.cmd_performance,
            "strategy": self._commands.cmd_strategy,
            "tokens": self._commands.cmd_tokens,
            "ask": self._commands.cmd_ask,
            "thoughts": self._commands.cmd_thoughts,
            "thought": self._commands.cmd_thought,
            "pause": self._commands.cmd_pause,
            "resume": self._commands.cmd_resume,
            "kill": self._commands.cmd_kill,
        }

        for name, handler in handlers.items():
            self._app.add_handler(CommandHandler(name, handler))

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        log.info("telegram.started")

    async def stop(self) -> None:
        """Gracefully stop the bot."""
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            log.info("telegram.stopped")

    @property
    def app(self) -> Application | None:
        return self._app
