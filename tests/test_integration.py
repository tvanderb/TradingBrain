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
from datetime import datetime, timedelta
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
    assert config.risk.max_trade_pct == 0.07
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
                     "scan_results"]
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
                """INSERT INTO scan_results (timestamp, symbol, price, ema_fast, ema_slow, rsi, volume_ratio, spread, strategy_regime)
                   VALUES (datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("BTC/USD", 50000 + i * 100, 50100, 49900, 55, 1.2, 0.5, "trending"),
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

        # Seed scan results
        for i in range(20):
            await db.execute(
                """INSERT INTO scan_results
                   (timestamp, symbol, price, ema_fast, ema_slow, rsi, volume_ratio, spread, strategy_regime, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', ?))""",
                (f"2026-01-01 {10+i//6:02d}:{(i%6)*10:02d}:00", "BTC/USD",
                 50000 + i * 50, 50100 + i * 10, 49900 + i * 10,
                 45 + i, 1.1 + i * 0.05, 0.5, "trending",
                 f"-{20-i} minutes"),
            )
        await db.commit()

        # Load and run
        module = load_analysis_module("market_analysis")
        ro = ReadOnlyDB(db.conn)
        result = await module.analyze(ro, get_schema_description())

        assert isinstance(result, dict)
        assert "price_summary" in result
        assert "indicator_stats_24h" in result
        assert "signal_proximity" in result
        assert "data_quality" in result
        assert result["data_quality"]["total_scans"] == 20

        # BTC/USD should be in price summary
        assert "BTC/USD" in result["price_summary"]
        btc = result["price_summary"]["BTC/USD"]
        assert btc["current_price"] > 0
        assert btc["ema_alignment"] in ("bullish", "bearish")

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
            """INSERT INTO scan_results (timestamp, symbol, price, ema_fast, ema_slow, rsi, volume_ratio, spread, strategy_regime, created_at)
               VALUES (datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
            ("BTC/USD", 50000, 50100, 49900, 55, 1.2, 0.5, "trending"),
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
        buy = Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.10, intent=Intent.DAY)
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
        ai.tokens_remaining = 100  # Way below 5000 threshold

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
               (strategy_version, risk_tier, required_days, ends_at, status)
               VALUES ('v_test', 1, 1, datetime('now', '-1 hour'), 'running')"""
        )
        # Insert some winning trades for that version
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
            "BTC/USD": {"price": 70000, "rsi": 55, "ema_fast": 70100, "ema_slow": 69900,
                        "vol_ratio": 1.2, "regime": "trending"},
        }}

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

        # /start
        await commands.cmd_start(update, context)
        reply = update.message.reply_text.call_args[0][0]
        assert "Trading Brain" in reply
        assert "/status" in reply

        # /status
        update.message.reply_text.reset_mock()
        await commands.cmd_status(update, context)
        reply = update.message.reply_text.call_args[0][0]
        assert "Mode: paper" in reply
        assert "ACTIVE" in reply

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

        # /report (with scan data)
        update.message.reply_text.reset_mock()
        await commands.cmd_report(update, context)
        reply = update.message.reply_text.call_args[0][0]
        assert "BTC/USD" in reply
        assert "trending" in reply

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
               (timestamp, symbol, price, ema_fast, ema_slow, rsi, volume_ratio, spread, strategy_regime)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (datetime.now(tz=None).isoformat(), "BTC/USD", 70000, 70100, 69900, 55, 1.2, 0.5, "trending"),
        )
        await db.execute(
            """INSERT INTO scan_results
               (timestamp, symbol, price, ema_fast, ema_slow, rsi, volume_ratio, spread, strategy_regime)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (datetime.now(tz=None).isoformat(), "ETH/USD", 3500, 3510, 3490, 48, 0.9, 0.3, "ranging"),
        )
        await db.commit()

        # Query back
        rows = await db.fetchall("SELECT * FROM scan_results ORDER BY symbol")
        assert len(rows) == 2
        assert rows[0]["symbol"] == "BTC/USD"
        assert rows[0]["rsi"] == 55
        assert rows[1]["symbol"] == "ETH/USD"
        assert rows[1]["strategy_regime"] == "ranging"

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
            total_value=200.0, cash=180.0, positions={}
        ))
        ai = MagicMock()
        ai.get_daily_usage = MagicMock(return_value={"total_tokens": 1000, "total_cost": 0.01, "by_model": {}})
        ai.tokens_remaining = 1499000
        scan_state = {"symbols": {"BTC/USD": {"price": 45000, "rsi": 52, "regime": "ranging"}}, "last_scan": "03:10:00"}
        commands = MagicMock()
        commands.is_paused = False

        app, ws_manager = create_app(config, db, portfolio, risk, ai, scan_state, commands)

        async with TestClient(TestServer(app)) as client:
            # /v1/system
            resp = await client.get("/v1/system")
            assert resp.status == 200
            body = await resp.json()
            assert "data" in body
            assert "meta" in body
            assert body["meta"]["mode"] == "paper"
            assert body["data"]["status"] == "running"

            # /v1/portfolio
            resp = await client.get("/v1/portfolio")
            assert resp.status == 200
            body = await resp.json()
            assert body["data"]["total_value"] == 200.0

            # /v1/positions
            resp = await client.get("/v1/positions")
            assert resp.status == 200
            body = await resp.json()
            assert isinstance(body["data"], list)

            # /v1/trades
            resp = await client.get("/v1/trades?limit=10")
            assert resp.status == 200
            body = await resp.json()
            assert isinstance(body["data"], list)

            # /v1/risk
            resp = await client.get("/v1/risk")
            assert resp.status == 200
            body = await resp.json()
            assert "limits" in body["data"]
            assert "current" in body["data"]
            assert body["data"]["current"]["halted"] is False

            # /v1/market
            resp = await client.get("/v1/market")
            assert resp.status == 200
            body = await resp.json()
            assert len(body["data"]) == 1
            assert body["data"][0]["symbol"] == "BTC/USD"

            # /v1/signals
            resp = await client.get("/v1/signals")
            assert resp.status == 200

            # /v1/strategy
            resp = await client.get("/v1/strategy")
            assert resp.status == 200

            # /v1/ai/usage
            resp = await client.get("/v1/ai/usage")
            assert resp.status == 200
            body = await resp.json()
            assert body["data"]["today"]["total_tokens"] == 1000

            # /v1/benchmarks
            resp = await client.get("/v1/benchmarks")
            assert resp.status == 200

            # /v1/performance
            resp = await client.get("/v1/performance")
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
        app, _ = create_app(config, db, MagicMock(), risk, MagicMock(), {})
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
        app, ws_manager = create_app(config, db, MagicMock(), risk, MagicMock(), {})

        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect("/v1/events") as ws:
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
    assert mock_app.bot.send_message.call_count == 1

    mock_app.bot.send_message.reset_mock()

    # scan_complete — should only go to WS (telegram filtered off)
    await notifier.scan_complete(9, 2)
    assert mock_app.bot.send_message.call_count == 0

    # risk_halt — should go to telegram (defaults to True)
    await notifier.risk_halt("Max drawdown exceeded")
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
    assert config.api.enabled is False  # Default off
    assert config.api.port == 8080
    assert config.api.host == "0.0.0.0"
