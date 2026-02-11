# Architecture: IO-Container Orchestration System

> v1 (three-brain) was removed in Session 14. This is the only architecture.

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
         │     Runs daily 3:30-6am EST       │
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
         │  6. Store observations, maintain   │
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
| `Signal` | symbol, action (BUY/SELL/CLOSE/MODIFY), size_pct, order_type, limit_price, stop_loss, take_profit, intent (DAY/SWING/POSITION), tag, confidence, reasoning, slippage_tolerance | One trading decision |

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
│   ├── main.py                    # Entry point, scheduler, lifecycle, restart safety
│   ├── shell/                     # RIGID infrastructure
│   │   ├── __init__.py
│   │   ├── contract.py            # IO contract types (Signal, SymbolData, etc.)
│   │   ├── kraken.py              # Kraken REST + WebSocket client
│   │   ├── risk.py                # Risk limit enforcement + halt evaluation
│   │   ├── portfolio.py           # Position tracking, paper/live trading, cash reconciliation
│   │   ├── truth.py               # 28 truth benchmark metrics
│   │   ├── data_store.py          # Historical data, tiered aggregation
│   │   ├── database.py            # SQLite connection + schema + system_meta
│   │   └── config.py              # Config loading + validation
│   ├── strategy/                  # Strategy loading + testing
│   │   ├── __init__.py
│   │   ├── loader.py              # Dynamic import with DB fallback chain
│   │   ├── sandbox.py             # AST-based code validation
│   │   └── backtester.py          # Historical backtesting (LIMIT simulation)
│   ├── orchestrator/              # AI brain
│   │   ├── __init__.py
│   │   ├── orchestrator.py        # Nightly review + modification cycle
│   │   ├── ai_client.py           # Anthropic / Vertex abstraction
│   │   └── reporter.py            # Report generation
│   ├── statistics/                # Analysis module infrastructure
│   │   ├── __init__.py
│   │   ├── loader.py              # Module loading and deployment
│   │   ├── sandbox.py             # Analysis module validation
│   │   └── readonly_db.py         # Read-only DB wrapper
│   ├── telegram/                  # User interface
│   │   ├── __init__.py
│   │   ├── bot.py                 # Bot setup + lifecycle
│   │   ├── commands.py            # 16 command handlers
│   │   └── notifications.py       # Dual dispatch (Telegram + WebSocket)
│   ├── api/                       # Data API
│   │   ├── __init__.py
│   │   ├── server.py              # aiohttp app with auth + error middleware
│   │   ├── routes.py              # 10 REST endpoints
│   │   └── websocket.py           # WebSocket event stream
│   └── utils/
│       ├── __init__.py
│       └── logging.py             # Structured logging
├── strategy/                      # AGENT WORKSPACE (outside src/)
│   ├── active/
│   │   └── strategy.py            # Currently running strategy
│   ├── archive/                   # Previous versions (strategy_v001.py, etc.)
│   └── strategy_document.md       # Long-term strategy (agent-maintained)
├── statistics/                    # AGENT WORKSPACE (analysis modules)
│   ├── active/
│   │   ├── market_analysis.py     # Market/exchange data analysis
│   │   └── trade_performance.py   # Trade performance analysis
│   └── archive/                   # Previous module versions
├── data/
│   └── brain.db                   # SQLite database
├── deploy/                        # Deployment automation
│   ├── setup.yml                  # VPS hardening (Ansible)
│   ├── playbook.yml               # App deployment (Ansible)
│   ├── inventory.yml              # VPS connection + secrets
│   ├── monitor.sh                 # Cron-based health monitor
│   └── restart.sh                 # Container restart helper
├── tests/
├── docs/
│   ├── DEPLOY.md                  # Admin deployment guide
│   ├── API.md                     # REST + WebSocket API reference
│   └── dev_notes/                 # Engineering notes
├── Dockerfile
├── docker-compose.yml
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
3. Cancel all conditional orders on Kraken (SL/TP)
4. Cancel unfilled limit orders on Kraken
5. Do NOT close positions (preserved across restarts)
6. Stop WebSocket, Telegram, API server, flush DB, close DB, exit

