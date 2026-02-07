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
