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
    limit_orders_attempted: int = 0
    limit_orders_filled: int = 0
    start_date: datetime | None = None
    end_date: datetime | None = None
    total_days: int = 0
    timeframe_mode: str = "single"  # "single" or "multi"

    def summary(self) -> str:
        parts = []
        if self.start_date and self.end_date:
            parts.append(f"Period: {self.start_date:%Y-%m-%d} to {self.end_date:%Y-%m-%d} ({self.total_days}d)")
        parts.append(
            f"Trades: {self.total_trades} | Win Rate: {self.win_rate:.1%} | "
            f"Net P&L: ${self.net_pnl:.2f} | Expectancy: ${self.expectancy:.4f} | "
            f"Sharpe: {self.sharpe:.2f} | Max DD: {self.max_drawdown_pct:.1%} | "
            f"Fees: ${self.total_fees:.2f}"
        )
        if self.limit_orders_attempted > 0:
            fill_rate = self.limit_orders_filled / self.limit_orders_attempted
            parts.append(f"Limit Fill: {fill_rate:.0%} ({self.limit_orders_filled}/{self.limit_orders_attempted})")
        return " | ".join(parts)

    def detailed_summary(self) -> str:
        """Extended summary for AI review — all metrics with backtest period."""
        lines = [
            f"Period: {self.start_date:%Y-%m-%d} to {self.end_date:%Y-%m-%d} ({self.total_days} days)" if self.start_date else "Period: N/A",
            f"Mode: {'Multi-timeframe (5m + 1h + 1d)' if self.timeframe_mode == 'multi' else 'Single timeframe'}",
            f"Total Trades: {self.total_trades}",
            f"Wins: {self.wins} | Losses: {self.losses}",
            f"Win Rate: {self.win_rate:.1%}",
            f"Net P&L: ${self.net_pnl:.2f}",
            f"Gross P&L: ${self.gross_pnl:.2f}",
            f"Total Fees: ${self.total_fees:.2f}",
            f"Expectancy: ${self.expectancy:.4f} per trade",
            f"Profit Factor: {self.profit_factor:.2f}",
            f"Sharpe Ratio: {self.sharpe:.2f}",
            f"Max Drawdown: {self.max_drawdown_pct:.1%}",
        ]
        if self.limit_orders_attempted > 0:
            fill_rate = self.limit_orders_filled / self.limit_orders_attempted
            lines.append(f"Limit Fill Rate: {fill_rate:.0%} ({self.limit_orders_filled}/{self.limit_orders_attempted})")
        return "\n".join(lines)


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

    def _get_maker_fee(self, symbol: str) -> float:
        """Get maker fee for symbol (per-pair if available, else global)."""
        if symbol in self._per_pair_fees:
            return self._per_pair_fees[symbol][0]
        return self._maker_fee

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

    def run(self, candle_data, timeframe: str = "1h") -> BacktestResult:
        """Run backtest on historical data.

        Args:
            candle_data: dict of symbol -> DataFrame (single-TF) or
                         dict of symbol -> (DataFrame_5m, DataFrame_1h, DataFrame_1d) (multi-TF)
            timeframe: candle timeframe (used for scan interval alignment, single-TF only)

        Returns:
            BacktestResult with performance metrics
        """
        first_val = next(iter(candle_data.values()), None)
        if isinstance(first_val, tuple):
            return self._run_multi(candle_data)
        return self._run_single(candle_data, timeframe)

    def _run_multi(self, candle_data: dict[str, tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]]) -> BacktestResult:
        """Run multi-timeframe backtest using native 5m/1h/1d data.

        Iterates at 1h resolution. Uses 5m sub-bars for SL/TP precision.
        No resampling — uses native DataFrames directly.
        """
        cash = self._starting_cash
        positions: dict[str, dict] = {}
        all_trades: list[BacktestTrade] = []
        daily_values: list[float] = []
        peak_value = cash
        self._tag_counter = {}

        limit_attempted = 0
        limit_filled = 0

        self._strategy.initialize(self._risk_limits, self._symbols)

        # Build union of all 1h timestamps across symbols
        all_timestamps: set = set()
        for symbol in self._symbols:
            if symbol not in candle_data:
                continue
            _, df_1h, _ = candle_data[symbol]
            if not df_1h.empty:
                all_timestamps.update(df_1h.index.tolist())
        timestamps = sorted(all_timestamps)

        if not timestamps:
            return BacktestResult()

        prev_day = None
        day_start_value = cash
        daily_pnl = 0.0
        daily_trade_count = 0
        consecutive_losses = 0
        halted_today = False
        drawdown_halted = False
        consecutive_loss_halted = False
        total_value = cash

        for ts in timestamps:
            # Day boundary check
            current_day = ts.date() if hasattr(ts, 'date') else None
            if current_day and current_day != prev_day:
                if prev_day is not None:
                    daily_values.append(total_value)
                    peak_value = max(peak_value, total_value)
                day_start_value = total_value
                daily_pnl = 0.0
                daily_trade_count = 0
                halted_today = False
                prev_day = current_day

            # Build SymbolData for each symbol at this timestamp
            markets = {}
            prices = {}
            for symbol in self._symbols:
                if symbol not in candle_data:
                    continue
                df_5m, df_1h, df_1d = candle_data[symbol]

                # Slice each timeframe up to current timestamp
                hist_1h = df_1h[df_1h.index <= ts]
                if hist_1h.empty:
                    continue

                current_price = float(hist_1h.iloc[-1]["close"])
                prices[symbol] = current_price

                hist_5m = df_5m[df_5m.index <= ts].tail(8640) if not df_5m.empty else pd.DataFrame()
                hist_1d_slice = df_1d[df_1d.index <= ts] if not df_1d.empty else pd.DataFrame()

                pair_fees = self._per_pair_fees.get(symbol)
                # Spread from 1h candles (last 100 bars)
                spread_sample = hist_1h.tail(100)
                if len(spread_sample) >= 10:
                    mid = spread_sample["close"]
                    intrabar_spread = (spread_sample["high"] - spread_sample["low"]) / mid
                    spread = float(np.median(intrabar_spread))
                else:
                    spread = 0.001

                markets[symbol] = SymbolData(
                    symbol=symbol,
                    current_price=current_price,
                    candles_5m=hist_5m,
                    candles_1h=hist_1h,
                    candles_1d=hist_1d_slice,
                    spread=spread,
                    volume_24h=float(hist_1h.tail(24)["volume"].sum()) if len(hist_1h) >= 24 else 0,
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

            # Execute signals (identical logic to _run_single)
            for signal in signals:
                price = prices.get(signal.symbol)
                if price is None:
                    continue

                if signal.action == Action.BUY and (halted_today or drawdown_halted or consecutive_loss_halted):
                    continue
                if signal.action == Action.BUY and daily_trade_count >= self._risk_limits.max_daily_trades:
                    continue

                if signal.action == Action.BUY:
                    tag = signal.tag or self._bt_tag(signal.symbol)
                    is_new = tag not in positions
                    if is_new and len(positions) >= self._risk_limits.max_positions:
                        continue
                    # LIMIT vs MARKET fill — use 1h bar for fill check
                    if signal.order_type == OrderType.LIMIT:
                        limit_attempted += 1
                        limit_p = signal.limit_price if signal.limit_price else price
                        sym_data = candle_data.get(signal.symbol)
                        if sym_data is not None:
                            _, df_1h_sym, _ = sym_data
                            if ts in df_1h_sym.index:
                                bar_low = float(df_1h_sym.loc[ts]["low"])
                            else:
                                bar_low = price
                        else:
                            bar_low = price
                        if bar_low > limit_p:
                            continue
                        limit_filled += 1
                        fill_price = limit_p
                        fee_pct = self._get_maker_fee(signal.symbol) / 100
                    else:
                        fill_price = price * (1 + self._slippage)
                        fee_pct = self._get_taker_fee(signal.symbol) / 100
                    if signal.size_pct > self._risk_limits.max_trade_pct:
                        continue
                    trade_value = total_value * signal.size_pct
                    existing_value = sum(
                        p["qty"] * prices.get(p["symbol"], p["avg_entry"])
                        for p in positions.values() if p["symbol"] == signal.symbol
                    )
                    if total_value > 0 and (existing_value + trade_value) / total_value > self._risk_limits.max_position_pct:
                        continue
                    fee = trade_value * fee_pct
                    if trade_value + fee > cash:
                        continue
                    qty = trade_value / fill_price
                    cash -= (trade_value + fee)
                    if tag in positions:
                        existing = positions[tag]
                        total_qty = existing["qty"] + qty
                        avg = (existing["avg_entry"] * existing["qty"] + fill_price * qty) / total_qty
                        existing["qty"] = total_qty
                        existing["avg_entry"] = avg
                        existing["entry_fee"] = existing.get("entry_fee", 0.0) + fee
                        if signal.stop_loss is not None:
                            existing["stop_loss"] = signal.stop_loss
                        if signal.take_profit is not None:
                            existing["take_profit"] = signal.take_profit
                    else:
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

                    total_value = cash + sum(
                        pos["qty"] * prices.get(pos.get("symbol", ""), pos["avg_entry"])
                        for pos in positions.values()
                    )
                    daily_pnl = total_value - day_start_value
                    if day_start_value > 0 and (-daily_pnl / day_start_value) >= self._risk_limits.max_daily_loss_pct:
                        halted_today = True

                elif signal.action in (Action.SELL, Action.CLOSE):
                    if signal.order_type == OrderType.LIMIT:
                        limit_attempted += 1
                        limit_p = signal.limit_price if signal.limit_price else price
                        sym_data = candle_data.get(signal.symbol)
                        if sym_data is not None:
                            _, df_1h_sym, _ = sym_data
                            if ts in df_1h_sym.index:
                                bar_high = float(df_1h_sym.loc[ts]["high"])
                            else:
                                bar_high = price
                        else:
                            bar_high = price
                        if bar_high < limit_p:
                            continue
                        limit_filled += 1

                    if signal.action == Action.CLOSE and not signal.tag:
                        to_close = [
                            (t, p) for t, p in positions.items()
                            if p.get("symbol") == signal.symbol
                        ]
                    else:
                        resolved = self._resolve_position(signal, positions)
                        to_close = [resolved] if resolved else []

                    for tag, pos in to_close:
                        if signal.order_type == OrderType.LIMIT:
                            fill_price = signal.limit_price if signal.limit_price else price
                            exit_fee_pct = self._get_maker_fee(signal.symbol) / 100
                        else:
                            fill_price = price * (1 - self._slippage)
                            exit_fee_pct = self._get_taker_fee(signal.symbol) / 100
                        if signal.action == Action.SELL and signal.size_pct > 0 and signal.size_pct < 1.0:
                            sell_value = total_value * signal.size_pct
                            qty = min(sell_value / fill_price, pos["qty"])
                        else:
                            qty = pos["qty"]
                        sale = qty * fill_price
                        exit_fee = sale * exit_fee_pct
                        close_fraction = min(qty / pos["qty"], 1.0) if pos["qty"] > 0 else 1.0
                        entry_fee = pos.get("entry_fee", 0.0) * close_fraction
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

                        remaining = pos["qty"] - qty
                        if remaining <= 0.000001:
                            try:
                                self._strategy.on_position_closed(signal.symbol, pnl, pnl_pct, tag=tag)
                            except TypeError:
                                self._strategy.on_position_closed(signal.symbol, pnl, pnl_pct)
                            del positions[tag]
                        else:
                            pos["qty"] = remaining
                            pos["entry_fee"] = pos.get("entry_fee", 0.0) - entry_fee

                    for bt_trade in all_trades[-len(to_close):]:
                        daily_trade_count += 1
                        if bt_trade.pnl < 0:
                            consecutive_losses += 1
                        else:
                            consecutive_losses = 0

                    total_value = cash + sum(
                        pos["qty"] * prices.get(pos.get("symbol", ""), pos["avg_entry"])
                        for pos in positions.values()
                    )
                    daily_pnl = total_value - day_start_value
                    if day_start_value > 0 and (-daily_pnl / day_start_value) >= self._risk_limits.max_daily_loss_pct:
                        halted_today = True
                    if consecutive_losses >= self._risk_limits.rollback_consecutive_losses:
                        consecutive_loss_halted = True

                elif signal.action == Action.MODIFY:
                    if signal.tag and signal.tag in positions:
                        pos = positions[signal.tag]
                        if signal.stop_loss is not None:
                            pos["stop_loss"] = signal.stop_loss
                        if signal.take_profit is not None:
                            pos["take_profit"] = signal.take_profit

            # SL/TP check with 5m sub-iteration for precision
            for tag in list(positions.keys()):
                pos = positions[tag]
                sym = pos.get("symbol", "")
                if pos.get("opened_at") == ts:
                    continue

                if sym not in candle_data:
                    continue
                df_5m_sym, df_1h_sym, _ = candle_data[sym]

                if not pos.get("stop_loss") and not pos.get("take_profit"):
                    continue

                triggered = False
                trigger_price = None

                # Try 5m sub-bars within this hour for precision
                if not df_5m_sym.empty:
                    # Get 5m bars within this 1h window
                    hour_start = ts
                    hour_end = ts + pd.Timedelta(hours=1)
                    sub_bars = df_5m_sym[(df_5m_sym.index >= hour_start) & (df_5m_sym.index < hour_end)]
                    for _, sub_bar in sub_bars.iterrows():
                        sub_low = float(sub_bar["low"])
                        sub_high = float(sub_bar["high"])
                        if pos.get("stop_loss") and sub_low <= pos["stop_loss"]:
                            trigger_price = pos["stop_loss"]
                            triggered = True
                            break
                        if pos.get("take_profit") and sub_high >= pos["take_profit"]:
                            trigger_price = pos["take_profit"]
                            triggered = True
                            break

                # Fall back to 1h bar if no 5m data triggered
                if not triggered and ts in df_1h_sym.index:
                    bar = df_1h_sym.loc[ts]
                    bar_low = float(bar["low"])
                    bar_high = float(bar["high"])
                    if pos.get("stop_loss") and bar_low <= pos["stop_loss"]:
                        trigger_price = pos["stop_loss"]
                        triggered = True
                    elif pos.get("take_profit") and bar_high >= pos["take_profit"]:
                        trigger_price = pos["take_profit"]
                        triggered = True

                if triggered and trigger_price is not None:
                    fill_price = trigger_price * (1 - self._slippage)
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

                    daily_trade_count += 1
                    if pnl < 0:
                        consecutive_losses += 1
                    else:
                        consecutive_losses = 0

                    total_value = cash + sum(
                        p["qty"] * prices.get(p.get("symbol", ""), p["avg_entry"])
                        for p in positions.values()
                    )
                    daily_pnl = total_value - day_start_value
                    if day_start_value > 0 and (-daily_pnl / day_start_value) >= self._risk_limits.max_daily_loss_pct:
                        halted_today = True
                    if consecutive_losses >= self._risk_limits.rollback_consecutive_losses:
                        consecutive_loss_halted = True

            # Drawdown halt
            if peak_value > 0 and (peak_value - total_value) / peak_value >= self._risk_limits.max_drawdown_pct:
                drawdown_halted = True

        # Final day
        daily_values.append(total_value)
        peak_value = max(peak_value, total_value)

        # Compute metrics (same as _run_single)
        result = BacktestResult(trades=all_trades)
        result.timeframe_mode = "multi"
        result.limit_orders_attempted = limit_attempted
        result.limit_orders_filled = limit_filled
        result.total_trades = len(all_trades)
        result.wins = sum(1 for t in all_trades if t.pnl > 0)
        result.losses = sum(1 for t in all_trades if t.pnl < 0)
        result.total_fees = float(sum(t.fee for t in all_trades))
        result.gross_pnl = float(sum(t.pnl + t.fee for t in all_trades))
        result.net_pnl = float(sum(t.pnl for t in all_trades))
        result.win_rate = result.wins / result.total_trades if result.total_trades > 0 else 0

        if result.total_trades > 0:
            avg_win = sum(t.pnl for t in all_trades if t.pnl > 0) / max(result.wins, 1)
            avg_loss = abs(sum(t.pnl for t in all_trades if t.pnl <= 0)) / max(result.losses, 1)
            result.expectancy = (result.win_rate * avg_win) - ((1 - result.win_rate) * avg_loss)

        gross_profit = sum(t.pnl for t in all_trades if t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in all_trades if t.pnl <= 0))
        result.profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')

        if daily_values:
            peak = daily_values[0]
            max_dd = 0
            for v in daily_values:
                if v > peak:
                    peak = v
                dd = (peak - v) / peak if peak > 0 else 0
                max_dd = max(max_dd, dd)
            result.max_drawdown_pct = max_dd

            returns = []
            for i in range(1, len(daily_values)):
                r = (daily_values[i] - daily_values[i-1]) / daily_values[i-1]
                returns.append(r)
            result.daily_returns = returns

            if returns and len(returns) > 1:
                mean_r = np.mean(returns)
                std_r = np.std(returns)
                result.sharpe = (mean_r / std_r * (365 ** 0.5)) if std_r > 0 else 0

        # Date metadata
        if timestamps:
            result.start_date = timestamps[0] if hasattr(timestamps[0], 'date') else None
            result.end_date = timestamps[-1] if hasattr(timestamps[-1], 'date') else None
            if result.start_date and result.end_date:
                result.total_days = max(1, (result.end_date - result.start_date).days)

        return result

    def _run_single(self, candle_data: dict[str, pd.DataFrame], timeframe: str = "1h") -> BacktestResult:
        """Run single-timeframe backtest (original behavior)."""
        # Initialize
        cash = self._starting_cash
        positions: dict[str, dict] = {}  # tag -> position dict
        all_trades: list[BacktestTrade] = []
        daily_values: list[float] = []
        peak_value = cash
        self._tag_counter = {}

        limit_attempted = 0
        limit_filled = 0

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
        daily_trade_count = 0
        consecutive_losses = 0
        halted_today = False
        drawdown_halted = False
        consecutive_loss_halted = False
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
                daily_trade_count = 0
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
                # Calculate per-symbol spread from recent candle data
                spread_sample = historical.tail(100)
                if len(spread_sample) >= 10:
                    mid = spread_sample["close"]
                    intrabar_spread = (spread_sample["high"] - spread_sample["low"]) / mid
                    spread = float(np.median(intrabar_spread))
                else:
                    spread = 0.001
                markets[symbol] = SymbolData(
                    symbol=symbol,
                    current_price=current_price,
                    candles_5m=hist_5m,
                    candles_1h=hist_1h,
                    candles_1d=hist_1d,
                    spread=spread,
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

                # Risk halt simulation: skip new entries if halted
                if signal.action == Action.BUY and (halted_today or drawdown_halted or consecutive_loss_halted):
                    continue
                # Daily trade count limit (skip new entries only)
                if signal.action == Action.BUY and daily_trade_count >= self._risk_limits.max_daily_trades:
                    continue

                if signal.action == Action.BUY:
                    # Resolve tag first (needed for average-in check)
                    tag = signal.tag or self._bt_tag(signal.symbol)
                    is_new = tag not in positions
                    # Enforce max_positions only for genuinely new positions (not average-in)
                    if is_new and len(positions) >= self._risk_limits.max_positions:
                        continue
                    # LIMIT vs MARKET fill simulation
                    if signal.order_type == OrderType.LIMIT:
                        limit_attempted += 1
                        limit_p = signal.limit_price if signal.limit_price else price
                        # Check if candle low reached the limit price (buy fills when price drops to limit)
                        df = candle_data.get(signal.symbol)
                        if df is not None and ts in df.index:
                            bar_low = float(df.loc[ts]["low"])
                        else:
                            bar_low = price
                        if bar_low > limit_p:
                            continue  # Limit not reached — order doesn't fill
                        limit_filled += 1
                        fill_price = limit_p
                        fee_pct = self._get_maker_fee(signal.symbol) / 100
                    else:
                        fill_price = price * (1 + self._slippage)  # slippage: buy higher
                        fee_pct = self._get_taker_fee(signal.symbol) / 100
                    # Reject oversized signals (matches live risk manager behavior)
                    if signal.size_pct > self._risk_limits.max_trade_pct:
                        continue
                    trade_value = total_value * signal.size_pct
                    # Enforce max_position_pct per symbol
                    existing_value = sum(
                        p["qty"] * prices.get(p["symbol"], p["avg_entry"])
                        for p in positions.values() if p["symbol"] == signal.symbol
                    )
                    if total_value > 0 and (existing_value + trade_value) / total_value > self._risk_limits.max_position_pct:
                        continue
                    fee = trade_value * fee_pct
                    if trade_value + fee > cash:
                        continue
                    qty = trade_value / fill_price
                    cash -= (trade_value + fee)
                    if tag in positions:
                        # Average into existing position (matches live behavior)
                        existing = positions[tag]
                        total_qty = existing["qty"] + qty
                        avg = (existing["avg_entry"] * existing["qty"] + fill_price * qty) / total_qty
                        existing["qty"] = total_qty
                        existing["avg_entry"] = avg
                        existing["entry_fee"] = existing.get("entry_fee", 0.0) + fee
                        if signal.stop_loss is not None:
                            existing["stop_loss"] = signal.stop_loss
                        if signal.take_profit is not None:
                            existing["take_profit"] = signal.take_profit
                    else:
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
                    # Check daily loss halt after BUY (fees reduce total_value)
                    daily_pnl = total_value - day_start_value
                    if day_start_value > 0 and (-daily_pnl / day_start_value) >= self._risk_limits.max_daily_loss_pct:
                        halted_today = True

                elif signal.action in (Action.SELL, Action.CLOSE):
                    # LIMIT sell simulation: check if candle high reaches limit price
                    if signal.order_type == OrderType.LIMIT:
                        limit_attempted += 1
                        limit_p = signal.limit_price if signal.limit_price else price
                        df = candle_data.get(signal.symbol)
                        if df is not None and ts in df.index:
                            bar_high = float(df.loc[ts]["high"])
                        else:
                            bar_high = price
                        if bar_high < limit_p:
                            continue  # Limit not reached — order doesn't fill
                        limit_filled += 1

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
                        # LIMIT vs MARKET fill price and fees
                        if signal.order_type == OrderType.LIMIT:
                            fill_price = signal.limit_price if signal.limit_price else price
                            exit_fee_pct = self._get_maker_fee(signal.symbol) / 100
                        else:
                            fill_price = price * (1 - self._slippage)  # slippage: sell lower
                            exit_fee_pct = self._get_taker_fee(signal.symbol) / 100
                        # SELL respects size_pct for partial sells (matches live _execute_sell)
                        if signal.action == Action.SELL and signal.size_pct > 0 and signal.size_pct < 1.0:
                            sell_value = total_value * signal.size_pct
                            qty = min(sell_value / fill_price, pos["qty"])
                        else:
                            qty = pos["qty"]
                        sale = qty * fill_price
                        exit_fee = sale * exit_fee_pct
                        close_fraction = min(qty / pos["qty"], 1.0) if pos["qty"] > 0 else 1.0
                        entry_fee = pos.get("entry_fee", 0.0) * close_fraction
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

                        remaining = pos["qty"] - qty
                        if remaining <= 0.000001:
                            try:
                                self._strategy.on_position_closed(signal.symbol, pnl, pnl_pct, tag=tag)
                            except TypeError:
                                self._strategy.on_position_closed(signal.symbol, pnl, pnl_pct)
                            del positions[tag]
                        else:
                            # Partial sell: update position (matching live _close_qty)
                            pos["qty"] = remaining
                            pos["entry_fee"] = pos.get("entry_fee", 0.0) - entry_fee

                    # Track trade count and consecutive losses for risk halt simulation
                    for bt_trade in all_trades[-len(to_close):]:
                        daily_trade_count += 1
                        if bt_trade.pnl < 0:
                            consecutive_losses += 1
                        else:
                            consecutive_losses = 0

                    # Recalculate total_value after trade(s)
                    total_value = cash + sum(
                        pos["qty"] * prices.get(pos.get("symbol", ""), pos["avg_entry"])
                        for pos in positions.values()
                    )
                    # Track daily P&L for risk halt simulation
                    daily_pnl = total_value - day_start_value
                    if day_start_value > 0 and (-daily_pnl / day_start_value) >= self._risk_limits.max_daily_loss_pct:
                        halted_today = True
                    # Consecutive loss halt (persists across days — matches rollback behavior)
                    if consecutive_losses >= self._risk_limits.rollback_consecutive_losses:
                        consecutive_loss_halted = True

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

                    # Track trade count and consecutive losses
                    daily_trade_count += 1
                    if pnl < 0:
                        consecutive_losses += 1
                    else:
                        consecutive_losses = 0

                    # Recalculate total_value after SL/TP close
                    total_value = cash + sum(
                        p["qty"] * prices.get(p.get("symbol", ""), p["avg_entry"])
                        for p in positions.values()
                    )
                    daily_pnl = total_value - day_start_value
                    if day_start_value > 0 and (-daily_pnl / day_start_value) >= self._risk_limits.max_daily_loss_pct:
                        halted_today = True
                    if consecutive_losses >= self._risk_limits.rollback_consecutive_losses:
                        consecutive_loss_halted = True

            # Check max_drawdown halt (persists across days — matches emergency stop behavior)
            if peak_value > 0 and (peak_value - total_value) / peak_value >= self._risk_limits.max_drawdown_pct:
                drawdown_halted = True

        # Capture final day's value (day boundary at top only records previous day)
        daily_values.append(total_value)
        peak_value = max(peak_value, total_value)

        # Compute metrics
        result = BacktestResult(trades=all_trades)
        result.limit_orders_attempted = limit_attempted
        result.limit_orders_filled = limit_filled
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

        # Date metadata
        if timestamps:
            result.start_date = timestamps[0] if hasattr(timestamps[0], 'date') else None
            result.end_date = timestamps[-1] if hasattr(timestamps[-1], 'date') else None
            if result.start_date and result.end_date:
                result.total_days = max(1, (result.end_date - result.start_date).days)

        return result
