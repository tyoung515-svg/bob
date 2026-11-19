"""Vouched core commands and production lifecycle wiring."""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

from aiohttp import web

from .admission import Admission
from .dispatcher import Dispatcher, exact_models
from .executor import Executor
from .models import Authority, CAPABILITIES, ExecutionError, integer, only, request_key, wire_integers, decode_integers
from .store import Store
from .workbench import capabilities as work_capabilities

SERVICE_KEY = web.AppKey("durable_execution", Admission)
EXECUTOR_KEY = web.AppKey("durable_executor", Executor)
router = web.RouteTableDef()


def lane_id():
    # Both lane services receive the same root; standalone core/gateway cwd may differ.
    root = Path(os.environ.get("BOB_LANE_ROOT", Path(__file__).resolve().parents[3])).resolve()
    return hashlib.sha256(str(root).encode()).hexdigest()


def policy_epoch(secret):
    from core.permissions import compute_scope_vouch
    return compute_scope_vouch("bob-execution-policy/v1", secret)


async def check_resources(pool, auth, args, *, allow_agent_project=False):
    """Independent project/conversation ownership, even for owner list filters."""
    project, conversation = args.get("project_id"), args.get("conversation_id")
    if not project and not conversation:
        return
    if pool is None:
        raise ExecutionError("resource_store_unavailable", 503)
    try:
        import uuid
        if project:
            row = await pool.fetchrow("SELECT id FROM projects WHERE id=$1 AND user_id=$2 AND is_archived=FALSE",
                                      uuid.UUID(project), auth.owner)
            if not row:
                raise ExecutionError("project_not_found", 404)
        if conversation:
            row = await pool.fetchrow("SELECT project_id FROM conversations WHERE id=$1 AND user_id=$2",
                                      uuid.UUID(conversation), auth.owner)
            if not row or (project and str(row["project_id"]) != project):
                raise ExecutionError("conversation_not_found", 404)
        # An owner-reviewed matching grant is the project ACL for text execution.
        # Proposal-only checks can inspect ownership but never dispatch. No grant
        # opens conversation data or infers resource permission from path strings.
        if auth.token_type == "agent" and (conversation or (project and not allow_agent_project)):
            raise ExecutionError("resource_not_found", 404)
    except (ValueError, TypeError, AttributeError):
        raise ExecutionError("resource_not_found", 404) from None


