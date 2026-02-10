"""WebSocket event stream — broadcasts system events to connected clients."""

from __future__ import annotations

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
        """Send event to all connected clients."""
        if not self._clients:
            return
        msg = json.dumps(event, default=str)
        closed = set()
        for ws in self._clients:
            try:
                await ws.send_str(msg)
            except Exception:
                closed.add(ws)
        if closed:
            self._clients -= closed
            log.warning("ws.clients_dropped", count=len(closed), remaining=len(self._clients))

    async def handle(self, request: web.Request) -> web.WebSocketResponse:
        """WebSocket endpoint handler. Auth via ?token= query param."""
        api_key = request.app.get(api_key_key, "")
        token = request.query.get("token", "")
        if not api_key:
            raise web.HTTPUnauthorized(text="API key not configured")
        if not hmac.compare_digest(token, api_key):
            raise web.HTTPUnauthorized(text="Invalid token")

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
