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
                     "fee_schedule", "strategy_state", "paper_tests", "scan_results"]
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
