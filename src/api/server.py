"""API Server — aiohttp app with auth middleware, REST routes, and WebSocket."""

from __future__ import annotations

import os
from datetime import datetime, timezone

import structlog
from aiohttp import web

from src.api import api_key_key, ctx_key
from src.api.routes import setup_routes
from src.api.websocket import WebSocketManager

log = structlog.get_logger()


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """Bearer token authentication. Skips WebSocket (handled separately)."""
    # WebSocket auth is handled in WebSocketManager.handle
    if request.path == "/v1/events":
        return await handler(request)

    api_key = request.app.get(api_key_key, "")
    if api_key:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] != api_key:
            return web.json_response(
                {"error": {"code": "unauthorized", "message": "Invalid or missing API key"}},
                status=401,
            )
    return await handler(request)


def create_app(
    config,
    db,
    portfolio,
    risk,
    ai,
    scan_state: dict,
    commands=None,
) -> tuple[web.Application, WebSocketManager]:
    """Create and configure the aiohttp application."""
    app = web.Application(middlewares=[auth_middleware])

    # Auth
    app[api_key_key] = os.getenv("API_KEY", "")

    # Shared context for route handlers
    app[ctx_key] = {
        "config": config,
        "db": db,
        "portfolio": portfolio,
        "risk": risk,
        "ai": ai,
        "scan_state": scan_state,
        "commands": commands,
        "started_at": datetime.now(timezone.utc),
    }

    # REST routes
    setup_routes(app)

    # WebSocket
    ws_manager = WebSocketManager()
    app.router.add_get("/v1/events", ws_manager.handle)

    return app, ws_manager
