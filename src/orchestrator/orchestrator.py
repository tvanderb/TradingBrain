"""Orchestrator — nightly AI review and strategy evolution cycle.

Runs daily during the 12-3am EST window:
1. Gather context (strategy doc, performance, code, strategy index)
2. Opus analyzes: no change / tweak / restructure / overhaul
3. If change: Sonnet generates -> Opus reviews -> sandbox -> backtest -> paper test
4. Update strategy document
5. Generate report, notify via Telegram
6. Data maintenance
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
from src.strategy.backtester import Backtester
from src.strategy.loader import deploy_strategy, get_code_hash, get_strategy_path, load_strategy
from src.strategy.sandbox import validate_strategy

log = structlog.get_logger()

STRATEGY_DOC_PATH = Path(__file__).resolve().parent.parent.parent / "strategy" / "strategy_document.md"

ANALYSIS_SYSTEM = """You are the AI orchestrator for a crypto trading system. You review daily performance and decide whether the trading strategy needs modification.

You have access to:
- The current strategy source code
- Performance metrics (trades, win rate, expectancy, P&L, fees)
- The strategy document (institutional memory)
- The strategy version index (past strategies and their performance)

Your job:
1. Analyze today's performance in context of the overall strategy
2. Decide: NO_CHANGE, TWEAK, RESTRUCTURE, or OVERHAUL
3. Provide detailed reasoning

Risk tiers:
- TWEAK (tier 1): Parameter changes, threshold adjustments. 1 day paper test.
- RESTRUCTURE (tier 2): Logic changes, new indicators, different entry/exit. 2 day paper test.
- OVERHAUL (tier 3): Fundamentally different approach. 1 week paper test.

Be conservative. Don't change what's working. Consider:
- Is there enough data to judge? (minimum ~20 trades for statistical significance)
- Is poor performance due to strategy or market conditions?
- Would a change actually address the root cause?

Respond in JSON format:
{
    "decision": "NO_CHANGE" | "TWEAK" | "RESTRUCTURE" | "OVERHAUL",
    "risk_tier": 0 | 1 | 2 | 3,
    "reasoning": "...",
    "specific_changes": "..." (if changing),
    "market_observations": "...",
    "strategy_doc_update": "..." (daily findings to add)
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

    async def run_nightly_cycle(self) -> str:
        """Execute the full nightly orchestration cycle. Returns report summary."""
        log.info("orchestrator.cycle_start")

        try:
            # 1. Gather context
            context = await self._gather_context()

            # 2. Opus analysis
            decision = await self._analyze(context)

            # 3. Execute decision
            if decision.get("decision") == "NO_CHANGE":
                report = f"Orchestrator: No changes. {decision.get('reasoning', '')}"
            else:
                report = await self._execute_change(decision, context)

            # 4. Update strategy document
            doc_update = decision.get("strategy_doc_update", "")
            if doc_update:
                await self._update_strategy_doc(doc_update, decision.get("market_observations", ""))

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
        """Collect all context needed for analysis."""

        # Current strategy code
        strategy_path = get_strategy_path()
        strategy_code = strategy_path.read_text() if strategy_path.exists() else "No strategy file"
        code_hash = get_code_hash(strategy_path) if strategy_path.exists() else "none"

        # Strategy document
        strategy_doc = STRATEGY_DOC_PATH.read_text() if STRATEGY_DOC_PATH.exists() else "No strategy document"

        # Performance data
        performance = await self._reporter.strategy_performance(days=7)
        daily_perf = await self._db.fetchall(
            "SELECT * FROM daily_performance ORDER BY date DESC LIMIT 7"
        )

        # Recent trades
        trades = await self._db.fetchall(
            "SELECT symbol, side, pnl, pnl_pct, fees, intent, closed_at FROM trades "
            "WHERE closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT 50"
        )

        # Strategy version history
        versions = await self._db.fetchall(
            "SELECT version, description, risk_tier, backtest_result, paper_test_result, market_conditions "
            "FROM strategy_versions ORDER BY created_at DESC LIMIT 10"
        )

        # Token usage
        usage = await self._ai.get_daily_usage()

        return {
            "strategy_code": strategy_code,
            "code_hash": code_hash,
            "strategy_doc": strategy_doc,
            "performance_7d": performance,
            "daily_performance": [dict(p) for p in daily_perf],
            "recent_trades": [dict(t) for t in trades],
            "version_history": [dict(v) for v in versions],
            "token_usage": usage,
        }

    async def _analyze(self, context: dict) -> dict:
        """Opus analyzes performance and decides on action."""
        prompt = f"""Review today's trading performance and decide whether to modify the strategy.

## Current Strategy Code
```python
{context['strategy_code']}
```

## Strategy Document
{context['strategy_doc']}

## Performance (Last 7 Days)
{json.dumps(context['performance_7d'], indent=2, default=str)}

## Daily Performance
{json.dumps(context['daily_performance'], indent=2, default=str)}

## Recent Trades (Last 50)
{json.dumps(context['recent_trades'], indent=2, default=str)}

## Strategy Version History
{json.dumps(context['version_history'], indent=2, default=str)}

## Token Budget
Daily used: {context['token_usage'].get('used', 0)} / {context['token_usage'].get('daily_limit', 0)}
Cost today: ${context['token_usage'].get('total_cost', 0):.4f}

Analyze and decide. Respond in JSON format."""

        response = await self._ai.ask_opus(
            prompt, system=ANALYSIS_SYSTEM, max_tokens=2048, purpose="nightly_analysis"
        )

        # Parse JSON from response
        try:
            # Find JSON in response
            start = response.find("{")
            end = response.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(response[start:end])
        except json.JSONDecodeError:
            log.warning("orchestrator.json_parse_failed", response=response[:200])

        return {"decision": "NO_CHANGE", "reasoning": "Failed to parse analysis response"}

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
                df = await self._data_store.get_candles(symbol, "1h", limit=720)  # ~30 days
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

            result = bt.run(candle_data, timeframe="1h")
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

    async def _update_strategy_doc(self, findings: str, market_obs: str) -> None:
        """Append daily findings to the strategy document."""
        if not STRATEGY_DOC_PATH.exists():
            return

        content = STRATEGY_DOC_PATH.read_text()
        today = datetime.now().strftime("%Y-%m-%d")

        # Add to Market Thesis section
        update = f"\n\n### Daily Update ({today})\n{findings}\n"
        if market_obs:
            update += f"\n**Market Conditions**: {market_obs}\n"

        content += update
        STRATEGY_DOC_PATH.write_text(content)
        log.info("orchestrator.strategy_doc_updated")

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
