# Trading Brain: System Goal, Audit, Roadmap & Risk Analysis

> Created: 2026-02-08 | Updated: 2026-02-11 | Status: Deployed, 72-hour paper test in progress

---

## System Goal

**Build an autonomous, self-evolving crypto trading system that generates consistent risk-adjusted returns through continuous strategy refinement, starting from a $200 paper account and scaling to real capital once profitability is proven.**

### Success Criteria
> **Note**: Specific numeric targets below are from Session 6 and serve as rough benchmarks only. Per the fund mandate framework (Sessions 7-8), the orchestrator is NOT given these as goals. The mandate is: "Portfolio growth with capital preservation. Avoid major drawdowns. Long-term fund." The orchestrator determines what matters based on its own judgment.

| Metric | Paper Phase Benchmark | Live Phase Benchmark |
|--------|-------------------|-------------------|
| Expectancy | > 0 (any positive) | > 0.5% per trade |
| Max Drawdown | < 40% (shell-enforced) | < 25% |
| Monthly P&L | Positive 2 of 3 months | Consistently positive |
| Strategy Evolution | At least 3 iterations | Stabilizing, fewer changes |

### What This System Is
- A mini autonomous crypto hedge fund operating 24/7
- A self-improving closed loop: trade → measure → analyze → adapt → trade
- An institutional memory that learns market patterns over years
- Full spectrum: day trading, swing trading, position holding — strategy decides

### What This System Is NOT
- A get-rich-quick scheme — expects losses early while learning
- A black box — full transparency via Telegram + strategy document
- Unlimited risk — hard shell-enforced limits the AI cannot override
- A static system — designed to evolve, not to stay the same

---

## System Audit Summary (2026-02-11)

### Fully Implemented & Verified

| Component | Tests | Status |
|-----------|:---:|:---:|
| IO Contract (types, interfaces, Action.MODIFY) | 6 | Working |
| Config system (TOML + .env + validation) | 5 | Working |
| Database (17+ tables, async SQLite, system_meta) | 8 | Working |
| Kraken REST client (orders, OHLC, fees, fill confirmation) | 3 | Working |
| Kraken WebSocket v2 (ticker, OHLC, reconnect) | 2 | Working |
| Risk manager (9 checks, rollback, halt evaluation) | 12 | Working |
| Portfolio tracker (paper + live, cash reconciliation, tags) | 15 | Working |
| Data store (tiered OHLCV, aggregation, pruning) | 4 | Working |
| Strategy loader (import, archive, deploy, DB fallback) | 5 | Working |
| Strategy sandbox (AST validation, transitive import blocking) | 8 | Working |
| Backtester (LIMIT simulation, per-symbol spread) | 7 | Working |
| AI client (Anthropic + Vertex, token tracking) | 3 | Working |
| Orchestrator (nightly AI cycle, code storage) | 6 | Working |
| Reporter (daily/weekly summaries) | 2 | Working |
| Telegram bot (16 commands, health, outlook, ask/Haiku) | 12 | Working |
| Notifier (18 event types, dual dispatch) | 6 | Working |
| Data API (10 REST endpoints, WebSocket) | 14 | Working |
| Statistics modules (market + trade performance) | 8 | Working |
| Truth benchmarks (28 metrics) | 4 | Working |
| Main (lifecycle, scheduler, restart safety L1-L9) | 10 | Working |
| Integration tests | **161/161** | **All green** |

### All Previously Known Issues — Resolved

1. ~~Paper test duration not enforced~~ — **FIXED** (Session J): `min_paper_test_trades` config, inconclusive status
2. ~~Live order fill tracking missing~~ — **FIXED** (Session B/D7): `query_order()` polling with fill confirmation
3. ~~WebSocket max-retry silent failure~~ — **FIXED** (Session 9): `set_on_failure()` callback + Telegram alert
4. ~~Import path fragility~~ — **FIXED** (Session 9): Top-level imports
5. ~~Client-side SL/TP only~~ — **FIXED** (Session B/D4): Exchange-native orders on Kraken
6. ~~Skills library import failure~~ — **FIXED** (Session K): Skills library removed, direct imports
7. ~~Paper cash phantom profit~~ — **FIXED** (Session L/L1): `system_meta` table + first-principles reconciliation
8. ~~No halt evaluation on restart~~ — **FIXED** (Session L/L2): `evaluate_halt_state()` on startup
9. ~~Strategy single point of failure~~ — **FIXED** (Session L/L4): DB fallback + paused mode

