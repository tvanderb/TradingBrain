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

## Risk Limits: Hard-Set, User-Only
- Max 5% of portfolio on a single trade
- Default 1-2% per trade
- Max positions: configurable
- Max daily loss: configurable %
- Max drawdown: configurable %
- Agent CANNOT modify these. Shell enforces as safety net on all signals.

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
- **Shell-enforced**: 5% portfolio drop/day → halt + rollback, 10 consecutive losses → pause, crashes → immediate rollback
- **Orchestrator-level**: Win rate <40% over 30 trades, negative expectancy over 1 week, Sharpe <0.3 over 2 weeks, drawdown >8%

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

## Performance Criteria (Priority)
1. Expectancy = (win_rate × avg_win) - (loss_rate × avg_loss)
2. Win Rate
3. Sharpe Ratio
4. P&L (outcome metric)
- Also track: Profit Factor, Max Drawdown, Time in Market

## Skills Library
- Agent builds reusable indicator functions in `strategy/skills/`
- Pure functions only (data in, result out, no side effects)
- Strategy Module imports and composes them
- Independently testable

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

### Decision: Explicit Orchestrator Goals
- **What**: Clear, prioritized goals embedded in the orchestrator's system prompt
- **Why**: Without explicit goals, the orchestrator optimizes for whatever seems locally reasonable. Could lead to random changes, over-trading, or analysis paralysis.
- **Primary**: Positive expectancy after fees
- **Secondary**: Win rate > 45%, Sharpe > 0.3, positive monthly P&L
- **Meta**: Conservative, build understanding, improve observability, institutional memory

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

### Decision: Aggressive Tilt — Low-Frequency High-Conviction Goals (Session 6)
- **What**: Replaced win rate and Sharpe ratio targets with profit factor and avg_win/avg_loss ratio. Loosened risk limits to give system room for asymmetric payoff strategies. Added fee-awareness meta-goal.
- **Why**: The fee wall (0.65-0.80% round-trip) means the system needs 1.5-2% moves to profit. This naturally favors fewer, bigger trades over frequent small-edge trades. A trend-following strategy can be very profitable at 30% win rate if wins are 3x larger than losses. The old 45% win rate target would push the orchestrator toward high-frequency approaches that get eaten by fees.
- **Changes**:
  - **Goals**: Win rate >45% → profit factor >1.2 + avg_win/avg_loss >2.0. Sharpe >0.3 → informational only. Added fee-awareness: expected move must be >3x round-trip fees.
  - **Risk limits**: max_trade_pct 5%→7%, max_position_pct 10%→15%, max_daily_loss 3%→6%, max_drawdown 10%→12%, consecutive_losses 10→disabled (999), rollback_daily_loss 5%→8%, default_trade_pct 2%→3%, default_take_profit 4%→6%.
  - **Philosophy**: Lower the floor on what's acceptable while learning (profit factor 1.2, drawdown 12%), raise the bar on what constitutes a good trade (2.0 reward-to-risk, 3x fees). Drawdown is the real safety net, not streak length.
- **Risk**: Wider limits mean the system can lose more before halting ($24 vs $20 on $200). Acceptable because: still survivable, and over-constraining early prevents the system from finding its edge.
