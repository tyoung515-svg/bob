"""Transport for native pulls and signed native receipts. No approval route."""
from aiohttp import web
from routers.execution import proxy
router=web.RouteTableDef()


@router.post('/api/pc/{operation}')
async def command(request):
    operation=request.match_info['operation']
    if operation not in {'pending','claim','receipt','end'}:
        return web.json_response({'error':{'code':'native_approval_required'}},status=403)
    if request['user'].get('token_type')!='portal':
        return web.json_response({'error':{'code':'native_portal_required'}},status=403)
    try:
        args=await request.json()
        if not isinstance(args,dict) or type(args.get('contract_version')) is not int or args.pop('contract_version')!=2:
            raise ValueError()
        return await proxy(request,'pc.'+operation,args)
    except (ValueError,TypeError):
        return web.json_response({'error':{'code':'invalid_args'}},status=422)
