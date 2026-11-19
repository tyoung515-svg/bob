"""Scoped MCP inspection, independently vouched to this lane's core."""
import time
import aiohttp
from aiohttp import web
from config import config
from core.inspection import OPERATIONS
from core.inspection_api import signature
from core.execution.models import ExecutionError
from routers.execution import authority_from_claims

router = web.RouteTableDef()


@router.post('/bob-inspection/{operation}')
async def inspect(request):
    try:
        operation = request.match_info['operation']
        claims = request['user']
        allowed = claims.get('faces')
        allowed = [] if allowed is None else allowed
        if (operation not in OPERATIONS or not isinstance(allowed,list) or len(allowed)>32
                or any(not isinstance(f,str) for f in allowed)
                or allowed and not {'assistant','planner'} & set(allowed)):
            raise ExecutionError('inspection_not_authorized',403)
        args = await request.json()
        if not isinstance(args,dict):
            raise ExecutionError('invalid_inspection')
        auth = authority_from_claims(claims)
        from core.execution.owner import current_epoch, owner_mapping
        from dataclasses import replace
        if not owner_mapping(auth.owner):
            raise ExecutionError('inspection_not_authorized',403)
        auth = replace(auth,credential_epoch=await current_epoch(auth.lane,auth.owner))
        # Gateway-authenticated token faces remain exact even in household-open
        # mode. Empty is the existing unrestricted-face convention.
        from routers.faces import _proxy_json
        roster = await _proxy_json('GET','/api/faces')
        faces = [f['id'] for f in roster if not allowed or f['id'] in allowed]
        now = int(time.time())
        envelope = {'authority':auth.as_dict(),'faces':faces,'operation':operation,'args':args,'iat':now,'exp':now+30}
        proof = signature(envelope,config.BOBCLAW_SECRET)
        if not proof:
            raise ExecutionError('inspection_not_authorized',403)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=100)) as session:
            async with session.post(config.CORE_URL.rstrip('/')+'/api/inspection/'+operation,
                    json={'envelope':envelope,'vouch':proof}) as response:
                return web.Response(body=await response.read(),status=response.status,content_type='application/json')
    except ExecutionError as exc:
        return web.json_response({'error':{'code':exc.code}},status=exc.status)
    except (ValueError,TypeError,KeyError):
        return web.json_response({'error':{'code':'invalid_inspection'}},status=422)
    except (aiohttp.ClientError,TimeoutError):
        return web.json_response({'error':{'code':'inspection_unavailable'}},status=503)
