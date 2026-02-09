# Trading Brain — Data API Reference

Read-only API for programmatic access to a running Trading Brain instance. Provides REST endpoints for querying system state and a WebSocket stream for live events.

**Base URL**: `http://<host>:8080/v1`

## Authentication

All requests require a bearer token set via the `API_KEY` environment variable.

**REST**: Include the token in the `Authorization` header:
```
Authorization: Bearer <your-api-key>
```

**WebSocket**: Pass the token as a query parameter:
```
ws://<host>:8080/v1/events?token=<your-api-key>
```

If no `API_KEY` is set in the environment, authentication is disabled (not recommended for production).

Unauthorized requests receive:
```json
{
  "error": {
    "code": "unauthorized",
    "message": "Invalid or missing API key"
  }
}
```

## Response Envelope

Every REST response is wrapped in an envelope:

```json
{
  "data": { ... },
  "meta": {
    "timestamp": "2026-02-09T03:15:00+00:00",
    "mode": "paper",
    "version": "2.0.0"
  }
}
```

| Field | Type | Description |
|---|---|---|
| `data` | object or array | The response payload |
| `meta.timestamp` | string | UTC ISO 8601 timestamp of the response |
| `meta.mode` | string | `"paper"` or `"live"` — the system's trading mode |
| `meta.version` | string | API version |

Error responses use a separate shape:
```json
{
  "error": {
    "code": "benchmark_error",
    "message": "Description of what went wrong"
  },
  "meta": { ... }
}
```

---

## REST Endpoints

### GET /v1/system

System health and status.

**Response:**
```json
{
  "status": "running",
  "mode": "paper",
  "uptime_seconds": 86400.5,
  "version": "2.0.0",
  "started_at": "2026-02-08T00:00:00+00:00",
  "last_scan": "2026-02-09T03:10:00",
  "paused": false,
  "halted": false,
  "halt_reason": null
}
```

| Field | Type | Description |
|---|---|---|
| `status` | string | Always `"running"` while API is reachable |
| `mode` | string | `"paper"` or `"live"` |
| `uptime_seconds` | float | Seconds since process started |
| `version` | string | System version |
| `started_at` | string | UTC ISO 8601 start time |
| `last_scan` | string or null | Timestamp of last completed market scan |
| `paused` | bool | Whether trading is paused via Telegram command |
| `halted` | bool | Whether risk manager has halted trading |
| `halt_reason` | string or null | Reason for halt (only present when halted) |

---

### GET /v1/portfolio

Current portfolio snapshot with live prices.

**Response:**
```json
{
  "total_value": 215.50,
  "cash": 180.00,
  "unrealized_pnl": -2.30,
  "position_count": 2,
  "allocation": {
    "cash_pct": 83.5,
    "positions_pct": 16.5
  }
}
```

| Field | Type | Description |
|---|---|---|
| `total_value` | float | Cash + position market value (USD) |
| `cash` | float | Available cash (USD) |
| `unrealized_pnl` | float | Market value minus cost basis across all positions |
| `position_count` | int | Number of open positions |
| `allocation.cash_pct` | float | Cash as percentage of portfolio |
| `allocation.positions_pct` | float | Positions as percentage of portfolio |

---

### GET /v1/positions

All open positions with live pricing.

**Response:**
```json
[
  {
    "symbol": "BTCUSD",
    "qty": 0.001,
    "entry_price": 45000.00,
    "current_price": 44800.00,
    "unrealized_pnl": -0.20,
    "unrealized_pnl_pct": -0.44,
    "stop_loss": 44100.00,
    "take_profit": 47700.00,
    "opened_at": "2026-02-08T12:00:00"
  }
]
```

| Field | Type | Description |
|---|---|---|
| `symbol` | string | Trading pair |
| `qty` | float | Position size |
| `entry_price` | float | Average entry price (from DB column `avg_entry`) |
| `current_price` | float or null | Latest price from scan (null if no recent data) |
| `unrealized_pnl` | float or null | `(current_price - entry_price) * qty` |
| `unrealized_pnl_pct` | float or null | `(current_price / entry_price - 1) * 100` |
| `stop_loss` | float or null | Stop loss price |
| `take_profit` | float or null | Take profit price |
| `opened_at` | string | When the position was opened |

---

### GET /v1/trades

Closed trade history. Returns raw rows from the `trades` table.

**Query Parameters:**

| Param | Type | Default | Description |
|---|---|---|---|
| `limit` | int | 50 | Max results (capped at 500) |
| `since` | string | — | Filter: `closed_at >= since` (ISO datetime) |
| `until` | string | — | Filter: `closed_at <= until` (ISO datetime) |
| `symbol` | string | — | Filter by trading pair |

