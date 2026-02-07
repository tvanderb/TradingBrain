"""Market regime classification.

Classifies current market conditions into regimes:
- trending_up: Clear uptrend with momentum
- trending_down: Clear downtrend with momentum
- ranging: Sideways, low directional movement
- volatile: High volatility, no clear direction
- breakout: Potential breakout from range

The regime affects which signals are prioritized and how
aggressively the system trades.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np
import pandas as pd

from src.core.logging import get_logger

log = get_logger("regime")


class MarketRegime(str, Enum):
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    RANGING = "ranging"
    VOLATILE = "volatile"
    BREAKOUT = "breakout"


@dataclass
class RegimeAnalysis:
    regime: MarketRegime
    confidence: float  # 0.0 to 1.0
    atr_pct: float  # ATR as % of price
    trend_strength: float  # ADX-like measure
    description: str


def classify_regime(df: pd.DataFrame) -> RegimeAnalysis:
    """Classify market regime from OHLC data.

    Expects a DataFrame with at least 50 rows and columns:
    open, high, low, close, volume
    """
    if len(df) < 50:
        return RegimeAnalysis(
            regime=MarketRegime.RANGING,
            confidence=0.0,
            atr_pct=0.0,
            trend_strength=0.0,
            description="Insufficient data for regime classification",
        )

    close = df["close"].values
    high = df["high"].values
    low = df["low"].values

    # ATR as percentage of price (volatility measure)
    tr = np.maximum(
        high[-20:] - low[-20:],
        np.maximum(
            np.abs(high[-20:] - close[-21:-1]),
            np.abs(low[-20:] - close[-21:-1]),
        ),
    )
    atr = np.mean(tr)
    atr_pct = (atr / close[-1]) * 100

    # Simple trend strength: slope of 20-period linear regression
    x = np.arange(20)
    y = close[-20:]
    slope = np.polyfit(x, y, 1)[0]
    slope_pct = (slope / close[-1]) * 100

    # Directional movement (simplified ADX-like)
    ema_fast = pd.Series(close).ewm(span=9).mean().iloc[-1]
    ema_slow = pd.Series(close).ewm(span=21).mean().iloc[-1]
    ema_gap_pct = ((ema_fast - ema_slow) / ema_slow) * 100

    # Bollinger Band width (relative to price)
    sma20 = np.mean(close[-20:])
    std20 = np.std(close[-20:])
    bb_width_pct = (4 * std20 / sma20) * 100  # Full BB width as %

    # Classification logic
    trend_strength = abs(ema_gap_pct)

    if atr_pct > 3.0 and trend_strength < 1.0:
        regime = MarketRegime.VOLATILE
        confidence = min(atr_pct / 5.0, 1.0)
        desc = f"High volatility (ATR {atr_pct:.1f}%), no clear trend"
    elif trend_strength > 1.5 and slope_pct > 0:
        regime = MarketRegime.TRENDING_UP
        confidence = min(trend_strength / 3.0, 1.0)
        desc = f"Uptrend (EMA gap {ema_gap_pct:.2f}%, slope {slope_pct:.3f}%)"
    elif trend_strength > 1.5 and slope_pct < 0:
        regime = MarketRegime.TRENDING_DOWN
        confidence = min(trend_strength / 3.0, 1.0)
        desc = f"Downtrend (EMA gap {ema_gap_pct:.2f}%, slope {slope_pct:.3f}%)"
    elif bb_width_pct < 3.0 and atr_pct < 1.5:
        regime = MarketRegime.BREAKOUT
        confidence = min((3.0 - bb_width_pct) / 3.0, 1.0)
        desc = f"Tight range, potential breakout (BB width {bb_width_pct:.1f}%)"
    else:
        regime = MarketRegime.RANGING
        confidence = 1.0 - min(trend_strength, 1.0)
        desc = f"Ranging market (trend strength {trend_strength:.2f})"

    analysis = RegimeAnalysis(
        regime=regime,
        confidence=confidence,
        atr_pct=round(atr_pct, 2),
        trend_strength=round(trend_strength, 3),
        description=desc,
    )

    log.info(
        "regime_classified",
        regime=regime.value,
        confidence=round(confidence, 2),
        atr_pct=round(atr_pct, 2),
    )
    return analysis
