# Discussions & Design Direction

## User Philosophy
- **Wants deep involvement in architecture decisions** — especially for complex features like self-evolution. Direct quote: "don't just go and make all the architectural and scope decisions on your own"
- **Prefers system transparency over AI magic** — when user asks for a report, they want to see what the system computed, not a freshly-generated AI response. The brain should show its own work.
- **Values rigorous note-taking** — wants notes to capture discussions, design direction, reasoning, and user preferences — not just todo checklists. Notes should be a living engineering journal.
- **Pragmatic about testing** — wants to paper test first, then iterate. Not afraid to run things and see what breaks.
- **Ambitious vision** — wants all 4 evolution levels (params, prompts, strategy composition, code gen). Not interested in a toy system.

## Design Direction: Telegram Commands
- `/status`, `/positions`, `/trades` — show real executor state
- `/report` — **must show existing scan calculations** (indicators, regime, signals), NOT call Claude for fresh analysis. Redesigned in session 2 after user feedback. Reads from shared `scan_state` dict populated by 5-min scan loop.
- `/signals` — shows signal history from DB (already works correctly)
- `/ask` — the ONE place where on-demand Claude calls are acceptable (user explicitly asks a question)
- `/report` old behavior was wrong: it called Claude with no market data, Claude said "I don't have access to market data." Even after I tried fixing it to include data, user pointed out the fundamental design issue: reports should reflect existing calculations, not generate new ones.

## Design Direction: Self-Evolution (Major Open Thread)
User wants 4 levels. Currently only Level 1 (parameter tuning) is implemented.

**Level 1: Parameter Tuning** (DONE)
- Executive brain (Opus) reads performance, adjusts numbers in strategy_params.json
- Max 20% change per cycle, all changes logged

**Level 2: Prompt Evolution** (NOT STARTED)
- Modify how the analyst brain evaluates signals
- Needs safety rails: A/B testing? Rollback mechanism?

**Level 3: Strategy Composition** (NOT STARTED)
- Enable/disable/combine indicator modules
- Could mean dynamic strategy weighting, adding new indicator combos

**Level 4: Code Generation** (NOT STARTED)
- Write new Python strategy functions
- Highest risk, needs sandbox, validation, human review
- User knows this is ambitious but wants it

**Key user statement**: "i would like to be involved with that discussion to the furthest extent" — this means a dedicated collaborative planning session, not me designing it solo.

**Status**: Deferred until paper trading validates the base system. User said "let's paper test then go back to working on expanding to a more ambitious plan."

## Discussion: Exchange Selection
- Started with Alpaca (US stocks + crypto)
- User is Canadian — Alpaca unavailable
- User suggested DEX (Uniswap/PancakeSwap)
- I explained gas fees would destroy sub-$1K accounts (Ethereum gas $2-50/trade, even BSC $0.10-0.50)
- Pivoted to Kraken CEX — user already had an account
- This simplified the system: crypto-only, no stock market hours logic, no PDT rules

## Discussion: Fee Impact on Small Accounts
- Initially assumed Kraken's published 0.16%/0.26% fees
- Reality at $0 volume tier: 0.25% maker / 0.40% taker
- Round trip: 0.65% (0.80% if both taker)
- On a $10 trade (5% of $200 portfolio), fees are ~$0.065
- Need a 2% BTC move just to cover fees on a $43 position — 27.7% of gross profit eaten
- This drives system design: higher conviction thresholds, larger minimum trade sizes, fee monitoring

## MAJOR PIVOT: IO-Container Orchestration Architecture (Session 2)

User proposed scrapping the three-brain architecture entirely in favor of a fundamentally different design:

