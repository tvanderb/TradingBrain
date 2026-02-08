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

## Statistics Shell (Two-Module Design)

### Overview
Two flexible analysis modules alongside the strategy module, following the same IO-container pattern. Each serves a distinct analytical purpose and evolves independently.

1. **Market Analysis Module** — analyzes historical exchange data ("What game are we playing?")
2. **Trade Performance Module** — analyzes trading results ("How well are we playing?")

Both are backed by **Truth Benchmarks** — rigid shell-computed ground truth the orchestrator cannot modify.

### Why Two Modules (Not One)
- **Different domains**: Market structure (candles, volume, volatility) vs execution quality (trades, P&L, signals) are fundamentally different analytical tasks
- **Different value timelines**: Market analysis is useful from day one (rich candle data exists). Trade performance needs trades to accumulate.
- **Independent evolution**: Orchestrator can change market analysis without risking trade performance calculations, and vice versa
- **Fault isolation**: If one module crashes, the other still delivers its report
- **Cleaner review**: Opus reviews one focused domain per module
- **Cross-referencing**: Both have read-only DB access, so either can query any table. The DB is the shared layer — no need for modules to call each other.

### Architecture Diagram
```
┌──────────────────────────────────────────────────────────────┐
│                    SHELL (Rigid)                              │
│                                                              │
│  ┌──────────────────────────────────────────────────┐        │
│  │         TRUTH BENCHMARKS (rigid)                  │        │
│  │  Actual P&L, win rate, fees, drawdown,            │        │
│  │  trade count, portfolio value, system stats        │        │
│  │  (orchestrator CANNOT modify)                     │        │
│  └──────────────────┬───────────────────────────────┘        │
│                     │                                         │
│  ┌──────────────────▼────────────┐  ┌────────────────────┐   │
│  │  MARKET ANALYSIS (flexible)   │  │ TRADE PERF (flex)  │   │
│  │  statistics/active/           │  │ statistics/active/  │   │
│  │    market_analysis.py         │  │   trade_perf.py     │   │
│  │  IN:  read-only DB + schema   │  │ IN:  read-only DB   │   │
│  │  OUT: market report dict      │  │ OUT: perf report    │   │
│  │  "What is the market doing?"  │  │ "How are we doing?" │   │
│  └──────────────────┬────────────┘  └──────┬─────────────┘   │
│                     │                       │                  │
│  ┌──────────────────▼───────────────────────▼──────────┐      │
│  │              ORCHESTRATOR                            │      │
│  │  Receives labeled inputs:                            │      │
│  │  1. Ground Truth (rigid benchmarks)                  │      │
│  │  2. Market Analysis (its analysis, it can change)    │      │
│  │  3. Trade Performance (its analysis, it can change)  │      │
│  │  4. Its Strategy (strategy code, it can change)      │      │
│  │  5. User Constraints (risk limits, config)           │      │
│  │                                                      │      │
│  │  Can change: strategy, both analysis modules,        │      │
│  │              strategy document                       │      │
│  │  Cannot change: truth benchmarks, risk limits, shell │      │
│  └──────────────────────────────────────────────────────┘      │
└──────────────────────────────────────────────────────────────┘
```

### Truth Benchmarks (src/shell/truth.py)
Rigid shell component. Simple metrics computed directly from raw data. Cannot be modified by orchestrator. These are the "weighing scale" — if either analysis module contradicts truth, the orchestrator knows its analysis is wrong.

| Metric | Computation | Purpose |
|--------|-------------|---------|
| net_pnl | SUM(trades.pnl) | Ground truth P&L |
| trade_count | COUNT(trades) | Activity level |
| win_count / loss_count | COUNT WHERE pnl > 0 / <= 0 | Win/loss split |
| win_rate | wins / total | Success rate |
| total_fees | SUM(trades.fees) | Fee drag |
| portfolio_value | cash + SUM(positions * price) | Current state |
| max_drawdown | Peak-to-trough from snapshots | Risk realized |
| consecutive_losses | Current streak from recent trades | Danger indicator |
| system_uptime | Now - first scan timestamp | Operational context |
| total_scans | COUNT(scan_results) | Activity level |
| total_signals | COUNT(signals) | Signal rate |
| signal_act_rate | Acted / Total signals | Execution rate |

**Note on regime**: Regime classification is NOT truth — it's a heuristic interpretation. Raw indicator values (price, EMA, RSI, volume) are truth. Regime labels are analysis. The scan_results table stores raw values; the strategy's regime classification is tagged on trades as "what the strategy thought" (a fact about the decision, not about the market).

### Analysis Module IO Contract

Both modules share the same base interface:

