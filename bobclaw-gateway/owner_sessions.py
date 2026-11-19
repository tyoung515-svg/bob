"""Durable, absolute-lifetime owner sessions; no refresh or credential journal.

BEGIN IMMEDIATE serializes attempts, challenge consumption, TOTP high-water mark
and session creation. Only hashes of opaque session bearers are persisted.
"""
import hashlib
import json
import os
import secrets
import time

from aiohttp import web
import pyotp

from config import config
from db import _sqlite_connection
from core.execution.api import lane_id
from core.execution.models import ExecutionError, only, text, decode_integers
from core.execution.owner import (AUDIENCE, OWNER_OPERATIONS, SESSION_OPERATIONS,
                                  WORK_APPROVAL_OPERATIONS, owner_mapping, unavailable_reason, session_operations)

router = web.RouteTableDef()
clock = time.time
SCHEMA = """
CREATE TABLE IF NOT EXISTS owner_epochs(lane TEXT,owner TEXT,epoch INTEGER NOT NULL DEFAULT 1,
 PRIMARY KEY(lane,owner));
CREATE TABLE IF NOT EXISTS owner_challenges(id TEXT PRIMARY KEY,lane TEXT,owner TEXT,epoch INTEGER,
 portal_id TEXT,link_nonce TEXT,operation TEXT,summary TEXT,expires_at REAL,used INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS owner_sessions(token_hash TEXT PRIMARY KEY,lane TEXT,owner TEXT,epoch INTEGER,
 portal_id TEXT,link_nonce TEXT,expires_at REAL,revoked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS owner_attempts(key TEXT PRIMARY KEY,failures INTEGER,next_at REAL);
"""


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


async def initialize():
    async with _sqlite_connection() as db:
        await db.executescript(SCHEMA)
        await db.execute('BEGIN IMMEDIATE')
        async with db.execute('PRAGMA table_info(owner_sessions)') as cursor:
            columns = [row[1] for row in await cursor.fetchall()]
        if 'allowed_operations' not in columns:
            await db.execute('ALTER TABLE owner_sessions ADD COLUMN allowed_operations TEXT')
        if not unavailable_reason():
            import os
            await db.execute("INSERT OR IGNORE INTO owner_epochs VALUES (?,?,1)",
                             (lane_id(), os.environ["BOB_DEFAULT_OWNER"]))
        await db.commit()


def _link(request):
    return text(request.headers.get("X-Bob-Broker-Link"), 128)


def portal(request):
    # Authenticate explicitly, including when a router is mounted without middleware.
    from auth import decode_access_token
    header = request.headers.get("Authorization", "")
    claims = decode_access_token(header[7:]) if header.startswith("Bearer ") else None
    if (not claims or claims.get("token_type") != "portal" or not claims.get("jti")
            or not owner_mapping(claims.get("sub"))):
        raise ExecutionError("not_found", 404)
    return claims, _link(request)


def metadata(row):
    return {"active": True, "owner": row["owner"], "actor": row["owner"], "lane": row["lane"],
            "expires_at": row["expires_at"], "allowed_operations": sorted(session_operations(row)),
            "credential_epoch": row["epoch"]}


async def authenticate(request, operation):
    from credential_auth import require_loaded
    require_loaded()
    header = request.headers.get("Authorization", "")
    token = header[7:] if header.startswith("Bearer bos1.") else ""
    async with _sqlite_connection() as db:
        db.row_factory = __import__("sqlite3").Row
        async with db.execute("SELECT s.* FROM owner_sessions s JOIN owner_epochs e USING(lane,owner) "
                              "WHERE s.token_hash=? AND s.epoch=e.epoch AND s.revoked=0 AND s.expires_at>?",
                              (token_hash(token), clock())) as cursor:
            row = await cursor.fetchone()
    if (operation not in SESSION_OPERATIONS or not row or operation not in session_operations(row) or not owner_mapping(row["owner"])
            or row["lane"] != lane_id() or row["link_nonce"] != _link(request)
            or row["portal_id"] != request.headers.get("X-Bob-Portal-Credential")):
        raise ExecutionError("owner_session_invalid", 403)
    from core import credential_state
    if credential_state.enabled() and row['epoch'] != credential_state.loaded_epoch():
        raise ExecutionError('owner_session_invalid', 403)
    return {"sub": row["owner"], "token_type": "human", "session_kind": "owner_stepup",
            "audience": AUDIENCE, "jti": row["token_hash"], "credential_epoch": row["epoch"],
            "portal_credential_id": row["portal_id"], "broker_link_nonce": row["link_nonce"],
            "allowed_operations": sorted(session_operations(row)), "scope": {}}


def errors(handler):
    async def wrapped(request):
        try:
            return await handler(request)
        except ExecutionError as exc:
            response = web.json_response({"error": {"code": exc.code, "detail": exc.detail}}, status=exc.status)
            if exc.status == 429:
                response.headers["Retry-After"] = str(exc.detail["retry_after"])
            return response
        except (ValueError, TypeError, KeyError):
            return web.json_response({"error": {"code": "invalid_args"}}, status=422)
    return wrapped


