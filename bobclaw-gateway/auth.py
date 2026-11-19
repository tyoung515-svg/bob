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
from aiohttp import WSMessage, WSMsgType, web

from config import config
from core.settings_contract import SettingsError
from core.permissions import Scope, evaluate_path
from core.paths import sps_lane_root

logger = logging.getLogger(__name__)

# Routes a scoped agent token (token_type='agent') may reach through the REST
# middleware. Everything else — every admin router — is denied (default-deny).
# Matched on the aiohttp route *canonical* (the ``{param}`` template), not the
# concrete path, so it is robust to ids in the URL. The WS endpoints are exempt
# from the middleware: /ws/chat authorizes agent tokens inside authenticate_ws
# and /ws/approvals rejects them there.
#
# 2026-09-30 (MCP action tools): the four GET-only read routes below feed the
# MCP server's list_faces / list_backends / get_server_time / check_model_slugs
# tools. Read-only, no secrets in any payload; postures reach the client only
# via /faces/{face_id} (needed for the pinned-slug staleness check).
_AGENT_ALLOWED_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/conversations"),
        ("GET", "/conversations/{conv_id}"),
        ("GET", "/conversations/{conv_id}/messages"),
        ("GET", "/bob-guide"),
        ("GET", "/faces"),
        ("GET", "/faces/{face_id}"),
        ("GET", "/models/available"),
        ("GET", "/backends/catalog"),
        ("GET", "/system/ports"),
        ("GET", "/agent-services/catalog"),
        ("GET", "/agent-memory/status"),
        ("POST", "/agent-memory/search"),
        ("POST", "/chat/questions/{question_id}/answer"),
        ("POST", "/agent-memory/read"),
        ("POST", "/agent-memory/save"),
        ("GET", "/work-memory/status"),
        ("POST", "/work-memory/search"),
        ("POST", "/work-memory/read"),
        ("POST", "/work-memory/save"),
        ("GET", "/v1/models"),
        ("POST", "/v1/chat/completions"),
        ("POST", "/agent-services/runs"),
        ("GET", "/agent-services/runs/{run_id}"),
        ("POST", "/agent-services/runs/{run_id}/cancel"),
        ("POST", "/agent-services/runs/{run_id}/messages"),
    }
)

# Every urllib REST call in portal-broker.py, using the routers' canonical
# parameter names. Portal access stays default-deny even in household open mode.
_PORTAL_ALLOWED_ROUTES = frozenset({
    ("GET", "/health"),
    ("GET", "/auth/status"),
    ("GET", "/bob-guide"),
    ("GET", "/faces"), ("GET", "/models/available"),
    ("GET", "/backends/catalog"), ("GET", "/capabilities"),
    ("GET", "/conversations"), ("POST", "/conversations"),
    ("GET", "/conversations/{conv_id}"),
    ("GET", "/conversations/{conv_id}/messages"),
    ("POST", "/conversations/{conv_id}/rename"),
    ("POST", "/conversations/{conv_id}/project"),
    ("POST", "/chat/questions/{question_id}/answer"),
    ("GET", "/approvals"), ("GET", "/approvals/{approval_id}"),
    ("POST", "/approvals/{approval_id}/decide"),
    ("GET", "/projects"), ("POST", "/projects"),
    ("GET", "/projects/{project_id}"), ("POST", "/projects/{project_id}"),
    ("DELETE", "/projects/{project_id}"),
    ("GET", "/agent-memory/status"), ("POST", "/agent-memory/search"),
    ("POST", "/agent-memory/read"), ("POST", "/agent-memory/save"),
    ("POST", "/agent-services/runs"), ("GET", "/agent-services/runs/{run_id}"),
    ("POST", "/agent-services/runs/{run_id}/cancel"),
    ("POST", "/agent-services/runs/{run_id}/messages"),
})


