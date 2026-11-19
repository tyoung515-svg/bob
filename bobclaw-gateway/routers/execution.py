"""Verified gateway authority for core durable commands (no execution tasks)."""
import asyncio
import time

import aiohttp
from aiohttp import web

from config import config
from core.execution.api import lane_id, policy_epoch
from core.execution.models import Authority, ExecutionError
from core.permissions import execution_vouch

def authority(request):
    return authority_from_claims(request["user"])


def authority_from_claims(claims):
    """Only pass claims already authenticated by the gateway middleware/socket."""
    kind = claims.get("token_type", "human")
    owner = str(claims["sub"])
    actor = (str(claims.get("agent_id") or claims.get("sender_id") or claims.get("jti") or "agent")
             if kind == "agent" else owner)
    return Authority(lane_id(), owner, actor, claims.get("scope") or {}, kind,
                     claims.get("jti", ""), policy_epoch(config.BOBCLAW_SECRET),
                     claims.get("credential_epoch"), claims.get("session_kind", ""), claims.get("audience", ""),
                     claims.get("allowed_operations", ()), claims.get("portal_credential_id", ""),
                     claims.get("broker_link_nonce", ""))


async def chat_work_fields(claims, conversation_id, face_id):
    from dataclasses import replace
    from core.execution.chat_context import FACES, signature
    from core.execution.owner import current_epoch, owner_mapping
    if face_id not in FACES | {'assistant', 'planner'} or not claims or not claims.get('sub'):
        return {}
    auth = authority_from_claims(claims)
    if auth.session_kind or auth.audience:
        return {}
    if owner_mapping(auth.owner):
        auth = replace(auth,credential_epoch=await current_epoch(auth.lane,auth.owner))
    now = int(time.time())
    expires = min(int(claims.get('exp',now)),now+600)
    if expires <= now:
        return {}
    value = {'authority':auth.as_dict(),'conversation_id':conversation_id,
             'face_id':face_id,'iat':now,'exp':expires}
    result = {'work_context':value,'work_context_vouch':signature(value,config.BOBCLAW_SECRET)} if face_id in FACES else {}
    if face_id in {'assistant', 'planner', 'orchestrator'}:
        from core.tools.workflow_context import signature as workflow_signature
        if face_id in {'assistant','planner'}:
            allowed = claims.get('faces')
            allowed = [] if allowed is None else allowed
            if not isinstance(allowed,list) or len(allowed)>32 or any(not isinstance(f,str) for f in allowed):
                return {}
            from core.faces.registry import get_default_registry
            value = {**value,'inspection_faces':[f.id for f in get_default_registry().all_faces()
                if not allowed or f.id in allowed]}
        result.update(workflow_context=value, workflow_context_vouch=workflow_signature(value, config.BOBCLAW_SECRET))
    return result


async def chat_pc_fields(claims, conversation_id, face_id):
    from dataclasses import replace
    from core.execution.pc_context import signature
    from core.execution.owner import current_epoch, owner_mapping
    import re
    face_id=face_id or '*'
    if not isinstance(face_id,str) or not (face_id=='*' or re.fullmatch('[A-Za-z0-9_-]{1,80}',face_id)) or not claims or not claims.get('sub'):
        return {}
    auth=authority_from_claims(claims)
    if auth.session_kind or auth.audience or not owner_mapping(auth.owner):
        return {}
    auth=replace(auth,credential_epoch=await current_epoch(auth.lane,auth.owner))
    now=int(time.time());expires=min(int(claims.get('exp',now)),now+600)
    if expires<=now:
        return {}
    value={'authority':auth.as_dict(),'conversation_id':conversation_id,'face_id':face_id,'iat':now,'exp':expires}
    return {'pc_context':value,'pc_context_vouch':signature(value,config.BOBCLAW_SECRET)}