---

## Predicted Problems

### Near-Term (Paper Trading Phase)

**1. Signal Drought**
- **Risk**: HIGH
- **Why**: EMA 9/21 crossover needs strong directional moves with volume. Crypto ranges 60-70% of the time. System may go hours or days without generating a signal.
- **Impact**: Frustrating but not dangerous. No signals = no losses.
- **Mitigation**: This is expected behavior. The orchestrator will eventually adapt the strategy to trade in more conditions. Patience is required.

**2. First-Trade Profitability Wall**
- **Risk**: HIGH
- **Why**: Round-trip fees of 0.65-0.80% require minimum ~1.5% favorable move to profit. On 5-minute candles, moves this large are uncommon. Early trades likely to lose.
- **Impact**: Could trigger consecutive-loss rollback (10 losses) before strategy has a fair chance.
- **Mitigation**: Monitor closely. If rollback triggers are too aggressive for the initial learning phase, consider temporarily relaxing consecutive-loss threshold from 10 to 20.

**3. Orchestrator Cold Start**
- **Risk**: MEDIUM
- **Why**: Nightly AI cycle needs performance data to analyze. With 0 trades, it has nothing to learn from. May make strategy changes based on "no signals" rather than actual trade performance.
- **Impact**: Could introduce unnecessary complexity or lower signal thresholds too aggressively.
- **Mitigation**: Strategy document already notes "observe v001 for at least 1 day." Opus should respect this. Monitor first orchestration report closely.

**4. Laptop Uptime**
- **Risk**: HIGH (for data continuity)
- **Why**: macOS laptop sleeps when lid closes, disconnects WiFi on sleep, restarts for updates. Already observed: 9 hours of runtime, only 5 scans completed (rest missed during sleep).
- **Impact**: Missed scans = missed trading opportunities. Nightly orchestration (3:30-6am) requires laptop to be awake and open. Data aggregation won't run during sleep.
- **Mitigation**: Keep laptop plugged in + awake during paper testing. Move to VPS for unattended 24/7 operation.

**5. Nightly Orchestration Timing**
- **Risk**: MEDIUM
- **Why**: Orchestration window is 3:30-6am EST. If laptop is asleep, orchestrator never runs. Strategy never evolves.
- **Impact**: System runs the same strategy forever. No learning.
- **Mitigation**: Either keep laptop awake overnight, or shift orchestration window to a time when laptop is awake (e.g., 8pm). VPS solves this permanently.

### Medium-Term (First Month)

**6. Strategy Evolution Quality**
- **Risk**: MEDIUM
- **Why**: Sonnet-generated strategies may be naive. System needs enough losing trades for Opus to identify patterns. Chicken-and-egg: need trades to learn, need good strategy to trade.
- **Impact**: First few strategy iterations may not improve performance.
- **Mitigation**: This is expected. The system is designed to iterate. Strategy document provides guardrails against wild swings in approach.

**7. Token Cost Creep**
- **Risk**: LOW
- **Why**: Even "no change" nights cost ~$0.30-0.50 for Opus analysis. Strategy changes add $0.50-2.00 per revision cycle. Budget is $22-45/month.
- **Impact**: At worst, burns through $300 Vertex credit in 7-14 months.
- **Mitigation**: Token tracking is built in. `/tokens` command shows daily usage. Budget limits are enforced in AI client.

**8. Paper vs Live Divergence**
- **Risk**: MEDIUM
- **Why**: Paper trading simulates 0.05% slippage, but real slippage on SOL/USD (lower liquidity) could be 0.1-0.5% in volatile markets. Paper results may be optimistic.
- **Impact**: Strategy that appears profitable in paper may lose money live.
- **Mitigation**: Track simulated vs. real spreads. When going live, start with BTC/USD only (highest liquidity) and smallest possible positions.

### Long-Term (Months 2+)

