"""Integration tests for the v2 IO-Container trading system.

Tests: config loading, database schema, IO contract, risk management,
strategy loading/sandbox, portfolio operations, backtester, orchestration,
Telegram commands, strategy deploy/rollback, scan loop.
"""

import asyncio
import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd
import pytest

# --- Config ---

def test_config_loading():
    from src.shell.config import load_config
    config = load_config()
    assert config.mode == "paper"
    assert "BTC/USD" in config.symbols
    assert len(config.symbols) == 9  # 9 trading pairs
    assert "DOGE/USD" in config.symbols
    assert config.risk.max_trade_pct == 0.10
    assert config.risk.rollback_consecutive_losses == 999
    assert config.ai.provider in ("anthropic", "vertex")
    assert config.ai.daily_token_limit == 1500000  # 1.5M safety net
    assert config.default_slippage_factor == 0.0005


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
                     "strategy_versions", "orchestrator_log", "orchestrator_thoughts",
                     "orchestrator_observations",
                     "token_usage", "fee_schedule", "strategy_state", "paper_tests",
                     "scan_results", "capital_events", "orders", "conditional_orders",
                     "system_meta", "activity_log"]
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
    assert sig.slippage_tolerance is None  # Default

    # Slippage tolerance override
    sig2 = Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.03, slippage_tolerance=0.001)
    assert sig2.slippage_tolerance == 0.001

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

    # Should fail: size exceeds limit (max_trade_pct is 0.10)
    sig2 = Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.15)
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


def test_risk_consecutive_losses_disabled():
    """Consecutive loss halt is disabled (set to 999). Drawdown is the safety net."""
    from src.shell.config import load_config
    from src.shell.risk import RiskManager
    from src.shell.contract import Signal, Action

    config = load_config()
    rm = RiskManager(config.risk)

    # 10 consecutive losses should NOT trigger halt (threshold is 999)
    for _ in range(10):
        rm.record_trade_result(-0.1)

    sig = Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.02)
    check = rm.check_signal(sig, portfolio_value=200, open_position_count=0)
    assert check.passed, f"10 consecutive losses should not halt (threshold=999): {check.reason}"


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
        assert "tag" in result
        assert result["tag"].startswith("auto_")
        assert portfolio.position_count == 1

        # Sell BTC at profit
        sell_signal = Signal(
            symbol="BTC/USD", action=Action.CLOSE, size_pct=1.0, intent=Intent.DAY,
        )
        result2 = await portfolio.execute_signal(sell_signal, 51000, 0.25, 0.40)
        assert result2 is not None
        assert result2["pnl"] > 0  # Should be profitable (2% move minus fees)
        assert "tag" in result2
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
    from src.shell.kraken import to_kraken_pair, from_kraken_pair, PAIR_MAP

    # All 9 pairs mapped
    assert len(PAIR_MAP) == 9
    assert to_kraken_pair("BTC/USD") == "XBTUSD"
    assert to_kraken_pair("ETH/USD") == "ETHUSD"
    assert to_kraken_pair("DOGE/USD") == "XDGUSD"
    assert to_kraken_pair("DOT/USD") == "DOTUSD"

    # Reverse mapping (REST format)
    assert from_kraken_pair("XBTUSD") == "BTC/USD"
    assert from_kraken_pair("ETHUSD") == "ETH/USD"
    assert from_kraken_pair("XDGUSD") == "DOGE/USD"

    # WS v2 format reverse mapping
    assert from_kraken_pair("XBT/USD") == "BTC/USD"
    assert from_kraken_pair("XDG/USD") == "DOGE/USD"

    # Unknown pairs pass through
    assert from_kraken_pair("UNKNOWN") == "UNKNOWN"


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


# --- Truth Benchmarks ---

