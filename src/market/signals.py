"""Technical indicator-based signal generation.

Generates trading signals from OHLC data using multiple strategies:
- Momentum (RSI)
- Mean Reversion (Bollinger Bands)
- Volume (relative volume)
- Trend (EMA crossover)

Each strategy produces a signal strength (0-1) and direction.
The composite signal combines them using configurable weights.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import ta

from src.core.config import StrategyParams
from src.core.logging import get_logger
from src.storage.models import Signal

log = get_logger("signals")


@dataclass
class RawSignal:
    """Pre-AI signal from technical analysis."""
    symbol: str
    signal_type: str
    strength: float  # 0.0 to 1.0
    direction: str  # "long", "short", "close"
    reasoning: str


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add all technical indicators to an OHLC DataFrame.

    Expects columns: open, high, low, close, volume
    """
    # RSI
    df["rsi"] = ta.momentum.rsi(df["close"], window=14)

    # Bollinger Bands
    bb = ta.volatility.BollingerBands(df["close"], window=20, window_dev=2)
    df["bb_upper"] = bb.bollinger_hband()
    df["bb_lower"] = bb.bollinger_lband()
    df["bb_mid"] = bb.bollinger_mavg()
    df["bb_pct"] = bb.bollinger_pband()  # % position within bands

    # EMAs
    df["ema_fast"] = ta.trend.ema_indicator(df["close"], window=9)
    df["ema_slow"] = ta.trend.ema_indicator(df["close"], window=21)

    # MACD
    macd = ta.trend.MACD(df["close"])
    df["macd"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_hist"] = macd.macd_diff()

    # ATR (for volatility context)
    df["atr"] = ta.volatility.average_true_range(df["high"], df["low"], df["close"])

    # Volume moving average
    df["vol_sma"] = df["volume"].rolling(window=20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_sma"].replace(0, np.nan)

    return df


def momentum_signal(df: pd.DataFrame, params: dict) -> RawSignal | None:
    """RSI-based momentum signal."""
    if len(df) < 15 or df["rsi"].isna().iloc[-1]:
        return None

    rsi = df["rsi"].iloc[-1]
    threshold = params.get("threshold", 0.65)

    if rsi < 30:
        strength = min((30 - rsi) / 30, 1.0)
        if strength >= threshold:
            return RawSignal(
                symbol=df.attrs.get("symbol", ""),
                signal_type="momentum",
                strength=strength,
                direction="long",
                reasoning=f"RSI oversold at {rsi:.1f}",
            )
    elif rsi > 70:
        strength = min((rsi - 70) / 30, 1.0)
        if strength >= threshold:
            return RawSignal(
                symbol=df.attrs.get("symbol", ""),
                signal_type="momentum",
                strength=strength,
                direction="short",
                reasoning=f"RSI overbought at {rsi:.1f}",
            )
    return None


def mean_reversion_signal(df: pd.DataFrame, params: dict) -> RawSignal | None:
    """Bollinger Band mean reversion signal."""
    if len(df) < 21 or df["bb_pct"].isna().iloc[-1]:
        return None

    bb_pct = df["bb_pct"].iloc[-1]
    close = df["close"].iloc[-1]
    bb_lower = df["bb_lower"].iloc[-1]
    bb_upper = df["bb_upper"].iloc[-1]

    if bb_pct < 0:
        # Below lower band
        distance = abs(bb_pct)
        strength = min(distance, 1.0)
        return RawSignal(
            symbol=df.attrs.get("symbol", ""),
            signal_type="mean_reversion",
            strength=strength,
            direction="long",
            reasoning=f"Price below lower BB (pct={bb_pct:.2f})",
        )
    elif bb_pct > 1:
        # Above upper band
        distance = bb_pct - 1.0
        strength = min(distance, 1.0)
        return RawSignal(
            symbol=df.attrs.get("symbol", ""),
            signal_type="mean_reversion",
            strength=strength,
            direction="short",
            reasoning=f"Price above upper BB (pct={bb_pct:.2f})",
        )
    return None


def trend_signal(df: pd.DataFrame, params: dict) -> RawSignal | None:
    """EMA crossover trend signal."""
    if len(df) < 22 or df["ema_fast"].isna().iloc[-1]:
        return None

    ema_fast = df["ema_fast"].iloc[-1]
    ema_slow = df["ema_slow"].iloc[-1]
    prev_fast = df["ema_fast"].iloc[-2]
    prev_slow = df["ema_slow"].iloc[-2]

    # Bullish crossover
    if prev_fast <= prev_slow and ema_fast > ema_slow:
        gap_pct = (ema_fast - ema_slow) / ema_slow
        strength = min(gap_pct * 100, 1.0)  # Normalize
        return RawSignal(
            symbol=df.attrs.get("symbol", ""),
            signal_type="trend",
            strength=max(strength, 0.5),  # Crossovers get minimum 0.5
            direction="long",
            reasoning=f"Bullish EMA crossover (fast={ema_fast:.2f} > slow={ema_slow:.2f})",
        )
    # Bearish crossover
    elif prev_fast >= prev_slow and ema_fast < ema_slow:
        gap_pct = (ema_slow - ema_fast) / ema_slow
        strength = min(gap_pct * 100, 1.0)
        return RawSignal(
            symbol=df.attrs.get("symbol", ""),
            signal_type="trend",
            strength=max(strength, 0.5),
            direction="short",
            reasoning=f"Bearish EMA crossover (fast={ema_fast:.2f} < slow={ema_slow:.2f})",
        )
    return None


def volume_signal(df: pd.DataFrame, params: dict) -> RawSignal | None:
    """Volume spike confirmation signal."""
    if len(df) < 21 or df["vol_ratio"].isna().iloc[-1]:
        return None

    vol_ratio = df["vol_ratio"].iloc[-1]
    threshold = params.get("relative_threshold", 1.5)

    if vol_ratio >= threshold:
        # Volume spike — direction based on price action
        close = df["close"].iloc[-1]
        prev_close = df["close"].iloc[-2]
        direction = "long" if close > prev_close else "short"
        strength = min((vol_ratio - 1.0) / 2.0, 1.0)  # Normalize

        return RawSignal(
            symbol=df.attrs.get("symbol", ""),
            signal_type="volume",
            strength=strength,
            direction=direction,
            reasoning=f"Volume spike {vol_ratio:.1f}x average, price {'up' if direction == 'long' else 'down'}",
        )
    return None


class SignalGenerator:
    """Generates composite trading signals from technical indicators."""

    def __init__(self, params: StrategyParams) -> None:
        self._params = params

    def generate(self, df: pd.DataFrame, symbol: str) -> RawSignal | None:
        """Generate a composite signal for a symbol.

        Returns the strongest signal if multiple agree, or None if no signal.
        """
        df = compute_indicators(df.copy())
        df.attrs["symbol"] = symbol

        signals: list[tuple[RawSignal, float]] = []

        # Collect signals with their weights
        strategies = [
            (momentum_signal, self._params.momentum),
            (mean_reversion_signal, self._params.mean_reversion),
            (trend_signal, self._params.trend),
            (volume_signal, self._params.volume),
        ]

        for fn, params in strategies:
            sig = fn(df, params)
            if sig:
                weight = params.get("weight", 0.25)
                signals.append((sig, weight))

        if not signals:
            return None

        # Calculate weighted composite strength
        total_weight = sum(w for _, w in signals)
        weighted_strength = sum(s.strength * w for s, w in signals) / total_weight

        # Direction consensus: majority weighted vote
        long_weight = sum(w for s, w in signals if s.direction == "long")
        short_weight = sum(w for s, w in signals if s.direction == "short")
        direction = "long" if long_weight >= short_weight else "short"

        # Build composite reasoning
        reasons = [s.reasoning for s, _ in signals]
        reasoning = " | ".join(reasons)

        composite = RawSignal(
            symbol=symbol,
            signal_type="composite",
            strength=weighted_strength,
            direction=direction,
            reasoning=reasoning,
        )

        log.info(
            "signal_generated",
            symbol=symbol,
            strength=round(weighted_strength, 3),
            direction=direction,
            components=len(signals),
        )
        return composite
