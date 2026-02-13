"""Configuration loading — merges settings.toml, risk_limits.toml, and .env."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"


@dataclass
class KrakenConfig:
    rest_url: str = "https://api.kraken.com"
    ws_url: str = "wss://ws.kraken.com/v2"
    api_key: str = ""
    secret_key: str = ""
    maker_fee_pct: float = 0.25
    taker_fee_pct: float = 0.40


@dataclass
class AIConfig:
    provider: str = "anthropic"
    anthropic_api_key: str = ""
    sonnet_model: str = "claude-sonnet-4-5-20250929"
    opus_model: str = "claude-opus-4-6"
    haiku_model: str = "claude-haiku-4-5-20251001"
    daily_token_limit: int = 1500000
    vertex_project_id: str = ""
    vertex_region: str = "us-east5"


@dataclass
class NotificationConfig:
    """Which events send Telegram alerts. All default True except high-frequency ones."""
    trade_executed: bool = True
    stop_triggered: bool = True
    risk_halt: bool = True
    risk_resumed: bool = True
    strategy_rollback: bool = True
    strategy_deployed: bool = True
    system_online: bool = True
    system_shutdown: bool = True
    system_error: bool = True
    websocket_feed_lost: bool = True
    daily_summary: bool = True
    weekly_report: bool = True
    # High-frequency — default off for Telegram
    signal_rejected: bool = False
    scan_complete: bool = False
    paper_test_started: bool = False
    paper_test_completed: bool = False
    orchestrator_cycle_started: bool = False
    orchestrator_cycle_completed: bool = False
    # Candidate events
    candidate_created: bool = True
    candidate_canceled: bool = True
    candidate_promoted: bool = True


@dataclass
class ApiConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8080


@dataclass
class TelegramConfig:
    enabled: bool = True
    bot_token: str = ""
    chat_id: str = ""
    allowed_user_ids: list[int] = field(default_factory=list)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)


@dataclass
class OrchestratorConfig:
    start_hour: int = 3
    start_minute: int = 30
    end_hour: int = 6
    max_revisions: int = 12             # inner loop: code quality iterations
    max_strategy_iterations: int = 9    # outer loop: Opus strategy direction
    max_candidates: int = 3             # max simultaneous candidate strategy slots


@dataclass
class DataConfig:
    candle_5m_retention_days: int = 30
    candle_1h_retention_days: int = 365
    candle_1d_retention_years: int = 7


@dataclass
class FeeConfig:
    check_interval_hours: int = 24


@dataclass
class RiskConfig:
    max_position_pct: float = 0.25
    max_positions: int = 5
    max_leverage: float = 1.0
    max_daily_loss_pct: float = 0.10
    max_daily_trades: int = 20
    max_trade_pct: float = 0.10
    default_trade_pct: float = 0.03
    default_stop_loss_pct: float = 0.02
    default_take_profit_pct: float = 0.06
    kill_switch: bool = False
    max_drawdown_pct: float = 0.40
    rollback_daily_loss_pct: float = 0.15
    rollback_consecutive_losses: int = 999


@dataclass
class Config:
    mode: str = "paper"
    paper_balance_usd: float = 200.0
    timezone: str = "US/Eastern"
    log_level: str = "INFO"
    default_slippage_factor: float = 0.0005  # 0.05% as decimal — used when signal has no override
    symbols: list[str] = field(default_factory=lambda: [
        "BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "DOGE/USD",
        "ADA/USD", "LINK/USD", "AVAX/USD", "DOT/USD",
    ])
    kraken: KrakenConfig = field(default_factory=KrakenConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    orchestrator: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    data: DataConfig = field(default_factory=DataConfig)
    fees: FeeConfig = field(default_factory=FeeConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    db_path: str = ""

    def is_paper(self) -> bool:
        return self.mode == "paper"


def load_config() -> Config:
    """Load configuration from TOML files and environment variables."""
    load_dotenv(PROJECT_ROOT / ".env")

    config = Config()
    config.db_path = str(PROJECT_ROOT / "data" / "brain.db")

    # Load settings.toml
    settings_path = CONFIG_DIR / "settings.toml"
    if settings_path.exists():
        with open(settings_path, "rb") as f:
            settings = tomllib.load(f)

        general = settings.get("general", {})
        config.mode = general.get("mode", config.mode)
        config.paper_balance_usd = general.get("paper_balance_usd", config.paper_balance_usd)
        config.timezone = general.get("timezone", config.timezone)
        config.log_level = general.get("log_level", config.log_level)
        config.default_slippage_factor = general.get("default_slippage_factor", config.default_slippage_factor)

        markets = settings.get("markets", {})
        config.symbols = markets.get("symbols", config.symbols)

        kraken = settings.get("kraken", {})
        config.kraken.rest_url = kraken.get("rest_url", config.kraken.rest_url)
        config.kraken.ws_url = kraken.get("ws_url", config.kraken.ws_url)
        config.kraken.maker_fee_pct = kraken.get("maker_fee_pct", config.kraken.maker_fee_pct)
        config.kraken.taker_fee_pct = kraken.get("taker_fee_pct", config.kraken.taker_fee_pct)

        ai = settings.get("ai", {})
        config.ai.provider = ai.get("provider", config.ai.provider)
        config.ai.sonnet_model = ai.get("sonnet_model", config.ai.sonnet_model)
        config.ai.opus_model = ai.get("opus_model", config.ai.opus_model)
        config.ai.haiku_model = ai.get("haiku_model", config.ai.haiku_model)
        config.ai.daily_token_limit = ai.get("daily_token_limit", config.ai.daily_token_limit)

        vertex = ai.get("vertex", {})
        config.ai.vertex_project_id = vertex.get("project_id", config.ai.vertex_project_id)
        config.ai.vertex_region = vertex.get("region", config.ai.vertex_region)

        orch = settings.get("orchestrator", {})
        config.orchestrator.start_hour = orch.get("start_hour", config.orchestrator.start_hour)
        config.orchestrator.start_minute = orch.get("start_minute", config.orchestrator.start_minute)
        config.orchestrator.end_hour = orch.get("end_hour", config.orchestrator.end_hour)
        config.orchestrator.max_revisions = orch.get("max_revisions", config.orchestrator.max_revisions)
        config.orchestrator.max_strategy_iterations = orch.get("max_strategy_iterations", config.orchestrator.max_strategy_iterations)
        config.orchestrator.max_candidates = orch.get("max_candidates", config.orchestrator.max_candidates)

        tg = settings.get("telegram", {})
        config.telegram.enabled = tg.get("enabled", config.telegram.enabled)
        config.telegram.allowed_user_ids = tg.get("allowed_user_ids", config.telegram.allowed_user_ids)

        tg_notif = tg.get("notifications", {})
        for key in vars(config.telegram.notifications):
            if key in tg_notif:
                setattr(config.telegram.notifications, key, tg_notif[key])

        api = settings.get("api", {})
        config.api.enabled = api.get("enabled", config.api.enabled)
        config.api.host = api.get("host", config.api.host)
        config.api.port = api.get("port", config.api.port)

        fees = settings.get("fees", {})
        config.fees.check_interval_hours = fees.get("check_interval_hours", config.fees.check_interval_hours)

        data = settings.get("data", {})
        config.data.candle_5m_retention_days = data.get("candle_5m_retention_days", config.data.candle_5m_retention_days)
        config.data.candle_1h_retention_days = data.get("candle_1h_retention_days", config.data.candle_1h_retention_days)
        config.data.candle_1d_retention_years = data.get("candle_1d_retention_years", config.data.candle_1d_retention_years)

    # Load risk_limits.toml
    risk_path = CONFIG_DIR / "risk_limits.toml"
    if risk_path.exists():
        with open(risk_path, "rb") as f:
            risk = tomllib.load(f)

        pos = risk.get("position", {})
        config.risk.max_position_pct = pos.get("max_position_pct", config.risk.max_position_pct)
        config.risk.max_positions = pos.get("max_positions", config.risk.max_positions)
        config.risk.max_leverage = pos.get("max_leverage", config.risk.max_leverage)

        daily = risk.get("daily", {})
        config.risk.max_daily_loss_pct = daily.get("max_daily_loss_pct", config.risk.max_daily_loss_pct)
        config.risk.max_daily_trades = daily.get("max_daily_trades", config.risk.max_daily_trades)

        per_trade = risk.get("per_trade", {})
        config.risk.max_trade_pct = per_trade.get("max_trade_pct", config.risk.max_trade_pct)
        config.risk.default_trade_pct = per_trade.get("default_trade_pct", config.risk.default_trade_pct)
        config.risk.default_stop_loss_pct = per_trade.get("default_stop_loss_pct", config.risk.default_stop_loss_pct)
        config.risk.default_take_profit_pct = per_trade.get("default_take_profit_pct", config.risk.default_take_profit_pct)

        emergency = risk.get("emergency", {})
        config.risk.kill_switch = emergency.get("kill_switch", config.risk.kill_switch)
        config.risk.max_drawdown_pct = emergency.get("max_drawdown_pct", config.risk.max_drawdown_pct)

        rollback = risk.get("rollback", {})
        config.risk.rollback_daily_loss_pct = rollback.get("max_daily_loss_pct", config.risk.rollback_daily_loss_pct)
        config.risk.rollback_consecutive_losses = rollback.get("max_consecutive_losses", config.risk.rollback_consecutive_losses)

    # Environment variables (secrets)
    config.kraken.api_key = os.getenv("KRAKEN_API_KEY", "")
    config.kraken.secret_key = os.getenv("KRAKEN_SECRET_KEY", "")
    config.ai.anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "")
    config.telegram.bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    config.telegram.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    # Validate critical values
    _validate_config(config)

    return config


def _validate_config(config: Config) -> None:
    """Validate config values are within sane ranges."""
    from zoneinfo import ZoneInfo

    errors = []

    if not (0 < config.risk.max_trade_pct <= 1):
        errors.append(f"max_trade_pct must be 0-1, got {config.risk.max_trade_pct}")
    if not (0 < config.risk.max_position_pct <= 1):
        errors.append(f"max_position_pct must be 0-1, got {config.risk.max_position_pct}")
    if not (0 < config.risk.max_daily_loss_pct <= 1):
        errors.append(f"max_daily_loss_pct must be 0-1, got {config.risk.max_daily_loss_pct}")
    if not (0 < config.risk.max_drawdown_pct <= 1):
        errors.append(f"max_drawdown_pct must be 0-1, got {config.risk.max_drawdown_pct}")
    if config.risk.max_positions < 1:
        errors.append(f"max_positions must be >= 1, got {config.risk.max_positions}")
    if not config.symbols:
        errors.append("At least one trading symbol must be configured")
    if config.mode not in ("paper", "live"):
        errors.append(f"mode must be 'paper' or 'live', got '{config.mode}'")
    if config.paper_balance_usd <= 0:
        errors.append(f"paper_balance_usd must be > 0, got {config.paper_balance_usd}")
    if not (0 <= config.default_slippage_factor <= 0.05):
        errors.append(f"default_slippage_factor must be 0-0.05, got {config.default_slippage_factor}")
    if config.risk.max_daily_trades < 1:
        errors.append(f"max_daily_trades must be >= 1, got {config.risk.max_daily_trades}")
    if config.risk.rollback_consecutive_losses < 1:
        errors.append(f"rollback_consecutive_losses must be >= 1, got {config.risk.rollback_consecutive_losses}")
    if config.fees.check_interval_hours < 1:
        errors.append(f"fees.check_interval_hours must be >= 1, got {config.fees.check_interval_hours}")
    if not (0 < config.risk.rollback_daily_loss_pct <= 1):
        errors.append(f"rollback_daily_loss_pct must be 0-1, got {config.risk.rollback_daily_loss_pct}")
    if hasattr(config, 'api') and config.api.enabled:
        if not (1 <= config.api.port <= 65535):
            errors.append(f"api.port must be 1-65535, got {config.api.port}")

    # L6: Timezone validity
    try:
        ZoneInfo(config.timezone)
    except (KeyError, Exception):
        errors.append(f"Invalid timezone: '{config.timezone}'")

    # L6: Symbol format — must contain "/" and end with "USD"
    for sym in config.symbols:
        if "/" not in sym:
            errors.append(f"Symbol must contain '/' separator: '{sym}'")
        elif not sym.endswith("USD"):
            errors.append(f"Symbol must end with 'USD': '{sym}'")

    # L6: Trade size consistency
    if config.risk.default_trade_pct > config.risk.max_trade_pct:
        errors.append(f"default_trade_pct ({config.risk.default_trade_pct}) > max_trade_pct ({config.risk.max_trade_pct})")
    if config.risk.max_trade_pct > config.risk.max_position_pct:
        errors.append(f"max_trade_pct ({config.risk.max_trade_pct}) > max_position_pct ({config.risk.max_position_pct})")

    if errors:
        raise ValueError("Config validation failed:\n  " + "\n  ".join(errors))
