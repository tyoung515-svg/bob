"""
BoBClaw Gateway — Auth Routes

POST /auth/login   — password (+ optional TOTP) → {access_token, refresh_token}
POST /auth/refresh — refresh_token rotation
POST /auth/logout  — invalidate refresh token
GET  /auth/status  — current auth status
"""
import logging

from aiohttp import web

from auth import (
    create_access_token,
    create_agent_token,
    decode_access_token,
    verify_password_plain,
    verify_totp_with_replay_protection,
)
from config import config
from core.permissions import Scope
from db import (
    check_login_locked,
    clear_login_attempts,
    create_refresh_token,
    invalidate_refresh_token,
    record_failed_login,
    revoke_all_refresh_tokens,
    validate_and_rotate_refresh_token,
)

logger = logging.getLogger(__name__)

router = web.RouteTableDef()

# Upper bound on the faces allow-list baked into an agent token (anti-bloat;
# face-ID validation against the registry lands in the phase that consumes it).
_MAX_AGENT_FACES = 32


async def _record_login_failure(ip: str) -> None:
    await record_failed_login(
        ip,
        threshold=config.LOGIN_MAX_FAILURES,
        base_seconds=config.LOGIN_LOCKOUT_BASE_SECONDS,
        max_seconds=config.LOGIN_LOCKOUT_MAX_SECONDS,
    )


@router.post("/auth/login")
async def login(request: web.Request) -> web.Response:
    """Authenticate with password and optional TOTP; return access + refresh tokens.

    Per-IP failed-login lockout (B1): after LOGIN_MAX_FAILURES consecutive failures an
    IP is locked out with exponential backoff (429 + Retry-After); a success clears it.
    """
    ip = request.remote or "unknown"

    locked_for = await check_login_locked(ip)
    if locked_for is not None:
        return web.json_response(
            {"error": "Too many failed login attempts; try again later"},
            status=429,
            headers={"Retry-After": str(locked_for)},
        )

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    password = body.get("password", "")
    totp_code = body.get("totp_code") or ""

    if not verify_password_plain(password):
        await _record_login_failure(ip)
        return web.json_response({"error": "Invalid credentials"}, status=401)

    if not await verify_totp_with_replay_protection(totp_code):
        await _record_login_failure(ip)
        return web.json_response({"error": "Invalid TOTP code"}, status=401)

    await clear_login_attempts(ip)
    access_token = create_access_token()
    refresh_token = await create_refresh_token()

    return web.json_response(
        {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "Bearer",
        }
    )


@router.post("/auth/refresh")
async def refresh(request: web.Request) -> web.Response:
    """Issue a new access token and rotated refresh token."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    old_token = body.get("refresh_token", "")
    if not old_token:
        return web.json_response({"error": "refresh_token required"}, status=400)

    new_refresh = await validate_and_rotate_refresh_token(old_token)
    if new_refresh is None:
        return web.json_response(
            {"error": "Invalid or expired refresh token"}, status=401
        )

    return web.json_response(
        {
            "access_token": create_access_token(),
            "refresh_token": new_refresh,
            "token_type": "Bearer",
        }
    )


@router.post("/auth/logout")
async def logout(request: web.Request) -> web.Response:
    """Invalidate a refresh token."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    token = body.get("refresh_token", "")
    if token:
        await invalidate_refresh_token(token)

    return web.json_response({"status": "logged out"})


