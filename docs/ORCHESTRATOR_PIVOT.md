# Orchestrator Pivot — Design Notes

> Session: 2026-02-27. Based on 12-day live performance review of paper trading VPS deployment.

## Context

After 12 days of paper trading (Feb 15–27), the system has zero live trades, $100 unchanged, and $38.41 in AI costs. The orchestrator correctly diagnoses its own problems but is paralyzed — cautious identity, slow decision-making, and a broken strategy generation pipeline where Sonnet generates code that doesn't match the orchestrator's intent.

## Performance Review Findings

### What works
- Infrastructure is solid: reliable scanning (31,158+ scans), WebSocket recovery, daily snapshots, data aggregation
- Orchestrator analysis quality is high — strategy document shows genuine learning
- Candidate system functions mechanically (trades execute, SL/TP work, paper portfolios track)
- Prediction system generates and grades predictions

### What doesn't work
- **Zero live trades in 12 days** — active strategy never generated a signal
- **Strategy description/code mismatch**: `strategy_versions.description` captures orchestrator intent, not actual generated code. Orchestrator asks for "momentum breakout," Sonnet generates "RSI < 25 mean-reversion." System doesn't catch the mismatch.
- **Orchestrator paralysis**: 11 days to promote a single strategy. Correctly identifies "Analysis Loop Without Execution" as its own failure mode but doesn't break the pattern.
- **Prediction miscalibration**: 33% refutation rate on "high confidence" predictions
- **AI cost disproportionate**: $38.41 against $100 fund (38% of fund value)

### Bugs found
- [ ] **Duplicate candidate trades**: Each of 7 candidate trades recorded 4x (28 rows for 7 unique trades)
- [ ] **Feb 16 runaway cycles**: Deployment restarts triggered multiple orchestrator cycles — schedule guard doesn't persist across restarts
- [ ] **Strategy description field**: Captures orchestrator intent, not actual generated code — needs validation or extraction from code

---

## Design Decisions

### 1. Modular Orchestration Loop (Phase Architecture)

**The most fundamental change.** Replace the monolithic single-call orchestration cycle with a modular, extensible phase-based loop.

#### Architecture

```python
class Phase:
    """One step in the orchestration cycle."""
    name: str                    # "reflect", "observe", "evaluate", "decide"
    required: bool               # If True, failure aborts the cycle
    model: str                   # "opus", "sonnet", "haiku"

    async def build_context(self, state: CycleState) -> dict
    async def build_prompt(self, context: dict) -> list[Message]
    async def parse_output(self, response: str, state: CycleState) -> PhaseResult

class CycleState:
    """Shared memory across phases. Each phase reads previous outputs, writes its own."""
    cycle_id: str
    phase_results: dict[str, PhaseResult]   # phase_name → output
    decisions: list[Decision]                # accumulated across phases

class OrchestrationCycle:
    phases: list[Phase]

    async def run(self):
        state = CycleState(cycle_id=generate_id())
        for phase in self.phases:
            context = await phase.build_context(state)
            prompt = await phase.build_prompt(context)
            response = await self.ai_client.call(prompt, model=phase.model)
            result = await phase.parse_output(response, state)
            state.phase_results[phase.name] = result
```

#### Design properties

- **Reorderable**: Phases are a list. Swap order, loop doesn't care. Dependencies are explicit in `build_context`.
- **Addable**: New phase = new Phase subclass, insert into list. Future phases (BACKTEST_REVIEW, MARKET_ALERT) slot in without restructuring.
- **Removable**: Skip phases by removing from list or marking disabled. Useful for debugging.
- **Model-flexible**: Each phase declares its model. REFLECT/OBSERVE/EVALUATE/DECIDE use Opus. EXECUTE uses Sonnet for code gen, Opus for review. Future summary/compression phases could use Haiku.
- **Failure-isolated**: `required` flag controls whether failure aborts the cycle. Failed REFLECT shouldn't block OBSERVE. Failed DECIDE should block EXECUTE.
- **Observable**: Each phase naturally produces activity_log entry, token_usage record, and timing data. Per-phase cost visibility.

#### Default phase sequence

```
Phase 1: REFLECT (Opus)
  Input:  predictions + recent grading data + strategy doc
  Output: graded predictions, updated strategy doc, learnings summary

Phase 2: OBSERVE (Opus)
  Input:  market data + indicators + external data (F&G, funding, OI, dominance)
          + strategy doc (from Phase 1)
  Output: market regime assessment, observations, notable conditions

Phase 3: EVALUATE (Opus)
  Input:  candidate performance summaries + active strategy performance
          + trade performance report + Phase 2 observations
  Output: per-candidate assessment, active strategy assessment,
          what's working/failing, what to test next

Phase 4: DECIDE (Opus)
  Input:  Phase 2 + Phase 3 outputs + strategy doc + current module code
          (only for modules being considered for modification)
  Output: decisions list with pseudocode for each code generation action

Phase 5: EXECUTE (per decision — Sonnet + Opus)
  5a: Sonnet implements pseudocode within framework
  5b: Sandbox validates syntax/safety/contract
  5c: Opus reviews generated code against pseudocode spec
  5d: Accept, revise, or reject
```

