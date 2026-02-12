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

## Design Direction: Strategy Evolution
> **Note**: v1 had a 4-level evolution concept (parameter tuning → prompt evolution → strategy composition → code gen). This was replaced entirely by the IO-container architecture where the orchestrator rewrites the strategy module directly. Sonnet generates code, Opus reviews, sandbox validates, backtest → paper test → deploy. See architecture.md for current design.

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

### Performance Criteria
> **Superseded** by fund mandate (Sessions 7-8). No prioritized list — orchestrator determines what matters. All metrics tracked by truth benchmarks (expectancy, win rate, P&L, profit factor, drawdown, fees). Mandate: "Portfolio growth with capital preservation. Avoid major drawdowns."

### Strategy Failure Thresholds
> **Note**: Specific values below are from the v2 design phase and were subsequently updated. Shell-enforced limits now come from `config/risk_limits.toml` (6% daily loss, 12% drawdown, 999 consecutive losses). Orchestrator review triggers were **removed entirely** per the fund mandate framework (Sessions 7-8) — the orchestrator uses its own judgment, not hardcoded thresholds.

**Shell-enforced (values from config):** Daily loss halt, drawdown halt, strategy crashes → rollback.
**Orchestrator:** No numeric triggers — uses identity + awareness to decide when to act.

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

### Orchestrator Goals — Superseded
> **Note**: This section was from Session 4. It was scrapped in Sessions 7-8 and replaced by the fund mandate framework: "Portfolio growth with capital preservation. Avoid major drawdowns. Long-term fund." No explicit goals, no meta-goals as directives — the orchestrator's behavior emerges from identity (Layer 1) + system awareness (Layer 2) + institutional memory (Layer 3). See "Orchestrator Prompt Design Framework" section below.

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

### Regime Classification is NOT Truth (Continued Discussion)
User questioned whether regime classification is reliable ground truth. Answer: no. Regime is a heuristic interpretation — different algorithms classify the same market differently. Raw indicator values (price, EMA, RSI, volume) ARE truth. Regime labels are analysis.

Decision: Store raw values in scan_results. Tag the strategy's regime classification on trades as `strategy_regime` — a fact about what the strategy *thought*, not what the market *was*. Analysis modules derive their own regime views from raw data.

### Two Analysis Modules vs One (Continued Discussion)
User asked whether historical exchange data analysis should be a separate module from trade performance analysis. After weighing pros/cons:

**Chose: Two separate modules (Option B)**

Key arguments for separation:
- Market analysis is valuable from day one (rich candle data). Trade performance needs trades.
- Different domains, different evolution velocities
- Fault isolation — one crash doesn't kill both reports
- Cleaner code review per domain
- Cross-referencing solved by shared DB access — both modules can query any table

Key arguments against (accepted as tradeoffs):
- More files/complexity — mitigated by shared infrastructure (one loader, one sandbox)
- Cross-referencing slightly less natural — mitigated by read-only DB access for both
- Larger orchestrator decision space — accepted, orchestrator is capable

### Orchestrator Must Understand Its Own Architecture
User emphasized: the orchestrator needs to understand what provides its inputs, what is hard truth, and what it can change. This must be explicit in the system prompt with clear labeling:
1. GROUND TRUTH (rigid benchmarks — cannot change)
2. YOUR MARKET ANALYSIS (designed by orchestrator — can change)
3. YOUR TRADE ANALYSIS (designed by orchestrator — can change)
4. YOUR STRATEGY (designed by orchestrator — can change, paper-tested)
5. USER CONSTRAINTS (risk limits, config — cannot change)

The orchestrator should also have explicit goals (not just vibes) so it knows what it's optimizing for.

### Additional User Insight: Analysis Modules Can Cause Losses
User corrected the characterization of analysis modules as "read-only, can't lose money." Miscalculated statistics can cause major losses by leading the orchestrator to systematically wrong decisions. The truth benchmarks exist specifically so the orchestrator can detect disconnect between its own analysis and reality.

Code review for analysis modules must verify mathematical correctness, not just code safety. This is as important as reviewing the trading strategy.

---

## System Critical Review & Gap Analysis (Session 7, 2026-02-08)

Full critical review of the system identified 12 categories of risks and gaps. User responded to each with design direction. Everything below is captured before implementation.

### 1. Signal Drought — AGREED, BUILD DROUGHT DETECTOR
- **Problem**: Strategy requires 3 conditions ANDed (EMA crossover + RSI filter + volume). In ranging markets (60-70% of crypto), this produces near-zero signals. Could go a week with no trades.
- **User decision**: Build a drought detector. When 0 signals in N days, the orchestrator should be prompted differently — not "review performance" (there is none) but "diagnose why no signals are being generated and whether the strategy's filters are too restrictive for current conditions."
- **Key principle**: Drought is a distinct system state that requires different orchestrator behavior, not just "wait for more data."

### 2. Feedback Loop Speed — KEEP DAILY CADENCE, EMPOWER THE ORCHESTRATOR
- **Problem**: 20+ trades needed for statistical judgment, but strategy may only generate 1-3/week. That's 7-20 weeks. Meanwhile orchestrator runs nightly spending $0.20+ on "still not enough data."
- **User decision**: Keep daily cadence. "Imagining a hedge fund, the owner/executive would be reviewing performance daily." Monthly check-ins won't work. The orchestrator is the fund manager — give it the right tools and guardrails.
- **User's key question**: "What data do we generate other than trading data? We should be able to poll historical market data from Kraken."
- **User's insight**: The strategy's signal rate is up to the orchestrator — it can change the strategy. The cold start strategy can also be changed.
- **Direction**: Empower the orchestrator to use all available data sources (historical Kraken data, scan results, market analysis) even when trade data is sparse. The orchestrator should be able to learn from market data, not just trade outcomes.
- **OPEN**: What does the cold start strategy look like? What additional data sources should the orchestrator have access to?

### 3. Guardrails Against Premature Action — NO HARD GATE, TRUST ALIGNED AGENT
- **Problem**: Nothing prevents orchestrator from changing strategy every night with insufficient data.
- **User decision**: "I don't like a hard-gate against changes. A well-informed agent that is aligned with our goals won't choose to frequently change strategy for bad reasons."
- **User's philosophy**: "We need to empower this orchestrator to manage this fund to the best of its abilities." If we properly empower and align the orchestrator, premature action shouldn't be a problem.
- **Direction**: Instead of hard gates, focus on better alignment — better prompts, better context, clearer goals, better self-awareness. The orchestrator should naturally make good decisions, not be prevented from making bad ones.
- **Counter-argument acknowledged**: This relies on prompt engineering quality. If alignment is wrong, the system has no safety net against rapid strategy churn.

### 4. Paper Test Enforcement — INFORM THE ORCHESTRATOR, DON'T HARD-GATE
- **Problem**: paper_tests table entries are created but never checked. Orchestrator can overwrite strategies mid-paper-test.
- **User decision**: "The orchestrator should be made aware of our paper testing system and should also be told if it has a strategy currently in testing before deployment. This is what I mean by empowering the orchestrator."
- **Direction**: Include paper test status in the orchestrator's context. If a strategy is mid-paper-test, the orchestrator sees this and should respect it. Don't add a hard gate that prevents deployment.
- **User's question**: "Do you see any problems with this?" — needs discussion.

### 5. Backtester Data Mismatch — FIX, USE 5m CANDLES
- **Problem**: Backtester runs on 1h candles but strategy uses 5m candles. Results are essentially meaningless.
- **User decision**: "This is a clear problem, let's adjust to use 5-minute candles for backtesting."
- **Status**: Straightforward fix.

### 6. Strategy Evolution "Rewrite From Scratch" — ADD DIFF CONTEXT TO REVIEW
- **Problem**: Sonnet regenerates the entire strategy file. Could subtly change things beyond the intended scope.
- **User decision**: Opus should receive a diff of old vs new strategy alongside the change purpose, so it can verify scope alignment.
- **User's question**: "Do you propose any additional or alternative approaches?" — needs discussion.

### 7. No Mechanism to Evaluate Whether Changes Helped — CRITICAL, MUST SOLVE
- **User's words**: "just changing strategy without being able to understand what the changes actually did is a fatal flaw in this entire system."
- **Direction**: The orchestrator needs version-partitioned performance data. It must be able to compare strategy v002's results against v001's results.
- **OPEN**: How exactly? Per-version trade metrics? Version-aware trade performance module? Rolling comparison windows?

### 8. Strategy Document Growing Without Bound — REVIEW ENTIRE STRATEGY DOC SYSTEM
- **User's words**: "Why is the strategy document growing linearly? This was supposed to be a semi-fixed long term plan."
- **User's concern**: "Trades should be tagged with strategy version. We need to review the entire strategy system. Things are clearly messy here."
- **Direction**: The strategy document's purpose and update mechanism need redesign. It was supposed to be institutional memory (~2,000 words, quarterly distillation) but is being used as a daily append log.
- **OPEN**: What should the strategy document actually contain? How should daily observations be stored? Should they go in the strategy doc or somewhere else?

