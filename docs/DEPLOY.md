# Trading Brain — Deployment Guide

## Prerequisites

- Docker and Docker Compose
- API keys:
  - **Kraken** — API key + secret (with trading permissions)
  - **Anthropic** — API key (or Google Vertex service account)
  - **Telegram** — Bot token + your chat ID

## Quick Start

```bash
git clone <repo-url> && cd trading-brain
cp .env.example .env          # Edit with your API keys
docker compose up -d
```

## Configuration

### `.env` — Secrets

```env
KRAKEN_API_KEY=           # Kraken API key
KRAKEN_SECRET_KEY=        # Kraken API secret
ANTHROPIC_API_KEY=        # Required if ai.provider = "anthropic"
TELEGRAM_BOT_TOKEN=       # Telegram bot token from @BotFather
TELEGRAM_CHAT_ID=         # Your Telegram user/chat ID
API_KEY=                  # Data API bearer token (required if api.enabled = true)
```

For Google Vertex instead of Anthropic, set `GOOGLE_APPLICATION_CREDENTIALS` to the path of your service account JSON and update `config/settings.toml` provider to `"vertex"`.

### `config/settings.toml` — System Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `general.mode` | `"paper"` | `"paper"` or `"live"` |
| `general.paper_balance_usd` | `200.0` | Starting paper balance |
| `general.timezone` | `"US/Eastern"` | System timezone |
| `markets.symbols` | 9 pairs | Trading pairs (BTC, ETH, SOL, XRP, DOGE, ADA, LINK, AVAX, DOT) |
| `ai.provider` | `"anthropic"` | `"anthropic"` or `"vertex"` |
| `orchestrator.start_hour` | `0` | Nightly review start (EST) |
| `orchestrator.end_hour` | `3` | Nightly review end (EST) |
| `telegram.enabled` | `true` | Enable Telegram bot |
| `telegram.allowed_user_ids` | `[]` | Authorized Telegram user IDs |

### `config/risk_limits.toml` — Risk Parameters

Controls hard limits enforced by the shell. Key settings:

| Setting | Default | Description |
|---------|---------|-------------|
| `position.max_position_pct` | `0.15` | Max 15% portfolio per position |
| `position.max_positions` | `5` | Max concurrent positions |
| `daily.max_daily_loss_pct` | `0.06` | Stop trading after 6% daily loss |
| `per_trade.default_trade_pct` | `0.03` | Default 3% per trade |
| `emergency.max_drawdown_pct` | `0.12` | 12% max drawdown halt |

## Running

```bash
# Start
docker compose up -d

# View logs
docker compose logs -f

# Stop
docker compose down

# Rebuild after code changes
docker compose up -d --build
```

## Monitoring — Telegram Commands

| Command | Description |
|---------|-------------|
| `/status` | System mode, portfolio value, daily P&L |
| `/positions` | Open positions with entry price, P&L, stops |
| `/trades` | Last 10 completed trades |
| `/report` | Latest market scan (prices, regime, indicators, signals) |
| `/risk` | Risk limits and current utilization |
| `/performance` | Daily performance summary |
| `/strategy` | Active strategy version and description |
| `/tokens` | AI token usage and cost breakdown |
| `/thoughts` | Browse orchestrator reasoning spool |
| `/thought <id> <step>` | Full AI response for a specific cycle step |
| `/ask <question>` | On-demand question to Claude |
| `/pause` | Pause trading (scans continue) |
| `/resume` | Resume trading, clear risk halt |
| `/kill` | Emergency stop — shutdown and close all positions |

## Operations

### Paper to Live

1. Edit `config/settings.toml`: change `mode = "live"`
2. Restart: `docker compose restart`
3. Verify via `/status` in Telegram

### Adding Funds

Deposit to Kraken normally. The system detects balance changes automatically.

### Emergency Stop

- **Telegram**: `/kill` — graceful shutdown, closes all positions
- **Manual**: `docker compose down` — stops container immediately

### Backups

The SQLite database is at `data/brain.db`. Back up this file while the container is stopped, or use `.backup` via sqlite3.

```bash
docker compose down
cp data/brain.db data/brain.db.bak
docker compose up -d
```

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Container exits immediately | Check `.env` has all required keys. Check logs: `docker compose logs` |
| "API key invalid" | Verify Kraken API key has trading permissions enabled |
| No Telegram messages | Verify `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Ensure `allowed_user_ids` includes your ID |
| Stale PID lockfile | Delete `data/brain.pid` and restart |
| DB locked | Stop container, delete `data/brain.db-shm` and `data/brain.db-wal`, restart |
| Orchestrator not running | Runs nightly 12-3am EST. Check `/tokens` for recent activity |
