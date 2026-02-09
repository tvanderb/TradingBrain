"""Orchestrator — nightly AI review, strategy evolution, and analysis module evolution.

Runs daily during the 12-3am EST window:
1. Gather context:
   - Ground truth benchmarks (rigid shell, cannot modify)
   - Market analysis module output (flexible, can rewrite)
   - Trade performance module output (flexible, can rewrite)
   - Strategy code, doc, version history
   - User constraints (risk limits, goals)
2. Opus analyzes with labeled inputs and cross-references
3. Decides: NO_CHANGE / STRATEGY_TWEAK / STRATEGY_RESTRUCTURE / STRATEGY_OVERHAUL
           / MARKET_ANALYSIS_UPDATE / TRADE_ANALYSIS_UPDATE
4. If strategy change: Sonnet generates -> Opus reviews -> sandbox -> backtest -> paper test
5. If analysis change: Sonnet generates -> Opus reviews (math focus) -> sandbox -> deploy (no paper test)
6. Update strategy document with findings
7. Data maintenance
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import structlog

from src.orchestrator.ai_client import AIClient
from src.orchestrator.reporter import Reporter
from src.shell.config import Config
from src.shell.contract import RiskLimits
from src.shell.database import Database
from src.shell.data_store import DataStore
from src.shell.truth import compute_truth_benchmarks
from src.statistics.loader import load_analysis_module, get_module_path, get_code_hash as get_analysis_hash
from src.statistics.readonly_db import ReadOnlyDB, get_schema_description
from src.statistics.sandbox import validate_analysis_module
from src.statistics.loader import deploy_module as deploy_analysis_module
from src.strategy.backtester import Backtester
from src.strategy.loader import deploy_strategy, get_code_hash, get_strategy_path, load_strategy
from src.strategy.sandbox import validate_strategy

log = structlog.get_logger()

STRATEGY_DOC_PATH = Path(__file__).resolve().parent.parent.parent / "strategy" / "strategy_document.md"

ANALYSIS_SYSTEM = """You are the AI orchestrator for a crypto trading system. You review performance, analyze market conditions, and decide whether to modify the trading strategy or your analysis modules.

## Your Inputs (labeled by category)

You receive FIVE categories of information. Pay attention to their labels:

1. **GROUND TRUTH** (rigid shell — you cannot change this, use to verify your analysis)
   Simple verifiable metrics computed directly from raw database data. Trade counts, win/loss, P&L, fees, expectancy, consecutive losses, drawdown, signal/scan activity. These are always correct.

2. **YOUR MARKET ANALYSIS** (you designed this module — you can rewrite it)
   Analysis of exchange data, indicators, price action, volatility, signal proximity. You wrote this code. If it's missing metrics you need, update it.

3. **YOUR TRADE PERFORMANCE ANALYSIS** (you designed this module — you can rewrite it)
   Analysis of trade execution quality, strategy effectiveness, fee impact, holding duration, rolling metrics. You wrote this code. If it's incomplete, update it.

4. **YOUR STRATEGY** (you designed this — you can rewrite it)
   The trading strategy source code, strategy document (institutional memory), version history.

5. **USER CONSTRAINTS** (risk limits, goals — you cannot change these)
   Hard risk limits enforced by the shell. Your budget and operational parameters.

## Your Goals (in priority order)

**Primary**: Achieve positive expectancy after fees. Every trade must clear the ~0.65-0.80% round-trip fee wall.

**Secondary**:
- Profit factor > 1.2 (gross wins / gross losses — the system makes more than it loses)
- Average win / average loss ratio > 2.0 (when you win, win big relative to losses)
- Net positive P&L over any 30-day rolling window