# Scoped callers may read/cancel their authorized jobs. The router/core checks
# job, project and artifact ownership; middleware admits only these exact routes.
# Proposal decisions remain on the separate owner-session route below.
_TOOL_JOB_ROUTES = frozenset({
    ('GET','/api/tool-job-attachments'), ('GET','/api/tool-jobs'), ('POST','/api/tool-jobs'),
    ('GET','/api/tool-jobs/{job_id}/audit'), ('POST','/api/tool-jobs/{job_id}/cancel'),
    ('GET','/api/tool-jobs/{job_id}/proposal'), ('GET','/api/tool-jobs/{job_id}/artifacts/{filename}'),
})
_AGENT_ALLOWED_ROUTES |= _TOOL_JOB_ROUTES
_PORTAL_ALLOWED_ROUTES |= _TOOL_JOB_ROUTES
_INSPECTION_READ_ROUTES = frozenset({('POST','/bob-inspection/{operation}')})
_AGENT_ALLOWED_ROUTES |= _INSPECTION_READ_ROUTES
_PORTAL_ALLOWED_ROUTES |= _INSPECTION_READ_ROUTES

# Workflow reads/proposals retain caller scope; definition mutations are owner
# only in core. Start/resume/gates use the exact owner-session map below.
_WORKFLOW_READ_ROUTES = frozenset({
    ('GET','/workflow-proposals'),
    ('GET','/workflows/catalog'), ('GET','/workflows'), ('GET','/workflows/{id}'),
    ('POST','/workflow-proposals'), ('GET','/workflow-proposals/{proposal_id}'),
    ('GET','/workflow-runs'), ('GET','/workflow-runs/{run_id}'),
    ('GET','/workflow-runs/{run_id}/events'), ('GET','/workflow-runs/{run_id}/artifacts/{artifact_id}'),
    ('POST','/workflow-runs/{run_id}/messages'), ('POST','/workflow-runs/{run_id}/stop'),
})
_AGENT_ALLOWED_ROUTES |= _WORKFLOW_READ_ROUTES
_PORTAL_ALLOWED_ROUTES |= _WORKFLOW_READ_ROUTES | frozenset({
    ('POST','/workflows'), ('POST','/workflows/{id}/copy'), ('POST','/workflows/{id}/delete'),
    ('POST','/workflow-runs/{run_id}/pause'),
    ('POST','/workflow-proposals/{proposal_id}/propose-again'),
})

# Owner reads remain portal-only even in open mode.
PORTAL_OWNER_READ_ROUTES = frozenset({
    ("POST", "/agent-memory/list"), ("GET", "/files/roots"),
    ("POST", "/files/list"), ("POST", "/files/read"),
})
_PORTAL_ALLOWED_ROUTES = _PORTAL_ALLOWED_ROUTES | PORTAL_OWNER_READ_ROUTES
_PORTAL_ALLOWED_ROUTES |= frozenset({('POST','/api/builder/{operation}')})
_PORTAL_ALLOWED_ROUTES |= frozenset({('POST','/api/workspace/browse')})
# The native client transports requests and signed receipts; this route cannot
# approve a PC grant. Admit its portal token through middleware as well as the
# router, while refusing human and delegated credentials even in open mode.
_PC_TRANSPORT_ROUTES = frozenset({('POST', '/api/pc/{operation}')})
_PORTAL_ALLOWED_ROUTES |= _PC_TRANSPORT_ROUTES

