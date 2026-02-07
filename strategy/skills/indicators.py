"""Core indicator functions — pure computations on OHLCV data."""

from __future__ import annotations

import numpy as np
import pandas as pd


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


def bollinger_bands(series: pd.Series, period: int = 20, std_dev: float = 2.0) -> tuple[float, float, float]:
    """Returns (upper, middle, lower) band values."""
    middle = series.rolling(period).mean()
    std = series.rolling(period).std()
    upper = middle + std_dev * std
    lower = middle - std_dev * std
    return float(upper.iloc[-1]), float(middle.iloc[-1]), float(lower.iloc[-1])


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[float, float, float]:
    """Returns (macd_line, signal_line, histogram)."""
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return float(macd_line.iloc[-1]), float(signal_line.iloc[-1]), float(histogram.iloc[-1])


def atr(df: pd.DataFrame, period: int = 14) -> float:
    """Average True Range."""
    high = df["high"]
    low = df["low"]
    close = df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])


def volume_ratio(volume: pd.Series, period: int = 20) -> float:
    """Current volume relative to N-period average."""
    avg = volume.tail(period).mean()
    current = volume.iloc[-1]
    return float(current / avg) if avg > 0 else 0.0


def classify_regime(df: pd.DataFrame) -> str:
    """Classify market regime from recent price action."""
    if len(df) < 30:
        return "unknown"

    close = df["close"].tail(30)
    returns = close.pct_change().dropna()

    volatility = float(returns.std())
    trend = float((close.iloc[-1] - close.iloc[0]) / close.iloc[0])
    mean_return = float(returns.mean())

    if volatility > 0.03:
        return "volatile"
    elif abs(trend) > 0.05:
        if trend > 0:
            return "trending_up"
        else:
            return "trending_down"
    elif abs(trend) < 0.01:
        return "ranging"
    else:
        return "breakout"


def compute_indicators(df: pd.DataFrame) -> dict:
    """Compute all standard indicators for a symbol. Used by scan loop for /report."""
    if len(df) < 30:
        return {}

    close = df["close"]
    vol = df["volume"]

    ema_fast = ema(close, 9)
    ema_slow = ema(close, 21)

    return {
        "rsi": rsi(close, 14),
        "ema_fast": float(ema_fast.iloc[-1]),
        "ema_slow": float(ema_slow.iloc[-1]),
        "vol_ratio": volume_ratio(vol, 20),
        "regime": classify_regime(df),
        "atr": atr(df, 14) if len(df) >= 14 else 0,
    }
