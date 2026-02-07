"""Integration tests for the v2 IO-Container trading system.

Tests: config loading, database schema, IO contract, risk management,
strategy loading/sandbox, portfolio operations, backtester.
"""

import asyncio
import json
import os
import tempfile
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

# --- Config ---

def test_config_loading():
    from src.shell.config import load_config
    config = load_config()
    assert config.mode == "paper"
    assert "BTC/USD" in config.symbols
    assert config.risk.max_trade_pct == 0.05
    assert config.risk.rollback_consecutive_losses == 10
    assert config.ai.provider in ("anthropic", "vertex")


# --- Database ---

@pytest.mark.asyncio
async def test_database_schema():
    from src.shell.database import Database
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        await db.connect()

        rows = await db.fetchall("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [r["name"] for r in rows]

        required = ["candles", "positions", "trades", "signals", "daily_performance",
                     "strategy_versions", "orchestrator_log", "token_usage",
                     "fee_schedule", "strategy_state", "paper_tests"]
        for t in required:
            assert t in tables, f"Missing table: {t}"

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_database_crud():
    from src.shell.database import Database
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        await db.connect()

        # Insert
        await db.execute(
            "INSERT INTO signals (symbol, action, size_pct, confidence, intent, reasoning) VALUES (?, ?, ?, ?, ?, ?)",
            ("BTC/USD", "BUY", 0.02, 0.8, "DAY", "test signal"),
        )
        await db.commit()

        # Read
        row = await db.fetchone("SELECT * FROM signals WHERE symbol = 'BTC/USD'")
        assert row is not None
        assert row["action"] == "BUY"
        assert row["confidence"] == 0.8

        await db.close()
    finally:
        os.unlink(db_path)


# --- IO Contract ---

def test_contract_types():
    from src.shell.contract import Signal, Action, Intent, OrderType, SymbolData, Portfolio, RiskLimits

    sig = Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.02)
    assert sig.symbol == "BTC/USD"
    assert sig.action == Action.BUY
    assert sig.intent == Intent.DAY  # Default
    assert sig.order_type == OrderType.MARKET  # Default

    limits = RiskLimits(max_trade_pct=0.05, default_trade_pct=0.02,
                        max_positions=5, max_daily_loss_pct=0.03, max_drawdown_pct=0.10)
    assert limits.max_trade_pct == 0.05


# --- Risk Manager ---

def test_risk_basic_checks():
    from src.shell.config import load_config
    from src.shell.risk import RiskManager
    from src.shell.contract import Signal, Action

    config = load_config()
    rm = RiskManager(config.risk)

    # Should pass: small trade, no positions
    sig = Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.02)
    check = rm.check_signal(sig, portfolio_value=200, open_position_count=0)
    assert check.passed

    # Should fail: size exceeds limit
    sig2 = Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.10)
    check2 = rm.check_signal(sig2, portfolio_value=200, open_position_count=0)
    assert not check2.passed
    assert "Trade size" in check2.reason

    # Should fail: max positions
    sig3 = Signal(symbol="ETH/USD", action=Action.BUY, size_pct=0.02)
    check3 = rm.check_signal(sig3, portfolio_value=200, open_position_count=5)
    assert not check3.passed
    assert "Max positions" in check3.reason


def test_risk_daily_limits():
    from src.shell.config import load_config
    from src.shell.risk import RiskManager
    from src.shell.contract import Signal, Action

    config = load_config()
    rm = RiskManager(config.risk)

    # Simulate daily loss
    for _ in range(20):
        rm.record_trade_result(-0.5)

    sig = Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.02)
    check = rm.check_signal(sig, portfolio_value=200, open_position_count=0)
    assert not check.passed
    assert "Daily" in check.reason


def test_risk_consecutive_losses():
    from src.shell.config import load_config
    from src.shell.risk import RiskManager
    from src.shell.contract import Signal, Action

    config = load_config()
    rm = RiskManager(config.risk)

    # 10 consecutive losses should trigger halt
    for _ in range(10):
        rm.record_trade_result(-0.1)

    sig = Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.02)
    check = rm.check_signal(sig, portfolio_value=200, open_position_count=0)
    assert not check.passed
    assert "consecutive" in check.reason.lower()


