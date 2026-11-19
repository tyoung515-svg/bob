"""Owner-control contract and replaceable credential-epoch reader.

Installed credential generations use the root-published nonsecret pointer;
pre-generation lanes read gateway SQLite through a read-only connection.
No default admin mapping, schema creation, or secret loading happens here.
"""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import time

from .models import ExecutionError

OWNER_OPERATIONS = frozenset({"runs.resume", "budgets.set", "budgets.get", "budgets.list",
                              "budgets.remove", "grants.issue", "grants.list", "grants.get", "grants.revoke",
                              "secrets.set", "policy.get", "policy.set", "flags.resolve", "jobs.decide",
                              "work.workspace.create", "work.publish", "builder.mode.set", "builder.approve", "builder.network.set",
                              "workflows.grant_start", "workflows.resume", "workflows.gate"})
SESSION_OPERATIONS = OWNER_OPERATIONS | {"owner.status", "owner.lock", "owner.logout"}
# Operations a signed-in desktop owner may approve with a click (BOB_OWNER_WORK_APPROVAL).
# Storing a provider key is included (owner decision 2026-11-16): it is write-only, the key
# is never returned. The approval policy itself still needs password + authenticator, as do
# password and factor changes, which do not use owner sessions at all.
WORK_APPROVAL_OPERATIONS = OWNER_OPERATIONS - {"policy.get", "policy.set"}
# Rows without a stored allowlist retain only their original 3.1 operations.
LEGACY_SESSION_OPERATIONS = SESSION_OPERATIONS - {'secrets.set', 'policy.get', 'policy.set', 'flags.resolve', 'jobs.decide', 'work.workspace.create', 'work.publish', 'builder.mode.set', 'builder.approve', 'builder.network.set', 'workflows.grant_start', 'workflows.resume', 'workflows.gate'}
AUDIENCE = "bob-owner-controls"


def session_operations(row):
    value = dict(row).get('allowed_operations')
    if value is None:
        return LEGACY_SESSION_OPERATIONS
    try:
        operations = json.loads(value)
        if not isinstance(operations, list) or any(not isinstance(op, str) for op in operations):
            return frozenset()
        return frozenset(operations) & SESSION_OPERATIONS
    except (ValueError, TypeError):
        return frozenset()


def unavailable_reason():
    from core.worker import mode
    if not os.environ.get("BOB_DEFAULT_OWNER", "").strip():
        return "explicit_lane_owner_required"
    if mode() != "worker":
        return "worker_boundary_required"
    return None


def owner_mapping(owner):
    return not unavailable_reason() and owner == os.environ.get("BOB_DEFAULT_OWNER")


def sqlite_path():
    return Path(os.environ.get("GATEWAY_SQLITE_PATH") or
                str(Path(os.environ.get("BOB_LANE_ROOT", ".")) / "gateway.db")).resolve()


def _read(sql, values):
    try:
        with sqlite3.connect(sqlite_path().as_uri() + "?mode=ro", uri=True, timeout=2) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(sql, values).fetchone()
            return dict(row) if row else None
    except sqlite3.Error:
        raise ExecutionError("owner_state_unavailable", 503) from None


async def current_epoch(lane, owner):
    if not owner_mapping(owner):
        raise ExecutionError("owner_controls_unavailable", 403, unavailable_reason())
    from core import credential_state
    if credential_state.enabled():
        try:
            return (await asyncio.to_thread(credential_state.active))['epoch']
        except Exception:
            raise ExecutionError('owner_state_unavailable', 503) from None
    row = await asyncio.to_thread(_read, "SELECT epoch FROM owner_epochs WHERE lane=? AND owner=?", (lane, owner))
    if not row:
        raise ExecutionError("owner_state_unavailable", 503)
    return row["epoch"]


async def check_epoch(auth):
    if type(auth.credential_epoch) is not int or auth.credential_epoch != await current_epoch(auth.lane, auth.owner):
        raise ExecutionError("credential_epoch_changed", 403)


async def require_owner(auth, operation, now=None):
    if (operation not in OWNER_OPERATIONS or auth.token_type != "human" or auth.actor != auth.owner
            or auth.session_kind != "owner_stepup" or auth.audience != AUDIENCE
            or operation not in auth.allowed_operations):
        raise ExecutionError("not_found", 404)
    await check_epoch(auth)
    row = await asyncio.to_thread(_read, "SELECT * FROM owner_sessions WHERE token_hash=?", (auth.credential_id,))
    if (not row or operation not in session_operations(row) or row["revoked"] or row["expires_at"] <= (time.time() if now is None else now)
            or (row["lane"], row["owner"], row["epoch"], row["portal_id"], row["link_nonce"])
            != (auth.lane, auth.owner, auth.credential_epoch, auth.portal_credential_id, auth.broker_link_nonce)):
        raise ExecutionError("owner_session_invalid", 403)


async def job_time_capabilities(*, execution_ready=True):
    """Live installed protocol support; an old/offline worker never advertises jobs."""
    from .models import CAPABILITIES
    result = {**CAPABILITIES["job_time"], "available":False, "reason":unavailable_reason()}
    if result["reason"]:
        return result
    if not execution_ready:
        return {**result, "reason":"execution_storage_or_executor_unavailable"}
    try:
        from core.worker.client import Transport, configuration
        cfg = configuration()
        hello = await asyncio.wait_for(asyncio.to_thread(lambda: Transport(cfg).hello()), 12)
        if hello.get("job_time_presets") != [900,1800,3600] or hello.get("max_timeout",0) < 3600:
            return {**result, "reason":"installed_job_ladder_unavailable"}
        return {**result, "available":True, "reason":None, "installed_max_seconds":hello["max_timeout"],
                "profile_revisions":hello["profiles"], "filesystem":hello.get('filesystem',{}),
                "scoped_workspace_root":bool(cfg.get('run_workspace_root') and Path(cfg['run_workspace_root']).is_dir())}
    except (OSError, RuntimeError, ValueError, TimeoutError, KeyError):
        return {**result, "reason":"installed_worker_unavailable"}


def operation_capabilities(job_time=None):
    reason = unavailable_reason()
    return {op: {"version": 2, "contract_versions": [2], "implemented": not bool(reason),
                 "available": not bool(reason), "reason": reason, "owner_session_required": op in SESSION_OPERATIONS,
                 "portal_credential_required": op in {"owner.begin", "owner.confirm"},
                 "request_key_required": op in {"grants.prepare", "grants.issue", "grants.revoke", "budgets.set", "budgets.remove", "runs.resume", "flags.resolve"},
                 "session_absolute_lifetime_seconds": 300, "session_refresh": False,
                 "integer_encoding": "safe_number_or_decimal_string", "job_grants_available": bool(job_time and job_time.get("available")),
                 "job_grants_reason": (job_time or {}).get("reason") or (None if job_time and job_time.get("available") else "installed_worker_not_negotiated"), "job_grant_matcher_version": 3}
            for op in SESSION_OPERATIONS | {"owner.begin", "owner.confirm", "grants.prepare"}}
