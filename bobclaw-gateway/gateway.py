"""
BoBClaw Gateway — Main Server

Usage:
    python gateway.py [--port PORT] [--host HOST] [--no-tls] [--skip-validation]
"""
import argparse
import logging
import os
import ssl
import sys
from typing import Any

import aiohttp_cors
from aiohttp import web

from app_state import CONVERSATION_STATE_KEY, POSTGRES_POOL_KEY, SESSION_STATE_KEY
from auth import auth_middleware
from audit_log import make_audit_log_middleware
from rate_limit import make_rate_limit_middleware
from security_headers import make_security_headers_middleware

from config import config
from db import close_postgres_pool, get_postgres_pool, init_db
from redis_client import close_redis
from routers.auth_routes import router as auth_router
from routers.capabilities import router as capabilities_router
from routers.chat import router as chat_router
from routers.approvals import router as approvals_router
from routers.conversations import router as conversations_router
from routers.faces import router as faces_router
from routers.ideas import router as ideas_router
from routers.memory import router as memory_router
from routers.memory_graph import router as memory_graph_router
from routers.models import router as models_router
from routers.projects import router as projects_router
from routers.routing_view import router as routing_view_router
from routers.system_routes import router as system_router
from routers.teams import router as teams_router

from core.backends.opencode_pool import _pool as _opencode_pool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifecycle hooks
# ---------------------------------------------------------------------------


async def _on_startup(app: web.Application) -> None:
    """Initialise database connections on application startup."""
    await init_db()
    if app.get(POSTGRES_POOL_KEY) is not None:
        logger.info("Postgres pool already provided by caller; skipping init")
        return
    try:
        app[POSTGRES_POOL_KEY] = await get_postgres_pool()
        logger.info("Postgres connection pool initialised")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Postgres pool init failed (non-fatal): %s", exc)
        app[POSTGRES_POOL_KEY] = None


async def _on_cleanup(app: web.Application) -> None:
    """Release resources on application shutdown."""
    await close_postgres_pool()
    await close_redis()
    try:
        await _opencode_pool.close()
    except Exception as exc:
        logger.warning("OpenCode pool close failed: %s", exc)
    logger.info("Gateway cleanup complete")


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def build_app(state_overrides: dict[web.AppKey, Any] | None = None) -> web.Application:
    """Create and configure the aiohttp Application."""
    app = web.Application(
        middlewares=[
            make_security_headers_middleware(),
            make_audit_log_middleware(enabled=config.AUDIT_LOG_ENABLED),
            auth_middleware,
            make_rate_limit_middleware(
                rate_per_minute=config.RATE_LIMIT_PER_MINUTE,
                burst=config.RATE_LIMIT_BURST,
            ),
        ]
    )
    app[SESSION_STATE_KEY] = {}
    app[CONVERSATION_STATE_KEY] = {}
    app[POSTGRES_POOL_KEY] = None

    if state_overrides:
        for key, value in state_overrides.items():
            app[key] = value

    # Register route tables
    app.router.add_routes(auth_router)
    app.router.add_routes(chat_router)
    app.router.add_routes(conversations_router)
    app.router.add_routes(projects_router)
    app.router.add_routes(faces_router)
    app.router.add_routes(ideas_router)
    app.router.add_routes(approvals_router)
    app.router.add_routes(capabilities_router)
    app.router.add_routes(models_router)
    app.router.add_routes(memory_router)
    app.router.add_routes(memory_graph_router)
    app.router.add_routes(routing_view_router)
    app.router.add_routes(teams_router)
    app.router.add_routes(system_router)

    # Web UI REMOVED (2026-07-02): the Preact stopgap wrapper was deprecated in favor of the
    # KMM desktop app. Only the JSON/WS API remains; `/` returns a tiny info response.
    async def _root(_request: web.Request) -> web.StreamResponse:
        return web.json_response(
            {"service": "bobclaw-gateway", "ui": "removed — use the desktop app",
             "api": ["/api/*", "/ws/chat", "/auth"]}
        )

    app.router.add_get("/", _root)

    # CORS — explicit allowlist (empty list in dev means no CORS headers)
    if config.ALLOWED_ORIGINS:
        cors_defaults: dict[str, aiohttp_cors.ResourceOptions] = {}
        for origin in config.ALLOWED_ORIGINS:
            cors_defaults[origin] = aiohttp_cors.ResourceOptions(
                allow_credentials=True,
                expose_headers=(),
                allow_headers=("Authorization", "Content-Type"),
                allow_methods=("GET", "POST", "PUT", "DELETE", "OPTIONS"),
            )
        cors = aiohttp_cors.setup(app, defaults=cors_defaults)
        for route in list(app.router.routes()):
            cors.add(route)

    # Lifecycle hooks
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)

    return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="BoBClaw API Gateway")
    parser.add_argument("--port", type=int, default=config.PORT, help="Listen port")
    parser.add_argument("--host", type=str, default=config.HOST, help="Bind address")
    parser.add_argument(
        "--no-tls", action="store_true", default=False, help="Disable TLS"
    )
    parser.add_argument(
        "--skip-validation",
        action="store_true",
        default=False,
        help="Skip config validation (requires BOBCLAW_ALLOW_UNSAFE=1)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.skip_validation:
        if os.getenv("BOBCLAW_ALLOW_UNSAFE") != "1":
            parser.error("--skip-validation requires BOBCLAW_ALLOW_UNSAFE=1")
        logger.warning(
            "UNSAFE: config validation skipped because BOBCLAW_ALLOW_UNSAFE=1"
        )
    else:
        try:
            config.validate()
        except ValueError as exc:
            logger.error("config validation failed: %s", exc)
            sys.exit(2)

    ssl_context: ssl.SSLContext | None = None
    use_tls = config.TLS_ENABLED and not args.no_tls
    if use_tls:
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.load_cert_chain(config.TLS_CERT, config.TLS_KEY)

    app = build_app()

    # Signal handling is delegated to aiohttp's web.run_app(), which installs its
    # own SIGINT/SIGTERM handlers and runs the graceful on_shutdown / cleanup_ctx
    # chain. (The previous hand-rolled handler only logged and stopped nothing.)

    logger.info(
        "Starting BoBClaw Gateway on %s:%d (TLS=%s)", args.host, args.port, use_tls
    )
    web.run_app(app, host=args.host, port=args.port, ssl_context=ssl_context)


if __name__ == "__main__":
    main()
