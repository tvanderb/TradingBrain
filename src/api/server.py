"""API Server — aiohttp app with auth middleware, REST routes, and WebSocket."""

from __future__ import annotations

import hmac
import os
from datetime import datetime, timezone

import structlog
from aiohttp import web

from src.api import api_key_key, ctx_key
from src.api.metrics import metrics_handler
from src.api.routes import setup_routes
from src.api.websocket import WebSocketManager
from src.shell.activity import ActivityWebSocketManager

log = structlog.get_logger()


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """Bearer token authentication. Skips WebSocket (handled separately)."""
    # WebSocket auth is handled in their respective managers
    if request.path in ("/v1/events", "/v1/activity/live", "/metrics"):
        return await handler(request)

    api_key = request.app.get(api_key_key, "")
    if not api_key:
        # No API key configured — reject all requests
        return web.json_response(
            {"error": {"code": "unauthorized", "message": "API key not configured"}},
            status=401,
        )
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or not hmac.compare_digest(auth[7:], api_key):
        return web.json_response(
            {"error": {"code": "unauthorized", "message": "Invalid or missing API key"}},
            status=401,
        )
    return await handler(request)


@web.middleware
async def error_middleware(request: web.Request, handler):
    """Catch unhandled exceptions and return generic error (no tracebacks to clients)."""
    try:
        return await handler(request)
    except web.HTTPException:
        raise  # Let aiohttp handle HTTP errors (401, 404, etc.)
    except Exception as e:
        log.error("api.unhandled_error", path=request.path, error=str(e),
                  error_type=type(e).__name__)
        return web.json_response(
            {
                "error": {"code": "internal_error", "message": "An unexpected error occurred"},
                "meta": {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "version": "2.0.0",
                },
            },
            status=500,
        )


def create_app(
    config,
    db,
    portfolio,
    risk,
    ai,
    scan_state: dict,
    commands=None,
    activity_logger=None,
) -> tuple[web.Application, WebSocketManager, ActivityWebSocketManager]:
    """Create and configure the aiohttp application."""
    app = web.Application(middlewares=[error_middleware, auth_middleware])

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
        "activity_logger": activity_logger,
        "started_at": datetime.now(timezone.utc),
    }

    # REST routes
    setup_routes(app)

    # Prometheus metrics (no auth — Docker-network only)
    app.router.add_get("/metrics", metrics_handler)

    # WebSocket — main event stream
    ws_manager = WebSocketManager()
    app.router.add_get("/v1/events", ws_manager.handle)

    # WebSocket — activity log stream
    activity_ws = ActivityWebSocketManager()
    activity_ws.set_db(db)
    app.router.add_get("/v1/activity/live", activity_ws.handle)

    return app, ws_manager, activity_ws