**9. Market Regime Shifts**
- **Risk**: MEDIUM
- **Why**: Crypto can go from calm to extreme volatility in minutes (flash crashes, regulatory news, exchange hacks). Strategy scans every 5 minutes — a lot can happen between scans.
- **Impact**: Positions could gap through stop-losses. Paper mode won't show this — paper fills are instant.
- **Mitigation**: Live mode should use Kraken's native stop-loss orders (server-side, not scan-dependent). This isn't implemented yet — currently all SL/TP checking is scan-based.

**10. Database Growth**
- **Risk**: LOW
- **Why**: 3 symbols * 288 5-min candles/day = 864 rows/day. 30 days = ~26K rows. After aggregation: 1h = 72/day, daily = 3/day.
- **Impact**: Minimal — SQLite handles millions of rows. Brain.db will stay under 50MB for years.
- **Mitigation**: Nightly aggregation keeps it trim. Already designed for 7-year retention.

**11. Kraken API Reliability**
- **Risk**: LOW-MEDIUM
- **Why**: REST API has rate limits (~15 calls/second). Current usage: ~9 calls per scan (3 symbols * 3 endpoints). Well within limits.
- **Impact**: Adding more symbols could approach limits. Major Kraken outages would halt trading.
- **Mitigation**: Built-in retry logic. Could add request throttling if expanding to 10+ symbols.

---

## Necessary Changes by Phase

### Before Unattended Paper Trading — ALL DONE
- [x] PID lockfile prevents multiple instances
- [x] WebSocket reconnection with backoff
- [x] Test Telegram commands from phone
- [x] Commit all code to git
- [x] Statistics shell — truth benchmarks + two analysis modules
- [x] Scan results collection — indicator state + strategy_regime every scan
- [x] Regime tagging — strategy_regime on trades and signals
- [x] Orchestrator awareness upgrade — labeled inputs, truth benchmarks, analysis reports, drought detection, paper test awareness

### Before First Orchestration Cycle
- [x] **Orchestrator thought spool** — `orchestrator_thoughts` DB table stores every AI response per cycle. Browsable via `/thoughts` (cycle list) and `/thought <cycle> <step>` (full response). Instrumented at all 5 AI call sites in orchestrator.

### Before Going Live (Month 2-3)
- [x] **Order fill tracking** — DONE (Session B/D7): `query_order()` polling with fill confirmation
- [x] **Server-side stop-losses** — DONE (Session B/D4): Exchange-native SL/TP on Kraken
- [x] **Position reconciliation on startup** — DONE (Session B): `_reconcile_orders()` on startup
- [x] **Paper test enforcement** — DONE (Session J): `min_paper_test_trades` with inconclusive status
- [x] **WebSocket failure alerting** — DONE (Session 9): `set_on_failure()` callback + Telegram alert
- [x] **VPS deployment** — DONE (Sessions 16-17): Docker + Ansible + Caddy + monitoring
- [x] **Restart safety** — DONE (Session L): 9 landmines fixed (L1-L9)
- [ ] **Live mode testing** — verify Kraken API key permissions with small real order

### Before Scaling Capital (Month 4+)
- [ ] **Order book depth analysis** — check liquidity before sizing trades
- [x] **SL/TP order management** — DONE (Session B/D4): System manages cancel-other logic for SL+TP pairs
- [ ] **Tax reporting** — export trades in format for Canadian tax filing
- [ ] **Automated DB backups** — scheduled brain.db snapshots

---

## Future Implementation Roadmap

### Phase 0: Statistics Shell & Orchestrator Upgrade — COMPLETE
**Goal**: Give the orchestrator situational awareness, hard-computed statistics, and clear mandate before it runs its first cycle.

> All 9 implementation steps complete (Sessions 5-9). Then 11 audit sessions (Sessions 10-L) hardened the system: 200+ fixes, position system redesign, exchange-native orders, restart safety, skills library removal. 161/161 tests passing. Deployed to VPS.

**Architecture**: Two flexible analysis modules + one rigid truth benchmarks layer:
- **Truth Benchmarks** (rigid shell) — simple verifiable metrics, orchestrator cannot modify
- **Market Analysis Module** (flexible) — analyzes exchange data, indicators, regimes, scan patterns
- **Trade Performance Module** (flexible) — analyzes trade execution quality, strategy effectiveness, fees

Both flexible modules follow the same IO-container pattern as the strategy: orchestrator rewrites, Opus reviews for mathematical correctness, sandbox validates, deploy. No paper testing needed (read-only).

