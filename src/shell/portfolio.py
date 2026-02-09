"""Portfolio Tracker — position management and P&L tracking.

Handles both paper and live trading. Part of the rigid shell.
Maintains positions, executes signals, tracks performance.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import structlog

from src.shell.config import Config
from src.shell.contract import (
    Action, Intent, OpenPosition, ClosedTrade, Portfolio, Signal, OrderType,
)
from src.shell.database import Database
from src.shell.kraken import KrakenREST

log = structlog.get_logger()


def _safe_intent(value: str) -> Intent:
    """Parse Intent from string, defaulting to DAY on invalid values."""
    try:
        return Intent[value]
    except KeyError:
        log.warning("portfolio.invalid_intent", value=value)
        return Intent.DAY


class PortfolioTracker:
    """Tracks positions, executes trades, computes P&L."""

    def __init__(self, config: Config, db: Database, kraken: KrakenREST) -> None:
        self._config = config
        self._db = db
        self._kraken = kraken
        self._positions: dict[str, dict] = {}  # symbol -> position dict
        self._cash: float = config.paper_balance_usd if config.is_paper() else 0.0
        self._starting_cash: float = self._cash
        self._fees_today: float = 0.0
        self._daily_start_value: float = 0.0

    @property
    def cash(self) -> float:
        return self._cash

    @property
    def position_count(self) -> int:
        return len(self._positions)

    @property
    def daily_start_value(self) -> float:
        return self._daily_start_value

    async def initialize(self) -> None:
        """Load positions from DB on startup."""
        rows = await self._db.fetchall("SELECT * FROM positions")
        for row in rows:
            self._positions[row["symbol"]] = dict(row)
        log.info("portfolio.loaded", positions=len(self._positions), cash=self._cash)

        # Load cash from last daily snapshot if available
        last_snap = await self._db.fetchone(
            "SELECT cash, portfolio_value FROM daily_performance ORDER BY date DESC LIMIT 1"
        )
        if last_snap and last_snap.get("cash") is not None:
            self._cash = last_snap["cash"]
        elif self._config.is_paper():
            self._cash = self._config.paper_balance_usd
        else:
            # Live mode — fetch balance from Kraken
            try:
                balances = await self._kraken.get_balance()
                self._cash = balances.get("ZUSD", balances.get("USD", 0.0))
                log.info("portfolio.live_balance_loaded", cash=self._cash)
            except Exception as e:
                log.warning("portfolio.live_balance_failed", error=str(e))

        # Use last daily snapshot to preserve daily P&L across restarts
        if last_snap and last_snap.get("portfolio_value") is not None:
            self._daily_start_value = last_snap["portfolio_value"]
        else:
            self._daily_start_value = await self.total_value()

    async def total_value(self) -> float:
        """Cash + sum of position values at current prices."""
        position_value = sum(
            p["qty"] * p.get("current_price", p["avg_entry"])
            for p in self._positions.values()
        )
        return self._cash + position_value

    def get_position_value(self, symbol: str) -> float:
        pos = self._positions.get(symbol)
        if not pos:
            return 0.0
        return pos["qty"] * pos.get("current_price", pos["avg_entry"])

    async def get_portfolio(self, prices: dict[str, float]) -> Portfolio:
        """Build a Portfolio snapshot for the Strategy Module."""
        # Update prices
        for symbol, price in prices.items():
            if symbol in self._positions:
                self._positions[symbol]["current_price"] = price

        open_positions = []
        for sym, p in self._positions.items():
            entry = p["avg_entry"]
            current = p.get("current_price", entry)
            qty = p["qty"]
            pnl = (current - entry) * qty  # Long-only system
            pnl_pct = pnl / (entry * qty) if entry * qty > 0 else 0.0

            open_positions.append(OpenPosition(
                symbol=sym,
                side=p.get("side", "long"),
                qty=qty,
                avg_entry=entry,
                current_price=current,
                unrealized_pnl=pnl,
                unrealized_pnl_pct=pnl_pct,
                intent=_safe_intent(p.get("intent", "DAY")),
                stop_loss=p.get("stop_loss"),
                take_profit=p.get("take_profit"),
                opened_at=datetime.fromisoformat(p["opened_at"]) if p.get("opened_at") else datetime.now(),
            ))

        # Recent trades
        trade_rows = await self._db.fetchall(
            "SELECT * FROM trades WHERE closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT 100"
        )
        recent_trades = []
        for t in trade_rows:
            recent_trades.append(ClosedTrade(
                symbol=t["symbol"],
                side=t["side"],
                qty=t["qty"],
                entry_price=t["entry_price"],
                exit_price=t["exit_price"] or 0.0,
                pnl=t["pnl"] or 0.0,
                pnl_pct=t["pnl_pct"] or 0.0,
                fees=t["fees"] or 0.0,
                intent=_safe_intent(t.get("intent", "DAY")),
                opened_at=datetime.fromisoformat(t["opened_at"]) if t.get("opened_at") else datetime.now(),
                closed_at=datetime.fromisoformat(t["closed_at"]) if t.get("closed_at") else datetime.now(),
            ))

        tv = await self.total_value()
        total_pnl = tv - self._starting_cash
        daily_pnl = tv - self._daily_start_value

        return Portfolio(
            cash=self._cash,
            total_value=tv,
            positions=open_positions,
            recent_trades=recent_trades,
            daily_pnl=daily_pnl,
            total_pnl=total_pnl,
            fees_today=self._fees_today,
        )

    def _get_slippage(self, signal: Signal) -> float:
        """Get slippage as a fraction (e.g. 0.0005). Signal override > config default."""
        if signal.slippage_tolerance is not None:
            return signal.slippage_tolerance
        return self._config.default_slippage_factor

    async def execute_signal(
        self, signal: Signal, current_price: float, maker_fee: float, taker_fee: float,
        strategy_regime: str | None = None,
    ) -> dict | None:
        """Execute a signal. Returns trade info dict or None if failed."""

        if signal.action == Action.BUY:
            return await self._execute_buy(signal, current_price, maker_fee, taker_fee)
        elif signal.action == Action.SELL:
            return await self._execute_sell(signal, current_price, maker_fee, taker_fee, strategy_regime)
        elif signal.action == Action.CLOSE:
            return await self._execute_close(signal, current_price, maker_fee, taker_fee, strategy_regime)
        return None

    async def _execute_buy(
        self, signal: Signal, price: float, maker_fee: float, taker_fee: float
    ) -> dict | None:
        portfolio_value = await self.total_value()
        trade_value = portfolio_value * signal.size_pct

        # Apply fee
        fee_pct = maker_fee if signal.order_type == OrderType.LIMIT else taker_fee
        fee = trade_value * (fee_pct / 100)

        if trade_value + fee > self._cash:
            log.warning("portfolio.insufficient_cash", needed=trade_value + fee, available=self._cash)
            return None

        qty = trade_value / price

        if self._config.is_paper():
            # Paper: simulate fill with slippage
            slippage = price * self._get_slippage(signal)
            fill_price = price + slippage
            qty = trade_value / fill_price
            fee = trade_value * (fee_pct / 100)
        else:
            # Live: place order on Kraken
            # For limit orders, use slippage tolerance to set price above market
            limit_price = price if signal.order_type == OrderType.LIMIT else None
            if signal.order_type == OrderType.LIMIT and limit_price and not signal.limit_price:
                limit_price = price * (1 + self._get_slippage(signal))
            elif signal.limit_price:
                limit_price = signal.limit_price
            result = await self._kraken.place_order(
                signal.symbol,
                "buy",
                signal.order_type.value.lower(),
                qty,
                limit_price,
            )
            fill_price = price  # Will be updated by fill callback
            log.info("portfolio.order_placed", result=result)

        # Deduct cash
        self._cash -= (qty * fill_price + fee)
        self._fees_today += fee

        # Store position (including entry fee for accurate P&L on close)
        now = datetime.now().isoformat()
        pos = {
            "symbol": signal.symbol,
            "side": "long",
            "qty": qty,
            "avg_entry": fill_price,
            "current_price": fill_price,
            "entry_fee": fee,
            "stop_loss": signal.stop_loss,
            "take_profit": signal.take_profit,
            "intent": signal.intent.value,
            "opened_at": now,
            "updated_at": now,
        }

        # If position exists, average in
        if signal.symbol in self._positions:
            existing = self._positions[signal.symbol]
            total_qty = existing["qty"] + qty
            avg = (existing["avg_entry"] * existing["qty"] + fill_price * qty) / total_qty
            pos["qty"] = total_qty
            pos["avg_entry"] = avg
            pos["entry_fee"] = existing.get("entry_fee", 0.0) + fee
            pos["opened_at"] = existing["opened_at"]

        self._positions[signal.symbol] = pos

        # Save to DB
        await self._db.execute(
            """INSERT OR REPLACE INTO positions
               (symbol, side, qty, avg_entry, current_price, stop_loss, take_profit, intent, opened_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pos["symbol"], pos["side"], pos["qty"], pos["avg_entry"], pos["current_price"],
             pos["stop_loss"], pos["take_profit"], pos["intent"], pos["opened_at"], pos["updated_at"]),
        )
        await self._db.commit()

        log.info("portfolio.buy", symbol=signal.symbol, qty=round(qty, 8),
                 price=round(fill_price, 2), fee=round(fee, 4), intent=signal.intent.value)

        return {
            "symbol": signal.symbol, "action": "BUY", "qty": qty,
            "price": fill_price, "fee": fee, "intent": signal.intent.value,
        }

    async def _execute_sell(
        self, signal: Signal, price: float, maker_fee: float, taker_fee: float,
        strategy_regime: str | None = None,
    ) -> dict | None:
        """Partial sell of a position."""
        pos = self._positions.get(signal.symbol)
        if not pos:
            log.warning("portfolio.no_position_to_sell", symbol=signal.symbol)
            return None

        portfolio_value = await self.total_value()
        sell_value = portfolio_value * signal.size_pct
        qty_to_sell = min(sell_value / price, pos["qty"])

        return await self._close_qty(signal.symbol, qty_to_sell, price, maker_fee, taker_fee, signal, strategy_regime)

    async def _execute_close(
        self, signal: Signal, price: float, maker_fee: float, taker_fee: float,
        strategy_regime: str | None = None,
    ) -> dict | None:
        """Close entire position."""
        pos = self._positions.get(signal.symbol)
        if not pos:
            log.warning("portfolio.no_position_to_close", symbol=signal.symbol)
            return None

        return await self._close_qty(signal.symbol, pos["qty"], price, maker_fee, taker_fee, signal, strategy_regime)

    async def _close_qty(
        self, symbol: str, qty: float, price: float,
        maker_fee: float, taker_fee: float, signal: Signal,
        strategy_regime: str | None = None,
    ) -> dict | None:
        pos = self._positions[symbol]

        if self._config.is_paper():
            slippage = price * self._get_slippage(signal)
            fill_price = price - slippage  # Slippage works against us
        else:
            order_type = signal.order_type.value.lower()
            limit_price = signal.limit_price if signal.order_type == OrderType.LIMIT else None
            result = await self._kraken.place_order(symbol, "sell", order_type, qty, limit_price)
            fill_price = price
            log.info("portfolio.sell_order_placed", result=result)

        sale_value = qty * fill_price
        fee_pct = maker_fee if signal.order_type == OrderType.LIMIT else taker_fee
        fee = sale_value * (fee_pct / 100)

        self._cash += (sale_value - fee)
        self._fees_today += fee

        # Calculate P&L (include both entry and exit fees)
        entry = pos["avg_entry"]
        # Apportion entry fee proportionally for partial closes
        total_entry_fee = pos.get("entry_fee", 0.0)
        close_fraction = qty / pos["qty"] if pos["qty"] > 0 else 1.0
        entry_fee_portion = total_entry_fee * close_fraction
        total_fee = entry_fee_portion + fee
        pnl = (fill_price - entry) * qty - total_fee
        pnl_pct = pnl / (entry * qty) if entry * qty > 0 else 0.0

        # Record trade
        now = datetime.now().isoformat()
        await self._db.execute(
            """INSERT INTO trades
               (symbol, side, qty, entry_price, exit_price, pnl, pnl_pct, fees, intent, strategy_version, strategy_regime, opened_at, closed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (symbol, pos.get("side", "long"), qty, entry, fill_price, pnl, pnl_pct,
             total_fee, pos.get("intent", "DAY"), None, strategy_regime, pos.get("opened_at", now), now),
        )

        # Update or remove position
        remaining_qty = pos["qty"] - qty
        if remaining_qty <= 0.000001:
            del self._positions[symbol]
            await self._db.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))
        else:
            pos["qty"] = remaining_qty
            pos["entry_fee"] = total_entry_fee - entry_fee_portion
            pos["updated_at"] = now
            await self._db.execute(
                "UPDATE positions SET qty = ?, updated_at = ? WHERE symbol = ?",
                (remaining_qty, now, symbol),
            )

        await self._db.commit()

        log.info("portfolio.sell", symbol=symbol, qty=round(qty, 8),
                 price=round(fill_price, 2), pnl=round(pnl, 4), fee=round(fee, 4))

        return {
            "symbol": symbol, "action": signal.action.value, "qty": qty,
            "price": fill_price, "pnl": pnl, "pnl_pct": pnl_pct, "fee": fee,
            "intent": pos.get("intent", "DAY"),
        }

    async def update_prices(self, prices: dict[str, float]) -> list[dict]:
        """Update position prices and check stop-loss/take-profit triggers.
        Returns list of triggered positions that need closing."""
        triggered = []
        for symbol, price in prices.items():
            if symbol not in self._positions:
                continue
            pos = self._positions[symbol]
            pos["current_price"] = price

            # Check stop-loss
            if pos.get("stop_loss") and price <= pos["stop_loss"]:
                triggered.append({"symbol": symbol, "reason": "stop_loss", "price": price})

            # Check take-profit
            if pos.get("take_profit") and price >= pos["take_profit"]:
                triggered.append({"symbol": symbol, "reason": "take_profit", "price": price})

        return triggered

    def reset_daily(self) -> None:
        self._fees_today = 0.0

    async def snapshot_daily(self) -> None:
        """Record end-of-day performance snapshot."""
        # Use configured timezone for date boundary (not UTC)
        tz = ZoneInfo(self._config.timezone)
        today = datetime.now(tz).strftime("%Y-%m-%d")

        tv = await self.total_value()
        trades = await self._db.fetchall(
            "SELECT pnl, fees FROM trades WHERE closed_at >= ? AND pnl IS NOT NULL",
            (today,),
        )
        wins = sum(1 for t in trades if t["pnl"] > 0)
        losses = sum(1 for t in trades if t["pnl"] <= 0)
        total = len(trades)
        gross = sum(t["pnl"] for t in trades)

        # gross_pnl = price movement without fees; net_pnl = after fees (already in trade.pnl)
        # trade.pnl includes both entry + exit fees, so gross = sum(pnl) IS the net figure
        # Reconstruct true gross by adding fees back: gross_before_fees = net + total_fees
        net = gross  # trade.pnl already has fees subtracted
        fees_from_trades = sum(t["fees"] for t in trades if t.get("fees"))
        gross_before_fees = net + fees_from_trades

        await self._db.execute(
            """INSERT OR REPLACE INTO daily_performance
               (date, portfolio_value, cash, total_trades, wins, losses, gross_pnl, net_pnl, fees_total, win_rate)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (today, tv, self._cash, total, wins, losses, gross_before_fees, net,
             self._fees_today, wins / total if total > 0 else 0.0),
        )
        await self._db.commit()
        self._daily_start_value = tv
        log.info("portfolio.daily_snapshot", value=round(tv, 2), trades=total, pnl=round(gross, 4))