@router.post("/api/execution/{operation}")
async def command(request):
    from core.config import config
    from core.permissions import verify_execution_vouch
    try:
        value = await request.json()
        only(value, {"envelope", "vouch"})
        envelope = value.get("envelope")
        if not verify_execution_vouch(envelope, value.get("vouch"), config.BOBCLAW_SECRET):
            raise ExecutionError("invalid_execution_vouch", 403)
        op = request.match_info["operation"]
        if op != envelope["operation"]:
            raise ExecutionError("invalid_execution_vouch", 403)
        raw_auth = envelope["authority"]
        only(raw_auth, Authority.__dataclass_fields__)
        auth = Authority(**raw_auth)
        if (auth.lane != lane_id() or not auth.owner or not auth.actor or not isinstance(auth.scope, dict)
                or auth.token_type not in {"human", "agent", "portal"}):
            raise ExecutionError("not_found", 404)
        if not auth.policy_epoch or auth.policy_epoch != policy_epoch(config.BOBCLAW_SECRET):
            raise ExecutionError("authorization_revoked", 403)
        from .owner import OWNER_OPERATIONS, require_owner, owner_mapping, check_epoch, unavailable_reason, operation_capabilities
        if op in OWNER_OPERATIONS:
            await require_owner(auth, op)
        elif auth.session_kind or auth.audience:
            raise ExecutionError("owner_audience_denied", 403)
        elif owner_mapping(auth.owner):
            await check_epoch(auth)
        if op.startswith("grants.") or op == "owner.summary":
            if not owner_mapping(auth.owner):
                raise ExecutionError("owner_controls_unavailable", 403, unavailable_reason())
        service = request.app.get(SERVICE_KEY)
        if not service:
            raise ExecutionError("execution_storage_unavailable", 503)
        args = decode_integers(envelope["args"])
        if not isinstance(args, dict):
            raise ExecutionError("invalid_args")
        if op == "capabilities":
            available = await service.store.available()
            from .owner import job_time_capabilities
            job_time = await job_time_capabilities(execution_ready=available and service.enabled)
            from core.tool_jobs.service import capabilities as job_capabilities
            scoped_jobs = await job_capabilities(available and service.enabled, job_time=job_time)
            from core.builder.capabilities import capabilities as builder_capabilities
            from core.workflow_surface import capabilities as surface_workflow_capabilities
            def workflow_capabilities(ready):
                controller=getattr(service.store,'workflow_controller',None)
                return surface_workflow_capabilities(ready,bool(controller and controller.running))
            return web.json_response(wire_integers({**CAPABILITIES, "tool_jobs": scoped_jobs, "worker_jobs": scoped_jobs["operations"]["jobs.start"]["available"], "job_time":{**job_time,"jobs_api":scoped_jobs["operations"]["jobs.start"]["available"]}, "operations": {**operation_capabilities(job_time), **scoped_jobs["operations"], **work_capabilities(available and service.enabled, job_time), **builder_capabilities(available, job_time), **workflow_capabilities(available)}, "storage_available": available,
                                      "execution_enabled": available and service.enabled,
                                      "blocked_reasons": [] if available and service.enabled else ["execution_storage_or_executor_unavailable"]}))
        if op.startswith('workflows.'):
            from core.workflow_surface import WorkflowSurface as WorkflowService
            result = await WorkflowService(service).command(auth, op, args)
        elif op.startswith('pc.'):
            from .pc import PC
            result = await PC(service.store.pool).command(auth, op, args)
        elif op.startswith("builder.") or op == 'workspace.browse':
            if not owner_mapping(auth.owner):
                raise ExecutionError('not_found',404)
            from core.builder.service import Builder
            from core.portal_resources import ResourceError
            try:
                result = await Builder(service.store.pool).command(auth, op, args)
            except ResourceError as exc:
                raise ExecutionError(exc.code,exc.status) from None
        elif op.startswith("work."):
            if not owner_mapping(auth.owner):
                raise ExecutionError("not_found",404)
            from .workbench import Workbench
            result = await Workbench(service).command(auth, op, args, face=envelope.get('face_id'))
        elif op.startswith("jobs."):
            if not owner_mapping(auth.owner):
                raise ExecutionError("not_found",404)
            from core.tool_jobs.service import Jobs
            result = await Jobs(service).command(auth, op, args)
        elif op == "teams.list":
            result = await service.teams(auth, args)
        elif op == "teams.save":
            result = await service.save(auth, args)
        elif op == "runs.start":
            # Store.start atomically requires/binds a matching grant for agent
            # projects, even below 200. A failed check rolls admission back.
            await check_resources(service.store.pool, auth, args, allow_agent_project=bool(owner_mapping(auth.owner)))
            result = await service.start(auth, args)
        elif op == "runs.get":
            only(args, {"run_id", "after_event_seq", "receipt_cursor"})
            result = await service.store.get(auth, args.get("run_id"), integer(args.get("after_event_seq", 0), 0),
                                             integer(args.get("receipt_cursor", 0), 0, 2147483647))
            await check_resources(service.store.pool, auth, result["run"], allow_agent_project=bool(result["run"].get("selected_grant_id")))
        elif op == "runs.list":
            only(args, {"cursor", "limit", "project_id", "status"})
            await check_resources(service.store.pool, auth, args, allow_agent_project=bool(owner_mapping(auth.owner)))
            result = await service.store.runs(auth, args.get("cursor"), integer(args.get("limit", 20), 1, 50),
                                              args.get("project_id"), args.get("status"))
            # Project authorization is revalidated on every read, not just start.
            visible = []
            for row in result["items"]:
                try:
                    await check_resources(service.store.pool, auth, row, allow_agent_project=bool(row.get("selected_grant_id")))
                    visible.append(row)
                except ExecutionError as exc:
                    if exc.status != 404:
                        raise
            result["items"] = visible
        elif op == "runs.cancel":
            only(args, {"run_id", "request_key"})
            request_key(args.get("request_key"))
            existing = await service.store.get(auth, args.get("run_id"))
            await check_resources(service.store.pool, auth, existing["run"], allow_agent_project=bool(existing["run"].get("selected_grant_id")))
            receipt = await service.store.cancel(auth, args["run_id"], args["request_key"])
            result = {**await service.store.get(auth, receipt["run_id"]), "operation_id": receipt["operation_id"]}
        elif op in {"flags.list", "flags.get", "flags.resolve"}:
            if op == "flags.list":
                result = await service.store.flags(auth, args)
                visible = []
                for flag in result["items"]:
                    try:
                        run = (await service.store.get(auth, flag["run_id"]))["run"]
                        await check_resources(service.store.pool, auth, run, allow_agent_project=bool(run.get("selected_grant_id")))
                        visible.append(flag)
                    except ExecutionError as exc:
                        if exc.status != 404:
                            raise
                result["items"] = visible
            else:
                existing = await service.store.flag_get(auth, args if op == "flags.get" else {"flag_id": args.get("flag_id")})
                await check_resources(service.store.pool, auth, existing["run"], allow_agent_project=bool(existing["run"].get("selected_grant_id")))
                result = await service.store.flag_resolve(auth, args) if op == "flags.resolve" else existing
        elif op == "runs.resume":
            if auth.token_type != "human" or auth.actor != auth.owner:
                raise ExecutionError("not_found", 404)
            if not service.enabled:
                raise ExecutionError("execution_executor_unavailable", 503)
            existing = await service.store.get(auth, args.get("run_id"))
            await check_resources(service.store.pool, auth, existing["run"])
            receipt = await service.store.resume(auth, args)
            result = {**await service.store.get(auth, receipt["run_id"]), "operation_id": receipt["operation_id"]}
        elif op == "budgets.set":
            if auth.token_type != "human" or auth.actor != auth.owner:
                raise ExecutionError("not_found", 404)
            await check_resources(service.store.pool, auth, args)
            result = await service.store.set_budget(auth, args)
        elif op == "budgets.get":
            only(args, {"project_id"})
            await check_resources(service.store.pool, auth, args)
            result = await service.store.get_budget(auth, args.get("project_id"))
        elif op == "budgets.list":
            result = await service.store.list_budgets(auth, args)
            visible = []
            for item in result["items"]:
                try:
                    await check_resources(service.store.pool, auth, item)
                    visible.append(item)
                except ExecutionError as exc:
                    if exc.status != 404:
                        raise
            result["items"] = visible
        elif op == "budgets.remove":
            await check_resources(service.store.pool, auth, args)
            result = await service.store.remove_budget(auth, args)
        elif op == "grants.prepare":
            if auth.token_type not in {"agent", "portal"}:
                raise ExecutionError("not_found", 404)
            if args.get("run_id"):
                existing = await service.store.get(auth, args["run_id"])
                await check_resources(service.store.pool, auth, existing["run"], allow_agent_project=bool(existing["run"].get("selected_grant_id")))
            else:
                await check_resources(service.store.pool, auth, args.get("request", {}), allow_agent_project=True)
            result = await service.prepare(auth, args)
        elif op == "grants.proposal":
            only(args, {"proposal_id"})
            result = await service.store.grant_proposal(auth, args.get("proposal_id"))
            await check_resources(service.store.pool, auth, result["coverage"]["request"], allow_agent_project=True)
        elif op == "grants.issue":
            # Original authority, team snapshot and live exact model bindings are
            # rechecked; the proposal's caller never gets to supply trusted digests.
            replay = await service.store.replay(auth, op, request_key(args.get("request_key")), args)
            if replay:
                return web.json_response(wire_integers(replay))
            proposal = await service.store.grant_proposal(auth, args.get("proposal_id"))
            coverage = proposal["coverage"]
            await check_resources(service.store.pool, auth, coverage["request"])
            await Admission.revalidate(service, {"snapshot": coverage["snapshot"]})
            if any(args.get(k) is not None and args.get(k) is not False for k in ("allow_extended_jobs", "allow_custom_job_limit", "job_seconds_ceiling", "job_override_seconds")):
                from .owner import job_time_capabilities
                jobs = await job_time_capabilities(execution_ready=service.enabled)
                if not jobs["available"]:
                    raise ExecutionError("job_grants_unavailable",409,jobs["reason"])
            result = await service.store.issue_grant(auth, args)
        elif op == "grants.get":
            only(args, {"grant_id"})
            result = await service.store.get_grant(auth, args.get("grant_id"))
        elif op == "grants.list":
            result = await service.store.list_grants(auth, args)
        elif op == "grants.revoke":
            result = await service.store.revoke_grant(auth, args)
        elif op == "policy.get":
            result = await service.store.get_policy(auth, args)
        elif op == "policy.set":
            from core.settings_client import installed_id
            installed_id()
            result = await service.store.set_policy(auth, args)
        elif op == "owner.summary":
            only(args, {"operation", "resource"})
            if auth.token_type != "portal" or args.get("operation") not in OWNER_OPERATIONS:
                raise ExecutionError("not_found", 404)
            resource = args.get("resource", {})
            only(resource, {"project_id", "grant_id", "conversation_id", "approval_id", "requested_mode"})
            if 'requested_mode' in resource and args['operation'] != 'builder.mode.set':
                raise ExecutionError('invalid_args')
            await check_resources(service.store.pool, auth, {k:v for k,v in resource.items() if k != 'requested_mode'})
            if args['operation'] == 'builder.network.set':
                from core.builder.service import Builder
                only(resource,{'project_id'})
                result = await Builder(service.store.pool).network.project(auth,resource.get('project_id'))
                result['project_id'] = resource['project_id']
            elif args['operation'] in {'builder.mode.set','builder.approve'}:
                from core.builder.service import Builder
                from core.builder.display import mode_display
                builder = Builder(service.store.pool)
                read_op = 'builder.mode.get' if args['operation']=='builder.mode.set' else 'builder.approval.get'
                only(resource, {'conversation_id','requested_mode'} if read_op == 'builder.mode.get' else {'approval_id'})
                result = await builder.command(auth,read_op,{k:v for k,v in resource.items() if k != 'requested_mode'})
                if read_op == 'builder.mode.get':
                    result['requested_mode'] = resource.get('requested_mode')
                    result['display'] = mode_display(result['mode'],resource.get('requested_mode'))
            elif "grant_id" in resource:
                result = await service.store.get_grant(auth, resource["grant_id"])
            elif args['operation']=='work.workspace.create':
                only(resource,{'project_id'})
                import uuid
                project=await service.store.pool.fetchrow(
                    'SELECT name FROM projects WHERE id=$1 AND user_id=$2 AND is_archived=FALSE',
                    uuid.UUID(resource['project_id']),auth.owner)
                if not project:raise ExecutionError('project_not_found',404)
                result={'project_id':resource['project_id'],'project_name':project['name'],'target':'bob-lane-workspace',
                        'path':'projects/'+resource['project_id'],'checks':['python.compile','python.unittest'],
                        'write_paths':['app'],'windows_folder_access':False}
                result['display'] = {'title':f'Create workspace for “{project["name"]}” (empty folder in your lane)',
                                     'lines':[], 'scope':'This project only.'}
            elif "project_id" in resource:
                result = await service.store.get_budget(auth, resource["project_id"])
            else:
                result = {"owner": auth.owner, "lane": auth.lane}
            result = {**result, "operation": args["operation"]}
        else:
            raise ExecutionError("unsupported_operation", 404)
        return web.json_response(wire_integers(result), status=202 if op in {"runs.start","jobs.start"} else 200)
    except ExecutionError as exc:
        return web.json_response(wire_integers({"error": {"code": exc.code, "detail": exc.detail}}), status=exc.status)
    except (ValueError, TypeError, KeyError):
        return web.json_response({"error": {"code": "invalid_args"}}, status=400)
    except Exception:
        logging.getLogger(__name__).exception("Durable command unavailable")
        return web.json_response({"error": {"code": "execution_storage_unavailable"}}, status=503)