#### Implementation Steps (in dependency order)

**Step 1: Database Schema Additions**
Files: `src/shell/database.py`
- Add `scan_results` table: symbol, timestamp, price, ema_fast, ema_slow, rsi, volume_ratio, spread, strategy_regime, signal_generated (bool), signal_action, signal_confidence
- Add `strategy_regime` column to `trades` table
- Add `strategy_regime` column to `signals` table
- Add indexes for efficient querying by timestamp and symbol
- Note: column is `strategy_regime` (not `regime`) — it records what the strategy *thought*, not ground truth

**Step 2: Scan Results Collection**
Files: `src/main.py` (scan loop)
- After each scan, write indicator state to `scan_results` for every symbol
- Record: price, ema_fast, ema_slow, rsi, volume_ratio, spread, strategy_regime
- Record whether a signal was generated and its action/confidence
- Tag `strategy_regime` on any signals generated
- Tag `strategy_regime` on any trades executed

**Step 3: Truth Benchmarks**
Files: `src/shell/truth.py` (new)
- Rigid shell component, orchestrator CANNOT modify
- Computes from raw DB data:
  - net_pnl, trade_count, win_count, loss_count, win_rate
  - total_fees, portfolio_value, max_drawdown, consecutive_losses
  - system_uptime, total_scans, total_signals, signal_act_rate
  - operational context: scans since startup, scan success rate, data freshness
- Returns structured dict
- All calculations trivially verifiable — no complex statistics

**Step 4: Analysis Module Infrastructure (Shared)**
Files: `src/statistics/__init__.py`, `src/statistics/loader.py`, `src/statistics/sandbox.py`
- Loader: dynamic import from `statistics/active/market_analysis.py` and `statistics/active/trade_performance.py`
  - Same pattern as strategy loader, but handles two modules
  - Each module extends `AnalysisBase` (from IO contract) with `async def analyze(db, schema) -> dict`
- Sandbox: validate code safety + verify no DB writes, no network, no filesystem writes
  - Different rules from strategy sandbox: allows read-only DB access, allows scipy/statistics imports
  - Must verify mathematical correctness (Opus review prompt focuses on formulas, edge cases, division-by-zero)
- Deploy: archive old version, write new code, verify it loads (per-module, independent)
- ReadOnlyDB wrapper: wraps aiosqlite connection, only allows SELECT queries
  - Blocks INSERT, UPDATE, DELETE, DROP, ALTER, CREATE at the query level

**Step 5: Initial Market Analysis Module**
Files: `statistics/active/market_analysis.py` (new)
- Hand-written starting point (like strategy v001)
- Analyzes exchange/indicator data:
  - Price action summary (current price, 24h change, 7d change per symbol)
  - Indicator distributions (how often RSI is overbought/oversold, EMA alignment frequency)
  - Volatility analysis (ATR, standard deviation, range width)
  - Volume patterns (time-of-day patterns, relative volume trends)
  - Scan signal proximity (how close indicators are to triggering signals)
  - Data quality report (gaps, freshness, coverage)
- Orchestrator rewrites this over time as it learns what market context it needs

**Step 6: Initial Trade Performance Module**
Files: `statistics/active/trade_performance.py` (new)
- Hand-written starting point
- Analyzes trade execution and strategy effectiveness:
  - Performance by symbol (win rate, expectancy, avg P&L per symbol)
  - Performance by strategy_regime (if enough data)
  - Signal analysis (generated vs acted, confidence vs outcome)
  - Fee impact (fees as % of gross profit, break-even move required)
  - Holding duration analysis (time in trade vs outcome)
  - Rolling metrics (7d, 30d if available)
  - Risk utilization (how close to limits, position sizing effectiveness)
- Orchestrator rewrites this over time as it learns what performance metrics matter

**Step 7: Orchestrator Integration**
Files: `src/orchestrator/orchestrator.py`
- Update `_gather_context()`:
  1. Run truth benchmarks → ground_truth dict
  2. Run market analysis module → market_report dict (independent, DB only)
  3. Run trade performance module → performance_report dict (independent, DB only)
  4. Gather strategy context (code, doc, versions)
  5. Gather operational context (system age, scan count, market state)
  - Note: Steps 2 & 3 run independently — neither module sees the other's output
  - The orchestrator cross-references both reports (correlating market conditions with trade outcomes)
  - Modules compute hard numbers accurately; the AI reasons across them