### Startup Sequence (with L1-L9 Restart Safety)
1. Load config + validate (L6: timezone, symbol format, trade size consistency)
2. Connect DB, run migrations (`system_meta` table, `strategy_versions.code` column)
3. Load positions from DB
4. **Reconcile paper cash** from first principles (L1): `starting_capital + deposits + pnl - position_costs`
   - Starting capital persisted in `system_meta` table (survives config changes)
   - Live mode: fetch Kraken balance (L7: fail-fast on auth failure)
5. Initialize risk manager, restore state from DB
6. **Evaluate halt conditions** (L2): drawdown, consecutive losses, daily loss, rollback triggers
7. **Detect orphaned positions** (L3): warn if positions exist for unconfigured symbols
8. **Load strategy with fallback** (L4): filesystem → DB (`strategy_versions.code`) → paused mode
9. **Check analysis modules** (L5): warn if files missing
10. Bootstrap candle data (fetch missing 5m/1h/1d)
11. Reconcile orders (L7: stale orders, orphaned conditionals, re-place expired SL/TP)
12. Start Telegram, API server, scheduler
13. Send alerts: system online + any halt/orphan/paused warnings

### Position Reconciliation (Live Mode)
- DB says positions X,Y,Z — Kraken says A,B,C
- Match: continue. Mismatch: update DB to match Kraken, log warning, notify
- Stale orders and orphaned conditionals cleaned up on startup

## Scheduled Jobs

| Job | Frequency | Purpose |
|-----|-----------|---------|
| Strategy scan | Every N min (strategy-defined, default 5) | Call `strategy.analyze()`, execute signals |
| Position monitor | Every 30 sec | Check SL/TP (paper: price check, live: exchange orders) |
| Fee check | Every 24h | Update fee schedule from Kraken API |
| Daily P&L snapshot | 23:55 local | Record daily performance |
| Orchestration cycle | 3:30-6:00 AM EST | AI review + strategy modification |
| Data aggregation | During orchestration | Tier old candles (5m→1h→daily) |
| Data pruning | During orchestration | Delete candles beyond retention period |
| Conditional order monitor | Every 30 sec (live only) | Check exchange SL/TP fills |

## AI Model Roles

| Role | Model | Purpose | Cost Estimate |
|------|-------|---------|--------------|
| Strategy code generation | Sonnet | Write/modify strategy.py | ~$0.10-0.20/cycle |
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
- **Cross-referencing**: Both modules run independently — neither sees the other's output. The orchestrator receives both reports and performs cross-referencing (e.g., correlating market conditions with trade outcomes). This is what LLMs are good at. The modules focus on computing hard numbers accurately — what LLMs are bad at.

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
│  │    market_analysis.py         │  │ trade_performance.py│   │
│  │  IN:  read-only DB + schema   │  │ IN: read-only DB     │   │
│  │                               │  │     + schema         │
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
Rigid shell component. 28 metrics computed directly from raw data. Cannot be modified by orchestrator. These are the "weighing scale" — if either analysis module contradicts truth, the orchestrator knows its analysis is wrong.

| Metric | Computation | Purpose |
|--------|-------------|---------|
| trade_count | COUNT(trades) | Activity level |
| win_count / loss_count | COUNT WHERE pnl > 0 / < 0 | Win/loss split |
| win_rate | wins / total | Success rate |
| net_pnl | SUM(trades.pnl) | Ground truth P&L |
| total_fees | SUM(trades.fees) | Fee drag |
| avg_win / avg_loss | AVG of positive/negative pnl | Payoff profile |
| expectancy | (win_rate * avg_win) + (loss_rate * avg_loss) | Expected value per trade |
| consecutive_losses | Current losing streak from recent trades | Danger indicator |
| portfolio_value / portfolio_cash | From daily snapshots | Current state |
| max_drawdown_pct | Peak-to-trough from snapshots | Risk realized |
| total_signals / acted_signals | COUNT(signals), SUM(acted_on) | Signal activity |
| signal_act_rate | acted / total | Execution rate |
| total_scans | COUNT(scan_results) | Activity level |
| first_scan_at / last_scan_at | MIN/MAX(scan_results.created_at) | Uptime + freshness |
| current_strategy_version | Latest deployed version | Strategy state |
| strategy_version_count | COUNT(strategy_versions) | Evolution pace |
| profit_factor | gross_wins / gross_losses | Win/loss ratio quality |
| close_reason_breakdown | GROUP BY close_reason | How trades close |
| avg_trade_duration_hours | AVG(closed_at - opened_at) | Holding behavior |
| best_trade_pnl_pct / worst_trade_pnl_pct | MAX/MIN(pnl_pct) | Outlier trades |
| sharpe_ratio / sortino_ratio | From daily return series | Risk-adjusted returns |

