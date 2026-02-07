# Build Progress

## Session 1 (2026-02-06)

### Completed
- Architecture planned and approved
- Phase 1: Foundation (pyproject.toml, config system, SQLite DB with 7 tables, structlog logging, token tracker)
- Phase 2: Market data (Kraken REST/WS client, 4 technical indicators, regime classifier)
- Phase 3: Executor (risk manager, paper trading simulator with slippage/fees, position tracker with SL/TP)
- Phase 4: Analyst brain (Sonnet-powered signal validation, cost-optimized prompts, /ask command)
- Phase 5: Executive brain (daily evolution cycle, parameter tuning, pattern library)
- Phase 6: Telegram (12 commands, proactive notifications, fee update alerts)
- Phase 7: Orchestrator (main.py, APScheduler, fee check scheduling)
- All imports verified, paper trader tested (buy/sell cycle with fees)

### Verified Working
- Config loading from 3 sources + .env
- Database schema creation (7 tables including fee_schedule)
- Paper trading: buy BTC at $50k, sell at $51k, realistic fees deducted
- Risk checks: trade size limits, position limits correctly enforced
- Technical indicators: RSI, Bollinger Bands, EMA, MACD, ATR, volume ratio
- Regime classification on synthetic data
- Parameter validation and clamping

### Tests Passing
- `test_integration.py`: Full pipeline — signal gen -> risk check -> paper buy -> paper sell -> DB store -> fee store
- `test_boot.py`: All 9 components initialize correctly in paper mode
- Key finding: fees eat 27.7% of gross profit on a 2% BTC move with $43 position. Minimum trade size enforcement is critical.

### Gotcha: telegram import
- python-telegram-bot v22.6 had an empty `telegram/__init__.py` on first install
- Fixed with `pip install --force-reinstall python-telegram-bot`
- Note for VPS deployment: may need same fix

### Current Status
- All code compiles and tests pass
- Need user's .env file to run live (Kraken API key, Anthropic key, Telegram bot token)
- `websockets` package not installed — data feed uses REST polling fallback (fine for testing)
- No git commit yet

### Live System Running (Session 1)
- System fully booted in paper mode with $200
- Kraken WebSocket v2 connected successfully (needed certifi SSL fix for macOS)
- Telegram bot is polling and responding
- Fee check confirmed: 0.25% maker / 0.40% taker ($0 volume tier)
- Waiting for first analyst scan (5-min interval)
- Market is quiet (BTC ranging/breakout, low volatility) — system correctly not forcing trades

### Gotchas Found
- macOS Python 3.14 SSL certs: websockets library needs `certifi.where()` passed to ssl context
- python-telegram-bot needed force-reinstall on first pip install
- Structlog JSON output needs PYTHONUNBUFFERED=1 for real-time visibility
- APScheduler interval trigger doesn't fire immediately — need `next_run_time=datetime.now()` for first-run
- WebSocket needs 3-failure fallback to REST polling (SSL errors on some systems)
- SOL/USD occasionally generates weak signals (0.5-0.6 strength) even in quiet markets — below 0.7 threshold

### Commands Added
- `/signals` — shows recent analyst evaluations (signals + AI decisions)
- `/report` — on-demand market analysis via Claude (uses analyst brain tokens)

### Open Items
- Evolution Levels 2-4: needs collaborative planning session with user
- User explicitly wants deep involvement in designing the self-evolution architecture
- Need to plan how code generation (Level 4) works with safety rails

## Session 2 (2026-02-07)

### Context
Continuing from session 1. System was running in paper mode, user tested Telegram commands.

### User Feedback Received
1. `/report` was broken — called Claude without market data, Claude said "I don't have access to market data"
2. User's key design insight: `/report` should show **existing system calculations**, not generate fresh AI analysis. "current calculations and reports" — the system already computes indicators every 5 min, just display those.
3. User wants more rigorous note-taking — not just progress, but discussions, design direction, user preferences

### Changes Made
- **Redesigned `/report` command**: No longer calls Claude. Now reads from shared `scan_state` dict populated by the 5-min scan loop. Shows: price, regime, RSI, BB%, EMA alignment, MACD histogram, volume ratio, and active signals for each symbol.
- **Updated scan loop**: Now stores full indicator state in `scan_state` dict (shared between scan and commands)
- **Added `scan_state` shared dict**: Created in `main.py`, passed to `BotCommands`, written by scan loop, read by `/report`
- **`ask_question()` now accepts `max_tokens` param** and uses `default=str` in json.dumps (cleanup)
- **Updated CLAUDE.md**: Expanded note-taking section with specific file organization, quality bar, and what to capture
- **Created `claude_notes/discussions.md`**: Captures design direction, user philosophy, open threads
- **Created `claude_notes/gotchas.md`**: All technical issues and their fixes for reference

### Files Modified
- `src/telegram/commands.py` — Redesigned `/report`, added `scan_state` dependency
- `src/main.py` — Added `scan_state` dict, scan loop now stores indicators, imported `compute_indicators`
- `src/brains/analyst.py` — `ask_question()` accepts `max_tokens`, uses `default=str` in json serialization
- `CLAUDE.md` — Expanded note-taking mechanism section

### MAJOR PIVOT: Architecture Redesign
- User proposed scrapping three-brain architecture for IO-Container design
- Extensive collaborative design discussion (see discussions.md for full thread)
- All design decisions finalized and approved
- Starting fresh on new branch — old code stays on main for reference

### What Was Agreed (Summary)
- IO-Container: rigid shell + flexible strategy module + AI orchestrator
- Sonnet generates code, Opus reviews
- Three-tier paper testing (1d / 2d / 1wk)
- 7-year tiered data retention
- Quarterly strategy document distillation
- Full spectrum trading (day/swing/hold)
- Google Vertex API support (user has $300 credit)
- Token budget: $22-45/month (150% of base)
- Fully autonomous with Telegram observability
- See architecture.md for full technical spec
- See decisions.md for complete decision log

### Current Status
- Old system stopped
- Design phase complete
- Ready to implement on new branch

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