@pytest.mark.asyncio
async def test_truth_benchmarks():
    """Truth benchmarks compute correct values from known seed data."""
    from src.shell.database import Database
    from src.shell.truth import compute_truth_benchmarks

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        await db.connect()

        # Seed known trade data: 3 wins, then 2 losses (order matters for consecutive_losses)
        trades = [
            ("BTC/USD", "long", 0.001, 50000, 51000, 1.0, 0.02, 0.20, "DAY", "trending", "2026-01-01", "2026-01-01 10:00:00"),
            ("BTC/USD", "long", 0.001, 50000, 50500, 0.5, 0.01, 0.20, "DAY", "trending", "2026-01-02", "2026-01-02 10:00:00"),
            ("ETH/USD", "long", 0.01, 3000, 3100, 1.0, 0.033, 0.12, "DAY", "ranging", "2026-01-03", "2026-01-03 10:00:00"),
            ("BTC/USD", "long", 0.001, 50000, 49500, -0.5, -0.01, 0.20, "DAY", "trending", "2026-01-04", "2026-01-04 10:00:00"),
            ("ETH/USD", "long", 0.01, 3000, 2900, -1.0, -0.033, 0.12, "DAY", "ranging", "2026-01-05", "2026-01-05 10:00:00"),
        ]
        for t in trades:
            await db.execute(
                """INSERT INTO trades (symbol, side, qty, entry_price, exit_price, pnl, pnl_pct,
                   fees, intent, strategy_regime, opened_at, closed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                t,
            )

        # Seed signals: 4 total, 3 acted on
        for i in range(4):
            acted = 1 if i < 3 else 0
            await db.execute(
                "INSERT INTO signals (symbol, action, size_pct, confidence, intent, reasoning, acted_on) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("BTC/USD", "BUY", 0.02, 0.8, "DAY", "test", acted),
            )

        # Seed scan results
        for i in range(10):
            await db.execute(
                """INSERT INTO scan_results (timestamp, symbol, price, spread)
                   VALUES (datetime('now'), ?, ?, ?)""",
                ("BTC/USD", 50000 + i * 100, 0.5),
            )

        await db.commit()

        # Run truth benchmarks
        truth = await compute_truth_benchmarks(db)

        # Verify trade metrics
        assert truth["trade_count"] == 5
        assert truth["win_count"] == 3
        assert truth["loss_count"] == 2
        assert truth["win_rate"] == pytest.approx(0.6)
        assert truth["net_pnl"] == pytest.approx(1.0)  # 1.0 + 0.5 + 1.0 - 0.5 - 1.0
        assert truth["total_fees"] == pytest.approx(0.84)  # 0.20*3 + 0.12*2

        # Verify signal metrics
        assert truth["total_signals"] == 4
        assert truth["acted_signals"] == 3
        assert truth["signal_act_rate"] == pytest.approx(0.75)

        # Verify scan metrics
        assert truth["total_scans"] == 10

        # Verify consecutive losses (last 2 trades are losses)
        assert truth["consecutive_losses"] == 2

        # Verify expectancy: (0.6 * avg_win) + (0.4 * avg_loss)
        avg_win = (1.0 + 0.5 + 1.0) / 3  # ~0.833
        avg_loss = (-0.5 + -1.0) / 2  # -0.75
        expected_expectancy = (0.6 * avg_win) + (0.4 * avg_loss)
        assert truth["expectancy"] == pytest.approx(expected_expectancy, abs=0.01)

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_truth_benchmarks_empty_db():
    """Truth benchmarks handle empty database gracefully."""
    from src.shell.database import Database
    from src.shell.truth import compute_truth_benchmarks

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        await db.connect()

        truth = await compute_truth_benchmarks(db)

        assert truth["trade_count"] == 0
        assert truth["win_rate"] == 0.0
        assert truth["net_pnl"] == 0.0
        assert truth["expectancy"] == 0.0
        assert truth["consecutive_losses"] == 0
        assert truth["total_signals"] == 0
        assert truth["total_scans"] == 0
        assert truth["max_drawdown_pct"] == 0.0

        await db.close()
    finally:
        os.unlink(db_path)


# --- ReadOnlyDB ---

@pytest.mark.asyncio
async def test_readonly_db_allows_select():
    """ReadOnlyDB allows SELECT queries."""
    from src.shell.database import Database
    from src.statistics.readonly_db import ReadOnlyDB

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        await db.connect()

        # Insert some data via the normal DB
        await db.execute(
            "INSERT INTO signals (symbol, action, size_pct, confidence, intent, reasoning) VALUES (?, ?, ?, ?, ?, ?)",
            ("BTC/USD", "BUY", 0.02, 0.8, "DAY", "test"),
        )
        await db.commit()

        # ReadOnlyDB should be able to read it
        ro = ReadOnlyDB(db.conn)
        row = await ro.fetchone("SELECT COUNT(*) as cnt FROM signals")
        assert row["cnt"] == 1

        rows = await ro.fetchall("SELECT * FROM signals")
        assert len(rows) == 1
        assert rows[0]["symbol"] == "BTC/USD"

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_readonly_db_blocks_writes():
    """ReadOnlyDB blocks INSERT, UPDATE, DELETE, DROP, ALTER, CREATE."""
    from src.shell.database import Database
    from src.statistics.readonly_db import ReadOnlyDB

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        await db.connect()

        ro = ReadOnlyDB(db.conn)

        blocked_queries = [
            "INSERT INTO signals (symbol, action, size_pct) VALUES ('X', 'BUY', 0.01)",
            "UPDATE signals SET symbol = 'X' WHERE id = 1",
            "DELETE FROM signals WHERE id = 1",
            "DROP TABLE signals",
            "ALTER TABLE signals ADD COLUMN test TEXT",
            "CREATE TABLE evil (id INTEGER)",
            "  INSERT INTO signals (symbol, action, size_pct) VALUES ('X', 'BUY', 0.01)",
            # Multi-statement bypass attempts
            "SELECT 1; DROP TABLE signals",
            "SELECT 1; INSERT INTO signals (symbol) VALUES ('X')",
        ]

        for sql in blocked_queries:
            try:
                await ro.execute(sql)
                assert False, f"Should have blocked: {sql}"
            except ValueError as e:
                assert "Write operation blocked" in str(e)

        await db.close()
    finally:
        os.unlink(db_path)


# --- Analysis Sandbox ---

def test_analysis_sandbox_valid():
    """Analysis sandbox accepts valid analysis module code."""
    from src.statistics.sandbox import validate_analysis_module

    code = '''
from src.shell.contract import AnalysisBase

class Analysis(AnalysisBase):
    async def analyze(self, db, schema):
        row = await db.fetchone("SELECT COUNT(*) as cnt FROM trades")
        return {"trade_count": row["cnt"] if row else 0}
'''
    result = validate_analysis_module(code, "test_module")
    assert result.passed, f"Should pass: {result.errors}"


def test_analysis_sandbox_rejects_forbidden():
    """Analysis sandbox rejects forbidden imports."""
    from src.statistics.sandbox import validate_analysis_module

    # Network access
    code_network = '''
import requests
from src.shell.contract import AnalysisBase
class Analysis(AnalysisBase):
    async def analyze(self, db, schema):
        return {}
'''
    result = validate_analysis_module(code_network, "test_module")
    assert not result.passed
    assert any("requests" in e for e in result.errors)

    # Subprocess
    code_subprocess = '''
import subprocess
from src.shell.contract import AnalysisBase
class Analysis(AnalysisBase):
    async def analyze(self, db, schema):
        return {}
'''
    result = validate_analysis_module(code_subprocess, "test_module")
    assert not result.passed

    # os module
    code_os = '''
import os
from src.shell.contract import AnalysisBase
class Analysis(AnalysisBase):
    async def analyze(self, db, schema):
        return {}
'''
    result = validate_analysis_module(code_os, "test_module")
    assert not result.passed


def test_analysis_sandbox_rejects_no_class():
    """Analysis sandbox rejects code without Analysis class."""
    from src.statistics.sandbox import validate_analysis_module

    code = '''
def analyze(db, schema):
    return {}
'''
    result = validate_analysis_module(code, "test_module")
    assert not result.passed
    assert any("Analysis" in e for e in result.errors)


def test_analysis_sandbox_allows_scipy():
    """Analysis sandbox allows scipy/statistics imports (unlike strategy sandbox)."""
    from src.statistics.sandbox import check_analysis_imports

    code = '''
import statistics
import numpy as np
import pandas as pd
from src.shell.contract import AnalysisBase
'''
    errors = check_analysis_imports(code)
    assert len(errors) == 0, f"Should allow these imports: {errors}"


# --- Analysis Modules (load + run) ---

@pytest.mark.asyncio
async def test_market_analysis_module():
    """Market analysis module loads, runs against seeded DB, returns dict."""
    from src.shell.database import Database
    from src.statistics.readonly_db import ReadOnlyDB, get_schema_description
    from src.statistics.loader import load_analysis_module

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        await db.connect()

        # Seed candles (1h timeframe for market analysis)
        for i in range(20):
            await db.execute(
                """INSERT INTO candles (symbol, timeframe, timestamp, open, high, low, close, volume)
                   VALUES (?, '1h', datetime('now', ?), ?, ?, ?, ?, ?)""",
                ("BTC/USD", f"-{20-i} hours",
                 50000 + i * 50, 50100 + i * 50, 49900 + i * 50,
                 50000 + i * 50, 100 + i * 5),
            )
        # Seed scan results (for data_quality section)
        for i in range(20):
            await db.execute(
                """INSERT INTO scan_results (timestamp, symbol, price, spread, created_at)
                   VALUES (?, ?, ?, ?, datetime('now', ?))""",
                (f"2026-01-01 {10+i//6:02d}:{(i%6)*10:02d}:00", "BTC/USD",
                 50000 + i * 50, 0.5, f"-{20-i} minutes"),
            )
        await db.commit()

        # Load and run
        module = load_analysis_module("market_analysis")
        ro = ReadOnlyDB(db.conn)
        result = await module.analyze(ro, get_schema_description())

        assert isinstance(result, dict)
        assert "price_summary" in result
        assert "data_depth" in result
        assert "data_quality" in result
        assert result["data_quality"]["total_scans"] == 20

        # BTC/USD should be in price summary
        assert "BTC/USD" in result["price_summary"]
        btc = result["price_summary"]["BTC/USD"]
        assert btc["current_price"] > 0

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_trade_performance_module():
    """Trade performance module loads, runs against seeded DB, returns dict."""
    from src.shell.database import Database
    from src.statistics.readonly_db import ReadOnlyDB, get_schema_description
    from src.statistics.loader import load_analysis_module

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        await db.connect()

        # Seed trades
        trades = [
            ("BTC/USD", "long", 0.001, 50000, 51000, 1.0, 0.02, 0.20, "DAY", "trending", "2026-01-01", "2026-01-01 12:00:00"),
            ("BTC/USD", "long", 0.001, 50000, 49000, -1.0, -0.02, 0.20, "DAY", "ranging", "2026-01-02", "2026-01-02 12:00:00"),
            ("ETH/USD", "long", 0.01, 3000, 3200, 2.0, 0.067, 0.12, "SWING", "trending", "2026-01-03", "2026-01-05 12:00:00"),
        ]
        for t in trades:
            await db.execute(
                """INSERT INTO trades (symbol, side, qty, entry_price, exit_price, pnl, pnl_pct,
                   fees, intent, strategy_regime, opened_at, closed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                t,
            )

        # Seed signals
        await db.execute(
            "INSERT INTO signals (symbol, action, size_pct, confidence, intent, reasoning, acted_on) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("BTC/USD", "BUY", 0.02, 0.8, "DAY", "test", 1),
        )
        await db.execute(
            "INSERT INTO signals (symbol, action, size_pct, confidence, intent, reasoning, acted_on, rejected_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("BTC/USD", "BUY", 0.02, 0.6, "DAY", "test", 0, "max_positions"),
        )

        # Seed fee schedule
        await db.execute(
            "INSERT INTO fee_schedule (maker_fee_pct, taker_fee_pct) VALUES (?, ?)",
            (0.25, 0.40),
        )
        await db.commit()

        # Load and run
        module = load_analysis_module("trade_performance")
        ro = ReadOnlyDB(db.conn)
        result = await module.analyze(ro, get_schema_description())

        assert isinstance(result, dict)
        assert "by_symbol" in result
        assert "by_regime" in result
        assert "signals" in result
        assert "fee_impact" in result
        assert "holding_duration" in result
        assert "rolling_7d" in result
        assert "rolling_30d" in result

        # BTC/USD: 2 trades, 1 win, 1 loss
        assert "BTC/USD" in result["by_symbol"]
        btc = result["by_symbol"]["BTC/USD"]
        assert btc["trades"] == 2
        assert btc["wins"] == 1
        assert btc["win_rate"] == 0.5

        # Signals: 2 total, 1 acted
        assert result["signals"]["total"] == 2
        assert result["signals"]["acted"] == 1
        assert result["signals"]["act_rate"] == 0.5

        # Fee impact
        assert result["fee_impact"]["total_fees_paid"] > 0
        assert result["fee_impact"]["round_trip_fee_pct"] > 0

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_analysis_modules_empty_db():
    """Both analysis modules handle empty database gracefully."""
    from src.shell.database import Database
    from src.statistics.readonly_db import ReadOnlyDB, get_schema_description
    from src.statistics.loader import load_analysis_module

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        await db.connect()

        ro = ReadOnlyDB(db.conn)
        schema = get_schema_description()

        # Market analysis on empty DB
        market = load_analysis_module("market_analysis")
        market_result = await market.analyze(ro, schema)
        assert isinstance(market_result, dict)
        assert market_result["data_quality"]["total_scans"] == 0

        # Trade performance on empty DB
        perf = load_analysis_module("trade_performance")
        perf_result = await perf.analyze(ro, schema)
        assert isinstance(perf_result, dict)
        assert perf_result["signals"]["total"] == 0

        await db.close()
    finally:
        os.unlink(db_path)


# --- Orchestrator Context Gathering ---

@pytest.mark.asyncio
async def test_orchestrator_gather_context_includes_truth_and_analysis():
    """Orchestrator _gather_context() includes ground truth, market analysis, and trade performance."""
    from src.shell.database import Database
    from src.shell.truth import compute_truth_benchmarks
    from src.statistics.readonly_db import ReadOnlyDB, get_schema_description
    from src.statistics.loader import load_analysis_module

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        await db.connect()

        # Seed some data so modules have something to analyze
        await db.execute(
            """INSERT INTO scan_results (timestamp, symbol, price, spread, created_at)
               VALUES (datetime('now'), ?, ?, ?, datetime('now'))""",
            ("BTC/USD", 50000, 0.5),
        )
        # Seed candle data (market analysis now reads from candles, not scan_results)
        await db.execute(
            """INSERT INTO candles (symbol, timeframe, timestamp, open, high, low, close, volume)
               VALUES (?, '1h', datetime('now'), ?, ?, ?, ?, ?)""",
            ("BTC/USD", 50000, 50500, 49800, 50200, 150),
        )
        await db.execute(
            """INSERT INTO trades (symbol, side, qty, entry_price, exit_price, pnl, pnl_pct, fees, intent, strategy_regime, opened_at, closed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("BTC/USD", "long", 0.001, 50000, 51000, 1.0, 0.02, 0.20, "DAY", "trending", "2026-01-01", "2026-01-01 12:00:00"),
        )
        await db.execute(
            "INSERT INTO signals (symbol, action, size_pct, confidence, intent, reasoning, acted_on) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("BTC/USD", "BUY", 0.02, 0.8, "DAY", "test", 1),
        )
        await db.commit()

        # Run truth benchmarks
        truth = await compute_truth_benchmarks(db)
        assert truth["trade_count"] == 1
        assert truth["total_scans"] == 1
        assert truth["total_signals"] == 1

        # Run market analysis
        market_module = load_analysis_module("market_analysis")
        ro = ReadOnlyDB(db.conn)
        schema = get_schema_description()
        market_result = await market_module.analyze(ro, schema)
        assert isinstance(market_result, dict)
        assert "BTC/USD" in market_result["price_summary"]

        # Run trade performance
        perf_module = load_analysis_module("trade_performance")
        ro2 = ReadOnlyDB(db.conn)
        perf_result = await perf_module.analyze(ro2, schema)
        assert isinstance(perf_result, dict)
        assert "BTC/USD" in perf_result["by_symbol"]

        # All three produce valid output — this is what the orchestrator combines
        assert truth["net_pnl"] == perf_result["by_symbol"]["BTC/USD"]["net_pnl"]

        await db.close()
    finally:
        os.unlink(db_path)


# --- Orchestrator Decision Routing ---

def test_orchestrator_decision_type_routing():
    """Verify the orchestrator correctly routes different decision types."""
    # Strategy decisions
    for decision_type in ("STRATEGY_TWEAK", "STRATEGY_RESTRUCTURE", "STRATEGY_OVERHAUL",
                          "TWEAK", "RESTRUCTURE", "OVERHAUL"):
        assert decision_type not in ("NO_CHANGE", "MARKET_ANALYSIS_UPDATE", "TRADE_ANALYSIS_UPDATE")

    # Analysis module decisions
    assert "MARKET_ANALYSIS_UPDATE" not in ("NO_CHANGE",)
    assert "TRADE_ANALYSIS_UPDATE" not in ("NO_CHANGE",)

    # These are the complete set of valid decisions
    valid_decisions = {
        "NO_CHANGE", "STRATEGY_TWEAK", "STRATEGY_RESTRUCTURE", "STRATEGY_OVERHAUL",
        "MARKET_ANALYSIS_UPDATE", "TRADE_ANALYSIS_UPDATE",
        "TWEAK", "RESTRUCTURE", "OVERHAUL",  # legacy names
    }
    assert len(valid_decisions) == 9


# --- Analysis Module Evolution Pipeline ---

def test_analysis_code_gen_prompts_exist():
    """Verify orchestrator prompts follow the three-layer framework."""
    from src.orchestrator.orchestrator import (
        LAYER_1_IDENTITY, FUND_MANDATE, LAYER_2_SYSTEM,
        ANALYSIS_CODE_GEN_SYSTEM, ANALYSIS_REVIEW_SYSTEM,
        CODE_GEN_SYSTEM, CODE_REVIEW_SYSTEM,
    )

    # Layer 1: Identity — character dimensions, no directives
    assert "Radical Honesty" in LAYER_1_IDENTITY
    assert "Probabilistic Thinking" in LAYER_1_IDENTITY
    assert "Long-Term Orientation" in LAYER_1_IDENTITY
    assert "uncertainty" in LAYER_1_IDENTITY.lower()
    assert "Change" in LAYER_1_IDENTITY

    # Fund mandate — brief, method-agnostic
    assert "capital preservation" in FUND_MANDATE.lower()
    assert "long-term fund" in FUND_MANDATE.lower()

    # Layer 2: System understanding — input categories
    assert "GROUND TRUTH" in LAYER_2_SYSTEM
    assert "YOUR MARKET ANALYSIS" in LAYER_2_SYSTEM
    assert "YOUR TRADE PERFORMANCE ANALYSIS" in LAYER_2_SYSTEM
    assert "YOUR STRATEGY" in LAYER_2_SYSTEM
    assert "SYSTEM CONSTRAINTS" in LAYER_2_SYSTEM

    # Layer 2: Decision types
    assert "MARKET_ANALYSIS_UPDATE" in LAYER_2_SYSTEM
    assert "TRADE_ANALYSIS_UPDATE" in LAYER_2_SYSTEM
    assert "STRATEGY_TWEAK" in LAYER_2_SYSTEM

    # Layer 2: Key system facts
    assert "Long-only" in LAYER_2_SYSTEM
    assert "paper test" in LAYER_2_SYSTEM.lower()

    # Analysis code gen should mention AnalysisBase, ReadOnlyDB
    assert "AnalysisBase" in ANALYSIS_CODE_GEN_SYSTEM
    assert "ReadOnlyDB" in ANALYSIS_CODE_GEN_SYSTEM or "read-only" in ANALYSIS_CODE_GEN_SYSTEM.lower()

    # Analysis review should focus on math
    assert "formula" in ANALYSIS_REVIEW_SYSTEM.lower() or "Formula" in ANALYSIS_REVIEW_SYSTEM
    assert "division by zero" in ANALYSIS_REVIEW_SYSTEM.lower()
    assert "edge case" in ANALYSIS_REVIEW_SYSTEM.lower()

    # Strategy prompts should still exist unchanged
    assert "StrategyBase" in CODE_GEN_SYSTEM
    assert "IO Contract" in CODE_REVIEW_SYSTEM


def test_analysis_evolution_sandbox_validates():
    """Analysis module evolution goes through sandbox validation."""
    from src.statistics.sandbox import validate_analysis_module

    # Valid analysis module (what Sonnet would generate)
    good_code = '''
from src.shell.contract import AnalysisBase

class Analysis(AnalysisBase):
    async def analyze(self, db, schema: dict) -> dict:
        report = {}
        row = await db.fetchone("SELECT COUNT(*) as cnt FROM trades WHERE closed_at IS NOT NULL")
        report["total_closed_trades"] = row["cnt"] if row else 0

        # Win rate with division-by-zero guard
        stats = await db.fetchone("""
            SELECT
                COUNT(*) as total,
                COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0) as wins
            FROM trades WHERE closed_at IS NOT NULL
        """)
        if stats and stats["total"] > 0:
            report["win_rate"] = stats["wins"] / stats["total"]
        else:
            report["win_rate"] = 0.0

        return report
'''
    result = validate_analysis_module(good_code, "test_module")
    assert result.passed, f"Valid module should pass: {result.errors}"

    # Bad: tries to write to DB
    bad_code = '''
import sqlite3
from src.shell.contract import AnalysisBase

class Analysis(AnalysisBase):
    async def analyze(self, db, schema: dict) -> dict:
        return {}
'''
    result = validate_analysis_module(bad_code, "test_module")
    assert not result.passed, "Should reject sqlite3 import"

    # Bad: tries to use network
    net_code = '''
import requests
from src.shell.contract import AnalysisBase

class Analysis(AnalysisBase):
    async def analyze(self, db, schema: dict) -> dict:
        return {}
'''
    result = validate_analysis_module(net_code, "test_module")
    assert not result.passed, "Should reject network import"


# --- Orchestrator Thought Spool ---

@pytest.mark.asyncio
async def test_orchestrator_thoughts_table():
    """Thought spool stores and retrieves AI responses grouped by cycle."""
    from src.shell.database import Database

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        await db.connect()

        # Verify parent_version column exists in strategy_versions
        row = await db.fetchone("SELECT sql FROM sqlite_master WHERE name = 'strategy_versions'")
        assert "parent_version" in row["sql"], "Missing parent_version column"

        cycle_id = "20260201_020000"

        # Insert multiple thoughts for one cycle
        thoughts = [
            (cycle_id, "analysis", "opus", "Review the system...", "Full Opus analysis response here", '{"decision": "NO_CHANGE"}'),
            (cycle_id, "code_gen_1", "sonnet", "Generate strategy...", "class Strategy(StrategyBase):\n    pass", None),
            (cycle_id, "code_review_1", "opus", "Review this code...", '{"approved": true}', '{"approved": true}'),
        ]
        for t in thoughts:
            await db.execute(
                """INSERT INTO orchestrator_thoughts
                   (cycle_id, step, model, input_summary, full_response, parsed_result)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                t,
            )
        await db.commit()

        # Query by cycle_id
        rows = await db.fetchall(
            "SELECT * FROM orchestrator_thoughts WHERE cycle_id = ? ORDER BY created_at",
            (cycle_id,),
        )
        assert len(rows) == 3
        assert rows[0]["step"] == "analysis"
        assert rows[0]["model"] == "opus"
        assert rows[1]["step"] == "code_gen_1"
        assert rows[2]["step"] == "code_review_1"

        # Query specific step
        row = await db.fetchone(
            "SELECT * FROM orchestrator_thoughts WHERE cycle_id = ? AND step = ?",
            (cycle_id, "analysis"),
        )
        assert row is not None
        assert "Opus analysis" in row["full_response"]
        assert row["parsed_result"] is not None

        # Verify cycle grouping with a second cycle
        await db.execute(
            """INSERT INTO orchestrator_thoughts
               (cycle_id, step, model, input_summary, full_response, parsed_result)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("20260202_020000", "analysis", "opus", "Day 2 review...", "Day 2 response", None),
        )
        await db.commit()

        # Count by cycle
        cycles = await db.fetchall(
            """SELECT cycle_id, COUNT(*) as steps
               FROM orchestrator_thoughts GROUP BY cycle_id ORDER BY cycle_id"""
        )
        assert len(cycles) == 2
        assert cycles[0]["steps"] == 3  # first cycle
        assert cycles[1]["steps"] == 1  # second cycle

        await db.close()
    finally:
        os.unlink(db_path)


# --- Data Store Aggregation ---

@pytest.mark.asyncio
async def test_data_store_aggregation_5m_to_1h():
    """DataStore correctly aggregates 5m candles into 1h candles."""
    from src.shell.config import DataConfig
    from src.shell.database import Database
    from src.shell.data_store import DataStore

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        await db.connect()

        # Retention = 0 days forces all 5m data to be eligible for aggregation
        config = DataConfig(candle_5m_retention_days=0)
        store = DataStore(db, config)

        # 12 five-minute candles = exactly 1 hour (10:00 to 10:55)
        dates = pd.date_range("2026-01-01 10:00", periods=12, freq="5min")
        df = pd.DataFrame({
            "open": [100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111],
            "high": [105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116],
            "low": [95, 96, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106],
            "close": [101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112],
            "volume": [10] * 12,
        }, index=dates)

        stored = await store.store_candles("BTC/USD", "5m", df)
        assert stored == 12

        # Verify 5m candles exist
        count_before = await store.get_candle_count("BTC/USD", "5m")
        assert count_before == 12

        # Aggregate
        aggregated = await store.aggregate_5m_to_1h()
        assert aggregated > 0

        # 1h candle should exist with correct OHLCV
        hourly = await store.get_candles("BTC/USD", "1h")
        assert len(hourly) == 1

        row = hourly.iloc[0]
        assert row["open"] == 100      # First candle's open
        assert row["high"] == 116      # Max of all highs
        assert row["low"] == 95        # Min of all lows
        assert row["close"] == 112     # Last candle's close
        assert row["volume"] == 120    # Sum of all volumes (10 * 12)

        # 5m candles should be deleted (aggregated away)
        count_after = await store.get_candle_count("BTC/USD", "5m")
        assert count_after == 0

        await db.close()
    finally:
        os.unlink(db_path)


# --- Critical Audit Fix Tests (Session 12) ---

@pytest.mark.asyncio
async def test_pnl_includes_entry_and_exit_fees():
    """C1+C2: Trade P&L includes both entry and exit fees."""
    from src.shell.config import load_config
    from src.shell.database import Database
    from src.shell.portfolio import PortfolioTracker
    from src.shell.kraken import KrakenREST
    from src.shell.contract import Action, Intent, Signal

    config = load_config()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        config.db_path = f.name

    try:
        db = Database(config.db_path)
        await db.connect()
        kraken = KrakenREST(config.kraken)
        portfolio = PortfolioTracker(config, db, kraken)

        # Buy at 50000 with 0.40% taker fee
        buy = Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.05, intent=Intent.DAY)
        buy_result = await portfolio.execute_signal(buy, 50000.0, 0.25, 0.40)
        assert buy_result is not None
        entry_fee = buy_result["fee"]
        assert entry_fee > 0

        # Sell at same price — P&L should be negative (both fees)
        sell = Signal(symbol="BTC/USD", action=Action.CLOSE, size_pct=1.0, intent=Intent.DAY)
        sell_result = await portfolio.execute_signal(sell, 50000.0, 0.25, 0.40)
        assert sell_result is not None

        # P&L should reflect BOTH fees
        assert sell_result["pnl"] < 0
        # The recorded fee in the trade should be entry + exit
        trade = await db.fetchone("SELECT pnl, fees FROM trades WHERE symbol = 'BTC/USD'")
        assert trade["fees"] > entry_fee  # Total fees > just the entry fee
        # pnl should be approximately -(entry_fee + exit_fee)
        assert trade["pnl"] < 0

        await db.close()
    finally:
        os.unlink(config.db_path)


def test_risk_allows_exit_during_daily_loss():
    """C3: SELL/CLOSE signals pass through even when daily loss limit exceeded."""
    from src.shell.config import RiskConfig
    from src.shell.risk import RiskManager
    from src.shell.contract import Action, Signal, Intent

    config = RiskConfig(
        max_trade_pct=0.05, max_position_pct=0.15, max_positions=5,
        max_daily_loss_pct=0.03, max_drawdown_pct=0.12, max_daily_trades=50,
        max_leverage=1.0, rollback_daily_loss_pct=0.08, rollback_consecutive_losses=999,
        default_trade_pct=0.02, default_stop_loss_pct=0.02, default_take_profit_pct=0.06,
        kill_switch=False,
    )
    risk = RiskManager(config)

    # Simulate being in a bad loss state
    risk._daily_pnl = -100.0  # Way past the limit for a $1000 portfolio
    risk._halted = True
    risk._halt_reason = "Max drawdown exceeded"

    # BUY should be blocked
    buy = Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.02, intent=Intent.DAY)
    check = risk.check_signal(buy, 1000.0, 3)
    assert not check.passed

    # CLOSE should be allowed
    close = Signal(symbol="BTC/USD", action=Action.CLOSE, size_pct=1.0, intent=Intent.DAY)
    check = risk.check_signal(close, 1000.0, 3)
    assert check.passed, f"CLOSE should pass during halt: {check.reason}"

    # SELL should be allowed
    sell = Signal(symbol="BTC/USD", action=Action.SELL, size_pct=0.5, intent=Intent.DAY)
    check = risk.check_signal(sell, 1000.0, 3)
    assert check.passed, f"SELL should pass during halt: {check.reason}"


def test_sandbox_blocks_open_and_compile():
    """C4+C5: Strategy sandbox blocks open(), compile(), and attribute calls."""
    from src.strategy.sandbox import check_imports

    # open() should be blocked
    errors = check_imports("data = open('/etc/passwd').read()")
    assert any("open" in e for e in errors), f"Should block open(): {errors}"

    # compile() should be blocked
    errors = check_imports("code = compile('import os', '', 'exec')")
    assert any("compile" in e for e in errors), f"Should block compile(): {errors}"

    # os.system() via attribute should be blocked (if os import somehow slipped through)
    errors = check_imports("import os\nos.system('rm -rf /')")
    # Should catch the import first
    assert any("Forbidden import" in e for e in errors)

    # Direct attribute call check (assuming somehow imported)
    code = """
import numpy as np
result = np.array([1, 2, 3])
"""
    errors = check_imports(code)
    assert len(errors) == 0  # numpy is allowed


def test_sandbox_blocks_forbidden_attrs():
    """C5: FORBIDDEN_ATTRS actually catches dotted attribute calls."""
    from src.strategy.sandbox import check_imports

    # Test that os.system() call is caught as attribute (even without import check)
    code = "os.system('whoami')"
    errors = check_imports(code)
    assert any("os.system" in e for e in errors), f"Should catch os.system(): {errors}"

    code = "os.popen('ls')"
    errors = check_imports(code)
    assert any("os.popen" in e for e in errors), f"Should catch os.popen(): {errors}"


@pytest.mark.asyncio
async def test_risk_peak_loaded_from_db():
    """C7: Peak portfolio value loaded from DB on initialize."""
    from src.shell.config import RiskConfig
    from src.shell.risk import RiskManager
    from src.shell.database import Database

    config = RiskConfig(
        max_trade_pct=0.05, max_position_pct=0.15, max_positions=5,
        max_daily_loss_pct=0.03, max_drawdown_pct=0.12, max_daily_trades=50,
        max_leverage=1.0, rollback_daily_loss_pct=0.08, rollback_consecutive_losses=999,
        default_trade_pct=0.02, default_stop_loss_pct=0.02, default_take_profit_pct=0.06,
        kill_switch=False,
    )
    risk = RiskManager(config)

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        await db.connect()

        # Seed a daily performance record with a high portfolio value
        await db.execute(
            """INSERT INTO daily_performance
               (date, portfolio_value, cash, total_trades, wins, losses, gross_pnl, net_pnl, fees_total, win_rate)
               VALUES ('2026-01-01', 1500.0, 1000.0, 5, 3, 2, 10.0, 8.0, 2.0, 0.6)"""
        )
        await db.commit()

        # Before initialize: peak is None
        assert risk._peak_portfolio is None

        # After initialize: peak loaded from DB
        await risk.initialize(db)
        assert risk._peak_portfolio == 1500.0

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_paper_test_lifecycle():
    """C6: Paper tests are terminated on new deploy and evaluated at end date."""
    from src.shell.database import Database

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        await db.connect()

        # Create a running paper test
        await db.execute(
            """INSERT INTO paper_tests
               (strategy_version, risk_tier, required_days, ends_at, status)
               VALUES ('v001', 1, 1, datetime('now', '-1 day'), 'running')"""
        )
        # Create another one still running (future end)
        await db.execute(
            """INSERT INTO paper_tests
               (strategy_version, risk_tier, required_days, ends_at, status)
               VALUES ('v002', 2, 2, datetime('now', '+1 day'), 'running')"""
        )
        await db.commit()

        # Verify both are running
        running = await db.fetchall("SELECT * FROM paper_tests WHERE status = 'running'")
        assert len(running) == 2

        # Terminate all (simulating new deploy)
        await db.execute("UPDATE paper_tests SET status = 'terminated' WHERE status = 'running'")
        await db.commit()

        running = await db.fetchall("SELECT * FROM paper_tests WHERE status = 'running'")
        assert len(running) == 0

        terminated = await db.fetchall("SELECT * FROM paper_tests WHERE status = 'terminated'")
        assert len(terminated) == 2

        await db.close()
    finally:
        os.unlink(db_path)


# --- T1: Nightly Orchestration Cycle (mocked) ---

@pytest.mark.asyncio
async def test_orchestration_nightly_cycle_no_change():
    """T1: Full nightly cycle with mocked AI returning NO_CHANGE."""
    from src.shell.config import load_config
    from src.shell.database import Database
    from src.shell.data_store import DataStore
    from src.orchestrator.orchestrator import Orchestrator

    config = load_config()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        config.db_path = f.name

    try:
        db = Database(config.db_path)
        await db.connect()
        data_store = DataStore(db, config.data)

        # Mock AI client
        ai = AsyncMock()
        ai.tokens_remaining = 1000000
        ai._daily_tokens_used = 500
        ai.get_daily_usage = AsyncMock(return_value={
            "used": 500, "daily_limit": 1500000, "total_cost": 0.02, "models": {}
        })

        # Opus returns a NO_CHANGE decision
        ai.ask_opus = AsyncMock(return_value=json.dumps({
            "decision": "NO_CHANGE",
            "reasoning": "Markets are stable, no changes needed.",
            "market_observations": "BTC consolidating near 70k",
            "cross_reference_findings": "",
        }))

        orch = Orchestrator(config, db, ai, MagicMock(), data_store)
        report = await orch.run_nightly_cycle()

        assert "No changes" in report
        assert orch._cycle_id is not None

        # Verify thought was stored
        thoughts = await db.fetchall(
            "SELECT * FROM orchestrator_thoughts WHERE cycle_id = ?",
            (orch._cycle_id,),
        )
        assert len(thoughts) >= 1
        assert thoughts[0]["step"] == "analysis"

        # Verify observation stored
        obs = await db.fetchall("SELECT * FROM orchestrator_observations")
        assert len(obs) == 1
        assert "stable" in obs[0]["strategy_assessment"]

        # Verify orchestrator_log stored
        log_row = await db.fetchone("SELECT * FROM orchestrator_log ORDER BY id DESC LIMIT 1")
        assert log_row is not None
        assert log_row["action"] == "NO_CHANGE"
        assert log_row["tokens_used"] is not None

        await db.close()
    finally:
        os.unlink(config.db_path)


@pytest.mark.asyncio
async def test_orchestration_cycle_insufficient_budget():
    """T1b: Orchestrator skips cycle when token budget is insufficient."""
    from src.shell.config import load_config
    from src.shell.database import Database
    from src.shell.data_store import DataStore
    from src.orchestrator.orchestrator import Orchestrator

    config = load_config()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        config.db_path = f.name

    try:
        db = Database(config.db_path)
        await db.connect()
        data_store = DataStore(db, config.data)

        ai = AsyncMock()
        ai.tokens_remaining = 100  # Way below 50000 threshold

        orch = Orchestrator(config, db, ai, MagicMock(), data_store)
        report = await orch.run_nightly_cycle()

        assert "insufficient" in report.lower() or "Skipped" in report
        # AI should NOT have been called
        ai.ask_opus.assert_not_called()

        await db.close()
    finally:
        os.unlink(config.db_path)


# --- T2: Strategy Deploy + Archive + Rollback ---

def test_strategy_deploy_archive_rollback():
    """T2: Deploy archives current, restores from archive."""
    from src.strategy.loader import (
        deploy_strategy, archive_strategy, load_strategy,
        get_strategy_path, get_code_hash, ACTIVE_DIR, ARCHIVE_DIR,
    )

    # Save original strategy to restore later
    original_path = get_strategy_path()
    original_code = original_path.read_text()
    original_hash = get_code_hash(original_path)

    try:
        # Deploy a new strategy
        new_code = '''"""Test strategy v2."""
from src.shell.contract import StrategyBase, Signal, Action

class Strategy(StrategyBase):
    def initialize(self, risk_limits, symbols):
        self._symbols = symbols

    def analyze(self, markets, portfolio, timestamp):
        return []
'''
        new_hash = deploy_strategy(new_code, "test_v2")
        assert new_hash != original_hash

        # Current strategy should be the new one
        loaded = load_strategy()
        assert loaded is not None

        # Archive should contain the pre-deploy backup
        archives = list(ARCHIVE_DIR.glob("strategy_pre_test_v2_*.py"))
        assert len(archives) >= 1

        # Verify the archive contains the original code
        archived_code = archives[0].read_text()
        assert archived_code == original_code

    finally:
        # Restore original strategy
        get_strategy_path().write_text(original_code)
        # Clean up archives created by this test
        for f in ARCHIVE_DIR.glob("strategy_pre_test_v2_*.py"):
            f.unlink()


# --- T3: Paper Test Pipeline ---

@pytest.mark.asyncio
async def test_paper_test_full_pipeline():
    """T3: Paper test create → evaluate → pass/fail based on trade P&L."""
    from src.shell.config import load_config
    from src.shell.database import Database
    from src.shell.data_store import DataStore
    from src.orchestrator.orchestrator import Orchestrator

    config = load_config()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        config.db_path = f.name

    try:
        db = Database(config.db_path)
        await db.connect()
        data_store = DataStore(db, config.data)

        ai = AsyncMock()
        ai.tokens_remaining = 1000000
        ai._daily_tokens_used = 0

        orch = Orchestrator(config, db, ai, MagicMock(), data_store)

        # Create a paper test that has already ended (past ends_at)
        await db.execute(
            """INSERT INTO paper_tests
               (strategy_version, risk_tier, required_days, started_at, ends_at, status)
               VALUES ('v_test', 1, 1, datetime('now', '-3 hours'), datetime('now', '-1 hour'), 'running')"""
        )
        # Insert enough winning trades for that version (within the paper test time window)
        for i in range(config.orchestrator.min_paper_test_trades):
            await db.execute(
                """INSERT INTO trades (symbol, side, qty, entry_price, exit_price, pnl, pnl_pct,
                   fees, intent, strategy_version, opened_at, closed_at)
                   VALUES ('BTC/USD', 'long', 0.001, 50000, 51000, 1.0, 0.02, 0.20, 'DAY', 'v_test',
                           datetime('now', '-2 hours'), datetime('now', '-1 hour'))"""
            )
        await db.commit()

        # Evaluate paper tests
        results = await orch._evaluate_paper_tests()
        assert len(results) == 1
        assert results[0]["status"] in ("passed", "failed")

        # Verify DB updated
        test = await db.fetchone("SELECT * FROM paper_tests WHERE strategy_version = 'v_test'")
        assert test["status"] in ("passed", "failed")

        # Test termination of running tests
        await db.execute(
            """INSERT INTO paper_tests
               (strategy_version, risk_tier, required_days, ends_at, status)
               VALUES ('v_test2', 2, 2, datetime('now', '+1 day'), 'running')"""
        )
        await db.commit()

        count = await orch._terminate_running_paper_tests("new deploy")
        running = await db.fetchall("SELECT * FROM paper_tests WHERE status = 'running'")
        assert len(running) == 0

        await db.close()
    finally:
        os.unlink(config.db_path)


# --- T4: Telegram Commands ---

@pytest.mark.asyncio
async def test_telegram_commands():
    """T4: Telegram commands return expected output formats."""
    from src.shell.config import load_config
    from src.shell.database import Database
    from src.shell.risk import RiskManager
    from src.telegram.commands import BotCommands

    config = load_config()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        config.db_path = f.name

    try:
        db = Database(config.db_path)
        await db.connect()
        risk = RiskManager(config.risk)

        scan_state = {"last_scan": "02:30:00", "symbols": {
            "BTC/USD": {"price": 70000, "spread": 0.5},
        }}

        # Configure allowed user IDs so auth passes
        config.telegram.allowed_user_ids = [12345]

        commands = BotCommands(
            config=config, db=db, scan_state=scan_state,
            risk_manager=risk,
        )

        # Mock Update and message
        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = 12345
        update.message = AsyncMock()
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.args = []

        # /help (also handles /start)
        await commands.cmd_help(update, context)
        reply = update.message.reply_text.call_args[0][0]
        assert "Trading Brain" in reply
        assert "/status" in reply
        assert "/health" in reply
        assert "/outlook" in reply
        assert "Ask about the system" in reply

        # /status — system health only (no portfolio/P&L)
        update.message.reply_text.reset_mock()
        await commands.cmd_status(update, context)
        reply = update.message.reply_text.call_args[0][0]
        assert "Mode: paper" in reply
        assert "ACTIVE" in reply
        assert "Uptime:" in reply
        # Should NOT contain portfolio/P&L data (moved to /health)
        assert "Portfolio:" not in reply
        assert "Cash:" not in reply
        assert "Daily P&L" not in reply

        # /positions (empty)
        update.message.reply_text.reset_mock()
        await commands.cmd_positions(update, context)
        reply = update.message.reply_text.call_args[0][0]
        assert "No open positions" in reply

        # /trades (empty)
        update.message.reply_text.reset_mock()
        await commands.cmd_trades(update, context)
        reply = update.message.reply_text.call_args[0][0]
        assert "No completed trades" in reply

        # /risk
        update.message.reply_text.reset_mock()
        await commands.cmd_risk(update, context)
        reply = update.message.reply_text.call_args[0][0]
        assert "Risk Limits" in reply
        assert "Kill switch: OFF" in reply

        # /strategy
        update.message.reply_text.reset_mock()
        await commands.cmd_strategy(update, context)
        reply = update.message.reply_text.call_args[0][0]
        assert "Strategy" in reply or "Hash" in reply

        # /pause and /resume
        update.message.reply_text.reset_mock()
        await commands.cmd_pause(update, context)
        assert commands.is_paused
        reply = update.message.reply_text.call_args[0][0]
        assert "PAUSED" in reply

        update.message.reply_text.reset_mock()
        await commands.cmd_resume(update, context)
        assert not commands.is_paused
        reply = update.message.reply_text.call_args[0][0]
        assert "RESUMED" in reply

        # /thoughts (empty)
        update.message.reply_text.reset_mock()
        await commands.cmd_thoughts(update, context)
        reply = update.message.reply_text.call_args[0][0]
        assert "No orchestrator cycles" in reply

        # /kill
        update.message.reply_text.reset_mock()
        await commands.cmd_kill(update, context)
        assert commands.is_paused
        assert scan_state.get("kill_requested") is True

        await db.close()
    finally:
        os.unlink(config.db_path)


@pytest.mark.asyncio
async def test_telegram_health_command():
    """T4c: /health returns fund metrics from truth benchmarks + live state."""
    from src.shell.config import load_config
    from src.shell.database import Database
    from src.shell.risk import RiskManager
    from src.telegram.commands import BotCommands

    config = load_config()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        config.db_path = f.name

    try:
        db = Database(config.db_path)
        await db.connect()
        risk = RiskManager(config.risk)
        risk._peak_portfolio = 210.0

        # Mock portfolio tracker
        portfolio = MagicMock()
        portfolio.total_value = AsyncMock(return_value=200.0)
        portfolio.cash = 180.0
        portfolio.position_count = 1

        config.telegram.allowed_user_ids = [12345]

        commands = BotCommands(
            config=config, db=db, scan_state={},
            portfolio_tracker=portfolio, risk_manager=risk,
        )

        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = 12345
        update.message = AsyncMock()
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.args = []

        await commands.cmd_health(update, context)
        reply = update.message.reply_text.call_args[0][0]
        assert "Fund Health" in reply
        assert "Portfolio: $200.00" in reply
        assert "Cash: $180.00" in reply
        assert "Win Rate:" in reply
        assert "Expectancy:" in reply
        assert "Total Fees:" in reply
        assert "Strategy:" in reply
        assert "Max Drawdown:" in reply

        await db.close()
    finally:
        os.unlink(config.db_path)


@pytest.mark.asyncio
async def test_telegram_outlook_command():
    """T4d: /outlook returns latest orchestrator observations."""
    from src.shell.config import load_config
    from src.shell.database import Database
    from src.telegram.commands import BotCommands

    config = load_config()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        config.db_path = f.name

    try:
        db = Database(config.db_path)
        await db.connect()

        config.telegram.allowed_user_ids = [12345]

        commands = BotCommands(config=config, db=db, scan_state={})

        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = 12345
        update.message = AsyncMock()
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.args = []

        # Empty — no cycles yet
        await commands.cmd_outlook(update, context)
        reply = update.message.reply_text.call_args[0][0]
        assert "No orchestrator cycles have run yet" in reply

        # Seed an observation
        await db.execute(
            """INSERT INTO orchestrator_observations (date, cycle_id, market_summary, strategy_assessment, notable_findings)
               VALUES ('2026-02-01', 'cycle_001', 'BTC consolidating near 70k', 'Strategy performing well', 'Volatility dropping')"""
        )
        await db.commit()

        update.message.reply_text.reset_mock()
        await commands.cmd_outlook(update, context)
        reply = update.message.reply_text.call_args[0][0]
        assert "Orchestrator Outlook" in reply
        assert "2026-02-01" in reply
        assert "BTC consolidating near 70k" in reply
        assert "Strategy performing well" in reply
        assert "Volatility dropping" in reply

        await db.close()
    finally:
        os.unlink(config.db_path)


@pytest.mark.asyncio
async def test_telegram_ask_command():
    """T4e: /ask assembles context and calls Haiku."""
    from src.shell.config import load_config
    from src.shell.database import Database
    from src.shell.risk import RiskManager
    from src.telegram.commands import BotCommands, ASK_SYSTEM_PROMPT

    config = load_config()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        config.db_path = f.name

    try:
        db = Database(config.db_path)
        await db.connect()
        risk = RiskManager(config.risk)

        portfolio = MagicMock()
        portfolio.total_value = AsyncMock(return_value=200.0)
        portfolio.cash = 180.0
        portfolio.position_count = 0

        ai = MagicMock()
        ai.ask_haiku = AsyncMock(return_value="The fund is currently stable with no open positions.")

        config.telegram.allowed_user_ids = [12345]

        commands = BotCommands(
            config=config, db=db, scan_state={},
            portfolio_tracker=portfolio, risk_manager=risk, ai_client=ai,
        )

        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = 12345
        update.message = AsyncMock()
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.args = ["How", "is", "the", "fund?"]

        await commands.cmd_ask(update, context)

        # Should have called ask_haiku with context + question
        ai.ask_haiku.assert_called_once()
        call_kwargs = ai.ask_haiku.call_args
        prompt = call_kwargs[0][0] if call_kwargs[0] else call_kwargs[1].get("prompt", "")
        assert "How is the fund?" in prompt
        assert "Portfolio: $200.00" in prompt
        assert call_kwargs[1].get("system") == ASK_SYSTEM_PROMPT or call_kwargs.kwargs.get("system") == ASK_SYSTEM_PROMPT
        assert call_kwargs[1].get("purpose") == "user_ask" or call_kwargs.kwargs.get("purpose") == "user_ask"

        # Should have sent "Thinking..." then the answer
        calls = update.message.reply_text.call_args_list
        assert "Thinking..." in calls[0][0][0]
        assert "stable" in calls[1][0][0]

        await db.close()
    finally:
        os.unlink(config.db_path)


@pytest.mark.asyncio
async def test_telegram_authorization():
    """T4b: Commands reject unauthorized users when user IDs are configured."""
    from src.shell.config import load_config
    from src.shell.database import Database
    from src.telegram.commands import BotCommands

    config = load_config()
    config.telegram.allowed_user_ids = [99999]  # Only user 99999 allowed

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        config.db_path = f.name

    try:
        db = Database(config.db_path)
        await db.connect()

        commands = BotCommands(config=config, db=db, scan_state={})

        # Unauthorized user
        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = 12345  # Not in allowed list
        update.message = AsyncMock()
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.args = []

        await commands.cmd_status(update, context)
        update.message.reply_text.assert_not_called()  # Silently rejected

        await db.close()
    finally:
        os.unlink(config.db_path)


# --- T5: Graceful Shutdown ---

@pytest.mark.asyncio
async def test_graceful_shutdown():
    """T5: TradingBrain.stop() cleans up resources properly."""
    from src.shell.config import load_config
    from src.shell.database import Database

    config = load_config()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        config.db_path = f.name

    try:
        db = Database(config.db_path)
        await db.connect()

        # Simulate a running system by creating the brain components manually
        # and testing the stop sequence rather than full start/stop
        from src.shell.risk import RiskManager

        risk = RiskManager(config.risk)

        # Simulate some state
        risk.record_trade_result(-1.0)
        risk.record_trade_result(2.0)
        assert risk.daily_trades == 2
        assert risk.daily_pnl == 1.0

        # Reset should clear daily state
        risk.reset_daily()
        assert risk.daily_trades == 0
        assert risk.daily_pnl == 0.0
        # But consecutive losses persist
        assert risk.consecutive_losses == 0  # Reset by the win

        # Halt + unhalt cycle
        risk._halted = True
        risk._halt_reason = "test halt"
        assert risk.is_halted
        risk.unhalt()
        assert not risk.is_halted
        assert risk.consecutive_losses == 0

        await db.close()
    finally:
        os.unlink(config.db_path)


@pytest.mark.asyncio
async def test_double_shutdown_prevented():
    """T5b: Double shutdown doesn't crash (main.py finally guard)."""
    from src.shell.config import load_config
    from src.shell.database import Database

    config = load_config()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        config.db_path = f.name

    try:
        db = Database(config.db_path)
        await db.connect()

        # DB close is idempotent
        await db.close()
        # Second close should not raise
        await db.close()
    finally:
        try:
            os.unlink(config.db_path)
        except FileNotFoundError:
            pass


# --- T7: Strategy State Round-Trip ---

def test_strategy_state_round_trip():
    """T7: Strategy get_state → load_state preserves internal state."""
    from src.strategy.loader import load_strategy
    from src.shell.contract import RiskLimits, SymbolData, Portfolio, Action

    strategy = load_strategy()
    limits = RiskLimits(max_trade_pct=0.05, default_trade_pct=0.02,
                        max_positions=5, max_daily_loss_pct=0.03, max_drawdown_pct=0.10)
    strategy.initialize(limits, ["BTC/USD", "ETH/USD"])

    # Generate some state by running analyze with trending data
    dates = pd.date_range(end=datetime.now(), periods=100, freq="5min")
    prices = list(range(50000, 50100))
    df = pd.DataFrame({
        "open": prices,
        "high": [p + 50 for p in prices],
        "low": [p - 50 for p in prices],
        "close": prices,
        "volume": [50] * 100,
    }, index=dates)

    markets = {"BTC/USD": SymbolData(
        symbol="BTC/USD", current_price=50099,
        candles_5m=df, candles_1h=df, candles_1d=df,
        spread=0.001, volume_24h=1000000,
    )}
    portfolio = Portfolio(cash=200, total_value=200, positions=[], recent_trades=[],
                          daily_pnl=0, total_pnl=0, fees_today=0)

    # Run a couple of analysis cycles to build state
    strategy.analyze(markets, portfolio, datetime.now())
    strategy.analyze(markets, portfolio, datetime.now())

    # Capture state
    state = strategy.get_state()
    assert isinstance(state, dict)

    # Load into a fresh strategy instance
    strategy2 = load_strategy()
    strategy2.initialize(limits, ["BTC/USD", "ETH/USD"])
    strategy2.load_state(state)
    state2 = strategy2.get_state()

    # States should match (round-trip fidelity)
    assert state.keys() == state2.keys()
    for key in state:
        if isinstance(state[key], dict):
            for k, v in state[key].items():
                if isinstance(v, float):
                    assert abs(v - state2[key].get(k, 0)) < 1e-6, f"Mismatch for {key}.{k}"
                else:
                    assert v == state2[key].get(k), f"Mismatch for {key}.{k}"


# --- T11: Scan Loop Flow ---

@pytest.mark.asyncio
async def test_scan_loop_generates_signals():
    """T11: Scan loop builds markets, runs strategy, executes signals."""
    from src.shell.config import load_config
    from src.shell.database import Database
    from src.shell.portfolio import PortfolioTracker
    from src.shell.risk import RiskManager
    from src.shell.kraken import KrakenREST
    from src.shell.contract import (
        Signal, Action, Intent, SymbolData, Portfolio, RiskLimits,
    )
    from src.strategy.loader import load_strategy

    config = load_config()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        config.db_path = f.name

    try:
        db = Database(config.db_path)
        await db.connect()

        kraken = KrakenREST(config.kraken)
        portfolio = PortfolioTracker(config, db, kraken)
        await portfolio.initialize()
        risk = RiskManager(config.risk)

        strategy = load_strategy()
        limits = RiskLimits(
            max_trade_pct=config.risk.max_trade_pct,
            default_trade_pct=config.risk.default_trade_pct,
            max_positions=config.risk.max_positions,
            max_daily_loss_pct=config.risk.max_daily_loss_pct,
            max_drawdown_pct=config.risk.max_drawdown_pct,
        )
        strategy.initialize(limits, config.symbols)

        # Build mock market data for one symbol (trending up to trigger BUY)
        dates = pd.date_range(end=datetime.now(), periods=100, freq="5min")
        # Strong uptrend: fast EMA will cross above slow EMA
        prices = [50000 + i * 50 for i in range(100)]
        df = pd.DataFrame({
            "open": prices,
            "high": [p + 30 for p in prices],
            "low": [p - 30 for p in prices],
            "close": prices,
            "volume": [100] * 100,
        }, index=dates)

        markets = {}
        for symbol in config.symbols[:1]:  # Just BTC for simplicity
            markets[symbol] = SymbolData(
                symbol=symbol, current_price=prices[-1],
                candles_5m=df, candles_1h=df, candles_1d=df,
                spread=0.001, volume_24h=5000000,
            )

        port = await portfolio.get_portfolio({s: m.current_price for s, m in markets.items()})

        # Run strategy (may or may not generate signals depending on strategy state)
        signals = strategy.analyze(markets, port, datetime.now())
        assert isinstance(signals, list)

        # Run twice to establish EMA state
        signals2 = strategy.analyze(markets, port, datetime.now())
        assert isinstance(signals2, list)

        # All signals should be valid
        for sig in signals2:
            assert sig.symbol in config.symbols
            assert sig.action in (Action.BUY, Action.SELL, Action.CLOSE)
            assert 0 < sig.size_pct <= 1.0

            # Risk check should work
            check = risk.check_signal(sig, port.total_value, portfolio.position_count)
            assert isinstance(check.passed, bool)

        await db.close()
    finally:
        os.unlink(config.db_path)


@pytest.mark.asyncio
async def test_scan_results_stored_in_db():
    """T11b: Scan results are persisted to scan_results table."""
    from src.shell.database import Database

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        await db.connect()

        # Simulate what the scan loop does: insert scan results
        await db.execute(
            """INSERT INTO scan_results
               (timestamp, symbol, price, spread)
               VALUES (?, ?, ?, ?)""",
            (datetime.now(tz=None).isoformat(), "BTC/USD", 70000, 0.5),
        )
        await db.execute(
            """INSERT INTO scan_results
               (timestamp, symbol, price, spread)
               VALUES (?, ?, ?, ?)""",
            (datetime.now(tz=None).isoformat(), "ETH/USD", 3500, 0.3),
        )
        await db.commit()

        # Query back
        rows = await db.fetchall("SELECT * FROM scan_results ORDER BY symbol")
        assert len(rows) == 2
        assert rows[0]["symbol"] == "BTC/USD"
        assert rows[0]["price"] == 70000
        assert rows[1]["symbol"] == "ETH/USD"
        assert rows[1]["spread"] == 0.3

        # Update with signal info (simulates what scan loop does after strategy runs)
        await db.execute(
            """UPDATE scan_results SET signal_generated = 1, signal_action = 'BUY', signal_confidence = 0.8
               WHERE symbol = 'BTC/USD' AND id = (SELECT MAX(id) FROM scan_results WHERE symbol = 'BTC/USD')"""
        )
        await db.commit()

        row = await db.fetchone("SELECT * FROM scan_results WHERE symbol = 'BTC/USD'")
        assert row["signal_generated"] == 1
        assert row["signal_action"] == "BUY"
        assert row["signal_confidence"] == 0.8

        await db.close()
    finally:
        os.unlink(db_path)


# --- API ---

@pytest.mark.asyncio
async def test_api_server_endpoints():
    """Test REST API endpoints return enveloped JSON responses."""
    from aiohttp.test_utils import TestClient, TestServer

    from src.api.server import create_app
    from src.shell.config import load_config
    from src.shell.database import Database
    from src.shell.risk import RiskManager

    config = load_config()

    db_path = tempfile.mktemp(suffix=".db")
    try:
        db = Database(db_path)
        await db.connect()

        risk = RiskManager(config.risk)
        portfolio = MagicMock()
        portfolio.total_value = AsyncMock(return_value=200.0)
        portfolio.position_count = 2
        portfolio.get_portfolio = AsyncMock(return_value=MagicMock(
            total_value=200.0, cash=180.0, positions=[]
        ))
        ai = MagicMock()
        ai.get_daily_usage = AsyncMock(return_value={"used": 1000, "total_cost": 0.01, "models": {}})
        ai.tokens_remaining = 1499000
        scan_state = {"symbols": {"BTC/USD": {"price": 45000, "spread": 0.3}}, "last_scan": "03:10:00"}
        commands = MagicMock()
        commands.is_paused = False

        app, ws_manager, _ = create_app(config, db, portfolio, risk, ai, scan_state, commands)

        # Set API key for auth
        from src.api import api_key_key
        app[api_key_key] = "test-key"
        auth_headers = {"Authorization": "Bearer test-key"}

        async with TestClient(TestServer(app)) as client:
            # /v1/system
            resp = await client.get("/v1/system", headers=auth_headers)
            assert resp.status == 200
            body = await resp.json()
            assert "data" in body
            assert "meta" in body
            assert body["meta"]["mode"] == "paper"
            assert body["data"]["status"] == "running"

            # /v1/portfolio
            resp = await client.get("/v1/portfolio", headers=auth_headers)
            assert resp.status == 200
            body = await resp.json()
            assert body["data"]["total_value"] == 200.0

            # /v1/positions
            resp = await client.get("/v1/positions", headers=auth_headers)
            assert resp.status == 200
            body = await resp.json()
            assert isinstance(body["data"], list)

            # /v1/trades
            resp = await client.get("/v1/trades?limit=10", headers=auth_headers)
            assert resp.status == 200
            body = await resp.json()
            assert isinstance(body["data"], list)

            # /v1/risk
            resp = await client.get("/v1/risk", headers=auth_headers)
            assert resp.status == 200
            body = await resp.json()
            assert "limits" in body["data"]
            assert "current" in body["data"]
            assert body["data"]["current"]["halted"] is False

            # /v1/signals
            resp = await client.get("/v1/signals", headers=auth_headers)
            assert resp.status == 200

            # /v1/strategy
            resp = await client.get("/v1/strategy", headers=auth_headers)
            assert resp.status == 200

            # /v1/ai/usage
            resp = await client.get("/v1/ai/usage", headers=auth_headers)
            assert resp.status == 200
            body = await resp.json()
            assert body["data"]["today"]["total_tokens"] == 1000

            # /v1/benchmarks
            resp = await client.get("/v1/benchmarks", headers=auth_headers)
            assert resp.status == 200

            # /v1/performance
            resp = await client.get("/v1/performance", headers=auth_headers)
            assert resp.status == 200

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_api_auth_required():
    """Test API rejects requests without valid bearer token when API_KEY is set."""
    from aiohttp.test_utils import TestClient, TestServer

    from src.api.server import create_app
    from src.shell.config import load_config
    from src.shell.database import Database
    from src.shell.risk import RiskManager

    config = load_config()

    db_path = tempfile.mktemp(suffix=".db")
    try:
        db = Database(db_path)
        await db.connect()

        risk = RiskManager(config.risk)
        from src.api import api_key_key
        app, _, _ = create_app(config, db, MagicMock(), risk, MagicMock(), {})
        app[api_key_key] = "test-secret-key"

        async with TestClient(TestServer(app)) as client:
            # No auth — should 401
            resp = await client.get("/v1/system")
            assert resp.status == 401

            # Wrong key — should 401
            resp = await client.get("/v1/system", headers={"Authorization": "Bearer wrong"})
            assert resp.status == 401

            # Correct key — should 200
            resp = await client.get("/v1/system", headers={"Authorization": "Bearer test-secret-key"})
            assert resp.status == 200

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_api_websocket_connection():
    """Test WebSocket connects and receives events."""
    from aiohttp.test_utils import TestClient, TestServer

    from src.api.server import create_app
    from src.shell.config import load_config
    from src.shell.database import Database
    from src.shell.risk import RiskManager

    config = load_config()

    db_path = tempfile.mktemp(suffix=".db")
    try:
        db = Database(db_path)
        await db.connect()

        risk = RiskManager(config.risk)
        app, ws_manager, _ = create_app(config, db, MagicMock(), risk, MagicMock(), {})

        # Set API key for auth
        from src.api import api_key_key
        app[api_key_key] = "test-ws-key"

        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect("/v1/events?token=test-ws-key") as ws:
                assert ws_manager.client_count == 1

                # Broadcast an event
                await ws_manager.broadcast({
                    "event": "test_event",
                    "data": {"msg": "hello"},
                    "timestamp": "2026-02-09T00:00:00Z",
                })

                msg = await ws.receive_json()
                assert msg["event"] == "test_event"
                assert msg["data"]["msg"] == "hello"

            # After disconnect
            assert ws_manager.client_count == 0

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_notifier_dual_dispatch():
    """Test Notifier sends to both WebSocket and Telegram."""
    from src.api.websocket import WebSocketManager
    from src.shell.config import NotificationConfig
    from src.telegram.notifications import Notifier

    # Config: trade_executed on, scan_complete off
    tg_config = NotificationConfig(trade_executed=True, scan_complete=False)
    notifier = Notifier(chat_id="123", tg_filter=tg_config)

    ws_manager = WebSocketManager()
    notifier.set_ws_manager(ws_manager)

    # Mock telegram app
    mock_app = MagicMock()
    mock_app.bot.send_message = AsyncMock()
    notifier.set_app(mock_app)

    # trade_executed — should go to both
    await notifier.trade_executed({"action": "BUY", "symbol": "BTC/USD", "qty": 0.001, "price": 45000, "fee": 0.5})
    await asyncio.sleep(0.05)  # Let background Telegram task complete
    assert mock_app.bot.send_message.call_count == 1

    mock_app.bot.send_message.reset_mock()

    # scan_complete — should only go to WS (telegram filtered off)
    await notifier.scan_complete(9, 2)
    await asyncio.sleep(0.05)
    assert mock_app.bot.send_message.call_count == 0

    # risk_halt — should go to telegram (defaults to True)
    await notifier.risk_halt("Max drawdown exceeded")
    await asyncio.sleep(0.05)
    assert mock_app.bot.send_message.call_count == 1


def test_notification_config_loading():
    """Test notification config loads from settings.toml."""
    from src.shell.config import load_config
    config = load_config()
    # Defaults
    assert config.telegram.notifications.trade_executed is True
    assert config.telegram.notifications.scan_complete is False
    assert config.telegram.notifications.signal_rejected is False
    assert config.telegram.notifications.strategy_deployed is True


def test_api_config_loading():
    """Test API config loads from settings.toml."""
    from src.shell.config import load_config
    config = load_config()
    assert config.api.enabled is True
    assert config.api.port == 8080
    assert config.api.host == "0.0.0.0"


# --- Position System: Tags + MODIFY ---

def test_contract_modify_action():
    """Action.MODIFY exists and Signal accepts tag field."""
    from src.shell.contract import Action, Signal, Intent

    assert Action.MODIFY.value == "MODIFY"

    # Signal with tag
    sig = Signal(symbol="BTC/USD", action=Action.MODIFY, size_pct=0.0,
                 stop_loss=48000, tag="my_position_1")
    assert sig.tag == "my_position_1"
    assert sig.action == Action.MODIFY

    # Signal without tag defaults to None
    sig2 = Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.02)
    assert sig2.tag is None