### Core Concept: "Hot-Swapping IO-Container"
- **Rigid shell**: Kraken connection, risk management, Telegram, DB, scheduling — NEVER modified by agent
- **Flexible core**: A single strategy file (the "container") that the agent CAN modify, rewrite, replace
- **Rigid interface**: Strategy file must consume specific inputs and produce specific outputs — the IO contract
- **Orchestration**: Daily (12am-3am EST), AI agent reviews performance, reads long-term strategy document, decides whether to modify strategy
- **Hot-swap**: If strategy changes, new version is sandboxed, paper-tested for functionality, then swapped in while positions are cleared

### What the Agent CAN Do
- Modify/rewrite the trading strategy code
- Add new indicators, data sources (must be free/open-source)
- Install new Python modules (with validation)
- Update the long-term strategy document
- Reason about what to change and why

### What the Agent CANNOT Do
- Modify risk management limits
- Modify infrastructure (Kraken client, Telegram, DB)
- Spend money outside Kraken
- Exceed user-defined risk parameters

### User's Key Principles
- "I want it to be a closed loop that it can modify to improve performance"
- "The inner core is changeable by the agent so as to continuously refine trading strategies autonomously"
- Strategy should be simple enough for an agent to work with (ideally one file)
- Agent gets freedom to reason about its own approach
- Must produce standalone reports detailing reasoning and modifications
- Long-term strategy document is critical — serves as institutional memory

### My Concerns Raised
1. Code generation reliability — functional testing ≠ correctness testing
2. Overfitting to recent market conditions / catastrophic forgetting
3. Position clearing during hot-swap is complex
4. Module installation security surface
5. Need backtesting, not just "does it run?" testing
6. Rollback mechanism for failed strategies
7. Cold start — what's the initial strategy?
8. Token costs ($5-15/day for Opus reasoning)

### User Also Wants
- Google Vertex API support (has $300 credit) alongside Anthropic direct
- Fresh start — current codebase is wrong architecture for this design
- Full architecture discussion before writing any code
- Deep involvement in agent decision process design

### Open Questions (Needs User Input)
1. How autonomous? Can agent change pairs, scan frequency, position sizing?
2. Performance criteria — what's the "fitness function"?
3. Human approval gate for strategy changes, or fully autonomous + report?
4. Automatic rollback triggers or manual?
5. Strategy scope — strictly technical, or can it add sentiment/macro?
6. History depth for backtesting?
7. What's the initial strategy before agent starts iterating?

### Decision: Start Fresh
- Agreed to new branch (not separate project)
- Reuse: Kraken client, risk management, Telegram scaffolding, DB patterns
- Discard: Three-brain architecture, signal generators, executive/analyst classes

## Design Discussion Round 2 (Session 2 continued)

### Code Safety: Sonnet Generates, Opus Reviews
- Sonnet writes strategy code (cheaper)
- Opus reviews for correctness, edge cases, IO contract compliance
- Max 2-3 revision cycles before aborting
- Opus also reviews the agent's self-assessed risk tier
- Estimated cost per review cycle: $0.50-1.00

### Three-Tier Paper Testing
| Tier | Scope | Duration | Example |
|------|-------|----------|---------|
| 1 | Param tweaks | 1 day | RSI threshold 70→75 |
| 2 | Logic restructure | 2 days | Added VWAP filter |
| 3 | Fundamental overhaul | 1 week | Momentum→mean reversion |
- Agent self-classifies risk tier, Opus validates classification
- Early days: frequent iteration. Over time: stabilize.

### Position Clearing: Not Needed
- Hot-swap takes seconds (file replace + reload)
- Open positions transfer to new strategy management
- Shell-enforced risk limits protect regardless
- New strategy handles inherited positions via its own logic

### Trading Style: Full Spectrum Hedge Fund
- Day trading, swing trading, holding — all allowed
- Strategy Module decides timeframe per trade
- Positions tagged with intent (day/swing/hold)
- Different exit logic per intent type
- IO contract needs position metadata for this

### Skills Library
- Agent builds reusable indicator functions in `skills/` directory
- Strategy Module imports and composes them
- Skills must be pure functions (data in, result out, no side effects)
- Independently testable
- Over time, agent builds a toolkit

