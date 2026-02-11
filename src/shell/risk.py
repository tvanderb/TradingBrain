"""Risk Manager — hard limit enforcement on all signals.

Part of the rigid shell. Agent CANNOT modify these limits.
Every signal from the Strategy Module passes through here before execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import structlog

from src.shell.config import RiskConfig
from src.shell.contract import Signal, Action
from src.shell.database import Database

log = structlog.get_logger()


@dataclass
class RiskCheck:
    passed: bool
    reason: str


class RiskManager:
    """Enforces hard-coded risk limits on all trading activity."""

    def __init__(self, config: RiskConfig) -> None:
        self._config = config
        self._daily_trades: int = 0
        self._daily_pnl: float = 0.0
        self._consecutive_losses: int = 0
        self._peak_portfolio: float | None = None
        self._halted: bool = False
        self._halt_reason: str = ""

    async def initialize(self, db: Database, tz_name: str = "US/Eastern") -> None:
        """Load peak portfolio value and restore risk counters from DB after restart."""
        row = await db.fetchone(
            "SELECT MAX(portfolio_value) as peak FROM daily_performance"
        )
        if row and row["peak"] is not None:
            self._peak_portfolio = row["peak"]
            log.info("risk.peak_loaded", peak=round(self._peak_portfolio, 2))

        # Restore daily counters — use configured timezone to match daily reset boundary
        tz = ZoneInfo(tz_name)
        local_today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        today_utc = local_today.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        day_row = await db.fetchone(
            "SELECT COUNT(*) as cnt, COALESCE(SUM(pnl), 0) as total_pnl FROM trades WHERE datetime(closed_at) >= datetime(?)",
            (today_utc,),
        )
        if day_row:
            self._daily_trades = day_row["cnt"]
            self._daily_pnl = day_row["total_pnl"]

        # Restore consecutive losses (count backwards from most recent trades)
        recent = await db.fetchall(
            "SELECT pnl FROM trades WHERE pnl IS NOT NULL ORDER BY closed_at DESC LIMIT 20"
        )
        streak = 0
        for t in recent:
            if t["pnl"] < 0:
                streak += 1
            else:
                break
        self._consecutive_losses = streak

        if self._daily_trades > 0 or streak > 0:
            log.info("risk.counters_restored", daily_trades=self._daily_trades,
                     daily_pnl=round(self._daily_pnl, 2), consecutive_losses=streak)

    @property
    def is_halted(self) -> bool:
        return self._halted or self._config.kill_switch

    @property
    def halt_reason(self) -> str:
        if self._config.kill_switch:
            return "Emergency kill switch is ON"
        return self._halt_reason

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    @property
    def daily_trades(self) -> int:
        return self._daily_trades

    @property
    def consecutive_losses(self) -> int:
        return self._consecutive_losses

    @property
    def peak_portfolio(self) -> float | None:
        return self._peak_portfolio

    def reset_daily(self) -> None:
        self._daily_trades = 0
        self._daily_pnl = 0.0
        # Auto-unhalt daily-loss halts (they should reset with the day)
        # Drawdown and consecutive-loss halts persist (they're cumulative/structural)
        if self._halted and "Daily portfolio drop" in self._halt_reason:
            self._halted = False
            self._halt_reason = ""
            log.info("risk.daily_loss_halt_cleared")
        log.info("risk.daily_reset")

    def record_trade_result(self, pnl: float) -> None:
        self._daily_pnl += pnl
        self._daily_trades += 1
        if pnl < 0:
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0

    def update_portfolio_peak(self, value: float) -> None:
        if self._peak_portfolio is None or value > self._peak_portfolio:
            self._peak_portfolio = value

    def check_signal(
        self,
        signal: Signal,
        portfolio_value: float,
        open_position_count: int,
        position_value_for_symbol: float = 0.0,
        daily_start_value: float | None = None,
        is_new_position: bool = True,
    ) -> RiskCheck:
        """Validate a signal against all risk limits. Returns pass/fail with reason."""
        trade_value = 0.0  # Initialize before conditional blocks that use it

        # Always allow SELL/CLOSE/MODIFY — must be able to exit or adjust positions under any conditions
        is_exit = signal.action in (Action.SELL, Action.CLOSE, Action.MODIFY)

        # Kill switch (only blocks new entries)
        if self._config.kill_switch and not is_exit:
            return RiskCheck(False, "Emergency kill switch is ON")

        # Halted (rollback triggered — only blocks new entries)
        if self._halted and not is_exit:
            return RiskCheck(False, f"Trading halted: {self._halt_reason}")

        # Daily loss limit (only blocks new entries) — use start-of-day value as base
        base_value = daily_start_value if daily_start_value and daily_start_value > 0 else portfolio_value
        max_daily_loss = base_value * self._config.max_daily_loss_pct
        if self._daily_pnl < -max_daily_loss and not is_exit:
            return RiskCheck(False, f"Daily loss limit: ${self._daily_pnl:.2f} < -${max_daily_loss:.2f}")

        # Daily trade count (only blocks new entries)
        if self._daily_trades >= self._config.max_daily_trades and not is_exit:
            return RiskCheck(False, f"Daily trade limit: {self._daily_trades}/{self._config.max_daily_trades}")

        # Max positions (only for genuinely new positions, not average-in)
        if signal.action == Action.BUY and is_new_position and open_position_count >= self._config.max_positions:
            return RiskCheck(False, f"Max positions: {open_position_count}/{self._config.max_positions}")

        # Per-trade size limit (only for new entries — exits/modifies can have size_pct=0)
        if not is_exit:
            # Basic signal validation (entries must have positive size)
            if signal.size_pct <= 0:
                return RiskCheck(False, f"Invalid size_pct: {signal.size_pct}")
            trade_value = portfolio_value * signal.size_pct
            max_trade = portfolio_value * self._config.max_trade_pct
            if trade_value > max_trade:
                return RiskCheck(False, f"Trade size {signal.size_pct:.1%} exceeds limit {self._config.max_trade_pct:.1%}")

        # Per-position size limit (existing + new — only for entries)
        if signal.action == Action.BUY:
            new_position_value = position_value_for_symbol + trade_value
            max_position = portfolio_value * self._config.max_position_pct
            if new_position_value > max_position:
                return RiskCheck(False, f"Position size ${new_position_value:.2f} exceeds limit ${max_position:.2f}")

        # Drawdown check (only blocks new entries — still allow exits)
        if self._peak_portfolio is not None and not is_exit:
            drawdown = (self._peak_portfolio - portfolio_value) / self._peak_portfolio
            if drawdown > self._config.max_drawdown_pct:
                self._halted = True
                self._halt_reason = f"Max drawdown {drawdown:.1%} > {self._config.max_drawdown_pct:.1%}"
                return RiskCheck(False, self._halt_reason)

        # Consecutive losses rollback trigger (only blocks new entries)
        if self._consecutive_losses >= self._config.rollback_consecutive_losses and not is_exit:
            self._halted = True
            self._halt_reason = f"{self._consecutive_losses} consecutive losses"
            return RiskCheck(False, self._halt_reason)

        return RiskCheck(True, "OK")

    def check_rollback_triggers(self, portfolio_value: float, starting_value: float) -> RiskCheck:
        """Check shell-enforced rollback triggers. Called after each trade."""

        # Daily portfolio drop trigger
        daily_loss_pct = (starting_value - portfolio_value) / starting_value if starting_value > 0 else 0
        if daily_loss_pct > self._config.rollback_daily_loss_pct:
            self._halted = True
            self._halt_reason = f"Daily portfolio drop {daily_loss_pct:.1%} > {self._config.rollback_daily_loss_pct:.1%}"
            return RiskCheck(False, self._halt_reason)

        # Consecutive losses
        if self._consecutive_losses >= self._config.rollback_consecutive_losses:
            self._halted = True
            self._halt_reason = f"{self._consecutive_losses} consecutive losses — rollback triggered"
            return RiskCheck(False, self._halt_reason)

        return RiskCheck(True, "OK")

    def clamp_signal(self, signal: Signal, portfolio_value: float) -> Signal:
        """Clamp signal size to respect risk limits. Returns modified signal."""
        max_size = self._config.max_trade_pct
        if signal.size_pct > max_size:
            log.warning("risk.clamped_size", symbol=signal.symbol, original=signal.size_pct, clamped=max_size)
            signal.size_pct = max_size
        return signal

    def unhalt(self) -> None:
        """Manual unhalt (user action via Telegram)."""
        self._halted = False
        self._halt_reason = ""
        self._consecutive_losses = 0
        log.info("risk.unhalted")