# v2 uses the same verified subject/scope and exact route allowlists. No budget
# authorization, provider mutation or worker routes are granted here.
_EXECUTION_V2_ROUTES = frozenset({
    ("POST", "/api/work/{operation}"),
    ("GET", "/agent-services/flags"),
    ("GET", "/agent-services/flags/{flag_id}"),
    ("GET", "/teams"), ("POST", "/teams"),
    ("GET", "/agent-services/runs"),
    ("GET", "/agent-services/catalog"),
})
_EXECUTION_OWNER_ROUTES = frozenset({
    ("POST", "/api/builder/mode.set"),
    ("POST", "/api/builder/approve"),
    ("POST", "/api/work/workspace.create"),
    ("POST", "/api/work/publish"),
    ("POST", "/api/tool-jobs/{job_id}/proposal/decision"),
    ("POST", "/agent-services/flags/{flag_id}/resolve"),
    ("POST", "/agent-services/runs/{run_id}/resume"),
    ("POST", "/agent-services/budgets"),
    ("GET", "/agent-services/budgets"),
    ("GET", "/agent-services/budgets/projects/{project_id}"),
    ("POST", "/agent-services/budgets/projects/{project_id}/remove"),
    ("POST", "/agent-services/grants"),
    ("GET", "/agent-services/grants"),
    ("GET", "/agent-services/grants/{grant_id}"),
    ("POST", "/agent-services/grants/{grant_id}/revoke"),
})
OWNER_ROUTE_OPERATIONS = {
    ('POST','/workflow-proposals/{proposal_id}/start'): 'workflows.grant_start',
    ('POST','/workflow-runs/{run_id}/resume'): 'workflows.resume',
    ('POST','/workflow-runs/{run_id}/gates/{wait_id}'): 'workflows.gate',
    ("POST", "/api/builder/mode.set"): "builder.mode.set",
    ("POST", "/api/builder/approve"): "builder.approve",
    ("POST", "/api/builder/network.set"): "builder.network.set",
    ("POST", "/api/work/workspace.create"): "work.workspace.create",
    ("POST", "/api/work/publish"): "work.publish",
    ("POST", "/api/tool-jobs/{job_id}/proposal/decision"): "jobs.decide",
    ("POST", "/settings/secrets"): "secrets.set",
    ("GET", "/settings/policy"): "policy.get",
    ("POST", "/settings/policy"): "policy.set",
    ("POST", "/agent-services/flags/{flag_id}/resolve"): "flags.resolve",
    ("POST", "/agent-services/runs/{run_id}/resume"): "runs.resume",
    ("POST", "/agent-services/budgets"): "budgets.set",
    ("GET", "/agent-services/budgets"): "budgets.list",
    ("GET", "/agent-services/budgets/projects/{project_id}"): "budgets.get",
    ("POST", "/agent-services/budgets/projects/{project_id}/remove"): "budgets.remove",
    ("POST", "/agent-services/grants"): "grants.issue",
    ("GET", "/agent-services/grants"): "grants.list",
    ("GET", "/agent-services/grants/{grant_id}"): "grants.get",
    ("POST", "/agent-services/grants/{grant_id}/revoke"): "grants.revoke",
    ("GET", "/auth/owner/status"): "owner.status",
    ("POST", "/auth/owner/lock"): "owner.lock",
    ("POST", "/auth/owner/logout"): "owner.logout",
}
_PROPOSAL_ROUTE = frozenset({("POST", "/agent-services/grant-proposals")})
_AGENT_ALLOWED_ROUTES |= _PROPOSAL_ROUTE
_PORTAL_ALLOWED_ROUTES |= _PROPOSAL_ROUTE

# Work-lane tokens (scope may_touch rooted at a lane repo, e.g. the SPS corpus)
# are confined to the lane memory routes REGARDLESS of AGENT_INNER_GATES. "open"
# household mode deliberately skips the allow-list above; that must never widen a
# work token into the personal surfaces. The lane roots are the same ones
# routers/work_memory.py authorizes on, so the two layers cannot drift apart.
_LANE_SCOPE_ROOTS: dict[str, str] = {
    **({"sps": str(root)} if (root := sps_lane_root()) is not None else {}),
}
_LANE_ALLOWED_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/work-memory/status"),
        ("POST", "/work-memory/search"),
        ("POST", "/work-memory/read"),
        ("POST", "/work-memory/save"),
    }
)


def lane_from_scope(scope: object) -> Optional[str]:
    """Lane name when this scope's may_touch is rooted at a lane repo, else None."""
    if not isinstance(scope, dict):
        return None
    try:
        validated = Scope.model_validate(scope)
    except Exception:
        return None
    for name, root in _LANE_SCOPE_ROOTS.items():
        if evaluate_path(root + "/work", validated) == "auto":
            return name
    return None


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
    """
    Compare a candidate password against config.BOBCLAW_PASSWORD using a
    constant-time comparison to mitigate timing attacks.
    """
    return isinstance(candidate, str) and hmac.compare_digest(candidate.encode("utf-8"), config.BOBCLAW_PASSWORD.encode("utf-8"))


