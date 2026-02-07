"""Strategy v001: EMA Crossover + RSI Filter + Volume Confirmation

Initial hand-written strategy. The AI orchestrator will iterate on this.

Logic:
- BUY when: EMA 9 crosses above EMA 21, RSI 14 between 30-70, volume > 1.2x average
- SELL/CLOSE when: EMA 9 crosses below EMA 21, or stop-loss/take-profit hit
- Day trading intent, 2% stop-loss, 4% take-profit
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from src.shell.contract import (
    Action, Intent, OrderType, Portfolio, RiskLimits, Signal, StrategyBase, SymbolData,
)


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> float:
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.inf)
    rsi_values = 100 - (100 / (1 + rs))
    return float(rsi_values.iloc[-1]) if len(rsi_values) > 0 else 50.0


class Strategy(StrategyBase):
    """EMA crossover with RSI filter and volume confirmation."""

    def __init__(self):
        self._risk_limits: RiskLimits | None = None
        self._symbols: list[str] = []
        self._prev_ema_fast: dict[str, float] = {}
        self._prev_ema_slow: dict[str, float] = {}
        self._trade_count: int = 0

    def initialize(self, risk_limits: RiskLimits, symbols: list[str]) -> None:
        self._risk_limits = risk_limits
        self._symbols = symbols

    def analyze(
        self,
        markets: dict[str, SymbolData],
        portfolio: Portfolio,
        timestamp: datetime,
    ) -> list[Signal]:
        signals = []

        for symbol, data in markets.items():
            signal = self._analyze_symbol(symbol, data, portfolio)
            if signal:
                signals.append(signal)

        return signals

    def _analyze_symbol(
        self, symbol: str, data: SymbolData, portfolio: Portfolio
    ) -> Signal | None:
        df = data.candles_5m
        if len(df) < 30:
            return None

        close = df["close"]
        volume = df["volume"]

        # Indicators
        ema_fast = ema(close, 9)
        ema_slow = ema(close, 21)
        current_rsi = rsi(close, 14)

        ema_f = float(ema_fast.iloc[-1])
        ema_s = float(ema_slow.iloc[-1])

        # Previous values for crossover detection
        prev_f = self._prev_ema_fast.get(symbol)
        prev_s = self._prev_ema_slow.get(symbol)
        self._prev_ema_fast[symbol] = ema_f
        self._prev_ema_slow[symbol] = ema_s

        if prev_f is None or prev_s is None:
            return None

        # Volume confirmation: current volume > 1.2x 20-period average
        vol_avg = float(volume.tail(20).mean())
        vol_current = float(volume.iloc[-1])
        volume_ok = vol_current > vol_avg * 1.2 if vol_avg > 0 else False

        # Check if we have a position
        has_position = any(p.symbol == symbol for p in portfolio.positions)
        price = data.current_price

        # BUY signal: EMA cross up + RSI filter + volume
        if not has_position:
            crossover_up = prev_f <= prev_s and ema_f > ema_s
            rsi_ok = 30 < current_rsi < 70

            if crossover_up and rsi_ok and volume_ok:
                size = self._risk_limits.default_trade_pct if self._risk_limits else 0.02
                return Signal(
                    symbol=symbol,
                    action=Action.BUY,
                    size_pct=size,
                    order_type=OrderType.MARKET,
                    stop_loss=price * 0.98,     # 2% SL
                    take_profit=price * 1.04,   # 4% TP
                    intent=Intent.DAY,
                    confidence=0.6 + (0.2 if volume_ok else 0),
                    reasoning=f"EMA 9/21 bullish cross, RSI={current_rsi:.1f}, vol={vol_current/vol_avg:.1f}x avg",
                )

        # CLOSE signal: EMA cross down
        if has_position:
            crossover_down = prev_f >= prev_s and ema_f < ema_s
            if crossover_down:
                return Signal(
                    symbol=symbol,
                    action=Action.CLOSE,
                    size_pct=1.0,
                    intent=Intent.DAY,
                    confidence=0.7,
                    reasoning=f"EMA 9/21 bearish cross, RSI={current_rsi:.1f}",
                )

        return None

    def on_fill(self, symbol: str, action: Action, qty: float, price: float, intent: Intent) -> None:
        self._trade_count += 1

    def on_position_closed(self, symbol: str, pnl: float, pnl_pct: float) -> None:
        pass

    def get_state(self) -> dict:
        return {
            "prev_ema_fast": self._prev_ema_fast,
            "prev_ema_slow": self._prev_ema_slow,
            "trade_count": self._trade_count,
        }

    def load_state(self, state: dict) -> None:
        self._prev_ema_fast = state.get("prev_ema_fast", {})
        self._prev_ema_slow = state.get("prev_ema_slow", {})
        self._trade_count = state.get("trade_count", 0)

    @property
    def scan_interval_minutes(self) -> int:
        return 5