### Analysis Module IO Contract

Both modules share the same base interface:

```python
class AnalysisBase(ABC):
    """Base class for analysis modules (market + trade performance)."""

    async def analyze(self, db: ReadOnlyDB, schema: dict) -> dict:
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

### Sandbox Rules
| Rule | Strategy Module | Analysis Modules |
|------|----------------|-------------------|
| Read DB | Forbidden | Allowed (read-only) |
| Write DB | Forbidden | Forbidden |
| Network | Forbidden | Forbidden |
| File I/O | Forbidden | Forbidden |
| subprocess/os/eval | Forbidden | Forbidden |
| pandas/numpy | Allowed | Allowed |
| ta (100+ indicators) | Allowed | Allowed |
| scipy | Allowed | Allowed |
| stdlib (math, statistics, collections, etc.) | Allowed | Allowed |
| src.shell.contract | Allowed | N/A |

**Strategy available imports**: pandas, numpy, ta, scipy, math, statistics, collections, dataclasses, datetime, functools, itertools, random, copy, src.shell.contract

### Orchestrator Mandate (embedded in system prompt)
**Fund mandate**: Portfolio growth with capital preservation. Avoid major drawdowns. Long-term fund.
**Framework**: Three-layer prompt — Identity (WHO) / System Understanding (WHAT it works with) / Institutional Memory (WHAT it learned). No numeric targets, no behavioral directives. See discussions.md Sessions 7-8.

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
9. Store observations (daily findings → orchestrator_observations DB table, rolling 30d)
10. Data maintenance (aggregation, pruning)
11. Send report via Telegram
```

### Database Schema (Key Tables)

```sql
-- Scan results: price + spread per symbol per scan
CREATE TABLE scan_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    price REAL NOT NULL,
    spread REAL,
    signal_generated INTEGER DEFAULT 0,
    signal_action TEXT,
    signal_confidence REAL,
    created_at TEXT DEFAULT (datetime('now'))
);

-- System metadata: key-value store for persistent settings
CREATE TABLE system_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Orders: exchange order tracking with fill data
CREATE TABLE orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT,
    symbol TEXT NOT NULL, tag TEXT,
    side TEXT NOT NULL, order_type TEXT NOT NULL,
    qty REAL NOT NULL, price REAL,
    fill_price REAL, fill_qty REAL, fill_fee REAL,
    status TEXT DEFAULT 'pending',
    created_at TEXT DEFAULT (datetime('now')),
    filled_at TEXT, cancelled_at TEXT
);

-- Conditional orders: exchange-native SL/TP pairs per position
CREATE TABLE conditional_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_tag TEXT NOT NULL,
    order_type TEXT NOT NULL,  -- 'stop_loss' or 'take_profit'
    order_id TEXT,
    trigger_price REAL NOT NULL,
    status TEXT DEFAULT 'active',
    created_at TEXT DEFAULT (datetime('now')),
    filled_at TEXT, cancelled_at TEXT
);

-- Capital events: deposit/withdrawal tracking
CREATE TABLE capital_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,  -- 'deposit' or 'withdrawal'
    amount REAL NOT NULL,
    note TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
```

Key columns on existing tables:
- `positions`: `tag TEXT NOT NULL` (globally unique identifier), `intent TEXT`, `strategy_version TEXT`
- `trades`: `close_reason TEXT`, `tag TEXT`
- `strategy_versions`: `code TEXT` (source code for DB fallback)

### File Structure
```
statistics/
├── active/
│   ├── market_analysis.py      # Market/exchange data analysis (orchestrator rewrites)
│   └── trade_performance.py    # Trade performance analysis (orchestrator rewrites)
├── archive/                    # Previous versions of both modules

src/statistics/
├── __init__.py
├── loader.py                   # Shared loader for both modules
├── sandbox.py                  # Shared sandbox validation
└── readonly_db.py              # ReadOnlyDB wrapper (SELECT only, null-byte blocked)
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
