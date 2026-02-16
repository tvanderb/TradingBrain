# Session Z: System Prompt Audit & Backtest Window Expansion

## Context

The orchestrator's second live cycle (20260216_122523) failed across all 3 outer iterations due to the backtest reviewer hallucinating incorrect IO contract field names in `revision_instructions`. Root cause: `BACKTEST_REVIEW_SYSTEM` and `CODE_REVIEW_SYSTEM` lacked critical contract details, causing a cascade of wrong advice (e.g., `reason=` instead of `reasoning=`, `market.hourly` instead of `market.candles_1h`, `Intent.SCALP` which doesn't exist).

This triggered a full cross-reference audit of every system prompt against every implementation file (`contract.py`, `sandbox.py`, `backtester.py`, `portfolio.py`, `risk.py`, `runner.py`, `manager.py`, `main.py`, `truth.py`).

## Design Philosophy

**"Maximize awareness, minimize direction."**

Every prompt should give the AI model complete, accurate knowledge of the system it operates within. No field names should be guessable — they should be documented. No constraints should be implicit — they should be stated. But the prompts should never tell the model WHAT to do with that knowledge. Awareness enables good decisions; directives constrain them.

---

## Audit Findings

### CRITICAL — Already Fixed

| ID | Prompt | Issue | Fix Applied |
|----|--------|-------|-------------|
| C1 | BACKTEST_REVIEW_SYSTEM | No IO contract reference — Opus hallucinated wrong field names (`reason=`, `.hourly`, `Intent.SCALP`) in revision_instructions, poisoning subsequent iterations | Added IO contract section with exact field names, Signal kwargs, enum values |
| C2 | CODE_REVIEW_SYSTEM | No Signal constructor params — couldn't catch `reason=` vs `reasoning=` errors | Added Signal kwargs, Action/Intent enum values, explicit "reasoning not reason" note |

### MEDIUM — To Fix

| ID | Prompt | Issue | Fix |
|----|--------|-------|-----|
| M1 | CODE_GEN_SYSTEM | OpenPosition listed 10 fields, actual has 12. Missing `.side` ("long") and `.opened_at` (datetime). Strategies can't do time-based exit logic. | Add `.side`, `.opened_at` to OpenPosition field list |
| M2 | CODE_GEN_SYSTEM | ClosedTrade listed 7 fields, actual has 11. Missing `.side`, `.qty`, `.opened_at`, `.closed_at`. Strategies can't analyze trade durations from `portfolio.recent_trades`. | Add `.side`, `.qty`, `.opened_at`, `.closed_at` to ClosedTrade |
| M3 | CODE_GEN_SYSTEM | No mention of optional StrategyBase methods: `on_fill()`, `on_position_closed()`, `get_state()`/`load_state()`, `scan_interval_minutes`. The failed cycle used `prev_rsi` as instance variable — `get_state()`/`load_state()` is how you persist that across restarts. | Add "Optional methods" section |
| M4 | CODE_GEN_SYSTEM | RiskLimits fields undocumented. Strategy receives `risk_limits` in `initialize()` but prompt doesn't list available fields. | Add RiskLimits field list |
| M5 | CODE_REVIEW_SYSTEM | Missing ClosedTrade fields. Reviewer can't verify trade analysis code. | Add ClosedTrade fields |
| M6 | CODE_REVIEW_SYSTEM | Missing RiskLimits fields. Reviewer can't verify correct usage of risk_limits. | Add RiskLimits fields |
| M7 | CODE_REVIEW_SYSTEM | Missing optional method signatures. Reviewer can't verify `on_fill(symbol, action, qty, price, intent, tag="")` or `on_position_closed(symbol, pnl, pnl_pct, tag="")` arg counts. Both backtester and runner use try/fallback for these. | Add optional method signatures |
| M8 | LAYER_2_SYSTEM | Stale: "Scan results: raw indicator values stored every scan" (line 203). Since Session 18, scan_results only stores `price` + `spread`. | Update text |
| M9 | LAYER_2_SYSTEM | Missing close_reason `promotion` in list (line 145). Used when candidate promoted with `close_all`. | Add `promotion` |
| M10 | LAYER_2_SYSTEM | "Candidates never halt" is misleading (line 114). Candidate runner self-enforces `max_positions` and `max_trade_pct` clamping — it just doesn't have halt states. | Clarify: candidates enforce position/trade limits but have no halt states |