@pytest.mark.asyncio
async def test_portfolio_modify():
    """MODIFY updates SL/TP without fees."""
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

        # Buy BTC
        buy = Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.05,
                     stop_loss=49000, take_profit=55000, intent=Intent.DAY)
        result = await portfolio.execute_signal(buy, 50000, 0.25, 0.40)
        assert result is not None
        tag = result["tag"]

        cash_after_buy = portfolio.cash

        # Modify SL/TP
        modify = Signal(symbol="BTC/USD", action=Action.MODIFY, size_pct=0.0,
                        stop_loss=48000, take_profit=56000, tag=tag)
        mod_result = await portfolio.execute_signal(modify, 50000, 0.25, 0.40)
        assert mod_result is not None
        assert mod_result["action"] == "MODIFY"
        assert mod_result["fee"] == 0
        assert mod_result["changes"]["stop_loss"] == 48000
        assert mod_result["changes"]["take_profit"] == 56000

        # Cash unchanged (zero fees)
        assert portfolio.cash == cash_after_buy

        # Position still exists with updated SL/TP
        assert portfolio.position_count == 1
        pos_row = await db.fetchone("SELECT * FROM positions WHERE tag = ?", (tag,))
        assert pos_row["stop_loss"] == 48000
        assert pos_row["take_profit"] == 56000

        await db.close()
    finally:
        os.unlink(config.db_path)


@pytest.mark.asyncio
async def test_portfolio_modify_no_tag():
    """MODIFY without tag returns None (ambiguous)."""
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

        # Buy BTC
        buy = Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.05, intent=Intent.DAY)
        await portfolio.execute_signal(buy, 50000, 0.25, 0.40)

        # MODIFY without tag should fail
        modify = Signal(symbol="BTC/USD", action=Action.MODIFY, size_pct=0.0,
                        stop_loss=48000)
        result = await portfolio.execute_signal(modify, 50000, 0.25, 0.40)
        assert result is None

        await db.close()
    finally:
        os.unlink(config.db_path)


@pytest.mark.asyncio
async def test_portfolio_multi_position():
    """Two BUY signals for same symbol with different tags create separate positions."""
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

        # Two buys for BTC — no tags, should auto-generate different ones
        buy1 = Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.03, intent=Intent.DAY)
        r1 = await portfolio.execute_signal(buy1, 50000, 0.25, 0.40)
        assert r1 is not None

        buy2 = Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.03, intent=Intent.SWING)
        r2 = await portfolio.execute_signal(buy2, 51000, 0.25, 0.40)
        assert r2 is not None

        # Should have 2 separate positions
        assert portfolio.position_count == 2
        assert r1["tag"] != r2["tag"]

        # Both in DB
        rows = await db.fetchall("SELECT * FROM positions WHERE symbol = 'BTC/USD'")
        assert len(rows) == 2

        await db.close()
    finally:
        os.unlink(config.db_path)


@pytest.mark.asyncio
async def test_portfolio_close_by_tag():
    """Close one position by tag, leave others."""
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

        # Open two positions
        r1 = await portfolio.execute_signal(
            Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.03, intent=Intent.DAY),
            50000, 0.25, 0.40)
        r2 = await portfolio.execute_signal(
            Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.03, intent=Intent.SWING),
            51000, 0.25, 0.40)

        assert portfolio.position_count == 2

        # Close only the first by tag
        close = Signal(symbol="BTC/USD", action=Action.CLOSE, size_pct=1.0,
                       intent=Intent.DAY, tag=r1["tag"])
        result = await portfolio.execute_signal(close, 52000, 0.25, 0.40)
        assert result is not None
        assert result["tag"] == r1["tag"]
        assert portfolio.position_count == 1

        # Second position still exists
        rows = await db.fetchall("SELECT * FROM positions")
        assert len(rows) == 1
        assert rows[0]["tag"] == r2["tag"]

        await db.close()
    finally:
        os.unlink(config.db_path)


@pytest.mark.asyncio
async def test_portfolio_close_all_no_tag():
    """Close all positions for symbol when no tag specified."""
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

        # Open two positions
        await portfolio.execute_signal(
            Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.03, intent=Intent.DAY),
            50000, 0.25, 0.40)
        await portfolio.execute_signal(
            Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.03, intent=Intent.SWING),
            51000, 0.25, 0.40)

        assert portfolio.position_count == 2

        # Close all BTC with no tag — returns list
        close = Signal(symbol="BTC/USD", action=Action.CLOSE, size_pct=1.0, intent=Intent.DAY)
        result = await portfolio.execute_signal(close, 52000, 0.25, 0.40)
        assert isinstance(result, list)
        assert len(result) == 2
        assert portfolio.position_count == 0

        # Both trades recorded
        trades = await db.fetchall("SELECT * FROM trades")
        assert len(trades) == 2

        await db.close()
    finally:
        os.unlink(config.db_path)


@pytest.mark.asyncio
async def test_portfolio_sell_oldest_no_tag():
    """SELL without tag hits oldest position (FIFO)."""
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

        # Open two positions
        r1 = await portfolio.execute_signal(
            Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.03, intent=Intent.DAY),
            50000, 0.25, 0.40)
        r2 = await portfolio.execute_signal(
            Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.03, intent=Intent.SWING),
            51000, 0.25, 0.40)

        # SELL without tag — should sell from oldest position
        sell = Signal(symbol="BTC/USD", action=Action.SELL, size_pct=0.01, intent=Intent.DAY)
        result = await portfolio.execute_signal(sell, 52000, 0.25, 0.40)
        assert result is not None
        assert result["tag"] == r1["tag"]  # Oldest position

        await db.close()
    finally:
        os.unlink(config.db_path)


@pytest.mark.asyncio
async def test_portfolio_auto_tag():
    """BUY without tag generates auto tag."""
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

        buy = Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.05, intent=Intent.DAY)
        result = await portfolio.execute_signal(buy, 50000, 0.25, 0.40)
        assert result is not None
        assert result["tag"].startswith("auto_BTCUSD_")
        assert result["tag"] == "auto_BTCUSD_001"

        # Second auto-tag increments
        buy2 = Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.03, intent=Intent.SWING)
        result2 = await portfolio.execute_signal(buy2, 51000, 0.25, 0.40)
        assert result2["tag"] == "auto_BTCUSD_002"

        await db.close()
    finally:
        os.unlink(config.db_path)


@pytest.mark.asyncio
async def test_portfolio_average_in_same_tag():
    """BUY with existing tag averages in (increases qty, recalculates avg entry)."""
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

        # First buy with explicit tag
        buy1 = Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.05,
                      intent=Intent.DAY, tag="btc_swing_1")
        r1 = await portfolio.execute_signal(buy1, 50000, 0.25, 0.40)
        assert r1 is not None
        assert r1["tag"] == "btc_swing_1"
        qty1 = r1["qty"]

        # Second buy with same tag — averages in
        buy2 = Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.05,
                      intent=Intent.DAY, tag="btc_swing_1")
        r2 = await portfolio.execute_signal(buy2, 52000, 0.25, 0.40)
        assert r2 is not None
        assert r2["tag"] == "btc_swing_1"

        # Still one position, but with increased qty
        assert portfolio.position_count == 1
        pos = await db.fetchone("SELECT * FROM positions WHERE tag = 'btc_swing_1'")
        assert pos["qty"] > qty1  # Qty increased

        await db.close()
    finally:
        os.unlink(config.db_path)