def contract(args):
    if type(args.get("contract_version", 2)) is not int or args.get("contract_version", 2) != 2:
        raise ExecutionError("unsupported_owner_contract")


def signed_in_work_approval(operation):
    """Root-configured local desktop policy; never a caller-selected bypass."""
    return (os.environ.get("BOB_OWNER_WORK_APPROVAL") == "signed_in_user"
            and operation in WORK_APPROVAL_OPERATIONS)


@router.post("/auth/owner/begin")
@errors
async def begin(request):
    claims, link = portal(request)
    args = await request.json()
    only(args, {"operation", "resource", "proposal_revision", "contract_version"})
    contract(args)
    args = decode_integers(args)
    operation = args.get("operation")
    if operation not in OWNER_OPERATIONS:
        raise ExecutionError("unsupported_operation")
    # Read the authoritative resource/proposal through the same scoped vouch path.
    # The summary never trusts a caller-authored description of an action.
    from routers.execution import proxy
    resource = args.get("resource", {})
    only(resource, {"proposal_id", "run_id", "project_id", "grant_id", "provider", "flag_id", "job_id", "conversation_id", "approval_id", "requested_mode"})
    if 'requested_mode' in resource:
        if operation != 'builder.mode.set' or resource['requested_mode'] not in {'suggest','approve','auto','yolo'}:
            raise ExecutionError('invalid_args')
    if operation in {"secrets.set", "policy.get", "policy.set"}:
        from routers.settings import capabilities
        if not capabilities()[operation]["available"]:
            raise ExecutionError("settings_unavailable", 503)
        if operation == "secrets.set":
            from core.settings_contract import PROVIDERS
            if set(resource) != {"provider"} or resource["provider"] not in PROVIDERS:
                raise ExecutionError("unsupported_provider")
        elif resource:
            raise ExecutionError("invalid_args")
        read_op, read_args = None, None
    elif operation == "workflows.grant_start":
        read_op, read_args = "workflows.proposal", {"proposal_id": text(resource.get("proposal_id"), 128), "contract_version":1}
    elif operation in {"workflows.resume", "workflows.gate"}:
        read_op, read_args = "workflows.run", {"run_id": text(resource.get("run_id"), 128), "contract_version":1}
    elif operation == "grants.issue":
        read_op, read_args = "grants.proposal", {"proposal_id": text(resource.get("proposal_id"), 128)}
    elif operation == "runs.resume":
        read_op, read_args = "runs.get", {"run_id": text(resource.get("run_id"), 128)}
    elif operation in {"jobs.decide", "work.publish"}:
        read_op, read_args = "jobs.proposal", {"job_id": text(resource.get("job_id"), 128)}
    elif operation == "flags.resolve":
        read_op, read_args = "flags.get", {"flag_id": text(resource.get("flag_id"), 128)}
    else:
        read_op, read_args = "owner.summary", {"operation": operation, "resource": resource}
    request["user"] = claims
    if read_op is None:
        summary = {"operation": operation, "owner": claims["sub"], "lane": lane_id(), **resource}
    else:
        response = await proxy(request, read_op, read_args)
        if response.status != 200:
            return response
        summary = json.loads(response.body)
        if operation == 'work.publish':
            summary.pop('applicable',None)
            summary.pop('apply_reason',None)
            summary.update(operation='work.publish',target='bob-lane-workspace',
                           publication_requires='Completed checked job, owner review and unchanged source snapshot')
    if args.get("proposal_revision") is not None and (summary.get("proposal", summary.get("run", summary))).get("revision") != args["proposal_revision"]:
        raise ExecutionError("revision_conflict", 409)
    challenge = secrets.token_urlsafe(32)
    now = clock()
    async with _sqlite_connection() as db:
        async with db.execute("SELECT epoch FROM owner_epochs WHERE lane=? AND owner=?", (lane_id(), claims["sub"])) as c:
            epoch = (await c.fetchone())[0]
        await db.execute("INSERT INTO owner_challenges VALUES (?,?,?,?,?,?,?,?,?,0)",
                         (challenge, lane_id(), claims["sub"], epoch, claims["jti"], link, operation,
                          json.dumps(summary), now + 120))
        await db.commit()
    return web.json_response({"challenge_id": challenge, "expires_at": now + 120, "summary": summary,
                              "confirmation_mode": "signed_in_user" if signed_in_work_approval(operation) else "password_totp"})


