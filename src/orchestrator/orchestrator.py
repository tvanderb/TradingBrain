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

import asyncio
import difflib
import json
import re
from datetime import datetime, timezone
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
from src.strategy.backtester import Backtester
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

# --- Orchestrator Prompt: Three-Layer Framework ---
# Layer 1 (Identity) + Fund Mandate + Layer 2 (System Understanding)
# Concatenated at runtime in _analyze(). See discussions.md Sessions 7-8.

LAYER_1_IDENTITY = """You are the fund manager for a crypto trading fund. You operate nightly — reviewing performance, analyzing markets, and deciding whether to modify the trading strategy or your analysis tools.

## Your Character

**Radical Honesty**
You do not rationalize your decisions. When a change didn't help, you acknowledge it. When a thesis isn't supported by data, you abandon it. You do not cherry-pick results, find patterns that aren't there, or ignore inconvenient findings. You acknowledge sample size limitations rather than drawing conclusions from insufficient data. A loss is a loss.

**Professional Judgment**
You are a thoughtful fund manager who has internalized the realities of markets. You bring judgment, not just computation. You are neither a day-trader chasing signals nor a rigid algorithm following rules.

**Comfort with Uncertainty**
You are comfortable saying "I don't have enough information yet." You do not force conclusions from thin data. But you do not use uncertainty as an excuse to avoid decisions — you know the difference between needing more data and avoiding responsibility.

**Probabilistic Thinking**
You think in distributions, not individual outcomes. A losing trade does not mean the strategy is wrong. A winning trade does not mean it is right. What matters is whether the system has an edge over many trades. You understand that statistical conclusions from small samples are unreliable.

**Relationship to Change**
Every modification resets the evaluation clock — new strategy means new data is needed to evaluate it. Persisting with something broken also has a cost. Change is a tool with a price. You understand that stability compounds and unnecessary changes introduce risk.

**Long-Term Orientation**
You think in terms of compounding — both returns and knowledge. Individual cycles are data points, not verdicts. The fund's trajectory over months matters more than any single decision."""

FUND_MANDATE = """## Fund Mandate

Portfolio growth with capital preservation. Avoid major drawdowns. This is a long-term fund."""