#### Prompt structure per phase

```
System prompt = IDENTITY (shared across all phases, ~500 tokens)
              + PHASE_INSTRUCTIONS (per-phase, focused on cognitive task)
              + PHASE_CONTEXT (data from build_context)
              + PREVIOUS_PHASE_OUTPUTS (selectively piped from CycleState)
```

The identity block is constant — "you are a competent fund manager, your goal is to grow the portfolio." Phase instructions define the current task — "you are grading predictions against reality" or "you are deciding what actions to take."

### 2. Orchestrator Identity Shift

**From**: Cautious fund manager who agonizes over decisions, treats candidates as high-stakes auditions
**To**: Competent, fast, intelligent fund manager who grows the portfolio through continuous learning and improvement

Core principles:
- The orchestrator's only goal is to grow the portfolio
- Candidates are paper-trading tools for testing hypotheses — not auditions
- Failure is information; inaction is the worst outcome
- Continuous improvement is the permanent operating mode, not a "research phase" that ends
- Conservative with real funds, aggressive with paper testing and learning
- When the orchestrator is confident in an algorithm (after hypothesis → test → fail → learn → prevent failure → test again), it introduces it to the active strategy

### 3. Expanded Action Space

Current actions: NO_CHANGE, CREATE_CANDIDATE, CANCEL_CANDIDATE, PROMOTE_CANDIDATE

New actions:
- **CREATE_CANDIDATE** — with pseudocode. Can be independent hypothesis OR modified version of active strategy for testing.
- **CANCEL_CANDIDATE** — cancel a running candidate, capture learnings.
- **PROMOTE_CANDIDATE** — candidate code becomes active strategy.
- **MODIFY_ANALYSIS** — rewrite/modify market analysis module (pseudocode → Sonnet → sandbox → review, no candidate testing needed).
- **MODIFY_PERFORMANCE** — rewrite/modify trade performance module (same pipeline, no candidate testing).
- **NO_CHANGE** — explicit decision to hold.

Active strategy modifications go through candidates: orchestrator creates a candidate with the modified active strategy code, tests on paper, then promotes. Not a separate action — a specific use of CREATE_CANDIDATE + PROMOTE_CANDIDATE.

### 4. Candidate Slots: 3 → 6

- Explained as powerful, flexible sandboxes for testing any idea
- Awareness of capability, not direction on usage (minimize direction principle)
- Can be short-lived — cancel after 48 hours if the learning is captured
- All 6 can run simultaneously

### 5. Pseudocode-Driven Code Generation

**Problem**: Orchestrator describes intent in natural language → Sonnet designs AND implements → code drifts from intent

**Solution**: Orchestrator writes pseudocode for the algorithm → Sonnet implements within the framework → review validates implementation matches pseudocode

This shifts algorithm design to Opus (which has institutional memory and market understanding) and reduces Sonnet's role to translation/implementation. Scales naturally as the orchestrator's understanding grows.

### 6. Unified Code Pipeline for All Module Types

Same Opus pseudocode → Sonnet implements → sandbox validates → Opus reviews process for:
- **Strategy** (active and candidate) — code that trades
- **Market analysis** — code that sees
- **Trade performance** — code that evaluates

The orchestrator should understand it can improve all three. Currently it has not modified the statistics modules at all.

### 7. Active Strategy Evolution (Option B)

Default: Orchestrator sends current strategy code + modification instructions + pseudocode for changes. Sonnet implements the modification.

Can also do full rewrites when the orchestrator judges that's better.

**Critical**: Modifications to the active strategy are tested through candidate pipeline before going live. The orchestrator creates a candidate with the modified version, runs it on paper, and only applies the change if performance is acceptable.

### 8. Richer Market Data — External Sources

Add free API data sources to give the orchestrator broader market awareness:
- **Fear & Greed Index** (Alternative.me) — daily sentiment
- **BTC/ETH Dominance** (CoinGecko) — money flow / risk appetite
- **Total Market Cap** (CoinGecko) — macro context
- **Funding Rates** (Binance Futures public) — derivatives positioning / leverage
- **Open Interest** (Binance Futures public) — leverage buildup

Collection approach (from Kairex):

| Data | Source | Interval | Auth | Per-Asset? |
|---|---|---|---|---|
| Funding rates | Binance `GET /fapi/v1/fundingRate` | 8h | None | Yes |
| Open interest | Binance `GET /fapi/v1/openInterest` | 1h | None | Yes |
| Fear & Greed | Alternative.me `GET /fng/` | Daily | None | No (global) |
| Dominance + mcap | CoinGecko `GET /api/v3/global` | Hourly | Optional free key | No (global) |

Storage: 3 new tables — `funding_rates` (symbol, timestamp, rate), `open_interest` (symbol, timestamp, value), `index_values` (index_type, timestamp, value).

