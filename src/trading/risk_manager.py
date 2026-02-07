"""Hard risk management — the safety net.

Enforces risk limits that are NEVER modifiable by AI.
Every trade must pass all risk checks before execution.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.core.config import RiskLimits
from src.core.logging import get_logger
from src.storage.models import FeeSchedule, Position

log = get_logger("risk")


@dataclass
class RiskCheck:
    passed: bool
    reason: str


class RiskManager:
    """Enforces hard-coded risk limits on all trading activity."""

    def __init__(self, limits: RiskLimits, fees: FeeSchedule | None = None) -> None:
        self._limits = limits
        self._fees = fees
        self._daily_trades = 0
        self._daily_pnl = 0.0
        self._peak_portfolio_value: float | None = None

    @property
    def kill_switch(self) -> bool:
        return self._limits.emergency.kill_switch

    def update_fees(self, fees: FeeSchedule) -> None:
        """Update current fee schedule for cost calculations."""
        self._fees = fees
        log.info("fees_updated", maker=fees.maker_fee_pct, taker=fees.taker_fee_pct)

    def reset_daily(self) -> None:
        """Reset daily counters (call at start of each trading day)."""
        self._daily_trades = 0
        self._daily_pnl = 0.0
        log.info("daily_risk_reset")

    def record_trade_result(self, pnl: float) -> None:
        """Track daily P&L for limit enforcement."""
        self._daily_pnl += pnl
        self._daily_trades += 1

    def check_trade(
        self,
        symbol: str,
        side: str,
        trade_usd: float,
        portfolio_value: float,
        open_positions: list[Position],
    ) -> RiskCheck:
        """Run all risk checks before allowing a trade.

        Returns RiskCheck with pass/fail and reason.
        """
        # Emergency kill switch
        if self._limits.emergency.kill_switch:
            return RiskCheck(False, "Emergency kill switch is ON")

        # Daily loss limit
        max_daily_loss = portfolio_value * self._limits.daily.max_daily_loss_pct
        if self._daily_pnl < -max_daily_loss:
            return RiskCheck(
                False,
                f"Daily loss limit hit: ${self._daily_pnl:.2f} exceeds -${max_daily_loss:.2f}",
            )

        # Daily trade count
        if self._daily_trades >= self._limits.daily.max_daily_trades:
            return RiskCheck(
                False,
                f"Daily trade limit: {self._daily_trades}/{self._limits.daily.max_daily_trades}",
            )

        # Max positions
        if side == "buy" and len(open_positions) >= self._limits.position.max_positions:
            return RiskCheck(
                False,
                f"Max positions: {len(open_positions)}/{self._limits.position.max_positions}",
            )

        # Per-trade size limit
        max_trade = portfolio_value * self._limits.per_trade.max_trade_pct
        if trade_usd > max_trade:
            return RiskCheck(
                False,
                f"Trade size ${trade_usd:.2f} exceeds limit ${max_trade:.2f} ({self._limits.per_trade.max_trade_pct:.0%})",
            )

        # Per-position size limit
        existing_position_value = 0.0
        for pos in open_positions:
            if pos.symbol == symbol and pos.current_price:
                existing_position_value = pos.qty * pos.current_price
        total_position = existing_position_value + trade_usd
        max_position = portfolio_value * self._limits.position.max_position_pct
        if total_position > max_position:
            return RiskCheck(
                False,
                f"Position size ${total_position:.2f} would exceed limit ${max_position:.2f}",
            )

        # Max drawdown check
        if self._peak_portfolio_value is not None:
            drawdown = (self._peak_portfolio_value - portfolio_value) / self._peak_portfolio_value
            if drawdown > self._limits.emergency.max_drawdown_pct:
                return RiskCheck(
                    False,
                    f"Drawdown {drawdown:.1%} exceeds max {self._limits.emergency.max_drawdown_pct:.1%}",
                )

        # Fee profitability check
        if self._fees:
            round_trip_fee = self._fees.round_trip_cost(trade_usd)
            if round_trip_fee > trade_usd * 0.01:  # Fees > 1% of trade
                log.warning(
                    "high_fee_ratio",
                    trade_usd=trade_usd,
                    round_trip_fee=round_trip_fee,
                    ratio=round_trip_fee / trade_usd,
                )

        # Track peak for drawdown calculation
        if self._peak_portfolio_value is None or portfolio_value > self._peak_portfolio_value:
            self._peak_portfolio_value = portfolio_value

        log.info(
            "risk_check_passed",
            symbol=symbol,
            side=side,
            trade_usd=round(trade_usd, 2),
            daily_pnl=round(self._daily_pnl, 2),
            daily_trades=self._daily_trades,
            open_positions=len(open_positions),
        )
        return RiskCheck(True, "All checks passed")

    def calculate_position_size(
        self, portfolio_value: float, signal_strength: float
    ) -> float:
        """Calculate trade size in USD based on signal strength and risk limits.

        Stronger signals get larger position sizes, up to the per-trade max.
        """
        max_pct = self._limits.per_trade.max_trade_pct
        # Scale between 50% and 100% of max based on signal strength
        scale = 0.5 + (signal_strength * 0.5)
        trade_pct = max_pct * scale
        return portfolio_value * trade_pct

    def get_stop_take_profit(
        self, entry_price: float, direction: str
    ) -> tuple[float, float]:
        """Calculate stop loss and take profit prices."""
        sl_pct = self._limits.per_trade.default_stop_loss_pct
        tp_pct = self._limits.per_trade.default_take_profit_pct

        if direction == "long":
            stop_loss = entry_price * (1 - sl_pct)
            take_profit = entry_price * (1 + tp_pct)
        else:
            stop_loss = entry_price * (1 + sl_pct)
            take_profit = entry_price * (1 - tp_pct)

        return round(stop_loss, 2), round(take_profit, 2)

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    @property
    def daily_trades(self) -> int:
        return self._daily_trades