def _matched_totp_step(code: str, valid_window: int) -> Optional[int]:
    """Return the TOTP timestep whose code equals *code*, else None.

    Searches the accepted window (RFC 6238 §5.2 clock-drift tolerance) and
    returns the step that actually matched, so replay protection can key on the
    code's own step instead of "now" — otherwise a drifted-but-valid code could
    be replayed during the following window.
    """
    if not config.TOTP_SECRET:
        return None
    totp = pyotp.TOTP(config.TOTP_SECRET)
    now_step = int(time.time() / 30)
    submitted = code.encode("utf-8", "replace")
    for offset in range(-valid_window, valid_window + 1):
        step = now_step + offset
        if hmac.compare_digest(submitted, totp.at(step * 30).encode("utf-8")):
            return step
    return None


def verify_totp(code: str) -> bool:
    """
    Verify a TOTP code against TOTP_SECRET within the configured step tolerance.
    Returns True if TOTP is not configured (secret is empty).
    """
    if not config.TOTP_SECRET:
        return True  # TOTP not configured — skip check
    return _matched_totp_step(code, config.TOTP_VALID_WINDOW) is not None


async def classify_totp(code: str, user_id: str = "admin") -> str:
    """Classify a submitted code: ``"ok"`` | ``"invalid"`` | ``"replay"``.

    Distinguishing these lets the login route report a wrong/drifted code
    separately from a code that was already used, instead of collapsing every
    failure into one message.
    """
    if not config.TOTP_SECRET:
        return "ok"  # TOTP not configured — skip check

    step = _matched_totp_step(code, config.TOTP_VALID_WINDOW)
    if step is None:
        return "invalid"

    from db import _sqlite_connection
    # Login and step-up use one transactional high-water mark. Never check then
    # store on separate connections: concurrent callers could spend one proof.
    async with _sqlite_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute("SELECT last_timestep FROM totp_timesteps WHERE user_id=?", (user_id,)) as cursor:
            row = await cursor.fetchone()
        if row and row[0] >= step:
            return "replay"
        await db.execute("INSERT INTO totp_timesteps VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET last_timestep=excluded.last_timestep", (user_id, step))
        await db.commit()
    return "ok"


async def verify_totp_with_replay_protection(code: str, user_id: str = "admin") -> bool:
    """Verify a TOTP code and reject replays (RFC 6238 §5.2).

    Kept as the boolean form for existing callers; see :func:`classify_totp` for
    the reason-carrying version.
    """
    return await classify_totp(code, user_id) == "ok"


def create_access_token(user_id: Optional[str] = None) -> str:
    """Create a signed JWT; fresh installed-lane login uses its explicit owner."""
    from core import credential_state
    if user_id is None:
        import os
        user_id = os.environ['BOB_DEFAULT_OWNER'] if credential_state.enabled() else 'admin'
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "iat": now,
        "exp": now + timedelta(minutes=config.ACCESS_TOKEN_MINUTES),
    }
    from core import credential_state
    credential_state.require_loaded()
    if credential_state.enabled():
        payload['credential_epoch'] = credential_state.loaded_epoch()
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
    from core import credential_state
    credential_state.require_loaded()
    if credential_state.enabled():
        snapshot = credential_state.active()
        if snapshot['epoch'] != credential_state.loaded_epoch():
            raise SettingsError('credential_generation_mismatch')
        payload['service_epoch'] = snapshot.get('revocation_epoch', 0)
    return jwt.encode(payload, config.JWT_SECRET, algorithm="HS256")


def create_portal_token(user_id: str, scope: dict) -> str:
    """The lane owner's app bearer, distinct from an autonomous agent."""
    Scope.model_validate(scope)
    now = datetime.now(timezone.utc)
    from core import credential_state
    credential_state.require_loaded()
    epoch = {}
    if credential_state.enabled():
        snapshot = credential_state.active()
        if snapshot['epoch'] != credential_state.loaded_epoch():
            raise SettingsError('credential_generation_mismatch')
        epoch['service_epoch'] = snapshot.get('revocation_epoch', 0)
    return jwt.encode({
        **epoch, "sub": user_id, "iat": now,
        "exp": now + timedelta(days=config.PORTAL_TOKEN_DAYS),
        "token_type": "portal", "scope": scope, "jti": uuid.uuid4().hex,
    }, config.JWT_SECRET, algorithm="HS256")


