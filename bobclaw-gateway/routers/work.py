"""Bounded day-one work routes, sharing verified execution/owner transport."""
from aiohttp import web
from routers.execution import proxy

router=web.RouteTableDef()
OPERATIONS={'catalog','preparation','status','start'}


async def dispatch(request,operation):
    try:
        args=await request.json()
        if not isinstance(args,dict) or type(args.get('contract_version')) is not int or args.pop('contract_version')!=2:
            raise ValueError()
        return await proxy(request,'work.'+operation,args)
    except (ValueError,TypeError):
        return web.json_response({'error':{'code':'invalid_args'}},status=422)


# Exact owner routes must precede the general work route. Their canonical paths
# bind one operation each in the existing owner-audience middleware.
@router.post('/api/work/workspace.create')
async def workspace_create(request):return await dispatch(request,'workspace.create')


@router.post('/api/work/publish')
async def publish(request):return await dispatch(request,'publish')


@router.post('/api/work/{operation}')
async def command(request):
    operation=request.match_info['operation']
    if operation not in OPERATIONS:return web.json_response({'error':{'code':'unsupported_operation'}},status=404)
    return await dispatch(request,operation)