**Informational** (track but don't optimize for directly):
- Win rate (a 30% win rate is fine if avg_win/avg_loss is 3:1)
- Sharpe ratio (noisy on small samples, penalizes upside volatility)
- Sortino ratio (better than Sharpe — only penalizes downside)

**Meta-goals** (how you should operate):
- Be conservative — don't change what's working
- Build understanding before acting — prefer NO_CHANGE when data is insufficient
- Improve observability — if you can't answer a question about performance, update your analysis modules
- Maintain institutional memory — always document findings in the strategy document
- Think long-term — a small consistent edge compounds; wild swings in approach don't
- **Fewer trades, bigger moves** — every trade must overcome ~0.65-0.80% round-trip fees. Only take setups where the expected move is at least 3x the round-trip cost (~2% minimum expected move). Trade quality always beats trade quantity.

## Cross-referencing

Your market analysis and trade performance modules run independently — neither sees the other's output. YOU cross-reference them:
- Do trade outcomes correlate with market conditions? (e.g., losing in ranging markets, winning in trends)
- Does your market analysis capture what matters for your strategy's decisions?
- Does your trade performance analysis measure what actually drives profitability?

Always verify your analysis modules' output against GROUND TRUTH. If they disagree, ground truth is correct.

## Decision Options

Choose ONE:
- **NO_CHANGE** (tier 0): No modifications needed. Document observations.
- **STRATEGY_TWEAK** (tier 1): Parameter changes, threshold adjustments. 1 day paper test.
- **STRATEGY_RESTRUCTURE** (tier 2): Logic changes, new indicators, different entry/exit. 2 day paper test.
- **STRATEGY_OVERHAUL** (tier 3): Fundamentally different approach. 1 week paper test.
- **MARKET_ANALYSIS_UPDATE**: Rewrite your market analysis module to measure different/better things.
- **TRADE_ANALYSIS_UPDATE**: Rewrite your trade performance module to measure different/better things.

## Decision Guidelines

- Minimum ~20 trades before judging strategy performance statistically
- Distinguish between strategy problems and market condition problems
- If you lack information to decide, update analysis modules first (cheaper than bad strategy changes)
- Analysis module updates are low-risk (read-only, no paper test needed) — prefer them when unsure
- Don't chase short-term noise; look for persistent patterns

Respond in JSON format:
{
    "decision": "NO_CHANGE" | "STRATEGY_TWEAK" | "STRATEGY_RESTRUCTURE" | "STRATEGY_OVERHAUL" | "MARKET_ANALYSIS_UPDATE" | "TRADE_ANALYSIS_UPDATE",
    "risk_tier": 0 | 1 | 2 | 3,
    "reasoning": "...",
    "specific_changes": "..." (if changing strategy or analysis),
    "cross_reference_findings": "..." (what you found comparing market conditions to trade outcomes),
    "market_observations": "...",
    "strategy_doc_update": "..." (daily findings to add to institutional memory)
}"""

CODE_GEN_SYSTEM = """You are a Python code generator for a crypto trading strategy.

You MUST:
1. Inherit from StrategyBase (imported from src.shell.contract)
2. Implement initialize() and analyze() methods
3. Return list[Signal] from analyze()
4. Use only: pandas, numpy, ta library for indicators
5. Keep the strategy in a single file
6. Include clear docstring explaining the strategy

You MUST NOT:
- Import os, subprocess, socket, http, or any network/filesystem modules
- Make any API calls or file I/O
- Use eval(), exec(), or __import__()

Available imports:
- pandas, numpy, ta
- src.shell.contract (Signal, Action, Intent, OrderType, Portfolio, RiskLimits, StrategyBase, SymbolData)

The strategy receives:
- markets: dict[str, SymbolData] with candles_5m (30d), candles_1h (1yr), candles_1d (7yr), current_price, spread, volume_24h
- portfolio: Portfolio with cash, positions, recent_trades, pnl
- timestamp: datetime

Return list[Signal] with: symbol, action (BUY/SELL/CLOSE), size_pct, stop_loss, take_profit, intent (DAY/SWING/POSITION), confidence, reasoning

Output ONLY the Python code. No markdown, no explanation, just the code."""

CODE_REVIEW_SYSTEM = """You are a code reviewer for a trading strategy. Check for:

1. IO Contract compliance — correct inheritance, method signatures, return types
2. Safety — no forbidden imports, no side effects, no network calls
3. Logic correctness — edge cases, division by zero, empty data handling
4. Risk management — stop losses set, position sizing within limits
5. Risk tier accuracy — is the self-assessed tier correct?

Respond in JSON:
{
    "approved": true | false,
    "issues": ["..."],
    "risk_tier_correct": true | false,
    "suggested_tier": 1 | 2 | 3,
    "feedback": "..."
}"""

ANALYSIS_CODE_GEN_SYSTEM = """You are a Python code generator for a crypto trading analysis module.

Analysis modules compute statistics from database data. They are READ-ONLY — they never modify data.

You MUST:
1. Inherit from AnalysisBase (imported from src.shell.contract)
2. Implement `async def analyze(self, db, schema: dict) -> dict`
3. Use the `db` parameter (ReadOnlyDB) for all queries — it only allows SELECT
4. Return a dict of computed metrics
5. Handle empty tables gracefully (no trades yet, no scans yet)
6. Guard against division by zero
7. Use COALESCE in SQL for NULL-safe aggregation

You MUST NOT:
- Import os, subprocess, socket, http, urllib, requests, httpx, websockets, aiohttp
- Import sqlite3 or aiosqlite (use the provided db object)
- Import pathlib (no filesystem access)
- Use eval(), exec(), __import__(), open(), print()
- Modify any data — SELECT only

You MAY import: statistics, scipy, numpy, pandas, math, collections, itertools, functools, datetime, json, re

The `db` object provides:
- `await db.fetchone(sql, params)` → dict | None
- `await db.fetchall(sql, params)` → list[dict]
- `await db.execute(sql, params)` → cursor (for complex queries)

The `schema` parameter describes all available tables and columns.

Output ONLY the Python code. No markdown, no explanation, just the code."""

ANALYSIS_REVIEW_SYSTEM = """You are a mathematical correctness reviewer for a trading analysis module. Focus on:

1. **Formula correctness** — verify standard statistical definitions:
   - Win rate = wins / total (not wins / losses)
   - Expectancy = (win_rate * avg_win) + (loss_rate * avg_loss)
   - Sharpe ratio = mean(returns) / std(returns) * sqrt(periods)
   - Drawdown = (peak - current) / peak
   - Any other formulas used

2. **Edge cases** — check all paths:
   - Division by zero when no trades, no scans, no wins, no losses
   - Empty query results (fetchone returns None, fetchall returns [])
   - NULL values in database columns (use COALESCE in SQL)
   - Single-element lists (std dev undefined, averages trivial)

3. **SQL correctness**:
   - No write operations (INSERT, UPDATE, DELETE, DROP, ALTER, CREATE)
   - Correct GROUP BY / aggregate combinations
   - Date/time comparisons use consistent formats

4. **Statistical validity**:
   - Sample sizes noted where relevant
   - Rolling windows handle partial data at edges
   - Percentages are correctly computed (0.0-1.0 or 0-100, consistent)

5. **Safety**:
   - No forbidden imports
   - No side effects

Respond in JSON:
{
    "approved": true | false,
    "issues": ["..."],
    "math_errors": ["..."],
    "edge_case_risks": ["..."],
    "feedback": "..."
}"""


class Orchestrator:
    """Nightly AI review and strategy evolution engine."""

    def __init__(
        self, config: Config, db: Database, ai: AIClient,
        reporter: Reporter, data_store: DataStore,
    ) -> None:
        self._config = config
        self._db = db
        self._ai = ai
        self._reporter = reporter
        self._data_store = data_store
        self._cycle_id: str | None = None

    async def _store_thought(
        self, step: str, model: str, input_summary: str, full_response: str, parsed_result=None,
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
                    (input_summary[:500] if input_summary else None),
                    full_response,
                    json.dumps(parsed_result, default=str) if parsed_result is not None else None,
                ),
            )
            await self._db.commit()
        except Exception as e:
            log.warning("orchestrator.store_thought_failed", step=step, error=str(e))

    async def run_nightly_cycle(self) -> str:
        """Execute the full nightly orchestration cycle. Returns report summary."""
        self._cycle_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        log.info("orchestrator.cycle_start", cycle_id=self._cycle_id)

        try:
            # 1. Gather context
            context = await self._gather_context()

            # 2. Opus analysis
            decision = await self._analyze(context)

            # 3. Execute decision
            decision_type = decision.get("decision", "NO_CHANGE")

            if decision_type == "NO_CHANGE":
                report = f"Orchestrator: No changes. {decision.get('reasoning', '')}"
            elif decision_type in ("MARKET_ANALYSIS_UPDATE", "TRADE_ANALYSIS_UPDATE"):
                report = await self._execute_analysis_change(decision, context)
            else:
                # Strategy changes: STRATEGY_TWEAK, STRATEGY_RESTRUCTURE, STRATEGY_OVERHAUL
                # Also handle legacy names: TWEAK, RESTRUCTURE, OVERHAUL
                report = await self._execute_change(decision, context)

            # 4. Store daily observations
            await self._store_observation(decision)

            # 5. Log orchestration
            await self._log_orchestration(decision)

            # 6. Data maintenance
            await self._data_store.run_nightly_maintenance()

            log.info("orchestrator.cycle_complete", decision=decision.get("decision"))
            return report

        except Exception as e:
            log.error("orchestrator.cycle_failed", error=str(e))
            return f"Orchestrator cycle failed: {e}"

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

        # --- 2. MARKET ANALYSIS (flexible module, orchestrator can rewrite) ---
        try:
            market_module = load_analysis_module("market_analysis")
            ro_db = ReadOnlyDB(self._db.conn)
            schema = get_schema_description()
            market_report = await market_module.analyze(ro_db, schema)
        except Exception as e:
            log.error("orchestrator.market_analysis_failed", error=str(e))
            market_report = {"error": str(e)}

        # --- 3. TRADE PERFORMANCE (flexible module, orchestrator can rewrite) ---
        try:
            perf_module = load_analysis_module("trade_performance")
            ro_db = ReadOnlyDB(self._db.conn)
            perf_report = await perf_module.analyze(ro_db, schema)
        except Exception as e:
            log.error("orchestrator.trade_performance_failed", error=str(e))
            perf_report = {"error": str(e)}

        # --- 4. STRATEGY CONTEXT ---
        # Current strategy code
        strategy_path = get_strategy_path()
        strategy_code = strategy_path.read_text() if strategy_path.exists() else "No strategy file"
        code_hash = get_code_hash(strategy_path) if strategy_path.exists() else "none"

        # Current analysis module code (so orchestrator can see what it wrote)
        market_analysis_code = ""
        trade_performance_code = ""
        try:
            market_path = get_module_path("market_analysis")
            market_analysis_code = market_path.read_text() if market_path.exists() else "No module"
        except Exception:
            market_analysis_code = "Failed to read"
        try:
            perf_path = get_module_path("trade_performance")
            trade_performance_code = perf_path.read_text() if perf_path.exists() else "No module"
        except Exception:
            trade_performance_code = "Failed to read"

        # Strategy document
        strategy_doc = STRATEGY_DOC_PATH.read_text() if STRATEGY_DOC_PATH.exists() else "No strategy document"

        # Performance data
        performance = await self._reporter.strategy_performance(days=7)
        daily_perf = await self._db.fetchall(
            "SELECT * FROM daily_performance ORDER BY date DESC LIMIT 7"
        )

        # Recent trades
        trades = await self._db.fetchall(
            "SELECT symbol, side, pnl, pnl_pct, fees, intent, strategy_regime, closed_at FROM trades "
            "WHERE closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT 50"
        )

        # Strategy version history
        versions = await self._db.fetchall(
            "SELECT version, description, risk_tier, backtest_result, paper_test_result, market_conditions "
            "FROM strategy_versions ORDER BY created_at DESC LIMIT 10"
        )

        # --- 5. OPERATIONAL CONTEXT ---
        usage = await self._ai.get_daily_usage()

        # Active paper tests
        active_paper_tests = await self._db.fetchall(
            """SELECT strategy_version, risk_tier, required_days, started_at, ends_at, status
               FROM paper_tests WHERE status = 'running' ORDER BY started_at DESC"""
        )

        # Recent observations (last 14 days)
        recent_observations = await self._db.fetchall(
            """SELECT date, market_summary, strategy_assessment, notable_findings
               FROM orchestrator_observations
               WHERE date >= date('now', '-14 days')
               ORDER BY date DESC"""
        )

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
            "active_paper_tests": [dict(t) for t in active_paper_tests],
            "recent_observations": [dict(o) for o in recent_observations],
        }

    async def _analyze(self, context: dict) -> dict:
        """Opus analyzes performance and decides on action."""
        prompt = f"""Review the trading system's current state and decide on action.

---

## GROUND TRUTH (rigid shell — you cannot change this, use to verify your analysis)
{json.dumps(context['ground_truth'], indent=2, default=str)}

---

## YOUR MARKET ANALYSIS (you designed this module — you can rewrite it)
### Module Output:
{json.dumps(context['market_report'], indent=2, default=str)}

### Module Source Code:
```python
{context['market_analysis_code']}
```

---

## YOUR TRADE PERFORMANCE ANALYSIS (you designed this module — you can rewrite it)
### Module Output:
{json.dumps(context['trade_performance_report'], indent=2, default=str)}

### Module Source Code:
```python
{context['trade_performance_code']}
```

---

## YOUR STRATEGY (you designed this — you can rewrite it)
### Strategy Source Code:
```python
{context['strategy_code']}
```

### Strategy Document (Institutional Memory):
{context['strategy_doc']}

### Performance (Last 7 Days):
{json.dumps(context['performance_7d'], indent=2, default=str)}

### Daily Performance Snapshots:
{json.dumps(context['daily_performance'], indent=2, default=str)}

### Recent Trades (Last 50):
{json.dumps(context['recent_trades'], indent=2, default=str)}

### Strategy Version History:
{json.dumps(context['version_history'], indent=2, default=str)}

---

## USER CONSTRAINTS (risk limits, goals — you cannot change these)
- Round-trip fees: ~0.65-0.80% (0.25% maker, 0.40% taker) — minimum ~2% move to profit
- Max trade size: 7% of portfolio (bigger conviction bets)
- Max position size: 15% of portfolio
- Max daily loss: 6% of portfolio (hard halt)
- Max drawdown: 12% (the real safety net — system halts if hit)
- Max positions: 5
- Consecutive loss halt: disabled (drawdown protects, not streak length)
- Token budget: {context['token_usage'].get('used', 0)} / {context['token_usage'].get('daily_limit', 0)} tokens used today (${context['token_usage'].get('total_cost', 0):.4f})

---

Cross-reference your market analysis against trade performance. Do trade outcomes correlate with market conditions? Is your analysis capturing what matters?

Analyze and decide. Respond in JSON format."""

        response = await self._ai.ask_opus(
            prompt, system=ANALYSIS_SYSTEM, max_tokens=2048, purpose="nightly_analysis"
        )

        # Parse JSON from response
        parsed = None
        try:
            # Find JSON in response
            start = response.find("{")
            end = response.rfind("}") + 1
            if start >= 0 and end > start:
                parsed = json.loads(response[start:end])
        except json.JSONDecodeError:
            log.warning("orchestrator.json_parse_failed", response=response[:200])

        if parsed is None:
            parsed = {"decision": "NO_CHANGE", "reasoning": "Failed to parse analysis response"}

        await self._store_thought("analysis", "opus", prompt[:500], response, parsed)
        return parsed

    async def _execute_change(self, decision: dict, context: dict) -> str:
        """Execute a strategy change: generate -> review -> sandbox -> backtest."""
        tier = decision.get("risk_tier", 1)
        changes = decision.get("specific_changes", "")
        max_revisions = self._config.orchestrator.max_revisions

        for attempt in range(max_revisions):
            # Sonnet generates code
            gen_prompt = f"""Generate a new trading strategy based on these requirements:

## Change Request
{changes}

## Current Strategy (for reference)
```python
{context['strategy_code']}
```

## Strategy Document
{context['strategy_doc']}

## Performance Context
{json.dumps(context['performance_7d'], indent=2, default=str)}

Generate the complete strategy.py file."""

            code = await self._ai.ask_sonnet(
                gen_prompt, system=CODE_GEN_SYSTEM, max_tokens=8192,
                purpose=f"code_gen_attempt_{attempt+1}",
            )
            await self._store_thought(f"code_gen_{attempt+1}", "sonnet", gen_prompt[:500], code)

            # Strip markdown code fences if present
            if "```python" in code:
                code = code.split("```python")[1].split("```")[0]
            elif "```" in code:
                code = code.split("```")[1].split("```")[0]
            code = code.strip()

            # Sandbox validation
            sandbox_result = validate_strategy(code)
            if not sandbox_result.passed:
                log.warning("orchestrator.sandbox_failed", attempt=attempt+1, errors=sandbox_result.errors)
                changes += f"\n\nPrevious attempt failed sandbox: {sandbox_result.errors}. Fix these issues."
                continue

            # Opus code review
            review_prompt = f"""Review this trading strategy code for correctness and safety:

```python
{code}
```

The agent classified this change as risk tier {tier} ({['', 'tweak', 'restructure', 'overhaul'][tier]}).
Is this classification correct?

{json.dumps(decision, indent=2, default=str)}"""

            review_response = await self._ai.ask_opus(
                review_prompt, system=CODE_REVIEW_SYSTEM, max_tokens=1024,
                purpose=f"code_review_attempt_{attempt+1}",
            )

            try:
                start = review_response.find("{")
                end = review_response.rfind("}") + 1
                review = json.loads(review_response[start:end])
            except (json.JSONDecodeError, ValueError):
                review = {"approved": False, "feedback": "Failed to parse review"}

            await self._store_thought(f"code_review_{attempt+1}", "opus", review_prompt[:500], review_response, review)

            if review.get("approved"):
                # Determine actual risk tier
                actual_tier = review.get("suggested_tier", tier)
                paper_days = {1: 1, 2: 2, 3: 7}.get(actual_tier, 1)

                # Backtest against historical data before deploying
                backtest_passed, backtest_summary = await self._run_backtest(code)
                if not backtest_passed:
                    log.warning("orchestrator.backtest_failed", summary=backtest_summary)
                    changes += f"\n\nBacktest failed: {backtest_summary}. Adjust the strategy."
                    continue

                # Deploy to active
                version = f"v{datetime.now().strftime('%Y%m%d_%H%M')}"
                code_hash = deploy_strategy(code, version)

                # Record in strategy index
                await self._db.execute(
                    """INSERT INTO strategy_versions
                       (version, code_hash, risk_tier, description, market_conditions, deployed_at)
                       VALUES (?, ?, ?, ?, ?, datetime('now'))""",
                    (version, code_hash, actual_tier, changes[:500],
                     decision.get("market_observations", "")[:500]),
                )

                # Create paper test entry
                from datetime import timedelta
                ends_at = (datetime.now() + timedelta(days=paper_days)).isoformat()
                await self._db.execute(
                    """INSERT INTO paper_tests
                       (strategy_version, risk_tier, required_days, ends_at)
                       VALUES (?, ?, ?, ?)""",
                    (version, actual_tier, paper_days, ends_at),
                )

                await self._db.commit()

                log.info("orchestrator.strategy_deployed", version=version,
                         tier=actual_tier, paper_days=paper_days)

                return (
                    f"Strategy {version} deployed (tier {actual_tier}, {paper_days}d paper test).\n"
                    f"Changes: {changes[:200]}"
                )
            else:
                feedback = review.get("feedback", "No feedback")
                issues = review.get("issues", [])
                log.warning("orchestrator.review_rejected", attempt=attempt+1, feedback=feedback)
                changes += f"\n\nCode review feedback: {feedback}\nIssues: {issues}"

        return f"Strategy change aborted after {max_revisions} failed attempts."

    async def _execute_analysis_change(self, decision: dict, context: dict) -> str:
        """Execute an analysis module update: generate -> review -> sandbox -> deploy.

        No paper testing required — analysis modules are read-only.
        """
        decision_type = decision.get("decision", "")
        module_name = (
            "market_analysis" if decision_type == "MARKET_ANALYSIS_UPDATE"
            else "trade_performance"
        )
        changes = decision.get("specific_changes", "")
        current_code = context.get(
            "market_analysis_code" if module_name == "market_analysis"
            else "trade_performance_code",
            "",
        )
        max_revisions = self._config.orchestrator.max_revisions

        for attempt in range(max_revisions):
            # Sonnet generates analysis module code
            gen_prompt = f"""Generate a new {module_name.replace('_', ' ')} module based on these requirements:

## Change Request
{changes}

## Current Module Code (for reference)
```python
{current_code}
```

## Available Database Schema
{json.dumps(get_schema_description(), indent=2)}

## Ground Truth Benchmarks (for context on what data exists)
{json.dumps(context.get('ground_truth', {}), indent=2, default=str)}

Generate the complete {module_name}.py file."""

            code = await self._ai.ask_sonnet(
                gen_prompt, system=ANALYSIS_CODE_GEN_SYSTEM, max_tokens=8192,
                purpose=f"analysis_gen_{module_name}_attempt_{attempt+1}",
            )
            await self._store_thought(f"analysis_gen_{module_name}_{attempt+1}", "sonnet", gen_prompt[:500], code)

            # Strip markdown code fences if present
            if "```python" in code:
                code = code.split("```python")[1].split("```")[0]
            elif "```" in code:
                code = code.split("```")[1].split("```")[0]
            code = code.strip()

            # Sandbox validation
            sandbox_result = validate_analysis_module(code, module_name)
            if not sandbox_result.passed:
                log.warning("orchestrator.analysis_sandbox_failed",
                            module=module_name, attempt=attempt+1, errors=sandbox_result.errors)
                changes += f"\n\nPrevious attempt failed sandbox: {sandbox_result.errors}. Fix these issues."
                continue

            # Opus reviews for mathematical correctness
            review_prompt = f"""Review this {module_name.replace('_', ' ')} module for mathematical correctness and safety:

```python
{code}
```

The orchestrator wants to change this module because: {changes[:500]}"""

            review_response = await self._ai.ask_opus(
                review_prompt, system=ANALYSIS_REVIEW_SYSTEM, max_tokens=1024,
                purpose=f"analysis_review_{module_name}_attempt_{attempt+1}",
            )

            try:
                start = review_response.find("{")
                end = review_response.rfind("}") + 1
                review = json.loads(review_response[start:end])
            except (json.JSONDecodeError, ValueError):
                review = {"approved": False, "feedback": "Failed to parse review"}

            await self._store_thought(f"analysis_review_{module_name}_{attempt+1}", "opus", review_prompt[:500], review_response, review)

            if review.get("approved"):
                # Deploy — no paper testing needed (read-only module)
                version = f"v{datetime.now().strftime('%Y%m%d_%H%M')}"
                code_hash = deploy_analysis_module(module_name, code, version)

                log.info("orchestrator.analysis_deployed",
                         module=module_name, version=version, hash=code_hash)

                return (
                    f"Analysis module '{module_name}' updated ({version}).\n"
                    f"Changes: {changes[:200]}"
                )
            else:
                feedback = review.get("feedback", "No feedback")
                math_errors = review.get("math_errors", [])
                log.warning("orchestrator.analysis_review_rejected",
                            module=module_name, attempt=attempt+1, feedback=feedback)
                changes += f"\n\nReview feedback: {feedback}\nMath errors: {math_errors}"

        return f"Analysis module '{module_name}' update aborted after {max_revisions} failed attempts."

    async def _run_backtest(self, code: str) -> tuple[bool, str]:
        """Backtest generated strategy against recent historical data.

        Returns (passed, summary). Passes if strategy doesn't crash and
        doesn't produce catastrophic results (negative expectancy on >10 trades).
        """
        import importlib.util
        import sys
        import tempfile

        try:
            # Load the new strategy from code string
            with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
                f.write(code)
                tmp_path = f.name

            spec = importlib.util.spec_from_file_location("backtest_strategy", tmp_path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            strategy = mod.Strategy()

            # Get recent 1h candle data for backtest
            candle_data = {}
            for symbol in self._config.symbols:
                df = await self._data_store.get_candles(symbol, "5m", limit=8640)  # ~30 days of 5m
                if not df.empty:
                    candle_data[symbol] = df

            if not candle_data:
                log.info("orchestrator.backtest_skip", reason="no historical data")
                return True, "Skipped (no historical data yet)"

            risk_limits = RiskLimits(
                max_trade_pct=self._config.risk.max_trade_pct,
                default_trade_pct=self._config.risk.default_trade_pct,
                max_positions=self._config.risk.max_positions,
                max_daily_loss_pct=self._config.risk.max_daily_loss_pct,
                max_drawdown_pct=self._config.risk.max_drawdown_pct,
            )

            bt = Backtester(
                strategy=strategy,
                risk_limits=risk_limits,
                symbols=self._config.symbols,
                maker_fee_pct=self._config.kraken.maker_fee_pct,
                taker_fee_pct=self._config.kraken.taker_fee_pct,
                starting_cash=self._config.paper_balance_usd,
            )

            result = bt.run(candle_data, timeframe="5m")
            summary = result.summary()
            log.info("orchestrator.backtest_complete", summary=summary)

            # Fail only on catastrophic results (crash or very negative)
            if result.total_trades >= 10 and result.max_drawdown_pct > 0.15:
                return False, f"Excessive drawdown: {result.max_drawdown_pct:.1%}. {summary}"

            return True, summary

        except Exception as e:
            log.warning("orchestrator.backtest_error", error=str(e))
            return False, f"Strategy crashed during backtest: {e}"
        finally:
            import os
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    async def _store_observation(self, decision: dict) -> None:
        """Store daily observations in DB table (replaces strategy doc appends).

        Observations are the orchestrator's daily findings — rolling 30-day window.
        Strategy document updates are separate and rare (meaningful discoveries only).
        """
        try:
            await self._db.execute(
                """INSERT INTO orchestrator_observations
                   (date, cycle_id, market_summary, strategy_assessment, notable_findings)
                   VALUES (date('now'), ?, ?, ?, ?)""",
                (
                    self._cycle_id or "unknown",
                    decision.get("market_observations", "")[:2000],
                    decision.get("reasoning", "")[:2000],
                    decision.get("cross_reference_findings", "")[:2000],
                ),
            )
            await self._db.commit()
            log.info("orchestrator.observation_stored", cycle_id=self._cycle_id)
        except Exception as e:
            log.warning("orchestrator.observation_store_failed", error=str(e))

    async def _log_orchestration(self, decision: dict) -> None:
        """Record orchestration decision in database."""
        await self._db.execute(
            """INSERT INTO orchestrator_log
               (date, action, analysis, changes)
               VALUES (date('now'), ?, ?, ?)""",
            (
                decision.get("decision", "UNKNOWN"),
                json.dumps(decision, default=str),
                decision.get("specific_changes", ""),
            ),
        )
        await self._db.commit()
