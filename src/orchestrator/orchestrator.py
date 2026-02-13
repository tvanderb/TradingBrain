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

**Strategy evolution** uses a candidate system:
- You can run up to {max_candidates} candidate strategies simultaneously in paper simulation.
- Each candidate mirrors the fund's portfolio at creation time and trades independently with live market data.
- Candidates go through the code pipeline (sandbox, code review, backtest) before deployment to a candidate slot.
- You choose how long to evaluate each candidate (or leave indefinite and promote when ready).
- You can cancel underperforming candidates at any time.
- When you promote a candidate, it becomes the active strategy. All other candidates are canceled.
- On promotion, you decide what happens to fund positions: "keep" (new strategy inherits them) or "close_all" (clean slate).

**Candidate execution:**
Candidates participate in every scan cycle. Same market data, same risk limits for signal sizing. Paper fills with slippage. Candidates never halt — risk halts only affect the fund.

**Decision types:**
- **NO_CHANGE**: Data keeps accumulating. Active candidates continue running.
- **CREATE_CANDIDATE**: Creates a new candidate strategy in a paper simulation slot. Goes through the code pipeline first.
- **CANCEL_CANDIDATE**: Cancels an underperforming or stale candidate. Frees the slot.
- **PROMOTE_CANDIDATE**: Promotes a candidate to become the active fund strategy. All candidates are cleared.
- **MARKET_ANALYSIS_UPDATE**: Rewrites the market analysis module (read-only, no paper test needed).
- **TRADE_ANALYSIS_UPDATE**: Rewrites the trade performance module (read-only, no paper test needed).

**Analysis module changes** — Sonnet generates → Opus reviews (math correctness focus) → sandbox → immediate deploy. No paper test needed (read-only modules).

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
### Backtester Capabilities and Limitations
The backtester runs against all available historical data: 5m (30 days), 1h (up to 1 year), 1d (up to 7 years). It iterates at 1h resolution using native multi-timeframe data. SL/TP checks use 5m resolution where available for intra-hour precision.

What the backtester does:
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
Strategy code runs in a sandboxed environment. Blocked modules: subprocess, os, shutil, socket, http, urllib, requests, httpx, websockets, aiohttp, sqlite3, aiosqlite, pathlib, sys, builtins, ctypes, importlib, types, threading, multiprocessing, pickle, io, tempfile, gc, inspect, operator. Blocked attribute access: __builtins__, __import__, __class__, __subclasses__, __bases__, __mro__, __globals__, __code__, __getattribute__, __dict__. Name-mangled private attributes are also blocked.

Available imports for your strategy code:
- pandas, numpy, ta (100+ technical indicators), scipy (stats, signal, optimize)
- Standard library: math, statistics, collections, dataclasses, datetime, functools, itertools, random, copy
- src.shell.contract (Signal, Action, Intent, OrderType, Portfolio, RiskLimits, StrategyBase, SymbolData, OpenPosition, ClosedTrade)

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
{{
    "decision": "NO_CHANGE" | "CREATE_CANDIDATE" | "CANCEL_CANDIDATE" | "PROMOTE_CANDIDATE" | "MARKET_ANALYSIS_UPDATE" | "TRADE_ANALYSIS_UPDATE",
    "reasoning": "Your analysis and the basis for your decision",
    "specific_changes": "What to build (CREATE_CANDIDATE only)",
    "slot": null,
    "replace_slot": null,
    "evaluation_duration_days": null,
    "position_handling": null,
    "cross_reference_findings": "Findings from comparing market conditions to trade outcomes",
    "market_observations": "Notable market observations from this cycle"
}}"""

CODE_GEN_SYSTEM = """You are a Python code generator for a crypto trading strategy.

You MUST:
1. Inherit from StrategyBase (imported from src.shell.contract)
2. Implement initialize(self, risk_limits: RiskLimits, symbols: list[str]) -> None
3. Implement analyze(self, markets: dict[str, SymbolData], portfolio: Portfolio, timestamp: datetime) -> list[Signal]
4. Keep the strategy in a single file
5. Include clear docstring explaining the strategy

