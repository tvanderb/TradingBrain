"""Tests for the modular orchestration phase architecture."""

import json
import os
import tempfile

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.orchestrator.cycle import CycleState, PhaseResult, OrchestrationCycle
from src.orchestrator.phases.base import Phase
from src.orchestrator.phases.reflect import ReflectPhase
from src.orchestrator.phases.observe import ObservePhase
from src.orchestrator.phases.execute import ExecutePhase


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _SuccessPhase(Phase):
    name = "success"
    required = True

    async def execute(self, state, orchestrator):
        state.context["success_ran"] = True
        return PhaseResult(phase_name=self.name, success=True, data={"ok": True})


class _FailPhase(Phase):
    name = "fail"
    required = True

    async def execute(self, state, orchestrator):
        raise RuntimeError("boom")


class _OptionalFailPhase(Phase):
    name = "optional_fail"
    required = False

    async def execute(self, state, orchestrator):
        raise RuntimeError("optional boom")


class _RecorderPhase(Phase):
    """Records the order it ran in via state.context['order']."""
    required = True

    def __init__(self, name_: str):
        self.name = name_

    async def execute(self, state, orchestrator):
        order = state.context.setdefault("order", [])
        order.append(self.name)
        return PhaseResult(phase_name=self.name, success=True, data={})


# ---------------------------------------------------------------------------
# CycleState
# ---------------------------------------------------------------------------

def test_cycle_state_get_phase_data_empty():
    """get_phase_data returns {} for phases that haven't run."""
    state = CycleState(cycle_id="test")
    assert state.get_phase_data("nonexistent") == {}


def test_cycle_state_get_phase_data_present():
    """get_phase_data returns the data dict from a completed phase."""
    state = CycleState(cycle_id="test")
    state.phase_results["foo"] = PhaseResult(
        phase_name="foo", success=True, data={"key": "value"}
    )
    assert state.get_phase_data("foo") == {"key": "value"}


# ---------------------------------------------------------------------------
# OrchestrationCycle — sequencing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_phases_execute_in_order():
    """Phases run in the order they are provided."""
    phases = [_RecorderPhase("a"), _RecorderPhase("b"), _RecorderPhase("c")]
    state = CycleState(cycle_id="test")
    cycle = OrchestrationCycle(phases, MagicMock())
    state = await cycle.run(state)
    assert state.context["order"] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_phase_results_accumulated():
    """Each phase's result is stored in state.phase_results."""
    phases = [_SuccessPhase()]
    state = CycleState(cycle_id="test")
    cycle = OrchestrationCycle(phases, MagicMock())
    state = await cycle.run(state)
    assert "success" in state.phase_results
    assert state.phase_results["success"].success is True


# ---------------------------------------------------------------------------
# Required vs optional failure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_required_phase_failure_aborts():
    """A required phase failure raises RuntimeError and stops the cycle."""
    phases = [_FailPhase(), _SuccessPhase()]
    state = CycleState(cycle_id="test")
    cycle = OrchestrationCycle(phases, MagicMock())
    with pytest.raises(RuntimeError, match="Required phase 'fail' failed"):
        await cycle.run(state)
    # success phase should not have run
    assert "success" not in state.phase_results or not state.phase_results.get("success", PhaseResult("x", False, {})).success


@pytest.mark.asyncio
async def test_optional_phase_failure_continues():
    """An optional phase failure logs but does not abort — next phase runs."""
    phases = [_OptionalFailPhase(), _SuccessPhase()]
    state = CycleState(cycle_id="test")
    cycle = OrchestrationCycle(phases, MagicMock())
    state = await cycle.run(state)
    # Optional phase recorded as failed
    assert state.phase_results["optional_fail"].success is False
    assert "optional boom" in state.phase_results["optional_fail"].error
    # Success phase still ran
    assert state.context.get("success_ran") is True


# ---------------------------------------------------------------------------
# ReflectPhase.should_run
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reflect_should_run_when_due():
    state = CycleState(cycle_id="test")
    state.context["reflection_due"] = True
    phase = ReflectPhase()
    assert await phase.should_run(state) is True


@pytest.mark.asyncio
async def test_reflect_should_not_run_when_not_due():
    state = CycleState(cycle_id="test")
    state.context["reflection_due"] = False
    phase = ReflectPhase()
    assert await phase.should_run(state) is False


@pytest.mark.asyncio
async def test_reflect_should_not_run_when_flag_missing():
    """If reflection_due is not in context at all, default to not running."""
    state = CycleState(cycle_id="test")
    phase = ReflectPhase()
    assert await phase.should_run(state) is False


# ---------------------------------------------------------------------------
# ExecutePhase.should_run
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_should_run_with_decisions():
    state = CycleState(cycle_id="test")
    state.decisions = [{"decision": "NO_CHANGE"}]
    phase = ExecutePhase()
    assert await phase.should_run(state) is True


@pytest.mark.asyncio
async def test_execute_should_not_run_empty_decisions():
    state = CycleState(cycle_id="test")
    state.decisions = []
    phase = ExecutePhase()
    assert await phase.should_run(state) is False


# ---------------------------------------------------------------------------
# Phase properties
# ---------------------------------------------------------------------------

def test_reflect_phase_is_optional():
    assert ReflectPhase().required is False


def test_observe_phase_is_required():
    assert ObservePhase().required is True


def test_execute_phase_is_required():
    assert ExecutePhase().required is True