def decode_access_token(token: str) -> Optional[dict]:
    """
    Decode and validate a JWT access token.
    Returns the payload dict on success, or None if invalid/expired.
    """
    try:
        payload = jwt.decode(token, config.JWT_SECRET, algorithms=["HS256"])
        from core import credential_state
        credential_state.require_loaded()
        if (credential_state.enabled() and payload.get('token_type', 'human') == 'human'
                and payload.get('credential_epoch') != credential_state.loaded_epoch()):
            return None
        if (credential_state.enabled() and payload.get('token_type') in {'portal', 'agent'}
                and payload.get('service_epoch', 0) != credential_state.active().get('revocation_epoch', 0)):
            return None
        return payload
    except SettingsError:
        return None
    except jwt.ExpiredSignatureError:
        logger.debug("Token expired")
        return None
    except jwt.InvalidTokenError as exc:
        logger.debug("Invalid token: %s", exc)
        return None


class CredentialWebSocketResponse(web.WebSocketResponse):
    """Recheck every authenticated frame and close idle stale sockets too."""
    def _generation_current(self):
        if not getattr(self, '_credential_guard', False):
            return True
        from core import credential_state
        try:
            credential_state.require_loaded()
            return self._credential_epoch == credential_state.active()['epoch']
        except SettingsError:
            return False

    async def receive(self, timeout=None):
        message = await super().receive(timeout=timeout)
        if not self._generation_current():
            await self.close(code=4401, message=b'credential_epoch_changed')
            return WSMessage(WSMsgType.CLOSE, 4401, 'credential_epoch_changed')
        return message

    async def send_str(self, data, compress=None):
        if not self._generation_current():
            await self.close(code=4401, message=b'credential_epoch_changed')
            raise ConnectionResetError('credential_epoch_changed')
        return await super().send_str(data, compress=compress)