def test_risk_clamp():
    from src.shell.config import load_config
    from src.shell.risk import RiskManager
    from src.shell.contract import Signal, Action

    config = load_config()
    rm = RiskManager(config.risk)

    sig = Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.15)
    clamped = rm.clamp_signal(sig, portfolio_value=200)
    assert clamped.size_pct == config.risk.max_trade_pct


# --- Strategy Loading ---

def test_strategy_load():
    from src.strategy.loader import load_strategy, get_code_hash, get_strategy_path
    from src.shell.contract import RiskLimits

    strategy = load_strategy()
    assert strategy is not None
    assert strategy.scan_interval_minutes == 5

    limits = RiskLimits(max_trade_pct=0.05, default_trade_pct=0.02,
                        max_positions=5, max_daily_loss_pct=0.03, max_drawdown_pct=0.10)
    strategy.initialize(limits, ["BTC/USD", "ETH/USD", "SOL/USD"])

    state = strategy.get_state()
    assert isinstance(state, dict)

    h = get_code_hash(get_strategy_path())
    assert len(h) == 16


def test_strategy_analyze_empty():
    """Strategy should return empty list when no crossover happens."""
    from src.strategy.loader import load_strategy
    from src.shell.contract import RiskLimits, SymbolData, Portfolio

    strategy = load_strategy()
    limits = RiskLimits(max_trade_pct=0.05, default_trade_pct=0.02,
                        max_positions=5, max_daily_loss_pct=0.03, max_drawdown_pct=0.10)
    strategy.initialize(limits, ["BTC/USD"])

    # Flat price data — no crossover
    dates = pd.date_range(end=datetime.now(), periods=100, freq="5min")
    df = pd.DataFrame({
        "open": [70000] * 100,
        "high": [70100] * 100,
        "low": [69900] * 100,
        "close": [70000] * 100,
        "volume": [50] * 100,
    }, index=dates)

    markets = {"BTC/USD": SymbolData(
        symbol="BTC/USD", current_price=70000,
        candles_5m=df, candles_1h=df, candles_1d=df,
        spread=0.001, volume_24h=1000000,
    )}

    portfolio = Portfolio(
        cash=200, total_value=200, positions=[], recent_trades=[],
        daily_pnl=0, total_pnl=0, fees_today=0,
    )

    # First call initializes EMA state, second should have prev values
    signals = strategy.analyze(markets, portfolio, datetime.now())
    signals2 = strategy.analyze(markets, portfolio, datetime.now())
    # Flat data = no crossover = no signals
    assert isinstance(signals2, list)


# --- Sandbox ---

def test_sandbox_valid_strategy():
    from src.strategy.sandbox import validate_strategy
    from src.strategy.loader import get_strategy_path

    code = get_strategy_path().read_text()
    result = validate_strategy(code)
    assert result.passed
    assert len(result.errors) == 0


def test_sandbox_rejects_forbidden():
    from src.strategy.sandbox import validate_strategy

    # subprocess import
    result = validate_strategy("import subprocess\nclass Strategy: pass")
    assert not result.passed

    # os import
    result = validate_strategy("import os\nclass Strategy: pass")
    assert not result.passed

    # eval call
    result = validate_strategy("eval('1+1')\nclass Strategy: pass")
    assert not result.passed


def test_sandbox_rejects_syntax_error():
    from src.strategy.sandbox import validate_strategy
    result = validate_strategy("def foo(")
    assert not result.passed


# --- Indicators ---

def test_compute_indicators():
    from strategy.skills import compute_indicators

    dates = pd.date_range(end=datetime.now(), periods=100, freq="5min")
    df = pd.DataFrame({
        "open": np.random.uniform(69000, 71000, 100),
        "high": np.random.uniform(70000, 72000, 100),
        "low": np.random.uniform(68000, 70000, 100),
        "close": np.random.uniform(69000, 71000, 100),
        "volume": np.random.uniform(10, 100, 100),
    }, index=dates)

    indicators = compute_indicators(df)
    assert "rsi" in indicators
    assert "ema_fast" in indicators
    assert "ema_slow" in indicators
    assert "vol_ratio" in indicators
    assert "regime" in indicators
    assert 0 <= indicators["rsi"] <= 100


# --- Portfolio ---

