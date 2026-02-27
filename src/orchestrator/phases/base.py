"""Base phase interface for orchestration cycle."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.orchestrator.cycle import CycleState, PhaseResult
    from src.orchestrator.orchestrator import Orchestrator


class Phase(ABC):
    """Abstract base class for orchestration phases."""

    name: str = ""
    required: bool = True

    async def should_run(self, state: CycleState) -> bool:
        """Override to conditionally skip. Default: always run."""
        return True

    @abstractmethod
    async def execute(self, state: CycleState, orchestrator: Orchestrator) -> PhaseResult:
        """Execute the phase. Access orchestrator dependencies via back-reference."""
        ...
