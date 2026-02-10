"""Strategy Sandbox — validates strategy code before deployment.

Tests that strategy code:
1. Parses without syntax errors
2. Defines a Strategy class inheriting from StrategyBase
3. Implements required methods (initialize, analyze)
4. Doesn't import forbidden modules (subprocess, os.system, etc.)
5. Runs analyze() without crashing on sample data
"""

from __future__ import annotations

import ast
import importlib.util
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import structlog

from src.shell.contract import (
    Action, Intent, OrderType, RiskLimits, Signal, StrategyBase, SymbolData, Portfolio,
    OpenPosition, ClosedTrade,
)

log = structlog.get_logger()

FORBIDDEN_IMPORTS = {
    "subprocess", "os", "shutil", "socket", "http",
    "urllib", "requests", "httpx", "websockets", "aiohttp",
    "sqlite3", "aiosqlite", "pathlib",
    "sys", "builtins", "ctypes", "importlib", "types",
    "code", "codeop", "runpy", "pkgutil",
    "threading", "multiprocessing", "pickle", "shelve", "marshal",
    "io", "tempfile", "gc", "inspect", "atexit", "signal",
}

FORBIDDEN_ATTRS = {"os.system", "os.popen", "os.exec", "os.environ", "os.path"}

FORBIDDEN_CALLS = {"eval", "exec", "__import__", "open", "compile"}


@dataclass
class SandboxResult:
    passed: bool
    errors: list[str]
    warnings: list[str]


def _get_dotted_name(node: ast.expr) -> str | None:
    """Reconstruct dotted attribute name from AST node (e.g., os.system)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _get_dotted_name(node.value)
        if parent:
            return f"{parent}.{node.attr}"
    return None


def check_imports(code: str) -> list[str]:
    """Check for forbidden imports, calls, and attribute access in strategy code."""
    errors = []
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"Syntax error: {e}"]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in FORBIDDEN_IMPORTS:
                    errors.append(f"Forbidden import: {alias.name}")

        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root = node.module.split(".")[0]
                if root in FORBIDDEN_IMPORTS:
                    errors.append(f"Forbidden import: from {node.module}")

        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
                errors.append(f"Forbidden function call: {node.func.id}()")
            # Check dotted attribute calls like os.system()
            elif isinstance(node.func, ast.Attribute):
                dotted = _get_dotted_name(node.func)
                if dotted and dotted in FORBIDDEN_ATTRS:
                    errors.append(f"Forbidden attribute call: {dotted}()")

    return errors


def _make_sample_data() -> tuple[dict[str, SymbolData], Portfolio, RiskLimits]:
    """Create sample data for sandbox testing."""
    symbols = [
        "BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "DOGE/USD",
        "ADA/USD", "LINK/USD", "AVAX/USD", "DOT/USD",
    ]
    _base_prices = {
        "BTC/USD": 70000, "ETH/USD": 2000, "SOL/USD": 80,
        "XRP/USD": 0.50, "DOGE/USD": 0.15, "ADA/USD": 0.40,
        "LINK/USD": 15.0, "AVAX/USD": 25.0, "DOT/USD": 5.0,
    }
    markets = {}

    for sym in symbols:
        base_price = _base_prices[sym]
        n = 100
        dates = pd.date_range(end=datetime.now(), periods=n, freq="5min")
        prices = base_price + np.random.randn(n).cumsum() * (base_price * 0.001)
        df = pd.DataFrame({
            "open": prices,
            "high": prices * 1.001,
            "low": prices * 0.999,
            "close": prices,
            "volume": np.random.uniform(100, 1000, n),
        }, index=dates)

        markets[sym] = SymbolData(
            symbol=sym,
            current_price=float(prices[-1]),
            candles_5m=df,
            candles_1h=df.resample("1h").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna(),
            candles_1d=df.resample("1D").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna(),
            spread=0.001,
            volume_24h=1000000,
        )

    portfolio = Portfolio(
        cash=200.0,
        total_value=200.0,
        positions=[],
        recent_trades=[],
        daily_pnl=0.0,
        total_pnl=0.0,
        fees_today=0.0,
    )

    risk_limits = RiskLimits(
        max_trade_pct=0.05,
        default_trade_pct=0.02,
        max_positions=5,
        max_daily_loss_pct=0.03,
        max_drawdown_pct=0.10,
    )

    return markets, portfolio, risk_limits


def validate_strategy(code: str) -> SandboxResult:
    """Full validation of strategy code in a sandbox."""
    errors = []
    warnings = []

    # Step 1: Check syntax
    try:
        ast.parse(code)
    except SyntaxError as e:
        return SandboxResult(False, [f"Syntax error at line {e.lineno}: {e.msg}"], [])

    # Step 2: Check forbidden imports
    import_errors = check_imports(code)
    if import_errors:
        return SandboxResult(False, import_errors, [])

    # Step 3: Load the module in a temp file
    tmp_path = None
    module_name = "sandbox_test_strategy"
    try:
        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
            f.write(code)
            tmp_path = f.name
        if module_name in sys.modules:
            del sys.modules[module_name]

        spec = importlib.util.spec_from_file_location(module_name, tmp_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        # Step 4: Check Strategy class exists
        strategy_cls = getattr(module, "Strategy", None)
        if strategy_cls is None:
            return SandboxResult(False, ["No 'Strategy' class found"], [])

        strategy = strategy_cls()
        if not isinstance(strategy, StrategyBase):
            return SandboxResult(False, ["Strategy must inherit from StrategyBase"], [])

        # Step 5: Test initialize()
        markets, portfolio, risk_limits = _make_sample_data()
        strategy.initialize(risk_limits, list(markets.keys()))

        # Step 6: Test analyze()
        signals = strategy.analyze(markets, portfolio, datetime.now())

        if not isinstance(signals, list):
            errors.append(f"analyze() must return list, got {type(signals).__name__}")
        else:
            for i, sig in enumerate(signals):
                if not isinstance(sig, Signal):
                    errors.append(f"Signal {i} is {type(sig).__name__}, expected Signal")
                elif sig.size_pct < 0 or sig.size_pct > 1:
                    warnings.append(f"Signal {i} size_pct={sig.size_pct} outside 0-1 range")

        # Step 7: Test get_state / load_state
        state = strategy.get_state()
        if not isinstance(state, dict):
            warnings.append(f"get_state() returned {type(state).__name__}, expected dict")
        else:
            strategy.load_state(state)

        # Step 8: Test scan_interval_minutes
        interval = strategy.scan_interval_minutes
        if not isinstance(interval, int) or interval < 1:
            warnings.append(f"scan_interval_minutes={interval} is invalid, must be positive int")

    except Exception as e:
        errors.append(f"Runtime error: {type(e).__name__}: {e}")
    finally:
        # Cleanup
        if module_name in sys.modules:
            del sys.modules[module_name]
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)

    if errors:
        return SandboxResult(False, errors, warnings)

    log.info("sandbox.passed", warnings=len(warnings))
    return SandboxResult(True, [], warnings)
