"""
BoBClaw Gateway — Authentication
JWT creation/validation, TOTP, password hashing, and aiohttp middleware.
"""
import asyncio
import hmac
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
import pyotp
from aiohttp import WSMsgType, web

from config import config

logger = logging.getLogger(__name__)

# Routes a scoped agent token (token_type='agent') may reach through the REST
# middleware. Everything else — every admin router — is denied (default-deny).
# Matched on the aiohttp route *canonical* (the ``{param}`` template), not the
# concrete path, so it is robust to ids in the URL. The WS endpoints are exempt
# from the middleware: /ws/chat authorizes agent tokens inside authenticate_ws
# and /ws/approvals rejects them there.
_AGENT_ALLOWED_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/conversations"),
        ("GET", "/conversations/{conv_id}"),
        ("GET", "/conversations/{conv_id}/messages"),
    }
)


def hash_password(plain: str) -> str:
    """Hash a plaintext password using bcrypt. Returns an encoded string."""
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a plaintext password against a bcrypt hash."""
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


def verify_password_plain(candidate: str) -> bool:
    """Verify a candidate admin password.

    Prefers the bcrypt hash (``config.BOBCLAW_PASSWORD_HASH``) so the plaintext is
    never stored at rest. Falls back to a constant-time comparison against the
    plaintext ``config.BOBCLAW_PASSWORD`` for backward compatibility (bcrypt already
    runs in constant time w.r.t. the candidate).
    """
    if config.BOBCLAW_PASSWORD_HASH:
        return verify_password(candidate, config.BOBCLAW_PASSWORD_HASH)
    return hmac.compare_digest(candidate, config.BOBCLAW_PASSWORD)


def verify_totp(code: str) -> bool:
    """
    Verify a TOTP code against TOTP_SECRET.
    Returns True if TOTP is not configured (secret is empty).
    """
    if not config.TOTP_SECRET:
        return True  # TOTP not configured — skip check
    totp = pyotp.TOTP(config.TOTP_SECRET)
    return totp.verify(code)


async def verify_totp_with_replay_protection(code: str, user_id: str = "admin") -> bool:
    """Verify a TOTP code and reject replays (RFC 6238 §5.2).

    On first acceptance, persists the current timestep so the same code
    cannot be reused within its validity window.  Returns True only when
    the code is cryptographically valid AND its timestep is newer than
    the last accepted one for *user_id*.
    """
    if not verify_totp(code):
        return False

    from db import check_totp_replay, store_totp_timestep

    timestep = int(time.time() / 30)
    is_replay = await check_totp_replay(user_id, timestep)
    if is_replay:
        logger.info("TOTP replay rejected for user=%r timestep=%d", user_id, timestep)
        return False

    await store_totp_timestep(user_id, timestep)
    return True


def create_access_token(user_id: str = "admin") -> str:
    """Create a signed JWT access token for `user_id`."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "iat": now,
        "exp": now + timedelta(minutes=config.ACCESS_TOKEN_MINUTES),
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm="HS256")


def create_agent_token(
    user_id: str = "admin",
    *,
    scope: Optional[dict] = None,
    faces: Optional[list] = None,
    ttl_days: Optional[int] = None,
) -> str:
    """Create a signed, scoped *agent* bearer token (Neck Beard MODE).

    Distinguished from a human access token by ``token_type='agent'``. Carries a
    Gate ``scope`` (the ``core.permissions.Scope`` shape, already validated by the
    caller) and a ``faces`` allow-list. The same HS256 secret and the unchanged
    :func:`decode_access_token` validate it — the extra claims simply ride
    through the existing verify path. A human token has no ``token_type`` claim,
    so ``create_access_token`` output is byte-identical to before.
    """
    now = datetime.now(timezone.utc)
    days = config.AGENT_TOKEN_DAYS if ttl_days is None else ttl_days
    payload = {
        "sub": user_id,
        "iat": now,
        "exp": now + timedelta(days=days),
        "token_type": "agent",
        "scope": scope or {},
        "faces": faces or [],
        # Stable token id so Phase 5 can denylist/revoke a leaked token without a
        # format migration, and so a live token correlates to its mint audit line.
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm="HS256")