async def initialize(app, pool, local_router):
    from core.lane_config import configured_mode, catalog, agent_service_definitions

    async def models():
        if configured_mode():
            definitions = agent_service_definitions(await catalog(local_router))
        else:
            from core.roster import get_roster
            definitions = {"models": {k: v.as_supplied() for k, v in get_roster().agent_services.models.items()}}
        return exact_models(definitions["models"])

    store = Store(pool, lane_id())
    service = Admission(store, models)
    service.enabled = False
    app[SERVICE_KEY] = service
    try:
        await store.initialize()
        from . import chat_records
        chat_records.POOL = pool
    except Exception:
        logging.getLogger(__name__).exception("Durable storage unavailable; start/resume disabled")
        return

    # Model and resource authority is rechecked on recovery and every dispatch.
    original_revalidate = service.revalidate

    async def revalidate(row):
        from core.config import config
        if row["run_authority"].get("policy_epoch") != policy_epoch(config.BOBCLAW_SECRET):
            raise ExecutionError("authorization_revoked", 409)
        await check_resources(pool, Authority(**row["run_authority"]), row["request"], allow_agent_project=bool(row.get("selected_grant_id")))
        from .owner import check_epoch, owner_mapping
        if owner_mapping(row["owner"]) or row["run_authority"].get("credential_epoch") is not None:
            await check_epoch(Authority(**row["run_authority"]))
        await original_revalidate(row)

    service.revalidate = revalidate
    executor = Executor(store, service, Dispatcher())
    app[EXECUTOR_KEY] = executor
    executor.start()
    service.enabled = True
    from . import policy as owner_policy
    owner_policy.POOL = pool
    from core.worker import mode
    if mode() == 'worker':
        import asyncio
        from core.worker.client import queue_bridge
        # Reconnect recovery does not depend on a new child execution request.
        try:
            bridge = await asyncio.to_thread(queue_bridge)
            await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(bridge.ready)),timeout=15)
        except Exception:
            logging.getLogger(__name__).exception('Worker queue unavailable; child dispatch disabled')


async def cleanup(app):
    if app.get(EXECUTOR_KEY):
        await app[EXECUTOR_KEY].close()
    import asyncio
    from core.worker.client import close_queues
    await asyncio.to_thread(close_queues)
