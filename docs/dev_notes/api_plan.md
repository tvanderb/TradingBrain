# Data API — Implementation Plan

> **Goal**: Programmatic read-only data access to a running Trading Brain instance. Any external software (dashboard, monitoring tool, statistical analysis, mobile app) can consume structured JSON over REST and receive live events over WebSocket.

## Architecture Overview

```
TradingBrain (existing asyncio process)
├── Scan Loop, Position Monitor, Orchestrator, etc.
├── Telegram Bot (existing — alerting)
└── API Server (NEW — aiohttp)
    ├── REST endpoints (/v1/...)
    ├── WebSocket endpoint (/v1/events)
    └── Auth middleware (bearer token)
```

**Same process, not a sidecar.** The API server runs inside TradingBrain's asyncio loop. Direct access to portfolio, risk, scan_state — no IPC, no DB contention.

## Phase 1: Event System Refactor

### 1.1 — Expand Notifier to cover all system events

Current Notifier has 8 methods. Add missing events:

| New Method | Where to call it | Currently |
|---|---|---|
| `risk_halt(reason)` | `risk.py` lines 141, 147, 159, 165 — when `_halted = True` | Silent |
| `risk_resumed()` | `risk.py` line 181 — when `clear_halt()` called | Silent |
| `signal_rejected(symbol, reason)` | `main.py` scan loop — when risk check fails | Silent |
| `scan_complete(symbol_count, signal_count)` | `main.py` — end of `_scan_loop` | Silent |
| `strategy_deployed(version, tier, changes)` | `orchestrator.py` line 911 — after successful deploy | Silent (bug) |
| `paper_test_started(version, days)` | `orchestrator.py` line 902 — after paper test created | Silent |
| `paper_test_completed(version, passed, results)` | `orchestrator.py` — in `_evaluate_paper_tests` | Silent |
| `orchestrator_cycle_started()` | `orchestrator.py` — start of `run_nightly_cycle` | Silent |
| `orchestrator_cycle_completed(decision_type)` | `orchestrator.py` — end of nightly cycle | Silent |
| `system_shutdown()` | `main.py` — in shutdown handler | Silent |

### 1.2 — Fix bug: strategy_change() never called

In `orchestrator.py` around line 911, after `log.info("orchestrator.strategy_deployed", ...)`:
- Call `self._notifier.strategy_change(version, actual_tier, changes)`
- Requires: pass `notifier` to Orchestrator constructor from `main.py`

### 1.3 — Fix bug: risk halts are silent

In `risk.py`, the RiskManager needs access to the Notifier. Options:
- **Option A**: Pass notifier to RiskManager constructor — simple but couples shell to telegram module
- **Option B**: Callback pattern — RiskManager accepts an `on_halt` callback, main.py wires it to notifier
- **Recommended: Option B** — keeps the shell decoupled. `RiskManager.__init__` accepts optional `on_halt: Callable` and `on_resume: Callable` callbacks.

### 1.4 — Dual-dispatch: WebSocket + Telegram

Refactor Notifier to dispatch each event to two targets:
1. **WebSocket broadcast** — always, every event, structured JSON
2. **Telegram message** — filtered by config

Internal pattern for each event method:
```python
async def trade_executed(self, trade: dict) -> None:
    event = {"event": "trade_executed", "data": {...}, "timestamp": "..."}
    await self._broadcast_ws(event)          # Always
    if self._tg_filter("trade_executed"):    # Config check
        await self._send_telegram(text)
```

### 1.5 — Telegram notification config

New section in `config/settings.toml`:

```toml
[telegram.notifications]
# Which events send Telegram alerts (all default true except noted)
trade_executed = true
stop_triggered = true
risk_halt = true
risk_resumed = true
rollback = true
strategy_deployed = true       # FIX: was silent, now defaults on
system_online = true
system_shutdown = true
system_error = true
websocket_failed = true
daily_summary = true
weekly_report = true
# High-frequency events — default off for Telegram
signal_rejected = false
scan_complete = false
paper_test_started = false
paper_test_completed = false
orchestrator_cycle_started = false
orchestrator_cycle_completed = false
```

## Phase 2: REST API

### 2.1 — Dependencies

Add to `pyproject.toml`:
```
"aiohttp>=3.9",
```

Why aiohttp:
- Already async (fits existing asyncio loop)
- Lightweight (no pydantic, uvicorn, starlette)
- Built-in WebSocket support
- Well-maintained, battle-tested

### 2.2 — Module structure

```
src/api/
├── __init__.py
├── server.py          # aiohttp app creation, startup/shutdown, auth middleware
├── routes.py          # REST endpoint handlers
└── websocket.py       # WebSocket connection manager + event broadcasting
```

