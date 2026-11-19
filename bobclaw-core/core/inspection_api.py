"""Signed read-only gateway and invocation boundaries; no owner elevation."""
import hmac
import time
from aiohttp import web

from core.execution.models import Authority, ExecutionError, only
from core.inspection import Inspection, OPERATIONS
from core.permissions import compute_scope_vouch, canonical_pipe_json
from core.portal_resources import ResourceError

router = web.RouteTableDef()


def signature(envelope, secret):
    return compute_scope_vouch('bob-inspection/v1\n'+canonical_pipe_json(envelope), secret)


def valid(envelope, proof, secret):
    try:
        return (isinstance(envelope,dict) and set(envelope)=={'authority','faces','operation','args','iat','exp'}
            and envelope['operation'] in OPERATIONS and isinstance(envelope['args'],dict)
            and isinstance(envelope['faces'],list) and 0<len(envelope['faces'])<=32
            and all(isinstance(f,str) for f in envelope['faces'])
            and bool({'assistant','planner'} & set(envelope['faces']))
            and type(envelope['iat']) is int and type(envelope['exp']) is int
            and 0<envelope['exp']-envelope['iat']<=60 and envelope['iat']<=time.time()<envelope['exp']
            and isinstance(proof,str) and bool(signature(envelope,secret))
            and hmac.compare_digest(signature(envelope,secret),proof))
    except (KeyError,TypeError,ValueError):
        return False


async def perform(request, auth, args, faces):
    from core.execution.api import SERVICE_KEY
    service = request.app.get(SERVICE_KEY)
    if service is None:
        raise ExecutionError('inspection_storage_unavailable',503)
    return await Inspection(service).command(auth, request.match_info['operation'], args, visible_faces=faces)


def response_error(exc):
    return web.json_response({'error':{'code':exc.code}}, status=exc.status)


@router.post('/api/inspection/{operation}')
async def command(request):
    from core.config import config
    try:
        body = await request.json()
        only(body, {'envelope','vouch'})
        envelope = body.get('envelope')
        if not valid(envelope, body.get('vouch'), config.BOBCLAW_SECRET) or envelope['operation']!=request.match_info['operation']:
            raise ExecutionError('inspection_not_authorized',403)
        result = await perform(request, Authority(**envelope['authority']), envelope['args'], envelope['faces'])
        return web.json_response(result)
    except (ExecutionError, ResourceError) as exc:
        return response_error(exc)
    except (TypeError, ValueError, KeyError):
        return web.json_response({'error':{'code':'invalid_inspection'}},status=422)
    except Exception:
        # No raw exception, stderr, filesystem path or credential in a response.
        return web.json_response({'error':{'code':'inspection_unavailable'}},status=503)


@router.post('/api/chat-inspection/{operation}')
async def chat_command(request):
    from core.config import config
    from core.tools.workflow_context import valid as context_valid
    from core.faces.registry import get_default_registry
    from core.inspection import authorize
    from core.execution.api import SERVICE_KEY
    import uuid
    try:
        body = await request.json()
        only(body, {'context','vouch','args'})
        value = body.get('context') or {}
        if (value.get('face_id') not in {'assistant','planner'} or not context_valid(value,body.get('vouch'),config.BOBCLAW_SECRET,
            owner=value.get('authority',{}).get('owner'), conversation=value.get('conversation_id'),face=value.get('face_id'))):
            raise ExecutionError('inspection_context_unavailable',403)
        auth = Authority(**value['authority'])
        await authorize(auth)
        service = request.app.get(SERVICE_KEY)
        if service is None:
            raise ExecutionError('inspection_storage_unavailable',503)
        if not await service.store.pool.fetchval('SELECT id FROM conversations WHERE id=$1 AND user_id=$2',
                uuid.UUID(value['conversation_id']),auth.owner):
            raise ExecutionError('conversation_not_found',404)
        faces = value.get('inspection_faces', [value['face_id']])
        if value['face_id'] not in faces:
            raise ExecutionError('inspection_not_authorized',403)
        result = await perform(request,auth,body.get('args'),faces)
        return web.json_response(result)
    except (ExecutionError,ResourceError) as exc:
        return response_error(exc)
    except (TypeError,ValueError,KeyError):
        return web.json_response({'error':{'code':'invalid_inspection'}},status=422)
    except Exception:
        return web.json_response({'error':{'code':'inspection_unavailable'}},status=503)