### Pip Install: Dropped
- Pre-install comprehensive analysis libraries
- If agent needs new library, that's a human decision

### Long-Term Strategy Document Structure
1. Current Market Thesis (updated daily)
2. Core Principles (hard-won lessons, prevents forgetting)
3. Active Strategy Summary (what and why)
4. Performance History (per version, with market conditions)
5. Risk Observations (dangerous patterns)
6. Adaptation Plan (deliberate forward-thinking)
7. Market Condition Playbook (approach per regime)
- Read by orchestrator EVERY night before decisions
- Prevents reactive changes, enforces deliberation

### Performance Criteria (Priority Order)
1. **Expectancy**: (win_rate × avg_win) - (loss_rate × avg_loss) — most important
2. **Win Rate**: User priority, also affects drawdown management
3. **Sharpe Ratio**: Risk-adjusted returns
4. **P&L**: Outcome metric, influenced by sizing
- Also track: Profit Factor, Max Drawdown, Time in Market

### Strategy Failure Thresholds
**Automatic rollback (shell-enforced):**
- 5% portfolio drop in a day → halt + rollback
- 10 consecutive losses → pause + review
- Strategy crashes → immediate rollback

**Orchestrator review triggers:**
- Win rate < 40% over 30+ trades
- Negative expectancy over 1 week
- Sharpe < 0.3 over 2 weeks
- Max drawdown > 8%

**Normal (not failure):**
- 3-5 losing trades in a row
- One bad day
- Temporary win rate dip during regime change

### Strategy Index (Database)
- Every version cataloged with metadata
- Tags, market conditions, parent version, test results
- Agent can query: "strategies that worked in ranging markets"
- Enables learning from history

### Backtesting Pipeline
1. Backtest against stored historical data (free, fast)
2. Paper test if backtest passes (free, slow, 1-7 days)
3. Deploy live if paper test passes
- Kraken OHLC: 720 candles/request, paginate for more
- Continuously store data locally, building history over time
- LLM cost is zero for backtesting itself

### User Preferences Confirmed
- Risk limits: hard-set, user-only. Max 5% per trade, default 1-2%
- Agent can adjust pairs, scan frequency, position sizing within limits
- Fully autonomous — no human approval gate
- Telegram notifications: trades, daily P&L, weekly report, strategy changes
- Agent should be focused, deliberate, less=more, long-term wins
- Not limited in approach but incentivized toward simplicity

### IO Contract — APPROVED
- Strategy class with: initialize(), analyze(), on_fill(), on_position_closed(), get_state(), load_state()
- Input types: SymbolData (multi-timeframe OHLCV), Portfolio, RiskLimits
- Output type: list[Signal] with action, size_pct, stops, intent, reasoning
- Strategy is pure logic — no network, no file I/O, no side effects
- Shell enforces risk limits as safety net
- Full details in architecture.md

### Data Tiering — APPROVED (7 Years)
- 0-30 days: 5-min candles
- 30 days - 1 year: 1-hour candles (aggregated nightly)
- 1-7 years: daily candles (aggregated nightly)
- Other data pruning: token logs aggregated after 3 months, signal history after 6 months
- Trade history: kept forever (tax + learning)
- Strategy versions: kept forever (small)

### Strategy Document Quarterly Refresh — APPROVED
- Every 4 quarters: distill lessons into Core Principles, archive old detail
- Active doc target: <2,000 words
- Yearly summaries archived at `strategy/archive/yearly/`
- Goal: the agent should be aware of historical market conditions, not just present

### Orchestrator Flow — APPROVED
1. Gather context (strategy doc, performance, index, code)
2. Opus analyzes: no change / tweak / restructure / overhaul
3. If change: Sonnet generates → Opus reviews → sandbox → backtest → paper test → deploy
4. Update strategy document with daily findings + market conditions
5. Generate report, notify via Telegram
6. Data maintenance (aggregation, pruning)
- Token budget: 150% of base → $22-45/month, $300 credit = 7-14 months

