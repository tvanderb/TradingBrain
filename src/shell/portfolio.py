"""Portfolio Tracker — position management and P&L tracking.

Handles both paper and live trading. Part of the rigid shell.
Maintains positions keyed by tag (globally unique identifier),
supporting multiple positions per symbol.
"""

from __future__ import annotations

import asyncio
import json
import time as _time
from datetime import datetime, timedelta, timezone
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
    """Tracks positions, executes trades, computes P&L.

    Positions are keyed by tag (globally unique). Multiple positions
    per symbol are supported. Tags are auto-generated when not provided.
    """

    def __init__(self, config: Config, db: Database, kraken: KrakenREST) -> None:
        self._config = config
        self._db = db
        self._kraken = kraken
        self._positions: dict[str, dict] = {}  # tag -> position dict
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

    def _generate_tag(self, symbol: str) -> str:
        """Auto-generate a unique tag for a new position: auto_{SYMBOL}_001."""
        clean = symbol.replace("/", "")
        existing = [
            t for t in self._positions
            if t.startswith(f"auto_{clean}_")
        ]
        idx = len(existing) + 1
        tag = f"auto_{clean}_{idx:03d}"
        # Handle collision (shouldn't happen, but be safe)
        while tag in self._positions:
            idx += 1
            tag = f"auto_{clean}_{idx:03d}"
        return tag

    def _resolve_position(self, signal: Signal) -> tuple[str, dict] | None:
        """Resolve which position a signal targets.

        - If signal.tag is set and exists, return that position.
        - If signal.tag is set but doesn't exist, return None.
        - If no tag: for SELL, return oldest position for symbol.
        - If no tag: for CLOSE, handled by caller (close all).
        - If no tag: for MODIFY, return None (ambiguous).
        """
        if signal.tag:
            pos = self._positions.get(signal.tag)
            if pos:
                return (signal.tag, pos)
            return None

        # No tag — find oldest position for this symbol
        symbol_positions = self._get_positions_for_symbol(signal.symbol)
        if symbol_positions:
            return symbol_positions[0]  # oldest first
        return None

    def _get_positions_for_symbol(self, symbol: str) -> list[tuple[str, dict]]:
        """Return all (tag, pos) pairs for a symbol, sorted by opened_at."""
        matches = [
            (tag, pos) for tag, pos in self._positions.items()
            if pos["symbol"] == symbol
        ]
        matches.sort(key=lambda x: x[1].get("opened_at", ""))
        return matches

    async def initialize(self) -> None:
        """Load positions from DB on startup."""
        rows = await self._db.fetchall("SELECT * FROM positions")
        for row in rows:
            self._positions[row["tag"]] = dict(row)
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
        """Aggregate position value across all tags for a symbol."""
        total = 0.0
        for pos in self._positions.values():
            if pos["symbol"] == symbol:
                total += pos["qty"] * pos.get("current_price", pos["avg_entry"])
        return total

    def refresh_prices(self, prices: dict[str, float]) -> None:
        """Update current_price on all positions matching symbols."""
        for tag, pos in self._positions.items():
            symbol = pos["symbol"]
            if symbol in prices:
                pos["current_price"] = prices[symbol]

    async def get_portfolio(self, prices: dict[str, float]) -> Portfolio:
        """Build a Portfolio snapshot for the Strategy Module."""
        # Update prices by symbol across all tags
        self.refresh_prices(prices)

        open_positions = []
        for tag, p in self._positions.items():
            entry = p["avg_entry"]
            current = p.get("current_price", entry)
            qty = p["qty"]
            pnl = (current - entry) * qty  # Long-only system
            pnl_pct = pnl / (entry * qty) if entry * qty > 0 else 0.0

            open_positions.append(OpenPosition(
                symbol=p["symbol"],
                side=p.get("side", "long"),
                qty=qty,
                avg_entry=entry,
                current_price=current,
                unrealized_pnl=pnl,
                unrealized_pnl_pct=pnl_pct,
                intent=_safe_intent(p.get("intent", "DAY")),
                stop_loss=p.get("stop_loss"),
                take_profit=p.get("take_profit"),
                opened_at=datetime.fromisoformat(p["opened_at"]) if p.get("opened_at") else datetime.now(timezone.utc),
                tag=tag,
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
                opened_at=datetime.fromisoformat(t["opened_at"]) if t.get("opened_at") else datetime.now(timezone.utc),
                closed_at=datetime.fromisoformat(t["closed_at"]) if t.get("closed_at") else datetime.now(timezone.utc),
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

    async def _confirm_fill(
        self, txid: str, tag: str, symbol: str, side: str, order_type: str,
        volume: float, purpose: str = "entry", timeout_seconds: int = 30,
        poll_interval: float = 2.0,
    ) -> dict:
        """Poll Kraken for actual fill data after placing an order.

        Returns dict with: fill_price, filled_volume, fee, cost, status.
        Raises TimeoutError if order doesn't fill in time.
        Raises RuntimeError if order is canceled/expired.
        """
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """INSERT INTO orders
               (txid, tag, symbol, side, order_type, volume, status, purpose, placed_at)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (txid, tag, symbol, side, order_type, volume, purpose, now),
        )
        await self._db.commit()

        deadline = _time.monotonic() + timeout_seconds
        while _time.monotonic() < deadline:
            try:
                order_info = await self._kraken.query_order(txid)
            except Exception as e:
                log.warning("confirm_fill.query_failed", txid=txid, error=str(e))
                await asyncio.sleep(poll_interval)
                continue

            status = order_info.get("status", "")

            if status == "closed":
                fill_price = float(order_info.get("price", 0))
                filled_volume = float(order_info.get("vol_exec", 0))
                fee = float(order_info.get("fee", 0))
                cost = float(order_info.get("cost", 0))
                filled_at = datetime.now(timezone.utc).isoformat()

                await self._db.execute(
                    """UPDATE orders SET status = 'filled', filled_volume = ?,
                       avg_fill_price = ?, fee = ?, cost = ?, filled_at = ?,
                       kraken_response = ? WHERE txid = ?""",
                    (filled_volume, fill_price, fee, cost, filled_at,
                     json.dumps(order_info), txid),
                )
                await self._db.commit()

                log.info("confirm_fill.success", txid=txid, symbol=symbol,
                         fill_price=fill_price, filled_volume=filled_volume, fee=fee)
                return {
                    "fill_price": fill_price,
                    "filled_volume": filled_volume,
                    "fee": fee,
                    "cost": cost,
                    "status": "filled",
                }

            if status in ("canceled", "expired"):
                await self._db.execute(
                    "UPDATE orders SET status = ? WHERE txid = ?", (status, txid),
                )
                await self._db.commit()
                raise RuntimeError(f"Order {txid} {status}")

            await asyncio.sleep(poll_interval)

        # Timeout — one final check (order may have filled between last poll and timeout)
        try:
            final_info = await self._kraken.query_order(txid)
            if final_info.get("status") == "closed":
                fill_price = float(final_info.get("price", 0))
                filled_volume = float(final_info.get("vol_exec", 0))
                fee = float(final_info.get("fee", 0))
                cost = float(final_info.get("cost", 0))
                filled_at = datetime.now(timezone.utc).isoformat()
                await self._db.execute(
                    """UPDATE orders SET status = 'filled', filled_volume = ?,
                       avg_fill_price = ?, fee = ?, cost = ?, filled_at = ?,
                       kraken_response = ? WHERE txid = ?""",
                    (filled_volume, fill_price, fee, cost, filled_at,
                     json.dumps(final_info), txid),
                )
                await self._db.commit()
                log.info("confirm_fill.success_at_timeout", txid=txid, symbol=symbol,
                         fill_price=fill_price, filled_volume=filled_volume, fee=fee)
                return {
                    "fill_price": fill_price, "filled_volume": filled_volume,
                    "fee": fee, "cost": cost, "status": "filled",
                }
            # Check for partial fills — process what was filled instead of losing it
            vol_exec = float(final_info.get("vol_exec", 0))
            if vol_exec > 0:
                fill_price = float(final_info.get("price", 0))
                fee = float(final_info.get("fee", 0))
                cost = float(final_info.get("cost", 0))
                filled_at = datetime.now(timezone.utc).isoformat()
                await self._db.execute(
                    """UPDATE orders SET status = 'partial_fill', filled_volume = ?,
                       avg_fill_price = ?, fee = ?, cost = ?, filled_at = ?,
                       kraken_response = ? WHERE txid = ?""",
                    (vol_exec, fill_price, fee, cost, filled_at,
                     json.dumps(final_info), txid),
                )
                await self._db.commit()
                # Cancel the remaining unfilled portion to prevent unexpected later fills
                try:
                    await self._kraken.cancel_order(txid)
                    log.info("confirm_fill.partial_remainder_canceled", txid=txid)
                except Exception as cancel_err:
                    log.warning("confirm_fill.partial_cancel_failed", txid=txid, error=str(cancel_err))
                log.warning("confirm_fill.partial_at_timeout", txid=txid,
                            vol_exec=vol_exec, volume=volume, fill_price=fill_price)
                return {
                    "fill_price": fill_price, "filled_volume": vol_exec,
                    "fee": fee, "cost": cost, "status": "partial_fill",
                }
        except Exception:
            pass

        await self._db.execute(
            "UPDATE orders SET status = 'timeout' WHERE txid = ?", (txid,),
        )
        await self._db.commit()
        raise TimeoutError(f"Order {txid} did not fill within {timeout_seconds}s")

    async def execute_signal(
        self, signal: Signal, current_price: float, maker_fee: float, taker_fee: float,
        strategy_regime: str | None = None, strategy_version: str | None = None,
    ) -> dict | list[dict] | None:
        """Execute a signal. Returns trade info dict, list (multi-close), or None if failed.

        Returns list[dict] only when CLOSE has no tag (closes all positions for symbol).
        """
        if signal.action == Action.BUY:
            return await self._execute_buy(signal, current_price, maker_fee, taker_fee, strategy_version)
        elif signal.action == Action.SELL:
            return await self._execute_sell(signal, current_price, maker_fee, taker_fee, strategy_regime, strategy_version)
        elif signal.action == Action.CLOSE:
            # No-tag CLOSE = close ALL positions for this symbol
            if not signal.tag:
                symbol_positions = self._get_positions_for_symbol(signal.symbol)
                if not symbol_positions:
                    log.warning("portfolio.no_position_to_close", symbol=signal.symbol)
                    return None
                if len(symbol_positions) == 1:
                    # Single position — return dict (not list) for backwards compat
                    tag, pos = symbol_positions[0]
                    return await self._close_qty(tag, pos["qty"], current_price, maker_fee, taker_fee, signal, strategy_regime, strategy_version)
                # Multiple positions — close all, return list
                results = []
                for tag, pos in symbol_positions:
                    r = await self._close_qty(tag, pos["qty"], current_price, maker_fee, taker_fee, signal, strategy_regime, strategy_version)
                    if r:
                        results.append(r)
                return results if results else None
            else:
                return await self._execute_close(signal, current_price, maker_fee, taker_fee, strategy_regime, strategy_version)
        elif signal.action == Action.MODIFY:
            return await self._execute_modify(signal)
        return None

    async def _execute_buy(
        self, signal: Signal, price: float, maker_fee: float, taker_fee: float,
        strategy_version: str | None = None,
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
            # Paper: simulate fill (slippage only for market orders)
            if signal.order_type == OrderType.MARKET:
                slippage = price * self._get_slippage(signal)
                fill_price = price + slippage
            else:
                fill_price = signal.limit_price if signal.limit_price else price
            qty = trade_value / fill_price
            fee = trade_value * (fee_pct / 100)
        else:
            # Live: place order on Kraken with fill confirmation
            # Generate tag early for order tracking
            tag_for_order = signal.tag or self._generate_tag(signal.symbol)
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
            txid = (result.get("txid") or [None])[0]
            if not txid:
                log.error("portfolio.no_txid", symbol=signal.symbol, result=result)
                return None

            try:
                fill_data = await self._confirm_fill(
                    txid, tag_for_order, signal.symbol, "buy",
                    signal.order_type.value.lower(), qty, purpose="entry",
                )
                fill_price = fill_data["fill_price"]
                qty = fill_data["filled_volume"]
                fee = fill_data["fee"]
            except (TimeoutError, RuntimeError) as e:
                log.error("portfolio.fill_failed", symbol=signal.symbol, txid=txid, error=str(e))
                try:
                    await self._kraken.cancel_order(txid)
                except Exception:
                    pass
                return None

            # Override tag with the one we generated for order tracking
            signal = Signal(
                symbol=signal.symbol, action=signal.action, size_pct=signal.size_pct,
                confidence=signal.confidence, intent=signal.intent,
                reasoning=signal.reasoning, tag=tag_for_order,
                stop_loss=signal.stop_loss, take_profit=signal.take_profit,
                order_type=signal.order_type, limit_price=signal.limit_price,
                slippage_tolerance=signal.slippage_tolerance,
            )

        # Deduct cash (live buys may exceed pre-check due to price movement)
        actual_cost = qty * fill_price + fee
        if actual_cost > self._cash:
            log.critical("portfolio.cash_negative_after_fill",
                         actual_cost=actual_cost, available=self._cash,
                         symbol=signal.symbol, fill_price=fill_price,
                         note="Order already filled on exchange — proceeding despite negative cash")
        self._cash -= actual_cost
        self._fees_today += fee

        # Resolve tag: if signal has a tag and it exists, average in
        tag = signal.tag
        existing = self._positions.get(tag) if tag else None

        if tag and existing:
            # Average into existing tagged position
            pos = existing
            total_qty = pos["qty"] + qty
            avg = (pos["avg_entry"] * pos["qty"] + fill_price * qty) / total_qty
            pos["qty"] = total_qty
            pos["avg_entry"] = avg
            pos["entry_fee"] = pos.get("entry_fee", 0.0) + fee
            pos["updated_at"] = datetime.now(timezone.utc).isoformat()
            # Preserve existing SL/TP if new signal doesn't specify them
            if signal.stop_loss is not None:
                pos["stop_loss"] = signal.stop_loss
            if signal.take_profit is not None:
                pos["take_profit"] = signal.take_profit
            pos["current_price"] = fill_price

            # Update DB
            await self._db.execute(
                """UPDATE positions SET qty = ?, avg_entry = ?, current_price = ?,
                   entry_fee = ?, stop_loss = ?, take_profit = ?, updated_at = ?
                   WHERE tag = ?""",
                (pos["qty"], pos["avg_entry"], pos["current_price"],
                 pos["entry_fee"], pos["stop_loss"], pos["take_profit"],
                 pos["updated_at"], tag),
            )
        else:
            # New position
            if not tag:
                tag = self._generate_tag(signal.symbol)

            now = datetime.now(timezone.utc).isoformat()
            pos = {
                "symbol": signal.symbol,
                "tag": tag,
                "side": "long",
                "qty": qty,
                "avg_entry": fill_price,
                "current_price": fill_price,
                "entry_fee": fee,
                "stop_loss": signal.stop_loss,
                "take_profit": signal.take_profit,
                "intent": signal.intent.value,
                "strategy_version": strategy_version,
                "opened_at": now,
                "updated_at": now,
            }
            self._positions[tag] = pos

            # Insert into DB
            await self._db.execute(
                """INSERT INTO positions
                   (symbol, tag, side, qty, avg_entry, current_price, entry_fee, stop_loss, take_profit, intent, strategy_version, opened_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (pos["symbol"], tag, pos["side"], pos["qty"], pos["avg_entry"],
                 pos["current_price"], pos["entry_fee"], pos["stop_loss"],
                 pos["take_profit"], pos["intent"], pos["strategy_version"],
                 pos["opened_at"], pos["updated_at"]),
            )

        await self._db.commit()

        log.info("portfolio.buy", symbol=signal.symbol, tag=tag, qty=round(qty, 8),
                 price=round(fill_price, 2), fee=round(fee, 4), intent=signal.intent.value)

        # Place exchange-native SL/TP (live mode only, no-op for paper)
        # Check both signal SL/TP AND existing position SL/TP (average-in without explicit SL/TP)
        effective_sl = signal.stop_loss or (pos.get("stop_loss") if existing else None)
        effective_tp = signal.take_profit or (pos.get("take_profit") if existing else None)
        if effective_sl or effective_tp:
            entry_txid = None
            if not self._config.is_paper():
                # Cancel old SL/TP before placing new ones (critical for average-in qty update)
                if existing:
                    await self._cancel_exchange_sl_tp(tag)
                # Retrieve the entry txid from the orders table
                order_row = await self._db.fetchone(
                    "SELECT txid FROM orders WHERE tag = ? AND purpose = 'entry' ORDER BY id DESC LIMIT 1",
                    (tag,),
                )
                entry_txid = order_row["txid"] if order_row else None
            # Use total position qty (not just new fill qty) for SL/TP sizing
            sl_tp_qty = pos["qty"] if existing else qty
            await self._place_exchange_sl_tp(
                tag, signal.symbol, sl_tp_qty, effective_sl, effective_tp,
                entry_txid=entry_txid,
            )

        return {
            "symbol": signal.symbol, "action": "BUY", "qty": qty,
            "price": fill_price, "fee": fee, "intent": signal.intent.value,
            "tag": tag,
        }

    async def _execute_sell(
        self, signal: Signal, price: float, maker_fee: float, taker_fee: float,
        strategy_regime: str | None = None, strategy_version: str | None = None,
    ) -> dict | None:
        """Partial sell of a position. No tag = sell oldest for symbol."""
        resolved = self._resolve_position(signal)
        if not resolved:
            log.warning("portfolio.no_position_to_sell", symbol=signal.symbol, tag=signal.tag)
            return None

        tag, pos = resolved
        portfolio_value = await self.total_value()
        sell_value = portfolio_value * signal.size_pct
        qty_to_sell = min(sell_value / price, pos["qty"])

        return await self._close_qty(tag, qty_to_sell, price, maker_fee, taker_fee, signal, strategy_regime, strategy_version)

    async def _execute_close(
        self, signal: Signal, price: float, maker_fee: float, taker_fee: float,
        strategy_regime: str | None = None, strategy_version: str | None = None,
    ) -> dict | None:
        """Close entire position by tag."""
        resolved = self._resolve_position(signal)
        if not resolved:
            log.warning("portfolio.no_position_to_close", symbol=signal.symbol, tag=signal.tag)
            return None

        tag, pos = resolved
        return await self._close_qty(tag, pos["qty"], price, maker_fee, taker_fee, signal, strategy_regime, strategy_version)

    async def _close_qty(
        self, tag: str, qty: float, price: float,
        maker_fee: float, taker_fee: float, signal: Signal,
        strategy_regime: str | None = None, strategy_version: str | None = None,
    ) -> dict | None:
        pos = self._positions[tag]
        symbol = pos["symbol"]

        # Cancel exchange-native SL/TP before closing (full close only)
        sl_tp_canceled = False
        if qty >= pos["qty"] - 0.000001:
            await self._cancel_exchange_sl_tp(tag)
            sl_tp_canceled = True

        # Use version from position if not provided (e.g., SL/TP triggered by position monitor)
        if strategy_version is None:
            strategy_version = pos.get("strategy_version")

        if self._config.is_paper():
            # Paper: simulate fill (slippage only for market orders)
            if signal.order_type == OrderType.MARKET:
                slippage = price * self._get_slippage(signal)
                fill_price = price - slippage  # Slippage works against us
            else:
                fill_price = signal.limit_price if signal.limit_price else price
            fee_pct = maker_fee if signal.order_type == OrderType.LIMIT else taker_fee
            sale_value = qty * fill_price
            fee = sale_value * (fee_pct / 100)
        else:
            order_type = signal.order_type.value.lower()
            if signal.order_type == OrderType.LIMIT and not signal.limit_price:
                limit_price = price * (1 - self._get_slippage(signal))
            elif signal.limit_price:
                limit_price = signal.limit_price
            else:
                limit_price = None
            result = await self._kraken.place_order(symbol, "sell", order_type, qty, limit_price)
            txid = (result.get("txid") or [None])[0]
            if not txid:
                log.error("portfolio.no_txid_sell", symbol=symbol, result=result)
                if sl_tp_canceled:
                    await self._place_exchange_sl_tp(
                        tag, symbol, pos["qty"], pos.get("stop_loss"), pos.get("take_profit"),
                    )
                return None

            try:
                fill_data = await self._confirm_fill(
                    txid, tag, symbol, "sell", order_type, qty, purpose="exit",
                )
                fill_price = fill_data["fill_price"]
                qty = fill_data["filled_volume"]
                fee = fill_data["fee"]
            except (TimeoutError, RuntimeError) as e:
                log.error("portfolio.sell_fill_failed", symbol=symbol, txid=txid, error=str(e))
                try:
                    await self._kraken.cancel_order(txid)
                except Exception:
                    pass
                if sl_tp_canceled:
                    await self._place_exchange_sl_tp(
                        tag, symbol, pos["qty"], pos.get("stop_loss"), pos.get("take_profit"),
                    )
                return None
            sale_value = qty * fill_price

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
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """INSERT INTO trades
               (symbol, tag, side, qty, entry_price, exit_price, pnl, pnl_pct, fees, intent, strategy_version, strategy_regime, opened_at, closed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (symbol, tag, pos.get("side", "long"), qty, entry, fill_price, pnl, pnl_pct,
             total_fee, pos.get("intent", "DAY"), strategy_version, strategy_regime, pos.get("opened_at", now), now),
        )

        # Update or remove position
        remaining_qty = pos["qty"] - qty
        if remaining_qty <= 0.000001:
            del self._positions[tag]
            await self._db.execute("DELETE FROM positions WHERE tag = ?", (tag,))
        else:
            pos["qty"] = remaining_qty
            pos["entry_fee"] = total_entry_fee - entry_fee_portion
            pos["updated_at"] = now
            await self._db.execute(
                "UPDATE positions SET qty = ?, entry_fee = ?, updated_at = ? WHERE tag = ?",
                (remaining_qty, pos["entry_fee"], now, tag),
            )
            # Re-place SL/TP for remaining quantity (needed for both:
            #   1. full-close that partially filled (sl_tp_canceled=True)
            #   2. intentional partial sell (sl_tp_canceled=False, exchange SL/TP qty is now stale))
            if pos.get("stop_loss") or pos.get("take_profit"):
                if not sl_tp_canceled:
                    await self._cancel_exchange_sl_tp(tag)
                log.info("portfolio.partial_sl_tp_replace", tag=tag,
                         remaining=round(remaining_qty, 8))
                await self._place_exchange_sl_tp(
                    tag, symbol, remaining_qty, pos.get("stop_loss"), pos.get("take_profit"),
                )

        await self._db.commit()

        log.info("portfolio.sell", symbol=symbol, tag=tag, qty=round(qty, 8),
                 price=round(fill_price, 2), pnl=round(pnl, 4), fee=round(fee, 4))

        return {
            "symbol": symbol, "action": signal.action.value, "qty": qty,
            "price": fill_price, "pnl": pnl, "pnl_pct": pnl_pct, "fee": fee,
            "intent": pos.get("intent", "DAY"), "tag": tag,
        }

    async def _execute_modify(self, signal: Signal) -> dict | None:
        """Modify SL/TP/intent on an existing position. Zero fees, no trade recorded."""
        if not signal.tag:
            log.warning("portfolio.modify_no_tag", symbol=signal.symbol)
            return None

        pos = self._positions.get(signal.tag)
        if not pos:
            log.warning("portfolio.modify_not_found", tag=signal.tag)
            return None

        changes = {}
        if signal.stop_loss is not None:
            changes["stop_loss"] = signal.stop_loss
            pos["stop_loss"] = signal.stop_loss
        if signal.take_profit is not None:
            changes["take_profit"] = signal.take_profit
            pos["take_profit"] = signal.take_profit
        # Only update intent on MODIFY if explicitly set to non-default
        # (Signal defaults to DAY, so DAY could mean "not explicitly set" — skip to avoid downgrade)
        if signal.action != Action.MODIFY or signal.intent != Intent.DAY:
            if signal.intent.value != pos.get("intent", "DAY"):
                changes["intent"] = signal.intent.value
                pos["intent"] = signal.intent.value

        if not changes:
            log.info("portfolio.modify_no_changes", tag=signal.tag)
            return None

        now = datetime.now(timezone.utc).isoformat()
        pos["updated_at"] = now

        # Build dynamic UPDATE
        set_clauses = ["updated_at = ?"]
        params = [now]
        for col, val in changes.items():
            set_clauses.append(f"{col} = ?")
            params.append(val)
        params.append(signal.tag)

        await self._db.execute(
            f"UPDATE positions SET {', '.join(set_clauses)} WHERE tag = ?",
            tuple(params),
        )
        await self._db.commit()

        # Update exchange-native SL/TP if SL or TP changed
        if "stop_loss" in changes or "take_profit" in changes:
            await self._cancel_exchange_sl_tp(signal.tag)
            await self._place_exchange_sl_tp(
                signal.tag, pos["symbol"], pos["qty"],
                pos.get("stop_loss"), pos.get("take_profit"),
            )

        log.info("portfolio.modify", tag=signal.tag, changes=changes)

        return {
            "symbol": pos["symbol"], "action": "MODIFY", "tag": signal.tag,
            "changes": changes, "fee": 0, "qty": pos["qty"], "price": pos.get("current_price", pos["avg_entry"]),
        }

    async def _place_exchange_sl_tp(
        self, tag: str, symbol: str, qty: float,
        stop_loss: float | None, take_profit: float | None,
        entry_txid: str | None = None,
    ) -> None:
        """Place SL/TP orders on Kraken for a position. Paper mode: no-op."""
        if self._config.is_paper():
            return
        if not stop_loss and not take_profit:
            return

        sl_txid = None
        tp_txid = None
        now = datetime.now(timezone.utc).isoformat()

        # Place stop-loss
        if stop_loss:
            for attempt in range(3):
                try:
                    result = await self._kraken.place_conditional_order(
                        symbol, "sell", "stop-loss", qty, stop_loss,
                    )
                    sl_txid = (result.get("txid") or [None])[0]
                    if sl_txid:
                        await self._db.execute(
                            """INSERT INTO orders
                               (txid, tag, symbol, side, order_type, volume, status, purpose, placed_at)
                               VALUES (?, ?, ?, 'sell', 'stop-loss', ?, 'pending', 'stop_loss', ?)""",
                            (sl_txid, tag, symbol, qty, now),
                        )
                    break
                except Exception as e:
                    log.warning("exchange_sl.place_failed", tag=tag, attempt=attempt + 1, error=str(e))
                    if attempt < 2:
                        await asyncio.sleep(1)

        # Place take-profit
        if take_profit:
            for attempt in range(3):
                try:
                    result = await self._kraken.place_conditional_order(
                        symbol, "sell", "take-profit", qty, take_profit,
                    )
                    tp_txid = (result.get("txid") or [None])[0]
                    if tp_txid:
                        await self._db.execute(
                            """INSERT INTO orders
                               (txid, tag, symbol, side, order_type, volume, status, purpose, placed_at)
                               VALUES (?, ?, ?, 'sell', 'take-profit', ?, 'pending', 'take_profit', ?)""",
                            (tp_txid, tag, symbol, qty, now),
                        )
                    break
                except Exception as e:
                    log.warning("exchange_tp.place_failed", tag=tag, attempt=attempt + 1, error=str(e))
                    if attempt < 2:
                        await asyncio.sleep(1)

        if sl_txid or tp_txid:
            await self._db.execute(
                """INSERT OR REPLACE INTO conditional_orders
                   (tag, symbol, entry_txid, sl_txid, tp_txid, sl_price, tp_price, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'active')""",
                (tag, symbol, entry_txid, sl_txid, tp_txid, stop_loss, take_profit),
            )
            await self._db.commit()
            log.info("exchange_sl_tp.placed", tag=tag, sl_txid=sl_txid, tp_txid=tp_txid)
        else:
            log.error("exchange_sl_tp.all_failed", tag=tag,
                       note="Position unprotected on exchange — client-side SL/TP still active")

    async def _cancel_exchange_sl_tp(self, tag: str) -> None:
        """Cancel exchange-native SL/TP orders for a position. Paper mode: no-op."""
        if self._config.is_paper():
            return

        cond = await self._db.fetchone(
            "SELECT * FROM conditional_orders WHERE tag = ? AND status = 'active'", (tag,),
        )
        if not cond:
            return

        for txid_key in ("sl_txid", "tp_txid"):
            txid = cond.get(txid_key)
            if txid:
                try:
                    await self._kraken.cancel_order(txid)
                except Exception as e:
                    log.warning("exchange_sl_tp.cancel_failed", tag=tag, txid=txid, error=str(e))
                await self._db.execute(
                    "UPDATE orders SET status = 'canceled' WHERE txid = ?", (txid,),
                )

        await self._db.execute(
            "UPDATE conditional_orders SET status = 'canceled', updated_at = ? WHERE tag = ?",
            (datetime.now(timezone.utc).isoformat(), tag),
        )
        await self._db.commit()
        log.info("exchange_sl_tp.canceled", tag=tag)

    async def record_exchange_fill(
        self, tag: str, fill_price: float, filled_volume: float, fee: float,
    ) -> dict | None:
        """Record a trade filled by the exchange (SL/TP). No new exchange calls.

        Handles both full and partial fills. Updates cash, position, and trades table.
        Returns trade result dict, or None if position not found.
        """
        pos = self._positions.get(tag)
        if not pos:
            return None

        symbol = pos["symbol"]
        entry = pos["avg_entry"]
        entry_fee = pos.get("entry_fee", 0.0)
        close_fraction = min(filled_volume / pos["qty"], 1.0) if pos["qty"] > 0 else 1.0
        entry_fee_portion = entry_fee * close_fraction
        total_fee = entry_fee_portion + fee
        pnl = (fill_price - entry) * filled_volume - total_fee
        pnl_pct = pnl / (entry * filled_volume) if entry * filled_volume > 0 else 0.0

        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """INSERT INTO trades
               (symbol, tag, side, qty, entry_price, exit_price, pnl, pnl_pct, fees,
                intent, strategy_version, strategy_regime, opened_at, closed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (symbol, tag, pos.get("side", "long"), filled_volume, entry,
             fill_price, pnl, pnl_pct, total_fee, pos.get("intent", "DAY"),
             pos.get("strategy_version"), None, pos.get("opened_at", now), now),
        )

        # Update cash
        sale_value = filled_volume * fill_price
        self._cash += (sale_value - fee)
        self._fees_today += fee

        # Update or remove position (handles partial fills)
        remaining = pos["qty"] - filled_volume
        if remaining <= 0.000001:
            del self._positions[tag]
            await self._db.execute("DELETE FROM positions WHERE tag = ?", (tag,))
        else:
            pos["qty"] = remaining
            pos["entry_fee"] = entry_fee - entry_fee_portion
            pos["updated_at"] = now
            await self._db.execute(
                "UPDATE positions SET qty = ?, entry_fee = ?, updated_at = ? WHERE tag = ?",
                (remaining, pos["entry_fee"], now, tag),
            )

        await self._db.commit()

        log.info("portfolio.exchange_fill", symbol=symbol, tag=tag,
                 qty=round(filled_volume, 8), price=round(fill_price, 2),
                 pnl=round(pnl, 4), fee=round(fee, 4))

        return {
            "symbol": symbol, "action": "CLOSE", "qty": filled_volume,
            "price": fill_price, "pnl": pnl, "pnl_pct": pnl_pct,
            "fee": fee, "intent": pos.get("intent", "DAY"), "tag": tag,
        }

    async def update_prices(self, prices: dict[str, float]) -> list[dict]:
        """Update position prices and check stop-loss/take-profit triggers.
        Returns list of triggered positions that need closing."""
        triggered = []
        for tag, pos in list(self._positions.items()):
            symbol = pos["symbol"]
            if symbol not in prices:
                continue
            price = prices[symbol]
            pos["current_price"] = price

            # Check stop-loss (takes priority over take-profit)
            if pos.get("stop_loss") and price <= pos["stop_loss"]:
                triggered.append({"symbol": symbol, "tag": tag, "reason": "stop_loss", "price": price})
            elif pos.get("take_profit") and price >= pos["take_profit"]:
                triggered.append({"symbol": symbol, "tag": tag, "reason": "take_profit", "price": price})

        return triggered

    def reset_daily(self) -> None:
        self._fees_today = 0.0

    async def snapshot_daily(self) -> None:
        """Record end-of-day performance snapshot."""
        # Use configured timezone for date boundary (not UTC)
        tz = ZoneInfo(self._config.timezone)
        today = datetime.now(tz).strftime("%Y-%m-%d")
        tomorrow = (datetime.now(tz) + timedelta(days=1)).strftime("%Y-%m-%d")

        # Flush current prices to DB before snapshot query
        for tag, pos in self._positions.items():
            await self._db.execute(
                "UPDATE positions SET current_price = ?, updated_at = ? WHERE tag = ?",
                (pos.get("current_price", pos["avg_entry"]), datetime.now(timezone.utc).isoformat(), tag),
            )

        tv = await self.total_value()
        trades = await self._db.fetchall(
            "SELECT pnl, fees FROM trades WHERE closed_at >= ? AND closed_at < ? AND pnl IS NOT NULL",
            (today, tomorrow),
        )
        wins = sum(1 for t in trades if t["pnl"] > 0)
        losses = sum(1 for t in trades if t["pnl"] < 0)
        total = len(trades)

        # trade.pnl already has fees subtracted (net P&L per trade)
        net_pnl = sum(t["pnl"] for t in trades)
        fees_total = sum(t["fees"] for t in trades if t.get("fees"))
        gross_pnl = net_pnl + fees_total  # Price movement without fees

        await self._db.execute(
            """INSERT OR REPLACE INTO daily_performance
               (date, portfolio_value, cash, total_trades, wins, losses, gross_pnl, net_pnl, fees_total, win_rate)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (today, tv, self._cash, total, wins, losses, gross_pnl, net_pnl,
             fees_total, wins / total if total > 0 else 0.0),
        )
        await self._db.commit()
        self._daily_start_value = tv
        log.info("portfolio.daily_snapshot", value=round(tv, 2), trades=total, net_pnl=round(net_pnl, 4))
