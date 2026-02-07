# Architecture: IO-Container Orchestration System

> **NOTE**: This replaces the old three-brain architecture (v1). That code remains on main branch for reference.

## System Diagram

```
┌─────────────────────────────────────────────────────────┐
│                    SHELL (Rigid)                         │
│  Never modified by agent. Enforces all hard limits.     │
│                                                         │
│  ┌──────────┐  ┌──────────┐  ┌───────────┐            │
│  │ Kraken   │  │ Risk     │  │ Portfolio  │            │
│  │ Client   │  │ Manager  │  │ Tracker    │            │
│  │ REST+WS  │  │ (hard    │  │ Paper+Live │            │
│  └────┬─────┘  │  limits) │  └─────┬─────┘            │
│       │        └────┬─────┘        │                    │
│       │             │              │                    │
│  ┌────▼─────────────▼──────────────▼─────┐             │
│  │        IO CONTRACT (rigid interface)   │             │
│  │  IN:  SymbolData, Portfolio, RiskLimits│             │
│  │  OUT: list[Signal]                     │             │
│  └────────────────┬──────────────────────┘             │
│                   │                                     │
│  ┌────────────────▼──────────────────────┐             │
│  │         STRATEGY MODULE (flexible)     │             │
│  │     strategy/active/strategy.py        │             │
│  │     + strategy/skills/*.py             │             │
│  │     (Agent CAN modify this)            │             │
│  └────────────────┬──────────────────────┘             │
│                   │                                     │
│  ┌────────────────▼──────────────────────┐             │
│  │  Data Store  │  Telegram  │  Database  │             │
│  │  (tiered     │  (commands │  (SQLite)  │             │
│  │   OHLCV)     │   + notif) │            │             │
│  └───────────────────────────────────────┘             │
└─────────────────────────────────────────────────────────┘

         ┌──────────────────────────────────┐
         │     ORCHESTRATOR (AI Brain)       │
         │     Runs daily 12-3am EST         │
         │                                   │
         │  1. Read strategy document         │
         │  2. Review performance data        │
         │  3. Query strategy index           │
         │  4. Decide: change or continue     │
         │  5. If change:                     │
         │     Sonnet generates new code      │
         │     Opus reviews for correctness   │
         │     Sandbox → Backtest → Paper     │
         │     Hot-swap if tests pass         │
         │  6. Update strategy document       │
         │  7. Generate report, notify user   │
         └──────────────────────────────────┘
```

## IO Contract

### Input Types (Shell → Strategy)

| Type | Contents | Purpose |
|------|----------|---------|
| `SymbolData` | current_price, candles_5m (30d), candles_1h (1yr), candles_1d (7yr), spread, volume_24h | All market data for one symbol |
| `Portfolio` | cash, total_value, positions, recent_trades (last 100), daily_pnl, total_pnl, fees | Full portfolio state |
| `RiskLimits` | max_trade_pct, default_trade_pct, max_positions, max_daily_loss_pct, max_drawdown_pct | Read-only risk constraints |
| `OpenPosition` | symbol, side, qty, avg_entry, current_price, unrealized_pnl/pct, intent, stop_loss, take_profit | One open position |
| `ClosedTrade` | symbol, side, qty, entry/exit price, pnl/pct, intent, timestamps | One completed trade |

### Output Types (Strategy → Shell)

| Type | Contents | Purpose |
|------|----------|---------|
| `Signal` | symbol, action (BUY/SELL/CLOSE), size_pct, order_type, limit_price, stop_loss, take_profit, intent (DAY/SWING/POSITION), confidence, reasoning | One trading decision |

### Strategy Class Interface

```python
class Strategy:
    def initialize(self, risk_limits: RiskLimits, symbols: list[str]) -> None
    def analyze(self, markets: dict[str, SymbolData], portfolio: Portfolio, timestamp: datetime) -> list[Signal]
    def on_fill(self, symbol, action, qty, price, intent) -> None
    def on_position_closed(self, symbol, pnl, pnl_pct) -> None
    def get_state(self) -> dict          # Serialize for persistence
    def load_state(self, state: dict)    # Restore after restart
    @property
    def scan_interval_minutes(self) -> int  # How often to call analyze()
```

**Rules**: Strategy is pure logic. No network calls, no file I/O, no subprocess calls. Shell enforces risk limits as safety net on all signals.

## Project Structure

