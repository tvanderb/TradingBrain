# Key Decisions Log

## Architecture: IO-Container (not Three-Brain)
- **v1 (scrapped)**: Three-brain architecture (Executive/Analyst/Executor) with parameter tuning
- **v2 (current)**: IO-Container with hot-swapping strategy module and AI orchestration
- **Why the pivot**: User wanted true autonomous code evolution, not just parameter tuning. The three-brain design was too complex and the strategy logic was spread across many files. New design: simple strategy in one file, rigid shell protects the system, AI agent rewrites the strategy.
- User's key insight: "the inner core is changeable by the agent so as to continuously refine trading strategies autonomously"

## Exchange: Kraken (not Alpaca, not DEX)
- **Why not Alpaca**: Not available to Canadian residents
- **Why not DEX**: Gas fees destroy sub-$1K accounts (Ethereum $2-50/trade, BSC $0.10-0.50)
- **Why Kraken**: Canadian-friendly, good API (REST + WebSocket v2), user already has account
- **Actual fees**: 0.25% maker / 0.40% taker at $0 volume tier (higher than published 0.16/0.26)
- Round-trip cost 0.65-0.80% — must factor into all strategy decisions

## Markets: Crypto Only, Agent Expands
- Start with BTC/USD, ETH/USD, SOL/USD
- Agent has full access to Kraken pairs — can add/remove symbols as it sees fit
- 24/7 trading, no PDT concerns

## Trading Style: Full Spectrum (Day/Swing/Hold)
- Operate like a mini crypto hedge fund
- Strategy Module decides timeframe per trade via `Intent` tag (DAY/SWING/POSITION)
- Different exit logic per intent
- Shell tracks intent metadata on positions

## AI Models: Sonnet Generates, Opus Reviews
- **Sonnet**: Writes strategy code (cheaper, good at code generation)
- **Opus**: Reviews code for correctness, IO contract compliance, risk classification
- Max 2-3 revision cycles before aborting a change
- Opus also validates the agent's self-assessed risk tier for paper testing

## AI Provider: Anthropic or Google Vertex
- User has $300 Google Vertex credit
- Config flag switches between providers: `provider = "vertex"` or `"anthropic"`
- SDK supports both via `AsyncAnthropic` / `AsyncAnthropicVertex`
- Same message format, clean swap

## Token Budget: 150% of Base Estimate
- Base estimate: $15-30/month
- Budgeted at 150%: **$22-45/month**
- This provides headroom, not encouragement to spend more
- $300 credit covers 7-14 months at this rate

## Risk Limits: Emergency Backstops, User-Only
- Widened to emergency-only levels (Session B/D5): max_trade 10%, max_position 25%, max_drawdown 40%, rollback 15%
- Default 3% per trade
- Max positions: 5
- Agent CANNOT modify these. Shell enforces as safety net on all signals.
- Philosophy: trust the aligned agent to self-regulate; hard limits are seatbelts, not driving instructions.

## Autonomy: Fully Autonomous + Observability
- No human approval gate for strategy changes
- All changes reported via Telegram
- Notifications: trade alerts, daily P&L, weekly summary, strategy change reports, rollback alerts
- User observes but does not gate decisions

## Paper Trading: Custom Simulator (Kraken has no sandbox)
- Simulated fills against real market prices
- Realistic slippage (0.05%) and fee simulation
- Toggled via config: `mode = "paper"` vs `"live"`

## Strategy Safety: Three-Tier Paper Testing
| Tier | Scope | Duration |
|------|-------|----------|
| 1 (Tweak) | Parameters, thresholds | 1 day |
| 2 (Restructure) | Logic changes, new indicators | 2 days |
| 3 (Overhaul) | Fundamentally different approach | 1 week |
- Agent self-classifies tier, Opus validates
- Pipeline: backtest → paper test → deploy

## Strategy Failure: Automatic Rollback
- **Shell-enforced**: Risk limits in `config/risk_limits.toml` — daily loss halt, drawdown halt, crashes → immediate rollback. Values are dynamic (pulled from config at runtime).
- **Orchestrator-level**: No hardcoded thresholds. Per the fund mandate framework (Sessions 7-8), the orchestrator uses its own judgment based on identity + full awareness of system state. No numeric triggers — the aligned agent decides when to act.