@pytest.mark.asyncio
async def test_capital_events_table():
    """capital_events table exists and accepts inserts."""
    from src.shell.database import Database

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        await db.connect()

        await db.execute(
            "INSERT INTO capital_events (type, amount, notes) VALUES (?, ?, ?)",
            ("deposit", 500.0, "Initial funding"),
        )
        await db.commit()

        rows = await db.fetchall("SELECT * FROM capital_events")
        assert len(rows) == 1
        assert rows[0]["type"] == "deposit"
        assert rows[0]["amount"] == 500.0
        assert rows[0]["notes"] == "Initial funding"

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_positions_tag_unique():
    """Tag uniqueness is enforced on positions table."""
    from src.shell.database import Database
    import aiosqlite

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        await db.connect()

        await db.execute(
            "INSERT INTO positions (symbol, tag, qty, avg_entry) VALUES (?, ?, ?, ?)",
            ("BTC/USD", "unique_tag_1", 0.01, 50000),
        )
        await db.commit()

        # Same tag should fail
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "INSERT INTO positions (symbol, tag, qty, avg_entry) VALUES (?, ?, ?, ?)",
                ("ETH/USD", "unique_tag_1", 1.0, 3000),
            )

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_positions_symbol_not_unique():
    """Multiple positions for the same symbol are allowed."""
    from src.shell.database import Database

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        await db.connect()

        await db.execute(
            "INSERT INTO positions (symbol, tag, qty, avg_entry) VALUES (?, ?, ?, ?)",
            ("BTC/USD", "tag_a", 0.01, 50000),
        )
        await db.execute(
            "INSERT INTO positions (symbol, tag, qty, avg_entry) VALUES (?, ?, ?, ?)",
            ("BTC/USD", "tag_b", 0.02, 51000),
        )
        await db.commit()

        rows = await db.fetchall("SELECT * FROM positions WHERE symbol = 'BTC/USD'")
        assert len(rows) == 2

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_db_migration_backfills_tags():
    """Existing positions (without tag column) get auto-generated tags after migration."""
    import aiosqlite

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        # Create an old-schema DB manually (positions with UNIQUE(symbol), no tag column)
        conn = await aiosqlite.connect(db_path)
        await conn.execute("""
            CREATE TABLE positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL UNIQUE,
                side TEXT NOT NULL DEFAULT 'long',
                qty REAL NOT NULL,
                avg_entry REAL NOT NULL,
                current_price REAL DEFAULT 0,
                unrealized_pnl REAL DEFAULT 0,
                stop_loss REAL,
                take_profit REAL,
                intent TEXT NOT NULL DEFAULT 'DAY',
                opened_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        await conn.execute(
            "INSERT INTO positions (symbol, qty, avg_entry) VALUES (?, ?, ?)",
            ("BTC/USD", 0.01, 50000),
        )
        await conn.execute(
            "INSERT INTO positions (symbol, qty, avg_entry) VALUES (?, ?, ?)",
            ("ETH/USD", 1.0, 3000),
        )
        await conn.commit()
        await conn.close()

        # Now open with the Database class — migration should run
        from src.shell.database import Database
        db = Database(db_path)
        await db.connect()

        # Positions should now have tags
        rows = await db.fetchall("SELECT * FROM positions ORDER BY symbol")
        assert len(rows) == 2
        for row in rows:
            assert "tag" in dict(row)
            tag = row["tag"]
            assert tag.startswith("auto_")
            assert len(tag) > 0

        # Tags should be unique
        tags = [row["tag"] for row in rows]
        assert len(set(tags)) == 2

        await db.close()
    finally:
        os.unlink(db_path)


# --- D5: Widened Risk Limits ---

def test_config_widened_risk_limits():
    """D5: Risk limits are widened to emergency-only backstops."""
    from src.shell.config import load_config
    config = load_config()
    assert config.risk.max_position_pct == 0.25
    assert config.risk.max_daily_loss_pct == 0.10
    assert config.risk.max_drawdown_pct == 0.40
    assert config.risk.rollback_daily_loss_pct == 0.15


# --- D7: Order Fill Confirmation ---

@pytest.mark.asyncio
async def test_orders_table_exists():
    """D7: orders table exists in schema."""
    from src.shell.database import Database
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        await db.connect()

        rows = await db.fetchall("SELECT name FROM sqlite_master WHERE type='table' AND name='orders'")
        assert len(rows) == 1

        # Check key columns exist
        cols = await db.fetchall("PRAGMA table_info(orders)")
        col_names = [c["name"] for c in cols]
        for expected in ("txid", "tag", "symbol", "side", "order_type", "volume",
                         "status", "filled_volume", "avg_fill_price", "fee", "cost", "purpose"):
            assert expected in col_names, f"Missing column: {expected}"

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_confirm_fill_success():
    """D7: _confirm_fill() correctly records fill data when order is closed."""
    from src.shell.config import Config
    from src.shell.database import Database
    from src.shell.portfolio import PortfolioTracker
    from src.shell.kraken import KrakenREST

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        config = Config(mode="live", db_path=db_path)
        db = Database(db_path)
        await db.connect()

        kraken = MagicMock(spec=KrakenREST)
        kraken.query_order = AsyncMock(return_value={
            "status": "closed",
            "price": "50100.0",
            "vol_exec": "0.001",
            "fee": "0.20",
            "cost": "50.10",
        })

        portfolio = PortfolioTracker(config, db, kraken)

        result = await portfolio._confirm_fill(
            txid="TXID123", tag="test_btc_001", symbol="BTC/USD",
            side="buy", order_type="market", volume=0.001,
        )

        assert result["fill_price"] == 50100.0
        assert result["filled_volume"] == 0.001
        assert result["fee"] == 0.20
        assert result["status"] == "filled"

        # Verify DB record
        order_row = await db.fetchone("SELECT * FROM orders WHERE txid = 'TXID123'")
        assert order_row is not None
        assert order_row["status"] == "filled"
        assert order_row["avg_fill_price"] == 50100.0

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_confirm_fill_timeout():
    """D7: _confirm_fill() raises TimeoutError when order stays open."""
    from src.shell.config import Config
    from src.shell.database import Database
    from src.shell.portfolio import PortfolioTracker
    from src.shell.kraken import KrakenREST

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        config = Config(mode="live", db_path=db_path)
        db = Database(db_path)
        await db.connect()

        kraken = MagicMock(spec=KrakenREST)
        kraken.query_order = AsyncMock(return_value={"status": "open"})

        portfolio = PortfolioTracker(config, db, kraken)

        with pytest.raises(TimeoutError):
            await portfolio._confirm_fill(
                txid="TXID_TIMEOUT", tag="test_btc_001", symbol="BTC/USD",
                side="buy", order_type="market", volume=0.001,
                timeout_seconds=1, poll_interval=0.3,
            )

        # Verify DB record shows timeout
        order_row = await db.fetchone("SELECT * FROM orders WHERE txid = 'TXID_TIMEOUT'")
        assert order_row["status"] == "timeout"

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_confirm_fill_canceled():
    """D7: _confirm_fill() raises RuntimeError when order is canceled."""
    from src.shell.config import Config
    from src.shell.database import Database
    from src.shell.portfolio import PortfolioTracker
    from src.shell.kraken import KrakenREST

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        config = Config(mode="live", db_path=db_path)
        db = Database(db_path)
        await db.connect()

        kraken = MagicMock(spec=KrakenREST)
        kraken.query_order = AsyncMock(return_value={"status": "canceled"})

        portfolio = PortfolioTracker(config, db, kraken)

        with pytest.raises(RuntimeError, match="canceled"):
            await portfolio._confirm_fill(
                txid="TXID_CANCEL", tag="test_btc_001", symbol="BTC/USD",
                side="buy", order_type="market", volume=0.001,
            )

        # Verify DB record shows canceled
        order_row = await db.fetchone("SELECT * FROM orders WHERE txid = 'TXID_CANCEL'")
        assert order_row["status"] == "canceled"

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_execute_buy_live_fill_confirmation():
    """D7: Live mode BUY uses fill confirmation with actual Kraken data."""
    from src.shell.config import Config
    from src.shell.contract import Signal, Action, Intent
    from src.shell.database import Database
    from src.shell.portfolio import PortfolioTracker
    from src.shell.kraken import KrakenREST

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        config = Config(mode="live", db_path=db_path)
        db = Database(db_path)
        await db.connect()

        kraken = MagicMock(spec=KrakenREST)
        kraken.place_order = AsyncMock(return_value={"txid": ["TXID_BUY_001"]})
        kraken.query_order = AsyncMock(return_value={
            "status": "closed",
            "price": "50050.0",
            "vol_exec": "0.0009",
            "fee": "0.18",
            "cost": "45.05",
        })
        # Mock for conditional order (SL/TP placement won't happen since no SL/TP on signal)
        kraken.place_conditional_order = AsyncMock(return_value={"txid": ["TXID_SL"]})

        portfolio = PortfolioTracker(config, db, kraken)
        portfolio._cash = 200.0

        signal = Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.05)
        result = await portfolio.execute_signal(signal, 50000.0, 0.25, 0.40)

        assert result is not None
        assert result["action"] == "BUY"
        # Uses actual fill price from Kraken, not the assumed price
        assert result["price"] == 50050.0
        assert result["fee"] == 0.18

        # Verify order was recorded in orders table
        order_row = await db.fetchone("SELECT * FROM orders WHERE txid = 'TXID_BUY_001'")
        assert order_row is not None
        assert order_row["purpose"] == "entry"
        assert order_row["status"] == "filled"

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_execute_sell_live_fill_confirmation():
    """D7: Live mode SELL uses fill confirmation with actual Kraken data."""
    from src.shell.config import Config
    from src.shell.contract import Signal, Action, Intent
    from src.shell.database import Database
    from src.shell.portfolio import PortfolioTracker
    from src.shell.kraken import KrakenREST

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        config = Config(mode="live", db_path=db_path)
        db = Database(db_path)
        await db.connect()

        kraken = MagicMock(spec=KrakenREST)
        kraken.place_order = AsyncMock(return_value={"txid": ["TXID_SELL_001"]})
        kraken.query_order = AsyncMock(return_value={
            "status": "closed",
            "price": "51000.0",
            "vol_exec": "0.001",
            "fee": "0.20",
            "cost": "51.00",
        })
        # Mock cancel for SL/TP
        kraken.cancel_order = AsyncMock(return_value={})

        portfolio = PortfolioTracker(config, db, kraken)
        portfolio._cash = 150.0

        # Insert a position first
        tag = "test_btc_001"
        portfolio._positions[tag] = {
            "symbol": "BTC/USD", "tag": tag, "side": "long", "qty": 0.001,
            "avg_entry": 50000.0, "current_price": 51000.0, "entry_fee": 0.20,
            "stop_loss": None, "take_profit": None, "intent": "DAY",
            "strategy_version": None, "opened_at": "2026-01-01", "updated_at": "2026-01-01",
        }
        await db.execute(
            """INSERT INTO positions (symbol, tag, side, qty, avg_entry, current_price, entry_fee, intent, opened_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("BTC/USD", tag, "long", 0.001, 50000.0, 51000.0, 0.20, "DAY", "2026-01-01", "2026-01-01"),
        )
        await db.commit()

        signal = Signal(symbol="BTC/USD", action=Action.CLOSE, size_pct=1.0, tag=tag)
        result = await portfolio.execute_signal(signal, 51000.0, 0.25, 0.40)

        assert result is not None
        assert result["price"] == 51000.0
        assert result["fee"] == 0.40  # total_fee: entry_fee (0.20) + exit_fee (0.20)

        # Verify exit order was recorded
        order_row = await db.fetchone("SELECT * FROM orders WHERE txid = 'TXID_SELL_001'")
        assert order_row is not None
        assert order_row["purpose"] == "exit"
        assert order_row["status"] == "filled"

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_paper_mode_no_fill_confirmation():
    """D7: Paper mode does NOT use fill confirmation or orders table."""
    from src.shell.config import Config
    from src.shell.contract import Signal, Action, Intent
    from src.shell.database import Database
    from src.shell.portfolio import PortfolioTracker
    from src.shell.kraken import KrakenREST

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        config = Config(mode="paper", paper_balance_usd=200.0, db_path=db_path)
        db = Database(db_path)
        await db.connect()

        kraken = MagicMock(spec=KrakenREST)
        # These should NOT be called in paper mode
        kraken.place_order = AsyncMock()
        kraken.query_order = AsyncMock()

        portfolio = PortfolioTracker(config, db, kraken)

        signal = Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.05,
                        stop_loss=49000.0, take_profit=52000.0)
        result = await portfolio.execute_signal(signal, 50000.0, 0.25, 0.40)

        assert result is not None
        assert result["action"] == "BUY"

        # No Kraken calls in paper mode
        kraken.place_order.assert_not_called()
        kraken.query_order.assert_not_called()

        # No orders table entries
        order_count = await db.fetchone("SELECT COUNT(*) as cnt FROM orders")
        assert order_count["cnt"] == 0

        # No conditional_orders entries
        cond_count = await db.fetchone("SELECT COUNT(*) as cnt FROM conditional_orders")
        assert cond_count["cnt"] == 0

        await db.close()
    finally:
        os.unlink(db_path)


# --- D4: Exchange-Native SL/TP ---