You MUST NOT:
- Import os, subprocess, socket, http, or any network/filesystem modules
- Make any API calls or file I/O
- Use eval(), exec(), or __import__()
- Generate SHORT signals — the system is long-only (no margin, no leverage)

Available imports:
- pandas, numpy, ta, scipy (scipy.stats, scipy.signal, scipy.optimize)
- Standard library: math, statistics, collections, dataclasses, datetime, functools, itertools, random, copy
- src.shell.contract (Signal, Action, Intent, OrderType, Portfolio, RiskLimits, StrategyBase, SymbolData, OpenPosition, ClosedTrade)

The `ta` library provides 100+ technical indicators:
- ta.trend: SMA, EMA, MACD, ADX, Ichimoku, Aroon, CCI, DPO, KST, PSAR
- ta.momentum: RSI, Stochastic, Williams %R, ROC, TSI, Ultimate Oscillator
- ta.volatility: ATR, Bollinger Bands, Keltner Channel, Donchian, Ulcer Index
- ta.volume: OBV, VWAP, MFI, Chaikin Money Flow, Force Index, EMV
Usage: ta.trend.ema_indicator(close, window=12) or ta.momentum.rsi(close, window=14)

scipy.stats provides statistical tools:
- zscore, pearsonr, spearmanr, linregress, norm.cdf/ppf, skew, kurtosis
scipy.signal: argrelextrema (support/resistance level detection)
scipy.optimize: minimize (position sizing optimization)

### SymbolData — EXACT attribute names (do NOT use `.candles` — it does not exist)

  class SymbolData:
      symbol: str
      current_price: float
      candles_5m: pd.DataFrame   # Last 30 days of 5-min OHLCV
      candles_1h: pd.DataFrame   # Last 1 year of 1-hour OHLCV
      candles_1d: pd.DataFrame   # Last 7 years of daily OHLCV
      spread: float
      volume_24h: float
      maker_fee_pct: float       # Per-pair maker fee (%)
      taker_fee_pct: float       # Per-pair taker fee (%)

  Each DataFrame has columns: open, high, low, close, volume (DatetimeIndex).
  Access pattern:
      data = markets["BTC/USD"]
      df_1h = data.candles_1h
      close = df_1h["close"]
      rsi = ta.momentum.rsi(close, window=14)

  IMPORTANT: During backtesting, DataFrames may be short or empty at early timestamps.
  Always check length before applying indicators:
      if len(df_1h) < 50:
          continue  # Not enough data for this symbol yet

### Portfolio

  class Portfolio:
      cash: float
      total_value: float
      positions: list[OpenPosition]   # OpenPosition has: symbol, qty, avg_entry, current_price, unrealized_pnl, unrealized_pnl_pct, intent, stop_loss, take_profit, tag
      recent_trades: list[ClosedTrade]  # Last 100 — ClosedTrade has: symbol, entry_price, exit_price, pnl, pnl_pct, fees, intent
      daily_pnl: float
      total_pnl: float
      fees_today: float

### Signal output

Return list[Signal] with: symbol, action (BUY/SELL/CLOSE/MODIFY), size_pct (0.0-1.0 of portfolio), order_type (MARKET/LIMIT), limit_price (for LIMIT), stop_loss, take_profit, intent (DAY/SWING/POSITION), confidence, reasoning, slippage_tolerance (optional), tag (optional)

Fee awareness:
- MARKET orders use taker fees. LIMIT orders use maker fees (lower).
- Access per-pair fees via data.maker_fee_pct / data.taker_fee_pct.

Position tags:
- Each position has a unique tag. Access via position.tag in portfolio.positions.
- BUY without tag creates a new position. BUY with an existing tag averages in.
- SELL/CLOSE without tag targets the oldest position for that symbol.
- MODIFY requires a tag — updates SL/TP/intent without closing. Use size_pct=0.

### Performance rules (prevent backtest timeout)
- Do NOT call .copy() on large DataFrames — compute indicators on originals.
- Use ta's functional API (e.g., ta.momentum.rsi()) not class-based API.
- Add early returns / guard clauses for empty or insufficient data.