### Agent Should Document Daily
- Key findings about strategy performance that day
- Update previous findings if strategy has run more than one day
- Market conditions observed (critical for strategy document)
- Agent must be aware of historical AND present market conditions

### Agent Learning Curve
- Expect frequent iteration in first month (like a toddler learning to walk)
- Should slow down as it gains experience
- Agent should self-recognize when to stabilize vs. when to iterate

### Initial Strategy: EMA + RSI + Volume (v001) — APPROVED
- EMA 9/21 crossover, RSI 14 filter, volume 1.2x confirmation
- Day trading intent, 2% SL, 4% TP
- All 3 symbols from day one, agent can expand/contract

### Vertex API — APPROVED
- Config flag: provider = "anthropic" or "vertex"
- SDK has AsyncAnthropicVertex, same message format
- User has $300 Google credit

### Scan Frequency — APPROVED
- Default 5 minutes, strategy can request different via `scan_interval_minutes` property

### Notifications — APPROVED
- Trade alerts (entries + exits)
- Daily P&L summary
- Weekly performance report
- Strategy change alerts with reasoning summary
- Automatic rollback alerts
- Good wins highlighted

### Graceful Startup/Shutdown — APPROVED
- Shutdown: save strategy state, cancel unfilled orders, do NOT close positions, clean exit
- Startup: load config, restore strategy state, reconcile positions (DB vs Kraken in live), resume
- DB is single source of truth. Process is stateless except for strategy state (saved every scan).
- Position reconciliation in live mode: DB vs Kraken, update DB to match exchange
- Pending paper tests resume on startup
- Systemd service on VPS with auto-restart on crash
- Full details in architecture.md

## All v2 Core Design Decisions FINALIZED — Built and Tested
- v2 implementation COMPLETE on `v2-io-container` branch
- 18/18 integration tests passing
- All components built per architecture.md spec
- Ready for paper trading with user's .env credentials

---

## Statistics Shell & Orchestrator Awareness (Session 4, 2026-02-08)

### The Problem
The orchestrator receives performance summaries and raw trade data, then Opus has to reason about both "what happened" and "what the numbers mean." LLMs are bad at math. If Opus miscalculates expectancy or misinterprets a distribution, it makes systematically wrong decisions — potentially for weeks. This is more dangerous than a single bad trade.

Additionally, the orchestrator lacks situational awareness:
- Doesn't know how long the system has been running
- Can't see how close signals came to triggering (scan history not stored)
- Doesn't know the market regime when trades happened
- Can't distinguish between "no signals because strategy is broken" and "no signals because market is ranging"

### User's Core Insight
The orchestrator shouldn't perform its own statistical analysis on raw data. It should receive hard-computed statistics from rigid code, AND have the ability to design what statistics it receives — like designing its own dashboard.

But these statistics are NOT safe just because they're read-only. **Miscalculated statistics can cause major losses** by leading the orchestrator to systematically wrong conclusions. A bug that inflates expectancy could prevent a necessary rollback while the portfolio bleeds.

### Solution: Three-Layer Input Architecture

**Layer 1: Truth Benchmarks (rigid shell, orchestrator CANNOT change)**
Simple, trivially verifiable metrics computed from raw data:
- Actual net P&L, trade count, win/loss count, win rate
- Actual fees paid, actual portfolio value
- Actual max drawdown, consecutive loss streak
- System operational stats (uptime, total scans, scan success rate)

These are the "weighing scale." If the statistics module contradicts these, the orchestrator knows its analysis is wrong, not reality.

