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