## Data Retention: 7 Years, Tiered
- 5-min candles: 30 days → aggregate to 1-hour
- 1-hour candles: 1 year → aggregate to daily
- Daily candles: 7 years
- Nightly aggregation job
- Total storage: ~30MB after 7 years (trivial)

## Strategy Document: Quarterly Distillation
- Active document kept under ~2,000 words
- Every 4 quarters: distill key lessons into Core Principles, archive old detail
- Yearly archives available for deep reference but not loaded by default
- Prevents context snowball over years of operation

## Performance Criteria
- **Superseded by fund mandate** (Sessions 7-8): "Portfolio growth with capital preservation. Avoid major drawdowns. Long-term fund."
- No prioritized numeric targets — orchestrator determines what matters via identity + awareness
- All metrics tracked by truth benchmarks: expectancy, win rate, P&L, profit factor, drawdown, fees
- Orchestrator decides relative importance based on context, not hardcoded priority order

## Skills Library — REMOVED (Session K)
- ~~Agent builds reusable indicator functions in `strategy/skills/`~~
- **Removed**: Every function was a trivial wrapper around pandas/ta. First live orchestrator cycle failed 3/3 importing from `src.strategy.skills.indicators`.
- **Replacement**: Strategy imports `pandas`, `numpy`, `ta`, `scipy`, and stdlib modules directly. No intermediary wrappers needed.

## Pip Install: Dropped
- Pre-install comprehensive analysis libraries (ta, pandas, numpy, etc.)
- New library requests are human decisions

## Deployment: Local First, Linux VPS Later
- macOS development
- Systemd service on VPS with auto-restart
- Graceful shutdown preserves positions across restarts

## Initial Strategy: EMA + RSI + Volume (v001)
- Hand-written starting point for agent to iterate on
- EMA 9/21 crossover, RSI 14 filter, volume 1.2x confirmation
- Day trading intent, 2% stop-loss, 4% take-profit
- All 3 symbols from day one, agent can adjust

## Statistics Shell (Session 4, 2026-02-08)

### Decision: Statistics Module — Second Flexible Module
- **What**: A second flexible module alongside the strategy, following the same IO-container pattern
- **Why**: LLMs are bad at math. Giving the orchestrator pre-computed hard statistics prevents systematic miscalculation. Letting it design its own analysis report gives it flexibility to see what matters.
- **Architecture**: Same as strategy module — orchestrator rewrites, sandbox validates, Opus reviews
- **Key difference from strategy**: No paper testing needed, but code review must verify mathematical correctness

### Decision: Read-Only DB Access for Statistics Module
- **What**: Statistics module receives a read-only database connection instead of pre-loaded DataFrames
- **Why**: User raised efficiency concern — loading ALL data every night is wasteful and gets worse over time. Module should query exactly what it needs.
- **Tradeoff**: Module has I/O capability (read-only), harder to sandbox than pure computation. But read-only is safe for data integrity, and queries are reviewed by Opus.

### Decision: Truth Benchmarks — Rigid Shell Component
- **What**: Simple metrics (P&L, win rate, fees, drawdown) computed by rigid code orchestrator CANNOT modify
- **Why**: User insight — bad statistics are as dangerous as bad trades. If the statistics module has a bug, the orchestrator needs ground truth to compare against.
- **Design**: Trivially verifiable from raw data. If statistics module contradicts truth benchmarks, orchestrator knows its analysis is wrong.

### Decision: Orchestrator Self-Awareness
- **What**: All orchestrator inputs are explicitly labeled by category (ground truth / its analysis / its strategy / user constraints)
- **Why**: Orchestrator must understand what it can change vs what it should trust. Without this, it might try to "fix" truth benchmark numbers by changing the statistics module instead of fixing the strategy.

