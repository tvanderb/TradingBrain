"""ObservePhase — gather context, observe market and fund state.

Focused on data analysis: market conditions, signal activity, analysis
module quality, and events since last cycle. Does not evaluate performance
or make decisions — those are separate phases.
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
    SYSTEM_CONTEXT,
    OBSERVE_PHASE_INSTRUCTIONS,
)

if TYPE_CHECKING:
    from src.orchestrator.cycle import CycleState, PhaseResult
    from src.orchestrator.orchestrator import Orchestrator

log = structlog.get_logger()


class ObservePhase(Phase):
    """Gather context and observe the current state of markets and fund."""

    name = "observe"
    required = True

    async def execute(self, state: CycleState, orchestrator: Orchestrator) -> PhaseResult:
        from src.orchestrator.cycle import PhaseResult

        start = time.monotonic()

        # 1. Gather all context (same as before — shared across phases)
        context = await orchestrator._gather_context()
        state.context["gathered"] = context

        # 2. Build OBSERVE prompt with market/analysis data
        time_context = await orchestrator._build_time_context(trigger=state.trigger)

        since = context.get("since_last_cycle")
        since_section = orchestrator._format_since_last_cycle(since) if since else ""

        prompt = f"""{time_context}
{since_section}

Current fund state for observation.

---

## GROUND TRUTH (rigid shell — you cannot change this)
{json.dumps(context["ground_truth"], indent=2, default=str)}

---

## YOUR MARKET ANALYSIS (you designed this module — you can rewrite it)
### Module Output:
{json.dumps(context["market_report"], indent=2, default=str)}

### Module Source Code:
```python
{context["market_analysis_code"]}
```

---

## YOUR TRADE PERFORMANCE ANALYSIS (you designed this module — you can rewrite it)
### Module Output:
{json.dumps(context["trade_performance_report"], indent=2, default=str)}

### Module Source Code:
```python
{context["trade_performance_code"]}
```

---

## YOUR STRATEGY
### Strategy Source Code:
```python
{context["strategy_code"]}
```

---

## SYSTEM CONSTRAINTS
{_build_system_constraints(orchestrator, context)}

---

## SIGNAL & OBSERVATION STATE
### Signal Drought Detection:
{json.dumps(context["signal_drought"], indent=2, default=str)}

### Recent Observations (last {orchestrator._config.orchestrator.reflection_interval_days} days):
{json.dumps(context["recent_observations"], indent=2, default=str) if context["recent_observations"] else "No prior observations."}

---

Respond in JSON format."""

        # 3. Build system prompt
        system_context = SYSTEM_CONTEXT.format(
            max_candidates=orchestrator._config.orchestrator.max_candidates,
        )
        system_prompt = (
            f"{LAYER_1_IDENTITY}\n\n---\n\n{FUND_MANDATE}\n\n---\n\n"
            f"{system_context}\n\n---\n\n{OBSERVE_PHASE_INSTRUCTIONS}"
        )

        # 4. Call Opus
        response = await orchestrator._ai.ask_opus(
            prompt, system=system_prompt, purpose="observe"
        )

        parsed = orchestrator._extract_json(response)
        if parsed is None:
            log.warning("orchestrator.observe_parse_failed", response=response[:500])
            parsed = {
                "market_observations": "Failed to parse observe response",
                "strategy_signal_assessment": "",
                "analysis_module_assessment": "",
                "since_last_cycle_summary": "",
                "data_quality_notes": "",
                "notable_conditions": "",
            }

        await orchestrator._store_thought("observe", "opus", prompt, response, parsed)

        state.context["observe_output"] = parsed

        return PhaseResult(
            phase_name=self.name,
            success=True,
            data={"has_observations": bool(parsed.get("market_observations"))},
            duration_seconds=time.monotonic() - start,
        )


def _build_system_constraints(orchestrator: Orchestrator, context: dict) -> str:
    """Build the system constraints section shared between phases."""
    cfg = orchestrator._config
    return (
        f"- Trading pairs: {', '.join(cfg.symbols)}\n"
        f"- System: Long-only (no short selling, no leverage)\n"
        f"- Maker fee: {cfg.kraken.maker_fee_pct}% / Taker fee: {cfg.kraken.taker_fee_pct}%\n"
        f"- Default slippage: {cfg.default_slippage_factor * 100:.2f}%\n"
        f"- Max trade size: {cfg.risk.max_trade_pct * 100:.0f}% of portfolio\n"
        f"- Default trade size: {cfg.risk.default_trade_pct * 100:.0f}% of portfolio\n"
        f"- Max position size: {cfg.risk.max_position_pct * 100:.0f}% of portfolio\n"
        f"- Max positions: {cfg.risk.max_positions}\n"
        f"- Max daily loss: {cfg.risk.max_daily_loss_pct * 100:.0f}% of portfolio (trading halts)\n"
        f"- Max drawdown: {cfg.risk.max_drawdown_pct * 100:.0f}% from peak (system halts)\n"
        f"- Consecutive loss halt: {cfg.risk.rollback_consecutive_losses} consecutive losses\n"
        f"- Max candidate slots: {cfg.orchestrator.max_candidates}"
    )