### 2.3 — Auth

Bearer token from `.env`:
```
API_KEY=<random-string>
```

Middleware checks `Authorization: Bearer <token>` on every request. Returns 401 on mismatch. WebSocket authenticates on handshake (token as query param or first message).

### 2.4 — Response envelope

Every REST response:
```json
{
  "data": { ... },
  "meta": {
    "timestamp": "2026-02-09T03:15:00Z",
    "mode": "paper",
    "version": "2.0.0"
  }
}
```

Error responses:
```json
{
  "error": {
    "code": "not_found",
    "message": "Resource not found"
  },
  "meta": { ... }
}
```

### 2.5 — Endpoints

#### System

**`GET /v1/system`** — Health and system info
```json
{
  "status": "running",
  "mode": "paper",
  "uptime_seconds": 86400,
  "version": "2.0.0",
  "started_at": "2026-02-08T00:00:00Z",
  "last_scan": "2026-02-09T03:10:00Z",
  "paused": false,
  "halted": false,
  "halt_reason": null
}
```

#### Portfolio

**`GET /v1/portfolio`** — Portfolio snapshot
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

#### Positions

**`GET /v1/positions`** — Open positions
```json
[
  {
    "symbol": "BTC/USD",
    "qty": 0.001,
    "entry_price": 45000.00,
    "current_price": 44800.00,
    "unrealized_pnl": -0.20,
    "unrealized_pnl_pct": -0.44,
    "stop_loss": 44100.00,
    "take_profit": 47700.00,
    "opened_at": "2026-02-08T12:00:00Z"
  }
]
```

#### Trades

**`GET /v1/trades`** — Closed trade history
- Query params: `limit` (default 50, max 500), `since` (ISO datetime), `until` (ISO datetime), `symbol` (filter)
```json
[
  {
    "id": 42,
    "symbol": "ETH/USD",
    "action": "SELL",
    "qty": 0.05,
    "price": 3200.00,
    "fee": 0.64,
    "pnl": 12.50,
    "pnl_pct": 3.2,
    "strategy_version": "v20260208_010000",
    "executed_at": "2026-02-08T15:30:00Z"
  }
]
```

#### Performance

**`GET /v1/performance`** — Daily performance series
- Query params: `since` (ISO date), `until` (ISO date)
```json
[
  {
    "date": "2026-02-08",
    "portfolio_value": 215.50,
    "daily_pnl": 3.20,
    "daily_pnl_pct": 1.5,
    "trade_count": 4,
    "win_count": 3,
    "loss_count": 1,
    "fees": 1.28
  }
]
```

#### Risk

