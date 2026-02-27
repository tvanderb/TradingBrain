"""Orchestration cycle framework — CycleState, PhaseResult, OrchestrationCycle."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from src.orchestrator.orchestrator import Orchestrator
    from src.orchestrator.phases.base import Phase

log = structlog.get_logger()


@dataclass
class PhaseResult:
    """Output of a single phase execution."""

    phase_name: str
    success: bool
    data: dict[str, Any]
    error: str | None = None
    duration_seconds: float = 0.0
    tokens_used: int = 0


@dataclass
class CycleState:
    """Shared state passed through all phases in a cycle."""

    cycle_id: str
    trigger: str = "scheduled"
    phase_results: dict[str, PhaseResult] = field(default_factory=dict)
    decisions: list[dict] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    reports: list[str] = field(default_factory=list)
    deployed_version: str | None = None

    def get_phase_data(self, phase_name: str) -> dict:
        """Get data from a completed phase, empty dict if not run."""
        result = self.phase_results.get(phase_name)
        if result is None:
            return {}
        return result.data


class OrchestrationCycle:
    """Runs a sequence of phases, collecting results into CycleState."""

    def __init__(self, phases: list[Phase], orchestrator: Orchestrator) -> None:
        self._phases = phases
        self._orchestrator = orchestrator

    async def run(self, state: CycleState) -> CycleState:
        """Execute phases in sequence.

        Required phases abort on failure, optional phases log and continue.
        Tracks per-phase token usage via AI client snapshot.
        """
        ai = self._orchestrator._ai

        for phase in self._phases:
            # Check if phase should run
            if not await phase.should_run(state):
                log.info("orchestrator.phase_skipped", phase=phase.name)
                continue

            log.info("orchestrator.phase_start", phase=phase.name)
            start = time.monotonic()
            tokens_before = ai._daily_tokens_used

            try:
                result = await phase.execute(state, self._orchestrator)
            except Exception as e:
                elapsed = time.monotonic() - start
                tokens_used = ai._daily_tokens_used - tokens_before
                result = PhaseResult(
                    phase_name=phase.name,
                    success=False,
                    data={},
                    error=str(e),
                    duration_seconds=elapsed,
                    tokens_used=tokens_used,
                )
                log.error(
                    "orchestrator.phase_failed",
                    phase=phase.name,
                    error=str(e),
                    required=phase.required,
                    tokens_used=tokens_used,
                    exc_info=True,
                )

            # Fill in token usage if the phase didn't set it
            if result.tokens_used == 0:
                result.tokens_used = ai._daily_tokens_used - tokens_before

            state.phase_results[phase.name] = result

            if result.success:
                log.info(
                    "orchestrator.phase_complete",
                    phase=phase.name,
                    duration=f"{result.duration_seconds:.1f}s",
                    tokens_used=result.tokens_used,
                )
            elif phase.required:
                log.error(
                    "orchestrator.required_phase_failed",
                    phase=phase.name,
                    error=result.error,
                    tokens_used=result.tokens_used,
                )
                raise RuntimeError(
                    f"Required phase '{phase.name}' failed: {result.error}"
                )
            # Optional phase failed — already logged, continue

        # Log cycle-level token summary
        total_tokens = sum(
            r.tokens_used for r in state.phase_results.values()
        )
        phase_breakdown = {
            name: r.tokens_used
            for name, r in state.phase_results.items()
            if r.tokens_used > 0
        }
        if phase_breakdown:
            log.info(
                "orchestrator.cycle_tokens",
                total=total_tokens,
                breakdown=phase_breakdown,
            )

        return state
