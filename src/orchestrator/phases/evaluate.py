"""EvaluatePhase — assess performance of strategy, candidates, and analysis tools.

Takes OBSERVE output and performance data. Outputs assessments and
cross-reference findings. Does not make decisions — that's the DECIDE phase.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import structlog

from src.orchestrator.phases.base import Phase
from src.orchestrator.prompts import (
    LAYER_1_IDENTITY,
    FUND_MANDATE,
    EVALUATE_PHASE_INSTRUCTIONS,
)

if TYPE_CHECKING:
    from src.orchestrator.cycle import CycleState, PhaseResult
    from src.orchestrator.orchestrator import Orchestrator

log = structlog.get_logger()


class EvaluatePhase(Phase):
    """Evaluate performance of active strategy, candidates, and analysis tools."""

    name = "evaluate"
    required = True

    async def execute(self, state: CycleState, orchestrator: Orchestrator) -> PhaseResult:
        from src.orchestrator.cycle import PhaseResult

        start = time.monotonic()
        context = state.context.get("gathered", {})
        observe_output = state.context.get("observe_output", {})

        # Build EVALUATE prompt — focused on performance data
        prompt = f"""## YOUR OBSERVATIONS (from this cycle's OBSERVE phase)
{json.dumps(observe_output, indent=2, default=str)}

---

## ACTIVE STRATEGY PERFORMANCE
### Strategy Document (Institutional Memory):
{context.get("strategy_doc", "No strategy document")}

### Performance (Last 7 Days):
{json.dumps(context.get("performance_7d", {}), indent=2, default=str)}

### Daily Performance Snapshots:
{json.dumps(context.get("daily_performance", []), indent=2, default=str)}

### Recent Trades (Last 50):
{json.dumps(context.get("recent_trades", []), indent=2, default=str)}

### Strategy Version History:
{json.dumps(context.get("version_history", []), indent=2, default=str)}

---

## CANDIDATE STRATEGIES
{json.dumps(context.get("candidates", []), indent=2, default=str) if context.get("candidates") else "No active candidates."}

---

## GROUND TRUTH (for cross-reference)
{json.dumps(context.get("ground_truth", {}), indent=2, default=str)}

---

Respond in JSON format."""

        # Build system prompt — lighter, no full system context
        system_prompt = (
            f"{LAYER_1_IDENTITY}\n\n---\n\n{FUND_MANDATE}\n\n---\n\n"
            f"{EVALUATE_PHASE_INSTRUCTIONS}"
        )

        response = await orchestrator._ai.ask_opus(
            prompt, system=system_prompt, purpose="evaluate"
        )

        parsed = orchestrator._extract_json(response)
        if parsed is None:
            log.warning("orchestrator.evaluate_parse_failed", response=response[:500])
            parsed = {
                "active_strategy_assessment": "Failed to parse evaluate response",
                "candidate_assessments": [],
                "analysis_tool_assessment": "",
                "cross_reference_findings": "",
                "whats_working": "",
                "whats_failing": "",
                "hypotheses": "",
            }

        await orchestrator._store_thought("evaluate", "opus", prompt, response, parsed)

        state.context["evaluate_output"] = parsed

        return PhaseResult(
            phase_name=self.name,
            success=True,
            data={"has_hypotheses": bool(parsed.get("hypotheses"))},
            duration_seconds=time.monotonic() - start,
        )
