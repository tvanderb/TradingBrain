# Trading Brain

An autonomous crypto trading fund managed by AI. The system trades 24/7 — scanning markets, generating signals, and executing trades within hard risk limits. Every night, an AI orchestrator reviews performance and market conditions and decides whether the trading strategy should evolve. Most nights, it doesn't change anything. When it does, the new code goes through generation, review, backtesting, and candidate paper testing before deployment. Over time, the orchestrator builds institutional memory — making falsifiable predictions, grading them against evidence, and periodically reflecting to update its strategy document.

## Architecture

```
                        ┌─────────────────────────────────────┐
                        │           RIGID SHELL               │
                        │  (agent cannot modify)              │
                        │                                     │
                        │  Kraken Client    Risk Manager      │
                        │  Portfolio        Database          │
                        │  Telegram         Data API          │
                        └────────────┬────────────────────────┘
                                     │
                              IO Contract
                      SymbolData + Portfolio + RiskLimits in
                            list[Signal] out
                                     │
                        ┌────────────┴────────────────────────┐
                        │        FLEXIBLE MODULES             │
                        │  (AI agent rewrites)                │
                        │                                     │
                        │  strategy/active/strategy.py        │
                        │  statistics/active/market_analysis   │
                        │  statistics/active/trade_performance │
                        └─────────────────────────────────────┘
                                     │
                        ┌────────────┴────────────────────────┐
                        │         ORCHESTRATOR                │
                        │  (nightly, 3:30-6am EST)            │
                        │                                     │
                        │  Opus analyzes → Sonnet generates   │
                        │  → Opus reviews → backtest          │
                        │  → paper test → deploy              │
                        └─────────────────────────────────────┘
```

**Shell** (rigid): Kraken REST/WebSocket client, risk manager with hard limits, portfolio tracker, SQLite database, Telegram bot, and a REST/WebSocket data API. The AI agent cannot modify any of these.

**Strategy Module** (flexible): A single Python file (`strategy/active/strategy.py`) that the orchestrator can rewrite. Communicates with the shell through a strict IO contract: receives market data and portfolio state, returns trading signals.

**Orchestrator** (nightly): Runs between 3:30-6am EST. If a reflection cycle is due, Opus first reviews its predictions against evidence and rewrites the strategy document. Then it reviews ground-truth benchmarks, market analysis, and trade performance, and decides: do nothing, create a candidate strategy, promote one, or update the analysis modules. When it decides to change something, Sonnet generates code, Opus reviews it, and it's sandboxed and backtested before deployment.

## Key Features

- **Long-only** crypto trading across 9 pairs (BTC, ETH, SOL, XRP, DOGE, ADA, LINK, AVAX, DOT)
- **Multi-position per symbol** with tag-based tracking (core + swing positions simultaneously)
- **Hard risk limits** enforced by the shell (max position size, daily loss halt, drawdown halt, trade limits)
- **Exchange-native SL/TP** on Kraken (survive server downtime) with client-side simulation in paper mode
- **Candidate strategy system**: up to 3 candidate strategies run paper simulations alongside the active strategy
- **Institutional learning**: falsifiable predictions, periodic reflection cycles, strategy document versioning
- **MAE tracking**: max adverse excursion on all positions (fund + candidate), carried to trades on close
- **Strategy sandbox** with AST-based validation blocks dangerous code before execution
- **Paper and live modes** with identical logic paths and fill confirmation
- **Restart safety**: 9 landmine fixes — persistent starting capital, halt evaluation on startup, strategy DB fallback, orphaned position detection, config validation
- **Telegram observability** with 17 commands for monitoring, plus an emergency kill switch
- **REST API** (13 endpoints) and **WebSocket** event stream (19 event types) for programmatic access
- **Activity log**: unified timeline across all subsystems with REST + WebSocket endpoints
- **Observability stack**: Prometheus metrics (50+ gauges), Loki log aggregation, Grafana dashboard — all self-hosted
- **Truth benchmarks**: 28 rigid metrics computed from raw DB data (not AI-generated)
- **Full audit trail**: every signal, trade, prediction, orchestrator decision, and AI response is recorded

## Quick Start

```bash
# Clone and configure
git clone <repo-url> && cd trading-brain
cp .env.example .env    # Add your API keys

# Run with Docker
docker compose up -d

# View logs
docker compose logs -f
```

See [docs/DEPLOY.md](docs/DEPLOY.md) for full deployment instructions.

## Configuration

| File | Purpose |
|------|---------|
| `.env` | API keys (Kraken, Anthropic, Telegram, Data API) |
| `config/settings.toml` | Trading mode, symbols, AI provider, schedule |
| `config/risk_limits.toml` | Hard risk limits (position size, daily loss, drawdown) |

## Project Structure

