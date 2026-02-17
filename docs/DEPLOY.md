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

#### Quick deploy (recommended for daily use)

The `deploy.sh` script detects what changed and picks the minimum-downtime action:

```bash
deploy/deploy.sh              # Deploy everything
deploy/deploy.sh --dry-run    # Preview without applying
```

| Changed files | Action | Downtime |
|---------------|--------|----------|
| `config/`, `strategy/`, `statistics/` only | SIGHUP → live config reload | **Zero** |
| `src/` or `docker-compose.yml` | Container restart | **~5 seconds** |
| `pyproject.toml` | Image rebuild + restart | **15-20 minutes** |
| Nothing | No action | None |

The script reads SSH connection details from `deploy/inventory.yml` (same source of truth as Ansible).

#### Ansible (full infrastructure)

For infrastructure changes (secrets, Caddy, Docker setup), use the Ansible playbook:

```bash
# Full sync (handler picks reload/restart/rebuild based on what changed)
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

## Live Config Reload

The system supports hot-reloading configuration without restarting the container. Trigger via:

- **SIGHUP signal**: `docker kill --signal=HUP trading-brain`
- **Telegram**: `/reload` command
- **Deploy script**: `deploy/deploy.sh` sends SIGHUP when only config/strategy files changed

After reload, a `config_reloaded` notification is sent with the list of updated, refused, and errored fields.

### Reloadable Fields

| Field group | Config path | What happens on reload |
|-------------|-------------|----------------------|
| Risk limits | `config/risk_limits.toml` (all sections) | RiskManager config replaced under trade lock |
| Notification filters | `telegram.notifications.*` | Notifier filter replaced; takes effect on next event |
| Orchestrator schedule | `orchestrator.start_hour`, `start_minute` | Orchestration cron job rescheduled |
| Orchestrator settings | `orchestrator.end_hour`, `max_revisions`, etc. | Config updated in-place |
| Fee check interval | `fees.check_interval_hours` | Fee check job rescheduled |
| Data retention | `data.candle_*_retention_*` | Config updated; takes effect on next pruning |
| AI models | `ai.sonnet_model`, `opus_model`, `haiku_model` | Config updated; next AI call uses new model |
| AI token limit | `ai.daily_token_limit` | Config updated in-place |
| Kraken fee defaults | `kraken.maker_fee_pct`, `taker_fee_pct` | Updated; overridden by per-pair fees from API |
| Slippage factor | `general.default_slippage_factor` | Config updated in-place |
| Log level | `general.log_level` | Logging reconfigured immediately |
| Allowed Telegram users | `telegram.allowed_user_ids` | Updated; takes effect on next command |
| Strategy code | `strategy/active/strategy.py` | Reloaded if file hash changed; state restored from DB |

### Immutable Fields (require restart)

| Field | Reason |
|-------|--------|
| `general.mode` | Fundamentally changes execution paths (paper vs live) |
| `markets.symbols` | Affects WebSocket subscriptions, data store, scan loop |
| `general.paper_balance_usd` | Meaningless after startup (portfolio already initialized) |
| `db_path` | Requires database reconnection |
| `telegram.bot_token` | Requires Telegram bot restart |
| `telegram.chat_id` | Requires Telegram bot restart |
| `kraken.api_key`, `secret_key` | Requires REST client restart |
| `api.host`, `api.port` | Requires API server restart |

If an immutable field is changed in the config file and a reload is triggered, the change is **refused** (logged with reason, old value kept) and reported in the `config_reloaded` notification.

If the config file has a parse error, the entire reload is aborted (old config preserved) and an error notification is sent.

## Running (Local Docker)

```bash
# Start
docker compose up -d

# View logs
docker compose logs -f

# Stop
docker compose down

# Rebuild after dependency changes (pyproject.toml)
docker compose up -d --build

# Restart after code changes (src/ is volume-mounted)
docker compose up -d --force-recreate

# Reload config without restart (config/, strategy/, statistics/)
docker kill --signal=HUP trading-brain
```

> **Note**: Application code (`src/`) is volume-mounted into the container, not baked into the image.
> The Docker image only contains pip dependencies. Code changes take effect on container restart,
> config/strategy changes take effect on SIGHUP.

## Monitoring — Telegram Commands

| Command | Description |
|---------|-------------|
| `/help` | System intro and command list |
| `/fund` | Portfolio overview: value, returns, drawdown, trade stats |
| `/positions` | Open positions with live P&L, stops, tags |
| `/trades` | Recent closed trades with entry/exit prices |
| `/risk` | Risk limits and current utilization |
| `/outlook` | Orchestrator's latest market view |
| `/candidates` | Candidate strategy status across all slots |
| `/thoughts` | Browse orchestrator reasoning spool |
| `/ask <question>` | Context-aware question to Haiku (portfolio + risk injected) |
| `/orchestrate` | Manually trigger nightly orchestration cycle |
| `/reflect` | Schedule reflection for the next orchestration cycle |
| `/reload` | Hot-reload config from disk (zero downtime) |
| `/pause` | Pause trading (scans continue) |
| `/resume` | Resume trading, clear risk halt |
| `/kill` | Emergency stop — cancel all orders, close positions, shutdown |

## Operations

### Paper to Live

1. Edit `config/settings.toml`: change `mode = "live"`
2. Restart container: `docker compose up -d --force-recreate` (or `deploy/deploy.sh`)
3. Verify via `/fund` in Telegram

**Note**: `mode` is an immutable field — it cannot be live-reloaded via SIGHUP. A container restart is required.

**Important**: `docker compose restart` does NOT re-read `.env` changes. Always use `docker compose up -d --force-recreate`.

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
| Orchestrator not running | Runs nightly 3:30-6am EST. Check `/thoughts` for recent activity |
| `.env` changes not applied | `docker compose restart` doesn't re-read `.env`. Use `deploy/restart.sh` |
| Strategy won't load | Check `strategy/active/strategy.py` exists. System falls back to DB, then paused mode |
