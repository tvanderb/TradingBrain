# Build Progress

## Sessions 1-2 (2026-02-06 to 2026-02-07) — v1 Build & Pivot to v2

> **Condensed**: Sessions 1-2 built the original three-brain architecture (v1), ran it briefly in paper mode, then pivoted to the IO-Container architecture (v2) after user proposed a fundamentally different design.

### v1 Summary (scrapped)
- Built full three-brain system: Executive, Analyst, Executor
- System ran in paper mode, confirmed Kraken WS/REST, Telegram, paper trading all worked
- Key finding: fees eat 27.7% of gross profit on a 2% BTC move — drove fee-aware design
- User feedback: `/report` should show existing calculations, not call Claude on demand

### Architecture Pivot
- User proposed scrapping three-brain for IO-Container (rigid shell + flexible strategy module)
- Full collaborative design discussion (see discussions.md)
- All v2 design decisions finalized: IO contract, tiered data, strategy evolution, skills library
- Started fresh on `v2-io-container` branch; v1 code preserved on main

### Technical Gotchas Discovered (still relevant)
- macOS Python 3.14 SSL: websockets needs `certifi.where()`
- python-telegram-bot: may need `--force-reinstall`
- APScheduler: `next_run_time=datetime.now()` for immediate first run
- Kraken actual fees: 0.25%/0.40% at $0 tier (not published 0.16%/0.26%)

## Session 3 (2026-02-07)

### Context
Continuing from session 2. All design finalized, notes comprehensive, user said "let it rip."

### Git Setup
- Committed v1 code on master branch (commit 2a6dbe5)
- Created `v2-io-container` branch
- Cleaned out v1 source code, preserved claude_notes/, config/, data/
- Created v2 directory structure per architecture.md

### Build Progress
- [x] Phase 1: Foundation (pyproject.toml, config, contracts, database, logging)
- [x] Phase 2: Shell (Kraken, risk, portfolio, data store)
- [x] Phase 3: Strategy (loader, sandbox, backtester, v001, skills)
- [x] Phase 4: Orchestrator (AI client, nightly cycle, reporter)
- [x] Phase 5: Telegram (bot, commands, notifications)
- [x] Phase 6: Main (scheduler, lifecycle, startup/shutdown)
- [x] Phase 7: Tests and verification — 18/18 passing

### Files Created (v2)
```
config/settings.toml          — Updated for v2 (AI provider, orchestrator, data tiering)
config/risk_limits.toml        — Updated (added rollback section)
pyproject.toml                 — Updated (v2.0.0, added websockets, certifi, anthropic[vertex])
.env.example                   — Updated (added Vertex comment)
.gitignore                     — Updated (added reports/, .claude/)

src/__init__.py
src/main.py                    — Full TradingBrain class with lifecycle, scheduler, scan loop
src/shell/__init__.py
src/shell/contract.py          — IO Contract: Signal, SymbolData, Portfolio, RiskLimits, StrategyBase
src/shell/config.py            — Config loading from TOML + .env
src/shell/database.py          — SQLite schema (11 tables), async Database class
src/shell/kraken.py            — Kraken REST + WebSocket v2 client
src/shell/risk.py              — Risk manager with rollback triggers
src/shell/portfolio.py         — Portfolio tracker (paper + live), P&L, daily snapshots
src/shell/data_store.py        — Tiered OHLCV storage, aggregation, pruning
src/strategy/__init__.py
src/strategy/loader.py         — Dynamic strategy import, archive, deploy
src/strategy/sandbox.py        — Strategy validation (syntax, imports, runtime)
src/strategy/backtester.py     — Historical backtesting with full metrics
src/orchestrator/__init__.py
src/orchestrator/ai_client.py  — Anthropic/Vertex abstraction, token tracking
src/orchestrator/orchestrator.py — Nightly cycle: analyze→generate→review→sandbox→deploy
src/orchestrator/reporter.py   — Daily/weekly reports, strategy performance metrics
src/telegram/__init__.py
src/telegram/bot.py            — Bot setup and lifecycle
src/telegram/commands.py       — 13 commands (/status, /positions, /report, /risk, etc.)
src/telegram/notifications.py  — Proactive alerts (trades, P&L, strategy changes, rollbacks)
src/utils/__init__.py
src/utils/logging.py           — Structured logging with structlog

strategy/active/strategy.py    — v001: EMA 9/21 + RSI 14 + Volume 1.2x
strategy/strategy_document.md  — Initial strategy document (7 sections)
strategy/skills/__init__.py
strategy/skills/indicators.py  — Reusable indicators (RSI, EMA, BB, MACD, ATR, vol ratio, regime)

tests/test_integration.py      — 18 tests covering all components
```