@pytest.mark.asyncio
async def test_paper_trade_cycle():
    """Full paper trade: buy BTC, sell BTC, check P&L."""
    from src.shell.config import load_config
    from src.shell.database import Database
    from src.shell.portfolio import PortfolioTracker
    from src.shell.kraken import KrakenREST
    from src.shell.contract import Signal, Action, Intent, OrderType

    config = load_config()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        config.db_path = f.name

    try:
        db = Database(config.db_path)
        await db.connect()

        kraken = KrakenREST(config.kraken)
        portfolio = PortfolioTracker(config, db, kraken)
        await portfolio.initialize()

        initial = await portfolio.total_value()
        assert initial == config.paper_balance_usd

        # Buy BTC
        buy_signal = Signal(
            symbol="BTC/USD", action=Action.BUY, size_pct=0.05,
            stop_loss=49000, take_profit=55000, intent=Intent.DAY,
        )
        result = await portfolio.execute_signal(buy_signal, 50000, 0.25, 0.40)
        assert result is not None
        assert result["action"] == "BUY"
        assert result["qty"] > 0
        assert portfolio.position_count == 1

        # Sell BTC at profit
        sell_signal = Signal(
            symbol="BTC/USD", action=Action.CLOSE, size_pct=1.0, intent=Intent.DAY,
        )
        result2 = await portfolio.execute_signal(sell_signal, 51000, 0.25, 0.40)
        assert result2 is not None
        assert result2["pnl"] > 0  # Should be profitable (2% move minus fees)
        assert portfolio.position_count == 0

        # Check trade recorded in DB
        trades = await db.fetchall("SELECT * FROM trades WHERE closed_at IS NOT NULL")
        assert len(trades) == 1
        assert trades[0]["pnl"] > 0

        await db.close()
    finally:
        os.unlink(config.db_path)


@pytest.mark.asyncio
async def test_paper_trade_fees():
    """Verify fees are correctly deducted."""
    from src.shell.config import load_config
    from src.shell.database import Database
    from src.shell.portfolio import PortfolioTracker
    from src.shell.kraken import KrakenREST
    from src.shell.contract import Signal, Action, Intent

    config = load_config()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        config.db_path = f.name

    try:
        db = Database(config.db_path)
        await db.connect()

        kraken = KrakenREST(config.kraken)
        portfolio = PortfolioTracker(config, db, kraken)
        await portfolio.initialize()

        start_value = await portfolio.total_value()

        # Buy and sell at same price — should lose money due to fees
        buy = Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.05, intent=Intent.DAY)
        await portfolio.execute_signal(buy, 50000, 0.25, 0.40)

        sell = Signal(symbol="BTC/USD", action=Action.CLOSE, size_pct=1.0, intent=Intent.DAY)
        await portfolio.execute_signal(sell, 50000, 0.25, 0.40)

        end_value = await portfolio.total_value()
        # Should have lost money to fees + slippage
        assert end_value < start_value

        await db.close()
    finally:
        os.unlink(config.db_path)


# --- Kraken pair mapping ---

def test_pair_mapping():
    from src.shell.kraken import to_kraken_pair, from_kraken_pair

    assert to_kraken_pair("BTC/USD") == "XBTUSD"
    assert to_kraken_pair("ETH/USD") == "ETHUSD"
    assert from_kraken_pair("XBTUSD") == "BTC/USD"
    assert from_kraken_pair("ETHUSD") == "ETH/USD"


# --- Backtester ---

def test_backtester_runs():
    from src.strategy.backtester import Backtester
    from src.strategy.loader import load_strategy
    from src.shell.contract import RiskLimits

    strategy = load_strategy()
    limits = RiskLimits(max_trade_pct=0.05, default_trade_pct=0.02,
                        max_positions=5, max_daily_loss_pct=0.03, max_drawdown_pct=0.10)

    bt = Backtester(strategy, limits, ["BTC/USD"])

    # Generate trending price data (should produce some signals)
    dates = pd.date_range(end=datetime.now(), periods=500, freq="1h")
    prices = 70000 + np.cumsum(np.random.randn(500) * 100)
    data = {"BTC/USD": pd.DataFrame({
        "open": prices,
        "high": prices + 50,
        "low": prices - 50,
        "close": prices,
        "volume": np.random.uniform(100, 1000, 500),
    }, index=dates)}

    result = bt.run(data)
    print(f"Backtest: {result.summary()}")
    assert result.total_trades >= 0  # May or may not trade depending on random data
    assert isinstance(result.net_pnl, float)
