# Trading Brain: System Goal, Roadmap & Risk Analysis

> Created: 2026-02-08 | Updated: 2026-02-22 | Status: Deployed on VPS, paper trading, 264/264 tests

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

## System Audit Summary (2026-02-17)

### Fully Implemented & Verified

| Component | Status |
|-----------|:---:|
| IO Contract (types, interfaces, Action.MODIFY) | Working |
| Config system (TOML + .env + validation) | Working |
| Database (20+ tables, async SQLite, system_meta) | Working |
| Kraken REST + WebSocket v2 (orders, OHLC, fees, fill confirmation) | Working |
| Risk manager (9 checks, rollback, halt evaluation) | Working |
| Portfolio tracker (paper + live, cash reconciliation, tags, multi-position) | Working |
| Data store (tiered OHLCV, aggregation, pruning) | Working |
| Strategy loader (import, archive, deploy, DB fallback) | Working |
| Strategy sandbox (AST validation, transitive import blocking) | Working |
| Backtester (multi-TF, LIMIT simulation, per-symbol spread, slippage) | Working |
| AI client (Anthropic + Vertex, token tracking, retry) | Working |
| Orchestrator (nested loops, candidates, reflection, predictions) | Working |
| Reporter (daily/weekly summaries) | Working |
| Telegram bot (16 commands, /fund, /outlook, /ask, /orchestrate, /reload) | Working |
| Notifier (26 event types, dual dispatch, signal drought, config_reloaded) | Working |
| Data API (20 REST endpoints, WebSocket, activity stream) | Working |
| Statistics modules (market + trade performance) | Working |
| Truth benchmarks (28 metrics, cached) | Working |
| Candidate system (3 slots, paper simulation, promote/cancel) | Working |
| Institutional learning (predictions, reflection, MAE, strategy doc versioning) | Working |
| Decision feedback loop (outcome tracking, SINCE YOUR LAST CYCLE) | Working |
| Observability (Prometheus /metrics, Loki, Grafana 53-panel dashboard) | Working |
| Activity log (unified timeline, REST + WS endpoints) | Working |
| Main (lifecycle, scheduler, restart safety L1-L9) | Working |
| Live config reload (SIGHUP + /reload, 3-tier deploy) | Working |
| Integration tests | **264/264 all green** |

All previously known issues from 11 audit rounds (Sessions 10-H) have been resolved. See progress.md for detailed fix history.

---

## Predicted Problems

### Near-Term (Paper Trading Phase)

**1. Signal Drought** — EXPECTED BEHAVIOR
- EMA crossover needs strong directional moves. Crypto ranges 60-70% of the time.
- No signals = no losses. Orchestrator will eventually adapt strategy.
- Signal drought notification added (24h of 0 signals).

**2. First-Trade Profitability Wall** — HIGH RISK
- Round-trip fees 0.65-0.80% require ~1.5% favorable move to profit.
- Early trades likely to lose. Monitor consecutive-loss rollback threshold.

**3. Orchestrator Cold Start** — MEDIUM RISK
- First cycles have little trade data. May make premature strategy changes.
- Mitigated by reflection system (earned knowledge) and prediction accountability.

**4. ~~Laptop Uptime~~ → SOLVED**: Deployed to VPS (Sessions 16-17)

**5. ~~Nightly Orchestration Timing~~ → SOLVED**: VPS runs 24/7

### Medium-Term (First Month)

**6. Strategy Evolution Quality** — MEDIUM RISK
- First iterations may not improve. System needs losing trades to learn from.
- Candidate system allows testing alternatives without risking fund capital.

**7. Token Cost Creep** — LOW RISK
- Even "no change" nights cost ~$0.30-0.50. Budget $22-45/month.
- Token tracking built in. Daily limit enforced. Usage visible via `/ask` or API.

**8. Paper vs Live Divergence** — MEDIUM RISK
- Paper simulates slippage but real slippage may be worse on low-liquidity pairs.
- Start live with BTC/USD only (highest liquidity), smallest positions.

### Long-Term (Months 2+)

**9. ~~Client-Side SL/TP Only~~ → SOLVED**: Exchange-native SL/TP on Kraken (Session B/D4)

**10. Database Growth** — LOW RISK: 9 symbols, nightly aggregation, <50MB for years

**11. Kraken API Reliability** — LOW RISK: ~9 calls/scan, well within limits, retry built in

---

## Necessary Changes by Phase

