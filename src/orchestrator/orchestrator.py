"""Orchestrator — nightly AI review, strategy evolution, and analysis module evolution.

Runs daily during the nightly EST window (configurable, default 3:30-6am):
1. Gather context:
   - Ground truth benchmarks (rigid shell, cannot modify)
   - Market analysis module output (flexible, can rewrite)
   - Trade performance module output (flexible, can rewrite)
   - Strategy code, doc, version history
   - Candidate strategies (up to 3 slots running in paper simulation)
   - User constraints (risk limits, goals)
2. Opus analyzes with labeled inputs and cross-references
3. Decides: NO_CHANGE / CREATE_CANDIDATE / CANCEL_CANDIDATE / PROMOTE_CANDIDATE
           / MARKET_ANALYSIS_UPDATE / TRADE_ANALYSIS_UPDATE
4. If create candidate: Sonnet generates -> Opus reviews -> sandbox -> backtest -> deploy to candidate slot
5. If promote candidate: candidate code deployed to active strategy, all candidates cleared
6. If analysis change: Sonnet generates -> Opus reviews (math focus) -> sandbox -> deploy (no paper test)
7. Update strategy document with findings
8. Data maintenance
"""

from __future__ import annotations

import asyncio
import difflib
import importlib.util
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import structlog

from src.orchestrator.ai_client import AIClient
from src.orchestrator.reporter import Reporter
from src.shell.config import Config
from src.telegram.notifications import Notifier
from src.shell.contract import RiskLimits
from src.shell.data_store import DataStore
from src.shell.database import Database
from src.shell.truth import compute_truth_benchmarks
from src.statistics.loader import deploy_module as deploy_analysis_module
from src.statistics.loader import get_code_hash as get_analysis_hash
from src.statistics.loader import get_module_path, load_analysis_module
from src.statistics.readonly_db import ReadOnlyDB, get_schema_description
from src.statistics.sandbox import validate_analysis_module
from src.strategy.backtester import Backtester, BacktestResult
from src.strategy.loader import (
    deploy_strategy,
    get_code_hash,
    get_strategy_path,
    load_strategy,
)
from src.strategy.sandbox import validate_strategy

log = structlog.get_logger()

STRATEGY_DOC_PATH = (
    Path(__file__).resolve().parent.parent.parent / "strategy" / "strategy_document.md"
)

# Prompt constants — extracted to src/orchestrator/prompts.py
# Re-exported here for backward compatibility with existing imports.
from src.orchestrator.prompts import (  # noqa: F401
    LAYER_1_IDENTITY,
    FUND_MANDATE,
    LAYER_2_SYSTEM,
    CODE_GEN_SYSTEM,
    CODE_REVIEW_SYSTEM,
    BACKTEST_REVIEW_SYSTEM,
    REFLECTION_USER_TEMPLATE,
    ANALYSIS_CODE_GEN_SYSTEM,
    ANALYSIS_REVIEW_SYSTEM,
    SYSTEM_CONTEXT,
    OBSERVE_PHASE_INSTRUCTIONS,
    EVALUATE_PHASE_INSTRUCTIONS,
    DECIDE_PHASE_INSTRUCTIONS,
)


