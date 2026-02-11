"""Strategy Loader — dynamically imports the active strategy module.

Handles loading, reloading, and hot-swapping strategy files.
The strategy must implement the StrategyBase interface from contract.py.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from src.shell.contract import StrategyBase

if TYPE_CHECKING:
    from src.shell.database import Database

log = structlog.get_logger()

STRATEGY_DIR = Path(__file__).resolve().parent.parent.parent / "strategy"
ACTIVE_DIR = STRATEGY_DIR / "active"
ARCHIVE_DIR = STRATEGY_DIR / "archive"


def get_strategy_path() -> Path:
    return ACTIVE_DIR / "strategy.py"


def get_code_hash(path: Path) -> str:
    """SHA256 hash of the strategy file contents."""
    content = path.read_bytes()
    return hashlib.sha256(content).hexdigest()[:16]


def load_strategy() -> StrategyBase:
    """Load the active strategy module and return a Strategy instance.

    Validates against sandbox rules before loading.
    Raises RuntimeError if the strategy file is missing or invalid.
    """
    from src.strategy.sandbox import validate_strategy

    path = get_strategy_path()
    if not path.exists():
        raise RuntimeError(f"Strategy file not found: {path}")

    # Validate sandbox rules before loading
    code = path.read_text()
    result = validate_strategy(code)
    if not result.passed:
        raise RuntimeError(f"Strategy validation failed: {result.errors}")

    module_name = "strategy_active"

    # Remove old module if reloading
    if module_name in sys.modules:
        del sys.modules[module_name]

    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load strategy from: {path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    # Find the Strategy class
    strategy_cls = getattr(module, "Strategy", None)
    if strategy_cls is None:
        raise RuntimeError("Strategy file must define a 'Strategy' class")

    strategy = strategy_cls()

    if not isinstance(strategy, StrategyBase):
        raise RuntimeError("Strategy must inherit from StrategyBase")

    code_hash = get_code_hash(path)
    log.info("strategy.loaded", path=str(path), hash=code_hash)
    return strategy


def archive_strategy(version: str) -> Path:
    """Copy current strategy to archive with version name."""
    src = get_strategy_path()
    if not src.exists():
        raise RuntimeError("No active strategy to archive")

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    dest = ARCHIVE_DIR / f"strategy_{version}.py"
    dest.write_text(src.read_text())
    log.info("strategy.archived", version=version, path=str(dest))
    return dest


def deploy_strategy(code: str, version: str) -> str:
    """Write new strategy code to active directory. Returns code hash."""
    # Archive current version first
    current_path = get_strategy_path()
    if current_path.exists():
        current_hash = get_code_hash(current_path)
        archive_strategy(f"pre_{version}_{current_hash}")

    ACTIVE_DIR.mkdir(parents=True, exist_ok=True)
    current_path.write_text(code)
    code_hash = get_code_hash(current_path)
    log.info("strategy.deployed", version=version, hash=code_hash)
    return code_hash


async def load_strategy_with_fallback(db: Database) -> StrategyBase | None:
    """Load strategy with DB fallback chain (L4 fix).

    1. Try filesystem (normal path)
    2. On failure: recover from latest strategy_versions.code in DB
    3. On failure: return None (paused mode)
    """
    # 1. Try filesystem
    try:
        return load_strategy()
    except Exception as e:
        log.warning("strategy.filesystem_load_failed", error=str(e))

    # 2. Try DB fallback
    try:
        row = await db.fetchone(
            "SELECT code, version FROM strategy_versions WHERE code IS NOT NULL ORDER BY deployed_at DESC LIMIT 1"
        )
        if row and row["code"]:
            log.info("strategy.recovering_from_db", version=row["version"])
            path = get_strategy_path()
            ACTIVE_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(row["code"])
            return load_strategy()
    except Exception as e:
        log.error("strategy.db_fallback_failed", error=str(e))

    # 3. All sources failed
    log.error("strategy.all_sources_failed")
    return None
