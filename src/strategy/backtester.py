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
        slippage_factor: float = 0.0005,
    ) -> None:
        self._strategy = strategy
        self._risk_limits = risk_limits
        self._symbols = symbols
        self._maker_fee = maker_fee_pct
        self._taker_fee = taker_fee_pct
        self._starting_cash = starting_cash
        self._per_pair_fees = per_pair_fees or {}
        self._slippage = slippage_factor  # applied to fill prices (buy higher, sell lower)
        self._tag_counter: dict[str, int] = {}  # symbol -> counter for auto-tags

    def _bt_tag(self, symbol: str) -> str:
        """Generate a unique backtest tag for a new position."""
        clean = symbol.replace("/", "")
        self._tag_counter[clean] = self._tag_counter.get(clean, 0) + 1
        return f"bt_{clean}_{self._tag_counter[clean]:03d}"

    def _get_taker_fee(self, symbol: str) -> float:
        """Get taker fee for symbol (per-pair if available, else global)."""
        if symbol in self._per_pair_fees:
            return self._per_pair_fees[symbol][1]
        return self._taker_fee

    def _resolve_position(self, signal: Signal, positions: dict[str, dict]) -> tuple[str, dict] | None:
        """Resolve position by tag (explicit) or oldest for symbol."""
        if signal.tag and signal.tag in positions:
            return (signal.tag, positions[signal.tag])
        # No tag — find oldest for this symbol
        matches = [
            (tag, pos) for tag, pos in positions.items()
            if pos.get("symbol") == signal.symbol
        ]
        if matches:
            matches.sort(key=lambda x: x[1].get("opened_at", ""))
            return matches[0]
        return None

    def _has_position_for_symbol(self, symbol: str, positions: dict[str, dict]) -> bool:
        """Check if any position exists for the given symbol."""
        return any(pos.get("symbol") == symbol for pos in positions.values())

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
        positions: dict[str, dict] = {}  # tag -> position dict
        all_trades: list[BacktestTrade] = []
        daily_values: list[float] = []
        peak_value = cash
        self._tag_counter = {}

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
        daily_pnl = 0.0
        halted_today = False
        drawdown_halted = False
        total_value = cash  # Track across iterations for day boundary detection

        for ts in timestamps:
            # Day boundary check — BEFORE trading so day_start_value is correct
            current_day = ts.date() if hasattr(ts, 'date') else None
            if current_day and current_day != prev_day:
                if prev_day is not None:
                    daily_values.append(total_value)
                    peak_value = max(peak_value, total_value)
                day_start_value = total_value
                daily_pnl = 0.0
                halted_today = False
                prev_day = current_day
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
                # Resample to proper timeframes
                hist_5m = historical.tail(8640)
                hist_1h = historical.resample("1h").agg(
                    {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
                ).dropna()
                hist_1d = historical.resample("1D").agg(
                    {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
                ).dropna()
                markets[symbol] = SymbolData(
                    symbol=symbol,
                    current_price=current_price,
                    candles_5m=hist_5m,
                    candles_1h=hist_1h,
                    candles_1d=hist_1d,
                    spread=0.001,
                    volume_24h=float(historical.tail(24)["volume"].sum()) if len(historical) >= 24 else 0,
                    maker_fee_pct=pair_fees[0] if pair_fees else self._maker_fee,
                    taker_fee_pct=pair_fees[1] if pair_fees else self._taker_fee,
                )

            if not markets:
                continue

            # Build portfolio snapshot
            open_positions = []
            for tag, pos in positions.items():
                sym = pos.get("symbol", "")
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
                    tag=tag,
                ))

            total_value = cash + sum(
                pos["qty"] * prices.get(pos.get("symbol", ""), pos["avg_entry"])
                for pos in positions.values()
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

                # Risk halt simulation: skip new entries if daily loss or drawdown exceeded
                if signal.action == Action.BUY and (halted_today or drawdown_halted):
                    continue

                if signal.action == Action.BUY:
                    # Enforce max_positions
                    if len(positions) >= self._risk_limits.max_positions:
                        continue
                    fill_price = price * (1 + self._slippage)  # slippage: buy higher
                    # Clamp size_pct to max_trade_pct
                    clamped_pct = min(signal.size_pct, self._risk_limits.max_trade_pct)
                    trade_value = total_value * clamped_pct
                    # Enforce max_position_pct per symbol
                    existing_value = sum(
                        p["qty"] * prices.get(p["symbol"], p["avg_entry"])
                        for p in positions.values() if p["symbol"] == signal.symbol
                    )
                    if total_value > 0 and (existing_value + trade_value) / total_value > self._risk_limits.max_position_pct:
                        continue
                    if trade_value > cash:
                        continue
                    qty = trade_value / fill_price
                    fee = trade_value * fee_pct
                    cash -= (trade_value + fee)
                    tag = signal.tag or self._bt_tag(signal.symbol)
                    positions[tag] = {
                        "symbol": signal.symbol,
                        "qty": qty, "avg_entry": fill_price,
                        "entry_fee": fee,
                        "stop_loss": signal.stop_loss, "take_profit": signal.take_profit,
                        "opened_at": ts,
                    }
                    try:
                        self._strategy.on_fill(signal.symbol, Action.BUY, qty, fill_price, signal.intent, tag=tag)
                    except TypeError:
                        self._strategy.on_fill(signal.symbol, Action.BUY, qty, fill_price, signal.intent)

                    # Recalculate total_value after trade
                    total_value = cash + sum(
                        pos["qty"] * prices.get(pos.get("symbol", ""), pos["avg_entry"])
                        for pos in positions.values()
                    )

                elif signal.action in (Action.SELL, Action.CLOSE):
                    # CLOSE without tag: close ALL positions for this symbol (matches live behavior)
                    if signal.action == Action.CLOSE and not signal.tag:
                        to_close = [
                            (t, p) for t, p in positions.items()
                            if p.get("symbol") == signal.symbol
                        ]
                    else:
                        # SELL (oldest/FIFO) or CLOSE with specific tag
                        resolved = self._resolve_position(signal, positions)
                        to_close = [resolved] if resolved else []

                    for tag, pos in to_close:
                        fill_price = price * (1 - self._slippage)  # slippage: sell lower
                        qty = pos["qty"]
                        sale = qty * fill_price
                        exit_fee = sale * fee_pct
                        entry_fee = pos.get("entry_fee", 0.0)
                        fee = entry_fee + exit_fee
                        pnl = (fill_price - pos["avg_entry"]) * qty - fee
                        pnl_pct = pnl / (pos["avg_entry"] * qty) if pos["avg_entry"] * qty > 0 else 0.0
                        cash += (sale - exit_fee)

                        all_trades.append(BacktestTrade(
                            symbol=signal.symbol, action=signal.action.value,
                            qty=qty, price=fill_price, fee=fee, pnl=pnl, pnl_pct=pnl_pct, timestamp=ts,
                        ))

                        try:
                            self._strategy.on_fill(signal.symbol, signal.action, qty, fill_price, signal.intent, tag=tag)
                        except TypeError:
                            self._strategy.on_fill(signal.symbol, signal.action, qty, fill_price, signal.intent)
                        try:
                            self._strategy.on_position_closed(signal.symbol, pnl, pnl_pct, tag=tag)
                        except TypeError:
                            self._strategy.on_position_closed(signal.symbol, pnl, pnl_pct)
                        del positions[tag]

                    # Recalculate total_value after trade(s)
                    total_value = cash + sum(
                        pos["qty"] * prices.get(pos.get("symbol", ""), pos["avg_entry"])
                        for pos in positions.values()
                    )
                    # Track daily P&L for risk halt simulation
                    daily_pnl = total_value - day_start_value
                    if day_start_value > 0 and (-daily_pnl / day_start_value) >= self._risk_limits.max_daily_loss_pct:
                        halted_today = True

                elif signal.action == Action.MODIFY:
                    # In-place modification of SL/TP
                    if signal.tag and signal.tag in positions:
                        pos = positions[signal.tag]
                        if signal.stop_loss is not None:
                            pos["stop_loss"] = signal.stop_loss
                        if signal.take_profit is not None:
                            pos["take_profit"] = signal.take_profit

            # Check stop-loss / take-profit on existing positions (skip same-bar entries)
            for tag in list(positions.keys()):
                pos = positions[tag]
                sym = pos.get("symbol", "")
                if pos.get("opened_at") == ts:
                    continue  # Don't trigger SL/TP on the same bar as entry

                df = candle_data.get(sym)
                if df is None or ts not in df.index:
                    continue
                bar = df.loc[ts]
                bar_low = float(bar["low"])
                bar_high = float(bar["high"])
                price = prices.get(sym)
                if price is None:
                    continue

                triggered = False
                # Use intrabar low for SL check, intrabar high for TP check
                if pos.get("stop_loss") and bar_low <= pos["stop_loss"]:
                    price = pos["stop_loss"]  # Fill at SL price (worst case)
                    triggered = True
                elif pos.get("take_profit") and bar_high >= pos["take_profit"]:
                    price = pos["take_profit"]  # Fill at TP price (best case)
                    triggered = True

                if triggered:
                    # SL/TP triggers become market orders — slippage applies
                    fill_price = price * (1 - self._slippage)
                    qty = pos["qty"]
                    sale = qty * fill_price
                    exit_fee = sale * (self._get_taker_fee(sym) / 100)
                    entry_fee = pos.get("entry_fee", 0.0)
                    fee = entry_fee + exit_fee
                    pnl = (fill_price - pos["avg_entry"]) * qty - fee
                    pnl_pct = pnl / (pos["avg_entry"] * qty) if pos["avg_entry"] * qty > 0 else 0.0
                    cash += (sale - exit_fee)
                    all_trades.append(BacktestTrade(
                        symbol=sym, action="CLOSE", qty=qty, price=fill_price,
                        fee=fee, pnl=pnl, pnl_pct=pnl_pct, timestamp=ts,
                    ))
                    try:
                        self._strategy.on_position_closed(sym, pnl, pnl_pct, tag=tag)
                    except TypeError:
                        self._strategy.on_position_closed(sym, pnl, pnl_pct)
                    del positions[tag]

                    # Recalculate total_value after SL/TP close
                    total_value = cash + sum(
                        p["qty"] * prices.get(p.get("symbol", ""), p["avg_entry"])
                        for p in positions.values()
                    )
                    daily_pnl = total_value - day_start_value
                    if day_start_value > 0 and (-daily_pnl / day_start_value) >= self._risk_limits.max_daily_loss_pct:
                        halted_today = True

            # Check max_drawdown halt (persists across days — matches emergency stop behavior)
            if peak_value > 0 and (peak_value - total_value) / peak_value >= self._risk_limits.max_drawdown_pct:
                drawdown_halted = True

        # Capture final day's value (day boundary at top only records previous day)
        daily_values.append(total_value)
        peak_value = max(peak_value, total_value)

        # Compute metrics
        result = BacktestResult(trades=all_trades)
        result.total_trades = len(all_trades)
        result.wins = sum(1 for t in all_trades if t.pnl > 0)
        result.losses = sum(1 for t in all_trades if t.pnl < 0)
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