### Decision: Fund Mandate Replaces Explicit Goals (Sessions 7-8)
- **What**: Scrapped prioritized numeric goals. Replaced with a fund mandate: "Portfolio growth with capital preservation. Avoid major drawdowns. Long-term fund."
- **Why**: Per "maximize awareness, minimize direction" framework — numeric targets are directives. A well-informed agent with the right identity decides what to optimize. The mandate is the investor's voice; the manager decides how.
- **See**: discussions.md Sessions 7-8 for full framework

### Decision: Statistics Module Code Review — Mathematical Focus
- **What**: Opus code review for statistics module must verify mathematical correctness, not just code safety
- **Why**: User emphasized "we can't have miscalculations." A syntactically valid function that computes expectancy wrong is more dangerous than one that crashes.
- **Review prompt**: Must check formulas against standard definitions, verify edge cases (division by zero, empty data), confirm statistical validity

### Decision: Scan Results Collection
- **What**: New `scan_results` table storing indicator state every scan
- **Why**: Without scan history, statistics module can only analyze trades. Can't answer "how close were we to a signal?" or "what regime were we in when we traded?"
- **Data**: price, EMA values, RSI, volume ratio, regime, spread, whether signal generated
- **Impact**: ~864 rows/day (3 symbols × 288 scans), trivial for SQLite

### Decision: Two Analysis Modules (Not One)
- **What**: Separate market analysis and trade performance into two independent modules
- **Why**: Different analytical domains (exchange data vs execution quality), different value timelines (market analysis useful from day one, trade performance needs trades), independent evolution, fault isolation
- **Cross-referencing**: Modules run independently, neither sees the other's output. The orchestrator (Opus) receives both reports and cross-references them — correlating market conditions with trade outcomes. Modules compute hard numbers; AI reasons across them.
- **Why not sequential (market → trade performance)**: Coupling — if market analysis changes output keys or crashes, trade performance breaks. Keeping them independent maximizes fault isolation.
- **Infrastructure**: Shared loader, shared sandbox, shared ReadOnlyDB wrapper. Not double the infrastructure — same infrastructure applied twice.

### Decision: Regime is NOT Truth
- **What**: Raw indicator values (price, EMA, RSI, volume) are stored as truth in scan_results. Regime classification is stored as `strategy_regime` — what the strategy *thought*, not what the market *was*.
- **Why**: User caught this — regime is a heuristic interpretation, not ground truth. Different algorithms classify the same market differently. The analysis modules should derive their own regime views from raw indicators, potentially disagreeing with the strategy's classification.
- **Impact**: Renamed `regime` columns to `strategy_regime` to make the distinction explicit.

### Decision: Regime Tagging on Trades and Signals
- **What**: Add `strategy_regime` column to trades and signals tables
- **Why**: Records what the strategy believed the regime was at decision time — useful as a fact about the decision process, not a fact about the market. Analysis modules can compare this against their own regime assessment.

### Decision: No Short Selling — Kraken Canada Restriction (Sessions 10-11)
- **Original plan**: Add `Action.SHORT` to the contract enum for full directional flexibility.
- **Implemented (Session 11)**: SHORT added to enum and backtester with side-aware P&L, inverted SL/TP, margin-model cash accounting.
- **Hard limitation discovered**: Kraken margin trading is NOT available to Canadian residents. From Kraken support: "Margin trading services are available to most verified clients that reside outside of the United States, United Kingdom, and Canada." Short selling requires margin. No workaround exists.
- **Resolution**: Action.SHORT removed from contract and backtester reverted to long-only. System is long-only.
- **Implication for orchestrator**: Layer 2 should inform the orchestrator that the system is long-only due to exchange restrictions. No short selling, no leverage. This is a hard constraint, not a preference.
- **If exchange changes**: If Kraken enables margin for Canada, or if the system moves to another exchange, SHORT support would need to be re-implemented (backtester side-tracking, portfolio `_execute_short`, Kraken `leverage` API param, margin fee tracking, liquidation monitoring).

### Decision: Token Budget as High Safety Net (Session 10)
- **What**: Keep token budget but set very high (~10x expected: 1.5M tokens). Remove as a hard operational gate.
- **Why**: Hard token limit contradicts "no hard gates, trust aligned agent" philosophy. Orchestrator should self-regulate via awareness (Layer 2) not be prevented from acting. Safety net only catches genuine bugs (infinite loops).
- **Previous**: 150K daily limit with hard skip at cycle start.
- **Change**: Raise limit to ~1.5M. Orchestrator sees spend in context, self-regulates. `max_revisions` (3 attempts) naturally caps per-cycle spend. Race condition becomes irrelevant at this threshold.