### Tests Passing (18/18)
- Config loading from TOML + env
- Database schema creation (11 tables)
- Database CRUD operations
- IO contract types (Signal, RiskLimits, etc.)
- Risk manager: basic checks, daily limits, consecutive losses, size clamping
- Strategy loading and initialization
- Strategy analyze() returns correct types
- Sandbox validates good strategies
- Sandbox rejects forbidden imports, eval, syntax errors
- Indicator computation (RSI, EMA, volume ratio, regime)
- Paper trade buy/sell cycle with P&L verification
- Paper trade fee deduction verification
- Kraken pair mapping (BTC/USD <-> XBTUSD)
- Backtester runs on synthetic data

### Key Findings
- Python 3.14.2 on macOS works with all deps
- aiosqlite executescript fails if leftover DB file exists with partial schema — always use fresh path
- Strategy sandbox catches forbidden imports via AST analysis before execution
- Paper trade slippage of 0.05% + taker fee 0.40% means ~0.45% cost per side

### Current Status
- All v2 code compiles and tests pass
- System ready to run in paper mode
- Need user's .env file (Kraken key, Anthropic key, Telegram token)
- System can start without API keys (will just fail on Kraken/AI calls gracefully)

## Session 4 (2026-02-07, continued)

### Context
Continuing from session 3. System was running in paper mode but had issues.

