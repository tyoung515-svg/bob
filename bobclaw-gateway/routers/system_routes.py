"""
BoBClaw Gateway — System Routes

GET /health         — liveness check
GET /system/ports   — service port map
GET /system/config  — safe (non-secret) configuration
"""
import logging

from aiohttp import web

from config import config

logger = logging.getLogger(__name__)

router = web.RouteTableDef()


@router.get("/health")
async def health(request: web.Request) -> web.Response:
    """Basic health check — accessible without authentication.

    Deliberately minimal: no internal service URLs or config are exposed on this
    unauthenticated endpoint (recon surface if the gateway is ever placed behind a
    reverse proxy). The service map lives behind auth at /system/config.
    """
    return web.json_response({"status": "ok", "service": "bobclaw-gateway"})


@router.get("/system/ports")
async def system_ports(request: web.Request) -> web.Response:
    """Return service-to-port mapping (requires authentication via middleware)."""
    return web.json_response(
        {
            "gateway": config.PORT,
            "core": 7825,
            "claude_pipeline": 7823,
            "canopy": 7822,
        }
    )


@router.get("/system/config")
async def system_config(request: web.Request) -> web.Response:
    """Return safe configuration — no secrets exposed (requires authentication)."""
    return web.json_response(
        {
            "port": config.PORT,
            "host": config.HOST,
            "tls_enabled": config.TLS_ENABLED,
            "access_token_minutes": config.ACCESS_TOKEN_MINUTES,
            "refresh_token_days": config.REFRESH_TOKEN_DAYS,
            "core_url": config.CORE_URL,
            "claude_pipeline_url": config.CLAUDE_PIPELINE_URL,
            "canopy_url": config.CANOPY_URL,
            "log_level": config.LOG_LEVEL,
            "totp_enabled": bool(config.TOTP_SECRET),
        }
    )