### Decision: Orchestrator-Controlled Slippage (Session 10)
- **What**: Replace hardcoded 0.05% slippage with configurable default + per-signal override.
- **Why**: Slippage is a trading concept, not just simulation. Affects order placement strategy, edge calculation, and is something the orchestrator can learn about and optimize.
- **Design**:
  - `Signal` gets optional `slippage_tolerance` field (float, defaults to None → use config)
  - Config gets `default_slippage_pct` (float, default 0.0005)
  - Paper mode: shell simulates using signal value or config fallback
  - Live mode: informs limit order price placement (buy limit = price * (1 + tolerance))
  - Trade performance module can track actual vs expected slippage over time
- **Learning loop**: Orchestrator adjusts slippage expectations per pair/condition as it learns from real fills

### Decision: Static 9-Pair List with Per-Pair Fees (Session 10)
- **What**: Expand from 3 to 9 pairs (12-pair cap). Static in config, not dynamically managed. Per-pair fee tracking and fees in IO contract.
- **Why**: Dynamic watchlist adds complexity for little benefit. "Active pairs is really just where active trades are." The orchestrator picks which to trade from the available set; it doesn't need to manage the watchlist.
- **Pairs**: BTC/USD, ETH/USD, SOL/USD, XRP/USD, DOGE/USD, ADA/USD, LINK/USD, AVAX/USD, DOT/USD
- **Fees in IO contract**: `SymbolData` gets `maker_fee_pct` / `taker_fee_pct`. Strategy makes fee-aware decisions (break-even threshold, order type selection, confidence weighting).
- **Fee storage**: `fee_schedule` table gets `symbol` column. `_check_fees()` iterates all pairs. Per-pair storage future-proofs for fee structure changes.
- **Rejected**: Dynamic `WATCHLIST_UPDATE` decision type — too complex, unnecessary at this scale.

### Decision: Loosened Risk Limits for Learning Phase (Session 6)
- **What**: Widened risk limits to give system room for asymmetric payoff strategies during learning.
- **Why**: Fee wall (0.65-0.80% round-trip) naturally favors fewer, bigger trades. Over-constraining early prevents the system from finding its edge.
- **Risk limit changes**: max_trade_pct 5%→7%, max_position_pct 10%→15%, max_daily_loss 3%→6%, max_drawdown 10%→12%, consecutive_losses 10→disabled (999), rollback_daily_loss 5%→8%, default_trade_pct 2%→3%, default_take_profit 4%→6%.
- **Note**: The specific numeric *goals* from Session 6 (profit factor >1.2, avg_win/avg_loss >2.0) were subsequently scrapped in Sessions 7-8 in favor of the fund mandate. The risk *limits* above remain in config.

### Decision: Position System Redesign (Session 19)

**Context**: Audit of the position system revealed several gaps that constrain the orchestrator and don't align with the "maximize awareness, minimize direction" philosophy.

#### D1: Add Action.MODIFY
- **What**: New signal action that adjusts SL/TP/intent on an existing position without closing it.
- **Why**: Without MODIFY, the strategy must CLOSE + re-BUY to move a stop-loss, paying double fees and slippage. A fund manager trailing stops should be a zero-cost operation.
- **Behavior**: MODIFY allowed even when halted (same bypass as CLOSE). Must specify tag to target a specific position.

#### D2: Multi-Position Per Symbol (Tags)
- **What**: Remove UNIQUE constraint on positions.symbol. Add a `tag` field. Multiple positions per symbol, identified by tag.
- **Why**: The fund should be able to hold a long-term BTC core position AND take short-term BTC swings simultaneously. The one-position-per-symbol constraint forces averaging in and prevents multi-timeframe strategies.
- **Tag rules**:
  - BUY with new tag → new position
  - BUY with existing tag → average into that position
  - BUY with no tag → auto-generate tag (e.g., "btc_usd_1")
  - CLOSE/SELL with tag → close that specific position
  - CLOSE/SELL with no tag → close ALL positions for that symbol
  - MODIFY with tag → update that position's SL/TP/intent
  - MODIFY with no tag → error (must be explicit)
  - Tag unique per symbol (UNIQUE on (symbol, tag))