### Issues Found & Fixed
1. **Multiple instances running simultaneously**: 4 Python processes were running the trading brain at once (spawned during prior session's testing). This caused:
   - `telegram.error.Conflict: terminated by other getUpdates request` — multiple bots polling same token
   - Event loop starvation — only 4/18 expected scans completed in 1.5 hours (big gaps: 65 min)
   - Fix: `kill -9` all 4 PIDs, cleaned stale WAL/SHM database files

2. **Added PID lockfile safeguard** (`src/main.py`):
   - `data/brain.pid` lockfile written on startup, checked with `os.kill(pid, 0)`
   - Blocks second instance with clear error message
   - Auto-cleaned on exit via `atexit` + explicit cleanup in `finally`
   - Fixed `ProcessNotFoundError` → `ProcessLookupError` (correct Python exception name)
   - Also catches `PermissionError` for edge case where process exists but owned by another user

3. **Stale DB files**: brain.db-wal and brain.db-shm left by force-killed processes caused `sqlite3.OperationalError: disk I/O error` on next startup. Deleted them manually.

### Orchestrator Fixes
- Opus model ID updated: `claude-opus-4-5-20250514` → `claude-opus-4-6` (in config, defaults, cost table)
- Backtester wired into orchestrator pipeline (was completely missing — went straight from review to deploy)
- Fixed `paper_balance` → `paper_balance_usd` config field reference

### Statistics Shell Design (Major Feature — Not Yet Built)
- Full collaborative design discussion with user (see discussions.md)
- Three-layer input architecture: truth benchmarks (rigid) + statistics module (flexible) + user constraints (rigid)
- Statistics module: read-only DB access, orchestrator rewrites, Opus reviews for mathematical correctness
- Truth benchmarks: simple verifiable metrics the orchestrator can compare against its analysis
- Orchestrator self-awareness: all inputs labeled by category, explicit prioritized goals
- New data collection: scan_results table, regime tagging on trades/signals
- 8-step integration plan drafted (see roadmap.md Phase 0)
- All decisions recorded in decisions.md, architecture in architecture.md

### Current Status
- Single clean instance running (PID lockfile prevents duplicates)
- Scans completing on schedule every 5 minutes
- Telegram connected without conflicts
- Fee check confirmed: 0.25% maker / 0.40% taker
- Strategy generating 0 signals (expected — EMA crossover needs trend to form)
- **Next**: Build statistics shell (Phase 0) before first orchestration cycle

## Session 5 (2026-02-08)

### Context
Continuing from session 4. All design finalized (two-module statistics shell), committed. Building Phase 0 step by step.

### Phase 0 Progress: Statistics Shell Implementation

**Step 1: Database Schema Additions** — DONE (commit 672d54c)
- Added `scan_results` table with raw indicator values + strategy_regime
- Added `strategy_regime` column to `trades` and `signals` tables
- Migration runner for existing databases (ALTER TABLE if column missing)
- Indexes on `scan_results(timestamp)` and `(symbol, timestamp)`

**Step 2: Scan Results Collection** — DONE (commit 67d7c45)
- Scan loop writes indicator state to `scan_results` after every scan
- Signal INSERTs include `strategy_regime` (both rejected and acted)
- Trade INSERTs include `strategy_regime` via `execute_signal()` parameter
- Position monitor passes last-known regime on SL/TP-triggered closes
- `scan_results` updated with signal info after strategy runs

**Step 3: Truth Benchmarks** — DONE (commit f84d6c8)
- Created `src/shell/truth.py` — rigid shell, orchestrator CANNOT modify
- `compute_truth_benchmarks(db)` returns 17 metrics: trade counts, win rate, P&L, fees, expectancy, consecutive losses, drawdown, signal/scan activity, strategy version
- All calculations trivially verifiable (COUNT, SUM, simple ratios)
- Tests: seeded data verification + empty DB edge case

**Step 4: Analysis Module Infrastructure** — DONE (commit 2d4c9ad)
- `AnalysisBase` added to IO contract (`src/shell/contract.py`): `async analyze(db, schema) -> dict`
- `src/statistics/readonly_db.py` — ReadOnlyDB wrapper blocks INSERT/UPDATE/DELETE/DROP/ALTER/CREATE via regex
- `src/statistics/loader.py` — dynamic import for both modules, archive, deploy (same pattern as strategy loader)
- `src/statistics/sandbox.py` — validates code safety; allows scipy/statistics but blocks network/filesystem/subprocess
- `get_schema_description()` returns dict describing all tables and columns for modules
- Tests: ReadOnlyDB allows SELECT / blocks writes, sandbox valid/invalid/no-class/imports

**Step 5: Market Analysis Module** — DONE (commit d445650)
- `statistics/active/market_analysis.py` (v001, hand-written starting point)
- Computes: price summary per symbol (current, 24h change, 7d change, EMA alignment, RSI, volume ratio)
- Indicator distributions (24h): RSI overbought/oversold frequency, EMA bullish %, volume avg
- Signal proximity: EMA gap %, cross nearness, RSI distance to extremes
- Data quality: total scans, first/last scan, scans last hour vs expected

**Step 6: Trade Performance Module** — DONE (commit d445650)
- `statistics/active/trade_performance.py` (v001, hand-written starting point)
- Computes: performance by symbol (trades, wins, win rate, P&L, expectancy)
- Performance by strategy_regime
- Signal analysis (total, acted, rejected, act rate, top rejection reasons)
- Fee impact (total fees, fees as % of gross wins, round-trip fee %, break-even move)
- Holding duration (avg hours overall, winning, losing)
- Rolling metrics (7d, 30d)

**Step 7: Orchestrator Integration** — NOT YET STARTED
- Wire truth benchmarks + both analysis modules into orchestrator nightly cycle
- Labeled inputs, explicit goals, cross-referencing instructions
- Analysis module evolution pipeline (Sonnet generates, Opus reviews math, sandbox, deploy)

**Step 8: Analysis Module Evolution** — NOT YET STARTED
- Expand orchestrator decision options: MARKET_ANALYSIS_UPDATE, TRADE_ANALYSIS_UPDATE
- Independent module evolution

**Step 9: Tests** — Tests written alongside each step (29/29 passing)

### Design Decisions Made This Session
- **Cross-referencing**: Modules run independently, orchestrator cross-references (option 3). Neither module sees the other's output.
- **Orchestrator thought spool**: Added to roadmap — store full AI responses in browsable format (design TBD, before first orchestration cycle)

### Files Created This Session
```
src/shell/truth.py                      — Truth benchmarks (rigid shell)
src/statistics/__init__.py              — Statistics package init
src/statistics/readonly_db.py           — ReadOnlyDB wrapper + schema description
src/statistics/loader.py                — Analysis module loader/archiver/deployer
src/statistics/sandbox.py               — Analysis code validation
statistics/active/market_analysis.py    — Market analysis v001
statistics/active/trade_performance.py  — Trade performance v001
```

### Files Modified This Session
```
src/shell/database.py        — scan_results table, strategy_regime columns, migrations
src/shell/contract.py        — Added AnalysisBase to IO contract
src/shell/portfolio.py       — strategy_regime parameter threading
src/main.py                  — Scan results collection, regime tagging on signals/trades
tests/test_integration.py    — 11 new tests (29 total, was 18)
claude_notes/architecture.md — Doc fixes (async, filenames, schema alignment)
claude_notes/decisions.md    — Cross-referencing decision
claude_notes/roadmap.md      — Phase 0 updated for two modules, thought spool added
```

### Test Count: 29/29 passing
Original 18 + 11 new:
- test_truth_benchmarks, test_truth_benchmarks_empty_db
- test_readonly_db_allows_select, test_readonly_db_blocks_writes
- test_analysis_sandbox_valid, test_analysis_sandbox_rejects_forbidden
- test_analysis_sandbox_rejects_no_class, test_analysis_sandbox_allows_scipy
- test_market_analysis_module, test_trade_performance_module
- test_analysis_modules_empty_db

### Current Status
- Phase 0 Steps 1-6 complete, committed on v2-io-container branch
- Steps 7-8 (orchestrator integration + evolution) remaining
- System is NOT running (was killed for development)
- No data loss risk — all schema changes are additive (new table + new columns)

## Session 6 (2026-02-08)

### Context
Continuing from session 5. Implementing orchestrator thought spool — prerequisite before first end-to-end orchestration test.

### Orchestrator Thought Spool — DONE
- **Purpose**: Capture every AI response from the nightly cycle so user can browse what the orchestrator was thinking. Previously, Opus reasoning was parsed for JSON and the raw text was discarded.
- **Design**: DB table `orchestrator_thoughts` grouped by `cycle_id`, `_store_thought()` helper called after each AI call, Telegram `/thoughts` + `/thought` commands for browsing.

**Changes Made**:
1. `src/shell/database.py` — Added `orchestrator_thoughts` table + `idx_thoughts_cycle` index
2. `src/orchestrator/orchestrator.py` — Added `self._cycle_id`, `_store_thought()` helper, cycle_id generation at cycle start, instrumented 5 AI call sites (analysis, code_gen, code_review, analysis_gen, analysis_review)
3. `src/telegram/commands.py` — Added `cmd_thoughts` (cycle index/list/detail) and `cmd_thought` (full response with chunking for 4096 limit), updated `/start` help text
4. `src/telegram/bot.py` — Registered `thoughts` and `thought` handlers
5. `tests/test_integration.py` — Added `orchestrator_thoughts` to required tables, added `test_orchestrator_thoughts_table` test

### Test Count: 34/34 passing
Previous 33 + 1 new: test_orchestrator_thoughts_table

### Current Status
- Thought spool complete and tested
- Ready for orchestrator integration (Steps 7-8) or first end-to-end test

## Session 7 (2026-02-08, continued)

### Context
E2E orchestrator test run, system review, critical gap analysis, and extensive design discussion. No code changes — all design and documentation.

### E2E Orchestrator Test — SUCCESS
- Ran real Opus API call against seeded scan data (5 BTC, 3 ETH)
- Cost: $0.21, decision: NO_CHANGE (correct — ranging market, 0 trades)
- Thought spool captured 1 thought (3,456 chars), browsable
- Proved the orchestrator makes reasonable decisions when given good context

### System Critical Review
- Reviewed full system end-to-end, identified 12 categories of risks and gaps
- User responded to each with design direction (see discussions.md for full detail)
- Key decisions: no hard gates (trust aligned agent), VPS-only deployment, hedge fund analogy

### Orchestrator Prompt Design Framework — APPROVED
- Three-layer framework: Identity (WHO) / System Understanding (WHAT it works with) / Institutional Memory (WHAT it learned)
- Core philosophy: "Maximize awareness, minimize direction"
- Identity statements vs directive statements — framework prohibits directives
- Fund mandate replaces goals section: portfolio growth, capital preservation, avoid major drawdowns, long-term
- This is the governing design document for ALL orchestrator prompting

### Misalignment Audit
- Documented 12 specific items across codebase that violate the new framework
- ANALYSIS_SYSTEM prompt: heavily misaligned (full of directives, no identity layer)
- _analyze() user prompt: minor (editorial commentary, hardcoded values)
- Other 4 prompts (CODE_GEN, CODE_REVIEW, ANALYSIS_CODE_GEN, ANALYSIS_REVIEW): fine
- Strategy document: pre-loaded wisdom violates "earned not pre-loaded" principle

### Current Status
- All design documented in discussions.md (Sessions 7-8 sections)
- No code committed — all documentation and planning

## Session 8 (2026-02-09)

### Context
Continuing from session 7. Focused prompt audit, identity design, Layer 2 system map, then implementation of pending changes.

### Design Work Completed
- **Focused prompt audit**: Line-by-line audit of all 5 prompts + user prompt against framework
- **Layer classification correction**: Architecture awareness → Layer 2, not Layer 1
- **Fund mandate decided**: Portfolio growth with capital preservation, avoid major drawdowns, long-term
- **Layer 1 identity designed**: 6 dimensions — radical honesty (foundation), professional character, uncertainty, probabilistic thinking, relationship to change, long-term orientation
- **Layer 2 system map**: Full map of what orchestrator can/can't do, external processes, data received, consequences of decisions. Content deferred to post-implementation.
- **Separate prompt strings**: Layer 1 and Layer 2 will be separate constants in code
- **Dynamic config**: Risk limits must come from config, not hardcoded
- **Post-implementation to-do**: Write all prompt content AFTER implementation changes

### Implementation Commits

**Commit c9ae53e** — Infrastructure (items 1-3):
1. `src/shell/database.py` — Added `orchestrator_observations` table + index
2. `src/orchestrator/orchestrator.py` — Fixed backtester from 1h to 5m candles
3. `strategy/strategy_document.md` — Stripped to factual minimum (earned knowledge only)
4. `tests/test_integration.py` — Added observations table to required tables list

**Commit 7c424f2** — Orchestrator improvements (items 4-6):
1. `src/orchestrator/orchestrator.py` — Replaced `_update_strategy_doc()` with `_store_observation()` (writes to DB, not strategy doc)
2. `statistics/active/trade_performance.py` — Added `by_version` section (GROUP BY strategy_version with full metrics)
3. `src/orchestrator/orchestrator.py` — Added active paper test status + recent observations to `_gather_context()`

**Commit 87d93bc** — Drought detection + prompt cleanup (items 7, 10):
1. `src/orchestrator/orchestrator.py` — Signal drought detector in `_gather_context()` (last signal time, 7d/30d counts, 24h scans)
2. `src/orchestrator/orchestrator.py` — Dynamic config values in `_analyze()` prompt (fees from Kraken API, risk limits from config)
3. `src/orchestrator/orchestrator.py` — Removed editorial commentary from USER CONSTRAINTS
4. `src/orchestrator/orchestrator.py` — Removed explicit cross-reference instruction
5. `src/orchestrator/orchestrator.py` — Added drought, paper test, observations sections to user prompt

### Files Modified This Session
```
src/shell/database.py              — orchestrator_observations table + index
src/orchestrator/orchestrator.py   — _store_observation(), drought detector, dynamic config, paper test awareness, prompt cleanup
statistics/active/trade_performance.py — by_version performance section
strategy/strategy_document.md      — stripped to factual minimum
tests/test_integration.py          — observations table in required list
claude_notes/discussions.md        — extensive design documentation (Sessions 7-8)
```

### Test Count: 34/34 passing (unchanged)

**Commit (Session 9)** — Bootstrap, strategy evolution, rough fixes:
1. `src/main.py` — Historical data bootstrap (`_bootstrap_historical_data()`), WS failure callback, strategy_state pruning (keep last 10), top-level `compute_indicators` import
2. `src/orchestrator/orchestrator.py` — Tier 1 targeted edit gen prompt, diff context for Opus reviewer (difflib), parent_version lineage tracking, token budget check at cycle start
3. `src/shell/kraken.py` — `set_on_failure()` callback on KrakenWebSocket, fires on permanent failure
4. `src/telegram/notifications.py` — `websocket_failed()` alert method
5. `tests/test_integration.py` — `test_data_store_aggregation_5m_to_1h`, parent_version column assertion

## Session 10 (2026-02-09)

### Context
Full system audit before prompt writing. Pruned stale notes, then three-agent parallel audit of entire codebase.

### Notes Pruning
- decisions.md: Updated Strategy Failure, Performance Criteria, Orchestrator Goals to reflect fund mandate
- architecture.md: Fixed orchestrator goals section, nightly flow diagram (observations replaces strategy doc update)
- roadmap.md: Marked fixed issues, Phase 0 complete, updated success criteria with mandate note
- progress.md: Condensed Sessions 1-2 (v1) from ~110 lines to ~25 lines
- discussions.md: Removed v1 self-evolution levels, updated all "will be added" items as implemented, marked open items resolved

### System Audit — 21 Findings

#### Category 1: Core Functionality (6 items)
| # | Finding | File | Severity |
|---|---------|------|----------|
| 1 | Backtester short P&L inverted | backtester.py:202 | Critical |
| 2 | Kraken API assumes non-empty dicts | kraken.py:99,118 | Critical |
| 3 | Portfolio cash init = 0 in live mode | portfolio.py:56-63 | Critical |
| 4 | JSON parsing from LLM fragile (rfind) | orchestrator.py:553,673,807 | Critical |
| 5 | Data aggregation can lose candles (DELETE before INSERT) | data_store.py:130-140 | Critical |
| 6 | Daily start value resets on restart | portfolio.py:65 | Critical |

#### Category 2: Graceful Autonomy (8 items)
| # | Finding | File | Severity |
|---|---------|------|----------|
| 7 | Scan loop failures silent (no Telegram alert) | main.py:455-457 | Medium |
| 8 | Telegram disabled silently (no startup warning) | bot.py:24-26 | Medium |
| 9 | Notifier no retry on transient failure | notifications.py:31-38 | Medium |
| 10 | WebSocket callback failures swallowed | kraken.py:268-277 | Medium |
| 11 | AI client init failure continues silently | main.py:115-119 | Medium |
| 12 | Emergency stop doesn't verify fills | main.py:554-572 | Medium |
| 13 | Position monitor stale prices on WS failure | main.py:459-472 | Medium |
| 14 | Fee check uses only first symbol | main.py:501-502 | Medium |

#### Category 3: Robustness & Cleanliness (7 items)
| # | Finding | File | Severity |
|---|---------|------|----------|
| 15 | Intent enum crashes on bad DB data | portfolio.py:104,125 | Medium |
| 16 | Sandbox tmp_path not initialized | sandbox.py:143, stats sandbox.py:105 | Medium |
| 17 | ReadOnlyDB regex multi-statement bypass | readonly_db.py:17-20 | Medium |
| 18 | Token budget race condition | ai_client.py:84-86 | Low |
| 19 | No config validation | config.py:105-198 | Low |
| 20 | Backtester first-day inflation | backtester.py:243-247 | Low |
| 21 | Hardcoded slippage | portfolio.py:176,277 | Cosmetic |

### Audit Fixes Applied

All meaningful findings fixed. Items #1 (backtester shorts — only supports longs by design), #14 (fee check — volume-tier based, not pair-specific), #18 (token budget — low risk), #20 (first-day — correct behavior), #21 (slippage — cosmetic) were triaged as not actionable.

**Fixes by file:**

| File | Fix | Finding |
|------|-----|---------|
| `kraken.py` | Empty dict guard in get_ohlc/get_ticker | #2 |
| `kraken.py` | Separate WS callback errors from parse errors | #10 |
| `portfolio.py` | Live mode cash fallback (query Kraken balance) | #3 |
| `portfolio.py` | Daily start value from last daily snapshot | #6 |
| `portfolio.py` | `_safe_intent()` helper for enum parsing | #15 |
| `data_store.py` | DELETE only inside `if not empty` (both aggregation methods) | #5 |
| `orchestrator.py` | `_extract_json()` with brace-depth tracking (all 3 parse sites) | #4 |
| `main.py` | Scan failure → Telegram alert | #7 |
| `main.py` | AI init failure → log.error with context | #11 |
| `main.py` | Emergency stop → notify + verify fills | #12 |
| `main.py` | Position monitor → log warning instead of bare except | #13 |
| `sandbox.py` | `tmp_path = None` before try block | #16 |
| `readonly_db.py` | Multi-statement check (split on semicolons) | #17 |
| `config.py` | `_validate_config()` for critical ranges | #19 |
| `bot.py` | Telegram disabled → log.warning | #8 |
| `notifications.py` | One retry with 1s delay on send failure | #9 |
| `backtester.py` | `float()` wrap on sum results (flaky test fix) | bonus |
| `test_integration.py` | Multi-statement bypass test cases | bonus |

### Pre-Prompt Features Implemented (Session 10 continued)

All 4 design decisions from audit triage now implemented:

| Feature | Files Changed | Details |
|---------|--------------|---------|
| ~~`Action.SHORT`~~ | ~~`contract.py`, `backtester.py`~~ | **REMOVED** — Kraken blocks margin trading for Canadian residents. System is long-only. |
| Token budget 1.5M | `config.py`, `settings.toml` | `daily_token_limit` 150K → 1.5M (safety net only) |
| Slippage tolerance | `contract.py`, `config.py`, `settings.toml`, `portfolio.py` | `Signal.slippage_tolerance` field, `default_slippage_pct` config, portfolio uses signal/config fallback |
| 9 pairs + per-pair fees | `kraken.py`, `config.py`, `settings.toml`, `database.py`, `main.py`, `contract.py`, `backtester.py` | 9-pair PAIR_MAP with WS reverse, `symbol` column on fee_schedule, per-pair fee cache in TradingBrain, `SymbolData.maker_fee_pct/taker_fee_pct`, backtester per-pair fees |

Tests updated: config, contract types, pair mapping all extended. **35/35 passing.**

### Prompt Writing — Three-Layer Framework (Session 11 continued)

Replaced monolithic `ANALYSIS_SYSTEM` with three-layer framework:

| Constant | Layer | Content |
|----------|-------|---------|
| `LAYER_1_IDENTITY` | Identity (WHO) | 6 dimensions: honesty, judgment, uncertainty, probabilistic thinking, change, long-term |
| `FUND_MANDATE` | Mandate | "Portfolio growth with capital preservation. Avoid major drawdowns. This is a long-term fund." |
| `LAYER_2_SYSTEM` | System (WHAT) | Architecture, decisions/consequences, boundaries, processes, inputs, data, response format |

**Removed from prompts** (framework violations):
- All behavioral directives ("Be conservative", "Prefer NO_CHANGE")
- All numeric thresholds ("Minimum ~20 trades", "Profit factor > 1.2", "3x fees")
- All decision heuristics ("If you lack information, update analysis modules first")
- `strategy_doc_update` from response JSON (dead field)

**Updated `_analyze()` user prompt**:
- "USER CONSTRAINTS" → "SYSTEM CONSTRAINTS"
- Added: trading pairs, long-only, slippage
- All values dynamic from config
- No editorial commentary

**Code gen/review prompts updated**:
- `CODE_GEN_SYSTEM`: added fee fields, slippage, long-only constraint to contract description
- `CODE_REVIEW_SYSTEM`: added long-only compliance check
- Code gen user prompts: injected dynamic system constraints (fees, pairs, risk limits, slippage)

**Test updated**: `test_analysis_code_gen_prompts_exist` → checks new constants (identity, mandate, Layer 2 content)

### Current Status (after Session 11)
- Branch: v2-io-container
- Tests: 35/35 passing
- System NOT running (stopped for development)
- All audit fixes + pre-prompt features + prompt writing COMPLETE
- **Next**: End-to-end review and test

## Session 12 (2026-02-09) — Full System Audit

### Audit Scope
All 29 source files, 1 test file, 2 config files, active strategy/statistics modules, skills library, strategy document. Four parallel audit agents. 59 total findings.

### Critical Fixes (7/7)
- **C1**: Trade P&L now includes entry fee (portfolio.py + backtester.py) — was overstating profits by 0.25-0.40%
- **C2**: Net P&L no longer double-counts exit fees in daily snapshots
- **C3**: SELL/CLOSE always pass through ALL risk checks (was trapped in losing positions)
- **C4**: Strategy sandbox blocks `open()`
- **C5**: FORBIDDEN_ATTRS now checked via `ast.Attribute` visitor (os.system, os.popen)
- **C6**: Paper test lifecycle implemented (terminate + evaluate)
- **C7**: Peak portfolio loaded from DB on restart (drawdown protection continuity)

### Medium Fixes (18/20, 2 deferred)
M1-M16, M18-M19 fixed. M17 (stats sandbox test analyze) and M20 (orchestrator log completeness) deferred.

Key fixes: timezone-aware daily snapshots, API timeout, token budget persistence, code fence case-insensitive stripping, per-query error handling in gather_context, observation pruning, backtest result storage, live sell respects order_type, on_fill called for exits, message chunking.

### Low Fixes (10/14)
- L3: Scan interval updated after strategy hot-swap
- L4: Version strings include seconds (prevent collision)
- L6: fee_schedule pruned after 90 days
- L7: Scan results use UTC timestamps
- L10: Removed duplicate _release_lock registration
- L11: Backtest module cleaned from sys.modules
- L12: ReadOnlyDB strips SQL comments before checking write patterns
- L14: _run_backtest tmp_path safe from UnboundLocalError
- L1, L2, L13: Fixed in batch 2

Skipped: L5 (overcounted inserts — logging only), L9 (duplicate observations — bounded by pruning)
False positive: L8 (get_candles not dead code)

### Cosmetic Fixes (2/5)
- X1: ReadOnlyDB schema reflects long-only system
- X5: Removed dead short-side P&L ternary

### Tests
41/41 passing. 6 new tests added for critical fixes.

### Batch 4: Previously-Deferred + Remaining Fixes
- M17: Stats sandbox now test-runs analyze() against in-memory DB
- M20: orchestrator_log stores version_from, deployed_version, tokens_used, cost_usd
- L5: store_candles returns actual insert count via cursor.rowcount
- L9: orchestrator_observations has UNIQUE(date, cycle_id), uses INSERT OR REPLACE
- X2: Renamed default_slippage_pct → default_slippage_factor
- X3: JSON_LOGS env var toggles JSONRenderer for VPS
- X4: numpy import moved to top-level in backtester

### Batch 5: Test Coverage (11 new tests)
- T1: Nightly orchestration cycle (2 tests: NO_CHANGE + insufficient budget)
- T2: Strategy deploy/archive/rollback (1 test)
- T3: Paper test pipeline with evaluation (1 test)
- T4: Telegram commands (2 tests: 10+ commands + authorization)
- T5: Graceful shutdown (2 tests: risk lifecycle + double close)
- T7: Strategy state round-trip (1 test)
- T11: Scan loop flow (2 tests: strategy run + DB persistence)

### Batch 6: End-to-End Review (Session 13)
Full system review with 4 parallel agents examining all 29 source files + 3 active modules.

**Verified issues fixed (4):**
1. reporter.py: `profit_factor` returned `float("inf")` → now returns `None` (JSON-safe)
2. orchestrator.py: Paper test results (`_evaluate_paper_tests()`) now passed into Opus context via `completed_paper_tests` key
3. ai_client.py: Added exponential backoff retry (3 attempts, 1s/2s/4s) for transient API errors (timeout, rate limit, 5xx)
4. main.py: Strategy hash initialized on startup → prevents unnecessary strategy reload on first nightly cycle

**False positives dismissed (7):** Markdown stripping (correct), cash going negative (guarded), Signal mutation (not frozen), self._ai null (always created), delete-before-commit (commits inside store_candles), daily token reset (called at midnight), zero-trade paper test pass (deliberate design).

### Current Status
- Branch: v2-io-container
- Tests: **52/52 passing**
- **ALL audit findings RESOLVED** + **end-to-end review complete**
- System ready for first real run

---

## Session 14 (2026-02-09) — Cleanup, Docker, Deployment Docs

### Cleanup
- Deleted `build/` (old v1 build artifacts)
- Deleted `package.json`, `pnpm-lock.yaml`, `node_modules/` (Node.js dev tooling)
- Cleaned runtime artifacts: `brain.pid`, `brain.db-shm`, `brain.db-wal`

### Directory Reorganization
- Moved `claude_notes/` → `docs/dev_notes/` (preserves history context)
- Created `docs/DEPLOY.md` — admin deployment guide
- Updated all `claude_notes/` references in CLAUDE.md and MEMORY.md

### Docker
- Created `Dockerfile` — `python:3.12-slim`, minimal deps, single entrypoint
- Created `docker-compose.yml` — volume mounts for data, config, strategy, statistics
- Created `.dockerignore` — excludes dev files from build context

### .gitignore Updates
- Removed `build/`, `package.json`, `pnpm-lock.yaml` entries (files deleted)
- Added runtime artifact patterns (`brain.pid`, `*.db-shm`, `*.db-wal`)

### Verification
- **52/52 tests passing** (unchanged)
- Docker not installed on dev machine (VPS-only) — files validated structurally