def decode_access_token(token: str) -> Optional[dict]:
    """
    Decode and validate a JWT access token.
    Returns the payload dict on success, or None if invalid/expired.
    """
    try:
        return jwt.decode(token, config.JWT_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        logger.debug("Token expired")
        return None
    except jwt.InvalidTokenError as exc:
        logger.debug("Invalid token: %s", exc)
        return None


async def authenticate_ws(
    request: web.Request,
    ws: web.WebSocketResponse,
    *,
    allow_agent: bool = False,
):
    """Authenticate a WebSocket upgrade — shared by /ws/chat and /ws/approvals.

    Pattern 1 — ``Authorization: Bearer <jwt>`` on the HTTP upgrade (native
    clients, e.g. the Kotlin app and the MCP server).
    Pattern 2 — first text frame ``{"type": "auth", "token": "<jwt>"}`` within
    5 seconds (browser clients that can't set the upgrade header).

    Returns ``(payload, initial_message)``: *initial_message* is the first
    non-auth frame (Pattern 2) or ``None`` (Pattern 1). On any failure the
    socket is already closed and ``(None, None)`` is returned.

    *allow_agent* defaults to **False** (fail-closed, matching the REST
    middleware's default-deny posture): a token with ``token_type == 'agent'``
    is rejected unless the caller opts in. /ws/chat passes ``allow_agent=True``;
    /ws/approvals keeps the default so a scoped agent token can never subscribe
    to the human approval stream.
    """

    async def _reject_frame(msg: str, code: str):
        try:
            await ws.send_json({"type": "error", "message": msg, "code": code})
        except Exception:  # noqa: BLE001 — best-effort; socket may be gone
            pass
        await ws.close()
        return None, None

    def _is_blocked_agent(payload: dict) -> bool:
        return not allow_agent and payload.get("token_type") == "agent"

    # Pattern 1: Authorization header on the HTTP upgrade
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        payload = decode_access_token(auth_header[7:])
        if payload is None:
            return await _reject_frame("Invalid or expired token", "invalid_token")
        if _is_blocked_agent(payload):
            return await _reject_frame(
                "Agent tokens cannot access this endpoint", "forbidden"
            )
        return payload, None

    # Pattern 2: first WebSocket frame auth
    try:
        incoming = await asyncio.wait_for(ws.receive(), timeout=5.0)
    except asyncio.TimeoutError:
        await ws.close(code=4401, message=b"auth timeout")
        return None, None

    if incoming.type != WSMsgType.TEXT:
        await ws.close(code=4401, message=b"text frame required")
        return None, None

    try:
        data = json.loads(incoming.data)
    except json.JSONDecodeError:
        await ws.close(code=4401, message=b"invalid json")
        return None, None
    # Valid JSON that isn't an object (``[]``, ``5``, ``"x"``, ``null``) would
    # crash the ``.get`` below — reject it as malformed, not 500.
    if not isinstance(data, dict):
        await ws.close(code=4401, message=b"invalid json")
        return None, None

    payload = decode_access_token(data.get("token") or "")
    if payload is None:
        await ws.close(code=4401, message=b"unauthorized")
        return None, None
    if _is_blocked_agent(payload):
        await ws.close(code=4401, message=b"unauthorized")
        return None, None

    initial = data if data.get("type") != "auth" else None
    return payload, initial


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """
    Validate JWT Bearer tokens on all routes except /auth/*, /health,
    /ws/chat, /ws/approvals, the root redirect (/), and the static web UI
    (/ui, /ui/*). Browsers can't set an Authorization header on a WebSocket
    upgrade, so both WS endpoints do their own Pattern-2 first-frame auth and
    must be exempt here or the upgrade is 401'd before that runs. The UI
    assets and login page must load before any token exists; every request the
    page then makes still carries a Bearer token.
    Sets request["user"] to the decoded payload on success.
    """
    path = request.path
    if (
        path == "/health"
        or path == "/auth"
        or path.startswith("/auth/")
        or path == "/ws/chat"
        or path == "/ws/approvals"
        or path == "/"
        or path == "/ui"
        or path.startswith("/ui/")
    ):
        return await handler(request)

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return web.json_response(
            {"error": "Missing or invalid Authorization header"}, status=401
        )

    token = auth_header[7:]
    payload = decode_access_token(token)
    if payload is None:
        return web.json_response({"error": "Invalid or expired token"}, status=401)

    request["user"] = payload

    # Default-deny for scoped agent tokens (Neck Beard MODE): an agent bearer may
    # reach ONLY the minimal conversation endpoints the headless MCP server
    # needs; every other REST route — all admin routers — is denied here. Matched
    # on the resolved route canonical so an out-of-allowlist or unknown route
    # both fail closed. (/ws/chat and /ws/approvals are exempt above and decide
    # agent access inside authenticate_ws.)
    if payload.get("token_type") == "agent":
        resource = getattr(request.match_info.route, "resource", None)
        canonical = getattr(resource, "canonical", None)
        if (request.method, canonical) not in _AGENT_ALLOWED_ROUTES:
            return web.json_response(
                {"error": "Agent token not permitted for this route"}, status=403
            )

    return await handler(request)