def watch_credential_generation(ws):
    from core import credential_state
    if not credential_state.enabled():
        return
    ws._credential_guard = True
    ws._credential_epoch = credential_state.loaded_epoch()
    async def watch():
        while not ws.closed:
            try:
                credential_state.require_loaded()
                if ws._credential_epoch != credential_state.active()['epoch']:
                    raise SettingsError('credential_generation_mismatch')
            except SettingsError:
                await ws.close(code=4401, message=b'credential_epoch_changed')
                return
            await asyncio.sleep(.2)
    ws._credential_watch = asyncio.create_task(watch())


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
        if payload.get("token_type") == "portal":
            return (request.path not in {"/ws/chat", "/ws/approvals"}
                    or lane_from_scope(payload.get("scope")) is not None)
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
        watch_credential_generation(ws)
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
    watch_credential_generation(ws)
    return payload, initial


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """
    Validate JWT Bearer tokens on all routes except /auth/*, /health,
    /ws/chat, /ws/approvals, /ws/monitor, and the root info route (/).
    Browsers can't set an Authorization header on a WebSocket upgrade, so the
    WS endpoints do their own Pattern-2 first-frame auth and must be exempt
    here or the upgrade is 401'd before that runs.
    Sets request["user"] to the decoded payload on success.
    """
    path = request.path
    from credential_auth import require_loaded
    from core.execution.models import ExecutionError
    try:
        require_loaded()
    except ExecutionError as exc:
        return web.json_response({'error': {'code': exc.code}}, status=exc.status)
    resource = getattr(request.match_info.route, "resource", None)
    canonical = getattr(resource, "canonical", None)
    owner_op = OWNER_ROUTE_OPERATIONS.get((request.method, canonical))
    header = request.headers.get("Authorization", "")
    # Enforce audience BEFORE public-auth exemptions. Opaque owner bearers can
    # never become generic humans, even on /auth/*, health or unknown routes.
    if ((path == '/settings/password' and request.method == 'POST')
            or (path in {'/settings/factors/prepare', '/settings/factors/commit', '/settings/factors/cancel'}
                and request.method == 'POST')
            or (path == '/settings/factors/binding' and request.method == 'GET')):
        # The combined form checks context and fresh factors inside its handler.
        return await handler(request)
    if header.startswith("Bearer bos1.") or owner_op:
        from owner_sessions import authenticate
        from core.execution.models import ExecutionError
        if not owner_op:
            return web.json_response({"error": {"code": "owner_audience_denied"}}, status=403)
        try:
            request["user"] = await authenticate(request, owner_op)
        except ExecutionError as exc:
            return web.json_response({"error": {"code": exc.code}}, status=exc.status)
        return await handler(request)
    if (
        # This one internal hop authenticates its signed, short-lived envelope.
        (path == "/chat/pipes" and request.method == "POST")
        or path == "/health"
        or path == "/auth"
        or path.startswith("/auth/")
        or path == "/ws/chat"
        or path == "/ws/approvals"
        or path == "/ws/monitor"
        or path == "/"
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
    resource = getattr(request.match_info.route, "resource", None)
    canonical = getattr(resource, "canonical", None)
    if ((request.method, canonical) in _EXECUTION_OWNER_ROUTES
            and payload.get("token_type", "human") != "human"):
        return web.json_response({"error": {"code": "not_found"}}, status=404)
    execution_v2 = False
    if (request.method, canonical) in _EXECUTION_V2_ROUTES:
        execution_v2 = request.query.get("contract_version") == "2"
        if request.method == "POST":
            try:
                body = await request.json()  # aiohttp caches it for the handler
                execution_v2 = (isinstance(body, dict) and type(body.get("contract_version")) is int
                                and body["contract_version"] == 2)
            except (ValueError, TypeError):
                execution_v2 = False
    if (request.method, canonical) in (PORTAL_OWNER_READ_ROUTES | _PC_TRANSPORT_ROUTES) and payload.get("token_type") != "portal":
        return web.json_response({"error": {"code": "scope_denied", "detail": "scope_denied"}}, status=403)

    if payload.get("token_type") == "portal":
        resource = getattr(request.match_info.route, "resource", None)
        canonical = getattr(resource, "canonical", None)
        if (lane_from_scope(payload.get("scope")) is not None
                or ((request.method, canonical) not in _PORTAL_ALLOWED_ROUTES and not execution_v2)):
            return web.json_response(
                {"error": "Portal token not permitted for this route"}, status=403)

    # Work-lane confinement: a lane token reaches ONLY the lane memory routes.
    # This runs BEFORE the AGENT_INNER_GATES bypass on purpose — "open" household
    # mode must not widen a work token into conversations/chat/personal surfaces.
    if payload.get("token_type") == "agent" and lane_from_scope(payload.get("scope")):
        resource = getattr(request.match_info.route, "resource", None)
        canonical = getattr(resource, "canonical", None)
        if (request.method, canonical) not in _LANE_ALLOWED_ROUTES:
            return web.json_response(
                {"error": "Lane token not permitted for this route"}, status=403
            )

    # Default-deny for scoped agent tokens (Neck Beard MODE): an agent bearer may
    # reach ONLY the minimal conversation endpoints the headless MCP server
    # needs; every other REST route — all admin routers — is denied here. Matched
    # on the resolved route canonical so an out-of-allowlist or unknown route
    # both fail closed. (/ws/chat and /ws/approvals are exempt above and decide
    # agent access inside authenticate_ws.)
    # PATCH-LATER: "open" inner-gates mode skips this
    # default-deny so Bob's own agents work as pre-family-layer; strict returns
    # after the scope-authority migration.
    if payload.get("token_type") == "agent" and config.AGENT_INNER_GATES != "open":
        resource = getattr(request.match_info.route, "resource", None)
        canonical = getattr(resource, "canonical", None)
        if (request.method, canonical) not in _AGENT_ALLOWED_ROUTES and not execution_v2:
            return web.json_response(
                {"error": "Agent token not permitted for this route"}, status=403
            )

    return await handler(request)