LAYER_2_SYSTEM = """## System

### Architecture
You operate within a rigid shell (Kraken exchange client, risk manager, portfolio tracker, database, Telegram). You control the flexible components: one trading strategy module and two analysis modules (market analysis and trade performance).

### Your Decisions and Their Consequences

**Strategy changes** trigger a pipeline: Sonnet generates code → Opus reviews → sandbox validates → backtest → paper test → deploy.
- **NO_CHANGE** (tier 0): Data keeps accumulating. Active paper tests continue.
- **STRATEGY_TWEAK** (tier 1): Targeted changes, 1-day paper test. Any active paper test on the previous version terminates and its data becomes incomplete.
- **STRATEGY_RESTRUCTURE** (tier 2): Logic changes, 2-day paper test. Same consequences.
- **STRATEGY_OVERHAUL** (tier 3): Fundamental approach change, 1-week paper test. Same consequences.

**Analysis module changes** — Sonnet generates → Opus reviews (math correctness focus) → sandbox → immediate deploy. No paper test needed (read-only modules).
- **MARKET_ANALYSIS_UPDATE**: Changes what market data you see next cycle.
- **TRADE_ANALYSIS_UPDATE**: Changes what performance data you see next cycle.

Deploying a new strategy while a paper test is active terminates that test. Rapid strategy changes destroy the ability to evaluate whether previous changes helped.

### Shell-Enforced Boundaries
These hard constraints cannot be bypassed, modified, or overridden:
- **Risk manager**: Silently clamps oversized trade requests to configured maximums.
- **Daily loss halt**: Trading stops for the day when cumulative losses hit the limit.
- **Drawdown halt**: System halts entirely when portfolio drops below the threshold from peak.
- **Consecutive loss halt**: System halts when consecutive losing trades reach the configured limit. This persists across days — only a winning trade resets the counter.
- **Truth benchmarks**: Metrics computed from raw database data. You cannot modify these. They exist so you can verify your analysis modules against reality. Includes: trade counts, win rate, net P&L, fees, expectancy, consecutive losses, portfolio state, max drawdown, signal activity, scan activity, strategy versions, profit factor, close reason breakdown, avg trade duration, best/worst trade P&L %, Sharpe ratio, and Sortino ratio.
- **Long-only**: Only long positions. Short selling is unavailable — Kraken margin trading is not accessible from Canada. No leverage.
- **Code pipeline**: All generated code must pass sandbox validation, Opus code review, and backtesting before deployment.

### Position System
Positions are identified by **tags** (globally unique identifiers). Multiple positions per symbol are supported.
- **Tags**: Each position has a unique tag (e.g., `auto_BTCUSD_001`). Auto-generated when not specified.
- **MODIFY action**: Updates SL/TP/intent on an existing position without closing it. Zero fees. Requires a tag.
- **CLOSE without tag**: Closes ALL positions for that symbol. CLOSE with tag closes only that position.
- **SELL without tag**: Sells from the oldest position for that symbol (FIFO).
- **BUY with existing tag**: Averages into that position. BUY without tag creates a new position.

### Close-Reason Tracking
Every trade close is tagged with a reason: `signal` (strategy-initiated), `stop_loss` (SL triggered), `take_profit` (TP triggered), `emergency` (emergency stop), or `reconciliation` (filled while system was down). The close_reason_breakdown in ground truth shows the distribution. High emergency or reconciliation counts indicate operational instability.

### Paper vs Live Execution
- **Paper mode**: Instant simulated fills with configurable slippage (default 0.05%). SL/TP checked client-side every 30 seconds. No exchange API calls.
- **Live mode**: Orders placed on Kraken with 30-second fill timeout. Partial fills are supported. Exchange-native SL/TP orders placed on Kraken after each BUY fill (3 retry attempts each). Startup reconciliation checks for orders that filled while the system was down.
- **Paper test evaluation**: A paper test must generate at least the configured minimum number of trades to pass. Below that threshold, the result is "inconclusive" — the strategy is not deployed. This prevents deploying untested strategies.

### Backtester Capabilities and Limitations
The backtester simulates strategy execution against historical candle data. What it does:
- Simulates MARKET orders with configurable slippage and taker fees.
- Simulates LIMIT orders: BUY fills only when candle low ≤ limit_price; SELL fills only when candle high ≥ limit_price. Uses maker fees for limit orders.
- Tracks limit order fill rates (attempted vs filled).
- Calculates per-symbol spread from median intrabar range of recent candles (not a fixed value).
- Simulates daily loss halt, max drawdown halt, consecutive loss halt, max positions, max trade size, and max position size per symbol.
- Supports partial sells, multi-position averaging, and SL/TP triggers.

What it cannot do:
- Simulate order book depth, queue priority, or realistic fill latency.
- Model market impact — a large order fills at the same slippage as a small one.
- Capture overnight gaps or exchange outages.

### Strategy Regime
If your strategy outputs a `regime` classification (e.g., "trending", "ranging"), this is the **strategy's opinion**, not ground truth. It is logged for correlation analysis but should not be treated as fact.

### Sandbox Restrictions
Strategy code runs in a sandboxed environment. Blocked modules: subprocess, os, shutil, socket, http, urllib, requests, httpx, websockets, aiohttp, sqlite3, aiosqlite, pathlib, sys, builtins, ctypes, importlib, types, threading, multiprocessing, pickle, io, tempfile, gc, inspect, operator. Blocked attribute access: __builtins__, __import__, __class__, __subclasses__, __bases__, __mro__, __globals__, __code__, __getattribute__, __dict__. Name-mangled private attributes are also blocked. Available imports: pandas, numpy, ta, src.shell.contract, src.strategy.skills.*.

### Available Skills Library
Pre-built indicator functions in `src.strategy.skills.indicators` (import via `from src.strategy.skills.indicators import ...`):
- `ema(series, period) → pd.Series` — Exponential moving average
- `rsi(series, period=14) → float` — Relative Strength Index
- `bollinger_bands(series, period=20, std_dev=2.0) → tuple[float, float, float]` — Upper, middle, lower bands
- `macd(series, fast=12, slow=26, signal=9) → tuple[float, float, float]` — MACD line, signal line, histogram
- `atr(df, period=14) → float` — Average True Range (requires OHLC DataFrame)
- `volume_ratio(volume, period=20) → float` — Current volume relative to moving average
- `classify_regime(df) → str` — Simple regime classification from OHLC data

### Risk Counter Persistence
Risk counters (daily trade count, daily P&L, consecutive losses) are restored from the database on system restart. The daily reset uses the configured timezone. The consecutive loss counter persists across days — only a winning trade resets it.

### Independent Processes
Running continuously without your involvement:
- **Scan loop** (every 5 min): Collects market data from Kraken, runs the active strategy, stores scan results, acts on signals that pass risk checks.
- **Position monitor** (every 30 sec): Checks open positions against stop-loss and take-profit. Closes triggered positions by tag (client-side in paper, exchange-native in live).
- **Conditional order monitor** (every 30 sec, live only): Polls Kraken for exchange-native SL/TP fills.
- **Data maintenance** (nightly, after your cycle): Aggregates and prunes candles beyond retention windows.
- **Failure alerting**: If your nightly cycle fails, a system error alert is sent automatically via Telegram.

### Your Inputs
Five categories, labeled by trust level:
1. **GROUND TRUTH** — Rigid shell metrics. Always correct. Use to verify your analysis.
2. **YOUR MARKET ANALYSIS** — Module you designed. You can rewrite it.
3. **YOUR TRADE PERFORMANCE ANALYSIS** — Module you designed. You can rewrite it.
4. **YOUR STRATEGY** — Code you designed. Changes go through the pipeline.
5. **SYSTEM CONSTRAINTS** — Risk limits, fees, operational parameters. You cannot change these.

If your analysis module output contradicts ground truth, ground truth is correct — your analysis has a bug.

### Data Landscape
All timeframes are bootstrapped from Kraken on cold start — the strategy has real data from minute one.
- 5-minute candles: last 30 days per symbol
- 1-hour candles: last 1 year per symbol
- Daily candles: up to 7 years per symbol
- Scan results: raw indicator values stored every scan
- Trades and signals: tagged with strategy version, strategy regime, position tag, and close reason

### Response Format
Respond in JSON:
{
    "decision": "NO_CHANGE" | "STRATEGY_TWEAK" | "STRATEGY_RESTRUCTURE" | "STRATEGY_OVERHAUL" | "MARKET_ANALYSIS_UPDATE" | "TRADE_ANALYSIS_UPDATE",
    "risk_tier": 0 | 1 | 2 | 3,
    "reasoning": "Your analysis and the basis for your decision",
    "specific_changes": "What exactly to change, if applicable",
    "cross_reference_findings": "Findings from comparing market conditions to trade outcomes",
    "market_observations": "Notable market observations from this cycle"
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
- Generate SHORT signals — the system is long-only (no margin, no leverage)

Available imports:
- pandas, numpy, ta
- src.shell.contract (Signal, Action, Intent, OrderType, Portfolio, RiskLimits, StrategyBase, SymbolData)
- src.strategy.skills.indicators (ema, rsi, bollinger_bands, macd, atr, volume_ratio, classify_regime)

The strategy receives:
- markets: dict[str, SymbolData] with candles_5m (30d), candles_1h (1yr), candles_1d (7yr), current_price, spread, volume_24h, maker_fee_pct, taker_fee_pct
  - maker_fee_pct and taker_fee_pct are per-pair (may differ across symbols)
- portfolio: Portfolio with cash, total_value, positions, recent_trades, daily_pnl, total_pnl, fees_today
- timestamp: datetime

Return list[Signal] with: symbol, action (BUY/SELL/CLOSE/MODIFY), size_pct, order_type (MARKET/LIMIT), limit_price (for LIMIT orders), stop_loss, take_profit, intent (DAY/SWING/POSITION), confidence, reasoning, slippage_tolerance (optional float override), tag (optional str — position identifier)

Fee awareness:
- MARKET orders use taker fees. LIMIT orders use maker fees (lower).
- Access per-pair fees via SymbolData.maker_fee_pct / SymbolData.taker_fee_pct.

Position tags:
- Each position has a unique tag. Access via OpenPosition.tag in portfolio.positions.
- BUY without tag creates a new position. BUY with an existing tag averages in.
- SELL/CLOSE without tag targets the oldest position for that symbol.
- MODIFY requires a tag — updates SL/TP/intent without closing. Use size_pct=0.

Example MODIFY signal:
  Signal(symbol="BTC/USD", action=Action.MODIFY, size_pct=0, tag="auto_BTCUSD_001", stop_loss=95000.0)

Output ONLY the Python code. No markdown, no explanation, just the code."""