Symbol mapping: Kraken pairs (BTC/USD) → Binance perps (BTCUSDT) for funding/OI queries. All 9 symbols have Binance perp equivalents.

**Data flows to all three module types**: strategies (via contract), market analysis (via DB), and trade performance (via DB).

### 9. Default Indicator Set in Market Analysis

Kairex-style technical indicators as a starting point:
- RSI (multi-timeframe: 5m, 1h, 1d)
- Bollinger Bands + bandwidth
- MACD histogram
- ADX
- EMA ribbon
- Volume ratios

Plus derived metrics from external data:
- Funding rate percentiles (90d), streaks, trends
- OI rate-of-change (24h, 7d)
- F&G with 7d/30d averages and trend direction
- Dominance with 7d/30d change

Orchestrator can modify, add, or remove any of these. They provide visibility into WHY strategies fire or don't — currently the orchestrator is blind to indicator values.

### 10. Trade Performance Covers Candidates

Trade performance analysis should query `candidate_trades` in addition to `trades`, so the orchestrator has quantitative feedback on experimental results. Without this, the orchestrator's evaluation capability is empty during the research phase.

### 11. Contract Changes for External Data

Per-symbol data (funding rates, open interest) → new fields on `SymbolData`.

Global data (fear & greed, dominance, market cap) → new `MarketContext` dataclass passed alongside `markets` and `portfolio` in the `analyze()` call.

This is a shell contract change — deliberate and stable once designed.

---

## Implementation Order

Sequenced to build foundational changes first, then layer capabilities on top.

### Phase 1: Orchestration Loop Architecture
The skeleton everything else hangs on.
- [ ] Design Phase base class, CycleState, OrchestrationCycle
- [ ] Implement REFLECT phase (extract from monolithic loop)
- [ ] Implement OBSERVE phase (extract from monolithic loop)
- [ ] Implement EVALUATE phase (extract from monolithic loop)
- [ ] Implement DECIDE phase (extract from monolithic loop)
- [ ] Implement EXECUTE phase (refactor code generation inner loop)
- [ ] Per-phase logging, token tracking, error handling
- [ ] Shared identity prompt block
- [ ] Wire into main.py scheduler (replace old orchestrator entry point)
- [ ] Tests for phase sequencing, failure isolation, state passing

### Phase 2: Identity & Prompt Rewrite
New voice, new capabilities awareness.
- [ ] Write new orchestrator identity (shared across all phases)
- [ ] Write per-phase instructions (REFLECT, OBSERVE, EVALUATE, DECIDE, EXECUTE)
- [ ] Update candidate awareness (tools for testing, not auditions; 6 slots)
- [ ] Add module modification awareness (market analysis, trade performance)
- [ ] Update system awareness with all available data, tools, and capabilities

### Phase 3: Pseudocode Pipeline & Expanded Actions
Fix the code generation problem.
- [ ] Update DECIDE phase output schema (pseudocode field, expanded action types)
- [ ] Update EXECUTE phase: Sonnet receives pseudocode + current code + framework contract
- [ ] Update review step: Opus validates code against pseudocode spec (not just "does this look reasonable")
- [ ] Add MODIFY_ANALYSIS and MODIFY_PERFORMANCE action types
- [ ] Unified code pipeline for all three module types (strategy, market analysis, trade performance)
- [ ] Strategy description field: extract from actual generated code, not orchestrator intent

### Phase 4: Candidate System Expansion
More experimental capacity.
- [ ] Expand candidate slots from 3 to 6
- [ ] Update candidate manager, DB schema, runner
- [ ] Update trade performance module to include candidate trade data
- [ ] Update EVALUATE phase to handle 6 candidates efficiently

### Phase 5: External Market Data
Broader awareness.
- [ ] Symbol mapping layer (Kraken → Binance)
- [ ] Binance Futures REST client (funding rates, open interest)
- [ ] Alternative.me client (fear & greed)
- [ ] CoinGecko client (dominance, market cap)
- [ ] New DB tables + migrations (funding_rates, open_interest, index_values)
- [ ] Polling jobs on scheduler (8h/1h/daily/hourly)
- [ ] Backfill on startup
- [ ] Retention and pruning

### Phase 6: Contract & Module Updates
Wire new data through to all consumers.
- [ ] Update `SymbolData` with funding_rate, open_interest fields
- [ ] Add `MarketContext` dataclass (F&G, dominance, market cap)
- [ ] Update strategy contract `analyze()` signature
- [ ] Update default market analysis with indicator set + external data derived metrics
- [ ] Update default trade performance to cover candidate trades + market condition correlation
- [ ] Update sandbox allowlist if needed

### Phase 7: Bug Fixes
- [ ] Fix duplicate candidate trades (investigate 4x insertion in candidate runner/manager)
- [ ] Fix orchestrator restart guard (persist last cycle time across restarts)

### Phase 8: Deploy & Verify
- [ ] Test full cycle locally
- [ ] Deploy to VPS
- [ ] Verify Binance API access from Hetzner VPS
- [ ] Monitor first 2-3 orchestration cycles
- [ ] Verify external data collection working