# ---------------------------------------------------------------------------
# Full cycle integration (mirrors test_orchestration_nightly_cycle_no_change)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_cycle_no_change():
    """Full orchestration cycle via phase architecture with NO_CHANGE decision."""
    from src.shell.config import load_config
    from src.shell.database import Database
    from src.shell.data_store import DataStore
    from src.orchestrator.orchestrator import Orchestrator

    config = load_config()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        config.db_path = f.name

    try:
        db = Database(config.db_path)
        await db.connect()
        data_store = DataStore(db, config.data)

        # Mock AI client
        ai = AsyncMock()
        ai.tokens_remaining = 1000000
        ai._daily_tokens_used = 500
        ai.get_daily_usage = AsyncMock(return_value={
            "used": 500, "daily_limit": 1500000, "total_cost": 0.02, "models": {}
        })

        # Opus returns a NO_CHANGE decision
        ai.ask_opus = AsyncMock(return_value=json.dumps({
            "decision": "NO_CHANGE",
            "reasoning": "Phase architecture test — no changes.",
            "market_observations": "BTC stable",
            "cross_reference_findings": "",
        }))

        orch = Orchestrator(config, db, ai, MagicMock(), data_store)
        report = await orch.run_nightly_cycle()

        assert "No changes" in report
        assert orch._cycle_id is not None

        # Verify observation stored
        obs = await db.fetchall("SELECT * FROM orchestrator_observations")
        assert len(obs) == 1

        # Verify orchestrator_log stored
        log_row = await db.fetchone(
            "SELECT * FROM orchestrator_log ORDER BY id DESC LIMIT 1"
        )
        assert log_row is not None
        assert log_row["action"] == "NO_CHANGE"

        await db.close()
    finally:
        os.unlink(config.db_path)


@pytest.mark.asyncio
async def test_full_cycle_reflect_failure_continues():
    """Reflection failure (optional phase) should not prevent observe/execute."""
    from src.shell.config import load_config
    from src.shell.database import Database
    from src.shell.data_store import DataStore
    from src.orchestrator.orchestrator import Orchestrator

    config = load_config()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        config.db_path = f.name

    try:
        db = Database(config.db_path)
        await db.connect()
        data_store = DataStore(db, config.data)

        ai = AsyncMock()
        ai.tokens_remaining = 1000000
        ai._daily_tokens_used = 500
        ai.get_daily_usage = AsyncMock(return_value={
            "used": 500, "daily_limit": 1500000, "total_cost": 0.02, "models": {}
        })

        # Opus returns NO_CHANGE for analysis
        ai.ask_opus = AsyncMock(return_value=json.dumps({
            "decision": "NO_CHANGE",
            "reasoning": "Stable.",
            "market_observations": "Calm markets",
            "cross_reference_findings": "",
        }))

        orch = Orchestrator(config, db, ai, MagicMock(), data_store)

        # Force reflection to be due, but make _reflect() raise
        original_should_reflect = orch._should_reflect
        orch._should_reflect = AsyncMock(return_value=True)
        orch._reflect = AsyncMock(side_effect=RuntimeError("Reflection exploded"))

        report = await orch.run_nightly_cycle()

        # Cycle should still complete successfully
        assert "No changes" in report

        # Verify observe still ran (observation stored)
        obs = await db.fetchall("SELECT * FROM orchestrator_observations")
        assert len(obs) == 1

        await db.close()
    finally:
        os.unlink(config.db_path)


# ---------------------------------------------------------------------------
# Import backward compatibility
# ---------------------------------------------------------------------------

def test_prompt_imports_from_orchestrator():
    """Prompt constants are still importable from orchestrator.py."""
    from src.orchestrator.orchestrator import (
        LAYER_1_IDENTITY, FUND_MANDATE, LAYER_2_SYSTEM,
        CODE_GEN_SYSTEM, CODE_REVIEW_SYSTEM, BACKTEST_REVIEW_SYSTEM,
        REFLECTION_USER_TEMPLATE, ANALYSIS_CODE_GEN_SYSTEM, ANALYSIS_REVIEW_SYSTEM,
    )
    assert "Radical Honesty" in LAYER_1_IDENTITY
    assert "capital preservation" in FUND_MANDATE.lower()
    assert "Architecture" in LAYER_2_SYSTEM
    assert "StrategyBase" in CODE_GEN_SYSTEM
    assert "IO Contract" in CODE_REVIEW_SYSTEM
    assert "backtest" in BACKTEST_REVIEW_SYSTEM.lower()
    assert "reflection" in REFLECTION_USER_TEMPLATE.lower()
    assert "AnalysisBase" in ANALYSIS_CODE_GEN_SYSTEM
    assert "Formula correctness" in ANALYSIS_REVIEW_SYSTEM


def test_prompt_imports_from_prompts():
    """Prompt constants are importable from the new prompts module."""
    from src.orchestrator.prompts import (
        LAYER_1_IDENTITY, FUND_MANDATE, LAYER_2_SYSTEM,
        CODE_GEN_SYSTEM, CODE_REVIEW_SYSTEM, BACKTEST_REVIEW_SYSTEM,
        REFLECTION_USER_TEMPLATE, ANALYSIS_CODE_GEN_SYSTEM, ANALYSIS_REVIEW_SYSTEM,
    )
    assert "Radical Honesty" in LAYER_1_IDENTITY
    assert "StrategyBase" in CODE_GEN_SYSTEM