```
src/
├── main.py                  # Brain class — scan loop, position monitor, scheduler
├── shell/                   # Rigid infrastructure (agent cannot modify)
│   ├── config.py            # Config loading and validation
│   ├── contract.py          # IO contract types (Signal, SymbolData, etc.)
│   ├── database.py          # SQLite with migrations
│   ├── kraken.py            # Kraken REST + WebSocket v2 client
│   ├── portfolio.py         # Position tracking, trade execution, P&L
│   ├── risk.py              # Hard risk limit enforcement
│   ├── truth.py             # 28 truth benchmark metrics
│   └── data_store.py        # Candle storage and aggregation
├── orchestrator/            # Nightly AI-driven strategy evolution
│   ├── orchestrator.py      # Full nightly cycle (reflect → analyze → generate → review → deploy)
│   ├── ai_client.py         # Anthropic API client with token tracking
│   └── reporter.py          # Performance reporting
├── candidates/              # Candidate strategy system
│   ├── runner.py            # Per-slot paper simulation engine
│   └── manager.py           # Lifecycle management (create, cancel, promote)
├── strategy/                # Strategy sandbox and validation
│   ├── loader.py            # Dynamic import with DB fallback
│   ├── sandbox.py           # AST-based code validation
│   └── backtester.py        # Historical backtesting engine
├── statistics/              # Analysis module infrastructure
│   ├── sandbox.py           # Analysis module validation
│   ├── loader.py            # Module loading and deployment
│   └── readonly_db.py       # Read-only DB wrapper for analysis
├── api/                     # Data API
│   ├── server.py            # aiohttp app with auth + error middleware
│   ├── routes.py            # 13 REST endpoints
│   ├── metrics.py           # Prometheus /metrics endpoint (50+ gauges)
│   └── websocket.py         # WebSocket event stream
└── telegram/                # Telegram bot
    ├── commands.py           # 17 bot commands
    └── notifications.py      # Dual dispatch (Telegram + WebSocket)

strategy/active/             # AI-rewritable strategy module
statistics/active/           # AI-rewritable analysis modules
config/                      # TOML configuration files
monitoring/                  # Prometheus, Grafana provisioning, dashboard JSON
tests/                       # 222 integration tests
docs/                        # Deployment guide and dev notes
deploy/                      # Ansible playbooks, VPS hardening, restart script
```

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/help` | System intro and command list |
| `/status` | System health: mode, active/paused/halted, last scan, uptime |
| `/health` | Fund metrics: portfolio value, daily P&L, drawdown, risk state |
| `/outlook` | Latest orchestrator observations (market summary, assessment) |
| `/positions` | Open positions with entry price, P&L, stops, tags |
| `/trades` | Last 10 completed trades |
| `/risk` | Risk limits and current utilization |
| `/performance` | Daily performance summary |
| `/strategy` | Active strategy version and description |
| `/tokens` | AI token usage and cost breakdown |
| `/ask` | Context-aware question to Haiku (portfolio + risk injected) |
| `/candidates` | Active candidate strategies and their performance |
| `/thoughts` | Browse orchestrator reasoning spool |
| `/thought` | View full AI response for a specific cycle step |
| `/reflect_tonight` | Schedule reflection for the next orchestration cycle |
| `/pause` / `/resume` | Pause/resume trading |
| `/kill` | Emergency stop — close all positions and shut down |

## Data API

Bearer token auth. Set `API_KEY` in `.env`.

**REST** (13 endpoints at `/v1/*`): system, portfolio, positions, trades, performance, risk, signals, strategy, ai/usage, benchmarks, activity, predictions, strategy-doc/versions.

**WebSocket** at `/v1/events?token=<API_KEY>`: real-time event stream (19 event types). Activity stream at `/v1/activity/live`.

**Prometheus** at `/metrics`: 50+ gauges including portfolio, risk, per-position, candidates, predictions, and reflection metrics.

All responses use an envelope format: `{data: ..., meta: {timestamp, mode, version}}`.

## Observability

Self-hosted monitoring stack (Prometheus + Loki + Grafana) on the same VPS:

- **Grafana dashboard** at port 3000 — fund overview, risk/trading metrics, per-position P&L, candidates, institutional learning, structured logs
- **Prometheus** scrapes `/metrics` every 30s — 50+ gauges with per-position/candidate labels, 90-day retention
- **Loki** ingests Docker container logs via the Loki log driver — structured JSON, queryable via LogQL
- **Memory budget**: ~1.46GB total (fits 2GB VPS with swap headroom)

## Development

```bash
# Install dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Run locally (paper mode)
python -m src.main
```

## Risk Model

The shell enforces hard limits as emergency backstops that the AI agent cannot override:

| Limit | Default | Description |
|-------|---------|-------------|
| Max position | 25% | Per-symbol portfolio allocation cap |
| Max positions | 5 | Concurrent open positions |
| Max trade | 10% | Per-trade portfolio allocation |
| Daily loss halt | 10% | Stops all trading for the day |
| Max drawdown | 40% | System halt from peak portfolio value |
| Rollback | 15% daily drop | Strategy rollback trigger |

These are intentionally wide — emergency circuit breakers, not operational constraints. The AI orchestrator self-regulates within these limits through strategy design. The shell always enforces limits before execution.

## License

Private / All rights reserved.