**Response:**
```json
[
  {
    "id": 42,
    "symbol": "ETHUSD",
    "side": "long",
    "qty": 0.05,
    "entry_price": 3100.00,
    "exit_price": 3200.00,
    "pnl": 4.36,
    "pnl_pct": 0.032,
    "fees": 1.14,
    "intent": "DAY",
    "strategy_version": "v20260208_010000",
    "strategy_regime": "trending_up",
    "opened_at": "2026-02-08T12:00:00",
    "closed_at": "2026-02-08T15:30:00",
    "notes": null
  }
]
```

| Field | Type | Description |
|---|---|---|
| `id` | int | Trade ID |
| `symbol` | string | Trading pair |
| `side` | string | `"long"` |
| `qty` | float | Quantity traded |
| `entry_price` | float | Entry price |
| `exit_price` | float or null | Exit price (null if still open) |
| `pnl` | float or null | Realized profit/loss (USD) |
| `pnl_pct` | float or null | P&L as decimal ratio |
| `fees` | float | Total fees paid |
| `intent` | string | Trade intent (`"DAY"`, `"SWING"`, `"POSITION"`) |
| `strategy_version` | string | Strategy version that generated the signal |
| `strategy_regime` | string or null | Market regime at time of trade |
| `opened_at` | string | When the trade was opened |
| `closed_at` | string or null | When the trade was closed |
| `notes` | string or null | Additional notes |

---

### GET /v1/performance

Daily performance snapshots.

**Query Parameters:**

| Param | Type | Default | Description |
|---|---|---|---|
| `since` | string | — | Filter: `date >= since` (ISO date, e.g. `2026-02-01`) |
| `until` | string | — | Filter: `date <= until` (ISO date) |

**Response:**
```json
[
  {
    "id": 10,
    "date": "2026-02-08",
    "portfolio_value": 215.50,
    "cash": 180.00,
    "total_trades": 4,
    "wins": 3,
    "losses": 1,
    "gross_pnl": 5.48,
    "net_pnl": 3.20,
    "fees_total": 2.28,
    "max_drawdown_pct": 0.02,
    "win_rate": 0.75,
    "expectancy": 1.80,
    "sharpe": null,
    "strategy_version": "v20260208_010000",
    "notes": null
  }
]
```

| Field | Type | Description |
|---|---|---|
| `date` | string | Date (YYYY-MM-DD) |
| `portfolio_value` | float | End-of-day portfolio value |
| `cash` | float | End-of-day cash |
| `total_trades` | int | Trades closed that day |
| `wins` | int | Winning trades |
| `losses` | int | Losing trades |
| `gross_pnl` | float | P&L before fees |
| `net_pnl` | float | P&L after fees |
| `fees_total` | float | Total fees that day |
| `max_drawdown_pct` | float | Max intraday drawdown |
| `win_rate` | float | Wins / total trades |
| `expectancy` | float | Expected value per trade |
| `sharpe` | float or null | Sharpe ratio (null if insufficient data) |
| `strategy_version` | string | Active strategy version |

---

### GET /v1/risk

Risk limits (from config) and current utilization.

**Response:**
```json
{
  "limits": {
    "max_position_pct": 0.15,
    "max_positions": 5,
    "max_daily_loss_pct": 0.06,
    "max_drawdown_pct": 0.12,
    "max_daily_trades": 20,
    "max_trade_pct": 0.10
  },
  "current": {
    "daily_pnl": -1.20,
    "daily_pnl_pct": -0.006,
    "daily_trades": 3,
    "consecutive_losses": 1,
    "drawdown_pct": 0.02,
    "halted": false,
    "halt_reason": null
  }
}
```

| Field | Type | Description |
|---|---|---|
| `limits.max_position_pct` | float | Max single position as fraction of portfolio |
| `limits.max_positions` | int | Max concurrent positions |
| `limits.max_daily_loss_pct` | float | Max daily loss as fraction of portfolio |
| `limits.max_drawdown_pct` | float | Max drawdown before halt |
| `limits.max_daily_trades` | int | Max trades per day |
| `limits.max_trade_pct` | float | Max trade size as fraction of portfolio |
| `current.daily_pnl` | float | Today's realized P&L (USD) |
| `current.daily_pnl_pct` | float | Today's P&L as fraction of portfolio |
| `current.daily_trades` | int | Trades executed today |
| `current.consecutive_losses` | int | Current consecutive loss streak |
| `current.drawdown_pct` | float | Current drawdown from peak |
| `current.halted` | bool | Whether risk halt is active |
| `current.halt_reason` | string or null | Reason for halt |