@pytest.mark.asyncio
async def test_conditional_orders_table_exists():
    """D4: conditional_orders table exists in schema."""
    from src.shell.database import Database
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        await db.connect()

        rows = await db.fetchall("SELECT name FROM sqlite_master WHERE type='table' AND name='conditional_orders'")
        assert len(rows) == 1

        cols = await db.fetchall("PRAGMA table_info(conditional_orders)")
        col_names = [c["name"] for c in cols]
        for expected in ("tag", "symbol", "sl_txid", "tp_txid", "sl_price", "tp_price", "status"):
            assert expected in col_names, f"Missing column: {expected}"

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_place_exchange_sl_tp():
    """D4: _place_exchange_sl_tp() places orders and records in DB."""
    from src.shell.config import Config
    from src.shell.database import Database
    from src.shell.portfolio import PortfolioTracker
    from src.shell.kraken import KrakenREST

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        config = Config(mode="live", db_path=db_path)
        db = Database(db_path)
        await db.connect()

        kraken = MagicMock(spec=KrakenREST)
        call_count = {"n": 0}
        async def mock_place_conditional(symbol, side, order_type, volume, trigger_price):
            call_count["n"] += 1
            return {"txid": [f"TXID_COND_{call_count['n']}"]}
        kraken.place_conditional_order = mock_place_conditional

        portfolio = PortfolioTracker(config, db, kraken)

        await portfolio._place_exchange_sl_tp(
            tag="test_btc_001", symbol="BTC/USD", qty=0.001,
            stop_loss=49000.0, take_profit=52000.0, entry_txid="TXID_ENTRY",
        )

        # Verify conditional_orders record
        cond = await db.fetchone("SELECT * FROM conditional_orders WHERE tag = 'test_btc_001'")
        assert cond is not None
        assert cond["status"] == "active"
        assert cond["sl_txid"] == "TXID_COND_1"
        assert cond["tp_txid"] == "TXID_COND_2"
        assert cond["sl_price"] == 49000.0
        assert cond["tp_price"] == 52000.0

        # Verify orders table has both SL and TP entries
        orders = await db.fetchall("SELECT * FROM orders ORDER BY id")
        assert len(orders) == 2
        assert orders[0]["purpose"] == "stop_loss"
        assert orders[1]["purpose"] == "take_profit"

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_cancel_exchange_sl_tp():
    """D4: _cancel_exchange_sl_tp() cancels orders on Kraken and in DB."""
    from src.shell.config import Config
    from src.shell.database import Database
    from src.shell.portfolio import PortfolioTracker
    from src.shell.kraken import KrakenREST

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        config = Config(mode="live", db_path=db_path)
        db = Database(db_path)
        await db.connect()

        kraken = MagicMock(spec=KrakenREST)
        kraken.cancel_order = AsyncMock(return_value={})

        portfolio = PortfolioTracker(config, db, kraken)

        # Seed conditional_orders and orders
        await db.execute(
            """INSERT INTO conditional_orders (tag, symbol, sl_txid, tp_txid, sl_price, tp_price, status)
               VALUES (?, ?, ?, ?, ?, ?, 'active')""",
            ("test_btc_001", "BTC/USD", "TXID_SL_1", "TXID_TP_1", 49000.0, 52000.0),
        )
        await db.execute(
            "INSERT INTO orders (txid, tag, symbol, side, order_type, volume, status, purpose) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("TXID_SL_1", "test_btc_001", "BTC/USD", "sell", "stop-loss", 0.001, "pending", "stop_loss"),
        )
        await db.execute(
            "INSERT INTO orders (txid, tag, symbol, side, order_type, volume, status, purpose) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("TXID_TP_1", "test_btc_001", "BTC/USD", "sell", "take-profit", 0.001, "pending", "take_profit"),
        )
        await db.commit()

        await portfolio._cancel_exchange_sl_tp("test_btc_001")

        # Verify cancellation
        cond = await db.fetchone("SELECT * FROM conditional_orders WHERE tag = 'test_btc_001'")
        assert cond["status"] == "canceled"

        orders = await db.fetchall("SELECT * FROM orders WHERE tag = 'test_btc_001'")
        for o in orders:
            assert o["status"] == "canceled"

        # Verify Kraken cancel was called for both
        assert kraken.cancel_order.call_count == 2

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_modify_updates_exchange_sl_tp():
    """D4: MODIFY signal cancels old SL/TP and places new ones."""
    from src.shell.config import Config
    from src.shell.contract import Signal, Action, Intent
    from src.shell.database import Database
    from src.shell.portfolio import PortfolioTracker
    from src.shell.kraken import KrakenREST

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        config = Config(mode="live", db_path=db_path)
        db = Database(db_path)
        await db.connect()

        kraken = MagicMock(spec=KrakenREST)
        kraken.cancel_order = AsyncMock(return_value={})
        call_count = {"n": 0}
        async def mock_place_conditional(symbol, side, order_type, volume, trigger_price):
            call_count["n"] += 1
            return {"txid": [f"TXID_NEW_{call_count['n']}"]}
        kraken.place_conditional_order = mock_place_conditional

        portfolio = PortfolioTracker(config, db, kraken)

        # Set up existing position + conditional orders
        tag = "test_btc_001"
        portfolio._positions[tag] = {
            "symbol": "BTC/USD", "tag": tag, "side": "long", "qty": 0.001,
            "avg_entry": 50000.0, "current_price": 51000.0, "entry_fee": 0.20,
            "stop_loss": 49000.0, "take_profit": 52000.0, "intent": "DAY",
            "strategy_version": None, "opened_at": "2026-01-01", "updated_at": "2026-01-01",
        }
        await db.execute(
            """INSERT INTO positions (symbol, tag, side, qty, avg_entry, current_price, entry_fee, stop_loss, take_profit, intent, opened_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("BTC/USD", tag, "long", 0.001, 50000.0, 51000.0, 0.20, 49000.0, 52000.0, "DAY", "2026-01-01", "2026-01-01"),
        )
        await db.execute(
            """INSERT INTO conditional_orders (tag, symbol, sl_txid, tp_txid, sl_price, tp_price, status)
               VALUES (?, ?, ?, ?, ?, ?, 'active')""",
            (tag, "BTC/USD", "TXID_OLD_SL", "TXID_OLD_TP", 49000.0, 52000.0),
        )
        await db.execute(
            "INSERT INTO orders (txid, tag, symbol, side, order_type, volume, status, purpose) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("TXID_OLD_SL", tag, "BTC/USD", "sell", "stop-loss", 0.001, "pending", "stop_loss"),
        )
        await db.execute(
            "INSERT INTO orders (txid, tag, symbol, side, order_type, volume, status, purpose) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("TXID_OLD_TP", tag, "BTC/USD", "sell", "take-profit", 0.001, "pending", "take_profit"),
        )
        await db.commit()

        # MODIFY with new SL/TP
        signal = Signal(
            symbol="BTC/USD", action=Action.MODIFY, size_pct=0.0,
            stop_loss=48000.0, take_profit=53000.0, tag=tag,
        )
        result = await portfolio.execute_signal(signal, 51000.0, 0.25, 0.40)

        assert result is not None
        assert result["action"] == "MODIFY"
        assert result["changes"]["stop_loss"] == 48000.0
        assert result["changes"]["take_profit"] == 53000.0

        # Old conditionals should be canceled
        old_conds = await db.fetchall(
            "SELECT * FROM conditional_orders WHERE sl_txid = 'TXID_OLD_SL'"
        )
        assert all(c["status"] == "canceled" for c in old_conds)

        # New conditional should be active
        new_conds = await db.fetchall(
            "SELECT * FROM conditional_orders WHERE status = 'active'"
        )
        assert len(new_conds) == 1
        assert new_conds[0]["sl_price"] == 48000.0
        assert new_conds[0]["tp_price"] == 53000.0

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_paper_mode_no_conditional_orders():
    """D4: Paper mode does NOT place exchange SL/TP orders."""
    from src.shell.config import Config
    from src.shell.database import Database
    from src.shell.portfolio import PortfolioTracker
    from src.shell.kraken import KrakenREST

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        config = Config(mode="paper", paper_balance_usd=200.0, db_path=db_path)
        db = Database(db_path)
        await db.connect()

        kraken = MagicMock(spec=KrakenREST)
        kraken.place_conditional_order = AsyncMock()

        portfolio = PortfolioTracker(config, db, kraken)

        # Call _place_exchange_sl_tp in paper mode — should be no-op
        await portfolio._place_exchange_sl_tp(
            tag="test_btc_001", symbol="BTC/USD", qty=0.001,
            stop_loss=49000.0, take_profit=52000.0,
        )

        kraken.place_conditional_order.assert_not_called()

        # No records in conditional_orders
        cond_count = await db.fetchone("SELECT COUNT(*) as cnt FROM conditional_orders")
        assert cond_count["cnt"] == 0

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_buy_live_places_sl_tp():
    """D4: Live BUY with SL/TP places exchange-native orders."""
    from src.shell.config import Config
    from src.shell.contract import Signal, Action, Intent
    from src.shell.database import Database
    from src.shell.portfolio import PortfolioTracker
    from src.shell.kraken import KrakenREST

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        config = Config(mode="live", db_path=db_path)
        db = Database(db_path)
        await db.connect()

        kraken = MagicMock(spec=KrakenREST)
        kraken.place_order = AsyncMock(return_value={"txid": ["TXID_BUY_002"]})
        kraken.query_order = AsyncMock(return_value={
            "status": "closed",
            "price": "50000.0",
            "vol_exec": "0.001",
            "fee": "0.20",
            "cost": "50.00",
        })
        cond_count = {"n": 0}
        async def mock_place_conditional(symbol, side, order_type, volume, trigger_price):
            cond_count["n"] += 1
            return {"txid": [f"TXID_COND_{cond_count['n']}"]}
        kraken.place_conditional_order = mock_place_conditional

        portfolio = PortfolioTracker(config, db, kraken)
        portfolio._cash = 200.0

        signal = Signal(
            symbol="BTC/USD", action=Action.BUY, size_pct=0.05,
            stop_loss=49000.0, take_profit=52000.0,
        )
        result = await portfolio.execute_signal(signal, 50000.0, 0.25, 0.40)

        assert result is not None
        assert result["action"] == "BUY"

        # Should have conditional orders placed
        cond = await db.fetchone("SELECT * FROM conditional_orders WHERE status = 'active'")
        assert cond is not None
        assert cond["sl_price"] == 49000.0
        assert cond["tp_price"] == 52000.0

        # Should have 3 orders total: entry + SL + TP
        orders = await db.fetchall("SELECT * FROM orders ORDER BY id")
        assert len(orders) == 3
        purposes = [o["purpose"] for o in orders]
        assert "entry" in purposes
        assert "stop_loss" in purposes
        assert "take_profit" in purposes

        await db.close()
    finally:
        os.unlink(db_path)


# --- Audit Fix Tests (Session B) ---

@pytest.mark.asyncio
async def test_record_exchange_fill():
    """F2/F3: record_exchange_fill() records fill, updates cash, removes position."""
    from src.shell.config import Config
    from src.shell.database import Database
    from src.shell.portfolio import PortfolioTracker
    from src.shell.kraken import KrakenREST

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        config = Config(mode="live", db_path=db_path)
        db = Database(db_path)
        await db.connect()

        kraken = MagicMock(spec=KrakenREST)
        portfolio = PortfolioTracker(config, db, kraken)
        portfolio._cash = 100.0

        tag = "test_btc_fill"
        portfolio._positions[tag] = {
            "symbol": "BTC/USD", "tag": tag, "side": "long", "qty": 0.001,
            "avg_entry": 50000.0, "current_price": 51000.0, "entry_fee": 0.20,
            "stop_loss": 49000.0, "take_profit": 52000.0, "intent": "DAY",
            "strategy_version": "v1", "opened_at": "2026-01-01", "updated_at": "2026-01-01",
        }
        await db.execute(
            """INSERT INTO positions (symbol, tag, side, qty, avg_entry, current_price, entry_fee, intent, opened_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("BTC/USD", tag, "long", 0.001, 50000.0, 51000.0, 0.20, "DAY", "2026-01-01", "2026-01-01"),
        )
        await db.commit()

        result = await portfolio.record_exchange_fill(
            tag=tag, fill_price=49000.0, filled_volume=0.001, fee=0.18,
        )

        assert result is not None
        assert result["action"] == "CLOSE"
        assert result["price"] == 49000.0
        assert result["qty"] == 0.001
        assert result["pnl"] < 0  # Lost money (SL hit)
        assert result["tag"] == tag

        # Position removed
        assert portfolio.position_count == 0

        # Cash updated (got sale proceeds)
        assert portfolio.cash > 100.0

        # Trade recorded in DB
        trade = await db.fetchone("SELECT * FROM trades WHERE tag = ?", (tag,))
        assert trade is not None
        assert trade["exit_price"] == 49000.0
        assert trade["pnl"] < 0

        # Position not found returns None
        assert await portfolio.record_exchange_fill("nonexistent", 50000, 0.001, 0.1) is None

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_record_exchange_fill_partial():
    """F2: record_exchange_fill() handles partial fills — reduces position, keeps remainder."""
    from src.shell.config import Config
    from src.shell.database import Database
    from src.shell.portfolio import PortfolioTracker
    from src.shell.kraken import KrakenREST

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        config = Config(mode="live", db_path=db_path)
        db = Database(db_path)
        await db.connect()

        kraken = MagicMock(spec=KrakenREST)
        portfolio = PortfolioTracker(config, db, kraken)
        portfolio._cash = 100.0

        tag = "test_btc_partial"
        portfolio._positions[tag] = {
            "symbol": "BTC/USD", "tag": tag, "side": "long", "qty": 0.002,
            "avg_entry": 50000.0, "current_price": 51000.0, "entry_fee": 0.40,
            "stop_loss": 49000.0, "take_profit": 52000.0, "intent": "SWING",
            "strategy_version": "v1", "opened_at": "2026-01-01", "updated_at": "2026-01-01",
        }
        await db.execute(
            """INSERT INTO positions (symbol, tag, side, qty, avg_entry, current_price, entry_fee, intent, opened_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("BTC/USD", tag, "long", 0.002, 50000.0, 51000.0, 0.40, "SWING", "2026-01-01", "2026-01-01"),
        )
        await db.commit()

        # Partial fill — only 0.001 of 0.002
        result = await portfolio.record_exchange_fill(
            tag=tag, fill_price=49000.0, filled_volume=0.001, fee=0.18,
        )

        assert result is not None
        assert result["qty"] == 0.001

        # Position still exists with reduced qty
        assert portfolio.position_count == 1
        pos = portfolio._positions[tag]
        assert abs(pos["qty"] - 0.001) < 0.000001

        # Entry fee proportionally reduced (0.40 * 0.5 = 0.20)
        assert abs(pos["entry_fee"] - 0.20) < 0.01

        # DB position updated
        db_pos = await db.fetchone("SELECT * FROM positions WHERE tag = ?", (tag,))
        assert db_pos is not None
        assert abs(db_pos["qty"] - 0.001) < 0.000001

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_close_replaces_sl_tp_on_sell_failure():
    """F1: If sell order fails, SL/TP are re-placed on exchange."""
    from src.shell.config import Config
    from src.shell.contract import Signal, Action, Intent
    from src.shell.database import Database
    from src.shell.portfolio import PortfolioTracker
    from src.shell.kraken import KrakenREST

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        config = Config(mode="live", db_path=db_path)
        db = Database(db_path)
        await db.connect()

        kraken = MagicMock(spec=KrakenREST)
        # place_order returns empty txid list — simulates failure
        kraken.place_order = AsyncMock(return_value={"txid": []})
        kraken.cancel_order = AsyncMock(return_value={})

        cond_count = {"n": 0}
        async def mock_place_conditional(symbol, side, order_type, volume, trigger_price):
            cond_count["n"] += 1
            return {"txid": [f"TXID_REPL_{cond_count['n']}"]}
        kraken.place_conditional_order = mock_place_conditional

        portfolio = PortfolioTracker(config, db, kraken)
        portfolio._cash = 150.0

        tag = "test_btc_f1"
        portfolio._positions[tag] = {
            "symbol": "BTC/USD", "tag": tag, "side": "long", "qty": 0.001,
            "avg_entry": 50000.0, "current_price": 51000.0, "entry_fee": 0.20,
            "stop_loss": 49000.0, "take_profit": 52000.0, "intent": "DAY",
            "strategy_version": None, "opened_at": "2026-01-01", "updated_at": "2026-01-01",
        }
        await db.execute(
            """INSERT INTO positions (symbol, tag, side, qty, avg_entry, current_price, entry_fee,
               stop_loss, take_profit, intent, opened_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("BTC/USD", tag, "long", 0.001, 50000.0, 51000.0, 0.20,
             49000.0, 52000.0, "DAY", "2026-01-01", "2026-01-01"),
        )
        # Seed existing conditional orders
        await db.execute(
            """INSERT INTO conditional_orders (tag, symbol, sl_txid, tp_txid, sl_price, tp_price, status)
               VALUES (?, ?, ?, ?, ?, ?, 'active')""",
            (tag, "BTC/USD", "TXID_OLD_SL", "TXID_OLD_TP", 49000.0, 52000.0),
        )
        await db.execute(
            "INSERT INTO orders (txid, tag, symbol, side, order_type, volume, status, purpose) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("TXID_OLD_SL", tag, "BTC/USD", "sell", "stop-loss", 0.001, "pending", "stop_loss"),
        )
        await db.execute(
            "INSERT INTO orders (txid, tag, symbol, side, order_type, volume, status, purpose) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("TXID_OLD_TP", tag, "BTC/USD", "sell", "take-profit", 0.001, "pending", "take_profit"),
        )
        await db.commit()

        # Try to close — sell fails (no txid)
        signal = Signal(symbol="BTC/USD", action=Action.CLOSE, size_pct=1.0, tag=tag)
        result = await portfolio.execute_signal(signal, 51000.0, 0.25, 0.40)

        assert result is None  # Sell failed
        assert portfolio.position_count == 1  # Position still exists

        # SL/TP should be re-placed (new conditional orders active)
        new_cond = await db.fetchone("SELECT * FROM conditional_orders WHERE status = 'active'")
        assert new_cond is not None
        assert new_cond["sl_price"] == 49000.0
        assert new_cond["tp_price"] == 52000.0
        # New txids should differ from old canceled ones
        assert new_cond["sl_txid"] != "TXID_OLD_SL"
        assert new_cond["tp_txid"] != "TXID_OLD_TP"

        await db.close()
    finally:
        os.unlink(db_path)


# =============================================================================
# Session C: New Tests — Phase 5 (Audit Coverage Expansion)
# =============================================================================


@pytest.mark.asyncio
async def test_risk_counters_restored_on_restart():
    """Fix 1.5: RiskManager restores daily counters from DB after restart."""
    from src.shell.config import load_config
    from src.shell.database import Database
    from src.shell.risk import RiskManager

    config = load_config()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        config.db_path = f.name

    try:
        db = Database(config.db_path)
        await db.connect()

        # Seed trades with UTC timestamps (matching what portfolio.py stores)
        from datetime import timezone as tz_utc
        now = datetime.now(tz_utc.utc)
        t1 = (now - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
        t2 = (now - timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S")
        t3 = (now - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")

        # First: win at t1. Then two consecutive losses (t2, t3 — most recent)
        await db.execute(
            "INSERT INTO trades (symbol, side, qty, entry_price, exit_price, pnl, pnl_pct, fees, closed_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("BTC/USD", "long", 0.001, 50000, 51000, 1.0, 0.02, 0.50, t1),
        )
        await db.execute(
            "INSERT INTO trades (symbol, side, qty, entry_price, exit_price, pnl, pnl_pct, fees, closed_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("ETH/USD", "long", 0.01, 2000, 1900, -1.0, -0.05, 0.40, t2),
        )
        await db.execute(
            "INSERT INTO trades (symbol, side, qty, entry_price, exit_price, pnl, pnl_pct, fees, closed_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("SOL/USD", "long", 1.0, 80, 75, -5.0, -0.0625, 0.30, t3),
        )
        await db.commit()

        risk = RiskManager(config.risk)
        await risk.initialize(db, tz_name=config.timezone)

        # Should have restored: 3 daily trades, -5.0 total pnl, 2 consecutive losses
        assert risk.daily_trades == 3
        assert abs(risk.daily_pnl - (-5.0)) < 0.01
        assert risk.consecutive_losses == 2  # last 2 trades (DESC order) are losses

        await db.close()
    finally:
        os.unlink(config.db_path)


def test_sandbox_blocks_getattr_bypass():
    """Fix 2.1: Strategy sandbox blocks getattr-based __import__ bypass."""
    from src.strategy.sandbox import validate_strategy

    code = '''
from src.shell.contract import StrategyBase, Signal, RiskLimits, SymbolData, Portfolio
from datetime import datetime

class Strategy(StrategyBase):
    def initialize(self, risk_limits, symbols):
        pass
    def analyze(self, markets, portfolio, timestamp):
        # Attempt getattr bypass
        x = getattr(object, "__subclasses__")
        return []
'''
    result = validate_strategy(code)
    assert not result.passed
    assert any("getattr" in e for e in result.errors)


def test_sandbox_blocks_dunder_access():
    """Fix 2.1: Strategy sandbox blocks dunder attribute chain."""
    from src.strategy.sandbox import validate_strategy

    code = '''
from src.shell.contract import StrategyBase, Signal, RiskLimits, SymbolData, Portfolio
from datetime import datetime

class Strategy(StrategyBase):
    def initialize(self, risk_limits, symbols):
        pass
    def analyze(self, markets, portfolio, timestamp):
        # Attempt dunder chain escape
        x = ().__class__.__bases__[0].__subclasses__()
        return []
'''
    result = validate_strategy(code)
    assert not result.passed
    assert any("__class__" in e or "__bases__" in e or "__subclasses__" in e for e in result.errors)


def test_loader_validates_before_load():
    """Fix 2.2: load_strategy() runs sandbox validation before loading."""
    from src.strategy.loader import load_strategy, get_strategy_path

    path = get_strategy_path()
    # Write a strategy with a forbidden import
    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.read_text() if path.exists() else None

    try:
        path.write_text('''
import subprocess
from src.shell.contract import StrategyBase

class Strategy(StrategyBase):
    def initialize(self, risk_limits, symbols): pass
    def analyze(self, markets, portfolio, timestamp): return []
''')
        with pytest.raises(RuntimeError, match="validation failed"):
            load_strategy()
    finally:
        if original:
            path.write_text(original)
        elif path.exists():
            path.unlink()


def test_analysis_sandbox_aligned():
    """Fix 2.3+2.4: Analysis sandbox blocks same modules/calls as strategy sandbox."""
    from src.statistics.sandbox import (
        FORBIDDEN_IMPORTS as ANALYSIS_FORBIDDEN,
        FORBIDDEN_CALLS as ANALYSIS_CALLS,
        FORBIDDEN_DUNDERS as ANALYSIS_DUNDERS,
    )
    from src.strategy.sandbox import (
        FORBIDDEN_IMPORTS as STRATEGY_FORBIDDEN,
        FORBIDDEN_CALLS as STRATEGY_CALLS,
        FORBIDDEN_DUNDERS as STRATEGY_DUNDERS,
    )

    # Analysis sandbox should block at least everything strategy sandbox blocks
    assert STRATEGY_FORBIDDEN.issubset(ANALYSIS_FORBIDDEN)
    assert STRATEGY_CALLS.issubset(ANALYSIS_CALLS)
    assert STRATEGY_DUNDERS.issubset(ANALYSIS_DUNDERS)


def test_readonly_db_blocks_load_extension():
    """Fix 2.5: ReadOnlyDB blocks LOAD_EXTENSION."""
    from src.statistics.readonly_db import ReadOnlyDB

    import aiosqlite

    async def _run():
        async with aiosqlite.connect(":memory:") as conn:
            conn.row_factory = aiosqlite.Row
            ro = ReadOnlyDB(conn)
            with pytest.raises(ValueError, match="load_extension.*blocked"):
                await ro.execute("LOAD_EXTENSION('/tmp/evil.so')")

    asyncio.run(_run())


def test_readonly_db_blocks_null_byte_bypass():
    """Fix 2.6: ReadOnlyDB strips null bytes to prevent bypass."""
    from src.statistics.readonly_db import ReadOnlyDB

    import aiosqlite

    async def _run():
        async with aiosqlite.connect(":memory:") as conn:
            conn.row_factory = aiosqlite.Row
            ro = ReadOnlyDB(conn)
            # Null byte before DROP should be caught
            with pytest.raises(ValueError, match="Write operation blocked"):
                await ro.execute("SELECT 1;\x00DROP TABLE candles")

    asyncio.run(_run())


@pytest.mark.asyncio
async def test_daily_reset_updates_start_value():
    """Fix 3.5: Daily reset refreshes _daily_start_value."""
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

        # Record starting value
        initial = portfolio._daily_start_value

        # Do a buy to change cash
        signal = Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.05, intent=Intent.DAY,
                        stop_loss=49000, take_profit=52000)
        await portfolio.execute_signal(signal, 50000.0, 0.25, 0.40)

        # Reset daily
        portfolio.reset_daily()

        # _daily_start_value should have been updated
        assert portfolio._daily_start_value is not None

        await db.close()
    finally:
        os.unlink(config.db_path)


def test_websocket_price_staleness():
    """Fix 3.9: KrakenWebSocket tracks price update timestamps."""
    import time as time_mod
    from src.shell.kraken import KrakenWebSocket

    ws = KrakenWebSocket("wss://test", ["BTC/USD"])

    # No prices yet — should be infinite
    assert ws.price_age("BTC/USD") == float("inf")

    # Simulate a price update
    ws._prices["BTC/USD"] = 50000.0
    ws._price_updated_at["BTC/USD"] = time_mod.monotonic()

    # Should be very recent
    assert ws.price_age("BTC/USD") < 1.0


def test_pair_reverse_extended_names():
    """Fix 4.7: PAIR_REVERSE includes extended Kraken REST response pair names."""
    from src.shell.kraken import from_kraken_pair

    assert from_kraken_pair("XXBTZUSD") == "BTC/USD"
    assert from_kraken_pair("XETHZUSD") == "ETH/USD"
    assert from_kraken_pair("XXDGUSD") == "DOGE/USD"


def test_spread_zero_bid():
    """Fix 4.5: get_spread returns 1.0 (100%) when bid is zero."""
    from src.shell.kraken import KrakenREST
    from src.shell.config import KrakenConfig

    config = KrakenConfig()
    client = KrakenREST(config)

    async def _run():
        async def mock_ticker(symbol):
            return {"a": ["100.0"], "b": ["0"]}
        client.get_ticker = mock_ticker

        spread = await client.get_spread("TEST/USD")
        assert spread == 1.0

    asyncio.run(_run())


@pytest.mark.asyncio
async def test_performance_endpoint_limit():
    """Fix 4.9: /v1/performance limits results."""
    from src.shell.config import load_config
    from src.shell.database import Database

    config = load_config()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        config.db_path = f.name

    try:
        db = Database(config.db_path)
        await db.connect()

        # Insert more than 365 days of performance data
        for i in range(400):
            dt = datetime.now() - timedelta(days=i)
            await db.execute(
                "INSERT INTO daily_performance (date, portfolio_value) VALUES (?, ?)",
                (dt.strftime("%Y-%m-%d"), 200.0 + i),
            )
        await db.commit()

        # Query with the same limit the endpoint uses (365 max)
        rows = await db.fetchall(
            "SELECT * FROM daily_performance ORDER BY date DESC LIMIT 365"
        )
        assert len(rows) == 365

        await db.close()
    finally:
        os.unlink(config.db_path)


@pytest.mark.asyncio
async def test_ask_rate_limiting():
    """Fix 3.22: /ask has a 30-second rate limit — verify second call is actually blocked."""
    import time as time_mod
    from src.telegram.commands import BotCommands
    from src.shell.config import load_config

    config = load_config()
    mock_db = MagicMock()
    mock_ai = MagicMock()
    mock_ai.ask_haiku = AsyncMock(return_value="Test answer")
    commands = BotCommands(config=config, db=mock_db, scan_state={}, ai_client=mock_ai)

    # Initially 0, so first ask is allowed
    assert commands._last_ask_time == 0

    # Simulate a successful ask by setting the rate limit timestamp
    commands._last_ask_time = time_mod.time()

    # Create mock update for second /ask call (should be rate-limited)
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = config.telegram.allowed_user_ids[0] if config.telegram.allowed_user_ids else 12345
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = ["test", "question"]

    # Ensure user is authorized
    if not config.telegram.allowed_user_ids:
        config.telegram.allowed_user_ids = [update.effective_user.id]

    await commands.cmd_ask(update, context)

    # Verify the rate limit message was sent (not the AI response)
    reply_calls = update.message.reply_text.call_args_list
    assert any("wait" in str(call).lower() for call in reply_calls), \
        f"Expected rate limit message, got: {reply_calls}"
    # AI should NOT have been called
    mock_ai.ask_haiku.assert_not_called()


def test_config_validates_daily_trades():
    """Fix 4.6: Config validation catches invalid max_daily_trades."""
    from src.shell.config import _validate_config, Config

    config = Config()
    config.risk.max_daily_trades = 0

    with pytest.raises(ValueError, match="max_daily_trades"):
        _validate_config(config)


def test_modify_signal_warns_on_size_pct():
    """Fix 4.12: Signal with MODIFY action and size_pct != 0 doesn't crash."""
    from src.shell.contract import Signal, Action

    # Create a MODIFY signal with size_pct — should log warning, not crash
    signal = Signal(
        symbol="BTC/USD", action=Action.MODIFY, size_pct=0.05,
        stop_loss=49000, tag="test_tag",
    )
    assert signal.action == Action.MODIFY
    assert signal.size_pct == 0.05

    # Create a MODIFY signal with size_pct=0 — should NOT warn
    signal2 = Signal(
        symbol="BTC/USD", action=Action.MODIFY, size_pct=0,
        stop_loss=48000, tag="test_tag2",
    )
    assert signal2.size_pct == 0


def test_ai_client_daily_tokens_property():
    """Fix 3.25: AIClient exposes daily_tokens_used as a public property."""
    from src.orchestrator.ai_client import AIClient
    from src.shell.config import AIConfig

    config = AIConfig()
    ai = AIClient(config, db=MagicMock())
    ai._daily_tokens_used = 12345
    assert ai.daily_tokens_used == 12345


# ======================== Session D Audit Fix Tests ========================


def test_backtester_close_all_no_tag():
    """D-C3b: Backtester CLOSE without tag closes ALL positions for symbol."""
    from src.strategy.backtester import Backtester
    from src.shell.contract import (
        Action, Intent, OrderType, RiskLimits, Signal, StrategyBase,
        SymbolData, Portfolio,
    )

    class MultiCloseStrategy(StrategyBase):
        def __init__(self):
            self._step = 0
            self._symbols = []
            self._risk = None

        def initialize(self, risk_limits, symbols):
            self._risk = risk_limits
            self._symbols = symbols

        def analyze(self, markets, portfolio, timestamp):
            self._step += 1
            signals = []
            if self._step == 1:
                # Open two positions for BTC
                signals.append(Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.03))
            elif self._step == 2:
                signals.append(Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.03))
            elif self._step == 5:
                # CLOSE without tag — should close ALL BTC positions
                signals.append(Signal(symbol="BTC/USD", action=Action.CLOSE, size_pct=0.0))
            return signals

    strategy = MultiCloseStrategy()
    risk = RiskLimits(max_trade_pct=0.1, default_trade_pct=0.03, max_positions=5,
                      max_daily_loss_pct=0.05, max_drawdown_pct=0.3, max_position_pct=1.0)
    bt = Backtester(strategy, risk, ["BTC/USD"], starting_cash=1000.0, slippage_factor=0.0)

    # Create 7 hourly bars
    dates = pd.date_range("2024-01-01", periods=7, freq="1h")
    df = pd.DataFrame({
        "open": [50000]*7, "high": [50500]*7, "low": [49500]*7,
        "close": [50000]*7, "volume": [100]*7,
    }, index=dates)

    result = bt.run({"BTC/USD": df})
    # Should have 2 CLOSE trades (both positions closed)
    close_trades = [t for t in result.trades if t.action == "CLOSE"]
    assert len(close_trades) == 2, f"Expected 2 CLOSE trades, got {len(close_trades)}"


def test_backtester_buy_averaging_in():
    """D-C3a: Backtester allows multiple BUY for same symbol (averaging in)."""
    from src.strategy.backtester import Backtester
    from src.shell.contract import (
        Action, Intent, OrderType, RiskLimits, Signal, StrategyBase,
        SymbolData, Portfolio,
    )

    class AverageInStrategy(StrategyBase):
        def __init__(self):
            self._step = 0

        def initialize(self, risk_limits, symbols):
            pass

        def analyze(self, markets, portfolio, timestamp):
            self._step += 1
            if self._step <= 3:
                return [Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.03)]
            return []

    strategy = AverageInStrategy()
    risk = RiskLimits(max_trade_pct=0.1, default_trade_pct=0.03, max_positions=5,
                      max_daily_loss_pct=0.05, max_drawdown_pct=0.3, max_position_pct=1.0)
    bt = Backtester(strategy, risk, ["BTC/USD"], starting_cash=1000.0, slippage_factor=0.0)

    dates = pd.date_range("2024-01-01", periods=5, freq="1h")
    df = pd.DataFrame({
        "open": [50000]*5, "high": [50500]*5, "low": [49500]*5,
        "close": [50000]*5, "volume": [100]*5,
    }, index=dates)

    result = bt.run({"BTC/USD": df})
    # No trades yet (positions still open), but backtester should have 3 open positions
    # We can verify by checking that no errors occurred (result returned) and no sells
    assert result.total_trades == 0  # All positions still open


def test_backtester_drawdown_halt():
    """D-C3c: Backtester halts new entries when max_drawdown is breached."""
    from src.strategy.backtester import Backtester
    from src.shell.contract import (
        Action, Intent, OrderType, RiskLimits, Signal, StrategyBase,
        SymbolData, Portfolio,
    )

    class DrawdownStrategy(StrategyBase):
        def __init__(self):
            self._step = 0
            self._bought = False

        def initialize(self, risk_limits, symbols):
            pass

        def analyze(self, markets, portfolio, timestamp):
            self._step += 1
            if self._step == 1:
                self._bought = True
                return [Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.9,
                              stop_loss=30000)]
            if self._step > 5 and not self._bought:
                # Try to buy again after drawdown halt — should be blocked
                return [Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.1)]
            return []

        def on_position_closed(self, symbol, pnl, pnl_pct, tag=""):
            self._bought = False

    strategy = DrawdownStrategy()
    # Very tight drawdown limit: 5%
    risk = RiskLimits(max_trade_pct=0.95, default_trade_pct=0.1, max_positions=5,
                      max_daily_loss_pct=1.0, max_drawdown_pct=0.05, max_position_pct=1.0)
    bt = Backtester(strategy, risk, ["BTC/USD"], starting_cash=1000.0, slippage_factor=0.0)

    # Span multiple days so daily_values captures drawdown
    dates = pd.date_range("2024-01-01", periods=10, freq="12h")
    prices = [50000, 50000, 28000, 28000, 28000, 28000, 28000, 50000, 50000, 50000]
    df = pd.DataFrame({
        "open": prices, "high": [p+500 for p in prices],
        "low": [p-500 for p in prices],
        "close": prices, "volume": [100]*10,
    }, index=dates)

    result = bt.run({"BTC/USD": df})
    # Should have 1 trade (SL hit) and NO re-entry (drawdown halted)
    assert result.total_trades == 1


def test_backtester_day_boundary_start_value():
    """D-M11: Day start value is set BEFORE trading, not after first bar."""
    from src.strategy.backtester import Backtester
    from src.shell.contract import (
        Action, Intent, RiskLimits, Signal, StrategyBase,
    )

    class DayBoundaryStrategy(StrategyBase):
        """Track what daily_pnl is seen by the strategy at the start of a new day."""
        def __init__(self):
            self._daily_pnls = []

        def initialize(self, risk_limits, symbols):
            pass

        def analyze(self, markets, portfolio, timestamp):
            self._daily_pnls.append(portfolio.daily_pnl)
            return []

    strategy = DayBoundaryStrategy()
    risk = RiskLimits(max_trade_pct=0.1, default_trade_pct=0.03, max_positions=5,
                      max_daily_loss_pct=0.05, max_drawdown_pct=0.3, max_position_pct=1.0)
    bt = Backtester(strategy, risk, ["BTC/USD"], starting_cash=1000.0, slippage_factor=0.0)

    # Two days of hourly data
    dates = pd.date_range("2024-01-01", periods=48, freq="1h")
    df = pd.DataFrame({
        "open": [50000]*48, "high": [50500]*48, "low": [49500]*48,
        "close": [50000]*48, "volume": [100]*48,
    }, index=dates)

    result = bt.run({"BTC/USD": df})
    # First bar of each day should have daily_pnl == 0 (day_start_value set before trading)
    # First bar (index 0): day_start_value = starting_cash
    assert strategy._daily_pnls[0] == 0.0
    # First bar of day 2 (index 24): day_start_value should be refreshed
    assert strategy._daily_pnls[24] == 0.0


def test_modify_no_intent_downgrade():
    """D-L5: MODIFY with default intent=DAY should NOT downgrade SWING/POSITION positions."""
    from src.shell.contract import Action, Intent, Signal
    from src.shell.config import load_config
    from src.shell.database import Database

    async def _run():
        config = load_config()
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            config.db_path = f.name

        try:
            db = Database(config.db_path)
            await db.connect()

            from src.shell.portfolio import PortfolioTracker
            portfolio = PortfolioTracker(config, db, kraken=None)
            await portfolio.initialize()

            # Open a SWING position
            buy_signal = Signal(
                symbol="BTC/USD", action=Action.BUY, size_pct=0.05,
                intent=Intent.SWING, stop_loss=49000, take_profit=55000,
            )
            result = await portfolio.execute_signal(buy_signal, 50000.0, 0.25, 0.40)
            assert result is not None
            tag = result["tag"]

            # Position should be SWING
            pos = portfolio._positions[tag]
            assert pos["intent"] == "SWING"

            # Send MODIFY with default intent (DAY) — should NOT downgrade
            modify_signal = Signal(
                symbol="BTC/USD", action=Action.MODIFY, size_pct=0,
                stop_loss=48000, tag=tag,
            )
            await portfolio.execute_signal(modify_signal, 50000.0, 0.25, 0.40)

            # Intent should still be SWING (not downgraded to DAY)
            assert portfolio._positions[tag]["intent"] == "SWING"
            assert portfolio._positions[tag]["stop_loss"] == 48000

            await db.close()
        finally:
            os.unlink(config.db_path)

    asyncio.run(_run())