- **Risk aggregation**: max_position_pct sums across all tags for a symbol. max_positions counts total open positions.

#### D3: Intent Stays Informational (No Mechanical Enforcement)
- **What**: Keep DAY/SWING/POSITION as metadata labels. The shell does NOT enforce different behavior per intent.
- **Why**: Enforcing auto-close on DAY trades or wider stops on POSITION trades is directive — it's the shell telling the strategy what to do. The strategy manages its own exit logic. "Maximize awareness, minimize direction."
- **Value**: Intent provides useful analytics ("my DAY trades have X win rate"), appears in trade history, communicates strategy reasoning. The orchestrator must be told intent is informational only.

#### D4: Exchange-Native Orders on Kraken
- **What**: Place actual stop-loss and take-profit orders on Kraken instead of client-side price monitoring.
- **Why**: Client-side monitoring (every 30s) means if the server goes down, SL/TP don't execute. Exchange-native orders survive server downtime and execute at exchange speed. Critical for scaling and long-term reliability.
- **Implementation**: After entry fill, place separate SL and TP orders. Monitor their status. When one fills, cancel the other. On MODIFY, cancel old orders and place new ones.
- **Kraken support**: Spot API supports stop-loss, take-profit, trailing-stop, stop-loss-limit, take-profit-limit, trailing-stop-limit order types. No native OCO — we manage the cancel-other logic ourselves.
- **Paper mode**: Continue simulating SL/TP with price checking (no exchange orders).

#### D5: Risk Limits — Trust the Aligned Agent
- **What**: Widen hard limits to emergency-only backstops. Default max_drawdown from 12% to 40%.
- **Why**: 12% max drawdown halts the system during normal crypto volatility. A BTC drawdown of 20% in a bull market is routine. The orchestrator should self-regulate via strategy design, with hard limits only as emergency circuit breakers.
- **Future**: Orchestrator-writable risk profile (separate session). The orchestrator sets operating parameters within hard ceilings.

#### D6: Capital Events Table
- **What**: New `capital_events` table tracking deposits and withdrawals with timestamps and notes.
- **Why**: Without this, a $500 cash injection into a $200 portfolio looks like 250% returns. Return calculations need to know the correct capital base.
- **Fields**: id, type (deposit/withdrawal), amount, note, created_at.

#### D7: Order Fill Confirmation (Live Mode)
- **What**: Poll Kraken for actual fill prices instead of assuming market price.
- **Why**: The existing TODO ("actual fill may differ") becomes critical with exchange-native orders. We need real fill data for accurate P&L and to confirm SL/TP order execution.

### Decision: Remove Skills Library (Session K)
- **What**: Delete `strategy/skills/` directory entirely. Strategy imports pandas/numpy/ta/scipy directly.
- **Why**: First live orchestrator cycle (3:30 AM) failed — all 3 code generation attempts imported `from src.strategy.skills.indicators` which fails at runtime. Every function in skills was a trivial wrapper (e.g., `compute_rsi(series)` just called `ta.momentum.RSIIndicator(series).rsi()`). Wrappers add indirection without value.
- **Alternative considered**: Fix import paths so sandbox allows skills. Rejected because removing abstraction layer is simpler and more honest — the orchestrator should know the real tools it's using.
- **Added**: `scipy>=1.12` as dependency. Expanded LAYER_2_SYSTEM and CODE_GEN_SYSTEM prompts with full toolkit documentation (ta.trend, ta.momentum, ta.volatility, ta.volume, scipy.stats/signal/optimize examples).

