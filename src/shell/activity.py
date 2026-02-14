"""Activity Log — unified fund timeline for observability.

Central writer for all system events. Writes to SQLite, pushes to
a dedicated WebSocket, and emits structlog entries.
"""

from __future__ import annotations

import asyncio
import hmac
import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import structlog
from aiohttp import web, WSMsgType

if TYPE_CHECKING:
    from src.api import api_key_key
    from src.shell.database import Database

log = structlog.get_logger()


class ActivityLogger:
    """Writes activity entries to DB, pushes to WebSocket, emits structlog."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._ws: ActivityWebSocketManager | None = None

    def set_ws_manager(self, ws: ActivityWebSocketManager) -> None:
        self._ws = ws

    async def log(
        self,
        category: str,
        summary: str,
        severity: str = "info",
        detail: dict | str | None = None,
    ) -> None:
        """Write an activity entry to DB, push to WS, emit structlog."""
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        # Serialize detail to JSON string for storage
        detail_str = None
        if detail is not None:
            if isinstance(detail, str):
                detail_str = detail
            else:
                try:
                    detail_str = json.dumps(detail, default=str)
                except (TypeError, ValueError):
                    detail_str = str(detail)

        await self._db.execute(
            "INSERT INTO activity_log (timestamp, category, severity, summary, detail) VALUES (?, ?, ?, ?, ?)",
            (ts, category, severity, summary, detail_str),
        )
        await self._db.commit()

        # structlog
        log.info("activity", category=category, severity=severity, summary=summary)

        # WebSocket push
        if self._ws:
            await self._ws.broadcast({
                "ts": ts,
                "cat": category,
                "sev": severity,
                "msg": summary,
            })

    # --- Convenience methods ---

    async def trade(self, summary: str, severity: str = "info", detail: dict | None = None) -> None:
        await self.log("TRADE", summary, severity, detail)

    async def risk(self, summary: str, severity: str = "info", detail: dict | None = None) -> None:
        await self.log("RISK", summary, severity, detail)

    async def system(self, summary: str, severity: str = "info", detail: dict | None = None) -> None:
        await self.log("SYSTEM", summary, severity, detail)

    async def scan(self, summary: str, severity: str = "info", detail: dict | None = None) -> None:
        await self.log("SCAN", summary, severity, detail)

    async def orch(self, summary: str, severity: str = "info", detail: dict | None = None) -> None:
        await self.log("ORCH", summary, severity, detail)

    async def strategy(self, summary: str, severity: str = "info", detail: dict | None = None) -> None:
        await self.log("STRATEGY", summary, severity, detail)

    async def candidate(self, summary: str, severity: str = "info", detail: dict | None = None) -> None:
        await self.log("CANDIDATE", summary, severity, detail)

    # --- Query methods ---

    async def recent(self, limit: int = 30) -> list[dict]:
        """Return last N entries in chronological order (oldest first)."""
        rows = await self._db.fetchall(
            "SELECT * FROM activity_log ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return list(reversed(rows))

    async def query(
        self,
        limit: int = 50,
        since: str | None = None,
        until: str | None = None,
        category: str | None = None,
        severity: str | None = None,
    ) -> list[dict]:
        """Filtered query for REST endpoint. Returns newest-first."""
        sql = "SELECT * FROM activity_log WHERE 1=1"
        params: list = []

        if since:
            sql += " AND timestamp >= ?"
            params.append(since)
        if until:
            sql += " AND timestamp <= ?"
            params.append(until)
        if category:
            sql += " AND category = ?"
            params.append(category)
        if severity:
            sql += " AND severity = ?"
            params.append(severity)

        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        return await self._db.fetchall(sql, tuple(params))


class ActivityWebSocketManager:
    """Dedicated WebSocket for activity log streaming.

    Backfills 20 entries on connect, then streams new entries live.
    """

    MAX_CLIENTS = 50

    def __init__(self) -> None:
        self._clients: set[web.WebSocketResponse] = set()
        self._db: Database | None = None

    def set_db(self, db: Database) -> None:
        self._db = db

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def broadcast(self, entry: dict) -> None:
        """Send entry to all connected clients."""
        if not self._clients:
            return
        msg = json.dumps(entry, default=str)
        clients = list(self._clients)
        results = await asyncio.gather(
            *[ws.send_str(msg) for ws in clients],
            return_exceptions=True,
        )
        closed = {clients[i] for i, r in enumerate(results) if isinstance(r, Exception)}
        if closed:
            self._clients -= closed

    async def handle(self, request: web.Request) -> web.WebSocketResponse:
        """WebSocket handler with auth and backfill."""
        from src.api import api_key_key

        api_key = request.app.get(api_key_key, "")
        token = request.query.get("token", "")
        if not api_key or not hmac.compare_digest(token, api_key):
            raise web.HTTPUnauthorized(text="Unauthorized")

        if len(self._clients) >= self.MAX_CLIENTS:
            raise web.HTTPServiceUnavailable(text="Too many WebSocket connections")

        ws = web.WebSocketResponse(heartbeat=30.0)
        await ws.prepare(request)
        self._clients.add(ws)

        # Backfill last 20 entries
        if self._db:
            rows = await self._db.fetchall(
                "SELECT timestamp, category, severity, summary FROM activity_log ORDER BY id DESC LIMIT 20"
            )
            for row in reversed(rows):
                try:
                    await ws.send_str(json.dumps({
                        "ts": row["timestamp"],
                        "cat": row["category"],
                        "sev": row["severity"],
                        "msg": row["summary"],
                    }))
                except Exception:
                    break

        try:
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            self._clients.discard(ws)

        return ws
