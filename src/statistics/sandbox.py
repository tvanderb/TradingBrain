"""Analysis Sandbox — validates analysis module code before deployment.

Different rules from strategy sandbox:
- ALLOWS: read-only DB access, scipy, statistics imports
- BLOCKS: DB writes, network, filesystem, subprocess, eval/exec

Tests that analysis code:
1. Parses without syntax errors
2. Defines an Analysis class inheriting from AnalysisBase
3. Implements analyze(db, schema) -> dict
4. Doesn't import forbidden modules
5. Runs analyze() without crashing on a test DB
"""

from __future__ import annotations

import asyncio
import ast
import importlib.util
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import aiosqlite
import structlog

from src.shell.contract import AnalysisBase
from src.shell.database import SCHEMA
from src.statistics.readonly_db import ReadOnlyDB, get_schema_description

log = structlog.get_logger()

# Forbidden for analysis modules (no network, no subprocess, no filesystem writes)
FORBIDDEN_IMPORTS = {
    "subprocess", "shutil", "socket", "http",
    "urllib", "requests", "httpx", "websockets", "aiohttp",
}

# These are allowed for analysis (unlike strategy sandbox which blocks them)
# - os: blocked (could write files)
# - sqlite3/aiosqlite: blocked (should use ReadOnlyDB, not raw connections)
# - pathlib: blocked (filesystem access)
FORBIDDEN_IMPORTS.update({
    "os", "sqlite3", "aiosqlite", "pathlib",
    "sys", "builtins", "ctypes", "importlib", "types",
    "code", "codeop", "runpy", "pkgutil",
    "threading", "multiprocessing", "pickle", "shelve", "marshal",
    "io", "tempfile", "gc", "inspect", "atexit", "signal",
})

FORBIDDEN_CALLS = {"eval", "exec", "__import__", "open", "compile", "print", "getattr", "setattr", "delattr", "globals", "vars", "dir"}

FORBIDDEN_ATTRS = {"os.system", "os.popen", "os.exec", "os.environ", "os.path"}

FORBIDDEN_DUNDERS = {"__builtins__", "__import__", "__class__", "__subclasses__", "__bases__", "__mro__", "__globals__", "__code__"}


@dataclass
class AnalysisSandboxResult:
    passed: bool
    errors: list[str]
    warnings: list[str]


def check_analysis_imports(code: str) -> list[str]:
    """Check for forbidden imports in analysis module code."""
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
            elif isinstance(node.func, ast.Attribute):
                dotted = _get_dotted_name(node.func)
                if dotted and dotted in FORBIDDEN_ATTRS:
                    errors.append(f"Forbidden attribute call: {dotted}()")

        # Block access to dangerous dunder attributes
        elif isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_DUNDERS:
                errors.append(f"Forbidden dunder access: .{node.attr}")

    return errors


def _get_dotted_name(node) -> str | None:
    """Reconstruct dotted attribute name from AST node (e.g., os.system)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _get_dotted_name(node.value)
        if parent:
            return f"{parent}.{node.attr}"
    return None


def validate_analysis_module(code: str, module_name: str) -> AnalysisSandboxResult:
    """Full validation of analysis module code in a sandbox.

    Args:
        code: The Python source code to validate
        module_name: 'market_analysis' or 'trade_performance' (for logging)
    """
    errors = []
    warnings = []
    tmp_path = None
    sys_module_name = f"sandbox_test_{module_name}"

    # Step 1: Check syntax
    try:
        ast.parse(code)
    except SyntaxError as e:
        return AnalysisSandboxResult(False, [f"Syntax error at line {e.lineno}: {e.msg}"], [])

    # Step 2: Check forbidden imports
    import_errors = check_analysis_imports(code)
    if import_errors:
        return AnalysisSandboxResult(False, import_errors, [])

    # Step 3: Load the module in a temp file
    try:
        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
            f.write(code)
            tmp_path = f.name

        if sys_module_name in sys.modules:
            del sys.modules[sys_module_name]

        spec = importlib.util.spec_from_file_location(sys_module_name, tmp_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[sys_module_name] = module
        spec.loader.exec_module(module)

        # Step 4: Check Analysis class exists and inherits AnalysisBase
        analysis_cls = getattr(module, "Analysis", None)
        if analysis_cls is None:
            return AnalysisSandboxResult(False, ["No 'Analysis' class found"], [])

        instance = analysis_cls()
        if not isinstance(instance, AnalysisBase):
            return AnalysisSandboxResult(False, ["Analysis must inherit from AnalysisBase"], [])

        # Step 5: Check analyze method signature
        import inspect
        sig = inspect.signature(instance.analyze)
        params = list(sig.parameters.keys())
        if "self" in params:
            params.remove("self")
        if len(params) < 2:
            errors.append(f"analyze() must accept (db, schema), got {params}")

        # Step 6: Test-run analyze() against an empty in-memory DB
        if not errors:
            try:
                result = _test_analyze(instance)
                if not isinstance(result, dict):
                    errors.append(f"analyze() must return dict, got {type(result).__name__}")
            except Exception as e:
                errors.append(f"analyze() crashed on test DB: {type(e).__name__}: {e}")

    except Exception as e:
        errors.append(f"Runtime error: {type(e).__name__}: {e}")
    finally:
        if sys_module_name in sys.modules:
            del sys.modules[sys_module_name]
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)

    if errors:
        return AnalysisSandboxResult(False, errors, warnings)

    log.info("analysis_sandbox.passed", module=module_name, warnings=len(warnings))
    return AnalysisSandboxResult(True, [], warnings)


def _test_analyze(instance: AnalysisBase) -> dict:
    """Run analyze() against an in-memory DB with schema but no data.

    Catches modules that crash on empty tables (common bug).
    Runs in a fresh event loop to avoid nesting issues.
    """

    async def _run() -> dict:
        async with aiosqlite.connect(":memory:") as conn:
            conn.row_factory = aiosqlite.Row
            await conn.executescript(SCHEMA)
            ro_db = ReadOnlyDB(conn)
            schema = get_schema_description()
            return await instance.analyze(ro_db, schema)

    # Use asyncio.run if no loop is running, otherwise run in thread
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is None:
        return asyncio.run(_run())
    else:
        # Already inside an async context — run in a new thread to avoid nested loop
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, _run())
            return future.result(timeout=10)