### Decision: Restart Safety — Persistent Starting Capital (Session L, L1)
- **What**: Store `paper_starting_capital` in `system_meta` table on first boot. Always reconcile paper cash from first principles.
- **Why**: First live deployment showed $103.01 portfolio when it should be ~$99.91. The `daily_performance` table was empty (no snapshot yet), so `portfolio.initialize()` used `config.paper_balance_usd` as baseline — but positions already existed, making position costs appear as phantom profit.
- **Design**: `system_meta` key-value table (key TEXT PRIMARY KEY, value TEXT). Cash formula: `starting_capital + deposits + total_pnl - position_costs`. Runs unconditionally in paper mode — no snapshot-dependent path.
- **Config change safety**: If `paper_balance_usd` changes in config, system logs a warning but uses the DB value. Use `/deposit` or `/withdraw` (capital_events) to adjust capital.

### Decision: Restart Safety — Halt Evaluation on Startup (Session L, L2)
- **What**: `evaluate_halt_state()` checks all halt conditions (drawdown, consecutive losses, daily loss, rollback) after startup, before any trading begins.
- **Why**: Previously, halt state was only computed during trading. A crash during a drawdown would restart into active trading.

### Decision: Strategy Fallback Chain (Session L, L4)
- **What**: `load_strategy_with_fallback(db)` tries: filesystem → DB (`strategy_versions.code`) → return None (paused mode).
- **Why**: Filesystem-only loading means a corrupted or missing strategy file bricks the system. DB fallback recovers from the last deployed version. Paused mode keeps nightly orchestration running so the AI can fix the problem.
- **Design**: Orchestrator stores strategy source in `strategy_versions.code` on deploy. Paused mode disables scan loop + position monitor but keeps scheduler + orchestrator active.

### Decision: Extended Config Validation (Session L, L6)
- **What**: Validate timezone (`ZoneInfo`), symbol format (`/` + `USD`), trade size consistency (`default <= max_trade <= max_position`).
- **Why**: Invalid config values previously caused silent runtime failures. Fail-fast on startup is cheaper than debugging a running system.

### Decision: Nested Orchestrator Loops (Session S)
- **What**: Replaced flat retry loop in `_execute_change()` with nested inner (code quality) + outer (strategy direction) loops.
- **Why**: Flat loop treated all failures identically — sandbox errors, review rejections, and backtest rejections all just appended text to Sonnet's prompt. Opus never re-analyzed the strategic direction after seeing backtest results. The user's mental model: "Opus is the fund manager directing a developer" — it should evaluate results and redirect, not just accumulate error text.
- **Design**: Inner loop (max_revisions=3) handles Sonnet → sandbox → Opus code review. Outer loop (max_strategy_iterations=3) handles backtest → Opus reviews results → deploy or provide `revision_instructions`. Opus's revision instructions replace (not append to) the accumulated changes, giving Sonnet a fresh starting point each outer iteration.
- **Alternative considered**: Keep flat loop but have Opus re-analyze mid-loop. Rejected because separating code quality from strategic direction makes each loop's responsibility clear.

### Decision: Deeper Bootstrap Backfill (Session S)
- **What**: Raised skip thresholds: 5m 1000→8000, 1h 200→8000, 1d 30→2000 (with 2555d lookback).
- **Why**: First live backtest had only ~30 days of 1h data. Deeper data means more market regimes covered. One-time cost on startup (~3 min for 9 symbols).

### Decision: Candidate Strategy System — Replace Paper Tests (Session T)
- **What**: Replace paper-test-on-active-strategy model with a candidate strategy system. Up to 3 candidate strategies run in paper simulation alongside the active strategy. Opus decides lifecycle: create, evaluate, cancel, promote.
- **Why**: The paper test concept was fundamentally broken. When a strategy was deployed, it became the active trading strategy AND simultaneously entered a "paper test" — meaning real money was at risk while the strategy was supposedly being "tested." The test evaluated trades that were executed for real.
- **Design**: CandidateRunner (per-slot paper simulation engine) + CandidateManager (lifecycle management). Candidates mirror fund portfolio at creation time, trade with live market data using paper fills with slippage. Data segregation: separate tables (candidate_positions, candidate_trades). On promotion, Opus chooses "keep" (inherit positions) or "close_all" (clean slate).
- **Risk tiers removed**: No more TWEAK/RESTRUCTURE/OVERHAUL with hard-coded paper test durations. Opus chooses evaluation duration freely or leaves indefinite.
- **Alternative considered**: Fix paper tests by running them in a separate portfolio tracker. Rejected because the candidate system is conceptually cleaner — it separates "testing a strategy" from "trading with a strategy" entirely.