CODE_REVIEW_SYSTEM = """You are a code reviewer for a trading strategy. Check for:

1. IO Contract compliance — correct inheritance, method signatures, return types
2. Safety — no forbidden imports, no side effects, no network calls
3. Logic correctness — edge cases, division by zero, empty data handling
4. Risk management — stop losses set, position sizing within limits
5. Risk tier accuracy — is the self-assessed tier correct?
6. Long-only compliance — no SHORT signals (system has no margin access)
7. Tag hygiene — MODIFY signals must include a tag. MODIFY without tag will be rejected.

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
        self,
        config: Config,
        db: Database,
        ai: AIClient,
        reporter: Reporter,
        data_store: DataStore,
        notifier: Notifier | None = None,
    ) -> None:
        self._config = config
        self._db = db
        self._ai = ai
        self._reporter = reporter
        self._data_store = data_store
        self._notifier = notifier
        self._cycle_id: str | None = None
        self._running = False
        self._cycle_lock = asyncio.Lock()

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
                    (input_summary[:500] if input_summary else None),
                    full_response,
                    json.dumps(parsed_result, default=str)
                    if parsed_result is not None
                    else None,
                ),
            )
            await self._db.commit()
        except Exception as e:
            log.warning("orchestrator.store_thought_failed", step=step, error=str(e))

    async def run_nightly_cycle(self) -> str:
        """Execute the full nightly orchestration cycle. Returns report summary."""
        if self._cycle_lock.locked():
            log.warning("orchestrator.already_running")
            return "Orchestrator: Skipped — cycle already in progress."
        async with self._cycle_lock:
            return await self._run_nightly_cycle_locked()

    async def _run_nightly_cycle_locked(self) -> str:
        self._running = True
        self._cycle_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        log.info("orchestrator.cycle_start", cycle_id=self._cycle_id)

        if self._notifier:
            await self._notifier.orchestrator_cycle_started()

        try:
            # 0. Check token budget
            if self._ai.tokens_remaining < 200000:
                log.warning(
                    "orchestrator.insufficient_budget",
                    remaining=self._ai.tokens_remaining,
                )
                if self._notifier:
                    await self._notifier.orchestrator_cycle_completed("SKIPPED_BUDGET")
                return "Orchestrator: Skipped — insufficient token budget remaining."

            # 0b. Evaluate any paper tests that have completed
            paper_results = await self._evaluate_paper_tests()

            # 1. Gather context
            context = await self._gather_context()

            # Include completed paper test results so Opus can learn from outcomes
            context["completed_paper_tests"] = paper_results

            # 2. Opus analysis
            decision = await self._analyze(context)

            # 3. Execute decision
            decision_type = str(decision.get("decision") or "NO_CHANGE").strip().upper()
            deployed_version = None

            valid_strategy_types = {
                "STRATEGY_TWEAK", "STRATEGY_RESTRUCTURE", "STRATEGY_OVERHAUL",
                "TWEAK", "RESTRUCTURE", "OVERHAUL",
            }

            if decision_type == "NO_CHANGE":
                report = f"Orchestrator: No changes. {decision.get('reasoning', '')}"
            elif decision_type in ("MARKET_ANALYSIS_UPDATE", "TRADE_ANALYSIS_UPDATE"):
                report = await self._execute_analysis_change(decision, context)
            elif decision_type in valid_strategy_types:
                report = await self._execute_change(decision, context)
                # Extract deployed version from report if deploy succeeded
                if "deployed" in report.lower():
                    ver_row = await self._db.fetchone(
                        "SELECT version FROM strategy_versions ORDER BY deployed_at DESC LIMIT 1"
                    )
                    deployed_version = ver_row["version"] if ver_row else None
            else:
                log.warning("orchestrator.unknown_decision_type", decision_type=decision_type)
                report = f"Orchestrator: Unknown decision type '{decision_type}' — treated as NO_CHANGE."

            # 4. Store daily observations
            await self._store_observation(decision)

            # 5. Log orchestration
            await self._log_orchestration(decision, deployed_version=deployed_version)

            # 6. Data maintenance
            await self._data_store.run_nightly_maintenance()

            log.info("orchestrator.cycle_complete", decision=decision.get("decision"))
            if self._notifier:
                await self._notifier.orchestrator_cycle_completed(decision_type)
            return report

        except Exception as e:
            log.error("orchestrator.cycle_failed", error=str(e), exc_info=True)
            if self._notifier:
                await self._notifier.system_error(f"Orchestrator cycle failed: {e}")
            raise
        finally:
            self._running = False

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
                "SELECT version, description, risk_tier, backtest_result, paper_test_result, market_conditions "
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

        try:
            active_paper_tests = await self._db.fetchall(
                """SELECT strategy_version, risk_tier, required_days, started_at, ends_at, status
                   FROM paper_tests WHERE status = 'running' ORDER BY started_at DESC"""
            )
        except Exception as e:
            log.warning("orchestrator.context_error", section="paper_tests", error=str(e))
            active_paper_tests = []

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
            recent_observations = await self._db.fetchall(
                """SELECT date, market_summary, strategy_assessment, notable_findings
                   FROM orchestrator_observations
                   WHERE date >= date('now', '-14 days')
                   ORDER BY date DESC"""
            )
        except Exception as e:
            log.warning("orchestrator.context_error", section="observations", error=str(e))
            recent_observations = []

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
            "signal_drought": drought_info,
        }

    async def _analyze(self, context: dict) -> dict:
        """Opus analyzes performance and decides on action."""
        prompt = f"""Current fund state for nightly review.

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
- Min paper test trades: {self._config.orchestrator.min_paper_test_trades} (below this → inconclusive, no deploy)
- Token budget: {context["token_usage"].get("used", 0)} / {context["token_usage"].get("daily_limit", 0)} tokens used today (${context["token_usage"].get("total_cost", 0):.4f})

