# Technical Gotchas

> Ongoing traps that affect development. One-time fixes have been pruned.

## Kraken API

**Pair format divergence**: REST accepts `BTCUSD` but returns `XXBTZUSD`. WebSocket v2 uses `XBT/USD` for BTC, `XDG/USD` for DOGE. All others standard. `PAIR_REVERSE` dict handles mapping.

**txid extraction**: `result["txid"]` may be an empty list, not None. Safe pattern: `(result.get("txid") or [None])[0]`.

**Fee tiers**: Published rates (0.16/0.26%) are for higher volume. At $0 volume: 0.25% maker / 0.40% taker. Round-trip 0.65-0.80%.

## SQLite

**`datetime('now')` is local**: Always use `datetime('now', 'utc')` for consistent timestamps.

**`isoformat()` vs SQLite format**: Python `isoformat()` produces `T` separator + `+00:00` suffix. SQLite datetime functions expect space separator, no suffix. Use `strftime('%Y-%m-%d %H:%M:%S')` for all SQLite datetime comparisons.

**`LIMIT -1` returns ALL rows**: Documented SQLite behavior. Guard with `max(1, ...)` on user-supplied limits.

**Can't DROP CONSTRAINT**: Must recreate table to remove constraints. Position table migration requires DROP + CREATE + backfill, wrapped in `BEGIN IMMEDIATE` / `COMMIT`.

## Python / asyncio

**PID lockfile**: `ProcessLookupError` NOT `ProcessNotFoundError` (doesn't exist in Python). Also catch `PermissionError` (process exists but owned by another user).

**asyncio.Lock serializes all trade paths**: Scan loop, SL/TP triggers, conditional orders, emergency stop, reconciliation all go through `_trade_lock`. Any deadlock blocks everything. `_analyzing` flag guards strategy callbacks during executor thread.

**Python 3.14 local import scoping**: Functions with local `import` statements AND module-level import references in `except` blocks cause `cannot access local variable` errors. Move all imports to module level.

## Sandbox

**BaseException not Exception**: Strategy `except Exception` lets `SystemExit`/`KeyboardInterrupt` through. Sandbox catches `BaseException`.

**Transitive src imports**: `import src.shell.config; src.shell.config.os.system("cmd")` bypasses checks. `ALLOWED_SRC_IMPORTS` allowlist (`src.shell.contract` only).

**Name-mangled attrs**: `_ReadOnlyDB__conn` bypasses AST checks. Regex `_\w+__\w+` blocks all name-mangled access.

**ReadOnlyDB**: Strips null bytes from queries. Blocks `LOAD_EXTENSION`. PRAGMA function-call syntax `PRAGMA foo(value)` blocked by `[=(]` in pattern.

## Docker / Deployment

**`docker compose restart` doesn't re-read `.env`**: Must use `docker compose up -d --force-recreate`.

**VPS file ownership**: Ansible initial sync creates files owned by macOS UID 501. Run `sudo chown -R trading:trading /srv/trading-brain/`. Only after initial Ansible setup.

**Docker legacy builder multiline RUN**: Newlines in Python scripts become Dockerfile instructions. Collapse to single line or install buildx.

**rsync --itemize-changes**: Use `'^[<>c*]'` to match all transfer types (`<` sent, `>` received, `c` local, `*` messages).

## Prometheus / Grafana

**`float("inf")` breaks Prometheus**: `profit_factor` can be infinite. Guard: `pf if pf != float("inf") else 0`.

**Truth cache cross-test contamination**: Tests sharing module-level `_truth_cache` need to clear `_truth_cache["data"] = None` in setup/teardown.

**Library panels lost on volume wipe**: Stored in Grafana's internal DB, not on disk. All panels must be inline. Run `python3 monitoring/build_dashboard.py` to regenerate.

**Provisioned dashboards**: Cannot update via Grafana API — must restart Grafana to re-provision from disk.

## Telegram

**Bot session conflict on restart**: ~10s of `Conflict: terminated by other getUpdates request`. Transient — use `drop_pending_updates=True` in `start_polling()`.

## aiohttp

**Content-Type with charset**: `web.Response(content_type="text/plain; ...")` raises ValueError. Set `resp.headers["Content-Type"]` directly for Prometheus output.

## Loki

**Docker label mapping**: `service` → `compose_service` in LogQL. Query: `{compose_service="trading-brain"}`.

**Plugin version tags**: Semver tags don't exist. Always use `grafana/loki-docker-driver:latest`.