### 9. Analysis Module Evolution Unguarded — TRUST ALIGNED AGENT
- **User's response**: "Why would the orchestrator do this? If it's misaligned this makes sense, but if we implement this system properly and empower the agent correctly, why would it do things to screw up its own trading practice?"
- **Direction**: Same philosophy as #3 — focus on alignment, not gates. If the orchestrator is well-informed and goal-aligned, it won't rewrite analysis modules arbitrarily.

### 10. Rough/Unpolished Items — CREATE FIX LIST
- **User decision**: "Let's make a clear and detailed list of these to fix before major deployment."
- Items identified: WebSocket silent failure, strategy_state table bloat, Reporter lacks statistical rigor, position monitor 30s gaps, import path fragility, token budget enforcement, orchestrator error handling, data store aggregation untested.

### 11. Scenario Failures — ADDRESSED BY ABOVE + VPS
- **User decision**: "Does fixing the above problems help with most of this?" (Yes, most scenario failures stem from the gaps above.)
- **User decision**: "Let's not design the system to run on a laptop, it is not designed to. Let's intend this to run on a dedicated 24/7 Linux machine — a VPS."
- **Direction**: Remove laptop-as-deployment from design thinking. System targets VPS from now on. Laptop is dev-only.

### 12. Philosophical Gaps

**"Less is more"**: User clarified — "generally speaking, but you have to be careful to consider what situation you're in. Sometimes 'less' may be a whole lot in a situation where 'more' is massive. We want to design an autonomous and adaptive system here. Let's make it pretty but let's make it work too."

**Autonomy vs cold start / supervised mode**: User's first thought: "we need to let it learn, so I don't know about a supervised period." Wants to hear more before deciding. — OPEN for discussion.

**Observability vs actionability**: User strongly rejects actionability for the observer. "We must design the system so that the orchestrator can act responsibly on its own. Observability is so that I can be up to date on what's going on with the fund and what the orchestrator is doing with my money."
- **User's analogy**: "Think of the orchestrator like a hedge fund manager and the user is an investor. The investor can take his money out, but he doesn't get to control the operations at the firm."
- **Direction**: No `/rollback` command. No user intervention in operations. Design the orchestrator to be competent enough that manual intervention is unnecessary. User's only controls: `/pause`, `/resume`, `/kill` (pull money out).

### Open Questions — ALL RESOLVED
All 6 open questions from round 1 were resolved in round 2 (Session 7) and subsequently implemented (Sessions 8-9). See round 2 sections below and progress.md for implementation details.

---

## System Critical Review — Round 2 (Session 7 continued)

### Overarching Design Philosophy (crystallized from user's responses)
**"Maximize awareness, minimize direction."** The orchestrator's decision quality comes from the quality of information it receives, not from the quality of instructions telling it what to think. Don't tell it what to focus on. Give it full context about the system, its state, its capabilities, and its constraints — then let it reason freely.

Key user statements:
- "We should actually avoid telling it what to focus on"
- "Not 'think this way'"
- "We need to avoid influencing it as much as possible. It should be able to work freely off data, not prompts."
- "Your goal is to decide what to do — now do it."
- "Let it be aware of its environment, the way the system it's working in works, and particularly what it can and can't do"

This is a shift from the current ANALYSIS_SYSTEM prompt which has directive guidance ("minimum ~20 trades before judging," "prefer NO_CHANGE when data is insufficient," "be conservative"). User wants these replaced with context and awareness, not behavioral directives.

### 1. Cold Start — DECIDED: Historical Data Bootstrap + Awareness Over Direction
- **On day 1**: System automatically polls Kraken for X months of historical data. Orchestrator has market data to analyze even with 0 trades.
- **Prompt approach**: Don't tell the orchestrator "focus on market analysis during cold start." Instead, give it full awareness of what data exists, what the system can do, and let it decide. "You have 0 trades, 3 months of market data, and a backtester — do what you think is right."
- **User observation**: "We've observed it's not a bad decision maker when it is aware." The e2e test proved this — Opus correctly chose NO_CHANGE with appropriate reasoning when given good context.
- **TODO**: Implement historical data bootstrap on first startup. Determine how much history to fetch (likely 30 days of 5m = ~8,640 candles per symbol, paginated).

### 2. Paper Test — DECIDED: System Awareness, Not Just State Awareness
- **Key distinction**: Orchestrator needs to know not just "there's a paper test running" (state) but "if you deploy a new strategy, the current paper test terminates and its results become incomplete" (system consequences).
- **Design principle**: The orchestrator should understand HOW the system works, not just WHAT its current state is. Awareness of mechanisms and consequences, not just data.
- **TODO**: Include paper test status + consequence explanation in orchestrator context.

### 3. Strategy Evolution — DECIDED: Three Approaches Combined
- **Targeted edit instructions**: For tier 1 tweaks, tell Sonnet specifically what to change rather than regenerating everything. For tier 2-3, full rewrite is appropriate.
- **Version lineage tracking**: Populate `parent_version` and structured `changes` field on strategy_versions. Orchestrator can trace which changes helped/hurt.
- **Diff context for review**: Opus receives old code, new code, and the diff alongside change purpose to verify scope alignment.
- **TODO**: Implement all three. Tier-dependent generation approach.

### 4. Version-Partitioned Performance — DECIDED: All Three Mechanisms
- **A**: Trade performance module partitions by strategy_version (GROUP BY).
- **B**: Orchestrator receives version comparison summary (careful with length — concise, not exhaustive).
- **C**: Strategy versions table populated with lifetime results when version is retired.
- **User note**: "We just need to be careful with the comparison summary, we shouldn't let that get too long while still providing the right context."
- **TODO**: Implement A first (simplest), then B and C.

### 5. Strategy Document — DECIDED: Split Into Two
- **Strategy Document** (`strategy/strategy_document.md`): Semi-permanent institutional memory, ~2,000 words. Updated infrequently — only when orchestrator has a meaningful discovery. Contains: thesis, core principles, playbook, known failure modes.
- **Daily Observations** (new DB table `orchestrator_observations`): Append-only with rolling window (keep 30 days, summarize older). Fields: date, market_summary, strategy_assessment, notable_findings. Orchestrator reads last 7-14 days as context.
- **Current problem**: Strategy doc is being used as a daily append log, growing linearly. This separates the concerns.
- **TODO**: Create observations table, modify orchestrator to write observations there instead of appending to strategy doc, keep strategy doc updates rare and meaningful.

### 6. Orchestrator Maturity & Risk Tolerance — DECIDED: Three-Layer Framework
- **Problem**: After a few losing trades, the orchestrator might panic and make premature changes. Need it to be risk-tolerant, confident, data-driven.
- **User's framing**: "We're building a responsible, mature hedge fund manager, not an amateur day-trader."
- **Resolution**: Maturity comes from three distinct layers, each living in a different place. See "Orchestrator Prompt Design Framework" below — this is the governing design document for all orchestrator prompting.

---

## Orchestrator Prompt Design Framework (APPROVED — Governing Design)

> This framework governs ALL orchestrator prompt design. Any future prompt changes must align with these principles. This is not a suggestion — it's the architecture.

### Core Philosophy: "Maximize Awareness, Minimize Direction"

The orchestrator's decision quality comes from the **quality of information** it receives, not from **instructions telling it what to think**. We do not tell the orchestrator what to focus on, what to prefer, or how to weigh options. We give it full context about the system, its state, its capabilities, and its constraints — then let it reason freely.

**What this means in practice:**
- NO: "Be conservative" / "Prefer NO_CHANGE when data is insufficient"
- YES: Full awareness of sample sizes, statistical significance, and what data exists
- NO: "Minimum ~20 trades before judging strategy performance"
- YES: Understanding that statistical significance requires sufficient samples (identity, not rule)
- NO: "Don't chase short-term noise" / "If you lack information, update analysis modules first"
- YES: Access to all data, understanding of all tools, freedom to decide

**Why:** A well-informed agent with the right identity makes better decisions than a constrained agent following rules. Rules create brittleness — the agent follows them even when the situation calls for an exception. Identity creates judgment — the agent adapts to the situation.

### The Three Layers

Orchestrator behavior emerges from three distinct layers, each living in a different place, each serving a different purpose:

#### Layer 1: Core Identity (System Prompt) — WHO it is

**Purpose:** Give the orchestrator its character, mental models, and self-understanding. This is the permanent foundation that doesn't change over time.

**Contains:**
- Professional identity: experienced fund manager who has seen market cycles, understands variance, knows the difference between noise and signal
- Statistical intuition: understands that small samples are unreliable, that losing streaks are normal, that premature reaction destroys edge
- Risk philosophy: knows that every trade starts in the red (fees), that the fee wall shapes what's viable, that position sizing matters on small accounts
- Self-awareness: understands its own architecture — what it can change, what it can't, how its decisions propagate through the system
- Intellectual honesty: willing to admit uncertainty, distinguish between "I don't know" and "this is bad"

