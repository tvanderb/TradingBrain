"""Risk Manager — hard limit enforcement on all signals.

Part of the rigid shell. Agent CANNOT modify these limits.
Every signal from the Strategy Module passes through here before execution.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from src.shell.config import RiskConfig
from src.shell.contract import Signal, Action

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

    def reset_daily(self) -> None:
        self._daily_trades = 0
        self._daily_pnl = 0.0
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
    ) -> RiskCheck:
        """Validate a signal against all risk limits. Returns pass/fail with reason."""

        # Kill switch
        if self._config.kill_switch:
            return RiskCheck(False, "Emergency kill switch is ON")

        # Halted (rollback triggered)
        if self._halted:
            return RiskCheck(False, f"Trading halted: {self._halt_reason}")

        # Daily loss limit
        max_daily_loss = portfolio_value * self._config.max_daily_loss_pct
        if self._daily_pnl < -max_daily_loss:
            return RiskCheck(False, f"Daily loss limit: ${self._daily_pnl:.2f} < -${max_daily_loss:.2f}")

        # Daily trade count
        if self._daily_trades >= self._config.max_daily_trades:
            return RiskCheck(False, f"Daily trade limit: {self._daily_trades}/{self._config.max_daily_trades}")

        # Max positions (only for new entries)
        if signal.action == Action.BUY and open_position_count >= self._config.max_positions:
            return RiskCheck(False, f"Max positions: {open_position_count}/{self._config.max_positions}")

        # Per-trade size limit
        trade_value = portfolio_value * signal.size_pct
        max_trade = portfolio_value * self._config.max_trade_pct
        if trade_value > max_trade:
            return RiskCheck(False, f"Trade size {signal.size_pct:.1%} exceeds limit {self._config.max_trade_pct:.1%}")

        # Per-position size limit (existing + new)
        if signal.action == Action.BUY:
            new_position_value = position_value_for_symbol + trade_value
            max_position = portfolio_value * self._config.max_position_pct
            if new_position_value > max_position:
                return RiskCheck(False, f"Position size ${new_position_value:.2f} exceeds limit ${max_position:.2f}")

        # Drawdown check
        if self._peak_portfolio is not None:
            drawdown = (self._peak_portfolio - portfolio_value) / self._peak_portfolio
            if drawdown > self._config.max_drawdown_pct:
                self._halted = True
                self._halt_reason = f"Max drawdown {drawdown:.1%} > {self._config.max_drawdown_pct:.1%}"
                return RiskCheck(False, self._halt_reason)

        # Consecutive losses rollback trigger
        if self._consecutive_losses >= self._config.rollback_consecutive_losses:
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
