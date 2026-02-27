"""ObservePhase — gather context, analyze, normalize decisions.

Combines OBSERVE + EVALUATE + DECIDE in a single Opus call (matching
the current _analyze() method). Phase 2 of the pivot splits these into
three distinct phases with separate prompts.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import structlog

from src.orchestrator.phases.base import Phase

if TYPE_CHECKING:
    from src.orchestrator.cycle import CycleState, PhaseResult
    from src.orchestrator.orchestrator import Orchestrator

log = structlog.get_logger()


class ObservePhase(Phase):
    """Gather context, run Opus analysis, normalize decisions."""

    name = "observe"
    required = True

    async def execute(self, state: CycleState, orchestrator: Orchestrator) -> PhaseResult:
        from src.orchestrator.cycle import PhaseResult

        start = time.monotonic()

        # 1. Gather context
        context = await orchestrator._gather_context()
        state.context["gathered"] = context

        # 2. Opus analysis
        parsed = await orchestrator._analyze(context, trigger=state.trigger)
        state.context["analysis_parsed"] = parsed

        # 3. Normalize decisions
        parsed = orchestrator._normalize_decisions(parsed)
        state.context["analysis_parsed"] = parsed
        state.decisions = parsed["decisions"]

        return PhaseResult(
            phase_name=self.name,
            success=True,
            data={
                "decision_count": len(state.decisions),
                "decision_types": [
                    str(d.get("decision") or "NO_CHANGE").strip().upper()
                    for d in state.decisions
                ],
            },
            duration_seconds=time.monotonic() - start,
        )