Output ONLY the Python code. No markdown, no explanation, just the code."""

CODE_REVIEW_SYSTEM = """You are a code reviewer for a trading strategy. Check for:

1. IO Contract compliance — correct inheritance, method signatures, return types
2. Safety — no forbidden imports, no side effects, no network calls
3. Logic correctness — edge cases, division by zero, empty data handling
4. Risk management — stop losses set, position sizing within limits
5. Long-only compliance — no SHORT signals (system has no margin access)
7. Tag hygiene — MODIFY signals must include a tag. MODIFY without tag will be rejected.
8. Data access correctness — see IO Contract below. Flag ANY wrong attribute name as an error.

### IO Contract (MUST match exactly — wrong names cause runtime crashes)

SymbolData attributes:
  .symbol (str), .current_price (float), .spread (float), .volume_24h (float)
  .candles_5m (DataFrame), .candles_1h (DataFrame), .candles_1d (DataFrame)
  .maker_fee_pct (float), .taker_fee_pct (float)

  THERE IS NO .candles, .data, .ohlcv, or .df attribute. Only candles_5m, candles_1h, candles_1d.
  Each DataFrame columns: open, high, low, close, volume (DatetimeIndex).

Portfolio attributes:
  .cash, .total_value, .positions (list[OpenPosition]), .recent_trades (list[ClosedTrade])
  .daily_pnl, .total_pnl, .fees_today

OpenPosition attributes:
  .symbol, .qty, .avg_entry, .current_price, .unrealized_pnl, .unrealized_pnl_pct
  .intent, .stop_loss, .take_profit, .tag, .side, .opened_at

Method signatures:
  initialize(self, risk_limits: RiskLimits, symbols: list[str]) -> None
  analyze(self, markets: dict[str, SymbolData], portfolio: Portfolio, timestamp: datetime) -> list[Signal]

Respond in JSON:
{
    "approved": true | false,
    "issues": ["..."],
    "feedback": "..."
}"""

BACKTEST_REVIEW_SYSTEM = """You are reviewing backtest results for a crypto trading strategy before it enters a candidate slot for forward testing.

These are simulation results — deterministic computation on a simplified market model.

**Known backtester limitations (do NOT penalize the strategy for these):**
- No order book depth, queue priority, or realistic fill latency
- No market impact modeling — large orders fill at the same slippage as small ones
- No overnight gaps or exchange outage simulation
- Historical data may not capture future market conditions

**Deployment context:**
- Approving means the strategy enters a candidate slot for forward paper testing alongside the active strategy
- Candidates trade with paper fills using live market data — no real money at risk
- Rejecting sends the strategy back for revision with your new direction

**Consider:**
- Trade count vs statistical significance (few trades = unreliable metrics)
- Drawdown severity and recovery patterns
- Win rate combined with risk/reward ratio
- Fee drag relative to gross P&L
- Whether the results suggest a real edge or noise

**If rejecting:** Provide specific, actionable revision instructions. Don't just say what's wrong —
say what to try differently. You are the fund manager directing a developer.
Examples: "Switch from momentum to mean reversion", "Add a volatility filter to reduce false signals",
"The entry criteria are too loose — require confirmation from multiple timeframes."

