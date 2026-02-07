"""Configuration loading and validation.

Loads three config sources:
- settings.toml: General settings (human-controlled)
- risk_limits.toml: Hard risk limits (NEVER AI-modified)
- strategy_params.json: Mutable strategy parameters (AI can adjust)
- .env: API keys and secrets
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
import os


CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"
PROJECT_ROOT = CONFIG_DIR.parent


@dataclass(frozen=True)
class KrakenConfig:
    rest_url: str
    ws_url: str
    api_key: str
    secret_key: str
    maker_fee_pct: float
    taker_fee_pct: float


@dataclass(frozen=True)
class MarketConfig:
    crypto_symbols: list[str]


@dataclass(frozen=True)
class AnalystConfig:
    scan_interval_minutes: int
    min_signal_strength: float
    daily_token_limit: int
    model: str


@dataclass(frozen=True)
class ExecutiveConfig:
    model: str
    daily_token_limit: int
    evolution_hour: int
    evolution_minute: int


@dataclass(frozen=True)
class TelegramConfig:
    enabled: bool
    bot_token: str
    chat_id: str
    allowed_user_ids: list[int]


@dataclass(frozen=True)
class FeeConfig:
    check_interval_hours: int
    min_profit_fee_ratio: float
    min_trade_usd: float


@dataclass(frozen=True)
class PositionLimits:
    max_position_pct: float
    max_positions: int
    max_leverage: float


@dataclass(frozen=True)
class DailyLimits:
    max_daily_loss_pct: float
    max_daily_trades: int


@dataclass(frozen=True)
class PerTradeLimits:
    max_trade_pct: float
    default_stop_loss_pct: float
    default_take_profit_pct: float


@dataclass(frozen=True)
class EmergencyLimits:
    kill_switch: bool
    max_drawdown_pct: float


@dataclass(frozen=True)
class RiskLimits:
    position: PositionLimits
    daily: DailyLimits
    per_trade: PerTradeLimits
    emergency: EmergencyLimits


@dataclass
class StrategyParams:
    """Mutable strategy parameters — the Executive Brain can adjust these."""
    version: int
    last_updated: str | None
    updated_by: str
    momentum: dict[str, Any]
    mean_reversion: dict[str, Any]
    volume: dict[str, Any]
    trend: dict[str, Any]

    _path: Path = field(default=CONFIG_DIR / "strategy_params.json", repr=False)

    def save(self) -> None:
        """Write current parameters back to disk."""
        data = {
            "version": self.version,
            "last_updated": self.last_updated,
            "updated_by": self.updated_by,
            "momentum": self.momentum,
            "mean_reversion": self.mean_reversion,
            "volume": self.volume,
            "trend": self.trend,
        }
        self._path.write_text(json.dumps(data, indent=2) + "\n")


@dataclass(frozen=True)
class Config:
    mode: str  # "paper" or "live"
    paper_balance_usd: float
    timezone: str
    log_level: str
    kraken: KrakenConfig
    markets: MarketConfig
    analyst: AnalystConfig
    executive: ExecutiveConfig
    telegram: TelegramConfig
    fees: FeeConfig
    risk: RiskLimits
    strategy: StrategyParams

    @staticmethod
    def load(config_dir: Path | None = None) -> Config:
        """Load all configuration from files and environment."""
        config_dir = config_dir or CONFIG_DIR
        load_dotenv(PROJECT_ROOT / ".env")

        # Load TOML configs
        with open(config_dir / "settings.toml", "rb") as f:
            settings = tomllib.load(f)

        with open(config_dir / "risk_limits.toml", "rb") as f:
            risk = tomllib.load(f)

        # Load mutable strategy params
        strategy_path = config_dir / "strategy_params.json"
        with open(strategy_path) as f:
            strategy_data = json.load(f)

        return Config(
            mode=settings["general"]["mode"],
            paper_balance_usd=settings["general"].get("paper_balance_usd", 1000.0),
            timezone=settings["general"]["timezone"],
            log_level=settings["general"]["log_level"],
            kraken=KrakenConfig(
                rest_url=settings["kraken"]["rest_url"],
                ws_url=settings["kraken"]["ws_url"],
                api_key=os.getenv("KRAKEN_API_KEY", ""),
                secret_key=os.getenv("KRAKEN_SECRET_KEY", ""),
                maker_fee_pct=settings["kraken"]["maker_fee_pct"],
                taker_fee_pct=settings["kraken"]["taker_fee_pct"],
            ),
            markets=MarketConfig(
                crypto_symbols=settings["markets"]["crypto_symbols"],
            ),
            analyst=AnalystConfig(
                scan_interval_minutes=settings["analyst"]["scan_interval_minutes"],
                min_signal_strength=settings["analyst"]["min_signal_strength"],
                daily_token_limit=settings["analyst"]["daily_token_limit"],
                model=settings["analyst"]["model"],
            ),
            executive=ExecutiveConfig(
                model=settings["executive"]["model"],
                daily_token_limit=settings["executive"]["daily_token_limit"],
                evolution_hour=settings["executive"]["evolution_hour"],
                evolution_minute=settings["executive"]["evolution_minute"],
            ),
            telegram=TelegramConfig(
                enabled=settings["telegram"]["enabled"],
                bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
                chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
                allowed_user_ids=settings["telegram"]["allowed_user_ids"],
            ),
            fees=FeeConfig(
                check_interval_hours=settings["fees"]["check_interval_hours"],
                min_profit_fee_ratio=settings["fees"]["min_profit_fee_ratio"],
                min_trade_usd=settings["fees"]["min_trade_usd"],
            ),
            risk=RiskLimits(
                position=PositionLimits(**risk["position"]),
                daily=DailyLimits(**risk["daily"]),
                per_trade=PerTradeLimits(**risk["per_trade"]),
                emergency=EmergencyLimits(**risk["emergency"]),
            ),
            strategy=StrategyParams(
                **strategy_data,
                _path=strategy_path,
            ),
        )