@router.post("/auth/revoke-all")
async def revoke_all(request: web.Request) -> web.Response:
    """Revoke ALL of the caller's refresh tokens (kill every session).

    Self-authenticating (the /auth/* prefix is middleware-exempt): requires a valid
    *admin* access token — an agent token may not revoke. Refresh tokens are opaque
    server-side rows, so this is immediate and complete; any outstanding access token
    still expires on its own within ACCESS_TOKEN_MINUTES.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return web.json_response(
            {"error": "Missing or invalid Authorization header"}, status=401
        )
    payload = decode_access_token(auth_header[7:])
    if payload is None:
        return web.json_response({"error": "Invalid or expired token"}, status=401)
    if payload.get("token_type") is not None:
        return web.json_response(
            {"error": "Only an admin token may revoke sessions"}, status=403
        )

    user_id = payload.get("sub", "admin")
    revoked = await revoke_all_refresh_tokens(user_id)
    request["user"] = payload  # so the audit middleware attributes the action
    logger.info("revoke-all: sub=%s revoked=%d refresh token(s)", user_id, revoked)
    return web.json_response({"status": "revoked", "revoked": revoked})


@router.post("/auth/agent-token")
async def mint_agent_token(request: web.Request) -> web.Response:
    """Mint a scoped, non-refreshable *agent* bearer token (Neck Beard MODE).

    The ``/auth/*`` prefix is exempt from ``auth_middleware`` (so the login page
    loads tokenless), therefore this route authenticates the caller itself: it
    requires a valid *admin* (non-agent) access token in the Authorization
    header. An agent token cannot mint further agent tokens — no privilege
    escalation. Admin proof is the existing access token (NOT password+TOTP per
    call: a fresh TOTP would be replay-rejected on the same 30s step).

    Body: ``{"scope": <Scope>, "faces": ["face-id", ...]}``. ``scope`` is
    validated against ``core.permissions.Scope`` (fail closed on malformed).
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return web.json_response(
            {"error": "Missing or invalid Authorization header"}, status=401
        )
    admin_payload = decode_access_token(auth_header[7:])
    if admin_payload is None:
        return web.json_response({"error": "Invalid or expired token"}, status=401)
    # Fail closed: only a human/admin token (which carries NO token_type claim)
    # may mint. An agent token — or any future JWT type minted with the same
    # secret — is rejected, so the highest-privilege operation is default-deny,
    # not deny-only-if-agent.
    if admin_payload.get("token_type") is not None:
        return web.json_response(
            {"error": "Only an admin token may mint agent tokens"}, status=403
        )

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    scope_in = body.get("scope") or {}
    faces = body.get("faces") or []
    if not isinstance(faces, list) or not all(isinstance(f, str) for f in faces):
        return web.json_response(
            {"error": "faces must be a list of strings"}, status=400
        )
    if len(faces) > _MAX_AGENT_FACES:
        return web.json_response(
            {"error": f"faces exceeds the {_MAX_AGENT_FACES}-entry cap"}, status=400
        )
    if not isinstance(scope_in, dict):
        return web.json_response({"error": "scope must be an object"}, status=400)
    # Reject unknown scope keys outright (a typo'd key would otherwise be silently
    # dropped by Scope's default extra='ignore', minting a quietly-weaker token).
    unknown = set(scope_in) - set(Scope.model_fields)
    if unknown:
        return web.json_response(
            {"error": f"unknown scope keys: {sorted(unknown)}"}, status=400
        )
    try:
        validated_scope = Scope.model_validate(scope_in).model_dump()
    except Exception as exc:  # pydantic ValidationError
        return web.json_response({"error": f"Invalid scope: {exc}"}, status=400)

    token = create_agent_token(
        user_id=admin_payload.get("sub", "admin"),
        scope=validated_scope,
        faces=faces,
    )
    # Attribute the mint: /auth/* is middleware-exempt so request["user"] isn't
    # set upstream — set it here so the outer audit-log middleware records the
    # minting admin, and emit an explicit forensic line (the token's jti ties a
    # live token back to it).
    request["user"] = admin_payload
    logger.info(
        "agent-token minted: sub=%s faces=%s scope=%s",
        admin_payload.get("sub"), faces, validated_scope,
    )
    return web.json_response({"access_token": token, "token_type": "Bearer"})


@router.get("/auth/status")
async def auth_status(request: web.Request) -> web.Response:
    """Return authentication status based on the Authorization header (no middleware)."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        payload = decode_access_token(auth_header[7:])
        if payload:
            return web.json_response(
                {"authenticated": True, "user": payload.get("sub")}
            )
    return web.json_response({"authenticated": False}, status=401)