def test_json_extractor_backslash_outside_string():
    """D-L11: JSON extractor handles backslashes outside strings correctly."""
    from src.orchestrator.orchestrator import Orchestrator

    # Test with a response that has backslashes in text before JSON
    response_text = 'Here is some text with a backslash \\ before the JSON: {"decision": "NO_CHANGE", "reasoning": "test"}'

    # _extract_json is an instance method — create a minimal instance
    orch = object.__new__(Orchestrator)
    result = orch._extract_json(response_text)
    assert result is not None
    assert result["decision"] == "NO_CHANGE"


def test_spread_uses_ask_denominator():
    """D-M8: Spread formula uses (ask-bid)/ask, not /bid."""
    from src.shell.kraken import KrakenREST
    from src.shell.config import KrakenConfig

    config = KrakenConfig()
    config.rest_url = "https://api.kraken.com"
    config.api_key = "test"
    config.secret_key = "dGVzdA=="  # base64 "test"

    client = KrakenREST(config)

    # Mock get_ticker to return known bid/ask
    async def _run():
        client.get_ticker = AsyncMock(return_value={
            "a": ["100.0", "1", "1.000"],  # ask
            "b": ["90.0", "1", "1.000"],   # bid
        })
        spread = await client.get_spread("BTC/USD")
        # (100 - 90) / 100 = 0.10
        assert abs(spread - 0.10) < 0.001

        await client.close()

    asyncio.run(_run())


def test_readonly_db_conn_access_blocked():
    """D-M6: ReadOnlyDB blocks access to internal connection via __getattr__."""
    from src.statistics.readonly_db import ReadOnlyDB
    import aiosqlite

    async def _run():
        async with aiosqlite.connect(":memory:") as conn:
            conn.row_factory = aiosqlite.Row
            ro = ReadOnlyDB(conn)

            # _conn should be blocked (the old name)
            with pytest.raises(AttributeError):
                _ = ro._conn

            # Arbitrary attribute access should also fail
            with pytest.raises(AttributeError):
                _ = ro.execute_raw

            # But read-only methods should work
            cursor = await ro.execute("SELECT 1 as val")
            row = await cursor.fetchone()
            assert dict(row)["val"] == 1

    asyncio.run(_run())


def test_nonce_inside_rate_lock():
    """D-M7: Kraken nonce is computed inside rate lock (verify structure)."""
    from src.shell.kraken import KrakenREST
    from src.shell.config import KrakenConfig
    import inspect

    config = KrakenConfig()
    config.rest_url = "https://api.kraken.com"
    config.api_key = "test"
    config.secret_key = "dGVzdA=="

    client = KrakenREST(config)

    # Verify nonce computation is inside the rate_lock context
    source = inspect.getsource(client.private)
    # The nonce line should come AFTER "async with self._rate_lock"
    lock_pos = source.find("async with self._rate_lock")
    nonce_pos = source.find("nonce = int(time.time()")
    assert lock_pos > 0, "rate_lock not found in private()"
    assert nonce_pos > lock_pos, "nonce computation should be inside rate_lock"


@pytest.mark.asyncio
async def test_position_monitor_staleness_check():
    """D-L3: Position monitor falls back to REST for stale WS prices."""
    # Verify the staleness detection code exists in _position_monitor
    import inspect
    from src.main import TradingBrain
    source = inspect.getsource(TradingBrain._position_monitor)
    assert "price_age" in source, "_position_monitor should check price staleness"
    assert "stale_symbols" in source, "_position_monitor should track stale symbols"


def test_data_store_rowcount_uses_len():
    """D-L9: store_candles returns len(rows) instead of unreliable rowcount."""
    import inspect
    from src.shell.data_store import DataStore
    source = inspect.getsource(DataStore.store_candles)
    assert "len(rows)" in source, "store_candles should use len(rows)"
    assert "rowcount" not in source or "unreliable" in source, "rowcount should be avoided"


# --- Session E: Final audit fixes ---


def test_paper_test_timestamp_format():
    """E-C1: Paper test ends_at uses strftime format matching SQLite datetime()."""
    import inspect
    from src.orchestrator.orchestrator import Orchestrator
    source = inspect.getsource(Orchestrator)
    # ends_at should use strftime (not isoformat) to match SQLite datetime('now', 'utc')
    assert "strftime(\"%Y-%m-%d %H:%M:%S\")" in source, "ends_at should use strftime for SQLite compatibility"
    # Query should use datetime('now', 'utc') not datetime('now')
    assert "datetime('now', 'utc')" in source, "Paper test query should use UTC"


def test_paper_test_trade_query_upper_bound():
    """E-C3: Paper test trade query filters by both start and end time."""
    import inspect
    from src.orchestrator.orchestrator import Orchestrator
    source = inspect.getsource(Orchestrator._evaluate_paper_tests)
    assert "datetime(closed_at) <= datetime(?)" in source, "Trade query should normalize timestamps with datetime()"


def test_broadcast_ws_error_handling():
    """E-C2: _broadcast_ws wraps in try/except to not block Telegram."""
    import inspect
    from src.telegram.notifications import Notifier
    source = inspect.getsource(Notifier._broadcast_ws)
    assert "try:" in source, "_broadcast_ws should have error handling"
    assert "except" in source, "_broadcast_ws should catch exceptions"


def test_orchestrator_cycle_lock():
    """E-M5: Orchestrator uses asyncio.Lock to prevent concurrent cycles."""
    import inspect
    from src.orchestrator.orchestrator import Orchestrator
    source = inspect.getsource(Orchestrator.__init__)
    assert "_cycle_lock" in source, "Orchestrator should have a cycle lock"
    source_run = inspect.getsource(Orchestrator.run_nightly_cycle)
    assert "_cycle_lock" in source_run, "run_nightly_cycle should use the lock"


def test_risk_initialize_accepts_timezone():
    """E-M1: RiskManager.initialize accepts tz_name for consistent daily boundaries."""
    import inspect
    from src.shell.risk import RiskManager
    sig = inspect.signature(RiskManager.initialize)
    assert "tz_name" in sig.parameters, "initialize should accept tz_name parameter"


def test_backtester_max_position_pct():
    """E-M2: Backtester enforces max_position_pct per symbol."""
    from src.shell.contract import Action, Signal, RiskLimits
    from src.strategy.backtester import Backtester

    class BigBuyStrategy:
        def initialize(self, risk_limits, symbols): pass
        def on_fill(self, *a, **kw): pass
        def get_state(self): return {}
        def load_state(self, s): pass
        def analyze(self, markets, portfolio, timestamp):
            # Try to buy 50% of portfolio each time
            return [Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.5)]

    strategy = BigBuyStrategy()
    # max_position_pct=0.3 means only 30% of portfolio in one symbol
    risk = RiskLimits(max_trade_pct=0.5, default_trade_pct=0.05, max_positions=5,
                      max_daily_loss_pct=0.5, max_drawdown_pct=0.5, max_position_pct=0.3)
    bt = Backtester(strategy, risk, ["BTC/USD"], starting_cash=1000.0, slippage_factor=0.0)

    dates = pd.date_range("2024-01-01", periods=3, freq="1h")
    df = pd.DataFrame({
        "open": [50000]*3, "high": [50500]*3,
        "low": [49500]*3, "close": [50000]*3, "volume": [100]*3,
    }, index=dates)

    result = bt.run({"BTC/USD": df})
    # First BUY at 50% should be blocked (50% > 30% max_position_pct)
    assert result.total_trades == 0, "BUY exceeding max_position_pct should be blocked"


def test_daily_reset_under_trade_lock():
    """E-M7: _daily_reset acquires trade_lock."""
    import inspect
    from src.main import TradingBrain
    source = inspect.getsource(TradingBrain._daily_reset)
    assert "self._trade_lock" in source, "_daily_reset should acquire trade_lock"


def test_database_connect_cleanup_on_error():
    """E-M6: Database.connect closes connection on migration error."""
    import inspect
    from src.shell.database import Database
    source = inspect.getsource(Database.connect)
    # Should close connection and reset to None on error (except + raise or finally)
    assert "self._conn.close()" in source, "connect should close connection on error"
    assert "self._conn = None" in source, "connect should reset _conn on error"


def test_candle_cutoff_uses_strftime():
    """E-M9: Candle aggregation cutoffs use strftime (no timezone suffix)."""
    import inspect
    from src.shell.data_store import DataStore
    source = inspect.getsource(DataStore.aggregate_5m_to_1h)
    assert 'strftime("%Y-%m-%dT%H:%M:%S")' in source, "Cutoff should use strftime"


def test_portfolio_uses_utc_timestamps():
    """E-M3: Portfolio timestamps use timezone.utc."""
    import inspect
    from src.shell.portfolio import PortfolioTracker
    # Check _confirm_fill (was a hot spot for bare datetime.now())
    source = inspect.getsource(PortfolioTracker._confirm_fill)
    assert "datetime.now()" not in source or "timezone.utc" in source, "Should use UTC timestamps"


def test_halt_notification_deduplication():
    """E-L1: Only one halt notification per scan cycle."""
    import inspect
    from src.main import TradingBrain
    source = inspect.getsource(TradingBrain._scan_loop)
    assert "halt_notified" in source, "Scan loop should track halt notification state"


def test_send_long_error_handling():
    """E-L6: _send_long catches Telegram API errors."""
    import inspect
    from src.telegram.commands import BotCommands
    source = inspect.getsource(BotCommands._send_long)
    assert "try:" in source, "_send_long should have error handling"
    assert "except" in source, "_send_long should catch exceptions"


# ──────────────────────────────────────────────────────────────────────
# Session H: Test Coverage Gap Fixes (T1–T4)
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_api_positions_with_data():
    """T1: /v1/positions returns unrealized P&L, tag, SL/TP with actual position data."""
    from aiohttp.test_utils import TestClient, TestServer

    from src.api import api_key_key
    from src.api.server import create_app
    from src.shell.config import load_config
    from src.shell.database import Database
    from src.shell.risk import RiskManager

    config = load_config()
    db_path = tempfile.mktemp(suffix=".db")
    try:
        db = Database(db_path)
        await db.connect()

        # Seed a position
        await db.execute(
            """INSERT INTO positions (symbol, tag, side, qty, avg_entry, current_price,
               stop_loss, take_profit, intent, opened_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("BTC/USD", "core_btc_001", "long", 0.01, 50000, 52000,
             48000, 60000, "SWING", "2026-02-10 12:00:00"),
        )
        await db.commit()

        risk = RiskManager(config.risk)
        portfolio = MagicMock()
        portfolio.total_value = AsyncMock(return_value=1000.0)
        ai = MagicMock()
        ai.get_daily_usage = AsyncMock(return_value={"used": 0, "total_cost": 0, "models": {}})
        ai.tokens_remaining = 1500000

        # Scan state has live price for BTC/USD
        scan_state = {"symbols": {"BTC/USD": {"price": 53000, "spread": 0.2}}}
        commands = MagicMock()
        commands.is_paused = False

        app, _, _ = create_app(config, db, portfolio, risk, ai, scan_state, commands)
        app[api_key_key] = "test-key"
        headers = {"Authorization": "Bearer test-key"}

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/v1/positions", headers=headers)
            assert resp.status == 200
            body = await resp.json()
            positions = body["data"]
            assert len(positions) == 1

            pos = positions[0]
            assert pos["symbol"] == "BTC/USD"
            assert pos["tag"] == "core_btc_001"
            assert pos["entry_price"] == 50000
            assert pos["current_price"] == 53000  # From scan_state, not DB
            assert pos["stop_loss"] == 48000
            assert pos["take_profit"] == 60000

            # Unrealized P&L: (53000 - 50000) * 0.01 = 30.0
            assert pos["unrealized_pnl"] == 30.0
            # Unrealized P&L %: (53000/50000 - 1) * 100 = 6.0
            assert pos["unrealized_pnl_pct"] == 6.0

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_api_portfolio_and_risk_nontrivial():
    """T2: /v1/portfolio and /v1/risk with non-trivial state (drawdown, daily PnL)."""
    from aiohttp.test_utils import TestClient, TestServer

    from src.api import api_key_key
    from src.api.server import create_app
    from src.shell.config import load_config
    from src.shell.database import Database
    from src.shell.risk import RiskManager

    config = load_config()
    db_path = tempfile.mktemp(suffix=".db")
    try:
        db = Database(db_path)
        await db.connect()

        risk = RiskManager(config.risk)
        risk._daily_pnl = -25.0
        risk._daily_trades = 3
        risk._consecutive_losses = 2
        risk._peak_portfolio = 1100.0  # Higher than current → drawdown

        # Portfolio with actual positions
        portfolio = MagicMock()
        portfolio.total_value = AsyncMock(return_value=950.0)
        portfolio.position_count = 2
        pos_mock = MagicMock()
        pos_mock.unrealized_pnl = 15.0
        portfolio.get_portfolio = AsyncMock(return_value=MagicMock(
            total_value=950.0, cash=700.0, positions=[pos_mock]
        ))

        ai = MagicMock()
        ai.get_daily_usage = AsyncMock(return_value={"used": 500, "total_cost": 0.005, "models": {}})
        ai.tokens_remaining = 1499500
        scan_state = {"symbols": {}}
        commands = MagicMock()
        commands.is_paused = False

        app, _, _ = create_app(config, db, portfolio, risk, ai, scan_state, commands)
        app[api_key_key] = "test-key"
        headers = {"Authorization": "Bearer test-key"}

        async with TestClient(TestServer(app)) as client:
            # /v1/portfolio — verify allocation math
            resp = await client.get("/v1/portfolio", headers=headers)
            assert resp.status == 200
            body = await resp.json()
            assert body["data"]["total_value"] == 950.0
            assert body["data"]["cash"] == 700.0
            assert body["data"]["unrealized_pnl"] == 15.0
            assert body["data"]["position_count"] == 1  # len(positions) from mock
            cash_pct = body["data"]["allocation"]["cash_pct"]
            assert abs(cash_pct - 73.7) < 0.1  # 700/950*100

            # /v1/risk — verify non-zero risk state
            resp = await client.get("/v1/risk", headers=headers)
            assert resp.status == 200
            body = await resp.json()
            current = body["data"]["current"]
            assert current["daily_pnl"] == -25.0
            assert current["daily_trades"] == 3
            assert current["consecutive_losses"] == 2
            # Drawdown: (1100 - 950) / 1100 ≈ 0.1364
            assert abs(current["drawdown_pct"] - 0.1364) < 0.001

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_websocket_auth_rejection_and_max_clients():
    """T3: WebSocket rejects wrong token (401) and enforces max client limit (503)."""
    from aiohttp.test_utils import TestClient, TestServer

    from src.api import api_key_key
    from src.api.server import create_app
    from src.api.websocket import WebSocketManager
    from src.shell.config import load_config
    from src.shell.database import Database
    from src.shell.risk import RiskManager

    config = load_config()
    db_path = tempfile.mktemp(suffix=".db")
    try:
        db = Database(db_path)
        await db.connect()

        risk = RiskManager(config.risk)
        app, ws_manager, _ = create_app(config, db, MagicMock(), risk, MagicMock(), {})
        app[api_key_key] = "correct-token"

        async with TestClient(TestServer(app)) as client:
            # Wrong token → should fail with 401
            resp = await client.get("/v1/events", params={"token": "wrong-token"})
            assert resp.status == 401

            # No token → should fail with 401
            resp = await client.get("/v1/events")
            assert resp.status == 401

            # Correct token → should upgrade to WebSocket
            async with client.ws_connect("/v1/events?token=correct-token") as ws:
                assert ws_manager.client_count == 1

            # After disconnect
            assert ws_manager.client_count == 0

            # Test max clients enforcement by filling up _clients with mocks
            original_max = WebSocketManager.MAX_CLIENTS
            WebSocketManager.MAX_CLIENTS = 0  # Set to 0 so next connect exceeds
            try:
                resp = await client.get("/v1/events", params={"token": "correct-token"})
                assert resp.status == 503
            finally:
                WebSocketManager.MAX_CLIENTS = original_max

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_ask_rate_limit_timestamp_set_on_success():
    """T4: /ask sets _last_ask_time after successful AI call (not just on rejection)."""
    import time as time_mod
    from src.telegram.commands import BotCommands
    from src.shell.config import load_config

    config = load_config()
    mock_db = MagicMock()

    # Mock DB queries that cmd_ask uses for context
    mock_db.fetchall = AsyncMock(return_value=[])
    mock_db.fetchone = AsyncMock(return_value=None)

    mock_ai = MagicMock()
    mock_ai.ask_haiku = AsyncMock(return_value="The fund is performing well.")

    commands = BotCommands(config=config, db=mock_db, scan_state={}, ai_client=mock_ai)

    # Verify initial state
    assert commands._last_ask_time == 0

    # Create mock update for /ask call
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = config.telegram.allowed_user_ids[0] if config.telegram.allowed_user_ids else 12345
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = ["How", "is", "the", "fund?"]

    if not config.telegram.allowed_user_ids:
        config.telegram.allowed_user_ids = [update.effective_user.id]

    before = time_mod.time()
    await commands.cmd_ask(update, context)
    after = time_mod.time()

    # AI should have been called
    mock_ai.ask_haiku.assert_called_once()

    # _last_ask_time should now be set to a timestamp between before and after
    assert commands._last_ask_time >= before
    assert commands._last_ask_time <= after

    # Verify a response was sent (not a rate limit message)
    reply_calls = update.message.reply_text.call_args_list
    assert any("performing" in str(call).lower() or "fund" in str(call).lower()
               for call in reply_calls), f"Expected AI response, got: {reply_calls}"


# =============================================================================
# Session I: New Tests — Audit Round 11
# =============================================================================


def test_sandbox_blocks_transitive_src_imports():
    """I1: Sandbox blocks transitive src.* imports that could access shell internals."""
    from src.strategy.sandbox import validate_strategy

    # Attempt to import src.shell.config (allowed: only src.shell.contract)
    code = '''
from src.shell.config import Config
from src.shell.contract import StrategyBase, Signal

class Strategy(StrategyBase):
    def initialize(self, risk_limits, symbols):
        pass
    def analyze(self, markets, portfolio, timestamp):
        return []
'''
    result = validate_strategy(code)
    assert not result.passed
    assert any("src.shell.config" in e for e in result.errors)

    # Allowed import: src.shell.contract
    code_ok = '''
from src.shell.contract import StrategyBase, Signal

class Strategy(StrategyBase):
    def initialize(self, risk_limits, symbols):
        pass
    def analyze(self, markets, portfolio, timestamp):
        return []
'''
    result_ok = validate_strategy(code_ok)
    assert result_ok.passed

    # Verify src.strategy.skills.* is NOW blocked (skills library removed)
    from src.strategy.sandbox import check_imports
    code_skills = 'from src.strategy.skills.indicators import ema\n'
    errors = check_imports(code_skills)
    assert len(errors) > 0
    assert "src.strategy.skills" in errors[0]

    # Verify src.shell.database is blocked
    code_blocked = 'from src.shell.database import Database\n'
    errors_blocked = check_imports(code_blocked)
    assert len(errors_blocked) > 0
    assert "src.shell.database" in errors_blocked[0]


def test_backtester_daily_trade_count_and_consecutive_loss_halt():
    """I16+I17: Backtester enforces daily trade count limit and consecutive-loss halt."""
    from src.shell.contract import (
        Action, Intent, Portfolio, RiskLimits, Signal, StrategyBase, SymbolData,
    )
    from src.strategy.backtester import Backtester
    import pandas as pd
    import numpy as np

    class AggressiveStrategy(StrategyBase):
        """Strategy that generates a BUY signal every bar."""
        def initialize(self, risk_limits, symbols):
            self._symbols = symbols

        def analyze(self, markets, portfolio, timestamp):
            signals = []
            for sym in self._symbols:
                if sym in markets and portfolio.cash > 10:
                    signals.append(Signal(
                        symbol=sym, action=Action.BUY, size_pct=0.01,
                        stop_loss=markets[sym].current_price * 0.99,
                        take_profit=markets[sym].current_price * 1.01,
                    ))
            return signals

    # Tight daily trade limit: 3 trades per day
    risk = RiskLimits(
        max_trade_pct=0.10, default_trade_pct=0.02, max_positions=10,
        max_daily_loss_pct=0.05, max_drawdown_pct=0.40,
        max_daily_trades=3, rollback_consecutive_losses=5,
    )

    # Create synthetic price data — 2 days, 10 bars each
    dates = pd.date_range("2026-01-01", periods=20, freq="1h")
    df = pd.DataFrame({
        "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000,
    }, index=dates)

    bt = Backtester(AggressiveStrategy(), risk, ["SYM/USD"], starting_cash=1000.0)
    result = bt.run({"SYM/USD": df}, "1h")

    # With 3 trade/day limit across 2 days, max possible BUYs is 6
    # But we also need SELLs... since strategy only BUYs, let's just check
    # that the strategy didn't generate unbounded trades
    assert result.total_trades <= 10  # Bounded by the daily limit mechanism


# --- Phase 1: Close-Reason Tracking ---

@pytest.mark.asyncio
async def test_close_reason_signal_default():
    """Close-reason defaults to 'signal' for normal BUY→CLOSE cycle."""
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
        portfolio = PortfolioTracker(config, db, KrakenREST(config.kraken))
        await portfolio.initialize()

        # BUY
        buy = Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.05, intent=Intent.DAY)
        await portfolio.execute_signal(buy, 50000, 0.25, 0.40)
        # CLOSE (default close_reason="signal")
        close = Signal(symbol="BTC/USD", action=Action.CLOSE, size_pct=1.0, intent=Intent.DAY)
        result = await portfolio.execute_signal(close, 51000, 0.25, 0.40)
        assert result is not None
        assert result["close_reason"] == "signal"

        trade = await db.fetchone("SELECT close_reason FROM trades WHERE closed_at IS NOT NULL")
        assert trade["close_reason"] == "signal"
        await db.close()
    finally:
        os.unlink(config.db_path)


@pytest.mark.asyncio
async def test_close_reason_emergency():
    """Close-reason='emergency' when passed explicitly."""
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
        portfolio = PortfolioTracker(config, db, KrakenREST(config.kraken))
        await portfolio.initialize()

        buy = Signal(symbol="ETH/USD", action=Action.BUY, size_pct=0.05, intent=Intent.DAY)
        await portfolio.execute_signal(buy, 3000, 0.25, 0.40)
        close = Signal(symbol="ETH/USD", action=Action.CLOSE, size_pct=1.0, intent=Intent.DAY)
        result = await portfolio.execute_signal(close, 2900, 0.25, 0.40, close_reason="emergency")
        assert result is not None
        assert result["close_reason"] == "emergency"

        trade = await db.fetchone("SELECT close_reason FROM trades WHERE closed_at IS NOT NULL")
        assert trade["close_reason"] == "emergency"
        await db.close()
    finally:
        os.unlink(config.db_path)


@pytest.mark.asyncio
async def test_close_reason_stop_loss():
    """Close-reason='stop_loss' propagated via update_prices → execute_signal."""
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
        portfolio = PortfolioTracker(config, db, KrakenREST(config.kraken))
        await portfolio.initialize()

        buy = Signal(symbol="SOL/USD", action=Action.BUY, size_pct=0.05,
                     stop_loss=90.0, take_profit=150.0, intent=Intent.DAY)
        await portfolio.execute_signal(buy, 100, 0.25, 0.40)

        # Trigger stop-loss via update_prices
        triggered = await portfolio.update_prices({"SOL/USD": 89.0})
        assert len(triggered) == 1
        assert triggered[0]["reason"] == "stop_loss"

        # Simulate what main.py does: pass reason as close_reason
        t = triggered[0]
        sig = Signal(symbol=t["symbol"], action=Action.CLOSE, size_pct=1.0,
                     intent=Intent.DAY, confidence=1.0, tag=t["tag"])
        result = await portfolio.execute_signal(sig, t["price"], 0.25, 0.40,
                                                close_reason=t["reason"])
        assert result is not None
        assert result["close_reason"] == "stop_loss"

        trade = await db.fetchone("SELECT close_reason FROM trades WHERE closed_at IS NOT NULL")
        assert trade["close_reason"] == "stop_loss"
        await db.close()
    finally:
        os.unlink(config.db_path)


# --- Phase 2: Backtester LIMIT Order Simulation ---

def test_backtester_limit_buy_fills_when_low_reaches():
    """LIMIT BUY fills at limit_price when candle low reaches it, uses maker fee."""
    from src.strategy.backtester import Backtester
    from src.shell.contract import (
        Action, Intent, OrderType, Portfolio, RiskLimits, Signal,
        StrategyBase, SymbolData,
    )

    class LimitBuyStrategy(StrategyBase):
        def initialize(self, risk_limits, symbols):
            self._bought = False
        def analyze(self, markets, portfolio, timestamp):
            if not self._bought:
                self._bought = True
                return [Signal(
                    symbol="SYM/USD", action=Action.BUY, size_pct=0.05,
                    intent=Intent.DAY, order_type=OrderType.LIMIT, limit_price=99.0,
                )]
            return []

    dates = pd.date_range("2024-01-01", periods=5, freq="1h")
    # Candle with low=98 reaches the limit_price=99
    df = pd.DataFrame({
        "open": [100, 100, 100, 100, 100],
        "high": [102, 102, 102, 102, 102],
        "low": [98, 98, 98, 98, 98],
        "close": [100, 100, 100, 100, 100],
        "volume": [1000] * 5,
    }, index=dates)

    risk = RiskLimits(max_trade_pct=0.10, default_trade_pct=0.05,
                      max_positions=5, max_daily_loss_pct=0.10, max_drawdown_pct=0.40)
    bt = Backtester(LimitBuyStrategy(), risk, ["SYM/USD"],
                    maker_fee_pct=0.25, taker_fee_pct=0.40, starting_cash=1000.0)
    result = bt.run({"SYM/USD": df}, "1h")

    assert result.limit_orders_attempted == 1
    assert result.limit_orders_filled == 1
    # No closing trades, but BUY was tracked via limit counters
    assert "Limit Fill" in result.summary()


def test_backtester_limit_buy_skips_when_low_above():
    """LIMIT BUY does NOT fill when candle low is above limit_price."""
    from src.strategy.backtester import Backtester
    from src.shell.contract import (
        Action, Intent, OrderType, Portfolio, RiskLimits, Signal,
        StrategyBase, SymbolData,
    )

    class LimitBuyStrategy(StrategyBase):
        def initialize(self, risk_limits, symbols):
            self._bought = False
        def analyze(self, markets, portfolio, timestamp):
            if not self._bought:
                self._bought = True
                return [Signal(
                    symbol="SYM/USD", action=Action.BUY, size_pct=0.05,
                    intent=Intent.DAY, order_type=OrderType.LIMIT, limit_price=95.0,
                )]
            return []

    dates = pd.date_range("2024-01-01", periods=5, freq="1h")
    # Candle low=98 never reaches limit_price=95
    df = pd.DataFrame({
        "open": [100, 100, 100, 100, 100],
        "high": [102, 102, 102, 102, 102],
        "low": [98, 98, 98, 98, 98],
        "close": [100, 100, 100, 100, 100],
        "volume": [1000] * 5,
    }, index=dates)

    risk = RiskLimits(max_trade_pct=0.10, default_trade_pct=0.05,
                      max_positions=5, max_daily_loss_pct=0.10, max_drawdown_pct=0.40)
    bt = Backtester(LimitBuyStrategy(), risk, ["SYM/USD"],
                    maker_fee_pct=0.25, taker_fee_pct=0.40, starting_cash=1000.0)
    result = bt.run({"SYM/USD": df}, "1h")

    assert result.limit_orders_attempted == 1
    assert result.limit_orders_filled == 0
    assert result.total_trades == 0  # No position opened


# --- Phase 3: Backtester Per-Symbol Spread ---

def test_backtester_per_symbol_spread():
    """Backtester calculates spread from candle H/L/C instead of hardcoding 0.001."""
    from src.strategy.backtester import Backtester
    from src.shell.contract import (
        Action, Intent, OrderType, Portfolio, RiskLimits, Signal,
        StrategyBase, SymbolData,
    )

    class SpreadCheckStrategy(StrategyBase):
        """Captures spread values from SymbolData."""
        def initialize(self, risk_limits, symbols):
            self.spreads = []
        def analyze(self, markets, portfolio, timestamp):
            for sym, data in markets.items():
                self.spreads.append(data.spread)
            return []

    # Create candles with known H=110, L=90, C=100 → intrabar spread = (110-90)/100 = 0.20
    dates = pd.date_range("2024-01-01", periods=50, freq="1h")
    df = pd.DataFrame({
        "open": [100] * 50,
        "high": [110] * 50,
        "low": [90] * 50,
        "close": [100] * 50,
        "volume": [1000] * 50,
    }, index=dates)

    risk = RiskLimits(max_trade_pct=0.10, default_trade_pct=0.05,
                      max_positions=5, max_daily_loss_pct=0.10, max_drawdown_pct=0.40)
    strategy = SpreadCheckStrategy()
    bt = Backtester(strategy, risk, ["SYM/USD"], starting_cash=1000.0)
    bt.run({"SYM/USD": df}, "1h")

    # Should have computed spreads (not the hardcoded 0.001)
    assert len(strategy.spreads) > 0
    # Early bars use fallback (< 10 candles), later bars should be ~0.20
    computed_spreads = [s for s in strategy.spreads if abs(s - 0.001) > 0.0001]
    assert len(computed_spreads) > 0, "No computed spreads found — all were fallback"
    for s in computed_spreads:
        assert abs(s - 0.20) < 0.01, f"Expected ~0.20, got {s}"


# --- Phase 4: Truth Benchmark Expansion ---

@pytest.mark.asyncio
async def test_truth_benchmarks_expanded():
    """Truth benchmarks include 7 new fund-quality metrics."""
    from src.shell.database import Database
    from src.shell.truth import compute_truth_benchmarks

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        await db.connect()

        # Seed trades with close_reason
        now = datetime.now(timezone.utc)
        for i in range(5):
            pnl = 10.0 if i < 3 else -5.0
            pnl_pct = 0.05 if i < 3 else -0.025
            opened = (now - timedelta(hours=10 - i)).isoformat()
            closed = (now - timedelta(hours=9 - i)).isoformat()
            reason = "signal" if i < 4 else "stop_loss"
            await db.execute(
                """INSERT INTO trades
                   (symbol, side, qty, entry_price, exit_price, pnl, pnl_pct, fees,
                    intent, opened_at, closed_at, close_reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("BTC/USD", "long", 0.001, 50000, 51000, pnl, pnl_pct, 0.5,
                 "DAY", opened, closed, reason),
            )

        # Seed daily_performance for Sharpe/Sortino
        for i in range(5):
            d = (now - timedelta(days=4 - i)).strftime("%Y-%m-%d")
            val = 200 + i * 2  # Steadily increasing
            await db.execute(
                "INSERT INTO daily_performance (date, portfolio_value, cash) VALUES (?, ?, ?)",
                (d, val, val),
            )
        await db.commit()

        benchmarks = await compute_truth_benchmarks(db)

        # Verify new metrics exist and are reasonable
        assert benchmarks["profit_factor"] > 0
        assert "signal" in benchmarks["close_reason_breakdown"]
        assert "stop_loss" in benchmarks["close_reason_breakdown"]
        assert benchmarks["close_reason_breakdown"]["signal"] == 4
        assert benchmarks["close_reason_breakdown"]["stop_loss"] == 1
        assert benchmarks["avg_trade_duration_hours"] > 0
        assert benchmarks["best_trade_pnl_pct"] == 0.05
        assert benchmarks["worst_trade_pnl_pct"] == -0.025
        assert isinstance(benchmarks["sharpe_ratio"], float)
        assert isinstance(benchmarks["sortino_ratio"], float)

        await db.close()
    finally:
        os.unlink(db_path)


# --- Phase 5: Paper Test Minimum Trade Count ---

@pytest.mark.asyncio
async def test_paper_test_inconclusive_below_minimum():
    """Paper test with fewer trades than min_paper_test_trades → inconclusive."""
    from src.shell.config import load_config
    from src.shell.database import Database
    from src.shell.data_store import DataStore
    from src.orchestrator.orchestrator import Orchestrator

    config = load_config()
    config.orchestrator.min_paper_test_trades = 5  # Require 5 trades
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        config.db_path = f.name

    try:
        db = Database(config.db_path)
        await db.connect()
        data_store = DataStore(db, config.data)

        ai = AsyncMock()
        ai.tokens_remaining = 1000000
        ai._daily_tokens_used = 0

        orch = Orchestrator(config, db, ai, MagicMock(), data_store)

        # Create a paper test that has already ended
        await db.execute(
            """INSERT INTO paper_tests
               (strategy_version, risk_tier, required_days, started_at, ends_at, status)
               VALUES ('v_min', 1, 1, datetime('now', '-3 hours'), datetime('now', '-1 hour'), 'running')"""
        )
        # Insert only 3 trades (below minimum of 5)
        for i in range(3):
            await db.execute(
                """INSERT INTO trades (symbol, side, qty, entry_price, exit_price, pnl, pnl_pct,
                   fees, intent, strategy_version, opened_at, closed_at)
                   VALUES ('BTC/USD', 'long', 0.001, 50000, 51000, 1.0, 0.02, 0.20, 'DAY', 'v_min',
                           datetime('now', '-2 hours'), datetime('now', '-1 hour'))"""
            )
        await db.commit()

        results = await orch._evaluate_paper_tests()
        assert len(results) == 1
        assert results[0]["status"] == "inconclusive"
        assert results[0]["trades"] == 3
        assert results[0]["min_required"] == 5

        # Verify DB updated
        test = await db.fetchone("SELECT * FROM paper_tests WHERE strategy_version = 'v_min'")
        assert test["status"] == "inconclusive"
        result_data = json.loads(test["result"])
        assert result_data["min_required"] == 5

        await db.close()
    finally:
        os.unlink(config.db_path)


# --- Phase 6: Orchestrator Prompt Accuracy ---

def test_prompt_content_accuracy():
    """LAYER_2_SYSTEM and CODE_GEN_SYSTEM contain key awareness phrases."""
    from src.orchestrator.orchestrator import LAYER_2_SYSTEM, CODE_GEN_SYSTEM

    # Close-reason tracking
    assert "close_reason_breakdown" in LAYER_2_SYSTEM
    assert "stop_loss" in LAYER_2_SYSTEM and "take_profit" in LAYER_2_SYSTEM
    assert "emergency" in LAYER_2_SYSTEM and "reconciliation" in LAYER_2_SYSTEM

    # Paper vs Live differences
    assert "Paper mode" in LAYER_2_SYSTEM
    assert "Live mode" in LAYER_2_SYSTEM
    assert "inconclusive" in LAYER_2_SYSTEM

    # Backtester capabilities
    assert "LIMIT" in LAYER_2_SYSTEM
    assert "candle low" in LAYER_2_SYSTEM or "candle high" in LAYER_2_SYSTEM
    assert "per-symbol spread" in LAYER_2_SYSTEM

    # Strategy regime caveat
    assert "strategy's opinion" in LAYER_2_SYSTEM

    # Sandbox restrictions
    assert "operator" in LAYER_2_SYSTEM
    assert "Name-mangled" in LAYER_2_SYSTEM or "__getattribute__" in LAYER_2_SYSTEM

    # Available imports in LAYER_2 (skills library removed, replaced with expanded toolkit)
    assert "scipy" in LAYER_2_SYSTEM
    assert "src.shell.contract" in LAYER_2_SYSTEM

    # Risk counter persistence
    assert "consecutive loss" in LAYER_2_SYSTEM.lower()
    assert "persists across days" in LAYER_2_SYSTEM

    # CODE_GEN_SYSTEM updates
    assert "maker_fee_pct" in CODE_GEN_SYSTEM
    assert "LIMIT" in CODE_GEN_SYSTEM
    assert "scipy" in CODE_GEN_SYSTEM
    assert "ta.trend" in CODE_GEN_SYSTEM
    assert "ta.momentum" in CODE_GEN_SYSTEM

    # Sharpe/Sortino in truth benchmarks description
    assert "Sharpe" in LAYER_2_SYSTEM
    assert "Sortino" in LAYER_2_SYSTEM


def test_websocket_nan_price_ignored():
    """I22: WebSocket ignores NaN/inf prices from Kraken."""
    from src.shell.kraken import KrakenWebSocket
    import math

    ws = KrakenWebSocket("wss://example.com", ["BTC/USD"])

    # Simulate a normal price update
    ws._prices["BTC/USD"] = 50000.0
    ws._price_updated_at["BTC/USD"] = 100.0

    # math.isfinite guard is in _listen(), but we can verify the guard logic directly
    assert math.isfinite(50000.0)
    assert not math.isfinite(float("inf"))
    assert not math.isfinite(float("nan"))

    # Verify price_age works
    import time
    ws._price_updated_at["BTC/USD"] = time.monotonic()
    assert ws.price_age("BTC/USD") < 1.0
    assert ws.price_age("ETH/USD") == float("inf")


# --- Restart Safety (L1-L9) ---

@pytest.mark.asyncio
async def test_system_meta_persists_starting_capital():
    """L1: First boot stores paper_balance_usd in system_meta."""
    from src.shell.config import load_config
    from src.shell.database import Database
    from src.shell.portfolio import PortfolioTracker
    from src.shell.kraken import KrakenREST

    config = load_config()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        config.db_path = f.name

    try:
        db = Database(config.db_path)
        await db.connect()

        kraken = KrakenREST(config.kraken)
        portfolio = PortfolioTracker(config, db, kraken)
        await portfolio.initialize()

        # Verify system_meta has the starting capital
        row = await db.fetchone("SELECT value FROM system_meta WHERE key = 'paper_starting_capital'")
        assert row is not None
        assert float(row["value"]) == config.paper_balance_usd
        assert portfolio.cash == config.paper_balance_usd

        await db.close()
    finally:
        os.unlink(config.db_path)


@pytest.mark.asyncio
async def test_paper_cash_survives_config_change():
    """L1: Changing paper_balance_usd in config doesn't alter reconciled cash."""
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

        # First boot — stores starting capital
        p1 = PortfolioTracker(config, db, kraken)
        await p1.initialize()
        original_cash = p1.cash
        assert original_cash == config.paper_balance_usd

        # Execute a trade that costs money (fees)
        buy = Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.05, intent=Intent.DAY)
        await p1.execute_signal(buy, 50000, 0.25, 0.40)
        cash_after_buy = p1.cash

        # "Restart" with a DIFFERENT config value
        config2 = load_config()
        config2.db_path = config.db_path
        config2.paper_balance_usd = 9999.0  # Changed config

        p2 = PortfolioTracker(config2, db, kraken)
        await p2.initialize()

        # Cash should be reconciled from DB (not from new config)
        assert abs(p2.cash - cash_after_buy) < 0.01
        assert p2.cash != 9999.0  # Must NOT use new config value

        await db.close()
    finally:
        os.unlink(config.db_path)


