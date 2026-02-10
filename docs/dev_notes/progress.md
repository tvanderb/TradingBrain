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

---

## Session 14 (cont.) — Data API Implementation

### Design Discussion
- User asked about programmatic observability API
- Shifted from dashboard-specific to general-purpose data access for any external software
- Decisions: full filtering, response envelopes (with mode), read-only, no candle data, single WebSocket event stream

### Phase 1: Event System Refactor
- **Notifier rewritten** with dual dispatch: WebSocket (always) + Telegram (filtered by config)
- 19 event methods covering: trades, risk, scans, strategy, orchestrator, system
- **Bug fix**: `strategy_change()` existed but was never called — wired into orchestrator
- **Bug fix**: risk halts were silent — wired notifications at call sites in main.py
- `NotificationConfig` added to config with per-event Telegram filtering

### Phase 2+3: REST API + WebSocket
- 11 REST endpoints: system, portfolio, positions, trades, performance, risk, market, signals, strategy, ai/usage, benchmarks
- Response envelope format: `{data: ..., meta: {timestamp, mode, version}}`
- Bearer token auth via middleware (API_KEY from .env)
- WebSocket event stream at `/v1/events` (token auth via query param)
- aiohttp server integrated into main.py lifecycle (startup/shutdown)

### Phase 4: Config, Docker, Tests
- Added `[telegram.notifications]` and `[api]` sections to settings.toml
- Updated docker-compose.yml with port mapping
- 6 new tests added

### Files Created
- `src/api/__init__.py` — typed AppKey definitions
- `src/api/server.py` — aiohttp app with auth middleware
- `src/api/routes.py` — 11 endpoint handlers
- `src/api/websocket.py` — WebSocketManager with broadcast

### Files Modified
- `src/telegram/notifications.py` — rewritten with dual dispatch
- `src/shell/config.py` — NotificationConfig + ApiConfig
- `src/main.py` — API server lifecycle, event wiring
- `src/orchestrator/orchestrator.py` — notifier integration
- `src/telegram/commands.py` — risk_resumed notification
- `pyproject.toml` — added aiohttp>=3.9

### Current Status
- Branch: v2-io-container
- Tests: **58/58 passing**
- Data API fully implemented and committed

---

## Session 15 (2026-02-09) — Full System Audit

### Audit Scope
End-to-end audit focused on alignment with goal (performant, growing, learning crypto hedge fund doing day/swing/hold trades), oversights, and bugs. Four parallel audits: core trading loop, strategy/orchestrator, config/operations, test coverage.

### Critical (5)

1. **Notifier passed to Orchestrator as None** — `main.py:133-136`
   - Orchestrator created before Notifier initialized. All orchestrator notifications silently fail.

2. **All CronTrigger jobs ignore configured timezone** — `main.py:236-257`
   - Config loads `timezone = "US/Eastern"` but never passed to scheduler or CronTrigger.
   - On UTC VPS: daily reset fires 4-5 hours early, orchestrator runs at wrong hour.

3. **Scan loop blocks ALL signals during halt, including exits** — `main.py:309-310`
   - `if self._risk.is_halted: return` short-circuits before strategy runs.
   - Risk manager allows SELL/CLOSE during halt (risk.py:95-96), but scan_loop never reaches it.
   - Strategy-driven exits blocked during halt. Only SL/TP from position monitor works.

4. **`strategy_version` always None in trade records** — `portfolio.py:345`
   - Trade INSERT hardcodes `None` for `strategy_version`. Portfolio has no version reference.
   - Compounds with #7: paper test queries trades by version, finds 0, auto-passes.

5. **Live mode: order fills assumed immediate, no confirmation** — `portfolio.py:216-227`
   - Cash deducted immediately at assumed price. No fill-confirmation or order-status polling.
   - Market order fills differ by spread+slippage. Limit orders may not fill at all.

### Medium (10)

