"""Executor Brain — Deterministic trade execution.

No AI. Pure Python. Maximum speed.
Validates signals against risk limits and executes trades.
Monitors positions for stop/take-profit triggers.
"""

from __future__ import annotations

from src.brains.base import BaseBrain
from src.core.config import Config
from src.core.logging import get_logger
from src.market.data_feed import DataFeed
from src.market.signals import RawSignal
from src.storage.database import Database
from src.storage.models import FeeSchedule
from src.storage import queries
from src.trading.order_manager import OrderManager, OrderResult
from src.trading.position_tracker import PositionTracker
from src.trading.risk_manager import RiskManager

log = get_logger("executor")


class ExecutorBrain(BaseBrain):
    """Deterministic execution brain — no AI, pure logic."""

    def __init__(
        self,
        config: Config,
        db: Database,
        data_feed: DataFeed,
    ) -> None:
        self._config = config
        self._db = db
        self._data_feed = data_feed
        self._risk = RiskManager(config.risk)
        self._orders = OrderManager(config, db)
        self._positions = PositionTracker(db, self._orders)
        self._active = False
        self._paused = False

        # Notification callback (set by orchestrator)
        self.on_trade_executed: callable | None = None
        self.on_stop_triggered: callable | None = None

    @property
    def name(self) -> str:
        return "executor"

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def risk_manager(self) -> RiskManager:
        return self._risk

    @property
    def position_tracker(self) -> PositionTracker:
        return self._positions

    @property
    def order_manager(self) -> OrderManager:
        return self._orders

    async def start(self) -> None:
        await self._positions.load_positions()
        self._active = True
        log.info("executor_started", mode=self._config.mode)

    async def stop(self) -> None:
        self._active = False
        await self._orders.close()
        log.info("executor_stopped")

    def pause(self) -> None:
        self._paused = True
        log.info("executor_paused")

    def resume(self) -> None:
        self._paused = False
        log.info("executor_resumed")

    def update_fees(self, fees: FeeSchedule) -> None:
        """Update fee schedule across all components."""
        self._risk.update_fees(fees)
        self._orders.update_fees(fees)
        log.info("executor_fees_updated", maker=fees.maker_fee_pct, taker=fees.taker_fee_pct)

    async def execute_signal(self, signal: RawSignal, ai_validated: bool = False) -> OrderResult | None:
        """Attempt to execute a validated trading signal.

        Args:
            signal: The raw or AI-validated signal
            ai_validated: Whether the analyst brain approved this signal
        """
        if not self._active or self._paused:
            return None

        if self._risk.kill_switch:
            log.warning("kill_switch_active")
            return None

        symbol = signal.symbol
        prices = self._data_feed.latest_prices
        current_price = prices.get(symbol)

        if current_price is None:
            log.warning("no_price_data", symbol=symbol)
            return None

        portfolio_value = self._orders.get_portfolio_value(prices)

        # Check if we should close an existing position
        existing = self._positions.get_position(symbol)
        if signal.direction == "close" and existing:
            return await self._close_position(symbol, current_price)

        # For new entries, check risk
        trade_usd = self._risk.calculate_position_size(portfolio_value, signal.strength)

        # Enforce minimum trade size (must meaningfully exceed fees)
        if trade_usd < self._config.fees.min_trade_usd:
            log.info("trade_too_small", trade_usd=trade_usd, min=self._config.fees.min_trade_usd)
            return None

        risk_check = self._risk.check_trade(
            symbol=symbol,
            side="buy" if signal.direction == "long" else "sell",
            trade_usd=trade_usd,
            portfolio_value=portfolio_value,
            open_positions=self._positions.open_positions,
        )

        if not risk_check.passed:
            log.info("risk_rejected", symbol=symbol, reason=risk_check.reason)
            return None

        # Calculate qty
        qty = trade_usd / current_price

        # Place order
        result = await self._orders.place_order(
            symbol=symbol,
            side="buy" if signal.direction == "long" else "sell",
            qty=qty,
            price=current_price,
            order_type="market",
            signal_id=None,  # Set after signal is saved to DB
            notes=f"Signal: {signal.signal_type} (strength={signal.strength:.2f}, ai={ai_validated})",
        )

        if result.success:
            # Track position
            stop_loss, take_profit = self._risk.get_stop_take_profit(
                result.filled_price or current_price,
                signal.direction,
            )
            await self._positions.open_position(
                symbol=symbol,
                qty=qty,
                entry_price=result.filled_price or current_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                side=signal.direction,
            )

            if self.on_trade_executed:
                await self.on_trade_executed(result, signal)

        return result

    async def monitor_positions(self) -> None:
        """Check positions for stop/take-profit triggers."""
        if not self._active:
            return

        prices = self._data_feed.latest_prices
        if not prices:
            return

        triggered = await self._positions.update_prices(prices)

        for symbol in triggered:
            price = prices.get(symbol)
            if price:
                await self._close_position(symbol, price)

    async def _close_position(self, symbol: str, price: float) -> OrderResult | None:
        """Close a position and record the P&L."""
        pos = self._positions.get_position(symbol)
        if not pos:
            return None

        result = await self._orders.place_order(
            symbol=symbol,
            side="sell" if pos.side == "long" else "buy",
            qty=pos.qty,
            price=price,
            order_type="market",
            notes=f"Position close at ${price:.2f}",
        )

        if result.success:
            pnl = await self._positions.close_position(symbol, result.filled_price or price)
            self._risk.record_trade_result(pnl)

            if result.trade_id:
                await queries.update_trade_pnl(self._db, result.trade_id, pnl)

            if self.on_stop_triggered:
                await self.on_stop_triggered(symbol, pnl)

        return result

    async def emergency_close_all(self) -> list[OrderResult]:
        """Emergency: close all positions immediately."""
        log.warning("emergency_close_all")
        results = []
        prices = self._data_feed.latest_prices

        for pos in list(self._positions.open_positions):
            price = prices.get(pos.symbol)
            if price:
                r = await self._close_position(pos.symbol, price)
                if r:
                    results.append(r)

        return results

    def get_status(self) -> dict:
        """Get executor status for Telegram/API."""
        prices = self._data_feed.latest_prices
        return {
            "active": self._active,
            "paused": self._paused,
            "mode": self._config.mode,
            "open_positions": self._positions.position_count,
            "unrealized_pnl": round(self._positions.get_total_unrealized_pnl(), 2),
            "daily_pnl": round(self._risk.daily_pnl, 2),
            "daily_trades": self._risk.daily_trades,
            "portfolio_value": round(self._orders.get_portfolio_value(prices), 2),
            "cash_balance": round(self._orders.get_balance(), 2),
        }
