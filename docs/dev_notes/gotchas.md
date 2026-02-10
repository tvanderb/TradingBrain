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