```python
class AnalysisBase(ABC):
    """Base class for analysis modules (market + trade performance)."""

    def analyze(self, db: ReadOnlyDB, schema: dict) -> dict:
        """Run analysis and return structured report.

        Args:
            db: Read-only database connection (SELECT only)
            schema: Dict describing all tables, columns, and types

        Returns:
            Dict of computed metrics. Structure is up to the module.
        """
        ...
```

### Market Analysis Module — Focus Areas
Analyzes: candles (5m/1h/1d), scan_results, price/volume structure
- Current market conditions per symbol (volatility, trend strength, volume profile)
- Regime classification (from raw indicators, may differ from strategy's classification)
- Historical patterns (support/resistance levels, typical move sizes)
- Cross-symbol correlation
- Volatility analysis (historical vs current, expansion/contraction)
- Data quality (gaps, coverage, freshness)

### Trade Performance Module — Focus Areas
Analyzes: trades, signals, portfolio snapshots
- Performance by symbol, by intent (day/swing/position)
- Signal quality (confidence vs outcome, generation vs execution rate)
- Fee impact (fees as % of gross profit, break-even move required)
- Rolling metrics (7d, 30d, 90d trends)
- Drawdown analysis (duration, recovery time, depth)
- Regime-tagged performance (using strategy's regime classification from scan_results)

### Sandbox Rules (Same for Both Modules)
| Rule | Strategy Module | Analysis Modules |
|------|----------------|-------------------|
| Read DB | Forbidden | Allowed (read-only) |
| Write DB | Forbidden | Forbidden |
| Network | Forbidden | Forbidden |
| File I/O | Forbidden | Forbidden |
| subprocess/os/eval | Forbidden | Forbidden |
| pandas/numpy | Allowed | Allowed |
| scipy/statistics | Not needed | Allowed |
| ta (indicators) | Allowed | Allowed |

### Orchestrator Goals (embedded in system prompt)
**Primary**: Positive expectancy after fees
**Secondary**: Win rate > 45%, Sharpe > 0.3, positive monthly P&L
**Meta**: Conservative changes, build understanding, improve observability, maintain institutional memory

### Updated Orchestrator Nightly Flow
```
1. Run truth benchmarks              → ground_truth dict
2. Run market analysis module        → market_report dict
3. Run trade performance module      → trade_report dict
4. Gather strategy context           → code, doc, version history
5. Gather operational context        → system age, scan count
6. Label all inputs explicitly:
     "GROUND TRUTH (rigid, you cannot change this)"
     "YOUR MARKET ANALYSIS (you designed this, you can change it)"
     "YOUR TRADE ANALYSIS (you designed this, you can change it)"
     "YOUR STRATEGY (you designed this, you can change it, paper-tested)"
     "USER CONSTRAINTS (you cannot change this)"
7. Opus analysis                     → decisions + reasoning
8. Possible decisions (zero or more per cycle):
     - STRATEGY_TWEAK / RESTRUCTURE / OVERHAUL → generate → review → backtest → deploy
     - UPDATE_MARKET_ANALYSIS → generate → review (math focus) → deploy
     - UPDATE_TRADE_PERFORMANCE → generate → review (math focus) → deploy
     - NO_CHANGE
9. Update strategy document
10. Data maintenance
11. Send report via Telegram
```

### Database Schema Additions

```sql
-- Scan results: captures every scan's indicator state (raw values, not interpretations)
CREATE TABLE scan_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    price REAL NOT NULL,
    ema_fast REAL,
    ema_slow REAL,
    rsi REAL,
    volume_ratio REAL,
    spread REAL,
    strategy_regime TEXT,          -- what the strategy classified (fact about decision)
    signal_generated INTEGER DEFAULT 0,
    signal_action TEXT,
    signal_confidence REAL,
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX idx_scan_results_ts ON scan_results(timestamp);
CREATE INDEX idx_scan_results_symbol ON scan_results(symbol);
```

Additional columns on existing tables:
- `trades`: add `strategy_regime TEXT` — what the strategy thought the regime was at trade time
- `signals`: add `strategy_regime TEXT` — what the strategy thought at signal time

### File Structure
```
statistics/
├── active/
│   ├── market_analysis.py      # Market/exchange data analysis (orchestrator rewrites)
│   └── trade_performance.py    # Trade performance analysis (orchestrator rewrites)
├── archive/                    # Previous versions of both modules
│
src/statistics/
├── __init__.py
├── loader.py                   # Shared loader for both modules
├── sandbox.py                  # Shared sandbox validation
└── readonly_db.py              # ReadOnlyDB wrapper (SELECT only)
```

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
