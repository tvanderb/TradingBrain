"""IO Contract — rigid interface between Shell and modules.

These types define EXACTLY what the Strategy and Analysis modules receive
and what they must return. The Shell enforces all constraints.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

import pandas as pd


# --- Enums ---

class Action(Enum):
    BUY = "BUY"
    SELL = "SELL"
    CLOSE = "CLOSE"
    MODIFY = "MODIFY"


class Intent(Enum):
    DAY = "DAY"
    SWING = "SWING"
    POSITION = "POSITION"


class OrderType(Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


# --- Input Types (Shell -> Strategy) ---

@dataclass(frozen=True)
class OpenPosition:
    symbol: str
    side: str               # "long" or "short"
    qty: float
    avg_entry: float
    current_price: float
    unrealized_pnl: float
    unrealized_pnl_pct: float
    intent: Intent
    stop_loss: Optional[float]
    take_profit: Optional[float]
    opened_at: datetime
    tag: str = ""


@dataclass(frozen=True)
class ClosedTrade:
    symbol: str
    side: str
    qty: float
    entry_price: float
    exit_price: float
    pnl: float
    pnl_pct: float
    fees: float
    intent: Intent
    opened_at: datetime
    closed_at: datetime


@dataclass(frozen=True)
class SymbolData:
    symbol: str
    current_price: float
    candles_5m: pd.DataFrame   # Last 30 days of 5-min candles
    candles_1h: pd.DataFrame   # Last 1 year of 1-hour candles
    candles_1d: pd.DataFrame   # Last 7 years of daily candles
    spread: float
    volume_24h: float
    maker_fee_pct: float = 0.25   # Per-pair fee from Kraken (%)
    taker_fee_pct: float = 0.40   # Per-pair fee from Kraken (%)


@dataclass(frozen=True)
class Portfolio:
    cash: float
    total_value: float
    positions: list[OpenPosition]
    recent_trades: list[ClosedTrade]  # Last 100
    daily_pnl: float
    total_pnl: float
    fees_today: float


@dataclass(frozen=True)
class RiskLimits:
    max_trade_pct: float
    default_trade_pct: float
    max_positions: int
    max_daily_loss_pct: float
    max_drawdown_pct: float
    max_position_pct: float = 0.25


# --- Output Types (Strategy -> Shell) ---

@dataclass
class Signal:
    symbol: str
    action: Action
    size_pct: float                      # 0.0-1.0 of portfolio
    order_type: OrderType = OrderType.MARKET
    limit_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    intent: Intent = Intent.DAY
    confidence: float = 0.5
    reasoning: str = ""
    slippage_tolerance: Optional[float] = None  # Override default; e.g. 0.0005 = 0.05%
    tag: Optional[str] = None                    # Position tag for multi-position / MODIFY

    def __post_init__(self):
        if self.action == Action.MODIFY and self.size_pct != 0:
            logging.getLogger(__name__).warning(
                "Signal MODIFY has size_pct=%s (ignored — MODIFY only updates SL/TP/intent)",
                self.size_pct,
            )


# --- Strategy Interface ---

class StrategyBase:
    """Base class that defines the IO contract for strategy modules.

    Strategies MUST implement: initialize(), analyze()
    Strategies SHOULD implement: on_fill(), on_position_closed(), get_state(), load_state()
    Strategies MAY override: scan_interval_minutes
    """

    def initialize(self, risk_limits: RiskLimits, symbols: list[str]) -> None:
        """Called once on startup. Store risk limits and symbol list."""
        raise NotImplementedError

    def analyze(
        self,
        markets: dict[str, SymbolData],
        portfolio: Portfolio,
        timestamp: datetime,
    ) -> list[Signal]:
        """Called every scan interval. Return trading signals (may be empty)."""
        raise NotImplementedError

    def on_fill(self, symbol: str, action: Action, qty: float, price: float, intent: Intent, tag: str = "") -> None:
        """Called when an order is filled. Update internal state."""
        pass

    def on_position_closed(self, symbol: str, pnl: float, pnl_pct: float, tag: str = "") -> None:
        """Called when a position is fully closed. Record outcome."""
        pass

    def get_state(self) -> dict:
        """Serialize internal state for persistence across restarts."""
        return {}

    def load_state(self, state: dict) -> None:
        """Restore internal state after restart."""
        pass

    @property
    def scan_interval_minutes(self) -> int:
        """How often analyze() should be called. Default 5 minutes."""
        return 5


# --- Analysis Module Interface ---

class AnalysisBase:
    """Base class for analysis modules (market analysis + trade performance).

    Analysis modules receive a read-only database connection and return
    a dict of computed metrics. The orchestrator can rewrite these modules.
    """

    async def analyze(self, db, schema: dict) -> dict:
        """Run analysis and return structured report.

        Args:
            db: ReadOnlyDB instance (SELECT only)
            schema: Dict describing all tables, columns, and types

        Returns:
            Dict of computed metrics. Structure is up to the module.
        """
        raise NotImplementedError