**Layer 2: Statistics Module (flexible, orchestrator CAN rewrite)**
A single Python file (like the strategy module) that the orchestrator designs and rewrites:
- Receives: read-only database connection + schema documentation
- Returns: dict of computed metrics (structured report)
- Can query exactly the data it needs (efficient, no unnecessary data loading)
- Pure computation — no writes, no network, no file I/O
- Sandbox validated, Opus code-reviewed with emphasis on mathematical correctness
- No paper testing required, but review must verify the math

**Layer 3: User Constraints (rigid, orchestrator CANNOT change)**
- Risk limits, config settings, symbol selection
- Token budget, orchestration window

### Orchestrator Self-Awareness
The orchestrator must understand its own architecture — what it receives and what each input means:
1. **Ground Truth** (rigid) — truth benchmarks, raw trade records, portfolio state, market prices
2. **Its Own Analysis** (flexible, it can change) — statistics module output, strategy document
3. **Its Own Strategy** (flexible, it can change, paper-tested) — active strategy code, version history
4. **User Constraints** (rigid, it cannot change) — risk limits, config

This labeling must be explicit in the orchestrator's system prompt so it reasons correctly about what to trust vs what to question.

### Orchestrator Goals — Explicit and Prioritized
**Primary goal**: Achieve positive expectancy after fees.
**Secondary goals** (priority order):
1. Win rate above 45% sustained
2. Sharpe ratio above 0.3
3. Positive monthly net P&L

**Meta-goals** (how it operates):
- Be conservative — when uncertain, don't change
- Build understanding before acting — accumulate data, observe conditions
- Improve its own observability — evolve the statistics module
- Maintain institutional memory via strategy document
- Recognize context — "3 days running with 0 trades" ≠ "3 months with 0 trades"

### Statistics Module vs Strategy Module

| Aspect | Strategy Module | Statistics Module |
|--------|----------------|-------------------|
| **Produces** | Trading signals | Analysis report |
| **IO Contract IN** | SymbolData, Portfolio, RiskLimits | Read-only DB connection + schema |
| **IO Contract OUT** | list[Signal] | dict of computed metrics |
| **DB Access** | None | Read-only |
| **Network** | None | None |
| **Orchestrator rewrites** | Yes | Yes |
| **Sandbox** | Yes (AST, no I/O) | Yes (no writes, no network) |
| **Code review** | Safety + correctness | Safety + mathematical correctness |
| **Paper test** | Yes (1-7 days by tier) | No (can't execute trades) |
| **Danger if wrong** | Bad trades (direct loss) | Bad analysis (systematic wrong decisions) |

### Data Collection Gaps Identified
Current system does NOT store enough data for meaningful statistical analysis:

| Missing Data | Why It Matters | Solution |
|---|---|---|
| Scan results | Can't see indicator values over time, can't measure "how close to signal" | New `scan_results` table |
| Regime at trade time | Can't compute "win rate by market regime" | Tag regime on trades + signals |
| Intraday portfolio values | Only daily snapshots, can't compute intraday drawdown | More granular snapshots or compute from trades |
| Spread at trade time | Can't analyze actual vs simulated execution costs | Store spread in trade records |

### Read-Only DB Access Decision
User raised concern about efficiency: loading ALL data as DataFrames every night is wasteful and gets worse over time. Decision: give the statistics module a read-only database connection with schema understanding. It writes its own queries, pulls exactly what it needs.

This is more efficient, more flexible, and still safe (read-only connection cannot modify data). The sandbox rules differ from strategy: statistics module CAN read from DB, CANNOT write/network/filesystem.

### Key User Statements
- "these statistics, if miscalculated or misinterpreted can cause major losses and damage long-term critical understanding"
- "the orchestrator must be able to compare what is computed to closer-to-truth data"
- "we can't have miscalculations"
- "the orchestrator must understand what is providing it input, what is hard truths and what it can and is supposed to change"
- "it should probably have clear goals"
- "should the orchestrator be able to choose what data it's looking to use to avoid unnecessary IO and memory usage?"