**Does NOT contain:**
- Behavioral directives ("be conservative," "prefer X over Y")
- Numeric thresholds ("wait for 20 trades," "win rate > 45%")
- Prioritized goal lists telling it what to optimize for
- Decision heuristics ("if X, then do Y")

**The key distinction:** Identity statements describe WHO the orchestrator is. Directive statements tell it WHAT to do. We want the former, not the latter.

Examples of identity vs directive:
| Directive (BAD) | Identity (GOOD) |
|---|---|
| "Be conservative — don't change what's working" | "You understand that stability compounds and unnecessary changes introduce risk" |
| "Minimum ~20 trades before judging" | "You know that statistical conclusions from small samples are unreliable" |
| "Prefer NO_CHANGE when data is insufficient" | "You are comfortable with uncertainty and patient enough to wait for clarity" |
| "Don't chase short-term noise" | "You distinguish between variance and persistent patterns" |
| "Fewer trades, bigger moves" | "You understand fee economics and their impact on strategy viability" |

#### Layer 2: System Understanding (System Prompt) — WHAT it's working with

**Purpose:** Factual context about the system, the market environment, and the tools available. Pure awareness — no direction about what to do with the information.

**Contains:**
- How the system works: scan loop (5m), position monitor (30s), data pipeline, paper testing mechanism
- What happens when decisions are made: deploying a new strategy terminates any active paper test, analysis module changes take effect immediately, strategy changes go through backtest → paper test pipeline
- Available tools: backtester (can test ideas before deploying), analysis modules (can be rewritten to measure different things), strategy (can be tweaked or overhauled)
- Market facts: fee structure (0.25% maker / 0.40% taker), available pairs, data retention (5m 30d, 1h 1yr, daily 7yr)
- Data landscape: what tables exist, what data is collected, what can be queried
- Constraints: risk limits (hard shell, cannot change), token budget, operational parameters

**Does NOT contain:**
- Suggestions about what to do ("if you lack data, update analysis modules")
- Preferences ("analysis updates are lower-risk than strategy changes")
- Priorities ("update observability before changing strategy")

#### Layer 3: Institutional Memory (Strategy Document) — WHAT it's learned

**Purpose:** Hard-won lessons accumulated through experience. Earns its place by being validated through actual trading results. Grows slowly over time.

**Contains:**
- Specific discoveries: "v003's mean-reversion approach lost money because crypto trends are too strong and fees eat the small gains"
- Market-specific knowledge: "SOL/USD has higher slippage than BTC/USD in low-volume periods"
- Strategy lineage: what was tried, what worked, what didn't, and why
- Current thesis and rationale (updated infrequently, only on meaningful discoveries)
- Market condition playbook (built from experience, not theory)

**Does NOT contain:**
- Daily observations (those go in the observations DB table)
- Behavioral instructions
- Generic trading wisdom that wasn't earned through this system's experience

**Key principle:** The strategy document is EARNED knowledge, not pre-loaded wisdom. On day 1, it should be nearly empty — just the v001 description and known fee structure. Everything else is discovered.

### How the Layers Interact

```
Day 1:
  Identity (rich)    + System Understanding (rich) + Memory (sparse)
  = Conservative decisions driven by self-awareness and humility

Day 30 (after trades):
  Identity (same)    + System Understanding (same) + Memory (growing)
  = More informed decisions, strategy doc captures what worked/failed

Day 180 (mature):
  Identity (same)    + System Understanding (same) + Memory (rich)
  = Confident decisions backed by extensive institutional knowledge
```

The orchestrator's behavior EVOLVES over time not because we change its instructions, but because its institutional memory grows. This is how real fund managers work — their character stays the same, but their judgment improves with experience.

### What This Means for Current Implementation

The current `ANALYSIS_SYSTEM` prompt violates this framework in multiple ways:
1. Contains behavioral directives disguised as goals ("be conservative," "prefer NO_CHANGE")
2. Contains numeric thresholds ("minimum ~20 trades")
3. Contains decision heuristics ("if you lack information, update analysis modules first")
4. Mixes identity, system understanding, and directives in a single prompt
5. Goals section tells the orchestrator what to optimize — should be awareness of what matters, not instructions to optimize

The prompt needs to be redesigned from scratch following this framework. See "Misalignment Audit" for the full list of changes needed.

### Implications for Other Prompts

- **CODE_GEN_SYSTEM**: Mostly fine — it's a technical spec for code generation, not a behavioral prompt. The constraints (must inherit StrategyBase, must not import os) are system facts, not behavioral directives.
- **CODE_REVIEW_SYSTEM**: Mostly fine — review criteria are system requirements. But should receive diff + change purpose for scope verification.
- **ANALYSIS_CODE_GEN_SYSTEM**: Same as CODE_GEN — technical spec, appropriate.
- **ANALYSIS_REVIEW_SYSTEM**: Same as CODE_REVIEW — technical criteria, appropriate.

### Relationship to Other Decisions

- **Signal drought detector**: Feeds into system understanding — orchestrator knows drought detection exists and what it reports
- **Paper test awareness**: System understanding layer — orchestrator knows how paper tests work and what deploying does to an active test
- **Version-partitioned performance**: Feeds into the data the orchestrator receives — it can see per-version results
- **Daily observations table**: Separates daily logs from institutional memory, keeping the strategy document focused
- **Targeted edits for strategy evolution**: A tool the orchestrator has available (system understanding), not something it's told to prefer

---

## Misalignment Audit (Session 7)

Everything below is currently misaligned with the approved framework and decisions. Organized by file.

### 1. `src/orchestrator/orchestrator.py` — ANALYSIS_SYSTEM prompt

**Problem:** The entire prompt needs redesign per the three-layer framework.

Specific violations:
- **"Your Goals (in priority order)"** — Tells the orchestrator what to optimize. Should be system awareness (fee structure, what metrics exist) not optimization directives.
- **"Be conservative — don't change what's working"** — Behavioral directive. Should be identity: understands that stability compounds.
- **"Build understanding before acting — prefer NO_CHANGE when data is insufficient"** — Tells it what to prefer. Should be identity: comfortable with uncertainty.
- **"Minimum ~20 trades before judging"** — Numeric threshold directive. Should be identity: understands statistical significance.
- **"If you lack information to decide, update analysis modules first"** — Heuristic telling it what to do. Should be system awareness: analysis modules are a tool available to it.
- **"Analysis module updates are low-risk — prefer them when unsure"** — Preference directive. Remove.
- **"Don't chase short-term noise"** — Directive. Identity handles this.
- **"Fewer trades, bigger moves"** — Directive disguised as wisdom. Fee awareness handles this.
- **"strategy_doc_update" in JSON response** — Forces nightly strategy doc append. Should be replaced by observations table. Strategy doc updates should be rare and meaningful.

**Action:** Rewrite ANALYSIS_SYSTEM from scratch following the framework. Split into identity + system understanding sections. Remove all directives.

### 2. `src/orchestrator/orchestrator.py` — _analyze() prompt

**Problem:** The nightly analysis prompt builds context but is missing key information.

Missing context:
- **Paper test status** — Orchestrator doesn't know if a strategy is mid-paper-test or the consequences of deploying over it.
- **System mechanism awareness** — Doesn't explain how deployment works, what paper tests do, what happens to data when a strategy changes.
- **Signal drought state** — No drought detection data included.
- **Version-partitioned performance** — Trades aren't broken down by strategy_version.
- **Historical data availability** — Orchestrator doesn't know what historical data exists or that the backtester is available.

JSON response changes needed:
- Remove `"strategy_doc_update"` — replace with observations table write
- Add mechanism for orchestrator to flag when strategy doc SHOULD be updated (rare, meaningful discoveries only)

### 3. `src/orchestrator/orchestrator.py` — _execute_change()

**Problems:**
- **No diff sent to Opus reviewer** — Opus sees only the new code, not what changed or why.
- **No targeted edit option** — Always full rewrite, even for tier 1 tweaks.
- **parent_version never populated** — `strategy_versions` table has the column but it's always NULL.
- ~~**Backtest uses 1h candles**~~ — FIXED (commit c9ae53e, now uses 5m)

### ~~4. `src/orchestrator/orchestrator.py` — _update_strategy_doc()~~ FIXED (commit 7c424f2)

Replaced `_update_strategy_doc()` with `_store_observation()`. Daily findings now go to `orchestrator_observations` DB table. Strategy doc no longer appended nightly.

### 5. `src/orchestrator/orchestrator.py` — _gather_context() — FIXED

All missing context has been added:
- ~~Active paper test status~~ — FIXED (commit 7c424f2)
- ~~Signal drought detection~~ — FIXED (commit 87d93bc)
- ~~Per-version performance breakdown~~ — FIXED (commit 7c424f2)
- ~~Last N daily observations~~ — FIXED (commit 7c424f2)
- ~~Historical data bootstrap~~ — FIXED (Session 9, `_bootstrap_historical_data()`)

