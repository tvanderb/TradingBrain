"""ExecutePhase — dispatch decisions to action handlers."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import structlog

from src.orchestrator.phases.base import Phase

if TYPE_CHECKING:
    from src.orchestrator.cycle import CycleState, PhaseResult
    from src.orchestrator.orchestrator import Orchestrator

log = structlog.get_logger()


class ExecutePhase(Phase):
    """Iterate decisions and dispatch to action handlers."""

    name = "execute"
    required = True

    async def should_run(self, state: CycleState) -> bool:
        return len(state.decisions) > 0

    async def execute(self, state: CycleState, orchestrator: Orchestrator) -> PhaseResult:
        from src.orchestrator.cycle import PhaseResult

        start = time.monotonic()
        context = state.context.get("gathered", {})

        for action in state.decisions:
            action_type = str(action.get("decision") or "NO_CHANGE").strip().upper()

            if action_type == "NO_CHANGE":
                state.reports.append("No changes.")
            elif action_type in ("MARKET_ANALYSIS_UPDATE", "TRADE_ANALYSIS_UPDATE"):
                state.reports.append(
                    await orchestrator._execute_analysis_change(action, context)
                )
            elif action_type == "CREATE_CANDIDATE":
                state.reports.append(
                    await orchestrator._create_candidate(action, context)
                )
            elif action_type == "CANCEL_CANDIDATE":
                state.reports.append(await orchestrator._cancel_candidate(action))
            elif action_type == "PROMOTE_CANDIDATE":
                rpt = await orchestrator._promote_candidate(action)
                state.reports.append(rpt)
                if "promoted" in rpt.lower():
                    ver_row = await orchestrator._db.fetchone(
                        "SELECT version FROM strategy_versions "
                        "WHERE deployed_at IS NOT NULL "
                        "ORDER BY deployed_at DESC LIMIT 1"
                    )
                    state.deployed_version = (
                        ver_row["version"] if ver_row else None
                    )
            else:
                log.warning(
                    "orchestrator.unknown_decision_type",
                    decision_type=action_type,
                )
                state.reports.append(f"Unknown decision '{action_type}'.")

            # Log each action
            await orchestrator._log_orchestration(
                action,
                deployed_version=state.deployed_version,
                outcome=state.reports[-1],
            )

        return PhaseResult(
            phase_name=self.name,
            success=True,
            data={"actions_executed": len(state.decisions)},
            duration_seconds=time.monotonic() - start,
        )
