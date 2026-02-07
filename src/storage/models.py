"""Data models for the trading system.

Plain dataclasses used throughout the application.
These are NOT ORM models — they're used for passing data between components.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Trade:
    symbol: str
    side: str  # "buy" or "sell"
    qty: float
    price: float
    order_type: str  # "market", "limit", "stop_loss", "take_profit"
    id: int | None = None
    filled_price: float | None = None
    status: str = "pending"
    signal_id: int | None = None
    exchange_order_id: str | None = None
    pnl: float | None = None
    commission: float = 0.0
    created_at: str | None = None
    filled_at: str | None = None
    notes: str | None = None


@dataclass
class Position:
    symbol: str
    qty: float
    avg_entry: float
    side: str = "long"
    id: int | None = None
    current_price: float | None = None
    unrealized_pnl: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    updated_at: str | None = None

    @property
    def market_value(self) -> float | None:
        if self.current_price is None:
            return None
        return self.qty * self.current_price


@dataclass
class Signal:
    symbol: str
    signal_type: str  # e.g. "momentum", "mean_reversion", "trend"
    strength: float  # 0.0 to 1.0
    direction: str  # "long", "short", "close"
    id: int | None = None
    reasoning: str | None = None
    ai_response: str | None = None
    acted_on: bool = False
    created_at: str | None = None


@dataclass
class DailyPerformance:
    date: str
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    max_drawdown_pct: float = 0.0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    token_usage: int = 0
    token_cost_usd: float = 0.0
    portfolio_value: float | None = None
    notes: str | None = None


@dataclass
class FeeSchedule:
    maker_fee_pct: float
    taker_fee_pct: float
    volume_30d_usd: float | None = None
    fee_tier: str | None = None
    checked_at: str | None = None

    def estimate_cost(self, trade_usd: float, is_maker: bool = False) -> float:
        """Estimate fee cost for a given trade size."""
        rate = self.maker_fee_pct if is_maker else self.taker_fee_pct
        return trade_usd * (rate / 100.0)

    def round_trip_cost(self, trade_usd: float, is_maker: bool = False) -> float:
        """Estimate round-trip fee cost (entry + exit)."""
        return self.estimate_cost(trade_usd, is_maker) * 2