async def proxy(request, operation, args):
    from routers.agent_services import _authorize, ServiceError, error
    try:
        from core.execution.owner import OWNER_OPERATIONS, current_epoch, owner_mapping
        if operation in OWNER_OPERATIONS:
            from owner_sessions import authenticate
            request["user"] = await authenticate(request, operation)
        elif operation in {"grants.prepare", "grants.proposal", "owner.summary"}:
            from auth import decode_access_token, lane_from_scope
            header = request.headers.get("Authorization", "")
            claims = decode_access_token(header[7:]) if header.startswith("Bearer ") else None
            if (not claims or claims.get("token_type") not in {"agent", "portal"}
                    or not owner_mapping(claims.get("sub")) or lane_from_scope(claims.get("scope"))):
                raise ExecutionError("not_found", 404)
            request["user"] = claims
        if operation != "capabilities":
            _authorize(request)
        auth = authority(request)
        if operation not in OWNER_OPERATIONS and owner_mapping(auth.owner):
            from dataclasses import replace
            auth = replace(auth, credential_epoch=await current_epoch(auth.lane, auth.owner))
        now = int(time.time())
        envelope = {"operation": operation, "args": args, "authority": auth.as_dict(),
                    "iat": now, "exp": now + 30}
        vouch = execution_vouch(envelope, config.BOBCLAW_SECRET)
        if not vouch:
            raise ExecutionError("execution_vouch_unavailable", 503)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=650 if operation=='builder.approve' else 40)) as session:
            async with session.post(config.CORE_URL.rstrip("/") + "/api/execution/" + operation,
                                    json={"envelope": envelope, "vouch": vouch}) as response:
                return web.Response(body=await response.read(), status=response.status, content_type="application/json")
    except ExecutionError as exc:
        return web.json_response({"error": {"code": exc.code, "detail": exc.detail}}, status=exc.status)
    except ServiceError as exc:
        return error(exc)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return web.json_response({"error": {"code": "execution_core_unavailable"}}, status=503)


def query(request, allowed):
    if set(request.query) - set(allowed) - {"contract_version"}:
        raise ValueError("unsupported query")
    if request.query.get("contract_version") != "2":
        raise ValueError("unsupported_run_contract")
    args = {k: v for k, v in request.query.items() if k != "contract_version"}
    for key in ("limit", "after_event_seq", "receipt_cursor"):
        if key in args:
            args[key] = int(args[key])
    return args


async def owner_command(request, operation, fields):
    try:
        body = await request.json()
        if (not isinstance(body, dict) or type(body.get("contract_version")) is not int
                or body["contract_version"] != 2 or set(body) - fields - {"contract_version"}):
            raise ValueError()
        args = {k: v for k, v in body.items() if k != "contract_version"}
        if operation == "runs.resume":
            args["run_id"] = request.match_info["run_id"]
        if operation == "flags.resolve":
            args["flag_id"] = request.match_info["flag_id"]
        return await proxy(request, operation, args)
    except (ValueError, TypeError):
        return web.json_response({"error": {"code": "invalid_args"}}, status=422)


async def resume_run(request):
    return await owner_command(request, "runs.resume", {"expected_revision", "request_key", "budget"})


async def flags_read(request, detail=False):
    try:
        args = query(request, {"after_event_seq", "job_cursor"} if detail else {"run_id", "cursor", "limit", "status"})
        if detail:
            args["flag_id"] = request.match_info["flag_id"]
        return await proxy(request, "flags.get" if detail else "flags.list", args)
    except (ValueError, TypeError):
        return web.json_response({"error": {"code": "invalid_args"}}, status=422)


async def flags_resolve(request):
    return await owner_command(request, "flags.resolve", {"expected_revision", "request_key", "decision", "guidance"})


async def set_project_budget(request):
    return await owner_command(request, "budgets.set", {"project_id", "call_limit", "expected_revision", "request_key"})


async def grant_prepare(request):
    return await owner_command(request, "grants.prepare", {"request" , "request_key", "run_id", "expected_revision"})


async def grant_issue(request):
    return await owner_command(request, "grants.issue", {"proposal_id", "proposal_revision", "expected_revision", "request_key",
                                "coverage_kind", "allow_long_runs", "auto_escalate", "call_ceiling", "expires_at",
                                "allow_extended_jobs", "allow_custom_job_limit", "job_seconds_ceiling", "job_override_seconds", "job_auto_escalate"})


async def grant_revoke(request):
    return await owner_resource_command(request, "grants.revoke", "grant_id")


async def budget_remove(request):
    return await owner_resource_command(request, "budgets.remove", "project_id")


async def owner_resource_command(request, operation, field):
    try:
        body = await request.json()
        from core.execution.models import only
        only(body, {"contract_version", "expected_revision", "request_key"})
        if type(body.get("contract_version")) is not int or body["contract_version"] != 2:
            raise ValueError()
        return await proxy(request, operation, {**{k:v for k,v in body.items() if k != "contract_version"}, field: request.match_info[field]})
    except (ValueError, TypeError, ExecutionError):
        return web.json_response({"error": {"code": "invalid_args"}}, status=422)


async def owner_read(request, operation, field=None):
    try:
        args = query(request, {"cursor", "limit", "kind"} if field is None else set())
        if field:
            args[field] = request.match_info[field]
        return await proxy(request, operation, args)
    except (ValueError, TypeError):
        return web.json_response({"error": {"code": "invalid_args"}}, status=422)
