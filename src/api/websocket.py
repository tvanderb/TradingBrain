"""WebSocket event stream — broadcasts system events to connected clients."""

from __future__ import annotations

import json

import structlog
from aiohttp import web, WSMsgType

from src.api import api_key_key

log = structlog.get_logger()


class WebSocketManager:
    """Tracks connected WebSocket clients and broadcasts events."""

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
        self._clients -= closed

    async def handle(self, request: web.Request) -> web.WebSocketResponse:
        """WebSocket endpoint handler. Auth via ?token= query param."""
        api_key = request.app.get(api_key_key, "")
        token = request.query.get("token", "")
        if api_key and token != api_key:
            raise web.HTTPUnauthorized(text="Invalid token")

        ws = web.WebSocketResponse()
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
