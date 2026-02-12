# Technical Gotchas & Fixes

## macOS Python 3.14 SSL Certificates
**Problem**: `websockets` library fails with `[SSL: CERTIFICATE_VERIFY_FAILED]` on macOS
**Fix**: Use certifi's CA bundle explicitly:
```python
import ssl, certifi
ssl_ctx = ssl.create_default_context(cafile=certifi.where())
async with websockets.connect(url, ssl=ssl_ctx) as ws: ...
```
**Note for VPS**: Linux usually has system certs, but keep this fix as fallback

## python-telegram-bot Empty Init
**Problem**: First `pip install python-telegram-bot` sometimes installs with empty `telegram/__init__.py`
**Fix**: `pip install --force-reinstall python-telegram-bot`
**When**: Happens on first install, not consistent. May recur on VPS deployment.

## APScheduler Interval First Run
**Problem**: `interval` trigger waits for one full interval before first execution (5 min wait for first scan)
**Fix**: `scheduler.add_job(fn, "interval", minutes=5, next_run_time=datetime.now())`
**Key**: Import `from datetime import datetime` — use `datetime.now()` not `datetime.utcnow()`

## Structlog Output Buffering
**Problem**: JSON log lines don't appear in real-time when running as background process
**Fix**: `PYTHONUNBUFFERED=1` environment variable before `python -m src.main`

## WebSocket Infinite Retry Loop
**Problem**: SSL failure causes endless reconnect attempts that never succeed
**Fix**: Counter with 3-failure fallback to REST polling:
```python
ws_failures = 0
while running:
    try:
        await ws_loop()
        ws_failures = 0
    except:
        ws_failures += 1
        if ws_failures >= 3:
            await poll_fallback()
            return
```

## pyproject.toml Build Backend
**Problem**: `setuptools.backends._legacy:_Backend` doesn't exist on Python 3.14
**Fix**: Use `setuptools.build_meta` instead

## Telegram Bot Conflict on Restart
**Problem**: Restarting bot causes `Conflict: terminated by other getUpdates request` errors for ~30 seconds
**Why**: Telegram's long-polling keeps old connection alive briefly
**Fix**: This is transient — the library auto-retries and resolves itself. Add `drop_pending_updates=True` to `start_polling()` to avoid processing stale commands.

## Kraken Fee Tiers vs Published Rates
**Problem**: Published rates (0.16% maker / 0.26% taker) are for higher volume tiers
**Reality**: At $0 30-day volume: 0.25% maker / 0.40% taker
**Impact**: Round-trip cost is 0.65-0.80%, much higher than expected. Must factor into all trade decisions.

## Kraken Pair Format
**Problem**: Kraken REST API uses different pair names than standard format
**Mapping**: `BTC/USD` -> `XBTUSD`, `ETH/USD` -> `ETHUSD`, `SOL/USD` -> `SOLUSD`
**Note**: WebSocket v2 uses standard format (`BTC/USD`), REST uses Kraken format (`XBTUSD`)

