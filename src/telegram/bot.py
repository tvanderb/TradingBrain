"""Telegram bot setup and lifecycle management."""

from __future__ import annotations

from telegram.ext import Application, CommandHandler

from src.core.config import Config
from src.core.logging import get_logger
from src.telegram.commands import BotCommands

log = get_logger("telegram")


class TelegramBot:
    """Telegram bot for user interaction with the trading brain."""

    def __init__(self, config: Config, commands: BotCommands) -> None:
        self._config = config
        self._commands = commands
        self._app: Application | None = None

    async def start(self) -> None:
        """Initialize and start the Telegram bot."""
        if not self._config.telegram.enabled:
            log.info("telegram_disabled")
            return

        token = self._config.telegram.bot_token
        if not token:
            log.warning("telegram_no_token", msg="Set TELEGRAM_BOT_TOKEN in .env")
            return

        self._app = Application.builder().token(token).build()
        self._register_handlers()

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)

        log.info("telegram_started")

    def _register_handlers(self) -> None:
        assert self._app is not None
        c = self._commands

        self._app.add_handler(CommandHandler("start", c.cmd_start))
        self._app.add_handler(CommandHandler("status", c.cmd_status))
        self._app.add_handler(CommandHandler("positions", c.cmd_positions))
        self._app.add_handler(CommandHandler("trades", c.cmd_trades))
        self._app.add_handler(CommandHandler("performance", c.cmd_performance))
        self._app.add_handler(CommandHandler("ask", c.cmd_ask))
        self._app.add_handler(CommandHandler("pause", c.cmd_pause))
        self._app.add_handler(CommandHandler("resume", c.cmd_resume))
        self._app.add_handler(CommandHandler("risk", c.cmd_risk))
        self._app.add_handler(CommandHandler("evolution", c.cmd_evolution))
        self._app.add_handler(CommandHandler("tokens", c.cmd_tokens))
        self._app.add_handler(CommandHandler("signals", c.cmd_signals))
        self._app.add_handler(CommandHandler("report", c.cmd_report))
        self._app.add_handler(CommandHandler("kill", c.cmd_kill))

    async def stop(self) -> None:
        if self._app and self._app.updater:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            log.info("telegram_stopped")
