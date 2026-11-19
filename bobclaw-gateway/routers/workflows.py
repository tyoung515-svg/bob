"""Strict workflow/v1 routes over the existing verified execution vouch path."""
import asyncio
import json
import re
from aiohttp import web
from core.execution.models import ExecutionError
from core.execution.workflows import FIELDS
from routers.execution import proxy

FIELDS = {**FIELDS, 'proposals': {'cursor', 'limit', 'include_expired'}, 'events': FIELDS['events'] | {'wait'}}
EVENT_WAIT_MAX = 25
EVENT_CHECK_INTERVAL = 1.0  # one core read a second per watched idle run

router = web.RouteTableDef()
ROUTES = (
    ('GET','/workflows/catalog','catalog'), ('GET','/workflows','list'),
    ('GET','/workflows/{id}','get'), ('POST','/workflows','save'),
    ('POST','/workflows/{id}/copy','copy'), ('POST','/workflows/{id}/delete','delete'),
    ('POST','/workflow-proposals','propose'), ('GET','/workflow-proposals','proposals'),
    ('GET','/workflow-proposals/{proposal_id}','proposal'),
    ('POST','/workflow-proposals/{proposal_id}/propose-again','propose_again'),
    ('POST','/workflow-proposals/{proposal_id}/start','grant_start'),
    ('GET','/workflow-runs','runs'), ('GET','/workflow-runs/{run_id}','run'),
    ('GET','/workflow-runs/{run_id}/events','events'),
    ('POST','/workflow-runs/{run_id}/pause','pause'), ('POST','/workflow-runs/{run_id}/resume','resume'),
    ('POST','/workflow-runs/{run_id}/stop','stop'), ('POST','/workflow-runs/{run_id}/messages','message'),
    ('POST','/workflow-runs/{run_id}/gates/{wait_id}','gate'),
    ('GET','/workflow-runs/{run_id}/artifacts/{artifact_id}','artifact'),
)


def handler(operation):
    async def command(request):
        try:
            args = dict(request.query) if request.method == 'GET' else await request.json()
            if (not isinstance(args, dict) or set(args) - (FIELDS[operation] | {'contract_version'})
                    or set(args) & set(request.match_info)):
                raise ValueError()
            version = args.get('contract_version')
            if (version != '1' if request.method=='GET' else type(version) is not int or version != 1):
                raise ValueError()
            args['contract_version'] = 1
            if operation == 'proposals' and 'include_expired' in args:
                if args['include_expired'] not in {'true','false'}:
                    raise ValueError()
                args['include_expired'] = args['include_expired'] == 'true'
            if operation == 'propose_again':
                claims = request['user']
                if claims.get('token_type','human') not in {'human','portal'}:
                    return web.json_response({'error':{'code':'proposal_again_not_permitted',
                        'detail':'Only the signed-in owner can ask for this proposal again.'}},status=403)
            wait = args.pop('wait', '0') if operation == 'events' else '0'
            if not re.fullmatch(r'(?:0|[1-9][0-9]*)(?:\.[0-9]+)?', wait) or not 0 <= float(wait) <= EVENT_WAIT_MAX:
                raise ValueError()
            deadline = asyncio.get_running_loop().time() + float(wait)
            while True:
                response = await proxy(request, 'workflows.'+operation, {**args, **request.match_info})
                # Re-enter the vouched read on each check. A wait holds no DB
                # transaction and never hides a revoked resource or a refusal.
                if operation != 'events' or response.status != 200 or not float(wait):
                    return response
                page = json.loads(response.body)
                if page.get('events'):
                    return response
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return response
                await asyncio.sleep(min(EVENT_CHECK_INTERVAL, remaining))
        except (ValueError, TypeError, ExecutionError):
            return web.json_response({'error':{'code':'invalid_args','detail':None}},status=422)
    command.__name__='workflow_'+operation
    return command


for _method, _path, _op in ROUTES:
    router.route(_method,_path)(handler(_op))
