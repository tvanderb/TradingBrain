"""WebSocket event stream — broadcasts system events to connected clients."""

from __future__ import annotations

import asyncio
import hmac
import json

import structlog
from aiohttp import web, WSMsgType

from src.api import api_key_key

log = structlog.get_logger()


class WebSocketManager:
    """Tracks connected WebSocket clients and broadcasts events."""

    MAX_CLIENTS = 50

    def __init__(self) -> None:
        self._clients: set[web.WebSocketResponse] = set()

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def broadcast(self, event: dict) -> None:
        """Send event to all connected clients concurrently."""
        if not self._clients:
            return
        msg = json.dumps(event, default=str)
        clients = list(self._clients)
        results = await asyncio.gather(
            *[ws.send_str(msg) for ws in clients],
            return_exceptions=True,
        )
        closed = {clients[i] for i, r in enumerate(results) if isinstance(r, Exception)}
        if closed:
            self._clients -= closed
            log.warning("ws.clients_dropped", count=len(closed), remaining=len(self._clients))

    async def handle(self, request: web.Request) -> web.WebSocketResponse:
        """WebSocket endpoint handler. Auth via ?token= query param."""
        api_key = request.app.get(api_key_key, "")
        token = request.query.get("token", "")
        if not api_key or not hmac.compare_digest(token, api_key):
            raise web.HTTPUnauthorized(text="Unauthorized")

        if len(self._clients) >= self.MAX_CLIENTS:
            raise web.HTTPServiceUnavailable(text="Too many WebSocket connections")

        ws = web.WebSocketResponse(heartbeat=30.0)
        await ws.prepare(request)
        self._clients.add(ws)
        log.info("ws.client_connected", clients=len(self._clients))

        try:
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
                # Clients don't send data — ignore messages
        finally:
            self._clients.discard(ws)
            log.info("ws.client_disconnected", clients=len(self._clients))

        return ws