@router.post("/auth/owner/confirm")
@errors
async def confirm(request):
    from auth import verify_password_plain
    from credential_auth import require_loaded
    require_loaded()
    claims, link = portal(request)
    args = await request.json()
    only(args, {"challenge_id", "password", "totp", "confirmation_mode", "contract_version"})
    contract(args)
    challenge = text(args.get("challenge_id"), 128)
    signed_in = args.get("confirmation_mode") == "signed_in_user"
    if "confirmation_mode" in args and (not signed_in or "password" in args or "totp" in args):
        raise ExecutionError("invalid_args", 422)
    password, code = ("", "") if signed_in else (text(args.get("password"), 1024), text(args.get("totp"), 16))
    now = clock()
    result, failure = None, None
    async with _sqlite_connection() as db:
        db.row_factory = __import__("sqlite3").Row
        await db.execute("BEGIN IMMEDIATE")
        # Aggregate owner plus authenticated portal/link dimensions. Changing a
        # nonce cannot evade the aggregate backoff. Successful proof doesn't
        # erase failed attempts made by another link.
        from credential_auth import keys as attempt_keys
        keys = attempt_keys('owner.confirm', [claims['jti'], link])
        for key in keys:
            async with db.execute("SELECT * FROM owner_attempts WHERE key=?", (key,)) as c:
                attempt = await c.fetchone()
            if attempt and attempt["next_at"] > now:
                raise ExecutionError("stepup_throttled", 429, {"retry_after": max(1, int(attempt["next_at"] - now + 1))})
        async with db.execute("SELECT c.* FROM owner_challenges c JOIN owner_epochs e USING(lane,owner) "
                              "WHERE c.id=? AND c.epoch=e.epoch", (challenge,)) as c:
            row = await c.fetchone()
        valid = (row and not row["used"] and row["expires_at"] > now and
                 (row["lane"], row["owner"], row["portal_id"], row["link_nonce"]) ==
                 (lane_id(), claims["sub"], claims["jti"], link))
        if signed_in and valid and not signed_in_work_approval(row["operation"]):
            raise ExecutionError("owner_confirmation_required", 403)
        step = None
        if not signed_in and valid and config.TOTP_SECRET and verify_password_plain(password):
            totp = pyotp.TOTP(config.TOTP_SECRET)
            import hmac
            for candidate in range(int(now / 30) - config.TOTP_VALID_WINDOW, int(now / 30) + config.TOTP_VALID_WINDOW + 1):
                if hmac.compare_digest(code.encode("utf-8"), totp.at(candidate * 30).encode("utf-8")):
                    step = candidate
                    break
        if step is not None:
            # Share login's high-water mark as well as the per-lane owner mark.
            async with db.execute("SELECT MAX(last_timestep) FROM totp_timesteps WHERE user_id IN (?,?)", (claims["sub"], "admin")) as c:
                previous = (await c.fetchone())[0]
            if previous is not None and step <= previous:
                step = None
        if not (valid and signed_in) and step is None:
            for key in keys:
                await db.execute("INSERT INTO owner_attempts VALUES (?,1,?) ON CONFLICT(key) DO UPDATE SET "
                                 "failures=MIN(failures+1,16),next_at=?+MIN(300,(1 << MIN(failures,8)))",
                                 (key, now + 1, now))
            failure = ExecutionError("invalid_stepup", 401)
        else:
            if step is not None:
                for user in {claims["sub"], "admin"}:
                    await db.execute("INSERT INTO totp_timesteps VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET last_timestep=MAX(last_timestep,excluded.last_timestep)", (user, step))
            await db.execute("UPDATE owner_challenges SET used=1 WHERE id=?", (challenge,))
            await db.execute("UPDATE owner_sessions SET revoked=1 WHERE lane=? AND owner=? AND portal_id=? AND link_nonce=?",
                             (lane_id(), claims["sub"], claims["jti"], link))
            token = "bos1." + secrets.token_urlsafe(48)
            operations = (WORK_APPROVAL_OPERATIONS | {"owner.status", "owner.lock", "owner.logout"}) if signed_in else SESSION_OPERATIONS
            allowed = json.dumps(sorted(operations))
            await db.execute("INSERT INTO owner_sessions(token_hash,lane,owner,epoch,portal_id,link_nonce,expires_at,revoked,allowed_operations) VALUES (?,?,?,?,?,?,?,0,?)",
                             (token_hash(token), lane_id(), claims["sub"], row["epoch"], claims["jti"], link, now + 300, allowed))
            from security_notice import notice
            result = {**notice(), "owner_bearer": token, "owner_session": metadata({**dict(row), "expires_at": now + 300, 'allowed_operations': allowed})}
        require_loaded()
        await db.commit()
    if failure:
        raise failure
    return web.json_response(result)


@router.get("/auth/owner/status")
@errors
async def status(request):
    claims = await authenticate(request, "owner.status")
    async with _sqlite_connection() as db:
        db.row_factory = __import__("sqlite3").Row
        async with db.execute("SELECT * FROM owner_sessions WHERE token_hash=?", (claims["jti"],)) as c:
            row = await c.fetchone()
    return web.json_response({"owner_session": metadata(row)})


@router.post("/auth/owner/lock")
@router.post("/auth/owner/logout")
@errors
async def lock(request):
    claims = await authenticate(request, "owner.lock")
    args = await request.json()
    only(args, {"contract_version"})
    contract(args)
    async with _sqlite_connection() as db:
        await db.execute("UPDATE owner_sessions SET revoked=1 WHERE token_hash=?", (claims["jti"],))
        await db.commit()
    from app_state import POSTGRES_POOL_KEY
    from core.builder.lifecycle import end_sessions
    from core.execution.api import lane_id
    await end_sessions(request.app.get(POSTGRES_POOL_KEY),lane_id(),claims['sub'],reason='owner_logged_out')
    return web.json_response({"owner_session": {"active": False}})