```
trading-brain/
├── config/
│   ├── settings.toml              # General settings (mode, API provider, etc.)
│   └── risk_limits.toml           # HARD risk limits (user-only, never agent-modified)
├── src/
│   ├── __init__.py
│   ├── main.py                    # Entry point, scheduler, lifecycle
│   ├── shell/                     # RIGID infrastructure
│   │   ├── __init__.py
│   │   ├── contract.py            # IO contract types (Signal, SymbolData, etc.)
│   │   ├── kraken.py              # Kraken REST + WebSocket client
│   │   ├── risk.py                # Risk limit enforcement
│   │   ├── portfolio.py           # Position tracking, paper/live trading
│   │   ├── data_store.py          # Historical data, tiered aggregation
│   │   ├── database.py            # SQLite connection + schema
│   │   └── config.py              # Config loading
│   ├── strategy/                  # Strategy loading + testing
│   │   ├── __init__.py
│   │   ├── loader.py              # Dynamic import of strategy.py
│   │   ├── sandbox.py             # Isolated strategy testing
│   │   └── backtester.py          # Historical backtesting
│   ├── orchestrator/              # AI brain
│   │   ├── __init__.py
│   │   ├── orchestrator.py        # Nightly review + modification cycle
│   │   ├── ai_client.py           # Anthropic / Vertex abstraction
│   │   └── reporter.py            # Report generation
│   ├── telegram/                  # User interface
│   │   ├── __init__.py
│   │   ├── bot.py                 # Bot setup + lifecycle
│   │   ├── commands.py            # Command handlers
│   │   └── notifications.py       # Outbound alerts
│   └── utils/
│       ├── __init__.py
│       └── logging.py             # Structured logging
├── strategy/                      # AGENT WORKSPACE (outside src/)
│   ├── active/
│   │   └── strategy.py            # Currently running strategy
│   ├── archive/                   # Previous versions (strategy_v001.py, etc.)
│   ├── skills/                    # Reusable indicator functions
│   └── strategy_document.md       # Long-term strategy (agent-maintained)
├── data/
│   └── brain.db                   # SQLite database
├── reports/                       # Orchestrator-generated reports
├── tests/
├── claude_notes/                  # Engineering notes (this directory)
├── pyproject.toml
├── .env.example
├── .gitignore
└── CLAUDE.md
```

## Data Tiering (7-Year Retention)

| Age | Resolution | Rows/symbol/year | Purpose |
|-----|-----------|-----------------|---------|
| 0-30 days | 5-min candles | ~8,600 | Day trading |
| 30 days - 1 year | 1-hour candles | ~8,760 | Swing trading |
| 1-7 years | Daily candles | ~365 | Macro trends, backtesting |

- Nightly aggregation job during orchestration window
- Total after 7 years: ~100K rows/symbol, ~30MB total
- Strategy document: quarterly distillation (archive yearly summaries, keep <2,000 words active)

## Graceful Startup/Shutdown

### Shutdown Sequence
1. Stop scheduler (no new scans)
2. Save strategy state → DB (`strategy.get_state()`)
3. Cancel unfilled limit orders on Kraken
4. Do NOT close positions (preserved across restarts)
5. Stop WebSocket, Telegram, flush DB, close DB, exit

### Startup Sequence
1. Load config, connect DB
2. Load active strategy module, restore state (`load_state()`)
3. Connect Kraken REST
4. **Reconcile positions** (DB vs Kraken in live mode)
5. Fetch prices, update unrealized P&L
6. Check for pending paper tests → resume
7. Start WebSocket, Telegram, scheduler
8. Send Telegram: "System online, X positions, $Y portfolio"

### Position Reconciliation (Live Mode)
- DB says positions X,Y,Z — Kraken says A,B,C
- Match: continue. Mismatch: update DB to match Kraken, log warning, notify

## Scheduled Jobs

| Job | Frequency | Purpose |
|-----|-----------|---------|
| Strategy scan | Every N min (strategy-defined, default 5) | Call `strategy.analyze()`, execute signals |
| Position monitor | Every 30 sec | Check stop-loss/take-profit triggers |
| Fee check | Every 24h | Update fee schedule from Kraken |
| Daily P&L snapshot | 23:55 local | Record daily performance |
| Orchestration cycle | 12:00-3:00 AM EST | AI review + strategy modification |
| Data aggregation | During orchestration | Tier old candles (5m→1h→daily) |
| Quarterly distillation | Every 4 quarters | Prune strategy document |

## AI Model Roles

| Role | Model | Purpose | Cost Estimate |
|------|-------|---------|--------------|
| Strategy code generation | Sonnet | Write/modify strategy.py and skills | ~$0.10-0.20/cycle |
| Code review + analysis | Opus | Review correctness, IO compliance, risk classification | ~$0.30-0.50/cycle |
| Total per orchestration night | — | When making changes | $0.75-2.25 |
| Total per month | — | ~$22-45 (budgeted at 150% of base estimate) | |

## API Provider Abstraction

```toml
# config/settings.toml
[ai]
provider = "vertex"  # or "anthropic"

[ai.vertex]
project_id = "gcp-project"
region = "us-east5"

[ai.anthropic]
# Uses ANTHROPIC_API_KEY env var
```

Both use identical message format. Clean swap via config flag.