### Before Going Live
- [x] Order fill tracking (Session B/D7)
- [x] Server-side stop-losses (Session B/D4)
- [x] Position reconciliation on startup (Session B)
- [x] WebSocket failure alerting (Session 9)
- [x] VPS deployment (Sessions 16-17)
- [x] Restart safety (Session L)
- [ ] **Live mode testing** — verify Kraken API key permissions with small real order

### Before Scaling Capital (Month 4+)
- [ ] Order book depth analysis
- [ ] Tax reporting — export trades for Canadian tax filing
- [ ] Automated DB backups

---

## Future Implementation Roadmap

### Phase 0: Statistics Shell & Orchestrator Upgrade — COMPLETE
All 9 implementation steps complete (Sessions 5-9). Then 11 audit sessions hardened the system. Deployed to VPS. 264/264 tests passing.

### Phase 1: Paper Validation — IN PROGRESS (started 2026-02-12)
**Goal**: Prove the system works end-to-end. First trades. First orchestration cycles.

- [x] Run paper trading 24/7 on VPS
- [x] First orchestration cycle ran (Session K)
- [x] Deployed with restart safety (Session L)
- [x] Candidate strategy system (Session T)
- [x] Institutional learning system — predictions + reflection (Session W)
- [x] Decision feedback loop (Session AB)
- [x] Live config reload + 3-tier deploy (Session AD)
- [ ] 10+ paper trades completed
- [ ] 5+ orchestration cycles with strategy changes
- [ ] Observe strategy evolution and candidate promotions
- [ ] Gather baseline performance data

**Exit criteria**: System has completed 10+ paper trades and 5+ orchestration cycles with strategy changes

### Phase 2: Strategy Maturation
**Goal**: Strategy stabilizes from frequent iteration to measured improvement.

- Review strategy document after 1 month of reflections
- Analyze win/loss patterns across market conditions
- Expanded analysis capabilities

**Exit criteria**: Positive expectancy over 30+ trades, strategy stabilizing

### Phase 3: Go Live
**Goal**: Deploy with real money, smallest viable positions.

- Live mode with $200 real capital
- BTC/USD only initially (highest liquidity)
- Minimum position sizes, maximum caution
- Compare live vs paper performance

**Exit criteria**: First profitable live month

### Phase 4: Scale & Diversify (Months 5-8)
**Goal**: Increase capital and add trading approaches.

- Add ETH/USD, SOL/USD to live trading
- Increase position sizes gradually
- Cross-pair correlation analysis
- Fee tier progression (volume → lower fees)

**Exit criteria**: Consistent monthly profitability, stable strategy

### Phase 5: Full Hedge Fund Mode (Months 9-12)
**Goal**: Sophisticated multi-strategy system.

- Multiple concurrent strategies (day + swing + position)
- Regime-aware position sizing and strategy selection
- Market microstructure analysis
- Expanded pair universe
- Evolution levels 2-4 (prompt evolution, strategy composition, advanced code gen)

**Exit criteria**: System manages $1K+ with minimal human intervention

### Phase 6: Long-Term (Year 2+)
**Goal**: Mature, self-sustaining trading operation.

- Multi-year data advantage (7-year OHLCV history)
- Refined strategy library tested across bull/bear/range markets
- Potential expansion to additional exchanges or markets
- Scale capital based on track record
- Strategy document becomes genuine institutional knowledge

---

## Long-Term Strategic Considerations

### The Data Advantage
The system's most valuable asset over time isn't the strategy code — it's the **accumulated data and institutional memory**. This data compounds. A strategy written in year 3 has access to patterns that no day-1 strategy could.

### The Fee Problem
Round-trip fees of 0.65-0.80% strongly favor fewer, higher-conviction trades, swing/position trading over day trading, maker orders (0.25% vs 0.40%), and volume accumulation for lower tiers.

### Evolution Velocity
- **Month 1**: Rapid iteration (daily changes). Learning to walk.
- **Months 2-3**: Iteration slows. Changes become tweaks, not overhauls.
- **Months 4+**: Stability with occasional adaptation.
- If still making daily overhauls at month 3, something is wrong.

### When to Go Live
1. Positive paper expectancy over 50+ trades
2. No shell-triggered rollbacks in last 2 weeks
3. Strategy has survived at least one market regime change
4. VPS running 24/7 for 1+ week without issues (done)
5. User trusts the system via Telegram observability
6. ~~Live-mode changes (order fill tracking, server-side stops)~~ — DONE
7. Live mode tested with small real order to verify Kraken API key permissions
