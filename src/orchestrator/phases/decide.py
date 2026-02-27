"""DecidePhase — decide what actions to take based on observations and evaluation.

Takes OBSERVE + EVALUATE outputs and produces a decisions list.
This is the final analytical phase before execution.
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
    DECIDE_PHASE_INSTRUCTIONS,
)

if TYPE_CHECKING:
    from src.orchestrator.cycle import CycleState, PhaseResult
    from src.orchestrator.orchestrator import Orchestrator

log = structlog.get_logger()


class DecidePhase(Phase):
    """Decide what actions to take based on observations and evaluation."""

    name = "decide"
    required = True

    async def execute(self, state: CycleState, orchestrator: Orchestrator) -> PhaseResult:
        from src.orchestrator.cycle import PhaseResult

        start = time.monotonic()
        context = state.context.get("gathered", {})
        observe_output = state.context.get("observe_output", {})
        evaluate_output = state.context.get("evaluate_output", {})

        # Build DECIDE prompt — prior phase outputs + action-relevant context
        prompt = f"""## YOUR OBSERVATIONS (from OBSERVE phase)
{json.dumps(observe_output, indent=2, default=str)}

---

## YOUR EVALUATION (from EVALUATE phase)
{json.dumps(evaluate_output, indent=2, default=str)}

---

## STRATEGY CONTEXT (for CREATE_CANDIDATE / analysis update decisions)
### Active Strategy Code:
```python
{context.get("strategy_code", "No strategy")}
```

### Strategy Document:
{context.get("strategy_doc", "No strategy document")}

### Market Analysis Module Code:
```python
{context.get("market_analysis_code", "No module")}
```

### Trade Performance Module Code:
```python
{context.get("trade_performance_code", "No module")}
```

---

## CANDIDATE SLOTS
{json.dumps(context.get("candidates", []), indent=2, default=str) if context.get("candidates") else "No active candidates. All {max_cands} slots available.".format(max_cands=orchestrator._config.orchestrator.max_candidates)}

---

Respond in JSON format."""

        # Build system prompt
        system_prompt = (
            f"{LAYER_1_IDENTITY}\n\n---\n\n{FUND_MANDATE}\n\n---\n\n"
            f"{DECIDE_PHASE_INSTRUCTIONS}"
        )

        response = await orchestrator._ai.ask_opus(
            prompt, system=system_prompt, purpose="decide"
        )

        parsed = orchestrator._extract_json(response)
        if parsed is None:
            log.warning("orchestrator.decide_parse_failed", response=response[:500])
            parsed = {
                "decisions": [{"decision": "NO_CHANGE"}],
                "reasoning": "Failed to parse decide response",
            }

        await orchestrator._store_thought("decide", "opus", prompt, response, parsed)

        # Normalize decisions (backward compat with single-decision format)
        parsed = orchestrator._normalize_decisions(parsed)
        state.decisions = parsed["decisions"]

        # Store the full parsed response for post-phase processing
        # Merge observe fields into analysis_parsed so post-phase code
        # can find market_observations, cross_reference_findings, etc.
        state.context["analysis_parsed"] = {
            **parsed,
            "market_observations": observe_output.get("market_observations", ""),
            "cross_reference_findings": evaluate_output.get("cross_reference_findings", ""),
        }

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