- Update `ANALYSIS_SYSTEM` prompt with labeled inputs:
  - "GROUND TRUTH (rigid shell, you cannot change this, use to verify your analysis)"
  - "YOUR MARKET ANALYSIS (you designed this module, you can rewrite it)"
  - "YOUR TRADE PERFORMANCE ANALYSIS (you designed this module, you can rewrite it)"
  - "YOUR STRATEGY (you designed this, you can rewrite it)"
  - "USER CONSTRAINTS (risk limits, goals — you cannot change these)"
- Embed explicit goals with priorities:
  - Primary: Positive expectancy after fees
  - Secondary: Win rate > 45%, Sharpe > 0.3, positive monthly P&L
  - Meta: Be conservative, build understanding, improve observability, maintain institutional memory
- Instruct orchestrator to cross-reference its analysis against truth benchmarks
- Add analysis module evolution pipeline:
  - Sonnet generates → Opus reviews (mathematical correctness focus) → sandbox → deploy
  - No paper testing required
  - Add `ANALYSIS_REVIEW_SYSTEM` prompt: verify formulas against standard definitions, check edge cases (division by zero, empty data), confirm statistical validity

**Step 8: Analysis Module Evolution in Orchestrator**
Files: `src/orchestrator/orchestrator.py`
- After main analysis decision, orchestrator can also decide: "I want to change what I measure"
- Decision options expand: NO_CHANGE / STRATEGY_TWEAK / STRATEGY_RESTRUCTURE / STRATEGY_OVERHAUL / MARKET_ANALYSIS_UPDATE / TRADE_ANALYSIS_UPDATE
- Each analysis update: generates new module code, Opus reviews for math, sandbox validates, deploys
- Must include reason: "I need to see performance by holding duration" or "I want to add correlation analysis"
- Both modules evolve independently — orchestrator can update one without touching the other

**Step 9: Tests**
Files: `tests/test_integration.py` (extend)
- Truth benchmarks produce correct values from known test data
- Market analysis module loads, receives ReadOnlyDB, returns dict
- Trade performance module loads, receives ReadOnlyDB, returns dict
- Analysis sandbox rejects writes, allows reads
- ReadOnlyDB wrapper blocks INSERT/UPDATE/DELETE/DROP/ALTER/CREATE
- Scan results are stored correctly with `strategy_regime` (not `regime`)
- `strategy_regime` is tagged on trades and signals

#### Integration Verification
After all steps:
- [ ] Existing 18 tests still pass
- [ ] New tests pass (truth benchmarks, both analysis modules, sandbox, ReadOnlyDB)
- [ ] Paper trading scan loop stores scan results with strategy_regime
- [ ] Truth benchmarks compute from DB correctly
- [ ] Market analysis module loads and runs against DB
- [ ] Trade performance module loads and runs against DB
- [ ] Orchestrator receives all inputs with explicit category labels
- [ ] Orchestrator can decide to change either analysis module independently
- [ ] System doesn't slow down perceptibly (scan loop adds ~1ms for DB write)

### Phase 1: Paper Validation (Weeks 1-4) — IN PROGRESS
**Goal**: Prove the system works end-to-end. First trades. First orchestration cycles.

- [x] Run paper trading 24/7 on VPS
- [x] First orchestration cycle ran (Session K — failed due to skills import, then fixed)
- [x] Deployed with restart safety (Session L)
- [ ] 72-hour paper test in progress (started 2026-02-11)
- [ ] First successful orchestration cycle with strategy changes
- [ ] 10+ paper trades completed
- [ ] 5+ orchestration cycles completed
- [ ] Observe strategy evolution (v001 → v002 → ...)
- [ ] Gather baseline performance data

**Exit criteria**: System has completed 10+ paper trades and 5+ orchestration cycles

### Phase 2: Strategy Maturation (Weeks 5-8)
**Goal**: Strategy stabilizes from frequent iteration to measured improvement.

- Review strategy document after 1 month of daily updates
- Analyze win/loss patterns across market conditions
- Consider adding: VWAP, Bollinger squeeze, ATR-based dynamic stops
- First quarterly document distillation
- Expanded analysis capabilities

**Exit criteria**: Positive expectancy over 30+ trades, strategy stabilizing