### LOW — To Fix

| ID | Prompt | Issue | Fix |
|----|--------|-------|-----|
| L1 | CODE_GEN_SYSTEM | No execution timeout warning. `analyze()` has 30-second timeout in production. Heavy computation fails silently. | Add timeout note |
| L2 | LAYER_2_SYSTEM | Practical backtest window not explicit. Orchestrator rejected strategies for "only 2 trades in 31 days" without understanding the window constraint. **Upgraded to MEDIUM** — directly caused production failure. | State window explicitly + expand to 90 days (see below) |
| L3 | CODE_GEN_SYSTEM | MODIFY constraint unclear: "Use size_pct=0" but contract only warns, doesn't enforce. | Clarify: "size_pct is ignored for MODIFY" |

---

## Backtest Window Expansion: 30 → 90 Days

### Problem

The 30-day backtest window creates selection pressure toward aggressive strategies:
- Conservative trend-following on 9 pairs: ~2 trades in 30 days
- Backtest reviewer correctly says "insufficient data" but only path is more signal frequency
- This contradicts the fund mandate ("capital preservation, avoid major drawdowns")
- Tonight's cycle wasted all 3 iterations in this exact loop

### Decision: Expand to 90 days

**Why 90 days:**
- ~3x more trades for conservative strategies (2 → 6-15), enough to distinguish "conservative but viable" from "broken"
- Still recent enough to reflect current market conditions
- Well within 1h data retention (1 year available)
- Runtime ~15s, safely under the 60s backtest timeout
- SL/TP uses 5m precision for last 30 days, falls back to 1h for days 31-90 (backtester already has this fallback)

**Why not longer:**
- 6 months (180d): ~30s runtime, approaching timeout risk; crypto market conditions from 6 months ago may not be relevant
- 1 year: timeout risk, stale data, crypto is a different market every few months

### Implementation Changes Required

#### 1. Data retention (`src/shell/config.py`)

No config change needed — 1h retention is already 365 days, 1d is 7 years. The data exists.

#### 2. Backtest data fetching (`src/orchestrator/orchestrator.py`)

The `_run_backtest()` method fetches candle data from DB to pass to the backtester. Currently it likely fetches based on 5m availability (~30 days). Change to fetch:
- **5m**: Last 30 days (unchanged — retention limit)
- **1h**: Last 90 days (expanded from ~30d)
- **1d**: Last 90 days (expanded from ~30d)

The backtester's `_run_multi()` iterates over the union of 1h timestamps, so expanding 1h data automatically extends the backtest window.

#### 3. Bootstrap data fetching (`src/main.py`)

Verify that bootstrap fetches enough 1h data. Current: 720 candles (30 days). Need: 2160 candles (90 days). But bootstrap already fetches based on retention config (365 days for 1h), so this should already have 1 year of 1h data. **Verify — may not need changes.**

#### 4. Prompt updates (LAYER_2_SYSTEM)

Update the backtester description to state the practical window:
- "Backtests cover approximately 90 days of trading history. The most recent 30 days have 5-minute precision for SL/TP checks; earlier data uses 1-hour precision."

**Important**: State this as fact, not guidance. Don't tell the orchestrator how to interpret trade counts — let it calibrate its own expectations from the stated window.

#### 5. Backtest timeout

Current: 60 seconds. At ~15s for 90 days, this has comfortable margin. No change needed. But monitor — if strategies get computationally heavy, the timeout may need adjustment later.

---

## Implementation Plan

### Phase 1: Prompt Fixes (all 13 findings)

All changes in `src/orchestrator/orchestrator.py`:

