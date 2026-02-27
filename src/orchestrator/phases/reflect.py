"""ReflectPhase — periodic reflection (grade predictions, rewrite strategy doc)."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import structlog

from src.orchestrator.phases.base import Phase

if TYPE_CHECKING:
    from src.orchestrator.cycle import CycleState, PhaseResult
    from src.orchestrator.orchestrator import Orchestrator

log = structlog.get_logger()


class ReflectPhase(Phase):
    """Run the periodic reflection cycle if due.

    Optional phase — a reflection failure should not abort the entire
    orchestration cycle.
    """

    name = "reflect"
    required = False

    async def should_run(self, state: CycleState) -> bool:
        return state.context.get("reflection_due", False)

    async def execute(self, state: CycleState, orchestrator: Orchestrator) -> PhaseResult:
        from src.orchestrator.cycle import PhaseResult

        start = time.monotonic()

        await orchestrator._reflect()

        # Clear manual trigger if set
        await orchestrator._db.execute(
            "DELETE FROM system_meta WHERE key = 'reflect_tonight'"
        )
        await orchestrator._db.commit()

        return PhaseResult(
            phase_name=self.name,
            success=True,
            data={"reflected": True},
            duration_seconds=time.monotonic() - start,
        )