---

### GET /v1/market

Latest scan data for each tracked symbol. Updates every scan cycle (~5 minutes).

**Response:**
```json
[
  {
    "symbol": "BTCUSD",
    "price": 44800.00,
    "regime": "ranging",
    "rsi": 52.3,
    "ema_fast": 44750.00,
    "ema_slow": 44600.00,
    "volume_ratio": 1.15,
    "spread": 0.0012,
    "signal": null
  }
]
```

| Field | Type | Description |
|---|---|---|
| `symbol` | string | Trading pair |
| `price` | float or null | Latest price |
| `regime` | string or null | Market regime classification |
| `rsi` | float or null | Relative Strength Index |
| `ema_fast` | float or null | Fast EMA value |
| `ema_slow` | float or null | Slow EMA value |
| `volume_ratio` | float or null | Current vs average volume ratio |
| `spread` | float or null | Bid-ask spread |
| `signal` | string or null | Signal generated this scan (e.g. `"BUY"`) |

---

### GET /v1/signals

Signal history from the strategy module.

**Query Parameters:**

| Param | Type | Default | Description |
|---|---|---|---|
| `limit` | int | 50 | Max results (capped at 500) |
| `since` | string | — | Filter: `created_at >= since` (ISO datetime) |
| `until` | string | — | Filter: `created_at <= until` (ISO datetime) |
| `symbol` | string | — | Filter by trading pair |
| `action` | string | — | Filter by action (`BUY`, `SELL`, `CLOSE`) |

**Response:**
```json
[
  {
    "id": 101,
    "symbol": "BTCUSD",
    "action": "BUY",
    "size_pct": 0.10,
    "confidence": 0.72,
    "intent": "DAY",
    "reasoning": "RSI oversold with bullish divergence",
    "strategy_version": "v20260208_010000",
    "strategy_regime": "trending_up",
    "acted_on": 1,
    "rejected_reason": null,
    "created_at": "2026-02-09T03:10:00"
  }
]
```

| Field | Type | Description |
|---|---|---|
| `id` | int | Signal ID |
| `symbol` | string | Trading pair |
| `action` | string | `"BUY"`, `"SELL"`, or `"CLOSE"` |
| `size_pct` | float | Requested position size as fraction of portfolio |
| `confidence` | float or null | Strategy confidence score (0-1) |
| `intent` | string or null | Trade intent |
| `reasoning` | string or null | Strategy's reasoning for the signal |
| `strategy_version` | string | Strategy version that generated this |
| `strategy_regime` | string or null | Market regime at generation time |
| `acted_on` | int | `1` if executed, `0` if not |
| `rejected_reason` | string or null | Why the signal was rejected (risk check failure, etc.) |
| `created_at` | string | When the signal was generated |

---

### GET /v1/strategy

Active strategy, running paper test, and recent version history.

**Response:**
```json
{
  "active": {
    "id": 3,
    "version": "v20260208_010000",
    "parent_version": "v20260207_010000",
    "code_hash": "a1b2c3d4",
    "risk_tier": 1,
    "description": "Added RSI divergence filter",
    "tags": "rsi,divergence",
    "backtest_result": "{...}",
    "paper_test_result": "{...}",
    "market_conditions": "ranging",
    "deployed_at": "2026-02-08T01:00:00",
    "retired_at": null,
    "created_at": "2026-02-08T00:45:00"
  },
  "paper_test": {
    "id": 2,
    "strategy_version": "v20260209_010000",
    "risk_tier": 2,
    "required_days": 3,
    "started_at": "2026-02-09T01:00:00",
    "ends_at": "2026-02-12T01:00:00",
    "status": "running",
    "result": null,
    "completed_at": null
  },
  "recent_versions": [ ... ]
}
```

| Field | Type | Description |
|---|---|---|
| `active` | object or null | Currently deployed strategy version |
| `active.version` | string | Version identifier |
| `active.parent_version` | string or null | What this version evolved from |
| `active.code_hash` | string | SHA hash of the strategy code |
| `active.risk_tier` | int | 1 (tweak), 2 (restructure), 3 (overhaul) |
| `active.description` | string or null | What changed |
| `active.deployed_at` | string | When it went live |
| `paper_test` | object or null | Currently running paper test (null if none) |
| `paper_test.status` | string | `"running"`, `"passed"`, or `"failed"` |
| `paper_test.ends_at` | string | When the test period ends |
| `recent_versions` | array | Last 10 strategy versions (same shape as `active`) |

