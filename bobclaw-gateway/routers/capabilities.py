"""BoBClaw Gateway — capabilities registry endpoint (read-only aggregation).

``GET /capabilities`` fans out three concurrent GETs to core's existing registry read
surfaces (``/api/faces`` + ``/api/backends`` + ``/api/models/available``) and composes them
into a single read-only document that BOTH the TUI slash-command palette (``CapabilitiesClient``
seam) and the desktop-app ``/`` palette consume from ONE call — build it once, both read it.

JWT-gated by the gateway ``auth_middleware`` like every other route (``/capabilities`` is not on
the public allowlist). Read-only: GETs only, no mutation, no session/app-state writes.

Degrade posture (palette-backing): a per-component failure (a core connection error, a non-200
status, or a JSON decode error) degrades THAT component to its empty value plus a top-level
``warnings`` entry; the endpoint still returns 200 with whatever it composed. Only when ALL
three component fetches fail does it surface 502 (mirrors the sibling ``routing_view`` posture).
"""
import asyncio
import json

import aiohttp
from aiohttp import web

from config import config
from core.lane_config import configured_mode
from core.portal_resources import OP_CAPABILITIES
from core.execution.http_cache import conditional_json

router = web.RouteTableDef()

# Empty fallback per component so a partial outage still composes a valid document.
_EMPTY = {
    "faces": [],
    "backends": {"items": [], "roles": []},
    "models": [],
}

# U2 (Decision D10): display-metadata keys guaranteed on every faces[] entry so the
# client sees a STABLE schema — present ⇒ passed through verbatim, absent ⇒ null.
_FACE_DISPLAY_FIELDS = ("display_name", "blurb", "simple_slot")


def _load_actions() -> tuple[list, str | None]:
    """Return the U3 action registry as ``(entries, warning)``.

    The action registry (SPEC §3 / D4) is STATIC core-side data — not a runtime read surface —
    so, like ``core.permissions`` elsewhere in the gateway, it is imported directly rather than
    fetched over HTTP. On any failure it degrades to ``([], warning)`` so the ``actions`` section
    stays additive and null-safe (the endpoint never fails on its account).
    """
    try:
        from core.actions import get_default_registry

        return get_default_registry().as_payload(), None
    except Exception as exc:  # noqa: BLE001 — never let the actions section break /capabilities
        return [], f"actions: {type(exc).__name__}: {exc}"[:160]


async def _fetch_json(
    session: aiohttp.ClientSession, url: str, component: str
) -> tuple[object, str | None]:
    """GET *url* and return ``(parsed_data, warning)``.

    On success ``warning`` is ``None``. On any failure (connection error, non-200 status, or a
    non-JSON body) returns the component's empty value plus a short human-readable warning
    string, so the caller degrades that component instead of failing the whole request.
    """
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return _EMPTY[component], f"{component}: HTTP {resp.status}"
            data = await resp.json()
            return data, None
    except (aiohttp.ClientError, ValueError) as exc:
        return _EMPTY[component], f"{component}: {type(exc).__name__}: {exc}"[:160]


