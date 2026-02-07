"""Position tracking and P&L monitoring.

Syncs positions between our database and the order manager,
monitors for stop loss and take profit triggers.
"""

from __future__ import annotations

from src.core.logging import get_logger
from src.storage.database import Database
from src.storage.models import Position
from src.storage import queries
from src.trading.order_manager import OrderManager

log = get_logger("positions")


class PositionTracker:
    """Tracks open positions, monitors P&L, and triggers stops."""

    def __init__(self, db: Database, order_manager: OrderManager) -> None:
        self._db = db
        self._order_manager = order_manager
        self._positions: dict[str, Position] = {}

    @property
    def open_positions(self) -> list[Position]:
        return list(self._positions.values())

    @property
    def position_count(self) -> int:
        return len(self._positions)

    async def load_positions(self) -> None:
        """Load positions from database on startup."""
        positions = await queries.get_open_positions(self._db)
        self._positions = {p.symbol: p for p in positions}
        log.info("positions_loaded", count=len(self._positions))

    async def open_position(
        self,
        symbol: str,
        qty: float,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        side: str = "long",
    ) -> None:
        """Record a new position after a buy fill."""
        pos = Position(
            symbol=symbol,
            qty=qty,
            avg_entry=entry_price,
            side=side,
            current_price=entry_price,
            unrealized_pnl=0.0,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
        await queries.upsert_position(self._db, pos)
        self._positions[symbol] = pos
        log.info(
            "position_opened",
            symbol=symbol,
            qty=qty,
            entry=entry_price,
            sl=stop_loss,
            tp=take_profit,
        )

    async def close_position(self, symbol: str, exit_price: float) -> float:
        """Close a position and calculate realized P&L."""
        pos = self._positions.get(symbol)
        if not pos:
            return 0.0

        if pos.side == "long":
            pnl = (exit_price - pos.avg_entry) * pos.qty
        else:
            pnl = (pos.avg_entry - exit_price) * pos.qty

        await queries.remove_position(self._db, symbol)
        del self._positions[symbol]

        log.info(
            "position_closed",
            symbol=symbol,
            entry=pos.avg_entry,
            exit=exit_price,
            pnl=round(pnl, 2),
        )
        return pnl

    async def update_prices(self, prices: dict[str, float]) -> list[str]:
        """Update current prices and return symbols that hit stops.

        Returns list of symbols that need to be closed (hit SL or TP).
        """
        triggered: list[str] = []

        for symbol, pos in self._positions.items():
            price = prices.get(symbol)
            if price is None:
                continue

            pos.current_price = price

            if pos.side == "long":
                pos.unrealized_pnl = (price - pos.avg_entry) * pos.qty
            else:
                pos.unrealized_pnl = (pos.avg_entry - price) * pos.qty

            await queries.upsert_position(self._db, pos)

            # Check stop loss
            if pos.stop_loss:
                if pos.side == "long" and price <= pos.stop_loss:
                    log.warning("stop_loss_triggered", symbol=symbol, price=price, stop=pos.stop_loss)
                    triggered.append(symbol)
                elif pos.side == "short" and price >= pos.stop_loss:
                    log.warning("stop_loss_triggered", symbol=symbol, price=price, stop=pos.stop_loss)
                    triggered.append(symbol)

            # Check take profit
            if pos.take_profit and symbol not in triggered:
                if pos.side == "long" and price >= pos.take_profit:
                    log.info("take_profit_triggered", symbol=symbol, price=price, tp=pos.take_profit)
                    triggered.append(symbol)
                elif pos.side == "short" and price <= pos.take_profit:
                    log.info("take_profit_triggered", symbol=symbol, price=price, tp=pos.take_profit)
                    triggered.append(symbol)

        return triggered

    def get_total_unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl or 0 for p in self._positions.values())

    def get_position(self, symbol: str) -> Position | None:
        return self._positions.get(symbol)