### 6. `strategy/strategy_document.md` — Content

**Problem:** Contains pre-loaded "wisdom" that wasn't earned through experience.

Sections that are fine (factual):
- §3 Active Strategy Summary — describes v001, factual
- §4 Performance History — empty template, fine

Sections that violate "earned not pre-loaded":
- §2 Core Principles — "Never fight the trend," "When in doubt, stay out" are generic trading advice, not lessons this system learned. Should be empty or minimal on day 1.
- §5 Risk Observations — "Low-volatility ranging markets will generate many false crossover signals" is theory, not experience. Let the system discover this.
- §6 Adaptation Plan — "Consider adding: MACD confirmation, ATR-based stops" is directive. The orchestrator should decide what to try.
- §7 Market Condition Playbook — Entirely theoretical. Should be built from experience.

**Action:** Strip to factual minimum. §1 empty. §2 only the fee fact (this IS ground truth, not theory). §3 v001 description. §4-7 empty, to be filled by earned experience.

### ~~7. `src/shell/database.py` — Missing tables~~ FIXED (commit c9ae53e)

Added `orchestrator_observations` table with index.

### ~~8. `statistics/active/trade_performance.py` — Missing version breakdown~~ FIXED (commit 7c424f2)

Added `by_version` section with full metrics (trades, wins, win_rate, net_pnl, fees, expectancy, first/last trade dates) grouped by `strategy_version`.

### ~~9. `src/main.py` — Missing historical data bootstrap~~ FIXED (Session 9)

`_bootstrap_historical_data()` in `src/main.py` — paginates Kraken OHLC API (720 candles/request, rate-limited 1/sec) to fetch ~30 days of 5m data per symbol on startup.

### ~~10. `src/orchestrator/orchestrator.py` — _run_backtest()~~ FIXED (commit c9ae53e)

Changed from `get_candles(symbol, "1h", limit=720)` to `get_candles(symbol, "5m", limit=8640)`.

### 11. Rough/Unpolished Items (fix before VPS deployment)

| # | Item | File(s) | Severity | Status |
|---|------|---------|----------|--------|
| 1 | ~~WebSocket silent failure at max retries~~ | `src/shell/kraken.py` | Medium | **FIXED** — `set_on_failure()` callback, Telegram alert |
| 2 | ~~strategy_state table bloat~~ | `src/main.py` | Low | **FIXED** — Prune to last 10 on write |
| 3 | Reporter lacks statistical rigor | `src/orchestrator/reporter.py` | Low | Acceptable — truth benchmarks + analysis modules handle real stats |
| 4 | Position monitor 30s gap | `src/main.py` | Medium | Acceptable for paper. Live mode needs server-side stops. |
| 5 | ~~Import path fragility~~ | `src/main.py` | Low | **FIXED** — Top-level import |
| 6 | ~~Token budget not checked at cycle level~~ | `src/orchestrator/orchestrator.py` | Low | **FIXED** — Check at cycle start (5000 token minimum) |
| 7 | Orchestrator broad Exception handling | `src/orchestrator/orchestrator.py` | Low | Acceptable — logs the error, returns report string |
| 8 | ~~Data store aggregation untested~~ | `src/shell/data_store.py` | Medium | **FIXED** — Integration test added |
| 9 | PID lockfile stale after kill -9 | `src/main.py` | Low | Already handled (checks if PID alive) |
| 10 | brain.db-wal/shm cleanup after crash | Manual | Low | Document in deployment guide |
| 11 | Paper test entries never checked/completed | `src/orchestrator/orchestrator.py` | High | Addressed by paper test awareness |
| 12 | Laptop sleep assumptions | Various | N/A | Resolved — system targets VPS |

---

## Focused Prompt Audit — Framework Alignment (Session 8, 2026-02-08)

Detailed line-by-line audit of all 5 orchestrator prompts + the `_analyze()` user prompt against the approved three-layer framework.

### Audit Results by Prompt

#### `ANALYSIS_SYSTEM` (lines 46-125) — HEAVILY MISALIGNED

**What's good (Layer 2 — System Understanding):**
- "Your Inputs" section (lines 48-65): Factual, labeled, describes the five input categories. Keep.
- "Cross-referencing" section (lines 89-96): Describes how the system works. Keep.
- "Decision Options" section (lines 98-107): Describes available tools. Keep.

**What's missing (Layer 1 — Identity):**
- No identity layer exists at all. No sense of WHO the orchestrator is — no character, no professional identity, no mental models. Just a single sentence: "You are the AI orchestrator for a crypto trading system."

**What violates the framework (Directives — should not exist):**
- Lines 67-87: Entire "Your Goals" section — numeric targets, behavioral instructions, meta-goals. ALL directives.
  - "Achieve positive expectancy after fees" — optimization directive
  - "Profit factor > 1.2" / "avg win/loss > 2.0" — numeric threshold directives
  - "Be conservative" — behavioral directive
  - "Prefer NO_CHANGE when data is insufficient" — preference directive
  - "Fewer trades, bigger moves" — strategy directive
  - All meta-goals are behavioral directives
- Lines 109-114: Entire "Decision Guidelines" section — all heuristics.
  - "Minimum ~20 trades before judging" — threshold directive
  - "If you lack information, update analysis modules first" — decision heuristic
  - "Analysis module updates are low-risk — prefer them when unsure" — preference directive
  - "Don't chase short-term noise" — behavioral directive
- Line 124: `strategy_doc_update` in JSON response — forces nightly doc appends

**Decision: Goals → Fund Mandate**
The goals were originally added because the user said "it should probably have clear goals" (Session 4). This remains true — even in the "maximize awareness, minimize direction" framework, a fund manager operates under a mandate from the investor. The investor sets return expectations; the manager decides how to achieve them.

However, the current goals section mixes three different things:
1. **Fund mandate** (investor expectations) — legitimate, keep but reframe
2. **Behavioral directives** (how to operate) — violates framework, remove
3. **Numeric thresholds** (specific targets) — need review as part of mandate design

**Decision:** Scrap the existing goals. Develop a specific fund mandate — the investor's expectations framed as awareness, not instructions. This is an open design task (see below).

#### `_analyze()` user prompt (lines 426-494) — MINOR MISALIGNMENT

**What's good:** Data presentation is well-structured with labeled sections.

**What violates the framework:**
- Lines 481-488: USER CONSTRAINTS section has interpretive commentary mixed with facts:
  - "minimum ~2% move to profit" — interpretation, let the orchestrator derive this
  - "bigger conviction bets" — editorial, just state the number
  - "the real safety net" — editorial, just state the limit
  - Raw numbers are facts. Interpretations are directives in disguise.
- Lines 492-493: "Cross-reference your market analysis against trade performance" — explicit instruction. The orchestrator should do this naturally based on identity.

#### `CODE_GEN_SYSTEM` (lines 127-153) — NO FRAMEWORK ISSUES

Technical spec for Sonnet code generation. Constraints ("must inherit StrategyBase," "must not import os") are system facts. Appropriate for its purpose.

**Note:** Has documented implementation improvements (diff + change purpose for context, targeted edits) — these are about what data flows to the prompt, not the prompt's design.

#### `CODE_REVIEW_SYSTEM` (lines 155-170) — NO FRAMEWORK ISSUES

Technical review criteria. System requirements, not behavioral directives.

