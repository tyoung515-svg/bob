"""Separately signed, invocation-local workflow authority. No owner sessions."""
from contextvars import ContextVar
import hmac
import logging
import re
import time

from aiohttp import web
from core.permissions import canonical_pipe_json, compute_scope_vouch
from core.execution.models import Authority, ExecutionError, only

READ_OPERATIONS = frozenset({'catalog', 'list', 'get', 'run', 'report'})
ASSISTANT_OPERATIONS = READ_OPERATIONS | {'propose', 'message', 'stop'}
current = ContextVar('workflow_invocation', default=None)
router = web.RouteTableDef()


def signature(value, secret):
    return compute_scope_vouch('bob-chat-workflows/v1\n' + canonical_pipe_json(value), secret)


def valid(value, proof, secret, *, owner, conversation, face):
    try:
        if (not isinstance(value, dict)
                or set(value) not in ({'authority', 'conversation_id', 'face_id', 'iat', 'exp'},
                    {'authority', 'conversation_id', 'face_id', 'iat', 'exp', 'inspection_faces'})
                or ('inspection_faces' in value and (not isinstance(value['inspection_faces'],list)
                    or len(value['inspection_faces'])>32 or any(not isinstance(f,str) for f in value['inspection_faces'])))
                or face not in {'assistant', 'planner', 'orchestrator'} or value['face_id'] != face
                or not isinstance(conversation, str) or not conversation
                or value['conversation_id'] != conversation or value['authority']['owner'] != owner
                or type(value['iat']) is not int or type(value['exp']) is not int
                or not 0 < value['exp']-value['iat'] <= 600
                or not value['iat'] <= time.time() < value['exp'] or not isinstance(proof, str)
                or not re.fullmatch(r'[a-f0-9]{64}', proof)):
            return False
        auth = Authority(**value['authority'])
        return (bool(auth.owner and auth.actor) and auth.token_type in {'human', 'portal', 'agent'}
                and not auth.session_kind and not auth.audience
                and hmac.compare_digest(signature(value, secret), proof))
    except (KeyError, TypeError, ValueError):
        return False


def bind(state):
    from core.config import config
    value = state.get('workflow_context')
    accepted = value if valid(value, state.get('workflow_context_vouch'), config.BOBCLAW_SECRET,
        owner=state.get('user_id'), conversation=state.get('conversation_id'), face=state.get('face_id')) else None
    return current.set((accepted, state.get('workflow_context_vouch')) if accepted else None)


async def dispatch(admission, value, operation, args):
    """Called only after verifying the gateway signature and current authority."""
    from core.workflow_surface import WorkflowSurface
    face = value['face_id']
    allowed = ASSISTANT_OPERATIONS if face in {'assistant', 'planner'} else READ_OPERATIONS if face == 'orchestrator' else ()
    if operation not in allowed:
        raise ExecutionError('not_found', 404)
    if not isinstance(args, dict) or 'origin_conversation_id' in args or 'assistant_origin' in args:
        raise ExecutionError('unsupported_fields',422,'args: expected an object without origin_conversation_id or assistant_origin; the authenticated context supplies the origin')
    origin = value['conversation_id'] if face in {'assistant', 'planner'} else None
    service = WorkflowSurface(admission, assistant_origin=origin)
    arguments = {**args, 'contract_version': 1}
    if operation == 'propose':
        arguments['origin_conversation_id'] = origin
    result = await service.command(Authority(**value['authority']),
        'workflows.' + ('run' if operation == 'report' else operation), arguments)
    if operation == 'report':
        return {'report': result['run']['report']}
    if operation == 'propose':
        p = result['proposal']
        result['action'] = {'type': 'workflow', 'title': p['coverage']['definition']['name'],
            'proposal_id': p['id'], 'proposal_revision': p['revision'], 'proposal_digest': p['digest']}
    return result


@router.post('/api/chat-workflows/{operation}')
async def command(request):
    from core.config import config
    from core.execution.api import SERVICE_KEY, lane_id, policy_epoch, check_resources
    from core.execution.owner import owner_mapping, check_epoch
    try:
        body = await request.json()
        only(body, {'context', 'vouch', 'args'})
        value = body.get('context') or {}
        if not valid(value, body.get('vouch'), config.BOBCLAW_SECRET,
                owner=value.get('authority', {}).get('owner'),
                conversation=value.get('conversation_id'), face=value.get('face_id')):
            raise ExecutionError('workflow_context_unavailable', 403)
        auth = Authority(**value['authority'])
        if auth.lane != lane_id() or auth.policy_epoch != policy_epoch(config.BOBCLAW_SECRET):
            raise ExecutionError('authorization_revoked', 403)
        if owner_mapping(auth.owner):
            await check_epoch(auth)
        # The signed conversation must still belong to this caller. No model or
        # RPC-selected conversation is allowed to stand in for this binding.
        service = request.app.get(SERVICE_KEY)
        if service is None:
            raise ExecutionError('execution_storage_unavailable', 503)
        await check_resources(service.store.pool, auth, {'conversation_id': value['conversation_id']})
        result = await dispatch(service, value, request.match_info['operation'], body.get('args'))
        return web.json_response(result)
    except ExecutionError as exc:
        return web.json_response({'error': {'code': exc.code, 'detail': exc.detail}}, status=exc.status)
    except (ValueError, TypeError, KeyError, AttributeError):
        return web.json_response({'error': {'code': 'invalid_args', 'detail': 'args: expected a JSON request object with context, vouch and args'}}, status=422)
    except Exception:
        logging.getLogger(__name__).exception('Workflow tools unavailable')
        return web.json_response({'error': {'code': 'execution_storage_unavailable'}}, status=503)


async def call(operation, args):
    import aiohttp
    from core.config import config
    bound = current.get()
    if not bound or bound[0]['exp'] <= time.time():
        return {'error': {'code': 'workflow_context_unavailable'}}
    value, proof = bound
    if operation not in (ASSISTANT_OPERATIONS if value['face_id'] in {'assistant', 'planner'} else READ_OPERATIONS):
        return {'error': {'code': 'not_found'}}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40)) as session:
            async with session.post(f'http://127.0.0.1:{config.PORT}/api/chat-workflows/{operation}',
                    json={'context': value, 'vouch': proof, 'args': args}) as response:
                return await response.json()
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return {'error': {'code': 'workflow_outcome_unknown',
            'detail': 'Read status or replay the exact same request_key and arguments. Observation failure does not stop a run.'}}