---

## SIGNAL & OBSERVATION STATE
### Signal Drought Detection:
{json.dumps(context["signal_drought"], indent=2, default=str)}

### Active Paper Tests:
{json.dumps(context["active_paper_tests"], indent=2, default=str) if context["active_paper_tests"] else "No active paper tests."}

### Completed Paper Tests (this cycle):
{json.dumps(context.get("completed_paper_tests", []), indent=2, default=str) if context.get("completed_paper_tests") else "No paper tests completed this cycle."}

### Recent Observations (last 14 days):
{json.dumps(context["recent_observations"], indent=2, default=str) if context["recent_observations"] else "No prior observations."}

---

Respond in JSON format."""

        # Build system prompt from three-layer framework
        system_prompt = (
            f"{LAYER_1_IDENTITY}\n\n---\n\n{FUND_MANDATE}\n\n---\n\n{LAYER_2_SYSTEM}"
        )

        response = await self._ai.ask_opus(
            prompt, system=system_prompt, max_tokens=2048, purpose="nightly_analysis"
        )

        # Parse JSON from response
        parsed = self._extract_json(response)
        if parsed is None:
            log.warning("orchestrator.json_parse_failed", response=response[:200])
            parsed = {
                "decision": "NO_CHANGE",
                "reasoning": "Failed to parse analysis response",
            }

        await self._store_thought("analysis", "opus", prompt[:500], response, parsed)
        return parsed

    async def _execute_change(self, decision: dict, context: dict) -> str:
        """Execute a strategy change: generate -> review -> sandbox -> backtest."""
        try:
            tier = max(1, min(3, int(decision.get("risk_tier", 1))))
        except (TypeError, ValueError):
            tier = 1
        changes = str(decision.get("specific_changes") or "")
        max_revisions = self._config.orchestrator.max_revisions

        # Get parent version for lineage tracking
        current_ver = await self._db.fetchone(
            "SELECT version FROM strategy_versions WHERE deployed_at IS NOT NULL ORDER BY deployed_at DESC LIMIT 1"
        )
        parent_version = current_ver["version"] if current_ver else None

        for attempt in range(max_revisions):
            # Sonnet generates code — tier 1 gets targeted edit instructions
            # System constraints shared by both tier 1 and tier 2+ prompts
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

            if tier == 1:
                gen_prompt = f"""Make targeted changes to the existing trading strategy.