**`GET /v1/risk`** — Risk limits and current utilization
```json
{
  "limits": {
    "max_position_pct": 0.15,
    "max_positions": 5,
    "max_daily_loss_pct": 0.06,
    "max_drawdown_pct": 0.12,
    "max_daily_trades": 20
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

#### Market

**`GET /v1/market`** — Latest scan data per symbol
```json
[
  {
    "symbol": "BTC/USD",
    "price": 44800.00,
    "regime": "ranging",
    "trend": "neutral",
    "rsi": 52.3,
    "ema_fast": 44750.00,
    "ema_slow": 44600.00,
    "volume_ratio": 1.15,
    "signal": null,
    "scanned_at": "2026-02-09T03:10:00Z"
  }
]
```

#### Signals

**`GET /v1/signals`** — Generated signals
- Query params: `limit` (default 50, max 500), `since`, `until`, `symbol`, `action` (BUY/SELL/CLOSE)
```json
[
  {
    "id": 101,
    "symbol": "BTC/USD",
    "action": "BUY",
    "confidence": 0.72,
    "strategy_version": "v20260208_010000",
    "executed": true,
    "rejected_reason": null,
    "created_at": "2026-02-09T03:10:00Z"
  }
]
```

#### Strategy

**`GET /v1/strategy`** — Active strategy and version history
```json
{
  "active": {
    "version": "v20260208_010000",
    "code_hash": "a1b2c3d4",
    "risk_tier": 1,
    "description": "...",
    "deployed_at": "2026-02-08T01:00:00Z"
  },
  "paper_test": {
    "version": "v20260209_010000",
    "status": "running",
    "ends_at": "2026-02-12T01:00:00Z"
  },
  "recent_versions": [...]
}
```

#### AI Usage

**`GET /v1/ai/usage`** — Token consumption and costs
```json
{
  "today": {
    "total_tokens": 45000,
    "total_cost_usd": 0.21,
    "by_model": {
      "claude-opus-4-6": { "calls": 2, "tokens": 30000, "cost_usd": 0.18 },
      "claude-sonnet-4-5-20250929": { "calls": 1, "tokens": 15000, "cost_usd": 0.03 }
    },
    "budget_remaining": 1455000
  }
}
```

#### Benchmarks

**`GET /v1/benchmarks`** — Truth benchmarks (17 metrics)
```json
{
  "total_trades": 42,
  "win_rate": 0.62,
  "profit_factor": 1.85,
  "avg_win": 8.50,
  "avg_loss": -4.20,
  "max_drawdown": 0.08,
  "...": "..."
}
```

## Phase 3: WebSocket Event Stream

### 3.1 — Endpoint

**`GET /v1/events`** — WebSocket upgrade

Auth: `?token=<API_KEY>` query parameter on connection.

### 3.2 — Event format

Every event:
```json
{
  "event": "trade_executed",
  "data": { ... },
  "timestamp": "2026-02-09T03:10:00Z"
}
```

### 3.3 — Full event catalog

| Event | Frequency | Data |
|---|---|---|
| `trade_executed` | Per trade | symbol, action, qty, price, fee, pnl |
| `stop_triggered` | Per trigger | symbol, reason, price |
| `risk_halt` | Rare | reason |
| `risk_resumed` | Rare | — |
| `signal_generated` | Per scan (0-N) | symbol, action, confidence |
| `signal_rejected` | Per rejection | symbol, reason |
| `scan_complete` | Every 5 min | symbol_count, signal_count |
| `strategy_deployed` | Rare | version, tier, changes |
| `strategy_rollback` | Rare | reason, version |
| `paper_test_started` | Rare | version, days |
| `paper_test_completed` | Rare | version, passed, results |
| `orchestrator_cycle_started` | Nightly | — |
| `orchestrator_cycle_completed` | Nightly | decision_type |
| `system_online` | Startup | portfolio_value, positions |
| `system_shutdown` | Shutdown | — |
| `system_error` | On error | message |
| `websocket_feed_lost` | Rare | — |
| `daily_summary` | Nightly | summary text |
| `weekly_report` | Weekly | report text |

### 3.4 — Connection management

`WebSocketManager` class:
- Tracks connected clients (set of WebSocket connections)
- `broadcast(event)` sends to all connected clients
- Handles disconnects gracefully (remove from set)
- No message backlog — if client disconnects, missed events are gone (REST is the source of truth for historical data)

## Phase 4: Configuration & Integration

### 4.1 — Config additions

`config/settings.toml`:
```toml
[api]
enabled = true
host = "0.0.0.0"
port = 8080
```

`.env`:
```
API_KEY=<generated-random-string>
```

### 4.2 — Docker updates

`docker-compose.yml` — add port mapping:
```yaml
ports:
  - "8080:8080"
```

### 4.3 — Startup integration

In `TradingBrain.start()`:
1. Create aiohttp app
2. Register routes + WebSocket
3. Start on configured port
4. Pass WebSocket manager reference to Notifier

Shutdown: aiohttp app cleanup in `TradingBrain.stop()`.

## Implementation Order

1. **Event system refactor** (Phase 1) — fix bugs, expand Notifier, add config filtering
2. **API server skeleton** (Phase 2.1-2.4) — aiohttp app, auth, envelope middleware
3. **REST endpoints** (Phase 2.5) — one at a time, starting with `/system`, `/portfolio`, `/positions`
4. **WebSocket** (Phase 3) — event stream, wire to Notifier
5. **Remaining endpoints** — `/trades`, `/performance`, `/signals`, etc.
6. **Config + Docker** (Phase 4) — settings, port mapping
7. **Tests** — API endpoint tests, WebSocket tests, event dispatch tests

## Files Modified

| File | Change |
|---|---|
| `src/telegram/notifications.py` | Expand with new events, dual-dispatch (WS + Telegram) |
| `src/shell/risk.py` | Add on_halt/on_resume callbacks |
| `src/orchestrator/orchestrator.py` | Wire notifier for strategy_deployed, paper tests, cycle events |
| `src/main.py` | Start API server, wire callbacks, add scan_complete events |
| `config/settings.toml` | Add `[api]` and `[telegram.notifications]` sections |
| `.env.example` | Add `API_KEY` |
| `docker-compose.yml` | Add port mapping |
| `pyproject.toml` | Add `aiohttp` dependency |

## Files Created

| File | Purpose |
|---|---|
| `src/api/__init__.py` | Package init |
| `src/api/server.py` | aiohttp app, auth middleware, startup/shutdown |
| `src/api/routes.py` | REST endpoint handlers |
| `src/api/websocket.py` | WebSocket manager + event broadcasting |