Respond in JSON:
{
    "deploy": true | false,
    "reasoning": "Your analysis of the backtest results and why you chose to deploy or reject",
    "concerns": ["Any concerns worth noting even if deploying"],
    "revision_instructions": "If rejecting: specific new direction for the next attempt. If deploying: empty string."
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

            # 1. Gather context
            context = await self._gather_context()

            # 2. Opus analysis
            decision = await self._analyze(context)

            # 3. Execute decision
            decision_type = str(decision.get("decision") or "NO_CHANGE").strip().upper()
            deployed_version = None

            if decision_type == "NO_CHANGE":
                report = f"Orchestrator: No changes. {decision.get('reasoning', '')}"
            elif decision_type in ("MARKET_ANALYSIS_UPDATE", "TRADE_ANALYSIS_UPDATE"):
                report = await self._execute_analysis_change(decision, context)
            elif decision_type == "CREATE_CANDIDATE":
                report = await self._create_candidate(decision, context)
            elif decision_type == "CANCEL_CANDIDATE":
                report = await self._cancel_candidate(decision)
            elif decision_type == "PROMOTE_CANDIDATE":
                report = await self._promote_candidate(decision)
                if "promoted" in report.lower():
                    ver_row = await self._db.fetchone(
                        "SELECT version FROM strategy_versions WHERE deployed_at IS NOT NULL ORDER BY deployed_at DESC LIMIT 1"
                    )
                    deployed_version = ver_row["version"] if ver_row else None
            else:
                log.warning("orchestrator.unknown_decision_type", decision_type=decision_type)
                report = f"Orchestrator: Unknown decision '{decision_type}' — treated as NO_CHANGE."

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
            "candidates": candidate_context,
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
- Max candidate slots: {self._config.orchestrator.max_candidates}
- Token budget: {context["token_usage"].get("used", 0)} / {context["token_usage"].get("daily_limit", 0)} tokens used today (${context["token_usage"].get("total_cost", 0):.4f})

---

## CANDIDATE STRATEGIES
{json.dumps(context.get("candidates", []), indent=2, default=str) if context.get("candidates") else "No active candidates. All slots available."}

---

## SIGNAL & OBSERVATION STATE
### Signal Drought Detection:
{json.dumps(context["signal_drought"], indent=2, default=str)}

### Recent Observations (last 14 days):
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

                # Code review
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

{json.dumps(decision, indent=2, default=str)}"""

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

            # Opus reviews backtest
            bt_review = await self._review_backtest(backtest_result, backtest_summary, decision, diff, attempt_history)

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
                await self._db.execute(
                    """INSERT INTO strategy_versions
                       (version, code_hash, description, backtest_result, market_conditions, code)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (version, code_hash, f"Candidate slot {slot}: {changes[:200]}",
                     backtest_summary, decision.get("market_observations", ""), approved_code),
                )
                await self._db.commit()

                if self._notifier:
                    await self._notifier.candidate_created(slot, version, eval_days)

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
        if self._notifier:
            await self._notifier.candidate_canceled(slot)
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

        # Get code and promote (cancels all candidates)
        code = await self._candidate_manager.promote_candidate(slot)

        # Deploy to active strategy file
        version = f"v{datetime.now().strftime('%Y%m%d_%H%M%S')}_promoted"
        code_hash = deploy_strategy(code, version)

        # Record in strategy_versions as deployed
        await self._db.execute(
            """INSERT INTO strategy_versions
               (version, code_hash, description, deployed_at, code)
               VALUES (?, ?, ?, datetime('now'), ?)""",
            (version, code_hash, f"Promoted from candidate slot {slot}", code),
        )
        await self._db.commit()

        # Signal main.py to reload strategy
        if self._scan_state:
            self._scan_state["strategy_reload_needed"] = True

        if self._notifier:
            await self._notifier.candidate_promoted(slot, version)
            await self._notifier.strategy_deployed(version, 0, f"Promoted from slot {slot}")

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
                df_5m = await self._data_store.get_candles(symbol, "5m", limit=8640)
                df_1h = await self._data_store.get_candles(symbol, "1h", limit=8760)
                df_1d = await self._data_store.get_candles(symbol, "1d", limit=2555)
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

        review_prompt = f"""Review these backtest results and decide whether to deploy the strategy to a candidate slot.

## Backtest Results
{summary}

## Strategy Change Context
{json.dumps({k: decision.get(k) for k in ("decision", "reasoning", "specific_changes")}, indent=2, default=str)}

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
            market_summary = decision.get("market_observations", "")
            strategy_assessment = decision.get("reasoning", "")
            log.info("orchestrator.observation_stored",
                     cycle_id=self._cycle_id,
                     market=market_summary or "",
                     assessment=strategy_assessment or "")
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
