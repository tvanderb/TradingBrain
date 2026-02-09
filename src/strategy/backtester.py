"""Backtester — test strategies against historical data.

Runs a strategy through stored OHLCV data and computes performance metrics.
Zero LLM cost — uses only local data and computation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd
import structlog

from src.shell.contract import (
    Action, Intent, OrderType, Portfolio, RiskLimits, Signal, StrategyBase, SymbolData,
    OpenPosition, ClosedTrade,
)

log = structlog.get_logger()


@dataclass
class BacktestTrade:
    symbol: str
    action: str
    qty: float
    price: float
    fee: float
    pnl: float
    pnl_pct: float
    timestamp: datetime


@dataclass
class BacktestResult:
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    gross_pnl: float = 0.0
    total_fees: float = 0.0
    net_pnl: float = 0.0
    win_rate: float = 0.0
    expectancy: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe: float = 0.0
    profit_factor: float = 0.0
    trades: list[BacktestTrade] = field(default_factory=list)
    daily_returns: list[float] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"Trades: {self.total_trades} | Win Rate: {self.win_rate:.1%} | "
            f"Net P&L: ${self.net_pnl:.2f} | Expectancy: ${self.expectancy:.4f} | "
            f"Sharpe: {self.sharpe:.2f} | Max DD: {self.max_drawdown_pct:.1%} | "
            f"Fees: ${self.total_fees:.2f}"
        )


class Backtester:
    """Simulates strategy execution against historical candle data."""

    def __init__(
        self,
        strategy: StrategyBase,
        risk_limits: RiskLimits,
        symbols: list[str],
        maker_fee_pct: float = 0.25,
        taker_fee_pct: float = 0.40,
        starting_cash: float = 200.0,
        per_pair_fees: dict[str, tuple[float, float]] | None = None,
    ) -> None:
        self._strategy = strategy
        self._risk_limits = risk_limits
        self._symbols = symbols
        self._maker_fee = maker_fee_pct
        self._taker_fee = taker_fee_pct
        self._starting_cash = starting_cash
        self._per_pair_fees = per_pair_fees or {}

    def _get_taker_fee(self, symbol: str) -> float:
        """Get taker fee for symbol (per-pair if available, else global)."""
        if symbol in self._per_pair_fees:
            return self._per_pair_fees[symbol][1]
        return self._taker_fee

    def run(self, candle_data: dict[str, pd.DataFrame], timeframe: str = "1h") -> BacktestResult:
        """Run backtest on historical data.

        Args:
            candle_data: dict of symbol -> DataFrame with OHLCV columns, datetime index
            timeframe: candle timeframe (used for scan interval alignment)

        Returns:
            BacktestResult with performance metrics
        """
        # Initialize
        cash = self._starting_cash
        positions: dict[str, dict] = {}
        all_trades: list[BacktestTrade] = []
        daily_values: list[float] = []
        peak_value = cash

        self._strategy.initialize(self._risk_limits, self._symbols)

        # Find common timestamps across all symbols
        all_timestamps = set()
        for df in candle_data.values():
            all_timestamps.update(df.index.tolist())
        timestamps = sorted(all_timestamps)

        if not timestamps:
            return BacktestResult()

        prev_day = None
        day_start_value = cash

        for ts in timestamps:
            # Build SymbolData for each symbol at this point
            markets = {}
            prices = {}
            for symbol in self._symbols:
                df = candle_data.get(symbol)
                if df is None:
                    continue

                # Get data up to current timestamp
                historical = df[df.index <= ts]
                if historical.empty:
                    continue

                current_price = float(historical.iloc[-1]["close"])
                prices[symbol] = current_price

                pair_fees = self._per_pair_fees.get(symbol)
                markets[symbol] = SymbolData(
                    symbol=symbol,
                    current_price=current_price,
                    candles_5m=historical.tail(8640),   # ~30 days of 5m
                    candles_1h=historical.tail(8760),
                    candles_1d=historical.tail(2555),
                    spread=0.001,
                    volume_24h=float(historical.tail(24)["volume"].sum()) if len(historical) >= 24 else 0,
                    maker_fee_pct=pair_fees[0] if pair_fees else self._maker_fee,
                    taker_fee_pct=pair_fees[1] if pair_fees else self._taker_fee,
                )

            if not markets:
                continue

            # Build portfolio snapshot
            open_positions = []
            for sym, pos in positions.items():
                cp = prices.get(sym, pos["avg_entry"])
                pnl = (cp - pos["avg_entry"]) * pos["qty"]
                pnl_pct = (cp - pos["avg_entry"]) / pos["avg_entry"] if pos["avg_entry"] > 0 else 0
                open_positions.append(OpenPosition(
                    symbol=sym, side="long", qty=pos["qty"],
                    avg_entry=pos["avg_entry"], current_price=cp,
                    unrealized_pnl=pnl, unrealized_pnl_pct=pnl_pct,
                    intent=Intent.DAY, stop_loss=pos.get("stop_loss"),
                    take_profit=pos.get("take_profit"),
                    opened_at=pos.get("opened_at", ts),
                ))

            total_value = cash + sum(
                pos["qty"] * prices.get(sym, pos["avg_entry"])
                for sym, pos in positions.items()
            )

            portfolio = Portfolio(
                cash=cash, total_value=total_value,
                positions=open_positions,
                recent_trades=[],
                daily_pnl=total_value - day_start_value,
                total_pnl=total_value - self._starting_cash,
                fees_today=0.0,
            )

            # Call strategy
            try:
                signals = self._strategy.analyze(markets, portfolio, ts)
            except Exception as e:
                log.warning("backtest.strategy_error", error=str(e), ts=str(ts))
                continue

            # Execute signals
            for signal in signals:
                price = prices.get(signal.symbol)
                if price is None:
                    continue

                fee_pct = self._get_taker_fee(signal.symbol) / 100

                if signal.action == Action.BUY and signal.symbol not in positions:
                    trade_value = total_value * signal.size_pct
                    if trade_value > cash:
                        continue
                    qty = trade_value / price
                    fee = trade_value * fee_pct
                    cash -= (trade_value + fee)
                    positions[signal.symbol] = {
                        "qty": qty, "avg_entry": price,
                        "entry_fee": fee,
                        "stop_loss": signal.stop_loss, "take_profit": signal.take_profit,
                        "opened_at": ts,
                    }
                    self._strategy.on_fill(signal.symbol, Action.BUY, qty, price, signal.intent)

                elif signal.action in (Action.SELL, Action.CLOSE) and signal.symbol in positions:
                    pos = positions[signal.symbol]
                    qty = pos["qty"]
                    sale = qty * price
                    exit_fee = sale * fee_pct
                    entry_fee = pos.get("entry_fee", 0.0)
                    fee = entry_fee + exit_fee
                    pnl = (price - pos["avg_entry"]) * qty - fee
                    pnl_pct = pnl / (pos["avg_entry"] * qty) if pos["avg_entry"] * qty > 0 else 0.0
                    cash += (sale - fee)

                    all_trades.append(BacktestTrade(
                        symbol=signal.symbol, action=signal.action.value,
                        qty=qty, price=price, fee=fee, pnl=pnl, pnl_pct=pnl_pct, timestamp=ts,
                    ))

                    self._strategy.on_fill(signal.symbol, signal.action, qty, price, signal.intent)
                    self._strategy.on_position_closed(signal.symbol, pnl, pnl_pct)
                    del positions[signal.symbol]

            # Check stop-loss / take-profit on existing positions (skip same-bar entries)
            for sym in list(positions.keys()):
                pos = positions[sym]
                if pos.get("opened_at") == ts:
                    continue  # Don't trigger SL/TP on the same bar as entry
                price = prices.get(sym)
                if price is None:
                    continue

                triggered = False
                if pos.get("stop_loss") and price <= pos["stop_loss"]:
                    triggered = True
                elif pos.get("take_profit") and price >= pos["take_profit"]:
                    triggered = True

                if triggered:
                    qty = pos["qty"]
                    sale = qty * price
                    exit_fee = sale * (self._get_taker_fee(sym) / 100)
                    entry_fee = pos.get("entry_fee", 0.0)
                    fee = entry_fee + exit_fee
                    pnl = (price - pos["avg_entry"]) * qty - fee
                    pnl_pct = pnl / (pos["avg_entry"] * qty) if pos["avg_entry"] * qty > 0 else 0.0
                    cash += (sale - fee)
                    all_trades.append(BacktestTrade(
                        symbol=sym, action="CLOSE", qty=qty, price=price,
                        fee=fee, pnl=pnl, pnl_pct=pnl_pct, timestamp=ts,
                    ))
                    self._strategy.on_position_closed(sym, pnl, pnl_pct)
                    del positions[sym]

            # Track daily values
            current_day = ts.date() if hasattr(ts, 'date') else None
            if current_day and current_day != prev_day:
                daily_values.append(total_value)
                peak_value = max(peak_value, total_value)
                day_start_value = total_value
                prev_day = current_day

        # Compute metrics
        result = BacktestResult(trades=all_trades)
        result.total_trades = len(all_trades)
        result.wins = sum(1 for t in all_trades if t.pnl > 0)
        result.losses = sum(1 for t in all_trades if t.pnl <= 0)
        result.total_fees = float(sum(t.fee for t in all_trades))
        result.gross_pnl = float(sum(t.pnl + t.fee for t in all_trades))
        result.net_pnl = float(sum(t.pnl for t in all_trades))
        result.win_rate = result.wins / result.total_trades if result.total_trades > 0 else 0

        # Expectancy
        if result.total_trades > 0:
            avg_win = sum(t.pnl for t in all_trades if t.pnl > 0) / max(result.wins, 1)
            avg_loss = abs(sum(t.pnl for t in all_trades if t.pnl <= 0)) / max(result.losses, 1)
            result.expectancy = (result.win_rate * avg_win) - ((1 - result.win_rate) * avg_loss)

        # Profit factor
        gross_profit = sum(t.pnl for t in all_trades if t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in all_trades if t.pnl <= 0))
        result.profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')

        # Max drawdown
        if daily_values:
            peak = daily_values[0]
            max_dd = 0
            for v in daily_values:
                if v > peak:
                    peak = v
                dd = (peak - v) / peak if peak > 0 else 0
                max_dd = max(max_dd, dd)
            result.max_drawdown_pct = max_dd

            # Daily returns for Sharpe
            returns = []
            for i in range(1, len(daily_values)):
                r = (daily_values[i] - daily_values[i-1]) / daily_values[i-1]
                returns.append(r)
            result.daily_returns = returns

            if returns and len(returns) > 1:
                mean_r = np.mean(returns)
                std_r = np.std(returns)
                result.sharpe = (mean_r / std_r * (365 ** 0.5)) if std_r > 0 else 0  # crypto: 365 days

        return result