class Orchestrator:
    """Nightly AI review and strategy evolution engine."""

    def __init__(
        self,
        config: Config,
        db: Database,
        ai: AIClient,
        reporter: Reporter,
        data_store: DataStore,
        notifier: Notifier | None = None,
        candidate_manager=None,
    ) -> None:
        self._config = config
        self._db = db
        self._ai = ai
        self._reporter = reporter
        self._data_store = data_store
        self._notifier = notifier
        self._candidate_manager = candidate_manager
        self._close_all_callback = None
        self._scan_state: dict | None = None
        self._cycle_id: str | None = None
        self._running = False
        self._cycle_lock = asyncio.Lock()

    def set_close_all_callback(self, callback) -> None:
        """Set callback for closing all fund positions during promotion."""
        self._close_all_callback = callback

    def set_scan_state(self, scan_state: dict) -> None:
        """Set reference to the shared scan_state dict."""
        self._scan_state = scan_state

    def _extract_json(self, response: str) -> dict | None:
        """Extract JSON object from AI response text.

        Handles responses that wrap JSON in explanatory text.
        Uses brace-depth tracking to find the outermost JSON object.
        """
        # Try direct parse first (entire response is JSON)
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            pass

        # Find the first { and walk to its matching }
        start = response.find("{")
        if start < 0:
            return None

        depth = 0
        in_string = False
        escape_next = False
        for i in range(start, len(response)):
            c = response[i]
            if escape_next:
                escape_next = False
                continue
            if in_string:
                if c == "\\":
                    escape_next = True
                    continue
                if c == '"':
                    in_string = False
                continue
            if c == '"':
                in_string = True
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(response[start : i + 1])
                    except json.JSONDecodeError:
                        return None
        return None

    @staticmethod
    def _normalize_decisions(parsed: dict) -> dict:
        """Normalize response to always have a decisions list.

        Handles backward compatibility: if LLM returns old single-decision
        format, wraps it in a list.
        """
        if "decisions" in parsed and isinstance(parsed["decisions"], list):
            return parsed  # Already new format
        # Old format: single decision — move action-specific fields into a decisions list
        action_fields = {
            "decision", "slot", "replace_slot", "specific_changes",
            "strategy_characterization", "evaluation_duration_days", "position_handling",
        }
        single = {k: parsed.pop(k) for k in list(parsed.keys()) if k in action_fields}
        if not single.get("decision"):
            single["decision"] = "NO_CHANGE"
        parsed["decisions"] = [single]
        return parsed

    async def _store_thought(
        self,
        step: str,
        model: str,
        input_summary: str,
        full_response: str,
        parsed_result=None,
    ) -> None:
        """Store an AI response in the thought spool for later browsing."""
        if not self._cycle_id:
            return
        try:
            await self._db.execute(
                """INSERT INTO orchestrator_thoughts
                   (cycle_id, step, model, input_summary, full_response, parsed_result)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    self._cycle_id,
                    step,
                    model,
                    input_summary,
                    full_response,
                    json.dumps(parsed_result, default=str)
                    if parsed_result is not None
                    else None,
                ),
            )
            await self._db.commit()

            # Emit to structlog for Loki/Grafana spool
            if step == "generate" or step.startswith("code_gen") or step.startswith("analysis_gen"):
                display = "[GENERATED CODE]"
            elif parsed_result:
                display = str(parsed_result)
            else:
                display = full_response or ""
            log.info("orchestrator.thought_stored",
                     step=step, model=model,
                     display=display,
                     detail=json.dumps(parsed_result, default=str) if parsed_result else "")
        except Exception as e:
            log.warning("orchestrator.store_thought_failed", step=step, error=str(e))

    async def run_nightly_cycle(self, trigger: str = "scheduled") -> str:
        """Execute the full orchestration cycle. Returns report summary."""
        if self._cycle_lock.locked():
            log.warning("orchestrator.already_running")
            return "Orchestrator: Skipped — cycle already in progress."
        async with self._cycle_lock:
            return await self._run_nightly_cycle_locked(trigger=trigger)

    async def _run_nightly_cycle_locked(self, trigger: str = "scheduled") -> str:
        from src.orchestrator.cycle import CycleState, OrchestrationCycle
        from src.orchestrator.phases import (
            ReflectPhase, ObservePhase, EvaluatePhase, DecidePhase, ExecutePhase,
        )

        self._running = True
        self._cycle_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        log.info("orchestrator.cycle_start", cycle_id=self._cycle_id)

        if self._notifier:
            await self._notifier.orchestrator_cycle_started()

        try:
            # --- Pre-phase: budget gate ---
            if self._ai.tokens_remaining < 200000:
                log.warning(
                    "orchestrator.insufficient_budget",
                    remaining=self._ai.tokens_remaining,
                )
                if self._notifier:
                    await self._notifier.orchestrator_cycle_completed(
                        "SKIPPED_BUDGET",
                        strategy_version=None,
                        candidate_count=len(self._candidate_manager.get_active_slots()) if self._candidate_manager else 0,
                        max_candidates=self._config.orchestrator.max_candidates,
                    )
                return "Orchestrator: Skipped — insufficient token budget remaining."

            # --- Pre-phase: compute reflection_due flag ---
            state = CycleState(cycle_id=self._cycle_id, trigger=trigger)
            state.context["reflection_due"] = await self._should_reflect()

            # --- Run phases ---
            phases = [
                ReflectPhase(),
                ObservePhase(),
                EvaluatePhase(),
                DecidePhase(),
                ExecutePhase(),
            ]
            cycle = OrchestrationCycle(phases, self)
            state = await cycle.run(state)

            # --- Post-phase: build report ---
            reports = state.reports
            report = " | ".join(reports) if len(reports) > 1 else (reports[0] if reports else "No actions.")

            # --- Post-phase: store observations & predictions ---
            parsed = state.context.get("analysis_parsed", {})
            await self._store_observation(parsed)
            if self._notifier:
                await self._notifier.orchestrator_observation(
                    market_observations=parsed.get("market_observations", ""),
                    reasoning=parsed.get("reasoning", ""),
                    cross_reference_findings=parsed.get("cross_reference_findings", ""),
                    doc_flag=bool(parsed.get("doc_flag")),
                    flag_reason=parsed.get("flag_reason") or "",
                )
            await self._store_predictions(parsed)

            # --- Post-phase: data maintenance ---
            await self._data_store.run_nightly_maintenance()

            # --- Post-phase: completion notification ---
            decisions_list = state.decisions
            decision_types = ", ".join(
                str(a.get("decision") or "NO_CHANGE").strip().upper()
                for a in decisions_list
            )
            log.info("orchestrator.cycle_complete", decisions=decision_types)
            if self._notifier:
                ver_row = await self._db.fetchone(
                    "SELECT version FROM strategy_versions WHERE deployed_at IS NOT NULL ORDER BY deployed_at DESC LIMIT 1"
                )
                strat_ver = ver_row["version"] if ver_row else None
                cand_count = len(self._candidate_manager.get_active_slots()) if self._candidate_manager else 0
                max_cands = self._config.orchestrator.max_candidates
                await self._notifier.orchestrator_cycle_completed(
                    decision_types,
                    strategy_version=strat_ver,
                    candidate_count=cand_count,
                    max_candidates=max_cands,
                )
            return report

        except Exception as e:
            log.error("orchestrator.cycle_failed", error=str(e), exc_info=True)
            if self._notifier:
                await self._notifier.system_error(f"Orchestrator cycle failed: {e}")
            raise
        finally:
            self._running = False

    async def _gather_since_last_cycle(self) -> dict | None:
        """Gather feedback about what happened since the last orchestrator cycle.

        Queries orchestrator_log, signals, candidate_signals, scan_results,
        and activity_log to build a structured summary. Returns None on first run.
        """
        # A. Last decision
        last = await self._db.fetchone(
            "SELECT action, outcome, strategy_version_to, tokens_used, cost_usd, created_at "
            "FROM orchestrator_log ORDER BY id DESC LIMIT 1"
        )
        if not last:
            return None

        since_ts = last["created_at"]  # UTC timestamp of last cycle

        # Hours since last cycle
        try:
            last_dt = datetime.strptime(since_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            # Fallback for date-only format
            try:
                last_dt = datetime.strptime(since_ts, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                last_dt = datetime.now(timezone.utc)
        hours_since = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600

        # B. Active strategy signals since last cycle
        sig_totals = await self._db.fetchone(
            "SELECT COUNT(*) as total, "
            "SUM(CASE WHEN acted_on = 1 THEN 1 ELSE 0 END) as executed, "
            "SUM(CASE WHEN rejected_reason IS NOT NULL THEN 1 ELSE 0 END) as rejected "
            "FROM signals WHERE created_at >= ?",
            (since_ts,),
        )
        rejection_rows = await self._db.fetchall(
            "SELECT rejected_reason, COUNT(*) as count "
            "FROM signals WHERE created_at >= ? AND rejected_reason IS NOT NULL "
            "GROUP BY rejected_reason",
            (since_ts,),
        )
        rejection_reasons = {r["rejected_reason"]: r["count"] for r in rejection_rows}

        # C. Candidate signals since last cycle
        cand_sig_rows = await self._db.fetchall(
            "SELECT candidate_slot, COUNT(*) as total, "
            "SUM(CASE WHEN acted_on = 1 THEN 1 ELSE 0 END) as executed "
            "FROM candidate_signals WHERE created_at >= ? "
            "GROUP BY candidate_slot",
            (since_ts,),
        )
        candidate_signals = {
            r["candidate_slot"]: {"total": r["total"], "executed": r["executed"] or 0}
            for r in cand_sig_rows
        }

        # D. Scan count
        scan_row = await self._db.fetchone(
            "SELECT COUNT(*) as count FROM scan_results WHERE created_at >= ?",
            (since_ts,),
        )
        total_scans = scan_row["count"] if scan_row else 0

        # E. Activity log (exclude SCAN category — too noisy)
        events = await self._db.fetchall(
            "SELECT timestamp, category, severity, summary "
            "FROM activity_log "
            "WHERE timestamp >= ? AND category != 'SCAN' "
            "ORDER BY id ASC LIMIT 50",
            (since_ts,),
        )
        events = [dict(e) for e in events]

        # F. Event summary
        errors = sum(1 for e in events if e["severity"] == "error")
        warnings = sum(1 for e in events if e["severity"] == "warning")
        risk_events = sum(1 for e in events if e["category"] == "RISK")
        restarts = sum(1 for e in events if e["category"] == "SYSTEM" and "restart" in (e["summary"] or "").lower())

        return {
            "last_decision": {
                "action": last["action"],
                "outcome": last["outcome"] or "",
                "created_at": since_ts,
                "tokens_used": last["tokens_used"] or 0,
                "cost_usd": last["cost_usd"] or 0,
            },
            "hours_since_last_cycle": round(hours_since, 1),
            "scanning": {
                "total_scans": total_scans,
                "active_signals": {
                    "total": sig_totals["total"] or 0,
                    "executed": sig_totals["executed"] or 0,
                    "rejected": sig_totals["rejected"] or 0,
                },
                "rejection_reasons": rejection_reasons,
                "candidate_signals": candidate_signals,
            },
            "events": events,
            "event_summary": {
                "errors": errors,
                "warnings": warnings,
                "risk_events": risk_events,
                "restarts": restarts,
            },
        }

    async def _gather_context(self) -> dict:
        """Collect all context needed for analysis.

        Gathers five categories of inputs:
        1. Ground truth (rigid shell benchmarks)
        2. Market analysis (flexible module output)
        3. Trade performance (flexible module output)
        4. Strategy context (code, doc, versions)
        5. Operational context (tokens, system age)
        """

        # --- 1. GROUND TRUTH (rigid shell, orchestrator cannot modify) ---
        try:
            ground_truth = await compute_truth_benchmarks(self._db)
        except Exception as e:
            log.error("orchestrator.truth_benchmarks_failed", error=str(e))
            ground_truth = {"error": str(e)}

        schema = get_schema_description()

        # --- 2. MARKET ANALYSIS (flexible module, orchestrator can rewrite) ---
        try:
            market_module = load_analysis_module("market_analysis")
            ro_db = ReadOnlyDB(self._db.conn)
            market_report = await asyncio.wait_for(
                market_module.analyze(ro_db, schema), timeout=30,
            )
        except asyncio.TimeoutError:
            log.error("orchestrator.market_analysis_timeout")
            market_report = {"error": "Analysis module timed out (>30s)"}
        except Exception as e:
            log.error("orchestrator.market_analysis_failed", error=str(e))
            market_report = {"error": str(e)}

        # --- 3. TRADE PERFORMANCE (flexible module, orchestrator can rewrite) ---
        try:
            perf_module = load_analysis_module("trade_performance")
            ro_db = ReadOnlyDB(self._db.conn)
            perf_report = await asyncio.wait_for(
                perf_module.analyze(ro_db, schema), timeout=30,
            )
        except asyncio.TimeoutError:
            log.error("orchestrator.trade_performance_timeout")
            perf_report = {"error": "Analysis module timed out (>30s)"}
        except Exception as e:
            log.error("orchestrator.trade_performance_failed", error=str(e))
            perf_report = {"error": str(e)}

        # --- 4. STRATEGY CONTEXT ---
        # Current strategy code
        strategy_path = get_strategy_path()
        strategy_code = (
            strategy_path.read_text() if strategy_path.exists() else "No strategy file"
        )
        code_hash = get_code_hash(strategy_path) if strategy_path.exists() else "none"

        # Current analysis module code (so orchestrator can see what it wrote)
        market_analysis_code = ""
        trade_performance_code = ""
        try:
            market_path = get_module_path("market_analysis")
            market_analysis_code = (
                market_path.read_text() if market_path.exists() else "No module"
            )
        except Exception:
            market_analysis_code = "Failed to read"
        try:
            perf_path = get_module_path("trade_performance")
            trade_performance_code = (
                perf_path.read_text() if perf_path.exists() else "No module"
            )
        except Exception:
            trade_performance_code = "Failed to read"

        # Strategy document
        strategy_doc = (
            STRATEGY_DOC_PATH.read_text()
            if STRATEGY_DOC_PATH.exists()
            else "No strategy document"
        )

        # Performance data (wrapped for graceful degradation)
        try:
            performance = await self._reporter.strategy_performance(days=7)
        except Exception as e:
            log.warning("orchestrator.context_error", section="performance", error=str(e))
            performance = {}

        try:
            daily_perf = await self._db.fetchall(
                "SELECT * FROM daily_performance ORDER BY date DESC LIMIT 7"
            )
        except Exception as e:
            log.warning("orchestrator.context_error", section="daily_perf", error=str(e))
            daily_perf = []

        try:
            trades = await self._db.fetchall(
                "SELECT symbol, side, pnl, pnl_pct, fees, intent, strategy_regime, closed_at FROM trades "
                "WHERE closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT 50"
            )
        except Exception as e:
            log.warning("orchestrator.context_error", section="trades", error=str(e))
            trades = []

        try:
            versions = await self._db.fetchall(
                "SELECT version, description, backtest_result, market_conditions "
                "FROM strategy_versions ORDER BY created_at DESC LIMIT 10"
            )
        except Exception as e:
            log.warning("orchestrator.context_error", section="versions", error=str(e))
            versions = []

        # --- 5. OPERATIONAL CONTEXT ---
        try:
            usage = await self._ai.get_daily_usage()
        except Exception as e:
            log.warning("orchestrator.context_error", section="usage", error=str(e))
            usage = {"models": {}, "total_cost": 0, "daily_limit": 0, "used": 0}

        # --- 6. CANDIDATE STRATEGIES ---
        candidate_context = []
        if self._candidate_manager:
            try:
                candidate_context = await self._candidate_manager.get_context_for_orchestrator()
            except Exception as e:
                log.warning("orchestrator.context_error", section="candidates", error=str(e))

        # Signal drought detection
        try:
            last_signal = await self._db.fetchone(
                "SELECT created_at FROM signals ORDER BY created_at DESC LIMIT 1"
            )
            signals_7d = await self._db.fetchone(
                "SELECT COUNT(*) as count FROM signals WHERE created_at >= datetime('now', '-7 days')"
            )
            signals_30d = await self._db.fetchone(
                "SELECT COUNT(*) as count FROM signals WHERE created_at >= datetime('now', '-30 days')"
            )
            scans_24h = await self._db.fetchone(
                "SELECT COUNT(*) as count FROM scan_results WHERE created_at >= datetime('now', '-1 day')"
            )
            drought_info = {
                "last_signal_at": last_signal["created_at"] if last_signal else None,
                "signals_last_7d": signals_7d["count"] if signals_7d else 0,
                "signals_last_30d": signals_30d["count"] if signals_30d else 0,
                "scans_last_24h": scans_24h["count"] if scans_24h else 0,
            }
        except Exception as e:
            log.warning("orchestrator.context_error", section="drought", error=str(e))
            drought_info = {"last_signal_at": None, "signals_last_7d": 0, "signals_last_30d": 0, "scans_last_24h": 0}

        try:
            interval = self._config.orchestrator.reflection_interval_days
            recent_observations = await self._db.fetchall(
                f"""SELECT date, market_summary, strategy_assessment, notable_findings
                   FROM orchestrator_observations
                   WHERE date >= date('now', '-{interval} days')
                   ORDER BY date DESC"""
            )
        except Exception as e:
            log.warning("orchestrator.context_error", section="observations", error=str(e))
            recent_observations = []

        # --- 7. SINCE LAST CYCLE (decision feedback loop) ---
        try:
            since_last_cycle = await self._gather_since_last_cycle()
        except Exception as e:
            log.warning("orchestrator.context_error", section="since_last_cycle", error=str(e))
            since_last_cycle = None

        return {
            # Ground truth (rigid)
            "ground_truth": ground_truth,
            # Analysis modules (flexible, orchestrator-designed)
            "market_report": market_report,
            "trade_performance_report": perf_report,
            # Analysis module source code (for rewriting)
            "market_analysis_code": market_analysis_code,
            "trade_performance_code": trade_performance_code,
            # Strategy context
            "strategy_code": strategy_code,
            "code_hash": code_hash,
            "strategy_doc": strategy_doc,
            "performance_7d": performance,
            "daily_performance": [dict(p) for p in daily_perf],
            "recent_trades": [dict(t) for t in trades],
            "version_history": [dict(v) for v in versions],
            # Operational
            "token_usage": usage,
            "candidates": candidate_context,
            "recent_observations": [dict(o) for o in recent_observations],
            "signal_drought": drought_info,
            "since_last_cycle": since_last_cycle,
        }

    async def _build_time_context(self, trigger: str = "scheduled") -> str:
        """Build a timestamp header for Opus prompts."""
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(self._config.timezone)
        now_utc = datetime.now(timezone.utc)
        now_local = now_utc.astimezone(tz)
        tz_abbrev = now_local.strftime("%Z")

        # Last orchestration cycle
        row = await self._db.fetchone(
            "SELECT date, cycle_id FROM orchestrator_observations ORDER BY id DESC LIMIT 1"
        )
        if row:
            # cycle_id format: YYYYMMDD_HHMMSS (local time)
            cid = row["cycle_id"]
            last_local = datetime.strptime(cid, "%Y%m%d_%H%M%S").replace(tzinfo=tz)
            last_utc = last_local.astimezone(timezone.utc)
            delta = now_utc - last_utc
            hours, remainder = divmod(int(delta.total_seconds()), 3600)
            minutes = remainder // 60
            last_line = (
                f"Last orchestration: {last_local.strftime('%Y-%m-%d %H:%M')} {tz_abbrev}"
                f" ({last_utc.strftime('%Y-%m-%d %H:%M')} UTC)"
                f" — {hours}h {minutes}m ago"
            )
        else:
            last_line = "Last orchestration: none (first cycle)"

        # Fund age from ground truth
        first_scan = await self._db.fetchone(
            "SELECT MIN(created_at) as first_scan FROM scan_results"
        )
        if first_scan and first_scan["first_scan"]:
            try:
                started = datetime.strptime(
                    first_scan["first_scan"][:19], "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=timezone.utc)
                fund_age_days = (now_utc - started).days
                fund_age_line = f"Fund age: {fund_age_days} days (started {started.strftime('%Y-%m-%d')})"
            except (ValueError, TypeError):
                fund_age_line = "Fund age: unknown"
        else:
            fund_age_line = "Fund age: 0 days (no scans yet)"

        # Cycles today
        cycles_row = await self._db.fetchone(
            "SELECT COUNT(DISTINCT cycle_id) as count FROM orchestrator_log WHERE date = date('now')"
        )
        cycles_today = ((cycles_row["count"] or 0) if cycles_row else 0) + 1

        return (
            f"## CURRENT TIME\n"
            f"Local ({self._config.timezone}): {now_local.strftime('%Y-%m-%d %H:%M')} {tz_abbrev}\n"
            f"UTC: {now_utc.strftime('%Y-%m-%d %H:%M')} UTC\n"
            f"Trigger: {trigger}\n"
            f"{fund_age_line}\n"
            f"Cycles today: {cycles_today} (including this one)\n"
            f"{last_line}"
        )

    @staticmethod
    def _format_since_last_cycle(since: dict) -> str:
        """Format the since-last-cycle feedback section for the analysis prompt."""
        ld = since["last_decision"]
        hours = since["hours_since_last_cycle"]
        h = int(hours)
        m = int((hours - h) * 60)

        lines = ["---", "", "## SINCE YOUR LAST CYCLE", ""]

        # Last decision
        lines.append(f"Last decision: {ld['action']}")
        if ld["outcome"]:
            lines.append(f"  Outcome: {ld['outcome']}")
        lines.append(f"  Time: {ld['created_at']} UTC ({h}h {m}m ago)")
        lines.append(f"  Cost: ${ld['cost_usd']:.2f} ({ld['tokens_used']:,} tokens)")
        lines.append("")

        # Scanning
        sc = since["scanning"]
        lines.append(f"Scanning: {sc['total_scans']} scans since last cycle")
        active = sc["active_signals"]
        lines.append(f"  Active strategy: {active['total']} signals ({active['executed']} executed, {active['rejected']} rejected)")
        for slot, counts in sorted(sc.get("candidate_signals", {}).items()):
            rejected = counts["total"] - counts["executed"]
            if rejected > 0:
                lines.append(f"  Candidate slot {slot}: {counts['total']} signals ({counts['executed']} executed, {rejected} rejected)")
            else:
                lines.append(f"  Candidate slot {slot}: {counts['total']} signals ({counts['executed']} executed)")
        if sc["rejection_reasons"]:
            reasons = ", ".join(f"{r} ({c})" for r, c in sc["rejection_reasons"].items())
            lines.append(f"  Rejection reasons: {reasons}")
        lines.append("")

        # System events
        events = since["events"]
        if events:
            lines.append("System events (excluding routine scans):")
            for e in events:
                lines.append(f"  [{e['timestamp']}] {e['category']} {e['severity']} — {e['summary']}")
            summary = since["event_summary"]
            lines.append(f"  Summary: {summary['errors']} errors, {summary['warnings']} warnings, {summary['restarts']} restarts")
        else:
            lines.append("System events: none")

        return "\n".join(lines)

    async def _analyze(self, context: dict, trigger: str = "scheduled") -> dict:
        """Opus analyzes performance and decides on action."""
        time_context = await self._build_time_context(trigger=trigger)

        # Build since-last-cycle section (empty string if first run)
        since = context.get("since_last_cycle")
        since_section = self._format_since_last_cycle(since) if since else ""

        prompt = f"""{time_context}
{since_section}

Current fund state for review.

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

## YOUR STRATEGY (you designed this — you can rewrite it)
### Strategy Source Code:
```python
{context["strategy_code"]}
```

### Strategy Document (Institutional Memory):
{context["strategy_doc"]}

### Performance (Last 7 Days):
{json.dumps(context["performance_7d"], indent=2, default=str)}

### Daily Performance Snapshots:
{json.dumps(context["daily_performance"], indent=2, default=str)}

### Recent Trades (Last 50):
{json.dumps(context["recent_trades"], indent=2, default=str)}

### Strategy Version History:
{json.dumps(context["version_history"], indent=2, default=str)}

---

## SYSTEM CONSTRAINTS (you cannot change these)
- Trading pairs: {", ".join(self._config.symbols)}
- System: Long-only (no short selling, no leverage)
- Maker fee: {self._config.kraken.maker_fee_pct}% / Taker fee: {self._config.kraken.taker_fee_pct}%
- Default slippage: {self._config.default_slippage_factor * 100:.2f}% (signals can override per-trade)
- Max trade size: {self._config.risk.max_trade_pct * 100:.0f}% of portfolio
- Default trade size: {self._config.risk.default_trade_pct * 100:.0f}% of portfolio
- Max position size: {self._config.risk.max_position_pct * 100:.0f}% of portfolio
- Max positions: {self._config.risk.max_positions}
- Max daily loss: {self._config.risk.max_daily_loss_pct * 100:.0f}% of portfolio (trading halts)
- Max drawdown: {self._config.risk.max_drawdown_pct * 100:.0f}% from peak (system halts)
- Consecutive loss halt: {self._config.risk.rollback_consecutive_losses} consecutive losses (persists across days)
- Max candidate slots: {self._config.orchestrator.max_candidates}
- Token budget: {context["token_usage"].get("used", 0)} / {context["token_usage"].get("daily_limit", 0)} tokens used today (${context["token_usage"].get("total_cost", 0):.4f})

---

## CANDIDATE STRATEGIES
{json.dumps(context.get("candidates", []), indent=2, default=str) if context.get("candidates") else "No active candidates. All slots available."}

---

## SIGNAL & OBSERVATION STATE
### Signal Drought Detection:
{json.dumps(context["signal_drought"], indent=2, default=str)}

### Recent Observations (last {self._config.orchestrator.reflection_interval_days} days):
{json.dumps(context["recent_observations"], indent=2, default=str) if context["recent_observations"] else "No prior observations."}

---

Respond in JSON format."""

        # Build system prompt from three-layer framework
        system_prompt = (
            f"{LAYER_1_IDENTITY}\n\n---\n\n{FUND_MANDATE}\n\n---\n\n{LAYER_2_SYSTEM}"
        )

        response = await self._ai.ask_opus(
            prompt, system=system_prompt, purpose="nightly_analysis"
        )

        # Parse JSON from response
        parsed = self._extract_json(response)
        if parsed is None:
            log.warning("orchestrator.json_parse_failed", response=response)
            parsed = {
                "decision": "NO_CHANGE",
                "reasoning": "Failed to parse analysis response",
            }

        await self._store_thought("analysis", "opus", prompt, response, parsed)
        return parsed

    async def _create_candidate(self, decision: dict, context: dict) -> str:
        """Create a candidate strategy with nested loops (same pipeline as old _execute_change).

        Instead of deploying to active strategy, deploys to a candidate slot.
        """
        if not self._candidate_manager:
            return "Cannot create candidate: no candidate manager."

        # Pick slot
        slot = self._pick_candidate_slot(decision)
        if slot is None:
            return "Cannot create candidate: all slots full and no replace_slot specified."

        changes = str(decision.get("specific_changes") or "")
        original_changes = changes
        max_inner = self._config.orchestrator.max_revisions
        max_outer = self._config.orchestrator.max_strategy_iterations
        eval_days = decision.get("evaluation_duration_days")

        system_constraints = (
            f"## System Constraints\n"
            f"- Trading pairs: {', '.join(self._config.symbols)}\n"
            f"- Long-only (no short selling, no leverage)\n"
            f"- Maker fee: {self._config.kraken.maker_fee_pct}% / Taker fee: {self._config.kraken.taker_fee_pct}%\n"
            f"- Default slippage: {self._config.default_slippage_factor * 100:.2f}%\n"
            f"- Max trade size: {self._config.risk.max_trade_pct * 100:.0f}% of portfolio\n"
            f"- Default trade size: {self._config.risk.default_trade_pct * 100:.0f}% of portfolio\n"
            f"- Max positions: {self._config.risk.max_positions}\n"
            f"- Max position per symbol: {self._config.risk.max_position_pct * 100:.0f}% of portfolio\n"
            f"- SymbolData includes maker_fee_pct and taker_fee_pct per pair\n"
            f"- Signal supports optional slippage_tolerance override (float)"
        )

        attempt_history = []

        for outer in range(max_outer):
            approved_code = None
            diff = None
            inner_changes = changes

            for inner in range(max_inner):
                gen_prompt = f"""Generate a new trading strategy based on these requirements:

## Change Request
{inner_changes}

## Current Strategy (for reference)
```python
{context["strategy_code"]}
```

## Strategy Document
{context["strategy_doc"]}

## Performance Context
{json.dumps(context["performance_7d"], indent=2, default=str)}

{system_constraints}

Generate the complete strategy.py file."""

                code = await self._ai.ask_sonnet(
                    gen_prompt,
                    system=CODE_GEN_SYSTEM,
                    purpose=f"candidate_gen_outer{outer + 1}_inner{inner + 1}",
                )
                await self._store_thought(
                    f"candidate_gen_o{outer + 1}_i{inner + 1}", "sonnet", gen_prompt, code
                )

                # Strip markdown code fences
                fence_match = re.search(r'```(?:python)?\s*\n(.*?)```', code, re.DOTALL | re.IGNORECASE)
                if fence_match:
                    code = fence_match.group(1)
                code = code.strip()

                # Sandbox
                sandbox_result = validate_strategy(code)
                if not sandbox_result.passed:
                    log.warning("orchestrator.candidate_sandbox_failed",
                                outer=outer + 1, inner=inner + 1, errors=sandbox_result.errors)
                    inner_changes += f"\n\nPrevious attempt failed sandbox: {sandbox_result.errors}. Fix these issues."
                    continue

                # Diff
                old_lines = context["strategy_code"].splitlines(keepends=True)
                new_lines = code.splitlines(keepends=True)
                diff = "".join(difflib.unified_diff(old_lines, new_lines, fromfile="current", tofile="proposed", n=3))

                # Code review — use current changes (includes revision instructions
                # from prior outer iterations) so the reviewer evaluates against
                # the actual goal, not the stale original decision.
                review_context = {
                    "decision": decision.get("decision"),
                    "current_instructions": inner_changes,
                    "original_reasoning": decision.get("reasoning", ""),
                }
                review_prompt = f"""Review this trading strategy code for correctness and safety.

## Changes from current strategy (diff)
```diff
{diff if diff else "(no textual diff — code may be identical)"}
```

## Full proposed code
```python
{code}
```

This is a candidate strategy that will run in paper simulation alongside the active strategy.

## What the code was asked to implement
{inner_changes}

## Original decision context
{json.dumps(review_context, indent=2, default=str)}"""

                review_response = await self._ai.ask_opus(
                    review_prompt, system=CODE_REVIEW_SYSTEM,
                    purpose=f"candidate_review_outer{outer + 1}_inner{inner + 1}",
                )
                review = self._extract_json(review_response)
                if review is None:
                    review = {"approved": False, "feedback": "Failed to parse review"}

                await self._store_thought(
                    f"candidate_review_o{outer + 1}_i{inner + 1}", "opus",
                    review_prompt, review_response, review,
                )

                if review.get("approved"):
                    approved_code = code
                    break
                else:
                    feedback = review.get("feedback", "No feedback")
                    issues = review.get("issues", [])
                    log.warning("orchestrator.candidate_review_rejected",
                                outer=outer + 1, inner=inner + 1, feedback=feedback)
                    inner_changes += f"\n\nCode review feedback: {feedback}\nIssues: {issues}"

            if approved_code is None:
                log.warning("orchestrator.candidate_code_quality_exhausted", outer=outer + 1)
                return f"Candidate creation aborted: code quality failed after {max_inner} attempts."

            # Backtest
            backtest_passed, backtest_summary, backtest_result = await self._run_backtest(approved_code)
            if not backtest_passed:
                attempt_history.append({"attempt": outer + 1, "outcome": "backtest_crash", "summary": backtest_summary})
                changes = f"Original goal: {original_changes}\n\nPrevious attempt crashed during backtest: {backtest_summary}. Try a different approach."
                continue

            # Opus reviews backtest — pass current changes so reviewer knows
            # what was actually attempted (may differ from original decision)
            bt_review = await self._review_backtest(backtest_result, backtest_summary, decision, diff, attempt_history, current_changes=changes)

            if bt_review.get("deploy", False):
                # Deploy to candidate slot
                version = f"v{datetime.now().strftime('%Y%m%d_%H%M%S')}_candidate"

                # Build portfolio snapshot from fund state
                snapshot = await self._get_portfolio_snapshot()

                # Get fund positions for cloning
                fund_positions = await self._db.fetchall("SELECT * FROM positions")
                initial_positions = [dict(p) for p in fund_positions]

                await self._candidate_manager.create_candidate(
                    slot=slot,
                    code=approved_code,
                    version=version,
                    description=changes[:500],
                    backtest_summary=backtest_summary[:2000] if backtest_summary else "",
                    evaluation_duration_days=eval_days,
                    portfolio_snapshot=snapshot,
                    initial_positions=initial_positions,
                )

                # Record in strategy_versions (not deployed — candidate only)
                from src.strategy.loader import hash_code_string
                code_hash = hash_code_string(approved_code)
                characterization = decision.get("strategy_characterization", "")
                desc = characterization if characterization else f"Candidate slot {slot}: {changes[:200]}"
                await self._db.execute(
                    """INSERT INTO strategy_versions
                       (version, code_hash, description, backtest_result, market_conditions, code)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (version, code_hash, desc,
                     backtest_summary, decision.get("market_observations", ""), approved_code),
                )
                await self._db.commit()

                eval_str = f"{eval_days}d" if eval_days else "indefinite"
                return f"Candidate deployed to slot {slot} as {version} (evaluation: {eval_str})."

            # Rejected
            reasoning = bt_review.get("reasoning", "No reasoning")
            revision = bt_review.get("revision_instructions", "")
            attempt_history.append({"attempt": outer + 1, "outcome": "rejected",
                                    "backtest_summary": backtest_summary, "reasoning": reasoning})

            if revision:
                changes = f"Original goal: {original_changes}\n\nRevision from fund manager (attempt {outer + 1}): {revision}"
            else:
                changes = f"{original_changes}\n\nPrevious backtest rejected: {reasoning}. Try a different approach."

        return f"Candidate creation aborted after {max_outer} strategy iterations."

    async def _cancel_candidate(self, decision: dict) -> str:
        """Cancel a running candidate strategy."""
        slot = decision.get("slot")
        if not slot or not self._candidate_manager:
            return "Cannot cancel: invalid slot or no candidate manager."
        try:
            slot = int(slot)
        except (TypeError, ValueError):
            return f"Cannot cancel: invalid slot '{slot}'."
        active = self._candidate_manager.get_active_slots()
        if slot not in active:
            return f"Cannot cancel: slot {slot} has no running candidate."
        await self._candidate_manager.cancel_candidate(slot, decision.get("reasoning", ""))
        return f"Candidate in slot {slot} canceled."

    async def _promote_candidate(self, decision: dict) -> str:
        """Promote a candidate to become the active strategy."""
        slot = decision.get("slot")
        if not slot or not self._candidate_manager:
            return "Cannot promote: invalid slot or no candidate manager."
        try:
            slot = int(slot)
        except (TypeError, ValueError):
            return f"Cannot promote: invalid slot '{slot}'."
        active = self._candidate_manager.get_active_slots()
        if slot not in active:
            return f"Cannot promote: slot {slot} has no running candidate."

        position_handling = decision.get("position_handling", "keep")

        # Close all fund positions if requested
        if position_handling == "close_all" and self._close_all_callback:
            await self._close_all_callback()

        # Retrieve candidate's characterization before promotion clears runners
        candidate_version = self._candidate_manager._runners[slot].version if slot in self._candidate_manager._runners else None
        candidate_desc = None
        if candidate_version:
            row = await self._db.fetchone(
                "SELECT description FROM strategy_versions WHERE version = ?",
                (candidate_version,),
            )
            if row:
                candidate_desc = row["description"]

        # Get code and promote (cancels all candidates)
        code = await self._candidate_manager.promote_candidate(slot)

        # Deploy to active strategy file
        version = f"v{datetime.now().strftime('%Y%m%d_%H%M%S')}_promoted"
        code_hash = deploy_strategy(code, version)

        # Record in strategy_versions as deployed — carry characterization from candidate
        desc = candidate_desc or f"Promoted from candidate slot {slot}"
        await self._db.execute(
            """INSERT INTO strategy_versions
               (version, code_hash, description, deployed_at, code)
               VALUES (?, ?, ?, datetime('now'), ?)""",
            (version, code_hash, desc, code),
        )
        await self._db.commit()

        # Signal main.py to reload strategy
        if self._scan_state:
            self._scan_state["strategy_reload_needed"] = True

        return f"Candidate from slot {slot} promoted as {version}. Position handling: {position_handling}."

    def _pick_candidate_slot(self, decision: dict) -> int | None:
        """Find an available candidate slot."""
        active = set(self._candidate_manager.get_active_slots())
        max_slots = self._config.orchestrator.max_candidates
        for i in range(1, max_slots + 1):
            if i not in active:
                return i
        replace = decision.get("replace_slot")
        if replace:
            try:
                replace = int(replace)
            except (TypeError, ValueError):
                return None
            if 1 <= replace <= max_slots:
                return replace
        return None

    async def _get_portfolio_snapshot(self) -> dict:
        """Snapshot fund portfolio for candidate initialization."""
        # Try to get cash from portfolio tracker state via scan_state
        # Fall back to paper_balance from config
        cash = self._config.paper_balance_usd

        # Query current portfolio value from daily_performance or positions
        positions = await self._db.fetchall("SELECT * FROM positions")
        pos_value = sum(
            (p.get("current_price") or p["avg_entry"]) * p["qty"]
            for p in positions
        )

        # Try to get actual cash from system_meta
        meta_row = await self._db.fetchone(
            "SELECT value FROM system_meta WHERE key = 'paper_starting_capital'"
        )
        if meta_row:
            try:
                starting = float(meta_row["value"])
                # Rough cash estimate: starting + trade PnL - position cost
                pnl_row = await self._db.fetchone(
                    "SELECT COALESCE(SUM(pnl), 0) as total_pnl FROM trades WHERE pnl IS NOT NULL"
                )
                total_pnl = pnl_row["total_pnl"] if pnl_row else 0
                cash = starting + total_pnl - pos_value
                cash = max(0, cash)
            except (ValueError, TypeError):
                pass

        return {
            "cash": round(cash, 2),
            "positions": [dict(p) for p in positions],
            "total_value": round(cash + pos_value, 2),
        }

    async def _execute_analysis_change(self, decision: dict, context: dict) -> str:
        """Execute an analysis module update: generate -> review -> sandbox -> deploy.

        No paper testing required — analysis modules are read-only.
        """
        decision_type = str(decision.get("decision") or "").strip().upper()
        module_name = (
            "market_analysis"
            if decision_type == "MARKET_ANALYSIS_UPDATE"
            else "trade_performance"
        )
        changes = str(decision.get("specific_changes") or "")
        current_code = context.get(
            "market_analysis_code"
            if module_name == "market_analysis"
            else "trade_performance_code",
            "",
        )
        max_revisions = self._config.orchestrator.max_revisions

        for attempt in range(max_revisions):
            # Sonnet generates analysis module code
            gen_prompt = f"""Generate a new {module_name.replace("_", " ")} module based on these requirements:

## Change Request
{changes}

## Current Module Code (for reference)
```python
{current_code}
```

## Available Database Schema
{json.dumps(get_schema_description(), indent=2)}

## Ground Truth Benchmarks (for context on what data exists)
{json.dumps(context.get("ground_truth", {}), indent=2, default=str)}

Generate the complete {module_name}.py file."""

            code = await self._ai.ask_sonnet(
                gen_prompt,
                system=ANALYSIS_CODE_GEN_SYSTEM,
                purpose=f"analysis_gen_{module_name}_attempt_{attempt + 1}",
            )
            await self._store_thought(
                f"analysis_gen_{module_name}_{attempt + 1}",
                "sonnet",
                gen_prompt,
                code,
            )

            # Strip markdown code fences if present
            fence_match = re.search(r'```(?:python)?\s*\n(.*?)```', code, re.DOTALL | re.IGNORECASE)
            if fence_match:
                code = fence_match.group(1)
            code = code.strip()

            # Sandbox validation
            sandbox_result = validate_analysis_module(code, module_name)
            if not sandbox_result.passed:
                log.warning(
                    "orchestrator.analysis_sandbox_failed",
                    module=module_name,
                    attempt=attempt + 1,
                    errors=sandbox_result.errors,
                )
                changes += f"\n\nPrevious attempt failed sandbox: {sandbox_result.errors}. Fix these issues."
                continue

            # Opus reviews for mathematical correctness
            review_prompt = f"""Review this {module_name.replace("_", " ")} module for mathematical correctness and safety:

```python
{code}
```

The orchestrator wants to change this module because: {changes}"""

            review_response = await self._ai.ask_opus(
                review_prompt,
                system=ANALYSIS_REVIEW_SYSTEM,
                purpose=f"analysis_review_{module_name}_attempt_{attempt + 1}",
            )

            review = self._extract_json(review_response)
            if review is None:
                review = {"approved": False, "feedback": "Failed to parse review"}

            await self._store_thought(
                f"analysis_review_{module_name}_{attempt + 1}",
                "opus",
                review_prompt,
                review_response,
                review,
            )

            if review.get("approved"):
                # Deploy — no paper testing needed (read-only module)
                version = f"v{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                code_hash = deploy_analysis_module(module_name, code, version)

                log.info(
                    "orchestrator.analysis_deployed",
                    module=module_name,
                    version=version,
                    hash=code_hash,
                )

                return (
                    f"Analysis module '{module_name}' updated ({version}).\n"
                    f"Changes: {changes}"
                )
            else:
                feedback = review.get("feedback", "No feedback")
                math_errors = review.get("math_errors", [])
                log.warning(
                    "orchestrator.analysis_review_rejected",
                    module=module_name,
                    attempt=attempt + 1,
                    feedback=feedback,
                )
                changes += (
                    f"\n\nReview feedback: {feedback}\nMath errors: {math_errors}"
                )

        return f"Analysis module '{module_name}' update aborted after {max_revisions} failed attempts."

    async def _run_backtest(self, code: str) -> tuple[bool, str, BacktestResult | None]:
        """Backtest generated strategy against historical data.

        Returns (passed, summary, result). Passes if strategy doesn't crash.
        Opus reviews the results separately to decide deployment.
        """
        tmp_path = None
        try:
            # Load the new strategy from code string
            with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
                f.write(code)
                tmp_path = f.name

            spec = importlib.util.spec_from_file_location("backtest_strategy", tmp_path)
            mod = importlib.util.module_from_spec(spec)

            def _load_and_init():
                spec.loader.exec_module(mod)
                return mod.Strategy()

            try:
                strategy = await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(None, _load_and_init),
                    timeout=10,
                )
            except asyncio.TimeoutError:
                return False, "Strategy module import timed out (>10s) — possible infinite loop at import time"

            # Get multi-timeframe candle data for backtest
            candle_data = {}
            for symbol in self._config.symbols:
                df_5m = await self._data_store.get_candles(symbol, "5m", limit=8640)   # 30 days (Kraken API limit)
                df_1h = await self._data_store.get_candles(symbol, "1h", limit=8760)   # 365 days (indicator context)
                df_1d = await self._data_store.get_candles(symbol, "1d", limit=365)    # 365 days (indicator context)
                if not df_1h.empty:
                    candle_data[symbol] = (df_5m, df_1h, df_1d)

            if not candle_data:
                log.info("orchestrator.backtest_skip", reason="no historical data")
                return True, "Skipped (no historical data yet)", None

            risk_limits = RiskLimits(
                max_trade_pct=self._config.risk.max_trade_pct,
                default_trade_pct=self._config.risk.default_trade_pct,
                max_positions=self._config.risk.max_positions,
                max_daily_loss_pct=self._config.risk.max_daily_loss_pct,
                max_drawdown_pct=self._config.risk.max_drawdown_pct,
                max_position_pct=self._config.risk.max_position_pct,
                max_daily_trades=self._config.risk.max_daily_trades,
                rollback_consecutive_losses=self._config.risk.rollback_consecutive_losses,
            )

            # Pull per-pair fees from DB (live fee schedule from Kraken API)
            per_pair_fees = {}
            try:
                rows = await self._db.fetchall(
                    "SELECT symbol, maker_fee_pct, taker_fee_pct FROM fee_schedule"
                )
                for row in rows:
                    per_pair_fees[row["symbol"]] = (row["maker_fee_pct"], row["taker_fee_pct"])
            except Exception:
                pass  # Fall back to global config fees

            bt = Backtester(
                strategy=strategy,
                risk_limits=risk_limits,
                symbols=self._config.symbols,
                maker_fee_pct=self._config.kraken.maker_fee_pct,
                taker_fee_pct=self._config.kraken.taker_fee_pct,
                starting_cash=self._config.paper_balance_usd,
                per_pair_fees=per_pair_fees,
                slippage_factor=self._config.default_slippage_factor,
            )

            # Run backtest with timeout (60s) to catch infinite loops in AI-generated code
            try:
                result = await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(
                        None, bt.run, candle_data
                    ),
                    timeout=60,
                )
            except asyncio.TimeoutError:
                return False, "Strategy backtest timed out (>60s) — possible infinite loop", None
            summary = result.detailed_summary()
            log.info("orchestrator.backtest_complete", summary=result.summary())

            return True, summary, result

        except Exception as e:
            log.warning("orchestrator.backtest_error", error=str(e))
            return False, f"Strategy crashed during backtest: {e}", None
        finally:
            # Clean up temp file
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            # Clean up leaked module from sys.modules
            sys.modules.pop("backtest_strategy", None)

    async def _review_backtest(
        self, result: BacktestResult | None, summary: str, decision: dict, diff: str,
        attempt_history: list[dict] | None = None,
        *, current_changes: str | None = None,
    ) -> dict:
        """Opus reviews backtest results and decides whether to deploy to candidate slot.

        Returns parsed JSON with 'deploy' bool, 'reasoning', 'concerns', and 'revision_instructions'.
        """
        if result is None:
            return {
                "deploy": True,
                "reasoning": "No historical data available — deploying to candidate slot for live evaluation.",
                "concerns": ["No backtest data to evaluate"],
                "revision_instructions": "",
            }

        # Format attempt history so Opus sees what's been tried
        if attempt_history:
            history_text = "\n".join(
                f"- Attempt {h['attempt']}: {h['outcome']} — {h.get('reasoning') or h.get('summary', '')}"
                for h in attempt_history
            )
        else:
            history_text = "This is the first attempt."

        # Use current_changes if available (reflects revision instructions from
        # prior iterations), otherwise fall back to original decision context
        if current_changes:
            change_context = f"## Current Strategy Instructions (may include revisions from prior attempts)\n{current_changes}"
        else:
            change_context = f"## Strategy Change Context\n{json.dumps({k: decision.get(k) for k in ('decision', 'reasoning', 'specific_changes')}, indent=2, default=str)}"

        review_prompt = f"""Review these backtest results and decide whether to deploy the strategy to a candidate slot.

## Backtest Results
{summary}

{change_context}

## Code Diff
```diff
{diff if diff else "(no textual diff)"}
```

## Previous Attempts
{history_text}"""

        response = await self._ai.ask_opus(
            review_prompt,
            system=BACKTEST_REVIEW_SYSTEM,
            purpose="backtest_review",
        )

        parsed = self._extract_json(response)
        if parsed is None:
            parsed = {"deploy": False, "reasoning": "Failed to parse backtest review response", "concerns": [], "revision_instructions": ""}

        await self._store_thought("backtest_review", "opus", review_prompt, response, parsed)
        return parsed

    async def _should_reflect(self) -> bool:
        """Check if it's time for a reflection cycle."""
        # Manual trigger via /reflect_tonight command
        manual = await self._db.fetchone(
            "SELECT value FROM system_meta WHERE key = 'reflect_tonight'"
        )
        if manual and manual["value"] == "1":
            return True

        interval = self._config.orchestrator.reflection_interval_days
        row = await self._db.fetchone(
            "SELECT value FROM system_meta WHERE key = 'last_reflection_date'"
        )
        if row:
            from datetime import date
            last = date.fromisoformat(row["value"])
            today = date.today()
            return (today - last).days >= interval
        else:
            # Never reflected — check if we have enough observations to reflect on
            obs_count = await self._db.fetchone(
                "SELECT COUNT(*) as count FROM orchestrator_observations"
            )
            min_obs = max(3, interval // 2)  # Need at least half a cycle of data
            return (obs_count["count"] if obs_count else 0) >= min_obs

    async def _gather_reflection_context(self) -> dict:
        """Gather all data needed for the reflection Opus call."""
        interval = self._config.orchestrator.reflection_interval_days
        window = f"-{interval} days"

        # Layer A: Narrative — what the orchestrator was thinking
        observations = await self._db.fetchall(
            f"""SELECT date, cycle_id, market_summary, strategy_assessment, notable_findings,
                      strategy_version, doc_flag, flag_reason
               FROM orchestrator_observations
               WHERE date >= date('now', '{window}')
               ORDER BY date ASC"""
        )

        flagged = [o for o in observations if o.get("doc_flag")]

        predictions = await self._db.fetchall(
            """SELECT id, cycle_id, claim, evidence, falsification, confidence,
                      evaluation_timeframe, category, created_at
               FROM predictions
               WHERE graded_at IS NULL
               ORDER BY created_at ASC"""
        )

        # Layer B: Evidence — what actually happened
        fund_trades = await self._db.fetchall(
            f"""SELECT symbol, tag, side, qty, entry_price, exit_price, pnl, pnl_pct,
                      fees, intent, strategy_version, strategy_regime, close_reason,
                      max_adverse_excursion, opened_at, closed_at
               FROM trades
               WHERE closed_at IS NOT NULL AND closed_at >= datetime('now', '{window}')
               ORDER BY closed_at ASC"""
        )

        candidate_trades = await self._db.fetchall(
            f"""SELECT candidate_slot, symbol, tag, side, qty, entry_price, exit_price,
                      pnl, pnl_pct, fees, intent, strategy_version, close_reason,
                      max_adverse_excursion, opened_at, closed_at
               FROM candidate_trades
               WHERE closed_at IS NOT NULL AND closed_at >= datetime('now', '{window}')
               ORDER BY closed_at ASC"""
        )

        fund_positions = await self._db.fetchall(
            "SELECT * FROM positions"
        )

        candidate_positions = await self._db.fetchall(
            "SELECT * FROM candidate_positions"
        )

        daily_perf = await self._db.fetchall(
            f"""SELECT * FROM daily_performance
               WHERE date >= date('now', '{window}')
               ORDER BY date ASC"""
        )

        candidate_daily = await self._db.fetchall(
            f"""SELECT * FROM candidate_daily_performance
               WHERE date >= date('now', '{window}')
               ORDER BY candidate_slot, date ASC"""
        )

        candidate_lifecycle = await self._db.fetchall(
            f"""SELECT slot, strategy_version, description, status, created_at, resolved_at
               FROM candidates
               WHERE created_at >= datetime('now', '{window}')
                  OR resolved_at >= datetime('now', '{window}')
               ORDER BY created_at ASC"""
        )

        strategy_versions = await self._db.fetchall(
            f"""SELECT version, description, deployed_at, retired_at, backtest_result
               FROM strategy_versions
               WHERE created_at >= datetime('now', '{window}')
               ORDER BY created_at ASC"""
        )

        fund_signals = await self._db.fetchall(
            f"""SELECT symbol, action, size_pct, confidence, strategy_regime,
                      acted_on, rejected_reason, tag, created_at
               FROM signals
               WHERE created_at >= datetime('now', '{window}')
               ORDER BY created_at ASC"""
        )

        candidate_signals = await self._db.fetchall(
            f"""SELECT candidate_slot, symbol, action, size_pct, confidence,
                      strategy_regime, acted_on, rejected_reason, tag, created_at
               FROM candidate_signals
               WHERE created_at >= datetime('now', '{window}')
               ORDER BY created_at ASC"""
        )

        return {
            "observations": [dict(o) for o in observations],
            "flagged_observations": [dict(o) for o in flagged],
            "predictions": [dict(p) for p in predictions],
            "fund_trades": [dict(t) for t in fund_trades],
            "candidate_trades": [dict(t) for t in candidate_trades],
            "fund_positions": [dict(p) for p in fund_positions],
            "candidate_positions": [dict(p) for p in candidate_positions],
            "daily_performance": [dict(d) for d in daily_perf],
            "candidate_daily_performance": [dict(d) for d in candidate_daily],
            "candidate_lifecycle": [dict(c) for c in candidate_lifecycle],
            "strategy_versions": [dict(v) for v in strategy_versions],
            "fund_signals": [dict(s) for s in fund_signals],
            "candidate_signals": [dict(s) for s in candidate_signals],
        }

    async def _archive_strategy_doc(self) -> None:
        """Archive current strategy document to strategy_doc_versions before rewriting."""
        if not STRATEGY_DOC_PATH.exists():
            return
        content = STRATEGY_DOC_PATH.read_text()
        if not content.strip():
            return

        row = await self._db.fetchone(
            "SELECT COALESCE(MAX(version), 0) as max_ver FROM strategy_doc_versions"
        )
        next_version = (row["max_ver"] if row else 0) + 1

        await self._db.execute(
            """INSERT INTO strategy_doc_versions (version, content, reflection_cycle_id)
               VALUES (?, ?, ?)""",
            (next_version, content, self._cycle_id),
        )
        await self._db.commit()
        log.info("orchestrator.strategy_doc_archived", version=next_version)

    async def _reflect(self) -> None:
        """Run the bi-weekly reflection: grade predictions, rewrite strategy doc."""
        log.info("orchestrator.reflection_start", cycle_id=self._cycle_id)

        # 1. Gather all reflection data
        ctx = await self._gather_reflection_context()

        # 2. Read current strategy document
        strategy_doc = (
            STRATEGY_DOC_PATH.read_text()
            if STRATEGY_DOC_PATH.exists()
            else "No strategy document exists yet."
        )

        # 3. Format the reflection prompt
        time_context = await self._build_time_context()
        prompt = time_context + "\n\n" + REFLECTION_USER_TEMPLATE.format(
            reflection_days=self._config.orchestrator.reflection_interval_days,
            strategy_doc=strategy_doc,
            observations=json.dumps(ctx["observations"], indent=2, default=str) if ctx["observations"] else "No observations in this period.",
            flagged_observations=json.dumps(ctx["flagged_observations"], indent=2, default=str) if ctx["flagged_observations"] else "No flagged observations.",
            predictions_to_grade=json.dumps(ctx["predictions"], indent=2, default=str) if ctx["predictions"] else "No predictions to grade.",
            fund_trades=json.dumps(ctx["fund_trades"], indent=2, default=str) if ctx["fund_trades"] else "No closed fund trades.",
            candidate_trades=json.dumps(ctx["candidate_trades"], indent=2, default=str) if ctx["candidate_trades"] else "No closed candidate trades.",
            fund_positions=json.dumps(ctx["fund_positions"], indent=2, default=str) if ctx["fund_positions"] else "No open fund positions.",
            candidate_positions=json.dumps(ctx["candidate_positions"], indent=2, default=str) if ctx["candidate_positions"] else "No open candidate positions.",
            daily_performance=json.dumps(ctx["daily_performance"], indent=2, default=str) if ctx["daily_performance"] else "No daily performance data.",
            candidate_daily_performance=json.dumps(ctx["candidate_daily_performance"], indent=2, default=str) if ctx["candidate_daily_performance"] else "No candidate daily performance data.",
            candidate_lifecycle=json.dumps(ctx["candidate_lifecycle"], indent=2, default=str) if ctx["candidate_lifecycle"] else "No candidate lifecycle events.",
            strategy_versions=json.dumps(ctx["strategy_versions"], indent=2, default=str) if ctx["strategy_versions"] else "No strategy versions deployed.",
            fund_signals=json.dumps(ctx["fund_signals"], indent=2, default=str) if ctx["fund_signals"] else "No fund signals.",
            candidate_signals=json.dumps(ctx["candidate_signals"], indent=2, default=str) if ctx["candidate_signals"] else "No candidate signals.",
        )

        # 4. Opus reflection call (same identity as analysis)
        system_prompt = (
            f"{LAYER_1_IDENTITY}\n\n---\n\n{FUND_MANDATE}\n\n---\n\n{LAYER_2_SYSTEM}"
        )

        response = await self._ai.ask_opus(
            prompt, system=system_prompt, purpose="reflection"
        )

        parsed = self._extract_json(response)
        if parsed is None:
            log.warning("orchestrator.reflection_parse_failed")
            await self._store_thought("reflection", "opus", prompt, response)
            return

        await self._store_thought("reflection", "opus", prompt, response, parsed)

        # 5. Archive current strategy doc
        await self._archive_strategy_doc()

        # 6. Write new strategy document
        new_doc = parsed.get("strategy_document", "")
        if new_doc and len(new_doc.strip()) > 50:
            STRATEGY_DOC_PATH.write_text(new_doc)
            log.info("orchestrator.strategy_doc_updated",
                     length=len(new_doc))

        # 7. Grade predictions by ID
        graded = parsed.get("graded_predictions", [])
        graded_count = 0
        for gp in graded:
            if not isinstance(gp, dict):
                continue
            pred_id = gp.get("prediction_id")
            grade = gp.get("grade", "")
            if pred_id is None or not grade:
                continue
            await self._db.execute(
                """UPDATE predictions
                   SET graded_at = datetime('now', 'utc'), grade = ?,
                       grade_evidence = ?, grade_learning = ?
                   WHERE id = ?""",
                (
                    grade[:100],
                    (gp.get("grade_evidence") or "")[:2000],
                    (gp.get("grade_learning") or "")[:2000],
                    pred_id,
                ),
            )
            graded_count += 1

        # Compute grade breakdown
        correct = sum(1 for gp in graded if isinstance(gp, dict) and gp.get("grade", "").lower() == "correct")
        incorrect = sum(1 for gp in graded if isinstance(gp, dict) and gp.get("grade", "").lower() == "incorrect")
        uncertain = graded_count - correct - incorrect

        # Extract key learnings from graded predictions
        key_learnings: list[str] = []
        for gp in graded:
            if not isinstance(gp, dict):
                continue
            learning = gp.get("grade_learning", "")
            if learning and learning.strip():
                key_learnings.append(learning.strip()[:500])

        # 8. Store new predictions from reflection
        new_preds = parsed.get("predictions", [])
        new_pred_count = 0
        for pred in new_preds:
            if not isinstance(pred, dict):
                continue
            claim = pred.get("claim", "")
            falsification = pred.get("falsification", "")
            if not claim or not falsification:
                continue
            await self._db.execute(
                """INSERT INTO predictions
                   (cycle_id, claim, evidence, falsification, confidence,
                    evaluation_timeframe, category)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    self._cycle_id or "unknown",
                    claim[:2000],
                    (pred.get("evidence") or "")[:2000],
                    falsification[:2000],
                    (pred.get("confidence") or "")[:50],
                    (pred.get("evaluation_timeframe") or "")[:100],
                    "reflection",
                ),
            )
            new_pred_count += 1

        # 9. Update last_reflection_date
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        await self._db.execute(
            "INSERT OR REPLACE INTO system_meta (key, value) VALUES ('last_reflection_date', ?)",
            (today,),
        )
        await self._db.commit()

        # 10. Notify
        summary = parsed.get("reflection_summary", "Reflection complete")
        if self._notifier:
            await self._notifier.reflection_completed(
                graded_count, new_pred_count, summary,
                correct=correct, incorrect=incorrect, uncertain=uncertain,
                key_learnings=key_learnings,
            )

        log.info("orchestrator.reflection_complete",
                 graded=graded_count, new_predictions=new_pred_count)

    async def _store_observation(self, decision: dict) -> None:
        """Store daily observations in DB table (replaces strategy doc appends).

        Observations are the orchestrator's daily findings — rolling window
        (lockstep with the reflection cycle interval).
        """
        try:
            # Get current strategy version for attribution
            strategy_version = await self._get_current_strategy_version()

            # REPLACE: if cycle re-runs for same date, latest observation wins
            await self._db.execute(
                """INSERT OR REPLACE INTO orchestrator_observations
                   (date, cycle_id, market_summary, strategy_assessment, notable_findings,
                    strategy_version, doc_flag, flag_reason)
                   VALUES (date('now', 'utc'), ?, ?, ?, ?, ?, ?, ?)""",
                (
                    self._cycle_id or "unknown",
                    decision.get("market_observations", "")[:5000],
                    decision.get("reasoning", "")[:5000],
                    decision.get("cross_reference_findings", "")[:5000],
                    strategy_version,
                    1 if decision.get("doc_flag") else 0,
                    (decision.get("flag_reason") or "")[:1000] if decision.get("doc_flag") else None,
                ),
            )
            # Prune observations older than reflection interval (lockstep with reflection cycle)
            interval = self._config.orchestrator.reflection_interval_days
            await self._db.execute(
                f"DELETE FROM orchestrator_observations WHERE date < date('now', '-{interval} days')"
            )
            # Prune thoughts older than 30 days
            await self._db.execute(
                "DELETE FROM orchestrator_thoughts WHERE created_at < datetime('now', '-30 days')"
            )
            await self._db.commit()
            market_summary = decision.get("market_observations", "")
            strategy_assessment = decision.get("reasoning", "")
            log.info("orchestrator.observation_stored",
                     cycle_id=self._cycle_id,
                     market=market_summary or "",
                     assessment=strategy_assessment or "")
        except Exception as e:
            log.warning("orchestrator.observation_store_failed", error=str(e))

    async def _get_current_strategy_version(self) -> str | None:
        """Get the currently deployed strategy version."""
        row = await self._db.fetchone(
            "SELECT version FROM strategy_versions WHERE retired_at IS NULL ORDER BY deployed_at DESC LIMIT 1"
        )
        return row["version"] if row else None

    async def _store_predictions(self, decision: dict) -> None:
        """Extract and store predictions from the orchestrator's decision."""
        predictions = decision.get("predictions")
        if not predictions or not isinstance(predictions, list):
            return

        count = 0
        for pred in predictions:
            if not isinstance(pred, dict):
                continue
            claim = pred.get("claim", "")
            evidence = pred.get("evidence", "")
            falsification = pred.get("falsification", "")
            confidence = pred.get("confidence", "")
            if not claim or not falsification:
                continue  # Skip incomplete predictions

            await self._db.execute(
                """INSERT INTO predictions
                   (cycle_id, claim, evidence, falsification, confidence,
                    evaluation_timeframe, category)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    self._cycle_id or "unknown",
                    claim[:2000],
                    evidence[:2000],
                    falsification[:2000],
                    confidence[:50],
                    (pred.get("evaluation_timeframe") or "")[:100],
                    (pred.get("category") or "")[:100],
                ),
            )
            count += 1

        if count > 0:
            await self._db.commit()
            log.info("orchestrator.predictions_stored", count=count)

    async def _log_orchestration(
        self, decision: dict, deployed_version: str | None = None, outcome: str = ""
    ) -> None:
        """Record orchestration decision in database."""
        # Get current strategy version — if we just deployed, the new version has retired_at IS NULL
        # so we need the SECOND most recent, or the deployed version's parent
        if deployed_version:
            parent = await self._db.fetchone(
                "SELECT parent_version FROM strategy_versions WHERE version = ?",
                (deployed_version,),
            )
            version_from = parent["parent_version"] if parent else None
        else:
            current = await self._db.fetchone(
                "SELECT version FROM strategy_versions WHERE retired_at IS NULL ORDER BY deployed_at DESC LIMIT 1"
            )
            version_from = current["version"] if current else None

        # Token usage for this cycle
        tokens_used = self._ai._daily_tokens_used  # Total for today (includes this cycle)
        row = await self._db.fetchone(
            "SELECT COALESCE(SUM(cost_usd), 0) as total FROM token_usage WHERE created_at >= date('now')"
        )
        cost_today = row["total"] if row else 0.0

        await self._db.execute(
            """INSERT INTO orchestrator_log
               (date, cycle_id, action, analysis, changes, strategy_version_from, strategy_version_to, tokens_used, cost_usd, outcome)
               VALUES (date('now'), ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                self._cycle_id,
                decision.get("decision", "UNKNOWN"),
                json.dumps(decision, default=str),
                decision.get("specific_changes", ""),
                version_from,
                deployed_version,
                tokens_used,
                cost_today,
                outcome,
            ),
        )
        await self._db.commit()
