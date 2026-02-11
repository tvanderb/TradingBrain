"""Analysis Module Loader — dynamically imports analysis modules.

Handles loading, archiving, and deploying both analysis modules:
- statistics/active/market_analysis.py
- statistics/active/trade_performance.py

Same pattern as strategy loader, adapted for two modules.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

import structlog

from src.shell.contract import AnalysisBase

log = structlog.get_logger()

STATISTICS_DIR = Path(__file__).resolve().parent.parent.parent / "statistics"
ACTIVE_DIR = STATISTICS_DIR / "active"
ARCHIVE_DIR = STATISTICS_DIR / "archive"

MODULE_FILES = {
    "market_analysis": "market_analysis.py",
    "trade_performance": "trade_performance.py",
}


def get_module_path(module_name: str) -> Path:
    """Get the path to an analysis module file."""
    if module_name not in MODULE_FILES:
        raise ValueError(f"Unknown module: {module_name}. Must be one of {list(MODULE_FILES.keys())}")
    return ACTIVE_DIR / MODULE_FILES[module_name]


def get_code_hash(path: Path) -> str:
    """SHA256 hash of the module file contents."""
    content = path.read_bytes()
    return hashlib.sha256(content).hexdigest()[:16]


def load_analysis_module(module_name: str) -> AnalysisBase:
    """Load an analysis module and return an Analysis instance.

    Args:
        module_name: Either 'market_analysis' or 'trade_performance'

    Returns:
        An instance of the module's Analysis class (must inherit AnalysisBase)

    Raises:
        RuntimeError: If the module file is missing or invalid
    """
    path = get_module_path(module_name)
    if not path.exists():
        raise RuntimeError(f"Analysis module not found: {path}")

    # Validate before loading (defense-in-depth, matching strategy loader pattern)
    from src.statistics.sandbox import validate_analysis_module
    code = path.read_text()
    validation = validate_analysis_module(code, module_name)
    if not validation.passed:
        raise RuntimeError(f"Analysis validation failed: {validation.errors}")

    sys_module_name = f"analysis_{module_name}"

    # Remove old module if reloading
    if sys_module_name in sys.modules:
        del sys.modules[sys_module_name]

    spec = importlib.util.spec_from_file_location(sys_module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load analysis module from: {path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[sys_module_name] = module
    spec.loader.exec_module(module)

    # Find the Analysis class
    analysis_cls = getattr(module, "Analysis", None)
    if analysis_cls is None:
        raise RuntimeError(f"Analysis module must define an 'Analysis' class: {path}")

    instance = analysis_cls()

    if not isinstance(instance, AnalysisBase):
        raise RuntimeError(f"Analysis must inherit from AnalysisBase: {path}")

    code_hash = get_code_hash(path)
    log.info("analysis.loaded", module=module_name, hash=code_hash)
    return instance


def archive_module(module_name: str, version: str) -> Path:
    """Copy current module to archive with version name."""
    src = get_module_path(module_name)
    if not src.exists():
        raise RuntimeError(f"No active {module_name} to archive")

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    dest = ARCHIVE_DIR / f"{module_name}_{version}.py"
    dest.write_text(src.read_text())
    log.info("analysis.archived", module=module_name, version=version, path=str(dest))
    return dest


def deploy_module(module_name: str, code: str, version: str) -> str:
    """Write new analysis module code. Returns code hash."""
    current_path = get_module_path(module_name)

    # Archive current version first
    if current_path.exists():
        current_hash = get_code_hash(current_path)
        archive_module(module_name, f"pre_{version}_{current_hash}")

    ACTIVE_DIR.mkdir(parents=True, exist_ok=True)
    current_path.write_text(code)
    code_hash = get_code_hash(current_path)
    log.info("analysis.deployed", module=module_name, version=version, hash=code_hash)
    return code_hash
