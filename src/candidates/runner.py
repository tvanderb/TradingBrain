"""CandidateRunner — paper simulation engine for one candidate strategy slot.

Each runner maintains its own portfolio state (cash + positions) and trades
independently using the same market data as the active strategy. No exchange
API calls are ever made — all fills are simulated with slippage.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import structlog

from src.shell.contract import (
    Action,
    Intent,
    OpenPosition,
    Portfolio,
    RiskLimits,
    Signal,
    StrategyBase,
    SymbolData,
)

log = structlog.get_logger()


class CandidateRunner:
    """Paper simulation engine for one candidate strategy slot."""

    def __init__(
        self,
        slot: int,
        strategy: StrategyBase,
        version: str,
        initial_cash: float,
        initial_positions: list[dict],
        risk_limits: RiskLimits,
        symbols: list[str],
        slippage_factor: float = 0.0005,
        maker_fee_pct: float = 0.25,
        taker_fee_pct: float = 0.40,
    ) -> None:
        self.slot = slot
        self.version = version
        self._strategy = strategy
        self._cash = initial_cash
        self._positions: dict[str, dict] = {}  # tag -> position dict
        self._trades: list[dict] = []  # completed trades (not yet persisted)
        self._all_trades: list[dict] = []  # ALL trades — never cleared, used for stats
        self._risk_limits = risk_limits
        self._symbols = symbols
        self._slippage = slippage_factor
        self._maker_fee = maker_fee_pct
        self._taker_fee = taker_fee_pct
        self._next_tag_counter: dict[str, int] = {}  # symbol -> counter
        self._pending_signals: list[dict] = []
        self._current_regime: str | None = None

        # Clone initial positions with candidate tag prefix
        for pos in initial_positions:
            tag = f"c{slot}_{pos.get('tag', pos['symbol'])}"
            self._positions[tag] = {
                "symbol": pos["symbol"],
                "tag": tag,
                "side": pos.get("side", "long"),
                "qty": pos["qty"],
                "avg_entry": pos["avg_entry"],
                "current_price": pos.get("current_price", pos["avg_entry"]),
                "unrealized_pnl": 0.0,
                "entry_fee": pos.get("entry_fee", 0.0),
                "stop_loss": pos.get("stop_loss"),
                "take_profit": pos.get("take_profit"),
                "intent": pos.get("intent", "DAY"),
                "strategy_version": version,
                "opened_at": pos.get("opened_at", datetime.now(timezone.utc).isoformat()),
            }

    def _next_tag(self, symbol: str) -> str:
        """Generate auto-incrementing tag for new positions."""
        clean = symbol.replace("/", "")
        count = self._next_tag_counter.get(symbol, 0) + 1
        self._next_tag_counter[symbol] = count
        return f"c{self.slot}_{clean}_{count:03d}"

    def _build_portfolio(self, prices: dict[str, float]) -> Portfolio:
        """Build a Portfolio snapshot from current runner state."""
        # Update current prices
        for tag, pos in self._positions.items():
            sym = pos["symbol"]
            if sym in prices:
                price = prices[sym]
                pos["current_price"] = price
                pos["unrealized_pnl"] = (price - pos["avg_entry"]) * pos["qty"]
                # MAE tracking: worst drawdown from entry
                if price < pos["avg_entry"]:
                    dd = (pos["avg_entry"] - price) / pos["avg_entry"]
                    if dd > pos.get("max_adverse_excursion", 0.0):
                        pos["max_adverse_excursion"] = dd

        open_positions = []
        for tag, pos in self._positions.items():
            entry = pos["avg_entry"]
            current = pos.get("current_price", entry)
            pnl = (current - entry) * pos["qty"]
            pnl_pct = (current / entry - 1) if entry > 0 else 0.0
            try:
                intent = Intent[pos.get("intent", "DAY")]
            except KeyError:
                intent = Intent.DAY
            open_positions.append(OpenPosition(
                symbol=pos["symbol"],
                side=pos.get("side", "long"),
                qty=pos["qty"],
                avg_entry=entry,
                current_price=current,
                unrealized_pnl=pnl,
                unrealized_pnl_pct=pnl_pct,
                intent=intent,
                stop_loss=pos.get("stop_loss"),
                take_profit=pos.get("take_profit"),
                opened_at=datetime.fromisoformat(pos.get("opened_at", datetime.now(timezone.utc).isoformat())),
                tag=tag,
            ))

        position_value = sum(
            pos.get("current_price", pos["avg_entry"]) * pos["qty"]
            for pos in self._positions.values()
        )

        # Compute total PnL from completed trades (use _all_trades for accuracy after persist)
        total_pnl = sum(t.get("pnl", 0) or 0 for t in self._all_trades if t.get("pnl") is not None)

        return Portfolio(
            cash=self._cash,
            total_value=self._cash + position_value,
            positions=open_positions,
            recent_trades=[],  # Candidates don't need ClosedTrade objects
            daily_pnl=0.0,
            total_pnl=total_pnl,
            fees_today=0.0,
        )

    @property
    def total_value(self) -> float:
        """Current total value: cash + position market values."""
        position_value = sum(
            pos.get("current_price", pos["avg_entry"]) * pos["qty"]
            for pos in self._positions.values()
        )
        return self._cash + position_value

    def run_scan(
        self, markets: dict[str, SymbolData], timestamp: datetime
    ) -> list[dict]:
        """Run strategy.analyze() and process signals with paper fills.

        Returns list of trade result dicts. Called synchronously from an executor.
        """
        prices = {sym: data.current_price for sym, data in markets.items()}
        portfolio = self._build_portfolio(prices)

        # Run strategy with timeout protection (called from executor already)
        try:
            signals = self._strategy.analyze(dict(markets), portfolio, timestamp)
        except Exception as e:
            log.warning("candidate.strategy_error", slot=self.slot, error=str(e))
            return []

        if not signals:
            return []

        # Extract regime from strategy (if it exposes one)
        self._current_regime = getattr(self._strategy, 'regime', None)

        results = []
        for signal in signals:
            if not isinstance(signal, Signal):
                continue

            # Build signal record for persistence
            signal_record = {
                "symbol": signal.symbol,
                "action": signal.action.value,
                "size_pct": signal.size_pct,
                "confidence": signal.confidence,
                "intent": signal.intent.value if signal.intent else None,
                "reasoning": signal.reasoning,
                "strategy_regime": self._current_regime,
                "acted_on": 0,
                "rejected_reason": None,
                "tag": signal.tag,
            }

            # Validate: no SHORT
            if signal.action not in (Action.BUY, Action.SELL, Action.CLOSE, Action.MODIFY):
                signal_record["rejected_reason"] = "invalid_action"
                self._pending_signals.append(signal_record)
                continue

            # Valid symbol check
            if signal.symbol not in markets:
                signal_record["rejected_reason"] = "invalid_symbol"
                self._pending_signals.append(signal_record)
                continue

            price = prices.get(signal.symbol, 0)
            if price <= 0:
                signal_record["rejected_reason"] = "invalid_price"
                self._pending_signals.append(signal_record)
                continue

            result = self._execute_signal(signal, price)
            if result:
                signal_record["acted_on"] = 1
                if isinstance(result, list):
                    results.extend(result)
                    if result:
                        signal_record["tag"] = result[0].get("tag")
                else:
                    results.append(result)
                    signal_record["tag"] = result.get("tag")
            else:
                signal_record["rejected_reason"] = "execution_failed"

            self._pending_signals.append(signal_record)

        return results

    def get_new_signals(self) -> list[dict]:
        """Return signals accumulated since last persist, then clear."""
        signals = list(self._pending_signals)
        self._pending_signals.clear()
        return signals

    def check_sl_tp(self, prices: dict[str, float]) -> list[dict]:
        """Check stop-loss and take-profit on candidate positions.

        Returns list of trade result dicts for triggered positions.
        """
        results = []
        tags_to_check = list(self._positions.keys())

        for tag in tags_to_check:
            pos = self._positions.get(tag)
            if not pos:
                continue

            symbol = pos["symbol"]
            current_price = prices.get(symbol)
            if current_price is None:
                continue

            # Update current price
            pos["current_price"] = current_price
            pos["unrealized_pnl"] = (current_price - pos["avg_entry"]) * pos["qty"]

            # MAE tracking: worst drawdown from entry
            if current_price < pos["avg_entry"]:
                dd = (pos["avg_entry"] - current_price) / pos["avg_entry"]
                if dd > pos.get("max_adverse_excursion", 0.0):
                    pos["max_adverse_excursion"] = dd

            triggered_reason = None
            if pos.get("stop_loss") and current_price <= pos["stop_loss"]:
                triggered_reason = "stop_loss"
            elif pos.get("take_profit") and current_price >= pos["take_profit"]:
                triggered_reason = "take_profit"

            if triggered_reason:
                result = self._close_position(tag, current_price, triggered_reason)
                if result:
                    results.append(result)
                    log.info("candidate.sl_tp", slot=self.slot, tag=tag,
                             reason=triggered_reason, price=current_price)

        return results

    def _execute_signal(self, signal: Signal, price: float) -> dict | list[dict] | None:
        """Execute a single signal with paper fills."""
        action = signal.action

        if action == Action.BUY:
            return self._execute_buy(signal, price)
        elif action == Action.SELL:
            return self._execute_sell(signal, price)
        elif action == Action.CLOSE:
            return self._execute_close(signal, price)
        elif action == Action.MODIFY:
            return self._execute_modify(signal)
        return None

    def _execute_buy(self, signal: Signal, price: float) -> dict | None:
        """Execute a BUY signal — create or average into a position."""
        # Risk limit: max positions
        if len(self._positions) >= self._risk_limits.max_positions and not signal.tag:
            return None

        # Clamp size to risk limits
        size_pct = min(signal.size_pct, self._risk_limits.max_trade_pct)
        trade_value = self._cash * size_pct if size_pct > 0 else 0
        if trade_value <= 0:
            return None

        # Paper fill with slippage
        fill_price = price * (1 + self._slippage)
        fee = trade_value * self._taker_fee / 100
        qty = (trade_value - fee) / fill_price
        if qty <= 0:
            return None

        self._cash -= trade_value

        # Find or create position
        tag = signal.tag
        if tag and tag in self._positions:
            # Average in
            pos = self._positions[tag]
            old_qty = pos["qty"]
            old_entry = pos["avg_entry"]
            new_qty = old_qty + qty
            pos["avg_entry"] = (old_entry * old_qty + fill_price * qty) / new_qty
            pos["qty"] = new_qty
            pos["entry_fee"] = pos.get("entry_fee", 0) + fee
            if signal.stop_loss is not None:
                pos["stop_loss"] = signal.stop_loss
            if signal.take_profit is not None:
                pos["take_profit"] = signal.take_profit
        else:
            # New position
            tag = tag or self._next_tag(signal.symbol)
            self._positions[tag] = {
                "symbol": signal.symbol,
                "tag": tag,
                "side": "long",
                "qty": qty,
                "avg_entry": fill_price,
                "current_price": price,
                "unrealized_pnl": 0.0,
                "entry_fee": fee,
                "stop_loss": signal.stop_loss,
                "take_profit": signal.take_profit,
                "intent": signal.intent.value,
                "strategy_version": self.version,
                "opened_at": datetime.now(timezone.utc).isoformat(),
                "max_adverse_excursion": 0.0,
            }

        result = {
            "action": "BUY",
            "symbol": signal.symbol,
            "qty": qty,
            "price": fill_price,
            "fee": fee,
            "tag": tag,
            "intent": signal.intent.value,
        }

        log.info("candidate.trade", slot=self.slot, symbol=signal.symbol,
                 action="BUY", qty=qty, price=fill_price)
        return result

    def _execute_sell(self, signal: Signal, price: float) -> dict | None:
        """Execute a SELL signal — close oldest position for symbol (FIFO)."""
        tag = signal.tag
        if tag:
            if tag not in self._positions:
                return None
            return self._close_position(tag, price, "signal")
        else:
            # FIFO: find oldest position for this symbol
            oldest_tag = None
            oldest_time = None
            for t, pos in self._positions.items():
                if pos["symbol"] == signal.symbol:
                    opened = pos.get("opened_at", "")
                    if oldest_tag is None or opened < oldest_time:
                        oldest_tag = t
                        oldest_time = opened
            if oldest_tag:
                return self._close_position(oldest_tag, price, "signal")
        return None

    def _execute_close(self, signal: Signal, price: float) -> dict | list[dict] | None:
        """Execute a CLOSE signal — close all positions for symbol or specific tag."""
        if signal.tag:
            if signal.tag not in self._positions:
                return None
            return self._close_position(signal.tag, price, "signal")
        else:
            # Close ALL positions for this symbol
            results = []
            tags = [t for t, p in self._positions.items() if p["symbol"] == signal.symbol]
            for tag in tags:
                result = self._close_position(tag, price, "signal")
                if result:
                    results.append(result)
            return results if results else None

    def _execute_modify(self, signal: Signal) -> dict | None:
        """Execute a MODIFY signal — update SL/TP/intent."""
        if not signal.tag or signal.tag not in self._positions:
            return None
        pos = self._positions[signal.tag]
        if signal.stop_loss is not None:
            pos["stop_loss"] = signal.stop_loss
        if signal.take_profit is not None:
            pos["take_profit"] = signal.take_profit
        if signal.intent:
            pos["intent"] = signal.intent.value
        return {"action": "MODIFY", "symbol": pos["symbol"], "tag": signal.tag,
                "stop_loss": pos.get("stop_loss"), "take_profit": pos.get("take_profit")}

    def _close_position(self, tag: str, price: float, close_reason: str) -> dict | None:
        """Close a position and record the trade."""
        pos = self._positions.pop(tag, None)
        if not pos:
            return None

        fill_price = price * (1 - self._slippage)
        qty = pos["qty"]
        entry = pos["avg_entry"]
        exit_fee = fill_price * qty * self._taker_fee / 100
        entry_fee = pos.get("entry_fee", 0)
        total_fees = entry_fee + exit_fee

        gross_pnl = (fill_price - entry) * qty
        net_pnl = gross_pnl - total_fees
        pnl_pct = (fill_price / entry - 1) if entry > 0 else 0.0

        self._cash += fill_price * qty - exit_fee

        trade = {
            "action": "SELL",
            "symbol": pos["symbol"],
            "tag": tag,
            "side": "long",
            "qty": qty,
            "entry_price": entry,
            "exit_price": fill_price,
            "price": fill_price,
            "pnl": net_pnl,
            "pnl_pct": pnl_pct,
            "fees": total_fees,
            "fee": exit_fee,
            "intent": pos.get("intent", "DAY"),
            "strategy_version": self.version,
            "strategy_regime": getattr(self, '_current_regime', None),
            "close_reason": close_reason,
            "opened_at": pos.get("opened_at"),
            "closed_at": datetime.now(timezone.utc).isoformat(),
            "max_adverse_excursion": pos.get("max_adverse_excursion", 0.0),
        }
        self._trades.append(trade)
        self._all_trades.append(trade)

        log.info("candidate.trade", slot=self.slot, symbol=pos["symbol"],
                 action="SELL", pnl=round(net_pnl, 4), reason=close_reason)
        return trade

    def get_status(self) -> dict:
        """Summary status for orchestrator context and API."""
        wins = sum(1 for t in self._all_trades if (t.get("pnl") or 0) > 0)
        losses = sum(1 for t in self._all_trades if (t.get("pnl") or 0) <= 0 and t.get("pnl") is not None)
        total_pnl = sum(t.get("pnl", 0) or 0 for t in self._all_trades)
        trade_count = len(self._all_trades)

        return {
            "slot": self.slot,
            "version": self.version,
            "status": "running",
            "cash": round(self._cash, 2),
            "total_value": round(self.total_value, 2),
            "position_count": len(self._positions),
            "trade_count": trade_count,
            "wins": wins,
            "losses": losses,
            "pnl": round(total_pnl, 4),
            "win_rate": round(wins / trade_count, 4) if trade_count > 0 else 0.0,
        }

    def get_new_trades(self) -> list[dict]:
        """Return trades accumulated since last persist, then clear."""
        trades = list(self._trades)
        self._trades.clear()
        return trades

    def get_positions(self) -> dict[str, dict]:
        """Return current positions dict."""
        return dict(self._positions)

    @property
    def code(self) -> str | None:
        """Get strategy source code if available (set by manager)."""
        return getattr(self, "_code", None)
