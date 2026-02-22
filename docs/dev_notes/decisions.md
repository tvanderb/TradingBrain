# Key Decisions

> Decisions that still actively inform development. Fully-integrated decisions (exchange choice, initial strategy, pip install policy, etc.) have been pruned.

## Architecture & Strategy

### IO-Container Design
- **Shell** (rigid): Kraken client, risk manager, portfolio tracker, Telegram, DB — agent cannot touch
- **Strategy module** (flexible): Single Python file the AI agent rewrites
- **IO contract**: `SymbolData + Portfolio + RiskLimits` in, `list[Signal]` out
- User's key insight: "the inner core is changeable by the agent so as to continuously refine trading strategies autonomously"

### Candidate Strategy System (Session T)
- **Replaces** paper-test-on-active-strategy (which was broken — real money at risk during "testing")
- Up to 3 candidates run paper simulation alongside active strategy
- Decision types: CREATE_CANDIDATE, CANCEL_CANDIDATE, PROMOTE_CANDIDATE
- No risk tiers or fixed durations — Opus manages lifecycle freely
- On promotion: Opus chooses "keep" (inherit positions) or "close_all" (clean slate)
- `UNIQUE(slot)` constraint — slot history overwrites, but trades/positions preserved in separate tables

### Nested Orchestrator Loops (Session S)
- Inner loop (max_revisions=3): Sonnet → sandbox → Opus code review
- Outer loop (max_strategy_iterations=3): backtest → Opus reviews results → deploy or `revision_instructions`
- Opus's revision instructions *replace* (not append to) the accumulated changes, giving Sonnet a fresh start

### Institutional Learning System (Session W)
- **Predictions**: Falsifiable claims graded during reflection. Stored with claim/evidence/confidence/timeframe.
- **Reflection** (configurable interval, default 7d): Full Opus call grades predictions, rewrites strategy doc, stores new predictions. Runs BEFORE nightly analysis.
- **Key trade-off**: Full strategy doc rewrite each reflection (not append). Previous versions permanently archived.
- **Manual trigger**: `/reflect` sets system_meta flag, reflection runs in next orchestration window.
- **MAE tracking**: Max adverse excursion on all positions (fund + candidate), carried to trades on close.

## Risk & Trading

### Risk Limits — Emergency Backstops (Session B/D5)
- max_trade 10%, max_position 25%, max_drawdown 40%, rollback 15%, default 3% per trade
- Agent CANNOT modify. Shell enforces as safety net on all signals.
- Philosophy: trust the aligned agent to self-regulate; hard limits are seatbelts, not driving instructions.

### Fund Mandate Replaces Numeric Goals (Sessions 7-8)
- Scrapped prioritized numeric targets. Mandate: "Portfolio growth with capital preservation. Avoid major drawdowns. Long-term fund."
- No numeric targets given to orchestrator — it decides what to optimize based on identity + awareness.

### Long-Only — Kraken Canada Restriction
- Kraken margin trading NOT available to Canadian residents. System is long-only.
- If exchange changes policy, SHORT support would need re-implementation.

### Static 9-Pair List with Per-Pair Fees (Session 10)
- BTC, ETH, SOL, XRP, DOGE, ADA, LINK, AVAX, DOT. 12-pair cap.
- Fees in IO contract: `SymbolData` gets `maker_fee_pct`/`taker_fee_pct`. Per-pair `fee_schedule` table.
- Dynamic watchlist rejected as unnecessary complexity at this scale.

### Intent is Informational (Session A/D3)
- DAY/SWING/POSITION are metadata labels. Shell does NOT enforce different behavior per intent.
- Strategy manages its own exit logic. "Maximize awareness, minimize direction."

## Infrastructure

### Live Config Reload via SIGHUP (Session AD)
- **Safe fields**: risk, notifications, orchestrator schedule, fees, AI models, slippage, log level, allowed_user_ids
- **Refused fields**: mode, symbols, paper_balance_usd, db_path, credentials, api.host/port
- Uses in-place mutation of Config dataclass so all component references see new values atomically.

### Deps-Only Docker Image (Session AD)
- Dockerfile installs only pip dependencies. `src/` volume-mounted. Code deploys = rsync + restart (~5s).
- Image rebuilds only when `pyproject.toml` changes (numpy/scipy/pandas take 15-20 min).

### Restart Safety (Session L)
- **L1**: Starting capital in `system_meta`, cash reconciles from first principles always
- **L2**: Halt evaluation on startup before any trading
- **L4**: Strategy fallback chain: filesystem → DB → paused mode (orchestrator still runs)
- **L6**: Config validates timezone, symbol format, trade size consistency

### Statistics Shell — Two Modules
- **Market analysis**: Exchange data ("What game are we playing?")
- **Trade performance**: Trading results ("How well are we playing?")
- Both backed by truth benchmarks (rigid, orchestrator cannot modify)
- Cross-referencing done by orchestrator (LLM strength), not by modules

### Token Budget — High Safety Net (Session 10)
- ~1.5M token limit (10x expected). Not an operational gate — orchestrator self-regulates via awareness.
- `max_revisions` (3 attempts) naturally caps per-cycle spend.

### Data Retention — 7 Years, Tiered
- 5-min candles: 30 days → 1-hour. 1-hour: 1 year → daily. Daily: 7 years.
- Strategy document: quarterly distillation, archive yearly summaries, keep <2,000 words active.