**CODE_GEN_SYSTEM** (M1, M2, M3, M4, L1, L3):
- Add `.side`, `.opened_at` to OpenPosition inline description
- Add `.side`, `.qty`, `.opened_at`, `.closed_at` to ClosedTrade inline description
- Add "Optional methods" section: `on_fill()`, `on_position_closed()`, `get_state()`/`load_state()`, `scan_interval_minutes`
- Add RiskLimits field list
- Add 30-second timeout note
- Clarify MODIFY size_pct behavior

**CODE_REVIEW_SYSTEM** (M5, M6, M7):
- Add ClosedTrade fields
- Add RiskLimits fields
- Add optional method signatures with callback fallback note

**LAYER_2_SYSTEM** (M8, M9, M10, L2):
- Fix scan_results description: "price and spread per symbol per scan"
- Add `promotion` to close_reason list
- Clarify candidate risk enforcement
- Add explicit backtest window description (90 days after Phase 2)

### Phase 2: Backtest Window Expansion

1. Read `_run_backtest()` in orchestrator.py to understand current data fetching
2. Modify candle data query to fetch 90 days of 1h data
3. Verify 1d data coverage
4. Verify backtester handles the 5m/1h boundary gracefully (it should — fallback exists)
5. Update LAYER_2_SYSTEM backtest description with 90-day window
6. Run tests

### Phase 3: Verify & Test

1. Run full test suite (230 tests)
2. Manual inspection: verify prompt text is accurate against contract
3. Deploy to VPS
4. Trigger manual orchestration cycle to validate

---

## Files to Modify

| File | Changes |
|------|---------|
| `src/orchestrator/orchestrator.py` | All prompt constant updates + backtest data fetching |
| `src/shell/config.py` | Only if new config needed (likely not) |
| `tests/test_integration.py` | Update any tests that assert on prompt content |
| `docs/dev_notes/progress.md` | Session Z entry |

## Additional Changes (Post-Audit)

### Backtest Window: 30 Days with Full 5m Precision
Original design doc proposed expanding to 90 days. Investigation revealed the code already fetched up to 365 days of 1h data. However, the 5m data (critical for accurate SL/TP trigger ordering) only covers 30 days. Running backtests beyond 30 days means the older portion has degraded SL/TP precision — hourly resolution can't determine whether SL or TP triggered first within an hour.

**Decision**: Keep backtest window at 30 days, aligned with 5m data availability. All three timeframes (5m, 1h, 1d) cover the same window. Full SL/TP precision throughout.

**Changes**: `_run_backtest()` limits: 1h `8760→720`, 1d `2555→30`. LAYER_2_SYSTEM updated.

### Strategy Characterization at Archive Time
When the orchestrator creates a candidate, it now provides a `strategy_characterization` — a brief description of the strategy's approach and target conditions. This is stored in `strategy_versions.description` and visible in the version history context for future cycles.

On promotion, the characterization carries forward from the candidate's original version record.

**Purpose**: Over time, the orchestrator accumulates a catalog of characterized strategies. Combined with the strategy document (institutional memory from reflection), this gives the orchestrator awareness of what approaches it has tried and in what conditions.

**Future**: When sufficient history exists, the architecture may expand to allow multi-step research (searching past strategies, comparing approaches) before making decisions. For now, the single-shot analysis pipeline is sufficient.

## Verified Correct (No Changes Needed)

These prompts were audited and found accurate:
- LAYER_1_IDENTITY (character traits — no factual claims)
- FUND_MANDATE
- ANALYSIS_CODE_GEN_SYSTEM (AnalysisBase, ReadOnlyDB, imports)
- ANALYSIS_REVIEW_SYSTEM (math checks, SQL safety)
- REFLECTION_USER_TEMPLATE (evidence categories, response format)
- ASK_SYSTEM_PROMPT (appropriate scope)
- BACKTEST_REVIEW_SYSTEM (after C1 fix)
- CODE_REVIEW_SYSTEM (after C2 fix)