## Multiple Instance Prevention
**Problem**: Running `python3 -m src.main` multiple times (e.g. during testing) spawns duplicate bots. All instances poll the same Telegram token, causing `Conflict: terminated by other getUpdates request` errors and event loop starvation (missed 14/18 scheduled scans in 1.5 hours).
**Fix**: PID lockfile at `data/brain.pid`. On startup, checks if PID is alive with `os.kill(pid, 0)`. Uses `ProcessLookupError` + `PermissionError` exceptions (NOT `ProcessNotFoundError` — that doesn't exist in Python). Auto-cleaned via `atexit` and explicit cleanup in `finally` block.
**Note**: `pkill -f` may not terminate processes — use `kill -9 <pid>` if needed. After force-killing, also delete `brain.db-wal` and `brain.db-shm` (stale WAL files cause `disk I/O error`).

## Ansible Handler Ordering (SSH Lockout)
**Problem**: Ansible handlers run at END of play, AFTER all tasks. If you harden sshd_config and `notify: restart sshd`, the SSH verify task runs against the OLD config (passes), then the handler restarts sshd with the new config — if the new config is broken, you're locked out.
**Fix**: Add `meta: flush_handlers` before any verify/connectivity-test tasks. Also run `sshd -t` before flushing to catch config errors while still connected.
**Also**: `UsePAM no` breaks Debian's sshd (compiled against PAM). `ChallengeResponseAuthentication` is deprecated in OpenSSH 8.7+ — use `KbdInteractiveAuthentication` instead.

## Shell Escaping in Inline Python
**Problem**: Running Python one-liners with `$` in f-strings gets eaten by bash substitution
**Fix**: Use standalone `.py` test files instead of inline scripts

## Kraken WebSocket Pair Names
**Problem**: WebSocket v2 uses different pair names than config format
**Mapping**: `BTC/USD` → `XBT/USD`, `DOGE/USD` → `XDG/USD`. All others use standard format.
**REST quirk**: REST accepts `BTCUSD` but returns `XXBTZUSD` in responses.

## Kraken txid Extraction
**Problem**: `result["txid"]` may be an empty list, not None
**Fix**: `(result.get("txid") or [None])[0]` — handles both None and empty list

## SQLite `datetime('now')` Is Local Time
**Problem**: `datetime('now')` uses server timezone, not UTC
**Fix**: Always use `datetime('now', 'utc')` for consistent timestamps

## SQLite Paper Test `ends_at` Format
**Problem**: `datetime.isoformat()` includes timezone offset, which SQLite datetime functions don't handle
**Fix**: Use `strftime('%Y-%m-%d %H:%M:%S')` format for all SQLite datetime comparisons

## SQLite LIMIT -1
**Gotcha**: `LIMIT -1` returns ALL rows in SQLite (documented behavior). Useful but surprising.

## SQLite Can't DROP CONSTRAINT
**Gotcha**: No `ALTER TABLE DROP CONSTRAINT` in SQLite. Must recreate table to remove constraints.
**Impact**: Position table migration (adding `tag` column) requires DROP + CREATE + backfill, wrapped in transaction.

## Sandbox: BaseException Not Exception
**Problem**: Strategy code catching `Exception` still lets `SystemExit`/`KeyboardInterrupt` through
**Fix**: Sandbox AST walk checks for `BaseException` catches. Also blocks `operator` module and name-mangled attributes (`_ClassName__attr`).

## asyncio.Lock Serializes All Trade Paths
**Gotcha**: The trade lock (`self._trade_lock`) serializes ALL trade execution — scan loop signals, SL/TP triggers, conditional orders, emergency stop, reconciliation. Any deadlock blocks everything.
**`_analyzing` flag**: Guards strategy callbacks from position monitor during executor thread. Must be set/cleared atomically.

## Docker `compose restart` Doesn't Re-Read `.env`
**Problem**: `docker compose restart` restarts the container with the OLD environment. `.env` changes are NOT applied.
**Fix**: Must use `docker compose up -d --force-recreate` or the `deploy/restart.sh` helper script.

## PID Lockfile on macOS
**Problem**: Python has no `ProcessNotFoundError` exception
**Fix**: Use `ProcessLookupError` for `os.kill(pid, 0)` checks. Also catch `PermissionError` (process exists but owned by another user).

## ReadOnlyDB Null-Byte Injection
**Problem**: Null bytes in SQL queries can bypass text-based blocking
**Fix**: `ReadOnlyDB` strips null bytes from all queries before validation. Also blocks `LOAD_EXTENSION`.

## Telegram Bot Session Conflict
**Problem**: If Telegram was polling from a previous instance, starting a new one causes ~10s of `Conflict: terminated by other getUpdates request`
**Fix**: `telegram.waiting_for_session_release` with 10s delay on startup. `drop_pending_updates=True` in `start_polling()`.

## Paper Cash Phantom Profit (L1)
**Problem**: If `daily_performance` is empty, portfolio fell back to `config.paper_balance_usd` as starting value. Changing config or having positions at startup caused phantom profit/loss.
**Fix**: Store starting capital in `system_meta` table, always reconcile from first principles: `starting_capital + deposits + total_pnl - position_costs`.

## Special Migration Crash Risk (L8)
**Problem**: Positions table recreation (DROP → CREATE → INSERT) without explicit transaction. Crash between DROP and INSERT loses all position data.
**Fix**: Wrap in `BEGIN IMMEDIATE` / `COMMIT` with rollback on error.

## aiohttp Content-Type with charset (N1)
**Problem**: `web.Response(content_type="text/plain; version=0.0.4; charset=utf-8")` raises `ValueError: charset must not be in content_type argument`. aiohttp parses charset separately.
**Fix**: Create `web.Response(body=output)`, then set `resp.headers["Content-Type"]` directly to the full Prometheus content type string.

## Loki Docker log driver version tags (N2)
**Problem**: `docker plugin install grafana/loki-docker-driver:3.4.0` fails with "not found". Same for `3.6.0`. The Docker plugin registry doesn't publish semver tags.
**Fix**: Always use `grafana/loki-docker-driver:latest`.

## Docker Compose `$` in .env passwords (N3)
**Problem**: Passwords containing `$` in `.env` cause Docker Compose warnings like `The "XOw80T" variable is not set` — `$` triggers variable interpolation.
**Fix**: In Jinja2 templates, escape with `{{ password | replace('$', '$$') }}`.

## Loki Docker label name mapping (N4)
**Problem**: Docker Compose `service` key maps to `compose_service` label in Loki, not `service`. LogQL query `{service="trading-brain"}` returns nothing.
**Fix**: Use `{compose_service="trading-brain"}` in LogQL queries.

## UFW SSH lockout on fresh deploy (N5)
**Problem**: Deployment playbook had firewall rules for ports 80, 443, 3000 but not 22. On a fresh VPS where `setup.yml` set the initial rules, subsequent `playbook.yml` runs could interact with UFW without ensuring SSH is allowed.
**Fix**: Added explicit `ufw allow 22/tcp` rule to `playbook.yml`, placed before all other firewall rules.