---

### GET /v1/ai/usage

AI token consumption and costs for the current day.

**Response:**
```json
{
  "today": {
    "total_tokens": 45000,
    "total_cost_usd": 0.21,
    "budget_limit": 1500000,
    "budget_remaining": 1455000,
    "by_model": {
      "claude-opus-4-6": {
        "calls": 2,
        "tokens": 30000,
        "cost_usd": 0.18
      },
      "claude-sonnet-4-5-20250929": {
        "calls": 1,
        "tokens": 15000,
        "cost_usd": 0.03
      }
    }
  }
}
```

| Field | Type | Description |
|---|---|---|
| `today.total_tokens` | int | Total tokens used today |
| `today.total_cost_usd` | float | Total cost in USD |
| `today.budget_limit` | int | Daily token budget from config |
| `today.budget_remaining` | int | Tokens remaining before budget exhausted |
| `today.by_model` | object | Per-model breakdown |

---

### GET /v1/benchmarks

Truth benchmarks — 17 verifiable metrics computed from raw database data. These are ground truth calculations that the AI orchestrator cannot modify.

**Response:**
```json
{
  "trade_count": 42,
  "win_count": 26,
  "loss_count": 16,
  "win_rate": 0.619,
  "net_pnl": 28.50,
  "total_fees": 8.40,
  "avg_win": 3.20,
  "avg_loss": -1.80,
  "expectancy": 1.27,
  "consecutive_losses": 0,
  "portfolio_value": 228.50,
  "portfolio_cash": 180.00,
  "max_drawdown_pct": 0.08,
  "total_signals": 150,
  "acted_signals": 42,
  "signal_act_rate": 0.28,
  "total_scans": 8640,
  "first_scan_at": "2026-02-01T00:00:00",
  "last_scan_at": "2026-02-09T03:10:00",
  "current_strategy_version": "v20260208_010000",
  "strategy_version_count": 3
}
```

| Field | Type | Description |
|---|---|---|
| `trade_count` | int | Total closed trades |
| `win_count` | int | Trades with positive P&L |
| `loss_count` | int | Trades with zero or negative P&L |
| `win_rate` | float | win_count / trade_count |
| `net_pnl` | float | Sum of all realized P&L |
| `total_fees` | float | Sum of all fees paid |
| `avg_win` | float | Average P&L of winning trades |
| `avg_loss` | float | Average P&L of losing trades |
| `expectancy` | float | (win_rate * avg_win) + (loss_rate * avg_loss) |
| `consecutive_losses` | int | Current consecutive loss streak |
| `portfolio_value` | float or null | Latest daily snapshot value |
| `portfolio_cash` | float or null | Latest daily snapshot cash |
| `max_drawdown_pct` | float | Peak-to-trough drawdown from daily snapshots |
| `total_signals` | int | Total signals generated |
| `acted_signals` | int | Signals that were executed |
| `signal_act_rate` | float | acted / total signals |
| `total_scans` | int | Total market scans performed |
| `first_scan_at` | string or null | Timestamp of first scan |
| `last_scan_at` | string or null | Timestamp of most recent scan |
| `current_strategy_version` | string or null | Currently deployed strategy version |
| `strategy_version_count` | int | Total strategy versions created |

Returns HTTP 500 with error envelope if computation fails.

---

## WebSocket Event Stream

### Connection

```
ws://<host>:8080/v1/events?token=<your-api-key>
```

The connection is **server-to-client only**. Client messages are ignored. The server pushes events as they occur. There is no message backlog — if a client disconnects, missed events are gone. Use REST endpoints for historical data.

### Event Format

Every WebSocket message is a JSON object:

```json
{
  "event": "trade_executed",
  "data": { ... },
  "timestamp": "2026-02-09T03:10:00+00:00"
}
```

| Field | Type | Description |
|---|---|---|
| `event` | string | Event type identifier |
| `data` | object | Event-specific payload |
| `timestamp` | string | UTC ISO 8601 when the event occurred |

### Event Catalog

#### Trade Events

**`trade_executed`** — A trade was filled.
```json
{
  "event": "trade_executed",
  "data": {
    "action": "BUY",
    "symbol": "BTCUSD",
    "qty": 0.001,
    "price": 45000.00,
    "fee": 0.18,
    "intent": "DAY",
    "pnl": null,
    "pnl_pct": null
  }
}
```

**`stop_triggered`** — A stop loss or take profit was hit.
```json
{
  "event": "stop_triggered",
  "data": {
    "symbol": "BTCUSD",
    "reason": "stop_loss",
    "price": 44100.00
  }
}
```

