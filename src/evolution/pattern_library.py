"""Learned pattern storage and retrieval.

Stores patterns discovered by the Executive Brain during evolution cycles.
Patterns are observations about market behavior, strategy performance,
and system optimization that accumulate over time.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from src.core.logging import get_logger

log = get_logger("patterns")

PATTERNS_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "learned_patterns.json"


def load_patterns() -> list[dict]:
    """Load the learned patterns library."""
    if not PATTERNS_FILE.exists():
        return []
    try:
        return json.loads(PATTERNS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def save_patterns(patterns: list[dict]) -> None:
    """Save patterns to disk."""
    PATTERNS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PATTERNS_FILE.write_text(json.dumps(patterns, indent=2) + "\n")


def add_patterns(new_patterns: list[str]) -> int:
    """Add new patterns from an evolution cycle.

    Returns the number of new unique patterns added.
    """
    existing = load_patterns()
    existing_texts = {p["text"] for p in existing}

    added = 0
    for text in new_patterns:
        if text not in existing_texts:
            existing.append({
                "text": text,
                "discovered_at": datetime.now().isoformat(),
                "confidence": 0.5,  # Initial confidence, updated over time
                "validations": 0,
            })
            added += 1

    if added > 0:
        save_patterns(existing)
        log.info("patterns_added", count=added, total=len(existing))

    return added