6. **`end_hour` config loaded but never enforced** — `config.py:82`
7. **Paper test evaluation: 0 trades = pass** — `orchestrator.py:979` (compounds with #4)
8. **Active strategy only generates DAY trades** — `strategy.py:117,130`
9. **`min_profit_fee_ratio` config loaded but never used** — dead config
10. **No credential validation at startup for live mode** — `main.py:71-196`
11. **Backtest doesn't use live fee schedule** — hardcoded 0.25%/0.40%
12. **Observations truncated to 2000 chars** — `orchestrator.py:1233-1240`
13. **No correlation analysis between symbols** — per-symbol only
14. **Position monitor can trigger both SL and TP on same update** — `portfolio.py:384-389`
15. **WebSocket fire-and-forget at startup** — `main.py:199`

### Low (7)

16. Position stale prices between scans
17. Clamped signals not recorded in audit trail
18. Emergency stop doesn't retry failed closes
19. Fee check race with config update
20. Bootstrap infinite loop edge case
21. Lockfile int parse not wrapped in try/except
22. Daily snapshot at 23:55 may miss last 5 min of trades

### Goal Alignment Gaps (6)

- No swing/position trade lifecycle enforcement
- No trailing stops
- No position aging awareness
- Analysis modules don't cross-reference market + trade data
- No time-of-day analysis
- Backtest assumes perfect fills (no slippage simulation)

### Session 15 — Fixes Applied (Pre-Audit)

**Critical fixes (5/5)**:
- C1: Moved orchestrator creation after notifier init (was receiving None)
- C2: Added timezone to AsyncIOScheduler constructor
- C3: Scan loop continues during halt (no early return), risk_check filters entries
- C4: Strategy version threaded through portfolio → positions → trades
- C5: Added live mode credential validation at startup

**Medium fixes (10/10)**:
- M6: Orchestrator timeout enforcement (asyncio.wait_for based on window hours)
- M7: Paper test 0-trade = "inconclusive" (not pass)
- M9: Removed dead min_profit_fee_ratio config
- M10: Credential validation for live mode
- M11: Backtester uses per-pair fees from DB + slippage_factor parameter
- M12: Observations truncation increased 2000→5000 chars
- M14: SL/TP double trigger fixed (elif instead of if)
- M15: WS task stored with done callback for error logging

**Low fixes (3/7 — 4 skipped as marginal)**:
- L18: Emergency stop retry (3 attempts per position)
- L21: Lockfile int parse wrapped in try/except for corrupt files
- L22: Daily snapshot moved from 23:55 to 23:59

**Goal alignment additions**:
- Backtester: slippage simulation on all fills (buy higher, sell lower)
- Backtester: entry fee cash accounting fix (was double-counted on sell)
- trade_performance.py: position aging (open positions with time buckets)
- trade_performance.py: cross-reference (regime×symbol, by intent type)
- trade_performance.py: time-of-day analysis (win rate/P&L by hour)

### Session 15 — Post-Fix Audit Results

**58/58 tests passing.**

**Critical (5)**:
1. `entry_fee` not persisted to positions DB — lost on restart → P&L overstated
2. `strategy_version` not persisted to positions DB — SL/TP closes lose attribution
3. Sandbox bypass: `import sys; sys.modules['os']` — sys not in FORBIDDEN_IMPORTS
4. Sandbox bypass: `import builtins; builtins.__import__('os')` — builtins not blocked
5. API route calls async `get_daily_usage()` without await — returns coroutine object

**Medium (12)**:
1. Position monitor skips strategy.on_fill()/on_position_closed() after SL/TP
2. Position monitor skips rollback trigger check + portfolio peak update
3. Backtester doesn't enforce max_positions
4. Backtester doesn't clamp size_pct to max_trade_pct
5. Backtester SL/TP uses close price, not intrabar high/low
6. Backtester candles_1h/1d receive raw 5m data
7. Notification event name mismatch: websocket_failed → "websocket_feed_lost"
8. Notification event name mismatch: rollback_alert → "strategy_rollback"
9. Old strategy version retired_at never set
10. Paper test evaluation doesn't filter trades by time window
11. Telegram bot open to ALL users when allowed_user_ids=[] (default)
12. ReadOnlyDB CTE bypass with `WITH ... INSERT`

**Low (8)**: reset_daily gaps, API auth when key not set, naive datetime, breakeven=loss, thoughts pruning, partial sell entry_fee, signal handler race, scan results for rejected signals

**Test Coverage Gaps (3 critical)**: emergency stop flow, position monitor pipeline, orchestrator deploy pipeline

### Session 15 — All Audit Findings Fixed

**58/58 tests passing.**

**All 5 Critical — FIXED:**
1. `entry_fee` + `strategy_version` added to positions DB schema + migrations + INSERT/UPDATE statements
2. Sandbox: `sys`, `builtins`, `ctypes`, `importlib`, `types`, `code`, `codeop`, `runpy`, `pkgutil` added to FORBIDDEN_IMPORTS
3. API route: `await` added to `ai.get_daily_usage()` (test mock updated to AsyncMock)

**All 12 Medium — FIXED:**
1-2. Position monitor: added strategy callbacks (on_fill, on_position_closed) + rollback triggers + peak update after SL/TP
3-4. Backtester: max_positions enforcement + size_pct clamped to max_trade_pct
5. Backtester: SL/TP now uses intrabar high/low instead of close price
6. Backtester: candles_1h/1d properly resampled from 5m data
7-8. NotificationConfig field names renamed to match dispatch event names (strategy_rollback, websocket_feed_lost)
9. Orchestrator: old strategy version retired_at set before deploying new version
10. Paper test evaluation: trades filtered by started_at/ends_at time window
11. Telegram: empty allowed_user_ids now rejects all users (was: allows all)
12. ReadOnlyDB: CTE bypass blocked with _CTE_WRITE_PATTERN regex

**5 of 8 Low — FIXED:**
1. Breakeven=loss: `pnl <= 0` changed to `pnl < 0` in truth.py and portfolio.py snapshot
2. Thoughts pruning: orchestrator_thoughts pruned to 30 days alongside observations
3. API default bind: changed from 0.0.0.0 to 127.0.0.1
4. Signal handler race: stored task ref, prevents double-stop on rapid Ctrl+C
5. Scan results: only acted-upon signals update scan_results (not rejected ones)

**3 Low — SKIPPED (not actionable):**
- reset_daily gaps: snapshot at 23:59 + reset at 00:00 is acceptable (1min gap)
- naive datetime: APScheduler handles timezone conversion internally
- partial sell entry_fee: already fixed in critical fixes (entry_fee persisted to DB)

---

## Session 16 (2026-02-09) — Final End-to-End Audit

### Audit Scope
Full codebase audit (all Python source files) focused on bugs, error handling, and alignment with autonomous crypto hedge fund goal. Five parallel agents: core trading loop, orchestrator/strategy, shell infrastructure, API/notifications, test coverage.

### Critical (14)

| # | File | Finding |
|---|------|---------|
| C1 | `main.py:221` | Kill switch cleared after failed emergency stop — `kill_requested` = False even if positions couldn't be closed |
| C2 | `main.py:411` | Strategy `analyze()` has no timeout — AI-rewritten strategy could infinite loop, blocking scan loop permanently |
| C3 | `portfolio.py:250-258` | SL/TP silently overwritten on position averaging — adding to position replaces stop_loss/take_profit; `None` removes protection |
| C4 | `portfolio.py:89-92` | Stale prices at startup — `total_value()` uses last DB price; overnight moves give wrong drawdown baseline |
| C5 | `main.py + risk.py` | Minimum trade size never enforced — `fees.min_trade_usd` ($20) in config but never checked |
| C6 | `routes.py:73-75` | Portfolio handler crashes — `port.positions.values()` on `list[OpenPosition]`; test masks with MagicMock |
| C7 | `kraken.py:259-265` | WebSocket subscribes with REST pair format — `XBTUSD` sent but WS v2 needs `XBT/USD` |
| C8 | `kraken.py:93` | Nonce collision under concurrent calls — `int(time.time() * 1000)` repeats within same ms during emergency stop |
| C9 | `server.py:25-49` | Empty API_KEY disables all authentication — no warning, all endpoints fully open |
| C10 | `routes.py + server.py` | No global error handler — unhandled exceptions return raw tracebacks to clients |
| C11 | `sandbox.py:32-38` | Sandbox misses dangerous modules — `threading`, `multiprocessing`, `pickle`, `io`, `tempfile`, `gc`, `inspect`, `atexit`, `signal` not blocked |
| C12 | `orchestrator.py:1159-1250` | No timeout on backtest execution — AI-generated strategy runs in-process with no time limit |
| C13 | `orchestrator.py:1288-1289` | version_from query returns new version — `WHERE retired_at IS NULL` finds newly deployed version after retire |
| C14 | `routes.py:313 + commands.py:312` | Exception messages leak internals — `str(e)` in API responses and Telegram `/ask` |

### Medium (29)

| # | File | Finding |
|---|------|---------|
| M1 | `main.py:366-369` | 5m candle fallback for empty 1h/1d — strategy gets wrong timeframe semantics |
| M2 | `main.py:567` | Position monitor PnL default of 0 — resets consecutive losses counter |
| M3 | `risk.py:69-72` | Consecutive losses carry across days — multi-day accumulation triggers rollback |
| M4 | `risk.py:82-83` | Peak portfolio only in memory — lost on crash, weakens drawdown protection |
| M5 | `portfolio.py:293` | Partial SELL uses portfolio value % not position value % |
| M6 | `portfolio.py:234` | Mixed timezones — Python inserts local time, SQLite defaults UTC |
| M7 | `orchestrator.py:371` | Token budget threshold 5000 too low — cycle needs 50K+ |
| M8 | `backtester.py` | Backtester doesn't simulate daily loss halt or drawdown halt |
| M9 | `orchestrator.py:391-401` | Unknown decision type treated as strategy change |
| M10 | `orchestrator.py:843` | Risk tier IndexError if AI returns tier > 3 |
| M11 | `orchestrator.py:989` | Paper test pass/fail too simplistic — 1 trade $0.01 profit passes |
| M12 | `orchestrator.py:423` | Cycle failure swallows traceback — no `traceback.format_exc()` |
| M13 | `orchestrator.py` | LAYER_2_SYSTEM prompt missing `max_position_pct` constraint |
| M14 | `database.py:277` | No query timeout — expensive SELECT blocks event loop |
| M15 | `kraken.py:80-87` | No Kraken REST rate limiter — 18+ calls per scan |
| M16 | `kraken.py:85-87` | Kraken error is a list, displayed raw |
| M17 | `config.py:250-270` | Config validation incomplete — port, hours, balance, slippage unchecked |
| M18 | `truth.py:86-95` | Max drawdown treats None portfolio_value as 0 — 100% drawdown spike |
| M19 | `commands.py:159` | Telegram `/trades` crashes on None pnl — TypeError on format |
| M20 | `websocket.py:25-36` | No WS backpressure or client limit — connection flooding possible |
| M21 | `websocket.py:38-59` | No WS heartbeat/ping-pong — zombie connections accumulate |
| M22 | `websocket.py:41` | WS auth token in URL — logged by proxies |
| M23 | `notifications.py:51` | Only 1 retry on Telegram failure — critical alerts lost |
| M24 | `routes.py` | No API rate limiting |
| M25 | `routes.py:120,230` | `int()` parsing crash — `?limit=abc` returns 500 |
| M26 | `routes.py:178` | Accesses private `risk._peak_portfolio` |
| M27 | `backtester.py:290-296` | Daily value tracking stale, misses final day |
| M28 | `orchestrator.py:800-808` | Markdown fence stripping fragile |
| M29 | `backtester.py` | Backtester doesn't enforce `max_position_pct` (not in RiskLimits) |

### Low (22)

L1-L22: Comment numbering, dead code, dust threshold, live fill TODO, dead clamp, fragile variable dependency, unhalt doesn't reset daily_pnl, unrealistic sample data, deploy_strategy no validation, Sharpe gaps, redundant commit, WS retry count, telegram truncation, SL/TP shows $0.00, empty env override, redundant SQL, API key not constant-time, strategy readable via Telegram, getattr defaults True for unknown events, no pagination metadata, no CORS, misleading regime labels.

### Test Coverage Gaps (7 critical paths)

| # | Area | Description |
|---|------|-------------|
| T1 | Emergency stop | `_emergency_stop()` + kill switch poll loop: zero coverage |
| T2 | Position monitor | `_position_monitor()` SL/TP trigger flow: zero coverage |
| T3 | Live Kraken | `place_order()`, `cancel_order()`, `_sign()`: zero coverage |
| T4 | Orchestrator change | `_execute_change()` pipeline: zero coverage |
| T5 | DB migrations | `_run_migrations()` on existing schema: zero coverage |
| T6 | WS reconnection | Connect/reconnect loop + message parsing: zero coverage |
| T7 | Brain lifecycle | `start()` and `stop()` sequences: zero coverage |

### Triage Notes

**Not actionable / by design:**
- M3: Consecutive losses across days — intentional (default threshold 999 disables it)
- M4: Peak portfolio memory-only — already persisted via daily snapshots, gap is crash-only
- M5: SELL size_pct relative to portfolio — consistent with BUY behavior, by design
- M6: Mixed timezones — known from Session 15, all internal usage is consistent
- M8: Backtester no risk halt simulation — backtester shows raw strategy performance
- M11: Paper test simplistic — already flagged Session 15, deliberate (orchestrator learns)
- M22: WS token in URL — inherent to WebSocket auth (no header support in browsers)
- M29: max_position_pct not in RiskLimits — shell enforces it; strategy doesn't need to know
- L1-L22: Cosmetic/low-risk, fix opportunistically

**Will fix (14 critical + ~15 medium):**
- All 14 criticals
- M1, M2, M7, M9, M10, M12, M13, M14, M15, M16, M17, M18, M19, M20, M21, M23, M24, M25, M26, M27, M28

### Critical Fixes Applied (14/14)

| ID | Fix | File(s) |
|----|-----|---------|
| C1 | Kill switch only clears after successful emergency stop | `main.py` |
| C2 | Strategy analyze() wrapped in 30s timeout via run_in_executor | `main.py` |
| C3 | Position averaging preserves existing SL/TP when new signal has None | `portfolio.py` |
| C4 | Startup refreshes prices from Kraken before setting portfolio peak | `main.py` |
| C5 | Minimum trade size ($20) enforced before executing buy | `portfolio.py` |
| C6 | Fixed portfolio_handler crash (iterated dict as list) | `routes.py` |
| C7 | WebSocket subscription uses WS v2 pair format (XBT/USD, XDG/USD) | `kraken.py` |
| C8 | Monotonic nonce counter prevents API collisions | `kraken.py` |
| C9 | Empty API_KEY rejects all requests; constant-time comparison | `server.py`, `websocket.py` |
| C10 | Global error middleware catches unhandled exceptions | `server.py` |
| C11 | 12 additional modules added to FORBIDDEN_IMPORTS | `sandbox.py` |
| C12 | Backtest wrapped in 60s timeout | `orchestrator.py` |
| C13 | version_from uses parent_version for lineage tracking | `orchestrator.py` |
| C14 | Exception messages replaced with generic text in API/Telegram | `routes.py`, `commands.py` |

### Medium Fixes Applied (19/29, 10 triaged as not actionable)

| ID | Fix | File(s) |
|----|-----|---------|
| M1 | Added warning log when using 5m candles as 1h/1d fallback | `main.py` |
| M2 | Position monitor only records PnL when not None (was defaulting to 0) | `main.py` |
| M7 | Token budget threshold increased from 5K to 50K | `orchestrator.py` |
| M9 | Unknown decision types treated as NO_CHANGE (was: strategy change) | `orchestrator.py` |
| M10 | Risk tier clamped to 1-3 at source + from review | `orchestrator.py` |
| M12 | Cycle failure logs full traceback via exc_info=True | `orchestrator.py` |
| M13 | Already present — max_position_pct in dynamic prompt at line 668 | N/A |
| M16 | Kraken error list joined with semicolons | `kraken.py` |
| M17 | Config validation: paper_balance, slippage, API port bounds | `config.py` |
| M18 | None portfolio_value skipped in drawdown calc (was treated as 0) | `truth.py` |
| M19 | /trades handles None pnl/pnl_pct/fees safely | `commands.py` |
| M20 | WebSocket max 50 clients, rejects with 503 above limit | `websocket.py` |
| M21 | WebSocket heartbeat=30s enables automatic ping/pong | `websocket.py` |
| M23 | Telegram retry increased to 3 attempts with exponential backoff | `notifications.py` |
| M25 | Safe int parsing for query params (no 500 on ?limit=abc) | `routes.py` |
| M26 | Added peak_portfolio property, removed private attr access | `risk.py`, `routes.py` |
| M27 | Backtester captures final day's value in daily_values | `backtester.py` |
| M28 | Markdown fence stripping uses regex (handles edge cases) | `orchestrator.py` |
| M15 | Kraken REST rate limiter (~3 calls/sec with async lock) | `kraken.py` |

**Not actionable / by design (10):** M3 (consecutive losses across days — intentional, threshold 999), M4 (peak memory-only — persisted via daily snapshots), M5 (SELL size_pct relative to portfolio — consistent with BUY), M6 (mixed timezones — internal usage consistent), M8 (backtester no risk halt — shows raw performance), M11 (paper test simplistic — deliberate), M22 (WS token in URL — inherent to WebSocket auth), M29 (max_position_pct not in RiskLimits — shell enforces it)

**Deferred (2):** M14 (query timeout — aiosqlite runs in thread, doesn't block event loop), M24 (API rate limiting — single-user auth'd API, low priority)

### Test Updates
- API server test: auth headers added (Bearer test-key)
- WebSocket test: token query param added (?token=test-ws-key)
- Budget test comment updated (5000 → 50000)

### Final Audit Pass (Round 2)

Second full audit with 5 agents across all 21 source files. ~70 raw findings, triaged down to 9 real issues.

**Fixes applied:**

| ID | Fix | File(s) |
|----|-----|---------|
| F1 | deployed_version extraction moved back to strategy branch (regression from M9) | `orchestrator.py` |
| F2 | Position monitor alerts + returns early when no prices and positions open | `main.py` |
| F3 | Error middleware envelope now includes `meta` for consistency | `server.py` |
| F4 | Breakeven trades (pnl=0) no longer counted as losses | `reporter.py`, `backtester.py` |
| F5 | Removed global config mutation in fee update | `main.py` |
| F6 | WebSocket broadcast logs when clients are dropped | `websocket.py` |

**User-requested change:** Removed min_trade_usd ($20) enforcement from portfolio.py.

### Final Audit Pass (Round 3)

Third full audit with 3 agents. After triage, 3 real bugs found and fixed:

| ID | Fix | File(s) |
|----|-----|---------|
| B1 | Daily snapshot fees_total used stale in-memory counter → now uses DB-derived `fees_from_trades` | `portfolio.py` |
| B2 | Zero price from failed ticker fetch caused ZeroDivisionError → now skips signal with warning | `main.py` |
| B3 | Paper test trade filter excluded trades closing after test window → now counts all closed trades from test period | `orchestrator.py` |

**False positives dismissed (7+):** WebSocket/fee/risk "race conditions" (asyncio is single-threaded), data_store param ordering (verified correct), ReadOnlyDB newline bypass (`\s*` catches newlines), various "silent failures" that are by-design graceful degradation.

### Session 16 (cont.) — Cold Start Testing & Deployment Prep

#### README
- Created `README.md` with architecture diagram, features, quick start, project structure, Telegram commands, API docs, risk model
- Verification found 3 inaccuracies: truth benchmarks 17→21, commands 14→15, event types 19→18. Fixed.
- Rewrote intro — old version said orchestrator "rewrites the strategy nightly" (misleading). New version accurately describes the decide-then-maybe-change flow.

#### Cold Start Paper Test
- Wiped DB, started system from scratch
- Discovered `scan.candle_fallback` warnings: bootstrap only fetched 5m candles, but scan loop substituted 5m data into `candles_1h` and `candles_1d` fields when those were empty. Strategy received wrong-timeframe data silently.
- **Fix**: Bootstrap now fetches all three timeframes from Kraken REST:
  - 5m: 30 days (~721 candles per symbol)
  - 1h: 1 year (~721 candles per symbol)
  - 1d: 1 year (~365 candles per symbol)
- Zero fallback warnings after fix — strategy gets real data from minute one.

#### Orchestrator Failure Alerting
- Found gap: `run_nightly_cycle()` caught exceptions and returned error string (swallowed). Caller in `main.py` sent it as a `daily_summary` — confusing, not a proper alert.
- **Fix**: Orchestrator now sends `system_error` via Telegram/WebSocket before re-raising. Caller catches but doesn't double-notify.

#### Deployment Prep
- Enabled API by default (`api.enabled = true`)
- Added `API_KEY` to `.env`
- Docker log persistence: `json-file` driver, 50MB max size, 5 rotated files
- Removed dead `min_trade_usd` from config dataclass, loader, and TOML
- User updated config: paper_balance_usd 200→100, timezone US/Eastern→America/New_York, sonnet model updated

#### Config Changes (user-applied)
- `paper_balance_usd`: 200.0 → 100.0
- `timezone`: "US/Eastern" → "America/New_York"
- `sonnet_model`: "claude-sonnet-4-5-20250929" → "claude-sonnet-4-5"

### Final State
- **Tests: 58/58 passing**
- **3 audit rounds + cold start test completed**
- **Merged to master**
- **Ready for VPS deployment**

## Session 16 (cont.) — Ansible Deployment & VPS Setup

### Ansible Deployment Created

Built a two-playbook Ansible deployment system in `deploy/`:

**`deploy/setup.yml`** — VPS Setup & Hardening (run once on fresh box):
- Creates `trading` deploy user with passwordless sudo
- Generates ed25519 SSH key pair locally at `deploy/keys/trading-brain`
- Hardens sshd: disables root login, password auth, limits auth tries
- Configures UFW firewall: deny all incoming, allow SSH (22) + API (80/443/8080)
- Creates 2GB swap file
- Supports both Debian/Ubuntu and Arch Linux

**`deploy/playbook.yml`** — Application Deployment (idempotent, re-runnable):
- Installs Docker via official repos (Debian or Arch)
- Creates `/srv/trading-brain/` directory structure
- Syncs project files via rsync: `src/`, `strategy/`, `statistics/`, `config/`, build files
- Renders `.env` from Jinja2 template with secrets from inventory
- Builds and starts container via `docker compose`
- Caddy reverse proxy: installed as system service, proxies port 80 → localhost:8080
- Handler-based restarts: only restarts container when relevant files change
- Tags: `setup`, `sync`, `secrets`, `build`, `start`, `verify`, `caddy`

**Supporting files:**
- `deploy/ansible.cfg` — SSH pipelining, host key checking off
- `deploy/inventory.yml.example` — Template with connection details + secret placeholders
- `deploy/templates/env.j2` — Jinja2 `.env` template
- `deploy/templates/Caddyfile.j2` — Reverse proxy config (swap `:80` for domain to get auto-HTTPS)

### VPS Deployed

- **Target**: Debian 13 (trixie), 2 vCPU, 2GB RAM, at 178.156.216.93
- VPS setup completed: user created, SSH keys, firewall, swap
- Application deployed: Docker installed, container built and running
- Caddy reverse proxy active on port 80
- API verified: `curl http://178.156.216.93/v1/system` returns system status
- Bootstrap completed: all 9 symbols × 3 timeframes (5m, 1h, 1d)
- Scans running every 5 minutes, zero fallback warnings

### Issues Found During Deployment

1. **`wheel` group doesn't exist on Debian**: setup.yml tried `groups: sudo,wheel`. Fixed with conditional: `sudo` for Debian, `wheel` for Arch.
2. **Ansible `yaml` callback removed**: `stdout_callback = yaml` (community.general) was removed in newer Ansible. Fixed to `result_format = yaml`.
3. **SSH verify used `ansible_host` in delegate**: resolved to `localhost` instead of VPS IP. Fixed to use `inventory_hostname`.
4. **Telegram Conflict error**: Still occurring on VPS. Same `terminated by other getUpdates request` from dev box. Not a multi-instance issue — only one container running.
5. **Telegram commands not working**: `allowed_user_ids = []` rejects all users by design. Added user's Telegram ID to config.

### Config Externalization

- Created `config/settings.example.toml` with default/placeholder values
- Added `config/settings.toml` to `.gitignore` (contains personal user ID)
- Untracked `settings.toml` from git (`git rm --cached`)
- `.env` was already gitignored with `.env.example` template
- `deploy/inventory.yml` was already gitignored with `inventory.yml.example` template

### Deployment Workflow

```bash
# First-time VPS setup (root + password):
cd deploy
ansible-playbook setup.yml -i "1.2.3.4," -u root --extra-vars "ansible_password=<pw>"

# Fill inventory with connection + secrets:
cp inventory.yml.example inventory.yml
# Edit inventory.yml

# Deploy application:
ansible-playbook playbook.yml

# Update after code changes (no container restart if only strategy/stats changed):
ansible-playbook playbook.yml --tags sync

# Deploy just Caddy:
ansible-playbook playbook.yml --tags caddy

# View logs remotely:
ssh -i keys/trading-brain trading@<host> "docker compose -f /srv/trading-brain/docker-compose.yml logs --tail=50"
```

### Telegram Conflict Resolved
- New bot token deployed via `ansible-playbook playbook.yml --tags secrets`
- Discovered `docker compose restart` does NOT re-read `.env` — only `up -d --force-recreate` works
- Fixed playbook handler permanently: `restart container` now uses `up -d --force-recreate`
- With new token + recreated container: zero Conflict errors, commands working

### First Orchestrator Cycle (from Arch dev box)
- Reviewed CSV exports from DataGrip (orchestrator ran on dev box before VPS deployment)
- **Analysis (Opus)**: Identified 4 fundamental flaws in v001 — restrictive volume filter (>1.2x), noisy 5m EMA crossover, RSI filter conflicts, 2% stop too tight for crypto. Decided STRATEGY_RESTRUCTURE tier 2.
- **Code Gen (Sonnet)**: Generated v002 — Hourly Trend-Following with Pullback Entry. 1h timeframe, pullback to EMA9 support, RSI 40-65, volume >0.8x, 4% SL / 8% TP (2:1 R:R), SWING intent.
- **Code Review (Opus)**: Approved with 4 minor notes. Clean restructure, long-only compliant, proper guards.
- Total cycle time: ~49 seconds. System working exactly as designed.

### VPS Health Check (Session 17)
- **Zero errors** in entire log since deployment
- Scan loop: 9 symbols every 5 min, no misses
- DB healthy: 16K+ candles (5m/1h/1d), 81 scan results, fee schedule populated
- API: All 11 endpoints responding through Caddy reverse proxy
- Resources: 0.12% CPU, 100MB RAM — very lean
- WebSocket: Connected to Kraken
- Telegram: Working, no Conflict errors
- Strategy state: Persisting every scan cycle
- `strategy_version_count: 0` expected — orchestrator hasn't run on VPS yet (first cycle tonight 12-3am EST)

### Stale Indicators Bug Found and Fixed
- After 8 hours of running, all indicators (RSI, EMA, volume_ratio) were frozen at startup values
- Prices updated fine (from WebSocket ticker), but candles were never refreshed after bootstrap
- Root cause: scan loop only fetched fresh candles if DB had < 30 rows. After bootstrap (1000+ rows), it never fetched again.
- Fix: fetch fresh 5m candles from Kraken REST every scan, 1h every hour, 1d every day. `INSERT OR IGNORE` handles duplicates.
- WebSocket OHLC subscription exists but callback was never registered — REST approach is simpler and more reliable.

### Security Incident — libprocesshider.so on VPS
- Discovered `/etc/ld.so.preload` referencing `libprocesshider.so` — a known rootkit process-hiding library
- Library file itself was missing but the preload entry remained, printing errors to stderr on every process
- This corrupted SSH stdout/SCP/rsync/Ansible output (binary protocol streams polluted by error messages)
- VPS was a Hetzner cloud instance — likely a tainted base image or previous tenant artifacts
- **VPS shut down immediately**. All keys must be rotated (Kraken, Anthropic, Telegram, API).

### Security Hardening (complete rewrite of deploy scripts)
- **setup.yml Phase 1**: Integrity verification before doing anything else
  - Checks `/etc/ld.so.preload` — fails if non-empty
  - Scans for SUID/SGID binaries outside standard paths
  - Checks for rogue root cron jobs, unexpected listening ports, unexpected users
- **Scoped sudo**: Deploy user can only run docker, systemctl, apt-get, ufw — not full root
- **System upgrades**: `apt upgrade` during setup + `unattended-upgrades` for ongoing security patches
- **fail2ban**: SSH jail — bans after 5 failed attempts for 1 hour
- **Firewall**: Only ports 22, 80, 443 open (removed port 8080 — API only via Caddy)
- **SSH hardening**: Added `LoginGraceTime 30`, `ClientAliveInterval 300`, `ClientAliveCountMax 2`
- **Docker hardening**: Non-root user in Dockerfile, `no-new-privileges`, `read_only` filesystem, tmpfs for /tmp
- **Removed old broad sudoers files**: wheel, cloud-init-users

### Current State
- Old VPS shut down, keys need rotation
- New Hetzner instance needed — will run setup.yml (with integrity checks) → playbook.yml
- All security fixes committed, stale indicators fix committed
- 58/58 tests passing