**Note:** Documented improvement — should receive diff + change purpose for scope verification (audit item #3). This is a context issue, not a framework violation.

#### `ANALYSIS_CODE_GEN_SYSTEM` (lines 172-201) — NO FRAMEWORK ISSUES

Technical spec. Same as CODE_GEN — appropriate constraints.

#### `ANALYSIS_REVIEW_SYSTEM` (lines 203-239) — NO FRAMEWORK ISSUES

Mathematical correctness criteria. Factual review standards.

### Layer Classification Correction

Previous audit incorrectly placed "self-awareness of architecture" under Layer 1 (Identity). Corrected:

- **Layer 1 (Identity):** WHO the orchestrator is — character, mental models, professional identity, statistical intuition, risk philosophy, intellectual honesty. Does NOT include system architecture knowledge.
- **Layer 2 (System Understanding):** HOW the system works — what decisions do what, how decisions impact past decisions (e.g., deploying a new strategy cancels an active paper test), system mechanics and consequences, available tools, data landscape, constraints.

The key distinction: Layer 1 is about the orchestrator's *character*. Layer 2 is about the *world it operates in*.

### Implementation Decision: Separate Prompt Strings

Layer 1 (Identity) and Layer 2 (System Understanding) will be separate prompt strings in the code, concatenated for the system prompt. This keeps the framework visible in the code without impacting functionality.

### Strategy Document (Layer 3) — Confirmed Misaligned

`strategy/strategy_document.md` contains pre-loaded generic trading wisdom that wasn't earned (audit item #6). Per Layer 3 definition: institutional memory is EARNED knowledge. On day 1, it should be nearly empty. Sections §2 (Core Principles), §5 (Risk Observations), §6 (Adaptation Plan), §7 (Market Condition Playbook) all contain theory, not experience. Must be stripped.

### Summary of Prompt Changes Needed

| Prompt | Framework Issue? | Action |
|--------|-----------------|--------|
| `ANALYSIS_SYSTEM` | Yes — heavily | Rewrite from scratch: add Layer 1, keep good Layer 2 parts, remove all directives, replace goals with fund mandate |
| `_analyze()` user prompt | Yes — minor | Remove interpretive commentary from USER CONSTRAINTS, remove cross-reference instruction |
| `CODE_GEN_SYSTEM` | No | Implementation improvements only (diff, targeted edits) |
| `CODE_REVIEW_SYSTEM` | No | Implementation improvements only (diff + change purpose) |
| `ANALYSIS_CODE_GEN_SYSTEM` | No | None |
| `ANALYSIS_REVIEW_SYSTEM` | No | None |
| `strategy_document.md` | Yes — Layer 3 | Strip pre-loaded wisdom, keep factual minimum |

### Fund Mandate — DECIDED

The existing goals section is scrapped and replaced with a fund mandate — the investor's expectations, framed as awareness.

**The mandate:** *Portfolio growth with capital preservation. Avoid major drawdowns. This is a long-term fund.*

Design principles:
- Simple and method-agnostic — doesn't prescribe trading style, frequency, or approach
- No specific return targets — we don't know what's achievable yet, and targets risk excessive risk-taking
- No specific drawdown numbers in the mandate — the hard limits already exist in the shell and are communicated as system facts
- The orchestrator figures out the "how" (including fee economics, trade selectivity) from system understanding and identity — not from the mandate
- The mandate is the investor's voice — "here's what I expect." The manager decides how.

The mandate lives in the system prompt alongside Layer 1 and Layer 2, clearly labeled as the fund mandate.

### Layer 2 Design Principle: Full External Awareness

**User directive:** "Make it fully aware of ALL constraints and processes outside of its own thinking."

Layer 2 (System Understanding) must cover not just data and numbers, but the full mechanical picture:
- **Risk limit mechanics**: The risk manager silently clamps oversized trade requests. Hitting 6% daily loss halts trading for the day. Hitting 12% drawdown halts the system entirely. These aren't just numbers — they're system behaviors with consequences.
- **Paper test mechanics**: Deploying a new strategy terminates any active paper test and its data becomes incomplete.
- **Scan loop mechanics**: How often data is collected, what triggers signals, how position monitoring works.
- **Strategy deployment pipeline**: What happens step by step when a strategy change is made (backtest → paper test → deploy).
- **Data landscape**: What historical data exists, what the backtester can do, what each analysis module measures.

The principle: the orchestrator should understand the *system it lives in* well enough to predict the consequences of its decisions before making them.

### Implementation Detail: Dynamic Config Values

Risk limits in the `_analyze()` user prompt are currently **hardcoded strings**:
```
- Max trade size: 7% of portfolio
- Max position size: 15% of portfolio
```

These MUST be pulled from `self._config.risk` dynamically, not hardcoded. If the user changes a limit in the config, the orchestrator should see the updated value on the next cycle automatically. The token budget line already does this correctly — risk limits should follow the same pattern.

### Layer 1: Identity — DESIGNED

The orchestrator's permanent character. Describes WHO it is — how it thinks, not what to do. Does not change over time. Honesty is the foundation; everything else builds on it.

**1. Radical Honesty (Foundation)**
The bedrock trait. Guards against the orchestrator's own capacity to rationalize (LLMs are excellent at constructing plausible narratives for any outcome).
- **Honest with itself**: Doesn't rationalize decisions or confirm its own biases. If a change didn't help, admits it. If a thesis isn't supported by data, abandons the thesis.
- **Honest with the data**: Doesn't cherry-pick, doesn't find patterns that aren't there, doesn't ignore inconvenient results. Acknowledges sample size limitations rather than drawing conclusions anyway.
- **Honest about results**: A loss is a loss. Doesn't frame failures as "unusual market conditions" unless the data actually supports that. Doesn't attribute wins to skill when they might be luck.

**2. Professional Character**
A thoughtful, experienced fund manager — not a day-trader chasing signals, not a rigid algorithm following rules. Someone who has internalized the realities of markets through experience.

**3. Relationship to Uncertainty**
Comfortable saying "I don't have enough information yet." Doesn't force conclusions from thin data. But also doesn't paralyze — knows the difference between "I need more data" and "I'm avoiding a decision."

**4. Probabilistic Thinking**
Thinks in distributions, not individual outcomes. A losing trade doesn't mean the strategy is wrong. A winning trade doesn't mean it's right. What matters is whether the system has an edge over many trades.

**5. Relationship to Change**
Understands that every modification resets the evaluation clock — new strategy means new data needed. But also knows that persisting with something broken has a cost too. Change isn't good or bad; it's a tool with a price.

**6. Long-Term Orientation**
Thinks in terms of compounding — both returns and knowledge. Individual cycles are data points, not verdicts. The fund's trajectory over months matters more than any single night's decision.

**What's deliberately NOT in the identity:**
- Fee economics, trade frequency, conservatism — these are conclusions the orchestrator arrives at from Layer 2 (sees fee structure) and Layer 3 (learns what works)
- Specific numbers or thresholds — these are system facts, not character
- Preferences or priorities — these are directives

### Layer 2: System Understanding — MAP (design only, content written after implementation)

> **NOTE:** This is a structural map of what Layer 2 must cover. The actual prompt content will be written AFTER pending implementation changes are complete, so it describes the system as it actually is. See "Post-Implementation To-Do" at the bottom.

#### What the Orchestrator CAN Do (its decisions and their consequences)

**Strategy decisions** — each triggers a pipeline and has downstream effects:

| Decision | Pipeline | Paper Test | Key Consequence |
|----------|----------|------------|-----------------|
| NO_CHANGE | None | — | Status quo. Active paper tests continue. Data keeps accumulating. |
| STRATEGY_TWEAK (tier 1) | Sonnet generates → Opus reviews → sandbox → backtest → deploy | 1 day | New version deployed. Active paper test on previous version TERMINATES (data incomplete). Evaluation clock resets. All new trades tagged with new version. |
| STRATEGY_RESTRUCTURE (tier 2) | Same pipeline | 2 days | Same consequences, bigger scope. |
| STRATEGY_OVERHAUL (tier 3) | Same pipeline | 1 week | Same consequences, fundamental change. |

**Analysis module decisions** — lower risk, no paper testing:

| Decision | Pipeline | Key Consequence |
|----------|----------|-----------------|
| MARKET_ANALYSIS_UPDATE | Sonnet generates → Opus reviews (math focus) → sandbox → deploy | Immediate effect. Changes what market data the orchestrator sees next cycle. No paper test needed (read-only). |
| TRADE_ANALYSIS_UPDATE | Same pipeline | Changes what performance data the orchestrator sees next cycle. |

**Critical system consequence the orchestrator must understand:**
Deploying a new strategy while a paper test is active terminates that test. The data from the interrupted test is incomplete and cannot be used to evaluate the previous version. This means rapid strategy changes destroy the ability to evaluate whether previous changes helped.

#### What the Orchestrator CANNOT Do (shell-enforced boundaries)

These are hard constraints enforced by the rigid shell. The orchestrator cannot bypass, modify, or override them:

- **Risk limits**: Max trade size, max position size, daily loss halt, max drawdown halt, max positions. Enforced by risk manager on every trade. The risk manager silently CLAMPS oversized requests — if the strategy requests 10% and the limit is 7%, the trade executes at 7%.
- **Daily loss halt**: If cumulative daily losses hit the limit, the system STOPS TRADING for the rest of the day. No signals are acted on.
- **Max drawdown halt**: If portfolio drops below the threshold from peak, the system HALTS ENTIRELY. This is the hard safety net.
- **Truth benchmarks**: 17 metrics computed from raw DB data. The orchestrator cannot modify these calculations. They exist specifically so the orchestrator can verify its own analysis modules against reality.
- **Fee structure**: Determined by the exchange (Kraken). Currently 0.25% maker / 0.40% taker at the $0 volume tier. Changes only when trading volume crosses tier thresholds.
- **Scan loop frequency**: Fixed at 5-minute intervals.
- **Position monitor frequency**: Fixed at 30-second intervals.
- **Data retention policy**: 5m candles kept 30 days, 1h kept 1 year, daily kept 7 years.
- **Sandbox**: All strategy and analysis module code must pass safety validation before deployment. No filesystem, network, or dangerous imports.
- **Code review**: All generated code is reviewed by Opus before deployment. Cannot be skipped.
- **Backtest gate**: Strategy changes must pass backtesting before deployment.

#### External Processes Running Independently

The orchestrator runs nightly, but these processes run continuously without its involvement:

- **Scan loop (every 5 min)**: Collects market data from Kraken, computes indicators (EMA, RSI, volume ratio), runs the active strategy's `analyze()` method, stores scan results, acts on any signals that pass risk checks.
- **Position monitor (every 30 sec)**: Checks open positions against stop-loss and take-profit levels. Closes positions that hit either threshold.
- **Data store maintenance (nightly, after orchestrator)**: Aggregates 5m candles → 1h, 1h → 1d. Prunes data beyond retention windows.
- **Paper trading simulation**: All trades execute in paper mode with simulated slippage (0.05%) and real fee calculations. Tracks positions, P&L, and portfolio value.

#### Data the Orchestrator Receives

**Currently implemented:**
- Ground truth benchmarks (17 metrics — trade counts, win rate, P&L, fees, expectancy, drawdown, signal/scan activity, strategy version)
- Market analysis module output (price summary, indicator distributions, signal proximity, data quality)
- Trade performance module output (performance by symbol/regime, signal analysis, fee impact, holding duration, rolling metrics)
- Strategy code, strategy document, version history
- Recent trades (last 50)
- Daily performance snapshots (last 7 days)
- Performance summary (last 7 days)
- Token usage (daily)

**Also implemented (Session 8-9):**
- Paper test status (active test, version, consequence of deploying now)
- Signal drought detection (last signal time, 7d/30d counts, 24h scans)
- Per-version performance breakdown (trade_performance by_version section)
- Recent daily observations (from orchestrator_observations table, last 7-14 days)
- Historical data bootstrap (30 days of 5m candles on first startup)

#### Response Format — To Be Updated During Prompt Writing

Current JSON response still includes `strategy_doc_update`. During prompt writing phase:
- Replace with observation fields (daily findings → `orchestrator_observations` table, rolling 30d)
- Add optional strategy doc flag for rare meaningful discoveries
- `_store_observation()` method already implemented (Session 8)

---

### Post-Implementation To-Do — CURRENT PHASE

1. [x] **System audit** — DONE (Session 10). 21 findings across 3 categories. See progress.md for table.
2. [x] **Fix all 21 audit findings** — DONE (Session 10). 16 fixes applied, 5 triaged as not actionable. 35/35 tests passing.
3. [x] ~~**Implement Action.SHORT**~~ — REMOVED. Kraken margin blocked for Canada. System is long-only. (Session 11)
4. [x] **Raise token budget safety net** — DONE (Session 11). 150K → 1.5M.
5. [x] **Implement slippage tolerance** — DONE (Session 11). Signal field + config default + paper/live usage.
6. [x] **Trading pairs + per-pair fees** — DONE (Session 11). 9 pairs, per-pair fee tracking, fees in IO contract.
7. [x] **Write Layer 1 prompt content** — DONE (Session 11). `LAYER_1_IDENTITY` constant: 6 identity dimensions.
8. [x] **Write Layer 2 prompt content** — DONE (Session 11). `LAYER_2_SYSTEM` constant: decisions/consequences, boundaries, processes, inputs, data landscape, response format.
9. [x] **Write fund mandate** — DONE (Session 11). `FUND_MANDATE` constant: "Portfolio growth with capital preservation. Avoid major drawdowns. This is a long-term fund."
10. [x] **Write response format** — DONE (Session 11). Removed `strategy_doc_update`, observations stored via existing `_store_observation()`.
11. [x] **Write `_analyze()` user prompt** — DONE (Session 11). Renamed to SYSTEM CONSTRAINTS, added long-only/slippage/pairs, dynamic config, no editorial.
12. [x] ~~Strip strategy_document.md~~ — DONE (commit c9ae53e)
13. [ ] **End-to-end review** — verify all three layers work together, no directive leakage

---

## System Audit — Session 10 (2026-02-09)

Full codebase audit before prompt writing phase. User directive: "let's look for oversights, for unpolished code, rough code, potential errors. let's think about foggy observability. let's think about graceful error handling. let's look at everything and make sure it's clean."

Three parallel audit agents scanned all source files. 21 findings organized into three categories.

### Category 1: Core Functionality — What's Actually Broken

**#1 — Backtester short P&L inverted** (`src/strategy/backtester.py:202`)
P&L always uses `(exit - entry) / entry`. For short positions, should be `(entry - exit) / entry`. All backtest metrics (expectancy, win rate, Sharpe) are wrong for short strategies. Orchestrator could deploy or reject strategies based on incorrect backtest data.

**#2 — Kraken API assumes non-empty dicts** (`src/shell/kraken.py:99,118`)
`get_ohlc()` and `get_ticker()` use `list(result.keys())[0]` with no bounds check. If Kraken returns empty result (API hiccup, pair delisted), `IndexError` crashes the entire scan cycle. No recovery, no alert.

**#3 — Portfolio cash = 0 in live mode** (`src/shell/portfolio.py:56-63`)
If `daily_performance` table is empty and mode is live, `_cash` stays at default 0.0. Paper mode falls back to `paper_balance_usd`; live mode has no fallback. All position sizing wrong from the start.

**#4 — JSON parsing from LLM fragile** (`src/orchestrator/orchestrator.py:553,673,807`)
Uses `rfind("}")` to find end of JSON in free-text LLM response. If LLM wraps JSON in code blocks or includes multiple JSON objects, grabs wrong brace. No validation of required keys. Missing `decision` key silently treated as `None`. Three call sites with same pattern (analysis, code review, analysis review).

**#5 — Data aggregation can lose candles** (`src/shell/data_store.py:130-140`)
Aggregation DELETEs 5m candles per-symbol, then INSERTs 1h aggregates. If INSERT fails mid-batch (e.g., disk error), 5m candles already gone. Single commit at end means partial failure = data loss.

**#6 — Daily start value resets on restart** (`src/shell/portfolio.py:65`)
`_daily_start_value` set to current portfolio value on init, not actual day-start. Mid-day restart → daily P&L calculated from restart point → risk manager's daily loss limit enforced against wrong baseline.

### Category 2: Graceful Autonomy — Silent Failures & Observability Gaps

**#7 — Scan loop failures silent** (`src/main.py:455-457`)
Exception is logged but no Telegram alert sent. APScheduler won't retry; next scan is 5 minutes away. System appears running while doing nothing. User unaware.

**#8 — Telegram disabled silently** (`src/telegram/bot.py:24-26`)
If bot_token missing or `enabled=false`, system starts normally with zero observability. Emergency alerts never reach user. Only debug-level log line indicates Telegram is off.

**#9 — Notifier no retry** (`src/telegram/notifications.py:31-38`)
All notifications catch exceptions and discard message. Transient Telegram API failure = critical alert permanently lost. No queue, no retry, no fallback.

**#10 — WebSocket callback failures swallowed** (`src/shell/kraken.py:268-277`)
Callbacks wrapped in `except (json.JSONDecodeError, KeyError): continue`. If a registered callback throws, it's silently caught. Price update callbacks could stop working. No logging.

**#11 — AI client init failure continues** (`src/main.py:115-119`)
If API key wrong or Anthropic down, system starts. Orchestrator never works, strategy never evolves. Only a `warning` log, no alert.

**#12 — Emergency stop doesn't verify fills** (`src/main.py:554-572`)
Sends close signals for all positions but doesn't confirm execution. If one close fails (network error), continues to next. User thinks all positions closed.

**#13 — Position monitor stale prices on WS failure** (`src/main.py:459-472`)
REST fallback can fail per-symbol with bare `except: pass`. Stop-loss/take-profit checked against outdated prices. No alert when price data is stale.

**#14 — Fee check uses only first symbol** (`src/main.py:501-502`)
Assumes all symbols have same fee tier. If symbols have different fees, wrong fees applied to some trades.

### Category 3: Robustness & Cleanliness

**#15 — Intent enum crashes on bad DB data** (`src/shell/portfolio.py:104,125`)
`Intent[p.get("intent", "DAY")]` — if DB contains invalid string, `KeyError` crashes portfolio init. System can't restart until DB manually cleaned.

**#16 — Sandbox tmp_path not initialized** (`src/strategy/sandbox.py:143`, `src/statistics/sandbox.py:105`)
If exception occurs before `tmp_path = f.name`, finally block tries to unlink undefined variable. `NameError` masks real error.

**#17 — ReadOnlyDB regex multi-statement bypass** (`src/statistics/readonly_db.py:17-20`)
Only checks first statement. `"SELECT 1; DROP TABLE trades"` passes. Low practical risk (code is reviewed) but gap exists.

**#18 — Token budget race condition** (`src/orchestrator/ai_client.py:84-86`)
Check before call, deduction after. Two concurrent calls could both pass. Unlikely in current design but fragile.

**#19 — No config validation** (`src/shell/config.py:105-198`)
Invalid values load silently. `max_daily_loss_pct = 150%` accepted without warning.

**#20 — Backtester first-day inflation** (`src/strategy/backtester.py:243-247`)
`prev_day = None` means first candle triggers new day, inflating daily_values by one extra entry. Drawdown and Sharpe slightly off.

**#21 — Hardcoded slippage** (`src/shell/portfolio.py:176,277`)
Fixed at 0.05%, not configurable. Paper trading slippage doesn't reflect per-pair market reality.

---

## Design Discussion: Audit Triage → New Features (Session 10)

Five audit findings were initially triaged as "not actionable." User reviewed each and escalated four into design decisions.

### #1 → Action.SHORT
- **Initial triage**: "Backtester only supports longs, not a bug"
- **User**: "I want to add short support"
- **Decision**: Add `Action.SHORT` to contract enum. Explicit action, not inferred from context. Backtester tracks side, uses side-aware P&L. Portfolio tracker already handles shorts.

### #14 → Trading Pairs + Per-Pair Fees (see full thread below)
- **Initial triage**: "Fees are volume-tier based, not pair-specific"
- **User**: "For long-term autonomy, should we ensure correct fees for all pairs? Can the orchestrator add new pairs?"
- **Discussion**: Explored dynamic watchlist (orchestrator adds/removes pairs) vs static list. User concluded: static list is simpler, expand from 3 to 9 pairs, with 12-pair cap. Per-pair fee storage for future-proofing.
- **Key insight from user**: "active pairs is really just where active trades are" — the orchestrator doesn't need to manage the watchlist, just decide which pairs to trade from the available set.

### #18 → Token Budget as Safety Net
- **Initial triage**: "Race condition can't happen in sequential orchestrator"
- **User**: "Why do we have an enforced token budget? I'm not sure I want that."
- **Discussion**: Hard token limit contradicts "no hard gates" philosophy. Orchestrator should self-regulate via awareness (Layer 2), not be hard-blocked.
- **Decision**: Keep limit as safety net at ~10x expected (150K → 1.5M). Only catches runaway bugs. Orchestrator sees costs in context and self-regulates.

### #20 → Confirmed Not a Bug
- **Initial triage**: "First candle records starting cash, correct baseline"
- **User**: "This sounds like correct functionality"
- **Decision**: Closed. Not a bug.

### #21 → Orchestrator-Controlled Slippage
- **Initial triage**: "Cosmetic, only affects paper mode"
- **User**: "I want to allow the orchestrator to create its own slippage rules. Is that possible?"
- **Discussion**: Slippage isn't just simulation — it affects limit order placement, edge calculation, and is something the orchestrator can learn. Signal gets `slippage_tolerance` field, config has default, live mode uses it for limit order pricing.
- **Key user insight**: "Does this tie into limit buy orders where the orchestrator could allow for some tolerance?" — yes, slippage tolerance directly informs limit price = current_price * (1 + tolerance).

---

## Trading Pairs — Full Design Thread (Session 10)

### The Question
Original system: 3 fixed pairs (BTC/USD, ETH/USD, SOL/USD). Design says "agent can expand" but no mechanism exists.

### Options Explored
1. **Dynamic watchlist** — orchestrator discovers pairs via Kraken API, adds/removes via new `WATCHLIST_UPDATE` decision type. Shell manages WS subscriptions and data pipeline dynamically.
2. **Static list, expanded** — curate a good set of pairs in config. Orchestrator trades within this universe.

### Decision: Static List (Option 2)
User reasoning: "Maybe we don't dynamically change trading pairs. Active pairs is really just where active trades are." Dynamic management adds complexity for little benefit at current scale.

Constraints decided:
- **12-pair cap** (resource cost per pair: scans, WS, candle storage)
- **Static in config** — the orchestrator picks which to trade, not which to monitor
- **Per-pair fee tracking** — store and use correct fees per pair

### Final Pair List (9 pairs, room for 3 more)

All verified on Kraken API (2026-02-09):

| # | Our Name | REST Input | Response Key | WS Name | Rationale |
|---|----------|-----------|-------------|---------|-----------|
| 1 | BTC/USD | BTCUSD | XXBTZUSD | XBT/USD | Anchor, highest liquidity |
| 2 | ETH/USD | ETHUSD | XETHZUSD | ETH/USD | L1 leader, DeFi base |
| 3 | SOL/USD | SOLUSD | SOLUSD | SOL/USD | High-performance L1 |
| 4 | XRP/USD | XRPUSD | XXRPZUSD | XRP/USD | Top 5 market cap, very liquid |
| 5 | DOGE/USD | DOGEUSD | XDGUSD | XDG/USD | Extremely high volume |
| 6 | ADA/USD | ADAUSD | ADAUSD | ADA/USD | Top 10, consistent volume |
| 7 | LINK/USD | LINKUSD | LINKUSD | LINK/USD | DeFi infra, less L1-correlated |
| 8 | AVAX/USD | AVAXUSD | AVAXUSD | AVAX/USD | Growing L1 ecosystem |
| 9 | DOT/USD | DOTUSD | DOTUSD | DOT/USD | Polkadot ecosystem |

**Naming**: REST API accepts plain names (BTCUSD, DOGEUSD) — Kraken resolves them. Response keys use legacy format for older assets (XXBTZUSD, XETHZUSD, XXRPZUSD, XDGUSD). Our code handles this — grabs first non-`last` key from results. WS feed uses XBT for BTC and XDG for DOGE — PAIR_REVERSE map translates back.

### Fees in the IO Contract
User question: "Do per-pair fees affect how the strategy needs to be written?"

**Current gap**: Strategy receives `SymbolData` + `Portfolio` + `RiskLimits` — no fee information. Generates signals blind to cost. Shell applies fees after the fact.

**Fix**: Add `maker_fee_pct` and `taker_fee_pct` to `SymbolData`. Strategy can then:
- Calculate per-pair break-even: only signal when expected move > N * round_trip
- Choose order type: limit (cheaper) vs market (faster)
- Factor total cost into signal confidence
- Adapt dynamically if fees change (volume tier upgrade, policy change)

### Per-Pair Fee System Design
- `fee_schedule` table gets `symbol` column
- `_check_fees()` iterates all pairs, stores per-pair rates
- Trade execution looks up pair-specific fee
- Config retains default fees as fallback before first check
- Scan loop populates `SymbolData.maker_fee_pct` / `taker_fee_pct` from stored per-pair data

---

## Pre-Prompt Features — Implemented (Session 11)

All four features implemented. Affect what the orchestrator can do → required before writing Layer 2 prompts.

### 1. Action.SHORT — REMOVED (Hard Limitation)
- **Initially implemented**: SHORT enum, backtester side-aware P&L, inverted SL/TP, margin-model cash accounting.
- **Hard limitation found**: Kraken margin trading is blocked for Canadian residents. From Kraken support (Dec 2025): "Margin trading services are available to most verified clients that reside outside of the United States, United Kingdom, and Canada." Short selling requires margin. No workaround.
- **Reverted**: Action.SHORT removed from contract enum. Backtester reverted to long-only. System is permanently long-only on Kraken.
- **Kraken margin reference** (for future): All 9 of our pairs support margin (BTC/ETH/SOL/XRP/DOGE up to 10x, ADA/LINK/AVAX/DOT up to 3x). Opening fee 0.01-0.05%, rollover fee same rate per 4 hours. But none of this is accessible from Canada.

### 2. Token Budget Safety Net — DONE
- `config.py` + `settings.toml`: `daily_token_limit` 150K → 1.5M
- Orchestrator self-regulates via Layer 2 awareness; limit only catches genuine bugs

### 3. Slippage Tolerance — DONE
- `contract.py`: `Signal.slippage_tolerance: Optional[float] = None`
- `config.py` + `settings.toml`: `default_slippage_pct = 0.0005`
- `portfolio.py`: `_get_slippage()` method — signal override > config fallback
- Paper mode: simulates slippage at configured rate
- Live mode: informs limit order price placement (buy limit = price * (1 + tolerance))

### 4. Trading Pairs + Per-Pair Fees — DONE
- `kraken.py`: PAIR_MAP expanded to 9 pairs + WS reverse mappings (XBT/USD → BTC/USD, XDG/USD → DOGE/USD)
- `config.py` + `settings.toml`: 9 symbols default
- `database.py`: `symbol` column on fee_schedule (with migration)
- `main.py`: `_pair_fees` dict cached per 24h, all fee usage sites use per-pair lookup with global fallback
- `contract.py`: `SymbolData.maker_fee_pct` / `taker_fee_pct` populated from per-pair cache
- `backtester.py`: Optional `per_pair_fees` dict, `_get_taker_fee()` per-symbol lookup

---

## Prompt Writing — Three-Layer Implementation (Session 11, continued)

### What Changed
Replaced the monolithic `ANALYSIS_SYSTEM` prompt with three separate constants following the approved framework:

**`LAYER_1_IDENTITY`** — WHO the orchestrator is:
- 6 identity dimensions: Radical Honesty, Professional Judgment, Comfort with Uncertainty, Probabilistic Thinking, Relationship to Change, Long-Term Orientation
- All written as identity statements, not directives
- Uses the exact approved phrasings from Sessions 7-8 (e.g., "You understand that stability compounds" not "Be conservative")

**`FUND_MANDATE`** — Investor expectations:
- "Portfolio growth with capital preservation. Avoid major drawdowns. This is a long-term fund."
- Brief, method-agnostic. The mandate IS deliberately directive — it's the investor's voice.

**`LAYER_2_SYSTEM`** — WHAT it works with:
- Architecture overview (rigid shell + flexible components)
- Decisions and consequences (strategy tiers, analysis modules, paper test termination)
- Shell-enforced boundaries (risk limits, long-only constraint, code pipeline)
- Independent processes (scan loop, position monitor, data maintenance)
- Input categories with trust levels (5 categories)
- Data landscape
- Response format (removed `strategy_doc_update`, observations stored via `_store_observation()`)

**`_analyze()` user prompt** — cleaned up:
- Renamed "USER CONSTRAINTS" to "SYSTEM CONSTRAINTS"
- Added: trading pairs list, long-only constraint, default slippage
- All values dynamic from config
- No editorial commentary
- Opening changed from directive ("Review...and decide") to factual ("Current fund state for nightly review")

### What Was Removed
- All behavioral directives: "Be conservative", "Prefer NO_CHANGE", "Fewer trades, bigger moves"
- All numeric thresholds: "Minimum ~20 trades", "Profit factor > 1.2", "3x round-trip cost"
- All decision heuristics: "If you lack information, update analysis modules first"
- All optimization instructions: "Achieve positive expectancy after fees"
- Cross-referencing instructions (orchestrator does this naturally from identity)
- `strategy_doc_update` from response JSON (dead field, observations already handled)

### Audit Against Framework
Post-implementation audit identified 3 potential concerns:
1. "stability compounds" in identity — this IS the approved identity statement from the framework design table
2. "Rapid strategy changes destroy..." in Layer 2 — this is a mechanical fact about the data pipeline
3. "Avoid major drawdowns" in mandate — the mandate IS the investor's directive by design

All three are intentional. Framework-aligned.

### Code Gen & Review Prompts — Also Updated
The `CODE_GEN_SYSTEM` and `CODE_REVIEW_SYSTEM` prompts were stale relative to contract changes. Updated:

**`CODE_GEN_SYSTEM`**:
- SymbolData description now includes `maker_fee_pct`, `taker_fee_pct`
- Portfolio description now includes `total_value`, `daily_pnl`, `total_pnl`, `fees_today`
- Signal description now includes `slippage_tolerance` (optional override)
- Added MUST NOT: "Generate SHORT signals — the system is long-only"

**`CODE_REVIEW_SYSTEM`**:
- Added check #6: "Long-only compliance — no SHORT signals"

**Code gen user prompts** (`_execute_change()`, both tier 1 and tier 2+):
- Injected dynamic `## System Constraints` block with: trading pairs, long-only, fees, slippage, trade sizes, max positions, SymbolData fee fields, Signal slippage field

**Analysis module prompts** — left as-is (they query DB directly, don't need trading config).

### Files Modified
- `src/orchestrator/orchestrator.py` — Replaced `ANALYSIS_SYSTEM` with 3 constants, updated `_analyze()` system prompt construction and user prompt, updated `CODE_GEN_SYSTEM` and `CODE_REVIEW_SYSTEM`, added dynamic constraints to code gen user prompts
- `tests/test_integration.py` — Updated `test_analysis_code_gen_prompts_exist` to import/check new constants

### Test Count: 35/35 passing

---

## Session 19 — Position System Audit & Redesign

### Context
After implementing the Telegram command redesign (/status, /health, /outlook, /ask), user asked: "are there any oversights with positions? does this align with our crypto hedge fund strategy?"

This triggered a deep audit of the position system that revealed several significant gaps.

### Position System Audit Findings

**1. No position modification (critical)**
- Strategy can't adjust SL/TP without closing and reopening — paying double fees + slippage
- A fund manager trailing stops should be a zero-cost operation
- Decision: Add `Action.MODIFY`

**2. Intent is decorative (misleading)**
- DAY/SWING/POSITION exist as enums but have ZERO mechanical effect
- decisions.md line 24 says "Different exit logic per intent" but this was never implemented
- DAY trades don't auto-close, POSITION trades get same 30s monitoring as scalps
- Decision: Keep as informational labels (enforcing is directive), but tell orchestrator truth

**3. Orchestrator awareness gap (violates core philosophy)**
- Layer 2 doesn't mention: can't modify positions, no trailing stops, intent is decorative, averaging-in behavior
- The orchestrator could write strategies that try to trail stops by generating new BUY signals with updated SL, not knowing it's paying double fees for what should be free
- Decision: After all changes, comprehensive Layer 2 rewrite. Going forward, every system change must include awareness update.

**4. One position per symbol (limiting)**
- UNIQUE constraint forces averaging in — can't have long-term BTC hold + short-term BTC trade
- User: "I don't like this constraint, let's get rid of it"
- Decision: Tags system for multi-position per symbol

**5. Risk limits too tight / too directive**
- 12% max drawdown on crypto is very aggressive — BTC alone has 20%+ drawdowns in bull markets
- User philosophy: "trust the aligned agent" — identity + awareness should replace hard rules
- Claude response shared by user: suggests volatility-based stops, ATR, tiered approaches
- Decision: Widen to 40% max drawdown. Future: orchestrator-writable risk profile.

**6. Client-side SL/TP monitoring (fragile at scale)**
- 30-second polling means server downtime = unprotected positions
- Kraken supports native stop-loss, take-profit, trailing-stop order types
- User: "even at small scale I want this, best option for scalability and long-term stability"
- Decision: Exchange-native orders. Paper mode continues with price simulation.

**7. No capital injection tracking**
- Depositing cash looks like trading profit
- Decision: capital_events table for deposits/withdrawals

### User Philosophy Reinforcement
- "Let's prioritize longevity in very long terms — giving it the ability to gracefully scale WITHOUT intervention"
- "We need to maximize awareness. After changes we make to this system, we need to ensure that it is MAXIMALLY AWARE of EVERYTHING"
- "I want to lean towards trusting the aligned agent. How can we minimize hard gates?"
- User thinks about scaling: $1K → $10K → $100K from trading AND cash injections

### Implementation Sequencing (agreed)
- **Session A**: Action.MODIFY + multi-position tags + capital_events + remove UNIQUE — contract and portfolio changes
- **Session B**: Exchange-native orders on Kraken + position monitor rewrite
- **Session C**: Risk profile system + widen hard limits + full Layer 2 rewrite

### What the Orchestrator Needs to Know (after all changes)
Full awareness update required:
- MODIFY action exists and is free (no fees/slippage)
- Tags enable multi-position per symbol
- Intent is informational only — strategy manages its own exit logic
- Exchange-native SL/TP orders (survive server downtime, execute at exchange speed)
- Risk limits are emergency backstops, not operational constraints
- One conditional close per entry (no native OCO — system manages cancel-other)
- Paper mode simulates everything; live mode places real orders

---

## Session S — Bootstrap Backfill + Orchestrator Loop Redesign

### Problem Statement
After the first live orchestration cycle (Session R), two structural issues:

1. **Shallow data**: Bootstrap thresholds were too low — 1h skipped at 200 candles (~8 days), 1d at 30 candles (~1 month). The backtester only had ~30 days to work with. Need deeper data for more market regime coverage.

2. **Flat retry loop**: When Opus rejected backtest results, the rejection text was appended to Sonnet's prompt and the same flat loop continued. Opus never re-analyzed — just accumulated error text in Sonnet's context. This doesn't match the "hedge fund manager directing a developer" mental model.

### Design Discussion
- User wants Opus to be the **central decision-maker** — it directs Sonnet, evaluates results, and either deploys or provides fresh strategic direction.
- Key insight: code quality failures (sandbox, review) are different from strategic failures (bad backtest results). Mixing them in one loop muddies responsibilities.
- Solution: **nested loops** — inner handles code quality (Sonnet iterates), outer handles strategy direction (Opus redirects).
- Opus's `revision_instructions` **replace** the changes (fresh direction), not append. This prevents prompt bloat and gives Sonnet a clean starting point.
- `attempt_history` shows Opus what's been tried so it can meaningfully redirect rather than repeating the same approach.

### Key Design Choices
- Inner loop exhaustion returns immediately (code quality failure is terminal for that cycle)
- Backtest crash feeds into outer loop (Opus can redirect around the crash)
- `original_changes` preserved so Opus's revision always references the original goal
- Bootstrap thresholds aligned with retention windows and backtester request sizes
