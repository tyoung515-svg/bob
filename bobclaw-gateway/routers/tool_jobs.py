"""Exact /api tool-job routes; shared vouched execution/owner transport."""
from aiohttp import web
from routers.execution import proxy

router = web.RouteTableDef()


def query(request, fields):
    if len(request.query) != len(set(request.query)) or set(request.query)-set(fields)-{'contract_version'}:
        raise ValueError()
    if request.query.get('contract_version','2') != '2': raise ValueError()
    args={k:v for k,v in request.query.items() if k!='contract_version'}
    for key in ('limit','offset','max_bytes','after_event_seq'):
        if key in args: args[key]=int(args[key])
    return args


async def dispatch(request,op,*,fields=(),mutation=False):
    try:
        if mutation:
            args=await request.json()
            if not isinstance(args,dict): raise ValueError()
            version=args.pop('contract_version',None)
            if version is not None and (type(version) is not int or version!=2): raise ValueError()
            if op in {'jobs.start','jobs.decide'} and version!=2:
                return web.json_response({'error':{'code':'client_upgrade_required','detail':'tool-jobs/2 required'}},status=409)
            if any(k in args for k in ('job_id','filename')): raise ValueError()
            key=request.headers.get('Idempotency-Key')
            if key:
                if 'request_key' in args and args['request_key']!=key: raise ValueError()
                args['request_key']=key
            if op=='jobs.start': args['contract_version']=2
        else:
            args=query(request,fields)
        if 'job_id' in request.match_info: args['job_id']=request.match_info['job_id']
        if 'filename' in request.match_info: args['filename']=request.match_info['filename']
        return await proxy(request,op,args)
    except (ValueError,TypeError):
        return web.json_response({'error':{'code':'invalid_args'}},status=422)


@router.get('/api/tool-job-attachments')
async def attachments(request): return await dispatch(request,'jobs.attachments')

@router.get('/api/tool-jobs')
async def listing(request): return await dispatch(request,'jobs.list',fields={'cursor','limit'})

@router.post('/api/tool-jobs')
async def start(request): return await dispatch(request,'jobs.start',mutation=True)

@router.get('/api/tool-jobs/{job_id}/audit')
async def audit(request): return await dispatch(request,'jobs.audit',fields={'after_event_seq'})

@router.post('/api/tool-jobs/{job_id}/cancel')
async def cancel(request): return await dispatch(request,'jobs.cancel',mutation=True)

@router.get('/api/tool-jobs/{job_id}/proposal')
async def proposal(request): return await dispatch(request,'jobs.proposal')

@router.post('/api/tool-jobs/{job_id}/proposal/decision')
async def decide(request): return await dispatch(request,'jobs.decide',mutation=True)

@router.get('/api/tool-jobs/{job_id}/artifacts/{filename}')
async def artifact(request): return await dispatch(request,'jobs.artifact',fields={'offset','max_bytes'})