## Change Request
{changes}

## IMPORTANT: This is a tier 1 tweak — make minimal, targeted changes only.
- Modify ONLY the specific parameters, thresholds, or logic described above.
- Keep everything else IDENTICAL to the current strategy.
- Do NOT restructure, reorganize, or rewrite unrelated code.

## Current Strategy (modify this)
```python
{context["strategy_code"]}
```

## Strategy Document
{context["strategy_doc"]}

## Performance Context
{json.dumps(context["performance_7d"], indent=2, default=str)}

{system_constraints}

Output the complete strategy.py file with your targeted changes applied."""
            else:
                gen_prompt = f"""Generate a new trading strategy based on these requirements:

## Change Request
{changes}

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
                max_tokens=8192,
                purpose=f"code_gen_attempt_{attempt + 1}",
            )
            await self._store_thought(
                f"code_gen_{attempt + 1}", "sonnet", gen_prompt[:500], code
            )

            # Strip markdown code fences if present
            fence_match = re.search(r'```(?:python)?\s*\n(.*?)```', code, re.DOTALL | re.IGNORECASE)
            if fence_match:
                code = fence_match.group(1)
            code = code.strip()

            # Sandbox validation
            sandbox_result = validate_strategy(code)
            if not sandbox_result.passed:
                log.warning(
                    "orchestrator.sandbox_failed",
                    attempt=attempt + 1,
                    errors=sandbox_result.errors,
                )
                changes += f"\n\nPrevious attempt failed sandbox: {sandbox_result.errors}. Fix these issues."
                continue

            # Generate diff for reviewer context
            old_lines = context["strategy_code"].splitlines(keepends=True)
            new_lines = code.splitlines(keepends=True)
            diff = "".join(
                difflib.unified_diff(
                    old_lines, new_lines, fromfile="current", tofile="proposed", n=3
                )
            )

            # Opus code review — includes diff for change context
            review_prompt = f"""Review this trading strategy code for correctness and safety.

## Changes from current strategy (diff)
```diff
{diff if diff else "(no textual diff — code may be identical)"}
```

## Full proposed code
```python
{code}
```

The agent classified this change as risk tier {tier} ({["", "tweak", "restructure", "overhaul"][tier]}).
Is this classification correct?

{json.dumps(decision, indent=2, default=str)}"""

            review_response = await self._ai.ask_opus(
                review_prompt,
                system=CODE_REVIEW_SYSTEM,
                max_tokens=1024,
                purpose=f"code_review_attempt_{attempt + 1}",
            )

            review = self._extract_json(review_response)
            if review is None:
                review = {"approved": False, "feedback": "Failed to parse review"}

            await self._store_thought(
                f"code_review_{attempt + 1}",
                "opus",
                review_prompt[:500],
                review_response,
                review,
            )

            if review.get("approved"):
                # Determine actual risk tier
                try:
                    actual_tier = max(1, min(3, int(review.get("suggested_tier", tier))))
                except (TypeError, ValueError):
                    actual_tier = tier
                paper_days = {1: 1, 2: 2, 3: 7}.get(actual_tier, 1)

                # Backtest against historical data before deploying
                backtest_passed, backtest_summary = await self._run_backtest(code)
                if not backtest_passed:
                    log.warning(
                        "orchestrator.backtest_failed", summary=backtest_summary
                    )
                    changes += (
                        f"\n\nBacktest failed: {backtest_summary}. Adjust the strategy."
                    )
                    continue

                # Terminate any running paper tests (superseded by new strategy)
                await self._terminate_running_paper_tests("superseded by new deploy")

                # Retire old version
                if parent_version:
                    await self._db.execute(
                        "UPDATE strategy_versions SET retired_at = datetime('now') WHERE version = ?",
                        (parent_version,),
                    )

                # Deploy to active
                version = f"v{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                code_hash = deploy_strategy(code, version)

                # Record in strategy index with parent version lineage
                await self._db.execute(
                    """INSERT INTO strategy_versions
                       (version, parent_version, code_hash, risk_tier, description, backtest_result, market_conditions, deployed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                    (
                        version,
                        parent_version,
                        code_hash,
                        actual_tier,
                        changes[:500],
                        backtest_summary[:500],
                        decision.get("market_observations", "")[:500],
                    ),
                )

                # Create paper test entry
                from datetime import timedelta

                ends_at = (datetime.now(timezone.utc) + timedelta(days=paper_days)).strftime("%Y-%m-%d %H:%M:%S")
                await self._db.execute(
                    """INSERT INTO paper_tests
                       (strategy_version, risk_tier, required_days, ends_at)
                       VALUES (?, ?, ?, ?)""",
                    (version, actual_tier, paper_days, ends_at),
                )

                await self._db.commit()

                log.info(
                    "orchestrator.strategy_deployed",
                    version=version,
                    tier=actual_tier,
                    paper_days=paper_days,
                    parent=parent_version,
                )

                if self._notifier:
                    await self._notifier.strategy_deployed(version, actual_tier, changes)
                    await self._notifier.paper_test_started(version, paper_days)

                return (
                    f"Strategy {version} deployed (tier {actual_tier}, {paper_days}d paper test).\n"
                    f"Changes: {changes[:200]}"
                )
            else:
                feedback = review.get("feedback", "No feedback")
                issues = review.get("issues", [])
                log.warning(
                    "orchestrator.review_rejected",
                    attempt=attempt + 1,
                    feedback=feedback,
                )
                changes += f"\n\nCode review feedback: {feedback}\nIssues: {issues}"

        return f"Strategy change aborted after {max_revisions} failed attempts."

    async def _terminate_running_paper_tests(self, reason: str = "superseded") -> int:
        """Terminate all running paper tests. Called before deploying a new strategy."""
        result = await self._db.execute(
            "UPDATE paper_tests SET status = 'terminated' WHERE status = 'running'"
        )
        await self._db.commit()
        count = result.rowcount if hasattr(result, 'rowcount') else 0
        if count:
            log.info("orchestrator.paper_tests_terminated", count=count, reason=reason)
        return count

    async def _evaluate_paper_tests(self) -> list[dict]:
        """Evaluate paper tests that have reached their end date.
        Returns list of evaluation results for context."""
        completed = await self._db.fetchall(
            """SELECT id, strategy_version, risk_tier, started_at, ends_at
               FROM paper_tests
               WHERE status = 'running' AND ends_at <= datetime('now', 'utc')"""
        )
        results = []
        for test in completed:
            version = test["strategy_version"]
            # Get trades made during the paper test period (filter by time window)
            trades = await self._db.fetchall(
                "SELECT pnl FROM trades WHERE strategy_version = ? AND pnl IS NOT NULL AND datetime(opened_at) >= datetime(?) AND closed_at IS NOT NULL AND datetime(closed_at) <= datetime(?)",
                (version, test["started_at"], test["ends_at"]),
            )
            total_pnl = sum(t["pnl"] for t in trades) if trades else 0.0
            trade_count = len(trades)
            wins = sum(1 for t in trades if t["pnl"] > 0)

            # Pass/fail: must have enough trades and not lose money
            min_trades = self._config.orchestrator.min_paper_test_trades
            if trade_count < min_trades:
                passed = False
                status = "inconclusive"  # Not enough trades — don't deploy untested strategy
            elif total_pnl >= 0:
                passed = True
                status = "passed"
            else:
                passed = False
                status = "failed"

            result_data = {"trades": trade_count, "pnl": round(total_pnl, 4), "wins": wins, "min_required": min_trades}
            await self._db.execute(
                "UPDATE paper_tests SET status = ?, result = ?, completed_at = datetime('now') WHERE id = ?",
                (status, json.dumps(result_data), test["id"]),
            )

            results.append({
                "version": version,
                "status": status,
                "trades": trade_count,
                "pnl": total_pnl,
                "min_required": min_trades,
            })

            if self._notifier:
                await self._notifier.paper_test_completed(
                    version, passed,
                    {"trades": trade_count, "pnl": round(total_pnl, 4), "wins": wins},
                )

            log.info(
                "orchestrator.paper_test_evaluated",
                version=version, status=status,
                trades=trade_count, pnl=round(total_pnl, 4),
            )

        if results:
            await self._db.commit()
        return results

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
                max_tokens=8192,
                purpose=f"analysis_gen_{module_name}_attempt_{attempt + 1}",
            )
            await self._store_thought(
                f"analysis_gen_{module_name}_{attempt + 1}",
                "sonnet",
                gen_prompt[:500],
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

The orchestrator wants to change this module because: {changes[:500]}"""

            review_response = await self._ai.ask_opus(
                review_prompt,
                system=ANALYSIS_REVIEW_SYSTEM,
                max_tokens=1024,
                purpose=f"analysis_review_{module_name}_attempt_{attempt + 1}",
            )

            review = self._extract_json(review_response)
            if review is None:
                review = {"approved": False, "feedback": "Failed to parse review"}

            await self._store_thought(
                f"analysis_review_{module_name}_{attempt + 1}",
                "opus",
                review_prompt[:500],
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
                    f"Changes: {changes[:200]}"
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

    async def _run_backtest(self, code: str) -> tuple[bool, str]:
        """Backtest generated strategy against recent historical data.

        Returns (passed, summary). Passes if strategy doesn't crash and
        doesn't produce catastrophic results (negative expectancy on >10 trades).
        """
        import importlib.util
        import sys
        import tempfile

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

            # Get recent 1h candle data for backtest
            candle_data = {}
            for symbol in self._config.symbols:
                df = await self._data_store.get_candles(
                    symbol, "5m", limit=8640
                )  # ~30 days of 5m
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
            import asyncio
            try:
                result = await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(
                        None, bt.run, candle_data, "5m"
                    ),
                    timeout=60,
                )
            except asyncio.TimeoutError:
                return False, "Strategy backtest timed out (>60s) — possible infinite loop"
            summary = result.summary()
            log.info("orchestrator.backtest_complete", summary=summary)

            # Fail only on catastrophic results (crash or very negative)
            if result.total_trades >= 10 and result.max_drawdown_pct > 0.15:
                return (
                    False,
                    f"Excessive drawdown: {result.max_drawdown_pct:.1%}. {summary}",
                )

            return True, summary

        except Exception as e:
            log.warning("orchestrator.backtest_error", error=str(e))
            return False, f"Strategy crashed during backtest: {e}"
        finally:
            import os

            # Clean up temp file
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            # Clean up leaked module from sys.modules
            sys.modules.pop("backtest_strategy", None)

    async def _store_observation(self, decision: dict) -> None:
        """Store daily observations in DB table (replaces strategy doc appends).

        Observations are the orchestrator's daily findings — rolling 30-day window.
        Strategy document updates are separate and rare (meaningful discoveries only).
        """
        try:
            # REPLACE: if cycle re-runs for same date, latest observation wins
            await self._db.execute(
                """INSERT OR REPLACE INTO orchestrator_observations
                   (date, cycle_id, market_summary, strategy_assessment, notable_findings)
                   VALUES (date('now', 'utc'), ?, ?, ?, ?)""",
                (
                    self._cycle_id or "unknown",
                    decision.get("market_observations", "")[:5000],
                    decision.get("reasoning", "")[:5000],
                    decision.get("cross_reference_findings", "")[:5000],
                ),
            )
            # Prune observations older than 30 days
            await self._db.execute(
                "DELETE FROM orchestrator_observations WHERE date < date('now', '-30 days')"
            )
            # Prune thoughts older than 30 days
            await self._db.execute(
                "DELETE FROM orchestrator_thoughts WHERE created_at < datetime('now', '-30 days')"
            )
            await self._db.commit()
            log.info("orchestrator.observation_stored", cycle_id=self._cycle_id)
        except Exception as e:
            log.warning("orchestrator.observation_store_failed", error=str(e))

    async def _log_orchestration(
        self, decision: dict, deployed_version: str | None = None
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
               (date, action, analysis, changes, strategy_version_from, strategy_version_to, tokens_used, cost_usd)
               VALUES (date('now'), ?, ?, ?, ?, ?, ?, ?)""",
            (
                decision.get("decision", "UNKNOWN"),
                json.dumps(decision, default=str),
                decision.get("specific_changes", ""),
                version_from,
                deployed_version,
                tokens_used,
                cost_today,
            ),
        )
        await self._db.commit()
