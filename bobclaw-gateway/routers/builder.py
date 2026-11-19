"""Exact Builder owner routes; model-facing routes cannot set authority."""
from aiohttp import web
from routers.execution import proxy

router = web.RouteTableDef()


@router.post('/api/workspace/browse')
async def browse(request):
    try:
        args = await request.json()
        if not isinstance(args,dict) or type(args.get('contract_version')) is not int or args.pop('contract_version')!=2:
            raise ValueError()
        return await proxy(request,'workspace.browse',args)
    except (ValueError,TypeError):
        return web.json_response({'error':{'code':'invalid_args'}},status=422)


async def dispatch(request, operation):
    try:
        args = await request.json()
        if not isinstance(args,dict) or type(args.get('contract_version')) is not int or args.pop('contract_version')!=2:
            raise ValueError()
        return await proxy(request,'builder.'+operation,args)
    except (ValueError,TypeError):
        return web.json_response({'error':{'code':'invalid_args'}},status=422)


@router.post('/api/builder/mode.set')
async def mode_set(request): return await dispatch(request,'mode.set')


@router.post('/api/builder/approve')
async def approve(request): return await dispatch(request,'approve')


@router.post('/api/builder/network.set')
async def network_set(request): return await dispatch(request,'network.set')


@router.post('/api/builder/{operation}')
async def command(request):
    operation=request.match_info['operation']
    if operation not in {'mode.get','preview','approval.get','approvals.get','network_log','task.end','project.result'}:
        return web.json_response({'error':{'code':'unsupported_operation'}},status=404)
    return await dispatch(request,operation)