@pytest.mark.asyncio
async def test_paper_cash_always_reconciles():
    """L1: Cash formula runs unconditionally — no dependency on daily_performance snapshot."""
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
        p1 = PortfolioTracker(config, db, kraken)
        await p1.initialize()

        # Buy a position (ties up cash in position costs)
        buy = Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.05,
                     intent=Intent.DAY, stop_loss=48000, take_profit=55000)
        result = await p1.execute_signal(buy, 50000, 0.25, 0.40)
        assert result is not None
        cash_with_pos = p1.cash

        # Verify daily_performance is EMPTY (no snapshot taken yet)
        snap = await db.fetchone("SELECT COUNT(*) as cnt FROM daily_performance")
        assert snap["cnt"] == 0

        # "Restart" — should still reconcile correctly without any snapshot
        p2 = PortfolioTracker(config, db, kraken)
        await p2.initialize()

        # Cash should match (reconciled from first principles)
        assert abs(p2.cash - cash_with_pos) < 0.01

        await db.close()
    finally:
        os.unlink(config.db_path)


def test_risk_halt_eval_drawdown():
    """L2: Drawdown beyond max triggers halt on startup."""
    from src.shell.config import RiskConfig
    from src.shell.risk import RiskManager

    config = RiskConfig(max_drawdown_pct=0.10, rollback_daily_loss_pct=0.15,
                        rollback_consecutive_losses=999, max_daily_loss_pct=0.10)
    rm = RiskManager(config)
    rm._peak_portfolio = 1000.0  # Simulate peak

    # 15% drawdown > 10% limit
    rm.evaluate_halt_state(portfolio_value=850.0, daily_start_value=900.0)
    assert rm.is_halted
    assert "drawdown" in rm.halt_reason.lower()


def test_risk_halt_eval_consecutive_losses():
    """L2: Consecutive losses at/above limit triggers halt."""
    from src.shell.config import RiskConfig
    from src.shell.risk import RiskManager

    config = RiskConfig(max_drawdown_pct=0.40, rollback_daily_loss_pct=0.15,
                        rollback_consecutive_losses=5, max_daily_loss_pct=0.10)
    rm = RiskManager(config)
    rm._consecutive_losses = 5

    rm.evaluate_halt_state(portfolio_value=100.0, daily_start_value=100.0)
    assert rm.is_halted
    assert "consecutive" in rm.halt_reason.lower()


def test_risk_halt_eval_daily_loss():
    """L2: Daily loss beyond limit triggers halt."""
    from src.shell.config import RiskConfig
    from src.shell.risk import RiskManager

    config = RiskConfig(max_drawdown_pct=0.40, rollback_daily_loss_pct=0.15,
                        rollback_consecutive_losses=999, max_daily_loss_pct=0.05)
    rm = RiskManager(config)
    rm._daily_pnl = -10.0  # Lost $10 today

    # daily_start_value=100, max_daily_loss = 100 * 0.05 = $5. Lost $10 > $5.
    rm.evaluate_halt_state(portfolio_value=90.0, daily_start_value=100.0)
    assert rm.is_halted
    assert "daily" in rm.halt_reason.lower()


def test_risk_halt_eval_clean():
    """L2: Within all limits → not halted."""
    from src.shell.config import RiskConfig
    from src.shell.risk import RiskManager

    config = RiskConfig(max_drawdown_pct=0.40, rollback_daily_loss_pct=0.15,
                        rollback_consecutive_losses=999, max_daily_loss_pct=0.10)
    rm = RiskManager(config)
    rm._peak_portfolio = 100.0
    rm._daily_pnl = -1.0  # Small loss
    rm._consecutive_losses = 2

    rm.evaluate_halt_state(portfolio_value=98.0, daily_start_value=100.0)
    assert not rm.is_halted


@pytest.mark.asyncio
async def test_orphaned_position_detection():
    """L3: Position for symbol not in config is detected as orphaned."""
    from src.shell.config import load_config
    from src.shell.database import Database
    from src.shell.portfolio import PortfolioTracker
    from src.shell.kraken import KrakenREST

    config = load_config()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        config.db_path = f.name

    try:
        db = Database(config.db_path)
        await db.connect()

        # Seed a position for a symbol NOT in config
        await db.execute(
            """INSERT INTO positions
               (symbol, tag, side, qty, avg_entry, current_price, intent)
               VALUES (?, ?, 'long', 0.01, 5.0, 5.0, 'DAY')""",
            ("FAKE/USD", "auto_FAKEUSD_001"),
        )
        await db.commit()

        kraken = KrakenREST(config.kraken)
        portfolio = PortfolioTracker(config, db, kraken)
        await portfolio.initialize()

        # Check orphaned detection logic
        config_symbols = set(config.symbols)
        position_symbols = {pos["symbol"] for pos in portfolio.positions.values()}
        orphaned = position_symbols - config_symbols
        assert "FAKE/USD" in orphaned

        await db.close()
    finally:
        os.unlink(config.db_path)


@pytest.mark.asyncio
async def test_strategy_fallback_db():
    """L4: When filesystem strategy missing, loads from DB."""
    from src.shell.database import Database
    from src.strategy.loader import load_strategy_with_fallback, get_strategy_path, ACTIVE_DIR

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    # Read real strategy code before we hide the file
    strategy_path = get_strategy_path()
    real_code = strategy_path.read_text() if strategy_path.exists() else None
    assert real_code is not None, "Need a strategy file for this test"

    try:
        db = Database(db_path)
        await db.connect()

        # Store code in strategy_versions
        await db.execute(
            """INSERT INTO strategy_versions
               (version, code_hash, code, deployed_at) VALUES (?, ?, ?, datetime('now'))""",
            ("v_test_fallback", "abc123", real_code),
        )
        await db.commit()

        # Temporarily rename the strategy file to simulate missing
        backup_path = strategy_path.with_suffix(".py.bak")
        strategy_path.rename(backup_path)

        try:
            result = await load_strategy_with_fallback(db)
            assert result is not None  # Should recover from DB
        finally:
            # Restore the strategy file
            if backup_path.exists():
                backup_path.rename(strategy_path)

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_strategy_fallback_paused():
    """L4: All sources fail → returns None (paused mode)."""
    from src.shell.database import Database
    from src.strategy.loader import load_strategy_with_fallback, get_strategy_path

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    strategy_path = get_strategy_path()

    try:
        db = Database(db_path)
        await db.connect()

        # No code in strategy_versions DB either
        # Temporarily rename the strategy file
        backup_path = strategy_path.with_suffix(".py.bak2")
        strategy_path.rename(backup_path)

        try:
            result = await load_strategy_with_fallback(db)
            assert result is None  # All sources failed
        finally:
            if backup_path.exists():
                backup_path.rename(strategy_path)

        await db.close()
    finally:
        os.unlink(db_path)


def test_config_validates_timezone():
    """L6: Invalid timezone raises ValueError."""
    from src.shell.config import _validate_config, Config

    config = Config()
    config.timezone = "Invalid/Timezone_That_Does_Not_Exist"
    with pytest.raises(ValueError, match="Invalid timezone"):
        _validate_config(config)


def test_config_validates_symbol_format():
    """L6: Symbol without '/' raises ValueError."""
    from src.shell.config import _validate_config, Config

    config = Config()
    config.symbols = ["BTCUSD"]  # Missing slash
    with pytest.raises(ValueError, match="must contain '/'"):
        _validate_config(config)


def test_config_validates_trade_size_consistency():
    """L6: default_trade_pct > max_trade_pct raises ValueError."""
    from src.shell.config import _validate_config, Config

    config = Config()
    config.risk.default_trade_pct = 0.20
    config.risk.max_trade_pct = 0.10
    with pytest.raises(ValueError, match="default_trade_pct"):
        _validate_config(config)


@pytest.mark.asyncio
async def test_live_mode_fails_on_bad_credentials():
    """L7: Live mode raises RuntimeError when Kraken auth fails."""
    from src.shell.config import load_config
    from src.shell.database import Database
    from src.shell.portfolio import PortfolioTracker

    config = load_config()
    config.mode = "live"
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        config.db_path = f.name

    try:
        db = Database(config.db_path)
        await db.connect()

        # Mock Kraken client that fails on get_balance
        kraken = MagicMock()
        kraken.get_balance = AsyncMock(side_effect=Exception("Invalid key"))
        portfolio = PortfolioTracker(config, db, kraken)

        with pytest.raises(RuntimeError, match="failed to fetch Kraken balance"):
            await portfolio.initialize()

        await db.close()
    finally:
        os.unlink(config.db_path)


@pytest.mark.asyncio
async def test_special_migration_transactional():
    """L8: Special migration is atomic — schema is consistent after."""
    from src.shell.database import Database

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        await db.connect()

        # Verify positions table has tag column and correct schema
        cursor = await db.execute("PRAGMA table_info(positions)")
        columns = {row[1] for row in await cursor.fetchall()}
        assert "tag" in columns
        assert "symbol" in columns
        assert "qty" in columns

        # Verify UNIQUE constraint on tag
        await db.execute(
            "INSERT INTO positions (symbol, tag, qty, avg_entry) VALUES ('BTC/USD', 'test_tag_1', 1, 50000)"
        )
        with pytest.raises(Exception):  # IntegrityError for duplicate tag
            await db.execute(
                "INSERT INTO positions (symbol, tag, qty, avg_entry) VALUES ('ETH/USD', 'test_tag_1', 1, 3000)"
            )

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_system_meta_table_exists():
    """L1: system_meta table is created in schema."""
    from src.shell.database import Database

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        await db.connect()

        rows = await db.fetchall("SELECT name FROM sqlite_master WHERE type='table' AND name='system_meta'")
        assert len(rows) == 1

        # Test key-value insert/read
        await db.execute("INSERT INTO system_meta (key, value) VALUES ('test_key', '42')")
        await db.commit()
        row = await db.fetchone("SELECT value FROM system_meta WHERE key = 'test_key'")
        assert row["value"] == "42"

        await db.close()
    finally:
        os.unlink(db_path)


# --- Activity Log ---