### Decision: Candidate Slot UNIQUE Constraint (Session T)
- **What**: `candidates` table has `UNIQUE(slot)` constraint. `INSERT OR REPLACE` overwrites slot rows.
- **Why**: Only one candidate per slot at a time. Historical candidate metadata (version, description) is overwritten, but the important historical data (trades, positions in `candidate_trades`/`candidate_positions`) is preserved in separate tables linked by `candidate_slot`.
- **Trade-off accepted**: We lose the `candidates` row history when a slot is reused. This is acceptable because the trade/position data is what matters for analysis.

### Decision: Institutional Learning System — Predictions + Reflection (Session W)
- **What**: The orchestrator makes falsifiable predictions during nightly decisions and periodically reflects on them, grading predictions and rewriting the strategy document.
- **Why**: Observation-and-summarize is journaling, not learning. True learning requires committing to falsifiable claims and then rigorously grading them against evidence. Without this, the orchestrator's judgment doesn't compound — it just journals.
- **Inspiration**: Ray Dalio's "Pain + Reflection = Progress" — principles created from experience, refined over time.
- **Design**: Predictions stored with claim/evidence/falsification/confidence/timeframe. Reflection cycle gathers 14 sections of evidence, Opus grades predictions by ID (avoiding fragile text matching), rewrites strategy doc, stores new predictions.
- **Key trade-off**: Full strategy doc rewrite each reflection (not append). Previous versions permanently archived. This ensures the document stays coherent rather than accumulating layers.
- **See**: `docs/dev_notes/strategy_document_design.md` for full design spec.

### Decision: Configurable Reflection Period (Session W)
- **What**: `orchestrator.reflection_interval_days` config option (default 7 days). Controls reflection trigger, observation window, and pruning.
- **Why**: Originally hardcoded at 14 days (bi-weekly). User wanted weekly reflections for faster learning during early operation. Made configurable to allow tuning based on experience.
- **Design**: Single config value propagates to all interval-dependent logic: `_should_reflect()` threshold, `_gather_reflection_context()` SQL windows (10 queries), `_gather_context()` observation window, `_store_observation()` pruning window, nightly prompt text.

### Decision: Manual Reflection Trigger — /reflect_tonight (Session W)
- **What**: Telegram command `/reflect_tonight` sets a `system_meta` flag. Orchestrator checks this flag before checking the time-based trigger. Flag is cleared after reflection runs.
- **Why**: The user (as fund investor) should be able to request an ad-hoc reflection — e.g., after a market event, a manual deposit, or just to test the system. The flag approach is simple and idempotent.
- **Alternative considered**: Immediate reflection on command. Rejected because reflection should happen within the orchestration window, not mid-trading-day.

### Decision: MAE Tracking on All Positions (Session W)
- **What**: Track max adverse excursion (worst drawdown from entry) on every position, both fund and candidate. Carried to trades table on close.
- **Why**: Tells the orchestrator how much pain a position experienced before reaching its outcome. A trade that made 5% but was down 15% at one point reveals different strategy characteristics than one that went straight up. Critical for reflection-quality feedback.
- **Design**: Updated on every price refresh (fund) and every SL/TP check (candidate). Persisted to DB. Column added to positions, candidate_positions, trades, candidate_trades.

### Decision: Candidate Data Parity (Session W)
- **What**: Added `candidate_signals` and `candidate_daily_performance` tables, mirroring fund-level signal and daily snapshot tracking.
- **Why**: Without signal and daily performance data, candidate strategies can only be evaluated on trade outcomes. The orchestrator needs the full picture — what signals were generated, which were acted on, how the portfolio evolved daily — to make informed promotion decisions.
- **Design**: Signals captured in `CandidateRunner.run_scan()`, daily snapshots in `CandidateManager.persist_state()`. Both tables pruned 30 days after candidate resolved.