@router.get("/capabilities")
async def get_capabilities(request: web.Request) -> web.Response:
    """Serve the aggregated live registry (faces / backends / capabilities), read-only.

    Fans out three concurrent GETs to core over ONE shared session, merges the backends by name
    (union of ``/api/backends`` cost caps + ``/api/models/available`` availability), and returns
    a stable, sorted JSON document. Partial failures degrade to a ``warnings`` list; a total core
    outage surfaces 502.
    """
    # Read-only feature discovery does not depend on the model registry being up.
    if request.query.get('operations') == '1':
        operations = {**OP_CAPABILITIES}
        execution_features = {}
        job_time = {'version':1, 'available':False, 'reason':'worker_boundary_required'}
        for name in (('teams.list', 'teams.save', 'runs.start', 'runs.get', 'runs.list', 'runs.cancel', 'runs.resume', 'budgets.set', 'flags.list', 'flags.get')
                     if request.query.get('contract_version') == '2' else ()):
            operations[name] = {'version': 2, 'implemented': True, 'contract_versions': [2],
                                'request_key_required': name in {'teams.save', 'runs.start', 'runs.cancel', 'runs.resume', 'budgets.set'},
                                'human_owner_required': name in {'runs.resume', 'budgets.set'},
                                'storage_required': 'postgres'}
        if request.query.get('contract_version') == '2':
            from core.execution.owner import operation_capabilities
            operations.update(operation_capabilities())
            state = {}
            # Availability is per operation, independent of model-catalog health.
            # SQLite session status/lock remain usable during a core outage.
            if operations['owner.begin']['available']:
                from routers.execution import proxy
                live = await proxy(request, 'capabilities', {})
                state = json.loads(live.body) if live.status == 200 else {}
                from core.execution.models import CAPABILITIES
                ready = bool(state.get('storage_available') and state.get('execution_enabled'))
                job_time = state.get('job_time') or {**CAPABILITIES['job_time'], 'available':False,
                            'reason':'installed_job_ladder_unavailable'}
                for name, capability in state.get('operations', {}).items():
                    if name in operations:
                        operations[name].update(capability)
                execution_features = {k:state[k] for k in ('capabilities_revision', 'grant_matcher_versions') if k in state}
                if not state.get('storage_available') or not state.get('execution_enabled'):
                    for name in ('owner.begin', 'grants.prepare', 'grants.issue', 'grants.list', 'grants.get', 'grants.revoke',
                                 'budgets.set', 'budgets.get', 'budgets.list', 'budgets.remove', 'runs.resume', 'flags.resolve'):
                        operations[name].update(available=False, reason='execution_storage_or_executor_unavailable')
            if not config.TOTP_SECRET or not config.BOBCLAW_PASSWORD:
                for name in ('owner.begin', 'owner.confirm'):
                    operations[name].update(available=False, implemented=False, reason='enrolled_password_totp_required')
            from core.tool_jobs.service import OPERATIONS as JOB_OPERATIONS
            for name in JOB_OPERATIONS:
                operations[name] = state.get('operations', {}).get(name) or {
                    'contract_versions':[2], 'implemented':True, 'available':False,
                    'reason':'execution_storage_or_executor_unavailable', 'owner_session_required':name=='jobs.decide'}
            from core.execution.workbench import OPERATIONS as WORK_OPERATIONS
            from core.workflow_surface import capabilities as workflow_capabilities
            for name, fallback in workflow_capabilities(False).items():
                operations[name] = state.get('operations', {}).get(name) or fallback
            from core.builder.capabilities import capabilities as builder_capabilities
            for name, fallback in builder_capabilities(False, {}).items():
                operations[name] = state.get('operations',{}).get(name) or fallback
                if name in {'builder.mode.set','builder.approve','builder.network.set'} and not operations['owner.begin']['available']:
                    operations[name].update(available=False,reason=operations['owner.begin']['reason'])
            for name in WORK_OPERATIONS:
                operations[name]=state.get('operations',{}).get(name) or {
                    'contract_versions':[2],'implemented':True,'available':False,'reason':'execution_storage_or_executor_unavailable'}
                if name in {'work.workspace.create','work.publish'} and not operations['owner.begin']['available']:
                    operations[name].update(available=False,reason=operations['owner.begin']['reason'])
            if not operations['owner.begin']['available']:
                operations['jobs.decide'].update(available=False,reason=operations['owner.begin']['reason'])
            execution_features['tool_jobs'] = state.get('tool_jobs', {'protocols':['tool-jobs/2'],
                'apply':{'available':False,'reason':'managed_apply_unavailable_until_4.3e'}})
            from routers.settings import capabilities as settings_capabilities
            operations.update(settings_capabilities())
            if not state.get('storage_available'):
                for name in ('policy.get', 'policy.set'):
                    if operations[name]['available']:
                        operations[name].update(available=False, reason='policy_storage_unavailable')
            # Secret storage/owner summaries need only SQLite and the root helper.
            if (operations['secrets.set']['available'] and config.TOTP_SECRET and config.BOBCLAW_PASSWORD
                    and operations['owner.begin']['reason'] == 'execution_storage_or_executor_unavailable'):
                operations['owner.begin'].update(available=True, reason=None)
            operations['settings.status'] = {'version': 1, 'contract_versions': [1],
                'implemented': True, 'available': operations['secrets.set']['available'],
                'reason': operations['secrets.set']['reason'], 'transport': 'broker_receipt_socket',
                'credential_retry': 'never', 'owner_session_required': False}
            for name in ('credentials.change', 'recovery.begin', 'recovery.confirm'):
                operations[name] = {'version': 2, 'contract_versions': [2], 'implemented': False,
                                    'available': False, 'reason': 'privileged_helper_later_slice_required'}
        return conditional_json(request, {'operations': operations, 'job_time':job_time, **execution_features})

    # Read CORE_URL at request time (NOT module import) — tests and launchers override
    # config.CORE_URL, exactly as the sibling faces/models/routing_view routers do.
    core_base = config.CORE_URL.rstrip("/")

    async with aiohttp.ClientSession() as session:
        (faces_data, faces_warn), (backends_data, backends_warn), (models_data, models_warn) = (
            await asyncio.gather(
                _fetch_json(session, f"{core_base}/api/faces", "faces"),
                _fetch_json(session, f"{core_base}/api/backends", "backends"),
                _fetch_json(session, f"{core_base}/api/models/available", "models"),
            )
        )

    warnings = [w for w in (faces_warn, backends_warn, models_warn) if w]

    # Total core outage → 502 (mirrors routing_view); a partial outage degrades below.
    if faces_warn and backends_warn and models_warn:
        raise web.HTTPBadGateway(
            text=json.dumps({"error": "capabilities: core registry unreachable"}),
            content_type="application/json",
        )

    # Pass faces through, but additively guarantee the U2 display-metadata keys on
    # every entry (null-safe: a face lacking a key gets it as null). Never mutates the
    # source dict; non-dict entries (defensive) pass through untouched.
    faces = [
        {**f, **{k: f.get(k) for k in _FACE_DISPLAY_FIELDS}} if isinstance(f, dict) else f
        for f in (faces_data if isinstance(faces_data, list) else [])
    ]
    backends_items = backends_data.get("items", []) if isinstance(backends_data, dict) else []
    backends_roles = backends_data.get("roles", []) if isinstance(backends_data, dict) else []
    models_list = models_data if isinstance(models_data, list) else []
    # In a configured lane, model rows are the authoritative membership. Cost
    # metadata cannot enroll adapters absent from that core catalog. A failed
    # model component leaves membership empty rather than guessing from costs.
    configured_names = ({m.get("backend") for m in models_list if isinstance(m, dict)}
                        if configured_mode() else None)

    # Merge one entry per backend name = union of availability (models/available) + cost caps
    # (/api/backends). Tolerate a backend present in only one source; never KeyError.
    merged: dict[str, dict] = {}
    for m in models_list:
        if not isinstance(m, dict):
            continue
        name = m.get("backend")
        if not name:
            continue
        merged[name] = {
            "backend": name,
            "available": bool(m.get("available", False)),
            "model": m.get("model"),
            **{key: m[key] for key in ("provider_label", "choices", "tool_capable", "billing") if key in m},
            "max_usd_per_worker": None,
            "max_fanout_width": None,
        }
    for item in backends_items:
        if not isinstance(item, dict):
            continue
        name = item.get("backend")
        if not name:
            continue
        if configured_names is not None and name not in configured_names:
            continue
        entry = merged.get(name)
        if entry is None:
            entry = merged[name] = {
                "backend": name,
                "available": False,
                "model": None,
                "max_usd_per_worker": None,
                "max_fanout_width": None,
            }
        entry["max_usd_per_worker"] = item.get("max_usd_per_worker")
        entry["max_fanout_width"] = item.get("max_fanout_width")

    backends_list = sorted(merged.values(), key=lambda e: e["backend"])
    available_backends = [b["backend"] for b in backends_list if b["available"]]

    # U3 (Decision D4): additive ``actions`` section — the static core-side action registry
    # (§3), served alongside faces/backends so the ONE /capabilities call also backs the command
    # palette, the helper bubble, and voice. Null-safe: a load failure degrades to [] + a warning.
    actions, actions_warn = _load_actions()
    if actions_warn:
        warnings.append(actions_warn)

    doc: dict = {
        "operations": OP_CAPABILITIES,
        "faces": faces,
        "backends": backends_list,
        "actions": actions,
        "capabilities": {
            "roles": backends_roles,
            "face_count": len(faces),
            "backend_count": len(backends_list),
            "available_backends": available_backends,
            "action_count": len(actions),
        },
    }
    if warnings:
        doc["warnings"] = warnings

    return web.json_response(doc)