### Phase 3: Go Live (Months 3-4)
**Goal**: Deploy with real money, smallest viable positions.

- VPS deployment (24/7 uptime)
- Live mode with $200 real capital
- BTC/USD only initially (highest liquidity)
- Minimum position sizes, maximum caution
- Implement order fill tracking + server-side stops
- Compare live vs paper performance

**Exit criteria**: First profitable live month

### Phase 4: Scale & Diversify (Months 5-8)
**Goal**: Increase capital and add trading approaches.

- Add ETH/USD, SOL/USD to live trading
- Develop swing trading strategies (1h/4h timeframes)
- Increase position sizes gradually
- Cross-pair correlation analysis
- Portfolio-level risk management (Kelly criterion)
- Fee tier progression (volume → lower fees → better profitability)

**Exit criteria**: Consistent monthly profitability, stable strategy

### Phase 5: Full Hedge Fund Mode (Months 9-12)
**Goal**: Sophisticated multi-strategy system.

- Multiple concurrent strategies (day + swing + position)
- Regime-aware position sizing and strategy selection
- Market microstructure analysis (order book, spread dynamics)
- Expanded pair universe based on liquidity screening
- Self-reporting performance dashboard
- Evolution levels 2-4 (prompt evolution, strategy composition, advanced code gen)

**Exit criteria**: System manages $1K+ with minimal human intervention

### Phase 6: Long-Term (Year 2+)
**Goal**: Mature, self-sustaining trading operation.

- Multi-year data advantage (7-year OHLCV history)
- Refined strategy library tested across bull/bear/range markets
- Potential expansion to additional exchanges or markets
- Consider adding traditional markets if regulations allow
- Scale capital based on track record
- Strategy document becomes genuine institutional knowledge

---

## Long-Term Strategic Considerations

### The Data Advantage
The system's most valuable asset over time isn't the strategy code — it's the **accumulated data and institutional memory**:
- 7 years of tiered OHLCV data across multiple crypto pairs
- Complete trade history with AI-generated reasoning for every decision
- Strategy version index with performance metadata per market condition
- Strategy document capturing lessons learned across market cycles

This data compounds. A strategy written in year 3 has access to patterns that no day-1 strategy could. The system should prioritize **data quality and completeness** over short-term profitability.

### The Fee Problem
At current volume ($0 tier), round-trip fees of 0.65-0.80% are the single biggest obstacle to profitability. A strategy needs to generate moves of 1.5%+ to clear fees. This strongly favors:
- **Fewer, higher-conviction trades** over high-frequency approaches
- **Swing/position trading** over pure day trading (larger moves justify fees)
- **Volume accumulation** to reach lower fee tiers (at $50K volume: 0.14%/0.24%)
- **Maker orders** where possible (limit orders at 0.25% vs market orders at 0.40%)

### Evolution Velocity
- **Month 1**: Expect rapid iteration (daily changes). The system is a toddler learning to walk.
- **Months 2-3**: Iteration should slow as strategy matures. Changes become tweaks, not overhauls.
- **Months 4+**: Stability with occasional adaptation. Changes driven by market regime shifts, not learning basics.
- If the system is still making daily overhauls at month 3, something is wrong with the fitness criteria.

### Risk Philosophy
The shell-enforced risk limits exist to prevent catastrophic loss, not to optimize returns. The strategy should learn to manage its own risk within those limits:
- Shell limits = seatbelt (prevents death)
- Strategy risk management = skilled driving (prevents accidents)
- The goal is for the strategy to never trigger shell limits because it manages risk proactively

### When to Go Live
Checklist before deploying real money:
1. Positive paper expectancy over 50+ trades
2. No shell-triggered rollbacks in last 2 weeks
3. Strategy has survived at least one market regime change
4. VPS deployed and running 24/7 for 1+ week without issues
5. User has tested all Telegram commands and trusts the system
6. ~~Live-mode changes implemented (order fill tracking, server-side stops)~~ — DONE
7. Live mode tested with small real order to verify Kraken API key permissions

> **Note**: Removed specific win rate target (45%) — per fund mandate, the orchestrator determines what success looks like.
> Items 1-5 are exit criteria for Phase 1. Item 6 is done. Item 7 is the final verification before switching `mode = "live"`.