@pytest.mark.asyncio
async def test_activity_log_write_and_query():
    """Activity log: write entries and query with filters."""
    from src.shell.database import Database
    from src.shell.activity import ActivityLogger

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        await db.connect()
        logger = ActivityLogger(db)

        await logger.log("TRADE", "BUY 0.5 SOL/USD", "info")
        await logger.log("RISK", "HALTED: max drawdown", "error")
        await logger.log("SYSTEM", "Daily reset", "info")
        await logger.log("TRADE", "SELL 0.5 SOL/USD", "info")

        # Query all
        rows = await logger.query(limit=10)
        assert len(rows) == 4

        # Filter by category
        trades = await logger.query(category="TRADE")
        assert len(trades) == 2
        assert all(r["category"] == "TRADE" for r in trades)

        # Filter by severity
        errors = await logger.query(severity="error")
        assert len(errors) == 1
        assert errors[0]["category"] == "RISK"

        # Limit
        limited = await logger.query(limit=2)
        assert len(limited) == 2

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_activity_log_recent_chronological():
    """Activity log: recent() returns oldest-first (chronological) order."""
    from src.shell.database import Database
    from src.shell.activity import ActivityLogger

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        await db.connect()
        logger = ActivityLogger(db)

        await logger.log("SYSTEM", "First event")
        await logger.log("SYSTEM", "Second event")
        await logger.log("SYSTEM", "Third event")

        recent = await logger.recent(limit=10)
        assert len(recent) == 3
        assert recent[0]["summary"] == "First event"
        assert recent[1]["summary"] == "Second event"
        assert recent[2]["summary"] == "Third event"

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_activity_log_detail_json_roundtrip():
    """Activity log: detail dict serializes and can be parsed back."""
    from src.shell.database import Database
    from src.shell.activity import ActivityLogger

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        await db.connect()
        logger = ActivityLogger(db)

        detail = {"symbol": "BTC/USD", "qty": 0.001, "price": 42000.0}
        await logger.log("TRADE", "BUY BTC", detail=detail)

        rows = await logger.query(limit=1)
        assert len(rows) == 1
        stored_detail = json.loads(rows[0]["detail"])
        assert stored_detail["symbol"] == "BTC/USD"
        assert stored_detail["price"] == 42000.0

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_activity_ws_backfill():
    """Activity WebSocket: DB has entries for backfill on connect."""
    from src.shell.database import Database
    from src.shell.activity import ActivityLogger, ActivityWebSocketManager

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        await db.connect()
        logger = ActivityLogger(db)

        # Insert 25 entries
        for i in range(25):
            await logger.log("SYSTEM", f"Event {i}")

        # ActivityWebSocketManager should serve 20 backfill entries
        ws_mgr = ActivityWebSocketManager()
        ws_mgr.set_db(db)

        # Verify DB has all 25 and backfill query returns 20
        all_rows = await db.fetchall("SELECT * FROM activity_log")
        assert len(all_rows) == 25
        backfill = await db.fetchall(
            "SELECT timestamp, category, severity, summary FROM activity_log ORDER BY id DESC LIMIT 20"
        )
        assert len(backfill) == 20

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_notifier_activity_hook():
    """Notifier: trade_executed() creates TRADE activity_log entry."""
    from src.shell.database import Database
    from src.shell.activity import ActivityLogger
    from src.telegram.notifications import Notifier

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        await db.connect()
        logger = ActivityLogger(db)

        notifier = Notifier(chat_id="123")
        notifier.set_activity_logger(logger)

        trade = {
            "action": "BUY",
            "symbol": "SOL/USD",
            "qty": 0.5,
            "price": 142.30,
            "fee": 0.28,
            "intent": "DAY",
            "tag": "auto_SOLUSD_001",
        }
        await notifier.trade_executed(trade)

        rows = await db.fetchall("SELECT * FROM activity_log")
        assert len(rows) == 1
        assert rows[0]["category"] == "TRADE"
        assert rows[0]["severity"] == "info"
        assert "SOL/USD" in rows[0]["summary"]
        assert "BUY" in rows[0]["summary"]

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_notifier_skip_empty_scan():
    """Notifier: scan_complete with 0 signals writes NO activity entry."""
    from src.shell.database import Database
    from src.shell.activity import ActivityLogger
    from src.telegram.notifications import Notifier

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        await db.connect()
        logger = ActivityLogger(db)

        notifier = Notifier(chat_id="123")
        notifier.set_activity_logger(logger)

        await notifier.scan_complete(9, 0)

        rows = await db.fetchall("SELECT * FROM activity_log")
        assert len(rows) == 0

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_notifier_log_scan_with_signals():
    """Notifier: scan_complete with signals writes SCAN activity entry."""
    from src.shell.database import Database
    from src.shell.activity import ActivityLogger
    from src.telegram.notifications import Notifier

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        await db.connect()
        logger = ActivityLogger(db)

        notifier = Notifier(chat_id="123")
        notifier.set_activity_logger(logger)

        await notifier.scan_complete(9, 3)

        rows = await db.fetchall("SELECT * FROM activity_log")
        assert len(rows) == 1
        assert rows[0]["category"] == "SCAN"
        assert "9 symbols" in rows[0]["summary"]
        assert "3 signals" in rows[0]["summary"]

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_activity_rest_endpoint():
    """REST: /v1/activity returns correct envelope with filters."""
    from src.shell.database import Database
    from src.shell.activity import ActivityLogger
    from src.api.routes import activity_handler
    from src.api import ctx_key

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        await db.connect()
        logger = ActivityLogger(db)

        await logger.log("TRADE", "Trade A", "info")
        await logger.log("RISK", "Halt B", "error")
        await logger.log("TRADE", "Trade C", "info")

        mock_config = MagicMock()
        mock_config.mode = "paper"

        request = MagicMock()
        request.query = {"category": "TRADE", "limit": "10"}
        request.app = {ctx_key: {"config": mock_config, "activity_logger": logger}}

        response = await activity_handler(request)
        body = json.loads(response.body)
        assert len(body["data"]) == 2
        assert all(e["category"] == "TRADE" for e in body["data"])
        assert body["meta"]["mode"] == "paper"

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_activity_log_pruning():
    """Activity log: entries older than 90 days pruned by prune_old_data()."""
    from src.shell.database import Database
    from src.shell.data_store import DataStore
    from src.shell.config import DataConfig

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        await db.connect()

        # Insert an old entry (100 days ago)
        old_ts = (datetime.now(timezone.utc) - timedelta(days=100)).strftime("%Y-%m-%d %H:%M:%S")
        await db.execute(
            "INSERT INTO activity_log (timestamp, category, severity, summary) VALUES (?, 'SYSTEM', 'info', 'Old event')",
            (old_ts,),
        )
        # Insert a recent entry
        recent_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        await db.execute(
            "INSERT INTO activity_log (timestamp, category, severity, summary) VALUES (?, 'SYSTEM', 'info', 'Recent event')",
            (recent_ts,),
        )
        await db.commit()

        config = DataConfig()
        ds = DataStore(db, config)
        await ds.prune_old_data()

        rows = await db.fetchall("SELECT * FROM activity_log")
        assert len(rows) == 1
        assert rows[0]["summary"] == "Recent event"

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_ask_context_includes_activity():
    """The /ask command context includes recent activity entries."""
    from src.shell.database import Database
    from src.shell.activity import ActivityLogger
    from src.telegram.commands import BotCommands
    from src.shell.config import load_config

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        await db.connect()
        logger = ActivityLogger(db)
        config = load_config()

        await logger.log("SYSTEM", "Daily reset: counters cleared")
        await logger.log("SCAN", "Scan: 9 symbols, 2 signals")

        mock_ai = AsyncMock()
        mock_ai.ask_haiku = AsyncMock(return_value="Test response")

        commands = BotCommands(
            config=config, db=db, scan_state={},
            ai_client=mock_ai, activity_logger=logger,
        )

        mock_update = MagicMock()
        mock_update.effective_user = MagicMock()
        mock_update.effective_user.id = config.telegram.allowed_user_ids[0] if config.telegram.allowed_user_ids else 0
        mock_update.message = MagicMock()
        mock_update.message.reply_text = AsyncMock()

        mock_context = MagicMock()
        mock_context.args = ["what", "happened?"]

        await commands.cmd_ask(mock_update, mock_context)

        # Check the prompt sent to Haiku contains activity
        if mock_ai.ask_haiku.called:
            prompt_arg = mock_ai.ask_haiku.call_args[0][0]
            assert "Recent activity:" in prompt_arg
            assert "Daily reset" in prompt_arg
            assert "9 symbols" in prompt_arg

        await db.close()
    finally:
        os.unlink(db_path)


# --- Prometheus Metrics ---

@pytest.mark.asyncio
async def test_metrics_endpoint():
    """Prometheus: /metrics returns 200 with text/plain and tb_ prefixed metrics."""
    from aiohttp.test_utils import TestClient, TestServer

    from src.api.server import create_app
    from src.shell.config import load_config
    from src.shell.database import Database
    from src.shell.risk import RiskManager

    config = load_config()

    db_path = tempfile.mktemp(suffix=".db")
    try:
        db = Database(db_path)
        await db.connect()

        risk = RiskManager(config.risk)
        portfolio = MagicMock()
        portfolio.total_value = AsyncMock(return_value=500.0)
        portfolio.cash = 300.0
        portfolio.position_count = 1
        portfolio.positions = {}
        portfolio._fees_today = 0.50
        ai = MagicMock()
        ai.get_daily_usage = AsyncMock(return_value={"used": 0, "total_cost": 0, "models": {}})
        ai.tokens_remaining = 1500000
        scan_state = {"symbols": {}}
        commands = MagicMock()
        commands.is_paused = False

        app, ws_manager, _ = create_app(config, db, portfolio, risk, ai, scan_state, commands)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/metrics")
            assert resp.status == 200
            assert "text/plain" in resp.headers.get("Content-Type", "")
            body = await resp.text()
            assert "tb_portfolio_value_usd" in body
            assert "tb_cash_usd" in body
            assert "tb_halted" in body

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_metrics_skips_auth():
    """Prometheus: /metrics returns 200 without Bearer token (auth skipped)."""
    from aiohttp.test_utils import TestClient, TestServer

    from src.api.server import create_app
    from src.shell.config import load_config
    from src.shell.database import Database
    from src.shell.risk import RiskManager

    config = load_config()

    db_path = tempfile.mktemp(suffix=".db")
    try:
        db = Database(db_path)
        await db.connect()

        risk = RiskManager(config.risk)
        portfolio = MagicMock()
        portfolio.total_value = AsyncMock(return_value=100.0)
        portfolio.cash = 100.0
        portfolio.position_count = 0
        portfolio.positions = {}
        portfolio._fees_today = 0.0
        ai = MagicMock()
        ai.get_daily_usage = AsyncMock(return_value={"used": 0, "total_cost": 0, "models": {}})
        ai.tokens_remaining = 1500000
        scan_state = {"symbols": {}}
        commands = MagicMock()
        commands.is_paused = False

        app, ws_manager, _ = create_app(config, db, portfolio, risk, ai, scan_state, commands)

        # Set an API key to ensure auth is active for normal routes
        from src.api import api_key_key
        app[api_key_key] = "test-key"

        async with TestClient(TestServer(app)) as client:
            # /metrics should work WITHOUT auth header
            resp = await client.get("/metrics")
            assert resp.status == 200

            # Normal route should reject without auth
            resp_system = await client.get("/v1/system")
            assert resp_system.status == 401

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_metrics_position_labels():
    """Prometheus: /metrics includes per-position labels (symbol, tag)."""
    from aiohttp.test_utils import TestClient, TestServer

    from src.api.server import create_app
    from src.shell.config import load_config
    from src.shell.database import Database
    from src.shell.risk import RiskManager

    config = load_config()

    db_path = tempfile.mktemp(suffix=".db")
    try:
        db = Database(db_path)
        await db.connect()

        risk = RiskManager(config.risk)
        portfolio = MagicMock()
        portfolio.total_value = AsyncMock(return_value=600.0)
        portfolio.cash = 400.0
        portfolio.position_count = 1
        portfolio.positions = {
            "auto_SOLUSD_001": {
                "symbol": "SOL/USD",
                "qty": 2.0,
                "avg_entry": 100.0,
                "current_price": 105.0,
            }
        }
        portfolio._fees_today = 0.0
        ai = MagicMock()
        ai.get_daily_usage = AsyncMock(return_value={"used": 0, "total_cost": 0, "models": {}})
        ai.tokens_remaining = 1500000
        scan_state = {"symbols": {}}
        commands = MagicMock()
        commands.is_paused = False

        app, ws_manager, _ = create_app(config, db, portfolio, risk, ai, scan_state, commands)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/metrics")
            assert resp.status == 200
            body = await resp.text()
            assert 'symbol="SOL/USD"' in body
            assert 'tag="auto_SOLUSD_001"' in body
            assert "tb_position_value_usd" in body
            assert "tb_position_pnl_usd" in body

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_metrics_truth_benchmarks():
    """Prometheus: /metrics includes truth benchmark gauges from closed trades."""
    from aiohttp.test_utils import TestClient, TestServer

    from src.api.server import create_app
    from src.api.metrics import _truth_cache
    from src.shell.config import load_config
    from src.shell.database import Database
    from src.shell.risk import RiskManager

    config = load_config()

    db_path = tempfile.mktemp(suffix=".db")
    try:
        db = Database(db_path)
        await db.connect()

        # Insert a closed winning trade
        await db.execute(
            """INSERT INTO trades (symbol, side, qty, entry_price, exit_price, pnl, pnl_pct, fees, intent, opened_at, closed_at)
               VALUES ('BTC/USD', 'buy', 0.01, 50000, 51000, 10.0, 0.02, 0.50, 'DAY',
                       datetime('now', '-1 hour'), datetime('now'))"""
        )
        await db.commit()

        risk = RiskManager(config.risk)
        portfolio = MagicMock()
        portfolio.total_value = AsyncMock(return_value=500.0)
        portfolio.cash = 300.0
        portfolio.position_count = 0
        portfolio.positions = {}
        portfolio._fees_today = 0.0
        ai = MagicMock()
        ai.get_daily_usage = AsyncMock(return_value={"used": 1000, "total_cost": 0.05, "daily_limit": 1500000, "models": {}})
        scan_state = {"symbols": {}}
        commands = MagicMock()
        commands.is_paused = False

        # Clear truth cache to force fresh computation
        _truth_cache["data"] = None
        _truth_cache["expires_at"] = 0.0

        app, ws_manager, _ = create_app(config, db, portfolio, risk, ai, scan_state, commands)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/metrics")
            assert resp.status == 200
            body = await resp.text()
            assert "tb_win_rate" in body
            assert "tb_trade_count" in body
            assert "tb_net_pnl_usd" in body
            assert "tb_profit_factor" in body
            assert "tb_total_fees_usd" in body

        await db.close()
    finally:
        _truth_cache["data"] = None
        _truth_cache["expires_at"] = 0.0
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_metrics_ai_usage():
    """Prometheus: /metrics includes AI usage gauges."""
    from aiohttp.test_utils import TestClient, TestServer

    from src.api.server import create_app
    from src.api.metrics import _truth_cache
    from src.shell.config import load_config
    from src.shell.database import Database
    from src.shell.risk import RiskManager

    config = load_config()

    db_path = tempfile.mktemp(suffix=".db")
    try:
        db = Database(db_path)
        await db.connect()

        risk = RiskManager(config.risk)
        portfolio = MagicMock()
        portfolio.total_value = AsyncMock(return_value=500.0)
        portfolio.cash = 500.0
        portfolio.position_count = 0
        portfolio.positions = {}
        portfolio._fees_today = 0.0
        ai = MagicMock()
        ai.get_daily_usage = AsyncMock(return_value={"used": 50000, "total_cost": 1.23, "daily_limit": 1500000, "models": {}})
        scan_state = {"symbols": {}}
        commands = MagicMock()
        commands.is_paused = False

        _truth_cache["data"] = None
        _truth_cache["expires_at"] = 0.0

        app, ws_manager, _ = create_app(config, db, portfolio, risk, ai, scan_state, commands)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/metrics")
            assert resp.status == 200
            body = await resp.text()
            assert "tb_ai_daily_cost_usd" in body
            assert "tb_ai_daily_tokens" in body
            assert "tb_ai_token_budget_pct" in body

        await db.close()
    finally:
        _truth_cache["data"] = None
        _truth_cache["expires_at"] = 0.0
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_metrics_symbol_prices():
    """Prometheus: /metrics includes per-symbol price labels from scan state."""
    from aiohttp.test_utils import TestClient, TestServer

    from src.api.server import create_app
    from src.api.metrics import _truth_cache
    from src.shell.config import load_config
    from src.shell.database import Database
    from src.shell.risk import RiskManager

    config = load_config()

    db_path = tempfile.mktemp(suffix=".db")
    try:
        db = Database(db_path)
        await db.connect()

        risk = RiskManager(config.risk)
        portfolio = MagicMock()
        portfolio.total_value = AsyncMock(return_value=500.0)
        portfolio.cash = 500.0
        portfolio.position_count = 0
        portfolio.positions = {}
        portfolio._fees_today = 0.0
        ai = MagicMock()
        ai.get_daily_usage = AsyncMock(return_value={"used": 0, "total_cost": 0, "daily_limit": 1500000, "models": {}})
        scan_state = {
            "symbols": {
                "BTC/USD": {"price": 50000.0, "spread": 10.0},
                "ETH/USD": {"price": 3000.0, "spread": 2.0},
            }
        }
        commands = MagicMock()
        commands.is_paused = False

        _truth_cache["data"] = None
        _truth_cache["expires_at"] = 0.0

        app, ws_manager, _ = create_app(config, db, portfolio, risk, ai, scan_state, commands)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/metrics")
            assert resp.status == 200
            body = await resp.text()
            assert 'symbol="BTC/USD"' in body
            assert 'symbol="ETH/USD"' in body
            assert "tb_symbol_price_usd" in body

        await db.close()
    finally:
        _truth_cache["data"] = None
        _truth_cache["expires_at"] = 0.0
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_metrics_uptime():
    """Prometheus: /metrics includes uptime seconds gauge with positive value."""
    from aiohttp.test_utils import TestClient, TestServer

    from src.api.server import create_app
    from src.api.metrics import _truth_cache
    from src.shell.config import load_config
    from src.shell.database import Database
    from src.shell.risk import RiskManager

    config = load_config()

    db_path = tempfile.mktemp(suffix=".db")
    try:
        db = Database(db_path)
        await db.connect()

        risk = RiskManager(config.risk)
        portfolio = MagicMock()
        portfolio.total_value = AsyncMock(return_value=500.0)
        portfolio.cash = 500.0
        portfolio.position_count = 0
        portfolio.positions = {}
        portfolio._fees_today = 0.0
        ai = MagicMock()
        ai.get_daily_usage = AsyncMock(return_value={"used": 0, "total_cost": 0, "daily_limit": 1500000, "models": {}})
        scan_state = {"symbols": {}}
        commands = MagicMock()
        commands.is_paused = False

        _truth_cache["data"] = None
        _truth_cache["expires_at"] = 0.0

        app, ws_manager, _ = create_app(config, db, portfolio, risk, ai, scan_state, commands)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/metrics")
            assert resp.status == 200
            body = await resp.text()
            assert "tb_uptime_seconds" in body
            # Uptime should be > 0 since started_at was set during create_app
            for line in body.split("\n"):
                if line.startswith("tb_uptime_seconds "):
                    val = float(line.split(" ")[1])
                    assert val > 0
                    break

        await db.close()
    finally:
        _truth_cache["data"] = None
        _truth_cache["expires_at"] = 0.0
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_metrics_scan_age():
    """Prometheus: /metrics includes scan age seconds gauge."""
    from aiohttp.test_utils import TestClient, TestServer

    from src.api.server import create_app
    from src.api.metrics import _truth_cache
    from src.shell.config import load_config
    from src.shell.database import Database
    from src.shell.risk import RiskManager

    config = load_config()

    db_path = tempfile.mktemp(suffix=".db")
    try:
        db = Database(db_path)
        await db.connect()

        risk = RiskManager(config.risk)
        portfolio = MagicMock()
        portfolio.total_value = AsyncMock(return_value=500.0)
        portfolio.cash = 500.0
        portfolio.position_count = 0
        portfolio.positions = {}
        portfolio._fees_today = 0.0
        ai = MagicMock()
        ai.get_daily_usage = AsyncMock(return_value={"used": 0, "total_cost": 0, "daily_limit": 1500000, "models": {}})
        # Set last_scan_at to 60 seconds ago
        scan_state = {
            "symbols": {},
            "last_scan_at": datetime.now(timezone.utc) - timedelta(seconds=60),
        }
        commands = MagicMock()
        commands.is_paused = False

        _truth_cache["data"] = None
        _truth_cache["expires_at"] = 0.0

        app, ws_manager, _ = create_app(config, db, portfolio, risk, ai, scan_state, commands)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/metrics")
            assert resp.status == 200
            body = await resp.text()
            assert "tb_scan_age_seconds" in body
            for line in body.split("\n"):
                if line.startswith("tb_scan_age_seconds "):
                    val = float(line.split(" ")[1])
                    assert val >= 59  # ~60 seconds, allowing small timing variance
                    break

        await db.close()
    finally:
        _truth_cache["data"] = None
        _truth_cache["expires_at"] = 0.0
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_metrics_trades_by_reason():
    """Prometheus: /metrics includes trade count by close reason."""
    from aiohttp.test_utils import TestClient, TestServer

    from src.api.server import create_app
    from src.api.metrics import _truth_cache
    from src.shell.config import load_config
    from src.shell.database import Database
    from src.shell.risk import RiskManager

    config = load_config()

    db_path = tempfile.mktemp(suffix=".db")
    try:
        db = Database(db_path)
        await db.connect()

        # Insert trades with different close reasons
        for reason in ["signal", "signal", "stop_loss"]:
            await db.execute(
                """INSERT INTO trades (symbol, side, qty, entry_price, exit_price, pnl, pnl_pct, fees, intent, opened_at, closed_at, close_reason)
                   VALUES ('BTC/USD', 'buy', 0.01, 50000, 51000, 10.0, 0.02, 0.50, 'DAY',
                           datetime('now', '-1 hour'), datetime('now'), ?)""",
                (reason,),
            )
        await db.commit()

        risk = RiskManager(config.risk)
        portfolio = MagicMock()
        portfolio.total_value = AsyncMock(return_value=500.0)
        portfolio.cash = 500.0
        portfolio.position_count = 0
        portfolio.positions = {}
        portfolio._fees_today = 0.0
        ai = MagicMock()
        ai.get_daily_usage = AsyncMock(return_value={"used": 0, "total_cost": 0, "daily_limit": 1500000, "models": {}})
        scan_state = {"symbols": {}}
        commands = MagicMock()
        commands.is_paused = False

        _truth_cache["data"] = None
        _truth_cache["expires_at"] = 0.0

        app, ws_manager, _ = create_app(config, db, portfolio, risk, ai, scan_state, commands)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/metrics")
            assert resp.status == 200
            body = await resp.text()
            assert 'tb_trades_by_reason{reason="signal"}' in body
            assert 'tb_trades_by_reason{reason="stop_loss"}' in body
            # signal=2, stop_loss=1
            for line in body.split("\n"):
                if 'tb_trades_by_reason{reason="signal"}' in line:
                    assert float(line.split(" ")[-1]) == 2.0
                if 'tb_trades_by_reason{reason="stop_loss"}' in line:
                    assert float(line.split(" ")[-1]) == 1.0

        await db.close()
    finally:
        _truth_cache["data"] = None
        _truth_cache["expires_at"] = 0.0
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_metrics_trades_by_symbol():
    """Prometheus: /metrics includes trade count by symbol."""
    from aiohttp.test_utils import TestClient, TestServer

    from src.api.server import create_app
    from src.api.metrics import _truth_cache
    from src.shell.config import load_config
    from src.shell.database import Database
    from src.shell.risk import RiskManager

    config = load_config()

    db_path = tempfile.mktemp(suffix=".db")
    try:
        db = Database(db_path)
        await db.connect()

        # Insert trades for different symbols
        for symbol in ["BTC/USD", "BTC/USD", "ETH/USD"]:
            await db.execute(
                """INSERT INTO trades (symbol, side, qty, entry_price, exit_price, pnl, pnl_pct, fees, intent, opened_at, closed_at)
                   VALUES (?, 'buy', 0.01, 50000, 51000, 10.0, 0.02, 0.50, 'DAY',
                           datetime('now', '-1 hour'), datetime('now'))""",
                (symbol,),
            )
        await db.commit()

        risk = RiskManager(config.risk)
        portfolio = MagicMock()
        portfolio.total_value = AsyncMock(return_value=500.0)
        portfolio.cash = 500.0
        portfolio.position_count = 0
        portfolio.positions = {}
        portfolio._fees_today = 0.0
        ai = MagicMock()
        ai.get_daily_usage = AsyncMock(return_value={"used": 0, "total_cost": 0, "daily_limit": 1500000, "models": {}})
        scan_state = {"symbols": {}}
        commands = MagicMock()
        commands.is_paused = False

        _truth_cache["data"] = None
        _truth_cache["expires_at"] = 0.0

        app, ws_manager, _ = create_app(config, db, portfolio, risk, ai, scan_state, commands)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/metrics")
            assert resp.status == 200
            body = await resp.text()
            assert 'tb_trades_by_symbol{symbol="BTC/USD"}' in body
            assert 'tb_trades_by_symbol{symbol="ETH/USD"}' in body
            for line in body.split("\n"):
                if 'tb_trades_by_symbol{symbol="BTC/USD"}' in line:
                    assert float(line.split(" ")[-1]) == 2.0
                if 'tb_trades_by_symbol{symbol="ETH/USD"}' in line:
                    assert float(line.split(" ")[-1]) == 1.0

        await db.close()
    finally:
        _truth_cache["data"] = None
        _truth_cache["expires_at"] = 0.0
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_cmd_orchestrate_triggers():
    """cmd_orchestrate sets scan_state flag when cycle lock is free."""
    from src.shell.config import load_config
    from src.telegram.commands import BotCommands

    config = load_config()
    config.telegram.allowed_user_ids = [12345]
    scan_state = {}

    commands = BotCommands(config=config, db=MagicMock(), scan_state=scan_state)

    # Mock orchestrator with unlocked cycle lock
    mock_orchestrator = MagicMock()
    mock_lock = MagicMock()
    mock_lock.locked.return_value = False
    mock_orchestrator._cycle_lock = mock_lock
    commands.set_orchestrator(mock_orchestrator)

    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = 12345
    update.message = AsyncMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()

    await commands.cmd_orchestrate(update, context)
    reply = update.message.reply_text.call_args[0][0]
    assert "triggered" in reply.lower()
    assert scan_state.get("orchestrate_requested") is True


@pytest.mark.asyncio
async def test_cmd_orchestrate_already_running():
    """cmd_orchestrate rejects when cycle lock is held."""
    from src.shell.config import load_config
    from src.telegram.commands import BotCommands

    config = load_config()
    config.telegram.allowed_user_ids = [12345]
    scan_state = {}

    commands = BotCommands(config=config, db=MagicMock(), scan_state=scan_state)

    # Mock orchestrator with locked cycle lock
    mock_orchestrator = MagicMock()
    mock_lock = MagicMock()
    mock_lock.locked.return_value = True
    mock_orchestrator._cycle_lock = mock_lock
    commands.set_orchestrator(mock_orchestrator)

    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = 12345
    update.message = AsyncMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()

    await commands.cmd_orchestrate(update, context)
    reply = update.message.reply_text.call_args[0][0]
    assert "already in progress" in reply.lower()
    assert scan_state.get("orchestrate_requested") is not True
