# Trading Brain — Deployment Guide

## Prerequisites

- API keys:
  - **Kraken** — API key + secret (with trading permissions)
  - **Anthropic** — API key (or Google Vertex service account)
  - **Telegram** — Bot token + your chat ID
  - **API key** — Any secret string for REST/WebSocket auth

## Quick Start (Local Docker)

```bash
git clone <repo-url> && cd trading-brain
cp .env.example .env                        # Edit with your API keys
cp config/settings.example.toml config/settings.toml  # Edit settings
docker compose up -d
```

## Quick Start (Ansible — Recommended)

Automated deployment to a remote VPS. Handles Docker install, file sync, secrets, Caddy reverse proxy.

### 1. Setup VPS (one-time)

Run against a fresh VPS with root + password access:

```bash
cd deploy
ansible-playbook setup.yml -i "<VPS_IP>," -u root \
  --extra-vars "ansible_password=<ROOT_PASSWORD>"
```

This runs a full security setup:

- **Integrity check** — Verifies VPS image is clean (checks `/etc/ld.so.preload`, SUID binaries, rogue crons, unexpected users/ports). Fails immediately if anything suspicious is found.
- **System updates** — Full `apt upgrade` + installs `unattended-upgrades` for automatic security patches
- **Deploy user** — `trading` user with scoped sudo (docker/systemctl/apt only, not full root)
- **SSH hardening** — Key-only auth, no root login, no passwords, `MaxAuthTries 3`, `LoginGraceTime 30`
- **fail2ban** — Bans IPs after 5 failed SSH attempts for 1 hour
- **Firewall** — SSH (22) + HTTP (80) + HTTPS (443) only. No direct API port access.
- **Swap** — 2GB swapfile

The SSH key is saved to `deploy/keys/trading-brain`.

### 2. Configure inventory

```bash
cp inventory.yml.example inventory.yml
```

Edit `inventory.yml` with your VPS connection details and secrets:

```yaml
trading-brain:
  ansible_host: "1.2.3.4"
  ansible_user: "trading"
  ansible_ssh_private_key_file: "keys/trading-brain"
  ansible_become: true

  # Secrets
  kraken_api_key: "..."
  kraken_secret_key: "..."
  anthropic_api_key: "..."
  telegram_bot_token: "..."
  telegram_chat_id: "..."
  api_key: "..."
```

### 3. Deploy

```bash
ansible-playbook playbook.yml
```

### 4. Verify

```bash
# Check container status
ssh -i keys/trading-brain trading@<VPS_IP> \
  "docker compose -f /srv/trading-brain/docker-compose.yml logs --tail=30"

# Test API (via Caddy)
curl -H "Authorization: Bearer <API_KEY>" http://<VPS_IP>/v1/system
```

### Updating

After code changes, re-run the playbook. It only restarts the container when relevant files change:

```bash
# Full sync (only restarts if src/, config/, or build files changed)
ansible-playbook playbook.yml --tags sync

# Secrets only
ansible-playbook playbook.yml --tags secrets

# Caddy only (never touches trading-brain container)
ansible-playbook playbook.yml --tags caddy
```

### Remote Operations

```bash
# View logs
ssh -i deploy/keys/trading-brain trading@<VPS_IP> \
  "docker compose -f /srv/trading-brain/docker-compose.yml logs --tail=100"

# Query database
ssh -i deploy/keys/trading-brain trading@<VPS_IP> \
  "sqlite3 /srv/trading-brain/data/brain.db 'SELECT * FROM trades ORDER BY closed_at DESC LIMIT 10'"

# Restart container (re-reads .env)
ssh -i deploy/keys/trading-brain trading@<VPS_IP> \
  "cd /srv/trading-brain && docker compose up -d --force-recreate"
```

### Caddy Reverse Proxy

Caddy runs as a system service, reverse-proxying port 80 to the container's port 8080. To enable auto-HTTPS with a domain, edit `/etc/caddy/Caddyfile` on the VPS (or `deploy/templates/Caddyfile.j2` locally):

```
# Replace :80 with your domain for auto-HTTPS:
api.example.com {
    reverse_proxy localhost:8080
}
```

Then re-deploy: `ansible-playbook playbook.yml --tags caddy`

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

Copy from `config/settings.example.toml` and customize. Key settings:

| Setting | Default | Description |
|---------|---------|-------------|
| `general.mode` | `"paper"` | `"paper"` or `"live"` |
| `general.paper_balance_usd` | `200.0` | Starting paper balance |
| `general.timezone` | `"America/New_York"` | System timezone |
| `markets.symbols` | 9 pairs | Trading pairs (BTC, ETH, SOL, XRP, DOGE, ADA, LINK, AVAX, DOT) |
| `ai.provider` | `"anthropic"` | `"anthropic"` or `"vertex"` |
| `orchestrator.start_hour` | `3` | Nightly review start hour (EST) |
| `orchestrator.start_minute` | `30` | Nightly review start minute |
| `orchestrator.end_hour` | `6` | Nightly review end (EST) |
| `telegram.enabled` | `true` | Enable Telegram bot |
| `telegram.allowed_user_ids` | `[]` | Authorized Telegram user IDs (empty = deny all) |
| `api.enabled` | `false` | Enable REST + WebSocket API |

### `config/risk_limits.toml` — Risk Parameters

Controls hard limits enforced by the shell. Key settings:

| Setting | Default | Description |
|---------|---------|-------------|
| `position.max_position_pct` | `0.25` | Max 25% portfolio per position |
| `position.max_positions` | `5` | Max concurrent positions |
| `daily.max_daily_loss_pct` | `0.10` | Stop trading after 10% daily loss |
| `per_trade.max_trade_pct` | `0.10` | Max 10% per trade |
| `per_trade.default_trade_pct` | `0.03` | Default 3% per trade |
| `emergency.max_drawdown_pct` | `0.40` | 40% max drawdown halt |
| `rollback.max_daily_loss_pct` | `0.15` | 15% daily drop → strategy rollback |

## Running (Local Docker)

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
| `/thoughts` | Browse orchestrator reasoning spool |
| `/thought <id> <step>` | Full AI response for a specific cycle step |
| `/ask <question>` | Context-aware question to Haiku (portfolio + risk injected) |
| `/pause` | Pause trading (scans continue) |
| `/resume` | Resume trading, clear risk halt |
| `/kill` | Emergency stop — cancel all orders, close positions, shutdown |

## Operations

### Paper to Live

1. Edit `config/settings.toml`: change `mode = "live"`
2. Restart: `deploy/restart.sh` (or re-run Ansible playbook)
3. Verify via `/status` in Telegram

**Important**: `docker compose restart` does NOT re-read `.env` changes. Always use `deploy/restart.sh` or `docker compose up -d --force-recreate`.

### Adding Funds

Deposit to Kraken normally. The system detects balance changes via the `capital_events` table.

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
| Telegram commands don't work | Add your Telegram user ID to `allowed_user_ids` in settings.toml (empty list = deny all) |
| Stale PID lockfile | Delete `data/brain.pid` and restart |
| DB locked | Stop container, delete `data/brain.db-shm` and `data/brain.db-wal`, restart |
| Orchestrator not running | Runs nightly 3:30-6am EST. Check `/tokens` for recent activity |
| `.env` changes not applied | `docker compose restart` doesn't re-read `.env`. Use `deploy/restart.sh` |
| Strategy won't load | Check `strategy/active/strategy.py` exists. System falls back to DB, then paused mode |
