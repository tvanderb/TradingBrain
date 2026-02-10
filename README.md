# Trading Brain

An autonomous crypto trading fund managed by AI. During the day, the system trades on its own — scanning markets, generating signals, and executing trades within hard risk limits. Every night, an AI orchestrator reviews performance and market conditions and decides whether the trading strategy should evolve. Most nights, it doesn't change anything. When it does, the new code goes through generation, review, backtesting, and paper testing before deployment.

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
                        │  (nightly, 12am-3am EST)            │
                        │                                     │
                        │  Opus analyzes → Sonnet generates   │
                        │  → Opus reviews → backtest          │
                        │  → paper test → deploy              │
                        └─────────────────────────────────────┘
```

**Shell** (rigid): Kraken REST/WebSocket client, risk manager with hard limits, portfolio tracker, SQLite database, Telegram bot, and a REST/WebSocket data API. The AI agent cannot modify any of these.

**Strategy Module** (flexible): A single Python file (`strategy/active/strategy.py`) that the orchestrator can rewrite. Communicates with the shell through a strict IO contract: receives market data and portfolio state, returns trading signals.

**Orchestrator** (nightly): Runs between 12am-3am EST. Claude Opus reviews ground-truth benchmarks, market analysis, and trade performance, then decides: do nothing, tweak the strategy, restructure it, or update the analysis modules. When it decides to change something, Sonnet generates code, Opus reviews it, and it's sandboxed, backtested, and paper-tested before deployment.

## Key Features

- **Long-only** crypto trading across 9 pairs (BTC, ETH, SOL, XRP, DOGE, ADA, LINK, AVAX, DOT)
- **Hard risk limits** enforced by the shell (max position size, daily loss halt, drawdown halt, trade limits)
- **Strategy sandbox** with AST-based import analysis blocks dangerous code before execution
- **Paper and live modes** with identical logic paths
- **Telegram observability** with 15 commands for monitoring, plus an emergency kill switch
- **REST API** (11 endpoints) and **WebSocket** event stream for programmatic access
- **Truth benchmarks**: 21 rigid metrics computed from raw DB data (not AI-generated)
- **Full audit trail**: every signal, trade, orchestrator decision, and AI response is recorded

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
│   ├── truth.py             # 21 truth benchmark metrics
│   └── data_store.py        # Candle storage and aggregation
├── orchestrator/            # Nightly AI-driven strategy evolution
│   ├── orchestrator.py      # Full nightly cycle (analyze → generate → review → deploy)
│   ├── ai_client.py         # Anthropic API client with token tracking
│   └── reporter.py          # Performance reporting
├── strategy/                # Strategy sandbox and validation
│   ├── sandbox.py           # AST-based code validation
│   └── backtester.py        # Historical backtesting engine
├── statistics/              # Analysis module infrastructure
│   ├── sandbox.py           # Analysis module validation
│   ├── loader.py            # Module loading and deployment
│   └── readonly_db.py       # Read-only DB wrapper for analysis
├── api/                     # Data API
│   ├── server.py            # aiohttp app with auth + error middleware
│   ├── routes.py            # 11 REST endpoints
│   └── websocket.py         # WebSocket event stream
└── telegram/                # Telegram bot
    ├── commands.py           # 15 bot commands
    └── notifications.py      # Dual dispatch (Telegram + WebSocket)

strategy/active/             # AI-rewritable strategy module
statistics/active/           # AI-rewritable analysis modules
config/                      # TOML configuration files
tests/                       # 58 integration tests
docs/                        # Deployment guide and dev notes
```

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/start` | System intro and command list |
| `/status` | System mode, portfolio value, daily P&L |
| `/positions` | Open positions with entry price, P&L, stops |
| `/trades` | Last 10 completed trades |
| `/report` | Latest market scan (prices, indicators, signals) |
| `/risk` | Risk limits and current utilization |
| `/performance` | Daily performance summary |
| `/strategy` | Active strategy version and description |
| `/tokens` | AI token usage and cost breakdown |
| `/ask` | On-demand question to Claude |
| `/thoughts` | Browse orchestrator reasoning spool |
| `/thought` | View full AI response for a specific cycle step |
| `/pause` / `/resume` | Pause/resume trading |
| `/kill` | Emergency stop — close all positions and shut down |

## Data API

Bearer token auth. Set `API_KEY` in `.env`.

**REST** (11 endpoints at `/v1/*`): system, portfolio, positions, trades, performance, risk, market, signals, strategy, ai/usage, benchmarks.

**WebSocket** at `/v1/events?token=<API_KEY>`: real-time event stream (18 event types).

All responses use an envelope format: `{data: ..., meta: {timestamp, mode, version}}`.

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

The shell enforces hard limits that the AI agent cannot override:

| Limit | Default | Description |
|-------|---------|-------------|
| Max position | 15% | Per-symbol portfolio allocation cap |
| Max positions | 5 | Concurrent open positions |
| Max trade | 7% | Per-trade portfolio allocation |
| Daily loss halt | 6% | Stops all trading for the day |
| Max drawdown | 12% | System halt from peak portfolio value |

The AI orchestrator can adjust the *strategy* (what signals to generate), but the shell always enforces these limits before execution.

## License

Private / All rights reserved.