**`signal_rejected`** — A strategy signal failed risk checks.
```json
{
  "event": "signal_rejected",
  "data": {
    "symbol": "BTCUSD",
    "action": "BUY",
    "reason": "max_positions_reached"
  }
}
```

#### Risk Events

**`risk_halt`** — Risk manager halted all trading.
```json
{
  "event": "risk_halt",
  "data": {
    "reason": "daily_loss_limit_exceeded"
  }
}
```

**`risk_resumed`** — Risk halt was cleared.
```json
{
  "event": "risk_resumed",
  "data": {}
}
```

**`strategy_rollback`** — Strategy was rolled back due to poor performance.
```json
{
  "event": "strategy_rollback",
  "data": {
    "reason": "consecutive_losses_exceeded",
    "version": "v20260207_010000"
  }
}
```

#### Scan Events

**`scan_complete`** — A market scan cycle finished.
```json
{
  "event": "scan_complete",
  "data": {
    "symbol_count": 9,
    "signal_count": 1
  }
}
```

#### Strategy Events

**`strategy_deployed`** — A new strategy version went live.
```json
{
  "event": "strategy_deployed",
  "data": {
    "version": "v20260209_010000",
    "tier": 1,
    "tier_name": "Tweak",
    "changes": "Adjusted RSI threshold from 30 to 28..."
  }
}
```
Note: `changes` is truncated to 500 characters.

**`paper_test_started`** — A new strategy version entered paper testing.
```json
{
  "event": "paper_test_started",
  "data": {
    "version": "v20260209_010000",
    "days": 3
  }
}
```

**`paper_test_completed`** — Paper test finished with results.
```json
{
  "event": "paper_test_completed",
  "data": {
    "version": "v20260209_010000",
    "passed": true,
    "results": {
      "trades": 5,
      "pnl": 2.30
    }
  }
}
```

#### Orchestrator Events

**`orchestrator_cycle_started`** — Nightly analysis cycle began.
```json
{
  "event": "orchestrator_cycle_started",
  "data": {}
}
```

**`orchestrator_cycle_completed`** — Nightly cycle finished.
```json
{
  "event": "orchestrator_cycle_completed",
  "data": {
    "decision_type": "NO_CHANGE"
  }
}
```
`decision_type` values: `"NO_CHANGE"`, `"TWEAK"`, `"RESTRUCTURE"`, `"OVERHAUL"`

#### System Events

**`system_online`** — System started up.
```json
{
  "event": "system_online",
  "data": {
    "portfolio_value": 215.50,
    "positions": 2
  }
}
```

**`system_shutdown`** — System is shutting down.
```json
{
  "event": "system_shutdown",
  "data": {}
}
```

**`system_error`** — An error occurred.
```json
{
  "event": "system_error",
  "data": {
    "message": "Kraken API timeout after 3 retries"
  }
}
```
Note: `message` is truncated to 500 characters.

**`websocket_feed_lost`** — Kraken price feed WebSocket disconnected permanently.
```json
{
  "event": "websocket_feed_lost",
  "data": {}
}
```

#### Summary Events

**`daily_summary`** — End-of-day summary.
```json
{
  "event": "daily_summary",
  "data": {
    "summary": "Daily summary text..."
  }
}
```

**`weekly_report`** — Weekly performance report.
```json
{
  "event": "weekly_report",
  "data": {
    "report": "Weekly report text..."
  }
}
```

---

## Configuration

Enable the API in `config/settings.toml`:

```toml
[api]
enabled = true    # default: false
host = "0.0.0.0"
port = 8080
```

Set the API key in `.env`:
```
API_KEY=your-secret-token-here
```

### Telegram Event Filtering

All events are always sent over WebSocket. Telegram delivery is configurable per event type in `config/settings.toml`:

```toml
[telegram.notifications]
trade_executed = true           # default: true
stop_triggered = true           # default: true
risk_halt = true                # default: true
risk_resumed = true             # default: true
rollback = true                 # default: true
strategy_deployed = true        # default: true
system_online = true            # default: true
system_shutdown = true          # default: true
system_error = true             # default: true
websocket_failed = true         # default: true
daily_summary = true            # default: true
weekly_report = true            # default: true
signal_rejected = false         # default: false (high frequency)
scan_complete = false           # default: false (high frequency)
paper_test_started = false      # default: false
paper_test_completed = false    # default: false
orchestrator_cycle_started = false   # default: false
orchestrator_cycle_completed = false # default: false
```
